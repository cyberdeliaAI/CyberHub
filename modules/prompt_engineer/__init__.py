"""Prompt Engineer module — LM Studio prompt workflow embedded in CyberHub.

The supplied Prompt-Engineer standalone HTML uses Tailwind CDN + a 2100-line
inline script for the chat logic, prompt registry and LM Studio API streaming.
Re-implementing that natively would be a major undertaking, so instead we:

  1. Load the standalone HTML once at startup
  2. Splice its <style> block and <body> content into the hub shell
  3. Apply CSS overrides that map the standalone's red-glass palette to
     the hub's flat dark-blue aesthetic and hide the standalone's own
     header bar (the hub topbar already labels the page)

Tailwind CDN is loaded only when the Prompt Engineer page is visited. The
standalone JS is left untouched — it references DOM elements by ID and those
IDs survive the splice.

If the standalone HTML is ever updated, drop the new file in static/ and
the next page reload picks it up automatically.
"""

import os
import re
from core import Module
from core.server import build_shell, _font_links


# ─── CSS overrides — map the standalone's red/glass palette to the hub ──────

OVERRIDES_CSS = r"""
/* ── Prompt Engineer hub overrides ─────────────────────────────────────────
   Applied AFTER the standalone's own <style> so they win the cascade.
   Goal: keep the standalone's layout/JS intact, but match hub aesthetics. */

/* The standalone forces body { height:100vh; overflow:hidden } and a radial
   background. Inside the hub it's no longer the <body> — it's a child of
   .hub-content. Override so it fills the hub content area. */
body {
    background: var(--bg-darkest) !important;
    overflow: auto !important;
    height: auto !important;
    font-family: var(--font) !important;
    color: var(--text) !important;
}
.hub-content { padding: 0; overflow: hidden; }

/* Outer container: standalone uses a max-7xl glass card centred in viewport.
   In the hub we want it to fill the available content area edge-to-edge. */
.hub-content > .pe-root {
    width: 100%; height: 100%; max-width: none !important;
    background: var(--bg-darkest);
    border-radius: 0; border: 0; box-shadow: none;
    display: flex; flex-direction: column;
    overflow: hidden;
}
.hub-content > .pe-root.card {
    /* override the standalone's .card glassmorphism */
    background: var(--bg-darkest) !important;
    backdrop-filter: none !important;
    border: 0 !important;
    box-shadow: none !important;
}

/* Standalone shows a red accent-line bar at the top of the card. The hub
   topbar already provides the page chrome — hide it. */
.pe-root > .accent-line { display: none !important; }

/* Standalone's own "PROMPT-ENGINEER V3.0" sidebar header — redundant inside
   the hub since the hub topbar already labels the page. */
#sidebar > .p-5.border-b.shrink-0 { display: none !important; }
#sidebar #settings-panel { padding-top: 14px; }

/* ── Palette: standalone uses red, hub uses blue accent ───────────────────
   We rebind the CSS variables the standalone defines, then patch the
   handful of Tailwind utility classes that hardcode red. */
.pe-root {
    --glass-bg: var(--bg-panel);
    --glass-border: var(--border);
    --accent-1: var(--accent);
    --neon: linear-gradient(90deg, var(--accent) 0%, var(--accent-dim) 100%);
    --text-primary: var(--text-bright);
    --text-secondary: var(--text);
    --text-muted: var(--text-dim);
}

/* Sidebar background and borders */
#sidebar { background: var(--bg-panel) !important; border-right: 1px solid var(--border) !important; }
#sidebar header, #sidebar .border-b, #sidebar .border-t {
    border-color: var(--border) !important;
}

/* Main area background — the standalone uses a black gradient; flatten it */
#main-area { background: var(--bg-darkest) !important; }
#main-area > header { background: var(--bg-panel) !important; border-bottom: 1px solid var(--border) !important; backdrop-filter: none !important; }
#main-area > div:last-child { background: var(--bg-panel) !important; border-top: 1px solid var(--border) !important; backdrop-filter: none !important; }

/* Inputs — replace the dark-glass look with hub input style */
.input-dark {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 6px !important;
    font-family: var(--font) !important;
}
.input-dark:focus { border-color: var(--accent) !important; background: var(--bg-card) !important; }

/* Sliders — accent the thumb and the focused track */
input[type=range]::-webkit-slider-thumb {
    background: var(--accent) !important;
    box-shadow: 0 0 6px rgba(74, 158, 255, 0.5) !important;
}
input[type=range]::-moz-range-thumb {
    background: var(--accent) !important;
    border: 0 !important;
}
input[type=range]::-webkit-slider-runnable-track {
    background: var(--bg-card) !important; border: 1px solid var(--border) !important;
}

/* Range readout — standalone uses .text-red-400; remap to hub accent */
.text-red-400, .text-red-300, .text-red-500 { color: var(--accent) !important; }
.bg-red-500\/10 { background: rgba(74, 158, 255, 0.10) !important; }
.bg-red-500\/20 { background: rgba(74, 158, 255, 0.20) !important; }
.border-red-500\/20 { border-color: rgba(74, 158, 255, 0.30) !important; }
.hover\:bg-red-500\/20:hover { background: rgba(74, 158, 255, 0.20) !important; }

/* Send button: was a red gradient — make it the hub accent solid button */
#send-btn {
    background: var(--accent) !important;
    color: #fff !important;
    box-shadow: 0 4px 14px rgba(74, 158, 255, 0.25) !important;
}
#send-btn:hover { background: var(--accent-dim) !important; transform: none !important; }

/* Chat bubbles — bot message border-left was red */
.msg-bot {
    border-left-color: var(--accent) !important;
    background: var(--bg-panel) !important;
    font-family: var(--mono) !important;
}
.msg-user { background: var(--bg-hover) !important; }

/* Mode badges — keep their semantic colors (green/orange/purple) but
   align them with hub palette tones. Standalone definitions already
   look fine on a dark bg; only nudge the borders. */

/* Status dot — green when ready, neutral red shown via inline class.
   Map the inline shadow/bg to hub green/red. */
#status-dot.bg-green-900 { background: var(--green) !important; box-shadow: 0 0 8px var(--green) !important; }

/* btn-action (small uppercase buttons): flat hub-style */
.btn-action {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 5px !important;
}
.btn-action:hover { background: var(--bg-hover) !important; color: var(--text-bright) !important; border-color: var(--accent) !important; }

/* header-btn (small icon buttons in header) */
.header-btn { background: var(--bg-card) !important; border: 1px solid var(--border) !important; color: var(--text-dim) !important; }
.header-btn:hover { background: var(--bg-hover) !important; color: var(--text-bright) !important; }

/* Toast: standalone uses red-tinted glass; use hub accent */
#toast-container .toast {
    background: var(--bg-panel) !important;
    border: 1px solid var(--border) !important;
    border-left: 3px solid var(--accent) !important;
    color: var(--text) !important;
    backdrop-filter: none !important;
}

/* Modal: glass-on-glass → hub panel */
.modal-overlay { background: rgba(0,0,0,.65) !important; backdrop-filter: blur(2px) !important; }
.modal-box { background: var(--bg-panel) !important; border: 1px solid var(--border) !important; }
#modal-save { background: var(--accent) !important; }
#modal-save:hover { background: var(--accent-dim) !important; }

/* Drop-overlay (when dragging an image over the chat) */
#drop-overlay { background: rgba(15, 18, 24, 0.85) !important; }
#drop-overlay .border-red-400\/60 { border-color: var(--accent) !important; }
#drop-overlay .text-red-400 { color: var(--accent) !important; }
#drop-overlay .text-red-300 { color: var(--accent) !important; }

/* Scrollbar — already hub-style globally via _BASE_CSS, but the standalone
   defines its own override; just align it. */
::-webkit-scrollbar-thumb { background: var(--border-light) !important; }
::-webkit-scrollbar-thumb:hover { background: var(--text-dim) !important; }

/* Misc: standalone uses red-500/600 hover states on specific buttons */
.hover\:text-red-500:hover, .hover\:text-red-300:hover, .hover\:text-red-400:hover { color: var(--accent) !important; }
.from-red-600, .to-red-800 { /* gradient stops — overridden via #send-btn already */ }
.shadow-red-900\/40 { box-shadow: 0 4px 14px rgba(74, 158, 255, 0.25) !important; }
.focus\:border-red-500:focus { border-color: var(--accent) !important; }
"""


