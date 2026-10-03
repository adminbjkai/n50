#!/usr/bin/env python3
"""
server.py — the n50.bjk.ai control room (stdlib only).

Runs one engine at a time (`engines/vNN.py`, dry run or publish), streams its output over
SSE, keeps every run's log under `_tmp/runs/`, and serves a small JSON API over the shared
tracker, master CSV and audit files. The UI lives in `web/`.

Production runs under systemd socket activation (`deploy/n50-runner.socket`): the process
starts on the first request and exits after N50_IDLE_EXIT seconds without traffic, so it
costs nothing while nobody is using it. Run `python3 server.py` for a plain local server.
"""

import collections
import csv
import queue
import gzip
import hashlib
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
ENGINE_DIR = ROOT / "engines"
WEB_DIR = ROOT / "web"
TMP = ROOT / "_tmp"
RUNS_DIR = TMP / "runs"
TRACKER = ROOT / "notion-selfhosted-tracker.json"
MASTER_CSV = ROOT / "notion-selfhosted-master.csv"
PORT = int(os.environ.get("N50_PORT", "8055"))
IDLE_EXIT = int(os.environ.get("N50_IDLE_EXIT", "900"))  # only used when socket-activated
KEEP_RUNS = 60
LOG_TAIL = 6000

# Ordered as shown in the UI. `size` is the set size the engine publishes. Every engine's
# standard publish refuses anything but exactly `size` picks (it exits without writing).
ENGINES = [
    {"id": "v17", "name": "Smart", "size": 50, "group": "main",
     "about": "GraphQL search sliced by creation date and self-host topics, a strict quality "
              "gate, and interest ranking. Fastest and the strictest on junk."},
    {"id": "v16", "name": "Trendshift", "size": 50, "group": "main",
     "about": "Adds live Trendshift trending feeds to v15's governed search, with a "
              "momentum bonus for trending repos."},
    {"id": "v15", "name": "Safe 50", "size": 50, "group": "main",
     "about": "Product-diversity floors with a GitHub rate-limit governor and warm caches; "
              "a thin pool ends cleanly instead of crashing."},
    {"id": "v11", "name": "Bench", "size": 50, "group": "main",
     "about": "The original fast runner: bench-backed discovery with early exit and looser "
              "quality rules."},
    {"id": "v12", "name": "Proof-first", "size": 50, "group": "older",
     "about": "Every pick needs tier A/B/C deploy proof; hard category caps."},
    {"id": "v13", "name": "Diversity", "size": 50, "group": "older",
     "about": "Family floors (files, knowledge, media, PDF…) with AI capped near 3 per set."},
    {"id": "v14", "name": "200 per set", "size": 200, "group": "older",
     "about": "Publishes 200 repos per set with reject cache, carry pool and near-miss parking."},
]
ENGINE_BY_ID = {e["id"]: e for e in ENGINES}
DEFAULT_ENGINE = "v17"
MODES = {"dry": ["--dry-run"], "publish": []}
AUDIT_RE = re.compile(r"^set(\d+)(?:_(v\d+))?_(verified|DRYRUN)_audit\.json$")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-v\d+-(dry|publish)$")


# --------------------------------------------------------------------------- data
class Cached:
    """Recompute a value only when the files it depends on change (by mtime)."""

    def __init__(self, fn, *paths):
        self.fn, self.paths, self.key, self.val = fn, paths, None, None
        self.lock = threading.Lock()

    def get(self):
        key = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in self.paths)
        with self.lock:
            if key != self.key:
                self.val, self.key = self.fn(), key
            return self.val


def _audit_index():
    """setNum -> list of audit files (published first, newest first)."""
    idx = collections.defaultdict(list)
    for p in TMP.glob("set*_audit.json"):
        m = AUDIT_RE.match(p.name)
        if m:
            idx[int(m.group(1))].append({"file": p.name, "engine": m.group(2),
                                         "kind": "dry" if m.group(3) == "DRYRUN" else "published",
                                         "mtime": p.stat().st_mtime})
    for v in idx.values():
        v.sort(key=lambda a: (a["kind"] != "published", -a["mtime"]))
    return dict(idx)


