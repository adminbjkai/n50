# run_next_set_v3.py — build progress

## Goal
Create run_next_set_v3.py: the best-yet "next set of 50 self-hosted apps" runner.
Coexists with v1 (run_next_set.py) and v2 (run_next_set_v2.py). Same tracker, same
series, same Notion AI Hub page, same master CSV. Must be runnable standalone; v1+v2
must stay fully runnable & untouched.

## State of the world (verified)
- 175 sets published, 8,671 unique repos. Next = set 176.
- v2 already added: appeal scoring, category diversity (soft cap), efficient search
  (reuse search payload), disk cache for /repos confirms, expanded categories
  (19 cats, "Web App/Other" ~10% vs v1 78%), relative date window, per-set rotation.
- gh CLI authed (adminbjkai), curl present, Notion token at ~/.config/notion/api_key.
- OpenRouter wiring exists in generate_csv_nemotron.sh (free Nemotron model).

## Phases
- [ ] Phase 0: plan + resource selection (DONE - in chat)
- [ ] Phase 1: parallel research (4 lanes)
- [ ] Phase 2: strategy synthesis + improvement guideline
- [ ] Phase 3: build run_next_set_v3.py
- [ ] Phase 4: verify (dry-run + fresh-context verifier), confirm no writes

## Decisions / constraints
- v3 must stay runnable with stdlib-only by default; any LLM enrichment OPTIONAL
  (degrades gracefully if no key).
- Do NOT modify v1 or v2.
- No gold-plating: only add improvements with real value.

## Findings (research complete — 4 lanes + claude-api skill + live tests)
- Live state now 182 sets / 9,021 repos (other machines published; v3 reconciles).
- Data integrity (measured): 27 cross-set dup repos, 30 sets != 50 (legacy), usedRepoUrls==union TRUE. master.csv 75% "Web App/Other", set 161 missing.
- GraphQL `gh api graphql` bulk-fetch VERIFIED: 1 call, 2 repos + file probes, cost=1pt/5000. Use aliased query to confirm ~35 repos/call + detect docker-compose/Dockerfile (immich compose is under docker/, probe multiple paths).
- awesome-selfhosted-data VERIFIED accessible (~2000 software/*.yml, source_code_url). Use as cached validator/booster (stdlib tarball+tarfile+regex).
- Claude API (from claude-api skill): model `claude-haiku-4-5`, structured outputs via output_config.format json_schema, endpoint /v1/messages, headers x-api-key + anthropic-version:2023-06-01. Omit thinking/effort for Haiku. OpenRouter free Nemotron as fallback (wiring in generate_csv_nemotron.sh).
- Search API returns full repo objects (reuse, no per-repo REST). GitHub search 30/min; GraphQL 5000pt/hr.

## v3 STRATEGY (improvement guideline)
1. KEEP v2 wins: search-payload reuse, appeal scoring, category diversity, disk cache, expanded cats, relative date, per-set rotation.
2. GraphQL batch-confirm (replaces per-repo REST): canonical name, fork/archived re-check, + Docker/compose detection (strong self-host signal). Fall back to REST canonical_repo on failure.
3. Stronger rubric (lane 2): velocity, liveliness, polish(homepage real/license/desc/topics), novelty sweet-spot 300-3000★, community fork-ratio, anti-gaming. Hard-reject table. Docker bonus.
4. awesome-selfhosted validator boost (optional, cached, graceful).
5. Optional LLM enrichment (--enrich): Claude Haiku -> OpenRouter -> skip. category override + self_hostable demote + interest_hook + novelty. Cached. Never a single point of failure.
6. Diversity select with MIN-SCORE FLOOR (fail loudly, don't scrape barrel) + gentle cap relax.
7. --verify invariant mode (no cross-set dup / each set==50 / usedRepoUrls==union) — report drift.
8. Publish lock (reduce simultaneous-run race). Richer Notion page (category+score+hook+docker badge).
9. Coexist: import run_next_set as v1, same tracker/CSV/series, do NOT modify v1 or v2.

## Build status
- [x] research done
- [ ] write run_next_set_v3.py
- [ ] test: --verify, pure functions, --dry-run (no writes), enrichment pure parts
