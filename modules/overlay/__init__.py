"""Overlay module — add labels, extracted metadata text, or watermarks to image batches."""

import io
import os
import re
from pathlib import Path
from urllib.parse import quote

from core import Module
from core.metadata import get_image_metadata, parse_sd_parameters
from core.server import build_shell

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps, PngImagePlugin
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
OUTPUT_FORMATS = {"source", "PNG", "JPEG", "WEBP"}
MODULE_DIR = Path(__file__).resolve().parent
BUNDLED_FONTS = MODULE_DIR / "fonts"
FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}
PREFERRED_FONT_NAMES = [
    "arial.ttf", "arialn.ttf", "helvetica.ttc", "sfns.ttf", "sfnstext.ttf",
    "dejavusans.ttf", "liberationsans-regular.ttf", "notosans-regular.ttf",
]
STOP_WORDS = [
    "woman", "man", "girl", "boy", "person", "male", "female",
    "flower", "garden", "car", "landscape", "portrait", "location", "scene",
    "clothing", "dress", "costume", "fabric", "texture",
]


def _safe_path(root, rel_path):
    try:
        root = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(root, rel_path))
        if os.path.commonpath([root, candidate]) != root:
            return None
        return candidate
    except Exception:
        return None


def _common_prefix(strings):
    if not strings:
        return ""
    first, last = min(strings), max(strings)
    i = 0
    while i < len(first) and i < len(last) and first[i] == last[i]:
        i += 1
    return first[:i]


def _common_suffix(strings):
    return _common_prefix([s[::-1] for s in strings])[::-1] if strings else ""


def _guess_regex(strings):
    strings = [re.sub(r"\s+", " ", s.strip()) for s in strings if s and s.strip()]
    if len(strings) < 2:
        return r"(.+)"
    prefix = _common_prefix(strings).strip()
    suffix = _common_suffix(strings).strip()
    middles = []
    for value in strings:
        start = len(prefix) if prefix and value.startswith(prefix) else 0
        end = len(value) - len(suffix) if suffix and value.endswith(suffix) else len(value)
        middles.append(value[start:end].strip() if start <= end else value)
    for stop in STOP_WORDS:
        if all(re.search(rf"\b{re.escape(stop)}\b(?=[,.\s]|$)", middle, re.I) for middle in middles):
            if prefix:
                return rf"{re.escape(prefix)}\s+(.+?)\s+{re.escape(stop)}\b"
            return rf"(.+?)\s+{re.escape(stop)}\b"
    if prefix and suffix:
        return rf"^{re.escape(prefix)}\s*(.+?)\s*{re.escape(suffix)}$"
    if prefix:
        return rf"^{re.escape(prefix)}\s*(.+?)\s*$"
    if suffix:
        return rf"^\s*(.+?)\s*{re.escape(suffix)}$"
    return r"(.+)"


def _next_output_path(directory, source_stem, suffix, extension):
    candidate = directory / f"{source_stem}{suffix}{extension}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{source_stem}{suffix}_{i}{extension}"
        if not candidate.exists():
            return candidate
        i += 1


def _hex_color(value, fallback):
    try:
        return ImageColor.getrgb(str(value))
    except Exception:
        return ImageColor.getrgb(fallback)


def _font(font_path, size):
    try:
        return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def _font_label(path, source):
    label = path.stem.replace("_", " ").replace("-", " ").strip() or path.name
    return f"{label} ({source})"


def _iter_font_files(directory):
    try:
        if not directory.is_dir():
            return
        for path in sorted(directory.rglob("*"), key=lambda p: str(p).lower()):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                yield path
    except (OSError, PermissionError):
        return


def _system_font_dirs():
    home = Path.home()
    dirs = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    else:
        dirs.append(Path("C:/Windows/Fonts"))
    dirs.extend([
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        home / "Library" / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".fonts",
        home / ".local" / "share" / "fonts",
    ])
    seen = set()
    out = []
    for directory in dirs:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out


def _wrap_lines(draw, text, font, max_width):
    result = []
    for raw in str(text).split("\n"):
        words = raw.split()
        if not words:
            result.append("")
            continue
        line = words[0]
        for word in words[1:]:
            attempt = f"{line} {word}"
            box = draw.textbbox((0, 0), attempt, font=font)
            if box[2] - box[0] <= max_width:
                line = attempt
            else:
                result.append(line)
                line = word
        result.append(line)
    return result


def _text_size(draw, lines, font):
    max_width = 0
    total_height = 0
    for line in lines:
        box = draw.textbbox((0, 0), line or "Ag", font=font)
        max_width = max(max_width, box[2] - box[0])
        total_height += max(1, box[3] - box[1])
    return max_width, total_height


