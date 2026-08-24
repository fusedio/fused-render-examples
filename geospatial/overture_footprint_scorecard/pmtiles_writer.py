"""Minimal PMTiles v3 writer: clustered archive, gzip tiles, root directory
only (entry counts here stay far below the 16 KB root-directory budget)."""
import gzip
import json
import struct


def zxy_to_tileid(z, x, y):
    tileid = (4 ** z - 1) // 3
    s = 1 << (z - 1) if z else 0
    d = 0
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return tileid + d


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _directory(entries):
    out = bytearray(_varint(len(entries)))
    last_id = 0
    for tileid, _offset, _length in entries:
        out += _varint(tileid - last_id)
        last_id = tileid
    for _ in entries:
        out += _varint(1)
    for _tileid, _offset, length in entries:
        out += _varint(length)
    previous_end = None
    for _tileid, offset, length in entries:
        if offset == previous_end:
            out += _varint(0)
        else:
            out += _varint(offset + 1)
        previous_end = offset + length
    return bytes(out)


def write_pmtiles(path, tiles, *, bounds, minzoom, maxzoom, metadata):
    """tiles: mapping of (z, x, y) -> raw MVT bytes (uncompressed)."""
    entries = []
    blob = bytearray()
    for (z, x, y), data in sorted(
        tiles.items(), key=lambda item: zxy_to_tileid(*item[0])
    ):
        compressed = gzip.compress(data, 6)
        entries.append((zxy_to_tileid(z, x, y), len(blob), len(compressed)))
        blob += compressed

    root = gzip.compress(_directory(entries), 6)
    if len(root) > 16384 - 127:
        raise ValueError(
            f"root directory is {len(root)} bytes; reduce maxzoom so it fits"
        )
    meta = gzip.compress(json.dumps(metadata).encode("utf-8"), 6)

    root_offset = 127
    meta_offset = root_offset + len(root)
    data_offset = meta_offset + len(meta)
    minx, miny, maxx, maxy = bounds
    header = struct.pack(
        "<7sBQQQQQQQQQQQBBBBBBiiiiBii",
        b"PMTiles", 3,
        root_offset, len(root),
        meta_offset, len(meta),
        0, 0,
        data_offset, len(blob),
        len(entries), len(entries), len(entries),
        1, 2, 2, 1,
        minzoom, maxzoom,
        int(minx * 1e7), int(miny * 1e7),
        int(maxx * 1e7), int(maxy * 1e7),
        minzoom,
        int((minx + maxx) / 2 * 1e7), int((miny + maxy) / 2 * 1e7),
    )
    assert len(header) == 127, len(header)
    with open(path, "wb") as f:
        f.write(header)
        f.write(root)
        f.write(meta)
        f.write(blob)
