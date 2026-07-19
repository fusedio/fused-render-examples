# Disaster response dashboard

Fuse satellite imagery, a storm track, and building footprints for a disaster
event into one map — the kind of rapid situational picture a response team
assembles in the hours after an event.

![Disaster response dashboard](../../assets/disaster_response_dashboard.png)

## What it demonstrates

Multi-layer data fusion over cloud-native sources: high-resolution post-event
imagery (Maxar Open Data), a hurricane track (NOAA IBTrACS), cloud masks, and
Overture building footprints, streamed as tiles/PMTiles and overlaid on one
MapLibre map. Built on the Maxar Open Data program (Hurricane Melissa, Oct 2025).

## Run it

Copy this folder into your Fused Render install and open `index.html`. No
keys required — all sources are public. First load crawls the imagery STAC via a
resumable, budget-bounded poll; then cached.

## Files

| File | Role |
|---|---|
| `disaster_data.py` | STAC crawl + track + AOI fusion, resumable poll protocol |
| `index.html` | MapLibre imagery/track/buildings layers + timeline |

## Deploying (hosted)

This page can be deployed. Locally the payload is assembled with a resumable
poll loop (each poll continues the acquisition fan-out where the last left off,
using `./.cache`). Hosted there is per-call subprocess isolation, so that cache
can't accumulate across polls — `disaster_data.py` detects the hosted runtime
(the `openfused` shim is present only when served) and instead **assembles the
whole payload in one, longer call**. Local behaviour is unchanged.

Requirement: **allow outbound HTTPS** to `maxar-opendata.s3.amazonaws.com` (STAC
footprints) and `www.ncei.noaa.gov` (IBTrACS best-track CSV, ~9.4 MB). No
secrets (the AOI building-damage series ships as a static snapshot). The fan-out
covers ~130 acquisitions in one call, so **confirm the per-call timeout is large
enough** — the code budgets it at 240 s (under a 300 s Lambda timeout).
