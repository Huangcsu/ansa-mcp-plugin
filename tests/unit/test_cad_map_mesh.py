import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations.cad_map_mesh import verify_translation


def _source_two_quads():
    points = {
        1: (0.0, 0.0, 0.0), 2: (1.0, 0.0, 0.0), 3: (2.0, 0.0, 0.0),
        4: (0.0, 1.0, 0.0), 5: (1.0, 1.0, 0.0), 6: (2.0, 1.0, 0.0),
    }
    return {
        "nodes": [{"id": node_id, "xyz": list(point)} for node_id, point in points.items()],
        "elements": [
            {"id": 10, "type": "cquad4", "nodes": [1, 2, 5, 4], "property_id": 7},
            {"id": 11, "type": " CQUAD4 ", "nodes": [2, 3, 6, 5], "property_id": 7},
        ],
    }


def _translated_output(source, steps):
    source_coords = {row["id"]: row["xyz"] for row in source["nodes"]}
    output_nodes = [
        {"id": row["id"], "xyz": row["xyz"][:]}
        for row in source["nodes"]
    ]
    layer_ids = {0: {node_id: node_id for node_id in source_coords}}
    for layer in range(1, steps + 1):
        layer_ids[layer] = {}
        for node_id, point in source_coords.items():
            output_id = 1000 + layer * 100 + node_id
            layer_ids[layer][node_id] = output_id
            output_nodes.append({
                "id": output_id,
                "xyz": [point[0], point[1], point[2] + layer],
            })

    output_elements = []
    for face_index, face in enumerate(source["elements"]):
        for layer in range(1, steps + 1):
            connectivity = [layer_ids[layer - 1][node] for node in face["nodes"]]
            connectivity.extend(layer_ids[layer][node] for node in face["nodes"])
            output_elements.append({
                "id": 500 + face_index * steps + layer,
                "type": "CHEXA",
                "nodes": connectivity,
            })
    return {"nodes": output_nodes, "elements": output_elements}, layer_ids


def _first_quad_source():
    source = _source_two_quads()
    source["elements"] = source["elements"][:1]
    used_ids = set(source["elements"][0]["nodes"])
    source["nodes"] = [row for row in source["nodes"] if row["id"] in used_ids]
    return source


