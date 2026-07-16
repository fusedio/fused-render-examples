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

Copy this folder into your Fused Render install and open `index.html`. Needs
a free `CENSUS_API_KEY` — copy `.env.example` to `.env`
([get one here](https://api.census.gov/data/key_signup.html)). First load per
city fetches the data via a detached warmer; then it's cached.

## Files

| File | Role |
|---|---|
| `site_data.py` | Tracts + demographics + competitor POIs + warm-up daemon |
| `index.html` | Choropleth + weighted scoring UI |

## Deploying (hosted)

This page can be deployed. Hosted there is no local filesystem and per-call
subprocess isolation, so the background warm-up daemon can't work.
`site_data.py` detects the hosted runtime (the `openfused` shim is present only
when served) and **skips the daemon**, computing the city fetch (TIGERweb tracts
+ ACS demand + Overture Places) inline in a single, longer call. Local behaviour
is unchanged.

Requirements:
- **Provision `CENSUS_API_KEY` as a hosted secret** — this is *required*: the ACS
  demand call 401s without it (locally it is read from a sibling `.env`).
- **Allow outbound HTTPS** to `tigerweb.geo.census.gov`, `api.census.gov`, and
  Overture Places S3 (`us-west-2`, via DuckDB `httpfs`). Confirm the per-call
  timeout accommodates the cold fetch.
