"""PNG/JPEG/WebP metadata reading and SD parameter parsing."""

import json
import os
import re
import struct
import zlib
from io import BytesIO
from pathlib import Path

VERBOSE = False

def log_debug(msg):
    if VERBOSE: print(f"[DEBUG] {msg}")

def read_png_metadata(filepath, raw_data=None):
    """Read tEXt and iTXt chunks from a PNG file."""
    meta = {}
    try:
        source = BytesIO(raw_data) if raw_data is not None else open(filepath, "rb")
        with source as f:
            sig = f.read(8)
            if sig[:4] != b"\x89PNG":
                return meta
            while True:
                header = f.read(8)
                if len(header) < 8: break
                length = struct.unpack(">I", header[:4])[0]
                chunk_type = header[4:8]
                data = f.read(length)
                f.read(4)
                if chunk_type == b"tEXt":
                    sep = data.index(b"\x00")
                    meta[data[:sep].decode("latin-1")] = data[sep+1:].decode("latin-1", errors="replace")
                elif chunk_type == b"iTXt":
                    sep = data.index(b"\x00")
                    key = data[:sep].decode("utf-8")
                    rest = data[sep+1:]
                    cf = rest[0]; rest = rest[2:]
                    sep2 = rest.index(b"\x00"); rest = rest[sep2+1:]
                    sep3 = rest.index(b"\x00"); td = rest[sep3+1:]
                    if cf:
                        try:
                            dobj = zlib.decompressobj()
                            td = dobj.decompress(td, 10 * 1024 * 1024)  # 10 MB max
                        except Exception: pass
                    meta[key] = td.decode("utf-8", errors="replace")
                elif chunk_type == b"IDAT":
                    break
    except Exception as e:
        log_debug(f"PNG metadata read error for {filepath}: {e}")
    return meta

def read_jpeg_metadata(filepath, raw_data=None):
    """Read EXIF UserComment + Pillow info for JPEG/WebP metadata."""
    meta = {}
    try:
        from PIL import Image
    except ImportError:
        return meta
    try:
        img = Image.open(BytesIO(raw_data) if raw_data is not None else filepath)
        if hasattr(img, "text"): meta.update(img.text)
        if hasattr(img, "info"):
            for k, v in img.info.items():
                if isinstance(v, str) and len(v) > 10: meta[k] = v
            if "exif" in img.info and not meta.get("parameters"):
                try:
                    exif_bytes = img.info["exif"]
                    for enc, prefix in [("utf-8", b"ASCII\x00\x00\x00"), ("utf-8", b"UNICODE\x00")]:
                        idx = exif_bytes.find(prefix)
                        if idx >= 0:
                            comment = exif_bytes[idx+len(prefix):].split(b"\x00\x00")[0].decode(enc, errors="replace").strip()
                            if comment and len(comment) > 20:
                                meta["parameters"] = comment
                                break
                except Exception as e:
                    log_debug(f"EXIF deep read error: {e}")
        img.close()
    except Exception as e:
        log_debug(f"JPEG metadata read error for {filepath}: {e}")
    return meta

def read_companion_txt(filepath):
    """Read companion text sidecars.

    Supports plain `image.txt` plus Civitai Grabber-style
    `image_meta.txt`. `_no_meta.txt` is intentionally ignored because it
    only documents that generation metadata was unavailable.
    """
    base, ext = os.path.splitext(filepath)
    if not ext: return {}
    candidates = (base + ".txt", base + "_meta.txt")
    for txt_path in candidates:
        if txt_path == filepath or not os.path.isfile(txt_path):
            continue
        try:
            with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
            if content and len(content) > 10:
                return {"parameters": content}
        except Exception as e:
            log_debug(f"Companion txt read error for {txt_path}: {e}")
    return {}

def _ref_parts(value):
    if isinstance(value, list) and value:
        output_index = value[1] if len(value) > 1 and isinstance(value[1], int) else None
        return str(value[0]), output_index
    if isinstance(value, (str, int)):
        return str(value), None
    return "", None

def _ref_id(value):
    return _ref_parts(value)[0]

