"""Support ops for the comfy view: directory setup, input-image listing and
upload, PNG workflow-metadata extraction, workflow file listing, and an
SQLite event log (comfy.db) recording every user action for history/audit.
"""

import base64
import json
import os
import re
import sqlite3
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import os, sys
    __file__ = os.path.join(sys.path[0], "api.py")

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "input")
OUTPUT = os.path.join(HERE, "output")
CACHE = os.path.join(HERE, ".cache")
WORKFLOWS = os.path.join(HERE, "workflows")
DB = os.path.join(HERE, "comfy.db")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,
             kind TEXT NOT NULL,
             detail TEXT NOT NULL DEFAULT ''
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS assets (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,
             node_type TEXT NOT NULL,
             kind TEXT NOT NULL,
             path TEXT NOT NULL,
             workflow TEXT NOT NULL DEFAULT ''
           )"""
    )
    return con


def _log(kind, detail):
    con = _db()
    with con:
        con.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), str(kind), json.dumps(detail) if not isinstance(detail, str) else detail),
        )
    con.close()


def _safe_name(name):
    name = os.path.basename(str(name)).strip()
    name = re.sub(r"[^\w.\- ()]+", "_", name)
    if not name or name.startswith("."):
        raise ValueError(f"invalid file name {name!r}")
    return name


def _list_inputs():
    files = []
    if os.path.isdir(INPUT):
        for n in sorted(os.listdir(INPUT)):
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS:
                files.append(n)
    return files


def _make_samples():
    """Generate a couple of demo inputs so LoadImage works out of the box."""
    import math

    import numpy as np
    from PIL import Image, ImageDraw

    xs, ys = np.meshgrid(np.linspace(0, 1, 512), np.linspace(0, 1, 512))
    r = np.sin(xs * 6.28 * 1.5) * 0.5 + 0.5
    g = np.sin(ys * 6.28 * 1.0 + 1.0) * 0.5 + 0.5
    b = np.sin((xs + ys) * 6.28 * 0.75 + 2.0) * 0.5 + 0.5
    arr = (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(os.path.join(INPUT, "sample_gradient.png"))

    img = Image.new("RGB", (512, 512), (24, 26, 32))
    d = ImageDraw.Draw(img)
    colors = [(255, 99, 71), (100, 181, 246), (129, 199, 132), (255, 213, 0), (179, 157, 219)]
    for i, c in enumerate(colors):
        cx = 90 + i * 85
        cy = 256 + int(120 * math.sin(i * 1.3))
        d.ellipse((cx - 60, cy - 60, cx + 60, cy + 60), fill=c)
    d.rectangle((40, 40, 200, 120), outline=(230, 230, 230), width=4)
    img.save(os.path.join(INPUT, "sample_shapes.png"))


def op_setup():
    for d in (INPUT, OUTPUT, CACHE, WORKFLOWS):
        os.makedirs(d, exist_ok=True)
    if not _list_inputs():
        try:
            _make_samples()
        except Exception as e:
            print(f"sample generation failed: {e}")
    _db().close()
    return {
        "root": HERE,
        "inputs": _list_inputs(),
        "workflows": op_list_workflows()["workflows"],
    }


def op_upload(name, data_b64):
    name = _safe_name(name)
    os.makedirs(INPUT, exist_ok=True)
    raw = base64.b64decode(data_b64)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("upload too large (max 64 MB)")
    path = os.path.join(INPUT, name)
    with open(path, "wb") as f:
        f.write(raw)
    _log("upload", {"name": name, "bytes": len(raw)})
    return {"name": name, "inputs": _list_inputs()}


def op_png_meta(path):
    """Extract embedded workflow JSON from a PNG's tEXt chunks."""
    from PIL import Image

    im = Image.open(path)
    info = im.info or {}
    for key in ("workflow", "prompt", "Workflow"):
        v = info.get(key)
        if v:
            return {"key": key, "workflow": v if isinstance(v, str) else str(v)}
    return {"key": None, "workflow": None}


def op_list_workflows():
    out = []
    if os.path.isdir(WORKFLOWS):
        for n in sorted(os.listdir(WORKFLOWS)):
            if n.lower().endswith(".json"):
                p = os.path.join(WORKFLOWS, n)
                out.append({"name": n, "path": p, "mtime": os.path.getmtime(p)})
    return {"workflows": out, "dir": WORKFLOWS}


def op_history(limit, kind):
    con = _db()
    q = "SELECT id, ts, kind, detail FROM events"
    args = []
    if kind:
        q += " WHERE kind = ?"
        args.append(kind)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(500, int(limit or 100))))
    rows = con.execute(q, args).fetchall()
    con.close()
    events = []
    for rid, ts, k, detail in rows:
        try:
            detail = json.loads(detail)
        except Exception:
            pass
        events.append({"id": rid, "ts": ts, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "kind": k, "detail": detail})
    return {"events": events}


SECRETS = os.path.join(HERE, "secrets.json")
KNOWN_KEYS = [
    {"name": "HF_TOKEN", "hint": "Hugging Face — huggingface.co/settings/tokens (text-to-image, captioning, VQA, image edit)"},
    {"name": "ARK_API_KEY", "hint": "BytePlus ModelArk — Seedance video generation"},
    {"name": "FAL_KEY", "hint": "fal.ai — fal.ai/dashboard/keys (LTX-Video generation)"},
]


