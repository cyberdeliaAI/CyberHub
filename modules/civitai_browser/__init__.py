"""Civitai Browser - hub-native model search, preview and download."""

import json
import os
import re
import threading
import time
from html import escape
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import requests

from core import Module
from core.server import build_shell


API_TIMEOUT = 30
MIN_SAFETENSORS_SIZE = 8192      # bytes — smaller = truncated/garbage download
MAX_NAME_LEN = 120
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ─── Download engine (adapted from RupertAvery-style robust grabbers) ───────────

def _sanitize_component(name, fallback="unknown"):
    """Make a string safe as ONE path component (folder name or file stem).
    Strips separators/illegal chars, blocks '..' and Windows reserved names."""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.replace("..", "").strip(" .")
    if not name:
        return fallback
    if name.upper().split(".")[0] in _WINDOWS_RESERVED:
        name = "_" + name
    return name[:MAX_NAME_LEN]


def _safe_filename(name, fallback="file.bin"):
    """Sanitize a filename coming from the API, preserving its extension."""
    name = os.path.basename((name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(name)
    stem = _sanitize_component(stem, "file")
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:12]
    return (stem + ext)[:MAX_NAME_LEN] or fallback


def _safe_join(base, *parts):
    """Join parts under base; raise if the result escapes base (traversal guard)."""
    base_abs = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base, *parts))
    if full != base_abs and not full.startswith(base_abs + os.sep):
        raise ValueError(f"path traversal blocked: {full}")
    return full


