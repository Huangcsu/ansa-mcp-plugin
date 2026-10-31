"""Known polygonal ring fixtures for the offline annular source proof."""

import copy
import math
import unittest

from ansa_mcp_bridge.operations.annular_source import audit


def ring(sectors=16, radii=(1.0, 1.5, 2.0), phase=0.0):
    nodes = []
    for radius in radii:
        for index in range(sectors):
            angle = phase + math.tau * index / sectors
            nodes.append({"id": len(nodes) + 1,
                          "xyz": [radius * math.cos(angle), radius * math.sin(angle), 0.0]})
    elements = []
    for band in range(len(radii) - 1):
        for index in range(sectors):
            following = (index + 1) % sectors
            elements.append({
                "id": len(elements) + 1, "type": "CQUAD4",
                "nodes": [band * sectors + index + 1, (band + 1) * sectors + index + 1,
                          (band + 1) * sectors + following + 1, band * sectors + following + 1],
                "property_id": 10, "block_id": None,
            })
    return {"deck": 2, "complete": True, "nodes": nodes, "elements": elements,
            "snapshot_sha256": "a" * 64}


def mixed_ring():
    source = ring(sectors=15, radii=tuple(1.0 + index / 7 for index in range(8)))
    for offset, element_index in enumerate((0, 3, 6, 9, 12)):
        quad = source["elements"][element_index]
        a, b, c, d = quad["nodes"]
        source["elements"][element_index] = {**quad, "type": "CTRIA3", "nodes": [a, b, c]}
        source["elements"].append({
            "id": 106 + offset, "type": "CTRIA3", "nodes": [a, c, d],
            "property_id": 10, "block_id": None,
        })
    return source


def options(**changes):
    return {"center": [0, 0, 0], "axis": [0, 0, 1], "inner_radius": 1.0,
            "outer_radius": 2.0, "max_chord_error": 0.05, "tolerance": 1e-7, **changes}


