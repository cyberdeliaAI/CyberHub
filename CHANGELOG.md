# Changelog

## Settings 1.4.1 - 6 September 2026

### Improved

- Reorganized each module row so its enable/disable control appears before its
  optional Settings button in one consistently aligned action area.
- Module settings now open only from the clearly labelled Settings button; the
  module name and description are no longer hidden click targets.
- Improved the module controls on narrow screens and added accessible labels
  and expanded-state information.

## CyberHub 1.3.0 - Modular Architecture

### Added

- Added the protected Module Manager with separate Installed, Official, Beta
  and Community views.
- Added manual per-module install, update and removal from independent GitHub
  repositories.
- Added a verified central registry with repository, version, dependency, size
  and SHA-256 information for every package.
- Added installed-file ownership records and uninstall backups. Module settings
  and user data are preserved by default.
- Added separate repository and release-package generation for Gallery and all
  optional modules.

### Changed

- CyberHub now has one modular edition. The starter contains Core, Settings,
  Module Manager and Gallery.
- Gallery remains included for new users but is independently versioned and can
  receive its own updates.
- Civitai Browser and Upscaler remain official beta modules and are installed
  separately.
- Update checks remain fully manual. CyberHub does not contact GitHub at startup
  or in the background.

### Security

- Official packages must use GitHub Release assets in the declared Cyberdelia
  repository and match their catalog checksum and size.
- Community packages are confined to their own module and namespaced resource
  folders and show an executable-code warning before installation.
- Settings and Module Manager cannot be disabled or removed.

## Pre-1.3 Development Changes

### Changed

- CyberHub moved from separate Lite and Full editions to one product in
  preparation for the modular 1.3 architecture.
- Civitai Browser and Upscaler are now clearly marked beta modules and are
  distributed as separate import packages instead of being bundled in the main
  CyberHub release.
- Expanded the installation guide with a dedicated Linux setup, including
  `python3-venv`, ZIP permission recovery and complete manual start/stop steps.
- Replaced the separate Lite and Full release builders with one
  `cyberhub_v<version>.zip` build.
- The version entered in the release builder is now also written into the
  packaged Hub, keeping the filename, manifest and in-app version consistent.
- Consolidated the product documentation, manual and license under the CyberHub
  name.
- Moved the official source and update endpoint from `CyberHub-Full` to the new
  public `cyberdeliaAI/CyberHub` repository.
- Replaced the edition-specific GitHub publishing scripts with
  `publish_github.py` for the complete source tree.

### Added

- Added LoRA Info 1.0.3, a read-only Safetensors inspector for embedded model
  metadata, training settings, tag frequencies, tensor statistics and hashes.
- LoRA Info supports both network workflows used elsewhere in CyberHub: select
  a file on the CyberHub computer, or choose/drop a file on the computer running
  the browser. Browser files send only their Safetensors metadata header to the
  Hub rather than uploading the full model.
- The CyberHub computer file browser provides direct shortcuts to Windows drive
  letters, macOS volumes, Linux mount locations and the Hub user's home folder.
- Fixed the module route to follow CyberHub's key-based navigation convention,
  so LoRA Info appears with its own icon in the Modules menu and can be enabled
  or disabled from Settings. The original hyphenated URL remains available.
- LoRA Info now accepts trainer metadata containing the non-standard `Infinity`,
  `-Infinity` and `NaN` constants while always returning browser-compatible JSON.
- Translated every visible LoRA Info label, message and file-browser action to
  English.
- Added the current Civitai `models.json` lookup database to the public source
  repository and release build, so model-name lookup works immediately.
- Added a manual GitHub Releases update flow in Settings. CyberHub only checks
  after the user clicks **Check for updates**; there is no startup or periodic
  network check.
- Added complete-Hub and per-module update choices, release notes, download
  progress and an explicit restart step.
- The release builder now creates an integrity catalog, a complete release
  ZIP and separate module update ZIPs for upload to one GitHub Release.
- Added `--modules-only` to the release builder for creating standalone beta or
  module packages without rebuilding the main CyberHub ZIP.
