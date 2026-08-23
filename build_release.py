#!/usr/bin/env python3
"""Build CyberHub release and manually installable update assets.

The complete CyberHub ZIP contains stable modules. Modules marked as beta are
distributed separately and can be built explicitly with --module. Large/runtime
resources are read from the local dev folder when present; see DATA-ASSETS.md
for files intentionally kept out of git. The output also includes an integrity
catalog and, by default, one ZIP per stable module for GitHub Releases.

Run:  python build_release.py [output_dir] [--version 1.3]
Default output_dir = ~/Downloads
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
import zipfile


REPO = os.path.dirname(os.path.abspath(__file__))
UPDATE_REPOSITORY = "cyberdeliaAI/CyberHub"


def default_hub_version():
    """Read Hub.VERSION without importing hub.py and triggering app imports."""
    try:
        with open(os.path.join(REPO, "hub.py"), "r", encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if line.startswith("VERSION"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "1.0"


def normalize_version(value):
    value = (value or "").strip()
    if not value:
        value = default_hub_version()
    if value.lower().startswith("v"):
        value = value[1:].strip()
    return value or "1.0"


def version_tag(version):
    return "v" + normalize_version(version)


CORE_FILES = [
    "hub.py",
    "start.sh",
    "start.bat",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    "README.md",
    "CHANGELOG.md",
    "core/__init__.py",
    "core/civitai.py",
    "core/help.py",
    "core/metadata.py",
    "core/server.py",
    "resources/fonts/download_fonts.py",
    "resources/fonts/README.txt",
    "resources/civitai/README.txt",
    "resources/civitai/models.json",
]


RELEASE_RESOURCES = [
    "resources/auto_tagger/README.txt",
    "resources/civitai-grabber/civit_image_downloader.py",
    "resources/civitai-grabber/grabber-requirements.txt",
    "resources/help/cyberhub-manual.md",
    "resources/danbooru/README.txt",
    "resources/danbooru/What_is_Danbooru.md",
    "resources/danbooru/tags.csv",
    "resources/danbooru/tags.json",
    "resources/danbooru/model_fp16.onnx",
    "resources/danbooru/ort.min.js",
    "resources/danbooru/ort-wasm-simd-threaded.jsep.mjs",
    "resources/danbooru/ort-wasm-simd-threaded.jsep.wasm",
    "resources/upscalers/Real-ESRGAN-x4plus.onnx",
    "resources/guides/Illustrious_NoobAI_Prompting_Master_Guide.pdf",
    "resources/guides/Pony_Prompting_Master_Guide.pdf",
    "resources/prompt-engineer/feather.min.js",
    "resources/prompt-engineer/tailwind.css",
]


# Resources owned by a module and needed when that module is distributed on its
# own. Large optional model bundles remain separate; these are the same baseline
# assets included in the complete CyberHub release.
MODULE_RESOURCES = {
    "civitai_grabber": [
        "resources/civitai-grabber/civit_image_downloader.py",
        "resources/civitai-grabber/grabber-requirements.txt",
    ],
    "danbooru": [
        "resources/danbooru/README.txt",
        "resources/danbooru/What_is_Danbooru.md",
        "resources/danbooru/tags.csv",
        "resources/danbooru/tags.json",
        "resources/danbooru/model_fp16.onnx",
        "resources/danbooru/ort.min.js",
        "resources/danbooru/ort-wasm-simd-threaded.jsep.mjs",
        "resources/danbooru/ort-wasm-simd-threaded.jsep.wasm",
    ],
    "gallery_auto_tagger": [
        "resources/auto_tagger/README.txt",
    ],
    "prompt_engineer": [
        "resources/prompt-engineer/feather.min.js",
        "resources/prompt-engineer/tailwind.css",
    ],
    "upscaler": [
        "resources/upscalers/Real-ESRGAN-x4plus.onnx",
    ],
}


def module_files(module_name):
    """Return files under modules/<name>/, excluding Python caches."""
    base = os.path.join(REPO, "modules", module_name)
    output = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [directory for directory in dirs if directory != "__pycache__"]
        for filename in files:
            if filename.endswith(".pyc") or filename == ".DS_Store":
                continue
            absolute = os.path.join(root, filename)
            output.append(os.path.relpath(absolute, REPO))
    return sorted(output)


def all_module_names():
    base = os.path.join(REPO, "modules")
    return sorted(
        directory for directory in os.listdir(base)
        if os.path.isfile(os.path.join(base, directory, "__init__.py"))
    )


def module_metadata(module_name):
    """Read release metadata without importing the module or its runtime."""
    path = os.path.join(REPO, "modules", module_name, "__init__.py")
    name = module_name.replace("_", " ").title()
    version = "1.0"
    release_stage = "stable"
    try:
        with open(path, "r", encoding="utf-8") as source:
            tree = ast.parse(source.read(), filename=path)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            values = {}
            for item in node.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "name", "version", "release_stage",
                    }:
                        try:
                            values[target.id] = str(ast.literal_eval(item.value))
                        except (ValueError, TypeError):
                            pass
            if "name" in values:
                name = values["name"]
                version = values.get("version", version)
                release_stage = values.get("release_stage", release_stage).strip().lower()
                break
    except (OSError, SyntaxError):
        pass
    if release_stage not in {"stable", "beta"}:
        release_stage = "stable"
    return {
        "name": name,
        "version": normalize_version(version),
        "release_stage": release_stage,
    }


def complete_module_names():
    """Return modules that belong in the complete, stable CyberHub build."""
    return [
        module_name for module_name in all_module_names()
        if module_metadata(module_name)["release_stage"] != "beta"
    ]


def complete_release_resources(module_names=None):
    """Exclude resources owned only by modules omitted from the complete build."""
    included = set(module_names if module_names is not None else complete_module_names())
    excluded_resources = set()
    for module_name in set(all_module_names()) - included:
        excluded_resources.update(MODULE_RESOURCES.get(module_name, []))
    return [path for path in RELEASE_RESOURCES if path not in excluded_resources]


def release_manifest(version):
    return json.dumps({
        "product": "CyberHub",
        "version": normalize_version(version),
    }, indent=2) + "\n"


def release_text(version):
    return f"CyberHub {normalize_version(version)}\n"


def package_manifest(package_type, package_id, version, minimum_hub_version=""):
    return json.dumps({
        "schema": 1,
        "product": "CyberHub",
        "type": package_type,
        "id": package_id,
        "version": normalize_version(version),
        "minimum_hub_version": normalize_version(minimum_hub_version) if minimum_hub_version else "",
    }, indent=2) + "\n"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def versioned_hub_source(version):
    """Return hub.py with the release version used inside the archive."""
    source_path = os.path.join(REPO, "hub.py")
    with open(source_path, "r", encoding="utf-8") as source:
        text = source.read()
    replacement = rf'\g<1>"{normalize_version(version)}"'
    text, count = re.subn(
        r'(?m)^(\s*VERSION\s*=\s*)["\'][^"\']+["\']\s*$',
        replacement,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not locate Hub.VERSION in hub.py")
    return text


def write_zip(zip_path, file_list, version, *, manifest, complete_release=False):
    missing = [path for path in file_list if not os.path.isfile(os.path.join(REPO, path))]
    if missing:
        print("  MISSING (skipped):")
        for path in missing:
            print(f"    - {path}")

    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in file_list:
            source = os.path.join(REPO, path)
            if os.path.isfile(source):
                if path == "hub.py":
                    archive.writestr(path, versioned_hub_source(version))
                else:
                    archive.write(source, path)
                written += 1
        # Keep the manifest below resources/ so CyberHub 1.2.x importers, which
        # already allow that directory, can install the first updater release.
        archive.writestr("resources/cyberhub-package.json", manifest)
        written += 1

        if complete_release:
            archive.write(os.path.join(REPO, "requirements.txt"), "requirements.txt")
            archive.writestr("cyberhub-version.txt", release_text(version))
            archive.writestr("resources/release.json", release_manifest(version))
            written += 3

            for source_name, archive_name in [
                ("LICENSE.md", "LICENSE.md"),
                ("THIRD-PARTY-NOTICES.md", "THIRD-PARTY-NOTICES.md"),
                ("licenses/GPL-3.0.txt", "licenses/GPL-3.0.txt"),
            ]:
                source = os.path.join(REPO, source_name)
                if os.path.isfile(source):
                    archive.write(source, archive_name)
                    written += 1
                else:
                    print(f"  MISSING (license): {source_name}")

    size_kb = os.path.getsize(zip_path) // 1024
    print(f"  -> {zip_path}  ({size_kb} KB, {written} files)")
    return {
        "asset": os.path.basename(zip_path),
        "sha256": sha256_file(zip_path),
        "size": os.path.getsize(zip_path),
    }


def build_catalog(version, packages):
    return {
        "schema": 1,
        "product": "CyberHub",
        "repository": UPDATE_REPOSITORY,
        "release_version": normalize_version(version),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packages": packages,
    }


def main():
    parser = argparse.ArgumentParser(description="Build the complete CyberHub release zip.")
    parser.add_argument(
        "output_dir", nargs="?", default=os.path.expanduser("~/Downloads"),
        help="Where to write the release zip (default: ~/Downloads)",
    )
    parser.add_argument(
        "--version", "-v", default=None,
        help="Release version, for example 1.3. If omitted in a terminal, you will be prompted.",
    )
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="Do not prompt; use Hub.VERSION when --version is omitted.",
    )
    parser.add_argument(
        "--no-module-updates", action="store_true",
        help="Build only the complete ZIP and omit separate module update ZIPs.",
    )
    parser.add_argument(
        "--modules-only", action="store_true",
        help="Build only explicitly selected module ZIPs; requires at least one --module.",
    )
    parser.add_argument(
        "--module", action="append", dest="selected_modules", metavar="NAME",
        help=(
            "Build a separate update ZIP for this module only. Repeat for multiple modules. "
            "Without this option every stable module gets an update ZIP."
        ),
    )
    parser.add_argument(
        "--module-min-hub", default=None,
        help=(
            "Minimum installed Hub version for separate module ZIPs. "
            "Defaults to this release version; set it explicitly for a module-only patch."
        ),
    )
    args = parser.parse_args()

    version = args.version
    if version is None:
        default_version = default_hub_version()
        if not args.no_prompt and sys.stdin.isatty():
            entered = input(f"Release version [{default_version}]: ").strip()
            version = entered or default_version
        else:
            version = default_version
    version = normalize_version(version)
    module_min_hub = normalize_version(args.module_min_hub or version)
    known_modules = all_module_names()
    complete_modules = complete_module_names()
    selected_modules = args.selected_modules or complete_modules
    unknown_modules = sorted(set(selected_modules) - set(known_modules))
    if unknown_modules:
        parser.error("Unknown module(s): " + ", ".join(unknown_modules))
    if args.no_module_updates and args.selected_modules:
        parser.error("--no-module-updates cannot be combined with --module")
    if args.modules_only and args.no_module_updates:
        parser.error("--modules-only cannot be combined with --no-module-updates")
    if args.modules_only and not args.selected_modules:
        parser.error("--modules-only requires at least one --module")

    output_dir = os.path.abspath(os.path.expanduser(args.output_dir))
    os.makedirs(output_dir, exist_ok=True)
    zip_name = f"cyberhub_{version_tag(version)}.zip"

    packages = []
    if not args.modules_only:
        files = list(CORE_FILES)
        for module_name in complete_modules:
            files.extend(module_files(module_name))
        files.extend(complete_release_resources(complete_modules))

        print(f"CyberHub {version} ({zip_name}):")
        print(f"  modules: {', '.join(complete_modules)}")
        full_record = write_zip(
            os.path.join(output_dir, zip_name),
            sorted(set(files)),
            version,
            manifest=package_manifest("hub", "cyberhub", version),
            complete_release=True,
        )
        packages.append({
            "type": "hub",
            "id": "cyberhub",
            "name": "CyberHub",
            "version": version,
            "minimum_hub_version": "",
            **full_record,
        })

    if not args.no_module_updates:
        print("\nModule update packages:" if packages else "Module update packages:")
        for module_name in selected_modules:
            metadata = module_metadata(module_name)
            module_version = metadata["version"]
            module_zip_name = f"cyberhub_module_{module_name}_v{module_version}.zip"
            module_record = write_zip(
                os.path.join(output_dir, module_zip_name),
                sorted(set(module_files(module_name) + MODULE_RESOURCES.get(module_name, []))),
                module_version,
                manifest=package_manifest(
                    "module", module_name, module_version,
                    minimum_hub_version=module_min_hub,
                ),
            )
            packages.append({
                "type": "module",
                "id": module_name,
                "name": metadata["name"],
                "version": module_version,
                "minimum_hub_version": module_min_hub,
                "release_stage": metadata["release_stage"],
                **module_record,
            })

    catalog_path = os.path.join(output_dir, "cyberhub-update.json")
    with open(catalog_path, "w", encoding="utf-8", newline="\n") as output:
        json.dump(build_catalog(version, packages), output, indent=2)
        output.write("\n")
    print(f"\nUpdate catalog:\n  -> {catalog_path}")
    print("  Upload the catalog and every ZIP listed in it to the same GitHub Release.")
    print("\nDone.")


if __name__ == "__main__":
    main()
