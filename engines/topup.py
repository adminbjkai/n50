"""
topup.py — fill a short set from v17's GraphQL discovery lane (shared by v11–v16).

The REST-search engines (v11–v16) are mined out after ~25k published repos: a run often
confirms 45–48 candidates for 50 slots, and every standard publish refuses a short set.
v17's GraphQL search, sliced by creation date, still surfaces 150+ unused repos that pass a
strict quality gate. When an engine's own selection comes up short, it calls `fill()` just
before its shortfall check, and the missing slots are filled from that lane.

Rules for a top-up pick:
  * passes v17's gate (deploy proof or awesome-selfhosted, real stars, recent pushes, junk gate)
  * not already published (v17 checks URLs, renames and re-uploads) and not already selected
  * one repo per owner across the whole set
  * AI repos only while the engine stays within its own AI ceiling
  * proof tier A when it ships compose/Dockerfile, B when it's on awesome-selfhosted

Top-up picks keep v17's interest score (a different scale from the engines' appeal score);
each is marked in the audit (`topUp`) and in its Notion line.
"""

import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

HOOK = "added by the v17 top-up lane"


def _owner(full_name):
    return (full_name or "").split("/")[0].lower()


def _to_candidate(r, family_of=None):
    """v17 repo dict -> the candidate shape v11–v16 use after selection."""
    docker = bool(r["compose"] or r["dockerfile"])
    repo = {k: r.get(k) for k in ("full_name", "html_url", "description", "topics", "language",
                                  "stargazers_count", "forks_count", "created_at", "pushed_at",
                                  "homepage", "license")}
    c = {
        "repo": repo, "score": r["_score"], "hcat": r["_cat"], "category": r["_cat"],
        "docker": docker, "compose": bool(r["compose"]), "dockerfile": bool(r["dockerfile"]),
        "awesome": False, "proof_tier": "A" if docker else "B", "fresh_gem": False,
        "breakdown": {f"v17 {k}": v for k, v in (r.get("_bd") or {}).items()},
        "enrich": {"interest_hook": HOOK}, "topup": "v17",
    }
    if family_of:
        c["family"] = family_of(r["_cat"])
    return c


def fill(version, selected, target, tracker, dry_run, ai_ceiling=None, is_ai_cat=None,
         family_of=None, cat_cap=None):
    """Return `selected` topped up to `target` (or as close as the lane allows) plus a record.

    `is_ai_cat(candidate) -> bool` and `ai_ceiling` let the caller keep its own AI cap;
    `cat_cap` is honoured first and relaxed only if the set can't be filled within it."""
    need = target - len(selected)
    if need <= 0:
        return selected, None
    t0 = time.time()
    print(f"[{version}] top-up: {len(selected)}/{target} after selection — filling {need} "
          f"from the v17 GraphQL lane …", flush=True)
    try:
        import v17  # lazy: only short runs pay for the import and the discovery
    except Exception as exc:  # noqa: BLE001
        print(f"[{version}] top-up unavailable ({exc}); leaving the set at {len(selected)}")
        return selected, {"from": "v17", "added": 0, "error": str(exc)}

    taken_urls = {c["repo"]["html_url"].lower().rstrip("/") for c in selected}
    owners = {_owner(c["repo"]["full_name"]) for c in selected}
    view = dict(tracker)
    view["usedRepoUrls"] = list(tracker.get("usedRepoUrls", [])) + sorted(taken_urls)
    args = SimpleNamespace(quick=False, dry_run=dry_run, save_caches=False)
    try:
        picks, stats = v17.discover(view, need + 25, datetime.now(timezone.utc), args)
    except Exception as exc:  # noqa: BLE001 — never turn a short set into a crash
        print(f"[{version}] top-up failed ({type(exc).__name__}: {exc}); leaving the set at {len(selected)}")
        return selected, {"from": "v17", "added": 0, "error": str(exc)}

    ai_now = sum(1 for c in selected if is_ai_cat and is_ai_cat(c))
    cats = {}
    for c in selected:
        k = c.get("category") or c.get("hcat")
        cats[k] = cats.get(k, 0) + 1
    added = []
    for capped in ((True, False) if cat_cap else (False,)):
        for r in picks:
            if len(added) >= need:
                break
            if r["html_url"].lower() in taken_urls or _owner(r["full_name"]) in owners:
                continue
            if capped and cats.get(r["_cat"], 0) >= cat_cap:
                continue
            c = _to_candidate(r, family_of)
            if ai_ceiling is not None and (v17.is_ai(r) or (is_ai_cat and is_ai_cat(c))):
                if ai_now >= ai_ceiling:
                    continue
                ai_now += 1
            added.append(c)
            cats[r["_cat"]] = cats.get(r["_cat"], 0) + 1
            owners.add(_owner(r["full_name"]))
            taken_urls.add(r["html_url"].lower())
    for c in added:
        print(f"[{version}]   + {c['repo']['full_name']:<42} [{c['category'][:24]}] "
              f"★{c['repo']['stargazers_count']} tier {c['proof_tier']} · "
              f"{(c['repo']['description'] or '')[:60]}")
    record = {"from": "v17", "needed": need, "added": len(added),
              "repos": [c["repo"]["full_name"] for c in added],
              "laneSeen": stats.get("seen"), "lanePassing": stats.get("passing"),
              "seconds": round(time.time() - t0, 1)}
    print(f"[{version}] top-up: added {len(added)} of {need} in {record['seconds']}s "
          f"→ {len(selected) + len(added)}/{target}", flush=True)
    sys.stdout.flush()
    return selected + added, record


