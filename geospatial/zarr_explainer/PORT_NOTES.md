# Zarr — the guided story (step-driven) — port notes

## Rev 5 (2026-07-20) — pixel icon integration (visual garnish only)

`pixel_icons/icons.js` is now **inlined** into `explainer.html` between
`/* PIXEL_ICONS_INLINE_START */` / `/* PIXEL_ICONS_INLINE_END */` markers in
its own `<script>` block (relative `<script src>` doesn't resolve under
`/render?path=…`, and `/view/...` returns an HTML wrapper). **Re-sync pitfall:**
icons.js's header comment contains a literal `</script>` — it must be escaped
to `<\/script>` inside the inline block or the script terminates early
("Invalid or unexpected token" + `PixelIcons is not defined`). The marker
comment says so too.

Placements (all via a `pixIcon(name, size, css)` helper; every canvas is
`pointer-events:none`, no interaction/copy/layout changes):
- **Act badges** — 18 px glyph before "ACT N · LABEL" in the kicker, one per
  act: globe / bulb / folder / receipt / bolt_fast ("try it" wasn't in the
  spec; bolt_fast chosen). Negative vertical margins (`margin:-5px 7px -3px 0`,
  `vertical-align:middle`) keep the inline-block from growing the line box, so
  header height is unchanged.
- **Step 9 format cards** — hand-rolled canvas sketches replaced by
  PixelIcons zarr / cog / netcdf / parquet at **64 px** (96 px and even 88 px
  push the bottom caption out of the 500 px stage — shrank instead of growing).
  Hover-dim grammar untouched (icons dim with their card).
- **Step 10 dataset cards** — 28 px glyph left of each title (MUR→globe,
  GEFS→clock_wait, CMIP6→cube, WSF→chunks_grid). The `.dn` title is now
  `display:flex` so two-line titles hang right of the glyph instead of
  wrapping underneath it.
- **Step 8 receipt** — 26 px `receipt` glyph beside the big byte total
  (flex row, centered).
- **Skipped:** `link_out` on the "learn more →" links — the text already ends
  in an arrow; a boxed gold glyph doubles the affordance and fights the serif
  header. Sparse won.

Verified (Playwright chromium headless, `--use-angle=metal`, 1440×830, via
`/render?path=…&step=N`): steps 0/2/5/8/9/10/11 — 0 pageerrors, 0 vertical
overflow (scrollHeight = 830 everywhere), all icon canvases non-blank
(pixel-sampled), crisp (imageSmoothing off in the lib), step-9 hover still
dims siblings + gold-borders the hot card. Shots in the session scratchpad
`icon_integrate_shots/`. (Step 5's pre-existing 300×150 blank preview canvas
is the lazy preview pane, not an icon.)

## Rev 4 (2026-07-20) — session-3 feedback: titles, copy-trim, receipt datacube

**Titles renamed** (Max's exact wording, mapping old-13-step numbers → the
current 12): 0 "An Interactive guide to Zarr" (page `<title>` matches;
subtitle is now ONE sentence naming the ocean-temperature dataset as the test
case) · 1 "Brute force approach: downloading everything" · 2 "Chunking" ·
3 "Chunk positions" · 4 "Zarr stores are folders of files" · 5 "Reading just
the chunks you need" (**title TBD — Max deciding keep/skip**, marker comment
next to it in the source) · 6 "Chunks in practice" · 7 "Sharding" · 8 "Your
data receipt" · 9–11 unchanged.

**Reduce-verbose pass**: every step lede cut to ≤ 2 short sentences (they
ranged 2–4; e.g. step 0 41→20 words, step 2 57→31, step 4 39→22); stage
captions trimmed to one line each (dropped double explanations and
parentheticals — e.g. the step-10 caption that repeated the cards' own
"open in the playground" was deleted). Hover-reveal texts untouched;
interaction cues kept but shortened.

