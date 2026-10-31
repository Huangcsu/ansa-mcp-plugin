"""Risk-based coverage for native CONS seed capture and guarded application."""
import copy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import cons_seed


class Entity:
    def __init__(self, kind, ident, position=None):
        self.kind = kind
        self._id = ident
        self.position = position

    def ansa_type(self, deck):
        return self.kind


def _row(kind, ident, *, fields=None, geometry=None):
    row = {"entity": {"type": kind, "id": ident}, "fields": fields or {}}
    if geometry is not None:
        row["geometry"] = geometry
    return row


def _audit_case():
    entities = [
        _row("CONS", 10, fields={
            "Num. of Nodes": 1, "Start Vector": [0, 0, 0], "End Vector": [1, 0, 0],
            "Order of Nodes": "1st", "Frozen": "NO", "Collapsed": "NO", "Biasing": "LINEAR",
        }),
        _row("CONS", 20, fields={"Name": "outside", "Num. of Nodes": 2}),
        _row("FACE", 5, geometry={"area": 10.0, "center": [0.5, 0.5, 0.0], "bounds": [0, 0, 0, 1, 1, 0]}),
        _row("ANSAPART", 3, fields={
            "Name": "body", "Mass": 1.0, "Area": 10.0,
            "COG x": 0.5, "COG y": 0.5, "COG z": 0.0,
        }),
        _row("GRID", 1, fields={"X1": 0.0, "X2": 0.0, "X3": 0.0}),
        _row("GRID", 2, fields={"X1": 1.0, "X2": 0.0, "X3": 0.0}),
        _row("GRID", 7, fields={"X1": 4.0, "X2": 0.0, "X3": 0.0}),
        _row("GRID", 8, fields={"X1": 5.0, "X2": 0.0, "X3": 0.0}),
        _row("CQUAD4", 40, fields={"EID": 40, "PID": 3, "G1": 1, "G2": 2, "G3": 7, "G4": 8}),
    ]
    before = {
        "entities": entities,
        "grids": [
            {"id": 1, "xyz": [0.0, 0.0, 0.0]},
            {"id": 2, "xyz": [1.0, 0.0, 0.0]},
            {"id": 7, "xyz": [4.0, 0.0, 0.0]},
            {"id": 8, "xyz": [5.0, 0.0, 0.0]},
        ],
        "perimeters": {10: [1, 2], 20: [7, 8]},
        "cad_sha256": "a" * 64,
    }
    prepared = {
        "before": before,
        "assignments": [{"cons_id": 10, "segments": 2}],
        "witness": {
            "boundaries": [{"cons_id": 10, "face_ids": [5], "endpoint_grid_ids": [1, 2]}],
            "geometry_witness": {"parts": [{"type": "FACE", "id": 5, "part_id": 3}]},
        },
    }
    readback = {"boundaries": [{"cons_id": 10, "status": "supported", "segments": 2}]}
    return prepared, before, readback


