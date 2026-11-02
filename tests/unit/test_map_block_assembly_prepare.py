from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import map_block_assembly_prepare as assembly


def _params():
    return {
        "deck": 1,
        "face_groups": [[1, 2, 3, 4, 5, 6], [6, 7, 8, 9, 10, 11]],
        "target_length": 5.0,
        "property_id": 9,
    }


def _entity(kind, ident, fields, *, part_id=None, solid_ids=None):
    row = {
        "entity": {"type": kind, "id": ident},
        "card_fields": list(fields),
        "fields": fields,
    }
    if part_id is not None:
        row["part_id"] = part_id
    if solid_ids is not None:
        row["solid_ids"] = solid_ids
    return row


def _snapshot(rows):
    return {"entities": rows, "cad_sha256": "a"*64, "grids": [], "perimeters": []}


class MapBlockAssemblyPrepareTests(unittest.TestCase):
    def test_planar_face_derived_area_and_bounds_roundoff_are_recomputed(self):
        import copy,math
        from tests.unit.test_cad_sweep_topology import _cube_witness
        w=_cube_witness();corners=[r['position'] for r in w['vertices'] if r['id'] in (101,102,103,104)]
        bounds=[min(p[k] for p in corners) for k in range(3)]+[max(p[k] for p in corners) for k in range(3)]
        before={'entity':{'type':'FACE','id':11},'geometry':{'area':6.,'bounds':bounds,'normal':[0.,0.,1.]}}
        after=copy.deepcopy(before);after['geometry']['area']+=math.ulp(6.);after['geometry']['bounds'][2]-=1e-15
        self.assertEqual(len(assembly.single._check_face_geometry_roundoff(copy.deepcopy(before),copy.deepcopy(after),w)),2)
        self.assertEqual(before['geometry']['area'],6.)
        for field,value in (('area',6.0001),('bounds',[*bounds[:2],-1e-10,*bounds[3:]]),('normal',[0.,1e-5,1.])):
            changed=copy.deepcopy(before);changed['geometry'][field]=value
            with self.assertRaises(RuntimeError):assembly.single._check_face_geometry_roundoff(copy.deepcopy(before),changed,w)

    def test_unit_tangent_display_roundoff_is_consistent_but_real_change_fails(self):
        import copy
        before={'fields':{'End Vector':'0, -1, 0'}};after={'fields':{'End Vector':'0, -1, -3.55271e-16'}}
        result=assembly.single._check_cons_vector_roundoff(copy.deepcopy(before),copy.deepcopy(after))
        self.assertEqual(len(result),1);self.assertEqual(before['fields']['End Vector'],'0, -1, 0')
        bounds={'geometry':{'bounds':[10.,-15.,0.,10.,15.,0.]}}
        current={'geometry':{'bounds':[10.,-15.,-1.7763568394002505e-15,10.,15.,0.]}}
        self.assertEqual(len(assembly.single._check_cons_bounds_roundoff(copy.deepcopy(bounds),copy.deepcopy(current))),1)
        current['geometry']['bounds'][2]=-1e-10
        with self.assertRaises(RuntimeError):assembly.single._check_cons_bounds_roundoff(copy.deepcopy(bounds),current)
        for value in ('0, -1, .00001','invalid','0, nan, 0'):
            with self.assertRaises(RuntimeError):assembly.single._check_cons_vector_roundoff(copy.deepcopy(before),{'fields':{'End Vector':value}})

    def test_square_face_counts_are_squared_before_the_write_budget_gate(self):
        from tests.unit.test_map_block_batch import assembly as fixture
        block,witness=fixture(((0,0,0),(10,10,10)),((10,0,0),(20,10,10)))
        local={v['volume_id']:assembly.batch.local_witness(witness,v['face_ids']) for v in block['volumes']}
        layout=assembly.batch.assembly_layout(block,local)
        counts={r['id']:10 for r in witness['boundaries']}
        with self.assertRaisesRegex(ValueError,'400-SHELL'):
            assembly._mesh_budget({'entities':[]},layout,local,counts,1.)

    def test_normalize_allows_one_shared_face_and_rejects_repeated_or_partial_groups(self):
        clean = assembly.normalize(_params())
        self.assertEqual(clean["face_groups"], [[1, 2, 3, 4, 5, 6], [6, 7, 8, 9, 10, 11]])
        self.assertEqual(assembly.normalize(clean), clean)
        for groups in (
            [[1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1]],
            [[1, 2, 3, 4, 5, 6], [5, 6, 7, 8, 9, 10]],
            [[1, 2, 3, 4, 5]],
        ):
            with self.subTest(groups=groups), self.assertRaises((TypeError, ValueError)):
                assembly.normalize({**_params(), "face_groups": groups})

    def test_edge_lengths_must_divide_to_one_through_twenty_exact_segments(self):
        self.assertEqual(assembly._segments_for_length(100.0, 5.0, "edge"), 20)
        self.assertEqual(assembly._segments_for_length(5.0, 5.0, "edge"), 1)
        for length, target in ((10.0, 3.0), (105.0, 5.0), (1.0, 2.0)):
            with self.subTest(length=length, target=target), self.assertRaises(ValueError):
                assembly._segments_for_length(length, target, "edge")

    def test_volume_matching_uses_complete_face_sets_not_return_order(self):
        groups = assembly.normalize(_params())["face_groups"]
        rows = [
            {"volume_id": 21, "face_ids": groups[1]},
            {"volume_id": 20, "face_ids": groups[0]},
        ]
        matched = assembly._match_volume_groups(rows, groups)
        self.assertEqual(matched[tuple(groups[0])]["volume_id"], 20)
        self.assertEqual(matched[tuple(groups[1])]["volume_id"], 21)
        with self.assertRaises(RuntimeError):
            assembly._match_volume_groups(rows[:1], groups)

    def test_mesh_budget_reserves_surface_and_followup_fill_entities(self):
        face_boundaries = []
        cons_segments = {}
        for face_id in range(1, 7):
            cons_ids = list(range((face_id-1)*4+1, face_id*4+1))
            face_boundaries.append({"face_id": face_id, "cons_ids": cons_ids})
            cons_segments.update({ident: 1 for ident in cons_ids})
        budget = assembly._mesh_budget(
            {"entities": [{}]},
            {"bounds": {1: {"min": [0.0, 0.0, 0.0], "max": [5.0, 5.0, 5.0]}}},
            {1: {"face_boundaries": face_boundaries}},
            cons_segments,
            5.0,
        )
        self.assertEqual(budget["predicted_boundary_shells"], 6)
        self.assertEqual(budget["conservative_new_grid_upper_bound"], 8)
        self.assertEqual(budget["predicted_followup_solids"], 1)
        self.assertEqual(budget["predicted_followup_internal_grids"], 0)

    def test_only_this_call_auto_assets_are_rebound_and_deleted(self):
        groups = assembly.normalize(_params())["face_groups"]
        before = _snapshot([
            _entity("MAT1", 8, {"MID": 8, "RHO": 1.0}),
            _entity("PSOLID", 9, {"PID": 9, "MID": 8, "Num.Elem": 0}),
        ])
        interim_rows = list(before["entities"])
        for volume_id, auto_pid, auto_mid, face_ids in (
            (20, 101, 201, groups[0]),
            (21, 102, 202, groups[1]),
        ):
            interim_rows.extend((
                _entity("MAT1", auto_mid, {"MID": auto_mid}),
                _entity("PSOLID", auto_pid, {"PID": auto_pid, "MID": auto_mid, "Num.Elem": 0}),
                _entity("VOLUME", volume_id,
                        {"ID": volume_id, "PID": auto_pid, "Type": "Map Block"},
                        part_id=1, solid_ids=[]),
            ))
        interim = _snapshot(interim_rows)
        final_rows = list(before["entities"])
        for volume_id, face_ids in ((20, groups[0]), (21, groups[1])):
            final_rows.append(_entity(
                "VOLUME", volume_id,
                {"ID": volume_id, "PID": 9, "Type": "Map Block"},
                part_id=1, solid_ids=[],
            ))
        final = _snapshot(final_rows)
        inspected = {"volumes": [
            {"volume_id": 21, "face_ids": groups[1], "part_id": 1},
            {"volume_id": 20, "face_ids": groups[0], "part_id": 1},
        ]}
        base = Mock()
        base.SetEntityCardValues.return_value = 0
        base.GetEntityCardValues.return_value = {"PID": 9}
        base.DeleteEntity.return_value = 0
        base.GetEntity.return_value = None
        prepared = {
            "clean": {"deck": 1, "face_groups": groups, "property_id": 9},
            "part_id": 1,
            "before": before,
        }
        result = {}
        returned = [
            SimpleNamespace(_id=21, ansa_type=lambda deck: "VOLUME"),
            SimpleNamespace(_id=20, ansa_type=lambda deck: "VOLUME"),
        ]
        with patch.object(assembly.cad_map, "_snapshot", side_effect=[interim, final]), \
             patch.object(assembly.map_block_inspection, "capture", return_value=inspected), \
             patch.object(assembly.features, "_entity", side_effect=lambda _b, _d, kind, ident: (kind, ident)):
            new_ids = assembly._bind_assets(
                Mock(), base, Mock(), Mock(), prepared, returned, result
            )

        self.assertEqual(new_ids, [20, 21])
        self.assertEqual(result["created_auto_psolid_ids"], [101, 102])
        self.assertEqual(result["created_auto_mat1_ids"], [201, 202])
        self.assertEqual([call.args[1] for call in base.SetEntityCardValues.call_args_list],
                         [("VOLUME", 20), ("VOLUME", 21)])
        self.assertEqual([call.args[0] for call in base.DeleteEntity.call_args_list],
                         [("PSOLID", 101), ("PSOLID", 102), ("MAT1", 201), ("MAT1", 202)])
        for call in base.DeleteEntity.call_args_list:
            self.assertEqual(call.kwargs, {"force": False, "compress": False})
        self.assertEqual(result["volume_group_map"], [
            {"volume_id": 20, "face_ids": groups[0]},
            {"volume_id": 21, "face_ids": groups[1]},
        ])

    def test_stale_plan_does_not_back_up_or_call_native_mapblock(self):
        clean = assembly.normalize(_params())
        before = _snapshot([])
        prepared = {"clean": clean, "before": before, "context": {}, "plan_sha256": "a"*64}
        mesh = Mock()
        applying = {
            **clean,
            "expected_plan_sha256": "b"*64,
            "backup_path": "new.ansa",
        }
        with patch.object(assembly.native, "_backup_preflight") as backup, \
             patch.object(assembly.guards, "_same_context"), \
             patch.object(assembly.cad_map, "_snapshot", return_value=before):
            result = assembly.apply_prepared(Mock(), Mock(), mesh, Mock(), applying, prepared)
        backup.assert_not_called()
        mesh.MapBlock.assert_not_called()
        self.assertFalse(result["api_called"])
        self.assertFalse(result["possible_effects"])
        self.assertFalse(result["recovery_required"])


if __name__ == "__main__":
    unittest.main()
