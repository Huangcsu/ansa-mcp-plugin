"""Focused tests for explicit, read-only MeshQuality failure statistics."""

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import failed_elements
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError
from test_mesh_checks import FakeBase, FakeCheck, FakeCheckAPI, FakeReport, FakeState


class FakeQualityCheck(FakeCheck):
    def __init__(self, base):
        super().__init__(base)
        object.__setattr__(self, "quality_criteria", "verified criteria")
        object.__setattr__(self, "use_criteria", "Use F11 values")

    def parameters(self):
        return {"quality_criteria": self.quality_criteria, "use_criteria": self.use_criteria}


class FakeFailedElementsBase(FakeBase):
    def __init__(self):
        super().__init__()

        def mesh_quality():
            self.factory_calls += 1
            check = FakeQualityCheck(self)
            for name in self.missing_options:
                delattr(check, name)
            self.checks_created.append(check)
            return check

        self.checks.mesh.MeshQuality = mesh_quality


class FailedElements(unittest.TestCase):
    def setUp(self):
        self.base = FakeFailedElementsBase()
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def params(self, entity_ids=(11,), **overrides):
        params = {"deck": 1, "entity_ids": list(entity_ids)}
        params.update(overrides)
        return params

    def run_operation(self, params=None):
        return failed_elements.execute(
            self.state, "mesh_failed_elements", params or self.params()
        )

    def test_counts_distinct_failed_leaves_and_executes_silently_without_fixing(self):
        shell_11 = self.base.add(11)
        shell_12 = self.base.add(12)
        self.base.reports = [
            FakeReport(
                status="error",
                entities=[shell_11],
                children=[
                    FakeReport(status="error", entities=[shell_11, shell_12]),
                    FakeReport(status="warning", entities=[shell_12]),
                    FakeReport(status="ok", entities=[]),
                ],
                report_type="header",
            )
        ]

        result = self.run_operation(self.params((12, 11)))

        self.assertEqual(result["check"], "failed_elements")
        self.assertEqual(result["api"], "ansa.base.checks.mesh.MeshQuality")
        self.assertEqual(result["failed_entity_ids"], [11, 12])
        self.assertEqual(result["failed_element_count"], 2)
        self.assertEqual(result["issue_counts"], {"ok": 1, "error": 1, "warning": 1})
        self.assertEqual(result["issue_count"], 3)
        self.assertEqual(result["criteria_source"], "current_session")
        self.assertIn("不表示全工程", result["criteria_note"])
        self.assertEqual(result["options"], {"use_criteria": "Use global criteria"})
        self.assertEqual(result["criteria_snapshot"]["quality_criteria"], "verified criteria")
        check = self.base.checks_created[0]
        self.assertEqual(check.calls, [{
            "exec_mode": FakeCheckAPI.EXEC_ON_SELECTED,
            "report": FakeCheckAPI.REPORT_NONE,
            "history": FakeCheckAPI.KEEP_OLD,
            "entities": [self.base.entities[("SHELL", 12)], shell_11],
        }])
        self.assertEqual(check.try_fix_calls, 0)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_batch_and_mcp_schema_reject_non_strict_or_extra_parameters(self):
        self.base.add(11)
        invalid_batches = (
            {"deck": True, "entity_ids": [11]},
            {"deck": 1, "entity_ids": []},
            {"deck": 1, "entity_ids": [True]},
            {"deck": 1, "entity_ids": [0]},
            {"deck": 1, "entity_ids": [11, 11]},
            {"deck": 1, "entity_ids": list(range(1, 202))},
            {"deck": 1, "entity_ids": [11], "quality_criteria": "extra"},
        )
        for params in invalid_batches:
            with self.subTest(params=params):
                with self.assertRaises((TypeError, ValueError)):
                    self.run_operation(params)
        self.assertEqual(self.base.factory_calls, 0)

        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.mcp.failed_elements import register
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

        server = FastMCP("failed-elements-schema")
        register(server, submit)
        args = {
            "operation_id": "failed-elements",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "entity_ids": [11],
        }
        invalid_args = (
            {"operation_id": ""}, {"deck": True}, {"entity_ids": []},
            {"entity_ids": [True]}, {"entity_ids": [0]},
            {"entity_ids": list(range(1, 202))}, {"ttl_seconds": True},
            {"ttl_seconds": -1}, {"ttl_seconds": 86401},
        )
        for changed in invalid_args:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    awaitable = server.call_tool(
                        "ansa_mesh_failed_elements", {**args, **changed}
                    )
                    asyncio.run(awaitable)
        self.assertEqual(submitted, [])

        tool = next(
            tool for tool in asyncio.run(server.list_tools())
            if tool.name == "ansa_mesh_failed_elements"
        )
        self.assertEqual(set(tool.inputSchema["properties"]), {
            "operation_id", "expected_target", "expected_model", "deck", "entity_ids", "ttl_seconds",
        })

        asyncio.run(server.call_tool("ansa_mesh_failed_elements", args))
        self.assertEqual(submitted[-1], (
            "mesh_failed_elements", {"deck": 1, "entity_ids": [11]}, "failed-elements", 300,
        ))

    def test_missing_unretained_options_and_unavailable_check_fail_closed(self):
        self.base.add(11)
        for name in ("use_criteria",):
            self.base.missing_options = (name,)
            with self.subTest(missing=name):
                with self.assertRaises(CapabilityUnavailableError):
                    self.run_operation()
                self.assertEqual(self.base.checks_created[-1].calls, [])

        self.base.missing_options = ()
        self.base.ignore_writes = ("use_criteria",)
        with self.assertRaisesRegex(CapabilityUnavailableError, "did not retain"):
            self.run_operation()
        self.assertEqual(self.base.checks_created[-1].calls, [])

        self.base.ignore_writes = ()
        self.base.available = False
        with self.assertRaisesRegex(CapabilityUnavailableError, "does not confirm support"):
            self.run_operation()
        self.assertEqual(self.base.checks_created[-1].calls, [])

        self.base.available = True
        original_factory = self.base.checks.mesh.MeshQuality
        for text in (None, ""):
            def invalid_factory(text=text):
                check = original_factory()
                check.quality_criteria = text
                return check
            self.base.checks.mesh.MeshQuality = invalid_factory
            with self.assertRaisesRegex(CapabilityUnavailableError, "non-empty current global criteria"):
                self.run_operation()
            self.assertEqual(self.base.checks_created[-1].calls, [])
        self.base.checks.mesh.MeshQuality = original_factory

        # A read-only result must not be accepted if the native checker changes
        # either its own criteria or the independently observed global criteria.
        for changed_global in (False, True):
            created = []
            def mutating_factory():
                check = original_factory()
                created.append(check)
                if changed_global:
                    if len(created) > 1:
                        check.quality_criteria = "changed global criteria"
                else:
                    original_execute = check.execute
                    def mutating_execute(**kwargs):
                        reports = original_execute(**kwargs)
                        check.quality_criteria = "changed check criteria"
                        return reports
                    check.execute = mutating_execute
                return check
            self.base.checks.mesh.MeshQuality = mutating_factory
            with self.subTest(changed_global=changed_global):
                with self.assertRaisesRegex(RuntimeError, "changed its check or global quality criteria"):
                    self.run_operation()
        self.base.checks.mesh.MeshQuality = original_factory

    def test_out_of_scope_and_unknown_reports_are_rejected(self):
        shell = self.base.add(11)
        outside = self.base.add(99, register_context=False)
        invalid_reports = (
            ([FakeReport(status="error", entities=[outside])], "outside the verified context"),
            ([FakeReport(status="unknown", entities=[shell])], "unknown status"),
            ([FakeReport(status="error", entities=[])], "affected entities are unknown"),
        )
        for reports, message in invalid_reports:
            with self.subTest(message=message):
                self.base.reports = reports
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_operation()


if __name__ == "__main__":
    unittest.main()
