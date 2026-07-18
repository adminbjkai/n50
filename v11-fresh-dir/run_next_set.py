#!/usr/bin/env python3
"""
run_next_set.py — publish the next *verified* set of 50 self-hosted apps to Notion.

Best-of-both runner: the rigorous verification logic from the M3 Max MacBook
(`run_next_verified_selfhosted_set.py`) made fully portable so it runs straight
out of this folder on any machine (Linux PC or Mac).

What it does, end to end:
  1. Loads the local tracker (next set number = completedSets + 1).
  2. Refuses to run if a page with the next title already exists under AI Hub.
  3. Seeds candidates from an archived Notion draft page (with a hardcoded
     fallback list) + a 10-query GitHub search sweep (~450 candidates).
  4. For each candidate: dedup against every prior set, resolve the canonical
     repo via the GitHub API (drops forks / archived / disabled), then apply a
     self-hosted-signal heuristic. Stops at exactly 50.
  5. Creates the Notion child page under AI Hub and appends the 50 repos.
  6. Updates the tracker in place and writes a full audit JSON to ./_tmp/.

Requirements:
  - python3 (stdlib only)
  - `gh` GitHub CLI, authenticated:  gh auth login   (or GH_TOKEN env var)
  - `curl`
  - A Notion integration token with access to the AI Hub page (see auth below).

Auth (first match wins):
  - env var  NOTION_API_KEY
  - file     $NOTION_API_KEY_FILE   (if set)
  - file     ~/.config/notion/api_key
The token is the OpenClaw "notion" skill key (starts with `ntn_`). Do NOT hardcode
it in this file or commit it.

Run:
  cd /apps/notion50/notion50new
  export NOTION_API_KEY="ntn_..."          # or put it in ~/.config/notion/api_key
  python3 run_next_set.py                   # publishes the next set, updates tracker
  python3 run_next_set.py --dry-run         # select + audit only, no Notion writes, no tracker change
"""

import argparse
import csv
import json
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACKER = HERE / "notion-selfhosted-tracker.json"
AUDIT_DIR = HERE / "_tmp"
# Live master CSV — one row per repo across all sets. Appended (never rewritten)
# at the end of every run, using repo data already fetched (zero extra API calls).
MASTER_CSV = HERE / "notion-selfhosted-master.csv"
MASTER_CSV_HEADER = ["set", "repo_name", "category", "repo_url", "description"]

# Notion API version. The Mac production runner used 2026-03-11; the OpenClaw
# backup skill docs documented 2025-09-03. 2026-03-11 is the proven-working one.
NOTION_VERSION = "2026-03-11"

# Archived Notion draft page used as a seed source for fresh repo URLs.
ARCHIVED_DRAFT_PAGE = "37f41186-19e5-812b-8ec0-f4ad1de80ab0"
ARCHIVED_DRAFT_FALLBACK_REPOS = [
    "https://github.com/SimonSaysGiveMeSmile/SoA-Web",
    "https://github.com/rejourneyco/rejourney",
    "https://github.com/rahmanef63/os-vps",
    "https://github.com/sourcesimian/mqtt-panel",
    "https://github.com/Nystik-gh/ignis",
    "https://github.com/slopsmith/slopsmith",
    "https://github.com/kirill-markin/flashcards-open-source-app",
    "https://github.com/AbhijeetP21/ClipSync",
    "https://github.com/theoden8/webspace_app",
    "https://github.com/eurohouse/eurohouse",
    "https://github.com/Ab-code520/kick-miner-pro",
    "https://github.com/RobXYZ/viofosync",
    "https://github.com/HirotakaDango/PHP-Music",
    "https://github.com/barsoom/ex-remit",
    "https://github.com/TNT-Likely/BeeCount-Cloud",
    "https://github.com/TJGigs/Bose-SoundTouch-Hybrid-2026",
    "https://github.com/robeertm/shelly-energy-analyzer",
    "https://github.com/JigSawFr/asustor-runtipi",
    "https://github.com/PrzemekSkw/totp-sync",
    "https://github.com/thisistonydang/server-setup",
    "https://github.com/Li-Evan/Bloom",
    "https://github.com/palavido-dev/gratis-gis",
    "https://github.com/mstrhakr/audplexus",
    "https://github.com/CarterPerez-dev/CertGames-Core",
    "https://github.com/ivanxgb/bridge-app",
    "https://github.com/compartmentdev/compartment",
    "https://github.com/basecamp/once",
    "https://github.com/dhruv282/expense-manager",
    "https://github.com/AlessioLaiso/tunetuna",
    "https://github.com/amnya/truenas-app-icon-manager",
    "https://github.com/BlocUnited-LLC/mozaiks",
    "https://github.com/cdsaidev/Pulse",
    "https://github.com/paritytech/polkadot-hub-app",
    "https://github.com/muink/luci-app-tinyfilemanager",
    "https://github.com/patillacode/piruetas",
    "https://github.com/tophers/MixerBee",
    "https://github.com/gwwo/arranger",
    "https://github.com/Insanity404/Clone-Sidekick",
    "https://github.com/vul-os/vulos",
    "https://github.com/NRCOM/Unwatched",
    "https://github.com/arespawn/WhatsAppToDiscord",
    "https://github.com/pinkpixel-dev/ByteBox",
    "https://github.com/smskit/smskit",
    "https://github.com/Akuma1tko/ChatGPT-WebView",
    "https://github.com/neverinfamous/do-manager",
    "https://github.com/yshalsager/mini-baheth",
    "https://github.com/madebyaris/time-tracker-freelance",
    "https://github.com/Seanime-contributions/Seanime-Android",
    "https://github.com/bljohnsondev/mishos",
    "https://github.com/gynet/keewebx",
]


