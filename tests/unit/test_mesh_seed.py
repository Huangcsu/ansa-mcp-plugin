import json
import math
import unittest

from ansa_mcp_bridge.operations.mesh_planning import plan_blocks
from ansa_mcp_bridge.operations.mesh_seed import recommend_seeds


def _line(edge_id, length, target_length, **kwargs):
    return {
        "id": edge_id,
        "length": length,
        "target_length": target_length,
        "kind": "line",
        **kwargs,
    }


def _arc(edge_id, length, target_length, radius, max_chord_error, **kwargs):
    return {
        "id": edge_id,
        "length": length,
        "target_length": target_length,
        "kind": "circular_arc",
        "radius": radius,
        "max_chord_error": max_chord_error,
        **kwargs,
    }


def _plan_result(seed_constraints):
    return plan_blocks({
        "template": "straight_sweep",
        "blocks": [{
            "id": "block-1", "source": {"type": "FACE", "id": 1},
            "target": {"type": "FACE", "id": 2}, "depends_on": [],
            "sweep_direction": [0, 0, 1], "layers": 1,
        }],
        "interfaces": [],
        "seed_constraints": seed_constraints,
    })


class RecommendSeedsTests(unittest.TestCase):
    def test_line_constraints_are_compatible_with_plan_blocks(self):
        result = recommend_seeds({
            "edges": [
                _line("edge-a", 2.2, 1.0, min_segments=4, multiple=3),
                _line("edge-b", 0.5, 2.0),
            ],
        })

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            [(row["edge_id"], row["required_segments"], row["count"])
             for row in result["edge_segments"]],
            [("edge-a", 4, 6), ("edge-b", 1, 1)],
        )
        self.assertEqual(result["input_origin"], "caller_declared_geometry")
        self.assertFalse(result["native_geometry_verified"])
        planned = _plan_result(result["seed_constraints"])
        self.assertEqual(planned["edge_segments"], [
            {"edge_id": "edge-a", "segments": 6},
            {"edge_id": "edge-b", "segments": 1},
        ])

    def test_closed_arc_minimum_and_small_chord_error_are_stable(self):
        result = recommend_seeds({
            "max_segments": 10000,
            "edges": [
                _arc("closed", math.tau, 100.0, 1.0, 100.0),
                _arc("fine", 1.0, 100.0, 1.0, 1e-18),
            ],
        })

        rows = {row["edge_id"]: row for row in result["edge_segments"]}
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(rows["closed"]["closed_arc"])
        self.assertEqual(rows["closed"]["required_segments"], 3)
        self.assertEqual(rows["closed"]["count"], 3)
        self.assertGreater(rows["fine"]["required_segments"], 10000)
        self.assertGreater(rows["fine"]["required_chord_error"], 0.0)
        self.assertLessEqual(rows["fine"]["required_chord_error"], 1e-18)
        self.assertIsNone(rows["fine"]["count"])
        self.assertEqual(result["seed_constraints"], [])

    def test_overlapping_equal_groups_merge_and_use_lcm(self):
        result = recommend_seeds({
            "edges": [
                _line("a", 4, 1, multiple=2),
                _line("b", 5, 1, multiple=3),
                _line("c", 10, 1, multiple=4),
                _line("d", 2, 1, multiple=5),
            ],
            "equal_groups": [["a", "b"], ["b", "c"]],
        })

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"]["equal_group_count"], 1)
        self.assertEqual(result["seed_constraints"], [
            {"id": "seed-constraint-0001", "edge_ids": ["d"], "segments": 5},
            {"id": "seed-constraint-0002", "edge_ids": ["a", "b", "c"], "segments": 12},
        ])
        self.assertEqual([row["count"] for row in result["edge_segments"]], [12, 12, 12, 5])

    def test_seed_constraints_chunk_large_equal_group_for_plan_blocks(self):
        edge_ids = [f"edge-{index:03}" for index in range(129)]
        result = recommend_seeds({
            "edges": [_line(edge_id, 2, 1) for edge_id in edge_ids],
            "equal_groups": [edge_ids],
        })

        self.assertEqual([len(item["edge_ids"]) for item in result["seed_constraints"]], [128, 1])
        planned = _plan_result(result["seed_constraints"])
        self.assertEqual(len(planned["edge_segments"]), 129)
        self.assertEqual({item["segments"] for item in planned["edge_segments"]}, {2})

    def test_large_equal_group_is_stored_once_and_keeps_plan_compatibility(self):
        edge_ids = [f"edge-{index:04}" for index in range(3000)]
        result = recommend_seeds({
            "edges": [_line(edge_id, 2, 1) for edge_id in edge_ids],
            "equal_groups": [edge_ids],
        })

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["equal_groups"]), 1)
        group = result["equal_groups"][0]
        self.assertEqual(group["edge_ids"], edge_ids)
        self.assertEqual(
            {row["equal_group_id"] for row in result["edge_segments"]},
            {group["id"]},
        )
        self.assertTrue(all("equal_group_edge_ids" not in row for row in result["edge_segments"]))
        constrained_edge_ids = [
            edge_id
            for constraint in result["seed_constraints"]
            for edge_id in constraint["edge_ids"]
        ]
        self.assertEqual(constrained_edge_ids, edge_ids)

        planned = _plan_result(result["seed_constraints"])
        self.assertEqual(len(planned["edge_segments"]), len(edge_ids))
        self.assertEqual(
            [row["edge_id"] for row in planned["edge_segments"]],
            edge_ids,
        )
        serialized = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertLess(len(serialized), 4 * 1024 * 1024)

    def test_budget_excess_is_incomplete_without_truncated_counts(self):
        result = recommend_seeds({
            "max_segments": 4,
            "edges": [_line("edge", 5, 1)],
        })

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["edge_segments"][0]["required_segments"], 5)
        self.assertEqual(result["edge_segments"][0]["group_required_segments"], 5)
        self.assertIsNone(result["edge_segments"][0]["recommended_segments"])
        self.assertIsNone(result["edge_segments"][0]["count"])
        self.assertIsNone(result["edge_segments"][0]["equal_group_id"])
        self.assertEqual(result["equal_groups"], [])
        self.assertEqual(result["summary"]["over_budget_groups"], [
            {"edge_id": "edge", "required_segments": 5},
        ])
        self.assertEqual(result["seed_constraints"], [])

    def test_rejects_invalid_schema_ids_and_numeric_values(self):
        invalid_plans = [
            {"edges": [_line("a", 1, 1, surprise=True)]},
            {"edges": [_line("a", 1, 1), _line("a", 2, 1)]},
            {"edges": [_line("a", 1, 1)], "equal_groups": [["a", "missing"]]},
            {"edges": [_line("a", True, 1)]},
            {"edges": [_line("a", float("nan"), 1)]},
            {"edges": [_line("a", 1, 1, min_segments=True)]},
            {"edges": [{
                "id": "a", "length": 1, "target_length": 1,
                "kind": "circular_arc", "radius": 1,
            }]},
            {"edges": [_line("a", 1, 1)], "max_segments": True},
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan), self.assertRaises((TypeError, ValueError)):
                recommend_seeds(plan)

    def test_ratio_overflow_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "ratio"):
            recommend_seeds({"edges": [_line("a", 1e308, 1e-308)]})
        with self.assertRaisesRegex(ValueError, "ratio"):
            recommend_seeds({
                "edges": [_arc("a", 1e308, 1.0, 1e-308, 0.1)],
            })


if __name__ == "__main__":
    unittest.main()
