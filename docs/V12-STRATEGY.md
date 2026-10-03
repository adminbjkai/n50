# v12 Strategy — Proof-First Self-Hostable Web Apps

## Why v12 exists

v11 delivered **speed** (bench + early-exit + ~25 search calls) but still ships many repos that are **not** self-hostable web apps: libraries, API clients, ML infra, frameworks, tutorials, and high-momentum projects with zero deploy proof.

Evidence from ~25 live v11 sets (≈ sets 355–380):

| Signal | Reality |
|---|---|
| Docker/compose on published picks | avg ~30/50 (often ~20) |
| awesome-selfhosted hits | ≈ 0 per set (list never *seeded*, only scored +6) |
| `selfhost_signal = 0` | majority of picks (topics-only and `GOOD_TOPICS` polluted) |
| Category skew | AI/LLM + Dev Tools dominate |
| Escape examples | python-binance, kemal, lmdeploy, express-zod-api, git-rewrite-commits, medium-writeups |

v11’s strategy doc promised “≥1 of {Docker, awesome, self-host topic}” — **never enforced after GraphQL**. v12 makes that promise real.

## Product definition

A pick is a **self-hosted open-source web app** iff:

> Installable server-side software you run on your own infrastructure, with a primary HTTP UI/API that *is* the product — not a library, client, list, tutorial, or weights dump.

**Implementer test:** *Would a non-developer put this behind a reverse proxy and use it every week?*

## Core design: proof tiers (hard gate)

Computed **after** GraphQL confirm + awesome index. No tier → cannot ship.

| Tier | Rule |
|---|---|
| **A** | Compose file **or** Dockerfile detected **and** self-host / product app intent (strict topics or desc phrases) |
| **B** | Listed in awesome-selfhosted-data (live, non-fork) |
| **C** | Explicit self-host keywords/topics **and** real product homepage (non-GitHub) **and** hard-reject pass — **no** compose/Dockerfile, **not** on awesome |

### Slot quotas (50)

- **≥ 40** must be tier A or B
- **≤ 10** may be tier C
- Fresh gems still need a tier (usually A or C); they do **not** bypass proof
- Fail-loud if the confirmed pool cannot fill quotas (widen search / lower min-score — never pad with unproven junk)

### Docker probe fix

v11 treated `.env.example` alone as `docker_signal`. v12 splits:

- `compose_signal` — docker-compose.yml / compose.yaml / docker-compose.yaml / docker/docker-compose.yml
- `dockerfile_signal` — root Dockerfile
- `.env.example` is recorded but **not** ship proof

## Gate tightening

1. **`hard_reject`** expanded: API clients/SDKs, ML-infra-not-app, frameworks-not-app, tutorials/writeups, research/weights, monorepo tooling, pure game clients without server UI.
2. **`passes_app_gate`**: remove stars≥30 + language escape hatch. Require strict self-host topic **or** strict desc phrase.
3. **`GOOD_TOPICS` / selfhost_signal**: bare `docker`, `open-source`, `kubernetes`, `fullstack` no longer count as self-host proof. Use `STRICT_SELFHOST_TOPICS`.
4. Library soft-escape via polluted GOOD_TOPICS removed unless tier A/B will later prove it (and hard_reject still blocks package-only wording).

## Scoring retune (proof beats virality)

| Component | v12 |
|---|---|
| compose | **+12** |
| dockerfile only | **+5** |
| awesome | **+10** |
| strict selfhost_signal | **+8** |
| momentum | cap **16** (was 24) |
| default min-score | **32** |
| LLM `self_hostable=false` | hard drop if `--enrich` (not mere −15 only) |

## Discovery (high-signal, residual-first)

1. **Lane 0 — free seed:** awesome residual (`awesome − used`) + proof-filtered bench (0 search calls).
2. **Lane 1 — high-proof composites** (~12 calls): selfhosted + docker compose / alternative / web UI phrases.
3. **Lane 2 — product category wedges** (~8): rotated niches (notes, media, finance, CRM, …) always co-constrained with self-host or compose.
4. **Lane 3 — fresh** (~8): created/updated sort, stars 1–80, **with** self-host proof phrases — no unconstrained `stars:>=1 created:>90d`.
5. Search-time negatives: `-awesome -boilerplate -template -sdk -library -tutorial …`
6. Early-exit on **proof-ready pool size**, not raw fresh_count. Cap ~40–50 search calls.
7. Bench stores only **proof-eligible** surplus (max 250).

## Diversity (lenient soft ceilings)

- Default cat-cap **5** (soft balance target)
- Soft ceilings (skew guards, not rigid walls): AI/LLM ≤10, DevTools/Networking/DevOps ≤6, Gaming ≤5, Web App/Other ≤10
- Prefer product/webapp categories in phase-1 seed order (Notes, Media, Productivity, Finance, …)
- Fresh quota default **4** — solid older missed apps are welcome; newness is not the goal
- If caps leave empty slots, soft-overflow fills with remaining A/B rather than failing the run

## Interchangeability (unchanged contract)

- Same tracker: `../notion-selfhosted-tracker.json`
- Same master CSV
- Same series title template / Notion parent
- Imports `run_next_set as v1` plumbing only
- Never modifies v11 or older files
- Always `--dry-run` first for real publishes from the UI

## v11 vs v12 at a glance

| | v11 | v12 |
|---|---|---|
| Primary goal | Speed via bench | **Quality + efficient discovery** |
| Ship proof | Soft score bonus | **Hard A/B/C gate** |
| Docker definition | compose/Dockerfile/**.env.example** | compose or Dockerfile only |
| Awesome list | +6 score if lucky | **Seed residual + Tier B** |
| App gate | stars≥30 escape | Strict self-host signal required |
| AI/LLM cap | soft cat-cap 5 (relaxable) | hard ≤4 |
| Bench | 400 soft-scored | 250 proof-eligible |
| Caches | `*_v11.json` | `*_v12.json` (separate) |
| Tracker/CSV | shared | **same shared files** |

## Success metrics (per audit)

| Metric | Target |
|---|---|
| shipProofCount / 50 | **100%** |
| tier A∪B / 50 | **≥ 40** |
| compose or awesome / 50 | **≥ 70%** preferred |
| AI/LLM share | **≤ 4** |
| Known-bad class (libs/SDKs) | **0** |
| Steady search calls | **≤ 30** warm |
| Wall clock | **≤ 180s** steady |

## UI (n50.bjk.ai)

- Default view remains **v11** (unchanged runner)
- Version control selects **v11** or **v12**; dry-run / publish for the active version
- One run at a time; nginx config unchanged (still proxies `:8055`)
