"""Pure-data audit for one selected planar NASTRAN shell macro.

Callers provide the already captured FE mesh and native open-CONS paths. The
core owns saved-model, millimeter, deck, first-order, and straight-CONS gates;
this module only checks their explicit FE and topology consequences.
"""

from collections import defaultdict
import math


_MAX_ELEMENTS = 2000
_MAX_NODES = 10000
_ELEMENT_SIZES = {"CQUAD4": 4, "CTRIA3": 3}


def _sequence(value, label):
    if type(value) not in (list, tuple):
        raise ValueError(f"{label} must be a sequence")
    return value


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _vector3(value, label):
    values = _sequence(value, label)
    if len(values) != 3:
        raise ValueError(f"{label} must contain exactly three coordinates")
    return tuple(_number(item, f"{label}[{index}]")
                 for index, item in enumerate(values))


def _unit_normal(value):
    vector = _vector3(value, "normal")
    scale = max(abs(component) for component in vector)
    if scale == 0.0:
        raise ValueError("normal must be finite and non-zero")
    reduced = tuple(component / scale for component in vector)
    length = math.hypot(*reduced)
    if not math.isfinite(length) or length == 0.0:
        raise ValueError("normal must be finite and non-zero")
    return tuple(component / length for component in reduced)


def _cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _dot(first, second):
    return math.fsum(a * b for a, b in zip(first, second))


def _unit_vector(vector, label):
    length = math.hypot(*vector)
    if not math.isfinite(length) or length == 0.0:
        raise ValueError(f"cannot construct a finite plane basis for {label}")
    return tuple(component / length for component in vector)


def _nodes(nodes):
    if type(nodes) is not dict:
        raise ValueError("nodes must be an object keyed by positive GRID IDs")
    if len(nodes) > _MAX_NODES:
        raise ValueError(f"surface exceeds {_MAX_NODES} nodes")
    normalized = {}
    for node_id, xyz in nodes.items():
        ident = _positive_id(node_id, "node ID")
        normalized[ident] = _vector3(xyz, f"node {ident} coordinates")
    return normalized


def _elements(elements):
    rows = _sequence(elements, "elements")
    if len(rows) > _MAX_ELEMENTS:
        raise ValueError(f"surface exceeds {_MAX_ELEMENTS} shell elements")
    if not rows:
        raise ValueError("surface must contain at least one shell element")

    normalized = {}
    topology_owners = {}
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise ValueError(f"elements[{index}] must be an object")
        ident = _positive_id(row.get("id"), f"elements[{index}].id")
        if ident in normalized:
            raise ValueError(f"duplicate shell element ID {ident}")
        element_type = row.get("element_type")
        if element_type not in _ELEMENT_SIZES:
            raise ValueError(
                f"element {ident}.element_type must be CQUAD4 or CTRIA3"
            )
        raw_node_ids = _sequence(
            row.get("node_ids"), f"element {ident}.node_ids"
        )
        expected = _ELEMENT_SIZES[element_type]
        if len(raw_node_ids) != expected:
            raise ValueError(
                f"{element_type} {ident} must contain exactly {expected} node IDs"
            )
        node_ids = tuple(
            _positive_id(value, f"element {ident}.node_ids[{position}]")
            for position, value in enumerate(raw_node_ids)
        )
        if len(set(node_ids)) != len(node_ids):
            raise ValueError(f"element {ident} contains duplicate node references")
        topology_key = tuple(sorted(node_ids))
        if topology_key in topology_owners:
            raise ValueError(
                f"elements {topology_owners[topology_key]} and {ident} "
                "contain duplicate cell topology"
            )
        topology_owners[topology_key] = ident
        normalized[ident] = {
            "element_type": element_type,
            "node_ids": node_ids,
        }
    return normalized


