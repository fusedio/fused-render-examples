// Unit tests for the pure map/date helpers in index.html.
//
// The app is a single self-contained HTML file (the fused daemon does not serve
// sibling assets), so the helpers live inline. This test extracts the block
// between the "== BEGIN pure-helpers ==" / "== END pure-helpers ==" markers and
// evaluates it in isolation -- so there is one source of truth, not a copy.
//
//   node --test test_maplib.mjs
//
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "index.html"), "utf8");

// skip the rest of the BEGIN marker line, capture whole lines up to the END line
const m = html.match(/BEGIN pure-helpers ==[^\n]*\n([\s\S]*?)\n[^\n]*END pure-helpers/);
assert.ok(m, "pure-helpers marker block not found in index.html");
// the block is a run of const/function declarations with no external deps;
// evaluate it and hand back the names the tests need
const lib = new Function(m[1] + `
  return { MAXLAT, R2D, clampLat, clampLon, validBox, crosses, splitAM, lonExtent,
           projY, unprojY,
           GOOGLE_TILE, tileZoom, tileGrid, intervalFromDates, datesFromInterval };
`)();

const approx = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

test("projY is 0 at the equator and clamps at the mercator limit", () => {
  assert.ok(approx(lib.projY(0), 0));
  assert.ok(approx(lib.projY(90), -180, 1e-4));   // clamped to +MAXLAT -> top edge
  assert.ok(approx(lib.projY(-90), 180, 1e-4));   // clamped to -MAXLAT -> bottom edge
});

test("projY / unprojY round-trip", () => {
  for (const lat of [-80, -45, -1, 0, 12.5, 35, 60, 82]) {
    assert.ok(approx(lib.unprojY(lib.projY(lat)), lat, 1e-6), `lat ${lat}`);
  }
});

test("projY decreases as latitude increases (north is up = negative y)", () => {
  assert.ok(lib.projY(10) < lib.projY(0));
  assert.ok(lib.projY(60) < lib.projY(10));
});

test("clampLat / clampLon / validBox", () => {
  assert.equal(lib.clampLat(100), lib.MAXLAT);
  assert.equal(lib.clampLat(-100), -lib.MAXLAT);
  assert.equal(lib.clampLon(200), 180);
  assert.equal(lib.clampLon(-200), -180);
  assert.equal(lib.validBox([1, 2, 3, 4]), true);
  assert.equal(lib.validBox([1, 2, 3]), false);
  assert.equal(lib.validBox([1, 2, NaN, 4]), false);
});

test("crosses detects an antimeridian bbox (west > east)", () => {
  assert.equal(lib.crosses([172.6, -20, -168.4, -10]), true);
  assert.equal(lib.crosses([-10, 0, 10, 5]), false);       // ordinary bbox
  assert.equal(lib.crosses([1, 2, 3]), false);             // not a bbox
});

test("splitAM leaves an ordinary bbox alone and splits a crossing one at 180", () => {
  assert.deepEqual(lib.splitAM([-10, 0, 10, 5]), [[-10, 0, 10, 5]]);
  assert.deepEqual(lib.splitAM([172.6, -20, -168.4, -10]),
    [[172.6, -20, 180, -10], [-180, -20, -168.4, -10]]);
});

test("lonExtent unwraps a crossing bbox to center on the data, not the world", () => {
  const ord = lib.lonExtent([[-10, 0, 10, 5]]);
  assert.ok(approx(ord.clon, 0) && approx(ord.spanX, 20));
  const cr = lib.lonExtent([[172.6, -20, -168.4, -10]]);  // east carried to 191.6
  assert.ok(approx(cr.spanX, 19, 1e-6), `spanX ${cr.spanX}`);
  assert.ok(approx(cr.clon, -177.9, 1e-6), `clon ${cr.clon}`);   // 182.1 wrapped
  assert.equal(lib.lonExtent([]), null);
});

test("GOOGLE_TILE builds the QGIS-style Google URL with a rotating host", () => {
  assert.equal(lib.GOOGLE_TILE(3, 5, 4), "https://mt0.google.com/vt/lyrs=y&x=3&y=5&z=4"); // (3+5)%4=0
  assert.equal(lib.GOOGLE_TILE(1, 0, 2), "https://mt1.google.com/vt/lyrs=y&x=1&y=0&z=2");
});

test("tileZoom picks a whole-world zoom near z0-z1 and a finer one when zoomed in", () => {
  assert.ok(lib.tileZoom(512, 360) <= 1);          // whole world in ~512px
  assert.ok(lib.tileZoom(512, 5) > lib.tileZoom(512, 360));  // a 5-degree window is much finer
  assert.ok(lib.tileZoom(512, 1e-6) <= 20);        // clamped
});

test("tileGrid covers the viewBox and clamps rows to the world", () => {
  // whole world at z1: 2x2 tiles, rows clamped to [0,1]
  const g = lib.tileGrid(-180, -180, 360, 360, 1);
  assert.equal(g.n, 2);
  assert.equal(g.tsz, 180);
  assert.equal(g.txmin, 0);
  assert.equal(g.txmax, 2);        // right edge lands on the wrap column (handled by caller)
  assert.equal(g.tymin, 0);
  assert.equal(g.tymax, 1);        // not 2 -- clamped to n-1
});

test("intervalFromDates builds a STAC interval, open-ended where a side is blank", () => {
  assert.equal(lib.intervalFromDates("", ""), "");
  assert.equal(lib.intervalFromDates("2021-01-01", "2021-12-31"),
    "2021-01-01T00:00:00Z/2021-12-31T23:59:59Z");
  assert.equal(lib.intervalFromDates("2021-06-01", ""), "2021-06-01T00:00:00Z/..");
  assert.equal(lib.intervalFromDates("", "2021-06-01"), "../2021-06-01T23:59:59Z");
});

test("datesFromInterval is the inverse for date-input values", () => {
  assert.deepEqual(lib.datesFromInterval("2021-01-01T00:00:00Z/2021-12-31T23:59:59Z"),
    { start: "2021-01-01", end: "2021-12-31" });
  assert.deepEqual(lib.datesFromInterval("2021-06-01T00:00:00Z/.."), { start: "2021-06-01", end: "" });
  assert.deepEqual(lib.datesFromInterval("../2021-06-01T23:59:59Z"), { start: "", end: "2021-06-01" });
  assert.deepEqual(lib.datesFromInterval(""), { start: "", end: "" });
});
