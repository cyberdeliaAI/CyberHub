"""WD-EVA02 ONNX inference helpers for CyberHub Auto Tagger.

The implementation follows SmilingWolf's public preprocessing contract while
remaining lightweight: no pandas, torch or Hugging Face client is required.
Heavy dependencies are imported only when the model is actually loaded.
"""

from __future__ import annotations

import csv
import os
import site
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


CATEGORY_GENERAL = 0
CATEGORY_CHARACTER = 4
CATEGORY_RATING = 9
CATEGORY_NAMES = {
    CATEGORY_GENERAL: "general",
    CATEGORY_CHARACTER: "character",
    CATEGORY_RATING: "rating",
}

_DLL_DIRECTORY_HANDLES = []
_DLL_DIRECTORY_PATHS = set()


@dataclass(frozen=True)
class Tag:
    name: str
    category: str
    score: float


@dataclass
class TagResult:
    general: List[Tag] = field(default_factory=list)
    character: List[Tag] = field(default_factory=list)
    rating: List[Tag] = field(default_factory=list)

    @property
    def all(self) -> List[Tag]:
        return self.rating + self.character + self.general

    @property
    def predicted_rating(self) -> str:
        return self.rating[0].name if self.rating else ""


def mcut_threshold(values, floor: float = 0.0) -> float:
    """Return the Maximum Cut threshold used by the official WD demo."""
    if values is None or len(values) < 2:
        return float(floor)
    ordered = sorted((float(value) for value in values), reverse=True)
    best_index = max(
        range(len(ordered) - 1),
        key=lambda index: ordered[index] - ordered[index + 1],
    )
    return max(float(floor), (ordered[best_index] + ordered[best_index + 1]) / 2.0)


def available_providers():
    try:
        _register_nvidia_dll_dirs()
        import onnxruntime as ort
        _preload_cuda_dlls(ort)
        return list(ort.get_available_providers())
    except Exception:
        return []


def _register_nvidia_dll_dirs():
    """Expose pip-installed NVIDIA DLLs before ONNX Runtime is imported."""
    if os.name != "nt":
        return
    roots = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    directories = []
    for root in roots:
        nvidia_root = Path(root) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for bin_dir in nvidia_root.glob("*/bin"):
            key = str(bin_dir.resolve())
            if key not in directories:
                directories.append(key)

    if directories:
        current = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
        known = {os.path.normcase(part) for part in current}
        prepend = [part for part in directories if os.path.normcase(part) not in known]
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + current)

    if hasattr(os, "add_dll_directory"):
        for key in directories:
            if key in _DLL_DIRECTORY_PATHS:
                continue
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(key))
                _DLL_DIRECTORY_PATHS.add(key)
            except OSError:
                pass


def _preload_cuda_dlls(ort):
    """Preload CUDA/cuDNN from NVIDIA wheels instead of a partial system copy."""
    _register_nvidia_dll_dirs()
    preload = getattr(ort, "preload_dlls", None)
    if not callable(preload):
        return
    # An empty directory explicitly selects NVIDIA pip site-packages. This keeps
    # a partial system cuDNN installation from winning the Windows DLL search.
    try:
        preload(directory="")
    except TypeError:
        try:
            preload()
        except Exception:
            pass
    except Exception:
        try:
            preload()
        except Exception:
            pass


def _provider_candidates(preference: str, available: Sequence[str]):
    preference = (preference or "Auto").strip().lower()
    aliases = {
        "cuda": "CUDAExecutionProvider",
        "coreml": "CoreMLExecutionProvider",
        "directml": "DmlExecutionProvider",
        "cpu": "CPUExecutionProvider",
    }
    hardware = [
        provider for provider in (
            "CUDAExecutionProvider",
            "CoreMLExecutionProvider",
            "DmlExecutionProvider",
        ) if provider in available
    ]
    cpu = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else []
    requested = aliases.get(preference)
    candidates = []
    if requested and requested in available:
        candidates.append([requested] + (cpu if requested != "CPUExecutionProvider" else []))
    elif preference == "auto":
        candidates.append(hardware + cpu)
    if cpu and cpu not in candidates:
        candidates.append(cpu)
    return [candidate for candidate in candidates if candidate]