def _first_text_widget(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_text_widget(item)
            if found:
                return found
    return ""

def _extract_comfy_generated_texts(workflow_json):
    """Map generated text preview nodes back to the ComfyUI node that produced them."""
    if not workflow_json or not isinstance(workflow_json, str):
        return {}
    try:
        workflow = json.loads(workflow_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(workflow, dict):
        return {}

    links = workflow.get("links", [])
    link_origins = {}
    if isinstance(links, list):
        for link in links:
            if isinstance(link, list) and len(link) >= 2:
                link_origins[str(link[0])] = str(link[1])

    generated = {}
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    if not nodes and isinstance(workflow, dict):
        nodes = [v for v in workflow.values() if isinstance(v, dict)]
    if not isinstance(nodes, list):
        return generated
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or node.get("class_type") or "")
        title = ""
        if isinstance(node.get("_meta"), dict):
            title = str(node["_meta"].get("title") or "")
        title = " ".join([title, str(node.get("title") or "")])
        node_match = " ".join([node_type, title]).lower()
        if not any(part in node_match for part in (
            "generated_output", "generated output", "showtext", "show text",
            "previewtext", "preview text", "current positive text",
        )):
            continue
        text = _first_text_widget(node.get("widgets_values"))
        if not text or not _looks_like_prompt_text(text):
            continue
        node_id = node.get("id")
        if node_id is not None:
            generated[str(node_id)] = text
        inputs = node.get("inputs", [])
        if not isinstance(inputs, list):
            continue
        for input_info in inputs:
            if not isinstance(input_info, dict):
                continue
            origin = link_origins.get(str(input_info.get("link")))
            if origin:
                generated[origin] = text
    return generated

def _resolve_comfy_text(data, value, prefer=None, visited=None, generated_texts=None):
    """Resolve text through common ComfyUI conditioning/context chains."""
    visited = visited or set()
    node_id, output_index = _ref_parts(value)
    if not node_id or node_id in visited:
        return ""
    visited.add(node_id)
    node = data.get(node_id, {})
    inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
    if not isinstance(inputs, dict):
        return ""

    if (
        generated_texts
        and node_id in generated_texts
        and not (prefer == "negative" and output_index and output_index > 0)
    ):
        return generated_texts[node_id]

    # Direct CLIPTextEncode-style nodes.
    text = inputs.get("text")
    if isinstance(text, str) and text.strip() and not (prefer == "negative" and output_index and output_index > 0):
        return text
    if isinstance(text, list):
        found = _resolve_comfy_text(
            data, text, prefer=prefer, visited=visited, generated_texts=generated_texts
        )
        if found:
            return found

    # PrimitiveStringMultiline and prompt-builder nodes expose text under other names.
    for key in ("value", "input_prompt"):
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate.strip() and not (prefer == "negative" and output_index and output_index > 0):
            return candidate

    # rgthree any_switcher style nodes select any1..any10 via the num input.
    selected = None
    try:
        selected_num = int(inputs.get("num"))
        selected = inputs.get(f"any{selected_num}")
    except (TypeError, ValueError):
        selected = None
    if isinstance(selected, list):
        found = _resolve_comfy_text(
            data, selected, prefer=prefer, visited=visited, generated_texts=generated_texts
        )
        if found:
            return found
    for key in [f"any{i}" for i in range(1, 11)]:
        candidate = inputs.get(key)
        if isinstance(candidate, list):
            found = _resolve_comfy_text(
                data, candidate, prefer=prefer, visited=visited, generated_texts=generated_texts
            )
            if found:
                return found

    # Context/pipe nodes often hide conditioning behind positive/negative refs.
    keys = []
    if prefer:
        keys.append(prefer)
    keys += [
        "positive", "negative", "conditioning", "cond", "clip_text",
        "prompt", "text_positive", "text_negative",
    ]
    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate.strip() and key in ("prompt", "clip_text"):
            return candidate
        if isinstance(candidate, list):
            found = _resolve_comfy_text(
                data, candidate, prefer=prefer, visited=visited, generated_texts=generated_texts
            )
            if found:
                return found
    return ""

def _resolve_comfy_model(data, value, visited=None):
    """Follow model links back to the loader that names the actual model."""
    visited = visited or set()
    node_id, _ = _ref_parts(value)
    if not node_id:
        return ""
    if node_id not in data and node_id.lower().endswith((".safetensors", ".ckpt")):
        return node_id
    if node_id in visited:
        return ""
    visited.add(node_id)

    node = data.get(node_id, {})
    inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
    if not isinstance(inputs, dict):
        return ""

    for key in ("ckpt_name", "unet_name", "model_name"):
        candidate = inputs.get(key)
        if isinstance(candidate, str) and candidate and candidate != "None":
            return candidate

    for key in ("model", "base_model", "unet", "ckpt"):
        candidate = inputs.get(key)
        if isinstance(candidate, list):
            found = _resolve_comfy_model(data, candidate, visited=visited)
            if found:
                return found
    return ""

def _looks_like_prompt_text(text):
    if not isinstance(text, str):
        return False
    text = text.strip()
    if len(text) < 12:
        return False
    if text[0] in "{[":
        return False
    lower = text.lower()
    if lower.startswith(("http://", "https://")):
        return False
    if lower.endswith((".safetensors", ".ckpt", ".pt", ".pth")):
        return False
    if "class_type" in lower or "widgets_values" in lower:
        return False
    if lower.startswith(("create improved", "return only", "no explanations", "follow this internal order")):
        return False
    if any(phrase in lower for phrase in (
        "you are an expert prompt engineer",
        "your task is to expand the user's prompt",
        "think step by step before writing",
        "then output a single expanded prompt",
        "follow these rules strictly",
        "user's input:",
    )):
        return False
    return True

def _is_negative_comfy_node(node, key=""):
    node_text = " ".join(
        str(v or "") for v in (
            node.get("class_type"),
            node.get("title"),
            node.get("_meta", {}).get("title") if isinstance(node.get("_meta"), dict) else "",
            key,
        )
    ).lower()
    return "negative" in node_text

def _score_comfy_text_candidate(node, key, text, generated=False):
    if not _looks_like_prompt_text(text) or _is_negative_comfy_node(node, key):
        return -1
    class_type = str(node.get("class_type") or node.get("type") or "").lower()
    title = str(node.get("title") or "").lower()
    if isinstance(node.get("_meta"), dict):
        title += " " + str(node["_meta"].get("title") or "").lower()
    score = min(len(text), 1200)
    if generated:
        score += 900
    if "positive" in title or key in ("prompt", "text", "value", "input_prompt"):
        score += 250
    if any(part in class_type for part in ("cliptextencode", "primitive", "string", "prompt", "engineer")):
        score += 150
    if "system" in key or "api" in key or key == "model":
        score -= 1000
    return score

def _best_comfy_text_candidate(data, generated_texts=None):
    best_score = -1
    best_text = ""
    generated_texts = generated_texts or {}
    for node_id, text in generated_texts.items():
        node = data.get(str(node_id), {})
        score = _score_comfy_text_candidate(node if isinstance(node, dict) else {}, "generated", text, generated=True)
        if score > best_score:
            best_score = score
            best_text = text.strip()
    for node in data.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict):
            for key in ("text", "prompt", "clip_text", "value", "input_prompt"):
                candidate = inputs.get(key)
                if not isinstance(candidate, str):
                    continue
                score = _score_comfy_text_candidate(node, key, candidate)
                if score > best_score:
                    best_score = score
                    best_text = candidate.strip()
        widget_text = _first_text_widget(node.get("widgets_values"))
        if widget_text:
            score = _score_comfy_text_candidate(node, "widgets_values", widget_text)
            if score > best_score:
                best_score = score
                best_text = widget_text.strip()
    return best_text

