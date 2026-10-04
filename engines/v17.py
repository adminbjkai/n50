#!/usr/bin/env python3
"""
v17.py — SMART 50-per-set runner: GraphQL-sliced discovery + quality-first curation.

Same numbered Notion series, same tracker + master CSV as v1–v16. Reuses v1 plumbing
(Notion/tracker) and v16's categorizer + base junk gate; everything else is new and lean.

Why v17: v16 averaged ~20/50 and the tail was weak (helm charts, game-server wrappers,
GitHub stats cards, plugins/clients for other apps, AI-account proxies, repos with 3 stars).
Root causes: (1) REST search is 30/min and returns thin, mined-out first pages;
(2) a separate confirm stage re-fetched every candidate; (3) the gate let anything with
>=3 stars through, and Trendshift stubs carried fake star counts into scoring.

Strategy — run smarter, not harder:
  1. GRAPHQL SLICED SEARCH. One GraphQL search = 100 fully-hydrated repos (stars, dates,
     topics, license, releases, compose/Dockerfile proof, OG image) for 1 point of a
     5000/hr budget — NOT the 30/min REST search limit. Queries are sliced by
     created-date windows × self-host topics, so each slice surfaces a different
     1000-result window instead of re-reading the same mined-out top page.
     Run 6-way parallel. No separate confirm stage.
  2. ADAPTIVE QUERY MEMORY. Each slice's yield of new, passing repos is remembered;
     slices that returned nothing new twice are skipped for 14 days (recent windows are
     always re-run because they keep changing).
  3. QUALITY-FIRST FILTER. Real stars (>=20, or >=10 if <120 days old), pushed within
     ~8 months, deploy proof (compose/Dockerfile) or awesome-selfhosted, English-readable
     description, web-app intent, and an expanded junk gate (helm/ansible/terraform,
     plugins/extensions/clients for other apps, game-server wrappers, bots, AI-account
     proxies, stats cards, trading/crypto, packaging of someone else's app).
  4. INTEREST SCORE. Stars (log), star velocity (stars/month), fresh release, compose,
     custom social image, real homepage, org owner, license, recent activity, curated
     membership (awesome-selfhosted / Trendshift) and a bonus for under-served
     "life" families (files, photos, media, knowledge, finance, home, food, books…).
  5. DIVERSITY. One repo per owner, <=6 per category, AI/LLM <=4 in the first pass. If the
     set is still short, non-AI picks fill past the category cap first, then AI apps that
     have a real web UI top it up, never beyond AI_FILL_CAP (20 of 50).
  6. SAFE DEDUPE. Exact URL match against 24k used URLs, plus rename/transfer detection:
     a candidate whose repo name collides with a used repo is resolved via the REST
     redirect so a renamed repo is never shipped twice.
  7. RECYCLING. Passing-but-unshipped repos go to a carry pool (21-day TTL), rejects to a
     reject cache, so the next run starts warm.

Always --dry-run first. Auth: gh CLI or GH_TOKEN; Notion token via env/file.
"""

import argparse
import csv
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
import v16  # noqa: E402 — categorize, base junk gate, token, trendshift, lock-free helpers
v1 = v16.v1

VERSION = "v17"
DEFAULT_TARGET = 50
TITLE_TEMPLATE = "Set {n} — 50 More Self-Hosted Open-Source Web Apps (2026)"
CACHE_DIR = v1.AUDIT_DIR
QUERY_MEM = CACHE_DIR / "query_memory_v17.json"
CARRY_CACHE = CACHE_DIR / "carry_v17.json"
REJECT_CACHE = CACHE_DIR / "reject_cache_v17.json"
RENAME_CACHE = CACHE_DIR / "rename_cache_v17.json"
LOCKFILE = CACHE_DIR / "publish_v17.lock"
LADDER_STATE = CACHE_DIR / "ladder_state_v17.json"

MIN_STARS = 20
MIN_STARS_YOUNG = 10          # repos created < YOUNG_DAYS ago
# When a rung can't fill the set, discovery reruns one rung lower. Every other rule is the
# same on every rung, and ranking still prefers the most-starred repos.
STAR_LADDER = [(20, 10), (10, 5), (5, 3)]   # (floor, floor if younger than YOUNG_DAYS)
LADDER_MEMORY_HOURS = 12      # start at the rung that last filled a set, for this long
YOUNG_DAYS = 120
MAX_PUSH_AGE_DAYS = 240
CAT_CAP = 6
AI_CAP = 4                    # first-pass cap; the set is curated around non-AI apps
AI_FILL_CAP = 20              # absolute AI ceiling when topping up a short set
WORKERS = 8
CARRY_TTL_DAYS = 21
REJECT_TTL_DAYS = 45
SKIP_DEAD_SLICE_DAYS = 14

SELFHOST_TOPICS = {"self-hosted", "selfhosted", "self-hosting", "self-hostable", "homelab",
                   "home-server", "selfhosted-app", "home-lab", "self-host"}
WEBAPP_TOPICS = {"web-app", "webapp", "web-application", "dashboard", "pwa", "web-ui", "webui"}
LIFE_FAMILY_WORDS = (
    "file", "photo", "image", "gallery", "video", "music", "podcast", "audiobook", "ebook",
    "book", "library", "recipe", "meal", "food", "grocery", "pantry", "inventory", "finance",
    "budget", "expense", "invoice", "accounting", "note", "wiki", "knowledge", "bookmark",
    "read-later", "rss", "feed", "calendar", "contact", "email", "mail", "home assistant",
    "home-automation", "smart home", "garden", "plant", "fitness", "health", "workout",
    "habit", "journal", "diary", "travel", "map", "gps", "pdf", "document", "paperless",
    "password", "vault", "backup", "sync", "family", "kids", "board game", "collection",
    "media server", "jellyfin", "plex", "kanban", "wedding", "event", "genealogy", "family tree",
)

# --- query plan -------------------------------------------------------------
TOPIC_QUALIFIERS = ["topic:self-hosted", "topic:selfhosted", "topic:homelab",
                    "topic:self-hosting", "topic:self-hostable"]
PHRASE_QUALIFIERS = ['"self-hosted" in:description', '"selfhosted" in:description',
                     '"homelab" in:description',
                     '"docker compose" "web" in:description',
                     '"open source alternative" in:description',
                     '"alternative to" "self" in:description']
# Interest wedges: life/product domains paired with self-host framing.
WEDGES = ["recipe", "budget", "photo", "bookmark", "ebook", "music", "podcast", "wiki",
          "notes", "inventory", "fitness", "calendar", "kanban", "invoice", "pdf",
          "file sharing", "rss", "genealogy", "garden", "home assistant", "password",
          "backup", "dashboard", "monitoring", "analytics", "forms", "surveys", "cms",
          "forum", "chat", "email", "video", "gallery", "maps", "travel", "habit",
          "home inventory", "media server", "photo library", "read later", "link shortener",
          "pastebin", "file upload", "document management", "time tracking", "project management",
          "helpdesk", "crm", "status page", "uptime", "password manager", "whiteboard", "diagram",
          "spreadsheet", "git", "wishlist", "chores", "meal planner", "plant", "pets", "vehicle",
          "warranty", "subscription tracker", "contacts", "radio", "comics", "manga", "games library"]
# Wedges re-searched inside recent creation windows: the top-by-stars wedge page is mined
# out, but each domain keeps growing a long tail of new apps.
WEDGE_WINDOWS = [(0, 180), (180, 540)]


def _windows(now):
    """Created-date slices: fine-grained recently (where new repos live), coarser older."""
    out = []
    edges = [0, 30, 60, 90, 120, 180, 240, 300, 365, 480, 600, 730, 1095, 1460, 2190]
    for a, b in zip(edges, edges[1:]):
        hi = (now - timedelta(days=a)).strftime("%Y-%m-%d")
        lo = (now - timedelta(days=b)).strftime("%Y-%m-%d")
        out.append((f"{lo}..{hi}", a))
    return out


def build_query_plan(now):
    plan = []
    push = (now - timedelta(days=MAX_PUSH_AGE_DAYS)).strftime("%Y-%m-%d")
    for win, age in _windows(now):
        floor = MIN_STARS_YOUNG if age < YOUNG_DAYS else MIN_STARS
        for tq in TOPIC_QUALIFIERS:
            plan.append({"q": f"{tq} created:{win} stars:>={floor} pushed:>={push} "
                              f"fork:false archived:false sort:stars", "recent": age < 90})
        if age < 365:
            for pq in PHRASE_QUALIFIERS:
                plan.append({"q": f"{pq} created:{win} stars:>={floor} pushed:>={push} "
                                  f"fork:false archived:false sort:stars", "recent": age < 90})
    for w in WEDGES:
        plan.append({"q": f'"{w}" self-hosted in:name,description,topics stars:>={MIN_STARS} '
                          f"pushed:>={push} fork:false archived:false sort:stars", "recent": False})
        for a, b in WEDGE_WINDOWS:
            hi = (now - timedelta(days=a)).strftime("%Y-%m-%d")
            lo = (now - timedelta(days=b)).strftime("%Y-%m-%d")
            floor = MIN_STARS_YOUNG if a < YOUNG_DAYS else MIN_STARS
            plan.append({"q": f'"{w}" self-hosted in:name,description,topics created:{lo}..{hi} '
                              f"stars:>={floor} pushed:>={push} fork:false archived:false sort:stars",
                         "recent": False})
    return plan


