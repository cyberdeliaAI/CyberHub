Default Danbooru filenames:
- tags.csv (Lookup / Builder / Checker)
- model_fp16.onnx (Auto-Tag model)
- tags.json (Auto-Tag labels)
Settings overrides take precedence over these files.

ONNX Runtime (browser) — fetched by resources/fonts/download_fonts.py:
- ort.min.js (loader)
- ort-wasm-simd-threaded.jsep.mjs (WASM backend, dynamically imported by ort.min.js)
- ort-wasm-simd-threaded.jsep.wasm
All three are required for Auto-Tag to run offline; the hub serves them under /onnx/.
