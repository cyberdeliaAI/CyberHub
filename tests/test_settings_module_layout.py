import unittest

from modules.settings import SETTINGS_BODY, SettingsModule


class SettingsModuleLayoutTests(unittest.TestCase):
    def test_module_actions_put_toggle_before_settings_button(self):
        toggle_slot = SETTINGS_BODY.index('<div class="module-toggle-slot">')
        settings_slot = SETTINGS_BODY.index('<div class="module-settings-slot">')

        self.assertLess(toggle_slot, settings_slot)
        self.assertIn("querySelectorAll('.chevron[data-acc]')", SETTINGS_BODY)
        self.assertNotIn("querySelectorAll('.mc-head[data-acc]')", SETTINGS_BODY)

    def test_settings_version_was_bumped_for_layout_change(self):
        self.assertEqual(SettingsModule.version, "1.4.1")


if __name__ == "__main__":
    unittest.main()
