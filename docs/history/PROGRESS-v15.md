# PROGRESS — v15 runner (safe 50/set: rate-limit governor + never-crash)

## Goal
Fix the `exit 1` the user hit on n50.bjk.ai when running **v13 diversity → Publish**:

```
RuntimeError: Only 20 proof-passing repos scored >= --min-score 20.0
(confirmed pool 20, A∪B available=12). Need 50. ... 328.1s
```

Deliver `v15.py`: a **50-per-set product-diversity runner** (v13 semantics) built on
v14's engine, but **way more efficient and safe** — it must never burn the session on
GitHub rate-limit sleeps and must never hard-crash on a thin pool. Wire it into
n50.bjk.ai conveniently.

## Root-cause analysis (evidence from this session)
1. **Rate-limit spiral.** GitHub *search* API = **30 calls/min** (confirmed live:
   `search: 30/30`). v13/v14 fire 36–48 search calls per wave with **no pacing** and
   only react to 403/429 with blind `15→30→45s` sleeps (v13.py:1008-1016). The failing
   log spent 328s largely in `[search] rate-limited, sleeping …`. Truncated sweeps →
   thin candidate pool.
2. **Hard crash on thin pool.** At 50/set `LENIENT_MODE` is OFF, so the graceful
   branch needs `>= max(30, 0.6*N)=30`; with 20 confirmed it fell through to
   `raise RuntimeError` (v13.py:2785 / v14.py:3065) → exit 1.
3. **v13 has no recycling.** No reject cache / carry pool / near-miss parking, so every
   run re-fetches known-bad repos and discards confirmed-but-unshipped ones. v14 added
   these but only ships them at 200/set.

## Strategy — v15 = v14 engine, retargeted to 50, made safe
Base on **v14.py** (superset of v13 diversity + v14 recycling). Surgical deltas:

- **D1 identity/target:** `VERSION=v15`, `DEFAULT_TARGET=50`,
  `V15_TITLE_TEMPLATE="Set {n} — 50 More Self-Hosted Open-Source Web Apps (2026)"`,
  caches → `*_v15.json`.
- **D2 rate-limit governor (the safety core):** proactive pacer around every search
  call — rolling ≤26/min budget + ~2.2s anti-burst gap; read `X-RateLimit-Remaining`
  / `-Reset` / `Retry-After` headers and wait to the *real* reset instead of blind
  escalation. Turns multi-minute stalls into small proactive pauses; no secondary-limit
  spiral.
- **D3 never-crash:** v15 runs LENIENT; second-chance min-score lowering is ungated;
  dry-run **always** completes with an honest audit; the final `raise RuntimeError`
  becomes a graceful ship (or, only if catastrophically thin on a real publish, a clean
  one-line message — never a traceback).
- **D4 warm seed:** v15 unions v14's reject/bench/carry caches read-only on load, so
  run 1 is already efficient (v14 measured 618 known-bad skips).
- **D5 self-test:** update version/target/title checks; add governor + graceful checks.
- **D6 server:** add `v15` runner + button; make it the recommended default in the UI.
- **D7 symlink + docs:** `run_next_set_v15.py`, `V15-STRATEGY.md`, README, this file.

## Stages
- S1 — Review project + prev dir, root-cause the exit 1 … ✅ (above)
- S2 — Build v15.py (D1–D4) … ✅ (governor, never-crash, warm seed, target 50)
- S3 — self-test green (D5) … ✅ `python3 v15.py --self-test` → ALL PASSED (0.15s;
  incl. 4 governor + graceful checks)
- S4 — dry-run: no rate-limit spiral, no crash … ✅ **PROVEN.** Full 494s run:
  **0** `rate-limited, waiting` lines and **0** tracebacks across ~200 governed
  search calls. Warm seed loaded **527** known-bad skips from v14. Corpus is
  genuinely mined out (20,496 used URLs) → only **18 proof-passing** confirmed
  (v13 crashed at 20 on this same corpus). v15 ran recovery + wave2 + family-gap
  top-up, then **shipped gracefully**: `[dry-run] would publish: Set 395 — 50 More
  … (18 repos)` + valid audit `set395_v15_DRYRUN_audit.json` (selectionHeadroom
  1.0, familyFloorShortfall populated, rejectCacheAfter 557). No `RuntimeError`,
  no exit 1 — the exact failure the user hit is gone.

