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
# Only fills this large read or write the star-ladder memory: it tells a full-set run where the
# pool ran dry, and a one-slot repair shouldn't skip the high-star rungs (or reset them for others).
LADDER_MEMORY_MIN_NEED = 10


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
        "_v17": r,  # original v17 record, for v17-format pages
    }
    if family_of:
        c["family"] = family_of(r["_cat"])
    return c


def _is_ai(c, v17, is_ai_cat):
    """One AI test for picks already in the set and lane picks alike: the engine's own AI
    category, or AI words in the description / v17's AI category (v17.is_ai)."""
    if is_ai_cat and is_ai_cat(c):
        return True
    r = c.get("_v17") or {"description": c["repo"].get("description") or "",
                          "_cat": c.get("category") or c.get("hcat") or ""}
    return v17.is_ai(r)


def trim_ai(version, selected, ai_ceiling, is_ai_cat, v17):
    """Drop the last AI picks past the ceiling (selection order = the engine's priority)."""
    if ai_ceiling is None:
        return selected, []
    ai_idx = [i for i, c in enumerate(selected) if _is_ai(c, v17, is_ai_cat)]
    cut = set(ai_idx[ai_ceiling:])
    if cut:
        print(f"[{version}] top-up: {len(ai_idx)} AI picks exceed the ceiling of {ai_ceiling}; "
              f"replacing {len(cut)}: " + ", ".join(selected[i]["repo"]["full_name"] for i in sorted(cut)))
    return [c for i, c in enumerate(selected) if i not in cut], [selected[i]["repo"]["full_name"] for i in sorted(cut)]


def trim_cap(version, selected, cat_cap):
    """Drop the lowest-scored lane picks in any category above `cat_cap` (repairs of sets the
    lane filled before the cap was held on every rung). The engine's own picks are kept."""
    if not cat_cap:
        return selected, []
    counts = {}
    for c in selected:
        counts[c.get("category")] = counts.get(c.get("category"), 0) + 1
    cut = set()
    for cat, k in counts.items():
        lane = sorted((c for c in selected if c.get("category") == cat and c.get("topup")),
                      key=lambda c: c.get("score", 0))
        cut.update(id(c) for c in lane[:max(0, k - cat_cap)])
    if cut:
        print(f"[{version}] category cap {cat_cap}: replacing {len(cut)} lane picks: "
              + ", ".join(f"{c['repo']['full_name']} [{c['category']}]" for c in selected if id(c) in cut))
    return ([c for c in selected if id(c) not in cut],
            [c["repo"]["full_name"] for c in selected if id(c) in cut])