class ConsSeedTests(unittest.TestCase):
    def test_nanometer_derived_bounds_do_not_relax_grid_or_cad_protection(self):
        prepared,before,readback=_audit_case()
        after=copy.deepcopy(before)
        after['entities'][2]['geometry']['bounds'][0]+=5e-7
        self.assertEqual(cons_seed._audit(prepared,after,readback)[0],[])
        large=copy.deepcopy(after);large['entities'][2]['geometry']['bounds'][0]=1e-5
        self.assertTrue(cons_seed._audit(prepared,large,readback)[0])
        moved=copy.deepcopy(after);moved['entities'][4]['fields']['X1']+=5e-7
        self.assertTrue(cons_seed._audit(prepared,moved,readback)[0])
        changed=copy.deepcopy(after);changed['cad_sha256']='b'*64
        self.assertTrue(cons_seed._audit(prepared,changed,readback)[0])

    def test_global_export_rejects_orphan_cad_before_export(self):
        curve = Entity("CURVE", 90)
        base = SimpleNamespace(GetEntityPart=Mock(return_value=None))
        def collect(_base, _deck, _container, kind):
            return [curve] if kind == "CURVE" else []
        with patch.object(cons_seed, "_collect", side_effect=collect), patch.object(cons_seed.features, "_export") as export:
            with self.assertRaisesRegex(ValueError, "Part ownership"):
                cons_seed._guard_snapshot(SimpleNamespace(), base, 1)
            export.assert_not_called()

    def test_global_guard_preserves_unrelated_local_coordinate_grid_card(self):
        grid, face, part = Entity("GRID", 1), Entity("FACE", 5), Entity("ANSAPART", 3)
        base = SimpleNamespace(GetEntityPart=Mock(return_value=part))
        card = {"entity": {"type": "GRID", "id": 1}, "fields": {"CP": 42, "X1": 1.}}
        def collect(_base, _deck, _container, kind):
            return {"GRID": [grid], "FACE": [face]}.get(kind, [])
        with (patch.object(cons_seed, "_collect", side_effect=collect),
              patch.object(cons_seed, "_card", return_value=card),
              patch.object(cons_seed.catalog, "_entity_reference", return_value={"type": "ANSAPART", "id": 3}),
              patch.object(cons_seed, "_grid", side_effect=AssertionError("global node must retain raw CP")),
              patch.object(cons_seed.features, "_export", return_value=b"fixture"),
              patch("ansa_mcp_bridge.operations.geometry_features_iges.canonical_digest", return_value="a"*64)):
            snapshot = cons_seed._guard_snapshot(SimpleNamespace(), base, 1)
        self.assertEqual(snapshot["grids"], [{"id": 1, "card": card}])

    def test_preflight_accepts_documented_zero_macro_shell_sentinel_only(self):
        face, macro = Entity("FACE", 5), Entity("MACRO", 6)
        witness = {"seed_snapshot_sha256": "d"*64, "boundaries": [{
            "cons_id": 10, "face_ids": [5], "status": "supported",
            "native_fields": {"Frozen": "NO", "Collapsed": "NO", "Biasing": "LINEAR"}}]}
        params = {"deck": 1, "face_ids": [5], "assignments": [{"cons_id": 10, "segments": 4}],
                  "expected_seed_snapshot_sha256": "d"*64, "backup_path": "new.ansa"}
        base = SimpleNamespace(GetFaceMacro=Mock(return_value=macro))
        mesh = SimpleNamespace(NumberPerimeters=Mock(), GetMacroShells=Mock(return_value=0))
        with (patch.dict(sys.modules, {"ansa": SimpleNamespace(mesh=mesh)}),
              patch.object(cons_seed.guards, "_execution_context", return_value={}),
              patch.object(cons_seed.guards, "_same_context"),
              patch.object(cons_seed, "capture", return_value=witness),
              patch.object(cons_seed.features, "_entity", return_value=face),
              patch.object(cons_seed, "_guard_snapshot", return_value={"entities": [], "grids": []}),
              patch.object(cons_seed, "_point_budget"),
              patch.object(cons_seed, "_collect", return_value=[]),
              patch.object(cons_seed.native, "_backup_preflight", return_value=()),):
            cons_seed.preflight(SimpleNamespace(assert_main=lambda: None), base, object(), params)
            mesh.GetMacroShells.return_value = None
            with self.assertRaisesRegex(ValueError, "unmeshed"):
                cons_seed.preflight(SimpleNamespace(assert_main=lambda: None), base, object(), params)

    def test_global_point_budget_rejects_before_cons_grid_collection(self):
        edges = [Entity("CONS", 1), Entity("CONS", 2)]
        def collect(_base, _deck, container, kind):
            self.assertIsNone(container, "must reject before materializing any CONS")
            return edges if kind == "CONS" else []
        with (patch.object(cons_seed, "_collect", side_effect=collect),
              patch.object(cons_seed, "_card", return_value={"fields": {"Order of Nodes": "1st", "Num. of Nodes": 4999}})):
            with self.assertRaisesRegex(ValueError, "point materialization exceeds"):
                cons_seed._guard_snapshot(SimpleNamespace(), object(), 1, materialize=True)

    def test_final_point_budget_uses_original_base_not_already_materialized_entities(self):
        edge = Entity("CONS", 1)
        entities = [Entity("GRID", i) for i in range(1, 6001)]
        grids = entities[1000:]
        def collect(_base, _deck, container, kind):
            return {"CONS": [edge], "__ALL_ENTITIES__": entities, "GRID": grids}.get(kind, [])
        def reference(_base, _deck, entity, *_args):
            return {"type": entity.kind, "id": entity._id}
        with (patch.object(cons_seed, "_collect", side_effect=collect),
              patch.object(cons_seed, "_card", return_value={"fields": {"Order of Nodes": "1st", "Num. of Nodes": 4998}}),
              patch.object(cons_seed.catalog, "_entity_reference", side_effect=reference),
              patch.object(cons_seed.native, "_scope_row", side_effect=lambda _b, _d, _e, ref: {"entity": ref, "fields": {}}),
              patch.object(cons_seed.features, "_export", return_value=b"fixture"),
              patch("ansa_mcp_bridge.operations.geometry_features_iges.canonical_digest", return_value="a"*64)):
            snapshot = cons_seed._guard_snapshot(SimpleNamespace(), object(), 1, materialize=True,
                                                budget_base={"entities": [None]*1000, "grids": []})
        self.assertEqual(len(snapshot["entities"]), 6000)

    def test_assignments_rejects_boolean_segment_count(self):
        with self.assertRaisesRegex(ValueError, "explicit integer"):
            cons_seed._assignments([{"cons_id": 10, "segments": True}])

    def test_assignments_rejects_duplicate_cons_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate CONS"):
            cons_seed._assignments([
                {"cons_id": 10, "segments": 2},
                {"cons_id": 10, "segments": 3},
            ])

    def test_assignments_rejects_total_seed_point_budget_overflow(self):
        with self.assertRaisesRegex(ValueError, "budget"):
            cons_seed._assignments([
                {"cons_id": 10, "segments": 5000},
                {"cons_id": 11, "segments": 5000},
            ])

    def _boundary(self, *, point_count, internal_nodes, hot_point_positions=None):
        cons = Entity("CONS", 10)
        grids = [Entity("GRID", index) for index in range(1, point_count + 1)]
        hot_points = [
            Entity("HOT POINT", 101, [0.0, 0.0, 0.0]),
            Entity("HOT POINT", 102, [float(point_count - 1), 0.0, 0.0]),
        ]
        if hot_point_positions is not None:
            for entity, position in zip(hot_points, hot_point_positions):
                entity.position = position
        face = Entity("FACE", 5)
        card = {"fields": {
            "Num. of Nodes": internal_nodes,
            "Order of Nodes": "1st",
            "Frozen": "NO",
            "Collapsed": "NO",
            "Biasing": "LINEAR",
        }}
        coords = {entity._id: [float(entity._id - 1), 0.0, 0.0] for entity in grids}
        base = SimpleNamespace(GetFacesOfCons=Mock(return_value=[face]))

        def collect(_base, _deck, _container, kind):
            return {"GRID": grids, "HOT POINT": hot_points}.get(kind, [])

        with (
            patch.object(cons_seed, "_card", return_value=card),
            patch.object(cons_seed, "_collect", side_effect=collect),
            patch.object(cons_seed, "_grid", side_effect=lambda _b, _d, entity: {
                "id": entity._id, "xyz": coords[entity._id], "card": {},
            }),
            patch.object(cons_seed.catalog, "_entity_reference", side_effect=lambda _b, _d, entity, *_a: {
                "type": entity.kind, "id": entity._id,
            }),
        ):
            return cons_seed._boundary(base, 1, cons, include_points=True)

    def test_read_boundary_never_instantiates_perimeter_grid(self):
        edge, face = Entity("CONS", 10), Entity("FACE", 5)
        vertices = [Entity("HOT POINT", 1, [0., 0., 0.]), Entity("HOT POINT", 2, [2., 0., 0.])]
        card = {"fields": {"Num. of Nodes": 2, "Order of Nodes": "1st", "Frozen": "NO",
                           "Collapsed": "NO", "Biasing": "LINEAR"}}
        def collect(_base, _deck, _container, kind):
            self.assertEqual(kind, "HOT POINT", "read must not collect GRID from CONS")
            return vertices
        with (patch.object(cons_seed, "_card", return_value=card),
              patch.object(cons_seed, "_collect", side_effect=collect),
              patch.object(cons_seed.catalog, "_entity_reference", return_value={"type": "FACE", "id": 5})):
            result = cons_seed._boundary(SimpleNamespace(GetFacesOfCons=lambda _e: [face]), 1, edge)
        self.assertEqual(result["segments"], 3)
        self.assertIsNone(result["points"])
        self.assertIsNone(result["point_count"])
        self.assertEqual(result["expected_point_count"], 4)
        self.assertEqual(result["point_validation"], "deferred_to_backed_up_write")

    def test_point_materialization_preserves_existing_grid_cards(self):
        prepared, before, _readback = _audit_case()
        changed = copy.deepcopy(before)
        changed["grids"][0]["xyz"][0] = 9.
        with patch.object(cons_seed, "_guard_snapshot", return_value=changed):
            with self.assertRaisesRegex(RuntimeError, "existing GRID"):
                cons_seed._materialize_points(SimpleNamespace(), object(), 1, prepared)

    def test_materialization_accepts_only_new_perimeter_grid_and_rejects_old_interior_reseeding(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                prepared, before, _readback = _audit_case()
                added = _row("GRID", 9, fields={"X1": .5})
                if existing:
                    before["entities"].append(added)
                    before["grids"].append({"id": 9, "xyz": [.5, 0., 0.]})
                initialized = copy.deepcopy(before)
                if not existing:
                    initialized["entities"].append(added)
                    initialized["grids"].append({"id": 9, "xyz": [.5, 0., 0.]})
                initialized["perimeters"][10] = [1, 2, 9]
                boundary = {"cons_id": 10, "status": "supported", "segments": 3 if existing else 2,
                            "points": [{"id": i} for i in (1, 2, 9)], "endpoint_grid_ids": [1, 2]}
                with (patch.object(cons_seed, "_guard_snapshot", return_value=initialized),
                      patch.object(cons_seed.features, "_entity", return_value=Entity("CONS", 10)),
                      patch.object(cons_seed, "_boundary", return_value=boundary)):
                    if existing:
                        with self.assertRaisesRegex(ValueError, "pre-existing perimeter interior GRID"):
                            cons_seed._materialize_points(SimpleNamespace(), object(), 1, prepared)
                    else:
                        audited, result = cons_seed._materialize_points(SimpleNamespace(), object(), 1, prepared)
                        self.assertEqual(result["new_virtual_point_ids"], [9])
                        self.assertNotIn(9, audited["protected_grid_ids"])

    def test_boundary_distinguishes_card_internal_nodes_from_points_and_edges(self):
        result = self._boundary(point_count=4, internal_nodes=2)

        self.assertEqual(result["native_internal_node_count"], 2)
        self.assertEqual(result["point_count"], 4)
        self.assertEqual(result["segments"], 3)
        self.assertEqual(result["status"], "supported")

    def test_boundary_points_inconsistent_with_card_are_retained_as_unsupported(self):
        result = self._boundary(point_count=2, internal_nodes=1)

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["point_count"], 2)
        self.assertIsNone(result["segments"])

    def test_boundary_hot_points_not_matching_boundary_endpoints_are_unsupported(self):
        result = self._boundary(
            point_count=3,
            internal_nodes=1,
            hot_point_positions=[[0.0, 0.0, 0.0], [8.0, 0.0, 0.0]],
        )

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["point_count"], 3)
        self.assertIsNone(result["segments"])

    def test_audit_rejects_out_of_scope_grid_fe_and_cons_changes(self):
        mutations = {
            "GRID": lambda snapshot: (
                snapshot["entities"][4]["fields"].update(X1=9.0),
                snapshot["grids"][0].update(xyz=[9.0, 0.0, 0.0]),
            ),
            "FE": lambda snapshot: snapshot["entities"][8]["fields"].update(PID=99),
            "CONS": lambda snapshot: snapshot["entities"][1]["fields"].update(Name="changed"),
        }
        for kind, mutate in mutations.items():
            with self.subTest(kind=kind):
                prepared, before, readback = _audit_case()
                after = copy.deepcopy(before)
                mutate(after)

                errors, _changes = cons_seed._audit(prepared, after, readback)

                self.assertTrue(errors)

    def test_newly_materialized_target_interior_grid_can_change_but_original_grid_cannot(self):
        prepared, before, readback = _audit_case()
        prepared["protected_grid_ids"] = [1, 2, 7, 8]
        before["entities"].append(_row("GRID", 9, fields={"X1": .5}))
        before["grids"].append({"id": 9, "xyz": [.5, 0., 0.]})
        before["perimeters"][10] = [1, 2, 9]
        after = copy.deepcopy(before)
        after["entities"][-1]["fields"]["X1"] = .6
        after["grids"][-1]["xyz"][0] = .6
        self.assertEqual(cons_seed._audit(prepared, after, readback)[0], [])
        after["entities"][4]["fields"]["X1"] = .1
        self.assertTrue(cons_seed._audit(prepared, after, readback)[0])

    def test_audit_accepts_target_and_tessellation_fields_with_unchanged_cad_hash(self):
        prepared, before, readback = _audit_case()
        after = copy.deepcopy(before)
        after["entities"][0]["fields"].update({
            "Num. of Nodes": 2, "Start Vector": [0.0, 0.0, 0.0], "End Vector": [2.0, 0.0, 0.0],
        })
        after["entities"][2]["geometry"].update(area=12.0, center=[1.0, 1.0, 0.0])
        after["entities"][3]["fields"].update({
            "Mass": 1.2, "Area": 12.0, "COG x": 1.0, "COG y": 1.0,
        })

        errors, changes = cons_seed._audit(prepared, after, readback)

        self.assertEqual(errors, [])
        self.assertEqual(len(changes), 3)

    def test_audit_rejects_tessellation_change_when_cad_hash_changes(self):
        prepared, before, readback = _audit_case()
        after = copy.deepcopy(before)
        after["entities"][0]["fields"]["Num. of Nodes"] = 2
        after["cad_sha256"] = "b" * 64

        errors, _changes = cons_seed._audit(prepared, after, readback)

        self.assertIn("native CAD export changed", errors)

    def test_affected_bbox_drift_is_bounded_and_requires_identical_cad(self):
        prepared, before, readback = _audit_case()
        for delta, allowed in ((2e-8, True), (2e-6, False)):
            after = copy.deepcopy(before)
            after["entities"][2]["geometry"]["bounds"][3] += delta
            before["entities"][3]["fields"]["Max y"] = 1.
            after["entities"][3]["fields"]["Max y"] = 1. + delta
            with self.subTest(delta=delta):
                self.assertEqual(not cons_seed._audit(prepared, after, readback)[0], allowed)

    def test_extra_face_part_has_no_target_derived_field_exemption(self):
        prepared, before, readback = _audit_case()
        prepared["witness"]["geometry_witness"]["parts"].append({"type": "FACE", "id": 8, "part_id": 4})
        before["entities"].append(_row("ANSAPART", 4, fields={"Max y": 1., "Mass": 2.}))
        for field, value in (("Max y", 1. + 2e-8), ("Mass", 3.)):
            after = copy.deepcopy(before)
            after["entities"][-1]["fields"][field] = value
            with self.subTest(field=field):
                self.assertTrue(cons_seed._audit(prepared, after, readback)[0])

    def test_audit_rejects_non_tessellation_target_card_change(self):
        prepared, before, readback = _audit_case()
        after = copy.deepcopy(before)
        after["entities"][0]["fields"].update({"Num. of Nodes": 2, "Frozen": "YES"})

        errors, _changes = cons_seed._audit(prepared, after, readback)

        self.assertTrue(any("original entity definitions" in error for error in errors))

    def test_apply_failure_returns_recovery_status_without_retrying_native_setter(self):
        prepared, before, readback = _audit_case()
        witness = {
            "source_scope": {"face_ids": [5], "cons_ids": [10]},
            "seed_snapshot_sha256": "d" * 64,
            "boundaries": [{"cons_id": 10, "points": []}],
        }
        prepared.update({
            "witness": witness,
            "context": {"epoch": 1},
            "backup_preflight": ("backup.ansa", "source.ansa", {}),
        })
        params = {"deck": 1}
        entity = Entity("CONS", 10)
        mesh = SimpleNamespace(NumberPerimeters=Mock(return_value=0))
        fake_ansa = SimpleNamespace(mesh=mesh, calc=SimpleNamespace())

        with (
            patch.dict(sys.modules, {"ansa": fake_ansa}),
            patch.object(cons_seed.guards, "_same_context"),
            patch.object(cons_seed.native, "_verify_backup", return_value={"verified": True}),
            patch.object(cons_seed, "_guard_snapshot", side_effect=[before, copy.deepcopy(before)]),
            patch.object(cons_seed, "capture", side_effect=[witness, witness]),
            patch.object(cons_seed, "_materialize_points", return_value=(prepared, {"attempted": True})),
            patch.object(cons_seed, "_boundary", return_value={"cons_id": 10, "status": "supported", "points": []}),
            patch.object(cons_seed.features, "_entity", return_value=entity),
            patch.object(cons_seed, "_audit", return_value=([], [])),
        ):
            result = cons_seed.apply_prepared(SimpleNamespace(), object(), object(), params, prepared)

        mesh.NumberPerimeters.assert_called_once_with(
            [entity], "2", apply_multiple_number=False, remesh_macros=False,
            apply_number_of="edges", use_ansa_defaults_values=False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["original_operation_must_be_queried"])
        self.assertEqual(result["summary"]["number_perimeters_calls"], 1)

        # A later native point collection can differ from the earlier card
        # capture. Its own segment/point counts must match the request too.
        mesh.NumberPerimeters.return_value = 1
        points = [{"id": i, "xyz": [float(i), 0., 0.]} for i in range(4)]
        fake_ansa.calc.ProjectPointToCons = lambda xyz, _edge: xyz
        final = {"cons_id": 10, "status": "supported", "segments": 3, "point_count": 4, "points": points}
        with (
            patch.dict(sys.modules, {"ansa": fake_ansa}),
            patch.object(cons_seed.guards, "_same_context"),
            patch.object(cons_seed.native, "_verify_backup", return_value={"verified": True}),
            patch.object(cons_seed, "_guard_snapshot", side_effect=[before, copy.deepcopy(before)]),
            patch.object(cons_seed, "capture", side_effect=[witness, witness]),
            patch.object(cons_seed, "_materialize_points", return_value=(prepared, {"attempted": True})),
            patch.object(cons_seed, "_boundary", return_value=final),
            patch.object(cons_seed.features, "_entity", return_value=entity),
            patch.object(cons_seed, "_audit", return_value=([], [])),
        ):
            result = cons_seed.apply_prepared(SimpleNamespace(), object(), object(), params, prepared)
        self.assertFalse(result["ok"])
        self.assertIn("final materialized boundary points differ from requested edge count", result["errors"])


if __name__ == "__main__":
    unittest.main()
