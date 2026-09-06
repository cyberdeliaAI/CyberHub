"""Settings module — configuration UI for the hub and every loaded module.

Settings page layout:
    1. Hub (title, port, startup module, network)
    2. Civitai integration (models.json path)
    3. Modules (enable/disable + per-module schema-driven settings)

Per-module settings are driven by each module's `settings_schema` dict.
Supported field types: string, folder, file, number, bool, select.
"""

from core import Module, available_module_classes, module_key_from_class
from core.server import build_shell, module_icon_html
from datetime import datetime
from io import BytesIO
import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import sys
import tempfile
import time
import threading
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


UPDATE_REPOSITORY = "cyberdeliaAI/CyberHub"
UPDATE_API_ROOT = f"https://api.github.com/repos/{UPDATE_REPOSITORY}"
UPDATE_CATALOG_ASSET = "cyberhub-update.json"


def _version_key(value):
    """Return a conservative comparable key for CyberHub-style versions."""
    text = str(value or "0").strip().lower().lstrip("v")
    main, separator, suffix = text.partition("-")
    main = main.split("+", 1)[0]
    numbers = [int(part) for part in re.findall(r"\d+", main)[:4]]
    numbers.extend([0] * (4 - len(numbers)))
    # A stable release sorts after a prerelease with the same numeric version.
    return tuple(numbers) + ((1, "") if not separator else (0, suffix))


def _is_newer_version(candidate, current):
    return _version_key(candidate) > _version_key(current)


