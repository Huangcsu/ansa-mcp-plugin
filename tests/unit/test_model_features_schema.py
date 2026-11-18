"""Schema and submission contracts for native Feature recognition."""

import unittest

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.mcp.model_features import register
from ansa_mcp_plugin.mcp.schemas import ToolResult
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, TASK_TOOLS, writes_model


class ModelFeaturesSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_registration_and_read_only_classification(self):
        tools = await create_server(object()).list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(len(names), 94)
        self.assertIn("ansa_model_features_recognize", names)
        self.assertIn("model_features_recognize_plan", READ_ONLY_TOOLS)
        self.assertIn("model_features_recognize", TASK_TOOLS)
        self.assertFalse(writes_model("model_features_recognize_plan"))
        self.assertTrue(writes_model("model_features_recognize"))

    async def test_recognize_plan_and_apply_submission(self):
        submitted = []

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, expected_target, expected_model, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("model-features-recognize-schema")
        register(server, submit)
        recognize_schema = server._tool_manager.get_tool("ansa_model_features_recognize").parameters
        self.assertIs(recognize_schema["additionalProperties"], False)
        self.assertEqual(recognize_schema["properties"]["action"]["default"], "plan")
        self.assertEqual(recognize_schema["properties"]["input_kind"]["default"], "cad")
        self.assertEqual(recognize_schema["properties"]["ttl_seconds"]["default"], 300)
        parameters_schema = recognize_schema["$defs"]["RecognitionParameters"]
        self.assertIs(parameters_schema["additionalProperties"], False)
        from ansa_mcp_shared.feature_recognition import RECOGNITION_OPTIONS

        self.assertEqual(
            set(parameters_schema["properties"]),
            {name for options in RECOGNITION_OPTIONS.values() for name in options},
        )

        common = {
            "operation_id": "recognize-plan",
            "expected_target": {
                "backend_id": "backend",
                "instance_id": "instance",
                "session_id": "session",
                "generation": 4,
            },
            "expected_model": {"epoch": 9},
            "deck": 1,
            "part_ids": [7, 3],
        }
        plan_args = {
            **common,
            "feature_types": ["FILLET", "HOLE 2D"],
            "parameters": {
                "fillet_max_radius": 2.5,
                "fillet_min_angle": 20.0,
                "hole_max_diameter": 8.0,
            },
        }
        apply_args = {
            **common,
            "operation_id": "recognize-apply",
            "feature_types": ["CHAMFER"],
            "parameters": {
                "chamfer_max_width": 1.5,
                "chamfer_min_angle": 30.0,
                "chamfer_max_angle": 150.0,
            },
            "action": "apply",
            "expected_plan_sha256": "a" * 64,
            "backup_path": r"C:\fixtures\recognize-backup.ansa",
            "ttl_seconds": 45,
        }

        await server.call_tool("ansa_model_features_recognize", plan_args)
        await server.call_tool("ansa_model_features_recognize", apply_args)

        plan_call, apply_call = submitted
        self.assertEqual(plan_call[:3], (
            "model_features_recognize_plan",
            {
                "deck": 1,
                "part_ids": [7, 3],
                "feature_types": ["FILLET", "HOLE 2D"],
                "parameters": {
                    "fillet_max_radius": 2.5,
                    "fillet_min_angle": 20.0,
                    "hole_max_diameter": 8.0,
                },
            },
            "recognize-plan",
        ))
        self.assertEqual(apply_call[:3], (
            "model_features_recognize",
            {
                "deck": 1,
                "part_ids": [7, 3],
                "feature_types": ["CHAMFER"],
                "parameters": {
                    "chamfer_max_width": 1.5,
                    "chamfer_min_angle": 30.0,
                    "chamfer_max_angle": 150.0,
                },
                "expected_plan_sha256": "a" * 64,
                "backup_path": r"C:\fixtures\recognize-backup.ansa",
            },
            "recognize-apply",
        ))
        self.assertEqual(plan_call[-1], 300)
        self.assertEqual(apply_call[-1], 45)
        await server.call_tool("ansa_model_features_recognize", {
            **common, "operation_id": "recognize-3d-hole", "modelling": "Solid",
            "feature_types": ["HOLE 3D"], "parameters": {},
        })
        self.assertEqual(submitted[-1][:3], (
            "model_features_recognize_plan",
            {"deck": 1, "part_ids": [7, 3], "feature_types": ["HOLE 3D"],
             "modelling": "Solid", "parameters": {}},
            "recognize-3d-hole",
        ))
        await server.call_tool("ansa_model_features_recognize", {
            **common, "operation_id": "recognize-shell-rib", "modelling": "Shell",
            "feature_types": ["RIB 2D"], "parameters": {}, "input_kind": "shell_mesh",
        })
        self.assertEqual(submitted[-1][:3], (
            "model_features_recognize_plan",
            {"deck": 1, "part_ids": [7, 3], "feature_types": ["RIB 2D"],
             "modelling": "Shell", "input_kind": "shell_mesh", "parameters": {}},
            "recognize-shell-rib",
        ))

    async def test_recognize_rejects_risky_parameter_combinations(self):
        submitted = []

        def submit(*args):
            submitted.append(args)
            raise AssertionError("rejected input must not reach submit")

        server = FastMCP("model-features-recognize-rejected")
        register(server, submit)
        base = {
            "operation_id": "recognize-rejected",
            "expected_target": {
                "backend_id": "backend",
                "instance_id": "instance",
                "session_id": "session",
                "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
            "part_ids": [1],
            "feature_types": ["FILLET"],
            "parameters": {
                "fillet_max_radius": 1.0,
                "fillet_min_angle": 10.0,
            },
        }

        schema_invalid = (
            {"feature_types": ["FILLET", "FILLET"]},
            {"feature_types": ["UNKNOWN"]},
            {"parameters": {
                "fillet_max_radius": 1.0,
                "fillet_min_angle": 10.0,
                "unknown": 1.0,
            }},
            {"parameters": {
                "fillet_max_radius": True,
                "fillet_min_angle": 10.0,
            }},
            {"parameters": {
                "fillet_max_radius": "1.0",
                "fillet_min_angle": 10.0,
            }},
            {"parameters": {
                "fillet_max_radius": float("inf"),
                "fillet_min_angle": 10.0,
            }},
            {"parameters": {
                "fillet_max_radius": 1.0,
                "fillet_min_angle": 180.0,
            }},
        )
        for changed in schema_invalid:
            with self.subTest(changed=changed), self.assertRaises(ToolError):
                await server.call_tool("ansa_model_features_recognize", {**base, **changed})

        rejected = (
            {**base, "parameters": {"fillet_max_radius": 1.0}},
            {**base, "parameters": {
                "fillet_max_radius": 1.0,
                "fillet_min_angle": 10.0,
                "chamfer_max_width": 1.0,
            }},
            {**base, "backup_path": r"C:\fixtures\should-not-write.ansa"},
            {**base, "action": "apply"},
            {**base, "modelling": "Shell", "input_kind": "shell_mesh"},
        )
        for arguments in rejected:
            response = await server.call_tool("ansa_model_features_recognize", arguments)
            self.assertTrue(response.isError, response)
            self.assertEqual(
                response.structuredContent["error"]["code"],
                "MODEL_FEATURES_RECOGNIZE_REJECTED",
            )
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
