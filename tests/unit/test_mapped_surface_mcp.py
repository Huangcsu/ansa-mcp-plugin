import copy
import unittest

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ansa_mcp_plugin.mcp.meshing import register
from ansa_mcp_plugin.mcp.schemas import ToolResult


class MappedSurfaceMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.submitted = []

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            self.submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        self.server = FastMCP("mapped-surface-schema")
        register(self.server, submit)

    def args(self):
        return {
            "operation_id": "mapped-surface",
            "expected_target": {
                "backend_id": "b",
                "instance_id": "i",
                "session_id": "s",
                "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "faces": [{
                "id": 11,
                "expected_fields": {"PID": 7, "Frozen": "NO"},
                "expected_macro_id": 101,
                "expected_shell_ids": [201],
            }],
        }

    def seed_scope(self):
        return {
            "face_ids": [11],
            "cons_ids": [31, 32, 33, 34],
            "expected_seed_snapshot_sha256": "a" * 64,
            "backup_path": r"F:\backups\mapped-surface.ansa",
        }

    async def test_length_mode_keeps_legacy_payload(self):
        args = {
            **self.args(),
            "deck": 1,
            "target_length": 2.5,
        }

        result = await self.server.call_tool("ansa_mesh_surface", args)

        self.assertFalse(result.isError)
        self.assertEqual(self.submitted, [(
            "mesh_surface",
            {
                "faces": args["faces"],
                "target_length": 2.5,
                "element_type": "tria",
                "deck": 1,
            },
            "mapped-surface",
            300,
        )])

    async def test_mapped_mode_forwards_seed_scope_without_target_or_null_deck(self):
        args = {
            **self.args(),
            "method": "mapped_existing_seeds",
            "element_type": "quad",
            "seed_scope": self.seed_scope(),
        }

        result = await self.server.call_tool("ansa_mesh_surface", args)

        self.assertFalse(result.isError)
        self.assertEqual(self.submitted, [(
            "mesh_surface",
            {
                "faces": args["faces"],
                "element_type": "quad",
                "method": "mapped_existing_seeds",
                "seed_scope": args["seed_scope"],
            },
            "mapped-surface",
            300,
        )])

    async def test_illegal_mode_combinations_return_structured_rejection(self):
        base = self.args()
        seed_scope = self.seed_scope()
        invalid = (
            {"method": "length"},
            {
                "method": "length",
                "target_length": 2.0,
                "seed_scope": seed_scope,
            },
            {
                "method": "mapped_existing_seeds",
                "element_type": "tria",
                "seed_scope": seed_scope,
            },
            {
                "method": "mapped_existing_seeds",
                "element_type": "quad",
            },
            {
                "method": "mapped_existing_seeds",
                "element_type": "quad",
                "target_length": 2.0,
                "seed_scope": seed_scope,
            },
        )

        for changed in invalid:
            with self.subTest(changed=changed):
                result = await self.server.call_tool(
                    "ansa_mesh_surface", {**base, **changed}
                )
                self.assertTrue(result.isError)
                self.assertEqual(
                    result.structuredContent["error"]["code"],
                    "MESH_SURFACE_REJECTED",
                )
        self.assertEqual(self.submitted, [])

    async def test_seed_scope_schema_rejects_bad_ids_hash_and_backup_path(self):
        base = self.args()
        seed_scope = self.seed_scope()
        invalid_scopes = (
            {**seed_scope, "face_ids": []},
            {**seed_scope, "face_ids": [11, 11]},
            {**seed_scope, "face_ids": [True]},
            {**seed_scope, "cons_ids": [31, 31]},
            {**seed_scope, "cons_ids": [0]},
            {**seed_scope, "expected_seed_snapshot_sha256": "A" * 64},
            {**seed_scope, "expected_seed_snapshot_sha256": "a" * 63},
            {**seed_scope, "backup_path": ""},
            {**seed_scope, "unexpected": True},
            {**seed_scope, "max_boundary_restore_mm": 0.001},
            {**seed_scope, "max_boundary_restore_mm": True},
            {**seed_scope, "allow_unmeshed_tessellation_updates": "yes"},
        )

        for invalid_scope in invalid_scopes:
            with self.subTest(seed_scope=invalid_scope):
                with self.assertRaises(ToolError):
                    await self.server.call_tool("ansa_mesh_surface", {
                        **copy.deepcopy(base),
                        "method": "mapped_existing_seeds",
                        "element_type": "quad",
                        "seed_scope": invalid_scope,
                    })
        self.assertEqual(self.submitted, [])


if __name__ == "__main__":
    unittest.main()
