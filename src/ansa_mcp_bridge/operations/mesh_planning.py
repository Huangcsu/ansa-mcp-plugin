"""Offline, bounded planning and source auditing for structured shell sweeps.

This module deliberately has no ANSA imports. ``plan_blocks`` accepts only an
explicit ``straight_sweep`` plan; it does not partition geometry or choose a
block layout. ``audit_source`` consumes a normalized snapshot adapter and fails
closed when the snapshot does not expose the documented node/element data.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import math


_MAX_BLOCKS = 128
_MAX_INTERFACES = 512
_MAX_CONSTRAINTS = 512
_MAX_CHAIN_EDGES = 128
_SUPPORTED_ELEMENT_TYPES = frozenset({"CQUAD4", "CTRIA3"})


def _object(value, required, optional=(), label="object"):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        raise ValueError(f"{label} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{label} has unsupported field(s): {', '.join(sorted(extra))}")
    return value


def _identifier(value, label):
    if type(value) is int:
        if value <= 0:
            raise ValueError(f"{label} must be a positive integer or non-empty string")
        return value
    if type(value) is str and value and value == value.strip() and len(value) <= 128:
        return value
    raise ValueError(f"{label} must be a positive integer or non-empty string")


def _id_key(value):
    return type(value).__name__, value


def _sort_id(value):
    key = _id_key(value)
    return key[0], key[1]


def _reference(value, label):
    if type(value) is dict:
        _object(value, ("type", "id"), label=label)
        entity_type = value["type"]
        if type(entity_type) is not str or not entity_type or entity_type != entity_type.strip():
            raise ValueError(f"{label}.type must be a non-empty exact string")
        return {"type": entity_type, "id": _identifier(value["id"], f"{label}.id")}
    return _identifier(value, label)


def _finite_number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_int(value, label, maximum=10000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _direction(value, label):
    if type(value) not in (list, tuple) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three finite numbers")
    vector = tuple(_finite_number(item, f"{label}[{index}]") for index, item in enumerate(value))
    length = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(length) or length <= 0:
        raise ValueError(f"{label} must have non-zero length")
    return [item / length for item in vector]


def _normalize_seed_constraints(value):
    """Validate fixed seeds and solve only uniquely determined integer sums."""
    if type(value) is not list or len(value) > _MAX_CONSTRAINTS:
        raise ValueError(f"seed_constraints must be a list of at most {_MAX_CONSTRAINTS} items")
    normalized = []
    equations = []
    seen_constraint_ids = set()
    assigned = {}
    assigned_by = {}

    def assign(edge_id, segments, constraint_id):
        edge_key = _id_key(edge_id)
        previous = assigned.get(edge_key)
        if previous is not None and previous != segments:
            prior_constraint = assigned_by[edge_key]
            raise ValueError(
                f"integer segment conflict for edge {edge_id!r}: "
                f"{previous} (constraint {prior_constraint!r}) versus "
                f"{segments} (constraint {constraint_id!r})"
            )
        assigned[edge_key] = segments
        assigned_by[edge_key] = constraint_id

    def parse_chain(raw_edges, label, constraint_id):
        if type(raw_edges) is not list or not 1 <= len(raw_edges) <= _MAX_CHAIN_EDGES:
            raise ValueError(f"{label} must contain 1 to {_MAX_CHAIN_EDGES} edges")
        chain = []
        seen_edges = set()
        for edge_index, raw_edge in enumerate(raw_edges):
            edge_label = f"{label}[{edge_index}]"
            if type(raw_edge) is dict:
                _object(raw_edge, ("edge_id",), ("segments",), edge_label)
                edge_id = _identifier(raw_edge["edge_id"], f"{edge_label}.edge_id")
                segments = None
                if "segments" in raw_edge:
                    segments = _positive_int(raw_edge["segments"], f"{edge_label}.segments")
                    assign(edge_id, segments, constraint_id)
            else:
                edge_id = _identifier(raw_edge, edge_label)
                segments = None
            edge_key = _id_key(edge_id)
            if edge_key in seen_edges:
                raise ValueError(f"{label} repeats edge id {edge_id!r}")
            seen_edges.add(edge_key)
            chain.append({"edge_id": edge_id, "segments": segments})
        return chain

    for index, raw in enumerate(value):
        if type(raw) is not dict:
            raise TypeError(f"seed_constraints[{index}] must be an object")
        if "left_edges" in raw or "right_edges" in raw:
            _object(raw, ("id", "left_edges", "right_edges"), label=f"seed_constraints[{index}]")
            constraint_id = _identifier(raw["id"], f"seed_constraints[{index}].id")
            key = _id_key(constraint_id)
            if key in seen_constraint_ids:
                raise ValueError(f"duplicate seed constraint id {constraint_id!r}")
            seen_constraint_ids.add(key)
            equations.append({
                "id": constraint_id,
                "left_edges": parse_chain(raw["left_edges"], f"seed_constraints[{index}].left_edges", constraint_id),
                "right_edges": parse_chain(raw["right_edges"], f"seed_constraints[{index}].right_edges", constraint_id),
            })
            continue
        if "edge_ids" in raw:
            _object(raw, ("id", "edge_ids", "segments"), label=f"seed_constraints[{index}]")
            raw_edges = raw["edge_ids"]
        else:
            _object(raw, ("id", "edge_id", "segments"), label=f"seed_constraints[{index}]")
            raw_edges = [raw["edge_id"]]
        constraint_id = _identifier(raw["id"], f"seed_constraints[{index}].id")
        key = _id_key(constraint_id)
        if key in seen_constraint_ids:
            raise ValueError(f"duplicate seed constraint id {constraint_id!r}")
        seen_constraint_ids.add(key)
        if type(raw_edges) is not list or not 1 <= len(raw_edges) <= _MAX_CHAIN_EDGES:
            raise ValueError(
                f"seed_constraints[{index}].edge_ids must contain 1 to {_MAX_CHAIN_EDGES} edges"
            )
        edge_ids = [_identifier(edge, f"seed_constraints[{index}].edge_ids[{i}]")
                    for i, edge in enumerate(raw_edges)]
        edge_keys = [_id_key(edge) for edge in edge_ids]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError(f"seed constraint {constraint_id!r} repeats an edge id")
        segments = _positive_int(raw["segments"], f"seed_constraints[{index}].segments")
        for edge_id in edge_ids:
            assign(edge_id, segments, constraint_id)
        normalized.append({"id": constraint_id, "edge_ids": edge_ids, "segments": segments})

    equations.sort(key=lambda item: _sort_id(item["id"]))

    def equation_state(equation):
        constant = 0
        coefficients = defaultdict(int)
        unknown_ids = {}
        for sign, side in ((1, equation["left_edges"]), (-1, equation["right_edges"])):
            for edge in side:
                key = _id_key(edge["edge_id"])
                segments = edge["segments"]
                if segments is None:
                    segments = assigned.get(key)
                if segments is None:
                    coefficients[key] += sign
                    unknown_ids[key] = edge["edge_id"]
                else:
                    constant += sign * segments
        coefficients = {key: coefficient for key, coefficient in coefficients.items() if coefficient}
        return constant, coefficients, unknown_ids

    changed = True
    while changed:
        changed = False
        for equation in equations:
            constant, coefficients, unknown_ids = equation_state(equation)
            if not coefficients:
                if constant:
                    raise ValueError(
                        f"integer segment sum conflict for constraint {equation['id']!r}: "
                        f"left total differs from right total by {constant}"
                    )
                continue
            if len(coefficients) == 1:
                edge_key, coefficient = next(iter(coefficients.items()))
                if abs(coefficient) == 1:
                    solved = -constant // coefficient
                    if solved <= 0 or solved > 10000 or constant + coefficient * solved != 0:
                        raise ValueError(
                            f"integer segment sum conflict for constraint {equation['id']!r}: "
                            f"edge {unknown_ids[edge_key]!r} would require count {solved} outside 1..10000"
                        )
                    assign(unknown_ids[edge_key], solved, equation["id"])
                    changed = True
                    break

    sum_results = []
    incomplete = []
    for equation in equations:
        constant, coefficients, unknown_ids = equation_state(equation)
        left_known = sum((edge["segments"] if edge["segments"] is not None
                          else assigned.get(_id_key(edge["edge_id"]), 0))
                         for edge in equation["left_edges"])
        right_known = sum((edge["segments"] if edge["segments"] is not None
                           else assigned.get(_id_key(edge["edge_id"]), 0))
                          for edge in equation["right_edges"])
        unresolved = sorted(unknown_ids.values(), key=_sort_id)
        solved = not coefficients and constant == 0 and not unresolved
        result = {
            "id": equation["id"],
            "status": "solved" if solved else "incomplete",
            "left_total": left_known if solved or not unresolved else None,
            "right_total": right_known if solved or not unresolved else None,
            "unresolved_edge_ids": unresolved,
        }
        if not solved:
            incomplete.append(result)
        normalized.append({
            "id": equation["id"],
            "left_edges": [
                {"edge_id": edge["edge_id"],
                 "segments": edge["segments"] if edge["segments"] is not None
                 else assigned.get(_id_key(edge["edge_id"]))}
                for edge in equation["left_edges"]
            ],
            "right_edges": [
                {"edge_id": edge["edge_id"],
                 "segments": edge["segments"] if edge["segments"] is not None
                 else assigned.get(_id_key(edge["edge_id"]))}
                for edge in equation["right_edges"]
            ],
            "status": result["status"],
        })
        sum_results.append(result)

    normalized.sort(key=lambda item: _sort_id(item["id"]))
    edge_segments = [
        {"edge_id": edge_id, "segments": segments}
        for edge_id, segments in sorted(
            ((key[1], segments) for key, segments in assigned.items()),
            key=lambda item: _sort_id(item[0]),
        )
    ]
    return normalized, edge_segments, sum_results, incomplete


def plan_blocks(plan):
    """Return a deterministic execution plan for explicit straight-sweep blocks.

    Sum constraints may specify left_edges and right_edges with edge IDs and
    optional known segment counts. A single unknown positive integer can be
    solved; underdetermined sums are returned with status incomplete.

    Contract for MCP adapters:

    * The plan may carry additional top-level context keys, which are ignored.
      It must include ``template``, ``blocks``, and ``interfaces``; the optional
      ``seed_constraints`` list is also strictly validated.
    * Every block has exactly ``id``, ``source``, ``target``, ``depends_on``,
      ``sweep_direction`` and ``layers``. References are scalar IDs or
      ``{"type": str, "id": ID}`` objects.
    * Every interface has ``id``, two distinct block IDs, and either a
      ``source``/``target`` reference pair or two ``facets`` references.
    * A seed constraint is ``{id, edge_ids, segments}`` (or the singular
      ``edge_id`` form). All counts are positive integers; overlapping edge
      constraints must agree.

    The return value contains ``execution_order``, normalized block and
    interface records, and resolved integer edge counts. No geometry is
    partitioned and no ANSA API is called.
    """
    if type(plan) is not dict:
        raise TypeError("plan must be an object")
    missing = {"template", "blocks", "interfaces"} - plan.keys()
    if missing:
        raise ValueError(f"plan is missing required field(s): {', '.join(sorted(missing))}")
    if plan["template"] != "straight_sweep":
        raise ValueError("template must explicitly be 'straight_sweep'; automatic partitioning is unsupported")

    raw_blocks = plan["blocks"]
    if type(raw_blocks) is not list or not 1 <= len(raw_blocks) <= _MAX_BLOCKS:
        raise ValueError(f"blocks must contain 1 to {_MAX_BLOCKS} explicit blocks")
    blocks = []
    block_by_key = {}
    for index, raw in enumerate(raw_blocks):
        label = f"blocks[{index}]"
        _object(raw, ("id", "source", "target", "depends_on", "sweep_direction", "layers"), label=label)
        block_id = _identifier(raw["id"], f"{label}.id")
        key = _id_key(block_id)
        if key in block_by_key:
            raise ValueError(f"duplicate block id {block_id!r}")
        source = _reference(raw["source"], f"{label}.source")
        target = _reference(raw["target"], f"{label}.target")
        if source == target:
            raise ValueError(f"{label}.source and target must identify different references")
        if type(raw["depends_on"]) is not list or len(raw["depends_on"]) > _MAX_BLOCKS:
            raise ValueError(f"{label}.depends_on must be a list of block IDs")
        dependencies = [_identifier(dep, f"{label}.depends_on[{i}]")
                        for i, dep in enumerate(raw["depends_on"])]
        dep_keys = [_id_key(dep) for dep in dependencies]
        if len(dep_keys) != len(set(dep_keys)):
            raise ValueError(f"{label}.depends_on contains a duplicate block ID")
        direction = _direction(raw["sweep_direction"], f"{label}.sweep_direction")
        layers = _positive_int(raw["layers"], f"{label}.layers")
        block = {
            "id": block_id,
            "source": source,
            "target": target,
            "depends_on": sorted(dependencies, key=_sort_id),
            "sweep_direction": direction,
            "layers": layers,
        }
        blocks.append(block)
        block_by_key[key] = block

    for block in blocks:
        for dependency in block["depends_on"]:
            if _id_key(dependency) not in block_by_key:
                raise ValueError(f"block {block['id']!r} depends on missing block {dependency!r}")
            if dependency == block["id"]:
                raise ValueError(f"block {block['id']!r} cannot depend on itself")

    indegree = {key: 0 for key in block_by_key}
    successors = defaultdict(list)
    for block in blocks:
        block_key = _id_key(block["id"])
        for dependency in block["depends_on"]:
            dependency_key = _id_key(dependency)
            indegree[block_key] += 1
            successors[dependency_key].append(block_key)
    id_by_key = {key: block["id"] for key, block in block_by_key.items()}
    ready = sorted((key for key, count in indegree.items() if count == 0),
                   key=lambda key: _sort_id(id_by_key[key]))
    execution_order = []
    while ready:
        current = ready.pop(0)
        execution_order.append(id_by_key[current])
        for successor in sorted(successors[current], key=lambda key: _sort_id(id_by_key[key])):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort(key=lambda key: _sort_id(id_by_key[key]))
    if len(execution_order) != len(blocks):
        cyclic = sorted((id_by_key[key] for key, count in indegree.items() if count), key=_sort_id)
        raise ValueError(f"block dependency cycle detected among {cyclic!r}")

    raw_interfaces = plan["interfaces"]
    if type(raw_interfaces) is not list or len(raw_interfaces) > _MAX_INTERFACES:
        raise ValueError(f"interfaces must be a list of at most {_MAX_INTERFACES} items")
    interfaces = []
    interface_keys = set()
    for index, raw in enumerate(raw_interfaces):
        label = f"interfaces[{index}]"
        if type(raw) is not dict:
            raise TypeError(f"{label} must be an object")
        if "facets" in raw:
            _object(raw, ("id", "blocks", "facets"), label=label)
            refs = raw["facets"]
            if type(refs) is not list or len(refs) != 2:
                raise ValueError(f"{label}.facets must contain exactly two facet references")
            interface = {"id": _identifier(raw["id"], f"{label}.id"),
                         "blocks": None,
                         "facets": [_reference(refs[i], f"{label}.facets[{i}]") for i in range(2)]}
        else:
            _object(raw, ("id", "blocks", "source", "target"), label=label)
            interface = {"id": _identifier(raw["id"], f"{label}.id"),
                         "blocks": None,
                         "source": _reference(raw["source"], f"{label}.source"),
                         "target": _reference(raw["target"], f"{label}.target")}
        interface_key = _id_key(interface["id"])
        if interface_key in interface_keys:
            raise ValueError(f"duplicate interface id {interface['id']!r}")
        interface_keys.add(interface_key)
        raw_block_ids = raw["blocks"]
        if type(raw_block_ids) is not list or len(raw_block_ids) != 2:
            raise ValueError(f"{label}.blocks must contain exactly two block IDs")
        pair = [_identifier(item, f"{label}.blocks[{i}]") for i, item in enumerate(raw_block_ids)]
        pair_keys = [_id_key(item) for item in pair]
        if pair_keys[0] == pair_keys[1]:
            raise ValueError(f"{label}.blocks must identify two different blocks")
        for block_id in pair:
            if _id_key(block_id) not in block_by_key:
                raise ValueError(f"{label} refers to missing block {block_id!r}")
        interface["blocks"] = sorted(pair, key=_sort_id)
        interfaces.append(interface)

    constraints, edge_segments, sum_results, incomplete_sums = _normalize_seed_constraints(
        plan.get("seed_constraints", [])
    )
    blocks.sort(key=lambda item: _sort_id(item["id"]))
    interfaces.sort(key=lambda item: _sort_id(item["id"]))
    return {
        "schema_version": 1,
        "status": "incomplete" if incomplete_sums else "complete",
        "template": "straight_sweep",
        "execution_order": execution_order,
        "blocks": blocks,
        "interfaces": interfaces,
        "seed_constraints": constraints,
        "sum_constraints": sum_results,
        "edge_segments": edge_segments,
        "scope": {"automatic_partitioning": False, "supported_templates": ["straight_sweep"]},
    }


def _snapshot_node_id(value, label):
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isdecimal() and int(value) > 0:
        return int(value)
    raise ValueError(f"{label} must be a positive integer node ID")


def _point(value, label):
    if type(value) not in (list, tuple) or len(value) != 3:
        raise NotImplementedError(f"{label} must expose exactly three coordinates")
    point = tuple(_finite_number(value[i], f"{label}[{i}]") for i in range(3))
    return point


def _element_connectivity(row, label):
    raw = row.get("node_ids")
    fields = row.get("fields", row.get("card_fields", {}))
    ordered = None
    if type(fields) is dict:
        for names in (("G1", "G2", "G3", "G4"), ("GA", "GB", "GC", "GD")):
            present = [name in fields for name in names]
            if present[0] and present[1] and present[2]:
                count = 4 if present[3] else 3
                ordered = [fields[name] for name in names[:count]]
                break
    values = ordered if ordered is not None else raw
    if type(values) not in (list, tuple) or len(values) not in (3, 4):
        raise NotImplementedError(f"{label} lacks ordered triangular or quadrilateral connectivity")
    node_ids = [_snapshot_node_id(value, f"{label}.node_ids[{i}]")
                for i, value in enumerate(values)]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError(f"{label} repeats a node ID")
    if raw is not None:
        raw_ids = [_snapshot_node_id(value, f"{label}.node_ids[{i}]")
                   for i, value in enumerate(raw)]
        if len(raw_ids) != len(node_ids) or set(raw_ids) != set(node_ids):
            raise ValueError(f"{label} card connectivity does not match node_ids")
    return node_ids


def _snapshot_element(row, index):
    label = f"snapshot.elements[{index}]"
    if type(row) is not dict:
        raise NotImplementedError(f"{label} must be a normalized element object")
    entity = row.get("entity")
    if type(entity) is dict:
        entity_id = entity.get("id", row.get("id"))
        entity_type = entity.get("type", row.get("type"))
    else:
        entity_id = row.get("id")
        entity_type = row.get("type")
    element_id = _snapshot_node_id(entity_id, f"{label}.id")
    node_ids = _element_connectivity(row, label)
    explicit_type = row.get("element_type")
    if explicit_type is None:
        explicit_type = row.get("type")
    if explicit_type is None and type(entity) is dict:
        explicit_type = entity.get("element_type")
    if explicit_type is None and type(entity) is dict and entity_type in _SUPPORTED_ELEMENT_TYPES:
        explicit_type = entity_type
    if explicit_type is None and entity_type == "SHELL":
        # The normalized SHELL category has no card subtype; its explicit
        # three/four-node connectivity safely bounds this adapter to CTRIA3/CQUAD4.
        explicit_type = "CTRIA3" if len(node_ids) == 3 else "CQUAD4"
    if explicit_type not in _SUPPORTED_ELEMENT_TYPES:
        raise NotImplementedError(
            f"{label} must identify CQUAD4 or CTRIA3; got {explicit_type!r}"
        )
    expected_nodes = 4 if explicit_type == "CQUAD4" else 3
    if len(node_ids) != expected_nodes:
        raise ValueError(f"{label} {explicit_type} must reference exactly {expected_nodes} nodes")
    edge_ids = row.get("edge_ids")
    if edge_ids is not None:
        if type(edge_ids) not in (list, tuple) or len(edge_ids) != expected_nodes:
            raise ValueError(f"{label}.edge_ids must contain one ID for each ordered element edge")
        edge_ids = [_identifier(edge, f"{label}.edge_ids[{i}]") for i, edge in enumerate(edge_ids)]
        if len({_id_key(edge) for edge in edge_ids}) != len(edge_ids):
            raise ValueError(f"{label}.edge_ids contains duplicates")
    return {"id": element_id, "type": explicit_type, "node_ids": node_ids, "edge_ids": edge_ids}


def _vector_delta(left, right):
    return tuple(right[i] - left[i] for i in range(3))


def _dot(left, right):
    return sum(left[i] * right[i] for i in range(3))


def _norm(value):
    return math.sqrt(_dot(value, value))


def _edge_key(left, right):
    return tuple(sorted((left, right)))


def audit_source(snapshot, *, source_ids, target_ids=None, direction, layers, target_length,
                 seed_constraints=None, minimum_edge_length=None, max_angle_degrees=5.0,
                 minimum_face_angle_degrees=5.0, maximum_face_angle_degrees=175.0):
    """Audit an explicit shell source/target pair from a normalized snapshot.

    MCP adapter contract: ``snapshot`` is an object containing ``nodes`` (a
    mapping from node ID to ``{"position": [x, y, z]}``, as used by the bridge
    FE snapshot) and ``elements`` (a sequence of objects with ``entity``
    ``{type, id}``, ordered ``node_ids`` or ordered card fields ``G1..G4`` /
    ``GA..GD``, and optionally ``element_type`` and per-edge ``edge_ids``).
    ``SHELL`` elements with three/four connected nodes are boundedly classified
    as CTRIA3/CQUAD4. Source and target arguments are shell element IDs.

    The audit checks one-to-one transverse node correspondence along a straight
    direction, shell connectivity correspondence, orientation angle, edge
    lengths, layer spacing, and any equal-length integer seed constraints. A
    short edge below 10% of ``target_length`` is reported as a warning; a
    caller-supplied ``minimum_edge_length`` is a hard limit. Unknown snapshot
    adapters or element types raise ``NotImplementedError`` and can never be
    reported as passing. This is an offline preflight, not mesh-generation or
    engineering acceptance evidence. Source interior angles are checked against
    configured limits, defaulting to five through 175 degrees. If target_ids is
    omitted or empty, source face angles, short edges and seed constraints are checked; target
    correspondence remains unknown and the report status is incomplete.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be an object with nodes and elements")
    if snapshot.get("complete", True) is not True:
        raise ValueError("source audit requires a complete snapshot")
    nodes_raw = snapshot.get("nodes")
    elements_raw = snapshot.get("elements")
    if not isinstance(nodes_raw, Mapping) or isinstance(elements_raw, (str, bytes)) \
            or not isinstance(elements_raw, Sequence):
        raise NotImplementedError("snapshot adapter must expose mapping nodes and sequence elements")

    nodes = {}
    for raw_id, row in nodes_raw.items():
        node_id = _snapshot_node_id(raw_id, "snapshot.nodes key")
        if node_id in nodes:
            raise ValueError(f"snapshot.nodes contains duplicate ID {node_id}")
        if isinstance(row, Mapping):
            position = row.get("position", row.get("xyz", row.get("coordinates")))
        else:
            position = row
        if position is None:
            raise NotImplementedError(f"snapshot.nodes[{node_id}] lacks a position")
        nodes[node_id] = _point(position, f"snapshot.nodes[{node_id}].position")

    elements = {}
    for index, row in enumerate(elements_raw):
        element = _snapshot_element(row, index)
        if element["id"] in elements:
            raise ValueError(f"snapshot.elements contains duplicate ID {element['id']}")
        missing_nodes = sorted(set(element["node_ids"]) - nodes.keys())
        if missing_nodes:
            raise ValueError(f"element {element['id']} references missing node IDs {missing_nodes!r}")
        elements[element["id"]] = element

    def normalize_selection(raw_ids, label):
        if type(raw_ids) not in (list, tuple) or not raw_ids:
            raise ValueError(f"{label} must contain at least one shell element ID")
        ids = [_snapshot_node_id(item, f"{label}[{i}]") for i, item in enumerate(raw_ids)]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label} contains duplicate element IDs")
        missing = [item for item in ids if item not in elements]
        if missing:
            raise ValueError(f"{label} refers to missing element IDs {missing!r}")
        return ids

    source_element_ids = normalize_selection(source_ids, "source_ids")
    has_target = target_ids is not None and target_ids != []
    target_element_ids = normalize_selection(target_ids, "target_ids") if has_target else []
    overlap = sorted(set(source_element_ids) & set(target_element_ids))
    if overlap:
        raise ValueError(f"source_ids and target_ids overlap at element IDs {overlap!r}")
    source_elements = [elements[item] for item in source_element_ids]
    target_elements = [elements[item] for item in target_element_ids]
    direction_vector = _direction(direction, "direction")
    layer_count = _positive_int(layers, "layers")
    target_length = _finite_number(target_length, "target_length")
    if target_length <= 0:
        raise ValueError("target_length must be positive")
    minimum_edge = None if minimum_edge_length is None else _finite_number(
        minimum_edge_length, "minimum_edge_length"
    )
    if minimum_edge is not None and minimum_edge <= 0:
        raise ValueError("minimum_edge_length must be positive when provided")
    max_angle = _finite_number(max_angle_degrees, "max_angle_degrees")
    if not 0 < max_angle < 90:
        raise ValueError("max_angle_degrees must be greater than 0 and less than 90")
    minimum_face_angle = _finite_number(minimum_face_angle_degrees, "minimum_face_angle_degrees")
    maximum_face_angle = _finite_number(maximum_face_angle_degrees, "maximum_face_angle_degrees")
    if not 0 < minimum_face_angle < maximum_face_angle < 180:
        raise ValueError("face angle limits must satisfy 0 < minimum < maximum < 180 degrees")
    constraints = [] if seed_constraints is None else seed_constraints

    issues = []
    warnings = []
    checks = {
        "topology_correspondence": False if has_target else None,
        "sweep_direction": False if has_target else None,
        "source_face_angles": False,
        "short_edges": False,
        "seed_constraints": False,
    }
    source_node_ids = sorted({node for element in source_elements for node in element["node_ids"]})
    target_node_ids = sorted({node for element in target_elements for node in element["node_ids"]})
    source_type_counts = Counter(element["type"] for element in source_elements)
    target_type_counts = Counter(element["type"] for element in target_elements)
    if has_target and source_type_counts != target_type_counts:
        issues.append({"code": "element_topology_mismatch",
                       "message": "source and target shell counts/types do not correspond"})
    if has_target and len(source_node_ids) != len(target_node_ids):
        issues.append({"code": "node_topology_mismatch",
                       "message": "source and target contain different numbers of unique nodes"})

    all_points = [nodes[node_id] for node_id in source_node_ids + target_node_ids]
    coordinate_span = max(
        (max(point[axis] for point in all_points) - min(point[axis] for point in all_points)
         for axis in range(3)), default=0.0
    )
    scale = max(coordinate_span, target_length, 1e-12)
    tolerance = max(target_length * 1e-6, scale * 1e-9)

    face_angles = []
    angle_issue_count = 0
    for element in source_elements:
        node_ids = element["node_ids"]
        for index, node_id in enumerate(node_ids):
            center = nodes[node_id]
            previous = nodes[node_ids[index - 1]]
            following = nodes[node_ids[(index + 1) % len(node_ids)]]
            left = _vector_delta(center, previous)
            right = _vector_delta(center, following)
            left_length, right_length = _norm(left), _norm(right)
            if left_length <= tolerance or right_length <= tolerance:
                angle_issue_count += 1
                issues.append({"code": "degenerate_source_face_angle",
                               "message": f"source element {element['id']} has a degenerate corner at node {node_id}"})
                continue
            cosine = max(-1.0, min(1.0, _dot(left, right) / (left_length * right_length)))
            angle = math.degrees(math.acos(cosine))
            face_angles.append(angle)
            if angle < minimum_face_angle or angle > maximum_face_angle:
                angle_issue_count += 1
                issues.append({"code": "source_face_angle_out_of_range",
                               "message": f"source element {element['id']} corner angle {angle:.6g} is outside the configured range"})
    checks["source_face_angles"] = angle_issue_count == 0

    mapping = {}
    pair_distances = []
    pair_angles = []
    if has_target and len(source_node_ids) == len(target_node_ids):
        used_targets = set()
        for source_node in source_node_ids:
            source_point = nodes[source_node]
            candidates = []
            for target_node in target_node_ids:
                target_point = nodes[target_node]
                delta = _vector_delta(source_point, target_point)
                axial = _dot(delta, direction_vector)
                residual = tuple(delta[i] - axial * direction_vector[i] for i in range(3))
                if _norm(residual) <= tolerance:
                    candidates.append((target_node, axial, _norm(residual), _norm(delta)))
            if len(candidates) != 1 or candidates[0][0] in used_targets:
                issues.append({"code": "node_correspondence_ambiguous",
                               "message": f"source node {source_node} has no unique target along direction"})
                continue
            target_node, axial, residual_length, distance = candidates[0]
            mapping[source_node] = target_node
            used_targets.add(target_node)
            if axial <= 0:
                issues.append({"code": "sweep_direction_reversed",
                               "message": f"source node {source_node} does not advance along direction"})
                continue
            angle = math.degrees(math.atan2(residual_length, axial))
            pair_angles.append(angle)
            pair_distances.append(distance)
            if angle > max_angle:
                issues.append({"code": "sweep_angle_exceeded",
                               "message": f"source node {source_node} sweep angle {angle:.6g} exceeds {max_angle:.6g} degrees"})
    elif has_target:
        issues.append({"code": "node_correspondence_unavailable",
                       "message": "different node counts prevent a one-to-one sweep correspondence"})

    topology_matches = False
    if (has_target and len(mapping) == len(source_node_ids) == len(target_node_ids)
            and source_type_counts == target_type_counts):
        source_cells = Counter(
            (element["type"], tuple(sorted(mapping[node] for node in element["node_ids"])))
            for element in source_elements
        )
        target_cells = Counter(
            (element["type"], tuple(sorted(element["node_ids"]))) for element in target_elements
        )
        source_edges_mapped = Counter(
            _edge_key(mapping[left], mapping[element["node_ids"][(index + 1) % len(element["node_ids"])]])
            for element in source_elements
            for index, left in enumerate(element["node_ids"])
        )
        target_edges = Counter(
            _edge_key(left, element["node_ids"][(index + 1) % len(element["node_ids"])])
            for element in target_elements
            for index, left in enumerate(element["node_ids"])
        )
        topology_matches = source_cells == target_cells and source_edges_mapped == target_edges
        if topology_matches:
            checks["topology_correspondence"] = True
        else:
            issues.append({"code": "element_connectivity_mismatch",
                           "message": "mapped source shell connectivity differs from target connectivity"})

    mean_sweep_distance = sum(pair_distances) / len(pair_distances) if pair_distances else None
    if not has_target:
        mean_sweep_distance = None
    elif pair_distances and min(pair_distances) > 0:
        spread = max(pair_distances) - min(pair_distances)
        if spread > max(tolerance, mean_sweep_distance * 0.01):
            issues.append({"code": "nonuniform_sweep_distance",
                           "message": "corresponding source/target nodes have non-uniform sweep distances"})
        if len(pair_angles) == len(source_node_ids) and max(pair_angles, default=0.0) <= max_angle \
                and spread <= max(tolerance, mean_sweep_distance * 0.01):
            checks["sweep_direction"] = True
    elif has_target and not any(issue["code"] == "sweep_direction_reversed" for issue in issues):
        issues.append({"code": "sweep_distance_unavailable", "message": "no positive source-to-target sweep distance was established"})

    source_edge_lengths = {}
    source_edge_ids = {}
    source_edge_counts = Counter()
    for element in source_elements:
        node_ids = element["node_ids"]
        explicit_edge_ids = element["edge_ids"]
        for index, left in enumerate(node_ids):
            right = node_ids[(index + 1) % len(node_ids)]
            key = _edge_key(left, right)
            length = _norm(_vector_delta(nodes[left], nodes[right]))
            source_edge_lengths[key] = length
            source_edge_counts[key] += 1
            if explicit_edge_ids is not None:
                edge_id = explicit_edge_ids[index]
                edge_key = _id_key(edge_id)
                prior = source_edge_ids.get(edge_key)
                if prior is not None and prior != key:
                    raise ValueError(f"snapshot reuses edge ID {edge_id!r} for different node pairs")
                source_edge_ids[edge_key] = key

    short_edge_threshold = target_length * 0.1
    short_edges = [
        {"nodes": list(key), "length": length}
        for key, length in sorted(source_edge_lengths.items()) if length < short_edge_threshold
    ]
    degenerate_threshold = max(scale, target_length) * 1e-12
    degenerate = [edge for edge in short_edges if edge["length"] <= degenerate_threshold]
    if degenerate:
        issues.append({"code": "degenerate_source_edge",
                       "message": f"{len(degenerate)} source edge(s) are geometrically degenerate"})
    below_minimum = []
    if minimum_edge is not None:
        below_minimum = [edge for edge in source_edge_lengths.values() if edge < minimum_edge]
        if below_minimum:
            issues.append({"code": "minimum_edge_length_violation",
                           "message": f"{len(below_minimum)} source edge(s) are shorter than minimum_edge_length"})
    if short_edges:
        warnings.append({"code": "short_source_edges",
                         "message": f"{len(short_edges)} source edge(s) are shorter than 10% of target_length"})
    checks["short_edges"] = not degenerate and (minimum_edge is None or not below_minimum)

    incomplete_reasons = []
    if not has_target:
        incomplete_reasons.append({
            "code": "target_correspondence_unknown",
            "message": "target_ids were omitted; source checks ran without target topology verification",
        })
    try:
        normalized_constraints, resolved_segments, sum_results, incomplete_sums = \
            _normalize_seed_constraints(constraints)
    except (TypeError, ValueError) as exc:
        normalized_constraints = []
        resolved_segments = []
        sum_results = []
        incomplete_sums = []
        issues.append({"code": "seed_constraint_invalid", "message": str(exc)})
    constraint_pitches = []
    constraints_ok = not incomplete_sums
    for result in incomplete_sums:
        incomplete_reasons.append({
            "code": "seed_sum_underdetermined",
            "message": f"seed sum {result['id']!r} has unresolved edge IDs {result['unresolved_edge_ids']!r}",
        })
    for constraint in normalized_constraints:
        if "left_edges" in constraint:
            missing_edges = []
            left_counts = []
            right_counts = []
            for side, counts in ((constraint["left_edges"], left_counts),
                                 (constraint["right_edges"], right_counts)):
                for edge in side:
                    edge_id = edge["edge_id"]
                    if _id_key(edge_id) not in source_edge_ids:
                        missing_edges.append(edge_id)
                    if edge["segments"] is not None:
                        counts.append(edge["segments"])
            if missing_edges:
                constraints_ok = False
                issues.append({"code": "seed_edge_missing",
                               "message": f"seed sum {constraint['id']!r} references unavailable source edge IDs {missing_edges!r}"})
                continue
            if any(edge["segments"] is None
                   for edge in constraint["left_edges"] + constraint["right_edges"]):
                continue
            left_total = sum(left_counts)
            right_total = sum(right_counts)
            if left_total != right_total:
                constraints_ok = False
                issues.append({"code": "seed_sum_mismatch",
                               "message": f"seed sum {constraint['id']!r} totals {left_total} and {right_total}"})
            continue
        chain_lengths = []
        missing_edges = []
        for edge_id in constraint["edge_ids"]:
            edge_key = source_edge_ids.get(_id_key(edge_id))
            if edge_key is None:
                missing_edges.append(edge_id)
            else:
                chain_lengths.append(source_edge_lengths[edge_key])
        if missing_edges:
            constraints_ok = False
            issues.append({"code": "seed_edge_missing",
                           "message": f"seed constraint {constraint['id']!r} references unavailable source edge IDs {missing_edges!r}"})
            continue
        chain_tolerance = max(max(chain_lengths) * 1e-6, tolerance)
        if max(chain_lengths) - min(chain_lengths) > chain_tolerance:
            constraints_ok = False
            issues.append({"code": "seed_chain_not_equal_length",
                           "message": f"seed constraint {constraint['id']!r} edges are not equal length"})
        pitch = sum(chain_lengths) / len(chain_lengths) / constraint["segments"]
        constraint_pitches.append({"constraint_id": constraint["id"],
                                  "segments": constraint["segments"],
                                  "edge_length": sum(chain_lengths) / len(chain_lengths),
                                  "segment_length": pitch})
        if abs(pitch - target_length) > target_length * 0.25:
            warnings.append({"code": "seed_pitch_differs_from_target_length",
                             "message": f"seed constraint {constraint['id']!r} segment length differs from target_length by more than 25%"})
    if any(result["status"] == "incomplete" for result in sum_results):
        constraints_ok = False
    checks["seed_constraints"] = constraints_ok

    if mean_sweep_distance is not None:
        layer_spacing = mean_sweep_distance / layer_count
        layer_ratio = layer_spacing / target_length
        if layer_ratio < 0.25 or layer_ratio > 4.0:
            warnings.append({"code": "layer_spacing_differs_from_target_length",
                             "message": "estimated sweep layer spacing is outside one quarter to four times target_length"})
    else:
        layer_spacing = None
        layer_ratio = None

    status = "failed" if issues else "incomplete" if incomplete_reasons else "complete"
    return {
        "schema_version": 1,
        "supported": True,
        "status": status,
        "passed": status == "complete",
        "issues": issues,
        "warnings": warnings,
        "incomplete_reasons": incomplete_reasons,
        "checks": checks,
        "scope": {"source_ids": source_element_ids, "target_ids": target_element_ids,
                  "element_types": sorted(source_type_counts.keys() | target_type_counts.keys())},
        "metrics": {
            "source_node_count": len(source_node_ids),
            "target_node_count": len(target_node_ids),
            "source_element_count": len(source_elements),
            "target_element_count": len(target_elements),
            "matched_node_count": len(mapping),
            "mean_sweep_distance": mean_sweep_distance,
            "max_sweep_angle_degrees": max(pair_angles, default=None),
            "minimum_source_face_angle_degrees": min(face_angles, default=None),
            "maximum_source_face_angle_degrees": max(face_angles, default=None),
            "layers": layer_count,
            "estimated_layer_spacing": layer_spacing,
            "layer_spacing_to_target_length": layer_ratio,
            "target_length": target_length,
            "source_edge_count": len(source_edge_lengths),
            "short_edge_count": len(short_edges),
            "short_edge_threshold": short_edge_threshold,
            "seed_constraint_pitches": constraint_pitches,
            "seed_sum_constraints": sum_results,
        },
    }
