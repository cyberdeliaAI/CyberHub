"""Upscaler — neural image upscaling (Real-ESRGAN / ESRGAN ONNX), server-side.

Runs the model with onnxruntime on the machine hosting CyberHub. That keeps it
cross-platform (CPU everywhere; CoreML on Apple Silicon, CUDA via onnxruntime-gpu
if installed) and lets it batch a whole folder with tiling for large images,
rather than choking a browser's WASM runtime.

You supply the models: drop one or more Real-ESRGAN / ESRGAN **ONNX** files into the
models folder and pick which to use on the page (Forge-style). The upscale factor is
detected automatically from the model.

Settings:
    models_folder  folder  Folder of .onnx upscaler models (default: resources/upscalers).
    output_folder  folder  Where batch (folder) upscales are written.
    tile_size      int     Tile edge in px (0 = whole image). Default 256.
    tile_pad       int     Tile overlap in px to avoid seams. Default 16.
    suffix         str     Filename suffix for results. Default "_upscaled".
"""

import math
import os
import re
import tempfile
import threading
import time

from core import Module
from core.server import build_shell

try:
    import numpy as np
    from PIL import Image
    from PIL import PngImagePlugin
    HAS_DEPS = True
    DEPS_ERROR = ""
except ImportError as exc:
    HAS_DEPS = False
    DEPS_ERROR = str(exc)
    np = None
    Image = None
    PngImagePlugin = None

try:
    import onnxruntime as ort
    HAS_ORT = True
    ORT_ERROR = ""
except ImportError as exc:
    HAS_ORT = False
    ORT_ERROR = str(exc)
    ort = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
DONE_STATES = {"done", "failed", "cancelled"}
JOB_TTL_SECONDS = 60 * 60
RESULT_TTL_SECONDS = 10 * 60


class UpscaleCancelled(Exception):
    pass


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, _num(v, lo)))


# ─── Tiling pipeline (pure; testable without a real model) ──────────────────────

def upscale_array(arr, run, scale, tile=256, pad=16, progress=None):
    """Upscale a float32 [H,W,3] image in [0,1] by `scale`, tile by tile.

    `run(tile_hwc) -> upscaled_hwc` does the actual model inference on one tile
    (any callable, so the tiling can be unit-tested with a plain numpy stand-in).
    Tiles are processed with `pad` px of context on each side and the padded
    border is cropped off after upscaling, which removes seams.
    """
    h, w, _ = arr.shape
    if not tile or tile <= 0:
        out = run(arr)
        if progress:
            progress(1, 1)
        return np.clip(out, 0.0, 1.0)

    out = np.zeros((h * scale, w * scale, 3), dtype=np.float32)
    tiles_x = math.ceil(w / tile)
    tiles_y = math.ceil(h / tile)
    total = tiles_x * tiles_y
    done = 0
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            x0, y0 = tx * tile, ty * tile
            x1, y1 = min(x0 + tile, w), min(y0 + tile, h)
            # Padded input region (clamped to the image).
            xp0, yp0 = max(x0 - pad, 0), max(y0 - pad, 0)
            xp1, yp1 = min(x1 + pad, w), min(y1 + pad, h)
            up = run(arr[yp0:yp1, xp0:xp1, :])
            # Crop the padding back off, in output (scaled) coordinates.
            ox0, oy0 = (x0 - xp0) * scale, (y0 - yp0) * scale
            ox1, oy1 = ox0 + (x1 - x0) * scale, oy0 + (y1 - y0) * scale
            out[y0 * scale:y1 * scale, x0 * scale:x1 * scale, :] = up[oy0:oy1, ox0:ox1, :]
            done += 1
            if progress:
                progress(done, total)
    return np.clip(out, 0.0, 1.0)


class OnnxUpscaler:
    """Wraps an ESRGAN-style ONNX session: HWC[0,1] tile in -> HWC[0,1] tile out.

    Adapts to whatever the model exposes, so the common Real-ESRGAN ONNX exports
    all work:
      * input dtype — float32 or float16 (e.g. the *.fp16.onnx exports);
      * input size — dynamic (any H×W) or **fixed** (e.g. Qualcomm's 128×128).
        Fixed-size models can't take CyberHub's variable edge tiles, so we pad each
        tile up to the model's input size and crop the output back.
    """

    def __init__(self, model_path):
        providers = ort.get_available_providers()
        # Prefer hardware acceleration when present; CPU is the universal fallback.
        order = [p for p in ("CUDAExecutionProvider", "CoreMLExecutionProvider",
                             "DmlExecutionProvider", "CPUExecutionProvider") if p in providers]
        attempts = [order] if order else []
        if "CPUExecutionProvider" in providers and order != ["CPUExecutionProvider"]:
            attempts.append(["CPUExecutionProvider"])
        last_error = None
        for provider_order in attempts or [None]:
            try:
                self.session = ort.InferenceSession(model_path, providers=provider_order)
                break
            except Exception as exc:
                last_error = exc
                self.session = None
        if self.session is None:
            raise RuntimeError(f"Could not load ONNX upscaler model: {last_error}")
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.np_dtype = np.float16 if "float16" in (inp.type or "") else np.float32
        shape = list(inp.shape) if inp.shape else []
        h = shape[2] if len(shape) == 4 else None
        w = shape[3] if len(shape) == 4 else None
        self.input_size = (h, w) if isinstance(h, int) and isinstance(w, int) else None
        self.mtime = os.path.getmtime(model_path)
        self.scale = self._detect_scale()

    def _detect_scale(self):
        ph, pw = self.input_size or (64, 64)
        probe = np.zeros((1, 3, ph, pw), dtype=self.np_dtype)
        try:
            out = self.session.run(None, {self.input_name: probe})[0]
            return max(1, round(out.shape[-1] / pw))
        except Exception:
            return 4  # ESRGAN default

    def run(self, tile_hwc):
        h, w = tile_hwc.shape[:2]
        src = tile_hwc
        if self.input_size:               # fixed-input model: pad tile up to it
            fh, fw = self.input_size
            buf = np.zeros((fh, fw, 3), dtype=np.float32)
            buf[:min(h, fh), :min(w, fw)] = tile_hwc[:fh, :fw]
            src = buf
        x = np.transpose(src, (2, 0, 1))[None].astype(self.np_dtype)  # [1,3,h,w]
        y = self.session.run(None, {self.input_name: x})[0].astype(np.float32)
        # Be tolerant of how different ESRGAN exports shape their output:
        # [1,3,H,W], [3,H,W] (no batch), [1,H,W,3], grayscale [H,W], etc.
        y = np.squeeze(y)
        if y.ndim == 3 and y.shape[0] in (3, 4):       # CHW
            out = np.transpose(y[:3], (1, 2, 0))
        elif y.ndim == 3 and y.shape[2] in (3, 4):     # HWC
            out = y[..., :3]
        elif y.ndim == 2:                              # grayscale
            out = np.stack([y, y, y], axis=-1)
        else:
            raise RuntimeError(f"Unsupported model output shape {tuple(y.shape)}")
        if out.max() > 2.0:                            # model emits [0,255], not [0,1]
            out = out / 255.0
        if self.input_size:                            # crop back to the real region
            out = out[:h * self.scale, :w * self.scale]
        return out


def _postprocess(img, ow, oh, target_scale, max_w, max_h):
    """Resize the model's native-scale output to the requested target scale (relative
    to the original size), then shrink to fit any max width/height — all with Lanczos."""
    if target_scale and target_scale > 0:
        tw, th = max(1, round(ow * target_scale)), max(1, round(oh * target_scale))
        if (tw, th) != img.size:
            img = img.resize((tw, th), Image.LANCZOS)
    w, h = img.size
    factor = 1.0
    if max_w and w > max_w:
        factor = min(factor, max_w / w)
    if max_h and h > max_h:
        factor = min(factor, max_h / h)
    if factor < 1.0:
        img = img.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.LANCZOS)
    return img


def _has_alpha(pil_img):
    return pil_img.mode in ("RGBA", "LA") or (pil_img.mode == "P" and "transparency" in pil_img.info)


