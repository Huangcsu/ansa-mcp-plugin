import unittest

from ansa_mcp_bridge.operations.mesh_planning import audit_source, plan_blocks


def _block(block_id, *, depends_on=(), source=None, target=None):
    return {
        "id": block_id,
        "source": source or {"type": "FACE", "id": 100 + int(block_id)},
        "target": target or {"type": "FACE", "id": 200 + int(block_id)},
        "depends_on": list(depends_on),
        "sweep_direction": [0, 0, 1],
        "layers": 4,
    }


def _plan():
    return {
        "template": "straight_sweep",
        "blocks": [_block("2", depends_on=["1"]), _block("1"), _block("3")],
        "interfaces": [
            {"id": "if-2", "blocks": ["1", "2"], "facets": ["f1", "f2"]},
            {"id": "if-1", "blocks": ["2", "3"], "source": "s2", "target": "s3"},
        ],
        "seed_constraints": [
            {"id": "chain-a", "edge_ids": ["e1", "e2"], "segments": 4},
            {"id": "chain-b", "edge_id": "e3", "segments": 2},
        ],
    }


def _snapshot(*, target_shift=None, source_edge_lengths=None, target_nodes=None, element_type="CQUAD4"):
    shift = target_shift or (0.0, 0.0, 4.0)
    target_nodes = target_nodes or [5, 6, 7, 8]
    base_nodes = {
        "1": {"position": [0, 0, 0]},
        "2": {"position": [1, 0, 0]},
        "3": {"position": [1, 1, 0]},
        "4": {"position": [0, 1, 0]},
    }
    corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for node_id, (x, y) in zip(target_nodes, corners):
        base_nodes[str(node_id)] = {"position": [x + shift[0], y + shift[1], shift[2]]}
    source_edges = ["s1", "s2", "s3", "s4"]
    if source_edge_lengths:
        # Move the second source node to create the requested first-edge length.
        base_nodes["2"]["position"] = [source_edge_lengths[0], 0, 0]
    elements = [
        {"entity": {"type": "SHELL", "id": 101}, "element_type": element_type,
         "node_ids": [1, 2, 3, 4], "edge_ids": source_edges},
        {"entity": {"type": "SHELL", "id": 201}, "element_type": element_type,
         "node_ids": target_nodes, "edge_ids": ["t1", "t2", "t3", "t4"]},
    ]
    return {"nodes": base_nodes, "elements": elements, "complete": True}