# --- GraphQL ---------------------------------------------------------------
_FIELDS = """
  nameWithOwner url stargazerCount forkCount createdAt pushedAt description homepageUrl
  isFork isArchived isTemplate isMirror usesCustomOpenGraphImage
  owner { __typename login }
  primaryLanguage { name }
  licenseInfo { spdxId }
  repositoryTopics(first:12){ nodes{ topic{ name } } }
  releases { totalCount }
  latestRelease { publishedAt }
  c1: object(expression:"HEAD:docker-compose.yml"){ __typename }
  c2: object(expression:"HEAD:compose.yaml"){ __typename }
  c3: object(expression:"HEAD:docker-compose.yaml"){ __typename }
  c4: object(expression:"HEAD:compose.yml"){ __typename }
  c5: object(expression:"HEAD:docker/docker-compose.yml"){ __typename }
  d1: object(expression:"HEAD:Dockerfile"){ __typename }
"""
# Search returns LIGHT fields only (fast: ~2.5s/100). The heavy fields above (compose /
# Dockerfile objects, releases) are fetched later only for survivors of the cheap gate.
_LIGHT = """
  nameWithOwner stargazerCount createdAt pushedAt description isFork isArchived isTemplate isMirror
  repositoryTopics(first:12){ nodes{ topic{ name } } }
"""
SEARCH_Q = ("query($q:String!,$after:String){ rateLimit{remaining} search(type:REPOSITORY,"
            " query:$q, first:%d, after:$after){ repositoryCount pageInfo{hasNextPage endCursor}"
            " nodes{ ... on Repository {" + _LIGHT + "} } } }")

_gql_lock = threading.Lock()
_gql_stats = {"calls": 0, "errors": 0, "remaining": None}


def gql(query, variables=None, retries=3):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    for attempt in range(retries):
        req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {v16.GH_TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode())
            with _gql_lock:
                _gql_stats["calls"] += 1
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"])[:200])
            return data.get("data") or {}
        except urllib.error.HTTPError as e:
            wait = float(e.headers.get("Retry-After") or (4 * (attempt + 1)))
            if e.code in (403, 429, 502, 503) and attempt + 1 < retries:
                time.sleep(min(wait, 60))
                continue
            with _gql_lock:
                _gql_stats["errors"] += 1
            return None
        except Exception:  # noqa: BLE001 — network blips: retry then give up quietly
            if attempt + 1 < retries:
                time.sleep(3 * (attempt + 1))
                continue
            with _gql_lock:
                _gql_stats["errors"] += 1
            return None
    return None


def normalise(node):
    if not node or not node.get("nameWithOwner"):
        return None
    full = node["nameWithOwner"]
    return {
        "full_name": full, "html_url": f"https://github.com/{full}",
        "description": (node.get("description") or "").strip(),
        "topics": [t["topic"]["name"] for t in (node.get("repositoryTopics") or {}).get("nodes", [])],
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "stargazers_count": node.get("stargazerCount", 0),
        "forks_count": node.get("forkCount", 0),
        "created_at": node.get("createdAt"), "pushed_at": node.get("pushedAt"),
        "homepage": node.get("homepageUrl") or "",
        "license": {"spdx_id": (node.get("licenseInfo") or {}).get("spdxId")},
        "owner_type": (node.get("owner") or {}).get("__typename"),
        "og_image": bool(node.get("usesCustomOpenGraphImage")),
        "releases": (node.get("releases") or {}).get("totalCount", 0),
        "latest_release": (node.get("latestRelease") or {}).get("publishedAt"),
        "compose": any(node.get(k) for k in ("c1", "c2", "c3", "c4", "c5")),
        "dockerfile": bool(node.get("d1")),
        "_hydrated": "c1" in node,
        "_bad_flags": bool(node.get("isFork") or node.get("isArchived")
                           or node.get("isTemplate") or node.get("isMirror")),
    }


def search_slice(q, pages=1, per_page=100):
    out, after = [], None
    for _ in range(pages):
        data = gql(SEARCH_Q % per_page, {"q": q, "after": after})
        if not data:
            break
        s = data.get("search") or {}
        if data.get("rateLimit"):
            _gql_stats["remaining"] = data["rateLimit"]["remaining"]
        out.extend(r for r in (normalise(n) for n in s.get("nodes") or []) if r)
        pi = s.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    return out


def hydrate(full_names, batch=25):
    """Fetch full metadata for known owner/name pairs (Trendshift, carry refresh)."""
    out = {}
    names = [n for n in full_names if re.fullmatch(r"[\w.-]+/[\w.-]+", n or "")]
    chunks = [names[i:i + batch] for i in range(0, len(names), batch)]

    def one(chunk):
        parts = []
        for i, fn in enumerate(chunk):
            o, n = fn.split("/", 1)
            parts.append(f"r{i}: repository(owner:{json.dumps(o)}, name:{json.dumps(n)}){{...F}}")
        q = "{ " + " ".join(parts) + " }\nfragment F on Repository {" + _FIELDS + "}"
        data = gql(q) or {}
        return [normalise(data.get(f"r{i}")) for i in range(len(chunk))]

    with ThreadPoolExecutor(WORKERS) as ex:
        for res in ex.map(one, chunks):
            for r in res:
                if r:
                    out[r["full_name"].lower()] = r
    return out