def _github_transport_url_allowed(url):
    """Allow only HTTPS endpoints used by GitHub's API and asset redirects."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (
                host in {"api.github.com", "github.com"}
                or host.endswith(".githubusercontent.com")
            )
        )
    except (TypeError, ValueError):
        return False


class _GitHubRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that leave GitHub-controlled HTTPS hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _github_transport_url_allowed(newurl):
            raise HTTPError(newurl, 403, "Unsafe update redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SettingsModule(Module):
    name = "Settings"
    version = "1.4.1"
    icon = "\u2699"   # ⚙
    description = "Configure the hub and individual modules."
    show_in_tabs = False     # gear icon in topbar handles navigation
    order = 999

    def __init__(self, hub):
        super().__init__(hub)
        self._github_update_lock = threading.Lock()
        self._github_update_running = False
        self._github_update_status = {
            "running": False,
            "stage": "idle",
            "message": "No update job is running.",
            "downloaded": 0,
            "total": 0,
            "percent": 0,
        }

    def routes_get(self):
        return {
            "/settings": self._page,
            "/api/settings": self._api_settings,
            "/api/settings/modules": self._api_modules,
            "/api/browse": self._api_browse,
            "/api/civitai/info": self._api_civitai_info,
            "/api/civitai/status": self._api_civitai_status,
            "/api/system/info": self._api_system_info,
            "/api/settings/update/check": self._api_update_check,
            "/api/settings/update/status": self._api_update_status,
        }

    def routes_post(self):
        return {
            "/api/settings/save": self._api_save,
            "/api/settings/save_path": self._api_save_path,
            "/api/settings/module/toggle": self._api_module_toggle,
            "/api/settings/network/regen_token": self._api_regen_token,
            "/api/civitai/update": self._api_civitai_update,
            "/api/settings/import_package": self._api_import_package,
            "/api/settings/update/install": self._api_update_install,
            "/api/restart": self._api_restart,
        }

    # ─── Pages ────────────────────────────────────────────────────────────
    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="settings", page_title="Settings",
            body_html=SETTINGS_BODY,
        )
        handler.respond_html(html)

    # ─── Browse API ────────────────────────────────────────────────────────
    @staticmethod
    def _local_interface_ips():
        ips = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                addr = info[4][0]
                if addr:
                    ips.add(addr)
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                ips.add(probe.getsockname()[0])
        except OSError:
            pass
        return ips

    def _api_browse(self, handler, qs):
        """List directories (and optionally files) at a given path.

        Returns precomputed absolute paths for every entry, breadcrumb,
        and parent — so the frontend never has to do platform-specific
        path joining (which broke v5 on Linux and looked ugly on Windows).

        Local-only by default: this endpoint takes any absolute path without
        root confinement. A LAN caller is allowed after passing CyberHub token
        authentication, or when `allow_remote_browse` is explicitly enabled.
        """
        client_ip = (handler.client_address[0] if hasattr(handler, "client_address") else "")
        # IPv4 loopback range (127.0.0.0/8) and IPv6 loopback (::1)
        is_local = (
            client_ip.startswith("127.")
            or client_ip in self._local_interface_ips()
        )
        allow_remote = bool(self.hub.settings.get_path("network.allow_remote_browse", False))
        authenticated_remote = bool(handler.require_auth and handler.auth_token)
        if not is_local and not allow_remote and not authenticated_remote:
            handler.respond_json({
                "error": "Folder browser is only available on the local machine. Enable Settings > Network > Allow remote folder browser to browse this Hub PC from another device."
            }, status=403)
            return
        import platform
        req_path = qs.get("path", [""])[0].strip()
        file_ext = qs.get("ext", [""])[0].strip().lower()  # e.g. ".json"
        is_windows = (platform.system() == "Windows")
        sep = os.sep

        # No path → list drives (Windows) or root (Linux/Mac)
        if not req_path:
            if is_windows:
                import string
                drives = []
                for letter in string.ascii_uppercase:
                    dp = f"{letter}:" + sep
                    if os.path.isdir(dp):
                        drives.append({"name": dp, "path": dp})
                handler.respond_json({
                    "path": "", "display": "Drives",
                    "parent": None,
                    "crumbs": [],
                    "dirs": drives, "files": [],
                    "is_drives": True, "sep": sep,
                })
                return
            else:
                req_path = "/"

        # Normalize: collapse "C:\Users\..\Users" → "C:\Users", trim trailing seps
        try:
            req_path = os.path.abspath(req_path)
        except Exception:
            handler.respond_json({"error": "Invalid path", "path": req_path}, status=400)
            return

        if not os.path.isdir(req_path):
            handler.respond_json({"error": "Not a directory", "path": req_path}, status=404)
            return

        # Parent: dirname, but at root point back to drives list (empty path)
        parent = os.path.dirname(req_path)
        if parent == req_path or not parent:
            parent = "" if is_windows else None
        # On Linux/Mac, "/" has dirname "/", which equals req_path → parent stays None
        # (no further up; clicking 🏠 still goes to drives/root via empty path)

        # Build breadcrumbs from absolute path
        crumbs = []
        if is_windows and len(req_path) >= 2 and req_path[1] == ":":
            # Windows: split off drive, then walk subsegments
            drive = req_path[:2] + sep  # "C:\"
            crumbs.append({"label": drive, "path": drive})
            rest = req_path[len(drive):].rstrip(sep)
            if rest:
                walking = drive
                for part in rest.split(sep):
                    if not part: continue
                    walking = os.path.join(walking, part)
                    crumbs.append({"label": part, "path": walking})
        else:
            # POSIX: start at /, walk down
            crumbs.append({"label": "/", "path": "/"})
            rest = req_path.lstrip("/")
            if rest:
                walking = "/"
                for part in rest.split("/"):
                    if not part: continue
                    walking = walking + part if walking == "/" else walking + "/" + part
                    crumbs.append({"label": part, "path": walking})

        dirs = []
        files = []
        try:
            for entry in sorted(os.listdir(req_path), key=str.lower):
                if entry.startswith("."):
                    continue
                full = os.path.join(req_path, entry)
                try:
                    if os.path.isdir(full):
                        dirs.append({"name": entry, "path": full})
                    elif file_ext and entry.lower().endswith(file_ext):
                        files.append({"name": entry, "path": full})
                except OSError:
                    continue  # broken symlinks etc.
        except PermissionError:
            handler.respond_json({"error": "Permission denied", "path": req_path}, status=403)
            return

        handler.respond_json({
            "path": req_path,
            "display": req_path,
            "parent": parent,
            "crumbs": crumbs,
            "dirs": dirs,
            "files": files,
            "is_drives": False,
            "sep": sep,
        })

    # ─── API ──────────────────────────────────────────────────────────────
    def _api_settings(self, handler, qs):
        import copy
        data = copy.deepcopy(self.hub.settings.data)
        data["_hub_version"] = getattr(self.hub, "VERSION", "1.0")
        # Don't leak the API key. Return a boolean signal instead so the UI
        # can show a "key saved" hint without putting a fake value in the input.
        civ = data.get("civitai")
        if isinstance(civ, dict):
            civ["api_key_set"] = bool(civ.get("api_key"))
            civ.pop("api_key", None)
        # The LAN access token is only revealed to localhost — over the network
        # the requester has already proven they have it (or they couldn't be here).
        net = data.get("network")
        if isinstance(net, dict):
            net["auth_token_set"] = bool(net.get("auth_token"))
            if not handler._is_loopback():
                net.pop("auth_token", None)
        handler.respond_json(data)

    def _api_regen_token(self, handler, content_len, content_type):
        """Replace network.auth_token with a fresh value. Loopback only."""
        if not handler._is_loopback():
            handler.respond_json({"error": "Token regeneration is only allowed from localhost"}, status=403)
            return
        import secrets
        new_token = secrets.token_urlsafe(24)
        self.hub.settings.set_path("network.auth_token", new_token)
        handler.respond_json({"ok": True, "token": new_token, "note": "Restart required to apply"})

    def _api_modules(self, handler, qs):
        modules_by_key = {}
        name_to_key = {}
        loaded = getattr(self.hub.registry, "modules", {}) or {}

        def add_module(key, source, loaded_mod=None):
            name = getattr(source, "name", key)
            norm_name = str(name).strip().lower()
            existing_key = name_to_key.get(norm_name)
            if existing_key and existing_key != key:
                existing = modules_by_key.get(existing_key)
                existing_loaded = bool(existing and existing.get("loaded"))
                if existing_loaded and loaded_mod is None:
                    return
                modules_by_key.pop(existing_key, None)
            name_to_key[norm_name] = key
            modules_by_key[key] = {
                "key": key,
                "name": name,
                "version": getattr(source, "version", "1.0"),
                "release_stage": getattr(source, "release_stage", "stable"),
                "icon": getattr(source, "icon", ""),
                "icon_html": module_icon_html(key),
                "description": getattr(source, "description", ""),
                "order": getattr(source, "order", 100),
                "show_in_tabs": getattr(source, "show_in_tabs", True),
                "enabled": key in {"settings", "module_manager"} or self.hub.settings.is_module_enabled(key),
                "protected": key in {"settings", "module_manager"},
                "loaded": loaded_mod is not None,
                "settings_schema": getattr(source, "settings_schema", {}),
                "current_settings": self.hub.settings.get_module(key),
            }

        for _folder, cls in available_module_classes():
            key = module_key_from_class(cls)
            mod = loaded.get(key)
            add_module(key, mod or cls, mod)

        for key, mod in loaded.items():
            add_module(key, mod, mod)

        modules = list(modules_by_key.values())
        # Sort by display order for the UI
        modules.sort(key=lambda m: (m["order"], m["name"].lower()))
        handler.respond_json(modules)

    def _api_save(self, handler, content_len, content_type):
        """Save a single module setting OR a top-level setting.

        Body: {"module": "gallery", "key": "per_page", "value": 200}
        Or for top-level: {"module": "", "key": "port", "value": 8899}
        """
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        module_key = (data.get("module") or "").strip().lower()
        key = data.get("key", "").strip()
        if not key:
            handler.respond_json({"error": "Missing key"}, status=400); return
        value = data.get("value")
        if module_key:
            if not self.hub.registry.get(module_key):
                handler.respond_json({"error": "Unknown module"}, status=404); return
            self.hub.settings.set_module_setting(module_key, key, value)
            mod = self.hub.registry.get(module_key)
            if mod:
                try:
                    mod.on_settings_changed(key, value)
                except Exception as e:
                    print(f"[SETTINGS] {module_key}.on_settings_changed raised: {e}")
        else:
            allowed_top_level = {"title", "startup", "theme", "port"}
            if key not in allowed_top_level:
                handler.respond_json({"error": "Setting cannot be modified through this endpoint"}, status=403); return
            self.hub.settings.set(key, value)
        handler.respond_json({"ok": True})

    def _api_save_path(self, handler, content_len, content_type):
        """Save a dotted top-level path (e.g. network.listen_lan).

        Body: {"path": "network.listen_lan", "value": true}
        """
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        path = (data.get("path") or "").strip()
        if not path:
            handler.respond_json({"error": "Missing path"}, status=400); return
        allowed_paths = {
            "civitai.models_path", "civitai.api_key",
            "civitai.verbose",
            "network.listen_lan", "network.share_network", "network.require_auth",
            "network.allow_remote_browse",
        }
        if path not in allowed_paths:
            handler.respond_json({"error": "Setting cannot be modified through this endpoint"}, status=403); return
        self.hub.settings.set_path(path, data.get("value"))
        if path == "civitai.verbose":
            self.hub.civitai.debug = bool(data.get("value"))
        handler.respond_json({"ok": True})

    def _api_module_toggle(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        module_key = (data.get("module") or "").strip().lower()
        if module_key in {"settings", "module_manager"}:
            handler.respond_json({"error": "Cannot disable a CyberHub system module"}, status=400); return
        enabled = bool(data.get("enabled", True))
        self.hub.settings.set_module_setting(module_key, "enabled", enabled)
        handler.respond_json({
            "ok": True, "module": module_key, "enabled": enabled,
            "note": "Restart required to apply",
        })

    # ─── Manual GitHub updates ───────────────────────────────────────────
    @staticmethod
    def _release_asset_digest(asset):
        digest = str((asset or {}).get("digest") or "").strip().lower()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return digest.split(":", 1)[1]
        return ""

    @staticmethod
    def _safe_release_page(url):
        try:
            parsed = urlparse(str(url or ""))
            expected = f"/{UPDATE_REPOSITORY}/releases/".lower()
            if (
                parsed.scheme == "https"
                and (parsed.hostname or "").lower() == "github.com"
                and parsed.path.lower().startswith(expected)
            ):
                return url
        except (TypeError, ValueError):
            pass
        return ""

    @staticmethod
    def _validate_release_asset_url(url):
        if not _github_transport_url_allowed(url):
            raise ValueError("GitHub returned an unsafe update URL.")
        parsed = urlparse(url)
        expected = f"/{UPDATE_REPOSITORY}/releases/download/".lower()
        if (parsed.hostname or "").lower() != "github.com" or not parsed.path.lower().startswith(expected):
            raise ValueError("Update asset does not belong to the official CyberHub repository.")

    def _read_github_url(self, url, *, max_bytes, expected_sha256="", output_path="", progress=None):
        if not _github_transport_url_allowed(url):
            raise ValueError("Refusing a non-GitHub update URL.")
        request = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"CyberHub/{getattr(self.hub, 'VERSION', '1.0')}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        opener = build_opener(_GitHubRedirectHandler())
        digest = hashlib.sha256()
        downloaded = 0
        chunks = [] if not output_path else None
        try:
            with opener.open(request, timeout=30) as response:
                final_url = response.geturl()
                if not _github_transport_url_allowed(final_url):
                    raise ValueError("GitHub redirected the update to an unsafe host.")
                raw_length = response.headers.get("Content-Length", "")
                try:
                    content_length = int(raw_length) if raw_length else 0
                except (TypeError, ValueError):
                    content_length = 0
                if content_length and content_length > max_bytes:
                    raise ValueError(
                        f"Update download is too large ({content_length // 1024 // 1024} MB)."
                    )
                target = open(output_path, "wb") if output_path else None
                try:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError("Update download exceeds the allowed size.")
                        digest.update(chunk)
                        if target:
                            target.write(chunk)
                        else:
                            chunks.append(chunk)
                        if progress:
                            progress(downloaded, content_length)
                finally:
                    if target:
                        target.close()
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("No public CyberHub release was found on GitHub.") from exc
            if exc.code in {403, 429}:
                raise ValueError("GitHub temporarily refused the update request. Try again later.") from exc
            raise ValueError(f"GitHub update request failed (HTTP {exc.code}).") from exc
        except URLError as exc:
            raise ValueError(f"Could not reach GitHub: {exc.reason}") from exc

        actual_sha256 = digest.hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256.lower():
            raise ValueError("Downloaded update failed its SHA-256 integrity check.")
        if output_path:
            return {"bytes": downloaded, "sha256": actual_sha256}
        return b"".join(chunks)

    def _github_api_json(self, endpoint):
        if not endpoint.startswith("/") or ".." in endpoint:
            raise ValueError("Invalid GitHub API endpoint.")
        raw = self._read_github_url(
            UPDATE_API_ROOT + endpoint,
            max_bytes=4 * 1024 * 1024,
        )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub returned an invalid release response.") from exc
        if not isinstance(value, dict):
            raise ValueError("GitHub returned an unexpected release response.")
        return value

    def _release_asset_bytes(self, asset, *, max_bytes):
        if not isinstance(asset, dict):
            raise ValueError("Update release asset is missing.")
        url = str(asset.get("browser_download_url") or "")
        self._validate_release_asset_url(url)
        expected = self._release_asset_digest(asset)
        if not expected:
            raise ValueError("GitHub did not provide a SHA-256 digest for the update catalog.")
        size = int(asset.get("size") or 0)
        if size < 1 or size > max_bytes:
            raise ValueError("Update catalog has an invalid size.")
        return self._read_github_url(url, max_bytes=max_bytes, expected_sha256=expected)

    @staticmethod
    def _validate_update_catalog(value):
        if not isinstance(value, dict):
            raise ValueError("CyberHub update catalog is not a JSON object.")
        if value.get("schema") != 1 or value.get("product") != "CyberHub":
            raise ValueError("Unsupported CyberHub update catalog.")
        repository = str(value.get("repository") or "")
        if repository != UPDATE_REPOSITORY:
            raise ValueError("Update catalog belongs to another repository.")
        packages = value.get("packages")
        if not isinstance(packages, list) or len(packages) > 100:
            raise ValueError("Update catalog contains an invalid package list.")

        cleaned = []
        seen_assets = set()
        for package in packages:
            if not isinstance(package, dict):
                raise ValueError("Update catalog contains an invalid package.")
            package_type = str(package.get("type") or "")
            package_id = str(package.get("id") or "")
            version = str(package.get("version") or "").strip().lstrip("v")
            asset = str(package.get("asset") or "")
            sha256 = str(package.get("sha256") or "").lower()
            minimum = str(package.get("minimum_hub_version") or "").strip().lstrip("v")
            if package_type not in {"hub", "module"}:
                raise ValueError("Update catalog contains an unknown package type.")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", package_id):
                raise ValueError("Update catalog contains an invalid package identifier.")
            if package_type == "hub" and package_id != "cyberhub":
                raise ValueError("Update catalog contains an invalid Hub package identifier.")
            if not version or len(version) > 40:
                raise ValueError("Update catalog contains an invalid package version.")
            if os.path.basename(asset) != asset or not asset.lower().endswith(".zip"):
                raise ValueError("Update catalog contains an invalid asset name.")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("Update catalog package has no valid SHA-256 digest.")
            if asset in seen_assets:
                raise ValueError("Update catalog contains duplicate assets.")
            seen_assets.add(asset)
            cleaned.append({
                "type": package_type,
                "id": package_id,
                "name": str(package.get("name") or package_id)[:120],
                "version": version,
                "minimum_hub_version": minimum,
                "asset": asset,
                "sha256": sha256,
            })
        return {
            "schema": 1,
            "product": "CyberHub",
            "repository": repository,
            "release_version": str(value.get("release_version") or "").strip().lstrip("v"),
            "packages": cleaned,
        }

    def _release_catalog(self, release):
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("Online installation only supports stable published releases.")
        assets = {
            str(asset.get("name") or ""): asset
            for asset in (release.get("assets") or [])
            if isinstance(asset, dict)
        }
        catalog_asset = assets.get(UPDATE_CATALOG_ASSET)
        if not catalog_asset:
            raise ValueError(
                "This release has no CyberHub update catalog. Use the manual ZIP import instead."
            )
        raw = self._release_asset_bytes(catalog_asset, max_bytes=2 * 1024 * 1024)
        try:
            catalog = self._validate_update_catalog(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("CyberHub update catalog is invalid JSON.") from exc

        release_version = str(catalog.get("release_version") or "")
        tag_version = str(release.get("tag_name") or "").strip().lstrip("v")
        if release_version and tag_version and _version_key(release_version) != _version_key(tag_version):
            raise ValueError("Update catalog version does not match the GitHub release tag.")

        for package in catalog["packages"]:
            asset = assets.get(package["asset"])
            if not asset:
                raise ValueError(f"GitHub release is missing {package['asset']}.")
            api_digest = self._release_asset_digest(asset)
            if not api_digest or api_digest != package["sha256"]:
                raise ValueError(f"Integrity information does not match for {package['asset']}.")
            package["size"] = int(asset.get("size") or 0)
            package["asset_id"] = int(asset.get("id") or 0)
        return catalog, assets

    def _installed_update_versions(self):
        modules = {}
        for folder, cls in available_module_classes():
            modules[folder] = {
                "name": str(getattr(cls, "name", folder)),
                "version": str(getattr(cls, "version", "1.0")),
            }
        return {
            "hub": str(getattr(self.hub, "VERSION", "1.0")),
            "modules": modules,
        }

    def _package_state(self, package, installed=None):
        installed = installed or self._installed_update_versions()
        if package["type"] == "hub":
            current = installed["hub"]
            present = True
        else:
            module = installed["modules"].get(package["id"])
            current = str((module or {}).get("version") or "0")
            present = module is not None
        minimum = package.get("minimum_hub_version") or ""
        compatible = not minimum or not _is_newer_version(minimum, installed["hub"])
        return {
            **package,
            "current_version": current if present else "Not installed",
            "installed": present,
            "compatible": compatible,
            "update_available": (not present or _is_newer_version(package["version"], current)),
        }

    def _release_update_info(self, release):
        catalog, _assets = self._release_catalog(release)
        installed = self._installed_update_versions()
        packages = [self._package_state(package, installed) for package in catalog["packages"]]
        return {
            "ok": True,
            "repository": UPDATE_REPOSITORY,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "current_hub_version": installed["hub"],
            "release": {
                "id": int(release.get("id") or 0),
                "tag": str(release.get("tag_name") or ""),
                "name": str(release.get("name") or release.get("tag_name") or "CyberHub update")[:160],
                "published_at": str(release.get("published_at") or ""),
                "url": self._safe_release_page(release.get("html_url")),
                "notes": str(release.get("body") or "")[:30000],
            },
            "packages": packages,
        }

    def _api_update_check(self, handler, qs):
        if not handler._is_loopback():
            handler.respond_json({"error": "Update checks are only available from localhost."}, status=403)
            return
        try:
            release = self._github_api_json("/releases/latest")
            handler.respond_json(self._release_update_info(release))
        except Exception as exc:
            handler.respond_json({"error": str(exc)}, status=502)

    def _set_github_update_status(self, **values):
        with self._github_update_lock:
            self._github_update_status.update(values)

    def _api_update_status(self, handler, qs):
        if not handler._is_loopback():
            handler.respond_json({"error": "Update status is only available from localhost."}, status=403)
            return
        with self._github_update_lock:
            payload = dict(self._github_update_status)
        handler.respond_json(payload)

    def _download_release_asset(self, asset, package, output_path):
        url = str(asset.get("browser_download_url") or "")
        self._validate_release_asset_url(url)
        expected_size = int(asset.get("size") or 0)
        if expected_size < 1 or expected_size > self._IMPORT_MAX_UPLOAD:
            raise ValueError("Update package has an invalid or unsupported size.")

        def progress(downloaded, response_total):
            total = response_total or expected_size
            percent = int(downloaded * 100 / total) if total else 0
            self._set_github_update_status(
                stage="download",
                message=f"Downloading {package['name']}...",
                downloaded=downloaded,
                total=total,
                percent=max(0, min(100, percent)),
            )

        result = self._read_github_url(
            url,
            max_bytes=self._IMPORT_MAX_UPLOAD,
            expected_sha256=package["sha256"],
            output_path=output_path,
            progress=progress,
        )
        if expected_size and result["bytes"] != expected_size:
            raise ValueError("Downloaded update size does not match the GitHub release.")

    def _github_update_worker(self, release_id, asset_name):
        download_path = ""
        try:
            self._set_github_update_status(
                running=True, stage="metadata", message="Validating the GitHub release...",
                downloaded=0, total=0, percent=0, error="", result=None,
            )
            release = self._github_api_json(f"/releases/{release_id}")
            catalog, assets = self._release_catalog(release)
            package = next(
                (item for item in catalog["packages"] if item["asset"] == asset_name),
                None,
            )
            if not package:
                raise ValueError("Selected package is not listed in the official update catalog.")
            state = self._package_state(package)
            if not state["compatible"]:
                raise ValueError(
                    f"{package['name']} requires CyberHub {package['minimum_hub_version']} or newer."
                )
            if not state["update_available"]:
                raise ValueError(f"{package['name']} {package['version']} is already installed.")

            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            download_dir = os.path.join(root, "updates", "downloads")
            os.makedirs(download_dir, exist_ok=True)
            fd, download_path = tempfile.mkstemp(prefix="cyberhub-update-", suffix=".zip", dir=download_dir)
            os.close(fd)
            self._download_release_asset(assets[asset_name], package, download_path)
            self._set_github_update_status(
                stage="install", message=f"Installing {package['name']}...", percent=100,
            )
            result = self._install_import_zip(
                download_path,
                asset_name,
                require_manifest=True,
                expected_package=package,
            )
            result["package"] = {
                "type": package["type"],
                "id": package["id"],
                "name": package["name"],
                "version": package["version"],
            }
            self._set_github_update_status(
                running=False, stage="done",
                message=f"{package['name']} {package['version']} installed. Restart CyberHub to apply it.",
                percent=100, result=result, error="",
            )
        except Exception as exc:
            self._set_github_update_status(
                running=False, stage="error", message="Update failed.", error=str(exc), result=None,
            )
        finally:
            if download_path:
                try:
                    os.remove(download_path)
                except OSError:
                    pass
            with self._github_update_lock:
                self._github_update_running = False

    def _api_update_install(self, handler, content_len, content_type):
        if not handler._is_loopback():
            handler.respond_json({"error": "Online update installation is only available from localhost."}, status=403)
            return
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        try:
            release_id = int(data.get("release_id") or 0)
        except (TypeError, ValueError):
            release_id = 0
        asset_name = str(data.get("asset") or "")
        if release_id < 1 or os.path.basename(asset_name) != asset_name or not asset_name.endswith(".zip"):
            handler.respond_json({"error": "Invalid update selection."}, status=400)
            return
        with self._github_update_lock:
            if self._github_update_running:
                handler.respond_json({"error": "Another update is already running."}, status=409)
                return
            self._github_update_running = True
            self._github_update_status = {
                "running": True,
                "stage": "starting",
                "message": "Starting update...",
                "downloaded": 0,
                "total": 0,
                "percent": 0,
                "error": "",
                "result": None,
            }
        threading.Thread(
            target=self._github_update_worker,
            args=(release_id, asset_name),
            daemon=True,
            name="cyberhub-github-update",
        ).start()
        handler.respond_json({"ok": True, "running": True})

    # ─── Update / module package import ───────────────────────────────────
    _IMPORT_MAX_UPLOAD = 512 * 1024 * 1024
    _IMPORT_MAX_UNPACKED = 768 * 1024 * 1024
    _IMPORT_MAX_FILES = 4000
    _IMPORT_ROOT_FILES = {
        "hub.py", "requirements.txt", "start.sh", "start.bat",
        "README.md", "CONTRIBUTING.md", "DATA-ASSETS.md",
        "LICENSE.md",
        "THIRD-PARTY-NOTICES.md", "cyberhub-version.txt",
        "build_release.py", "publish_github.py", "Dockerfile",
        "docker-compose.yml", ".dockerignore", "cyberhub-package.json",
    }
    _IMPORT_PACKAGE_METADATA = {
        "cyberhub-package.json", "resources/cyberhub-package.json",
    }
    _IMPORT_ROOT_DIRS = (
        "core/", "modules/", "resources/", "licenses/",
    )
    _IMPORT_BLOCKED_PARTS = {
        "__pycache__", ".git", ".github", ".venv", "venv", "env",
        "__MACOSX", ".codex", ".agents",
    }
    _IMPORT_BLOCKED_NAMES = {
        "settings.json", ".cyberhub.lock", "cyberdelia.db",
        "cyberdelia.db-wal", "cyberdelia.db-shm",
    }
    _IMPORT_BLOCKED_EXTS = (
        ".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
    )

    @staticmethod
    def _clean_zip_path(name):
        raw = (name or "").replace("\\", "/").strip()
        if not raw or raw.endswith("/"):
            return ""
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise ValueError(f"Unsafe absolute path in zip: {name}")
        norm = posixpath.normpath(raw)
        while norm.startswith("./"):
            norm = norm[2:]
        if not norm or norm == ".":
            return ""
        if norm.startswith("../") or norm == ".." or "/../" in norm:
            raise ValueError(f"Unsafe parent path in zip: {name}")
        return norm

    @classmethod
    def _is_blocked_import_path(cls, rel):
        parts = rel.split("/")
        base = parts[-1]
        if any(part in cls._IMPORT_BLOCKED_PARTS for part in parts):
            return True
        if base in cls._IMPORT_BLOCKED_NAMES:
            return True
        if base.startswith(".") and base not in {".gitignore"}:
            return True
        return base.lower().endswith(cls._IMPORT_BLOCKED_EXTS)

    @classmethod
    def _allowed_root_import_path(cls, rel):
        if rel in cls._IMPORT_ROOT_FILES:
            return True
        return any(rel.startswith(prefix) for prefix in cls._IMPORT_ROOT_DIRS)

    @staticmethod
    def _module_name_from_zip(filename):
        stem = os.path.splitext(os.path.basename(filename or ""))[0]
        stem = stem.replace("-", "_").replace(" ", "_").lower()
        stem = re.sub(r"[^a-z0-9_]", "", stem)
        if not stem or not re.match(r"^[a-z_][a-z0-9_]*$", stem):
            return "imported_module"
        return stem

    @staticmethod
    def _single_top(paths):
        tops = {p.split("/", 1)[0] for p in paths if p}
        return next(iter(tops)) if len(tops) == 1 else ""

    @classmethod
    def _strip_wrapper_folder(cls, paths):
        """Strip a GitHub-style single parent folder when it wraps a hub zip."""
        top = cls._single_top(paths)
        if not top:
            return paths
        stripped = [p.split("/", 1)[1] for p in paths if "/" in p]
        if not stripped:
            return paths
        looks_like_hub = any(
            p in cls._IMPORT_ROOT_FILES or p.startswith(("modules/", "core/", "resources/"))
            for p in stripped
        )
        return stripped if looks_like_hub else paths

    @staticmethod
    def _zipinfo_is_symlink(info):
        return ((info.external_attr >> 16) & 0o170000) == 0o120000

    def _plan_import_members(self, zf, filename):
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > self._IMPORT_MAX_FILES:
            raise ValueError(f"Too many files in zip ({len(infos)}). Maximum is {self._IMPORT_MAX_FILES}.")
        total_unpacked = sum(max(0, info.file_size) for info in infos)
        if total_unpacked > self._IMPORT_MAX_UNPACKED:
            mb = total_unpacked // 1024 // 1024
            raise ValueError(f"Zip unpacks to {mb} MB. Maximum is {self._IMPORT_MAX_UNPACKED // 1024 // 1024} MB.")

        raw_paths = []
        info_by_path = {}
        for info in infos:
            if self._zipinfo_is_symlink(info):
                raise ValueError(f"Symlinks are not allowed in update zips: {info.filename}")
            rel = self._clean_zip_path(info.filename)
            if not rel:
                continue
            if rel.startswith("__MACOSX/") or rel.endswith(".DS_Store"):
                continue
            if rel in info_by_path:
                raise ValueError(f"Duplicate path in update zip: {rel}")
            raw_paths.append(rel)
            info_by_path[rel] = info
        if not raw_paths:
            raise ValueError("Zip does not contain any installable files.")

        root_paths = self._strip_wrapper_folder(raw_paths)
        root_mode = any(self._allowed_root_import_path(p) for p in root_paths)
        planned = []

        if root_mode:
            # Re-map through stripped wrapper paths if needed.
            by_stripped = {}
            if root_paths is not raw_paths:
                top = self._single_top(raw_paths)
                for raw in raw_paths:
                    if raw.startswith(top + "/"):
                        by_stripped[raw.split("/", 1)[1]] = raw
            for rel in root_paths:
                raw = by_stripped.get(rel, rel)
                if rel in self._IMPORT_PACKAGE_METADATA:
                    continue
                if self._is_blocked_import_path(rel):
                    continue
                if not self._allowed_root_import_path(rel):
                    raise ValueError(f"Path is not allowed in a CyberHub update zip: {rel}")
                planned.append((rel, info_by_path[raw]))
        else:
            # Convenience module zip: my_module/__init__.py, or a flat __init__.py zip.
            top = self._single_top(raw_paths)
            if top and f"{top}/__init__.py" in raw_paths:
                module_name = top
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", module_name):
                    module_name = self._module_name_from_zip(module_name)
                for raw in raw_paths:
                    if self._is_blocked_import_path(raw):
                        continue
                    planned.append((f"modules/{module_name}/{raw.split('/', 1)[1]}", info_by_path[raw]))
            elif "__init__.py" in raw_paths:
                module_name = self._module_name_from_zip(filename)
                for raw in raw_paths:
                    if self._is_blocked_import_path(raw):
                        continue
                    planned.append((f"modules/{module_name}/{raw}", info_by_path[raw]))
            else:
                raise ValueError("Zip is not recognized as a CyberHub update or module package.")

        if not planned:
            raise ValueError("Zip only contains ignored or blocked files.")
        return planned

    @staticmethod
    def _package_manifest_from_zip(zf):
        locations = []
        for name in ("resources/cyberhub-package.json", "cyberhub-package.json"):
            try:
                locations.append(zf.getinfo(name))
            except KeyError:
                pass
        if not locations:
            return None
        if len(locations) > 1:
            raise ValueError("CyberHub package contains duplicate manifests.")
        info = locations[0]
        if info.file_size < 2 or info.file_size > 64 * 1024:
            raise ValueError("CyberHub package manifest has an invalid size.")
        try:
            value = json.loads(zf.read(info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise ValueError("CyberHub package manifest is invalid.") from exc
        if not isinstance(value, dict) or value.get("schema") not in {1, 2} or value.get("product") != "CyberHub":
            raise ValueError("Unsupported CyberHub package manifest.")
        package_type = str(value.get("type") or "")
        package_id = str(value.get("id") or "")
        version = str(value.get("version") or "").strip().lstrip("v")
        minimum = str(value.get("minimum_hub_version") or "").strip().lstrip("v")
        if package_type not in {"hub", "module"}:
            raise ValueError("CyberHub package has an invalid type.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", package_id):
            raise ValueError("CyberHub package has an invalid identifier.")
        if package_type == "hub" and package_id != "cyberhub":
            raise ValueError("CyberHub package has an invalid Hub identifier.")
        if not version or len(version) > 40:
            raise ValueError("CyberHub package has an invalid version.")
        return {
            "type": package_type,
            "id": package_id,
            "name": str(value.get("name") or package_id)[:120],
            "version": version,
            "minimum_hub_version": minimum,
            "repository": str(value.get("repository") or "")[:160],
            "publisher_type": str(value.get("publisher_type") or "official")[:40],
            "channel": "beta" if value.get("channel") == "beta" else "stable",
        }

    def _validate_online_package_manifest(self, manifest, expected):
        if not manifest:
            raise ValueError("Online update is missing cyberhub-package.json.")
        for key in ("type", "id", "version", "minimum_hub_version"):
            expected_value = str(expected.get(key) or "").strip().lstrip("v")
            actual_value = str(manifest.get(key) or "").strip().lstrip("v")
            if actual_value != expected_value:
                raise ValueError(f"Downloaded package manifest does not match its catalog {key}.")
        minimum = manifest.get("minimum_hub_version") or ""
        current = str(getattr(self.hub, "VERSION", "1.0"))
        if minimum and _is_newer_version(minimum, current):
            raise ValueError(f"Package requires CyberHub {minimum} or newer.")

    @staticmethod
    def _validate_online_package_scope(manifest, planned):
        """Keep an official module package inside its own ownership boundary."""
        if manifest.get("type") != "module":
            return
        module_id = manifest["id"]
        module_prefix = f"modules/{module_id}/"
        community_resource_prefix = f"resources/modules/{module_id}/"
        paths = [rel for rel, _info in planned]
        if f"{module_prefix}__init__.py" not in paths:
            raise ValueError(f"Module package is missing {module_prefix}__init__.py.")
        for rel in paths:
            if manifest.get("publisher_type") == "community":
                if rel.startswith(module_prefix) or rel.startswith(community_resource_prefix):
                    continue
                raise ValueError(
                    f"Community module {module_id} is not allowed to replace {rel}."
                )
            if rel.startswith(module_prefix) or rel.startswith(("resources/", "licenses/")):
                continue
            raise ValueError(
                f"Module package {module_id} is not allowed to replace {rel}."
            )

    def _install_import_zip(self, source, filename, *, require_manifest=False, expected_package=None):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_root = os.path.join(root, "updates", "backups", f"import-{stamp}")
        # Stage inside the CyberHub install so Windows can atomically replace files
        # when the system temp folder is on another drive (e.g. C: temp -> D: hub).
        tmp_parent = os.path.join(root, "updates", "tmp")
        os.makedirs(tmp_parent, exist_ok=True)
        tmp_root = tempfile.mkdtemp(prefix="cyberhub-import-", dir=tmp_parent)
        installed = []
        updated = 0
        created = 0
        zip_source = BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
        try:
            with zipfile.ZipFile(zip_source) as zf:
                manifest = self._package_manifest_from_zip(zf)
                if require_manifest:
                    self._validate_online_package_manifest(manifest, expected_package or {})
                planned = self._plan_import_members(zf, filename)
                if require_manifest:
                    self._validate_online_package_scope(manifest, planned)
                for rel, info in planned:
                    stage_path = os.path.join(tmp_root, *rel.split("/"))
                    os.makedirs(os.path.dirname(stage_path), exist_ok=True)
                    with zf.open(info) as src, open(stage_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if rel == "start.sh":
                        os.chmod(stage_path, 0o755)

            # Compile staged Python before touching the live install.
            import py_compile
            for rel, _info in planned:
                if rel.endswith(".py"):
                    py_compile.compile(os.path.join(tmp_root, *rel.split("/")), doraise=True)

            # Prepare every backup before the first live file is replaced.
            for rel, _info in planned:
                dst = os.path.join(root, *rel.split("/"))
                if not os.path.abspath(dst).startswith(os.path.abspath(root) + os.sep):
                    raise ValueError(f"Refusing to install outside CyberHub: {rel}")
                if os.path.exists(dst):
                    backup = os.path.join(backup_root, *rel.split("/"))
                    os.makedirs(os.path.dirname(backup), exist_ok=True)
                    shutil.copy2(dst, backup)
                    updated += 1
                else:
                    created += 1

            try:
                for rel, _info in planned:
                    src = os.path.join(tmp_root, *rel.split("/"))
                    dst = os.path.join(root, *rel.split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.replace(src, dst)
                    installed.append(rel)
            except Exception:
                # Restore every file already touched during this transaction.
                for rel in reversed(installed):
                    dst = os.path.join(root, *rel.split("/"))
                    backup = os.path.join(backup_root, *rel.split("/"))
                    try:
                        if os.path.isfile(backup):
                            rollback = dst + ".cyberhub-rollback"
                            shutil.copy2(backup, rollback)
                            os.replace(rollback, dst)
                        elif os.path.exists(dst):
                            os.remove(dst)
                    except OSError:
                        pass
                raise
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        if manifest and manifest.get("type") == "module" and hasattr(self.hub, "module_store"):
            self.hub.module_store.record_install(
                manifest,
                installed,
                repository=manifest.get("repository", ""),
                publisher_type=manifest.get("publisher_type", "official"),
            )

        return {
            "ok": True,
            "filename": filename,
            "installed": len(installed),
            "updated": updated,
            "created": created,
            "backup": backup_root if updated else "",
            "restart_required": True,
            "files": installed[:80],
            "truncated": len(installed) > 80,
        }

    def _api_import_package(self, handler, content_len, content_type):
        if not handler._is_loopback():
            handler.respond_json({"error": "Update import is only available from localhost."}, status=403)
            return
        if "multipart/form-data" not in (content_type or ""):
            handler.respond_json({"error": "Expected a ZIP file upload."}, status=400)
            return
        try:
            try:
                files = handler.parse_multipart(
                    content_len, content_type, max_upload=self._IMPORT_MAX_UPLOAD
                )
            except TypeError:
                # Compatibility with CyberHub 1.2 builds whose core/server.py
                # still has the older parse_multipart(content_len, content_type)
                # signature. The old helper has a 100 MB upload cap, which is
                # enough for normal module hotfix zips like this one.
                files = handler.parse_multipart(content_len, content_type)
            upload = files.get("package") or files.get("file")
            if not upload or not upload.get("data"):
                handler.respond_json({"error": "Missing ZIP file."}, status=400)
                return
            filename = upload.get("filename") or "update.zip"
            if not filename.lower().endswith(".zip"):
                handler.respond_json({"error": "Only .zip files can be imported."}, status=400)
                return
            result = self._install_import_zip(upload["data"], filename)
            handler.respond_json(result)
        except zipfile.BadZipFile:
            handler.respond_json({"error": "Invalid ZIP file."}, status=400)
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=400)

    # ─── Civitai API ──────────────────────────────────────────────────────
    def _api_civitai_info(self, handler, qs):
        handler.respond_json(self.hub.civitai.get_info())

    def _api_civitai_status(self, handler, qs):
        handler.respond_json(self.hub.civitai._update_status)

    def _api_civitai_update(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        api_key = (data.get("api_key") or "").strip()
        # Save API key if provided
        if api_key:
            self.hub.settings.set_path("civitai.api_key", api_key)
        else:
            # Try from settings
            api_key = (self.hub.settings.get_path("civitai.api_key", "") or "").strip()
        result = self.hub.civitai.start_update(api_key=api_key)
        handler.respond_json(result)

    def _api_restart(self, handler, content_len, content_type):
        """Restart the hub process."""
        handler.respond_json({"ok": True, "message": "Restarting..."})
        import subprocess
        def do_restart():
            import time; time.sleep(0.5)
            print("\n[HUB] Restarting...")
            if os.name == "nt":
                # Windows: os.execv closes the console window on double-click.
                # Use subprocess with CREATE_NEW_CONSOLE instead.
                subprocess.Popen(
                    [sys.executable] + sys.argv,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
                os._exit(42)
            else:
                os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=do_restart, daemon=True).start()

    # ─── Resource status ──────────────────────────────────────────────────
    def _resource_status(self):
        """Report which files the hub auto-loads from resources/, and whether a
        Settings override is overriding the bundled default. Lets the user see
        at a glance what was actually picked up (e.g. models.json, fonts, ONNX).

        Each entry: {label, present, source ('override'|'resources'|'missing'),
        path, size}. Never raises — a broken probe just yields present=False.
        """
        def hsize(n):
            n = float(n)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024:
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} PB"

        def entry(label, path, is_override):
            present = bool(path) and os.path.isfile(path)
            if not present:
                source = "missing"
            elif is_override:
                source = "override"
            else:
                source = "resources"
            return {
                "label": label,
                "present": present,
                "source": source,
                "path": path or "",
                "size": hsize(os.path.getsize(path)) if present else "",
            }

        items = []

        # Civitai models.json (override: civitai.models_path / CLI --models)
        try:
            civ = self.hub.civitai
            is_ov = (civ.source_mode or "") in ("override", "cli")
            items.append(entry("Civitai models.json", civ.source_path or "", is_ov))
        except Exception:
            pass

        # Danbooru tag/auto-tag files (override per file in Settings → Danbooru)
        try:
            dan = self.hub.registry.get("danbooru")
            if dan is not None:
                for key, label in (("csv_file", "Danbooru tags.csv"),
                                   ("model_file", "Danbooru Auto-Tag model"),
                                   ("tags_file", "Danbooru Auto-Tag labels")):
                    path, source = dan._resource(key)
                    items.append(entry(label, path, source == "override"))
        except Exception:
            pass

        # Bundled UI assets (no override) — fetched by download_fonts.py
        try:
            import glob as _glob
            fonts = _glob.glob(self.hub.resource_path("fonts", "*.woff2"))
            items.append({
                "label": "UI fonts (woff2)",
                "present": bool(fonts),
                "source": "resources" if fonts else "missing",
                "path": self.hub.resource_path("fonts"),
                "size": f"{len(fonts)} files" if fonts else "",
            })
        except Exception:
            pass

        # ONNX Runtime (browser) for Danbooru Auto-Tag
        try:
            ort = self.hub.resource_path("danbooru", "ort.min.js")
            wasm = self.hub.resource_path("danbooru", "ort-wasm-simd-threaded.jsep.wasm")
            ready = os.path.isfile(ort) and os.path.isfile(wasm)
            items.append({
                "label": "ONNX Runtime (browser)",
                "present": ready,
                "source": "resources" if ready else "missing",
                "path": ort,
                "size": hsize(os.path.getsize(wasm)) if os.path.isfile(wasm) else "",
            })
        except Exception:
            pass

        return items

    # ─── System Info ──────────────────────────────────────────────────────
    def _api_system_info(self, handler, qs):
        """Return diagnostic info for the Settings status panel.

        Designed to be copy-pasted into bug reports — covers the host system
        (CPU/GPU/RAM/storage), the hub itself (version, Python, bind, modules),
        and the data the hub manages (image count, library size).
        """
        import platform
        import shutil
        import subprocess

        info = {
            "status": "running",
            "version": getattr(self.hub, "VERSION", "dev"),
            "python": platform.python_version(),
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "hostname": platform.node(),
            "architecture": platform.machine(),
        }

        # CPU
        cpu = platform.processor()
        # platform.processor() often returns just "x86_64" on Linux — try better sources
        if not cpu or len(cpu) < 10:
            try:
                if platform.system() == "Darwin":
                    cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"],
                                                   text=True, timeout=3).strip()
                elif platform.system() == "Linux":
                    with open("/proc/cpuinfo") as f:
                        for line in f:
                            if line.startswith("model name"):
                                cpu = line.split(":", 1)[1].strip()
                                break
                elif platform.system() == "Windows":
                    cpu = platform.processor()
            except Exception:
                cpu = f"{os.cpu_count() or '?'} cores"
        info["cpu"] = cpu or f"{os.cpu_count() or '?'} cores"
        info["cpu_cores"] = os.cpu_count() or 0

        # RAM — total, available, used (best-effort per platform)
        ram_total = ram_avail = 0
        try:
            if platform.system() == "Windows":
                out = subprocess.check_output(
                    ["wmic", "computersystem", "get", "totalphysicalmemory"],
                    text=True, timeout=3)
                for line in out.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        ram_total = int(line)
                try:
                    out2 = subprocess.check_output(
                        ["wmic", "OS", "get", "FreePhysicalMemory"],
                        text=True, timeout=3)
                    for line in out2.strip().split("\n"):
                        line = line.strip()
                        if line.isdigit():
                            ram_avail = int(line) * 1024  # WMIC reports KB
                except Exception: pass
            elif platform.system() == "Darwin":
                out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=3)
                ram_total = int(out.strip())
                try:
                    vm = subprocess.check_output(["vm_stat"], text=True, timeout=3)
                    page_size = 4096
                    free_pages = inactive_pages = 0
                    for line in vm.split("\n"):
                        if "page size of" in line:
                            for tok in line.split():
                                if tok.isdigit(): page_size = int(tok); break
                        if line.startswith("Pages free:"):
                            free_pages = int(line.split(":")[1].strip().rstrip("."))
                        elif line.startswith("Pages inactive:"):
                            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
                    ram_avail = (free_pages + inactive_pages) * page_size
                except Exception: pass
            elif platform.system() == "Linux":
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            ram_total = int(line.split()[1]) * 1024
                        elif line.startswith("MemAvailable:"):
                            ram_avail = int(line.split()[1]) * 1024
        except Exception:
            pass
        info["ram"] = f"{ram_total / (1024**3):.1f} GB" if ram_total else "?"
        if ram_total and ram_avail:
            ram_used = ram_total - ram_avail
            info["ram_total_bytes"] = ram_total
            info["ram_used_bytes"] = ram_used
            info["ram_used"] = f"{ram_used / (1024**3):.1f} GB"
            info["ram_free"] = f"{ram_avail / (1024**3):.1f} GB"
            info["ram_pct"] = round(100 * ram_used / ram_total, 1)

        # Storage (drive where hub.py lives) — total/used/free
        try:
            hub_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            usage = shutil.disk_usage(hub_dir)
            info["storage_total"] = f"{usage.total / (1024**3):.1f} GB"
            info["storage_free"] = f"{usage.free / (1024**3):.1f} GB"
            info["storage_used"] = f"{(usage.total - usage.free) / (1024**3):.1f} GB"
            info["storage_pct"] = round(100 * (usage.total - usage.free) / usage.total, 1)
            info["storage_path"] = hub_dir
        except Exception:
            info["storage_total"] = "?"
            info["storage_free"] = "?"

        # GPU
        gpu = ""
        try:
            if platform.system() == "Windows":
                out = subprocess.check_output(
                    ["wmic", "path", "win32_VideoController", "get", "name"],
                    text=True, timeout=3)
                lines = [l.strip() for l in out.strip().split("\n") if l.strip() and l.strip() != "Name"]
                gpu = lines[0] if lines else ""
            elif platform.system() == "Linux":
                out = subprocess.check_output(["lspci"], text=True, timeout=3)
                for line in out.split("\n"):
                    if "VGA" in line or "3D controller" in line:
                        gpu = line.split(":", 2)[-1].strip()
                        break
            elif platform.system() == "Darwin":
                out = subprocess.check_output(
                    ["system_profiler", "SPDisplaysDataType"],
                    text=True, timeout=5)
                for line in out.split("\n"):
                    line = line.strip()
                    if line.startswith("Chipset Model:"):
                        gpu = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        info["gpu"] = gpu or "Not detected"

        # Hub — bind, port, uptime, modules
        info["bind"] = getattr(self.hub, "bind", "?")
        info["port"] = getattr(self.hub, "port", "?")
        started_at = getattr(self.hub, "started_at", None)
        if started_at:
            uptime_s = int(time.time() - started_at)
            d, rem = divmod(uptime_s, 86400)
            h, rem = divmod(rem, 3600)
            m, _ = divmod(rem, 60)
            parts = []
            if d: parts.append(f"{d}d")
            if h: parts.append(f"{h}h")
            if m or not parts: parts.append(f"{m}m")
            info["uptime"] = " ".join(parts)
        modules_list = []
        for key, mod in (getattr(self.hub.registry, "modules", None) or {}).items():
            modules_list.append({
                "key": key,
                "name": getattr(mod, "name", key),
                "version": getattr(mod, "version", "1.0"),
            })
        info["modules"] = modules_list
        info["module_count"] = len(modules_list)

        # Data — image count from gallery, library card count + attachment dir size
        try:
            gallery_mod = (self.hub.registry.modules or {}).get("gallery")
            if gallery_mod and getattr(gallery_mod, "db", None):
                stats = gallery_mod.db.get_stats()
                info["image_count"] = stats.get("files", 0)
                info["folder_count"] = stats.get("folders", 0)
                info["images_with_meta"] = stats.get("with_metadata", 0)
                info["favorites_count"] = stats.get("favorites", 0)
                # DB file size
                try:
                    db_path = self.hub.data_path("cyberdelia.db")
                    if os.path.isfile(db_path):
                        info["db_size"] = f"{os.path.getsize(db_path) / (1024**2):.1f} MB"
                except Exception: pass
                # Thumb directory size
                try:
                    thumb_dir = getattr(gallery_mod, "thumb_dir", None)
                    if thumb_dir and os.path.isdir(thumb_dir):
                        total = 0
                        for root, _, files in os.walk(thumb_dir):
                            for f in files:
                                try: total += os.path.getsize(os.path.join(root, f))
                                except OSError: pass
                        info["thumbs_size"] = f"{total / (1024**2):.1f} MB"
                except Exception: pass
        except Exception:
            pass
        try:
            lib_mod = (self.hub.registry.modules or {}).get("library")
            if lib_mod and getattr(lib_mod, "db", None):
                cards = lib_mod.db.list_cards(limit=99999)
                info["library_cards"] = cards.get("total", 0)
                # Attachments size
                att_dir = getattr(lib_mod, "attachments_dir", None)
                if att_dir and os.path.isdir(att_dir):
                    total = 0; count = 0
                    for root, _, files in os.walk(att_dir):
                        for f in files:
                            try: total += os.path.getsize(os.path.join(root, f)); count += 1
                            except OSError: pass
                    info["attachments_count"] = count
                    info["attachments_size"] = f"{total / (1024**2):.1f} MB"
        except Exception:
            pass

        # Optional dependencies — useful when things fail on someone else's machine
        deps = {}
        # Note: the Danbooru auto-tag runs onnxruntime-web (ort.min.js) in the
        # browser, so there is no Python `onnxruntime` dependency to check here.
        for name, pkg in [("Pillow", "PIL"), ("send2trash", "send2trash"),
                          ("numpy", "numpy"),
                          ("opencv", "cv2"), ("requests", "requests")]:
            try:
                __import__(pkg)
                deps[name] = "ok"
            except ImportError:
                deps[name] = "missing"
        info["dependencies"] = deps

        # What the hub auto-loaded from resources/ (and any active overrides)
        info["resources"] = self._resource_status()

        handler.respond_json(info)


SETTINGS_BODY = r"""
<style>
.settings-page { padding:24px; max-width:1180px; margin:0 auto; }
/* Two-column layout: global settings on the left, Modules on the right.
   Collapses to a single column on narrow screens. align-items:start so the
   shorter column doesn't stretch to match the taller one. */
