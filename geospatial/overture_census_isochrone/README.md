# Overture + Census isochrone dashboard

Type an address, pick a travel time, and see the drive/walk/bike isochrone —
then the Overture POIs and Census income inside it, aggregated to H3 hexes.

![Overture + Census isochrone](../../assets/overture_census_isochrone.png)

## What it demonstrates

A multi-source geospatial dashboard: geocoding + routing (OpenRouteService),
POIs (Overture Maps on S3, queried live via DuckDB), and demographics (US Census
ACS), all fused inside one reachability polygon and rendered as hexes + charts.

## Run it

Copy this folder into your Fused Render install and open `dashboard.html`.
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
| `dashboard.html` | Map + KPIs + charts |