# --- quality gate ------------------------------------------------------------
JUNK = [
    # The repo IS a chart/role/module (an app that merely offers a Helm option is fine).
    (r"\b(a |an |the )?helm charts? (for|to|that)\b|\bansible (roles?|playbooks?|collections?) (for|to|that)\b|\bterraform (modules?|providers?) (for|to|that)\b|\bk8s operator\b|\bkubernetes operator\b", "infra-packaging"),
    (r"\b(plugin|extension|add-?on|addon|integration|theme|skin|widget|module|mod)\s+(for|to)\b", "plugin-for-other-app"),
    (r"\b(client|app|frontend|player|companion)\s+for\s+(jellyfin|plex|navidrome|subsonic|emby|immich|home assistant|nextcloud|sonarr|radarr|mastodon|matrix|lemmy)", "client-for-other-app"),
    (r"\b(android|ios|iphone|mobile|desktop|windows|macos|tvos)\s+(app|client|application)\b|\b(apps?|clients?) for (android|ios|iphone|windows|macos)\b", "native-client"),
    (r"\bfor (the |your )?\*?arr\b( (ecosystem|stack|apps?))?|\*arr (media )?stack\b", "plugin-for-other-app"),
    (r"\b(rest )?api\b.{0,50}\busing (yt-dlp|ffmpeg|puppeteer|playwright|selenium)\b|\bapi (wrapper )?(for|around) yt-dlp\b", "component-or-dev-tool"),
    (r"\bdocker[- ]?(compose)?[- ]?(solution|setup|stack|configuration|config|environment|template)s?\s+(for|to)\b|\bready[- ]to[- ]use\b.{0,40}\bdocker[- ]?compose\b|\b(monitoring|logging|observability) stack\b.{0,60}\b(prometheus|grafana|loki)\b", "packaging-of-other-app"),
    (r"\bdedicated server\b|\bfor [\w ]{0,30}dedicated servers\b|\bgame server (for|of)\b|\bserver for (minecraft|palworld|valheim|ark|rust|terraria)", "game-server-wrapper"),
    (r"\b(discord|telegram|slack|whatsapp|twitch|feishu|lark|wechat|dingtalk|qq)\b.{0,30}\bbot\b|\bbot for (discord|telegram|slack|feishu)", "chat-bot"),
    (r"\bprivate servers?\b|\bgame servers?\b.{0,40}\b(minecraft|valheim|palworld|terraria|factorio|cs2|ark)\b", "game-server-wrapper"),
    (r"\b(browser-based |web )client for (?!your\b)[\w.-]+|\bfor use alongside\b|\bcompanion (tool|app|service) (for|to)\b", "companion-for-other-app"),
    (r"2api\b|\bto[- ]?api\b|account pool|\b(ai |llm )?subscription pool|reverse[- ]proxy for (chatgpt|claude|openai|gemini|codex|cursor|kiro|grok|copilot)|\b(chatgpt|claude|gemini|codex|kiro|grok|copilot|cursor) (account|api) (proxy|pool|gateway)", "ai-account-proxy"),
    (r"\b(trading bot|crypto|binance|bybit|coinbase|airdrop|memecoin|defi|quant(itative)? trading|stock pick|arbitrage|mev)\b", "trading-crypto"),
    (r"\b(readme|github) (stats|profile|streak)|\bstats cards?\b|\bsvg cards?\b|profile readme", "github-vanity"),
    (r"\buserscript\b|\btampermonkey\b|\bbrowser extension\b|\bchrome extension\b", "browser-extension"),
    (r"\b(docker images?|docker-?compose files?|compose (files|stack|templates?)|dockerfiles?|deployment|install(er|ation) scripts?|setup scripts?)\s+for\b", "packaging-of-other-app"),
    (r"\bmade for (my|own|our) (own )?(personal )?(server|homelab|setup)\b|\bpersonal use\b|^(my|personal) |\bmy (homelab|home lab|server|setup|infra)\b|\bhomelab (config|setup|infrastructure|repo|gitops)\b|\bgitops\b|\bdotfiles\b|\bnixos config", "personal-setup"),
    (r"\b(starter|boilerplate|template|scaffold|example|sample|demo|tutorial|course|workshop|homework|assignment|learning)\b( (app|project|repo|for|of|to))", "template-or-learning"),
    (r"\bawesome\b.*\b(list|collection)\b|\bcurated list\b", "list"),
    # The repo IS an MCP server (apps that include one as a feature are fine).
    (r"\b(mcp|model context protocol) servers? (for|that|to|which|exposing)\b|\bis an? (mcp|model context protocol) server\b", "mcp-server"),
    (r"\bprogramming language\b|\bsdk (built|written) in\b|\bengine sdk\b|\breference (implementation|server|relay|spool)\b|\b(sdk|code|client) generator\b|\bbackend (api|service|server)? ?for [\w.-]+\b", "component-or-dev-tool"),
    (r"\b(ai|api|llm|token) (reseller|resale)\b|\breseller panel\b", "ai-account-proxy"),
    (r"\bsend (e-?mails?|messages|sms) without limits\b|\bbulk (e-?mail|sms|mail)\w*\b|\bmass (e-?mail|mail)\w*\b", "growth-or-device-farm"),
    (r"\bcommand and control\b|\bc2 (server|framework)\b|\bwhatsapp (rest )?api\b|\bunofficial (whatsapp|instagram|tiktok) api\b", "scraper-or-shady"),
    (r"\b(cloudron|yunohost|umbrel|casaos|unraid|truenas)(\.io)? app (package|template)\b|\bapp package for\b", "packaging-of-other-app"),
    (r"\b(rest )?api (around|wrapping)\b|\bwrapper (around|for)\b|\b(sso|auth|authentication|oauth2?) (library )?for (go|golang|python|node(\.js)?|rust|java|php|react)\b|\bapi for node(\.js)?\b", "component-or-dev-tool"),
    (r"\badult (tube|site|content|video)s?\b|\bporn\w*\b|\bnsfw\b|\bhentai\b", "adult-content"),
    (r"\b(library|sdk|framework)\s+for\s+(production\s+)?(llm|ai|agents?|python|typescript|javascript|node(\.js)?|go|rust|react|vue)\b", "library-or-sdk"),
    (r"\bagent (runtime|harness|sandbox|stack|infrastructure|framework)\b|\bdocker sandbox\b|\b[\d,]+\+? tool integrations\b|\bmemory (layer|system|api|store) for (ai )?agents?\b", "agent-infrastructure"),
    (r"\bmulti[- ]account|\baccounts? (manager|management|farm)|\bauto(matic)? ?(sign[- ]?in|check[- ]?in)|签到|\bfree[- ]tier (farm|abuse)", "account-farming"),
    (r"\b(gamma exposure|options (flow|chain)|stock|stocks|forex|trading|trader|trade journal|broker sync|portfolio tracker for (crypto|stocks))\b", "trading-crypto"),
    (r"\b(serving kit|inference (kit|stack) for|exl[23])\b", "model-serving-kit"),
    (r"\b(sidecar|addons?|add-ons?)\b", "addon-or-companion"),
    (r"\b(device|phone|iphone) farm\b|\bfarm of (real )?(iphones|phones|devices)\b|\btraffic distribution system\b|\blead[- ]gen|\blead (sourcing|generation|scraping|enrichment)\b|\bclay\.com\b|\bfinds? (the )?people\b|\bcold (email|outreach)|\bgrowth hack", "growth-or-device-farm"),
    (r"\binstall(ation)? (config|configuration|files?) for\b|\bconfig only\b", "packaging-of-other-app"),
    (r"\bone[- ](shot|command|click)\s+(docker\s+)?(install|deploy|setup|self-hosting)\w*\s+(of|for)\b|\b(docker )?self-hosting for the\b|\bdocker (deployment|setup|installer) for\b", "packaging-of-other-app"),
    (r"\b(dashboard|ui|frontend|manager|portal|panel)\s+for\s+(your\s+)?(self-hosted\s+)?[\w.-]+\s+instances?\b", "companion-for-other-app"),
    (r"\bcold[- ]call\w*|\b(power|auto|predictive|progressive)[- ]?dialer\b|\bauto(matically)?[- ]?(view|like|follow)(s|ing|er)?\b", "growth-or-device-farm"),
    (r"\bdownloader for (apple music|spotify|deezer|qobuz|tidal|youtube|soundcloud)\b|\b(music|stream|spotify|apple music|deezer|tidal|qobuz|youtube) ripper\b|\b(spotify|tidal|deezer|qobuz|apple music) (ingest|download\w*|rip\w*)\b|\bdownload\w* (music )?from (spotify|tidal|deezer|qobuz|apple music)\b", "scraper-or-shady"),
    (r"\bscraper\b|\bcrawler\b|\bspam\b|\bmass (dm|mail)|\bbot farm\b|\bcheat\b|\bpiracy\b|\bcrack(ed)?\b|\bwarez\b", "scraper-or-shady"),
]
JUNK_RE = [(re.compile(p, re.I), why) for p, why in JUNK]
WEB_INTENT = re.compile(r"\b(web|webui|web ui|web-based|browser|dashboard|self-?host\w*|homelab|alternative|"
                        r"server|platform|portal|app|application|service|manager|tracker|panel|"
                        r"pwa|interface|ui)\b", re.I)


def _norm_desc(d):
    return re.sub(r"[^a-z0-9]+", " ", (d or "").lower()).strip()


def _age_days(iso, now):
    try:
        return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    except Exception:  # noqa: BLE001
        return 99999


CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


ENGLISH_WORDS = {"the", "and", "for", "with", "your", "you", "to", "of", "that", "from", "is", "an",
                 "on", "it", "this", "by", "or", "into", "without", "all", "every", "a", "in"}
FOREIGN_WORDS = {"der", "die", "das", "und", "ohne", "mit", "für", "als", "ein", "eine", "nicht", "zu",
                 "von", "auf", "bei", "de", "e", "para", "com", "sem", "seu", "sua", "um", "uma", "não",
                 "y", "el", "los", "las", "con", "por", "del", "le", "les", "et", "pour", "avec", "sans",
                 "des", "du", "une", "est", "il", "di", "che", "gli", "och", "med", "för", "og", "er"}
ACCENTED = re.compile(r"[ãõçáíóúâêôàüößäñœ]")


def readable(desc):
    letters = [c for c in desc if c.isalpha()]
    if len(letters) < 20 or len(CJK.findall(desc)) > 8:
        return False
    words = set(re.findall(r"[a-zãõçáéíóúâêôàüößäñœ]+", desc.lower()))
    foreign = len(words & FOREIGN_WORDS) + (1 if ACCENTED.search(desc.lower()) else 0)
    if foreign >= 2 and not words & ENGLISH_WORDS:   # Latin-script but not English
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.6


# Well-known apps: a description that is *about* one of these (and not an alternative to
# it) is an add-on / sync tool / client for someone else's product, not a standalone app.
HOST_APPS = re.compile(r"\b(jellyfin|plex|emby|navidrome|subsonic|immich|nextcloud|paperless(-ngx)?|"
                       r"karakeep|hoarder|sonarr|radarr|lidarr|prowlarr|readarr|seerr|overseerr|"
                       r"jellyseerr|stremio|home assistant|mastodon|lemmy|chatwoot|audiobookshelf|"
                       r"calibre-web|kavita|komga|obsidian|notion|trilium|linkwarden|vaultwarden|"
                       r"authentik|pi-hole|adguard|unraid|truenas|proxmox|portainer|n8n|firefly(?: iii)?|"
                       r"coolify|headscale|twenty crm|firecrawl|frigate|ghost|wordpress|photoprism|lancache|"
                       r"bazarr|qbittorrent|transmission|deluge|maintainerr|tautulli|rustdesk|asterisk|"
                       r"freeswitch|invoice ninja|linkding|actual budget|unifi|hermes agent|instapaper)\b", re.I)
# Companion tools for the *arr stack and torrent clients, whatever their topics say.
ARR_TOOLS = re.compile(r"\b(sonarr|radarr|lidarr|prowlarr|readarr|bazarr|qbittorrent|transmission|deluge|"
                       r"maintainerr|tautulli|overseerr|jellyseerr)\b", re.I)
# Alternatives say so ("Jellyfin alternative", "replaces Plex", "like Notion"); a bare
# "instead of static values" is not about another app.
ALT_TO = re.compile(r"\b(alternatives?|replacements?|replaces|drop-in)\b|\b(instead of|like) (using )?"
                    + HOST_APPS.pattern[2:], re.I)
# "Works with Nextcloud, Radicale…", "friendly to Obsidian": compatibility, not an add-on.
COMPAT = re.compile(r"\b(works with|compatible with|friendly to|interoperable with|imports? from|migrate from)"
                    r"[^.]{0,60}?" + HOST_APPS.pattern, re.I)
# "Sonarr/Radarr for games": an app modelled on a known one, not an add-on to it.
ANALOGY = re.compile(HOST_APPS.pattern + r"(\s*/\s*[\w-]+)?\s+for\s+(games|books|comics|music|podcasts|"
                     r"recipes|audiobooks|movies|photos|ebooks|manga|anime|papers|notes)\b", re.I)
# "... for every Jellyfin user", "powered by Twenty CRM": built on another app even when the
# repo carries a media-server/alternative topic.
BUILT_ON = re.compile(r"\b(for|on top of|powered by|built on|integrat\w* with|companion to)\s+"
                      r"(every\s+|your\s+|all\s+|self-hosted\s+)?" + HOST_APPS.pattern[2:], re.I)
TERMINAL = re.compile(r"\b(tui|terminal ui|terminal user interface|command[- ]line|cli tool)\b", re.I)


