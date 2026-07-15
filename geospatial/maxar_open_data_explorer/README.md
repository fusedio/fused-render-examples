# Maxar Open Data explorer

Browse the Maxar Open Data program — 55+ disaster events worldwide — and stream
the high-resolution visual COGs straight into the browser via HTTP range
requests, no downloads.

<!-- Screenshot pending (WebGL map needs a live capture). -->

## What it demonstrates

A catalog explorer over a public STAC: crawl the Maxar Open Data events on S3,
list each event's acquisitions and ARD tiles, and stream the visual
Cloud-Optimized GeoTIFFs on demand — with an optional Overture buildings overlay.
Pairs well with the [COG range-request viewer](../cog_range_viewer/) to see the
byte-level mechanics.

## Run it

Copy this folder into your Fused Render install and open `explorer.html`. No keys
required (the STAC and COGs are public, CORS-open). The event catalog crawl is
resumable across the runtime's time limit.

## Files

| File | Role |
|---|---|
| `catalog.py` | Crawls the Maxar/Vantor Open Data STAC (events → acquisitions → tiles) |
| `explorer.html` | Event browser + COG-streaming map |
