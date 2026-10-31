import copy
import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ansa_mcp_bridge.operations.cad_sweep_topology import plan_translation


SOURCE_FACE = 11
TARGET_FACE = 12


def _dot(first, second):
    return sum(first[index] * second[index] for index in range(3))


def _cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _normalize(vector):
    length = math.sqrt(_dot(vector, vector))
    return tuple(component / length for component in vector)


def _add(first, second):
    return tuple(first[index] + second[index] for index in range(3))


def _scale(vector, factor):
    return tuple(component * factor for component in vector)


def _cube_witness(direction=(0.0, 0.0, 1.0),
                  half_width=1.0, half_height=1.5, distance=3.0):
    axis = _normalize(tuple(direction))
    seed = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.8 else (0.0, 1.0, 0.0)
    width_axis = _normalize(_cross(seed, axis))
    height_axis = _cross(axis, width_axis)

    source_ids = [101, 102, 103, 104]
    target_ids = [201, 202, 203, 204]
    signs = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    source_points = [
        _add(_scale(width_axis, half_width * width_sign),
             _scale(height_axis, half_height * height_sign))
        for width_sign, height_sign in signs
    ]
    target_points = [
        _add(point, _scale(axis, distance)) for point in source_points
    ]
    coordinates = dict(zip(source_ids + target_ids,
                           source_points + target_points))

    endpoint_map = {}
    for index in range(4):
        endpoint_map[1 + index] = (source_ids[index], source_ids[(index + 1) % 4])
        endpoint_map[5 + index] = (target_ids[index], target_ids[(index + 1) % 4])
        endpoint_map[9 + index] = (source_ids[index], target_ids[index])

    face_cons = {
        SOURCE_FACE: [1, 2, 3, 4],
        TARGET_FACE: [5, 6, 7, 8],
    }
    for index in range(4):
        face_cons[13 + index] = [
            1 + index,
            9 + (index + 1) % 4,
            5 + index,
            9 + index,
        ]
    cons_faces = {}
    for face_id, cons_ids in face_cons.items():
        for cons_id in cons_ids:
            cons_faces.setdefault(cons_id, []).append(face_id)

    boundaries = []
    for cons_id in sorted(endpoint_map):
        endpoints = endpoint_map[cons_id]
        adjacent_faces = sorted(cons_faces[cons_id])
        boundaries.append({
            "id": cons_id,
            "face_ids": adjacent_faces,
            "native_card_length": math.dist(
                coordinates[endpoints[0]], coordinates[endpoints[1]]),
            "native_card_min_radius": 1.0,
            "measurement_source": "CONS card; may be discretized, not an analytic classification",
            "endpoint_topology": {
                "status": "available",
                "hot_point_ids": sorted(endpoints),
                "orientation_basis": "ascending native HOT POINT ID, not native CONS parameter direction",
                "reason": None,
            },
        })

    geometry_vertices = [
        {
            "id": point_id,
            "position": list(coordinates[point_id]),
            "cons_ids": sorted(
                cons_id for cons_id, endpoints in endpoint_map.items()
                if point_id in endpoints
            ),
        }
        for point_id in sorted(coordinates)
    ]
    source_snapshot = "a" * 64
    return {
        "status": "captured",
        "feature_schema": 2,
        "source_scope": {"face_ids": sorted(face_cons), "curve_ids": []},
        "feature_context": {"database": "fixture.ansa", "units": "millimeter"},
        "face_boundaries": [
            {"face_id": face_id, "cons_ids": cons_ids}
            for face_id, cons_ids in sorted(face_cons.items())
        ],
        "parts": [{"type": "FACE", "id": face_id, "part_id": 9}
                  for face_id in sorted(face_cons)],
        "boundaries": boundaries,
        "vertices": geometry_vertices,
        "curve_lengths": [],
        "cad_export_sha256": "b" * 64,
        "source_snapshot_sha256": source_snapshot,
        "contours": {"status": "available"},
        "seed_plan": {"status": "not_requested"},
        "usable_for_mutation": False,
        "freshness": "scoped CAD export precision plus native topology and context",
        "summary": {"face_count": 6, "curve_count": 0, "cons_count": 12,
                    "vertex_count": 8},
        "limitations": [],
    }


def _set_hot_point(witness, point_id, xyz):
    for point in witness["vertices"]:
        if point["id"] == point_id:
            point["position"] = list(xyz)