**New: the receipt datacube (step 8).** `SES.touched` (a Map, so it dedupes
across replays) records every chunk a read touched — `recordTouches()` parses
v3 keys (`analysed_sst/c/t/y/x`, the mock store) and v2 dot keys
(`analysed_sst/t.y.x`, NASA's store) at all three read sites: the step-5 mock
read (16 July cells), the real S3 read (its one chunk), and playground reads
on MOCK/MUR; playground reads on other stores bump `SES.otherReads` and the
step-8 caption notes they aren't drawn. The cube: 12 ghost slabs (mini store,
month 0 on top), fetched cells vibrant thermal orange; a real read adds a
second ghost slab — NASA's 11×10 grid, labeled with its time slab of 1,289.
Hovering a vibrant cell dims everything else (the house hover grammar) and
prints its key + size. Cold entry shows the ghost cube + "read something
first — your pulls will light up here". State lives on the module-level SES,
so it survives step navigation like the receipt totals always did.
Ledger/bigno moved to the right of the cube; hit-testing is manual
point-in-quad (canvas isPointInPath vs the dpr transform is a trap).

**Verified 2026-07-20** (Playwright chrome headless `--use-angle=metal`,
1440×830, `/render?path=…&step=N`): 12/12 steps screenshotted and eyeballed,
0 pageerrors, 0 vertical overflow; all new titles + the one-line step-0
subtitle render; step-5 three beats → step 8 shows 16 lit cells
("17 chunks fetched" after the real read), hover prints
`analysed_sst/c/6/3/0 · 15 KB — local mini store`; full real S3 read
(13 MB / 8 req / 77.4 s cold) → NASA slab appears, hover prints
`analysed_sst/1242.7.5 · 13 MB — NASA's store on S3`. Shots: scratchpad
`s3_shots/`. (Beat buttons need `:visible` in selectors — the step keeps the
hidden beat button in the DOM.)

---

## Rev 3 (2026-07-20) — session-2 feedback + Zarr-3-only mock store

The mock store was rebuilt as **Zarr v3** (`zarr.json` everywhere, chunk keys
`analysed_sst/c/t/y/x`, 200 files / 4.85 MB, zstd) and the story now teaches
**Zarr 3 only** — no v2/v3 lesson. The real MUR store is still v2; the single
place its dot-key appears (the real read result) carries one throwaway line
("NASA's store is older and spells its names with dots — same idea").

**Now 12 steps** (old 0+2 merged): 0 hook w/ 4-phase AUTOPLAY build (grid
draws → slabs stack → counter multiplies to 4.17 T → 8.3 TB), each phase
reveals a stat chip; after the build, hovering a chip highlights its component
and dims everything else · 1 download-everything · 2 cut — plays once on
entry, then the pointer scrubs cut progress (x-position) and resting on a
tile lifts THAT tile (label = its v3 key); no replay button · 3 naming —
axes annotated (lat ticks 0–3 "counted from the south", lon 0–3 west→east,
month strip letters + index digits), map-hover dims the strip and vice versa
· 4 tree — v3 files, chunk hover greys the ENTIRE mini-map except that cell,
zarr.json previews are plain-words "ID cards" with a "see the actual file →"
raw-JSON toggle · 5 reading — three click-through beats (compute the names →
16 v3 key chips / fetch → tiles light one-by-one ~2 s / paint), then the
real S3 read with a PINNED fixed-height download panel: progress bar off the
byte counter, mini-map of the one chunk, one status line updated in place;
`/clearcache` runs first so the real read is ALWAYS cold · 6 all-or-nothing —
bars + a datacube (pixelated iso boxes: gold sliver inside blue MUR chunk;
CMIP6 box dwarfing the speck), one cube state per beat · 7 sharding REDESIGN —
the SAME 16 world tiles from the cut step (real coastline crops, labeled
c/6/y/x, REAL file sizes from /ls) pack into one horizontal file strip with
16 colored byte bands + an index table; hover any tile/band/index-row →
static triple highlight, rest dims, nothing moves · 8 receipt · 9 formats —
all 4 cards on ONE page, staggered entrance, hover dims the others ·
10 dataset cards · 11 playground.

**Global hover grammar** (user rule 3): hovering any component dims the rest
(`.dimhov/.dim`, canvas alpha) — retrofitted to naming/tree/sharding/formats.

**zarr_probe.py**: new `/clearcache?file=` (drops the store handle + LRU +
meter → next read genuinely cold); parallel-fetch threshold 16→8 MB and a new
per-part `inflight` byte counter in the meter (returned by `/stats`) so the
read-step progress bar moves DURING the transfer, not only at the end.
Version hash bumped → stale daemons self-restart. Note: a running daemon
holds opened stores in memory — after rebuilding the mock store it served the
old v2 tree until restarted (clearcache/version-bump both fix this).

