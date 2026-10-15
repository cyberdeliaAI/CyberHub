# CyberHub Lite {{VERSION}}

The basic CyberHub installation: **Core, Settings, Module Manager and Gallery
{{GALLERY_VERSION}}**. Add other official modules whenever you need them.

Built on {{BUILD_DATE}} for GitHub distribution.
Lite and the Civitai Full Bundle use the same Core, Settings and Module Manager;
they differ only in the modules and assets included in the initial download.

## New installation

1. Extract this ZIP into a **new folder**.
2. Install Python 3.10–3.14. Python 3.12 or 3.13 is recommended.
3. Windows: double-click `start.bat`. macOS/Linux: run `bash start.sh` in this folder.
4. Open the printed localhost address and add your image folders in Settings.

The launcher creates a local Python environment and installs packages. Internet
access is needed for package setup and the first font download. The archive has
no Python interpreter, user settings, database, images, API keys or AI models.
The checkpoint-name lookup export is dated **15 October 2026**.

## Add modules and update

Open Module Manager to choose and install additional modules. Gallery, Settings
and Module Manager are immediately available. Civitai Browser and Upscaler retain
their beta status when installed separately.

Use the separate `cyberhub_update_v{{VERSION}}.zip` for an existing installation,
or the normal Settings system updater.
The system package preserves Gallery and all independently installed modules.
Do not import this Lite starter as a system update or extract it over an existing
installation. Its fresh-install manifest and root marker prevent update import.

Core/system updates and individual module updates remain separate. Checks are
manual by default. Optional startup update notifications are disabled by default;
they never install packages automatically.

If you later add Auto Tagger or Upscaler on **Intel macOS**, use Python 3.10–3.13:
ONNX Runtime does not have an Intel macOS Python 3.14 wheel. Those modules may
need separate model downloads and a suitable GPU runtime for acceleration.

Read `LICENSE.md`, `THIRD-PARTY-NOTICES.md` and the notices under `licenses/`.
`resources/distribution.json` records this starter's component versions and
verified Gallery source archive. Full platform/GPU testing remains separate.
