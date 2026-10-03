# v11 Strategy — `run_next_set_v11.py`

Design for the next runner. Goal: **same guarantees, same interchangeability (same tracker/CSV/series, imports v1 plumbing, never touches older versions), but ~90–150s per run instead of 7–14 min, ~25–40 GitHub calls instead of ~132, fewer LLM tokens, and stricter quality** — with instrumentation that proves each claim from the audit.

## Design principles (locked)

1. **Fewer calls, not parallel calls.** GitHub search is server-limited to ~30/min; parallelism can't beat that. The wall clock is a linear function of search-call count, so the whole speed strategy is call-budget reduction.
2. **Deterministic skeleton, LLM as garnish** (the v6/v7 lesson). Heuristic category always drives diversity; LLM output only annotates and tiebreaks, and synthetic/bypass entries are labeled as such.
3. **One version, honest labels.** A single `VERSION = "v11"` constant feeds *every* label — audit filename, audit `version` field, cache names, log prefixes — via f-strings only. No hardcoded version strings anywhere else (the v10 bug class becomes impossible).
4. **Regression scorecard before/after.** Every change is validated with `--dry-run` and compared on: timing, search calls, category spread, score min/mean/max, Docker %, fresh-gem count.

## The core new idea: the candidate bench

v9/v10 score ~2,800 fresh candidates per run and keep 50 — then discard ~2,750 vetted candidates. v11 banks them.

- After selection, persist the top ~400 scored-but-unselected candidates to `_tmp/bench_v11.json` (score, category, `_fresh` flag, found-by provenance, content signature; 14-day TTL).
- Next run: load the bench, drop anything now in `usedRepoUrls`, and **start the sweep with hundreds of pre-vetted candidates already in hand**.
- The sweep becomes *top-up only*: run queries (highest historical yield first, reusing v5's yield-tracking idea) until `fresh_scored ≥ 6 × 50`, then **stop immediately** (restore + strengthen the early-exit v10 deleted).
- Freshness stays honest: bench entries are re-confirmed by the existing GraphQL batch step (2 calls for ~70 finalists) before publish, so stale stars/archived repos can't slip through; and a minimum of ~8 search calls always runs so each set still discovers brand-new repos and the fresh-gem quota (10 slots) still fills from `created`-sorted sweeps.

**Expected effect:** first run ~35–45 calls (~150s); steady-state runs ~10–25 calls (**~60–120s**). Worst case (bench empty/exhausted) caps at 60 calls (~200s) — still 2× faster than today.

## Full pipeline (deltas from v9/v10 marked ★)

1. Reconcile with Notion (v1 plumbing, v4's O(n) fix) — unchanged.
2. ★ Load bench + dedup against tracker.
3. ★ Budgeted adaptive sweep: composite + quality + fresh-gem queries, ordered by persisted per-query yield, hard early-exit at `fresh_scored ≥ 300`, absolute cap 60 calls. Native urllib (keep v10's win), sequential.
4. Gate + score + heuristic-category diversity + fresh-gem quota — carry v9's logic verbatim (it's the family's best).
5. GraphQL batch-confirm + Docker detect, **sequential** chunks (2–3 chunks; parallelism saves nothing and risks secondary limits ★revert), REST fallback parallel is fine.
6. ★ Optional `--enrich`: keep v10's bypass but mark it `{"source": "bypass"}` and give it no hook text; only truly ambiguous repos go to the LLM (typically 15–25 of 50 → 2 Haiku calls, ~3–5k tokens).
7. Publish + tracker + CSV + audit — unchanged, plus ★ per-phase timings in the audit (`{"sweep": s, "confirm": s, "notion": s, ...}`) so future slowness is diagnosable at a glance.
8. ★ Quality hardening: a pick must have ≥1 of {Docker/compose detected, awesome-selfhosted listed, explicit self-host topic}; pure-library/mobile/API-client rejection kept from v4; min-score floor kept.

## What v11 explicitly keeps from each ancestor

v1 plumbing/sync/atomicity · v2 search-payload scoring + cat-cap · v3 GraphQL+Docker+awesome+min-score+lock+verify · v4 tight gate + O(n) reconcile · v5 yield tracking + content-hash cache + `--why` · v6/v7 heuristic-category diversity + breakdowns · v8/v9 fresh-gem quota, multi-sort, composite queries · v10 native urllib + enrich bypass (fixed).

## What v11 explicitly drops/reverts

- v10's unused search "parallelism" scaffolding and parallel GraphQL.
- The always-burn-full-budget sweep (restore early-exit).
- Hardcoded version labels; fabricated enrich hooks.
- The 80-line historical docstring — replaced by a 15-line header pointing at this file and `CHANGELOG` section in the old v11-fresh-dir README (now docs/history/README-v11-v17.md).

## Targets (verify with --dry-run before first publish)

| Metric | v9/v10 today | v11 target |
|---|---|---|
| Wall clock (steady state) | 444s median, 862s max | **≤ 120s** (≤ 200s worst) |
| GitHub search calls | ~132 | **≤ 25 steady / ≤ 60 cap** |
| Repos scored per pick | ~56:1 | ~8:1 (bench amortized) |
| LLM tokens per --enrich run | ~6–10k | **≤ 5k** (bypass + ambiguous-only) |
| Category "Other" share | low (v9 gate) | same or lower |
| Fresh gems per set | 10 quota | 10 quota (unchanged) |
| File size | 1,782 lines | ~1,100 lines |

## Build & validation plan

1. Copy v9's selection core (not v10's) as the base; port v10's urllib clients cleanly; add bench + budgeter + per-phase timing (~1 session).
2. `python3 run_next_set_v11.py --self-test` — pure functions.
3. Two consecutive `--dry-run`s (cold, then bench-warm) — record the scorecard, compare against a v9 dry-run side by side.
4. `--verify` against the live tracker.
5. First real publish only after explicit user go-ahead (per next-agent-prompt.md rules); report set number + URL + real numbers.
6. Update README + the `next-set` skill to point at v11.

## Also worth doing (separate from v11, pending user approval)

- One-time data cleanup the TOOL-REVIEW asked for in June and is still open: finish the CSV description backfill, document the 27 legacy duplicate repos + 30 off-count sets as legacy in the README.
- Extract `notion50_core.py` so v12+ are ~200-line strategy files instead of full copies (only if a v12 ever happens; v11 itself stays a self-contained new file per the coexistence rule).
