"""Native association is established before distances are measured."""

import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from ansa_mcp_bridge.operations import structured_boundary as boundary
from ansa_mcp_bridge.operations import structured_meshing, structured_native, structured_evidence
from ansa_mcp_plugin.__main__ import create_server
from test_structured_native import FakeBase


def fixture(offset=0.0):
    base = FakeBase()
    face = base.add("FACE", 1, area=1.0, **{"Num.Quads": 1, "Num.Trias": 0})
    shell = base.add("SHELL", 10, type="CQUAD4", EID=10, PID=1, G1=1, G2=2, G3=3, G4=4)
    points = [[0., 0., 0.], [1., 0., offset], [1., 1., 0.], [0., 1., 0.]]
    for i, xyz in enumerate(points, 1):
        base.add("GRID", i, NID=i, CP=0, **dict(zip(("X1", "X2", "X3"), xyz)))
    mesh = SimpleNamespace(GetMacroShells=lambda macros: [shell])
    snapshot = {"nodes": [{"id": i, "xyz": p} for i, p in enumerate(points, 1)]}
    return base, mesh, face, snapshot


class NativeBoundaryTests(unittest.TestCase):
    def test_all_members_remain_measured_after_coordinate_outlier(self):
        base, mesh, face, snapshot = fixture(offset=0.1)
        records, entities = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        self.assertTrue(records[0]["membership_complete"])
        self.assertEqual(records[0]["shells"][0]["nodes"], [1, 2, 3, 4])
        calls = []
        def project(point, targets):
            calls.append((list(point), targets))
            return [[point[0], point[1], 0.], abs(point[2]), [0., 0., point[2]], [0., 0., 1.], face]
        result = boundary.measure_nodes(base, SimpleNamespace(ProjectPointToContainer=project), 1,
            snapshot, records, entities, 0.02)
        self.assertEqual(result["requested_node_face_pairs"], 4)
        self.assertEqual(result["measured_node_face_pairs"], 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["status"], "fail")
        self.assertAlmostEqual(result["maximum_distance"], 0.1)
        self.assertEqual([r["node_id"] for r in result["rows"] if r["status"] == "fail"], [2])
        self.assertFalse(result["continuous_surface_coverage_proven"])

    def test_multi_face_macro_is_not_attributed_to_each_individual_face(self):
        base, mesh, face, _ = fixture()
        other = base.add("FACE", 2, area=1.)
        base.GetFacesOfMacros = lambda macros: [face, other]
        rows, _ = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        self.assertFalse(rows[0]["membership_complete"])
        self.assertEqual(rows[0]["shells"], [])
        self.assertEqual(rows[0]["macro_shell_ids"], [10])
        self.assertEqual(rows[0]["membership_origin"], "ambiguous_multi_FACE_macro")

    def test_oversized_macro_is_rejected_before_member_metadata_reads(self):
        base, mesh, face, _ = fixture()
        base.GetFacesOfMacros = lambda macros: [face] * 1001
        with patch.object(boundary.catalog, "_entity_reference", wraps=boundary.catalog._entity_reference) as lookup:
            with self.assertRaisesRegex(ValueError, "macro expansion"):
                boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        self.assertLessEqual(lookup.call_count, 3)

    def test_incomplete_cylinder_face_membership_propagates_to_single_hole(self):
        from test_mesh_audit import _cylindrical_hex_snapshot
        snapshot = _cylindrical_hex_snapshot()
        snapshot.update(complete=True, integrity={"complete": True}, snapshot_sha256="a"*64)
        base = FakeBase()
        face1 = base.add("FACE", 1, area=1., **{"Num.Quads": 1, "Num.Trias": 0})
        base.add("FACE", 2, area=1., **{"Num.Quads": 0, "Num.Trias": 0})
        shell = base.add("SHELL", 10, type="CQUAD4", EID=10, PID=1, G1=1, G2=2, G3=6, G4=5)
        mesh = SimpleNamespace(GetMacroShells=lambda macros: [shell] if macros[0]._id == 1 else 0)
        calc = SimpleNamespace(ProjectPointToContainer=lambda p, targets: [list(p),0.,[0.,0.,0.],[0.,0.,1.],targets[0]])
        ansa = SimpleNamespace(base=base,mesh=mesh,calc=calc,constants=SimpleNamespace(NASTRAN=1))
        request = {"face_ids": [1,2], "tolerance": .02,
            "cylinders": [{"label": "bore", "face_ids": [1,2], "center": [0.,0.,0.],
                           "axis": [0.,0.,1.], "radius": 1., "tolerance": .02}]}
        state = SimpleNamespace(assert_main=lambda: None)
        params = {"deck": 1,"entity_ids": [150],"interfaces": [],"expected_boundary": None,
            "protected_cylinders": None,"expected_components": 1,"cad_boundary": request}
        with patch.dict("sys.modules", {"ansa": ansa}), \
             patch.object(structured_native,"capture_mesh",return_value=copy.deepcopy(snapshot)), \
             patch.object(structured_evidence,"store_report",side_effect=lambda state,deck,kind,result: result):
            result = structured_meshing.execute(state,"mesh_interface_audit",params)
        hole = result["sections"]["geometry"]["cylinders"][0]
        self.assertEqual(hole["measured_nodes_status"], "pass")
        self.assertEqual(hole["membership_status"], "incomplete")
        self.assertEqual(hole["source_face_ids"], [1,2])
        self.assertEqual(hole["status"], "incomplete")
        self.assertEqual(result["sections"]["geometry"]["radial_checks_status"], "incomplete")

    def test_unmeshed_and_absent_card_count_use_complete_native_return(self):
        base, mesh, face, _ = fixture()
        mesh.GetMacroShells = lambda macros: 0
        face.fields["Num.Quads"] = 0
        rows, _ = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        self.assertFalse(rows[0]["membership_complete"])
        self.assertEqual(rows[0]["declared_shell_count"], 0)
        mesh.GetMacroShells = lambda macros: [base.GetEntity(1, "SHELL", 10)]
        del face.fields["Num.Trias"]
        rows, _ = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        self.assertEqual(rows[0]["declared_shell_count"], 1)
        self.assertEqual(rows[0]["shell_count_origin"], "complete_GetMacroShells_return")
        self.assertTrue(rows[0]["membership_complete"])

    def test_projection_budget_does_not_silently_sample(self):
        base, mesh, face, snapshot = fixture()
        rows, entities = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        with patch.object(boundary, "MAX_PROJECTIONS", 3):
            result = boundary.measure_nodes(base, SimpleNamespace(), 1, snapshot, rows, entities, .02)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["measured_node_face_pairs"], 0)
        self.assertEqual(result["requested_node_face_pairs"], 4)

    def test_projection_missing_or_inconsistent_is_not_passed(self):
        base, mesh, face, snapshot = fixture()
        rows, entities = boundary.capture_membership(base, mesh, SimpleNamespace(NASTRAN=1), 1, [1])
        result = boundary.measure_nodes(base, SimpleNamespace(ProjectPointToContainer=lambda *args: None),
            1, snapshot, rows, entities, .02)
        self.assertEqual(result["status"], "incomplete")
        wrong = base.add("FACE", 3, area=1.)
        for output in ([[0., 0., 0.], 0., [], [], wrong], [[0., 0., 0.], 1., [], [], face]):
            with self.assertRaises(RuntimeError):
                boundary.measure_nodes(base, SimpleNamespace(ProjectPointToContainer=lambda *args: output),
                    1, snapshot, rows, entities, .02)

    def test_strict_request_and_schema(self):
        request = {"face_ids": [1], "tolerance": .02, "cylinders": []}
        self.assertEqual(boundary.normalize_request(request), request)
        for change in ({"face_ids": [1, 1]}, {"tolerance": 0.}, {"tolerance": float("nan")}, {"unexpected": True}):
            with self.assertRaises((ValueError, TypeError)):
                boundary.normalize_request({**request, **change})
        server = create_server(SimpleNamespace())
        tools = asyncio.run(server.list_tools())
        self.assertEqual(len(tools), 91)
        model = server._tool_manager.get_tool("ansa_mesh_interface_audit").fn_metadata.arg_model
        args = {"operation_id": "cad", "expected_target": {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
                "expected_model": {"epoch": 1}, "deck": 1, "entity_ids": [1], "cad_boundary": request}
        self.assertEqual(model.model_validate(args).cad_boundary.face_ids, [1])
        with self.assertRaises(ValidationError):
            model.model_validate({**args, "cad_boundary": {**request, "auto_heal": True}})


if __name__ == "__main__":
    unittest.main()
