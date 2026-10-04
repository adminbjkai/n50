# n50

The control room behind **https://n50.bjk.ai**: it finds self-hosted open-source web apps on
GitHub and publishes them to Notion as a numbered series, "Set N — 50 More Self-Hosted
Open-Source Web Apps (2026)", under the 🤖 AI Hub page. Every published repo is recorded so
no repo is ever published twice.

## Layout

| Path | What it is |
|---|---|
| `server.py` | Stdlib web server: runs one engine at a time, streams its output, JSON API over the series data. |
| `web/` | The UI (`index.html`, `app.css`, `app.js`, self-hosted Archivo font). No build step. |
| `engines/` | The set builders. `v17.py` is the default; `v11`–`v16` stay runnable and top up a short set from v17's lane (`topup.py`). `run_next_set.py` is the shared Notion/tracker plumbing they import. |
| `deploy/` | systemd socket + service units (copied to `/etc/systemd/system/`). |
| `docs/` | `ENGINES.md` (which engine does what), per-engine strategy notes, history. |
| `notion-selfhosted-tracker.json` | Source of truth: completed sets, page links, every used repo URL. |
| `notion-selfhosted-master.csv` | One row per published repo (set, name, category, URL, description). |
| `_tmp/` | Audits (`setN_vNN_verified_audit.json` for published sets, `…_DRYRUN_audit.json` for previews, both tracked in git), engine caches and run logs under `_tmp/runs/` (local only). |

## Using the UI

- **Engine** — pick one on the left. Each shows how full its recent sets were. v17 is the default.
- **Dry run** (or press `D`) — builds the next set without writing anything. When it finishes
  the 50-port panel lights up and the **Picks** tab shows every pick, its category mix and
  whether the set is publishable.
- **Publish Set N** — starts a 3-second countdown (click again or press `Esc` to cancel), then
  runs the engine again and, only if it selects a full set, writes the Notion page, the
  tracker and the CSV.
- **Stop** — ends a dry run. A publish can't be stopped halfway.
- **Published** — every set with its fill, engine, date and Notion link; click a set to list its repos.
- **Runs** — the last 60 runs with their full output; open one to read it in the console.
- **Find a published repo** (`/`) — searches all published repos by name, description or category.
- Reloading the page mid-run picks the live output back up. "Notify me" sends a desktop
  notification when a run finishes in a background tab. `1`–`4` switch tabs.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/overview` | Series counts, next set, last dry-run preview, engines + fill stats, all sets, run state. |
| GET | `/api/set?n=494` | Picks for a set (master CSV for published sets, newest audit otherwise). |
| GET | `/api/search?q=…` | Published-repo search (40 hits max). |
| GET | `/api/runs`, `/api/run-log?id=…` | Run history and stored logs. |
| GET | `/api/stream` | Server-sent events: `hello` (state + current log), `line`, `state`, `done`. |
| POST | `/api/run` `{engine, mode: "dry"\|"publish"}` | Requires header `X-N50: 1` (blocks cross-site requests). |
| POST | `/api/stop` | Stops a dry run. Same header. |
| GET | `/health` | Liveness. |

## Running

```bash
python3 server.py                      # local, 127.0.0.1:8055 (N50_PORT to change)
python3 engines/v17.py --self-test     # every engine has --self-test and --dry-run
python3 engines/v17.py --dry-run
```

Auth: GitHub via `gh auth login` or `GH_TOKEN`; Notion via `NOTION_API_KEY` or
`~/.config/notion/api_key`.

## Production

- nginx (`/etc/nginx/sites-available/n50.bjk.ai`) terminates TLS, asks for the admin basic-auth
  login, and proxies to `127.0.0.1:8055` with buffering off for the live stream.
- systemd **socket activation**: `n50-runner.socket` listens on 8055 and starts
  `n50-runner.service` on the first request. The server exits after 15 idle minutes
  (`N50_IDLE_EXIT`), never during a run, so it uses no memory while nobody is looking.
- Deploy a change: `sudo cp deploy/n50-runner.* /etc/systemd/system/ && sudo systemctl daemon-reload`
  (only if the units changed), then `sudo systemctl restart n50-runner.service` for
  `server.py` changes. Edits to `web/` and `engines/` are live immediately: static files are
  read per request and every run starts a fresh engine process.
- Stopping or restarting the service ends a running dry run but waits (up to 15 minutes) for
  a publish to finish, so Notion and the tracker never get out of step.
