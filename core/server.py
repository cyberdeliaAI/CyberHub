"""HTTP server, settings persistence, shared HTML shell.

Design notes
------------
The hub has ONE topbar. Modules render the rest of the page inside that
shell, so the navigation never duplicates. The shell is produced by
`build_shell()`, which all modules call from their _page() handler.

There is intentionally no separate "hub menu" overlay. Adding modules
just adds tabs to the topbar — that is the navigation.
"""

import gzip as _gzip
import json
import secrets
import mimetypes
import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse


# ─── Settings ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "title": "CyberHub",
    "startup": "gallery",
    "theme": "dark",
    "port": 8899,
    "network": {
        "listen_lan": False,
        "share_network": False,
        "require_auth": True,    # require an access token for non-loopback requests when listen_lan is on
        "auth_token": "",        # auto-generated on first LAN launch
        "allow_remote_browse": False,  # allow LAN clients to use the host filesystem folder picker
    },
    "civitai": {
        "models_path": "",
    },
    "modules": {},          # per-module settings dicts
}


class Settings:
    """Thread-safe settings backed by settings.json next to hub.py."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
        self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self._merge_defaults(loaded, DEFAULT_SETTINGS)
            self.data = loaded
            if self.data.get("title") == "Cyberdelia Hub":
                self.data["title"] = "CyberHub"
                self.save()
        except Exception as e:
            print(f"[SETTINGS] Failed to load {self.path}: {e} — using defaults")

    @staticmethod
    def _merge_defaults(target, defaults):
        """Fill in missing top-level keys from defaults (one level deep)."""
        for k, v in defaults.items():
            if k not in target:
                target[k] = json.loads(json.dumps(v))
            elif isinstance(v, dict) and isinstance(target[k], dict):
                for kk, vv in v.items():
                    target[k].setdefault(kk, vv)

    def save(self):
        with self.lock:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as e:
                print(f"[SETTINGS] Failed to save: {e}")

    # ─── Top-level keys ──────────────────────────────────────────────────────
    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
        self.save()

    # ─── Nested key (e.g. network.listen_lan) ────────────────────────────────
    def get_path(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_path(self, dotted, value):
        with self.lock:
            parts = dotted.split(".")
            node = self.data
            for p in parts[:-1]:
                if p not in node or not isinstance(node[p], dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value
        self.save()

    # ─── Per-module settings ─────────────────────────────────────────────────
    def get_module(self, module_key):
        return self.data.setdefault("modules", {}).get(module_key, {})

    def get_module_setting(self, module_key, key, default=None):
        return self.get_module(module_key).get(key, default)

    def set_module_setting(self, module_key, key, value):
        with self.lock:
            self.data.setdefault("modules", {}).setdefault(module_key, {})[key] = value
        self.save()

    def is_module_enabled(self, module_key):
        # Default: enabled. Settings module is always on (enforced in hub.py).
        return self.get_module(module_key).get("enabled", True)


# ─── Shared HTML shell ───────────────────────────────────────────────────────

# The shell is split into the head/CSS (shared by all modules) and the
# topbar markup (which slots in dynamic tab links). Each module renders
# its own body inside this shell.

_BASE_CSS = """
:root {
    --bg-darkest:#0a0c10; --bg-dark:#0f1218; --bg-panel:#141820;
    --bg-card:#1a1f2a; --bg-hover:#222838; --bg-active:#1c2436;
    --bg-input:#1a1f2a; --bg-main:#0a0c10; --bg:#0a0c10;
    --border:#1e2433; --border-light:#2a3040;
    --text:#c8cdd8; --text-dim:#6b7280; --text-bright:#e8ecf4;
    --accent:#4a9eff; --accent-dim:#2d6ab3; --accent-glow:rgba(74,158,255,0.08);
    --green:#3ddc84; --orange:#f59e0b; --red:#ef4444; --purple:#a78bfa;
    --prompt-text:#a9b1d6; --neg-prompt:#f7768e;
    --setting-key:#9ece6a; --setting-val:#c0caf5;
    --radius:6px;
    --font:'IBM Plex Sans',-apple-system,sans-serif;
    --mono:'JetBrains Mono','Consolas',monospace;
    --thumb-size:180px;
}
body.theme-light {
    --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
    --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
    --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
    --border:#d9e1ee; --border-light:#c7d3e4;
    --text:#334155; --text-dim:#718096; --text-bright:#111827;
    --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
    --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
    --prompt-text:#334155; --neg-prompt:#be123c;
    --setting-key:#3f7d20; --setting-val:#243b65;
}
@media (prefers-color-scheme: light) {
    body.theme-system {
        --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
        --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
        --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
        --border:#d9e1ee; --border-light:#c7d3e4;
        --text:#334155; --text-dim:#718096; --text-bright:#111827;
        --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
        --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
        --prompt-text:#334155; --neg-prompt:#be123c;
        --setting-key:#3f7d20; --setting-val:#243b65;
    }
}
* { margin:0; padding:0; box-sizing:border-box; }

