# Buildings → hexagons

An interactive, four-minute scrollytelling explainer: how do you draw all
2.6 billion buildings on Earth in a browser, from a file smaller than one phone
photo?

![Buildings to hexagons](../../assets/buildings_to_hexagons.png)

## What it demonstrates

H3 hexagon aggregation as a data-compression story. The whole planet's building
footprints are summarized into ~13k hexagons (one 0.4 MB file), and the explainer
walks through the trick step by step — with live maps at each stage — plus what
the summarization costs you.

## Run it

Copy this folder into your Fused Render install and open `index.html`. The
`data/` folder ships the pre-computed hex tiles; `h3_ingest.py` shows how they
were built from Overture building footprints.

## Files

| File | Role |
|---|---|
| `index.html` | The step-driven interactive story |
| `h3_ingest.py` | Aggregates Overture buildings into H3 hexes (how `data/` was made) |
| `data/` | Pre-computed hex tiles for several cities + the world |

## Deploying (hosted)

This page can be deployed. Both the client (baked extracts) and `h3_ingest.py`
(live H3 aggregation) load the bundled `data/` files, and both resolve them
portably: the page maps each file through a build-time `fused.rawUrl("./data/…")`
literal (`RAW_URLS` in `index.html`) and `h3_ingest.py` uses
`openfused.asset_path("data", …)` when served (a read-only bundle exposes no
`/api/fs/raw` and no writable `data/` path). Local behaviour is unchanged (the
app reads via `/api/fs/raw`).

Notes for deploying:

1. **The `data/` files bundle automatically.** A hosted page can only fetch paths
   the exporter saw as string literals at build time, so every file is registered
   as a literal in the `RAW_URLS` manifest in `index.html` — that both bundles it
   (~17 MB total; within the inline-upload cap) and lets the client look it up by
   its computed name. **Add a row to `RAW_URLS` whenever you add a data file**, or
   it won't be exported.
2. **Bake DuckDB + the H3 community extension into the serve image**, and allow
   outbound HTTPS on first use so DuckDB can fetch the `h3` community extension —
   `h3_ingest.py`'s live-compute steps run on the serve interpreter. Static steps
   that only read the baked extracts work without it.
