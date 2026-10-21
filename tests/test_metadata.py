import json
import unittest

from core.metadata import _split_a1111_settings, parse_sd_parameters


class A1111MetadataTests(unittest.TestCase):
    def parameters(self, resources, model="krea2TurboNvfp4", key="Civitai resources"):
        model_field = f", Model: {model}" if model is not None else ""
        return ("A cinematic scene. <lora:zoom:-4.0>\n"
                "Negative prompt: low resolution, random letters\n"
                "Steps: 8, Sampler: Euler, CFG scale: 1.0, Seed: 13048732417415, "
                "Size: 1376x768" + model_field + ", Version: ComfyUI, " + key + ": "
                + json.dumps(resources) + ", Clip skip: 2")

    def test_nested_json_is_one_value_and_following_settings_survive(self):
        value = json.dumps([{"name": 'A, "quoted" name', "nested": [{"path": "a:b,c"}]}])
        settings = _split_a1111_settings("Steps: 8, Resources: " + value + ", Seed: 42")
        self.assertEqual(settings, {"Steps": "8", "Resources": value, "Seed": "42"})

    def test_json_object_and_escaped_backslashes(self):
        value = json.dumps({"path": 'C:\\models\\', "name": 'a \\"b,c', "values": [1, 2]})
        self.assertEqual(_split_a1111_settings("Data: " + value + ", Steps: 8"),
                         {"Data": value, "Steps": "8"})

    def test_quoted_hashes_and_ordinary_settings_are_unchanged(self):
        self.assertEqual(_split_a1111_settings(
            'Steps: 20, Lora hashes: "a: 123, b: 456", Model: example, Seed: 42'),
            {"Steps": "20", "Lora hashes": "a: 123, b: 456", "Model": "example", "Seed": "42"})

    def test_escaped_quote_does_not_end_a_quoted_value(self):
        value = r'a: \"quoted, name\", b: 456'
        self.assertEqual(_split_a1111_settings('Lora hashes: "' + value + '", Steps: 8'),
                         {"Lora hashes": value, "Steps": "8"})

    def test_checkpoint_and_all_seven_loras_preserve_weights(self):
        weights = [-4.0, 1.0, 1.0, 0.5, 0.6, 0.2, 0.6]
        resources = [{"type": "checkpoint", "modelName": "Krea 2 Turbo",
                      "modelVersionName": "krea2_turbo_nvfp4"}]
        resources += [{"type": "lora", "modelName": f"LoRA {i}", "modelVersionName": "Krea 2",
                       "weight": w, "air": f"urn:air:krea2:lora:civitai:100@{i}"}
                      for i, w in enumerate(weights)]
        raw = self.parameters(resources)
        result = parse_sd_parameters(raw)
        settings = result["settings"]
        self.assertEqual(result["raw"], raw)
        self.assertEqual(result["prompt"], "A cinematic scene. <lora:zoom:-4.0>")
        self.assertEqual(result["negative_prompt"], "low resolution, random letters")
        self.assertEqual(settings["Model"], "krea2TurboNvfp4")
        self.assertEqual(settings["LoRAs"], ", ".join(f"LoRA {i}:{w}" for i, w in enumerate(weights)))
        self.assertEqual(set(settings), {"Steps", "Sampler", "CFG scale", "Seed", "Size",
                                         "Model", "Version", "LoRAs", "Clip skip"})
        self.assertEqual(settings["Clip skip"], "2")

    def test_checkpoint_fallback_never_uses_a_lora(self):
        resources = [{"type": "lora", "modelName": "Style", "weight": 0},
                     {"type": "checkpoint", "modelName": "Base", "modelVersionName": "Base v2"},
                     {"type": "checkpoint", "modelVersionName": "Second base"}]
        settings = parse_sd_parameters(self.parameters(resources, model=None))["settings"]
        self.assertEqual(settings["Model"], "Base v2")
        self.assertEqual(settings["LoRAs"], "Style:0")

    def test_camelcase_resources_in_a1111_settings(self):
        resources = [{"type": "lora", "modelVersionName": "Style v2", "weight": -0.5}]
        settings = parse_sd_parameters(self.parameters(resources, key="civitaiResources"))["settings"]
        self.assertEqual(settings["LoRAs"], "Style v2:-0.5")
        self.assertNotIn("civitaiResources", settings)

    def test_invalid_resource_data_remains_visible_without_crashing(self):
        for resources in ({"unexpected": [1, 2]}, "broken JSON", [None, 7, {"type": 2}],
                          [{"type": "lora", "modelName": {"unexpected": "object"}}]):
            with self.subTest(resources=resources):
                settings = parse_sd_parameters(self.parameters(resources))["settings"]
                self.assertEqual(settings["Model"], "krea2TurboNvfp4")
                expected = resources if isinstance(resources, str) else json.dumps(resources)
                self.assertEqual(settings["Civitai resources"], expected)
                self.assertNotIn("LoRAs", settings)

    def test_missing_names_and_weights_are_safe(self):
        resources = [{"type": "lora"}, {"type": "lora", "modelName": "Style"},
                     {"type": "lora", "modelName": "Other", "weight": "0.75"}]
        settings = parse_sd_parameters(self.parameters(resources))["settings"]
        self.assertEqual(settings["LoRAs"], "Style, Other:0.75")

    def test_unclosed_json_does_not_create_fake_settings(self):
        value = '[{"modelName":"Style", "weight":0.5'
        self.assertEqual(_split_a1111_settings("Steps: 8, Civitai resources: " + value),
                         {"Steps": "8", "Civitai resources": value})


class CivitaiMetadataCompatibilityTests(unittest.TestCase):
    def test_existing_civitai_json_and_python_literal_formats(self):
        resources = [{"type": "checkpoint", "modelVersionName": "Base v1"},
                     {"type": "lora", "modelName": "Style", "modelVersionName": "Style v2", "weight": 1},
                     {"type": "lora", "modelVersionName": "Zoom", "weight": -4.0}]
        for value in (json.dumps(resources), repr(resources)):
            with self.subTest(value=value):
                raw = "prompt: Scene\nnegativePrompt: Blur\nsteps: 8\ncivitaiResources: " + value
                result = parse_sd_parameters(raw)
                self.assertEqual(result["raw"], raw)
                self.assertEqual(result["prompt"], "Scene")
                self.assertEqual(result["negative_prompt"], "Blur")
                self.assertEqual(result["settings"],
                                 {"Steps": "8", "Model": "Base v1", "LoRAs": "Style v2, Zoom:-4.0"})


if __name__ == "__main__":
    unittest.main()
