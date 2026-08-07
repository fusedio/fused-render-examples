"""Node executor for the comfy view. One call = one node.

Receives the node type, its widget values and resolved input values, executes
the operation with PIL, and returns output values. Images travel between nodes
as {"__image__": "<abs png path>", "w": int, "h": int} refs pointing into
.cache/; results are memoized on disk by the caller-computed signature.
"""

import json
import os
import random
import re
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import os, sys
    __file__ = os.path.join(sys.path[0], "engine.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
INPUT = os.path.join(HERE, "input")
OUTPUT = os.path.join(HERE, "output")
MODELS = os.path.join(HERE, ".models")
SECRETS = os.path.join(HERE, "secrets.json")


def _secret(name):
    try:
        with open(SECRETS, "r", encoding="utf-8") as f:
            v = json.load(f).get(name)
            if v:
                return v
    except Exception:
        pass
    return os.environ.get(name)


def _require_secret(name, hint):
    v = _secret(name)
    if not v:
        raise RuntimeError(
            f"missing API key {name!r} — set it via the 🔑 Keys dialog in the top bar ({hint})"
        )
    return v


def _resolve_secret(w, name, hint):
    """Prefer a key typed directly into the node's `api_key` widget; otherwise
    fall back to secrets.json / env via the 🔑 Keys dialog."""
    inline = w.get("api_key")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    return _require_secret(name, hint)


def _http(url, data=None, headers=None, timeout=25, method=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} from {url.split('/')[2]}: {body}") from None


def _download_file(url, dest, timeout=300):
    """Download to dest atomically (tmp + rename) so partial files never persist."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"downloading {os.path.basename(dest)}: {total >> 20} MB")
    os.replace(tmp, dest)
    return dest


def _img_ref(path, img):
    return {"__image__": path, "w": img.width, "h": img.height}


def _load(ref, mode="RGB"):
    from PIL import Image

    if not isinstance(ref, dict) or "__image__" not in ref:
        raise ValueError(f"expected an IMAGE value, got {type(ref).__name__}")
    return Image.open(ref["__image__"]).convert(mode)


def _save_cache(img, sig, slot):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{sig}_{slot}.png")
    img.save(path)
    return _img_ref(path, img)


def _clamp_int(v, lo, hi):
    return max(lo, min(hi, int(round(float(v)))))


def _parse_color(c, default=(0, 0, 0)):
    if isinstance(c, str):
        c = c.strip()
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", c)
        if m:
            h = m.group(1)
            return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    return default


def _font(size):
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- operations
# Each op: fn(w, ins, ctx) -> (outputs_list, ui_dict_or_None)


def op_load_image(w, ins, ctx):
    from PIL import Image

    name = os.path.basename(str(w.get("image", "")))
    path = os.path.join(INPUT, name)
    if not name or not os.path.isfile(path):
        raise FileNotFoundError(
            f"input image {name!r} not found — upload one via the node's button"
        )
    im = Image.open(path)
    rgba = im.convert("RGBA")
    rgb = rgba.convert("RGB")
    mask = rgba.getchannel("A")
    return [_save_cache(rgb, ctx["sig"], 0), _save_cache(mask, ctx["sig"], 1)], None


def op_empty_image(w, ins, ctx):
    from PIL import Image

    wd = _clamp_int(w.get("width", 512), 1, 8192)
    ht = _clamp_int(w.get("height", 512), 1, 8192)
    img = Image.new("RGB", (wd, ht), _parse_color(w.get("color", "#000000")))
    return [_save_cache(img, ctx["sig"], 0)], None


def op_noise(w, ins, ctx):
    import numpy as np
    from PIL import Image

    wd = _clamp_int(w.get("width", 512), 1, 4096)
    ht = _clamp_int(w.get("height", 512), 1, 4096)
    rng = np.random.default_rng(int(w.get("seed", 0)) & 0xFFFFFFFF)
    if str(w.get("mode", "color")) == "grayscale":
        arr = rng.integers(0, 256, (ht, wd), dtype=np.uint8)
        img = Image.fromarray(arr, "L").convert("RGB")
    else:
        arr = rng.integers(0, 256, (ht, wd, 3), dtype=np.uint8)
        img = Image.fromarray(arr, "RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_gradient(w, ins, ctx):
    import numpy as np
    from PIL import Image

    wd = _clamp_int(w.get("width", 512), 1, 8192)
    ht = _clamp_int(w.get("height", 512), 1, 8192)
    c1 = np.array(_parse_color(w.get("color1", "#000000")), dtype=np.float32)
    c2 = np.array(_parse_color(w.get("color2", "#ffffff")), dtype=np.float32)
    ang = float(w.get("angle", 0.0)) * 3.141592653589793 / 180.0
    import math

    xs, ys = np.meshgrid(np.linspace(0, 1, wd), np.linspace(0, 1, ht))
    t = xs * math.cos(ang) + ys * math.sin(ang)
    t -= t.min()
    if t.max() > 0:
        t /= t.max()
    arr = (c1[None, None] * (1 - t[..., None]) + c2[None, None] * t[..., None]).astype(
        np.uint8
    )
    return [_save_cache(Image.fromarray(arr, "RGB"), ctx["sig"], 0)], None


def op_brightness_contrast(w, ins, ctx):
    from PIL import ImageEnhance

    img = _load(ins["image"])
    img = ImageEnhance.Brightness(img).enhance(max(0.0, float(w.get("brightness", 1.0))))
    img = ImageEnhance.Contrast(img).enhance(max(0.0, float(w.get("contrast", 1.0))))
    return [_save_cache(img, ctx["sig"], 0)], None


def op_hue_saturation(w, ins, ctx):
    import numpy as np
    from PIL import Image, ImageEnhance

    img = _load(ins["image"])
    img = ImageEnhance.Color(img).enhance(max(0.0, float(w.get("saturation", 1.0))))
    shift = int(round(float(w.get("hue_shift", 0)))) % 360
    if shift:
        hsv = np.array(img.convert("HSV"), dtype=np.uint8)
        hsv[..., 0] = (hsv[..., 0].astype(np.int16) + int(shift * 255 / 360)) % 256
        img = Image.fromarray(hsv, "HSV").convert("RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_invert(w, ins, ctx):
    from PIL import ImageOps

    return [_save_cache(ImageOps.invert(_load(ins["image"])), ctx["sig"], 0)], None


def op_grayscale(w, ins, ctx):
    img = _load(ins["image"]).convert("L").convert("RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_posterize(w, ins, ctx):
    from PIL import ImageOps

    bits = _clamp_int(w.get("bits", 3), 1, 8)
    return [_save_cache(ImageOps.posterize(_load(ins["image"]), bits), ctx["sig"], 0)], None


def op_threshold(w, ins, ctx):
    img = _load(ins["image"], "L")
    t = _clamp_int(w.get("threshold", 128), 0, 255)
    mask = img.point(lambda p: 255 if p >= t else 0)
    return [_save_cache(mask, ctx["sig"], 0)], None


def op_mask_to_image(w, ins, ctx):
    img = _load(ins["mask"], "L").convert("RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_blur(w, ins, ctx):
    from PIL import ImageFilter

    r = max(0.0, float(w.get("radius", 4.0)))
    img = _load(ins["image"]).filter(ImageFilter.GaussianBlur(r))
    return [_save_cache(img, ctx["sig"], 0)], None


def op_sharpen(w, ins, ctx):
    from PIL import ImageFilter

    img = _load(ins["image"]).filter(
        ImageFilter.UnsharpMask(
            radius=max(0.0, float(w.get("radius", 2.0))),
            percent=_clamp_int(w.get("percent", 150), 0, 500),
        )
    )
    return [_save_cache(img, ctx["sig"], 0)], None


def op_edges(w, ins, ctx):
    from PIL import ImageFilter

    img = _load(ins["image"]).convert("L").filter(ImageFilter.FIND_EDGES).convert("RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_pixelate(w, ins, ctx):
    from PIL import Image

    img = _load(ins["image"])
    f = _clamp_int(w.get("factor", 8), 1, 256)
    small = img.resize((max(1, img.width // f), max(1, img.height // f)), Image.NEAREST)
    return [_save_cache(small.resize(img.size, Image.NEAREST), ctx["sig"], 0)], None


_RESAMPLE = {"nearest": 0, "bilinear": 2, "bicubic": 3, "lanczos": 1}


def op_resize(w, ins, ctx):
    from PIL import Image

    img = _load(ins["image"])
    wd = _clamp_int(w.get("width", 512), 1, 8192)
    ht = _clamp_int(w.get("height", 512), 1, 8192)
    rs = _RESAMPLE.get(str(w.get("method", "lanczos")), 1)
    return [_save_cache(img.resize((wd, ht), Image.Resampling(rs)), ctx["sig"], 0)], None


def op_scale(w, ins, ctx):
    from PIL import Image

    img = _load(ins["image"])
    f = max(0.01, float(w.get("factor", 0.5)))
    size = (max(1, int(img.width * f)), max(1, int(img.height * f)))
    return [_save_cache(img.resize(size, Image.LANCZOS), ctx["sig"], 0)], None


def op_crop(w, ins, ctx):
    img = _load(ins["image"])
    x = _clamp_int(w.get("x", 0), 0, img.width - 1)
    y = _clamp_int(w.get("y", 0), 0, img.height - 1)
    cw = _clamp_int(w.get("width", 256), 1, img.width - x)
    ch = _clamp_int(w.get("height", 256), 1, img.height - y)
    return [_save_cache(img.crop((x, y, x + cw, y + ch)), ctx["sig"], 0)], None


def op_rotate(w, ins, ctx):
    from PIL import Image

    img = _load(ins["image"])
    ang = float(w.get("angle", 90.0))
    expand = bool(w.get("expand", True))
    out = img.rotate(-ang, resample=Image.BICUBIC, expand=expand, fillcolor=(0, 0, 0))
    return [_save_cache(out, ctx["sig"], 0)], None


def op_flip(w, ins, ctx):
    from PIL import ImageOps

    img = _load(ins["image"])
    out = ImageOps.flip(img) if str(w.get("axis", "horizontal")) == "vertical" else ImageOps.mirror(img)
    return [_save_cache(out, ctx["sig"], 0)], None


def _blend_arrays(a, b, mode):
    import numpy as np

    a = a.astype(np.float32) / 255.0
    b = b.astype(np.float32) / 255.0
    if mode == "multiply":
        out = a * b
    elif mode == "screen":
        out = 1 - (1 - a) * (1 - b)
    elif mode == "overlay":
        out = np.where(a <= 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))
    elif mode == "add":
        out = np.clip(a + b, 0, 1)
    elif mode == "difference":
        out = np.abs(a - b)
    elif mode == "darken":
        out = np.minimum(a, b)
    elif mode == "lighten":
        out = np.maximum(a, b)
    else:  # normal
        out = b
    return out, a


def op_blend(w, ins, ctx):
    import numpy as np
    from PIL import Image

    a = _load(ins["image_a"])
    b = _load(ins["image_b"]).resize(a.size)
    alpha = min(1.0, max(0.0, float(w.get("blend_factor", 0.5))))
    blended, base = _blend_arrays(np.array(a), np.array(b), str(w.get("mode", "normal")))
    out = (base * (1 - alpha) + blended * alpha) * 255.0
    img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_composite(w, ins, ctx):
    img = _load(ins["destination"]).copy()
    src = _load(ins["source"], "RGBA")
    x = int(round(float(w.get("x", 0))))
    y = int(round(float(w.get("y", 0))))
    mask = src.getchannel("A")
    if ins.get("mask") is not None:
        m = _load(ins["mask"], "L").resize(src.size)
        from PIL import ImageChops

        mask = ImageChops.multiply(mask, m)
    img.paste(src.convert("RGB"), (x, y), mask)
    return [_save_cache(img, ctx["sig"], 0)], None


def op_text_overlay(w, ins, ctx):
    from PIL import ImageDraw

    img = _load(ins["image"]).copy()
    text = ins.get("text")
    if text is None:
        text = str(w.get("text", ""))
    size = _clamp_int(w.get("size", 48), 4, 512)
    draw = ImageDraw.Draw(img)
    draw.text(
        (int(round(float(w.get("x", 32)))), int(round(float(w.get("y", 32))))),
        str(text),
        fill=_parse_color(w.get("color", "#ffffff"), (255, 255, 255)),
        font=_font(size),
    )
    return [_save_cache(img, ctx["sig"], 0)], None


def op_preview(w, ins, ctx):
    img = _load(ins["image"])
    ref = _save_cache(img, ctx["sig"], 0)
    return [], {"images": [ref]}


def _next_counter(prefix):
    os.makedirs(OUTPUT, exist_ok=True)
    n = 0
    pat = re.compile(re.escape(prefix) + r"_(\d{5})_", re.IGNORECASE)
    for name in os.listdir(OUTPUT):
        m = pat.match(name)
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def op_save_image(w, ins, ctx):
    from PIL import PngImagePlugin

    img = _load(ins["image"])
    prefix = str(w.get("filename_prefix", "comfy")) or "comfy"
    prefix = prefix.replace("%date%", time.strftime("%Y-%m-%d"))
    prefix = re.sub(r"[^\w.\- ]+", "_", prefix)
    path = os.path.join(OUTPUT, f"{prefix}_{_next_counter(prefix):05d}_.png")
    meta = PngImagePlugin.PngInfo()
    if ctx.get("workflow"):
        meta.add_text("workflow", ctx["workflow"])
    img.save(path, pnginfo=meta)
    return [], {"images": [_img_ref(path, img)], "saved": [path]}


def op_string(w, ins, ctx):
    return [str(w.get("value", ""))], None


def op_int(w, ins, ctx):
    return [int(round(float(w.get("value", 0))))], None


def op_float(w, ins, ctx):
    return [float(w.get("value", 0.0))], None


def op_seed(w, ins, ctx):
    return [int(w.get("seed", 0))], None


def op_reroute(w, ins, ctx):
    return [ins.get("input")], None


def op_levels(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    black = _clamp_int(w.get("black", 0), 0, 254)
    white = _clamp_int(w.get("white", 255), black + 1, 255)
    gamma = max(0.1, min(5.0, float(w.get("gamma", 1.0))))
    x = np.asarray(img, dtype=np.float32)
    x = np.clip((x - black) / (white - black), 0, 1) ** (1.0 / gamma)
    out = Image.fromarray((x * 255).astype(np.uint8), "RGB")
    return [_save_cache(out, ctx["sig"], 0)], None


def op_autocontrast(w, ins, ctx):
    from PIL import ImageOps

    cutoff = max(0.0, min(45.0, float(w.get("cutoff", 1.0))))
    return [_save_cache(ImageOps.autocontrast(_load(ins["image"]), cutoff=cutoff), ctx["sig"], 0)], None


def op_equalize(w, ins, ctx):
    from PIL import ImageOps

    return [_save_cache(ImageOps.equalize(_load(ins["image"])), ctx["sig"], 0)], None


def op_solarize(w, ins, ctx):
    from PIL import ImageOps

    t = _clamp_int(w.get("threshold", 128), 0, 255)
    return [_save_cache(ImageOps.solarize(_load(ins["image"]), threshold=t), ctx["sig"], 0)], None


def op_sepia(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    s = max(0.0, min(1.0, float(w.get("strength", 0.8))))
    x = np.asarray(img, dtype=np.float32)
    gray = x.mean(axis=-1, keepdims=True)
    sep = np.clip(gray * [1.07, 0.85, 0.55], 0, 255)
    out = Image.fromarray((x * (1 - s) + sep * s).astype(np.uint8), "RGB")
    return [_save_cache(out, ctx["sig"], 0)], None


def op_color_balance(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    gains = [max(0.0, min(2.5, float(w.get(k, 1.0)))) for k in ("red", "green", "blue")]
    x = np.clip(np.asarray(img, dtype=np.float32) * gains, 0, 255)
    return [_save_cache(Image.fromarray(x.astype(np.uint8), "RGB"), ctx["sig"], 0)], None


def op_tint(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    s = max(0.0, min(1.0, float(w.get("strength", 0.5))))
    c = np.array(_parse_color(w.get("color", "#3366aa")), dtype=np.float32) / 255.0
    x = np.asarray(img, dtype=np.float32)
    tinted = x * c[None, None]
    out = Image.fromarray((x * (1 - s) + tinted * s).astype(np.uint8), "RGB")
    return [_save_cache(out, ctx["sig"], 0)], None


def op_vignette(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    strength = max(0.0, min(1.0, float(w.get("strength", 0.6))))
    soft = max(0.05, min(1.0, float(w.get("softness", 0.5))))
    ht, wd = img.height, img.width
    ys, xs = np.mgrid[0:ht, 0:wd].astype(np.float32)
    r = np.hypot((xs - wd / 2) / (wd / 2), (ys - ht / 2) / (ht / 2)) / 1.4142
    mask = 1 - strength * np.clip((r - (1 - soft)) / soft, 0, 1) ** 2
    x = np.asarray(img, dtype=np.float32) * mask[..., None]
    return [_save_cache(Image.fromarray(x.astype(np.uint8), "RGB"), ctx["sig"], 0)], None


def op_emboss(w, ins, ctx):
    from PIL import ImageFilter

    return [_save_cache(_load(ins["image"]).filter(ImageFilter.EMBOSS), ctx["sig"], 0)], None


def op_median(w, ins, ctx):
    from PIL import ImageFilter

    size = _clamp_int(w.get("size", 3), 3, 9)
    size += 1 - size % 2  # must be odd
    return [_save_cache(_load(ins["image"]).filter(ImageFilter.MedianFilter(size)), ctx["sig"], 0)], None


def op_film_grain(w, ins, ctx):
    import numpy as np
    from PIL import Image

    img = _load(ins["image"])
    amount = max(0.0, min(1.0, float(w.get("amount", 0.15))))
    rng = np.random.default_rng(int(w.get("seed", 0)) & 0xFFFFFFFF)
    x = np.asarray(img, dtype=np.float32)
    noise = rng.normal(0, 255 * amount * 0.35, x.shape[:2])[..., None]
    out = np.clip(x + noise, 0, 255).astype(np.uint8)
    return [_save_cache(Image.fromarray(out, "RGB"), ctx["sig"], 0)], None


def op_add_border(w, ins, ctx):
    from PIL import ImageOps

    size = _clamp_int(w.get("size", 24), 1, 512)
    fill = _parse_color(w.get("color", "#ffffff"), (255, 255, 255))
    return [_save_cache(ImageOps.expand(_load(ins["image"]), border=size, fill=fill), ctx["sig"], 0)], None


def op_channel_split(w, ins, ctx):
    img = _load(ins["image"])
    r, g, b = img.split()
    return [
        _save_cache(r, ctx["sig"], 0),
        _save_cache(g, ctx["sig"], 1),
        _save_cache(b, ctx["sig"], 2),
    ], None


def op_channel_merge(w, ins, ctx):
    from PIL import Image

    r = _load(ins["red"], "L")
    g = _load(ins["green"], "L").resize(r.size)
    b = _load(ins["blue"], "L").resize(r.size)
    return [_save_cache(Image.merge("RGB", (r, g, b)), ctx["sig"], 0)], None


# ------------------------------------------------------- local ML: depth → 3D

DEPTH_MODEL_URL = (
    "https://huggingface.co/onnx-community/depth-anything-v2-small"
    "/resolve/main/onnx/model_quantized.onnx"
)
DEPTH_MODEL_PATH = os.path.join(MODELS, "depth_anything_v2_small_q.onnx")


def op_depth_estimation(w, ins, ctx):
    """Depth-Anything-V2-small (quantized ONNX) on CPU. ~0.7 s at 434px."""
    import numpy as np
    from PIL import Image

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("onnxruntime not installed — run: uv pip install onnxruntime")

    if not os.path.isfile(DEPTH_MODEL_PATH):
        print("first run: downloading Depth-Anything-V2-small (~27 MB)…")
        _download_file(DEPTH_MODEL_URL, DEPTH_MODEL_PATH, timeout=280)

    img = _load(ins["image"])
    size = _clamp_int(w.get("size", 434), 140, 700)
    size -= size % 14  # model requires multiples of 14
    x = np.array(img.resize((size, size), Image.BICUBIC), dtype=np.float32) / 255.0
    x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    x = x.transpose(2, 0, 1)[None].astype(np.float32)

    sess = ort.InferenceSession(DEPTH_MODEL_PATH, providers=["CPUExecutionProvider"])
    depth = sess.run(None, {"pixel_values": x})[0][0]
    d = (depth - depth.min()) / (depth.max() - depth.min() + 1e-9)
    if bool(w.get("invert", False)):
        d = 1.0 - d
    gray = Image.fromarray((d * 255).astype(np.uint8), "L").resize(img.size, Image.BICUBIC)
    return [_save_cache(gray.convert("RGB"), ctx["sig"], 0)], None


YOLO_MODEL_URL = "https://huggingface.co/onnx-community/yolov10n/resolve/main/onnx/model.onnx"
YOLO_MODEL_PATH = os.path.join(MODELS, "yolov10n.onnx")
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


def op_object_detection(w, ins, ctx):
    """YOLOv10-nano (ONNX, NMS-free) on CPU — closed-set 80-class COCO detector.

    Preprocessing matches the model's own transformers.js processor config:
    resize so the longest edge is 640 (aspect ratio preserved), zero-pad to
    640x640 anchored top-left, rescale to 0-1, no mean/std normalization.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("onnxruntime not installed — run: uv pip install onnxruntime")

    if not os.path.isfile(YOLO_MODEL_PATH):
        print("first run: downloading YOLOv10-nano (~11 MB)…")
        _download_file(YOLO_MODEL_URL, YOLO_MODEL_PATH, timeout=280)

    img = _load(ins["image"])
    threshold = max(0.05, min(0.95, float(w.get("threshold", 0.4))))

    size = 640
    scale = size / max(img.width, img.height)
    rw, rh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    resized = img.resize((rw, rh), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    canvas.paste(resized, (0, 0))
    x = (np.asarray(canvas, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]

    sess = ort.InferenceSession(YOLO_MODEL_PATH, providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name
    preds = sess.run(None, {in_name: x})[0][0]  # [N, 6]: xmin,ymin,xmax,ymax,score,class_id

    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font(16)
    detections = []
    for xmin, ymin, xmax, ymax, score, cls_id in preds:
        if score < threshold:
            continue
        cls_id = int(cls_id)
        label = COCO_CLASSES[cls_id] if 0 <= cls_id < len(COCO_CLASSES) else str(cls_id)
        box = [xmin / scale, ymin / scale, xmax / scale, ymax / scale]
        box[0] = max(0, min(img.width, box[0]))
        box[2] = max(0, min(img.width, box[2]))
        box[1] = max(0, min(img.height, box[1]))
        box[3] = max(0, min(img.height, box[3]))
        draw.rectangle(box, outline=(255, 64, 64), width=2)
        tag = f"{label} {score:.2f}"
        draw.rectangle(
            [box[0], max(0, box[1] - 18), box[0] + 8 * len(tag), max(18, box[1])],
            fill=(255, 64, 64),
        )
        draw.text((box[0] + 2, max(0, box[1] - 17)), tag, fill=(255, 255, 255), font=font)
        detections.append({
            "label": label, "score": round(float(score), 4),
            "box": [round(float(v), 1) for v in box],
        })

    detections.sort(key=lambda d: -d["score"])
    return [_save_cache(out, ctx["sig"], 0), json.dumps(detections)], None


REMBG_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
REMBG_MODEL_PATH = os.path.join(MODELS, "u2netp.onnx")


def op_background_removal(w, ins, ctx):
    """U2Net-p (ONNX) salient-object matting on CPU — outputs a soft MASK that
    composes with the existing MaskToImage / ImageComposite nodes.

    Preprocessing matches rembg's own U2netpSession exactly: resize 320x320
    (LANCZOS), rescale by the image's own max (not a fixed /255), then
    ImageNet mean/std normalize.
    """
    import numpy as np
    from PIL import Image

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("onnxruntime not installed — run: uv pip install onnxruntime")

    if not os.path.isfile(REMBG_MODEL_PATH):
        print("first run: downloading U2Net-p (~4.5 MB)…")
        _download_file(REMBG_MODEL_URL, REMBG_MODEL_PATH, timeout=120)

    img = _load(ins["image"])
    mean, std = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
    small = img.resize((320, 320), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64)
    arr = arr / max(arr.max(), 1e-6)
    x = np.zeros_like(arr)
    for c in range(3):
        x[:, :, c] = (arr[:, :, c] - mean[c]) / std[c]
    x = x.transpose(2, 0, 1)[None].astype(np.float32)

    sess = ort.InferenceSession(REMBG_MODEL_PATH, providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name
    pred = sess.run(None, {in_name: x})[0][:, 0, :, :]
    pred = (pred - pred.min()) / (pred.max() - pred.min() + 1e-9)
    mask = Image.fromarray((np.squeeze(pred) * 255).astype(np.uint8), "L").resize(img.size, Image.LANCZOS)
    return [_save_cache(mask, ctx["sig"], 0)], None


def op_image_to_pointcloud(w, ins, ctx):
    """image + depth map → interleaved float32 [x y z r g b] binary."""
    import numpy as np

    img = _load(ins["image"])
    depth = _load(ins["depth"], "L").resize(img.size)
    stride = _clamp_int(w.get("stride", 4), 1, 32)
    wd, ht = img.size
    # keep the cloud under ~150k points regardless of input size
    while (wd // stride) * (ht // stride) > 150_000:
        stride += 1
    zscale = max(0.05, min(4.0, float(w.get("depth_scale", 0.6))))

    rgb = np.asarray(img, dtype=np.float32)[::stride, ::stride] / 255.0
    d = np.asarray(depth, dtype=np.float32)[::stride, ::stride] / 255.0
    h2, w2 = d.shape
    us, vs = np.meshgrid(np.arange(w2), np.arange(h2))
    m = float(max(wd, ht))
    xs = (us * stride - wd / 2) / m
    ys = -(vs * stride - ht / 2) / m
    zs = (d - 0.5) * zscale
    pts = np.stack(
        [xs, ys, zs, rgb[..., 0], rgb[..., 1], rgb[..., 2]], axis=-1
    ).reshape(-1, 6).astype("<f4")

    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{ctx['sig']}_pc.bin")
    pts.tofile(path)
    return [{"__pointcloud__": path, "points": int(pts.shape[0])}], None


def op_preview_3d(w, ins, ctx):
    pc = ins.get("pointcloud")
    if not isinstance(pc, dict) or "__pointcloud__" not in pc:
        raise ValueError("expected a POINTCLOUD value")
    return [], {"pointcloud": pc}


# --------------------------------------------------------------- API nodes

HF_ROUTER = "https://router.huggingface.co/hf-inference/models/"


def op_hf_text_to_image(w, ins, ctx):
    from PIL import Image
    import io

    token = _resolve_secret(w, "HF_TOKEN", "create one at huggingface.co/settings/tokens")
    prompt = ins.get("prompt")
    if prompt is None:
        prompt = str(w.get("prompt", ""))
    if not str(prompt).strip():
        raise ValueError("prompt is empty")
    model = str(w.get("model", "black-forest-labs/FLUX.1-schnell")).strip()
    params = {
        "width": _clamp_int(w.get("width", 768), 64, 1536),
        "height": _clamp_int(w.get("height", 768), 64, 1536),
    }
    seed = int(w.get("seed", 0))
    if seed:
        params["seed"] = seed
    body = json.dumps({"inputs": str(prompt), "parameters": params}).encode()
    data, ctype = _http(
        HF_ROUTER + model,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=27,
    )
    if "json" in ctype:  # error or still-loading payload
        raise RuntimeError(f"HF API: {data[:400].decode('utf-8', 'replace')}")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return [_save_cache(img, ctx["sig"], 0)], None


def op_hf_image_caption(w, ins, ctx):
    token = _resolve_secret(w, "HF_TOKEN", "create one at huggingface.co/settings/tokens")
    ref = ins["image"]
    model = str(w.get("model", "Salesforce/blip-image-captioning-large")).strip()
    with open(ref["__image__"], "rb") as f:
        raw = f.read()
    data, _ = _http(
        HF_ROUTER + model,
        data=raw,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/png"},
        timeout=27,
    )
    out = json.loads(data)
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"HF API: {out['error']}")
    caption = out[0].get("generated_text", "") if isinstance(out, list) and out else str(out)
    return [caption], {"text": caption}


def op_hf_visual_qa(w, ins, ctx):
    import base64

    token = _resolve_secret(w, "HF_TOKEN", "create one at huggingface.co/settings/tokens")
    ref = ins["image"]
    question = ins.get("question")
    if question is None:
        question = str(w.get("question", ""))
    if not str(question).strip():
        raise ValueError("question is empty")
    model = str(w.get("model", "dandelin/vilt-b32-finetuned-vqa")).strip()
    with open(ref["__image__"], "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = json.dumps({"inputs": {"image": b64, "question": str(question)}}).encode()
    data, _ = _http(
        HF_ROUTER + model,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=27,
    )
    out = json.loads(data)
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"HF API: {out['error']}")
    answer = out[0].get("answer", "") if isinstance(out, list) and out else str(out)
    return [answer], {"text": answer}


def op_hf_image_edit(w, ins, ctx):
    """Instruction-based image editing (e.g. InstructPix2Pix, Qwen-Image-Edit)
    via the same HF Inference router used by HFTextToImage, but with an
    IMAGE input carried as base64 in `inputs` alongside the text prompt."""
    import base64
    import io
    from PIL import Image

    token = _resolve_secret(w, "HF_TOKEN", "create one at huggingface.co/settings/tokens")
    prompt = ins.get("prompt")
    if prompt is None:
        prompt = str(w.get("prompt", ""))
    if not str(prompt).strip():
        raise ValueError("prompt is empty")
    model = str(w.get("model", "timbrooks/instruct-pix2pix")).strip()

    img = _load(ins["image"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    body = json.dumps({"inputs": b64, "parameters": {"prompt": str(prompt)}}).encode()
    data, ctype = _http(
        HF_ROUTER + model,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=27,
    )
    if "json" in ctype:  # error or still-loading payload
        raise RuntimeError(f"HF API: {data[:400].decode('utf-8', 'replace')}")
    out_img = Image.open(io.BytesIO(data)).convert("RGB")
    return [_save_cache(out_img, ctx["sig"], 0)], None


def op_seedance_video(w, ins, ctx):
    """ByteDance Seedance via BytePlus ModelArk. Video generation takes minutes,
    far beyond one 30 s node call — so: submit once, persist the task id keyed
    by this node's signature, and each re-run resumes polling the same task."""
    token = _resolve_secret(w, "ARK_API_KEY", "BytePlus ModelArk console → API keys")
    endpoint = str(w.get("endpoint", "https://ark.ap-southeast.bytepluses.com/api/v3")).rstrip("/")
    model = str(w.get("model", "seedance-1-0-lite-t2v-250428")).strip()
    prompt = ins.get("prompt")
    if prompt is None:
        prompt = str(w.get("prompt", ""))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    os.makedirs(CACHE, exist_ok=True)
    task_file = os.path.join(CACHE, f"{ctx['sig']}.task")
    task_id = None
    if os.path.isfile(task_file):
        with open(task_file, "r", encoding="utf-8") as f:
            task_id = f.read().strip() or None

    if not task_id:
        text = f"{prompt} --resolution {w.get('resolution', '720p')} --duration {int(w.get('duration', 5))}"
        body = json.dumps({"model": model, "content": [{"type": "text", "text": text}]}).encode()
        data, _ = _http(endpoint + "/contents/generations/tasks", data=body, headers=headers, timeout=25)
        task_id = json.loads(data).get("id")
        if not task_id:
            raise RuntimeError(f"Seedance: no task id in response: {data[:300]!r}")
        with open(task_file, "w", encoding="utf-8") as f:
            f.write(task_id)
        print(f"seedance task submitted: {task_id}")

    deadline = time.time() + 20  # leave headroom inside the 30 s node budget
    status = "queued"
    while time.time() < deadline:
        data, _ = _http(f"{endpoint}/contents/generations/tasks/{task_id}", headers=headers, timeout=15)
        st = json.loads(data)
        status = st.get("status", "unknown")
        if status == "succeeded":
            video_url = (st.get("content") or {}).get("video_url")
            if not video_url:
                raise RuntimeError(f"Seedance succeeded but no video_url: {data[:300]!r}")
            os.makedirs(OUTPUT, exist_ok=True)
            path = os.path.join(OUTPUT, f"seedance_{task_id[-8:]}.mp4")
            _download_file(video_url, path, timeout=120)
            os.remove(task_file)
            return [{"__video__": path}], {"videos": [path]}
        if status == "failed":
            os.remove(task_file)
            raise RuntimeError(f"Seedance task failed: {data[:300]!r}")
        time.sleep(3)
    raise RuntimeError(
        f"Seedance still {status} (task {task_id}) — video takes a few minutes; "
        "press Run again to keep waiting (the task resumes, it is not resubmitted)"
    )


FAL_QUEUE = "https://queue.fal.run/"


def op_ltx_video_image_to_video(w, ins, ctx):
    """LTX-Video 13B distilled (Lightricks) via fal.ai's queue API. Generation
    is fast for this distilled model but still crosses one 30 s node call —
    submit once, persist the request id keyed by this node's signature, and
    each re-run resumes polling the same request."""
    import base64
    import io

    token = _resolve_secret(w, "FAL_KEY", "create one at fal.ai/dashboard/keys")
    endpoint = str(w.get("model", "fal-ai/ltx-video-13b-distilled/image-to-video")).strip()
    prompt = ins.get("prompt")
    if prompt is None:
        prompt = str(w.get("prompt", ""))
    if not str(prompt).strip():
        raise ValueError("prompt is empty")
    headers = {"Authorization": f"Key {token}", "Content-Type": "application/json"}

    img = _load(ins["image"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    os.makedirs(CACHE, exist_ok=True)
    task_file = os.path.join(CACHE, f"{ctx['sig']}.faltask")
    request_id = None
    if os.path.isfile(task_file):
        with open(task_file, "r", encoding="utf-8") as f:
            request_id = f.read().strip() or None

    if not request_id:
        body = json.dumps({"prompt": str(prompt), "image_url": data_uri}).encode()
        data, _ = _http(FAL_QUEUE + endpoint, data=body, headers=headers, timeout=25)
        request_id = json.loads(data).get("request_id")
        if not request_id:
            raise RuntimeError(f"fal.ai: no request_id in response: {data[:300]!r}")
        with open(task_file, "w", encoding="utf-8") as f:
            f.write(request_id)
        print(f"fal.ai LTX-Video task submitted: {request_id}")

    status_url = f"{FAL_QUEUE}{endpoint}/requests/{request_id}/status"
    result_url = f"{FAL_QUEUE}{endpoint}/requests/{request_id}"
    deadline = time.time() + 20  # leave headroom inside the 30 s node budget
    status = "IN_QUEUE"
    while time.time() < deadline:
        data, _ = _http(status_url, headers=headers, timeout=15)
        status = json.loads(data).get("status", "UNKNOWN")
        if status == "COMPLETED":
            data, _ = _http(result_url, headers=headers, timeout=15)
            result = json.loads(data)
            video_url = (result.get("video") or {}).get("url")
            if not video_url:
                raise RuntimeError(f"fal.ai succeeded but no video url: {data[:300]!r}")
            os.makedirs(OUTPUT, exist_ok=True)
            path = os.path.join(OUTPUT, f"ltxvideo_{request_id[-8:]}.mp4")
            _download_file(video_url, path, timeout=120)
            os.remove(task_file)
            return [{"__video__": path}], {"videos": [path]}
        time.sleep(2)
    raise RuntimeError(
        f"fal.ai LTX-Video still {status} (request {request_id}) — video takes "
        "under a minute; press Run again to keep waiting (the task resumes, it is not resubmitted)"
    )


def op_preview_video(w, ins, ctx):
    v = ins.get("video")
    if not isinstance(v, dict) or "__video__" not in v:
        raise ValueError("expected a VIDEO value")
    return [], {"videos": [v["__video__"]]}


def _video_ref(ins, key="video"):
    v = ins.get(key)
    if not isinstance(v, dict) or "__video__" not in v:
        raise ValueError("expected a VIDEO value")
    return v["__video__"]


def op_video_trim(w, ins, ctx):
    """Trim to [start, end) seconds and optionally cap the width, re-encoding
    as H.264 via imageio's bundled ffmpeg so the result plays in a browser."""
    import imageio
    import numpy as np
    from PIL import Image

    src = _video_ref(ins)
    start = max(0.0, float(w.get("start", 0)))
    end = float(w.get("end", 3))
    if end <= start:
        raise ValueError("end must be greater than start")
    max_width = _clamp_int(w.get("max_width", 480), 0, 4096)

    reader = imageio.get_reader(src)
    try:
        fps = float(reader.get_meta_data().get("fps", 24) or 24)
        os.makedirs(OUTPUT, exist_ok=True)
        out_path = os.path.join(OUTPUT, f"trim_{ctx['sig']}.mp4")
        writer = imageio.get_writer(out_path, fps=fps, codec="libx264", quality=8)
        try:
            for i, frame in enumerate(reader):
                t = i / fps
                if t < start:
                    continue
                if t >= end:
                    break
                if max_width and frame.shape[1] > max_width:
                    img = Image.fromarray(frame)
                    h = max(1, round(img.height * max_width / img.width))
                    frame = np.array(img.resize((max_width, h), Image.BICUBIC))
                writer.append_data(frame)
        finally:
            writer.close()
    finally:
        reader.close()
    return [{"__video__": out_path}], {"videos": [out_path]}


RIFE_MODEL_URL = (
    "https://huggingface.co/yuvraj108c/rife-onnx/resolve/main"
    "/rife47_ensemble_True_scale_1_sim.onnx"
)
RIFE_MODEL_PATH = os.path.join(MODELS, "rife47_ensemble.onnx")


def _rife_middle_frame(sess, in_name0, in_name1, in_namet, a, b, timestep):
    """One RIFE inference: a, b are HxWx3 uint8 RGB frames of matching size."""
    import numpy as np

    h, w = a.shape[:2]
    ph, pw = (32 - h % 32) % 32, (32 - w % 32) % 32
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw), (0, 0)), mode="edge")
        b = np.pad(b, ((0, ph), (0, pw), (0, 0)), mode="edge")

    def prep(x):
        return (x.astype("float32") / 255.0).transpose(2, 0, 1)[None]

    out = sess.run(None, {
        in_name0: prep(a), in_name1: prep(b), in_namet: np.array([timestep], dtype="float32"),
    })[0][0]
    out = (out.transpose(1, 2, 0).clip(0, 1) * 255).astype("uint8")
    return out[:h, :w]


def op_video_interpolate_rife(w, ins, ctx):
    """RIFE (Practical-RIFE v4.7, ensemble) frame interpolation on CPU.

    Bounded to a short, downscaled clip so a multi-second video stays inside
    the node's execution budget: at most `max_seconds` of source is read and
    the longest edge is capped to `max_width` before interpolating.
    """
    import imageio
    import numpy as np
    from PIL import Image

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("onnxruntime not installed — run: uv pip install onnxruntime")

    if not os.path.isfile(RIFE_MODEL_PATH):
        print("first run: downloading RIFE v4.7 (~20 MB)…")
        _download_file(RIFE_MODEL_URL, RIFE_MODEL_PATH, timeout=280)

    src = _video_ref(ins)
    multiplier = _clamp_int(w.get("multiplier", 2), 2, 4)
    max_seconds = max(0.5, min(6.0, float(w.get("max_seconds", 2.0))))
    max_width = _clamp_int(w.get("max_width", 384), 64, 960)

    reader = imageio.get_reader(src)
    try:
        fps = float(reader.get_meta_data().get("fps", 24) or 24)
        max_frames = max(2, round(max_seconds * fps))
        frames = []
        for i, frame in enumerate(reader):
            if i >= max_frames:
                break
            img = Image.fromarray(frame).convert("RGB")
            if img.width > max_width:
                h = max(1, round(img.height * max_width / img.width))
                img = img.resize((max_width, h), Image.BICUBIC)
            frames.append(np.array(img))
    finally:
        reader.close()
    if len(frames) < 2:
        raise ValueError("need at least 2 frames to interpolate")

    sess = ort.InferenceSession(RIFE_MODEL_PATH, providers=["CPUExecutionProvider"])
    in0, in1, int_ = (i.name for i in sess.get_inputs())

    out_frames = [frames[0]]
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        for k in range(1, multiplier):
            out_frames.append(_rife_middle_frame(sess, in0, in1, int_, a, b, k / multiplier))
        out_frames.append(b)

    os.makedirs(OUTPUT, exist_ok=True)
    out_path = os.path.join(OUTPUT, f"rife_{ctx['sig']}.mp4")
    writer = imageio.get_writer(out_path, fps=fps * multiplier, codec="libx264", quality=8)
    try:
        for f in out_frames:
            writer.append_data(f)
    finally:
        writer.close()
    return [{"__video__": out_path}], {"videos": [out_path]}


def op_load_video(w, ins, ctx):
    name = os.path.basename(str(w.get("video", "")))
    for base in (INPUT, OUTPUT):
        path = os.path.join(base, name)
        if name and os.path.isfile(path):
            return [{"__video__": path}], {"videos": [path]}
    raise FileNotFoundError(f"video {name!r} not found in input/ or output/")


OPS = {
    "LoadImage": op_load_image,
    "EmptyImage": op_empty_image,
    "NoiseImage": op_noise,
    "GradientImage": op_gradient,
    "BrightnessContrast": op_brightness_contrast,
    "HueSaturation": op_hue_saturation,
    "Invert": op_invert,
    "Grayscale": op_grayscale,
    "Posterize": op_posterize,
    "Threshold": op_threshold,
    "MaskToImage": op_mask_to_image,
    "GaussianBlur": op_blur,
    "Sharpen": op_sharpen,
    "EdgeDetect": op_edges,
    "Pixelate": op_pixelate,
    "ImageResize": op_resize,
    "ImageScale": op_scale,
    "ImageCrop": op_crop,
    "ImageRotate": op_rotate,
    "ImageFlip": op_flip,
    "ImageBlend": op_blend,
    "ImageComposite": op_composite,
    "TextOverlay": op_text_overlay,
    "PreviewImage": op_preview,
    "SaveImage": op_save_image,
    "PrimitiveString": op_string,
    "PrimitiveInt": op_int,
    "PrimitiveFloat": op_float,
    "PrimitiveSeed": op_seed,
    "Reroute": op_reroute,
    "Levels": op_levels,
    "AutoContrast": op_autocontrast,
    "Equalize": op_equalize,
    "Solarize": op_solarize,
    "Sepia": op_sepia,
    "ColorBalance": op_color_balance,
    "Tint": op_tint,
    "Vignette": op_vignette,
    "Emboss": op_emboss,
    "MedianFilter": op_median,
    "FilmGrain": op_film_grain,
    "AddBorder": op_add_border,
    "ChannelSplit": op_channel_split,
    "ChannelMerge": op_channel_merge,
    "DepthEstimation": op_depth_estimation,
    "ObjectDetectionYOLO": op_object_detection,
    "BackgroundRemoval": op_background_removal,
    "ImageToPointCloud": op_image_to_pointcloud,
    "Preview3D": op_preview_3d,
    "HFTextToImage": op_hf_text_to_image,
    "HFImageCaption": op_hf_image_caption,
    "HFVisualQA": op_hf_visual_qa,
    "HFImageEdit": op_hf_image_edit,
    "SeedanceTextToVideo": op_seedance_video,
    "LTXVideoImageToVideo": op_ltx_video_image_to_video,
    "PreviewVideo": op_preview_video,
    "LoadVideo": op_load_video,
    "VideoTrim": op_video_trim,
    "VideoInterpolateRIFE": op_video_interpolate_rife,
}

CACHEABLE = {t for t in OPS if t != "SaveImage"}


# Bare main() — NOT @fused.udf — so the same file runs under both engines:
# the built-in executor calls main(**params) directly (a udf wrapper would
# hang trying to authenticate), and the fused engine's compat bridge binds
# params by annotation and calls it the same way.
def main(
    node_type: str,
    widgets: str = "{}",
    inputs: str = "{}",
    sig: str = "",
    workflow: str = "",
):
    if node_type not in OPS:
        raise ValueError(f"cannot execute node type {node_type!r} (no local implementation)")
    sig = re.sub(r"[^\w-]", "", sig) or f"nosig{random.randrange(1 << 48):012x}"
    manifest = os.path.join(CACHE, f"{sig}.json")

    if node_type in CACHEABLE and os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                cached = json.load(f)
            refs = [v for v in cached.get("outputs", []) if isinstance(v, dict)]
            refs += (cached.get("ui") or {}).get("images", [])
            if all(os.path.isfile(r["__image__"]) for r in refs if "__image__" in r):
                cached["cached"] = True
                return cached
        except Exception:
            pass  # corrupt manifest — recompute

    ctx = {"sig": sig, "workflow": workflow}
    outputs, ui = OPS[node_type](json.loads(widgets), json.loads(inputs), ctx)
    result = {"outputs": outputs, "ui": ui, "cached": False}
    if node_type in CACHEABLE:
        os.makedirs(CACHE, exist_ok=True)
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(result, f)
    return result
