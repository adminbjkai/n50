# Engines

Every engine builds the next set in the same numbered series and appends to the same
tracker and master CSV, so they can be swapped freely. Each keeps its own caches in `_tmp/`
(`*_vNN.json`) and writes one audit per run. All take `--self-test` and `--dry-run`.

| Engine | Name in UI | Set size | Discovery | Notes |
|---|---|---|---|---|
| **v17** | Smart (default) | 50 | GitHub GraphQL search sliced by creation date × self-host topics, plus life-app wedges in recent windows, Trendshift, awesome-selfhosted, carry pool | [V17-STRATEGY.md](V17-STRATEGY.md) |
| v16 | Trendshift | 50 | v15 + Trendshift trending feeds (daily/weekly/monthly/yearly/self-hosted topic) | v15's governor and recycling; Trendshift momentum bonus; 12 h feed cache |
| v15 | Safe 50 | 50 | v13's product-family queries under a REST rate-limit governor | [V15-STRATEGY.md](V15-STRATEGY.md) |
| v11 | Bench | 50 | Bench-backed REST discovery with early exit | [V11-STRATEGY.md](V11-STRATEGY.md) |
| v12 | Proof-first | 50 | REST + awesome-selfhosted residuals; tier A/B/C deploy proof | [V12-STRATEGY.md](V12-STRATEGY.md) |
| v13 | Diversity | 50 | Family floors, theme bank, AI ≈3/set | [V13-STRATEGY.md](V13-STRATEGY.md) |
| v14 | 200 per set | 200 | v13 + reject cache, carry pool, near-miss parking | [V14-STRATEGY.md](V14-STRATEGY.md) |

**Full sets only.** Since late September 2026 every engine's standard publish refuses
anything but a full set (50, or 200 for v14) and exits without writing to Notion. The older
"ship what you found" floors in v13–v16 (30, 10, 10 and 110) now only shape selection; their
dry runs may still print "would publish … (N repos)" for a short set, but the publish would
stop. v14's guard compared against 50 and so could never publish a 200 set; it was fixed on
2026-10-03 to compare against its own target. v17 always writes its dry-run audit, so the UI
can preview even a short v17 set. The others write one only when selection itself succeeds
(v11/v12: exactly 50; v13–v16: above their selection floors); otherwise the run ends with
an error line, which the console shows.

v1–v10 predate this folder (retrospective: [history/ANALYSIS-v1-v10.md](history/ANALYSIS-v1-v10.md)).
The UI tags a published set with its engine from the tracker or its audit file name; 206
early sets (all at or below Set 377) have neither and show "—".

## Quality screen and top-up lane (v11–v16)

The REST-search engines are mined out (a typical run confirms 45–48 candidates for 50 slots)
and their own junk rules are old: real sets shipped deployment templates, compose bundles,
0-star repos, AI-account proxies, native clients, add-ons for other apps, renamed copies of
published repos and the same owner several times. Since 2026-10-03, right after an engine's
own selection and before its shortfall check:

1. **`engines/quality.py` screens the picks** with v17's gate on search-level fields (junk
   patterns, readable description, not stale, not a fork/template, not built on another app),
   web-app intent, the AI rule (an AI description must show a web UI), owner dedupe (best
   *passing* pick per owner), re-upload dedupe (same description as a published repo) and
   rename/transfer dedupe (GitHub redirect check), and a code check (a repo with no primary
   language and no Dockerfile/compose is a README or guide, not an app). The star floor is 3,
   not v17's 20, because these engines deliberately feature fresh gems (their median pick has
   4–6 stars). Kept picks are relabelled with v17's categorizer, so every engine publishes
   the same, more accurate categories; this runs after selection and changes labels only.
2. **`engines/topup.py` refills the freed slots** from v17's GraphQL discovery: never a repo
   already selected, one per owner, the engine's own AI ceiling (v13–v16: 3, v12: 10, v11:
   none; a pick counts as AI by its category or by AI words in its description, the same test
   for the engine's own picks and lane picks, and AI picks past the ceiling are replaced)
   and `--cat-cap` (held on every star rung, relaxed only if the whole ladder can't fill
   the set within it), proof tier A (compose/Dockerfile)
   or B (awesome-selfhosted). Each top-up pick is marked in the audit (`topUp`) and on its
   Notion line, and the page intro counts them; their score is v17's interest score.
3. The engine's normal audit, page and CSV code then run on the final 50.