def load_notion_key():
    """Resolve the Notion token without ever hardcoding it. First match wins."""
    key = os.environ.get("NOTION_API_KEY")
    if key:
        return key.strip()
    candidates = []
    if os.environ.get("NOTION_API_KEY_FILE"):
        candidates.append(Path(os.environ["NOTION_API_KEY_FILE"]))
    candidates.append(Path.home() / ".config" / "notion" / "api_key")
    for path in candidates:
        if path.is_file():
            return path.read_text().strip()
    raise SystemExit(
        "No Notion token found. Set NOTION_API_KEY, or NOTION_API_KEY_FILE, "
        "or create ~/.config/notion/api_key"
    )


NOTION_KEY = None  # populated in main() so --help works without a token


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{cmd!r}\n{result.stderr}\n{result.stdout}")
    return result.stdout


def gh_api(path, params=None):
    cmd = ["gh", "api", path, "-X", "GET"]
    for key, value in (params or {}).items():
        cmd += ["-f", f"{key}={value}"]
    return json.loads(run(cmd))


def notion_request(method, endpoint, payload=None):
    cmd = [
        "curl", "-sS", "-X", method,
        f"https://api.notion.com/v1/{endpoint}",
        "-H", f"Authorization: Bearer {NOTION_KEY}",
        "-H", f"Notion-Version: {NOTION_VERSION}",
        "-H", "Content-Type: application/json",
    ]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    data = json.loads(run(cmd))
    if data.get("object") == "error":
        raise RuntimeError(json.dumps(data, indent=2))
    return data


def fetch_blocks(block_id):
    blocks = []
    cursor = None
    while True:
        endpoint = f"blocks/{block_id}/children?page_size=100"
        if cursor:
            endpoint += f"&start_cursor={cursor}"
        data = notion_request("GET", endpoint)
        blocks.extend(data["results"])
        if not data.get("has_more"):
            return blocks
        cursor = data["next_cursor"]


def extract_github_urls_from_notion_page(page_id):
    text = json.dumps(fetch_blocks(page_id))
    urls = []
    seen = set()
    for raw in re.findall(r"https://github\.com/[^\s\]\\\"\)<>]+", text):
        clean = raw.replace("\\/", "/").split("#")[0].split("?")[0].rstrip(".,;:")
        match = re.match(r"(https://github\.com/[^/]+/[^/,\s]+)", clean)
        if not match:
            continue
        repo = match.group(1).rstrip(".")
        key = repo.lower()
        if key not in seen:
            seen.add(key)
            urls.append(repo)
    return urls


def canonical_repo(url):
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)$", url.rstrip("/"))
    if not match:
        return None
    owner, repo = match.groups()
    try:
        data = gh_api(f"/repos/{owner}/{repo}")
    except RuntimeError:
        return None
    if data.get("archived") or data.get("fork") or data.get("disabled"):
        return None
    return data


