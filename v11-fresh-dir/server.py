#!/usr/bin/env python3
"""
server.py — tiny stdlib web UI for running the v11 Notion runner.

Serves one page at n50.bjk.ai with two buttons:
  • Dry run   -> python3 run_next_set_v11.py --dry-run   (zero writes, safe)
  • Publish   -> python3 run_next_set_v11.py             (writes to Notion; gated)

Output streams live to the page via Server-Sent Events. Only one run may be
in flight at a time. No external deps — Python 3 stdlib only.
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
SCRIPT = HERE / "run_next_set_v11.py"
PORT = int(os.environ.get("N50_PORT", "8055"))
PUBLISH_CONFIRM = "PUBLISH"  # UI must send this to arm a real publish

# Modes the button can request -> the exact argv appended to the runner.
MODES = {
    "dry":     ["--dry-run"],
    "publish": [],
}

# ---- single-run guard + live subscriber fan-out --------------------------
_lock = threading.Lock()          # held for the duration of a run
_run_state = {"active": False, "mode": None, "started": 0.0}
_subs_lock = threading.Lock()
_subscribers = []                 # list[queue.Queue] of currently-watching clients


def _broadcast(event, data):
    payload = (event, data)
    with _subs_lock:
        for q in list(_subscribers):
            q.put(payload)


def _run_command(mode):
    """Run the runner for `mode`, broadcasting each output line. Runs in a thread."""
    argv = ["python3", "-u", str(SCRIPT)] + MODES[mode]
    _run_state.update(active=True, mode=mode, started=time.time())
    _broadcast("status", {"active": True, "mode": mode})
    _broadcast("line", f"$ {' '.join(shlex.quote(a) for a in argv)}")
    _broadcast("line", f"  (cwd: {HERE})")
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
    except Exception as exc:  # noqa: BLE001 - surface any launch failure to the UI
        _broadcast("line", f"[server] failed to run: {exc!r}")
        rc = -1
    finally:
        elapsed = time.time() - _run_state["started"]
        _run_state.update(active=False, mode=None)
        _broadcast("done", {"rc": rc, "elapsed": round(elapsed, 1)})
        _broadcast("status", {"active": False, "mode": None})
        _lock.release()


def _try_start(mode):
    """Acquire the run lock and spawn the worker. Returns (ok, message)."""
    if mode not in MODES:
        return False, f"unknown mode {mode!r}"
    if not _lock.acquire(blocking=False):
        return False, "A run is already in progress."
    threading.Thread(target=_run_command, args=(mode,), daemon=True).start()
    return True, "started"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>n50 · v11 runner</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #0d1117; color: #c9d1d9; padding: 24px; max-width: 960px; margin-inline: auto;
  }
  h1 { font-size: 18px; margin: 0 0 4px; color: #e6edf3; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .bar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
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
  #log {
    background: #010409; border: 1px solid #30363d; border-radius: 8px; padding: 14px;
    height: 62vh; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    font-size: 13px; color: #b9c1cb;
  }
  #log .cmd { color: #58a6ff; }
  #log .warn { color: #d29922; }
  #log .err  { color: #f85149; }
  #log .ok   { color: #3fb950; }
</style>
</head>
<body>
  <h1>v11 runner &middot; n50</h1>
  <div class="sub">Runs <code>run_next_set_v11.py</code> in <code>__HERE__</code> and streams output live.</div>
  <div class="bar">
    <button id="btn-dry" class="dry" onclick="run('dry')">▶ Dry run (safe, no writes)</button>
    <button id="btn-pub" class="pub" onclick="publish()">⚠ Publish to Notion</button>
    <span id="pill" class="pill idle">idle</span>
    <span id="clock"></span>
  </div>
  <div id="log">Ready. Click a button to start.\n</div>

<script>
const logEl = document.getElementById('log');
const pill  = document.getElementById('pill');
const clock = document.getElementById('clock');
const btnDry = document.getElementById('btn-dry');
const btnPub = document.getElementById('btn-pub');
let es = null, t0 = 0, timer = null;

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
  if (on) {
    pill.className = 'pill run'; pill.textContent = 'running…';
    t0 = Date.now();
    timer = setInterval(() => { clock.textContent = ((Date.now()-t0)/1000).toFixed(1) + 's'; }, 100);
  } else {
    clearInterval(timer); timer = null;
  }
}
function start(mode, confirm) {
  logEl.textContent = '';
  setRunning(true);
  let url = '/run?mode=' + encodeURIComponent(mode);
  if (confirm) url += '&confirm=' + encodeURIComponent(confirm);
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
  if (rc === 0) { pill.className = 'pill ok'; pill.textContent = 'done ✓'; }
  else if (rc === null) { pill.className = 'pill err'; pill.textContent = 'blocked'; }
  else { pill.className = 'pill err'; pill.textContent = 'exit ' + rc; }
}
function run(mode) { start(mode, null); }
function publish() {
  const typed = prompt('This PUBLISHES the next set to Notion (real writes, not reversible).\\n\\nType PUBLISH to proceed:');
  if (typed !== 'PUBLISH') { append('Publish cancelled.', 'warn'); return; }
  start('publish', 'PUBLISH');
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet default logging
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
            self._send(200, json.dumps({"ok": True, "active": _run_state["active"]}),
                       "application/json")
        elif parsed.path == "/run":
            self._handle_run(parse_qs(parsed.query))
        else:
            self._send(404, "not found", "text/plain")

    # ---- SSE run endpoint -------------------------------------------------
    def _sse_header(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # disable nginx proxy buffering
        self.end_headers()

    def _sse_event(self, event, data):
        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _handle_run(self, qs):
        mode = (qs.get("mode") or ["dry"])[0]
        confirm = (qs.get("confirm") or [""])[0]

        # Subscribe BEFORE starting so we never miss the first lines.
        q = queue.Queue()
        with _subs_lock:
            _subscribers.append(q)
        try:
            self._sse_header()
            if mode == "publish" and confirm != PUBLISH_CONFIRM:
                self._sse_event("error-msg", "Refused: publish requires confirmation.")
                return

            already = _run_state["active"]
            if not already:
                ok, msg = _try_start(mode)
                if not ok:
                    # Lost the race, or bad mode. If a run is now active, just attach.
                    if not _run_state["active"]:
                        self._sse_event("error-msg", msg)
                        return

            # Relay broadcasts until this run finishes.
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # comment ping
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
    if not SCRIPT.exists():
        raise SystemExit(f"runner not found: {SCRIPT}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    print(f"n50 runner UI on http://127.0.0.1:{PORT}  (script: {SCRIPT})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
