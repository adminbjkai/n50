"""
quality.py — screen any engine's selection with v17's quality gate (shared by v11–v16).

The REST-search engines (v11–v16) each have their own, older junk rules. In practice their
sets shipped deployment templates, compose bundles, 0-star repos, AI-account proxies,
native clients, add-ons for other apps and the same owner several times. Before an engine's
shortfall check, `screen()` drops those picks; the top-up lane (topup.py) then refills the
freed slots from v17's pool. Each engine keeps its own discovery, scoring and diversity.

A pick is dropped when it:
  * fails v17's gate on search-level fields (junk patterns, stale, unreadable or missing
    description, fork/archived/template, built on another app)
  * has no code (no primary language: a README/guide repo) and no Dockerfile/compose
  * has fewer than LEGACY_MIN_STARS stars. The legacy engines feature "fresh gems" (their
    median pick has 4–6 stars), so v17's 20-star floor would erase their style; 3 still
    removes repos nobody outside the author has looked at.
  * shows no web-app or self-host intent (topics or description)
  * is an AI repo whose description shows no web UI / app
  * repeats an owner already in the set (the higher-scored pick is kept)
  * has the same description as a repo already published (re-uploads / mirrors)
  * is a renamed or transferred copy of a published repo (GitHub redirect check)

Kept picks are relabelled with v17's categorizer (description-first overrides on top of v16's
keyword rules), so every engine's page and CSV use the same, more accurate categories. This
happens after selection, so it changes labels only, not which repos were chosen.
"""

import csv
import re
from datetime import datetime, timezone

LEGACY_MIN_STARS = 3
AI_WORDS = re.compile(r"\b(ai|llm|gpt|agent|agents|rag|chatbot|openai|claude|mcp)\b", re.I)


def _adapt(c):
    """Legacy candidate -> the repo shape v17.gate reads."""
    r = c["repo"]
    lic = r.get("license") or {}
    return {
        "full_name": r.get("full_name") or "", "html_url": r.get("html_url") or "",
        "description": (r.get("description") or "").strip(),
        "topics": list(r.get("topics") or []), "language": r.get("language"),
        "stargazers_count": r.get("stargazers_count") or 0,
        "created_at": r.get("created_at") or "", "pushed_at": r.get("pushed_at") or "",
        "homepage": r.get("homepage") or "", "license": lic if isinstance(lic, dict) else {},
        "_bad_flags": bool(r.get("fork") or r.get("archived") or r.get("is_template")),
        "_cat": c.get("category") or c.get("hcat") or "",
        "compose": bool(c.get("compose")), "dockerfile": bool(c.get("dockerfile") or c.get("docker")),
    }