def looks_like_selfhosted_app(repo):
    name = repo.get("full_name", "").lower()
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    language = repo.get("language") or "Unknown"
    bad_terms = [
        "awesome", "guide", "curated list", "collection of", "roadmap",
        "template", "theme", "starter", "dotfiles", "wallpaper",
        "leetcode", "interview", "mobile client", "android client",
    ]
    if any(term in name or term in desc for term in bad_terms):
        return False
    good_topics = {
        "self-hosted", "selfhosted", "docker", "docker-compose", "web",
        "webapp", "web-app", "dashboard", "admin", "homelab", "server",
        "monitoring", "automation", "cms", "crm", "analytics", "ai",
        "notion", "notes", "kanban", "collaboration", "media-server",
    }
    good_words = [
        "self-host", "self host", "dashboard", "admin", "web app",
        "web application", "server", "docker", "homelab", "monitoring",
        "automation", "collaboration", "cms", "crm", "analytics",
        "platform", "manager", "tracker", "notion", "kanban", "media",
    ]
    if topics & good_topics:
        return True
    if any(word in desc for word in good_words):
        return True
    return language not in {"Unknown", "Shell"} and repo.get("stargazers_count", 0) >= 50


# Broad, rotating query bank. After ~160 sets the original 10 "recently-updated"
# queries kept resurfacing the same already-harvested popular repos, so discovery
# now sweeps many more topics across BOTH sort orders and digs deeper into pages,
# stopping as soon as it has enough *fresh* (not-yet-used) candidates.
SEARCH_TOPICS = [
    "self-hosted", "selfhosted", "homelab", "dashboard", "web-app", "webapp",
    "docker", "docker-compose", "kubernetes", "monitoring", "automation",
    "cms", "crm", "analytics", "kanban", "notes", "note-taking", "bookmarks",
    "rss", "media-server", "music-server", "photos", "file-sharing", "wiki",
    "password-manager", "task-management", "project-management", "collaboration",
    "finance", "budgeting", "recipe", "fitness", "calendar", "feed-reader",
    "ai", "llm", "chatbot", "raspberry-pi", "nas", "proxy", "vpn", "backup",
]
SEARCH_PHRASES = [
    '"self-hosted" "docker compose"', '"self-hosted" "web app"',
    '"homelab" dashboard', '"admin dashboard" docker',
    '"open source" "web application" docker', '"AI" "webui" self-hosted',
    '"notion" self-hosted', '"kanban" "docker compose"',
    '"self hostable" web', '"docker compose" dashboard',
]


def search_candidates(used=None, target_fresh=220):
    """Return candidate repo URLs, prioritising ones NOT already used.

    Sweeps the topic + phrase query bank across both sort orders and several
    pages, and returns early once `target_fresh` previously-unused URLs are found
    (keeps API usage and wall-clock bounded). Falls back to returning everything
    collected if it can't reach the fresh target.
    """
    used = used or set()
    queries = [f"topic:{t} stars:>=8 pushed:>=2024-06-01" for t in SEARCH_TOPICS]
    queries += [f"{p} stars:>=8 pushed:>=2024-06-01" for p in SEARCH_PHRASES]

    urls = []
    seen = set()
    fresh = 0
    for query in queries:
        for sort in ("updated", "stars"):
            for page in range(1, 5):
                data = gh_api("/search/repositories", {
                    "q": query,
                    "sort": sort,
                    "order": "desc",
                    "per_page": "100",
                    "page": str(page),
                })
                items = data.get("items", [])
                for repo in items:
                    url = repo["html_url"]
                    key = url.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    urls.append(url)
                    if key not in used:
                        fresh += 1
                if len(items) < 100:
                    break  # no more pages for this query/sort
            if fresh >= target_fresh:
                # put fresh ones first so the filter loop reaches 50 quickly
                urls.sort(key=lambda u: u.lower() in used)
                return urls
    urls.sort(key=lambda u: u.lower() in used)
    return urls


def rt(text, link=None, bold=False):
    item = {
        "type": "text",
        "text": {"content": text[:2000]},
        "annotations": {"bold": bold},
    }
    if link:
        item["text"]["link"] = {"url": link}
    return item


