# CyberHub

**A local workspace for AI image collections, metadata, prompting and image tools.**

CyberHub runs on your own computer. It can index large image libraries, read
generation metadata, search prompts and models, and provide production tools
without uploading your images to a cloud service.

CyberHub has one main edition. Stable modules ship together and can be enabled
or disabled in Settings. Modules still under active development, including the
Civitai Browser and Upscaler, are published separately as clearly marked beta
packages.

## Highlights

- **Gallery**: fast folder browsing, metadata filters, search, favorites,
  collections, model grouping, keyboard selection and safe system-trash delete.
- **Viewer and Compare**: inspect generation metadata or compare up to four
  images and their settings.
- **LoRA Info**: inspect local `.safetensors` metadata, training tags, tensor
  structure and hashes from either the browser computer or the CyberHub host.
- **Auto Tagger**: create local visual AI tags for Gallery images and search by
  combinations of tags.
- **Captioner**: generate LoRA-training captions with a local vision-language
  model and matching `.txt` sidecar files.
- **Prompt tools**: Prompt Engineer, Prompt Library and the Danbooru toolkit.
- **Image tools**: Cropper, Amateur Photo, Overlay and metadata copy.
- **Civitai tools**: maintain model-name metadata and download image collections
  with Civitai Grabber.
- **Optional beta modules**: Civitai Browser and Upscaler can be installed as
  separate packages without replacing the complete CyberHub installation.

Generation metadata support includes Automatic1111/Forge, ComfyUI, Civitai,
InvokeAI, NovelAI, SwarmUI, Fooocus variants and Easy Diffusion.

## Install

For a normal installation, download the current CyberHub release ZIP and extract
it to its own folder.

- **Windows:** double-click `start.bat`.
- **macOS:** open a terminal in the CyberHub folder and run `./start.sh`.
- **Linux:** open a terminal in the CyberHub folder and run `bash start.sh`.

Using `bash start.sh` on Linux also works when the archive tool did not preserve
the executable permission. On Debian/Ubuntu, install the virtual-environment
package first if needed:

```bash
sudo apt install python3 python3-venv python3-pip
```

CyberHub supports Python 3.10 through 3.13. The start script creates a local
virtual environment and installs the required Python packages. Open the printed
local URL, then use Settings to add your image folders and configure modules.
Detailed Linux and manual installation instructions are available in the
CyberHub manual.

An existing CyberHub installation can be updated from **Settings -> Software
updates -> Check for updates**. This is always a manual action: CyberHub does
not contact GitHub at startup or in the background. Downloads come only from
the official `cyberdeliaAI/CyberHub` GitHub Releases page and are checked
against their published SHA-256 digest before installation. Existing files are
backed up and a restart is requested after a successful install.

You can also extract a complete release over the installation, or use
**Settings -> Maintenance -> Import update or module ZIP** when working offline.
Local `settings.json`, Gallery databases and downloaded models are not part of
the release overwrite flow.

## Docker

Docker users can build and start CyberHub with:

```bash
docker compose up --build -d
docker compose logs cyberhub
```

Open `http://localhost:8899`. Docker networking makes the browser appear as a
remote client, so CyberHub prints an access token in the container log on first
launch. Enter that token on the authentication page.

The supplied Compose configuration:

- runs CyberHub as a non-root user;
- binds port 8899 to host localhost only;
- stores settings, the Gallery database, thumbnails and Library attachments in
  the `cyberhub_data` volume;
- stores the optional Auto Tagger download in a separate persistent volume.

Edit `docker-compose.yml` to mount one or more image folders, for example
`/host/images:/images/output:ro`, then add `/images/output` in Gallery Settings.
Read-only mounts are the safe default; use `:rw` only when CyberHub should be
allowed to alter files in that folder.

For LAN access, change the port mapping to `8899:8899`. Keep authentication
enabled and use the token URL shown by `docker compose logs cyberhub`. Never add
`--no-auth` when the port is exposed beyond host localhost.

## Source Repository

This repository contains the complete CyberHub source, including beta module
source code for collaborative development. Beta modules remain separate from the
stable installation ZIP. Large or independently downloaded runtime assets are
intentionally kept out of git, including some ONNX models and downloaded fonts.
The current Civitai `models.json` lookup database is included and can be updated
from Settings. See
[`DATA-ASSETS.md`](DATA-ASSETS.md) when preparing a source checkout or building a
release.

Build the release files with:

```bash
python build_release.py ~/Downloads
```

The builder creates the stable `cyberhub_v<version>.zip`, separate stable-module
update ZIPs and `cyberhub-update.json`. Upload the catalog and every ZIP listed
inside it as assets of the same public GitHub Release. The in-app update check
cannot read releases from a private repository without credentials.

For a release where an individual module may also be installed on the previous
Hub version, pass that compatibility floor explicitly, for example:

```bash
python build_release.py ~/Downloads --version 1.2.6.1 \
  --module captioner --module-min-hub 1.2.6
```

The resulting release still contains the cumulative stable CyberHub ZIP, but
only adds a separate Captioner ZIP. Repeat `--module` for every independently
installable module changed in that release. Use `--no-module-updates` when only
the main ZIP should be published.

Beta modules are never added to the main ZIP automatically. Build their manual
import packages explicitly, for example:

```bash
python build_release.py ~/Downloads --version 1.1.1-beta --modules-only \
  --module civitai_browser --module upscaler --module-min-hub 1.2.6
```

This produces separate Browser and Upscaler ZIPs. Install either package from
**Settings -> Maintenance -> Import update or module ZIP**, then restart
CyberHub.

## Manual

See [`resources/help/cyberhub-manual.md`](resources/help/cyberhub-manual.md) for
the complete user manual. The same manual is available inside CyberHub from the
help button.

## Privacy

Normal use runs locally over `http://localhost`. Images and prompts remain on
your machine. Optional LAN mode allows access from another device on your own
trusted network and supports access-token protection.

## License

CyberHub's own code and protected assets are provided under the
[`CyberHub End User License Agreement`](LICENSE.md). Bundled and downloaded
third-party components keep their own licenses; see
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## Contributing

Suggestions and focused contributions are welcome. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) before submitting code or documentation.