- Added Docker and Docker Compose support based on the contribution by
  [Owen Knight / PsychoLogicAu](https://github.com/PsychoLogicAu) in
  CyberHub-Lite PR #1.
- Docker stores settings, the shared SQLite database, thumbnails and Library
  attachments in a persistent data volume. The optional Auto Tagger model uses
  a separate persistent volume.
- Added `CYBERHUB_DATA_DIR` for selecting a user-data directory without changing
  the established portable layout of native installations.

### Security and Reliability

- GitHub update catalogs and packages must belong to the official repository,
  use HTTPS and match GitHub's published SHA-256 asset digests. Redirects to
  non-GitHub hosts are rejected.
- Online module packages are confined to their own module plus resources and
  licenses. Python files compile before installation, existing files are backed
  up first and a failed replacement is rolled back.
- The container runs as a dedicated non-root user and includes a health check.
- Compose binds to host localhost by default and keeps CyberHub token
  authentication enabled. LAN exposure requires an explicit port change.
- SIGTERM now requests a normal HTTP-server shutdown without a forced process
  exit.
- Authenticated remote users may use the container folder browser; unauthenticated
  remote browsing remains disabled unless explicitly enabled in Settings.

### Compatibility

- Existing CyberHub installations can be updated in place; local settings,
  databases and downloaded models remain outside the release overwrite flow.
- Older Lite manual paths remain a help-page fallback during incremental
  updates, but new releases contain only the unified manual.
- Older changelog entries below retain their original Lite and Full names as
  historical release records.

### Version Bump

- LoRA Info: 1.0.3
- Captioner: 1.5
- Civitai Browser: 1.1.1-beta
- Upscaler: 1.1.1-beta
- Settings: 1.3.2

## Captioner 1.5 - 28 Aug 2026

### Fixed

- Updating a saved Captioner preset now preserves its Library card type and
  target, so the preset no longer disappears from the Captioner list.
- New and updated presets bypass the browser cache and remain selected by their
  exact card ID, making them visible immediately and handling duplicate titles
  correctly.
- Presets previously displaced by the update bug are recovered through their
  `captioner-preset` Library tag; updating one restores its card type.
- Partial Prompt Library updates now preserve omitted card fields instead of
  resetting them to defaults.

## Gallery 1.2.13 / Settings 1.2.6 - 20 Aug 2026

### Added

- Bulk Gallery deletion now runs as a visible background job with live file
  counts, progress and completion status.
- Settings -> Maintenance now includes **Delete images without metadata**. It
  previews the number of matching images, requires confirmation and sends them
  to the system trash. Only successfully processed images are eligible;
  pending and failed metadata reads are excluded.

### Improved

- Gallery reloads the active folder, search, timeline, favorites, collection or
  AI-tag view after deletion instead of leaving empty spaces on the page.
- When deletion removes the old last page, Gallery automatically opens the new
  final page and selects a nearby remaining image.
- Batch database cleanup is performed together after files reach the system
  trash, reducing per-file database work during large deletions.

### Version Bumps

- Gallery: 1.2.13
- Settings: 1.2.6

## Gallery 1.2.12 - 20 Aug 2026

### Added

- Added a persistent metadata filter with **All images**, **With metadata** and
  **Without metadata** views. The filter also applies to search, favorites,
  timelines, manual collections and AI-tag results.
- **Without metadata** only includes successfully processed files. Images still
  waiting for metadata extraction or carrying a processing error are excluded
  so they cannot be mistaken for safe cleanup candidates.
- Gallery File Info now shows the selected image's server folder and complete
  file path, with copy actions for both values.

### Version Bump

- Gallery: 1.2.12

## CyberHub 1.2.6 - 8 Aug 2026

This is a complete Lite and Full refresh containing every update released after
CyberHub 1.2.5.

### Lite

- Gallery 1.2.11 adds faster two-stage background indexing, visible processing
  progress, faster maintenance jobs, **All images**, model grouping, improved
  keyboard selection and reliable folder restoration between modules.
- Improved generation-metadata parsing, including modern ComfyUI and Krea2
  workflows.
- Updated manuals, licenses, Settings maintenance support and the bundled
  Civitai model-name database.

### Full

- Includes everything in Lite plus every current Full module.
- Added Auto Tagger 1.0.5 with local WD EVA02-Large tagging, folder jobs,
  searchable AI tags, selectable tag combinations and GPU-provider support.
- Added Captioner 1.2 with LM Studio defaults, LoRA trigger support, persistent
  batches, matching `.txt` caption files and the SDXL WD Hybrid workflow.
- Improved Prompt Engineer 1.2, Civitai Browser, Civitai Grabber and Upscaler.
- Full continues to bundle the Danbooru runtime/model, prompting guides, Civitai
  `models.json` and the default Real-ESRGAN upscaler. The optional all-upscaler
  model bundle remains separate, as in the previous Full release.

### Version Bumps

- Hub: 1.2.6
- Gallery: 1.2.11
- Settings: 1.2.5
- Viewer: 1.1
- Captioner: 1.2
- Auto Tagger: 1.0.5
- Prompt Engineer: 1.2

## Captioner 1.2 / Auto Tagger 1.0.5 - 22 Jul 2026

### Added

- Added the optional **SDXL WD Hybrid** Captioner preset. Auto Tagger first
  creates a local WD EVA02-Large tag list, then the vision model refines those
  tags while inspecting the original image.
- Added a configurable WD confidence threshold and a **Constant WD tags** field
  for traits that must be represented only by the LoRA trigger token.
- Added a single-image Auto Tagger endpoint for Captioner. It does not add the
  uploaded image or its temporary tags to the Gallery database.

### Improved

- Hybrid captions remove low-confidence, rating, character-identity, meta and
  quality tags before they reach the vision model.
- Hybrid SDXL captions use readable spaces inside tags while preserving an
  underscore in the configured trigger token.
- Renamed the user-facing **Save sidecars** action to the clearer **Save caption
  files**; exported fallback archives are now named `caption-files.zip`.
- Background Gallery tagging and interactive Captioner tagging now share one
  inference lock, preventing simultaneous ONNX jobs from competing for GPU/CPU
  memory.
- The hybrid preset is only shown when Auto Tagger is installed. Lite and Full
  installations without that optional module keep the normal Captioner presets.

## Captioner 1.1.1 - 22 Jul 2026

### Added

- Added optional `.txt` sidecars for training datasets. **Open folder** grants
  access to the image folder and completed captions are written automatically
  as matching files such as `alex01.png` -> `alex01.txt`.
- Added **Save sidecars**. Browsers without writable-folder access receive a ZIP
  containing one matching `.txt` file per completed image.

### Fixed

- Long Captioner queues now scroll independently, so **Run all**, **Clear** and
  progress remain visible with large batches.

## Captioner 1.1 - 22 Jul 2026

### Added

- Added optional Temperature, Top P, Max Tokens, Top K and Presence Penalty
  overrides. Empty fields use the values configured in LM Studio.
- Added an optional persistent trigger word for LoRA dataset captions.
- Added **Clear generation overrides**.

### Improved

- API URLs now accept host addresses with or without `/v1`, fixing custom LM
  Studio ports such as `http://localhost:8000`.
- Captioner now connects browser-direct like Prompt Engineer, with the existing
  CyberHub-server request path retained as an automatic fallback.
- Empty model and generation fields are omitted from requests instead of sending
  empty or hard-coded default values.
- Replaced the built-in SDXL, Z-Image and Flux scripts with LoRA-training prompts
  that describe only variable dataset elements.
- Trigger placeholders are removed when no trigger is configured; when supplied,
  the trigger is guaranteed at the beginning of the returned caption.

## Prompt Engineer 1.2 - 21 Jul 2026

### Improved

- Generation settings are now optional overrides, matching Manuscript Workbench.
- Empty Temperature, Top P, Top K, Max Tokens and Presence Penalty fields use
  the values configured in LM Studio and are omitted from API requests.
- Added **Clear overrides** to return every generation setting to LM Studio.
- Model detection no longer silently changes generation overrides.

## Prompt Engineer 1.1 - 21 Jul 2026

### Added

- Added a clipboard button and Ctrl+V/Command+V image pasting for Vision prompts.
- Image paste through the keyboard also works when CyberHub is opened over LAN.

### Fixed

- The selected system prompt now survives navigation and page reloads reliably.
- LM Studio preferences now persist immediately, including server address,
  context limit, generation controls and image preprocessing settings.
- Restoring the page no longer lets the default prompt overwrite saved state.

## Auto Tagger 1.0.4 - 15 Jul 2026

### Added

- Added a searchable Gallery-folder selector for tagging one indexed folder.
- Folder jobs include every image in the selected folder and its subfolders.
- Added separate **Tag folder** and **Re-tag folder** actions. The current Gallery
  folder is used as the initial selection when available.

## Auto Tagger 1.0.3 - 15 Jul 2026

### Fixed

- Windows now exposes NVIDIA pip-package DLL folders before ONNX Runtime is
  imported, including cuDNN 9 engine sublibraries loaded during first inference.
- Added a provider warm-up before a tagging job. A broken CUDA/cuDNN installation
  now stops immediately with a useful error instead of silently processing the
  library on CPU.

## Gallery 1.2.11 - 15 Jul 2026

### Improved

- AI tags in the metadata panel are now selectable buttons.
- **Filter selected** searches for images containing every selected tag (AND),
  making combinations such as `fur_trim` and `nose` directly accessible.
- The previous ambiguous **Filter these tags** action no longer silently chooses
  the image's three highest-scoring general tags.

## Auto Tagger 1.0.2 - 15 Jul 2026

### Improved

- Original images are reduced to the model input size during loading instead of
  creating full-resolution RGBA and square intermediate images.
- Added automatic inference batches: CUDA uses 4, CoreML/DirectML use 2 and CPU
  uses 1. A manual value from 1 through 8 remains available in Settings.
- Successful tag results are committed to SQLite once per batch instead of once
  per image.
- Speed and ETA now ignore the first provider warm-up batch and show measured
  images per second.
- Added optional **Gallery thumbnail (fast)** input for servers reading originals
  from NAS, SMB or other network storage. Original input remains the default.
- Unsupported provider batch sizes fall back to batch 1 for the rest of the job.

## Gallery 1.2.10 / Auto Tagger 1.0.1 - 15 Jul 2026

### Fixed

- Auto Tagger now reports the requested and effective execution provider instead
  of showing unrelated providers such as Azure.
- Selecting CUDA now clearly reports **CUDA unavailable; using CPU** when the
  NVIDIA ONNX Runtime package is absent.
- CUDA/cuDNN libraries supplied by PyTorch or NVIDIA pip packages are preloaded
  before creating the ONNX session when supported by ONNX Runtime.
- Start scripts install the CPU runtime only when no ONNX Runtime variant exists,
  so a manually installed `onnxruntime-gpu` is no longer replaced on restart.
- Both `/auto_tagger` and `/auto-tagger` open the module page.
- Gallery now displays visual tags in a separate **AI Tags** tab next to
  **Metadata** and **Raw Metadata** when Auto Tagger is installed.

## Gallery 1.2.9 / Auto Tagger 1.0 / Settings 1.2.5 - 15 Jul 2026

### Added

- Gallery root now opens **All images** across every configured root instead of
  showing an empty page until a folder is selected.
- Added the optional Full **Auto Tagger** module using the local WD EVA02-Large
  Tagger v3 ONNX model. The model is downloaded only after explicit installation.
- Added background tagging with progress, ETA, pause, cancel, safe single-image
  fallback and optional automatic tagging of newly indexed images.
- Added AI tags and confidence scores to the Gallery metadata panel, plus bulk
  tagging for selected images and current folders.
- Added AI-tag search with autocomplete and comma-separated multi-tag matching.
  Search state, sorting, grouping and pagination are preserved when returning to
  Gallery.

### Improved

- Auto Tagger data is removed automatically when its Gallery file is deleted.
- Lite and Full installations without the Auto Tagger module keep the normal
  Gallery interface and make no Auto Tagger API requests.
- Settings number fields now honor an optional decimal step from module schemas.

### Version Bumps

- Gallery: 1.2.9
- Auto Tagger: 1.0
- Settings: 1.2.5

## Gallery 1.2.8 / Settings 1.2.4 - 15 Jul 2026

### Improved

- Gallery maintenance in Settings now shows a live task phase, progress bar,
  processed/total counts, percentage and elapsed time.
- **Re-index folders** now uses the fast discovery scan and only queues new or
  changed images instead of forcing every indexed image through processing again.
- Search rebuild uses database-only keyset batches instead of increasingly
  expensive offset queries. It never reopens source images.
- Generate all thumbnails streams indexed paths from SQLite in bounded batches,
  opens only images with missing or stale thumbnails, and no longer walks every
  folder twice or retains the complete library inventory in memory.
- Optimize database now runs in the background, pauses image processing briefly,
  reports its current phase, and uses bounded SQLite query-statistics maintenance.
  A full database rewrite only runs when more than 10% and at least 5 MB can be
  reclaimed. Routine Optimize no longer performs an unbounded full FTS merge.

### Version Bumps

- Gallery: 1.2.8
- Settings: 1.2.4

## Gallery 1.2.7 - 15 Jul 2026

This Gallery update is focused on responsiveness for very large image libraries.

### Added

- Added two-stage indexing: Gallery first discovers files without opening image
  contents, then processes metadata, dimensions, thumbnails, model grouping,
  tags and search data in the background.
- Added compact scan and background-processing progress in the Gallery status
  bar, including a pause/resume control for background work.
- Added an optional filesystem watcher that applies file changes directly
  instead of rescanning every configured root. Manual re-index remains available
  as a fallback.
- Added Gallery settings for background processing, filesystem watching and the
  number of processing workers. The conservative default is two workers.

### Improved

- Image processing now reuses one file read for metadata, dimensions and the
  thumbnail instead of opening the same image repeatedly.
- Existing model information is backfilled from stored metadata in the
  background, without reopening the source images.
- Folder counts and newest dates are cached. A single file change now refreshes
  only its folder branch instead of recalculating the complete library.
- Folder scans load existing file timestamps once per folder instead of running
  a database query for every image.
- Added composite database indexes for common folder, sort, processing and model
  queries.
- Thumbnail prefetch is smaller and runs when the browser is idle so opening and
  scrolling the Gallery receive priority.

### Notes

- Existing Gallery databases are upgraded automatically. Do not delete or
  rebuild the database after updating.
- The first start can show a one-time background model-information pass for
  images already in the database.
- Opened images and requested thumbnails are given priority while background
  processing continues.

## CyberHub 1.2.5 - 27 Jun 2026

This is a complete Lite and Full refresh after the 1.2 release, focused on
Gallery accessibility, safer update imports, and the new Full modules.

### Added

- Added Full modules to the complete Full build:
  - Civitai Browser
  - Civitai Grabber
  - Upscaler
- Added Gallery setting: **Skip delete confirmation**. When enabled, delete
  actions do not ask again, but files still move to the system trash.
- Added Gallery keyboard multi-selection with `Shift + Arrow keys`.
- Added visible Gallery delete buttons in the metadata panel and fullscreen
  image view.

### Improved

- Gallery keyboard selection now uses an anchor/focus range:
  `Shift + Arrow` expands selection, and reversing direction shrinks it again.
- Gallery `Delete` key works in fullscreen image view.
- Gallery delete confirmation dialog is keyboard-friendly:
  `Left` / `Right` choose Cancel or Delete, `Enter` confirms the focused button,
  and `Escape` cancels.
- Gallery keeps the selection near the deleted image instead of jumping back to
  the first image after delete.
- Gallery cleans empty folder records after deleting files.
- Gallery cleans roots removed from Settings during re-index, without requiring
  users to delete the whole database.
- `Ctrl/Cmd + Click` multi-selection now visibly includes the previously
  selected image when starting a multi-select set.
- Settings update/module ZIP import now stages files inside the CyberHub folder,
  fixing Windows installs where `%TEMP%` is on another drive.
- Windows restart from Settings no longer leaves the previous `cmd` window
  waiting at `Press any key...`.
- `start.sh` is more robust on Linux: it reports found Python versions, explains
  missing `python3-venv`/`venv` package issues, and accepts an existing `venv/`
  folder as well as `.venv/`.

### Version Bumps

- Hub: 1.2.5
- Gallery: 1.2.5
- Settings: 1.2.3
- Viewer: 1.1
- Civitai Browser: 0.2

### Notes

- Deleted files are still sent to the system trash.
- Full includes all current modules. The large optional all-upscaler ONNX bundle
  can still be distributed separately if you do not want the main Full ZIP to be
  very large.
- Restart CyberHub after importing update/module ZIPs.

## CyberHub Lite 1.2 - 22 Jun 2026

This update builds on the Civitai Lite 1.0 release.

### Added

- Added a root `MANUAL.md` for GitHub users and kept the in-app manual at
  `resources/help/cyberhub-lite-manual.md`.
- Added license and third-party notice files to the Lite release zip.
- Added an optional Gallery setting: **Close other folders when opening one**.
- Added ZIP import support in Settings -> Maintenance for update/module packages.

### Improved

- Gallery now hides folders that contain no images and have no image-containing
  subfolders.
- Gallery restores the active folder tree when returning from another module.
- Gallery folder accordion mode now also closes sibling branches when selecting
  a folder without subfolders.
- Gallery folder filtering is faster for large trees by using folder index data
  instead of scanning image rows.
- Viewer and Gallery share the improved metadata reader.
- Viewer no longer shows raw ComfyUI workflow JSON as the prompt when a clean
  prompt can be extracted.
- Improved ComfyUI metadata extraction for modern workflow/node chains.
- Lite Civitai release zips include the current `resources/civitai/models.json`
  for first-run model-name lookup. GitHub source installs can fetch/update it
  from Settings -> Civitai.

### Version Bumps

- Hub: 1.2
- Gallery: 1.2
- Viewer: 1.1
- Settings: 1.1

### Notes

- No re-index is required for the metadata improvements; metadata is parsed live.
- Folder tree changes use the existing Gallery database. Refresh the browser page
  after updating.