/* Topbar (shared across every module page) */
.topbar { background:var(--bg-dark); border-bottom:1px solid var(--border); display:flex; align-items:center; padding:0 16px; gap:12px; z-index:10; }
.topbar-title { font-family:var(--mono); font-weight:600; font-size:14px; color:var(--accent); letter-spacing:-0.3px; white-space:nowrap; cursor:pointer; text-decoration:none; }
.topbar-title:hover { color:var(--text-bright); }
.topbar-spacer { flex:1; }
.topbar-controls { display:flex; gap:4px; align-items:center; }
.btn-icon { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; text-decoration:none; }
.btn-icon:hover { background:var(--bg-hover); color:var(--text); }
.btn-icon.active { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }

""" + r"""/* Hub menu (hamburger + dropdown panel) */
.hub-menu { position:relative; }
.hub-menu-btn { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; padding:0; }
.hub-menu-btn:hover { background:var(--bg-hover); color:var(--text); }
.hub-menu.open .hub-menu-btn { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.hub-menu-active { font-size:12px; color:var(--text); margin-left:2px; white-space:nowrap; display:flex; align-items:center; gap:5px; pointer-events:none; }
.hub-menu-active .icon { width:14px; height:14px; display:inline-flex; align-items:center; justify-content:center; color:currentColor; }
.hub-menu-panel { position:absolute; top:calc(100% + 4px); left:0; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:var(--radius); min-width:520px; padding:4px; box-shadow:0 8px 24px rgba(0,0,0,.5); z-index:1000; opacity:0; visibility:hidden; transform:translateY(-4px); transition:opacity .12s, transform .12s, visibility .12s; display:grid; grid-template-columns:repeat(2, minmax(240px, 1fr)); gap:1px 4px; }
.hub-menu.open .hub-menu-panel { opacity:1; visibility:visible; transform:translateY(0); }
.hub-menu-item { display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:4px; text-decoration:none; color:var(--text); font-size:12px; transition:background .1s; cursor:pointer; min-width:0; }
.hub-menu-item:hover { background:var(--bg-hover); }
.hub-menu-item.active { background:var(--bg-active); color:var(--accent); }
.hub-menu-item .icon { width:20px; height:20px; display:inline-flex; align-items:center; justify-content:center; flex-shrink:0; color:currentColor; }
.hub-menu-item .icon svg, .hub-menu-active .icon svg { width:16px; height:16px; display:block; }
.hub-menu-item > span:not(.icon) { min-width:0; overflow:hidden; }
.hub-menu-item .label { display:block; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-item .desc { display:block; font-size:10px; color:var(--text-dim); font-weight:400; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-sep { grid-column:1 / -1; height:1px; background:var(--border); margin:4px 6px; }
.hub-menu-panel > a:last-child { grid-column:1 / -1; }
@media (max-width:560px) { .hub-menu-panel { grid-template-columns:1fr; min-width:240px; } .hub-menu-panel > a:last-child { grid-column:auto; } }

/* Toasts */
.toast { position:fixed; bottom:40px; right:16px; background:var(--bg-card); border:1px solid var(--border); color:var(--text); padding:8px 14px; border-radius:var(--radius); font-size:12px; z-index:2000; animation:toast-in .2s ease-out; box-shadow:0 4px 16px rgba(0,0,0,.4); }
@keyframes toast-in { from{opacity:0;transform:translateY(8px)} }

::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--border-light); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--text-dim); }
"""

def theme_body_class(settings):
    """Return the body class for the configured color theme."""
    theme = (settings.get("theme", "dark") or "dark").strip().lower()
    if theme not in ("dark", "light", "system"):
        theme = "dark"
    return "theme-" + theme


def module_icon_html(key):
    """Monochrome inline icons for module navigation."""
    paths = {
        "gallery": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="1.5"/><path d="M21 15l-5-5L5 19"/>',
        "viewer": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
        "cropper": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/><circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/>',
        "tools": '<rect x="8" y="3" width="8" height="4" rx="1"/><path d="M9 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3"/><path d="M8 12h8"/><path d="M8 16h6"/>',
        "meta_copy_tool": '<path d="M14 3l7 7"/><path d="M5 21l4-1 11-11-3-3L6 17l-1 4z"/><path d="M3 7h7"/>',
        "captioner": '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-7a8 8 0 1 1 18-4z"/>',
        "prompt-engineer": '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z"/>',
        "prompt_engineer": '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z"/>',
        "grabber": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>',
        "civitai_grabber": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>',
        "civitai_browser": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a13 13 0 0 1 0 18"/><path d="M12 3a13 13 0 0 0 0 18"/>',
        "library": '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22V5.5z"/><path d="M8 7h8"/><path d="M8 11h6"/>',
        "prompt_library": '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22V5.5z"/><path d="M8 7h8"/><path d="M8 11h6"/>',
        "overlay": '<path d="M4 20h16"/><path d="M14 4l6 6-9 9H5v-6l9-9z"/>',
        "danbooru": '<path d="M20 10l-8 10L4 12V4h8l8 6z"/><circle cx="9" cy="9" r="1.5"/>',
        "amateur-photo": '<path d="M4 8h4l2-3h4l2 3h4v11H4V8z"/><circle cx="12" cy="14" r="4"/>',
        "amateur_photo": '<path d="M4 8h4l2-3h4l2 3h4v11H4V8z"/><circle cx="12" cy="14" r="4"/>',
        "compare": '<circle cx="9" cy="9" r="5"/><circle cx="15" cy="15" r="5"/>',
        "settings": '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.3 3a7 7 0 0 0-1.7 1l-2.4-1-2 3.5L5.1 11a7 7 0 0 0 0 2l-2 1.5 2 3.5 2.4-1a7 7 0 0 0 1.7 1l.3 3h5l.3-3a7 7 0 0 0 1.7-1l2.4 1 2-3.5-2-1.5c.1-.3.1-.7.1-1z"/>',
    }
    body = paths.get(key, '<circle cx="12" cy="12" r="4"/>')
    return (
        '<span class="icon" aria-hidden="true">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f'{body}</svg></span>'
    )

_HUB_MENU_JS = r"""
/* Hub hamburger menu: toggle on click, close on outside-click or Esc */
(function() {
    function init() {
        document.addEventListener('click', function(ev) {
            var btn = ev.target.closest('.hub-menu-btn');
            var menu = document.querySelector('.hub-menu');
            if (!menu) return;
            if (btn && menu.contains(btn)) {
                ev.preventDefault();
                menu.classList.toggle('open');
            } else if (!ev.target.closest('.hub-menu-panel')) {
                menu.classList.remove('open');
            }
        });
        document.addEventListener('keydown', function(ev) {
            if (ev.key === 'Escape') {
                var menu = document.querySelector('.hub-menu.open');
                if (menu) menu.classList.remove('open');
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
"""

_BASE_JS = """
function showToast(msg) { var e=document.querySelector('.toast'); if(e)e.remove(); var t=document.createElement('div'); t.className='toast'; t.textContent=msg; document.body.appendChild(t); setTimeout(function(){t.remove()},2500); }
function escHtml(s) { var d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }
function escAttr(s) { return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;'); }
function formatSize(b) { if(!b) return '?'; if(b<1024) return b+' B'; if(b<1024*1024) return (b/1024).toFixed(1)+' KB'; return (b/1024/1024).toFixed(1)+' MB'; }
function copyFallback(text) { try { var ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select(); var ok=document.execCommand('copy'); document.body.removeChild(ta); return ok; } catch(e) { return false; } }
function copyText(text, btn) { var orig=btn?btn.textContent:''; function ok(){if(btn){btn.textContent='\\u2713 Copied';setTimeout(function(){btn.textContent=orig},1500);}} if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok).catch(function(){copyFallback(text)&&ok();});} else { if(copyFallback(text)) ok(); } }
"""


def build_module_menu(registry, active_key):
    """Return the hamburger menu HTML (button + dropdown panel).

    Used by both build_topbar() (viewer/tools/settings) and the gallery
    module (which substitutes it into its own page via {MODULE_NAV}).
    Keeps every module page consistent and frees up topbar space as
    more modules are added.
    """
    tabs = registry.visible_tabs()
    active_mod = next((m for m in tabs if m.key() == active_key), None)
    if active_mod is None and active_key == "settings":
        # Settings tab is hidden, but we still want to label it when active
        active_mod = registry.get("settings")

    hamburger_svg = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        '<line x1="4" y1="7" x2="20" y2="7"/>'
        '<line x1="4" y1="12" x2="20" y2="12"/>'
        '<line x1="4" y1="17" x2="20" y2="17"/></svg>'
    )

    items = []
    for m in tabs:
        cls = "hub-menu-item" + (" active" if m.key() == active_key else "")
        icon = module_icon_html(m.key())
        desc = (m.description or "").strip()
        desc_html = f'<span class="desc">{desc}</span>' if desc else ""
        items.append(
            f'<a href="/{m.key()}" class="{cls}">'
            f'{icon}<span><span class="label">{m.name}</span>{desc_html}</span></a>'
        )
    panel_html = "".join(items)
    # Settings link at the bottom of the panel
    set_cls = "hub-menu-item" + (" active" if active_key == "settings" else "")
    panel_html += (
        '<div class="hub-menu-sep"></div>'
        f'<a href="/settings" class="{set_cls}">'
        f'{module_icon_html("settings")}'
        '<span><span class="label">Settings</span>'
        '<span class="desc">Configure hub and modules</span></span></a>'
    )

    active_label = ""
    if active_mod:
        ai = module_icon_html(active_mod.key())
        active_label = f'<span class="hub-menu-active">{ai}{active_mod.name}</span>'

    return (
        '<div class="hub-menu">'
        f'<button class="hub-menu-btn" aria-label="Modules" type="button">{hamburger_svg}</button>'
        f'<div class="hub-menu-panel">{panel_html}</div>'
        '</div>'
        + active_label
    )


def build_topbar(registry, settings, active_key, extra_html="", controls_html=""):
    """Return the inner HTML for <div class="topbar">…</div>.

    Args:
        registry: ModuleRegistry instance.
        settings: Settings instance (used for hub title).
        active_key: settings key of the currently-active module ("gallery", "viewer", …).
        extra_html: HTML rendered between the menu and the spacer
            (e.g. breadcrumb + search in the gallery).
        controls_html: HTML rendered inside topbar-controls (right side,
            before the settings gear).
    """
    from html import escape as _esc
    title = _esc(settings.get("title", "CyberHub"))
    menu_html = build_module_menu(registry, active_key)

    settings_active = " active" if active_key == "settings" else ""
    theme_toggle = (
        f'<button class="btn-icon theme-toggle-btn" type="button" title="Toggle dark/light mode" '
        f'aria-label="Toggle dark/light mode" onclick="hubToggleTheme()">'
        f'<svg class="theme-icon theme-icon-moon" width="16" height="16" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3 6.6 6.6 0 0 0 21 12.8z"/></svg>'
        f'<svg class="theme-icon theme-icon-sun" width="16" height="16" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round"><circle cx="12" cy="12" r="4"/>'
        f'<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
        f'</button>'
    )
    gear = (
        f'<a href="/settings" class="btn-icon{settings_active}" title="Settings" '
        f'aria-label="Settings">'
        f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2"><circle cx="12" cy="12" r="3"/>'
        f'<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></a>'
    )

    # Help (?) button — left of the gear. Only on real module pages (a key that
    # maps to a help section); the /help page itself passes an empty key.
    help_btn = ""
    if active_key:
        active_mod = registry.get(active_key)
        help_title = _esc(active_mod.name) if active_mod else _esc(active_key)
        help_btn = (
            f'<button class="btn-icon help-btn" type="button" title="Help for this page" '
            f'aria-label="Help" data-help-key="{_esc(active_key)}" data-help-title="{help_title}" '
            f'onclick="hubOpenHelp()">'
            f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'<circle cx="12" cy="12" r="10"/>'
            f'<path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg></button>'
        )

    return (
        f'{menu_html}'
        f'<a href="/" class="topbar-title">&#x2B21; {title}</a>'
        f'{extra_html}'
        f'<div class="topbar-spacer"></div>'
        f'<div class="topbar-controls">{controls_html}{theme_toggle}{help_btn}{gear}</div>'
        f'{HELP_OVERLAY_HTML}'
    )


# Shared help modal markup + styles + script. Injected once per page by
# build_topbar(), so every page (build_shell modules + the gallery) gets it.
HELP_OVERLAY_HTML = """
<style>
.help-overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;z-index:6000;padding:24px}
.help-overlay.open{display:flex}
.help-dialog{background:var(--bg-panel);border:1px solid var(--border-light);border-radius:12px;max-width:740px;width:100%;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 16px 48px rgba(0,0,0,.5)}
.help-dialog-head{display:flex;align-items:center;gap:12px;padding:13px 18px;border-bottom:1px solid var(--border);flex-shrink:0}
.help-dialog-title{font-weight:600;font-size:15px;color:var(--text-bright)}
.help-full-link{margin-left:auto;font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap}
.help-full-link:hover{text-decoration:underline}
.help-close{background:none;border:none;color:var(--text-dim);cursor:pointer;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;padding:0}
.help-close:hover{background:var(--bg-hover);color:var(--text)}
.help-dialog-body,.help-page{padding:6px 22px 22px;overflow-y:auto;font-size:13px;line-height:1.62;color:var(--text)}
.help-loading{color:var(--text-dim);padding:18px 0}
.help-dialog-body h1,.help-page h1{font-size:20px}
.help-dialog-body h2,.help-page h2{font-size:16px;margin:18px 0 8px;color:var(--text-bright)}
.help-dialog-body h3,.help-page h3,.help-dialog-body h4,.help-page h4{font-size:13px;margin:14px 0 6px;color:var(--text-bright);text-transform:uppercase;letter-spacing:.4px}
.help-dialog-body p,.help-page p{margin:8px 0}
.help-dialog-body code,.help-page code{font-family:var(--mono);background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:12px}
.help-dialog-body pre,.help-page pre{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:10px 0}
.help-dialog-body pre code,.help-page pre code{border:none;background:none;padding:0}
.help-dialog-body a,.help-page a{color:var(--accent)}
.help-dialog-body table,.help-page table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12px}
.help-dialog-body th,.help-page th,.help-dialog-body td,.help-page td{border:1px solid var(--border);padding:5px 9px;text-align:left}
.help-dialog-body th,.help-page th{background:var(--bg-card);color:var(--text-dim)}
.help-dialog-body ul,.help-page ul,.help-dialog-body ol,.help-page ol{padding-left:20px;margin:8px 0}
.help-dialog-body li,.help-page li{margin:3px 0}
.help-dialog-body hr,.help-page hr{border:none;border-top:1px solid var(--border);margin:16px 0}
.help-page{max-width:860px;margin:0 auto;padding:24px}
.help-btn svg,.help-close svg{display:block}
.theme-toggle-btn .theme-icon{display:block}
.theme-toggle-btn .theme-icon-sun{display:none}
body.theme-light .theme-toggle-btn .theme-icon-moon{display:none}
body.theme-light .theme-toggle-btn .theme-icon-sun{display:block}
</style>
<div class="help-overlay" id="hubHelpOverlay" onclick="if(event.target===this)hubCloseHelp()">
  <div class="help-dialog">
    <div class="help-dialog-head">
      <span class="help-dialog-title" id="hubHelpTitle">Help</span>
      <a class="help-full-link" href="/help">Open full manual &rarr;</a>
      <button class="help-close" onclick="hubCloseHelp()" aria-label="Close" title="Close">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <div class="help-dialog-body" id="hubHelpBody"></div>
  </div>
</div>
<script>
function hubOpenHelp(){
  var btn=document.querySelector('.help-btn');
  var key=btn?btn.getAttribute('data-help-key'):'';
  document.getElementById('hubHelpTitle').textContent=(btn&&btn.getAttribute('data-help-title'))||'Help';
  var body=document.getElementById('hubHelpBody');
  body.innerHTML='<div class="help-loading">Loading\\u2026</div>';
  document.getElementById('hubHelpOverlay').classList.add('open');
  fetch('/help/section/'+encodeURIComponent(key)).then(function(r){return r.text();}).then(function(h){
    body.innerHTML=h||'<p>No help available for this page yet. See the <a href="/help">full manual</a>.</p>';
  }).catch(function(){ body.innerHTML='<p>Could not load help. Open the <a href="/help">full manual</a>.</p>'; });
}
function hubCloseHelp(){ var o=document.getElementById('hubHelpOverlay'); if(o) o.classList.remove('open'); }
document.addEventListener('keydown',function(e){ if(e.key==='Escape') hubCloseHelp(); });
function hubCurrentTheme(){
  if(document.body.classList.contains('theme-light')) return 'light';
  if(document.body.classList.contains('theme-system')){
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return 'dark';
}
function hubApplyTheme(value){
  document.body.classList.remove('theme-dark','theme-light','theme-system');
  document.body.classList.add('theme-'+value);
  var sel=document.getElementById('themeSelect');
  if(sel) sel.value=value;
}
function hubToggleTheme(){
  var next=hubCurrentTheme()==='dark' ? 'light' : 'dark';
  hubApplyTheme(next);
  fetch('/api/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({module:'',key:'theme',value:next})})
    .catch(function(){ hubApplyTheme(next==='dark'?'light':'dark'); });
}
</script>
"""


def _local_font_css():
    """Generate @font-face CSS for locally bundled fonts, or empty string if not present."""
    import glob
    _hub_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    font_dir = os.path.join(_hub_root, "resources", "fonts")
    woff2_files = glob.glob(os.path.join(font_dir, "*.woff2"))
    if not woff2_files:
        return ""
    css = ""
    font_map = {
        "ibm-plex-sans": "IBM Plex Sans",
        "jetbrains-mono": "JetBrains Mono",
        "inter": "Inter",
    }
    for f in sorted(woff2_files):
        name = os.path.basename(f)  # e.g. ibm-plex-sans-400.woff2
        stem = name.rsplit(".", 1)[0]  # ibm-plex-sans-400
        parts = stem.rsplit("-", 1)
        if len(parts) != 2:
            continue
        family_key, weight = parts
        family = font_map.get(family_key)
        if not family:
            continue
        css += f"@font-face {{ font-family:'{family}'; font-weight:{weight}; font-style:normal; font-display:swap; src:url('/fonts/{name}') format('woff2'); }}\n"
    return css

def _font_links():
    """Return <link> tags for Google Fonts if local fonts are not available."""
    local_css = _local_font_css()
    if local_css:
        return f"<style>{local_css}</style>"
    return '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">'


def build_shell(registry, settings, active_key, page_title, body_html,
                head_extra="", body_class=""):
    """Wrap a module body in the full HTML document with the shared topbar.

    `body_html` should NOT include <body> or the topbar — just the content.
    The shell handles the document chrome and renders the topbar via
    build_topbar(). Modules that need to customize the topbar (e.g. add
    breadcrumb + search) should override _render_topbar themselves rather
    than passing custom extra_html through here.
    """
    from html import escape as _esc
    title = _esc(settings.get("title", "CyberHub"))
    topbar = build_topbar(registry, settings, active_key)
    fonts = _font_links()
    classes = " ".join(c for c in (theme_body_class(settings), body_class) if c)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title} — {title}</title>
{fonts}
<style>
{_BASE_CSS}
body {{ background:var(--bg-darkest); color:var(--text); font-family:var(--font); font-size:13px; line-height:1.5; min-height:100vh; }}
.topbar {{ height:48px; }}
.hub-content {{ height:calc(100vh - 48px); overflow:auto; }}
{head_extra}
</style>
</head>
<body class="{classes}">
<div class="topbar">{topbar}</div>
<div class="hub-content">
{body_html}
</div>
<script>
{_BASE_JS}
{_HUB_MENU_JS}
</script>
</body>
</html>
"""


# ─── HTTP handler ────────────────────────────────────────────────────────────

class _BodyCountingReader:
    """Thin wrapper around the request rfile that tracks bytes consumed by the handler.

    Only purpose: let do_POST know whether the body was fully read, so it can drain any
    leftover bytes before the keep-alive connection serves the next request. Forwards
    every other attribute to the underlying file object so existing readers (read_body_json,
    parse_multipart, anything calling readline/readinto) work unchanged.
    """
    __slots__ = ("_base", "_remaining")
    def __init__(self, base, total):
        self._base = base
        self._remaining = max(0, int(total))
    def read(self, n=-1):
        chunk = self._base.read() if (n is None or n < 0) else self._base.read(n)
        self._remaining = max(0, self._remaining - len(chunk))
        return chunk
    def readline(self, *args, **kwargs):
        chunk = self._base.readline(*args, **kwargs)
        self._remaining = max(0, self._remaining - len(chunk))
        return chunk
    def readinto(self, b):
        n = self._base.readinto(b)
        if n:
            self._remaining = max(0, self._remaining - n)
        return n
    def __getattr__(self, name):
        return getattr(self._base, name)

class HubHandler(SimpleHTTPRequestHandler):
    """Dispatches requests to registered module routes.

    Class attributes are set by hub.py before serving:
        HubHandler.registry = ModuleRegistry instance
        HubHandler.settings = Settings instance
        HubHandler.hub      = Hub instance (with .civitai etc.)
        HubHandler.auth_token   = string token required for non-localhost
                                  requests when require_auth is True; "" disables.
        HubHandler.require_auth = bool; True → enforce auth_token for non-loopback.
    """

    registry = None
    settings = None
    hub = None
    auth_token = ""
    require_auth = False

    # Cookie name for the LAN auth token
    AUTH_COOKIE = "cdhub_token"

    def log_message(self, fmt, *args):
        pass  # silence the default per-request stdout spam

    # ─── Auth ────────────────────────────────────────────────────────────────
    def _is_loopback(self):
        """Connection came from this machine? Local browser bypasses auth."""
        try:
            host, _ = self.client_address[:2]
        except (TypeError, ValueError, IndexError):
            return False
        # IPv4 loopback, IPv6 loopback, and IPv4-mapped IPv6 loopback
        return host.startswith("127.") or host in ("::1", "::ffff:127.0.0.1")

    def _extract_token(self, parsed):
        """Try cookie, then ?token=…, then `Authorization: Bearer …`."""
        # Cookie
        raw = self.headers.get("Cookie", "")
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k == self.AUTH_COOKIE and v:
                        return v
        # Query string
        qs = parse_qs(parsed.query)
        if "token" in qs and qs["token"]:
            return qs["token"][0]
        # Bearer header
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        return ""

    def _check_auth(self, parsed):
        """Return True if the request is allowed to proceed.

        Side effect: if the token was supplied in the query string and is
        valid, we set a cookie and 302 to the same path *without* the token
        in the URL so it doesn't get bookmarked or appear in referrers.
        Caller should return immediately after this writes a response.
        """
        if not self.require_auth or not self.auth_token:
            return True
        if self._is_loopback():
            return True

        supplied = self._extract_token(parsed)
        if supplied and secrets.compare_digest(supplied, self.auth_token):
            # If the token came via the URL, redirect to clean URL + set cookie
            qs = parse_qs(parsed.query)
            if qs.get("token", [""])[0] == self.auth_token:
                clean_qs = {k: v for k, v in qs.items() if k != "token"}
                from urllib.parse import urlencode
                new_query = urlencode([(k, v[0]) for k, v in clean_qs.items()])
                target = parsed.path + ("?" + new_query if new_query else "")
                self.send_response(302)
                self.send_header("Set-Cookie",
                    f"{self.AUTH_COOKIE}={self.auth_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")
                self.send_header("Location", target)
                self.end_headers()
                return False
            return True

        # No token or wrong token → reject
        if parsed.path.startswith("/api/"):
            try: self.respond_json({"error": "Authentication required"}, status=401)
            except Exception: pass
        else:
            self._serve_auth_page(parsed.path)
        return False

    def _serve_auth_page(self, return_to):
        from html import escape
        body = (
            "<!DOCTYPE html><html><head><meta charset=utf-8>"
            "<title>CyberHub — Authentication</title>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>"
            "body{background:#0a0c10;color:#c8cdd8;font-family:system-ui,-apple-system,sans-serif;"
            "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
            "form{background:#141820;border:1px solid #1e2433;border-radius:10px;padding:28px 32px;"
            "max-width:400px;width:90%;box-shadow:0 8px 24px rgba(0,0,0,.5)}"
            "h1{font-size:16px;color:#4a9eff;margin:0 0 8px;font-weight:600}"
            "p{font-size:13px;color:#6b7280;margin:0 0 18px;line-height:1.5}"
            "label{display:block;font-size:11px;color:#9ca3af;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}"
            "input{width:100%;background:#1a1f2a;border:1px solid #1e2433;color:#e8ecf4;"
            "padding:10px 12px;border-radius:6px;font:inherit;font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;box-sizing:border-box}"
            "input:focus{border-color:#4a9eff}"
            "button{width:100%;margin-top:14px;background:#4a9eff;color:#fff;border:none;padding:10px;"
            "border-radius:6px;font:inherit;font-weight:600;font-size:13px;cursor:pointer}"
            "button:hover{background:#2d6ab3}"
            "</style></head><body>"
            f"<form method='GET' action='{escape(return_to)}'>"
            "<h1>&#x2B21; CyberHub</h1>"
            "<p>This hub is running in LAN mode. Enter the access token from the server console.</p>"
            "<label for='t'>Access token</label>"
            "<input id='t' name='token' autocomplete='off' autofocus required>"
            "<button type='submit'>Continue</button>"
            "</form></body></html>"
        )
        data = body.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    # ─── Small helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _int(qs, key, default=1, min_val=1, max_val=100000):
        try:
            v = int(qs.get(key, [str(default)])[0])
            return max(min_val, min(v, max_val))
        except (ValueError, IndexError):
            return default

    # ─── Dispatch ────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._check_auth(parsed):
            return
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 0. Static font files
        if path.startswith("/fonts/"):
            fname = os.path.basename(unquote(path[7:]))
            if fname.endswith(".woff2") and ".." not in fname:
                _hub_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                fpath = os.path.join(_hub_root, "resources", "fonts", fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "font/woff2")
                    self.send_header("Content-Length", len(data))
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_error(404)
            return

        # 0b. Local ONNX runtime — ort.min.js plus the WASM backend files it
        # dynamically imports (e.g. ort-wasm-simd-threaded.jsep.mjs / .wasm).
        if path.startswith("/onnx/"):
            fname = path[len("/onnx/"):]
            # Only allow flat filenames from the danbooru asset dir — no traversal.
            if fname and "/" not in fname and ".." not in fname:
                _hub_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                fpath = os.path.join(_hub_root, "resources", "danbooru", fname)
                if os.path.isfile(fpath):
                    if fname.endswith(".wasm"):
                        ctype = "application/wasm"
                    elif fname.endswith((".mjs", ".js")):
                        ctype = "text/javascript"
                    else:
                        ctype = "application/octet-stream"
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", len(data))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_error(404)
            return

        # 0c. In-app help, rendered from the user manual (single source of truth).
        if path == "/help":
            from core import help as _help
            body = '<div class="help-page">' + _help.full_html() + '</div>'
            html = build_shell(self.registry, self.settings, active_key="",
                               page_title="Help", body_html=body)
            self.respond_html(html)
            return
        if path.startswith("/help/section/"):
            from core import help as _help
            key = unquote(path[len("/help/section/"):]).strip("/")
            mod = self.registry.get(key)
            frag = _help.section_html(mod.name) if mod else ""
            self.respond_html(frag or
                '<p>No help available for this page yet. '
                'See the <a href="/help">full manual</a>.</p>')
            return

        # 1. Exact GET routes
        route = self.registry.get_routes.get(path)
        if route:
            _, handler = route
            try:
                handler(self, qs)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                if path.startswith("/api/"):
                    try: self.respond_json({"error": str(e)}, status=500)
                    except Exception: pass
                else:
                    self.send_error(500)
            return

        # 2. Prefix routes (thumb, image, …)
        for prefix, (_, handler) in self.registry.prefix_routes.items():
            if path.startswith(prefix):
                rel = unquote(path[len(prefix):])
                try:
                    handler(self, rel)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

        # 3. Root → startup module
        if path == "/" or path == "":
            startup = self.settings.get("startup", "gallery")
            if not self.registry.get(startup):
                # Fall back to the first registered, visible module
                tabs = self.registry.visible_tabs()
                startup = tabs[0].key() if tabs else "settings"
            self.send_response(302)
            self.send_header("Location", f"/{startup}")
            self.end_headers()
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._check_auth(parsed):
            return
        path = parsed.path
        content_type = self.headers.get("Content-Type", "")
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_len = 0

        # Wrap rfile so we can tell whether the handler consumed the request body.
        # If we don't, an endpoint that ignores its body (e.g. an action handler that
        # just kicks off work) leaves the bytes sitting in the socket buffer. With
        # keep-alive on, the next request reads those bytes as the start of its own
        # request line and the server replies 501 ("Unsupported method '{}GET'").
        orig_rfile = self.rfile
        wrapper = _BodyCountingReader(orig_rfile, content_len)
        self.rfile = wrapper

        route = self.registry.post_routes.get(path)
        try:
            if route:
                _, handler = route
                try:
                    handler(self, content_len, content_type)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:
                    try: self.respond_json({"error": str(e)}, status=500)
                    except Exception: pass
            else:
                self.send_error(404)
        finally:
            self.rfile = orig_rfile
            # Drain unread bytes in chunks so a 100 MB upload doesn't get loaded into
            # memory just to be discarded.
            left = wrapper._remaining
            while left > 0:
                try:
                    block = orig_rfile.read(min(left, 65536))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                if not block:
                    break
                left -= len(block)

    # ─── Response helpers (used by all modules) ─────────────────────────────
    def respond_html(self, html, status=200):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, obj, status=200):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        ae = self.headers.get("Accept-Encoding", "")
        if "gzip" in ae and len(data) > 512:
            data = _gzip.compress(data, compresslevel=1)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)

    def respond_binary(self, data, mime, download_name=None):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(data))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        else:
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.wfile.write(data)

    def serve_file(self, filepath, immutable=False):
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(filepath)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", size)
            if immutable:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass

    def read_body_json(self, content_len):
        body = self.rfile.read(content_len) if content_len > 0 else b""
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return None

    def parse_multipart(self, content_len, content_type, max_upload=None):
        """Parse multipart/form-data into {name: {"data": bytes, "filename": str, "content_type": str}}."""
        MAX_UPLOAD = max_upload or (100 * 1024 * 1024)  # default: 100 MB
        if content_len > MAX_UPLOAD:
            max_mb = MAX_UPLOAD // 1024 // 1024
            raise ValueError(f"Upload too large ({content_len // 1024 // 1024} MB). Maximum is {max_mb} MB.")
        body = self.rfile.read(content_len)
        files = {}
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
                break
        if not boundary:
            return files
        boundary_bytes = b"--" + boundary.encode("utf-8")
        for chunk in body.split(boundary_bytes):
            if not chunk or chunk in (b"--", b"--\r\n"):
                continue
            if b"\r\n\r\n" not in chunk:
                continue
            header_block, content = chunk.split(b"\r\n\r\n", 1)
            if content.endswith(b"\r\n"):
                content = content[:-2]
            name = None
            filename = ""
            part_ct = ""
            for line in header_block.decode("utf-8", errors="replace").split("\r\n"):
                lower = line.lower()
                if lower.startswith("content-disposition:"):
                    for param in line.split(";"):
                        param = param.strip()
                        if param.startswith("name="):
                            name = param[5:].strip('"')
                        elif param.startswith("filename="):
                            filename = param[9:].strip('"')
                elif lower.startswith("content-type:"):
                    part_ct = line.split(":", 1)[1].strip()
            if name and content:
                files[name] = {"data": content, "filename": filename, "content_type": part_ct}
        return files


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Suppress connection-reset noise from browsers closing tabs
        pass
