#!/usr/bin/env python3
"""Resolve an eopix reference outside any browser.

    python3 resolve.py 'eopix:v1:image=...;window=...;ranges=...'

Fetches ONLY the referenced byte ranges from the COG, decodes the JPEG tiles
(stdlib + Pillow, no GDAL), crops the window and writes chip_<gers>.png.
This is the whole point of the reference: ~90 lines resolve it anywhere.
"""
import io, struct, sys, urllib.request

# The demo registry — in production this image-id -> URL lookup is a catalog (STAC).
REGISTRY = {
    "WV03_20250114_10400100A06B8000/ard11-031311102320/visual":
        "https://maxar-opendata.s3.amazonaws.com/events/WildFires-LosAngeles-Jan-2025"
        "/ard/11/031311102320/2025-01-14/10400100A06B8000-visual.tif",
    "WV02_20250708_1030010116430E00/ard14-031313311231/visual":
        "https://maxar-opendata.s3.amazonaws.com/events/Texas-Flooding-July-2025"
        "/ard/14/031313311231/2025-07-08/1030010116430E00-visual.tif",
    "WV03_20250304_10400100A1616C00/ard51-122200032211/visual":
        "https://maxar-opendata.s3.amazonaws.com/events/Typhoon-Kalmaegi-Nov-2025"
        "/ard/51/122200032211/2025-03-04/10400100A1616C00-visual.tif",
}

def parse_ref(s):
    s = "".join(s.split())
    if not s.startswith("eopix:v1:"):
        sys.exit("expected a string starting with eopix:v1:")
    return dict(p.split("=", 1) for p in s[9:].split(";") if "=" in p)

def fetch(url, a, b):
    req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}"})
    return urllib.request.urlopen(req).read()

def read_toc(url):
    """Parse the BigTIFF IFD0 tile tables from the first 256 KB (one range read)."""
    buf = fetch(url, 0, 262143)
    bo = "<" if buf[:2] == b"II" else ">"
    if struct.unpack(bo + "H", buf[2:4])[0] != 43:
        sys.exit("expected BigTIFF (this resolver keeps it minimal)")
    off = struct.unpack(bo + "Q", buf[8:16])[0]
    n = struct.unpack(bo + "Q", buf[off:off + 8])[0]
    entries = {}
    for i in range(n):
        e = buf[off + 8 + i*20 : off + 28 + i*20]
        tag, typ = struct.unpack(bo + "HH", e[:4])
        cnt = struct.unpack(bo + "Q", e[4:12])[0]
        entries[tag] = (typ, cnt, e[12:20])
    def arr(tag):
        typ, cnt, raw = entries[tag]
        fmt = {3: "H", 4: "I", 16: "Q"}[typ]
        size = struct.calcsize(fmt) * cnt
        data = raw[:size] if size <= 8 else \
            buf[struct.unpack(bo + "Q", raw)[0]:][:size]
        return struct.unpack(bo + str(cnt) + fmt, data)
    toc = {
        "width": arr(256)[0], "height": arr(257)[0],
        "tw": arr(322)[0], "th": arr(323)[0],
        "offsets": arr(324), "counts": arr(325),
        "jpeg_tables": None,
    }
    if 347 in entries:  # raw bytes, not shorts
        typ, cnt, raw = entries[347]
        vo = struct.unpack(bo + "Q", raw)[0] if cnt > 8 else None
        toc["jpeg_tables"] = buf[vo:vo + cnt] if vo else raw[:cnt]
    return toc

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        from PIL import Image
    except ImportError:
        sys.exit("pip install Pillow (the only non-stdlib dependency)")

    ref = parse_ref(sys.argv[1])
    url = REGISTRY.get(ref["image"]) or sys.exit(f"unknown image id: {ref['image']}")
    x, y, w, h = map(int, ref["window"].split(","))
    ranges = [tuple(map(int, r.split("-"))) for r in ref["ranges"].split(",")]
    gers = ref.get("overture", "/unknown").split("/")[1]

    toc = read_toc(url)
    cols = -(-toc["width"] // toc["tw"])
    chip = Image.new("RGB", (w, h))
    fetched = 0
    for a, b in ranges:                      # one HTTP request per range
        blob = fetch(url, a, b)
        fetched += len(blob)
        for t, (off, cnt) in enumerate(zip(toc["offsets"], toc["counts"])):
            if not (a <= off and off + cnt - 1 <= b):
                continue                     # tile not inside this range
            raw = blob[off - a : off - a + cnt]
            merged = toc["jpeg_tables"][:-2] + raw[2:]   # abbreviated-JPEG merge
            tile = Image.open(io.BytesIO(merged))
            tc, tr = t % cols, t // cols
            chip.paste(tile, (tc * toc["tw"] - x, tr * toc["th"] - y))

    out = f"chip_{gers[:8]}.png"
    chip.save(out)
    print(f"wrote {out} ({w}x{h} px) — fetched {fetched:,} bytes "
          f"in {len(ranges) + 1} range requests (incl. table of contents)")
    print("note: mask not applied — re-derive the footprint from "
          f"Overture {ref.get('overture', '?').split('/')[0]} to verify/clip; "
          "the sha-256 in the reference is the check.")

if __name__ == "__main__":
    main()
