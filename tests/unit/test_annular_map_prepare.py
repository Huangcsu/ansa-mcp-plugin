"""Risk-focused offline contracts for the restricted annular MapBlock tool."""
import copy
from collections import Counter
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

import ansa_mcp_bridge.operations as operations
from ansa_mcp_bridge.operations import annular_map_prepare as prepare
from ansa_mcp_bridge.operations import structured_evidence, structured_meshing
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import writes_model


def params():
    return {"deck": 1, "face_ids": list(range(11, 19)), "source_face_ids": [11, 12, 13, 14],
            "center": [0., 0., 0.], "axis": [0., 0., 1.], "inner_radius": 1.,
            "outer_radius": 2., "distance": 3., "target_length": .5,
            "max_chord_error": .2, "property_id": 90}


def row(kind, ident, **fields):
    return {"entity": {"type": kind, "id": ident}, "fields": fields}


def snapshot(rows):
    return {"entities": rows, "grids": [], "cad_sha256": "d" * 64}


def volume(ident):
    return SimpleNamespace(_id=ident, ansa_type=lambda deck: "VOLUME")


def block_fixture():
    fields = {key: 0 for key in ("Hexas", "Pentas", "Tetras", "Pyramids", "Polyhedrals", "Grids", "Internal grids")}
    fields.update(State="Ready to Mesh", Status="Unmeshed", PID=91)
    volumes = [{"volume_id": 101 + i, "card": {"fields": copy.deepcopy(fields)}, "part_id": 1,
                "native_mapping_status": "native_ready", "caps_match_complete_boundary": True,
                "face_mesh_membership_complete": True, "face_ids": [11 + i, 15 + i]}
               for i in range(4)]
    membership = [{"face_id": ident, "macro_face_ids": [ident], "macro_shell_ids": [200 + i],
                   "membership_complete": True, "shells": [{"id": 200 + i, "property_id": 10}]}
                  for i, ident in enumerate(params()["face_ids"])]
    block = {"volumes": volumes, "face_membership": membership,
             "surface_mesh": {"elements": [{"id": 200 + i} for i in range(8)],
                              "nodes": [{"id": 500 + i} for i in range(8)]}}
    before = snapshot([{**row("FACE", ident), "assignments": {"__prop__": 10}} for ident in params()["face_ids"]])
    prepared = {"clean": params(), "part_id": 1, "before": before}
    additions = {"VOLUME": [101, 102, 103, 104], "SHELL": list(range(200, 208)),
                 "GRID": list(range(500, 508)), "PSOLID": [91], "MAT1": [81]}
    return block, prepared, additions


