"""Compare — Side-by-side image comparison with settings and prompt diff."""

import os
from core import Module
from core.server import build_shell


class CompareModule(Module):
    name = "Compare"
    icon = "\U0001F50D"
    description = "Compare 2\u20134 images side by side with metadata diff."
    order = 15
    settings_schema = {}
    show_in_tabs = False  # Only accessible from Gallery multi-select

    def routes_get(self):
        return {"/compare": self._page}

    def _page(self, handler, qs):
        handler.respond_html(build_shell(self.hub.registry, self.hub.settings,
            active_key="compare", page_title="Compare", body_html=PAGE_BODY))



PAGE_BODY = r"""
<style>
.cmp-wrap{padding:16px;max-width:1600px;margin:0 auto;font-size:14px}
.cmp-header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.cmp-header h2{font-size:16px;font-weight:500;margin:0;color:var(--text)}
.cmp-header .back{color:var(--text-dim);text-decoration:none;font-size:13px}
.cmp-header .back:hover{color:var(--text)}
.cmp-tabs{display:flex;gap:2px;background:var(--bg-input);border-radius:6px;padding:2px}
.cmp-tab{padding:6px 16px;border-radius:5px;cursor:pointer;font-size:13px;color:var(--text-dim);transition:all .12s}
.cmp-tab:hover{color:var(--text)}
.cmp-tab.act{background:var(--bg-panel);color:var(--text);font-weight:500}

.cmp-grid{display:grid;gap:12px;margin-bottom:16px}
.cmp-grid.n2{grid-template-columns:1fr 1fr}
.cmp-grid.n3{grid-template-columns:1fr 1fr 1fr}
.cmp-grid.n4{grid-template-columns:1fr 1fr 1fr 1fr}
@media (max-width:1100px){.cmp-grid.n3,.cmp-grid.n4{grid-template-columns:1fr 1fr}}
@media (max-width:680px){.cmp-grid.n2,.cmp-grid.n3,.cmp-grid.n4{grid-template-columns:1fr}}

.cmp-img-card{background:var(--bg-panel);border:2px solid var(--border);border-radius:8px;overflow:hidden;position:relative}
.cmp-img-card img{width:100%;aspect-ratio:1;object-fit:contain;background:var(--bg-main,#111)}
.cmp-img-label{padding:8px 10px;font-size:11px;font-family:var(--mono,monospace);color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.cmp-colors{display:flex;gap:0}
.cmp-dot{width:12px;height:12px;border-radius:50%;display:inline-block;margin-right:4px;flex-shrink:0}

.cmp-section{background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:12px}
.cmp-section-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text-dim);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.cmp-row{display:flex;align-items:baseline;gap:8px;padding:4px 0;font-size:13px;border-bottom:1px solid var(--border-subtle,rgba(128,128,128,.1))}
.cmp-row:last-child{border-bottom:none}
.cmp-key{color:var(--text-dim);min-width:100px;font-size:12px}
.cmp-val{color:var(--text);font-family:var(--mono,monospace);font-size:12px;word-break:break-word}

.cmp-tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px;cursor:default;transition:opacity .15s}
.cmp-tag.shared{background:var(--bg-input);color:var(--text-dim)}
.cmp-tag.partial{background:var(--bg-input);color:var(--text);border:1px solid var(--border)}
.cmp-tag.unique{font-weight:500}
.cmp-tag.dim{opacity:.25}

.cmp-unique-block{margin-top:8px;padding:8px 10px;border-radius:6px;border:2px solid var(--border)}
.cmp-unique-title{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;display:flex;align-items:center;gap:4px}

.cmp-partial-block{margin-top:8px}
.cmp-partial-row{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:12px}
.cmp-partial-dots{display:flex;gap:2px}
.cmp-partial-label{color:var(--text);font-family:var(--mono,monospace)}
.cmp-partial-count{color:var(--text-dim);font-size:11px}

.cmp-empty{text-align:center;padding:60px 20px;color:var(--text-dim)}
.cmp-loading{text-align:center;padding:40px;color:var(--text-dim)}
</style>

<div class="cmp-wrap" id="cmpWrap">
  <div class="cmp-loading" id="cmpLoading">Loading images and metadata...</div>
</div>

<script>
var IMG_COLORS = ['#4a9eff','#f0c040','#50c878','#e05080'];
var paths = [], metas = [], mode = 'settings';

function init() {
    var params = new URLSearchParams(window.location.search);
    /* New form: ?path=a&path=b — handles commas in filenames correctly because
       URLSearchParams.getAll() preserves each value as a single decoded string. */
    paths = params.getAll('path').filter(function(p){return p.trim();}).slice(0,4);
    /* Backward-compat: support old ?paths=a,b URLs (won't be generated anymore but may
       exist in bookmarks/history). Skipped if the new form already returned values. */
    if (!paths.length) {
        var raw = params.get('paths') || '';
        paths = raw.split(',').filter(function(p){return p.trim();}).slice(0,4);
    }
    if (paths.length < 2) {
        document.getElementById('cmpWrap').innerHTML = '<div class="cmp-empty">Select 2\u20134 images in the Gallery to compare.<br><br><a href="/gallery" style="color:var(--accent,#4a9eff)">Back to Gallery</a></div>';
        return;
    }
    // Fetch metadata for all
    Promise.all(paths.map(function(p){
        return fetch('/api/metadata?path='+encodeURIComponent(p)).then(function(r){return r.json()});
    })).then(function(results){
        metas = results;
        render();
    }).catch(function(e){
        document.getElementById('cmpWrap').innerHTML = '<div class="cmp-empty">Error loading metadata: '+e+'</div>';
    });
}

function render() {
    var n = paths.length;
    var h = '<div class="cmp-header">';
    h += '<a class="back" href="/gallery">&larr; Gallery</a>';
    h += '<h2>Comparing '+n+' images</h2>';
    h += '<div class="cmp-tabs">';
    h += '<div class="cmp-tab'+(mode==='settings'?' act':'')+'" onclick="mode=\'settings\';render()">Settings</div>';
    h += '<div class="cmp-tab'+(mode==='prompt'?' act':'')+'" onclick="mode=\'prompt\';render()">Prompt</div>';
    h += '</div></div>';

    // Image grid
    h += '<div class="cmp-grid n'+n+'">';
    for (var i = 0; i < n; i++) {
        h += '<div class="cmp-img-card" style="border-color:'+IMG_COLORS[i]+'">';
        h += '<img src="/image/'+encodeURIComponent(paths[i])+'" loading="lazy">';
        h += '<div class="cmp-img-label"><span class="cmp-dot" style="background:'+IMG_COLORS[i]+'"></span>'+esc(paths[i].split('/').pop())+'</div>';
        h += '</div>';
    }
    h += '</div>';

    if (mode === 'settings') h += renderSettingsDiff();
    else h += renderPromptDiff();

    document.getElementById('cmpWrap').innerHTML = h;
}

function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}

// ─── Settings Diff ─────────────────────────────────────────────────

function getSettings(idx) {
    var m = metas[idx]; if (!m) return {};
    var p = m.parsed || {}, info = m.info || {};
    var s = {};
    // The hub's metadata parser nests A1111-style generation settings under
    // parsed.settings as { Steps, Sampler, "CFG scale", Seed, Model, ... } — these are
    // the wire-format keys, capitalized. Flatten them so the diff sees real values.
    if (p.settings && typeof p.settings === 'object') {
        for (var sk in p.settings) {
            var sv = p.settings[sk];
            if (sv !== undefined && sv !== null && sv !== '') s[sk] = String(sv);
        }
    }
    // Image dimensions from the info block
    if (info.width && info.height) s['dimensions'] = info.width + ' x ' + info.height;
    // Catch non-standard primitive keys at the top level of parsed (other parsers/formats).
    // Skip the structural fields and anything that's not a primitive — those don't fit as a
    // single setting value (e.g. `raw` is the full param dump, `settings` is the dict we just flattened).
    for (var k in p) {
        if (k === 'prompt' || k === 'negative_prompt' || k === 'workflow' || k === 'raw' || k === 'settings') continue;
        if (s[k] !== undefined) continue;
        var v = p[k];
        if (v === undefined || v === null || v === '') continue;
        if (typeof v === 'object') continue;
        s[k] = String(v);
    }
    return s;
}

function renderSettingsDiff() {
    var n = paths.length;
    var allSettings = paths.map(function(_,i){return getSettings(i);});

    // Collect all keys
    var allKeys = {};
    allSettings.forEach(function(s){for(var k in s) allKeys[k]=true;});
    var keys = Object.keys(allKeys).sort();

    // Classify: shared, partial, unique
    var shared = [], partial = [], unique = [];
    keys.forEach(function(k){
        var vals = allSettings.map(function(s){return s[k]||'';});
        var nonEmpty = vals.filter(function(v){return v;});
        if (nonEmpty.length === 0) return;
        var allSame = nonEmpty.length === n && nonEmpty.every(function(v){return v === nonEmpty[0];});
        if (allSame) { shared.push({key:k, value:nonEmpty[0]}); }
        else if (nonEmpty.length === 1) {
            var idx = vals.findIndex(function(v){return v;});
            unique.push({key:k, value:nonEmpty[0], idx:idx});
        } else {
            partial.push({key:k, vals:vals});
        }
    });

    var h = '';

    // Shared
    if (shared.length) {
        h += '<div class="cmp-section"><div class="cmp-section-title">&#x2713; Shared settings (identical in all '+n+' images)</div>';
        shared.forEach(function(s){
            h += '<div class="cmp-row"><span class="cmp-key">'+esc(s.key)+'</span><span class="cmp-val">'+esc(s.value)+'</span></div>';
        });
        h += '</div>';
    }

    // Partial (different values)
    if (partial.length) {
        h += '<div class="cmp-section"><div class="cmp-section-title">&#x2194; Different settings</div>';
        partial.forEach(function(p){
            h += '<div class="cmp-row"><span class="cmp-key">'+esc(p.key)+'</span><span class="cmp-val">';
            p.vals.forEach(function(v,i){
                if (v) h += '<span class="cmp-dot" style="background:'+IMG_COLORS[i]+'"></span>'+esc(v)+'&nbsp;&nbsp;';
            });
            h += '</span></div>';
        });
        h += '</div>';
    }

    // Unique
    if (unique.length) {
        h += '<div class="cmp-section"><div class="cmp-section-title">&#x2022; Unique settings (only in one image)</div>';
        unique.forEach(function(u){
            h += '<div class="cmp-row"><span class="cmp-key">'+esc(u.key)+'</span><span class="cmp-val"><span class="cmp-dot" style="background:'+IMG_COLORS[u.idx]+'"></span>'+esc(u.value)+'</span></div>';
        });
        h += '</div>';
    }

    if (!shared.length && !partial.length && !unique.length) {
        h += '<div class="cmp-section"><div style="color:var(--text-dim);text-align:center;padding:20px">No settings metadata found</div></div>';
    }
    return h;
}

// ─── Prompt Diff ───────────────────────────────────────────────────

function tokenize(prompt) {
    if (!prompt) return [];
    return prompt.split(',').map(function(t){return t.trim();}).filter(function(t){return t;});
}

function renderPromptDiff() {
    var n = paths.length;
    var prompts = metas.map(function(m){return (m.parsed||{}).prompt||'';});
    var negatives = metas.map(function(m){return (m.parsed||{}).negative_prompt||'';});

    var allTokens = prompts.map(tokenize);
    var allNegTokens = negatives.map(tokenize);

    var h = '';
    h += renderTokenDiff('Positive prompt tags', allTokens, n);
    h += renderTokenDiff('Negative prompt tags', allNegTokens, n);
    return h;
}

function renderTokenDiff(title, tokenSets, n) {
    // Count occurrences of each token across images
    var tokenMap = {};
    tokenSets.forEach(function(tokens, idx){
        tokens.forEach(function(t){
            var key = t.toLowerCase();
            if (!tokenMap[key]) tokenMap[key] = {text:t, indices:new Set(), count:0};
            tokenMap[key].indices.add(idx);
            tokenMap[key].count++;
        });
    });

    var allTokenKeys = Object.keys(tokenMap);
    if (!allTokenKeys.length) return '';

    var shared = [], partial = [], unique = [];
    allTokenKeys.forEach(function(k){
        var t = tokenMap[k];
        if (t.indices.size === n) shared.push(t);
        else if (t.indices.size === 1) unique.push(t);
        else partial.push(t);
    });
    // Sort partial by count desc
    partial.sort(function(a,b){return b.count - a.count;});

    var h = '<div class="cmp-section"><div class="cmp-section-title">'+esc(title)+'</div>';

    // Shared tags
    if (shared.length) {
        h += '<div style="margin-bottom:8px"><span style="font-size:11px;color:var(--text-dim);font-weight:600">SHARED (all '+n+')</span><br>';
        shared.forEach(function(t){
            h += '<span class="cmp-tag shared">'+esc(t.text)+'</span>';
        });
        h += '</div>';
    }

    // Partial tags with colored dots
    if (partial.length) {
        h += '<div class="cmp-partial-block"><span style="font-size:11px;color:var(--text-dim);font-weight:600">PARTIAL</span>';
        partial.forEach(function(t){
            h += '<div class="cmp-partial-row" onmouseenter="highlightImgs(['+Array.from(t.indices).join(',')+'])" onmouseleave="highlightImgs(null)">';
            h += '<div class="cmp-partial-dots">';
            for (var i=0;i<n;i++){
                h += '<span class="cmp-dot" style="background:'+(t.indices.has(i)?IMG_COLORS[i]:'transparent')+';border:1px solid '+(t.indices.has(i)?IMG_COLORS[i]:'var(--border)')+'"></span>';
            }
            h += '</div>';
            h += '<span class="cmp-partial-label">'+esc(t.text)+'</span>';
            h += '<span class="cmp-partial-count">('+t.indices.size+'/'+n+')</span>';
            h += '</div>';
        });
        h += '</div>';
    }

    // Unique tags per image
    if (unique.length) {
        var byImg = {};
        unique.forEach(function(t){
            var idx = Array.from(t.indices)[0];
            if (!byImg[idx]) byImg[idx] = [];
            byImg[idx].push(t);
        });
        for (var i=0;i<n;i++){
            if (!byImg[i] || !byImg[i].length) continue;
            h += '<div class="cmp-unique-block" style="border-color:'+IMG_COLORS[i]+'">';
            h += '<div class="cmp-unique-title"><span class="cmp-dot" style="background:'+IMG_COLORS[i]+'"></span>Only in image '+(i+1)+'</div>';
            byImg[i].forEach(function(t){
                h += '<span class="cmp-tag unique" style="background:'+IMG_COLORS[i]+'22;color:'+IMG_COLORS[i]+'">'+esc(t.text)+'</span>';
            });
            h += '</div>';
        }
    }

    h += '</div>';
    return h;
}

function highlightImgs(indices) {
    var cards = document.querySelectorAll('.cmp-img-card');
    cards.forEach(function(c, i){
        if (!indices) { c.style.opacity = '1'; return; }
        c.style.opacity = indices.indexOf(i) >= 0 ? '1' : '0.3';
    });
}

init();
</script>
"""
