#!/usr/bin/env python3
"""
repair_set.py — replace picks in a published set that fail the current quality gate.

    python3 repair_set.py 495            # plan only: what would be dropped and added
    python3 repair_set.py 495 --apply    # rewrite the Notion page, tracker entry, CSV rows, audit

The set keeps its number, title and size. Picks are re-checked with live GitHub data through
engines/quality.py; failing ones are replaced from v17's GraphQL lane (engines/topup.py) under
the set's own engine rules (AI ceiling, category cap). Dropped repos stay in usedRepoUrls so
they are never picked again. The page is rebuilt with the engine's own page format: new
blocks are appended first and the old ones deleted after, so a failure can't leave the page
empty. Backups of the tracker, CSV and audit go to _tmp/backup-repair-<time>/.
"""

import argparse
import csv
import importlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone

sys.dont_write_bytecode = True
import quality  # noqa: E402
import topup  # noqa: E402
import v17  # noqa: E402

v1 = v17.v1


def load_set(n):
    tracker = json.loads(v1.TRACKER.read_text())
    entries = [p for p in tracker["completedPages"] if p.get("setNum") == n]
    if len(entries) != 1:
        raise SystemExit(f"set {n}: expected one tracker entry, found {len(entries)}")
    entry = entries[0]
    engine = entry.get("version")
    if engine not in ("v11", "v12", "v13", "v14", "v15", "v16", "v17"):
        raise SystemExit(f"set {n}: engine {engine!r} has no page builder here")
    audit_path = v1.AUDIT_DIR / f"set{n}_{engine}_verified_audit.json"
    audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
    with open(v1.MASTER_CSV, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["set"] == str(n)]
    return tracker, entry, engine, audit_path, audit, rows


def build_candidates(engine, audit, rows, now):
    """Published rows + live GitHub data -> candidates in the engine's own shape."""
    live = v17.hydrate([r["repo_name"] for r in rows])
    by_url = {}
    for a in audit.get("selected") or audit.get("picks") or []:
        url = (a.get("url") or f"https://github.com/{a.get('repo', '')}").lower()
        by_url[url] = a
    cands, missing = [], []
    for row in rows:
        r = live.get(row["repo_name"].lower())
        if not r:
            missing.append(row["repo_name"])
            continue
        a = by_url.get(row["repo_url"].lower(), {})
        docker = bool(r["compose"] or r["dockerfile"])
        c = {"repo": r, "score": a.get("score", 0), "breakdown": a.get("breakdown") or {},
             "category": row["category"], "hcat": row["category"], "docker": a.get("docker", docker),
             "compose": r["compose"], "dockerfile": r["dockerfile"], "awesome": a.get("awesome", False),
             "proof_tier": a.get("proof_tier"), "fresh_gem": a.get("fresh_gem", False),
             "family": a.get("family"), "_published_url": row["repo_url"]}
        if a.get("hook"):
            c["enrich"] = {"interest_hook": a["hook"]}
        if a.get("topup") or a.get("hook") == topup.HOOK:   # v17-sourced: v17's categories apply
            c.update(category=v17.categorize(r), hcat=v17.categorize(r), topup="v17", _v17=r)
            r["_score"], r["_bd"], r["_cat"] = c["score"], c["breakdown"], c["category"]
        if engine == "v17":
            r["_score"], r["_bd"] = v17.interest_score(r, now)
            r["_cat"] = v17.categorize(r)
            c.update(score=r["_score"], category=r["_cat"], hcat=r["_cat"], _v17=r)
        cands.append(c)
    return cands, missing


def engine_rules(engine, mod):
    if engine == "v17":
        return dict(ai_ceiling=v17.AI_FILL_CAP, is_ai_cat=lambda c: v17.is_ai(c["_v17"]))
    if engine in ("v11",):
        return dict(ai_ceiling=None, is_ai_cat=lambda c: c.get("hcat") == "AI / LLM")
    if engine == "v12":
        return dict(ai_ceiling=mod.HARD_CAT_CAPS.get("AI / LLM", 10),
                    is_ai_cat=lambda c: c.get("hcat") == "AI / LLM",
                    cat_cap=getattr(mod, "DEFAULT_CAT_CAP", None))
    return dict(ai_ceiling=mod.HARD_CAT_CAPS.get("AI / LLM", 3),
                is_ai_cat=lambda c: mod.family_of(c.get("hcat") or "") == "ai",
                family_of=mod.family_of, cat_cap=getattr(mod, "DEFAULT_CAT_CAP", None))


def page_for(engine, mod, n, final, audit):
    if engine == "v17":
        picked = sorted((c["_v17"] for c in final), key=lambda r: -r["_score"])
        for r in picked:
            r.setdefault("_cat", v17.categorize(r))
        return v17.page_blocks(n, picked, audit.get("stats") or {"unused": "?", "seen": "?"}), picked
    import inspect
    kw = {"special_page": False} if "special_page" in inspect.signature(mod.page_blocks).parameters else {}
    return mod.page_blocks(n, final, **kw), final