AUDITS = Cached(_audit_index, TMP)


def _tracker_view():
    t = json.loads(TRACKER.read_text())
    audits = AUDITS.get()
    sets = []
    for p in t.get("completedPages", []):
        n = p.get("setNum") or 0
        if not n:
            continue
        pub = next((a for a in audits.get(n, []) if a["kind"] == "published"), None)
        engine = p.get("version") or (pub or {}).get("engine")
        count = p.get("count") or len(p.get("repos", []))
        size = 200 if engine == "v14" or "200 More" in p.get("title", "") else 50
        sets.append({"n": n, "title": p.get("title", ""), "count": count, "size": size,
                     "engine": engine, "url": p.get("pageUrl"),
                     "at": round(pub["mtime"]) if pub else None, "special": bool(p.get("special"))})
    sets.sort(key=lambda s: s["n"])
    pid = (t.get("parentPageId") or "").replace("-", "")
    return {"completed": t.get("completedSets", 0), "used": len(t.get("usedRepoUrls", [])),
            "parent": t.get("parentPageTitle"), "parentUrl": f"https://www.notion.so/{pid}" if pid else None,
            "sets": sets}


TRACKER_VIEW = Cached(_tracker_view, TRACKER, TMP)


def engine_stats(sets):
    out = {}
    for e in ENGINES:
        mine = [s for s in sets if s["engine"] == e["id"] and not s["special"]]
        recent = mine[-8:]
        out[e["id"]] = {
            "sets": len(mine),
            "recent": [{"n": s["n"], "count": s["count"], "size": s["size"]} for s in recent],
            "avgFill": round(sum(min(1, s["count"] / s["size"]) for s in recent) / len(recent), 3)
            if recent else None,
            "lastAt": max((s["at"] or 0 for s in mine), default=0) or None,
        }
    return out


