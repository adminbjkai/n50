# v13 Strategy — Product Diversity First

## Why v13

Set 381 (v12) still shipped **AI/LLM ≈ 10/50** with thin coverage of real product web apps (file share, knowledge base, media, PDF, bookmarks, monitoring, sports, etc.). Proof tiers alone do not force breadth — high-momentum AI still fills soft caps.

**v13 goal:** ship **valuable self-hostable web apps across many product families**. Solid older missed repos welcome. AI is hard-capped, not preferred.

## Core mechanisms

### 1. Product families (selection key)

| Family | Floors/50 | Ceil/50 | Examples |
|---|---:|---:|---|
| files | 3 | 6 | upload/share, personal cloud |
| knowledge | 3 | 6 | notes, wiki, KB, search |
| media | 3 | 6 | media server, photos, music |
| documents | 2 | 5 | PDF tools, paperless |
| productivity | 2 | 5 | kanban, calendar, CRM, finance |
| bookmarks_rss | 2 | 4 | bookmarks, RSS, read-it-later |
| dash_mon | 2 | 5 | homepage, uptime, status |
| security | 1 | 4 | password managers |
| sports | 1 | 3 | scores, fantasy, stats |
| db_light | 1 | 3 | SQL admin UIs |
| **ai** | **0** | **3** | only web-UI AI apps |
| devops / gaming / other | 0 | 3–6 | secondary |

Floors/ceils scale with `--target N` via `scale_n(base, N)`.

### 2. Theme discovery bank

Lane 2b runs rotated product queries: file sharing, notion alternatives, media servers, PDF editors, video proxy/stream, startpages, bookmarks, uptime, DB admin, sports, rust self-hosted web, recipes, CMS, helpdesk, … — each co-constrained with self-hosted / docker compose / web UI.

### 3. Multi-pass selection (`select_diverse`)

1. **Floors** — fill each product family up to floor  
2. **Score fill** under family ceils (prefer A/B)  
3. **Soft overflow** (non-AI) to hit N  
4. **AI hard-ceil enforce** — drop excess AI rather than ship an AI pile  
5. Proof A∪B / C fractions  

### 4. Scoring

- Product families: **+6**  
- AI with web UI: **−8** damp; AI without web UI: **hard drop** (unless awesome)  
- Rust non-AI: **+1.5**  
- Compose **+14**, Dockerfile **+7**, awesome **+10**  
- Mild youth; maturity bonus for active established projects  

### 5. Flags

```bash
python3 v13.py --dry-run                 # next Set N of 50
python3 v13.py                           # publish Set N
python3 v13.py --count 300 \
  --special-title "…" --force-publish    # one-off showcase page
```

Special pages: custom title, **do not** advance `completedSets`, still append all URLs to `usedRepoUrls`.

## v12 vs v13

| | v12 | v13 |
|---|---|---|
| Diversity | soft caps only | **family floors + AI hard ceil** |
| Discovery | general selfhosted | **theme bank** (product wedges) |
| AI | often 8–12/50 | **≤ 3/50** |
| Count | fixed 50 | `--target` / `--count` (e.g. 300) |
| Special pages | no | `--special-title` |

## Success metrics (N=50)

- AI/LLM ≤ 3  
- files+knowledge+media+documents ≥ 8 combined  
- ship proof 100%  
- ≥ 12 product families represented when pool allows  
