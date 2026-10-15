import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import Module, ModuleRegistry, module_key_from_class, module_runtime_key_from_class
from core.server import Settings
from hub import load_modules
from modules.settings import SettingsModule
from modules.module_manager import ModuleManagerModule


IDENTITIES = [('amateur_photo', 'Amateur Photo', 'amateur-photo'),
              ('civitai_grabber', 'Civitai Grabber', 'grabber'),
              ('tools', 'Meta Copy Tool', 'tools'),
              ('prompt_engineer', 'Prompt Engineer', 'prompt-engineer')]


class ModuleEnablementTests(unittest.TestCase):
    def test_key_resolution_preserves_legacy_api_and_skips_constructors(self):
        class DynamicModule(Module):
            name = 'Dynamic Module'
            def __init__(self, hub):
                raise AssertionError('Must not construct a module to inspect its key')
            def key(self):
                return self.initialized_only
        self.assertEqual(module_runtime_key_from_class(DynamicModule), 'dynamic_module')
        for (_, cls), (_, _, key) in zip(self.classes, IDENTITIES):
            self.assertEqual(module_runtime_key_from_class(cls), key)
            self.assertNotEqual(module_key_from_class(cls), key)
        self.assertEqual(self.constructed, [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.constructed = []
        self.classes = []
        for folder, name, key in IDENTITIES:
            def init(instance, hub, key=key):
                self.constructed.append(key)
                Module.__init__(instance, hub)
            cls = type(folder + 'Module', (Module,), {'name': name, 'key': lambda self, key=key: key,
                                                     '__init__': init})
            self.classes.append((folder, cls))
        self.settings = Settings(str(Path(self.temp.name) / 'settings.json'))
        self.hub = SimpleNamespace(settings=self.settings, registry=ModuleRegistry(), module_store=Mock())
        self.hub.module_store.records.return_value = {}
        self.hub.module_store.PROTECTED = {'settings', 'module_manager'}

    def load(self):
        self.hub.registry = ModuleRegistry()
        with patch('hub.available_module_classes', return_value=self.classes):
            load_modules(self.hub)

    def test_runtime_toggle_stops_all_four_modules_without_constructing_them(self):
        api = SettingsModule(self.hub)
        for _, _, key in IDENTITIES:
            handler = Mock()
            handler.read_body_json.return_value = {'module': key, 'enabled': False}
            api._api_module_toggle(handler, 0, 'application/json')
            self.assertTrue(handler.respond_json.call_args.args[0]['ok'])
        self.settings = self.hub.settings = Settings(self.settings.path)
        self.load()
        self.assertEqual(list(self.hub.registry.modules), [])
        self.assertEqual(self.constructed, [])

    def test_runtime_preference_takes_precedence_over_legacy_key(self):
        for (_, cls), (_, _, key) in zip(self.classes, IDENTITIES):
            self.settings.set_module_setting(module_key_from_class(cls), 'enabled', False)
            self.settings.set_module_setting(key, 'enabled', True)
        self.load()
        self.assertEqual(set(self.hub.registry.modules), {key for _, _, key in IDENTITIES})

    def test_legacy_disabled_preference_remains_effective_until_runtime_toggle(self):
        for _, cls in self.classes:
            self.settings.set_module_setting(module_key_from_class(cls), 'enabled', False)
        self.load()
        self.assertEqual(self.constructed, [])
        self.assertEqual(list(self.hub.registry.modules), [])

    def test_settings_inventory_retains_runtime_ids_for_disabled_modules(self):
        self.settings.set_module_setting('grabber', 'enabled', False)
        self.load()
        handler = Mock()
        with patch('modules.settings.available_module_classes', return_value=self.classes):
            SettingsModule(self.hub)._api_modules(handler, {})
        inventory = handler.respond_json.call_args.args[0]
        self.assertEqual({item['key'] for item in inventory}, {key for _, _, key in IDENTITIES})
        grabber = next(item for item in inventory if item['key'] == 'grabber')
        self.assertFalse(grabber['enabled'])
        self.assertFalse(grabber['loaded'])

    def test_manager_reports_loaded_runtime_keys(self):
        self.load()
        with patch('modules.module_manager.available_module_classes', return_value=self.classes):
            inventory = ModuleManagerModule(self.hub)._installed()
        self.assertTrue(all(item['enabled'] for item in inventory))

    def test_system_modules_stay_loaded_even_when_settings_disable_them(self):
        self.classes = [(key, type(key + 'Module', (Module,), {'name': name}))
                        for key, name in [('settings', 'Settings'), ('module_manager', 'Module Manager')]]
        for key, _ in self.classes:
            self.settings.set_module_setting(key, 'enabled', False)
        self.load()
        self.assertEqual(set(self.hub.registry.modules), {'settings', 'module_manager'})


if __name__ == '__main__':
    unittest.main()