def _is_comfy_sampler_node(class_type, inputs):
    if "KSampler" in class_type:
        return True
    if "Sampler" not in class_type:
        return False
    return (
        isinstance(inputs, dict)
        and ("positive" in inputs or "negative" in inputs)
        and any(k in inputs for k in ("steps", "steps_to_run", "cfg", "sampler_name", "seed"))
    )

def extract_comfyui_prompt(meta):
    """Extract prompt/model/sampler from ComfyUI workflow JSON."""
    prompt_json = meta.get("prompt", "")
    if not prompt_json or not isinstance(prompt_json, str): return meta
    try:
        data = json.loads(prompt_json)
        if not isinstance(data, dict): return meta
        generated_texts = _extract_comfy_generated_texts(meta.get("workflow", ""))
        generated_texts.update(_extract_comfy_generated_texts(prompt_json))
        prompt_text = neg_text = model_name = sampler = steps = cfg = ""
        for node_id, node in data.items():
            ct = node.get("class_type", "")
            inputs = node.get("inputs", {})
            if _is_comfy_sampler_node(ct, inputs):
                candidate_sampler = inputs.get("sampler_name") or ct
                candidate_steps = str(inputs.get("steps", inputs.get("steps_to_run", steps)))
                candidate_cfg = str(inputs.get("cfg", cfg))
                pos_ref = inputs.get("positive")
                pt = ""
                if isinstance(pos_ref, list):
                    pt = _resolve_comfy_text(
                        data, pos_ref, prefer="positive", generated_texts=generated_texts
                    )
                neg_ref = inputs.get("negative")
                nt = ""
                if isinstance(neg_ref, list):
                    nt = _resolve_comfy_text(
                        data, neg_ref, prefer="negative", generated_texts=generated_texts
                    )
                if pt:
                    prompt_text = pt
                    if nt:
                        neg_text = nt
                    sampler = candidate_sampler
                    steps = candidate_steps
                    cfg = candidate_cfg
                    model_from_sampler = _resolve_comfy_model(data, inputs.get("model"))
                    if model_from_sampler:
                        model_name = model_from_sampler
                elif not prompt_text:
                    sampler = candidate_sampler
                    steps = candidate_steps
                    cfg = candidate_cfg
            if not model_name and ct in ("CheckpointLoaderSimple", "CheckpointLoader", "UNETLoader"):
                model_name = inputs.get(
                    "ckpt_name",
                    inputs.get("unet_name", inputs.get("model_name", model_name))
                )
        if not prompt_text:
            prompt_text = _best_comfy_text_candidate(data, generated_texts=generated_texts)
        if prompt_text:
            parts = [prompt_text]
            if neg_text: parts.append(f"Negative prompt: {neg_text}")
            settings = []
            if steps: settings.append(f"Steps: {steps}")
            if sampler: settings.append(f"Sampler: {sampler}")
            if cfg: settings.append(f"CFG scale: {cfg}")
            if model_name: settings.append(f"Model: {model_name}")
            settings.append("Source: ComfyUI")
            if settings and not settings[0].startswith("Steps:"):
                settings.insert(0, "Steps: ")
            parts.append(", ".join(settings))
            meta["parameters"] = "\n".join(parts)
            log_debug(f"ComfyUI prompt extracted: {prompt_text[:80]}...")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_debug(f"ComfyUI extraction failed: {e}")
    return meta

