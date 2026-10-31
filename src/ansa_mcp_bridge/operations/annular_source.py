"""Pure-data proof of a bounded planar CTRIA3/CQUAD4 annular mesh template.

This checks the polygonal FE domain only. It neither establishes analytic CAD
equivalence/continuous CAD coverage nor authorizes a model mutation.
"""

from collections import defaultdict
import math
import re

from . import mesh_audit, mesh_planning


_OPTIONS = ("center", "axis", "inner_radius", "outer_radius", "max_chord_error", "tolerance")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _Unsupported(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise _Unsupported(reason)


def _options(options):
    if type(options) is not dict:
        raise ValueError("options must be an object")
    mesh_planning._object(options, _OPTIONS, label="options")
    center = mesh_audit._vector3(options["center"], "center")
    raw_axis = mesh_audit._vector3(options["axis"], "axis")
    scale = max(abs(value) for value in raw_axis)
    if scale == 0:
        raise ValueError("axis must have non-zero length")
    axis = mesh_planning._direction([value / scale for value in raw_axis], "axis")
    result = {"center": list(center), "axis": axis}
    for key in _OPTIONS[2:]:
        result[key] = mesh_audit._finite_number(options[key], key)
    if not 0 < result["inner_radius"] < result["outer_radius"]:
        raise ValueError("radii must satisfy 0 < inner_radius < outer_radius")
    if result["max_chord_error"] <= 0:
        raise ValueError("max_chord_error must be positive")
    if not 0 < result["tolerance"] <= 0.001:
        raise ValueError("tolerance must satisfy 0 < tolerance <= 0.001")
    return result


def _source(source, *, max_elements=200):
    if type(max_elements) is not int or max_elements not in (200, 400):
        raise ValueError("annular source budget must be 200 or 400 elements")
    if type(source) is not dict:
        raise ValueError("source must be a capture_source object")
    digest = source.get("snapshot_sha256")
    if type(digest) is not str or not _SHA256.fullmatch(digest):
        raise ValueError("source.snapshot_sha256 must be a lowercase SHA-256 digest")
    if source.get("complete", True) is not True:
        raise ValueError("source must be complete")
    raw_nodes = mesh_audit._sequence(source.get("nodes"), "source.nodes")
    raw_elements = mesh_audit._sequence(source.get("elements"), "source.elements")
    if not 1 <= len(raw_nodes) <= 4 * max_elements or not 1 <= len(raw_elements) <= max_elements:
        raise ValueError(f"source requires 1..{4 * max_elements} nodes and 1..{max_elements} elements")
    coords, elements = {}, {}
    for row in raw_nodes:
        if type(row) is not dict:
            raise ValueError("source node must be an object with id and xyz")
        ident = mesh_audit._positive_id(row.get("id"), "node.id")
        if ident in coords:
            raise ValueError(f"duplicate node ID {ident}")
        coords[ident] = mesh_audit._vector3(row.get("xyz"), f"node {ident}.xyz")
    for row in raw_elements:
        if type(row) is not dict:
            raise ValueError("source element must be an object with id, type and nodes")
        ident = mesh_audit._positive_id(row.get("id"), "element.id")
        if ident in elements:
            raise ValueError(f"duplicate element ID {ident}")
        kind = row.get("type")
        if type(kind) is not str or not kind:
            raise ValueError("element.type must be a non-empty string")
        raw_ids = mesh_audit._sequence(row.get("nodes"), f"element {ident}.nodes")
        if not 1 <= len(raw_ids) <= 800:
            raise ValueError("element.nodes requires 1..800 node IDs")
        ids = tuple(mesh_audit._positive_id(value, "element node ID") for value in raw_ids)
        if len(set(ids)) != len(ids) or not set(ids) <= coords.keys():
            raise ValueError(f"element {ident} has repeated or unknown node IDs")
        expected_nodes = {"CQUAD4": 4, "CTRIA3": 3}.get(kind)
        if expected_nodes is not None and len(ids) != expected_nodes:
            raise ValueError(f"{kind} {ident} must contain exactly {expected_nodes} node IDs")
        elements[ident] = {"type": kind, "nodes": ids}
    return coords, elements


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _area(points):
    return 0.5 * math.fsum(
        first[0] * second[1] - first[1] * second[0]
        for first, second in zip(points, points[1:] + points[:1])
    )


def _on_segment(point, first, second, eps):
    length = math.dist(first, second)
    if abs(_cross(first, second, point)) > eps * length:
        return False
    projection = math.fsum((point[i] - first[i]) * (second[i] - first[i]) for i in range(2))
    return -eps * length <= projection <= length * length + eps * length


def _embedded(points, edges, eps):
    # Checking every node also covers boundary T-junctions and overlapping edges
    # sharing an endpoint; neither is allowed to hide behind a common node ID.
    for a, b in edges:
        for ident, point in points.items():
            _require(ident in (a, b) or not _on_segment(point, points[a], points[b], eps),
                     f"node {ident} lies on another mesh edge (hanging node or overlap)")
    for index, (a, b) in enumerate(edges):
        for c, d in edges[index + 1:]:
            if {a, b} & {c, d}:
                continue
            p, q, r, s = points[a], points[b], points[c], points[d]
            touching = any((_on_segment(r, p, q, eps), _on_segment(s, p, q, eps),
                            _on_segment(p, r, s, eps), _on_segment(q, r, s, eps)))
            ab, cd = math.dist(p, q), math.dist(r, s)
            sides = (_cross(p, q, r), _cross(p, q, s), _cross(r, s, p), _cross(r, s, q))
            crossing = ((sides[0] > eps * ab and sides[1] < -eps * ab
                         or sides[1] > eps * ab and sides[0] < -eps * ab)
                        and (sides[2] > eps * cd and sides[3] < -eps * cd
                             or sides[3] > eps * cd and sides[2] < -eps * cd))
            _require(not touching and not crossing, "nonincident mesh edges intersect or overlap")


def _boundary_loops(boundary):
    outgoing, incoming = {}, {}
    for a, b in boundary:
        _require(a not in outgoing and b not in incoming, "boundary is not a set of simple directed loops")
        outgoing[a], incoming[b] = b, a
    _require(set(outgoing) == set(incoming), "boundary loop is open")
    remaining, loops = set(outgoing), []
    while remaining:
        start = min(remaining)
        loop, current = [], start
        while current in remaining:
            loop.append(current)
            remaining.remove(current)
            current = outgoing[current]
        _require(current == start and len(loop) >= 3, "boundary loop is not simple and closed")
        loops.append(loop)
    _require(len(loops) == 2, "annular source requires exactly two boundary loops")
    return loops


def _loop_report(ids, points, radius, direction, scale):
    polygon = [points[ident] for ident in ids]
    increments = []
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        cross = first[0] * second[1] - first[1] * second[0]
        dot = first[0] * second[0] + first[1] * second[1]
        angle = direction * math.atan2(cross, dot)
        _require(0 < angle < math.pi, "boundary angles are not strictly monotone around the center")
        increments.append(angle)
    sweep = math.fsum(increments)
    _require(abs(sweep - math.tau) <= 128 * len(ids) * math.ulp(math.tau),
             "boundary does not wind exactly once around the center")
    signed_area = _area(polygon)
    _require(direction * signed_area > 0, "boundary polygon orientation is inconsistent")
    angle = max(increments)
    area = abs(signed_area) * scale * scale
    _require(math.isfinite(area) and area > 0, "boundary area is outside finite numeric range")
    return {"node_ids": ids, "radius": radius, "orientation": direction,
            "angular_sweep_radians": sweep, "maximum_edge_angle_radians": angle,
            "polygon_area": area, "maximum_chord_error": radius * (1 - math.cos(angle / 2))}


def _geometry(coords, elements, options, result):
    _require(all(row["type"] in ("CQUAD4", "CTRIA3") for row in elements.values()),
             "only linear CTRIA3 and CQUAD4 source elements are supported")
    used = {ident for row in elements.values() for ident in row["nodes"]}
    _require(used == coords.keys(), "source contains unused nodes")
    center, axis = options["center"], options["axis"]
    radius, tolerance = options["outer_radius"], options["tolerance"]
    eps = tolerance / radius
    _require(math.isfinite(eps) and eps > 0, "tolerance cannot be represented relative to the outer radius")
    reference = [0.0, 0.0, 0.0]
    reference[min(range(3), key=lambda i: abs(axis[i]))] = 1.0
    u = mesh_planning._direction(mesh_audit._add_cross(reference, axis), "plane basis")
    v = mesh_audit._add_cross(axis, u)
    points, radii, plane_distances = {}, {}, []
    for ident, point in coords.items():
        delta = mesh_audit._sub(point, center)
        axial = mesh_audit._dot(delta, axis)
        _require(math.isfinite(axial) and abs(axial) <= tolerance,
                 "source is not in the axis-normal plane through center")
        scaled = tuple(value / radius for value in delta)
        projected = (mesh_audit._dot(scaled, u), mesh_audit._dot(scaled, v))
        radial = math.hypot(*projected)
        _require(math.isfinite(radial)
                 and options["inner_radius"] / radius - eps <= radial <= 1 + eps,
                 "source node lies outside the declared radial range")
        points[ident], radii[ident] = projected, radial
        plane_distances.append(abs(axial))
    node_ids = sorted(points)
    for index, ident in enumerate(node_ids):
        for other in node_ids[index + 1:]:
            _require(math.dist(points[ident], points[other]) > eps,
                     f"different node IDs {ident}/{other} have coincident coordinates within tolerance")

    uses, neighbors, areas = defaultdict(list), {ident: set() for ident in elements}, []
    orientation = None
    for ident, row in sorted(elements.items()):
        ids = row["nodes"]
        polygon = [points[node] for node in ids]
        turns = []
        for i in range(len(ids)):
            a, b, c = polygon[i], polygon[(i + 1) % len(ids)], polygon[(i + 2) % len(ids)]
            turn = _cross(a, b, c)
            _require(abs(turn) > eps * max(math.dist(a, b), math.dist(b, c)),
                     f"element {ident} is degenerate or not strictly convex")
            turns.append(1 if turn > 0 else -1)
        _require(len(set(turns)) == 1, f"element {ident} is not strictly convex")
        if orientation is None:
            orientation = turns[0]
        _require(turns[0] == orientation, "source element orientations are inconsistent/inverted")
        element_area = abs(_area(polygon))
        _require(math.isfinite(element_area) and element_area > 0,
                 f"element {ident} does not have positive area")
        areas.append(element_area)
        for a, b in zip(ids, ids[1:] + ids[:1]):
            _require(math.dist(points[a], points[b]) > eps, "mesh edge is degenerate")
            uses[mesh_planning._edge_key(a, b)].append((a, b, ident))
    boundary = []
    for owners in uses.values():
        _require(len(owners) in (1, 2), "mesh edge has nonmanifold or overlapping uses")
        if len(owners) == 1:
            boundary.append(owners[0][:2])
        else:
            a, b, first = owners[0]
            c, d, second = owners[1]
            _require((a, b) == (d, c), "internal edge uses are not exactly opposite")
            neighbors[first].add(second)
            neighbors[second].add(first)
    _embedded(points, sorted(uses), eps)
    _require(len(mesh_audit._components(elements, neighbors)) == 1, "source mesh is not edge-connected")
    loops = _boundary_loops(boundary)
    _require(len(coords) - len(uses) + len(elements) == 0, "annular source Euler characteristic is not zero")
    boundaries = {}
    for loop in loops:
        matches = [name for name in ("inner", "outer")
                   if all(abs(radii[ident] - options[name + "_radius"] / radius) <= eps for ident in loop)]
        _require(len(matches) == 1 and matches[0] not in boundaries,
                 "boundary loop does not uniquely match the inner or outer radius")
        name = matches[0]
        boundaries[name] = _loop_report(loop, points, options[name + "_radius"],
                                        orientation if name == "outer" else -orientation, radius)
    _require(set(boundaries) == {"inner", "outer"}, "both inner and outer circular boundary loops are required")
    result["boundaries"] = boundaries
    polygon_area = (boundaries["outer"]["polygon_area"] - boundaries["inner"]["polygon_area"])
    area_sum = math.fsum(areas) * radius * radius
    roundoff = 64 * math.ulp(1.0) * boundaries["outer"]["polygon_area"]
    _require(math.isfinite(area_sum) and polygon_area > 0 and abs(area_sum - polygon_area) <= roundoff,
             "positive element areas do not equal outer polygon area minus inner polygon area")
    result["metrics"] = {
        "node_count": len(coords), "element_count": len(elements), "edge_count": len(uses),
        "internal_edge_count": len(uses) - len(boundary), "boundary_edge_count": len(boundary),
        "euler_characteristic": 0, "orientation": orientation,
        "maximum_plane_distance": max(plane_distances), "element_area_sum": area_sum,
        "annular_polygon_area": polygon_area, "area_residual": area_sum - polygon_area,
    }
    for name, loop in boundaries.items():
        _require(loop["maximum_chord_error"] <= options["max_chord_error"],
                 f"{name} boundary theoretical chord error exceeds max_chord_error")


def audit(source, options, *, max_elements=200):
    """Audit raw capture_source lists against six explicit annular options.

    Malformed data/options raise ValueError. Unsupported FE geometry returns a
    reason with status='unsupported'; success is 'verified_mesh_template'.
    snapshot_sha256 is retained as the caller's capture identifier. Neither
    outcome proves original CAD coverage or provides mutation authority.
    """
    normalized = _options(options)
    coords, elements = _source(source, max_elements=max_elements)
    result = {"status": "unsupported", "usable_for_mutation": False,
              "original_CAD_coverage_proven": False,
              "source_snapshot_sha256": source["snapshot_sha256"], "options": normalized,
              "limitations": ["polygonal FE annulus and hole preservation only",
                              "original analytic CAD equivalence and continuous CAD coverage are not proven"]}
    try:
        _geometry(coords, elements, normalized, result)
    except _Unsupported as exc:
        result["reason"] = str(exc)
    else:
        result["status"] = "verified_mesh_template"
    return result