**Playground cold-load bug** (session-2 item 11): `?step=…&file=…` deep links
failed because `fused.params.get("file")` doesn't carry ad-hoc query params
under the app — the playground now reads `file` from `location.search` first
(same trick as the `?step=` boot). All four dataset cards verified cold.

**Verified 2026-07-20** (Playwright chrome headless `--use-angle=metal`,
1440×830, `/render?path=…&step=N`): 12/12 steps, 0 pageerrors, 0 vertical
overflow. Exercised: step-0 mid-build + all 4 chip hover-dims; cut scrub at
3 pointer positions + tile lift; axis annotations + mutual map/strip dim;
tree ID cards + raw toggle + chunk isolate; all 3 read beats; REAL S3 read
cold twice (13 MB in 55.8 s / 71.0 s — clearcache confirmed, bar ticked
3.4→5→6.7→10→12→13.4 MB); datacube per beat; shard pack + tile/band/index-row
static hovers; formats one-page hover-dim (entrance animation had to be
transition-based — `animation-fill-mode:both` beat the `.dim` opacity);
every dataset card cold-loads (MUR 7 arrays, GEFS 31, CMIP6 7, WSF 52);
mock playground read lights v3-keyed cells. Screenshots: scratchpad
`s2_shots/`.

---

## Rev 2 (2026-07-20) — feedback rework ("Zarr explainer.md" in the obsidian vault)

Full rework of the steps against Max's written feedback. Design rules applied
everywhere: one piece of information at a time (click/hover reveals the next);
anything that would take >2 s against S3 runs on the **local mock store**
(`mock_store/mur_sst_mini.zarr` — real ERA5-derived SST, 12×360×720, 1×90×180
chunks, 206 files / 4.7 MB, the exact layout of `s3://mur-sst`); real S3 is
ONE explicit opt-in moment where the wait is the lesson; no curl, no codec
jargon, per-act "Learn more →" links (zarr.dev / earthmover blog / CNG guide).

