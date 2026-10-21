"""Group decisions before truncation and isolate only a requested criterion."""

import copy
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import native_quality, quality_workflow as bridge
from test_quality_workflow import FakeBase, FakeEntity, FakeState
import test_quality_workflow as workflow_fixtures


class QualityDiagnosticsTests(unittest.TestCase):
    def test_summary_counts_overlap_once_and_keeps_groups_after_truncation(self):
        rows = [{"id": eid, "violations": ["ASPECT", "JACOB", "ASPECT"],
                 "metric_errors": {"JACOB": "x" * 512}} for eid in range(1, 2001)]
        report = {"rows": rows, "failed_entity_ids": list(range(1, 2001)),
                  "unknown_entity_ids": [1], "complete": False, "passed": False}
        result = bridge._bounded_result({"before": report, "after": copy.deepcopy(report)})
        for value in (result["before"], result["after"]):
            self.assertEqual(value["summary"], {"status": "incomplete", "checked_count": 2000,
                "failed_count": 2000, "unknown_count": 1, "passed_count": 0})
            self.assertEqual(value["violation_summary"], [
                {"criterion": "ASPECT", "failed_count": 2000},
                {"criterion": "JACOB", "failed_count": 2000}])
            self.assertTrue(value["rows_truncated"])
        self.assertLessEqual(len(bridge._encoded(result)), bridge._MAX_RESULT_BYTES)

    def locate(self, criterion, native=False, stale=False):
        base = FakeBase()
        entities = [FakeEntity(11), FakeEntity(12)]
        base.visible = [entities[0]]
        aspect, jacob = ("aspect ratio", "jacobian") if native else ("ASPECT", "JACOB")
        before = {"scope_sha256": "a" * 64, "geometry_sha256": "g" * 64,
                  "entity_type": "SHELL", "failed_entity_ids": [11, 12],
                  "unknown_entity_ids": [], "complete": True, "passed": False,
                  "criteria_snapshot": {"active_criteria": [{"name": aspect}, {"name": jacob}]},
                  "rows": [{"id": 11, "violations": [aspect]}, {"id": 12, "violations": [jacob]}]}
        profile = {"template": "linear_shell"} if native else {
            "analysis_type": "linear", "criteria": [{"name": "ASPECT", "max": 5.},
                {"name": "JACOB", "min": .5}, {"name": "WARP", "max": 10.}]}
        ansa = types.ModuleType("ansa")
        ansa.base, ansa.constants = base, types.SimpleNamespace(NASTRAN=1)
        ansa.guitk = types.SimpleNamespace(BCApplicationIsGuiMode=lambda: True)
        params = {"deck": 1, "entity_ids": [11, 12], "profile": profile,
                  "expected_scope_sha256": ("b" if stale else "a") * 64, "criterion": criterion}
        with patch.dict(sys.modules, {"ansa": ansa}), \
             patch.object(bridge, "_report", return_value=(before, entities, {}, {})), \
             patch.object(native_quality, "report", return_value=(before, entities, {}, {})), \
             patch.object(bridge, "_scope", return_value=(entities, {}, {}, "g" * 64)):
            try:
                return bridge.execute(FakeState(), "quality_locate", params), base
            finally:
                self.last_base = base

    def test_native_and_legacy_filter_isolate_only_matching_failed_elements(self):
        for criterion, native in (("ASPECT", False), ("aspect ratio", True)):
            with self.subTest(native=native):
                result, base = self.locate(criterion, native)
                self.assertEqual(result["located_entity_ids"], [11])
                self.assertEqual(result["criterion"], criterion)
                self.assertEqual([e._id for e in base.selected], [11])
                self.assertTrue(result["display_changed"])
                self.assertEqual(result["report"]["summary"]["failed_count"], 2)

    def test_invalid_or_stale_filter_never_changes_display(self):
        for criterion, native, stale in ((None, False, False), ("", False, False),
                (" ", False, False), ("JACOB", True, False), ("aspect ratio", False, False),
                ("unmapped_native_issue", True, False), ("ASPECT", False, True)):
            with self.subTest(criterion=criterion, native=native, stale=stale):
                with self.assertRaises(ValueError):
                    self.locate(criterion, native, stale)
                self.assertEqual(self.last_base.or_calls, 0)

    def test_active_criterion_without_failures_preserves_display(self):
        result, base = self.locate("WARP")
        self.assertEqual(result["located_entity_ids"], [])
        self.assertFalse(result["display_changed"])
        self.assertEqual(result["reason"], "no_matching_failures")
        self.assertEqual(base.or_calls, 0)


class QualityFilterSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_forwards_filter_and_rejects_invalid_shape(self):
        from mcp.server.fastmcp.exceptions import ToolError
        helper = workflow_fixtures.QualityWorkflowSchemaTests()
        submitted = []
        server = helper._server(submitted)
        args = helper._args()
        for key in ("backup_path", "movable_node_ids", "maximum_displacement", "method"):
            args.pop(key)
        await server.call_tool("ansa_mesh_quality_locate", dict(args, criterion="ASPECT"))
        self.assertEqual(submitted[-1][1]["criterion"], "ASPECT")
        await server.call_tool("ansa_mesh_quality_locate", args)
        self.assertNotIn("criterion", submitted[-1][1])
        for criterion in (True, 1, "", "x" * 129):
            with self.subTest(criterion=criterion), self.assertRaises(ToolError):
                await server.call_tool("ansa_mesh_quality_locate", dict(args, criterion=criterion))
        self.assertEqual(len(submitted), 2)


if __name__ == "__main__":
    unittest.main()
