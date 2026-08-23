"""LoRA Info — safely inspect local Safetensors metadata without loading tensors."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import struct
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from core import Module
from core.server import build_shell


MAX_HEADER_BYTES = 64 * 1024 * 1024
PATH_REQUEST_LIMIT = 1024 * 1024
SAFETENSORS_SUFFIX = ".safetensors"


def server_locations():
    """Return useful filesystem starting points for the CyberHub computer."""
    locations = []
    seen = set()

    def add(label, path, kind="drive"):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return
        identity = os.path.normcase(os.path.realpath(path))
        if identity in seen:
            return
        seen.add(identity)
        locations.append({"label": label, "path": path, "kind": kind})

    home = os.path.expanduser("~")
    add("Home", home, "home")
    system = platform.system()
    if system == "Windows":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            add(drive, drive)
    elif system == "Darwin":
        add("System drive", "/")
        if os.path.isdir("/Volumes"):
            try:
                for name in sorted(os.listdir("/Volumes"), key=str.lower):
                    add(name, os.path.join("/Volumes", name))
            except OSError:
                pass
    else:
        add("System drive", "/")
        for root in ("/mnt", "/media", os.path.join("/run/media", os.path.basename(home))):
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root), key=str.lower)
            except OSError:
                continue
            if not entries:
                add(os.path.basename(root) or root, root)
            for name in entries:
                add(name, os.path.join(root, name))
    return locations


def _jsonish(value):
    """Decode the JSON strings commonly stored inside Safetensors metadata."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{\"-0123456789" or stripped in ("-", "."):
        return value
    try:
        # Some LoRA trainers write JavaScript-style non-finite constants even
        # though JSON itself does not support them. Keep those values readable
        # without ever returning invalid JSON to the browser.
        return json.loads(stripped, parse_constant=lambda token: token)
    except (json.JSONDecodeError, TypeError, ValueError):
        return value


