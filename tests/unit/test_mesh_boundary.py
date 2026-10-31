import json
import math
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import mesh_audit
from ansa_mcp_bridge.operations.mesh_audit import _FACE_TABLE
from ansa_mcp_bridge.operations.mesh_boundary import audit_boundary_membership


HEX_NODES = list(range(1, 9))
HEX_FACETS = [
    [1, 4, 3, 2],
    [5, 6, 7, 8],
    [1, 2, 6, 5],
    [2, 3, 7, 6],
    [3, 4, 8, 7],
    [4, 1, 5, 8],
]


def _snapshot(elements, *, coordinate_overrides=None, node_ids=None, complete=True):
    if node_ids is None:
        node_ids = sorted({node for element in elements for node in element["nodes"]})
    coordinate_overrides = coordinate_overrides or {}
    return {
        "deck": 1,
        "complete": complete,
        "nodes": [
            {"id": node_id, "xyz": coordinate_overrides.get(node_id, [float(node_id), 0.0, 0.0])}
            for node_id in node_ids
        ],
        "elements": elements,
        "integrity": {
            "complete": complete,
            "missing_node_ids": [],
        },
    }


def _hex_snapshot(*, coordinate_overrides=None):
    return _snapshot(
        [{"id": 101, "type": "CHEXA", "nodes": HEX_NODES}],
        coordinate_overrides=coordinate_overrides,
    )


def _face_records(facets):
    return [
        {
            "face_id": 100 + index,
            "shells": [{
                "id": 1000 + index,
                "type": "CQUAD4" if len(nodes) == 4 else "CTRIA3",
                "nodes": list(nodes),
            }],
            "declared_shell_count": 1,
            "membership_complete": True,
        }
        for index, nodes in enumerate(facets)
    ]


