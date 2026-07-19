# Overture + Census isochrone dashboard

Type an address, pick a travel time, and see the drive/walk/bike isochrone —
then the Overture POIs and Census income inside it, aggregated to H3 hexes.

![Overture + Census isochrone](../../assets/overture_census_isochrone.png)

## What it demonstrates

A multi-source geospatial dashboard: geocoding + routing (OpenRouteService),
POIs (Overture Maps on S3, queried live via DuckDB), and demographics (US Census
ACS), all fused inside one reachability polygon and rendered as hexes + charts.

## Run it

Copy this folder into your Fused Render install and open `index.html`.
Needs two free API keys — copy `.env.example` to `.env` and fill in:
- `ORS_API_KEY` — [openrouteservice.org](https://openrouteservice.org/dev)
- `CENSUS_API_KEY` — [api.census.gov](https://api.census.gov/data/key_signup.html)

## Files

| File | Role |
|---|---|
| `iso_area.py` | Geocode + isochrone + hex universe |
| `poi_panel.py` | Overture POIs inside the isochrone → H3 counts |
| `census_panel.py` | Census ACS income per hex |
| `_common.py` | Shared caching, DuckDB/H3, key loading, warm-up daemon |
| `index.html` | Map + KPIs + charts |

## Deploying (hosted)

This page can be deployed. Hosted there is no local filesystem and per-call
subprocess isolation, so the POI panel's background warm-up daemon can't work.
`_common.py` detects the hosted runtime (the `openfused` shim is present only
when served) and **skips the daemon**, running the Overture Places scan inline in
a single, longer call (the isochrone and census panels already run inline). Local
behaviour is unchanged.

Requirements: **allow outbound HTTPS** to `nominatim.openstreetmap.org`,
`valhalla1.openstreetmap.de`, `api.openrouteservice.org`, Overture
(`stac.overturemaps.org` + S3), `tigerweb.geo.census.gov`, and `www2.census.gov`.
`ORS_API_KEY` is optional (Valhalla is the keyless primary; ORS is a fallback);
if you want the ORS fallback, provision it as a hosted secret. Confirm the
per-call timeout accommodates the cold Overture scan.