def _json_safe(value):
    """Return a recursively strict-JSON-safe representation of *value*."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _first(metadata, *keys, default=None):
    for key in keys:
        value = metadata.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return default


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _shape_product(shape):
    if not isinstance(shape, list):
        return 0
    total = 1
    for size in shape:
        if not isinstance(size, int) or size < 0:
            return 0
        total *= size
    return total


def _format_timestamp(value):
    number = _number(value)
    if number is None:
        return value or None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return value


def _tag_frequency(value):
    """Aggregate Kohya tag-frequency data across all dataset folders."""
    counts = Counter()

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, (int, float)) and not isinstance(child, bool):
                    if math.isfinite(float(child)):
                        counts[str(key)] += child
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return [
        {"tag": tag, "count": int(count) if float(count).is_integer() else count}
        for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]


def _dataset_image_count(value):
    if not isinstance(value, dict):
        return None
    total = 0
    found = False
    for item in value.values():
        if isinstance(item, dict):
            count = _number(item.get("img_count"))
            if count is not None:
                total += count
                found = True
    return total if found else None


def _tensor_group(name):
    lowered = name.lower()
    if any(token in lowered for token in ("lora_te", "text_encoder", "text_model", "clip_")):
        return "Text encoder"
    if any(token in lowered for token in ("lora_unet", "diffusion_model", "transformer", "unet")):
        return "Denoiser"
    return "Other"


def _tensor_stats(header, data_size):
    by_dtype = {}
    by_group = {}
    warnings = []
    tensor_count = 0
    parameter_count = 0
    tensor_bytes = 0

    for name, descriptor in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(descriptor, dict):
            warnings.append(f"Tensor {name!r} has no valid descriptor")
            continue
        dtype = str(descriptor.get("dtype") or "Unknown")
        shape = descriptor.get("shape")
        offsets = descriptor.get("data_offsets")
        params = _shape_product(shape)
        byte_count = 0
        valid_offsets = (
            isinstance(offsets, list) and len(offsets) == 2
            and all(isinstance(item, int) for item in offsets)
            and 0 <= offsets[0] <= offsets[1] <= data_size
        )
        if valid_offsets:
            byte_count = offsets[1] - offsets[0]
        else:
            warnings.append(f"Tensor {name!r} has invalid data offsets")

        tensor_count += 1
        parameter_count += params
        tensor_bytes += byte_count
        dtype_row = by_dtype.setdefault(dtype, {"dtype": dtype, "tensors": 0, "parameters": 0, "bytes": 0})
        dtype_row["tensors"] += 1
        dtype_row["parameters"] += params
        dtype_row["bytes"] += byte_count
        group = _tensor_group(name)
        group_row = by_group.setdefault(group, {"group": group, "tensors": 0, "parameters": 0, "bytes": 0})
        group_row["tensors"] += 1
        group_row["parameters"] += params
        group_row["bytes"] += byte_count

    return {
        "count": tensor_count,
        "parameters": parameter_count,
        "bytes": tensor_bytes,
        "by_dtype": sorted(by_dtype.values(), key=lambda row: (-row["parameters"], row["dtype"])),
        "by_group": sorted(by_group.values(), key=lambda row: (-row["parameters"], row["group"])),
    }, warnings


def analyze_safetensors_header(
        header, *, filename="", file_size=None, sha256=None, source="browser",
        header_bytes_len=None):
    """Create a UI-friendly LoRA report from a decoded Safetensors header."""
    if not isinstance(header, dict):
        raise ValueError("The Safetensors header must be a JSON object")
    raw_metadata = header.get("__metadata__", {})
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict):
        raise ValueError("The __metadata__ value must be an object")
    stored_metadata = {str(key): _json_safe(value) for key, value in raw_metadata.items()}
    metadata = {key: _jsonish(value) for key, value in stored_metadata.items()}

    header_bytes = header_bytes_len
    if header_bytes is None:
        header_bytes = len(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if file_size is None:
        file_size = 8 + header_bytes
    try:
        file_size = int(file_size)
    except (TypeError, ValueError):
        raise ValueError("Invalid file size") from None
    if file_size < 8:
        raise ValueError("The file is too small to be a Safetensors file")
    data_size = max(0, file_size - 8 - header_bytes)
    tensors, warnings = _tensor_stats(header, data_size)
    if not raw_metadata:
        warnings.insert(0, "This file has no embedded LoRA metadata")

    dataset_dirs = metadata.get("ss_dataset_dirs")
    tag_frequency = _tag_frequency(metadata.get("ss_tag_frequency"))
    network_args = metadata.get("ss_network_args")
    optimizer_args = metadata.get("ss_optimizer_args")
    resolution = _first(metadata, "ss_resolution", "modelspec.resolution")
    if isinstance(resolution, (list, tuple)):
        resolution = " × ".join(str(part) for part in resolution)

    started_raw = metadata.get("ss_training_started_at")
    finished_raw = metadata.get("ss_training_finished_at")
    started_num = _number(started_raw)
    finished_num = _number(finished_raw)
    duration_seconds = None
    if started_num is not None and finished_num is not None and finished_num >= started_num:
        duration_seconds = finished_num - started_num

    model_name = _first(
        metadata, "modelspec.title", "ss_output_name", "modelspec.name",
        default=Path(filename).stem if filename else "Unknown LoRA",
    )
    summary = {
        "name": model_name,
        "author": metadata.get("modelspec.author"),
        "description": _first(metadata, "modelspec.description", "modelspec.usage_hint"),
        "base_model": _first(metadata, "ss_sd_model_name", "modelspec.architecture"),
        "architecture": metadata.get("modelspec.architecture"),
        "implementation": metadata.get("modelspec.implementation"),
        "network_module": metadata.get("ss_network_module"),
        "network_dim": _number(metadata.get("ss_network_dim")),
        "network_alpha": _number(metadata.get("ss_network_alpha")),
        "network_args": network_args,
        "precision": _first(metadata, "ss_mixed_precision", "ss_save_precision"),
        "optimizer": metadata.get("ss_optimizer"),
        "optimizer_args": optimizer_args,
        "scheduler": metadata.get("ss_lr_scheduler"),
        "learning_rate": _number(metadata.get("ss_learning_rate")),
        "unet_lr": _number(metadata.get("ss_unet_lr")),
        "text_encoder_lr": _number(metadata.get("ss_text_encoder_lr")),
        "batch_size": _number(metadata.get("ss_batch_size_per_device")),
        "gradient_accumulation": _number(metadata.get("ss_gradient_accumulation_steps")),
        "epoch": _number(metadata.get("ss_epoch")),
        "epochs": _number(metadata.get("ss_num_epochs")),
        "steps": _number(metadata.get("ss_steps")),
        "max_steps": _number(metadata.get("ss_max_train_steps")),
        "resolution": resolution,
        "clip_skip": _number(metadata.get("ss_clip_skip")),
        "seed": _number(metadata.get("ss_seed")),
        "dataset_images": _dataset_image_count(dataset_dirs),
        "training_started": _format_timestamp(started_raw),
        "training_finished": _format_timestamp(finished_raw),
        "training_duration_seconds": duration_seconds,
    }
    stored_hashes = {
        "model": metadata.get("sshs_model_hash"),
        "legacy": metadata.get("sshs_legacy_hash"),
    }
    hashes = {
        "sha256": sha256,
        "autov2": sha256[:10] if sha256 else None,
        "stored_model": stored_hashes["model"],
        "stored_legacy": stored_hashes["legacy"],
    }
    report = {
        "file": {
            "name": os.path.basename(filename) if filename else "LoRA.safetensors",
            "size": file_size,
            "header_bytes": header_bytes,
            "source": source,
        },
        "summary": summary,
        "hashes": hashes,
        "tags": tag_frequency,
        "tensors": tensors,
        "metadata": stored_metadata,
        "metadata_count": len(metadata),
        "warnings": warnings[:25],
    }
    return _json_safe(report)


def parse_safetensors_header(header_bytes, **kwargs):
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("Safetensors header is larger than the 64 MB safety limit")
    try:
        header = json.loads(
            header_bytes.decode("utf-8"),
            parse_constant=lambda token: token,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("Safetensors header is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Safetensors JSON header: {exc.msg}") from exc
    return analyze_safetensors_header(header, header_bytes_len=len(header_bytes), **kwargs)


def read_safetensors_file(path, *, include_hash=True):
    """Read only the header; hash the file incrementally when requested."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not path.lower().endswith(SAFETENSORS_SUFFIX):
        raise ValueError("Choose a .safetensors file")
    if not os.path.isfile(path):
        raise FileNotFoundError("LoRA file not found")
    file_size = os.path.getsize(path)
    if file_size < 8:
        raise ValueError("The file is too small to be a Safetensors file")

    digest = hashlib.sha256() if include_hash else None
    with open(path, "rb") as source:
        prefix = source.read(8)
        if digest:
            digest.update(prefix)
        header_size = struct.unpack("<Q", prefix)[0]
        if header_size > MAX_HEADER_BYTES:
            raise ValueError("Safetensors header is larger than the 64 MB safety limit")
        if header_size > file_size - 8:
            raise ValueError("Safetensors header extends beyond the end of the file")
        header_bytes = source.read(header_size)
        if len(header_bytes) != header_size:
            raise ValueError("Safetensors header is incomplete")
        report = parse_safetensors_header(
            header_bytes,
            filename=path,
            file_size=file_size,
            source="hub",
        )
        if digest:
            digest.update(header_bytes)
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)

    if digest:
        sha256 = digest.hexdigest()
        report["hashes"]["sha256"] = sha256
        report["hashes"]["autov2"] = sha256[:10]
    return report


