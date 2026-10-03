# v14 Strategy — 200 per set + candidate recycling

## Why v14

Two problems measured on the v13 sets (388–390):

**1. Fetched repos went to waste.** Per run: ~500 repos fetched → ~120–250 scored
→ 50 shipped → only 12–40 carried forward. Everything else was discarded, and
the rejects (95–157 per run) were re-discovered and re-confirmed on the very
next run because nothing remembered them.

**2. Throughput.** 50 repos per set against a corpus of 20,000+ already-used
URLs means each run pays a large fixed discovery cost for a small output.

v14 keeps every v13 quality mechanism (proof tiers, family floors, AI hard cap)
and changes the economics.

## What changed

### 1. Target = 200 per set

- `DEFAULT_TARGET = 200`; title comes from `V14_TITLE_TEMPLATE`
  → `Set {n} — 200 More Self-Hosted Open-Source Web Apps (2026)`.
- **Set numbering continues the same series.** Set 391 followed set 390 (a
  50-repo v13 set). `completedSets` advances exactly as before.
- The tracker's own `titleTemplate` still says "50 More …" and is never
  rewritten, so v11/v12/v13 keep publishing their own titles unchanged.
- Publishing 200 does **not** require `--force-publish` (the guard now compares
  against `DEFAULT_TARGET`); any other count still does.

### 2. Reject cache — `_tmp/reject_cache_v14.json`

Every `hard_reject` / `not-live` / `no-ship-proof` / `duplicate` URL is stored
with a timestamp and skipped at search-ingest time on later runs.

- Reasons about the repo's *nature* (`not-live/fork/archived`,
  `ai-without-webui`, `duplicate`) get a 120-day TTL.
- Everything else gets 21 days, because a repo can gain a compose file.
- Capped at 40,000 entries, newest kept.

Measured effect: second run skipped **618 known-bad repos** and the confirm
stage dropped from **132.5s → 3.5s**.

### 3. Carry pool — `_tmp/carry_v14.json`

Confirmed, proof-passing repos that did not make the cut are persisted (merged,
not overwritten) for 21 days and re-seeded as lane-0 input next run at zero
search cost. v13 threw this GraphQL spend away.

### 4. Near-miss parking — `_tmp/nearmiss_v14.json`

Scored candidates that never reached a confirm call are parked slim and
re-seeded next run. (Zero on runs where the shortlist covers all scored
candidates — it only fires when discovery outruns the confirm budget.)

### 5. Bench is merged, not clobbered

`BENCH_MAX` 250 → 900, and the bench is now unioned with the surviving previous
bench instead of being overwritten wholesale.

### 6. Family-gap top-up wave

After the first confirm, families below ~1.6× their scaled floor trigger
targeted `THEME_QUERY_BANK` searches (sorted by stars/forks, pages 1–3, so they
don't repeat the main sweep's page-1-by-updated results).

### 7. Graduated overflow ceiling

v13's pass-3 overflow allowed `2 × ceil` in one go, which is how a single family
could reach 40/200 against a ceiling of 20. v14 fills in stages — 1.25×, then
1.5×, then 2.0× — so slots spread before any family piles up.

### 8. Honest diversity reporting

The audit now carries `familyFloorShortfall`, `confirmedPoolDepth`, and
`selectionHeadroom`, and the run prints families that came in under floor.

## Known limitation — the corpus is mined out for narrow families

Set 391 shipped with `files 2/12`, `knowledge 2/12`, `documents 1/8`,
`bookmarks_rss 0/8`. This is **not** a selection bug. Direct measurement:

| Theme query | Raw GitHub hits | Unused after 390 sets |
|---|---:|---:|
| `file_upload_share` | 82 | **0** |
| `knowledge_wiki_notes` | 62 | **0** |
| `bookmarks` | 34 | **0** |
| `pdf_editor` | 0 | 0 |

After 20,000+ used URLs, those product families contain no unused repos that
the current queries can reach. Selection headroom on set 391 was **1.08×**
(216 confirmed for 200 slots) — below roughly 1.5× the diversity selector has
nothing to rebalance with, so it must take almost everything confirmed.

**Consequence:** at N=200 the tail of the set includes weak picks (guide repos,
dotfile/compose collections, personal homelab configs) that a 50-repo set would
have out-competed. Raising quality at this size needs *new discovery surface*,
not tighter selection — see below.

## Next levers (not yet built)

1. Broaden the theme bank with new query wedges and non-English terms.
2. Mine awesome-selfhosted **forks/PRs** and sibling lists (awesome-sysadmin,
   selfh.st) — `awesomeResidual` has been 0 for many sets, which is why tier B
   is 0.
3. Crawl "alternativeto" style seeds and GitHub topic pages directly.
4. Consider N=100 as the sustainable size until discovery surface grows.

## Files

| Path | Purpose |
|---|---|
| `v14.py` | The runner |
| `run_next_set_v14.py` | Symlink → `v14.py` |
| `_tmp/reject_cache_v14.json` | Known-bad repos (TTL'd) |
| `_tmp/carry_v14.json` | Confirmed-but-unshipped carryover |
| `_tmp/nearmiss_v14.json` | Scored-but-unconfirmed parking |
| `_tmp/bench_v14.json` | Merged proof-eligible bench |
| `_tmp/set{N}_v14_*.json` | Per-run audits |
