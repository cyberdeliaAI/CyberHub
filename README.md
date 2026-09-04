# CyberHub

**A local workspace for AI image collections, metadata, prompting and image tools.**

CyberHub runs on your own computer. It can index large image libraries, read
generation metadata, search prompts and models, and provide production tools
without uploading your images to a cloud service.

CyberHub has one modular edition. The starter download contains Core, Settings,
Module Manager and Gallery, so it is useful immediately without becoming a
large all-in-one package. Additional stable and beta modules are installed,
updated or removed individually from Module Manager.

## Highlights

- **Module Manager**: manually check the official catalog and choose exactly
  which stable, beta or community modules to install.
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
- **Independent updates**: Gallery and every optional module can be updated
  without replacing unrelated parts of CyberHub.

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

Update CyberHub itself from **Settings -> Software updates -> Check for
updates**. Install or update individual modules from **Module Manager -> Check
for updates**. Both are always manual actions: CyberHub does not contact GitHub
at startup or in the background. Downloads are tied to their declared GitHub
Release repository and checked against a published SHA-256 digest. Existing
files are backed up and a restart is requested after installation.

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

This repository contains CyberHub Core, Settings and Module Manager. Gallery
and the optional tools have independent repositories and release versions. The
starter release combines Core with Gallery for a complete first-run experience.

The central
[`CyberHub-Registry`](https://github.com/cyberdeliaAI/CyberHub-Registry)
contains the catalog that Module Manager reads only after the user clicks
**Check for updates**. Official and community ownership is shown separately
from stable or beta status.

Large or downloaded runtime assets are intentionally kept out of source control,
including some ONNX models and downloaded fonts. The current Civitai
`models.json` lookup database is included in the starter and can be updated from
Settings. See [`DATA-ASSETS.md`](DATA-ASSETS.md) for details.

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
