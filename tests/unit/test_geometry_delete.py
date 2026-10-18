"""Risk checks for fresh FACE conditions and exact deletion scope."""

import copy
import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import geometry, geometry_delete
from ansa_mcp_shared.operations import writes_model
from test_entity_catalog import FakeBase, FakeState


class DeleteBase(FakeBase):
    def __init__(self):
        super().__init__()
        self.geometry = {}
        self.macros = {}
        self.cons_by_face = {}
        self.mesh_ids = {}
        self.write_calls = []
        self.mode = "success"
        for face_id in (1, 2):
            self.add("FACE", face_id, ID=face_id, PID=1, Thickness=0.0, Frozen="NO")
            self.geometry[face_id] = {"area": float(face_id), "center": [float(face_id), 0., 0.],
                                      "bounds": [float(face_id), 0., 0., float(face_id)+1., 1., 0.]}
            self.macros[face_id] = [face_id]
            self.cons_by_face[face_id] = list(range(face_id*10, face_id*10+4))
            for entity_id in self.cons_by_face[face_id]:
                self.add("CONS", entity_id)
                self.add("CURVE", entity_id)
        self.add("GRID", 88, X1=1.0, X2=2.0, X3=3.0)
        self.add("PSHELL", 1, PID=1)
        self.add("MAT1", 1, MID=1)

    def GetFaceArea(self, face):
        return self.geometry[face._id]["area"]

    def Cog(self, face):
        return self.geometry[face._id]["center"]

    def GetFaceOrientation(self, face):
        return [0., 0., 2.]

    def BoundBox(self, entities):
        return self.geometry[entities[0]._id]["bounds"]

    def GetFaceMacro(self, face):
        return face

    def GetFacesOfMacros(self, macros):
        return [self.GetEntity(1, "FACE", entity_id) for entity_id in self.macros[macros[0]._id]]

    def CollectEntities(self, deck, containers, entity_type, recursive=False):
        if entity_type == "CONS" and containers is not None:
            return [self.GetEntity(deck, "CONS", entity_id) for entity_id in self.cons_by_face[containers._id]]
        if entity_type in ("__PROPERTIES__", "__MATERIALS__"):
            entity_type = "PSHELL" if entity_type == "__PROPERTIES__" else "MAT1"
        return super().CollectEntities(deck, containers, entity_type, recursive)

    def GetFacesOfCons(self, cons):
        return [self.GetEntity(1, "FACE", face_id) for face_id, ids in self.cons_by_face.items()
                if any(entity._id in ids for entity in cons)]

    def DeleteEntity(self, entities, force=False, compress=True):
        self.write_calls.append({"ids": [entity._id for entity in entities], "force": force, "compress": compress})
        for entity in entities[:1] if self.mode == "partial" else entities:
            self.entities.pop(("FACE", entity._id))
            for entity_id in self.cons_by_face[entity._id]:
                self.entities.pop(("CONS", entity_id), None)
        if self.mode == "partial":
            raise RuntimeError("failed after one FACE")
        if self.mode == "outside":
            self.geometry[2]["area"] += 1.0
            self.entities.pop(("GRID", 88))
        return 1 if self.mode == "nonzero" else 0


class DeleteMesh:
    def __init__(self, base):
        self.base = base

    def GetMacroShells(self, macros):
        return [self.base.GetEntity(1, "SHELL", entity_id)
                for entity_id in self.base.mesh_ids.get(macros[0]._id, [])] or 0