class LoRAInfoModule(Module):
    name = "LoRA Info"
    version = "1.0.3"
    icon = "\U0001F9EC"
    description = "Inspect LoRA Safetensors metadata, training tags, tensors and hashes locally."
    order = 28
    show_in_tabs = True
    settings_schema = {}

    def key(self):
        return "lora_info"

    def routes_get(self):
        return {
            "/lora_info": self._page,
            # Backward-compatible alias for links created by the first version.
            "/lora-info": self._page,
            "/api/lora-info/locations": self._locations,
        }

    def routes_post(self):
        return {
            "/api/lora-info/analyze-path": self._analyze_path,
            "/api/lora-info/analyze-header": self._analyze_header,
        }

    def _page(self, handler, qs):
        handler.respond_html(build_shell(
            self.hub.registry,
            self.hub.settings,
            active_key="lora_info",
            page_title="LoRA Info",
            body_html=PAGE_BODY,
        ))

    def _server_file_access_allowed(self, handler):
        if handler._is_loopback():
            return True
        if self.hub.settings.get_path("network.allow_remote_browse", False):
            return True
        return bool(handler.require_auth and handler.auth_token)

    def _analyze_path(self, handler, content_len, content_type):
        if content_len > PATH_REQUEST_LIMIT:
            handler.respond_json({"error": "Request is too large"}, status=413)
            return
        if not self._server_file_access_allowed(handler):
            handler.respond_json({
                "error": "Hub files can only be opened locally unless remote folder browsing or LAN authentication is enabled."
            }, status=403)
            return
        data = handler.read_body_json(content_len)
        if not isinstance(data, dict):
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        try:
            report = read_safetensors_file(data.get("path"), include_hash=bool(data.get("include_hash", True)))
        except (ValueError, FileNotFoundError, OSError) as exc:
            handler.respond_json({"error": str(exc)}, status=400)
            return
        handler.respond_json(report)

    def _locations(self, handler, qs):
        if not self._server_file_access_allowed(handler):
            handler.respond_json({
                "error": "Hub drives can only be browsed locally unless remote folder browsing or LAN authentication is enabled."
            }, status=403)
            return
        handler.respond_json({"locations": server_locations()})

    def _analyze_header(self, handler, content_len, content_type):
        if content_len > MAX_HEADER_BYTES + (1024 * 1024):
            handler.respond_json({"error": "Metadata request exceeds the 64 MB safety limit"}, status=413)
            return
        data = handler.read_body_json(content_len)
        if not isinstance(data, dict):
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        header_text = data.get("header")
        if not isinstance(header_text, str):
            handler.respond_json({"error": "No Safetensors header received"}, status=400)
            return
        try:
            report = parse_safetensors_header(
                header_text.encode("utf-8"),
                filename=str(data.get("filename") or ""),
                file_size=data.get("file_size"),
                source="browser",
            )
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)
            return
        handler.respond_json(report)


