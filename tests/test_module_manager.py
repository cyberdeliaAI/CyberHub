import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.module_store import ModuleStore
from modules.module_manager import ModuleManagerModule
from modules.settings import SettingsModule


class ModuleRestartTests(unittest.TestCase):
    def test_install_marks_restart_pending_and_new_process_clears_it(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Mock(spec=SettingsModule)
            settings._install_import_zip.return_value = {"restart_required": True}
            hub = SimpleNamespace(
                registry=Mock(), data_path=lambda *parts: os.path.join(root, *parts),
            )
            hub.registry.get.return_value = settings
            module = ModuleManagerModule(hub)
            self.assertFalse(module._job["restart_required"])
            item = {"id": "viewer", "name": "Viewer", "version": "1.1",
                    "download_url": "https://github.com/example/viewer.zip",
                    "size": 100, "sha256": "a" * 64, "asset": "viewer.zip"}
            module._install_worker(item)
            self.assertTrue(module._job["restart_required"])
            self.assertEqual(module._job["stage"], "done")
            module._set_job(running=True, stage="starting")
            self.assertTrue(module._job["restart_required"])
            settings._read_github_url.side_effect = ValueError("Download failed")
            module._install_worker(item)
            self.assertEqual(module._job["stage"], "error")
            self.assertTrue(module._job["restart_required"])
            self.assertFalse(ModuleManagerModule(hub)._job["restart_required"])

    def test_remove_requires_restart_only_on_success(self):
        hub = SimpleNamespace(module_store=Mock())
        handler = Mock()
        handler._is_loopback.return_value = True
        handler.read_body_json.return_value = {"module": "viewer"}
        module = ModuleManagerModule(hub)
        hub.module_store.uninstall.side_effect = ValueError("Cannot remove")
        module._api_remove(handler, 0, "application/json")
        self.assertFalse(module._job["restart_required"])
        hub.module_store.uninstall.side_effect = None
        hub.module_store.uninstall.return_value = {"removed": 1}
        module._api_remove(handler, 0, "application/json")
        self.assertTrue(module._job["restart_required"])

    def test_remove_is_blocked_while_installing(self):
        hub = SimpleNamespace(module_store=Mock())
        handler = Mock()
        handler._is_loopback.return_value = True
        module = ModuleManagerModule(hub)
        module._set_job(running=True)
        module._api_remove(handler, 0, "application/json")
        hub.module_store.uninstall.assert_not_called()
        self.assertEqual(handler.respond_json.call_args.kwargs["status"], 409)


class ModuleStoreTests(unittest.TestCase):
    def test_managed_module_removal_uses_owned_files_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as root:
            module_file = os.path.join(root, "modules", "sample", "__init__.py")
            resource_file = os.path.join(root, "resources", "sample", "data.txt")
            os.makedirs(os.path.dirname(module_file))
            os.makedirs(os.path.dirname(resource_file))
            with open(module_file, "w", encoding="utf-8") as output:
                output.write("value = 1\n")
            with open(resource_file, "w", encoding="utf-8") as output:
                output.write("data\n")
            store = ModuleStore(root, root)
            store.record_install(
                {"type": "module", "id": "sample", "name": "Sample", "version": "1.0"},
                ["modules/sample/__init__.py", "resources/sample/data.txt"],
            )
            result = store.uninstall("sample")
            self.assertEqual(result["removed"], 2)
            self.assertFalse(os.path.exists(module_file))
            self.assertFalse(os.path.exists(resource_file))
            self.assertTrue(os.path.isfile(os.path.join(
                result["backup"], "modules", "sample", "__init__.py",
            )))
            self.assertIsNone(store.get("sample"))

    def test_protected_modules_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as root:
            store = ModuleStore(root, root)
            store.adopt("settings", name="Settings", version="1.0")
            with self.assertRaisesRegex(ValueError, "protected"):
                store.uninstall("settings")


class ModuleCatalogTests(unittest.TestCase):
    def manager(self, installed=None):
        module = ModuleManagerModule(SimpleNamespace(VERSION="1.4.1"))
        module._installed = Mock(return_value=installed or [])
        module._fetch_catalog = Mock(return_value=self.valid_catalog())
        return module

    def test_only_newer_installed_modules_count_as_updates(self):
        for installed, expected in [
            ([], False),
            ([{"id": "gallery", "version": "1.2.12"}], True),
            ([{"id": "gallery", "version": "1.2.13"}], False),
            ([{"id": "gallery", "version": "1.2.14"}], False),
            ([{"id": "gallery", "version": "1.2.12", "protected": True}], False),
        ]:
            with self.subTest(installed=installed):
                module = self.manager(installed)
                item = module._catalog_response(self.valid_catalog())["modules"][0]
                self.assertEqual(bool(item["update_available"]), expected)

    def test_blocked_updates_remain_visible_with_requirements(self):
        module = self.manager([{"id": "gallery", "version": "1.0"}])
        catalog = self.valid_catalog()
        catalog["modules"][0].update(minimum_hub_version="9.0", dependencies=["viewer"])
        item = module._catalog_response(catalog)["modules"][0]
        self.assertTrue(item["update_available"])
        self.assertFalse(item["compatible"])
        self.assertEqual(item["missing_dependencies"], ["viewer"])

    def test_opening_catalog_is_local_and_refresh_is_explicit(self):
        module = self.manager()
        handler = Mock()
        module._api_catalog(handler, {})
        module._fetch_catalog.assert_not_called()
        self.assertEqual(handler.respond_json.call_args.args[0]["checked_at"], "")
        module._api_catalog(handler, {"refresh": ["1"]})
        module._fetch_catalog.assert_called_once()
        checked_at = handler.respond_json.call_args.args[0]["checked_at"]
        self.assertTrue(checked_at)
        module._api_catalog(handler, {})
        module._fetch_catalog.assert_called_once()
        self.assertEqual(handler.respond_json.call_args.args[0]["checked_at"], checked_at)

    def test_cached_results_reflect_installation_and_survive_failed_check(self):
        module = self.manager([{"id": "gallery", "version": "1.2.12"}])
        handler = Mock()
        module._api_catalog(handler, {"refresh": ["1"]})
        checked_at = handler.respond_json.call_args.args[0]["checked_at"]
        self.assertTrue(handler.respond_json.call_args.args[0]["modules"][0]["update_available"])
        module._installed.return_value = [{"id": "gallery", "version": "1.2.13"}]
        module._fetch_catalog.side_effect = ValueError("Offline")
        module._api_catalog(handler, {"refresh": ["1"]})
        self.assertEqual(handler.respond_json.call_args.kwargs["status"], 502)
        module._api_catalog(handler, {})
        data = handler.respond_json.call_args.args[0]
        self.assertFalse(data["modules"][0]["update_available"])
        self.assertEqual(data["checked_at"], checked_at)
        self.assertEqual(module._fetch_catalog.call_count, 2)

    def test_empty_successful_catalog_is_not_treated_as_unchecked(self):
        module = self.manager()
        module._fetch_catalog.return_value = {"modules": []}
        handler = Mock()
        module._api_catalog(handler, {"refresh": ["1"]})
        module._api_catalog(handler, {})
        self.assertTrue(handler.respond_json.call_args.args[0]["checked_at"])
        module._fetch_catalog.assert_called_once()

    def test_installed_package_version_is_used_before_restart(self):
        hub = SimpleNamespace(module_store=Mock(), registry=SimpleNamespace(modules={}))
        hub.module_store.PROTECTED = {"settings", "module_manager"}
        hub.module_store.records.return_value = {
            "gallery": {"managed": True, "version": "1.2.13", "channel": "stable"},
            "settings": {"managed": False, "version": "1.0"},
        }
        module = ModuleManagerModule(hub)
        gallery = type("GalleryModule", (), {"name": "Gallery", "version": "1.2.12", "release_stage": "beta"})
        with patch("modules.module_manager.available_module_classes", return_value=[
            ("gallery", gallery), ("settings", SettingsModule),
        ]):
            installed = {item["id"]: item for item in module._installed()}
        self.assertEqual(installed["gallery"]["version"], "1.2.13")
        self.assertEqual(installed["gallery"]["channel"], "stable")
        self.assertEqual(installed["settings"]["version"], SettingsModule.version)

    def valid_catalog(self):
        return {
            "schema": 1,
            "product": "CyberHub",
            "repository": "cyberdeliaAI/CyberHub-Registry",
            "generated_at": "2026-09-04T00:00:00+00:00",
            "modules": [{
                "id": "gallery",
                "name": "Gallery",
                "summary": "Gallery module",
                "version": "1.2.13",
                "minimum_hub_version": "1.3.0",
                "repository": "cyberdeliaAI/CyberHub-Gallery",
                "publisher_type": "official",
                "channel": "stable",
                "dependencies": [],
                "python_dependencies": ["Pillow>=9.0"],
                "asset": "gallery.zip",
                "download_url": "https://github.com/cyberdeliaAI/CyberHub-Gallery/releases/download/v1.2.13/gallery.zip",
                "sha256": "a" * 64,
                "size": 100,
            }],
        }

    def test_official_catalog_entry_is_accepted(self):
        module = object.__new__(ModuleManagerModule)
        cleaned = module._validate_catalog(self.valid_catalog())
        self.assertEqual(cleaned["modules"][0]["id"], "gallery")

    def test_official_entry_cannot_point_outside_organization(self):
        value = self.valid_catalog()
        value["modules"][0]["repository"] = "someone/CyberHub-Gallery"
        value["modules"][0]["download_url"] = (
            "https://github.com/someone/CyberHub-Gallery/releases/download/v1.2.13/gallery.zip"
        )
        module = object.__new__(ModuleManagerModule)
        with self.assertRaisesRegex(ValueError, "outside"):
            module._validate_catalog(value)

    def test_download_must_match_declared_repository(self):
        value = self.valid_catalog()
        value["modules"][0]["download_url"] = (
            "https://github.com/cyberdeliaAI/Other/releases/download/v1.2.13/gallery.zip"
        )
        module = object.__new__(ModuleManagerModule)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            module._validate_catalog(value)


if __name__ == "__main__":
    unittest.main()
