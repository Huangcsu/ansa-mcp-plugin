"""Risk checks for explicit macro scope, mesh failure recovery and SHELL deletion."""

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.journal import Journal
from ansa_mcp_bridge.operations import meshing
from test_entity_catalog import FakeBase, FakeState


class MeshBase(FakeBase):
    def __init__(self):
        super().__init__()
        self.faces_by_macro = {1: [1]}
        self.face = self.add("FACE", 1, ID=1, PID=1, Thickness=1.0, Frozen="NO")
        self.add("SHELL", 77, PID=2)
        self.add("GRID", 88, X1=1.0, X2=2.0, X3=3.0)
        self.face_macro = {1: 1}
        self.face_bounds = {1: [0.0, 0.0, 0.0, 20.0, 20.0, 0.0]}
        self.cons_by_face = {1: [201, 202, 203, 204]}
        self.cons_faces = {cons_id: [1] for cons_id in range(201, 205)}
        for cons_id in range(201, 205):
            self.add("CONS", cons_id)
        self.settings = {"mesh_change_option": "macro perimeters", "element_type": "Mixed",
                         "element_order": "2nd Order", "target_element_length": "average"}
        self.settings_writes = 0

    def GetFaceMacro(self, face):
        macro_id = self.face_macro.get(face._id)
        return None if macro_id is None else self.GetEntity(self.deck, "FACE", macro_id)

    def GetFacesOfMacros(self, macros):
        macro_id = macros[0]._id if hasattr(macros[0], "_id") else macros[0]
        return [self.GetEntity(self.deck, "FACE", entity_id) for entity_id in self.faces_by_macro[macro_id]]

    def GetFacesOfCons(self, cons):
        return [self.GetEntity(self.deck, "FACE", face_id) for face_id in self.cons_faces[cons._id]]

    def CollectEntities(self, deck, containers, entity_type, recursive=False):
        if entity_type == "CONS" and containers is not None:
            face_id = containers._id if hasattr(containers, "_id") else containers
            return [self.GetEntity(deck, "CONS", cons_id)
                    for cons_id in self.cons_by_face.get(face_id, [])]
        return super().CollectEntities(deck, containers, entity_type, recursive=recursive)

    def BoundBox(self, entities):
        bounds = [self.face_bounds[entity._id] for entity in entities]
        return ([min(row[index] for row in bounds) for index in range(3)]
                + [max(row[index] for row in bounds) for index in range(3, 6)])

    def BCSettingsGetValues(self, fields):
        return {key: self.settings[key] for key in fields}

    def BCSettingsSetValues(self, values):
        self.settings_writes += 1
        self.settings.update(values)
        return 0


class FakeMesh:
    def __init__(self, base):
        self.base = base
        self.shell_ids = []
        self.mesh_calls = 0
        self.fail_mesh = False
        self.fail_restore = False
        self.raise_restore = False
        self.bad_shell_readback = False
        self.native_quad_type = "QUAD"
        self.native_quad_pid = 7
        self.native_quad_extra = {}
        self.fail_length = False
        self.length_calls = []

    def GetMacroShells(self, macros):
        if self.bad_shell_readback:
            return None
        return [self.base.GetEntity(self.base.deck, "SHELL", entity_id) for entity_id in self.shell_ids] or 0

    def SaveMeshParams(self, filename):
        saved = {key: value for key, value in self.base.settings.items() if key != "mesh_change_option"}
        Path(filename).write_text(json.dumps(saved), encoding="utf-8")
        return 1

    def ReadMeshParams(self, filename):
        if self.raise_restore:
            raise RuntimeError("unreadable parameter backup")
        if self.fail_restore:
            return 0
        self.base.settings.update(json.loads(Path(filename).read_text(encoding="utf-8")))
        return 1

    def SetMeshParamTargetLength(self, mode, length):
        self.base.settings["target_element_length"] = str(length)
        return 1

    def Mesh(self, faces):
        self.mesh_calls += 1
        self.shell_ids = []
        quad = self.base.settings["element_type"].casefold() == "quad"
        if quad:
            for entity_id, nodes in ((91, (1, 2, 5, 4)), (92, (2, 3, 6, 5)),
                                      (93, (4, 5, 8, 7)), (94, (5, 6, 9, 8))):
                fields = {
                    "EID": entity_id, "PID": self.native_quad_pid,
                    "type": self.native_quad_type,
                    "G1": nodes[0], "G2": nodes[1], "G3": nodes[2], "G4": nodes[3],
                }
                fields.update(self.native_quad_extra)
                self.base.add("SHELL", entity_id, **fields)
                self.shell_ids.append(entity_id)
            self.base.face.fields.update({"Num.Quads": 4, "Num.Trias": 0})
        else:
            for entity_id in (91, 92):
                self.base.add("SHELL", entity_id, PID=1)
                self.shell_ids.append(entity_id)
        if self.fail_mesh:
            raise RuntimeError("native mesher failed after a partial write")
        return 1

    def ApplyNewLengthToMacros(self, element_length, perimeters, use_ansa_defaults_values):
        self.length_calls.append((element_length, [cons._id for cons in perimeters],
                                  use_ansa_defaults_values))
        if self.fail_length:
            return 0
        for cons in perimeters:
            for face in self.base.GetFacesOfCons(cons):
                face.fields["Elem.Length"] = element_length
        return 0


