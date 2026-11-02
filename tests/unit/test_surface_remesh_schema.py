"""Schema, routing and release contracts for existing-shell surface remeshing."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

import ansa_mcp_bridge.operations as bridge_operations
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.mcp.meshing import SurfaceRemeshOptions, register
from ansa_mcp_plugin.mcp.schemas import ToolResult
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, TASK_TOOLS, writes_model


class SurfaceRemeshSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.submitted = []

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            self.submitted.append(
                (tool, params, operation_id, expected_target, expected_model, ttl)
            )
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        self.server = FastMCP("surface-remesh-schema")
        register(self.server, submit)

    def args(self):
        return {
            "operation_id": "surface-remesh",
            "expected_target": {
                "backend_id": "backend",
                "instance_id": "instance",
                "session_id": "session",
                "generation": 7,
            },
            "expected_model": {"epoch": 11},
            "faces": [{
                "id": 11,
                "expected_fields": {"PID": 7, "Frozen": "NO"},
                "expected_macro_id": 101,
                "expected_shell_ids": [201, 202],
            }],
        }

    def remesh(self):
        return {
            "algorithm": "FREE",
            "element_type": "quad",
            "element_order": "1st Order",
            "boundary_policy": "preserve",
            "target_length": 2.5,
            "expected_scope_sha256": "a" * 64,
            "expected_seed_snapshot_sha256": "b" * 64,
            "backup_path": r"F:\backups\surface-remesh.ansa",
        }

    def seed_scope(self):
        return {
            "face_ids": [11],
            "cons_ids": [31, 32, 33, 34],
            "expected_seed_snapshot_sha256": "b" * 64,
            "backup_path": r"F:\backups\mapped-surface.ansa",
        }

    def test_remesh_options_are_strict_complete_and_require_mapped_quad(self):
        remesh = self.remesh()
        validated = SurfaceRemeshOptions.model_validate(remesh)
        self.assertEqual(validated.model_dump(), remesh)
        self.assertIs(SurfaceRemeshOptions.model_config["extra"], "forbid")
        self.assertIs(SurfaceRemeshOptions.model_config["strict"], True)

        schema = SurfaceRemeshOptions.model_json_schema()
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(remesh))
        self.assertEqual(set(schema["properties"]), set(remesh))

        invalid = (
            {**remesh, "algorithm": "free"},
            {**remesh, "element_type": "beam"},
            {**remesh, "element_order": "2nd Order"},
            {**remesh, "boundary_policy": "relax"},
            {**remesh, "target_length": True},
            {**remesh, "target_length": "2.5"},
            {**remesh, "target_length": 0.0},
            {**remesh, "target_length": float("inf")},
            {**remesh, "expected_scope_sha256": "A" * 64},
            {**remesh, "expected_scope_sha256": "a" * 63},
            {**remesh, "expected_seed_snapshot_sha256": "B" * 64},
            {**remesh, "expected_seed_snapshot_sha256": "b" * 63},
            {**remesh, "backup_path": ""},
            {**remesh, "backup_path": 7},
            {**remesh, "unexpected": True},
            {**remesh, "algorithm": "mapped", "element_type": "mixed"},
            {**remesh, "algorithm": "mapped", "element_type": "tria"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SurfaceRemeshOptions.model_validate(value)
        for field in remesh:
            value = dict(remesh)
            del value[field]
            with self.subTest(missing=field), self.assertRaises(ValidationError):
                SurfaceRemeshOptions.model_validate(value)

    async def test_new_mode_forwards_only_complete_faces_remesh_and_deck(self):
        remesh = self.remesh()
        args = {
            **self.args(),
            "method": "remesh_existing",
            "deck": 3,
            "remesh": remesh,
            "ttl_seconds": 41,
        }

        result = await self.server.call_tool("ansa_mesh_surface", args)

        self.assertFalse(result.isError)
        self.assertEqual(len(self.submitted), 1)
        tool, params, operation_id, target, model, ttl = self.submitted[0]
        self.assertEqual(tool, "mesh_surface_remesh")
        self.assertEqual(params, {
            "deck": 3,
            "faces": args["faces"],
            "remesh": remesh,
        })
        self.assertEqual(operation_id, "surface-remesh")
        self.assertEqual(target.model_dump(), args["expected_target"])
        self.assertEqual(model.model_dump(), args["expected_model"])
        self.assertEqual(ttl, 41)

    async def test_legacy_modes_keep_exact_payloads_and_reject_remesh(self):
        base = self.args()
        length = {**base, "deck": 1, "target_length": 2.5}
        mapped = {
            **base,
            "method": "mapped_existing_seeds",
            "element_type": "quad",
            "seed_scope": self.seed_scope(),
        }

        await self.server.call_tool("ansa_mesh_surface", length)
        await self.server.call_tool("ansa_mesh_surface", mapped)
        self.assertEqual(self.submitted[0][:2], (
            "mesh_surface",
            {
                "faces": base["faces"],
                "target_length": 2.5,
                "element_type": "tria",
                "deck": 1,
            },
        ))
        self.assertEqual(self.submitted[1][:2], (
            "mesh_surface",
            {
                "faces": base["faces"],
                "element_type": "quad",
                "method": "mapped_existing_seeds",
                "seed_scope": mapped["seed_scope"],
            },
        ))

        for legacy in (length, mapped):
            with self.subTest(method=legacy.get("method", "length")):
                result = await self.server.call_tool(
                    "ansa_mesh_surface", {**legacy, "remesh": self.remesh()}
                )
                self.assertTrue(result.isError)
                self.assertEqual(
                    result.structuredContent["error"]["code"],
                    "MESH_SURFACE_REJECTED",
                )
        self.assertEqual(len(self.submitted), 2)

    async def test_remesh_mode_rejects_missing_required_and_mixed_outer_fields(self):
        base = {
            **self.args(),
            "method": "remesh_existing",
            "deck": 1,
            "remesh": self.remesh(),
        }
        invalid = (
            {**base, "remesh": None},
            {**base, "deck": None},
            {**base, "deck": 0},
            {**base, "target_length": 2.5},
            {**base, "seed_scope": self.seed_scope()},
            {**base, "element_type": "quad"},
        )

        for changed in invalid:
            with self.subTest(changed=changed):
                result = await self.server.call_tool("ansa_mesh_surface", changed)
                self.assertTrue(result.isError)
                self.assertEqual(
                    result.structuredContent["error"]["code"],
                    "MESH_SURFACE_REJECTED",
                )

        with self.assertRaises(ToolError):
            await self.server.call_tool("ansa_mesh_surface", {**base, "deck": True})
        with self.assertRaises(ToolError):
            await self.server.call_tool("ansa_mesh_surface", {
                **base,
                "remesh": {
                    **self.remesh(),
                    "algorithm": "mapped",
                    "element_type": "tria",
                },
            })
        self.assertEqual(self.submitted, [])

    async def test_registration_write_classification_and_actual_bridge_module_dispatch(self):
        tools = await create_server(object()).list_tools()
        self.assertIn("ansa_mesh_surface", {tool.name for tool in tools})
        self.assertNotIn("ansa_mesh_surface_remesh", {tool.name for tool in tools})

        self.assertIn("mesh_surface_remesh", TASK_TOOLS)
        self.assertNotIn("mesh_surface_remesh", READ_ONLY_TOOLS)
        self.assertTrue(writes_model("mesh_surface_remesh"))

        project_root = Path(__file__).resolve().parents[2]
        script_path = project_root / "scripts" / "stage_native_release.py"
        spec = importlib.util.spec_from_file_location("stage_native_release_for_test", script_path)
        stage_native_release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage_native_release)
        self.assertIn(
            "ansa_mcp_bridge/operations/surface_remesh.py",
            stage_native_release.SOURCE_FILES,
        )

        with patch.dict(
            sys.modules,
            {"ansa": types.SimpleNamespace(base=object(), constants=object())},
        ):
            from ansa_mcp_bridge.operations import surface_remesh

        result = {"routed": True}
        params = {"deck": 1, "faces": [], "remesh": {}}
        state = types.SimpleNamespace(assert_main=Mock())
        with patch.object(surface_remesh, "execute", return_value=result) as dispatch:
            self.assertIs(
                bridge_operations.execute(state, "mesh_surface_remesh", params),
                result,
            )
        dispatch.assert_called_once_with(state, "mesh_surface_remesh", params)


if __name__ == "__main__":
    unittest.main()
