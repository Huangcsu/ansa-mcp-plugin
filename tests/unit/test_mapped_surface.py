import copy
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import mapped_surface
from ansa_mcp_bridge.operations.mapped_surface_topology import four_sides, verify_lattice


def _cons(cons_id, start, end, segments):
    return {
        "cons_id": cons_id,
        "status": "supported",
        "segments": segments,
        "hot_points": [{"id": start}, {"id": end}],
        "native_fields": {
            "Frozen": "NO",
            "Collapsed": "NO",
            "Biasing": "LINEAR",
        },
    }


def _boundaries():
    return [
        _cons(10, 1, 2, 2),
        _cons(11, 2, 3, 2),
        _cons(12, 3, 4, 2),
        _cons(13, 4, 1, 2),
    ]


def _lattice_sides():
    return [
        [1, 2, 3],
        [3, 6, 9],
        [9, 8, 7],
        [7, 4, 1],
    ]


def _lattice_elements():
    return [
        {"id": 101, "type": "CQUAD4", "nodes": [1, 2, 5, 4]},
        {"id": 102, "type": "CQUAD4", "nodes": [2, 3, 6, 5]},
        {"id": 103, "type": "CQUAD4", "nodes": [4, 5, 8, 7]},
        {"id": 104, "type": "CQUAD4", "nodes": [5, 6, 9, 8]},
    ]


def _scope_row(entity_type, entity_id, **extra):
    return {
        "entity": {"type": entity_type, "id": entity_id},
        "fields": {},
        **extra,
    }


class MappedSurfaceTopologyTests(unittest.TestCase):
    def test_four_supported_cons_form_one_ordered_loop(self):
        ordered = four_sides([10, 11, 12, 13], _boundaries())

        self.assertEqual(
            [(row["cons_id"], row["start"], row["end"]) for row in ordered],
            [(10, 1, 2), (11, 2, 3), (12, 3, 4), (13, 4, 1)],
        )

    def test_opposite_segment_mismatch_is_rejected(self):
        boundaries = _boundaries()
        boundaries[2]["segments"] = 3

        with self.assertRaisesRegex(ValueError, "opposite CONS"):
            four_sides([10, 11, 12, 13], boundaries)

    def test_complete_two_by_two_lattice_is_verified(self):
        result = verify_lattice(_lattice_elements(), _lattice_sides())

        self.assertTrue(result["verified"])
        self.assertEqual(result["segments"], [2, 2])
        self.assertEqual(result["shell_count"], 4)
        self.assertEqual(result["node_count"], 9)
        self.assertEqual(result["free_edge_count"], 8)

    def test_one_reversed_quad_is_rejected(self):
        elements = _lattice_elements()
        elements[0]["nodes"] = [4, 5, 2, 1]

        with self.assertRaisesRegex(ValueError, "orientation"):
            verify_lattice(elements, _lattice_sides())

    def test_missing_quad_is_rejected(self):
        elements = _lattice_elements()[:-1]

        with self.assertRaisesRegex(ValueError, "SHELL count"):
            verify_lattice(elements, _lattice_sides())

    def test_quad_with_wrong_node_connectivity_is_rejected(self):
        elements = _lattice_elements()
        elements[0]["nodes"] = [1, 2, 5, 3]

        with self.assertRaisesRegex(ValueError, "free edges"):
            verify_lattice(elements, _lattice_sides())