def _blend_original(upscaled, source, amount):
    """Blend some of the original image back in to soften over-sharpened results."""
    amount = _clamp(amount, 0.0, 0.9)
    if amount <= 0:
        return upscaled
    mode = "RGBA" if upscaled.mode == "RGBA" else "RGB"
    alpha = upscaled.getchannel("A") if mode == "RGBA" else None
    base = source.convert(mode).resize(upscaled.size, Image.BILINEAR)
    blended = Image.blend(upscaled.convert(mode), base, amount)
    if alpha is not None:
        blended.putalpha(alpha)
    return blended


def _upscale_pil(up, pil_img, tile, pad, target_scale=0, max_w=0, max_h=0,
                 progress=None, blend=0):
    """Upscale a PIL image with the given OnnxUpscaler. For fixed-input models the
    tile is sized to fit the model's input (so padded tiles never overflow it).
    target_scale / max_w / max_h post-resize the result (Lanczos)."""
    source = pil_img.convert("RGBA") if _has_alpha(pil_img) else pil_img.convert("RGB")
    rgb = source.convert("RGB")
    ow, oh = rgb.size
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    if up.input_size:
        f = min(up.input_size)
        pad = min(pad, max(0, f // 8))
        tile = max(8, f - 2 * pad)
    out = upscale_array(arr, up.run, up.scale, tile=tile, pad=pad, progress=progress)
    img = Image.fromarray((out * 255.0 + 0.5).astype("uint8"), "RGB")
    img = _postprocess(img, ow, oh, target_scale, max_w, max_h)
    if source.mode == "RGBA":
        img = img.convert("RGBA")
        img.putalpha(source.getchannel("A").resize(img.size, Image.LANCZOS))
    return _blend_original(img, source, blend)


def _pnginfo_from_image(pil_img):
    """Copy textual PNG metadata such as A1111/ComfyUI parameters/workflow."""
    if not PngImagePlugin or not getattr(pil_img, "info", None):
        return None
    pnginfo = PngImagePlugin.PngInfo()
    added = 0
    for key, value in pil_img.info.items():
        if isinstance(value, str):
            try:
                pnginfo.add_text(str(key), value)
                added += 1
            except Exception:
                pass
    return pnginfo if added else None


def _image_save_format(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "JPEG"
    if ext == ".webp":
        return "WEBP"
    if ext in (".tif", ".tiff"):
        return "TIFF"
    return "PNG"


def _safe_stem(filename, default="image"):
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" .")
    return stem or default


def _unique_path(path):
    """Return path, or path with _2/_3... if it already exists."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(2, 10000):
        candidate = f"{base}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{base}_{int(time.time())}{ext}"


def _save_image_atomic(img, path, source_img=None):
    """Save via a same-folder temp file, then atomically replace the destination."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = _image_save_format(path)
    kwargs = {}
    if fmt == "PNG":
        pnginfo = _pnginfo_from_image(source_img) if source_img else None
        if pnginfo:
            kwargs["pnginfo"] = pnginfo
    elif fmt == "JPEG":
        img = img.convert("RGB")

    tmp_name = None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=os.path.splitext(path)[1] or ".png",
        dir=os.path.dirname(path),
    )
    os.close(fd)
    try:
        img.save(tmp_name, format=fmt, **kwargs)
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


# ─── Module ─────────────────────────────────────────────────────────────────────

class UpscalerModule(Module):
    name = "Upscaler"
    version = "1.1.1-beta"
    release_stage = "beta"
    icon = "⬆"   # ⬆
    description = "Beta. Neural image upscaling (Real-ESRGAN / ESRGAN ONNX), with tiling."
    order = 46
    settings_schema = {
        "models_folder": {
            "type": "folder", "label": "Upscaler models folder", "default": "",
            "desc": "Folder containing your ONNX upscaler models (e.g. RealESRGAN_x4plus.onnx). "
                    "You pick which one to use on the Upscaler page, like Forge's upscaler list. "
                    "Drop multiple .onnx files here to switch between them.",
        },
        "output_folder": {
            "type": "folder", "label": "Output folder", "default": "",
            "desc": "Where batch (folder) upscales are saved. Leave empty to write next to the source. "
                    "Single dropped images download straight back to your browser; multi-image single queues "
                    "are saved here automatically when this folder is set.",
        },
        "tile_size": {
            "type": "number", "label": "Tile size (px)", "default": 256, "min": 0, "max": 2048,
            "desc": "Process the image in tiles of this size (0 = whole image at once). "
                    "Smaller tiles use less memory but are a little slower.",
        },
        "tile_pad": {
            "type": "number", "label": "Tile overlap (px)", "default": 16, "min": 0, "max": 128,
            "desc": "Context overlap between tiles, removed after upscaling to avoid seams.",
        },
        "suffix": {
            "type": "string", "label": "Output suffix", "default": "_upscaled",
            "desc": "Added to the filename of each upscaled image.",
        },
    }

    def __init__(self, hub):
        super().__init__(hub)
        self._cache = {}          # model path -> OnnxUpscaler
        self._jobs = {}
        self._results = {}        # job id -> PNG bytes (single-image results)
        self._lock = threading.Lock()
        self._counter = 0

    def key(self):
        return "upscaler"

    def routes_get(self):
        return {
            "/upscaler": self._page,
            "/api/upscaler/status": self._api_status,
            "/api/upscaler/models": self._api_models,
            "/api/upscaler/one-result": self._api_one_result,
        }

    def routes_post(self):
        return {
            "/api/upscaler/run": self._api_run,
            "/api/upscaler/one": self._api_upscale_one,
            "/api/upscaler/cancel": self._api_cancel,
        }

    def _page(self, handler, qs):
        # Model availability is resolved live by the page (upLoadModels →
        # /api/upscaler/models), so no server-side placeholder is needed.
        handler.respond_html(build_shell(
            self.hub.registry, self.hub.settings,
            active_key=self.key(), page_title="Upscaler", body_html=PAGE_BODY))

    # ─── Models (folder + pick one, Forge-style) ───────────────────────────────
    def _models_dir(self):
        configured = (self.setting("models_folder", "") or "").strip()
        if configured:
            return configured
        return os.path.join(self.hub.resources_dir, "upscalers")

    def _list_models(self):
        d = self._models_dir()
        if not d or not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.lower().endswith(".onnx"))

    def _model_hint(self, model_name):
        name = (model_name or "").lower()
        if name.startswith("1x") or "skin" in name or "ircnn" in name:
            return "1x enhance/detail model — keeps the same size."
        if "anime" in name or "kemono" in name:
            return "Best for anime, illustration, line art, or stylized images."
        if "text2hd" in name:
            return "Good for text, graphics, and UI-like sharp edges."
        if "clearreality" in name or "realistic" in name:
            return "Photo/realistic 4x model; can add crisp detail."
        if "remacri" in name or "siax" in name or "nickelback" in name:
            return "General high-detail 4x model; strong sharpening."
        if "realesrgan" in name or "real-esrgan" in name:
            return "General Real-ESRGAN model for photos and mixed images."
        if "2x" in name:
            return "2x model; useful when 4x is too much."
        if "4x" in name:
            return "4x model; best when you want a large upscale."
        return "ONNX upscaler model."

    def _api_models(self, handler, qs):
        if not (HAS_DEPS and HAS_ORT):
            handler.respond_json({"ok": False, "error":
                "Upscaler needs numpy, Pillow and onnxruntime. " + (DEPS_ERROR or ORT_ERROR)})
            return
        d = self._models_dir()
        if not d:
            handler.respond_json({"ok": False, "error":
                "Set an Upscaler models folder in Settings → Upscaler."})
            return
        models = self._list_models()
        handler.respond_json({"ok": True, "models": models,
                              "hints": {m: self._model_hint(m) for m in models}})

    def _get_upscaler(self, model_name):
        if not (HAS_DEPS and HAS_ORT):
            raise RuntimeError("Upscaler needs numpy, Pillow and onnxruntime. "
                               + (DEPS_ERROR or ORT_ERROR))
        d = self._models_dir()
        if not d or not os.path.isdir(d):
            raise RuntimeError("Set a valid Upscaler models folder in Settings → Upscaler.")
        name = os.path.basename(model_name or "")   # block path traversal
        if not name.lower().endswith(".onnx"):
            raise RuntimeError("Pick a model.")
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            raise RuntimeError(f"Model not found: {name}")
        cached = self._cache.get(path)
        if cached is None or cached.mtime != os.path.getmtime(path):
            cached = OnnxUpscaler(path)
            self._cache[path] = cached
        return cached

    # ─── Single dropped image (multipart upload → upscaled image back) ─────────
    def _api_upscale_one(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
        except Exception as e:
            handler.respond_json({"error": f"Upload failed: {e}"}, status=400); return
        item = files.get("file") or {}

        def field(name, default=""):
            f = files.get(name)
            if f and f.get("data") is not None:
                try:
                    return f["data"].decode("utf-8", "replace")
                except Exception:
                    return default
            return default

        if not item.get("data"):
            handler.respond_json({"error": "No image uploaded"}, status=400); return
        if not (HAS_DEPS and HAS_ORT):
            handler.respond_json({"error": "Upscaler needs numpy, Pillow and onnxruntime. "
                                  + (DEPS_ERROR or ORT_ERROR)}, status=500); return
        opts = {
            "model": field("model"),
            "target_scale": _num(field("scale"), 0.0),
            "max_w": int(_num(field("max_w"), 0)),
            "max_h": int(_num(field("max_h"), 0)),
            "blend": _clamp(field("blend"), 0.0, 0.9),
            "save_to_folder": field("save_to_folder") == "1",
            "source_name": item.get("filename") or "image.png",
        }
        with self._lock:
            self._cleanup_jobs_locked()
            self._counter += 1
            job_id = str(self._counter)
            now = time.time()
            self._jobs[job_id] = {"state": "starting", "pct": 0,
                                  "created": now, "updated": now}
        t = threading.Thread(target=self._run_one_job,
                             args=(job_id, item["data"], opts), daemon=True)
        t.start()
        handler.respond_json({"ok": True, "id": job_id})

    def _run_one_job(self, job_id, data_bytes, opts):
        """Upscale one uploaded image with tile progress; stash the PNG for fetch."""
        import io
        try:
            up = self._get_upscaler(opts.get("model", ""))
            src = Image.open(io.BytesIO(data_bytes))

            def prog(d, t):
                if self._cancel_requested(job_id):
                    raise UpscaleCancelled()
                self._set(job_id, state="running", pct=int(d / max(t, 1) * 100))

            out = _upscale_pil(up, src,
                               int(self.setting("tile_size", 256) or 0),
                               int(self.setting("tile_pad", 16) or 0),
                               target_scale=opts.get("target_scale", 0.0),
                               max_w=opts.get("max_w", 0), max_h=opts.get("max_h", 0),
                               progress=prog, blend=opts.get("blend", 0.0))
            buf = io.BytesIO()
            pnginfo = _pnginfo_from_image(src)
            out.save(buf, format="PNG", **({"pnginfo": pnginfo} if pnginfo else {}))
            saved_path = ""
            if opts.get("save_to_folder"):
                out_dir = (self.setting("output_folder", "") or "").strip()
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                    suffix = self.setting("suffix", "_upscaled") or "_upscaled"
                    stem = _safe_stem(opts.get("source_name"), "image")
                    saved_path = _unique_path(os.path.join(out_dir, f"{stem}{suffix}.png"))
                    _save_image_atomic(out, saved_path, source_img=src)
            with self._lock:
                self._results[job_id] = buf.getvalue()
            self._set(job_id, state="done", pct=100, saved_path=saved_path)
        except UpscaleCancelled:
            self._set(job_id, state="cancelled")
        except Exception as e:
            self._set(job_id, state="failed", error=str(e))

    def _api_one_result(self, handler, qs):
        jid = qs.get("id", [""])[0]
        with self._lock:
            self._cleanup_jobs_locked()
            data = self._results.pop(jid, None)
            if data is not None:
                self._jobs.pop(jid, None)
        if data is None:
            handler.respond_json({"error": "Result not ready"}, status=404); return
        handler.respond_binary(data, "image/png")

    # ─── Folder batch (background job + progress) ──────────────────────────────
    def _api_run(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len) or {}
        src = (data.get("input") or "").strip()
        model = data.get("model") or ""
        if not src or not os.path.exists(src):
            handler.respond_json({"error": "Input folder not found"}, status=400); return
        out_dir = (data.get("output") or self.setting("output_folder", "") or "").strip()
        if os.path.isfile(src):
            files = [src] if os.path.splitext(src)[1].lower() in IMAGE_EXTS else []
        else:
            files = sorted(
                os.path.join(src, f) for f in os.listdir(src)
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
        if not files:
            handler.respond_json({"error": "No images found"}, status=400); return
        opts = {
            "target_scale": _num(data.get("scale"), 0.0),
            "max_w": int(_num(data.get("max_w"), 0)),
            "max_h": int(_num(data.get("max_h"), 0)),
            "blend": _clamp(data.get("blend"), 0.0, 0.9),
            "existing": data.get("existing") if data.get("existing") in ("skip", "overwrite") else "skip",
        }
        with self._lock:
            self._cleanup_jobs_locked()
            self._counter += 1
            job_id = str(self._counter)
            now = time.time()
            self._jobs[job_id] = {
                "state": "starting", "total": len(files), "done": 0, "pct": 0,
                "saved": 0, "skipped": 0, "created": now, "updated": now,
            }
        t = threading.Thread(target=self._run_job, args=(job_id, files, out_dir, model, opts), daemon=True)
        t.start()
        handler.respond_json({"ok": True, "id": job_id, "count": len(files)})

    def _api_cancel(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len) or {}
        jid = str(data.get("id") or "")
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                handler.respond_json({"error": "Unknown job"}, status=404)
                return
            if job.get("state") in ("done", "failed", "cancelled"):
                handler.respond_json({"ok": True, "state": job.get("state")})
                return
            job["state"] = "cancel_requested"
        handler.respond_json({"ok": True, "state": "cancel_requested"})

    def _api_status(self, handler, qs):
        jid = qs.get("id", [""])[0]
        with self._lock:
            self._cleanup_jobs_locked()
            handler.respond_json(dict(self._jobs.get(jid) or {"state": "unknown"}))

    def _set(self, jid, **kw):
        with self._lock:
            if jid in self._jobs:
                kw["updated"] = time.time()
                self._jobs[jid].update(kw)

    def _cleanup_jobs_locked(self):
        now = time.time()
        for jid, job in list(self._jobs.items()):
            updated = float(job.get("updated") or job.get("created") or now)
            state = job.get("state")
            has_result = jid in self._results
            ttl = RESULT_TTL_SECONDS if has_result else JOB_TTL_SECONDS
            if (state in DONE_STATES or has_result) and now - updated > ttl:
                self._results.pop(jid, None)
                self._jobs.pop(jid, None)

    def _cancel_requested(self, jid):
        with self._lock:
            return (self._jobs.get(jid) or {}).get("state") == "cancel_requested"

    def _run_job(self, job_id, files, out_dir, model, opts=None):
        opts = opts or {}
        try:
            up = self._get_upscaler(model)
        except Exception as e:
            self._set(job_id, state="failed", error=str(e)); return
        tile = int(self.setting("tile_size", 256) or 0)
        pad = int(self.setting("tile_pad", 16) or 0)
        suffix = self.setting("suffix", "_upscaled") or "_upscaled"
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                self._set(job_id, state="failed", error=f"Cannot create output folder: {e}"); return

        done = 0
        saved = []
        skipped = 0
        self._set(job_id, state="running")
        for path in files:
            if self._cancel_requested(job_id):
                self._set(job_id, state="cancelled", done=done, saved=len(saved), skipped=skipped)
                return
            try:
                def prog(d, t):
                    if self._cancel_requested(job_id):
                        raise UpscaleCancelled()
                    overall = (done + d / max(t, 1)) / len(files)
                    self._set(job_id, pct=int(overall * 100), file=os.path.basename(path))

                stem, ext = os.path.splitext(os.path.basename(path))
                ext = ext if ext.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png"
                dest_dir = out_dir or os.path.dirname(path)
                dest_path = os.path.join(dest_dir, f"{stem}{suffix}{ext}")
                if os.path.exists(dest_path) and opts.get("existing") == "skip":
                    skipped += 1
                    self._set(job_id, skipped=skipped, file=os.path.basename(path))
                    done += 1
                    self._set(job_id, done=done, pct=int(done / len(files) * 100))
                    continue

                with Image.open(path) as src_img:
                    out_img = _upscale_pil(up, src_img, tile, pad,
                                           target_scale=opts.get("target_scale", 0.0),
                                           max_w=opts.get("max_w", 0), max_h=opts.get("max_h", 0),
                                           progress=prog, blend=opts.get("blend", 0.0))
                    _save_image_atomic(out_img, dest_path, source_img=src_img)
                saved.append(dest_path)
            except UpscaleCancelled:
                self._set(job_id, state="cancelled", done=done, saved=len(saved), skipped=skipped)
                return
            except Exception as e:
                self._set(job_id, last_error=f"{os.path.basename(path)}: {e}")
            done += 1
            self._set(job_id, done=done, pct=int(done / len(files) * 100), saved=len(saved), skipped=skipped)
        self._set(job_id, state="done", pct=100, saved=len(saved), skipped=skipped)


PAGE_BODY = r"""
<style>
.up-wrap{max-width:1120px;margin:0 auto;padding:24px}
.up-wrap h2{font-size:18px;color:var(--text-bright);margin:0 0 4px}
.up-sub{color:var(--text-dim);font-size:13px;margin:0 0 18px}
.up-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;padding:18px;margin-bottom:14px}
.up-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.up-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim)}
.up-row{display:flex;gap:8px}
.up-input,.up-select{flex:1;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:9px 11px;font:inherit;font-size:13px}
.up-btn{border:1px solid transparent;border-radius:6px;background:var(--accent);color:#fff;padding:9px 16px;font:inherit;font-weight:600;font-size:13px;cursor:pointer}
.up-btn:disabled{opacity:.6;cursor:default}
.up-btn.secondary{background:var(--bg-card);border-color:var(--border);color:var(--text)}
.up-btn.danger{background:#7f1d1d;border-color:#991b1b;color:#fff}
.up-status{font-size:13px;color:var(--text-dim);margin-top:8px;min-height:18px}
.up-bar{height:6px;background:var(--bg-card);border-radius:4px;overflow:hidden;margin-top:10px;display:none}
.up-bar > div{height:100%;width:0;background:var(--accent);transition:width .3s}
.up-qprogress{display:none;margin-top:12px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px}
.up-qprogress.active{display:block}
.up-qprogress-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;color:var(--text-dim);font-size:12px}
.up-qprogress-head strong{color:var(--text);font-weight:600}
.up-qprogress-track{height:8px;border-radius:999px;background:var(--bg-input);overflow:hidden}
.up-qprogress-fill{height:100%;width:0;background:linear-gradient(90deg,var(--accent),#60a5fa);transition:width .35s ease}
.up-note{font-size:12px;color:var(--text-dim);line-height:1.5}
.up-note code{font-family:var(--mono);background:var(--bg-card);padding:1px 5px;border-radius:4px}
.up-mode{display:flex;gap:4px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:3px;margin-bottom:14px}
.up-mode button{flex:1;height:32px;border:0;border-radius:6px;background:transparent;color:var(--text-dim);font:12px var(--font);font-weight:600;cursor:pointer}
.up-mode button.active{background:var(--bg-active);color:var(--accent)}
.up-scale{display:flex;gap:4px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:3px}
.up-scale button{flex:1;height:32px;border:0;border-radius:6px;background:transparent;color:var(--text-dim);font:12px var(--font);font-weight:600;cursor:pointer}
.up-scale button.active{background:var(--bg-active);color:var(--accent)}
.up-drop{border:2px dashed var(--border-light);border-radius:8px;padding:40px 16px;text-align:center;color:var(--text-dim);cursor:pointer;background:var(--bg-card);transition:all .2s}
.up-drop:hover,.up-drop.dragover{border-color:var(--accent);background:var(--bg-active)}
.up-drop .ic{font-size:30px}
.up-drop input{display:none}
.up-preview{margin-top:12px;text-align:center}
.up-preview img{max-width:100%;max-height:340px;border-radius:8px;border:1px solid var(--border)}
.up-presets{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:10px}
.up-preset{border:1px solid var(--border);border-radius:6px;background:var(--bg-card);color:var(--text);padding:8px 7px;cursor:pointer;text-align:left;font:inherit}
.up-preset:hover{border-color:var(--accent-dim);background:var(--bg-active)}
.up-preset strong{display:block;font-size:12px;color:var(--text-bright)}
.up-preset span{display:block;font-size:10px;color:var(--text-dim);margin-top:2px}
.up-queue-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:12px}
.up-view-toggle{display:flex;gap:3px;background:var(--bg-card);border:1px solid var(--border);border-radius:7px;padding:2px}
.up-view-toggle button{border:0;background:transparent;color:var(--text-dim);border-radius:5px;padding:5px 8px;cursor:pointer;font:12px var(--font)}
.up-view-toggle button.active{background:var(--bg-active);color:var(--accent)}
.up-queue{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:8px;max-height:260px;overflow:auto}
.up-queue.list{display:flex;flex-direction:column}
.up-qitem{border:1px solid var(--border);border-radius:8px;background:var(--bg-card);padding:6px;cursor:pointer;min-width:0}
.up-qitem.active{border-color:var(--accent);background:var(--bg-active)}
.up-qitem img{width:100%;aspect-ratio:1.2;object-fit:cover;border-radius:5px;display:block;background:var(--bg-input)}
.up-queue.list .up-qitem{display:grid;grid-template-columns:54px 1fr auto;align-items:center;gap:8px}
.up-queue.list .up-qitem img{width:54px;height:42px;aspect-ratio:auto}
.up-qname{font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:5px}
.up-queue.list .up-qname{margin-top:0}
.up-qstatus{font-size:10px;color:var(--text-dim);margin-top:3px;text-transform:capitalize}
.up-qitem.processing .up-qstatus{color:var(--accent)}
.up-qitem.done .up-qstatus{color:#4ade80}
.up-qitem.error .up-qstatus{color:#f87171}
.up-compare{position:relative;width:100%;height:420px;max-width:100%;margin:12px auto 0;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--bg-card);cursor:ew-resize;--pos:50%;--zoom:1;--pan-x:0px;--pan-y:0px}
.up-compare.zoomed{cursor:grab}
.up-compare.zoomed.dragging{cursor:grabbing}
.up-compare img{position:absolute;inset:0;width:100%;height:100%;max-width:none;max-height:none;object-fit:contain;user-select:none;-webkit-user-drag:none;pointer-events:none;transform:translate(var(--pan-x),var(--pan-y)) scale(var(--zoom));transform-origin:center center}
.up-compare .up-after{clip-path:inset(0 0 0 var(--pos))}
.up-compare .up-divider{position:absolute;top:0;bottom:0;left:var(--pos);width:2px;background:var(--accent);box-shadow:0 0 0 1px rgba(0,0,0,.25);transform:translateX(-1px);z-index:3;cursor:ew-resize}
.up-compare .up-handle{position:absolute;left:var(--pos);top:50%;width:34px;height:34px;border-radius:50%;transform:translate(-50%,-50%);background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700;box-shadow:0 6px 18px rgba(0,0,0,.4);z-index:4;cursor:ew-resize}
.up-compare .up-badge{position:absolute;top:10px;padding:4px 8px;border-radius:999px;background:rgba(0,0,0,.58);color:#fff;font-size:10px;text-transform:uppercase;letter-spacing:.5px;z-index:3}
.up-compare .up-badge.left{left:10px}
.up-compare .up-badge.right{right:10px}
.up-zoom-tools{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);display:flex;align-items:center;gap:4px;background:rgba(0,0,0,.58);border:1px solid rgba(255,255,255,.12);border-radius:999px;padding:4px;z-index:4}
.up-zoom-tools button{width:28px;height:26px;border:0;border-radius:999px;background:rgba(255,255,255,.08);color:#fff;font:700 13px var(--font);cursor:pointer}
.up-zoom-tools button:hover{background:rgba(255,255,255,.18)}
.up-zoom-tools span{min-width:44px;text-align:center;color:#fff;font:11px var(--mono)}
@media(max-width:720px){.up-presets{grid-template-columns:1fr 1fr}.up-queue.list .up-qitem{grid-template-columns:46px 1fr}}
.up-pane{display:none}
.up-pane.active{display:block}
</style>

<div class="up-wrap">
  <h2>Upscaler</h2>
  <p class="up-sub">Neural upscaling with your ESRGAN / Real-ESRGAN ONNX models — drop a single image or batch a whole folder.</p>

  <div class="up-card">
    <div class="up-field">
      <span class="up-label">Upscaler model</span>
      <div class="up-row">
        <select class="up-select" id="upModel"><option>Loading…</option></select>
        <button class="up-btn secondary" onclick="upLoadModels()" title="Refresh list">↻</button>
      </div>
      <div class="up-status" id="upModelInfo"></div>
    </div>
  </div>

  <div class="up-card">
    <div class="up-field" style="margin-bottom:14px">
      <span class="up-label">Scale factor</span>
      <div class="up-scale" id="upScaleBtns">
        <button class="active" data-s="0">Native</button>
        <button data-s="1">1×</button>
        <button data-s="1.5">1.5×</button>
        <button data-s="2">2×</button>
        <button data-s="3">3×</button>
        <button data-s="4">4×</button>
      </div>
      <div class="up-note" style="margin-top:6px">"Native" uses the model's own factor. With 1× models, Native keeps the original size. Fixed values resize the result to that multiple of the original (Lanczos).</div>
      <div class="up-presets">
        <button class="up-preset" data-w="1024" data-h="1024"><strong>SDXL Square</strong><span>1024 × 1024</span></button>
        <button class="up-preset" data-w="1216" data-h="832"><strong>SDXL Landscape</strong><span>1216 × 832</span></button>
        <button class="up-preset" data-w="832" data-h="1216"><strong>SDXL Portrait</strong><span>832 × 1216</span></button>
        <button class="up-preset" data-w="3840" data-h="2160"><strong>4K</strong><span>3840 × 2160</span></button>
        <button class="up-preset" data-w="1920" data-h="1080"><strong>Full HD</strong><span>1920 × 1080</span></button>
        <button class="up-preset" data-w="1280" data-h="720"><strong>720p</strong><span>1280 × 720</span></button>
      </div>
    </div>
    <div class="up-row">
      <div class="up-field" style="flex:1;margin:0">
        <span class="up-label">Max width (0 = no limit)</span>
        <input class="up-input" id="upMaxW" type="number" min="0" value="0">
      </div>
      <div class="up-field" style="flex:1;margin:0">
        <span class="up-label">Max height (0 = no limit)</span>
        <input class="up-input" id="upMaxH" type="number" min="0" value="0">
      </div>
    </div>
    <div class="up-field" style="margin-top:12px;margin-bottom:0">
      <span class="up-label">Blend original</span>
      <select class="up-select" id="upBlend">
        <option value="0">Off — pure AI upscale</option>
        <option value="0.3">Low — soften slightly</option>
        <option value="0.5">Medium — balanced</option>
        <option value="0.7">High — keep more original texture</option>
      </select>
      <div class="up-note" style="margin-top:6px">Blending mixes some original image back in. Useful when a model looks too sharp or too plastic.</div>
    </div>
  </div>

  <div class="up-mode">
    <button id="upTabOne" class="active" onclick="upSetMode('one')">Single image</button>
    <button id="upTabFolder" onclick="upSetMode('folder')">Folder (batch)</button>
  </div>

  <!-- Single image (drag/drop, downloads result back) -->
  <div class="up-pane active" id="upPaneOne">
    <div class="up-card">
      <div class="up-drop" id="upDrop">
        <div class="ic">🖼</div>
        <div>Drop images here, or click to choose</div>
        <div class="up-note" style="margin-top:6px">JPG · PNG · WebP · BMP · TIFF — queue processes one image at a time</div>
        <input type="file" id="upFile" accept="image/*" multiple>
      </div>
      <div class="up-queue-head">
        <div class="up-note" id="upQueueInfo">No images queued.</div>
        <div class="up-view-toggle">
          <button class="active" id="upThumbView" onclick="upSetQueueView('thumb')">Thumbs</button>
          <button id="upListView" onclick="upSetQueueView('list')">List</button>
        </div>
      </div>
      <div class="up-queue" id="upQueue"></div>
      <div class="up-row" style="margin-top:12px">
        <button class="up-btn" id="upProcessQueue" onclick="upProcessQueue()" disabled>Upscale queue</button>
        <button class="up-btn secondary" onclick="upClearQueue()">Clear queue</button>
      </div>
      <div class="up-qprogress" id="upQueueProgress">
        <div class="up-qprogress-head">
          <span id="upQueueProgressText">Ready</span>
          <strong id="upQueueEta">ETA --</strong>
        </div>
        <div class="up-qprogress-track"><div class="up-qprogress-fill" id="upQueueProgressFill"></div></div>
      </div>
      <div class="up-preview" id="upPrev"></div>
      <div class="up-status" id="upOneStatus"></div>
    </div>
  </div>

  <!-- Folder batch (server-side, saved to output folder) -->
  <div class="up-pane" id="upPaneFolder">
    <div class="up-card">
      <div class="up-field">
        <span class="up-label">Input folder</span>
        <div class="up-row">
          <input class="up-input" id="upIn" placeholder="/path/to/folder">
          <button class="up-btn secondary" onclick="upBrowse('upIn')">Browse</button>
        </div>
      </div>
      <div class="up-field">
        <span class="up-label">Output folder (optional — defaults to Settings, or next to source)</span>
        <div class="up-row">
          <input class="up-input" id="upOut" placeholder="(uses the Settings output folder)">
          <button class="up-btn secondary" onclick="upBrowse('upOut')">Browse</button>
        </div>
      </div>
      <div class="up-field">
        <span class="up-label">Existing output files</span>
        <select class="up-select" id="upExisting">
          <option value="skip">Skip existing files</option>
          <option value="overwrite">Overwrite existing files</option>
        </select>
      </div>
      <div class="up-row">
        <button class="up-btn" id="upRun" onclick="upRunFolder()">Upscale folder</button>
        <button class="up-btn danger" id="upCancel" onclick="upCancelFolder()" style="display:none">Cancel</button>
      </div>
      <div class="up-status" id="upStatus"></div>
      <div class="up-bar" id="upBar"><div></div></div>
    </div>
  </div>

  <div class="up-card up-note" id="upHelp" style="display:none">
    <strong>No models folder set.</strong> Open <a class="cb-link" href="/settings">Settings → Upscaler</a> and set
    <code>Upscaler models folder</code> to a folder containing ONNX upscalers. Community ONNX exports of
    <code>RealESRGAN_x4plus</code> (general) and <code>RealESRGAN_x4plus_anime_6B</code> (anime) work well — drop
    several .onnx files in there and switch between them above.
  </div>
</div>

<script>
function upModel(){ return document.getElementById('upModel').value; }

var upScaleVal = 0;
function upSetScaleValue(value){
  upScaleVal = Number(value) || 0;
  document.querySelectorAll('#upScaleBtns button').forEach(function(b){
    b.classList.toggle('active', Number(b.getAttribute('data-s')) === upScaleVal);
  });
}
(function(){
  var btns = document.querySelectorAll('#upScaleBtns button');
  btns.forEach(function(b){ b.onclick = function(){ upSetScaleValue(b.getAttribute('data-s')); }; });
  document.querySelectorAll('.up-preset').forEach(function(b){
    b.onclick = function(){ upApplyPreset(Number(b.dataset.w), Number(b.dataset.h)); };
  });
})();
function upMaxW(){ return parseInt(document.getElementById('upMaxW').value, 10) || 0; }
function upMaxH(){ return parseInt(document.getElementById('upMaxH').value, 10) || 0; }
function upBlend(){ return Number(document.getElementById('upBlend').value) || 0; }
var upSingleJob = '';
var upFolderJob = '';
var upModelHints = {};
var upQueue = [];
var upQueueView = 'thumb';
var upSelected = -1;
var upProcessingQueue = false;
var upQueueStartedAt = 0;
var upEtaAnchor = null;       // {t, f}: progress reference taken after warm-up, for a stable ETA
var upQueueRunTotal = 0;
var upQueueRunDoneAtStart = 0;
var upCompareResizeHandler = null;

function upStoreGet(key, fallback){
  try { return localStorage.getItem(key) || fallback; } catch(e) { return fallback; }
}

function upStoreSet(key, value){
  try { localStorage.setItem(key, value); } catch(e) {}
}

function upLoadModels(){
  fetch('/api/upscaler/models').then(function(r){return r.json();}).then(function(d){
    var sel = document.getElementById('upModel'), info = document.getElementById('upModelInfo');
    if (!d.ok){
      sel.innerHTML = '<option value="">(none)</option>';
      info.textContent = d.error || '';
      document.getElementById('upHelp').style.display = '';
      return;
    }
    document.getElementById('upHelp').style.display = (d.models && d.models.length) ? 'none' : '';
    if (!d.models.length){ sel.innerHTML = '<option value="">(no .onnx models found)</option>'; info.textContent = 'Drop ONNX upscalers into the models folder.'; return; }
    upModelHints = d.hints || {};
    sel.innerHTML = d.models.map(function(m){ return '<option value="'+escAttr(m)+'">'+escHtml(m)+'</option>'; }).join('');
    var stored = upStoreGet('cyberhub.upscaler.model', '');
    if (stored && d.models.indexOf(stored) >= 0) sel.value = stored;
    function updateModelInfo(){
      var hint = upModelHints[sel.value] || '';
      info.textContent = d.models.length + ' model(s) available · ' + sel.value + (hint ? ' · ' + hint : '');
    }
    updateModelInfo();
    sel.onchange = function(){ upStoreSet('cyberhub.upscaler.model', sel.value); updateModelInfo(); };
  }).catch(function(){ document.getElementById('upModelInfo').textContent = 'Could not list models.'; });
}

function upSetMode(m){
  document.getElementById('upTabOne').classList.toggle('active', m==='one');
  document.getElementById('upTabFolder').classList.toggle('active', m==='folder');
  document.getElementById('upPaneOne').classList.toggle('active', m==='one');
  document.getElementById('upPaneFolder').classList.toggle('active', m==='folder');
}

/* ── Single image queue: drag/drop + click ── */
var upDrop = document.getElementById('upDrop'), upFile = document.getElementById('upFile');
upDrop.onclick = function(){ upFile.click(); };
upFile.onchange = function(){ if (upFile.files.length) upAddFiles(Array.from(upFile.files)); upFile.value=''; };
['dragenter','dragover'].forEach(function(e){ upDrop.addEventListener(e, function(ev){ ev.preventDefault(); upDrop.classList.add('dragover'); }); });
['dragleave','drop'].forEach(function(e){ upDrop.addEventListener(e, function(ev){ ev.preventDefault(); upDrop.classList.remove('dragover'); }); });
upDrop.addEventListener('drop', function(ev){ var files = Array.from(ev.dataTransfer.files || []).filter(function(f){ return f.type.indexOf('image/') === 0 || /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(f.name); }); if (files.length) upAddFiles(files); });

function upAddFiles(files){
  files.forEach(function(file){
    var item = { file:file, name:file.name, status:'queued', inputUrl:URL.createObjectURL(file), outputUrl:'', error:'' };
    upQueue.push(item);
  });
  if (upSelected < 0 && upQueue.length) upSelected = 0;
  upRenderQueue();
  upRenderSelected();
}

function upSetQueueView(view){
  upQueueView = view === 'list' ? 'list' : 'thumb';
  document.getElementById('upThumbView').classList.toggle('active', upQueueView === 'thumb');
  document.getElementById('upListView').classList.toggle('active', upQueueView === 'list');
  upRenderQueue();
}

function upRenderQueue(){
  var q = document.getElementById('upQueue');
  q.classList.toggle('list', upQueueView === 'list');
  document.getElementById('upProcessQueue').disabled = !upQueue.length || upProcessingQueue;
  var done = upQueue.filter(function(x){ return x.status === 'done'; }).length;
  var errors = upQueue.filter(function(x){ return x.status === 'error'; }).length;
  document.getElementById('upQueueInfo').textContent = upQueue.length
    ? upQueue.length + ' queued · ' + done + ' done' + (errors ? ' · ' + errors + ' errors' : '')
    : 'No images queued.';
  if (!upQueue.length){ q.innerHTML = ''; return; }
  q.innerHTML = upQueue.map(function(item, i){
    return '<div class="up-qitem ' + escAttr(item.status) + (i === upSelected ? ' active' : '') + '" onclick="upSelectItem(' + i + ')">' +
      '<img src="' + escAttr(item.inputUrl) + '" alt="">' +
      '<div><div class="up-qname" title="' + escAttr(item.name) + '">' + escHtml(item.name) + '</div>' +
      '<div class="up-qstatus">' + escHtml(item.status) + (item.error ? ': ' + escHtml(item.error) : '') + '</div></div>' +
      '</div>';
  }).join('');
  upUpdateQueueProgress();
}

function upSelectItem(index){
  upSelected = index;
  upRenderQueue();
  upRenderSelected();
}

function upClearQueue(){
  if (upProcessingQueue) return;
  upQueue.forEach(function(item){
    if (item.inputUrl) URL.revokeObjectURL(item.inputUrl);
    if (item.outputUrl) URL.revokeObjectURL(item.outputUrl);
  });
  upQueue = [];
  upSelected = -1;
  upRenderQueue();
  upRenderSelected();
  upResetQueueProgress();
  document.getElementById('upOneStatus').textContent = '';
}

function upRenderSelected(){
  var item = upQueue[upSelected];
  var box = document.getElementById('upPrev');
  if (!item){ box.innerHTML = ''; return; }
  if (item.outputUrl){
    box.innerHTML = '<div class="up-compare" id="upCompare">' +
      '<img class="up-before" src="' + escAttr(item.inputUrl) + '" alt="Original">' +
      '<img class="up-after" src="' + escAttr(item.outputUrl) + '" alt="Upscaled">' +
      '<span class="up-badge left">Old / original</span><span class="up-badge right">New / upscaled</span>' +
      '<div class="up-divider"></div><div class="up-handle">↔</div>' +
      '<div class="up-zoom-tools"><button type="button" onclick="upZoomCompare(-0.25)">−</button><span id="upZoomLabel">100%</span><button type="button" onclick="upZoomCompare(0.25)">+</button><button type="button" onclick="upResetCompareZoom()">Fit</button></div>' +
      '</div>';
    upBindCompare();
  } else {
    box.innerHTML = '<img src="' + escAttr(item.inputUrl) + '">';
  }
}

function upBindCompare(){
  var cmp = document.getElementById('upCompare');
  if (!cmp) return;
  var after = cmp.querySelector('.up-after');
  cmp._zoom = 1;
  cmp._panX = 0;
  cmp._panY = 0;
  upApplyCompareZoom(cmp);
  function sizeCompare(){
    var img = after || cmp.querySelector('img');
    if (!img || !img.naturalWidth || !img.naturalHeight) return;
    var parentW = cmp.parentElement ? cmp.parentElement.clientWidth : cmp.clientWidth;
    var ratio = img.naturalWidth / Math.max(img.naturalHeight, 1);
    var maxH = Math.max(420, Math.min(window.innerHeight * 0.82, 860));
    var width = Math.min(parentW, Math.round(maxH * ratio));
    var height = Math.round(width / ratio);
    if (height > maxH) {
      height = maxH;
      width = Math.round(height * ratio);
    }
    cmp.style.width = Math.max(220, width) + 'px';
    cmp.style.height = Math.max(260, height) + 'px';
  }
  if (after && after.complete) sizeCompare();
  if (after) after.addEventListener('load', sizeCompare, {once:true});
  if (upCompareResizeHandler) window.removeEventListener('resize', upCompareResizeHandler);
  upCompareResizeHandler = function(){ if (document.getElementById('upCompare') === cmp) sizeCompare(); };
  window.addEventListener('resize', upCompareResizeHandler);
  function setFromEvent(ev){
    var rect = cmp.getBoundingClientRect();
    var pct = Math.max(0, Math.min(100, ((ev.clientX - rect.left) / Math.max(rect.width, 1)) * 100));
    cmp.style.setProperty('--pos', pct.toFixed(2) + '%');
  }
  var dragging = false;
  var panStart = null;
  cmp.addEventListener('pointerdown', function(ev){
    if (ev.target.closest('.up-zoom-tools')) return;
    dragging = true;
    cmp.setPointerCapture(ev.pointerId);
    var dragSlider = ev.target.closest('.up-handle,.up-divider') || ev.shiftKey || cmp._zoom <= 1.01;
    if (!dragSlider && cmp._zoom > 1.01) {
      panStart = {x:ev.clientX, y:ev.clientY, px:cmp._panX || 0, py:cmp._panY || 0};
      cmp.classList.add('dragging');
    } else {
      panStart = null;
      setFromEvent(ev);
    }
    ev.preventDefault();
  });
  cmp.addEventListener('pointermove', function(ev){
    if (!dragging) return;
    if (panStart) {
      cmp._panX = panStart.px + ev.clientX - panStart.x;
      cmp._panY = panStart.py + ev.clientY - panStart.y;
      upApplyCompareZoom(cmp);
    } else {
      setFromEvent(ev);
    }
  });
  cmp.addEventListener('pointerup', function(){ dragging = false; panStart = null; cmp.classList.remove('dragging'); });
  cmp.addEventListener('pointercancel', function(){ dragging = false; panStart = null; cmp.classList.remove('dragging'); });
  cmp.addEventListener('wheel', function(ev){
    ev.preventDefault();
    upZoomCompare(ev.deltaY < 0 ? 0.25 : -0.25);
  }, {passive:false});
}

function upApplyCompareZoom(cmp){
  cmp = cmp || document.getElementById('upCompare');
  if (!cmp) return;
  var z = Math.max(1, Math.min(5, Number(cmp._zoom) || 1));
  if (z <= 1.01) {
    z = 1;
    cmp._panX = 0;
    cmp._panY = 0;
  }
  cmp._zoom = z;
  cmp.classList.toggle('zoomed', z > 1.01);
  cmp.style.setProperty('--zoom', z.toFixed(2));
  cmp.style.setProperty('--pan-x', Math.round(cmp._panX || 0) + 'px');
  cmp.style.setProperty('--pan-y', Math.round(cmp._panY || 0) + 'px');
  var label = document.getElementById('upZoomLabel');
  if (label) label.textContent = Math.round(z * 100) + '%';
}

function upZoomCompare(delta){
  var cmp = document.getElementById('upCompare');
  if (!cmp) return;
  cmp._zoom = Math.max(1, Math.min(5, (Number(cmp._zoom) || 1) + delta));
  upApplyCompareZoom(cmp);
}

function upResetCompareZoom(){
  var cmp = document.getElementById('upCompare');
  if (!cmp) return;
  cmp._zoom = 1;
  cmp._panX = 0;
  cmp._panY = 0;
  upApplyCompareZoom(cmp);
}

function upApplyPreset(w, h){
  document.getElementById('upMaxW').value = w || 0;
  document.getElementById('upMaxH').value = h || 0;
  var item = upQueue[upSelected];
  if (!item){
    upSetScaleValue(0);
    document.getElementById('upOneStatus').textContent = 'Preset set: max ' + w + ' × ' + h + '. Add an image to auto-pick a scale.';
    return;
  }
  var img = new Image();
  img.onload = function(){
    var scale = Math.min(w / Math.max(img.naturalWidth, 1), h / Math.max(img.naturalHeight, 1));
    var choices = [1.5, 2, 3, 4], best = 0, bestDiff = Infinity;
    choices.forEach(function(c){ var d = Math.abs(c - scale); if (d < bestDiff){ best = c; bestDiff = d; } });
    if (scale <= 1.15) best = 0;
    upSetScaleValue(best);
    document.getElementById('upOneStatus').textContent = 'Preset set: max ' + w + ' × ' + h + (best ? ', scale ' + best + '×.' : ', native model scale.');
  };
  img.src = item.inputUrl;
}

function upDownloadBlob(blob, filename){
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(function(){ URL.revokeObjectURL(url); }, 30000);
}

function upFormatDuration(seconds){
  seconds = Math.max(0, Math.round(Number(seconds) || 0));
  var m = Math.floor(seconds / 60), s = seconds % 60;
  if (m >= 60) {
    var h = Math.floor(m / 60);
    m = m % 60;
    return h + 'h ' + (m ? m + 'm' : '');
  }
  return m ? (m + 'm ' + String(s).padStart(2, '0') + 's') : (s + 's');
}

function upQueueFinishedCount(){
  return upQueue.filter(function(x){ return x.status === 'done' || x.status === 'error'; }).length;
}

function upResetQueueProgress(){
  upQueueStartedAt = 0;
  upEtaAnchor = null;
  upQueueRunTotal = 0;
  upQueueRunDoneAtStart = 0;
  var box = document.getElementById('upQueueProgress');
  if (box) box.classList.remove('active');
  var fill = document.getElementById('upQueueProgressFill');
  if (fill) fill.style.width = '0%';
  var text = document.getElementById('upQueueProgressText');
  if (text) text.textContent = 'Ready';
  var eta = document.getElementById('upQueueEta');
  if (eta) eta.textContent = 'ETA --';
}

function upUpdateQueueProgress(label){
  var box = document.getElementById('upQueueProgress');
  if (!box) return;
  if (!upQueue.length && !upProcessingQueue) { upResetQueueProgress(); return; }
  var finished = upQueueFinishedCount();
  var runDone = Math.max(0, finished - upQueueRunDoneAtStart);
  var runTotal = upQueueRunTotal || (upProcessingQueue ? Math.max(1, upQueue.length - upQueueRunDoneAtStart) : upQueue.length);
  // Include the in-flight image's tile progress so the bar (and ETA) advance
  // smoothly *within* each image, not just per finished image.
  var processing = upQueue.filter(function(x){ return x.status === 'processing'; })[0];
  var curFrac = processing ? (Number(processing.pct) || 0) / 100 : 0;
  var effDone = Math.min(runTotal, runDone + curFrac);
  var frac = runTotal ? effDone / runTotal : 0;
  box.classList.toggle('active', upProcessingQueue || finished > 0);
  document.getElementById('upQueueProgressFill').style.width = Math.round(frac * 100) + '%';
  document.getElementById('upQueueProgressText').textContent = label || (
    upProcessingQueue ? ('Processing ' + Math.min(runDone + 1, runTotal) + ' of ' + runTotal) : (finished + ' of ' + upQueue.length + ' finished')
  );
  var eta = 'ETA --';
  if (upProcessingQueue) {
    // Anchor the rate once we're past the first ~5% (the one-time model warm-up
    // happens in the first tile/image), then linearly extrapolate. Works for a
    // single image and for a whole queue alike.
    if (!upEtaAnchor && frac >= 0.05) upEtaAnchor = { t: Date.now(), f: frac };
    if (frac >= 1) {
      eta = 'ETA 0s';
    } else if (upEtaAnchor && frac > upEtaAnchor.f) {
      var secPerFrac = ((Date.now() - upEtaAnchor.t) / 1000) / (frac - upEtaAnchor.f);
      eta = 'ETA ' + upFormatDuration(secPerFrac * (1 - frac));
    } else {
      eta = 'ETA estimating…';
    }
  } else if (finished && finished >= upQueue.length) {
    eta = 'Complete';
  }
  document.getElementById('upQueueEta').textContent = eta;
}

function upProcessQueue(){
  if (!upModel()){ document.getElementById('upOneStatus').textContent = 'Pick a model first.'; return; }
  if (!upQueue.length || upProcessingQueue) return;
  upQueue.forEach(function(item){
    if (item.status === 'error') {
      item.status = 'queued';
      item.error = '';
    }
  });
  upQueueRunDoneAtStart = upQueue.filter(function(x){ return x.status === 'done'; }).length;
  upQueueRunTotal = upQueue.length - upQueueRunDoneAtStart;
  if (upQueueRunTotal <= 0) {
    upRenderQueue();
    upUpdateQueueProgress('Queue already done.');
    document.getElementById('upOneStatus').textContent = 'Queue already done.';
    return;
  }
  upProcessingQueue = true;
  upQueueStartedAt = Date.now();
  document.getElementById('upProcessQueue').disabled = true;
  upRenderQueue();
  upUpdateQueueProgress('Starting queue...');
  upProcessNext(0);
}

function upProcessNext(index){
  while (index < upQueue.length && upQueue[index].status === 'done') index++;
  if (index >= upQueue.length){
    upProcessingQueue = false;
    upRenderQueue();
    document.getElementById('upOneStatus').textContent = 'Queue done.';
    upUpdateQueueProgress('Queue done.');
    return;
  }
  upSelected = index;
  upUpdateQueueProgress('Processing ' + (Math.max(0, upQueueFinishedCount() - upQueueRunDoneAtStart) + 1) + ' of ' + Math.max(1, upQueueRunTotal));
  upDoQueuedItem(index).then(function(){ upProcessNext(index + 1); });
}

function upDoQueuedItem(index){
  var item = upQueue[index];
  if (!item) return Promise.resolve();
  item.status = 'processing'; item.error = ''; item.pct = 0;
  upRenderQueue(); upRenderSelected();
  var st = document.getElementById('upOneStatus'); st.textContent = 'Upscaling ' + item.name + ' …';
  upUpdateQueueProgress('Upscaling ' + item.name);
  var fd = new FormData(); fd.append('file', item.file); fd.append('model', upModel());
  fd.append('scale', upScaleVal); fd.append('max_w', upMaxW()); fd.append('max_h', upMaxH()); fd.append('blend', upBlend());
  fd.append('save_to_folder', upQueue.length > 1 ? '1' : '0');
  return fetch('/api/upscaler/one', {method:'POST', body: fd})
    .then(function(r){ return r.json(); })
    .then(function(d){ if (d.error || !d.id) throw new Error(d.error || 'could not start'); upSingleJob = d.id; return upPollOne(d.id, item); })
    .then(function(blob){
      if (item.outputUrl) URL.revokeObjectURL(item.outputUrl);
      item.outputUrl = URL.createObjectURL(blob);
      item.status = 'done'; item.pct = 100;
      var name = item.name.replace(/\.[^.]+$/, '') + '_upscaled.png';
      if (item.savedPath) {
        st.textContent = 'Done — saved to ' + item.savedPath;
      } else {
        upDownloadBlob(blob, name);
        st.textContent = 'Done — downloaded ' + name;
      }
      upRenderQueue(); upRenderSelected();
      upUpdateQueueProgress();
    })
    .catch(function(e){
      item.status = (e && e.message === 'cancelled') ? 'queued' : 'error';
      item.error = (item.status === 'error') ? (e.message || 'failed') : '';
      st.textContent = item.status === 'error' ? ('Error: ' + item.error) : 'Cancelled.';
      upRenderQueue(); upRenderSelected();
      upUpdateQueueProgress();
    })
    .then(function(){ upSingleJob = ''; });
}

/* Poll a single-image job for tile progress, then fetch the upscaled PNG. */
function upPollOne(id, item){
  return new Promise(function(resolve, reject){
    function tick(){
      fetch('/api/upscaler/status?id=' + encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(s){
        if (s.state === 'done'){
          item.pct = 100; item.savedPath = s.saved_path || ''; upRenderSelected(); upUpdateQueueProgress();
          fetch('/api/upscaler/one-result?id=' + encodeURIComponent(id))
            .then(function(r){ return r.ok ? r.blob() : r.json().then(function(j){ throw new Error(j.error||'no result'); }); })
            .then(resolve).catch(reject);
          return;
        }
        if (s.state === 'failed'){ reject(new Error(s.error || 'failed')); return; }
        if (s.state === 'cancelled'){ reject(new Error('cancelled')); return; }
        item.pct = s.pct || 0;
        document.getElementById('upOneStatus').textContent = 'Upscaling ' + item.name + ' … ' + (item.pct) + '%';
        upRenderSelected(); upUpdateQueueProgress();
        setTimeout(tick, 350);
      }).catch(reject);
    }
    tick();
  });
}

/* ── Folder batch ── */
function upBrowse(targetId){
  var cur = document.getElementById(targetId).value || '';
  fetch('/api/browse?path=' + encodeURIComponent(cur)).then(function(r){return r.json();}).then(function(d){
    var p = prompt('Path:', (d && d.path) || cur);
    if (p !== null) document.getElementById(targetId).value = p;
  }).catch(function(){ var p = prompt('Enter a path:', cur); if (p !== null) document.getElementById(targetId).value = p; });
}

function upRunFolder(){
  if (!upModel()){ document.getElementById('upStatus').textContent = 'Pick a model first.'; return; }
  var input = document.getElementById('upIn').value.trim();
  if (!input){ document.getElementById('upStatus').textContent = 'Enter an input folder.'; return; }
  var btn = document.getElementById('upRun'); btn.disabled = true;
  document.getElementById('upCancel').style.display = '';
  document.getElementById('upBar').style.display = 'block';
  document.getElementById('upStatus').textContent = 'Starting…';
  fetch('/api/upscaler/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      input: input,
      output: document.getElementById('upOut').value.trim(),
      model: upModel(),
      scale: upScaleVal,
      max_w: upMaxW(),
      max_h: upMaxH(),
      blend: upBlend(),
      existing: document.getElementById('upExisting').value
    })})
    .then(function(r){return r.json();})
    .then(function(d){ if (d.error || !d.id) throw new Error(d.error || 'could not start'); upFolderJob = d.id; upPoll(d.id, btn); })
    .catch(function(e){ document.getElementById('upStatus').textContent = 'Error: ' + e.message; btn.disabled = false; document.getElementById('upCancel').style.display = 'none'; });
}

function upCancelFolder(){
  if (!upFolderJob) return;
  document.getElementById('upCancel').disabled = true;
  document.getElementById('upStatus').textContent = 'Cancelling…';
  fetch('/api/upscaler/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: upFolderJob})})
    .catch(function(){});
}

function upPoll(id, btn){
  fetch('/api/upscaler/status?id=' + encodeURIComponent(id)).then(function(r){return r.json();}).then(function(s){
    document.querySelector('#upBar > div').style.width = (s.pct || 0) + '%';
    if (s.state === 'done'){ document.getElementById('upStatus').textContent = 'Done — ' + (s.saved || 0) + ' saved, ' + (s.skipped || 0) + ' skipped.' + (s.last_error ? ' Some files had errors.' : ''); btn.disabled = false; document.getElementById('upCancel').style.display = 'none'; document.getElementById('upCancel').disabled = false; upFolderJob = ''; return; }
    if (s.state === 'cancelled'){ document.getElementById('upStatus').textContent = 'Cancelled — ' + (s.saved || 0) + ' saved, ' + (s.skipped || 0) + ' skipped.'; btn.disabled = false; document.getElementById('upCancel').style.display = 'none'; document.getElementById('upCancel').disabled = false; upFolderJob = ''; return; }
    if (s.state === 'failed'){ document.getElementById('upStatus').textContent = 'Failed: ' + (s.error || ''); btn.disabled = false; document.getElementById('upCancel').style.display = 'none'; document.getElementById('upCancel').disabled = false; upFolderJob = ''; return; }
    document.getElementById('upStatus').textContent = 'Upscaling ' + (s.file || '') + '  (' + (s.done || 0) + '/' + (s.total || 0) + ')';
    setTimeout(function(){ upPoll(id, btn); }, 600);
  }).catch(function(){ document.getElementById('upStatus').textContent = 'Lost connection.'; btn.disabled = false; document.getElementById('upCancel').style.display = 'none'; document.getElementById('upCancel').disabled = false; });
}

upLoadModels();
</script>
"""
