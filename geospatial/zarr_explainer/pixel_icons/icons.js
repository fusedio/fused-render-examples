/* ============================================================
   PixelIcons — pixelated / dithered icon library
   for the Zarr explainer (paper-editorial design system).

   Dependency-free. Attach with:
     <script src="pixel_icons/icons.js"></script>
     PixelIcons.draw('zarr', canvasEl, { size: 96, palette: 'paper' });

   Every icon is painted on a small logical grid (16 / 24 / 32 px)
   then blitted up with imageSmoothingEnabled = false, so pixels
   stay crisp at any size and any devicePixelRatio.
   Tone is done with a 4x4 ordered-dither (Bayer) matrix — the same
   checker shading the explainer's world map uses.
   ============================================================ */
(function () {
  "use strict";

  /* ---------- palettes (mirrors explainer.html tokens) ---------- */
  const PALETTES = {
    paper: {
      ink:      "#28231a",
      dim:      "#7b7260",
      line:     "#d6cdb8",
      card:     "#fffdf8",
      paper:    "#f6f2e9",
      gold:     "#b8860b",
      goldSoft: "#e9c869",
      goldDeep: "#7a5807",
      land:     "#e8dcc0",
      landMid:  "#cdbd97",
      landDark: "#a8966b",
      ocean:    "#d7e4ea",
      oceanDeep:"#b9cfd9",
      fetch:    "#1f8fb8",
      fetchSoft:"#9fcfe2",
      cache:    "#2f9e57",
      cacheSoft:"#b5dcc3"
    },
    /* thermal accent — warm check-palette to sanity-check contrast */
    thermal: {
      ink:      "#241c14",
      dim:      "#8a6a52",
      line:     "#dcc9ae",
      card:     "#fdf6ec",
      paper:    "#f6ede0",
      gold:     "#c4571a",
      goldSoft: "#f0a468",
      goldDeep: "#7c3208",
      land:     "#eedcbe",
      landMid:  "#d5b487",
      landDark: "#a97f4e",
      ocean:    "#e3d3be",
      oceanDeep:"#cdb694",
      fetch:    "#b03a12",
      fetchSoft:"#e8b294",
      cache:    "#6f7d2c",
      cacheSoft:"#cdd3a4"
    }
  };

  /* 4x4 Bayer ordered-dither matrix (values 0..15) */
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
  const bayer = (x, y) => BAYER[(y & 3) * 4 + (x & 3)];

  /* ---------- 32x16 land mask, downsampled at build time from
     mock_store/land_mask.json (lat 58°S..86°N, majority filter).
     Row 0 = north. -------------------------------------------- */
  const LAND32 = [
    "00000000000111000000000000000000",
    "00000000000111000000000111100000",
    "00111111000000000111111111111111",
    "00001111011000000111111111110000",
    "00000111110000001111111111110000",
    "00000111100000000001111111100000",
    "00000011000000011111111111100000",
    "00000000000000111111101111000000",
    "00000000011000011111000000000000",
    "00000000011100000111000000000000",
    "00000000011110000111000000000000",
    "00000000001110000110000000011000",
    "00000000001100000110000000111100",
    "00000000011000000000000000001000",
    "00000000000000000000000000000000",
    "00000000000000000000000000000000"
  ];

  /* ---------- tiny painter over a logical-pixel context ---------- */
  function painter(ctx, pal) {
    const p = {
      pal,
      px(x, y, c) { ctx.fillStyle = pal[c] || c; ctx.fillRect(x, y, 1, 1); },
      rect(x, y, w, h, c) { ctx.fillStyle = pal[c] || c; ctx.fillRect(x, y, w, h); },
      frame(x, y, w, h, c) {
        p.rect(x, y, w, 1, c); p.rect(x, y + h - 1, w, 1, c);
        p.rect(x, y, 1, h, c); p.rect(x + w - 1, y, 1, h, c);
      },
      /* ordered dither: paint cA where bayer < d (0..16), else cB (or skip) */
      dither(x, y, w, h, cA, d, cB) {
        for (let yy = y; yy < y + h; yy++)
          for (let xx = x; xx < x + w; xx++) {
            const on = bayer(xx, yy) < d;
            if (on) p.px(xx, yy, cA);
            else if (cB) p.px(xx, yy, cB);
          }
      },
      hline(x, y, w, c) { p.rect(x, y, w, 1, c); },
      vline(x, y, h, c) { p.rect(x, y, 1, h, c); }
    };
    return p;
  }

  /* ---------- ASCII-map painter ----------
     key: char -> color role, or {c, d, c2} for dithered mix */
  function paintMap(p, map, key) {
    for (let y = 0; y < map.length; y++) {
      const row = map[y];
      for (let x = 0; x < row.length; x++) {
        const k = key[row[x]];
        if (!k) continue;
        if (typeof k === "string") { p.px(x, y, k); }
        else {
          const on = bayer(x, y) < k.d;
          if (on) p.px(x, y, k.c);
          else if (k.c2) p.px(x, y, k.c2);
        }
      }
    }
  }

  /* ============================================================
     ICON DEFINITIONS
     each: { grid: N, paint(p, pal) }  — logical square grid NxN
     ============================================================ */
  const ICONS = {};

  /* ---- globe : pixel world map, real continents (32 grid) ---- */
  ICONS.globe = {
    grid: 32,
    paint(p) {
      // ocean field with a soft dithered frame band
      p.rect(0, 7, 32, 18, "ocean");
      p.dither(0, 7, 32, 1, "oceanDeep", 8);
      p.dither(0, 24, 32, 1, "oceanDeep", 8);
      // gentle ocean texture
      p.dither(0, 8, 32, 16, "oceanDeep", 2);
      // land
      for (let y = 0; y < 16; y++)
        for (let x = 0; x < 32; x++)
          if (LAND32[y][x] === "1") p.px(x, y + 8, "landDark");
      // dithered land highlight (sun-side)
      for (let y = 0; y < 16; y++)
        for (let x = 0; x < 32; x++)
          if (LAND32[y][x] === "1" && bayer(x, y) < 5) p.px(x, y + 8, "landMid");
    }
  };

  /* ---- cube : isometric data cube (24 grid) ---- */
  ICONS.cube = {
    grid: 24,
    paint(p) {
      const cx = 12;
      // top diamond: rows 3..13, widest at row 8 (x 1..22)
      for (let y = 3; y <= 13; y++) {
        const hw = (y <= 8 ? (y - 3) * 2 + 1 : (13 - y) * 2 + 1);
        p.rect(cx - hw, y, hw * 2, 1, "land");
        p.px(cx - hw, y, "ink"); p.px(cx + hw - 1, y, "ink");
      }
      p.rect(cx - 1, 3, 2, 1, "ink");
      // left face: cols 1..11, dithered within the face only
      for (let x = 1; x <= 11; x++) {
        const yt = 8 + ((x - 1) >> 1) + 1;
        for (let i = 0; i < 8; i++) {
          const y = yt + i;
          p.px(x, y, bayer(x, y) < 5 ? "landDark" : "landMid");
        }
        p.px(x, yt + 8, "ink");
      }
      // right face: mirrored, darker, dithered within the face only
      for (let x = 12; x <= 22; x++) {
        const m = 22 - x;
        const yt = 8 + (m >> 1) + 1;
        for (let i = 0; i < 8; i++) {
          const y = yt + i;
          p.px(x, y, bayer(x, y) < 2 ? "ink" : "landDark");
        }
        p.px(x, yt + 8, "ink");
      }
      // vertical edges
      p.vline(1, 8, 9, "ink"); p.vline(22, 8, 9, "ink");
      p.vline(11, 13, 9, "ink"); p.px(12, 13, "ink");
      // top-face grid hint (it's data)
      p.dither(cx - 5, 6, 10, 5, "landMid", 3);
    }
  };

  /* ---- zarr : chunked cube — 3x3 grid of blocks, one gold (24 grid) ---- */
  ICONS.zarr = {
    grid: 24,
    paint(p) {
      for (let r = 0; r < 3; r++)
        for (let c = 0; c < 3; c++) {
          const x = 1 + c * 8, y = 1 + r * 8; // 7px blocks, 1px gaps
          const hot = (r === 1 && c === 1);
          p.rect(x, y, 7, 7, hot ? "goldSoft" : "land");
          p.frame(x, y, 7, 7, hot ? "gold" : "landDark");
          p.dither(x + 1, y + 1, 5, 5, hot ? "gold" : "landMid", 4);
        }
      // corner tick: it's one addressable block
      p.px(1 + 8 + 1, 1 + 8 + 1, "goldDeep");
    }
  };

  /* ---- cog : one big raster file with overview pyramid (24 grid) ---- */
  ICONS.cog = {
    grid: 24,
    paint(p) {
      // page body with a clean folded corner (fold x13..16, y1..4)
      p.rect(3, 1, 14, 22, "card");
      p.rect(13, 1, 4, 4, "paper");   // cut the corner
      p.hline(3, 1, 10, "ink");       // top edge up to the fold
      p.vline(3, 1, 22, "ink");       // left
      p.hline(3, 22, 14, "ink");      // bottom
      p.vline(16, 4, 19, "ink");      // right, below the fold
      p.px(13, 1, "ink"); p.px(14, 2, "ink"); p.px(15, 3, "ink"); // diagonal
      p.hline(13, 4, 4, "ink");       // fold underside
      p.px(13, 2, "line"); p.px(13, 3, "line"); p.px(14, 3, "line"); // flap
      // overview pyramid: 3 rasters, small on top
      p.dither(9, 6, 3, 2, "fetch", 9, "fetchSoft");
      p.frame(8, 5, 5, 4, "ink");
      p.dither(8, 11, 6, 3, "fetch", 9, "fetchSoft");
      p.frame(7, 10, 8, 5, "ink");
      p.dither(6, 17, 9, 3, "fetch", 9, "fetchSoft");
      p.frame(5, 16, 11, 5, "ink");
    }
  };

  /* ---- netcdf : classic container box with layers (24 grid) ---- */
  ICONS.netcdf = {
    grid: 24,
    paint(p) {
      // lid (slightly lifted)
      p.rect(4, 2, 16, 3, "landMid");
      p.frame(4, 2, 16, 3, "ink");
      // box body
      p.rect(3, 6, 18, 16, "land");
      p.frame(3, 6, 18, 16, "ink");
      // three stacked layers inside
      const shades = ["landMid", "landDark", "landMid"];
      for (let i = 0; i < 3; i++) {
        const y = 8 + i * 5;
        p.dither(5, y, 14, 4, shades[i], 9, "land");
        p.frame(5, y, 14, 4, "ink");
      }
      // little "n" dimension ticks on the lid
      p.px(7, 3, "ink"); p.px(11, 3, "ink"); p.px(15, 3, "ink");
    }
  };

  /* ---- parquet : columnar bars (24 grid) ---- */
  ICONS.parquet = {
    grid: 24,
    paint(p) {
      const bars = [
        { x: 3,  h: 13, c: "fetch" },
        { x: 8,  h: 18, c: "gold" },
        { x: 13, h: 9,  c: "fetch" },
        { x: 18, h: 15, c: "fetch" }
      ];
      for (const b of bars) {
        const y = 21 - b.h;
        p.dither(b.x, y, 4, b.h, b.c, 9, b.c === "gold" ? "goldSoft" : "fetchSoft");
        p.frame(b.x, y, 4, b.h, "ink");
        // row-group segment lines
        for (let yy = y + 4; yy < 20; yy += 4) p.hline(b.x, yy, 4, "ink");
      }
      p.hline(1, 22, 22, "ink"); // baseline
    }
  };

  /* ---- chunk : single tile (16 grid) ---- */
  ICONS.chunk = {
    grid: 16,
    paint(p) {
      p.rect(2, 2, 12, 12, "land");
      p.dither(3, 3, 10, 10, "landMid", 6);
      p.frame(2, 2, 12, 12, "ink");
      // gold index tag in the corner: chunk "0.0"
      p.rect(3, 3, 4, 4, "goldSoft");
      p.frame(3, 3, 4, 4, "gold");
    }
  };

  /* ---- chunks_grid : 4x4 grid of tiles, one fetched (24 grid) ---- */
  ICONS.chunks_grid = {
    grid: 24,
    paint(p) {
      for (let r = 0; r < 4; r++)
        for (let c = 0; c < 4; c++) {
          const x = 0 + c * 6, y = 0 + r * 6; // 5px tiles + 1px gap
          const fetched = (r === 1 && c === 2);
          const cached = (r === 2 && c === 1);
          const body = fetched ? "fetch" : cached ? "cache" : "land";
          p.rect(x, y, 5, 5, body);
          if (!fetched && !cached) p.dither(x + 1, y + 1, 3, 3, "landMid", 5);
          p.frame(x, y, 5, 5, fetched ? "ink" : cached ? "ink" : "landDark");
        }
    }
  };

  /* ---- shard : many tiles packed into one file strip (24 grid) ---- */
  ICONS.shard = {
    grid: 24,
    paint(p) {
      // the one file (outer capsule)
      p.rect(1, 6, 22, 12, "card");
      p.frame(1, 6, 22, 12, "ink");
      // packed chunk tiles inside
      for (let i = 0; i < 4; i++) {
        const x = 3 + i * 4;
        p.rect(x, 8, 3, 8, "land");
        p.dither(x, 8, 3, 8, "landMid", 6);
        p.frame(x, 8, 3, 8, "landDark");
      }
      // gold index footer block at the end (shard index)
      p.rect(19, 8, 3, 8, "goldSoft");
      p.dither(19, 8, 3, 8, "gold", 6);
      p.frame(19, 8, 3, 8, "gold");
    }
  };

  /* ---- folder (16 grid) ---- */
  ICONS.folder = {
    grid: 16,
    paint(p) {
      paintMap(p, [
        "................",
        ".########.......",
        ".#ffffff#.......",
        ".##############.",
        ".#ffffffffffff#.",
        ".#ffffffffffff#.",
        ".#ffffffffffff#.",
        ".#ffffffffffff#.",
        ".#ffffffffffff#.",
        ".#dddddddddddd#.",
        ".#dddddddddddd#.",
        ".#dddddddddddd#.",
        ".##############.",
        "................",
        "................",
        "................"
      ], {
        "#": "ink",
        "f": "land",
        "d": { c: "landMid", d: 8, c2: "land" }
      });
    }
  };

  /* ---- file_json : metadata file with braces (16 grid) ---- */
  ICONS.file_json = {
    grid: 16,
    paint(p) {
      // page + fold + braces
      p.rect(2, 1, 12, 13, "card");
      p.frame(2, 1, 12, 13, "ink");
      // folded corner
      p.rect(10, 1, 4, 4, "paper");
      p.px(10, 1, "ink"); p.px(11, 2, "ink"); p.px(12, 3, "ink");
      p.hline(10, 4, 4, "ink");
      p.vline(13, 4, 10, "ink");
      p.px(10, 2, "line"); p.px(10, 3, "line"); p.px(11, 3, "line"); // flap
      // { } braces in ink
      p.vline(5, 6, 4, "ink"); p.px(6, 5, "ink"); p.px(6, 10, "ink"); p.px(4, 7, "ink");
      p.vline(10, 6, 4, "ink"); p.px(9, 5, "ink"); p.px(9, 10, "ink"); p.px(11, 7, "ink");
      // gold key:value dot
      p.rect(7, 7, 2, 2, "gold");
    }
  };

  /* ---- download : arrow into tray (16 grid) ---- */
  ICONS.download = {
    grid: 16,
    paint(p) {
      // arrow shaft
      p.rect(7, 1, 2, 6, "fetch");
      // arrow head
      p.rect(4, 6, 8, 1, "fetch");
      p.rect(5, 7, 6, 1, "fetch");
      p.rect(6, 8, 4, 1, "fetch");
      p.rect(7, 9, 2, 1, "fetch");
      // dither trail (bytes streaming)
      p.dither(7, 1, 2, 5, "fetchSoft", 5);
      // tray
      p.vline(2, 10, 4, "ink"); p.vline(13, 10, 4, "ink");
      p.hline(2, 13, 12, "ink");
      p.dither(3, 12, 10, 1, "line", 8);
    }
  };

  /* ---- receipt : till receipt (24 grid) ---- */
  ICONS.receipt = {
    grid: 24,
    paint(p) {
      p.rect(6, 1, 12, 20, "card");
      p.vline(6, 1, 20, "ink"); p.vline(17, 1, 20, "ink");
      p.hline(6, 1, 12, "ink");
      // zigzag torn bottom
      for (let x = 6; x < 18; x += 2) {
        p.px(x, 21, "ink"); p.px(x + 1, 20, "ink");
        p.px(x + 1, 21, "paper");
      }
      // printed lines
      p.hline(8, 4, 8, "dim");
      p.hline(8, 7, 5, "dim"); p.hline(14, 7, 2, "landDark");
      p.hline(8, 10, 6, "dim"); p.hline(15, 10, 1, "landDark");
      p.hline(8, 13, 4, "dim"); p.hline(14, 13, 2, "landDark");
      // divider + gold total
      p.dither(8, 15, 8, 1, "dim", 8);
      p.hline(8, 17, 3, "ink"); p.rect(13, 16, 3, 2, "goldSoft");
      p.frame(13, 16, 3, 2, "gold");
    }
  };

  /* ---- clock_wait (16 grid) ---- */
  ICONS.clock_wait = {
    grid: 16,
    paint(p) {
      p.rect(3, 3, 10, 10, "card");
      // circle-ish rim
      p.hline(5, 2, 6, "ink"); p.hline(5, 13, 6, "ink");
      p.vline(2, 5, 6, "ink"); p.vline(13, 5, 6, "ink");
      p.px(3, 3, "ink"); p.px(4, 3, "ink"); p.px(3, 4, "ink");
      p.px(11, 3, "ink"); p.px(12, 3, "ink"); p.px(12, 4, "ink");
      p.px(3, 11, "ink"); p.px(3, 12, "ink"); p.px(4, 12, "ink");
      p.px(12, 11, "ink"); p.px(11, 12, "ink"); p.px(12, 12, "ink");
      p.rect(4, 4, 8, 8, "card"); p.dither(4, 4, 8, 8, "paper", 3);
      // hands: minute up, hour right (waiting…)
      p.vline(7, 4, 4, "ink");
      p.hline(7, 8, 3, "ink"); p.px(7, 8, "gold");
    }
  };

  /* ---- bolt_fast (16 grid) ---- */
  ICONS.bolt_fast = {
    grid: 16,
    paint(p) {
      paintMap(p, [
        "................",
        "........KK......",
        ".......KGG......",
        "......KGGG......",
        ".....KGGG.......",
        "....KGGG........",
        "...KGGGGGKK.....",
        "...KKKKGGGK.....",
        "......KGGK......",
        ".....KGGK.......",
        "....KGGK........",
        "....KGK.........",
        "...KGK..........",
        "...KK...........",
        "................",
        "................"
      ], { "K": "ink", "G": "gold" });
      // speed ticks trailing on the left
      p.hline(1, 9, 2, "dim");
      p.hline(1, 11, 2, "dim");
    }
  };

  /* ---- link_out : learn-more arrow (16 grid) ---- */
  ICONS.link_out = {
    grid: 16,
    paint(p) {
      // box
      p.frame(2, 5, 9, 9, "ink");
      p.rect(3, 6, 7, 7, "card");
      p.dither(3, 6, 7, 7, "paper", 3);
      // arrow out to top-right
      p.px(8, 8, "gold"); p.px(9, 7, "gold"); p.px(10, 6, "gold");
      p.px(11, 5, "gold"); p.px(12, 4, "gold");
      // arrow head
      p.hline(9, 2, 5, "gold");
      p.vline(13, 2, 5, "gold");
      p.px(12, 3, "gold");
    }
  };

  /* ---- bulb : idea (16 grid) ---- */
  ICONS.bulb = {
    grid: 16,
    paint(p) {
      paintMap(p, [
        "................",
        ".....#####......",
        "....#ggggg#.....",
        "...#ggggggg#....",
        "...#ggGgggg#....",
        "...#gGggggg#....",
        "...#ggggggg#....",
        "....#ggggg#.....",
        ".....#ggg#......",
        ".....#####......",
        ".....#KKK#......",
        ".....#KKK#......",
        "......###.......",
        "................",
        "................",
        "................"
      ], {
        "#": "ink",
        "g": { c: "goldSoft", d: 11, c2: "gold" },
        "G": "card",
        "K": { c: "dim", d: 8, c2: "line" }
      });
      // rays
      p.px(1, 4, "gold"); p.px(13, 4, "gold");
      p.px(2, 1, "gold"); p.px(12, 1, "gold");
      p.px(7, 0, "gold");
    }
  };

  /* ---- act badges : aliases with palette shifts ---- */
  function shifted(base, shift) {
    return {
      grid: ICONS[base].grid,
      paint(p) {
        const pal2 = Object.assign({}, p.pal, shift(p.pal));
        ICONS[base].paint(painterProxy(p, pal2));
      }
    };
  }
  function painterProxy(p, pal2) {
    const q = Object.assign({}, p);
    q.pal = pal2;
    q.px = (x, y, c) => p.px(x, y, pal2[c] || c);
    q.rect = (x, y, w, h, c) => p.rect(x, y, w, h, pal2[c] || c);
    q.frame = (x, y, w, h, c) => p.frame(x, y, w, h, pal2[c] || c);
    q.hline = (x, y, w, c) => p.hline(x, y, w, pal2[c] || c);
    q.vline = (x, y, h, c) => p.vline(x, y, h, pal2[c] || c);
    q.dither = (x, y, w, h, cA, d, cB) =>
      p.dither(x, y, w, h, pal2[cA] || cA, d, cB ? (pal2[cB] || cB) : cB);
    return q;
  }
  ICONS.act_data = shifted("cube", (pal) => ({
    land: pal.goldSoft, landMid: pal.gold, landDark: pal.goldDeep
  }));
  ICONS.act_idea = shifted("bulb", () => ({}));
  ICONS.act_files = shifted("folder", (pal) => ({
    land: pal.goldSoft, landMid: pal.gold
  }));
  ICONS.act_world = shifted("globe", (pal) => ({
    ocean: pal.paper, oceanDeep: pal.line,
    landDark: pal.gold, landMid: pal.goldSoft
  }));

  /* ============================================================
     public API
     ============================================================ */
  function resolvePalette(pal) {
    if (!pal) return PALETTES.paper;
    if (typeof pal === "string") return PALETTES[pal] || PALETTES.paper;
    return Object.assign({}, PALETTES.paper, pal);
  }

  function draw(name, canvas, opts) {
    opts = opts || {};
    const def = ICONS[name];
    if (!def) throw new Error("PixelIcons: unknown icon '" + name + "'");
    const size = opts.size || 48;
    const pal = resolvePalette(opts.palette);

    // paint on the logical grid
    const off = document.createElement("canvas");
    off.width = def.grid; off.height = def.grid;
    const octx = off.getContext("2d");
    def.paint(painter(octx, pal), pal);

    // blit up, crisp
    const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  const names = [
    "zarr", "cog", "netcdf", "parquet",
    "globe", "cube", "chunk", "chunks_grid", "shard",
    "folder", "file_json", "download", "receipt",
    "clock_wait", "bolt_fast", "link_out", "bulb",
    "act_data", "act_idea", "act_files", "act_world"
  ];

  window.PixelIcons = { draw, names, palettes: PALETTES, icons: ICONS };
})();
