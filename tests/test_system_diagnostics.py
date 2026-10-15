import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from modules.settings import (
    SettingsModule, _auto_tagger_diagnostics, _onnx_diagnostics, _system_gpus,
)


class GpuDiagnosticsTests(unittest.TestCase):
    def detect(self, system, output):
        with patch("platform.system", return_value=system), \
             patch("subprocess.check_output", return_value=output):
            return _system_gpus()

    def test_linux_lists_display_and_compute_adapters_in_bus_order(self):
        listing = (
            "03:00.0 VGA compatible controller: AMD Radeon RX 580 (rev ef)\n"
            "0b:00.0 3D controller: NVIDIA GeForce RTX 3060 (rev a1)\n"
            "0c:00.0 Display controller: Other GPU\n"
            "0d:00.0 Audio device: NVIDIA Audio\n"
        )
        self.assertEqual(self.detect("Linux", listing), [
            "AMD Radeon RX 580 (rev ef)", "NVIDIA GeForce RTX 3060 (rev a1)", "Other GPU",
        ])

    def test_windows_retains_multiple_identical_cards(self):
        self.assertEqual(self.detect("Windows", "Name\r\nRTX 3060\r\nRTX 3060\r\n"),
                         ["RTX 3060", "RTX 3060"])

    def test_windows_uses_cim_when_wmic_is_missing_or_empty(self):
        for first in (FileNotFoundError("wmic"), "Name\r\n"):
            with self.subTest(first=first), patch("platform.system", return_value="Windows"), \
                 patch("subprocess.check_output", side_effect=[first, "AMD\nNVIDIA\n"]) as run:
                self.assertEqual(_system_gpus(), ["AMD", "NVIDIA"])
                self.assertIn("Get-CimInstance Win32_VideoController", run.call_args.args[0][-1])

    def test_macos_lists_all_chipsets(self):
        self.assertEqual(self.detect("Darwin", "Graphics/Displays:\n  Chipset Model: Intel UHD\n"
                                    "  Chipset Model: AMD Radeon\n"), ["Intel UHD", "AMD Radeon"])

    def test_unavailable_detection_is_nonfatal(self):
        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.check_output", side_effect=FileNotFoundError):
            self.assertEqual(_system_gpus(), [])


class OnnxDiagnosticsTests(unittest.TestCase):
    def test_available_providers_do_not_create_a_session_or_preload_cuda(self):
        ort = SimpleNamespace(__version__="1.31.0", get_available_providers=Mock(
            return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]),
            InferenceSession=Mock(), preload_dlls=Mock())
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            result = _onnx_diagnostics()
        self.assertEqual(result["version"], "1.31.0")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["providers"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        ort.InferenceSession.assert_not_called()
        ort.preload_dlls.assert_not_called()

    def test_missing_onnx_is_optional(self):
        with patch.dict(sys.modules, {"onnxruntime": None}):
            result = _onnx_diagnostics()
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["error"], "")

    def test_broken_native_import_is_reported(self):
        with patch("builtins.__import__", side_effect=OSError("libcudnn.so cannot be loaded")):
            result = _onnx_diagnostics()
        self.assertEqual(result["status"], "error")
        self.assertIn("libcudnn.so", result["error"])

    def test_missing_transitive_dependency_is_not_reported_as_missing_onnx(self):
        with patch("builtins.__import__", side_effect=ModuleNotFoundError("No numpy", name="numpy")):
            result = _onnx_diagnostics()
        self.assertEqual(result["status"], "error")

    def test_provider_query_failure_preserves_version(self):
        ort = SimpleNamespace(__version__="1.31.0", get_available_providers=Mock(
            side_effect=RuntimeError("Provider query failed")))
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            result = _onnx_diagnostics()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["version"], "1.31.0")
        self.assertEqual(result["error"], "Provider query failed")


class AutoTaggerDiagnosticsTests(unittest.TestCase):
    def module(self, session=None):
        return SimpleNamespace(setting=Mock(return_value="CUDA"),
                               _tagger=SimpleNamespace(_session=session, device="CUDAExecutionProvider"),
                               _runtime_status=Mock(), _get_tagger=Mock())

    def test_module_not_loaded_is_distinct_from_idle(self):
        self.assertEqual(_auto_tagger_diagnostics({})["state"], "not_loaded")
        module = self.module()
        module._tagger = None
        result = _auto_tagger_diagnostics({"auto_tagger": module})
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["requested"], "CUDA")
        self.assertEqual(result["active"], "")
        module._get_tagger.assert_not_called()
        module._runtime_status.assert_not_called()

    def test_loaded_session_reports_cpu_fallback_even_when_cuda_was_requested(self):
        module = self.module(SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"]))
        result = _auto_tagger_diagnostics({"auto_tagger": module})
        self.assertEqual(result["state"], "loaded")
        self.assertEqual(result["requested"], "CUDA")
        self.assertEqual(result["active"], "CPUExecutionProvider")

    def test_loaded_cuda_session_reports_actual_provider_order(self):
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        module = self.module(SimpleNamespace(get_providers=lambda: providers))
        result = _auto_tagger_diagnostics({"auto_tagger": module})
        self.assertEqual(result["active"], "CUDAExecutionProvider")
        self.assertEqual(result["providers"], providers)

    def test_unloaded_session_does_not_report_stale_device(self):
        self.assertEqual(_auto_tagger_diagnostics({"auto_tagger": self.module()})["active"], "")

    def test_session_errors_do_not_break_the_report(self):
        module = self.module(SimpleNamespace(get_providers=Mock(side_effect=RuntimeError("Session error"))))
        result = _auto_tagger_diagnostics({"auto_tagger": module})
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error"], "Session error")


class SystemInfoEndpointTests(unittest.TestCase):
    def test_endpoint_reports_current_interpreter_and_preserves_gpu_string(self):
        hub = SimpleNamespace(registry=SimpleNamespace(modules={}))
        module = SettingsModule(hub)
        handler = SimpleNamespace(respond_json=Mock())
        with patch("modules.settings._system_gpus", return_value=["AMD", "NVIDIA"]), \
             patch("modules.settings._onnx_diagnostics", return_value={"status": "missing"}), \
             patch("platform.processor", return_value="Test CPU processor"), \
             patch("subprocess.check_output", side_effect=OSError("Not available in fixture")), \
             patch.object(module, "_resource_status", return_value=[]):
            module._api_system_info(handler, {})
        payload = handler.respond_json.call_args.args[0]
        self.assertEqual(payload["python_executable"], sys.executable)
        self.assertEqual(payload["gpus"], ["AMD", "NVIDIA"])
        self.assertEqual(payload["gpu"], "AMD; NVIDIA")
        self.assertEqual(payload["onnx_runtime"]["status"], "missing")
        self.assertEqual(payload["auto_tagger"]["state"], "not_loaded")


if __name__ == "__main__":
    unittest.main()
