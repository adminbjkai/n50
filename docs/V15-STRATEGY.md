# v15 Strategy — Safe 50/set (governed + never-crash)

## Why v15

On n50.bjk.ai, selecting **v13 diversity → Publish** crashed:

```
[v13] confirmed 12 proof-passing repos …
[v13] second discovery wave (have 15, need 50) …
[search] rate-limited, sleeping 15s …
[search] rate-limited, sleeping 30s …
[search] rate-limited, sleeping 45s …
RuntimeError: Only 20 proof-passing repos scored >= --min-score 20.0
(confirmed pool 20, A∪B available=12). Need 50. — exited 1 in 328.1s
```

Two failures compounded:

1. **Rate-limit spiral.** GitHub's Search API allows ~**30 requests/min**. v13/v14
   fire 36–48 search calls *per wave* with no pacing, and only react to a 403/429
   with a blind `15 → 30 → 45s` sleep. A single set burned 328s, most of it asleep,
   and truncated sweeps left the candidate pool thin.
2. **Hard crash on a thin pool.** At 50/set `LENIENT_MODE` was off, so when only 20
   repos confirmed, the run fell through to `raise RuntimeError` and exited 1 — a
   Python traceback in the web log, nothing shipped.

The corpus is genuinely mined out after 390+ sets (see V14-STRATEGY.md), so "just try
harder" isn't enough — the runner has to **spend its search budget efficiently** and
**degrade gracefully** instead of aborting.

## What v15 is

v15 is v14's whole engine (v13 product-diversity + v14 recycling) **retargeted to
50/set** and made efficient + safe. It replaces v13 as the recommended 50/set runner.

### 1. Rate-limit governor (the safety core)

A proactive pacer wraps every search call (`_pace_search` / `_respect_search_headers`
/ `_retry_after_seconds`):

- **Rolling ≤26 calls/min** (margin under GitHub's 30) with a **~2.2s anti-burst gap**,
  so the secondary limit is never tripped.
- Reads **`X-RateLimit-Remaining` / `-Reset`** off every response and waits for the
  *real* reset only when the quota is actually nearly out.
- On a 403/429, honours **`Retry-After`** (or the exact reset) instead of a blind
  escalating sleep.

Only the *search* endpoint is governed — core/GraphQL are 5000/hr and never binding.

**Measured (dry-run, set 395):** the first wave's **68 search calls completed in
158.6s with 0 reactive rate-limit sleeps** — just 5 proactive pauses of ≤1.2s. v13's
log on the same corpus showed `sleeping 15s/30s/45s` stacks.

### 2. Never crashes

- v15 **always** runs `LENIENT_MODE` (broader gates + graceful ship). The job is to
  ship a solid, honestly-reported set from a thin corpus, not to abort.
- The second-chance min-score lowering is **ungated** (always tries to reach N first).
- Dry-run **always** completes and writes its audit.
- The old `raise RuntimeError` is gone. v15 ships the best available set (honestly
  reported: "shipping N of requested 50 — pool thin after X used URLs"). Only a
  *catastrophically* thin pool (`< max(15, 0.3·N)`, i.e. an outage / auth failure)
  halts — and then with a **clean one-line message**, never a traceback in the web log.

### 3. Warm cache seed

v15's reject / bench / carry caches **union v14's read-only** on load, so run 1 is
already efficient. **Measured:** run 1 loaded "**527 known-bad repos will be
skipped**" from v14's reject cache — no cold start.

### 4. Everything from v13 / v14, unchanged

Family floors (files/knowledge/media/PDF/bookmarks/monitoring/sports/…), AI hard-cap
~3/50, theme discovery bank, proof tiers A/B/C, reject cache, carry pool, near-miss
parking, merged bench, family-gap top-up wave, graduated overflow ceiling.

## Behaviour / safety

| | v13 (50/set) | v15 (50/set) |
|---|---|---|
| Search pacing | none — reactive 15/30/45s sleeps | **governed ≤26/min + header-aware** |
| Thin pool | `raise RuntimeError` → exit 1 | **graceful ship**, honest count |
| Web log on failure | Python traceback | clean one-line message (or ships) |
| Run 1 efficiency | cold (no reject cache) | **warm** (unions v14 caches) |
| Publish 50 | no force-publish needed | no force-publish needed |
| Tracker / CSV | shared, appended | **same** shared, appended |

## Flags

```bash
python3 v15.py --self-test     # fast, offline — includes governor + graceful checks
python3 v15.py --dry-run       # next Set N of 50; never publishes; always completes
python3 v15.py                 # publish Set N (50). Gated in the UI by typing PUBLISH
```

Non-standard sizes (`--count N`, `--special-title`) still require `--force-publish`.

## Files

| Path | Purpose |
|---|---|
| `v15.py` | The runner |
| `run_next_set_v15.py` | Symlink → `v15.py` |
| `_tmp/reject_cache_v15.json` | Known-bad repos (TTL'd); unions v14 on load |
| `_tmp/carry_v15.json` | Confirmed-but-unshipped carryover |
| `_tmp/nearmiss_v15.json` | Scored-but-unconfirmed parking |
| `_tmp/bench_v15.json` | Merged proof-eligible bench |
| `_tmp/set{N}_v15_*.json` | Per-run audits |

## Deploy note

The site is served by `server.py` under `systemd` unit `n50-runner.service`
(`/etc/systemd/system/`). `server.py` now lists v15 in `RUNNERS`, exposes the
`v15 · safe 50 ★` button, and sets `DEFAULT_VERSION = "v15"`. Restart to deploy:
`sudo systemctl restart n50-runner.service`.

> Latent bug noted (not v15's, not fixed here): `server.py`'s SIGTERM handler calls
> `server.shutdown()` from the serving thread, which deadlocks — so a manual
> `kill <pid>` hangs the old process. systemd's stop still works (it SIGKILLs after
> the stop timeout). A one-line fix would move shutdown to a helper thread.
