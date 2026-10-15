from pathlib import Path
from types import SimpleNamespace
import re
import unittest


class LauncherCompatibilityTests(unittest.TestCase):
    def test_onnx_wheel_gap_only_excludes_intel_macos_python314_full_installs(self):
        script = (Path(__file__).resolve().parents[1] / 'start.sh').read_text()
        statement = re.search(r'ok=\$\("\$cmd" -c "([^"]+)"', script).group(1)
        for system, arch, version, needs_onnx, expected in [
            ('Darwin', 'x86_64', (3, 14), 1, '0'),
            ('Darwin', 'x86_64', (3, 13), 1, '1'),
            ('Darwin', 'x86_64', (3, 14), 0, '1'),
            ('Darwin', 'arm64', (3, 14), 1, '1'),
            ('Linux', 'x86_64', (3, 14), 1, '1'),
            ('Windows', 'AMD64', (3, 14), 1, '1'),
            ('Darwin', 'arm64', (3, 15), 1, '0'),
            ('Darwin', 'arm64', (3, 9), 1, '0'),
        ]:
            with self.subTest(system=system, arch=arch, version=version, needs_onnx=needs_onnx):
                result = []
                code = statement.replace('$NEEDS_ONNX', str(needs_onnx)).removeprefix('import sys, platform; ')
                exec(code, {'sys': SimpleNamespace(version_info=version),
                            'platform': SimpleNamespace(system=lambda: system, machine=lambda: arch),
                            'print': result.append})
                self.assertEqual(result, [expected])


if __name__ == '__main__':
    unittest.main()
