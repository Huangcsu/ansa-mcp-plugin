"""Risk-focused relationship, stale-state and native freeze readback regressions."""

import copy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import mesh_scope as scope
from ansa_mcp_bridge.operations import structured_evidence
from tests.unit.test_structured_native import FakeBase, FakePreviewState


class State(FakePreviewState):
    instance_id = "instance"
    session_id = "session"
    model_epoch = 4

    def probe(self):
        result = super().probe()
        result["model"].update(epoch=self.model_epoch, write_enabled=True)
        result["persistence_blocked"] = False
        return result


class Model(FakeBase):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fault = None
        self.macro_faces = {1: [1], 2: [2], 3: [3]}
        self.macro_shells = {1: [10], 2: [11], 3: []}
        self.face_cons = {1: [1, 2, 9], 2: [3, 9, 15], 3: [4, 15]}
        self.volume_members = {31: {"FACE": [1, 2], "SHELL": [10, 11], "SOLID": [20]}}
        for ident in (1, 2, 3):
            self.add("FACE", ident, ID=ident, Frozen="NO", area=1., center=[float(ident), 0., 0.])
            self.face_bounds[ident] = [float(ident), 0., 0., float(ident + 1), 1., 0.]
        for ident in (1, 2, 3, 4, 9, 15):
            self.add("CONS", ident, ID=ident, Frozen="NO", **{"Num. of Nodes": 0})
        for ident in range(1, 9):
            self.add("GRID", ident, NID=ident, CP=0, X1=float(ident), X2=0., X3=0.)
        for ident, nodes in ((10, [1, 2, 3, 4]), (11, [3, 4, 5, 6]), (12, [7, 8, 5])):
            self.add("SHELL", ident, EID=ident, PID=1, FROZEN="NO",
                     **{f"G{i + 1}": node for i, node in enumerate(nodes)})
        self.add("SOLID", 20, EID=20, PID=2, FROZEN="NO", **{f"G{i}": i for i in range(1, 9)})
        self.add("VOLUME", 31, ID=31, Frozen="NO", Type="Map Block")

    def GetEntityCardValues(self, deck, entity, fields):
        result = super().GetEntityCardValues(deck, entity, fields)
        for field in fields:
            if field in ("__part__", "__prop__"):
                result[field] = None
        return result

    def CollectEntities(self, deck, container, kind, *_args, **_kwargs):
        if container is not None and container._type == "VOLUME":
            return [self.entities[(kind, ident)] for ident in self.volume_members[container._id].get(kind, [])]
        return super().CollectEntities(deck, container, kind, *_args, **_kwargs)

    def GetFaceMacro(self, face):
        ident = next(mid for mid, members in self.macro_faces.items() if face._id in members)
        return self.entities[("FACE", ident)]

    def GetFacesOfMacros(self, macros):
        return [self.entities[("FACE", ident)] for macro in macros for ident in self.macro_faces[macro._id]]

    def GetFacesOfCons(self, cons):
        return [self.entities[("FACE", ident)] for ident, ids in self.face_cons.items() if cons._id in ids]

    def GetMacroShells(self, macros):
        return [self.entities[("SHELL", ident)] for macro in macros for ident in self.macro_shells[macro._id]] or 0

    def GetShellMacro(self, shells):
        return [self.entities[("FACE", mid)] for mid, ids in self.macro_shells.items() if any(s._id in ids for s in shells)] or 0

    def PerimetersOfMacro(self, macros):
        counts = {}
        for macro in macros:
            for face in self.macro_faces[macro._id]:
                for ident in self.face_cons[face]:
                    counts[ident] = counts.get(ident, 0) + 1
        return [self.entities[("CONS", ident)] for ident, count in sorted(counts.items()) if count == 1]

    def NodesToElements(self, nodes, sources):
        return {node: [e for (kind, _), e in self.entities.items() if kind in {"SHELL", "SOLID"}
                       and any(repr_name.startswith("G") and value == node._id for repr_name, value in e.fields.items())]
                for node in nodes}

    def FreezeElements(self, deck, kind, entities):
        return self._change(entities, "FROZEN", "YES", "freeze")

    def UnFreezeElements(self, deck, kind, entities):
        return self._change(entities, "FROZEN", "NO", "unfreeze")

    def FacesFreezeUnFreeze(self, faces, unfreeze=False):
        value = "NO" if unfreeze else "YES"
        self._change(faces, "Frozen", value, "unfreeze" if unfreeze else "freeze")
        return len(faces)

    def _change(self, entities, field, value, action):
        self.calls.append((action, [(e._type, e._id) for e in entities]))
        for entity in entities[:1] if self.fault == "partial" else entities:
            entity.fields[field] = value
        if self.fault == "outside":
            self.entities[("GRID", 8)].fields["X1"] += 1.
        if self.fault == "exception":
            raise RuntimeError("native interrupted after mutation")
        return self.fault != "false_return"


