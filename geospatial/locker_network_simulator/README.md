# Parcel locker network simulator

Place parcel lockers on a map of a real delivery day and watch the courier's
route re-solve — see how many home stops get captured and how much drive time
and distance you save.

![Parcel locker network simulator](../../assets/locker_network_simulator.png)

## What it demonstrates

A live logistics optimization on real road networks: a seeded day of ~120
parcels over real Amsterdam addresses (Overture), routed with the public OSRM
road-network matrix, solved with nearest-neighbour + 2-opt + or-opt. Drop a
locker (or let it suggest sites from real shops) and the tour re-optimizes with
captured stops removed. Fully fictional scenario, real data and routing.

## Run it

Copy this folder into your Fused Render install and open `index.html`. First
run fetches Amsterdam addresses from Overture via a detached warmer (~30 s), then
routing is cached.

## Files

| File | Role |
|---|---|
| `simulate.py` | Baseline vs. locker-optimized tour + KPIs |
| `suggest.py` | Greedy next-best locker site from real shop locations |
| `tour_data.py` | Overture address/shop warm-up daemon |
| `_common.py` | OSRM matrices, TSP heuristics, caching |
| `index.html` | Map, controls, before/after KPIs |

## Deploying (hosted)

This page can be deployed. Hosted there is no local filesystem, no reachable
`127.0.0.1`, and per-call subprocess isolation — so the background warm-up daemon
can't work (a detached warmer can't outlive the call and its `./.cache` wouldn't
survive to the next one). `_common.py` detects the hosted runtime (the
`openfused` shim is present only when served) and **skips the daemon**, computing
the Overture pool/shops inline in a single, longer call; the larger hosted budget
absorbs the cold fetch the daemon existed to hide locally. Local behaviour is
unchanged.

Requirement: **allow outbound HTTPS** from the serve environment to
`router.project-osrm.org` and Overture (`stac.overturemaps.org` + its S3 in
`us-west-2`, via DuckDB `httpfs`). No secrets. Confirm the per-call timeout
accommodates the cold Overture scan.