# Ordered most-specific-first; the first rule that matches a repo's topics/name/
# description wins. Used for the master CSV "category" column.
CATEGORY_RULES = [
    ("AI / LLM", ["llm", "gpt", "chatbot", "rag", "openai", "ollama", "ai-agent",
                  "agents", "machine-learning", "ml", "ai"]),
    ("Media / Streaming", ["media", "video", "music", "audio", "streaming", "plex",
                           "jellyfin", "podcast", "movie", "photo", "photos", "gallery"]),
    ("Monitoring / Observability", ["monitoring", "observability", "metrics", "grafana",
                                    "prometheus", "uptime", "status-page", "logging", "logs"]),
    ("DevOps / Infra", ["kubernetes", "k8s", "helm", "gitops", "terraform", "ansible",
                        "ci-cd", "devops", "cluster", "infrastructure", "homelab-as-code"]),
    ("Networking / VPN", ["vpn", "wireguard", "tailscale", "dns", "proxy", "traefik",
                          "firewall", "gateway", "network"]),
    ("Security / Auth", ["password", "vault", "secrets", "2fa", "totp", "sso", "oauth",
                         "identity", "auth", "security"]),
    ("Notes / Knowledge", ["notes", "note-taking", "wiki", "knowledge-base", "markdown",
                           "bookmark", "bookmarks", "read-it-later", "rss", "feed-reader"]),
    ("Productivity / Tasks", ["kanban", "todo", "task-management", "project-management",
                              "calendar", "time-tracking", "productivity", "notion"]),
    ("Finance / Budget", ["finance", "budget", "budgeting", "expense", "accounting",
                          "invoice", "invoicing", "crypto", "bitcoin", "btc"]),
    ("Files / Storage / Backup", ["file-sharing", "storage", "backup", "sync", "nextcloud",
                                  "cloud-storage", "drive", "s3"]),
    ("CRM / Business", ["crm", "erp", "helpdesk", "ticketing", "support", "sales", "business"]),
    ("Communication / Social", ["chat", "messaging", "forum", "email", "mail", "social",
                                "comments", "discord", "matrix"]),
    ("Health / Food / Fitness", ["health", "fitness", "workout", "recipe", "cooking",
                                 "nutrition", "diet", "meal"]),
    ("Automation", ["automation", "workflow", "n8n", "scraper", "bot", "cron", "self-hosted-automation"]),
    ("Dashboard / Homelab", ["dashboard", "homelab", "homepage", "startpage", "nas",
                             "home-server", "self-hosted", "selfhosted"]),
    ("Analytics / Data", ["analytics", "data", "database", "etl", "bi"]),
    ("CMS / Website", ["cms", "blog", "website", "static-site", "publishing", "e-commerce", "ecommerce"]),
    ("Docker / Containers", ["docker", "docker-compose", "container", "containers"]),
]


def categorize(repo):
    """Best-effort single category from a repo's topics / name / description."""
    name = (repo.get("full_name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in repo.get("topics", [])}
    text = f"{name} {desc} {' '.join(topics)}"
    for label, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in topics or re.search(r"\b" + re.escape(kw) + r"\b", text):
                return label
    return "Web App / Other"


def master_csv_row(set_num, repo):
    desc = (repo.get("description") or "").replace("\n", " ").replace("\r", " ").strip()
    return [set_num, repo.get("full_name", ""), categorize(repo), repo.get("html_url", ""), desc]


def append_master_csv(set_num, repos):
    """Append one row per repo to the live master CSV (creates header if new)."""
    new_file = not MASTER_CSV.exists()
    with open(MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(MASTER_CSV_HEADER)
        for repo in repos:
            writer.writerow(master_csv_row(set_num, repo))


def page_blocks(set_num, repos):
    blocks = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    rt(
                        f"This set is intentionally non-overlapping with all previous {set_num - 1} sets. "
                        "Repositories were canonicalized through the GitHub API and checked against the rebuilt tracker before publication."
                    )
                ]
            },
        },
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt("Top 50 repos")]}},
    ]
    for repo in repos:
        desc = repo.get("description") or "Open-source self-hostable project."
        topics = ", ".join(repo.get("topics", [])[:6]) or "no topics"
        details = (
            f" — {repo.get('language') or 'Unknown'} — {desc[:420]} "
            f"(★ {repo.get('stargazers_count', 0):,}; updated {repo.get('pushed_at', '')[:10]}; topics: {topics})"
        )
        blocks.append({
            "object": "block",
            "type": "numbered_list_item",
            "numbered_list_item": {
                "rich_text": [
                    rt(repo["full_name"], link=repo["html_url"], bold=True),
                    rt(details),
                ]
            },
        })
    return blocks