def gate(repo, now, curated=False, light=False):
    """Return None if the repo passes, else a short reject reason.

    light=True runs only the checks that need search-result fields (pre-hydration)."""
    if repo.get("_bad_flags"):
        return "fork/archived/template"
    desc = repo["description"]
    if not desc or len(desc) < 25:
        return "no-description"
    if not readable(desc):
        return "not-english-readable"
    age = _age_days(repo.get("created_at") or "", now)
    floor = MIN_STARS_YOUNG if age < YOUNG_DAYS else MIN_STARS
    if repo["stargazers_count"] < floor:
        return "too-few-stars"
    if _age_days(repo.get("pushed_at") or "", now) > MAX_PUSH_AGE_DAYS:
        return "stale"
    blob = f"{repo['full_name'].split('/')[-1].replace('-', ' ')} {desc}".lower()
    for rx, why in JUNK_RE:
        if rx.search(blob):
            return why
    if re.match(r"(an? |the )?(mcp|model context protocol) server\b", desc, re.I):
        return "mcp-server"
    if re.match(r"(asp\.net( core)?|laravel|django|rails|react|vue|angular|express|flask|fastapi|spring boot)\s*[-–—:]",
                desc, re.I):
        return "library-or-sdk"   # "ASP.NET Core - helper for …": a framework add-on
    if not ALT_TO.search(desc) and not ANALOGY.search(desc) and (
            BUILT_ON.search(desc) or (HOST_APPS.search(desc) and not COMPAT.search(desc) and not
                                      {t.lower() for t in repo["topics"]} & {"media-server", "alternative"})):
        return "built-on-other-app"
    if ARR_TOOLS.search(desc) and not ALT_TO.search(desc) and not ANALOGY.search(desc):
        return "plugin-for-other-app"
    if TERMINAL.search(desc) and not re.search(r"\bweb\b", desc, re.I):
        return "terminal-app"
    base = v16.hard_reject(repo)
    if base:
        return base
    if light:
        return None
    if not (repo["compose"] or repo["dockerfile"] or curated):
        return "no-deploy-proof"
    topics = {t.lower() for t in repo["topics"]}
    if not (topics & (SELFHOST_TOPICS | WEBAPP_TOPICS) or WEB_INTENT.search(desc) or curated):
        return "no-web-app-intent"
    return None


def interest_score(repo, now, curated=None):
    curated = curated or set()
    stars = repo["stargazers_count"]
    age_mo = max(1.0, _age_days(repo.get("created_at") or "", now) / 30.4)
    velocity = stars / age_mo
    push_age = _age_days(repo.get("pushed_at") or "", now)
    rel_age = _age_days(repo.get("latest_release") or "", now)
    desc = repo["description"].lower()
    topics = {t.lower() for t in repo["topics"]}
    b = {
        "stars": 10 * math.log10(stars + 1),
        "velocity": min(12.0, 4 * math.log2(1 + velocity / 10)),
        "deploy": 6 if repo["compose"] else (3 if repo["dockerfile"] else 0),
        "release": (3 if repo["releases"] else 0) + (2 if rel_age <= 90 else 0),
        "polish": (3 if repo["og_image"] else 0)
                  + (3 if repo["homepage"] and "github" not in repo["homepage"] else 0),
        "org": 2 if repo.get("owner_type") == "Organization" else 0,
        "license": 2 if (repo["license"] or {}).get("spdx_id") not in (None, "NOASSERTION") else -4,
        "active": 3 if push_age <= 30 else (1 if push_age <= 90 else 0),
        "curated": (5 if "awesome" in curated else 0) + (4 if "trendshift" in curated else 0),
        "selfhost": 2 if topics & SELFHOST_TOPICS else 0,
        "life": 3 if any(w in desc or w.replace(" ", "-") in topics for w in LIFE_FAMILY_WORDS) else 0,
        "ai": -6 if re.search(r"\b(ai|llm|gpt|agent|agents|rag|chatbot|openai|claude)\b", desc) else 0,
    }
    return round(sum(b.values()), 2), {k: round(v, 1) for k, v in b.items() if v}


CATEGORY_OVERRIDES = [
    (r"\b(crm|help ?desk|support desk|ticketing|customer (service|support))\b", "CRM / Business"),
    (r"\b(epub|ebooks?|ebook reader|audiobooks?|book library|fb2|opds|calibre)\b", "Books / Reading / Library"),
    (r"\b(radio (server|station)|internet radio|music player|podcasts?)\b", "Media / Streaming"),
    (r"\b(markdown notes|note-taking|notes app|knowledge base|wiki)\b", "Notes / Knowledge"),
    (r"\b(dashboard (application|app) for your|dashboard for your (containers|services|apps|homelab))\b", "Dashboard / Homelab"),
    (r"\b(photography|portfolio|photo (browser|management|manager)|stl|3mf|3d models?)\b", "Image / Design / Creative"),
    (r"\b(document management|paperless)\b", "Documents / PDF / Paperless"),
    (r"\b(period|menstrual|fertility|cycle tracking|intimacy tracking)\b", "Health / Food / Fitness"),
    (r"\b(mailing lists?|newsletters?|transactional e-?mails?|e-?mail (platform|client|sequencer))\b", "Communication / Social"),
    (r"\b(status (portal|page)|uptime)\b", "Monitoring / Observability"),
    (r"\b(news (inbox|reader|dashboard)|feed reader|rss)\b", "Notes / Knowledge"),
    (r"\b(points[- ]and[- ]rewards|family (points|rewards))\b", "Productivity / Tasks"),
    (r"\b((homelab|personal|start|home) (dashboard|page)|homepage)\b", "Dashboard / Homelab"),
    (r"\b(dictionary|vocabulary|language learning)\b", "Education / Learning"),
    (r"\b(day planner|planner pwa|to-?do lists?)\b", "Productivity / Tasks"),
    (r"\b(backups?|family (digital )?safe|file (storage|sharing)|dropbox)\b", "Files / Storage / Backup"),
    (r"\b(observability|uptime|status page|speedtests?|netflow|error tracking|telemetry|monitoring)\b", "Monitoring / Observability"),
    (r"\b(forum|community platform|chat rooms?|messaging|webrtc)\b", "Communication / Social"),
    (r"\b(learning platform|courses?|flashcards|exams?|quiz\w*|lms)\b", "Education / Learning"),
    (r"\b(cookie consent|consent management|gdpr|privacy (checkup|dashboard))\b", "Privacy / Ad-Blocking"),
    (r"\b(baas|backend as a service|supabase alternative|firebase alternative)\b", "Developer Tools / Utilities"),
    (r"\b(app distribution|paas|ci/cd)\b", "DevOps / Infra"),
    (r"\b(3d print\w*|filament)\b", "Home Automation / IoT"),
    (r"\b(virtual tabletop|vtt|tabletop|ttrpg|board games?|dungeons?|arcade|retro games?|game (library|saves?|servers?))\b", "Gaming / Game Servers"),
    (r"\b(osint|threat intel\w*|siem|sigma detection|red team)\b", "Security / Auth"),
    (r"\b(crm|help ?desk|support desk|ticketing)\b", "CRM / Business"),
    (r"\b(fitness|workout|strength[- ]training|sleep tracking|fitbit|habit tracker|health tracker|wellness|nutrition|longevity)\b", "Health / Food / Fitness"),
    (r"\b(time[- ]tracking|time tracker|timesheets?|to-?do|task manager|pomodoro|vehicle|car maintenance|home inventory|household)\b", "Productivity / Tasks"),
    (r"\b(voice assistant|replacement for siri|alexa|smart[- ]home|home automation)\b", "Home Automation / IoT"),
    (r"\b(blog|blogging|cms|website builder|publishing platform|static site)\b", "CMS / Website"),
    (r"\b(pdf|slides|presentations?|spreadsheets?|office suite|pptx|docx|word processor|ocr)\b", "Documents / PDF / Paperless"),
    (r"\b(web analytics|product analytics|analytics)\b", "Analytics / Data"),
    (r"\b(gps|gpx|maps?|geospatial|location|travel|trip planner|road ?trips?|itinerar\w+)\b", "Maps / GIS / Location"),
    (r"\b(photo|photos|gallery|image hosting)\b", "Image / Design / Creative"),
    (r"\b(recipe|recipes|meal|grocery|pantry)\b", "Health / Food / Fitness"),
    (r"\b(budget|finance|expense|invoice|accounting|erp)\b", "Finance / Budget"),
    (r"\b(bookmark|read[- ]later|rss|feed reader)\b", "Notes / Knowledge"),
    (r"\b(ebook|ebooks|kobo|kindle|audiobook|library of books)\b", "Books / Reading / Library"),
    (r"\b(music|podcast|video|streaming|movies|tv shows)\b", "Media / Streaming"),
    (r"\b(translation|speech|transcri\w+|llm|agent|agents)\b", "AI / LLM"),
]
CATEGORY_OVERRIDES = [(re.compile(p, re.I), c) for p, c in CATEGORY_OVERRIDES]


def categorize(repo):
    """Description-first overrides for families v16's keyword categorizer misfiles.
    The override matching earliest in the description wins (descriptions lead with what the
    app is; a feature list later shouldn't decide); list order breaks ties."""
    best = None
    for order, (rx, cat) in enumerate(CATEGORY_OVERRIDES):
        m = rx.search(repo["description"])
        if m and (best is None or (m.start(), order) < best[:2]):
            best = (m.start(), order, cat)
    return best[2] if best else v16.categorize(repo)


