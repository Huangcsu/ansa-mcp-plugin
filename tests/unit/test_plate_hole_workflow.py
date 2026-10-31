import math
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import plate_hole_workflow as workflow


def params():
    return {
        "length": 60.0,
        "width": 40.0,
        "thickness": 6.0,
        "hole_radius": 6.0,
        "transition_half_size": 9.0,
        "target_length": 1.5,
        "thickness_layers": 4,
        "hole_segments": 48,
        "radial_segments": 4,
        "center": [0.0, 0.0, 0.0],
    }


class Entity:
    def __init__(self, entity_id, kind, position=None):
        self._id = entity_id
        self.kind = kind
        self.position = position

    def ansa_type(self, _deck):
        return self.kind


class SeedBase:
    def __init__(self):
        self.cons = Entity(199, "CONS")
        self.points = [
            Entity(1, "HOT POINT", [-1.288310667e-6, -9.0, 3.0]),
            Entity(2, "HOT POINT", [9.0, -9.0, 3.0]),
        ]

    def CollectEntities(self, _deck, container, kind):
        if kind == "CONS":
            return [self.cons]
        if kind == "HOT POINT" and container is self.cons:
            return self.points
        return []

    def GetEntityCardValues(self, _deck, entity, _fields):
        return {"__id__": entity._id, "__type__": entity.kind}


class State:
    def assert_main(self):
        return None


