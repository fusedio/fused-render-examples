# download_manager

A multithreaded, resumable download manager, rendered by Fused Render.

Paste one or more URLs, pick a destination folder and a thread count, and watch
segmented downloads progress live — with pause/resume, a shared speed limit, a
throughput chart, and reveal-in-Finder on finished files. Downloads run in a
detached worker, so they survive both the page and the app closing. Standard
library only.

## What it demonstrates

State that outlives a `runPython` call. Every `main()` invocation is a fresh
subprocess killed at 60 s, so nothing here lives in globals: task metadata,
per-segment byte offsets, the worker heartbeat, and the shared rate-limit
bucket all live on disk under `_downloads/`. The `start` action spawns
`daemon.py` in its own session to move bytes without a timeout, falling back to
a bounded inline `pump` if the hand-off fails. Because progress is persisted per
segment as it happens, an interrupted download resumes rather than restarts —
across a pause, a crash, or a reboot.

## Run it

Copy this folder into your Fused Render install and open `index.html`. Paste a
URL and hit add; downloads land in `~/Downloads` unless you change the folder.

## Files

| File | Role |
|---|---|
| `downloads.py` | The engine: `add`/`probe`, `start` (detached) and `pump` (inline), `pause`/`resume`, `list`, `remove`, `limit`, `open`/`reveal` |
| `daemon.py` | Detached per-task worker (`python daemon.py <task-id>`), spawned by `start` so a download outlives the page |
| `index.html` | Queue UI: add form, per-task progress and segments, speed chart, filters, and the shared speed limit |
