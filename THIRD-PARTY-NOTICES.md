# Third-Party Notices

CyberHub's own code and protected assets are licensed under the CyberHub End User
License Agreement. This file lists third-party components distributed with, or
downloaded by, CyberHub. Each keeps its own license, which governs that component.

Items marked **[downloaded]** are not shipped inside the zip; CyberHub fetches them
on first run and they then become part of the running application.

---

## Core Components

### Civitai `models.json` (`resources/civitai/models.json`)
A checkpoint hash → model-name lookup table derived from Civitai. This is data, not
code, and is subject to **Civitai's Terms of Service / API terms**
(https://civitai.com/content/tos). Provided for convenience; redistribute in line
with Civitai's terms.

### UI fonts — **[downloaded]**
Fetched from Google Fonts on first run by `resources/fonts/download_fonts.py`:
- **Inter** — © The Inter Project Authors — SIL Open Font License 1.1
- **IBM Plex Sans** — © IBM Corp. — SIL Open Font License 1.1
- **JetBrains Mono** — © JetBrains s.r.o. — SIL Open Font License 1.1

SIL Open Font License 1.1: https://openfontlicense.org

---

## Bundled and Optional Module Components

### Civitai image downloader (`resources/civitai-grabber/civit_image_downloader.py`)
From **CivitAI_Image_grabber** by Confuzu
(https://github.com/Confuzu/CivitAI_Image_grabber).
**License: GNU General Public License v3.0 (GPL-3.0).** The full text is provided in
`licenses/GPL-3.0.txt`. CyberHub invokes this script as a **separate subprocess**;
it is aggregated with, not linked into, CyberHub. Your rights in this specific file
are governed by GPL-3.0 — you may use, modify, and redistribute *this file* under
GPL-3.0, and its source is included.

### Feather Icons (`resources/prompt-engineer/feather.min.js`)
© Cole Bemis — **MIT License** (see MIT text below).

### Tailwind CSS (`resources/prompt-engineer/tailwind.css`, `tailwind.js`)
© Tailwind Labs, Inc. — **MIT License** (see MIT text below).

### ONNX Runtime Web (`ort.min.js`, `ort-wasm-simd-threaded.jsep.*`) — **[downloaded]**
© Microsoft Corporation — **MIT License** (see MIT text below). Fetched on first run
for the Danbooru auto-tagger.

### Danbooru auto-tagger model (`resources/danbooru/model_fp16.onnx`, `tags.csv`, `tags.json`)
An ONNX image-tagging model and its tag vocabulary. **License: see the model's
upstream source.** ⚠️ *Confirm and fill in the exact model, author, and license here
before publishing CyberHub* (WD-style taggers are commonly Apache-2.0, but verify the
specific model you ship). Danbooru tag *names* are factual labels.

### WD EVA02-Large Tagger v3 — **[downloaded]**
The optional Gallery Auto Tagger downloads `model.onnx` and `selected_tags.csv`
from **SmilingWolf/wd-eva02-large-tagger-v3** at a pinned upstream revision:
https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3

**License: Apache License 2.0.** The model is not included in CyberHub release
archives; it is downloaded only after the user clicks **Install model**.

### Prompting guides (`resources/guides/*.pdf`)
⚠️ *If these PDFs are your own work, no third-party notice is needed and you may
remove this entry. If they are from another author, add their name and terms here.*

---

## Python runtime dependencies (installed via pip, not bundled)

CyberHub installs these at setup; they are not redistributed in the zip and keep
their own licenses: Pillow (HPND/MIT-CMU), send2trash (BSD), requests (Apache-2.0),
watchdog (Apache-2.0), numpy (BSD), opencv-python-headless (Apache-2.0),
onnxruntime (MIT), httpx (BSD), aiofiles (Apache-2.0), tqdm (MPL-2.0/MIT),
tenacity (Apache-2.0).

The separately distributed **Upscaler beta** module can run user-supplied ONNX
models. Its package also bundles `resources/upscalers/Real-ESRGAN-x4plus.onnx`.
Real-ESRGAN is BSD-3-Clause, © Xintao Wang et al.; other ESRGAN/Real-ESRGAN
models remain under their respective authors' terms.

---

## MIT License

The MIT-licensed components above are distributed under the following terms:

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
