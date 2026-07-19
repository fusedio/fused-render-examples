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
portably: the page loads each through a single `fused.rawUrl("data/" + name)`
(resolved against the page dir locally, and against the bundled asset map when
served) and `h3_ingest.py` reads them beside the script (`data/<name>`) — bundle
v2 lands every bundled file at its real page-relative path under the project
root, so the same path resolves locally and hosted. Local behaviour is unchanged.

Notes for deploying:

1. **The `data/` files bundle automatically** via the bundle manifest at the top
   of `index.html`:
   `<script type="application/fused-bundle">{ "include": ["data/*.json"] }</script>`.
   The exporter can't see the computed `fused.rawUrl("data/" + name)` path, so the
   glob is what ships the files (~17 MB total; within the inline-upload cap) and
   the hosted `_asset` route resolves the computed name by key. **Add a file to
   `data/` and it ships** — no per-file table to maintain.
2. **Bake DuckDB + the H3 community extension into the serve image**, and allow
   outbound HTTPS on first use so DuckDB can fetch the `h3` community extension —
   `h3_ingest.py`'s live-compute steps run on the serve interpreter. Static steps
   that only read the baked extracts work without it.
