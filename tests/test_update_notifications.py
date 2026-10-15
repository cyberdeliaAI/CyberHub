import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.server import Settings, build_module_menu
from core.module_store import ModuleStore
from core import ModuleRegistry
from core.update_notifications import UpdateNotifications
from modules.settings import SettingsModule
from modules.module_manager import ModuleManagerModule


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = SimpleNamespace(VERSION="1.4.2", data_path=lambda *p: str(self.root.joinpath(*p)),
                                   settings=Settings(str(self.root / "settings.json")), registry=ModuleRegistry(),
                                   module_store=ModuleStore(str(self.root), str(self.root)))
        self.settings = SettingsModule(self.hub)
        self.manager = ModuleManagerModule(self.hub)
        self.hub.registry.register(self.settings)
        self.hub.registry.register(self.manager)
        self.service = self.hub.update_notifications = UpdateNotifications(self.hub)
        self.local = [{"id":"gallery", "name":"Gallery", "version":"1.0", "channel":"stable", "protected":False}]
        self.manager._installed = Mock(side_effect=lambda: self.local)
        self.system = {"packages":[{"type":"hub", "id":"cyberhub", "name":"CyberHub", "version":"1.5.0",
                                    "minimum_hub_version":""}]}
        self.catalog = {"modules":[self.item("gallery", "1.1")]}

    @staticmethod
    def item(module_id, version, **kwargs):
        return {"id":module_id, "name":module_id, "version":version, "minimum_hub_version":"",
                "dependencies":[], "channel":"stable", **kwargs}

    def remember(self):
        self.service.remember("system", self.system)
        self.service.remember("modules", self.catalog)

    def test_disabled_by_default_and_local_reads_never_use_network(self):
        with patch.object(self.settings, "_github_api_json") as network, patch.object(self.manager, "_fetch_catalog") as catalog:
            self.assertFalse(self.service.start())
            self.assertFalse(self.service.snapshot()["automatic_check"])
            self.assertEqual(self.service.snapshot()["count"], 0)
            network.assert_not_called(); catalog.assert_not_called()
        self.assertFalse(Path(self.service.path).exists())

    def test_cache_survives_restart_and_offline_reads(self):
        self.remember()
        reloaded = UpdateNotifications(self.hub)
        with patch.object(self.manager, "_fetch_catalog", side_effect=AssertionError("Unexpected network")):
            snapshot = reloaded.snapshot()
        self.assertEqual(snapshot["count"], 2)
        self.assertTrue(snapshot["checked_at"]["system"])

    def test_automatic_daily_limit_is_persistent_and_per_source(self):
        self.hub.settings.set_path("updates.automatic_check", True)
        with patch("core.update_notifications.time.time", return_value=100000):
            self.remember()
            self.assertFalse(UpdateNotifications(self.hub).start())
        with patch("core.update_notifications.time.time", return_value=100000 + 86400), patch("core.update_notifications.threading.Thread") as thread:
            self.assertTrue(self.service.start())
            self.assertEqual(thread.call_args.kwargs["args"][0], ["system", "modules"])

    def test_manual_check_bypasses_disabled_setting_and_daily_limit(self):
        self.remember()
        with patch("core.update_notifications.threading.Thread") as thread:
            self.assertTrue(self.service.start(force=True))
            self.assertFalse(self.service.start(force=True))
            self.assertEqual(thread.call_count, 1)

    def test_failed_check_preserves_results_and_does_not_retry_on_each_start(self):
        self.remember()
        with patch.object(self.settings, "_github_api_json", side_effect=OSError("Offline")), patch.object(self.manager, "_fetch_catalog", side_effect=OSError("Offline")):
            self.service._check(["system", "modules"], True)
        self.assertEqual(self.service.snapshot()["count"], 2)
        self.assertEqual(self.service.snapshot()["errors"], {"system":"Offline", "modules":"Offline"})
        self.hub.settings.set_path("updates.automatic_check", True)
        self.assertFalse(UpdateNotifications(self.hub).start())

    def test_first_failed_check_is_not_reported_as_up_to_date(self):
        with patch.object(self.settings, "_github_api_json", side_effect=OSError("Offline")):
            self.service._check(["system"], True)
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["checked_at"]["system"], "")
        self.assertEqual(snapshot["errors"]["system"], "Offline")

    def test_successful_checks_use_existing_validators_and_never_install(self):
        with patch.object(self.settings, "_github_api_json", return_value={"tag_name":"v1.5.0"}), \
             patch.object(self.settings, "_release_update_info", return_value=self.system) as validate, \
             patch.object(self.manager, "_fetch_catalog", return_value=self.catalog), \
             patch.object(self.settings, "_install_import_zip") as install:
            self.service._check(["system", "modules"], True)
        validate.assert_called_once(); install.assert_not_called()
        self.assertEqual(self.service.snapshot()["count"], 2)
        self.assertEqual(self.manager._catalog["gallery"]["version"], "1.1")

    def test_existing_manual_checks_feed_notification_cache(self):
        handler = Mock(); handler._is_loopback.return_value = True
        with patch.object(self.settings, "_github_api_json", return_value={}), patch.object(self.settings, "_release_update_info", return_value=self.system):
            self.settings._api_update_check(handler, {})
        with patch.object(self.manager, "_fetch_catalog", return_value=self.catalog):
            self.manager._api_catalog(handler, {"refresh":["1"]})
        self.assertEqual(self.service.snapshot()["count"], 2)

    def test_only_installed_modules_are_counted_and_beta_is_not_offered_to_stable(self):
        self.catalog["modules"] += [self.item("new", "2.0")]
        self.remember()
        self.assertEqual(self.service.snapshot()["count"], 2)
        self.catalog["modules"][0] = self.item("gallery", "2.0-beta", channel="beta")
        self.service.remember("modules", self.catalog)
        self.assertEqual(self.service.snapshot()["count"], 1)
        self.local[0]["channel"] = "beta"
        self.assertEqual(self.service.snapshot()["count"], 2)

    def test_installed_new_version_and_removal_clear_cached_module_notice(self):
        self.remember()
        self.local[0]["version"] = "1.1"
        self.assertEqual(self.service.snapshot()["count"], 1)
        self.local.clear()
        self.assertEqual(self.service.snapshot()["count"], 1)

    def test_installed_core_pending_restart_does_not_keep_notifying(self):
        self.remember()
        self.settings._github_update_status.update(stage="done", result={"package":{"type":"hub","version":"1.5.0"}})
        self.assertEqual(self.service.snapshot()["count"], 1)
        self.settings._github_update_status.update(stage="starting", result=None)
        self.assertEqual(self.service.snapshot()["count"], 2)

    def test_incompatible_update_has_explicit_minimum(self):
        self.catalog["modules"][0]["minimum_hub_version"] = "9.0"
        self.remember()
        module = next(u for u in self.service.snapshot()["updates"] if u["type"] == "module")
        self.assertFalse(module["compatible"])
        self.assertEqual(module["minimum_hub_version"], "9.0")

    def test_corrupt_cache_and_bad_timestamps_do_not_block_startup(self):
        Path(self.service.path).write_text('{"schema":1,"sources":{"system":null,"modules":[]}}')
        service = UpdateNotifications(self.hub)
        self.assertEqual(service.snapshot()["count"], 0)
        service._cache["sources"]["system"] = {"last_attempt":"invalid"}
        self.assertTrue(service._due("system", 100))
        self.assertTrue(service._due("modules", 100))
        service._cache["sources"]["system"]["data"] = {"packages":None}
        self.assertEqual(service.snapshot()["count"], 0)

    def test_preference_is_boolean_local_only_and_disabling_never_checks(self):
        handler = Mock(); handler._is_loopback.return_value = False
        handler.read_body_json.return_value = {"path":"updates.automatic_check", "value":True}
        self.settings._api_save_path(handler, 0, "application/json")
        self.assertEqual(handler.respond_json.call_args.kwargs["status"], 403)
        handler._is_loopback.return_value = True
        handler.read_body_json.return_value["value"] = "false"
        self.settings._api_save_path(handler, 0, "application/json")
        self.assertEqual(handler.respond_json.call_args.kwargs["status"], 400)
        handler.read_body_json.return_value["value"] = False
        with patch.object(self.service, "start") as start:
            self.settings._api_save_path(handler, 0, "application/json")
            start.assert_not_called()
        self.assertFalse(self.service.automatic_enabled())

    def test_manual_notification_check_cannot_be_triggered_remotely(self):
        handler = Mock(); handler._is_loopback.return_value = False
        with patch.object(self.service, "start") as start:
            self.settings._api_notifications_check(handler, 0, "application/json")
            start.assert_not_called()
        self.assertEqual(handler.respond_json.call_args.kwargs["status"], 403)

    def test_cached_catalog_is_revalidated_before_restore(self):
        self.remember()
        with patch.object(self.manager, "_validate_catalog", side_effect=ValueError("Invalid")), patch.object(self.manager, "_remember_catalog") as remember:
            self.service.restore_catalog()
            remember.assert_not_called()

    def test_real_menu_has_one_notice_link_but_no_install_action(self):
        html = build_module_menu(self.hub.registry, "settings")
        self.assertEqual(html.count('href="/settings#update-notifications"'), 1)
        self.assertIn('data-hub-update-count hidden', html)
        self.assertNotIn('/api/updates/check', html)


if __name__ == "__main__":
    unittest.main()
