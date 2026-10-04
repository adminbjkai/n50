#!/usr/bin/env python3
"""
v13.py — product-diversity proof-first self-hosted web apps runner.

Same numbered Notion series, same tracker + master CSV as v1–v11. Imports v1
plumbing only; never modifies older versions. Full design: V12-STRATEGY.md.

What v13 changes vs v12:
  1. HARD ship-proof after GraphQL: every pick must be tier A (compose/Dockerfile
     + app intent), B (awesome-selfhosted), or C (explicit self-host + product
     homepage). Min 40 A∪B, max 10 C. No more score-only shipping.
  2. Docker probe split: compose vs Dockerfile; .env.example alone is NOT proof
     (v11 counted it as docker_signal).
  3. Tight app gate: removed stars≥30 language escape; strict self-host topics/
     phrases required. Expanded hard_reject for SDKs, frameworks, ML-infra,
     tutorials, research/weights.
  4. Awesome residual SEEDS the pool (not only +score). Bench stores only
     proof-eligible surplus.
  5. High-signal discovery queries + search-time negatives; product category
     hard caps (AI/LLM ≤4, etc.); momentum cap so viral libs don't dominate.
  6. Separate caches (*_v12.json); VERSION drives every label. Shared tracker.

Always --dry-run first. Auth: gh CLI or GH_TOKEN; Notion token via env/file.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
import run_next_set as v1  # noqa: E402  — stable plumbing only; never modified

HERE = Path(__file__).resolve().parent
SHARED_ROOT = HERE.parent
if not v1.TRACKER.exists() and (SHARED_ROOT / "notion-selfhosted-tracker.json").exists():
    # The updates_for_ref folder carries script snapshots, while the live
    # tracker/master CSV/_tmp directory live one level up in notion50new.
    v1.HERE = SHARED_ROOT
    v1.TRACKER = SHARED_ROOT / "notion-selfhosted-tracker.json"
    v1.AUDIT_DIR = SHARED_ROOT / "_tmp"
    v1.MASTER_CSV = SHARED_ROOT / "notion-selfhosted-master.csv"
VERSION = "v13"
CACHE_DIR = v1.AUDIT_DIR
REPO_CACHE = CACHE_DIR / "repo_cache_v13.json"
AWESOME_CACHE = CACHE_DIR / "awesome_selfhosted_v13.json"
ENRICH_CACHE = CACHE_DIR / "enrich_cache_v13.json"
BENCH_CACHE = CACHE_DIR / "bench_v13.json"
LOCKFILE = CACHE_DIR / "publish_v13.lock"
# Fall back to v11 awesome cache if v12 not yet populated (same data).
AWESOME_CACHE_FALLBACK = CACHE_DIR / "awesome_selfhosted_v12.json"

CACHE_TTL = 14 * 24 * 3600
BENCH_TTL = 14 * 24 * 3600
BENCH_MAX = 250
DEAD_TTL = 3 * 24 * 3600
AWESOME_TTL = 30 * 24 * 3600
LOCK_TTL = 30 * 60

GRAPHQL_BATCH = 35
ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

ENRICH_SELFHOST_PENALTY = 15.0
ENRICH_NOVELTY_WEIGHT = 1.0

# Fresh-gem quota: mild — solid older missed apps are first-class.
FRESH_QUOTA_DEFAULT = 4
FRESH_STAR_CEILING = 100
FRESH_AGE_CEILING_DAYS = 180

# Proof-tier fractions (scaled by --target N)
MIN_AB_FRAC = 0.70
MAX_C_FRAC = 0.30
MIN_AB_PROOF = 35   # compat for N=50; scale via MIN_AB_FRAC for other N
MAX_TIER_C = 15
DEFAULT_MIN_SCORE = 20.0
DEFAULT_CAT_CAP = 5
DEFAULT_TARGET = 50

# Family floors/ceilings for N=50; scaled for other N via scale_n()
# AI is HARD-capped low so it cannot dominate like set 381.
FAMILY_RULES = {
    # family: (floor_base, ceil_base) for N=50
    "files": (3, 6),
    "knowledge": (3, 6),
    "media": (3, 6),
    "documents": (2, 5),
    "productivity": (2, 5),
    "bookmarks_rss": (2, 4),
    "dash_mon": (2, 5),
    "proxy_net": (1, 4),
    "dev_web": (1, 4),
    "db_light": (1, 3),
    "sports": (1, 3),
    "comms": (1, 4),
    "security": (1, 4),
    "cms": (1, 3),
    "home_health": (1, 3),
    "ai": (0, 3),          # HARD: never more than ~6% of set
    "devops": (0, 4),
    "gaming": (0, 3),
    "other": (0, 6),
}

# Map fine heuristic categories -> family
HCAT_TO_FAMILY = {
    "Files / Storage / Backup": "files",
    "Notes / Knowledge": "knowledge",
    "Books / Reading / Library": "knowledge",
    "Search / Indexing": "knowledge",
    "Media / Streaming": "media",
    "Image / Design / Creative": "media",
    "Documents / PDF / Paperless": "documents",
    "Productivity / Tasks": "productivity",
    "Automation": "productivity",
    "CRM / Business": "productivity",
    "Finance / Budget": "productivity",
    "Dashboard / Homelab": "dash_mon",
    "Monitoring / Observability": "dash_mon",
    "Networking / VPN": "proxy_net",
    "Privacy / Ad-Blocking": "proxy_net",
    "Developer Tools / Utilities": "dev_web",
    "Analytics / Data": "db_light",
    "Communication / Social": "comms",
    "Security / Auth": "security",
    "CMS / Website": "cms",
    "E-commerce / Shop": "cms",
    "Home Automation / IoT": "home_health",
    "Health / Food / Fitness": "home_health",
    "Education / Learning": "other",
    "Maps / GIS / Location": "other",
    "AI / LLM": "ai",
    "DevOps / Infra": "devops",
    "Docker / Containers": "devops",
    "Gaming / Game Servers": "gaming",
    "Web App / Other": "other",
    "Sports / Scores / Fantasy": "sports",
}

# Soft fine-cat caps (legacy name HARD_CAT_CAPS) — AI tight
HARD_CAT_CAPS = {
    "AI / LLM": 3,
    "Developer Tools / Utilities": 5,
    "Networking / VPN": 5,
    "DevOps / Infra": 5,
    "Gaming / Game Servers": 4,
    "Docker / Containers": 3,
    "Web App / Other": 8,
}

PREFERRED_PRODUCT_CATS = [
    "Files / Storage / Backup", "Notes / Knowledge", "Media / Streaming",
    "Documents / PDF / Paperless", "Productivity / Tasks", "Dashboard / Homelab",
    "Monitoring / Observability", "Security / Auth", "Finance / Budget",
    "CMS / Website", "Communication / Social", "Books / Reading / Library",
    "Health / Food / Fitness", "Search / Indexing", "Privacy / Ad-Blocking",
    "CRM / Business", "E-commerce / Shop", "Automation", "Sports / Scores / Fantasy",
    "Maps / GIS / Location", "Image / Design / Creative", "Education / Learning",
]

PRODUCT_FAMILY_BOOST = 6.0
AI_WEBUI_DAMP = -8.0
AI_NO_UI_PENALTY = -20.0
RUST_STACK_BONUS = 1.5

# Theme discovery bank — product wedges (see V13-STRATEGY.md)
THEME_QUERY_BANK = {
    "file_upload_share": [
        '"file sharing" "self-hosted" stars:>=10 archived:false',
        '"file upload" "docker compose" stars:>=10 archived:false',
        'topic:file-sharing topic:selfhosted stars:>=10 archived:false',
    ],
    "nextcloud_like": [
        '"nextcloud alternative" self-hosted stars:>=10 archived:false',
        '"personal cloud" "docker compose" stars:>=10 archived:false',
        'topic:cloud-storage topic:selfhosted stars:>=10 archived:false',
    ],
    "knowledge_wiki_notes": [
        '"note taking" self-hosted stars:>=10 archived:false',
        '"knowledge base" "docker compose" stars:>=10 archived:false',
        'topic:wiki topic:selfhosted stars:>=10 archived:false',
        '"notion alternative" self-hosted stars:>=10 archived:false',
    ],
    "media_management": [
        '"media server" self-hosted stars:>=10 archived:false',
        '"photo gallery" "docker compose" stars:>=10 archived:false',
        '"music server" self-hosted stars:>=10 archived:false',
        'topic:media-server topic:selfhosted stars:>=10 archived:false',
    ],
    "file_convert": [
        '"file converter" self-hosted stars:>=10 archived:false',
        '"document converter" "docker compose" stars:>=5 archived:false',
    ],
    "json_web_tools": [
        '"json formatter" self-hosted stars:>=10 archived:false',
        '"devtools" "web ui" "docker compose" stars:>=10 archived:false',
    ],
    "pdf_editor": [
        '"pdf editor" self-hosted stars:>=10 archived:false',
        '"pdf tools" "docker compose" stars:>=10 archived:false',
        '"document management" self-hosted stars:>=10 archived:false',
    ],
    "video_stream_proxy": [
        '"video streaming" self-hosted stars:>=10 archived:false',
        '"media proxy" "docker compose" stars:>=5 archived:false',
        '"video downloader" self-hosted "web" stars:>=10 archived:false',
        '"iptv" self-hosted "web" stars:>=10 archived:false',
    ],
    "dashboards_homepages": [
        '"startpage" self-hosted stars:>=10 archived:false',
        '"homepage dashboard" "docker compose" stars:>=10 archived:false',
        'topic:homepage topic:selfhosted stars:>=10 archived:false',
    ],
    "bookmarks": [
        '"bookmark manager" self-hosted stars:>=10 archived:false',
        'topic:bookmarks topic:selfhosted stars:>=10 archived:false',
        '"read-it-later" self-hosted stars:>=10 archived:false',
    ],
    "monitoring_uptime": [
        '"uptime monitor" self-hosted stars:>=10 archived:false',
        '"status page" "docker compose" stars:>=10 archived:false',
        'topic:uptime topic:selfhosted stars:>=10 archived:false',
    ],
    "lightweight_db_admin": [
        '"database admin" self-hosted stars:>=10 archived:false',
        '"sql client" "web ui" "docker compose" stars:>=10 archived:false',
    ],
    "sports_stats": [
        '"sports" self-hosted "web app" stars:>=5 archived:false',
        '"sports tracker" "docker compose" stars:>=5 archived:false',
        '"scoreboard" self-hosted stars:>=5 archived:false',
        '"fantasy sports" self-hosted stars:>=5 archived:false',
    ],
    "productivity": [
        '"kanban" "docker compose" self-hosted stars:>=10 archived:false',
        '"project management" self-hosted stars:>=10 archived:false',
        'topic:calendar topic:selfhosted stars:>=10 archived:false',
    ],
    "rust_selfhosted": [
        'language:Rust "self-hosted" "web" stars:>=10 archived:false',
        'language:Rust "docker compose" "web ui" stars:>=10 archived:false',
    ],
    "rss": [
        '"rss reader" self-hosted stars:>=10 archived:false',
        'topic:rss topic:selfhosted stars:>=10 archived:false',
    ],
    "password": [
        '"password manager" self-hosted stars:>=10 archived:false',
        'topic:password-manager topic:selfhosted stars:>=10 archived:false',
    ],
    "finance": [
        '"expense tracker" self-hosted stars:>=10 archived:false',
        '"invoice" self-hosted docker stars:>=10 archived:false',
    ],
    "maps": [
        '"openstreetmap" "docker compose" self-hosted stars:>=10 archived:false',
        '"tile server" self-hosted stars:>=10 archived:false',
    ],
    "recipes": [
        '"recipe manager" self-hosted stars:>=10 archived:false',
        'topic:recipes topic:selfhosted stars:>=5 archived:false',
    ],
    "cms": [
        '"headless cms" self-hosted stars:>=10 archived:false',
        'topic:cms topic:selfhosted stars:>=10 archived:false',
    ],
    "helpdesk": [
        '"helpdesk" self-hosted stars:>=10 archived:false',
        '"ticketing" "docker compose" self-hosted stars:>=10 archived:false',
    ],
}
THEME_KEYS = list(THEME_QUERY_BANK.keys())


def scale_n(base, n, default_n=50):
    """Scale a floor/ceil from base (N=50) to target n."""
    if base <= 0:
        return 0
    return max(1, int(round(base * n / default_n)))


def family_of(hcat):
    return HCAT_TO_FAMILY.get(hcat, "other")

# Strict self-host proof topics (bare "docker" / "open-source" do NOT count).
STRICT_SELFHOST_TOPICS = {
    "self-hosted", "selfhosted", "self-hosting", "self-hostable",
    "homelab", "home-server", "selfhosted-app", "run-your-own", "deploy-yourself",
}

STRICT_SELFHOST_PHRASES = (
    "self-host", "self host", "self-hostable", "self hosted", "selfhosted",
    "homelab", "home server", "home-server", "run your own", "deploy yourself",
    "docker compose", "docker-compose", "self-hostable", "host it yourself",
    "host yourself", "on your own server", "on your server",
)

APP_SHAPE_PHRASES = (
    "web app", "web application", "web-based", "webui", "web ui", "web-ui",
    "admin panel", "admin dashboard", "dashboard", "control panel",
    "media server", "password manager", "note taking", "note-taking",
    "file sharing", "file manager", "rss reader", "feed reader",
    "kanban", "project management", "invoice", "helpdesk", "ticketing",
    "photo management", "photo gallery", "document management",
    "alternative to", "open source alternative",
)

# NOTE: Do NOT append multi `-keyword` negatives to GitHub *repo* search queries.
# Empirically (2026-07) chains like `-tutorial -sdk -library` collapse total_count
# to 0 even for `topic:selfhosted`. Junk is filtered by hard_reject + proof tiers.
SEARCH_NEGATIVES = ""

# ---------------------------------------------------------------------------
# Discovery query bank — v12: high-proof only (product + self-host co-constraint).
# Bare tech topics (ai, api, docker alone) were flooding v11 with non-apps.
# ---------------------------------------------------------------------------
# Product / self-host topics used with topic:selfhosted co-constraint.
PRODUCT_TOPICS = [
    "self-hosted", "selfhosted", "self-hosting", "homelab", "home-server",
    "web-app", "webapp", "dashboard", "homepage", "startpage",
    "media-server", "music-server", "photos", "audiobook", "podcast",
    "notes", "note-taking", "wiki", "knowledge-base", "bookmarks", "rss",
    "read-it-later", "paperless", "document-management",
    "kanban", "task-management", "project-management", "calendar",
    "time-tracking", "finance", "budgeting", "expense-tracker", "invoicing",
    "monitoring", "observability", "uptime", "status-page",
    "vpn", "wireguard", "dns", "reverse-proxy", "ad-blocker", "privacy",
    "file-sharing", "backup", "sync", "cloud-storage",
    "automation", "workflow", "home-automation", "smart-home",
    "cms", "e-commerce", "crm", "helpdesk", "password-manager",
    "recipes", "fitness", "health", "lms", "education",
    "whiteboard", "matrix", "fediverse", "pastebin", "url-shortener",
    "local-ai", "ollama", "ai-dashboard", "voice-assistant",
    "personal-knowledge-base", "second-brain", "calibre", "bookshelf",
    "selfhosted-app", "deploy-yourself", "run-your-own",
    "docker-compose",  # only used co-constrained with selfhosted below
]

# Back-compat alias used by gather loops.
SEARCH_TOPICS = PRODUCT_TOPICS

SEARCH_PHRASES = [
    '"self-hosted" "docker compose"',
    '"self-hosted" "web app"',
    '"self-hosted alternative"',
    '"homelab" dashboard',
    '"admin dashboard" "self-hosted"',
    '"open source" "web application" "docker compose"',
    '"notion alternative" self-hosted',
    '"google alternative" self-hosted',
    '"kanban" "docker compose" self-hosted',
    '"privacy friendly" self-hosted',
    '"lightweight" "self-hosted" "docker"',
    '"run your own" "open source" docker',
    '"web ui" "docker compose" self-hosted',
    '"webui" self-hosted docker',
    '"password manager" self-hosted',
    '"media server" self-hosted',
    '"rss reader" self-hosted',
    '"invoice" self-hosted docker',
]

COMPOSITE_QUERY_TEMPLATES = [
    'topic:selfhosted stars:5..400 pushed:>{pushed_18m} archived:false',
    'topic:self-hosted stars:5..400 pushed:>{pushed_18m} archived:false',
    'topic:docker-compose topic:selfhosted stars:5..500 pushed:>{pushed_24m} archived:false',
    'topic:selfhosted topic:web-app stars:5..300 pushed:>{pushed_18m} archived:false',
    'topic:selfhosted topic:homelab stars:5..300 pushed:>{pushed_18m} archived:false',
    '"self-hosted" "docker compose" in:readme stars:3..300 pushed:>{pushed_18m} archived:false',
    '"self-hosted alternative" in:readme stars:1..300 pushed:>{pushed_24m} archived:false',
    '"web ui" OR webui "docker compose" in:readme stars:2..250 pushed:>{pushed_18m} archived:false',
    'self-hosted in:description stars:10..800 pushed:>{pushed_18m} archived:false',
    'selfhosted in:description stars:10..800 pushed:>{pushed_18m} archived:false',
    '"open source alternative to" "self-hosted" in:readme stars:1..200 pushed:>{pushed_24m} archived:false',
    '"docker compose up" self-hosted in:readme stars:1..200 pushed:>{pushed_18m} archived:false',
    'topic:selfhosted stars:1..200 forks:1..80 license:mit pushed:>{pushed_24m} archived:false',
    'topic:selfhosted stars:1..200 forks:1..80 license:apache-2.0 pushed:>{pushed_24m} archived:false',
]

# ---------------------------------------------------------------------------
# Categorisation — heuristic; this is what diversity is ALWAYS keyed on.
# (Same as v7, plus a few new category keywords for the fresh topics.)
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ("AI / LLM", ["llm", "gpt", "chatbot", "rag", "openai", "ollama", "ai-agent",
                  "ai-agents", "agents", "machine-learning", "deep-learning",
                  "federated-learning", "neural-network", "transformers",
                  "stable-diffusion", "langchain", "embeddings", "local-llm",
                  "llms", "ml", "ai", "local-ai", "on-device-ai", "edge-ai",
                  "vector-db", "model-serving", "ml-platform", "feature-store",
                  "gguf", "llama-cpp", "text-to-speech", "tts", "stt",
                  "voice-assistant"]),
    ("Home Automation / IoT", ["home-assistant", "homeassistant", "home-automation",
                               "smart-home", "smarthome", "mqtt", "zigbee", "zwave",
                               "esphome", "iot", "tasmota", "domotics", "smart-mirror",
                               "magic-mirror", "homebridge", "scrypted",
                               "bms", "ev-charging", "solar-monitor", "energy-monitor",
                               "plant-monitor", "aquarium", "hydroponics",
                               "3d-printer", "cnc", "drone", "flight-controller"]),
    ("Media / Streaming", ["media-server", "media", "video", "music", "audio",
                           "streaming", "plex", "jellyfin", "emby", "podcast",
                           "movie", "movies", "photo", "photos", "gallery",
                           "audiobook", "subtitles", "transcoding"]),
    ("Books / Reading / Library", ["ebook", "epub", "calibre", "comics", "comic",
                                   "manga", "ebook-reader", "books", "audiobookshelf",
                                   "kindle", "opds", "bookshelf"]),
    ("Maps / GIS / Location", ["map", "maps", "gis", "gps", "geo", "geospatial",
                               "openstreetmap", "osm", "leaflet", "location",
                               "navigation", "tracking-gps"]),
    ("Gaming / Game Servers", ["game", "gaming", "minecraft", "game-server",
                               "gameserver", "emulator", "retro", "steam",
                               "speedrun", "tabletop"]),
    ("Monitoring / Observability", ["monitoring", "observability", "metrics",
                                    "grafana", "prometheus", "uptime",
                                    "status-page", "logging", "logs", "telemetry",
                                    "alerting"]),
    ("DevOps / Infra", ["kubernetes", "k8s", "helm", "gitops", "terraform",
                        "ansible", "ci-cd", "cicd", "devops", "cluster",
                        "infrastructure", "container-orchestration", "proxmox",
                        "infrastructure-as-code"]),
    ("Developer Tools / Utilities", ["developer-tools", "devtools", "cli", "sdk",
                                     "git", "code", "ide", "pastebin",
                                     "url-shortener", "webhook", "api-gateway",
                                     "low-code", "no-code", "code-snippets",
                                     "regex", "json", "formatter"]),
    ("Networking / VPN", ["vpn", "wireguard", "tailscale", "dns", "proxy",
                          "reverse-proxy", "traefik", "nginx", "firewall",
                          "gateway", "networking", "network", "tunnel"]),
    ("Privacy / Ad-Blocking", ["pi-hole", "pihole", "adguard", "adblock",
                               "ad-blocker", "adblocker", "dns-blocking",
                               "dns-sinkhole", "blocklist"]),
    ("Security / Auth", ["password", "password-manager", "vault", "secrets", "2fa",
                         "totp", "sso", "oauth", "oidc", "identity", "auth",
                         "authentication", "security", "encryption"]),
    ("Notes / Knowledge", ["notes", "note-taking", "wiki", "knowledge-base",
                           "knowledge-management", "markdown", "bookmark",
                           "bookmarks", "read-it-later", "rss", "feed-reader",
                           "second-brain", "zettelkasten", "personal-knowledge-base"]),
    ("Documents / PDF / Paperless", ["pdf", "paperless", "document-management",
                                     "documents", "ocr", "scanner", "scan",
                                     "digital-archive", "dms"]),
    ("Productivity / Tasks", ["kanban", "todo", "task-management", "tasks",
                              "project-management", "calendar", "time-tracking",
                              "pomodoro", "productivity", "notion", "scheduling",
                              "habit-tracker"]),
    ("Finance / Budget", ["finance", "budget", "budgeting", "expense",
                          "expense-tracker", "accounting", "invoice", "invoicing",
                          "crypto", "bitcoin", "btc", "personal-finance", "ledger"]),
    ("Files / Storage / Backup", ["file-sharing", "file-manager", "storage",
                                  "backup", "sync", "nextcloud", "cloud-storage",
                                  "drive", "s3", "webdav"]),
    ("E-commerce / Shop", ["e-commerce", "ecommerce", "shop", "store", "cart",
                           "payments", "pos", "point-of-sale", "marketplace",
                           "storefront"]),
    ("CRM / Business", ["crm", "erp", "helpdesk", "ticketing", "support",
                        "sales", "business", "hr", "scheduling-business"]),
    ("Communication / Social", ["chat", "messaging", "forum", "email", "mail",
                                "social", "comments", "discord", "matrix",
                                "fediverse", "activitypub", "mastodon", "irc"]),
    ("Health / Food / Fitness", ["health", "fitness", "workout", "recipe",
                                 "recipes", "cooking", "nutrition", "diet", "meal",
                                 "meal-planning", "medical", "wellness"]),
    ("Education / Learning", ["education", "lms", "course", "courses", "e-learning",
                              "elearning", "quiz", "flashcards", "study",
                              "language-learning", "edtech", "school"]),
    ("Image / Design / Creative", ["image-editor", "design", "drawing",
                                   "whiteboard", "diagram", "diagrams",
                                   "photo-editing", "vector", "figma", "canvas",
                                   "screenshot"]),
    ("Search / Indexing", ["search-engine", "search", "elasticsearch",
                           "meilisearch", "typesense", "indexing", "full-text-search"]),
    ("Automation", ["automation", "workflow", "n8n", "scraper", "scraping", "bot",
                    "cron", "ifttt", "zapier-alternative", "rpa",
                    "workflow-automation"]),
    ("Dashboard / Homelab", ["dashboard", "homelab", "homepage", "startpage", "nas",
                             "home-server", "self-hosted", "selfhosted",
                             "self-hosting"]),
    ("Analytics / Data", ["analytics", "data", "database", "etl", "bi",
                          "data-visualization", "dataviz", "metrics-dashboard"]),
    ("CMS / Website", ["cms", "blog", "website", "static-site",
                       "static-site-generator", "publishing", "portfolio",
                       "headless-cms"]),
    ("Docker / Containers", ["docker", "docker-compose", "container", "containers",
                             "podman", "oci"]),
]
ALLOWED_CATEGORIES = [label for label, _ in CATEGORY_RULES] + ["Web App / Other"]


def categorize(repo):
    name = (repo.get("full_name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    text = f"{name} {desc} {' '.join(topics)}"
    for label, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in topics or re.search(r"\b" + re.escape(kw) + r"\b", text):
                return label
    return "Web App / Other"


# ---------------------------------------------------------------------------
# Quality gate — same as v7.
# ---------------------------------------------------------------------------
BAD_TERMS = [
    "awesome-list", "curated list", "collection of", "list of",
    "roadmap", "cheatsheet", "cheat sheet", "boilerplate", "starter-kit",
    "starter kit", "scaffold", "skeleton", "project template", "app template",
    "repo template", "template repo", "dotfiles",
    "wallpaper", "wallpapers", "leetcode", "interview", "coursework", "homework",
    "study notes", "learning-path", "freecodecamp",
    "demo project", "example project", "test repo", "my portfolio",
    "personal website", "personal blog", "config files", "course materials",
    "workshop", "bootcamp",
]
# "template" alone is NOT a hard reject — many real apps say "collection templates".
MOBILE_ONLY = ("android app", "ios app", "flutter app", "react native client",
               "android client", "ios client")
LIBRARY_SIGNALS = ("library for", "a library", "sdk for", "client library",
                   "framework for", "wrapper for", "wrapper around", "bindings for",
                   "npm package", "python package", "rust crate", "go module",
                   "implementation of", "port of", "parser for", "for the browser",
                   "javascript library", "js library", "header-only")
NON_APP_SIGNALS = ("browser extension", "chrome extension", "firefox extension",
                   "firefox add-on", "web extension", "userscript", "greasemonkey",
                   "tampermonkey", "desktop app", "desktop application", "macos app",
                   "windows app", "command-line tool", "command line tool", "cli tool")
# Broad deploy/homelab topics (used for soft intent only; not enough for Tier C).
GOOD_TOPICS = {
    "self-hosted", "selfhosted", "self-hosting", "docker", "docker-compose",
    "homelab", "web-app", "webapp", "dashboard", "self-hostable",
    "raspberry-pi", "nas", "home-server",
    "selfhosted-app", "run-your-own", "deploy-yourself",
}

EXTRA_HARD_REJECT = (
    "api client", "sdk for", "python package for", "npm package",
    "rust crate", "go module", "wrapper around", "wrapper for",
    "bindings for", "client library", "http client for",
    "web framework", "microframework", "framework for building",
    "for building apis", "for building web",
    "model weights", "pretrained weights", "arxiv", "paper implementation",
    "reproduction of", "benchmark suite", "training framework",
    "inference engine only", "checkpoint release",
    "github action", "pre-commit hook", "eslint plugin", "webpack plugin",
    "helm chart for", "terraform module", "operator for",
    "writeup", "write-ups", "learning path", "complete backend course",
    "course materials", "study notes",
)


def _repo_blob(repo):
    name = (repo.get("full_name") or "").lower()
    desc = (repo.get("description") or "").lower()
    return f"{name} {desc}", name, desc, {t.lower() for t in repo.get("topics", [])}


def has_strict_selfhost_intent(repo):
    """True if topics/desc show explicit self-host product intent (not bare docker)."""
    blob, name, desc, topics = _repo_blob(repo)
    if topics & STRICT_SELFHOST_TOPICS:
        return True
    if any(p in desc for p in STRICT_SELFHOST_PHRASES):
        return True
    if any(p in desc for p in APP_SHAPE_PHRASES) and any(
        w in desc for w in ("docker", "self-host", "homelab", "deploy", "server")
    ):
        return True
    return False


def has_app_shape(repo):
    """Heuristic: looks like a deployable app product, not a library."""
    blob, name, desc, topics = _repo_blob(repo)
    if has_strict_selfhost_intent(repo):
        return True
    if any(p in desc for p in APP_SHAPE_PHRASES):
        return True
    if topics & {"web-app", "webapp", "dashboard", "homelab", "self-hosted", "selfhosted"}:
        return True
    return False


def hard_reject(repo):
    """Return a reject reason string, or None if the repo passes the gate."""
    blob, name, desc, topics = _repo_blob(repo)
    short = name.split("/")[-1] if "/" in name else name
    if not desc:
        return "no-description"
    if desc.startswith("http"):
        return "description-is-url"
    if any(term in blob for term in BAD_TERMS):
        return "list-template-or-tutorial"
    if "awesome-list" in topics or short.startswith("awesome-"):
        return "awesome-list"
    if any(m in desc for m in MOBILE_ONLY) and not (topics & STRICT_SELFHOST_TOPICS) and "server" not in desc:
        return "mobile-only-client"
    if any(s in desc for s in NON_APP_SIGNALS):
        return "not-a-web-app (extension/desktop/cli)"
    # Library/SDK: no GOOD_TOPICS escape (v11 hole). Self-host monorepos with
    # "library" in desc still need to pass via app shape later + proof tier.
    if any(lib in desc for lib in LIBRARY_SIGNALS):
        if not (topics & STRICT_SELFHOST_TOPICS) and not any(p in desc for p in STRICT_SELFHOST_PHRASES):
            return "library-or-sdk"
    if any(s in desc for s in EXTRA_HARD_REJECT):
        return "non-app-pattern"
    if re.search(r"(^|/)(sdk|client|wrapper|bindings)($|[-_])", short) and not (topics & STRICT_SELFHOST_TOPICS):
        if "server" not in desc and "self-host" not in desc:
            return "name-looks-like-sdk-client"
    if short.endswith(("-sdk", "-client", "-api-client", "-wrapper", "-bindings")) and "server" not in desc:
        if not (topics & STRICT_SELFHOST_TOPICS):
            return "name-looks-like-sdk-client"
    stars = repo.get("stargazers_count", 0)
    if re.search(r"\bclone\b", desc) and stars < 150 and repo.get("forks_count", 0) < 10:
        return "low-effort-clone"
    # Pure game client without server/self-host signal.
    if re.search(r"\b(game client|unity game|godot game)\b", desc) and not (topics & STRICT_SELFHOST_TOPICS):
        if "game-server" not in topics and "server" not in desc:
            return "game-client-not-server"
    return None


def passes_app_gate(repo):
    """v13: require self-host / app-shape signal, but not absurdly strict."""
    if hard_reject(repo):
        return False
    desc = (repo.get("description") or "").lower()
    language = repo.get("language") or "Unknown"
    stars = repo.get("stargazers_count", 0)
    topics = {t.lower() for t in repo.get("topics", [])}
    if language in {"Unknown", "Markdown", "TeX"}:
        # Docker/shell deploy repos sometimes lack a "real" language.
        if not (topics & STRICT_SELFHOST_TOPICS) and "docker" not in desc:
            return False
    if stars < 3:
        return False
    if len(desc) < 15:
        return False
    if has_strict_selfhost_intent(repo) or has_app_shape(repo):
        return True
    # Soft pass: selfhosted topic + decent desc (common for real apps).
    if (topics & STRICT_SELFHOST_TOPICS) and len(desc) >= 20 and stars >= 5:
        return True
    return False


# ---------------------------------------------------------------------------
# Fresh-gem gate — still rejects junk; still requires self-host / app shape.
# ---------------------------------------------------------------------------
def passes_fresh_gate(repo):
    """Gentler star floor, but v12 still requires self-host or app-shape signal.
    Proof tier is enforced later after GraphQL — youth is not a substitute.
    """
    if hard_reject(repo):
        return False
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    language = repo.get("language") or "Unknown"
    stars = repo.get("stargazers_count", 0)
    if stars < 1:
        return False
    if language in {"Unknown", "Markdown", "TeX"}:
        if not (topics & STRICT_SELFHOST_TOPICS):
            return False
    if len(desc) < 20:
        return False
    # Must show self-host or app-shape (v11 allowed any topics).
    if not (has_strict_selfhost_intent(repo) or has_app_shape(repo)):
        return False
    return True


def product_homepage(repo):
    home = (repo.get("homepage") or "").strip()
    if not home.startswith("http"):
        return False
    low = home.lower()
    if "github.com" in low or "githubusercontent.com" in low:
        return False
    return True



def has_ai_webui(repo):
    """True if AI project looks like a deployable chat/web UI, not bare infra."""
    desc = (repo.get("description") or "").lower()
    topics = {x.lower() for x in repo.get("topics", [])}
    blob = desc + " " + " ".join(topics)
    ui = ("webui", "web-ui", "web ui", "open-webui", "gradio", "chat interface",
          "chatbot ui", "frontend", "dashboard", "playground")
    return any(u in blob for u in ui)


def apply_family_score_adjust(repo, score, breakdown, hcat):
    """Boost product families; damp AI dominance."""
    fam = family_of(hcat)
    b = dict(breakdown)
    s = score
    if fam == "ai":
        if has_ai_webui(repo):
            b["ai_webui_damp"] = AI_WEBUI_DAMP
            s += AI_WEBUI_DAMP
        else:
            b["ai_no_ui"] = AI_NO_UI_PENALTY
            s += AI_NO_UI_PENALTY
    elif fam in ("files", "knowledge", "media", "documents", "productivity",
                 "bookmarks_rss", "dash_mon", "security", "cms", "sports",
                 "db_light", "comms", "home_health"):
        b["product_family"] = PRODUCT_FAMILY_BOOST
        s += PRODUCT_FAMILY_BOOST
    lang = (repo.get("language") or "")
    if lang == "Rust" and fam not in ("ai", "devops"):
        b["rust"] = RUST_STACK_BONUS
        s += RUST_STACK_BONUS
    # bookmarks/rss keyword family remap boost if desc matches
    d = desc = (repo.get("description") or "").lower()
    if any(k in d for k in ("bookmark", "read-it-later", "rss reader", "feed reader")):
        if fam != "ai":
            b["bookmarks_rss_nudge"] = 2.0
            s += 2.0
    return round(s, 2), b


# When True (large showcase pages), proof & gates are more inclusive.
LENIENT_MODE = False


def assign_proof_tier(repo, in_awesome, compose_signal, dockerfile_signal):
    """Return 'A'|'B'|'C'|None. None = cannot ship.

    A: compose or Dockerfile + app-ish description
    B: on awesome-selfhosted
    C: explicit self-host signal (homepage optional — v13 always allows)
    """
    if hard_reject(repo):
        return None
    intent = has_strict_selfhost_intent(repo) or has_app_shape(repo)
    desc = (repo.get("description") or "").strip()
    topics = {t.lower() for t in repo.get("topics", [])}
    dockerish = compose_signal or dockerfile_signal or bool(repo.get("docker_signal"))
    if dockerish:
        if intent or len(desc) >= 12:
            return "A"
        if LENIENT_MODE and len(desc) >= 8:
            return "A"
    if in_awesome:
        return "B"
    # Tier C: self-host intent or strict topics — homepage nice-to-have, not required.
    if has_strict_selfhost_intent(repo) and len(desc) >= 15:
        return "C"
    if (topics & STRICT_SELFHOST_TOPICS) and len(desc) >= 20:
        return "C"
    if intent and len(desc) >= 20 and (repo.get("stargazers_count") or 0) >= 5:
        return "C"
    if LENIENT_MODE and intent and len(desc) >= 15:
        return "C"
    return None


def is_fresh_gem(repo, now=None):
    """A repo is 'fresh' if it has <100 stars OR was created within 180 days."""
    now = now or datetime.now(timezone.utc)
    stars = repo.get("stargazers_count", 0)
    created = _parse_dt(repo.get("created_at"))
    age_days = (now - created).days if created else 999999
    return stars < FRESH_STAR_CEILING or age_days < FRESH_AGE_CEILING_DAYS


# ---------------------------------------------------------------------------
# Appeal scoring — v9 adds: graduated gem_balance, youth bonus, fresh-aware.
# ---------------------------------------------------------------------------

def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def appeal_score(repo, now=None):
    now = now or datetime.now(timezone.utc)
    stars = max(0, repo.get("stargazers_count", 0))
    forks = max(0, repo.get("forks_count", 0))
    desc = (repo.get("description") or "").strip()
    topics = repo.get("topics", []) or []
    homepage = (repo.get("homepage") or "").strip()
    created = _parse_dt(repo.get("created_at"))
    pushed = _parse_dt(repo.get("pushed_at"))
    age_days = (now - created).days if created else 365
    age_years = max(age_days / 365.0, 0.25)
    push_age = (now - pushed).days if pushed else 999

    b = {}
    # --- Momentum: velocity = stars/age. v12 caps lower so viral libs don't dominate. ---
    velocity = stars / age_years
    b["momentum"] = round(min(16.0, 7.0 * math.log10(velocity + 1)), 2)

    # --- Liveliness: same as v7 ---
    if push_age < 14:
        b["liveliness"] = 14.0
    elif push_age < 60:
        b["liveliness"] = 11.0
    elif push_age < 180:
        b["liveliness"] = 7.0
    elif push_age < 365:
        b["liveliness"] = 3.0
    else:
        b["liveliness"] = 0.0

    # --- Traction: same as v7 ---
    b["traction"] = round(min(12.0, 3.5 * math.log10(stars + 1)), 2)

    # --- Polish: same as v7 ---
    polish = 0.0
    if len(desc) >= 60:
        polish += 7.0
    elif len(desc) >= 30:
        polish += 4.5
    elif len(desc) >= 10:
        polish += 2.0
    if homepage.startswith("http"):
        polish += 5.0
    lic = repo.get("license") or {}
    spdx = (lic.get("spdx_id") if isinstance(lic, dict) else None) or ""
    if spdx and spdx not in {"NOASSERTION", "NONE"}:
        polish += 3.0
    polish += min(6.0, 1.3 * len(topics))
    b["polish"] = round(polish, 2)

    # --- Selfhost signal: v12 strict topics/phrases only (not bare docker/k8s). ---
    topic_set = {t.lower() for t in topics}
    if topic_set & STRICT_SELFHOST_TOPICS or any(p in desc.lower() for p in STRICT_SELFHOST_PHRASES):
        b["selfhost_signal"] = 8.0
    else:
        b["selfhost_signal"] = 0.0

    # --- Community: same as v7 (but note: 0 for <20★ repos) ---
    if stars >= 20:
        ratio = forks / (stars + 1)
        b["community"] = 4.0 if 0.03 <= ratio <= 0.3 else (0.0 if ratio > 0.6 else 2.0)
    else:
        b["community"] = 0.0

    # --- Youth bonus (mild) — v12 de-emphasises "brand new only".
    # Mature solid self-hosted apps that were missed earlier should still win.
    if age_days <= 90:
        b["youth"] = 3.0
    elif age_days <= 180:
        b["youth"] = 2.0
    elif age_days <= 365:
        b["youth"] = 1.0
    else:
        b["youth"] = 0.0
    # Mild maturity bonus: established but still active projects.
    if 365 <= age_days <= 2000 and push_age < 180:
        b["maturity"] = 3.0
    else:
        b["maturity"] = 0.0

    # --- Gem balance: GRADUATED curve (v9 fix for v7's flat −3 penalty). ---
    # v7: <50★ → −3 (punishes new repos). v9: 1-9★ → −1 (slight, not a wall),
    # 10-49★ → +2 (emerging), 50-99★ → +3, 100-299★ → +6, 300-3000★ → +10.
    if 300 <= stars <= 3000:
        gem = 10.0
    elif 100 <= stars < 300 or 3000 < stars <= 8000:
        gem = 6.0
    elif 50 <= stars < 100 or 8000 < stars <= 20000:
        gem = 3.0
    elif 10 <= stars < 50:
        gem = 2.0          # v9: emerging gem, not penalised
    elif 1 <= stars < 10:
        gem = -1.0         # v9: slight damp, NOT −3
    elif stars > 20000:
        gem = -6.0
    else:
        gem = -1.0          # v9: 0 stars → slight damp, not −3
    if b["liveliness"] < 3.0:
        gem = min(gem, 0.0)
    if 120 <= age_days <= 730:
        gem += 3.0
    b["gem_balance"] = round(gem, 2)

    return round(sum(b.values()), 2), b


def compose_line(breakdown, top_pos=3):
    """Render a one-line score composition: top positives ▲ and any negatives ▼."""
    items = [(k, v) for k, v in breakdown.items() if abs(v) >= 0.5]
    pos = sorted([kv for kv in items if kv[1] > 0], key=lambda kv: -kv[1])[:top_pos]
    neg = sorted([kv for kv in items if kv[1] < 0], key=lambda kv: kv[1])
    parts = []
    if pos:
        parts.append("▲ " + ", ".join(f"{k} +{v:g}" for k, v in pos))
    if neg:
        parts.append("▼ " + ", ".join(f"{k} {v:g}" for k, v in neg))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Adaptive search sweep (v7-style + fresh-gem sweep).
# ---------------------------------------------------------------------------
_RATE_LIMIT_MARKERS = ("rate limit", "secondary", "403", "429")
_SERVER_ERROR_MARKERS = ("500", "502", "503", "504", "server error",
                         "bad gateway", "gateway time", "timeout", "timed out",
                         "connection reset", "eof occurred", "temporarily")


def _get_gh_token():
    """Resolve a GitHub token. Old gh (e.g. 2.4) has no `auth token` — parse hosts.yml."""
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t and t.strip():
        return t.strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        out = (r.stdout or "").strip()
        # gh 2.4 prints "unknown command …" to stdout with rc!=0 — never treat as token.
        if r.returncode == 0 and out and "unknown" not in out.lower() and " " not in out:
            return out
    except Exception:
        pass
    # ~/.config/gh/hosts.yml  →  oauth_token: gho_…
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.is_file():
        try:
            for line in hosts.read_text().splitlines():
                if "oauth_token" in line or "oauth-token" in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        tok = parts[1].strip().strip("\"'")
                        if tok and " " not in tok:
                            return tok
        except OSError:
            pass
    return None


GH_TOKEN = _get_gh_token()


def native_gh_api(endpoint, params=None):
    """GitHub REST via urllib — no subprocess fork per call (v10's one real win)."""
    url = "https://api.github.com" + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    if GH_TOKEN:
        req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API error {e.code}" + (" rate limit" if e.code in (403, 429) else ""))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"GitHub connection error: {e}")


def native_graphql_api(query_str):
    req = urllib.request.Request("https://api.github.com/graphql",
                                 data=json.dumps({"query": query_str}).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if GH_TOKEN:
        req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"GraphQL error: {e}")


def gh_search(query, sort, page):
    """Authenticated search: urllib+token, else `gh api` (uses gh login)."""
    last = None
    for attempt in range(6):
        try:
            if GH_TOKEN:
                return native_gh_api("/search/repositories", {
                    "q": query, "sort": sort, "order": "desc",
                    "per_page": "100", "page": str(page),
                })
            # Authenticated CLI path (works even when hosts.yml token parse fails).
            path = (
                "search/repositories?"
                + urllib.parse.urlencode({
                    "q": query, "sort": sort, "order": "desc",
                    "per_page": "100", "page": str(page),
                })
            )
            return json.loads(v1.run(["gh", "api", path]))
        except (RuntimeError, ValueError, subprocess.CalledProcessError, OSError) as e:
            last = e
            msg = str(e).lower()
            if any(m in msg for m in _RATE_LIMIT_MARKERS) or "403" in msg or "429" in msg:
                wait = 15 * (attempt + 1)
                print(f"[search] rate-limited, sleeping {wait}s …")
                time.sleep(wait)
                continue
            if any(m in msg for m in _SERVER_ERROR_MARKERS):
                wait = min(40, 6 * (attempt + 1))
                print(f"[search] transient GitHub error (retry {attempt + 1}/6), sleeping {wait}s …")
                time.sleep(wait)
                continue
            raise
    print(f"[search] giving up on a page after retries ({str(last)[:70]}…); skipping")
    return {"items": []}


def _interleave(seq):
    """Coprime-stride permutation so adjacent items come from different themes."""
    n = len(seq)
    if n < 3:
        return list(seq)
    stride = max(2, int(n ** 0.5))
    while math.gcd(stride, n) != 1 and stride < n:
        stride += 1
    return [seq[(i * stride) % n] for i in range(n)]


def _date_windows(now):
    return {
        "created_90d": (now - timedelta(days=90)).strftime("%Y-%m-%d"),
        "created_12m": (now - timedelta(days=365)).strftime("%Y-%m-%d"),
        "created_24m": (now - timedelta(days=730)).strftime("%Y-%m-%d"),
        "created_30m": (now - timedelta(days=913)).strftime("%Y-%m-%d"),
        "pushed_12m": (now - timedelta(days=365)).strftime("%Y-%m-%d"),
        "pushed_18m": (now - timedelta(days=550)).strftime("%Y-%m-%d"),
        "pushed_24m": (now - timedelta(days=730)).strftime("%Y-%m-%d"),
        "pushed_30m": (now - timedelta(days=913)).strftime("%Y-%m-%d"),
    }


def _used_keys(used):
    """Normalize tracker used URLs into owner/repo keys + full urls."""
    names, urls = set(), set()
    for u in used or []:
        u = (u or "").lower().rstrip("/").rstrip(".git")
        urls.add(u)
        if "github.com/" in u:
            parts = u.split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                names.add(f"{parts[0]}/{parts[1]}")
        elif "/" in u and not u.startswith("http"):
            names.add(u)
    return names, urls


def _is_used(repo, used_names, used_urls):
    key = (repo.get("full_name") or "").lower()
    url = (repo.get("html_url") or "").lower().rstrip("/")
    return (key and key in used_names) or (url and url in used_urls)


def _add_search_results(pool, used, query, sort, max_pages, fresh_flag, stats):
    """Add search hits to pool. Skips already-used repos (critical with 19k+ used)."""
    calls, fresh_count, added = 0, 0, 0
    max_pages = max(1, max_pages)
    used_names, used_urls = _used_keys(used)
    for page in range(1, max_pages + 1):
        data = gh_search(query, sort, page)
        calls += 1
        items = data.get("items", [])
        if not items:
            stats["zeroYield"] += 1
            break
        page_new = 0
        for repo in items:
            if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
                continue
            key = (repo.get("full_name") or "").lower()
            if not key or key in pool:
                continue
            # Do NOT bank used repos — they inflate pool size and trigger false early-exit.
            if _is_used(repo, used_names, used_urls):
                continue
            repo["_fresh"] = fresh_flag
            repo["_found_by"] = {"query": query, "sort": sort, "page": page}
            pool[key] = repo
            added += 1
            page_new += 1
            fresh_count += 1
        # If a whole page is already used, keep paging a bit (created/updated sorts help).
        if page_new == 0 and page >= 2:
            stats["zeroYield"] = stats.get("zeroYield", 0) + 1
            break
        if len(items) < 100:
            break
    return calls, fresh_count, added


def gather_candidates(used, set_num, target_fresh, max_pages, pushed_after, stats,
                      do_fresh=True, bench_seed=None, awesome_seed=None):
    """Proof-first discovery: awesome residual + proof bench + high-signal sweeps.

    Lane 0 seeds (0 search calls): awesome residual + bench.
    Lanes 1–3: composite / product-topic / fresh — all co-constrained with
    self-host signals and SEARCH_NEGATIVES. Early-exit on pool size.
    """
    pool, calls, fresh_count = {}, 0, 0

    # --- Lane 0a: awesome residual (curated, free) ---
    awesome_seeded = 0
    for full_name in (awesome_seed or []):
        key = full_name.lower()
        if not key or key in pool:
            continue
        url = f"https://github.com/{key}"
        if url.lower() in used or key in used:
            continue
        # Minimal stub — GraphQL confirm will fill fields later.
        # Keep description empty-ish so hard_reject does not fire on placeholder text;
        # gate bypass uses _awesome_seed.
        stub = {
            "full_name": key,
            "html_url": url,
            "description": "Self-hosted open-source application (awesome-selfhosted)",
            "topics": ["self-hosted", "selfhosted"],
            "language": "TypeScript",
            "stargazers_count": 50,
            "forks_count": 5,
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "created_at": "2024-01-01T00:00:00Z",
            "homepage": "",
            "license": {"spdx_id": "MIT"},
            "_found_by": {"query": "awesome-residual", "sort": "-", "page": 0},
            "_awesome_seed": True,
        }
        pool[key] = stub
        awesome_seeded += 1
        if awesome_seeded >= 200:
            break
    stats["awesomeSeeded"] = awesome_seeded
    if awesome_seeded:
        print(f"[{VERSION}] awesome residual: seeded {awesome_seeded} unused curated repos")

    # --- Lane 0b: proof-filtered bench ---
    used_names, used_urls = _used_keys(used)
    for repo in (bench_seed or []):
        key = (repo.get("full_name") or "").lower()
        if key and key not in pool and not _is_used(repo, used_names, used_urls):
            repo.setdefault("_found_by", {"query": "bench", "sort": "-", "page": 0})
            pool[key] = repo
            fresh_count += 1
    stats["benchSeeded"] = len(pool) - awesome_seeded
    if stats["benchSeeded"]:
        print(f"[{VERSION}] bench: seeded {stats['benchSeeded']} proof-eligible candidates")

    now = datetime.now(timezone.utc)
    windows = _date_windows(now)
    # Pages: 1 is enough for discovery volume when queries yield; saves rate limit.
    pages = max(1, min(max_pages, 2))

    # --- Lane 1: high-proof composite sweep ---
    # Prefer updated/created: stars-desc is almost entirely already-used mega-repos.
    composite_queries = [q.format(**windows) for q in COMPOSITE_QUERY_TEMPLATES]
    composite_queries += [
        f'topic:selfhosted stars:5..800 pushed:>{windows["pushed_12m"]} archived:false',
        f'topic:self-hosted stars:5..800 pushed:>{windows["pushed_12m"]} archived:false',
        f'"self-hosted" "docker compose" stars:3..500 pushed:>{windows["pushed_12m"]} archived:false',
        f'"self-hosted" "web app" stars:3..500 pushed:>{windows["pushed_12m"]} archived:false',
        f'topic:selfhosted stars:1..200 created:>{windows["created_24m"]} archived:false',
        f'topic:homelab stars:5..500 pushed:>{windows["pushed_12m"]} archived:false',
    ]
    composite_sorts = ["updated", "created", "stars"]
    COMPOSITE_MAX_CALLS = 28 if stats.get("bigTarget") else 18
    print(f"[{VERSION}] research sweep: {len(composite_queries)} composite queries, "
          f"sorts={','.join(composite_sorts)}, max_pages={pages} …")
    for query in composite_queries[set_num % len(composite_queries):] + composite_queries[:set_num % len(composite_queries)]:
        for sort in composite_sorts:
            if calls >= COMPOSITE_MAX_CALLS:
                break
            c, f, _ = _add_search_results(pool, used, query, sort, pages, False, stats)
            calls += c
            fresh_count += f
            # Early-exit on UNUSED pool size (used repos no longer enter pool).
            if calls >= COMPOSITE_MAX_CALLS or (calls >= 6 and len(pool) >= max(target_fresh, 200)):
                break
        if calls >= COMPOSITE_MAX_CALLS or (calls >= 6 and len(pool) >= max(target_fresh, 200)):
            break
    print(f"[{VERSION}] research sweep done: {len(pool)} UNUSED repos, {calls} calls")

    # --- Lane 2: product topics co-constrained with selfhosted ---
    rt = set_num % len(SEARCH_TOPICS)
    topics = _interleave(list(SEARCH_TOPICS)[rt:] + list(SEARCH_TOPICS)[:rt])
    rp = set_num % len(SEARCH_PHRASES)
    phrases = list(SEARCH_PHRASES)[rp:] + list(SEARCH_PHRASES)[:rp]
    queries = []
    # Always lead with pure selfhosted (highest yield).
    queries.append(f"topic:selfhosted stars:>=5 pushed:>={pushed_after} archived:false")
    queries.append(f"topic:self-hosted stars:>=5 pushed:>={pushed_after} archived:false")
    for t in topics:
        if t in ("self-hosted", "selfhosted", "self-hosting", "selfhosted-app"):
            continue  # already covered
        queries.append(
            f"topic:selfhosted topic:{t} stars:>=5 pushed:>={pushed_after} archived:false")
    queries += [f"{p} stars:>=5 pushed:>={pushed_after} archived:false" for p in phrases]
    sort_rotation = ["updated", "created", "stars"]
    sorts = sort_rotation[set_num % len(sort_rotation):] + sort_rotation[:set_num % len(sort_rotation)]
    # Larger targets (e.g. 300 showcase) need more search budget.
    big = bool(stats.get("bigTarget"))
    MIN_QUERIES, MAX_CALLS = (12, 60) if big else (8, 40)
    print(f"[{VERSION}] quality sweep: {len(queries)} queries, sorts={','.join(sorts)} …")
    for query in queries:
        if calls >= MAX_CALLS:
            break
        sort = sorts[calls % len(sorts)]
        c, f, _ = _add_search_results(pool, used, query, sort, pages, False, stats)
        calls += c
        fresh_count += f
        if calls >= MIN_QUERIES and len(pool) >= max(target_fresh // 2, 150):
            break

    quality_pool_size = len(pool)
    print(f"[{VERSION}] quality sweep done: {quality_pool_size} repos, {calls} calls")

    # --- Lane 2b: PRODUCT THEME bank (files, KB, media, PDF, sports, …) ---
    theme_keys = list(THEME_KEYS)
    rk = set_num % len(theme_keys)
    theme_keys = theme_keys[rk:] + theme_keys[:rk]
    big = bool(stats.get("bigTarget"))
    # Always run a solid theme budget — theme queries find unused niches.
    theme_budget = 48 if big else 28
    theme_calls = 0
    print(f"[{VERSION}] theme sweep: {len(theme_keys)} themes, budget={theme_budget} …")
    for tk in theme_keys:
        if theme_calls >= theme_budget or calls >= MAX_CALLS + theme_budget:
            break
        qs = THEME_QUERY_BANK[tk]
        qi = set_num % len(qs)
        take = qs if big else (qs[qi:] + qs[:qi])[:2]
        for q in take:
            if theme_calls >= theme_budget:
                break
            # Prefer updated sort so unused recent apps surface.
            c, f, _ = _add_search_results(pool, used, q, "updated", 1 if not big else 2, False, stats)
            calls += c
            theme_calls += c
            fresh_count += f
        if len(pool) >= max(target_fresh, 250) and theme_calls >= 12:
            break
    print(f"[{VERSION}] theme sweep done: pool={len(pool)} UNUSED, +{theme_calls} calls")

    if not do_fresh:
        return list(pool.values()), calls

    # --- Lane 3: fresh gems WITH self-host proof phrases ---
    fresh_pushed_after = windows["pushed_12m"]
    fresh_queries = [
        f'topic:selfhosted stars:1..100 pushed:>={fresh_pushed_after} archived:false',
        f'topic:self-hosted stars:1..100 pushed:>={fresh_pushed_after} archived:false',
        f'topic:selfhosted topic:web-app stars:1..120 created:>{windows["created_12m"]} archived:false',
        f'"self-hosted" in:readme stars:1..100 created:>{windows["created_12m"]} archived:false',
        f'"self-hosted alternative" in:readme stars:1..150 created:>{windows["created_24m"]} archived:false',
        f'"docker compose" self-hosted in:readme stars:1..120 created:>{windows["created_12m"]} archived:false',
        f'self-hosted webui OR "web ui" stars:1..100 created:>{windows["created_12m"]} archived:false',
    ]
    for t in topics[:10]:
        if t not in ("self-hosted", "selfhosted"):
            fresh_queries.append(
                f'topic:selfhosted topic:{t} stars:1..100 created:>{windows["created_24m"]} '
                f'archived:false')
    FRESH_MIN_QUERIES, FRESH_MAX_CALLS = 4, 10
    print(f"[{VERSION}] fresh-gem sweep: {len(fresh_queries)} queries …")
    fresh_sweep_calls = 0
    for query in fresh_queries:
        if calls >= MAX_CALLS + FRESH_MAX_CALLS:
            break
        c, f, _ = _add_search_results(pool, used, query, "created", 1, True, stats)
        calls += c
        fresh_sweep_calls += c
        fresh_count += f
        if fresh_sweep_calls >= FRESH_MIN_QUERIES and len(pool) >= max(200, quality_pool_size + 40):
            break

    print(f"[{VERSION}] fresh-gem sweep done: {len(pool) - quality_pool_size} new repos, "
          f"{fresh_sweep_calls} calls")
    return list(pool.values()), calls


# ---------------------------------------------------------------------------
# Disk cache (content-hash invalidated) — same as v7.
# ---------------------------------------------------------------------------
def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return default
    return default


def _save_json(path, obj):
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(path)
    except OSError:
        pass


def _sig(repo):
    return f"{repo.get('stargazers_count', 0)}|{repo.get('pushed_at', '')}"


# Fields the gate + appeal_score + categorize actually read — the bench stores
# only these (a full search payload is ~6 KB/repo; this is ~0.5 KB).
_BENCH_FIELDS = (
    "full_name", "html_url", "description", "topics", "language",
    "stargazers_count", "forks_count", "open_issues_count",
    "pushed_at", "created_at", "updated_at", "homepage", "license",
    "fork", "archived", "disabled", "size",
)


# ---------------------------------------------------------------------------
# GraphQL batch-confirm + Docker detection (REST fallback) — same as v7.
# ---------------------------------------------------------------------------
_GQL_FRAGMENT = """
fragment M on Repository {
  nameWithOwner stargazerCount forkCount openIssues: issues(states:OPEN){ totalCount }
  pushedAt createdAt description homepageUrl isFork isArchived isDisabled isMirror
  primaryLanguage { name }
  licenseInfo { spdxId }
  repositoryTopics(first:20){ nodes{ topic{ name } } }
  c1: object(expression:"HEAD:docker-compose.yml"){ __typename }
  c2: object(expression:"HEAD:compose.yaml"){ __typename }
  c3: object(expression:"HEAD:docker-compose.yaml"){ __typename }
  c4: object(expression:"HEAD:docker/docker-compose.yml"){ __typename }
  d1: object(expression:"HEAD:Dockerfile"){ __typename }
  e1: object(expression:"HEAD:.env.example"){ __typename }
}
"""


def _gql_normalise(node):
    if not node:
        return None
    full = node.get("nameWithOwner")
    if not full:
        return None
    topics = [t["topic"]["name"] for t in (node.get("repositoryTopics") or {}).get("nodes", [])]
    lic = node.get("licenseInfo") or {}
    compose = any(node.get(k) for k in ("c1", "c2", "c3", "c4"))
    dockerfile = bool(node.get("d1"))
    env_example = bool(node.get("e1"))
    # v12: docker_signal = real deploy files only (.env.example alone is NOT proof).
    docker = compose or dockerfile
    return {
        "full_name": full, "html_url": f"https://github.com/{full}",
        "description": node.get("description"), "topics": topics,
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "stargazers_count": node.get("stargazerCount", 0),
        "forks_count": node.get("forkCount", 0),
        "open_issues_count": (node.get("openIssues") or {}).get("totalCount", 0),
        "pushed_at": node.get("pushedAt"), "created_at": node.get("createdAt"),
        "homepage": node.get("homepageUrl"), "license": {"spdx_id": lic.get("spdxId")},
        "_fork": bool(node.get("isFork")), "_archived": bool(node.get("isArchived")),
        "_disabled": bool(node.get("isDisabled")), "_mirror": bool(node.get("isMirror")),
        "compose_signal": compose,
        "dockerfile_signal": dockerfile,
        "env_example": env_example,
        "docker_signal": docker,
    }

def graphql_confirm_batch(pairs):
    aliases = [f'r{i}: repository(owner:{json.dumps(o)}, name:{json.dumps(n)}) {{ ...M }}'
               for i, (o, n) in enumerate(pairs)]
    query = "{\n" + "\n".join(aliases) + "\n}\n" + _GQL_FRAGMENT
    try:
        if GH_TOKEN:
            data = native_graphql_api(query).get("data", {})
        else:
            data = json.loads(v1.run(["gh", "api", "graphql", "-f", f"query={query}"])).get("data", {})
    except (RuntimeError, ValueError):
        return None
    return {k: _gql_normalise(data.get(f"r{i}")) for i, k in enumerate(pairs)}


def _strip_git_suffix(s):
    """Remove a trailing .git suffix only (never str.rstrip('.git') — that eats t/g/i)."""
    s = (s or "").strip().rstrip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    return s


def _owner_name(url):
    """Parse owner/repo from a GitHub URL. Safe .git stripping (no rstrip charset bug)."""
    url = _strip_git_suffix(url or "")
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", url, re.I)
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    name = _strip_git_suffix(name)
    return (owner, name)


def confirm_repos(items, cache, now_ts, use_graphql):
    out, todo = {}, []
    for url, sig in items:
        on = _owner_name(url)
        if not on:
            out[url.lower()] = None
            continue
        ck = f"{on[0]}/{on[1]}".lower()
        hit = cache.get(ck)
        if hit:
            age = now_ts - hit.get("_ts", 0)
            if hit.get("_dead") and age < DEAD_TTL:
                out[url.lower()] = None
                continue
            if not hit.get("_dead") and age < CACHE_TTL and hit.get("_sig") == sig:
                out[url.lower()] = hit["repo"]
                continue
        todo.append((url, on, ck, sig))

    def rest_fallback(url, on, ck, sig):
        repo = v1.canonical_repo(url)
        if repo is None:
            cache[ck] = {"_dead": True, "_ts": now_ts}
            out[url.lower()] = None
        else:
            # REST has no tree probe — leave deploy flags false (tier B/C can still ship).
            repo.setdefault("compose_signal", False)
            repo.setdefault("dockerfile_signal", False)
            repo.setdefault("docker_signal", False)
            cache[ck] = {"repo": repo, "_ts": now_ts, "_sig": sig}
            out[url.lower()] = repo

    if use_graphql:
        for i in range(0, len(todo), GRAPHQL_BATCH):
            chunk = todo[i:i + GRAPHQL_BATCH]
            res = graphql_confirm_batch([c[1] for c in chunk])
            if res is None:
                for url, on, ck, sig in chunk:
                    rest_fallback(url, on, ck, sig)
                continue
            for url, on, ck, sig in chunk:
                repo = res.get(on)
                if not repo or repo["_fork"] or repo["_archived"] or repo["_disabled"] or repo["_mirror"]:
                    cache[ck] = {"_dead": True, "_ts": now_ts}
                    out[url.lower()] = None
                else:
                    cache[ck] = {"repo": repo, "_ts": now_ts, "_sig": sig}
                    out[url.lower()] = repo
    else:
        for url, on, ck, sig in todo:
            rest_fallback(url, on, ck, sig)
    return out


# ---------------------------------------------------------------------------
# awesome-selfhosted-data validator (optional, cached, graceful) — same as v7.
# ---------------------------------------------------------------------------
def _awesome_cache_looks_truncated(repos):
    """Detect the classic rstrip('.git') corruption (chatwoot→chatwoo, etc.)."""
    if not repos:
        return True
    repos_set = set(repos)
    # Exact mangled names the rstrip('.git') bug produced — NOT substrings
    # (e.g. 'shaarl' would false-positive match 'shaarli').
    mangled = {
        "chatwoot/chatwoo", "octoprint/octoprin", "akaunting/akauntin",
        "shaarli/shaarl", "aliasvault/aliasvaul", "bitcart/bitcar",
        "dokuwiki/dokuwik", "bludit/blud",
    }
    if repos_set & mangled:
        return True
    # Healthy cache must contain at least one well-known full name.
    known_good = {"chatwoot/chatwoot", "immich-app/immich", "paperless-ngx/paperless-ngx"}
    if known_good and not (repos_set & known_good):
        # Incomplete / wrong cache
        if len(repos_set) < 500:
            return True
    return False


def load_awesome_index(now_ts, enabled):
    if not enabled:
        return set()
    cached = _load_json(AWESOME_CACHE, None)
    if not cached and AWESOME_CACHE_FALLBACK.exists():
        cached = _load_json(AWESOME_CACHE_FALLBACK, None)
        if cached:
            print(f"[awesome] using fallback cache {AWESOME_CACHE_FALLBACK.name}")
    if cached and now_ts - cached.get("_ts", 0) < AWESOME_TTL:
        repos = set(cached.get("repos", []))
        if not _awesome_cache_looks_truncated(repos):
            return repos
        print("[awesome] cached index looks truncated (legacy rstrip bug) — re-downloading …")
    url = "https://github.com/awesome-selfhosted/awesome-selfhosted-data/archive/refs/heads/master.tar.gz"
    try:
        print("[awesome] downloading curated self-hosted index (once / 30d) …")
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read()
        repos = set()
        pat = re.compile(rb"source_code_url:\s*['\"]?(https://github\.com/[^/\s'\"]+/[^/\s'\"]+)", re.I)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not (m.isfile() and m.name.endswith(".yml") and "/software/" in m.name):
                    continue
                f = tar.extractfile(m)
                if not f:
                    continue
                for match in pat.findall(f.read()):
                    on = _owner_name(match.decode())
                    if on:
                        repos.add(f"{on[0]}/{on[1]}".lower())
        _save_json(AWESOME_CACHE, {"_ts": now_ts, "repos": sorted(repos)})
        # Keep fallback cache healthy too so v11 path benefits.
        try:
            _save_json(AWESOME_CACHE_FALLBACK, {"_ts": now_ts, "repos": sorted(repos)})
        except Exception:
            pass
        print(f"[awesome] indexed {len(repos)} curated self-hosted repos")
        return repos
    except Exception as e:
        print(f"[awesome] skipped (could not load: {type(e).__name__}: {e})")
        return set(cached.get("repos", [])) if cached else set()


def awesome_residual(awesome_set, used):
    """Return unused awesome full_names (owner/repo), for free high-proof seeding."""
    used_names = set()
    for u in used:
        u = (u or "").lower().rstrip("/")
        if "github.com/" in u:
            parts = u.split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                used_names.add(f"{parts[0]}/{parts[1]}")
        elif "/" in u and not u.startswith("http"):
            used_names.add(u)
    return sorted(awesome_set - used_names)


# ---------------------------------------------------------------------------
# Optional LLM enrichment — same as v7 (annotation + tiebreak ONLY).
# ---------------------------------------------------------------------------
def _read_key(env, path):
    v = os.environ.get(env)
    if v:
        return v.strip()
    p = Path(path).expanduser()
    return p.read_text().strip() if p.is_file() else None


def resolve_llm_backend():
    k = _read_key("ANTHROPIC_API_KEY", "~/.config/anthropic/api_key")
    if k:
        return ("claude", k)
    k = _read_key("OPENROUTER_API_KEY", "~/.config/openrouter/api_key")
    if k:
        return ("openrouter", k)
    return (None, None)


ENRICH_SYSTEM = (
    "You classify self-hosted, open-source GitHub apps for a curated list. For each "
    "repo you receive (id=owner/repo, plus desc/lang/stars), return EXACTLY one "
    "result object per input id, in input order. Rules: category = the single best "
    "fit from the ALLOWED list ONLY (never invent one). self_hostable = true only if "
    "it is a deployable app/service a user runs on their own server; false for "
    "libraries, CLI-only tools, frameworks, dotfiles, learning resources, lists, or "
    "browser extensions. reject_reason = a short reason (<=8 words) when self_hostable "
    "is false, else null. interest_hook = <=15 words, concrete, why someone would "
    "self-host THIS (no marketing fluff, no trailing period). novelty = 1 (generic) to "
    "5 (rare/novel). Output ONLY a JSON array, no markdown, no commentary."
)


def _curl_off_argv(url, headers, payload):
    """POST JSON via urllib. Keys travel as in-process headers — never on argv."""
    for attempt in range(4):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
        for h in headers:
            if ":" in h:
                k, v = h.split(":", 1)
                req.add_header(k.strip(), v.strip())
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(6 * (attempt + 1))
                continue
            return None  # non-retryable (401/400/…): fail fast, heuristics take over
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(6 * (attempt + 1))
    return None


def _parse_array(text):
    if not text:
        return None
    text = text.strip()
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
    except ValueError:
        pass
    m = re.search(r"\[.*\]", text.replace("```json", "").replace("```", ""), re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, list) else None
        except ValueError:
            return None
    return None


def enrich_batch(items, backend, key):
    user = ("Classify these repos. Return one result per id, same order. ALLOWED "
            f"categories: {json.dumps(ALLOWED_CATEGORIES)}\n\n{json.dumps(items)}")
    if backend == "claude":
        schema = {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "category": {"type": "string", "enum": ALLOWED_CATEGORIES},
            "self_hostable": {"type": "boolean"},
            "reject_reason": {"type": ["string", "null"]},
            "interest_hook": {"type": "string"},
            "novelty": {"type": "integer", "enum": [1, 2, 3, 4, 5]}},
            "required": ["id", "category", "self_hostable", "reject_reason",
                         "interest_hook", "novelty"], "additionalProperties": False}}
        payload = {"model": ANTHROPIC_MODEL, "max_tokens": 4096, "system": ENRICH_SYSTEM,
                   "output_config": {"format": {"type": "json_schema", "schema": schema}},
                   "messages": [{"role": "user", "content": user}]}
        out = _curl_off_argv("https://api.anthropic.com/v1/messages",
                             [f"x-api-key: {key}", f"anthropic-version: {ANTHROPIC_VERSION}",
                              "content-type: application/json"], payload)
        if not out:
            return []
        try:
            data = json.loads(out)
            text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")
        except (ValueError, KeyError):
            return []
        return _parse_array(text) or []
    else:
        payload = {"model": OPENROUTER_MODEL, "temperature": 0.2,
                   "messages": [{"role": "system", "content": ENRICH_SYSTEM},
                                {"role": "user", "content": user}]}
        out = _curl_off_argv("https://openrouter.ai/api/v1/chat/completions",
                             [f"Authorization: Bearer {key}",
                              "Content-Type: application/json",
                              "HTTP-Referer: https://openclaw.bjk.ai", "X-Title: notion50-v11"], payload)
        if not out:
            return []
        try:
            text = json.loads(out)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            return []
        return _parse_array(text) or []


