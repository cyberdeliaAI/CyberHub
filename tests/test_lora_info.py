import hashlib
import json
import os
import struct
import tempfile
import unittest

import build_release
from core import ModuleRegistry
from core.server import build_module_menu, module_icon_html
from modules.lora_info import (
    LoRAInfoModule,
    MAX_HEADER_BYTES,
    PAGE_BODY,
    analyze_safetensors_header,
    parse_safetensors_header,
    read_safetensors_file,
    server_locations,
)


class LoRAInfoTests(unittest.TestCase):
    def _safetensors_bytes(self):
        metadata = {
            "ss_output_name": "portrait_style_v3",
            "ss_sd_model_name": "sd_xl_base_1.0",
            "ss_network_module": "networks.lora",
            "ss_network_dim": "32",
            "ss_network_alpha": "16",
            "ss_resolution": "[1024, 1024]",
            "ss_batch_size_per_device": "4",
            "ss_steps": "1200",
            "ss_dataset_dirs": json.dumps({"20_subject": {"img_count": 25}}),
            "ss_tag_frequency": json.dumps({
                "20_subject": {"portrait_style": 25, "red hair": 12},
                "10_closeup": {"portrait_style": 8},
            }),
            "sshs_model_hash": "stored-model-hash",
        }
        header = {
            "__metadata__": metadata,
            "lora_unet_block.lora_up.weight": {
                "dtype": "F16", "shape": [2, 3], "data_offsets": [0, 12],
            },
            "lora_te_text.lora_down.weight": {
                "dtype": "F32", "shape": [2, 2], "data_offsets": [12, 28],
            },
        }
        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        return struct.pack("<Q", len(header_bytes)) + header_bytes + bytes(28)

    def test_module_version_and_dual_network_sources(self):
        self.assertEqual(LoRAInfoModule.version, "1.0.3")
        self.assertEqual(build_release.module_metadata("lora_info")["version"], "1.0.3")
        self.assertIn("lora_info", build_release.complete_module_names())
        self.assertIn("This computer", PAGE_BODY)
        self.assertIn("CyberHub computer", PAGE_BODY)
        self.assertNotIn("Deze computer", PAGE_BODY)
        self.assertNotIn("CyberHub-computer", PAGE_BODY)
        self.assertIn("/api/lora-info/analyze-header", PAGE_BODY)
        self.assertIn("/api/lora-info/analyze-path", PAGE_BODY)
        self.assertIn("file.slice(8,8+headerSize)", PAGE_BODY)
        self.assertIn("&ext=.safetensors", PAGE_BODY)
        self.assertIn("/api/lora-info/locations", PAGE_BODY)

    def test_module_uses_the_route_expected_by_menu_and_settings(self):
        module = object.__new__(LoRAInfoModule)
        routes = module.routes_get()
        self.assertIn("/lora_info", routes)
        self.assertIn("/lora-info", routes)
        self.assertTrue(module.show_in_tabs)

        registry = ModuleRegistry()
        registry.register(module)
        menu = build_module_menu(registry, "lora_info")
        self.assertIn('href="/lora_info"', menu)
        self.assertIn("LoRA Info", menu)
        self.assertNotIn('<circle cx="12" cy="12" r="4"/>', module_icon_html("lora_info"))

    def test_server_locations_include_home_and_have_unique_paths(self):
        locations = server_locations()
        paths = [item["path"] for item in locations]
        self.assertIn(os.path.abspath(os.path.expanduser("~")), paths)
        identities = [os.path.normcase(os.path.realpath(path)) for path in paths]
        self.assertEqual(len(identities), len(set(identities)))

    def test_reads_metadata_tensors_tags_and_hash_without_loading_model(self):
        payload = self._safetensors_bytes()
        with tempfile.NamedTemporaryFile(suffix=".safetensors", delete=False) as handle:
            handle.write(payload)
            path = handle.name
        try:
            report = read_safetensors_file(path)
        finally:
            os.unlink(path)

        self.assertEqual(report["summary"]["name"], "portrait_style_v3")
        self.assertEqual(report["summary"]["resolution"], "1024 × 1024")
        self.assertEqual(report["summary"]["dataset_images"], 25)
        self.assertEqual(report["tensors"]["count"], 2)
        self.assertEqual(report["tensors"]["parameters"], 10)
        self.assertEqual(report["tensors"]["bytes"], 28)
        self.assertEqual(report["tags"][0], {"tag": "portrait_style", "count": 33})
        self.assertEqual(report["hashes"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(report["hashes"]["autov2"], hashlib.sha256(payload).hexdigest()[:10])
        self.assertEqual(report["hashes"]["stored_model"], "stored-model-hash")

    def test_browser_header_report_does_not_require_tensor_bytes(self):
        header = {
            "__metadata__": {"modelspec.title": "Browser LoRA"},
            "lora_unet.weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]},
        }
        report = analyze_safetensors_header(
            header,
            filename="Browser LoRA.safetensors",
            file_size=4096,
            source="browser",
        )
        self.assertEqual(report["summary"]["name"], "Browser LoRA")
        self.assertEqual(report["file"]["source"], "browser")
        self.assertIsNone(report["hashes"]["sha256"])

    def test_non_finite_trainer_metadata_returns_strict_json(self):
        header_bytes = (
            b'{"__metadata__":{"modelspec.title":"Non-finite LoRA",'
            b'"ss_network_args":"{\\"d_legacy\\":Infinity,'
            b'\\"noise\\":NaN,\\"floor\\":-Infinity}"}}'
        )
        report = parse_safetensors_header(
            header_bytes,
            filename="non-finite.safetensors",
            file_size=8 + len(header_bytes),
        )

        self.assertEqual(report["summary"]["network_args"], {
            "d_legacy": "Infinity",
            "noise": "NaN",
            "floor": "-Infinity",
        })
        json.dumps(report, allow_nan=False)

    def test_rejects_declared_header_beyond_file_and_safety_limit(self):
        for header_size, message in (
            (100, "extends beyond"),
            (MAX_HEADER_BYTES + 1, "64 MB"),
        ):
            with self.subTest(header_size=header_size):
                with tempfile.NamedTemporaryFile(suffix=".safetensors", delete=False) as handle:
                    handle.write(struct.pack("<Q", header_size) + b"{}")
                    path = handle.name
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        read_safetensors_file(path, include_hash=False)
                finally:
                    os.unlink(path)

    def test_missing_metadata_is_reported_but_tensors_remain_visible(self):
        header = {
            "lora_unet.weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]},
        }
        report = analyze_safetensors_header(header, filename="plain.safetensors", file_size=1024)
        self.assertEqual(report["summary"]["name"], "plain")
        self.assertEqual(report["tensors"]["count"], 1)
        self.assertIn("no embedded LoRA metadata", report["warnings"][0])


if __name__ == "__main__":
    unittest.main()