def fill(version, selected, target, tracker, dry_run, ai_ceiling=None, is_ai_cat=None,
         family_of=None, cat_cap=None):
    """Return `selected` topped up to `target` (or as close as the lane allows) plus a record.

    `is_ai_cat(candidate) -> bool` and `ai_ceiling` keep the caller's AI cap (picks over it are
    replaced); `cat_cap` is honoured on every star rung and relaxed only if the whole ladder
    can't fill the set within it."""
    try:
        import v17  # lazy: only short runs pay for the import and the discovery
    except Exception as exc:  # noqa: BLE001
        print(f"[{version}] top-up unavailable ({exc}); leaving the set at {len(selected)}")
        return selected, {"from": "v17", "added": 0, "error": str(exc)}
    selected, over_ai = trim_ai(version, selected, ai_ceiling, is_ai_cat, v17)
    need = target - len(selected)
    if need <= 0:
        return selected, None
    t0 = time.time()
    print(f"[{version}] top-up: {len(selected)}/{target} after selection — filling {need} "
          f"from the v17 GraphQL lane …", flush=True)

    taken_urls = {c["repo"]["html_url"].lower().rstrip("/") for c in selected}
    owners = {_owner(c["repo"]["full_name"]) for c in selected}
    view = dict(tracker)
    view["usedRepoUrls"] = list(tracker.get("usedRepoUrls", [])) + sorted(taken_urls)
    args = SimpleNamespace(quick=False, dry_run=dry_run, save_caches=False)
    ai_now = sum(1 for c in selected if _is_ai(c, v17, is_ai_cat))
    cats = {}
    for c in selected:
        k = c.get("category") or c.get("hcat")
        cats[k] = cats.get(k, 0) + 1
    added, stats, floors, pool, lane_failed = [], {}, [], [], False
    ladder = getattr(v17, "STAR_LADDER", None) or [(getattr(v17, "MIN_STARS", None),
                                                      getattr(v17, "MIN_STARS_YOUNG", None))]
    memory = need >= LADDER_MEMORY_MIN_NEED and hasattr(v17, "ladder_record")
    start = v17.ladder_start() if memory else 0
    try:
        for rung, (floor, young) in enumerate(ladder):
            if rung < start:
                continue
            if len(added) >= need:
                break
            if hasattr(v17, "set_star_floor"):
                v17.set_star_floor(floor, young)
            floors.append(floor)
            try:
                picks, stats = v17.discover(view, need + 25, datetime.now(timezone.utc), args)
            except Exception as exc:  # noqa: BLE001 — never turn a short set into a crash
                print(f"[{version}] top-up lane failed ({type(exc).__name__}: {exc}); "
                      f"keeping {len(selected) + len(added)} picks")
                stats = {"error": str(exc)}
                lane_failed = True
                break
            picks = _drop_renamed(v17, picks, tracker, version)
            pool += picks
            ai_now = _take(picks, need, added, taken_urls, owners, cats, cat_cap, ai_ceiling, ai_now,
                           is_ai_cat, family_of, v17, capped=bool(cat_cap))
            if memory and (len(added) >= need or (floor, young) == ladder[-1]):
                v17.ladder_record(rung)
            if len(added) < need and (floor, young) != ladder[-1]:
                print(f"[{version}] top-up: {len(added)}/{need} at ≥{floor}★; trying a lower star floor …",
                      flush=True)
        if cat_cap and len(added) < need and pool and not lane_failed:   # only after the whole ladder
            print(f"[{version}] top-up: {len(added)}/{need} within the category cap of {cat_cap} on every "
                  f"rung; relaxing it for the rest", flush=True)
            ai_now = _take(pool, need, added, taken_urls, owners, cats, cat_cap, ai_ceiling, ai_now,
                           is_ai_cat, family_of, v17, capped=False)
    finally:
        if hasattr(v17, "set_star_floor"):
            v17.set_star_floor(*ladder[0])
    for c in added:
        print(f"[{version}]   + {c['repo']['full_name']:<42} [{c['category'][:24]}] "
              f"★{c['repo']['stargazers_count']} tier {c['proof_tier']} · "
              f"{(c['repo']['description'] or '')[:60]}")
    record = {"from": "v17", "needed": need, "added": len(added), "starFloors": floors,
              "replacedOverAiCeiling": over_ai,
              "repos": [c["repo"]["full_name"] for c in added],
              "laneSeen": stats.get("seen"), "lanePassing": stats.get("passing"),
              "seconds": round(time.time() - t0, 1)}
    if stats.get("error"):
        record["error"] = stats["error"]
    print(f"[{version}] top-up: added {len(added)} of {need} in {record['seconds']}s "
          f"→ {len(selected) + len(added)}/{target}", flush=True)
    sys.stdout.flush()
    return selected + added, record


def _drop_renamed(v17, picks, tracker, version):
    """Second rename/transfer check on lane picks (v17 resolves a capped number per run)."""
    try:
        import quality
        dups = quality._renamed_copies(v17, [{"repo": {"full_name": r["full_name"]}} for r in picks],
                                       tracker, None)
    except Exception:  # noqa: BLE001 — the lane's own dedupe still applied
        return picks
    if dups:
        print(f"[{version}] top-up: skipped {len(dups)} renamed copies of published repos: "
              + ", ".join(sorted(dups)))
    return [r for r in picks if r["full_name"].lower() not in dups]