AI_APP = re.compile(r"\b(web ?ui|webui|web app|web-based|web interface|dashboard|workspace|interface|"
                    r"browser[- ]based|in (the|your) browser|browser workbench|frontend|panel|portal|"
                    r"chat ui|chat app|chat client|chatgpt clone|clone|ui|web application|pwa|"
                    r"workstation|alternative|web chat|editor|studio|"
                    r"self-host\w* (app|workspace))\b", re.I)


def is_ai(repo):
    return bool(re.search(r"\b(ai|llm|gpt|agent|agents|rag|chatbot|openai|claude|mcp)\b",
                          repo["description"].lower())) or repo["_cat"] == "AI / LLM"


# --- caches -------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    tmp.replace(path)


# --- rename / transfer dedupe --------------------------------------------------
def rename_duplicates(cands, used_urls, used_by_name):
    """Return set of candidate keys that are renamed/transferred copies of used repos."""
    cache = load_json(RENAME_CACHE, {})
    todo = set()
    for c in cands:
        short = c["full_name"].split("/")[-1].lower()
        for old in used_by_name.get(short, []):
            if old not in cache:
                todo.add(old)
    todo = sorted(todo)[:400]  # REST lookups are cached, so later runs only resolve new names

    def resolve(old):
        try:
            r = v16.native_gh_api(f"/repos/{old}")
            return old, (r.get("full_name") or "").lower()
        except Exception:  # noqa: BLE001 — 404 = deleted; keep the old name
            return old, old

    with ThreadPoolExecutor(WORKERS) as ex:
        for old, new in ex.map(resolve, todo):
            cache[old] = new
    save_json(RENAME_CACHE, cache)
    resolved_used = {cache.get(o, o) for olds in used_by_name.values() for o in olds if o in cache}
    dups = set()
    for c in cands:
        k = c["full_name"].lower()
        if k in resolved_used:
            dups.add(k)
    return dups


# --- selection -------------------------------------------------------------------
def select(cands, target):
    """Pass 1: score order, one per owner, category cap, AI <= AI_CAP.
    Pass 2: non-AI past the category cap. Pass 3: more AI, up to AI_FILL_CAP.
    AI repos are only ever picked when the description shows a web UI / app (AI_APP)."""
    picked, owners, cats, ai = [], set(), {}, 0
    ranked = sorted(cands, key=lambda x: -x["_score"])
    for c in ranked:
        owner = c["full_name"].split("/")[0].lower()
        cat = c["_cat"]
        if owner in owners or cats.get(cat, 0) >= CAT_CAP:
            continue
        if is_ai(c):
            if ai >= AI_CAP or not AI_APP.search(c["description"]):
                continue
            ai += 1
        picked.append(c)
        owners.add(owner)
        cats[cat] = cats.get(cat, 0) + 1
        if len(picked) >= target:
            break
    taken = {c["full_name"] for c in picked}
    for allow_ai in (False, True):
        if len(picked) >= target:
            break
        for c in ranked:
            owner = c["full_name"].split("/")[0].lower()
            if c["full_name"] in taken or owner in owners:
                continue
            if is_ai(c) != allow_ai:
                continue
            if allow_ai and (ai >= AI_FILL_CAP or not AI_APP.search(c["description"])):
                continue
            ai += allow_ai
            picked.append(c)
            taken.add(c["full_name"])
            owners.add(owner)
            if len(picked) >= target:
                break
    return sorted(picked, key=lambda x: -x["_score"])


def set_star_floor(floor, young):
    global MIN_STARS, MIN_STARS_YOUNG
    MIN_STARS, MIN_STARS_YOUNG = floor, young


def ladder_start():
    """Rung to start from: a lower rung's results include every higher rung's repos (same
    searches, lower floor, ranked by stars), so skipping rungs that recently came up short
    loses nothing and saves a full discovery pass each."""
    st = load_json(LADDER_STATE, {})
    if time.time() - st.get("t", 0) < LADDER_MEMORY_HOURS * 3600:
        return max(0, min(int(st.get("rung", 0)), len(STAR_LADDER) - 1))
    return 0


def ladder_record(rung):
    save_json(LADDER_STATE, {"rung": rung, "t": int(time.time())})


def discover_ladder(tracker, target, now, args, enough=None):
    """discover() on each STAR_LADDER rung until `enough(picked)` (default: a full set).
    Leaves MIN_STARS/MIN_STARS_YOUNG at the rung that produced the returned picks."""
    enough = enough or (lambda p: len(p) >= target)
    start = ladder_start()
    if start:
        print(f"[{VERSION}] star ladder: starting at ≥{STAR_LADDER[start][0]}★ (higher rungs came up short "
              f"in the last {LADDER_MEMORY_HOURS} h)", flush=True)
    for i in range(start, len(STAR_LADDER)):
        floor, young = STAR_LADDER[i]
        set_star_floor(floor, young)
        picked, stats = discover(tracker, target, now, args)
        stats["starFloor"] = [floor, young]
        if enough(picked) or i == len(STAR_LADDER) - 1:
            ladder_record(i)
            return picked, stats
        nxt = STAR_LADDER[i + 1]
        print(f"[{VERSION}] {len(picked)}/{target} at ≥{floor}★ (≥{young}★ if under {YOUNG_DAYS} days); "
              f"retrying one rung lower at ≥{nxt[0]}★ (≥{nxt[1]}★) …", flush=True)