.settings-cols { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:0 16px; align-items:start; }
.settings-col { min-width:0; }
@media (max-width: 900px) { .settings-cols { grid-template-columns:1fr; } }
.settings-section { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:16px; }
.settings-section h2 { font-size:14px; font-weight:600; color:var(--text-bright); margin-bottom:14px; display:flex; align-items:center; gap:8px; }
.settings-section h2 .section-icon { width:16px; height:16px; display:inline-flex; align-items:center; justify-content:center; color:var(--text-dim); flex-shrink:0; }
.settings-section h2 .section-icon svg { width:15px; height:15px; display:block; }
.version-badge { display:inline-flex; align-items:center; height:18px; padding:0 7px; border-radius:999px; background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim); font-size:10px; font-family:var(--mono); font-weight:600; line-height:1; }
.stage-badge { display:inline-flex; align-items:center; height:18px; padding:0 7px; border-radius:999px; font-size:10px; font-weight:700; line-height:1; text-transform:uppercase; letter-spacing:.35px; }
.stage-badge.beta { color:#f7c66b; border:1px solid rgba(247,198,107,.45); background:rgba(247,198,107,.1); }
.settings-section h3 { font-size:12px; font-weight:600; color:var(--text); margin:14px 0 8px; text-transform:uppercase; letter-spacing:.5px; }
.settings-row { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); gap:16px; }
.settings-row:last-child { border-bottom:none; }
.settings-row.col { flex-direction:column; align-items:stretch; }
.settings-label { font-size:12px; color:var(--text); }
.settings-label .desc { font-size:11px; color:var(--text-dim); margin-top:2px; font-weight:400; }
.settings-input { background:var(--bg-card); border:1px solid var(--border); color:var(--text); font:inherit; font-size:12px; padding:6px 10px; border-radius:var(--radius); outline:none; min-width:200px; font-family:var(--mono); }
.settings-input:focus { border-color:var(--accent); }
.settings-input.wide { width:100%; min-width:0; }
.settings-input.narrow { min-width:0; width:90px; text-align:right; }
.settings-textarea { display:block; padding:9px 11px; line-height:1.55; font-size:11px; resize:vertical; min-height:120px; }