class PlanBlocksTests(unittest.TestCase):
    def test_returns_deterministic_topological_order_and_integer_counts(self):
        result = plan_blocks(_plan())
        self.assertEqual(result["execution_order"], ["1", "2", "3"])
        self.assertEqual(result["edge_segments"], [
            {"edge_id": "e1", "segments": 4},
            {"edge_id": "e2", "segments": 4},
            {"edge_id": "e3", "segments": 2},
        ])
        self.assertFalse(result["scope"]["automatic_partitioning"])
        self.assertEqual([row["id"] for row in result["interfaces"]], ["if-1", "if-2"])

    def test_input_order_does_not_change_plan(self):
        first = plan_blocks(_plan())
        second_plan = _plan()
        second_plan["blocks"].reverse()
        second_plan["interfaces"].reverse()
        second = plan_blocks(second_plan)
        self.assertEqual(first, second)

    def test_accepts_full_top_level_plan_context_but_ignores_it(self):
        plan = _plan()
        plan["metadata"] = {"report_scope": "C/D"}
        result = plan_blocks(plan)
        self.assertNotIn("metadata", result)
        self.assertEqual(result["execution_order"], ["1", "2", "3"])

    def test_rejects_unsupported_template_and_inner_extra_fields(self):
        plan = _plan()
        plan["template"] = "auto_partition"
        with self.assertRaisesRegex(ValueError, "straight_sweep"):
            plan_blocks(plan)
        plan = _plan()
        plan["blocks"][0]["magic"] = True
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            plan_blocks(plan)

    def test_rejects_duplicate_missing_and_cyclic_blocks(self):
        plan = _plan()
        plan["blocks"].append(_block("1"))
        with self.assertRaisesRegex(ValueError, "duplicate block id"):
            plan_blocks(plan)
        plan = _plan()
        plan["blocks"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "depends on missing"):
            plan_blocks(plan)
        plan = _plan()
        plan["blocks"][0]["depends_on"] = ["3"]
        plan["blocks"][2]["depends_on"] = ["2"]
        with self.assertRaisesRegex(ValueError, "cycle detected"):
            plan_blocks(plan)

    def test_rejects_interface_with_missing_block(self):
        plan = _plan()
        plan["interfaces"][0]["blocks"] = ["1", "missing"]
        with self.assertRaisesRegex(ValueError, "missing block"):
            plan_blocks(plan)

    def test_reports_conflicting_integer_segment_counts(self):
        plan = _plan()
        plan["seed_constraints"].append(
            {"id": "conflict", "edge_id": "e2", "segments": 3}
        )
        with self.assertRaisesRegex(ValueError, "integer segment conflict.*e2"):
            plan_blocks(plan)

    def test_solves_one_unknown_integer_in_equal_total_edge_chains(self):
        plan = _plan()
        plan["seed_constraints"] = [
            {"id": "known", "edge_id": "e1", "segments": 2},
            {"id": "balance", "left_edges": ["e1", "e2"],
             "right_edges": [{"edge_id": "e3", "segments": 5}]},
        ]
        result = plan_blocks(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["sum_constraints"], [{
            "id": "balance", "status": "solved", "left_total": 5,
            "right_total": 5, "unresolved_edge_ids": [],
        }])
        self.assertEqual(result["edge_segments"], [
            {"edge_id": "e1", "segments": 2},
            {"edge_id": "e2", "segments": 3},
            {"edge_id": "e3", "segments": 5},
        ])

    def test_underdetermined_edge_chain_sum_is_incomplete(self):
        plan = _plan()
        plan["seed_constraints"] = [
            {"id": "balance", "left_edges": ["e1", "e2"], "right_edges": ["e3", "e4"]},
        ]
        result = plan_blocks(plan)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["sum_constraints"][0]["status"], "incomplete")
        self.assertEqual(result["sum_constraints"][0]["unresolved_edge_ids"],
                         ["e1", "e2", "e3", "e4"])
        self.assertTrue(all(item["segments"] is None for item in result["seed_constraints"][0]["left_edges"]))

    def test_conflicting_edge_chain_sum_is_clear(self):
        plan = _plan()
        plan["seed_constraints"] = [
            {"id": "known", "edge_id": "e1", "segments": 4},
            {"id": "balance", "left_edges": ["e1"], "right_edges": [
                {"edge_id": "e2", "segments": 3},
            ]},
        ]
        with self.assertRaisesRegex(ValueError, "integer segment sum conflict.*balance"):
            plan_blocks(plan)

    def test_does_not_mutate_input(self):
        plan = _plan()
        original = repr(plan)
        plan_blocks(plan)
        self.assertEqual(repr(plan), original)