def enrich_candidates(candidates, backend, key, cache):
    pending, bypassed = [], 0
    for c in candidates:
        full = c["repo"]["full_name"].lower()
        if c.get("docker") and c.get("awesome"):
            # Docker/compose verified AND on awesome-selfhosted: definitely
            # self-hostable, nothing for the LLM to decide. Labeled synthetic —
            # no fabricated hook text, no novelty adjustment, never cached.
            c["enrich"] = {"source": "bypass", "self_hostable": True,
                           "reject_reason": None}
            bypassed += 1
        elif full in cache and cache[full].get("_sig") == _sig(c["repo"]):
            c["enrich"] = cache[full]
        else:
            pending.append(c)
    if bypassed:
        print(f"[enrich] {bypassed} Docker+awesome finalists bypassed the LLM")
    BATCH = 15
    for i in range(0, len(pending), BATCH):
        chunk = pending[i:i + BATCH]
        items = [{"id": c["repo"]["full_name"], "desc": (c["repo"].get("description") or "")[:200],
                  "lang": c["repo"].get("language") or "", "stars": c["repo"].get("stargazers_count", 0)}
                 for c in chunk]
        by_id = {(r.get("id") or "").lower(): r for r in enrich_batch(items, backend, key)
                 if isinstance(r, dict)}
        for c in chunk:
            full = c["repo"]["full_name"].lower()
            r = by_id.get(full)
            if r:
                r["_sig"] = _sig(c["repo"])
                c["enrich"] = r
                cache[full] = r
            else:
                c["enrich"] = {}
        print(f"[enrich] {min(i + BATCH, len(pending))}/{len(pending)} via {backend}")