.settings-toggle { position:relative; width:40px; height:22px; cursor:pointer; flex-shrink:0; }
.settings-toggle input { display:none; }
.settings-toggle .slider { position:absolute; inset:0; background:var(--bg-card); border:1px solid var(--border); border-radius:11px; transition:all .2s; }
.settings-toggle .slider:before { content:''; position:absolute; width:16px; height:16px; border-radius:50%; background:var(--text-dim); left:2px; top:2px; transition:all .2s; }
.settings-toggle input:checked + .slider { background:var(--accent-glow); border-color:var(--accent-dim); }
.settings-toggle input:checked + .slider:before { background:var(--accent); transform:translateX(18px); }

.module-card { display:flex; align-items:center; gap:12px; padding:12px 0; border-bottom:1px solid var(--border); }
.module-card:last-child { border-bottom:none; }
.module-card .mc-head { display:flex; align-items:center; gap:12px; flex:1; min-width:0; }
.module-actions { display:grid; grid-template-columns:minmax(92px,max-content) 84px; align-items:center; justify-content:end; gap:8px; flex-shrink:0; }
.module-toggle-slot, .module-settings-slot { display:flex; align-items:center; justify-content:flex-end; min-width:0; }
.module-settings-slot { width:84px; }
.module-card .chevron { display:inline-flex; align-items:center; justify-content:center; gap:5px; width:84px; height:28px; padding:0 8px; border:1px solid var(--border-light); border-radius:var(--radius); background:var(--bg-card); color:var(--text-dim); font:inherit; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.4px; transition:all .15s; cursor:pointer; flex-shrink:0; }
.module-card .chevron .module-expand-icon { display:inline-block; font-size:10px; line-height:1; transition:transform .15s; }
.module-card .chevron.open { color:var(--accent); border-color:var(--accent-dim); background:var(--accent-glow); }
.module-card .chevron.open .module-expand-icon { transform:rotate(90deg); }
.module-card .chevron:hover { color:var(--accent); border-color:var(--accent-dim); background:var(--bg-hover); }
.module-icon { width:32px; height:32px; display:flex; align-items:center; justify-content:center; color:var(--text-dim); flex-shrink:0; }
.module-icon svg { width:18px; height:18px; display:block; }
.module-info { flex:1; min-width:0; }
.module-info .name { font-size:13px; font-weight:600; color:var(--text-bright); display:flex; align-items:center; gap:8px; min-width:0; }
.module-info .name-text { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.module-info .desc { font-size:11px; color:var(--text-dim); }

.module-settings { overflow:hidden; max-height:0; opacity:0; transition:max-height .3s ease, opacity .2s ease, padding .2s ease, margin .2s ease; margin-top:0; padding:0 12px; background:var(--bg-card); border:1px solid transparent; border-radius:var(--radius); }
.module-settings.open { max-height:2000px; opacity:1; padding:12px; margin-top:8px; border-color:var(--border); }
.module-settings .settings-row { padding:6px 0; }

@media (max-width: 520px) {
    .module-card { align-items:flex-start; flex-wrap:wrap; }
    .module-card .mc-head { flex-basis:100%; }
    .module-actions { width:calc(100% - 44px); margin-left:44px; grid-template-columns:1fr 84px; }
    .module-toggle-slot { justify-content:flex-start; }
}

.restart-banner { display:none; align-items:center; justify-content:space-between; gap:14px; background:var(--bg-card); border:1px solid var(--orange); border-radius:8px; padding:10px 12px; font-size:12px; color:var(--orange); margin-bottom:16px; scroll-margin-top:64px; }
.restart-banner.visible { display:flex; }
.restart-banner .restart-copy { min-width:0; }
.restart-banner .restart-title { font-weight:700; color:var(--orange); margin-bottom:2px; }
.restart-banner .restart-desc { color:var(--text-dim); font-size:11px; }
.restart-banner .action-btn { flex-shrink:0; }
@media (max-width: 640px) {
    .restart-banner { align-items:stretch; flex-direction:column; }
    .restart-banner .action-btn { width:100%; }
}

.action-btn { background:var(--accent); color:#fff; border:none; padding:6px 14px; border-radius:var(--radius); font:inherit; font-size:11px; font-weight:400; cursor:pointer; transition:all .15s; white-space:nowrap; }
.action-btn:hover:not(:disabled) { background:var(--accent-dim); }
.action-btn:disabled { opacity:.5; cursor:default; }
.action-btn.secondary { background:var(--bg-card); color:var(--text); border:1px solid var(--border); font-weight:400; }
.action-btn.secondary:hover:not(:disabled) { background:var(--bg-hover); }
.action-btn.danger { background:rgba(239,68,68,.1); color:var(--red); border:1px solid rgba(239,68,68,.45); }
.action-btn.danger:hover:not(:disabled) { background:rgba(239,68,68,.2); }

.status-line { font-size:11px; color:var(--text-dim); margin-top:8px; font-family:var(--mono); }
.maint-status { display:flex; flex-direction:column; gap:6px; }
.maint-status-head { display:flex; justify-content:space-between; gap:12px; color:var(--text); }
.maint-status-head .elapsed { color:var(--text-dim); white-space:nowrap; }
.maint-progress { height:5px; overflow:hidden; border-radius:3px; background:var(--border); }
.maint-progress-fill { width:0; height:100%; background:var(--accent); transition:width .25s ease; }
.maint-progress-fill.indeterminate { width:30%; animation:maint-slide 1.1s ease-in-out infinite; }
.maint-detail { color:var(--text-dim); }
.maint-error { color:var(--red); }
@keyframes maint-slide { from { transform:translateX(-110%); } to { transform:translateX(340%); } }
.import-file { color:var(--text-dim); font-size:11px; min-width:0; }
.import-file::file-selector-button { margin-right:10px; background:var(--bg-card); color:var(--text); border:1px solid var(--border); border-radius:var(--radius); padding:6px 10px; font:inherit; font-size:11px; cursor:pointer; }
.import-file::file-selector-button:hover { background:var(--bg-hover); }
.import-summary { color:var(--text); }
.import-summary.ok { color:var(--green); }
.import-summary.err { color:var(--red); }
.update-release { margin-top:10px; border-top:1px solid var(--border); padding-top:10px; }
.update-release-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:8px; }
.update-release-title { color:var(--text-bright); font-weight:600; font-size:13px; }
.update-release-meta { color:var(--text-dim); font-family:var(--mono); font-size:10px; margin-top:3px; }
.update-package-list { display:flex; flex-direction:column; gap:6px; }
.update-package { display:flex; align-items:center; gap:12px; padding:9px 10px; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); }
.update-package-info { min-width:0; flex:1; }
.update-package-name { color:var(--text); font-size:12px; font-weight:600; }
.update-package-version { color:var(--text-dim); font-family:var(--mono); font-size:10px; margin-top:2px; }
.update-package-note { color:var(--orange, #f59e0b); font-size:10px; margin-top:3px; }
.update-empty { padding:10px; border:1px dashed var(--border); border-radius:var(--radius); color:var(--green); font-size:11px; }
.update-notes { margin-top:9px; color:var(--text-dim); font-size:11px; }
.update-notes summary { cursor:pointer; color:var(--text); }
.update-notes pre { margin:8px 0 0; padding:9px; max-height:180px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); color:var(--text-dim); font:11px/1.5 var(--mono); }
.update-job { display:none; margin-top:10px; }
.update-job.visible { display:block; }
.update-job-line { display:flex; justify-content:space-between; gap:10px; color:var(--text); font-size:11px; margin-bottom:6px; }
.update-job-line .bytes { color:var(--text-dim); font-family:var(--mono); white-space:nowrap; }
.update-progress { height:6px; overflow:hidden; border-radius:3px; background:var(--border); }
.update-progress-fill { width:0; height:100%; background:var(--accent); transition:width .2s ease; }
.update-progress-fill.indeterminate { width:30%; animation:maint-slide 1.1s ease-in-out infinite; }

.help-link { color:var(--accent); text-decoration:none; font-size:11px; }
.help-link:hover { text-decoration:underline; }

/* Folder list */
.folder-list { display:flex; flex-direction:column; gap:4px; margin-top:8px; }
.folder-empty { padding:10px 12px; background:var(--bg-card); border:1px dashed var(--border-light); border-radius:var(--radius); font-size:11px; color:var(--text-dim); font-style:italic; text-align:center; }
.folder-item { display:flex; align-items:center; gap:8px; padding:6px 10px; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); font-family:var(--mono); font-size:11px; color:var(--text); }
.folder-item .path { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.folder-item .remove-btn { color:var(--text-dim); cursor:pointer; font-size:14px; transition:color .15s; background:none; border:none; padding:0 4px; }
.folder-item .remove-btn:hover { color:var(--red); }
.folder-add-row { display:flex; gap:4px; margin-top:8px; }

/* Browse input with button */
.browse-wrap { display:flex; gap:4px; width:100%; }
.browse-wrap input { flex:1; }

/* Browse dialog */
.browse-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:5000; display:none; align-items:center; justify-content:center; }
.browse-overlay.open { display:flex; }
.browse-dialog { background:var(--bg-panel); border:1px solid var(--border); border-radius:10px; width:560px; max-height:70vh; display:flex; flex-direction:column; box-shadow:0 12px 48px rgba(0,0,0,.7); }
.browse-header { padding:14px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
.browse-header h3 { font-size:14px; font-weight:600; color:var(--text-bright); margin:0; }
.browse-close { background:none; border:none; color:var(--text-dim); font-size:18px; cursor:pointer; padding:4px 8px; border-radius:4px; }
.browse-close:hover { background:var(--bg-hover); color:var(--text); }
.browse-crumb { padding:8px 16px; font-family:var(--mono); font-size:11px; color:var(--text-dim); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:4px; flex-wrap:wrap; overflow:hidden; }
.browse-crumb span { cursor:pointer; color:var(--accent); transition:opacity .15s; }
.browse-crumb span:hover { opacity:.7; }
.browse-crumb .sep { color:var(--text-dim); cursor:default; }
.browse-body { flex:1; overflow-y:auto; padding:4px 0; }
.browse-entry { display:flex; align-items:center; gap:10px; padding:8px 16px; cursor:pointer; font-size:12px; color:var(--text); transition:background .1s; }
.browse-entry:hover { background:var(--bg-hover); }
.browse-entry.selected { background:var(--bg-active); color:var(--accent); }
.browse-entry .icon { width:18px; height:18px; display:flex; align-items:center; justify-content:center; flex-shrink:0; color:var(--text-dim); }
.browse-entry .icon svg { width:16px; height:16px; display:block; }
.browse-entry .name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.browse-entry.file .icon { opacity:.5; }
.browse-empty { padding:24px 16px; text-align:center; color:var(--text-dim); font-size:12px; }
.browse-footer { padding:12px 16px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; gap:8px; }
.browse-path-display { font-family:var(--mono); font-size:10px; color:var(--text-dim); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.browse-select-btn:disabled { opacity:.4; cursor:not-allowed; }

/* System info bar */
/* Diagnostic panel — collapsible. Data is only fetched on first expand so the
   Settings page loads instantly even when the thumb-folder walk would take a moment. */
.diag-panel { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; margin-bottom:16px; overflow:hidden; }
.diag-head { display:flex; align-items:center; justify-content:space-between; padding:10px 14px; cursor:pointer; user-select:none; transition:background .15s; }
.diag-head:hover { background:var(--bg-hover); }
.diag-panel.open .diag-head { border-bottom:1px solid var(--border); }
.diag-title { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.8px; color:var(--text-dim); display:flex; align-items:center; gap:6px; }
.diag-title .section-icon { width:15px; height:15px; display:inline-flex; align-items:center; justify-content:center; color:var(--text-dim); }
.diag-title .section-icon svg { width:14px; height:14px; display:block; }
.diag-chev { display:inline-block; color:var(--text-dim); transition:transform .15s; font-size:12px; }
.diag-panel.open .diag-chev { transform:rotate(90deg); }
.diag-hint { font-weight:400; font-size:10px; color:var(--text-dim); opacity:.7; letter-spacing:0; text-transform:none; margin-left:4px; }
.diag-panel.open .diag-hint { display:none; }
.diag-actions { display:flex; gap:6px; }
.diag-copy-btn { background:var(--bg-card); border:1px solid var(--border); color:var(--text); padding:5px 10px; font-size:11px; border-radius:5px; cursor:pointer; font-family:inherit; }
.diag-copy-btn:hover { background:var(--bg-hover); border-color:var(--accent-dim); }
.diag-copy-btn.copied { background:var(--green-bg, rgba(34,197,94,.15)); border-color:var(--green); color:var(--green); }
.diag-body { /* visibility toggled inline so we can keep the structure but skip layout cost when closed */ }
.diag-loading { padding:24px; text-align:center; font-size:12px; color:var(--text-dim); font-family:var(--mono); }
.diag-sections { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:1px; background:var(--border); }
.diag-section { background:var(--bg-panel); padding:12px 14px; }
.diag-section-title { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.8px; color:var(--accent); margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid var(--border); }
.diag-row { display:flex; justify-content:space-between; gap:12px; padding:3px 0; font-size:12px; line-height:1.5; }
.diag-row .k { color:var(--text-dim); flex-shrink:0; }
.diag-row .v { color:var(--text); text-align:right; word-break:break-all; min-width:0; font-family:var(--mono); font-size:11px; }
.diag-row .v.ok    { color:var(--green); }
.diag-row .v.warn  { color:var(--orange, #f59e0b); }
.diag-row .v.miss  { color:var(--text-dim); font-style:italic; }
.diag-bar { display:inline-block; width:60px; height:4px; background:var(--border); border-radius:2px; vertical-align:middle; margin-left:6px; overflow:hidden; }
.diag-bar .fill { display:block; height:100%; background:var(--accent); transition:width .3s; }
.diag-bar .fill.high { background:var(--orange, #f59e0b); }
.diag-bar .fill.crit { background:var(--red); }
@media (max-width: 980px) {
    .diag-sections { grid-template-columns:repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
    .diag-sections { grid-template-columns:1fr; }
}
</style>

<div class="settings-page">

  <div class="restart-banner" id="restartBanner">
    <div class="restart-copy">
      <div class="restart-title">Restart required</div>
      <div class="restart-desc">Restart the hub for these changes to take effect. The browser will reconnect automatically.</div>
    </div>
    <button class="action-btn" onclick="restartHub()">Restart now</button>
  </div>

  <div class="settings-cols">
   <div class="settings-col">

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"/></svg></span>Hub <span class="version-badge" id="hubVersion">v1.0</span></h2>
        <div class="settings-row">
            <div class="settings-label">Hub title<div class="desc">Shown in the top-left of the topbar</div></div>
            <input class="settings-input" type="text" id="hubTitle" onchange="saveTop('title', this.value)">
        </div>
        <div class="settings-row">
            <div class="settings-label">Startup module<div class="desc">Which module opens when you visit /</div></div>
            <select class="settings-input" id="startupSelect" onchange="saveTop('startup', this.value)"></select>
        </div>
        <div class="settings-row">
            <div class="settings-label">Theme<div class="desc">Applies to the hub interface. Other open tabs update after refresh.</div></div>
            <select class="settings-input" id="themeSelect" onchange="saveTheme(this.value)">
                <option value="dark">Dark</option>
                <option value="light">Light</option>
                <option value="system">System</option>
            </select>
        </div>
        <div class="settings-row">
            <div class="settings-label">Port<div class="desc">Requires restart</div></div>
            <input class="settings-input narrow" type="number" id="portInput" onchange="saveTop('port', parseInt(this.value, 10))">
        </div>
    </div>

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M4 12h16"/><path d="M12 4c2 2.2 3 4.8 3 8s-1 5.8-3 8"/><path d="M12 4c-2 2.2-3 4.8-3 8s1 5.8 3 8"/></svg></span>Network</h2>
        <div class="settings-row">
            <div class="settings-label">Listen on LAN<div class="desc">&#x26A0; Allows other devices on your network to reach this hub. Only use on trusted networks. Requires restart.</div></div>
            <label class="settings-toggle"><input type="checkbox" id="listenLanInput"><span class="slider"></span></label>
        </div>
        <div class="settings-row">
            <div class="settings-label">Auto-add Windows firewall rule<div class="desc">Implies "Listen on LAN". Adding the rule needs Administrator the first time you run.</div></div>
            <label class="settings-toggle"><input type="checkbox" id="shareNetworkInput"><span class="slider"></span></label>
        </div>
        <div class="settings-row">
            <div class="settings-label">Require LAN access token<div class="desc">Other devices must supply a token to connect. Localhost is never asked. Recommended whenever Listen on LAN is on. Requires restart.</div></div>
            <label class="settings-toggle"><input type="checkbox" id="requireAuthInput"><span class="slider"></span></label>
        </div>
        <div class="settings-row">
            <div class="settings-label">Allow remote folder browser<div class="desc">&#x26A0; Lets LAN clients browse folders on this Hub PC for tools like Meta Copy. Use only on trusted networks; access token is strongly recommended.</div></div>
            <label class="settings-toggle"><input type="checkbox" id="allowRemoteBrowseInput"><span class="slider"></span></label>
        </div>
        <div class="settings-row col" id="tokenRow" style="display:none">
            <div class="settings-label">Access token<div class="desc">Share this URL with other devices on your LAN. Anyone with it has full hub access.</div></div>
            <div class="browse-wrap">
                <input class="settings-input wide" type="text" id="tokenUrlInput" readonly style="font-family:var(--mono);font-size:11px">
                <button class="action-btn secondary" id="copyTokenBtn">Copy</button>
                <button class="action-btn secondary" id="regenTokenBtn">Regenerate</button>
            </div>
            <div class="status-line" id="tokenStatus"></div>
        </div>
    </div>

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v4"/><path d="M12 18v4"/><path d="M2 12h4"/><path d="M18 12h4"/></svg></span>Civitai integration</h2>
        <div class="settings-row col">
            <div class="settings-label">models.json override<div class="desc">Optional override. Default: resources/civitai/models.json. Requires restart after change.</div></div>
            <div class="browse-wrap">
                <input class="settings-input wide" type="text" id="modelsPath" placeholder="Leave empty to use resources/civitai/models.json" onchange="saveSubPath('civitai.models_path', this.value)">
                <button class="action-btn secondary" onclick="openBrowse('file','.json',function(p){document.getElementById('modelsPath').value=p;saveSubPath('civitai.models_path',p)})">Browse</button>
                <button class="action-btn secondary" onclick="document.getElementById('modelsPath').value='';saveSubPath('civitai.models_path','')">Clear override</button>
            </div>
        </div>
        <div class="settings-row col">
            <div class="settings-label">API key<div class="desc">From <a href="https://civitai.com/user/account" target="_blank" class="help-link">civitai.com/user/account</a> (or civitai.red for NSFW). Needed to download/update models.</div></div>
            <input class="settings-input wide" type="password" id="civitaiApiKey" placeholder="Your Civitai API key" onchange="if(this.value)saveSubPath('civitai.api_key', this.value)">
        </div>
        <div class="settings-row">
            <div class="settings-label">Update models<div class="desc">Fetches new checkpoints from Civitai and merges into models.json</div></div>
            <div style="display:flex;gap:8px;align-items:center">
                <button class="action-btn" id="civitaiUpdateBtn" onclick="startCivitaiUpdate()">Update now</button>
            </div>
        </div>
        <div class="settings-row">
            <div class="settings-label">Verbose Civitai update log<div class="desc">Print update request/status/error details to the server console. API keys are not printed.</div></div>
            <label class="settings-toggle"><input type="checkbox" id="civitaiVerboseInput"><span class="slider"></span></label>
        </div>
        <div class="status-line" id="civitaiStatus"></div>
    </div>

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/></svg></span>Software updates</h2>
        <div class="settings-row">
            <div class="settings-label">Check GitHub for updates<div class="desc">Only connects to the official CyberHub GitHub repository when you click this button. CyberHub never checks automatically.</div></div>
            <button class="action-btn" id="githubUpdateCheckBtn" onclick="checkCyberHubUpdates()">Check for updates</button>
        </div>
        <div class="status-line" id="githubUpdateStatus">No update check has been performed.</div>
        <div class="update-release" id="githubUpdateRelease" style="display:none">
            <div class="update-release-head">
                <div>
                    <div class="update-release-title" id="githubUpdateTitle"></div>
                    <div class="update-release-meta" id="githubUpdateMeta"></div>
                </div>
                <a class="help-link" id="githubUpdateLink" href="#" target="_blank" rel="noopener" style="display:none">View release</a>
            </div>
            <div class="update-package-list" id="githubUpdatePackages"></div>
            <details class="update-notes" id="githubUpdateNotesWrap" style="display:none">
                <summary>Release notes</summary>
                <pre id="githubUpdateNotes"></pre>
            </details>
        </div>
        <div class="update-job" id="githubUpdateJob">
            <div class="update-job-line"><span id="githubUpdateJobMessage">Preparing update...</span><span class="bytes" id="githubUpdateJobBytes"></span></div>
            <div class="update-progress"><div class="update-progress-fill" id="githubUpdateProgress"></div></div>
            <div class="status-line" id="githubUpdateJobError"></div>
        </div>
    </div>

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M19 11a7 7 0 0 0-12-4l-3 3"/><path d="M5 13a7 7 0 0 0 12 4l3-3"/></svg></span>Maintenance</h2>
        <div class="settings-row">
            <div class="settings-label">Restart hub<div class="desc">Applies all pending changes. The browser will reconnect automatically.</div></div>
            <button class="action-btn" onclick="restartHub()">Restart now</button>
        </div>
        <div class="settings-row col">
            <div class="settings-label">Import update or module ZIP<div class="desc">Installs CyberHub update zips and new module zips. Existing files are backed up first. Localhost only.</div></div>
            <div class="browse-wrap">
                <input class="settings-input wide import-file" type="file" id="importZipInput" accept=".zip,application/zip">
                <button class="action-btn" id="importZipBtn" onclick="importPackage()">Import ZIP</button>
            </div>
            <div class="status-line" id="importStatus"></div>
        </div>
        <div id="galleryMaintRows">
            <div class="settings-row">
                <div class="settings-label">Gallery maintenance<div class="desc">Quickly sync folders, rebuild database search data, or pre-generate missing thumbnails.</div></div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">
                    <button class="action-btn secondary" id="btnReindex">Re-index folders</button>
                    <button class="action-btn secondary" id="btnSearchIndex">Rebuild search</button>
                    <button class="action-btn secondary" id="btnGenThumbs">Generate all thumbnails</button>
                </div>
            </div>
            <div class="settings-row">
                <div class="settings-label">Optimize database<div class="desc">Refresh query statistics and only rewrite the database when enough unused space can actually be reclaimed.</div></div>
                <button class="action-btn secondary" id="btnOptimizeDb">Optimize now</button>
            </div>
            <div class="settings-row">
                <div class="settings-label">Delete images without metadata<div class="desc">Counts completed images with no generation metadata, asks for confirmation, then moves them to the system trash in the background. Pending and failed metadata reads are excluded.</div></div>
                <button class="action-btn danger" id="btnDeleteNoMetadata">Check and delete...</button>
            </div>
            <div class="status-line" id="galleryActionStatus"></div>
        </div>
    </div>

   </div><!-- /settings-col (left) -->
   <div class="settings-col">

    <div class="settings-section">
        <h2><span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/></svg></span>Modules</h2>
        <div id="moduleList"></div>
    </div>

   </div><!-- /settings-col (right) -->
  </div><!-- /settings-cols -->

    <div class="diag-panel">
        <div class="diag-head" id="diagHead" onclick="toggleDiagnostics()">
            <div class="diag-title">
                <span class="diag-chev" id="diagChev">&#9656;</span>
                <span class="section-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1-2 3-.2-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.2h-3.6V21a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.2.1-2-3 .1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H4v-4h.2a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1 2-3 .2.1a1.6 1.6 0 0 0 1.8.3 1.6 1.6 0 0 0 1-1.5V3h3.6v.2a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.2-.1 2 3-.1.1a1.6 1.6 0 0 0-.3 1.8 1.6 1.6 0 0 0 1.5 1h.2v4H20a1.6 1.6 0 0 0-1.6 1z"/></svg></span>System diagnostics
                <span class="diag-hint" id="diagHint">— click to expand</span>
            </div>
            <div class="diag-actions">
                <button class="diag-copy-btn" id="diagRefreshBtn" onclick="event.stopPropagation(); loadDiagnostics(true);" style="display:none" title="Re-run all checks">&#x21BB; Refresh</button>
                <button class="diag-copy-btn" id="diagCopyBtn" onclick="event.stopPropagation(); copyDiagnostics();" style="display:none">&#x1F4CB; Copy as text</button>
            </div>
        </div>
        <div class="diag-body" id="diagBody" style="display:none">
            <div class="diag-loading" id="diagLoading" style="display:none">Loading diagnostics…</div>
            <div class="diag-sections" id="diagSections">
                <!-- filled by JS from /api/system/info on first expand -->
            </div>
        </div>
    </div>

</div>

<!-- Browse dialog -->
<div class="browse-overlay" id="browseOverlay">
    <div class="browse-dialog">
        <div class="browse-header">
            <h3 id="browseTitle">Select folder</h3>
            <button class="browse-close" onclick="closeBrowse()">&times;</button>
        </div>
        <div class="browse-crumb" id="browseCrumb"></div>
        <div class="browse-body" id="browseBody"></div>
        <div class="browse-footer">
            <div class="browse-path-display" id="browseCurPath"></div>
            <button class="action-btn" id="browseSelectBtn" onclick="selectBrowse()">Select</button>
        </div>
    </div>
</div>

<script>
var _restart = document.getElementById('restartBanner');
function flagRestart() {
    if (!_restart) return;
    try { localStorage.setItem('settingsRestartPending', '1'); } catch(e) {}
    var wasVisible = _restart.classList.contains('visible');
    _restart.classList.add('visible');
    setTimeout(function(){
        _restart.scrollIntoView({ behavior:'smooth', block:'start' });
    }, wasVisible ? 0 : 40);
}
try {
    if (localStorage.getItem('settingsRestartPending') === '1' && _restart) {
        _restart.classList.add('visible');
    }
} catch(e) {}

// ─── Browse Dialog ───────────────────────────────────────────────────────
var _browseMode = 'folder'; // 'folder' or 'file'
var _browseExt = '';
var _browseCallback = null;
var _browseCurPath = '';
var _browseSelectedFile = '';
var BROWSE_ICONS = {
    computer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M8 21h8"/><path d="M12 16v5"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5v-9z"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>'
};

function openBrowse(mode, ext, callback) {
    _browseMode = mode || 'folder';
    _browseExt = ext || '';
    _browseCallback = callback;
    _browseSelectedFile = '';
    document.getElementById('browseTitle').textContent = mode === 'file' ? 'Select file' : 'Select folder';
    document.getElementById('browseSelectBtn').textContent = mode === 'file' ? 'Select file' : 'Select this folder';
    document.getElementById('browseOverlay').classList.add('open');
    loadBrowseDir('');
}

function closeBrowse() {
    document.getElementById('browseOverlay').classList.remove('open');
    _browseCallback = null;
}

function selectBrowse() {
    var path = (_browseMode === 'file' && _browseSelectedFile) ? _browseSelectedFile : _browseCurPath;
    if (path && _browseCallback) {
        _browseCallback(path);
    }
    closeBrowse();
}

async function loadBrowseDir(path) {
    var ext = (_browseMode === 'file') ? '&ext=' + encodeURIComponent(_browseExt) : '';
    var url = '/api/browse?path=' + encodeURIComponent(path || '') + ext;
    var data;
    try {
        var r = await fetch(url);
        data = await r.json();
    } catch(e) {
        document.getElementById('browseBody').innerHTML = '<div class="browse-empty">Failed to load: ' + escHtml(e.message) + '</div>';
        return;
    }

    if (data.error) {
        document.getElementById('browseBody').innerHTML = '<div class="browse-empty">' + escHtml(data.error) + '</div>';
        return;
    }

    _browseCurPath = data.path || '';
    _browseSelectedFile = '';
    document.getElementById('browseCurPath').textContent = data.display || 'Drives';

    // Breadcrumb — backend gives us pre-built segments with paths
    var crumb = document.getElementById('browseCrumb');
    var crumbParts = ['<span class="browse-crumb-home" data-browse-path="">' + BROWSE_ICONS.computer + '</span>'];
    var crumbs = data.crumbs || [];
    for (var ci = 0; ci < crumbs.length; ci++) {
        crumbParts.push('<span class="sep">&#x203A;</span>');
        crumbParts.push('<span data-browse-path="' + escAttr(crumbs[ci].path) + '">' + escHtml(crumbs[ci].label) + '</span>');
    }
    crumb.innerHTML = crumbParts.join('');

    // Entries — backend gives us absolute paths for every dir/file
    var body = document.getElementById('browseBody');
    var entries = '';

    if (data.parent !== null && data.parent !== undefined) {
        entries += '<div class="browse-entry" data-browse-path="' + escAttr(data.parent) + '">'
                + '<span class="icon">' + BROWSE_ICONS.up + '</span><span class="name">..</span></div>';
    }

    for (var di = 0; di < data.dirs.length; di++) {
        var d = data.dirs[di];
        entries += '<div class="browse-entry" data-browse-path="' + escAttr(d.path) + '">'
                + '<span class="icon">' + BROWSE_ICONS.folder + '</span><span class="name">' + escHtml(d.name) + '</span></div>';
    }

    for (var fi = 0; fi < data.files.length; fi++) {
        var f = data.files[fi];
        entries += '<div class="browse-entry file" data-browse-file="' + escAttr(f.path) + '">'
                + '<span class="icon">' + BROWSE_ICONS.file + '</span><span class="name">' + escHtml(f.name) + '</span></div>';
    }

    if (!entries) entries = '<div class="browse-empty">Empty folder</div>';
    body.innerHTML = entries;

    document.getElementById('browseSelectBtn').disabled = (_browseMode === 'file' && !_browseSelectedFile);
}

/* One-time event delegation: navigate on click in body/crumb */
document.addEventListener('click', function(ev) {
    var ov = document.getElementById('browseOverlay');
    if (!ov || !ov.classList.contains('open')) return;
    var navEl = ev.target.closest('[data-browse-path]');
    if (navEl) {
        loadBrowseDir(navEl.getAttribute('data-browse-path'));
        return;
    }
    var fileEl = ev.target.closest('[data-browse-file]');
    if (fileEl) {
        document.querySelectorAll('.browse-entry.selected').forEach(function(e) { e.classList.remove('selected'); });
        fileEl.classList.add('selected');
        _browseSelectedFile = fileEl.getAttribute('data-browse-file');
        document.getElementById('browseSelectBtn').disabled = false;
        document.getElementById('browseCurPath').textContent = _browseSelectedFile;
    }
});

/* Close dialog on Escape, on overlay click (but not dialog click) */
document.addEventListener('keydown', function(ev) {
    if (ev.key === 'Escape') {
        var ov = document.getElementById('browseOverlay');
        if (ov && ov.classList.contains('open')) closeBrowse();
    }
});
document.addEventListener('click', function(ev) {
    var ov = document.getElementById('browseOverlay');
    if (ov && ov.classList.contains('open') && ev.target === ov) closeBrowse();
});

/* Browse-trigger delegation: rendered Browse buttons for `folder`/`file` inputs */
document.addEventListener('click', function(ev) {
    var btn = ev.target.closest('.browse-trigger');
    if (!btn) return;
    ev.preventDefault();
    var modKey = btn.getAttribute('data-mod');
    var fieldKey = btn.getAttribute('data-key');
    var mode = btn.getAttribute('data-mode') || 'folder';
    var ext = btn.getAttribute('data-ext') || '';
    openBrowse(mode, ext, function(path) {
        var inp = document.querySelector('[data-mod="' + modKey + '"][data-key="' + fieldKey + '"]');
        if (inp) {
            inp.value = path;
            inp.dispatchEvent(new Event('change'));
        }
    });
});

// ─── Folder list helpers ─────────────────────────────────────────────────
function openFolderBrowse(modKey, fieldKey) {
    openBrowse('folder', '', function(path) {
        addFolderToList(modKey, fieldKey, path);
    });
}

async function addFolderToList(modKey, fieldKey, path) {
    if (!path) return;
    var s = await fetch('/api/settings').then(function(r){return r.json()});
    var folders = ((s.modules || {})[modKey] || {})[fieldKey] || [];
    if (folders.indexOf(path) >= 0) { showToast('Already added'); return; }
    folders.push(path);
    await saveModule(modKey, fieldKey, folders);
    flagRestart();
    loadAll();
}

async function removeFolderFromList(modKey, fieldKey, index) {
    var s = await fetch('/api/settings').then(function(r){return r.json()});
    var folders = ((s.modules || {})[modKey] || {})[fieldKey] || [];
    folders.splice(index, 1);
    await saveModule(modKey, fieldKey, folders);
    flagRestart();
    loadAll();
}

function saveTop(key, value) {
    return fetch('/api/settings/save', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({module:'', key:key, value:value}) })
        .then(function(){
            showToast('Saved');
            if (key === 'port' || key === 'startup') flagRestart();
        });
}
function saveTheme(value) {
    document.body.classList.remove('theme-dark', 'theme-light', 'theme-system');
    document.body.classList.add('theme-' + value);
    return saveTop('theme', value);
}
function saveSubPath(path, value) {
    return fetch('/api/settings/save_path', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({path:path, value:value}) })
        .then(function(){
            showToast('Saved');
            if (path !== 'civitai.api_key' && path !== 'civitai.verbose') flagRestart();
        });
}
function saveModule(modKey, key, value) {
    return fetch('/api/settings/save', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({module:modKey, key:key, value:value}) })
        .then(function(){ showToast('Saved'); });
}
function clearModuleOverride(modKey, key) {
    return saveModule(modKey, key, '').then(function(){ loadAll(); });
}
function toggleModule(modKey, enabled) {
    return fetch('/api/settings/module/toggle', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({module:modKey, enabled:enabled}) })
        .then(function(){ flagRestart(); });
}

function renderField(modKey, fieldKey, schema, currentValue) {
    var label = schema.label || fieldKey;
    var desc = schema.desc || (schema.requires_restart ? 'Requires restart' : '');
    var t = schema.type || 'string';
    var val = (currentValue === undefined || currentValue === null) ? (schema.default !== undefined ? schema.default : '') : currentValue;
    var restartAttr = schema.requires_restart ? ' data-restart="1"' : '';
    var inputHtml = '';
    if (t === 'bool') {
        var checked = val ? 'checked' : '';
        inputHtml = '<label class="settings-toggle"><input type="checkbox" ' + checked + ' data-mod="'+modKey+'" data-key="'+fieldKey+'" data-type="bool"' + restartAttr + '><span class="slider"></span></label>';
    } else if (t === 'number') {
        inputHtml = '<input class="settings-input narrow" type="number" value="' + escAttr(val) + '" data-mod="'+modKey+'" data-key="'+fieldKey+'" data-type="number"' + restartAttr
            + (schema.min !== undefined ? ' min="'+schema.min+'"' : '')
            + (schema.max !== undefined ? ' max="'+schema.max+'"' : '')
            + (schema.step !== undefined ? ' step="'+schema.step+'"' : '')
            + '>';
    } else if (t === 'select') {
        var opts = (schema.options || []).map(function(o){
            var sel = (String(o) === String(val)) ? ' selected' : '';
            return '<option value="'+escAttr(o)+'"'+sel+'>'+escHtml(o)+'</option>';
        }).join('');
        inputHtml = '<select class="settings-input" data-mod="'+modKey+'" data-key="'+fieldKey+'" data-type="select"' + restartAttr + '>' + opts + '</select>';
    } else if (t === 'textarea') {
        inputHtml = '<textarea class="settings-input wide settings-textarea" data-mod="'+modKey+'" data-key="'+fieldKey+'" data-type="string"' + restartAttr
            + (schema.placeholder ? ' placeholder="'+escAttr(schema.placeholder)+'"' : '')
            + (schema.rows ? ' rows="'+schema.rows+'"' : ' rows="8"')
            + '>' + escHtml(val) + '</textarea>';
    } else if (t === 'folder_list') {
        var items = Array.isArray(val) ? val : [];
        inputHtml = '<div class="folder-list" id="fl_'+modKey+'_'+fieldKey+'">';
        if (!items.length) {
            inputHtml += '<div class="folder-empty">No folders configured</div>';
        }
        for (var fli = 0; fli < items.length; fli++) {
            inputHtml += '<div class="folder-item"><span class="path">' + escHtml(items[fli]) + '</span>'
                + '<button class="remove-btn" onclick="removeFolderFromList(\''+modKey+'\',\''+fieldKey+'\','+fli+')">&times;</button></div>';
        }
        inputHtml += '</div>';
        inputHtml += '<div class="folder-add-row">'
            + '<button class="action-btn secondary" onclick="openFolderBrowse(\''+modKey+'\',\''+fieldKey+'\')">Add folder</button>'
            + '</div>';
    } else { // string, folder, file
        var ext = (t === 'file' && schema.ext) ? schema.ext : '.json';
        inputHtml = '<div class="browse-wrap"><input class="settings-input wide" type="text" value="' + escAttr(val) + '" data-mod="'+modKey+'" data-key="'+fieldKey+'" data-type="string"' + restartAttr
            + (schema.placeholder ? ' placeholder="'+escAttr(schema.placeholder)+'"' : '')
            + '>';
        if (t === 'folder' || t === 'file') {
            var mode = (t === 'file') ? 'file' : 'folder';
            var extArg = (t === 'file') ? ext : '';
            inputHtml += '<button class="action-btn secondary browse-trigger" '
                + 'data-mod="'+modKey+'" data-key="'+fieldKey+'" '
                + 'data-mode="'+mode+'" data-ext="'+escAttr(extArg)+'">Browse</button>';
            if (schema.override) {
                inputHtml += '<button class="action-btn secondary" onclick="clearModuleOverride(\''+modKey+'\',\''+fieldKey+'\')">Clear override</button>';
            }
        }
        inputHtml += '</div>';
    }
    var rowClass = (t === 'string' || t === 'folder' || t === 'file' || t === 'textarea') ? 'settings-row col' : 'settings-row';
    return '<div class="' + rowClass + '">'
         + '<div class="settings-label">' + escHtml(label)
         + (desc ? '<div class="desc">' + escHtml(desc) + '</div>' : '')
         + '</div>' + inputHtml + '</div>';
}

function wireFieldEvents(container) {
    container.querySelectorAll('[data-mod][data-key]').forEach(function(el) {
        var modKey = el.getAttribute('data-mod');
        var key = el.getAttribute('data-key');
        var type = el.getAttribute('data-type');
        function send() {
            var value;
            if (type === 'bool') value = el.checked;
            else if (type === 'number') {
                var n = parseFloat(el.value); value = isNaN(n) ? 0 : n;
                if (el.step === '1' || (!('step' in el) || el.step === '') && Number.isInteger(parseFloat(el.value))) {
                    value = Math.round(value);
                }
            }
            else value = el.value;
            saveModule(modKey, key, value);
            if (el.dataset.restart === '1') flagRestart();
        }
        el.addEventListener('change', send);
    });
}

/* Gallery maintenance uses one server-side job at a time. The status endpoint
   keeps this page informative even for long jobs and after navigating away/back. */
var _galleryMaintTimer = null;

function setGalleryMaintenanceButtons(disabled) {
    ['btnReindex','btnSearchIndex','btnGenThumbs','btnOptimizeDb','btnDeleteNoMetadata'].forEach(function(id) {
        var button = document.getElementById(id);
        if (button) button.disabled = !!disabled;
    });
}

function renderGalleryMaintenance(data) {
    var st = document.getElementById('galleryActionStatus');
    if (!st || !data) return false;
    var job = data.job || {};
    var scan = data.scan || {};
    var background = data.background || {};
    if (!job.active && scan.active) {
        job = {
            active:true, label:'Gallery folder scan', stage:'scan',
            message:'Scanning ' + (scan.root || 'gallery folders'),
            current:scan.scanned || 0, total:scan.total || 0,
            percent:scan.total && scan.scanned <= scan.total ? (scan.scanned / scan.total * 100) : 0,
            elapsed:job.elapsed || 0
        };
    }
    setGalleryMaintenanceButtons(!!job.active);

    var pending = (background.pending || 0) + (background.model_pending || 0);
    var keepPolling = !!job.active || !!scan.active || !!pending || !!background.active;
    if (!job.label && !pending) {
        st.innerHTML = '';
        return keepPolling;
    }

    var label = job.label || 'Background image processing';
    var message = job.error ? ('Error: ' + job.error) : (job.message || 'Working');
    var elapsed = job.elapsed ? Math.round(job.elapsed) + 's' : '';
    var current = Number(job.current || 0);
    var total = Number(job.total || 0);
    var exactProgress = total > 0 && current <= total;
    var percent = exactProgress ? Math.max(0, Math.min(100, Number(job.percent || 0))) : 0;
    var detail = exactProgress ? (current + ' / ' + total + ' · ' + percent.toFixed(1) + '%') : '';
    var details = job.details || {};
    if (details.generated != null) {
        detail += (detail ? ' · ' : '') + details.generated + ' generated · ' +
            (details.skipped || 0) + ' current · ' + (details.errors || 0) + ' errors';
    }
    if (details.deleted != null) {
        detail += (detail ? ' · ' : '') + details.deleted + ' moved to trash';
        if (details.failed) detail += ' · ' + details.failed + ' failed';
    }
    if (!job.active && pending) {
        detail += (detail ? ' · ' : '') + pending + ' images remain in background processing';
    }
    var fillClass = 'maint-progress-fill' + (job.active && !exactProgress ? ' indeterminate' : '');
    var fillStyle = exactProgress ? ('width:' + percent + '%') : (!job.active ? 'width:100%' : '');
    st.innerHTML = '<div class="maint-status">' +
        '<div class="maint-status-head"><span>' + escHtml(label) + '</span><span class="elapsed">' + escHtml(elapsed) + '</span></div>' +
        '<div class="maint-progress"><div class="' + fillClass + '" style="' + fillStyle + '"></div></div>' +
        '<div class="' + (job.error ? 'maint-error' : 'maint-detail') + '">' + escHtml(message) +
            (detail ? ' · ' + escHtml(detail) : '') + '</div>' +
        '</div>';
    return keepPolling;
}

function pollGalleryMaintenance(delay) {
    if (_galleryMaintTimer) clearTimeout(_galleryMaintTimer);
    _galleryMaintTimer = setTimeout(function() {
        fetch('/api/maintenance_status').then(function(r){ return r.json(); }).then(function(data) {
            var keepPolling = renderGalleryMaintenance(data);
            if (keepPolling) pollGalleryMaintenance(1000);
        }).catch(function(e) {
            var st = document.getElementById('galleryActionStatus');
            if (st) st.textContent = 'Error reading maintenance status: ' + e.message;
            setGalleryMaintenanceButtons(false);
        });
    }, delay == null ? 0 : delay);
}

/* Wire buttons using onclick so repeated loadAll() calls do not stack handlers. */
function updateGalleryMaint(mods) {
    var rows = document.getElementById('galleryMaintRows');
    if (!rows) return;
    var gal = mods.filter(function(m){ return m.key === 'gallery'; })[0];
    var enabled = !!(gal && gal.enabled);
    rows.style.display = enabled ? '' : 'none';
    if (!enabled) return;
    var st = document.getElementById('galleryActionStatus');

    function run(btn, url, body, startingText) {
        if (!btn) return;
        btn.onclick = function() {
            setGalleryMaintenanceButtons(true);
            st.textContent = startingText;
            fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:body })
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.error) throw new Error(d.error);
                    if (!d.started) {
                        st.textContent = d.busy ? 'Another Gallery maintenance task is already running.' : 'This task is already running.';
                        pollGalleryMaintenance(0);
                        return;
                    }
                    pollGalleryMaintenance(100);
                })
                .catch(function(e) {
                    st.textContent = 'Error: ' + e.message;
                    setGalleryMaintenanceButtons(false);
                });
        };
    }

    run(document.getElementById('btnReindex'), '/api/reindex', JSON.stringify({force:false}), 'Starting folder scan...');
    run(document.getElementById('btnSearchIndex'), '/api/rebuild_search', '{}', 'Starting search rebuild...');
    run(document.getElementById('btnGenThumbs'), '/api/generate_thumbs', '{}', 'Starting thumbnail check...');
    run(document.getElementById('btnOptimizeDb'), '/api/optimize_db', '{}', 'Starting database optimization...');
    var deleteButton = document.getElementById('btnDeleteNoMetadata');
    if (deleteButton) {
        deleteButton.onclick = async function() {
            setGalleryMaintenanceButtons(true);
            st.textContent = 'Checking images without metadata...';
            try {
                var previewResponse = await fetch('/api/delete/preview');
                var preview = await previewResponse.json();
                if (!previewResponse.ok || preview.error) throw new Error(preview.error || ('HTTP ' + previewResponse.status));
                var count = Number(preview.count || 0);
                if (!preview.trash) throw new Error('Safe delete is unavailable because send2trash is not installed.');
                if (!count) {
                    st.textContent = 'No completed images without generation metadata were found.';
                    setGalleryMaintenanceButtons(false);
                    return;
                }
                var confirmed = confirm(
                    'Move ' + count.toLocaleString() + ' image' + (count === 1 ? '' : 's') + ' without generation metadata to the system trash?\n\n' +
                    'Only successfully analyzed images are included. Pending and failed metadata reads are excluded.'
                );
                if (!confirmed) {
                    st.textContent = 'Delete cancelled.';
                    setGalleryMaintenanceButtons(false);
                    return;
                }
                st.textContent = 'Starting background delete...';
                var startResponse = await fetch('/api/delete/start', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({scope:'without_metadata', expected_count:count})
                });
                var started = await startResponse.json();
                if (!startResponse.ok || started.error) throw new Error(started.error || ('HTTP ' + startResponse.status));
                if (!started.started) {
                    st.textContent = started.empty ? 'No matching images remain.' : 'Another Gallery maintenance task is already running.';
                    setGalleryMaintenanceButtons(false);
                    pollGalleryMaintenance(0);
                    return;
                }
                pollGalleryMaintenance(100);
            } catch (e) {
                st.textContent = 'Error: ' + e.message;
                setGalleryMaintenanceButtons(false);
            }
        };
    }
    pollGalleryMaintenance(0);
}

