import copy
import json
import math
import unittest

from ansa_mcp_bridge.operations.surface_remesh_audit import audit_surface


def _quad():
    nodes = {
        1: [0.0, 0.0, 0.0],
        2: [1.0, 0.0, 0.0],
        3: [1.0, 1.0, 0.0],
        4: [0.0, 1.0, 0.0],
    }
    elements = [{
        "id": 10,
        "element_type": "CQUAD4",
        "node_ids": [1, 2, 3, 4],
    }]
    paths = [[1, 2], [2, 3], [3, 4], [4, 1]]
    return elements, nodes, paths


def _mixed():
    nodes = {
        1: [0.0, 0.0, 0.0],
        2: [1.0, 0.0, 0.0],
        3: [2.0, 0.0, 0.0],
        4: [2.0, 1.0, 0.0],
        5: [1.0, 1.0, 0.0],
    }
    elements = [
        {"id": 21, "element_type": "CTRIA3", "node_ids": [1, 2, 5]},
        {"id": 22, "element_type": "CQUAD4", "node_ids": [2, 3, 4, 5]},
    ]
    paths = [[1, 2], [2, 3], [3, 4], [4, 5], [5, 1]]
    return elements, nodes, paths


def _two_quads():
    nodes = {
        1: [0.0, 0.0, 0.0],
        2: [1.0, 0.0, 0.0],
        3: [1.0, 1.0, 0.0],
        4: [0.0, 1.0, 0.0],
        5: [3.0, 0.0, 0.0],
        6: [4.0, 0.0, 0.0],
        7: [4.0, 1.0, 0.0],
        8: [3.0, 1.0, 0.0],
    }
    elements = [
        {"id": 31, "element_type": "CQUAD4", "node_ids": [1, 2, 3, 4]},
        {"id": 32, "element_type": "CQUAD4", "node_ids": [5, 6, 7, 8]},
    ]
    paths = [
        [1, 2], [2, 3], [3, 4], [4, 1],
        [5, 6], [6, 7], [7, 8], [8, 5],
    ]
    return elements, nodes, paths


def _holed_square():
    nodes = {
        1: [0.0, 0.0, 0.0],
        2: [4.0, 0.0, 0.0],
        3: [4.0, 4.0, 0.0],
        4: [0.0, 4.0, 0.0],
        5: [1.0, 1.0, 0.0],
        6: [2.0, 1.0, 0.0],
        7: [2.0, 2.0, 0.0],
        8: [1.0, 2.0, 0.0],
    }
    elements = [
        {"id": 41, "element_type": "CQUAD4", "node_ids": [1, 2, 6, 5]},
        {"id": 42, "element_type": "CQUAD4", "node_ids": [2, 3, 7, 6]},
        {"id": 43, "element_type": "CQUAD4", "node_ids": [3, 4, 8, 7]},
        {"id": 44, "element_type": "CQUAD4", "node_ids": [4, 1, 5, 8]},
    ]
    paths = [
        [1, 2], [2, 3], [3, 4], [4, 1],
        [5, 6], [6, 7], [7, 8], [8, 5],
    ]
    return elements, nodes, paths


def _pinched_internal_vertex_with_euler_failure():
    root_three = math.sqrt(3.0)
    nodes = {
        1: [0.0, 0.0, 0.0],
        2: [-1.0, -root_three, 0.0],
        3: [2.0, 0.0, 0.0],
        4: [-1.0, root_three, 0.0],
        5: [-2.0, -2.0 * root_three, 0.0],
        6: [4.0, 0.0, 0.0],
        7: [-2.0, 2.0 * root_three, 0.0],
    }
    elements = [
        {"id": 51, "element_type": "CTRIA3", "node_ids": [1, 2, 3]},
        {"id": 52, "element_type": "CTRIA3", "node_ids": [1, 3, 4]},
        {"id": 53, "element_type": "CTRIA3", "node_ids": [1, 4, 2]},
        {"id": 54, "element_type": "CTRIA3", "node_ids": [1, 5, 6]},
        {"id": 55, "element_type": "CTRIA3", "node_ids": [1, 6, 7]},
        {"id": 56, "element_type": "CTRIA3", "node_ids": [1, 7, 5]},
        {"id": 57, "element_type": "CQUAD4", "node_ids": [3, 2, 6, 5]},
    ]
    paths = [[3, 4], [4, 2], [2, 6], [6, 7], [7, 5], [5, 3]]
    return elements, nodes, paths


