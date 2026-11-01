"""Risk-focused contracts for the extended CONS seed edit entrypoint."""

import asyncio
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

import ansa_mcp_bridge.operations as bridge_operations
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, TASK_TOOLS, writes_model


class PerimeterEditSchemaTests(unittest.TestCase):
    def setUp(self):
        self.submitted = []

        def submit_task(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            self.submitted.append(
                (tool, params, operation_id, expected_target, expected_model, ttl_seconds)
            )
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

        self.server = create_server(SimpleNamespace(submit_task=submit_task))
        self.tool = self.server._tool_manager.get_tool("ansa_mesh_seed_apply")
        self.model = self.tool.fn_metadata.arg_model
        self.common = {
            "operation_id": "seed-edit",
            "expected_target": {
                "backend_id": "backend",
                "instance_id": "instance",
                "session_id": "session",
                "generation": 4,
            },
            "expected_model": {"epoch": 7},
            "deck": 1,
            "face_ids": [5, 6],
            "expected_seed_snapshot_sha256": "a" * 64,
            "backup_path": str(Path.cwd() / "seed-edit-backup.ansa"),
        }

    def _invoke(self, values):
        validated = self.model.model_validate(values)
        kwargs = {
            name: getattr(validated, name)
            for name in type(validated).model_fields
        }
        self.tool.fn(**kwargs)
        return validated

    def test_seed_edit_is_discriminated_strict_union_with_independent_schemas(self):
        tools = asyncio.run(self.server.list_tools())
        self.assertEqual(len(tools), 93)

        schema = self.model.model_json_schema()
        edit_schema = schema["properties"]["edit"]["anyOf"][0]
        self.assertEqual(edit_schema["discriminator"]["propertyName"], "action")
        self.assertEqual(
            edit_schema["discriminator"]["mapping"],
            {
                "number": "#/$defs/NumberSeedEdit",
                "length": "#/$defs/LengthSeedEdit",
                "align": "#/$defs/AlignSeedEdit",
            },
        )

        expected_properties = {
            "NumberSeedEdit": {"action", "expected_scope_sha256", "count_type", "assignments"},
            "LengthSeedEdit": {"action", "expected_scope_sha256", "cons_ids", "target_length"},
            "AlignSeedEdit": {
                "action",
                "expected_scope_sha256",
                "source_cons_id",
                "source_start_hot_point_id",
                "targets",
            },
        }
        for name, properties in expected_properties.items():
            with self.subTest(name=name):
                definition = schema["$defs"][name]
                self.assertIs(definition["additionalProperties"], False)
                self.assertEqual(set(definition["properties"]), properties)
        for name in ("SeedCountAssignment", "SeedAlignTarget"):
            with self.subTest(name=name):
                self.assertIs(schema["$defs"][name]["additionalProperties"], False)
        for definition_name, property_name in (
            ("NumberSeedEdit", "assignments"),
            ("LengthSeedEdit", "cons_ids"),
            ("AlignSeedEdit", "targets"),
        ):
            with self.subTest(definition=definition_name, property=property_name):
                bounded = schema["$defs"][definition_name]["properties"][property_name]
                maximum = 31 if definition_name == "AlignSeedEdit" else 32
                self.assertEqual((bounded["minItems"], bounded["maxItems"]), (1, maximum))

        edits = (
            ({"action": "number", "expected_scope_sha256": "b" * 64, "count_type": "edges",
              "assignments": [{"cons_id": 2, "count": 1}]}, "NumberSeedEdit"),
            ({"action": "length", "expected_scope_sha256": "c" * 64,
              "cons_ids": [2], "target_length": 1.5}, "LengthSeedEdit"),
            ({"action": "align", "expected_scope_sha256": "d" * 64,
              "source_cons_id": 2, "source_start_hot_point_id": 11,
              "targets": [{"cons_id": 3, "start_hot_point_id": 12}]}, "AlignSeedEdit"),
        )
        for edit, expected_type in edits:
            with self.subTest(action=edit["action"]):
                validated = self.model.model_validate({**self.common, "edit": edit})
                self.assertEqual(type(validated.edit).__name__, expected_type)

    def test_legacy_assignments_forwarding_is_byte_for_byte_unchanged(self):
        values = {
            **self.common,
            "operation_id": "legacy-seed-apply",
            "assignments": [{"cons_id": 21, "segments": 8}],
            "ttl_seconds": 77,
        }
        validated = self._invoke(values)

        self.assertEqual(len(self.submitted), 1)
        tool, params, operation_id, target, model, ttl_seconds = self.submitted[0]
        self.assertEqual(tool, "mesh_seed_apply")
        self.assertEqual(params, {
            "deck": 1,
            "face_ids": [5, 6],
            "assignments": [{"cons_id": 21, "segments": 8}],
            "expected_seed_snapshot_sha256": "a" * 64,
            "backup_path": self.common["backup_path"],
        })
        self.assertEqual(operation_id, "legacy-seed-apply")
        self.assertEqual(target, validated.expected_target.model_dump())
        self.assertEqual(model, validated.expected_model.model_dump())
        self.assertEqual(ttl_seconds, 77)

    def test_edit_forwarding_uses_new_tool_and_exact_bindings(self):
        edit = {
            "action": "length",
            "expected_scope_sha256": "b" * 64,
            "cons_ids": [21, 22],
            "target_length": 2.5,
        }
        validated = self._invoke({**self.common, "edit": edit, "ttl_seconds": 88})

        self.assertEqual(len(self.submitted), 1)
        tool, params, operation_id, target, model, ttl_seconds = self.submitted[0]
        self.assertEqual(tool, "mesh_seed_edit")
        self.assertEqual(params, {
            "deck": 1,
            "face_ids": [5, 6],
            "edit": edit,
            "expected_seed_snapshot_sha256": "a" * 64,
            "backup_path": self.common["backup_path"],
        })
        self.assertEqual(operation_id, "seed-edit")
        self.assertEqual(target, validated.expected_target.model_dump())
        self.assertEqual(model, validated.expected_model.model_dump())
        self.assertEqual(ttl_seconds, 88)

    def test_assignments_and_edit_are_mutually_exclusive_and_required(self):
        number_edit = {
            "action": "number",
            "expected_scope_sha256": "b" * 64,
            "count_type": "edges",
            "assignments": [{"cons_id": 2, "count": 3}],
        }
        for values in (
            self.common,
            {
                **self.common,
                "assignments": [{"cons_id": 2, "segments": 3}],
                "edit": number_edit,
            },
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValueError, "exactly one of assignments or edit"
            ):
                self._invoke(values)

    def test_invalid_discriminators_extra_fields_and_strict_values_are_rejected(self):
        base_edit = {
            "action": "number",
            "expected_scope_sha256": "b" * 64,
            "count_type": "edges",
            "assignments": [{"cons_id": 2, "count": 3}],
        }
        changes = (
            {"edit": {**base_edit, "action": "unknown"}},
            {"edit": {**base_edit, "extra": True}},
            {"edit": {
                **base_edit,
                "assignments": [{"cons_id": 2, "count": 3, "extra": True}],
            }},
            {"edit": {**base_edit, "expected_scope_sha256": "B" * 64}},
            {"edit": {**base_edit, "count_type": "faces"}},
            {"edit": {**base_edit, "assignments": [{"cons_id": 2, "count": True}]}},
            {"edit": {**base_edit, "assignments": [{"cons_id": 2, "count": 0}]}},
            {"edit": {**base_edit, "assignments": [{"cons_id": 2, "count": 10001}]}},
            {"edit": {
                "action": "length", "expected_scope_sha256": "b" * 64,
                "cons_ids": [], "target_length": 1.0,
            }},
            {"edit": {
                "action": "length", "expected_scope_sha256": "b" * 64,
                "cons_ids": list(range(1, 34)), "target_length": 1.0,
            }},
            {"edit": {
                "action": "length", "expected_scope_sha256": "b" * 64,
                "cons_ids": [2], "target_length": 0.0,
            }},
            {"edit": {
                "action": "length", "expected_scope_sha256": "b" * 64,
                "cons_ids": [2], "target_length": True,
            }},
            {"edit": {
                "action": "length", "expected_scope_sha256": "b" * 64,
                "cons_ids": [True], "target_length": 1.0,
            }},
            {"unexpected": True},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.model.model_validate({**self.common, **change})

    def test_number_nodes_require_at_least_two_points(self):
        edit = {
            "action": "number",
            "expected_scope_sha256": "b" * 64,
            "count_type": "nodes",
            "assignments": [{"cons_id": 2, "count": 1}],
        }
        with self.assertRaisesRegex(ValidationError, "node counts must be at least 2"):
            self.model.model_validate({**self.common, "edit": edit})

        edit["assignments"][0]["count"] = 2
        validated = self.model.model_validate({**self.common, "edit": edit})
        self.assertEqual(validated.edit.assignments[0].count, 2)

    def test_all_cons_lists_reject_duplicate_ids(self):
        edits = (
            {
                "action": "number",
                "expected_scope_sha256": "b" * 64,
                "count_type": "edges",
                "assignments": [
                    {"cons_id": 2, "count": 3},
                    {"cons_id": 2, "count": 4},
                ],
            },
            {
                "action": "length",
                "expected_scope_sha256": "b" * 64,
                "cons_ids": [2, 2],
                "target_length": 1.0,
            },
            {
                "action": "align",
                "expected_scope_sha256": "b" * 64,
                "source_cons_id": 2,
                "source_start_hot_point_id": 11,
                "targets": [
                    {"cons_id": 3, "start_hot_point_id": 12},
                    {"cons_id": 3, "start_hot_point_id": 13},
                ],
            },
        )
        for edit in edits:
            with self.subTest(action=edit["action"]), self.assertRaises(ValidationError):
                self.model.model_validate({**self.common, "edit": edit})

    def test_align_source_cannot_be_targeted(self):
        edit = {
            "action": "align",
            "expected_scope_sha256": "b" * 64,
            "source_cons_id": 2,
            "source_start_hot_point_id": 11,
            "targets": [{"cons_id": 2, "start_hot_point_id": 12}],
        }
        with self.assertRaisesRegex(ValidationError, "source_cons_id must not appear in targets"):
            self.model.model_validate({**self.common, "edit": edit})

    def test_seed_edit_is_a_write_only_task_and_dispatches_to_perimeter_edit(self):
        self.assertIn("mesh_seed_edit", TASK_TOOLS)
        self.assertNotIn("mesh_seed_edit", READ_ONLY_TOOLS)
        self.assertTrue(writes_model("mesh_seed_edit"))

        result = {"routed": True}
        params = {"deck": 1, "face_ids": [5], "edit": {"action": "length"}}
        with (patch.dict(sys.modules, {"ansa": SimpleNamespace(base=object(), constants=object())}),
              patch("ansa_mcp_bridge.operations.perimeter_edit.execute", return_value=result) as dispatch):
            self.assertIs(
                bridge_operations.execute(SimpleNamespace(assert_main=Mock()), "mesh_seed_edit", params),
                result,
            )
        dispatch.assert_called_once()
        state, tool, forwarded = dispatch.call_args.args
        self.assertEqual(tool, "mesh_seed_edit")
        self.assertIs(forwarded, params)


if __name__ == "__main__":
    unittest.main()
