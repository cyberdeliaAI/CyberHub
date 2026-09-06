import unittest
from html.parser import HTMLParser
from types import SimpleNamespace

from core.server import build_module_menu


class MenuParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.in_footer = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("class") == "hub-menu-footer":
            self.in_footer = True
        if tag == "a":
            self.links.append((attrs["href"], attrs["class"], self.in_footer))


class ModuleMenuTests(unittest.TestCase):
    def render(self, active="gallery", manager=True):
        def module(key, name):
            return SimpleNamespace(key=lambda: key, name=name, description="Description")

        modules = {key: module(key, name) for key, name in [
            ("module_manager", "Module Manager"), ("gallery", "Gallery"),
            ("viewer", "Viewer"), ("settings", "Settings"),
        ] if manager or key != "module_manager"}
        registry = SimpleNamespace(
            visible_tabs=lambda: list(modules.values()), get=modules.get,
        )
        parser = MenuParser()
        parser.feed(build_module_menu(registry, active))
        return parser.links

    def test_management_links_are_last_and_not_duplicated(self):
        links = self.render()
        self.assertEqual([link[0] for link in links], [
            "/gallery", "/viewer", "/settings", "/module_manager",
        ])
        self.assertEqual([link[2] for link in links], [False, False, True, True])

    def test_active_management_link_is_highlighted(self):
        for active in ("settings", "module_manager"):
            links = self.render(active=active)
            self.assertEqual([href for href, cls, _ in links if " active" in cls], ["/" + active])

    def test_no_broken_manager_link_when_module_is_missing(self):
        self.assertEqual([link[0] for link in self.render(manager=False)], [
            "/gallery", "/viewer", "/settings",
        ])


if __name__ == "__main__":
    unittest.main()
