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
   rename/transfer dedupe (GitHub redirect check). The star floor is 3, not v17's 20,
   because these engines deliberately feature fresh gems (their median pick has 4–6 stars).
2. **`engines/topup.py` refills the freed slots** from v17's GraphQL discovery: never a repo
   already selected, one per owner, the engine's own AI ceiling (v13–v16: 3, v12: 10, v11:
   none) and `--cat-cap` (relaxed only if needed to fill), proof tier A (compose/Dockerfile)
   or B (awesome-selfhosted). Each top-up pick is marked in the audit (`topUp`) and on its
   Notion line, and the page intro counts them; their score is v17's interest score.
3. The engine's normal audit, page and CSV code then run on the final 50.

The lane walks v17's **star ladder** (below) when the top rung can't supply enough repos.
If the lane fails, the engine continues with what it has, and a short standard publish
still refuses. `python3 engines/quality.py --self-test` and `topup.py --self-test` cover the
rules offline.

## Star ladder (v17 and the lane)

v17 searches with a floor of 20 stars (10 for repos under 120 days old). New non-AI apps that
clear that bar are scarce: one published set uses up most of them for days. When a rung
can't fill the set, discovery reruns one rung lower: ≥10★ (≥5★ young), then ≥5★ (≥3★
young). Every other rule is identical on every rung, ranking still favours the most-starred
repos, and v17's page states the floor that was actually used. A lower rung's results
include every higher rung's repos (same searches, lower floor), so v17 and the lane remember
the rung that last filled a set (`_tmp/ladder_state_v17.json`, 12 h) and start there instead
of re-running rungs known to be exhausted.

## Repairing a published set

`python3 engines/repair_set.py N` re-checks set N's picks with live GitHub data and the
current rules and shows what it would replace; `--apply` replaces them from the lane under
the set's own engine rules, rebuilds the Notion page in that engine's format (new blocks
are appended before the old ones are deleted), updates the tracker entry, CSV rows and
audit (`repair` record), and keeps backups in `_tmp/backup-repair-<time>/`. Replaced repos
stay in `usedRepoUrls`, so they never come back.

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