class AbaqusMeshBase(MeshBase):
    def __init__(self):
        super().__init__()
        self.deck = 2
        self.entities.clear()
        self.face = self.add("FACE", 1, ID=1, PID=7, Thickness=2.0, Frozen="NO",
                             **{"Elem.Length": "20", "Num.Quads": 0, "Num.Trias": 0})
        for cons_id in range(201, 205):
            self.add("CONS", cons_id)
        for index, (x, y) in enumerate(((0, 0), (10, 0), (20, 0), (0, 10), (10, 10),
                                        (20, 10), (0, 20), (10, 20), (20, 20)), start=1):
            self.add("NODE", index, X=float(x), Y=float(y), Z=0.0)
        self.add("SHELL", 77, EID=77, PID=9, type="QUAD",
                 G1=1, G2=2, G3=3, G4=4)


class AbaqusNonIntegerMeshBase(AbaqusMeshBase):
    def __init__(self):
        super().__init__()
        self.face_bounds[1] = [0.0, 0.0, 0.0, 21.0, 20.0, 0.0]
        for index, (x, y) in enumerate(((0, 0), (10.5, 0), (21, 0), (0, 10), (10.5, 10),
                                        (21, 10), (0, 20), (10.5, 20), (21, 20)), start=1):
            self.add("NODE", index, X=float(x), Y=float(y), Z=0.0)


class MeshingHarness(unittest.TestCase):
    def setUp(self):
        self.base = self.make_base()
        self.mesh = FakeMesh(self.base)
        self.state = FakeState()
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.state.session_dir = Path(directory.name)
        ansa = types.ModuleType("ansa")
        ansa.base, ansa.mesh = self.base, self.mesh
        ansa.constants = types.SimpleNamespace(NASTRAN=1, ABAQUS=2)
        module_patch = patch.dict(sys.modules, {"ansa": ansa})
        module_patch.start()
        self.addCleanup(module_patch.stop)

    def make_base(self):
        return MeshBase()

    def condition(self):
        return meshing.execute(self.state, "mesh_faces", {"face_ids": [1]})["faces"][0]["condition"]

    def surface(self, condition=None, element_type=None, target_length=2.0):
        params = {"faces": [condition or self.condition()], "target_length": target_length}
        if element_type is not None:
            params["element_type"] = element_type
        return meshing.execute(self.state, "mesh_surface", params)


