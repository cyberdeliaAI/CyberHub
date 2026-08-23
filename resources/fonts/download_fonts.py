#!/usr/bin/env python3
"""Download Google Fonts for local use in CyberHub."""
import os, re, urllib.request

CSS_URL = "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@300;400;600;800&display=swap"
DIR = os.path.dirname(os.path.abspath(__file__))

print("[FONTS] Fetching font CSS...")
# A full Chrome User-Agent is required: without the "Chrome/..." token Google
# Fonts falls back to serving .ttf instead of the .woff2 files the hub expects.
req = urllib.request.Request(CSS_URL, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})
css = urllib.request.urlopen(req).read().decode()

# Parse font-face blocks
blocks = re.findall(r'@font-face\s*\{([^}]+)\}', css)
count = 0
for block in blocks:
    family = re.search(r"font-family:\s*'([^']+)'", block)
    weight = re.search(r"font-weight:\s*(\d+)", block)
    url = re.search(r"url\(([^)]+\.woff2)\)", block)
    if not (family and weight and url): continue
    name = family.group(1).lower().replace(" ", "-")
    fname = f"{name}-{weight.group(1)}.woff2"
    path = os.path.join(DIR, fname)
    if os.path.exists(path):
        print(f"  [SKIP] {fname} (already exists)")
        continue
    print(f"  [GET]  {fname}")
    urllib.request.urlretrieve(url.group(1), path)
    count += 1

# Also download ONNX Runtime for Danbooru auto-tag. ort.min.js is the loader;
# it dynamically imports the WASM backend (the .jsep.mjs + .wasm pair), so all
# three must be present locally for the auto-tag to work offline. This is only
# needed only when the Danbooru module is installed, so skip the ONNX download
# entirely when modules/danbooru/ is absent.
_danbooru_module = os.path.join(os.path.dirname(os.path.dirname(DIR)), "modules", "danbooru")
if os.path.isdir(_danbooru_module):
    ONNX_BASE = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.21.0/dist/"
    ONNX_FILES = [
        "ort.min.js",
        "ort-wasm-simd-threaded.jsep.mjs",
        "ort-wasm-simd-threaded.jsep.wasm",
    ]
    onnx_dir = os.path.join(os.path.dirname(DIR), "danbooru")
    os.makedirs(onnx_dir, exist_ok=True)
    print("\n[ONNX]  Checking ONNX Runtime files in resources/danbooru/...")
    for fname in ONNX_FILES:
        dest = os.path.join(onnx_dir, fname)
        if os.path.exists(dest):
            print(f"  [SKIP] {fname} (already exists)")
            continue
        print(f"  [GET]  {fname}")
        urllib.request.urlretrieve(ONNX_BASE + fname, dest)
        sz = os.path.getsize(dest) / 1024
        print(f"  [OK]   {sz:.0f} KB")

# Note: Tailwind CSS for the Prompt Engineer is pre-built and shipped at
# resources/prompt-engineer/tailwind.css — no runtime download needed.

print(f"\nDone. {count} font files downloaded.")
print("Restart the hub to use local fonts.")
