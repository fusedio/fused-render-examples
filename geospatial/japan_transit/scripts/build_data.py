"""Build japan_transit static data files.

Inputs (scratchpad downloads):
  JP/JP.txt                          GeoNames postal codes for Japan
  N02/UTF-8/N02-23_Station.geojson   MLIT N02-23 stations
  N02/UTF-8/N02-23_RailroadSection.geojson  MLIT N02-23 rail sections

Outputs (../data/):
  postal.bin        Float32Array [lon,lat] * N
  postal_pref.bin   Uint8Array prefecture index per point
  postal_meta.json  {count, prefs: [names]}
  rail.json         slim FeatureCollection {n,r,o,c} c=0 shinkansen,1 jr,2 metro,3 private,4 tram/mono
  stations.json     [{i,g,n,r,ln,lr,op,c,x,y}]
  graph.json        {rides:[[a,b,mins,km]], transfers:[[a,b,mins]]}

Run:  ../../../.venv/Scripts/python.exe build_data.py <scratchpad_dir>
"""

import json
import math
import struct
import sys
import unicodedata
from collections import defaultdict
from heapq import heappush, heappop
from pathlib import Path

import pykakasi

SRC = Path(sys.argv[1])
OUT = Path(__file__).resolve().parent.parent / "data"
OUT.mkdir(exist_ok=True)

kks = pykakasi.kakasi()


def romaji(text):
    parts = [item["hepburn"] for item in kks.convert(text)]
    s = " ".join(p for p in parts if p.strip())
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(w.capitalize() for w in s.split())


