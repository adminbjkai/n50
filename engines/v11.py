#!/usr/bin/env python3
"""
run_next_set_v11.py — the fast, bench-backed runner.

Same numbered Notion series, same tracker + master CSV, fully interchangeable
with v1-v10. Imports v1's plumbing; never modifies older versions.

What v11 changes vs v9/v10 (full rationale: docs/history/ANALYSIS-v1-v10.md +
V11-STRATEGY.md):
  1. Candidate BENCH: scored-but-unselected candidates from the previous run
     are persisted (_tmp/bench_v11.json, 14-day TTL) and reused, so
     steady-state runs need ~10-25 GitHub search calls instead of ~132
     (v9/v10 median 444s -> target <=120s). Finalists are always re-confirmed
     live via GraphQL before publish, so stale bench data cannot ship.
  2. Restored + strengthened early-exit sweeps with a hard 60-call cap
     (v10 accidentally removed v9's early exits and always burned ~100 calls).
  3. Native urllib HTTP for GitHub search/GraphQL + LLM calls (v10's one real
     win) — no subprocess fork per call; keys travel in-process, never argv.
  4. One VERSION constant drives every label (audit filename + version field,
     caches, prints) — fixes v10 stamping its audits as "v9".
  5. --enrich bypass: Docker+awesome-verified finalists skip the LLM (labeled
     {"source": "bypass"}, no fabricated hooks); only ambiguous repos are sent.
  6. Per-phase timings in the audit (sweepSeconds/confirmSeconds/total).

Selection core (gate, scoring, diversity, fresh-gem quota) is v9's, unchanged.
Flags: identical to v9. Auth: gh CLI or GH_TOKEN; Notion token via env/file;
optional ANTHROPIC/OPENROUTER key for --enrich. Always --dry-run first.
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
VERSION = "v11"
CACHE_DIR = v1.AUDIT_DIR
REPO_CACHE = CACHE_DIR / "repo_cache_v11.json"
AWESOME_CACHE = CACHE_DIR / "awesome_selfhosted_v11.json"
ENRICH_CACHE = CACHE_DIR / "enrich_cache_v11.json"
BENCH_CACHE = CACHE_DIR / "bench_v11.json"
LOCKFILE = CACHE_DIR / "publish_v11.lock"

CACHE_TTL = 14 * 24 * 3600
BENCH_TTL = 14 * 24 * 3600
BENCH_MAX = 400
DEAD_TTL = 3 * 24 * 3600
AWESOME_TTL = 30 * 24 * 3600
LOCK_TTL = 30 * 60

GRAPHQL_BATCH = 35
ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

ENRICH_SELFHOST_PENALTY = 15.0
ENRICH_NOVELTY_WEIGHT = 1.0

# Fresh-gem quota: how many of 50 slots are *reserved* for fresh gems.
FRESH_QUOTA_DEFAULT = 10
FRESH_STAR_CEILING = 100       # repos above this many stars are NOT "fresh"
FRESH_AGE_CEILING_DAYS = 180   # repos older than this are NOT "fresh"


# ---------------------------------------------------------------------------
# Discovery query bank — v7's topics + 20+ new emerging-tech topics.
# ---------------------------------------------------------------------------
SEARCH_TOPICS = [
    # --- v7's original topics (kept verbatim) ---
    "self-hosted", "selfhosted", "self-hosting", "homelab", "docker",
    "docker-compose", "kubernetes", "raspberry-pi", "nas", "home-server",
    "dashboard", "homepage", "startpage", "web-app", "webapp",
    "ai", "llm", "rag", "chatbot", "ai-agent", "local-llm", "openai",
    "media-server", "music-server", "podcast", "photos", "video", "streaming",
    "audiobook", "comics", "manga",
    "notes", "note-taking", "wiki", "knowledge-base", "bookmarks", "rss",
    "read-it-later", "markdown", "paperless", "document-management", "ebook",
    "kanban", "task-management", "project-management", "calendar",
    "time-tracking", "todo", "scheduling",
    "finance", "budgeting", "expense-tracker", "accounting", "invoicing",
    "developer-tools", "git", "ci-cd", "api", "webhook", "pastebin",
    "url-shortener", "code-snippets", "low-code",
    "monitoring", "observability", "uptime", "status-page", "vpn", "wireguard",
    "dns", "proxy", "reverse-proxy", "ad-blocker", "privacy",
    "file-sharing", "backup", "sync", "cloud-storage",
    "automation", "workflow", "home-automation", "smart-home", "mqtt", "iot",
    "analytics", "cms", "blog", "e-commerce", "search-engine",
    "recipes", "fitness", "health", "maps", "gaming", "game-server", "education",
    "lms", "whiteboard", "diagram", "crm", "helpdesk", "password-manager",
    "matrix", "fediverse",
    # --- v9 NEW: emerging-tech topics for fresh discovery ---
    "local-ai", "on-device-ai", "edge-ai", "ai-dashboard",
    "voice-assistant", "tts", "stt", "text-to-speech",
    "vector-db", "embedding", "ollama", "llama-cpp", "gguf",
    "ml-platform", "feature-store", "model-serving",
    "workflow-automation", "n8n", "zapier-alternative",
    "bms", "ev-charging", "solar-monitor", "energy-monitor",
    "plant-monitor", "aquarium", "hydroponics",
    "3d-printer", "cnc", "drone", "flight-controller",
    "smart-mirror", "magic-mirror", "homebridge", "scrypted",
    "bookshelf", "opds", "calibre",
    "gitops", "infrastructure-as-code",
    "personal-knowledge-base", "second-brain", "zettelkasten",
    "selfhosted-app", "deploy-yourself", "run-your-own",
]
SEARCH_PHRASES = [
    '"self-hosted" "docker compose"', '"self-hosted" "web app"',
    '"self hostable" alternative', '"homelab" dashboard',
    '"admin dashboard" docker', '"open source" "web application" docker',
    '"local first" app', '"open source alternative to"',
    '"AI" "webui" self-hosted', '"notion alternative"',
    '"google alternative" self-hosted', '"kanban" "docker compose"',
    '"docker compose" dashboard', '"privacy friendly" self-hosted',
    # v9 new phrases for fresh discovery
    '"newly created" "open source" "web app"',
    '"just launched" "self-hosted"',
    '"lightweight" "self-hosted" "docker"',
    '"simple" "open source" "web app" "docker"',
    '"minimal" "self-hosted" alternative',
    '"run your own" "open source"',
    '"deploy in minutes" "docker"',
]

COMPOSITE_QUERY_TEMPLATES = [
    # Directly from the research report's high-yield GitHub search patterns.
    'topic:selfhosted stars:3..100 created:>{created_24m} pushed:>{pushed_18m} archived:false',
    'topic:self-hosted stars:3..100 created:>{created_24m} pushed:>{pushed_18m} archived:false',
    'topic:docker-compose topic:selfhosted stars:10..500 pushed:>{pushed_24m} archived:false',
    'topic:docker topic:selfhosted stars:10..500 pushed:>{pushed_24m} archived:false',
    'topic:selfhosted topic:web-app stars:10..300 created:>{created_30m} archived:false',
    'self-hosted in:readme stars:5..300 pushed:>{pushed_18m} archived:false',
    'selfhosted in:description stars:5..500 pushed:>{pushed_24m} archived:false',
    '"self-hosted alternative" in:readme stars:1..500 pushed:>{pushed_30m} archived:false',
    '"open source alternative to" in:readme stars:1..500 pushed:>{pushed_30m} archived:false',
    '"docker compose" in:readme stars:1..300 pushed:>{pushed_18m} archived:false',
    '"run your own" in:readme stars:1..300 pushed:>{pushed_24m} archived:false',
    'topic:docker-compose stars:1..200 forks:1..50 pushed:>{pushed_18m} archived:false',
    'topic:selfhosted stars:1..200 forks:1..50 license:mit pushed:>{pushed_24m} archived:false',
    'topic:selfhosted stars:1..200 forks:1..50 license:apache-2.0 pushed:>{pushed_24m} archived:false',
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
    "awesome", "awesome-list", "curated list", "collection of", "list of",
    "roadmap", "cheatsheet", "cheat sheet", "boilerplate", "starter-kit",
    "starter kit", "scaffold", "skeleton", "template", "theme", "dotfiles",
    "wallpaper", "wallpapers", "leetcode", "interview", "coursework", "homework",
    "tutorial", "study notes", "learning-path", "freecodecamp", "hacktoberfest",
    "demo project", "example project", "test repo", "my portfolio",
    "personal website", "personal blog", "config files", "course materials",
    "workshop", "bootcamp",
]
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
GOOD_TOPICS = {
    "self-hosted", "selfhosted", "self-hosting", "docker", "docker-compose",
    "homelab", "web-app", "webapp", "dashboard", "self-hostable", "open-source",
    "kubernetes", "raspberry-pi", "nas", "home-server", "fullstack",
    "selfhosted-app", "run-your-own", "deploy-yourself",
}


def hard_reject(repo):
    """Return a reject reason string, or None if the repo passes the gate."""
    name = (repo.get("full_name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    blob = f"{name} {desc}"
    if not desc:
        return "no-description"
    if desc.startswith("http"):
        return "description-is-url"
    if any(term in blob for term in BAD_TERMS):
        return "list-template-or-tutorial"
    if "awesome-list" in topics or name.split("/")[-1].startswith("awesome-"):
        return "awesome-list"
    if any(m in desc for m in MOBILE_ONLY) and not (topics & GOOD_TOPICS) and "server" not in desc:
        return "mobile-only-client"
    if any(s in desc for s in NON_APP_SIGNALS):
        return "not-a-web-app (extension/desktop/cli)"
    if any(lib in desc for lib in LIBRARY_SIGNALS) and not (topics & GOOD_TOPICS):
        return "library-or-sdk"
    stars = repo.get("stargazers_count", 0)
    if re.search(r"\bclone\b", desc) and stars < 150 and repo.get("forks_count", 0) < 10:
        return "low-effort-clone"
    return None


def passes_app_gate(repo):
    if hard_reject(repo):
        return False
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    language = repo.get("language") or "Unknown"
    stars = repo.get("stargazers_count", 0)
    if topics & GOOD_TOPICS:
        return True
    app_words = (
        "self-host", "self host", "self-hostable", "dashboard", "web app",
        "web application", "web-based", "server", "docker", "homelab",
        "monitoring", "automation", "manager", "tracker", "platform",
        "alternative to", "self-hosted", "deploy", "api for", "app for",
    )
    if any(w in desc for w in app_words):
        return True
    return (
        language not in {"Unknown", "Shell", "Markdown", "HTML", "TeX"}
        and stars >= 30
        and len(desc) >= 25
    )


# ---------------------------------------------------------------------------
# NEW: Fresh-gem gate — gentler, accepts low-star repos with real signals.
# ---------------------------------------------------------------------------
def passes_fresh_gate(repo):
    """A gentler gate for fresh repos (1+ stars). Does NOT require self-host
    keywords — a new repo might not know to tag itself 'self-hosted' yet.
    Still rejects the same junk (awesome-lists, templates, libraries, etc.).
    """
    if hard_reject(repo):
        return False
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    language = repo.get("language") or "Unknown"
    stars = repo.get("stargazers_count", 0)
    # Must have at least 1 star and a real language.
    if stars < 1:
        return False
    if language in {"Unknown", "Shell", "Markdown", "HTML", "TeX"}:
        # Still allow if it has self-hosted topics (some Docker-only repos are Shell).
        if not (topics & GOOD_TOPICS):
            return False
    # Must have at least one topic OR self-host keywords in description.
    if not topics and not any(w in desc for w in (
        "self-host", "self host", "docker", "homelab", "web app", "web application",
        "server", "dashboard", "platform", "alternative to", "deploy",
    )):
        return False
    # Must have a real description (at least 15 chars — gentler than v7's 25).
    if len(desc) < 15:
        return False
    return True


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
    # --- Momentum: velocity = stars/age. Key for fresh gems. ---
    velocity = stars / age_years
    b["momentum"] = round(min(24.0, 8.0 * math.log10(velocity + 1)), 2)

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

    # --- Selfhost signal: same as v7 ---
    b["selfhost_signal"] = 5.0 if ({t.lower() for t in topics} & GOOD_TOPICS) else 0.0

    # --- Community: same as v7 (but note: 0 for <20★ repos) ---
    if stars >= 20:
        ratio = forks / (stars + 1)
        b["community"] = 4.0 if 0.03 <= ratio <= 0.3 else (0.0 if ratio > 0.6 else 2.0)
    else:
        b["community"] = 0.0

    # --- NEW: Youth bonus — rewards genuinely new projects. ---
    # v7 had nothing here; a 5-star repo 2 weeks old scored the same as a
    # 5-star repo 3 years old. v9 gives a youth bonus so new repos surface.
    if age_days <= 90:
        b["youth"] = 6.0
    elif age_days <= 180:
        b["youth"] = 4.0
    elif age_days <= 365:
        b["youth"] = 2.0
    else:
        b["youth"] = 0.0

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
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        return r.stdout.strip() or None
    except Exception:
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
    last = None
    for attempt in range(6):
        try:
            return native_gh_api("/search/repositories", {
                "q": query, "sort": sort, "order": "desc",
                "per_page": "100", "page": str(page),
            })
        except RuntimeError as e:
            last = e
            msg = str(e).lower()
            if any(m in msg for m in _RATE_LIMIT_MARKERS):
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


def _add_search_results(pool, used, query, sort, max_pages, fresh_flag, stats):
    calls, fresh_count, added = 0, 0, 0
    max_pages = max(1, max_pages)
    for page in range(1, max_pages + 1):
        data = gh_search(query, sort, page)
        calls += 1
        items = data.get("items", [])
        if not items:
            stats["zeroYield"] += 1
            break
        for repo in items:
            if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
                continue
            key = (repo.get("full_name") or "").lower()
            if not key or key in pool:
                continue
            repo["_fresh"] = fresh_flag
            repo["_found_by"] = {"query": query, "sort": sort, "page": page}
            pool[key] = repo
            added += 1
            if key not in used and (repo.get("html_url") or "").lower() not in used:
                fresh_count += 1
        if len(items) < 100:
            break
    return calls, fresh_count, added


def gather_candidates(used, set_num, target_fresh, max_pages, pushed_after, stats,
                      do_fresh=True, bench_seed=None):
    """Bench-seeded discovery: previous run's vetted surplus + bounded sweeps.

    The bench (pre-vetted, unused candidates persisted by the previous run)
    seeds the pool first, so the sweeps below usually early-exit after a few
    calls. The quality sweep uses stars:>=8; the fresh sweep uses stars:>=1
    with `created` sort. Each repo gets a `_fresh` flag.
    """
    pool, calls, fresh_count = {}, 0, 0
    for repo in (bench_seed or []):
        key = (repo.get("full_name") or "").lower()
        if key and key not in pool:
            repo.setdefault("_found_by", {"query": "bench", "sort": "-", "page": 0})
            pool[key] = repo
            fresh_count += 1
    stats["benchSeeded"] = len(pool)
    if pool:
        print(f"[v11] bench: seeded {len(pool)} pre-vetted candidates from the previous run")
    now = datetime.now(timezone.utc)
    windows = _date_windows(now)

    # --- Research composite sweep: report-derived queries across all useful sorts. ---
    composite_queries = [q.format(**windows) for q in COMPOSITE_QUERY_TEMPLATES]
    composite_sorts = ["stars", "updated", "created"]
    COMPOSITE_MAX_CALLS = 12   # v11: was 30; bench covers the difference
    print(f"[v11] research sweep: {len(composite_queries)} composite queries, "
          f"sorts={','.join(composite_sorts)}, max_pages={max_pages} …")
    for query in composite_queries[set_num % len(composite_queries):] + composite_queries[:set_num % len(composite_queries)]:
        for sort in composite_sorts:
            if calls >= COMPOSITE_MAX_CALLS:
                break
            c, f, _ = _add_search_results(pool, used, query, sort, max_pages, False, stats)
            calls += c
            fresh_count += f
            if calls >= COMPOSITE_MAX_CALLS or (calls >= 6 and fresh_count >= target_fresh):
                break
        if calls >= COMPOSITE_MAX_CALLS or (calls >= 6 and fresh_count >= target_fresh):
            break
    print(f"[v11] research sweep done: {len(pool)} repos, {calls} calls, {fresh_count} fresh")

    # --- Quality sweep (v9/v7-style, stars:>=8) ---
    rt = set_num % len(SEARCH_TOPICS)
    topics = _interleave(list(SEARCH_TOPICS)[rt:] + list(SEARCH_TOPICS)[:rt])
    rp = set_num % len(SEARCH_PHRASES)
    phrases = list(SEARCH_PHRASES)[rp:] + list(SEARCH_PHRASES)[:rp]
    queries = [f"topic:{t} stars:>=8 pushed:>={pushed_after} archived:false" for t in topics]
    queries += [f"{p} stars:>=8 pushed:>={pushed_after} archived:false" for p in phrases]
    sort_rotation = ["stars", "updated", "created"]
    sorts = sort_rotation[set_num % len(sort_rotation):] + sort_rotation[:set_num % len(sort_rotation)]
    MIN_QUERIES, MAX_CALLS = 10, 44   # v11: was 24, 75 — hard total cap 60 incl. fresh sweep
    print(f"[v11] quality sweep: {len(queries)} queries, sorts={','.join(sorts)}, stars>=8 …")
    for query in queries:
        if calls >= MAX_CALLS:
            break
        for sort in sorts[:2]:
            if calls >= MAX_CALLS:
                break
            c, f, _ = _add_search_results(pool, used, query, sort, max_pages, False, stats)
            calls += c
            fresh_count += f
        if calls >= MIN_QUERIES and fresh_count >= target_fresh:
            break

    quality_pool_size = len(pool)
    print(f"[v11] quality sweep done: {quality_pool_size} repos, {calls} calls, "
          f"{fresh_count} fresh")

    if not do_fresh:
        return list(pool.values()), calls

    # --- Fresh-gem sweep: stars:>=1, created/updated sort, report-backed phrases. ---
    fresh_pushed_after = windows["pushed_12m"]
    fresh_topics = _interleave(list(SEARCH_TOPICS)[rt:] + list(SEARCH_TOPICS)[:rt])
    fresh_queries = [f"topic:{t} stars:>=1 pushed:>={fresh_pushed_after} archived:false"
                     for t in fresh_topics[:30]]  # top 30 topics for fresh sweep
    fresh_queries += [
        "stars:>=1 created:>%s archived:false" % windows["created_90d"],
        "stars:>=1 pushed:>%s archived:false" % fresh_pushed_after,
        '"self-hosted" in:readme stars:1..80 created:>%s archived:false' % windows["created_12m"],
        '"self-hosted alternative" in:readme stars:1..120 created:>%s archived:false' % windows["created_24m"],
        '"docker compose" in:readme stars:1..120 created:>%s archived:false' % windows["created_12m"],
    ]
    fresh_sorts = ["created", "updated"]
    FRESH_MIN_QUERIES, FRESH_MAX_CALLS = 8, 16   # v11: was 15, 25
    print(f"[v11] fresh-gem sweep: {len(fresh_queries)} queries, sorts={','.join(fresh_sorts)}, stars>=1 …")
    fresh_sweep_calls = 0
    for query in fresh_queries:
        if calls >= MAX_CALLS + FRESH_MAX_CALLS:
            break
        for fresh_sort in fresh_sorts:
            if calls >= MAX_CALLS + FRESH_MAX_CALLS:
                break
            c, f, _ = _add_search_results(pool, used, query, fresh_sort, max_pages, True, stats)
            calls += c
            fresh_sweep_calls += c
            fresh_count += f
        if fresh_sweep_calls >= FRESH_MIN_QUERIES and fresh_count >= target_fresh // 2:
            break

    print(f"[v11] fresh-gem sweep done: {len(pool) - quality_pool_size} new repos, "
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
    docker = any(node.get(k) for k in ("c1", "c2", "c3", "c4", "d1", "e1"))
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


def _owner_name(url):
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)", url.rstrip("/"))
    return (m.group(1), m.group(2)) if m else None


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
def load_awesome_index(now_ts, enabled):
    if not enabled:
        return set()
    cached = _load_json(AWESOME_CACHE, None)
    if cached and now_ts - cached.get("_ts", 0) < AWESOME_TTL:
        return set(cached.get("repos", []))
    url = "https://github.com/awesome-selfhosted/awesome-selfhosted-data/archive/refs/heads/master.tar.gz"
    try:
        print("[awesome] downloading curated self-hosted index (once / 30d) …")
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read()
        repos = set()
        pat = re.compile(rb"source_code_url:\s*['\"]?(https://github\.com/[^/\s'\"]+/[^/\s'\"]+)")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not (m.isfile() and m.name.endswith(".yml") and "/software/" in m.name):
                    continue
                f = tar.extractfile(m)
                if not f:
                    continue
                for match in pat.findall(f.read()):
                    on = _owner_name(match.decode().rstrip(".git").rstrip("/"))
                    if on:
                        repos.add(f"{on[0]}/{on[1]}".lower())
        _save_json(AWESOME_CACHE, {"_ts": now_ts, "repos": sorted(repos)})
        print(f"[awesome] indexed {len(repos)} curated self-hosted repos")
        return repos
    except Exception as e:
        print(f"[awesome] skipped (could not load: {type(e).__name__})")
        return set(cached.get("repos", [])) if cached else set()


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
def select_balanced(pool, cap, min_score, fresh_quota=0):
    """Two phases on the HEURISTIC category, plus an optional fresh-gem quota.

    Phase 1: seed the best-scored repo of each distinct category (broad coverage).
    Phase 2: fill to 50 by score under `cap` per category, relaxing if needed.
    Fresh quota: if >0, try to ensure at least `fresh_quota` fresh gems are in
    the set by boosting fresh gems that narrowly missed the cut.
    """
    selected, counts, taken = [], {}, set()

    # Phase 1: one per category (highest-scored within each).
    best_by_cat = {}
    for i, c in enumerate(pool):
        if c["score"] < min_score:
            continue
        if c["hcat"] not in best_by_cat:
            best_by_cat[c["hcat"]] = i
    for i in sorted(best_by_cat.values(), key=lambda j: pool[j]["score"], reverse=True):
        if len(selected) >= 50:
            break
        selected.append(i)
        taken.add(i)
        counts[pool[i]["hcat"]] = 1

    # Phase 2: fill by score under the cap, relaxing one notch at a time.
    cur_cap = cap
    eligible = sum(1 for c in pool if c["score"] >= min_score)
    while len(selected) < 50 and len(taken) < eligible:
        progressed = False
        for i, c in enumerate(pool):
            if len(selected) >= 50:
                break
            if i in taken or c["score"] < min_score:
                continue
            if counts.get(c["hcat"], 0) >= cur_cap:
                continue
            selected.append(i)
            taken.add(i)
            counts[c["hcat"]] = counts.get(c["hcat"], 0) + 1
            progressed = True
        if len(selected) >= 50:
            break
        if not progressed:
            cur_cap += 1
    return [pool[i] for i in selected]


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
        print(f"[v11] fresh-gem quota: swapped {swaps} non-fresh → fresh gems "
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
    title = tracker["titleTemplate"].format(n=set_num)
    if title in live_titles:
        raise RuntimeError(f"Title collision: {title} already exists in Notion")

    now = datetime.now(timezone.utc)
    now_ts = int(time.time())
    pushed_after = (now - timedelta(days=550)).strftime("%Y-%m-%d")
    stats = {"zeroYield": 0}

    bench_seed = []
    bench = _load_json(BENCH_CACHE, None)
    if bench and now_ts - bench.get("_ts", 0) < BENCH_TTL:
        for repo in bench.get("repos", []):
            u = (repo.get("html_url") or "").lower()
            fn = (repo.get("full_name") or "").lower()
            if u and u not in used and fn not in used:
                bench_seed.append(repo)

    print(f"[v11] gathering candidates for set {set_num} …")
    pool, calls = gather_candidates(
        used, set_num, args.target_fresh, args.max_pages, pushed_after, stats,
        do_fresh=not args.no_fresh, bench_seed=bench_seed)
    sweep_seconds = round(time.time() - t0, 1)
    print(f"[v11] {len(pool)} unique repos from {calls} search calls "
          f"({stats['zeroYield']} zero-yield queries skipped, {sweep_seconds}s)")

    # Score with the appropriate gate: fresh repos get the gentler gate.
    scored = []
    for repo in pool:
        if (repo.get("html_url") or "").lower() in used or (repo.get("full_name") or "").lower() in used:
            continue
        is_fresh = repo.pop("_fresh", False)  # remove temp flag before scoring
        gate_fn = passes_fresh_gate if is_fresh else passes_app_gate
        if not gate_fn(repo):
            continue
        s, b = appeal_score(repo, now=now)
        scored.append({"repo": repo, "score": s, "breakdown": dict(b), "fresh_gem": is_fresh})
    scored.sort(key=lambda c: c["score"], reverse=True)

    fresh_scored = sum(1 for c in scored if c.get("fresh_gem"))
    print(f"[v11] {len(scored)} candidates passed the gate and were scored "
          f"({fresh_scored} fresh gems)")

    awesome = load_awesome_index(now_ts, not args.no_awesome)
    repo_cache = _load_json(REPO_CACHE, {})

    # Diverse shortlist: round-robin by heuristic category (same as v7).
    SHORTLIST_MAX = 130
    by_cat = {}
    for c in scored:
        by_cat.setdefault(categorize(c["repo"]), []).append(c)
    cats = list(by_cat)
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
    print(f"[v11] confirming a diverse shortlist of {len(shortlist)} (across {len(by_cat)} categories)")

    audit = {"version": VERSION, "setNum": set_num, "searchCalls": calls,
             "zeroYieldQueries": stats["zeroYield"], "poolSize": len(pool),
             "scoredCount": len(scored), "freshScored": fresh_scored,
             "graphql": not args.no_graphql,
             "awesome": bool(awesome), "enrich": False, "rejected": [],
             "selected": [], "categoryDistribution": {}, "scoreStats": {},
             "dockerCount": 0, "freshCount": 0,
             "benchSeeded": stats.get("benchSeeded", 0), "benchSaved": 0,
             "sweepSeconds": sweep_seconds, "confirmSeconds": 0.0}

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
            base, bd = appeal_score(repo, now=now)
            in_awesome = repo["full_name"].lower() in awesome
            if repo.get("docker_signal"):
                bd["docker"] = 8.0
                base += 8.0
                audit["dockerCount"] += 1
            if in_awesome:
                bd["awesome"] = 6.0
                base += 6.0
            confirmed.append({"repo": repo, "score": round(base, 2), "breakdown": bd,
                              "docker": bool(repo.get("docker_signal")), "awesome": in_awesome,
                              "hcat": categorize(repo),
                              "fresh_gem": c.get("fresh_gem", False)})
    _save_json(REPO_CACHE, repo_cache)
    audit["confirmSeconds"] = round(time.time() - tc, 1)
    confirmed.sort(key=lambda c: c["score"], reverse=True)
    print(f"[v11] confirmed {len(confirmed)} live repos "
          f"({audit['dockerCount']} ship Docker/compose) ({time.time()-tc:.1f}s)")

    # Optional LLM enrichment — annotation + gentle tiebreak ONLY.
    if args.enrich:
        backend, key = resolve_llm_backend()
        if backend:
            audit["enrich"] = backend
            ecache = _load_json(ENRICH_CACHE, {})
            enrich_candidates(confirmed[:120], backend, key, ecache)
            _save_json(ENRICH_CACHE, ecache)
            for c in confirmed:
                e = c.get("enrich") or {}
                if e.get("self_hostable") is False:
                    c["breakdown"]["llm_selfhost"] = -ENRICH_SELFHOST_PENALTY
                    c["score"] = round(c["score"] - ENRICH_SELFHOST_PENALTY, 2)
                if isinstance(e.get("novelty"), int):
                    adj = round((e["novelty"] - 3) * ENRICH_NOVELTY_WEIGHT, 2)
                    if adj:
                        c["breakdown"]["llm_novelty"] = adj
                        c["score"] = round(c["score"] + adj, 2)
            confirmed.sort(key=lambda c: c["score"], reverse=True)
        else:
            print("[enrich] no ANTHROPIC/OPENROUTER key found — skipping enrichment")

    selected = select_with_fresh_quota(confirmed, args.cat_cap, args.min_score, args.fresh_quota)
    if len(selected) < 50:
        # REST search is mined out; fill the gap from v17's GraphQL lane (engines/topup.py).
        import topup
        selected, audit["topUp"] = topup.fill(VERSION, selected, 50, tracker, args.dry_run,
                                              ai_ceiling=None,
                                              is_ai_cat=lambda c: c.get("hcat") == "AI / LLM",
            cat_cap=getattr(args, "cat_cap", None))
    if len(selected) != 50:
        raise RuntimeError(
            f"Only {len(selected)} repos scored >= --min-score {args.min_score} "
            f"(confirmed pool {len(confirmed)}). Widen the search "
            f"(--target-fresh/--max-pages) or lower --min-score.")

    for c in selected:
        e = c.get("enrich") or {}
        c["category"] = c["hcat"]
        c["llm_category"] = e.get("category") if e.get("category") in ALLOWED_CATEGORIES else None

    cat_counts = {}
    for c in selected:
        cat_counts[c["category"]] = cat_counts.get(c["category"], 0) + 1
    audit["categoryDistribution"] = dict(sorted(cat_counts.items(), key=lambda kv: -kv[1]))
    audit["freshCount"] = sum(1 for c in selected if c.get("fresh_gem"))
    audit["selected"] = [{
        "url": c["repo"]["html_url"], "score": c["score"], "category": c["category"],
        "llm_category": c.get("llm_category"), "stars": c["repo"].get("stargazers_count", 0),
        "docker": c["docker"], "awesome": c["awesome"], "fresh_gem": c.get("fresh_gem", False),
        "hook": (c.get("enrich") or {}).get("interest_hook", ""),
        "breakdown": {k: round(v, 2) for k, v in c["breakdown"].items()},
        "composition": compose_line(c["breakdown"]),
    } for c in selected]
    sc = [c["score"] for c in selected]
    audit["scoreStats"] = {"min": min(sc), "max": max(sc), "mean": round(sum(sc) / len(sc), 2)}

    # v11: bank the vetted surplus for the next run (the "bench").
    sel_keys = {c["repo"]["full_name"].lower() for c in selected}
    dead = {u.lower() for u, _reason in audit["rejected"]}
    bench_out = []
    for c in scored:
        r = c["repo"]
        k = (r.get("full_name") or "").lower()
        if not k or k in sel_keys or (r.get("html_url") or "").lower() in dead:
            continue
        slim = {f: r.get(f) for f in _BENCH_FIELDS if f in r}
        slim["_fresh"] = c.get("fresh_gem", False)
        bench_out.append(slim)
        if len(bench_out) >= BENCH_MAX:
            break
    _save_json(BENCH_CACHE, {"_ts": now_ts, "setNum": set_num, "repos": bench_out})
    audit["benchSaved"] = len(bench_out)
    print(f"[v11] bench: saved {len(bench_out)} vetted candidates for the next run")

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
    blocks = [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [v1.rt(
            f"Set {set_num}: 50 self-hosted open-source apps, curated for quality and "
            f"interestingness (non-overlapping with all previous {set_num - 1} sets). Each "
            "repo was scored on momentum, recent activity, polish, self-host signal, "
            f"youth bonus and hidden-gem balance, then diversified across categories. "
            f"{docker_n} of 50 ship a Docker/compose file. {fresh_n} of 50 are fresh gems "
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
        tail = f" — {hook}" if hook else ""
        comp = compose_line(c["breakdown"])
        details = (
            f" — [{c['category']}]{badge}{star}{fresh} {repo.get('language') or 'Unknown'} — {desc[:340]}{tail} "
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
    aw = _load_json(AWESOME_CACHE, None)
    en = _load_json(ENRICH_CACHE, {})
    print(f"[stats] completedSets        : {tracker.get('completedSets')}  (next = {int(tracker.get('completedSets',0))+1})")
    print(f"[stats] usedRepoUrls         : {len(tracker.get('usedRepoUrls', []))}")
    print(f"[stats] completedPages       : {len(pages)}")
    print(f"[stats] v11 repo cache        : {len(rc)} entries ({live} live / {len(rc)-live} dead)")
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

    # v7's checks (still valid):
    check(not passes_app_gate({"full_name": "x/awesome-selfhosted", "description": "curated list",
                               "topics": ["awesome"], "language": "Markdown", "stargazers_count": 9999}),
          "rejects awesome-list")
    check(not passes_app_gate({"full_name": "y/starter", "description": "a boilerplate template",
                               "topics": ["template"], "language": "TS", "stargazers_count": 500}),
          "rejects template")
    check(not passes_app_gate({"full_name": "z/lib", "description": "a library for parsing JSON",
                               "topics": ["json"], "language": "Go", "stargazers_count": 800}),
          "rejects bare library")
    app = {"full_name": "a/prism", "description": "Self-hosted family dashboard with docker compose",
           "topics": ["self-hosted", "docker", "dashboard"], "language": "TypeScript",
           "stargazers_count": 800, "forks_count": 40, "created_at": "2025-06-01T00:00:00Z",
           "pushed_at": datetime.now(timezone.utc).isoformat(), "homepage": "https://prism.app",
           "license": {"spdx_id": "MIT"}}
    check(passes_app_gate(app), "accepts a real self-hosted app")
    s, b = appeal_score(app)
    check(s > 60 and "gem_balance" in b, f"scores a gem-zone app ({s})")
    check(appeal_score(dict(app, stargazers_count=90000))[0] < s, "damps mega-repos")
    check(categorize({"full_name": "a/ng-diagram", "description": "Angular diagram library",
                      "topics": ["diagram"]}) == "Image / Design / Creative", "diagram→Image")
    check(_parse_array('```json\n[{"id":"a/b"}]\n```') == [{"id": "a/b"}], "tolerant JSON parse")

    # v9 NEW checks:
    # 1. Fresh gate accepts a low-star repo with topics + description.
    fresh_repo = {"full_name": "a/newapp", "description": "A lightweight self-hosted task tracker",
                  "topics": ["task-management", "web-app"], "language": "Go",
                  "stargazers_count": 3, "forks_count": 1,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "pushed_at": datetime.now(timezone.utc).isoformat()}
    check(passes_fresh_gate(fresh_repo), "fresh gate accepts a 3-star repo with topics")

    # 2. Fresh gate rejects a repo with no topics and no description.
    check(not passes_fresh_gate({"full_name": "b/empty", "description": "x",
                                 "topics": [], "language": "Python", "stargazers_count": 1}),
          "fresh gate rejects no-topic repo")

    # 3. Fresh gem scoring: a 3-star repo created 2 weeks ago should score
    #    reasonably (not be crushed by gem_balance).
    s_fresh, b_fresh = appeal_score(fresh_repo)
    check(s_fresh > 40, f"fresh gem scores reasonably ({s_fresh})")
    check(b_fresh.get("youth", 0) == 6.0, "youth bonus = 6 for <90d repo")
    check(b_fresh.get("gem_balance", 0) > -3, f"fresh gem not harshly penalised (gem={b_fresh.get('gem_balance')})")

    # 4. is_fresh_gem identifies a new repo.
    check(is_fresh_gem(fresh_repo), "is_fresh_gem identifies new repo")
    old_repo = dict(fresh_repo, stargazers_count=5000,
                   created_at="2020-01-01T00:00:00Z")
    check(not is_fresh_gem(old_repo), "is_fresh_gem rejects old high-star repo")

    # 5. Diversity still works (same as v7 test).
    cats = ["AI / LLM", "Media / Streaming", "DevOps / Infra", "Notes / Knowledge",
            "Security / Auth", "Finance / Budget", "Automation"]
    pool = []
    sc = 100
    for j in range(40):
        pool.append({"score": sc, "hcat": "AI / LLM", "repo": {}, "breakdown": {}}); sc -= 1
    for cat in cats[1:]:
        for j in range(12):
            pool.append({"score": sc, "hcat": cat, "repo": {}, "breakdown": {}}); sc -= 1
    pool.sort(key=lambda c: c["score"], reverse=True)
    sel = select_balanced(pool, cap=5, min_score=0)
    ai = sum(1 for c in sel if c["hcat"] == "AI / LLM")
    distinct = len({c["hcat"] for c in sel})
    check(len(sel) == 50, "balanced fills exactly 50")
    check(ai <= 9 and distinct >= 7,
          f"balanced caps the dominant category (AI={ai}, distinct={distinct})")

    # 6. Fresh quota: with fresh gems in pool, they should appear in selection.
    pool2 = []
    sc = 100
    for j in range(40):
        pool2.append({"score": sc, "hcat": "AI / LLM", "repo": {"html_url": f"https://github.com/x/old{j}"},
                      "breakdown": {}, "fresh_gem": False}); sc -= 1
    for cat in cats[1:]:
        for j in range(12):
            pool2.append({"score": sc, "hcat": cat, "repo": {"html_url": f"https://github.com/x/{cat[:3]}{j}"},
                          "breakdown": {}, "fresh_gem": False}); sc -= 1
    # Add 15 fresh gems with decent scores
    for j in range(15):
        pool2.append({"score": 50, "hcat": "Web App / Other",
                      "repo": {"html_url": f"https://github.com/x/fresh{j}"},
                      "breakdown": {}, "fresh_gem": True})
    pool2.sort(key=lambda c: -c["score"])
    sel2 = select_with_fresh_quota(pool2, cap=5, min_score=0, fresh_quota=10)
    fresh_in = sum(1 for c in sel2 if c.get("fresh_gem"))
    check(fresh_in >= 5, f"fresh quota reserves slots for fresh gems (got {fresh_in})")

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
    print(f"  gate        : {'REJECT — ' + reject if reject else 'PASS'}")
    fresh = passes_fresh_gate(repo)
    print(f"  fresh gate  : {'PASS' if fresh else 'FAIL'}")
    print(f"  is_fresh    : {is_fresh_gem(repo)}")
    print(f"  category    : {categorize(repo)}")
    s, b = appeal_score(repo)
    res = confirm_repos([(url, _sig(repo))], {}, int(time.time()), not args.no_graphql)
    cr = res.get(url.lower())
    if cr and cr.get("docker_signal"):
        b["docker"] = 8.0
        s += 8.0
    print(f"  appeal      : {s:.1f}")
    print(f"  composition : {compose_line(b, top_pos=8)}")
    print(f"  breakdown   : " + ", ".join(f"{k}={v:g}" for k, v in b.items()))
    print(f"  docker/comp : {bool(cr and cr.get('docker_signal'))}")


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
        description="Publish the next set of 50 (v11: bench-backed fast discovery, "
                    "v9 selection core: fresh gems, diversity, all v7 features).")
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
                   help="Skip the fresh-gem sweep (v7-equivalent behaviour).")
    p.add_argument("--target-fresh", type=int, default=250)
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--cat-cap", type=int, default=5)
    p.add_argument("--min-score", type=float, default=25.0)
    p.add_argument("--fresh-quota", type=int, default=FRESH_QUOTA_DEFAULT,
                   help="Max slots reserved for fresh gems (default 10).")
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
    print(f"[v11] selected 50 | categories: "
          + ", ".join(f"{k}={v}" for k, v in audit["categoryDistribution"].items()))
    print(f"[v11] appeal min/mean/max = {audit['scoreStats']['min']}/{audit['scoreStats']['mean']}/"
          f"{audit['scoreStats']['max']} | {audit['dockerCount']} ship Docker | "
          f"{audit['freshCount']} fresh gems | timing: {audit['timingSeconds']}s")
    if args.breakdown:
        print("[v11] per-pick score composition:")
        for c in sorted(selected, key=lambda x: -x["score"]):
            fresh_mark = " ✨" if c.get("fresh_gem") else ""
            print(f"   {c['score']:>5.0f} [{c['category']:<26}] {c['repo']['full_name']:<38}{fresh_mark} "
                  f"{compose_line(c['breakdown'])}")

    if args.dry_run:
        ap = v1.AUDIT_DIR / f"set{set_num}_{VERSION}_DRYRUN_audit.json"
        ap.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
        print(f"[dry-run] would publish: {title}")
        print(f"[dry-run] audit -> {ap}")
        return

    if len(selected) != 50:
        raise SystemExit(f"[{VERSION}] selected {len(selected)}/50 repos; refusing an incomplete standard page.")

    acquire_lock()
    try:
        page = v1.notion_request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": tracker["parentPageId"]},
            "properties": {"title": {"title": [v1.rt(title)]}}})
        v1.append_blocks(page["id"], page_blocks(set_num, selected))
        urls = [c["repo"]["html_url"] for c in selected]
        entry = {"setNum": set_num, "title": title, "pageId": page["id"],
                 "pageUrl": v1.notion_url(title, page["id"]), "repos": urls}
        tracker["completedSets"] = set_num
        tracker["completedPages"].append(entry)
        merged, seen = [], set()
        for u in tracker.get("usedRepoUrls", []) + urls:
            if u.lower() not in seen:
                seen.add(u.lower())
                merged.append(u)
        tracker["usedRepoUrls"] = merged
        v1.write_tracker(tracker)
        append_master_csv(set_num, selected)
        (v1.AUDIT_DIR / f"set{set_num}_{VERSION}_verified_audit.json").write_text(
            json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    finally:
        release_lock()

    print(json.dumps(entry, indent=2, ensure_ascii=False))
    print(f"usedRepoUrls={len(tracker['usedRepoUrls'])}")
    print(f"master CSV -> {v1.MASTER_CSV.name} (+{len(selected)} rows)")


if __name__ == "__main__":
    main()
