import unittest

from ansa_mcp_bridge.operations.solid_overlap import audit_solid_overlaps


def _hex_points(low, high):
    x0, y0, z0 = low
    x1, y1, z1 = high
    return [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]


def _snapshot(cells):
    nodes_by_id = {}
    elements = []
    for element_id, element_type, node_ids, points in cells:
        for node_id, point in zip(node_ids, points):
            if node_id in nodes_by_id and nodes_by_id[node_id] != point:
                raise AssertionError(f"fixture node {node_id} has inconsistent coordinates")
            nodes_by_id[node_id] = point
        elements.append({"id": element_id, "type": element_type, "nodes": list(node_ids)})
    nodes = [{"id": node_id, "xyz": list(nodes_by_id[node_id])} for node_id in sorted(nodes_by_id)]
    return nodes, elements


def _two_hexes(second_low, second_high, *, shared_origin=False):
    first_points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    second_points = _hex_points(second_low, second_high)
    first_ids = list(range(1, 9))
    second_ids = list(range(9, 17))
    if shared_origin:
        second_ids[0] = 1
        second_points = _hex_points((0.0, 0.0, 0.0), (2.0, 0.5, 2.0))
    return _snapshot([
        (101, "CHEXA8", first_ids, first_points),
        (202, "CHEXA8", second_ids, second_points),
    ])