The lane walks v17's **star ladder** (below) when the top rung can't supply enough repos.
If the lane fails, the engine continues with what it has, and a short standard publish
still refuses. `python3 engines/quality.py --self-test` and `topup.py --self-test` cover the
rules offline.

When a run still can't reach a full set, every engine exits with one clean line (no
traceback) saying how many clean picks it had and that nothing was written.

**v14 (200 per set)** rarely has enough clean candidates now. A dry run on 2026-10-03, after
seven 50-repo sets that day, got 78 candidates of its own; only 9 passed the screen, and the
lane could add 46, so it stopped at 55/200. It will work again only when far more new
apps have appeared.

## Measured on 2026-10-03 (Sets 494–500, one per engine)

| Set | Engine | Own picks kept | From the lane | Later repairs |
|---|---|---|---|---|
| 494 | v17 | 50 | — | 4 replaced (agent infra, HITL library, backend, *arr companion) |
| 495 | v16 | 50 at publish | — | 27 (first repair; the publish predates the screen), then 2 (LibreChat renamed copy, adult-flagged downloader) |
| 496 | v15 | 9 | 41 | 6 (statainer renamed copy, packaging, API wrappers, crypto, SSO lib) |
| 497 | v13 | 9 | 41 | 11 (companions, bots, game servers, reseller, bulk mailer, renamed copy) |
| 498 | v16 | 2 | 48 | 9 (add-ons, language/SDK, non-English, SVG cards, HA add-on) |
| 499 | v12 | 4 | 46 | 6 (API wrapper, *arr tool, personal repos, Spotify ingest, UniFi add-on) |
| 500 | v11 | 9 | 41 | 8 (clients, CLI, library, sync add-ons, packaging, lead-gen) |

Repairs happened because the rules kept improving during the day's review. An independent
review that night led to a second round: 494's page rebuilt with its true star floor; 495
2 replaced (README-only guide, AI over the ceiling); 496 1 (category cap); 497 9 (AI over
the ceiling, 7 lane picks over the Monitoring cap); 499 1 (Telegram bot); 500 1 (Navidrome
client); about 25 categories relabelled across the seven sets. Every page, tracker entry
and CSV row agree.

## Star ladder (v17 and the lane)

v17 searches with a floor of 20 stars (10 for repos under 120 days old). New non-AI apps that
clear that bar are scarce: one published set uses up most of them for days. When a rung
can't fill the set, discovery reruns one rung lower: ≥10★ (≥5★ young), then ≥5★ (≥3★
young). Every other rule is identical on every rung, ranking still favours the most-starred
repos, and v17's page states the floor that was actually used (a repaired v17 page states
the highest rung that all of its picks clear). A lower rung searches a superset of a higher
rung's pool, so v17 and the lane remember the rung that last filled a set
(`_tmp/ladder_state_v17.json`, 12 h) and start there instead of re-running rungs known to
be exhausted. The trade-off is that the picks can include lower-star repos the higher rung
would have left out. Only fills of 10 or more slots read or write this memory, so a small
repair always starts at the top rung.

## Repairing a published set

`python3 engines/repair_set.py N` re-checks set N's picks with live GitHub data and the
current rules (including the AI ceiling, categories and, for lane picks, the engine's
category cap) and shows what it would change;
`--apply` replaces failing picks from the lane under the set's own engine rules, rebuilds
the Notion page in that engine's format (new blocks are appended before the old ones are
deleted), updates the tracker entry, CSV rows (in place) and audit (`repairs`, one record
per repair), and keeps backups in `_tmp/backup-repair-<time>/`. `--rebuild` rewrites the
page even when nothing changed. Replaced repos stay in `usedRepoUrls`, so they never come
back. The audit's other fields still describe the original run.

## Picking an engine

The UI shows each engine's recent fill (how many of the 50 slots its last eight sets
filled). Any engine can be used: a short run is topped up from v17's lane (below), so the
choice is about style of picks. v17 is strictest on junk; v13–v16 favour product-family
diversity; v11 is fastest with looser rules. Only a full set publishes.

A publish always runs discovery again rather than reusing the dry run's list, so its picks
can differ slightly from the preview (new repos, refreshed caches).

## Shared rules

- **No repo twice:** exact URL match against `usedRepoUrls` in the tracker. v17 also
  resolves renamed/transferred repos and drops re-uploads with a description identical to
  one already published.
- **Notion first:** every engine reconciles the tracker with the live Notion children of
  the parent page before numbering the next set, and refuses a title that already exists.
- **One publish at a time:** the server runs one engine at a time; engines also hold a
  publish lock file in `_tmp/`.
