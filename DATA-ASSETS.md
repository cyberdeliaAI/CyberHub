# Large data assets (kept out of git)

These files are **not** stored in the repository — they're too large for normal git
(`model_fp16.onnx` alone exceeds GitHub's 100 MB per-file limit) and/or change
independently of the code. Keep them in your local working copy and back them up
separately (cloud storage, external drive, etc.). The build (`build_release.py`)
picks them up from disk.

| File | Size | How to obtain |
|------|------|---------------|
| `resources/auto_tagger/wd-eva02-large-tagger-v3/model.onnx` and `selected_tags.csv` | ~1.3 GB | Auto Tagger -> Install model. Optional runtime download; not included in release zips. |
| `resources/civitai/models.json` | ~14 MB | In-app: Settings → Civitai → Update models. |
| `resources/danbooru/model_fp16.onnx` | ~120 MB | Danbooru auto-tagger model — keep your own copy / re-export from source. |
| `resources/danbooru/tags.csv` | ~8 MB | Danbooru tag database for the toolkit. |
| `resources/danbooru/ort*.js`, `*.mjs`, `*.wasm` | ~23 MB | ONNX Runtime Web — fetched on first run by `download_fonts.py` (when the Danbooru module is present). |
| `resources/upscalers/*.onnx` | varies | ONNX upscaler/detail models for the Upscaler module. Keep these outside git and restore/copy them locally. |
| `resources/fonts/*.woff2` | small | Fetched on first run by `download_fonts.py`. |

Small data files that **do** stay in git: `resources/danbooru/tags.json`,
`resources/danbooru/README.txt`, the guide PDFs, and all README/Markdown docs.

> Reminder: a fresh clone cannot reproduce every release ZIP until the
> bundled release assets are placed back in the paths above.

## Docker

The supplied Compose file persists normal Hub state and the optional Auto Tagger
download in named Docker volumes. Large user-supplied Upscaler models and Gallery
images should be mounted from host folders. The Docker build excludes downloaded
Auto Tagger files and Upscaler models. The default Upscaler model is distributed
with the separate Upscaler beta package.
