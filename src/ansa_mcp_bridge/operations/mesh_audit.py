"""Pure-data topology and protected-cylinder audit for S25 CHEXA8/CPENTA6 meshes.

No ANSA modules, live model state, native quality checks, or general collision
checks are used here. The public result is JSON-serializable and keeps the full
per-section evidence so callers can persist/page it independently.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict


MAX_NODES = 300_000
MAX_ELEMENTS = 50_000
MAX_DECLARED_FACETS = 300_000
MAX_FACET_NODE_REFERENCES = 1_000_000
MAX_INTERFACES = 5_000
MAX_PROTECTED_CYLINDERS = 500

_HEX8_FACES = (
    (0, 3, 2, 1),  # z-
    (4, 5, 6, 7),  # z+
    (0, 1, 5, 4),  # y-
    (1, 2, 6, 5),  # x+
    (2, 3, 7, 6),  # y+
    (3, 0, 4, 7),  # x-
)
_PENTA6_FACES = (
    (0, 2, 1),      # triangular end-
    (3, 4, 5),      # triangular end+
    (0, 1, 4, 3),
    (1, 2, 5, 4),
    (2, 0, 3, 5),
)
_FACE_TABLE = {"CHEXA": _HEX8_FACES, "CPENTA": _PENTA6_FACES}


class _AuditContext:
    def __init__(self):
        self.states = {
            "topology": "pass",
            "interfaces": "pass",
            "expected_boundary": "pass",
            "geometry": "pass",
            "quality": "pass",
            "intersections": "pass",
        }
        self.diagnostics = []

    def issue(self, section, severity, code, message, **details):
        current = self.states[section]
        if severity == "fail" or (severity == "incomplete" and current == "pass"):
            self.states[section] = severity
        entry = {
            "section": section,
            "severity": severity,
            "code": code,
            "message": message,
        }
        if details:
            entry["details"] = details
        self.diagnostics.append(entry)


def _combine(*statuses):
    if "fail" in statuses:
        return "fail"
    if "incomplete" in statuses:
        return "incomplete"
    return "pass"


def _sequence(value, label):
    if type(value) not in (list, tuple):
        raise ValueError(f"{label} must be a list or tuple")
    return value


def _finite_number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite int or float")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be representable as a finite float") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer (bool is not accepted)")
    return value


def _optional_owner_id(value, label):
    if value is None:
        return None
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value and len(value) <= 256:
        return value
    raise ValueError(f"{label} must be a positive integer, a non-empty string, or null")


def _vector3(value, label):
    values = _sequence(value, label)
    if len(values) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return tuple(_finite_number(item, f"{label}[{index}]") for index, item in enumerate(values))


def _prepare_snapshot(snapshot):
    if type(snapshot) is not dict:
        raise ValueError("snapshot must be a dict")
    raw_nodes = _sequence(snapshot.get("nodes"), "snapshot.nodes")
    raw_elements = _sequence(snapshot.get("elements"), "snapshot.elements")
    if len(raw_nodes) > MAX_NODES:
        raise ValueError(f"snapshot exceeds MAX_NODES={MAX_NODES}")
    if len(raw_elements) > MAX_ELEMENTS:
        raise ValueError(f"snapshot exceeds MAX_ELEMENTS={MAX_ELEMENTS}")

    coords = {}
    for index, row in enumerate(raw_nodes):
        if type(row) is not dict:
            raise ValueError(f"snapshot.nodes[{index}] must be a dict")
        node_id = _positive_id(row.get("id"), f"snapshot.nodes[{index}].id")
        if node_id in coords:
            raise ValueError(f"duplicate node id {node_id}")
        coords[node_id] = _vector3(row.get("xyz"), f"node {node_id}.xyz")

    elements = {}
    connectivity_owner = {}
    supported = {}
    unsupported = []
    for index, row in enumerate(raw_elements):
        if type(row) is not dict:
            raise ValueError(f"snapshot.elements[{index}] must be a dict")
        element_id = _positive_id(row.get("id"), f"snapshot.elements[{index}].id")
        if element_id in elements:
            raise ValueError(f"duplicate element id {element_id}")
        element_type = row.get("type")
        if type(element_type) is not str or not element_type:
            raise ValueError(f"element {element_id}.type must be a non-empty string")
        element_type = element_type.upper()
        raw_connectivity = _sequence(row.get("nodes"), f"element {element_id}.nodes")
        if not raw_connectivity:
            raise ValueError(f"element {element_id}.nodes cannot be empty")
        if len(raw_connectivity) > MAX_NODES:
            raise ValueError(f"element {element_id}.nodes exceeds MAX_NODES={MAX_NODES}")
        node_ids = tuple(
            _positive_id(node_id, f"element {element_id}.nodes[{node_index}]")
            for node_index, node_id in enumerate(raw_connectivity)
        )
        if len(set(node_ids)) != len(node_ids):
            raise ValueError(f"element {element_id} contains duplicate node IDs")
        missing = sorted(set(node_ids) - coords.keys())
        if missing:
            raise ValueError(f"element {element_id} references unknown node IDs {missing}")

        block_id = _optional_owner_id(row.get("block_id"), f"element {element_id}.block_id")
        property_id = _optional_owner_id(row.get("property_id"), f"element {element_id}.property_id")
        volume = None
        if "volume" in row and row["volume"] is not None:
            volume = _finite_number(row["volume"], f"element {element_id}.volume")

        element = {
            "id": element_id,
            "type": element_type,
            "nodes": node_ids,
            "block_id": block_id,
            "property_id": property_id,
            "reported_volume": volume,
        }
        elements[element_id] = element
        canonical_connectivity = tuple(sorted(node_ids))
        if canonical_connectivity in connectivity_owner:
            other_id = connectivity_owner[canonical_connectivity]
            raise ValueError(
                f"elements {other_id} and {element_id} have duplicate node connectivity"
            )
        connectivity_owner[canonical_connectivity] = element_id

        expected_arity = 8 if element_type == "CHEXA" else 6 if element_type == "CPENTA" else None
        if expected_arity is None:
            unsupported.append({"element_id": element_id, "type": element_type, "node_count": len(node_ids)})
        elif len(node_ids) != expected_arity:
            unsupported.append({
                "element_id": element_id,
                "type": element_type,
                "node_count": len(node_ids),
                "reason": f"{element_type} topology requires exactly {expected_arity} nodes",
            })
        else:
            supported[element_id] = element

    return coords, elements, supported, unsupported


def _normalize_facets(raw_facets, label, coords, *, max_facets=MAX_DECLARED_FACETS):
    facets = _sequence(raw_facets, label)
    if len(facets) > max_facets:
        raise ValueError(f"{label} exceeds the facet budget ({max_facets})")
    normalized = []
    seen = set()
    node_references = 0
    for index, raw_face in enumerate(facets):
        values = _sequence(raw_face, f"{label}[{index}]")
        if len(values) not in (3, 4):
            raise ValueError(f"{label}[{index}] must be a triangular or quadrilateral face")
        face = tuple(_positive_id(node_id, f"{label}[{index}][{j}]") for j, node_id in enumerate(values))
        if len(set(face)) != len(face):
            raise ValueError(f"{label}[{index}] contains duplicate node IDs")
        missing = sorted(set(face) - coords.keys())
        if missing:
            raise ValueError(f"{label}[{index}] references unknown node IDs {missing}")
        key = tuple(sorted(face))
        if key in seen:
            raise ValueError(f"{label} contains duplicate face {list(key)}")
        seen.add(key)
        normalized.append((key, face))
        node_references += len(face)
        if node_references > MAX_FACET_NODE_REFERENCES:
            raise ValueError(f"{label} exceeds MAX_FACET_NODE_REFERENCES={MAX_FACET_NODE_REFERENCES}")
    return normalized


def _normalize_interfaces(interfaces, coords, context):
    if interfaces is None:
        context.issue(
            "interfaces", "incomplete", "interfaces_not_provided",
            "No expected interface set was provided; interface coverage is unknown.",
        )
        return None
    rows = _sequence(interfaces, "interfaces")
    if len(rows) > MAX_INTERFACES:
        raise ValueError(f"interfaces exceeds MAX_INTERFACES={MAX_INTERFACES}")

    normalized = []
    seen_ids = set()
    seen_pairs = set()
    seen_faces = set()
    total_facets = 0
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise ValueError(f"interfaces[{index}] must be a dict")
        interface_id = row.get("id")
        if type(interface_id) is not str or not interface_id or len(interface_id) > 256:
            context.issue(
                "interfaces", "incomplete", "interface_id_missing",
                "An interface has no valid id; a generated label is used in the section details.",
                index=index,
            )
            interface_id = f"interface-{index + 1}"
        if interface_id in seen_ids:
            raise ValueError(f"duplicate interface id {interface_id!r}")
        seen_ids.add(interface_id)

        blocks = row.get("blocks")
        block_pair = None
        if blocks is not None:
            values = _sequence(blocks, f"interface {interface_id}.blocks")
            if (len(values) != 2 or any(type(item) is not str or not item or len(item) > 256 for item in values)
                    or values[0] == values[1]):
                raise ValueError(f"interface {interface_id}.blocks must be two distinct non-empty strings")
            block_pair = tuple(sorted(values))
            if block_pair in seen_pairs:
                raise ValueError(f"multiple interface entries declare the same block pair {block_pair}")
            seen_pairs.add(block_pair)
        else:
            context.issue(
                "interfaces", "incomplete", "interface_blocks_missing",
                "Interface ownership is unknown because the expected block pair is absent.",
                interface_id=interface_id,
            )

        facets_provided = "facets" in row and row.get("facets") is not None
        raw_facets = row.get("facets")
        if raw_facets is None:
            context.issue(
                "interfaces", "incomplete", "interface_facets_missing",
                "The expected facet list is absent for an interface.",
                interface_id=interface_id,
            )
            facets = []
        else:
            facets = _normalize_facets(raw_facets, f"interface {interface_id}.facets", coords)
        total_facets += len(facets)
        if total_facets > MAX_DECLARED_FACETS:
            raise ValueError(f"interface facets exceed MAX_DECLARED_FACETS={MAX_DECLARED_FACETS}")
        for key, _face in facets:
            if key in seen_faces:
                raise ValueError(f"duplicate expected interface facet {list(key)}")
            seen_faces.add(key)
        normalized.append({
            "id": interface_id,
            "blocks": block_pair,
            "facets": facets,
            "facets_provided": facets_provided,
        })
    return normalized


def _normalize_cylinders(protected_cylinders, coords, context):
    if protected_cylinders is None:
        context.issue(
            "geometry", "incomplete", "protected_cylinders_not_provided",
            "No protected-cylinder set was provided; protected geometry coverage is unknown.",
        )
        return None
    rows = _sequence(protected_cylinders, "protected_cylinders")
    if len(rows) > MAX_PROTECTED_CYLINDERS:
        raise ValueError(f"protected_cylinders exceeds MAX_PROTECTED_CYLINDERS={MAX_PROTECTED_CYLINDERS}")

    normalized = []
    labels = set()
    total_facets = 0
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise ValueError(f"protected_cylinders[{index}] must be a dict")
        label = row.get("label")
        if type(label) is not str or not label or len(label) > 256:
            raise ValueError(f"protected_cylinders[{index}].label must be a non-empty string")
        if label in labels:
            raise ValueError(f"duplicate protected-cylinder label {label!r}")
        labels.add(label)
        center = _vector3(row.get("center"), f"cylinder {label}.center")
        axis = _vector3(row.get("axis"), f"cylinder {label}.axis")
        axis_norm = math.hypot(*axis)
        if not math.isfinite(axis_norm) or axis_norm == 0.0:
            raise ValueError(f"cylinder {label}.axis must have a finite non-zero length")
        unit_axis = tuple(value / axis_norm for value in axis)
        radius = _finite_number(row.get("radius"), f"cylinder {label}.radius")
        tolerance = _finite_number(row.get("tolerance"), f"cylinder {label}.tolerance")
        if radius <= 0.0:
            raise ValueError(f"cylinder {label}.radius must be positive")
        if tolerance < 0.0:
            raise ValueError(f"cylinder {label}.tolerance must be non-negative")
        raw_facets = row.get("facets")
        if raw_facets is None:
            context.issue(
                "geometry", "incomplete", "cylinder_facets_missing",
                "The protected cylinder has no declared facet list.", label=label,
            )
            facets = []
        else:
            facets = _normalize_facets(raw_facets, f"cylinder {label}.facets", coords)
        if not facets:
            context.issue(
                "geometry", "incomplete", "cylinder_facets_empty",
                "An empty protected-cylinder facet list cannot establish surface coverage.", label=label,
            )
        total_facets += len(facets)
        if total_facets > MAX_DECLARED_FACETS:
            raise ValueError(f"cylinder facets exceed MAX_DECLARED_FACETS={MAX_DECLARED_FACETS}")
        normalized.append({
            "label": label,
            "center": center,
            "axis": unit_axis,
            "radius": radius,
            "tolerance": tolerance,
            "facets": facets,
        })
    return normalized


def _add_cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left, right):
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _sub(left, right):
    result = tuple(a - b for a, b in zip(left, right))
    if any(not math.isfinite(value) for value in result):
        raise ValueError("coordinates overflowed during geometric calculation")
    return result


def _signed_polyhedral_volume(element, coords):
    origin = coords[element["nodes"][0]]
    points = [coords[node_id] for node_id in element["nodes"]]
    extents = tuple(max(point[i] for point in points) - min(point[i] for point in points) for i in range(3))
    if any(not math.isfinite(value) for value in extents):
        raise ValueError(f"element {element['id']} coordinate extent is not finite")
    scale = math.hypot(*extents)
    if not math.isfinite(scale) or (scale and scale > sys.float_info.max ** (1.0 / 3.0)):
        raise ValueError(f"element {element['id']} volume scale is not representable")

    total = 0.0
    for indices in _FACE_TABLE[element["type"]]:
        face_nodes = [element["nodes"][position] for position in indices]
        first = _sub(coords[face_nodes[0]], origin)
        for corner in range(1, len(face_nodes) - 1):
            second = _sub(coords[face_nodes[corner]], origin)
            third = _sub(coords[face_nodes[corner + 1]], origin)
            total += _dot(first, _add_cross(second, third))
    volume = total / 6.0
    if not math.isfinite(volume):
        raise ValueError(f"element {element['id']} signed polyhedral measure is not finite")
    zero_tolerance = scale ** 3 * 1e-12
    return volume, zero_tolerance


def _orientation_relation(first, second):
    count = len(first)
    for offset in range(count):
        rotated = second[offset:] + second[:offset]
        if first == rotated:
            return "same"
        reverse = tuple(reversed(rotated))
        if first == reverse:
            return "opposite"
    return "incompatible"


def _build_face_uses(supported):
    face_uses = defaultdict(list)
    for element_id, element in supported.items():
        for indices in _FACE_TABLE[element["type"]]:
            oriented = tuple(element["nodes"][position] for position in indices)
            face_uses[tuple(sorted(oriented))].append((element_id, oriented))
    return face_uses


def _components(supported, neighbors):
    remaining = set(supported)
    components = []
    while remaining:
        seed = min(remaining)
        pending = [seed]
        reached = set()
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(neighbors[current] - reached)
        remaining.difference_update(reached)
        components.append(sorted(reached))
    return components


def _topology_audit(coords, elements, supported, unsupported, expected_components, context):
    topology = {
        "status": "pass",
        "element_count": len(elements),
        "supported_element_count": len(supported),
        "unsupported_element_count": len(unsupported),
        "unsupported_elements": list(unsupported),
        "component_count": 0,
        "components": [],
        "expected_components": expected_components,
        "interior_face_count": 0,
        "free_face_count": 0,
        "free_facets": [],
        "non_manifold_faces": [],
        "shared_face_orientation_issues": [],
        "skin_edge_issues": [],
        "signed_polyhedral_measure": {
            "method": "oriented triangular fan over CHEXA8/CPENTA6 faces; signed volume is not a Jacobian or full element-quality test",
            "negative_elements": [],
            "degenerate_elements": [],
            "minimum": None,
        },
    }
    if unsupported:
        for item in unsupported:
            context.issue(
                "topology", "incomplete", "unsupported_solid_topology",
                "Only linear CHEXA8 and CPENTA6 facets are implemented.", **item,
            )
    if not elements:
        context.issue("topology", "fail", "empty_mesh", "The snapshot contains no solid elements.")

    measures = []
    for element_id, element in supported.items():
        volume, zero_tolerance = _signed_polyhedral_volume(element, coords)
        measures.append((element_id, volume))
        if element["reported_volume"] is not None and element["reported_volume"] <= 0.0:
            record = {"element_id": element_id, "reported_volume": element["reported_volume"]}
            topology["signed_polyhedral_measure"]["negative_elements"].append(record)
            context.issue(
                "topology", "fail", "non_positive_reported_volume",
                "The supplied element volume is non-positive.", **record,
            )
        if volume < -zero_tolerance:
            record = {"element_id": element_id, "signed_polyhedral_volume": volume}
            topology["signed_polyhedral_measure"]["negative_elements"].append(record)
            context.issue(
                "topology", "fail", "negative_signed_polyhedral_measure",
                "The oriented polyhedral measure is negative; this does not constitute a Jacobian check.",
                **record,
            )
        elif abs(volume) <= zero_tolerance:
            record = {"element_id": element_id, "signed_polyhedral_volume": volume}
            topology["signed_polyhedral_measure"]["degenerate_elements"].append(record)
            context.issue(
                "topology", "fail", "zero_signed_polyhedral_measure",
                "The oriented polyhedral measure is zero or scale-degenerate.", **record,
            )
    if measures:
        topology["signed_polyhedral_measure"]["minimum"] = min(value for _eid, value in measures)

    face_uses = _build_face_uses(supported)
    neighbors = {element_id: set() for element_id in supported}
    skin_edges = defaultdict(list)
    boundary_keys = []
    for key, uses in face_uses.items():
        if len(uses) > 2:
            record = {"nodes": list(key), "element_ids": sorted(use[0] for use in uses)}
            topology["non_manifold_faces"].append(record)
            context.issue(
                "topology", "fail", "non_manifold_face",
                "A face is used by more than two solid elements.", **record,
            )
        elif len(uses) == 2:
            topology["interior_face_count"] += 1
            first_id, first_face = uses[0]
            second_id, second_face = uses[1]
            neighbors[first_id].add(second_id)
            neighbors[second_id].add(first_id)
            relation = _orientation_relation(first_face, second_face)
            if relation != "opposite":
                record = {
                    "nodes": list(key),
                    "element_ids": [first_id, second_id],
                    "relation": relation,
                }
                topology["shared_face_orientation_issues"].append(record)
                context.issue(
                    "topology", "fail", "shared_face_orientation",
                    "The two uses of a shared face are not oppositely oriented.", **record,
                )
        elif len(uses) == 1:
            topology["free_face_count"] += 1
            boundary_keys.append(key)
            oriented = uses[0][1]
            for edge_index, start in enumerate(oriented):
                end = oriented[(edge_index + 1) % len(oriented)]
                skin_edges[tuple(sorted((start, end)))].append((start, end, key))

    topology["free_facets"] = [list(key) for key in sorted(boundary_keys)]
    for edge_key, uses in skin_edges.items():
        reason = None
        if len(uses) == 1:
            reason = "open"
        elif len(uses) > 2:
            reason = "non_manifold"
        elif uses[0][0] == uses[1][0] and uses[0][1] == uses[1][1]:
            reason = "same_direction"
        if reason:
            record = {
                "nodes": list(edge_key),
                "incidence_count": len(uses),
                "reason": reason,
                "facets": [list(use[2]) for use in uses],
            }
            topology["skin_edge_issues"].append(record)
            context.issue(
                "topology", "fail", "skin_edge_issue",
                "The exterior skin has an open, non-manifold, or inconsistently oriented edge.",
                **record,
            )

    components = _components(supported, neighbors)
    topology["components"] = [
        {"index": index, "element_ids": component}
        for index, component in enumerate(components, start=1)
    ]
    topology["component_count"] = len(components)
    if not unsupported and len(components) != expected_components:
        context.issue(
            "topology", "fail", "component_count_mismatch",
            "Face-connected component count does not match expected_components.",
            expected=expected_components, actual=len(components),
        )
    elif unsupported:
        context.issue(
            "topology", "incomplete", "component_count_incomplete",
            "Unsupported elements prevent a complete face-component count.",
            known_component_count=len(components), expected=expected_components,
        )
    topology["status"] = context.states["topology"]
    return topology, face_uses


def _boundary_audit(expected_boundary, face_uses, unsupported, coords, context):
    actual = sorted(key for key, uses in face_uses.items() if len(uses) == 1)
    section = {
        "status": "pass",
        "provided": expected_boundary is not None,
        "expected_facets": None,
        "actual_free_facets": [list(key) for key in actual],
        "missing_facets": [],
        "unexpected_facets": [],
        "comparison_complete": False,
    }
    if expected_boundary is None:
        context.issue(
            "expected_boundary", "incomplete", "expected_boundary_not_provided",
            "The full expected boundary set is required to detect local cracks and boundary omissions.",
        )
        section["status"] = context.states["expected_boundary"]
        return section, None

    normalized = _normalize_facets(expected_boundary, "expected_boundary", coords)
    expected = sorted(key for key, _face in normalized)
    section["expected_facets"] = [list(key) for key in expected]
    if unsupported:
        context.issue(
            "expected_boundary", "incomplete", "boundary_comparison_incomplete",
            "Unsupported element types/orders prevent comparison against the complete actual free boundary.",
            supported_free_facet_count=len(actual), expected_facet_count=len(expected),
        )
    else:
        expected_set, actual_set = set(expected), set(actual)
        missing = sorted(expected_set - actual_set)
        unexpected = sorted(actual_set - expected_set)
        section["missing_facets"] = [list(key) for key in missing]
        section["unexpected_facets"] = [list(key) for key in unexpected]
        section["comparison_complete"] = True
        if missing or unexpected:
            context.issue(
                "expected_boundary", "fail", "boundary_set_mismatch",
                "The complete expected boundary and actual free-face sets differ.",
                missing_count=len(missing), unexpected_count=len(unexpected),
            )
        else:
            section["status"] = "pass"
    section["status"] = context.states["expected_boundary"]
    return section, set(expected)


def _audit_interfaces(interfaces, elements, supported, unsupported, face_uses, coords, context):
    normalized = _normalize_interfaces(interfaces, coords, context)
    actual_by_pair = defaultdict(set)
    unknown_owner_facets = []
    for key, uses in face_uses.items():
        if len(uses) != 2:
            continue
        first = elements[uses[0][0]]["block_id"]
        second = elements[uses[1][0]]["block_id"]
        if type(first) is not str or type(second) is not str:
            unknown_owner_facets.append({"nodes": list(key), "element_ids": [uses[0][0], uses[1][0]]})
        elif first != second:
            actual_by_pair[tuple(sorted((first, second)))].add(key)

    section = {
        "status": context.states["interfaces"],
        "provided": interfaces is not None,
        "groups": [],
        "actual_cross_block_facets": [
            {"blocks": list(pair), "facets": [list(key) for key in sorted(facets)]}
            for pair, facets in sorted(actual_by_pair.items())
        ],
        "unknown_owner_facets": unknown_owner_facets,
        "unlisted_actual_facets": [],
    }
    if unknown_owner_facets:
        context.issue(
            "interfaces", "incomplete", "interface_side_ownership_unknown",
            "At least one shared face has a side without a string block_id; it cannot be assigned or excluded as an interface.",
            facets=unknown_owner_facets,
        )
    if unsupported:
        context.issue(
            "interfaces", "incomplete", "interface_topology_incomplete",
            "Unsupported elements prevent complete shared-face and interface coverage.",
        )

    declared_pairs = set()
    if normalized is not None:
        for group in normalized:
            pair = group["blocks"]
            expected_keys = {key for key, _face in group["facets"]}
            actual_keys = actual_by_pair.get(pair, set()) if pair is not None else set()
            details = {
                "id": group["id"],
                "blocks": list(pair) if pair is not None else None,
                "expected_facets": [list(key) for key in sorted(expected_keys)],
                "actual_facets": [list(key) for key in sorted(actual_keys)],
                "missing_facets": [],
                "unexpected_facets": [],
                "ownership_unknown_facets": [],
            }
            group_status = "pass"
            unknown_expected_keys = set()
            for key in expected_keys:
                uses = face_uses.get(key, [])
                if len(uses) == 2:
                    owners = [elements[uses[0][0]]["block_id"], elements[uses[1][0]]["block_id"]]
                    if any(type(owner) is not str for owner in owners):
                        details["ownership_unknown_facets"].append(list(key))
                        unknown_expected_keys.add(key)
                        group_status = _combine(group_status, "incomplete")
                    elif pair is not None and tuple(sorted(owners)) != pair:
                        group_status = "fail"
                elif unsupported:
                    group_status = _combine(group_status, "incomplete")
                else:
                    group_status = "fail"

            if pair is None:
                group_status = _combine(group_status, "incomplete")
            else:
                declared_pairs.add(pair)
                verifiable_expected_keys = expected_keys - unknown_expected_keys
                if group["facets_provided"] and verifiable_expected_keys != actual_keys:
                    if unsupported:
                        group_status = _combine(group_status, "incomplete")
                    else:
                        group_status = "fail"
                    details["missing_facets"] = [list(key) for key in sorted(verifiable_expected_keys - actual_keys)]
                    details["unexpected_facets"] = [list(key) for key in sorted(actual_keys - expected_keys)]
                elif not group["facets_provided"]:
                    group_status = _combine(group_status, "incomplete")
            if not group["facets"]:
                group_status = _combine(group_status, "incomplete")
            details["status"] = group_status
            section["groups"].append(details)
            if group_status != "pass":
                context.issue(
                    "interfaces", group_status, "interface_group_unverified" if group_status == "incomplete" else "interface_face_mismatch",
                    "Expected interface faces and both block owners were not fully verified." if group_status == "incomplete"
                    else "Expected and actual interface faces or side block ownership differ.",
                    interface_id=group["id"],
                    missing_facets=details["missing_facets"],
                    unexpected_facets=details["unexpected_facets"],
                    ownership_unknown_facets=details["ownership_unknown_facets"],
                )

        unlisted = []
        for pair, keys in sorted(actual_by_pair.items()):
            if pair not in declared_pairs:
                unlisted.extend({"blocks": list(pair), "nodes": list(key)} for key in sorted(keys))
        section["unlisted_actual_facets"] = unlisted
        if unlisted:
            severity = "incomplete" if unsupported else "fail"
            context.issue(
                "interfaces", severity,
                "unlisted_actual_interfaces" if severity == "fail" else "possible_unlisted_interfaces",
                "Actual cross-block shared faces are not covered by an expected interface entry.",
                facets=unlisted,
            )

    section["status"] = context.states["interfaces"]
    return section


def _measure_radial_distance(point, center, axis):
    relative = _sub(point, center)
    axial = _dot(relative, axis)
    radial = tuple(relative[index] - axial * axis[index] for index in range(3))
    if any(not math.isfinite(value) for value in radial):
        raise ValueError("radial-distance calculation overflowed")
    result = math.hypot(*radial)
    if not math.isfinite(result):
        raise ValueError("radial-distance calculation is not finite")
    return result


def _enforce_declared_facet_budget(boundary, interfaces, geometry):
    facet_count = len(boundary.get("expected_facets") or [])
    facet_count += sum(len(group["expected_facets"]) for group in interfaces.get("groups", []))
    facet_count += sum(cylinder["facet_count"] for cylinder in geometry.get("cylinders", []))
    reference_count = sum(len(face) for face in (boundary.get("expected_facets") or []))
    reference_count += sum(
        len(face)
        for group in interfaces.get("groups", [])
        for face in group["expected_facets"]
    )
    reference_count += sum(
        len(face["nodes"])
        for cylinder in geometry.get("cylinders", [])
        for face in cylinder["facets"]
    )
    if facet_count > MAX_DECLARED_FACETS:
        raise ValueError(f"combined declared facets exceed MAX_DECLARED_FACETS={MAX_DECLARED_FACETS}")
    if reference_count > MAX_FACET_NODE_REFERENCES:
        raise ValueError(
            f"combined declared facets exceed MAX_FACET_NODE_REFERENCES={MAX_FACET_NODE_REFERENCES}"
        )
    return facet_count, reference_count


def _audit_cylinders(coords, elements, supported, unsupported, face_uses, expected_boundary_set,
                     expected_boundary_provided, protected_cylinders, context):
    normalized = _normalize_cylinders(protected_cylinders, coords, context)
    section = {
        "status": context.states["geometry"],
        "provided": protected_cylinders is not None,
        "coverage_basis": "explicit facets are the caller-declared complete protected-surface scope; every listed node is measured",
        "expected_boundary_provided": expected_boundary_provided,
        "cylinders": [],
    }
    if normalized is None:
        section["status"] = context.states["geometry"]
        return section
    if not expected_boundary_provided:
        context.issue(
            "geometry", "incomplete", "cylinder_boundary_relation_unknown",
            "Protected-cylinder facets cannot be reconciled to the complete expected boundary because expected_boundary is absent.",
        )
    if unsupported:
        context.issue(
            "geometry", "incomplete", "cylinder_surface_mapping_incomplete",
            "Unsupported elements prevent complete free-face ownership checks for protected-cylinder facets.",
        )

    for cylinder in normalized:
        label = cylinder["label"]
        facet_records = []
        measurements = []
        mapped_count = 0
        out_of_tolerance = 0
        max_error = None
        for facet_index, (key, _oriented) in enumerate(cylinder["facets"]):
            uses = face_uses.get(key, [])
            is_free = len(uses) == 1
            is_expected = expected_boundary_set is not None and key in expected_boundary_set
            if is_free:
                mapped_count += 1
            face_status = "pass"
            if not is_free:
                if unsupported:
                    face_status = "incomplete"
                    context.issue(
                        "geometry", "incomplete", "cylinder_facet_free_face_unknown",
                        "The declared cylinder facet cannot be confirmed as an actual free face.",
                        label=label, facet_index=facet_index, nodes=list(key), actual_use_count=len(uses),
                    )
                else:
                    face_status = "fail"
                    context.issue(
                        "geometry", "fail", "cylinder_facet_not_free_face",
                        "A declared protected-cylinder facet is not exactly one actual free face.",
                        label=label, facet_index=facet_index, nodes=list(key), actual_use_count=len(uses),
                    )
            if expected_boundary_set is not None and not is_expected:
                face_status = _combine(face_status, "fail")
                context.issue(
                    "geometry", "fail", "cylinder_facet_not_in_expected_boundary",
                    "A declared protected-cylinder facet is absent from expected_boundary.",
                    label=label, facet_index=facet_index, nodes=list(key),
                )

            for node_id in _oriented:
                radial_distance = _measure_radial_distance(coords[node_id], cylinder["center"], cylinder["axis"])
                error = abs(radial_distance - cylinder["radius"])
                within = error <= cylinder["tolerance"]
                max_error = error if max_error is None else max(max_error, error)
                record = {
                    "facet_index": facet_index,
                    "facet_nodes": list(key),
                    "node_id": node_id,
                    "radial_distance": radial_distance,
                    "absolute_radial_error": error,
                    "tolerance": cylinder["tolerance"],
                    "within_tolerance": within,
                }
                measurements.append(record)
                if not within:
                    out_of_tolerance += 1
                    face_status = "fail"
                    context.issue(
                        "geometry", "fail", "cylinder_node_out_of_tolerance",
                        "A declared cylinder-facet node exceeds radial tolerance; all listed nodes are measured without filtering.",
                        label=label, **record,
                    )
            facet_records.append({
                "nodes": list(key),
                "actual_free_face": is_free,
                "expected_boundary_member": is_expected if expected_boundary_set is not None else None,
                "actual_use_count": len(uses),
                "status": face_status,
            })

        cylinder_status = "pass"
        for facet in facet_records:
            cylinder_status = _combine(cylinder_status, facet["status"])
        if not cylinder["facets"]:
            cylinder_status = _combine(cylinder_status, "incomplete")
        if unsupported:
            cylinder_status = _combine(cylinder_status, "incomplete")
        result = {
            "label": label,
            "center": list(cylinder["center"]),
            "axis_unit": list(cylinder["axis"]),
            "radius": cylinder["radius"],
            "tolerance": cylinder["tolerance"],
            "facet_count": len(facet_records),
            "mapped_free_facet_count": mapped_count,
            "measured_node_occurrences": len(measurements),
            "unique_measured_node_count": len({record["node_id"] for record in measurements}),
            "out_of_tolerance_node_occurrences": out_of_tolerance,
            "maximum_absolute_radial_error": max_error,
            "facets": facet_records,
            "measurements": measurements,
            "status": cylinder_status,
        }
        section["cylinders"].append(result)
        if cylinder_status != "pass":
            context.issue(
                "geometry", cylinder_status,
                "protected_cylinder_unverified" if cylinder_status == "incomplete" else "protected_cylinder_failed",
                "Protected-cylinder facet ownership, boundary coverage, or radial tolerance did not fully pass.",
                label=label,
                facet_count=len(facet_records),
                mapped_free_facet_count=mapped_count,
                measured_node_occurrences=len(measurements),
                out_of_tolerance_node_occurrences=out_of_tolerance,
            )
    section["status"] = context.states["geometry"]
    return section


def audit_protected_cylinders(snapshot, protected_cylinders, *, expected_boundary=None):
    """Audit every declared cylinder facet and node against pure snapshot data.

    ``facets`` is the caller's complete scope declaration. The helper verifies
    each face against the actual one-use skin and, when supplied, the complete
    expected boundary set. It never selects nodes/faces by whether they meet
    tolerance. The return value contains the full ``geometry`` section and all
    its diagnostics, suitable for independent persistence or paging.
    """
    context = _AuditContext()
    coords, elements, supported, unsupported = _prepare_snapshot(snapshot)
    face_uses = _build_face_uses(supported)
    boundary_section, boundary_set = _boundary_audit(
        expected_boundary, face_uses, unsupported, coords, context,
    )
    geometry = _audit_cylinders(
        coords, elements, supported, unsupported, face_uses, boundary_set,
        expected_boundary is not None, protected_cylinders, context,
    )
    _enforce_declared_facet_budget(boundary_section, {"groups": []}, geometry)
    geometry["status"] = _combine(context.states["expected_boundary"], context.states["geometry"])
    return {
        "status": geometry["status"],
        "geometry": geometry,
        "expected_boundary": boundary_section,
        "diagnostics": context.diagnostics,
    }


def audit_snapshot(snapshot, *, interfaces=None, protected_cylinders=None,
                    expected_boundary=None, expected_components=1):
    """Audit an S25 nodes/elements snapshot without importing or calling ANSA.

    Supported solid topology is exactly linear CHEXA8 and CPENTA6. Unknown
    types/orders make dependent checks incomplete. Native quality and
    non-adjacent-solid intersection checks are deliberately unavailable.

    Interface contract::

        [{"id": "joint-1", "blocks": ["A", "B"],
          "facets": [[1, 2, 3, 4], ...]}]

    ``blocks`` names the two element ``block_id`` owners, in either order.
    ``expected_boundary`` and each cylinder/interface ``facets`` are complete
    lists of 3- or 4-node faces. Full evidence lives in independent ``sections``.
    """
    if type(expected_components) is not int or not 1 <= expected_components <= MAX_ELEMENTS:
        raise ValueError(f"expected_components must be an integer from 1 to {MAX_ELEMENTS}")
    context = _AuditContext()
    coords, elements, supported, unsupported = _prepare_snapshot(snapshot)

    topology, face_uses = _topology_audit(
        coords, elements, supported, unsupported, expected_components, context,
    )
    boundary, boundary_set = _boundary_audit(
        expected_boundary, face_uses, unsupported, coords, context,
    )
    interface_section = _audit_interfaces(
        interfaces, elements, supported, unsupported, face_uses, coords, context,
    )
    geometry = _audit_cylinders(
        coords, elements, supported, unsupported, face_uses, boundary_set,
        expected_boundary is not None, protected_cylinders, context,
    )
    declared_facet_count, facet_node_reference_count = _enforce_declared_facet_budget(
        boundary, interface_section, geometry,
    )

    quality = {
        "status": "incomplete",
        "native_quality_available": False,
        "checked": False,
        "reason": "No native ANSA quality criteria/results are present in this pure-data snapshot.",
        "signed_polyhedral_measure_is_not_quality_acceptance": True,
    }
    context.issue(
        "quality", "incomplete", "native_quality_unavailable",
        "Native quality evidence was not supplied; signed polyhedral volume is not a substitute.",
    )
    intersections = {
        "status": "incomplete",
        "checked": False,
        "scope": "non-adjacent solid-solid intersections are not implemented",
    }
    context.issue(
        "intersections", "incomplete", "non_adjacent_intersections_unchecked",
        "Non-adjacent solid-solid intersections were not checked.",
    )

    topology_status = _combine(
        context.states["topology"],
        context.states["interfaces"],
        context.states["expected_boundary"],
    )
    geometry_status = _combine(
        context.states["geometry"], context.states["expected_boundary"],
    )
    quality_status = context.states["quality"]
    overall_status = _combine(
        topology_status, geometry_status, quality_status, context.states["intersections"],
    )

    topology["status"] = _combine(context.states["topology"], context.states["interfaces"], context.states["expected_boundary"])
    boundary["status"] = context.states["expected_boundary"]
    interface_section["status"] = context.states["interfaces"]
    geometry["status"] = _combine(context.states["geometry"], context.states["expected_boundary"])
    quality["status"] = quality_status
    intersections["status"] = context.states["intersections"]
    summary = {
        "element_count": len(elements),
        "supported_element_count": len(supported),
        "unsupported_element_count": len(unsupported),
        "component_count": topology["component_count"],
        "expected_component_count": expected_components,
        "interior_face_count": topology["interior_face_count"],
        "actual_free_face_count": topology["free_face_count"],
        "expected_boundary_facet_count": len(boundary["expected_facets"] or []),
        "boundary_comparison_complete": boundary["comparison_complete"],
        "interface_group_count": len(interface_section["groups"]),
        "protected_cylinder_count": len(geometry["cylinders"]),
        "native_quality_available": False,
        "non_adjacent_intersections_checked": False,
        "declared_facet_count": declared_facet_count,
        "facet_node_reference_count": facet_node_reference_count,
        "input_budgets": {
            "max_nodes": MAX_NODES,
            "max_elements": MAX_ELEMENTS,
            "max_declared_facets": MAX_DECLARED_FACETS,
            "max_facet_node_references": MAX_FACET_NODE_REFERENCES,
            "max_interfaces": MAX_INTERFACES,
            "max_protected_cylinders": MAX_PROTECTED_CYLINDERS,
        },
    }
    sections = {
        "topology": topology,
        "interfaces": interface_section,
        "expected_boundary": boundary,
        "geometry": geometry,
        "quality": quality,
        "intersections": intersections,
    }
    return {
        "status": overall_status,
        "topology_status": topology_status,
        "geometry_status": geometry_status,
        "quality_status": quality_status,
        "summary": summary,
        "diagnostics": context.diagnostics,
        "sections": sections,
    }


__all__ = ["audit_snapshot", "audit_protected_cylinders"]