function loadAll() {
    Promise.all([
        fetch('/api/settings').then(function(r){return r.json()}),
        fetch('/api/settings/modules').then(function(r){return r.json()}),
    ]).then(function(both) {
        var s = both[0], mods = both[1];

        document.getElementById('hubTitle').value = s.title || 'CyberHub';
        document.getElementById('portInput').value = s.port || 8899;
        document.getElementById('themeSelect').value = s.theme || 'dark';

        var net = s.network || {};
        var listenEl = document.getElementById('listenLanInput');
        var shareEl = document.getElementById('shareNetworkInput');
        var authEl = document.getElementById('requireAuthInput');
        var remoteBrowseEl = document.getElementById('allowRemoteBrowseInput');
        var tokenRow = document.getElementById('tokenRow');
        var tokenUrlInput = document.getElementById('tokenUrlInput');
        var tokenStatus = document.getElementById('tokenStatus');
        listenEl.checked = !!net.listen_lan;
        shareEl.checked = !!net.share_network;
        authEl.checked = net.require_auth !== false;  // default true
        remoteBrowseEl.checked = !!net.allow_remote_browse;
        listenEl.onchange = function() {
            saveSubPath('network.listen_lan', this.checked);
            updateTokenRowVisibility();
        };
        shareEl.onchange = function() {
            saveSubPath('network.share_network', this.checked);
            if (this.checked && !listenEl.checked) { listenEl.checked = true; saveSubPath('network.listen_lan', true); }
            updateTokenRowVisibility();
        };
        authEl.onchange = function() {
            saveSubPath('network.require_auth', this.checked);
            updateTokenRowVisibility();
        };
        remoteBrowseEl.onchange = function() {
            saveSubPath('network.allow_remote_browse', this.checked);
            if (this.checked && !authEl.checked) {
                tokenStatus.textContent = 'Remote folder browser is enabled without an access token. Use only on a trusted LAN.';
            }
        };

        function updateTokenRowVisibility() {
            var shouldShow = (listenEl.checked || shareEl.checked) && authEl.checked;
            tokenRow.style.display = shouldShow ? '' : 'none';
            if (shouldShow) {
                if (net.auth_token) {
                    // Best-effort: show LAN URL with token. We use the hub's port
                    // (which is what the user will reach over LAN); host is
                    // a placeholder since the browser doesn't know the LAN IP.
                    var port = s.port || 8899;
                    tokenUrlInput.value = 'http://<your-lan-ip>:' + port + '/?token=' + net.auth_token;
                    tokenStatus.textContent = '';
                } else if (net.auth_token_set) {
                    tokenUrlInput.value = '(token saved — restart hub from LAN mode to view it)';
                    tokenStatus.textContent = '';
                } else {
                    tokenUrlInput.value = '(no token yet — will be generated when you restart in LAN mode)';
                    tokenStatus.textContent = '';
                }
            }
        }
        updateTokenRowVisibility();

        document.getElementById('copyTokenBtn').onclick = function() {
            copyText(tokenUrlInput.value, this);
        };
        document.getElementById('regenTokenBtn').onclick = function() {
            if (!confirm('Generate a new access token? Devices using the old URL will be locked out after restart.')) return;
            var btn = this;
            btn.disabled = true; tokenStatus.textContent = 'Generating…';
            fetch('/api/settings/network/regen_token', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' })
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.error) { tokenStatus.textContent = 'Error: ' + d.error; return; }
                    net.auth_token = d.token;
                    net.auth_token_set = true;
                    updateTokenRowVisibility();
                    tokenStatus.textContent = 'New token generated. Restart the hub to apply.';
                    flagRestart();
                })
                .catch(function(e) { tokenStatus.textContent = 'Error: ' + e.message; })
                .finally(function() { btn.disabled = false; });
        };

        var civ = s.civitai || {};
        document.getElementById('modelsPath').value = civ.models_path || '';
        var civVerboseInput = document.getElementById('civitaiVerboseInput');
        if (civVerboseInput) {
            civVerboseInput.checked = !!civ.verbose;
            civVerboseInput.onchange = function(){ saveSubPath('civitai.verbose', this.checked); };
        }
        var apiInput = document.getElementById('civitaiApiKey');
        apiInput.value = '';
        apiInput.placeholder = civ.api_key_set
            ? 'API key saved — type a new key to replace it'
            : 'Your Civitai API key';
        var hubVer = document.getElementById('hubVersion');
        if (hubVer) hubVer.textContent = 'v' + escHtml(s._hub_version || '1.0');
        loadCivitaiInfo();

        // Startup module dropdown — visible modules only (Settings has its own gear)
        var sel = document.getElementById('startupSelect');
        sel.innerHTML = '';
        mods.forEach(function(m) {
            if (!m.show_in_tabs || !m.enabled) return;
            var o = document.createElement('option');
            o.value = m.key;
            o.textContent = m.name;
            if (m.key === (s.startup || 'gallery')) o.selected = true;
            sel.appendChild(o);
        });

        // Modules section
        var list = document.getElementById('moduleList');
        list.innerHTML = '';
        mods.forEach(function(m) {
            if (m.key === 'settings') return;
            var card = document.createElement('div');
            card.className = 'module-card';
            var checked = m.enabled ? 'checked' : '';
            var schema = m.settings_schema || {};
            var hasSettings = m.enabled && Object.keys(schema).length > 0;
            var stageBadge = String(m.release_stage || 'stable').toLowerCase() === 'beta'
                ? '<span class="stage-badge beta">Beta</span>' : '';
            var protectedBadge = m.protected ? '<span class="stage-badge">System</span>' : '';
            var toggleHtml = m.protected
                ? '<span class="version-badge">Always enabled</span>'
                : '<label class="settings-toggle" title="Enable or disable ' + escHtml(m.name) + '"><input type="checkbox" ' + checked + ' data-mod-toggle="' + m.key + '" aria-label="Enable or disable ' + escHtml(m.name) + '"><span class="slider"></span></label>';
            var settingsHtml = hasSettings
                ? '<button type="button" class="chevron" id="chev_' + m.key + '" data-acc="' + m.key + '" title="Show module settings" aria-expanded="false"><span class="module-expand-icon">&#x25B6;</span><span class="module-expand-text">Settings</span></button>'
                : '';
            card.innerHTML =
                '<div class="mc-head">' +
                    '<div class="module-icon">' + (m.icon_html || '') + '</div>' +
                    '<div class="module-info">' +
                        '<div class="name"><span class="name-text">' + escHtml(m.name) + '</span><span class="version-badge">v' + escHtml(m.version || '1.0') + '</span>' + stageBadge + protectedBadge + '</div>' +
                        '<div class="desc">' + escHtml(m.description || '') + '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="module-actions">' +
                    '<div class="module-toggle-slot">' + toggleHtml + '</div>' +
                    '<div class="module-settings-slot">' + settingsHtml + '</div>' +
                '</div>';

            var wrapper = document.createElement('div');
            wrapper.style.marginBottom = '4px';
            wrapper.appendChild(card);

            // Per-module schema-driven settings
            var schemaKeys = Object.keys(schema);
            if (hasSettings) {
                var box = document.createElement('div');
                box.className = 'module-settings';
                box.id = 'msettings_' + m.key;
                var html = '';
                schemaKeys.forEach(function(k) {
                    html += renderField(m.key, k, schema[k], (m.current_settings || {})[k]);
                });
                box.innerHTML = html;
                wireFieldEvents(box);
                wrapper.appendChild(box);
            }

            list.appendChild(wrapper);
        });

        // Gallery maintenance lives in the global Maintenance block; show it only
        // when the gallery module is enabled (its endpoints need the gallery DB).
        updateGalleryMaint(mods);

        // Wire module enable/disable toggles
        list.querySelectorAll('[data-mod-toggle]').forEach(function(el) {
            el.addEventListener('change', function() {
                toggleModule(this.getAttribute('data-mod-toggle'), this.checked);
            });
        });

        // Only the explicit Settings button opens a module's settings panel.
        list.querySelectorAll('.chevron[data-acc]').forEach(function(button) {
            button.addEventListener('click', function() {
                var key = this.getAttribute('data-acc');
                var box = document.getElementById('msettings_' + key);
                var chev = document.getElementById('chev_' + key);
                if (!box) return;
                var isOpen = box.classList.contains('open');
                box.classList.toggle('open');
                if (chev) {
                    var nowOpen = box.classList.contains('open');
                    chev.classList.toggle('open', nowOpen);
                    chev.title = nowOpen ? 'Hide module settings' : 'Show module settings';
                    chev.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
                    var label = chev.querySelector('.module-expand-text');
                    if (label) label.textContent = nowOpen ? 'Hide' : 'Settings';
                }
            });
        });
    });
}

