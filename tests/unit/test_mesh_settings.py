"""Risk checks for strict shell mesh settings updates and recovery."""

import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import mesh_settings
from ansa_mcp_plugin.mcp.meshing import MeshSettingsChange
from test_entity_catalog import FakeState
from test_meshing import FakeMesh, MeshBase


class SettingsBase(MeshBase):
    def __init__(self):
        super().__init__()
        self.settings_calls = []
        self.partial_failure_call = None
        self.partial_failure_code = 7

    def BCSettingsSetValues(self, values):
        self.settings_calls.append(dict(values))
        status = super().BCSettingsSetValues(values)
        if len(self.settings_calls) == self.partial_failure_call:
            return self.partial_failure_code
        return status


class SettingsMesh(FakeMesh):
    def __init__(self, base):
        super().__init__(base)
        self.save_calls = []
        self.read_calls = []
        self.length_calls = []
        self.partial_length_raise = False
        self.wrong_target_length = False

    def SaveMeshParams(self, filename):
        self.save_calls.append(filename)
        return super().SaveMeshParams(filename)

    def ReadMeshParams(self, filename):
        self.read_calls.append(filename)
        return super().ReadMeshParams(filename)

    def SetMeshParamTargetLength(self, mode, length):
        self.length_calls.append((mode, length))
        if self.partial_length_raise:
            self.base.settings["target_element_length"] = str(length + 10.0)
            raise RuntimeError("target length setter failed after a partial change")
        if self.wrong_target_length:
            self.base.settings["target_element_length"] = str(length * 2.0)
            return 1
        return super().SetMeshParamTargetLength(mode, length)


