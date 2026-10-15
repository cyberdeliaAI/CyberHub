import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
from build_core_packages import write_archive
from build_module_notice_updates import without_module_version


class ReleasePackageTests(unittest.TestCase):
    def test_notice_patch_comparison_ignores_only_the_module_version(self):
        baseline = "class Example(Module):\n    name = 'Example'\n    def key(self): return 'example'\n"
        versioned = baseline.replace("    name", "    version = '1.0.1'\n    name")
        self.assertEqual(without_module_version(baseline), without_module_version(versioned))
        changed_route = versioned.replace("return 'example'", "return 'renamed'")
        self.assertNotEqual(without_module_version(baseline), without_module_version(changed_route))

    def test_private_state_is_rejected_before_creating_an_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'release.zip'
            for name in ['settings.json', '../hub.py', 'resources/library/user.db', '.venv/python']:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    write_archive(target, {name: b'private state'}, '2026-10-15')
                self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