def discover(tracker, target, now, args):
    t0 = time.time()
    used_urls = {u.lower().rstrip("/") for u in tracker.get("usedRepoUrls", [])}
    for p in tracker.get("completedPages", []):
        used_urls.update(u.lower().rstrip("/") for u in p.get("repos", []))
    used_full = {u.split("github.com/")[-1] for u in used_urls if "github.com/" in u}
    used_by_name = {}
    for fn in used_full:
        used_by_name.setdefault(fn.split("/")[-1], []).append(fn)

    # Re-uploads / mirrors of already-published projects carry the identical description.
    used_desc = set()
    try:
        with open(v1.MASTER_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = _norm_desc(row.get("description"))
                if len(d) >= 20:
                    used_desc.add(d)
    except OSError:
        pass

    now_ts = int(time.time())
    rejects = {k: v for k, v in load_json(REJECT_CACHE, {}).items()
               if now_ts - v.get("t", 0) < REJECT_TTL_DAYS * 86400}
    mem = load_json(QUERY_MEM, {})
    stats = {"slices": 0, "slicesSkipped": 0, "seen": 0, "unused": 0, "rejectReasons": {}}

    # 1) GraphQL sliced search, adaptive
    plan = build_query_plan(now)
    live = []
    for item in plan:
        m = mem.get(item["q"].split(" pushed:")[0], {})
        dead = m.get("zeroRuns", 0) >= 2 and now_ts - m.get("t", 0) < SKIP_DEAD_SLICE_DAYS * 86400
        if dead and not item["recent"]:
            stats["slicesSkipped"] += 1
            continue
        live.append(item)
    if args.quick:
        live = live[: max(10, len(live) // 3)]
    print(f"[{VERSION}] GraphQL sliced discovery: {len(live)} slices "
          f"({stats['slicesSkipped']} dead slices skipped by query memory), {WORKERS}-way parallel …",
          flush=True)

    pool, slice_new = {}, {}

    def run_slice(item):
        return item, search_slice(item["q"], pages=3 if item["recent"] else 1)

    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (item, repos) in enumerate(ex.map(run_slice, live), 1):
            stats["slices"] += 1
            fresh = 0
            for r in repos:
                k = r["full_name"].lower()
                stats["seen"] += 1
                if k in used_full or k in rejects:
                    continue
                fresh += 1  # counts overlap with other slices: a slice is only dead if it finds nothing usable
                pool.setdefault(k, r)
            slice_new[item["q"].split(" pushed:")[0]] = fresh
            if i % 20 == 0:
                print(f"[{VERSION}]   {i}/{len(live)} slices · {len(pool)} unused candidates · "
                      f"{time.time() - t0:.0f}s", flush=True)

    # 2) curated streams: Trendshift (cached scrape) + carry pool, hydrated with REAL data
    #    (curated repos are hydrated up-front: no search fields to pre-gate them on)
    curated = {}
    extra = []
    try:
        ts = v16.load_or_scrape_trendshift(now_ts, use_cache=True)
        for meta in (ts or {}).values():
            fn = meta["full_name"]
            curated.setdefault(fn.lower(), set()).add("trendshift")
            extra.append(fn)
    except Exception as exc:  # noqa: BLE001
        print(f"[{VERSION}] trendshift unavailable ({exc}); continuing")
    try:
        aw = v16.load_awesome_index(now_ts, True)
        aw_names = aw if isinstance(aw, (set, list)) else (aw or {}).keys()
        for u in aw_names:
            fn = str(u).lower().split("github.com/")[-1].strip("/")
            if fn.count("/") == 1:
                curated.setdefault(fn, set()).add("awesome")
    except Exception:  # noqa: BLE001
        pass
    carry = {k: v for k, v in load_json(CARRY_CACHE, {}).items()
             if now_ts - v.get("t", 0) < CARRY_TTL_DAYS * 86400}
    extra += [v["full_name"] for v in carry.values()]
    extra = [fn for fn in dict.fromkeys(extra)
             if fn.lower() not in used_full and fn.lower() not in pool and fn.lower() not in rejects]
    if extra:
        hyd = hydrate(extra)
        pool.update({k: v for k, v in hyd.items() if k not in used_full})
        print(f"[{VERSION}] curated/carry stream: hydrated {len(hyd)} of {len(extra)} "
              f"(Trendshift + carry pool) with real metadata", flush=True)
    stats["unused"] = len(pool)

    # 3) cheap gate on light fields, then hydrate ONLY survivors with deploy-proof fields
    def reject(k, why):
        stats["rejectReasons"][why] = stats["rejectReasons"].get(why, 0) + 1
        if why not in ("too-few-stars", "stale"):  # those can change; don't cache
            new_rejects[k] = {"why": why, "t": now_ts}

    new_rejects, survivors = {}, []
    for k, r in pool.items():
        why = gate(r, now, curated=bool(curated.get(k, set()) & {"awesome"}), light=True)
        if why:
            reject(k, why)
        else:
            survivors.append(k)
    need = [pool[k]["full_name"] for k in survivors if not pool[k].get("_hydrated")]
    print(f"[{VERSION}] cheap gate: {len(survivors)} of {len(pool)} survive; hydrating "
          f"{len(need)} with deploy-proof fields …", flush=True)
    hyd = hydrate(need)
    for k in survivors:
        if k in hyd:
            pool[k] = hyd[k]

    # 4) full gate + score
    passing = []
    for k in survivors:
        r = pool[k]
        if not r.get("_hydrated"):
            reject(k, "hydrate-failed")
            continue
        cur = curated.get(k, set())
        why = gate(r, now, curated=bool(cur & {"awesome"}))
        if not why and _norm_desc(r["description"]) in used_desc:
            why = "same-description-as-published"
        if why:
            reject(k, why)
            continue
        r["_score"], r["_bd"] = interest_score(r, now, cur)
        r["_cat"] = categorize(r)
        passing.append(r)
    print(f"[{VERSION}] gate: {len(passing)} pass of {len(pool)} unused "
          f"(seen {stats['seen']}) · top rejects: "
          + ", ".join(f"{k}={v}" for k, v in sorted(stats["rejectReasons"].items(),
                                                     key=lambda kv: -kv[1])[:8]), flush=True)

    # 4) rename/transfer dedupe on the plausible shortlist only
    shortlist = sorted(passing, key=lambda x: -x["_score"])[: target * 3]
    dups = rename_duplicates(shortlist, used_urls, used_by_name)
    if dups:
        print(f"[{VERSION}] rename-dedupe: dropped {len(dups)} renamed/transferred repos "
              f"already published ({', '.join(sorted(dups)[:5])}{'…' if len(dups) > 5 else ''})")
    passing = [r for r in passing if r["full_name"].lower() not in dups]

    picked = select(passing, target)

    # 5) persist memory, carry, rejects
    for q, n in slice_new.items():
        prev = mem.get(q, {})
        mem[q] = {"t": now_ts, "new": n, "zeroRuns": (prev.get("zeroRuns", 0) + 1) if n == 0 else 0}
    # Date-windowed slice keys change daily; forget anything not seen for two skip periods.
    mem = {q: m for q, m in mem.items() if now_ts - m.get("t", 0) < 2 * SKIP_DEAD_SLICE_DAYS * 86400}
    save_json(QUERY_MEM, mem)
    rejects.update(new_rejects)
    save_json(REJECT_CACHE, rejects)
    picked_keys = {r["full_name"].lower() for r in picked}
    new_carry = {r["full_name"].lower(): {"full_name": r["full_name"], "t": now_ts, "score": r["_score"]}
                 for r in passing if r["full_name"].lower() not in picked_keys}
    carry.update(new_carry)
    for k in picked_keys:
        carry.pop(k, None)
    if not args.dry_run or args.save_caches:
        save_json(CARRY_CACHE, carry)

    stats.update({"passing": len(passing), "renameDups": len(dups),
                  "gqlCalls": _gql_stats["calls"], "gqlErrors": _gql_stats["errors"],
                  "gqlRemaining": _gql_stats["remaining"], "carrySize": len(carry),
                  "seconds": round(time.time() - t0, 1)})
    return picked, stats


# --- Notion page -------------------------------------------------------------------
def _fmt_k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def page_blocks(set_num, picked, stats):
    dist = {}
    for r in picked:
        dist[r["_cat"]] = dist.get(r["_cat"], 0) + 1
    dist_line = " · ".join(f"{k} ({v})" for k, v in sorted(dist.items(), key=lambda kv: -kv[1]))
    compose_n = sum(1 for r in picked if r["compose"])
    docker_n = sum(1 for r in picked if r["compose"] or r["dockerfile"])
    ai_n = sum(1 for r in picked if is_ai(r))
    stars_med = sorted(r["stargazers_count"] for r in picked)[len(picked) // 2] if picked else 0
    proof = (f"Every pick ships a Dockerfile or compose file ({compose_n} with compose)"
             if docker_n == len(picked) else
             f"{docker_n} of {len(picked)} ship a Dockerfile or compose file ({compose_n} with compose) "
             "and the rest are listed on awesome-selfhosted")
    blocks = [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [v1.rt(
            f"Set {set_num}: {len(picked)} self-hosted open-source web apps (v17 smart curation). "
            f"{proof}. Each has at least {MIN_STARS_YOUNG} real stars if under {YOUNG_DAYS} days "
            f"old, otherwise {MIN_STARS} (median ★ {_fmt_k(stars_med)}), was pushed in the last "
            f"{MAX_PUSH_AGE_DAYS // 30} months, and passed a junk gate (no plugins or add-ons for "
            "other apps, clients, Helm charts, bots, game-server wrappers or AI-account proxies). "
            f"One repo per owner; {ai_n} AI {'pick' if ai_n == 1 else 'picks'}, each with a web UI. "
            "Ranked by interest: popularity, momentum (★/month), releases and polish. "
            f"Screened {stats['unused']} unused candidates from {stats['seen']} search hits.")]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            v1.rt("Category mix: ", bold=True), v1.rt(dist_line or "—")]}},
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [v1.rt(f"Top {len(picked)} repos")]}},
    ]
    now = datetime.now(timezone.utc)
    for r in picked:
        age_mo = max(1.0, _age_days(r["created_at"] or "", now) / 30.4)
        vel = r["stargazers_count"] / age_mo
        flags = (" 🐳" if r["compose"] or r["dockerfile"] else "") + (" 🏷️" if r["releases"] else "")
        home = f" · {r['homepage']}" if r["homepage"] and "github.com" not in r["homepage"] else ""
        desc = r["description"] if len(r["description"]) <= 340 else r["description"][:339].rstrip() + "…"
        details = (
            f" — [{r['_cat']}]{flags} {r.get('language') or 'Unknown'} — {desc} "
            f"(★ {r['stargazers_count']:,} · ~{vel:.0f}★/mo · updated {(r['pushed_at'] or '')[:10]}"
            f" · interest {r['_score']:.0f}{home}; topics: {', '.join(r['topics'][:6]) or '—'})"
        )
        blocks.append({"object": "block", "type": "numbered_list_item",
                       "numbered_list_item": {"rich_text": [
                           v1.rt(r["full_name"], link=r["html_url"], bold=True),
                           v1.rt(details)]}})
    return blocks


