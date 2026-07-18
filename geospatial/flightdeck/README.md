# Flightdeck

A live flight-operations suite — seven interlinked views over real-time
ADS-B traffic, weather and earth hazards. Every number on screen is real,
right now. [Live](https://unstable.open.fused.io/wihwz3nnlet3avpsr3qa22ywrm?flight=AAL292).

## What it demonstrates

- **Flight lookup** (`flightdeck.html`, the entry view) — type a flight number
  and get its route arc on a map, a 3D model of the actual aircraft type,
  cabin amenities, METAR/TAF weather at both ends, and the live position.
  Compare two flights head-to-head with `?flight=AI302&vs=EK500`.
- **A connected fleet of views** (`pages/`) — radar (regional live traffic),
  sky (wind particles + precipitation + flights on one map), airport
  (departure/arrival board), hazards (quakes, storms, volcanoes, jet stream),
  pulse (global traffic counters) and a self-driving tour that chains them.
  Cross-page links work in the local explorer *and* on deployed mounts: the
  `FD()` helper resolves links from the page path locally and falls back to a
  `FLEET` map of mount URLs when the page is hosted.
- **Deploy-friendly authoring** — every `fused.runPython()` path is a string
  literal, heavy 3D models (24 MB of GLBs) live on a public CDN instead of the
  bundle, and secrets are `fused.secrets[...]` store lookups with an anonymous
  fallback, so the same files run locally and deploy unchanged.

## Run it

Copy the folder into your Fused Render install and open `flightdeck.html`.
Idle view shows verified flights airborne right now — click one, or try
`?flight=AAL292`. The tour (`pages/tour.html`) is the guided version.

## Data sources (all keyless)

- **ADS-B / live traffic** — [OpenSky Network](https://opensky-network.org/)
  (anonymous: 400 credits/day; add `opensky_client_id` / `opensky_client_secret`
  to the secrets store for 4000) and [adsb.lol](https://adsb.lol/).
- **Weather** — [aviationweather.gov](https://aviationweather.gov/) METAR/TAF,
  [Open-Meteo](https://open-meteo.com/), [RainViewer](https://www.rainviewer.com/).
- **Hazards** — USGS earthquakes, NOAA/NHC storm tracks, Smithsonian volcanoes.
- **3D aircraft models** — free CGTrader downloads (neutralized liveries,
  converted via obj2gltf), served from
  [AkshilVT/flightdeck-assets](https://github.com/AkshilVT/flightdeck-assets)
  via jsDelivr.