class MeshBoundaryTests(unittest.TestCase):
    def test_outlier_coordinate_does_not_change_node_id_membership(self):
        records = _face_records(HEX_FACETS)
        records[0].update({
            "macro_face_ids": [7],
            "macro_id": 7,
            "macro_shell_ids": [1000],
            "cad_area": 12.5,
            "membership_origin": "native_macro_membership",
        })
        result = audit_boundary_membership(
            _hex_snapshot(coordinate_overrides={1: [1_000_000.0, -1_000_000.0, 5_000_000.0]}),
            records,
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["summary"]["membership_basis"], "exact_node_id_set")
        self.assertEqual(result["summary"]["mapped_shell_facet_count"], 6)
        self.assertEqual(result["unassigned_free_facets"], [])
        self.assertEqual(result["faces"][0]["source_metadata"]["macro_id"], 7)
        self.assertEqual(result["faces"][0]["source_metadata"]["cad_area"], 12.5)
        self.assertEqual(
            set(result),
            {
                "schema_version", "status", "summary", "faces", "expected_boundary",
                "unassigned_free_facets", "duplicate_face_assignments",
                "not_free_shell_facets", "diagnostics",
            },
        )
        json.dumps(result, allow_nan=False)

    def test_unmeshed_unknown_membership_and_missing_nodes_are_incomplete(self):
        unmeshed_records = _face_records(HEX_FACETS)
        unmeshed_records[0].update(shells=[], declared_shell_count=0)
        unmeshed = audit_boundary_membership(_hex_snapshot(), unmeshed_records)
        self.assertEqual(unmeshed["status"], "incomplete")
        self.assertIn("face_unmeshed", {item["code"] for item in unmeshed["diagnostics"]})

        unknown_records = _face_records(HEX_FACETS)
        unknown_records[0]["declared_shell_count"] = None
        unknown_records[0]["membership_complete"] = None
        unknown = audit_boundary_membership(_hex_snapshot(), unknown_records)
        self.assertEqual(unknown["status"], "incomplete")
        self.assertIn("declared_shell_count_unknown", {item["code"] for item in unknown["diagnostics"]})
        self.assertIn("face_membership_incomplete", {item["code"] for item in unknown["diagnostics"]})

        missing_node_records = _face_records(HEX_FACETS)
        missing_node_records[0]["shells"][0]["nodes"] = [1, 4, 3, 99]
        missing_node = audit_boundary_membership(_hex_snapshot(), missing_node_records)
        shell = missing_node["faces"][0]["shells"][0]
        self.assertEqual(missing_node["status"], "incomplete")
        self.assertEqual(shell["nodes"], [1, 4, 3, 99])
        self.assertEqual(shell["facet"]["mapping_status"], "missing_nodes")
        self.assertEqual(shell["facet"]["missing_node_ids"], [99])
        self.assertIn([1, 4, 3, 99], missing_node["expected_boundary"])

        # The adapter may add CAD GRID coordinates to the snapshot. Such nodes
        # still cannot map unless a solid element actually references their IDs.
        augmented_snapshot = _snapshot(
            [{"id": 101, "type": "CHEXA", "nodes": HEX_NODES}],
            node_ids=HEX_NODES + [99],
        )
        augmented = audit_boundary_membership(augmented_snapshot, missing_node_records)
        augmented_shell = augmented["faces"][0]["shells"][0]
        self.assertEqual(augmented["status"], "incomplete")
        self.assertEqual(augmented_shell["facet"]["mapping_status"], "unmapped_nodes")
        self.assertEqual(augmented_shell["facet"]["missing_node_ids"], [])
        self.assertEqual(augmented_shell["facet"]["missing_solid_node_ids"], [99])

    def test_missing_face_inventory_is_incomplete(self):
        result = audit_boundary_membership(_hex_snapshot(), None)

        self.assertEqual(result["status"], "incomplete")
        self.assertIn("face_records_missing", {item["code"] for item in result["diagnostics"]})

    def test_unassigned_solid_free_facet_fails_full_boundary_audit(self):
        result = audit_boundary_membership(_hex_snapshot(), _face_records(HEX_FACETS[:-1]))

        self.assertEqual(result["status"], "fail")
        self.assertEqual(len(result["unassigned_free_facets"]), 1)
        self.assertIn("unassigned_solid_free_facets", {item["code"] for item in result["diagnostics"]})

    def test_shell_mapped_to_interior_solid_facet_fails(self):
        elements = [
            {"id": 101, "type": "CHEXA", "nodes": list(range(1, 9))},
            {"id": 102, "type": "CHEXA", "nodes": [3, 9, 10, 2, 7, 11, 12, 6]},
        ]
        snapshot = _snapshot(elements, node_ids=list(range(1, 13)))
        records = _face_records([[2, 3, 7, 6]])

        result = audit_boundary_membership(snapshot, records, require_all_boundary=False)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["not_free_shell_facets"][0]["solid_use_count"], 2)
        self.assertEqual(result["faces"][0]["shells"][0]["facet"]["mapping_status"], "not_free")

    def test_same_facet_assigned_to_multiple_faces_fails(self):
        records = _face_records(HEX_FACETS)
        records.append({
            "face_id": 999,
            "shells": [{"id": 9999, "type": "CQUAD4", "nodes": HEX_FACETS[0]}],
            "declared_shell_count": 1,
            "membership_complete": True,
        })

        result = audit_boundary_membership(_hex_snapshot(), records)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(len(result["duplicate_face_assignments"]), 1)
        self.assertEqual(
            {item["code"] for item in result["diagnostics"]} & {"facet_assigned_to_multiple_faces"},
            {"facet_assigned_to_multiple_faces"},
        )

    def test_quad_is_not_stitched_from_two_free_triangle_facets(self):
        elements = [
            {"id": 201, "type": "CPENTA", "nodes": [1, 2, 3, 5, 6, 7]},
            {"id": 202, "type": "CPENTA", "nodes": [1, 3, 4, 8, 9, 10]},
        ]
        snapshot = _snapshot(elements, node_ids=list(range(1, 11)))
        records = _face_records([[1, 2, 3, 4]])

        result = audit_boundary_membership(snapshot, records, require_all_boundary=False)

        self.assertEqual(result["status"], "fail")
        self.assertIn("quad_shell_split_into_triangles", {item["code"] for item in result["diagnostics"]})
        diagnostic = next(
            item for item in result["diagnostics"] if item["code"] == "quad_shell_split_into_triangles"
        )
        self.assertEqual(len(diagnostic["details"]["matching_free_triangles"]), 2)

    def test_mixed_chexa_cpenta_surfaces_are_matched_by_node_ids(self):
        elements = [
            {"id": 301, "type": "CHEXA", "nodes": list(range(1, 9))},
            {"id": 302, "type": "CPENTA", "nodes": list(range(9, 15))},
        ]
        facets = []
        for element in elements:
            facets.extend(
                [[element["nodes"][position] for position in face]
                 for face in _FACE_TABLE[element["type"]]]
            )
        snapshot = _snapshot(elements, node_ids=list(range(1, 15)))

        result = audit_boundary_membership(snapshot, _face_records(facets))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["summary"]["solid_free_facet_count"], 11)
        self.assertEqual(result["summary"]["expected_boundary_facet_count"], 11)

    def test_unknown_topology_is_incomplete(self):
        element = {"id": 401, "type": "CHEXA20", "nodes": list(range(1, 21))}
        snapshot = _snapshot([element], node_ids=list(range(1, 21)))
        result = audit_boundary_membership(snapshot, _face_records([[1, 2, 3, 4]]))

        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unknown_solid_topology", {item["code"] for item in result["diagnostics"]})

    def test_shell_and_snapshot_validation_and_budgets_are_strict(self):
        malformed = _face_records([[1, 4, 3, 2]])
        del malformed[0]["shells"][0]["id"]
        with self.assertRaisesRegex(ValueError, "positive integer"):
            audit_boundary_membership(_hex_snapshot(), malformed, require_all_boundary=False)

        repeated_node = _face_records([[1, 4, 3, 2]])
        repeated_node[0]["shells"][0]["nodes"] = [1, 4, 3, 1]
        with self.assertRaisesRegex(ValueError, "duplicate node IDs"):
            audit_boundary_membership(_hex_snapshot(), repeated_node, require_all_boundary=False)

        repeated_shell_id = _face_records([[1, 4, 3, 2], [5, 6, 7, 8]])
        repeated_shell_id[0]["shells"].append(dict(repeated_shell_id[0]["shells"][0]))
        repeated_shell_id[0]["declared_shell_count"] = 2
        with self.assertRaisesRegex(ValueError, "duplicate shell id"):
            audit_boundary_membership(_hex_snapshot(), repeated_shell_id, require_all_boundary=False)

        nonfinite = _hex_snapshot()
        nonfinite["nodes"][0]["xyz"][0] = math.inf
        with self.assertRaisesRegex(ValueError, "finite"):
            audit_boundary_membership(nonfinite, _face_records(HEX_FACETS))

        nonfinite_area = _face_records([[1, 4, 3, 2]])
        nonfinite_area[0]["cad_area"] = math.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            audit_boundary_membership(_hex_snapshot(), nonfinite_area, require_all_boundary=False)

        with patch("ansa_mcp_bridge.operations.mesh_boundary.MAX_FACE_RECORDS", 0):
            with self.assertRaisesRegex(ValueError, "MAX_FACE_RECORDS=0"):
                audit_boundary_membership(_hex_snapshot(), _face_records([[1, 4, 3, 2]]))
        with patch("ansa_mcp_bridge.operations.mesh_boundary.MAX_SHELLS", 0):
            with self.assertRaisesRegex(ValueError, "MAX_SHELLS=0"):
                audit_boundary_membership(_hex_snapshot(), _face_records([[1, 4, 3, 2]]))
        with patch("ansa_mcp_bridge.operations.mesh_boundary.MAX_FACET_NODE_REFERENCES", 3):
            with self.assertRaisesRegex(ValueError, "MAX_FACET_NODE_REFERENCES=3"):
                audit_boundary_membership(_hex_snapshot(), _face_records([[1, 4, 3, 2]]))
        with patch.object(mesh_audit, "MAX_ELEMENTS", 0):
            with self.assertRaisesRegex(ValueError, "MAX_ELEMENTS=0"):
                audit_boundary_membership(_hex_snapshot(), _face_records(HEX_FACETS))


if __name__ == "__main__":
    unittest.main()
