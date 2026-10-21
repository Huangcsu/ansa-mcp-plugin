"""Regression checks for temporary native quality-criteria synchronization."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ansa_mcp_bridge.operations import native_quality


ORIGINAL = """ANSA_Version = 2026.1.0
failed_index_shells = 1
failed_index_solids = 1
Jacobian definition = 0.1, 2.0
aspect ratio [shells] = OFF, average : 0XFFFFFF | 1. | 1.0, 2.0 / 3.0, 4.0 / 5.0, 6.0 / 7.0, 8.0
tetra collapse [solids] = ON, volume : 0XFFFFFF | 1. | 0.1, -1.0 / 0.2, -1.0 / 0.3, -1.0 / 0.4, -1.0
"""

EFFECTIVE = """ANSA_Version = 2026.1.0
failed_index_shells = 1
failed_index_solids = 1
Jacobian definition = 0.1, 2.0
aspect ratio [shells] = ON, average : 0XFFFFFF | 1. | 1.0, 2.0 / 3.0, 4.0 / 10.0, 6.0 / 7.0, 8.0
tetra collapse [solids] = ON, volume : 0XFFFFFF | 1. | 0.1, -1.0 / 0.2, -1.0 / 0.3, -1.0 / 0.4, -1.0
"""


class FakeCheck:
    def __init__(self, base):
        self.base = base

    def parameters(self):
        return {"quality_criteria": self.base.criteria}


class FakeBase:
    def __init__(self, criteria=ORIGINAL):
        self.criteria = criteria
        self.checks = types.SimpleNamespace(
            mesh=types.SimpleNamespace(MeshQuality=lambda: FakeCheck(self))
        )


class FakeMesh:
    def __init__(self, base):
        self.base = base
        self.read_calls = []
        self.apply_effective = True
        self.apply_backup = True
        self.raise_after_effective = False
        self.effective_transform = None
        self.backup_transform = None

    def SaveQualityCriteria(self, filename):
        Path(filename).write_text(self.base.criteria, encoding="utf-8")
        return 1

    def ReadQualityCriteria(self, filename):
        path = Path(filename)
        self.read_calls.append(path.name)
        text = path.read_text(encoding="utf-8")
        if path.name == "effective.ansa_qual":
            if self.apply_effective:
                self.base.criteria = (self.effective_transform(text)
                                      if self.effective_transform else text)
            if self.raise_after_effective:
                raise RuntimeError("simulated read failure after partial apply")
        elif self.apply_backup:
            self.base.criteria = self.backup_transform(text) if self.backup_transform else text
        return 1


class FakeHeader:
    def __init__(self, base, error=None):
        self.base = base
        self.error = error
        self.calls = 0
        self.criteria_during_fix = None

    def try_fix(self, request_gui=False):
        self.calls += 1
        self.request_gui = request_gui
        self.criteria_during_fix = self.base.criteria
        if self.error:
            raise self.error
        return "fixed"


class QualityCriteriaSyncTests(unittest.TestCase):
    def make_fixture(self, *, fix_error=None):
        base = FakeBase()
        mesh = FakeMesh(base)
        ansa = types.ModuleType("ansa")
        ansa.mesh = mesh
        module_patch = patch.dict(sys.modules, {"ansa": ansa})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        result = {
            "criteria_synchronized": False,
            "api_invoked": False,
            "criteria_restored": False,
        }
        return base, mesh, FakeHeader(base, fix_error), result

    def run_fix(self, base, header, result):
        return native_quality._run_fix_with_criteria(
            base, header, EFFECTIVE, ORIGINAL, result
        )

    def test_failed_backup_is_not_read_and_fix_is_not_invoked(self):
        base, mesh, header, result = self.make_fixture()

        def failed_save(filename):
            Path(filename).write_text("partial invalid backup", encoding="utf-8")
            return 0

        with patch.object(mesh, "SaveQualityCriteria", side_effect=failed_save):
            with self.assertRaisesRegex(RuntimeError, "failed to save the original"):
                self.run_fix(base, header, result)

        self.assertEqual(mesh.read_calls, [])
        self.assertEqual(header.calls, 0)
        self.assertEqual(base.criteria, ORIGINAL)
        self.assertFalse(result["api_invoked"])

    def test_fix_sees_effective_criteria_and_original_is_restored(self):
        base, mesh, header, result = self.make_fixture()

        def serialization_variants(text):
            return (text.replace("failed_index_shells = 1", "failed_index_shells = 1.0")
                    .replace("= ON, average :", " =   ON,   average   :")
                    .replace("10.0", "1.000e+1"))

        mesh.effective_transform = serialization_variants

        self.assertEqual(self.run_fix(base, header, result), "fixed")

        self.assertTrue(native_quality._criteria_matches(EFFECTIVE, header.criteria_during_fix))
        self.assertFalse(native_quality._criteria_matches(ORIGINAL, header.criteria_during_fix))
        self.assertEqual(header.request_gui, False)
        self.assertEqual(header.calls, 1)
        self.assertEqual(base.criteria, ORIGINAL)
        self.assertEqual(mesh.read_calls, ["effective.ansa_qual", "original.ansa_qual"])
        self.assertEqual(result, {
            "criteria_synchronized": True,
            "api_invoked": True,
            "criteria_restored": True,
        })

    def test_fix_exception_still_restores_original_criteria(self):
        base, mesh, header, result = self.make_fixture(fix_error=RuntimeError("fix failed"))

        with self.assertRaisesRegex(RuntimeError, "fix failed"):
            self.run_fix(base, header, result)

        self.assertEqual(header.calls, 1)
        self.assertEqual(base.criteria, ORIGINAL)
        self.assertEqual(mesh.read_calls, ["effective.ansa_qual", "original.ansa_qual"])
        self.assertTrue(result["criteria_synchronized"])
        self.assertTrue(result["api_invoked"])
        self.assertTrue(result["criteria_restored"])

    def test_row_configuration_change_is_not_accepted_as_synchronized(self):
        base, mesh, header, result = self.make_fixture()
        mesh.effective_transform = lambda text: text.replace("| 1. |", "| 0. |")
        with self.assertRaisesRegex(RuntimeError, "did not match after applying"):
            self.run_fix(base, header, result)
        self.assertEqual(header.calls, 0)
        self.assertTrue(result["criteria_restored"])

    def test_double_failure_keeps_fix_error_and_recovery_criteria(self):
        base, mesh, header, result = self.make_fixture(fix_error=ValueError("original fix failure"))
        mesh.apply_backup = False
        with self.assertRaisesRegex(RuntimeError, "failed to restore original"):
            self.run_fix(base, header, result)
        self.assertIn("original fix failure", result["repair_error"])
        self.assertIn("restoring original", result["criteria_restore_error"])
        retained = Path(result["criteria_backup_path"])
        self.addCleanup(retained.unlink)
        self.assertEqual(retained.read_text(encoding="utf-8"), ORIGINAL)

    def test_inactive_calculation_is_restored_via_native_setter_and_getter(self):
        base, mesh, header, result = self.make_fixture()
        extra = "distance from perimeters [shells] = OFF, DISTORTION : 0XFFFFFF | 1. | " + "0.001, -1. / " * 4 + "\n"
        original, effective = ORIGINAL + extra, EFFECTIVE + extra
        base.criteria = original
        mesh.effective_transform = mesh.backup_transform = lambda text: text.replace("DISTORTION", "ABSOLUTE")
        calls = []

        def get_options(name):
            row = native_quality._parse(base.criteria)[0][("shells", name)]
            return {"status": 0, "calculation": row["calculation"], "value": 0.001}

        def set_options(name, status, calculation, value):
            self.assertIs(status, False)
            calls.append((name, calculation, value))
            base.criteria = base.criteria.replace("ABSOLUTE", calculation)
            return 1

        base.F11ShellsOptionsGet, base.F11ShellsOptionsSet = get_options, set_options
        native_quality._run_fix_with_criteria(base, header, effective, original, result)
        self.assertEqual(base.criteria, original)
        self.assertEqual(header.calls, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["criteria_restored"])

    def test_repair_accepts_semantically_restored_criteria_without_false_recovery(self):
        from ansa_mcp_bridge.operations import quality_workflow as workflow
        base, mesh, header, _ = self.make_fixture()
        base.DataBaseName = lambda: "current.ansa"
        base.Check = types.SimpleNamespace(EXEC_ON_SELECTED=4, REPORT_NONE=0, KEEP_OLD=8)
        header.entities, header.issues, header.has_fix, header.fix_method = [], [], True, 2
        check = types.SimpleNamespace(parameters=lambda: {"quality_criteria": base.criteria},
                                      is_available_in_deck=lambda _deck: True, execute=lambda **_kw: [header])
        base.checks.mesh.MeshQuality = lambda: check
        entity = types.SimpleNamespace(_id=11)
        node = types.SimpleNamespace(position=(0.0, 0.0, 0.0))
        original_fix = header.try_fix

        def fix(request_gui=False):
            node.position = (0.05, 0.0, 0.0)
            return original_fix(request_gui=request_gui)

        header.try_fix = fix
        mesh.backup_transform = lambda text: text.replace("failed_index_shells = 1", "failed_index_shells = 1.0")
        before = {"complete": True, "failed_entity_ids": [11], "entity_type": "SHELL",
                  "geometry_sha256": "g", "rows": [{"id": 11, "violations": ["ASPECT"]}]}
        after = {**before, "failed_entity_ids": [], "rows": [{"id": 11, "violations": []}]}

        def snapshot(*_args):
            return {"elements": [{"entity": {"type": "SHELL", "id": 11}, "node_ids": [101]}],
                    "nodes": {"101": {"position": list(node.position),
                                      "card": {"fields": dict(zip(("X", "Y", "Z"), node.position))}}}}

        with patch.object(workflow, "_custom_criteria", return_value=EFFECTIVE), \
             patch.object(workflow, "_scope", return_value=([entity], {101: node}, {}, "g")), \
             patch.object(workflow, "_fe_snapshot", side_effect=snapshot), \
             patch.object(workflow, "_validity", return_value=[{"id": 11, "measure": 1.0, "jacobian": 1.0}]), \
             patch.object(workflow, "_report", return_value=(after, [entity], {101: node}, {})), \
             patch("ansa_mcp_bridge.operations._model_save", return_value={"path": "backup.ansa"}), \
             patch("ansa_mcp_bridge.operations.quality_options.run_with_options", side_effect=lambda base, family, maximum, callback, result: callback()):
            result = workflow._repair(None, base, types.SimpleNamespace(), 1,
                {"movable_node_ids": [101], "maximum_displacement": 0.1,
                 "method": "fix_quality", "backup_path": "backup.ansa"},
                {"element_family": "shell"}, [11], before, [entity], {101: node})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["applied"])
        self.assertTrue(result["criteria_restored"])
        self.assertFalse(result.get("recovery_required", False))
        self.assertNotEqual(base.criteria, ORIGINAL)
        self.assertTrue(native_quality._criteria_matches(ORIGINAL, base.criteria))

    def test_read_returning_one_without_applying_is_rejected_and_restored(self):
        base, mesh, header, result = self.make_fixture()
        mesh.apply_effective = False

        with self.assertRaisesRegex(RuntimeError, "did not match after applying"):
            self.run_fix(base, header, result)

        self.assertEqual(header.calls, 0)
        self.assertEqual(base.criteria, ORIGINAL)
        self.assertFalse(result["criteria_synchronized"])
        self.assertFalse(result["api_invoked"])
        self.assertTrue(result["criteria_restored"])

    def test_readback_rejects_changed_algorithm_and_threshold(self):
        for transform, label in (
            (lambda text: text.replace(", average :", ", median :"), "algorithm"),
            (lambda text: text.replace("10.0", "10.125"), "threshold"),
        ):
            with self.subTest(mismatch=label):
                base, mesh, header, result = self.make_fixture()
                mesh.effective_transform = transform

                with self.assertRaisesRegex(RuntimeError, "did not match after applying"):
                    self.run_fix(base, header, result)

                self.assertEqual(header.calls, 0)
                self.assertEqual(base.criteria, ORIGINAL)
                self.assertTrue(result["criteria_restored"])

    def test_read_exception_after_partial_apply_still_restores(self):
        base, mesh, header, result = self.make_fixture()
        mesh.raise_after_effective = True

        with self.assertRaisesRegex(RuntimeError, "partial apply"):
            self.run_fix(base, header, result)

        self.assertEqual(header.calls, 0)
        self.assertEqual(base.criteria, ORIGINAL)
        self.assertEqual(mesh.read_calls, ["effective.ansa_qual", "original.ansa_qual"])
        self.assertTrue(result["criteria_restored"])
        self.assertFalse(result["criteria_synchronized"])
        self.assertFalse(result["api_invoked"])

    def test_restore_returning_one_without_restoring_raises_and_is_not_reported(self):
        base, mesh, header, result = self.make_fixture()
        mesh.apply_backup = False

        with self.assertRaisesRegex(RuntimeError, "failed to restore original"):
            self.run_fix(base, header, result)

        self.assertEqual(header.calls, 1)
        self.assertFalse(result["criteria_restored"])
        self.assertTrue(result["criteria_synchronized"])
        self.assertTrue(result["api_invoked"])
        retained = Path(result["criteria_backup_path"])
        self.addCleanup(retained.unlink)
        self.assertEqual(retained.read_text(encoding="utf-8"), ORIGINAL)


if __name__ == "__main__":
    unittest.main()