class OverlayModule(Module):
    name = "Overlay"
    icon = "\U0001F4DD"  # 📝
    description = "Add text labels, metadata-extracted captions, or watermarks to image batches."
    order = 32

    settings_schema = {
        "extra_font_folder": {
            "type": "folder", "override": True, "label": "Fonts folder override",
            "desc": "Optional override. Default: resources/fonts/",
            "default": "", "placeholder": "Leave empty to use resources/fonts/",
        }
    }

    def __init__(self, hub):
        super().__init__(hub)
        # Folder choices persist across restarts (saved to settings.json on pick).
        self._session_input_folder = self.setting("input_folder", "")
        self._session_output_folder = self.setting("output_folder", "")
        self._font_cache_key = None
        self._font_cache = []

    def routes_get(self):
        return {
            "/overlay": self._page,
            "/api/overlay/state": self._api_state,
        }

    def routes_post(self):
        return {
            "/api/overlay/session": self._api_session,
            "/api/overlay/preview": self._api_preview,
            "/api/overlay/process": self._api_process,
            "/api/overlay/auto_regex": self._api_auto_regex,
        }

    def prefix_routes(self):
        return {"/overlay/source/": self._serve_source}

    def _page(self, handler, qs):
        handler.respond_html(build_shell(
            self.hub.registry, self.hub.settings,
            active_key="overlay", page_title="Overlay", body_html=PAGE_BODY,
        ))

    def _input_folder(self):
        folder = self._session_input_folder.strip()
        return os.path.abspath(folder) if folder and os.path.isdir(folder) else ""

    def _output_folder(self, root):
        chosen = self._session_output_folder.strip()
        return Path(os.path.abspath(chosen)) if chosen else Path(root) / "overlay-output"

    def _list_files(self, root):
        return [p.name for p in sorted(Path(root).iterdir(), key=lambda p: p.name.lower())
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]

    def _font_options(self):
        override = str(self.setting("extra_font_folder", "") or "").strip()
        primary = Path(os.path.abspath(override)) if override else Path(self.hub.resource_path("fonts"))
        cache_key = (override, str(primary))
        if self._font_cache_key == cache_key:
            return list(self._font_cache)
        paths = []
        directories = [
            (primary, "custom" if override else "resources"),
            (BUNDLED_FONTS, "bundled"),
        ]
        directories.extend((directory, "system") for directory in _system_font_dirs())
        seen = set()
        for directory, source in directories:
            for path in _iter_font_files(directory):
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                paths.append({"label": _font_label(path, source), "value": str(path)})
        self._font_cache_key = cache_key
        self._font_cache = paths
        return list(paths)

    def _default_font_path(self):
        options = self._font_options()
        if not options:
            return ""
        by_name = {Path(item["value"]).name.lower(): item["value"] for item in options}
        for name in PREFERRED_FONT_NAMES:
            if name in by_name:
                return by_name[name]
        return options[0]["value"]

    def _font_resource_status(self):
        override = str(self.setting("extra_font_folder", "") or "").strip()
        path = os.path.abspath(override) if override else self.hub.resource_path("fonts")
        return {"path": path, "source": "override" if override else "default"}

    def _write_state(self, handler):
        root = self._input_folder()
        files = []
        if root:
            try:
                files = self._list_files(root)
            except PermissionError:
                handler.respond_json({"error": "Permission denied reading input folder"}, status=403)
                return
        handler.respond_json({
            "configured": bool(root),
            "input_folder": self._session_input_folder,
            "output_folder": self._session_output_folder,
            "effective_output_folder": str(self._output_folder(root)) if root else "",
            "files": files,
            "fonts": self._font_options(),
            "font_resource": self._font_resource_status(),
        })

    def _api_state(self, handler, qs):
        self._write_state(handler)

    def _api_session(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        source = str(data.get("input_folder") or "").strip()
        output = str(data.get("output_folder") or "").strip()
        if not source:
            handler.respond_json({"error": "Choose an input folder first"}, status=400)
            return
        source = os.path.abspath(source)
        if not os.path.isdir(source):
            handler.respond_json({"error": "Input folder not found"}, status=404)
            return
        if output:
            output = os.path.abspath(output)
            if os.path.exists(output) and not os.path.isdir(output):
                handler.respond_json({"error": "Output path is not a folder"}, status=400)
                return
        self._session_input_folder = source
        self._session_output_folder = output
        self.hub.settings.set_module_setting(self.key(), "input_folder", source)
        self.hub.settings.set_module_setting(self.key(), "output_folder", output)
        self._write_state(handler)

    def _serve_source(self, handler, rel_path):
        root = self._input_folder()
        if not root:
            handler.send_error(503)
            return
        full = _safe_path(root, rel_path)
        if not full or not os.path.isfile(full) or Path(full).suffix.lower() not in SUPPORTED_EXTS:
            handler.send_error(404)
            return
        handler.serve_file(full)

    def _source_file(self, filename):
        root = self._input_folder()
        if not root:
            raise ValueError("Choose an input folder in Overlay first")
        full = _safe_path(root, str(filename or ""))
        if not full or not os.path.isfile(full) or Path(full).suffix.lower() not in SUPPORTED_EXTS:
            raise FileNotFoundError("Source image not found")
        return full

    def _positive_prompt(self, filepath):
        meta = get_image_metadata(filepath)
        raw = meta.get("parameters") or ""
        if raw:
            return parse_sd_parameters(raw).get("prompt", raw)
        prompt = meta.get("prompt") or ""
        if isinstance(prompt, str):
            parsed = parse_sd_parameters(prompt)
            return parsed.get("prompt", prompt)
        return ""

    def _build_text(self, filepath, options):
        mode = str(options.get("mode") or "static")
        if mode == "static":
            return str(options.get("static_text") or "")
        positive = re.sub(r"\s+", " ", self._positive_prompt(filepath).strip())
        pattern = str(options.get("regex") or r"(.+)")
        try:
            match = re.search(pattern, positive, flags=re.I)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}")
        extracted = ""
        if match:
            try:
                group = int(options.get("group", 1))
            except Exception:
                group = 1
            if group == 0:
                extracted = match.group(0)
            elif match.lastindex and group <= match.lastindex:
                extracted = match.group(group)
            else:
                extracted = match.group(0)
        parts = [str(options.get("prefix") or "").strip(), extracted.strip(), str(options.get("suffix") or "").strip()]
        return " ".join(p for p in parts if p)

    def _render(self, filepath, options, preview=False):
        if not HAS_PIL:
            raise RuntimeError("Pillow is required for Overlay")
        with Image.open(filepath) as source:
            base = ImageOps.exif_transpose(source).convert("RGBA")
        original_size = base.size
        scale = 1.0
        if preview and max(base.size) > 1500:
            scale = 1500 / max(base.size)
            base = base.resize((max(1, int(base.width * scale)), max(1, int(base.height * scale))), Image.Resampling.LANCZOS)
        text = self._build_text(filepath, options)
        rect = options.get("box") or {}
        x = int(float(rect.get("x", 50)) * scale)
        y = int(float(rect.get("y", 50)) * scale)
        w = max(20, int(float(rect.get("w", 420)) * scale))
        h = max(20, int(float(rect.get("h", 110)) * scale))
        font_size = max(8, int(float(options.get("font_size", 38)) * scale))
        pad_x = max(0, int(float(options.get("pad_x", 16)) * scale))
        pad_y = max(0, int(float(options.get("pad_y", 10)) * scale))
        font_path = str(options.get("font_path") or "")
        allowed_fonts = {item["value"] for item in self._font_options()}
        if font_path and font_path not in allowed_fonts:
            font_path = ""
        font = _font(font_path or self._default_font_path(), font_size)
        bg = _hex_color(options.get("bg_color", "#1A1A2E"), "#1A1A2E")
        fg = _hex_color(options.get("text_color", "#FFFFFF"), "#FFFFFF")
        alpha = max(0, min(255, int(options.get("bg_alpha", 200))))
        auto_width = bool(options.get("auto_width", True))
        auto_height = bool(options.get("auto_height", True))
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        lines = _wrap_lines(draw, text, font, max(1, (base.width if auto_width else w - 2 * pad_x)))
        text_w, text_h = _text_size(draw, lines, font)
        if auto_width:
            w = max(20, text_w + 2 * pad_x)
        if auto_height:
            h = max(20, text_h + 2 * pad_y)
        x = min(max(0, x), max(0, base.width - w))
        y = min(max(0, y), max(0, base.height - h))
        draw.rectangle((x, y, x + w, y + h), fill=(*bg, alpha))
        cursor_y = y + pad_y
        bold = bool(options.get("bold", False))
        italic = bool(options.get("italic", False))
        for n, line in enumerate(lines):
            box = draw.textbbox((0, 0), line or "Ag", font=font)
            line_height = max(1, box[3] - box[1])
            cursor_x = x + pad_x + (int(line_height * .15) * n if italic else 0)
            draw.text((cursor_x, cursor_y - box[1]), line, font=font, fill=fg)
            if bold:
                draw.text((cursor_x + 1, cursor_y - box[1]), line, font=font, fill=fg)
                draw.text((cursor_x, cursor_y - box[1] + 1), line, font=font, fill=fg)
            cursor_y += line_height
        result = Image.alpha_composite(base, overlay)
        rendered_box = {"x": round(x / scale), "y": round(y / scale), "w": round(w / scale), "h": round(h / scale)}
        return result, rendered_box, text, original_size

    def _api_preview(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        try:
            full = self._source_file(data.get("file"))
            image, box, text, size = self._render(full, data.get("options") or {}, preview=True)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            handler.send_response(200)
            handler.send_header("Content-Type", "image/png")
            handler.send_header("X-Overlay-Box", quote(f'{box["x"]},{box["y"]},{box["w"]},{box["h"]}'))
            handler.send_header("X-Overlay-Text", quote(text[:300]))
            handler.send_header("X-Source-Size", f"{size[0]},{size[1]}")
            payload = buffer.getvalue()
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        except FileNotFoundError as exc:
            handler.respond_json({"error": str(exc)}, status=404)
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=500)

    def _api_auto_regex(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        root = self._input_folder()
        if not root:
            handler.respond_json({"error": "Choose an input folder first"}, status=503)
            return
        prompts = []
        for filename in self._list_files(root):
            prompt = self._positive_prompt(str(Path(root) / filename))
            if prompt:
                prompts.append(prompt)
        if len(prompts) < 2:
            handler.respond_json({"error": "Need at least two images with readable prompt metadata"}, status=400)
            return
        handler.respond_json({"regex": _guess_regex(prompts), "count": len(prompts)})

    def _save_image(self, result, source_path, output_path, fmt, quality):
        kwargs = {}
        with Image.open(source_path) as source:
            info = dict(source.info)
            exif = info.get("exif")
        if fmt == "PNG":
            pnginfo = PngImagePlugin.PngInfo()
            for key, value in info.items():
                if isinstance(value, str):
                    try:
                        pnginfo.add_text(str(key), value)
                    except Exception:
                        pass
            kwargs["pnginfo"] = pnginfo
            result.save(output_path, format="PNG", **kwargs)
        elif fmt == "JPEG":
            rgb = Image.new("RGB", result.size, "white")
            rgb.paste(result, mask=result.getchannel("A"))
            kwargs["quality"] = quality
            if exif:
                kwargs["exif"] = exif
            rgb.save(output_path, format="JPEG", **kwargs)
        elif fmt == "WEBP":
            kwargs["quality"] = quality
            if exif:
                kwargs["exif"] = exif
            result.save(output_path, format="WEBP", **kwargs)

    def _api_process(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        root = self._input_folder()
        if not root:
            handler.respond_json({"error": "Choose an input folder first"}, status=503)
            return
        options = data.get("options") or {}
        fmt_choice = str(options.get("output_format") or "source")
        if fmt_choice not in OUTPUT_FORMATS:
            handler.respond_json({"error": "Invalid output format"}, status=400)
            return
        suffix = str(options.get("filename_suffix") or "_overlay")
        if any(c in suffix for c in "/\\"):
            handler.respond_json({"error": "Invalid filename suffix"}, status=400)
            return
        quality = max(10, min(100, int(options.get("quality", 92))))
        output = self._output_folder(root)
        try:
            output.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            handler.respond_json({"error": "Permission denied creating output folder"}, status=403)
            return
        saved, failures = [], []
        for filename in self._list_files(root):
            source_path = str(Path(root) / filename)
            try:
                image, _, _, _ = self._render(source_path, options, preview=False)
                source_ext = Path(filename).suffix.lower()
                if fmt_choice == "source":
                    fmt = "JPEG" if source_ext in {".jpg", ".jpeg"} else source_ext[1:].upper()
                    out_ext = ".jpg" if fmt == "JPEG" else source_ext
                else:
                    fmt = fmt_choice
                    out_ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt]
                target = _next_output_path(output, Path(filename).stem, suffix, out_ext)
                self._save_image(image, source_path, target, fmt, quality)
                saved.append(target.name)
            except Exception as exc:
                failures.append({"file": filename, "error": str(exc)})
        handler.respond_json({"ok": True, "saved": len(saved), "files": saved, "failed": failures, "output_folder": str(output)})


PAGE_BODY = r'''
<style>
.ov-app{height:calc(100vh - 53px);display:grid;grid-template-columns:370px 1fr;background:var(--bg)}
.ov-side{border-right:1px solid var(--border);background:var(--bg-panel);display:flex;flex-direction:column;min-height:0}.ov-title{padding:17px 18px 13px;border-bottom:1px solid var(--border)}.ov-title h1{font-size:17px;color:var(--text-bright);margin:0 0 4px}.ov-title p{font-size:11px;color:var(--text-dim);margin:0}.ov-scroll{padding:15px;overflow:auto;flex:1}.ov-section{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--text-dim);display:flex;align-items:center;gap:8px;margin:10px 0 8px}.ov-section:after{content:'';height:1px;background:var(--border);flex:1}
.ov-row{display:flex;align-items:center;gap:7px;margin-bottom:8px}.ov-col{display:grid;grid-template-columns:1fr 1fr;gap:8px}.ov-input,.ov-select,.ov-textarea,.ov-num{font:12px var(--font);background:var(--bg-card);border:1px solid var(--border);border-radius:7px;color:var(--text);padding:7px 9px;outline:none}.ov-input:focus,.ov-select:focus,.ov-textarea:focus,.ov-num:focus{border-color:var(--accent)}.ov-input{flex:1;min-width:0}.ov-num{width:70px}.ov-textarea{width:100%;height:62px;resize:vertical;line-height:1.5}.ov-btn{border:1px solid var(--border);background:var(--bg-card);color:var(--text);font:12px var(--font);font-weight:400;border-radius:7px;padding:7px 11px;cursor:pointer;white-space:nowrap}.ov-btn:hover{border-color:var(--accent);color:var(--text-bright)}.ov-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:400}.ov-btn.big{padding:10px 16px;width:100%}.ov-muted{font-size:11px;color:var(--text-dim)}.ov-label{font-size:11px;color:var(--text-dim);min-width:64px}.ov-path{font:10px var(--mono);color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:-3px 0 8px}.ov-toggle{display:flex;margin-bottom:9px}.ov-toggle button{flex:1;border:1px solid var(--border);background:var(--bg-card);color:var(--text-dim);padding:8px;font:12px var(--font);cursor:pointer}.ov-toggle button:first-child{border-radius:7px 0 0 7px}.ov-toggle button:last-child{border-radius:0 7px 7px 0}.ov-toggle button.active{background:rgba(0,214,143,.16);border-color:var(--accent);color:var(--accent)}input[type=color]{width:39px;height:32px;background:none;border:0;padding:0;cursor:pointer}input[type=range]{accent-color:var(--accent);flex:1}
.ov-main{display:grid;grid-template-rows:54px 1fr 48px;min-width:0;min-height:0}.ov-top{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--bg-panel)}.ov-files{min-width:230px;max-width:420px}.ov-counter{font:11px var(--mono);color:var(--text-dim);margin-left:auto}.ov-stage{min-height:0;display:flex;align-items:center;justify-content:center;padding:16px;position:relative;overflow:hidden;background:var(--bg-card)}.ov-preview-wrap{position:relative;display:none;max-width:100%;max-height:100%}.ov-preview{display:block;max-width:100%;max-height:calc(100vh - 180px);user-select:none;-webkit-user-drag:none}.ov-box{position:absolute;border:2px dashed var(--accent);cursor:move;display:none}.ov-handle{position:absolute;right:-6px;bottom:-6px;width:14px;height:14px;border-radius:3px;background:var(--accent);border:2px solid var(--bg-panel);cursor:nwse-resize}.ov-empty{text-align:center;color:var(--text-dim)}.ov-empty strong{font-size:22px;color:var(--text-bright);display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:8px}.ov-empty svg{width:24px;height:24px;color:var(--text-dim)}.ov-bottom{display:flex;align-items:center;gap:10px;padding:8px 16px;border-top:1px solid var(--border);color:var(--text-dim);font-size:12px}.ov-extracted{font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.ov-status{margin-left:auto;color:var(--accent)}
.browse-overlay{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:9000;display:none;align-items:center;justify-content:center}.browse-overlay.open{display:flex}.browse-dialog{background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;width:600px;max-height:74vh;display:flex;flex-direction:column}.browse-header,.browse-footer{padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px}.browse-header{border-bottom:1px solid var(--border)}.browse-footer{border-top:1px solid var(--border)}.browse-header h3{font-size:14px;margin:0}.browse-close{font-size:20px;color:var(--text-dim);background:none;border:0;cursor:pointer}.browse-crumb{padding:8px 16px;border-bottom:1px solid var(--border);font:11px var(--mono);color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.browse-body{min-height:240px;max-height:420px;overflow:auto;padding:5px 0}.browse-entry{display:flex;gap:10px;align-items:center;padding:8px 16px;cursor:pointer;font-size:12px}.browse-entry:hover{background:var(--bg-hover);color:var(--accent)}.browse-current{font:10px var(--mono);color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 18px;border-radius:8px;background:var(--accent);color:#fff;font-size:12px;font-weight:400;display:none;z-index:9999}.toast.err{background:#ff6b6b;color:#fff}
@media(max-width:1000px){.ov-app{grid-template-columns:320px 1fr}}
</style>
<div class="ov-app">
 <aside class="ov-side"><div class="ov-title"><h1>Image Overlay</h1><p>Add labels, prompt fragments or watermarks in batch.</p></div><div class="ov-scroll">
  <div class="ov-section">Folders</div>
  <div class="ov-row"><input id="inputFolder" class="ov-input" placeholder="Input folder..."><button class="ov-btn" id="browseInput">Browse</button></div><div class="ov-path" id="inPath">No input folder loaded</div>
  <div class="ov-row"><input id="outputFolder" class="ov-input" placeholder="Output folder (blank = /overlay-output)..."><button class="ov-btn" id="browseOutput">Browse</button></div><div class="ov-path" id="outPath">Output defaults to input/overlay-output</div>
  <button class="ov-btn primary big" id="loadFolder">Load images</button>
  <div class="ov-section">Text Content</div>
  <div class="ov-toggle"><button id="modeExtract" class="active">Extract metadata</button><button id="modeStatic">Static text</button></div>
  <div id="extractControls"><input id="regex" class="ov-input" style="width:100%;margin-bottom:7px" value="\b(\w+)\s+woman\b" placeholder="Regular expression..."><div class="ov-row"><label class="ov-label">Group</label><input id="group" class="ov-num" type="number" min="0" value="1"><button id="autoRegex" class="ov-btn">Auto-Detect</button></div><div class="ov-col"><input id="prefix" class="ov-input" placeholder="Prefix"><input id="suffix" class="ov-input" placeholder="Suffix"></div></div>
  <div id="staticControls" style="display:none"><textarea id="staticText" class="ov-textarea" placeholder="Watermark or label text..."></textarea></div>
  <div class="ov-section">Style</div>
  <div class="ov-row"><label class="ov-label">Font</label><select id="font" class="ov-select" style="flex:1"><option value="">Default</option></select><input id="fontSize" class="ov-num" type="number" min="8" max="300" value="40"></div>
  <div class="ov-row"><label class="ov-label">Text</label><input id="textColor" type="color" value="#ffffff"><label class="ov-label" style="min-width:30px">BG</label><input id="bgColor" type="color" value="#1a1a2e"></div>
  <div class="ov-row"><label class="ov-label">Opacity</label><input id="opacity" type="range" min="0" max="255" value="200"><span id="opacityValue" class="ov-muted">200</span></div>
  <div class="ov-row"><label class="ov-label">Padding</label><input id="padX" class="ov-num" type="number" value="16" min="0"><input id="padY" class="ov-num" type="number" value="10" min="0"><span class="ov-muted">X / Y</span></div>
  <div class="ov-row"><label><input id="bold" type="checkbox"> Bold</label><label><input id="italic" type="checkbox"> Italic</label><label><input id="autoWidth" type="checkbox" checked> Auto W</label><label><input id="autoHeight" type="checkbox" checked> Auto H</label></div>
  <div class="ov-section">Export</div>
  <div class="ov-row"><select id="format" class="ov-select" style="flex:1"><option value="source">Keep source format</option><option>PNG</option><option>JPEG</option><option>WEBP</option></select><input id="quality" class="ov-num" type="number" min="10" max="100" value="92" title="JPEG/WEBP quality"></div>
  <div class="ov-row"><label class="ov-label">Suffix</label><input id="fileSuffix" class="ov-input" value="_overlay"></div>
  <button class="ov-btn primary big" id="process">▶ Process all images</button>
 </div></aside>
 <main class="ov-main"><div class="ov-top"><button class="ov-btn" id="prev">◀ Prev</button><select id="files" class="ov-select ov-files"></select><button class="ov-btn" id="next">Next ▶</button><button class="ov-btn" id="resetBox">Reset box</button><span class="ov-counter" id="counter"></span></div>
  <div class="ov-stage"><div id="empty" class="ov-empty"><strong><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16"/><path d="M14 4l6 6-9 9H5v-6l9-9z"/></svg>Overlay</strong>Choose an input folder to begin.</div><div id="wrap" class="ov-preview-wrap"><img id="preview" class="ov-preview"><div id="box" class="ov-box"><span class="ov-handle" id="handle"></span></div></div></div>
  <div class="ov-bottom"><span>Extracted text:</span><span class="ov-extracted" id="extracted">—</span><span class="ov-status" id="status"></span></div></main>
</div>
<div id="browseOverlay" class="browse-overlay"><div class="browse-dialog"><div class="browse-header"><h3>Select folder</h3><button class="browse-close" id="browseClose">×</button></div><div class="browse-crumb" id="browseCrumb"></div><div class="browse-body" id="browseBody"></div><div class="browse-footer"><span class="browse-current" id="browsePath"></span><button class="ov-btn primary" id="browseSelect">Select folder</button></div></div></div><div class="toast" id="toast"></div>
<script>
(function(){'use strict';
var $=function(id){return document.getElementById(id);}, state={files:[],index:-1,mode:'extract',box:{x:50,y:50,w:420,h:110},sourceSize:{w:1,h:1},drag:null,prefs:{}};var browseTarget=null,browseCurrent='',previewTimer=null;
function esc(s){return String(s||'').replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function attr(s){return esc(s);}
function toast(msg,err){var t=$('toast');t.textContent=msg;t.className='toast'+(err?' err':'');t.style.display='block';clearTimeout(t._timer);t._timer=setTimeout(function(){t.style.display='none';},2600);}
function apiError(r){return r.json().catch(function(){return {};}).then(function(d){throw new Error(d.error||('HTTP '+r.status));});}
function options(){return {mode:state.mode,regex:$('regex').value,group:Number($('group').value||1),prefix:$('prefix').value,suffix:$('suffix').value,static_text:$('staticText').value,font_path:$('font').value,font_size:Number($('fontSize').value||40),text_color:$('textColor').value,bg_color:$('bgColor').value,bg_alpha:Number($('opacity').value||200),pad_x:Number($('padX').value||16),pad_y:Number($('padY').value||10),bold:$('bold').checked,italic:$('italic').checked,auto_width:$('autoWidth').checked,auto_height:$('autoHeight').checked,box:state.box,output_format:$('format').value,quality:Number($('quality').value||92),filename_suffix:$('fileSuffix').value};}
function savePrefs(){try{var p=options();delete p.box;localStorage.setItem('cdhub_overlay_prefs',JSON.stringify(p));}catch(e){}}
function loadPrefs(){try{var p=JSON.parse(localStorage.getItem('cdhub_overlay_prefs')||'{}');Object.keys(p).forEach(function(k){var map={static_text:'staticText',font_path:'font',font_size:'fontSize',text_color:'textColor',bg_color:'bgColor',bg_alpha:'opacity',pad_x:'padX',pad_y:'padY',auto_width:'autoWidth',auto_height:'autoHeight',output_format:'format',filename_suffix:'fileSuffix'};var id=map[k]||k, el=$(id);if(!el)return;if(el.type==='checkbox')el.checked=!!p[k];else el.value=p[k];});if(p.mode)setMode(p.mode);$('opacityValue').textContent=$('opacity').value;}catch(e){}}
function setMode(mode){state.mode=mode;$('modeExtract').classList.toggle('active',mode==='extract');$('modeStatic').classList.toggle('active',mode==='static');$('extractControls').style.display=mode==='extract'?'block':'none';$('staticControls').style.display=mode==='static'?'block':'none';queuePreview();savePrefs();}
function fillFonts(fonts){var current=$('font').value, html='<option value="">Default</option>';(fonts||[]).forEach(function(f){html+='<option value="'+attr(f.value)+'">'+esc(f.label)+'</option>';});$('font').innerHTML=html;if([].some.call($('font').options,function(o){return o.value===current;}))$('font').value=current;}
function loadState(){fetch('/api/overlay/state').then(function(r){return r.ok?r.json():apiError(r);}).then(applyState).catch(function(e){toast(e.message,true);});}
function applyState(s){$('inputFolder').value=s.input_folder||'';$('outputFolder').value=s.output_folder||'';$('inPath').textContent=s.input_folder||'No input folder loaded';$('outPath').textContent=s.effective_output_folder||'Output defaults to input/overlay-output';fillFonts(s.fonts);state.files=s.files||[];var select=$('files');select.innerHTML=state.files.map(function(f){return '<option>'+esc(f)+'</option>';}).join('');if(!state.files.length){state.index=-1;$('wrap').style.display='none';$('empty').style.display='block';$('counter').textContent='';return;}state.index=Math.max(0,Math.min(state.index<0?0:state.index,state.files.length-1));select.selectedIndex=state.index;$('empty').style.display='none';$('wrap').style.display='block';renderPreview();updateCounter();}
function loadFolders(){fetch('/api/overlay/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_folder:$('inputFolder').value.trim(),output_folder:$('outputFolder').value.trim()})}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(s){state.index=-1;applyState(s);toast('Images loaded');}).catch(function(e){toast(e.message,true);});}
function updateCounter(){$('counter').textContent=state.files.length?(state.index+1)+' / '+state.files.length:'';}
function current(){return state.files[state.index]||'';}
function queuePreview(){savePrefs();if(!current())return;clearTimeout(previewTimer);previewTimer=setTimeout(renderPreview,90);}
function renderPreview(){if(!current())return;fetch('/api/overlay/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:current(),options:options()})}).then(function(r){if(!r.ok)return apiError(r);var box=(decodeURIComponent(r.headers.get('X-Overlay-Box')||'50,50,420,110')).split(',').map(Number), sz=(r.headers.get('X-Source-Size')||'1,1').split(',').map(Number), text=decodeURIComponent(r.headers.get('X-Overlay-Text')||'');state.box={x:box[0],y:box[1],w:box[2],h:box[3]};state.sourceSize={w:sz[0],h:sz[1]};$('extracted').textContent=text||'—';return r.blob();}).then(function(blob){if(!blob)return;var url=URL.createObjectURL(blob), img=$('preview');img.onload=function(){if(img._url)URL.revokeObjectURL(img._url);img._url=url;placeBox();};img.src=url;}).catch(function(e){toast(e.message,true);});}
function placeBox(){var img=$('preview'), box=$('box'), sx=img.clientWidth/state.sourceSize.w, sy=img.clientHeight/state.sourceSize.h;box.style.display='block';box.style.left=(state.box.x*sx)+'px';box.style.top=(state.box.y*sy)+'px';box.style.width=(state.box.w*sx)+'px';box.style.height=(state.box.h*sy)+'px';}
function browseOpen(target){browseTarget=target;$('browseOverlay').classList.add('open');browseLoad($(target).value.trim());}
function browseLoad(path){fetch('/api/browse?path='+encodeURIComponent(path||'')).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){browseCurrent=d.path||'';$('browsePath').textContent=d.display||browseCurrent||'Drives';var crumbs=['<span data-path="">Root</span>'];(d.crumbs||[]).forEach(function(c){crumbs.push('<span> › </span><span data-path="'+attr(c.path)+'">'+esc(c.label)+'</span>');});$('browseCrumb').innerHTML=crumbs.join('');var html='';if(d.parent!==null&&d.parent!==undefined)html+='<div class="browse-entry" data-path="'+attr(d.parent)+'">↩ <span>..</span></div>';(d.dirs||[]).forEach(function(dir){html+='<div class="browse-entry" data-path="'+attr(dir.path)+'"><span>'+esc(dir.name)+'</span></div>';});$('browseBody').innerHTML=html||'<div class="browse-entry">No subfolders</div>';}).catch(function(e){toast(e.message,true);});}
$('browseInput').onclick=function(){browseOpen('inputFolder');};$('browseOutput').onclick=function(){browseOpen('outputFolder');};$('browseClose').onclick=function(){$('browseOverlay').classList.remove('open');};$('browseSelect').onclick=function(){if(browseCurrent&&browseTarget)$(browseTarget).value=browseCurrent;$('browseOverlay').classList.remove('open');};$('browseOverlay').onclick=function(e){if(e.target===this){this.classList.remove('open');return;}var el=e.target.closest('[data-path]');if(el)browseLoad(el.getAttribute('data-path'));};
$('loadFolder').onclick=loadFolders;$('modeExtract').onclick=function(){setMode('extract');};$('modeStatic').onclick=function(){setMode('static');};$('files').onchange=function(){state.index=this.selectedIndex;state.box={x:50,y:50,w:420,h:110};updateCounter();renderPreview();};$('prev').onclick=function(){if(state.index>0){state.index--;$('files').selectedIndex=state.index;updateCounter();renderPreview();}};$('next').onclick=function(){if(state.index<state.files.length-1){state.index++;$('files').selectedIndex=state.index;updateCounter();renderPreview();}};$('resetBox').onclick=function(){state.box={x:50,y:50,w:420,h:110};renderPreview();};
['regex','group','prefix','suffix','staticText','font','fontSize','textColor','bgColor','opacity','padX','padY','bold','italic','autoWidth','autoHeight','format','quality','fileSuffix'].forEach(function(id){$(id).addEventListener('input',function(){if(id==='opacity')$('opacityValue').textContent=this.value;queuePreview();});$(id).addEventListener('change',queuePreview);});
$('autoRegex').onclick=function(){fetch('/api/overlay/auto_regex',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){$('regex').value=d.regex;queuePreview();toast('Pattern detected from '+d.count+' images');}).catch(function(e){toast(e.message,true);});};
$('process').onclick=function(){var btn=this;btn.disabled=true;btn.textContent='Processing…';$('status').textContent='Processing images…';fetch('/api/overlay/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({options:options()})}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){$('status').textContent='Saved '+d.saved+' images';toast('Saved '+d.saved+' images to '+d.output_folder+(d.failed.length?' · '+d.failed.length+' failed':''),!!d.failed.length);}).catch(function(e){toast(e.message,true);$('status').textContent='Failed';}).finally(function(){btn.disabled=false;btn.textContent='▶ Process all images';});};
var box=$('box'), handle=$('handle');function point(e){var r=$('preview').getBoundingClientRect();return {x:(e.clientX-r.left)*state.sourceSize.w/r.width,y:(e.clientY-r.top)*state.sourceSize.h/r.height};}box.onmousedown=function(e){e.preventDefault();var p=point(e);state.drag={kind:e.target===handle?'resize':'move',p:p,start:Object.assign({},state.box)};};window.addEventListener('mousemove',function(e){if(!state.drag)return;var p=point(e), dx=p.x-state.drag.p.x,dy=p.y-state.drag.p.y;if(state.drag.kind==='move'){state.box.x=Math.max(0,Math.min(state.sourceSize.w-state.box.w,state.drag.start.x+dx));state.box.y=Math.max(0,Math.min(state.sourceSize.h-state.box.h,state.drag.start.y+dy));}else{state.box.w=Math.max(30,Math.min(state.sourceSize.w-state.box.x,state.drag.start.w+dx));state.box.h=Math.max(30,Math.min(state.sourceSize.h-state.box.y,state.drag.start.h+dy));}placeBox();});window.addEventListener('mouseup',function(){if(state.drag){state.drag=null;renderPreview();}});window.addEventListener('resize',placeBox);
loadPrefs();loadState();
})();
</script>
'''