class CadMapMeshTests(unittest.TestCase):
    def test_translation_maps_reused_target_and_shared_sidewall_nodes(self):
        source = _source_two_quads()
        output, layer_ids = _translated_output(source, steps=2)

        result = verify_translation(source, output, [0.0, 0.0, 1.0], 2.0, 2)

        self.assertEqual(len(result["node_layer_pairs"]), 18)
        self.assertEqual(len(result["source_cell_matches"]), 4)
        self.assertAlmostEqual(result["total_volume"], 4.0)
        self.assertAlmostEqual(result["min_sampled_jacobian"], 0.125)
        self.assertEqual(result["max_coordinate_error"], 0.0)
        self.assertIn("does not prove positive Jacobian", result["jacobian_scope"])
        pairs = {
            (row["source_node_id"], row["layer"]): row["output_node_id"]
            for row in result["node_layer_pairs"]
        }
        self.assertEqual(pairs[(2, 1)], layer_ids[1][2])
        self.assertEqual(pairs[(2, 2)], layer_ids[2][2])
        self.assertEqual(pairs[(5, 1)], layer_ids[1][5])

    def test_rejects_hex_connectivity_that_skips_a_layer(self):
        source = _first_quad_source()
        output, layer_ids = _translated_output(source, steps=2)
        output["elements"][0]["nodes"] = [
            *[layer_ids[0][node] for node in source["elements"][0]["nodes"]],
            *[layer_ids[2][node] for node in source["elements"][0]["nodes"]],
        ]

        with self.assertRaisesRegex(ValueError, "adjacent translation layers"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 2.0, 2)

    def test_accepts_valid_hex_with_upper_face_listed_first(self):
        source = _first_quad_source()
        output, layer_ids = _translated_output(source, steps=1)
        face = source["elements"][0]["nodes"]
        reversed_cycle = [face[0], face[3], face[2], face[1]]
        output["elements"][0]["nodes"] = [
            *[layer_ids[1][node] for node in reversed_cycle],
            *[layer_ids[0][node] for node in reversed_cycle],
        ]

        result = verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

        self.assertAlmostEqual(result["total_volume"], 1.0)

    def test_rejects_duplicate_cell_connectivity(self):
        source = _first_quad_source()
        output, _ = _translated_output(source, steps=2)
        output["elements"][1]["nodes"] = output["elements"][0]["nodes"][:]

        with self.assertRaisesRegex(ValueError, "duplicate node connectivity"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 2.0, 2)

    def test_rejects_missing_cells_and_budget_overruns(self):
        source = _source_two_quads()
        output, _ = _translated_output(source, steps=1)
        incomplete = {
            "nodes": output["nodes"],
            "elements": output["elements"][:1],
        }
        with self.assertRaisesRegex(ValueError, "missing or extra cells"):
            verify_translation(source, incomplete, [0.0, 0.0, 1.0], 1.0, 1)

        with patch("ansa_mcp_bridge.operations.cad_map_mesh._MAX_SOLIDS", 1):
            with self.assertRaisesRegex(ValueError, "solid budget"):
                verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)
        with patch("ansa_mcp_bridge.operations.cad_map_mesh._MAX_NODES", 10):
            with self.assertRaisesRegex(ValueError, "node budget"):
                verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

    def test_rejects_duplicate_coordinates_even_when_node_count_matches(self):
        source = _first_quad_source()
        output, _ = _translated_output(source, steps=1)
        output["nodes"][-1]["xyz"] = output["nodes"][-2]["xyz"][:]

        with self.assertRaisesRegex(ValueError, "duplicate or tolerance-ambiguous coordinates"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

    def test_rejects_inverted_hex_jacobian(self):
        source = _first_quad_source()
        output, layer_ids = _translated_output(source, steps=1)
        face_nodes = source["elements"][0]["nodes"]
        reversed_face = list(reversed(face_nodes))
        output["elements"][0]["nodes"] = [
            *reversed_face,
            *[layer_ids[1][node] for node in reversed_face],
        ]

        with self.assertRaisesRegex(ValueError, "Jacobian/volume validation"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

    def test_rejects_replacing_an_original_layer_zero_node_id(self):
        source = _first_quad_source()
        output, _ = _translated_output(source, steps=1)
        output["nodes"][0]["id"] = 9001
        output["elements"][0]["nodes"] = [
            9001 if node_id == 1 else node_id
            for node_id in output["elements"][0]["nodes"]
        ]

        with self.assertRaisesRegex(ValueError, "must retain its original node ID"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

    def test_rejects_source_coordinate_duplicates_and_invalid_direction(self):
        source = _source_two_quads()
        source["nodes"][1]["xyz"] = source["nodes"][0]["xyz"][:]
        output, _ = _translated_output(_source_two_quads(), steps=1)
        with self.assertRaisesRegex(ValueError, "duplicate or tolerance-ambiguous coordinates"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)

        with self.assertRaisesRegex(ValueError, "unit vector"):
            verify_translation(_source_two_quads(), _translated_output(_source_two_quads(), 1)[0],
                               [0.0, 0.0, 2.0], 1.0, 1)

    def test_rejects_extra_output_nodes(self):
        source = _first_quad_source()
        output, _ = _translated_output(source, steps=1)
        output["nodes"].append({"id": 9999, "xyz": [9.0, 9.0, 9.0]})

        with self.assertRaisesRegex(ValueError, "missing or extra nodes"):
            verify_translation(source, output, [0.0, 0.0, 1.0], 1.0, 1)


if __name__ == "__main__":
    unittest.main()
