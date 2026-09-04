#!/usr/bin/env python3
"""
CyberHub — Modular AI image toolkit.

Modules live under modules/ and are auto-discovered at startup. Enable
or disable them from the Settings page. Native installs keep their state next
to this file. Containers can set CYBERHUB_DATA_DIR to use a persistent volume.

Usage:
    python hub.py                       # use settings.json
    python hub.py --port 8899           # override port for this session
    python hub.py --listen              # bind 0.0.0.0 for this session
    python hub.py --share-network       # listen + add Windows firewall rule
    python hub.py --models /path.json   # override Civitai models.json
    python hub.py --verbose             # debug logging
"""

import argparse
import os
import signal
import socket
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import ModuleRegistry, available_module_classes, module_key_from_class
from core.civitai import CivitaiLookup
from core.module_store import ModuleStore
from core.server import Settings, HubHandler, ThreadedHTTPServer

RESOURCES_DIR = os.path.join(HERE, "resources")


def _resolve_data_dir():
    configured = os.environ.get("CYBERHUB_DATA_DIR", "").strip()
    if not configured:
        return HERE
    return os.path.abspath(os.path.expanduser(configured))


DATA_DIR = _resolve_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
INSTANCE_LOCK_PATH = os.path.join(DATA_DIR, ".cyberhub.lock")
_INSTANCE_LOCK_FILE = None


def acquire_instance_lock():
    """Prevent two CyberHub processes from using the same portable folder.

    This lock is taken before modules start, so a second launch cannot kick off
    another Gallery indexer against the same SQLite database. The lock file may
    remain on disk after exit; the OS lock itself is released automatically when
    the process stops.
    """
    global _INSTANCE_LOCK_FILE
    lock_file = open(INSTANCE_LOCK_PATH, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        print("[ERROR] CyberHub is already running from this folder.")
        print("        Close the other CyberHub window/terminal first, then start again.")
        sys.exit(1)
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()}\nstarted={int(time.time())}\n")
    lock_file.flush()
    _INSTANCE_LOCK_FILE = lock_file


# ─── Hub object (shared across modules) ──────────────────────────────────────

class Hub:
    """Central context passed to every module.

    Modules reach the settings store, the registry, and the Civitai
    lookup through this object.
    """

    VERSION = "1.3.0"

    # Subdirectories under resources/ that should always exist. Modules can
    # rely on these being present even if the user wipes the folder.
    RESOURCE_SUBDIRS = (
        "auto_tagger", "civitai", "danbooru", "fonts", "help", "upscalers"
    )

    def __init__(self, settings):
        self.settings = settings
        self.registry = ModuleRegistry()
        self.civitai = CivitaiLookup()
        self.resources_dir = RESOURCES_DIR
        self.data_dir = DATA_DIR
        self.module_store = ModuleStore(HERE, DATA_DIR)
        # Compatibility alias for modules contributed against the Docker PR.
        self.settings_dir = DATA_DIR
        self.is_container = os.environ.get("CYBERHUB_DOCKER", "").lower() in {
            "1", "true", "yes", "on",
        }
        # Filled in by run_server(); the diagnostic /api/system_info reads these so the
        # status panel can show "bound to 0.0.0.0:8899, up 2h 14m".
        self.bind = None
        self.port = None
        self.started_at = None
        self._ensure_resource_dirs()

    def _ensure_resource_dirs(self):
        """Create resources/<sub>/ folders if a user deleted them.

        Resources are looked up by absolute path. If a subfolder is missing,
        the module's status endpoint just reports "not ready", which is
        confusing if the user meant to keep that resource. Recreating the
        empty folder makes the layout self-healing.
        """
        for sub in self.RESOURCE_SUBDIRS:
            try:
                os.makedirs(os.path.join(self.resources_dir, sub), exist_ok=True)
            except OSError:
                pass  # read-only filesystem, etc. — modules will still degrade gracefully

    def resource_path(self, *parts):
        """Return an absolute path below the portable resources directory."""
        return os.path.join(self.resources_dir, *parts)

    def data_path(self, *parts):
        """Return an absolute path below the persistent user-data directory."""
        return os.path.join(self.data_dir, *parts)

    # Convenience for older module code that called these directly
    def civitai_lookup(self, parsed):
        return self.civitai.lookup(parsed)