# ─── HTML splicing ──────────────────────────────────────────────────────────

_RE_STYLE  = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_RE_BODY   = re.compile(r"<body[^>]*>(.*?)</body>", re.S | re.I)


def _splice_standalone(html):
    """Pull out the <style> contents and the body inner HTML.

    Returns (style_text, body_inner) or (None, None) if the file doesn't
    look like the expected standalone.
    """
    style_m = _RE_STYLE.search(html)
    body_m  = _RE_BODY.search(html)
    if not style_m or not body_m:
        return None, None
    return style_m.group(1), body_m.group(1)


# ─── Module ─────────────────────────────────────────────────────────────────

class PromptEngineerModule(Module):
    name = "Prompt Engineer"
    version = "1.2"
    icon = "\u2728"  # ✨
    description = "Build, rewrite and generate image prompts through a local LM Studio model."
    order = 38
    settings_schema = {}

    def key(self):
        # Stable URL/settings key without spaces in the navigation path.
        return "prompt-engineer"

    def __init__(self, hub):
        super().__init__(hub)
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")
        # Locally-bundled front-end assets (Tailwind + Feather) live here so the tool
        # can run fully offline. Falls back to CDN only if a file is missing.
        self.assets_dir = os.path.join(hub.resources_dir, "prompt-engineer")

    def routes_get(self):
        return {
            "/prompt-engineer": self._page,
            "/prompt-engineer/guide": self._guide,
        }

    def prefix_routes(self):
        return {"/pe-assets/": self._serve_asset}

    def _serve_asset(self, handler, tail):
        """Serve a locally-bundled asset from resources/prompt-engineer/.
        Only whitelisted basenames; no path traversal."""
        allowed = {"tailwind.css": "text/css; charset=utf-8",
                   "feather.min.js": "application/javascript"}
        name = os.path.basename(tail.split("?", 1)[0])
        mime = allowed.get(name)
        if not mime:
            handler.respond_json({"error": "Not found"}, status=404); return
        fpath = os.path.join(self.assets_dir, name)
        if not os.path.isfile(fpath):
            handler.respond_json({"error": "Not found"}, status=404); return
        with open(fpath, "rb") as f:
            data = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", len(data))
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.end_headers()
        handler.wfile.write(data)

    def _has_local_asset(self, name):
        return os.path.isfile(os.path.join(self.assets_dir, name))

    def _page(self, handler, qs):
        try:
            with open(os.path.join(self.static_dir, "prompt-engineer.html"),
                      encoding="utf-8") as f:
                raw = f.read()
        except OSError as exc:
            handler.respond_html(
                f"<div style='padding:32px;color:#f87171'>Prompt Engineer HTML missing: {exc}</div>",
                status=503,
            )
            return

        style_text, body_inner = _splice_standalone(raw)
        if style_text is None:
            handler.respond_html(
                "<div style='padding:32px;color:#f87171'>"
                "Prompt Engineer HTML could not be parsed (no &lt;style&gt; or &lt;body&gt; block found)."
                "</div>", status=500)
            return

        # head_extra is appended inside the <style> block of build_shell.
        # We close that block to inject Tailwind stylesheet + Feather scripts, then open
        # a fresh <style> so the document remains valid.
        # Tailwind is pre-built from v3.4 (no runtime JIT) so we get a stable layout no
        # matter what cdn.tailwindcss.com serves at any given time.
        tw_present = self._has_local_asset("tailwind.css")
        feather_src = "/pe-assets/feather.min.js" if self._has_local_asset("feather.min.js") \
                      else "https://cdn.jsdelivr.net/npm/feather-icons/dist/feather.min.js"
        font_block = _font_links()
        head_extra = (
            f"{style_text}\n{OVERRIDES_CSS}\n"
            "</style>\n"
            + ('<link rel="stylesheet" href="/pe-assets/tailwind.css">\n' if tw_present
               else '<script src="https://cdn.tailwindcss.com"></script>\n')
            + f'<script src="{feather_src}"></script>\n'
            + f'{font_block}\n'
            + "<style>\n"
        )

        # Wrap the standalone's body inner content with a .pe-root marker so
        # CSS overrides can target it precisely. The original body had only
        # one direct child (the .card div); add the marker to that.
        body_inner = body_inner.replace(
            'class="w-full max-w-7xl h-full card',
            'class="pe-root w-full max-w-7xl h-full card', 1)

        page = build_shell(
            self.hub.registry, self.hub.settings,
            active_key=self.key(),
            page_title="Prompt Engineer",
            body_html=body_inner,
            head_extra=head_extra,
        )
        handler.respond_html(page)

    def _guide(self, handler, qs):
        handler.serve_file(os.path.join(self.static_dir, "setup-guide.html"))