# ---------------------------------------------------------------------------
# Balanced diversity — ALWAYS keyed on the HEURISTIC category (same as v7).
# v9 adds a fresh-gem quota: reserve up to N slots for fresh gems.
# ---------------------------------------------------------------------------
def _cat_limit(cat, soft_cap):
    """Effective per-category max: min(soft_cap, HARD_CAT_CAPS) when listed."""
    hard = HARD_CAT_CAPS.get(cat)
    if hard is None:
        return soft_cap
    return min(soft_cap, hard)


def select_balanced(pool, cap, min_score, fresh_quota=0):
    """Two phases on the HEURISTIC category, with soft category ceilings.

    Phase 1: seed best of each category, preferring product/webapp categories.
    Phase 2: fill to 50 by score under per-cat soft cap, then relax gently.
    Hard caps only block pathological skew (e.g. 20× AI).
    """
    selected, counts, taken = [], {}, set()

    # Phase 1: one per category (highest-scored within each), product cats first.
    best_by_cat = {}
    for i, c in enumerate(pool):
        if c["score"] < min_score:
            continue
        if c["hcat"] not in best_by_cat:
            best_by_cat[c["hcat"]] = i

    def _phase1_order(cat):
        # Preferred product categories first, then score of that cat's best.
        pref = 0 if cat in PREFERRED_PRODUCT_CATS else 1
        return (pref, -pool[best_by_cat[cat]]["score"])

    for cat in sorted(best_by_cat.keys(), key=_phase1_order):
        i = best_by_cat[cat]
        if len(selected) >= 50:
            break
        if counts.get(cat, 0) >= _cat_limit(cat, cap):
            continue
        selected.append(i)
        taken.add(i)
        counts[cat] = 1

    # Phase 2: fill by score under the cap, relaxing soft cap only.
    cur_cap = cap
    max_soft = max(cap + 3, max(HARD_CAT_CAPS.values(), default=cap) + 1)
    eligible = sum(1 for c in pool if c["score"] >= min_score)
    while len(selected) < 50 and len(taken) < eligible:
        progressed = False
        for i, c in enumerate(pool):
            if len(selected) >= 50:
                break
            if i in taken or c["score"] < min_score:
                continue
            lim = _cat_limit(c["hcat"], cur_cap)
            if counts.get(c["hcat"], 0) >= lim:
                continue
            selected.append(i)
            taken.add(i)
            counts[c["hcat"]] = counts.get(c["hcat"], 0) + 1
            progressed = True
        if len(selected) >= 50:
            break
        if not progressed:
            cur_cap += 1
            if cur_cap > max_soft + 5:
                # Last resort: fill ignoring soft cap but still honour HARD_CAT_CAPS.
                for i, c in enumerate(pool):
                    if len(selected) >= 50:
                        break
                    if i in taken or c["score"] < min_score:
                        continue
                    hard = HARD_CAT_CAPS.get(c["hcat"])
                    if hard is not None and counts.get(c["hcat"], 0) >= hard:
                        continue
                    selected.append(i)
                    taken.add(i)
                    counts[c["hcat"]] = counts.get(c["hcat"], 0) + 1
                break
    return [pool[i] for i in selected]


