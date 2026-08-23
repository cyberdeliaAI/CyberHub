"""Civitai Grabber — Download images from Civitai by username, model, tag or version."""

import json, os, subprocess, sys, threading, time
from core import Module
from core.server import build_shell


class CivitaiGrabberModule(Module):
    name = "Civitai Grabber"
    version = "1.1.1"
    icon = "\U0001F4E5"
    description = "Download images from Civitai by username, model ID, tag or version."
    order = 40
    settings_schema = {}

    def key(self):
        return "grabber"

    def __init__(self, hub):
        super().__init__(hub)
        self._job = None  # {"proc": Popen, "log": [], "started": float, "args": dict, "done": bool}
        self._lock = threading.Lock()

    def _script_path(self):
        return os.path.join(self.hub.resources_dir, "civitai-grabber", "civit_image_downloader.py")

    def _gallery_folders(self):
        folders = self.hub.settings.data.get("modules", {}).get("gallery", {}).get("folders", [])
        if isinstance(folders, list):
            return [f for f in folders if isinstance(f, str)]
        return []

    def routes_get(self):
        return {
            "/grabber": self._page,
            "/api/grabber/status": self._api_status,
            "/api/grabber/folders": self._api_folders,
        }

    def routes_post(self):
        return {
            "/api/grabber/start": self._api_start,
            "/api/grabber/stop": self._api_stop,
        }

    def _page(self, handler, qs):
        handler.respond_html(build_shell(self.hub.registry, self.hub.settings,
            active_key="grabber", page_title="Civitai Grabber", body_html=PAGE_BODY))

    def _api_folders(self, handler, qs):
        handler.respond_json({"folders": self._gallery_folders()})

    def _api_status(self, handler, qs):
        with self._lock:
            if not self._job:
                handler.respond_json({"running": False})
                return
            # Check if process finished
            if self._job["proc"].poll() is not None:
                self._job["done"] = True
            handler.respond_json({
                "running": not self._job["done"],
                "done": self._job["done"],
                "exit_code": self._job["proc"].returncode,
                "log": self._job["log"][-80:],
                "args": self._job["args"],
                "elapsed": int(time.time() - self._job["started"]),
            })

    def _api_start(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return

        with self._lock:
            if self._job and not self._job["done"]:
                handler.respond_json({"error": "A download is already running"}, status=409); return

        script = self._script_path()
        if not os.path.exists(script):
            handler.respond_json({"error": "civit_image_downloader.py not found in resources/civitai-grabber/"}, status=404); return

        # Only username (1) and model version ID (4) are enabled in this hub.
        mode = str(data.get("mode", "1"))
        if mode not in ("1", "4"):
            handler.respond_json({"error": "Unsupported mode — only username and model version ID are enabled."}, status=400); return
        output_dir = data.get("output_dir", "").strip()
        if not output_dir:
            handler.respond_json({"error": "No output folder selected"}, status=400); return

        # Build CLI args
        args = [sys.executable, script, "--mode", mode, "--output_dir", output_dir]

        quality = data.get("quality", "1")
        args += ["--quality", str(quality)]

        if mode == "1":
            username = data.get("username", "").strip()
            if not username:
                handler.respond_json({"error": "Username required"}, status=400); return
            args += ["--username", username]
            # Deep scan retrieves images beyond Civitai's ~50K pagination cap (username only).
            if data.get("deep_scan"):
                args += ["--deep_scan"]
        else:  # mode == "4"
            version_id = data.get("version_id", "").strip()
            if not version_id:
                handler.respond_json({"error": "Model version ID required"}, status=400); return
            args += ["--model_version_id", version_id]

        # Filter tags apply to both username and model-version modes.
        filter_tags = data.get("filter_tags", "").strip()
        if filter_tags:
            args += ["--filter_tags", filter_tags]

        max_images = data.get("max_images", "").strip()
        if max_images:
            args += ["--max_images", max_images]

        # Videos are always skipped — this hub's grabber is image-only.
        args += ["--no_videos"]

        redownload = data.get("redownload", False)
        if redownload:
            args += ["--redownload", "1"]

        semaphore = data.get("semaphore", "5").strip()
        args += ["--semaphore_limit", semaphore]

        # Start subprocess
        job = {
            "proc": None, "log": [], "started": time.time(), "done": False,
            "args": {"mode": mode, "output_dir": output_dir},
        }

        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,  # never let the script block on interactive prompts
                text=True, bufsize=1, cwd=os.path.dirname(script),
            )
            job["proc"] = proc
        except Exception as e:
            handler.respond_json({"error": f"Failed to start: {e}"}, status=500); return

        with self._lock:
            self._job = job

        # Background thread to read output
        def reader():
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    # Strip ANSI codes for web display
                    import re
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                    with self._lock:
                        self._job["log"].append(clean)
                        if len(self._job["log"]) > 500:
                            self._job["log"] = self._job["log"][-300:]
            proc.wait()
            with self._lock:
                self._job["done"] = True

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        handler.respond_json({"ok": True, "pid": proc.pid})

    def _api_stop(self, handler, content_len, content_type):
        with self._lock:
            if not self._job or self._job["done"]:
                handler.respond_json({"error": "No running job"}, status=400); return
            try:
                self._job["proc"].terminate()
                self._job["done"] = True
                self._job["log"].append("[HUB] Download stopped by user")
            except Exception:
                pass
        handler.respond_json({"ok": True})