# ─── Extra generator formats (ported from RupertAvery/DiffusionToolkit) ──────────
#
# DiffusionToolkit's Metadata.cs recognises many tools beyond A1111/ComfyUI. We
# split them two ways:
#   * Formats whose data is fully inside one "parameters" JSON string (SwarmUI,
#     RuinedFooocus, Fooocus-in-PNG) are parsed in parse_sd_parameters(), so the
#     original JSON stays visible in the Raw Metadata tab.
#   * Multi-chunk formats (InvokeAI ×3, NovelAI, FooocusMRE, EasyDiffusion) write
#     several PNG tEXt chunks; _normalize_known_formats() reads those chunks and
#     synthesises a clean A1111 "parameters" string. The original chunks remain in
#     the meta dict (Raw Metadata tab) untouched.

def _a1111_params(prompt, negative, *, steps=None, sampler=None, cfg=None, seed=None,
                  width=None, height=None, model=None, model_hash=None, source=None):
    """Build an A1111-style 'parameters' string from normalized fields. The settings
    line is anchored with 'Steps:' so parse_sd_parameters() detects it."""
    out = [str(prompt or "").strip()]
    neg = str(negative or "").strip()
    if neg:
        out.append("Negative prompt: " + neg)
    seg = []

    def add(k, v):
        if v not in (None, "", "None"):
            seg.append(f"{k}: {v}")

    add("Steps", steps)
    add("Sampler", sampler)
    add("CFG scale", cfg)
    add("Seed", seed)
    if width and height:
        add("Size", f"{width}x{height}")
    add("Model", model)
    add("Model hash", model_hash)
    if source:
        add("Source", source)
    if seg and not seg[0].startswith("Steps:"):
        seg.insert(0, "Steps: ")  # anchor so the settings line is recognised
    out.append(", ".join(seg))
    return "\n".join(out)