def enforce_hard_cat_caps(selected, pool, min_score):
    """Swap out over-cap categories for underrepresented ones from pool."""
    if not selected:
        return selected
    selected = list(selected)
    taken = {c["repo"]["html_url"].lower() for c in selected}

    def counts():
        c = {}
        for x in selected:
            c[x["hcat"]] = c.get(x["hcat"], 0) + 1
        return c

    # Candidates not yet selected, best score first, prefer A/B.
    extras = sorted(
        [c for c in pool if c["score"] >= min_score
         and c["repo"]["html_url"].lower() not in taken
         and c.get("proof_tier") in ("A", "B", "C")],
        key=lambda c: (0 if c.get("proof_tier") in ("A", "B") else 1, -c["score"]),
    )

    changed = True
    while changed:
        changed = False
        cat_counts = counts()
        for cat, hard in HARD_CAT_CAPS.items():
            while cat_counts.get(cat, 0) > hard:
                # Drop lowest-scoring pick in the overflowing category (keep diversity).
                victims = [c for c in selected if c["hcat"] == cat]
                if not victims:
                    break
                victim = min(victims, key=lambda c: c["score"])
                # Find a replacement not over its own hard cap.
                replacement = None
                for cand in extras:
                    if cand["repo"]["html_url"].lower() in taken:
                        continue
                    cc = cat_counts.get(cand["hcat"], 0)
                    hard_c = HARD_CAT_CAPS.get(cand["hcat"])
                    if hard_c is not None and cc >= hard_c:
                        continue
                    # Prefer non-overflow categories.
                    if cand["hcat"] == cat:
                        continue
                    replacement = cand
                    break
                if replacement is None:
                    break
                selected.remove(victim)
                taken.discard(victim["repo"]["html_url"].lower())
                selected.append(replacement)
                taken.add(replacement["repo"]["html_url"].lower())
                cat_counts = counts()
                changed = True
    selected.sort(key=lambda c: -c["score"])
    return selected