def _plane_projection(nodes, raw_normal, tolerance):
    if not nodes:
        raise ValueError("surface must contain at least one node")
    tau = _number(tolerance, "tolerance")
    if tau < 0.0:
        raise ValueError("tolerance must be non-negative")

    normal = _unit_normal(raw_normal)
    axis_index = min(range(3), key=lambda index: abs(normal[index]))
    axis = tuple(1.0 if index == axis_index else 0.0 for index in range(3))
    basis_u = _unit_vector(_cross(axis, normal), "normal")
    basis_v = _cross(normal, basis_u)
    origin = next(iter(nodes.values()))

    projected = {}
    for node_id, point in nodes.items():
        delta = tuple(point[index] - origin[index] for index in range(3))
        if not all(math.isfinite(value) for value in delta):
            raise ValueError(f"node {node_id} coordinates exceed numeric range")
        distance = _dot(delta, normal)
        if not math.isfinite(distance) or abs(distance) > tau:
            raise ValueError(
                f"node {node_id} is not coplanar within tolerance={tau}"
            )
        uv = (_dot(delta, basis_u), _dot(delta, basis_v))
        if not all(math.isfinite(value) for value in uv):
            raise ValueError(f"node {node_id} plane projection is not finite")
        projected[node_id] = uv
    return projected


