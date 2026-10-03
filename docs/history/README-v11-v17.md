# v11-fresh-dir — n50 runner family (v11 → v15)

Workspace for the "Set N — 50 More Self-Hosted Open-Source Web Apps (2026)" Notion pipeline, served at **https://n50.bjk.ai** (nginx → `127.0.0.1:8055`).

> **Use v17 for the next set of 50** — smart GraphQL-sliced discovery + quality-first gate; fills a real 50/50 in ~1–2 min. See [`V17-STRATEGY.md`](./V17-STRATEGY.md). (Older note:) **v15** It is the recommended default in the UI: v13's
> product diversity, but rate-limit **governed** (no 300s of reactive sleeps) and it
> **never crashes** on a thin pool. See [`V15-STRATEGY.md`](./V15-STRATEGY.md).

## Files

| File | What it is |
|---|---|
| `v15.py` | **Recommended 50/set runner** — v13 diversity + rate-limit governor + never-crash graceful ship + warm caches. |
| `v11.py` | Bench-backed fast discovery (original production default). |
| `v12.py` | **Proof-first runner** — tier A/B/C ship proof; does not override v11. |
| `v13.py` | **Product-diversity runner** — family floors (files/KB/media/PDF/…), AI hard-capped, theme discovery bank. *Superseded by v15.* |
| `v14.py` | **200-per-set runner** — same series numbering, plus candidate recycling (reject cache, carry pool, near-miss parking). |
| `run_next_set_v11.py` / `v12` / `v13` / `v14` / `v15` | Historical names / symlinks. |
| `run_next_set.py` | Unmodified v1 plumbing. Tracker/CSV/`_tmp` one level up (`/apps/n50/`). |
| `server.py` | Web UI: **v15 / v11 / v12 / v13 / v14** toggles (v15 default). SSE live log. |
| `V11-STRATEGY.md` … `V15-STRATEGY.md` | Design docs. |
| `ANALYSIS.md` | Retrospective v1→v10. |

## Shared state (both versions)

Both v11 and v12 **append** to the same files:

- `/apps/n50/notion-selfhosted-tracker.json`
- `/apps/n50/notion-selfhosted-master.csv`
- audits under `/apps/n50/_tmp/set{N}_{v11|v12}_*.json`

Caches are **version-scoped** so they do not clobber each other:

- v11: `bench_v11.json`, `repo_cache_v11.json`, …
- v12: `bench_v12.json`, `repo_cache_v12.json`, …

## Run (CLI)

```bash
cd /apps/n50/v11-fresh-dir

# v11 (default production)
python3 v11.py --self-test
python3 v11.py --dry-run
python3 v11.py                 # publish — only after dry-run + go-ahead

# v12 (proof-first)
python3 v12.py --self-test
python3 v12.py --dry-run
python3 v12.py                 # publish — only after dry-run + go-ahead
```

Flags (both): `--stats`, `--why URL`, `--breakdown`, `--enrich`, `--no-graphql`, `--no-awesome`, `--no-fresh`, `--target-fresh`, `--max-pages`, `--cat-cap`, `--min-score`, `--fresh-quota`, `--verify`.

## Web UI (n50.bjk.ai)

```bash
python3 server.py   # 127.0.0.1:8055 — nginx already proxies n50.bjk.ai here
```

- **v15 (safe 50/set)** is selected by default; v11–v14 remain one click away.
- Dry run / Publish run the active version.
- Publish starts directly from the button.
- Served by `systemd` unit `n50-runner.service`; deploy edits with
  `sudo systemctl restart n50-runner.service`.
- One run at a time; output streams over SSE (`proxy_buffering off` in nginx is correct).

Nginx snippet (unchanged — no need to edit for v12):

```nginx
location / {
    proxy_pass http://127.0.0.1:8055;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 86400;
    # … standard forwarded headers …
}
```

## v11 vs v12 (why v12 exists)

| | **v11** | **v12** |
|---|---|---|
| Goal | Speed (bench + early-exit) | **Solid self-hostable web apps** |
| Ship rule | Soft: docker/awesome boost score | **Hard tier A/B/C** — no tier, no ship |
| Docker proof | compose / Dockerfile / **.env.example** | compose or Dockerfile only |
| Awesome list | +score if lucky | **Seeds residual unused** + Tier B |
| App gate | stars≥30 + language can pass alone | Strict self-host / app-shape required |
| Junk escapes | Libraries, SDKs, frameworks, tutorials still appeared | Expanded `hard_reject` + no stars escape |
| Category skew | AI/LLM often 10–15/50 | Hard caps (AI≤4, DevTools≤3, …) |
| Quotas | 50 any that score | ≥40 A∪B, ≤10 C |
| Bench | 400 soft-scored | 250 proof-eligible only |
| Tracker | shared | **same shared** |