**The 13 steps now:** 0 hook · 1 download-everything · 2 stack of world maps
(lat/lon face drawn TO SCALE 2:1 with the real coastline from
`land_mask.json`, inlined into the page) · 3 chunking (animated: grid lines
sweep in, tiles shatter apart, one hero chunk pops forward; replay button) ·
4 computable names (hover-driven, gers_pixel_ref-style: pointer over the map
+ a month strip writes `analysed_sst/6.2.3` live, the changing segment glows;
bidirectional — hovering a key segment lights its axis band on the map) ·
5 tree-first store tour (real `/ls` file tree of the mock store; hover any
file → preview: .zmetadata's 5 fields with per-field plain-word hover
explanations, chunk files → size + mini-map of their position; absorbs old
steps 5+8, old 5-D step 9 deleted) · 6 reading (mock read animates 16 chunks
lighting then renders a real blue→red SST world map; then opt-in "do it for
real" → live MUR read with a byte counter polled from `/stats`) · 7
all-or-nothing (click-through sub-beats: asked 2.2 MB → sent 13 MB → CMIP6
80 MB, one bar at a time) · 8 sharding (synthetic: 32 scattered files fly
into one shard box; hover an inner chunk → its byte-range band in the file
strip + the index entry) · 9 receipt (network bytes only; local mock reads
listed as free) · 10 format cards (Zarr / GeoTIFF+COG / NetCDF-HDF5 /
Parquet, one card at a time, iconographic) · 11 dataset cards (MUR / GEFS /
CMIP6 / WSF — click opens the playground preloaded via `?file=`) · 12
playground (defaults to the mock store; mock chip added).

**zarr_probe.py:** added `/ls?file=` (walks a LOCAL store dir, returns every
file + size; refuses remote URLs and `~/.fused-render` mounts). Version hash
auto-bumps → stale daemons restart themselves.

**Facts for the new mock store:** land_mask row 0 = 90°S (data is
south-first; the page flips for display). Full-plane mock read = 16 files,
~380 KB compressed, <10 ms. Chunk file sizes 10–32 KB (`0.0.0` 24.0 KB,
`6.2.3` 32.2 KB, `11.3.3` 9.9 KB); `.zmetadata` 1,807 B. Hero chunk used
throughout steps 3/4/5/6: `analysed_sst/6.2.3` (July, lat band 2, lon block
3 — S/SE Asia, distinct digits on all three axes on purpose).

**Verified 2026-07-20** (Playwright chrome headless, 1440×830, via
`/render?path=…&step=N`): all 13 steps screenshotted, zero page errors
(one benign favicon 404), zero vertical overflow; hover-naming key updates +
segment glow both directions; tree hover previews incl. per-field
explanations; mock read renders a colored SST map; real S3 read moved
13 MB in 37.7 s with the live counter ticking; sub-beats, sharding hover
(index entry + byte band), format cards, dataset-card → playground
navigation, playground mock default + local read — all exercised and
eyeballed. Screenshots: scratchpad `rework_shots/`.

---

## Rev 1 (original notes below — step numbering is pre-rework)

**What it is** — variant A of the A/B experiment against `what_is_zarr` (the
single-view inspector). Same subject, opposite shape: a 14-step, 5-act guided
story in the tiles-explainer step engine (`Next`/`Back`/arrow keys/progress
dots, fixed 500px stage, `?step=N` deep links). Audience: technically
comfortable, zero geospatial/scientific-data background. Every number is
measured live via the daemon (`/tree /rawmeta /slice /head`) or explicitly
labelled an estimate/canned copy.

**Files**
- `explainer.html` — self-contained page (no CDN deps), paper-editorial style.
- `zarr_probe.py` — copy of `what_is_zarr/zarr_probe.py` with two changes:
  `STATE`/`DAEMON_VENV` moved to `~/.cache/fused-render-zarrsteps/` (so this
  daemon and the sibling ports' daemons don't kill each other via the shared
  state file; `SHARED_VENV` still points at the zarraoi venv, so no rebuild),
  and a new **`/head?url=`** endpoint (HEAD any s3://, https:// or local
  object → exact byte size; follows the S3 `x-amz-bucket-region` redirect
  dance) so the page can quote real object sizes *without* downloading them.

## The 14 steps (5 acts)
- **Act 1 · the problem** — 0: MUR SST hook (6,443 days × 17,999 × 36,000 =
  4.17T numbers / 8.3 TB, live from `.zmetadata`); 1: download-everything
  simulation (progress advances at a real 300 Mbit/s pace, "≈ 2.6 days"
  labelled an estimate; bar width exaggerated 8× and says so) + the pinch
  question.
- **Act 2 · how Zarr works** — 2: the cube (axes labelled, real dims);
  3: cut into chunks (11 × 10 × 1,289 = 141,790, real grid); 4: names are
  computable (division card → `analysed_sst/1242.7.5`, cube cells cycle real
  keys); 5: the real `.zmetadata` verbatim (scrolled to `analysed_sst/.zarray`,
  10.5 KB describing 8.3 TB = 1:791M, size via /head); 6: **live read** —
  2 June 2019, seas around Italy → 13.4 MB / 1 request / ~5 s cold, slice
  painted on the cube, cache story on re-read; 7: all-or-nothing gotcha —
  asked 2.2 MB vs 13 MB moved vs CMIP6's 80,221,890-byte chunk (live HEAD,
  never downloaded; 600-month chunks).
- **Act 3 · on disk** — 8: store = named files; tree with real keys; the same
  chunk as local path / s3:// / https:// (https one HEAD-verified); one-sentence
  v2 (`.zmetadata`) vs v3 (`zarr.json`).
- **Act 4 · why it won** — 9: GEFS 5-D (2,119×31×181×721×1440, live tree);
  10: sharding — same pinch-sized ask read live from the v3 sharded store:
  289 KB vs MUR's 13 MB (~46×), shard spans 3.3 GB logical; 11: the receipt —
  session ledger (every fetch listed) vs the 2.6-day estimate; 12: adoption
  card (NASA/NOAA, CMIP6, ESA EOPF, Google ERA5, OME-Zarr, genomics) + the
  HDF5/NetCDF vs Zarr vs Parquet table ("Zarr is Parquet's sibling for grids").
- **Act 5 · playground** — 13: free inspector (paste any store; preset chips
  MUR/GEFS/CMIP6/WSF; array dropdown; draggable per-dim chunk bars; honest
  estimate w/ caps 64 objects / 4M cells; live reads paint the cube).

