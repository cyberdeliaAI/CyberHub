"""Cropper module — browser-based dataset crop workflow for CyberHub.

Adapted from the standalone CyberCropper desktop tool. Folder choices are
session-scoped in the Cropper screen, while stable export defaults remain in
Settings. Crops are rendered in-browser and saved server-side with Pillow.
"""

import json
import os
from pathlib import Path

from core import Module
from core.server import build_shell

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
    LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except ImportError:
    HAS_PIL = False
    Image = None
    ImageOps = None
    LANCZOS = None

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
EXPORT_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def safe_resolve(root, rel_path):
    """Resolve a relative path below root and block path traversal."""
    try:
        root_abs = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root_abs, rel_path))
        if full != root_abs and not full.startswith(root_abs + os.sep):
            return None
        return full
    except (OSError, ValueError):
        return None


def next_output_path(directory, stem, suffix):
    candidate = directory / f"{stem}_crop{suffix}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{stem}_crop_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


class CropperModule(Module):
    name = "Cropper"
    icon = "\u2702"
    description = "Crop and resize image datasets with aspect-ratio presets and batch navigation."
    order = 25

    settings_schema = {
        "default_aspect": {
            "type": "select", "label": "Default aspect ratio",
            "options": ["Free", "1:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9"],
            "default": "1:1",
        },
        "default_max_size": {
            "type": "number", "label": "Default maximum size",
            "desc": "Longest exported side in pixels; output is never upscaled.",
            "default": 1024, "min": 64, "max": 16384,
        },
        "default_format": {
            "type": "select", "label": "Default export format",
            "options": ["PNG", "JPEG", "WEBP"], "default": "PNG",
        },
        "default_quality": {
            "type": "number", "label": "Default JPEG/WEBP quality",
            "default": 90, "min": 10, "max": 100,
        },
    }

    def __init__(self, hub):
        super().__init__(hub)
        # Folder choices persist across restarts (saved to settings.json on pick).
        self._session_input_folder = self.setting("input_folder", "")
        self._session_output_folder = self.setting("output_folder", "")

    def routes_get(self):
        return {
            "/cropper": self._page,
            "/api/cropper/state": self._api_state,
        }

    def routes_post(self):
        return {
            "/api/cropper/session": self._api_session,
            "/api/cropper/save": self._api_save,
        }

    def prefix_routes(self):
        return {
            "/cropper/image/": self._serve_image,
        }

    def _input_folder(self):
        folder = self._session_input_folder.strip()
        return os.path.abspath(folder) if folder and os.path.isdir(folder) else ""

    def _output_folder(self, input_folder):
        folder = self._session_output_folder.strip()
        return Path(os.path.abspath(folder)) if folder else Path(input_folder) / "crops"

    def _config(self):
        return {
            "aspect": self.setting("default_aspect", "1:1"),
            "max_size": int(self.setting("default_max_size", 1024) or 1024),
            "format": self.setting("default_format", "PNG") or "PNG",
            "quality": int(self.setting("default_quality", 90) or 90),
        }

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="cropper", page_title="Cropper",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    def _api_session(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        input_folder = str(data.get("input_folder") or "").strip()
        output_folder = str(data.get("output_folder") or "").strip()
        if not input_folder:
            handler.respond_json({"error": "Choose an input folder first"}, status=400)
            return
        input_folder = os.path.abspath(input_folder)
        if not os.path.isdir(input_folder):
            handler.respond_json({"error": "Input folder not found"}, status=404)
            return
        if output_folder:
            output_folder = os.path.abspath(output_folder)
            if os.path.exists(output_folder) and not os.path.isdir(output_folder):
                handler.respond_json({"error": "Output path is not a folder"}, status=400)
                return
        self._session_input_folder = input_folder
        self._session_output_folder = output_folder
        self.hub.settings.set_module_setting(self.key(), "input_folder", input_folder)
        self.hub.settings.set_module_setting(self.key(), "output_folder", output_folder)
        self._write_state(handler)

    def _list_files(self, root):
        files = []
        for path in sorted(Path(root).iterdir(), key=lambda p: p.name.lower()):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                files.append(path.name)
        return files

    def _write_state(self, handler):
        root = self._input_folder()
        if not root:
            handler.respond_json({
                "configured": False, "files": [], "config": self._config(),
                "input_folder": self._session_input_folder,
                "output_folder": self._session_output_folder,
            })
            return
        try:
            files = self._list_files(root)
        except PermissionError:
            handler.respond_json({"error": "Permission denied reading input folder"}, status=403)
            return
        handler.respond_json({
            "configured": True,
            "input_folder": root,
            "output_folder": self._session_output_folder,
            "effective_output_folder": str(self._output_folder(root)),
            "files": files,
            "config": self._config(),
        })

    def _api_state(self, handler, qs):
        self._write_state(handler)

    def _serve_image(self, handler, rel_path):
        root = self._input_folder()
        if not root:
            handler.send_error(503)
            return
        full = safe_resolve(root, rel_path)
        if not full or not os.path.isfile(full) or Path(full).suffix.lower() not in SUPPORTED_EXTS:
            handler.send_error(404)
            return
        handler.serve_file(full)

    def _api_save(self, handler, content_len, content_type):
        if not HAS_PIL:
            handler.respond_json({"error": "Pillow is required for Cropper"}, status=503)
            return
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        root = self._input_folder()
        if not root:
            handler.respond_json({"error": "Choose an input folder in Cropper first"}, status=503)
            return
        rel_path = str(data.get("file") or "").strip()
        full = safe_resolve(root, rel_path)
        if not full or not os.path.isfile(full) or Path(full).suffix.lower() not in SUPPORTED_EXTS:
            handler.respond_json({"error": "Source image not found"}, status=404)
            return
        crop_box = data.get("crop")
        if not isinstance(crop_box, dict):
            handler.respond_json({"error": "Missing crop selection"}, status=400)
            return
        try:
            x = int(round(float(crop_box.get("x"))))
            y = int(round(float(crop_box.get("y"))))
            w = int(round(float(crop_box.get("w"))))
            h = int(round(float(crop_box.get("h"))))
            rotation = int(data.get("rotation", 0)) % 360
            if rotation not in (0, 90, 180, 270):
                raise ValueError
            max_size = max(1, min(16384, int(data.get("max_size", 1024))))
            quality = max(10, min(100, int(data.get("quality", 90))))
        except (TypeError, ValueError):
            handler.respond_json({"error": "Invalid crop/export values"}, status=400)
            return
        if w < 2 or h < 2:
            handler.respond_json({"error": "Crop selection is too small"}, status=400)
            return
        fmt = str(data.get("format") or "PNG").upper()
        if fmt not in EXPORT_FORMATS:
            handler.respond_json({"error": "Unsupported export format"}, status=400)
            return
        try:
            with Image.open(full) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                if rotation:
                    image = image.rotate(-rotation, expand=True)
                if bool(data.get("flip_h", False)):
                    image = ImageOps.mirror(image)
                if bool(data.get("flip_v", False)):
                    image = ImageOps.flip(image)
                iw, ih = image.size
                if x < 0 or y < 0 or x + w > iw or y + h > ih:
                    handler.respond_json({"error": "Crop selection lies outside the image"}, status=400)
                    return
                result = image.crop((x, y, x + w, y + h))
                if max(result.size) > max_size:
                    ratio = max_size / float(max(result.size))
                    result = result.resize(
                        (max(1, round(result.width * ratio)), max(1, round(result.height * ratio))),
                        LANCZOS,
                    )
                out_dir = self._output_folder(root)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = next_output_path(out_dir, Path(full).stem, EXPORT_FORMATS[fmt])
                save_args = {"quality": quality} if fmt in ("JPEG", "WEBP") else {}
                # Preserve original metadata
                pnginfo = None
                exif_data = None
                try:
                    if hasattr(source, "info") and fmt == "PNG":
                        from PIL import PngImagePlugin
                        pnginfo = PngImagePlugin.PngInfo()
                        for k, v in source.info.items():
                            if isinstance(v, str):
                                pnginfo.add_text(k, v)
                        save_args["pnginfo"] = pnginfo
                    if hasattr(source, "info") and "exif" in source.info and fmt in ("JPEG", "WEBP"):
                        save_args["exif"] = source.info["exif"]
                except Exception:
                    pass  # metadata preservation is best-effort
                result.save(out_path, format=fmt, **save_args)
        except PermissionError:
            handler.respond_json({"error": "Permission denied writing crop"}, status=403)
            return
        except OSError as exc:
            handler.respond_json({"error": f"Could not save crop: {exc}"}, status=400)
            return
        handler.respond_json({
            "ok": True, "filename": out_path.name, "path": str(out_path),
            "width": result.width, "height": result.height,
        })


PAGE_BODY = r"""
<style>
.cropper { height:calc(100vh - 48px); display:flex; flex-direction:column; background:var(--bg-darkest); }
.crop-toolbar, .crop-footer { flex:0 0 auto; background:var(--bg-panel); border-bottom:1px solid var(--border); padding:10px 14px; display:flex; align-items:center; gap:14px; }
.crop-toolbar { flex-wrap:wrap; }
.crop-footer { border-bottom:0; border-top:1px solid var(--border); justify-content:space-between; padding:9px 14px; }
.crop-group { display:flex; align-items:center; gap:7px; border-right:1px solid var(--border-light); padding-right:14px; }
.crop-group:last-child { border-right:0; }
.crop-label { color:var(--text-dim); font-size:10px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
.crop-select, .crop-input { height:30px; background:var(--bg-card); border:1px solid var(--border-light); color:var(--text-bright); border-radius:6px; padding:0 9px; font:inherit; font-size:12px; }
.crop-input { width:66px; font-family:var(--mono); }
.crop-path { width:225px; font-family:var(--mono); font-size:11px; }
.crop-btn { height:31px; padding:0 12px; background:var(--bg-card); border:1px solid var(--border-light); color:var(--text); border-radius:6px; font:inherit; font-size:12px; cursor:pointer; }
.crop-btn:hover { border-color:var(--accent); color:var(--text-bright); }
.crop-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:400; }
.crop-btn.primary:hover { background:var(--accent-dim); }
.crop-btn.green { background:var(--green); border-color:var(--green); color:#fff; font-weight:400; }
.crop-btn:disabled { opacity:.4; cursor:not-allowed; }
.crop-main { flex:1 1 auto; min-height:0; padding:12px; }
.crop-stage { position:relative; height:100%; border:1px solid var(--border); border-radius:10px; background:var(--bg-card); overflow:hidden; }
#cropCanvas { display:block; width:100%; height:100%; cursor:crosshair; }
.crop-empty { position:absolute; inset:0; display:flex; flex-direction:column; gap:12px; align-items:center; justify-content:center; color:var(--text-dim); text-align:center; }
.crop-empty .symbol { font-size:46px; opacity:.45; }
.crop-empty strong { font-size:15px; color:var(--text); font-weight:500; }
.crop-note { font-size:11px; color:var(--text-dim); }
.crop-status { display:flex; gap:16px; align-items:center; font-family:var(--mono); font-size:11px; color:var(--text-dim); min-width:0; }
#cropFilename { color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:330px; }
.crop-actions { display:flex; gap:7px; align-items:center; }
.crop-toast { position:absolute; left:50%; top:18px; transform:translateX(-50%); padding:9px 14px; background:var(--bg-panel); border:1px solid var(--green); border-radius:7px; color:var(--green); font-size:12px; font-weight:500; box-shadow:0 10px 26px rgba(0,0,0,.4); display:none; z-index:5; }
.crop-toast.error { color:var(--red); border-color:var(--red); }
.crop-check { display:flex; align-items:center; gap:5px; color:var(--text); font-size:12px; cursor:pointer; }
.crop-quality { display:none; align-items:center; gap:5px; }
#qualityOutput { min-width:24px; color:var(--text-dim); font-family:var(--mono); font-size:11px; }
.browse-overlay { position:fixed; inset:0; background:rgba(0,0,0,.65); z-index:5000; display:none; align-items:center; justify-content:center; }
.browse-overlay.open { display:flex; }
.browse-dialog { background:var(--bg-panel); border:1px solid var(--border); border-radius:10px; width:590px; max-height:72vh; display:flex; flex-direction:column; box-shadow:0 12px 48px rgba(0,0,0,.7); }
.browse-header, .browse-footer { padding:12px 16px; display:flex; align-items:center; justify-content:space-between; gap:8px; }
.browse-header { border-bottom:1px solid var(--border); }
.browse-footer { border-top:1px solid var(--border); }
.browse-header h3 { font-size:14px; font-weight:600; margin:0; color:var(--text-bright); }
.browse-close { background:none; border:0; color:var(--text-dim); font-size:20px; cursor:pointer; }
.browse-crumb { padding:8px 16px; border-bottom:1px solid var(--border); font:11px var(--mono); color:var(--accent); overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.browse-body { overflow-y:auto; min-height:230px; max-height:420px; padding:5px 0; }
.browse-entry { display:flex; gap:10px; align-items:center; padding:8px 16px; cursor:pointer; color:var(--text); font-size:12px; }
.browse-entry:hover, .browse-entry.selected { background:var(--bg-hover); color:var(--accent); }
.browse-path-display { font:10px var(--mono); color:var(--text-dim); min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
@media (max-width:1180px) { .crop-path { width:180px; } .crop-footer { flex-wrap:wrap; } }
</style>
<div class="cropper">
  <div class="crop-toolbar">
    <div class="crop-group"><span class="crop-label">Folders</span>
      <input id="inputFolder" class="crop-input crop-path" placeholder="Input folder..."><button id="browseInput" class="crop-btn">Browse</button>
      <input id="outputFolder" class="crop-input crop-path" placeholder="Output folder (blank = /crops)..."><button id="browseOutput" class="crop-btn">Browse</button>
      <button id="loadFolder" class="crop-btn primary">Load</button>
    </div>
    <div class="crop-group"><span class="crop-label">Crop</span>
      <select id="aspect" class="crop-select"><option>Free</option><option>1:1</option><option>2:3</option><option>3:2</option><option>4:5</option><option>5:4</option><option>9:16</option><option>16:9</option></select>
      <input id="customAspect" class="crop-input" placeholder="W:H"><button id="addAspect" class="crop-btn">+</button>
    </div>
    <div class="crop-group"><span class="crop-label">Export</span>
      <label style="font-size:12px;color:var(--text-dim)">Max</label><input id="maxSize" type="number" min="64" max="16384" class="crop-input" value="1024">
      <select id="format" class="crop-select"><option>PNG</option><option>JPEG</option><option>WEBP</option></select>
      <div id="qualityBox" class="crop-quality"><input id="quality" type="range" min="10" max="100" value="90"><span id="qualityOutput">90</span></div>
    </div>
    <div class="crop-group"><span class="crop-label">View</span><label class="crop-check"><input id="grid" type="checkbox"> Grid</label><button id="fit" class="crop-btn">Fit</button><button id="undo" class="crop-btn" title="Undo (Ctrl+Z)">Undo</button><span id="zoom" style="font:11px var(--mono);color:var(--text-dim)">100%</span></div>
  </div>
  <div class="crop-main"><div class="crop-stage" id="stage">
    <canvas id="cropCanvas"></canvas>
    <div class="crop-empty" id="empty"><div class="symbol">&#x2702;</div><strong>Cropper</strong><div id="emptyText">Choose an input folder above to begin.</div><div class="crop-note">Folder choices last only for this Hub session.</div></div>
    <div id="toast" class="crop-toast"></div>
  </div></div>
  <div class="crop-footer">
    <div class="crop-actions">
      <button id="prev" class="crop-btn">&#x25C0; Prev</button><button id="next" class="crop-btn">Next &#x25B6;</button>
      <button id="rotateL" class="crop-btn" title="Rotate left">&#x21B6;</button><button id="rotateR" class="crop-btn" title="Rotate right">&#x21B7;</button>
      <button id="flipH" class="crop-btn" title="Flip horizontal">&#x21D4;</button><button id="flipV" class="crop-btn" title="Flip vertical">&#x21D5;</button>
    </div>
    <div class="crop-status"><span id="counter"></span><span id="cropFilename"></span><span id="dimensions"></span><span id="selection"></span></div>
    <div class="crop-actions"><button id="save" class="crop-btn primary">Save Crop</button><button id="saveNext" class="crop-btn green">Save + Next</button></div>
  </div>
</div>
<div id="browseOverlay" class="browse-overlay"><div class="browse-dialog">
  <div class="browse-header"><h3>Select folder</h3><button id="browseClose" class="browse-close">&times;</button></div>
  <div id="browseCrumb" class="browse-crumb"></div><div id="browseBody" class="browse-body"></div>
  <div class="browse-footer"><span id="browsePath" class="browse-path-display"></span><button id="browseSelect" class="crop-btn primary">Select folder</button></div>
</div></div>
<script>
(function() {
  var files=[], index=-1, image=null, transformed=null, rotation=0, flipH=false, flipV=false;
  var scale=1, offset={x:0,y:0}, selection=null, drag=null, grid=false, undoStack=[];
  var canvas=document.getElementById('cropCanvas'), ctx=canvas.getContext('2d'), stage=document.getElementById('stage');
  var HANDLE=10, browseTarget=null, browseCurrent='';
  function $(id){ return document.getElementById(id); }
  function cloneSelection(s){ return s ? {x:s.x,y:s.y,w:s.w,h:s.h} : null; }
  function snapshot(){ return {selection:cloneSelection(selection),rotation:rotation,flipH:flipH,flipV:flipV}; }
  function pushUndo(){ undoStack.push(snapshot()); if(undoStack.length>60) undoStack.shift(); updateUndo(); }
  function updateUndo(){ $('undo').disabled=!undoStack.length; }
  function undo(){ if(!undoStack.length)return; var s=undoStack.pop(); selection=s.selection; rotation=s.rotation; flipH=s.flipH; flipV=s.flipV; buildTransformed(); fit(); updateInfo(); updateUndo(); }
  function toast(msg, error){ var t=$('toast'); t.textContent=msg; t.className='crop-toast'+(error?' error':''); t.style.display='block'; clearTimeout(t._timer); t._timer=setTimeout(function(){t.style.display='none';},3000); }
  function apiError(r){ return r.json().catch(function(){return {};}).then(function(d){throw new Error(d.error || ('HTTP '+r.status));}); }
  function resize(){ canvas.width=stage.clientWidth; canvas.height=stage.clientHeight; if(image && !drag) fit(); else render(); }
  window.addEventListener('resize', resize);
  function applyConfig(s){ var c=s.config||{}; $('aspect').value=c.aspect||'1:1'; $('maxSize').value=c.max_size||1024; $('format').value=c.format||'PNG'; $('quality').value=c.quality||90; $('qualityOutput').textContent=$('quality').value; updateQuality(); }
  function applyState(s, preserveConfig){
    if(!preserveConfig) applyConfig(s);
    $('inputFolder').value=s.input_folder||''; $('outputFolder').value=s.output_folder||'';
    files=s.files||[]; image=null; transformed=null; selection=null; drag=null; undoStack=[]; updateUndo();
    if(!s.configured){ index=-1; $('empty').style.display='flex'; $('emptyText').textContent='Choose an input folder above to begin.'; setControls(); render(); return; }
    if(!files.length){ index=-1; $('empty').style.display='flex'; $('emptyText').textContent='No supported images found in this input folder.'; setControls(); render(); return; }
    index=0; loadCurrent();
  }
  fetch('/api/cropper/state').then(function(r){return r.ok?r.json():apiError(r);}).then(function(s){applyState(s,false);}).catch(function(e){toast(e.message,true);});
  function loadSession(){ var body={input_folder:$('inputFolder').value.trim(),output_folder:$('outputFolder').value.trim()}; fetch('/api/cropper/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(s){applyState(s,true); toast('Folder loaded');}).catch(function(e){toast(e.message,true);}); }
  function setControls(){ var has=!!image; ['prev','next','rotateL','rotateR','flipH','flipV','save','saveNext','fit'].forEach(function(id){$(id).disabled=!has;}); $('prev').disabled=!has||index<=0; $('next').disabled=!has||index>=files.length-1; updateUndo(); }
  function loadCurrent(){ selection=null; rotation=0; flipH=false; flipV=false; undoStack=[]; updateUndo(); var img=new Image(); img.onload=function(){image=img; buildTransformed(); $('empty').style.display='none'; setControls(); fit(); updateInfo();}; img.onerror=function(){toast('Could not load image',true);}; img.src='/cropper/image/'+encodeURIComponent(files[index])+'?v='+Date.now(); }
  function buildTransformed(){ if(!image)return; var rotated=document.createElement('canvas'), rctx=rotated.getContext('2d'), turn=(rotation%360+360)%360; if(turn===90||turn===270){rotated.width=image.height;rotated.height=image.width;}else{rotated.width=image.width;rotated.height=image.height;} rctx.translate(rotated.width/2,rotated.height/2);rctx.rotate(turn*Math.PI/180);rctx.drawImage(image,-image.width/2,-image.height/2); var out=document.createElement('canvas'),octx=out.getContext('2d');out.width=rotated.width;out.height=rotated.height;octx.translate(flipH?out.width:0,flipV?out.height:0);octx.scale(flipH?-1:1,flipV?-1:1);octx.drawImage(rotated,0,0);transformed=out; }
  function fit(){ if(!transformed)return;scale=Math.min((canvas.width-30)/transformed.width,(canvas.height-30)/transformed.height);scale=Math.max(.02,scale);offset.x=(canvas.width-transformed.width*scale)/2;offset.y=(canvas.height-transformed.height*scale)/2;render(); }
  function screenRect(){ if(!selection)return null; return {x:offset.x+selection.x*scale,y:offset.y+selection.y*scale,w:selection.w*scale,h:selection.h*scale}; }
  function handles(){ var r=screenRect();if(!r)return{};var mx=r.x+r.w/2,my=r.y+r.h/2;return {nw:[r.x,r.y],n:[mx,r.y],ne:[r.x+r.w,r.y],e:[r.x+r.w,my],se:[r.x+r.w,r.y+r.h],s:[mx,r.y+r.h],sw:[r.x,r.y+r.h],w:[r.x,my]}; }
  function render(){ ctx.clearRect(0,0,canvas.width,canvas.height);if(!transformed)return;ctx.drawImage(transformed,offset.x,offset.y,transformed.width*scale,transformed.height*scale);$('zoom').textContent=Math.round(scale*100)+'%';if(!selection){updateInfo();return;}var r=screenRect();ctx.save();ctx.fillStyle='rgba(0,0,0,.55)';ctx.beginPath();ctx.rect(offset.x,offset.y,transformed.width*scale,transformed.height*scale);ctx.rect(r.x,r.y,r.w,r.h);ctx.fill('evenodd');ctx.strokeStyle='#4a9eff';ctx.lineWidth=2;ctx.strokeRect(r.x,r.y,r.w,r.h);if(grid){ctx.strokeStyle='rgba(255,255,255,.7)';ctx.setLineDash([4,5]);for(var i=1;i<3;i++){ctx.beginPath();ctx.moveTo(r.x+r.w*i/3,r.y);ctx.lineTo(r.x+r.w*i/3,r.y+r.h);ctx.stroke();ctx.beginPath();ctx.moveTo(r.x,r.y+r.h*i/3);ctx.lineTo(r.x+r.w,r.y+r.h*i/3);ctx.stroke();}ctx.setLineDash([]);}var hs=handles();Object.keys(hs).forEach(function(k){var p=hs[k];ctx.fillStyle='#4a9eff';ctx.strokeStyle='#ffffff';ctx.lineWidth=1;ctx.fillRect(p[0]-HANDLE/2,p[1]-HANDLE/2,HANDLE,HANDLE);ctx.strokeRect(p[0]-HANDLE/2,p[1]-HANDLE/2,HANDLE,HANDLE);});ctx.restore();updateInfo(); }
  function imagePoint(e){ var r=canvas.getBoundingClientRect();return{x:Math.max(0,Math.min(transformed.width,(e.clientX-r.left-offset.x)/scale)),y:Math.max(0,Math.min(transformed.height,(e.clientY-r.top-offset.y)/scale))}; }
  function canvasPoint(e){var r=canvas.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};}
  function ratio(){var v=$('aspect').value;if(v==='Free')return null;var p=v.split(':').map(Number);return p.length===2&&p[0]>0&&p[1]>0?p[0]/p[1]:null;}
  function normalized(x,y,w,h){if(w<0){x+=w;w=-w;}if(h<0){y+=h;h=-h;}x=Math.max(0,Math.min(transformed.width-1,x));y=Math.max(0,Math.min(transformed.height-1,y));w=Math.max(1,Math.min(transformed.width-x,w));h=Math.max(1,Math.min(transformed.height-y,h));return{x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h)};}
  function makeSelection(a,b){var x=Math.min(a.x,b.x),y=Math.min(a.y,b.y),w=Math.abs(b.x-a.x),h=Math.abs(b.y-a.y),ar=ratio();if(ar){if(w/Math.max(1,h)>ar)w=h*ar;else h=w/ar;}return normalized(x,y,Math.min(w,transformed.width-x),Math.min(h,transformed.height-y));}
  function hitHandle(e){var p=canvasPoint(e), hs=handles(), tol=HANDLE;for(var k in hs){if(Math.abs(p.x-hs[k][0])<=tol&&Math.abs(p.y-hs[k][1])<=tol)return k;}return null;}
  function resizeSelection(mode,p,orig){var left=orig.x,top=orig.y,right=orig.x+orig.w,bottom=orig.y+orig.h,anchor;if(mode==='nw'||mode==='n'||mode==='w')anchor={x:right,y:bottom};else if(mode==='ne'||mode==='e')anchor={x:left,y:bottom};else if(mode==='sw'||mode==='s')anchor={x:right,y:top};else anchor={x:left,y:top};return makeSelection(anchor,p);}
  canvas.addEventListener('mousedown',function(e){if(!transformed||e.button!==0)return;var p=imagePoint(e), handle=selection?hitHandle(e):null;if(handle){pushUndo();drag={mode:'resize',handle:handle,orig:cloneSelection(selection)};}else if(selection&&p.x>=selection.x&&p.x<=selection.x+selection.w&&p.y>=selection.y&&p.y<=selection.y+selection.h){pushUndo();drag={mode:'move',start:p,orig:cloneSelection(selection)};}else{if(selection)pushUndo();drag={mode:'new',start:p};selection=null;}render();});
  canvas.addEventListener('mousemove',function(e){if(!drag||!transformed)return;var p=imagePoint(e);if(drag.mode==='new')selection=makeSelection(drag.start,p);else if(drag.mode==='move'){var dx=p.x-drag.start.x,dy=p.y-drag.start.y;selection={x:Math.max(0,Math.min(transformed.width-drag.orig.w,Math.round(drag.orig.x+dx))),y:Math.max(0,Math.min(transformed.height-drag.orig.h,Math.round(drag.orig.y+dy))),w:drag.orig.w,h:drag.orig.h};}else if(drag.mode==='resize')selection=resizeSelection(drag.handle,p,drag.orig);render();});
  window.addEventListener('mouseup',function(){drag=null;});
  canvas.addEventListener('wheel',function(e){if(!transformed)return;e.preventDefault();var r=canvas.getBoundingClientRect(),cx=e.clientX-r.left,cy=e.clientY-r.top,old=scale;scale=Math.max(.02,Math.min(10,scale*(e.deltaY<0?1.1:.9)));offset.x=cx-(cx-offset.x)*scale/old;offset.y=cy-(cy-offset.y)*scale/old;render();},{passive:false});
  function updateInfo(){if(!image){$('counter').textContent='';$('cropFilename').textContent='';$('dimensions').textContent='';$('selection').textContent='';return;}$('counter').textContent=(index+1)+' / '+files.length;$('cropFilename').textContent=files[index];$('dimensions').textContent=transformed.width+' × '+transformed.height;$('selection').textContent=selection?'Crop '+selection.w+' × '+selection.h:'';}
  function transform(action){if(!image)return;pushUndo();if(action==='left')rotation=(rotation+270)%360;if(action==='right')rotation=(rotation+90)%360;if(action==='h')flipH=!flipH;if(action==='v')flipV=!flipV;selection=null;buildTransformed();fit();updateInfo();}
  function nudge(dx,dy){if(!selection||!transformed)return;pushUndo();selection.x=Math.max(0,Math.min(transformed.width-selection.w,selection.x+dx));selection.y=Math.max(0,Math.min(transformed.height-selection.h,selection.y+dy));render();}
  function updateQuality(){$('qualityBox').style.display=($('format').value==='PNG')?'none':'flex';}
  function save(next){if(!selection||selection.w<2||selection.h<2){toast('Draw a crop selection first',true);return;}var body={file:files[index],crop:selection,rotation:rotation,flip_h:flipH,flip_v:flipV,max_size:Number($('maxSize').value)||1024,format:$('format').value,quality:Number($('quality').value)||90};$('save').disabled=true;$('saveNext').disabled=true;fetch('/api/cropper/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){toast('Saved '+d.filename+' ('+d.width+'×'+d.height+')');if(next&&index<files.length-1){index++;loadCurrent();}else setControls();}).catch(function(e){toast(e.message,true);setControls();});}
  function browseOpen(target){browseTarget=target;$('browseOverlay').classList.add('open');browseLoad($(target).value.trim());}
  function browseLoad(path){fetch('/api/browse?path='+encodeURIComponent(path||'')).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){
    browseCurrent=d.path||'';
    $('browsePath').textContent=d.display||browseCurrent||'Drives';
    var crumbs=['<span data-browse-path="">&#x1F4BB;</span>'];
    (d.crumbs||[]).forEach(function(c){crumbs.push('<span class="sep">&#x203A;</span><span data-browse-path="'+escapeAttr(c.path)+'">'+escapeHtml(c.label)+'</span>');});
    $('browseCrumb').innerHTML=crumbs.join(' ');
    var html='';
    if(d.parent!==null&&d.parent!==undefined){html+='<div class="browse-entry" data-browse-path="'+escapeAttr(d.parent)+'">&#x21A9; <span>..</span></div>';}
    (d.dirs||[]).forEach(function(dir){html+='<div class="browse-entry" data-browse-path="'+escapeAttr(dir.path)+'">&#x1F4C1; <span>'+escapeHtml(dir.name)+'</span></div>';});
    if(!html)html='<div class="browse-entry">No subfolders</div>';
    $('browseBody').innerHTML=html;
  }).catch(function(e){toast(e.message,true);});}
  function escapeHtml(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function escapeAttr(v){return escapeHtml(v);}
  // Custom aspect ratios — stored in localStorage so they survive reloads.
  var CUSTOM_ASPECTS_KEY = 'cdhub_cropper_custom_aspects';
  function loadCustomAspects() {
    try { return JSON.parse(localStorage.getItem(CUSTOM_ASPECTS_KEY) || '[]') || []; }
    catch(e) { return []; }
  }
  function saveCustomAspects(list) {
    try { localStorage.setItem(CUSTOM_ASPECTS_KEY, JSON.stringify(list)); }
    catch(e) { /* quota / private mode */ }
  }
  function renderCustomAspectOptions() {
    var sel=$('aspect'), current=sel.value;
    // Drop existing custom options (those tagged data-custom)
    Array.prototype.slice.call(sel.querySelectorAll('option[data-custom="1"]')).forEach(function(o){ sel.removeChild(o); });
    loadCustomAspects().forEach(function(v) {
      var o=document.createElement('option');
      o.value=v; o.textContent=v; o.setAttribute('data-custom','1');
      sel.appendChild(o);
    });
    if (Array.prototype.slice.call(sel.options).some(function(o){return o.value===current;})) sel.value=current;
  }
  $('addAspect').onclick=function(){
    var v=$('customAspect').value.trim();
    if(!/^\d+\s*:\s*\d+$/.test(v)){toast('Use W:H, for example 7:9',true);return;}
    v=v.replace(/\s/g,'');
    var list=loadCustomAspects();
    if (list.indexOf(v)<0) { list.push(v); saveCustomAspects(list); }
    renderCustomAspectOptions();
    $('aspect').value=v; $('customAspect').value='';
    if(selection)pushUndo(); selection=null; render();
    toast('Saved '+v);
  };
  $('customAspect').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();$('addAspect').click();}});
  // Remove a custom aspect by typing it in the input and pressing the - button
  // (no UI for managing the full list — keep it simple; users can clear localStorage)
  renderCustomAspectOptions();
  $('browseInput').onclick=function(){browseOpen('inputFolder');};$('browseOutput').onclick=function(){browseOpen('outputFolder');};$('browseClose').onclick=function(){$('browseOverlay').classList.remove('open');};$('browseSelect').onclick=function(){if(browseTarget&&browseCurrent){$(browseTarget).value=browseCurrent;}$('browseOverlay').classList.remove('open');};$('browseOverlay').onclick=function(e){if(e.target===this){this.classList.remove('open');return;}var nav=e.target.closest('[data-browse-path]');if(nav){browseLoad(nav.getAttribute('data-browse-path'));}};
  $('loadFolder').onclick=loadSession;$('inputFolder').addEventListener('keydown',function(e){if(e.key==='Enter')loadSession();});$('outputFolder').addEventListener('keydown',function(e){if(e.key==='Enter')loadSession();});
  $('prev').onclick=function(){if(index>0){index--;loadCurrent();}};$('next').onclick=function(){if(index<files.length-1){index++;loadCurrent();}};$('fit').onclick=fit;$('undo').onclick=undo;$('rotateL').onclick=function(){transform('left');};$('rotateR').onclick=function(){transform('right');};$('flipH').onclick=function(){transform('h');};$('flipV').onclick=function(){transform('v');};$('save').onclick=function(){save(false);};$('saveNext').onclick=function(){save(true);};$('grid').onchange=function(){grid=this.checked;render();};$('format').onchange=updateQuality;$('quality').oninput=function(){$('qualityOutput').textContent=this.value;};$('aspect').onchange=function(){if(selection)pushUndo();selection=null;render();};
  document.addEventListener('keydown',function(e){if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;var k=e.key.toLowerCase();if((e.ctrlKey||e.metaKey)&&k==='z'){e.preventDefault();undo();return;}if(k==='a'){if(index>0){index--;loadCurrent();}}else if(k==='f'){if(index<files.length-1){index++;loadCurrent();}}else if(k==='s'){save(false);}else if(k==='d'){save(true);}else if(k==='r'){transform('right');}else if(k==='l'){transform('left');}else if(k==='h'){transform('h');}else if(k==='v'){transform('v');}else if(k==='g'){$('grid').checked=!$('grid').checked;grid=$('grid').checked;render();}else if(e.key==='Escape'&&selection){pushUndo();selection=null;render();}else if(e.key.indexOf('Arrow')===0&&selection){e.preventDefault();var amount=e.shiftKey?10:1;if(e.key==='ArrowLeft')nudge(-amount,0);if(e.key==='ArrowRight')nudge(amount,0);if(e.key==='ArrowUp')nudge(0,-amount);if(e.key==='ArrowDown')nudge(0,amount);}});
  resize();setControls();updateUndo();
})();
</script>
"""
