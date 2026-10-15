"""Opt-in release checks and persistent, local-only notification results."""

from datetime import datetime
import copy
import json
import math
import os
import threading
import time


class UpdateNotifications:
    INTERVAL = 24 * 60 * 60

    def __init__(self, hub):
        self.hub = hub
        self.path = hub.data_path("update-notifications.json")
        self._lock = threading.RLock()
        self._checking = False
        self._cache = {"schema": 1, "sources": {}}
        try:
            if os.path.getsize(self.path) <= 2 * 1024 * 1024:
                with open(self.path, encoding="utf-8") as source:
                    value = json.load(source)
                if value.get("schema") == 1 and isinstance(value.get("sources"), dict):
                    self._cache["sources"] = {
                        name: source for name, source in value["sources"].items()
                        if name in {"system", "modules"} and isinstance(source, dict)
                    }
        except (OSError, ValueError, AttributeError):
            pass

    def automatic_enabled(self):
        return self.hub.settings.get_path("updates.automatic_check", False) is True

    @staticmethod
    def _date(value):
        try:
            return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            return ""

    def _due(self, name, now):
        source = self._cache["sources"].get(name, {})
        attempt = source.get("last_attempt", 0) if isinstance(source, dict) else 0
        if not isinstance(attempt, (int, float)) or not math.isfinite(attempt):
            return True
        return not attempt or now < attempt or now - attempt >= self.INTERVAL

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as output:
                json.dump(self._cache, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            print(f"[UPDATES] Could not save notification results: {exc}")

    def remember(self, name, data):
        """Save a successful existing manual check without another network request."""
        if name not in {"system", "modules"}:
            return
        with self._lock:
            now = time.time()
            self._cache["sources"][name] = {
                "last_attempt": now, "checked_at": now, "error": "", "data": copy.deepcopy(data),
            }
            self._save()

    def restore_catalog(self):
        with self._lock:
            source = self._cache["sources"].get("modules", {})
            catalog = copy.deepcopy(source.get("data")) if isinstance(source, dict) else None
        manager = self.hub.registry.get("module_manager")
        if manager and isinstance(catalog, dict):
            try:
                # Revalidate persisted catalog data before exposing install actions.
                validated = manager._validate_catalog(catalog)
                validated["checked_at"] = catalog.get("checked_at", "")
                validated["release_url"] = catalog.get("release_url", "")
                manager._remember_catalog(validated)
            except (ValueError, TypeError, KeyError):
                pass

    def start(self, *, force=False):
        """Start one background check; only explicit manual checks bypass opt-in/cadence."""
        if not force and not self.automatic_enabled():
            return False
        with self._lock:
            if self._checking:
                return False
            names = [name for name in ("system", "modules") if force or self._due(name, time.time())]
            if not names:
                return False
            self._checking = True
        threading.Thread(target=self._check, args=(names, force), daemon=True,
                         name="cyberhub-update-check").start()
        return True

    def _check(self, names, force):
        try:
            for name in names:
                if not force and not self.automatic_enabled():
                    break
                with self._lock:
                    previous = self._cache["sources"].get(name, {})
                    if not isinstance(previous, dict):
                        previous = {}
                    self._cache["sources"][name] = {**previous, "last_attempt": time.time()}
                    self._save()
                try:
                    if name == "system":
                        settings = self.hub.registry.get("settings")
                        release = settings._github_api_json("/releases/latest")
                        data = settings._release_update_info(release)
                    else:
                        manager = self.hub.registry.get("module_manager")
                        data = manager._fetch_catalog()
                        data["checked_at"] = self._date(time.time())
                        manager._remember_catalog(data)
                    self.remember(name, data)
                except Exception as exc:
                    with self._lock:
                        self._cache["sources"][name]["error"] = str(exc)[:400]
                        self._save()
        finally:
            with self._lock:
                self._checking = False

    def snapshot(self):
        """Read cached metadata and compare local versions; never contact GitHub."""
        with self._lock:
            sources = copy.deepcopy(self._cache["sources"])
            checking = self._checking
        settings = self.hub.registry.get("settings")
        manager = self.hub.registry.get("module_manager")
        available = []
        system = sources.get("system", {}).get("data", {})
        if settings and isinstance(system, dict):
            pending = (settings._github_update_status.get("result") or {}).get("package", {})
            packages = system.get("packages", [])
            for package in packages if isinstance(packages, list) else []:
                if not isinstance(package, dict) or package.get("type") != "hub":
                    continue
                try:
                    state = settings._package_state(package)
                    if (pending.get("type") == "hub" and pending.get("version") == package.get("version")
                            and settings._github_update_status.get("stage") == "done"):
                        continue
                    if state["update_available"]:
                        available.append({"type": "hub", "id": "cyberhub", "name": "CyberHub",
                                          "version": state["version"], "current_version": state["current_version"],
                                          "compatible": state["compatible"], "minimum_hub_version": state.get("minimum_hub_version", "")})
                except (ValueError, KeyError, TypeError):
                    continue
        catalog = sources.get("modules", {}).get("data", {})
        if manager and isinstance(catalog, dict):
            installed = {item["id"]: item for item in manager._installed()}
            try:
                items = manager._catalog_response(catalog)["modules"]
            except (ValueError, KeyError, TypeError):
                items = []
            for item in items:
                current = installed.get(item["id"], {})
                if not item["update_available"] or (item["channel"] == "beta" and current.get("channel") != "beta"):
                    continue
                available.append({"type": "module", "id": item["id"], "name": item["name"],
                                  "version": item["version"], "current_version": item["installed_version"],
                                  "compatible": item["compatible"], "minimum_hub_version": item["minimum_hub_version"]})
        dates = {name: self._date(source.get("checked_at", 0)) if source.get("checked_at") else ""
                 for name, source in sources.items() if isinstance(source, dict)}
        errors = {name: source.get("error", "") for name, source in sources.items()
                  if isinstance(source, dict) and source.get("error")}
        return {"automatic_check": self.automatic_enabled(), "checking": checking,
                "count": len(available), "updates": available, "checked_at": dates, "errors": errors}
