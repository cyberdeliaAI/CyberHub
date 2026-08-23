"""Meta Copy Tool — restore PNG generation metadata after external upscaling.

Includes:
- Single-file quick merge: original PNG metadata -> upscaled PNG pixels.
- Batch Match workflow adapted from CyberHub's PNG Meta Copy desktop tool:
  source/upscaled/output folders, suffix-regex matching, match preview, progress log,
  and optional safe cleanup through the operating-system recycle bin.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from core import Module
from core.metadata import merge_png_meta, get_image_metadata, parse_sd_parameters
from core.server import build_shell

try:
    from send2trash import send2trash
    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False
    send2trash = None


class ToolsModule(Module):
    name = "Meta Copy Tool"

    def key(self):
        """Keep stable internal module id/routes while showing a clearer label."""
        return "tools"

    icon = "\U0001F527"   # 🔧
    description = "Restore metadata onto upscaled PNGs, one file or an automatically matched batch."
    order = 30
    settings_schema = {}

    def __init__(self, hub):
        super().__init__(hub)
        # Folder choices persist across restarts (saved to settings.json on pick).
        self._source_folder = self.setting("source_folder", "")
        self._upscaled_folder = self.setting("upscaled_folder", "")
        self._output_folder = self.setting("output_folder", "")
        self._suffix_pattern = r"-gigapixel.*"

    def _persist_folders(self):
        s = self.hub.settings
        s.set_module_setting(self.key(), "source_folder", self._source_folder)
        s.set_module_setting(self.key(), "upscaled_folder", self._upscaled_folder)
        s.set_module_setting(self.key(), "output_folder", self._output_folder)

    def routes_get(self):
        return {
            "/tools": self._page,
            "/api/tools/batch/state": self._batch_state,
        }

    def routes_post(self):
        return {
            "/api/merge": self._merge,
            "/api/tools/analyze": self._analyze,
            "/api/tools/batch/session": self._batch_session,
            "/api/tools/batch/run": self._batch_run,
        }

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="tools", page_title="Meta Copy Tool",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    # ── Existing single-file merge ───────────────────────────────────────
    def _merge(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
            source_item = files.get("source")
            target_item = files.get("target")
            if not source_item or not target_item or not source_item.get("data") or not target_item.get("data"):
                handler.respond_json({"error": "Need both source and target PNG files"}, status=400)
                return
            merged = merge_png_meta(source_item["data"], target_item["data"])
            handler.respond_binary(merged, "image/png")
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=400)

    def _analyze(self, handler, content_len, content_type):
        """Analyze an uploaded image for metadata preview."""
        try:
            files = handler.parse_multipart(content_len, content_type)
            file_item = files.get("file")
            if not file_item or not file_item.get("data"):
                handler.respond_json({"error": "No file uploaded"}, status=400)
                return
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
                    parsed["prompt"] = meta.get("prompt", "")
                civitai = self.hub.civitai.lookup(parsed) if self.hub.civitai else None
                handler.respond_json({"parsed": parsed, "raw_meta": meta, "civitai": civitai})
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500)

    # ── Batch workflow ────────────────────────────────────────────────────
    @staticmethod
    def _existing_folder(path):
        path = str(path or "").strip()
        return os.path.abspath(path) if path and os.path.isdir(path) else ""

    @staticmethod
    def _list_pngs(folder):
        if not folder:
            return []
        try:
            return sorted(
                p for p in Path(folder).iterdir()
                if p.is_file() and p.suffix.lower() == ".png"
            )
        except OSError:
            return []

    def _validate_pattern(self, pattern):
        try:
            return re.compile((pattern or "") + r"$")
        except re.error as exc:
            raise ValueError(f"Invalid suffix regular expression: {exc}")

    def _scan_matches(self):
        src_folder = self._existing_folder(self._source_folder)
        inp_folder = self._existing_folder(self._upscaled_folder)
        pattern = self._validate_pattern(self._suffix_pattern)
        source_files = self._list_pngs(src_folder)
        input_files = self._list_pngs(inp_folder)

        lookup = {}
        duplicate_keys = set()
        for path in input_files:
            key = pattern.sub("", path.stem) if self._suffix_pattern else path.stem
            if key in lookup:
                duplicate_keys.add(key)
            else:
                lookup[key] = path

        pairs = []
        unmatched_source = []
        matched_inputs = set()
        for source in source_files:
            target = lookup.get(source.stem)
            if target and source.stem not in duplicate_keys:
                meta = get_image_metadata(str(source))
                pairs.append({
                    "source": source.name,
                    "upscaled": target.name,
                    "has_metadata": bool(meta),
                    "metadata_keys": list(meta.keys())[:8],
                })
                matched_inputs.add(target.name)
            elif source.stem in duplicate_keys:
                unmatched_source.append({"name": source.name, "reason": "Multiple upscaled files match this name"})
            else:
                unmatched_source.append({"name": source.name, "reason": "No matching upscale found"})

        unmatched_upscaled = [
            p.name for p in input_files if p.name not in matched_inputs
        ]
        return {
            "source_count": len(source_files),
            "upscaled_count": len(input_files),
            "pairs": pairs,
            "unmatched_source": unmatched_source,
            "unmatched_upscaled": unmatched_upscaled,
            "duplicate_keys": sorted(duplicate_keys),
        }

    def _state_payload(self):
        payload = {
            "source_folder": self._source_folder,
            "upscaled_folder": self._upscaled_folder,
            "output_folder": self._output_folder,
            "suffix_pattern": self._suffix_pattern,
            "trash_available": HAS_TRASH,
        }
        if self._existing_folder(self._source_folder) and self._existing_folder(self._upscaled_folder):
            payload.update(self._scan_matches())
        else:
            payload.update({
                "source_count": 0, "upscaled_count": 0, "pairs": [],
                "unmatched_source": [], "unmatched_upscaled": [], "duplicate_keys": [],
            })
        return payload

    def _batch_state(self, handler, qs):
        try:
            handler.respond_json(self._state_payload())
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)

    def _batch_session(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        source = str(data.get("source_folder", "")).strip()
        upscaled = str(data.get("upscaled_folder", "")).strip()
        output = str(data.get("output_folder", "")).strip()
        suffix = str(data.get("suffix_pattern", self._suffix_pattern))
        try:
            self._validate_pattern(suffix)
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)
            return
        if not source or not os.path.isdir(source):
            handler.respond_json({"error": "Original/source folder not found"}, status=404)
            return
        if not upscaled or not os.path.isdir(upscaled):
            handler.respond_json({"error": "Upscaled/input folder not found"}, status=404)
            return
        self._source_folder = os.path.abspath(source)
        self._upscaled_folder = os.path.abspath(upscaled)
        self._output_folder = os.path.abspath(output) if output else ""
        self._suffix_pattern = suffix
        self._persist_folders()
        try:
            handler.respond_json(self._state_payload())
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)

    def _batch_run(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        try:
            scan = self._scan_matches()
        except ValueError as exc:
            handler.respond_json({"error": str(exc)}, status=400)
            return

        source_root = self._existing_folder(self._source_folder)
        input_root = self._existing_folder(self._upscaled_folder)
        output_value = str(data.get("output_folder", self._output_folder) or "").strip()
        if not source_root or not input_root:
            handler.respond_json({"error": "Load source and upscaled folders before processing"}, status=400)
            return
        if not output_value:
            handler.respond_json({"error": "Please choose an output folder"}, status=400)
            return

        output_root = Path(os.path.abspath(output_value))
        delete_originals = bool(data.get("delete_originals", False))
        overwrite = bool(data.get("overwrite", False))
        if delete_originals and not HAS_TRASH:
            handler.respond_json({
                "error": "Safe cleanup is unavailable because send2trash is not installed. No files were deleted."
            }, status=503)
            return
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            handler.respond_json({"error": f"Cannot create output folder: {exc}"}, status=403)
            return
        self._output_folder = str(output_root)
        self._persist_folders()

        results = []
        ok = skipped = failed = 0
        for pair in scan["pairs"]:
            src = Path(source_root) / pair["source"]
            inp = Path(input_root) / pair["upscaled"]
            out = output_root / src.name
            try:
                resolved_out = out.resolve()
                if resolved_out in (src.resolve(), inp.resolve()):
                    results.append({"ok": False, "status": "skipped", "source": src.name,
                                    "upscaled": inp.name, "message": "Output would overwrite an input file"})
                    skipped += 1
                    continue
                if out.exists() and not overwrite:
                    results.append({"ok": False, "status": "skipped", "source": src.name,
                                    "upscaled": inp.name, "message": "Output already exists"})
                    skipped += 1
                    continue
                merged = merge_png_meta(src.read_bytes(), inp.read_bytes())
                out.write_bytes(merged)
                msg = f"Saved {out.name}"
                if delete_originals:
                    send2trash(str(src))
                    send2trash(str(inp))
                    msg += " · originals moved to recycle bin"
                results.append({"ok": True, "status": "ok", "source": src.name,
                                "upscaled": inp.name, "output": out.name, "message": msg})
                ok += 1
            except Exception as exc:
                results.append({"ok": False, "status": "failed", "source": src.name,
                                "upscaled": inp.name, "message": str(exc)})
                failed += 1

        handler.respond_json({
            "ok": failed == 0,
            "processed": len(scan["pairs"]),
            "success": ok,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        })


PAGE_BODY = r"""
<style>
.mc-app{display:flex;flex-direction:column;height:calc(100vh - 48px);background:var(--bg-darkest);min-height:0}
.mc-tabs{display:flex;gap:4px;padding:0 22px;background:var(--bg-panel);border-bottom:1px solid var(--border);flex-shrink:0}
.mc-tab{font:inherit;font-size:13px;font-weight:600;background:none;border:0;color:var(--text-dim);padding:11px 14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.mc-tab:hover{color:var(--text)}
.mc-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.mc-body{flex:1 1 auto;min-height:0;overflow:auto;padding:18px 22px}
.mc-pane{display:none}.mc-pane.active{display:block}

.mc-grid{display:grid;grid-template-columns:minmax(360px,430px) 1fr;gap:16px;align-items:start}
.mc-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;padding:15px}
.mc-card-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;color:var(--text-dim);margin-bottom:12px}
.mc-mode{display:flex;gap:4px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:3px;margin-bottom:13px}
.mc-mode button{flex:1;height:30px;border:0;border-radius:6px;background:transparent;color:var(--text-dim);font:12px var(--font);font-weight:600;cursor:pointer}
.mc-mode button.active{background:var(--bg-active);color:var(--accent)}
.mc-row{display:grid;grid-template-columns:90px 1fr auto;align-items:center;gap:7px;margin-bottom:9px}
.mc-label{font-size:12px;color:var(--text-dim)}
.mc-input{height:32px;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:0 10px;font:12px var(--font);min-width:0}
.mc-input:focus{outline:none;border-color:var(--accent)}
.mc-btn{height:32px;background:var(--bg-card);border:1px solid var(--border);border-radius:7px;color:var(--text);padding:0 12px;font:12px var(--font);font-weight:400;cursor:pointer;white-space:nowrap}
.mc-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text-bright)}
.mc-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.mc-btn.primary:hover:not(:disabled){background:var(--accent-dim);border-color:var(--accent-dim);color:#fff}
.mc-btn:disabled{opacity:.42;cursor:default}
.mc-options{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:14px;font-size:12px;color:var(--text)}
.mc-options label{display:flex;align-items:center;gap:6px;cursor:pointer}
.mc-options input[type=checkbox]{accent-color:var(--accent)}
.mc-actions{display:flex;gap:8px;margin-top:14px}
.mc-file-input{display:none}
.mc-help{font-size:11px;color:var(--text-dim);margin-top:8px;line-height:1.55}
.mc-help code{background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:10px}

.mc-summary{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 11px}
.mc-badge{background:var(--bg-card);border:1px solid var(--border);border-radius:999px;padding:4px 10px;font-size:11px;color:var(--text-dim)}
.mc-badge.ok{color:var(--green);border-color:rgba(61,220,132,.32)}
.mc-badge.warn{color:#f59e0b;border-color:rgba(245,158,11,.32)}
.mc-table{border:1px solid var(--border);border-radius:8px;overflow:auto;max-height:355px}
.mc-table table{border-collapse:collapse;width:100%;font-size:11px}
.mc-table th{position:sticky;top:0;background:var(--bg-card);text-align:left;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);padding:8px 10px;font-size:10px;font-weight:700}
.mc-table td{padding:8px 10px;border-top:1px solid var(--border);font-family:var(--mono);word-break:break-all;color:var(--text)}
.mc-meta-ok{color:var(--green)}
.mc-meta-none{color:#f59e0b}
.mc-log{height:160px;overflow:auto;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;margin-top:12px;padding:9px 11px;font:11px/1.55 var(--mono);color:var(--text-dim);white-space:pre-wrap}

.mc-merge{display:grid;grid-template-columns:1fr auto 1fr;gap:16px;align-items:start;max-width:1000px;margin:0 auto}
.mc-merge-panel{background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:16px}
.mc-merge-panel h3{font-size:12px;font-weight:600;color:var(--text-bright);margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;display:flex;align-items:center;gap:7px}
.mc-merge-panel h3 .section-icon{width:15px;height:15px;display:inline-flex;align-items:center;justify-content:center;color:var(--text-dim);flex-shrink:0}
.mc-merge-panel h3 .section-icon svg{width:14px;height:14px;display:block}
.mc-drop{border:2px dashed var(--border-light);border-radius:8px;padding:38px 16px;text-align:center;color:var(--text-dim);transition:all .2s;cursor:pointer;background:var(--bg-card);min-height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px}
.mc-drop:hover,.mc-drop.dragover{border-color:var(--accent);background:var(--bg-active)}
.mc-drop .mc-drop-icon{font-size:28px}
.mc-drop input{display:none}
.mc-drop.has-file{border-style:solid;border-color:var(--green)}
.mc-drop .mc-fname{font-family:var(--mono);font-size:11px;color:var(--green);word-break:break-all}
.mc-drop .mc-fsize{font-size:10px;color:var(--text-dim)}
.mc-preview{margin-top:12px;text-align:center}
.mc-preview img{max-width:100%;max-height:200px;border-radius:6px;border:1px solid var(--border)}
.mc-arrow{display:flex;align-items:center;justify-content:center;font-size:24px;color:var(--accent);padding-top:80px}
.mc-merge-actions{text-align:center;margin-top:20px}
.mc-merge-status{margin-top:12px;font-size:12px;color:var(--text-dim);text-align:center}
.mc-meta-preview{margin-top:12px;font-size:10px;color:var(--text-dim);font-family:var(--mono);max-height:100px;overflow-y:auto;background:var(--bg-card);padding:6px 8px;border-radius:var(--radius);border:1px solid var(--border);white-space:pre-wrap;word-break:break-all}

/* Folder browser — shared pattern across modules, same class names */
.browse-overlay{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:9000;display:none;align-items:center;justify-content:center}
.browse-overlay.open{display:flex}
.browse-dialog{background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;width:600px;max-height:74vh;display:flex;flex-direction:column}
.browse-header,.browse-footer{padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.browse-header{border-bottom:1px solid var(--border)}
.browse-footer{border-top:1px solid var(--border)}
.browse-header h3{font-size:14px;margin:0}
.browse-close{font-size:20px;color:var(--text-dim);background:none;border:0;cursor:pointer}
.browse-crumb{padding:8px 16px;border-bottom:1px solid var(--border);font:11px var(--mono);color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.browse-body{min-height:240px;max-height:420px;overflow:auto;padding:5px 0}
.browse-entry{display:flex;gap:10px;align-items:center;padding:8px 16px;cursor:pointer;font-size:12px}
.browse-entry:hover{background:var(--bg-hover);color:var(--accent)}
.browse-current{font:10px var(--mono);color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}

@media(max-width:900px){.mc-grid{grid-template-columns:1fr}.mc-merge{grid-template-columns:1fr}.mc-arrow{padding-top:0;transform:rotate(90deg)}.mc-row{grid-template-columns:1fr}.mc-row .mc-label{margin-bottom:-3px}}
</style>

<div class="mc-app">
  <div class="mc-tabs">
    <button class="mc-tab active" data-pane="batch">Batch Match</button>
    <button class="mc-tab" data-pane="single">Single File Merge</button>
  </div>

  <div class="mc-body">
    <section id="pane-batch" class="mc-pane active">
      <div class="mc-grid">
        <div class="mc-card">
          <div class="mc-card-title">Folders &amp; Matching</div>
          <div class="mc-mode"><button class="active" id="modeServer" type="button">Server folders</button><button id="modeBrowser" type="button">Browser folders</button></div>
          <div class="serverOnly">
            <div class="mc-row"><span class="mc-label">Originals</span><input id="batchSource" class="mc-input" placeholder="PNG files with metadata"><button class="mc-btn browseBtn" data-target="batchSource">Browse</button></div>
            <div class="mc-row"><span class="mc-label">Upscaled</span><input id="batchUpscaled" class="mc-input" placeholder="Externally upscaled PNG files"><button class="mc-btn browseBtn" data-target="batchUpscaled">Browse</button></div>
            <div class="mc-row"><span class="mc-label">Output</span><input id="batchOutput" class="mc-input" placeholder="Required output folder"><button class="mc-btn browseBtn" data-target="batchOutput">Browse</button></div>
          </div>
          <div class="browserOnly" style="display:none">
            <div class="mc-row"><span class="mc-label">Originals</span><input id="browserSourceLabel" class="mc-input" readonly placeholder="Choose local folder with metadata PNGs"><button class="mc-btn" id="chooseBrowserSource">Choose</button></div>
            <div class="mc-row"><span class="mc-label">Upscaled</span><input id="browserUpscaledLabel" class="mc-input" readonly placeholder="Choose local folder with upscaled PNGs"><button class="mc-btn" id="chooseBrowserUpscaled">Choose</button></div>
            <div class="mc-help">Browser files are uploaded one matched pair at a time, merged on the Hub, then downloaded back to this computer as <code>meta-copy-output.zip</code>.</div>
            <input id="browserSourceFiles" class="mc-file-input" type="file" accept=".png" webkitdirectory multiple>
            <input id="browserUpscaledFiles" class="mc-file-input" type="file" accept=".png" webkitdirectory multiple>
          </div>
          <div class="mc-row"><span class="mc-label">Strip suffix</span><input id="batchSuffix" class="mc-input" value="-gigapixel.*" placeholder="-gigapixel.*"><span></span></div>
          <div class="mc-help">Regex applied to the end of each upscaled filename before matching. Example: <code>-gigapixel.*</code> matches <code>portrait-gigapixel-scale-4x.png</code> to <code>portrait.png</code>.</div>
          <div class="mc-options">
            <label class="serverOnly"><input type="checkbox" id="batchOverwrite"> Overwrite existing output</label>
            <label class="serverOnly"><input type="checkbox" id="batchDelete"> Move originals to recycle bin after success</label>
          </div>
          <div class="mc-actions">
            <button class="mc-btn" id="scanBtn">🔍 Scan &amp; Match</button>
            <button class="mc-btn primary" id="runBtn" disabled>▶ Copy Metadata</button>
          </div>
          <div class="mc-log" id="batchLog">Choose the original and upscaled folders, then click Scan &amp; Match.</div>
        </div>

        <div class="mc-card">
          <div class="mc-card-title">Matches</div>
          <div class="mc-summary" id="batchSummary"><span class="mc-badge">Not scanned</span></div>
          <div class="mc-table">
            <table><thead><tr><th>Original</th><th>Upscaled</th><th>Metadata</th></tr></thead><tbody id="matchRows"><tr><td colspan="3">No matches loaded.</td></tr></tbody></table>
          </div>
          <div class="mc-help" id="unmatchedInfo"></div>
        </div>
      </div>
    </section>

    <section id="pane-single" class="mc-pane">
      <div class="mc-merge">
        <div class="mc-merge-panel">
          <h3><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="3" width="8" height="4" rx="1"/><path d="M9 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3"/><path d="M8 12h8"/><path d="M8 16h6"/></svg></span>Original with metadata</h3>
          <div class="mc-drop" id="srcDrop"><div class="mc-drop-icon">📷</div><div>Drop original PNG</div><input type="file" id="srcFile" accept=".png"></div>
          <div class="mc-preview" id="srcPreview" style="display:none"><img id="srcImg" src="" alt=""></div>
          <div class="mc-meta-preview" id="srcMeta" style="display:none"></div>
        </div>
        <div class="mc-arrow">➡</div>
        <div class="mc-merge-panel">
          <h3><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="1.5"/><path d="M21 16l-5-5-4 4-2-2-5 5"/></svg></span>Upscaled image</h3>
          <div class="mc-drop" id="tgtDrop"><div class="mc-drop-icon">⬆</div><div>Drop upscaled PNG</div><input type="file" id="tgtFile" accept=".png"></div>
          <div class="mc-preview" id="tgtPreview" style="display:none"><img id="tgtImg" src="" alt=""></div>
        </div>
      </div>
      <div class="mc-merge-actions"><button class="mc-btn primary" id="mergeBtn" disabled>Merge Metadata → Download</button></div>
      <div class="mc-merge-status" id="mergeStatus"></div>
    </section>
  </div>
</div>

<div id="browseOverlay" class="browse-overlay"><div class="browse-dialog"><div class="browse-header"><h3>Select folder</h3><button class="browse-close" id="browseClose">×</button></div><div class="browse-crumb" id="browseCrumb"></div><div class="browse-body" id="browseBody"></div><div class="browse-footer"><span class="browse-current" id="browsePath"></span><button class="mc-btn primary" id="browseSelect">Select folder</button></div></div></div>

<script>
(function(){
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function attr(s){return esc(s).replace(/'/g,'&#39;');}
function apiError(r){return r.json().catch(function(){return {};}).then(function(d){throw new Error(d.error||('HTTP '+r.status));});}
function toast(msg,bad){ if(typeof showToast==='function') showToast(msg, bad ? 'error' : undefined); else alert(msg); }

document.querySelectorAll('.mc-tab').forEach(function(btn){btn.onclick=function(){document.querySelectorAll('.mc-tab').forEach(function(b){b.classList.remove('active');});document.querySelectorAll('.mc-pane').forEach(function(p){p.classList.remove('active');});btn.classList.add('active');$('pane-'+btn.dataset.pane).classList.add('active');};});

/* Folder browser shared with the other image modules */
var browseTarget='', browseCurrent='';
function browseOpen(target){browseTarget=target;$('browseOverlay').classList.add('open');browseLoad($(target).value.trim());}
function browseLoad(path){fetch('/api/browse?path='+encodeURIComponent(path||'')).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){browseCurrent=d.path||'';$('browsePath').textContent=d.display||browseCurrent||'Drives';var crumbs=['<span data-path="">Root</span>'];(d.crumbs||[]).forEach(function(c){crumbs.push('<span> › </span><span data-path="'+attr(c.path)+'">'+esc(c.label)+'</span>');});$('browseCrumb').innerHTML=crumbs.join('');var html='';if(d.parent!==null&&d.parent!==undefined)html+='<div class="browse-entry" data-path="'+attr(d.parent)+'">↩ <span>..</span></div>';(d.dirs||[]).forEach(function(dir){html+='<div class="browse-entry" data-path="'+attr(dir.path)+'"><span>'+esc(dir.name)+'</span></div>';});$('browseBody').innerHTML=html||'<div class="browse-entry">No subfolders</div>';}).catch(function(e){browseCurrent='';$('browseBody').innerHTML='<div class="browse-entry">Failed to load: '+esc(e.message)+'</div>';$('browsePath').textContent='';toast(e.message,true);});}
document.querySelectorAll('.browseBtn').forEach(function(btn){btn.onclick=function(){browseOpen(btn.dataset.target);};});
$('browseClose').onclick=function(){$('browseOverlay').classList.remove('open');};
$('browseSelect').onclick=function(){if(browseCurrent&&browseTarget)$(browseTarget).value=browseCurrent;$('browseOverlay').classList.remove('open');};
$('browseOverlay').onclick=function(e){if(e.target===this){this.classList.remove('open');return;}var el=e.target.closest('[data-path]');if(el)browseLoad(el.getAttribute('data-path'));};

/* Batch mode */
var batchState={pairs:[]};
var batchMode='server';
var browserFiles={source:[],upscaled:[]};
function batchBody(){return {source_folder:$('batchSource').value.trim(),upscaled_folder:$('batchUpscaled').value.trim(),output_folder:$('batchOutput').value.trim(),suffix_pattern:$('batchSuffix').value};}
function setHeadStatus(text, kind){
    var el=$('mcStatus'); if(!el) return;
    el.textContent=text;
    el.style.color = kind==='ok' ? 'var(--green)' : (kind==='warn' ? '#f59e0b' : '');
}
function setBatchMode(mode){
    batchMode=mode;
    $('modeServer').classList.toggle('active',mode==='server');
    $('modeBrowser').classList.toggle('active',mode==='browser');
    document.querySelectorAll('.serverOnly').forEach(function(el){el.style.display=mode==='server'?'':'none';});
    document.querySelectorAll('.browserOnly').forEach(function(el){el.style.display=mode==='browser'?'':'none';});
    $('runBtn').textContent=mode==='browser'?'▶ Copy Metadata & Download Zip':'▶ Copy Metadata';
    batchState={pairs:[]};
    $('runBtn').disabled=true;
    $('batchSummary').innerHTML='<span class="mc-badge">Not scanned</span>';
    $('matchRows').innerHTML='<tr><td colspan="3">No matches loaded.</td></tr>';
    $('unmatchedInfo').textContent='';
    $('batchLog').textContent=mode==='browser'
        ? 'Choose local original and upscaled folders, then click Scan & Match.'
        : 'Choose the original and upscaled folders, then click Scan & Match.';
}
function pngFiles(fileList){
    return Array.prototype.slice.call(fileList||[]).filter(function(f){return /\.png$/i.test(f.name);}).map(function(f){
        var rel=f.webkitRelativePath||f.name;
        var name=rel.split(/[\\/]/).pop()||f.name;
        var stem=name.replace(/\.png$/i,'');
        return {file:f,rel:rel,name:name,stem:stem,size:f.size};
    }).sort(function(a,b){return a.rel.localeCompare(b.rel);});
}
function folderLabel(files){
    if(!files.length)return '';
    var first=files[0].rel||files[0].name;
    var folder=first.indexOf('/')>=0?first.split('/')[0]:'Selected files';
    return folder+' · '+files.length+' PNG';
}
function chooseBrowserFolder(kind){
    $(kind==='source'?'browserSourceFiles':'browserUpscaledFiles').click();
}
function scanBrowserMatches(){
    var suffix=$('batchSuffix').value||'';
    var pattern;
    try{pattern=new RegExp(suffix+'$');}
    catch(e){throw new Error('Invalid suffix regular expression: '+e.message);}
    var lookup={},duplicateKeys={},sourceDup={},sourceSeen={};
    browserFiles.upscaled.forEach(function(item){
        var key=suffix?item.stem.replace(pattern,''):item.stem;
        if(lookup[key])duplicateKeys[key]=true; else lookup[key]=item;
    });
    browserFiles.source.forEach(function(item){if(sourceSeen[item.stem])sourceDup[item.stem]=true;sourceSeen[item.stem]=true;});
    var pairs=[],unmatched=[];
    browserFiles.source.forEach(function(source){
        var target=lookup[source.stem];
        if(sourceDup[source.stem])unmatched.push({name:source.rel,reason:'Multiple originals share this filename stem'});
        else if(duplicateKeys[source.stem])unmatched.push({name:source.rel,reason:'Multiple upscaled files match this name'});
        else if(target)pairs.push({source:source.rel,upscaled:target.rel,has_metadata:null,metadata_keys:[],sourceFile:source.file,upscaledFile:target.file,outputName:source.name});
        else unmatched.push({name:source.rel,reason:'No matching upscale found'});
    });
    var matched={};
    pairs.forEach(function(p){matched[p.upscaled]=true;});
    var unmatchedUpscaled=browserFiles.upscaled.filter(function(f){return !matched[f.rel];}).map(function(f){return f.rel;});
    return {mode:'browser',source_folder:folderLabel(browserFiles.source),upscaled_folder:folderLabel(browserFiles.upscaled),suffix_pattern:suffix,source_count:browserFiles.source.length,upscaled_count:browserFiles.upscaled.length,pairs:pairs,unmatched_source:unmatched,unmatched_upscaled:unmatchedUpscaled,duplicate_keys:Object.keys(duplicateKeys)};
}
function crc32(bytes){var c=~0;for(var i=0;i<bytes.length;i++){c^=bytes[i];for(var k=0;k<8;k++)c=(c>>>1)^(0xEDB88320&-(c&1));}return(~c)>>>0;}
function u16(n){var b=new Uint8Array(2);new DataView(b.buffer).setUint16(0,n,true);return b;}
function u32(n){var b=new Uint8Array(4);new DataView(b.buffer).setUint32(0,n,true);return b;}
function safeZipName(name,used){name=String(name||'output.png').replace(/^[\\/]+/,'').replace(/\\/g,'/').replace(/\.\./g,'_');if(!name)name='output.png';var base=name,ext='',m=name.match(/^(.*?)(\.[^.\/]*)$/);if(m){base=m[1];ext=m[2];}var out=name,i=2;while(used[out]){out=base+'_'+i+ext;i++;}used[out]=true;return out;}
async function makeZipBlob(items){
    var encoder=new TextEncoder(),parts=[],central=[],offset=0,used={};
    for(var i=0;i<items.length;i++){
        var bytes=new Uint8Array(await items[i].blob.arrayBuffer());
        var nameBytes=encoder.encode(safeZipName(items[i].name,used));
        var crc=crc32(bytes),size=bytes.length;
        var local=[u32(0x04034b50),u16(20),u16(0x0800),u16(0),u16(0),u16(0),u32(crc),u32(size),u32(size),u16(nameBytes.length),u16(0),nameBytes];
        parts.push.apply(parts,local);parts.push(bytes);
        central.push({nameBytes:nameBytes,crc:crc,size:size,offset:offset});
        offset+=30+nameBytes.length+size;
    }
    var centralStart=offset;
    central.forEach(function(c){
        var row=[u32(0x02014b50),u16(20),u16(20),u16(0x0800),u16(0),u16(0),u16(0),u32(c.crc),u32(c.size),u32(c.size),u16(c.nameBytes.length),u16(0),u16(0),u16(0),u16(0),u32(0),u32(c.offset),c.nameBytes];
        parts.push.apply(parts,row);offset+=46+c.nameBytes.length;
    });
    var centralSize=offset-centralStart;
    parts.push(u32(0x06054b50),u16(0),u16(0),u16(central.length),u16(central.length),u32(centralSize),u32(centralStart),u16(0));
    return new Blob(parts,{type:'application/zip'});
}
async function runBrowserBatch(){
    var pairs=batchState.pairs||[];
    if(!pairs.length){toast('Scan browser folders first',true);return;}
    var btn=$('runBtn'),results=[],zipItems=[],ok=0,failed=0;
    btn.disabled=true;btn.textContent='Processing 0/'+pairs.length+'…';$('batchLog').textContent='Processing browser files one pair at a time...';
    for(var i=0;i<pairs.length;i++){
        var p=pairs[i],fd=new FormData();
        fd.append('source',p.sourceFile,p.source);
        fd.append('target',p.upscaledFile,p.upscaled);
        try{
            var r=await fetch('/api/merge',{method:'POST',body:fd});
            if(!r.ok){var err=await r.json().catch(function(){return{};});throw new Error(err.error||('HTTP '+r.status));}
            var blob=await r.blob();
            zipItems.push({name:p.outputName,blob:blob});
            ok++;results.push('[OK] '+p.upscaled+' -> '+p.outputName);
        }catch(e){failed++;results.push('[FAILED] '+p.upscaled+' -> '+p.outputName+' · '+e.message);}
        btn.textContent='Processing '+(i+1)+'/'+pairs.length+'…';
        $('batchLog').textContent=results.join('\n');
    }
    if(zipItems.length){
        var zip=await makeZipBlob(zipItems);
        var a=document.createElement('a');
        a.href=URL.createObjectURL(zip);
        a.download='meta-copy-output.zip';
        a.click();
    }
    results.push('', 'Done — OK: '+ok+' | Failed: '+failed+(zipItems.length?' | Downloaded: meta-copy-output.zip':''));
    $('batchLog').textContent=results.join('\n');
    toast('Done — '+ok+' metadata copies'+(failed?' ('+failed+' failed)':''));
    btn.textContent='▶ Copy Metadata & Download Zip';btn.disabled=!pairs.length;
}
function renderScan(d){
    batchState=d;
    var pairs=d.pairs||[];
    $('runBtn').disabled=!pairs.length;
    $('batchSummary').innerHTML='<span class="mc-badge ok">'+pairs.length+' matched</span><span class="mc-badge">'+(d.source_count||0)+' originals</span><span class="mc-badge">'+(d.upscaled_count||0)+' upscales</span>'+(d.unmatched_source&&d.unmatched_source.length?'<span class="mc-badge warn">'+d.unmatched_source.length+' unmatched</span>':'');
    var rows=pairs.map(function(p){var meta=p.has_metadata===null?'queued':(p.has_metadata?'✓ '+esc((p.metadata_keys||[]).join(', ')):'⚠ none found');var cls=p.has_metadata===false?'mc-meta-none':'mc-meta-ok';return '<tr><td>'+esc(p.source)+'</td><td>'+esc(p.upscaled)+'</td><td class="'+cls+'">'+meta+'</td></tr>';}).join('');
    $('matchRows').innerHTML=rows||'<tr><td colspan="3">No matches found.</td></tr>';
    var messages=[];
    (d.unmatched_source||[]).forEach(function(p){messages.push('No match: '+p.name+' — '+p.reason);});
    if((d.unmatched_upscaled||[]).length) messages.push('Unused upscales: '+d.unmatched_upscaled.length);
    $('unmatchedInfo').textContent=messages.join(' · ');
    var log=['Source: '+(d.source_folder||''),'Upscaled: '+(d.upscaled_folder||''),'Suffix regex: '+JSON.stringify(d.suffix_pattern||'')+'$','',pairs.length+' pair(s) matched.'];
    (d.unmatched_source||[]).forEach(function(p){log.push('  ✗ '+p.name+' — '+p.reason);});
    $('batchLog').textContent=log.join('\n');
    if(pairs.length) setHeadStatus(pairs.length+' pair(s) ready to copy.', 'ok');
    else if(d.source_count||d.upscaled_count) setHeadStatus('Scan complete — no matches. Check your suffix regex.', 'warn');
}
$('modeServer').onclick=function(){setBatchMode('server');};
$('modeBrowser').onclick=function(){setBatchMode('browser');};
$('chooseBrowserSource').onclick=function(){chooseBrowserFolder('source');};
$('chooseBrowserUpscaled').onclick=function(){chooseBrowserFolder('upscaled');};
$('browserSourceFiles').onchange=function(){browserFiles.source=pngFiles(this.files);$('browserSourceLabel').value=folderLabel(browserFiles.source)||'No PNG files selected';};
$('browserUpscaledFiles').onchange=function(){browserFiles.upscaled=pngFiles(this.files);$('browserUpscaledLabel').value=folderLabel(browserFiles.upscaled)||'No PNG files selected';};
$('scanBtn').onclick=function(){
    if(batchMode==='browser'){
        try{renderScan(scanBrowserMatches());}
        catch(e){toast(e.message,true);$('batchLog').textContent='Error: '+e.message;}
        return;
    }
    var btn=this;btn.disabled=true;
    fetch('/api/tools/batch/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(batchBody())})
    .then(function(r){return r.ok?r.json():apiError(r);}).then(renderScan)
    .catch(function(e){toast(e.message,true);$('batchLog').textContent='Error: '+e.message;setHeadStatus('Scan failed — '+e.message, 'warn');})
    .finally(function(){btn.disabled=false;});
};
$('runBtn').onclick=function(){
    if(batchMode==='browser'){runBrowserBatch();return;}
    if(!$('batchOutput').value.trim()){toast('Choose an output folder first',true);return;}
    if($('batchDelete').checked&&!confirm('Move BOTH original and upscaled files to the recycle bin after each successful copy?')) return;
    var btn=this;btn.disabled=true;btn.textContent='Processing…';setHeadStatus('Processing…');
    fetch('/api/tools/batch/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({output_folder:$('batchOutput').value.trim(),delete_originals:$('batchDelete').checked,overwrite:$('batchOverwrite').checked})})
    .then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){
        var log=['── Processing ──────────────────────────────'];
        (d.results||[]).forEach(function(item){log.push('['+item.status.toUpperCase()+'] '+item.upscaled+' → '+(item.output||item.source)+' · '+item.message);});
        log.push('', 'Done — OK: '+d.success+' | Skipped: '+d.skipped+' | Failed: '+d.failed);
        $('batchLog').textContent=log.join('\n');
        toast('Done — '+d.success+' metadata copies saved'+(d.failed?' ('+d.failed+' failed)':''));
        setHeadStatus('OK: '+d.success+' · Skipped: '+d.skipped+' · Failed: '+d.failed, d.failed?'warn':'ok');
        if($('batchDelete').checked) $('scanBtn').click();
    })
    .catch(function(e){toast(e.message,true);$('batchLog').textContent='Error: '+e.message;setHeadStatus('Run failed — '+e.message, 'warn');})
    .finally(function(){btn.textContent='▶ Copy Metadata';btn.disabled=!(batchState.pairs||[]).length;});
};
fetch('/api/tools/batch/state').then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){
    $('batchSource').value=d.source_folder||'';$('batchUpscaled').value=d.upscaled_folder||'';$('batchOutput').value=d.output_folder||'';$('batchSuffix').value=d.suffix_pattern||'-gigapixel.*';
    if(!d.trash_available){$('batchDelete').disabled=true;$('batchDelete').parentNode.title='Install send2trash for safe cleanup';}
    if((d.pairs||[]).length)renderScan(d);
}).catch(function(){});

/* Single-file merge */
var srcFileData=null,tgtFileData=null;
function setupDrop(dropId,fileId,side){var drop=$(dropId),input=$(fileId);drop.addEventListener('click',function(){input.click();});drop.addEventListener('dragover',function(e){e.preventDefault();drop.classList.add('dragover');});drop.addEventListener('dragleave',function(){drop.classList.remove('dragover');});drop.addEventListener('drop',function(e){e.preventDefault();drop.classList.remove('dragover');if(e.dataTransfer.files[0])loadFile(e.dataTransfer.files[0],side);});input.addEventListener('change',function(){if(this.files[0])loadFile(this.files[0],side);});}
function loadFile(file,side){if(!file||!file.name.toLowerCase().endsWith('.png')){toast('Only PNG files are supported in this workflow',true);return;}var url=URL.createObjectURL(file);if(side==='src'){srcFileData=file;$('srcDrop').classList.add('has-file');$('srcDrop').innerHTML='<div class="mc-fname">'+esc(file.name)+'</div><div class="mc-fsize">'+(file.size/1024/1024).toFixed(1)+' MB</div>';$('srcPreview').style.display='block';$('srcImg').src=url;var fd=new FormData();fd.append('file',file);fetch('/api/tools/analyze',{method:'POST',body:fd}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(data){var meta=$('srcMeta');meta.style.display='block';if(data&&data.parsed&&data.parsed.prompt){meta.textContent=data.parsed.prompt.slice(0,200)+(data.parsed.prompt.length>200?'...':'');meta.style.color='';}else{meta.textContent='No metadata found in this file';meta.style.color='#ef4444';}}).catch(function(){});}else{tgtFileData=file;$('tgtDrop').classList.add('has-file');$('tgtDrop').innerHTML='<div class="mc-fname">'+esc(file.name)+'</div><div class="mc-fsize">'+(file.size/1024/1024).toFixed(1)+' MB</div>';$('tgtPreview').style.display='block';$('tgtImg').src=url;}$('mergeBtn').disabled=!(srcFileData&&tgtFileData);}
$('mergeBtn').onclick=function(){if(!srcFileData||!tgtFileData)return;var btn=this;btn.disabled=true;$('mergeStatus').textContent='Merging…';var fd=new FormData();fd.append('source',srcFileData);fd.append('target',tgtFileData);fetch('/api/merge',{method:'POST',body:fd}).then(function(r){return r.ok?r.blob():apiError(r);}).then(function(blob){var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=tgtFileData.name.replace(/\.png$/i,'_merged.png');a.click();$('mergeStatus').textContent='Done! Downloaded as '+a.download;}).catch(function(e){$('mergeStatus').textContent='Error: '+e.message;}).finally(function(){btn.disabled=false;});};
setupDrop('srcDrop','srcFile','src');setupDrop('tgtDrop','tgtFile','tgt');
})();
</script>
"""
