"""Search the local parquet index -- instant, offline, spatially aware.

Same query understanding and ranking as discover.py (shared code), but the
collections come from ./data/index/parts/*.parquet via duckdb instead of live
HTTP fan-out: bbox intersection and the kind (raster/vector) filter run as SQL
predicates, token matching is a coarse SQL prefilter, and the surviving rows
are scored/ordered in Python by the exact same scorer the live path uses.
"""

import time

import discover
import index_store as store


def main(q: str = "", bbox: str = "", datetime: str = "", limit: int = 60, kind: str = ""):
    started = time.time()
    parts = store.part_files()
    meta = store.read_meta()
    if not parts:
        return {"q": q.strip(), "built": False, "place": None, "bbox_used": "",
                "collections": [], "sources": [], "total": 0, "indexed": 0,
                "elapsed_ms": 0}

    tokens, place, qbox, bbox_used = discover.resolve_query(q, bbox)
    qinterval = discover._parse_interval(datetime)

    rows, indexed = _query(parts, tokens, qbox, kind.strip().lower())

    # term weights (idf) are judged across the candidate rows, same as the live
    # path, so a distinctive query term outranks common ones
    weights = discover.idf_weights(rows, tokens) if (tokens and rows) else None

    # Score and filter on the raw rows (title/keywords/... are plain columns);
    # only the rows that make the cut pay for full materialization.
    scored = []
    for row in rows:
        text_score = discover._score_tokens(row, tokens, weights)
        if tokens and text_score == 0:
            continue
        if qinterval and not discover._interval_overlap(qinterval, [row["t_start"], row["t_end"]]):
            continue
        bbox = None if row["west"] is None else [row["west"], row["south"], row["east"], row["north"]]
        bonus = discover._spatial_bonus(qbox, bbox) if (text_score or not tokens) else 0.0
        scored.append((text_score + bonus, text_score, row))
    scored.sort(key=lambda s: (-s[0], (s[2]["title"] or "").lower()))
    total = len(scored)

    collections = []
    for score, text_score, row in scored[: max(1, int(limit))]:
        c = store.collection_from_row(row)
        c["text_score"] = text_score
        c["score"] = score
        c["server_matched"] = False
        collections.append(c)

    sources = []
    for slug, entry in sorted(meta.get("sources", {}).items()):
        spec = entry.get("source", slug)
        sources.append({
            "base": spec,
            "host": discover._host(store.source_url(spec)),
            "ok": entry.get("status") != "error",
            "supports_q": True,
            "returned": entry.get("count", 0),
            "number_matched": None,
            "error": entry.get("error"),
        })

    return {
        "q": q.strip(),
        "built": True,
        "place": place,
        "bbox_used": bbox_used,
        "collections": collections,
        "sources": sources,
        "total": total,
        "indexed": indexed,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _query(parts, tokens, qbox, kind):
    import duckdb
    con = duckdb.connect()
    files = "[" + ", ".join("'" + p.replace("'", "''").replace("\\", "/") + "'" for p in parts) + "]"
    where, params = [], []

    if qbox:
        # overlap, or no advertised extent (don't exclude those -- same rule as live).
        # A stored antimeridian-crossing row (west > east, STAC's encoding for it --
        # query boxes here never cross: drawn ones clamp at +/-180, the gazetteer
        # clamps Russia's to 180.0) is the union of [west,180] and [-180,east], so
        # it needs the opposite combinator from the ordinary west<=east case --
        # same idea as the map's splitAM, expressed as a predicate instead of a draw.
        where.append("""(
            west IS NULL
            OR (west <= east AND NOT (east < ? OR west > ? OR north < ? OR south > ?))
            OR (west > east AND (east >= ? OR west <= ?) AND north >= ? AND south <= ?)
        )""")
        params += [qbox[0], qbox[2], qbox[1], qbox[3],
                   qbox[0], qbox[2], qbox[1], qbox[3]]
    if kind in ("raster", "vector"):
        where.append("kind = ?")
        params.append(kind)
    if tokens:
        # coarse prefilter; the word-boundary scorer makes the real call
        ors = []
        for t in tokens:
            ors.append("(title ILIKE ? OR id ILIKE ? OR description ILIKE ? "
                       "OR array_to_string(keywords, ' ') ILIKE ? OR source_terms ILIKE ?)")
            params += ["%" + t + "%"] * 5
        where.append("(" + " OR ".join(ors) + ")")

    # union_by_name so parts written before a schema addition still load
    files = f"{files}, union_by_name=true"
    sql = f"SELECT * FROM read_parquet({files})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    # total indexed comes from parquet footers -- no second data scan
    import pyarrow.parquet as pq
    indexed = sum(pq.ParquetFile(p).metadata.num_rows for p in parts)
    return rows, indexed


if __name__ == "__main__":
    import json
    print(json.dumps(main(q="land cover for india", kind="raster", limit=8), indent=2)[:4000])
