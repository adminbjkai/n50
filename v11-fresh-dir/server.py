#!/usr/bin/env python3
"""
server.py — stdlib web UI for n50.bjk.ai (v11 + v12 + v13 runners).

Default remains **v11**. Toggle v12 (proof-first) or v13 (product-diversity).
All append to the same shared tracker/CSV; caches are version-scoped.

  • Dry run   -> python3 <runner> --dry-run
  • Publish   -> python3 <runner>   (Notion writes)

SSE live log. One run at a time. Nginx proxies :8055 (unchanged).
"""

import html
import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("N50_PORT", "8055"))
RUNNERS = {
    "v11": HERE / "v11.py",
    "v12": HERE / "v12.py",
    "v13": HERE / "v13.py",
    "v14": HERE / "v14.py",
    "v15": HERE / "v15.py",
    "v16": HERE / "v16.py",
    "v17": HERE / "v17.py",
}
if not RUNNERS["v11"].exists() and (HERE / "run_next_set_v11.py").exists():
    RUNNERS["v11"] = HERE / "run_next_set_v11.py"
if not RUNNERS["v12"].exists() and (HERE / "run_next_set_v12.py").exists():
    RUNNERS["v12"] = HERE / "run_next_set_v12.py"
if not RUNNERS["v13"].exists() and (HERE / "run_next_set_v13.py").exists():
    RUNNERS["v13"] = HERE / "run_next_set_v13.py"
if not RUNNERS["v14"].exists() and (HERE / "run_next_set_v14.py").exists():
    RUNNERS["v14"] = HERE / "run_next_set_v14.py"
if not RUNNERS["v15"].exists() and (HERE / "run_next_set_v15.py").exists():
    RUNNERS["v15"] = HERE / "run_next_set_v15.py"
if not RUNNERS["v16"].exists() and (HERE / "run_next_set_v16.py").exists():
    RUNNERS["v16"] = HERE / "run_next_set_v16.py"

# v16 is the recommended default: Trendshift stream discovery + safe 50/set.
DEFAULT_VERSION = "v17"

MODES = {
    "dry":     ["--dry-run"],
    "publish": [],
}

_lock = threading.Lock()
_run_state = {"active": False, "mode": None, "version": None, "started": 0.0}
_subs_lock = threading.Lock()
_subscribers = []


def _broadcast(event, data):
    payload = (event, data)
    with _subs_lock:
        for q in list(_subscribers):
            q.put(payload)


