CyberHub Auto Tagger resources
================================

The optional Auto Tagger module uses:

  SmilingWolf/wd-eva02-large-tagger-v3
  https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3
  License: Apache-2.0

The model is not bundled with CyberHub. Open Auto Tagger and click
"Install model" to download the pinned model.onnx and selected_tags.csv into:

  resources/auto_tagger/wd-eva02-large-tagger-v3/

Tagging and searching run locally after installation. Removing the downloaded
model does not remove tags already stored in the Gallery database.

NVIDIA acceleration (Windows)
-----------------------------
Selecting CUDA in Settings chooses the provider but does not install NVIDIA's
ONNX Runtime package. Stop CyberHub, then run from the CyberHub folder:

  .venv\Scripts\python.exe -m pip uninstall -y onnxruntime onnxruntime-gpu
  .venv\Scripts\python.exe -m pip install --upgrade "onnxruntime-gpu[cuda,cudnn]"

Restart CyberHub. The Runtime card should show CUDA. Azure is not used by this
module as a local execution provider.

If CUDA is listed but the first inference reports a missing
cudnn_engines_tensor_ir64_9.dll, repair the cuDNN wheel and restart CyberHub:

  .venv\Scripts\python.exe -m pip install --upgrade --force-reinstall nvidia-cudnn-cu12

Auto Tagger validates CUDA with a short warm-up before processing the queue. It
will stop with an error instead of silently completing a large job on CPU.

Gallery folder jobs
-------------------
The Background tagging panel can target one folder from the Gallery index.
"Tag folder" processes only images that still need the current tag revision;
"Re-tag folder" replaces results for every image in that folder. Both actions
include subfolders. The most recently opened Gallery folder is selected when it
is available.

Performance
-----------
Inference batch size 0 selects batch 4 for CUDA, 2 for CoreML/DirectML and 1
for CPU. If the server reads originals from a NAS or mapped network drive,
select "Gallery thumbnail (fast)" in Settings -> Auto Tagger. Gallery thumbnail
files preserve the full composition, but can lose some fine-detail tag accuracy.