class PlateHoleWorkflowTests(unittest.TestCase):
    def test_native_part_summaries_do_not_hide_definition_changes(self):
        part = {"fields": {"Name": "plate", "Mass": 1.0, "PID": "2"}}
        cards = {"PSHELL": [{"entity": {"id": 2}, "card_fields": ["T", "Num.Elem"],
                              "fields": {"T": 1.0, "Num.Elem": 0}}]}
        current_part = {"fields": {"Name": "plate", "Mass": 2.0, "PID": "2, 3"}}
        current_cards = deepcopy(cards)
        current_cards["PSHELL"][0]["fields"]["Num.Elem"] = 3920
        self.assertTrue(workflow._compare_definitions(part, cards, current_part, current_cards)[
            "original_material_property_definitions_unchanged"])
        current_part["fields"]["Name"] = "other"
        with self.assertRaisesRegex(ValueError, "ANSAPART"):
            workflow._compare_definitions(part, cards, current_part, current_cards)
        current_part["fields"]["Name"] = "plate"
        current_cards["PSHELL"][0]["fields"]["T"] = 2.0
        with self.assertRaisesRegex(ValueError, "material/property"):
            workflow._compare_definitions(part, cards, current_part, current_cards)

    def test_closed_plate_topology_counts_holed_caps_as_annuli(self):
        edges = [(1, 2), (2, 3), (3, 4), (4, 1),
                 (5, 6), (6, 7), (7, 8), (8, 5),
                 (1, 5), (2, 6), (3, 7), (4, 8),
                 (9, 10), (9, 10), (11, 12), (11, 12), (9, 11), (10, 12)]
        faces = [[1, 2, 3, 4, 13, 14], [5, 6, 7, 8, 15, 16],
                 [1, 5, 9, 10], [2, 6, 10, 11], [3, 7, 11, 12],
                 [4, 8, 12, 9], [13, 15, 17, 18], [14, 16, 17, 18]]
        witness = {
            "face_boundaries": [{"face_id": i, "cons_ids": row}
                                for i, row in enumerate(faces, 1)],
            "boundaries": [{"id": i, "face_ids": [f for f, row in enumerate(faces, 1) if i in row],
                            "endpoint_topology": {"hot_point_ids": list(pair)}}
                           for i, pair in enumerate(edges, 1)],
            "vertices": [{"id": i, "position": [0.0, 0.0, 0.0]} for i in range(1, 13)],
        }
        result = workflow._source_topology(None, None, witness)
        self.assertEqual(result["euler_characteristic"], 0)
        self.assertEqual(result["face_loop_counts"]["1"], 2)
        self.assertEqual(result["face_loop_counts"]["2"], 2)
        positions = [(-30, -20, 3), (30, -20, 3), (30, 20, 3), (-30, 20, 3),
                     (-30, -20, -3), (30, -20, -3), (30, 20, -3), (-30, 20, -3),
                     (6, 0, 3), (-6, 0, 3), (6, 0, -3), (-6, 0, -3)]
        witness["vertices"] = [{"id": i, "position": list(p)}
                               for i, p in enumerate(positions, 1)]
        bounds = {}
        for i, cons_ids in enumerate(faces, 1):
            points = [positions[v - 1] for c in cons_ids for v in edges[c - 1]]
            bounds[i] = ([min(p[j] for p in points) for j in range(3)]
                         + [max(p[j] for p in points) for j in range(3)])
        base = Mock()
        base.BoundBox.side_effect = lambda face: bounds[face._id]
        base.GetFaceArea.return_value = 1.0
        surfaces = {"surfaces": [{"id": i, "radius_s": {"infinite": True},
                                  "radius_t": {"infinite": False, "value": 6.0},
                                  "axis_candidate": [0.0, 0.0, 1.0]} for i in (7, 8)]}
        geometry = workflow._validate_source_geometry(
            base, 1, workflow.plan(params()), witness, surfaces,
            {"FACE": [Entity(i, "FACE") for i in range(1, 9)], "CURVE": []},
        )
        self.assertEqual(geometry["hole_face_ids"], [7, 8])
        from ansa_mcp_bridge.journal import Journal
        Journal._canonical(geometry, "preflight geometry")

    def test_plan_is_pure_recipe_and_rejects_invalid_parameters(self):
        recipe = workflow.plan(params())
        self.assertEqual(recipe["predicted_solids"], 4672)
        self.assertEqual(recipe["predicted_nodes"], 6300)
        self.assertFalse(recipe["usable_for_mutation"])
        with self.assertRaises(ValueError):
            workflow.plan({**params(), "length": True})
        with self.assertRaises(ValueError):
            workflow.plan({**params(), "unexpected": 1})
        with self.assertRaises(ValueError):
            workflow.plan({**params(), "center": [0.0, math.inf, 0.0]})

    def test_real_cons_endpoint_roundoff_uses_shared_nominal_seed_helper(self):
        groups, rows = workflow._classify_cons_edges(SeedBase(), 1, params())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cons_id"], 199)
        self.assertEqual(rows[0]["kind"], "outer_straight")
        self.assertEqual(rows[0]["segments"], 6)
        self.assertEqual([len(entities) for entities in groups.values()], [1])

    def test_model_bound_plan_mismatch_stops_before_backup_or_artifacts(self):
        with (
            patch.object(workflow, "_prepare_preflight", return_value={"plan_sha256": "b" * 64}),
            patch.object(workflow.native, "_backup_preflight") as backup,
            patch.object(workflow.native, "_verify_backup") as verify_backup,
            patch.object(workflow, "_new_artifact_directory") as artifact_dir,
        ):
            with self.assertRaisesRegex(ValueError, "model-bound"):
                workflow.execute(
                    State(), object(), object(), object(), params(),
                    backup_path="unused-backup.ansa", output_path="unused-output.ansa",
                    expected_plan_sha256="a" * 64,
                )
        backup.assert_not_called()
        verify_backup.assert_not_called()
        artifact_dir.assert_not_called()

    def _execute_after_preflight(self, preflight_results, verify_backup_effect):
        prepared = {
            "plan_sha256": "a" * 64,
            "recipe": workflow.plan(params()),
            "source_face_sha256": "b" * 64,
            "input_snapshot": {},
            "clean": params(),
            "context": {},
            "settings": SimpleNamespace(snapshot_sha256="c" * 64),
            "scope": {},
            "definitions": {},
            "part_row": {},
        }
        preflight = Mock(side_effect=[
            prepared if result is None else result for result in preflight_results
        ])
        verify_backup = Mock(side_effect=verify_backup_effect)
        execute_mutation = Mock()
        run_with_context = Mock()
        ansa_module = ModuleType("ansa")
        ansa_module.calc = object()
        state = State()
        base = SimpleNamespace(CurrentDataBaseName="source.ansa")
        constants = SimpleNamespace(NASTRAN=1)
        with (
            patch.dict("sys.modules", {"ansa": ansa_module}),
            patch.object(workflow, "_prepare_preflight", preflight),
            patch.object(workflow, "_validate_output_paths",
                         return_value=(Path("backup.ansa"), Path("output.ansa"))),
            patch.object(workflow.native, "_backup_preflight", return_value=()),
            patch.object(workflow.native, "_verify_backup", verify_backup),
            patch.object(workflow, "_new_artifact_directory", return_value=Path("artifacts")),
            patch.object(workflow, "_write_json",
                         side_effect=lambda path, _payload: {"path": str(path)}),
            patch.object(workflow, "_write_stage"),
            patch.object(workflow, "_execute_mutation", execute_mutation),
            patch.object(workflow.meshing_context, "run_with_context", run_with_context),
        ):
            result = workflow.execute(
                state, base, Mock(), constants, params(),
                backup_path="backup.ansa", output_path="output.ansa",
                expected_plan_sha256=prepared["plan_sha256"],
            )
        return result, preflight, verify_backup, execute_mutation, run_with_context, base

    def test_backup_call_failure_requires_recovery_and_stops_native_work(self):
        def fail_after_save_as(base, *_args):
            base.CurrentDataBaseName = "backup.ansa"
            raise RuntimeError("SaveAs changed the current database path")

        result, preflight, verify_backup, execute_mutation, run_with_context, base = (
            self._execute_after_preflight([None], fail_after_save_as)
        )
        self.assertEqual(base.CurrentDataBaseName, "backup.ansa")
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(result["error"]["type"], "RuntimeError")
        preflight.assert_called_once()
        verify_backup.assert_called_once()
        execute_mutation.assert_not_called()
        run_with_context.assert_not_called()

    def test_post_backup_preflight_failure_requires_recovery_and_stops_native_work(self):
        result, preflight, verify_backup, execute_mutation, run_with_context, _base = (
            self._execute_after_preflight(
                [None, ValueError("model changed after backup")],
                {"path": "backup.ansa"},
            )
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(result["error"]["message"], "model changed after backup")
        self.assertEqual(preflight.call_count, 2)
        verify_backup.assert_called_once()
        execute_mutation.assert_not_called()
        run_with_context.assert_not_called()

    def test_native_unknown_error_is_not_replayed(self):
        native_api = Mock(side_effect=RuntimeError("unknown native outcome"))
        with patch.object(workflow.volume_meshing, "_same_context", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "unknown native outcome"):
                workflow._native_call_once(State(), 1, {}, native_api, [1])
        native_api.assert_called_once_with([1])

    def test_complete_capture_marker_requires_full_ids_and_membership(self):
        elements = [
            {"id": 10, "type": "CHEXA", "nodes": [1, 2, 3, 4, 5, 6, 7, 8], "block_id": "VOLUME:1"},
            {"id": 11, "type": "CHEXA", "nodes": [1, 2, 3, 4, 5, 6, 7, 8], "block_id": "VOLUME:2"},
        ]
        nodes = [{"id": value, "xyz": [float(value), 0.0, 0.0]} for value in range(1, 9)]
        complete = workflow._complete_capture(1, elements, nodes, {10: [1], 11: [2]}, 2, 8)
        self.assertIs(complete["integrity"]["complete"], True)
        self.assertIs(complete["ownership"]["complete"], True)
        with self.assertRaisesRegex(ValueError, "membership"):
            workflow._complete_capture(1, elements, nodes, {10: [1]}, 2, 8)

    def test_source_face_hash_tracks_native_face_evidence(self):
        witness = {"cad_export_sha256": "a" * 64, "source_snapshot_sha256": "b" * 64}
        surfaces = [{"id": 1, "surface_index": 2}]
        bounds = {1: [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]}
        scope_rows = [{"entity": {"type": "FACE", "id": 1}}]
        original = workflow._source_face_sha256(witness, surfaces, bounds, scope_rows)
        self.assertEqual(original, workflow._source_face_sha256(witness, surfaces, bounds, scope_rows))
        changed = workflow._source_face_sha256(
            {**witness, "cad_export_sha256": "c" * 64}, surfaces, bounds, scope_rows
        )
        self.assertNotEqual(original, changed)


if __name__ == "__main__":
    unittest.main()