class MappedSurfaceRoundoffTests(unittest.TestCase):
    def test_explicit_projection_restore_requires_unchanged_cad_and_both_points_on_cons(self):
        original={'fields':{'NID':101,'CP':0,'X1':1.,'X2':2.,'X3':3.}}
        changed=copy.deepcopy(original);changed['fields']['X1']+=2e-6
        for mode in ('valid','changed_cad','off_cons'):
            with self.subTest(mode=mode):
                prepared=self._prepared(original);prepared.update(context={},boundary_restore_tolerance_mm=1e-5)
                prepared['before']['cad_sha256']='a'*64
                prepared['witness']['boundaries'][0]['cons_id']=31
                base=Mock();base.SetEntityCardValues.return_value=0;changes=[]
                project=lambda point,cons:[list(point)] if mode!='off_cons' else [[point[0],point[1]+1.,point[2]]]
                with patch.dict(sys.modules,{'ansa':SimpleNamespace(calc=SimpleNamespace(ProjectPointToCons=project))}), \
                     patch.object(mapped_surface.features,'_entity',return_value=Mock(_id=101)), \
                     patch.object(mapped_surface.seeds,'_card',side_effect=[changed,original]), \
                     patch.object(mapped_surface.guards,'_same_context'), \
                     patch.object(mapped_surface.seeds,'_guard_snapshot',return_value={'cad_sha256':('b' if mode=='changed_cad' else 'a')*64}):
                    if mode=='valid':
                        mapped_surface._restore_boundary_roundoff(base,1,prepared,changes,state=Mock())
                        self.assertTrue(changes[0]['restored']);self.assertEqual(changes[0]['policy'],'explicit_native_perimeter_projection')
                    else:
                        with self.assertRaises(RuntimeError):mapped_surface._restore_boundary_roundoff(base,1,prepared,changes,state=Mock())
                        base.SetEntityCardValues.assert_not_called()

    def _prepared(self, card):
        return {
            "witness": {
                "boundaries": [{
                    "points": [{"id": 101}],
                }],
            },
            "before": {
                "grids": [{
                    "id": 101,
                    "card": card,
                }],
            },
        }

    def test_native_boundary_roundoff_is_restored_and_read_back_exactly(self):
        original = {
            "fields": {
                "NID": 101,
                "CP": 0,
                "X1": 1.0,
                "X2": 2.0,
                "X3": 3.0,
            },
        }
        native = copy.deepcopy(original)
        native["fields"]["X1"] += 5e-13
        node = Mock(_id=101)
        base = Mock()
        base.SetEntityCardValues.return_value = 0
        changes = []

        with (
            patch.object(mapped_surface.features, "_entity", return_value=node),
            patch.object(mapped_surface.seeds, "_card", side_effect=[native, original]),
        ):
            mapped_surface._restore_boundary_roundoff(
                base, 1, self._prepared(original), changes
            )

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["grid_id"], 101)
        self.assertEqual(changes[0]["native_xyz"], [1.0 + 5e-13, 2.0, 3.0])
        self.assertEqual(changes[0]["original_xyz"], [1.0, 2.0, 3.0])
        self.assertTrue(changes[0]["restored"])
        base.SetEntityCardValues.assert_called_once_with(
            1, node, {"X1": 1.0, "X2": 2.0, "X3": 3.0}
        )

    def test_boundary_drift_above_roundoff_is_rejected_without_setter(self):
        original = {
            "fields": {
                "NID": 101,
                "CP": 0,
                "X1": 1.0,
                "X2": 2.0,
                "X3": 3.0,
            },
        }
        native = copy.deepcopy(original)
        native["fields"]["X1"] += 2e-12
        base = Mock()

        with (
            patch.object(mapped_surface.features, "_entity", return_value=Mock(_id=101)),
            patch.object(mapped_surface.seeds, "_card", return_value=native),
            self.assertRaisesRegex(RuntimeError, "beyond numerical roundoff"),
        ):
            mapped_surface._restore_boundary_roundoff(
                base, 1, self._prepared(original), []
            )

        base.SetEntityCardValues.assert_not_called()

    def test_non_coordinate_boundary_field_change_is_rejected_without_setter(self):
        original = {
            "fields": {
                "NID": 101,
                "CP": 0,
                "X1": 1.0,
                "X2": 2.0,
                "X3": 3.0,
                "PS": 1,
            },
        }
        native = copy.deepcopy(original)
        native["fields"]["X1"] += 5e-13
        native["fields"]["PS"] = 2
        base = Mock()

        with (
            patch.object(mapped_surface.features, "_entity", return_value=Mock(_id=101)),
            patch.object(mapped_surface.seeds, "_card", return_value=native),
            self.assertRaisesRegex(RuntimeError, "beyond numerical roundoff"),
        ):
            mapped_surface._restore_boundary_roundoff(
                base, 1, self._prepared(original), []
            )

        base.SetEntityCardValues.assert_not_called()