def append_blocks(page_id, blocks):
    for i in range(0, len(blocks), 100):
        notion_request("PATCH", f"blocks/{page_id}/children", {"children": blocks[i:i + 100]})
        time.sleep(0.35)


def notion_url(title, page_id):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
    return f"https://www.notion.so/{slug}-{page_id.replace('-', '')}"


# Matches "Set 159 — ..." or "Set 1 - ..." at the start of a page title.
SET_NUM_RE = re.compile(r"^Set\s+(\d+)\b")


def write_tracker(tracker):
    """Atomic write so a crashed/concurrent run can't leave a half-written tracker."""
    tmp = TRACKER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracker, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(TRACKER)


def live_set_pages(parent_id):
    """Return {setNum: {'pageId', 'title'}} for every live 'Set N' child page under AI Hub.

    This is the cross-machine source of truth: the pages that actually exist in
    Notion, regardless of what any local tracker file claims.
    """
    pages = {}
    for block in fetch_blocks(parent_id):
        if block.get("type") != "child_page" or block.get("in_trash"):
            continue
        title = block["child_page"]["title"]
        match = SET_NUM_RE.match(title)
        if not match:
            continue  # report pages etc. — not part of the numbered series
        pages[int(match.group(1))] = {"pageId": block["id"], "title": title}
    return pages


def reconcile_with_notion(tracker):
    """Make the local tracker agree with Notion before doing anything.

    Pulls in any set that exists in Notion but is missing locally (e.g. another
    machine published it), merging both the set entry and its repos into the
    global dedup list. Returns a summary dict; mutates `tracker` in place.
    """
    live = live_set_pages(tracker["parentPageId"])
    known = {int(p["setNum"]) for p in tracker.get("completedPages", [])}
    missing = sorted(set(live) - known)

    pulled = []
    for set_num in missing:
        page = live[set_num]
        repos = extract_github_urls_from_notion_page(page["pageId"])
        tracker["completedPages"].append({
            "setNum": set_num,
            "title": page["title"],
            "pageId": page["pageId"],
            "pageUrl": notion_url(page["title"], page["pageId"]),
            "repos": repos,
        })
        # merge into the global dedup list, preserving order, case-insensitive
        seen = {u.lower() for u in tracker.get("usedRepoUrls", [])}
        for url in repos:
            if url.lower() not in seen:
                seen.add(url.lower())
                tracker.setdefault("usedRepoUrls", []).append(url)
        pulled.append({"setNum": set_num, "repos": len(repos)})

    tracker["completedPages"].sort(key=lambda p: int(p["setNum"]))
    # Authoritative latest = highest set that exists ANYWHERE (Notion or local).
    local_max = max(known) if known else 0
    live_max = max(live) if live else 0
    authoritative = max(local_max, live_max, int(tracker.get("completedSets", 0)))
    tracker["completedSets"] = authoritative

    return {
        "liveLatest": live_max,
        "localLatestBefore": local_max,
        "authoritativeLatest": authoritative,
        "pulled": pulled,
        "changed": bool(pulled) or authoritative != local_max,
        "liveTitles": {p["title"] for p in live.values()},
    }


