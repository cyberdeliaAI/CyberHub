"""CyberHub Module Manager: manual, verified module installation and updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlparse

from core import Module, available_module_classes, module_key_from_class
from core.server import build_shell
from modules.settings import SettingsModule, _is_newer_version


REGISTRY_REPOSITORY = "cyberdeliaAI/CyberHub-Registry"
REGISTRY_API = f"https://api.github.com/repos/{REGISTRY_REPOSITORY}"
REGISTRY_ASSET = "cyberhub-registry.json"
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_MODULE_BYTES = 512 * 1024 * 1024
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ModuleManagerModule(Module):
    name = "Module Manager"
    version = "1.0.1"
    description = "Install, update and remove CyberHub modules manually."
    order = 5
    show_in_tabs = True

    def __init__(self, hub):
        super().__init__(hub)
        self._lock = threading.RLock()
        self._catalog = {}
        self._catalog_meta = {}
        self._job = {
            "running": False,
            "stage": "idle",
            "message": "No module operation is running.",
            "downloaded": 0,
            "total": 0,
            "percent": 0,
            "error": "",
            "restart_required": False,
        }

    def routes_get(self):
        return {
            "/module_manager": self._page,
            "/modules": self._page,
            "/api/module-manager/local": self._api_local,
            "/api/module-manager/catalog": self._api_catalog,
            "/api/module-manager/status": self._api_status,
        }

    def routes_post(self):
        return {
            "/api/module-manager/install": self._api_install,
            "/api/module-manager/remove": self._api_remove,
        }

    def _page(self, handler, qs):
        handler.respond_html(build_shell(
            self.hub.registry,
            self.hub.settings,
            active_key="module_manager",
            page_title="Module Manager",
            body_html=PAGE,
        ))

    def _installed(self):
        records = self.hub.module_store.records()
        output = []
        for folder, cls in available_module_classes():
            record = records.get(folder, {})
            output.append({
                "id": folder,
                "name": str(getattr(cls, "name", folder)),
                "summary": str(getattr(cls, "description", "")),
                "version": str(getattr(cls, "version", "1.0")),
                "channel": "beta" if getattr(cls, "release_stage", "stable") == "beta" else "stable",
                "publisher_type": record.get("publisher_type") or "local",
                "repository": record.get("repository") or "",
                "protected": folder in self.hub.module_store.PROTECTED,
                "enabled": module_key_from_class(cls) in self.hub.registry.modules,
            })
        return sorted(output, key=lambda item: (not item["protected"], item["name"].lower()))

    def _api_local(self, handler, qs):
        handler.respond_json({
            "hub_version": str(getattr(self.hub, "VERSION", "1.0")),
            "registry_repository": REGISTRY_REPOSITORY,
            "modules": self._installed(),
        })

    @staticmethod
    def _asset_digest(asset):
        digest = str((asset or {}).get("digest") or "").strip().lower()
        return digest.split(":", 1)[1] if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else ""

    @staticmethod
    def _validate_release_url(url, repository):
        try:
            parsed = urlparse(str(url or ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("The registry contains an invalid download URL.") from exc
        expected = f"/{repository}/releases/download/".lower()
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "github.com"
            or not parsed.path.lower().startswith(expected)
            or parsed.username
            or parsed.password
        ):
            raise ValueError("A module download does not belong to its declared GitHub repository.")

    @staticmethod
    def _decode_json(raw, label):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"GitHub returned invalid {label} JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError(f"GitHub returned an invalid {label} document.")
        return value

    def _fetch_catalog(self):
        settings = self.hub.registry.get("settings")
        if not isinstance(settings, SettingsModule):
            raise ValueError("Settings update services are unavailable.")
        release_raw = settings._read_github_url(
            REGISTRY_API + "/releases/latest", max_bytes=4 * 1024 * 1024,
        )
        release = self._decode_json(release_raw, "release")
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("The module registry has no published stable release.")
        assets = {
            str(asset.get("name") or ""): asset
            for asset in release.get("assets", []) if isinstance(asset, dict)
        }
        asset = assets.get(REGISTRY_ASSET)
        if not asset:
            raise ValueError("The latest registry release has no module catalog.")
        digest = self._asset_digest(asset)
        if not digest:
            raise ValueError("GitHub did not provide an integrity digest for the module catalog.")
        size = int(asset.get("size") or 0)
        if size < 2 or size > MAX_REGISTRY_BYTES:
            raise ValueError("The module catalog has an invalid size.")
        url = str(asset.get("browser_download_url") or "")
        self._validate_release_url(url, REGISTRY_REPOSITORY)
        raw = settings._read_github_url(
            url, max_bytes=MAX_REGISTRY_BYTES, expected_sha256=digest,
        )
        catalog = self._validate_catalog(self._decode_json(raw, "module catalog"))
        catalog["release_url"] = str(release.get("html_url") or "")
        return catalog

    def _validate_catalog(self, value):
        if (
            value.get("schema") != 1
            or value.get("product") != "CyberHub"
            or value.get("repository") != REGISTRY_REPOSITORY
        ):
            raise ValueError("Unsupported CyberHub module catalog.")
        modules = value.get("modules")
        if not isinstance(modules, list) or len(modules) > 250:
            raise ValueError("The module catalog contains an invalid module list.")
        cleaned = []
        seen = set()
        for item in modules:
            if not isinstance(item, dict):
                raise ValueError("The module catalog contains an invalid entry.")
            module_id = str(item.get("id") or "")
            repository = str(item.get("repository") or "")
            publisher_type = str(item.get("publisher_type") or "")
            channel = str(item.get("channel") or "")
            version = str(item.get("version") or "").strip().lstrip("v")
            minimum = str(item.get("minimum_hub_version") or "").strip().lstrip("v")
            url = str(item.get("download_url") or "")
            sha256 = str(item.get("sha256") or "").lower()
            size = int(item.get("size") or 0)
            dependencies = item.get("dependencies") or []
            python_dependencies = item.get("python_dependencies") or []
            if not MODULE_ID_RE.fullmatch(module_id) or module_id in seen:
                raise ValueError("The module catalog contains an invalid or duplicate identifier.")
            if not REPOSITORY_RE.fullmatch(repository):
                raise ValueError(f"{module_id} has an invalid repository.")
            if publisher_type not in {"official", "community"}:
                raise ValueError(f"{module_id} has an invalid publisher type.")
            if publisher_type == "official" and not repository.startswith("cyberdeliaAI/"):
                raise ValueError(f"Official module {module_id} is outside the Cyberdelia organization.")
            if channel not in {"stable", "beta"} or not version or len(version) > 40:
                raise ValueError(f"{module_id} has invalid release information.")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size < 1 or size > MAX_MODULE_BYTES:
                raise ValueError(f"{module_id} has invalid integrity information.")
            if not isinstance(dependencies, list) or any(not MODULE_ID_RE.fullmatch(str(dep)) for dep in dependencies):
                raise ValueError(f"{module_id} has invalid module dependencies.")
            if not isinstance(python_dependencies, list) or len(python_dependencies) > 50:
                raise ValueError(f"{module_id} has invalid Python dependencies.")
            self._validate_release_url(url, repository)
            seen.add(module_id)
            cleaned.append({
                "type": "module",
                "id": module_id,
                "name": str(item.get("name") or module_id)[:120],
                "summary": str(item.get("summary") or "")[:500],
                "version": version,
                "minimum_hub_version": minimum,
                "repository": repository,
                "publisher_type": publisher_type,
                "channel": channel,
                "dependencies": list(dict.fromkeys(str(dep) for dep in dependencies)),
                "python_dependencies": [str(dep)[:160] for dep in python_dependencies],
                "asset": str(item.get("asset") or "")[:200],
                "download_url": url,
                "sha256": sha256,
                "size": size,
            })
        return {
            "schema": 1,
            "product": "CyberHub",
            "repository": REGISTRY_REPOSITORY,
            "generated_at": str(value.get("generated_at") or ""),
            "modules": cleaned,
        }

    def _catalog_response(self, catalog):
        installed = {item["id"]: item for item in self._installed()}
        hub_version = str(getattr(self.hub, "VERSION", "1.0"))
        modules = []
        for item in catalog.get("modules", []):
            current = installed.get(item["id"])
            missing_dependencies = [dep for dep in item["dependencies"] if dep not in installed]
            compatible = not item["minimum_hub_version"] or not _is_newer_version(
                item["minimum_hub_version"], hub_version,
            )
            copy = dict(item)
            copy.update({
                "installed": current is not None,
                "installed_version": str((current or {}).get("version") or ""),
                "update_available": current is None or _is_newer_version(
                    item["version"], (current or {}).get("version") or "0",
                ),
                "compatible": compatible,
                "missing_dependencies": missing_dependencies,
            })
            modules.append(copy)
        return {
            "ok": True,
            "generated_at": catalog.get("generated_at", ""),
            "release_url": catalog.get("release_url", ""),
            "modules": modules,
        }

    def _api_catalog(self, handler, qs):
        refresh = str(qs.get("refresh", [""])[0]).lower() in {"1", "true", "yes"}
        try:
            with self._lock:
                has_catalog = bool(self._catalog)
            if refresh or not has_catalog:
                catalog = self._fetch_catalog()
                with self._lock:
                    self._catalog = {item["id"]: item for item in catalog["modules"]}
                    self._catalog_meta = catalog
            with self._lock:
                catalog = dict(self._catalog_meta)
                catalog["modules"] = list(self._catalog.values())
            handler.respond_json(self._catalog_response(catalog))
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=502)

    def _set_job(self, **changes):
        with self._lock:
            self._job.update(changes)

    def _api_status(self, handler, qs):
        with self._lock:
            handler.respond_json(dict(self._job))

    def _download_progress(self, downloaded, total):
        percent = int(downloaded * 100 / total) if total else 0
        self._set_job(downloaded=downloaded, total=total, percent=min(99, percent))

    def _install_worker(self, item):
        download_path = ""
        try:
            settings = self.hub.registry.get("settings")
            if not isinstance(settings, SettingsModule):
                raise ValueError("Settings update services are unavailable.")
            download_dir = self.hub.data_path("updates", "downloads")
            os.makedirs(download_dir, exist_ok=True)
            fd, download_path = tempfile.mkstemp(
                prefix=f"cyberhub-{item['id']}-", suffix=".zip", dir=download_dir,
            )
            os.close(fd)
            self._set_job(stage="download", message=f"Downloading {item['name']}...", percent=0)
            settings._read_github_url(
                item["download_url"],
                max_bytes=min(MAX_MODULE_BYTES, max(item["size"] + 1024, item["size"] * 2)),
                expected_sha256=item["sha256"],
                output_path=download_path,
                progress=self._download_progress,
            )
            self._set_job(stage="install", message=f"Installing {item['name']}...", percent=100)
            result = settings._install_import_zip(
                download_path,
                item["asset"] or os.path.basename(item["download_url"]),
                require_manifest=True,
                expected_package=item,
            )
            self._set_job(
                running=False,
                stage="done",
                message=f"{item['name']} {item['version']} installed. Restart CyberHub to apply it.",
                error="",
                result=result,
                percent=100,
                restart_required=True,
            )
        except Exception as exc:
            self._set_job(
                running=False, stage="error", message="Module installation failed.",
                error=str(exc), result=None,
            )
        finally:
            if download_path:
                try:
                    os.remove(download_path)
                except OSError:
                    pass

    def _api_install(self, handler, content_len, content_type):
        if not handler._is_loopback():
            handler.respond_json({
                "error": "Module installation is only available from a browser on the CyberHub server."
            }, status=403)
            return
        data = handler.read_body_json(content_len)
        module_id = str((data or {}).get("module") or "").strip().lower()
        with self._lock:
            item = dict(self._catalog.get(module_id) or {})
            running = bool(self._job.get("running"))
        if not item:
            handler.respond_json({"error": "Refresh the module list before installing."}, status=400)
            return
        if running:
            handler.respond_json({"error": "Another module operation is already running."}, status=409)
            return
        installed = {entry["id"]: entry for entry in self._installed()}
        missing = [dependency for dependency in item["dependencies"] if dependency not in installed]
        if missing:
            handler.respond_json({
                "error": "Install required module(s) first: " + ", ".join(missing)
            }, status=400)
            return
        current = installed.get(module_id)
        if current and not _is_newer_version(item["version"], current["version"]):
            handler.respond_json({"error": "This version is already installed."}, status=400)
            return
        minimum = item.get("minimum_hub_version") or ""
        if minimum and _is_newer_version(minimum, getattr(self.hub, "VERSION", "1.0")):
            handler.respond_json({"error": f"This module requires CyberHub {minimum} or newer."}, status=400)
            return
        self._set_job(
            running=True, stage="starting", message=f"Preparing {item['name']}...",
            downloaded=0, total=0, percent=0, error="", result=None,
        )
        threading.Thread(
            target=self._install_worker, args=(item,), daemon=True,
            name=f"cyberhub-install-{module_id}",
        ).start()
        handler.respond_json({"ok": True, "running": True})

    def _api_remove(self, handler, content_len, content_type):
        if not handler._is_loopback():
            handler.respond_json({
                "error": "Module removal is only available from a browser on the CyberHub server."
            }, status=403)
            return
        with self._lock:
            if self._job["running"]:
                handler.respond_json({"error": "Another module operation is already running."}, status=409)
                return
        data = handler.read_body_json(content_len)
        module_id = str((data or {}).get("module") or "").strip().lower()
        try:
            result = self.hub.module_store.uninstall(module_id)
            self._set_job(
                stage="done", message="Module removed. Restart CyberHub to apply it.",
                percent=0, error="", restart_required=True,
            )
            handler.respond_json(result)
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=400)


PAGE = r"""
<style>
.mm{max-width:1180px;margin:0 auto;padding:24px 22px 50px}.mm h1{font-size:22px;margin:0 0 5px}.mm-lead{color:var(--text-dim);font-size:13px;margin-bottom:20px}.mm-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}.mm-tabs{display:flex;gap:3px;padding:3px;background:var(--bg-input);border:1px solid var(--border);border-radius:7px}.mm-tab,.mm-btn{border:0;border-radius:5px;padding:8px 13px;font:inherit;font-size:12px;cursor:pointer}.mm-tab{background:transparent;color:var(--text-dim)}.mm-tab.active{background:var(--accent);color:#fff}.mm-btn{background:var(--accent);color:#fff}.mm-btn.secondary{background:var(--bg-input);color:var(--text);border:1px solid var(--border)}.mm-btn.danger{background:transparent;color:#ef6464;border:1px solid rgba(239,100,100,.45)}.mm-btn:disabled{opacity:.45;cursor:default}.mm-time{margin-left:auto;color:var(--text-dim);font-size:11px}.mm-note{border:1px solid var(--border);background:var(--bg-panel);border-radius:7px;padding:11px 13px;color:var(--text-dim);font-size:12px;margin-bottom:14px}.mm-note.warn{border-color:#8a6a25;color:#d4ad52}.mm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}.mm-card{border:1px solid var(--border);background:var(--bg-panel);border-radius:8px;padding:14px;min-width:0}.mm-head{display:flex;gap:10px;align-items:flex-start}.mm-title{font-size:14px;font-weight:650}.mm-version{font-family:var(--mono);font-size:10px;color:var(--text-dim);background:var(--bg-input);padding:2px 6px;border-radius:4px;margin-left:5px}.mm-desc{font-size:12px;color:var(--text-dim);line-height:1.45;margin:8px 0 12px;min-height:34px}.mm-tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:12px}.mm-tag{font-size:10px;padding:3px 6px;border-radius:4px;background:var(--bg-input);color:var(--text-dim)}.mm-tag.beta{color:#e5b54b}.mm-tag.community{color:#bd8cff}.mm-actions{display:flex;align-items:center;gap:7px}.mm-repo{margin-left:auto;color:var(--accent);font-size:11px;text-decoration:none}.mm-empty{color:var(--text-dim);padding:28px 4px}.mm-job{display:none;border:1px solid var(--border);background:var(--bg-panel);border-radius:7px;padding:11px 13px;margin-bottom:14px}.mm-job.show{display:block}.mm-jobline{display:flex;justify-content:space-between;font-size:12px;margin-bottom:8px}.mm-track{height:5px;background:var(--bg-input);border-radius:3px;overflow:hidden}.mm-fill{height:100%;background:var(--accent);width:0}.mm-error{color:#ef6464;font-size:11px;margin-top:7px}@media(max-width:600px){.mm{padding:16px 12px}.mm-grid{grid-template-columns:1fr}.mm-time{width:100%;margin-left:0}}
</style>
<style>
.mm-restart{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px;padding:12px 0;border-block:1px solid var(--border);font-size:12px}.mm-restart[hidden]{display:none}.mm-restart-copy{flex:1;min-width:160px}.mm-restart-error{width:100%;color:#ef6464}.mm-jobline{gap:12px}.mm-jobline span:first-child{min-width:0;overflow-wrap:anywhere}
</style>
<main class="mm">
  <h1>Module Manager</h1>
  <div class="mm-lead">Choose how extensive you want CyberHub to be. Nothing is checked or installed automatically.</div>
  <div class="mm-bar">
    <div class="mm-tabs">
      <button class="mm-tab active" data-tab="installed">Installed</button>
      <button class="mm-tab" data-tab="official">Official</button>
      <button class="mm-tab" data-tab="beta">Beta</button>
      <button class="mm-tab" data-tab="community">Community</button>
    </div>
    <button class="mm-btn" id="mmRefresh">Check for updates</button>
    <span class="mm-time" id="mmTime">Not checked yet</span>
  </div>
  <div class="mm-job" id="mmJob"><div class="mm-jobline"><span id="mmJobText"></span><span id="mmPct"></span></div><div class="mm-track"><div class="mm-fill" id="mmFill"></div></div><div class="mm-error" id="mmError"></div></div>
  <div class="mm-restart" id="mmRestart" hidden>
    <span class="mm-restart-copy" id="mmRestartText" role="status">Restart required to apply module changes.</span>
    <button class="mm-btn" id="mmRestartButton">Restart CyberHub</button>
    <div class="mm-restart-error" id="mmRestartError" role="alert"></div>
  </div>
  <div class="mm-note" id="mmNote">The base installation contains CyberHub system files and Gallery. Other modules can be added independently.</div>
  <section class="mm-grid" id="mmGrid"></section>
</main>
<script>
(function(){
  const state={tab:'installed',local:[],catalog:[],busy:false,restarting:false};
  const $=id=>document.getElementById(id);
  function e(value){const d=document.createElement('div');d.textContent=value==null?'':value;return d.innerHTML}
  function localMap(){const o={};state.local.forEach(x=>o[x.id]=x);return o}
  function tags(item,installed){let out='';if(item.channel==='beta')out+='<span class="mm-tag beta">Beta</span>';if(item.publisher_type==='community')out+='<span class="mm-tag community">Community</span>';else out+='<span class="mm-tag">Official</span>';if(installed&&!item.protected)out+='<span class="mm-tag">Installed</span>';if(item.protected)out+='<span class="mm-tag">System</span>';return out}
  function render(){
    const locals=localMap();let list=[];
    if(state.tab==='installed')list=state.local.map(x=>Object.assign({},x,{installed:true,installed_version:x.version}));
    else list=state.catalog.filter(x=>state.tab==='official'?x.publisher_type==='official'&&x.channel==='stable':state.tab==='beta'?x.channel==='beta':x.publisher_type==='community');
    if(!list.length){$('mmGrid').innerHTML='<div class="mm-empty">'+(state.tab==='installed'?'No modules are installed.':'Check for updates to load the current module list.')+'</div>';return}
    $('mmGrid').innerHTML=list.map(item=>{
      const local=locals[item.id],installed=!!local||!!item.installed,canUpdate=installed&&item.update_available;
      let action='';
      if(item.protected) action='<button class="mm-btn secondary" disabled>Protected</button>';
      else if(state.tab==='installed') action='<button class="mm-btn danger" data-remove="'+e(item.id)+'">Remove</button>';
      else if(!item.compatible) action='<button class="mm-btn secondary" disabled>Requires Hub '+e(item.minimum_hub_version)+'</button>';
      else if(item.missing_dependencies&&item.missing_dependencies.length) action='<button class="mm-btn secondary" disabled>Needs '+e(item.missing_dependencies.join(', '))+'</button>';
      else if(canUpdate) action='<button class="mm-btn" data-install="'+e(item.id)+'">Update</button>';
      else if(installed) action='<button class="mm-btn secondary" disabled>Up to date</button>';
      else action='<button class="mm-btn" data-install="'+e(item.id)+'">Install</button>';
      const repo=item.repository?'<a class="mm-repo" target="_blank" rel="noopener" href="https://github.com/'+e(item.repository)+'">GitHub</a>':'';
      return '<article class="mm-card"><div class="mm-head"><div class="mm-title">'+e(item.name)+' <span class="mm-version">v'+e(item.version)+'</span></div></div><div class="mm-desc">'+e(item.summary||item.description||'')+'</div><div class="mm-tags">'+tags(item,installed)+'</div><div class="mm-actions">'+action+repo+'</div></article>';
    }).join('');
    updateControls();
  }
  function updateControls(){document.querySelectorAll('[data-install],[data-remove]').forEach(b=>b.disabled=state.busy||state.restarting);$('mmRestartButton').disabled=state.busy||state.restarting}
  async function json(url,options){const response=await fetch(url,options);const data=await response.json().catch(()=>({error:'Invalid server response'}));if(!response.ok)throw new Error(data.error||'Request failed');return data}
  async function loadLocal(){const data=await json('/api/module-manager/local');state.local=data.modules||[];render()}
  async function refresh(){
    const b=$('mmRefresh');b.disabled=true;b.textContent='Checking...';$('mmNote').classList.remove('warn');
    try{const data=await json('/api/module-manager/catalog?refresh=1');state.catalog=data.modules||[];$('mmTime').textContent=data.generated_at?'Updated '+data.generated_at:'Module list loaded';$('mmNote').textContent='The list was loaded manually from the verified CyberHub registry. Installations require a restart.';render()}
    catch(err){$('mmNote').classList.add('warn');$('mmNote').textContent=err.message;}
    finally{b.disabled=false;b.textContent='Check for updates'}
  }
  async function install(id){
    if(state.busy||state.restarting)return;
    const item=state.catalog.find(x=>x.id===id);if(!item)return;
    let question=(item.installed?'Update ':'Install ')+item.name+' '+item.version+'?';
    if(item.publisher_type==='community')question+='\n\nCommunity modules contain executable code and are not maintained by Cyberdelia.';
    if(item.python_dependencies&&item.python_dependencies.length)question+='\n\nPython packages may be required after installation: '+item.python_dependencies.join(', ');
    if(!confirm(question))return;
    state.busy=true;updateControls();
    try{await json('/api/module-manager/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({module:id})});poll()}
    catch(err){state.busy=false;updateControls();alert(err.message)}
  }
  async function remove(id){if(state.busy||state.restarting)return;const item=state.local.find(x=>x.id===id);if(!item||!confirm('Remove '+item.name+'?\n\nA backup is kept. Your module settings and user data are preserved.'))return;state.busy=true;updateControls();try{await json('/api/module-manager/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({module:id})});await poll()}catch(err){state.busy=false;updateControls();alert(err.message)}}
  async function restart(){
    if(state.busy||state.restarting||!confirm('Restart CyberHub? Active tasks will be interrupted. The page will reload automatically.'))return;
    state.restarting=true;updateControls();$('mmRestartError').textContent='';$('mmRestartButton').textContent='Restarting...';
    try{
      const status=await json('/api/module-manager/status');
      if(status.running)throw new Error('A module operation is still running. Wait for it to finish.');
      await json('/api/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      $('mmRestartText').textContent='Waiting for CyberHub to restart...';
      const deadline=Date.now()+60000;
      async function reconnect(){
        try{
          const s=await json('/api/module-manager/status',{cache:'no-store',signal:AbortSignal.timeout(3000)});
          // A cleared flag confirms the new process, not the old server still shutting down.
          if(s.restart_required===false){try{localStorage.removeItem('settingsRestartPending')}catch(e){}location.reload();return}
        }catch(err){}
        if(Date.now()<deadline){setTimeout(reconnect,1000);return}
        $('mmRestartError').textContent='CyberHub has not reconnected. Check the server console, then reload this page.';
        state.restarting=false;updateControls();$('mmRestartButton').textContent='Restart CyberHub';
      }
      setTimeout(reconnect,2000);
    }catch(err){$('mmRestartError').textContent=err.message;state.restarting=false;updateControls();$('mmRestartButton').textContent='Restart CyberHub'}
  }
  async function poll(){
    try{const s=await json('/api/module-manager/status');state.busy=!!s.running;updateControls();$('mmRestart').hidden=!s.restart_required;const show=s.running||s.stage==='done'||s.stage==='error';$('mmJob').classList.toggle('show',show);$('mmJobText').textContent=s.message||'';$('mmPct').textContent=s.percent?s.percent+'%':'';$('mmFill').style.width=(s.percent||0)+'%';$('mmError').textContent=s.error||'';if(s.running){setTimeout(poll,600)}else if(s.stage==='done'){await loadLocal();if(state.catalog.length)refresh()}}
    catch(err){$('mmJob').classList.add('show');$('mmError').textContent=err.message}
  }
  document.addEventListener('click',ev=>{const tab=ev.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;document.querySelectorAll('.mm-tab').forEach(x=>x.classList.toggle('active',x===tab));render();return}const add=ev.target.closest('[data-install]');if(add)install(add.dataset.install);const del=ev.target.closest('[data-remove]');if(del)remove(del.dataset.remove)});
  $('mmRestartButton').addEventListener('click',restart);
  $('mmRefresh').addEventListener('click',refresh);loadLocal().catch(err=>{$('mmNote').textContent=err.message;$('mmNote').classList.add('warn')});poll();
})();
</script>
"""
