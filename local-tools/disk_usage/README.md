# disk_usage

A treemap disk-space explorer for your local filesystem, rendered by Fused Render.

Scan any directory, see where the space actually goes as a zoomable treemap,
preview files, and move junk to the Trash — with protected-path guards so you
can't nuke something important. Standard library only.

<!-- Screenshot pending (live local-tool page needs a manual capture). -->

## What it demonstrates

A genuinely useful local desktop tool built as one UDF + one view: the Python
side walks the filesystem (`du` / `os.scandir`) and the browser draws an
interactive treemap over the result. Defaults to `~/Desktop`.

## Run it

Copy this folder into your Fused Render install and open `disk.html`. Point it
at any directory.

## Files

| File | Role |
|---|---|
| `disk.py` | `scan` (directory sizes), `preview` (file metadata), `trash` (guarded move to `~/.Trash`) |
| `disk.html` | Zoomable treemap UI over the scan result |