// ─── Civitai ─────────────────────────────────────────────────────────────
function loadCivitaiInfo() {
    fetch('/api/civitai/info').then(function(r){return r.json()}).then(function(info) {
        var el = document.getElementById('civitaiStatus');
        if (!info || !info.loaded) {
            if (info && info.path) {
                var missingSource = info.source_mode === 'override' ? 'Override not found: ' : 'Default resource not found: ';
                el.textContent = missingSource + info.path;
            } else {
                el.textContent = 'No models.json configured';
            }
            return;
        }
        var parts = [];
        var source = info.source_mode === 'override' ? 'override' : (info.source_mode === 'cli' ? 'CLI override' : 'resources/civitai');
        parts.push(source + ' · ' + info.hashes + ' hashes from ' + (info.model_count || '?') + ' models');
        if (info.file_size_mb) parts.push(info.file_size_mb + ' MB');
        if (info.data_date) {
            var age = info.age_days;
            parts.push('updated ' + info.data_date + (age > 0 ? ' (' + age + ' days ago)' : ' (today)'));
        }
        el.textContent = parts.join(' \u00B7 ');
    }).catch(function() {});
}

var _civitaiPollTimer = null;
function startCivitaiUpdate() {
    var btn = document.getElementById('civitaiUpdateBtn');
    var apiInput = document.getElementById('civitaiApiKey');
    var apiKey = apiInput ? apiInput.value.trim() : '';
    btn.disabled = true;
    btn.textContent = 'Updating...';
    var st = document.getElementById('civitaiStatus');
    st.textContent = 'Starting update...';

    fetch('/api/civitai/update', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ api_key: apiKey }) })
        .then(function(r){return r.json()})
        .then(function(d) {
            if (d.error) {
                st.textContent = 'Error: ' + d.error;
                btn.disabled = false;
                btn.textContent = 'Update now';
                return;
            }
            if (apiInput && apiKey) {
                apiInput.value = '';
                apiInput.placeholder = 'API key saved — type a new key to replace it';
            }
            // Start polling for status
            pollCivitaiStatus();
        })
        .catch(function(e) {
            st.textContent = 'Error: ' + e.message;
            btn.disabled = false;
            btn.textContent = 'Update now';
        });
}

