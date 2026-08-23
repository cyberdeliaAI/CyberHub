#!/usr/bin/env python3
"""Publish the complete CyberHub source into a GitHub working copy.

Stable and beta module sources and the unified documentation are copied so they
can be maintained together. Beta modules remain separate distribution packages.
Large or downloaded runtime assets are intentionally excluded from git; restore
them in the dev folder before creating a distribution ZIP with build_release.py.

Usage:
    python publish_github.py /path/to/CyberHub
"""

import os
import shutil
import sys

from build_release import (
    CORE_FILES,
    REPO,
    all_module_names,
    module_files,
)


DOC_FILES = {
    "LICENSE.md": "LICENSE.md",
    "THIRD-PARTY-NOTICES.md": "THIRD-PARTY-NOTICES.md",
    "README.md": "README.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "DATA-ASSETS.md": "DATA-ASSETS.md",
    ".gitignore": ".gitignore",
    "build_release.py": "build_release.py",
    "publish_github.py": "publish_github.py",
    "licenses/GPL-3.0.txt": "licenses/GPL-3.0.txt",
}


SOURCE_RESOURCES = [
    "resources/auto_tagger/README.txt",
    "resources/civitai/README.txt",
    "resources/civitai-grabber/civit_image_downloader.py",
    "resources/civitai-grabber/grabber-requirements.txt",
    "resources/danbooru/README.txt",
    "resources/danbooru/What_is_Danbooru.md",
    "resources/danbooru/tags.json",
    "resources/fonts/README.txt",
    "resources/fonts/download_fonts.py",
    "resources/guides/Illustrious_NoobAI_Prompting_Master_Guide.pdf",
    "resources/guides/Pony_Prompting_Master_Guide.pdf",
    "resources/help/cyberhub-manual.md",
    "resources/help/screenshots/01-gallery-overview.png",
    "resources/help/screenshots/02-gallery-multiselect.png",
    "resources/help/screenshots/08-topbar-menu.png",
    "resources/prompt-engineer/feather.min.js",
    "resources/prompt-engineer/tailwind.css",
]


EXCLUDED_ASSET_HINTS = [
    "resources/auto_tagger/*/model.onnx and selected_tags.csv",
    "resources/civitai/models.json",
    "resources/fonts/*.woff2 / *.ttf / *.TTF",
    "resources/danbooru/model_fp16.onnx",
    "resources/danbooru/tags.csv",
    "resources/danbooru/ort*.js / *.mjs / *.wasm",
    "resources/upscalers/*.onnx",
    "settings.json and cyberdelia.db*",
    "updates/*.zip and update backups",
]


LEGACY_FILES = [
    "LICENSE-LITE.md",
    "LICENSE-FULL.md",
    "README-LITE.md",
    "publish_lite.py",
    "publish_full.py",
    "resources/help/cyberhub-lite-manual.md",
    "resources/help/cyberhub-light-manual.md",
]


def source_files():
    """Return source files while excluding local release-only data assets."""
    files = [path for path in CORE_FILES if not path.endswith("models.json")]
    for module_name in all_module_names():
        files.extend(module_files(module_name))
    tests_dir = os.path.join(REPO, "tests")
    for root, directories, filenames in os.walk(tests_dir):
        directories[:] = [name for name in directories if name != "__pycache__"]
        for filename in filenames:
            if not filename.endswith((".py", ".json")):
                continue
            files.append(os.path.relpath(os.path.join(root, filename), REPO))
    files.extend(SOURCE_RESOURCES)
    return sorted(set(files))


def copy_file(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)


def remove_legacy_files(destination):
    removed = []
    for relative in LEGACY_FILES:
        path = os.path.join(destination, relative)
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
            removed.append(relative)
    return removed


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    destination = os.path.abspath(os.path.expanduser(sys.argv[1]))
    os.makedirs(destination, exist_ok=True)
    if os.path.realpath(destination) == os.path.realpath(REPO):
        print("Refusing to publish onto the dev folder itself; pick a separate folder.")
        raise SystemExit(1)

    copied = 0
    missing = []
    for relative in source_files():
        source = os.path.join(REPO, relative)
        if not os.path.isfile(source):
            missing.append(relative)
            continue
        copy_file(source, os.path.join(destination, relative))
        copied += 1

    requirements = os.path.join(REPO, "requirements.txt")
    if os.path.isfile(requirements):
        copy_file(requirements, os.path.join(destination, "requirements.txt"))
        copied += 1
    else:
        missing.append("requirements.txt")

    for source_name, target_name in DOC_FILES.items():
        source = os.path.join(REPO, source_name)
        if not os.path.isfile(source):
            missing.append(source_name)
            continue
        copy_file(source, os.path.join(destination, target_name))
        copied += 1

    removed = remove_legacy_files(destination)
    modules = all_module_names()
    print(f"Published {copied} files to {destination}")
    print(f"  modules: {', '.join(modules)}")
    if removed:
        print("  removed legacy edition files:")
        for relative in removed:
            print(f"    - {relative}")
    print("  excluded large/runtime assets:")
    for hint in EXCLUDED_ASSET_HINTS:
        print(f"    - {hint}")
    if missing:
        print("  WARNING - missing sources (not copied):")
        for relative in missing:
            print(f"    - {relative}")
    print("\nNext:")
    print(f"  cd {destination}")
    print("  git add -A && git commit -m 'Sync CyberHub' && git push")


if __name__ == "__main__":
    main()
