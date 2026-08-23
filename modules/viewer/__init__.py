"""Meta Viewer module — drop an image to inspect its generation metadata."""

import json
import os
import struct
import tempfile
import zlib

from core import Module
from core.metadata import get_image_metadata, parse_sd_parameters
from core.server import build_shell


TEXT_CHUNK_TAGS = (b"tEXt", b"iTXt", b"zTXt")


def _png_chunk(tag, payload):
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def _png_text_chunk(key, value):
    key = str(key or "").strip()
    if not key or "\x00" in key:
        raise ValueError("Metadata keys must be non-empty and cannot contain null bytes")
    value = "" if value is None else str(value)
    try:
        key_bytes = key.encode("latin-1")
        value_bytes = value.encode("latin-1")
        return b"tEXt", key_bytes + b"\x00" + value_bytes
    except UnicodeEncodeError:
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        # iTXt layout: key\0 compression_flag compression_method language\0 translated_key\0 text
        return b"iTXt", key_bytes + b"\x00\x00\x00\x00\x00" + value_bytes


def rewrite_png_metadata(png_bytes, metadata):
    """Replace PNG text metadata chunks while preserving image pixels/chunks."""
    if not isinstance(metadata, dict):
        raise ValueError("Raw metadata must be a JSON object")
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Metadata editing currently supports PNG files only")
    chunks = []
    pos = 8
    while pos + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[pos:pos + 4])[0]
        tag = png_bytes[pos + 4:pos + 8]
        payload_start = pos + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(png_bytes):
            raise ValueError("Invalid PNG chunk structure")
        chunks.append((tag, png_bytes[payload_start:payload_end]))
        pos = crc_end
        if tag == b"IEND":
            break
    text_chunks = []
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text_chunks.append(_png_text_chunk(key, value))
    out = bytearray(png_bytes[:8])
    inserted = False
    for tag, payload in chunks:
        if tag in TEXT_CHUNK_TAGS:
            continue
        out += _png_chunk(tag, payload)
        if tag == b"IHDR" and not inserted:
            for text_tag, text_payload in text_chunks:
                out += _png_chunk(text_tag, text_payload)
            inserted = True
    return bytes(out)


class ViewerModule(Module):
    name = "Viewer"
    version = "1.1"
    icon = "\U0001F50D"   # 🔍
    description = "Drop an image to view its generation metadata."
    order = 20

    settings_schema = {
        # No persistent settings yet — but the module can still be
        # toggled on/off from the Settings page.
    }

    def routes_get(self):
        return {"/viewer": self._page}

    def routes_post(self):
        return {
            "/api/viewer/analyze": self._analyze,
            "/api/viewer/rewrite": self._rewrite,
        }

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="viewer", page_title="Meta Viewer",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    def _analyze(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
            file_item = files.get("file")
            if not file_item or not file_item.get("data"):
                handler.respond_json({"error": "No file uploaded"}, status=400)
                return
            # Use original extension for correct metadata reading
            filename = file_item.get("filename", "")
            suffix = os.path.splitext(filename)[1].lower() if filename else ".png"
            if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                suffix = ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(file_item["data"])
                tmp_path = tmp.name
            try:
                meta = get_image_metadata(tmp_path)
                parsed = {}
                if "parameters" in meta:
                    parsed = parse_sd_parameters(meta["parameters"])
                elif "prompt" in meta:
                    prompt = meta.get("prompt", "")
                    if isinstance(prompt, str) and prompt.lstrip().startswith(("{", "[")):
                        parsed["workflow"] = prompt[:2000] + "..." if len(prompt) > 2000 else prompt
                    else:
                        parsed["prompt"] = prompt
                w, h = 0, 0
                try:
                    from PIL import Image
                    with Image.open(tmp_path) as img:
                        w, h = img.size
                except Exception:
                    pass
                civitai = self.hub.civitai.lookup(parsed)
                handler.respond_json({
                    "parsed": parsed, "raw_meta": meta,
                    "info": {"width": w, "height": h},
                    "civitai": civitai,
                })
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500)

    def _rewrite(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
            file_item = files.get("file")
            meta_item = files.get("raw_meta_json")
            if not file_item or not file_item.get("data"):
                handler.respond_json({"error": "No file uploaded"}, status=400)
                return
            if not meta_item or not meta_item.get("data"):
                handler.respond_json({"error": "No metadata payload provided"}, status=400)
                return
            filename = file_item.get("filename", "") or "image.png"
            if os.path.splitext(filename)[1].lower() != ".png":
                handler.respond_json({"error": "Metadata editing currently supports PNG files only"}, status=400)
                return
            try:
                raw_meta = json.loads(meta_item["data"].decode("utf-8"))
            except Exception as exc:
                handler.respond_json({"error": f"Invalid metadata JSON: {exc}"}, status=400)
                return
            edited = rewrite_png_metadata(file_item["data"], raw_meta)
            stem = os.path.splitext(os.path.basename(filename))[0] or "image"
            handler.respond_binary(edited, "image/png", download_name=f"{stem}_metadata.png")
        except ValueError as e:
            handler.respond_json({"error": str(e)}, status=400)
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500)