class CadSweepTopologyTests(unittest.TestCase):
    def test_axis_aligned_box_returns_complete_template(self):
        result = plan_translation(
            _cube_witness(), SOURCE_FACE, TARGET_FACE, (0, 0, 2), 3)

        self.assertEqual(result["template"], "T1_planar_quad_straight_sweep")
        self.assertEqual(result["direction"], [0.0, 0.0, 1.0])
        self.assertEqual(result["source_loop"], [101, 102, 103, 104])
        self.assertEqual(result["target_loop"], [201, 202, 203, 204])
        self.assertEqual([pair["connector_cons_id"]
                          for pair in result["vertex_pairs"]], [9, 10, 11, 12])
        self.assertEqual(len(result["side_pairs"]), 4)
        self.assertEqual(len(result["planes"]), 6)
        self.assertAlmostEqual(result["source_area"], 6.0)
        self.assertAlmostEqual(result["expected_volume"], 18.0)
        self.assertFalse(result["cad_surface_geometry_verified"])
        self.assertEqual(
            [pair["connector_cons_ids"] for pair in result["side_pairs"]],
            [[9, 10], [10, 11], [11, 12], [12, 9]],
        )
        normals = {plane["face_id"]: plane["outward_normal"]
                   for plane in result["planes"]}
        self.assertEqual(normals[SOURCE_FACE], [0.0, 0.0, -1.0])
        self.assertEqual(normals[TARGET_FACE], [0.0, 0.0, 1.0])

    def test_rotated_translation_normalizes_direction(self):
        direction = (2.0, 4.0, 6.0)
        result = plan_translation(
            _cube_witness(direction=direction),
            SOURCE_FACE,
            TARGET_FACE,
            direction,
            3.0,
        )

        expected_direction = _normalize(direction)
        self.assertEqual(len(result["direction"]), 3)
        for actual, expected in zip(result["direction"], expected_direction):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(result["source_area"], 6.0)
        self.assertAlmostEqual(result["expected_volume"], 18.0)
        self.assertEqual(result["source_loop"], [101, 102, 103, 104])
        self.assertEqual(result["target_loop"], [201, 202, 203, 204])

    def test_missing_face_and_outside_adjacency_are_rejected(self):
        missing_face = _cube_witness()
        missing_face["face_boundaries"].pop()
        with self.assertRaisesRegex(ValueError, "exactly 6|exactly six"):
            plan_translation(missing_face, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

        outside_adjacency = _cube_witness()
        for boundary in outside_adjacency["boundaries"]:
            if boundary["id"] == 1:
                boundary["face_ids"][1] = 999
                break
        with self.assertRaisesRegex(ValueError, "outside|inconsistent"):
            plan_translation(outside_adjacency, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_geometry_feature_witness_scope_and_adjacency_are_checked(self):
        mismatched_scope = _cube_witness()
        mismatched_scope["source_scope"]["face_ids"][0] = 999
        with self.assertRaisesRegex(ValueError, "source_scope must match"):
            plan_translation(mismatched_scope, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

        mismatched_endpoint = _cube_witness()
        mismatched_endpoint["boundaries"][0][
            "endpoint_topology"]["hot_point_ids"][1] = 999
        with self.assertRaisesRegex(ValueError, "missing from witness.vertices"):
            plan_translation(mismatched_endpoint, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

        missing_incidence = _cube_witness()
        missing_incidence["vertices"][0]["cons_ids"].pop()
        with self.assertRaisesRegex(ValueError, "HOT POINT/CONS adjacency is inconsistent"):
            plan_translation(missing_incidence, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_extra_in_domain_hot_point_adjacency_is_rejected(self):
        witness = _cube_witness()
        witness["vertices"][0]["cons_ids"].append(2)
        with self.assertRaisesRegex(ValueError, "HOT POINT/CONS adjacency is inconsistent"):
            plan_translation(witness, SOURCE_FACE, TARGET_FACE, (0, 0, 1), 3)
        witness = _cube_witness()
        witness["vertices"][0]["cons_ids"].append(999)
        self.assertFalse(plan_translation(witness, SOURCE_FACE, TARGET_FACE,
                                          (0, 0, 1), 3)["cad_surface_geometry_verified"])

    def test_wrong_target_translation_is_rejected(self):
        witness = _cube_witness()
        for point_id in (201, 202, 203, 204):
            point = next(point for point in witness["vertices"]
                         if point["id"] == point_id)
            _set_hot_point(witness, point_id,
                           _add(point["position"], (0, 0, 0.25)))

        with self.assertRaisesRegex(ValueError, "translated target"):
            plan_translation(witness, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_missing_longitudinal_cons_is_rejected(self):
        witness = _cube_witness()
        witness["boundaries"] = [
            boundary for boundary in witness["boundaries"]
            if boundary["id"] != 9
        ]
        for face in witness["face_boundaries"]:
            face["cons_ids"] = [
                cons_id for cons_id in face["cons_ids"] if cons_id != 9
            ]

        with self.assertRaisesRegex(
                ValueError, "exactly|distinct|12|twelve"):
            plan_translation(witness, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_duplicate_hot_point_identity_is_rejected(self):
        witness = _cube_witness()
        witness["vertices"][0]["id"] = witness["vertices"][1]["id"]

        with self.assertRaisesRegex(ValueError, "duplicate HOT POINT IDs"):
            plan_translation(witness, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_concave_and_nonplanar_cap_corners_are_rejected(self):
        concave = _cube_witness()
        _set_hot_point(concave, 101, (0.0, 0.0, 0.0))
        _set_hot_point(concave, 201, (0.0, 0.0, 3.0))
        with self.assertRaisesRegex(ValueError, "concave|crossing|degenerate"):
            plan_translation(concave, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

        nonplanar = _cube_witness()
        _set_hot_point(nonplanar, 101, (-1.0, -1.5, 0.125))
        _set_hot_point(nonplanar, 201, (-1.0, -1.5, 3.125))
        with self.assertRaisesRegex(ValueError, "not planar"):
            plan_translation(nonplanar, SOURCE_FACE, TARGET_FACE,
                             (0, 0, 1), 3)

    def test_bool_nan_and_nonpositive_scalars_are_rejected(self):
        valid = _cube_witness()
        cases = [
            ({"direction": True}, "must be|not bool"),
            ({"direction": (0.0, float("nan"), 1.0)}, "finite"),
            ({"distance": False}, "not bool"),
            ({"distance": 0.0}, "positive"),
            ({"tolerance": True}, "not bool"),
            ({"tolerance": float("inf")}, "finite"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                args = {
                    "direction": (0.0, 0.0, 1.0),
                    "distance": 3.0,
                    "tolerance": 1e-7,
                }
                args.update(overrides)
                with self.assertRaisesRegex(ValueError, message):
                    plan_translation(
                        copy.deepcopy(valid), SOURCE_FACE, TARGET_FACE,
                        args["direction"], args["distance"], args["tolerance"])


if __name__ == "__main__":
    unittest.main()