PAGE_BODY = r"""
<style>
.gr-wrap{padding:20px;max-width:800px;margin:0 auto;font-size:14px}
.gr-section{background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:16px 20px;margin-bottom:12px}
.gr-section h3{font-size:14px;font-weight:600;color:var(--text-bright,var(--text));margin:0 0 12px;display:flex;align-items:center;gap:7px}
.gr-section h3 .section-icon{width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center;color:var(--text-dim);flex-shrink:0}
.gr-section h3 .section-icon svg{width:15px;height:15px;display:block}
.gr-row{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px;align-items:flex-end}
.gr-row > *{flex:1;min-width:160px}
.gr-field{display:flex;flex-direction:column;gap:3px}
.gr-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim)}
.gr-field input,.gr-field select{background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:7px 10px;color:var(--text);font-size:13px;width:100%}
.gr-check{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-dim);cursor:pointer}
.gr-check input{width:auto}
.gr-btn{padding:8px 20px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid var(--border);background:var(--accent,#4a9eff);color:#fff;border-color:transparent}
.gr-btn:hover{opacity:.85}
.gr-btn.stop{background:var(--red,#e05050)}
.gr-btn:disabled{opacity:.4;cursor:not-allowed}
.gr-log{background:var(--bg-main,#111);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:var(--mono,monospace);font-size:11px;color:var(--text-dim);max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.gr-status{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:13px}
.gr-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.gr-dot.run{background:#50c878;animation:pulse 1s infinite}
.gr-dot.idle{background:var(--text-dim)}
.gr-dot.done{background:var(--accent,#4a9eff)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.gr-info{font-size:12px;color:var(--text-dim);margin-top:6px;line-height:1.5}
</style>

<div class="gr-wrap">
  <div class="gr-section">
    <h3><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v11"/><path d="M8 10l4 4 4-4"/><path d="M5 18h14"/></svg></span>Civitai Image Grabber</h3>
    <div class="gr-row">
      <div class="gr-field">
        <span class="gr-label">Mode</span>
        <select id="grMode" onchange="updateMode()">
          <option value="1">Username</option>
          <option value="4">Model Version ID</option>
        </select>
      </div>
      <div class="gr-field" id="grInputWrap">
        <span class="gr-label" id="grInputLabel">Username(s)</span>
        <input id="grInput" placeholder="comma-separated">
      </div>
    </div>
    <div class="gr-row" id="grFilterRow" style="display:none">
      <div class="gr-field">
        <span class="gr-label">Filter tags (optional)</span>
        <input id="grFilterTags" placeholder="anime, portrait...">
      </div>
    </div>
    <div class="gr-row">
      <div class="gr-field">
        <span class="gr-label">Quality</span>
        <select id="grQuality"><option value="1">SD (jpeg)</option><option value="2" selected>HD (png)</option></select>
      </div>
      <div class="gr-field">
        <span class="gr-label">Max images (0 = unlimited)</span>
        <input id="grMax" type="number" value="0" min="0">
      </div>
      <div class="gr-field">
        <span class="gr-label">Concurrent downloads</span>
        <input id="grSem" type="number" value="5" min="1" max="20">
      </div>
    </div>
    <div class="gr-row" style="margin-bottom:0">
      <label class="gr-check" id="grDeepWrap"><input type="checkbox" id="grDeep"> Deep scan <span style="color:var(--text-dim)">(50K+ galleries)</span></label>
      <label class="gr-check"><input type="checkbox" id="grRedl"> Allow re-download</label>
    </div>
  </div>

  <div class="gr-section">
    <h3><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h7l2 2h9v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z"/></svg></span>Destination folder</h3>
    <div class="gr-row">
      <div class="gr-field">
        <span class="gr-label">Gallery folder</span>
        <select id="grFolder"><option value="">Loading...</option></select>
      </div>
    </div>
    <div id="grNewFolderWrap" style="display:none">
      <div class="gr-row">
        <div class="gr-field">
          <span class="gr-label">New folder path</span>
          <input id="grNewFolder" placeholder="C:\Images\Civitai or /home/user/civitai">
        </div>
      </div>
    </div>
    <div class="gr-info">Downloaded images go straight into the chosen folder. Username downloads use the username; model-version downloads use a readable model/version folder name when Civitai metadata is available. Add this folder as a Gallery root to browse them.</div>
  </div>

  <div class="gr-section">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <button class="gr-btn" id="grStart" onclick="startJob()">&#x25B6; Start download</button>
      <button class="gr-btn stop" id="grStop" onclick="stopJob()" style="display:none">&#x25A0; Stop</button>
      <div class="gr-status" id="grStatus"><span class="gr-dot idle"></span> Idle</div>
    </div>
    <div class="gr-log" id="grLog">Ready. Configure options above and click Start.</div>
  </div>
</div>

<script>
var _polling = null;

function updateMode() {
    var m = document.getElementById('grMode').value;
    var labels = {'1':'Username(s)','4':'Model Version ID(s)'};
    var placeholders = {'1':'artist1, artist2','4':'123456'};
    document.getElementById('grInputLabel').textContent = labels[m];
    document.getElementById('grInput').placeholder = placeholders[m];
    // Filter tags apply to both modes; deep scan is username-only.
    document.getElementById('grFilterRow').style.display = '';
    document.getElementById('grDeepWrap').style.display = (m==='1') ? '' : 'none';
}

function loadFolders() {
    fetch('/api/grabber/folders').then(function(r){return r.json()}).then(function(d){
        var sel = document.getElementById('grFolder');
        var h = '';
        (d.folders||[]).forEach(function(f){
            h += '<option value="'+f.replace(/"/g,'&quot;')+'">'+f+'</option>';
        });
        h += '<option value="__new__">+ New folder...</option>';
        sel.innerHTML = h;
        sel.onchange = function(){
            document.getElementById('grNewFolderWrap').style.display = sel.value === '__new__' ? '' : 'none';
        };
    });
}

function getOutputDir() {
    var sel = document.getElementById('grFolder');
    if (sel.value === '__new__') {
        return document.getElementById('grNewFolder').value.trim();
    }
    return sel.value;
}

function startJob() {
    var mode = document.getElementById('grMode').value;
    var input = document.getElementById('grInput').value.trim();
    var output = getOutputDir();
    if (!input) { alert('Please enter a ' + {1:'username',4:'version ID'}[mode]); return; }
    if (!output) { alert('Please select a destination folder'); return; }

    var data = {
        mode: mode,
        output_dir: output,
        quality: document.getElementById('grQuality').value,
        max_images: document.getElementById('grMax').value || '',
        semaphore: document.getElementById('grSem').value || '5',
        redownload: document.getElementById('grRedl').checked,
        filter_tags: document.getElementById('grFilterTags').value,
    };
    if (mode==='1') { data.username = input; data.deep_scan = document.getElementById('grDeep').checked; }
    else if (mode==='4') { data.version_id = input; }

    document.getElementById('grStart').disabled = true;
    document.getElementById('grLog').textContent = 'Starting download...\n';

    fetch('/api/grabber/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(function(r){return r.json()})
    .then(function(r){
        if (r.error) { document.getElementById('grLog').textContent = 'Error: ' + r.error; document.getElementById('grStart').disabled = false; return; }
        startPolling();
    })
    .catch(function(e){ document.getElementById('grLog').textContent = 'Error: ' + e; document.getElementById('grStart').disabled = false; });
}

function stopJob() {
    fetch('/api/grabber/stop', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(function(r){return r.json()}).then(function(){});
}

function startPolling() {
    document.getElementById('grStart').style.display = 'none';
    document.getElementById('grStop').style.display = '';
    if (_polling) clearInterval(_polling);
    _polling = setInterval(pollStatus, 1500);
    pollStatus();
}

function pollStatus() {
    fetch('/api/grabber/status').then(function(r){return r.json()}).then(function(d){
        var log = document.getElementById('grLog');
        var status = document.getElementById('grStatus');

        if (d.log && d.log.length) {
            log.textContent = d.log.join('\n');
            log.scrollTop = log.scrollHeight;
        }

        var elapsed = d.elapsed || 0;
        var mm = Math.floor(elapsed/60), ss = elapsed%60;
        var timeStr = mm + ':' + (ss<10?'0':'') + ss;

        if (d.running) {
            status.innerHTML = '<span class="gr-dot run"></span> Downloading... (' + timeStr + ')';
        } else if (d.done) {
            var code = d.exit_code;
            status.innerHTML = '<span class="gr-dot done"></span> ' + (code === 0 ? 'Completed' : 'Finished (exit ' + code + ')') + ' in ' + timeStr;
            document.getElementById('grStart').style.display = '';
            document.getElementById('grStart').disabled = false;
            document.getElementById('grStop').style.display = 'none';
            clearInterval(_polling);
            _polling = null;
        }
    });
}

// Check if there's already a running job
function checkRunning() {
    fetch('/api/grabber/status').then(function(r){return r.json()}).then(function(d){
        if (d.running) { startPolling(); }
    });
}

updateMode();
loadFolders();
checkRunning();
</script>
"""
