# Store site selection

"Where should I open a cafe?" — score Census tracts in a city by population,
income, and distance to existing competitors, with weight sliders that re-rank
instantly.

![Store site selection](../../assets/store_site_selection.png)

## What it demonstrates

A classic site-selection workflow built from open data: Census tracts + ACS
demographics (keyless Census APIs) and competitor POIs (Overture Places on S3
via DuckDB). Scoring runs in the browser so the weight sliders re-rank with zero
Python round-trips.

## Run it

Copy this folder into your Fused Render install and open `index.html`. It opens
straight to a working **Austin** dashboard with **no setup** — that city ships as
a bundled data snapshot (`data/austin.json`).

To load the other cities live, add a free `CENSUS_API_KEY`: copy `.env.example`
to `.env` ([get one here](https://api.census.gov/data/key_signup.html)). Without
a key, non-bundled cities show an inline prompt rather than an error. First live
load per city fetches via a detached warmer, then caches.

## Demo data snapshot

`data/austin.json` is the committed `_fetch_city("austin")` payload (tracts +
ACS demand + Overture cafes) so the example renders instantly, key-free. It's
geometry-simplified (shapely, ~44 m tolerance) to keep the file small (~240 KB).
Regenerate with a key set by running `_fetch_city("austin")` and dumping
`{label, tracts, cafes}` to `data/austin.json`, then simplifying tract polygons.

## Files

| File | Role |
|---|---|
| `site_data.py` | Tracts + demographics + competitor POIs + warm-up daemon |
| `index.html` | Choropleth + weighted scoring UI |
| `data/austin.json` | Bundled key-free demo snapshot (default city) |

## Deploying (hosted)

This page can be deployed. Hosted there is no local filesystem and per-call
subprocess isolation, so the background warm-up daemon can't work.
`site_data.py` detects the hosted runtime (the `openfused` shim is present only
when served) and **skips the daemon**, computing the city fetch (TIGERweb tracts
+ ACS demand + Overture Places) inline in a single, longer call. Local behaviour
is unchanged.

Requirements:
- **`CENSUS_API_KEY` as a hosted secret** — needed only for cities *other than*
  the bundled Austin snapshot, which renders hosted with no key. Other cities'
  ACS demand call needs it (locally it's read from a sibling `.env`); without it
  they show the inline "add a key" prompt instead of erroring.
- **Allow outbound HTTPS** to `tigerweb.geo.census.gov`, `api.census.gov`, and
  Overture Places S3 (`us-west-2`, via DuckDB `httpfs`). Confirm the per-call
  timeout accommodates the cold fetch.