class AnnularSourceTests(unittest.TestCase):
    def test_fill_budget_opt_in_does_not_expand_default_public_source_budget(self):
        source = ring(sectors=100, radii=(1., 1.3, 1.6, 2.))
        with self.assertRaises(ValueError):
            audit(source, options())
        self.assertEqual(audit(source, options(), max_elements=400)['status'], 'verified_mesh_template')
        with self.assertRaises(ValueError):
            audit(ring(sectors=201), options(), max_elements=400)

    def test_full_quad_annulus_and_translated_rotated_plane(self):
        source = ring()
        original = copy.deepcopy(source)
        result = audit(source, options())
        self.assertEqual(result["status"], "verified_mesh_template")
        self.assertFalse(result["usable_for_mutation"])
        self.assertFalse(result["original_CAD_coverage_proven"])
        self.assertEqual(result["source_snapshot_sha256"], source["snapshot_sha256"])
        self.assertEqual(result["metrics"]["euler_characteristic"], 0)
        self.assertAlmostEqual(result["metrics"]["element_area_sum"],
                               result["metrics"]["annular_polygon_area"])
        self.assertAlmostEqual(result["boundaries"]["inner"]["angular_sweep_radians"], math.tau)
        self.assertAlmostEqual(result["boundaries"]["outer"]["maximum_chord_error"],
                               2 * (1 - math.cos(math.pi / 16)))
        self.assertEqual(source, original)

        for node in source["nodes"]:
            x, y, _ = node["xyz"]
            node["xyz"] = [5, x + 6, y + 7]
        rotated = audit(source, options(center=[5, 6, 7], axis=[10, 0, 0]))
        self.assertEqual(rotated["status"], "verified_mesh_template")
        reversed_axis = audit(original, options(axis=[0, 0, -1]))
        self.assertEqual(reversed_axis["status"], "verified_mesh_template")

    def test_mixed_quad_triangle_annulus(self):
        source = mixed_ring()
        result = audit(source, options())

        self.assertEqual(result["status"], "verified_mesh_template")
        self.assertFalse(result["usable_for_mutation"])
        self.assertFalse(result["original_CAD_coverage_proven"])
        self.assertEqual(sum(row["type"] == "CQUAD4" for row in source["elements"]), 100)
        self.assertEqual(sum(row["type"] == "CTRIA3" for row in source["elements"]), 10)
        self.assertEqual(result["metrics"]["euler_characteristic"], 0)
        self.assertEqual(result["metrics"]["boundary_edge_count"], 30)
        self.assertAlmostEqual(result["metrics"]["element_area_sum"],
                               result["metrics"]["annular_polygon_area"])
        self.assertAlmostEqual(result["boundaries"]["inner"]["radius"], 1.0)
        self.assertAlmostEqual(result["boundaries"]["outer"]["radius"], 2.0)
        self.assertLessEqual(result["boundaries"]["inner"]["maximum_chord_error"],
                             options()["max_chord_error"])
        self.assertLessEqual(result["boundaries"]["outer"]["maximum_chord_error"],
                             options()["max_chord_error"])

    def test_mixed_source_rejects_inverted_triangles_duplicate_node_identity_and_other_types(self):
        inverted = mixed_ring()
        next(row for row in inverted["elements"] if row["type"] == "CTRIA3")["nodes"].reverse()

        duplicate_identity = mixed_ring()
        triangles = [row for row in duplicate_identity["elements"] if row["type"] == "CTRIA3"]
        first, second = next((first, second) for index, first in enumerate(triangles)
                             for second in triangles[index + 1:]
                             if len(set(first["nodes"]) & set(second["nodes"])) == 2)
        shared = set(first["nodes"]) & set(second["nodes"])
        original_id = min(shared)
        duplicate_id = max(node["id"] for node in duplicate_identity["nodes"]) + 1
        original_node = next(node for node in duplicate_identity["nodes"] if node["id"] == original_id)
        duplicate_identity["nodes"].append({"id": duplicate_id, "xyz": list(original_node["xyz"])})
        second["nodes"] = [duplicate_id if ident == original_id else ident
                           for ident in second["nodes"]]

        unsupported_type = mixed_ring()
        next(row for row in unsupported_type["elements"] if row["type"] == "CTRIA3")["type"] = "CTRIA6"

        for name, source in (("inverted triangle", inverted),
                             ("duplicate node identity", duplicate_identity),
                             ("unsupported element type", unsupported_type)):
            with self.subTest(case=name):
                result = audit(source, options())
                self.assertEqual(result["status"], "unsupported")
                self.assertFalse(result["usable_for_mutation"])
                self.assertTrue(result["reason"])

    def test_missing_strip_filled_hole_overlapping_or_inverted_cells(self):
        missing = ring()
        missing["elements"] = [row for index, row in enumerate(missing["elements"])
                               if index not in (0, 16)]
        filled = ring(sectors=4, radii=(1.0, 2.0))
        filled["elements"].append({"id": 5, "type": "CQUAD4", "nodes": [1, 2, 3, 4]})
        overlapping = ring()
        duplicate = copy.deepcopy(overlapping["elements"][0])
        duplicate["id"] = 100
        overlapping["elements"].append(duplicate)
        inverted = ring()
        inverted["elements"][0]["nodes"].reverse()
        for name, source, opts in (
                ("missing strip", missing, options()),
                ("filled hole", filled, options(max_chord_error=1.0)),
                ("overlapping cell", overlapping, options()),
                ("inverted cell", inverted, options())):
            with self.subTest(case=name):
                result = audit(source, opts)
                self.assertEqual(result["status"], "unsupported")
                self.assertTrue(result["reason"])

    def test_different_ids_at_coincident_coordinates_are_rejected(self):
        source = ring()
        source["nodes"].append({"id": 1000, "xyz": list(source["nodes"][0]["xyz"])})
        source["elements"][0]["nodes"][0] = 1000
        result = audit(source, options())
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("coincident", result["reason"])

    def test_chord_error_is_reported_and_enforced_for_each_ring(self):
        result = audit(ring(), options(max_chord_error=0.001))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("chord error", result["reason"])
        self.assertGreater(result["boundaries"]["inner"]["maximum_chord_error"], 0.001)
        self.assertGreater(result["boundaries"]["outer"]["maximum_chord_error"], 0.001)

    def test_wrong_axis_nonplanar_node_and_wrong_plane_center_are_unsupported(self):
        nonplanar = ring()
        nonplanar["nodes"][0]["xyz"][2] = 0.01
        for source, opts in ((ring(), options(axis=[1, 0, 0])),
                             (nonplanar, options()),
                             (ring(), options(center=[0, 0, 0.01]))):
            with self.subTest(options=opts):
                result = audit(source, opts)
                self.assertEqual(result["status"], "unsupported")
                self.assertIn("plane", result["reason"])

    def test_crossing_meshes_and_hanging_node_are_unsupported(self):
        crossing = ring()
        other = ring(phase=math.pi / 16)
        for node in other["nodes"]:
            node["id"] += 1000
        for element in other["elements"]:
            element["id"] += 1000
            element["nodes"] = [ident + 1000 for ident in element["nodes"]]
        crossing["nodes"].extend(other["nodes"])
        crossing["elements"].extend(other["elements"])
        result = audit(crossing, options())
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("intersect", result["reason"])

        hanging = ring()
        midpoint = [(hanging["nodes"][16]["xyz"][i] + hanging["nodes"][17]["xyz"][i]) / 2
                    for i in range(3)]
        hanging["nodes"].extend([
            {"id": 49, "xyz": [math.cos(math.pi / 16), math.sin(math.pi / 16), 0]},
            {"id": 50, "xyz": midpoint},
        ])
        hanging["elements"][0]["nodes"] = [1, 17, 50, 49]
        hanging["elements"].append({"id": 33, "type": "CQUAD4", "nodes": [49, 50, 18, 2]})
        result = audit(hanging, options())
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("hanging", result["reason"])

    def test_malformed_shapes_numbers_ids_references_and_options_raise_value_error(self):
        invalid_sources = []
        for mutation in (
                lambda s: s["nodes"][0]["xyz"].__setitem__(0, float("nan")),
                lambda s: s["nodes"][1].__setitem__("id", 1),
                lambda s: s["elements"][0]["nodes"].__setitem__(0, 9999),
                lambda s: s.__setitem__("snapshot_sha256", "missing"),
                lambda s: s.__setitem__("elements", s["elements"] * 7)):
            source = ring()
            mutation(source)
            invalid_sources.append(source)
        for source in invalid_sources:
            with self.subTest(source=source["snapshot_sha256"]), self.assertRaises(ValueError):
                audit(source, options())
        for opts in (options(extra=True), options(tolerance=0.002), options(axis=[0, 0, 0]),
                     options(center=[0, 0]), options(outer_radius=0.5)):
            with self.subTest(options=opts), self.assertRaises(ValueError):
                audit(ring(), opts)


if __name__ == "__main__":
    unittest.main()