def audit_rows(engine, final):
    if engine == "v17":
        return "picks", [{"repo": r["full_name"], "url": r["html_url"], "score": r["_score"],
                          "stars": r["stargazers_count"], "category": r["_cat"],
                          "description": r["description"], "language": r.get("language"),
                          "compose": r["compose"], "docker": r["compose"] or r["dockerfile"],
                          "ai": v17.is_ai(r), "breakdown": r["_bd"]} for r in final]
    return "selected", [{"url": c["repo"]["html_url"], "score": c["score"], "category": c["category"],
                         "stars": c["repo"].get("stargazers_count", 0), "docker": c["docker"],
                         "compose": c.get("compose"), "dockerfile": c.get("dockerfile"),
                         "awesome": c.get("awesome", False), "proof_tier": c.get("proof_tier"),
                         "family": c.get("family"), "fresh_gem": c.get("fresh_gem", False),
                         "hook": (c.get("enrich") or {}).get("interest_hook", ""), "topup": c.get("topup"),
                         "breakdown": c.get("breakdown") or {}} for c in final]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("set", type=int)
    ap.add_argument("--apply", action="store_true", help="Write Notion, tracker, CSV and audit.")
    args = ap.parse_args()
    if not v17.v16.GH_TOKEN:
        raise SystemExit("no GitHub token (gh auth login or GH_TOKEN)")
    now = datetime.now(timezone.utc)
    n = args.set
    tracker, entry, engine, audit_path, audit, rows = load_set(n)
    size = entry.get("count") or len(rows)
    mod = v17 if engine == "v17" else importlib.import_module(engine)
    print(f"[repair] Set {n} ({engine}, {len(rows)} published rows) — checking with live GitHub data …")

    cands, missing = build_candidates(engine, audit, rows, now)
    published_cat = {row["repo_url"].lower(): row["category"] for row in rows}
    recat = [(c["repo"]["full_name"], published_cat.get(c["_published_url"].lower()), c["category"])
             for c in cands if published_cat.get(c["_published_url"].lower()) != c["category"]]
    for name, old, new in recat:
        print(f"[repair] category: {name}: {old} → {new}")
    kept, record = quality.screen("repair", cands, ignore_set=n, tracker=tracker)
    dropped = (record or {}).get("dropped", []) + [{"repo": m, "why": "unavailable-on-github"} for m in missing]
    if missing:
        print(f"[repair] {len(missing)} published repos are gone from GitHub: {', '.join(missing)}")
    if not dropped and not recat:
        print(f"[repair] Set {n} passes the current gate; nothing to do.")
        return

    final, top = topup.fill("repair", kept, size, tracker, dry_run=not args.apply, **engine_rules(engine, mod))
    if len(final) != size:
        raise SystemExit(f"[repair] could only reach {len(final)}/{size}; not touching Set {n}.")
    added = [c["repo"]["full_name"] for c in final if c.get("topup") and c in final[len(kept):]]
    blocks, final_rows = page_for(engine, mod, n, final, audit)
    print(f"[repair] plan: drop {len(dropped)}, add {len(added)}; page {len(blocks)} blocks")
    if not args.apply:
        print("[repair] plan only — run with --apply to write Notion, tracker, CSV and audit.")
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = v1.AUDIT_DIR / f"backup-repair-{stamp}"
    backup.mkdir()
    for path in (v1.TRACKER, v1.MASTER_CSV) + ((audit_path,) if audit_path.exists() else ()):
        shutil.copy2(path, backup / path.name)
    print(f"[repair] backups -> {backup}")

    v1.NOTION_KEY = v1.load_notion_key()
    old = v1.fetch_blocks(entry["pageId"])
    v1.append_blocks(entry["pageId"], blocks)          # new content first …
    for b in old:                                      # … then remove the old blocks
        v1.notion_request("DELETE", f"blocks/{b['id']}")
    print(f"[repair] Notion page rewritten ({len(old)} old blocks removed, {len(blocks)} added)")

    urls = [c["repo"]["html_url"] for c in final] if engine != "v17" else [r["html_url"] for r in final_rows]
    entry["repos"] = urls
    entry["count"] = len(urls)
    entry["repaired"] = now.strftime("%Y-%m-%d")
    seen = {u.lower() for u in tracker["usedRepoUrls"]}
    tracker["usedRepoUrls"] += [u for u in urls if u.lower() not in seen]
    v1.write_tracker(tracker)

    with open(v1.MASTER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        others = [row for row in reader if row and row[0] != str(n)]
    new_rows = []
    for item in final_rows:
        r = item if engine == "v17" else item["repo"]
        cat = item["_cat"] if engine == "v17" else item["category"]
        desc = (r.get("description") or "").replace("\n", " ").replace("\r", " ").strip()
        new_rows.append([n, r["full_name"], cat, r["html_url"], desc])
    tmp = v1.MASTER_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(others)
        w.writerows(new_rows)
    tmp.replace(v1.MASTER_CSV)

    key, items = audit_rows(engine, final_rows)
    audit[key] = items
    dist = {}
    for row in new_rows:
        dist[row[2]] = dist.get(row[2], 0) + 1
    audit["categoryDistribution"] = dict(sorted(dist.items(), key=lambda kv: -kv[1]))
    audit["repair"] = {"at": now.isoformat(timespec="seconds"), "dropped": dropped, "added": added,
                       "recategorized": [{"repo": a, "from": b, "to": c} for a, b, c in recat],
                       "topUp": top}
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(f"[repair] Set {n}: tracker, master CSV ({len(new_rows)} rows) and audit updated ✅")


if __name__ == "__main__":
    main()
