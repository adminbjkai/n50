# PROGRESS — n50.bjk.ai overhaul (Oct 2026)

**Goal:** a tidier, lighter, better-looking n50 control room that keeps every engine
runnable, makes v17 actually fill 50/50 again, and is live on n50.bjk.ai + GitHub.
**Started:** 2026-10-03 · **Sandbox:** dry-runs only; no Notion publish without the user.

## Baseline (measured 2026-10-03)
- Backend down: `n50-runner.service` stopped + disabled since 2026-10-02 06:49 (no note why).
  nginx vhost + basic auth ("Admin") intact.
- 7 engines (v11–v17, ~25k lines) in `v11-fresh-dir/`, plus a stale duplicate
  `run_next_set_v11.py`, 6 unused symlinks, `__pycache__`, a `.bak`; `_tmp/` 51 MB incl.
  caches for engines that no longer exist (v2–v10), Mac-path scripts, old logs.
- Usage since v17 shipped (09-24): v11 ×4, v15 ×5, v16 ×5, v17 ×4 — engines are swapped
  by hand to chase yield. GitHub `origin/main` is 1 commit behind (v17 commit unpushed).
- v17 dry-run for Set 494: 151 pass the gate, 132 are AI → AI cap 4 → **23/50**, refuses.
- UI: one 400-line file, version buttons, raw log; `/run` is a GET (CSRF-able publish);
  reload loses the log; no history, no preview of picks.

## Stages
| # | Deliverable | Acceptance | Status | Evidence |
|---|---|---|---|---|
| 1 | Restructure + clean | engines in `engines/`, server+web at root, docs in `docs/`, dead files trashed, caches untracked | ✅ | all 7 engine `--self-test` ALL PASSED from `engines/`; `_tmp` 51→34 MB |
| 2 | Server rewrite | JSON API, POST+header run/stop, replayable log, run history, socket-activated idle exit | ✅ | verifier: API vs tracker/CSV exact, traversal 400/404, 403 w/o header, 409 on double start, stop → rc −15; idle exit observed (40 s test); stop mid-dry-run 0.56 s |
| 3 | New UI | distinctive design, engine stats, proof sheet, back issues, log tools | ✅ | Playwright flow 1440/390 px, no console errors; reload mid-run replays log; picks auto-open on finish |
| 4 | v17 yield | dry-run fills 50/50 without junk; self-test green | ✅ | dry runs: 23/50 → 50/50 (95 s, 5 AI) → 50/50 after gate fixes (70 s, 17 AI, 64 MB RSS); self-test 32 checks |
| 5 | Deploy | socket+service live, nginx path works, screenshots | ✅ | socket enabled/active; units == deploy/; KillMode=mixed, 5 min stop timeout; public URL 401 (basic auth) as before |
| 6 | Docs + GitHub | README/docs current, pushed | ✅ | README, docs/ENGINES.md, V17 update; pushed to adminbjkai/n50 main |
| 7 | Independent verify | fresh-context verifier PASS | ✅ | round 1: 7/8 + 7 bugs → fixed; round 2: server/gate/shutdown PASS, 1 doc sentence + a11y gaps + stop-timeout/run-record races → fixed and re-tested (stop 0.57 s, record saved; Home/End/Space work) |

## Decisions
- Keep all engines (v11–v17) runnable — they are still used. Group them; feature v17.
- No Notion publish during this work (outward-facing); dry-runs only.
- Publish stays one click (README: "Publish starts directly from the button"), but gets a
  3-second cancelable countdown instead of a dialog.
- v17 AI policy: pass 1 keeps AI ≤4; a short set is filled with non-AI past the category cap
  first, then AI apps whose description shows a web UI, never more than 20/50. AI without a
  web-app description is never picked. (2026-10-03)
- v17 query-memory bug: overlap between slices counted as "nothing new" and marked slices
  dead for 14 days. Fixed; dead counters reset; memory pruned after 28 days.
- Dry runs always write their audit (with `publishable`), so a short set can be previewed.
- Verifier round 1 found: every engine refuses short standard publishes (guards added
  Sep 28–30), so "ship short" docs/UI were wrong; v14's guard compared to 50 (target 200) —
  fixed to DEFAULT_TARGET. Restarts would SIGTERM a publish → KillMode=mixed + server waits.
  Narrowed v17 gate regexes that hit legit apps; AI_APP no longer accepts bare platform/browser.
- Round 2: TimeoutStopSec 900 (legacy publishes took up to 511 s); shutdown refuses new runs
  and waits until the run record is written. Known leftover: loponai/oneshotmatrix-style
  "one-shot" bundles can pass the narrowed packaging rule (accepted; low impact).

## Follow-up (2026-10-03 evening): short sets on v11–v16
User's v16 publish selected 45/50 and refused. Cause: REST search mined out (48 confirmed for
50 slots). Added `engines/topup.py`: before each engine's shortfall check, missing slots are
filled from v17's GraphQL lane (v17 gate + dedupe, owner-unique, engine's AI ceiling and
`--cat-cap` honoured first, tier A/B, marked in audit `topUp` and on the Notion line).
Evidence (dry runs via the live server): v16 45→50 (91.6 s top-up), v15 36→50 (92.3 s),
v16 49→50 (64.4 s); v13 filled 50 itself (top-up skipped cleanly). Audit check on the v16
run: 50 unique, 0 already published, 0 duplicate owners, tier A+B 35. Also fixed the
A∪B warning to count after the top-up. Self-tests: all 7 engines + `topup.py` (7 checks).

## Real publishes, one per engine (2026-10-03, user-requested)
Order v17 → v16 → v15 → v13 → v12 → v11 → v14 (200, last). After each: check_set (tracker,
CSV, Notion, no reuse, no duplicate owners) + reading every line.
- Set 494 v17: 50/50 clean structurally; review found 2 agent-infra picks + bad categories →
  gate/category rules; repaired (3 replaced incl. a backend component).
- Set 495 v16: 50/50 but ~28 junk (templates, compose bundles, 0★, AI proxy, clients, dup
  owner) → built quality.py (v17 gate for every engine, 3★ floor) + repair_set.py; repaired
  27, then LibreChat (renamed copy of a published repo) + 1 more.
- Lane exhausted at ≥20★ after 494 → STAR_LADDER (20/10 → 10/5 → 5/3) in v17 + lane.
- Set 496 v15: 9 own + 41 lane; review found statainer (renamed copy) + 5 junk → rename
  dedupe in quality.screen; repaired 6; 2 categories refreshed.
- Set 497 v13: 9 own + 41 lane; review → companion/*arr/bot/game-server/reseller/bulk-mail
  rules, categories; repaired twice (11 replaced). Lane rename re-check added after LANBooru.
- Set 498 v16 (started by the user from the UI at 22:05): 2 own + 48 lane (≥5★ rung, 503
  passing); review → RustDesk/Asterisk/Invoice Ninja add-ons, language/SDK, non-English;
  repaired (9 replaced). Found repair_set race with concurrent publishes → re-read tracker
  before write + refuse while the server publishes.
- Category ordering regression caught before writing (killed the chain) → earliest-match
  categorization; star-ladder memory. All of 494–498 re-checked under final rules.
- Lesson: `pkill -f <pattern>` kills the calling shell when the pattern is in its command
  line — use `ps -eo pid,args | grep "^..."` and kill by pid.