PAGE_BODY = r"""
<style>
.viewer-content { max-width:1180px; margin:0 auto; padding:18px 22px; }
.drop-zone {
    border:1px dashed var(--border-light); border-radius:8px; padding:14px 16px;
    color:var(--text-dim); transition:all .2s; cursor:pointer;
    background:var(--bg-panel); margin-bottom:14px; display:flex; align-items:center; gap:12px;
}
.drop-zone:hover, .drop-zone.dragover { border-color:var(--accent); background:var(--bg-active); color:var(--text); }
.drop-zone .big { width:34px; height:34px; border-radius:8px; background:var(--bg-card); border:1px solid var(--border); display:flex; align-items:center; justify-content:center; color:var(--accent); flex-shrink:0; }
.drop-zone .big svg { width:18px; height:18px; display:block; }
.drop-zone .drop-main { font-size:13px; color:var(--text); }
.drop-zone .drop-sub { font-size:11px; margin-top:2px; color:var(--text-dim); }
.drop-zone input { display:none; }
.viewer-result { display:none; grid-template-columns:minmax(280px, 42%) minmax(0, 1fr); gap:14px; align-items:start; }
.viewer-result.visible { display:grid; }
.viewer-preview { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:12px; min-width:0; position:sticky; top:12px; }
.viewer-preview img { display:block; max-width:100%; max-height:calc(100vh - 160px); margin:0 auto; border-radius:6px; border:1px solid var(--border); background:var(--bg-card); object-fit:contain; }
.viewer-file-name { margin-top:10px; font:11px var(--mono); color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.viewer-meta { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:14px; min-width:0; }
.vm-section { margin-bottom:14px; }
.vm-title { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.6px; color:var(--text-dim); margin-bottom:6px; padding-bottom:4px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; }
.vm-title .copy-btn { cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.vm-title .copy-btn:hover { color:var(--accent); }
.vm-prompt { font-family:var(--mono); font-size:11px; line-height:1.6; color:var(--prompt-text); word-break:break-word; white-space:pre-wrap; background:var(--bg-card); padding:8px 10px; border-radius:var(--radius); border:1px solid var(--border); }
.vm-prompt.negative { color:var(--neg-prompt); }
.vm-grid { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-family:var(--mono); font-size:11px; }
.vm-key { color:var(--setting-key); white-space:nowrap; }
.vm-val { color:var(--setting-val); word-break:break-all; }
.vm-raw { font-family:var(--mono); font-size:10px; line-height:1.6; color:var(--text); white-space:pre-wrap; word-break:break-all; background:var(--bg-card); padding:10px; border-radius:var(--radius); border:1px solid var(--border); max-height:300px; overflow-y:auto; }
.vm-file-info { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:11px; }
.vm-file-info .label { color:var(--text-dim); }
.vm-file-info .value { color:var(--text); font-family:var(--mono); }
.vm-empty { text-align:center; color:var(--text-dim); padding:40px 0; }
.vm-actions { display:flex; gap:10px; align-items:center; }
.vm-action { cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.vm-action:hover { color:var(--accent); }
.vm-modal { position:fixed; inset:0; background:rgba(0,0,0,.66); display:none; align-items:center; justify-content:center; z-index:7000; padding:18px; }
.vm-modal.open { display:flex; }
.vm-dialog { width:min(920px, 96vw); max-height:90vh; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:8px; display:flex; flex-direction:column; box-shadow:0 14px 42px rgba(0,0,0,.45); }
.vm-dialog-head, .vm-dialog-foot { padding:12px 16px; display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--border); }
.vm-dialog-foot { border-bottom:0; border-top:1px solid var(--border); justify-content:flex-end; }
.vm-dialog-title { color:var(--text-bright); font-weight:600; font-size:14px; }
.vm-dialog-close { margin-left:auto; border:0; background:none; color:var(--text-dim); font-size:20px; cursor:pointer; }
.vm-dialog-body { padding:14px 16px; overflow:auto; display:grid; gap:10px; }
.vm-edit-label { font-size:10px; font-weight:600; letter-spacing:.6px; text-transform:uppercase; color:var(--text-dim); }
.vm-edit-textarea { width:100%; min-height:110px; resize:vertical; background:var(--bg-card); border:1px solid var(--border); color:var(--text); border-radius:6px; padding:10px; outline:none; font:11px/1.55 var(--mono); }
.vm-edit-textarea.raw { min-height:260px; }
.vm-edit-textarea:focus { border-color:var(--accent); }
.vm-btn { border:1px solid var(--border); background:var(--bg-card); color:var(--text); border-radius:6px; padding:8px 12px; font:12px var(--font); cursor:pointer; }
.vm-btn:hover { border-color:var(--accent); color:var(--text-bright); }
.vm-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.vm-edit-error { color:#f87171; font:11px var(--mono); margin-right:auto; }
@media (max-width: 860px) {
    .viewer-result { grid-template-columns:1fr; }
    .viewer-preview { position:static; }
}
</style>
<div class="viewer-content">
    <div class="drop-zone" id="viewerDrop">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="1.5"/><path d="M21 15l-5-5L5 19"/></svg></div>
        <div>
            <div class="drop-main">Drop a PNG, JPG, or WEBP here, or click to select</div>
            <div class="drop-sub">Reads embedded generation metadata locally.</div>
        </div>
        <input type="file" id="viewerFile" accept=".png,.jpg,.jpeg,.webp">
    </div>
    <div id="viewerPreview" class="viewer-result">
        <div class="viewer-preview"><img id="viewerImg" src=""><div class="viewer-file-name" id="viewerFileName"></div></div>
        <div class="viewer-meta" id="viewerMeta"></div>
    </div>
</div>
<div id="metaEditModal" class="vm-modal">
    <div class="vm-dialog">
        <div class="vm-dialog-head"><div class="vm-dialog-title">Edit PNG metadata</div><button class="vm-dialog-close" id="editClose" type="button">&times;</button></div>
        <div class="vm-dialog-body">
            <label class="vm-edit-label" for="editPrompt">Prompt</label>
            <textarea id="editPrompt" class="vm-edit-textarea"></textarea>
            <label class="vm-edit-label" for="editNegativePrompt">Negative prompt</label>
            <textarea id="editNegativePrompt" class="vm-edit-textarea"></textarea>
            <button id="editApplyPrompt" class="vm-btn" type="button">Apply prompts to raw metadata</button>
            <label class="vm-edit-label" for="editRaw">Raw metadata JSON</label>
            <textarea id="editRaw" class="vm-edit-textarea raw" spellcheck="false"></textarea>
        </div>
        <div class="vm-dialog-foot"><span id="editError" class="vm-edit-error"></span><button id="editCancel" class="vm-btn" type="button">Cancel</button><button id="editDownload" class="vm-btn primary" type="button">Download edited PNG</button></div>
    </div>
</div>
<script>
(function() {
    var dropZone = document.getElementById('viewerDrop');
    var fileInput = document.getElementById('viewerFile');
    var currentFile = null;
    var currentData = null;
    var currentImageUrl = '';
    var copyPayloads = {};
    dropZone.addEventListener('click', function() { fileInput.click(); });
    dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', function() { dropZone.classList.remove('dragover'); });
    dropZone.addEventListener('drop', function(e) {
        e.preventDefault(); dropZone.classList.remove('dragover');
        if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', function() { if (this.files[0]) handleFile(this.files[0]); });

    function handleFile(file) {
        if (!file) return;
        currentFile = file;
        currentData = null;
        copyPayloads = {};
        if (currentImageUrl) URL.revokeObjectURL(currentImageUrl);
        currentImageUrl = URL.createObjectURL(file);
        document.getElementById('viewerImg').src = currentImageUrl;
        document.getElementById('viewerFileName').textContent = file.name;
        document.getElementById('viewerPreview').classList.add('visible');
        var fd = new FormData(); fd.append('file', file);
        fetch('/api/viewer/analyze', { method:'POST', body:fd })
            .then(function(r) { return r.json(); })
            .then(function(data) { renderMeta(data, file); })
            .catch(function(e) {
                document.getElementById('viewerMeta').innerHTML =
                    '<div class="vm-empty">Error: ' + escHtml(e.message) + '</div>';
            });
    }

    function renderMeta(data, file) {
        var el = document.getElementById('viewerMeta');
        currentData = data || null;
        if (!data || data.error) {
            el.innerHTML = '<div class="vm-empty">' + escHtml(data && data.error ? data.error : 'No metadata found') + '</div>';
            return;
        }
        var h = '', parsed = data.parsed || {}, raw = data.raw_meta || {}, info = data.info || {};
        var isPng = /\.png$/i.test(file.name || '');
        copyPayloads = {};

        h += '<div class="vm-section"><div class="vm-title">File Info</div><div class="vm-file-info">';
        h += '<span class="label">Name</span><span class="value">' + escHtml(file.name) + '</span>';
        h += '<span class="label">Size</span><span class="value">' + formatSize(file.size) + '</span>';
        if (info.width) {
            h += '<span class="label">Dimensions</span><span class="value">' + info.width + ' \u00D7 ' + info.height + '</span>';
        }
        h += '</div></div>';

        if (parsed.prompt) {
            copyPayloads.prompt = parsed.prompt;
            h += '<div class="vm-section"><div class="vm-title">Prompt <span class="vm-action" data-copy-key="prompt">Copy</span></div>';
            h += '<div class="vm-prompt">' + escHtml(parsed.prompt) + '</div></div>';
        }
        if (parsed.negative_prompt) {
            copyPayloads.negative = parsed.negative_prompt;
            h += '<div class="vm-section"><div class="vm-title">Negative Prompt <span class="vm-action" data-copy-key="negative">Copy</span></div>';
            h += '<div class="vm-prompt negative">' + escHtml(parsed.negative_prompt) + '</div></div>';
        }
        if (data.civitai) {
            var c = data.civitai;
            h += '<div class="vm-section"><div class="vm-title">Model (Civitai)</div><div class="vm-prompt">';
            h += '<a href="https://civitai.com/models/' + c.id + '" target="_blank" style="color:var(--accent)">' + escHtml(c.model) + '</a>';
            if (c.version) h += ' \u00B7 ' + escHtml(c.version);
            if (c.base) h += ' \u00B7 ' + escHtml(c.base);
            if (c.creator) h += ' \u00B7 by ' + escHtml(c.creator);
            h += '</div></div>';
        }
        if (parsed.settings && Object.keys(parsed.settings).length) {
            h += '<div class="vm-section"><div class="vm-title">Settings</div><div class="vm-grid">';
            var settingsKeys = Object.keys(parsed.settings);
            [
                ['Model', 'Model hash'],
                ['VAE', 'VAE hash']
            ].forEach(function(pair) {
                var first = settingsKeys.indexOf(pair[0]);
                var second = settingsKeys.indexOf(pair[1]);
                if (first >= 0 && second >= 0 && second < first) {
                    settingsKeys.splice(second, 1);
                    first = settingsKeys.indexOf(pair[0]);
                    settingsKeys.splice(first + 1, 0, pair[1]);
                }
            });
            settingsKeys.forEach(function(k) {
                h += '<span class="vm-key">' + escHtml(k) + '</span><span class="vm-val">' + escHtml(parsed.settings[k]) + '</span>';
            });
            h += '</div></div>';
        }
        var rawText = '';
        for (var rk in raw) {
            var rv = raw[rk];
            if (rv !== null && typeof rv === 'object') rv = JSON.stringify(rv, null, 2);
            rawText += rk + ': ' + rv + '\n\n';
        }
        if (rawText) {
            copyPayloads.raw = rawText;
            h += '<div class="vm-section"><div class="vm-title"><span>Raw Metadata</span><span class="vm-actions"><span class="vm-action" data-copy-key="raw">Copy All</span>';
            if (isPng) h += '<span class="vm-action" data-action="edit">Edit</span>';
            h += '</span></div>';
            h += '<div class="vm-raw">' + escHtml(rawText) + '</div></div>';
        }
        if (!parsed.prompt && !rawText) {
            if (isPng) {
                h += '<div class="vm-section"><div class="vm-title"><span>Raw Metadata</span><span class="vm-actions"><span class="vm-action" data-action="edit">Edit</span></span></div>';
                h += '<div class="vm-empty">No generation metadata found</div></div>';
            } else {
                h += '<div class="vm-empty">No generation metadata found</div>';
            }
        }
        el.innerHTML = h;
        el.querySelectorAll('[data-copy-key]').forEach(function(btn) {
            btn.addEventListener('click', function() { copyText(copyPayloads[this.getAttribute('data-copy-key')] || '', this); });
        });
        el.querySelectorAll('[data-action="edit"]').forEach(function(btn) {
            btn.addEventListener('click', openEditor);
        });
    }

    function openEditor() {
        if (!currentFile || !/\.png$/i.test(currentFile.name || '')) {
            alert('Metadata editing currently supports PNG files only.');
            return;
        }
        var raw = (currentData && currentData.raw_meta) || {};
        document.getElementById('editPrompt').value = (currentData && currentData.parsed && currentData.parsed.prompt) || '';
        document.getElementById('editNegativePrompt').value = (currentData && currentData.parsed && currentData.parsed.negative_prompt) || '';
        document.getElementById('editRaw').value = JSON.stringify(raw, null, 2);
        setEditError('');
        document.getElementById('metaEditModal').classList.add('open');
    }

    function closeEditor() {
        document.getElementById('metaEditModal').classList.remove('open');
    }

    function setEditError(msg) {
        document.getElementById('editError').textContent = msg || '';
    }

    function parseRawEditor() {
        var raw;
        try {
            raw = JSON.parse(document.getElementById('editRaw').value || '{}');
        } catch (e) {
            throw new Error('Raw metadata is not valid JSON: ' + e.message);
        }
        if (!raw || Array.isArray(raw) || typeof raw !== 'object') {
            throw new Error('Raw metadata must be a JSON object.');
        }
        return raw;
    }

    function replacePromptsInParameters(parameters, prompt, negativePrompt) {
        var text = String(parameters || '');
        var lines = text.split('\n');
        var negIdx = -1, settingsIdx = -1;
        for (var i = 0; i < lines.length; i++) {
            if (negIdx < 0 && /^Negative prompt:/i.test(lines[i])) negIdx = i;
            if (/^Steps:\s*/i.test(lines[i])) {
                settingsIdx = i;
                break;
            }
        }
        var out = [prompt || ''];
        if (negativePrompt) out.push('Negative prompt: ' + negativePrompt);
        if (settingsIdx >= 0) out = out.concat(lines.slice(settingsIdx));
        else if (negIdx < 0 && !text.trim() && !negativePrompt) out = [prompt || ''];
        return out.join('\n');
    }

    function applyPromptToRaw() {
        try {
            var raw = parseRawEditor();
            var prompt = document.getElementById('editPrompt').value || '';
            var negativePrompt = document.getElementById('editNegativePrompt').value || '';
            raw.parameters = replacePromptsInParameters(raw.parameters || '', prompt, negativePrompt);
            document.getElementById('editRaw').value = JSON.stringify(raw, null, 2);
            setEditError('');
        } catch (e) {
            setEditError(e.message);
        }
    }

    function downloadEdited() {
        var raw;
        try {
            raw = parseRawEditor();
        } catch (e) {
            setEditError(e.message);
            return;
        }
        setEditError('');
        var btn = document.getElementById('editDownload');
        var old = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Saving...';
        var fd = new FormData();
        fd.append('file', currentFile, currentFile.name);
        fd.append('raw_meta_json', new Blob([JSON.stringify(raw)], {type:'application/json'}), 'metadata.json');
        fetch('/api/viewer/rewrite', {method:'POST', body:fd})
            .then(function(r) {
                if (!r.ok) {
                    return r.json().catch(function(){return {};}).then(function(d){ throw new Error(d.error || ('HTTP ' + r.status)); });
                }
                return r.blob();
            })
            .then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = (currentFile.name || 'image.png').replace(/\.png$/i, '') + '_metadata.png';
                a.click();
                setTimeout(function(){ URL.revokeObjectURL(a.href); }, 2000);
                closeEditor();
            })
            .catch(function(e) { setEditError(e.message); })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = old;
            });
    }

    document.getElementById('editClose').addEventListener('click', closeEditor);
    document.getElementById('editCancel').addEventListener('click', closeEditor);
    document.getElementById('editApplyPrompt').addEventListener('click', applyPromptToRaw);
    document.getElementById('editDownload').addEventListener('click', downloadEdited);
    document.getElementById('metaEditModal').addEventListener('click', function(e) {
        if (e.target === this) closeEditor();
    });
})();
</script>
"""
