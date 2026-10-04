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
5. **Diversity**: one repo per owner, ≤6 per category, AI ≤4 in the first pass (see the
   October update for how a short set is filled).
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

## October 2026 update
By 2026-10-03 v17 stopped filling sets: a dry run for Set 494 passed 151 repos through the
gate, but **132 were AI tools**, and with AI capped at 4 it selected 23/50 and refused to
publish. New self-hosted repos on GitHub are now mostly AI; the non-AI long tail was simply
not being searched. Changes:

- **Wedge windows.** Each of the 68 life-app wedges (recipes, budget, manga, vehicle,
  warranty, chores…) is also searched inside two recent creation windows (0–180 and
  180–540 days). The top-by-stars wedge page is mined out; the new long tail isn't.
- **Bounded AI fill.** Pass 1 is unchanged (AI ≤4). If the set is short, non-AI repos fill
  past the category cap first, then AI repos, up to **20 of 50**. Any AI pick must describe
  a web UI or app (`AI_APP`), so agent backends, memory APIs and serving kits never qualify.
- **Gate additions** for junk seen in real runs: AI subscription pools, "dedicated servers"
  companions, trade journals and broker sync, model-serving kits, mostly-CJK descriptions,
  "for every Jellyfin user" / "powered by Twenty CRM" style add-ons (even with a
  `media-server` topic), dashboards for someone else's instances, one-command installers,
  cold-call dialers and auto-viewers, music rippers, terminal-only tools. Firefly III,
  Coolify, Headscale, Twenty CRM, Firecrawl and Frigate joined the host-app list.
- **Query-memory fix.** A slice whose repos had already been collected by an overlapping
  slice counted as "nothing new" and was skipped for 14 days. Now only slices that find no
  unused, unrejected repo count as dead, and entries older than 28 days are pruned.
- **Audit.** Dry runs always write their audit, with `publishable`, `aiCount` and each
  pick's URL, description, language and compose/Dockerfile flags, so the UI can preview
  even a short set. The final list is ordered by interest score.

Measured on Set 494 dry runs (2026-10-03): before 23/50 (refused); after 50/50 in 70–105 s
with 5–17 AI picks, 240–250 GraphQL points, 64 MB peak memory.

Later the same day, reviewing real published picks led to more rules (libraries/SDKs for
agents or languages, agent infrastructure, backend components, SDK generators, compose
bundles, app-store packages, API wrappers, unofficial WhatsApp APIs, adult content,
command-and-control, crypto exchanges, native clients "for Android") and to narrowing others
that hit real apps (Helm *option*, apps that *include* an MCP server, standalone companion
apps, "X for games" analogies, "works with Nextcloud" compatibility). Categories gained
tabletop/gaming, CRM/helpdesk, fitness, time tracking/household, smart home and publishing.
The star ladder (see ENGINES.md) was added once Set 494 had used up most ≥20★ non-AI apps.
Self-test: 65 checks, including "must keep" cases for legitimate apps.

## Run
    python3 v17.py --self-test
    python3 v17.py --dry-run [--breakdown] [--quick]
    python3 v17.py            # publish Set N (50)