def append_master_csv(set_num, picked):
    new_file = not v1.MASTER_CSV.exists()
    with open(v1.MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(v1.MASTER_CSV_HEADER)
        for r in picked:
            w.writerow([set_num, r["full_name"], r["_cat"], r["html_url"],
                        r["description"].replace("\n", " ").replace("\r", " ")])


# --- self-test -------------------------------------------------------------------------
def self_test():
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)

    def mk(name, desc, stars=120, compose=True, topics=("self-hosted",), created="2025-06-01T00:00:00Z"):
        return {"full_name": name, "html_url": f"https://github.com/{name}", "description": desc,
                "topics": list(topics), "language": "Go", "stargazers_count": stars,
                "forks_count": 5, "created_at": created, "pushed_at": "2026-09-10T00:00:00Z",
                "homepage": "", "license": {"spdx_id": "MIT"}, "owner_type": "User",
                "og_image": False, "releases": 3, "latest_release": "2026-09-01T00:00:00Z",
                "compose": compose, "dockerfile": False, "_bad_flags": False, "_hydrated": True}

    good = mk("a/recipes", "Self-hosted recipe manager and meal planner with a clean web UI")
    cases = [
        (good, None),
        (mk("b/helm-x", "A Helm chart to self-host Stoat Chat"), "infra-packaging"),
        (mk("c/jc", "One plugin to rule them all — plugin for Jellyfin requests"), "plugin-for-other-app"),
        (mk("d/vici", "Music player for Navidrome and Subsonic servers — Android and Windows"), "client-for-other-app"),
        (mk("e/pal", "Palworld dedicated server manager web dashboard"), "game-server-wrapper"),
        (mk("f/stats", "Beautiful GitHub stats cards for your README"), "github-vanity"),
        (mk("g/pool", "Self-hosted multi-account Codex account pool gateway"), "ai-account-proxy"),
        (mk("h/tiny", "Self-hosted recipe manager with a clean web UI", stars=4), "too-few-stars"),
        (mk("i/zh", "自托管的网盘推广员全自动化收益与管理平台，支持多种网盘"), "not-english-readable"),
        (mk("j/nodock", "Self-hosted recipe manager with a clean web UI", compose=False), "no-deploy-proof"),
        (mk("k/young", "Self-hosted habit tracker web app for families", stars=12,
            created="2026-08-01T00:00:00Z"), None),
        (mk("l/pool", "A lightweight, self-hosted AI subscription pool for teams."), "ai-account-proxy"),
        (mk("m/map", "Web map for Satisfactory dedicated servers: live map and power"), "game-server-wrapper"),
        (mk("n/tj", "The open-source trade journal. Broker sync, P&L calendar and analytics"), "trading-crypto"),
        (mk("o/kit", "GLM Flash EXL3 on two DGX boxes — production serving kit"), "model-serving-kit"),
        (mk("q/jd", "Personalised recommendation libraries for every Jellyfin user", topics=("media-server",)), "built-on-other-app"),
        (mk("r/cp", "A self-hosted client portal powered by Twenty CRM"), "built-on-other-app"),
        (mk("s/fc", "A beautiful monitoring dashboard for self-hosted Firecrawl instances"), "companion-for-other-app"),
        (mk("t/fx", "One-command Docker self-hosting for the Fluxer open source chat platform"), "packaging-of-other-app"),
        (mk("u/s5", "Fast, lightweight SOCKS5 server in Rust — TUI dashboard, headless mode"), "terminal-app"),
        (mk("v/jf", "Self-hosted Jellyfin alternative: stream your movies from a web UI"), None),
        # legitimate apps the gate must keep (false-positive guards)
        (mk("w/mon", "Self-hosted monitoring dashboard for Linux servers with alerts"), None),
        (mk("w/one", "Self-hosted recipe manager, one-command install with Docker; web UI"), None),
        (mk("w/pbx", "Self-hosted phone system web app with SIP dialer and voicemail"), None),
        (mk("w/cd", "Self-hosted CD ripper and music library web app"), None),
        (mk("w/llm", "Self-hosted LLM chat web UI that supports GGUF models"), None),
        (mk("w/chess", "Self-hosted chess server web app with dedicated servers support"), None),
        (mk("x/hitl", "Open source human-in-the-loop library for production LLM agents with a web dashboard"), "library-or-sdk"),
        (mk("x/eve", "The whole stack on your machine: a Docker sandbox, memory, 1,070 tool integrations, and a dashboard"), "agent-infrastructure"),
        (mk("w/lib", "Self-hosted library for your ebooks and comics with a web reader"), None),
        (mk("y/epg", "A ready to use plug-and-forget Docker Compose to download EPG with cron"), "packaging-of-other-app"),
        (mk("y/php", "Docker-environment for web-development on PHP"), "packaging-of-other-app"),
        (mk("y/graf", "Docker Compose monitoring stack for Docker hosts: Prometheus, Grafana, Alertmanager"), "packaging-of-other-app"),
        (mk("y/arr", "Multi-language audio enforcement for the *arr media stack"), "plugin-for-other-app"),
        (mk("y/vpn", "Open-source VPN clients for Android and Windows, self-hosted panel"), "native-client"),
        (mk("z/retro", "Game library manager with indexer search and download automation. Sonarr/Radarr for games."), None),
        (mk("z/takt", "Self-hosted kanban board with flow metrics. Docker, systemd or Helm; runs air-gapped."), None),
        (mk("z/mh", "A self-hosted model library web app. Includes an MCP server and scoped keys for agentic use."), None),
        (mk("z/kt", "A companion web app for groups playing a tabletop adventure path"), None),
        (mk("z/mcp", "An MCP server for searching your notes from any AI client"), "mcp-server"),
        (mk("q/seance", "A self-hostable contact form for Ghost"), "built-on-other-app"),
        (mk("q/perga", "Backend API for Perga - a personal workspace for daily planning"), "component-or-dev-tool"),
        (mk("q/sdk", "Open-source SDK generator for OpenAPI 3: idiomatic Rust and TypeScript"), "component-or-dev-tool"),
        (mk("q/c2", "Open-source, self-hosted command and control for fleets of drones"), "scraper-or-shady"),
        (mk("r/lucid", "Self-hostable web client for your CalDAV calendars. Works with Nextcloud, Radicale, iCloud."), None),
        (mk("r/nex", "Notes as Markdown files in the browser, with a graph. Self-hosted, friendly to Obsidian."), None),
        (mk("r/tube", "Free, self-hosted adult tube site CMS (PHP 8.2+)"), "adult-content"),
        (mk("s/cloudron", "Cloudron.io app package for self-hosting a Nostr relay, with backups"), "packaging-of-other-app"),
        (mk("s/scipio", "Self-hosted REST API around israeli-bank-scrapers: fetch transactions"), "component-or-dev-tool"),
        (mk("s/wa", "Free, self-hosted WhatsApp REST API for Node.js. Send text and media"), "scraper-or-shady"),
        (mk("s/bn", "Read-only Binance Pay payment checker with a web dashboard"), "trading-crypto"),
        (mk("s/sso", "Self-hosted Enterprise SSO for Go — one OAuth2-style flow in your app"), "component-or-dev-tool"),
        (mk("t/pp", "An unofficial companion tool created for use alongside PhotoPrism to enable API endpoints"), "companion-for-other-app"),
        (mk("t/cal", "Auto-tunes Sonarr/Radarr quality-tier sizes from your library", topics=("media-server",)), "plugin-for-other-app"),
        (mk("t/qb", "Find disk files not tracked by any qBittorrent torrent and reclaim storage"), "built-on-other-app"),
        (mk("t/fs", "Self-hosted Telegram customer service bot: routes each chat into a forum"), "chat-bot"),
        (mk("t/wow", "A web-based management panel for AzerothCore WoW private servers"), "game-server-wrapper"),
        (mk("t/play", "Self-hosted game servers on your own VPS, starting with Minecraft"), "game-server-wrapper"),
        (mk("t/arr", "A self-hosted media manager: an alternative to Sonarr and Radarr in one app"), None),
        (mk("t/cal2", "Auto-tunes Sonarr/Radarr sizes from your library, instead of static guide values"), "built-on-other-app"),
        (mk("t/like", "A self-hosted reading app, like Kavita but for comics only"), None),
        (mk("t/resell", "Open-source AI reseller panel with customer API keys and prepaid balance"), "ai-account-proxy"),
        (mk("t/mail", "Send emails without limits. Self-hosted. Built in Go."), "growth-or-device-farm"),
        (mk("u/rd", "Self-hosted RustDesk address book web app with live online status"), "built-on-other-app"),
        (mk("u/ast", "Clean, self-hosted web phone for a single SIP line, built on Asterisk"), "built-on-other-app"),
        (mk("u/lang", "Self-hosted systems and scientific programming language with epistemic types"), "component-or-dev-tool"),
        (mk("u/sdk", "Modular document and spreadsheet engine SDK built in pure Rust"), "component-or-dev-tool"),
        (mk("u/card", "A self-hosted GitHub repository gallery and SVG card service"), "github-vanity"),
        (mk("u/de", "Belege per Drag und Drop als Ausgaben buchen, lokale OCR, ohne Cloud, Docker Web App"), "not-english-readable"),
        (mk("u/pt", "Controle financeiro pessoal e familiar self-hosted: renda, despesas, cartões, metas"), "not-english-readable"),
        (mk("u/en", "Self-hosted minimal time tracking."), None),
        (mk("u/ha", "A self-hosted family dashboard built on Home Assistant API, works with any calendar"), "built-on-other-app"),
        (mk("w/lead", "Open-source, self-hosted alternative to Clay.com for lead sourcing and enrichment"), "growth-or-device-farm"),
        (mk("w/octi", "Browser-based client for Octi: A multi-device manager."), "companion-for-other-app"),
        (mk("w/arm", "Install configuration for self-hosted ArmoryHub. Config only, the image is private"), "packaging-of-other-app"),
        (mk("w/asp", "ASP.NET Core - Transform server-side validations to client-side without JavaScript"), "library-or-sdk"),
        (mk("w/cli", "Command line server and client for webhooks deliveries"), "terminal-app"),
        (mk("w/sync", "Service to sync transactions to Actual Budget"), "built-on-other-app"),
        (mk("v/spot", "Self-hosted personal music server with Spotify ingest"), "scraper-or-shady"),
        (mk("v/alt", "Open-source alternative to Intercom and Linear. Support, projects, CRM", topics=()), None),
        (mk("v/ytdlp", "Production-ready REST API for video downloads and metadata extraction using yt-dlp"), "component-or-dev-tool"),
        (mk("v/gamearr", "The definitive Video Game PVR for the arr ecosystem. Automate metadata"), "plugin-for-other-app"),
        (mk("v/personal", "A status portal made for own personal server hosting different services"), "personal-setup"),
        (mk("p/cjk", "知归是一个面向个人使用的 AI 知识归档工具，把内容链接发送给机器人 GitHub web app"), "not-english-readable"),
    ]
    ok = True
    for repo, want in cases:
        got = gate(repo, now)
        good_case = got == want
        ok &= good_case
        print(f"  {'ok ' if good_case else 'FAIL'} gate {repo['full_name']:<10} -> {got} (want {want})")
    s_big, _ = interest_score(mk("x/big", "Self-hosted photo gallery", stars=5000), now)
    s_small, _ = interest_score(mk("y/small", "Self-hosted photo gallery", stars=25), now)
    t = s_big > s_small
    ok &= t
    print(f"  {'ok ' if t else 'FAIL'} score monotonic in stars ({s_big} > {s_small})")
    cands = []
    for i in range(12):
        r = mk(f"o{i % 3}/r{i}", "Self-hosted web app for notes", stars=100 + i)
        r["_score"], r["_bd"] = interest_score(r, now)
        r["_cat"] = "Notes / Knowledge"
        cands.append(r)
    sel = select(cands, 50)
    t = len(sel) == 3
    ok &= t
    print(f"  {'ok ' if t else 'FAIL'} one repo per owner ({len(sel)} == 3)")
    # AI top-up: 30 non-AI + 40 AI apps (web UI) + 5 AI infra -> 30 + 20 AI, infra never picked
    pool = []
    for i in range(30):
        r = mk(f"n{i}/app", f"Self-hosted recipe manager number {i} with a web UI", stars=500 - i)
        pool.append(r)
    for i in range(40):
        pool.append(mk(f"a{i}/ai", f"Self-hosted AI agent workspace number {i} with a web UI", stars=900 - i))
    for i in range(5):
        pool.append(mk(f"x{i}/ai", f"Persistent memory API for AI agents, backend number {i}", stars=5000))
    for r in pool:
        r["_score"], r["_bd"] = interest_score(r, now)
        r["_cat"] = "AI / LLM" if "AI" in r["description"] else "Health / Food / Fitness"
    sel = select(pool, 50)
    n_ai = sum(1 for r in sel if is_ai(r))
    t = len(sel) == 50 and n_ai == AI_FILL_CAP and not any(r["full_name"].startswith("x") for r in sel)
    ok &= t
    print(f"  {'ok ' if t else 'FAIL'} AI top-up fills 50 with {n_ai} AI (cap {AI_FILL_CAP}); AI without a web UI never picked")
    t = len(build_query_plan(now)) > 60
    ok &= t
    print(f"  {'ok ' if t else 'FAIL'} query plan size {len(build_query_plan(now))}")
    for desc, want in [("Self-hosted, chat-style support desk", "CRM / Business"),
                       ("Self-hosted strength-training analytics with sleep tracking", "Health / Food / Fitness"),
                       ("Self-hosted minimal time tracking.", "Productivity / Tasks"),
                       ("Self-hosted vehicle management and maintenance tracking platform", "Productivity / Tasks"),
                       ("A fully local, self-hosted replacement for Siri and Alexa with smart-home control", "Home Automation / IoT"),
                       ("A modern open-source publishing platform built with Go", "CMS / Website"),
                       ("Self-hosted web slides editor with .pptx round-trip", "Documents / PDF / Paperless"),
                       ("Privacy-first PDF toolkit with browser-based editing and AI summaries", "Documents / PDF / Paperless"),
                       ("Self-hosted community platform in a single Go binary. Chat, forum, WebRTC video rooms", "Communication / Social"),
                       ("Self-hosted, lightweight observability tool for indie developers", "Monitoring / Observability"),
                       ("A streamlined, self-hosted learning platform focused on simplicity", "Education / Learning"),
                       ("Self-hosted automatic backups of your files with a web dashboard", "Files / Storage / Backup"),
                       ("Play classic arcade games online with friends, straight from the browser", "Gaming / Game Servers"),
                       ("Polished self-hostable browser-based EPUB & PDF library", "Books / Reading / Library"),
                       ("Minimalist day planner PWA with a vertical timeline", "Productivity / Tasks"),
                       ("A self-hosted English dictionary with 300 000 entries", "Education / Learning"),
                       ("Combines CRM, project management, invoicing, time tracking and monitoring", "CRM / Business"),
                       ("A modern, self-hosted photography portfolio platform built with React and analytics", "Image / Design / Creative"),
                       ("Privacy-first PWA for period and intimacy tracking", "Health / Food / Fitness"),
                       ("Modern, streamlined, open-source customer service software with monitoring", "CRM / Business"),
                       ("A one-binary, self-hosted mailing list manager", "Communication / Social")]:
        got = categorize(mk("c/c", desc))
        t = got == want
        ok &= t
        print(f"  {'ok ' if t else 'FAIL'} category {desc[:40]!r} -> {got}")
    print("SELF-TEST", "ALL PASSED" if ok else "FAILED")
    return ok