def _stream_download(url, dest, token, progress=None, max_retries=3, retry_delay=5):
    """Stream `url` to `dest` atomically. Returns 'downloaded' | 'skipped' | 'failed'.

    - Token goes in the Authorization header — never in the URL or any log.
    - Writes to dest+'.tmp', then os.replace() — no corrupt half-files.
    - Skips if dest already exists.
    - Retries on timeout/connection errors; does NOT retry HTTP 4xx (auth).
    - Guards against truncated .safetensors (re-downloads if suspiciously small).
    """
    if os.path.exists(dest):
        return "skipped"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    headers = {"User-Agent": "CyberHub Civitai Browser"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(max_retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(20, 60)) as r:
                if r.status_code == 404:
                    return "failed"
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if progress is not None:
                                progress(done, total)
            if dest.endswith(".safetensors") and os.path.getsize(tmp) < MIN_SAFETENSORS_SIZE:
                raise IOError("truncated .safetensors")
            os.replace(tmp, dest)
            return "downloaded"
        except requests.HTTPError:
            _rm(tmp)
            return "failed"   # auth / 4xx — retrying won't help
        except (requests.Timeout, requests.ConnectionError, IOError, OSError):
            _rm(tmp)
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return "failed"
    return "failed"


def _rm(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# Civitai model `type` -> SD WebUI Forge / ComfyUI models subfolder. The file is
# dropped flat into this folder so the app's model scanner picks it up directly.
_FORGE_FOLDERS = {
    "Checkpoint": "Stable-diffusion",
    "LORA": "Lora",
    "LoCon": "Lora",
    "DoRA": "Lora",
    "TextualInversion": "embeddings",
    "VAE": "VAE",
    "Controlnet": "ControlNet",
    "Upscaler": "ESRGAN",
    "Hypernetwork": "hypernetworks",
    "MotionModule": "Lora",
}


def _forge_subfolder(model_type):
    return _FORGE_FOLDERS.get(model_type or "", "Other")
DEFAULT_BASE_MODELS = [
    "Anima", "Illustrious", "NoobAI", "Pony", "Pony V7", "SDXL 1.0", "SD 1.5",
    "Flux.1 D", "Flux.1 S", "Flux.1 Krea", "Flux.2 D",
    "Wan Video", "Wan Video 2.2 I2V-A14B", "Wan Video 2.2 T2V-A14B",
    "Qwen", "Hunyuan Video", "LTXV", "ZImageTurbo", "Other",
]
MODEL_TYPES = [
    "Checkpoint", "LORA", "TextualInversion", "VAE", "Controlnet",
    "Upscaler", "MotionModule", "Wildcards", "Workflows",
]


class CivitaiBrowserModule(Module):
    name = "Civitai Browser"
    version = "1.1.1-beta"
    release_stage = "beta"
    icon = "C"
    description = "Beta. Browse Civitai models, preview them, and download into your models folder."
    order = 41
    settings_schema = {
        "archive_folder": {
            "type": "folder", "label": "Archive folder", "default": "",
            "desc": "Archive layout: Type/BaseModel/Model/Version (e.g. Lora/SDXL 1.0/Real Dream/v1.0), "
                    "with triggerWords/description/details sidecars. Good for keeping a sorted collection.",
        },
        "forge_folder": {
            "type": "folder", "label": "Forge / ComfyUI models folder", "default": "",
            "desc": "App layout: drops the file straight into the matching models subfolder "
                    "(Checkpoint→Stable-diffusion, LORA→Lora, TextualInversion→embeddings, VAE→VAE, "
                    "Upscaler→ESRGAN) so your SD UI picks it up immediately. Point this at your "
                    "Forge/ComfyUI models/ directory.",
        },
        "download_previews": {
            "type": "bool", "label": "Download preview images", "default": True,
            "desc": "Archive mode: save a few previews + their _meta.txt (Gallery/Viewer can read them). "
                    "Forge mode: save a single <model>.preview.jpeg thumbnail next to the file.",
        },
    }
    # Note: "Blur NSFW previews" is a live toggle in the browser sidebar (remembered
    # via localStorage), so it's intentionally NOT a server setting — one control,
    # no duplication.

    def __init__(self, hub):
        super().__init__(hub)
        self._downloads = {}            # id -> progress dict
        self._dl_lock = threading.Lock()
        self._dl_counter = 0

    def key(self):
        return "civitai_browser"

    def routes_get(self):
        return {
            "/civitai_browser": self._page,
            "/api/civitai-browser/search": self._api_search,
            "/api/civitai-browser/model": self._api_model,
            "/api/civitai-browser/base-models": self._api_base_models,
            "/api/civitai-browser/download-status": self._api_download_status,
        }

    def routes_post(self):
        return {
            "/api/civitai-browser/download": self._api_download,
        }

    def _page(self, handler, qs):
        archive_ready = bool((self.setting("archive_folder", "") or "").strip())
        forge_ready = bool((self.setting("forge_folder", "") or "").strip())
        body = (PAGE_BODY
                .replace("__ARCHIVE_READY__", "true" if archive_ready else "false")
                .replace("__FORGE_READY__", "true" if forge_ready else "false"))
        handler.respond_html(build_shell(
            self.hub.registry,
            self.hub.settings,
            active_key=self.key(),
            page_title="Civitai Browser",
            body_html=body,
        ))

    def _domain(self, qs):
        value = (qs.get("domain", ["full"])[0] or "full").strip().lower()
        return "civitai.com" if value == "sfw" else "civitai.red"

    def _api_key(self):
        return (self.hub.settings.get_path("civitai.api_key", "") or "").strip()

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "CyberHub Civitai Browser"}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request_json(self, url, params=None):
        resp = requests.get(url, params=params, headers=self._headers(), timeout=API_TIMEOUT)
        if resp.status_code >= 400:
            detail = ""
            try:
                payload = resp.json()
                detail = payload.get("message") or payload.get("error") or ""
            except Exception:
                detail = resp.text[:300]
            # Civitai's full-text search (Meilisearch) is frequently overloaded and
            # returns 503. That's server-side — point the user at the reliable path.
            if resp.status_code == 503:
                raise RuntimeError(
                    "Civitai's text search is temporarily overloaded (their server). "
                    "Try again shortly, browse with the filters, or paste a model link / ID."
                )
            raise RuntimeError(f"Civitai returned {resp.status_code}" + (f": {detail}" if detail else ""))
        return resp.json()

    def _resolve_direct(self, domain, term):
        """If `term` is a Civitai model link, a download link, or a bare model ID,
        fetch that one model directly. The /models?query= search misses a lot of
        models (NSFW-flagged ones, and anything its weak text index doesn't rank),
        so a pasted link/ID is the reliable way to land on an exact model."""
        term = (term or "").strip()
        if not term:
            return None
        model_id = None
        dl = re.search(r"/api/download/models/(\d+)", term)
        if dl:
            vdata = self._request_json(f"https://{domain}/api/v1/model-versions/{dl.group(1)}")
            model_id = vdata.get("modelId")
            if not model_id:
                raise RuntimeError(f"Civitai download/version ID {dl.group(1)} did not resolve to a model.")
        if model_id is None:
            mp = re.search(r"/models/(\d+)", term)
            if mp:
                model_id = mp.group(1)
        if model_id is None and re.fullmatch(r"\d+", term):
            model_id = term
        if model_id is None:
            return None
        return self._request_json(f"https://{domain}/api/v1/models/{model_id}")

    def _api_search(self, handler, qs):
        domain = self._domain(qs)
        url = f"https://{domain}/api/v1/models"
        limit = _int_qs(qs, "limit", 24, 1, 100)
        params = {"limit": limit}

        cursor = _first(qs, "cursor")
        if cursor:
            params["cursor"] = cursor

        query = _first(qs, "q")
        search_type = _first(qs, "search_type", "model")
        exact = _first(qs, "exact") == "1"

        # A pasted link or bare ID beats the weak text search — resolve it directly.
        if query and search_type == "model" and not cursor:
            try:
                direct = self._resolve_direct(domain, query)
            except Exception as e:
                handler.respond_json({"error": str(e)}, status=502)
                return
            if direct is not None:
                direct["_hub"] = {"domain": domain}
                self._add_download_links(direct)
                handler.respond_json({"items": [direct], "metadata": {}, "_hub": {"domain": domain}})
                return

        if query:
            if exact and search_type == "model":
                query = f'"{query}"'
            if search_type == "tag":
                params["tag"] = query
            elif search_type == "user":
                params["username"] = query
            else:
                params["query"] = query

        for q_key, api_key in (
            ("types", "types"),
            ("sort", "sort"),
            ("period", "period"),
            ("baseModels", "baseModels"),
        ):
            value = _first(qs, q_key)
            if value:
                params[api_key] = value

        nsfw = _first(qs, "nsfw")
        if nsfw in ("true", "false"):
            params["nsfw"] = nsfw

        try:
            data = self._request_json(url, params=params)
            data["_hub"] = {"domain": domain}
            handler.respond_json(data)
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=502)

    def _api_model(self, handler, qs):
        model_id = _first(qs, "id")
        if not model_id or not re.match(r"^\d+$", model_id):
            handler.respond_json({"error": "Missing or invalid model id"}, status=400)
            return

        domain = self._domain(qs)
        try:
            data = self._request_json(f"https://{domain}/api/v1/models/{model_id}")
            data["_hub"] = {"domain": domain}
            self._add_download_links(data)
            handler.respond_json(data)
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=502)

    def _api_base_models(self, handler, qs):
        domain = self._domain(qs)
        url = f"https://{domain}/api/v1/models"
        # Civitai exposes the allowed enum values in the validation error for this
        # sentinel request. Fall back to a curated static list when that changes.
        try:
            resp = requests.get(
                url,
                params={"baseModels": "GetModels", "limit": 1},
                headers=self._headers(),
                timeout=API_TIMEOUT,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
            options = _merge_base_model_options(_extract_base_model_options(data))
        except Exception:
            options = _merge_base_model_options([])
        handler.respond_json({"baseModels": options})

    def _add_download_links(self, model):
        token = self._api_key()
        domain = model.get("_hub", {}).get("domain") or "civitai.red"
        for version in model.get("modelVersions", []) or []:
            for file_info in version.get("files", []) or []:
                url = file_info.get("downloadUrl") or ""
                if not url:
                    continue
                file_info["_hubDownloadUrl"] = _download_url_with_token(url, token)
            version["_hubModelPageUrl"] = f"https://{domain}/models/{model.get('id')}?modelVersionId={version.get('id')}"

    # ─── Server-side download (Archive layout or Forge/ComfyUI layout) ───────
    def _api_download(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len) or {}
        model_id = str(data.get("model_id", "")).strip()
        version_id = str(data.get("version_id", "")).strip()
        file_id = str(data.get("file_id", "")).strip()
        domain = "civitai.com" if data.get("domain") == "sfw" else "civitai.red"
        mode = "forge" if data.get("mode") == "forge" else "archive"
        if not re.match(r"^\d+$", model_id):
            handler.respond_json({"error": "Invalid model id"}, status=400)
            return
        folder_key = "forge_folder" if mode == "forge" else "archive_folder"
        root = (self.setting(folder_key, "") or "").strip()
        if not root or not os.path.isdir(root):
            label = "Forge / ComfyUI" if mode == "forge" else "Archive"
            handler.respond_json({"error": f"Set the {label} folder in Settings → Civitai Browser first."}, status=400)
            return
        with self._dl_lock:
            self._dl_counter += 1
            dl_id = str(self._dl_counter)
            self._downloads[dl_id] = {"state": "starting", "pct": 0}
        t = threading.Thread(target=self._do_download,
                             args=(dl_id, domain, model_id, version_id, file_id, mode), daemon=True)
        t.start()
        handler.respond_json({"ok": True, "id": dl_id})

    def _api_download_status(self, handler, qs):
        dl_id = _first(qs, "id")
        with self._dl_lock:
            prog = dict(self._downloads.get(dl_id) or {"state": "unknown"})
        handler.respond_json(prog)

    def _setp(self, dl_id, **kw):
        with self._dl_lock:
            if dl_id in self._downloads:
                self._downloads[dl_id].update(kw)

    def _do_download(self, dl_id, domain, model_id, version_id, file_id, mode="archive"):
        try:
            model = self._request_json(f"https://{domain}/api/v1/models/{model_id}")
            model["_hub"] = {"domain": domain}
            versions = model.get("modelVersions", []) or []
            version = next((v for v in versions if str(v.get("id")) == version_id), None) \
                or (versions[0] if versions else None)
            if not version:
                self._setp(dl_id, state="failed", error="No model version found")
                return
            files = version.get("files", []) or []
            target = next((f for f in files if str(f.get("id")) == file_id), None) \
                or (files[0] if files else None)
            if not target or not target.get("downloadUrl"):
                self._setp(dl_id, state="failed", error="No downloadable file")
                return

            mname = _sanitize_component(model.get("name") or model_id)
            if mode == "forge":
                # App layout: flat into the matching models subfolder.
                root = (self.setting("forge_folder", "") or "").strip()
                folder = _safe_join(root, _forge_subfolder(model.get("type")))
            else:
                # Archive layout: Type/BaseModel/Model/Version + sidecars.
                root = (self.setting("archive_folder", "") or "").strip()
                mtype = _sanitize_component(model.get("type") or "Other", "Other")
                base = _sanitize_component(version.get("baseModel") or "Other", "Other")
                vname = _sanitize_component(version.get("name") or version_id)
                folder = _safe_join(root, mtype, base, mname, vname)
            os.makedirs(folder, exist_ok=True)
            if mode == "archive":
                self._write_sidecars(folder, model, version)

            fname = _safe_filename(target.get("name") or (mname + ".safetensors"))
            dest = _safe_join(folder, fname)
            token = self._api_key()
            self._setp(dl_id, state="downloading", file=fname, folder=folder, pct=0)

            def cb(done, total):
                self._setp(dl_id, received=done, total=total,
                           pct=int(done * 100 / total) if total else 0)

            url = target["downloadUrl"]
            sep = "&" if "?" in url else "?"
            result = _stream_download(url + f"{sep}nsfw=true", dest, token, progress=cb)
            if result == "failed":
                self._setp(dl_id, state="failed", error="Download failed (check server log / API key)")
                return

            if self.setting("download_previews", True):
                try:
                    if mode == "forge":
                        self._forge_preview(folder, fname, version, token)
                    else:
                        self._download_previews(folder, domain, version, token)
                except Exception:
                    pass

            self._setp(dl_id, state="done", pct=100, result=result, path=dest, folder=folder)
        except Exception as e:
            self._setp(dl_id, state="failed", error=str(e))

    def _forge_preview(self, folder, model_filename, version, token):
        """Save the first non-video preview as <model>.preview.<ext> — the thumbnail
        convention SD WebUI Forge / A1111 read next to a model file."""
        img = next((i for i in (version.get("images") or [])
                    if i.get("type") != "video" and i.get("url")), None)
        if not img:
            return
        stem = os.path.splitext(model_filename)[0]
        ext = os.path.splitext(img["url"].split("?")[0])[1] or ".jpeg"
        _stream_download(img["url"], os.path.join(folder, stem + ".preview" + ext), token)

    def _write_sidecars(self, folder, model, version):
        try:
            tw = version.get("trainedWords") or []
            with open(_safe_join(folder, "triggerWords.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(tw) + ("\n" if tw else ""))
            with open(_safe_join(folder, "description.html"), "w", encoding="utf-8") as f:
                f.write(model.get("description") or "")
            creator = (model.get("creator") or {}).get("username", "")
            lines = [
                f"Model: {model.get('name','')}",
                f"Model URL: https://civitai.com/models/{model.get('id')}",
                f"Type: {model.get('type','')}",
                f"Version: {version.get('name','')}",
                f"Base model: {version.get('baseModel','')}",
                f"Creator: {creator}",
            ]
            with open(_safe_join(folder, "details.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except (OSError, ValueError):
            pass

    def _download_previews(self, folder, domain, version, token, limit=4):
        """Save a few preview images + their generation metadata as _meta.txt so the
        Gallery/Viewer metadata parser can read them (Civitai key/value format)."""
        img_dir = os.path.join(folder, "previews")
        # The images API carries the `meta` (generation params); the model endpoint
        # often doesn't, so fetch it for richer sidecars.
        metas = {}
        try:
            data = self._request_json(
                f"https://{domain}/api/v1/images",
                params={"modelVersionId": version.get("id"), "limit": limit, "nsfw": "X"},
            )
            for im in data.get("items", []) or []:
                if im.get("url"):
                    metas[im["url"]] = im.get("meta") or {}
        except Exception:
            pass
        imgs = (version.get("images") or [])[:limit]
        for i, im in enumerate(imgs):
            url = im.get("url")
            if not url:
                continue
            ext = os.path.splitext(url.split("?")[0])[1] or ".jpeg"
            base = _safe_filename(f"preview_{i+1}{ext}")
            dest = os.path.join(img_dir, base)
            if _stream_download(url, dest, token) == "failed":
                continue
            meta = metas.get(url) or im.get("meta") or {}
            if meta:
                try:
                    with open(os.path.splitext(dest)[0] + "_meta.txt", "w", encoding="utf-8") as f:
                        f.write(_format_civitai_meta(meta))
                except OSError:
                    pass


def _format_civitai_meta(meta):
    """Render a Civitai image `meta` dict as the key/value text our metadata parser
    reads. prompt/negativePrompt go LAST so their multi-line values don't swallow
    trailing keys."""
    if not isinstance(meta, dict):
        return ""

    def fmt(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)

    lines = []
    for k, v in meta.items():
        if k in ("prompt", "negativePrompt") or v in (None, ""):
            continue
        lines.append(f"{k}: {fmt(v)}")
    if meta.get("prompt"):
        lines.append(f"prompt: {fmt(meta['prompt'])}")
    if meta.get("negativePrompt"):
        lines.append(f"negativePrompt: {fmt(meta['negativePrompt'])}")
    return "\n".join(lines)


def _first(qs, key, default=""):
    values = qs.get(key)
    if not values:
        return default
    return (values[0] or default).strip()


def _int_qs(qs, key, default, min_value, max_value):
    try:
        value = int(_first(qs, key, str(default)))
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _download_url_with_token(url, token):
    if not token:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _extract_base_model_options(payload):
    if not isinstance(payload, dict):
        return []
    message = ""
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or ""
    elif isinstance(error, str):
        message = error
    if isinstance(message, str) and message:
        try:
            parsed = json.loads(message)
            values = parsed[0]["errors"][0][0]["values"]
            if isinstance(values, list):
                return [str(v) for v in values if v]
        except Exception:
            return []
    return []


def _merge_base_model_options(values):
    """Keep curated new/important bases even when Civitai's enum endpoint lags."""
    out = []
    seen = set()
    for value in list(DEFAULT_BASE_MODELS) + list(values or []):
        value = str(value or "").strip()
        key = value.lower()
        if value and key not in seen:
            out.append(value)
            seen.add(key)
    return out


def _option_tags(values):
    return "\n".join(f'<option value="{escape(v)}">{escape(v)}</option>' for v in values)


PAGE_BODY = f"""
<style>
.cb-wrap{{height:100%;display:grid;grid-template-columns:320px minmax(0,1fr);background:var(--bg-main)}}
.cb-side{{border-right:1px solid var(--border);background:var(--bg-panel);padding:16px;overflow:auto}}
.cb-side h2{{font-size:15px;color:var(--text-bright);margin:0 0 12px}}
.cb-form{{display:flex;flex-direction:column;gap:11px}}
.cb-field{{display:flex;flex-direction:column;gap:4px}}
.cb-label{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim)}}
.cb-input,.cb-select{{width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 10px;font:inherit;font-size:13px}}
.cb-row{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.cb-check{{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--text);cursor:pointer}}
.cb-btn{{border:1px solid transparent;border-radius:6px;background:var(--accent);color:#fff;padding:9px 12px;font:inherit;font-weight:600;font-size:13px;cursor:pointer}}
.cb-btn:hover{{opacity:.9}}
.cb-btn.secondary{{background:var(--bg-card);border-color:var(--border);color:var(--text)}}
.cb-main{{display:grid;grid-template-rows:auto minmax(0,1fr);min-width:0}}
.cb-head{{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg-dark)}}
.cb-status{{font-size:12px;color:var(--text-dim)}}
.cb-grid{{padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;overflow:auto;align-content:start}}
.cb-card{{background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;cursor:pointer;min-width:0;transition:border-color .12s,transform .12s}}
.cb-card:hover{{border-color:var(--accent-dim);transform:translateY(-1px)}}
.cb-img{{aspect-ratio:4/3;background:var(--bg-card);display:flex;align-items:center;justify-content:center;color:var(--text-dim);overflow:hidden;position:relative}}
.cb-img img,.cb-img video{{width:100%;height:100%;object-fit:cover;display:block}}
.cb-vbadge{{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.7);color:#fff;font-size:9px;font-weight:600;padding:1px 6px;border-radius:4px;letter-spacing:.5px;pointer-events:none;z-index:1}}
/* NSFW blur + click-to-reveal (works in both the grid and the drawer strip) */
.cb-blur{{filter:blur(22px);transform:scale(1.08)}}
.cb-reveal{{position:absolute;inset:0;margin:auto;width:fit-content;height:fit-content;background:rgba(0,0,0,.66);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:6px 12px;font:inherit;font-size:11px;font-weight:600;cursor:pointer;z-index:2;white-space:nowrap}}
.cb-reveal:hover{{background:rgba(0,0,0,.88)}}
/* Preview carousel inside the model drawer */
.cb-shots{{display:flex;gap:8px;overflow-x:auto;padding:2px 0 10px;scroll-snap-type:x mandatory}}
.cb-shots::-webkit-scrollbar{{height:7px}}
.cb-shots::-webkit-scrollbar-thumb{{background:var(--border-light);border-radius:4px}}
.cb-shot{{position:relative;flex:0 0 auto;width:150px;aspect-ratio:3/4;border-radius:6px;overflow:hidden;background:var(--bg-card);scroll-snap-align:start;cursor:zoom-in}}
.cb-shot img,.cb-shot video{{width:100%;height:100%;object-fit:cover;display:block}}
/* Lightbox for an enlarged preview */
.cb-lightbox{{position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:1400;display:none;align-items:center;justify-content:center;padding:24px;cursor:zoom-out}}
.cb-lightbox.open{{display:flex}}
.cb-lightbox img,.cb-lightbox video{{max-width:100%;max-height:100%;border-radius:8px;box-shadow:0 12px 40px rgba(0,0,0,.5)}}
.cb-meta{{padding:10px}}
.cb-title{{font-size:13px;font-weight:600;color:var(--text-bright);line-height:1.25;margin-bottom:6px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}}
.cb-sub{{font-size:11px;color:var(--text-dim);display:flex;gap:6px;flex-wrap:wrap}}
.cb-pill{{border:1px solid var(--border);background:var(--bg-card);border-radius:999px;padding:1px 6px;white-space:nowrap}}
.cb-empty{{padding:36px;color:var(--text-dim);font-size:13px}}
.cb-more{{margin:0 16px 16px;display:none}}
.cb-drawer{{position:fixed;right:0;top:48px;bottom:0;width:min(560px,100vw);background:var(--bg-panel);border-left:1px solid var(--border-light);box-shadow:-12px 0 30px rgba(0,0,0,.35);z-index:1200;transform:translateX(100%);transition:transform .16s;display:flex;flex-direction:column}}
.cb-drawer.open{{transform:translateX(0)}}
.cb-drawer-head{{display:flex;align-items:center;gap:10px;padding:13px 16px;border-bottom:1px solid var(--border)}}
.cb-drawer-title{{font-weight:600;color:var(--text-bright);font-size:14px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cb-close{{margin-left:auto;background:none;border:1px solid transparent;color:var(--text-dim);border-radius:6px;width:30px;height:30px;cursor:pointer}}
.cb-close:hover{{background:var(--bg-hover);color:var(--text)}}
.cb-drawer-body{{padding:16px;overflow:auto;font-size:13px}}
.cb-version{{border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--bg-main);margin-bottom:10px}}
.cb-version h3{{font-size:13px;color:var(--text-bright);margin:0 0 8px}}
.cb-file{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;border-top:1px solid var(--border);padding-top:8px;margin-top:8px}}
.cb-file-name{{font-family:var(--mono);font-size:11px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cb-file-actions{{display:flex;gap:6px;align-items:center;flex-shrink:0}}
.cb-btn.cb-dl{{padding:5px 10px;font-size:11px;min-width:74px;text-align:center}}
.cb-btn.cb-dl:disabled{{opacity:.85;cursor:default}}
.cb-btn.cb-dl-done{{background:var(--green,#2ea043);border-color:transparent}}
.cb-btn.secondary{{padding:5px 10px;font-size:11px}}
.cb-link{{color:var(--accent);text-decoration:none}}
.cb-link:hover{{text-decoration:underline}}
.cb-trigger{{font-family:var(--mono);font-size:11px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px;margin-top:8px;white-space:pre-wrap;color:var(--prompt-text)}}
@media (max-width:760px){{.cb-wrap{{grid-template-columns:1fr}}.cb-side{{border-right:none;border-bottom:1px solid var(--border)}}}}
</style>

<div class="cb-wrap">
  <aside class="cb-side">
    <h2>Civitai Browser</h2>
    <div class="cb-form">
      <div class="cb-field">
        <span class="cb-label">Search</span>
        <input class="cb-input" id="cbQ" placeholder="name, or paste a Civitai link / ID" onkeydown="if(event.key==='Enter')cbSearch(true)">
      </div>
      <div class="cb-row">
        <div class="cb-field">
          <span class="cb-label">Search type</span>
          <select class="cb-select" id="cbSearchType">
            <option value="model">Model name</option>
            <option value="tag">Tag</option>
            <option value="user">Username</option>
          </select>
        </div>
        <div class="cb-field">
          <span class="cb-label">Domain</span>
          <select class="cb-select" id="cbDomain">
            <option value="full">Full catalog</option>
            <option value="sfw">SFW only</option>
          </select>
        </div>
      </div>
      <label class="cb-check"><input type="checkbox" id="cbExact"> Exact model search</label>
      <label class="cb-check"><input type="checkbox" id="cbBlur" onchange="cbBlurNsfw=this.checked;localStorage.setItem('cbBlurNsfw',this.checked?'1':'0');cbRender()"> Blur NSFW previews</label>
      <div class="cb-row">
        <div class="cb-field">
          <span class="cb-label">Type</span>
          <select class="cb-select" id="cbType">
            <option value="">Any</option>
            {_option_tags(MODEL_TYPES)}
          </select>
        </div>
        <div class="cb-field">
          <span class="cb-label">Content</span>
          <select class="cb-select" id="cbNsfw">
            <option value="true">Include NSFW</option>
            <option value="false">SFW only</option>
          </select>
        </div>
      </div>
      <div class="cb-field">
        <span class="cb-label">Base model</span>
        <select class="cb-select" id="cbBase"><option value="">Any</option>{_option_tags(DEFAULT_BASE_MODELS)}</select>
      </div>
      <div class="cb-row">
        <div class="cb-field">
          <span class="cb-label">Sort</span>
          <select class="cb-select" id="cbSort">
            <option value="Newest">Newest</option>
            <option value="Most Downloaded">Most Downloaded</option>
            <option value="Highest Rated">Highest Rated</option>
            <option value="Most Liked">Most Liked</option>
            <option value="Most Discussed">Most Discussed</option>
          </select>
        </div>
        <div class="cb-field">
          <span class="cb-label">Period</span>
          <select class="cb-select" id="cbPeriod">
            <option value="">All time</option>
            <option value="Day">Day</option>
            <option value="Week">Week</option>
            <option value="Month">Month</option>
            <option value="Year">Year</option>
          </select>
        </div>
      </div>
      <button class="cb-btn" onclick="cbSearch(true)">Search</button>
      <button class="cb-btn secondary" onclick="cbReset()">Reset</button>
    </div>
  </aside>

  <main class="cb-main">
    <div class="cb-head">
      <div class="cb-status" id="cbStatus">Ready.</div>
      <div class="topbar-spacer"></div>
      <button class="cb-btn secondary" id="cbMore" onclick="cbSearch(false)">Load more</button>
    </div>
    <div class="cb-grid" id="cbGrid"><div class="cb-empty">Search Civitai models from inside CyberHub.</div></div>
  </main>
</div>

<div class="cb-drawer" id="cbDrawer">
  <div class="cb-drawer-head">
    <div class="cb-drawer-title" id="cbDrawerTitle">Model</div>
    <button class="cb-close" onclick="cbCloseDrawer()">x</button>
  </div>
  <div class="cb-drawer-body" id="cbDrawerBody"></div>
</div>

<div class="cb-lightbox" id="cbLightbox" onclick="cbCloseZoom()"></div>

<script>
var cbCursor = '';
var cbLoading = false;
var cbItems = [];
var cbBlurNsfw = localStorage.getItem('cbBlurNsfw') !== '0';   // default on, remembered
var cbArchiveReady = __ARCHIVE_READY__;
var cbForgeReady = __FORGE_READY__;

// Civitai nsfwLevel: 1=PG, 2=PG13, 4=R, 8=X, 16=XXX. Blur R and above.
function cbIsNsfw(media) {{
  if (!media) return false;
  if (media.nsfw === true) return true;
  return (Number(media.nsfwLevel) || 0) >= 4;
}}

function cbParams(reset) {{
  if (reset) cbCursor = '';
  var p = new URLSearchParams();
  var q = document.getElementById('cbQ').value.trim();
  if (q) p.set('q', q);
  p.set('search_type', document.getElementById('cbSearchType').value);
  p.set('domain', document.getElementById('cbDomain').value);
  if (document.getElementById('cbExact').checked) p.set('exact', '1');
  var type = document.getElementById('cbType').value;
  if (type) p.set('types', type);
  var nsfw = document.getElementById('cbNsfw').value;
  if (nsfw) p.set('nsfw', nsfw);
  var base = document.getElementById('cbBase').value;
  if (base) p.set('baseModels', base);
  var sort = document.getElementById('cbSort').value;
  if (sort) p.set('sort', sort);
  var period = document.getElementById('cbPeriod').value;
  if (period) p.set('period', period);
  if (cbCursor) p.set('cursor', cbCursor);
  p.set('limit', '100');
  return p;
}}

var cbAutoFill = false;
var CB_AUTOFILL_TARGET = 48;   // text search returns tiny batches — top up the grid

function cbSearch(reset) {{
  if (cbLoading) return;
  cbLoading = true;
  if (reset) {{
    cbItems = [];
    cbAutoFill = true;
    cbRender();
  }}
  document.getElementById('cbStatus').textContent =
    cbItems.length ? ('Loading more… (' + cbItems.length + ' so far)') : 'Loading Civitai…';
  fetch('/api/civitai-browser/search?' + cbParams(reset).toString())
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      if (d.error) throw new Error(d.error);
      cbItems = cbItems.concat(d.items || []);
      cbCursor = (d.metadata && d.metadata.nextCursor) || '';
      cbRender();
      document.getElementById('cbMore').style.display = cbCursor ? '' : 'none';
      document.getElementById('cbStatus').textContent = (cbItems.length || 0) + ' models loaded' + (cbCursor ? ' - more available' : '');
    }})
    .catch(function(e){{
      cbAutoFill = false;
      document.getElementById('cbStatus').textContent = 'Error: ' + e.message;
      if (!cbItems.length) document.getElementById('cbGrid').innerHTML = '<div class="cb-empty">Could not load Civitai: ' + escHtml(e.message) + '</div>';
    }})
    .finally(function(){{
      cbLoading = false;
      // Meilisearch text search hands back small batches — keep chaining the
      // cursor until the first screen is comfortably full, then stop.
      if (cbAutoFill && cbCursor && cbItems.length < CB_AUTOFILL_TARGET) {{
        cbSearch(false);
      }} else {{
        cbAutoFill = false;
      }}
    }});
}}

function cbRender() {{
  var grid = document.getElementById('cbGrid');
  if (!cbItems.length) {{
    grid.innerHTML = '<div class="cb-empty">No models loaded yet.</div>';
    return;
  }}
  grid.innerHTML = cbItems.map(cbCardHtml).join('');
}}

// Civitai CDN serves full-resolution originals by default (often 2-3 MB each —
// brutal for a grid). Rewrite the transform segment (the path part containing
// '=', right before the filename) to a width-capped preview. Videos get a
// transcoded, smaller mp4.
function cbMediaUrl(url, isVideo, size) {{
  if (!url) return '';
  size = size || 450;
  var t = isVideo ? ('transcode=true,width=' + size + ',quality=80') : ('width=' + size);
  return url.replace(/\\/[^/]*=[^/]*\\/([^/?]+)(\\?.*)?$/, '/' + t + '/$1$2');
}}

function cbMediaTag(media, size) {{
  media = media || {{}};
  var url = media.url || '';
  if (!url) return 'No preview';
  var blur = (cbBlurNsfw && cbIsNsfw(media)) ? ' cb-blur' : '';
  var inner;
  if (media.type === 'video') {{
    // preload=metadata shows the first frame without pulling the whole clip;
    // play on hover keeps the grid light.
    inner = '<span class="cb-vbadge">VIDEO</span><video class="' + blur.trim() + '" muted loop playsinline preload="metadata" ' +
           'onmouseenter="this.play()" onmouseleave="this.pause()">' +
           '<source src="' + escAttr(cbMediaUrl(url, true, size)) + '" type="video/mp4"></video>';
  }} else {{
    inner = '<img class="' + blur.trim() + '" loading="lazy" src="' + escAttr(cbMediaUrl(url, false, size)) + '">';
  }}
  if (blur) inner += '<button class="cb-reveal" onclick="cbReveal(event,this)">NSFW — show</button>';
  return inner;
}}

function cbReveal(ev, btn) {{
  ev.stopPropagation();
  var m = btn.parentNode.querySelector('.cb-blur');
  if (m) m.classList.remove('cb-blur');
  btn.remove();
}}

function cbCardHtml(m) {{
  var v = (m.modelVersions || [])[0] || {{}};
  var media = (v.images || [])[0] || {{}};
  var stats = m.stats || {{}};
  var badges = [m.type, v.baseModel].filter(Boolean).map(function(x){{ return '<span class="cb-pill">' + escHtml(x) + '</span>'; }}).join('');
  return '<div class="cb-card" onclick="cbOpenModel(' + Number(m.id) + ')">' +
    '<div class="cb-img">' + cbMediaTag(media) + '</div>' +
    '<div class="cb-meta">' +
      '<div class="cb-title">' + escHtml(m.name || 'Untitled') + '</div>' +
      '<div class="cb-sub">' + badges + '<span class="cb-pill">' + Number(stats.downloadCount || 0).toLocaleString() + ' downloads</span></div>' +
      '<div class="cb-sub" style="margin-top:6px">by ' + escHtml((m.creator && m.creator.username) || 'unknown') + '</div>' +
    '</div></div>';
}}

function cbOpenModel(id) {{
  var domain = document.getElementById('cbDomain').value;
  var drawer = document.getElementById('cbDrawer');
  drawer.classList.add('open');
  document.getElementById('cbDrawerTitle').textContent = 'Loading...';
  document.getElementById('cbDrawerBody').innerHTML = '<div class="cb-empty">Loading model details...</div>';
  fetch('/api/civitai-browser/model?id=' + encodeURIComponent(id) + '&domain=' + encodeURIComponent(domain))
    .then(function(r){{ return r.json(); }})
    .then(function(m){{
      if (m.error) throw new Error(m.error);
      document.getElementById('cbDrawerTitle').textContent = m.name || 'Model';
      document.getElementById('cbDrawerBody').innerHTML = cbModelHtml(m);
    }})
    .catch(function(e){{
      document.getElementById('cbDrawerTitle').textContent = 'Error';
      document.getElementById('cbDrawerBody').innerHTML = '<div class="cb-empty">' + escHtml(e.message) + '</div>';
    }});
}}

function cbModelHtml(m) {{
  var domain = (m._hub && m._hub.domain) || 'civitai.red';
  var pageUrl = 'https://' + domain + '/models/' + encodeURIComponent(m.id);
  var html = '<p><a class="cb-link" target="_blank" href="' + escAttr(pageUrl) + '">Open on Civitai</a></p>';
  html += '<p class="cb-sub">' + escHtml(m.type || '') + ' by ' + escHtml((m.creator && m.creator.username) || 'unknown') + '</p>';
  (m.tags || []).slice(0, 18).forEach(function(t){{ html += '<span class="cb-pill">' + escHtml(t) + '</span> '; }});
  html += '<div style="height:12px"></div>';
  (m.modelVersions || []).forEach(function(v){{ html += cbVersionHtml(v, m.id); }});
  return html;
}}

function cbVersionHtml(v, modelId) {{
  var words = (v.trainedWords || []).filter(Boolean);
  var html = '<div class="cb-version">';
  html += '<h3>' + escHtml(v.name || ('Version ' + v.id)) + '</h3>';
  html += '<div class="cb-sub"><span class="cb-pill">' + escHtml(v.baseModel || 'Base unknown') + '</span><span class="cb-pill">' + escHtml((v.files || []).length + ' files') + '</span></div>';
  var shots = (v.images || []).slice(0, 16);
  if (shots.length) {{
    html += '<div class="cb-shots">' + shots.map(function(m){{
      var big = escAttr(cbMediaUrl(m.url || '', m.type === 'video', 900));
      return '<div class="cb-shot" data-full="' + big + '" data-video="' + (m.type === 'video' ? '1' : '') + '" onclick="cbZoom(this)">' + cbMediaTag(m, 320) + '</div>';
    }}).join('') + '</div>';
  }}
  if (words.length) {{
    html += '<div class="cb-trigger">' + escHtml(words.join('\\n')) + '</div>';
  }}
  (v.files || []).forEach(function(f){{
    var size = f.sizeKB ? formatSize(f.sizeKB * 1024) : '';
    var url = f._hubDownloadUrl || f.downloadUrl || '';
    html += '<div class="cb-file"><div class="cb-file-name" title="' + escAttr(f.name || '') + '">' + escHtml(f.name || 'file') + '<br><span class="cb-sub">' + escHtml((f.type || '') + (size ? ' - ' + size : '')) + '</span></div>';
    html += '<div class="cb-file-actions">';
    var args = Number(modelId) + ',' + Number(v.id) + ',' + Number(f.id || 0);
    if (cbArchiveReady) {{
      html += '<button class="cb-btn cb-dl" title="Archive: Type/BaseModel/Model/Version" onclick="cbDownload(' + args + ',\\'archive\\',this)">Archive</button>';
    }}
    if (cbForgeReady) {{
      html += '<button class="cb-btn cb-dl" title="Forge/ComfyUI: flat into the models subfolder" onclick="cbDownload(' + args + ',\\'forge\\',this)">Forge</button>';
    }}
    html += url ? '<a class="cb-btn secondary" target="_blank" href="' + escAttr(url) + '">Browser</a>' : '';
    html += '</div></div>';
  }});
  html += '</div>';
  return html;
}}

function cbDownload(modelId, versionId, fileId, mode, btn) {{
  var domain = document.getElementById('cbDomain').value;
  btn.disabled = true;
  var orig = btn.textContent;
  btn.textContent = 'Starting…';
  fetch('/api/civitai-browser/download', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{model_id: modelId, version_id: versionId, file_id: fileId, domain: domain, mode: mode}})
  }})
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      if (d.error || !d.id) throw new Error(d.error || 'could not start');
      cbPollDownload(d.id, btn, orig);
    }})
    .catch(function(e){{ btn.textContent = 'Error'; btn.title = e.message; btn.disabled = false; }});
}}

function cbPollDownload(id, btn, orig) {{
  fetch('/api/civitai-browser/download-status?id=' + encodeURIComponent(id))
    .then(function(r){{ return r.json(); }})
    .then(function(s){{
      if (s.state === 'done') {{
        btn.textContent = s.result === 'skipped' ? '✓ Exists' : '✓ Saved';
        btn.classList.add('cb-dl-done');
        return;
      }}
      if (s.state === 'failed') {{
        btn.textContent = 'Failed';
        btn.title = s.error || '';
        btn.disabled = false;
        return;
      }}
      btn.textContent = (s.state === 'downloading' && s.pct != null) ? (s.pct + '%') : 'Working…';
      setTimeout(function(){{ cbPollDownload(id, btn, orig); }}, 700);
    }})
    .catch(function(){{ btn.textContent = 'Failed'; btn.disabled = false; }});
}}

function cbCloseDrawer() {{
  document.getElementById('cbDrawer').classList.remove('open');
}}

function cbZoom(el) {{
  if (el.querySelector('.cb-blur')) return;  // blurred — click 'show' first
  var url = el.getAttribute('data-full');
  var isVid = el.getAttribute('data-video') === '1';
  var lb = document.getElementById('cbLightbox');
  lb.innerHTML = isVid
    ? '<video src="' + escAttr(url) + '" autoplay loop muted playsinline controls></video>'
    : '<img src="' + escAttr(url) + '">';
  lb.classList.add('open');
}}

function cbCloseZoom() {{
  var lb = document.getElementById('cbLightbox');
  lb.classList.remove('open');
  lb.innerHTML = '';
}}

function cbReset() {{
  ['cbQ','cbType','cbBase','cbPeriod'].forEach(function(id){{ document.getElementById(id).value = ''; }});
  document.getElementById('cbNsfw').value = 'true';
  document.getElementById('cbSearchType').value = 'model';
  document.getElementById('cbDomain').value = 'full';
  document.getElementById('cbSort').value = 'Newest';
  document.getElementById('cbExact').checked = false;
  document.getElementById('cbBlur').checked = true;
  cbBlurNsfw = true;
  cbItems = [];
  cbCursor = '';
  document.getElementById('cbMore').style.display = 'none';
  document.getElementById('cbStatus').textContent = 'Ready.';
  cbRender();
}}

function cbLoadBaseModels() {{
  fetch('/api/civitai-browser/base-models?domain=' + encodeURIComponent(document.getElementById('cbDomain').value))
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      var base = document.getElementById('cbBase');
      var current = base.value;
      var options = '<option value="">Any</option>';
      (d.baseModels || []).forEach(function(v){{ options += '<option value="' + escAttr(v) + '">' + escHtml(v) + '</option>'; }});
      base.innerHTML = options;
      if (current) base.value = current;
    }})
    .catch(function(){{}});
}}

document.getElementById('cbBlur').checked = cbBlurNsfw;
document.getElementById('cbDomain').addEventListener('change', cbLoadBaseModels);
cbLoadBaseModels();
</script>
"""