class MeshingChecks(MeshingHarness):

    def test_quad_is_rejected_for_nastran_before_parameter_writes(self):
        with self.assertRaisesRegex(ValueError, "ABAQUS"):
            self.surface(element_type="quad")
        self.assertEqual(self.base.settings_writes, 0)
        self.assertEqual(self.mesh.mesh_calls, 0)

    def test_no_mesh_sentinel_is_distinct_from_failed_readback(self):
        row = meshing.execute(self.state, "mesh_faces", {"face_ids": [1]})["faces"][0]
        self.assertEqual(row["condition"]["expected_shell_ids"], [])
        self.assertEqual(self.base.settings_writes, 0)
        self.mesh.bad_shell_readback = True
        with self.assertRaisesRegex(RuntimeError, "GetMacroShells"):
            self.condition()

    def test_incomplete_macro_and_stale_face_are_rejected_before_writes(self):
        condition = self.condition()
        condition["expected_fields"]["Thickness"] = 99.0
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.surface(condition)
        self.base.add("FACE", 2, ID=2, PID=1, Thickness=1.0, Frozen="NO")
        self.base.faces_by_macro[1] = [1, 2]
        with self.assertRaisesRegex(ValueError, "every FACE"):
            self.surface()
        self.assertEqual(self.base.settings_writes, 0)
        self.assertEqual(self.mesh.mesh_calls, 0)

    def test_success_restores_parameters_and_preserves_unrelated_shell(self):
        before = dict(self.base.settings)
        result = self.surface()
        self.assertTrue(result["ok"])
        self.assertEqual(result["created_shell_ids"], [91, 92])
        self.assertTrue(result["scope_verified"])
        self.assertEqual(self.base.settings, before)
        self.assertIsNotNone(self.base.GetEntity(1, "SHELL", 77))
        with self.assertRaisesRegex(ValueError, "unmeshed"):
            self.surface()
        self.assertEqual(self.mesh.mesh_calls, 1)

    def test_tria_keeps_pid_zero_face_compatibility(self):
        self.base.face.fields["PID"] = 0
        result = self.surface()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["element_type"], "tria")
        self.assertEqual(result["created_shell_ids"], [91, 92])

    def test_partial_native_failure_retains_effects_and_restores_parameters(self):
        before = dict(self.base.settings)
        self.mesh.fail_mesh = True
        result = self.surface()
        self.assertFalse(result["ok"])
        self.assertIn("partial write", result["error"])
        self.assertEqual(result["created_shell_count"], 2)
        self.assertTrue(result["parameters_restored"])
        self.assertEqual(self.base.settings, before)
        self.assertEqual(self.mesh.mesh_calls, 1)

    def test_parameter_restore_failure_is_not_reported_as_success(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.mesh.shell_ids = []
                self.mesh.fail_restore = not raises
                self.mesh.raise_restore = raises
                result = self.surface()
                self.assertFalse(result["ok"])
                self.assertFalse(result["parameters_restored"])
                self.assertIn("restoration", result["error"])
                self.assertEqual(self.base.settings["mesh_change_option"], "macro perimeters")
                self.assertTrue(Path(result["parameter_backup"]).is_file())

    def test_face_readback_failure_keeps_global_mesh_effects(self):
        native_mesh = self.mesh.Mesh
        def break_readback(faces):
            status = native_mesh(faces)
            self.mesh.bad_shell_readback = True
            return status
        self.mesh.Mesh = break_readback
        result = self.surface()
        self.assertFalse(result["ok"])
        self.assertEqual(result["created_shell_ids"], [91, 92])
        self.assertEqual(result["face_readback_errors"][0]["id"], 1)

    def test_partial_delete_keeps_requested_and_outside_changes(self):
        self.surface()
        def partial_delete(entities, **options):
            self.base.entities.pop(("SHELL", 91))
            self.base.entities.pop(("SHELL", 77))
            raise RuntimeError("partial native deletion")
        self.base.DeleteEntity = partial_delete
        result = meshing.execute(self.state, "mesh_delete", {"entities": [
            {"id": 91, "expected_fields": {"PID": 1}}, {"id": 92, "expected_fields": {"PID": 1}},
        ]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted_shell_ids"], [91])
        self.assertEqual(result["remaining_requested_shell_ids"], [92])
        self.assertEqual(result["outside_removed_shell_ids"], [77])

    def test_delete_checks_whole_batch_and_preserves_nodes_and_other_shells(self):
        self.surface()
        conditions = [{"id": 91, "expected_fields": {"PID": 1}}, {"id": 92, "expected_fields": {"PID": 99}}]
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            meshing.execute(self.state, "mesh_delete", {"entities": conditions})
        self.assertEqual(self.base.delete_calls, 0)
        conditions[1]["expected_fields"]["PID"] = 1
        result = meshing.execute(self.state, "mesh_delete", {"entities": conditions})
        self.assertTrue(result["scope_verified"])
        self.assertEqual(result["shell_count_after"], 1)
        self.assertIsNotNone(self.base.GetEntity(1, "SHELL", 77))
        self.assertIsNotNone(self.base.GetEntity(1, "GRID", 88))
        self.assertIsNotNone(self.base.GetEntity(1, "FACE", 1))


class AbaqusQuadChecks(MeshingHarness):
    def make_base(self):
        return AbaqusMeshBase()

    def test_quad_uses_deck_fields_and_verifies_four_node_shells(self):
        row = meshing.execute(self.state, "mesh_faces", {"face_ids": [1]})["faces"][0]
        self.assertEqual(row["condition"]["expected_fields"],
                         {"ID": 1, "PID": 7, "Thickness": 2.0, "Frozen": "NO"})
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["element_type"], "quad")
        self.assertEqual(result["created_shell_ids"], [91, 92, 93, 94])
        self.assertTrue(result["scope_verified"])
        self.assertEqual(result["quad_shell_count"], 4)
        self.assertEqual([item["node_ids"] for item in result["quad_shells"]],
                         [[1, 2, 5, 4], [2, 3, 6, 5], [4, 5, 8, 7], [5, 6, 9, 8]])
        self.assertTrue(all(item["property_id"] == 7 for item in result["quad_shells"]))
        self.assertTrue(all(item["edge_lengths"] == [10.0, 10.0, 10.0, 10.0]
                            for item in result["quad_shells"]))
        self.assertEqual(result["edge_length_min"], 10.0)
        self.assertEqual(result["edge_length_max"], 10.0)
        self.assertEqual(result["edge_length_upper_factor"], 1.5)
        self.assertEqual(result["edge_length_upper_limit"], 15.0)
        self.assertTrue(result["edge_length_bound_verified"])
        self.assertTrue(result["perimeter_target_verified"])
        self.assertTrue(result["target_length_verified"])
        self.assertIn("1.5x", result["target_length_interpretation"])
        self.assertEqual(result["mesh_bounds"], [0.0, 0.0, 0.0, 20.0, 20.0, 0.0])
        self.assertEqual(result["actual_size"], [20.0, 20.0, 0.0])
        self.assertTrue(result["bounds_verified"])
        self.assertEqual(result["perimeter_scope"]["cons_ids"], [201, 202, 203, 204])
        self.assertEqual(result["length_application"]["cons_count"], 4)
        self.assertEqual(result["face_derived_after_mesh"]["1"]["num_quads"], 4)
        self.assertEqual(result["face_derived_after_mesh"]["1"]["num_trias"], 0)
        self.assertEqual(list(result["face_derived_before"]), ["1"])
        self.assertEqual(list(result["face_derived_after_apply"]), ["1"])
        self.assertEqual(self.base.settings["mesh_change_option"], "macro perimeters")

    def test_quad_result_passes_journal_canonical_json(self):
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertTrue(result["ok"], result.get("error"))
        Journal._canonical(result, "result")

    def test_quad_readback_rejects_native_triangle_output(self):
        self.mesh.native_quad_type = "TRIA"
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertFalse(result["ok"])
        self.assertIn("QUAD", result["error"])
        self.assertEqual(result["created_shell_ids"], [91, 92, 93, 94])

    def test_quad_readback_rejects_wrong_property(self):
        self.mesh.native_quad_pid = 999
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertFalse(result["ok"])
        self.assertIn("property", result["error"])
        self.assertEqual(result["created_shell_ids"], [91, 92, 93, 94])

    def test_quad_readback_rejects_higher_order_nodes(self):
        self.mesh.native_quad_extra = {"G5": 10}
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertFalse(result["ok"])
        self.assertIn("higher-order", result["error"])
        self.assertIn("G5", result["error"])
        self.assertEqual(result["created_shell_ids"], [91, 92, 93, 94])

    def test_cons_shared_with_unselected_face_is_rejected_before_writes(self):
        self.base.add("FACE", 2, ID=2, PID=8, Thickness=2.0, Frozen="NO",
                      **{"Elem.Length": "20", "Num.Quads": 0, "Num.Trias": 0})
        self.base.face_macro[2] = 2
        self.base.faces_by_macro[2] = [2]
        self.base.face_bounds[2] = [20.0, 0.0, 0.0, 40.0, 20.0, 0.0]
        self.base.add("CONS", 205)
        self.base.cons_by_face[1].append(205)
        self.base.cons_faces[205] = [1, 2]
        with self.assertRaisesRegex(ValueError, "outside"):
            self.surface(element_type="quad", target_length=10.0)
        self.assertEqual(self.base.settings_writes, 0)
        self.assertEqual(self.mesh.length_calls, [])
        self.assertEqual(self.mesh.mesh_calls, 0)

    def test_scoped_length_noop_is_not_reported_as_success(self):
        self.mesh.fail_length = True
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertFalse(result["ok"])
        self.assertIn("Elem.Length", result["error"])
        self.assertEqual(result["created_shell_ids"], [])
        self.assertEqual(self.mesh.mesh_calls, 0)
        self.assertEqual(self.mesh.length_calls, [("10", [201, 202, 203, 204], False)])
        self.assertTrue(result["parameters_restored"])

    def test_missing_face_field_is_reported_without_mesh_or_parameter_writes(self):
        self.base.face.fields.pop("Frozen")
        with self.assertRaisesRegex(RuntimeError, "Frozen"):
            meshing.execute(self.state, "mesh_faces", {"face_ids": [1]})
        self.assertEqual(self.base.settings_writes, 0)
        self.assertEqual(self.mesh.mesh_calls, 0)


class AbaqusNonIntegerQuadChecks(MeshingHarness):
    def make_base(self):
        return AbaqusNonIntegerMeshBase()

    def test_non_integer_rectangle_accepts_edges_up_to_1_5x_target(self):
        result = self.surface(element_type="quad", target_length=10.0)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["edge_length_min"], 10.0)
        self.assertEqual(result["edge_length_max"], 10.5)
        self.assertEqual(result["edge_length_upper_factor"], 1.5)
        self.assertEqual(result["edge_length_upper_limit"], 15.0)
        self.assertTrue(result["edge_length_bound_verified"])
        self.assertIn("1.5x", result["target_length_interpretation"])
        self.assertEqual(result["mesh_bounds"], [0.0, 0.0, 0.0, 21.0, 20.0, 0.0])
        self.assertEqual(result["actual_size"], [21.0, 20.0, 0.0])


if __name__ == "__main__":
    unittest.main()
