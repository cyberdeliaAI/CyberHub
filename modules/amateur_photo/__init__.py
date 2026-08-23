"""Amateur Photo module — apply realistic snapshot / imperfect-camera effects.

Adapted from Amateur Photo Studio v4.0. The original CustomTkinter interface is
translated to a browser UI while the OpenCV filter pipeline remains server-side.
"""

import io
import json
import os
import threading
from pathlib import Path

from core import Module
from core.server import build_shell

try:
    import numpy as np
    import cv2
    from PIL import Image, ImageOps
    HAS_FILTERS = True
    FILTER_ERROR = ""
except ImportError as exc:
    HAS_FILTERS = False
    FILTER_ERROR = str(exc)
    cv2 = None
    np = None
    Image = None
    ImageOps = None

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

_dust_texture_cache = None
_dust_lock = threading.Lock()


def get_dust_texture(width, height):
    global _dust_texture_cache
    with _dust_lock:
        if _dust_texture_cache is None:
            base_h, base_w = 1080, 1920
            dust = np.zeros((base_h, base_w), dtype=np.float32)
            for _ in range(200):
                x = np.random.randint(0, base_w)
                y = np.random.randint(0, base_h)
                r = np.random.randint(1, 5)
                intensity = np.random.uniform(0.2, 0.6)
                cv2.circle(dust, (x, y), r, intensity, -1)
            dust = cv2.GaussianBlur(dust, (5, 5), 0)
            _dust_texture_cache = dust
        return cv2.resize(_dust_texture_cache, (width, height), interpolation=cv2.INTER_LINEAR)


def simulate_jpeg(img_bgr, quality):
    quality = int(max(1, min(100, round(quality))))
    ok, enc = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return img_bgr
    dec = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return dec if dec is not None else img_bgr


