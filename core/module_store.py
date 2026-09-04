"""Installed-module ownership and safe removal for CyberHub."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import shutil
import threading


MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ModuleStore:
    """Track installed module versions and the files owned by each package."""

    SCHEMA = 1
    PROTECTED = frozenset({"settings", "module_manager"})

    def __init__(self, install_root, data_dir):
        self.install_root = os.path.realpath(install_root)
        self.data_dir = os.path.realpath(data_dir)
        self.path = os.path.join(self.data_dir, "installed-modules.json")
        self._lock = threading.RLock()
        self._data = self._load()

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _empty(self):
        return {"schema": self.SCHEMA, "product": "CyberHub", "modules": {}}

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as source:
                value = json.load(source)
            if (
                isinstance(value, dict)
                and value.get("schema") == self.SCHEMA
                and value.get("product") == "CyberHub"
                and isinstance(value.get("modules"), dict)
            ):
                return value
        except (OSError, ValueError, TypeError):
            pass
        return self._empty()

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as output:
            json.dump(self._data, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary, self.path)

    @staticmethod
    def _clean_files(files):
        cleaned = []
        seen = set()
        for value in files or []:
            rel = str(value or "").replace("\\", "/").strip("/")
            if not rel or rel in seen or rel.startswith("../") or "/../" in rel:
                continue
            seen.add(rel)
            cleaned.append(rel)
        return sorted(cleaned)

    def records(self):
        with self._lock:
            return json.loads(json.dumps(self._data["modules"]))

    def get(self, module_id):
        with self._lock:
            value = self._data["modules"].get(str(module_id or ""))
            return json.loads(json.dumps(value)) if value else None

    def adopt(self, module_id, *, name, version, channel="stable"):
        """Record an already present module without claiming shared resources."""
        if not MODULE_ID_RE.fullmatch(str(module_id or "")):
            return
        with self._lock:
            if module_id in self._data["modules"]:
                record = self._data["modules"][module_id]
                if record.get("version") != str(version):
                    record["version"] = str(version)
                    record["updated_at"] = self._utc_now()
                    self._save()
                return
            module_root = os.path.join(self.install_root, "modules", module_id)
            files = []
            if os.path.isdir(module_root):
                for root, dirs, filenames in os.walk(module_root):
                    dirs[:] = [item for item in dirs if item != "__pycache__"]
                    for filename in filenames:
                        if filename.endswith((".pyc", ".pyo")):
                            continue
                        absolute = os.path.join(root, filename)
                        files.append(os.path.relpath(absolute, self.install_root))
            now = self._utc_now()
            self._data["modules"][module_id] = {
                "id": module_id,
                "name": str(name or module_id),
                "version": str(version or "1.0"),
                "channel": "beta" if channel == "beta" else "stable",
                "repository": "",
                "publisher_type": "bundled" if module_id in self.PROTECTED else "local",
                "managed": False,
                "protected": module_id in self.PROTECTED,
                "files": self._clean_files(files),
                "installed_at": now,
                "updated_at": now,
            }
            self._save()

    def record_install(self, manifest, files, *, repository="", publisher_type="official"):
        module_id = str((manifest or {}).get("id") or "")
        if (manifest or {}).get("type") != "module" or not MODULE_ID_RE.fullmatch(module_id):
            return
        with self._lock:
            previous = self._data["modules"].get(module_id, {})
            now = self._utc_now()
            owned_files = self._clean_files(list(previous.get("files") or []) + list(files or []))
            self._data["modules"][module_id] = {
                "id": module_id,
                "name": str((manifest or {}).get("name") or previous.get("name") or module_id),
                "version": str((manifest or {}).get("version") or "1.0"),
                "channel": "beta" if (manifest or {}).get("channel") == "beta" else "stable",
                "repository": str(repository or (manifest or {}).get("repository") or ""),
                "publisher_type": str(publisher_type or "official"),
                "managed": True,
                "protected": module_id in self.PROTECTED,
                "files": owned_files,
                "installed_at": previous.get("installed_at") or now,
                "updated_at": now,
            }
            self._save()

    def uninstall(self, module_id):
        module_id = str(module_id or "").strip().lower()
        if not MODULE_ID_RE.fullmatch(module_id):
            raise ValueError("Invalid module identifier.")
        if module_id in self.PROTECTED:
            raise ValueError("This is a protected CyberHub system module.")

        with self._lock:
            record = self._data["modules"].get(module_id)
            if not record:
                raise ValueError("Module is not registered as installed.")
            files = self._clean_files(record.get("files"))
            module_prefix = f"modules/{module_id}/"
            if not record.get("managed"):
                files = [path for path in files if path.startswith(module_prefix)]
            if not files:
                raise ValueError("No safely removable files are registered for this module.")

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_root = os.path.join(
                self.install_root, "updates", "backups", f"uninstall-{module_id}-{stamp}"
            )
            removed = []
            try:
                for rel in files:
                    absolute = os.path.realpath(os.path.join(self.install_root, *rel.split("/")))
                    if not absolute.startswith(self.install_root + os.sep):
                        raise ValueError(f"Refusing to remove a file outside CyberHub: {rel}")
                    if not os.path.isfile(absolute):
                        continue
                    backup = os.path.join(backup_root, *rel.split("/"))
                    os.makedirs(os.path.dirname(backup), exist_ok=True)
                    shutil.copy2(absolute, backup)
                    os.remove(absolute)
                    removed.append(rel)
            except Exception:
                for rel in reversed(removed):
                    source = os.path.join(backup_root, *rel.split("/"))
                    destination = os.path.join(self.install_root, *rel.split("/"))
                    try:
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        shutil.copy2(source, destination)
                    except OSError:
                        pass
                raise

            for rel in sorted(removed, key=lambda value: value.count("/"), reverse=True):
                directory = os.path.dirname(os.path.join(self.install_root, *rel.split("/")))
                while directory.startswith(self.install_root + os.sep):
                    try:
                        os.rmdir(directory)
                    except OSError:
                        break
                    directory = os.path.dirname(directory)
            del self._data["modules"][module_id]
            self._save()
            return {
                "ok": True,
                "module": module_id,
                "removed": len(removed),
                "backup": backup_root if removed else "",
                "restart_required": True,
            }
