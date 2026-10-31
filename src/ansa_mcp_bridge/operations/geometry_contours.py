"""Build closed contours from native FACE/CONS topology and curve analysis."""

from collections import Counter, defaultdict
import math


_MAX_FACES = 20
_MAX_EDGES = 500
_TAU = 2.0 * math.pi


def _object(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f"{label} must contain exactly: {', '.join(fields)}")
    return value


def _identifier(value, label):
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _number(value, label, positive=False):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    if positive and number <= 0.0:
        raise ValueError(f"{label} must be positive")
    return number


def _vector3(value, label):
    if type(value) not in (list, tuple) or len(value) != 3:
        raise ValueError(f"{label} must have three finite components")
    return tuple(_number(component, label) for component in value)


def _distance(left, right):
    return math.hypot(*(a - b for a, b in zip(left, right)))


def _geometry_issue(edge, tolerance):
    if edge["kind"] == "native_cons":
        return None
    if edge["kind"] == "line":
        chord = _distance(edge["start"], edge["end"])
        if not math.isfinite(chord) or not math.isclose(chord, edge["length"], rel_tol=1e-6,
                abs_tol=min(tolerance, edge["length"] * 1e-6)):
            return "line endpoints do not match its length"
        return None
    radius = edge["radius"]
    precision = min(tolerance, radius * 1e-6)
    radial = tuple(edge["start"][i] - edge["center"][i] for i in range(3))
    for point in (edge["start"], edge["end"]):
        vector = tuple(point[i] - edge["center"][i] for i in range(3))
        if (abs(math.hypot(*vector) - radius) > precision
                or abs(math.fsum(vector[i] * edge["axis"][i] for i in range(3))) > precision):
            return "endpoint does not match its circle data"
    sweep = edge["sweep_angle_radians"]
    length = radius * sweep
    if not math.isfinite(length) or not math.isclose(length, edge["length"], rel_tol=1e-6, abs_tol=precision):
        return "arc length does not match its radius and sweep"
    axis = edge["axis"]
    cross = (axis[1]*radial[2]-axis[2]*radial[1], axis[2]*radial[0]-axis[0]*radial[2],
             axis[0]*radial[1]-axis[1]*radial[0])
    axial = math.fsum(axis[i] * radial[i] for i in range(3))
    cosine, sine = math.cos(sweep), math.sin(sweep)
    rotated = tuple(edge["center"][i] + cosine*radial[i] + sine*cross[i] + (1.-cosine)*axial*axis[i]
                    for i in range(3))
    if _distance(rotated, edge["end"]) > precision:
        return "arc endpoint does not match its signed sweep"
    return None