def _take(picks, need, added, taken_urls, owners, cats, cat_cap, ai_ceiling, ai_now, is_ai_cat,
          family_of, v17, capped):
    """Move acceptable lane picks into `added` (within the category cap when `capped`)."""
    for r in picks:
        if len(added) >= need:
            break
        if r["html_url"].lower() in taken_urls or _owner(r["full_name"]) in owners:
            continue
        if capped and cats.get(r["_cat"], 0) >= cat_cap:
            continue
        c = _to_candidate(r, family_of)
        if ai_ceiling is not None and _is_ai(c, v17, is_ai_cat):
            if ai_now >= ai_ceiling:
                continue
            ai_now += 1
        added.append(c)
        cats[r["_cat"]] = cats.get(r["_cat"], 0) + 1
        owners.add(_owner(r["full_name"]))
        taken_urls.add(r["html_url"].lower())
    return ai_now


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
    fake.is_ai = lambda r: r["_cat"] == "AI / LLM" or " AI " in f" {r['description']} "
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
    # ladder: rung 1 yields nothing usable, rung 2 fills
    rungs = []
    fake.STAR_LADDER = [(20, 10), (5, 3)]
    fake.MIN_STARS, fake.MIN_STARS_YOUNG = 20, 10
    fake.set_star_floor = lambda f, y: rungs.append(f)
    fake.discover = lambda tracker, target, now, args: (lane[:target] if rungs[-1] == 5 else [], {"seen": 1})
    out4, rec4 = fill("vX", sel, 5, {"usedRepoUrls": []}, True)
    checks.append(("star ladder retries a lower rung", len(out4) == 5 and rec4["starFloors"] == [20, 5]))
    fake.discover = lambda tracker, target, now, args: (lane[:target], {"seen": 99, "passing": len(lane)})
    lane[4:4] = [mk("m1/a", cat="Media / Streaming", score=90), mk("m2/b", cat="Media / Streaming", score=89)]
    media = [{"repo": {"full_name": f"s{i}/m", "html_url": f"https://github.com/s{i}/m"},
              "hcat": "Media / Streaming", "category": "Media / Streaming"} for i in range(3)]
    out3, _ = fill("vX", media, 6, {"usedRepoUrls": []}, True, cat_cap=3)
    checks.append(("category cap honoured while the set can still fill",
                   not any(c["repo"]["full_name"].startswith("m") for c in out3[3:])))
    # AI picks already over the ceiling are replaced (description counts, not only category)
    ai_sel = [{"repo": {"full_name": f"a{i}/x", "html_url": f"https://github.com/a{i}/x",
                        "description": "Self-hosted AI agent chat web UI"}, "hcat": "Web App / Other"}
              for i in range(3)]
    out5, rec5 = fill("vX", ai_sel, 3, {"usedRepoUrls": []}, True, ai_ceiling=1,
                      is_ai_cat=lambda c: c.get("hcat") == "AI / LLM")
    checks.append(("AI picks over the ceiling are replaced",
                   rec5["replacedOverAiCeiling"] == ["a1/x", "a2/x"] and len(out5) == 3
                   and sum(1 for c in out5 if "AI" in (c["repo"]["description"] or "")) == 1))
    # the category cap holds across rungs: a lower rung within the cap beats an over-cap pick
    rungs.clear()
    hi = [mk("h1/m", cat="Media / Streaming")]
    lo = [mk("l1/n", cat="Notes / Knowledge")]
    fake.discover = lambda tracker, target, now, args: (hi if rungs[-1] == 20 else hi + lo, {"seen": 1})
    out6, rec6 = fill("vX", media, 4, {"usedRepoUrls": []}, True, cat_cap=3)
    checks.append(("category cap holds across star rungs",
                   [c["repo"]["full_name"] for c in out6[3:]] == ["l1/n"] and rec6["starFloors"] == [20, 5]))
    # an error mid-ladder still resets the star floor; small fills ignore ladder memory
    rungs.clear()
    fake.ladder_start = lambda: 1
    fake.ladder_record = lambda rung: (_ for _ in ()).throw(OSError("disk full"))
    fake.discover = lambda tracker, target, now, args: (lane[:target], {"seen": 1})
    try:
        fill("vX", [], 12, {"usedRepoUrls": []}, True)
    except OSError:
        pass
    checks.append(("star floor reset after an error", rungs[-1] == 20))
    rungs.clear()
    fill("vX", sel, 4, {"usedRepoUrls": []}, True)
    checks.append(("small fill starts at the top rung", rungs[0] == 20))
    # a lane error on a lower rung must not relax the cap for picks seen on a higher rung
    rungs.clear()
    fake.ladder_start = lambda: 0
    fake.discover = lambda tracker, target, now, args: (
        [mk("h2/m", cat="Media / Streaming")] if rungs[-1] == 20 else (_ for _ in ()).throw(RuntimeError("down")),
        {"seen": 1})
    out8, rec8 = fill("vX", media, 4, {"usedRepoUrls": []}, True, cat_cap=3)
    checks.append(("lane error mid-ladder keeps the cap", len(out8) == 3 and rec8.get("error") == "down"))
    capped = [{"repo": {"full_name": f"c{i}/m"}, "category": "Monitoring", "score": i, "topup": "v17" if i else None}
              for i in range(5)]
    kept7, cut7 = trim_cap("vX", capped, 2)
    checks.append(("cap trim drops the lowest-scored lane picks only", cut7 == ["c1/m", "c2/m", "c3/m"]
                   and [c["repo"]["full_name"] for c in kept7] == ["c0/m", "c4/m"]))
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