def _read_secrets():
    try:
        with open(SECRETS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def op_set_secret(name, value):
    name = re.sub(r"[^\w]+", "_", str(name)).strip("_")
    if not name:
        raise ValueError("invalid key name")
    s = _read_secrets()
    if value:
        s[name] = value
    else:
        s.pop(name, None)
    with open(SECRETS, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    try:
        os.chmod(SECRETS, 0o600)
    except Exception:
        pass
    _log("secret_set" if value else "secret_deleted", {"name": name})  # never the value
    return op_list_secrets()


def op_list_secrets():
    s = _read_secrets()
    out = []
    names = {k["name"] for k in KNOWN_KEYS}
    for k in KNOWN_KEYS:
        v = s.get(k["name"]) or os.environ.get(k["name"])
        out.append({
            "name": k["name"], "hint": k["hint"],
            "set": bool(v),
            "masked": (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("set" if v else ""),
            "from_env": k["name"] not in s and bool(os.environ.get(k["name"])),
        })
    for name, v in s.items():
        if name not in names:
            out.append({"name": name, "hint": "custom", "set": True,
                        "masked": (v[:4] + "…" + v[-4:]) if len(v) > 10 else "set", "from_env": False})
    return {"keys": out}


def op_record_asset(path, kind, node_type, workflow):
    if not path or not os.path.isfile(path):
        raise ValueError("asset file not found")
    con = _db()
    with con:
        # same path re-generated (e.g. cache-identical rerun) → refresh, don't duplicate
        con.execute("DELETE FROM assets WHERE path = ?", (path,))
        con.execute(
            "INSERT INTO assets (ts, node_type, kind, path, workflow) VALUES (?, ?, ?, ?, ?)",
            (time.time(), node_type or "?", kind or "image", path, workflow or ""),
        )
    con.close()
    return {"ok": True}


def op_list_assets(limit):
    con = _db()
    rows = con.execute(
        "SELECT id, ts, node_type, kind, path FROM assets ORDER BY id DESC LIMIT ?",
        (max(1, min(300, int(limit or 100))),),
    ).fetchall()
    con.close()
    out = []
    for rid, ts, node_type, kind, path in rows:
        exists = os.path.isfile(path)
        out.append({
            "id": rid, "ts": ts,
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
            "node_type": node_type, "kind": kind, "path": path,
            "exists": exists, "in_output": path.startswith(OUTPUT + os.sep),
            "name": os.path.basename(path),
        })
    return {"assets": out}


def op_get_asset_workflow(asset_id):
    con = _db()
    row = con.execute("SELECT workflow FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
    con.close()
    if not row or not row[0]:
        raise ValueError("no workflow stored for this asset")
    return {"workflow": row[0]}


def op_save_asset(asset_id):
    import shutil

    con = _db()
    row = con.execute("SELECT path, kind FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
    con.close()
    if not row:
        raise ValueError("asset not found")
    src, kind = row
    if not os.path.isfile(src):
        raise ValueError("asset file no longer exists (cache cleared?)")
    os.makedirs(OUTPUT, exist_ok=True)
    ext = os.path.splitext(src)[1] or (".png" if kind == "image" else ".bin")
    n = 1
    while True:
        dest = os.path.join(OUTPUT, f"asset_{time.strftime('%Y%m%d')}_{n:04d}{ext}")
        if not os.path.exists(dest):
            break
        n += 1
    shutil.copyfile(src, dest)
    _log("asset_saved", {"src": src, "dest": dest})
    con = _db()
    with con:
        con.execute("UPDATE assets SET path = ? WHERE id = ?", (dest, int(asset_id)))
    con.close()
    return {"path": dest}


def op_delete_asset(asset_id):
    con = _db()
    with con:
        con.execute("DELETE FROM assets WHERE id = ?", (int(asset_id),))
    con.close()
    return {"ok": True}


def op_clear_history():
    con = _db()
    with con:
        con.execute("DELETE FROM events")
    con.close()
    return {"ok": True}


# Bare main() — NOT @fused.udf — so the same file runs under both engines:
# the built-in executor calls main(**params) directly (a udf wrapper would
# hang trying to authenticate), and the fused engine's compat bridge binds
# params by annotation and calls it the same way.
def main(
    op: str,
    name: str = "",
    data_b64: str = "",
    path: str = "",
    kind: str = "",
    detail: str = "",
    limit: int = 100,
    value: str = "",
    asset_id: int = 0,
    node_type: str = "",
    workflow: str = "",
):
    if op == "set_secret":
        return op_set_secret(name, value)
    if op == "list_secrets":
        return op_list_secrets()
    if op == "record_asset":
        return op_record_asset(path, kind, node_type, workflow)
    if op == "list_assets":
        return op_list_assets(limit)
    if op == "get_asset_workflow":
        return op_get_asset_workflow(asset_id)
    if op == "save_asset":
        return op_save_asset(asset_id)
    if op == "delete_asset":
        return op_delete_asset(asset_id)
    if op == "setup":
        return op_setup()
    if op == "list_inputs":
        return {"inputs": _list_inputs()}
    if op == "upload":
        return op_upload(name, data_b64)
    if op == "png_meta":
        return op_png_meta(path)
    if op == "list_workflows":
        return op_list_workflows()
    if op == "log":
        _log(kind or "event", detail)
        return {"ok": True}
    if op == "history":
        return op_history(limit, kind)
    if op == "clear_history":
        return op_clear_history()
    raise ValueError(f"unknown op {op!r}")