def _jget(d, *keys, default=None):
    """First present, non-empty value among keys in dict d."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _png_ihdr_size(filepath, raw_data=None):
    """Read (width, height) from a PNG's IHDR chunk. Used by NovelAI, whose params
    chunk omits dimensions."""
    try:
        source = BytesIO(raw_data) if raw_data is not None else open(filepath, "rb")
        with source as f:
            if f.read(8)[:4] != b"\x89PNG":
                return (None, None)
            f.read(8)  # IHDR length + type
            ihdr = f.read(8)
            w = struct.unpack(">I", ihdr[:4])[0]
            h = struct.unpack(">I", ihdr[4:8])[0]
            return (w, h)
    except Exception:
        return (None, None)


# ---- string-contained JSON formats (parsed in parse_sd_parameters) -------------

def _parse_swarmui(raw, obj):
    p = obj.get("sui_image_params", {})
    s = {}
    if _jget(p, "steps") is not None: s["Steps"] = str(_jget(p, "steps"))
    if _jget(p, "sampler") is not None: s["Sampler"] = str(_jget(p, "sampler"))
    if _jget(p, "cfgscale") is not None: s["CFG scale"] = str(_jget(p, "cfgscale"))
    if _jget(p, "seed") is not None: s["Seed"] = str(_jget(p, "seed"))
    if p.get("width") and p.get("height"): s["Size"] = f"{p['width']}x{p['height']}"
    if _jget(p, "model") is not None: s["Model"] = str(_jget(p, "model"))
    s["Source"] = "SwarmUI"
    return {"raw": raw, "prompt": str(p.get("prompt", "")).strip(),
            "negative_prompt": str(p.get("negativeprompt", "")).strip(), "settings": s}


def _parse_ruined_fooocus(raw, obj):
    s = {}
    if _jget(obj, "steps") is not None: s["Steps"] = str(_jget(obj, "steps"))
    if _jget(obj, "sampler_name") is not None: s["Sampler"] = str(_jget(obj, "sampler_name"))
    if _jget(obj, "cfg") is not None: s["CFG scale"] = str(_jget(obj, "cfg"))
    if _jget(obj, "seed") is not None: s["Seed"] = str(_jget(obj, "seed"))
    if obj.get("width") and obj.get("height"): s["Size"] = f"{obj['width']}x{obj['height']}"
    if _jget(obj, "base_model_name") is not None: s["Model"] = str(_jget(obj, "base_model_name"))
    if _jget(obj, "base_model_hash") is not None: s["Model hash"] = str(_jget(obj, "base_model_hash"))
    s["Source"] = "RuinedFooocus"
    return {"raw": raw, "prompt": str(obj.get("Prompt", "")).strip(),
            "negative_prompt": str(obj.get("Negative", "")).strip(), "settings": s}


def _parse_fooocus_json(raw, obj):
    def joinarr(v):
        if isinstance(v, list): return ", ".join(str(x) for x in v)
        return str(v or "")
    s = {}
    if _jget(obj, "steps") is not None: s["Steps"] = str(_jget(obj, "steps"))
    if _jget(obj, "sampler") is not None: s["Sampler"] = str(_jget(obj, "sampler"))
    if _jget(obj, "guidance_scale") is not None: s["CFG scale"] = str(_jget(obj, "guidance_scale"))
    if _jget(obj, "seed") is not None: s["Seed"] = str(_jget(obj, "seed"))
    res = obj.get("resolution")
    if isinstance(res, str):
        nums = re.findall(r"\d+", res)
        if len(nums) >= 2: s["Size"] = f"{nums[0]}x{nums[1]}"
    if _jget(obj, "base_model") is not None: s["Model"] = str(_jget(obj, "base_model"))
    if _jget(obj, "base_model_hash") is not None: s["Model hash"] = str(_jget(obj, "base_model_hash"))
    s["Source"] = "Fooocus"
    return {"raw": raw, "prompt": joinarr(obj.get("full_prompt") or obj.get("prompt")).strip(),
            "negative_prompt": joinarr(obj.get("full_negative_prompt") or obj.get("negative_prompt")).strip(),
            "settings": s}


# ---- multi-chunk formats (synthesised into a 'parameters' string) --------------

def _norm_invokeai2(meta):
    """Current InvokeAI: invokeai_metadata flat JSON. Note: 'scheduler' not 'sampler'."""
    obj = json.loads(meta["invokeai_metadata"])
    model = obj.get("model")
    if isinstance(model, dict):
        model = model.get("model_name") or model.get("name")
    return _a1111_params(
        obj.get("positive_prompt"), obj.get("negative_prompt"),
        steps=obj.get("steps"), sampler=obj.get("scheduler"),
        cfg=obj.get("cfg_scale"), seed=obj.get("seed"),
        width=obj.get("width"), height=obj.get("height"),
        model=model, source="InvokeAI")


def _norm_invokeai_sdmeta(meta):
    """InvokeAI 'sd-metadata' JSON: params nested under 'image'."""
    obj = json.loads(meta["sd-metadata"])
    img = obj.get("image", {})
    prompt = img.get("prompt")
    if isinstance(prompt, list) and prompt:
        prompt = prompt[0].get("prompt", "") if isinstance(prompt[0], dict) else str(prompt[0])
    return _a1111_params(
        prompt, None,
        steps=img.get("steps"), sampler=img.get("sampler"),
        cfg=img.get("cfg_scale"), seed=img.get("seed"),
        width=img.get("width"), height=img.get("height"),
        model_hash=obj.get("model_hash"), source="InvokeAI")


def _norm_invokeai_dream(meta):
    """Legacy InvokeAI 'Dream' CLI string: prompt in quotes + -s/-S/-W/-H/-C/-A flags."""
    s = meta["Dream"]
    pm = re.search(r'"([^"]*)"', s)
    prompt = pm.group(1) if pm else s

    def flag(f):
        m = re.search(r"%s\s+(\S+)" % re.escape(f), s)
        return m.group(1) if m else None

    return _a1111_params(
        prompt, None,
        steps=flag("-s"), sampler=flag("-A"), cfg=flag("-C"), seed=flag("-S"),
        width=flag("-W"), height=flag("-H"), source="InvokeAI")


def _norm_novelai(meta, filepath, raw_data=None):
    """NovelAI: Description chunk = prompt, Source chunk = model hash, Comment = JSON."""
    prompt = meta.get("Description", "")
    model_hash = None
    src = meta.get("Source", "")
    hm = re.search(r"[0-9A-Fa-f]{8}", src or "")
    if hm:
        model_hash = hm.group(0)
    comment = {}
    try:
        comment = json.loads(meta.get("Comment", "") or "{}")
    except Exception:
        comment = {}
    w, h = _png_ihdr_size(filepath, raw_data=raw_data)
    return _a1111_params(
        prompt, comment.get("uc"),
        steps=comment.get("steps"), sampler=comment.get("sampler"),
        cfg=comment.get("scale"), seed=comment.get("seed"),
        width=w, height=h, model_hash=model_hash, source="NovelAI")


def _norm_fooocus_mre(meta):
    """FooocusMRE: a 'Comment' JSON chunk (no NovelAI Software tag)."""
    obj = json.loads(meta["Comment"])

    def withreal(base, real):
        parts = [str(obj.get(base, "") or "")]
        rv = obj.get(real)
        if isinstance(rv, list) and rv:
            parts.append(", ".join(str(x) for x in rv))
        return ", ".join(p for p in parts if p).strip(", ")

    return _a1111_params(
        withreal("prompt", "real_prompt"), withreal("negative_prompt", "real_negative_prompt"),
        steps=obj.get("steps"), sampler=obj.get("sampler"),
        cfg=obj.get("cfg"), seed=obj.get("seed"),
        width=obj.get("width"), height=obj.get("height"),
        model=obj.get("base_model"), source="Fooocus MRE")


def _norm_easydiffusion(meta):
    """EasyDiffusion writes each parameter as its own PNG tEXt chunk."""
    return _a1111_params(
        meta.get("prompt"), meta.get("negative_prompt"),
        steps=meta.get("num_inference_steps"), sampler=meta.get("sampler_name"),
        cfg=meta.get("guidance_scale"), seed=meta.get("seed"),
        width=meta.get("width"), height=meta.get("height"),
        model=meta.get("use_stable_diffusion_model"), source="EasyDiffusion")


def _normalize_known_formats(meta, filepath, raw_data=None):
    """Detect a multi-chunk generator format and return a synthesised A1111
    'parameters' string, or None. Mirrors DiffusionToolkit's dispatch order."""
    try:
        if isinstance(meta.get("invokeai_metadata"), str):
            return _norm_invokeai2(meta)
        if isinstance(meta.get("sd-metadata"), str):
            return _norm_invokeai_sdmeta(meta)
        if isinstance(meta.get("Dream"), str):
            return _norm_invokeai_dream(meta)
        if meta.get("Software") == "NovelAI":
            return _norm_novelai(meta, filepath, raw_data=raw_data)
        # Comment JSON without the NovelAI Software tag → FooocusMRE
        if isinstance(meta.get("Comment"), str) and meta.get("Software") != "NovelAI":
            c = meta["Comment"].strip()
            if c.startswith("{"):
                return _norm_fooocus_mre(meta)
        # EasyDiffusion: plain-text 'prompt' chunk (ComfyUI's is JSON, starts with '{')
        p = meta.get("prompt")
        if isinstance(p, str) and p and not p.strip().startswith("{") and "negative_prompt" in meta:
            return _norm_easydiffusion(meta)
    except Exception as e:
        log_debug(f"Known-format normalize failed: {e}")
    return None