PAGE_BODY = r"""
<style>
.li-wrap{max-width:1180px;margin:0 auto;padding:22px}.li-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:18px}.li-title h1{font-size:22px;color:var(--text-bright);margin:0 0 4px}.li-title p{color:var(--text-dim);max-width:700px}.li-badge{font:10px var(--mono);text-transform:uppercase;letter-spacing:.7px;color:var(--green);border:1px solid color-mix(in srgb,var(--green) 45%,transparent);background:color-mix(in srgb,var(--green) 8%,transparent);border-radius:999px;padding:5px 9px;white-space:nowrap}
.li-source{background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:16px}.li-tabs{display:flex;border-bottom:1px solid var(--border);background:var(--bg-dark)}.li-tab{border:0;border-right:1px solid var(--border);background:transparent;color:var(--text-dim);padding:11px 16px;cursor:pointer;font:12px var(--font)}.li-tab.active{color:var(--accent);background:var(--bg-panel);box-shadow:inset 0 2px 0 var(--accent)}.li-source-pane{padding:16px}.li-source-pane[hidden]{display:none}.li-drop{border:1px dashed var(--border-light);border-radius:8px;padding:30px 18px;text-align:center;cursor:pointer;background:var(--bg-card);transition:.15s}.li-drop:hover,.li-drop.drag{border-color:var(--accent);background:var(--accent-glow)}.li-drop strong{display:block;color:var(--text-bright);font-size:14px;margin-bottom:4px}.li-drop span{font-size:12px;color:var(--text-dim)}
.li-path-row{display:flex;gap:8px}.li-input{flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:9px 10px;font:12px var(--mono)}.li-btn{border:1px solid var(--border-light);background:var(--bg-card);color:var(--text);border-radius:6px;padding:8px 13px;cursor:pointer;font-size:12px}.li-btn:hover{border-color:var(--accent);color:var(--accent)}.li-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.li-btn:disabled{opacity:.45;cursor:not-allowed}.li-note{font-size:11px;color:var(--text-dim);margin-top:9px}.li-check{display:inline-flex;align-items:center;gap:6px;margin-top:10px;color:var(--text-dim);font-size:11px}
.li-status{display:none;align-items:center;gap:10px;background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:16px;color:var(--text-dim)}.li-status.show{display:flex}.li-spin{width:15px;height:15px;border:2px solid var(--border-light);border-top-color:var(--accent);border-radius:50%;animation:li-spin .8s linear infinite}@keyframes li-spin{to{transform:rotate(360deg)}}
.li-result{display:none}.li-result.show{display:block}.li-model-head{background:linear-gradient(120deg,var(--bg-panel),var(--bg-card));border:1px solid var(--border);border-radius:10px;padding:17px 18px;margin-bottom:14px}.li-model-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.li-model-name{font-size:20px;color:var(--text-bright);overflow-wrap:anywhere}.li-file-name{font:11px var(--mono);color:var(--text-dim);margin-top:3px}.li-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.li-chip{font-size:11px;background:var(--bg-active);border:1px solid var(--border-light);border-radius:999px;padding:4px 8px;color:var(--text)}
.li-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:12px}.li-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:9px;padding:14px 16px;min-width:0}.li-card h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--text-dim);margin:0 0 10px}.li-kv{display:grid;grid-template-columns:minmax(120px,.7fr) minmax(0,1.3fr);gap:7px 12px;font-size:12px}.li-k{color:var(--text-dim)}.li-v{color:var(--text);overflow-wrap:anywhere}.li-v.mono{font-family:var(--mono);font-size:10px}.li-empty{color:var(--text-dim);font-style:italic;font-size:12px}.li-wide{grid-column:1/-1}.li-actions{display:flex;gap:7px;flex-wrap:wrap}
.li-section{background:var(--bg-panel);border:1px solid var(--border);border-radius:9px;margin-bottom:12px;overflow:hidden}.li-section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 15px;border-bottom:1px solid var(--border)}.li-section-head h2{font-size:13px;color:var(--text-bright);margin:0}.li-section-body{padding:13px 15px}.li-search{max-width:280px;width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:5px;color:var(--text);padding:6px 8px;font-size:11px}.li-table-wrap{overflow:auto;max-height:420px}.li-table{width:100%;border-collapse:collapse;font-size:11px}.li-table th{text-align:left;color:var(--text-dim);font-weight:500;border-bottom:1px solid var(--border);padding:7px 8px;position:sticky;top:0;background:var(--bg-panel);z-index:1}.li-table td{padding:7px 8px;border-bottom:1px solid var(--border);vertical-align:top}.li-table tr:last-child td{border-bottom:0}.li-table code{font:10px var(--mono);color:var(--setting-key)}.li-meta-value{white-space:pre-wrap;overflow-wrap:anywhere;max-width:760px;max-height:140px;overflow:auto;font:10px/1.45 var(--mono);color:var(--setting-val)}.li-tag-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 12px}.li-tag{position:relative;background:var(--bg-card);border-radius:5px;padding:6px 8px;overflow:hidden}.li-tag-bar{position:absolute;inset:0 auto 0 0;background:var(--accent-glow);pointer-events:none}.li-tag-text{position:relative;display:flex;justify-content:space-between;gap:8px;font-size:11px}.li-warnings{border-color:color-mix(in srgb,var(--orange) 45%,var(--border));}.li-warning{font-size:11px;color:var(--orange);margin:4px 0}
.li-browse-overlay{position:fixed;inset:0;background:rgba(0,0,0,.68);z-index:9000;display:none;align-items:center;justify-content:center;padding:16px}.li-browse-overlay.open{display:flex}.li-browse-dialog{background:var(--bg-panel);border:1px solid var(--border-light);border-radius:10px;width:620px;max-width:100%;max-height:76vh;display:flex;flex-direction:column}.li-browse-head,.li-browse-foot{padding:12px 15px;display:flex;align-items:center;gap:8px}.li-browse-head{border-bottom:1px solid var(--border)}.li-browse-head h3{font-size:14px;margin:0;flex:1}.li-browse-close{border:0;background:none;color:var(--text-dim);font-size:20px;cursor:pointer}.li-crumb{padding:8px 15px;border-bottom:1px solid var(--border);font:10px var(--mono);color:var(--accent);display:flex;flex-wrap:wrap;gap:5px}.li-crumb span[data-path]{cursor:pointer}.li-browse-body{min-height:260px;overflow:auto;padding:5px}.li-entry{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:5px;cursor:pointer;font-size:12px}.li-entry:hover{background:var(--bg-hover)}.li-entry.selected{background:var(--bg-active);color:var(--accent)}.li-browse-foot{border-top:1px solid var(--border);justify-content:flex-end}.li-browse-current{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:10px var(--mono);color:var(--text-dim)}
.li-locations{display:flex;gap:6px;overflow-x:auto;padding:9px 14px;border-bottom:1px solid var(--border);min-height:46px}.li-location{display:flex;align-items:center;gap:6px;flex:0 0 auto;border:1px solid var(--border);background:var(--bg-card);color:var(--text);border-radius:6px;padding:6px 9px;font-size:11px;cursor:pointer}.li-location:hover{border-color:var(--accent);color:var(--accent)}.li-location-path{color:var(--text-dim);font:9px var(--mono)}
@media(max-width:760px){.li-wrap{padding:14px}.li-hero{display:block}.li-badge{display:inline-block;margin-top:9px}.li-grid{grid-template-columns:1fr}.li-wide{grid-column:auto}.li-tag-grid{grid-template-columns:1fr}.li-model-row{display:block}.li-actions{margin-top:10px}.li-path-row{flex-wrap:wrap}.li-path-row .li-input{flex-basis:100%}}
</style>

<div class="li-wrap">
  <div class="li-hero">
    <div class="li-title"><h1>LoRA Info</h1><p>Inspect metadata, training settings, tag frequencies and tensor information without loading the model or running code from the file.</p></div>
    <span class="li-badge">Local &amp; read-only</span>
  </div>

  <section class="li-source">
    <div class="li-tabs" role="tablist">
      <button class="li-tab active" id="localTab" role="tab" aria-selected="true">This computer</button>
      <button class="li-tab" id="hubTab" role="tab" aria-selected="false">CyberHub computer</button>
    </div>
    <div class="li-source-pane" id="localPane">
      <div class="li-drop" id="dropZone" tabindex="0" role="button"><strong>Drop a LoRA here or click to choose</strong><span>Only the Safetensors metadata header is sent to CyberHub; the model weights stay on this computer.</span></div>
      <input id="fileInput" type="file" accept=".safetensors,application/octet-stream" hidden>
    </div>
    <div class="li-source-pane" id="hubPane" hidden>
      <div class="li-path-row"><input class="li-input" id="hubPath" placeholder="Path to a .safetensors file on the CyberHub computer"><button class="li-btn" id="browseBtn">Browse</button><button class="li-btn primary" id="inspectPathBtn">Inspect</button></div>
      <label class="li-check"><input type="checkbox" id="hashFile" checked> Calculate SHA-256 and AutoV2 (may take a little longer for very large files)</label>
      <div class="li-note">This option reads the file directly on the computer running CyberHub; the model weights do not travel over the network.</div>
    </div>
  </section>

  <div class="li-status" id="status"><span class="li-spin"></span><span id="statusText">Inspecting LoRA…</span></div>
  <div class="li-result" id="result">
    <section class="li-model-head"><div class="li-model-row"><div><div class="li-model-name" id="modelName"></div><div class="li-file-name" id="fileName"></div></div><div class="li-actions"><button class="li-btn" id="copyJson">Copy metadata</button><button class="li-btn" id="downloadJson">Save JSON</button></div></div><div class="li-chips" id="chips"></div></section>
    <div class="li-grid">
      <section class="li-card"><h2>Model</h2><div class="li-kv" id="modelInfo"></div></section>
      <section class="li-card"><h2>Network</h2><div class="li-kv" id="networkInfo"></div></section>
      <section class="li-card"><h2>Training</h2><div class="li-kv" id="trainingInfo"></div></section>
      <section class="li-card"><h2>File &amp; hashes</h2><div class="li-kv" id="fileInfo"></div></section>
    </div>
    <section class="li-section li-warnings" id="warningsSection" hidden><div class="li-section-head"><h2>Notes</h2></div><div class="li-section-body" id="warnings"></div></section>
    <section class="li-section" id="tagsSection"><div class="li-section-head"><h2>Training tags <span id="tagCount"></span></h2><input class="li-search" id="tagSearch" placeholder="Filter tags…"></div><div class="li-section-body"><div class="li-tag-grid" id="tags"></div></div></section>
    <section class="li-section"><div class="li-section-head"><h2>Tensor overview</h2></div><div class="li-section-body"><div class="li-table-wrap"><table class="li-table"><thead><tr><th>Group</th><th>Tensors</th><th>Parameters</th><th>Size</th></tr></thead><tbody id="tensorGroups"></tbody></table></div><div class="li-table-wrap" style="margin-top:12px"><table class="li-table"><thead><tr><th>Data type</th><th>Tensors</th><th>Parameters</th><th>Size</th></tr></thead><tbody id="tensorDtypes"></tbody></table></div></div></section>
    <section class="li-section"><div class="li-section-head"><h2>All metadata <span id="metadataCount"></span></h2><input class="li-search" id="metadataSearch" placeholder="Search keys or values…"></div><div class="li-section-body"><div class="li-table-wrap"><table class="li-table"><thead><tr><th>Key</th><th>Value</th><th></th></tr></thead><tbody id="metadataRows"></tbody></table></div></div></section>
  </div>
</div>

<div class="li-browse-overlay" id="browseOverlay"><div class="li-browse-dialog"><div class="li-browse-head"><h3>Choose a LoRA on the CyberHub computer</h3><button class="li-browse-close" id="browseClose">×</button></div><div class="li-locations" id="browseLocations"><span class="li-empty">Loading drives…</span></div><div class="li-crumb" id="browseCrumb"></div><div class="li-browse-body" id="browseBody"></div><div class="li-browse-foot"><span class="li-browse-current" id="browseCurrent"></span><button class="li-btn primary" id="browseSelect" disabled>Select</button></div></div></div>

<script>
(function(){
'use strict';
var $=function(id){return document.getElementById(id)}, report=null, browsePath='', selectedPath='';
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function apiError(response){return response.json().then(function(data){throw new Error(data.error||('HTTP '+response.status))}).catch(function(error){if(error instanceof Error)throw error;throw new Error('HTTP '+response.status)})}
function fmtBytes(value){var n=Number(value||0),u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return (i? n.toFixed(n>=100?0:n>=10?1:2):String(n))+' '+u[i]}
function fmtNumber(value){return value==null?'—':Number(value).toLocaleString()}
function fmtDuration(value){if(value==null)return null;var s=Math.round(Number(value)),h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return (h?h+'h ':'')+m+'m '+(s%60)+'s'}
function pretty(value){if(value==null||value==='')return '—';if(typeof value==='object')return JSON.stringify(value,null,2);return String(value)}
function kv(target,rows){$(target).innerHTML=rows.filter(function(row){return row[1]!=null&&row[1]!==''}).map(function(row){return '<div class="li-k">'+esc(row[0])+'</div><div class="li-v'+(row[2]?' mono':'')+'">'+esc(pretty(row[1]))+'</div>'}).join('')||'<div class="li-empty">Not stored in this file.</div>'}
function setBusy(message){$('statusText').textContent=message;$('status').classList.add('show');$('result').classList.remove('show')}
function fail(error){$('status').classList.remove('show');showToast(error.message||String(error))}
async function post(url,data){var response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!response.ok)return apiError(response);return response.json()}
function chip(label,value){return value==null||value===''?'':'<span class="li-chip">'+esc(label)+': '+esc(value)+'</span>'}
function renderRows(target,rows,key){$(target).innerHTML=(rows||[]).map(function(row){return '<tr><td>'+esc(row[key])+'</td><td>'+fmtNumber(row.tensors)+'</td><td>'+fmtNumber(row.parameters)+'</td><td>'+fmtBytes(row.bytes)+'</td></tr>'}).join('')||'<tr><td colspan="4" class="li-empty">No tensors found.</td></tr>'}
function renderTags(){var query=$('tagSearch').value.trim().toLowerCase(),all=report.tags||[],rows=all.filter(function(row){return !query||row.tag.toLowerCase().indexOf(query)!==-1}),max=rows.length?Math.max.apply(null,rows.map(function(row){return Number(row.count)||0})):1;$('tagCount').textContent='('+all.length+')';$('tags').innerHTML=rows.slice(0,300).map(function(row){var width=Math.max(2,Math.round((Number(row.count)||0)/max*100));return '<div class="li-tag"><span class="li-tag-bar" style="width:'+width+'%"></span><span class="li-tag-text"><span>'+esc(row.tag)+'</span><strong>'+esc(row.count)+'</strong></span></div>'}).join('')||'<div class="li-empty">No tag frequencies stored.</div>'}
function renderMetadata(){var query=$('metadataSearch').value.trim().toLowerCase(),entries=Object.entries(report.metadata||{}).filter(function(item){return !query||(item[0]+' '+pretty(item[1])).toLowerCase().indexOf(query)!==-1});$('metadataCount').textContent='('+report.metadata_count+')';$('metadataRows').innerHTML=entries.map(function(item,index){return '<tr><td><code>'+esc(item[0])+'</code></td><td><div class="li-meta-value">'+esc(pretty(item[1]))+'</div></td><td><button class="li-btn li-copy-one" data-index="'+index+'">Copy</button></td></tr>'}).join('')||'<tr><td colspan="3" class="li-empty">No metadata found.</td></tr>';$('metadataRows').querySelectorAll('.li-copy-one').forEach(function(button){button.onclick=function(){var item=entries[Number(button.dataset.index)];copyText(pretty(item[1]));showToast('Value copied')}})}
function render(data){report=data;var s=data.summary||{},f=data.file||{},h=data.hashes||{},t=data.tensors||{};$('status').classList.remove('show');$('result').classList.add('show');$('modelName').textContent=s.name||f.name;$('fileName').textContent=f.name+' · '+fmtBytes(f.size)+' · '+(f.source==='hub'?'CyberHub computer':'this computer');$('chips').innerHTML=chip('Base model',s.base_model)+chip('Network',s.network_module)+chip('Dimension',s.network_dim)+chip('Tensors',fmtNumber(t.count));kv('modelInfo',[['Name',s.name],['Author',s.author],['Description',s.description],['Base model',s.base_model],['Architecture',s.architecture],['Implementation',s.implementation]]);kv('networkInfo',[['Module',s.network_module],['Dimension / rank',s.network_dim],['Alpha',s.network_alpha],['Precision',s.precision],['Network arguments',s.network_args]]);kv('trainingInfo',[['Resolution',s.resolution],['Batch size',s.batch_size],['Gradient accumulation',s.gradient_accumulation],['Epoch',s.epoch!=null?(s.epoch+(s.epochs!=null?' / '+s.epochs:'')):s.epochs],['Steps',s.steps!=null?(s.steps+(s.max_steps!=null?' / '+s.max_steps:'')):s.max_steps],['Optimizer',s.optimizer],['Optimizer arguments',s.optimizer_args],['Scheduler',s.scheduler],['Learning rate',s.learning_rate],['UNet learning rate',s.unet_lr],['Text encoder learning rate',s.text_encoder_lr],['Clip skip',s.clip_skip],['Seed',s.seed],['Dataset images',s.dataset_images],['Started (UTC)',s.training_started],['Finished (UTC)',s.training_finished],['Duration',fmtDuration(s.training_duration_seconds)]]);kv('fileInfo',[['File',f.name],['Size',fmtBytes(f.size)],['Header',fmtBytes(f.header_bytes)],['Tensors',fmtNumber(t.count)],['Parameters',fmtNumber(t.parameters)],['Tensor size',fmtBytes(t.bytes)],['SHA-256',h.sha256,true],['AutoV2',h.autov2,true],['Stored model hash',h.stored_model,true],['Stored legacy hash',h.stored_legacy,true]]);var warnings=data.warnings||[];$('warningsSection').hidden=!warnings.length;$('warnings').innerHTML=warnings.map(function(w){return '<div class="li-warning">'+esc(w)+'</div>'}).join('');renderTags();renderRows('tensorGroups',t.by_group,'group');renderRows('tensorDtypes',t.by_dtype,'dtype');renderMetadata();window.scrollTo({top:$('result').offsetTop-70,behavior:'smooth'})}
async function inspectBrowserFile(file){if(!file)return;if(!file.name.toLowerCase().endsWith('.safetensors')){fail(new Error('Choose a .safetensors file'));return}try{setBusy('Reading the Safetensors header on this computer…');if(file.size<8)throw new Error('The file is too small to be a Safetensors file');var prefix=await file.slice(0,8).arrayBuffer(),view=new DataView(prefix),low=view.getUint32(0,true),high=view.getUint32(4,true),headerSize=high*4294967296+low;if(!Number.isSafeInteger(headerSize)||headerSize>67108864)throw new Error('Safetensors header exceeds the 64 MB safety limit');if(headerSize>file.size-8)throw new Error('Safetensors header extends beyond the end of the file');var header=await file.slice(8,8+headerSize).text();setBusy('Analyzing LoRA metadata…');render(await post('/api/lora-info/analyze-header',{filename:file.name,file_size:file.size,header:header}))}catch(error){fail(error)}}
async function inspectHubPath(){var path=$('hubPath').value.trim();if(!path){showToast('Choose a LoRA file first');return}try{setBusy($('hashFile').checked?'Inspecting LoRA and calculating hashes…':'Reading the LoRA header…');render(await post('/api/lora-info/analyze-path',{path:path,include_hash:$('hashFile').checked}))}catch(error){fail(error)}}
function setSource(source){var local=source==='local';$('localTab').classList.toggle('active',local);$('hubTab').classList.toggle('active',!local);$('localTab').setAttribute('aria-selected',local);$('hubTab').setAttribute('aria-selected',!local);$('localPane').hidden=!local;$('hubPane').hidden=local}
function copyText(text){if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text);var area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();document.execCommand('copy');area.remove();return Promise.resolve()}
function exportMetadata(){var blob=new Blob([JSON.stringify(report.metadata||{},null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=(report.file.name||'lora').replace(/\.safetensors$/i,'')+'-metadata.json';a.click();setTimeout(function(){URL.revokeObjectURL(url)},1000)}
async function browseLoad(path){try{var response=await fetch('/api/browse?path='+encodeURIComponent(path||'')+'&ext=.safetensors'),data=response.ok?await response.json():await apiError(response);browsePath=data.path||'';selectedPath='';$('browseCurrent').textContent=data.display||browsePath||'Root';$('browseSelect').disabled=true;var crumbs=['<span data-path="">Computer</span>'];(data.crumbs||[]).forEach(function(c){crumbs.push('<span>›</span><span data-path="'+esc(c.path)+'">'+esc(c.label)+'</span>')});$('browseCrumb').innerHTML=crumbs.join('');var html='';if(data.parent!==null&&data.parent!==undefined)html+='<div class="li-entry" data-path="'+esc(data.parent)+'">↩ ..</div>';(data.dirs||[]).forEach(function(d){html+='<div class="li-entry" data-path="'+esc(d.path)+'">📁 '+esc(d.name)+'</div>'});(data.files||[]).forEach(function(f){html+='<div class="li-entry" data-file="'+esc(f.path)+'">◇ '+esc(f.name)+'</div>'});$('browseBody').innerHTML=html||'<div class="li-entry li-empty">No LoRA files in this folder.</div>'}catch(error){$('browseBody').innerHTML='<div class="li-entry li-empty">'+esc(error.message)+'</div>'}}
async function loadLocations(){try{var response=await fetch('/api/lora-info/locations'),data=response.ok?await response.json():await apiError(response),locations=data.locations||[];$('browseLocations').innerHTML=locations.map(function(item){return '<button class="li-location" data-path="'+esc(item.path)+'"><span>'+(item.kind==='home'?'⌂':'▣')+'</span><span>'+esc(item.label)+'</span><span class="li-location-path">'+esc(item.path)+'</span></button>'}).join('')||'<span class="li-empty">No drives found.</span>'}catch(error){$('browseLocations').innerHTML='<span class="li-empty">'+esc(error.message)+'</span>'}}
function openBrowse(){$('browseOverlay').classList.add('open');loadLocations();browseLoad($('hubPath').value?$('hubPath').value.replace(/[\\\/][^\\\/]*$/,''):'')}
function closeBrowse(){$('browseOverlay').classList.remove('open')}
$('localTab').onclick=function(){setSource('local')};$('hubTab').onclick=function(){setSource('hub')};$('dropZone').onclick=function(){$('fileInput').click()};$('dropZone').onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();$('fileInput').click()}};$('fileInput').onchange=function(){inspectBrowserFile(this.files[0]);this.value=''};['dragenter','dragover'].forEach(function(name){$('dropZone').addEventListener(name,function(e){e.preventDefault();$('dropZone').classList.add('drag')})});['dragleave','drop'].forEach(function(name){$('dropZone').addEventListener(name,function(e){e.preventDefault();$('dropZone').classList.remove('drag')})});$('dropZone').addEventListener('drop',function(e){inspectBrowserFile(e.dataTransfer.files[0])});$('inspectPathBtn').onclick=inspectHubPath;$('browseBtn').onclick=openBrowse;$('browseClose').onclick=closeBrowse;$('browseOverlay').onclick=function(e){if(e.target===this)closeBrowse();var nav=e.target.closest('[data-path]');if(nav)browseLoad(nav.getAttribute('data-path'));var file=e.target.closest('[data-file]');if(file){$('browseBody').querySelectorAll('.selected').forEach(function(el){el.classList.remove('selected')});file.classList.add('selected');selectedPath=file.getAttribute('data-file');$('browseCurrent').textContent=selectedPath;$('browseSelect').disabled=false}};$('browseSelect').onclick=function(){if(selectedPath){$('hubPath').value=selectedPath;closeBrowse()}};$('copyJson').onclick=function(){copyText(JSON.stringify(report.metadata||{},null,2)).then(function(){showToast('Metadata copied')})};$('downloadJson').onclick=exportMetadata;$('tagSearch').oninput=renderTags;$('metadataSearch').oninput=renderMetadata;document.addEventListener('keydown',function(e){if(e.key==='Escape')closeBrowse()});
})();
</script>
"""
