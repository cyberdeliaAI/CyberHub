"""Danbooru module — native browser toolkit for CyberHub.

Vanilla JavaScript implementation inspired by the standalone Danbooru Toolkit.
Tag workflows are fully local in the browser. Auto-Tag optionally streams a
user-configured ONNX model and labels JSON through authenticated hub routes and
loads onnxruntime-web only when requested.
"""

import os
from pathlib import Path
from core import Module
from core.server import build_shell


class DanbooruModule(Module):
    name = "Danbooru"
    icon = "\U0001F3F7"  # 🏷
    description = "Danbooru tag lookup, prompt building, checking, random prompts, and optional ONNX auto-tagging."
    order = 35

    settings_schema = {
        "csv_file": {
            "type": "file", "override": True,
            "label": "Tag database CSV override",
            "desc": "Optional override. Default: resources/danbooru/tags.csv",
            "ext": ".csv", "default": "",
            "placeholder": "Leave empty to use resources/danbooru/tags.csv",
        },
        "model_file": {
            "type": "file", "override": True,
            "label": "Auto-Tag ONNX model override",
            "desc": "Optional override. Default: resources/danbooru/model_fp16.onnx",
            "ext": ".onnx", "default": "",
            "placeholder": "Leave empty to use resources/danbooru/model_fp16.onnx",
        },
        "tags_file": {
            "type": "file", "override": True,
            "label": "Auto-Tag labels JSON override",
            "desc": "Optional override. Default: resources/danbooru/tags.json",
            "ext": ".json", "default": "",
            "placeholder": "Leave empty to use resources/danbooru/tags.json",
        },
    }

    RESOURCE_DEFAULTS = {
        "csv_file": ("danbooru", "tags.csv"),
        "model_file": ("danbooru", "model_fp16.onnx"),
        "tags_file": ("danbooru", "tags.json"),
    }

    def routes_get(self):
        return {
            "/danbooru": self._page,
            "/api/danbooru/status": self._api_status,
            "/api/danbooru/guide": self._api_what_is_danbooru,
            "/api/danbooru/what-is-danbooru": self._api_what_is_danbooru,
        }

    def prefix_routes(self):
        return {
            "/danbooru/assets/": self._serve_asset,
        }

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry,
            self.hub.settings,
            active_key="danbooru",
            page_title="Danbooru",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    def _resource(self, key):
        """Resolve a Danbooru resource: optional Settings override, else resources/danbooru/."""
        override = str(self.setting(key, "") or "").strip()
        if override:
            return os.path.abspath(override), "override"
        return os.path.abspath(self.hub.resource_path(*self.RESOURCE_DEFAULTS[key])), "default"

    def _api_status(self, handler, qs):
        csv_file, csv_source = self._resource("csv_file")
        model, model_source = self._resource("model_file")
        tags, tags_source = self._resource("tags_file")
        handler.respond_json({
            "csv_configured": csv_source == "override",
            "csv_ready": os.path.isfile(csv_file),
            "csv_name": os.path.basename(csv_file),
            "csv_path": csv_file, "csv_source": csv_source,
            "model_configured": model_source == "override",
            "model_ready": os.path.isfile(model),
            "model_name": os.path.basename(model),
            "model_path": model, "model_source": model_source,
            "tags_configured": tags_source == "override",
            "tags_ready": os.path.isfile(tags),
            "tags_name": os.path.basename(tags),
            "tags_path": tags, "tags_source": tags_source,
            "auto_tag_ready": bool(os.path.isfile(model) and os.path.isfile(tags)),
        })

    def _api_what_is_danbooru(self, handler, qs):
        path = os.path.abspath(self.hub.resource_path("danbooru", "What_is_Danbooru.md"))
        if not os.path.isfile(path):
            handler.respond_json({"error": "What is Danbooru page not found in resources/danbooru/"}, status=404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            handler.respond_json({"error": str(e)}, status=500)
            return
        handler.respond_json({"name": os.path.basename(path), "markdown": text})

    def _serve_asset(self, handler, remaining_path):
        asset = (remaining_path or "").strip("/").lower()
        keys = {"csv": "csv_file", "model": "model_file", "tags": "tags_file"}
        key = keys.get(asset)
        if not key:
            handler.respond_json({"error": "Unknown Danbooru asset"}, status=404)
            return
        path, source = self._resource(key)
        if not os.path.isfile(path):
            rel = "/".join(self.RESOURCE_DEFAULTS[key])
            handler.respond_json({"error": f"Resource not found. Place it in resources/{rel} or set an override in Settings."}, status=404)
            return
        handler.serve_file(path)


PAGE_BODY = r"""
<style>
.db-page{height:100%;display:flex;flex-direction:column;background:var(--bg-darkest);color:var(--text)}
.db-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 22px;border-bottom:1px solid var(--border);background:var(--bg-panel);flex-shrink:0}
.db-title{display:flex;flex-direction:column;gap:2px}.db-title h1{font-size:16px;color:var(--text-bright);line-height:1.2;margin:0}.db-sub{font-size:11px;color:var(--text-dim)}
.db-actions{display:flex;align-items:center;gap:8px}.db-btn{border:1px solid var(--border);background:var(--bg-card);color:var(--text);font:inherit;font-size:12px;font-weight:400;padding:7px 13px;border-radius:7px;cursor:pointer;transition:.15s}.db-btn:hover:not(:disabled){border-color:var(--accent-dim);color:var(--text-bright);transform:translateY(-1px)}.db-btn:disabled{opacity:.45;cursor:not-allowed}.db-btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:400}.db-btn.danger{color:var(--red)}.db-btn.sm{padding:4px 9px;font-size:11px}
.db-status{display:none;padding:7px 22px;border-bottom:1px solid var(--border);font-size:12px;flex-shrink:0}.db-status.on{display:block}.db-status.ok{color:var(--accent);background:rgba(0,214,143,.08)}.db-status.err{color:var(--red);background:rgba(255,107,107,.08)}
.db-tabs{display:flex;gap:0;padding:0 14px;border-bottom:1px solid var(--border);background:var(--bg-panel);flex-shrink:0}.db-tab{position:relative;border:none;background:transparent;color:var(--text-dim);font:inherit;font-size:13px;font-weight:500;padding:12px 15px;cursor:pointer}.db-tab.active{color:var(--accent)}.db-tab.active:after{content:'';position:absolute;bottom:0;left:16%;right:16%;height:2px;border-radius:2px;background:var(--accent)}
.db-main{flex:1;overflow:auto;padding:18px 22px}.db-view{display:none;max-width:1440px;margin:0 auto}.db-view.active{display:block;animation:dbfade .16s ease-out}@keyframes dbfade{from{opacity:.25;transform:translateY(4px)}to{opacity:1;transform:none}}
.db-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.db-input,.db-select,.db-textarea,.db-num{border:1px solid var(--border);border-radius:7px;background:var(--bg-card);color:var(--text);font:inherit;font-size:13px;padding:8px 11px;outline:none}.db-input:focus,.db-select:focus,.db-textarea:focus,.db-num:focus{border-color:var(--accent)}.db-input{flex:1;min-width:180px}.db-num{width:68px}.db-textarea{width:100%;min-height:95px;resize:vertical;font-family:var(--mono);line-height:1.7}
.db-panel{background:var(--bg-panel);border:1px solid var(--border);border-radius:9px;overflow:hidden}.db-panel-head{display:flex;justify-content:space-between;align-items:center;padding:10px 13px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text-dim);font-weight:600;text-transform:uppercase;letter-spacing:.5px}.db-panel-pad{padding:12px}.db-empty{padding:38px;text-align:center;color:var(--text-dim);font-size:13px}
.db-table{width:100%;border-collapse:collapse}.db-table th{position:sticky;top:0;background:var(--bg-panel);z-index:1;text-align:left;padding:9px 13px;border-bottom:1px solid var(--border);color:var(--text-dim);font-size:11px;text-transform:uppercase;letter-spacing:.7px}.db-table th:last-child,.db-table td:last-child{text-align:right}.db-table td{padding:7px 13px;border-bottom:1px solid rgba(255,255,255,.04);font-size:12px}.db-table tbody tr{cursor:pointer}.db-table tbody tr:hover{background:var(--bg-card)}.db-scroll{max-height:calc(100vh - 270px);overflow:auto}.mono{font-family:var(--mono)}
.cat-general{color:#d1d5db}.cat-character{color:#60a5fa}.cat-copyright{color:#fbbf24}.cat-artist{color:#c084fc}.cat-meta{color:#34d399}.cat-other{color:#9ca3af}
body.theme-light .cat-general{color:#334155}
body.theme-light .cat-character{color:#1d4ed8}
body.theme-light .cat-copyright{color:#92400e}
body.theme-light .cat-artist{color:#7e22ce}
body.theme-light .cat-meta{color:#065f46;font-weight:700}
body.theme-light .cat-other{color:#64748b}
@media (prefers-color-scheme: light){
  body.theme-system .cat-general{color:#334155}
  body.theme-system .cat-character{color:#1d4ed8}
  body.theme-system .cat-copyright{color:#92400e}
  body.theme-system .cat-artist{color:#7e22ce}
  body.theme-system .cat-meta{color:#065f46;font-weight:700}
  body.theme-system .cat-other{color:#64748b}
}
.db-builder{display:grid;grid-template-columns:minmax(320px,1fr) 470px;gap:18px;height:calc(100vh - 190px)}.db-col{min-height:0;display:flex;flex-direction:column;gap:10px}.db-col>.db-input,.db-auto>.db-input{flex:0 0 auto;width:100%;height:38px}.db-builder .db-panel{flex:1;overflow:auto}.chips{display:flex;flex-wrap:wrap;align-content:flex-start;gap:5px;min-height:118px}.chip{display:inline-flex;align-items:center;gap:5px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:4px 9px;font-size:12px;font-family:var(--mono)}.chip button{border:none;background:none;color:var(--text-dim);cursor:pointer;font-size:14px}.output{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-height:58px;font-family:var(--mono);font-size:12px;word-break:break-word;color:var(--text)}
.result-columns{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.tag-wrap{display:flex;flex-wrap:wrap;gap:5px}.known .chip{color:#6ee7b7}.unknown .chip{color:#f87171}
.random-pools td input[type=text]{width:100%;font-family:var(--mono);font-size:11px}.pool-weight{min-width:178px}.pool-weight-select{width:74px;padding:6px 7px}.pool-weight-args{display:inline-flex;align-items:center;gap:4px;margin-left:5px;vertical-align:middle}.pool-weight-num{width:55px!important;padding:6px 6px!important;font-family:var(--mono);font-size:11px}.random-output{font-family:var(--mono);line-height:1.7;word-break:break-word}.db-mini-list{max-height:155px;overflow:auto}.db-mini-item{display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.04);padding:6px 10px;font-size:11px}.db-mini-item .grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tagger-grid{display:grid;grid-template-columns:360px 1fr;gap:18px}.dropzone{height:290px;border:2px dashed var(--border);border-radius:10px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--text-dim);cursor:pointer;overflow:hidden;background:var(--bg-panel)}.dropzone.drag{border-color:var(--accent);background:rgba(0,214,143,.05)}.dropzone img{max-width:100%;max-height:100%;display:block}.tagger-results{min-height:290px}.confidence{display:flex;align-items:center;gap:10px;margin:12px 0;color:var(--text-dim);font-size:12px}.confidence input{accent-color:var(--accent)}
.guide-layout{display:grid;grid-template-columns:minmax(0, 1fr);gap:14px;max-width:980px;margin:0 auto}.guide-doc{line-height:1.75;font-size:13px}.guide-doc h1{font-size:24px;margin:0 0 14px;color:var(--text-bright)}.guide-doc h2{font-size:18px;margin:24px 0 10px;color:var(--text-bright);border-top:1px solid var(--border);padding-top:18px}.guide-doc h3{font-size:15px;margin:18px 0 8px;color:var(--text)}.guide-doc p{margin:0 0 12px}.guide-doc ul,.guide-doc ol{margin:0 0 14px 22px;padding:0}.guide-doc li{margin:5px 0}.guide-doc blockquote{margin:12px 0;padding:10px 14px;border-left:3px solid var(--accent);background:var(--bg-card);color:var(--text-dim)}.guide-doc code{font-family:var(--mono);font-size:12px;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:1px 4px}.guide-doc pre{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto}.guide-doc pre code{border:0;background:transparent;padding:0}.guide-doc table{width:100%;border-collapse:collapse;margin:12px 0 18px}.guide-doc th,.guide-doc td{border:1px solid var(--border);padding:8px 10px;vertical-align:top}.guide-doc th{background:var(--bg-card);color:var(--text-bright);text-align:left}.guide-doc hr{border:0;border-top:1px solid var(--border);margin:22px 0}.guide-doc .loading{color:var(--text-dim);text-align:center;padding:30px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:9px 18px;border-radius:8px;font-weight:400;font-size:12px;z-index:1000;box-shadow:0 8px 32px rgba(0,0,0,.35);display:none}.toast.err{background:var(--red);color:#fff}
.db-auto{position:relative}.db-auto-list{position:absolute;top:100%;left:0;right:0;background:var(--bg-panel);border:1px solid var(--border-light);border-top:0;border-radius:0 0 7px 7px;max-height:240px;overflow:auto;z-index:200;display:none;box-shadow:0 8px 24px rgba(0,0,0,.45)}.db-auto-list.open{display:block}.db-auto-item{padding:7px 11px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12px;border-bottom:1px solid rgba(255,255,255,.03)}.db-auto-item:last-child{border-bottom:0}.db-auto-item:hover,.db-auto-item.focus{background:var(--bg-hover)}.db-auto-item .name{font-family:var(--mono);color:var(--text)}.db-auto-item .meta{color:var(--text-dim);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
@media(max-width:900px){.db-builder,.tagger-grid{display:block;height:auto}.db-builder .db-col+ .db-col,.tagger-results{margin-top:16px}.db-head{flex-direction:column;align-items:stretch}.db-scroll{max-height:420px}}
</style>
<div class="db-page">
  <div class="db-head">
    <div class="db-title"><h1>Danbooru Toolkit</h1><div class="db-sub" id="csvInfo">Checking CSV configuration…</div></div>
    <div class="db-actions">
      <input id="csvFile" type="file" accept=".csv,.txt" hidden>
      <input id="importSettingsFile" type="file" accept=".json" hidden>
      <button class="db-btn" id="reloadCsv">Default / Override CSV</button>
      <button class="db-btn primary" id="openCsv">Temporary CSV</button>
      <button class="db-btn" id="clearTempCsv" title="Discard the temporary CSV and reload the configured one" hidden>×</button>
      <button class="db-btn" id="exportSettings" title="Export pools, favorites and history to a JSON file">Export</button>
      <button class="db-btn" id="importSettings" title="Import pools, favorites and history from a JSON file">Import</button>
    </div>
  </div>
  <div class="db-status" id="status"></div>
  <div class="db-tabs">
    <button class="db-tab active" data-tab="lookup">Lookup</button><button class="db-tab" data-tab="builder">Builder</button><button class="db-tab" data-tab="checker">Checker</button><button class="db-tab" data-tab="random">Random</button><button class="db-tab" data-tab="tagger">Auto-Tag</button><button class="db-tab" data-tab="guide">What is Danbooru?</button>
  </div>
  <div class="db-main">
    <section class="db-view active" id="view-lookup">
      <div class="db-row"><input class="db-input" id="lookupQuery" placeholder="Search tags..."><button class="db-btn primary" id="lookupToBuilder" disabled>→ Builder</button><label><input type="checkbox" id="lookupExact"> Exact</label><select class="db-select" id="lookupSort"><option value="freq">Frequency</option><option value="alpha">Alphabetical</option></select><select class="db-select" id="resultLimit" title="Maximum results"><option value="25">25</option><option value="50" selected>50</option><option value="100">100</option><option value="200">200</option><option value="all">All</option></select></div>
      <div class="db-panel"><div class="db-panel-head"><span>Tag results</span><span id="lookupCount">0</span></div><div class="db-scroll"><table class="db-table"><thead><tr><th>Tag</th><th>Category</th><th>Frequency</th></tr></thead><tbody id="lookupRows"></tbody></table><div class="db-empty" id="lookupEmpty">Add resources/danbooru/tags.csv, set an override in Settings, or load a temporary CSV.</div></div></div>
    </section>
    <section class="db-view" id="view-builder">
      <div class="db-builder"><div class="db-col"><input class="db-input" id="builderSearch" placeholder="Search tags; double-click to add..."><div class="db-panel"><table class="db-table"><thead><tr><th>Tag</th><th>Category</th><th>Frequency</th></tr></thead><tbody id="builderRows"></tbody></table><div class="db-empty" id="builderEmpty">Add resources/danbooru/tags.csv, set an override in Settings, or load a temporary CSV.</div></div></div>
      <div class="db-col"><div class="db-row" style="justify-content:space-between;margin-bottom:0"><strong>Prompt <span id="builderTotal">(0)</span></strong><label><input type="checkbox" id="builderSort"> Auto-sort</label></div><div class="db-auto" style="margin-top:6px"><input class="db-input" id="builderAuto" placeholder="Type a tag to add (autocomplete)..." autocomplete="off"><div class="db-auto-list" id="builderAutoList"></div></div><div class="db-panel db-panel-pad chips" id="builderChips"><div class="db-empty" style="width:100%">Add tags from Lookup or Search.</div></div><div class="output" id="builderOutput">...</div><div class="db-row"><button class="db-btn primary" id="copyBuilder">Copy</button><button class="db-btn" id="sortBuilder">Sort</button><button class="db-btn" id="undoBuilder">Undo</button><button class="db-btn danger" id="clearBuilder">Clear</button></div><div class="db-row"><input class="db-input" id="favoriteName" placeholder="Favorite name..."><button class="db-btn" id="saveFavorite">Save</button></div><div class="db-panel"><div class="db-panel-head"><span>Favorites</span><button class="db-btn sm danger" id="clearFavorites">Clear</button></div><div class="db-mini-list" id="favoriteList"></div></div></div></div>
    </section>
    <section class="db-view" id="view-checker">
      <div class="db-auto" style="margin-bottom:8px"><input class="db-input" id="checkerAuto" placeholder="Quick add: type a tag, Enter to append to prompt..." autocomplete="off"><div class="db-auto-list" id="checkerAutoList"></div></div>
      <label style="display:block;margin-bottom:6px;font-weight:600">Paste your prompt:</label><textarea class="db-textarea" id="checkerText" placeholder="masterpiece, best_quality, 1girl, blue_eyes..."></textarea><div class="db-row" style="margin-top:12px"><button class="db-btn primary" id="checkPrompt">Check</button><button class="db-btn" id="normalizePrompt">Normalize</button><button class="db-btn danger" id="removeUnknown">Remove unknown</button><button class="db-btn" id="copyChecker">Copy</button></div><div class="result-columns"><div><div class="db-panel-head">Known <span id="knownCount">0</span></div><div class="db-panel db-panel-pad tag-wrap known" id="knownTags"></div></div><div><div class="db-panel-head">Unknown <span id="unknownCount">0</span></div><div class="db-panel db-panel-pad tag-wrap unknown" id="unknownTags"></div></div></div><div class="db-panel" style="margin-top:14px"><div class="db-panel-head">Suggestions</div><div class="db-panel-pad" id="suggestions"><span style="color:var(--text-dim)">Run Check to see suggestions for unknown tags.</span></div></div>
    </section>
    <section class="db-view" id="view-random">
      <div class="db-row"><button class="db-btn primary" id="generateRandom">Generate</button><button class="db-btn" id="randomToBuilder">→ Builder</button><button class="db-btn" id="copyRandom">Copy</button><label style="margin-left:auto"><input type="checkbox" id="keepUnderscores"> Keep underscores</label></div><div class="db-panel db-panel-pad random-output" id="randomOutput">Generate a prompt from the pools below.</div><div class="db-panel" style="margin-top:14px"><div class="db-panel-head"><span>Tag pools</span><button class="db-btn sm" id="resetPools">Reset</button></div><table class="db-table random-pools"><thead><tr><th>Pool</th><th>Pick</th><th>Weight</th><th>Lock</th><th>On</th><th>Tags</th></tr></thead><tbody id="poolRows"></tbody></table></div><div class="db-panel" style="margin-top:14px"><div class="db-panel-head"><span>History</span><button class="db-btn sm danger" id="clearHistory">Clear</button></div><div class="db-mini-list" id="historyList"></div></div>
    </section>
    <section class="db-view" id="view-tagger">
      <div class="db-row"><button class="db-btn primary" id="loadModel">Load Auto-Tag model</button><span class="db-sub" id="taggerConfig">Checking configuration…</span></div><div class="tagger-grid"><div><input id="tagImage" type="file" accept="image/*" hidden><div class="dropzone" id="dropzone"><div id="dropPrompt">Drop an image here<br>or click to select</div></div><div class="confidence"><span>Threshold</span><input type="range" min="5" max="95" value="35" id="threshold"><span id="thresholdLabel">0.35</span></div><button class="db-btn primary" id="predict" disabled>Analyze image</button></div><div class="db-panel tagger-results"><div class="db-panel-head"><span>Predicted tags</span><span id="predictionCount">0</span></div><div class="db-panel-pad"><div class="tag-wrap" id="predictionTags"><span style="color:var(--text-dim)">Add the Auto-Tag files to resources/danbooru/ or set overrides in Settings.</span></div><div class="db-row" style="margin-top:16px"><button class="db-btn" id="predToBuilder" disabled>→ Builder</button><button class="db-btn" id="predToChecker" disabled>→ Checker</button></div></div></div></div>
    </section>
    <section class="db-view" id="view-guide">
      <div class="guide-layout">
        <div class="db-panel db-panel-pad" style="line-height:1.75"><h2 style="margin-bottom:12px">What is Danbooru?</h2><p><strong>Lookup</strong> loads <code>resources/danbooru/tags.csv</code> unless a Settings override is set. <strong>Builder</strong> assembles prompts, <strong>Checker</strong> validates tag prompts, <strong>Random</strong> generates prompts from editable pools, and <strong>Auto-Tag</strong> can analyze images when the optional ONNX files are installed.</p><p style="color:var(--text-dim);margin-top:12px">This page is loaded from <code>resources/danbooru/What_is_Danbooru.md</code>.</p></div>
        <div class="db-panel db-panel-pad guide-doc" id="guideDoc"><div class="loading">Loading Danbooru guide...</div></div>
      </div>
    </section>
  </div>
</div><div class="toast" id="toast"></div>
<script>
(function(){
'use strict';
var $=function(id){return document.getElementById(id);};
var CAT={0:'general',1:'artist',2:'unknown',3:'copyright',4:'character',5:'meta',6:'invalid',7:'general',8:'artist',9:'contributor',10:'copyright',11:'character',12:'species',13:'invalid',14:'meta',15:'lore'};
var ORDER=[4,11,3,10,1,8,0,7,12,5,14,15,9,2,6,13];
var DB={items:[],map:{},aliases:{},loaded:false,name:'',source:''};
var configuredCsvReady=false, configuredCsvName='';
var builder=[], builderUndo=[], selected=new Set(), lastRandom='', predictions=[];
var modelSession=null, modelTags=null, inputType='float32', selectedImage=null;
var guideLoaded=false;
var DEFAULT_POOLS=[
 {name:'Quality',tags:['masterpiece','best_quality','high_quality','normal_quality','low_quality','worst_quality','highres','absurdres','ultra_detailed','intricate_details'],count:3,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Subject',tags:['1girl','1boy','2girls','solo','multiple_girls','couple'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Hair',tags:['long_hair','short_hair','blonde_hair','black_hair','brown_hair','blue_hair','red_hair','white_hair','pink_hair','silver_hair','twintails','ponytail','braid','bob_cut','messy_hair'],count:2,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Eyes',tags:['blue_eyes','red_eyes','green_eyes','brown_eyes','purple_eyes','yellow_eyes','heterochromia','closed_eyes'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Expression',tags:['smile','blush','laugh','serious','angry','crying','surprised','embarrassed','grin','pout','open_mouth','closed_mouth'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Outfit',tags:['school_uniform','dress','casual','suit','bikini','kimono','hoodie','armor','maid','nurse','witch','military_uniform','jacket','shirt','skirt'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Pose',tags:['standing','sitting','kneeling','lying','walking','running','jumping','leaning','arms_up','hand_on_hip','crossed_arms','looking_at_viewer','looking_away','from_behind','from_side'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Background',tags:['outdoors','indoors','simple_background','white_background','nature','city','sky','forest','beach','sunset','night','rain','snow','classroom','bedroom'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Angle',tags:['upper_body','full_body','portrait','close-up','cowboy_shot','from_above','from_below','dutch_angle','wide_shot'],count:1,enabled:true,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5},
 {name:'Lighting',tags:['soft_lighting','dramatic_lighting','backlighting','natural_lighting','rim_lighting','sunlight','moonlight','neon_lights','golden_hour','volumetric_lighting'],count:1,enabled:false,locked:false,chosen:null,weight:'none',wFixed:1.2,wMin:0.8,wMax:1.5}
];
var storedPools=loadStore('dbt_vanilla_pools', null);
var pools=Array.isArray(storedPools)?DEFAULT_POOLS.map(function(def,i){return Object.assign({},def,storedPools[i]||{});}):clone(DEFAULT_POOLS);
var favorites=loadStore('dbt_vanilla_favs', []), history=loadStore('dbt_vanilla_hist', []);
function clone(x){return JSON.parse(JSON.stringify(x));}
function loadStore(k, fallback){try{var x=localStorage.getItem(k);return x?JSON.parse(x):fallback;}catch(e){return fallback;}}
function saveStore(k,x){try{localStorage.setItem(k,JSON.stringify(x));}catch(e){}}
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function toast(msg, err){var t=$('toast');t.textContent=msg;t.className='toast'+(err?' err':'');t.style.display='block';clearTimeout(t._hide);t._hide=setTimeout(function(){t.style.display='none';},2500);}
function setStatus(msg, err){var el=$('status');el.textContent=msg;el.className='db-status on '+(err?'err':'ok');}
function copyText(s){if(!s)return;function fb(){try{var t=document.createElement('textarea');t.value=s;t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.select();var k=document.execCommand('copy');document.body.removeChild(t);toast(k?'Copied':'Could not copy',!k);}catch(e){toast('Could not copy',true);}}if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(s).then(function(){toast('Copied');}).catch(fb);}else{fb();}}
function catClass(cat){var c=CAT[cat]||'other';return 'cat-'+(c==='general'||c==='artist'||c==='copyright'||c==='character'||c==='meta'?c:'other');}
function cleanTag(v){return String(v||'').trim().replace(/^\\?\((.+?):[0-9.]+\\?\)$/,'$1').replace(/\s+/g,'_').toLowerCase();}
function splitPrompt(v){return String(v||'').split(/[,\n]+/).map(function(x){return x.trim();}).filter(Boolean);}
function parseCsv(text, name){var lines=text.split(/\r?\n/).filter(function(x){return x.trim()&&!x.trim().startsWith('#');});if(!lines.length)throw new Error('CSV is empty');var first=lines[0], delim=first.indexOf('|')>=0?'|':first.indexOf('\t')>=0?'\t':';';if(delim===';'&&first.indexOf(',')>=0)delim=',';if(first.indexOf(',')>=0)delim=',';function row(line){if(delim!==',')return line.split(delim);var out=[],cur='',q=false;for(var i=0;i<line.length;i++){var ch=line[i];if(ch==='"'){q=!q;}else if(ch===','&&!q){out.push(cur);cur='';}else cur+=ch;}out.push(cur);return out;}
 var values=[], map={}, aliases={};lines.forEach(function(line,ix){var c=row(line);if(!c.length)return;var tag=(c[0]||'').trim();if(!tag||tag.toLowerCase()==='tag'||tag.toLowerCase()==='id')return;var cat=0,freq=0,als=[];if(delim===','&&c.length>=3){cat=parseInt(c[1],10);if(isNaN(cat))cat=0;freq=parseInt(c[2],10)||0;if(c.length>=4)als=(c[3]||'').split(/[, ]+/).filter(Boolean);}else if(delim===','&&c.length===2){freq=parseInt(c[1],10)||0;}else{tag=(c.length>=2?c[1]:c[0]||'').trim();freq=parseInt(c.length>=4?c[3]:c[2])||0;}
 var low=tag.toLowerCase(), item={tag:tag,low:low,cat:cat,freq:freq,aliases:als.map(function(a){return a.toLowerCase();})}; values.push(item);map[low]=item;item.aliases.forEach(function(a){aliases[a]=low;});});
 ['masterpiece','best_quality','high_quality','highres','absurdres'].forEach(function(tag){if(!map[tag]){var it={tag:tag,low:tag,cat:5,freq:999999,aliases:[]};values.push(it);map[tag]=it;}});values.sort(function(a,b){return b.freq-a.freq;});DB={items:values,map:map,aliases:aliases,loaded:true,name:name||'CSV'};updateCsvUi();searchLookup();searchBuilder();}
function itemFor(tag){var k=cleanTag(tag);return DB.map[DB.aliases[k]||k]||null;}
function resultLimit(){var el=$('resultLimit'), value=el?el.value:'50';return value==='all'?Infinity:Math.max(1,parseInt(value,10)||50);}
function search(q, limit){if(!DB.loaded)return [];var low=String(q||'').trim().toLowerCase(), exact=$('lookupExact').checked;if(exact&&low){var one=itemFor(low);return one?[one]:[];}var out=DB.items.filter(function(it){return !low||it.low.indexOf(low)>=0||it.aliases.some(function(a){return a.indexOf(low)>=0;});});if($('lookupSort').value==='alpha')out.sort(function(a,b){return a.low.localeCompare(b.low);});return limit===Infinity?out:out.slice(0,limit||50);}
function updateCsvUi(){var text=DB.loaded?(DB.items.length.toLocaleString()+' tags · '+DB.name):'No CSV tag database loaded';$('csvInfo').textContent=text;var btn=$('clearTempCsv');if(btn){btn.hidden = !DB.loaded || !/Temporary/.test(DB.name||'');}if(DB.loaded)setStatus('Loaded '+DB.items.length.toLocaleString()+' tags from '+DB.name,false);}
function rowHtml(it, select){return '<tr data-tag="'+esc(it.tag)+'"'+(select?' data-select="1"':'')+'><td class="mono '+catClass(it.cat)+'">'+esc(it.tag)+'</td><td>'+esc(CAT[it.cat]||'general')+'</td><td>'+Number(it.freq||0).toLocaleString()+'</td></tr>';}
function searchLookup(){var rows=search($('lookupQuery').value,resultLimit());$('lookupRows').innerHTML=rows.map(function(it){return rowHtml(it,true);}).join('');$('lookupEmpty').style.display=rows.length?'none':'block';$('lookupEmpty').textContent=DB.loaded?'No results':'Add resources/danbooru/tags.csv, set an override in Settings, or load a temporary CSV.';$('lookupCount').textContent=rows.length;$('lookupToBuilder').disabled=!selected.size;}
function searchBuilder(){var rows=search($('builderSearch').value,resultLimit());$('builderRows').innerHTML=rows.map(function(it){return rowHtml(it,false);}).join('');$('builderEmpty').style.display=rows.length?'none':'block';}
function sortTags(list){var ranks={};ORDER.forEach(function(x,i){ranks[x]=i;});return list.slice().sort(function(a,b){var ia=itemFor(a),ib=itemFor(b),ra=ranks[ia?ia.cat:2]||99,rb=ranks[ib?ib.cat:2]||99;if(ra!==rb)return ra-rb;return (ib?ib.freq:0)-(ia?ia.freq:0);});}
function pushBuilderUndo(){builderUndo.push(builder.slice());if(builderUndo.length>50)builderUndo.shift();}
function addBuilder(tag){var canonical=itemFor(tag);tag=canonical?canonical.tag:cleanTag(tag);if(builder.map(cleanTag).indexOf(cleanTag(tag))>=0)return;pushBuilderUndo();builder.push(tag);if($('builderSort').checked)builder=sortTags(builder);renderBuilder();}
function renderBuilder(){var chips=$('builderChips');if(!builder.length){chips.innerHTML='<div class="db-empty" style="width:100%">Add tags from Lookup or Search.</div>';}else{chips.innerHTML=builder.map(function(t,i){var it=itemFor(t);return '<span class="chip '+catClass(it?it.cat:2)+'">'+esc(t)+'<button data-remove="'+i+'">×</button></span>';}).join('');}$('builderTotal').textContent='('+builder.length+')';$('builderOutput').textContent=builder.length?builder.join(', ')+',':'...';renderFavorites();}
function promptCheck(){var tags=splitPrompt($('checkerText').value), known=[], unknown=[];tags.forEach(function(tag){var it=itemFor(tag);(it?known:unknown).push(it?it.tag:cleanTag(tag));});renderCheck(known,unknown);return {known:known,unknown:unknown};}
function renderTagPills(el,tags,kind){el.innerHTML=tags.length?tags.map(function(t){var it=itemFor(t);return '<span class="chip '+catClass(it?it.cat:2)+'">'+esc(t)+'</span>';}).join(''):'<span style="color:var(--text-dim)">None</span>';}
function suggestFor(tag){var hits=suggestCandidates(tag,1);return hits.length?hits[0]:'';}
function suggestCandidates(tag, max){
    max=max||5;
    var normalized=cleanTag(tag).replace(/-/g,'_'), seen={}, out=[];
    function add(v){var it=itemFor(v);v=it?it.tag:cleanTag(v);if(v&&!seen[cleanTag(v)]&&itemFor(v)){seen[cleanTag(v)]=1;out.push(v);}return out.length>=max;}
    if(add(normalized))return out;
    var tokens=normalized.split('_').filter(function(x){return x.length>1;});
    for(var i=0;i<tokens.length-1;i++){if(add(tokens[i]+'_'+tokens[i+1]))return out;}
    for(i=0;i<tokens.length;i++){if(add(tokens[i]))return out;}
    var q=tokens[0]||normalized;
    DB.items.some(function(it){if(it.low.indexOf(q)>=0||it.aliases.some(function(a){return a.indexOf(q)>=0;}))return add(it.tag);return false;});
    return out;
}
function applySuggestionMap(map){var tags=splitPrompt($('checkerText').value).map(function(t){var key=cleanTag(t);return map[key]||t;});$('checkerText').value=tags.join(', ')+(tags.length?',':'');promptCheck();}
function renderCheck(known, unknown){$('knownCount').textContent=known.length;$('unknownCount').textContent=unknown.length;renderTagPills($('knownTags'),known,'known');renderTagPills($('unknownTags'),unknown,'unknown');var canApply=false;var s=unknown.map(function(t){var hits=suggestCandidates(t,4);if(hits.length)canApply=true;var buttons=hits.length?hits.map(function(hit,idx){return '<button class="db-btn sm" data-replace-old="'+esc(t)+'" data-replace-new="'+esc(hit)+'">'+esc(hit)+(idx?'':'')+'</button>';}).join(' '):'<span style="color:var(--text-dim)">no suggestion</span>';return '<div class="db-mini-item"><span class="mono grow" style="color:var(--red)">'+esc(t)+'</span><span>→</span>'+buttons+'</div>';}).join('');if(s&&canApply)s='<div class="db-row" style="justify-content:flex-end;margin-bottom:8px"><button class="db-btn primary sm" id="applyAllSuggestions">Apply All</button></div>'+s;$('suggestions').innerHTML=s||'<span style="color:var(--text-dim)">No unknown tags.</span>';}
function weightControls(p,i){var selected=p.weight||'none';var select='<select class="db-select pool-weight-select" data-pool-weight="'+i+'"><option value="none"'+(selected==='none'?' selected':'')+'>—</option><option value="fixed"'+(selected==='fixed'?' selected':'')+'>Fixed</option><option value="random"'+(selected==='random'?' selected':'')+'>Rand</option></select>';var args='';if(selected==='fixed'){args='<span class="pool-weight-args"><input class="db-num pool-weight-num" type="number" min="0.1" max="3" step="0.05" value="'+Number(p.wFixed||1.2)+'" data-pool-fixed="'+i+'"></span>';}else if(selected==='random'){args='<span class="pool-weight-args"><input class="db-num pool-weight-num" type="number" min="0.1" max="3" step="0.05" value="'+Number(p.wMin||0.8)+'" data-pool-min="'+i+'"><span>–</span><input class="db-num pool-weight-num" type="number" min="0.1" max="3" step="0.05" value="'+Number(p.wMax||1.5)+'" data-pool-max="'+i+'"></span>';}return select+args;}
function renderPools(){var html=pools.map(function(p,i){return '<tr><td>'+esc(p.name)+'</td><td><input class="db-num" style="width:46px" type="number" min="1" max="20" value="'+p.count+'" data-pool-count="'+i+'"></td><td class="pool-weight">'+weightControls(p,i)+'</td><td><input type="checkbox" data-pool-lock="'+i+'" '+(p.locked?'checked':'')+'></td><td><input type="checkbox" data-pool-on="'+i+'" '+(p.enabled?'checked':'')+'></td><td><input class="db-input" type="text" value="'+esc(p.tags.join(', '))+'" data-pool-tags="'+i+'"></td></tr>';}).join('');$('poolRows').innerHTML=html;renderHistory();}
function chooseRandom(arr,n){var a=arr.slice(), out=[];while(a.length&&out.length<n){out.push(a.splice(Math.floor(Math.random()*a.length),1)[0]);}return out;}
function weightedTag(tag,p){var text=$('keepUnderscores').checked?tag:tag.replace(/_/g,' ');text=text.replace(/\(/g,'\\(').replace(/\)/g,'\\)');if(p.weight==='fixed'){var fixed=Number(p.wFixed||1.2).toFixed(2).replace(/0+$/,'').replace(/\.$/,'');return '('+text+':'+fixed+')';}if(p.weight==='random'){var lo=Number(p.wMin||0.8), hi=Number(p.wMax||1.5);if(hi<lo){var tmp=lo;lo=hi;hi=tmp;}return '('+text+':'+(lo+Math.random()*(hi-lo)).toFixed(2)+')';}return text;}
function generateRandom(){var result=[];pools.forEach(function(p){if(!p.enabled||!p.tags.length)return;var tags=(p.locked&&p.chosen)?p.chosen:chooseRandom(p.tags,p.count);if(p.locked)p.chosen=tags.slice();result=result.concat(tags.map(function(t){return weightedTag(t,p);}));});lastRandom=result.join(', ');$('randomOutput').textContent=lastRandom||'No enabled pools.';if(lastRandom){history.unshift({time:new Date().toLocaleString(),prompt:lastRandom});history=history.slice(0,50);saveStore('dbt_vanilla_hist',history);renderHistory();toast('Generated');}saveStore('dbt_vanilla_pools',pools);}
function renderHistory(){var html=history.map(function(h,i){return '<div class="db-mini-item"><span style="color:var(--text-dim);min-width:120px">'+esc(h.time)+'</span><span class="mono grow">'+esc(h.prompt)+'</span><button class="db-btn sm" data-history-builder="'+i+'">Load</button><button class="db-btn sm" data-history-copy="'+i+'">Copy</button></div>';}).join('');$('historyList').innerHTML=html||'<div class="db-empty">No history yet.</div>';}
function renderFavorites(){var html=favorites.map(function(f,i){return '<div class="db-mini-item"><strong style="min-width:75px">'+esc(f.name)+'</strong><span class="mono grow">'+esc(f.prompt)+'</span><button class="db-btn sm" data-fav-load="'+i+'">Load</button><button class="db-btn sm" data-fav-copy="'+i+'">Copy</button><button class="db-btn sm danger" data-fav-remove="'+i+'">×</button></div>';}).join('');$('favoriteList').innerHTML=html||'<div class="db-empty" style="padding:18px">No favorites yet.</div>';}
function inlineMarkdown(s){return esc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>');}
function renderMarkdown(md){
    var lines=String(md||'').replace(/\r/g,'').split('\n'), html=[], para=[], list=null, quote=[], code=[], table=[];
    function flushPara(){if(para.length){html.push('<p>'+inlineMarkdown(para.join(' '))+'</p>');para=[];}}
    function flushList(){if(list){html.push('<'+list.type+'>'+list.items.map(function(x){return '<li>'+inlineMarkdown(x)+'</li>';}).join('')+'</'+list.type+'>');list=null;}}
    function flushQuote(){if(quote.length){html.push('<blockquote>'+quote.map(inlineMarkdown).join('<br>')+'</blockquote>');quote=[];}}
    function flushCode(){if(code.length){html.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');code=[];}}
    function flushTable(){if(table.length){var rows=table.filter(function(r){return !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(r);}).map(function(r){return r.replace(/^\||\|$/g,'').split('|').map(function(c){return c.trim();});});if(rows.length){var head=rows.shift();html.push('<table><thead><tr>'+head.map(function(c){return '<th>'+inlineMarkdown(c)+'</th>';}).join('')+'</tr></thead><tbody>'+rows.map(function(row){return '<tr>'+row.map(function(c){return '<td>'+inlineMarkdown(c)+'</td>';}).join('')+'</tr>';}).join('')+'</tbody></table>');}table=[];}}
    lines.forEach(function(line){
        if(code.length || /^```/.test(line)){if(/^```/.test(line)){if(code.length)flushCode();else code.push('');return;}code.push(line);return;}
        if(/^\s*\|.+\|\s*$/.test(line)){flushPara();flushList();flushQuote();table.push(line);return;} else flushTable();
        if(!line.trim()){flushPara();flushList();flushQuote();return;}
        var h=line.match(/^(#{1,4})\s+(.+)$/); if(h){flushPara();flushList();flushQuote();html.push('<h'+h[1].length+'>'+inlineMarkdown(h[2])+'</h'+h[1].length+'>');return;}
        if(/^\s*---+\s*$/.test(line)){flushPara();flushList();flushQuote();html.push('<hr>');return;}
        var q=line.match(/^>\s?(.*)$/); if(q){flushPara();flushList();quote.push(q[1]);return;}
        var ul=line.match(/^\s*[-*]\s+(.+)$/); if(ul){flushPara();flushQuote();if(!list||list.type!=='ul')list={type:'ul',items:[]};list.items.push(ul[1]);return;}
        var ol=line.match(/^\s*\d+\.\s+(.+)$/); if(ol){flushPara();flushQuote();if(!list||list.type!=='ol')list={type:'ol',items:[]};list.items.push(ol[1]);return;}
        flushList();flushQuote();para.push(line.trim());
    });
    flushCode();flushTable();flushPara();flushList();flushQuote();
    return html.join('');
}
async function loadGuide(){if(guideLoaded)return;guideLoaded=true;try{var r=await fetch('/api/danbooru/what-is-danbooru');var d=await r.json();if(!r.ok||d.error)throw new Error(d.error||'Could not load What is Danbooru');$('guideDoc').innerHTML=renderMarkdown(d.markdown||'');}catch(e){$('guideDoc').innerHTML='<div class="db-empty">'+esc(e.message)+'</div>';}}
function switchTab(tab){document.querySelectorAll('.db-tab').forEach(function(b){b.classList.toggle('active',b.dataset.tab===tab);});document.querySelectorAll('.db-view').forEach(function(v){v.classList.toggle('active',v.id==='view-'+tab);});if(tab==='tagger')loadTaggerStatus();if(tab==='guide')loadGuide();}
async function getDanbooruStatus(){var r=await fetch('/api/danbooru/status');if(!r.ok)throw new Error('Could not read Danbooru configuration.');return r.json();}
async function loadConfiguredCsv(){try{var d=await getDanbooruStatus();configuredCsvReady=!!d.csv_ready;configuredCsvName=d.csv_name||'';if(!configuredCsvReady){DB={items:[],map:{},aliases:{},loaded:false,name:''};updateCsvUi();searchLookup();searchBuilder();setStatus('Add resources/danbooru/tags.csv, set an override in Settings, or load a temporary CSV.',true);return;}var r=await fetch('/danbooru/assets/csv');if(!r.ok)throw new Error('Configured CSV file not available');var txt=await r.text();parseCsv(txt,(configuredCsvName||'CSV')+' · '+(d.csv_source==='override'?'Override':'resources/danbooru'));}catch(e){setStatus(e.message,true);toast(e.message,true);}}
async function loadTaggerStatus(){try{var d=await getDanbooruStatus();$('taggerConfig').textContent=d.auto_tag_ready?((d.model_source==='override'?'Override: ':'resources/danbooru: ')+d.model_name+' + '+d.tags_name):'Add model_fp16.onnx and tags.json to resources/danbooru/ or set overrides in Settings.';$('loadModel').disabled=!d.auto_tag_ready;}catch(e){$('taggerConfig').textContent='Could not read Auto-Tag configuration.';}}
function loadOrt(){return new Promise(function(resolve,reject){if(window.ort){resolve(window.ort);return;}function tryLoad(url,fallback){var s=document.createElement('script');s.src=url;s.onload=function(){resolve(window.ort);};s.onerror=function(){if(fallback){tryLoad(fallback,null);}else{reject(new Error('Could not load ONNX Runtime. Place ort.min.js in resources/danbooru/ or ensure internet access.'));}};document.head.appendChild(s);}tryLoad('/onnx/ort.min.js','https://cdn.jsdelivr.net/npm/onnxruntime-web@1.21.0/dist/ort.min.js');});}
async function loadModel(){try{$('loadModel').disabled=true;$('taggerConfig').textContent='Loading ONNX Runtime and model…';await loadOrt();modelTags=await fetch('/danbooru/assets/tags').then(function(r){if(!r.ok)throw new Error('tags.json not available');return r.json();});modelSession=await ort.InferenceSession.create('/danbooru/assets/model',{executionProviders:['wasm']});inputType='float32';try{var zero=new ort.Tensor('float32',new Float32Array(3*224*224),[1,3,224,224]);await modelSession.run({[modelSession.inputNames[0]]:zero});}catch(probe){if(/float16|expected.*float16/i.test(String(probe.message||probe)))inputType='float16';else throw probe;}$('taggerConfig').textContent='Model ready · '+modelTags.length.toLocaleString()+' output tags · '+inputType; $('predict').disabled=!selectedImage;toast('Auto-Tag model loaded');}catch(e){$('taggerConfig').textContent=e.message;toast(e.message,true);$('loadModel').disabled=false;}}
function floatToHalf(val){var f=new Float32Array([val]), bits=new Uint32Array(f.buffer)[0], sign=(bits>>16)&0x8000, exp=((bits>>23)&255)-127+15, mant=bits&0x7fffff;if(exp<=0)return sign;if(exp>=31)return sign|0x7c00;return sign|(exp<<10)|(mant>>13);}
function imageTensor(img){var c=document.createElement('canvas');c.width=224;c.height=224;var x=c.getContext('2d'), sc=Math.min(224/img.naturalWidth,224/img.naturalHeight),w=Math.round(img.naturalWidth*sc),h=Math.round(img.naturalHeight*sc);x.fillStyle='#000';x.fillRect(0,0,224,224);x.drawImage(img,Math.round((224-w)/2),Math.round((224-h)/2),w,h);var p=x.getImageData(0,0,224,224).data,total=3*224*224,i;if(inputType==='float16'){var half=new Uint16Array(total);for(i=0;i<224*224;i++){half[i]=floatToHalf(p[i*4]/255);half[224*224+i]=floatToHalf(p[i*4+1]/255);half[2*224*224+i]=floatToHalf(p[i*4+2]/255);}return new ort.Tensor('float16',half,[1,3,224,224]);}var out=new Float32Array(total);for(i=0;i<224*224;i++){out[i]=p[i*4]/255;out[224*224+i]=p[i*4+1]/255;out[2*224*224+i]=p[i*4+2]/255;}return new ort.Tensor('float32',out,[1,3,224,224]);}
async function predict(){if(!modelSession||!selectedImage)return;try{$('predict').disabled=true;var input=imageTensor(selectedImage), outputs=await modelSession.run({[modelSession.inputNames[0]]:input}), data=outputs[modelSession.outputNames[0]].data, threshold=Number($('threshold').value)/100;predictions=[];for(var i=0;i<data.length;i++){if(data[i]>=threshold)predictions.push({tag:modelTags[i],score:Number(data[i])});}predictions.sort(function(a,b){return b.score-a.score;});renderPredictions();}catch(e){toast('Auto-Tag failed: '+e.message,true);}finally{$('predict').disabled=false;}}
function renderPredictions(){var t=Number($('threshold').value)/100;var visible=predictions.filter(function(p){return p.score>=t;});$('predictionCount').textContent=visible.length;$('predictionTags').innerHTML=visible.length?visible.map(function(p){return '<span class="chip">'+esc(p.tag)+' <small>'+p.score.toFixed(2)+'</small></span>';}).join(''):'<span style="color:var(--text-dim)">No tags above threshold.</span>';$('predToBuilder').disabled=!visible.length;$('predToChecker').disabled=!visible.length;}
// Reusable typeahead autocomplete — used by Builder and Checker.
// Caller supplies the input element id, the list element id and an onPick(tag, item) callback.
function attachAutocomplete(inputId, listId, onPick) {
    var input=$(inputId), list=$(listId), items=[], focusIdx=-1, debounceId=null;
    function close(){ list.classList.remove('open'); list.innerHTML=''; items=[]; focusIdx=-1; }
    function render(){
        if(!items.length){ close(); return; }
        list.innerHTML = items.map(function(it,i){
            return '<div class="db-auto-item'+(i===focusIdx?' focus':'')+'" data-idx="'+i+'"><span class="name">'+esc(it.tag)+'</span><span class="meta">'+esc(it.catLabel||'')+' · '+(it.freq||0).toLocaleString()+'</span></div>';
        }).join('');
        list.classList.add('open');
        if(focusIdx>=0){
            var el=list.children[focusIdx];
            if(el && el.scrollIntoView) el.scrollIntoView({block:'nearest'});
        }
    }
    function refresh(){
        var q=input.value.trim();
        if(q.length<1 || !DB.loaded){ close(); return; }
        items=search(q,12); focusIdx=items.length?0:-1; render();
    }
    input.addEventListener('input', function(){
        clearTimeout(debounceId);
        debounceId=setTimeout(refresh,80);
    });
    input.addEventListener('focus', refresh);
    input.addEventListener('blur', function(){ setTimeout(close,150); }); // delay to allow click
    input.addEventListener('keydown', function(e){
        if(!list.classList.contains('open')){
            if(e.key==='Enter' && DB.loaded && input.value.trim()){
                // Fall back to direct text add even without suggestions
                e.preventDefault();
                onPick(input.value.trim(), null);
                input.value=''; close();
            }
            return;
        }
        if(e.key==='ArrowDown'){ e.preventDefault(); focusIdx=Math.min(items.length-1, focusIdx+1); render(); }
        else if(e.key==='ArrowUp'){ e.preventDefault(); focusIdx=Math.max(0, focusIdx-1); render(); }
        else if(e.key==='Enter'){
            e.preventDefault();
            if(focusIdx>=0){ var pick=items[focusIdx]; onPick(pick.tag, pick); input.value=''; close(); }
            else if(input.value.trim()){ onPick(input.value.trim(), null); input.value=''; close(); }
        }
        else if(e.key==='Escape'){ close(); }
    });
    list.addEventListener('mousedown', function(e){
        var row=e.target.closest('.db-auto-item');
        if(!row) return;
        e.preventDefault();
        var pick=items[Number(row.dataset.idx)];
        if(pick){ onPick(pick.tag, pick); input.value=''; close(); }
    });
}
attachAutocomplete('builderAuto','builderAutoList', function(tag){
    addBuilder(tag);
});
attachAutocomplete('checkerAuto','checkerAutoList', function(tag){
    var ta=$('checkerText'), v=ta.value.trim();
    var sep = (!v) ? '' : (v.endsWith(',') ? ' ' : ', ');
    ta.value = v + sep + tag + ',';
    promptCheck();
});

// Tabs and CSV
Array.prototype.forEach.call(document.querySelectorAll('.db-tab'),function(b){b.onclick=function(){switchTab(b.dataset.tab);};});$('reloadCsv').onclick=function(){$('clearTempCsv').hidden=true;loadConfiguredCsv();};$('openCsv').onclick=function(){$('csvFile').click();};$('csvFile').onchange=function(){var file=this.files[0];if(!file)return;var fr=new FileReader();fr.onload=function(){try{parseCsv(fr.result,file.name+' · Temporary');setStatus('Temporary CSV loaded for this browser session. Use Settings for the default database.',false);$('clearTempCsv').hidden=false;}catch(e){toast(e.message,true);}};fr.readAsText(file);this.value='';};$('clearTempCsv').onclick=function(){$('clearTempCsv').hidden=true;loadConfiguredCsv();toast('Temporary CSV discarded');};

// Settings export / import (pools, favorites, history, custom CSV ignored — too big and re-fetchable)
var SETTINGS_KEYS={pools:'dbt_vanilla_pools',favorites:'dbt_vanilla_favs',history:'dbt_vanilla_hist'};
$('exportSettings').onclick=function(){
    var dump={version:1,exported_at:new Date().toISOString()};
    Object.keys(SETTINGS_KEYS).forEach(function(k){
        try { dump[k] = JSON.parse(localStorage.getItem(SETTINGS_KEYS[k]) || 'null'); }
        catch(e) { dump[k] = null; }
    });
    var blob=new Blob([JSON.stringify(dump,null,2)],{type:'application/json'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download='danbooru-settings.json';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){URL.revokeObjectURL(a.href);},2000);
    toast('Settings exported');
};
$('importSettings').onclick=function(){$('importSettingsFile').click();};
$('importSettingsFile').onchange=function(){
    var file=this.files[0]; this.value='';
    if(!file)return;
    var fr=new FileReader();
    fr.onload=function(){
        try {
            var data=JSON.parse(fr.result);
            if(!data||typeof data!=='object'){throw new Error('File is not a valid settings dump');}
            var imported=[];
            Object.keys(SETTINGS_KEYS).forEach(function(k){
                if(data[k] !== undefined && data[k] !== null){
                    localStorage.setItem(SETTINGS_KEYS[k], JSON.stringify(data[k]));
                    imported.push(k);
                }
            });
            if(!imported.length){toast('Nothing to import (file had no pools / favorites / history)',true);return;}
            // Reload in-memory state from localStorage
            pools=loadStore('dbt_vanilla_pools',clone(DEFAULT_POOLS));
            favorites=loadStore('dbt_vanilla_favs',[]);
            history=loadStore('dbt_vanilla_hist',[]);
            renderPools(); renderFavorites();
            toast('Imported: '+imported.join(', '));
        } catch(e) {
            toast('Import failed: '+e.message,true);
        }
    };
    fr.readAsText(file);
};
$('lookupQuery').oninput=searchLookup;$('lookupExact').onchange=searchLookup;$('lookupSort').onchange=searchLookup;$('resultLimit').onchange=function(){searchLookup();searchBuilder();};$('lookupRows').onclick=function(e){var tr=e.target.closest('tr[data-tag]');if(!tr)return;var tag=tr.dataset.tag;if(selected.has(tag))selected.delete(tag);else selected.add(tag);tr.style.background=selected.has(tag)?'var(--bg-card)':'';$('lookupToBuilder').disabled=!selected.size;};$('lookupRows').ondblclick=function(e){var tr=e.target.closest('tr[data-tag]');if(tr){addBuilder(tr.dataset.tag);switchTab('builder');}};$('lookupToBuilder').onclick=function(){selected.forEach(addBuilder);selected.clear();switchTab('builder');searchLookup();};
$('builderSearch').oninput=searchBuilder;$('builderRows').ondblclick=function(e){var tr=e.target.closest('tr[data-tag]');if(tr)addBuilder(tr.dataset.tag);};$('builderChips').onclick=function(e){var b=e.target.closest('[data-remove]');if(b){pushBuilderUndo();builder.splice(Number(b.dataset.remove),1);renderBuilder();}};$('copyBuilder').onclick=function(){copyText(builder.length?builder.join(', ')+',':'');};$('sortBuilder').onclick=function(){pushBuilderUndo();builder=sortTags(builder);renderBuilder();};$('undoBuilder').onclick=function(){if(builderUndo.length){builder=builderUndo.pop();renderBuilder();}};$('clearBuilder').onclick=function(){if(builder.length){pushBuilderUndo();builder=[];renderBuilder();}};$('builderSort').onchange=function(){if(this.checked){pushBuilderUndo();builder=sortTags(builder);renderBuilder();}};$('saveFavorite').onclick=function(){var n=$('favoriteName').value.trim();if(!n||!builder.length){toast('Add a name and prompt first',true);return;}favorites.push({name:n,prompt:builder.join(', ')});saveStore('dbt_vanilla_favs',favorites);$('favoriteName').value='';renderFavorites();};$('clearFavorites').onclick=function(){if(favorites.length&&confirm('Clear all favorites?')){favorites=[];saveStore('dbt_vanilla_favs',favorites);renderFavorites();}};$('favoriteList').onclick=function(e){var l=e.target.closest('[data-fav-load]'),c=e.target.closest('[data-fav-copy]'),r=e.target.closest('[data-fav-remove]');if(l){pushBuilderUndo();builder=splitPrompt(favorites[Number(l.dataset.favLoad)].prompt);renderBuilder();}if(c){copyText(favorites[Number(c.dataset.favCopy)].prompt);}if(r){favorites.splice(Number(r.dataset.favRemove),1);saveStore('dbt_vanilla_favs',favorites);renderFavorites();}};
$('checkPrompt').onclick=promptCheck;$('normalizePrompt').onclick=function(){var tags=splitPrompt($('checkerText').value).map(function(t){var it=itemFor(t);return it?it.tag:cleanTag(t);});$('checkerText').value=tags.join(', ')+(tags.length?',':'');promptCheck();};$('removeUnknown').onclick=function(){var r=promptCheck();$('checkerText').value=r.known.join(', ')+(r.known.length?',':'');promptCheck();};$('copyChecker').onclick=function(){copyText($('checkerText').value);};$('suggestions').onclick=function(e){var all=e.target.closest('#applyAllSuggestions');if(all){var map={};splitPrompt($('checkerText').value).forEach(function(t){var hit=suggestFor(t);if(hit)map[cleanTag(t)]=hit;});applySuggestionMap(map);return;}var b=e.target.closest('[data-replace-old]');if(!b)return;var old=b.dataset.replaceOld,nw=b.dataset.replaceNew, tags=splitPrompt($('checkerText').value).map(function(t){return cleanTag(t)===cleanTag(old)?nw:t;});$('checkerText').value=tags.join(', ')+',';promptCheck();};
$('generateRandom').onclick=generateRandom;$('copyRandom').onclick=function(){copyText(lastRandom);};$('randomToBuilder').onclick=function(){if(lastRandom){pushBuilderUndo();builder=splitPrompt(lastRandom).map(function(t){return t.replace(/ /g,'_');});renderBuilder();switchTab('builder');}};$('resetPools').onclick=function(){pools=clone(DEFAULT_POOLS);saveStore('dbt_vanilla_pools',pools);renderPools();};$('clearHistory').onclick=function(){if(history.length&&confirm('Clear all history?')){history=[];saveStore('dbt_vanilla_hist',history);renderHistory();}};$('poolRows').onchange=function(e){var el=e.target, i, rerender=false;if(el.dataset.poolCount!==undefined){i=Number(el.dataset.poolCount);pools[i].count=Math.max(1,Number(el.value)||1);}if(el.dataset.poolWeight!==undefined){i=Number(el.dataset.poolWeight);pools[i].weight=el.value;rerender=true;}if(el.dataset.poolFixed!==undefined){i=Number(el.dataset.poolFixed);pools[i].wFixed=Math.max(0.1,Math.min(3,Number(el.value)||1.2));}if(el.dataset.poolMin!==undefined){i=Number(el.dataset.poolMin);pools[i].wMin=Math.max(0.1,Math.min(3,Number(el.value)||0.8));}if(el.dataset.poolMax!==undefined){i=Number(el.dataset.poolMax);pools[i].wMax=Math.max(0.1,Math.min(3,Number(el.value)||1.5));}if(el.dataset.poolLock!==undefined){i=Number(el.dataset.poolLock);pools[i].locked=el.checked;if(!el.checked)pools[i].chosen=null;}if(el.dataset.poolOn!==undefined){i=Number(el.dataset.poolOn);pools[i].enabled=el.checked;}if(el.dataset.poolTags!==undefined){i=Number(el.dataset.poolTags);pools[i].tags=el.value.split(',').map(function(t){return t.trim();}).filter(Boolean);pools[i].chosen=null;}saveStore('dbt_vanilla_pools',pools);if(rerender)renderPools();};$('historyList').onclick=function(e){var b=e.target.closest('[data-history-builder]'),c=e.target.closest('[data-history-copy]');if(b){pushBuilderUndo();builder=splitPrompt(history[Number(b.dataset.historyBuilder)].prompt).map(function(t){return t.replace(/ /g,'_');});renderBuilder();switchTab('builder');}if(c){copyText(history[Number(c.dataset.historyCopy)].prompt);}};
$('loadModel').onclick=loadModel;$('dropzone').onclick=function(){$('tagImage').click();};$('dropzone').ondragover=function(e){e.preventDefault();this.classList.add('drag');};$('dropzone').ondragleave=function(){this.classList.remove('drag');};$('dropzone').ondrop=function(e){e.preventDefault();this.classList.remove('drag');setTagImage(e.dataTransfer.files[0]);};$('tagImage').onchange=function(){setTagImage(this.files[0]);};function setTagImage(file){if(!file||!file.type.startsWith('image/'))return;var url=URL.createObjectURL(file), img=new Image();img.onload=function(){selectedImage=img;$('dropzone').innerHTML='<img src="'+url+'" alt="Selected image">';$('predict').disabled=!modelSession;};img.src=url;}$('predict').onclick=predict;$('threshold').oninput=function(){$('thresholdLabel').textContent=(Number(this.value)/100).toFixed(2);renderPredictions();};$('predToBuilder').onclick=function(){predictions.filter(function(p){return p.score>=Number($('threshold').value)/100;}).forEach(function(p){addBuilder(p.tag);});switchTab('builder');};$('predToChecker').onclick=function(){$('checkerText').value=predictions.filter(function(p){return p.score>=Number($('threshold').value)/100;}).map(function(p){return p.tag;}).join(', ')+',';switchTab('checker');promptCheck();};
try{localStorage.removeItem('dbt_vanilla_csv');localStorage.removeItem('dbt_vanilla_csv_name');}catch(e){}updateCsvUi();renderBuilder();renderPools();loadConfiguredCsv();loadTaggerStatus();
})();
</script>
"""
