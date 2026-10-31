import math
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import mesh_audit


def _one_hex_snapshot(*, block_id="A", offset_node_2=0.0):
    xyz = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
    ]
    xyz[1] = (xyz[1][0], xyz[1][1] + offset_node_2, xyz[1][2])
    element = {"id": 101, "type": "CHEXA", "nodes": list(range(1, 9))}
    if block_id is not None:
        element["block_id"] = block_id
    return {
        "nodes": [{"id": i + 1, "xyz": list(point)} for i, point in enumerate(xyz)],
        "elements": [element],
    }


def _cylindrical_hex_snapshot(*, node_2_offset=0.0):
    xyz = [
        (1.0, 0.0, 0.0), (0.0, 1.0 + node_2_offset, 0.0),
        (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0),
        (1.0, 0.0, 1.0), (0.0, 1.0, 1.0),
        (-1.0, 0.0, 1.0), (0.0, -1.0, 1.0),
    ]
    return {
        "nodes": [{"id": i + 1, "xyz": list(point)} for i, point in enumerate(xyz)],
        "elements": [{"id": 150, "type": "CHEXA", "nodes": list(range(1, 9))}],
    }


HEX_BOUNDARY = [
    [1, 4, 3, 2], [5, 6, 7, 8], [1, 2, 6, 5],
    [2, 3, 7, 6], [3, 4, 8, 7], [4, 1, 5, 8],
]


def _two_hex_snapshot(*, second_block="B", cracked=False):
    points = {
        1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0),
        5: (0, 0, 1), 6: (1, 0, 1), 7: (1, 1, 1), 8: (0, 1, 1),
        9: (2, 0, 0), 10: (2, 1, 0), 11: (2, 0, 1), 12: (2, 1, 1),
    }
    second_nodes = [2, 9, 10, 3, 6, 11, 12, 7]
    if cracked:
        points[13] = points[3]
        second_nodes[3] = 13
    first = {"id": 201, "type": "CHEXA", "nodes": [1, 2, 3, 4, 5, 6, 7, 8], "block_id": "A"}
    second = {"id": 202, "type": "CHEXA", "nodes": second_nodes}
    if second_block is not None:
        second["block_id"] = second_block
    return {
        "nodes": [{"id": node_id, "xyz": list(point)} for node_id, point in points.items()],
        "elements": [first, second],
    }


TWO_HEX_BOUNDARY = [
    [1, 4, 8, 5], [9, 10, 12, 11],
    [1, 2, 3, 4], [2, 9, 10, 3],
    [5, 6, 7, 8], [6, 11, 12, 7],
    [1, 2, 6, 5], [2, 9, 11, 6],
    [4, 3, 7, 8], [3, 10, 12, 7],
]


def _mixed_hex_penta_snapshot():
    points = {
        1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0),
        5: (0, 0, 1), 6: (1, 0, 1), 7: (1, 1, 1), 8: (0, 1, 1),
        9: (2, 0, 0), 10: (2, 0, 1),
    }
    return {
        "nodes": [{"id": node_id, "xyz": list(point)} for node_id, point in points.items()],
        "elements": [
            {"id": 301, "type": "CHEXA", "nodes": [1, 2, 3, 4, 5, 6, 7, 8], "block_id": "hex"},
            {"id": 302, "type": "CPENTA", "nodes": [2, 9, 3, 6, 10, 7], "block_id": "wedge"},
        ],
    }


MIXED_BOUNDARY = [
    [1, 4, 8, 5], [1, 2, 3, 4], [5, 6, 7, 8], [1, 2, 6, 5], [4, 3, 7, 8],
    [2, 3, 9], [6, 10, 7], [2, 9, 10, 6], [9, 3, 7, 10],
]