class GeometryDeleteChecks(unittest.TestCase):
    def setUp(self):
        self.base = DeleteBase()
        self.mesh = DeleteMesh(self.base)
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.state = FakeState()
        ansa = types.ModuleType("ansa")
        ansa.base, ansa.mesh, ansa.constants = self.base, self.mesh, self.constants
        self.module_patch = patch.dict(sys.modules, {"ansa": ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def conditions(self, ids=(1, 2)):
        result = geometry_delete.prepare_preview(self.base, self.mesh, self.constants, 1, list(ids))
        self.assertTrue(result["delete_ready"])
        return copy.deepcopy(result["delete_conditions"])

    def delete(self, conditions):
        return geometry_delete.execute(self.state, "geometry_delete", {"deck": 1, "faces": conditions})

    def test_condition_preview_is_read_only_and_rejects_frozen_mesh_macros_and_shared_cons(self):
        preview = geometry.execute(self.state, "geometry_preview", {"deck": 1, "query": "largest_area",
                                   "include_delete_conditions": True})
        self.assertTrue(preview["delete_ready"])
        self.assertEqual([row["id"] for row in preview["delete_conditions"]], [2, 1])
        for reason in ("frozen", "mesh", "macro", "shared"):
            with self.subTest(reason=reason):
                self.base.entities[("FACE", 1)].fields["Frozen"] = "YES" if reason == "frozen" else "NO"
                self.base.macros[1] = [1, 2] if reason == "macro" else [1]
                self.base.add("SHELL", 77, PID=1)
                self.base.mesh_ids[1] = [77] if reason == "mesh" else []
                self.base.cons_by_face[2] = [10, 20, 21, 22] if reason == "shared" else [20, 21, 22, 23]
                result = geometry_delete.prepare_preview(self.base, self.mesh, self.constants, 1, [1])
                self.assertFalse(result["delete_ready"])
                self.assertEqual(result["delete_conditions"], [])
                self.assertEqual(result["delete_rejections"][0]["id"], 1)
        self.assertEqual(self.base.write_calls, [])
        self.assertTrue(writes_model("geometry_delete"))
        with self.assertRaisesRegex(ValueError, "geometry_delete"):
            geometry_delete.catalog._entity_delete(self.base, 1, {"entity_type": "FACE", "entities": []})

    def test_entire_batch_and_scope_are_validated_before_the_first_write(self):
        original = self.conditions()
        bad_cases = []
        for change in ("geometry", "fields", "cons", "missing", "duplicate", "boolean"):
            bad = copy.deepcopy(original)
            if change == "geometry":
                bad[1]["expected_geometry"]["area"] += 1.0
            elif change == "fields":
                bad[1]["expected_fields"].pop("Frozen")
            elif change == "cons":
                bad[1]["expected_cons_ids"] = [999]
            elif change == "missing":
                bad[1]["id"] = 999
            elif change == "duplicate":
                bad[1] = bad[0]
            else:
                bad[1]["id"] = True
            bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, RuntimeError, LookupError)):
                    self.delete(bad)
        collect = self.base.CollectEntities
        def too_many_faces(deck, containers, kind):
            if kind == "FACE" and containers is None:
                return [self.base.GetEntity(deck, "FACE", 1)] * 10001
            return collect(deck, containers, kind)
        with patch.object(self.base, "CollectEntities", side_effect=too_many_faces):
            with self.assertRaisesRegex(RuntimeError, "exceeds 10000"):
                self.delete(original[:1])
        self.assertEqual(self.base.write_calls, [])
        self.assertIsNotNone(self.base.GetEntity(1, "FACE", 1))
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "NASTRAN"):
            geometry_delete.execute(self.state, "geometry_delete", {"deck": 2, "faces": original})

    def test_exact_deletion_scope_preserves_nodes_curves_materials_and_properties(self):
        conditions = self.conditions((1,))
        self.base.mode = "nonzero"
        result = self.delete(conditions)
        self.assertEqual(result["deleted_face_ids"], [1])
        self.assertEqual(result["remaining_requested_face_ids"], [])
        self.assertTrue(result["scope_verified"])
        self.assertEqual(result["changed_protected_types"], [])
        self.assertEqual(result["removed_boundary_cons_count"], 4)
        self.assertEqual(self.base.write_calls, [{"ids": [1], "force": False, "compress": False}])
        self.assertIn("warnings", result)
        self.assertIsNotNone(self.base.GetEntity(1, "FACE", 2))
        json.dumps(result, allow_nan=False)

    def test_partial_or_outside_effects_keep_readback_and_never_claim_success(self):
        self.base.mode = "partial"
        result = self.delete(self.conditions())
        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertEqual(result["deleted_face_ids"], [1])
        self.assertEqual(result["remaining_requested_face_ids"], [2])
        self.assertFalse(result["scope_verified"])
        self.base = DeleteBase()
        self.mesh = DeleteMesh(self.base)
        self.base.mode = "outside"
        result = geometry_delete._delete(self.base, self.mesh, self.constants, 1, {"faces": self.conditions((1,))})
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed_outside_face_ids"], [2])
        self.assertEqual(result["changed_protected_types"], ["GRID"])

    def test_late_geometry_or_protected_readback_failure_keeps_observed_deletion_ids(self):
        conditions = self.conditions((1,))
        area = self.base.GetFaceArea
        collect = self.base.CollectEntities
        def late_area_failure(face):
            if self.base.write_calls and face._id == 2:
                raise RuntimeError("late geometry readback failed")
            return area(face)
        def late_protected_failure(deck, containers, kind):
            if self.base.write_calls and kind == "__MATERIALS__":
                raise RuntimeError("late material readback failed")
            return collect(deck, containers, kind)
        with patch.object(self.base, "GetFaceArea", side_effect=late_area_failure), \
                patch.object(self.base, "CollectEntities", side_effect=late_protected_failure):
            result = self.delete(conditions)
        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertFalse(result["scope_verified"])
        self.assertEqual(result["deleted_face_ids"], [1])
        self.assertEqual(result["remaining_requested_face_ids"], [])
        self.assertEqual(result["unconfirmed_outside_face_ids"], [2])
        self.assertEqual(result["unconfirmed_protected_types"], ["__MATERIALS__"])
        self.assertEqual(result["removed_boundary_cons_count"], 4)


class GeometryDeleteSchemaChecks(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_conditions_are_rejected_before_admission(self):
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.__main__ import create_server

        class Service:
            calls = []
            def submit_task(self, *args):
                self.calls.append(args)
                raise AssertionError("Invalid deletion must never enter the task queue")

        service = Service()
        server = create_server(service)
        condition = {"id": 1, "expected_fields": {"ID": 1, "PID": 1, "Thickness": 0., "Frozen": "NO"},
                     "expected_geometry": {"area": 1., "center": [0., 0., 0.],
                         "bounds": [0., 0., 0., 1., 1., 0.], "normal": [0., 0., 1.]}, "expected_cons_ids": [1]}
        args = {"operation_id": "schema-delete", "expected_target": {"backend_id": "b", "instance_id": "i",
                "session_id": "s", "generation": 1}, "expected_model": {"epoch": 1}, "deck": 1, "faces": [condition]}
        invalid = [{**args, "deck": True}, {**args, "faces": []},
                   {**args, "faces": [{**condition, "id": True}]},
                   {**args, "faces": [{**condition, "expected_geometry": {**condition["expected_geometry"], "area": float("nan")}}]},
                   {**args, "faces": [{**condition, "expected_cons_ids": [True]}]}]
        for values in invalid:
            with self.assertRaises(ToolError):
                await server.call_tool("ansa_geometry_delete", values)
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