class SurfaceRemeshAuditTests(unittest.TestCase):
    def test_positive_quad_triangle_and_mixed_surface(self):
        quad = audit_surface(*_quad(), [0.0, 0.0, 7.0])
        self.assertEqual(quad["shell_count"], 1)
        self.assertEqual(quad["quad_count"], 1)
        self.assertEqual(quad["tria_count"], 0)
        self.assertEqual(quad["boundary_edge_count"], 4)
        self.assertEqual(quad["boundary_loop_count"], 1)
        self.assertEqual(quad["euler"], 1)
        self.assertAlmostEqual(quad["area"], 1.0)
        self.assertIs(quad["connected"], True)
        self.assertEqual(set(quad), {
            "shell_count", "node_count", "quad_count", "tria_count",
            "boundary_edge_count", "internal_edge_count",
            "boundary_loop_count", "euler", "area", "connected",
        })
        json.dumps(quad, allow_nan=False)

        triangle = audit_surface(
            [{"id": 11, "element_type": "CTRIA3", "node_ids": [1, 2, 3]}],
            {1: [0, 0, 2], 2: [1, 0, 2], 3: [0, 1, 2]},
            [[1, 2], [2, 3], [3, 1]],
            [0, 0, 4],
        )
        self.assertEqual(triangle["tria_count"], 1)
        self.assertAlmostEqual(triangle["area"], 0.5)

        mixed = audit_surface(*_mixed(), [0, 0, 1])
        self.assertEqual(mixed["shell_count"], 2)
        self.assertEqual(mixed["quad_count"], 1)
        self.assertEqual(mixed["tria_count"], 1)
        self.assertEqual(mixed["internal_edge_count"], 1)
        self.assertAlmostEqual(mixed["area"], 1.5)

    def test_normal_is_normalized_and_input_is_not_mutated(self):
        elements, nodes, paths = _quad()
        for point in nodes.values():
            point[2] = 5.0
        original = copy.deepcopy((elements, nodes, paths))
        result = audit_surface(elements, nodes, paths, [0, 0, 100], tolerance=1e-8)
        self.assertAlmostEqual(result["area"], 1.0)
        self.assertEqual((elements, nodes, paths), original)

    def test_duplicate_ids_topology_or_node_references_are_rejected(self):
        elements, nodes, paths = _quad()

        duplicate_id = copy.deepcopy(elements)
        duplicate_id.append(copy.deepcopy(elements[0]))
        with self.assertRaisesRegex(ValueError, "duplicate shell element ID"):
            audit_surface(duplicate_id, nodes, paths, [0, 0, 1])

        duplicate_topology = copy.deepcopy(elements)
        duplicate_topology.append({
            "id": 99,
            "element_type": "CQUAD4",
            "node_ids": [4, 3, 2, 1],
        })
        with self.assertRaisesRegex(ValueError, "duplicate cell topology"):
            audit_surface(duplicate_topology, nodes, paths, [0, 0, 1])

        repeated = copy.deepcopy(elements)
        repeated[0]["node_ids"] = [1, 2, 2, 4]
        with self.assertRaisesRegex(ValueError, "duplicate node references"):
            audit_surface(repeated, nodes, paths, [0, 0, 1])

        missing = copy.deepcopy(elements)
        missing[0]["node_ids"] = [1, 2, 3, 99]
        with self.assertRaisesRegex(ValueError, "unknown node IDs"):
            audit_surface(missing, nodes, paths, [0, 0, 1])

    def test_invalid_coordinates_normal_and_unused_nodes_are_rejected(self):
        elements, nodes, paths = _quad()
        for label, point in (
            ("nonfinite", [0.0, 0.0, math.inf]),
            ("non3d", [0.0, 0.0]),
        ):
            with self.subTest(case=label):
                bad_nodes = copy.deepcopy(nodes)
                bad_nodes[4] = point
                with self.assertRaises(ValueError):
                    audit_surface(elements, bad_nodes, paths, [0, 0, 1])

        unused_nodes = copy.deepcopy(nodes)
        unused_nodes[5] = [2.0, 2.0, 0.0]
        with self.assertRaisesRegex(ValueError, "unused node"):
            audit_surface(elements, unused_nodes, paths, [0, 0, 1])

        for bad_normal in ([0, 0, 0], [0, math.nan, 1]):
            with self.subTest(normal=bad_normal):
                with self.assertRaisesRegex(ValueError, "normal"):
                    audit_surface(elements, nodes, paths, bad_normal)

    def test_nonplanar_or_flipped_or_degenerate_cells_are_rejected(self):
        elements, nodes, paths = _quad()

        nonplanar = copy.deepcopy(nodes)
        nonplanar[4][2] = 1e-5
        with self.assertRaisesRegex(ValueError, "coplanar"):
            audit_surface(elements, nonplanar, paths, [0, 0, 1], tolerance=1e-7)

        reversed_quad = copy.deepcopy(elements)
        reversed_quad[0]["node_ids"].reverse()
        with self.assertRaisesRegex(ValueError, "quad.*corner Jacobian"):
            audit_surface(reversed_quad, nodes, paths, [0, 0, 1])

        concave = copy.deepcopy(nodes)
        concave[3] = [0.5, 0.5, 0.0]
        with self.assertRaisesRegex(ValueError, "quad.*concave"):
            audit_surface(elements, concave, paths, [0, 0, 1])

        folded = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 1.0, 0.0],
            3: [1.0, 0.0, 0.0],
            4: [0.0, 1.0, 0.0],
        }
        with self.assertRaisesRegex(ValueError, "quad.*folded"):
            audit_surface(elements, folded, paths, [0, 0, 1])

        zero = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 0.0, 0.0],
            3: [2.0, 0.0, 0.0],
        }
        with self.assertRaisesRegex(ValueError, "triangle.*zero area"):
            audit_surface(
                [{"id": 12, "element_type": "CTRIA3", "node_ids": [1, 2, 3]}],
                zero,
                [[1, 2], [2, 3], [3, 1]],
                [0, 0, 1],
            )

        flipped = {1: [0.0, 0.0, 0.0], 2: [1.0, 0.0, 0.0],
                   3: [0.0, 1.0, 0.0]}
        with self.assertRaisesRegex(ValueError, "triangle.*flipped"):
            audit_surface(
                [{"id": 13, "element_type": "CTRIA3", "node_ids": [1, 3, 2]}],
                flipped,
                [[1, 2], [2, 3], [3, 1]],
                [0, 0, 1],
            )

    def test_nonmanifold_edge_and_same_direction_internal_use_are_rejected(self):
        nonmanifold_nodes = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 0.0, 0.0],
            3: [0.5, 1.0, 0.0],
            4: [0.5, -1.0, 0.0],
            5: [0.5, 2.0, 0.0],
        }
        nonmanifold = [
            {"id": 61, "element_type": "CTRIA3", "node_ids": [1, 2, 3]},
            {"id": 62, "element_type": "CTRIA3", "node_ids": [2, 1, 4]},
            {"id": 63, "element_type": "CTRIA3", "node_ids": [1, 2, 5]},
        ]
        with self.assertRaisesRegex(ValueError, "non-manifold"):
            audit_surface(nonmanifold, nonmanifold_nodes, [], [0, 0, 1])

        same_direction_nodes = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 0.0, 0.0],
            3: [0.5, 1.0, 0.0],
            4: [0.5, 2.0, 0.0],
        }
        same_direction = [
            {"id": 64, "element_type": "CTRIA3", "node_ids": [1, 2, 3]},
            {"id": 65, "element_type": "CTRIA3", "node_ids": [1, 2, 4]},
        ]
        with self.assertRaisesRegex(ValueError, "same direction"):
            audit_surface(
                same_direction,
                same_direction_nodes,
                [[1, 3], [3, 2], [2, 4], [4, 1]],
                [0, 0, 1],
            )

    def test_boundary_reference_mismatch_duplicate_and_unknown_ids_are_rejected(self):
        elements, nodes, paths = _quad()

        with self.assertRaisesRegex(ValueError, "single-use edge set differs"):
            audit_surface(elements, nodes, [[1, 2, 3], [3, 4]], [0, 0, 1])
        with self.assertRaisesRegex(ValueError, "single-use edge set differs"):
            audit_surface(
                elements,
                nodes,
                [[1, 2], [2, 3], [3, 4], [4, 1], [1, 3]],
                [0, 0, 1],
            )
        with self.assertRaisesRegex(ValueError, "duplicate edge"):
            audit_surface(
                elements,
                nodes,
                [[1, 2], [2, 3], [3, 4], [4, 1], [1, 2]],
                [0, 0, 1],
            )
        with self.assertRaisesRegex(ValueError, "unknown GRID"):
            audit_surface(elements, nodes, [[2, 99]], [0, 0, 1])
        with self.assertRaisesRegex(ValueError, "open simple chain"):
            audit_surface(elements, nodes, [[1, 2, 3, 1]], [0, 0, 1])

    def test_disconnected_components_and_point_contact_are_rejected(self):
        elements, nodes, paths = _two_quads()
        with self.assertRaisesRegex(ValueError, "disconnected"):
            audit_surface(elements, nodes, paths, [0, 0, 1])

        point_contact_nodes = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 0.0, 0.0],
            3: [0.0, 1.0, 0.0],
            4: [-1.0, 0.0, 0.0],
            5: [0.0, -1.0, 0.0],
        }
        point_contact = [
            {"id": 71, "element_type": "CTRIA3", "node_ids": [1, 2, 3]},
            {"id": 72, "element_type": "CTRIA3", "node_ids": [1, 4, 5]},
        ]
        with self.assertRaisesRegex(ValueError, "point contact"):
            audit_surface(
                point_contact,
                point_contact_nodes,
                [[1, 2], [2, 3], [3, 1], [1, 4], [4, 5], [5, 1]],
                [0, 0, 1],
            )

    def test_boundary_vertex_degree_must_be_two(self):
        root_three = math.sqrt(3.0)
        nodes = {
            1: [0.0, 0.0, 0.0],
            2: [-1.0, -root_three, 0.0],
            3: [2.0, 0.0, 0.0],
            4: [-2.0, -2.0 * root_three, 0.0],
            5: [4.0, 0.0, 0.0],
        }
        elements = [
            {"id": 81, "element_type": "CTRIA3", "node_ids": [1, 2, 3]},
            {"id": 82, "element_type": "CTRIA3", "node_ids": [1, 4, 5]},
            {"id": 83, "element_type": "CQUAD4", "node_ids": [3, 2, 5, 4]},
        ]
        paths = [[1, 2], [2, 5], [5, 1], [1, 4], [4, 3], [3, 1]]
        with self.assertRaisesRegex(ValueError, "boundary vertex 1 has degree 4"):
            audit_surface(elements, nodes, paths, [0, 0, 1])

    def test_shared_boundary_requires_only_selected_macro_fe_and_full_edges(self):
        elements = [
            {"id": 90, "element_type": "CQUAD4", "node_ids": [1, 2, 5, 6]},
            {"id": 91, "element_type": "CQUAD4", "node_ids": [2, 3, 4, 5]},
        ]
        nodes = {
            1: [0.0, 0.0, 0.0],
            2: [1.0, 0.0, 0.0],
            3: [2.0, 0.0, 0.0],
            4: [2.0, 1.0, 0.0],
            5: [1.0, 1.0, 0.0],
            6: [0.0, 1.0, 0.0],
        }
        # Edge 1-2-3 is the selected macro's shared perimeter. The adjacent
        # outside macro owns no rows in this call and must not be inferred.
        paths = [[1, 2, 3], [3, 4], [4, 5], [5, 6], [6, 1]]
        result = audit_surface(elements, nodes, paths, [0, 0, 1])
        self.assertEqual(result["boundary_edge_count"], 6)
        self.assertEqual(result["internal_edge_count"], 1)

        with self.assertRaisesRegex(ValueError, "single-use edge set differs"):
            audit_surface(
                elements,
                nodes,
                [[1, 3], [3, 4], [4, 5], [5, 6], [6, 1]],
                [0, 0, 1],
            )

    def test_holed_surface_has_two_loops_and_zero_euler_characteristic(self):
        result = audit_surface(*_holed_square(), [0, 0, 1])
        self.assertEqual(result["shell_count"], 4)
        self.assertEqual(result["node_count"], 8)
        self.assertEqual(result["quad_count"], 4)
        self.assertEqual(result["boundary_edge_count"], 8)
        self.assertEqual(result["internal_edge_count"], 4)
        self.assertEqual(result["boundary_loop_count"], 2)
        self.assertEqual(result["euler"], 0)
        self.assertAlmostEqual(result["area"], 15.0)

    def test_euler_characteristic_rejects_pinched_internal_vertex(self):
        elements, nodes, paths = _pinched_internal_vertex_with_euler_failure()
        with self.assertRaisesRegex(ValueError, "Euler characteristic is 0, expected 1"):
            audit_surface(elements, nodes, paths, [0, 0, 1])

    def test_macro_budgets_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "2000 shell elements"):
            audit_surface([{} for _ in range(2001)], {}, [], [0, 0, 1])
        with self.assertRaisesRegex(ValueError, "10000 nodes"):
            audit_surface([], {index: [0, 0, 0] for index in range(1, 10002)}, [], [0, 0, 1])


if __name__ == "__main__":
    unittest.main()