def get_image_metadata(filepath, raw_data=None):
    """Read AI generation metadata. Tries embedded data first, then companion .txt."""
    ext = Path(filepath).suffix.lower()
    if ext == ".png":
        meta = read_png_metadata(filepath, raw_data=raw_data)
    elif ext in (".jpg", ".jpeg", ".webp"):
        # Civitai downloads usually keep generation data in a sidecar
        # `<image>_meta.txt`. Checking it first avoids opening every JPEG/WebP
        # with Pillow during large gallery index passes.
        meta = read_companion_txt(filepath)
        if not meta:
            meta = read_jpeg_metadata(filepath, raw_data=raw_data)
    else:
        meta = {}
    if meta.get("prompt") and "parameters" not in meta:
        meta = extract_comfyui_prompt(meta)
    # Detect multi-chunk generator formats (InvokeAI, NovelAI, FooocusMRE,
    # EasyDiffusion) and synthesise a clean 'parameters' string. SwarmUI /
    # RuinedFooocus / Fooocus-JSON keep their JSON in 'parameters' and are handled
    # at parse time, so we only run this when there's no usable parameters string.
    params_val = meta.get("parameters")
    has_a1111 = isinstance(params_val, str) and not params_val.strip().startswith("{")
    if not has_a1111:
        norm = _normalize_known_formats(meta, filepath, raw_data=raw_data)
        if norm:
            meta["parameters"] = norm
            log_debug(f"Normalized known format for {filepath}")
    if ext == ".png" and (not meta or (not meta.get("parameters") and not meta.get("prompt"))):
        txt_meta = read_companion_txt(filepath)
        if txt_meta:
            meta.update(txt_meta)
            log_debug(f"Metadata from companion .txt: {filepath}")
    return meta