def _published_descriptions(v17, ignore_set=None):
    out = set()
    try:
        with open(v17.v1.MASTER_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if ignore_set is not None and row.get("set") == str(ignore_set):
                    continue
                d = v17._norm_desc(row.get("description"))
                if len(d) >= 20:
                    out.add(d)
    except OSError:
        pass
    return out


def why_not(c, v17, now, used_desc):
    """Reason string if the pick should be dropped, else None."""
    r = _adapt(c)
    # Stars are judged by the legacy floor below, so lift them past v17's own floor here.
    why = v17.gate(dict(r, stargazers_count=10 ** 6), now, curated=bool(c.get("awesome")), light=True)
    if why:
        return why
    if not r["language"] and not (r["compose"] or r["dockerfile"] or c.get("awesome")):
        return "no-code (docs-only repo)"
    if r["stargazers_count"] < LEGACY_MIN_STARS:
        return f"too-few-stars (<{LEGACY_MIN_STARS})"
    topics = {t.lower() for t in r["topics"]}
    if not (topics & (v17.SELFHOST_TOPICS | v17.WEBAPP_TOPICS) or v17.WEB_INTENT.search(r["description"])
            or c.get("awesome")):
        return "no-web-app-intent"
    # The AI rule follows the description, not the engine's category (which can be wrong).
    if AI_WORDS.search(r["description"]) and not v17.AI_APP.search(r["description"]):
        return "ai-without-web-ui"
    if v17._norm_desc(r["description"]) in used_desc:
        return "same-description-as-published"
    return None


def _renamed_copies(v17, selected, tracker, ignore_set):
    """Full names (lowercase) of picks that are renamed/transferred copies of published repos."""
    own = set()
    for p in tracker.get("completedPages", []):
        if ignore_set is not None and str(p.get("setNum")) == str(ignore_set):
            own.update(u.lower().rstrip("/") for u in p.get("repos", []))
    used = {u.lower().rstrip("/") for u in tracker.get("usedRepoUrls", [])} - own
    used_full = {u.split("github.com/")[-1] for u in used if "github.com/" in u}
    by_name = {}
    for fn in used_full:
        by_name.setdefault(fn.split("/")[-1], []).append(fn)
    cands = [{"full_name": c["repo"].get("full_name") or ""} for c in selected]
    return v17.rename_duplicates(cands, used, by_name)


def screen(version, selected, ignore_set=None, tracker=None):
    """Return (kept, record). Order of `selected` is preserved for kept picks.
    `ignore_set` skips that set's own entries in the published checks (repairs); `tracker`
    enables the renamed/transferred-copy check."""
    if not selected:
        return selected, None
    try:
        import v17  # lazy: shares the gate and patterns with the v17 engine
    except Exception as exc:  # noqa: BLE001 — never block a run on the screen itself
        print(f"[{version}] quality screen unavailable ({exc}); keeping the selection as is")
        return selected, {"error": str(exc)}
    now = datetime.now(timezone.utc)
    used_desc = _published_descriptions(v17, ignore_set)
    renamed = _renamed_copies(v17, selected, tracker, ignore_set) if tracker else set()
    reasons = {id(c): ("renamed-copy-of-published" if (c["repo"].get("full_name") or "").lower() in renamed
                       else why_not(c, v17, now, used_desc)) for c in selected}
    best_by_owner = {}                      # best-scored *passing* pick per owner
    for c in selected:
        if reasons[id(c)]:
            continue
        o = (c["repo"].get("full_name") or "").split("/")[0].lower()
        if o not in best_by_owner or c.get("score", 0) > best_by_owner[o].get("score", 0):
            best_by_owner[o] = c
    kept, dropped, relabelled = [], [], []
    for c in selected:
        name = c["repo"].get("full_name") or ""
        why = reasons[id(c)]
        if not why and best_by_owner.get(name.split("/")[0].lower()) is not c:
            why = "duplicate-owner"
        if why:
            dropped.append({"repo": name, "why": why})
            continue
        cat = (c.get("_v17") or {}).get("_cat") or v17.categorize(_adapt(c))
        if cat != (c.get("category") or c.get("hcat")):
            relabelled.append({"repo": name, "from": c.get("category") or c.get("hcat"), "to": cat})
        c["category"] = c["hcat"] = cat
        kept.append(c)
    if dropped:
        print(f"[{version}] quality screen: dropped {len(dropped)} of {len(selected)} picks that fail v17's gate:")
        for d in dropped:
            print(f"[{version}]   - {d['repo']:<42} {d['why']}")
    else:
        print(f"[{version}] quality screen: all {len(selected)} picks pass v17's gate")
    if relabelled:
        print(f"[{version}] quality screen: {len(relabelled)} picks relabelled with v17's categories")
    return kept, {"checked": len(selected), "dropped": dropped, "relabelled": relabelled}


def self_test():
    import sys
    sys.dont_write_bytecode = True
    now = datetime.now(timezone.utc)

    def mk(name, desc, stars=150, topics=("self-hosted",), score=50, language="Go", docker=True):
        return {"repo": {"full_name": name, "html_url": f"https://github.com/{name}", "description": desc,
                         "topics": list(topics), "stargazers_count": stars, "language": language,
                         "created_at": "2025-01-01T00:00:00Z", "pushed_at": now.strftime("%Y-%m-%dT00:00:00Z")},
                "score": score, "category": "Web App / Other", "docker": docker}
    sel = [
        mk("good/app", "Self-hosted recipe manager with a clean web UI"),
        mk("good/two", "Self-hosted bookmark manager with a web UI", score=40),       # same owner, lower
        mk("zero/stars", "Self-hosted recipe manager with a clean web UI", stars=0),
        mk("tpl/x", "Railway deployment template for CopilotKit with persistent storage"),
        mk("cli/sig", "A dead simple tool to sign files and verify digital signatures.", topics=()),
        mk("ai/proxy", "Gemini web to OpenAI-compatible API, self-hosted, no API key"),
        mk("ai/infra", "Connect your AI agents and LLM harnesses with any provider, self-hosted"),
        mk("vpn/apps", "Open-source VPN clients for Android and Windows with a self-hosted panel"),
        mk("ok/ai", "Self-hosted AI chat web UI for your team"),
        mk("gem/new", "Self-hosted habit tracker web app for families", stars=4),
        mk("lc/chat", "Enhanced ChatGPT Clone: agents, MCP, multiple AI providers, self-hosted"),
        mk("dup/bad", "Railway deployment template for a chat app", score=99),     # best score but fails
        mk("dup/good", "Self-hosted kanban board with a web UI", score=10),        # so this one is kept
        mk("ai/pwa", "Self-hosted PWA to control Claude Code or any terminal from your phone"),
        dict(mk("rr/resume", "A one-of-a-kind resume builder that keeps your privacy in mind."),
             category="AI / LLM"),                                                 # wrong engine category
        mk("doc/only", "DuckDNS, Nginx Proxy Manager and free HTTPS for the server on your desk",
           language=None, docker=False),                                           # a README, no code
        mk("tier/c", "Self-hosted habit tracker web app for small teams", language="Rust", docker=False),
    ]
    kept, rec = screen("test", sel)
    names = [c["repo"]["full_name"] for c in kept]
    ok = names == ["good/app", "ok/ai", "gem/new", "lc/chat", "dup/good", "ai/pwa", "rr/resume", "tier/c"]
    print(f"  {'ok ' if ok else 'FAIL'} kept {names}")
    print("quality self-test:", "ALL PASSED" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if ("--self-test" in sys.argv and self_test()) else (0 if "--self-test" not in sys.argv else 1))