class MappedSurfaceGridTests(unittest.TestCase):
    def test_new_grid_nondefault_cd_ps_seid_is_rejected(self):
        valid_fields = {"CD": None, "PS": 0, "SEID": ""}

        for field, value in (("CD", 2), ("PS", 9), ("SEID", 1)):
            card = {"fields": {**valid_fields, field: value}}
            with (
                self.subTest(field=field),
                patch.object(
                    mapped_surface.features,
                    "_entity",
                    return_value=Mock(_id=102),
                ),
                patch.object(mapped_surface.seeds, "_card", return_value=card),
                self.assertRaisesRegex(RuntimeError, field),
            ):
                mapped_surface._check_new_grids(Mock(), 1, [102])


class MappedSurfaceObservationTests(unittest.TestCase):
    def test_observed_ids_are_retained_when_scope_read_fails(self):
        prepared = {
            "context": {},
            "before": {
                "entities": [
                    _scope_row("GRID", 101),
                ],
            },
            "budget_base": {
                "entities": [],
                "grids": [],
            },
        }
        old = Mock(_id=101)
        old.ansa_type.return_value = "GRID"
        shell = Mock(_id=201)
        shell.ansa_type.return_value = "SHELL"
        grid = Mock(_id=202)
        grid.ansa_type.return_value = "GRID"
        observed = {}

        with (
            patch.object(mapped_surface.guards, "_same_context"),
            patch.object(
                mapped_surface.seeds,
                "_collect",
                return_value=[old, shell, grid],
            ),
            patch.object(
                mapped_surface.seeds,
                "_guard_snapshot",
                side_effect=RuntimeError("scope read failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "scope read failed"),
        ):
            mapped_surface._readback(
                Mock(), Mock(), Mock(), 1, prepared, observed=observed
            )

        self.assertEqual(observed["created_shell_ids"], [201])
        self.assertEqual(observed["created_shell_count"], 1)
        self.assertEqual(observed["new_grid_ids"], [202])
        self.assertEqual(
            observed["observed_added_entities"],
            [{"type": "GRID", "id": 202}, {"type": "SHELL", "id": 201}],
        )

    def test_stale_context_prevents_backup_and_cross_model_entity_attribution(self):
        prepared = {"context": {}, "backup_preflight": ("unused.ansa",),
                    "witness": {"seed_snapshot_sha256": "a"*64}}
        with (patch.object(mapped_surface.guards, "_same_context", side_effect=RuntimeError("stale context")),
              patch.object(mapped_surface.native, "_verify_backup") as backup,
              patch.object(mapped_surface.native, "_unconfirmed_backup", return_value={"verified":False}),
              patch.object(mapped_surface, "_observe_entities") as observe):
            result = mapped_surface.apply_prepared(Mock(), Mock(), Mock(), Mock(), {"deck":1}, prepared)
        backup.assert_not_called()
        observe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["mesh_started"])
        self.assertNotIn("created_shell_ids", result)
        self.assertIn("stale context", result["error"])


class MappedSurfaceAuditTests(unittest.TestCase):
    def test_source_face_state_and_counts_are_validated_against_verified_quad_lattice(self):
        prepared,before,after=self._case();prepared['source_faces'][0]['shell_count']=1
        before['entities'][1]['fields'].update({'Meshed With':'UNMESHED','Type':'MIXED','Num.Trias':0,'Num.Quads':0})
        after['entities'][1]['fields'].update({'Meshed With':'4 SIDED','Type':'QUAD','Num.Trias':0,'Num.Quads':1})
        changes=mapped_surface._audit(prepared,after,[201],[102])
        self.assertTrue(any(c['reason']=='verified_source_mesh_state' for c in changes))
        after['entities'][1]['fields']['Num.Quads']=2
        with self.assertRaisesRegex(RuntimeError,'state/count'):mapped_surface._audit(prepared,after,[201],[102])

    def test_opt_in_unmeshed_display_updates_keep_grid_and_unrelated_face_protected(self):
        prepared,before,after=self._case()
        prepared['unmeshed_tessellation_face_ids']=[11]
        after['entities'][2]['geometry'].update(area=2.2,center=[2.1,.5,0.])
        changes=mapped_surface._audit(prepared,after,[201],[102])
        self.assertTrue(any(c['reason']=='unmeshed_native_tessellation' for c in changes))
        for idx,field,value in ((3,'area',4.),(4,'X1',1e-6)):
            bad=copy.deepcopy(after)
            bad['entities'][idx]['geometry' if idx==3 else 'fields'][field]=value
            with self.subTest(field=field),self.assertRaises(RuntimeError):mapped_surface._audit(prepared,bad,[201],[102])

    def _case(self):
        before = {
            "cad_sha256": "a" * 64,
            "perimeters": {"10": [1, 2]},
            "entities": [
                _scope_row(
                    "ANSAPART",
                    1,
                    fields={"Mass": 1.0, "Area": 1.0},
                ),
                _scope_row(
                    "FACE",
                    10,
                    fields={"Frozen": "NO"},
                    geometry={"area": 1.0, "center": [0.5, 0.5, 0.0]},
                ),
                _scope_row(
                    "FACE",
                    11,
                    fields={"Frozen": "NO"},
                    geometry={"area": 2.0, "center": [2.0, 0.5, 0.0]},
                ),
                _scope_row(
                    "FACE",
                    12,
                    fields={"Frozen": "NO"},
                    geometry={"area": 3.0, "center": [3.0, 0.5, 0.0]},
                ),
                _scope_row(
                    "GRID",
                    101,
                    fields={"NID": 101, "CP": 0, "X1": 0.0, "X2": 0.0, "X3": 0.0},
                ),
            ],
        }
        after = copy.deepcopy(before)
        after["entities"].extend([
            _scope_row("SHELL", 201, fields={"PID": 7, "type": "CQUAD4"}),
            _scope_row("GRID", 102, fields={"NID": 102, "X1": 1.0}),
        ])
        prepared = {
            "before": before,
            "source_faces": [{"id": 10, "part_id": 1}],
            "witness": {
                "boundaries": [{"cons_id": 10, "face_ids": [10, 11]}],
            },
        }
        return prepared, before, after

    def test_protected_grid_face_or_unrelated_entity_change_is_rejected(self):
        prepared, _before, clean_after = self._case()
        self.assertEqual(
            mapped_surface._audit(prepared, clean_after, [201], [102]),
            [],
        )

        rounded = copy.deepcopy(clean_after)
        adjacent = next(
            item for item in rounded["entities"]
            if item["entity"] == {"type": "FACE", "id": 11}
        )
        adjacent["geometry"]["area"] = 2.0 * (1 + 5e-13)
        adjacent["geometry"]["center"][0] += 5e-13
        changes = mapped_surface._audit(prepared, rounded, [201], [102])
        self.assertEqual(
            {(row["field"], row["reason"]) for row in changes},
            {
                ("area", "adjacent_display_roundoff"),
                ("center", "adjacent_display_roundoff"),
            },
        )

        source_recomputed = copy.deepcopy(clean_after)
        source = next(
            item for item in source_recomputed["entities"]
            if item["entity"] == {"type": "FACE", "id": 10}
        )
        source["geometry"] = {"area": 1.1, "center": [0.6, 0.5, 0.0]}
        changes = mapped_surface._audit(
            prepared, source_recomputed, [201], [102]
        )
        self.assertEqual(
            {row["reason"] for row in changes},
            {"source_tessellation"},
        )

        mutations = (
            ("GRID", 101, lambda row: row["fields"].update({"X1": 1e-6})),
            ("FACE", 11, lambda row: row["fields"].update({"Frozen": "YES"})),
            ("FACE", 12, lambda row: row["geometry"].update({"area": 3.1})),
        )
        for entity_type, entity_id, mutate in mutations:
            after = copy.deepcopy(clean_after)
            row = next(
                item for item in after["entities"]
                if item["entity"] == {"type": entity_type, "id": entity_id}
            )
            mutate(row)
            with self.subTest(entity_type=entity_type), self.assertRaisesRegex(
                RuntimeError, "protected definition"
            ):
                mapped_surface._audit(prepared, after, [201], [102])

        after = copy.deepcopy(clean_after)
        after["entities"].append(_scope_row("CURVE", 301))
        with self.assertRaisesRegex(RuntimeError, "unrelated entities"):
            mapped_surface._audit(prepared, after, [201], [102])


class MappedSurfaceApplyTests(unittest.TestCase):
    def test_native_exception_still_restores_parameters_and_requires_recovery(self):
        before_settings = {
            "mesh_change_option": "macro perimeters",
            "element_type": "Mixed",
            "element_order": "2nd Order",
            "target_element_length": "average",
        }
        temporary_settings = {
            **before_settings,
            "mesh_change_option": "mesh",
            "element_type": "quad",
            "element_order": "1st Order",
        }
        prepared = {
            "witness": {
                "seed_snapshot_sha256": "a" * 64,
                "boundaries": [],
            },
            "before": {"snapshot_sha256": "b" * 64, "grids": []},
            "context": {"epoch": 1},
            "source_faces": [{"entity": Mock(), "id": 11}],
            "backup_preflight": (
                r"F:\backups\mapped.ansa",
                r"F:\models\source.ansa",
                {"sha256": "c" * 64},
            ),
        }
        params = {"deck": 1}
        base = Mock()
        base.BCSettingsSetValues.return_value = 0
        mesh = Mock()
        mesh.ReadMeshParams.return_value = 1
        mesh.Create4SidedMesh.side_effect = RuntimeError("native crash")
        readback = Mock(return_value={
            "lattice_verified": True,
            "source_geometry_unchanged": True,
            "seeds_preserved": True,
        })

        def save_params(path):
            Path(path).write_text("parameters", encoding="utf-8")
            return 1

        mesh.SaveMeshParams.side_effect = save_params

        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        ) as directory:
            state = Mock(session_dir=Path(directory))
            with (
                patch.object(
                    mapped_surface.native,
                    "_verify_backup",
                    return_value={"verified": True},
                ),
                patch.object(mapped_surface.guards, "_same_context"),
                patch.object(
                    mapped_surface.seeds,
                    "_guard_snapshot",
                    return_value=prepared["before"],
                ),
                patch.object(
                    mapped_surface.seeds,
                    "_materialize_points",
                    return_value=(copy.deepcopy(prepared), {"attempted": True}),
                ),
                patch.object(
                    mapped_surface.meshing,
                    "_settings",
                    side_effect=[
                        dict(before_settings),
                        dict(temporary_settings),
                        dict(before_settings),
                    ],
                ),
                patch.object(
                    mapped_surface,
                    "_restore_boundary_roundoff",
                    side_effect=RuntimeError("roundoff restore failed"),
                ),
                patch.object(
                    mapped_surface,
                    "_readback",
                    readback,
                ),
            ):
                result = mapped_surface.apply_prepared(
                    state, base, mesh, Mock(), params, prepared
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["mesh_started"], result)
        self.assertTrue(result["parameters_restored"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["recovery_required"])
        self.assertTrue(result["original_operation_must_be_queried"])
        self.assertIn("RuntimeError: native crash", result["errors"])
        self.assertIn(
            "mapped boundary restoration: RuntimeError: roundoff restore failed",
            result["errors"],
        )
        self.assertEqual(result["settings_after"], before_settings)
        mesh.SaveMeshParams.assert_called_once()
        mesh.ReadMeshParams.assert_called_once()
        readback.assert_called_once()
        self.assertIs(readback.call_args.kwargs["observed"], result)
        self.assertEqual(base.BCSettingsSetValues.call_count, 2)
        self.assertEqual(
            base.BCSettingsSetValues.call_args_list[0].args[0],
            {
                "mesh_change_option": "mesh",
                "element_type": "quad",
                "element_order": "1st Order",
            },
        )
        self.assertEqual(
            base.BCSettingsSetValues.call_args_list[1].args[0],
            {"mesh_change_option": "macro perimeters"},
        )


if __name__ == "__main__":
    unittest.main()
