import math
import unittest

from ansa_mcp_bridge.operations.geometry_contours import build_contours


def _line(edge_id, first_vertex, second_vertex, start, end):
    return {"id": edge_id,
            "vertex_ids": [first_vertex, second_vertex],
            "analysis": {"status": "supported", "kind": "line",
                         "start": list(start), "end": list(end), "length": 1.0}}


def _arc(edge_id, first_vertex, second_vertex, start, end, sweep):
    return {"id": edge_id,
            "vertex_ids": [first_vertex, second_vertex],
            "analysis": {"status": "supported", "kind": "circular_arc",
                         "start": list(start), "end": list(end), "length": sweep,
                         "center": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0],
                         "radius": 1.0, "sweep_angle_radians": sweep}}


def _rectangle(edge_id=1, vertex_id=1, x_offset=0.0):
    points = [(x_offset, 0.0, 0.0), (x_offset + 1.0, 0.0, 0.0),
              (x_offset + 1.0, 1.0, 0.0), (x_offset, 1.0, 0.0)]
    edges = []
    for index in range(4):
        edges.append(_line(edge_id + index, vertex_id + index,
                           vertex_id + (index + 1) % 4,
                           points[index], points[(index + 1) % 4]))
    return edges


class GeometryContoursTests(unittest.TestCase):
    def test_rectangle_forms_a_closed_contour(self):
        result = build_contours([{"face_id": 10, "cons_ids": [1, 2, 3, 4]}],
                                _rectangle())

        self.assertEqual(result["status"], "complete")
        face = result["faces"][0]
        self.assertEqual(face["status"], "complete")
        self.assertEqual(face["geometry_status"], "complete")
        self.assertEqual(face["issues"], [])
        self.assertEqual(face["loops"], [{
            "cons_ids": [1, 2, 3, 4], "directions": [1, 1, 1, 1],
            "length": 4.0, "kind": "closed_contour"}])

    def test_two_semicircles_form_a_closed_circular_contour(self):
        edges = [_arc(1, 1, 2, (1, 0, 0), (-1, 0, 0), math.pi),
                 _arc(2, 2, 1, (-1, 0, 0), (1, 0, 0), math.pi)]

        loop = build_contours([{"face_id": 1, "cons_ids": [2, 1]}], edges)["faces"][0]["loops"][0]

        self.assertEqual(loop["cons_ids"], [1, 2])
        self.assertEqual(loop["directions"], [1, 1])
        self.assertEqual(loop["kind"], "closed_circular_contour")
        self.assertEqual(loop["center"], [0.0, 0.0, 0.0])
        self.assertEqual(loop["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(loop["radius"], 1.0)

    def test_single_full_circle_self_loop_is_recognized(self):
        edge = _arc(7, 9, 9, (1, 0, 0), (1, 0, 0), 2.0 * math.pi)

        result = build_contours([{"face_id": 3, "cons_ids": [7]}], [edge])

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["faces"][0]["loops"][0]["cons_ids"], [7])
        self.assertEqual(result["faces"][0]["loops"][0]["directions"], [1])
        self.assertEqual(result["faces"][0]["loops"][0]["kind"], "closed_circular_contour")

    def test_inconsistent_sweep_length_or_tiny_radius_cannot_claim_circle(self):
        # Both endpoints lie on the circle, and sweeps add to 2*pi, but the endpoints
        # are only 90 degrees apart: radius/endpoint checks alone would misclassify.
        malformed = [_arc(1, 1, 2, (1, 0, 0), (0, 1, 0), math.pi),
                     _arc(2, 2, 1, (0, 1, 0), (1, 0, 0), math.pi)]
        bad_length = [_arc(1, 1, 2, (1, 0, 0), (-1, 0, 0), math.pi),
                      _arc(2, 2, 1, (-1, 0, 0), (1, 0, 0), math.pi)]
        bad_length[0]['analysis']['length'] = 1.
        tiny = _arc(1, 1, 1, (0, 0, 0), (0, 0, 0), 2*math.pi)
        tiny['analysis'].update(radius=1e-10, length=2*math.pi*1e-10)
        bad_line = _rectangle()
        bad_line[0]['analysis']['length'] = 2.
        for edges in (malformed, bad_length, [tiny], bad_line):
            with self.subTest(edges=edges):
                face = build_contours([{'face_id':1,'cons_ids':[e['id'] for e in edges]}],edges)['faces'][0]
                self.assertEqual(face['status'], 'incomplete')
                self.assertEqual(face['geometry_status'], 'incomplete')
                self.assertEqual(face['loops'], [])

    def test_disconnected_loops_are_returned_independently(self):
        edges = _rectangle(1, 1) + _rectangle(5, 5, 3.0)

        face = build_contours([{"face_id": 4, "cons_ids": list(range(1, 9))}], edges)["faces"][0]

        self.assertEqual(face["status"], "complete")
        self.assertEqual([loop["cons_ids"] for loop in face["loops"]],
                         [[1, 2, 3, 4], [5, 6, 7, 8]])

    def test_shared_face_references_count_against_unique_edge_budget(self):
        edges = []
        for i in range(150):
            edges.extend(_rectangle(4 * i + 1, 4 * i + 1, 3. * i))
        # 300 unique edges referenced twice are legal, while 501 unique edges are not.
        selected = edges[:300]
        result = build_contours([{"face_id": 1, "cons_ids": list(range(1, 301))},
                                 {"face_id": 2, "cons_ids": list(range(1, 301))}], selected)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["faces"][0]["loops"]), 75)
        with self.assertRaisesRegex(ValueError, "unique FACE"):
            build_contours([{"face_id": 1, "cons_ids": list(range(1, 301))},
                            {"face_id": 2, "cons_ids": list(range(301, 502))}], [])

    def test_same_native_vertex_has_one_position_across_faces(self):
        edges = _rectangle() + _rectangle(5, 5, 10.)
        edges[4]['vertex_ids'][0] = 1
        edges[7]['vertex_ids'][1] = 1
        result = build_contours([{'face_id':1,'cons_ids':[1,2,3,4]},
                                 {'face_id':2,'cons_ids':[5,6,7,8]}], edges)
        self.assertEqual(result['status'], 'incomplete')
        for face in result['faces']:
            self.assertEqual(face['status'], 'incomplete')
            self.assertEqual(face['geometry_status'], 'incomplete')
            self.assertEqual(face['loops'], [])
            self.assertTrue(any('coordinates conflict' in issue for issue in face['issues']))

    def test_open_branch_duplicate_and_coincident_distinct_vertices_are_incomplete(self):
        cases = [
            ("open", [_line(1, 1, 2, (0, 0, 0), (1, 0, 0)),
                      _line(2, 2, 3, (1, 0, 0), (2, 0, 0))], [1, 2]),
            ("branch", [_line(1, 1, 2, (0, 0, 0), (1, 0, 0)),
                        _line(2, 1, 3, (0, 0, 0), (0, 1, 0)),
                        _line(3, 1, 4, (0, 0, 0), (-1, 0, 0))], [1, 2, 3]),
            ("duplicate", _rectangle(), [1, 1, 2, 3, 4]),
            ("native ids", [_line(1, 1, 2, (0, 0, 0), (1, 0, 0)),
                            _line(2, 3, 4, (1, 0, 0), (0, 0, 0))], [1, 2]),
        ]
        for label, edges, cons_ids in cases:
            with self.subTest(label=label):
                face = build_contours([{"face_id": 1, "cons_ids": cons_ids}], edges)["faces"][0]
                self.assertEqual(face["status"], "incomplete")
                self.assertEqual(face["loops"], [])
                self.assertTrue(face["issues"])

    def test_topology_only_unsupported_and_coordinate_conflicts_are_distinguished(self):
        topology_edges = _rectangle()
        for edge in topology_edges:
            analysis = edge["analysis"]
            edge["analysis"] = {"status": "topology_only", "kind": "native_cons",
                                 "start": analysis["start"], "end": analysis["end"],
                                 "length": analysis["length"]}
        topology_face = build_contours(
            [{"face_id": 9, "cons_ids": [1, 2, 3, 4]}], topology_edges)["faces"][0]
        self.assertEqual(topology_face["status"], "complete")
        self.assertEqual(topology_face["geometry_status"], "incomplete")
        self.assertEqual(topology_face["loops"], [{
            "cons_ids": [1, 2, 3, 4], "directions": [1, 1, 1, 1],
            "length": 4.0, "kind": "closed_contour",
            "length_source": "native_card_approximation"}])

        for analysis in ({"supported": False, "reason": "not analytic"},
                         {"status": "unsupported", "reason": "not analytic"}):
            with self.subTest(analysis=analysis):
                unsupported_edges = _rectangle()
                unsupported_edges[1]["analysis"] = analysis
                unsupported = build_contours(
                    [{"face_id": 1, "cons_ids": [1, 2, 3, 4]}],
                    unsupported_edges)["faces"][0]
                self.assertEqual(unsupported["status"], "incomplete")
                self.assertEqual(unsupported["geometry_status"], "incomplete")
                self.assertEqual(unsupported["loops"], [])
                self.assertTrue(any("unsupported" in issue for issue in unsupported["issues"]))

        conflicting_edges = _rectangle()
        conflicting_edges[1]["analysis"]["start"] = [1.01, 0.0, 0.0]
        conflict = build_contours([{"face_id": 2, "cons_ids": [1, 2, 3, 4]}],
                                  conflicting_edges, tolerance=1e-4)["faces"][0]
        self.assertEqual(conflict["status"], "incomplete")
        self.assertEqual(conflict["loops"], [])
        self.assertTrue(any("coordinates conflict" in issue for issue in conflict["issues"]))

    def test_reversed_edge_orientations_are_reported_in_traversal_order(self):
        edges = [
            _line(1, 2, 1, (1, 0, 0), (0, 0, 0)),
            _line(2, 2, 3, (1, 0, 0), (1, 1, 0)),
            _line(3, 4, 3, (0, 1, 0), (1, 1, 0)),
            _line(4, 1, 4, (0, 0, 0), (0, 1, 0)),
        ]

        face = build_contours([{"face_id": 1, "cons_ids": [4, 3, 2, 1]}], edges)["faces"][0]

        self.assertEqual(face["status"], "complete")
        self.assertEqual(face["loops"][0]["cons_ids"], [1, 2, 3, 4])
        self.assertEqual(face["loops"][0]["directions"], [-1, 1, -1, -1])

    def test_input_order_does_not_change_output_and_invalid_inputs_raise(self):
        edges = _rectangle(1, 1) + _rectangle(5, 5, 3.0)
        faces = [{"face_id": 20, "cons_ids": [8, 6, 5, 7]},
                 {"face_id": 10, "cons_ids": [3, 1, 4, 2]}]
        expected = build_contours(faces, edges)
        shuffled = build_contours(
            [{"face_id": 10, "cons_ids": [2, 4, 1, 3]},
             {"face_id": 20, "cons_ids": [7, 5, 8, 6]}],
            list(reversed(edges)))
        self.assertEqual(shuffled, expected)

        for tolerance in (0, -1, float("nan"), float("inf"), 1.0001e-3, True):
            with self.subTest(tolerance=tolerance):
                with self.assertRaises(ValueError):
                    build_contours([], [], tolerance=tolerance)
        with self.assertRaises(ValueError):
            build_contours([{"face_id": True, "cons_ids": []}], [])
        with self.assertRaises(ValueError):
            build_contours([{"face_id": 1, "cons_ids": []}], [
                _arc(1, 1, 2, (1, 0, 0), (-1, 0, 0), 2.0 * math.pi + 1e-6)])
        with self.assertRaises(ValueError):
            build_contours([], [], tolerance=10 ** 1000)
        with self.assertRaises(ValueError):
            build_contours([{"face_id": i, "cons_ids": []} for i in range(21)], [])
        with self.assertRaises(ValueError):
            build_contours([], [{"id": i, "vertex_ids": [i, i + 1],
                                 "analysis": {"supported": False}} for i in range(501)])


if __name__ == "__main__":
    unittest.main()
