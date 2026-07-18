# v11-fresh-dir — Retrospective + v11 Plan

Fresh workspace for the next-generation runner of the "Set N — 50 More Self-Hosted Open-Source Web Apps (2026)" Notion pipeline, plus the full analysis of how v1→v10 got here.

## Files

| File | What it is |
|---|---|
| `run_next_set_v11.py` | **The v11 runner** — v9's selection core + candidate bench + bounded 60-call sweep + native urllib HTTP. Fully interchangeable with v1–v10 (same tracker/CSV/series). |
| `run_next_set.py` | Unmodified copy of the v1 plumbing that v11 imports (`import run_next_set as v1`). v11 auto-resolves the live tracker/CSV/`_tmp` one level up in `notion50new-v4/`. |
| `ANALYSIS.md` | Full retrospective of all 10 versions: the goal, what was done well/cleverly, what went wrong, a close bug-level review of v10, measured timing/token data from the real audits, and a scorecard. |
| `V11-STRATEGY.md` | The design for v11 — architecture, what it keeps/drops from each ancestor, numeric targets, and the build/validation plan. |
| `README.md` | This file. |

## Run

```bash
cd /Users/m17/2026/notion50/notion50new-v4/v11-fresh-dir
python3 run_next_set_v11.py --self-test   # pure-function checks (no network)
python3 run_next_set_v11.py --dry-run     # full pipeline, ZERO writes — always do this first
python3 run_next_set_v11.py --verify      # tracker invariant check
python3 run_next_set_v11.py               # publish the next set (only after dry-run + go-ahead)
```

All v9 flags work unchanged: `--stats`, `--why URL`, `--breakdown`, `--enrich`, `--no-graphql`, `--no-awesome`, `--no-fresh`, `--target-fresh`, `--max-pages`, `--cat-cap`, `--min-score`, `--fresh-quota`. Caches (incl. the new `bench_v11.json`) live in the shared `../_tmp/` and are safe to delete.

## Measured results (2026-07-10, real dry-runs against the live tracker, set 301)

| Metric | v9/v10 (36 real runs) | v11 cold | v11 warm (bench seeded) |
|---|---|---|---|
| Wall clock | **444s median**, 862s max | **118s** | **73s** |
| GitHub search calls | ~132 | 24 | 24 |
| Confirm phase | (in total) | 35.5s | **1.3s** (cached) |
| Categories in set | ~29 keys | 18 across 50 picks | 18 |
| Docker/compose confirmed | 36 | 60 of 130 confirmed | 60 |
| Fresh gems in set | 10 | 16 | 16 |
| Appeal min/mean/max | comparable | 36.6 / 64.2 / 94.4 | identical |

`--self-test`: 18/18 pass. Audit now records `benchSeeded`/`benchSaved`/`sweepSeconds`/`confirmSeconds` and is correctly labeled `"version": "v11"`.

## TL;DR of the analysis

- The pipeline **works** (~299 sets, ~14,874 tracked repos, publishing through today) and its safety/observability design (Notion-as-truth sync, atomic writes, dry-run, per-run audits) is genuinely strong.
- The **latest runner (v10) is slow because of call count, not code speed**: ~132 sequential GitHub search calls against a 30/min rate limit ⇒ 444s median, up to 14 min. v10's parallelism never touched the search loop, and it accidentally *deleted* v9's early-exit, so it always burns its full call budget.
- v10 also has real bugs: it still labels its audits **"v9"** (runs are indistinguishable in history), and its enrichment "bypass" writes fabricated hook text into the cache.
- The big development-cost mistake across versions was **full-file copying** (v10 = 1,782 lines, only ~345 actually changed) with 80-line historical docstrings regenerated every iteration — that's where the tokens went during the building phases, not at runtime.

## What makes v11 better (the headline claims)

1. **Time: ~90–150s per run instead of 7–14 min.** A persisted "candidate bench" reuses the ~2,750 scored-but-unused candidates each run currently throws away, so steady-state runs need ~10–25 search calls instead of ~132. Restored + strengthened early-exit; hard cap of 60 calls.
2. **API efficiency: ~8 repos scored per pick instead of ~56**, and confirms stay at 2–3 GraphQL batch calls. Kinder to rate limits ⇒ fewer 15–45s backoff sleeps ⇒ more predictable runs.
3. **Token efficiency: ≤ 5k Haiku tokens on `--enrich` runs, zero otherwise.** Only genuinely ambiguous finalists go to the LLM; Docker+awesome-verified repos bypass it (correctly labeled, no fabricated hooks). Development tokens drop too: ~1,100-line file, 15-line header, history lives here instead of in the docstring.
4. **Quality: same or better than v9** — keeps the family's best selection core (v9's gate/scoring/diversity/fresh-gem quota, v6/v7's heuristic-driven diversity) and adds a hard rule that every pick shows real self-hostability evidence (compose file, awesome-selfhosted listing, or explicit self-host topic). Bench entries are re-confirmed live before publish, so nothing stale ships.
5. **Trustworthy bookkeeping:** one `VERSION` constant drives every label (fixes the v9/v10 audit confusion), and audits gain per-phase timings so any future slowness is diagnosable from the JSON alone.
6. **Fully interchangeable**, like every version before it: same tracker, same master CSV, same series numbering, imports v1's plumbing, never modifies v1–v10, always `--dry-run` first.

## Status

**Built and validated (2026-07-10):** `--self-test` all pass; cold + warm `--dry-run` completed against the live tracker (set 301 would be next) with zero writes. **6.1× faster than the v9/v10 median** (73s warm vs 444s). Not yet used for a real publish — first real publish requires explicit go-ahead per the standing operating rules.
