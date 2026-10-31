"""Finite, explicit O-grid recipe; native ANSA remains the mesh generator."""
from __future__ import annotations

import hashlib
import json
import math

_FIELDS = frozenset({"length", "width", "thickness", "hole_radius", "transition_half_size",
    "target_length", "thickness_layers", "hole_segments", "radial_segments", "center"})


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    return float(value)


def _segments(length, target):
    ratio = length / target
    return max(1, math.ceil(ratio - 1e-10 * max(1., abs(ratio))))


def _counts(p):
    a = p["transition_half_size"]
    nx = _segments(p["length"] / 2 - a, p["target_length"])
    ny = _segments(p["width"] / 2 - a, p["target_length"])
    na = _segments(a, p["target_length"])
    nr, layers = p["radial_segments"], p["thickness_layers"]
    quads = 4 * nx * ny + 4 * na * (nx + ny) + 8 * na * nr
    perimeter = 4 * (nx + ny + 2 * na) + p["hole_segments"]
    source_nodes = quads + perimeter // 2
    return {"x_outer": nx, "y_outer": ny, "transition": na,
        "source_quads": quads, "source_nodes": source_nodes,
        "solids": quads * layers, "nodes": source_nodes * (layers + 1),
        "boundary_shells": 2 * quads + layers * (24 * na + 10 * (nx + ny) + 8 * nr),
        "outer_shells": 2 * quads + layers * perimeter}


def normalize(params):
    if type(params) is not dict or set(params) != _FIELDS:
        raise ValueError("plate parameters must contain exactly: " + ", ".join(sorted(_FIELDS)))
    p = {}
    for name in ("length", "width", "thickness", "hole_radius", "transition_half_size", "target_length"):
        p[name] = _number(params[name], name)
        if not .001 <= p[name] <= 100000.:
            raise ValueError(name + " must be in 0.001..100000 millimeters")
    for name in ("thickness_layers", "hole_segments", "radial_segments"):
        value = params[name]
        if type(value) is not int or not 1 <= value <= (1600 if name == "hole_segments" else 100):
            raise ValueError(name + " is outside its integer budget")
        p[name] = value
    center = params["center"]
    if type(center) is not list or len(center) != 3:
        raise ValueError("center must contain three coordinates")
    p["center"] = [_number(x, "center") for x in center]
    if any(abs(x) > 1e6 for x in p["center"]):
        raise ValueError("center coordinates exceed the finite template range")
    if not 0 < p["hole_radius"] < p["transition_half_size"] < min(p["length"], p["width"]) / 2:
        raise ValueError("require 0 < hole_radius < transition_half_size < min(length,width)/2")
    if p["hole_segments"] % 8 or p["hole_segments"] < 16:
        raise ValueError("hole_segments must be a multiple of eight, at least sixteen")
    counts = _counts(p)
    if p["hole_segments"] // 8 != counts["transition"]:
        raise ValueError("hole_segments/8 must match each opposite transition edge's integer seed count")
    if counts["solids"] > 10000 or counts["boundary_shells"] > 5000 or counts["nodes"] > 15000:
        raise ValueError("recipe exceeds 10000 solids, 5000 boundary shells, or 15000 nodes")
    return p


def build_plan(params):
    p = normalize(params)
    c = _counts(p)
    a = p["transition_half_size"]
    result = {"schema": "ansa-plate-hole-o-grid", "version": 1, "params": p,
        "x_cuts": [p["center"][0] + d for d in (-a, 0., a)],
        "y_cuts": [p["center"][1] + d for d in (-a, 0., a)],
        "diagonal_directions": [[1., 1., 0.], [1., -1., 0.]],
        "nominal_axis_lengths": [p["length"] / 2 - a, a, p["width"] / 2 - a],
        "axis_segment_counts": [c["x_outer"], c["transition"], c["y_outer"]],
        "arc_segments": p["hole_segments"] // 8, "radial_segments": p["radial_segments"],
        "thickness_layers": p["thickness_layers"], "predicted_source_quads": c["source_quads"],
        "predicted_source_nodes": c["source_nodes"], "predicted_solids": c["solids"],
        "predicted_nodes": c["nodes"], "predicted_boundary_shells": c["boundary_shells"],
        "predicted_outer_shells": c["outer_shells"], "block_count": 20, "faces": 92,
        "geometry_tolerance": min(1e-4, min(p["hole_radius"], a - p["hole_radius"], p["thickness"]) / 1000),
        "usable_for_mutation": False}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    result["recipe_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def seed_for_edge(endpoints, params):
    """Match native endpoints to declared nominal edges without moving any node."""
    plan = build_plan(params)
    p, tol = plan["params"], plan["geometry_tolerance"]
    if type(endpoints) not in (list, tuple) or len(endpoints) != 2:
        raise ValueError("perimeter requires exactly two native endpoints")
    points = []
    for point in endpoints:
        if type(point) not in (list, tuple) or len(point) != 3:
            raise ValueError("perimeter endpoint must contain three coordinates")
        points.append([_number(x, "endpoint") - c for x, c in zip(point, p["center"])])
    first, second = points
    if any(abs(q[0]) > p["length"] / 2 + tol or abs(q[1]) > p["width"] / 2 + tol
           or abs(abs(q[2]) - p["thickness"] / 2) > tol for q in points):
        raise ValueError("perimeter endpoint lies outside the declared plate skin")
    dz = abs(first[2] - second[2])
    if abs(dz - p["thickness"]) <= tol and math.dist(first[:2], second[:2]) <= tol:
        return {"segments": p["thickness_layers"], "kind": "thickness"}
    if dz > tol:
        raise ValueError("perimeter is not planar or parallel to thickness")
    radii = [math.hypot(q[0], q[1]) for q in points]
    r, a = p["hole_radius"], p["transition_half_size"]
    if all(abs(v - r) <= tol for v in radii):
        angles = [math.atan2(q[1], q[0]) for q in points]
        angle = abs((angles[1] - angles[0] + math.pi) % math.tau - math.pi)
        if abs(angle - math.pi / 4) > tol / r * 4:
            raise ValueError("hole perimeter must be one explicit 45-degree arc")
        return {"segments": plan["arc_segments"], "kind": "hole_arc"}
    if min(abs(v - r) for v in radii) <= tol and max(abs(q[i]) for q in points for i in (0, 1)) <= a + tol:
        cross = first[0] * second[1] - first[1] * second[0]
        dot = first[0] * second[0] + first[1] * second[1]
        if abs(cross) > tol * sum(radii) or dot <= 0:
            raise ValueError("ring connector is not radial")
        return {"segments": p["radial_segments"], "kind": "ring_radial"}
    if min(abs(first[0] - second[0]), abs(first[1] - second[1])) > tol:
        raise ValueError("outer perimeter must be axis aligned")
    length = math.dist(first, second)
    matches = [index for index, nominal in enumerate(plan["nominal_axis_lengths"]) if abs(length - nominal) <= tol * 4]
    if not matches:
        raise ValueError("perimeter does not match a declared nominal edge length")
    counts = {plan["axis_segment_counts"][i] for i in matches}
    if len(counts) != 1:
        raise ValueError("ambiguous nominal perimeter seed count")
    return {"segments": counts.pop(), "kind": "outer_straight"}
