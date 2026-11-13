"""The positional CLI image folder must never rewrite or shrink saved folders.

`python hub.py <folder>` used to save `[folder]` as the only Gallery folder.
That made every saved folder "unconfigured", so Gallery removed their
favorites, collections and tags, and the change stayed after the session.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from core.server import Settings
from hub import gallery_session_folders


class CliGalleryFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.saved = [str(self.base / "Photos"), str(self.base / "Archive")]
        self.extra = self.base / "Session"
        for folder in self.saved + [str(self.extra)]:
            Path(folder).mkdir()
        self.path = self.base / "settings.json"
        self.write_settings({"modules": {"gallery": {"folders": self.saved}}})

    def tearDown(self):
        self.temp.cleanup()

    def write_settings(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def saved_on_disk(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["modules"]["gallery"]

    def apply(self, settings, folder):
        abs_folder, folders = gallery_session_folders(settings, folder)
        settings.set_session_value("modules.gallery.folders", folders)
        return abs_folder, folders

    def test_cli_folder_is_added_after_saved_folders_for_this_session(self):
        settings = Settings(str(self.path))
        abs_folder, folders = self.apply(settings, str(self.extra))
        self.assertEqual(abs_folder, str(self.extra))
        self.assertEqual(folders, self.saved + [str(self.extra)])
        self.assertEqual(settings.get_module_setting("gallery", "folders", []), folders)
        self.assertEqual(settings.get_path("modules.gallery.folders"), folders)
        self.assertEqual(self.saved_on_disk()["folders"], self.saved)

    def test_later_save_of_another_setting_does_not_persist_cli_folder(self):
        settings = Settings(str(self.path))
        self.apply(settings, str(self.extra))
        settings.set("theme", "light")
        settings.set_module_setting("gallery", "per_page", 100)
        on_disk = self.saved_on_disk()
        self.assertEqual(on_disk["folders"], self.saved)
        self.assertEqual(on_disk["per_page"], 100)
        self.assertEqual(Settings(str(self.path)).get_module_setting("gallery", "folders"), self.saved)

    def test_saved_folder_given_on_cli_is_not_duplicated(self):
        settings = Settings(str(self.path))
        _, folders = self.apply(settings, self.saved[0] + os.sep)
        self.assertEqual(folders, self.saved)

    def test_legacy_single_folder_stays_configured(self):
        self.write_settings({"modules": {"gallery": {"folder": self.saved[0]}}})
        settings = Settings(str(self.path))
        _, folders = self.apply(settings, str(self.extra))
        self.assertEqual(folders, [self.saved[0], str(self.extra)])

    def test_first_run_without_saved_folders_uses_cli_folder(self):
        self.write_settings({})
        settings = Settings(str(self.path))
        _, folders = self.apply(settings, str(self.extra))
        self.assertEqual(folders, [str(self.extra)])
        self.assertNotIn("gallery", json.loads(self.path.read_text(encoding="utf-8")).get("modules", {}))

    def test_missing_cli_folder_raises_without_changing_settings(self):
        settings = Settings(str(self.path))
        with self.assertRaises(FileNotFoundError):
            gallery_session_folders(settings, str(self.base / "missing"))
        self.assertEqual(settings.get_module_setting("gallery", "folders"), self.saved)
        self.assertEqual(self.saved_on_disk()["folders"], self.saved)

    def test_session_value_does_not_leak_into_other_keys(self):
        settings = Settings(str(self.path))
        settings.set_session_value("modules.gallery.folders", ["x"])
        self.assertIsNone(settings.get_module_setting("viewer", "folders"))
        self.assertEqual(settings.get_module("gallery")["folders"], self.saved)


if __name__ == "__main__":
    unittest.main()
