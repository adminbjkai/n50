# run_next_set_v6.py — build plan

## Goal
New runner combining best of v1–v5; do NOT modify any existing file. Coexists (import
run_next_set as v1; same tracker/CSV/Notion/series; _v6 cache names). Live: completedSets=233
-> v6 publishes Set 234.

## Keep from v4 (user likes these)
- Sorted category-count console line.
- Two-stage diversity giving broad spread + interesting/useful picks.
- Rich per-pick Notion line (category, docker badge, lang, desc, hook, stars, appeal, topics).
- Signed appeal breakdown (momentum/liveliness/traction/polish/selfhost/community/gem(neg)).

## Fix the v5 regression
- v5 diversifies on the LLM category (cat_for prefers enrich.category) + novelty-reorders the
  balanced seed -> collapses to few buckets ("only LLM, messes up diversity").
- v6: diversity ALWAYS on heuristic categorize(); LLM only annotates + soft tiebreak/demote.

## Keep from v5
- --why <url> inspector, decision-trace audit, content-hash cache invalidation, adaptive sweep
  (total_count early-exit + zero-yield demotion), stale-enrich (opt-in), richer --stats, --self-test.

## v6 NEW / improved
- SURFACE pluses/minuses: store full breakdown (incl docker/awesome/novelty/self_hostable adj)
  in audit per pick; show top ▲ positives + ▼ negatives on the Notion page per pick; --breakdown
  prints per-pick composition to console.
- Diversity = balanced two-phase on HEURISTIC cats: Phase1 seed top-1 per distinct category
  (guarantees coverage), Phase2 fill by score under --cat-cap with relaxation, min-score floor,
  fail-loud if <50.
- Carry the transient-5xx/502 search retry fix.
- Token off argv (curl --config) ; publish lock with stale-PID/TTL takeover ; --verify.

## Flags
--dry-run --sync-only --verify --stats --self-test --why URL --enrich --no-graphql --no-awesome
--breakdown --target-fresh(400) --max-pages(3) --cat-cap(5) --min-score(30)

## Status
- [x] research (3 agents) done
- [ ] write run_next_set_v6.py
- [ ] test: --self-test, pure fns, --verify, --dry-run (no writes), --why