def _run_command(mode, version):
    script = RUNNERS.get(version) or RUNNERS[DEFAULT_VERSION]
    argv = ["python3", "-u", str(script)] + MODES[mode]
    _run_state.update(active=True, mode=mode, version=version, started=time.time())
    _broadcast("status", {"active": True, "mode": mode, "version": version})
    _broadcast("line", f"$ {' '.join(shlex.quote(a) for a in argv)}")
    _broadcast("line", f"  (cwd: {HERE} · runner: {version})")
    _broadcast("line", "")
    rc = -1
    try:
        proc = subprocess.Popen(
            argv, cwd=str(HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        for line in proc.stdout:
            _broadcast("line", line.rstrip("\n"))
        rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _broadcast("line", f"[server] failed to run: {exc!r}")
        rc = -1
    finally:
        elapsed = time.time() - _run_state["started"]
        _run_state.update(active=False, mode=None, version=None)
        _broadcast("done", {"rc": rc, "elapsed": round(elapsed, 1)})
        _broadcast("status", {"active": False, "mode": None, "version": None})
        _lock.release()


def _try_start(mode, version):
    if mode not in MODES:
        return False, f"unknown mode {mode!r}"
    if version not in RUNNERS:
        return False, f"unknown version {version!r}"
    if not RUNNERS[version].exists():
        return False, f"runner missing: {RUNNERS[version]}"
    if not _lock.acquire(blocking=False):
        return False, "A run is already in progress."
    threading.Thread(target=_run_command, args=(mode, version), daemon=True).start()
    return True, "started"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>n50 · v17 (smart) / v16 / v15 / v11 / v12 / v13 / v14</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #0d1117; color: #c9d1d9; padding: 24px; max-width: 980px; margin-inline: auto;
  }
  h1 { font-size: 18px; margin: 0 0 4px; color: #e6edf3; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 16px; }
  .bar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .ver {
    display: flex; gap: 0; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
  }
  .ver button {
    border: 0; border-radius: 0; margin: 0; background: #161b22; color: #8b949e;
    padding: 10px 14px; font-weight: 600;
  }
  .ver button.active { background: #238636; color: #fff; }
  .ver button:hover:not(:disabled):not(.active) { background: #21262d; color: #e6edf3; }
  button {
    font: inherit; font-weight: 600; padding: 10px 18px; border-radius: 8px;
    border: 1px solid #30363d; cursor: pointer; color: #e6edf3; background: #21262d;
  }
  button:hover:not(:disabled) { background: #30363d; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.dry { border-color: #238636; }
  button.dry:hover:not(:disabled) { background: #238636; }
  button.pub { border-color: #b6531c; color: #ffa657; }
  button.pub:hover:not(:disabled) { background: #b6531c; color: #fff; }
  .pill { font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid #30363d; }
  .pill.idle { color: #8b949e; }
  .pill.run  { color: #d29922; border-color: #d29922; }
  .pill.ok   { color: #3fb950; border-color: #3fb950; }
  .pill.err  { color: #f85149; border-color: #f85149; }
  #clock { color: #8b949e; font-size: 12px; }
  .note {
    font-size: 12px; color: #8b949e; border: 1px solid #30363d; border-radius: 8px;
    padding: 10px 12px; margin-bottom: 14px; background: #161b22; line-height: 1.45;
  }
  .note strong { color: #e6edf3; }
  .note .v12 { color: #58a6ff; }
  .note .v13 { color: #d2a8ff; }
  .note .v14 { color: #ffa657; }
  .note .v15 { color: #3fb950; }
  .note .v16 { color: #58a6ff; }
  .note .v17 { color: #3fb950; }
  .ver button.rec { color: #3fb950; }
  .ver button.rec.active { color: #fff; }
  #log {
    background: #010409; border: 1px solid #30363d; border-radius: 8px; padding: 14px;
    height: 58vh; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    font-size: 13px; color: #b9c1cb;
  }
  #log .cmd { color: #58a6ff; }
  #log .warn { color: #d29922; }
  #log .err  { color: #f85149; }
  #log .ok   { color: #3fb950; }
</style>
</head>
<body>
  <h1>n50 runner · self-hosted web apps series</h1>
  <div class="sub">Streams <code>v17.py</code> / <code>v16.py</code> / <code>v15.py</code> / <code>v11.py</code> / <code>v12.py</code> / <code>v13.py</code> / <code>v14.py</code> from <code>__HERE__</code>. Shared tracker/CSV.</div>

  <div class="bar">
    <div class="ver" role="group" aria-label="Runner version">
      <button type="button" id="ver-v17" class="rec active" onclick="setVersion('v17')">v17 · smart ★</button>
      <button type="button" id="ver-v16" onclick="setVersion('v16')">v16 · trendshift</button>
      <button type="button" id="ver-v15" onclick="setVersion('v15')">v15 · safe 50</button>
      <button type="button" id="ver-v11" onclick="setVersion('v11')">v11</button>
      <button type="button" id="ver-v12" onclick="setVersion('v12')">v12 proof</button>
      <button type="button" id="ver-v13" onclick="setVersion('v13')">v13 diversity</button>
      <button type="button" id="ver-v14" onclick="setVersion('v14')">v14 · 200/set</button>
    </div>
    <button id="btn-dry" class="dry" onclick="run('dry')">▶ Dry run (safe)</button>
    <button id="btn-pub" class="pub" onclick="publish()">⚠ Publish to Notion</button>
    <span id="pill" class="pill idle">idle</span>
    <span id="clock"></span>
  </div>

  <div class="note" id="ver-note"></div>
  <div id="log">Ready. v17 (smart curation) selected by default — GraphQL-sliced discovery, quality-first junk gate, interest ranking, full 50/set in ~1–2 min. All runners share the same tracker.\n</div>

<script>
const logEl = document.getElementById('log');
const pill  = document.getElementById('pill');
const clock = document.getElementById('clock');
const btnDry = document.getElementById('btn-dry');
const btnPub = document.getElementById('btn-pub');
const verNote = document.getElementById('ver-note');
const btnV11 = document.getElementById('ver-v11');
const btnV12 = document.getElementById('ver-v12');
const btnV13 = document.getElementById('ver-v13');
const btnV14 = document.getElementById('ver-v14');
const btnV15 = document.getElementById('ver-v15');
const btnV16 = document.getElementById('ver-v16');
const btnV17 = document.getElementById('ver-v17');
let es = null, t0 = 0, timer = null;
let version = 'v17';

const NOTES = {
  v17: '<strong class="v17">v17 active — recommended</strong> — <em>smart curation</em>. GitHub <strong>GraphQL search sliced by creation date × self-host topics</strong> (100 fully-described repos per call, off the 30/min search limit, 8-way parallel, adaptive memory skips dead slices) → cheap gate → Docker/compose proof fetched only for survivors. <strong>Quality gate</strong>: real stars (≥20, ≥10 if new), active in last 8 months, compose/Dockerfile, readable English, and no plugins/companions/clients for other apps, Helm/Ansible, bots, game-server wrappers, AI-account proxies, trading, multi-account farming. Ranked by <strong>interest</strong> (stars, ★/month momentum, releases, polish, life-app bonus, AI penalty). One repo per owner, ≤6 per category, AI ≤4. Rename/transfer + re-upload dedupe. Fills a real 50/50 in ~1–2 min.',
  v16: '<strong class="v16">v16 active — recommended</strong> — <em>Trendshift trending stream (Lane T)</em>. Solves candidate exhaustion on the 23k+ used repo corpus by pulling real-time active trending apps from <strong>Trendshift.io</strong> (daily, trending, weekly, monthly, yearly, topics/self-hosted) with 0 GitHub search calls. Trendshift momentum scoring bonus + proof-tier synergy + full v15 rate-limit governor and never-crash guarantee. Same shared tracker.',
  v15: '<strong class="v15">v15 active</strong> — <em>safe 50/set product diversity</em>. Everything v13 does (family floors, AI hard-capped ~3/50, theme bank), plus: a <strong>rate-limit governor</strong> that paces GitHub search under its 30/min limit (no more 300s of reactive sleeps), <strong>never crashes</strong> on a thin pool (ships the best available set, honestly reported, instead of exiting 1), and <strong>warm caches</strong> seeded from v14 so run 1 already skips known-bad repos. Same shared tracker.',
  v11: '<strong>v11 active</strong> — bench-backed fast discovery. Shared tracker.',
  v12: '<strong class="v12">v12 active</strong> — proof-first (tier A/B/C). Same tracker as v11.',
  v13: '<strong class="v13">v13 active</strong> — <em>product diversity</em>: family floors (files, knowledge, media, PDF, bookmarks, monitoring, sports, rust, …). AI hard-capped at ~3/50. Theme discovery bank. Same shared tracker. <em>Note: superseded by v15, which is v13’s diversity made safe.</em>',
  v14: '<strong class="v14">v14 active</strong> — <em>200 repos per set</em> (title reads “200 More …”). Set number continues the same series as the 50-repo sets. Candidate recycling: reject cache, carry pool, near-miss parking.'
};

function setVersion(v) {
  version = v;
  btnV11.classList.toggle('active', v === 'v11');
  btnV12.classList.toggle('active', v === 'v12');
  btnV13.classList.toggle('active', v === 'v13');
  btnV14.classList.toggle('active', v === 'v14');
  btnV15.classList.toggle('active', v === 'v15');
  btnV16.classList.toggle('active', v === 'v16');
  btnV17.classList.toggle('active', v === 'v17');
  verNote.innerHTML = NOTES[v] || NOTES.v17;
  if (!pill.classList.contains('run')) {
    pill.className = 'pill idle';
    pill.textContent = 'idle · ' + v;
  }
}
function append(text, cls) {
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = text + "\\n";
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.appendChild(span);
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}
function classify(line) {
  const l = line.toLowerCase();
  if (line.startsWith('$ ')) return 'cmd';
  if (l.includes('error') || l.includes('traceback') || l.includes('fail')) return 'err';
  if (l.includes('warn') || l.includes('skip')) return 'warn';
  if (line.includes('✅') || l.includes('passed') || l.includes(' ok ') || l.startsWith('  ok')) return 'ok';
  return '';
}
function setRunning(on) {
  btnDry.disabled = on; btnPub.disabled = on;
  btnV11.disabled = on; btnV12.disabled = on; btnV13.disabled = on;
  btnV14.disabled = on; btnV15.disabled = on; btnV16.disabled = on; btnV17.disabled = on;
  if (on) {
    pill.className = 'pill run'; pill.textContent = 'running ' + version + '…';
    t0 = Date.now();
    timer = setInterval(() => { clock.textContent = ((Date.now()-t0)/1000).toFixed(1) + 's'; }, 100);
  } else {
    clearInterval(timer); timer = null;
  }
}
function start(mode) {
  logEl.textContent = '';
  setRunning(true);
  let url = '/run?mode=' + encodeURIComponent(mode) + '&version=' + encodeURIComponent(version);
  es = new EventSource(url);
  es.addEventListener('line', e => append(JSON.parse(e.data), classify(JSON.parse(e.data))));
  es.addEventListener('error-msg', e => { append(JSON.parse(e.data), 'err'); finish(null); });
  es.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    append('', ''); append('— exited ' + d.rc + ' in ' + d.elapsed + 's —', d.rc === 0 ? 'ok' : 'err');
    finish(d.rc);
  });
  es.onerror = () => { if (es) { es.close(); es = null; } setRunning(false); };
}
function finish(rc) {
  if (es) { es.close(); es = null; }
  setRunning(false);
  if (rc === 0) { pill.className = 'pill ok'; pill.textContent = 'done ✓ · ' + version; }
  else if (rc === null) { pill.className = 'pill err'; pill.textContent = 'blocked'; }
  else { pill.className = 'pill err'; pill.textContent = 'exit ' + rc + ' · ' + version; }
}
function run(mode) { start(mode); }
function publish() {
  start('publish');
}
setVersion('v17');
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.replace("__HERE__", html.escape(str(HERE))))
        elif parsed.path == "/health":
            self._send(200, json.dumps({
                "ok": True,
                "active": _run_state["active"],
                "version": _run_state.get("version"),
                "runners": {k: str(v) for k, v in RUNNERS.items()},
                "default": DEFAULT_VERSION,
            }), "application/json")
        elif parsed.path == "/run":
            self._handle_run(parse_qs(parsed.query))
        else:
            self._send(404, "not found", "text/plain")

    def _sse_header(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_event(self, event, data):
        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _handle_run(self, qs):
        mode = (qs.get("mode") or ["dry"])[0]
        version = (qs.get("version") or [DEFAULT_VERSION])[0]
        q = queue.Queue()
        with _subs_lock:
            _subscribers.append(q)
        try:
            self._sse_header()
            if not _run_state["active"]:
                ok, msg = _try_start(mode, version)
                if not ok and not _run_state["active"]:
                    self._sse_event("error-msg", msg)
                    return
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse_event(event, data)
                if event == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)


def main():
    missing = [f"{k}: {p}" for k, p in RUNNERS.items() if not p.exists()]
    if missing:
        raise SystemExit("runner(s) not found: " + "; ".join(missing))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(
        f"n50 UI http://127.0.0.1:{PORT}  default={DEFAULT_VERSION}  "
        + ", ".join(f"{k}={p.name}" for k, p in RUNNERS.items()),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