def _split_a1111_settings(s):
    """Parse an A1111 settings line into a dict.

    Format: `Key: value, Key2: "value with, internal commas", Key3: value3`
    The old regex `([^,]+?)(?:,|$)` split on every comma and broke fields like
    `Lora hashes: "a: 1, b: 2"` into a truncated value plus a bogus 'b' key.
    This hand-written tokenizer reads each value either until the next unquoted
    comma, or — when the value starts with `"` — until the matching closing quote.
    """
    out = {}
    i, n = 0, len(s)
    while i < n:
        # Skip separators (commas, whitespace, newlines)
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        # Read key up to ':'
        key_start = i
        while i < n and s[i] != ":":
            i += 1
        if i >= n:
            break  # malformed tail, ignore
        key = s[key_start:i].strip()
        i += 1  # skip ':'
        # Skip whitespace between ':' and value
        while i < n and s[i] in " \t":
            i += 1
        # Read value — quote-aware
        if i < n and s[i] == '"':
            i += 1  # skip opening quote
            val_start = i
            while i < n and s[i] != '"':
                i += 1
            value = s[val_start:i]
            if i < n:
                i += 1  # skip closing quote
        else:
            val_start = i
            while i < n and s[i] != ",":
                i += 1
            value = s[val_start:i].rstrip()
        if key:
            out[key] = value
    return out

# Keys Civitai emits in its key/value metadata blob (the `_meta.txt` sidecar the
# grabber writes, or embedded). camelCase keys are the giveaway — A1111 never
# uses them. Listed longest-first matters for the regex (so "Model hash" wins
# over "Model"); the parser sorts by length so order here is just for reference.
_CIVITAI_KNOWN_KEYS = (
    "prompt", "negativePrompt", "sampler", "cfgScale", "steps", "seed",
    "Size", "width", "height", "clipSkip", "quantity", "workflow",
    "baseModel", "Model hash", "Model", "VAE hash", "VAE", "resources",
    "civitaiResources", "Created Date", "nsfw", "draft",
    "Denoising strength", "Hires upscaler", "Schedule type", "Version",
    "Clip skip", "CFG scale", "Seed", "Sampler", "Steps",
)


def _looks_like_civitai(raw):
    """True when `raw` is Civitai's key/value format rather than A1111's."""
    if re.search(r"(?m)^\s*(negativePrompt|cfgScale|civitaiResources|baseModel|clipSkip)\s*:", raw):
        return True
    # Lowercase `prompt:` + `sampler:`/`steps:`/`seed:` with no A1111 `Steps:` line.
    if (re.search(r"(?m)^\s*prompt\s*:", raw)
            and re.search(r"(?m)^\s*(sampler|steps|seed)\s*:", raw)
            and not re.search(r"(?m)^\s*Steps\s*:", raw)):
        return True
    return False


def _parse_civitai_meta(raw):
    """Parse Civitai's key/value metadata into prompt / negative_prompt / settings.

    Multi-line values (prompt, negativePrompt — they contain BREAK blocks and span
    many lines) accumulate until the next recognised key line.
    """
    keys_sorted = sorted(_CIVITAI_KNOWN_KEYS, key=len, reverse=True)
    key_re = re.compile(r"^(" + "|".join(re.escape(k) for k in keys_sorted) + r")\s*:\s?(.*)$")
    fields = {}
    cur_key = None
    cur_val = []

    def flush():
        if cur_key is not None and cur_key not in fields:
            fields[cur_key] = "\n".join(cur_val).rstrip()

    for line in raw.split("\n"):
        m = key_re.match(line)
        if m:
            flush()
            cur_key = m.group(1)
            cur_val = [m.group(2)]
        elif cur_key is not None:
            cur_val.append(line)
    flush()

    result = {"raw": raw}
    result["prompt"] = (fields.get("prompt") or "").strip()
    if fields.get("negativePrompt") is not None:
        result["negative_prompt"] = fields["negativePrompt"].strip()

    settings = {}

    def setif(disp, *keys):
        if disp in settings:
            return
        for k in keys:
            v = fields.get(k)
            if v is not None and str(v).strip():
                settings[disp] = str(v).strip()
                return

    setif("Steps", "steps", "Steps")
    setif("Sampler", "sampler", "Sampler")
    setif("CFG scale", "cfgScale", "CFG scale")
    setif("Seed", "seed", "Seed")
    if fields.get("Size", "").strip():
        settings["Size"] = fields["Size"].strip()
    elif fields.get("width", "").strip() and fields.get("height", "").strip():
        settings["Size"] = f"{fields['width'].strip()}x{fields['height'].strip()}"
    setif("Schedule type", "Schedule type")
    setif("Clip skip", "clipSkip", "Clip skip")
    setif("Denoising strength", "Denoising strength")
    setif("Hires upscaler", "Hires upscaler")

    # Model: prefer an explicit `Model:`; otherwise pull the checkpoint name out of
    # civitaiResources. LoRAs from that list are surfaced too.
    model = (fields.get("Model") or "").strip()
    loras = []
    cres = fields.get("civitaiResources")
    if cres:
        parsed_list = None
        try:
            parsed_list = json.loads(cres)
        except Exception:
            try:
                import ast
                parsed_list = ast.literal_eval(cres)
            except Exception:
                parsed_list = None
        if isinstance(parsed_list, list):
            for item in parsed_list:
                if not isinstance(item, dict):
                    continue
                t = (item.get("type") or "").lower()
                name = item.get("modelVersionName") or item.get("modelName") or ""
                if t == "checkpoint" and not model and name:
                    model = name
                elif t == "lora" and name:
                    w = item.get("weight")
                    loras.append(name + (f":{w}" if w not in (None, "", 1) else ""))
    if model:
        settings["Model"] = model
    setif("Model hash", "Model hash")
    setif("VAE", "VAE")
    setif("VAE hash", "VAE hash")
    if fields.get("baseModel", "").strip():
        settings["Base model"] = fields["baseModel"].strip()
    if loras:
        settings["LoRAs"] = ", ".join(loras)
    setif("Version", "Version")

    result["settings"] = settings
    return result