class AnnularMapPrepareTests(unittest.TestCase):
    def test_public_schema_action_dispatch_limits_and_write_classification(self):
        calls = []
        def submit(tool, payload, operation_id, expected_target, expected_model, ttl):
            calls.append((tool, payload))
            return {"task": {"target": expected_target, "model": expected_model}, "state": "queued",
                    "result": None, "dispatch": None, "cancel_pending": False, "acked": False,
                    "operation_id": operation_id, "task_id": "task"}
        tool = create_server(SimpleNamespace(submit_task=submit))._tool_manager.get_tool("ansa_mesh_annular_source_prepare")
        model = tool.fn_metadata.arg_model
        schema = model.model_json_schema()["properties"]
        self.assertEqual(schema["face_ids"]["maxItems"], 64)
        self.assertEqual((schema["source_face_ids"]["minItems"], schema["source_face_ids"]["maxItems"]), (4, 4))
        common = {**params(), "operation_id": "prepare", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}}
        def invoke(change):
            values = model.model_validate({**common, **change})
            arguments = values.model_dump()
            arguments.update(expected_target=values.expected_target, expected_model=values.expected_model)
            return tool.fn(**arguments)
        invoke({"action": "plan"})
        self.assertEqual(calls[-1], ("mesh_annular_source_plan", params()))
        invoke({"action": "apply", "expected_plan_sha256": "a" * 64,
                "backup_path": str(Path.cwd() / "new-prepare.ansa")})
        self.assertEqual(calls[-1][0], "mesh_annular_source_apply")
        self.assertFalse(writes_model("mesh_annular_source_plan"))
        self.assertTrue(writes_model("mesh_annular_source_apply"))
        for change in ({"action": "apply"}, {"action": "plan", "expected_plan_sha256": "a" * 64},
                       {"action": "plan", "axis": [0., 0., 2.]},
                       {"action": "plan", "source_face_ids": [11, 12, 13, 99]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                invoke(change)
        for change in ({"face_ids": list(range(1, 66))}, {"source_face_ids": [11, 11, 12, 13]},
                       {"source_face_ids": [11, 12, 13]}, {"max_chord_error": True}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({**common, "action": "plan", **change})

    def test_normalization_inline_plan_and_bridge_dispatch(self):
        clean = prepare.normalize(params())
        self.assertEqual(clean, prepare.normalize(clean))
        budget = prepare._source_mesh_budget(clean)
        self.assertEqual((budget["circumferential_segments"], budget["radial_segments"],
                          budget["estimated_source_shells"]), (28, 2, 56))
        for change in ({"axis": [0., 0., 0.]}, {"outer_radius": .5}, {"target_length": True},
                       {"face_ids": params()["face_ids"] + [11]}, {"backup_path": "unexpected.ansa"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare.normalize({**params(), **change})
        prepared = {"clean": clean, "before": snapshot([]), "settings": SimpleNamespace(snapshot_sha256="b" * 64),
                    "source_mesh_budget": budget, "plan_sha256": "a" * 64}
        fake_ansa = SimpleNamespace(base=object(), constants=object(), mesh=object())
        with patch.dict(sys.modules, {"ansa": fake_ansa}), \
             patch.object(structured_meshing.catalog, "_deck", return_value=1), \
             patch.object(prepare, "preflight", return_value=prepared) as preflight:
            result = operations.execute(SimpleNamespace(assert_main=Mock()), "mesh_annular_source_plan", clean)
        preflight.assert_called_once()
        self.assertEqual(result["plan_sha256"], "a" * 64)
        self.assertEqual(result["source_mesh_budget"], budget)
        self.assertEqual(result["annular_tolerance_mm"], 1e-6)
        self.assertNotIn("report_id", result)

    def test_size_budget_accepts_known_3mm_110_shell_case(self):
        case = {**params(), "inner_radius": 10., "outer_radius": 19.,
                "distance": 16., "target_length": 3.}
        clean = prepare.normalize(case)
        budget = prepare._source_mesh_budget(clean)
        self.assertEqual((budget["circumferential_segments"], budget["radial_segments"],
                          budget["estimated_source_shells"]), (40, 3, 120))
        self.assertLessEqual(budget["estimated_source_shells"], budget["maximum_source_shells"])
        self.assertFalse(budget["estimate_is_native_upper_bound"])

    def test_tiny_target_is_rejected_before_backup_settings_or_mapblock(self):
        tiny = {**params(), "inner_radius": 10., "outer_radius": 19.,
                "distance": 16., "target_length": 1e-9}
        prepared = {"clean": tiny, "context": {}, "before": snapshot([]), "plan_sha256": "a" * 64}
        mesh = Mock()
        with patch.object(prepare.native, "_backup_preflight") as backup, \
             self.assertRaisesRegex(ValueError, "200-shell limit"):
            prepare.apply_prepared(Mock(), Mock(), mesh, Mock(), {
                **tiny, "expected_plan_sha256": "a" * 64, "backup_path": "unused.ansa"}, prepared)
        backup.assert_not_called()
        mesh.SetMeshParamTargetLength.assert_not_called()
        mesh.MapBlock.assert_not_called()

    def test_size_budget_caps_integer_estimates_and_rejects_float_overflow(self):
        boundary = {**params(), "inner_radius": .6, "outer_radius": 3.1,
                    "distance": 1., "target_length": .5}
        exact = prepare._source_mesh_budget(prepare.normalize(boundary))
        self.assertEqual(exact["estimated_source_shells"], 200)
        over = {**boundary, "inner_radius": .59}
        with self.assertRaisesRegex(ValueError, "200-shell limit"):
            prepare.normalize(over)

        overflow = {**params(), "inner_radius": 1e308, "outer_radius": 1.5e308,
                    "distance": 16., "target_length": 1e-308}
        with self.assertRaisesRegex(ValueError, "200-shell limit"):
            prepare.normalize(overflow)

    def test_source_budget_uses_radial_width_and_boundary_budget_limits_long_sweeps(self):
        case = {**params(), "inner_radius": 10., "outer_radius": 19.,
                "distance": 16., "target_length": 3.}
        short = prepare._source_mesh_budget(prepare.normalize(case))
        longer = prepare._source_mesh_budget(prepare.normalize({**case, "distance": 32.}))
        self.assertEqual(short["estimated_source_shells"], longer["estimated_source_shells"])
        self.assertGreater(longer["estimated_boundary_shells"], short["estimated_boundary_shells"])
        with self.assertRaisesRegex(ValueError, "200-shell limit"):
            prepare.normalize({**case, "inner_radius": 1., "distance": .01, "target_length": 1.})
        with self.assertRaisesRegex(ValueError, "5000-shell inspection limit"):
            prepare.normalize({**case, "distance": 1e300})

    def test_source_mesh_budget_is_visible_and_bound_into_plan_hash(self):
        clean = prepare.normalize(params())
        budget = prepare._source_mesh_budget(clean)
        context, database, before = {"epoch": 1}, "input.ansa", snapshot([])
        settings = SimpleNamespace(snapshot_sha256="b" * 64)
        digest = prepare._plan_sha256(clean, context, database, before, settings, budget)
        changed_budget = {**budget, "estimated_source_shells": budget["estimated_source_shells"] + 1}
        changed_digest = prepare._plan_sha256(clean, context, database, before, settings, changed_budget)
        self.assertNotEqual(digest, changed_digest)

        plan = prepare.public_plan({"clean": clean, "context": context, "database": database,
            "before": before, "settings": settings, "source_mesh_budget": budget, "plan_sha256": digest})
        self.assertEqual(plan["source_mesh_budget"], budget)
        self.assertEqual(plan["summary"]["source_shell_limit"], 200)

    def test_stale_plan_does_not_backup_or_call_native(self):
        prepared = {"clean": params(), "before": snapshot([]), "plan_sha256": "a" * 64}
        with patch.object(prepare.native, "_backup_preflight") as backup:
            mesh = Mock()
            result = prepare.apply_prepared(Mock(), Mock(), mesh, Mock(), {
                **params(), "expected_plan_sha256": "b" * 64, "backup_path": "unused.ansa"}, prepared)
        backup.assert_not_called()
        mesh.SetMeshParamTargetLength.assert_not_called()
        mesh.MapBlock.assert_not_called()
        self.assertFalse(result["possible_effects"])

    def test_backup_or_post_backup_preflight_failure_requires_recovery_without_mapblock(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "input.ansa"
            source_path.write_bytes(b"saved model")
            prepared = {"clean": params(), "before": snapshot([]), "plan_sha256": "a" * 64, "context": {"epoch": 1}}
            for failed_step in ("backup", "preflight"):
                with self.subTest(failed_step=failed_step), \
                     patch.object(prepare.native, "_backup_preflight", return_value=(Path(directory) / "backup.ansa", str(source_path), {"sha": "old"})), \
                     patch.object(prepare.native, "_verify_backup", side_effect=RuntimeError("SaveAs changed state") if failed_step == "backup" else None,
                                  return_value={"saved": True, "path": "backup.ansa"}), \
                     patch.object(prepare, "preflight", side_effect=RuntimeError("changed after backup")), \
                     patch.object(prepare, "_snapshot", return_value=prepared["before"]), \
                     patch.object(prepare.guards, "_database_name", return_value="changed.ansa"), \
                     patch.object(prepare.native.model_files, "_fingerprint", return_value={"sha": "changed"}):
                    mesh = Mock()
                    result = prepare.apply_prepared(Mock(), Mock(), mesh, Mock(), {
                        **params(), "expected_plan_sha256": "a" * 64, "backup_path": "backup.ansa"}, prepared)
                mesh.MapBlock.assert_not_called()
                self.assertTrue(result["backup_attempted"])
                self.assertTrue(result["possible_effects"])
                self.assertTrue(result["recovery_required"])
                self.assertEqual(result["current_database_path"], "changed.ansa")
                self.assertFalse(result["original_database_file_unchanged"])

    def test_single_native_call_and_complete_restore_on_success_and_exception(self):
        initial = {"element_type": "Quad", "element_order": "1st Order", "mesh_change_option": "x", "target_element_length": "9"}
        prepared = {"clean": params(), "context": {}, "settings": SimpleNamespace(
            settings=initial, current_criteria="bound", rows={}, f11={})}
        def restore(state, base, mesh, saved, result):
            result["restored"] = True
            return []
        for fail in (False, True):
            mesh = Mock()
            mesh.SetMeshParamTargetLength.return_value = 1
            mesh.MapBlock.side_effect = RuntimeError("native partial failure") if fail else None
            base = Mock()
            with self.subTest(fail=fail), \
                 patch.object(prepare.guards, "_same_context"), \
                 patch.object(prepare.meshing_context, "_summary", return_value={"snapshot_sha256": "a" * 64}), \
                 patch.object(prepare.meshing_context, "_settings", return_value={**initial, "target_element_length": ".5"}), \
                 patch.object(prepare.meshing_context, "_criteria", return_value="bound"), \
                 patch.object(prepare.meshing_context, "_parse_criteria", return_value=({}, {})), \
                 patch.object(prepare.meshing_context, "_read_f11_states", return_value=({}, [])), \
                 patch.object(prepare.meshing_context.native_quality, "_require_criteria_match"), \
                 patch.object(prepare.meshing_context, "_restore_context", side_effect=restore) as restoration, \
                 patch.object(prepare.features, "_entity", side_effect=lambda b, d, k, i: i):
                result = {}
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "partial failure"):
                        prepare._map_once(Mock(), base, mesh, prepared, result)
                else:
                    prepare._map_once(Mock(), base, mesh, prepared, result)
            mesh.SetMeshParamTargetLength.assert_called_once_with("absolute", .5)
            mesh.MapBlock.assert_called_once_with(params()["face_ids"])
            restoration.assert_called_once()
            self.assertTrue(result["meshing_context"]["restored"])
            base.BCSettingsSetValues.assert_not_called()

    def test_only_new_unused_shared_assets_are_deleted_after_four_bindings(self):
        before = snapshot([row("MAT1", 80), row("PSOLID", 90, MID=80)])
        volumes = [row("VOLUME", 101 + i, PID=91 + i // 2) for i in range(4)]
        assets = [row("PSOLID", 91, MID=81), row("PSOLID", 92, MID=81), row("MAT1", 81)]
        interim = snapshot(before["entities"] + volumes + assets)
        rebound = snapshot(before["entities"] + [row("VOLUME", 101 + i, PID=90) for i in range(4)] + assets)
        properties_deleted = snapshot([r for r in rebound["entities"] if r["entity"] not in (
            {"type": "PSOLID", "id": 91}, {"type": "PSOLID", "id": 92})])
        _, _, additions = prepare._inventory(before, interim)
        prepared = {"clean": params(), "before": before, "context": {}}
        base = Mock()
        base.SetEntityCardValues.return_value = 0
        base.GetEntityCardValues.return_value = {"PID": 90}
        base.DeleteEntity.return_value = 0
        base.GetEntity.return_value = None
        with patch.object(prepare.guards, "_same_context"), \
             patch.object(prepare, "_snapshot", side_effect=[rebound, properties_deleted]), \
             patch.object(prepare.features, "_entity", side_effect=lambda b, d, k, i: (k, i)):
            prepare._bind_and_clean(Mock(), base, prepared, interim, additions, {})
        self.assertEqual(base.SetEntityCardValues.call_count, 4)
        self.assertEqual([call.args[0] for call in base.DeleteEntity.call_args_list], [("PSOLID", 91), ("PSOLID", 92), ("MAT1", 81)])
        for call in base.DeleteEntity.call_args_list:
            self.assertEqual(call.kwargs, {"force": False, "compress": False})
        used = copy.deepcopy(rebound)
        used["entities"].append(row("SHELL", 201, PID=91))
        base.DeleteEntity.reset_mock()
        with patch.object(prepare.guards, "_same_context"), patch.object(prepare, "_snapshot", return_value=used), \
             patch.object(prepare.features, "_entity", side_effect=lambda b, d, k, i: (k, i)), \
             self.assertRaisesRegex(RuntimeError, "unused"):
            prepare._bind_and_clean(Mock(), base, prepared, interim, additions, {})
        base.DeleteEntity.assert_not_called()

    def test_four_complete_blocks_source_membership_and_200_shell_budget(self):
        block, prepared, additions = block_fixture()
        source_ids, _ = prepare._source_readback(block, prepared, additions)
        self.assertEqual(source_ids, [200, 201, 202, 203])
        for change in ("not_ready", "incomplete", "ambiguous", "over_budget"):
            candidate, added = copy.deepcopy(block), copy.deepcopy(additions)
            if change == "not_ready":
                candidate["volumes"][0]["native_mapping_status"] = "incomplete"
            elif change == "incomplete":
                candidate["face_membership"][0]["membership_complete"] = False
            elif change == "ambiguous":
                candidate["face_membership"][1]["macro_shell_ids"] = [200]
            else:
                candidate["face_membership"][0]["macro_shell_ids"] = list(range(1000, 1201))
                candidate["face_membership"][0]["shells"] = [{"id": i, "property_id": 10} for i in range(1000, 1201)]
                ids = sorted(i for r in candidate["face_membership"] for i in r["macro_shell_ids"])
                added["SHELL"] = ids
                candidate["surface_mesh"]["elements"] = [{"id": i} for i in ids]
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                prepare._source_readback(candidate, prepared, added)

    def test_protected_definitions_and_unknown_new_types_fail_closed(self):
        before = snapshot([row("ANSAPART", 1, **{"Last Edit": "old"}), row("MAT1", 80, E=123.),
                           row("WPLANE", 2, OriginX=0.)])
        added = [row("VOLUME", 101 + i, PID=90) for i in range(4)]
        after = snapshot(copy.deepcopy(before["entities"]) + added)
        _, _, additions = prepare._inventory(before, after)
        prepared = {"before": before, "clean": params(), "part_id": 1, "part": Mock(), "source_pids": []}
        with patch.object(prepare, "_expected_part_summary", return_value={}), \
             patch.object(prepare, "_part_aggregate", return_value={}), \
             patch.object(prepare, "_part_surface_display", return_value={"allowed_fields": []}), \
             patch.object(prepare, "_surface_projection", return_value={"status": "pass"}):
            prepare._verify_scope(Mock(), prepared, after, {"surface_mesh": {"elements": [], "nodes": []}, "face_membership": []}, Counter(), additions)
            for index, field, value in ((1, "E", 124.), (2, "OriginX", 1.)):
                changed = copy.deepcopy(after)
                changed["entities"][index]["fields"][field] = value
                with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "protected") as raised:
                    prepare._verify_scope(Mock(), prepared, changed, {"surface_mesh": {"elements": [], "nodes": []}, "face_membership": []}, Counter(), additions)
                self.assertEqual(raised.exception.diagnostics["before"]["fields"][field], before["entities"][index]["fields"][field])
                self.assertEqual(raised.exception.diagnostics["after"]["fields"][field], value)
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            prepare._inventory(before, snapshot(before["entities"] + [row("SOLID", 999)]))

    def test_part_area_is_recomputed_and_only_bounding_roundoff_is_permitted(self):
        box = [0., 0., 0., 1., 1., 1.]
        fields = {**dict(zip(("Min x", "Min y", "Min z", "Max x", "Max y", "Max z"), box)),
                  "Size x": 1., "Size y": 1., "Size z": 1., "Size min": 1., "Diagonal": 3.**.5,
                  "Area": 1.}
        prepared = {"clean": params(), "before": snapshot([row("ANSAPART", 1, **fields)]), "part_id": 1, "part": Mock()}
        surface = {"nodes": [{"id": i+1, "xyz": p} for i,p in enumerate(([0.,0.,0.],[1.,0.,0.],[1.,1.,0.],[0.,1.,0.]))],
                   "elements": [{"id": 200, "nodes": [1,2,3,4]}]}
        base = SimpleNamespace(BoundBox=lambda part: box)
        result = prepare._part_surface_display(base, prepared, row("ANSAPART", 1, **fields), surface)
        self.assertEqual(result["FE_triangle_area"], 1.)
        for field, value in (("Area", 1.01), ("Max x", 1.01), ("Size x", 1.01)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                prepare._part_surface_display(base, prepared, row("ANSAPART", 1, **{**fields, field: value}), surface)

    def test_absolute_perimeter_policy_requires_actual_surface_members_and_exact_controls(self):
        edge = {"status": "supported", "face_ids": [11], "point_count": 2,
                "endpoint_grid_ids": [1,2],
                "points": [{"id": 1, "xyz": [0.,0.,0.]}, {"id": 2, "xyz": [1.,0.,0.]}]}
        fields = {"Num. of Nodes": 0, "Elem.Length": .5, "Distort.Angle": 0.,
                  "Start Vector": "1, 0, 0", "End Vector": "-1, 0, 0"}
        points = {p["id"]: p["xyz"] for p in edge["points"]}
        projection = Mock(side_effect=lambda point, cons: (tuple(point),))
        with patch.object(prepare.features, "_entity"), patch.object(prepare.seeds, "_boundary", return_value=edge), \
             patch.dict(sys.modules, {"ansa": SimpleNamespace(calc=SimpleNamespace(ProjectPointToCons=projection))}):
            prepare._cons_perimeter(Mock(), {"clean": params()}, 1, fields, points)
            refined = prepare._cons_perimeter(Mock(), {"clean": params()}, 1, {**fields, "Elem.Length": .4}, points)
            self.assertTrue(refined["native_local_refinement"])
            for change in ({"Distort.Angle": 1.}, {"Elem.Length": 3.}, {"Num. of Nodes": 1}, {"Start Vector": "nan, 0, 0"}):
                with self.subTest(change=change), self.assertRaises(RuntimeError):
                    prepare._cons_perimeter(Mock(), {"clean": params()}, 1, {**fields, **change}, points)
            with self.assertRaisesRegex(RuntimeError, "GRID differs"):
                prepare._cons_perimeter(Mock(), {"clean": params()}, 1, fields, {1: [0.,0.,0.]})
            display = prepare._cons_perimeter(Mock(), {"clean": params()}, 1, {**fields, "Start Vector": "0, 1, 0"}, points)
            self.assertFalse(display["vector_display_matches_FE_chords"])
            projection.side_effect = lambda point, cons: ((point[0], point[1]+.3, point[2]),)
            with self.assertRaisesRegex(RuntimeError, "projection exceeds"):
                prepare._cons_perimeter(Mock(), {"clean": params()}, 1, fields, points)

    def test_perimeter_display_drift_and_actual_point_bounds_have_separate_budgets(self):
        before = [0.,0.,0.,1.,1.,1.]
        after = [0.,0.,0.,1.,1.,1.+2.4e-6]
        result = prepare._cons_bounds_display(before, after, after, .2)
        self.assertGreater(result["maximum_display_change_mm"], 1e-6)
        with self.assertRaises(RuntimeError):
            prepare._cons_bounds_display(before, [0.,0.,0.,1.,1.,1.3], [0.,0.,0.,1.,1.,1.3], .2)
        with self.assertRaises(RuntimeError):
            prepare._cons_bounds_display(before, after, before, .2)

    def test_complete_native_face_projection_must_pass_before_display_allowances(self):
        with patch.dict(sys.modules, {"ansa": SimpleNamespace(calc=object())}), \
             patch.object(prepare.features, "_entity"), \
             patch.object(prepare.structured_boundary, "measure_nodes") as measure:
            for status in ("incomplete", "fail"):
                measure.return_value = {"status": status}
                with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "projection"):
                    prepare._surface_projection(Mock(), {"clean": params()}, {"surface_mesh": {}, "face_membership": []})

    def test_success_returns_pageable_source_and_rechecks_source_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1, instance_id="i", session_id="s")
            before = snapshot([row("MAT1", 80), row("PSOLID", 90, MID=80)])
            after = snapshot(before["entities"] + [row("VOLUME", 101 + i, PID=90) for i in range(4)])
            prepared = {"clean": params(), "before": before, "context": {}, "plan_sha256": "a" * 64}
            source = {"snapshot_sha256": "c" * 64, "nodes": [{"id": 500, "xyz": [1., 0., 0.]}],
                      "elements": [{"id": 200, "type": "CTRIA3"}, {"id": 201, "type": "CQUAD4"}]}
            def map_once(state, base, mesh, current, result):
                result.update(api_called=True, settings_api_called=True)
                return [volume(101 + i) for i in range(4)]
            with patch.object(prepare.native, "_backup_preflight", return_value=(Path(directory) / "backup.ansa", "input.ansa", {})), \
                 patch.object(prepare.native, "_verify_backup", return_value={"saved": True}), \
                 patch.object(prepare, "preflight", return_value=prepared), \
                 patch.object(prepare, "_map_once", side_effect=map_once), \
                 patch.object(prepare, "_snapshot", return_value=after), \
                 patch.object(prepare.map_block_inspection, "capture", return_value={}), \
                 patch.object(prepare, "_source_readback", return_value=([200, 201], Counter())), \
                 patch.object(prepare.native, "capture_source", return_value=source) as capture, \
                 patch.object(prepare.annular_source, "audit", return_value={"status": "verified_mesh_template"}) as audit, \
                 patch.object(prepare, "_bind_and_clean"), patch.object(prepare, "_verify_scope", return_value={}), \
                 patch.object(prepare.guards, "_same_context"), \
                 patch("ansa_mcp_bridge.operations.mapped_surface._check_new_grids"):
                result = prepare.apply_prepared(state, Mock(), Mock(), Mock(), {
                    **params(), "expected_plan_sha256": "a" * 64, "backup_path": "backup.ansa"}, prepared)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(result["scope_verified"])
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(audit.call_args.args[1]["tolerance"], 1e-6)
            self.assertEqual(result["source_ids"], result["shell_ids"])
            report_id = result["source_evidence"]["report_id"]
            self.assertEqual(structured_evidence.read_page(state, 1, report_id, "/source_ids")["rows"], [200, 201])
            self.assertEqual(structured_evidence.read_page(state, 1, report_id, "/source_sha256")["rows"], ["c" * 64])
            self.assertEqual(structured_evidence.read_page(state, 1, report_id, "/source_snapshot/elements")["rows"], source["elements"])

    def test_failed_inventory_retains_new_ids_even_when_scope_is_violated(self):
        before = snapshot([row("MAT1", 80), row("PSOLID", 90, MID=80)])
        prepared = {"clean": params(), "before": before, "context": {}, "plan_sha256": "a" * 64}
        def partially_create(state, base, mesh, current, result):
            result["api_called"] = True
            raise RuntimeError("partial native change")
        for failure in ("removed", "unexpected", "unreadable"):
            after = snapshot(copy.deepcopy(before["entities"]) + [row("VOLUME", 101 + i) for i in range(4)])
            if failure == "removed":
                after["entities"].pop(0)
            elif failure == "unexpected":
                after["entities"].append(row("SOLID", 999))
            with self.subTest(failure=failure), \
                 patch.object(prepare.native, "_backup_preflight", return_value=(Path("backup.ansa"), "input.ansa", {})), \
                 patch.object(prepare.native, "_verify_backup", return_value={"saved": True}), \
                 patch.object(prepare, "preflight", return_value=prepared), \
                 patch.object(prepare, "_map_once", side_effect=partially_create), \
                 patch.object(prepare, "_snapshot", return_value=after, side_effect=RuntimeError("unreadable") if failure == "unreadable" else None):
                result = prepare.apply_prepared(Mock(), Mock(), Mock(), Mock(), {
                    **params(), "expected_plan_sha256": "a" * 64, "backup_path": "backup.ansa"}, prepared)
            self.assertTrue(result["recovery_required"])
            if failure == "unreadable":
                self.assertFalse(result["inventory_observation_complete"])
                self.assertIsNone(result["new_volume_ids"])
            else:
                self.assertTrue(result["inventory_observation_complete"])
                self.assertEqual(result["new_volume_ids"], [101, 102, 103, 104])
                self.assertEqual(bool(result["removed_original_entities"]), failure == "removed")
                self.assertEqual(bool(result["unexpected_new_entities"]), failure == "unexpected")

    def test_provenance_readback_failure_revokes_the_source_success_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1, instance_id="i", session_id="s",
                authorization={"backend_id": "b", "generation": 1}, identity=lambda: {"instance_id": "i", "session_id": "s"})
            prepared = {"plan_sha256": "a" * 64}
            for failure in ("changed", "unreadable"):
                native_result = {"ok": True, "status": "verified_source_preparation", "scope_verified": True,
                                 "source_ids": [200], "source_sha256": "b" * 64}
                with self.subTest(failure=failure), \
                     patch.dict(sys.modules, {"ansa": SimpleNamespace(base=object(), mesh=object(), constants=object())}), \
                     patch.object(prepare, "preflight", return_value=prepared), \
                     patch.object(prepare, "apply_prepared", return_value=native_result), \
                     patch.object(prepare.native, "capture_source", return_value={"snapshot_sha256": "c" * 64},
                                  side_effect=RuntimeError("unreadable") if failure == "unreadable" else None):
                    result = structured_meshing._with_operation_ledger(state, 1, "mesh_annular_source_apply", {"expected_plan_sha256": "a" * 64})
                self.assertFalse(result["ok"])
                self.assertFalse(result["scope_verified"])
                self.assertFalse(result["provenance_mapping_complete"])
                self.assertEqual(result["status"], "recovery_required")
                self.assertEqual(result["provenance_stage"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
