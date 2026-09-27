import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_connection import AIConnection, normalize_api_url
from core.server import Settings
from modules.settings import SettingsModule


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Settings(str(Path(self.temp.name) / 'settings.json'))
        self.service = AIConnection(self.store)

    def central(self, **extra):
        return self.service.save({'api_url':'shared.test:8000/v1/', 'model':'vendor/vision', **extra})

    def test_normalizes_once_and_preserves_proxy_path(self):
        for address in ['http://host:8000/v1/', 'host:8000', 'http://host:8000']:
            self.assertEqual(normalize_api_url(address), 'http://host:8000/v1')
        self.assertEqual(normalize_api_url('https://host/proxy/v1/'), 'https://host/proxy/v1')
        for address in ['', 'ftp://host', 'http://user:secret@host', 'http://host?token=x', 'http://host#x', 'http://host:wrong']:
            with self.assertRaises(ValueError): normalize_api_url(address)

    def test_existing_connection_is_preserved_and_new_module_inherits(self):
        self.store.set_module_settings('captioner', {'api_url':'http://old/v1', 'model':'old', 'temperature':0.7})
        self.central()
        self.assertEqual(self.service.resolve('captioner')['connection_mode'], 'custom')
        cfg = self.service.resolve('prompt-engineer')
        self.assertEqual(cfg['connection_mode'], 'shared')
        self.assertEqual(cfg['model'], 'vendor/vision')
        self.assertEqual(self.store.get_module('captioner')['temperature'], 0.7)

    def test_shared_model_override_does_not_destroy_custom_backup(self):
        self.store.set_module_settings('captioner', {'api_url':'http://old/v1', 'model':'old'})
        self.central()
        values = self.service.module_values({'connection_mode':'shared', 'shared_model':'vendor/caption', 'api_url':'http://ignored'})
        self.store.set_module_settings('captioner', values)
        cfg = self.service.resolve('captioner')
        self.assertEqual(cfg['model'], 'vendor/caption')
        self.assertEqual(cfg['custom']['api_url'], 'http://old/v1')
        self.assertEqual(cfg['custom']['model'], 'old')
        self.store.set_module_setting('captioner', 'shared_model', '')
        self.central(model='vendor/new', api_url='other.test')
        self.assertEqual(self.service.resolve('captioner')['model'], 'vendor/new')
        self.assertEqual(self.service.resolve('captioner')['api_url'], 'http://other.test/v1')

    def test_missing_or_invalid_central_never_redirects_explicit_shared(self):
        self.store.set_module_setting('captioner', 'connection_mode', 'shared')
        self.assertTrue(self.service.resolve('captioner')['connection_error'])
        with self.assertRaises(ValueError): self.service.module_values({'connection_mode':'shared'})
        self.store.set('ai_connection', {'api_url':'ftp://broken'})
        self.assertTrue(self.service.resolve('captioner')['connection_error'])
        self.store.set_module_settings('prompt-engineer', {'api_url':'http://own/v1', 'connection_mode':'custom'})
        self.assertEqual(self.service.resolve('prompt-engineer')['api_url'], 'http://own/v1')

    def test_global_and_module_settings_survive_reload(self):
        self.central()
        self.store.set_module_settings('captioner', {'connection_mode':'shared', 'shared_model':'custom'})
        old_compatible_store = Settings(self.store.path)
        old_compatible_store.set('theme', 'light')
        restored = AIConnection(Settings(self.store.path)).resolve('captioner')
        self.assertEqual(restored['model'], 'custom')
        self.assertEqual(restored['api_url'], 'http://shared.test:8000/v1')

    def test_invalid_save_leaves_connection_unchanged(self):
        before = self.central()
        handler = Mock()
        handler.read_body_json.return_value = {'api_url':'ftp://host'}
        module = SettingsModule(SimpleNamespace(ai_connection=self.service))
        module._api_ai_save(handler, 0, 'application/json')
        self.assertEqual(handler.respond_json.call_args.kwargs['status'], 400)
        self.assertEqual(self.service.shared(), before)

    def test_detection_does_not_save_draft(self):
        self.central()
        before = dict(self.store.data)
        handler = Mock()
        handler.read_body_json.return_value = {'api_url':'http://draft', 'transport':'hub'}
        self.service.models = Mock(return_value={'models':['vendor/model']})
        SettingsModule(SimpleNamespace(ai_connection=self.service))._api_ai_models(handler, 0, '')
        self.assertEqual(self.store.data, before)
        self.service.models.assert_called_once()


if __name__ == '__main__': unittest.main()
