"""Replacement, engineering reference and failure-path regression tests."""

import copy
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import surface_remesh as remesh
from tests.unit.test_cons_seed import Entity, _audit_case, _row
from tests.unit.test_perimeter_edit import prepared as seed_prepared


def request():
    return {"deck": 1, "faces": [{"id": 5, "expected_fields": {"Frozen": "NO", "PID": 3},
            "expected_macro_id": 5, "expected_shell_ids": [10]}], "remesh": {
            "algorithm": "FREE", "element_type": "quad", "element_order": "1st Order",
            "boundary_policy": "preserve", "target_length": 1.,
            "expected_scope_sha256": "a"*64, "expected_seed_snapshot_sha256": "b"*64,
            "backup_path": "new.ansa"}}


def shell():
    fields = {"type": "CQUAD4", "EID": 10, "PID": 3,
              **{f"G{i}": i for i in range(1, 5)},
              "mat.orient": "MCID", "MCID": None, "ZOFFS": None, "TFLAG": " ",
              **{f"T{i}": 0. for i in range(1, 5)}, "FROZEN": "NO", "FROZEN_ID": "NO",
              "FROZEN_DELETE": "NO", "FREEZE ON DEPENETRATION": "NO", "Comment": ""}
    return {"card_fields": list(fields), "fields": fields}


def audit_case(reuse=False):
    prepared, before, _ = _audit_case()
    point = seed_prepared()["before"]["grids"][-1]
    before["grids"].append(point)
    before["entities"].extend([_row("SHELL", 10, fields={"EID": 10}),
                               _row("GRID", 9, fields=point["card"]["fields"])])
    after = copy.deepcopy(before)
    after["entities"] = [r for r in after["entities"] if (r["entity"]["type"], r["entity"]["id"]) not in {("SHELL", 10), ("GRID", 9)}]
    after["grids"] = [r for r in after["grids"] if r["id"] != 9]
    shell_id, node_id = (10, 9) if reuse else (20, 11)
    point = copy.deepcopy(point)
    point["id"] = point["card"]["fields"]["NID"] = node_id
    after["grids"].append(point)
    after["entities"].extend([_row("SHELL", shell_id, fields={"EID": shell_id}),
                              _row("GRID", node_id, fields=point["card"]["fields"])])
    prepared.update(old_shell_ids=[10], old_interior_ids=[9], source_faces=[{"id": 5, "part_id": 3}])
    return prepared, after, {"shell_ids": [shell_id], "node_ids": [1, 2, node_id]}


class SurfaceRemeshTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(assert_main=Mock())
        self.base = SimpleNamespace(
            GetEntity=lambda d, k, i: Entity(k, i),
            TypesInCategory=lambda d, k: [],
            CollectEntities=lambda *a, **kw: [],
            GetEntityCardValues=lambda d, e, f: {"__id__": e._id, "__type__": e.kind})
        self.mesh = SimpleNamespace(RemeshShells=Mock(return_value=[Entity("SHELL", 20)]),
                                    Create4SidedMesh=Mock(return_value=SimpleNamespace(meshed_ents=[Entity("FACE", 5)])))

    def test_strict_normalization_rejects_unbounded_or_ambiguous_modes(self):
        self.assertEqual(remesh.normalize(request())["face_ids"], [5])
        for field, value in (("algorithm", "CFD"), ("element_type", ""),
                             ("target_length", True), ("target_length", float("inf")),
                             ("element_order", "2nd Order"), ("boundary_policy", "change"),
                             ("expected_scope_sha256", "A"*64)):
            args = request(); args["remesh"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                remesh.normalize(args)
        for change in ({"deck": True}, {"target_length": 2.}, {"seed_scope": {}}):
            with self.assertRaises((ValueError, TypeError)):
                remesh.normalize({**request(), **change})
        args = request(); args["remesh"].update(algorithm="mapped", element_type="mixed")
        with self.assertRaisesRegex(ValueError, "quad"):
            remesh.normalize(args)

    def test_shell_engineering_and_metadata_are_not_discarded(self):
        remesh._shell_definition(shell())
        for field, value in (("MCID", 7), ("ZOFFS", .1), ("T1", .5), ("FROZEN", "YES"),
                             ("FROZEN_DELETE", "YES"), ("Comment", "keep"), ("FUTURE", [1])):
            row = shell(); row["fields"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                remesh._shell_definition(row)
        row = shell(); row["card_fields"].remove("ZOFFS")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            remesh._shell_definition(row)

    def test_replacement_audit_accepts_reused_and_new_ids(self):
        for reuse in (False, True):
            prepared, after, data = audit_case(reuse)
            changes, interior = remesh._audit(prepared, after, data, {})
            self.assertEqual(interior, {9 if reuse else 11})
            self.assertEqual(changes, [])

    def test_replacement_audit_protects_cad_boundary_and_outside_fe(self):
        for change in ("cad", "grid", "outside_fe", "extra", "stale_interior"):
            prepared, after, data = audit_case()
            if change == "cad": after["cad_sha256"] = "c"*64
            elif change == "grid": next(r for r in after["entities"] if r["entity"] == {"type": "GRID", "id": 1})["fields"]["X1"] = 1e-10
            elif change == "outside_fe": after["entities"][-3]["fields"]["PID"] = 99
            elif change == "extra": after["entities"].append(_row("SHELL", 100))
            else: after["entities"].append(_row("GRID", 9))
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                remesh._audit(prepared, after, data, {})

    def test_reference_index_uses_actual_entity_refs_not_matching_numbers(self):
        entity = Entity("LOAD", 2)
        entity.card_fields = lambda deck: ["numeric", "reference"]
        entity.get_entity_values = lambda deck, fields: {"numeric": 10, "reference": Entity("PSHELL", 10)}
        snapshot = {"entities": [_row("LOAD", 2)]}
        self.base.CollectEntities = lambda *a, **kw: [entity]
        with patch.object(self.base, "GetEntity", side_effect=AssertionError("unsafe handle reacquisition")):
            self.assertEqual(remesh._reference_index(self.base, 1, snapshot, {("SHELL", 10)}),
                             {("LOAD", 2): [("PSHELL", 10)]})
            entity.get_entity_values = lambda deck, fields: {"numeric": 0, "reference": [Entity("SHELL", 10)]}
            with self.assertRaisesRegex(ValueError, "external entity"):
                remesh._reference_index(self.base, 1, snapshot, {("SHELL", 10)})

    def test_set_members_and_missing_reference_fields_block_replacement(self):
        entity = Entity("SET", 2)
        entity.card_fields = lambda deck: ["SID"]
        entity.get_entity_values = lambda deck, fields: {"SID": 2}
        self.base.CollectEntities = lambda d, e, k, *a, **kw: [entity] if k == "__ALL_ENTITIES__" else [Entity(k, 10)] if k == "SHELL" else []
        with patch.object(self.base, "GetEntity", side_effect=AssertionError("unsafe handle reacquisition")):
            with self.assertRaisesRegex(ValueError, "external entity"):
                remesh._reference_index(self.base, 1, {"entities": [_row("SET", 2)]}, {("SHELL", 10)})
            entity.get_entity_values = lambda deck, fields: {}
            with self.assertRaisesRegex(ValueError, "incomplete"):
                remesh._reference_index(self.base, 1, {"entities": [_row("SET", 2)]}, set())

    def test_connections_and_include_rejected_before_any_card_scan_or_backup(self):
        for kind in ("SpotweldPoint_Type", "Bolt_Type", "CONNECTOR_ENTITY", "FutureConnection", "INCLUDE"):
            entity = Entity(kind, 2)
            entity.card_fields = Mock(side_effect=AssertionError("unsafe card scan"))
            self.base.TypesInCategory = lambda *a: ["FutureConnection"]
            self.base.CollectEntities = lambda *a, **kw: [entity]
            with patch.object(remesh.native, "_check_nastran"), patch.object(remesh.guards, "_execution_context"), \
                    patch.object(remesh.mesh_scope, "capture") as scope, \
                    patch.object(remesh.native, "_verify_backup") as backup:
                result = remesh.apply(self.state, self.base, self.mesh, None, None, request())
            with self.subTest(kind=kind):
                self.assertFalse(result["ok"]); self.assertFalse(result["possible_effects"])
                self.assertIn("containing", result["error"])
                entity.card_fields.assert_not_called(); backup.assert_not_called(); scope.assert_not_called()
        self.mesh.RemeshShells.assert_not_called()

    def test_unavailable_membership_or_changed_reference_inventory_is_not_empty(self):
        self.base.TypesInCategory = lambda *a: None
        with self.assertRaises(RuntimeError):
            remesh._membership_gate(self.base, 1)
        with self.assertRaisesRegex(RuntimeError, "inventory changed"):
            remesh._reference_index(self.base, 1, {"entities": [_row("LOAD", 2)]}, set())

    def test_free_and_mapped_call_native_once_without_predeletion(self):
        prepared = {"context": {}, "old_shell_ids": [10], "source_faces": [{"entity": Entity("FACE", 5)}]}
        with patch.object(remesh.guards, "_same_context"):
            for mode in ("FREE", "mapped"):
                clean = remesh.normalize(request()); clean["remesh"]["algorithm"] = mode
                result = {}
                remesh._invoke(self.state, self.base, self.mesh, clean, prepared, result)
                self.assertTrue(result["api_called"])
        self.mesh.RemeshShells.assert_called_once()
        self.mesh.Create4SidedMesh.assert_called_once()
        kw = self.mesh.Create4SidedMesh.call_args.kwargs
        self.assertFalse(kw["only_unmeshed"])
        self.assertTrue(kw["protect_selection_bounds"])
        self.assertEqual(kw["spacing"], "off")

    def test_stale_scope_rejected_before_snapshot_backup_or_native(self):
        with patch.object(remesh.native, "_check_nastran"), patch.object(remesh.guards, "_execution_context"), \
                patch.object(remesh.mesh_scope, "capture", return_value={"scope_sha256": "c"*64}), \
                patch.object(remesh.seeds, "_guard_snapshot") as snapshot, \
                patch.object(remesh.native, "_verify_backup") as backup:
            result = remesh.apply(self.state, self.base, self.mesh, None, None, request())
        self.assertFalse(result["ok"]); self.assertFalse(result["possible_effects"])
        self.assertIn("stale", result["error"])
        snapshot.assert_not_called(); backup.assert_not_called(); self.mesh.RemeshShells.assert_not_called()

    def test_native_exception_is_observed_but_never_retried_or_reported_success(self):
        prepared = {"context": {}, "before": {}, "backup": ("new.ansa",)}
        calls = []
        def context(state, base, mesh, length, callback, result, **kw):
            result["restored"] = True
            callback()
        def invoke(*args):
            args[-1]["api_called"] = True
            calls.append(1)
            raise RuntimeError("lost native reply")
        with ExitStack() as stack:
            for owner, name, value in ((remesh, "_preflight", prepared), (remesh.native, "_verify_backup", {}),
                (remesh.guards, "_same_context", None), (remesh.seeds, "_guard_snapshot", {}),
                (remesh.mesh_scope, "capture", {"scope_sha256": "a"*64}), (remesh, "_materialize", prepared),
                (remesh.meshing, "_face_snapshot", (None, {}, []))):
                stack.enter_context(patch.object(owner, name, return_value=value))
            stack.enter_context(patch.object(remesh.meshing_context, "run_with_context", side_effect=context))
            stack.enter_context(patch.object(remesh, "_invoke", side_effect=invoke))
            verify = stack.enter_context(patch.object(remesh, "_verify"))
            result = remesh.apply(self.state, self.base, self.mesh, None, None, request())
        self.assertEqual(calls, [1]); verify.assert_called_once()
        self.assertFalse(result["ok"]); self.assertTrue(result["recovery_required"])
        self.assertTrue(result["original_operation_must_be_queried"])
        self.assertIn("lost native reply", result["error"])


if __name__ == "__main__":
    unittest.main()