def self_test():
    """Offline check of the fill rules with a stubbed v17 lane."""
    import types
    fake = types.ModuleType("v17")

    def mk(name, desc="Self-hosted recipe manager with a web UI", cat="Health / Food / Fitness", score=40):
        return {"full_name": name, "html_url": f"https://github.com/{name}", "description": desc,
                "topics": [], "language": "Go", "stargazers_count": 50, "forks_count": 1,
                "created_at": "", "pushed_at": "2026-09-01", "homepage": "", "license": {},
                "compose": True, "dockerfile": False, "_score": score, "_cat": cat, "_bd": {"stars": 17}}
    lane = [mk("taken/dup"), mk("own/other"), mk("ai1/x", "Self-hosted AI agent workspace web UI", "AI / LLM"),
            mk("ai2/y", "Self-hosted AI chat web UI", "AI / LLM")] + [mk(f"n{i}/app") for i in range(10)]
    fake.discover = lambda tracker, target, now, args: (lane[:target], {"seen": 99, "passing": len(lane)})
    fake.is_ai = lambda r: r["_cat"] == "AI / LLM"
    sys.modules["v17"] = fake
    sel = [{"repo": {"full_name": "taken/dup", "html_url": "https://github.com/taken/dup"}, "hcat": "Media / Streaming"},
           {"repo": {"full_name": "own/first", "html_url": "https://github.com/own/first"}, "hcat": "AI / LLM"}]
    out, rec = fill("vX", sel, 7, {"usedRepoUrls": []}, True, ai_ceiling=2,
                    is_ai_cat=lambda c: c.get("hcat") == "AI / LLM")
    names = [c["repo"]["full_name"] for c in out]
    checks = [
        ("fills to target", len(out) == 7 and rec["added"] == 5),
        ("skips already-selected repo", names.count("taken/dup") == 1),
        ("one repo per owner", "own/other" not in names),
        ("AI ceiling respected", sum(1 for n in names if n.startswith("ai")) == 1),
        ("tier A for compose", all(c["proof_tier"] == "A" for c in out[2:])),
    ]
    fake.discover = lambda *a: (_ for _ in ()).throw(RuntimeError("lane down"))
    out2, rec2 = fill("vX", sel, 7, {"usedRepoUrls": []}, True)
    checks.append(("lane failure keeps the set, no crash", out2 == sel and rec2["added"] == 0))
    fake.discover = lambda tracker, target, now, args: (lane[:target], {"seen": 99, "passing": len(lane)})
    lane[4:4] = [mk("m1/a", cat="Media / Streaming", score=90), mk("m2/b", cat="Media / Streaming", score=89)]
    media = [{"repo": {"full_name": f"s{i}/m", "html_url": f"https://github.com/s{i}/m"},
              "hcat": "Media / Streaming", "category": "Media / Streaming"} for i in range(3)]
    out3, _ = fill("vX", media, 6, {"usedRepoUrls": []}, True, cat_cap=3)
    checks.append(("category cap honoured while the set can still fill",
                   not any(c["repo"]["full_name"].startswith("m") for c in out3[3:])))
    ok = True
    for name, good in checks:
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'} {name}")
    print("topup self-test:", "ALL PASSED" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    print(__doc__)