class AuditSourceTests(unittest.TestCase):
    def test_passes_corresponding_straight_sweep_and_equal_edge_chain(self):
        report = audit_source(
            _snapshot(), source_ids=[101], target_ids=[201], direction=[0, 0, 1],
            layers=4, target_length=1.0,
            seed_constraints=[{"id": "edges", "edge_ids": ["s1", "s2"], "segments": 1}],
        )
        self.assertTrue(report["supported"])
        self.assertTrue(report["passed"])
        self.assertTrue(report["checks"]["topology_correspondence"])
        self.assertTrue(report["checks"]["seed_constraints"])
        self.assertEqual(report["metrics"]["mean_sweep_distance"], 4.0)
        self.assertEqual(report["metrics"]["estimated_layer_spacing"], 1.0)

    def test_wrong_direction_fails_closed(self):
        report = audit_source(_snapshot(), source_ids=[101], target_ids=[201],
                              direction=[0, 0, -1], layers=4, target_length=1.0)
        self.assertFalse(report["passed"])
        self.assertIn("sweep_direction_reversed", {item["code"] for item in report["issues"]})

    def test_topology_connectivity_mismatch_is_reported(self):
        snapshot = _snapshot()
        snapshot["elements"][1]["node_ids"] = [5, 7, 6, 8]
        report = audit_source(snapshot, source_ids=[101], target_ids=[201], direction=[0, 0, 1],
                              layers=4, target_length=1.0)
        self.assertFalse(report["passed"])
        self.assertIn("element_connectivity_mismatch", {item["code"] for item in report["issues"]})

    def test_short_edges_are_reported_as_warnings(self):
        report = audit_source(_snapshot(), source_ids=[101],
                              target_ids=[201], direction=[0, 0, 1], layers=4, target_length=20.0)
        self.assertTrue(report["passed"])
        self.assertIn("short_source_edges", {item["code"] for item in report["warnings"]})
        self.assertGreater(report["metrics"]["short_edge_count"], 0)

    def test_source_only_audit_checks_face_angles_and_returns_incomplete(self):
        report = audit_source(_snapshot(), source_ids=[101], target_ids=[],
                              direction=[0, 0, 1], layers=4, target_length=20.0)
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["passed"])
        self.assertIsNone(report["checks"]["topology_correspondence"])
        self.assertIsNone(report["checks"]["sweep_direction"])
        self.assertTrue(report["checks"]["source_face_angles"])
        self.assertIn("short_source_edges", {item["code"] for item in report["warnings"]})
        self.assertEqual(report["metrics"]["minimum_source_face_angle_degrees"], 90.0)

    def test_source_audit_reports_underdetermined_seed_sum_incomplete(self):
        report = audit_source(
            _snapshot(), source_ids=[101], target_ids=None, direction=[0, 0, 1],
            layers=4, target_length=1.0,
            seed_constraints=[{
                "id": "balance", "left_edges": ["s1", "s2"], "right_edges": ["s3", "s4"],
            }],
        )
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["checks"]["seed_constraints"])
        self.assertIn("seed_sum_underdetermined",
                      {item["code"] for item in report["incomplete_reasons"]})

    def test_source_audit_checks_resolved_edge_chain_totals(self):
        report = audit_source(
            _snapshot(), source_ids=[101], target_ids=[201], direction=[0, 0, 1],
            layers=4, target_length=1.0,
            seed_constraints=[
                {"id": "known", "edge_id": "s1", "segments": 2},
                {"id": "balance", "left_edges": ["s1", "s2"],
                 "right_edges": [{"edge_id": "s3", "segments": 3}]},
            ],
        )
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["checks"]["seed_constraints"])
        self.assertEqual(report["metrics"]["seed_sum_constraints"][0]["left_total"], 3)
        self.assertEqual(report["metrics"]["seed_sum_constraints"][0]["right_total"], 3)

    def test_equal_length_constraint_and_missing_edge_are_audited(self):
        snapshot = _snapshot(source_edge_lengths=[0.5])
        report = audit_source(snapshot, source_ids=[101], target_ids=[201], direction=[0, 0, 1],
                              layers=4, target_length=1.0,
                              seed_constraints=[{"id": "chain", "edge_ids": ["s1", "s2"], "segments": 1}])
        self.assertFalse(report["passed"])
        self.assertIn("seed_chain_not_equal_length", {item["code"] for item in report["issues"]})
        missing = audit_source(_snapshot(), source_ids=[101], target_ids=[201], direction=[0, 0, 1],
                               layers=4, target_length=1.0,
                               seed_constraints=[{"id": "chain", "edge_id": "absent", "segments": 1}])
        self.assertIn("seed_edge_missing", {item["code"] for item in missing["issues"]})

    def test_unknown_snapshot_or_element_adapter_is_not_reported_as_pass(self):
        with self.assertRaises(NotImplementedError):
            audit_source({"nodes": [], "elements": []}, source_ids=[1], target_ids=[2],
                         direction=[0, 0, 1], layers=1, target_length=1)
        snapshot = _snapshot(element_type="SHELL_UNKNOWN")
        with self.assertRaises(NotImplementedError):
            audit_source(snapshot, source_ids=[101], target_ids=[201], direction=[0, 0, 1],
                         layers=4, target_length=1)

    def test_rejects_duplicate_and_overlapping_scope_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate element IDs"):
            audit_source(_snapshot(), source_ids=[101, 101], target_ids=[201],
                         direction=[0, 0, 1], layers=4, target_length=1)
        with self.assertRaisesRegex(ValueError, "overlap"):
            audit_source(_snapshot(), source_ids=[101], target_ids=[101],
                         direction=[0, 0, 1], layers=4, target_length=1)


if __name__ == "__main__":
    unittest.main()