def _parse_edges(edges):
    if type(edges) is not list or len(edges) > _MAX_EDGES:
        raise ValueError(f"edges must be a list of at most {_MAX_EDGES} items")
    parsed = {}
    for index, raw in enumerate(edges):
        label = f"edges[{index}]"
        _object(raw, ("id", "vertex_ids", "analysis"), label)
        edge_id = _identifier(raw["id"], f"{label}.id")
        if edge_id in parsed:
            raise ValueError(f"duplicate edge id {edge_id}")
        vertex_ids = raw["vertex_ids"]
        if type(vertex_ids) is not list or len(vertex_ids) != 2:
            raise ValueError(f"{label}.vertex_ids must contain two integer IDs")
        vertices = tuple(_identifier(v, f"{label}.vertex_ids") for v in vertex_ids)
        analysis = raw["analysis"]
        if type(analysis) is not dict:
            raise ValueError(f"{label}.analysis must be an object")
        if analysis.get("status") == "topology_only":
            if analysis.get("kind") != "native_cons":
                raise ValueError(f"{label}.analysis.kind must be native_cons for topology_only data")
            item = {"id": edge_id, "vertex_ids": vertices, "supported": True,
                    "geometry_complete": False, "kind": "native_cons"}
            item["start"] = _vector3(analysis.get("start"), f"{label}.analysis.start")
            item["end"] = _vector3(analysis.get("end"), f"{label}.analysis.end")
            item["length"] = _number(analysis.get("length"), f"{label}.analysis.length", True)
        elif analysis.get("status") == "unsupported":
            item = {"id": edge_id, "vertex_ids": vertices, "supported": False,
                    "geometry_complete": False}
        elif analysis.get("status") == "supported":
            item = {"id": edge_id, "vertex_ids": vertices, "supported": True,
                    "geometry_complete": True}
        else:
            if "status" in analysis:
                raise ValueError(f"{label}.analysis.status is unsupported")
            if type(analysis.get("supported")) is not bool:
                raise ValueError(f"{label}.analysis.supported must be a boolean")
            item = {"id": edge_id, "vertex_ids": vertices,
                    "supported": analysis["supported"],
                    "geometry_complete": analysis["supported"]}
        if item["supported"] and item.get("kind") != "native_cons":
            kind = analysis.get("kind")
            if kind not in ("line", "circular_arc"):
                raise ValueError(f"{label}.analysis.kind must be line or circular_arc")
            item["kind"] = kind
            item["start"] = _vector3(analysis.get("start"), f"{label}.analysis.start")
            item["end"] = _vector3(analysis.get("end"), f"{label}.analysis.end")
            item["length"] = _number(analysis.get("length"), f"{label}.analysis.length", True)
            if kind == "circular_arc":
                item["center"] = _vector3(analysis.get("center"), f"{label}.analysis.center")
                axis = _vector3(analysis.get("axis"), f"{label}.analysis.axis")
                axis_norm = math.hypot(*axis)
                if not math.isclose(axis_norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
                    raise ValueError(f"{label}.analysis.axis must be a unit vector")
                item["axis"] = tuple(component / axis_norm for component in axis)
                item["radius"] = _number(analysis.get("radius"), f"{label}.analysis.radius", True)
                sweep = _number(analysis.get("sweep_angle_radians"),
                                f"{label}.analysis.sweep_angle_radians", True)
                if sweep > _TAU:
                    raise ValueError(f"{label}.analysis.sweep_angle_radians must not exceed 2*pi")
                item["sweep_angle_radians"] = sweep
        parsed[edge_id] = item
    return parsed


def _parse_faces(face_boundaries):
    if type(face_boundaries) is not list or len(face_boundaries) > _MAX_FACES:
        raise ValueError(f"face_boundaries must be a list of at most {_MAX_FACES} items")
    parsed = {}
    unique_edges = set()
    for index, raw in enumerate(face_boundaries):
        label = f"face_boundaries[{index}]"
        _object(raw, ("face_id", "cons_ids"), label)
        face_id = _identifier(raw["face_id"], f"{label}.face_id")
        if face_id in parsed:
            raise ValueError(f"duplicate face id {face_id}")
        cons_ids = raw["cons_ids"]
        if type(cons_ids) is not list or len(cons_ids) > _MAX_EDGES:
            raise ValueError(f"{label}.cons_ids must be a list of at most {_MAX_EDGES} IDs")
        parsed[face_id] = [_identifier(value, f"{label}.cons_ids") for value in cons_ids]
        unique_edges.update(parsed[face_id])
        if len(unique_edges) > _MAX_EDGES:
            raise ValueError(f"unique FACE boundaries exceed the {_MAX_EDGES}-edge budget")
    return parsed


def _components(edge_ids, edges_by_id):
    adjacency = defaultdict(list)
    for edge_id in sorted(edge_ids):
        first, second = edges_by_id[edge_id]["vertex_ids"]
        adjacency[first].append(edge_id)
        adjacency[second].append(edge_id)

    seen_vertices = set()
    result = []
    for start in sorted(adjacency):
        if start in seen_vertices:
            continue
        pending = [start]
        seen_vertices.add(start)
        component_edges = set()
        while pending:
            vertex = pending.pop()
            for edge_id in adjacency[vertex]:
                component_edges.add(edge_id)
                for neighbor in edges_by_id[edge_id]["vertex_ids"]:
                    if neighbor not in seen_vertices:
                        seen_vertices.add(neighbor)
                        pending.append(neighbor)
        result.append(sorted(component_edges))
    return result


def _trace_loop(component, edges_by_id, adjacency):
    start_edge = min(component)
    first, second = edges_by_id[start_edge]["vertex_ids"]
    start_vertex = min(first, second)
    direction = 1 if first == start_vertex else -1
    edge_ids = []
    directions = []
    used = set()
    current_edge = start_edge
    current_vertex = start_vertex

    while current_edge not in used:
        used.add(current_edge)
        edge_ids.append(current_edge)
        directions.append(direction)
        first, second = edges_by_id[current_edge]["vertex_ids"]
        next_vertex = second if direction == 1 else first
        if next_vertex == start_vertex:
            if len(used) == len(component):
                return edge_ids, directions
            return None
        candidates = sorted({edge_id for edge_id in adjacency[next_vertex]
                             if edge_id in component and edge_id not in used})
        if len(candidates) != 1:
            return None
        current_edge = candidates[0]
        first, second = edges_by_id[current_edge]["vertex_ids"]
        if first == next_vertex:
            direction = 1
        elif second == next_vertex:
            direction = -1
        else:
            return None
        current_vertex = next_vertex
    return None


def _circular_contour(edge_ids, directions, edges_by_id, tolerance):
    arc_edges = [edges_by_id[edge_id] for edge_id in edge_ids]
    if any(edge["kind"] != "circular_arc" for edge in arc_edges):
        return None

    reference = arc_edges[0]
    centers = [edge["center"] for edge in arc_edges]
    radii = [edge["radius"] for edge in arc_edges]
    if any(_distance(center, reference["center"]) > tolerance for center in centers):
        return None
    if max(radii) - min(radii) > tolerance:
        return None

    effective_axes = [tuple(component * direction for component in edge["axis"])
                      for edge, direction in zip(arc_edges, directions)]
    axis_tolerance = max(1e-8, min(1e-3, tolerance))
    for index, axis in enumerate(effective_axes):
        if any(_distance(axis, previous) > axis_tolerance
               for previous in effective_axes[:index]):
            return None
    if math.fsum(edge["sweep_angle_radians"] for edge in arc_edges) - _TAU > tolerance:
        return None
    if _TAU - math.fsum(edge["sweep_angle_radians"] for edge in arc_edges) > tolerance:
        return None

    return {"kind": "closed_circular_contour",
            "center": list(reference["center"]),
            "axis": list(effective_axes[0]),
            "radius": reference["radius"]}


def build_contours(face_boundaries, edges, tolerance=1e-7) -> dict:
    """Build deterministic closed contour loops using native vertex IDs only."""
    tolerance = _number(tolerance, "tolerance", True)
    if tolerance > 1e-3:
        raise ValueError("tolerance must not exceed 1e-3")
    edges_by_id = _parse_edges(edges)
    faces_by_id = _parse_faces(face_boundaries)
    faces = []
    # Native vertex identity is global across the request, including shared FACE boundaries.
    global_points = defaultdict(list)
    for edge in edges_by_id.values():
        if edge["supported"]:
            for vertex, point in zip(edge["vertex_ids"], (edge["start"], edge["end"])):
                global_points[vertex].append(point)
    global_conflicts = {vertex for vertex, points in global_points.items()
        if any(_distance(left, right) > tolerance for i, left in enumerate(points) for right in points[i+1:])}

    for face_id in sorted(faces_by_id):
        references = faces_by_id[face_id]
        counts = Counter(references)
        issues = set()
        topology_complete = bool(references)
        geometry_complete = bool(references)
        duplicates = {edge_id for edge_id, count in counts.items() if count > 1}
        missing = sorted(edge_id for edge_id in counts if edge_id not in edges_by_id)
        for edge_id in sorted(duplicates):
            issues.add(f"FACE {face_id}: duplicate CONS reference {edge_id} ({counts[edge_id]} times)")
            topology_complete = False
        if missing:
            issues.add(f"FACE {face_id}: missing CONS edge data {missing}")
            topology_complete = False
            geometry_complete = False
        if not references:
            issues.add(f"FACE {face_id}: no boundary edges were provided")
            topology_complete = False
            geometry_complete = False

        edge_ids = sorted(edge_id for edge_id in counts if edge_id in edges_by_id)
        unsupported = {edge_id for edge_id in edge_ids
                       if not edges_by_id[edge_id]["supported"]}
        for edge_id in sorted(unsupported):
            issues.add(f"FACE {face_id}: CONS {edge_id} has unsupported analysis")
            topology_complete = False
            geometry_complete = False
        topology_only = {edge_id for edge_id in edge_ids
                         if not edges_by_id[edge_id]["geometry_complete"]}
        for edge_id in sorted(topology_only):
            issues.add(
                f"FACE {face_id}: CONS {edge_id} is topology-only; length is a native-card approximation"
            )
            geometry_complete = False

        invalid_geometry = set()
        for edge_id in edge_ids:
            edge = edges_by_id[edge_id]
            if not edge["supported"]:
                continue
            try:
                geometry_issue = _geometry_issue(edge, tolerance)
            except ArithmeticError:
                geometry_issue = "analytic geometry exceeds numeric precision"
            if geometry_issue:
                invalid_geometry.add(edge_id)
                topology_complete = False
                geometry_complete = False
                issues.add(f"FACE {face_id}: CONS {edge_id} {geometry_issue}")

        face_vertices = {v for edge_id in edge_ids for v in edges_by_id[edge_id]["vertex_ids"]}
        conflicting_vertices = sorted(face_vertices.intersection(global_conflicts))
        for vertex_id in conflicting_vertices:
            topology_complete = False
            geometry_complete = False
            issues.add(f"FACE {face_id}: analysis coordinates conflict at native vertex {vertex_id}")
        if conflicting_vertices:
            issues.add(f"FACE {face_id}: loops withheld because vertex coordinates conflict")

        adjacency = defaultdict(list)
        for edge_id in edge_ids:
            first, second = edges_by_id[edge_id]["vertex_ids"]
            adjacency[first].append(edge_id)
            adjacency[second].append(edge_id)

        loops = []
        for component in _components(edge_ids, edges_by_id):
            degree = Counter()
            for edge_id in component:
                first, second = edges_by_id[edge_id]["vertex_ids"]
                degree[first] += 1
                degree[second] += 1
            branch = sorted((vertex, value) for vertex, value in degree.items() if value > 2)
            open_vertices = sorted((vertex, value) for vertex, value in degree.items() if value < 2)
            if branch:
                topology_complete = False
                issues.add(f"FACE {face_id}: branch vertices {branch} in CONS component {component}")
            if open_vertices:
                topology_complete = False
                issues.add(f"FACE {face_id}: open contour vertices {open_vertices} in CONS component {component}")
            if branch or open_vertices:
                continue

            blockers = []
            if duplicates.intersection(component):
                blockers.append("duplicate CONS reference")
            if unsupported.intersection(component):
                blockers.append("unsupported edge analysis")
            if invalid_geometry.intersection(component):
                blockers.append("inconsistent arc geometry")
            if conflicting_vertices:
                blockers.append("conflicting vertex coordinates")
            if blockers:
                issues.add(
                    f"FACE {face_id}: unresolved CONS component {component} ({', '.join(blockers)})"
                )
                continue

            traced = _trace_loop(component, edges_by_id, adjacency)
            if traced is None:
                topology_complete = False
                issues.add(f"FACE {face_id}: could not trace closed CONS component {component}")
                continue
            ordered_ids, directions = traced
            circle = _circular_contour(ordered_ids, directions, edges_by_id, tolerance)
            loop = {"cons_ids": ordered_ids,
                    "directions": directions,
                    "length": math.fsum(edges_by_id[edge_id]["length"]
                                         for edge_id in ordered_ids),
                    "kind": circle["kind"] if circle else "closed_contour"}
            if any(edges_by_id[edge_id]["kind"] == "native_cons"
                   for edge_id in ordered_ids):
                loop["length_source"] = "native_card_approximation"
            if circle:
                loop.update({key: circle[key] for key in ("center", "axis", "radius")})
            loops.append(loop)

        loops.sort(key=lambda loop: (tuple(loop["cons_ids"]), tuple(loop["directions"])))
        faces.append({"face_id": face_id,
                      "status": "complete" if topology_complete else "incomplete",
                      "geometry_status": "complete" if geometry_complete and topology_complete else "incomplete",
                      "loops": loops, "issues": sorted(issues)})

    status = "incomplete" if any(face["status"] == "incomplete" for face in faces) else "complete"
    return {"status": status, "faces": faces}
