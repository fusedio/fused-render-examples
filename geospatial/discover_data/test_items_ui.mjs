// Tests for the pure item-panel helpers in index.html (itemThumb, noOpenNote,
// blockedReason). The app is one self-contained HTML file, so the helpers live
// inline; this extracts the block between the "== BEGIN item-panel-helpers ==" /
// "== END item-panel-helpers ==" markers and evaluates it in isolation -- one
// source of truth, no copy. (The per-asset reason text is decided server-side in
// items.py and tested in test_items.py.)
//
//   node --test test_items_ui.mjs
//
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "index.html"), "utf8");

const m = html.match(/BEGIN item-panel-helpers[^\n]*\n([\s\S]*?)\n[^\n]*END item-panel-helpers/);
assert.ok(m, "item-panel-helpers marker block not found in index.html");
const fns = new Function(m[1] + "\nreturn { itemThumb, noOpenNote, blockedReason };")();

// A VEDA-style item: a private s3:// COG plus a titiler PNG preview (role
// "overview") -- nothing the map can open, but there is a picture to show.
const vedaItem = {
  id: "TX_flood_MAXAR4",
  assets: [
    { key: "cog_default", type: "image/tiff", roles: ["data"],
      href: "s3://veda-data-store/tx-flood-maxar/x.tif", auth: "",
      reason: "not fetchable over HTTP (s3://) — the catalog keeps this one private" },
    { key: "rendered_preview_dashboard", type: "image/png", roles: ["overview"],
      href: "https://openveda.cloud/api/raster/.../preview.png", auth: "none",
      reason: "the map template can't read this format" },
  ],
};

test("blockedReason returns the server-provided reason verbatim", () => {
  assert.equal(fns.blockedReason(vedaItem.assets[0]), vedaItem.assets[0].reason);
  assert.equal(fns.blockedReason({ reason: "" }), "");
  assert.equal(fns.blockedReason({}), "");
});

test("noOpenNote flags private off-HTTP data without claiming a tile API exists", () => {
  const note = fns.noOpenNote([vedaItem]);
  assert.match(note, /private/);
  assert.doesNotMatch(note, /tile API/);
});

test("noOpenNote lists the reason when nothing is off-HTTP", () => {
  const item = { assets: [{ auth: "none", reason: "the map template can't read this format" }] };
  const note = fns.noOpenNote([item]);
  assert.match(note, /can't read this format/);
  assert.doesNotMatch(note, /private/);
});

test("noOpenNote joins multiple distinct reasons (the Deltares download + unreadable case)", () => {
  const item = { assets: [
    { auth: "azure-sas", reason: "NetCDF/HDF can't be streamed into the map — open it by downloading the whole file (↓), which may be large" },
    { auth: "none", reason: "the map template can't read this format" },
  ] };
  const note = fns.noOpenNote([item]);
  assert.match(note, /downloading the whole file/);
  assert.match(note, /can't read this format/);
  assert.match(note, /; /);   // the two reasons are joined, not collapsed into one
});

test("itemThumb surfaces a titiler PNG preview (role overview), not the COG", () => {
  const th = fns.itemThumb(vedaItem);
  assert.ok(th);
  assert.equal(th.key, "rendered_preview_dashboard");
});

test("itemThumb accepts a parameterized image media type", () => {
  const item = { assets: [{ key: "thumbnail", type: "image/png; charset=binary",
    roles: ["thumbnail"], href: "https://x/y.png" }] };
  assert.ok(fns.itemThumb(item));
});

test("itemThumb never returns an image/tiff (a COG would render as a broken img)", () => {
  const item = { assets: [{ key: "thumbnail", type: "image/tiff",
    roles: ["thumbnail"], href: "https://x/y.tif" }] };
  assert.equal(fns.itemThumb(item), null);
});