class SolidOverlapTests(unittest.TestCase):
    def test_aabb_separated_solids_pass(self):
        nodes, elements = _two_hexes((2.0, 0.0, 0.0), (3.0, 1.0, 1.0))
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["summary"]["broadphase_separated_pair_count"], 1)
        self.assertEqual(result["summary"]["untested_pair_count"], 0)

    def test_shared_face_is_contact_not_overlap(self):
        first_points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        second_points = _hex_points((1.0, 0.0, 0.0), (2.0, 1.0, 1.0))
        nodes, elements = _snapshot([
            (101, "CHEXA", list(range(1, 9)), first_points),
            (202, "CHEXA", [2, 9, 10, 3, 6, 11, 12, 7], second_points),
        ])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["summary"]["contact_or_below_tolerance_pair_count"], 1)
        self.assertEqual(result["pairs"][0]["status"], "contact_or_below_tolerance")

    def test_partial_overlap_fails(self):
        nodes, elements = _two_hexes((0.5, 0.0, 0.0), (1.5, 1.0, 1.0))
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["pairs"][0]["element_ids"], [101, 202])
        self.assertEqual(result["pairs"][0]["status"], "overlap")
        self.assertAlmostEqual(result["pairs"][0]["minimum_axis_overlap"], 0.5)

    def test_containment_is_detected_as_overlap(self):
        nodes, elements = _two_hexes((0.25, 0.25, 0.25), (0.75, 0.75, 0.75))
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["summary"]["overlap_pair_count"], 1)

    def test_independent_duplicate_solids_are_detected(self):
        points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        nodes, elements = _snapshot([
            (101, "CHEXA8", list(range(1, 9)), points),
            (202, "CHEXA8", list(range(9, 17)), points),
        ])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["summary"]["overlap_pair_count"], 1)

    def test_shared_node_does_not_hide_positive_volume_overlap(self):
        nodes, elements = _two_hexes((-0.5, -0.5, -0.5), (0.5, 0.5, 0.5), shared_origin=True)
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["summary"]["overlap_pair_count"], 1)

    def test_penetration_within_tolerance_is_not_called_exact_zero(self):
        nodes, elements = _two_hexes((1.0 - 5.0e-5, 0.0, 0.0), (2.0 - 5.0e-5, 1.0, 1.0))
        result = audit_solid_overlaps(nodes, elements, tolerance=1.0e-4)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["pairs"][0]["status"], "contact_or_below_tolerance")
        self.assertIn("does not assert exact zero", result["tolerance"]["semantics"])

    def test_warped_pair_is_incomplete_when_hulls_do_not_separate(self):
        warped = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        warped[6] = (1.0, 1.0, 1.2)
        regular = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        nodes, elements = _snapshot([
            (101, "CHEXA8", list(range(1, 9)), warped),
            (202, "CHEXA8", list(range(9, 17)), regular),
        ])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["summary"]["invalid_geometry_element_count"], 1)
        self.assertEqual(result["summary"]["convex_hull_checked_pair_count"], 1)
        self.assertEqual(result["summary"]["unresolved_pair_count"], 1)

    def test_degenerate_solid_is_incomplete(self):
        flat_points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        nodes, elements = _snapshot([(101, "CHEXA8", list(range(1, 9)), flat_points)])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["summary"]["invalid_geometry_element_count"], 1)
        self.assertEqual(result["summary"]["unresolved_element_count"], 1)

    def test_small_shared_face_warp_below_geometry_epsilon_is_not_false_overlap(self):
        lower = _hex_points((0.0,0.0,0.0),(1000.0,1000.0,1000.0))
        upper = _hex_points((0.0,0.0,1000.0),(1000.0,1000.0,2000.0))
        lower[6] = (1000.0,1000.0,1000.0+5e-8)
        upper[2] = lower[6]
        nodes,elements = _snapshot([
            (101,"CHEXA8",list(range(1,9)),lower),
            (202,"CHEXA8",[5,6,7,8,9,10,11,12],upper),
        ])
        result = audit_solid_overlaps(nodes,elements)
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["summary"]["overlap_pair_count"],0)
        self.assertEqual(result["unresolved"][0]["reason"],"numerical_or_planarity_uncertainty_exceeds_overlap_margin")

    def test_large_exact_planar_shared_face_does_not_false_fail_from_projection_roundoff(self):
        size=1e9
        transform=lambda p:(p[0]-p[2],2*p[0]-p[1],p[2]-p[1])
        lower=[transform(p) for p in _hex_points((0,0,0),(size,size,size))]
        upper=[transform(p) for p in _hex_points((0,0,size),(size,size,2*size))]
        nodes,elements=_snapshot([(101,"CHEXA8",list(range(1,9)),lower),
                                  (202,"CHEXA8",list(range(9,17)),upper)])
        result=audit_solid_overlaps(nodes,elements)
        self.assertNotEqual(result["status"],"fail")
        self.assertEqual(result["summary"]["overlap_pair_count"],0)

    def test_large_thin_positive_overlap_does_not_false_pass_from_cancellation(self):
        size=2**49;h=2**-21
        def transform(p):
            u,v,w=p
            return (h*(-3*u-4*v+2*w),h*(-u+3*v-3*w),h*(-4*u-2*w))
        lower=[transform(p) for p in _hex_points((0,0,0),(size,size,size))]
        upper=[transform(p) for p in _hex_points((0,0,size-1),(size,size,2*size-1))]
        nodes,elements=_snapshot([(101,"CHEXA8",list(range(1,9)),lower),
                                  (202,"CHEXA8",list(range(9,17)),upper)])
        result=audit_solid_overlaps(nodes,elements)
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["summary"]["contact_or_below_tolerance_pair_count"],0)


    def test_hex_penta_mixed_pair_shared_face_is_contact(self):
        hex_points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        penta_points = [
            (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (1.0, 0.0, 1.0),
            (2.0, 0.0, 0.0), (2.0, 1.0, 0.0), (2.0, 0.0, 1.0),
        ]
        nodes, elements = _snapshot([
            (101, "CHEXA8", list(range(1, 9)), hex_points),
            (202, "CPENTA6", [2, 3, 6, 9, 10, 11], penta_points),
        ])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["summary"]["supported_element_count"], 2)
        self.assertEqual(result["summary"]["contact_or_below_tolerance_pair_count"], 1)

    def test_invalid_ids_and_topology_fail_closed(self):
        nodes, elements = _two_hexes((2.0, 0.0, 0.0), (3.0, 1.0, 1.0))
        duplicate_connectivity = [
            {**elements[0], "nodes": list(elements[0]["nodes"])},
            {**elements[1], "nodes": list(elements[1]["nodes"])},
        ]
        duplicate_connectivity[0]["nodes"][1] = duplicate_connectivity[0]["nodes"][0]
        with self.assertRaisesRegex(ValueError, "duplicate node IDs"):
            audit_solid_overlaps(nodes, duplicate_connectivity)

        missing_reference = [{**elements[0], "nodes": list(elements[0]["nodes"])}]
        missing_reference[0]["nodes"] = list(missing_reference[0]["nodes"])
        missing_reference[0]["nodes"][0] = 999
        with self.assertRaisesRegex(ValueError, "unknown node IDs"):
            audit_solid_overlaps(nodes, missing_reference)

        duplicate_element_id = [
            {**elements[0], "nodes": list(elements[0]["nodes"])},
            {**elements[1], "nodes": list(elements[1]["nodes"])},
        ]
        duplicate_element_id[1]["id"] = duplicate_element_id[0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate element id"):
            audit_solid_overlaps(nodes, duplicate_element_id)

    def test_unsupported_element_is_counted_and_incomplete(self):
        points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))[:4]
        nodes, elements = _snapshot([(101, "CTETRA4", [1, 2, 3, 4], points)])
        result = audit_solid_overlaps(nodes, elements)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["summary"]["unsupported_element_count"], 1)
        self.assertEqual(result["unresolved"][0]["kind"], "element")

    def test_candidate_budget_reports_untested_pairs(self):
        nodes, elements = _two_hexes((0.5, 0.0, 0.0), (1.5, 1.0, 1.0))
        result = audit_solid_overlaps(nodes, elements, max_candidate_pairs=0)
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["limits"]["candidate_budget_exceeded"])
        self.assertEqual(result["summary"]["candidate_pair_count"], 1)
        self.assertEqual(result["summary"]["untested_pair_count"], 1)
        self.assertEqual(result["summary"]["overlap_pair_count"], 0)

    def test_detail_limit_preserves_total_finding_count(self):
        points = _hex_points((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        nodes, elements = _snapshot([
            (101, "CHEXA8", list(range(1, 9)), points),
            (202, "CHEXA8", list(range(9, 17)), points),
            (303, "CHEXA8", list(range(17, 25)), points),
        ])
        result = audit_solid_overlaps(nodes, elements, max_detail_rows=1)
        self.assertEqual(result["summary"]["overlap_pair_count"], 3)
        self.assertEqual(len(result["pairs"]), 1)
        self.assertTrue(result["summary"]["details_truncated"])


if __name__ == "__main__":
    unittest.main()
