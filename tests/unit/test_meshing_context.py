import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ansa_mcp_bridge.operations.meshing_context import (
    MeshingContextError,
    capture,
    run_with_context,
)
from ansa_mcp_bridge.operations import quality_options


_CRITERIA = """ANSA_Version = 2026.1.0
failed_index_shells = 1
failed_index_solids = 1
Jacobian definition = 0.1, 2.0
aspect ratio [shells] = ON, NASTRAN : 0XFFFFFF | 1. | 1.0, 2.0 / 3.0, 4.0 / 5.0, 6.0 / 7.0, 8.0
min length [shells] = OFF, : 0XFFFFFF | 1. | 0.1, -1.0 / 0.2, -1.0 / 0.3, -1.0 / 0.4, -1.0
tetra collapse [solids] = ON, volume : 0XFFFFFF | 1. | 0.1, -1.0 / 0.2, -1.0 / 0.3, -1.0 / 0.4, -1.0
"""


class FakeState:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir)
        self.assert_count = 0

    def assert_main(self):
        self.assert_count += 1


class FakeQuality:
    def __init__(self, base):
        self.base = base

    def parameters(self):
        return {"quality_criteria": self.base.criteria}


class FakeBase:
    def __init__(self):
        self.settings = {
            "mesh_change_option": "All",
            "element_type": "Tria",
            "element_order": "2nd Order",
            "target_element_length": "2.5",
        }
        self.criteria = _CRITERIA
        self.options = {
            "shells": {
                "aspect ratio": {"status": 1, "calculation": "NASTRAN", "value": 3.0},
                "min length": {"status": 0, "calculation": 0, "value": 0.25},
            },
            "solids": {
                "tetra collapse": {"status": True, "calculation": "volume", "value": 0.4},
            },
        }
        self.getter_override = {}
        self.settings_getter_override = {}
        self.setter_calls = []
        self.settings_set_calls = []
        self.checks = SimpleNamespace(
            mesh=SimpleNamespace(MeshQuality=lambda: FakeQuality(self))
        )

    def BCSettingsGetValues(self, keys):
        return {
            key: self.settings_getter_override.get(key, self.settings[key])
            for key in keys
        }

    def BCSettingsSetValues(self, values):
        self.settings_set_calls.append(dict(values))
        self.settings.update(values)
        return 0

    def _get_f11(self, family, name):
        if (family, name) in self.getter_override:
            return self.getter_override[(family, name)]
        return dict(self.options[family][name])

    def _set_f11(self, family, name, status, calculation, value):
        self.setter_calls.append((family, name, status, calculation, value))
        prior = self.options[family][name]
        self.options[family][name] = {
            "status": status,
            "calculation": prior["calculation"] if calculation == "" else calculation,
            "value": float(value),
        }
        return 1

    def F11ShellsOptionsGet(self, name):
        return self._get_f11("shells", name)

    def F11SolidsOptionsGet(self, name):
        return self._get_f11("solids", name)

    def F11ShellsOptionsSet(self, name, status, calculation, value):
        return self._set_f11("shells", name, status, calculation, value)

    def F11SolidsOptionsSet(self, name, status, calculation, value):
        return self._set_f11("solids", name, status, calculation, value)


class FakeMesh:
    def __init__(self, base):
        self.base = base
        self.extra_option = "Preserve"
        self.save_mesh_count = 0
        self.read_mesh_count = 0
        self.save_quality_count = 0
        self.read_quality_count = 0
        self.fail_read_mesh_at = None

    def SaveMeshParams(self, filename):
        self.save_mesh_count += 1
        values = [
            (key, self.base.settings[key])
            for key in (
                "mesh_change_option",
                "element_type",
                "element_order",
                "target_element_length",
            )
        ]
        values.append(("unrelated_option", self.extra_option))
        Path(filename).write_text(
            "".join(f"{key} = {value}\n" for key, value in values),
            encoding="utf-8",
        )
        return 1

    def ReadMeshParams(self, filename):
        self.read_mesh_count += 1
        if self.read_mesh_count == self.fail_read_mesh_at:
            return 0
        values = quality_options._parameter_values(
            Path(filename).read_text(encoding="utf-8")
        )
        for key in self.base.settings:
            self.base.settings[key] = values[key][-1]
        self.extra_option = values["unrelated_option"][-1]
        return 1

    def SaveQualityCriteria(self, filename):
        self.save_quality_count += 1
        Path(filename).write_text(self.base.criteria, encoding="utf-8")
        return 1

    def ReadQualityCriteria(self, filename):
        self.read_quality_count += 1
        self.base.criteria = Path(filename).read_text(encoding="utf-8")
        return 1

    def SetMeshParamTargetLength(self, mode, value):
        if mode != "absolute":
            return 0
        self.base.settings["target_element_length"] = str(value)
        return 1


class MeshingContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[2], prefix=".meshing-context-test-"
        )
        self.state = FakeState(self.temp.name)
        self.base = FakeBase()
        self.mesh = FakeMesh(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def assert_timings(self, result, completed):
        timings = result["timings_ms"]
        self.assertEqual(
            list(timings), ["capture", "apply", "callback", "restore", "total"]
        )
        for stage, elapsed in timings.items():
            if stage in completed:
                self.assertIs(type(elapsed), float)
                self.assertTrue(math.isfinite(elapsed))
                self.assertGreaterEqual(elapsed, 0.0)
            else:
                self.assertIsNone(elapsed)

    def test_callback_runs_with_temporary_settings_then_full_context_restores(self):
        original_settings = dict(self.base.settings)
        seen = []

        def callback():
            seen.append(dict(self.base.settings))
            self.base.settings["mesh_change_option"] = "Changed"
            self.mesh.extra_option = "CallbackChanged"
            self.base.criteria = _CRITERIA.replace(
                "aspect ratio [shells] = ON", "aspect ratio [shells] = OFF"
            )
            self.base.options["shells"]["aspect ratio"]["value"] = 9.0
            return {"called": True}

        result = {}
        returned = run_with_context(
            self.state, self.base, self.mesh, 10.0, callback, result
        )

        self.assertEqual(returned, {"called": True})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["element_type"], "Quad")
        self.assertEqual(seen[0]["element_order"], "1st Order")
        self.assertEqual(float(seen[0]["target_element_length"]), 10.0)
        self.assertEqual(self.base.settings, original_settings)
        self.assertEqual(self.mesh.extra_option, "Preserve")
        self.assertTrue(result["restored"])
        self.assertTrue(result["mpar"]["verified"])
        self.assertTrue(result["criteria"]["verified"])
        self.assertTrue(result["f11"]["verified"])
        self.assertTrue(result["settings"]["verified"])
        self.assertEqual(result["coverage"], "all_current_exported_criteria_rows")
        self.assertEqual(result["counts"]["criteria_rows"], 3)
        self.assertTrue(Path(result["recovery_paths"]["mesh_parameters"]).is_file())
        self.assertTrue(Path(result["recovery_paths"]["quality_criteria"]).is_file())

    def test_mixed_and_tria_settings_restore_after_success_and_callback_failure(self):
        original_settings = dict(self.base.settings)

        for element_type in ("Mixed", "Tria"):
            with self.subTest(element_type=element_type):
                seen = []
                success_result = {}
                run_with_context(
                    self.state,
                    self.base,
                    self.mesh,
                    8.0,
                    lambda: seen.append(dict(self.base.settings)),
                    success_result,
                    element_type=element_type,
                )

                self.assertEqual(seen[0]["element_type"], element_type)
                self.assertEqual(
                    success_result["requested_element_type"], element_type
                )
                self.assertEqual(self.base.settings, original_settings)
                self.assertTrue(success_result["restored"])
                self.assert_timings(
                    success_result,
                    {"capture", "apply", "callback", "restore", "total"},
                )

                calls = []
                failure_result = {}

                def callback_error():
                    calls.append(dict(self.base.settings))
                    self.base.settings["mesh_change_option"] = "Changed"
                    raise RuntimeError("callback failed")

                with self.assertRaises(MeshingContextError) as raised:
                    run_with_context(
                        self.state,
                        self.base,
                        self.mesh,
                        8.0,
                        callback_error,
                        failure_result,
                        element_type=element_type,
                    )

                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["element_type"], element_type)
                self.assertIsInstance(raised.exception.operation_error, RuntimeError)
                self.assertTrue(failure_result["restored"])
                self.assertEqual(self.base.settings, original_settings)
                self.assert_timings(
                    failure_result,
                    {"capture", "apply", "callback", "restore", "total"},
                )

    def test_element_type_readback_mismatch_blocks_callback_and_restores_original_context(self):
        self.base.settings_getter_override["element_type"] = "Tria"
        original_settings = dict(self.base.settings)
        calls = []
        result = {}

        with self.assertRaises(MeshingContextError) as raised:
            run_with_context(
                self.state,
                self.base,
                self.mesh,
                8.0,
                lambda: calls.append(True),
                result,
                element_type="Mixed",
            )

        self.assertIsInstance(raised.exception.operation_error, RuntimeError)
        self.assertEqual(calls, [])
        self.assertEqual(
            self.base.settings_set_calls[0],
            {"element_type": "Mixed", "element_order": "1st Order"},
        )
        self.assertEqual(self.base.settings, original_settings)
        self.assertTrue(result["restored"])
        self.assertTrue(result["settings"]["verified"])
        self.assert_timings(
            result, {"capture", "apply", "restore", "total"}
        )

    def test_invalid_element_type_is_rejected_before_native_capture(self):
        for invalid in ("mixed", object()):
            with self.subTest(invalid=invalid):
                result = {}
                with self.assertRaises(MeshingContextError):
                    run_with_context(
                        self.state,
                        self.base,
                        self.mesh,
                        8.0,
                        lambda: self.fail("callback must not run"),
                        result,
                        element_type=invalid,
                    )

                self.assertEqual(self.state.assert_count, 0)
                self.assertEqual(self.base.settings_set_calls, [])
                self.assertEqual(self.mesh.save_mesh_count, 0)
                self.assertEqual(self.mesh.save_quality_count, 0)
                self.assertEqual(
                    result["requested_element_type"],
                    invalid if isinstance(invalid, str) else None,
                )
                json.dumps(result, allow_nan=False)
                self.assert_timings(result, {"total"})

    def test_callback_error_is_raised_after_restore_and_callback_is_not_retried(self):
        calls = []

        def callback():
            calls.append(1)
            self.base.options["shells"]["aspect ratio"]["value"] = 7.0
            raise RuntimeError("callback failed")

        result = {}
        with self.assertRaises(MeshingContextError) as raised:
            run_with_context(self.state, self.base, self.mesh, 8.0, callback, result)

        self.assertEqual(calls, [1])
        self.assertIsInstance(raised.exception.operation_error, RuntimeError)
        self.assertEqual(raised.exception.restore_errors, [])
        self.assertTrue(result["restored"])
        self.assertEqual(self.base.options["shells"]["aspect ratio"]["value"], 3.0)
        self.assert_timings(
            result, {"capture", "apply", "callback", "restore", "total"}
        )

    def test_mesh_restore_failure_does_not_block_criteria_f11_or_backup_retention(self):
        self.mesh.fail_read_mesh_at = 1

        def callback():
            self.base.criteria = _CRITERIA.replace(
                "aspect ratio [shells] = ON", "aspect ratio [shells] = OFF"
            )
            self.base.options["shells"]["aspect ratio"]["value"] = 8.0
            return None

        result = {}
        with self.assertRaises(MeshingContextError) as raised:
            run_with_context(self.state, self.base, self.mesh, 8.0, callback, result)

        self.assertIsNone(raised.exception.operation_error)
        self.assertTrue(raised.exception.restore_errors)
        self.assertEqual(self.mesh.read_quality_count, 1)
        self.assertTrue(self.base.setter_calls)
        self.assertTrue(result["criteria"]["verified"])
        self.assertTrue(result["f11"]["verified"])
        self.assertFalse(result["mpar"]["verified"])
        self.assertFalse(result["restored"])
        self.assert_timings(
            result, {"capture", "apply", "callback", "restore", "total"}
        )
        backup = Path(result["recovery_paths"]["mesh_parameters"])
        self.assertTrue(backup.is_file())
        self.assertIn("unrelated_option = Preserve", backup.read_text(encoding="utf-8"))

    def test_inactive_calculation_zero_uses_empty_setter_argument_and_gets_zero_back(self):
        def callback():
            self.base.options["shells"]["min length"]["status"] = 1
            self.base.options["shells"]["min length"]["value"] = 6.0

        result = {}
        run_with_context(self.state, self.base, self.mesh, 6.0, callback, result)

        matching = [call for call in self.base.setter_calls if call[1] == "min length"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0][3], "")
        self.assertEqual(self.base.options["shells"]["min length"]["calculation"], 0)
        self.assertTrue(result["f11"]["verified"])

    def test_changed_snapshot_rejects_before_setters_or_callback(self):
        planned = capture(self.state, self.base, self.mesh)
        self.base.options["shells"]["aspect ratio"]["value"] = 4.0
        original_settings = dict(self.base.settings)
        calls = []
        result = {}

        with self.assertRaises(MeshingContextError):
            run_with_context(
                self.state,
                self.base,
                self.mesh,
                5.0,
                lambda: calls.append(True),
                result,
                expected_snapshot_sha256=planned["snapshot_sha256"],
            )

        self.assertEqual(calls, [])
        self.assertEqual(self.base.setter_calls, [])
        self.assertEqual(self.base.settings, original_settings)
        self.assertFalse(result["restoration_attempted"])
        self.assertTrue(result["errors"]["operation_error"])

    def test_invalid_getter_value_refuses_callback_before_any_native_file_save(self):
        self.base.getter_override[("shells", "aspect ratio")] = {
            "status": 1,
            "calculation": None,
            "value": 3.0,
        }
        calls = []
        result = {}

        with self.assertRaises(MeshingContextError):
            run_with_context(
                self.state, self.base, self.mesh, 5.0, lambda: calls.append(True), result
            )

        self.assertEqual(calls, [])
        self.assertEqual(self.mesh.save_mesh_count, 0)
        self.assertEqual(self.mesh.save_quality_count, 0)
        self.assertFalse(result["restoration_attempted"])
        self.assert_timings(result, {"capture", "total"})

        self.base.getter_override[("shells", "aspect ratio")] = {
            "status": "true",
            "calculation": "NASTRAN",
            "value": 3.0,
        }
        with self.assertRaises(MeshingContextError):
            run_with_context(
                self.state, self.base, self.mesh, 5.0, lambda: calls.append(True), {}
            )
        self.assertEqual(calls, [])
        self.assertEqual(self.mesh.save_mesh_count, 0)


if __name__ == "__main__":
    unittest.main()
