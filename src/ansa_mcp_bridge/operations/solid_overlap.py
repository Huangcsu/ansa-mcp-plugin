"""Pure-Python overlap audit for linear CHEXA8 and CPENTA6 solids.

This module has no ANSA imports or third-party dependencies. Coordinates and
tolerances use the caller's length units.
"""

from bisect import bisect_right
import heapq
import itertools
import math


DEFAULT_TOLERANCE = 1.0e-8
DEFAULT_MAX_CANDIDATE_PAIRS = 500_000
DEFAULT_MAX_DETAIL_ROWS = 1_000
_BVH_LEAF_SIZE = 8

_SUPPORTED_TYPES = {
    "CHEXA": ("CHEXA8", 8),
    "CHEXA8": ("CHEXA8", 8),
    "CPENTA": ("CPENTA6", 6),
    "CPENTA6": ("CPENTA6", 6),
}
_HULL_SEPARATION_REASONS = frozenset(("warped_face", "nonconvex_face", "nonconvex_polyhedron"))
_FACES = {
    "CHEXA8": (
        (0, 3, 2, 1), (4, 5, 6, 7),
        (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
    ),
    "CPENTA6": (
        (0, 2, 1), (3, 4, 5),
        (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5),
    ),
}


def _sequence(value, label):
    if type(value) not in (list, tuple):
        raise ValueError(f"{label} must be a list or tuple")
    return value


def _finite_number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _vector3(value, label):
    values = _sequence(value, label)
    if len(values) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return tuple(_finite_number(item, f"{label}[{index}]") for index, item in enumerate(values))


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(vector, factor):
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _dot(a, b):
    return math.fsum((a[0] * b[0], a[1] * b[1], a[2] * b[2]))


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(vector):
    return math.sqrt(_dot(vector, vector))


def _unit(vector):
    length = _norm(vector)
    if not math.isfinite(length) or length == 0.0:
        return None
    return _scale(vector, 1.0 / length)


def _bbox(points):
    return (
        tuple(min(point[axis] for point in points) for axis in range(3)),
        tuple(max(point[axis] for point in points) for axis in range(3)),
    )


def _bbox_union(a, b):
    return (
        tuple(min(a[0][axis], b[0][axis]) for axis in range(3)),
        tuple(max(a[1][axis], b[1][axis]) for axis in range(3)),
    )


def _bbox_separated(a, b, tolerance):
    return any(
        b[0][axis] - a[1][axis] > tolerance
        or a[0][axis] - b[1][axis] > tolerance
        for axis in range(3)
    )


def _prepare_inputs(nodes, elements):
    raw_nodes = _sequence(nodes, "nodes")
    raw_elements = _sequence(elements, "elements")
    coordinates = {}
    for index, row in enumerate(raw_nodes):
        if type(row) is not dict:
            raise ValueError(f"nodes[{index}] must be a dict")
        node_id = _positive_id(row.get("id"), f"nodes[{index}].id")
        if node_id in coordinates:
            raise ValueError(f"duplicate node id {node_id}")
        coordinates[node_id] = _vector3(row.get("xyz"), f"node {node_id}.xyz")

    seen_element_ids = set()
    prepared = []
    for index, row in enumerate(raw_elements):
        if type(row) is not dict:
            raise ValueError(f"elements[{index}] must be a dict")
        element_id = _positive_id(row.get("id"), f"elements[{index}].id")
        if element_id in seen_element_ids:
            raise ValueError(f"duplicate element id {element_id}")
        seen_element_ids.add(element_id)

        raw_type = row.get("type")
        if type(raw_type) is not str or not raw_type.strip():
            raise ValueError(f"element {element_id}.type must be a non-empty string")
        type_key = raw_type.strip().upper()
        type_info = _SUPPORTED_TYPES.get(type_key)
        raw_connectivity = _sequence(row.get("nodes"), f"element {element_id}.nodes")
        if not raw_connectivity:
            raise ValueError(f"element {element_id}.nodes cannot be empty")
        node_ids = tuple(
            _positive_id(node_id, f"element {element_id}.nodes[{node_index}]")
            for node_index, node_id in enumerate(raw_connectivity)
        )
        if len(set(node_ids)) != len(node_ids):
            raise ValueError(f"element {element_id} contains duplicate node IDs")
        missing = sorted(set(node_ids) - coordinates.keys())
        if missing:
            raise ValueError(f"element {element_id} references unknown node IDs {missing}")

        item = {
            "id": element_id,
            "type": type_info[0] if type_info else type_key,
            "node_ids": node_ids,
            "supported": type_info is not None and len(node_ids) == type_info[1],
            "bbox": _bbox([coordinates[node_id] for node_id in node_ids]),
        }
        if type_info is None:
            item["unsupported_reason"] = "unsupported_element_type"
        elif len(node_ids) != type_info[1]:
            item["unsupported_reason"] = "unsupported_node_count"
        prepared.append(item)

    prepared.sort(key=lambda item: item["id"])
    return coordinates, prepared


def _geometry_tolerances(points):
    bounds = _bbox(points)
    extents = tuple(bounds[1][axis] - bounds[0][axis] for axis in range(3))
    scale = max(extents)
    if not math.isfinite(scale) or not all(math.isfinite(value) for value in extents):
        return bounds, scale, math.inf, math.inf
    coordinate_magnitude = max((abs(value) for point in points for value in point), default=1.0)
    roundoff = math.ulp(coordinate_magnitude if coordinate_magnitude else 1.0) * 16.0
    linear = max(scale * 1.0e-10, roundoff, 1.0e-14)
    area = linear * max(scale, linear)
    if not math.isfinite(linear) or not math.isfinite(area):
        return bounds, scale, math.inf, math.inf
    return bounds, scale, linear, area


def _polygon_normal(points, face, center):
    normal = (0.0, 0.0, 0.0)
    for index, point_index in enumerate(face):
        first = _sub(points[point_index], center)
        second = _sub(points[face[(index + 1) % len(face)]], center)
        normal = _add(normal, _cross(first, second))
    return _unit(normal)


def _element_geometry(element, coordinates):
    points = tuple(coordinates[node_id] for node_id in element["node_ids"])
    bounds, scale, linear_eps, area_eps = _geometry_tolerances(points)
    faces = _FACES[element["type"]]
    reason = None

    if not math.isfinite(scale) or not math.isfinite(linear_eps) or not math.isfinite(area_eps):
        reason = "nonfinite_geometry_range"
    elif scale <= linear_eps or len(set(points)) != len(points):
        reason = "degenerate_geometry"

    center = tuple((bounds[0][axis] + bounds[1][axis]) * 0.5 for axis in range(3))
    face_normals = []
    # Approximate planarity at a scale-based tolerance can exceed the overlap
    # tolerance. Keep its actual residual for the positive-overlap decision.
    planarity_error = 0.0
    edge_indices = set()
    if reason is None:
        for face in faces:
            normal = _polygon_normal(points, face, center)
            if normal is None:
                reason = "degenerate_face"
                break
            anchor = points[face[0]]
            face_error = max(abs(_dot(_sub(points[index], anchor), normal)) for index in face[1:])
            planarity_error = max(planarity_error, face_error)
            if face_error > linear_eps:
                reason = "warped_face"
                break

            turns = []
            for index, point_index in enumerate(face):
                first = _sub(points[face[(index + 1) % len(face)]], points[point_index])
                second = _sub(points[face[(index + 2) % len(face)]], points[face[(index + 1) % len(face)]])
                turns.append(_dot(_cross(first, second), normal))
            if any(abs(turn) <= area_eps for turn in turns):
                reason = "degenerate_face"
                break
            if any(turn < -area_eps for turn in turns):
                reason = "nonconvex_face"
                break

            positive_side = False
            negative_side = False
            for point_index, point in enumerate(points):
                if point_index in face:
                    continue
                distance = _dot(_sub(point, anchor), normal)
                positive_side = positive_side or distance > linear_eps
                negative_side = negative_side or distance < -linear_eps
            if positive_side and negative_side:
                reason = "nonconvex_polyhedron"
                break

            face_normals.append(normal)
            for index, point_index in enumerate(face):
                other = face[(index + 1) % len(face)]
                edge_indices.add((min(point_index, other), max(point_index, other)))

    if reason is None:
        volume_terms = []
        for face in faces:
            origin = _sub(points[face[0]], center)
            for index in range(1, len(face) - 1):
                second = _sub(points[face[index]], center)
                third = _sub(points[face[index + 1]], center)
                volume_terms.append(_dot(origin, _cross(second, third)) / 6.0)
        signed_volume = math.fsum(volume_terms)
        volume_epsilon = linear_eps * max(scale, linear_eps) ** 2
        if not math.isfinite(signed_volume) or not math.isfinite(volume_epsilon):
            reason = "nonfinite_geometry_arithmetic"
        elif abs(signed_volume) <= volume_epsilon:
            reason = "degenerate_volume"
        elif signed_volume < 0.0:
            reason = "inverted_orientation"

    valid = reason is None
    edges = tuple((points[first], points[second]) for first, second in sorted(edge_indices)) if valid else ()
    geometry = {
        "points": points,
        "bbox": bounds,
        "valid": valid,
        "reason": reason,
        "face_normals": tuple(face_normals) if valid else (),
        "edges": edges,
        "hull": None,
        "planarity_error": planarity_error,
    }
    if not valid and reason != "nonfinite_geometry_range":
        geometry["hull"] = _convex_hull_geometry(points, linear_eps, area_eps)
    return geometry


def _cross_2d(origin, first, second):
    return (
        (first[0] - origin[0]) * (second[1] - origin[1])
        - (first[1] - origin[1]) * (second[0] - origin[0])
    )


def _convex_hull_geometry(raw_points, linear_eps, area_eps):
    points = []
    point_index = {}
    for point in raw_points:
        if point not in point_index:
            point_index[point] = len(points)
            points.append(point)
    if len(points) < 4:
        return None

    bounds, scale, _, _ = _geometry_tolerances(points)
    if not math.isfinite(scale) or not math.isfinite(linear_eps) or not math.isfinite(area_eps):
        return None
    center = tuple((bounds[0][axis] + bounds[1][axis]) * 0.5 for axis in range(3))
    faces_by_vertices = {}
    count = len(points)
    for first, second, third in itertools.combinations(range(count), 3):
        raw_normal = _cross(_sub(points[second], points[first]), _sub(points[third], points[first]))
        if _norm(raw_normal) <= area_eps:
            continue
        normal = _unit(raw_normal)
        if normal is None:
            continue
        distances = [_dot(_sub(point, points[first]), normal) for point in points]
        has_positive = any(distance > linear_eps for distance in distances)
        has_negative = any(distance < -linear_eps for distance in distances)
        if has_positive and has_negative:
            continue
        if has_positive:
            normal = _scale(normal, -1.0)
        coplanar = [index for index, distance in enumerate(distances) if abs(distance) <= linear_eps]
        direction = _unit(_sub(points[second], points[first]))
        if direction is None:
            continue
        perpendicular = _cross(normal, direction)
        projected = []
        for index in coplanar:
            relative = _sub(points[index], points[first])
            projected.append((_dot(relative, direction), _dot(relative, perpendicular), index))
        projected.sort()
        lower = []
        for entry in projected:
            while len(lower) >= 2 and _cross_2d(lower[-2], lower[-1], entry) <= area_eps:
                lower.pop()
            lower.append(entry)
        upper = []
        for entry in reversed(projected):
            while len(upper) >= 2 and _cross_2d(upper[-2], upper[-1], entry) <= area_eps:
                upper.pop()
            upper.append(entry)
        ordered = [entry[2] for entry in lower[:-1] + upper[:-1]]
        if len(ordered) >= 3:
            faces_by_vertices[frozenset(ordered)] = (tuple(ordered), normal)

    if len(faces_by_vertices) < 4:
        return None
    faces = tuple(faces_by_vertices.values())
    volume_terms = []
    edge_indices = set()
    normals = []
    for face, normal in faces:
        normals.append(normal)
        origin = _sub(points[face[0]], center)
        for index in range(1, len(face) - 1):
            second = _sub(points[face[index]], center)
            third = _sub(points[face[index + 1]], center)
            volume_terms.append(_dot(origin, _cross(second, third)) / 6.0)
        for index, point_index in enumerate(face):
            other = face[(index + 1) % len(face)]
            edge_indices.add((min(point_index, other), max(point_index, other)))
    hull_volume = math.fsum(volume_terms)
    volume_epsilon = linear_eps * max(scale, linear_eps) ** 2
    if not math.isfinite(hull_volume) or not math.isfinite(volume_epsilon):
        return None
    if abs(hull_volume) <= volume_epsilon:
        return None
    edges = tuple((points[first], points[second]) for first, second in sorted(edge_indices))
    return {
        "points": tuple(points),
        "bbox": bounds,
        "face_normals": tuple(normals),
        "edges": edges,
    }


def _sat_axes(first, second):
    seen = set()

    def add_axis(raw_axis):
        axis = _unit(raw_axis)
        if axis is None:
            return None
        for component in axis:
            if abs(component) > 1.0e-14:
                if component < 0.0:
                    axis = _scale(axis, -1.0)
                break
        key = axis
        if key not in seen:
            seen.add(key)
            return axis
        return None

    for normal in first["face_normals"]:
        axis = add_axis(normal)
        if axis is not None:
            yield axis
    for normal in second["face_normals"]:
        axis = add_axis(normal)
        if axis is not None:
            yield axis
    first_edges = tuple(_sub(edge[1], edge[0]) for edge in first["edges"])
    second_edges = tuple(_sub(edge[1], edge[0]) for edge in second["edges"])
    for edge_a in first_edges:
        length_a = _norm(edge_a)
        for edge_b in second_edges:
            length_b = _norm(edge_b)
            raw_axis = _cross(edge_a, edge_b)
            if _norm(raw_axis) > 1.0e-12 * length_a * length_b:
                axis = add_axis(raw_axis)
                if axis is not None:
                    yield axis


def _sat_relation(first, second, tolerance):
    axes = _sat_axes(first, second)
    bounds = _bbox(first["points"] + second["points"])
    origin = tuple((bounds[0][axis] + bounds[1][axis]) * 0.5 for axis in range(3))
    minimum_overlap = math.inf
    # Subtraction, normalization and dot products can lose low-order bits even
    # for exactly planar input. An ambiguous projection must never clear a pair.
    magnitude = max(abs(value) for point in first["points"] + second["points"] for value in point)
    projection_error = 128.0 * math.ulp(magnitude if magnitude else 1.0)
    minimum_lower_bound = math.inf
    ambiguous_projection = False
    for axis in axes:
        projections_a = [_dot(_sub(point, origin), axis) for point in first["points"]]
        projections_b = [_dot(_sub(point, origin), axis) for point in second["points"]]
        overlap = min(max(projections_a), max(projections_b)) - max(min(projections_a), min(projections_b))
        if overlap + projection_error < -tolerance:
            return "separated", {"separating_gap": -overlap, "projection_error_guard": projection_error}
        if overlap + projection_error <= tolerance:
            # One projection alone certifies no overlap above tolerance.
            # Remaining edge axes cannot invalidate that separating witness.
            return "contact_or_below_tolerance", {"witness_axis_overlap": overlap,
                "projection_error_guard": projection_error}
        ambiguous_projection = ambiguous_projection or overlap - projection_error <= tolerance
        minimum_overlap = min(minimum_overlap, overlap)
        minimum_lower_bound = min(minimum_lower_bound, overlap - projection_error)
    if not math.isfinite(minimum_overlap):
        return "unresolved", {"reason": "no_valid_sat_axes"}
    approximation = 4.0 * (first.get("planarity_error",0.0)+second.get("planarity_error",0.0))
    if ambiguous_projection or minimum_lower_bound <= tolerance + approximation:
        return "unresolved", {"reason":"numerical_or_planarity_uncertainty_exceeds_overlap_margin",
                              "projection_error_guard": projection_error}
    if minimum_overlap > tolerance:
        return "overlap", {"minimum_axis_overlap": minimum_overlap}
    return "contact_or_below_tolerance", {"minimum_axis_overlap": minimum_overlap}


def _build_bvh(elements):
    if len(elements) <= _BVH_LEAF_SIZE:
        bounds = elements[0]["bbox"]
        for item in elements[1:]:
            bounds = _bbox_union(bounds, item["bbox"])
        return {"bbox": bounds, "count": len(elements), "items": tuple(elements)}

    bounds = elements[0]["bbox"]
    for item in elements[1:]:
        bounds = _bbox_union(bounds, item["bbox"])
    centers = [
        tuple((item["bbox"][0][axis] + item["bbox"][1][axis]) * 0.5 for axis in range(3))
        for item in elements
    ]
    spreads = tuple(max(center[axis] for center in centers) - min(center[axis] for center in centers) for axis in range(3))
    axis = max(range(3), key=lambda index: (spreads[index], -index))
    ordered = sorted(
        elements,
        key=lambda item: (
            (item["bbox"][0][axis] + item["bbox"][1][axis]) * 0.5,
            item["id"],
        ),
    )
    middle = len(ordered) // 2
    left = _build_bvh(ordered[:middle])
    right = _build_bvh(ordered[middle:])
    return {"bbox": bounds, "count": len(elements), "left": left, "right": right}


def _remember_detail(heap, limit, section, row):
    if limit == 0:
        return
    ids = [row["element_id"]] if row.get("kind") == "element" else row["element_ids"]
    first = ids[0]
    second = ids[1] if len(ids) > 1 else first
    section_rank = 0 if section == "unresolved" else 1
    key = (first, second, section_rank)
    entry = (-key[0], -key[1], -key[2], section, row)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
    elif entry[:3] > heap[0][:3]:
        heapq.heapreplace(heap, entry)


def _unsupported_pair_details(all_ids, unsupported_ids, heap, detail_limit):
    if not unsupported_ids or detail_limit == 0:
        return
    unsupported_set = set(unsupported_ids)
    added = 0
    for index, first in enumerate(all_ids[:-1]):
        if first in unsupported_set:
            seconds = all_ids[index + 1:]
        else:
            seconds = unsupported_ids[bisect_right(unsupported_ids, first):]
        for second in seconds:
            _remember_detail(heap, detail_limit, "unresolved", {
                "kind": "pair",
                "element_ids": [first, second],
                "reason": "unsupported_element_geometry",
                "proof_basis": "No conservative solid bound is available for every element type in this pair.",
            })
            added += 1
            if added >= detail_limit:
                return


def _walk_bvh_pairs(first_node, second_node, state):
    if state["stopped"]:
        return
    if first_node is second_node:
        if "items" in first_node:
            items = first_node["items"]
            for first_index in range(len(items)):
                for second_index in range(first_index + 1, len(items)):
                    if not _visit_element_pair(items[first_index], items[second_index], state):
                        return
            return
        _walk_bvh_pairs(first_node["left"], first_node["left"], state)
        _walk_bvh_pairs(first_node["left"], first_node["right"], state)
        _walk_bvh_pairs(first_node["right"], first_node["right"], state)
        return

    if _bbox_separated(first_node["bbox"], second_node["bbox"], state["tolerance"]):
        state["broadphase_separated_pair_count"] += first_node["count"] * second_node["count"]
        return

    first_leaf = "items" in first_node
    second_leaf = "items" in second_node
    if first_leaf and second_leaf:
        for first in first_node["items"]:
            for second in second_node["items"]:
                if not _visit_element_pair(first, second, state):
                    return
        return

    if not first_leaf and (second_leaf or first_node["count"] >= second_node["count"]):
        _walk_bvh_pairs(first_node["left"], second_node, state)
        _walk_bvh_pairs(first_node["right"], second_node, state)
    else:
        _walk_bvh_pairs(first_node, second_node["left"], state)
        _walk_bvh_pairs(first_node, second_node["right"], state)


def _visit_element_pair(first, second, state):
    if _bbox_separated(first["bbox"], second["bbox"], state["tolerance"]):
        state["broadphase_separated_pair_count"] += 1
        return True

    state["candidate_pair_count"] += 1
    if state["narrowphase_checked_pair_count"] >= state["max_candidate_pairs"]:
        state["stopped"] = True
        return False

    state["narrowphase_checked_pair_count"] += 1
    ids = sorted((first["id"], second["id"]))
    geometry_a = first["geometry"]
    geometry_b = second["geometry"]
    overlaps = [min(first["bbox"][1][axis],second["bbox"][1][axis])
                - max(first["bbox"][0][axis],second["bbox"][0][axis]) for axis in range(3)]
    if min(overlaps) <= state["tolerance"]:
        state["contact_or_below_tolerance_pair_count"] += 1
        _remember_detail(state["detail_heap"],state["max_detail_rows"],"pairs",{
            "element_ids":ids,"status":"contact_or_below_tolerance",
            "proof_basis":"all first-order nodal points enclosed by AABB; a coordinate axis bounds overlap",
            "witness_axis_overlap":min(overlaps),"tolerance":state["tolerance"],
        })
        return True
    if geometry_a["valid"] and geometry_b["valid"]:
        state["sat_checked_pair_count"] += 1
        relation, evidence = _sat_relation(geometry_a, geometry_b, state["tolerance"])
        if relation == "overlap":
            state["overlap_pair_count"] += 1
            _remember_detail(state["detail_heap"], state["max_detail_rows"], "pairs", {
                "element_ids": ids,
                "status": "overlap",
                "minimum_axis_overlap": evidence["minimum_axis_overlap"],
                "tolerance": state["tolerance"],
            })
        elif relation == "contact_or_below_tolerance":
            state["contact_or_below_tolerance_pair_count"] += 1
            _remember_detail(state["detail_heap"], state["max_detail_rows"], "pairs", {
                "element_ids": ids,
                "status": relation,
                "witness_axis_overlap": evidence["witness_axis_overlap"],
                "projection_error_guard": evidence["projection_error_guard"],
                "tolerance": state["tolerance"],
                "semantic": "May be exact contact, a small gap, or penetration no greater than tolerance.",
            })
        elif relation == "separated":
            state["sat_separated_pair_count"] += 1
        else:
            state["unresolved_pair_count"] += 1
            _remember_detail(state["detail_heap"], state["max_detail_rows"], "unresolved", {
                "kind": "pair", "element_ids": ids, "reason": evidence["reason"],
            })
        return True

    hull_a = geometry_a["hull"] if not geometry_a["valid"] else geometry_a
    hull_b = geometry_b["hull"] if not geometry_b["valid"] else geometry_b
    if hull_a is None or hull_b is None:
        state["unresolved_pair_count"] += 1
        _remember_detail(state["detail_heap"], state["max_detail_rows"], "unresolved", {
            "kind": "pair", "element_ids": ids, "reason": "convex_hull_unavailable",
        })
        return True

    state["convex_hull_checked_pair_count"] += 1
    relation, evidence = _sat_relation(hull_a, hull_b, state["tolerance"])
    if relation == "separated":
        state["convex_hull_separated_pair_count"] += 1
    elif relation == "contact_or_below_tolerance":
        state["contact_or_below_tolerance_pair_count"] += 1
        _remember_detail(state["detail_heap"],state["max_detail_rows"],"pairs",{
            "element_ids":ids,"status":relation,"proof_basis":"nodal convex-hull projection witness",
            "witness_axis_overlap":evidence["witness_axis_overlap"],
            "projection_error_guard":evidence["projection_error_guard"],"tolerance":state["tolerance"],
        })
    else:
        state["unresolved_pair_count"] += 1
        _remember_detail(state["detail_heap"], state["max_detail_rows"], "unresolved", {
            "kind": "pair",
            "element_ids": ids,
            "reason": "convex_hulls_overlap_or_are_within_tolerance",
            "proof_basis": "Nodal convex hull SAT could not prove separation; no overlap is asserted.",
        })
    return True


def _finish_details(heap):
    entries = sorted(heap, key=lambda entry: (-entry[0], -entry[1], -entry[2]))
    pairs = []
    unresolved = []
    for _, _, _, section, row in entries:
        if section == "pairs":
            pairs.append(row)
        else:
            unresolved.append(row)
    return pairs, unresolved


def audit_solid_overlaps(
    nodes,
    elements,
    *,
    tolerance=DEFAULT_TOLERANCE,
    max_candidate_pairs=DEFAULT_MAX_CANDIDATE_PAIRS,
    max_detail_rows=DEFAULT_MAX_DETAIL_ROWS,
):
    """Audit potential solid overlaps without importing or calling ANSA.

    ``nodes`` is a sequence of ``{"id": int, "xyz": [x, y, z]}`` rows;
    ``elements`` is a sequence of ``{"id": int, "type": str, "nodes": [...]}``
    rows. Supported type spellings are CHEXA/CHEXA8 and CPENTA/CPENTA6.

    Structural input errors raise ``ValueError``. Geometrically degenerate,
    warped, non-convex, or unsupported elements are reported as incomplete.
    """
    tolerance = _finite_number(tolerance, "tolerance")
    if tolerance < 0.0:
        raise ValueError("tolerance must be nonnegative")
    for value, label in (
        (max_candidate_pairs, "max_candidate_pairs"),
        (max_detail_rows, "max_detail_rows"),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{label} must be a nonnegative integer")

    coordinates, prepared = _prepare_inputs(nodes, elements)
    known = []
    unsupported = []
    geometry_issue_counts = {}
    unresolved_geometry_ids = set()
    for element in prepared:
        if not element["supported"]:
            unsupported.append(element)
            continue
        geometry = _element_geometry(element, coordinates)
        element["geometry"] = geometry
        if not geometry["valid"]:
            geometry_issue_counts[geometry["reason"]] = geometry_issue_counts.get(geometry["reason"], 0) + 1
            if geometry["reason"] not in _HULL_SEPARATION_REASONS or geometry["hull"] is None:
                unresolved_geometry_ids.add(element["id"])
        known.append(element)

    detail_heap = []
    for element in prepared:
        if not element["supported"]:
            _remember_detail(detail_heap, max_detail_rows, "unresolved", {
                "kind": "element",
                "element_id": element["id"],
                "element_type": element["type"],
                "reason": element["unsupported_reason"],
            })
        elif element["id"] in unresolved_geometry_ids:
            _remember_detail(detail_heap, max_detail_rows, "unresolved", {
                "kind": "element",
                "element_id": element["id"],
                "element_type": element["type"],
                "reason": element["geometry"]["reason"],
            })

    unsupported_ids = [element["id"] for element in unsupported]
    all_ids = [element["id"] for element in prepared]
    total_pair_count = len(prepared) * (len(prepared) - 1) // 2
    unsupported_related_pair_count = total_pair_count - len(known) * (len(known) - 1) // 2
    _unsupported_pair_details(all_ids, unsupported_ids, detail_heap, max_detail_rows)

    state = {
        "tolerance": tolerance,
        "max_candidate_pairs": max_candidate_pairs,
        "max_detail_rows": max_detail_rows,
        "detail_heap": detail_heap,
        "stopped": False,
        "candidate_pair_count": 0,
        "narrowphase_checked_pair_count": 0,
        "broadphase_separated_pair_count": 0,
        "sat_checked_pair_count": 0,
        "sat_separated_pair_count": 0,
        "convex_hull_checked_pair_count": 0,
        "convex_hull_separated_pair_count": 0,
        "overlap_pair_count": 0,
        "contact_or_below_tolerance_pair_count": 0,
        "unresolved_pair_count": unsupported_related_pair_count,
    }
    if len(known) > 1:
        tree = _build_bvh(known)
        _walk_bvh_pairs(tree, tree, state)

    classified_pair_count = (
        unsupported_related_pair_count
        + state["broadphase_separated_pair_count"]
        + state["narrowphase_checked_pair_count"]
    )
    untested_pair_count = total_pair_count - classified_pair_count
    unresolved_element_count = len(unsupported) + len(unresolved_geometry_ids)
    unsupported_type_counts = {}
    unsupported_reason_counts = {}
    for element in unsupported:
        unsupported_type_counts[element["type"]] = unsupported_type_counts.get(element["type"], 0) + 1
        reason = element["unsupported_reason"]
        unsupported_reason_counts[reason] = unsupported_reason_counts.get(reason, 0) + 1
    if state["overlap_pair_count"]:
        status = "fail"
    elif (
        unresolved_element_count
        or state["unresolved_pair_count"]
        or untested_pair_count
    ):
        status = "incomplete"
    else:
        status = "pass"

    pairs, unresolved_rows = _finish_details(detail_heap)
    relevant_detail_count = (
        state["overlap_pair_count"]
        + state["contact_or_below_tolerance_pair_count"]
        + state["unresolved_pair_count"]
        + unresolved_element_count
    )
    summary = {
        "element_count": len(prepared),
        "supported_element_count": len(known),
        "unsupported_element_count": len(unsupported),
        "unsupported_type_counts": dict(sorted(unsupported_type_counts.items())),
        "unsupported_reason_counts": dict(sorted(unsupported_reason_counts.items())),
        "invalid_geometry_element_count": sum(geometry_issue_counts.values()),
        "hull_separable_geometry_element_count": sum(
            1 for element in known
            if not element["geometry"]["valid"]
            and element["geometry"]["reason"] in _HULL_SEPARATION_REASONS
            and element["geometry"]["hull"] is not None
        ),
        "geometry_issue_counts": dict(sorted(geometry_issue_counts.items())),
        "total_pair_count": total_pair_count,
        "unsupported_related_pair_count": unsupported_related_pair_count,
        "broadphase_separated_pair_count": state["broadphase_separated_pair_count"],
        "candidate_pair_count": state["candidate_pair_count"],
        "candidate_pair_count_complete": not state["stopped"],
        "narrowphase_checked_pair_count": state["narrowphase_checked_pair_count"],
        "sat_checked_pair_count": state["sat_checked_pair_count"],
        "sat_separated_pair_count": state["sat_separated_pair_count"],
        "convex_hull_checked_pair_count": state["convex_hull_checked_pair_count"],
        "convex_hull_separated_pair_count": state["convex_hull_separated_pair_count"],
        "overlap_pair_count": state["overlap_pair_count"],
        "contact_or_below_tolerance_pair_count": state["contact_or_below_tolerance_pair_count"],
        "unresolved_element_count": unresolved_element_count,
        "unresolved_pair_count": state["unresolved_pair_count"],
        "untested_pair_count": untested_pair_count,
        "reported_pair_count": len(pairs),
        "reported_unresolved_count": len(unresolved_rows),
        "details_truncated": relevant_detail_count > len(pairs) + len(unresolved_rows),
    }
    return {
        "status": status,
        "summary": summary,
        "pairs": pairs,
        "unresolved": unresolved_rows,
        "algorithm": {
            "broadphase": "deterministic balanced AABB BVH; shared node IDs and shared faces are retained",
            "narrowphase": "convex planar polyhedron SAT using face normals and edge cross-product axes",
            "invalid_geometry_fallback": "nodal convex-hull SAT may certify separation; otherwise the pair is unresolved",
            "overlap_rule": "only geometrically valid convex planar pairs with every SAT axis overlap greater than tolerance fail",
            "pair_detail_scope": "pairs lists overlap and contact_or_below_tolerance findings; separated counts are summarized",
        },
        "tolerance": {
            "value": tolerance,
            "units": "input coordinate units",
            "semantics": "contact_or_below_tolerance may include a small gap or penetration; it does not assert exact zero overlap",
        },
        "status_semantics": {
            "pass": "No proven overlap above tolerance; all eligible pairs are resolved, while contact_or_below_tolerance is not an exact-zero claim.",
            "fail": "At least one valid convex planar pair has SAT overlap greater than tolerance on every axis.",
            "incomplete": "Unsupported or degenerate input, unresolved pairs, or pairs left untested prevent a complete conclusion.",
        },
        "limits": {
            "max_candidate_pairs": max_candidate_pairs,
            "max_detail_rows": max_detail_rows,
            "candidate_budget_exceeded": state["stopped"],
        },
    }
