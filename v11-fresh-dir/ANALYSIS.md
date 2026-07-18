# notion50 Runner Family — Full Retrospective (v1 → v10)

_Analyzed: 2026-07-10 · Scope: every runner, audit, log, and doc in `/Users/m17/2026/notion50/notion50new-v4/` · Evidence-based: numbers below come from the actual `_tmp/` audits, the tracker, and the code, not the docs._

---

## 1. The goal, as it evolved

The aim was constant from day one: **automatically publish the next numbered Notion page "Set N — 50 More Self-Hosted Open-Source Web Apps (2026)" under the 🤖 AI Hub**, with hard guarantees — exactly 50 repos, never repeating any repo from any prior set, shared tracker + master CSV, safe to run from two machines with Notion as the source of truth.

What evolved was the definition of "a good 50":

| Era | What "good" meant |
|---|---|
| v1 | Any 50 that pass a keyword self-hosted heuristic (first-come) |
| v2–v3 | *Curated* 50 — appeal-scored, category-diversified, Docker-verified, awesome-selfhosted-validated |
| v4–v5 | Same, but rigorous & operable — tight gate, self-test, verify, stats, --why |
| v6–v7 | Fix the v5 diversity regression; surface score breakdowns |
| v8–v9 | Find *hidden gems* — brand-new low-star repos the quality gate systematically excluded |
| v10 | Make it faster via parallelism + native HTTP (partially succeeded, see §4) |

Current live state: ~299 sets published, ~14,874 used repo URLs in the tracker. The system works and has published continuously (audits run through 2026-07-10).

---

## 2. What was done well / cleverly along the way

**Architecture (mostly v1, kept forever):**
- **Notion as cross-machine source of truth** with self-healing reconcile on every run — the correct fix for two-machine split-brain, and it held up across ~300 sets.
- **Safety primitives from the start**: `--dry-run`, atomic tracker writes (tmp+rename), title-collision guard, publish-then-persist ordering, per-run audit JSON with every rejection reason. This is the stuff people usually skip; it made every later version debuggable.
- **stdlib-only, no pip** — the portability constraint was honored for 10 versions.

