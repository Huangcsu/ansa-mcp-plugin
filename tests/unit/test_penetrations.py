"""Focused checks for the scoped, read-only mesh penetration operation."""

import math
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import execute as dispatch_operation
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model
from test_mesh_checks import FakeBase, FakeState


class FakePenetrationBase(FakeBase):
    def __init__(self):
        super().__init__()
        self.native_result = {"intersected": [], "penetrated": [], "penetrations": 0}
        self.native_calls = []

    def CheckPenetration2Sets(self, entities1, entities2, mode, factor, fast_run):
        self.native_calls.append((
            list(entities1),
            list(entities2),
            {"mode": mode, "factor": factor, "fast_run": fast_run},
        ))
        return self.native_result


class MeshPenetrations(unittest.TestCase):
    def setUp(self):
        self.ansa = types.ModuleType("ansa")
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.install_base()
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def install_base(self):
        self.base = FakePenetrationBase()
        self.ansa.base = self.base

    def params(self, first_ids=(11,), second_ids=(12,), **overrides):
        params = {
            "deck": 1,
            "first_ids": list(first_ids),
            "second_ids": list(second_ids),
            "thickness_mode": "property",
            "factor": 1,
        }
        params.update(overrides)
        return params

    def prepare_scope(self, first_pid=101, second_pid=102,
                      first_thickness=1.0, second_thickness=2.0):
        properties = {}
        for property_id, thickness in (
            (first_pid, first_thickness), (second_pid, second_thickness)
        ):
            if property_id not in properties:
                prop = self.base.add(property_id, entity_type="PSHELL")
                prop.fields["T"] = thickness
                properties[property_id] = prop
        first = self.base.add(11)
        first.fields["PID"] = first_pid
        second = self.base.add(12)
        second.fields["PID"] = second_pid
        return first, second

    def run_operation(self, params=None):
        return dispatch_operation(
            self.state,
            "mesh_penetrations",
            self.params() if params is None else params,
        )

    def test_modes_return_both_categories_and_readonly_deduplicated_scope(self):
        first, second = self.prepare_scope()
        self.base.native_result = {
            "intersected": [second, first, second],
            "penetrated": [first, second, first],
            "penetrations": 7,
        }

        for thickness_mode, native_mode, factor in (
            ("property", 2, 1),
            ("user", 3, 0.5),
        ):
            with self.subTest(thickness_mode=thickness_mode):
                result = self.run_operation(self.params(
                    thickness_mode=thickness_mode, factor=factor
                ))
                self.assertEqual(result["check"], "penetrations")
                self.assertEqual(result["scope"], {"first_ids": [11], "second_ids": [12]})
                self.assertTrue(result["read_only"])
                self.assertFalse(result["auto_fix"])
                self.assertEqual(result["thickness_mode"], thickness_mode)
                self.assertEqual(result["native_options"], {
                    "mode": native_mode, "factor": float(factor), "fast_run": 0,
                })
                self.assertEqual(result["property_context"], {
                    "first": [{"shell_id": 11, "property_id": 101, "thickness": 1.0}],
                    "second": [{"shell_id": 12, "property_id": 102, "thickness": 2.0}],
                })
                self.assertEqual(result["native_count"], 7)
                self.assertEqual(result["native_intersected_list_count"], 3)
                self.assertEqual(result["native_penetrated_list_count"], 3)
                for category in ("intersected", "penetrated", "affected"):
                    self.assertEqual(result[category]["entity_ids"], [11, 12])
                    self.assertEqual(result[category]["count"], 2)
                    self.assertEqual(result[category]["first_ids"], [11])
                    self.assertEqual(result[category]["second_ids"], [12])
                    self.assertEqual(
                        [ref["id"] for ref in result[category]["references"]], [11, 12]
                    )
                self.assertEqual(result["result_limits"], {
                    "native_intersected_list_items": 400,
                    "native_penetrated_list_items": 400,
                })
                self.assertIn("不解释为相交对数", result["note"])

        self.assertEqual(
            [call[2] for call in self.base.native_calls],
            [
                {"mode": 2, "factor": 1.0, "fast_run": 0},
                {"mode": 3, "factor": 0.5, "fast_run": 0},
            ],
        )
        self.assertIn("mesh_penetrations", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("mesh_penetrations"))

    def test_shared_pshell_is_rejected_before_native_call(self):
        self.prepare_scope(first_pid=101, second_pid=101)

        with self.assertRaises(ValueError):
            self.run_operation()

        self.assertEqual(self.base.native_calls, [])

    def test_missing_or_invalid_property_metadata_fails_before_native_call(self):
        cases = (
            "missing_pid_field",
            "missing_pid_value",
            "missing_property",
            "wrong_property_type",
            "missing_thickness_field",
            "invalid_thickness",
            "missing_card_fields_api",
        )
        for case in cases:
            with self.subTest(case=case):
                self.install_base()
                first, _ = self.prepare_scope()
                if case == "missing_pid_field":
                    del first.fields["PID"]
                elif case == "missing_pid_value":
                    first.fields["PID"] = None
                elif case == "missing_property":
                    first.fields["PID"] = 999
                elif case == "wrong_property_type":
                    prop = self.base.add(101, entity_type="PSHELL", actual_type="MAT1")
                    prop.fields["T"] = 1.0
                elif case == "missing_thickness_field":
                    del self.base.entities[("PSHELL", 101)].fields["T"]
                elif case == "invalid_thickness":
                    self.base.entities[("PSHELL", 101)].fields["T"] = math.nan
                elif case == "missing_card_fields_api":
                    first.card_fields = None

                with self.assertRaises((RuntimeError, ValueError, LookupError)):
                    self.run_operation()
                self.assertEqual(self.base.native_calls, [])

        for thickness in (0, -1, math.nan, math.inf, True, "1", 10**1000):
            with self.subTest(thickness=repr(thickness)):
                self.install_base()
                self.prepare_scope(first_thickness=thickness)
                with self.assertRaises(RuntimeError):
                    self.run_operation()
                self.assertEqual(self.base.native_calls, [])

    def test_strict_inputs_fail_before_entity_reads(self):
        invalid = (
            {"deck": True},
            {"deck": 0},
            {"first_ids": []},
            {"first_ids": [True]},
            {"first_ids": [11, 11]},
            {"first_ids": list(range(1, 202))},
            {"second_ids": [11]},
            {"thickness_mode": "Property"},
            {"thickness_mode": ""},
            {"factor": True},
            {"factor": "1"},
            {"factor": 0},
            {"factor": -1},
            {"factor": math.nan},
            {"factor": math.inf},
            {"factor": 10**1000},
            {"fix": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    self.run_operation(self.params(**changed))
        for missing in ("thickness_mode", "factor"):
            with self.subTest(missing=missing):
                params = self.params()
                del params[missing]
                with self.assertRaises(ValueError):
                    self.run_operation(params)

        self.assertEqual(self.base.get_entity_calls, [])
        self.assertEqual(self.base.native_calls, [])

    def test_shell_subtype_and_grid_context_are_verified_before_native_call(self):
        for failure in ("unsupported_shell", "grid_scope_mismatch"):
            with self.subTest(failure=failure):
                self.install_base()
                first, _ = self.prepare_scope()
                if failure == "unsupported_shell":
                    first.fields["type"] = "SHELL"
                else:
                    self.base.collect_override = True
                    self.base.grids.pop()
                with self.assertRaises((RuntimeError, CapabilityUnavailableError)):
                    self.run_operation()
                self.assertEqual(self.base.native_calls, [])

    def test_missing_api_capabilities_fail_closed_before_entity_reads(self):
        for api in (
            "CurrentDeck",
            "GetEntity",
            "GetEntityCardValues",
            "CollectEntities",
            "CheckPenetration2Sets",
        ):
            with self.subTest(api=api):
                self.install_base()
                setattr(self.base, api, None)
                with self.assertRaises(CapabilityUnavailableError):
                    self.run_operation()
                self.assertEqual(self.base.get_entity_calls, [])
                self.assertEqual(self.base.native_calls, [])

    def test_malformed_native_results_fail_closed(self):
        first, second = self.prepare_scope()
        outside = self.base.add(13)
        valid_both = [first, second]
        invalid_results = (
            None,
            {"intersected": [], "penetrated": []},
            {"intersected": (), "penetrated": [], "penetrations": 1},
            {"intersected": [first] * 401, "penetrated": [], "penetrations": 1},
            {"intersected": [], "penetrated": [second] * 401, "penetrations": 1},
            {"intersected": [outside, second], "penetrated": [], "penetrations": 1},
            {"intersected": [first], "penetrated": valid_both, "penetrations": 1},
            {"intersected": valid_both, "penetrated": [second], "penetrations": 1},
            {"intersected": [object()], "penetrated": [], "penetrations": 1},
            {"intersected": valid_both, "penetrated": [], "penetrations": True},
            {"intersected": valid_both, "penetrated": [], "penetrations": -1},
            {"intersected": valid_both, "penetrated": [], "penetrations": 1 << 63},
            {"intersected": valid_both, "penetrated": [], "penetrations": 0},
            {"intersected": [], "penetrated": [], "penetrations": 1},
        )
        for native_result in invalid_results:
            with self.subTest(native_result=native_result):
                self.base.native_result = native_result
                with self.assertRaises(RuntimeError):
                    self.run_operation()


class MeshPenetrationsMcpSchema(unittest.IsolatedAsyncioTestCase):
    async def test_schema_is_strict_and_main_server_registers_the_tool(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from ansa_mcp_plugin.__main__ import create_server
        from ansa_mcp_plugin.mcp.penetrations import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        submitted = []
        target = {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1}

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("mesh-penetrations-schema")
        register(server, submit)
        tools = await server.list_tools()
        tool = next(item for item in tools if item.name == "ansa_mesh_penetrations")
        properties = tool.inputSchema["properties"]
        self.assertIs(tool.inputSchema.get("additionalProperties"), False)
        self.assertTrue({"first_ids", "second_ids", "thickness_mode", "factor"}.issubset(properties))
        self.assertNotIn("check", properties)
        self.assertNotIn("fix", properties)

        args = {
            "operation_id": "mesh-penetrations",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "first_ids": [11],
            "second_ids": [12],
            "thickness_mode": "property",
            "factor": 1,
        }
        invalid = (
            {"fix": True},
            {"check": "penetrations"},
            {"thickness_mode": "Property"},
            {"factor": True},
            {"factor": "1"},
            {"factor": 0},
            {"factor": math.nan},
            {"factor": math.inf},
            {"factor": 10**1000},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_mesh_penetrations", {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_mesh_penetrations", args)
        self.assertEqual(submitted, [(
            "mesh_penetrations",
            {
                "deck": 1,
                "first_ids": [11],
                "second_ids": [12],
                "thickness_mode": "property",
                "factor": 1.0,
            },
            "mesh-penetrations",
            300,
        )])

        main_server = create_server(object())
        registered = await main_server.list_tools()
        self.assertEqual(len(registered), 65)
        self.assertIn("ansa_mesh_penetrations", {item.name for item in registered})


if __name__ == "__main__":
    unittest.main()