def _cross2(first, second, third):
    return (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _element_geometry(cells, projected):
    areas = []
    for element_id, cell in sorted(cells.items()):
        node_ids = cell["node_ids"]
        points = [projected[node_id] for node_id in node_ids]
        turns = []
        for index, point in enumerate(points):
            previous = points[index - 1]
            following = points[(index + 1) % len(points)]
            turn = _cross2(previous, point, following)
            if not math.isfinite(turn):
                raise ValueError(
                    f"element {element_id} corner Jacobian is not finite"
                )
            turns.append(turn)

        if cell["element_type"] == "CTRIA3":
            if any(turn < 0.0 for turn in turns):
                raise ValueError(f"triangle {element_id} is flipped or folded")
            if any(turn == 0.0 for turn in turns):
                raise ValueError(f"triangle {element_id} has zero area")
        else:
            if any(turn <= 0.0 for turn in turns):
                raise ValueError(
                    f"quad {element_id} has a negative corner Jacobian, "
                    "is concave, or is folded"
                )

        twice_area = math.fsum(
            point[0] * following[1] - following[0] * point[1]
            for point, following in zip(points, points[1:] + points[:1])
        )
        if not math.isfinite(twice_area):
            raise ValueError(f"element {element_id} area is not finite")
        area = 0.5 * twice_area
        if area == 0.0:
            raise ValueError(f"element {element_id} has zero area")
        if area < 0.0:
            raise ValueError(f"element {element_id} is flipped")
        areas.append(area)
    return areas


def _edge_topology(cells):
    uses = defaultdict(list)
    for element_id, cell in sorted(cells.items()):
        node_ids = cell["node_ids"]
        for first, second in zip(node_ids, node_ids[1:] + node_ids[:1]):
            uses[tuple(sorted((first, second)))].append(
                (first, second, element_id)
            )

    boundary_edges = set()
    neighbors = {element_id: set() for element_id in cells}
    for edge, owners in sorted(uses.items()):
        if len(owners) > 2:
            raise ValueError(
                f"edge {edge} is non-manifold with {len(owners)} uses"
            )
        if len(owners) == 1:
            boundary_edges.add(edge)
            continue
        first, second = owners
        if first[:2] != (second[1], second[0]):
            raise ValueError(
                f"internal edge {edge} has two shell uses in the same direction"
            )
        first_element, second_element = first[2], second[2]
        if first_element == second_element:
            raise ValueError(f"internal edge {edge} is used twice by one cell")
        neighbors[first_element].add(second_element)
        neighbors[second_element].add(first_element)
    return uses, boundary_edges, neighbors


def _boundary_references(boundary_paths, nodes):
    paths = _sequence(boundary_paths, "boundary_paths")
    references = set()
    for path_index, raw_path in enumerate(paths):
        path = _sequence(raw_path, f"boundary_paths[{path_index}]")
        if len(path) < 2:
            raise ValueError(
                f"boundary_paths[{path_index}] must contain at least two GRID IDs"
            )
        node_ids = tuple(
            _positive_id(value, f"boundary_paths[{path_index}][{index}]")
            for index, value in enumerate(path)
        )
        missing = sorted(set(node_ids) - nodes.keys())
        if missing:
            raise ValueError(
                f"boundary_paths[{path_index}] references unknown GRID IDs {missing}"
            )
        if len(set(node_ids)) != len(node_ids):
            raise ValueError(
                f"boundary_paths[{path_index}] must be an open simple chain "
                "without repeated GRID IDs"
            )
        for first, second in zip(node_ids, node_ids[1:]):
            edge = tuple(sorted((first, second)))
            if edge in references:
                raise ValueError(
                    f"boundary reference contains duplicate edge {edge}"
                )
            references.add(edge)
    return references


def _compare_boundaries(actual, references):
    if actual == references:
        return
    unexpected = sorted(actual - references)
    missing = sorted(references - actual)
    raise ValueError(
        "actual single-use edge set differs from boundary_paths: "
        f"unexpected={unexpected}, missing={missing}"
    )


def _require_edge_connected(cells, neighbors):
    remaining = set(cells)
    components = 0
    while remaining:
        components += 1
        pending = [next(iter(remaining))]
        reached = set()
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(neighbors[current] - reached)
        remaining.difference_update(reached)
    if components != 1:
        raise ValueError(
            "internal-edge shell adjacency graph is disconnected; "
            "point contact does not connect cells"
        )


def _boundary_loops(boundary_edges):
    adjacency = defaultdict(set)
    for first, second in boundary_edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    for node_id, adjacent in sorted(adjacency.items()):
        if len(adjacent) != 2:
            raise ValueError(
                f"boundary vertex {node_id} has degree {len(adjacent)}, expected 2"
            )

    remaining = set(adjacency)
    loop_count = 0
    while remaining:
        start = next(iter(remaining))
        loop = [start]
        seen = {start}
        previous = None
        current = start
        while True:
            choices = adjacency[current] - ({previous} if previous is not None else set())
            if not choices:
                raise ValueError("boundary graph does not form a closed loop")
            following = min(choices)
            if following == start:
                break
            if following in seen:
                raise ValueError("boundary graph contains a non-simple loop")
            loop.append(following)
            seen.add(following)
            previous, current = current, following
        if len(loop) < 3:
            raise ValueError("boundary loop contains fewer than three vertices")
        loop_count += 1
        remaining.difference_update(loop)
    return loop_count


def audit_surface(elements, nodes, boundary_paths, normal, *, tolerance=1e-7):
    """Validate one planar shell macro and return a JSON-serializable summary.

    The caller guarantees one saved NASTRAN millimeter FACE macro with
    first-order, straight open CONS paths and an existing surface mesh.
    ``boundary_paths`` contain only the selected macro's CONs. A shared
    perimeter can belong to an outside meshed macro because matching uses the
    complete selected-FE single-use edge set; outside cells are not inferred.

    Success covers explicit FE topology, orientation, and supplied boundary
    identity only. It does not prove continuous CAD coverage.
    """
    normalized_nodes = _nodes(nodes)
    cells = _elements(elements)
    referenced_nodes = {
        node_id for cell in cells.values() for node_id in cell["node_ids"]
    }
    missing_nodes = sorted(referenced_nodes - normalized_nodes.keys())
    if missing_nodes:
        raise ValueError(
            f"surface elements reference unknown node IDs {missing_nodes}"
        )
    unused_nodes = sorted(normalized_nodes.keys() - referenced_nodes)
    if unused_nodes:
        raise ValueError(f"surface contains unused node IDs {unused_nodes}")

    uses, boundary_edges, neighbors = _edge_topology(cells)
    references = _boundary_references(boundary_paths, normalized_nodes)
    _compare_boundaries(boundary_edges, references)
    _require_edge_connected(cells, neighbors)
    boundary_loop_count = _boundary_loops(boundary_edges)

    vertex_count = len(normalized_nodes)
    edge_count = len(uses)
    element_count = len(cells)
    euler = vertex_count - edge_count + element_count
    expected_euler = 2 - boundary_loop_count
    if euler != expected_euler:
        raise ValueError(
            f"Euler characteristic is {euler}, expected {expected_euler} "
            f"for {boundary_loop_count} boundary loops"
        )

    projected = _plane_projection(normalized_nodes, normal, tolerance)
    areas = _element_geometry(cells, projected)
    total_area = math.fsum(areas)
    if not math.isfinite(total_area) or total_area <= 0.0:
        raise ValueError("total surface area must be finite and positive")
    quad_count = sum(
        cell["element_type"] == "CQUAD4" for cell in cells.values()
    )
    tria_count = sum(
        cell["element_type"] == "CTRIA3" for cell in cells.values()
    )
    return {
        "shell_count": element_count,
        "node_count": vertex_count,
        "quad_count": quad_count,
        "tria_count": tria_count,
        "boundary_edge_count": len(boundary_edges),
        "internal_edge_count": edge_count - len(boundary_edges),
        "boundary_loop_count": boundary_loop_count,
        "euler": euler,
        "area": total_area,
        "connected": True,
    }
