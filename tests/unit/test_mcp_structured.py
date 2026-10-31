from pathlib import Path
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from ansa_mcp_plugin.__main__ import create_server


class StructuredMeshingMcpTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            self.calls.append((tool, params))
            return {
                "task": {"target": expected_target, "model": expected_model},
                "state": "queued",
                "result": None,
                "dispatch": None,
                "cancel_pending": False,
                "acked": False,
                "operation_id": operation_id,
                "task_id": "task-" + operation_id,
            }

        self.tool = create_server(SimpleNamespace(submit_task=submit))._tool_manager.get_tool(
            "ansa_mesh_map_block_fill"
        )
        self.model = self.tool.fn_metadata.arg_model
        self.common = {
            "operation_id": "fill",
            "expected_target": {
                "backend_id": "b",
                "instance_id": "i",
                "session_id": "s",
                "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
        }

    def invoke(self, delta):
        request = self.model.model_validate({**self.common, **delta})
        fields = request.model_dump()
        fields.update(expected_target=request.expected_target, expected_model=request.expected_model)
        return self.tool.fn(**fields)

    def test_optional_annular_dict_schema_routes_and_omits_none_for_legacy_calls(self):
        schema = self.model.model_json_schema()
        annular_schema = schema["properties"]["annular_options"]
        self.assertTrue(any(item.get("type") == "object" for item in annular_schema["anyOf"]))

        self.invoke({"action": "plan", "volume_id": 7})
        self.assertEqual(
            self.calls[-1],
            ("mesh_map_block_fill_plan", {"deck": 1, "volume_id": 7}),
        )

        self.invoke({"action": "plan", "volume_ids": [1, 2]})
        self.assertEqual(
            self.calls[-1],
            ("mesh_map_block_fill_plan", {"deck": 1, "volume_ids": [1, 2]}),
        )

        options = {
            "source_face_ids": [201, 202, 203, 204],
            "center": [0.0, 0.0, 0.0],
            "axis": [0.0, 0.0, 1.0],
            "inner_radius": 10.0,
            "outer_radius": 20.0,
            "distance": 5.0,
            "max_chord_error": 0.02,
        }
        self.invoke({"action": "plan", "volume_ids": [1, 2, 3, 4], "annular_options": options})
        self.assertEqual(
            self.calls[-1],
            (
                "mesh_map_block_fill_plan",
                {"deck": 1, "volume_ids": [1, 2, 3, 4], "annular_options": options},
            ),
        )

    def test_annular_options_require_volume_ids_and_reject_non_dict_values(self):
        options = {"source_face_ids": [201, 202, 203, 204]}
        with self.assertRaisesRegex(ValueError, "annular_options requires volume_ids"):
            self.invoke({"action": "plan", "volume_id": 7, "annular_options": options})

        with self.assertRaises(ValidationError):
            self.model.model_validate({
                **self.common,
                "action": "plan",
                "volume_ids": [1, 2, 3, 4],
                "annular_options": [],
            })

    def test_apply_keeps_existing_plan_and_backup_fields_with_optional_route(self):
        backup_path = str(Path.cwd() / "new-fill.ansa")
        self.invoke({
            "action": "apply",
            "volume_id": 7,
            "expected_plan_sha256": "a" * 64,
            "backup_path": backup_path,
        })
        self.assertEqual(
            self.calls[-1],
            (
                "mesh_map_block_fill_apply",
                {
                    "deck": 1,
                    "volume_id": 7,
                    "expected_plan_sha256": "a" * 64,
                    "backup_path": backup_path,
                },
            ),
        )

        options = {"source_face_ids": [201, 202, 203, 204]}
        self.invoke({
            "action": "apply",
            "volume_ids": [1, 2, 3, 4],
            "annular_options": options,
            "expected_plan_sha256": "b" * 64,
            "backup_path": backup_path,
        })
        self.assertEqual(
            self.calls[-1],
            (
                "mesh_map_block_fill_apply",
                {
                    "deck": 1,
                    "volume_ids": [1, 2, 3, 4],
                    "annular_options": options,
                    "expected_plan_sha256": "b" * 64,
                    "backup_path": backup_path,
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