def build_motion_kernel(length, angle_deg):
    length = max(1, int(round(length)))
    kernel = np.zeros((length, length), dtype=np.float32)
    kernel[length // 2, :] = 1.0
    kernel /= kernel.sum()
    center = (length / 2, length / 2)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    rot = cv2.warpAffine(kernel, M, (length, length), flags=cv2.INTER_LINEAR)
    s = rot.sum()
    if s > 1e-6:
        rot /= s
    return rot


def add_barrel_distortion(img, k=-0.1):
    h, w = img.shape[:2]
    fx, fy = w, h
    cx, cy = w / 2.0, h / 2.0
    cam_matrix = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float32)
    dist_coeffs = np.array([k, 0, 0, 0, 0], dtype=np.float32)
    map1, map2 = cv2.initUndistortRectifyMap(cam_matrix, dist_coeffs, None, cam_matrix, (w, h), cv2.CV_32FC1)
    return cv2.remap(img, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


# ── Core Filter Pipeline ───────────────────────────────────────────

def apply_amateur_filter(
    img_bgr,
    dr_factor=1.0, hsv_hue_var=2.0, hsv_sat_var=5.0, hsv_val_var=6.0,
    rgb_noise_std=0.007, grain_amount=0.12, grain_size=2,
    beta=0.0, alpha=1.0, gamma=1.0, warmth=0.02, desat=0.9,
    read_noise=0.004, shot_noise=0.02, sharp_radius=3, sharp_amount=0.2,
    use_mid_jpeg=True, mid_jpeg_quality=50,
    use_motion=False, motion_len=5, motion_angle=0.0,
    ca_shift_px=1, vignette_strength=0.2, vignette_feather=0.5,
    distortion_strength=0.0, dust_amount=0.0, random_seed=None,
):
    if random_seed is not None:
        np.random.seed(int(random_seed))

    img01 = img_bgr.astype(np.float32) / 255.0

    # Lens Distortion
    if abs(distortion_strength) > 1e-4:
        img01 = add_barrel_distortion(img01, distortion_strength)

    # Tone
    if abs(dr_factor - 1.0) > 1e-4:
        img01 = np.clip(0.5 + (img01 - 0.5) * float(dr_factor), 0.0, 1.0)
    img01 = np.clip(img01 * float(alpha) + (float(beta) / 255.0), 0.0, 1.0)
    if abs(gamma - 1.0) > 1e-3:
        img01 = np.clip(np.power(img01, max(0.01, float(gamma))), 0.0, 1.0)

    # Color
    b, g, r = cv2.split(img01)
    if warmth != 0.0:
        f = float(warmth)
        g *= 1.0 - f
        b *= 1.0 - f
    img01 = np.clip(cv2.merge([b, g, r]), 0.0, 1.0)

    hsv = cv2.cvtColor((img01 * 255.0).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
    if desat != 1.0:
        hsv[..., 1] *= float(desat)
        hsv[..., 1] = np.clip(hsv[..., 1], 0, 255)

    if any(v > 0 for v in (hsv_hue_var, hsv_sat_var, hsv_val_var)):
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        if hsv_hue_var > 0:
            H = (H + np.random.normal(0.0, float(hsv_hue_var), H.shape)).astype(np.float32) % 180.0
        if hsv_sat_var > 0:
            S = np.clip(S + np.random.normal(0.0, float(hsv_sat_var), S.shape), 0, 255).astype(np.float32)
        if hsv_val_var > 0:
            V = np.clip(V + np.random.normal(0.0, float(hsv_val_var), V.shape), 0, 255).astype(np.float32)
        hsv = cv2.merge([H, S, V])
    img01 = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32) / 255.0

    # Noise
    if read_noise > 0.0 or shot_noise > 0.0:
        var = (float(read_noise) ** 2) + (float(shot_noise) * np.clip(img01, 0.0, 1.0))
        img01 = np.clip(img01 + np.random.normal(0.0, np.sqrt(var), img01.shape).astype(np.float32), 0.0, 1.0)
    if rgb_noise_std > 0.0:
        img01 = np.clip(img01 + np.random.normal(0.0, float(rgb_noise_std), img01.shape).astype(np.float32), 0.0, 1.0)

    # Sharpen
    k = int(sharp_radius)
    k = k + 1 - (k % 2)
    k = max(1, k)
    blurred = cv2.GaussianBlur(img01, (k, k), 0)
    img01 = np.clip(img01 + float(sharp_amount) * (img01 - blurred), 0.0, 1.0)

    out = (img01 * 255).astype(np.uint8)

    # JPEG
    if use_mid_jpeg:
        out = simulate_jpeg(out, mid_jpeg_quality)

    # Motion
    if use_motion and motion_len > 1:
        kernel = build_motion_kernel(motion_len, motion_angle)
        out = cv2.filter2D(out, -1, kernel)

    # Radial CA
    if ca_shift_px != 0:
        h, w = out.shape[:2]
        y, x = np.indices((h, w))
        center_x, center_y = w / 2, h / 2
        dx, dy = x - center_x, y - center_y
        dist = np.sqrt(dx**2 + dy**2)
        max_dist = np.sqrt(center_x**2 + center_y**2)
        norm_dist = dist / (max_dist + 1e-6)
        shift_amount = ca_shift_px * (norm_dist ** 2)

        b, g, r = cv2.split(out)
        map_r_x = (x + dx * shift_amount * 0.05).astype(np.float32)
        map_r_y = (y + dy * shift_amount * 0.05).astype(np.float32)
        map_b_x = (x - dx * shift_amount * 0.05).astype(np.float32)
        map_b_y = (y - dy * shift_amount * 0.05).astype(np.float32)

        r = cv2.remap(r, map_r_x, map_r_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        b = cv2.remap(b, map_b_x, map_b_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        out = cv2.merge([b, g, r])

    # Grain
    if grain_amount > 0.0:
        h, w = out.shape[:2]
        sz = max(1, int(round(grain_size)))
        gh, gw = max(1, h // sz), max(1, w // sz)
        gtex = np.random.rand(gh, gw).astype(np.float32) - 0.5
        gtex = cv2.resize(gtex, (w, h), interpolation=cv2.INTER_LINEAR)
        gtex = np.repeat(gtex[..., None], 3, axis=2)
        img01 = out.astype(np.float32) / 255.0
        img01 = np.clip(img01 + float(grain_amount) * gtex, 0.0, 1.0)
        out = (img01 * 255).astype(np.uint8)

    # Dust
    if dust_amount > 0.0:
        h, w = out.shape[:2]
        dust = get_dust_texture(w, h)[..., None]  # expand to (h, w, 1) for broadcasting
        img01 = out.astype(np.float32) / 255.0
        img01 = np.clip(img01 * (1.0 - dust * dust_amount), 0.0, 1.0)
        out = (img01 * 255).astype(np.uint8)

    # Vignette
    if vignette_strength > 0.0:
        img01 = out.astype(np.float32) / 255.0
        h, w = img01.shape[:2]
        y, x = np.indices((h, w))
        cx, cy = w / 2, h / 2
        r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        r_norm = r / np.sqrt(cx**2 + cy**2)
        feather = max(1e-6, float(vignette_feather))
        mask = 1.0 - float(vignette_strength) * np.power(np.clip((r_norm - (1 - feather)) / feather, 0, 1), 2.0)
        mask = np.repeat(mask[..., None], 3, axis=2).astype(np.float32)
        img01 = np.clip(img01 * mask, 0.0, 1.0)
        out = (img01 * 255).astype(np.uint8)

    return out

PRESETS = {'Gentle Touch': {'description': 'Barely noticeable — just enough to feel real', 'category': 'Subtle', 'params': {'dr_factor': 0.99, 'hsv_hue_var': 0.8, 'hsv_sat_var': 2.0, 'hsv_val_var': 3.0, 'rgb_noise_std': 0.003, 'grain_amount': 0.06, 'grain_size': 2, 'alpha': 1.01, 'beta': 2, 'gamma': 1.01, 'warmth': 0.01, 'desat': 0.95, 'read_noise': 0.002, 'shot_noise': 0.01, 'sharp_radius': 3, 'sharp_amount': 0.15, 'use_mid_jpeg': True, 'mid_jpeg_quality': 72, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 0.3, 'vignette_strength': 0.1, 'vignette_feather': 0.65, 'distortion_strength': -0.02, 'dust_amount': 0.03}}, 'Phone Snap': {'description': 'Quick phone shot — slightly noisy, oversaturated', 'category': 'Subtle', 'params': {'dr_factor': 0.97, 'hsv_hue_var': 1.5, 'hsv_sat_var': 4.0, 'hsv_val_var': 5.0, 'rgb_noise_std': 0.005, 'grain_amount': 0.08, 'grain_size': 1, 'alpha': 1.05, 'beta': 5, 'gamma': 0.97, 'warmth': 0.015, 'desat': 0.93, 'read_noise': 0.004, 'shot_noise': 0.025, 'sharp_radius': 2, 'sharp_amount': 0.3, 'use_mid_jpeg': True, 'mid_jpeg_quality': 60, 'use_motion': False, 'motion_len': 2, 'motion_angle': 0, 'ca_shift_px': 0.5, 'vignette_strength': 0.12, 'vignette_feather': 0.6, 'distortion_strength': -0.04, 'dust_amount': 0.0}}, 'Disposable Camera': {'description': 'Party-vibe disposable — flash-washed, warm, grainy', 'category': 'Analog', 'params': {'dr_factor': 0.9, 'hsv_hue_var': 3.5, 'hsv_sat_var': 9.0, 'hsv_val_var': 10.0, 'rgb_noise_std': 0.012, 'grain_amount': 0.18, 'grain_size': 3, 'alpha': 0.96, 'beta': 8, 'gamma': 1.12, 'warmth': 0.055, 'desat': 0.82, 'read_noise': 0.012, 'shot_noise': 0.055, 'sharp_radius': 5, 'sharp_amount': 0.12, 'use_mid_jpeg': True, 'mid_jpeg_quality': 42, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 1.8, 'vignette_strength': 0.38, 'vignette_feather': 0.42, 'distortion_strength': -0.18, 'dust_amount': 0.28}}, 'Faded Film': {'description': 'Old film stock — lifted blacks, muted tones', 'category': 'Analog', 'params': {'dr_factor': 0.85, 'hsv_hue_var': 2.5, 'hsv_sat_var': 6.0, 'hsv_val_var': 7.0, 'rgb_noise_std': 0.008, 'grain_amount': 0.14, 'grain_size': 3, 'alpha': 0.94, 'beta': 12, 'gamma': 1.08, 'warmth': 0.035, 'desat': 0.78, 'read_noise': 0.008, 'shot_noise': 0.04, 'sharp_radius': 3, 'sharp_amount': 0.1, 'use_mid_jpeg': True, 'mid_jpeg_quality': 55, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 1.2, 'vignette_strength': 0.3, 'vignette_feather': 0.5, 'distortion_strength': -0.08, 'dust_amount': 0.2}}, 'Hard JPEG': {'description': 'Compression hell — heavy blocking, color banding', 'category': 'Digital', 'params': {'dr_factor': 1.0, 'hsv_hue_var': 0.0, 'hsv_sat_var': 0.0, 'hsv_val_var': 0.0, 'rgb_noise_std': 0.0, 'grain_amount': 0.04, 'grain_size': 1, 'alpha': 1.0, 'beta': 0, 'gamma': 1.0, 'warmth': 0.0, 'desat': 0.95, 'read_noise': 0.0, 'shot_noise': 0.0, 'sharp_radius': 1, 'sharp_amount': 0.1, 'use_mid_jpeg': True, 'mid_jpeg_quality': 32, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 0, 'vignette_strength': 0.08, 'vignette_feather': 0.7, 'distortion_strength': 0.0, 'dust_amount': 0.0}}, 'Early Webcam': {'description': '2005 webcam — low-res feel, noisy, washed out', 'category': 'Digital', 'params': {'dr_factor': 0.88, 'hsv_hue_var': 3.0, 'hsv_sat_var': 8.0, 'hsv_val_var': 10.0, 'rgb_noise_std': 0.015, 'grain_amount': 0.1, 'grain_size': 1, 'alpha': 1.1, 'beta': 10, 'gamma': 0.92, 'warmth': 0.01, 'desat': 0.88, 'read_noise': 0.01, 'shot_noise': 0.06, 'sharp_radius': 1, 'sharp_amount': 0.4, 'use_mid_jpeg': True, 'mid_jpeg_quality': 38, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 0.5, 'vignette_strength': 0.15, 'vignette_feather': 0.55, 'distortion_strength': -0.05, 'dust_amount': 0.0}}, 'Shaky Hands': {'description': 'Slight hand tremor — subtle motion blur', 'category': 'Motion', 'params': {'dr_factor': 0.98, 'hsv_hue_var': 1.5, 'hsv_sat_var': 4.0, 'hsv_val_var': 5.0, 'rgb_noise_std': 0.005, 'grain_amount': 0.1, 'grain_size': 2, 'alpha': 1.02, 'beta': 3, 'gamma': 1.03, 'warmth': 0.02, 'desat': 0.91, 'read_noise': 0.004, 'shot_noise': 0.02, 'sharp_radius': 3, 'sharp_amount': 0.18, 'use_mid_jpeg': True, 'mid_jpeg_quality': 55, 'use_motion': True, 'motion_len': 5, 'motion_angle': 4, 'ca_shift_px': 0.8, 'vignette_strength': 0.18, 'vignette_feather': 0.55, 'distortion_strength': -0.06, 'dust_amount': 0.08}}, 'Motion Accident': {'description': 'Caught off-guard — heavy blur, messy exposure', 'category': 'Motion', 'params': {'dr_factor': 0.95, 'hsv_hue_var': 2.5, 'hsv_sat_var': 6.0, 'hsv_val_var': 7.0, 'rgb_noise_std': 0.008, 'grain_amount': 0.14, 'grain_size': 2, 'alpha': 1.04, 'beta': 5, 'gamma': 1.06, 'warmth': 0.025, 'desat': 0.88, 'read_noise': 0.006, 'shot_noise': 0.03, 'sharp_radius': 3, 'sharp_amount': 0.16, 'use_mid_jpeg': True, 'mid_jpeg_quality': 46, 'use_motion': True, 'motion_len': 10, 'motion_angle': 8, 'ca_shift_px': 1.5, 'vignette_strength': 0.3, 'vignette_feather': 0.48, 'distortion_strength': -0.1, 'dust_amount': 0.15}}, 'Night Out': {'description': 'Dark venue — high ISO noise, warm flash spill', 'category': 'Mood', 'params': {'dr_factor': 0.82, 'hsv_hue_var': 4.0, 'hsv_sat_var': 10.0, 'hsv_val_var': 12.0, 'rgb_noise_std': 0.018, 'grain_amount': 0.2, 'grain_size': 2, 'alpha': 0.92, 'beta': -5, 'gamma': 1.15, 'warmth': 0.06, 'desat': 0.8, 'read_noise': 0.015, 'shot_noise': 0.07, 'sharp_radius': 3, 'sharp_amount': 0.1, 'use_mid_jpeg': True, 'mid_jpeg_quality': 45, 'use_motion': True, 'motion_len': 4, 'motion_angle': -3, 'ca_shift_px': 2.0, 'vignette_strength': 0.45, 'vignette_feather': 0.4, 'distortion_strength': -0.12, 'dust_amount': 0.1}}, 'Nostalgic Summer': {'description': 'Warm golden haze — sun-bleached and dreamy', 'category': 'Mood', 'params': {'dr_factor': 0.88, 'hsv_hue_var': 2.0, 'hsv_sat_var': 5.0, 'hsv_val_var': 6.0, 'rgb_noise_std': 0.006, 'grain_amount': 0.12, 'grain_size': 3, 'alpha': 1.0, 'beta': 10, 'gamma': 1.1, 'warmth': 0.07, 'desat': 0.83, 'read_noise': 0.005, 'shot_noise': 0.025, 'sharp_radius': 3, 'sharp_amount': 0.08, 'use_mid_jpeg': True, 'mid_jpeg_quality': 55, 'use_motion': False, 'motion_len': 0, 'motion_angle': 0, 'ca_shift_px': 1.0, 'vignette_strength': 0.25, 'vignette_feather': 0.55, 'distortion_strength': -0.1, 'dust_amount': 0.22}}}
PARAM_SECTIONS = [['Lens', [['Distortion', 'distortion_strength', -0.5, 0.5, 0.01, 'Barrel/pincushion distortion'], ['Chromatic Aberr', 'ca_shift_px', 0, 4, 0.5, 'Radial color fringing'], ['Vignette', 'vignette_strength', 0.0, 0.6, 0.01, 'Edge darkening intensity'], ['Vig. Falloff', 'vignette_feather', 0.2, 0.9, 0.01, 'Vignette gradient softness']]], ['Sensor Noise', [['Film Grain', 'grain_amount', 0.0, 0.5, 0.01, 'Grain overlay intensity'], ['Grain Size', 'grain_size', 1, 6, 1, 'Grain particle scale'], ['Read Noise', 'read_noise', 0.0, 0.02, 0.0005, 'Sensor read noise floor'], ['Shot Noise', 'shot_noise', 0.0, 0.08, 0.001, 'Photon shot noise'], ['RGB Noise', 'rgb_noise_std', 0.0, 0.03, 0.001, 'Random color channel noise'], ['Dust & Specs', 'dust_amount', 0.0, 0.8, 0.01, 'Lens dust particles']]], ['Tone & Exposure', [['Dynamic Range', 'dr_factor', 0.5, 1.5, 0.01, 'Highlight/shadow compression'], ['Contrast', 'alpha', 0.85, 1.25, 0.01, 'Overall contrast'], ['Brightness', 'beta', -20, 20, 1, 'Exposure offset'], ['Gamma', 'gamma', 0.8, 1.4, 0.01, 'Tone curve gamma']]], ['Color', [['Warmth', 'warmth', 0.0, 0.08, 0.001, 'Warm color shift'], ['Desaturation', 'desat', 0.75, 1.0, 0.01, 'Color saturation'], ['Hue Jitter', 'hsv_hue_var', 0.0, 8.0, 0.1, 'Random hue variation'], ['Sat. Jitter', 'hsv_sat_var', 0.0, 20.0, 0.5, 'Random saturation variation'], ['Val. Jitter', 'hsv_val_var', 0.0, 20.0, 0.5, 'Random value variation']]], ['Sharpening', [['Sharp Radius', 'sharp_radius', 1, 9, 1, 'Unsharp mask radius'], ['Sharp Amount', 'sharp_amount', 0.0, 0.6, 0.01, 'Unsharp mask intensity']]], ['Compression', [['JPEG Quality', 'mid_jpeg_quality', 30, 90, 1, 'Mid-pipeline JPEG quality']]], ['Motion Blur', [['Blur Length', 'motion_len', 0, 15, 1, 'Motion blur kernel size'], ['Blur Angle', 'motion_angle', -15, 15, 1, 'Motion blur direction']]], ['Output', [['Final JPEG Q', 'final_jpeg_quality', 30, 100, 1, 'Final output JPEG quality'], ['Random Seed', 'random_seed', -1, 9999, 1, 'Fixed seed (-1 = random)']]]]


def safe_resolve(root, rel_path):
    try:
        root_abs = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(root_abs, rel_path))
        if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
            return None
        return candidate
    except (OSError, ValueError):
        return None


def next_output_path(directory, stem, ext):
    candidate = directory / f"{stem}_amateur{ext}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{stem}_amateur_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


class AmateurPhotoModule(Module):
    name = "Amateur Photo"
    icon = "\U0001f4f7"
    description = "Turn clean images into casual phone, film, webcam or imperfect snapshot aesthetics."
    order = 40
    settings_schema = {}

    def key(self):
        return "amateur-photo"

    def __init__(self, hub):
        super().__init__(hub)
        # Folder choices persist across restarts (saved to settings.json on pick).
        self._session_input_folder = self.setting("input_folder", "")
        self._session_output_folder = self.setting("output_folder", "")

    def routes_get(self):
        return {
            "/amateur-photo": self._page,
            "/api/amateur-photo/state": self._api_state,
        }

    def routes_post(self):
        return {
            "/api/amateur-photo/session": self._api_session,
            "/api/amateur-photo/preview": self._api_preview,
            "/api/amateur-photo/save": self._api_save,
            "/api/amateur-photo/process": self._api_process,
        }

    def prefix_routes(self):
        return {
            "/amateur-photo/image/": self._serve_image,
        }

    def _input_folder(self):
        folder = self._session_input_folder.strip()
        return os.path.abspath(folder) if folder and os.path.isdir(folder) else ""

    def _output_folder(self, input_folder):
        folder = self._session_output_folder.strip()
        return Path(os.path.abspath(folder)) if folder else Path(input_folder) / "amateur-output"

    def _files(self):
        root = self._input_folder()
        if not root:
            return []
        try:
            return sorted([
                p.name for p in Path(root).iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
                and "_amateur" not in p.stem.lower()
            ], key=str.lower)
        except OSError:
            return []

    def _state(self):
        folder = self._input_folder()
        return {
            "ready": HAS_FILTERS,
            "error": FILTER_ERROR if not HAS_FILTERS else "",
            "input_folder": self._session_input_folder,
            "output_folder": self._session_output_folder,
            "default_output": str(Path(folder) / "amateur-output") if folder else "",
            "files": self._files(),
            "presets": PRESETS,
            "sections": PARAM_SECTIONS,
        }

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="amateur-photo", page_title="Amateur Photo",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    def _api_state(self, handler, qs):
        handler.respond_json(self._state())

    def _api_session(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        input_folder = str(data.get("input_folder", "")).strip()
        output_folder = str(data.get("output_folder", "")).strip()
        if not input_folder:
            handler.respond_json({"error": "Choose an input folder first"}, status=400)
            return
        if not os.path.isdir(input_folder):
            handler.respond_json({"error": "Input folder does not exist"}, status=404)
            return
        if output_folder and not os.path.isdir(output_folder):
            try:
                Path(output_folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                handler.respond_json({"error": f"Cannot create output folder: {exc}"}, status=400)
                return
        self._session_input_folder = input_folder
        self._session_output_folder = output_folder
        self.hub.settings.set_module_setting(self.key(), "input_folder", input_folder)
        self.hub.settings.set_module_setting(self.key(), "output_folder", output_folder)
        handler.respond_json(self._state())

    def _source_path(self, name):
        root = self._input_folder()
        if not root or not name:
            return None
        full = safe_resolve(root, name)
        if not full or not os.path.isfile(full) or Path(full).suffix.lower() not in SUPPORTED_EXTS:
            return None
        return full

    def _serve_image(self, handler, rel_path):
        full = self._source_path(rel_path)
        if not full:
            handler.respond_json({"error": "Image not found"}, status=404)
            return
        handler.serve_file(full)

    def _params(self, data):
        incoming = data.get("params") or {}
        preset_name = str(data.get("preset") or "Gentle Touch")
        base = dict(PRESETS.get(preset_name, PRESETS["Gentle Touch"])["params"])
        allowed = {
            "dr_factor", "hsv_hue_var", "hsv_sat_var", "hsv_val_var",
            "rgb_noise_std", "grain_amount", "grain_size", "beta", "alpha",
            "gamma", "warmth", "desat", "read_noise", "shot_noise",
            "sharp_radius", "sharp_amount", "use_mid_jpeg", "mid_jpeg_quality",
            "use_motion", "motion_len", "motion_angle", "ca_shift_px",
            "vignette_strength", "vignette_feather", "random_seed",
            "distortion_strength", "dust_amount",
        }
        for key in allowed:
            if key in incoming:
                base[key] = incoming[key]
        for key in ("grain_size", "sharp_radius", "motion_len", "mid_jpeg_quality"):
            try:
                base[key] = int(float(base.get(key, 0)))
            except (TypeError, ValueError):
                pass
        for key in ("use_mid_jpeg", "use_motion"):
            base[key] = bool(base.get(key, False))
        seed = base.get("random_seed", None)
        try:
            seed = int(seed)
            base["random_seed"] = seed if seed >= 0 else None
        except (TypeError, ValueError):
            base["random_seed"] = None
        return base

    def _read_image(self, full):
        if not HAS_FILTERS:
            raise RuntimeError("OpenCV / Pillow dependencies are not installed")
        raw = np.fromfile(full, dtype=np.uint8)
        img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not read image")
        return img

    def _process(self, full, params, max_preview=None):
        img = self._read_image(full)
        if max_preview and max(img.shape[:2]) > max_preview:
            scale = max_preview / max(img.shape[:2])
            img = cv2.resize(img, (max(1, int(img.shape[1] * scale)), max(1, int(img.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            params = dict(params)
            for key in ("grain_size", "motion_len", "sharp_radius"):
                params[key] = max(1 if key != "motion_len" else 0, int(round(float(params.get(key, 1)) * scale)))
            params["ca_shift_px"] = float(params.get("ca_shift_px", 0)) * scale
        return apply_amateur_filter(img, **params)

    def _binary_image(self, handler, image, ext=".jpg", quality=90):
        ext = ext.lower()
        if ext not in SUPPORTED_EXTS:
            ext = ".jpg"
        encode_ext = ".jpg" if ext == ".jpeg" else ext
        args = []
        if encode_ext == ".jpg":
            args = [int(cv2.IMWRITE_JPEG_QUALITY), int(max(1, min(100, quality)))]
        elif encode_ext == ".webp":
            args = [int(cv2.IMWRITE_WEBP_QUALITY), int(max(1, min(100, quality)))]
        ok, buf = cv2.imencode(encode_ext, image, args)
        if not ok:
            raise ValueError("Could not encode preview")
        payload = buf.tobytes()
        handler.send_response(200)
        handler.send_header("Content-Type", MIME_BY_EXT.get(ext, "image/jpeg"))
        handler.send_header("Content-Length", len(payload))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(payload)

    def _api_preview(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        full = self._source_path(str(data.get("file", "")))
        if not full:
            handler.respond_json({"error": "Image not found"}, status=404)
            return
        try:
            params = self._params(data)
            out = self._process(full, params, max_preview=1400)
            self._binary_image(handler, out, ".jpg", int(data.get("final_quality", 88) or 88))
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=500)

    def _save_one(self, name, data):
        full = self._source_path(name)
        if not full:
            raise FileNotFoundError(f"Image not found: {name}")
        input_folder = self._input_folder()
        out_dir = self._output_folder(input_folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        source_ext = Path(name).suffix.lower()
        fmt = str(data.get("format", "source")).upper()
        ext = source_ext if fmt == "SOURCE" else { "PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp" }.get(fmt, source_ext)
        out_path = next_output_path(out_dir, Path(name).stem, ext)
        params = self._params(data)
        out = self._process(full, params)
        quality = int(data.get("final_quality", 90) or 90)
        encode_ext = ".jpg" if ext in (".jpg", ".jpeg") else ext
        args = []
        if encode_ext == ".jpg":
            args = [int(cv2.IMWRITE_JPEG_QUALITY), int(max(1, min(100, quality)))]
        elif encode_ext == ".webp":
            args = [int(cv2.IMWRITE_WEBP_QUALITY), int(max(1, min(100, quality)))]
        ok, buf = cv2.imencode(encode_ext, out, args)
        if not ok:
            raise ValueError("Could not encode output image")
        buf.tofile(str(out_path))
        # Preserve original PNG metadata in the output
        try:
            from PIL import Image as _PILImage
            from PIL import PngImagePlugin
            with _PILImage.open(full) as src_img:
                if ext == ".png" and hasattr(src_img, "info"):
                    # Re-open the output and inject text chunks
                    with _PILImage.open(str(out_path)) as out_img:
                        pnginfo = PngImagePlugin.PngInfo()
                        for k, v in src_img.info.items():
                            if isinstance(v, str):
                                pnginfo.add_text(k, v)
                        pnginfo.add_text("CyberHub Process", f"Amateur Photo / {data.get('preset', 'custom')}")
                        out_img.save(str(out_path), pnginfo=pnginfo)
                elif ext in (".jpg", ".jpeg", ".webp") and "exif" in src_img.info:
                    with _PILImage.open(str(out_path)) as out_img:
                        out_img.save(str(out_path), exif=src_img.info["exif"])
        except Exception:
            pass  # metadata preservation is best-effort
        sidecar = out_path.with_name(out_path.stem + "_settings.json")
        sidecar.write_text(json.dumps({
            "source": name,
            "preset": data.get("preset", "Gentle Touch"),
            "params": params,
            "format": fmt,
            "final_quality": quality,
        }, indent=2), encoding="utf-8")
        return out_path.name

    def _api_save(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        try:
            output = self._save_one(str(data.get("file", "")), data)
            handler.respond_json({"ok": True, "output": output})
        except FileNotFoundError as exc:
            handler.respond_json({"error": str(exc)}, status=404)
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=500)

    def _api_process(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        files = self._files()
        if not files:
            handler.respond_json({"error": "No images loaded"}, status=400)
            return
        outputs, errors = [], []
        for name in files:
            try:
                outputs.append(self._save_one(name, data))
            except Exception as exc:
                errors.append({"file": name, "error": str(exc)})
        status = 200 if outputs else 500
        handler.respond_json({"ok": bool(outputs), "processed": len(outputs), "outputs": outputs, "errors": errors}, status=status)


PAGE_BODY = r"""
<style>
.ap-wrap{height:calc(100vh - 54px);display:grid;grid-template-columns:350px 1fr;background:var(--bg-darkest);color:var(--text)}
@media (max-width:820px){.ap-wrap{grid-template-columns:1fr;height:auto;min-height:calc(100vh - 54px)}.ap-side{border-right:none;border-bottom:1px solid var(--border)}}
.ap-side{overflow:auto;background:var(--bg-panel);border-right:1px solid var(--border);padding:16px}
.ap-main{min-width:0;display:flex;flex-direction:column;padding:14px;gap:12px}
.ap-title{font-size:20px;font-weight:750;margin-bottom:4px;color:var(--text-bright)} .ap-muted{font-size:12px;color:var(--text-dim)}
.ap-card{background:var(--bg-card);border:1px solid var(--border);border-radius:11px;padding:12px;margin-top:12px}
.ap-label{display:block;color:var(--text-dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin:0 0 6px}
.ap-row{display:flex;gap:7px;align-items:center;margin-top:7px} .ap-row input[type=text],.ap-row select,.ap-select{height:32px;background:var(--bg-input);border:1px solid var(--border-light);border-radius:7px;color:var(--text);padding:0 9px;min-width:0;flex:1}
.ap-btn{height:32px;background:var(--bg-card);border:1px solid var(--border-light);border-radius:7px;color:var(--text);padding:0 12px;cursor:pointer;font-weight:400} .ap-btn:hover{background:var(--bg-hover)} .ap-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:400} .ap-btn.green{background:var(--green);border-color:var(--green);color:#fff;font-weight:400} .ap-btn:disabled{opacity:.42;cursor:not-allowed}
.ap-section{margin-top:12px;border-top:1px solid var(--border);padding-top:10px} .ap-section h3{font-size:11px;color:var(--accent);letter-spacing:.08em;text-transform:uppercase;margin:0 0 8px}
.ap-control{margin:8px 0} .ap-control-head{display:flex;justify-content:space-between;color:var(--text);font-size:12px;margin-bottom:3px} .ap-control output{font-family:Consolas,monospace;color:var(--accent)}
.ap-control input[type=range]{width:100%;accent-color:var(--accent)}
.ap-toggle{display:flex;gap:10px;font-size:12px;color:var(--text);margin:8px 0} .ap-toggle label{display:flex;align-items:center;gap:5px}
.ap-toolbar{min-height:45px;background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;display:flex;align-items:center;padding:7px 10px;gap:8px}
.ap-grow{flex:1} .ap-status{font-size:12px;color:var(--text-dim)}
.ap-stage{position:relative;flex:1;min-height:300px;background:var(--bg-dark);border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:crosshair}
.ap-stage.zoomed{cursor:grab}.ap-stage.zoomed.panning{cursor:grabbing}
.ap-stage img{max-width:100%;max-height:100%;object-fit:contain;user-select:none;pointer-events:none;transform-origin:center center}
.ap-after{position:absolute;inset:0;display:flex;align-items:center;justify-content:center} .ap-before{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;clip-path:inset(0 50% 0 0)} .ap-divider{position:absolute;top:0;bottom:0;left:50%;width:2px;background:#fff;opacity:.8;pointer-events:none}
.ap-empty{color:var(--text-dim);text-align:center} .ap-compare{width:220px;accent-color:var(--accent)} .ap-zoom-status{min-width:42px;text-align:center;font-family:Consolas,monospace}
.ap-compare-overlay{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);display:flex;align-items:center;gap:10px;background:rgba(15,23,42,.78);border:1px solid var(--border-light);border-radius:999px;padding:7px 12px;color:var(--text);font-size:12px;backdrop-filter:blur(8px);z-index:5}
.theme-light .ap-compare-overlay{background:rgba(255,255,255,.84)}
.ap-footer{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--text-dim)}
.ap-toast{position:fixed;bottom:20px;right:22px;background:var(--bg-card);border:1px solid var(--border-light);padding:10px 14px;border-radius:9px;z-index:90;display:none} .ap-toast.err{border-color:var(--red);color:var(--red)}
.ap-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center} .ap-modal.open{display:flex} .ap-dialog{background:var(--bg-panel);border:1px solid var(--border-light);border-radius:12px;width:min(650px,92vw);height:min(520px,78vh);display:flex;flex-direction:column;padding:14px}
.ap-crumb{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:var(--accent);padding:8px 0;cursor:pointer} .ap-browse{flex:1;border:1px solid var(--border);border-radius:8px;overflow:auto;padding:7px} .ap-entry{padding:7px 9px;border-radius:6px;cursor:pointer;font-size:13px} .ap-entry:hover{background:var(--bg-hover)}
.ap-file-list{width:180px;height:32px;background:var(--bg-input);color:var(--text);border:1px solid var(--border-light);border-radius:7px}
</style>
<div class="ap-wrap">
 <aside class="ap-side">
  <div class="ap-title">Amateur Photo</div><div class="ap-muted">Imperfect camera effects for more casual-looking images.</div>
  <div class="ap-card">
   <span class="ap-label">Source images</span>
   <div class="ap-row"><input id="apInput" type="text" placeholder="Input folder"><button class="ap-btn" id="apBrowseIn">Browse</button></div>
   <div class="ap-row"><input id="apOutput" type="text" placeholder="Output defaults to input/amateur-output"><button class="ap-btn" id="apBrowseOut">Browse</button></div>
   <div class="ap-row"><button class="ap-btn primary" id="apLoad">Load folder</button></div>
  </div>
  <div class="ap-card">
   <span class="ap-label">Preset</span>
   <select id="apPreset" class="ap-select"></select>
   <div id="apPresetDesc" class="ap-muted" style="margin-top:8px"></div>
   <div class="ap-row"><button class="ap-btn" id="apExportPreset">Export</button><button class="ap-btn" id="apImportPreset">Import</button><input id="apPresetFile" type="file" accept=".json" hidden></div>
  </div>
  <div class="ap-card">
    <div class="ap-toggle"><label><input type="checkbox" id="apMidJpeg"> Mid JPEG</label><label><input type="checkbox" id="apMotion"> Motion Blur</label></div>
    <div id="apControls"></div>
  </div>
 </aside>
 <main class="ap-main">
  <div class="ap-toolbar">
   <button class="ap-btn" id="apPrev">◀ Prev</button><button class="ap-btn" id="apNext">Next ▶</button>
   <select class="ap-file-list" id="apFiles"></select>
   <span class="ap-status" id="apCounter"></span>
   <div class="ap-grow"></div>
   <button class="ap-btn" id="apZoomOut" title="Zoom out">−</button>
   <span class="ap-status ap-zoom-status" id="apZoomLabel">Fit</span>
   <button class="ap-btn" id="apZoomIn" title="Zoom in">+</button>
   <button class="ap-btn" id="apZoomFit" title="Fit to view">Fit</button>
   <button class="ap-btn" id="apZoom100" title="View at 100%">100%</button>
   <select class="ap-select" id="apFormat" style="flex:0 0 105px"><option value="source">Source</option><option value="PNG">PNG</option><option value="JPEG">JPEG</option><option value="WEBP">WEBP</option></select>
   <button class="ap-btn primary" id="apSave">Save output</button>
   <button class="ap-btn green" id="apBatch">Process all</button>
  </div>
  <div class="ap-stage" id="apStage">
   <div class="ap-empty" id="apEmpty">Choose an input folder to start</div>
   <div class="ap-after" id="apAfterWrap" style="display:none"><img id="apAfter"></div>
   <div class="ap-before" id="apBeforeWrap" style="display:none"><img id="apBefore"></div>
   <div class="ap-divider" id="apDivider" style="display:none"></div>
   <label class="ap-compare-overlay" id="apCompareOverlay" style="display:none">Compare <input class="ap-compare" id="apSplit" type="range" min="0" max="100" value="50"></label>
  </div>
  <div class="ap-footer"><span id="apStatus">Ready</span><span></span></div>
 </main>
</div>
<div class="ap-toast" id="apToast"></div>
<div class="ap-modal" id="apBrowseModal"><div class="ap-dialog">
 <div class="ap-row"><strong id="apBrowseTitle">Select folder</strong><div class="ap-grow"></div><button class="ap-btn" id="apBrowseClose">Close</button></div>
 <div class="ap-crumb" id="apCrumb"></div>
 <div class="ap-browse" id="apBrowseBody"></div>
 <div class="ap-row"><span class="ap-status" id="apBrowsePath"></span><div class="ap-grow"></div><button class="ap-btn primary" id="apChooseFolder">Select folder</button></div>
</div></div>
<script>
(function(){
var PRESETS={"Gentle Touch":{"description":"Barely noticeable — just enough to feel real","category":"Subtle","params":{"dr_factor":0.99,"hsv_hue_var":0.8,"hsv_sat_var":2.0,"hsv_val_var":3.0,"rgb_noise_std":0.003,"grain_amount":0.06,"grain_size":2,"alpha":1.01,"beta":2,"gamma":1.01,"warmth":0.01,"desat":0.95,"read_noise":0.002,"shot_noise":0.01,"sharp_radius":3,"sharp_amount":0.15,"use_mid_jpeg":true,"mid_jpeg_quality":72,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":0.3,"vignette_strength":0.1,"vignette_feather":0.65,"distortion_strength":-0.02,"dust_amount":0.03}},"Phone Snap":{"description":"Quick phone shot — slightly noisy, oversaturated","category":"Subtle","params":{"dr_factor":0.97,"hsv_hue_var":1.5,"hsv_sat_var":4.0,"hsv_val_var":5.0,"rgb_noise_std":0.005,"grain_amount":0.08,"grain_size":1,"alpha":1.05,"beta":5,"gamma":0.97,"warmth":0.015,"desat":0.93,"read_noise":0.004,"shot_noise":0.025,"sharp_radius":2,"sharp_amount":0.3,"use_mid_jpeg":true,"mid_jpeg_quality":60,"use_motion":false,"motion_len":2,"motion_angle":0,"ca_shift_px":0.5,"vignette_strength":0.12,"vignette_feather":0.6,"distortion_strength":-0.04,"dust_amount":0.0}},"Disposable Camera":{"description":"Party-vibe disposable — flash-washed, warm, grainy","category":"Analog","params":{"dr_factor":0.9,"hsv_hue_var":3.5,"hsv_sat_var":9.0,"hsv_val_var":10.0,"rgb_noise_std":0.012,"grain_amount":0.18,"grain_size":3,"alpha":0.96,"beta":8,"gamma":1.12,"warmth":0.055,"desat":0.82,"read_noise":0.012,"shot_noise":0.055,"sharp_radius":5,"sharp_amount":0.12,"use_mid_jpeg":true,"mid_jpeg_quality":42,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":1.8,"vignette_strength":0.38,"vignette_feather":0.42,"distortion_strength":-0.18,"dust_amount":0.28}},"Faded Film":{"description":"Old film stock — lifted blacks, muted tones","category":"Analog","params":{"dr_factor":0.85,"hsv_hue_var":2.5,"hsv_sat_var":6.0,"hsv_val_var":7.0,"rgb_noise_std":0.008,"grain_amount":0.14,"grain_size":3,"alpha":0.94,"beta":12,"gamma":1.08,"warmth":0.035,"desat":0.78,"read_noise":0.008,"shot_noise":0.04,"sharp_radius":3,"sharp_amount":0.1,"use_mid_jpeg":true,"mid_jpeg_quality":55,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":1.2,"vignette_strength":0.3,"vignette_feather":0.5,"distortion_strength":-0.08,"dust_amount":0.2}},"Hard JPEG":{"description":"Compression hell — heavy blocking, color banding","category":"Digital","params":{"dr_factor":1.0,"hsv_hue_var":0.0,"hsv_sat_var":0.0,"hsv_val_var":0.0,"rgb_noise_std":0.0,"grain_amount":0.04,"grain_size":1,"alpha":1.0,"beta":0,"gamma":1.0,"warmth":0.0,"desat":0.95,"read_noise":0.0,"shot_noise":0.0,"sharp_radius":1,"sharp_amount":0.1,"use_mid_jpeg":true,"mid_jpeg_quality":32,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":0,"vignette_strength":0.08,"vignette_feather":0.7,"distortion_strength":0.0,"dust_amount":0.0}},"Early Webcam":{"description":"2005 webcam — low-res feel, noisy, washed out","category":"Digital","params":{"dr_factor":0.88,"hsv_hue_var":3.0,"hsv_sat_var":8.0,"hsv_val_var":10.0,"rgb_noise_std":0.015,"grain_amount":0.1,"grain_size":1,"alpha":1.1,"beta":10,"gamma":0.92,"warmth":0.01,"desat":0.88,"read_noise":0.01,"shot_noise":0.06,"sharp_radius":1,"sharp_amount":0.4,"use_mid_jpeg":true,"mid_jpeg_quality":38,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":0.5,"vignette_strength":0.15,"vignette_feather":0.55,"distortion_strength":-0.05,"dust_amount":0.0}},"Shaky Hands":{"description":"Slight hand tremor — subtle motion blur","category":"Motion","params":{"dr_factor":0.98,"hsv_hue_var":1.5,"hsv_sat_var":4.0,"hsv_val_var":5.0,"rgb_noise_std":0.005,"grain_amount":0.1,"grain_size":2,"alpha":1.02,"beta":3,"gamma":1.03,"warmth":0.02,"desat":0.91,"read_noise":0.004,"shot_noise":0.02,"sharp_radius":3,"sharp_amount":0.18,"use_mid_jpeg":true,"mid_jpeg_quality":55,"use_motion":true,"motion_len":5,"motion_angle":4,"ca_shift_px":0.8,"vignette_strength":0.18,"vignette_feather":0.55,"distortion_strength":-0.06,"dust_amount":0.08}},"Motion Accident":{"description":"Caught off-guard — heavy blur, messy exposure","category":"Motion","params":{"dr_factor":0.95,"hsv_hue_var":2.5,"hsv_sat_var":6.0,"hsv_val_var":7.0,"rgb_noise_std":0.008,"grain_amount":0.14,"grain_size":2,"alpha":1.04,"beta":5,"gamma":1.06,"warmth":0.025,"desat":0.88,"read_noise":0.006,"shot_noise":0.03,"sharp_radius":3,"sharp_amount":0.16,"use_mid_jpeg":true,"mid_jpeg_quality":46,"use_motion":true,"motion_len":10,"motion_angle":8,"ca_shift_px":1.5,"vignette_strength":0.3,"vignette_feather":0.48,"distortion_strength":-0.1,"dust_amount":0.15}},"Night Out":{"description":"Dark venue — high ISO noise, warm flash spill","category":"Mood","params":{"dr_factor":0.82,"hsv_hue_var":4.0,"hsv_sat_var":10.0,"hsv_val_var":12.0,"rgb_noise_std":0.018,"grain_amount":0.2,"grain_size":2,"alpha":0.92,"beta":-5,"gamma":1.15,"warmth":0.06,"desat":0.8,"read_noise":0.015,"shot_noise":0.07,"sharp_radius":3,"sharp_amount":0.1,"use_mid_jpeg":true,"mid_jpeg_quality":45,"use_motion":true,"motion_len":4,"motion_angle":-3,"ca_shift_px":2.0,"vignette_strength":0.45,"vignette_feather":0.4,"distortion_strength":-0.12,"dust_amount":0.1}},"Nostalgic Summer":{"description":"Warm golden haze — sun-bleached and dreamy","category":"Mood","params":{"dr_factor":0.88,"hsv_hue_var":2.0,"hsv_sat_var":5.0,"hsv_val_var":6.0,"rgb_noise_std":0.006,"grain_amount":0.12,"grain_size":3,"alpha":1.0,"beta":10,"gamma":1.1,"warmth":0.07,"desat":0.83,"read_noise":0.005,"shot_noise":0.025,"sharp_radius":3,"sharp_amount":0.08,"use_mid_jpeg":true,"mid_jpeg_quality":55,"use_motion":false,"motion_len":0,"motion_angle":0,"ca_shift_px":1.0,"vignette_strength":0.25,"vignette_feather":0.55,"distortion_strength":-0.1,"dust_amount":0.22}}};
var SECTIONS=[["Lens",[["Distortion","distortion_strength",-0.5,0.5,0.01,"Barrel/pincushion distortion"],["Chromatic Aberr","ca_shift_px",0,4,0.5,"Radial color fringing"],["Vignette","vignette_strength",0.0,0.6,0.01,"Edge darkening intensity"],["Vig. Falloff","vignette_feather",0.2,0.9,0.01,"Vignette gradient softness"]]],["Sensor Noise",[["Film Grain","grain_amount",0.0,0.5,0.01,"Grain overlay intensity"],["Grain Size","grain_size",1,6,1,"Grain particle scale"],["Read Noise","read_noise",0.0,0.02,0.0005,"Sensor read noise floor"],["Shot Noise","shot_noise",0.0,0.08,0.001,"Photon shot noise"],["RGB Noise","rgb_noise_std",0.0,0.03,0.001,"Random color channel noise"],["Dust & Specs","dust_amount",0.0,0.8,0.01,"Lens dust particles"]]],["Tone & Exposure",[["Dynamic Range","dr_factor",0.5,1.5,0.01,"Highlight/shadow compression"],["Contrast","alpha",0.85,1.25,0.01,"Overall contrast"],["Brightness","beta",-20,20,1,"Exposure offset"],["Gamma","gamma",0.8,1.4,0.01,"Tone curve gamma"]]],["Color",[["Warmth","warmth",0.0,0.08,0.001,"Warm color shift"],["Desaturation","desat",0.75,1.0,0.01,"Color saturation"],["Hue Jitter","hsv_hue_var",0.0,8.0,0.1,"Random hue variation"],["Sat. Jitter","hsv_sat_var",0.0,20.0,0.5,"Random saturation variation"],["Val. Jitter","hsv_val_var",0.0,20.0,0.5,"Random value variation"]]],["Sharpening",[["Sharp Radius","sharp_radius",1,9,1,"Unsharp mask radius"],["Sharp Amount","sharp_amount",0.0,0.6,0.01,"Unsharp mask intensity"]]],["Compression",[["JPEG Quality","mid_jpeg_quality",30,90,1,"Mid-pipeline JPEG quality"]]],["Motion Blur",[["Blur Length","motion_len",0,15,1,"Motion blur kernel size"],["Blur Angle","motion_angle",-15,15,1,"Motion blur direction"]]],["Output",[["Final JPEG Q","final_jpeg_quality",30,100,1,"Final output JPEG quality"],["Random Seed","random_seed",-1,9999,1,"Fixed seed (-1 = random)"]]]];
var state={files:[],index:-1,preset:'Gentle Touch',params:{},previewTimer:null,browseTarget:null,browseCurrent:'',zoom:1,panX:0,panY:0};
var $=function(id){return document.getElementById(id);};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function toast(msg,err){var t=$('apToast');t.textContent=msg;t.className='ap-toast'+(err?' err':'');t.style.display='block';clearTimeout(t._x);t._x=setTimeout(function(){t.style.display='none';},3200);}
function apiError(r){return r.json().catch(function(){return {error:'HTTP '+r.status};}).then(function(d){throw new Error(d.error||('HTTP '+r.status));});}
function formatValue(v,step){return Number(step)<1?Number(v).toFixed(String(step).split('.')[1].length):String(Math.round(Number(v)));}
function applyPreset(name){state.preset=name;state.params=Object.assign({},PRESETS[name].params); $('apPresetDesc').textContent=PRESETS[name].category+' · '+PRESETS[name].description; $('apMidJpeg').checked=!!state.params.use_mid_jpeg;$('apMotion').checked=!!state.params.use_motion;renderControls();queuePreview();}
function renderControls(){var html='';SECTIONS.forEach(function(sec){html+='<div class="ap-section"><h3>'+esc(sec[0])+'</h3>';sec[1].forEach(function(c){var value=state.params[c[1]];if(c[1]==='final_jpeg_quality') value=state.params.final_jpeg_quality||90;if(c[1]==='random_seed' && (value===null||value===undefined)) value=-1;html+='<div class="ap-control"><div class="ap-control-head"><span title="'+esc(c[5])+'">'+esc(c[0])+'</span><output id="out_'+c[1]+'">'+formatValue(value,c[4])+'</output></div><input type="range" data-param="'+c[1]+'" min="'+c[2]+'" max="'+c[3]+'" step="'+c[4]+'" value="'+value+'"></div>';});html+='</div>';});$('apControls').innerHTML=html;Array.prototype.forEach.call($('apControls').querySelectorAll('input[data-param]'),function(el){el.oninput=function(){var k=this.getAttribute('data-param'),v=Number(this.value);state.params[k]=v;$('out_'+k).textContent=formatValue(v,this.step);queuePreview();};});}
function payload(){return {file:current(),preset:state.preset,params:state.params,final_quality:state.params.final_jpeg_quality||90,format:$('apFormat').value||'source'};}
function current(){return state.files[state.index]||'';}
function queuePreview(){if(!current())return;clearTimeout(state.previewTimer);state.previewTimer=setTimeout(renderPreview,130);}
function renderPreview(){if(!current())return;var before='/amateur-photo/image/'+encodeURIComponent(current());$('apBefore').src=before;fetch('/api/amateur-photo/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}).then(function(r){return r.ok?r.blob():apiError(r);}).then(function(blob){var url=URL.createObjectURL(blob);var img=$('apAfter');img.onload=function(){if(img._old)URL.revokeObjectURL(img._old);img._old=url;};img.src=url;$('apEmpty').style.display='none';$('apAfterWrap').style.display='flex';$('apBeforeWrap').style.display='flex';$('apDivider').style.display='block';$('apCompareOverlay').style.display='flex';$('apStatus').textContent=current()+' · '+PRESETS[state.preset].description;}).catch(function(e){toast(e.message,true);});}
function setFileIndex(i){if(!state.files.length)return;state.index=Math.max(0,Math.min(i,state.files.length-1));$('apFiles').selectedIndex=state.index;$('apCounter').textContent=(state.index+1)+' / '+state.files.length;setZoom(1,0,0);renderPreview();}
function applyState(s){state.files=s.files||[];$('apInput').value=s.input_folder||$('apInput').value;$('apOutput').value=s.output_folder||'';$('apFiles').innerHTML=state.files.map(function(f){return '<option>'+esc(f)+'</option>';}).join('');if(state.files.length) setFileIndex(0);else {$('apCounter').textContent='';$('apEmpty').style.display='block';$('apAfterWrap').style.display='none';$('apBeforeWrap').style.display='none';$('apDivider').style.display='none';$('apCompareOverlay').style.display='none';}}
function loadFolders(){fetch('/api/amateur-photo/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_folder:$('apInput').value.trim(),output_folder:$('apOutput').value.trim()})}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(s){applyState(s);toast((s.files||[]).length+' images loaded');}).catch(function(e){toast(e.message,true);});}
function saveOne(){if(!current())return;fetch('/api/amateur-photo/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){toast('Saved: '+d.output);}).catch(function(e){toast(e.message,true);});}
function processAll(){if(!state.files.length)return;if(!confirm('Process all '+state.files.length+' images with the current settings?'))return;var b=$('apBatch');b.disabled=true;b.textContent='Processing…';fetch('/api/amateur-photo/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){toast('Processed '+d.processed+' images'+(d.errors.length?' · '+d.errors.length+' errors':''),!!d.errors.length);}).catch(function(e){toast(e.message,true);}).finally(function(){b.disabled=false;b.textContent='Process all';});}
function updateSplit(){var p=Number($('apSplit').value);$('apBeforeWrap').style.clipPath='inset(0 '+(100-p)+'% 0 0)';$('apDivider').style.left=p+'%';}
function clampPan(){if(state.zoom<=1){state.panX=0;state.panY=0;return;}var r=$('apStage').getBoundingClientRect();var limX=r.width*(state.zoom-1)/2,limY=r.height*(state.zoom-1)/2;state.panX=Math.max(-limX,Math.min(limX,state.panX));state.panY=Math.max(-limY,Math.min(limY,state.panY));}
function applyZoom(){clampPan();var tf='translate('+state.panX+'px,'+state.panY+'px) scale('+state.zoom+')';$('apBefore').style.transform=tf;$('apAfter').style.transform=tf;$('apZoomLabel').textContent=state.zoom===1?'Fit':Math.round(state.zoom*100)+'%';$('apStage').classList.toggle('zoomed',state.zoom>1);}
function setZoom(z,x,y){state.zoom=Math.max(1,Math.min(8,z));if(x!==undefined)state.panX=x;if(y!==undefined)state.panY=y;if(state.zoom===1){state.panX=0;state.panY=0;}applyZoom();}
function zoomBy(mult){setZoom(state.zoom*mult);}
function zoomActual(){var img=$('apAfter');var ratio=1;if(img&&img.clientWidth&&img.naturalWidth)ratio=img.naturalWidth/img.clientWidth;setZoom(Math.max(1,ratio),0,0);}
function browseOpen(target){state.browseTarget=target;$('apBrowseTitle').textContent=target==='apInput'?'Select input folder':'Select output folder';$('apBrowseModal').classList.add('open');browseLoad($(target).value.trim());}
function browseLoad(path){fetch('/api/browse?path='+encodeURIComponent(path||'')).then(function(r){return r.ok?r.json():apiError(r);}).then(function(d){state.browseCurrent=d.path||'';$('apBrowsePath').textContent=d.display||state.browseCurrent||'Drives';var crumbs=['<span data-path="">💻</span>'];(d.crumbs||[]).forEach(function(c){crumbs.push('<span>›</span><span data-path="'+esc(c.path)+'">'+esc(c.label)+'</span>');});$('apCrumb').innerHTML=crumbs.join('');var html='';if(d.parent!==null&&d.parent!==undefined)html+='<div class="ap-entry" data-path="'+esc(d.parent)+'">↩ ..</div>';(d.dirs||[]).forEach(function(dir){html+='<div class="ap-entry" data-path="'+esc(dir.path)+'">📁 '+esc(dir.name)+'</div>';});$('apBrowseBody').innerHTML=html||'<div class="ap-entry">No subfolders</div>';}).catch(function(e){toast(e.message,true);});}
$('apPreset').innerHTML=Object.keys(PRESETS).map(function(k){return '<option>'+esc(k)+'</option>';}).join('');$('apPreset').onchange=function(){applyPreset(this.value);};
$('apMidJpeg').onchange=function(){state.params.use_mid_jpeg=this.checked;queuePreview();};$('apMotion').onchange=function(){state.params.use_motion=this.checked;queuePreview();};
$('apLoad').onclick=loadFolders;$('apPrev').onclick=function(){setFileIndex(state.index-1);};$('apNext').onclick=function(){setFileIndex(state.index+1);};$('apFiles').onchange=function(){setFileIndex(this.selectedIndex);};$('apSave').onclick=saveOne;$('apBatch').onclick=processAll;$('apSplit').oninput=updateSplit;$('apZoomOut').onclick=function(){zoomBy(1/1.25);};$('apZoomIn').onclick=function(){zoomBy(1.25);};$('apZoomFit').onclick=function(){setZoom(1,0,0);};$('apZoom100').onclick=zoomActual;
var splitDrag=false,panDrag=false,panStart=null;function stageSplit(e){var r=$('apStage').getBoundingClientRect(),v=Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100));$('apSplit').value=v;updateSplit();}
$('apStage').addEventListener('wheel',function(e){if(!current())return;e.preventDefault();zoomBy(e.deltaY<0?1.12:1/1.12);},{passive:false});
$('apStage').addEventListener('mousedown',function(e){if(!current())return;if(state.zoom>1&&!e.shiftKey){panDrag=true;panStart={x:e.clientX,y:e.clientY,px:state.panX,py:state.panY};$('apStage').classList.add('panning');}else{splitDrag=true;stageSplit(e);}});
document.addEventListener('mousemove',function(e){if(splitDrag)stageSplit(e);if(panDrag&&panStart){state.panX=panStart.px+(e.clientX-panStart.x);state.panY=panStart.py+(e.clientY-panStart.y);applyZoom();}});
document.addEventListener('mouseup',function(){splitDrag=false;panDrag=false;panStart=null;$('apStage').classList.remove('panning');});
$('apBrowseIn').onclick=function(){browseOpen('apInput');};$('apBrowseOut').onclick=function(){browseOpen('apOutput');};$('apBrowseClose').onclick=function(){$('apBrowseModal').classList.remove('open');};$('apChooseFolder').onclick=function(){if(state.browseCurrent)$(state.browseTarget).value=state.browseCurrent;$('apBrowseModal').classList.remove('open');};$('apBrowseModal').onclick=function(e){if(e.target===this) this.classList.remove('open');var x=e.target.closest('[data-path]');if(x)browseLoad(x.getAttribute('data-path'));};
$('apExportPreset').onclick=function(){var blob=new Blob([JSON.stringify({preset:state.preset,params:state.params},null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='amateur-photo-preset.json';a.click();setTimeout(function(){URL.revokeObjectURL(a.href);},200);};
$('apImportPreset').onclick=function(){$('apPresetFile').click();};$('apPresetFile').onchange=function(){var f=this.files[0];if(!f)return;f.text().then(function(txt){var d=JSON.parse(txt);if(d.preset&&PRESETS[d.preset]){state.preset=d.preset;$('apPreset').value=d.preset;}state.params=Object.assign({},PRESETS[state.preset].params,d.params||{});$('apMidJpeg').checked=!!state.params.use_mid_jpeg;$('apMotion').checked=!!state.params.use_motion;renderControls();queuePreview();toast('Preset imported');}).catch(function(e){toast('Invalid preset file',true);});this.value='';};
document.addEventListener('keydown',function(e){if(e.target && /input|select|textarea/i.test(e.target.tagName))return;if(e.key==='a'||e.key==='A')setFileIndex(state.index-1);if(e.key==='f'||e.key==='F')setFileIndex(state.index+1);if(e.key==='s'||e.key==='S')saveOne();});
applyPreset('Gentle Touch');updateSplit();applyZoom();fetch('/api/amateur-photo/state').then(function(r){return r.json();}).then(function(s){if(!s.ready)toast('OpenCV is unavailable: '+s.error,true);applyState(s);});
})();
</script>
"""
