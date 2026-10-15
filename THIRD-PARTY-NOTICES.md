# Third-Party Notices

CyberHub's own code and protected assets are licensed under the CyberHub End User
License Agreement. This file lists third-party components distributed with, or
downloaded by, CyberHub. Each keeps its own license, which governs that component.

Items marked **[downloaded]** are fetched separately. The Full Bundle already
includes the browser ONNX runtime and the official Danbooru/Upscaler model assets;
fonts and the optional Gallery Auto Tagger model retain their separate download
behavior. Each component keeps its own terms.

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

### ONNX Runtime Web (`ort.min.js`, `ort-wasm-simd-threaded.jsep.*`)
© Microsoft Corporation — **MIT License** (see MIT text below). The Full Bundle
includes version 1.21.0 for Danbooru; minimal distributions can fetch it on first run.

### Danbooru auto-tagger model (`resources/danbooru/model_fp16.onnx`, `tags.csv`, `tags.json`)
The bundled file is the FP16 ONNX conversion by **mmmm3** of
[Danbooru Autotagger](https://github.com/danbooru/autotagger), a ResNet-152 tagger.
The converter publishes it under **MIT**:
https://huggingface.co/mmmm3/danbooru-autotagger-onnx/tree/d777ba70d4611d234dbc7cf18f7da41c5381f728

The exact bundled `model_fp16.onnx` is 126,110,506 bytes, SHA-256
`e561c01bc237f011be7d54c8448fe5ef006df7f96c73d0ef4a3cedbb3f5dbcd1`,
matching the upstream LFS object. The 5,501 output labels match the values and
order in Danbooru Autotagger's `data/tags.json` at revision
`1edfba77c94d77246595d684c501da3abb8b1ccc`. Danbooru Autotagger's MIT text is
included in `licenses/Danbooru-Autotagger-MIT.txt`. The separate `tags.csv` is the
toolkit's Danbooru tag lookup data, not the model's output-label ordering.
Detailed artifact provenance is recorded in
`resources/provenance/danbooru-autotagger.json`.

### WD EVA02-Large Tagger v3 — **[downloaded]**
The optional Gallery Auto Tagger downloads `model.onnx` and `selected_tags.csv`
from **SmilingWolf/wd-eva02-large-tagger-v3** at a pinned upstream revision:
https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3

**License: Apache License 2.0.** The model is not included in CyberHub release
archives; it is downloaded only after the user clicks **Install model**.

### Prompting guides (`resources/guides/*.pdf`)
The **Illustrious / NoobAI Prompting Master Guide** and **Pony Prompting Master
Guide** are original Cyberdelia works. Authorship was confirmed by the project
owner on 15 October 2026. They are covered by CyberHub's own terms in `LICENSE.md`;
no third-party authorship or external license is claimed for these PDFs.

---

## Python runtime dependencies (installed via pip, not bundled)

CyberHub installs these at setup; they are not redistributed in the zip and keep
their own licenses: Pillow (HPND/MIT-CMU), send2trash (BSD), requests (Apache-2.0),
watchdog (Apache-2.0), numpy (BSD), opencv-python-headless (Apache-2.0),
onnxruntime (MIT), httpx (BSD), aiofiles (Apache-2.0), tqdm (MPL-2.0/MIT),
tenacity (Apache-2.0).

The separately distributed **Upscaler beta** module can run user-supplied ONNX
models. Its package also bundles `resources/upscalers/Real-ESRGAN-x4plus.onnx`.
Real-ESRGAN is **BSD-3-Clause**, © 2021 Xintao Wang. Its full upstream license
is included in `licenses/Real-ESRGAN-BSD-3-Clause.txt`:
https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE
Other ESRGAN/Real-ESRGAN models remain under their respective authors' terms.

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
