# v17 Strategy — smart curation (GraphQL-sliced discovery + quality-first gate)

## Why
v16 shipped ~20/50 per set in ~160s, and the tail was weak: Helm charts, game-server
wrappers, GitHub stats cards, plugins/clients for other apps, AI-account proxies, repos
with 3 stars. Trendshift stubs entered scoring with fake star counts (always 50).

## What v17 does (`v17.py`, ~800 lines, standalone; reuses v1 Notion/tracker plumbing and v16's base junk gate + categorizer)
1. **GraphQL sliced search**: `search(type:REPOSITORY)` sliced by created-date windows
   (30-day slices recently, coarser older) × self-host topics/phrases, plus 36 "life app"
   wedges (recipes, budget, photos, ebooks…). Uses the 5000/hr GraphQL budget, not the
   30/min REST search limit. 8-way parallel. About 150 slices finish in about 55s.
2. **Two-phase hydration**: search returns light fields only. A cheap gate filters on
   those, and only the survivors are hydrated with compose/Dockerfile/releases/OG-image fields.
   There is no separate confirm stage.
3. **Quality gate**: ≥20 real stars (≥10 if <120 days old), pushed within 8 months,
   compose or Dockerfile (or awesome-selfhosted), readable English description, web-app
   intent. Rejects plugins/companions/sidecars, anything built on another app
   (Jellyfin/Immich/Paperless/Karakeep/…, unless it's an alternative to it), native clients,
   Helm/Ansible/Terraform, bots, game-server wrappers, AI-account proxies, multi-account
   farming, trading/crypto, lead-gen/device farms, templates/courses, MCP servers.
4. **Interest score**: log stars, ★/month momentum, compose, recent release, custom
   social image, real homepage, org owner, license, recent pushes, curated membership
   (Trendshift / awesome), "life app" bonus, AI penalty.
5. **Diversity**: one repo per owner, ≤6 per category, AI ≤4.
6. **Dedupe beyond URLs**: rename/transfer resolution via REST redirect for name
   collisions, and exact-description match against the master CSV (catches re-uploads).
7. **Memory**: `query_memory_v17.json` skips slices that returned nothing new in 2 runs
   for 14 days, `reject_cache_v17.json` holds rejects for 45 days, and `carry_v17.json`
   holds passing-but-unshipped repos for 21 days (written on publish only).

## Measured (dry-run, Set 475, 2026-09-24)
| | v16 (set 474) | v17 |
|---|---|---|
| Repos shipped | 20 / 50 | **50 / 50** |
| Wall time | 161.8s | **65–107s** (warm/cold) |
| Rate-limit sleeps | governed | none (GraphQL, ~100–200 points of 5000/hr) |
| Duplicates caught | URL only | URL + 1–4 renames + ~10 re-uploads |

## Run
    python3 v17.py --self-test
    python3 v17.py --dry-run [--breakdown] [--quick]
    python3 v17.py            # publish Set N (50)