# --- lock / main ----------------------------------------------------------------------
def acquire_lock():
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            if v16._pid_alive(pid):
                raise SystemExit(f"[{VERSION}] another publish is running (pid {pid})")
        except ValueError:
            pass
    LOCKFILE.write_text(str(__import__("os").getpid()))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--quick", action="store_true", help="Run a third of the slices (fast smoke test).")
    p.add_argument("--breakdown", action="store_true", help="Print per-pick score composition.")
    p.add_argument("--save-caches", action="store_true")
    p.add_argument("--target", type=int, default=DEFAULT_TARGET)
    args = p.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    if not v16.GH_TOKEN:
        raise SystemExit(f"[{VERSION}] no GitHub token (gh auth login or GH_TOKEN) — GraphQL needs one.")

    t0 = time.time()
    tracker = json.loads(v1.TRACKER.read_text())
    v1.NOTION_KEY = v1.load_notion_key()
    v1.AUDIT_DIR.mkdir(exist_ok=True)
    for _p in tracker.get("completedPages", []):
        if _p.get("setNum") is None:
            _p["setNum"] = 0
    sync = v1.reconcile_with_notion(tracker)
    print(f"[sync] latest in Notion={sync['liveLatest']} | local-before={sync['localLatestBefore']} "
          f"| authoritative={sync['authoritativeLatest']}", flush=True)
    if sync["changed"] and not args.dry_run:
        v1.write_tracker(tracker)
    set_num = int(tracker["completedSets"]) + 1
    title = TITLE_TEMPLATE.format(n=set_num)
    if title in sync["liveTitles"]:
        raise SystemExit(f"[{VERSION}] title collision: {title} already exists in Notion")
    print(f"[{VERSION}] next: {title} · {len(tracker.get('usedRepoUrls', []))} used URLs", flush=True)

    now = datetime.now(timezone.utc)
    picked, stats = discover_ladder(tracker, args.target, now, args)
    dist = {}
    for r in picked:
        dist[r["_cat"]] = dist.get(r["_cat"], 0) + 1
    scores = [r["_score"] for r in picked] or [0]
    stars = sorted(r["stargazers_count"] for r in picked) or [0]
    print(f"[{VERSION}] selected {len(picked)}/{args.target} ({sum(1 for r in picked if is_ai(r))} AI) | median ★ {stars[len(stars) // 2]} | "
          f"interest min/max {min(scores):.0f}/{max(scores):.0f} | compose "
          f"{sum(r['compose'] for r in picked)} | star floor ≥{MIN_STARS} (≥{MIN_STARS_YOUNG} young) | GraphQL calls {stats['gqlCalls']} "
          f"(errors {stats['gqlErrors']}, budget left {stats['gqlRemaining']}) | {stats['seconds']}s")
    print(f"[{VERSION}] categories: " + ", ".join(f"{k}={v}" for k, v in
                                                 sorted(dist.items(), key=lambda kv: -kv[1])))
    for i, r in enumerate(picked, 1):
        line = f"  {i:>2}. {r['_score']:>5.1f} ★{r['stargazers_count']:>6} [{r['_cat'][:22]:<22}] " \
               f"{r['full_name']:<42} {r['description'][:70]}"
        print(line)
        if args.breakdown:
            print("         " + " ".join(f"{k}{v:+}" for k, v in r["_bd"].items()))

    audit = {"version": VERSION, "setNum": set_num, "title": title, "count": len(picked),
             "stats": stats, "categoryDistribution": dist,
             "picks": [{"repo": r["full_name"], "url": r["html_url"], "score": r["_score"],
                        "stars": r["stargazers_count"], "category": r["_cat"],
                        "description": r["description"], "language": r.get("language"),
                        "compose": r["compose"], "docker": r["compose"] or r["dockerfile"],
                        "ai": is_ai(r), "breakdown": r["_bd"]} for r in picked],
             "aiCount": sum(1 for r in picked if is_ai(r)),
             "publishable": len(picked) == args.target,
             "timingSeconds": round(time.time() - t0, 1)}
    if args.dry_run:
        ap = v1.AUDIT_DIR / f"set{set_num}_{VERSION}_DRYRUN_audit.json"
        ap.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
        print(f"[dry-run] audit -> {ap}")
    if len(picked) != args.target:
        raise SystemExit(f"[{VERSION}] selected {len(picked)}/{args.target} fresh repos; refusing an underfilled or oversized page.")
    if args.dry_run:
        print(f"[dry-run] would publish: {title} ({len(picked)} repos) in {audit['timingSeconds']}s")
        return
    if args.target != DEFAULT_TARGET:
        raise SystemExit(f"[{VERSION}] refusing to publish non-standard --target {args.target}")

    acquire_lock()
    try:
        page = v1.notion_request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": tracker["parentPageId"]},
            "properties": {"title": {"title": [v1.rt(title)]}}})
        v1.append_blocks(page["id"], page_blocks(set_num, picked, stats))
        urls = [r["html_url"] for r in picked]
        entry = {"setNum": set_num, "special": False, "title": title, "pageId": page["id"],
                 "pageUrl": v1.notion_url(title, page["id"]), "repos": urls,
                 "count": len(urls), "version": VERSION}
        tracker["completedSets"] = set_num
        tracker["completedPages"].append(entry)
        merged, seen = [], set()
        for u in tracker.get("usedRepoUrls", []) + urls:
            if u.lower() not in seen:
                seen.add(u.lower())
                merged.append(u)
        tracker["usedRepoUrls"] = merged
        v1.write_tracker(tracker)
        append_master_csv(set_num, picked)
        (v1.AUDIT_DIR / f"set{set_num}_{VERSION}_verified_audit.json").write_text(
            json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    finally:
        LOCKFILE.unlink(missing_ok=True)
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    print(f"[{VERSION}] published {title} ({len(picked)} repos) · master CSV +{len(picked)} rows "
          f"· {round(time.time() - t0, 1)}s ✅")


if __name__ == "__main__":
    main()