**Clever individual wins:**
- **v2:** scoring on the *search payload itself* (search already returns the full repo object) instead of one `/repos` call per candidate — cut hundreds of API calls to ~8.
- **v3:** GraphQL batch-confirm — ~35 repos confirmed *and* probed for docker-compose/Dockerfile/.env.example in **one** API call. The single best efficiency idea in the whole project. Also: awesome-selfhosted validator (free curated ground truth), min-score fail-loud floor, `--verify` invariant mode (directly answering the TOOL-REVIEW's #1 ask), publish lock.
- **v4:** fixed the O(n²) reconcile; much tighter app gate (library/SDK/CLI rejection).
- **v5:** content-hash cache invalidation; per-query fresh-yield tracking that demotes dead queries across runs; `--why <url>` explain mode — excellent debuggability.
- **v6/v7:** the most important *product* lesson — **the LLM must never drive diversity**. v5 let LLM-assigned categories bucket the diversity phase and diversity collapsed. v6/v7 pinned diversity to the heuristic category and demoted the LLM to annotation + small tiebreak. That separation (deterministic skeleton, LLM as garnish) is the right pattern and v11 must keep it.
- **v8/v9:** diagnosing *why* good new repos never appeared (stars:>=8 floors, −3 gem penalty under 50★, only one sort order, no `created` sweep) and fixing it with a fresh-gem quota — genuine root-cause work, backed by a research report.
- **Process:** `--self-test` pure-function checks, deterministic per-set query rotation (set number as seed), caches with TTLs, graceful degradation everywhere (no GraphQL→REST, no key→heuristics).

---

## 3. What was done badly / should have been different

**1. The full-copy-per-version model became the main source of bugs and waste.**
The "never modify v1…v(N−1); every new version is a new file" rule was safe, but implemented as *copy the entire previous file* (~1,400–2,000 lines each). v9→v10 is a 1,782-line file with only ~345 changed diff lines. Ten near-identical copies means:
- drift bugs (v10 still stamps `"version": "v9"` and writes `set{N}_v9_*_audit.json` — see §4);
- ~70–90 KB re-read and re-emitted per iteration — this is where most of the *development* token cost went. Every version also restates the full history in an 80-line docstring.
- **Better:** one shared `core.py` (plumbing + gate + scoring toolkit) plus thin per-version strategy files, or one runner with `--strategy` presets. The v1-import pattern proved deltas work; it just was never applied to v2+'s own code.

**2. Search-call growth was never treated as the budget it is.**
GitHub's search API allows ~30 requests/min. Measured from audits:
- v7: ~40 calls → **median 100s/run** (min 77s)
- v9/v10: ~131–133 calls → **median 444s/run** (max 862s = 14.4 min)

That's the entire "v10 takes a lot of time" problem: 132 sequential search calls cannot finish in under ~4.5 min *no matter what*, and every 403 adds a 15–45s backoff. The fresh-gem mission (v8/v9) tripled the call count and nobody re-checked the wall-clock consequence. Worse, each run builds a pool of ~6,400 repos and scores ~2,800 of them **to pick 50** — a 56:1 waste ratio, and the ~2,750 scored-but-unused candidates are thrown away every run instead of being banked for the next one.

**3. Historical data hygiene was diagnosed but never finished.** The 2026-06-16 TOOL-REVIEW found 27 duplicate repos, 30 sets ≠ 50 repos, a master CSV 94% empty of descriptions, and two divergent CSVs. `--verify` was added (good) but the one-time cleanup/backfill was never completed. The plumbing has always been better than the data.

**4. Versions regressed each other because there was no fixed benchmark.** v5 broke diversity; v6/v7 fixed it; v8 loosened the gate v4 tightened; v10 silently removed v9's early-exit. A tiny frozen scorecard (dry-run: time, calls, category spread, score stats, % Docker) compared before/after would have caught each regression in minutes. The data existed in audits — it was just never diffed.

**5. Quality gate escapes.** The scheduled-run log shows a v2 batch selecting `SwiftSunburstDiagram` (an iOS library), `flutter-crm`, `amocrm-api-php` (a PHP API client), `plantuml-libs` — not self-hostable web apps. v4+ gates are much better, but "is there actually a compose file / is it on awesome-selfhosted / does it really serve a web UI" should gate harder than keyword bags.

---

## 4. v10 specifically — a close review of the latest

v10's stated goal: speed, via (a) native `urllib` HTTP instead of shelling out to `gh`/`curl`, and (b) `ThreadPoolExecutor` parallelism. Verdict: **the right instincts, the wrong hot path, plus real bugs.**

**What v10 got right:**
- Native `urllib` for search/GraphQL/Anthropic removes per-call subprocess fork overhead and the temp-file curl-config dance — cleaner and marginally faster.
- Parallel GraphQL confirm (5 workers) and parallel REST fallback (10 workers) — legitimately speeds up the confirm phase.
- Parallel LLM enrichment chunks — fine.
- "Smart bypass" *idea* (skip LLM enrichment for repos that are Docker-verified **and** awesome-listed) is a genuinely good token saver.

**What v10 got wrong (bugs and regressions):**
1. **The parallelism never touched the bottleneck.** `_run_search_task` was refactored into a picklable task shape, but `execute_sweep` still calls it **sequentially** in a plain for-loop. The search sweep — ~85% of the wall clock — is exactly as slow as v9. (And parallelizing it wouldn't help anyway: 30 searches/min is a server-side limit; the only real fix is *fewer calls*.)
2. **It removed v9's early-exit.** v9's quality sweep stopped at `calls >= 24 and fresh_count >= target_fresh`, and the fresh sweep at `fresh_count >= target_fresh // 2`. v10's `execute_sweep` only stops at `MAX_CALLS` — so v10 *always* burns the full 75+25 call budget even when it has enough candidates. v10 is plausibly **slower** than v9 on good days. This is the single change most responsible for "it takes a lot of time."
3. **Identity bug:** audits are hardcoded `"version": "v9"` and written to `set{N}_v9_*_audit.json` (lines 1301/1735/1760), so v9 and v10 runs are **indistinguishable** in the audit history — the 36 "v9" audits include an unknown number of v10 runs.
4. **The smart bypass fabricates enrichment.** It writes a canned `interest_hook` ("Curated awesome-selfhosted app with Docker support.") and `novelty: 3` into the same field real LLM output goes, with no marker that it's synthetic — it pollutes the enrich cache and the Notion page hooks with boilerplate.
5. **Parallel GraphQL (5 workers) courts GitHub secondary rate limits** — GitHub explicitly asks for serialized mutative/expensive calls; bursts of concurrent GraphQL from one token can trigger 403 secondary limits, which v10's GraphQL path then treats as total failure per-chunk.
6. **Token-off-argv regression:** v9 deliberately passed the Anthropic key via a curl config file so it never appeared on `ps`; v10's urllib approach is actually fine (in-process headers), but the retry loop now catches bare `Exception` and silently retries non-retryable errors (e.g. 401) four times with sleeps.
7. Duplicate/misplaced `import urllib.parse` and `import concurrent.futures` mid-file — cosmetic, but symptomatic of the copy-paste-patch workflow.

**Time budget of a current run (~444s median), reconstructed:**
- ~130 sequential search calls at ~2–3s each incl. rate-limit backoffs: **~330–400s**
- Notion reconcile of ~300 child pages (paginated) + publish 50 blocks at 0.35s pacing: **~30–50s**
- GraphQL confirm + awesome + scoring + CSV: **~20–40s**

**Token/cost profile:** normal runs use zero LLM tokens (enrich is opt-in). With `--enrich`, ~50 finalists in batches of 12 on Haiku ≈ 4–5 calls, ~6–10k tokens — cheap and cached. The token problem was never runtime; it was **development**: regenerating ~1,800-line files with 80-line historical docstrings each iteration.

---

## 5. Scorecard

| Dimension | v10 state | Note |
|---|---|---|
| Correctness of pipeline | 8/10 | Publishes reliably; identity bug + fake-enrich blemishes |
| Speed | 3/10 | 7–14 min/run; v7 proved ~100s is achievable |
| API efficiency | 3/10 | 132 search calls, 2,800 scored, for 50 picks; surplus discarded |
| Selection quality | 7/10 | Best gate + diversity + gems of the family; some escapes remain |
| Token efficiency (runtime) | 9/10 | LLM opt-in, batched, cached, bypass idea good (execution flawed) |
| Token efficiency (development) | 2/10 | Full-file copies × 10 versions |
| Observability | 9/10 | Audits, --why, --stats, --verify, breakdowns — genuinely excellent |
| Data hygiene | 5/10 | Legacy dups/off-count sets + CSV backfill still unfinished |

**Bottom line:** the family converged on the right *selection* design (deterministic gate + heuristic-category diversity + LLM as garnish, fresh-gem quota, GraphQL confirm) but drifted into an unnecessary 4–14 minute runtime and a maintenance model that manufactures drift bugs. Everything needed for a ~90-second, cheaper, better-quality v11 already exists in the codebase — see `V11-STRATEGY.md`.