## S7 — Published Set 395 ✅ (user-authorized, option 1)
`python3 v15.py` exit 0, **0 tracebacks / 0 rate-limit spirals**. Shipped **18 repos**
gracefully (WARN, not crash). `completedSets` 394 → **395**; tracker+CSV appended
(usedRepoUrls 20496 → 20514). Page:
https://www.notion.so/Set-395-50-More-Self-Hosted-Open-Source-Web-Apps-2026-3b14118619e5818ea8dcd79ccf6d319b

## S8 — Speed/efficiency tuning (user-requested) ✅
User asked to widen queries + lower threshold so runs are faster and cheaper. Changes
to v15.py (self-test still ALL PASSED):
- **Skip the waste waves:** 2nd discovery wave + family-gap top-up now fire only to
  rescue a catastrophically thin wave 1 (`WAVE2_FLOOR/TOPUP_FLOOR = 12`), instead of
  whenever confirmed < 50. On the mined-out corpus those waves added ~2 repos for
  ~half the runtime — now skipped.
- **Lower threshold:** `DEFAULT_MIN_SCORE` 20→12, lenient cap 18→10, ship floor 15→10
  (a genuinely thin corpus of 13–18 now ships; only true outages <10 halt).
- **Trim wave-1 budget:** composite 18→16, quality 40→34, theme 28→24, fresh 10→6.
- **Widen queries:** new `v15_fresh_wedges` theme (non-English self-host terms, FOSS
  framing, newer topic tags, homelab siblings) → 23 themes; higher yield per call.

**Measured (dry-run set 396, rebalanced):** **137.9s** end-to-end vs **494s** untuned
(**3.6× faster**); wave 2 + top-up **skipped**; 0 rate-limit spirals; shipped **15**
repos gracefully (`would publish: Set 396 … (15 repos)` + valid audit). First over-trim
(budgets too low) confirmed 13 < floor 15 and correctly refused to publish — proving the
graceful floor still works; rebalanced to fix. Server picks up v15.py edits
automatically (spawns `python3 v15.py` fresh per run — no restart needed).

Tradeoff, as requested: ~15/run fast, vs ~18/run slow. Speed >> count per user.

## Honest caveat (report to user)
v15 fixes the **crash and the inefficiency**, but it cannot manufacture repos that
don't exist. After 394 sets the corpus is mined out at the strict-proof 50-diversity
bar, so this run shipped **18, not 50**. That is corpus exhaustion (documented in
V14-STRATEGY.md), not a v15 defect — v13 got 20 here and then *crashed*; v15 got 18
and *shipped honestly*. Getting closer to 50 needs a user decision: widen discovery
surface (new query wedges / awesome forks / sibling lists) or relax the proof bar for
a lower-quality tail. Neither was done — out of scope without the user's call.
- S5 — server UI + symlink … ✅ deployed: `/health` → `default:v15`, 5 runners;
  page serves `v15 · safe 50 ★` button (active default). Retired a stale 6-day
  manual server.py that was holding :8055 and crash-looping the systemd unit
  (`Address already in use`); `n50-runner.service` now `active` with new code.
  Symlink `run_next_set_v15.py → v15.py`.
- S6 — docs … ✅ `V15-STRATEGY.md` (new), `README.md`, this file.

## Deploy incident (fixed)
The live site was served by a **stale manual `python3 -u server.py` (pid 1707982,
6 days old)**, not systemd. Its SIGTERM handler deadlocks (`server.shutdown()` on the
serving thread) so `kill` hung; SIGKILL + `systemctl restart` deployed the updated
server cleanly. Noted the latent shutdown bug in V15-STRATEGY.md (not fixed — out of
scope).

## Decisions
1. Base v15 on v14 (not v13) — v14 already has the recycling + waves v13 lacks.
2. v15 owns its title template ("50 More"); tracker `titleTemplate` untouched.
3. Governor targets the *search* endpoint only (core/graphql are 5000/hr, not binding).
4. Never emit a Python traceback to the UI; graceful ship on dry-run always.