def select_set(tracker, live_titles, dry_run=False):
    """Pick the next 50 verified, non-overlapping repos. Returns (set_num, title, selected, audit)."""
    used = {url.lower() for url in tracker.get("usedRepoUrls", [])}
    for page in tracker.get("completedPages", []):
        used.update(url.lower() for url in page.get("repos", []))

    set_num = int(tracker["completedSets"]) + 1
    title = tracker["titleTemplate"].format(n=set_num)

    # Belt-and-suspenders after reconcile: never create a duplicate page title.
    if title in live_titles:
        raise RuntimeError(f"Title collision: {title} already exists in Notion")

    # Optional small seed list (most are long-since used; the dedup loop drops those).
    # The old archived-draft Notion page is gone (404), so we no longer fetch it.
    seeds = [s for s in ARCHIVED_DRAFT_FALLBACK_REPOS if s.lower() not in used]
    candidate_urls = seeds + search_candidates(used=used)

    selected = []
    selected_keys = set()
    audit = {"draftSeeds": seeds, "rejected": [], "selected": []}
    for url in candidate_urls:
        if len(selected) == 50:
            break
        if url.lower() in used or url.lower() in selected_keys:
            audit["rejected"].append([url, "duplicate"])
            continue
        repo = canonical_repo(url)
        if not repo:
            audit["rejected"].append([url, "not-live-canonical-or-fork-archived"])
            continue
        canonical = repo["html_url"]
        key = canonical.lower()
        if key in used or key in selected_keys:
            audit["rejected"].append([canonical, "canonical-duplicate"])
            continue
        if not looks_like_selfhosted_app(repo):
            audit["rejected"].append([canonical, "not-enough-selfhosted-app-signal"])
            continue
        selected.append(repo)
        selected_keys.add(key)

    if len(selected) != 50:
        raise RuntimeError(f"Only selected {len(selected)} repos")
    return set_num, title, selected, audit


def main():
    global NOTION_KEY
    parser = argparse.ArgumentParser(description="Publish the next verified set of 50 to Notion.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Reconcile + select + write audit only. No Notion page, no published set.")
    parser.add_argument("--sync-only", action="store_true",
                        help="Only reconcile the local tracker with Notion's live state, then exit.")
    args = parser.parse_args()

    NOTION_KEY = load_notion_key()
    AUDIT_DIR.mkdir(exist_ok=True)
    tracker = json.loads(TRACKER.read_text())

    # --- Step 0: make local tracker agree with Notion (cross-machine source of truth) ---
    sync = reconcile_with_notion(tracker)
    if sync["pulled"]:
        print(f"[sync] pulled {len(sync['pulled'])} set(s) from Notion that were missing locally: "
              + ", ".join(f"#{p['setNum']}({p['repos']} repos)" for p in sync["pulled"]))
    print(f"[sync] latest set in Notion={sync['liveLatest']} · local-before={sync['localLatestBefore']} "
          f"· authoritative={sync['authoritativeLatest']}")
    if sync["changed"] and not args.dry_run:
        write_tracker(tracker)  # persist the pulled-in state immediately
        print(f"[sync] local tracker updated -> completedSets={tracker['completedSets']}, "
              f"usedRepoUrls={len(tracker['usedRepoUrls'])}")

    if args.sync_only:
        print("[sync] sync-only complete; nothing published.")
        return

    set_num, title, selected, audit = select_set(tracker, sync["liveTitles"], dry_run=args.dry_run)
    repos = [repo["html_url"] for repo in selected]
    audit["selected"] = repos

    if args.dry_run:
        audit_path = AUDIT_DIR / f"set{set_num}_DRYRUN_audit.json"
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
        print(f"[dry-run] would publish: {title}")
        print(f"[dry-run] selected 50 repos; audit -> {audit_path}")
        return

    page = notion_request("POST", "pages", {
        "parent": {"type": "page_id", "page_id": tracker["parentPageId"]},
        "properties": {"title": {"title": [rt(title)]}},
    })
    append_blocks(page["id"], page_blocks(set_num, selected))

    entry = {
        "setNum": set_num,
        "title": title,
        "pageId": page["id"],
        "pageUrl": notion_url(title, page["id"]),
        "repos": repos,
    }
    tracker["completedSets"] = set_num
    tracker["completedPages"].append(entry)
    merged = []
    seen = set()
    for url in tracker.get("usedRepoUrls", []) + repos:
        key = url.lower()
        if key not in seen:
            seen.add(key)
            merged.append(url)
    tracker["usedRepoUrls"] = merged
    write_tracker(tracker)

    # Append the 50 new rows to the live master CSV (uses already-fetched repo data).
    append_master_csv(set_num, selected)

    (AUDIT_DIR / f"set{set_num}_verified_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    print(f"usedRepoUrls={len(tracker['usedRepoUrls'])}")
    print(f"master CSV -> {MASTER_CSV.name} (+{len(selected)} rows)")


if __name__ == "__main__":
    main()
