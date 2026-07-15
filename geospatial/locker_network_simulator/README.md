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

Copy this folder into your Fused Render install and open `simulator.html`. First
run fetches Amsterdam addresses from Overture via a detached warmer (~30 s), then
routing is cached.

## Files

| File | Role |
|---|---|
| `simulate.py` | Baseline vs. locker-optimized tour + KPIs |
| `suggest.py` | Greedy next-best locker site from real shop locations |
| `tour_data.py` | Overture address/shop warm-up daemon |
| `_common.py` | OSRM matrices, TSP heuristics, caching |
| `simulator.html` | Map, controls, before/after KPIs |