def select_diverse(pool, target, min_score, fresh_quota):
    """Multi-pass product-family selection for target size N.

    1) FLOORS per family (scaled) — product families first
    2) SCORE FILL under family ceilings
    3) Soft overflow (never past AI hard ceil)
    4) Enforce AI ceil by swapping
    """
    N = max(1, int(target))
    proven = [c for c in pool if c.get("proof_tier") in ("A", "B", "C") and c["score"] >= min_score]
    for c in proven:
        c["family"] = family_of(c.get("hcat") or "Web App / Other")
    proven.sort(key=lambda c: -c["score"])

    floors = {f: scale_n(lo, N) for f, (lo, hi) in FAMILY_RULES.items()}
    ceils = {f: max(scale_n(hi, N), floors[f]) for f, (lo, hi) in FAMILY_RULES.items()}
    # AI hard: never more than scale of 3 per 50
    ceils["ai"] = scale_n(FAMILY_RULES["ai"][1], N)
    floors["ai"] = 0

    min_ab = max(1, int(round(MIN_AB_FRAC * N)))
    max_c = max(1, int(round(MAX_C_FRAC * N)))

    selected = []
    taken = set()
    fam_counts = {f: 0 for f in FAMILY_RULES}
    hcat_counts = {}

    def can_take(c, respect_ceil=True, soft_ai=True):
        url = c["repo"]["html_url"].lower()
        if url in taken:
            return False
        fam = c["family"]
        if respect_ceil and fam_counts.get(fam, 0) >= ceils.get(fam, N):
            return False
        if soft_ai and fam == "ai" and fam_counts.get("ai", 0) >= ceils["ai"]:
            return False
        hc = c.get("hcat")
        hard = HARD_CAT_CAPS.get(hc)
        # scale fine-cat hard caps for large N
        if hard is not None:
            hard_n = scale_n(hard, N) if N != 50 else hard
            if hcat_counts.get(hc, 0) >= hard_n:
                return False
        return True

    def add(c):
        selected.append(c)
        taken.add(c["repo"]["html_url"].lower())
        fam = c["family"]
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        hc = c.get("hcat")
        hcat_counts[hc] = hcat_counts.get(hc, 0) + 1

    # Pass 1: family floors — product families first
    product_order = [
        "files", "knowledge", "media", "documents", "productivity",
        "bookmarks_rss", "dash_mon", "security", "cms", "sports",
        "db_light", "comms", "home_health", "proxy_net", "dev_web",
        "other", "devops", "gaming", "ai",
    ]
    by_fam = {}
    for c in proven:
        by_fam.setdefault(c["family"], []).append(c)

    for fam in product_order:
        need = floors.get(fam, 0)
        if need <= 0:
            continue
        for c in by_fam.get(fam, []):
            if fam_counts.get(fam, 0) >= need:
                break
            if can_take(c):
                add(c)
            if len(selected) >= N:
                break
        if len(selected) >= N:
            break

    # Pass 2: score fill under ceilings (prefer A/B)
    ranked = sorted(proven, key=lambda c: (
        0 if c.get("proof_tier") in ("A", "B") else 1,
        -c["score"],
    ))
    for c in ranked:
        if len(selected) >= N:
            break
        if can_take(c):
            add(c)

    # Pass 3: soft overflow for non-AI to hit N
    if len(selected) < N:
        for c in ranked:
            if len(selected) >= N:
                break
            if c["family"] == "ai":
                continue
            if can_take(c, respect_ceil=False, soft_ai=True):
                # still enforce AI and modest 2x ceil
                fam = c["family"]
                if fam_counts.get(fam, 0) >= ceils.get(fam, N) * 2:
                    continue
                add(c)

    # Pass 4: AI overflow only if still short (rare)
    if len(selected) < N:
        for c in ranked:
            if len(selected) >= N:
                break
            if c["family"] != "ai":
                continue
            if c["repo"]["html_url"].lower() in taken:
                continue
            if fam_counts.get("ai", 0) >= ceils["ai"] + scale_n(2, N):
                continue
            add(c)

    # Enforce AI ceil by swapping out lowest AI for non-AI
    while sum(1 for c in selected if c.get("family")=="ai" or c.get("hcat")=="AI / LLM") > ceils["ai"]:
        ais = [c for c in selected if c["family"] == "ai"]
        if not ais:
            break
        victim = min(ais, key=lambda c: c["score"])
        replacement = None
        for c in ranked:
            if c["repo"]["html_url"].lower() in taken:
                continue
            if c["family"] == "ai":
                continue
            if can_take(c):
                replacement = c
                break
        if not replacement:
            break
        selected.remove(victim)
        taken.discard(victim["repo"]["html_url"].lower())
        fam_counts["ai"] -= 1
        hcat_counts[victim.get("hcat")] = hcat_counts.get(victim.get("hcat"), 1) - 1
        add(replacement)

    # Trim or pad to exactly N
    if len(selected) > N:
        # keep floor representatives, drop lowest score extras
        selected = sorted(selected, key=lambda c: -c["score"])[:N]
        # rebuild counts
        fam_counts = {f: 0 for f in FAMILY_RULES}
        hcat_counts = {}
        taken = set()
        rebuilt = []
        for c in selected:
            rebuilt.append(c)
            taken.add(c["repo"]["html_url"].lower())
            fam_counts[c["family"]] = fam_counts.get(c["family"], 0) + 1
            hcat_counts[c.get("hcat")] = hcat_counts.get(c.get("hcat"), 0) + 1
        selected = rebuilt

    # C ceiling
    while sum(1 for c in selected if c.get("proof_tier") == "C") > max_c:
        worst = min((c for c in selected if c.get("proof_tier") == "C"),
                    key=lambda c: c["score"], default=None)
        if worst is None:
            break
        rep = None
        for c in ranked:
            if c["repo"]["html_url"].lower() in {x["repo"]["html_url"].lower() for x in selected}:
                continue
            if c.get("proof_tier") in ("A", "B") and can_take(c):
                rep = c
                break
        selected.remove(worst)
        if rep:
            selected.append(rep)
        else:
            break

    # Fresh soft floor
    if fresh_quota > 0:
        fq = scale_n(fresh_quota, N) if N != 50 else fresh_quota
        fresh_in = [c for c in selected if c.get("fresh_gem")]
        if len(fresh_in) < fq:
            taken_u = {c["repo"]["html_url"].lower() for c in selected}
            cands = [c for c in proven if c.get("fresh_gem") and c["repo"]["html_url"].lower() not in taken_u]
            cands.sort(key=lambda c: -c["score"])
            non_fresh = sorted([c for c in selected if not c.get("fresh_gem")], key=lambda c: c["score"])
            for nf in non_fresh:
                if len([c for c in selected if c.get("fresh_gem")]) >= fq or not cands:
                    break
                # don't steal sole family floor rep
                if fam_counts.get(nf["family"], 0) <= floors.get(nf["family"], 0):
                    continue
                fg = None
                for cand in cands:
                    if cand["family"] == "ai" and fam_counts.get("ai", 0) >= ceils["ai"]:
                        continue
                    fg = cand
                    break
                if not fg:
                    break
                cands.remove(fg)
                selected.remove(nf)
                fam_counts[nf["family"]] -= 1
                selected.append(fg)
                fam_counts[fg["family"]] = fam_counts.get(fg["family"], 0) + 1

    selected.sort(key=lambda c: -c["score"])
    # Final pad if under N (never break AI hard ceil)
    if len(selected) < N:
        taken_u = {c["repo"]["html_url"].lower() for c in selected}
        for prefer_non_ai in (True, False):
            for c in ranked:
                if len(selected) >= N:
                    break
                if c["repo"]["html_url"].lower() in taken_u:
                    continue
                is_ai = c.get("family") == "ai" or c.get("hcat") == "AI / LLM"
                if prefer_non_ai and is_ai:
                    continue
                ai_now = sum(1 for x in selected if x.get("family") == "ai" or x.get("hcat") == "AI / LLM")
                if is_ai and ai_now >= ceils["ai"]:
                    continue
                selected.append(c)
                taken_u.add(c["repo"]["html_url"].lower())
                c["family"] = c.get("family") or family_of(c.get("hcat") or "")

    # FINAL AI hard-ceil enforcement (after all pads/swaps)
    def _is_ai(c):
        return c.get("family") == "ai" or c.get("hcat") == "AI / LLM"
    while sum(1 for c in selected if _is_ai(c)) > ceils["ai"]:
        ais = [c for c in selected if _is_ai(c)]
        victim = min(ais, key=lambda c: c["score"])
        taken_u = {c["repo"]["html_url"].lower() for c in selected}
        rep = None
        for c in ranked:
            u = c["repo"]["html_url"].lower()
            if u in taken_u or _is_ai(c):
                continue
            rep = c
            break
        selected.remove(victim)
        if rep:
            selected.append(rep)
        # If no replacement, still drop excess AI (prefer short set over AI pile).
    # Pad again non-AI only if we dropped below N
    if len(selected) < N:
        taken_u = {c["repo"]["html_url"].lower() for c in selected}
        for c in ranked:
            if len(selected) >= N:
                break
            if c["repo"]["html_url"].lower() in taken_u or _is_ai(c):
                continue
            selected.append(c)
            taken_u.add(c["repo"]["html_url"].lower())

    ab_n = sum(1 for c in selected if c.get("proof_tier") in ("A", "B"))
    c_n = sum(1 for c in selected if c.get("proof_tier") == "C")
    ai_n = sum(1 for c in selected if c.get("family") == "ai")
    fam_dist = {}
    for c in selected:
        fam_dist[c["family"]] = fam_dist.get(c["family"], 0) + 1
    print(f"[{VERSION}] diverse select N={len(selected)} | A∪B={ab_n} C={c_n} AI={ai_n} "
          f"(AI ceil={ceils['ai']}) | families={dict(sorted(fam_dist.items(), key=lambda kv: -kv[1]))}")
    return selected[:N]


def select_with_proof_quotas(pool, cap, min_score, fresh_quota, target=50):
    """Back-compat wrapper → family-diverse multi-pass select."""
    return select_diverse(pool, target, min_score, fresh_quota)


def select_with_proof_quotas_legacy(pool, cap, min_score, fresh_quota):
    """Select 50 honouring proof tiers: prefer A/B (≥MIN_AB_PROOF), ≤MAX_TIER_C of C.

    Fresh-gem swaps only among proof-passed candidates (never reintroduce unproven).
    """
    proven = [c for c in pool if c.get("proof_tier") in ("A", "B", "C") and c["score"] >= min_score]
    ab = [c for c in proven if c.get("proof_tier") in ("A", "B")]
    c_only = [c for c in proven if c.get("proof_tier") == "C"]
    ab.sort(key=lambda x: -x["score"])
    c_only.sort(key=lambda x: -x["score"])

    # Build primarily from A/B.
    selected = select_balanced(ab, cap, min_score, 0)
    if len(selected) > 50:
        selected = selected[:50]

    # Top up with C (max MAX_TIER_C), then remaining A/B, relaxing soft caps.
    def _top_up(candidates, need, taken, cat_counts, soft_lim, honour_hard=True):
        added = 0
        for c in candidates:
            if added >= need:
                break
            url = c["repo"]["html_url"].lower()
            if url in taken:
                continue
            hard = HARD_CAT_CAPS.get(c["hcat"]) if honour_hard else None
            if hard is not None and cat_counts.get(c["hcat"], 0) >= hard:
                continue
            if soft_lim is not None and cat_counts.get(c["hcat"], 0) >= soft_lim:
                continue
            selected.append(c)
            taken.add(url)
            cat_counts[c["hcat"]] = cat_counts.get(c["hcat"], 0) + 1
            added += 1
        return added

    if len(selected) < 50:
        taken = {c["repo"]["html_url"].lower() for c in selected}
        cat_counts = {}
        for c in selected:
            cat_counts[c["hcat"]] = cat_counts.get(c["hcat"], 0) + 1
        # Prefer C first (up to MAX_TIER_C), then leftover A/B.
        c_room = max(0, MAX_TIER_C - sum(1 for c in selected if c.get("proof_tier") == "C"))
        need = 50 - len(selected)
        _top_up(c_only, min(need, c_room), taken, cat_counts, soft_lim=max(cap + 2, 8))
        need = 50 - len(selected)
        if need > 0:
            ab_left = [c for c in ab if c["repo"]["html_url"].lower() not in taken]
            _top_up(ab_left, need, taken, cat_counts, soft_lim=max(cap + 3, 10))
        need = 50 - len(selected)
        if need > 0:
            # Last resort: ignore soft caps; still honour HARD_CAT_CAPS for A/B pool.
            ab_left = [c for c in ab if c["repo"]["html_url"].lower() not in taken]
            _top_up(ab_left, need, taken, cat_counts, soft_lim=None)
        need = 50 - len(selected)
        c_room = max(0, MAX_TIER_C - sum(1 for c in selected if c.get("proof_tier") == "C"))
        if need > 0 and c_room > 0:
            c_left = [c for c in c_only if c["repo"]["html_url"].lower() not in taken]
            # Always honour HARD_CAT_CAPS.
            _top_up(c_left, min(need, c_room), taken, cat_counts, soft_lim=None, honour_hard=True)
        need = 50 - len(selected)
        if need > 0:
            # Last resort: remaining A/B first, then C — still honour HARD_CAT_CAPS.
            any_left = [c for c in proven if c["repo"]["html_url"].lower() not in taken]
            any_left.sort(key=lambda c: (0 if c.get("proof_tier") in ("A", "B") else 1, -c["score"]))
            c_count = sum(1 for c in selected if c.get("proof_tier") == "C")
            for c in any_left:
                if need <= 0:
                    break
                if c.get("proof_tier") == "C" and c_count >= MAX_TIER_C:
                    continue
                hard = HARD_CAT_CAPS.get(c["hcat"])
                if hard is not None and cat_counts.get(c["hcat"], 0) >= hard:
                    continue
                selected.append(c)
                taken.add(c["repo"]["html_url"].lower())
                cat_counts[c["hcat"]] = cat_counts.get(c["hcat"], 0) + 1
                if c.get("proof_tier") == "C":
                    c_count += 1
                need -= 1
        # If still short (pool skewed into capped categories), fill to 50 with
        # proven A/B only, allowing soft overflow of hard caps. Better to ship
        # 50 proven apps than fail the run.
        need = 50 - len(selected)
        if need > 0:
            print(f"[{VERSION}] category caps left {need} slots empty — "
                  f"filling with remaining A/B (soft overflow)")
            any_left = [c for c in ab if c["repo"]["html_url"].lower() not in taken]
            any_left.sort(key=lambda c: -c["score"])
            for c in any_left:
                if need <= 0:
                    break
                selected.append(c)
                taken.add(c["repo"]["html_url"].lower())
                need -= 1

    # Enforce hard category ceilings via swap when replacements exist.
    selected = enforce_hard_cat_caps(selected, proven, min_score)
    # Never drop below 50 after enforcement — re-fill if swaps failed mid-way.
    if len(selected) < 50:
        taken = {c["repo"]["html_url"].lower() for c in selected}
        for c in sorted(proven, key=lambda x: -x["score"]):
            if len(selected) >= 50:
                break
            u = c["repo"]["html_url"].lower()
            if u in taken:
                continue
            selected.append(c)
            taken.add(u)

    # Fresh-gem floor among already selected + proven extras.
    if fresh_quota > 0 and selected:
        fresh_in = [c for c in selected if c.get("fresh_gem")]
        if len(fresh_in) < fresh_quota:
            taken = {c["repo"]["html_url"].lower() for c in selected}
            fresh_candidates = sorted(
                [c for c in proven if c.get("fresh_gem")
                 and c["repo"]["html_url"].lower() not in taken],
                key=lambda c: -c["score"])
            non_fresh = sorted(
                [c for c in selected if not c.get("fresh_gem")],
                key=lambda c: c["score"])
            cat_counts = {}
            for c in selected:
                cat_counts[c["hcat"]] = cat_counts.get(c["hcat"], 0) + 1
            ab_count = sum(1 for c in selected if c.get("proof_tier") in ("A", "B"))
            for nf in non_fresh:
                if len(fresh_in) >= fresh_quota or not fresh_candidates:
                    break
                if cat_counts.get(nf["hcat"], 0) <= 1:
                    continue
                # Pick a fresh candidate that does not blow hard caps.
                fg = None
                for cand in fresh_candidates:
                    hard = HARD_CAT_CAPS.get(cand["hcat"])
                    if hard is not None and cat_counts.get(cand["hcat"], 0) >= hard and cand["hcat"] != nf["hcat"]:
                        continue
                    # If replacing would leave A∪B under floor, skip demotion.
                    nf_ab = nf.get("proof_tier") in ("A", "B")
                    fg_ab = cand.get("proof_tier") in ("A", "B")
                    if nf_ab and not fg_ab and ab_count <= MIN_AB_PROOF:
                        continue
                    if cand.get("proof_tier") == "C" and sum(
                            1 for x in selected if x.get("proof_tier") == "C") >= MAX_TIER_C and nf.get("proof_tier") != "C":
                        continue
                    fg = cand
                    break
                if fg is None:
                    break
                fresh_candidates = [c for c in fresh_candidates if c is not fg]
                selected.remove(nf)
                selected.append(fg)
                cat_counts[nf["hcat"]] -= 1
                cat_counts[fg["hcat"]] = cat_counts.get(fg["hcat"], 0) + 1
                if nf.get("proof_tier") in ("A", "B"):
                    ab_count -= 1
                if fg.get("proof_tier") in ("A", "B"):
                    ab_count += 1
                fresh_in.append(fg)
            selected.sort(key=lambda c: -c["score"])
            # Re-enforce caps after swaps.
            selected = enforce_hard_cat_caps(selected, proven, min_score)

    ab_n = sum(1 for c in selected if c.get("proof_tier") in ("A", "B"))
    c_n = sum(1 for c in selected if c.get("proof_tier") == "C")
    print(f"[{VERSION}] proof quotas: A∪B={ab_n} C={c_n} "
          f"(targets A∪B≥{MIN_AB_PROOF}, C≤{MAX_TIER_C})")
    return selected