class WDTagger:
    """Lazy, thread-safe wrapper for WD-EVA02 ONNX models."""

    def __init__(self, model_dir, provider="Auto"):
        self.model_dir = Path(model_dir)
        self.model_path = self.model_dir / "model.onnx"
        self.csv_path = self.model_dir / "selected_tags.csv"
        self.provider_preference = provider
        self._session = None
        self._load_lock = threading.Lock()
        self._names = []
        self._categories = []
        self._category_indexes = {}
        self._input_name = ""
        self._output_name = ""
        self._input_layout = "NHWC"
        self._target_size = 448
        self.device = ""

    @property
    def loaded(self):
        return self._session is not None

    @property
    def target_size(self):
        return self._target_size

    def load(self):
        if self._session is not None:
            return self
        with self._load_lock:
            if self._session is not None:
                return self
            if not self.model_path.is_file() or not self.csv_path.is_file():
                raise FileNotFoundError(
                    f"Expected model.onnx and selected_tags.csv in {self.model_dir}"
                )

            _register_nvidia_dll_dirs()
            import onnxruntime as ort

            _preload_cuda_dlls(ort)
            self._load_vocabulary()
            available = list(ort.get_available_providers())
            candidates = _provider_candidates(self.provider_preference, available)
            if not candidates:
                raise RuntimeError("ONNX Runtime has no usable execution provider")

            error = None
            for providers in candidates:
                try:
                    options = ort.SessionOptions()
                    options.log_severity_level = 3
                    self._session = ort.InferenceSession(
                        str(self.model_path), sess_options=options, providers=providers
                    )
                    disable_fallback = getattr(self._session, "disable_fallback", None)
                    if callable(disable_fallback):
                        disable_fallback()
                    break
                except Exception as exc:
                    error = exc
                    self._session = None
            if self._session is None:
                raise RuntimeError(f"Could not load Auto Tagger model: {error}")

            model_input = self._session.get_inputs()[0]
            shape = list(model_input.shape or [])
            if len(shape) != 4:
                self._session = None
                raise RuntimeError(f"Unsupported model input shape: {shape}")
            if isinstance(shape[-1], int) and shape[-1] in (1, 3, 4):
                self._input_layout = "NHWC"
                spatial = shape[1:3]
            elif isinstance(shape[1], int) and shape[1] in (1, 3, 4):
                self._input_layout = "NCHW"
                spatial = shape[2:4]
            else:
                self._input_layout = "NHWC"
                spatial = shape[1:3]
            sizes = [value for value in spatial if isinstance(value, int) and value > 0]
            self._target_size = sizes[0] if sizes else 448
            if any(value != self._target_size for value in sizes):
                self._session = None
                raise RuntimeError(f"Non-square model input is not supported: {shape}")

            self._input_name = model_input.name
            outputs = self._session.get_outputs()
            if not outputs:
                self._session = None
                raise RuntimeError("The ONNX model exposes no output")
            self._output_name = outputs[0].name
            output_shape = list(outputs[0].shape or [])
            expected = output_shape[-1] if output_shape else None
            if isinstance(expected, int) and expected != len(self._names):
                self._session = None
                raise RuntimeError(
                    f"Model has {expected} outputs but selected_tags.csv has {len(self._names)} rows"
                )
            active = self._session.get_providers()
            self.device = active[0] if active else "Unknown"
        return self

    def unload(self):
        with self._load_lock:
            self._session = None
            self.device = ""

    def warmup(self):
        """Run one unpersisted image to validate and warm the active provider."""
        from PIL import Image

        self.load()
        image = Image.new("RGB", (self._target_size, self._target_size), "white")
        try:
            self.tag_image(image)
        finally:
            image.close()
        return self.device

    def _load_vocabulary(self):
        names = []
        categories = []
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "name" not in reader.fieldnames or "category" not in reader.fieldnames:
                raise RuntimeError("selected_tags.csv must contain name and category columns")
            for row in reader:
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                try:
                    category = int(float(row.get("category", "-1")))
                except (TypeError, ValueError):
                    category = -1
                names.append(name)
                categories.append(category)
        if not names:
            raise RuntimeError("selected_tags.csv contains no tags")
        self._names = names
        self._categories = categories
        self._category_indexes = {
            category: [index for index, value in enumerate(categories) if value == category]
            for category in (CATEGORY_GENERAL, CATEGORY_CHARACTER, CATEGORY_RATING)
        }

    def _prepare_image(self, image):
        import numpy as np
        from PIL import Image

        rgba = image.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, (255, 255, 255))
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
        max_dim = max(rgb.size)
        if max_dim != self._target_size:
            resampling = getattr(Image, "Resampling", Image).BICUBIC
            scale = self._target_size / max_dim
            resized = rgb.resize(
                (
                    max(1, round(rgb.size[0] * scale)),
                    max(1, round(rgb.size[1] * scale)),
                ),
                resampling,
            )
            rgb.close()
            rgb = resized
        left = (self._target_size - rgb.size[0]) // 2
        top = (self._target_size - rgb.size[1]) // 2
        padded = Image.new(
            "RGB", (self._target_size, self._target_size), (255, 255, 255)
        )
        padded.paste(rgb, (left, top))
        rgb.close()
        array = np.asarray(padded, dtype=np.float32)[:, :, ::-1]
        padded.close()
        if self._input_layout == "NCHW":
            array = np.transpose(array, (2, 0, 1))
        return array

    def tag_images(
        self,
        images: Iterable,
        general_threshold=0.35,
        character_threshold=0.85,
        adaptive_threshold=False,
    ) -> List[TagResult]:
        import numpy as np

        self.load()
        prepared = [self._prepare_image(image) for image in images]
        if not prepared:
            return []
        batch = np.stack(prepared, axis=0)
        predictions = self._session.run(
            [self._output_name], {self._input_name: batch}
        )[0]
        active = self._session.get_providers()
        self.device = active[0] if active else "Unknown"
        if predictions.ndim == 1:
            predictions = predictions[None, :]
        if predictions.shape[-1] != len(self._names):
            raise RuntimeError(
                f"Model returned {predictions.shape[-1]} scores for {len(self._names)} tags"
            )
        return [
            self._postprocess(
                row,
                general_threshold=float(general_threshold),
                character_threshold=float(character_threshold),
                adaptive_threshold=bool(adaptive_threshold),
            )
            for row in predictions
        ]

    def tag_image(self, image, **kwargs) -> TagResult:
        return self.tag_images([image], **kwargs)[0]

    def _postprocess(
        self,
        scores,
        general_threshold=0.35,
        character_threshold=0.85,
        adaptive_threshold=False,
    ):
        general_indexes = self._category_indexes.get(CATEGORY_GENERAL, [])
        character_indexes = self._category_indexes.get(CATEGORY_CHARACTER, [])
        rating_indexes = self._category_indexes.get(CATEGORY_RATING, [])

        if adaptive_threshold:
            general_threshold = mcut_threshold([scores[index] for index in general_indexes])
            character_threshold = mcut_threshold(
                [scores[index] for index in character_indexes], floor=0.15
            )

        general = [
            Tag(self._names[index], "general", float(scores[index]))
            for index in general_indexes if float(scores[index]) > general_threshold
        ]
        character = [
            Tag(self._names[index], "character", float(scores[index]))
            for index in character_indexes if float(scores[index]) > character_threshold
        ]
        rating = []
        if rating_indexes:
            best = max(rating_indexes, key=lambda index: float(scores[index]))
            rating = [Tag(self._names[best], "rating", float(scores[best]))]
        general.sort(key=lambda tag: tag.score, reverse=True)
        character.sort(key=lambda tag: tag.score, reverse=True)
        return TagResult(general=general, character=character, rating=rating)
