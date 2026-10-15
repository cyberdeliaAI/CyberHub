import importlib.util
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from modules.settings import SettingsModule


spec = importlib.util.spec_from_file_location(
    'full_bundle_builder', Path(__file__).resolve().parents[1] / 'tools/build_full_bundle.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class FullBundleTests(unittest.TestCase):
    def test_fresh_install_flag_refuses_import_before_replacing_files(self):
        payload = BytesIO()
        manifest = {'schema': 2, 'product': 'CyberHub', 'type': 'hub', 'id': 'cyberhub',
                    'version': '1.5.0', 'fresh_install_only': True}
        with zipfile.ZipFile(payload, 'w') as zf:
            zf.writestr('resources/cyberhub-package.json', json.dumps(manifest))
            zf.writestr('hub.py', 'new_source = True\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'hub.py'
            target.write_text('original_source = True\n')
            with patch('modules.settings.__file__', str(root / 'modules/settings/__init__.py')):
                with self.assertRaisesRegex(ValueError, 'fresh installation'):
                    SettingsModule(SimpleNamespace())._install_import_zip(payload.getvalue(), 'bundle.zip')
            self.assertEqual(target.read_text(), 'original_source = True\n')

    def test_bundle_root_marker_is_rejected_by_the_legacy_path_planner(self):
        payload = BytesIO()
        with zipfile.ZipFile(payload, 'w') as zf:
            zf.writestr('CyberHub-Full-Bundle/hub.py', 'pass\n')
            zf.writestr('CyberHub-Full-Bundle/FULL-BUNDLE.json', '{}')
        with zipfile.ZipFile(BytesIO(payload.getvalue())) as zf:
            with self.assertRaisesRegex(ValueError, 'FULL-BUNDLE.json'):
                SettingsModule(SimpleNamespace())._plan_import_members(zf, 'bundle.zip')

    def test_wrapped_bundle_has_a_clear_fresh_install_rejection(self):
        payload = BytesIO()
        with zipfile.ZipFile(payload, 'w') as zf:
            zf.writestr('CyberHub-Full-Bundle/hub.py', 'pass\n')
            zf.writestr('CyberHub-Full-Bundle/FULL-BUNDLE.json', '{}')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('modules.settings.__file__', str(root / 'modules/settings/__init__.py')):
                with self.assertRaisesRegex(ValueError, 'fresh installation'):
                    SettingsModule(SimpleNamespace())._install_import_zip(payload.getvalue(), 'bundle.zip')
            self.assertFalse((root / 'hub.py').exists())

    def test_provenance_mismatch_is_rejected(self):
        files = {'resources/provenance/danbooru-autotagger.json': json.dumps({
            'model': {'path': 'model', 'sha256': builder.digest(b'expected')},
            'labels': {'path': 'labels', 'sha256': builder.digest(b'labels')}}).encode(),
            'resources/provenance/prompting-guides.json': b'{"files": []}',
            'model': b'changed', 'labels': b'labels'}
        with self.assertRaisesRegex(ValueError, 'Provenance hash mismatch'):
            builder.verify_provenance(files)
        files['model'] = b'expected'
        builder.verify_provenance(files)

    def test_private_state_and_escaping_paths_are_rejected(self):
        for name in ('../settings.json', '/tmp/source.py', 'C:/source.py',
                     'modules/gallery/../../source.py', 'modules\\gallery\\source.py',
                     'settings.json', 'resources/library/cyberdelia.db',
                     'resources/library/cyberdelia.db-wal',
                     'modules/gallery/__pycache__/source.pyc', '.venv/bin/python',
                     'resources/update-notifications.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.safe_member(name)

    def test_shared_resources_cannot_silently_overwrite_different_bytes(self):
        files, origins = {}, {}
        builder.add_file(files, origins, 'resources/shared.json', b'first', 'Core')
        builder.add_file(files, origins, 'resources/shared.json', b'first', 'Gallery')
        with self.assertRaisesRegex(ValueError, 'Conflicting bundle file'):
            builder.add_file(files, origins, 'resources/shared.json', b'different', 'Other module')
        self.assertEqual(files['resources/shared.json'], b'first')

    def test_archive_name_and_size_are_insufficient_without_matching_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / '_build/sample.zip'
            path.parent.mkdir()
            path.write_bytes(b'not the expected artifact')
            module = {'id': 'sample', 'version': '1.0', 'repository': 'cyberdeliaAI/Sample',
                      'asset': path.name, 'size': path.stat().st_size, 'sha256': '0' * 64}
            with self.assertRaisesRegex(ValueError, 'No locally verified archive'):
                builder.find_archive(root, module)
            module['sha256'] = builder.digest(path.read_bytes())
            self.assertEqual(builder.find_archive(root, module), path)


if __name__ == '__main__':
    unittest.main()