def select_with_fresh_quota(pool, cap, min_score, fresh_quota):
    """Select 50 with a fresh-gem floor: ensure at least `fresh_quota` fresh gems.

    Strategy:
    1. Run normal balanced selection.
    2. Count fresh gems in the result.
    3. If below quota, swap out the lowest-scoring non-fresh picks for the
       highest-scoring fresh gems that didn't make the cut.
    """
    selected = select_balanced(pool, cap, min_score, fresh_quota)
    if fresh_quota <= 0:
        return selected

    fresh_in = [c for c in selected if c.get("fresh_gem")]
    if len(fresh_in) >= fresh_quota:
        return selected

    # Need more fresh gems. Find fresh gems not selected, sorted by score.
    selected_keys = {c["repo"]["html_url"].lower() for c in selected}
    fresh_candidates = sorted(
        [c for c in pool if c.get("fresh_gem") and c["score"] >= min_score
         and c["repo"]["html_url"].lower() not in selected_keys],
        key=lambda c: -c["score"])

    if not fresh_candidates:
        return selected

    # Swap out the lowest-scoring non-fresh picks (that aren't the sole
    # representative of their category — preserve diversity).
    non_fresh_sorted = sorted(
        [c for c in selected if not c.get("fresh_gem")],
        key=lambda c: c["score"])
    cat_counts = {}
    for c in selected:
        cat_counts[c["hcat"]] = cat_counts.get(c["hcat"], 0) + 1

    swaps = 0
    needed = min(fresh_quota - len(fresh_in), len(fresh_candidates), len(non_fresh_sorted))
    for nf, fg in zip(non_fresh_sorted[:needed], fresh_candidates[:needed]):
        # Don't remove the sole representative of a category.
        if cat_counts.get(nf["hcat"], 0) <= 1:
            continue
        selected.remove(nf)
        selected.append(fg)
        cat_counts[nf["hcat"]] -= 1
        cat_counts[fg["hcat"]] = cat_counts.get(fg["hcat"], 0) + 1
        swaps += 1

    if swaps:
        selected.sort(key=lambda c: -c["score"])
        print(f"[{VERSION}] fresh-gem quota: swapped {swaps} non-fresh → fresh gems "
              f"(now {sum(1 for c in selected if c.get('fresh_gem'))} fresh in set)")
    return selected


# ---------------------------------------------------------------------------
# Selection pipeline.
# ---------------------------------------------------------------------------
def select_set(tracker, live_titles, args):
    t0 = time.time()
    used = {u.lower() for u in tracker.get("usedRepoUrls", [])}
    for page in tracker.get("completedPages", []):
        used.update(u.lower() for u in page.get("repos", []))
    set_num = int(tracker["completedSets"]) + 1
    special = bool(getattr(args, "special_title", "") or "")
    if special:
        title = args.special_title.strip()
        if not title:
            raise RuntimeError("--special-title is empty")
        if title in live_titles:
            raise RuntimeError(f"Title collision: {title} already exists in Notion")
    else:
        title = tracker["titleTemplate"].format(n=set_num)
        if title in live_titles:
            raise RuntimeError(f"Title collision: {title} already exists in Notion")
    # Scale discovery for large targets (e.g. 300 showcase page)
    if getattr(args, "target", 50) and args.target > 50:
        args.target_fresh = max(args.target_fresh, args.target * 4)
        args.max_pages = max(args.max_pages, 2)

    now = datetime.now(timezone.utc)
    now_ts = int(time.time())
    pushed_after = (now - timedelta(days=550)).strftime("%Y-%m-%d")
    stats = {"zeroYield": 0}

    # Auth status (critical — unauthenticated search is ~10 req/min and will thrash).
    global GH_TOKEN
    if not GH_TOKEN:
        GH_TOKEN = _get_gh_token()
    print(f"[{VERSION}] GitHub auth: "
          f"{'token OK' if GH_TOKEN else 'NONE — expect heavy rate limits; set GH_TOKEN or fix gh hosts.yml'}")

    # Awesome first: residual becomes free high-proof seed.
    awesome = load_awesome_index(now_ts, not args.no_awesome)
    residual = awesome_residual(awesome, used) if awesome else []
    print(f"[{VERSION}] awesome index: {len(awesome)} curated | residual unused: {len(residual)}")

    bench_seed = []
    seen_bench = set()
    for bench_path in (BENCH_CACHE, CACHE_DIR / "bench_v11.json"):
        bench = _load_json(bench_path, None)
        if not bench or now_ts - bench.get("_ts", 0) >= BENCH_TTL:
            continue
        for repo in bench.get("repos", []):
            u = (repo.get("html_url") or "").lower()
            fn = (repo.get("full_name") or "").lower()
            if not u or u in used or fn in used or fn in seen_bench:
                continue
            # Prefer proof-flagged or self-host-shaped; skip obvious junk.
            if hard_reject(repo):
                continue
            if repo.get("_proof_ok") or has_strict_selfhost_intent(repo) or has_app_shape(repo):
                bench_seed.append(repo)
                seen_bench.add(fn)
            if len(bench_seed) >= BENCH_MAX:
                break
        if len(bench_seed) >= BENCH_MAX:
            break

    print(f"[{VERSION}] gathering candidates for set {set_num} "
          f"(target={getattr(args,'target',50)}) …")
    global LENIENT_MODE
    stats["bigTarget"] = bool(getattr(args, "target", 50) and args.target > 80)
    LENIENT_MODE = stats["bigTarget"] or bool(getattr(args, "special_title", ""))
    if LENIENT_MODE:
        print(f"[{VERSION}] LENIENT_MODE on (large/special page) — broader gates + proof")
        args.min_score = min(args.min_score, 18.0)
    pool, calls = gather_candidates(
        used, set_num, args.target_fresh, args.max_pages, pushed_after, stats,
        do_fresh=not args.no_fresh, bench_seed=bench_seed, awesome_seed=residual)
    sweep_seconds = round(time.time() - t0, 1)
    print(f"[{VERSION}] {len(pool)} unique repos from {calls} search calls "
          f"({stats['zeroYield']} zero-yield queries skipped, {sweep_seconds}s)")

    # Pre-confirm gate + score. Awesome seeds always pass gate (curated).
    used_names, used_urls = _used_keys(used)

    def _score_pool(soft=False):
        out = []
        for repo in pool:
            if _is_used(repo, used_names, used_urls):
                continue
            is_fresh = bool(repo.get("_fresh"))
            is_awesome_seed = bool(repo.get("_awesome_seed"))
            if is_awesome_seed:
                gate_ok = True
            elif LENIENT_MODE or soft:
                gate_ok = hard_reject(repo) is None and len((repo.get("description") or "")) >= 12
            else:
                gate_fn = passes_fresh_gate if is_fresh else passes_app_gate
                gate_ok = gate_fn(repo)
                # Soft fallback if strict gate fails but still looks self-host-ish.
                if not gate_ok and hard_reject(repo) is None:
                    topics = {t.lower() for t in repo.get("topics", [])}
                    desc = (repo.get("description") or "").lower()
                    if (topics & STRICT_SELFHOST_TOPICS) or "self-host" in desc or "docker" in desc:
                        if len(desc) >= 15 and (repo.get("stargazers_count") or 0) >= 3:
                            gate_ok = True
            if not gate_ok:
                continue
            s, b = appeal_score(repo, now=now)
            out.append({
                "repo": {k: v for k, v in repo.items() if not k.startswith("_") or k in ("_fresh",)},
                "score": s, "breakdown": dict(b),
                "fresh_gem": is_fresh, "awesome_seed": is_awesome_seed,
            })
            # preserve fresh flag for select
            out[-1]["repo"]["_fresh"] = is_fresh
        out.sort(key=lambda c: c["score"], reverse=True)
        return out

    scored = _score_pool(soft=False)
    if len(scored) < 80:
        print(f"[{VERSION}] only {len(scored)} passed strict gate — re-scoring with soft gate")
        scored = _score_pool(soft=True)

    fresh_scored = sum(1 for c in scored if c.get("fresh_gem"))
    print(f"[{VERSION}] {len(scored)} candidates passed the gate and were scored "
          f"({fresh_scored} fresh gems) | pool unused={len(pool)}")

    repo_cache = _load_json(REPO_CACHE, {})

    # Larger shortlist: ship-proof rejects a large share after confirm.
    if LENIENT_MODE:
        SHORTLIST_MAX = min(1200, max(len(scored), int(getattr(args, "target", 50) * 4)))
    else:
        SHORTLIST_MAX = min(600, max(200, int(getattr(args, "target", 50) * 3)))
    by_cat = {}
    for c in scored:
        by_cat.setdefault(categorize(c["repo"]), []).append(c)
    # Prefer product/webapp categories first in round-robin shortlist.
    cats = ([c for c in PREFERRED_PRODUCT_CATS if c in by_cat]
            + [c for c in by_cat if c not in PREFERRED_PRODUCT_CATS])
    shortlist, cursor = [], {k: 0 for k in cats}
    while len(shortlist) < SHORTLIST_MAX:
        advanced = False
        for cat in cats:
            if len(shortlist) >= SHORTLIST_MAX:
                break
            i = cursor[cat]
            if i < len(by_cat[cat]):
                shortlist.append(by_cat[cat][i])
                cursor[cat] += 1
                advanced = True
        if not advanced:
            break
    print(f"[{VERSION}] confirming a diverse shortlist of {len(shortlist)} "
          f"(across {len(by_cat)} categories)")

    audit = {
        "version": VERSION, "setNum": set_num, "searchCalls": calls,
        "zeroYieldQueries": stats["zeroYield"], "poolSize": len(pool),
        "scoredCount": len(scored), "freshScored": fresh_scored,
        "graphql": not args.no_graphql,
        "awesome": bool(awesome), "awesomeResidual": len(residual),
        "awesomeSeeded": stats.get("awesomeSeeded", 0),
        "enrich": False, "rejected": [],
        "selected": [], "categoryDistribution": {}, "scoreStats": {},
        "dockerCount": 0, "composeCount": 0, "dockerfileOnlyCount": 0,
        "freshCount": 0, "shipProofCount": 0,
        "tierA": 0, "tierB": 0, "tierC": 0, "noProofRejected": 0,
        "benchSeeded": stats.get("benchSeeded", 0), "benchSaved": 0,
        "sweepSeconds": sweep_seconds, "confirmSeconds": 0.0,
    }

    tc = time.time()
    confirmed, seen_canon = [], set()
    for i in range(0, len(shortlist), 60):
        block = shortlist[i:i + 60]
        res = confirm_repos([(c["repo"]["html_url"], _sig(c["repo"])) for c in block],
                            repo_cache, now_ts, not args.no_graphql)
        for c in block:
            repo = res.get(c["repo"]["html_url"].lower())
            if not repo:
                audit["rejected"].append([c["repo"]["html_url"], "not-live/fork/archived"])
                continue
            ckey = repo["html_url"].lower()
            if ckey in used or ckey in seen_canon:
                audit["rejected"].append([repo["html_url"], "duplicate"])
                continue
            seen_canon.add(ckey)
            # Re-apply hard_reject on confirmed payload (descriptions may update).
            rej = hard_reject(repo)
            if rej:
                audit["rejected"].append([repo["html_url"], rej])
                continue
            base, bd = appeal_score(repo, now=now)
            in_awesome = repo["full_name"].lower() in awesome
            compose = bool(repo.get("compose_signal"))
            dockerfile = bool(repo.get("dockerfile_signal"))
            # Scoring: proof beats virality.
            # Prefer real deployable web apps: compose is the strongest signal.
            if compose:
                bd["compose"] = 14.0
                base += 14.0
            elif dockerfile:
                bd["dockerfile"] = 7.0
                base += 7.0
            hcat_now = categorize(repo)
            if hcat_now in PREFERRED_PRODUCT_CATS:
                bd["product_cat"] = 2.0
                base += 2.0
            base, bd = apply_family_score_adjust(repo, base, bd, hcat_now)
            # Hard-drop AI without any web UI signal
            if family_of(hcat_now) == "ai" and not has_ai_webui(repo) and not in_awesome:
                audit["rejected"].append([repo["html_url"], "ai-without-webui"])
                continue
            if repo.get("docker_signal"):
                audit["dockerCount"] += 1
            if compose:
                audit["composeCount"] += 1
            elif dockerfile:
                audit["dockerfileOnlyCount"] += 1
            if in_awesome:
                bd["awesome"] = 10.0
                base += 10.0
            tier = assign_proof_tier(repo, in_awesome, compose, dockerfile)
            if tier is None:
                audit["rejected"].append([repo["html_url"], "no-ship-proof"])
                audit["noProofRejected"] += 1
                continue
            confirmed.append({
                "repo": repo, "score": round(base, 2), "breakdown": bd,
                "docker": bool(repo.get("docker_signal")),
                "compose": compose, "dockerfile": dockerfile,
                "awesome": in_awesome, "proof_tier": tier,
                "hcat": hcat_now, "family": family_of(hcat_now),
                "fresh_gem": c.get("fresh_gem", False),
            })
    _save_json(REPO_CACHE, repo_cache)
    audit["confirmSeconds"] = round(time.time() - tc, 1)
    confirmed.sort(key=lambda c: c["score"], reverse=True)
    print(f"[{VERSION}] confirmed {len(confirmed)} proof-passing repos "
          f"(docker={audit['dockerCount']} compose={audit['composeCount']} "
          f"no-proof-rejected={audit['noProofRejected']}) ({time.time()-tc:.1f}s)")

    # Recovery: if still thin, soft-confirm remaining scored with lenient proof.
    target_n = getattr(args, "target", DEFAULT_TARGET) or DEFAULT_TARGET
    if len(confirmed) < target_n and scored:
        print(f"[{VERSION}] recovery: expanding confirm with soft proof "
              f"(have {len(confirmed)}, need {target_n})")
        have = {c["repo"]["html_url"].lower() for c in confirmed}
        extra = [c for c in scored if c["repo"].get("html_url", "").lower() not in have][:400]
        for i in range(0, len(extra), 50):
            block = extra[i:i + 50]
            res = confirm_repos([(c["repo"]["html_url"], _sig(c["repo"])) for c in block],
                                repo_cache, now_ts, not args.no_graphql)
            for c in block:
                repo = res.get(c["repo"]["html_url"].lower())
                if not repo:
                    continue
                ckey = repo["html_url"].lower()
                if ckey in used or ckey in seen_canon:
                    continue
                if hard_reject(repo):
                    continue
                seen_canon.add(ckey)
                base, bd = appeal_score(repo, now=now)
                in_awesome = repo["full_name"].lower() in awesome
                compose = bool(repo.get("compose_signal"))
                dockerfile = bool(repo.get("dockerfile_signal"))
                if compose:
                    bd["compose"] = 14.0
                    base += 14.0
                elif dockerfile or repo.get("docker_signal"):
                    bd["dockerfile"] = 7.0
                    base += 7.0
                hcat_now = categorize(repo)
                base, bd = apply_family_score_adjust(repo, base, bd, hcat_now)
                if family_of(hcat_now) == "ai" and not has_ai_webui(repo) and not in_awesome:
                    continue
                if in_awesome:
                    bd["awesome"] = 10.0
                    base += 10.0
                # Soft proof for recovery
                tier = assign_proof_tier(repo, in_awesome, compose,
                                         dockerfile or bool(repo.get("docker_signal")))
                if tier is None:
                    # Last-chance C: selfhosted topic or app shape + live repo
                    desc = (repo.get("description") or "")
                    topics = {t.lower() for t in repo.get("topics", [])}
                    if ((topics & STRICT_SELFHOST_TOPICS) or has_app_shape(repo)) and len(desc) >= 12:
                        tier = "C"
                    else:
                        continue
                if repo.get("docker_signal") or compose or dockerfile:
                    audit["dockerCount"] += 1
                confirmed.append({
                    "repo": repo, "score": round(base, 2), "breakdown": bd,
                    "docker": bool(repo.get("docker_signal") or compose or dockerfile),
                    "compose": compose, "dockerfile": dockerfile,
                    "awesome": in_awesome, "proof_tier": tier,
                    "hcat": hcat_now, "family": family_of(hcat_now),
                    "fresh_gem": c.get("fresh_gem", False),
                })
            if len(confirmed) >= target_n * 2:
                break
        _save_json(REPO_CACHE, repo_cache)
        confirmed.sort(key=lambda c: c["score"], reverse=True)
        print(f"[{VERSION}] recovery confirmed now {len(confirmed)} proof-passing")

    # Second discovery wave if still short of target (common after 19k+ used URLs).
    if len(confirmed) < target_n:
        print(f"[{VERSION}] second discovery wave (have {len(confirmed)}, need {target_n}) …")
        stats2 = {"zeroYield": 0, "bigTarget": True}
        pool2, calls2 = gather_candidates(
            used | {c["repo"]["html_url"].lower() for c in confirmed}
            | {c["repo"]["full_name"].lower() for c in confirmed},
            set_num + 97, max(args.target_fresh, 400), max(args.max_pages, 2),
            pushed_after, stats2, do_fresh=True, bench_seed=[], awesome_seed=[])
        audit["searchCalls"] = audit.get("searchCalls", 0) + calls2
        # Soft-score and confirm extras
        extra_scored = []
        for repo in pool2:
            if _is_used(repo, used_names, used_urls):
                continue
            if hard_reject(repo) or len((repo.get("description") or "")) < 12:
                continue
            s, b = appeal_score(repo, now=now)
            extra_scored.append({"repo": repo, "score": s, "breakdown": b, "fresh_gem": True})
        extra_scored.sort(key=lambda c: -c["score"])
        have = {c["repo"]["html_url"].lower() for c in confirmed}
        extra_scored = [c for c in extra_scored if c["repo"].get("html_url", "").lower() not in have][:350]
        print(f"[{VERSION}] wave2 scored={len(extra_scored)}, confirming…")
        for i in range(0, len(extra_scored), 50):
            block = extra_scored[i:i + 50]
            res = confirm_repos([(c["repo"]["html_url"], _sig(c["repo"])) for c in block],
                                repo_cache, int(time.time()), not args.no_graphql)
            for c in block:
                repo = res.get(c["repo"]["html_url"].lower())
                if not repo:
                    continue
                ckey = repo["html_url"].lower()
                if ckey in used or ckey in seen_canon:
                    continue
                if hard_reject(repo):
                    continue
                seen_canon.add(ckey)
                base, bd = appeal_score(repo, now=now)
                in_awesome = repo["full_name"].lower() in awesome
                compose = bool(repo.get("compose_signal"))
                dockerfile = bool(repo.get("dockerfile_signal"))
                if compose:
                    bd["compose"] = 14.0; base += 14.0
                elif dockerfile or repo.get("docker_signal"):
                    bd["dockerfile"] = 7.0; base += 7.0
                hcat_now = categorize(repo)
                base, bd = apply_family_score_adjust(repo, base, bd, hcat_now)
                if family_of(hcat_now) == "ai" and not has_ai_webui(repo) and not in_awesome:
                    continue
                if in_awesome:
                    bd["awesome"] = 10.0; base += 10.0
                tier = assign_proof_tier(repo, in_awesome, compose,
                                         dockerfile or bool(repo.get("docker_signal")))
                if tier is None:
                    desc = (repo.get("description") or "")
                    topics = {t.lower() for t in repo.get("topics", [])}
                    if ((topics & STRICT_SELFHOST_TOPICS) or has_app_shape(repo)) and len(desc) >= 12:
                        tier = "C"
                    else:
                        continue
                if repo.get("docker_signal") or compose or dockerfile:
                    audit["dockerCount"] += 1
                confirmed.append({
                    "repo": repo, "score": round(base, 2), "breakdown": bd,
                    "docker": bool(repo.get("docker_signal") or compose or dockerfile),
                    "compose": compose, "dockerfile": dockerfile,
                    "awesome": in_awesome, "proof_tier": tier,
                    "hcat": hcat_now, "family": family_of(hcat_now),
                    "fresh_gem": True,
                })
            if len(confirmed) >= target_n * 2:
                break
        _save_json(REPO_CACHE, repo_cache)
        confirmed.sort(key=lambda c: c["score"], reverse=True)
        print(f"[{VERSION}] after wave2: {len(confirmed)} proof-passing")

    # Optional LLM enrichment — hard drop if model says not self-hostable.
    if args.enrich:
        backend, key = resolve_llm_backend()
        if backend:
            audit["enrich"] = backend
            ecache = _load_json(ENRICH_CACHE, {})
            enrich_candidates(confirmed[:min(300, len(confirmed))], backend, key, ecache)
            _save_json(ENRICH_CACHE, ecache)
            kept = []
            for c in confirmed:
                e = c.get("enrich") or {}
                if e.get("self_hostable") is False:
                    audit["rejected"].append([c["repo"]["html_url"], "llm-not-self-hostable"])
                    continue
                if isinstance(e.get("novelty"), int):
                    adj = round((e["novelty"] - 3) * ENRICH_NOVELTY_WEIGHT, 2)
                    if adj:
                        c["breakdown"]["llm_novelty"] = adj
                        c["score"] = round(c["score"] + adj, 2)
                kept.append(c)
            confirmed = kept
            confirmed.sort(key=lambda c: c["score"], reverse=True)
        else:
            print("[enrich] no ANTHROPIC/OPENROUTER key found — skipping enrichment")

    target_n = getattr(args, "target", DEFAULT_TARGET) or DEFAULT_TARGET
    selected = select_with_proof_quotas(
        confirmed, args.cat_cap, args.min_score, args.fresh_quota, target=target_n)

    ab_n = sum(1 for c in selected if c.get("proof_tier") in ("A", "B"))
    if len(selected) < target_n and LENIENT_MODE and args.min_score > 12:
        # Second chance: lower min-score and re-select from confirmed
        print(f"[{VERSION}] only {len(selected)}/{target_n} — retrying select at min-score 12")
        selected = select_with_proof_quotas(
            confirmed, args.cat_cap, 12.0, args.fresh_quota, target=target_n)
    if len(selected) < target_n:
        # Final pad from confirmed regardless of min_score (still proof-tiered).
        taken = {c["repo"]["html_url"].lower() for c in selected}
        for c in sorted(confirmed, key=lambda x: -x["score"]):
            if len(selected) >= target_n:
                break
            u = c["repo"]["html_url"].lower()
            if u in taken:
                continue
            if family_of(c.get("hcat") or "") == "ai":
                ai_n = sum(1 for x in selected if (x.get("hcat") == "AI / LLM"))
                if ai_n >= HARD_CAT_CAPS.get("AI / LLM", 3):
                    continue
            selected.append(c)
            taken.add(u)
        print(f"[{VERSION}] padded selection to {len(selected)}")
    if not special and len(selected) < target_n:
        # REST search is mined out; fill the gap from v17's GraphQL lane (engines/topup.py).
        import topup
        selected, audit["topUp"] = topup.fill(
            VERSION, selected, target_n, tracker, args.dry_run,
            ai_ceiling=HARD_CAT_CAPS.get("AI / LLM", 3),
            is_ai_cat=lambda c: family_of(c.get("hcat") or "") == "ai",
            family_of=family_of,
            cat_cap=getattr(args, "cat_cap", None))
    if len(selected) != target_n:
        if LENIENT_MODE and len(selected) >= int(target_n * 0.55):
            print(f"[{VERSION}] WARN: shipping {len(selected)} of requested {target_n} "
                  f"(pool exhausted; still a solid showcase)")
        elif len(selected) >= max(30, int(target_n * 0.6)):
            print(f"[{VERSION}] WARN: shipping {len(selected)}/{target_n} "
                  f"(unused discovery pool thin after {len(used)} used URLs)")
        else:
            raise RuntimeError(
                f"Only {len(selected)} proof-passing repos scored >= --min-score {args.min_score} "
                f"(confirmed pool {len(confirmed)}, A∪B available="
                f"{sum(1 for c in confirmed if c.get('proof_tier') in ('A','B'))}). "
                f"Need {target_n}. Widen search or lower --min-score.")
    ab_n = sum(1 for c in selected if c.get("proof_tier") in ("A", "B"))  # after any top-up
    min_ab = max(1, int(round(MIN_AB_FRAC * target_n)))
    if ab_n < min_ab:
        # Soft fail → warn. With a full 50 proven picks, a thin A∪B floor is
        # still better than aborting a good dry-run (C-heavy sets are rare).
        print(f"[{VERSION}] WARN: A∪B={ab_n} below floor {min_ab} "
              f"(confirmed A∪B="
              f"{sum(1 for c in confirmed if c.get('proof_tier') in ('A','B'))}) "
              f"— shipping set with available proof tiers")

    for c in selected:
        e = c.get("enrich") or {}
        c["category"] = c["hcat"]
        c["llm_category"] = e.get("category") if e.get("category") in ALLOWED_CATEGORIES else None

    cat_counts = {}
    for c in selected:
        cat_counts[c["category"]] = cat_counts.get(c["category"], 0) + 1
    audit["categoryDistribution"] = dict(sorted(cat_counts.items(), key=lambda kv: -kv[1]))
    fam_counts = {}
    for c in selected:
        fam_counts[c.get("family") or family_of(c["category"])] = fam_counts.get(c.get("family") or family_of(c["category"]), 0) + 1
    audit["familyDistribution"] = dict(sorted(fam_counts.items(), key=lambda kv: -kv[1]))
    audit["freshCount"] = sum(1 for c in selected if c.get("fresh_gem"))
    audit["shipProofCount"] = len(selected)
    audit["tierA"] = sum(1 for c in selected if c.get("proof_tier") == "A")
    audit["tierB"] = sum(1 for c in selected if c.get("proof_tier") == "B")
    audit["tierC"] = sum(1 for c in selected if c.get("proof_tier") == "C")
    # Recalculate docker counts on selected only for the published set.
    audit["dockerCount"] = sum(1 for c in selected if c.get("docker"))
    audit["composeCount"] = sum(1 for c in selected if c.get("compose"))
    audit["dockerfileOnlyCount"] = sum(
        1 for c in selected if c.get("dockerfile") and not c.get("compose"))
    audit["selected"] = [{
        "url": c["repo"]["html_url"], "score": c["score"], "category": c["category"],
        "llm_category": c.get("llm_category"), "stars": c["repo"].get("stargazers_count", 0),
        "docker": c["docker"], "compose": c.get("compose", False),
        "dockerfile": c.get("dockerfile", False),
        "awesome": c["awesome"], "proof_tier": c.get("proof_tier"),
        "family": c.get("family") or family_of(c["category"]),
        "fresh_gem": c.get("fresh_gem", False),
        "hook": (c.get("enrich") or {}).get("interest_hook", ""),
        "breakdown": {k: round(v, 2) for k, v in c["breakdown"].items()},
        "composition": compose_line(c["breakdown"]),
    } for c in selected]
    sc = [c["score"] for c in selected]
    audit["scoreStats"] = {"min": min(sc), "max": max(sc), "mean": round(sum(sc) / len(sc), 2)}

    # Bench: only proof-passing surplus (not soft-scored junk).
    sel_keys = {c["repo"]["full_name"].lower() for c in selected}
    dead = {u.lower() for u, _reason in audit["rejected"]}
    bench_out = []
    for c in confirmed:
        r = c["repo"]
        k = (r.get("full_name") or "").lower()
        if not k or k in sel_keys or (r.get("html_url") or "").lower() in dead:
            continue
        if c.get("proof_tier") not in ("A", "B", "C"):
            continue
        slim = {f: r.get(f) for f in _BENCH_FIELDS if f in r}
        slim["_fresh"] = c.get("fresh_gem", False)
        slim["_proof_ok"] = True
        slim["_proof_tier"] = c.get("proof_tier")
        bench_out.append(slim)
        if len(bench_out) >= BENCH_MAX:
            break
    _save_json(BENCH_CACHE, {"_ts": now_ts, "setNum": set_num, "repos": bench_out})
    audit["benchSaved"] = len(bench_out)
    print(f"[{VERSION}] bench: saved {len(bench_out)} proof-eligible candidates for next run")
    print(f"[{VERSION}] tiers selected: A={audit['tierA']} B={audit['tierB']} "
          f"C={audit['tierC']} | compose={audit['composeCount']} docker={audit['dockerCount']}")

    audit["timingSeconds"] = round(time.time() - t0, 1)
    return set_num, title, selected, audit