## Measured facts baked in as canned demo values (2026-07-20)
- MUR slice (t=6210, lat 12700–13500, lon 18700–20100) → `analysed_sst/1242.7.5`,
  13,384,457 B, 1 request, 4.5 s, 290.19–294.35 K.
- GEFS `temperature_2m` (idx 2118,0,4; lat 188–202, lon 48–62) → shard
  `c/2118/0/0/0/0`, 289,005 B in 2 range requests, 13.7–19.5 °C.
- CMIP6 `ts/1.0.0` = 80,221,890 B (HEAD); `.zmetadata` = 10,551 B.

## Pitfalls hit (beyond what_is_zarr's list, which all still apply)
- **Concurrent `fused.runPython` calls hang.** Boot warming + a step's
  `api()` + `headObj()` all called `ensureDaemon()` at once → three parallel
  runPython calls → the page stalled forever on "fetching…" with **no error
  anywhere**. Fix: single-flight `ensureDaemon()` (share one promise), and
  `api()` awaits it instead of trying a fetch against `port=null` first.
- **The daemon's chunk cache is in-memory only** (`CACHE_CAP` LRU). Verifying
  "cold" read numbers after curl-testing the same window requires
  `curl /quit` + re-ensure; there is no `.cache` dir to move for this one.
- The MUR chunk compressed size **varies ~4–34 MB by content** (ice/land
  compress small, open Atlantic big). Lisbon's chunk (7,4) is 33 MB ≈ 60 s+
  on this link — moved the through-line to the Mediterranean chunk (7,5),
  13.4 MB ≈ 5 s. Same lesson, tolerable wait. Polar chunk 1241.9.4 is 4.3 MB
  if it ever needs to be cheaper.
- `data.dynamical.org` returns **403 to HEAD** requests — GEFS sizes are
  quoted from store metadata (logical) instead of /head.
- One early concurrent-curl test showed `/slice` touched keys from **two**
  time chunks (`1200.7.4` + `1241.7.4`) for a single-day read — not
  reproducible sequentially; likely concurrent ops' keys bleeding into each
  other's `touched` attribution in `metered()`. The page only ever issues
  sequential reads, so not chased further.
- Deep-linking in-app: `fused.params` may not carry ad-hoc query params, so
  the boot reads `?step=` straight from `location.search` first (works both
  for `/render?path=…&step=N` and `file://…?step=N`).
- Receipt step: cached reads report `requests: 0`, so "did you do the live
  steps" must key off `SES.log.length`, not request count — otherwise it
  claims you skipped steps you did (with a visible ledger right below).

## Test log (2026-07-20, app at 127.0.0.1:1777, Playwright chrome headless)
- All 14 steps screenshotted via `/render?path=…&step=N` at 1440×830 — no
  overflow, stage fits, no console/page errors (one benign favicon 404).
- Step 6 cold: 13 MB / 1 req / 6.5 s live; warm: "0 bytes — cache" branch.
- Step 7: CMIP6 80.2 MB confirmed by live HEAD in-page.
- Step 10: 289 KB vs 13 MB bars, both measured in-session.
- Playground: MUR default read (3.8 MB — middle-of-ocean chunk), preset
  switch to GEFS (5 dims of bars), sharded read 38 KB in 10 reqs
  ("sharding kept it small"), session ledger accumulates across steps.
- Sequential Next×13 click-through: no errors; receipt shows the ledger.
- Standalone `file://` demo mode: banner + canned values labelled, steps
  0/5/6/7/10/13 + a canned playground read — no errors.

**Rev 2.1 (post-verify polish):** step-6 narration log now resets to its intro lines on every "read it again" instead of accumulating duplicates. Independent full-pass verification: 13/13 steps, 0 pageerrors, 0 vertical overflow at 1440×830.

**Rev 4.1 (live-review fixes):** step-1 download sim confesses at 10 s ("not really" title + gold joke line). Real-S3 panel progress bar was flex-crushed to 2 px (fixed .dlpanel height + no flex:none on .pbar) — now min-height + flex:none, verified filling at 60 % mid-read. Step-6 datacube redesigned: the whole-store ghost box is constant context at every beat (ask sliver → blue traveled chunk → CMIP6 near-fill), labels fixed position, duplicate beat-0 caption removed.