function pollCivitaiStatus() {
    if (_civitaiPollTimer) clearTimeout(_civitaiPollTimer);
    fetch('/api/civitai/status').then(function(r){return r.json()}).then(function(s) {
        var st = document.getElementById('civitaiStatus');
        var btn = document.getElementById('civitaiUpdateBtn');
        var msg = s.message || '';
        if (s.progress) msg += ' — ' + s.progress;
        st.textContent = msg;
        if (s.running) {
            _civitaiPollTimer = setTimeout(pollCivitaiStatus, 1000);
        } else {
            btn.disabled = false;
            btn.textContent = 'Update now';
            loadCivitaiInfo();
        }
    }).catch(function() {
        document.getElementById('civitaiUpdateBtn').disabled = false;
        document.getElementById('civitaiUpdateBtn').textContent = 'Update now';
    });
}

// ─── Manual GitHub updates ───────────────────────────────────────────────
var _githubUpdateInfo = null;
var _githubUpdatePollTimer = null;

function formatUpdateBytes(value) {
    var bytes = Number(value || 0);
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

async function updateResponseJson(response, allowJobError) {
    var data;
    try { data = await response.json(); }
    catch (e) { throw new Error('CyberHub received an invalid update response.'); }
    if (!response.ok || (data.error && !allowJobError)) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
}

function setUpdateButtonsDisabled(disabled) {
    var check = document.getElementById('githubUpdateCheckBtn');
    if (check) check.disabled = !!disabled;
    document.querySelectorAll('[data-update-asset]').forEach(function(button) {
        button.disabled = !!disabled || button.dataset.compatible !== '1';
    });
}

function renderCyberHubUpdate(data) {
    _githubUpdateInfo = data;
    var release = data.release || {};
    var wrap = document.getElementById('githubUpdateRelease');
    var list = document.getElementById('githubUpdatePackages');
    document.getElementById('githubUpdateTitle').textContent = release.name || release.tag || 'CyberHub release';
    var meta = [];
    if (release.tag) meta.push(release.tag);
    if (release.published_at) meta.push(new Date(release.published_at).toLocaleString());
    document.getElementById('githubUpdateMeta').textContent = meta.join(' · ');
    var link = document.getElementById('githubUpdateLink');
    if (release.url) {
        link.href = release.url;
        link.style.display = '';
    } else {
        link.removeAttribute('href');
        link.style.display = 'none';
    }

    list.innerHTML = '';
    var available = (data.packages || []).filter(function(item) { return item.update_available; });
    if (!available.length) {
        var empty = document.createElement('div');
        empty.className = 'update-empty';
        empty.textContent = 'CyberHub and the installed modules are up to date.';
        list.appendChild(empty);
    } else {
        available.forEach(function(item) {
            var row = document.createElement('div');
            row.className = 'update-package';
            var info = document.createElement('div');
            info.className = 'update-package-info';
            var name = document.createElement('div');
            name.className = 'update-package-name';
            name.textContent = item.name + (item.type === 'hub' ? ' (complete update)' : '');
            var version = document.createElement('div');
            version.className = 'update-package-version';
            version.textContent = item.current_version + '  →  ' + item.version + (item.size ? '  ·  ' + formatUpdateBytes(item.size) : '');
            info.appendChild(name);
            info.appendChild(version);
            if (!item.compatible) {
                var note = document.createElement('div');
                note.className = 'update-package-note';
                note.textContent = 'Requires CyberHub ' + item.minimum_hub_version + ' or newer. Install the complete update first.';
                info.appendChild(note);
            }
            var button = document.createElement('button');
            button.className = 'action-btn' + (item.type === 'module' ? ' secondary' : '');
            button.textContent = item.type === 'hub' ? 'Install update' : 'Install module';
            button.dataset.updateAsset = item.asset;
            button.dataset.updateName = item.name;
            button.dataset.updateVersion = item.version;
            button.dataset.compatible = item.compatible ? '1' : '0';
            button.disabled = !item.compatible;
            button.onclick = function() { installCyberHubUpdate(this); };
            row.appendChild(info);
            row.appendChild(button);
            list.appendChild(row);
        });
    }

    var notesWrap = document.getElementById('githubUpdateNotesWrap');
    var notes = String(release.notes || '').trim();
    notesWrap.style.display = notes ? '' : 'none';
    document.getElementById('githubUpdateNotes').textContent = notes;
    wrap.style.display = '';
}

async function checkCyberHubUpdates() {
    var button = document.getElementById('githubUpdateCheckBtn');
    var status = document.getElementById('githubUpdateStatus');
    var release = document.getElementById('githubUpdateRelease');
    button.disabled = true;
    button.textContent = 'Checking...';
    status.textContent = 'Contacting the official CyberHub GitHub repository...';
    release.style.display = 'none';
    try {
        var response = await fetch('/api/settings/update/check', { cache:'no-store' });
        var data = await updateResponseJson(response);
        renderCyberHubUpdate(data);
        status.textContent = 'Checked ' + (data.checked_at ? new Date(data.checked_at).toLocaleString() : 'just now') + '. No automatic checks are enabled.';
    } catch (e) {
        status.innerHTML = '<span class="import-summary err">Update check failed: ' + escHtml(e.message) + '</span>';
    } finally {
        button.disabled = false;
        button.textContent = 'Check for updates';
    }
}

async function installCyberHubUpdate(button) {
    if (!_githubUpdateInfo || !_githubUpdateInfo.release) return;
    var label = button.dataset.updateName + ' ' + button.dataset.updateVersion;
    if (!confirm('Install ' + label + '? Existing files are backed up first. CyberHub must be restarted afterwards.')) return;
    setUpdateButtonsDisabled(true);
    var job = document.getElementById('githubUpdateJob');
    job.classList.add('visible');
    document.getElementById('githubUpdateJobMessage').textContent = 'Starting ' + label + '...';
    document.getElementById('githubUpdateJobBytes').textContent = '';
    document.getElementById('githubUpdateJobError').textContent = '';
    var progress = document.getElementById('githubUpdateProgress');
    progress.style.width = '30%';
    progress.classList.add('indeterminate');
    try {
        var response = await fetch('/api/settings/update/install', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
                release_id:_githubUpdateInfo.release.id,
                asset:button.dataset.updateAsset
            })
        });
        await updateResponseJson(response);
        pollCyberHubUpdate();
    } catch (e) {
        progress.classList.remove('indeterminate');
        progress.style.width = '0%';
        document.getElementById('githubUpdateJobError').innerHTML = '<span class="import-summary err">' + escHtml(e.message) + '</span>';
        setUpdateButtonsDisabled(false);
    }
}