def read_csv_rows(pred, limit=None):
    out = []
    try:
        with open(MASTER_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if pred(row):
                    out.append(row)
                    if limit and len(out) >= limit:
                        break
    except OSError:
        pass
    return out


def _repo_from_url(u):
    return (u or "").split("github.com/")[-1].strip("/")


def normalise_picks(audit):
    """Both audit shapes (v17 `picks`, v11–v16 `selected`) -> one list."""
    out = []
    for i, p in enumerate(audit.get("picks") or audit.get("selected") or [], 1):
        if not isinstance(p, dict):
            continue
        repo = p.get("repo") or _repo_from_url(p.get("url"))
        out.append({
            "rank": i, "repo": repo, "url": p.get("url") or f"https://github.com/{repo}",
            "category": p.get("category") or "", "stars": p.get("stars"),
            "score": p.get("score"), "description": p.get("description") or "",
            "compose": p.get("compose"), "docker": p.get("docker", p.get("dockerfile")),
            "language": p.get("language"),
        })
    return out


def load_audit(name):
    if not AUDIT_RE.match(name):
        return None
    p = TMP / name
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def set_detail(n):
    """Picks for a set: published sets use the master CSV (authoritative), merged with the
    audit's scores; unpublished sets fall back to their newest dry-run audit."""
    files = AUDITS.get().get(n, [])
    audit_file = files[0] if files else None
    audit = load_audit(audit_file["file"]) if audit_file else None
    by_url = {x["url"].lower(): x for x in normalise_picks(audit or {})}
    rows = read_csv_rows(lambda r: r.get("set") == str(n))
    if rows:
        picks = []
        for i, r in enumerate(rows, 1):
            extra = by_url.get(r["repo_url"].lower(), {})
            picks.append({**extra, "rank": i, "repo": r["repo_name"], "url": r["repo_url"],
                          "category": r["category"] or extra.get("category", ""),
                          "description": r["description"] or extra.get("description", "")})
    else:
        picks = list(by_url.values())
    view = TRACKER_VIEW.get()
    meta = next((s for s in view["sets"] if s["n"] == n), None)
    return {"n": n, "set": meta, "audit": audit_file, "picks": picks,
            "stats": (audit or {}).get("stats"), "timing": (audit or {}).get("timingSeconds")}


def _set_num(v):
    m = re.search(r"\d+", v or "")
    return int(m.group()) if m else 0


def search_catalogue(q, limit=40):
    q = q.strip().lower()
    if len(q) < 2:
        return [], 0
    terms = q.split()

    def hit(r):
        blob = f"{r['repo_name']} {r['description']} {r['category']}".lower()
        return all(t in blob for t in terms)

    rows = read_csv_rows(hit)
    name_first = sorted(rows, key=lambda r: (q not in r["repo_name"].lower(), -_set_num(r["set"])))
    return [{"set": r["set"], "repo": r["repo_name"], "url": r["repo_url"],
             "category": r["category"], "description": r["description"]}
            for r in name_first[:limit]], len(rows)


# --------------------------------------------------------------------------- runs
class Runner:
    """One engine subprocess at a time; lines fan out to SSE subscribers and a log file."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.run = None          # metadata of the current or most recent run
        self.lines = collections.deque(maxlen=LOG_TAIL)
        self.subs = set()
        self.subs_lock = threading.Lock()
        self.closing = False
        self._restore_last()

    def _restore_last(self):
        """After an idle exit the next visit starts a fresh process; show the last run again."""
        metas = sorted(RUNS_DIR.glob("*.json")) if RUNS_DIR.exists() else []
        for meta in reversed(metas):
            try:
                self.run = json.loads(meta.read_text())
                log = meta.with_suffix(".log")
                self.lines.extend(log.read_text(errors="replace").splitlines()[-LOG_TAIL:])
                return
            except (OSError, ValueError):
                continue

    def broadcast(self, event, data):
        with self.subs_lock:
            for q in list(self.subs):
                q.put((event, data))

    def state(self):
        r = self.run or {}
        return {"active": bool(self.proc), **{k: r.get(k) for k in
                ("id", "engine", "mode", "started", "ended", "rc", "elapsed", "result", "stopped")}}

    def start(self, engine, mode):
        if engine not in ENGINE_BY_ID:
            return False, f"Unknown engine {engine!r}."
        if mode not in MODES:
            return False, f"Unknown mode {mode!r}."
        script = ENGINE_DIR / f"{engine}.py"
        if not script.exists():
            return False, f"Engine file missing: engines/{engine}.py"
        with self.lock:
            if self.closing:
                return False, "The server is restarting; try again in a few seconds."
            if self.proc:
                return False, "A run is already in progress."
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            started = time.time()
            rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(started)) + f"-{engine}-{mode}"
            argv = ["python3", "-u", str(script)] + MODES[mode]
            self.proc = subprocess.Popen(
                argv, cwd=str(ENGINE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                start_new_session=True)
            self.run = {"id": rid, "engine": engine, "mode": mode, "started": started,
                        "ended": None, "rc": None, "elapsed": None, "result": None, "stopped": False}
            self.lines.clear()
        threading.Thread(target=self._pump, args=(self.proc, rid), daemon=True).start()
        self.broadcast("state", self.state())
        return True, rid

    def _pump(self, proc, rid):
        log_path = RUNS_DIR / f"{rid}.log"
        with open(log_path, "w", encoding="utf-8") as log:
            head = f"$ python3 engines/{self.run['engine']}.py {' '.join(MODES[self.run['mode']])}".rstrip()
            for line in [head, ""]:
                self.lines.append(line)
                log.write(line + "\n")
                self.broadcast("line", line)
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                self.lines.append(line)
                log.write(line + "\n")
                log.flush()
                self.broadcast("line", line)
        rc = proc.wait()
        run = self.run
        run.update(ended=time.time(), rc=rc, elapsed=round(time.time() - run["started"], 1),
                   result=self._find_result(run["started"]))
        (RUNS_DIR / f"{rid}.json").write_text(json.dumps(run, indent=1))
        self._prune()
        with self.lock:
            self.proc = None
        self.broadcast("done", self.state())

    @staticmethod
    def _find_result(started):
        """The audit file this run wrote (engines write one per dry run or publish)."""
        best = None
        for p in TMP.glob("set*_audit.json"):
            m = AUDIT_RE.match(p.name)
            mt = p.stat().st_mtime
            if m and mt >= started - 1 and (not best or mt > best[1]):
                best = (p, mt, m)
        if not best:
            return None
        p, _, m = best
        audit = load_audit(p.name) or {}
        picks = audit.get("picks") or audit.get("selected") or []
        return {"file": p.name, "set": int(m.group(1)), "count": len(picks),
                "kind": "dry" if m.group(3) == "DRYRUN" else "published",
                "publishable": audit.get("publishable", True)}

    def _prune(self):
        logs = sorted(RUNS_DIR.glob("*.log"))
        for old in logs[:-KEEP_RUNS]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)

    def stop(self):
        with self.lock:
            proc, run = self.proc, self.run
            if not proc:
                return False, "Nothing is running."
            if run["mode"] != "dry":
                return False, "A publish can't be stopped midway; it would leave Notion and the tracker out of step."
            run["stopped"] = True
        def kill(sig):
            try:
                if proc.poll() is None:
                    os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        kill(signal.SIGTERM)
        threading.Timer(5, kill, args=(signal.SIGKILL,)).start()
        return True, "stopping"


RUNNER = Runner()


def recent_runs(limit=20):
    out = []
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- http
_last_request = time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "n50"

    def log_message(self, *a):
        pass

    def send(self, body, code=200, ctype="application/json; charset=utf-8", cache="no-cache", etag=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, separators=(",", ":")).encode())
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        gz = len(data) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "") \
            and not ctype.startswith(("font/", "image/png"))
        if gz:
            data = gzip.compress(data, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def error(self, code, msg):
        self.send({"ok": False, "error": msg}, code)

    # ---- GET
    def do_GET(self):
        global _last_request
        _last_request = time.time()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        try:
            if path == "/" or path == "/index.html":
                return self.static("index.html")
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            if path == "/health":
                return self.send({"ok": True, "active": bool(RUNNER.proc),
                                  "engines": [e["id"] for e in ENGINES], "default": DEFAULT_ENGINE})
            if path == "/api/overview":
                return self.send(self.overview())
            if path == "/api/set":
                n = q.get("n", "")
                if not n.isdigit():
                    return self.error(400, "Set number must be a positive integer.")
                return self.send(set_detail(int(n)))
            if path == "/api/search":
                hits, total = search_catalogue(q.get("q", ""))
                return self.send({"hits": hits, "total": total})
            if path == "/api/runs":
                return self.send({"runs": recent_runs()})
            if path == "/api/run-log":
                rid = q.get("id", "")
                if not RUN_ID_RE.match(rid):
                    return self.error(400, "Bad run id.")
                p = RUNS_DIR / f"{rid}.log"
                if not p.exists():
                    return self.error(404, "That run's log has been pruned.")
                meta = RUNS_DIR / f"{rid}.json"
                return self.send({"run": json.loads(meta.read_text()) if meta.exists() else None,
                                  "lines": p.read_text(errors="replace").splitlines()[-LOG_TAIL:]})
            if path == "/api/stream":
                return self.stream()
            self.error(404, "Not found.")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 — never kill the server on one bad request
            self.error(500, f"{type(exc).__name__}: {exc}")

    do_HEAD = do_GET

    def overview(self):
        view = TRACKER_VIEW.get()
        nxt = view["completed"] + 1
        dry = next((a for a in AUDITS.get().get(nxt, []) if a["kind"] == "dry"), None)
        preview = None
        if dry:
            audit = load_audit(dry["file"]) or {}
            preview = {"file": dry["file"], "engine": dry["engine"], "at": round(dry["mtime"]),
                       "count": len(audit.get("picks") or audit.get("selected") or []),
                       "publishable": audit.get("publishable", True)}
        return {"series": {k: view[k] for k in ("completed", "used", "parent", "parentUrl")}, "next": nxt,
                "preview": preview, "engines": ENGINES, "default": DEFAULT_ENGINE,
                "stats": engine_stats(view["sets"]), "sets": view["sets"][::-1],
                "totalSets": len(view["sets"]), "run": RUNNER.state(), "runs": recent_runs(8)}

    def static(self, rel):
        p = (WEB_DIR / rel).resolve()
        if WEB_DIR not in p.parents or not p.is_file():
            return self.error(404, "Not found.")
        data = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if rel.startswith("fonts/") else "no-cache"
        self.send(data, 200, ctype, cache, etag='"' + hashlib.md5(data).hexdigest()[:16] + '"')

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = queue.SimpleQueue()
        with RUNNER.subs_lock:
            RUNNER.subs.add(q)
            hello = {"state": RUNNER.state(), "lines": list(RUNNER.lines) if RUNNER.run else []}
        try:
            self._event("hello", hello)
            while True:
                try:
                    self._event(*q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with RUNNER.subs_lock:
                RUNNER.subs.discard(q)

    def _event(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    # ---- POST (state-changing; custom header blocks cross-site form/img requests)
    def do_POST(self):
        global _last_request
        _last_request = time.time()
        if self.headers.get("X-N50") != "1":
            return self.error(403, "Missing X-N50 header.")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.error(400, "Body must be a JSON object.")
        path = urlparse(self.path).path
        if path == "/api/run":
            ok, msg = RUNNER.start(body.get("engine", DEFAULT_ENGINE), body.get("mode", "dry"))
            return self.send({"ok": True, "id": msg}) if ok else self.error(409, msg)
        if path == "/api/stop":
            ok, msg = RUNNER.stop()
            return self.send({"ok": True}) if ok else self.error(409, msg)
        self.error(404, "Not found.")


# --------------------------------------------------------------------------- main
def make_server():
    fds = int(os.environ.get("LISTEN_FDS", "0"))
    if fds and os.environ.get("LISTEN_PID") == str(os.getpid()):
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = socket.socket(fileno=3)  # systemd passes the listening socket as fd 3
        return server, True
    return ThreadingHTTPServer(("127.0.0.1", PORT), Handler), False


def main():
    server, activated = make_server()
    server.daemon_threads = True

    def shutdown():
        # A publish must not be cut off between the Notion write and the tracker update:
        # wait for it (the unit allows 5 minutes). A dry run is simply stopped.
        with RUNNER.lock:
            RUNNER.closing = True
            dry = bool(RUNNER.proc) and RUNNER.run["mode"] == "dry"
        if dry:
            RUNNER.stop()
        while RUNNER.proc:          # cleared only after the run record and log are saved
            time.sleep(0.2)
        server.shutdown()

    stop = lambda *_: threading.Thread(target=shutdown, daemon=True).start()  # noqa: E731
    signal.signal(signal.SIGTERM, stop)
    if activated and IDLE_EXIT > 0:
        def idle_watch():
            while True:
                time.sleep(30)
                if not RUNNER.proc and not RUNNER.subs and time.time() - _last_request > IDLE_EXIT:
                    stop()
                    return
        threading.Thread(target=idle_watch, daemon=True).start()
    print(f"n50 on 127.0.0.1:{PORT} ({'socket-activated, idle exit ' + str(IDLE_EXIT) + 's' if activated else 'standalone'})"
          f" · engines: {', '.join(e['id'] for e in ENGINES)}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
