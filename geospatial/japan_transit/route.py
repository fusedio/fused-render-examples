def main(mode: str = "route", from_g: str = "", to_g: str = ""):
    import json
    import os
    from heapq import heappush, heappop
    from collections import defaultdict

    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "data", "stations.json"), encoding="utf-8") as f:
        stations = json.load(f)
    with open(os.path.join(base, "data", "graph.json"), encoding="utf-8") as f:
        graph = json.load(f)

    adj = defaultdict(list)
    for a, b, mins, km, geom in graph["rides"]:
        adj[a].append((b, mins, km, "ride", geom))
        adj[b].append((a, mins, km, "ride", geom[::-1]))
    for a, b, mins in graph["transfers"]:
        adj[a].append((b, mins, 0.0, "transfer", None))
        adj[b].append((a, mins, 0.0, "transfer", None))

    by_group = defaultdict(list)
    for s in stations:
        by_group[s["g"]].append(s["i"])

    from_groups = [g for g in from_g.split(",") if g in by_group]
    if not from_groups:
        raise ValueError(f"unknown origin station group {from_g!r}")
    sources = [i for g in from_groups for i in by_group[g]]

    XFER_FRICTION = 3.0   # routing-only nudge toward one-seat rides

    def dijkstra(penalty=None):
        """Routing cost inflates penalized lines and adds transfer friction;
        recorded weights stay real so reported times are honest."""
        dist, prev = {}, {}
        pq = []
        for i in sources:
            dist[i] = 0.0
            prev[i] = None
            heappush(pq, (0.0, i))
        while pq:
            d, v = heappop(pq)
            if d > dist.get(v, 1e18):
                continue
            for u, w, km, kind, geom in adj[v]:
                if kind == "transfer":
                    cost = w + XFER_FRICTION
                else:
                    cost = w
                    if penalty:
                        f = penalty.get(stations[u]["ln"])
                        if f:
                            cost = w * f
                nd = d + cost
                if nd < dist.get(u, 1e18):
                    dist[u] = nd
                    prev[u] = (v, w, km, kind, geom)
                    heappush(pq, (nd, u))
        return dist, prev

    dist, prev = dijkstra()

    if mode == "iso":
        best = {}
        for s in stations:
            d = dist.get(s["i"])
            if d is not None and d < best.get(s["g"], (1e18,))[0]:
                best[s["g"]] = (d, s["x"], s["y"], s["r"], s["n"])
        return {"iso": [
            {"g": g, "m": round(d, 1), "x": x, "y": y, "r": r, "n": n}
            for g, (d, x, y, r, n) in best.items()
        ]}

    to_groups = [g for g in to_g.split(",") if g in by_group]
    if not to_groups:
        raise ValueError(f"unknown destination station group {to_g!r}")
    targets = [i for g in to_groups for i in by_group[g]]

    def extract(prev, dist):
        end, end_d = None, 1e18
        for i in targets:
            if i in prev and dist.get(i, 1e18) < end_d:
                end, end_d = i, dist[i]
        if end is None:
            return None
        chain = []
        v = end
        while v is not None:
            p = prev[v]
            chain.append((v, p))
            v = p[0] if p else None
        chain.reverse()

        legs, total = [], 0.0
        for v, p in chain:
            s = stations[v]
            if p is None:
                legs.append({"type": "start", "station": s})
                continue
            _, w, km, kind, geom = p
            total += w
            if kind == "transfer":
                legs.append({"type": "transfer", "mins": w, "station": s})
            else:
                last = legs[-1] if legs else None
                if last and last["type"] == "ride" and last["line"] == s["ln"]:
                    last["mins"] += w
                    last["km"] += km
                    last["stops"].append({"n": s["n"], "r": s["r"], "x": s["x"], "y": s["y"]})
                    last["path"].extend(geom[1:] if geom else [])
                else:
                    legs.append({
                        "type": "ride", "line": s["ln"], "lineR": s["lr"],
                        "operator": s["op"], "cat": s["c"], "mins": w, "km": km,
                        "stops": [{"n": s["n"], "r": s["r"], "x": s["x"], "y": s["y"]}],
                        "path": list(geom) if geom else [],
                    })
        return {"legs": legs, "mins": total}

    def tidy(legs):
        """Collapse duplicate transfers inside one station complex and merge
        same-line rides split by an operator handover (through-running trains
        like the Hokuriku Shinkansen — passengers stay seated)."""
        out = []
        for leg in legs:
            if leg["type"] == "transfer" and out and out[-1]["type"] == "transfer":
                continue
            if (leg["type"] == "ride" and len(out) >= 2
                    and out[-1]["type"] == "transfer"
                    and out[-2]["type"] == "ride" and out[-2]["line"] == leg["line"]):
                out.pop()
                prev = out[-1]
                prev["mins"] += leg["mins"]
                prev["km"] += leg["km"]
                prev["stops"].extend(leg["stops"])
                prev["path"].extend(leg["path"])
                continue
            out.append(leg)
        return out

    def pack(res):
        res["legs"] = tidy(res["legs"])
        res["mins"] = sum(l.get("mins", 0) for l in res["legs"])
        out, km = [], 0.0
        for leg in res["legs"]:
            if leg["type"] == "start":
                s = leg["station"]
                out.append({"type": "start", "n": s["n"], "r": s["r"], "x": s["x"], "y": s["y"]})
            elif leg["type"] == "transfer":
                s = leg["station"]
                out.append({"type": "transfer", "mins": round(leg["mins"]),
                            "n": s["n"], "r": s["r"], "x": s["x"], "y": s["y"]})
            else:
                km += leg["km"]
                out.append({
                    "type": "ride", "line": leg["line"], "lineR": leg["lineR"],
                    "operator": leg["operator"], "cat": leg["cat"],
                    "mins": round(leg["mins"], 1), "km": round(leg["km"], 1),
                    "nStops": len(leg["stops"]), "stops": leg["stops"],
                    "path": leg["path"],
                })
        return {"totalMins": round(res["mins"], 1), "totalKm": round(km, 1), "legs": out}

    def signature(res):
        return tuple(l["line"] for l in res["legs"] if l["type"] == "ride")

    best = extract(prev, dist)
    if best is None:
        return {"ok": False, "reason": "no rail path found"}

    # alternatives: rerun with each used line penalized, plus a no-Shinkansen run
    candidates = [best]
    used_lines = [(l["mins"], l["line"], l["cat"]) for l in best["legs"] if l["type"] == "ride"]
    used_lines.sort(reverse=True)
    penalties = [{ln: 8.0} for _, ln, _ in used_lines[:3]]
    if any(c == 0 for _, _, c in used_lines):
        shink = {s["ln"] for s in stations if s["c"] == 0}
        penalties.append({ln: 50.0 for ln in shink})
    for pen in penalties:
        d2, p2 = dijkstra(pen)
        alt = extract(p2, d2)
        if alt:
            candidates.append(alt)

    seen, routes = set(), []
    for c in sorted(candidates, key=lambda r: r["mins"]):
        sig = signature(c)
        if sig in seen or c["mins"] > best["mins"] * 1.6:
            continue
        seen.add(sig)
        routes.append(c)
        if len(routes) == 3:
            break

    packed = [pack(r) for r in routes]
    base_lines = {l["line"] for l in packed[0]["legs"] if l["type"] == "ride"}
    base_xfers = sum(1 for l in packed[0]["legs"] if l["type"] == "transfer")
    for i, p in enumerate(packed):
        if i == 0:
            p["tag"] = "Fastest"
            continue
        via = next((l for l in p["legs"] if l["type"] == "ride" and l["line"] not in base_lines), None)
        xfers = sum(1 for l in p["legs"] if l["type"] == "transfer")
        if via:
            p["tag"] = f"Via {via['line']}"
            p["tagR"] = f"Via {via['lineR']}"
        elif xfers < base_xfers:
            p["tag"] = "Fewer transfers"
        else:
            p["tag"] = "Alternative"
    return {"ok": True, "routes": packed}
