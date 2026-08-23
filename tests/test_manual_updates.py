import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import build_release
from modules.settings import (
    SETTINGS_BODY,
    SettingsModule,
    _github_transport_url_allowed,
    _is_newer_version,
)


class ManualUpdateTests(unittest.TestCase):
    def setUp(self):
        self.module = SettingsModule(SimpleNamespace(VERSION="1.2.6"))

    def test_version_comparison_handles_stable_and_prerelease_versions(self):
        self.assertTrue(_is_newer_version("1.2.7", "1.2.6"))
        self.assertTrue(_is_newer_version("1.2.6", "1.2.6-rc1"))
        self.assertFalse(_is_newer_version("1.2.6", "1.2.6"))
        self.assertFalse(_is_newer_version("1.2.5", "1.2.6"))

    def test_update_transport_is_confined_to_github_https_hosts(self):
        self.assertTrue(_github_transport_url_allowed("https://api.github.com/repos/test/test"))
        self.assertTrue(_github_transport_url_allowed("https://release-assets.githubusercontent.com/file"))
        self.assertFalse(_github_transport_url_allowed("http://github.com/file"))
        self.assertFalse(_github_transport_url_allowed("https://github.com.example.test/file"))
        self.assertFalse(_github_transport_url_allowed("https://user:secret@github.com/file"))

    def test_release_asset_must_belong_to_official_repository(self):
        good = "https://github.com/cyberdeliaAI/CyberHub/releases/download/v1.3/update.zip"
        self.module._validate_release_asset_url(good)
        with self.assertRaisesRegex(ValueError, "official CyberHub repository"):
            self.module._validate_release_asset_url(
                "https://github.com/someone/CyberHub/releases/download/v1.3/update.zip"
            )

    def test_catalog_requires_official_repository_and_sha256(self):
        package = {
            "type": "hub",
            "id": "cyberhub",
            "name": "CyberHub",
            "version": "1.3",
            "minimum_hub_version": "",
            "asset": "cyberhub_v1.3.zip",
            "sha256": "a" * 64,
        }
        catalog = {
            "schema": 1,
            "product": "CyberHub",
            "repository": "cyberdeliaAI/CyberHub",
            "release_version": "1.3",
            "packages": [package],
        }
        cleaned = self.module._validate_update_catalog(catalog)
        self.assertEqual(cleaned["packages"][0]["sha256"], "a" * 64)

        other = dict(catalog, repository="someone/else")
        with self.assertRaisesRegex(ValueError, "another repository"):
            self.module._validate_update_catalog(other)
        bad_package = dict(package, sha256="missing")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.module._validate_update_catalog(dict(catalog, packages=[bad_package]))

    def test_module_package_cannot_replace_core_or_another_module(self):
        manifest = {"type": "module", "id": "captioner"}
        own_files = [
            ("modules/captioner/__init__.py", None),
            ("modules/captioner/view.js", None),
            ("resources/captioner/readme.txt", None),
        ]
        self.module._validate_online_package_scope(manifest, own_files)

        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.module._validate_online_package_scope(
                manifest,
                own_files + [("core/server.py", None)],
            )
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.module._validate_online_package_scope(
                manifest,
                own_files + [("modules/gallery/__init__.py", None)],
            )

    def test_package_manifest_is_read_but_not_installed_as_a_live_file(self):
        manifest = build_release.package_manifest("module", "captioner", "1.2", "1.2.6")
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("resources/cyberhub-package.json", manifest)
            archive.writestr("modules/captioner/__init__.py", "class Captioner: pass\n")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            parsed = self.module._package_manifest_from_zip(archive)
            planned = self.module._plan_import_members(archive, "captioner.zip")
        self.assertEqual(parsed["id"], "captioner")
        self.assertEqual([path for path, _info in planned], ["modules/captioner/__init__.py"])

    def test_settings_page_never_checks_github_during_normal_load(self):
        load_all = SETTINGS_BODY.split("function loadAll()", 1)[1].split(
            "// ─── Manual GitHub updates", 1
        )[0]
        self.assertNotIn("/api/settings/update/check", load_all)
        self.assertIn("onclick=\"checkCyberHubUpdates()\"", SETTINGS_BODY)
        with patch.object(SettingsModule, "_github_api_json") as request:
            SettingsModule(SimpleNamespace(VERSION="1.2.6"))
            request.assert_not_called()

    def test_release_builder_writes_matching_manifest_and_digest(self):
        with tempfile.TemporaryDirectory() as output_dir:
            path = os.path.join(output_dir, "settings.zip")
            manifest = build_release.package_manifest("module", "settings", "1.3", "1.2.6")
            record = build_release.write_zip(
                path,
                build_release.module_files("settings"),
                "1.3",
                manifest=manifest,
            )
            with zipfile.ZipFile(path) as archive:
                packaged = json.loads(archive.read("resources/cyberhub-package.json"))
            with open(path, "rb") as source:
                actual_digest = hashlib.sha256(source.read()).hexdigest()
            self.assertEqual(packaged["type"], "module")
            self.assertEqual(packaged["id"], "settings")
            self.assertEqual(record["sha256"], actual_digest)

    def test_beta_modules_are_separate_from_complete_release(self):
        browser = build_release.module_metadata("civitai_browser")
        upscaler = build_release.module_metadata("upscaler")
        complete_modules = build_release.complete_module_names()
        complete_resources = build_release.complete_release_resources(complete_modules)

        self.assertEqual(browser["version"], "1.1.1-beta")
        self.assertEqual(browser["release_stage"], "beta")
        self.assertEqual(upscaler["version"], "1.1.1-beta")
        self.assertEqual(upscaler["release_stage"], "beta")
        self.assertNotIn("civitai_browser", complete_modules)
        self.assertNotIn("upscaler", complete_modules)
        self.assertIn("captioner", complete_modules)
        self.assertNotIn(
            "resources/upscalers/Real-ESRGAN-x4plus.onnx",
            complete_resources,
        )

    def test_settings_renders_beta_release_stage(self):
        self.assertIn('stage-badge beta', SETTINGS_BODY)


if __name__ == "__main__":
    unittest.main()
