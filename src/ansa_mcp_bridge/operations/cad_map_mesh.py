"""求解器中立的平移 Map 网格快照审计。"""

from itertools import product
import math

from .hexa_meshing import _hexa_integration
from .mesh_audit import _finite_number, _positive_id, _sequence, _vector3


_MAX_SOLIDS = 5000
_MAX_NODES = 10000
_UNIT_DIRECTION_TOLERANCE = 1e-12
_NEIGHBOR_CELLS = tuple(product((-1, 0, 1), repeat=3))
_JACOBIAN_SCOPE = (
    "Center and 2x2x2 Gauss points only; this sampling does not prove positive "
    "Jacobian over the full domain of an arbitrarily distorted CHEXA."
)


class _SpatialIndex:
    """用容差大小的空间桶查找近点，避免逐对全量比较。"""

    def __init__(self, tolerance):
        self.tolerance = tolerance
        self.buckets = {}

    def _key(self, point):
        key = []
        for value in point:
            value_num, value_den = value.as_integer_ratio()
            size_num, size_den = self.tolerance.as_integer_ratio()
            key.append((value_num * size_den) // (value_den * size_num))
        return tuple(key)

    def _nearby_entries(self, point):
        key = self._key(point)
        for offset in _NEIGHBOR_CELLS:
            bucket_key = tuple(key[axis] + offset[axis] for axis in range(3))
            yield from self.buckets.get(bucket_key, ())

    def add_unique(self, point, payload, label):
        for other, _ in self._nearby_entries(point):
            if math.dist(point, other) <= self.tolerance:
                raise ValueError(f"{label} contain duplicate or tolerance-ambiguous coordinates")
        self.buckets.setdefault(self._key(point), []).append((point, payload))

    def matches(self, point):
        return [
            (payload, math.dist(point, other))
            for other, payload in self._nearby_entries(point)
            if math.dist(point, other) <= self.tolerance
        ]


def _snapshot_rows(snapshot, label):
    if type(snapshot) is not dict:
        raise ValueError(f"{label} must be a dict")
    return (
        _sequence(snapshot.get("nodes"), f"{label}.nodes"),
        _sequence(snapshot.get("elements"), f"{label}.elements"),
    )


def _read_nodes(rows, label, tolerance):
    if len(rows) > _MAX_NODES:
        raise ValueError(f"{label} exceeds the {_MAX_NODES}-node budget")
    coords = {}
    unique_positions = _SpatialIndex(tolerance)
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise ValueError(f"{label}[{index}] must be a dict")
        node_id = _positive_id(row.get("id"), f"{label}[{index}].id")
        if node_id in coords:
            raise ValueError(f"{label} contains duplicate node ID {node_id}")
        point = _vector3(row.get("xyz"), f"{label} node {node_id}.xyz")
        unique_positions.add_unique(point, node_id, label)
        coords[node_id] = point
    return coords


def _read_elements(rows, label, expected_type, arity, node_ids):
    elements = []
    ids = set()
    connectivities = {}
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise ValueError(f"{label}[{index}] must be a dict")
        element_id = _positive_id(row.get("id"), f"{label}[{index}].id")
        if element_id in ids:
            raise ValueError(f"{label} contains duplicate element ID {element_id}")
        ids.add(element_id)
        element_type = row.get("type")
        if type(element_type) is not str or not element_type.strip():
            raise ValueError(f"{label} element {element_id}.type must be a non-empty string")
        element_type = element_type.strip().upper()
        if element_type != expected_type:
            raise ValueError(
                f"{label} element {element_id} must have topology {expected_type}"
            )
        raw_nodes = _sequence(row.get("nodes"), f"{label} element {element_id}.nodes")
        if len(raw_nodes) != arity:
            raise ValueError(
                f"{label} element {element_id} must reference exactly {arity} nodes"
            )
        connectivity = tuple(
            _positive_id(node_id, f"{label} element {element_id}.nodes[{node_index}]")
            for node_index, node_id in enumerate(raw_nodes)
        )
        if len(set(connectivity)) != arity:
            raise ValueError(f"{label} element {element_id} contains duplicate node IDs")
        missing = sorted(set(connectivity) - node_ids)
        if missing:
            raise ValueError(f"{label} element {element_id} references unknown node IDs {missing}")
        canonical = tuple(sorted(connectivity))
        if canonical in connectivities:
            raise ValueError(
                f"{label} elements {connectivities[canonical]} and {element_id} "
                "have duplicate node connectivity"
            )
        connectivities[canonical] = element_id
        elements.append({"id": element_id, "nodes": connectivity})
    return elements


def _is_quad_cycle(actual, expected):
    for sequence in (expected, tuple(reversed(expected))):
        for offset in range(4):
            if actual == sequence[offset:] + sequence[:offset]:
                return True
    return False


def verify_translation(source, output, direction, distance, steps, tolerance=1e-7):
    """核验四边源面按单位方向平移生成的 CHEXA 连接、节点和体积。"""
    direction = _vector3(direction, "direction")
    direction_norm = math.hypot(*direction)
    if abs(direction_norm - 1.0) > _UNIT_DIRECTION_TOLERANCE:
        raise ValueError("direction must be a unit vector")
    distance = _finite_number(distance, "distance")
    if distance <= 0.0:
        raise ValueError("distance must be positive")
    if type(steps) is not int or steps <= 0:
        raise ValueError("steps must be a positive integer")
    tolerance = _finite_number(tolerance, "tolerance")
    if tolerance <= 0.0:
        raise ValueError("tolerance must be positive")

    source_node_rows, source_element_rows = _snapshot_rows(source, "source")
    output_node_rows, output_element_rows = _snapshot_rows(output, "output")
    if not source_element_rows:
        raise ValueError("source must contain at least one CQUAD4")
    if len(source_element_rows) * steps > _MAX_SOLIDS:
        raise ValueError(f"translation exceeds the {_MAX_SOLIDS}-solid budget")
    if len(source_node_rows) > _MAX_NODES:
        raise ValueError(f"source exceeds the {_MAX_NODES}-node budget")
    expected_node_count = len(source_node_rows) * (steps + 1)
    if expected_node_count > _MAX_NODES:
        raise ValueError(f"translated mesh exceeds the {_MAX_NODES}-node budget")
    if len(output_node_rows) > _MAX_NODES:
        raise ValueError(f"output exceeds the {_MAX_NODES}-node budget")
    if len(output_element_rows) > _MAX_SOLIDS:
        raise ValueError(f"output exceeds the {_MAX_SOLIDS}-solid budget")
    if len(output_node_rows) != expected_node_count:
        raise ValueError(
            f"expected exactly {expected_node_count} output nodes; "
            f"read {len(output_node_rows)} (missing or extra nodes)"
        )
    expected_solid_count = len(source_element_rows) * steps
    if len(output_element_rows) != expected_solid_count:
        raise ValueError(
            f"expected exactly {expected_solid_count} CHEXA elements; "
            f"read {len(output_element_rows)} (missing or extra cells)"
        )

    source_coords = _read_nodes(source_node_rows, "source.nodes", tolerance)
    source_elements = _read_elements(
        source_element_rows, "source.elements", "CQUAD4", 4, set(source_coords)
    )
    referenced_source_nodes = {
        node_id for element in source_elements for node_id in element["nodes"]
    }
    if referenced_source_nodes != set(source_coords):
        unused = sorted(set(source_coords) - referenced_source_nodes)
        raise ValueError(f"source contains nodes unused by its CQUAD4 faces: {unused}")

    face_by_nodes = {}
    for element in source_elements:
        face_key = frozenset(element["nodes"])
        if face_key in face_by_nodes:
            raise ValueError(
                f"source CQUAD4 elements {face_by_nodes[face_key]} and {element['id']} "
                "have duplicate face connectivity"
            )
        face_by_nodes[face_key] = element["id"]
    element_by_id = {element["id"]: element for element in source_elements}

    step_delta = tuple(component * (distance / steps) for component in direction)
    expected_positions = _SpatialIndex(tolerance)
    expected_by_pair = {}
    for source_id in sorted(source_coords):
        origin = source_coords[source_id]
        for layer in range(steps + 1):
            point = tuple(origin[axis] + step_delta[axis] * layer for axis in range(3))
            if not all(math.isfinite(value) for value in point):
                raise ValueError(
                    f"translated position for source node {source_id}, layer {layer} is not finite"
                )
            pair = (source_id, layer)
            expected_positions.add_unique(point, pair, "expected source/layer positions")
            expected_by_pair[pair] = point

    output_coords = _read_nodes(output_node_rows, "output.nodes", tolerance)
    output_node_to_pair = {}
    pair_to_output_node = {}
    max_coordinate_error = 0.0
    for node_id in sorted(output_coords):
        point = output_coords[node_id]
        matches = expected_positions.matches(point)
        if len(matches) != 1:
            raise ValueError(
                f"output GRID {node_id} has {len(matches)} source/layer position matches"
            )
        pair, coordinate_error = matches[0]
        if pair in pair_to_output_node:
            raise ValueError(f"multiple output GRIDs map to source node/layer {pair}")
        if pair[1] == 0 and node_id != pair[0]:
            raise ValueError(
                f"layer 0 for source node {pair[0]} must retain its original node ID"
            )
        pair_to_output_node[pair] = node_id
        output_node_to_pair[node_id] = pair
        max_coordinate_error = max(max_coordinate_error, coordinate_error)
    if len(pair_to_output_node) != len(expected_by_pair):
        missing = sorted(set(expected_by_pair) - set(pair_to_output_node))
        raise ValueError(f"translated output is missing source/layer nodes: {missing[:8]}")

    output_elements = _read_elements(
        output_element_rows, "output.elements", "CHEXA", 8, set(output_coords)
    )
    expected_cells = {
        (element["id"], layer)
        for element in source_elements
        for layer in range(1, steps + 1)
    }
    matched_cells = {}
    volumes = []
    minimum_jacobian = math.inf

    for element in output_elements:
        element_id = element["id"]
        connectivity = element["nodes"]
        mapped_nodes = [output_node_to_pair[node_id] for node_id in connectivity]
        first_face, second_face = mapped_nodes[:4], mapped_nodes[4:]
        first_layers = {pair[1] for pair in first_face}
        second_layers = {pair[1] for pair in second_face}
        if len(first_layers) != 1 or len(second_layers) != 1:
            raise ValueError(f"CHEXA {element_id} does not connect two adjacent layers")
        first_layer = next(iter(first_layers))
        second_layer = next(iter(second_layers))
        if abs(first_layer - second_layer) != 1:
            raise ValueError(f"CHEXA {element_id} does not connect adjacent translation layers")
        layer = max(first_layer, second_layer)
        if not 1 <= layer <= steps:
            raise ValueError(f"CHEXA {element_id} maps outside the translated layer range")
        first_source_ids = tuple(pair[0] for pair in first_face)
        second_source_ids = tuple(pair[0] for pair in second_face)
        if first_source_ids != second_source_ids:
            raise ValueError(f"CHEXA {element_id} does not pair corresponding source nodes")
        source_element_id = face_by_nodes.get(frozenset(first_source_ids))
        if source_element_id is None:
            raise ValueError(f"CHEXA {element_id} has no matching source CQUAD4 face")
        source_face = element_by_id[source_element_id]["nodes"]
        if not _is_quad_cycle(first_source_ids, source_face):
            raise ValueError(f"CHEXA {element_id} connectivity changes the source quad edges")
        cell_key = (source_element_id, layer)
        if cell_key not in expected_cells:
            raise ValueError(f"CHEXA {element_id} maps outside the expected source/layer cells")
        if cell_key in matched_cells:
            raise ValueError(f"source CQUAD4 {source_element_id}, layer {layer} is duplicated")
        matched_cells[cell_key] = element_id

        points = [output_coords[node_id] for node_id in connectivity]
        try:
            volume, sampled_jacobian, _ = _hexa_integration(points)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"CHEXA {element_id} failed Jacobian/volume validation: {exc}") from exc
        if not math.isfinite(volume) or volume <= 0.0:
            raise ValueError(f"CHEXA {element_id} has no positive finite volume")
        volumes.append(volume)
        minimum_jacobian = min(minimum_jacobian, sampled_jacobian)

    if set(matched_cells) != expected_cells:
        missing = sorted(expected_cells - set(matched_cells))
        raise ValueError(f"translated output has missing source/layer cells: {missing[:8]}")
    try:
        total_volume = math.fsum(volumes)
    except OverflowError as exc:
        raise ValueError("total translated volume is not finite") from exc
    if not math.isfinite(total_volume) or total_volume <= 0.0:
        raise ValueError("total translated volume must be positive and finite")

    return {
        "node_layer_pairs": [
            {
                "source_node_id": source_id,
                "layer": layer,
                "output_node_id": pair_to_output_node[(source_id, layer)],
            }
            for source_id in sorted(source_coords)
            for layer in range(steps + 1)
        ],
        "source_cell_matches": [
            {
                "source_element_id": source_element_id,
                "layer": layer,
                "output_element_id": matched_cells[(source_element_id, layer)],
            }
            for source_element_id, layer in sorted(matched_cells)
        ],
        "total_volume": total_volume,
        "min_sampled_jacobian": minimum_jacobian,
        "max_coordinate_error": max_coordinate_error,
        "jacobian_scope": _JACOBIAN_SCOPE,
    }