class MeshSettingsChecks(unittest.TestCase):
    def make_case(self):
        base = SettingsBase()
        mesh = SettingsMesh(base)
        state = FakeState()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state.session_dir = Path(directory.name)
        ansa = types.ModuleType("ansa")
        ansa.base, ansa.mesh = base, mesh
        module_patch = patch.dict(sys.modules, {"ansa": ansa})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        return state, base, mesh

    @staticmethod
    def expected(base):
        return dict(base.settings)

    @staticmethod
    def update(state, base, settings, expected=None):
        return mesh_settings.execute(state, "mesh_settings_update", {
            "expected_settings": dict(expected if expected is not None else base.settings),
            "settings": settings,
        })

    def test_read_returns_snapshot_and_condition_without_any_write(self):
        state, base, mesh = self.make_case()
        before = dict(base.settings)

        result = mesh_settings.execute(state, "mesh_settings_read", {})

        self.assertEqual(result, {"read_only": True, "settings": before, "condition": before})
        self.assertEqual(mesh.save_calls, [])
        self.assertEqual(mesh.length_calls, [])
        self.assertEqual(base.settings_calls, [])
        self.assertEqual(base.settings_writes, 0)

    def test_invalid_or_stale_snapshot_is_rejected_before_backup_or_set(self):
        cases = (
            ("missing", lambda expected: expected.pop("target_element_length")),
            ("unknown", lambda expected: expected.update(unknown="stale")),
            ("stale", lambda expected: expected.update(element_type="Tria")),
            ("non_string", lambda expected: expected.update(element_type=3)),
            ("empty", lambda expected: expected.update(element_type="")),
            ("too_long", lambda expected: expected.update(element_type="x" * 129)),
        )
        for name, alter in cases:
            with self.subTest(name=name):
                state, base, mesh = self.make_case()
                expected = self.expected(base)
                alter(expected)
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    self.update(state, base, {"element_type": "tria"}, expected)
                self.assertEqual(mesh.save_calls, [])
                self.assertEqual(base.settings_calls, [])
                self.assertEqual(mesh.length_calls, [])

        state, base, mesh = self.make_case()
        valid_expected = self.expected(base)
        payloads = (
            {"settings": {"element_type": "tria"}},
            {"expected_settings": valid_expected},
            {"expected_settings": valid_expected, "settings": {"element_type": "tria"}, "extra": True},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    mesh_settings.execute(state, "mesh_settings_update", payload)
        self.assertEqual(mesh.save_calls, [])
        self.assertEqual(base.settings_calls, [])
        self.assertEqual(mesh.length_calls, [])

    def test_invalid_changes_are_rejected_before_backup_or_set(self):
        cases = (
            ("empty", {}),
            ("unknown", {"future_setting": 1}),
            ("mode_is_read_only", {"mesh_change_option": "mesh"}),
            ("element_type", {"element_type": "hex"}),
            ("element_order", {"element_order": "1st order"}),
            ("target_bool", {"target_length": True}),
            ("target_zero", {"target_length": 0}),
            ("target_infinite", {"target_length": float("inf")}),
            ("target_string", {"target_length": "2.0"}),
            ("target_null", {"target_length": None, "element_type": "tria"}),
            ("type_null", {"element_type": None, "element_order": "1st Order"}),
            ("order_null", {"element_order": None, "target_length": 2.0}),
        )
        for name, changes in cases:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    MeshSettingsChange.model_validate(changes)
                state, base, mesh = self.make_case()
                with self.assertRaises((TypeError, ValueError)):
                    self.update(state, base, changes)
                self.assertEqual(mesh.save_calls, [])
                self.assertEqual(base.settings_calls, [])
                self.assertEqual(mesh.length_calls, [])

    def test_single_and_combined_updates_preserve_every_unspecified_setting(self):
        cases = (
            {"element_type": "quad"},
            {"element_order": "1st Order"},
            {"target_length": 3.5},
            {"element_type": "tria", "element_order": "1st Order", "target_length": 4.25},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                state, base, mesh = self.make_case()
                before = dict(base.settings)
                result = self.update(state, base, changes)

                self.assertTrue(result["ok"])
                self.assertTrue(result["persisted_in_session"])
                self.assertFalse(result["defaults_file_written"])
                if "element_type" in changes or "element_order" in changes:
                    self.assertEqual(result["settings_set_code"], 0)
                if "target_length" in changes:
                    self.assertEqual(result["target_length_set_code"], 1)
                self.assertEqual(result["settings_before"], before)
                self.assertEqual(result["settings_after"]["mesh_change_option"], before["mesh_change_option"])
                expected_changed = set()
                if "element_type" in changes:
                    self.assertEqual(result["settings_after"]["element_type"].casefold(), changes["element_type"])
                    if changes["element_type"].casefold() != before["element_type"].casefold():
                        expected_changed.add("element_type")
                if "element_order" in changes:
                    self.assertEqual(result["settings_after"]["element_order"], changes["element_order"])
                    if changes["element_order"] != before["element_order"]:
                        expected_changed.add("element_order")
                if "target_length" in changes:
                    self.assertTrue(math.isclose(
                        float(result["settings_after"]["target_element_length"]), changes["target_length"], rel_tol=1e-9
                    ))
                    expected_changed.add("target_element_length")
                self.assertEqual(set(result["changed"]), expected_changed)
                if "element_type" not in changes:
                    self.assertEqual(result["settings_after"]["element_type"], before["element_type"])
                if "element_order" not in changes:
                    self.assertEqual(result["settings_after"]["element_order"], before["element_order"])
                if "target_length" not in changes:
                    self.assertEqual(result["settings_after"]["target_element_length"], before["target_element_length"])
                backup = Path(result["parameter_backup"])
                self.assertEqual(backup.parent, state.session_dir)
                self.assertEqual(backup.suffix, ".ansa_mpar")
                self.assertTrue(backup.is_file())
                self.assertGreater(backup.stat().st_size, 0)
                self.assertEqual(len(mesh.save_calls), 1)

    def test_partial_native_failure_and_readback_mismatch_restore_and_keep_evidence(self):
        for failure in ("native", "readback"):
            with self.subTest(failure=failure):
                state, base, mesh = self.make_case()
                before = dict(base.settings)
                if failure == "native":
                    base.partial_failure_call = 1
                else:
                    mesh.wrong_target_length = True

                result = self.update(state, base, {"element_type": "tria", "target_length": 2.5})

                self.assertFalse(result["ok"])
                self.assertTrue(result["possible_effects"])
                self.assertIn("settings_after_update", result)
                self.assertEqual(result["settings_after_update"]["element_type"], "Tria")
                self.assertEqual(result["settings_after"], before)
                self.assertTrue(result["parameters_restored"])
                self.assertEqual(result["restore_file_code"], 1)
                self.assertEqual(result["restore_mode_code"], 0)
                self.assertTrue(Path(result["parameter_backup"]).is_file())
                if failure == "native":
                    self.assertEqual(result["settings_set_code"], 7)
                    self.assertEqual(result["changed"], ["element_type"])
                else:
                    self.assertEqual(result["target_length_set_code"], 1)
                    self.assertNotEqual(
                        result["settings_after_update"]["target_element_length"], "2.5"
                    )
                    self.assertIn("readback", result["error"])

    def test_file_restore_exception_still_restores_mode_and_returns_final_evidence(self):
        state, base, mesh = self.make_case()
        before = dict(base.settings)
        mesh.partial_length_raise = True
        mesh.raise_restore = True

        result = self.update(state, base, {"element_type": "tria", "target_length": 4.0})

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertFalse(result["parameters_restored"])
        self.assertIn("settings_after_update", result)
        self.assertEqual(result["settings_after_update"]["element_type"], "Tria")
        self.assertEqual(result["settings_after"]["mesh_change_option"], before["mesh_change_option"])
        self.assertEqual(result["restore_mode_code"], 0)
        self.assertIn("restore_file_error", result)
        self.assertEqual(result["settings_after"]["target_element_length"], "14.0")
        self.assertNotEqual(result["settings_after"], before)
        self.assertTrue(Path(result["parameter_backup"]).is_file())


if __name__ == "__main__":
    unittest.main()
