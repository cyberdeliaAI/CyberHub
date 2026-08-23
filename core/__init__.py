"""
CyberHub — Module framework.

Each module is a folder under modules/ containing an __init__.py with a
Module subclass. Modules register HTTP routes and a settings_schema, and
the hub framework wires them into the topbar and the settings page.
"""

from collections import OrderedDict
import importlib
import os


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES_DIR = os.path.join(ROOT_DIR, "modules")


class Module:
    """Base class for hub modules."""

    # ─── Override these in subclasses ────────────────────────────────────────
    name = "Module"                  # Display name in the topbar (e.g. "Gallery")
    version = "1.0"                  # Module release/version shown in Settings
    release_stage = "stable"         # stable or beta; shown in Settings and used by release builds
    icon = ""                        # Optional emoji/glyph shown next to the name
    description = ""                 # Shown on the Settings page
    order = 100                      # Lower = further left in the topbar
    show_in_tabs = True              # Set False for hidden modules (e.g. Settings)
    settings_schema = {}             # See modules/settings/__init__.py for the format

    def __init__(self, hub):
        self.hub = hub

    # ─── HTTP routing ────────────────────────────────────────────────────────
    def routes_get(self):
        """Return {path: handler_func} for GET routes."""
        return {}

    def routes_post(self):
        """Return {path: handler_func} for POST routes."""
        return {}

    def prefix_routes(self):
        """Return {prefix: handler_func} for path-prefix GET routes.

        Useful for /thumb/<rel_path>, /image/<rel_path>, etc. The handler
        receives (handler, remaining_path) where remaining_path is the URL
        suffix after the prefix, URL-decoded.
        """
        return {}

    # ─── Lifecycle ───────────────────────────────────────────────────────────
    def on_startup(self):
        """Called once at server startup, after the settings are loaded."""
        pass

    def on_settings_changed(self, key, value):
        """Called when one of this module's settings is updated via the UI.

        Default: no-op. Settings that require a restart should say so in
        their schema rather than trying to hot-reload.
        """
        pass

    # ─── Settings shortcuts ──────────────────────────────────────────────────
    def setting(self, key, default=None):
        """Get a single setting for this module, falling back to the schema
        default and then `default`."""
        schema_default = self.settings_schema.get(key, {}).get("default")
        return self.hub.settings.get_module_setting(
            self.key(), key,
            default if schema_default is None else schema_default
        )

    def key(self):
        """URL-safe settings key for this module (lowercased, spaces → underscores)."""
        return module_key_from_name(self.name)


def module_key_from_name(name):
    """URL-safe settings key for a module display name."""
    return name.lower().replace(" ", "_")


def module_key_from_class(cls):
    """URL-safe settings key for a Module subclass."""
    return module_key_from_name(getattr(cls, "name", cls.__name__))


def discover_module_names():
    """List of subfolder names in modules/ that contain an __init__.py."""
    names = []
    if not os.path.isdir(MODULES_DIR):
        return names
    for entry in sorted(os.listdir(MODULES_DIR)):
        sub = os.path.join(MODULES_DIR, entry)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "__init__.py")):
            names.append(entry)
    return names


def find_module_class(pkg):
    """Return the first Module subclass exported by `pkg` (skipping the base class itself)."""
    for attr_name in dir(pkg):
        attr = getattr(pkg, attr_name)
        if isinstance(attr, type) and issubclass(attr, Module) and attr is not Module:
            return attr
    return None


def available_module_classes():
    """Return [(folder_name, ModuleSubclass), ...] for all importable module folders."""
    classes = []
    for name in discover_module_names():
        try:
            pkg = importlib.import_module(f"modules.{name}")
            cls = find_module_class(pkg)
        except Exception as e:
            print(f"[MODULE] {name}: import failed — {e}")
            continue
        if cls is None:
            print(f"[MODULE] {name}: no Module subclass found")
            continue
        classes.append((name, cls))
    return classes


class ModuleRegistry:
    """Holds Module instances and their route lookup tables."""

    def __init__(self):
        self.modules = OrderedDict()   # key -> Module instance
        self.get_routes = {}           # exact path -> (Module, handler)
        self.post_routes = {}          # exact path -> (Module, handler)
        self.prefix_routes = {}        # prefix -> (Module, handler)

    def register(self, module):
        key = module.key()
        self.modules[key] = module
        for path, h in module.routes_get().items():
            if path in self.get_routes:
                existing = self.get_routes[path][0].name
                raise ValueError(
                    f"GET route conflict: {path!r} already registered by "
                    f"{existing!r}; cannot register {module.name!r}"
                )
            self.get_routes[path] = (module, h)
        for path, h in module.routes_post().items():
            if path in self.post_routes:
                existing = self.post_routes[path][0].name
                raise ValueError(
                    f"POST route conflict: {path!r} already registered by "
                    f"{existing!r}; cannot register {module.name!r}"
                )
            self.post_routes[path] = (module, h)
        for prefix, h in module.prefix_routes().items():
            if prefix in self.prefix_routes:
                existing = self.prefix_routes[prefix][0].name
                raise ValueError(
                    f"Prefix route conflict: {prefix!r} already registered by "
                    f"{existing!r}; cannot register {module.name!r}"
                )
            self.prefix_routes[prefix] = (module, h)

    def get(self, key):
        return self.modules.get(key.lower())

    def visible_tabs(self):
        """Modules that should appear as topbar tabs, in display order."""
        return sorted(
            (m for m in self.modules.values() if m.show_in_tabs),
            key=lambda m: (m.order, m.name.lower())
        )