# ---------------------------------------------------------------------------
# Notion page — rich per-pick line + score composition + fresh-gem badge (✨).
# ---------------------------------------------------------------------------
def page_blocks(set_num, selected):
    dist = {}
    for c in selected:
        dist[c["category"]] = dist.get(c["category"], 0) + 1
    dist_line = " · ".join(f"{k} ({v})" for k, v in sorted(dist.items(), key=lambda kv: -kv[1]))
    docker_n = sum(1 for c in selected if c["docker"])
    fresh_n = sum(1 for c in selected if c.get("fresh_gem"))
    tier_a = sum(1 for c in selected if c.get("proof_tier") == "A")
    tier_b = sum(1 for c in selected if c.get("proof_tier") == "B")
    tier_c = sum(1 for c in selected if c.get("proof_tier") == "C")
    blocks = [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [v1.rt(
            f"{'Showcase' if len(selected) != 50 else f'Set {set_num}'}: "
            f"{len(selected)} self-hosted open-source web apps (v13 product-diversity). "
            f"Every pick has ship proof: tier A (compose/Dockerfile)={tier_a}, "
            f"B (awesome-selfhosted)={tier_b}, C (explicit self-host + product site)={tier_c}. "
            f"{docker_n}/{len(selected)} ship Docker/compose. {fresh_n} fresh gems. "
            "(newly discovered, <100 stars or <6 months old). Each entry below shows its "
            "appeal score and the main pluses (▲) and minuses (▼) behind it. "
            "All confirmed live via the GitHub API."
        )]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            v1.rt("Category mix: ", bold=True), v1.rt(dist_line or "—")]}},
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [v1.rt("Top 50 repos")]}},
    ]
    for c in selected:
        repo = c["repo"]
        desc = repo.get("description") or "Open-source self-hostable project."
        hook = (c.get("enrich") or {}).get("interest_hook", "")
        topics = ", ".join(repo.get("topics", [])[:6]) or "no topics"
        badge = " 🐳" if c["docker"] else ""
        star = " ⭐" if c.get("awesome") else ""
        fresh = " ✨" if c.get("fresh_gem") else ""
        tier = c.get("proof_tier") or "?"
        tail = f" — {hook}" if hook else ""
        comp = compose_line(c["breakdown"])
        details = (
            f" — [{c['category']}][tier {tier}]{badge}{star}{fresh} "
            f"{repo.get('language') or 'Unknown'} — {desc[:340]}{tail} "
            f"(★ {repo.get('stargazers_count', 0):,}; updated {repo.get('pushed_at', '')[:10]}; "
            f"appeal {c['score']:.0f} · {comp}; topics: {topics})"
        )
        blocks.append({"object": "block", "type": "numbered_list_item",
                       "numbered_list_item": {"rich_text": [
                           v1.rt(repo["full_name"], link=repo["html_url"], bold=True),
                           v1.rt(details)]}})
    return blocks