def haversine_km(x1, y1, x2, y2):
    r = 6371.0
    p1, p2 = math.radians(y1), math.radians(y2)
    dp, dl = p2 - p1, math.radians(x2 - x1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- postal
def build_postal():
    prefs, pref_idx = [], {}
    coords, pidx, names, codes = [], [], [], []
    seen = set()
    with open(SRC / "JP" / "JP.txt", encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 11 or not c[9] or not c[10]:
                continue
            lat, lon = float(c[9]), float(c[10])
            key = (round(lon, 4), round(lat, 4))
            if key in seen:
                continue
            seen.add(key)
            pref = c[3] or "?"
            if pref not in pref_idx:
                pref_idx[pref] = len(prefs)
                prefs.append(pref)
            coords.append((lon, lat))
            pidx.append(pref_idx[pref])
            names.append(c[2])
            codes.append(c[1])
    with open(OUT / "postal.bin", "wb") as f:
        for lon, lat in coords:
            f.write(struct.pack("<ff", lon, lat))
    with open(OUT / "postal_pref.bin", "wb") as f:
        f.write(bytes(pidx))
    with open(OUT / "postal_meta.json", "w", encoding="utf-8") as f:
        json.dump({"count": len(coords), "prefs": prefs}, f, ensure_ascii=False)
    with open(OUT / "postal_names.json", "w", encoding="utf-8") as f:
        json.dump({"n": names, "c": codes}, f, ensure_ascii=False, separators=(",", ":"))
    print("postal points:", len(coords), "prefs:", len(prefs))


# ---------------------------------------------------------------- categories
TRAM_TYPES = {"13", "14", "16", "17", "21", "22", "25"}  # cable/suspended/AGT/trolley/tram
MONO_TYPES = {"23", "24"}


def category(props):
    t, k = props.get("N02_001", ""), props.get("N02_002", "")
    name, op = props.get("N02_003", ""), props.get("N02_004", "")
    if k == "1":
        return 0
    if t in TRAM_TYPES or t in MONO_TYPES:
        return 4
    if "地下鉄" in op or "メトロ" in op or "地下鉄" in name:
        return 2
    if k == "3":
        return 2
    if k == "2":
        return 1
    return 3


# ---------------------------------------------------------------- rail
def build_rail():
    """Emit the rail network as a GPU-ready columnar binary (rail.bin) plus
    a small meta JSON — no per-load JSON parsing or feature iteration.

    rail.bin, one block per category 0..4:
      Float32 positions   [x,y] * verts
      Uint32  startIndices paths+1 (zero-based within the category)
      Uint16  name index   per path (padded to 4 bytes)
    """
    d = json.load(open(SRC / "N02/UTF-8/N02-23_RailroadSection.geojson", encoding="utf-8"))
    by_cat = [[] for _ in range(5)]
    names, name_idx = [], {}
    rom_cache = {}
    for f in d["features"]:
        p = f["properties"]
        n = p.get("N02_003", "")
        if n not in name_idx:
            if n not in rom_cache:
                rom_cache[n] = romaji(n)
            name_idx[n] = len(names)
            names.append([n, rom_cache[n]])
        by_cat[category(p)].append((f["geometry"]["coordinates"], name_idx[n]))

    blob = bytearray()
    cats = []
    for feats in by_cat:
        pos_off = len(blob)
        starts, verts = [0], 0
        pos = bytearray()
        nidx = []
        for coords, ni in feats:
            for x, y in coords:
                pos += struct.pack("<ff", x, y)
            verts += len(coords)
            starts.append(verts)
            nidx.append(ni)
        blob += pos
        idx_off = len(blob)
        for s in starts:
            blob += struct.pack("<I", s)
        name_off = len(blob)
        for ni in nidx:
            blob += struct.pack("<H", ni)
        if len(blob) % 4:
            blob += b"\x00" * (4 - len(blob) % 4)
        cats.append({"paths": len(feats), "verts": verts,
                     "pos": pos_off, "idx": idx_off, "name": name_off})

    with open(OUT / "rail.bin", "wb") as f:
        f.write(blob)
    with open(OUT / "rail_meta.json", "w", encoding="utf-8") as f:
        json.dump({"cats": cats, "names": names}, f,
                  ensure_ascii=False, separators=(",", ":"))
    print("rail sections:", sum(c["paths"] for c in cats),
          "verts:", sum(c["verts"] for c in cats),
          "bin bytes:", len(blob), "lines:", len(names))
    return d["features"]


# ---------------------------------------------------------------- stations + graph
SPEED = {0: 150.0, 1: 55.0, 2: 32.0, 3: 45.0, 4: 20.0}  # km/h avg incl. stops
DWELL = {0: 1.0, 1: 0.7, 2: 0.6, 3: 0.7, 4: 0.5}


def build_stations_graph(raw_sections):
    sd = json.load(open(SRC / "N02/UTF-8/N02-23_Station.geojson", encoding="utf-8"))
    stations = []
    rom_cache = {}
    by_line = defaultdict(list)   # line key -> [station index]
    for f in sd["features"]:
        p = f["properties"]
        name = p.get("N02_005", "")
        line, op = p.get("N02_003", ""), p.get("N02_004", "")
        g = p.get("N02_005g") or p.get("N02_005c") or str(len(stations))
        cs = f["geometry"]["coordinates"]
        mx, my = cs[len(cs) // 2]
        for key in (name, line, op):
            if key not in rom_cache:
                rom_cache[key] = romaji(key)
        i = len(stations)
        stations.append({
            "i": i, "g": g, "n": name, "r": rom_cache[name],
            "ln": line, "lr": rom_cache[line], "op": op, "c": category(p),
            "x": round(mx, 5), "y": round(my, 5),
        })
        by_line[(line, op)].append(i)

    sec_by_line = defaultdict(list)
    for f in raw_sections:
        p = f["properties"]
        sec_by_line[(p.get("N02_003", ""), p.get("N02_004", ""))].append(
            f["geometry"]["coordinates"])

    rides = []
    for key, st_idx in by_line.items():
        segs = sec_by_line.get(key)
        if not segs or len(st_idx) < 2:
            continue
        # vertex graph of this line's track
        adj = defaultdict(list)
        for seg in segs:
            for (x1, y1), (x2, y2) in zip(seg, seg[1:]):
                a, b = (round(x1, 5), round(y1, 5)), (round(x2, 5), round(y2, 5))
                if a == b:
                    continue
                w = haversine_km(x1, y1, x2, y2)
                adj[a].append((b, w))
                adj[b].append((a, w))
        verts = list(adj.keys())
        # snap each station to nearest vertex (coarse grid to keep it fast)
        grid = defaultdict(list)
        for v in verts:
            grid[(int(v[0] * 100), int(v[1] * 100))].append(v)
        snap = {}
        for i in st_idx:
            s = stations[i]
            gx, gy = int(s["x"] * 100), int(s["y"] * 100)
            best, bd = None, 1e9
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for v in grid.get((gx + dx, gy + dy), ()):
                        d = haversine_km(s["x"], s["y"], v[0], v[1])
                        if d < bd:
                            best, bd = v, d
            if best is None:
                for v in verts:
                    d = haversine_km(s["x"], s["y"], v[0], v[1])
                    if d < bd:
                        best, bd = v, d
            snap[i] = best
        # multi-source dijkstra: label every vertex with nearest station
        dist, label, prev = {}, {}, {}
        pq = []
        for i in st_idx:
            v = snap[i]
            if dist.get(v, 1e18) > 0:
                dist[v], label[v], prev[v] = 0.0, i, None
                heappush(pq, (0.0, v, i))
        while pq:
            d, v, src = heappop(pq)
            if d > dist.get(v, 1e18) or label[v] != src:
                continue
            for u, w in adj[v]:
                nd = d + w
                if nd < dist.get(u, 1e18):
                    dist[u], label[u], prev[u] = nd, src, v
                    heappush(pq, (nd, u, src))

        def walk_back(v):
            path = [v]
            while prev.get(v) is not None:
                v = prev[v]
                path.append(v)
            return path  # v .. station vertex

        # voronoi boundary edges -> adjacent stations (keep track path)
        pair_best = {}
        for v in verts:
            for u, w in adj[v]:
                if v in label and u in label and label[v] != label[u]:
                    a, b = sorted((label[v], label[u]))
                    d = dist[v] + w + dist[u]
                    if d < pair_best.get((a, b), (1e18, None))[0]:
                        if label[v] == a:
                            path = walk_back(v)[::-1] + walk_back(u)
                        else:
                            path = walk_back(u)[::-1] + walk_back(v)
                        pair_best[(a, b)] = (d, path)
        cat = stations[st_idx[0]]["c"]
        for (a, b), (km, path) in pair_best.items():
            mins = km / SPEED[cat] * 60.0 + DWELL[cat]
            geom = [[p[0], p[1]] for p in path]
            rides.append([a, b, round(mins, 2), round(km, 3), geom])

    # transfer edges
    transfers = []
    by_group = defaultdict(list)
    for s in stations:
        by_group[s["g"]].append(s["i"])
    for g, idxs in by_group.items():
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                a, b = idxs[i], idxs[j]
                if stations[a]["ln"] != stations[b]["ln"]:
                    transfers.append([a, b, 5.0])
    # nearby different-group walking transfers (<300 m)
    grid = defaultdict(list)
    for s in stations:
        grid[(int(s["x"] * 200), int(s["y"] * 200))].append(s["i"])
    seen = set()
    for s in stations:
        gx, gy = int(s["x"] * 200), int(s["y"] * 200)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j <= s["i"]:
                        continue
                    o = stations[j]
                    if o["g"] == s["g"] or o["ln"] == s["ln"]:
                        continue
                    if (s["i"], j) in seen:
                        continue
                    if haversine_km(s["x"], s["y"], o["x"], o["y"]) < 0.3:
                        seen.add((s["i"], j))
                        transfers.append([s["i"], j, 8.0])

    with open(OUT / "stations.json", "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT / "graph.json", "w", encoding="utf-8") as f:
        json.dump({"rides": rides, "transfers": transfers}, f, separators=(",", ":"))
    print("stations:", len(stations), "rides:", len(rides), "transfers:", len(transfers))


if __name__ == "__main__":
    build_postal()
    raw = build_rail()
    build_stations_graph(raw)
