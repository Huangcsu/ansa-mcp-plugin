"""受控识别的计划、保护与失败恢复回归。"""

import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import entity_catalog as catalog
from ansa_mcp_bridge.operations import model_feature_recognition as recognition
from tests.unit.test_entity_catalog import FakeBase
from tests.unit.test_volume_meshing import FakeState


class RecognitionChecks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = FakeBase()
        base = self.base
        self.current = self.root / "input.ansa"
        self.current.write_bytes(b"input database")
        base.DataBaseName = lambda: str(self.current)
        self.backup_calls = []

        def save(path, **kwargs):
            self.backup_calls.append(path)
            Path(path).write_bytes(b"new backup")
            return 0

        base.SaveAs = save
        part1 = base.add("ANSAPART", 1, Name="target", **{"Features Status": "Not Recognized"})
        part2 = base.add("ANSAPART", 2, Name="protected", **{"Features Status": "Recognized"})
        face1 = base.add("FACE", 10, ID=10, PID=1)
        face2 = base.add("FACE", 20, ID=20, PID=1)
        base.GetEntityPart = lambda entity: {10: part1, 20: part2}[entity._id]
        old = base.add("FILLET", 9, **{"Min. Radius": 2.0})
        self.by_part = {1: [], 2: [old]}
        self.contents = {("FILLET", 9): [face2]}
        self.settings = {key: "false" if key in recognition._TOGGLES else "20." for key in recognition._SETTING_KEYS}
        self.settings.update({key: "false" if type(value) is bool else value for key, value in recognition._FIXED.items()})
        self.settings.update(mesh_change_option="mesh", element_type="Mixed", element_order="1st Order")
        self.initial = dict(self.settings)
        base.BCSettingsGetValues = lambda keys: {key: self.settings[key] for key in keys}
        self.settings_calls = []

        def set_settings(values):
            self.settings_calls.append(dict(values))
            self.settings.update(values)
            return 0

        base.BCSettingsSetValues = set_settings
        original_collect = base.CollectEntities

        def collect(deck, container, kind, *args, **kwargs):
            if container is part1:
                return [face1] if kind == "FACE" else []
            if container is part2:
                return [face2] if kind == "FACE" else []
            if container is not None:
                return self.contents.get((container._type, container._id), [])
            return original_collect(deck, None, kind)

        base.CollectEntities = collect
        self.native_calls = 0
        self.after_recognize = None

        def recognize(lock_fe_perimeters=False):
            self.assertTrue(lock_fe_perimeters)
            self.native_calls += 1
            part1.fields["Features Status"] = "Recognized"
            new = base.add("FILLET", 1, **{"Min. Radius": 1.5})
            self.contents[("FILLET", 1)] = [face1]
            self.by_part[1] = [new]
            if self.after_recognize:
                self.after_recognize()

        base.FeatureHandler = lambda parts: types.SimpleNamespace(
            get_features_of_part=lambda part: self.by_part[part._id], recognize=recognize)

        def save_params(path):
            Path(path).write_text(json.dumps(self.settings), encoding="utf-8")
            return 1

        self.restore_failure = False

        def read_params(path):
            self.settings.update(json.loads(Path(path).read_text(encoding="utf-8")))
            return 0 if self.restore_failure else 1

        ansa = types.ModuleType("ansa")
        ansa.base, ansa.constants = base, types.SimpleNamespace(NASTRAN=1)
        ansa.mesh = types.SimpleNamespace(SaveMeshParams=save_params, ReadMeshParams=read_params)
        self.state = FakeState(base)
        self.state.session_dir = self.root
        self.state.model_epoch = 1
        self.state.instance_id, self.state.session_id = "instance", "session"
        module_patch = patch.dict(sys.modules, {"ansa": ansa})
        module_patch.start()
        self.addCleanup(module_patch.stop)

        def scope(_base, deck):
            rows = [{"entity": catalog._entity_reference(base, deck, entity),
                     "fields": copy.deepcopy(entity.fields), "card_fields": list(entity.fields)}
                    for entity in base.entities.values()]
            return {"complete": True, "deck": deck, "entities": rows}

        snapshot_patch = patch.object(recognition.native, "_capture_scope", side_effect=scope)
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.params = {"deck": 1, "part_ids": [1], "feature_types": ["FILLET"],
                       "parameters": {"fillet_max_radius": 5.0, "fillet_min_angle": 20.0}}

    def plan(self):
        return recognition.execute(self.state, "model_features_recognize_plan", self.params)

    def apply(self, plan=None):
        planned = self.plan() if plan is None else plan
        return recognition.execute(self.state, "model_features_recognize", {
            **self.params, "expected_plan_sha256": planned["plan_sha256"],
            "backup_path": str(self.root / "backup.ansa"),
        })

    def test_plan_is_read_only_and_apply_preserves_existing_features_and_settings(self):
        plan = self.plan()
        self.assertTrue(plan["read_only"])
        self.assertEqual(self.backup_calls, [])
        self.assertEqual(self.settings_calls, [])
        result = self.apply(plan)
        self.assertTrue(result["ok"], result["error"])
        self.assertTrue(result["backup"]["verified"])
        self.assertTrue(result["settings_restored"])
        self.assertEqual(self.settings, self.initial)
        self.assertEqual(result["created_counts_by_type"], {"FILLET": 1})
        self.assertEqual(result["created_features"][0]["contents"][0]["id"], 10)
        self.assertEqual(self.native_calls, 1)

    def test_solid_selection_binds_mode_and_keeps_other_native_categories_disabled(self):
        self.params.update(modelling="Solid", feature_types=["HOLE 3D", "FLANGE 3D"], parameters={})
        plan = self.plan()
        self.assertEqual(plan["modelling"], "Solid")
        settings = plan["settings_to_apply"]
        self.assertEqual(settings["modelling_mesh_type"], "Solid Modelling")
        self.assertTrue(settings["recognize_holes_3d"])
        self.assertTrue(settings["recognize_flanges_3d"])
        self.assertFalse(settings["recognize_holes_2d"])
        self.assertFalse(settings["recognize_fasteners"])
        self.params["feature_types"] = ["FLANGE 2D"]
        with self.assertRaisesRegex(ValueError, "Solid modelling does not support"):
            self.plan()
        self.assertEqual(self.settings_calls, [])

    def test_fastener_plan_with_no_configured_dm_fails_before_backup(self):
        self.params.update(
            modelling="Solid",
            feature_types=["FASTENER"],
            parameters={"fastener_embedded_bolts": False, "fastener_embedded_clips": False},
        )
        ansa = sys.modules["ansa"]
        query = lambda *args, **kwargs: {"error": 0, "output": []}
        with patch.object(ansa, "dm", types.SimpleNamespace(
            GetRoot=lambda: "", QueryDMObjects=query
        ), create=True):
            with self.assertRaisesRegex(ValueError, "configured ANSA DM root"):
                self.plan()
        with patch.object(ansa, "dm", types.SimpleNamespace(
            GetRoot=lambda: "C:/test-dm", QueryDMObjects=query
        ), create=True):
            with self.assertRaisesRegex(ValueError, "populated DM Fastener"):
                self.plan()
        self.assertEqual(self.backup_calls, [])
        self.assertEqual(self.native_calls, 0)

    def test_extended_feature_with_unreadable_contents_rejects_plan(self):
        stamp = self.base.add("STAMP", 3, Height=2.0)
        self.by_part[2].append(stamp)
        with self.assertRaisesRegex(RuntimeError, "contents are not readable"):
            self.plan()
        self.assertEqual(self.backup_calls, [])

    def test_new_feature_member_on_unselected_part_fails_scope_protection(self):
        def attach_outside_face():
            self.contents[("FILLET", 1)] = [self.base.entities[("FACE", 20)]]

        self.after_recognize = attach_outside_face
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["recovery_required"])
        self.assertIn("unselected Part", result["error"])

    def test_existing_inactive_category_rejects_before_settings_or_model_write(self):
        stamp = self.base.add("STAMP", 3, Height=2.0)
        self.by_part[1].append(stamp)
        self.contents[("STAMP", 3)] = [self.base.entities[("FACE", 10)]]
        self.params.update(modelling="Solid", feature_types=["HOLE 3D"], parameters={})
        with self.assertRaisesRegex(ValueError, "clear inactive.*Part 1: STAMP"):
            self.plan()
        self.assertEqual(self.settings_calls, [])
        self.assertEqual(self.backup_calls, [])
        self.assertEqual(self.native_calls, 0)

    def test_stale_scope_or_settings_reject_before_backup(self):
        for setting in (False, True):
            with self.subTest(setting=setting):
                plan = self.plan()
                if setting:
                    self.settings["recognize_fillets_max_radius"] = "80."
                else:
                    self.base.entities[("FACE", 10)].fields["PID"] = 8
                with self.assertRaisesRegex(ValueError, "stale"):
                    self.apply(plan)
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(self.backup_calls, [])

    def test_native_partial_failure_keeps_error_after_readback_and_restores_settings(self):
        def fail():
            raise RuntimeError("native failed after Feature creation")
        self.after_recognize = fail
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertIn("native failed", result["error"])
        self.assertTrue(result["recovery_required"])
        self.assertTrue(result["settings_restored"])
        self.assertTrue(result["after"]["features"])
        self.assertEqual(self.native_calls, 1)

    def test_mesh_parameter_restore_failure_cannot_report_success(self):
        self.restore_failure = True
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertFalse(result["settings_restored"])
        self.assertTrue(result["scope_verified"])
        self.assertIn("restoration", result["error"])

    def test_protected_card_or_existing_contents_changes_are_reported(self):
        before = recognition._capture(self.base, 1)
        self.base.entities[("FACE", 20)].fields["PID"] = 2
        change = recognition._transition(before, recognition._capture(self.base, 1), self.params)
        self.assertFalse(change["scope_verified"])
        self.assertIn("protected model", ";".join(change["errors"]))
        self.base.entities[("FACE", 20)].fields["PID"] = 1
        self.contents[("FILLET", 9)] = [self.base.entities[("FACE", 10)]]
        change = recognition._transition(before, recognition._capture(self.base, 1), self.params)
        self.assertFalse(change["scope_verified"])
        self.assertIn("existing Feature", ";".join(change["errors"]))

    def test_backup_failure_prevents_native_call_and_preserves_recovery_evidence(self):
        self.base.SaveAs = lambda *args, **kwargs: 1
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertFalse(result["api_called"])
        self.assertFalse(result["backup"]["verified"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(self.settings_calls, [])
        self.assertEqual(self.native_calls, 0)

    def test_part_with_beam_is_rejected_before_backup(self):
        beam = self.base.add("CBEAM", 30, PID=1)
        original = self.base.CollectEntities

        def collect(deck, container, kind, *args, **kwargs):
            if container is self.base.entities[("ANSAPART", 1)] and kind == "__ELEMENTS__":
                return [beam]
            return original(deck, container, kind, *args, **kwargs)

        with patch.object(self.base, "CollectEntities", side_effect=collect):
            with self.assertRaisesRegex(ValueError, "CAD-only"):
                self.plan()
        self.assertEqual(self.backup_calls, [])

    def test_shell_mesh_perimeters_must_be_new_and_confined_to_selected_parts(self):
        def perimeter(ident, shell_ids):
            return {
                "entity": {"type": "FE PERIMETER", "id": ident},
                "adjacent_shell_ids": shell_ids,
            }

        before = {
            "scope": {"entities": [
                {"entity": {"type": "SHELL", "id": 10}, "part_id": 1},
                {"entity": {"type": "SHELL", "id": 20}, "part_id": 2},
                perimeter(1, [20]),
            ]},
            "features": [],
            "parts": [{"part": {"id": 1}, "features_status": "Recognized"}],
        }
        request = {"part_ids": [1], "feature_types": ["RIB 2D"], "input_kind": "shell_mesh"}
        after = copy.deepcopy(before)
        after["scope"]["entities"].append(perimeter(2, [10]))
        result = recognition._transition(before, after, request)
        self.assertTrue(result["scope_verified"], result["errors"])
        self.assertEqual(result["created_fe_perimeters"][0]["part_ids"], [1])

        after["scope"]["entities"][-1]["adjacent_shell_ids"].append(20)
        result = recognition._transition(before, after, request)
        self.assertFalse(result["scope_verified"])
        self.assertIn("unselected Part ownership", result["errors"][0])

        after["scope"]["entities"][-1]["adjacent_shell_ids"] = [10]
        after["scope"]["entities"][2]["adjacent_shell_ids"] = [10, 20]
        result = recognition._transition(before, after, request)
        self.assertFalse(result["scope_verified"])
        self.assertIn("protected model entity changed", result["errors"][0])

    def test_many_protected_changes_return_bounded_failure_receipt_and_full_evidence(self):
        for entity_id in range(100, 4100):
            self.base.add("GRID", entity_id, X1=0.0)

        def mutate():
            for (kind, _), entity in self.base.entities.items():
                if kind == "GRID":
                    entity.fields["X1"] = 1.0

        self.after_recognize = mutate
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertTrue(result["result_paged"])
        self.assertTrue(result["recovery_required"])
        self.assertLessEqual(len(result["error"]), 4096)
        stored = json.loads((self.root / "structured-mesh-evidence" / (result["report_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(len(stored["result"]["errors"]), 4000)


if __name__ == "__main__":
    unittest.main()
