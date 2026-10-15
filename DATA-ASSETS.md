# Large data assets (kept out of git)

These files are **not** stored in the repository — they're too large for normal git
(`model_fp16.onnx` alone exceeds GitHub's 100 MB per-file limit) and/or change
independently of the code. Keep them in your local working copy and back them up
separately (cloud storage, external drive, etc.). The legacy `build_release.py`
reference does not identify a builder present in the active repository.
For the unpublished Full Bundle, `tools/build_full_bundle.py` obtains module
runtime/assets from locally archived official ZIPs verified against the Registry,
and checks their tracked runtime files against sibling source repositories.

| File | Size | How to obtain |
|------|------|---------------|
| `resources/auto_tagger/wd-eva02-large-tagger-v3/model.onnx` and `selected_tags.csv` | ~1.3 GB | Auto Tagger -> Install model. Optional runtime download; not included in release zips. |
| `resources/danbooru/model_fp16.onnx` | ~120 MB | FP16 conversion from `mmmm3/danbooru-autotagger-onnx`; exact revision, SHA-256 and MIT source are in `resources/provenance/danbooru-autotagger.json` and `THIRD-PARTY-NOTICES.md`. |
| `resources/danbooru/tags.csv` | ~8 MB | Danbooru tag database for the toolkit. |
| `resources/danbooru/ort*.js`, `*.mjs`, `*.wasm` | ~23 MB | ONNX Runtime Web — fetched on first run by `download_fonts.py` (when the Danbooru module is present). |
| `resources/upscalers/*.onnx` | varies | ONNX upscaler/detail models for the Upscaler module. Keep these outside git and restore/copy them locally. |
| `resources/fonts/*.woff2` | small | Fetched on first run by `download_fonts.py`. |

Data files that **do** stay in git include `resources/civitai/models.json`,
`resources/danbooru/tags.json`, `resources/danbooru/README.txt`, the guide PDFs,
and all README/Markdown docs. The bundled Civitai database can still be refreshed
from Settings.

> Reminder: a fresh clone cannot reproduce every release ZIP until the
> bundled release assets are placed back in the paths above.

## Full Bundle local build

Run from the active Core repository in the multi-repository workspace:

```bash
python3 tools/build_full_bundle.py \
  --output ../_test-builds/CyberHub/full-bundle-v1.5.0-review-2026-10-15 \
  --build-date 2026-10-15
```

The builder uses `module-sources.json`, the sibling Registry catalog, and matching
module archives under `_release-assets/<repository>/` or `_build/`. It does not
download, install packages, commit or publish. A missing/mismatched artifact or
resource aborts the build. Current Core runtime files are read from the working
tree, including the approved uncommitted notification component and provenance
records. Current license notices are added separately from the verified module
runtime bytes; conflicting shared license/resource files abort the build.

The Full Bundle includes the Danbooru and default Upscaler assets supplied in
their official module ZIPs. Auto Tagger's WD model, Python, Python packages and
CUDA are not bundled. Fonts retain the normal first-launch download behavior.
The generated README identifies the test build, all module versions and beta
channels. `resources/bundle.json` records provenance; the adjacent build report
contains each packaged file's digest and origin. Install by extracting to a new
folder, rather than importing this all-module archive over an existing Hub.
The manifest's `fresh_install_only` flag and root `FULL-BUNDLE.json` marker
enforce this separation in current and older allowlist-based importers.
The Full Bundle targets Civitai. Matching Lite/system packages target GitHub;
all candidates remain unpublished. Building packages creates no GitHub commit,
tag or release. The separate local Registry candidate must only be published
after its new module archives are publicly accessible and verified.

## Lite and system packages

`tools/build_core_packages.py` creates `cyberhub_lite_v1.5.0.zip` (Core, Settings,
Module Manager and verified Gallery 1.2.16) and `cyberhub_update_v1.5.0.zip`
(Core/system modules only). `cyberhub-update.json` points only to the latter.
The Lite manifest/root marker reject accidental system-update import in current
and older allowlist-based importers. Both distributions use the owner's Civitai
checkpoint-name lookup export dated 15 October 2026.

`tools/build_module_notice_updates.py` builds the reviewed Danbooru 1.0.1 and
Upscaler 1.1.2-beta notice patches from a frozen baseline Registry and matching
local archives. It checks that module Python ASTs differ only in their version
assignment and that existing runtime assets stay unchanged. New notices, license
texts and provenance records are added separately. It does not publish or modify
the Registry; proposed catalog entries are recorded in its build reports.

## Docker

The supplied Compose file persists normal Hub state and the optional Auto Tagger
download in named Docker volumes. Large user-supplied Upscaler models and Gallery
images should be mounted from host folders. The Docker build excludes downloaded
Auto Tagger files and Upscaler models. The default Upscaler model is distributed
with the separate Upscaler beta package.
