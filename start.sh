#!/usr/bin/env bash
# CyberHub — Start script for macOS / Linux
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer CyberHub's auto-created .venv, but also accept a user-created venv/
# so manual Linux installs can still run through this launcher later.
VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ] && [ -x "venv/bin/python" ]; then
    VENV_DIR="venv"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    # Find a supported Python 3. Prefer versions with reliable wheels for
    # Pillow/numpy/OpenCV.
    PYTHON=""
    FOUND_PYTHONS=""
    for cmd in "${CYBERHUB_PYTHON:-}" python3.12 python3.11 python3.10 python3.13 python3 python; do
        [ -n "$cmd" ] || continue
        if command -v "$cmd" &>/dev/null; then
            version=$("$cmd" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>/dev/null || true)
            [ -n "$version" ] && FOUND_PYTHONS="${FOUND_PYTHONS}${cmd} (${version}) "
            ok=$("$cmd" -c "import sys; print('1' if sys.version_info >= (3,10) and sys.version_info < (3,14) else '0')" 2>/dev/null || echo "0")
            if [ "$ok" = "1" ]; then
                PYTHON="$cmd"
                break
            fi
        fi
    done

    if [ -z "$PYTHON" ]; then
        echo "[ERROR] Supported Python not found."
        echo "[ERROR] CyberHub currently supports Python 3.10, 3.11, 3.12 or 3.13."
        if [ -n "$FOUND_PYTHONS" ]; then
            echo "[INFO] Python commands found: $FOUND_PYTHONS"
        else
            echo "[INFO] No python3/python command was found in PATH."
        fi
        echo "[INFO] Install a supported Python, or run with:"
        echo "       CYBERHUB_PYTHON=/path/to/python3.12 ./start.sh"
        exit 1
    fi

    echo "[SETUP] Creating virtual environment..."
    if ! "$PYTHON" -m venv "$VENV_DIR"; then
        echo
        echo "[ERROR] Could not create the Python virtual environment."
        echo "[ERROR] On Debian/Ubuntu this usually means the venv package is missing."
        echo "[INFO] Try one of these, then run ./start.sh again:"
        echo "       sudo apt install python3-venv"
        echo "       sudo apt install python3.12-venv"
        exit 1
    fi
fi

# Keep base dependencies and the requirements of installed modules up to date.
echo "[SETUP] Checking Python packages..."
deps_failed=0
if ! "$VENV_DIR/bin/python" -m pip install --upgrade pip -q; then
    deps_failed=1
elif ! "$VENV_DIR/bin/python" -m pip install -q -r requirements.txt; then
    deps_failed=1
fi
if [ "$deps_failed" = "0" ]; then
    for module_requirements in modules/*/requirements.txt; do
        [ -f "$module_requirements" ] || continue
        if ! "$VENV_DIR/bin/python" -m pip install -q -r "$module_requirements"; then
            deps_failed=1
            break
        fi
    done
fi
if [ -d "modules/upscaler" ] || [ -d "modules/gallery_auto_tagger" ]; then
    if [ "$deps_failed" = "0" ] && ! "$VENV_DIR/bin/python" -c "import onnxruntime" >/dev/null 2>&1; then
        echo "[SETUP] Installing ONNX Runtime CPU fallback..."
        if ! "$VENV_DIR/bin/python" -m pip install -q "onnxruntime>=1.17"; then
            deps_failed=1
        fi
    fi
fi
if [ "$deps_failed" = "0" ] && ! "$VENV_DIR/bin/python" -c "import PIL, requests, send2trash" >/dev/null 2>&1; then
    deps_failed=1
fi
if [ "$deps_failed" = "1" ]; then
    echo
    echo "[ERROR] Python packages are missing or failed to install."
    echo "[ERROR] This usually happens when the .venv was created with an unsupported Python version."
    echo "[ERROR] Recommended fix:"
    echo "        1. Install Python 3.12 or 3.11"
    echo "        2. Delete the $VENV_DIR folder inside this hub folder"
    echo "        3. Run ./start.sh again"
    echo
    "$VENV_DIR/bin/python" --version
    exit 1
fi

# Download local fonts + ONNX runtime on first run (idempotent, skips existing)
if [ ! -f "resources/fonts/inter-400.woff2" ]; then
    echo "[SETUP] Downloading fonts and assets (first run only)..."
    "$VENV_DIR/bin/python" resources/fonts/download_fonts.py || \
        echo "[WARN] Font download failed — the hub will fall back to system fonts."
fi

# Run
exec "$VENV_DIR/bin/python" hub.py "$@"
