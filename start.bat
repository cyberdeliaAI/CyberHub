@echo off
:: CyberHub — Start script for Windows
cd /d "%~dp0"

:: Find a supported Python 3 only when the venv is missing. Prefer versions
:: with reliable wheels for Pillow/numpy/OpenCV on Windows.
set PYTHON=
if exist ".venv\Scripts\python.exe" goto venv_ready

for %%V in (3.12 3.11 3.10 3.13) do (
    if not defined PYTHON (
        py -%%V -c "import sys" >nul 2>&1 && set "PYTHON=py -%%V"
    )
)
if not defined PYTHON (
    where python >nul 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) and sys.version_info < (3,14) else 1)" >nul 2>&1 && set "PYTHON=python"
)
if not defined PYTHON (
    where py >nul 2>&1 && py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) and sys.version_info < (3,14) else 1)" >nul 2>&1 && set "PYTHON=py -3"
)
if not defined PYTHON (
    echo [ERROR] Supported Python not found. Install Python 3.10, 3.11, 3.12 or 3.13 from https://www.python.org
    pause
    exit /b 1
)

echo [SETUP] Creating virtual environment...
%PYTHON% -m venv .venv

:venv_ready

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) and sys.version_info < (3,14) else 1)" >nul 2>&1
if errorlevel 1 goto bad_venv

echo [SETUP] Using Python:
".venv\Scripts\python.exe" -c "import sys; print('        ' + sys.executable); print('        ' + sys.version.split()[0])"

:: Always ensure dependencies are up to date. Do not hide failures: if Pillow,
:: numpy or OpenCV fail to install, the hub should not continue with a broken
:: image stack.
echo [SETUP] Checking Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
if errorlevel 1 goto deps_failed
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto deps_failed
:: Keep an existing CUDA/DirectML/CoreML runtime. Install CPU only when no ONNX
:: Runtime variant is importable yet.
".venv\Scripts\python.exe" -c "import onnxruntime" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing ONNX Runtime CPU fallback...
    ".venv\Scripts\python.exe" -m pip install -q "onnxruntime>=1.17"
    if errorlevel 1 goto deps_failed
)
".venv\Scripts\python.exe" -c "import PIL, requests, send2trash" >nul 2>&1
if errorlevel 1 goto deps_failed
goto deps_ok

:bad_venv
echo.
echo [ERROR] Existing .venv uses an unsupported Python version.
echo [ERROR] CyberHub supports Python 3.10, 3.11, 3.12 or 3.13.
echo [ERROR] Recommended fix:
echo         1. Install Python 3.12 or 3.11 from https://www.python.org
echo         2. Delete the .venv folder inside this hub folder
echo         3. Run start.bat again
echo.
".venv\Scripts\python.exe" --version
pause
exit /b 1

:deps_failed
echo.
echo [ERROR] Python packages are missing or failed to install.
echo [ERROR] This usually happens when the .venv was created with an unsupported Python version.
echo [ERROR] Recommended fix:
echo         1. Install Python 3.12 or 3.11 from https://www.python.org
echo         2. Delete the .venv folder inside this hub folder
echo         3. Run start.bat again
echo.
".venv\Scripts\python.exe" --version
pause
exit /b 1

:deps_ok

:: cuDNN 9 is split into multiple DLLs which load each other at runtime. Keep
:: NVIDIA pip package bin folders on PATH so those sibling DLLs remain visible.
for /d %%D in (".venv\Lib\site-packages\nvidia\*\bin") do set "PATH=%%~fD;%PATH%"

:: Download local fonts + ONNX runtime on first run (idempotent, skips existing)
if not exist "resources\fonts\inter-400.woff2" (
    echo [SETUP] Downloading fonts and assets, first run only...
    ".venv\Scripts\python.exe" resources\fonts\download_fonts.py || echo [WARN] Font download failed - the hub will use system fonts.
)

:: Run
".venv\Scripts\python.exe" hub.py %*
if errorlevel 42 exit /b 0
pause
