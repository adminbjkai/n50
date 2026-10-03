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

## Picking an engine

The UI shows each engine's recent fill (how many of the 50 slots its last eight sets
filled). Start with a v17 dry run. If it comes up short, try v11 (looser quality rules) or
v16/v15 (different discovery lanes); only a full set publishes.

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
