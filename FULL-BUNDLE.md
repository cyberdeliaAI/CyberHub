# CyberHub FULL Bundle

**The complete CyberHub toolkit in one download. All 16 official modules included.**

Core **{{VERSION}}** · local test build **{{BUILD_DATE}}** · **not published**.
Prepared for Civitai distribution. GitHub Lite/system packages use the same Core;
the Full Bundle itself is kept separate. No release or tag has been published.
This bundle includes the current Core working-tree changes, including optional
update notifications, expanded GPU/ONNX diagnostics and corrected module switches.
System components: Settings **1.6.0**, Module Manager **1.1.1**. Independently
versioned official modules retain their existing versions.

## Start here

1. Extract the ZIP into a **new folder**. Open `CyberHub-Full-Bundle`.
2. Install Python 3.10–3.14 if needed. Python 3.12 or 3.13 is recommended.
   **Intel macOS:** use Python 3.10–3.13. Python 3.14 has no compatible Intel
   macOS ONNX Runtime wheel for Auto Tagger/Upscaler; the launcher detects this.
3. Start CyberHub:
   - **Windows:** double-click `start.bat`.
   - **macOS:** open Terminal in this folder and run `bash start.sh`.
   - **Linux:** open a terminal in this folder and run `bash start.sh`.
     On Debian/Ubuntu, the `python3-venv` package may be needed.
4. Open the localhost URL printed in the terminal.
5. Add your image folders in Settings. Gallery is the default starting page.

The first launch creates a local Python environment and installs the packages for
all included modules. An internet connection is needed for package installation
and the first font download. No Python interpreter, virtual environment, CUDA
installation or personal data is included in this archive.

**Use this bundle for a new installation.** Keep your existing installation for
comparison. Do not import the Full Bundle as a system update or extract it over
an existing installation: it contains every module, and could replace versions
you updated independently. Existing installations can continue using the usual
Core and individual module updates.

## Included modules

| Module | Version | Channel |
| --- | --- | --- |
{{MODULE_TABLE}}

Settings and Module Manager are included system components. Civitai Browser and
Upscaler are beta modules and keep their beta labels. Modules remain modular and
can be updated separately through Module Manager; Core and its system components
update together through Settings. Update checks and installation are manual by
default. Automatic notification checks are optional and disabled by default.
The Core update checker reads GitHub releases. Once the matching Lite/system
release is published there, its system updater also updates this Full Bundle's
Core without replacing optional modules. Module Manager continues using the
official module Registry. The local candidates are not available online yet.

## AI models and connections

- **Danbooru:** the model, tag data, browser ONNX runtime and prompting guides
  distributed with the official Danbooru package are included.
- **Upscaler beta:** the official package's default Real-ESRGAN-x4plus ONNX model
  is included. Additional models remain optional.
- **Gallery Auto Tagger:** the module is included; its approximately 1.3 GB WD
  model is downloaded only when you select **Install model** in Auto Tagger.
- **Captioner and Prompt Engineer:** configure a local OpenAI-compatible model
  server, such as LM Studio, through Settings → AI connection. A language/vision
  model server and its models are separate installations.
- NVIDIA GPU acceleration requires a compatible driver and GPU runtime in the
  CyberHub environment. Selecting CUDA does not install those components. The
  launcher preserves an already importable ONNX Runtime variant and installs a
  CPU fallback only when no runtime is importable.

The application runs locally. Downloads and configured AI connections retain
their existing behavior; this is not an offline installer. The supplied Docker
files install the requirements of all included modules, but an actual container
build/run has not been validated on this machine. Use native startup for this
test build.

## Testing and support

Open Settings → System diagnostics → Refresh → Copy as text to report the running
Python environment, all detected GPUs, ONNX providers and loaded Auto Tagger
session provider. No model is loaded merely to gather diagnostics.

The bundle has no configured image folders, API keys, database, thumbnail cache,
download history or Gallery Auto Tagger model. Runtime data is created only after
you launch it. Read `LICENSE.md` and `THIRD-PARTY-NOTICES.md`; module-specific
notices are under `licenses/modules/`. Model and guide provenance is recorded
under `resources/provenance/`.

Build provenance and exact included component versions are recorded in
`resources/bundle.json`. This is one CyberHub application with all modules
preinstalled; it does not revive a separate legacy Full codebase.