Full design: [`V12-STRATEGY.md`](./V12-STRATEGY.md).

### What “proof” means in v12

- **Tier A** — GraphQL sees compose and/or Dockerfile **and** self-host/app intent  
- **Tier B** — listed in awesome-selfhosted-data  
- **Tier C** — explicit self-host keywords/topics **and** real product homepage (≤10 of 50)

### Measured problem v12 targets

From ~25 late v11 sets: docker often ~20–30/50, almost zero awesome hits, majority `selfhost_signal=0`, and published NO-PROOF examples (API clients, frameworks, ML infra, tutorials). v11 promised ship-proof in strategy docs but never enforced it after GraphQL — v12 does.

## Auth

- GitHub: `gh auth login` or `GH_TOKEN` / `GITHUB_TOKEN`
- Notion: `NOTION_API_KEY` or `~/.config/notion/api_key`
- Optional `--enrich`: Anthropic / OpenRouter keys

## v14 — 200 per set

```bash
python3 v14.py --self-test
python3 v14.py --dry-run
python3 v14.py                 # publishes "Set N — 200 More …"
```

- Publishes **200 repos** per set; the title reads "200 More …".
- **Set numbering continues the same series** — set 391 followed the 50-repo
  set 390, and `completedSets` advances exactly as before. The tracker's
  `titleTemplate` (still "50 More …") is never rewritten, so v11/v12/v13 are
  unaffected.
- Tracker and master CSV are appended exactly as before.
- 200 is the normal size, so it does **not** need `--force-publish`.

**Recycling caches** (the anti-waste work) — all under `/apps/n50/_tmp/`:

| Cache | What it holds |
|---|---|
| `reject_cache_v14.json` | Known-bad repos, so they are never re-fetched (120d for structural rejects, 21d otherwise) |
| `carry_v14.json` | Confirmed, proof-passing repos that were not shipped — re-seeded free next run |
| `nearmiss_v14.json` | Scored candidates that never reached a confirm call |
| `bench_v14.json` | Proof-eligible bench, now **merged** rather than overwritten |

Measured on set 391's runs: the reject cache skipped **618** known-bad repos and
cut the confirm stage from **132.5s to 3.5s**.

⚠️ **Known limitation at N=200.** After 390 sets (20,000+ used URLs) several
product families are mined out — `file_upload_share` returns 82 GitHub hits with
**0 unused**. Set 391 had only 1.08× selection headroom (216 confirmed for 200
slots), so the diversity selector could not rebalance and the tail of the set
includes weak picks. See [`V14-STRATEGY.md`](./V14-STRATEGY.md) for the data and
the discovery-surface levers that would fix it.

## v15 — safe 50 per set (governed, never-crash)

```bash
python3 v15.py --self-test     # fast, offline (incl. governor + graceful checks)
python3 v15.py --dry-run       # next Set N of 50; always completes, never publishes
python3 v15.py                 # publish Set N (50). No --force-publish needed.
```

Built to fix the `exit 1` v13 hit when the mined-out corpus + GitHub rate limits left
it short of 50. v15 keeps v13's product diversity and adds:

- **Rate-limit governor** — paces GitHub *search* under its ~30/min limit (rolling
  ≤26/min + ~2.2s anti-burst gap) and reads `X-RateLimit-*` / `Retry-After` headers,
  so runs stop burning minutes in reactive `sleeping 15s/30s/45s` stacks. Measured:
  68 search calls in 158.6s with **0** reactive sleeps.
- **Never crashes** — always lenient; ships the best available set (honestly counted)
  instead of a traceback. Only a catastrophic pool (outage/auth) halts, with a clean
  one-line message. Dry-run always completes.
- **Warm caches** — reject/bench/carry seed read-only from v14, so run 1 already
  skipped **527** known-bad repos.

Full design: [`V15-STRATEGY.md`](./V15-STRATEGY.md).

## Status

- **v11**: bench-backed fast discovery (original default).
- **v12**: proof-first (tier A/B/C); `--self-test` green.
- **v13**: product-diversity runner; sets 384–390. *Superseded by v15.*
- **v14**: 200-per-set; `--self-test` green; **published Set 391 (200 repos)**.
- **v15**: built 2026-08-03; `--self-test` green; **recommended default** in the UI
  (safe 50/set). Always dry-run before the first real v15 publish.