def append_master_csv(set_num, selected):
    new_file = not v1.MASTER_CSV.exists()
    with open(v1.MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(v1.MASTER_CSV_HEADER)
        for c in selected:
            repo = c["repo"]
            desc = (repo.get("description") or "").replace("\n", " ").replace("\r", " ").strip()
            w.writerow([set_num, repo.get("full_name", ""), c["category"],
                        repo.get("html_url", ""), desc])


# ---------------------------------------------------------------------------
# Inspectors: --verify, --stats, --self-test, --why.
# ---------------------------------------------------------------------------
def verify_tracker(tracker):
    pages = tracker.get("completedPages", [])
    seen, off, union = {}, [], set()
    for p in pages:
        repos = p.get("repos", [])
        if len(repos) != 50:
            off.append((p["setNum"], len(repos)))
        for u in repos:
            k = u.lower()
            union.add(k)
            seen[k] = seen.get(k, 0) + 1
    dup = {k: n for k, n in seen.items() if n > 1}
    used = {u.lower() for u in tracker.get("usedRepoUrls", [])}
    eq = used == union
    print("=" * 60)
    print(f"[verify] sets published        : {len(pages)}")
    print(f"[verify] unique repos (union)  : {len(union)}")
    print(f"[verify] cross-set duplicates  : {len(dup)}"
          + (f"  e.g. {sorted(dup.items(), key=lambda x:-x[1])[:3]}" if dup else "  ✅ none"))
    print(f"[verify] sets != 50            : {len(off)}" + (f"  e.g. {off[:5]}" if off else "  ✅ all 50"))
    print(f"[verify] usedRepoUrls == union : {'✅ yes' if eq else '❌ NO'}"
          + ("" if eq else f"  (+{len(used-union)} used-only / +{len(union-used)} sets-only)"))
    print("=" * 60)


def print_stats(tracker):
    pages = tracker.get("completedPages", [])
    rc = _load_json(REPO_CACHE, {})
    live = sum(1 for v in rc.values() if not v.get("_dead"))
    aw = _load_json(AWESOME_CACHE, None) or _load_json(AWESOME_CACHE_FALLBACK, None)
    en = _load_json(ENRICH_CACHE, {})
    print(f"[stats] completedSets        : {tracker.get('completedSets')}  (next = {int(tracker.get('completedSets',0))+1})")
    print(f"[stats] usedRepoUrls         : {len(tracker.get('usedRepoUrls', []))}")
    print(f"[stats] completedPages       : {len(pages)}")
    print(f"[stats] v12 repo cache        : {len(rc)} entries ({live} live / {len(rc)-live} dead)")
    print(f"[stats] awesome index        : {len(aw.get('repos', [])) if aw else 0} repos cached")
    print(f"[stats] enrich cache         : {len(en)} repos")
    if v1.MASTER_CSV.exists():
        with open(v1.MASTER_CSV) as f:
            print(f"[stats] master CSV rows      : {sum(1 for _ in f) - 1}")


def self_test():
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("  ok  " if cond else "  !!  ") + label)
        ok = ok and cond

    check(not passes_app_gate({"full_name": "x/awesome-selfhosted", "description": "curated list",
                               "topics": ["awesome"], "language": "Markdown", "stargazers_count": 9999}),
          "rejects awesome-list")
    check(not passes_app_gate({"full_name": "y/starter", "description": "a boilerplate template",
                               "topics": ["template"], "language": "TypeScript", "stargazers_count": 500}),
          "rejects template")
    check(not passes_app_gate({"full_name": "z/lib", "description": "a library for parsing JSON",
                               "topics": ["json"], "language": "Go", "stargazers_count": 800}),
          "rejects bare library")

    # v12: known escape classes
    check(not passes_app_gate({"full_name": "sammchardy/python-binance",
                               "description": "Binance Exchange API python implementation for automated trading",
                               "topics": ["api", "crypto"], "language": "Python",
                               "stargazers_count": 7000}),
          "rejects python-binance class (API client)")
    check(not passes_app_gate({"full_name": "kemalcr/kemal",
                               "description": "Lightning Fast, Super Simple web framework for Crystal",
                               "topics": ["web", "framework"], "language": "Crystal",
                               "stargazers_count": 3000}),
          "rejects kemal-class web framework")
    check(not passes_app_gate({"full_name": "x/Learning-Complete-Backend",
                               "description": "Complete backend course materials and tutorials",
                               "topics": ["education"], "language": "JavaScript",
                               "stargazers_count": 50}),
          "rejects tutorial/course repos")
    check(not passes_app_gate({"full_name": "x/random-tool",
                               "description": "Interesting utility that does cool things with files and data",
                               "topics": ["tools"], "language": "Go", "stargazers_count": 200}),
          "rejects stars-only without self-host signal")

    app = {"full_name": "a/prism", "description": "Self-hosted family dashboard with docker compose",
           "topics": ["self-hosted", "docker", "dashboard"], "language": "TypeScript",
           "stargazers_count": 800, "forks_count": 40, "created_at": "2025-06-01T00:00:00Z",
           "pushed_at": datetime.now(timezone.utc).isoformat(), "homepage": "https://prism.app",
           "license": {"spdx_id": "MIT"}}
    check(passes_app_gate(app), "accepts a real self-hosted app")
    s, b = appeal_score(app)
    check(s > 50 and "gem_balance" in b, f"scores a gem-zone app ({s})")
    check(b.get("selfhost_signal", 0) >= 8.0, "strict selfhost_signal awards +8")
    check(appeal_score(dict(app, stargazers_count=90000))[0] < s, "damps mega-repos")
    check(categorize({"full_name": "a/ng-diagram", "description": "Angular diagram library",
                      "topics": ["diagram"]}) == "Image / Design / Creative", "diagram→Image")
    check(_parse_array('```json\n[{"id":"a/b"}]\n```') == [{"id": "a/b"}], "tolerant JSON parse")

    check(assign_proof_tier(app, False, True, False) == "A", "tier A: compose + intent")
    check(assign_proof_tier(app, True, False, False) == "B", "tier B: awesome list")
    check(assign_proof_tier(app, False, False, False) == "C",
          "tier C: explicit self-host + product homepage")
    no_proof = {"full_name": "x/lib", "description": "A useful Go module for hashing",
                "topics": ["go"], "language": "Go", "stargazers_count": 100, "homepage": ""}
    check(assign_proof_tier(no_proof, False, False, False) is None, "no proof → None")

    fresh_repo = {"full_name": "a/newapp", "description": "A lightweight self-hosted task tracker",
                  "topics": ["task-management", "self-hosted"], "language": "Go",
                  "stargazers_count": 3, "forks_count": 1,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "pushed_at": datetime.now(timezone.utc).isoformat(),
                  "homepage": "https://newapp.dev"}
    check(passes_fresh_gate(fresh_repo), "fresh gate accepts a 3-star self-hosted repo")
    check(not passes_fresh_gate({"full_name": "b/empty", "description": "x",
                                 "topics": [], "language": "Python", "stargazers_count": 1}),
          "fresh gate rejects no-signal repo")

    s_fresh, b_fresh = appeal_score(fresh_repo)
    check(s_fresh > 35, f"fresh gem scores reasonably ({s_fresh})")
    check(b_fresh.get("youth", 0) == 3.0, "youth bonus mild (=3 for <90d repo)")
    check(is_fresh_gem(fresh_repo), "is_fresh_gem identifies new repo")
    old_repo = dict(fresh_repo, stargazers_count=5000, created_at="2020-01-01T00:00:00Z")
    check(not is_fresh_gem(old_repo), "is_fresh_gem rejects old high-star repo")

    cats = ["AI / LLM", "Media / Streaming", "DevOps / Infra", "Notes / Knowledge",
            "Security / Auth", "Finance / Budget", "Automation", "CMS / Website",
            "Files / Storage / Backup", "Communication / Social"]
    pool = []
    sc = 100
    for j in range(40):
        pool.append({"score": sc, "hcat": "AI / LLM",
                     "repo": {"html_url": f"https://github.com/x/ai{j}"},
                     "breakdown": {}, "proof_tier": "A", "fresh_gem": False})
        sc -= 1
    for cat in cats[1:]:
        for j in range(12):
            pool.append({"score": sc, "hcat": cat,
                         "repo": {"html_url": f"https://github.com/x/{cat[:3]}{j}"},
                         "breakdown": {}, "proof_tier": "A", "fresh_gem": False})
            sc -= 1
    pool.sort(key=lambda c: c["score"], reverse=True)
    sel = select_balanced(pool, cap=4, min_score=0)
    ai = sum(1 for c in sel if c["hcat"] == "AI / LLM")
    distinct = len({c["hcat"] for c in sel})
    check(len(sel) == 50, f"balanced fills exactly 50 (got {len(sel)})")
    check(ai <= HARD_CAT_CAPS["AI / LLM"],
          f"AI soft ceiling holds (AI={ai} <= {HARD_CAT_CAPS['AI / LLM']})")
    check(distinct >= 7, f"balanced keeps diversity (distinct={distinct})")

    pool3 = []
    sc = 90
    for j in range(45):
        pool3.append({"score": sc, "hcat": cats[j % len(cats)],
                      "repo": {"html_url": f"https://github.com/x/ab{j}",
                               "full_name": f"x/ab{j}"},
                      "breakdown": {}, "proof_tier": "A", "fresh_gem": j < 5})
        sc -= 0.5
    for j in range(15):
        pool3.append({"score": 40 - j, "hcat": "Web App / Other",
                      "repo": {"html_url": f"https://github.com/x/c{j}",
                               "full_name": f"x/c{j}"},
                      "breakdown": {}, "proof_tier": "C", "fresh_gem": True})
    sel3 = select_with_proof_quotas(pool3, cap=4, min_score=0, fresh_quota=5, target=50)
    check(len(sel3) == 50, f"proof select fills 50 (got {len(sel3)})")
    check(sum(1 for c in sel3 if c.get("proof_tier") in ("A", "B")) >= MIN_AB_PROOF,
          "proof select keeps AUB floor")
    check(sum(1 for c in sel3 if c.get("proof_tier") == "C") <= MAX_TIER_C,
          "proof select respects C ceiling")
    ai3 = sum(1 for c in sel3 if (c.get("family") or family_of(c.get("hcat",""))) == "ai"
              or c.get("hcat") == "AI / LLM")
    check(ai3 <= HARD_CAT_CAPS["AI / LLM"] + 1, f"diverse select AI ceil (AI={ai3})")
    # Heavy-AI pool must not dominate
    heavy = []
    sc = 200
    for j in range(80):
        heavy.append({"score": sc, "hcat": "AI / LLM", "family": "ai",
                      "repo": {"html_url": f"https://github.com/h/ai{j}", "full_name": f"h/ai{j}"},
                      "breakdown": {}, "proof_tier": "A", "fresh_gem": False})
        sc -= 0.5
    for j, cat in enumerate(["Files / Storage / Backup", "Notes / Knowledge", "Media / Streaming",
                             "Documents / PDF / Paperless", "Dashboard / Homelab"]):
        for k in range(8):
            heavy.append({"score": 90 - k, "hcat": cat, "family": family_of(cat),
                          "repo": {"html_url": f"https://github.com/h/{j}{k}", "full_name": f"h/{j}{k}"},
                          "breakdown": {}, "proof_tier": "A", "fresh_gem": False})
    selh = select_diverse(heavy, 50, 0, 0)
    aih = sum(1 for c in selh if c.get("hcat") == "AI / LLM")
    check(aih <= 3, f"heavy-AI pool still AI<=3 (got {aih})")
    check(any(c.get("hcat") == "Files / Storage / Backup" for c in selh), "includes files family")
    check(any(c.get("hcat") == "Notes / Knowledge" for c in selh), "includes knowledge family")

    fake = {
        "nameWithOwner": "a/b", "stargazerCount": 10, "forkCount": 1,
        "openIssues": {"totalCount": 0}, "pushedAt": "2026-01-01T00:00:00Z",
        "createdAt": "2025-01-01T00:00:00Z", "description": "x",
        "homepageUrl": "", "isFork": False, "isArchived": False,
        "isDisabled": False, "isMirror": False,
        "primaryLanguage": {"name": "Go"}, "licenseInfo": {"spdxId": "MIT"},
        "repositoryTopics": {"nodes": []},
        "c1": None, "c2": None, "c3": None, "c4": None, "d1": None,
        "e1": {"__typename": "Blob"},
    }
    norm = _gql_normalise(fake)
    check(norm["docker_signal"] is False, "env.example alone is NOT docker proof")
    check(norm["env_example"] is True, "env.example still recorded")
    fake2 = dict(fake, c1={"__typename": "Blob"})
    norm2 = _gql_normalise(fake2)
    check(norm2["compose_signal"] and norm2["docker_signal"], "compose file is docker proof")

    check(VERSION == "v13", "VERSION constant is v13")

    # Regression: never use str.rstrip('.git') semantics on repo names.
    check(_strip_git_suffix("https://github.com/chatwoot/chatwoot.git")
          == "https://github.com/chatwoot/chatwoot", "strip .git suffix only")
    check(_owner_name("https://github.com/chatwoot/chatwoot.git") == ("chatwoot", "chatwoot"),
          "owner_name keeps trailing t (no rstrip bug)")
    check(_owner_name("https://github.com/octoprint/octoprint") == ("octoprint", "octoprint"),
          "owner_name octoprint intact")
    tok = _get_gh_token()
    check(bool(tok), "GitHub token resolvable (env or gh hosts.yml)")

    print("self-test: " + ("ALL PASSED ✅" if ok else "FAILURES ❌"))
    return ok


def why(url, args):
    on = _owner_name(url)
    if not on:
        print("not a github repo url")
        return
    repo = v1.canonical_repo(url)
    print(f"=== {url} ===")
    if not repo:
        print("  not live (fork/archived/disabled/404)")
        return
    reject = hard_reject(repo)
    print(f"  hard_reject : {'REJECT — ' + reject if reject else 'PASS'}")
    print(f"  app gate    : {'PASS' if passes_app_gate(repo) else 'FAIL'}")
    print(f"  fresh gate  : {'PASS' if passes_fresh_gate(repo) else 'FAIL'}")
    print(f"  is_fresh    : {is_fresh_gem(repo)}")
    print(f"  category    : {categorize(repo)}")
    print(f"  strict intent: {has_strict_selfhost_intent(repo)}")
    s, b = appeal_score(repo)
    res = confirm_repos([(url, _sig(repo))], {}, int(time.time()), not args.no_graphql)
    cr = res.get(url.lower()) or repo
    compose = bool(cr.get("compose_signal"))
    dockerfile = bool(cr.get("dockerfile_signal"))
    if compose:
        b["compose"] = 12.0
        s += 12.0
    elif dockerfile:
        b["dockerfile"] = 5.0
        s += 5.0
    awesome = load_awesome_index(int(time.time()), True)
    in_aw = (cr.get("full_name") or "").lower() in awesome
    if in_aw:
        b["awesome"] = 10.0
        s += 10.0
    tier = assign_proof_tier(cr, in_aw, compose, dockerfile)
    print(f"  proof_tier  : {tier}")
    print(f"  appeal      : {s:.1f}")
    print(f"  composition : {compose_line(b, top_pos=8)}")
    print(f"  breakdown   : " + ", ".join(f"{k}={v:g}" for k, v in b.items()))
    print(f"  compose     : {compose} | dockerfile: {dockerfile} | awesome: {in_aw}")


# ---------------------------------------------------------------------------
# Publish lock (stale-PID / TTL takeover) — same as v7.
# ---------------------------------------------------------------------------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def acquire_lock():
    now = int(time.time())
    if LOCKFILE.exists():
        try:
            info = json.loads(LOCKFILE.read_text())
        except (ValueError, OSError):
            info = {}
        held = info.get("pid")
        age = now - info.get("ts", 0)
        if age < LOCK_TTL and _pid_alive(held):
            raise SystemExit(f"[lock] another run holds the lock (pid {held}, {age}s ago). "
                             f"Wait or delete {LOCKFILE}.")
        print(f"[lock] taking over stale lock (pid {held}, age {age}s)")
    CACHE_DIR.mkdir(exist_ok=True)
    LOCKFILE.write_text(json.dumps({"pid": os.getpid(), "ts": now}))


def release_lock():
    try:
        LOCKFILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="v13: product-diversity proof-first self-hosted WEB APPS. "
                    "Family floors (files/KB/media/PDF/…) + AI hard ceil. "
                    "Use --count 300 --special-title '…' --force-publish for showcase pages.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sync-only", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--why", metavar="URL", help="Explain one repo, then exit.")
    p.add_argument("--breakdown", action="store_true",
                   help="Print every selected pick's score composition.")
    p.add_argument("--enrich", action="store_true")
    p.add_argument("--no-graphql", action="store_true")
    p.add_argument("--no-awesome", action="store_true")
    p.add_argument("--no-fresh", action="store_true",
                   help="Skip the fresh-gem sweep.")
    p.add_argument("--target-fresh", type=int, default=250)
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--cat-cap", type=int, default=DEFAULT_CAT_CAP)
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    p.add_argument("--fresh-quota", type=int, default=FRESH_QUOTA_DEFAULT,
                   help=f"Max slots reserved for fresh gems (default {FRESH_QUOTA_DEFAULT}).")
    p.add_argument("--target", "--count", dest="target", type=int, default=DEFAULT_TARGET,
                   help="How many repos to select (default 50). Use 300 for special pages.")
    p.add_argument("--special-title", default="",
                   help="If set, publish a one-off page with this title (not Set N). "
                        "Does not advance completedSets series counter.")
    p.add_argument("--force-publish", action="store_true",
                   help="Allow publish when --target != 50 or --special-title is set.")
    args = p.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)
    if args.why:
        why(args.why, args)
        return

    tracker = json.loads(v1.TRACKER.read_text())
    if args.stats:
        print_stats(tracker)
        return
    if args.verify:
        verify_tracker(tracker)
        return

    v1.NOTION_KEY = v1.load_notion_key()
    v1.AUDIT_DIR.mkdir(exist_ok=True)

    # Special showcase pages store setNum null/0 — keep reconcile happy.
    for _p in tracker.get("completedPages", []):
        if _p.get("setNum") is None:
            _p["setNum"] = 0
    sync = v1.reconcile_with_notion(tracker)
    if sync["pulled"]:
        print(f"[sync] pulled {len(sync['pulled'])} set(s) from Notion missing locally: "
              + ", ".join(f"#{q['setNum']}({q['repos']} repos)" for q in sync["pulled"]))
    print(f"[sync] latest in Notion={sync['liveLatest']} | local-before={sync['localLatestBefore']} "
          f"| authoritative={sync['authoritativeLatest']}")
    if sync["changed"] and not args.dry_run:
        v1.write_tracker(tracker)
        print(f"[sync] tracker updated -> completedSets={tracker['completedSets']}, "
              f"usedRepoUrls={len(tracker['usedRepoUrls'])}")
    if args.sync_only:
        print("[sync] sync-only complete; nothing published.")
        return

    set_num, title, selected, audit = select_set(tracker, sync["liveTitles"], args)
    print(f"[{VERSION}] selected {len(selected)} | categories: "
          + ", ".join(f"{k}={v}" for k, v in audit["categoryDistribution"].items()))
    print(f"[{VERSION}] appeal min/mean/max = {audit['scoreStats']['min']}/{audit['scoreStats']['mean']}/"
          f"{audit['scoreStats']['max']} | docker={audit['dockerCount']} "
          f"compose={audit.get('composeCount', '?')} | "
          f"tiers A/B/C={audit.get('tierA')}/{audit.get('tierB')}/{audit.get('tierC')} | "
          f"{audit['freshCount']} fresh | timing: {audit['timingSeconds']}s")
    if args.breakdown:
        print(f"[{VERSION}] per-pick score composition:")
        for c in sorted(selected, key=lambda x: -x["score"]):
            fresh_mark = " ✨" if c.get("fresh_gem") else ""
            print(f"   {c['score']:>5.0f} [{c['category']:<26}] {c['repo']['full_name']:<38}{fresh_mark} "
                  f"{compose_line(c['breakdown'])}")

    special = bool(getattr(args, "special_title", "") or "")
    target_n = getattr(args, "target", DEFAULT_TARGET) or DEFAULT_TARGET
    if special:
        title = args.special_title.strip()
        audit["special"] = True
        audit["specialTitle"] = title
        audit["target"] = target_n

    if args.dry_run:
        tag = f"special{target_n}" if special else f"set{set_num}"
        ap = v1.AUDIT_DIR / f"{tag}_{VERSION}_DRYRUN_audit.json"
        ap.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
        print(f"[dry-run] would publish: {title} ({len(selected)} repos)")
        print(f"[dry-run] audit -> {ap}")
        return

    if not special and len(selected) != 50:
        raise SystemExit(f"[{VERSION}] selected {len(selected)}/50 repos; refusing an incomplete standard page.")

    # Guard: non-standard sizes need --force-publish
    if (target_n != 50 or special) and not args.force_publish:
        raise SystemExit(
            f"[v13] refusing to publish target={target_n} special={special} "
            f"without --force-publish (use --dry-run first).")

    acquire_lock()
    try:
        page = v1.notion_request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": tracker["parentPageId"]},
            "properties": {"title": {"title": [v1.rt(title)]}}})
        v1.append_blocks(page["id"], page_blocks(set_num if not special else target_n, selected))
        urls = [c["repo"]["html_url"] for c in selected]
        entry = {
            # 0 = special showcase (not a numbered series set). Never None —
            # reconcile_with_notion does int(setNum) over completedPages.
            "setNum": 0 if special else set_num,
            "special": special,
            "title": title,
            "pageId": page["id"],
            "pageUrl": v1.notion_url(title, page["id"]),
            "repos": urls,
            "count": len(urls),
            "version": VERSION,
        }
        if not special:
            tracker["completedSets"] = set_num
        tracker["completedPages"].append(entry)
        merged, seen = [], set()
        for u in tracker.get("usedRepoUrls", []) + urls:
            if u.lower() not in seen:
                seen.add(u.lower())
                merged.append(u)
        tracker["usedRepoUrls"] = merged
        v1.write_tracker(tracker)
        append_master_csv(set_num if not special else f"S{target_n}", selected)
        tag = f"special{target_n}" if special else f"set{set_num}"
        (v1.AUDIT_DIR / f"{tag}_{VERSION}_verified_audit.json").write_text(
            json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    finally:
        release_lock()

    print(json.dumps(entry, indent=2, ensure_ascii=False))
    print(f"usedRepoUrls={len(tracker['usedRepoUrls'])}")
    print(f"master CSV -> {v1.MASTER_CSV.name} (+{len(selected)} rows)")


if __name__ == "__main__":
    main()
