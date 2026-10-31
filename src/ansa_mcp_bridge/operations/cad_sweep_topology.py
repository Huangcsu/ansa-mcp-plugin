"""Deck-neutral topology planning for a planar quadrilateral straight sweep.

This module consumes the read-only schema-2 result from
``geometry_features.capture``.  It does not read or write a model and does not
depend on a solver deck, FE cards, or ANSA modules.
"""

import math


_TEMPLATE = "T1_planar_quad_straight_sweep"
_ANGULAR_EPSILON = 1e-7


def _object(value, label):
    if type(value) is not dict:
        raise ValueError("%s must be an object" % label)
    return value


def _items(value, label, count=None):
    if type(value) not in (list, tuple):
        raise ValueError("%s must be a list or tuple" % label)
    if count is not None and len(value) != count:
        raise ValueError("%s must contain exactly %d items" % (label, count))
    return value


def _identifier(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError("%s must be a positive integer, not bool" % label)
    return value


def _identifier_list(value, label, count=None):
    values = _items(value, label, count)
    identifiers = tuple(_identifier(item, label) for item in values)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("%s must contain distinct IDs" % label)
    return identifiers


def _number(value, label, positive=False):
    if type(value) not in (int, float):
        raise ValueError("%s must be an int or float, not bool" % label)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("%s must be finite" % label) from exc
    if not math.isfinite(number):
        raise ValueError("%s must be finite" % label)
    if positive and number <= 0.0:
        raise ValueError("%s must be positive" % label)
    return number


def _vector3(value, label):
    components = _items(value, label, 3)
    return tuple(_number(component, "%s[%d]" % (label, index))
                 for index, component in enumerate(components))


def _add(first, second):
    return tuple(math.fsum(pair) for pair in zip(first, second))


def _subtract(first, second):
    return tuple(first[index] - second[index] for index in range(3))


def _scale(vector, factor):
    result = tuple(component * factor for component in vector)
    if not all(math.isfinite(component) for component in result):
        raise ValueError("numeric operation overflowed")
    return result


def _dot(first, second):
    result = math.fsum(first[index] * second[index] for index in range(3))
    if not math.isfinite(result):
        raise ValueError("numeric operation overflowed")
    return result


def _cross(first, second):
    result = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    if not all(math.isfinite(component) for component in result):
        raise ValueError("numeric operation overflowed")
    return result


def _norm(vector):
    result = math.hypot(*vector)
    if not math.isfinite(result):
        raise ValueError("numeric operation overflowed")
    return result


def _distance(first, second):
    return _norm(_subtract(first, second))


def _normalize(vector, label):
    length = _norm(vector)
    if length == 0.0:
        raise ValueError("%s must be nonzero" % label)
    return tuple(component / length for component in vector)


def _mean(points):
    if not points:
        raise ValueError("cannot average an empty point set")
    inverse = 1.0 / len(points)
    return tuple(math.fsum(point[index] for point in points) * inverse
                 for index in range(3))


def _edge_key(first, second):
    return (first, second) if first < second else (second, first)


def _other_endpoint(endpoints, vertex):
    first, second = endpoints
    if vertex == first:
        return second
    if vertex == second:
        return first
    raise ValueError("CONS is not incident to the requested HOT POINT")


def _parse_faces(witness):
    witness = _object(witness, "witness")
    if type(witness.get("feature_schema")) is not int or witness["feature_schema"] != 2:
        raise ValueError("witness.feature_schema must be 2")

    source_scope = _object(witness.get("source_scope"), "witness.source_scope")
    source_face_ids = _identifier_list(
        source_scope.get("face_ids"), "witness.source_scope.face_ids", 6)
    _identifier_list(
        source_scope.get("curve_ids"), "witness.source_scope.curve_ids", 0)
    feature_digest = witness.get("source_snapshot_sha256")
    if (type(feature_digest) is not str or len(feature_digest) != 64
            or any(character not in "0123456789abcdef" for character in feature_digest)):
        raise ValueError("witness.source_snapshot_sha256 must be a SHA-256 digest")

    raw_faces = _items(witness.get("face_boundaries"),
                       "witness.face_boundaries", 6)
    face_cons = {}
    cons_faces = {}
    for face_index, raw_face in enumerate(raw_faces):
        label = "witness.face_boundaries[%d]" % face_index
        face = _object(raw_face, label)
        face_id = _identifier(face.get("face_id"), label + ".face_id")
        if face_id in face_cons:
            raise ValueError("duplicate FACE id %d" % face_id)
        raw_cons = _items(face.get("cons_ids"), label + ".cons_ids", 4)
        cons_ids = _identifier_list(raw_cons, label + ".cons_ids", 4)
        face_cons[face_id] = cons_ids
        for cons_id in cons_ids:
            cons_faces.setdefault(cons_id, []).append(face_id)

    if len(face_cons) != 6:
        raise ValueError("selected domain must contain exactly six distinct FACEs")
    if set(source_face_ids) != set(face_cons):
        raise ValueError("source_scope must match the selected FACEs")
    if len(cons_faces) != 12:
        raise ValueError("selected domain must contain exactly twelve distinct CONS")
    for cons_id, faces in cons_faces.items():
        if len(faces) != 2 or len(set(faces)) != 2:
            raise ValueError(
                "CONS %d must be incident to exactly two selected FACEs" % cons_id)

    raw_parts = _items(witness.get("parts"), "witness.parts")
    face_parts = {}
    for part_index, raw_part in enumerate(raw_parts):
        label = "witness.parts[%d]" % part_index
        part = _object(raw_part, label)
        kind = part.get("type")
        if type(kind) is not str or not kind:
            raise ValueError("%s.type must be a nonempty string" % label)
        if kind != "FACE":
            raise ValueError("six-face geometry witness must contain FACE parts only")
        face_id = _identifier(part.get("id"), label + ".id")
        part_id = _identifier(part.get("part_id"), label + ".part_id")
        if face_id in face_parts:
            raise ValueError("duplicate FACE part assignment for %d" % face_id)
        face_parts[face_id] = part_id
    if set(face_parts) != set(face_cons):
        raise ValueError("FACE part assignments must cover the selected FACEs exactly")
    if len(set(face_parts.values())) != 1:
        raise ValueError("all selected FACEs must belong to the same Part")

    return face_cons, {key: tuple(value) for key, value in cons_faces.items()}


def _parse_boundaries(witness, face_cons, cons_faces, tolerance):
    raw_boundaries = _items(witness.get("boundaries"), "witness.boundaries", 12)
    selected_cons = set(cons_faces)
    boundaries = {}
    point_incidence = {}
    points = {}
    vertex_cons = {}
    raw_vertices = _items(witness.get("vertices"), "witness.vertices", 8)
    for vertex_index, raw_vertex in enumerate(raw_vertices):
        label = "witness.vertices[%d]" % vertex_index
        vertex = _object(raw_vertex, label)
        point_id = _identifier(vertex.get("id"), label + ".id")
        if point_id in points:
            raise ValueError("witness contains duplicate HOT POINT IDs")
        points[point_id] = _vector3(vertex.get("position"), label + ".position")
        vertex_cons[point_id] = set(
            _identifier_list(vertex.get("cons_ids"), label + ".cons_ids"))

    for boundary_index, raw_boundary in enumerate(raw_boundaries):
        label = "witness.boundaries[%d]" % boundary_index
        boundary = _object(raw_boundary, label)
        cons_id = _identifier(boundary.get("id"), label + ".id")
        if cons_id in boundaries:
            raise ValueError("duplicate CONS id %d" % cons_id)
        if cons_id not in selected_cons:
            raise ValueError("CONS %d is outside the selected FACE domain" % cons_id)

        boundary_faces = _identifier_list(boundary.get("face_ids"), label + ".face_ids", 2)
        if any(face_id not in face_cons for face_id in boundary_faces):
            raise ValueError("CONS %d has FACE adjacency outside the selected domain"
                             % cons_id)
        if set(boundary_faces) != set(cons_faces[cons_id]):
            raise ValueError(
                "CONS %d FACE adjacency is inconsistent with face_boundaries"
                % cons_id)

        endpoint = _object(boundary.get("endpoint_topology"),
                           label + ".endpoint_topology")
        if endpoint.get("status") != "available":
            raise ValueError("CONS %d HOT POINT endpoints are incomplete" % cons_id)
        endpoints = _identifier_list(
            endpoint.get("hot_point_ids"),
            label + ".endpoint_topology.hot_point_ids", 2)
        if any(point_id not in points for point_id in endpoints):
            raise ValueError("CONS %d references a HOT POINT missing from witness.vertices"
                             % cons_id)
        if _distance(points[endpoints[0]], points[endpoints[1]]) <= tolerance:
            raise ValueError("CONS %d has degenerate endpoints" % cons_id)
        boundaries[cons_id] = {"face_ids": boundary_faces,
                               "endpoints": endpoints}
        for point_id in endpoints:
            point_incidence.setdefault(point_id, set()).add(cons_id)

    if set(boundaries) != selected_cons:
        raise ValueError("boundaries must cover the twelve selected CONS exactly")
    if len(points) != 8:
        raise ValueError("selected domain must contain exactly eight distinct HOT POINTs")
    if set(point_incidence) != set(points):
        raise ValueError("HOT POINT incidence and coordinates are incomplete")
    if any(len(cons_ids) != 3 for cons_ids in point_incidence.values()):
        raise ValueError("every selected HOT POINT must have degree three")

    for point_id, selected_cons_ids in point_incidence.items():
        if vertex_cons[point_id] & selected_cons != selected_cons_ids:
            raise ValueError("witness HOT POINT/CONS adjacency is inconsistent")

    ordered_points = sorted(points)
    for first_index, first_id in enumerate(ordered_points):
        for second_id in ordered_points[first_index + 1:]:
            if _distance(points[first_id], points[second_id]) <= tolerance:
                raise ValueError(
                    "distinct HOT POINTs %d and %d have coincident coordinates"
                    % (first_id, second_id))

    endpoint_keys = set()
    for cons_id, boundary in boundaries.items():
        key = _edge_key(*boundary["endpoints"])
        if key in endpoint_keys:
            raise ValueError("multiple CONS connect the same two HOT POINTs")
        endpoint_keys.add(key)

    return boundaries, points


def _trace_face_loop(face_id, cons_ids, boundaries):
    adjacency = {}
    for cons_id in cons_ids:
        for point_id in boundaries[cons_id]["endpoints"]:
            adjacency.setdefault(point_id, []).append(cons_id)
    vertices = set(adjacency)
    if len(vertices) != 4:
        raise ValueError("FACE %d must contain exactly four HOT POINTs" % face_id)
    if any(len(set(edges)) != 2 for edges in adjacency.values()):
        raise ValueError("FACE %d boundary must be one simple four-corner loop" % face_id)

    start = min(vertices)
    current = start
    point_loop = [start]
    cons_loop = []
    used = set()
    for _ in range(4):
        candidates = sorted(set(adjacency[current]) - used)
        if not candidates or (len(candidates) != 1 and used):
            raise ValueError("FACE %d boundary is disconnected or branched" % face_id)
        cons_id = candidates[0]
        used.add(cons_id)
        cons_loop.append(cons_id)
        current = _other_endpoint(boundaries[cons_id]["endpoints"], current)
        if current == start:
            break
        if current in point_loop:
            raise ValueError("FACE %d boundary closes before all four CONS" % face_id)
        point_loop.append(current)

    if current != start or len(used) != 4 or len(point_loop) != 4:
        raise ValueError("FACE %d boundary is not one closed four-sided loop" % face_id)
    return tuple(point_loop), tuple(cons_loop)


def _polygon_area_vector(points):
    anchor = points[0]
    result = [0.0, 0.0, 0.0]
    for index in range(1, len(points) - 1):
        vector = _cross(_subtract(points[index], anchor),
                        _subtract(points[index + 1], anchor))
        result = [math.fsum((result[axis], vector[axis])) for axis in range(3)]
    return tuple(0.5 * component for component in result)


def _analyze_face(face_id, point_ids, points, tolerance):
    coordinates = [points[point_id] for point_id in point_ids]
    edge_lengths = [
        _distance(coordinates[index], coordinates[(index + 1) % 4])
        for index in range(4)
    ]
    if any(length <= tolerance for length in edge_lengths):
        raise ValueError("FACE %d has a degenerate or zero-length edge" % face_id)

    area_vector = _polygon_area_vector(coordinates)
    area_magnitude = _norm(area_vector)
    if area_magnitude <= tolerance * tolerance:
        raise ValueError("FACE %d is degenerate or self-crossing" % face_id)
    normal = tuple(component / area_magnitude for component in area_vector)

    origin = coordinates[0]
    if any(abs(_dot(_subtract(point, origin), normal)) > tolerance
           for point in coordinates):
        raise ValueError("FACE %d corners are not planar within tolerance" % face_id)

    center = _mean(coordinates)
    raw_center_vector = _subtract(center, origin)
    tangent_seed = min(
        (_subtract(coordinates[(index + 1) % 4], coordinates[index])
         for index in range(4)),
        key=lambda vector: (-_norm(vector), vector),
    )
    tangent = _normalize(tangent_seed, "FACE %d tangent" % face_id)
    tangent = _normalize(_subtract(tangent, _scale(normal, _dot(tangent, normal))),
                         "FACE %d plane tangent" % face_id)
    second_tangent = _cross(normal, tangent)
    projected = [
        (_dot(_subtract(point, origin), tangent),
         _dot(_subtract(point, origin), second_tangent))
        for point in coordinates
    ]

    turn_epsilon = tolerance * max(edge_lengths)
    turns = []
    for index in range(4):
        first = (projected[(index + 1) % 4][0] - projected[index][0],
                 projected[(index + 1) % 4][1] - projected[index][1])
        second = (projected[(index + 2) % 4][0] - projected[(index + 1) % 4][0],
                  projected[(index + 2) % 4][1] - projected[(index + 1) % 4][1])
        turns.append(first[0] * second[1] - first[1] * second[0])
    if not (all(turn > turn_epsilon for turn in turns)
            or all(turn < -turn_epsilon for turn in turns)):
        raise ValueError("FACE %d is concave, crossed, or degenerate" % face_id)

    # raw_center_vector also keeps a valid center calculation exercised before
    # the caller orients each plane using the box center.
    if _norm(raw_center_vector) <= tolerance:
        raise ValueError("FACE %d has no distinct geometric center" % face_id)
    return {"origin": list(center), "normal": normal}


def _orient_and_check_faces(face_loops, points, tolerance):
    box_center = _mean(list(points.values()))
    planes = {}
    for face_id in sorted(face_loops):
        analysed = _analyze_face(face_id, face_loops[face_id], points, tolerance)
        origin = tuple(analysed["origin"])
        normal = analysed["normal"]
        radial = _subtract(origin, box_center)
        if abs(_dot(radial, normal)) <= tolerance:
            raise ValueError("FACE %d center does not define an outward side" % face_id)
        if _dot(radial, normal) < 0.0:
            normal = _scale(normal, -1.0)
        if any(_dot(_subtract(point, origin), normal) > tolerance
               for point in points.values()):
            raise ValueError("selected FACEs do not form a convex polyhedron")
        planes[face_id] = {"face_id": face_id,
                           "origin": list(origin),
                           "outward_normal": list(normal)}
    return planes


def _translation_mapping(source_loop, target_loop, points, delta, tolerance):
    mapping = {}
    for source_point in source_loop:
        expected = _add(points[source_point], delta)
        candidates = [
            target_point for target_point in target_loop
            if _distance(points[target_point], expected) <= tolerance
        ]
        if len(candidates) != 1:
            raise ValueError(
                "source HOT POINT %d does not map uniquely to the translated target"
                % source_point)
        mapping[source_point] = candidates[0]
    if len(set(mapping.values())) != 4:
        raise ValueError("target vertex matching is not one-to-one")
    return mapping


def _plan_translation(witness, source_face_id, target_face_id, direction,
                      distance, tolerance):
    direction = _normalize(_vector3(direction, "direction"), "direction")
    distance = _number(distance, "distance", positive=True)
    tolerance = _number(tolerance, "tolerance", positive=True)
    source_face_id = _identifier(source_face_id, "source_face_id")
    target_face_id = _identifier(target_face_id, "target_face_id")
    if source_face_id == target_face_id:
        raise ValueError("source_face_id and target_face_id must differ")

    witness = _object(witness, "witness")
    face_cons, cons_faces = _parse_faces(witness)
    if source_face_id not in face_cons:
        raise ValueError("source_face_id is not in the selected FACE domain")
    if target_face_id not in face_cons:
        raise ValueError("target_face_id is not in the selected FACE domain")
    boundaries, points = _parse_boundaries(
        witness, face_cons, cons_faces, tolerance)

    face_loops = {}
    face_cons_loops = {}
    for face_id in sorted(face_cons):
        point_loop, cons_loop = _trace_face_loop(
            face_id, face_cons[face_id], boundaries)
        face_loops[face_id] = point_loop
        face_cons_loops[face_id] = cons_loop

    source_loop = face_loops[source_face_id]
    target_loop_native = face_loops[target_face_id]
    if set(source_loop).intersection(target_loop_native):
        raise ValueError("source and target loops must not share HOT POINTs")

    planes = _orient_and_check_faces(face_loops, points, tolerance)
    source_normal = tuple(planes[source_face_id]["outward_normal"])
    target_normal = tuple(planes[target_face_id]["outward_normal"])
    if abs(_dot(source_normal, direction)) < 1.0 - _ANGULAR_EPSILON:
        raise ValueError("source FACE plane is not perpendicular to direction")
    if abs(_dot(target_normal, direction)) < 1.0 - _ANGULAR_EPSILON:
        raise ValueError("target FACE plane is not perpendicular to direction")
    if _dot(source_normal, direction) >= 0.0:
        raise ValueError("source outward normal must oppose the sweep direction")
    if _dot(target_normal, direction) <= 0.0:
        raise ValueError("target outward normal must follow the sweep direction")

    delta = _scale(direction, distance)
    mapping = _translation_mapping(
        source_loop, target_loop_native, points, delta, tolerance)
    mapped_target_loop = tuple(mapping[point_id] for point_id in source_loop)

    edge_lookup = {}
    for cons_id, boundary in boundaries.items():
        edge_lookup[_edge_key(*boundary["endpoints"])] = cons_id

    connector_by_source = {}
    vertex_pairs = []
    for source_point in source_loop:
        target_point = mapping[source_point]
        connector_id = edge_lookup.get(_edge_key(source_point, target_point))
        if connector_id is None:
            raise ValueError(
                "missing longitudinal CONS for source HOT POINT %d" % source_point)
        if connector_id in face_cons[source_face_id] or connector_id in face_cons[target_face_id]:
            raise ValueError("longitudinal CONS cannot be a cap boundary")
        connector_boundary = boundaries[connector_id]
        connector_matches = (
            set(connector_boundary["endpoints"]) == {source_point, target_point}
            and _distance(points[target_point],
                          _add(points[source_point], delta)) <= tolerance
        )
        if not connector_matches:
            raise ValueError(
                "longitudinal CONS %d does not match the translated corners"
                % connector_id)
        connector_by_source[source_point] = connector_id
        vertex_pairs.append({
            "source_hot_point_id": source_point,
            "target_hot_point_id": target_point,
            "connector_cons_id": connector_id,
        })

    source_cons_loop = face_cons_loops[source_face_id]
    target_cap_cons = set(face_cons[target_face_id])
    remaining_faces = set(face_cons) - {source_face_id, target_face_id}
    side_pairs = []
    used_side_faces = set()
    for index, source_cons_id in enumerate(source_cons_loop):
        next_index = (index + 1) % 4
        source_point = source_loop[index]
        next_source_point = source_loop[next_index]
        target_point = mapping[source_point]
        next_target_point = mapping[next_source_point]
        target_cons_id = edge_lookup.get(_edge_key(target_point, next_target_point))
        if target_cons_id is None or target_cons_id not in target_cap_cons:
            raise ValueError(
                "translated source CONS %d has no matching target cap edge"
                % source_cons_id)

        candidates = [
            face_id for face_id in remaining_faces
            if source_cons_id in face_cons[face_id]
            and target_cons_id in face_cons[face_id]
        ]
        if len(candidates) != 1:
            raise ValueError(
                "source/target cap edges %d/%d must be enclosed by exactly one side FACE"
                % (source_cons_id, target_cons_id))
        side_face_id = candidates[0]
        if side_face_id in used_side_faces:
            raise ValueError("one side FACE cannot enclose multiple cap edge pairs")
        used_side_faces.add(side_face_id)

        connector_ids = [
            connector_by_source[source_point],
            connector_by_source[next_source_point],
        ]
        expected_side_edges = {
            source_cons_id, target_cons_id,
            connector_ids[0], connector_ids[1],
        }
        if set(face_cons[side_face_id]) != expected_side_edges:
            raise ValueError(
                "side FACE %d does not contain exactly its two cap and two longitudinal CONS"
                % side_face_id)
        side_pairs.append({
            "source_cons_id": source_cons_id,
            "target_cons_id": target_cons_id,
            "side_face_id": side_face_id,
            "connector_cons_ids": connector_ids,
        })

    if used_side_faces != remaining_faces:
        raise ValueError("the four side FACEs must be used exactly once")
    explained_faces = {source_face_id, target_face_id}.union(used_side_faces)
    explained_edges = (
        set(face_cons[source_face_id])
        | set(face_cons[target_face_id])
        | set(connector_by_source.values())
    )
    if explained_faces != set(face_cons):
        raise ValueError("not all six selected FACEs are explained by the template")
    if explained_edges != set(boundaries):
        raise ValueError("not all twelve selected CONS are explained by the template")

    source_coordinates = [points[point_id] for point_id in source_loop]
    source_area = _norm(_polygon_area_vector(source_coordinates))
    expected_volume = source_area * distance
    if not math.isfinite(source_area) or source_area <= 0.0:
        raise ValueError("source area is not finite and positive")
    if not math.isfinite(expected_volume) or expected_volume <= 0.0:
        raise ValueError("expected volume is not finite and positive")

    return {
        "template": _TEMPLATE,
        "source_face_id": source_face_id,
        "target_face_id": target_face_id,
        "side_face_ids": sorted(used_side_faces),
        "direction": list(direction),
        "distance": distance,
        "source_loop": list(source_loop),
        "target_loop": list(mapped_target_loop),
        "vertex_pairs": vertex_pairs,
        "side_pairs": side_pairs,
        "planes": [planes[face_id] for face_id in sorted(planes)],
        "source_area": source_area,
        "expected_volume": expected_volume,
        "cad_surface_geometry_verified": False,
    }


def plan_translation(witness, source_face_id, target_face_id, direction,
                     distance, tolerance=1e-7):
    """Plan a strict six-face translation from ``geometry_features.capture``.

    Native FACE, CONS and HOT POINT identities define the topology.  Coordinates
    validate only the declared corners and translation; continuous CAD surface
    geometry remains unverified.
    """
    try:
        return _plan_translation(
            witness, source_face_id, target_face_id, direction,
            distance, tolerance)
    except ValueError:
        raise
    except (ArithmeticError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("unsupported straight-sweep witness: %s" % exc) from exc
