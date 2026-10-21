"""Coverage, immutable pages and disk evidence at the large-model boundary."""

import copy
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import quality_reports, quality_workflow as workflow


class QualityJobsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = types.SimpleNamespace(session_dir=Path(directory.name), model_epoch=1,
                                           instance_id="instance", session_id="session")
        self.base = types.SimpleNamespace(CollectEntities=lambda *a, **k: [types.SimpleNamespace(_id=i) for i in range(1, 2502)])
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.profile = {"element_family": "shell"}
        self.calls = []

    def reporter(self, base, constants, deck, profile, ids):
        self.calls.append(ids)
        rows = [{"id": eid, "violations": ["ASPECT"] if eid % 2 else [], "unknown": [],
                 "metrics": {"ASPECT": 6.0 if eid % 2 else 1.0}, "passed": not eid % 2} for eid in ids]
        return {"rows": rows, "entity_ids": ids, "failed_entity_ids": [eid for eid in ids if eid % 2],
                "unknown_entity_ids": [], "complete": True, "geometry_sha256": str(ids),
                "scope_sha256": workflow._digest(ids)}, None, None, None

    def build(self, reporter=None):
        with patch.object(workflow, "_criteria", return_value="criteria"), \
             patch.object(workflow, "_scope", side_effect=lambda b, c, d, f, ids: (None, None, None, str(ids))):
            return quality_reports.build(self.state, self.base, self.constants, 1, self.profile,
                                         {"scope": "all", "batch_size": 1000}, reporter or self.reporter)

    def test_more_than_2000_have_complete_counts_and_lossless_filtered_pages(self):
        value = self.build()
        self.assertTrue(value["complete"])
        self.assertEqual(value["summary"]["checked_count"], 2501)
        self.assertEqual(value["summary"]["failed_count"], 1251)
        self.assertEqual([len(x) for x in self.calls], [1000, 1000, 501])
        ids, offset = [], 0
        while offset is not None:
            page = quality_reports.page(self.state, 1, {"report_id": value["report_id"], "criterion": "ASPECT", "offset": offset, "limit": 200})
            ids.extend(row["id"] for row in page["rows"])
            offset = page["next_offset"]
        self.assertEqual(ids, list(range(1, 2502, 2)))
        self.state.model_epoch += 1
        with self.assertRaisesRegex(ValueError, "different model"):
            quality_reports.page(self.state, 1, {"report_id": value["report_id"]})

    def test_failed_batch_is_incomplete_and_reports_actual_coverage(self):
        def fail(base, constants, deck, profile, ids):
            if ids[0] > 2000:
                raise RuntimeError("native call failed")
            return self.reporter(base, constants, deck, profile, ids)
        value = self.build(fail)
        self.assertFalse(value["complete"])
        self.assertFalse(value["passed"])
        self.assertEqual(value["summary"]["checked_count"], 2000)
        self.assertEqual(value["requested_count"], 2501)
        self.assertEqual(value["reason"], "batch_failed")

    def test_prior_chunk_change_invalidates_whole_snapshot(self):
        with patch.object(workflow, "_criteria", return_value="criteria"), \
             patch.object(workflow, "_scope", return_value=(None, None, None, "changed")):
            value = quality_reports.build(self.state, self.base, self.constants, 1, self.profile,
                                          {"scope": "all"}, self.reporter)
        self.assertFalse(value["complete"])
        self.assertFalse(value["snapshot_verified"])
        self.assertEqual(value["reason"], "snapshot_changed")

    def test_disk_snapshot_retains_outside_cards_and_exact_coordinate_restore(self):
        node = types.SimpleNamespace(_id=1, position=(1.0, 2.0, 3.0))
        element = types.SimpleNamespace(_id=2, ansa_type=lambda deck: "SHELL")
        def collect(base, deck, container, kind):
            return [element] if kind == "__ELEMENTS__" else [node]
        def card(base, deck, entity):
            return {"entity": {"type": "SHELL" if entity is element else "GRID", "id": entity._id},
                    "fields": {"property": 7} if entity is element else {"X1": entity.position[0]}}
        with patch.object(workflow, "_MAX_MODEL_FE", 1), patch.object(workflow, "_collect", side_effect=collect), \
             patch.object(workflow, "_card", side_effect=card), \
             patch.object(workflow.catalog, "_get_entity", return_value=(node, None)):
            before = workflow._fe_snapshot(None, self.constants, 1)
            unchanged = workflow._fe_snapshot(None, self.constants, 1)
            self.assertEqual(before, unchanged)
            node.position = (9.0, 2.0, 3.0)
            changed = workflow._fe_snapshot(None, self.constants, 1)
            self.assertNotEqual(before, changed)
            self.assertTrue(workflow._restore_positions(None, self.constants, 1, before, changed))
            self.assertEqual(node.position, (1.0, 2.0, 3.0))

    def test_bounded_continuation_reuses_baseline_and_keeps_verified_progress(self):
        before = {"failed_entity_ids": [1, 2]}
        middle = {"failed_entity_ids": [2]}
        nodes = {1: types.SimpleNamespace(position=(0.0, 0.0, 0.0))}
        params = {"backup_path": str(self.state.session_dir / "backup.ansa"),
                  "maximum_displacement": 1.0, "max_iterations": 3}
        good = {"ok": True, "applied": True, "improved": True, "quality_passed": False,
                "after": middle, "changed_node_ids": [1], "reason": "improved"}
        bad = {"ok": False, "applied": False, "after": middle,
               "coordinates_rolled_back": True, "recovery_required": False, "error": "no progress"}
        with patch.object(workflow, "_fe_snapshot", return_value={}), patch.object(workflow, "_criteria", return_value="criteria"), \
             patch.object(workflow, "_repair", side_effect=[good, bad]) as repair:
            value = workflow._repair_iterations(self.state, None, self.constants, 1, params, {}, [1, 2], before, [], nodes)
        self.assertTrue(value["ok"])
        self.assertEqual(value["before"], before)
        self.assertEqual(value["after"], middle)
        self.assertEqual(value["termination_reason"], "round_rolled_back")
        self.assertEqual(len(value["iterations"]), 2)
        self.assertEqual(repair.call_args_list[1].args[4]["maximum_total_displacement"], 1.0)
        self.assertTrue(repair.call_args_list[1].args[4]["backup_path"].endswith("-iteration-2.ansa"))

    def test_last_noop_does_not_erase_applied_progress_and_preflight_error_is_reported(self):
        before = {"complete": True, "failed_entity_ids": [1, 2]}
        middle = {"complete": True, "failed_entity_ids": [2]}
        params = {"backup_path": str(self.state.session_dir / "backup.ansa"), "maximum_displacement": 1.0, "max_iterations": 3}
        good = {"ok": True, "applied": True, "improved": True, "quality_passed": False,
                "after": middle, "changed_node_ids": [1], "reason": "improved"}
        noop = {"ok": True, "applied": False, "improved": False, "quality_passed": False,
                "after": middle, "reason": "native_fix_unavailable"}
        for final in (noop, FileExistsError("backup already exists")):
            with self.subTest(final=type(final).__name__), patch.object(workflow, "_fe_snapshot", return_value={}), \
                 patch.object(workflow, "_criteria", return_value="criteria"), \
                 patch.object(workflow, "_repair", side_effect=[copy.deepcopy(good), final]):
                value = workflow._repair_iterations(self.state, None, self.constants, 1, params, {}, [1, 2], before, [], {})
            self.assertTrue(value["ok"])
            self.assertTrue(value["applied"])
            self.assertEqual(value["repair_outcome"], "partially_repaired")
            self.assertEqual(value["after"], middle)
            self.assertEqual(len(value["iterations"]), 2)

    def test_recovery_required_does_not_access_invalid_native_node_handles(self):
        class Node:
            usable = True
            @property
            def position(self):
                if not self.usable:
                    raise AssertionError("invalid native node must not be accessed")
                return (0.0, 0.0, 0.0)
        node = Node()
        before = {"complete": True, "failed_entity_ids": [1]}
        def fail(*args):
            node.usable = False
            return {"ok": False, "applied": False, "recovery_required": True, "after": before}
        with patch.object(workflow, "_repair", side_effect=fail):
            value = workflow._repair_iterations(self.state, None, self.constants, 1,
                {"backup_path": "backup.ansa", "maximum_displacement": 1.0, "max_iterations": 2}, {}, [1], before, [], {1: node})
        self.assertTrue(value["recovery_required"])
        self.assertIsNone(value["maximum_cumulative_displacement"])


if __name__ == "__main__":
    unittest.main()