def parse_sd_parameters(raw):
    """Parse A1111-style 'parameters' string into structured fields."""
    result = {"raw": raw}
    if not raw: return result
    # Self-contained JSON formats (the whole blob lives in the parameters string):
    # SwarmUI, RuinedFooocus, Fooocus. Keeping them here means the raw JSON stays
    # visible in the Raw Metadata tab and is re-parsed on view.
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            if "sui_image_params" in obj:
                return _parse_swarmui(raw, obj)
            if obj.get("software") == "RuinedFooocus":
                return _parse_ruined_fooocus(raw, obj)
            if "full_prompt" in obj or "full_negative_prompt" in obj:
                return _parse_fooocus_json(raw, obj)
    if _looks_like_civitai(raw):
        return _parse_civitai_meta(raw)
    lines = raw.strip().split("\n")
    neg_idx = settings_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("Negative prompt:"): neg_idx = i
        if line.startswith("Steps:"): settings_idx = i
    if neg_idx > 0: result["prompt"] = "\n".join(lines[:neg_idx]).strip()
    elif settings_idx > 0: result["prompt"] = "\n".join(lines[:settings_idx]).strip()
    else: result["prompt"] = raw.strip()
    if neg_idx >= 0:
        ne = settings_idx if settings_idx > neg_idx else len(lines)
        result["negative_prompt"] = "\n".join(lines[neg_idx:ne]).replace("Negative prompt:","",1).strip()
    if settings_idx >= 0:
        sl = "\n".join(lines[settings_idx:])
        result["settings"] = _split_a1111_settings(sl)
    return result

def merge_png_meta(source_bytes, target_bytes):
    """Merge tEXt/iTXt/zTXt chunks from source PNG into target PNG pixels."""
    import io
    TEXT_CHUNK_TAGS = (b"tEXt", b"iTXt", b"zTXt")
    def read_chunks(data):
        f = io.BytesIO(data)
        sig = f.read(8); chunks = []
        while True:
            header = f.read(8)
            if len(header) < 8: break
            length = struct.unpack(">I", header[:4])[0]
            tag = header[4:8]; payload = f.read(length); f.read(4)
            chunks.append((tag, payload))
            if tag == b"IEND": break
        return sig, chunks
    sig_src, chunks_src = read_chunks(source_bytes)
    sig_tgt, chunks_tgt = read_chunks(target_bytes)
    if sig_src[:4] != b"\x89PNG": raise ValueError("Source is not a valid PNG file")
    if sig_tgt[:4] != b"\x89PNG": raise ValueError("Target is not a valid PNG file")
    text_chunks = [c for c in chunks_src if c[0] in TEXT_CHUNK_TAGS]
    out = []; inserted = False
    for tag, payload in chunks_tgt:
        if tag in TEXT_CHUNK_TAGS: continue
        out.append((tag, payload))
        if tag == b"IHDR" and not inserted: out.extend(text_chunks); inserted = True
    result = io.BytesIO(); result.write(sig_tgt)
    for tag, payload in out:
        result.write(struct.pack(">I", len(payload))); result.write(tag)
        result.write(payload); result.write(struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    return result.getvalue()
