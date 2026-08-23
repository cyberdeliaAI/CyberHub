import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.server import Settings
from hub import Hub
from modules.settings import SettingsModule


ROOT = Path(__file__).resolve().parents[1]


class _BrowseHandler:
    client_address = ("203.0.113.10", 12345)

    def __init__(self, require_auth):
        self.require_auth = require_auth
        self.auth_token = "valid-token" if require_auth else ""
        self.payload = None
        self.status = 200

    def respond_json(self, payload, status=200):
        self.payload = payload
        self.status = status


class DockerSupportTests(unittest.TestCase):
    def _probe_data_dir(self, configured=None):
        environment = os.environ.copy()
        if configured is None:
            environment.pop("CYBERHUB_DATA_DIR", None)
        else:
            environment["CYBERHUB_DATA_DIR"] = configured
        return subprocess.check_output(
            [sys.executable, "-c", "import hub; print(hub.DATA_DIR)"],
            cwd=ROOT,
            env=environment,
            text=True,
        ).strip()

    def test_native_install_keeps_portable_data_layout(self):
        self.assertEqual(self._probe_data_dir(), str(ROOT))

    def test_environment_can_select_persistent_data_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            self.assertEqual(self._probe_data_dir(data_dir), data_dir)

    def test_authenticated_remote_folder_browse_is_allowed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            hub = Hub(Settings(os.path.join(data_dir, "settings.json")))
            handler = _BrowseHandler(require_auth=True)
            SettingsModule(hub)._api_browse(handler, {"path": [data_dir]})
            self.assertEqual(handler.status, 200)
            self.assertEqual(handler.payload["path"], data_dir)

    def test_unauthenticated_remote_folder_browse_stays_blocked(self):
        with tempfile.TemporaryDirectory() as data_dir:
            hub = Hub(Settings(os.path.join(data_dir, "settings.json")))
            handler = _BrowseHandler(require_auth=False)
            SettingsModule(hub)._api_browse(handler, {"path": [data_dir]})
            self.assertEqual(handler.status, 403)

    def test_container_defaults_are_non_root_and_localhost_only(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("USER cyberhub", dockerfile)
        self.assertLess(dockerfile.index("USER cyberhub"), dockerfile.index("ENTRYPOINT"))
        self.assertIn("127.0.0.1:8899:8899", compose)
        self.assertNotIn("--no-auth", dockerfile + compose)
        self.assertIn("settings.json", dockerignore)


if __name__ == "__main__":
    unittest.main()
