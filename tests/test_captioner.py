import unittest

from modules.captioner import CaptionerModule, PAGE_BODY


class CaptionerTests(unittest.TestCase):
    def test_module_version(self):
        self.assertEqual(CaptionerModule.version, "1.5")

    def test_trigger_prefix_always_uses_comma_and_space(self):
        normalize = CaptionerModule._ensure_trigger_prefix
        expected = "druuna_style, An adult woman stands in a field."

        self.assertEqual(normalize("druuna_style: An adult woman stands in a field.", "druuna_style"), expected)
        self.assertEqual(normalize("druuna_style An adult woman stands in a field.", "druuna_style"), expected)
        self.assertEqual(normalize("druuna_style, An adult woman stands in a field.", "druuna_style"), expected)
        self.assertEqual(normalize("An adult woman stands in a field.", "druuna_style"), expected)

    def test_krea_2_presets_and_batch_controls_are_available(self):
        self.assertIn("built:krea2_character", PAGE_BODY)
        self.assertIn("built:krea2_style", PAGE_BODY)
        self.assertIn('id="stopAllBtn"', PAGE_BODY)
        self.assertIn("function stopAll()", PAGE_BODY)
        self.assertIn('id="runUncaptionedBtn"', PAGE_BODY)
        self.assertIn("function runUncaptioned()", PAGE_BODY)
        self.assertIn("!String(img.caption || '').trim()", PAGE_BODY)
        self.assertIn('class="cap-batch-actions"', PAGE_BODY)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", PAGE_BODY)
        self.assertIn("cache: 'no-store'", PAGE_BODY)
        self.assertIn("type: 'captioner'", PAGE_BODY)
        self.assertIn("tag=captioner-preset", PAGE_BODY)
        self.assertIn("const presetMap = new Map()", PAGE_BODY)


if __name__ == "__main__":
    unittest.main()