class MeshScopeTests(unittest.TestCase):
    def setUp(self):
        self.state, self.base = State(), Model()
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.args = {"deck": 1, "entity_type": "FACE", "entity_ids": [1]}

    def capture(self, **changed):
        return scope.capture(self.state, self.base, self.base, self.constants, {**self.args, **changed})

    def apply(self, action="freeze", params=None, backup=None):
        clean = params or self.args
        fingerprint = self.capture(**clean)["scope_sha256"]
        args = {**clean, "action": action, "expected_scope_sha256": fingerprint, "backup_path": "unused.ansa"}
        with patch.object(scope.native, "_backup_preflight", return_value=("backup", "current", {})), \
                patch.object(scope.native, "_verify_backup", side_effect=backup or (lambda *_: {"verified": True})):
            return scope.freeze(self.state, self.base, self.base, self.constants, args)

    def test_relationships_share_cons_without_recursive_geometry_flood_or_seed_materialization(self):
        before = copy.deepcopy([e.fields for e in self.base.entities.values()])
        result = self.capture()
        self.assertEqual([r["entity"]["id"] for r in result["faces"]], [1, 2])
        shared = next(row for row in result["cons"] if row["entity"]["id"] == 9)
        self.assertEqual(shared["face_ids"], [1, 2])
        outer = next(row for row in result["cons"] if row["entity"]["id"] == 15)
        self.assertEqual(outer["unexpanded_face_ids"], [3])
        self.assertEqual(result["freeze_targets"]["FACE"], [1])
        self.assertEqual(result["freeze_targets"]["CONS"], [])
        self.assertEqual(result["freeze_effects"]["cons_ids"], [1, 2, 9])
        self.assertEqual(result["volumes"][0]["solid_ids"], [20])
        node = next(row for row in result["nodes"] if row["card"]["entity"]["id"] == 3)
        self.assertEqual({r["type"] for r in node["attached_entities"]}, {"SHELL", "SOLID"})
        self.assertEqual(before, [e.fields for e in self.base.entities.values()])
        self.assertEqual(result["scope_sha256"], self.capture()["scope_sha256"])
        for kind, ident in (("CONS", 9), ("SHELL", 10), ("SOLID", 20), ("VOLUME", 31), ("SHELL", 12)):
            self.assertTrue(self.capture(entity_type=kind, entity_ids=[ident])["ok"])

    def test_freeze_and_unfreeze_levels_preserve_fe_geometry_and_other_flags(self):
        for kind, ids in (("FACE", [1]), ("SHELL", [10]), ("SOLID", [20])):
            with self.subTest(kind=kind):
                args = {**self.args, "entity_type": kind, "entity_ids": ids}
                before = scope._guard(self.base, 1)
                frozen = self.apply(params=args)
                self.assertTrue(frozen["ok"], frozen)
                self.assertTrue(frozen["scope_verified"])
                self.assertTrue(frozen["api_called"])
                self.assertTrue(frozen["backup_verified"])
                if kind == "FACE":
                    self.assertEqual(self.base.entities[("SHELL", 10)].fields["FROZEN"], "NO")
                    self.assertEqual(self.base.entities[("FACE", 2)].fields["Frozen"], "NO")
                unchanged_calls = len(self.base.calls)
                no_op = self.apply(params=args)
                self.assertTrue(no_op["no_op"])
                self.assertFalse(no_op["backup_attempted"])
                self.assertEqual(len(self.base.calls), unchanged_calls)
                unfrozen = self.apply("unfreeze", args)
                self.assertTrue(unfrozen["ok"], unfrozen)
                self.assertEqual(scope._guard(self.base, 1), before)

    def test_stale_or_wrong_selection_is_rejected_before_backup_and_native_call(self):
        fingerprint = self.capture()["scope_sha256"]
        self.base.entities[("CONS", 9)].fields["Frozen"] = "YES"
        with patch.object(scope.native, "_backup_preflight") as backup:
            result = scope.freeze(self.state, self.base, self.base, self.constants,
                                  {**self.args, "action": "freeze", "expected_scope_sha256": fingerprint, "backup_path": "unused"})
        self.assertFalse(result["ok"])
        self.assertIn("stale", result["error"])
        self.assertFalse(result["possible_effects"])
        backup.assert_not_called()
        self.assertEqual(self.base.calls, [])

    def test_unfreeze_preserves_outside_macro_and_shared_effective_protection(self):
        for ident in (1, 2):
            self.base.entities[("FACE", ident)].fields["Frozen"] = "YES"
        result = self.apply("unfreeze")
        self.assertTrue(result["ok"], result)
        current = self.capture()
        shared = next(row for row in current["cons"] if row["entity"]["id"] == 9)
        self.assertEqual(shared["fields"]["Frozen"], "NO")
        self.assertEqual(shared["effective_macro_protection"]["protecting_macro_ids"], [2])
        self.assertTrue(shared["effective_macro_protection"]["value"])
        self.assertEqual(self.base.entities[("FACE", 2)].fields["Frozen"], "YES")

    def test_multi_face_macro_modifies_only_representative_flag_and_reports_complete_effects(self):
        self.base.macro_faces = {1: [1, 2], 3: [3]}
        self.base.macro_shells = {1: [10, 11], 3: []}
        args = {**self.args, "entity_ids": [2]}
        before = self.capture(**args)
        self.assertEqual(before["freeze_targets"]["FACE"], [1])
        self.assertEqual(before["freeze_effects"]["face_ids"], [1, 2])
        result = self.apply(params=args)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.base.calls[-1], ("freeze", [("FACE", 1)]))
        after = self.capture(**args)
        child = next(row for row in after["faces"] if row["entity"]["id"] == 2)
        self.assertEqual(child["freeze_state"]["value"], "NO")
        self.assertTrue(child["effective_macro_protection"]["value"])

    def test_partial_native_failure_unrelated_change_and_false_success_require_recovery(self):
        for fault in ("partial", "exception", "false_return", "outside"):
            with self.subTest(fault=fault):
                self.setUp()
                self.base.fault = fault
                args = {**self.args, "entity_type": "SHELL", "entity_ids": [10, 11]}
                result = self.apply(params=args)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["api_called"])
                self.assertTrue(result["recovery_required"])
                self.assertEqual(len(self.base.calls), 1)
                self.assertIn("observed_scope_sha256", result)

    def test_backup_mutation_stops_before_freeze(self):
        def mutate(*_):
            self.base.entities[("GRID", 8)].fields["X1"] += 1.
            return {"verified": True}
        result = self.apply(backup=mutate)
        self.assertFalse(result["ok"])
        self.assertFalse(result["api_called"])
        self.assertEqual(self.base.calls, [])

    def test_missing_freeze_state_and_incomplete_native_memberships_are_not_assumed_safe(self):
        del self.base.entities[("FACE", 1)].fields["Frozen"]
        self.assertFalse(self.apply()["api_called"])
        self.setUp()
        self.base.NodesToElements = lambda nodes, sources: {}
        with self.assertRaisesRegex(RuntimeError, "omitted"):
            self.capture()
        self.setUp()
        self.base.GetFacesOfCons = lambda cons: [self.base.entities[("FACE", 1)]] * 2
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.capture()

    def test_model_change_after_native_call_is_not_observed_as_the_original_model(self):
        native = self.base.FreezeElements
        def change(*args):
            result = native(*args)
            self.state.model_epoch += 1
            return result
        self.base.FreezeElements = change
        result = self.apply(params={**self.args, "entity_type": "SHELL", "entity_ids": [10]})
        self.assertFalse(result["ok"])
        self.assertIn("observation_error", result)
        self.assertNotIn("observed_scope_sha256", result)
        self.assertEqual(result["observed_target_states"], [])

    def test_changed_macro_membership_cannot_pass_freeze_verification(self):
        original = self.base.FreezeElements
        def change(*args):
            result = original(*args)
            self.base.macro_shells[1], self.base.macro_shells[2] = [11], [10]
            return result
        self.base.FreezeElements = change
        result = self.apply(params={**self.args, "entity_type": "SHELL", "entity_ids": [10]})
        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["recovery_required"])

    def test_target_observation_survives_native_exception_and_failed_relation_api(self):
        def partial(*_):
            self.base.entities[("SHELL", 10)].fields["FROZEN"] = "YES"
            self.base.NodesToElements = lambda *_a, **_k: None
            raise RuntimeError("native failed after one element")
        self.base.FreezeElements = partial
        result = self.apply(params={**self.args, "entity_type": "SHELL", "entity_ids": [10, 11]})
        self.assertFalse(result["ok"])
        self.assertIn("observation_error", result)
        self.assertIn("native failed", result["native_error"])
        self.assertEqual([(row["entity"]["id"], row["freeze_state"]["value"])
                          for row in result["observed_target_states"]], [(10, "YES"), (11, "NO")])

    def test_large_read_is_paged_and_freshness_validates_current_relation_state(self):
        result = self.capture()
        with tempfile.TemporaryDirectory() as temp:
            self.state.session_dir = Path(temp)
            ansa = types.SimpleNamespace(base=self.base, mesh=self.base, constants=self.constants)
            with patch.dict(sys.modules, {"ansa": ansa}), patch.object(scope, "_MAX_SCOPE_BYTES", 10 * 1024 * 1024):
                # Force paging via a large legitimate card string, without huge meshes.
                self.base.entities[("FACE", 1)].fields["Name"] = "名" * 70000
                descriptor = scope.execute(self.state, "mesh_scope_read", self.args)
                self.assertTrue(descriptor["stored_complete"])
                self.assertIn("scope_sha256", descriptor)
                page = structured_evidence.read_page(self.state, 1, descriptor["report_id"], "cons", 0, 1,
                    freshness_validator=lambda kind, saved: scope.validate_report(self.state, self.base, self.base, self.constants, 1, saved))
                self.assertIsNotNone(page)
                self.base.entities[("CONS", 9)].fields["Frozen"] = "YES"
                with self.assertRaisesRegex(ValueError, "stale"):
                    structured_evidence.read_page(self.state, 1, descriptor["report_id"], "cons", 0, 1,
                        freshness_validator=lambda kind, saved: scope.validate_report(self.state, self.base, self.base, self.constants, 1, saved))


if __name__ == "__main__":
    unittest.main()