class MeshAuditTests(unittest.TestCase):
    def test_mixed_hex_penta_is_face_connected_and_oriented(self):
        snapshot = _mixed_hex_penta_snapshot()
        result = mesh_audit.audit_snapshot(
            snapshot,
            interfaces=[{"id": "hex-wedge", "blocks": ["hex", "wedge"], "facets": [[2, 3, 7, 6]]}],
            protected_cylinders=[],
            expected_boundary=MIXED_BOUNDARY,
            expected_components=1,
        )

        self.assertEqual(result["topology_status"], "pass")
        self.assertEqual(result["geometry_status"], "pass")
        self.assertEqual(result["quality_status"], "incomplete")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["sections"]["topology"]["component_count"], 1)
        self.assertEqual(result["sections"]["topology"]["interior_face_count"], 1)
        self.assertEqual(result["sections"]["interfaces"]["groups"][0]["status"], "pass")
        self.assertEqual(result["sections"]["expected_boundary"]["missing_facets"], [])
        self.assertGreater(result["sections"]["topology"]["signed_polyhedral_measure"]["minimum"], 0)
        self.assertEqual(result["sections"]["quality"]["checked"], False)

    def test_local_crack_is_found_against_complete_expected_boundary(self):
        snapshot = _two_hex_snapshot(cracked=True)
        result = mesh_audit.audit_snapshot(
            snapshot,
            interfaces=[{"id": "A-B", "blocks": ["A", "B"], "facets": [[2, 3, 6, 7]]}],
            protected_cylinders=[],
            expected_boundary=TWO_HEX_BOUNDARY,
            expected_components=1,
        )

        self.assertEqual(result["topology_status"], "fail")
        self.assertEqual(result["sections"]["topology"]["component_count"], 2)
        self.assertEqual(result["sections"]["expected_boundary"]["status"], "fail")
        self.assertTrue(result["sections"]["expected_boundary"]["missing_facets"])
        self.assertTrue(result["sections"]["expected_boundary"]["unexpected_facets"])
        self.assertIn("boundary_set_mismatch", {item["code"] for item in result["diagnostics"]})

    def test_unknown_interface_side_owner_is_incomplete(self):
        result = mesh_audit.audit_snapshot(
            _two_hex_snapshot(second_block=None),
            interfaces=[{"id": "A-B", "blocks": ["A", "B"], "facets": [[2, 3, 6, 7]]}],
            protected_cylinders=[],
            expected_boundary=TWO_HEX_BOUNDARY,
            expected_components=1,
        )

        self.assertEqual(result["topology_status"], "incomplete")
        self.assertEqual(result["sections"]["interfaces"]["status"], "incomplete")
        self.assertTrue(result["sections"]["interfaces"]["unknown_owner_facets"])
        self.assertNotEqual(result["status"], "pass")

    def test_cylinder_measurement_includes_outlier_node(self):
        snapshot = _cylindrical_hex_snapshot(node_2_offset=0.1)
        cylinder_facets = [[1, 2, 6, 5], [2, 3, 7, 6], [3, 4, 8, 7], [4, 1, 5, 8]]
        result = mesh_audit.audit_protected_cylinders(
            snapshot,
            [{
                "label": "bore",
                "center": [0, 0, 0],
                "axis": [0, 0, 2],
                "radius": 1,
                "tolerance": 0.01,
                "facets": cylinder_facets,
            }],
            expected_boundary=HEX_BOUNDARY,
        )

        section = result["geometry"]["cylinders"][0]
        self.assertEqual(result["status"], "fail")
        self.assertEqual(section["measured_node_occurrences"], 16)
        self.assertEqual(section["out_of_tolerance_node_occurrences"], 2)
        measured = [row for row in section["measurements"] if row["node_id"] == 2 and row["facet_index"] == 0]
        self.assertEqual(len(measured), 1)
        self.assertAlmostEqual(measured[0]["absolute_radial_error"], 0.1)
        self.assertFalse(measured[0]["within_tolerance"])

    def test_unknown_element_type_keeps_topology_incomplete(self):
        snapshot = _one_hex_snapshot()
        snapshot["elements"][0]["type"] = "CHEXA20"
        result = mesh_audit.audit_snapshot(
            snapshot, interfaces=[], protected_cylinders=[], expected_boundary=HEX_BOUNDARY,
        )
        self.assertEqual(result["topology_status"], "incomplete")
        self.assertEqual(result["sections"]["topology"]["unsupported_element_count"], 1)
        self.assertFalse(result["sections"]["expected_boundary"]["comparison_complete"])

    def test_negative_signed_polyhedral_measure_is_reported(self):
        snapshot = _one_hex_snapshot()
        snapshot["elements"][0]["nodes"] = [1, 4, 3, 2, 5, 8, 7, 6]
        result = mesh_audit.audit_snapshot(
            snapshot, interfaces=[], protected_cylinders=[], expected_boundary=HEX_BOUNDARY,
        )
        self.assertEqual(result["topology_status"], "fail")
        measure = result["sections"]["topology"]["signed_polyhedral_measure"]
        self.assertLess(measure["minimum"], 0)
        self.assertIn(
            "negative_signed_polyhedral_measure",
            {item["code"] for item in result["diagnostics"]},
        )
        self.assertIn("not a Jacobian", measure["method"])

    def test_strict_snapshot_validation(self):
        snapshot = _one_hex_snapshot()
        snapshot["elements"][0]["nodes"][1] = snapshot["elements"][0]["nodes"][0]
        with self.assertRaisesRegex(ValueError, "duplicate node IDs"):
            mesh_audit.audit_snapshot(snapshot)

        snapshot = _one_hex_snapshot()
        snapshot["elements"][0]["nodes"][1] = 99
        with self.assertRaisesRegex(ValueError, "unknown node IDs"):
            mesh_audit.audit_snapshot(snapshot)

        snapshot = _one_hex_snapshot()
        snapshot["nodes"][0]["xyz"][0] = math.inf
        with self.assertRaisesRegex(ValueError, "finite"):
            mesh_audit.audit_snapshot(snapshot)

    def test_budget_limit_is_explicit(self):
        with patch.object(mesh_audit, "MAX_NODES", 0):
            with self.assertRaisesRegex(ValueError, "MAX_NODES=0"):
                mesh_audit.audit_snapshot(_one_hex_snapshot())


if __name__ == "__main__":
    unittest.main()