function pollCyberHubUpdate() {
    if (_githubUpdatePollTimer) clearTimeout(_githubUpdatePollTimer);
    fetch('/api/settings/update/status', { cache:'no-store' })
        .then(function(response) { return updateResponseJson(response, true); })
        .then(function(status) {
            var job = document.getElementById('githubUpdateJob');
            var progress = document.getElementById('githubUpdateProgress');
            var error = document.getElementById('githubUpdateJobError');
            job.classList.add('visible');
            document.getElementById('githubUpdateJobMessage').textContent = status.message || 'Updating...';
            var bytes = '';
            if (status.downloaded) {
                bytes = formatUpdateBytes(status.downloaded);
                if (status.total) bytes += ' / ' + formatUpdateBytes(status.total);
            }
            document.getElementById('githubUpdateJobBytes').textContent = bytes;
            if (status.stage === 'download' && status.total) {
                progress.classList.remove('indeterminate');
                progress.style.width = Math.max(0, Math.min(100, Number(status.percent || 0))) + '%';
            } else if (status.running) {
                progress.style.width = '30%';
                progress.classList.add('indeterminate');
            } else {
                progress.classList.remove('indeterminate');
                progress.style.width = status.stage === 'done' ? '100%' : '0%';
            }
            if (status.running) {
                _githubUpdatePollTimer = setTimeout(pollCyberHubUpdate, 500);
            } else if (status.stage === 'done') {
                error.innerHTML = '<span class="import-summary ok">Installed successfully. Restart CyberHub to use the update.</span>';
                flagRestart();
            } else {
                error.innerHTML = '<span class="import-summary err">Update failed: ' + escHtml(status.error || status.message || 'Unknown error') + '</span>';
                setUpdateButtonsDisabled(false);
            }
        })
        .catch(function(e) {
            document.getElementById('githubUpdateJobError').innerHTML = '<span class="import-summary err">Could not read update progress: ' + escHtml(e.message) + '</span>';
            setUpdateButtonsDisabled(false);
        });
}

// ─── Restart ─────────────────────────────────────────────────────────────
function restartHub() {
    if (!confirm('Restart the hub? The page will reload automatically.')) return;
    try { localStorage.removeItem('settingsRestartPending'); } catch(e) {}
    fetch('/api/restart', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' })
        .then(function() {
            showToast('Restarting...');
            setTimeout(function poll() {
                fetch('/api/settings').then(function() { location.reload(); })
                    .catch(function() { setTimeout(poll, 1000); });
            }, 2000);
        })
        .catch(function() {});
}

async function importPackage() {
    var input = document.getElementById('importZipInput');
    var btn = document.getElementById('importZipBtn');
    var st = document.getElementById('importStatus');
    if (!input || !input.files || !input.files.length) {
        st.innerHTML = '<span class="import-summary err">Choose a ZIP file first.</span>';
        return;
    }
    var file = input.files[0];
    if (!/\.zip$/i.test(file.name || '')) {
        st.innerHTML = '<span class="import-summary err">Only .zip files can be imported.</span>';
        return;
    }
    var form = new FormData();
    form.append('package', file, file.name);
    btn.disabled = true;
    btn.textContent = 'Importing...';
    st.textContent = 'Uploading and validating ' + file.name + '...';
    try {
        var r = await fetch('/api/settings/import_package', { method:'POST', body:form });
        var d = await r.json();
        if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
        var parts = [
            'Installed ' + d.installed + ' file' + (d.installed === 1 ? '' : 's'),
            d.created + ' new',
            d.updated + ' updated'
        ];
        if (d.backup) parts.push('backup: ' + d.backup);
        st.innerHTML = '<span class="import-summary ok">' + escHtml(parts.join(' · ')) + '</span>';
        input.value = '';
        flagRestart();
        loadAll();
    } catch(e) {
        st.innerHTML = '<span class="import-summary err">Import failed: ' + escHtml(e.message) + '</span>';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Import ZIP';
    }
}

// ─── System Info ─────────────────────────────────────────────────────────
/* Last-loaded sysinfo blob — used by copy-to-clipboard to format a plain-text
   report we can paste into a bug report. Diagnostics are collapsed by default
   and only fetched when the user opens the panel — the server-side checks
   (subprocess calls on Windows, walking thumb/attachment folders) aren't free,
   especially on a gallery with hundreds of thousands of files. */
var _lastSysInfo = null;
var _diagLoaded = false;
var _diagLoading = false;

function toggleDiagnostics() {
    var panel = document.querySelector('.diag-panel');
    var body = document.getElementById('diagBody');
    var isOpen = panel.classList.toggle('open');
    body.style.display = isOpen ? '' : 'none';
    if (isOpen && !_diagLoaded && !_diagLoading) {
        loadDiagnostics(false);
    }
}

/* Fetch the diagnostic info. Re-runs if `force` is true (Refresh button) — otherwise
   the first expand fetches once and subsequent expands reuse the cached data. */
function loadDiagnostics(force) {
    if (_diagLoading) return;
    if (_diagLoaded && !force) return;
    _diagLoading = true;
    var loading = document.getElementById('diagLoading');
    var sections = document.getElementById('diagSections');
    var refreshBtn = document.getElementById('diagRefreshBtn');
    var copyBtn = document.getElementById('diagCopyBtn');
    loading.style.display = '';
    sections.innerHTML = '';
    if (refreshBtn) refreshBtn.style.display = 'none';
    if (copyBtn)    copyBtn.style.display = 'none';
    fetch('/api/system/info').then(function(r){return r.json()}).then(function(d) {
        _lastSysInfo = d;
        _diagLoaded = true;
        _diagLoading = false;
        loading.style.display = 'none';
        if (refreshBtn) refreshBtn.style.display = '';
        if (copyBtn)    copyBtn.style.display = '';
        renderDiagnostics(d);
    }).catch(function(e) {
        _diagLoading = false;
        loading.textContent = 'Error loading diagnostics: ' + e;
    });
}

/* Render four grouped sections of diagnostic info, stacked vertically. The grid
   auto-fits at minmax(280px, 1fr) so on a wide screen they sit side-by-side and
   on narrow ones they stack. */
function renderDiagnostics(d) {
    function esc(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    function pctBar(pct) {
        var cls = pct >= 90 ? 'crit' : pct >= 75 ? 'high' : '';
        return '<span class="diag-bar"><span class="fill ' + cls + '" style="width:' + Math.min(100, pct) + '%"></span></span>';
    }
    function row(k, v, cls) {
        if (v == null || v === '') return '';
        return '<div class="diag-row"><span class="k">' + esc(k) + '</span><span class="v ' + (cls||'') + '">' + v + '</span></div>';
    }
    var sections = [];
    /* --- Hub --- */
    var hub = '';
    hub += row('Version',    'v' + esc(d.version));
    hub += row('Python',     esc(d.python));
    hub += row('Bind',       esc(d.bind) + (d.port ? ':' + esc(d.port) : ''));
    hub += row('Uptime',     esc(d.uptime || '?'));
    hub += row('Modules',    esc(d.module_count) + ' loaded');
    if (d.modules && d.modules.length) {
        var names = d.modules.map(function(m){ return m.name + ' v' + (m.version || '1.0'); }).join(', ');
        hub += row('Loaded',  esc(names));
    }
    sections.push({ title: 'Hub', body: hub });

    /* --- System --- */
    var sys = '';
    sys += row('OS',         esc(d.platform) + ' ' + esc(d.platform_release || ''));
    sys += row('Hostname',   esc(d.hostname));
    sys += row('Arch',       esc(d.architecture));
    sys += row('CPU',        esc(d.cpu) + (d.cpu_cores ? ' (' + esc(d.cpu_cores) + ' cores)' : ''));
    sys += row('GPU',        esc(d.gpu));
    var ramStr = esc(d.ram_used || '') + (d.ram_used ? ' / ' : '') + esc(d.ram || '?');
    if (d.ram_pct != null) ramStr += pctBar(d.ram_pct);
    sys += row('RAM',        ramStr);
    var stoStr = esc(d.storage_used || '') + (d.storage_used ? ' / ' : '') + esc(d.storage_total || '?');
    if (d.storage_pct != null) stoStr += pctBar(d.storage_pct);
    sys += row('Storage',    stoStr);
    if (d.storage_path) sys += row('Hub at', esc(d.storage_path));
    sections.push({ title: 'System', body: sys });

    /* --- Data --- */
    var data = '';
    data += row('Images indexed',    esc(d.image_count != null ? d.image_count : '?'));
    data += row('Folders',           esc(d.folder_count));
    data += row('With metadata',     esc(d.images_with_meta));
    data += row('Favorites',         esc(d.favorites_count));
    data += row('Library cards',     esc(d.library_cards));
    data += row('Attachments',       d.attachments_count != null ? (esc(d.attachments_count) + ' files, ' + esc(d.attachments_size)) : '');
    data += row('Database',          esc(d.db_size || ''));
    data += row('Thumbnails',        esc(d.thumbs_size || ''));
    sections.push({ title: 'Data', body: data });

    /* --- Resources --- */
    /* Shows what the hub picked up from resources/ and whether a Settings
       override is overriding the bundled default, so a missing or overridden
       asset is visible at a glance. */
    var res = '';
    if (d.resources && d.resources.length) {
        d.resources.forEach(function(r) {
            var tag, cls;
            if (r.source === 'override') { tag = 'override'; cls = 'ok'; }
            else if (r.source === 'resources') { tag = 'resources'; cls = 'ok'; }
            else { tag = 'missing'; cls = 'miss'; }
            var mark = r.present ? '&#x2713; ' : '&#x2717; ';
            var val = mark + tag + (r.size ? ' · ' + esc(r.size) : '');
            res += '<div class="diag-row" title="' + esc(r.path) + '">' +
                   '<span class="k">' + esc(r.label) + '</span>' +
                   '<span class="v ' + cls + '">' + val + '</span></div>';
        });
    }
    sections.push({ title: 'Resources', body: res });

    /* --- Dependencies --- */
    var deps = '';
    if (d.dependencies) {
        var keys = Object.keys(d.dependencies);
        keys.forEach(function(name) {
            var status = d.dependencies[name];
            deps += row(name, status === 'ok' ? '&#x2713; installed' : 'missing',
                              status === 'ok' ? 'ok' : 'miss');
        });
    }
    sections.push({ title: 'Optional deps', body: deps });

    /* Render */
    var html = sections.map(function(s) {
        return '<div class="diag-section">' +
               '<div class="diag-section-title">' + esc(s.title) + '</div>' +
               s.body + '</div>';
    }).join('');
    document.getElementById('diagSections').innerHTML = html;
}

/* Build a plain-text dump of the diagnostic info, suitable for pasting into a
   bug report. Strips HTML, keeps the grouping. */
function copyDiagnostics() {
    var d = _lastSysInfo;
    if (!d) return;
    var lines = [];
    lines.push('=== CyberHub diagnostics ===');
    lines.push('');
    lines.push('# Hub');
    lines.push('  Version:     v' + (d.version || '?'));
    lines.push('  Python:      ' + (d.python || '?'));
    lines.push('  Bind:        ' + (d.bind || '?') + ':' + (d.port || '?'));
    lines.push('  Uptime:      ' + (d.uptime || '?'));
    lines.push('  Modules:     ' + (d.module_count || 0) + ' loaded');
    if (d.modules && d.modules.length)
        lines.push('               ' + d.modules.map(function(m){return m.name + ' v' + (m.version || '1.0');}).join(', '));
    lines.push('');
    lines.push('# System');
    lines.push('  OS:          ' + (d.platform || '?') + ' ' + (d.platform_release || ''));
    lines.push('  Hostname:    ' + (d.hostname || '?'));
    lines.push('  Arch:        ' + (d.architecture || '?'));
    lines.push('  CPU:         ' + (d.cpu || '?') + (d.cpu_cores ? ' (' + d.cpu_cores + ' cores)' : ''));
    lines.push('  GPU:         ' + (d.gpu || '?'));
    var ramLine = '  RAM:         ' + (d.ram || '?');
    if (d.ram_used) ramLine += ' (' + d.ram_used + ' used, ' + d.ram_pct + '%)';
    lines.push(ramLine);
    var stoLine = '  Storage:     ' + (d.storage_total || '?');
    if (d.storage_used) stoLine += ' (' + d.storage_used + ' used, ' + d.storage_pct + '%)';
    lines.push(stoLine);
    if (d.storage_path) lines.push('  Hub at:      ' + d.storage_path);
    lines.push('');
    lines.push('# Data');
    lines.push('  Images:      ' + (d.image_count != null ? d.image_count : '?'));
    lines.push('  Folders:     ' + (d.folder_count != null ? d.folder_count : '?'));
    lines.push('  With meta:   ' + (d.images_with_meta != null ? d.images_with_meta : '?'));
    lines.push('  Favorites:   ' + (d.favorites_count != null ? d.favorites_count : '?'));
    lines.push('  Library:     ' + (d.library_cards != null ? d.library_cards + ' cards' : '?'));
    if (d.attachments_count != null)
        lines.push('  Attachments: ' + d.attachments_count + ' files, ' + (d.attachments_size || ''));
    if (d.db_size)     lines.push('  Database:    ' + d.db_size);
    if (d.thumbs_size) lines.push('  Thumbnails:  ' + d.thumbs_size);
    lines.push('');
    lines.push('# Resources (loaded from resources/)');
    if (d.resources && d.resources.length) {
        d.resources.forEach(function(r) {
            var state = (r.present ? r.source : 'MISSING');
            var line = '  ' + (r.label + ':').padEnd(28) + state;
            if (r.size) line += ' (' + r.size + ')';
            lines.push(line);
            if (r.path) lines.push('  ' + ''.padEnd(28) + r.path);
        });
    }
    lines.push('');
    lines.push('# Optional dependencies');
    if (d.dependencies) {
        Object.keys(d.dependencies).forEach(function(n) {
            lines.push('  ' + n.padEnd(13) + d.dependencies[n]);
        });
    }
    var txt = lines.join('\n');
    var btn = document.getElementById('diagCopyBtn');
    /* navigator.clipboard requires a secure context (https or localhost). The hub
       runs on localhost, so we expect it to work; fall back to a textarea trick
       if it doesn't. */
    var done = function() {
        btn.textContent = '\u2713 Copied';
        btn.classList.add('copied');
        setTimeout(function(){ btn.textContent = '\uD83D\uDCCB Copy as text'; btn.classList.remove('copied'); }, 1800);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done).catch(function(){ fallbackCopy(txt); done(); });
    } else {
        fallbackCopy(txt); done();
    }
}
function fallbackCopy(txt) {
    var ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
}
/* No auto-load — toggleDiagnostics() fetches on first expand. */

loadAll();
</script>
"""