def load_modules(hub):
    """Import every module folder; register the ones that are enabled.

    Settings and Module Manager are always enabled so the installation can
    always be configured and repaired.
    """
    for name, cls in available_module_classes():
        key = module_key_from_class(cls)
        version = getattr(cls, "version", "1.0")
        release_stage = getattr(cls, "release_stage", "stable")
        hub.module_store.adopt(
            name, name=getattr(cls, "name", name), version=version, channel=release_stage,
        )
        if key not in {"settings", "module_manager"} and not hub.settings.is_module_enabled(key):
            print(f"[MODULE] {cls.name} v{version}: disabled")
            continue

        try:
            instance = cls(hub)
            hub.registry.register(instance)
            instance.on_startup()
            print(f"[MODULE] {instance.name} v{getattr(instance, 'version', '1.0')}: loaded")
        except Exception as e:
            print(f"[MODULE] {cls.name} v{version}: startup failed — {e}")
            print(traceback.format_exc())


# ─── Network helpers ─────────────────────────────────────────────────────────

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def is_windows_admin():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def add_windows_firewall_rule(port):
    if os.name != "nt":
        return False
    import subprocess
    exe = sys.executable
    cmds = [
        ["netsh", "advfirewall", "firewall", "add", "rule",
         f"name=CyberHub Port {port}", "dir=in", "action=allow",
         "protocol=TCP", f"localport={port}", "profile=private", "enable=yes"],
        ["netsh", "advfirewall", "firewall", "add", "rule",
         "name=CyberHub Python", "dir=in", "action=allow",
         f"program={exe}", "profile=private", "enable=yes"],
    ]
    try:
        for c in cmds:
            subprocess.run(c, check=False, capture_output=True, text=True)
        print(f"[FIREWALL] Rules added for port {port}")
        return True
    except Exception as e:
        print(f"[FIREWALL] Could not add rule: {e}")
        return False


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="CyberHub")
    p.add_argument("folder", nargs="?", default=None,
                   help="Optional: image folder for the Gallery module (overrides settings)")
    p.add_argument("--port", "-p", type=int, default=None, help="Override server port")
    p.add_argument("--listen", action="store_true",
                   help="Listen on all interfaces (overrides settings)")
    p.add_argument("--share-network", action="store_true",
                   help="Listen on LAN + add Windows firewall rule")
    p.add_argument("--no-auth", action="store_true",
                   help="Disable LAN access token for this session (use only on trusted networks)")
    p.add_argument("--models", type=str, default=None,
                   help="Override Civitai models.json path")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Debug logging in metadata reader")
    args = p.parse_args()

    acquire_instance_lock()

    if args.verbose:
        from core import metadata
        metadata.VERBOSE = True

    settings = Settings(SETTINGS_PATH)

    # Positional folder arg overrides gallery folder for this session
    if args.folder:
        abs_folder = os.path.abspath(args.folder)
        if not os.path.isdir(abs_folder):
            print(f"[ERROR] Folder not found: {abs_folder}")
            sys.exit(1)
        settings.set_path("modules.gallery.folders", [abs_folder])
        print(f"[HUB] Gallery folder (CLI override): {abs_folder}")

    # Resolve effective values (CLI overrides settings for this session)
    port = args.port or int(settings.get("port", 8899))
    listen = args.listen or args.share_network or bool(settings.get_path("network.listen_lan", False))
    share_network = args.share_network or bool(settings.get_path("network.share_network", False))
    require_auth = bool(settings.get_path("network.require_auth", True))

    # Auto-generate a LAN access token on first launch with listen_lan on.
    # CLI flag --no-auth disables it for the session without rewriting settings.
    auth_token = settings.get_path("network.auth_token", "") or ""
    if listen and require_auth and not args.no_auth and not auth_token:
        import secrets
        auth_token = secrets.token_urlsafe(24)
        settings.set_path("network.auth_token", auth_token)
        print(f"[AUTH] Generated new LAN access token (saved to settings.json)")

    hub = Hub(settings)

    # Civitai database resolution. Order of priority:
    #   1. --models CLI arg
    #   2. Settings override (civitai.models_path)
    #   3. Canonical resources/civitai/models.json
    #   4. Auto-detect legacy locations (next to hub.py, inside any configured
    #      gallery folder). This keeps users coming from the standalone
    #      gallery.py working — they had models.json colocated there.
    # source_path stays set to the canonical location even when nothing is
    # loaded yet, so "Update now" creates the file in the right place.
    models_override = settings.get_path("civitai.models_path", "") or ""
    canonical = hub.resource_path("civitai", "models.json")
    explicit_path = args.models or models_override
    models_path = os.path.abspath(explicit_path or canonical)
    hub.civitai.source_path = models_path
    hub.civitai.source_mode = "cli" if args.models else ("override" if models_override else "default")
    hub.civitai.debug = bool(settings.get_path("civitai.verbose", False))

    if os.path.isfile(models_path):
        hub.civitai.load(models_path)
    elif not explicit_path:
        # Try legacy locations only when the user hasn't explicitly pointed at a file.
        candidates = [os.path.abspath(os.path.join(HERE, "models.json"))]
        gallery_folders = settings.get_module_setting("gallery", "folders", []) or []
        if not gallery_folders:
            legacy_folder = settings.get_module_setting("gallery", "folder", "")
            if legacy_folder:
                gallery_folders = [legacy_folder]
        for folder in gallery_folders:
            if folder:
                candidates.append(os.path.abspath(os.path.join(folder, "models.json")))
        found = None
        for cand in candidates:
            if os.path.isfile(cand):
                found = cand
                break
        if found:
            # Load from the legacy location, but leave source_path pointing at
            # the canonical resources path so Settings → Update writes there.
            hub.civitai.load(found)
            print(f"[CIVITAI] Auto-detected legacy models.json: {found}")
            print(f"[CIVITAI] To make this permanent, either:")
            print(f"  • copy/move it to {canonical}")
            print(f"  • or set Settings → Civitai integration → models.json override")
        else:
            print(f"[CIVITAI] Database not found yet: {models_path}")
    else:
        # User explicitly pointed at a path that doesn't exist — surface that.
        print(f"[CIVITAI] Database not found yet: {models_path}")

    print(f"[HUB] {settings.get('title', 'CyberHub')} v{hub.VERSION} starting…")
    load_modules(hub)

    if not hub.registry.modules:
        print("[ERROR] No modules loaded — nothing to serve.")
        sys.exit(1)

    if share_network and os.name == "nt":
        if is_windows_admin():
            add_windows_firewall_rule(port)
        else:
            print("[FIREWALL] Not running as Administrator — auto-firewall skipped.")
            print(f'[FIREWALL] To add manually (one-time, as Admin):')
            print(f'  netsh advfirewall firewall add rule name="CyberHub {port}" '
                  f'dir=in action=allow protocol=TCP localport={port} profile=private enable=yes')

    HubHandler.registry = hub.registry
    HubHandler.settings = settings
    HubHandler.hub = hub
    # Auth is required when LAN listening is active, auth is enabled, the user
    # did not pass --no-auth, and a token exists.
    HubHandler.require_auth = bool(
        listen
        and require_auth
        and not args.no_auth
        and auth_token
    )
    HubHandler.auth_token = auth_token if HubHandler.require_auth else ""

    bind = "0.0.0.0" if listen else "127.0.0.1"
    server = ThreadedHTTPServer((bind, port), HubHandler)
    hub.bind = bind
    hub.port = port
    hub.started_at = time.time()

    startup = settings.get("startup", "gallery")
    if not hub.registry.get(startup):
        tabs = hub.registry.visible_tabs()
        startup = tabs[0].key() if tabs else "settings"

    if listen:
        ip = get_lan_ip()
        print(f"[SERVE] http://localhost:{port}  (local)")
        if HubHandler.require_auth:
            print(f"[SERVE] http://{ip}:{port}/?token={auth_token}  (network — share this URL)")
            print(f"[SERVE] LAN auth: token required. Disable with --no-auth or Settings → Network.")
        else:
            print(f"[SERVE] http://{ip}:{port}  (network — no auth)")
    else:
        print(f"[SERVE] http://localhost:{port}")
    print(f"[SERVE] Startup module: {startup}")
    print("[SERVE] Ctrl+C to stop")

    shutdown_requested = threading.Event()

    def request_shutdown(_signum, _frame):
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        print("\n[STOP] Shutdown requested...")
        # BaseServer.shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down...")
    finally:
        server.server_close()
        print("[STOP] CyberHub stopped.")


if __name__ == "__main__":
    main()
