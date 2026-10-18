"""Guarded ABAQUS Map-volume meshing from explicit SHELL regions."""

from __future__ import annotations

import math
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import element_types, entity_catalog as catalog, model_files, volume_meshing


_TOOL = "mesh_hex_map"
_MAX_MODEL_ENTITIES = 10000
_MAX_REGION_ITEMS = 200
_MAX_TOTAL_REGION_ITEMS = 400
_MAX_VOLUMES_FOR_OVERLAP_CHECK = 1000
_MAX_STEPS = 10000
_MAX_CARD_FIELDS = 512
_SNAPSHOT_BUDGET = MAX_FRAME_BYTES // 4
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_VOLUME_REL_TOL = 1e-6

_SHELL_NODE_FIELDS = tuple(f"G{index}" for index in range(1, 5))
_SOLID_NODE_FIELDS = tuple(f"G{index}" for index in range(1, 9))
_HIGHER_ORDER_FIELDS = tuple(f"G{index}" for index in range(9, 29))
_DERIVED_FIELDS = frozenset({"numelem"})
_GAUSS = 1.0 / math.sqrt(3.0)
_HEXA_NATURAL = (
    (-1.0, -1.0, -1.0),
    (1.0, -1.0, -1.0),
    (1.0, 1.0, -1.0),
    (-1.0, 1.0, -1.0),
    (-1.0, -1.0, 1.0),
    (1.0, -1.0, 1.0),
    (1.0, 1.0, 1.0),
    (-1.0, 1.0, 1.0),
)
_HEXA_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)
_REQUIRED_LABELS = {
    "NODE": ("X", "Y", "Z"),
    "SHELL": ("PID", *_SHELL_NODE_FIELDS),
    "SOLID": ("type", "EID", "PID", *_SOLID_NODE_FIELDS),
    "VOLUME": ("ID", "PID", "Type", "Status"),
    # FACE cards are read leniently for whole-model scope. This batch does
    # not accept FACE as a selected Map region.
    "FACE": (),
    "ANSAPART": ("Name",),
    "SOLID_SECTION": ("MID",),
    "MATERIAL": ("Name",),
}
_CATEGORY_TYPES = {
    "nodes": ("NODE", "NODE"),
    "shells": ("SHELL", "SHELL"),
    "solids": ("SOLID", "SOLID"),
    "volumes": ("VOLUME", "VOLUME"),
    "faces": ("FACE", "FACE"),
    "parts": ("ANSAPART", "ANSAPART"),
    "sections": ("SOLID_SECTION", "SOLID_SECTION"),
    "materials": ("MATERIAL", "MATERIAL"),
}


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _encoded(value):
    return element_types._encoded(value)


def _field_key(name):
    return "".join(char.casefold() for char in name if char.isalnum())


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_number(value, label):
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _labels(entity, deck, label):
    getter = getattr(entity, "card_fields", None)
    if not callable(getter):
        raise NotImplementedError(f"{label} card_fields capability is unavailable")
    labels = getter(deck)
    if type(labels) not in (list, tuple):
        raise RuntimeError(f"{label} card_fields did not return a field-name list")
    if len(labels) > _MAX_CARD_FIELDS:
        raise ValueError(f"{label} card_fields exceeds the {_MAX_CARD_FIELDS}-field limit")
    if any(type(name) is not str or not name for name in labels):
        raise RuntimeError(f"{label} card_fields returned an invalid field name")
    if len(labels) != len(set(labels)):
        raise RuntimeError(f"{label} card_fields returned duplicate field names")
    return tuple(labels)


def _stable_fields(kind, labels):
    if kind == "ANSAPART":
        return tuple(
            name for name in labels
            if name in {"Name", "Module Id", "ID"} or "frozen" in name.casefold()
        )
    return tuple(name for name in labels if _field_key(name) not in _DERIVED_FIELDS)


def _row(base, deck, entity, reference, kind):
    labels = _labels(entity, deck, f"{kind} {reference['id']}")
    missing = sorted(set(_REQUIRED_LABELS[kind]) - set(labels))
    if missing:
        raise NotImplementedError(
            f"{kind} {reference['id']} lacks required card field(s): {', '.join(missing)}"
        )
    selected = _stable_fields(kind, labels)
    values = catalog._read_card(base, deck, entity, reference, selected)
    return {
        "entity": reference,
        "card_fields": list(selected),
        "fields": values,
    }


def _collect(base, deck, entity_type, expected_type=None):
    collect = getattr(base, "CollectEntities", None)
    if not callable(collect):
        raise NotImplementedError("ANSA base.CollectEntities is unavailable")
    entities = collect(
        deck,
        None,
        entity_type,
        False,
        filter_visible=False,
        hidden_entities=True,
    )
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({entity_type}) did not return an entity list")
    if len(entities) > _MAX_MODEL_ENTITIES:
        raise ValueError(
            f"{entity_type} collection exceeds the {_MAX_MODEL_ENTITIES}-entity limit"
        )

    pairs = []
    seen = set()
    for entity in entities:
        reference = catalog._entity_reference(
            base, deck, entity, expected_type
        )
        key = (reference["type"], reference["id"])
        if key in seen:
            raise RuntimeError(
                f"CollectEntities({entity_type}) contains duplicate identities"
            )
        seen.add(key)
        pairs.append((entity, reference))
    pairs.sort(key=lambda pair: (pair[1]["type"], pair[1]["id"]))
    return pairs


def _capture_snapshot(base, deck):
    all_pairs = _collect(base, deck, "__ALL_ENTITIES__")
    all_refs = [reference for _, reference in all_pairs]
    all_keys = {(reference["type"], reference["id"]) for reference in all_refs}

    snapshot = {
        "complete": True,
        "deck": deck,
        "entity_count": len(all_refs),
        "entity_references": all_refs,
    }
    for category, (entity_type, expected_type) in _CATEGORY_TYPES.items():
        pairs = _collect(base, deck, entity_type, expected_type)
        for _entity, reference in pairs:
            if (reference["type"], reference["id"]) not in all_keys:
                raise RuntimeError(
                    f"typed {entity_type} collection is missing from __ALL_ENTITIES__"
                )
        snapshot[category] = [
            _row(
                base,
                deck,
                entity,
                reference,
                expected_type,
            )
            for entity, reference in pairs
        ]
    snapshot["counts"] = {
        category: len(snapshot[category]) for category in _CATEGORY_TYPES
    }
    return snapshot


def _normalise_region(value, label):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    catalog._object(value, ("type", "id", "expected_fields"), label=label)
    entity_type = value["type"]
    if entity_type != "SHELL":
        raise ValueError(
            f"{label}.type must be SHELL; FACE regions are not supported in this batch"
        )
    entity_id = _positive_int(value["id"], f"{label}.id")
    expected = value["expected_fields"]
    if type(expected) is not dict or not 1 <= len(expected) <= 100:
        raise ValueError(f"{label}.expected_fields must contain 1 to 100 card values")
    if any(type(name) is not str or not name for name in expected):
        raise ValueError(f"{label}.expected_fields keys must be non-empty strings")
    required = {"PID", *_SHELL_NODE_FIELDS}
    missing = sorted(required - expected.keys())
    if missing:
        raise ValueError(
            f"{label}.expected_fields must include: {', '.join(missing)}"
        )
    return {
        "type": entity_type,
        "id": entity_id,
        "expected_fields": dict(expected),
    }


def _normalise_entity_ref(value, label, expected_type):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    catalog._object(value, ("deck", "type", "id", "expected_fields"), label=label)
    if type(value["deck"]) is not int:
        raise TypeError(f"{label}.deck must be an integer")
    if value["type"] != expected_type:
        raise ValueError(f"{label}.type must be {expected_type}")
    entity_id = _positive_int(value["id"], f"{label}.id")
    expected = value["expected_fields"]
    if type(expected) is not dict or not 1 <= len(expected) <= 100:
        raise ValueError(f"{label}.expected_fields must contain 1 to 100 card values")
    if any(type(name) is not str or not name for name in expected):
        raise ValueError(f"{label}.expected_fields keys must be non-empty strings")
    return {
        "deck": value["deck"],
        "type": expected_type,
        "id": entity_id,
        "expected_fields": dict(expected),
    }


def _validate_inputs(params):
    catalog._object(
        params,
        (
            "deck",
            "master",
            "slave",
            "round",
            "property",
            "backup_path",
        ),
        (
            "method",
            "steps",
            "target_length",
            "part",
            "light_volume_representation",
        ),
        label="params",
    )
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")

    regions = {}
    total_items = 0
    entity_types = set()
    keys = set()
    for name in ("master", "slave", "round"):
        values = params[name]
        if type(values) is not list or not 1 <= len(values) <= _MAX_REGION_ITEMS:
            raise ValueError(
                f"{name} must contain 1 to {_MAX_REGION_ITEMS} entity conditions"
            )
        normalised = []
        for index, value in enumerate(values):
            item = _normalise_region(value, f"{name}[{index}]")
            key = (item["type"], item["id"])
            if key in keys:
                raise ValueError(
                    "master, slave and round must not contain duplicate entity references"
                )
            keys.add(key)
            entity_types.add(item["type"])
            normalised.append(item)
        total_items += len(normalised)
        regions[name] = normalised
    if total_items > _MAX_TOTAL_REGION_ITEMS:
        raise ValueError(
            f"master, slave and round may contain at most {_MAX_TOTAL_REGION_ITEMS} entities"
        )
    if entity_types != {"SHELL"}:
        raise ValueError(
            "master, slave and round must use SHELL entities only in this batch"
        )

    method = params.get("method", "Normal parts")
    if method not in {"Normal parts", "Thin parts"}:
        raise ValueError("method must be 'Normal parts' or 'Thin parts'")

    steps = params.get("steps")
    target_length = params.get("target_length")
    if (steps is None) == (target_length is None):
        raise ValueError("provide exactly one of steps or target_length")
    if steps is not None:
        if type(steps) is not int or not 1 <= steps <= _MAX_STEPS:
            raise ValueError(f"steps must be an integer from 1 to {_MAX_STEPS}")
    else:
        target_length = _finite_number(target_length, "target_length")
        if target_length <= 0:
            raise ValueError("target_length must be a positive finite number")

    part = params.get("part")
    if part is not None:
        part = _normalise_entity_ref(part, "part", "ANSAPART")
        if part["deck"] != params["deck"]:
            raise ValueError("part.deck must match params.deck")
    prop = _normalise_entity_ref(params["property"], "property", "SOLID_SECTION")
    if prop["deck"] != params["deck"]:
        raise ValueError("property.deck must match params.deck")
    if "MID" not in prop["expected_fields"]:
        raise ValueError("property.expected_fields must include MID")

    light = params.get("light_volume_representation", False)
    if type(light) is not bool:
        raise TypeError("light_volume_representation must be a boolean")
    backup_path = params["backup_path"]
    if type(backup_path) is not str or not backup_path:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    normalised = {
        "deck": params["deck"],
        "master": regions["master"],
        "slave": regions["slave"],
        "round": regions["round"],
        "method": method,
        "steps": steps,
        "target_length": target_length,
        "part": part,
        "property": prop,
        "light_volume_representation": light,
        "backup_path": backup_path,
    }
    try:
        size = len(_encoded(normalised))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mesh_hex_map request is not strict JSON: {exc}") from exc
    if size > _REQUEST_BUDGET:
        raise ValueError("mesh_hex_map request exceeds the 128 KiB budget")
    return normalised


def _freeze_value(value):
    return type(value) is str and value.strip().casefold() in {"no", "0", ""}


def _validate_freeze_fields(base, deck, entity, reference, label):
    labels = _labels(entity, deck, label)
    fields = [
        name for name in labels
        if _field_key(name) in {"frozen", "frozenid", "frozendelete"}
    ]
    if not fields:
        return
    values = catalog._read_card(base, deck, entity, reference, tuple(fields))
    for name, value in values.items():
        if value is None or value == "":
            continue
        if not _freeze_value(value) and value != 0:
            raise ValueError(f"{label} is frozen or has an unknown {name} state")


def _resolve_conditions(base, deck, params, snapshot):
    node_ids = {row["entity"]["id"] for row in snapshot["nodes"]}
    resolved = {"master": [], "slave": [], "round": []}
    source_nodes = set()
    for name in ("master", "slave", "round"):
        for item in params[name]:
            entity, reference, _expected, _ignored_labels = catalog._validate_entity_values(
                base,
                deck,
                item["type"],
                item["id"],
                item["expected_fields"],
            )
            fields = catalog._read_card(
                base, deck, entity, reference, ("PID", *_SHELL_NODE_FIELDS)
            )
            _positive_int(fields["PID"], f"SHELL {reference['id']} PID")
            nodes = [
                _positive_int(fields[field], f"SHELL {reference['id']} {field}")
                for field in _SHELL_NODE_FIELDS
            ]
            if len(set(nodes)) != 4:
                raise ValueError(
                    f"SHELL {reference['id']} must use four distinct nodes"
                )
            missing = sorted(set(nodes) - node_ids)
            if missing:
                raise RuntimeError(
                    f"SHELL {reference['id']} references missing NODE(s): "
                    + ", ".join(str(value) for value in missing)
                )
            source_nodes.update(nodes)
            _validate_freeze_fields(
                base, deck, entity, reference, f"SHELL {reference['id']}"
            )
            resolved[name].append((entity, reference))

    part = None
    if params["part"] is not None:
        part_item = params["part"]
        part_entity, part_reference, _expected, _ignored_labels = catalog._validate_entity_values(
            base,
            deck,
            "ANSAPART",
            part_item["id"],
            part_item["expected_fields"],
        )
        part = (part_entity, part_reference)

    prop_item = params["property"]
    prop_entity, prop_reference, _expected, _ignored_labels = catalog._validate_entity_values(
        base,
        deck,
        "SOLID_SECTION",
        prop_item["id"],
        prop_item["expected_fields"],
    )
    mid = _positive_int(
        catalog._read_card(base, deck, prop_entity, prop_reference, ("MID",))["MID"],
        "SOLID_SECTION MID",
    )
    material_entity, material_reference = catalog._get_entity(
        base, deck, "MATERIAL", mid
    )
    _labels(material_entity, deck, f"MATERIAL {material_reference['id']}")
    return {
        "regions": resolved,
        "part": part,
        "property": (prop_entity, prop_reference),
        "material": (material_entity, material_reference),
        "source_nodes": sorted(source_nodes),
    }


def _reject_existing_boundary(base, deck, snapshot, region_keys):
    if len(snapshot["volumes"]) > _MAX_VOLUMES_FOR_OVERLAP_CHECK:
        raise ValueError(
            f"existing VOLUME count exceeds the {_MAX_VOLUMES_FOR_OVERLAP_CHECK}-volume overlap-check limit"
        )
    for row in snapshot["volumes"]:
        volume, _reference = catalog._get_entity(
            base, deck, "VOLUME", row["entity"]["id"]
        )
        shells = base.CollectEntities(deck, volume, "SHELL")
        if shells is None or (type(shells) is int and shells == 0):
            continue
        if type(shells) not in (list, tuple):
            raise RuntimeError(
                f"existing VOLUME {row['entity']['id']} boundary is not a SHELL list"
            )
        for shell in shells:
            reference = catalog._entity_reference(base, deck, shell, "SHELL")
            if (reference["type"], reference["id"]) in region_keys:
                raise ValueError(
                    f"SHELL {reference['id']} already belongs to existing VOLUME "
                    f"{row['entity']['id']}"
                )


def _node_coordinates(snapshot):
    result = {}
    for row in snapshot["nodes"]:
        fields = row["fields"]
        result[row["entity"]["id"]] = tuple(
            _finite_number(fields[name], f"NODE {row['entity']['id']} {name}")
            for name in ("X", "Y", "Z")
        )
    return result


def _region_points(base, deck, entities, coordinates):
    points = []
    centers = []
    for entity, reference in entities:
        fields = catalog._read_card(
            base, deck, entity, reference, _SHELL_NODE_FIELDS
        )
        nodes = [
            _positive_int(fields[field], f"SHELL {reference['id']} {field}")
            for field in _SHELL_NODE_FIELDS
        ]
        shell_points = []
        for node_id in nodes:
            if node_id not in coordinates:
                raise RuntimeError(
                    f"SHELL {reference['id']} references an unknown NODE {node_id}"
                )
            shell_points.append(coordinates[node_id])
        points.extend(shell_points)
        centers.append(_average(shell_points))
    return points, centers


def _average(points):
    return tuple(
        sum(point[axis] for point in points) / len(points)
        for axis in range(3)
    )


def _local_diameter(points):
    mins = [min(point[axis] for point in points) for axis in range(3)]
    maxs = [max(point[axis] for point in points) for axis in range(3)]
    diameter = math.sqrt(
        sum((maxs[axis] - mins[axis]) ** 2 for axis in range(3))
    )
    if not math.isfinite(diameter) or diameter <= 0:
        raise ValueError("region has no finite local diameter")
    return diameter


def _projection_span(points, direction):
    projections = [
        sum(value * axis for value, axis in zip(point, direction))
        for point in points
    ]
    return max(projections) - min(projections)


def _derive_steps(base, deck, params, resolved, snapshot):
    if params["steps"] is not None:
        return {
            "source": "steps",
            "steps": params["steps"],
            "target_length": None,
            "sweep_length": None,
            "planarity": None,
        }
    coordinates = _node_coordinates(snapshot)
    master_points, _master_centers = _region_points(
        base, deck, resolved["regions"]["master"], coordinates
    )
    slave_points, _slave_centers = _region_points(
        base, deck, resolved["regions"]["slave"], coordinates
    )
    master_center = _average(master_points)
    slave_center = _average(slave_points)
    vector = tuple(a - b for a, b in zip(slave_center, master_center))
    length = math.sqrt(sum(value * value for value in vector))
    local_scale = max(
        _local_diameter(master_points),
        _local_diameter(slave_points),
    )
    planarity_tolerance = local_scale * 1e-6
    if not math.isfinite(length) or length <= planarity_tolerance:
        raise ValueError(
            "master and slave regions have no finite separation for target_length"
        )
    direction = tuple(value / length for value in vector)
    master_span = _projection_span(master_points, direction)
    slave_span = _projection_span(slave_points, direction)
    if (
        master_span > planarity_tolerance
        or slave_span > planarity_tolerance
    ):
        raise ValueError(
            "target_length requires planar, parallel master/slave SHELL regions "
            "normal to a straight sweep; use explicit steps for other geometry"
        )
    steps = max(
        1,
        math.ceil(length / params["target_length"]),
    )
    if steps > _MAX_STEPS:
        raise ValueError(
            f"target_length implies {steps} steps, exceeding the {_MAX_STEPS}-step limit"
        )
    return {
        "source": "target_length",
        "steps": steps,
        "target_length": params["target_length"],
        "sweep_length": length,
        "planarity": {
            "local_diameter": local_scale,
            "tolerance": planarity_tolerance,
            "master_span": master_span,
            "slave_span": slave_span,
            "mode": "planar_parallel_straight_sweep",
        },
        "master_center": list(master_center),
        "slave_center": list(slave_center),
        "direction": list(direction),
    }


def _shape_derivatives(xi, eta, zeta):
    derivatives = []
    for node_xi, node_eta, node_zeta in _HEXA_NATURAL:
        derivatives.append((
            0.125 * node_xi * (1.0 + eta * node_eta) * (1.0 + zeta * node_zeta),
            0.125 * node_eta * (1.0 + xi * node_xi) * (1.0 + zeta * node_zeta),
            0.125 * node_zeta * (1.0 + xi * node_xi) * (1.0 + eta * node_eta),
        ))
    return derivatives


def _jacobian_determinant(points, xi, eta, zeta):
    derivatives = _shape_derivatives(xi, eta, zeta)
    columns = []
    for derivative in range(3):
        columns.append(tuple(
            sum(
                derivatives[node][derivative] * points[node][axis]
                for node in range(8)
            )
            for axis in range(3)
        ))
    first, second, third = columns
    return (
        first[0] * (second[1] * third[2] - second[2] * third[1])
        - first[1] * (second[0] * third[2] - second[2] * third[0])
        + first[2] * (second[0] * third[1] - second[1] * third[0])
    )


def _hexa_integration(points):
    if len(points) != 8:
        raise ValueError("HEXA8 requires exactly eight node coordinates")
    edge_lengths = [
        math.sqrt(sum(
            (points[second][axis] - points[first][axis]) ** 2
            for axis in range(3)
        ))
        for first, second in _HEXA_EDGES
    ]
    local_scale = max(edge_lengths)
    if not math.isfinite(local_scale) or local_scale <= 0:
        raise ValueError("HEXA8 has no finite local edge length")
    jacobian_tolerance = local_scale ** 3 * 1e-10

    center_jacobian = _jacobian_determinant(points, 0.0, 0.0, 0.0)
    if (
        not math.isfinite(center_jacobian)
        or center_jacobian <= jacobian_tolerance
    ):
        raise ValueError("HEXA8 has a non-positive Jacobian at the center")

    minimum_jacobian = center_jacobian
    volume = 0.0
    for xi in (-_GAUSS, _GAUSS):
        for eta in (-_GAUSS, _GAUSS):
            for zeta in (-_GAUSS, _GAUSS):
                jacobian = _jacobian_determinant(points, xi, eta, zeta)
                if (
                    not math.isfinite(jacobian)
                    or jacobian <= jacobian_tolerance
                ):
                    raise ValueError(
                        "HEXA8 has a non-positive Jacobian at a Gauss point"
                    )
                minimum_jacobian = min(minimum_jacobian, jacobian)
                volume += jacobian
    if not math.isfinite(volume) or volume <= 0:
        raise ValueError("HEXA8 has a non-positive integrated volume")
    return volume, minimum_jacobian, local_scale


def _is_hexa8_type(value):
    if type(value) is not str:
        return False
    kind = value.strip().upper()
    return kind in {"HEXA", "HEXA8"} or (
        kind.startswith("C3D8") and not kind.startswith("C3D20")
    )


def _strict_method(value):
    return "".join(char for char in value.casefold() if char.isalnum())


def _new_rows(before, after, category):
    before_ids = {row["entity"]["id"] for row in before[category]}
    return [
        row for row in after[category]
        if row["entity"]["id"] not in before_ids
    ]


def _row_map(rows):
    result = {}
    for row in rows:
        entity_id = row["entity"]["id"]
        if entity_id in result:
            raise RuntimeError(f"duplicate entity ID {entity_id} in snapshot")
        result[entity_id] = row
    return result


def _validate_scope(base, deck, before, after, params, resolved, steps_info):
    errors = []
    before_keys = {
        (item["type"], item["id"]) for item in before["entity_references"]
    }
    after_keys = {
        (item["type"], item["id"]) for item in after["entity_references"]
    }
    removed = sorted(before_keys - after_keys)
    if removed:
        errors.append(
            "model entities were removed: "
            + ", ".join(f"{kind} {entity_id}" for kind, entity_id in removed[:20])
        )

    for category in _CATEGORY_TYPES:
        old = _row_map(before[category])
        new = _row_map(after[category])
        for entity_id, row in old.items():
            if entity_id not in new:
                errors.append(f"original {category[:-1]} {entity_id} disappeared")
            elif not catalog._strict_equal(row, new[entity_id]):
                errors.append(f"original {category[:-1]} {entity_id} card changed")

    new_volumes = _new_rows(before, after, "volumes")
    new_solids = _new_rows(before, after, "solids")
    new_nodes = _new_rows(before, after, "nodes")
    new_volume_ids = {row["entity"]["id"] for row in new_volumes}
    new_solid_ids = {row["entity"]["id"] for row in new_solids}
    new_node_ids = {row["entity"]["id"] for row in new_nodes}
    allowed_new = (
        {("VOLUME", entity_id) for entity_id in new_volume_ids}
        | {("SOLID", entity_id) for entity_id in new_solid_ids}
        | {("NODE", entity_id) for entity_id in new_node_ids}
    )
    unexpected_new = sorted(after_keys - before_keys - allowed_new)
    if unexpected_new:
        errors.append(
            "model references changed outside the new VOLUME/SOLID/NODE scope: "
            + ", ".join(f"{kind} {entity_id}" for kind, entity_id in unexpected_new[:20])
        )

    if len(new_volumes) != 1:
        errors.append(f"expected exactly one new VOLUME, found {len(new_volumes)}")
    if not new_solids:
        errors.append("VolumesMap created no SOLID entities")

    property_id = params["property"]["id"]
    coordinates = _node_coordinates(after)
    used_nodes = set()
    solid_types = set()
    volume_sum = 0.0
    volume_scale_sum = 0.0
    for row in new_solids:
        entity_id = row["entity"]["id"]
        fields = row["fields"]
        labels = set(row["card_fields"])
        missing = {
            "type", "EID", "PID", *_SOLID_NODE_FIELDS
        } - labels
        if missing:
            errors.append(
                f"SOLID {entity_id} lacks required field(s): "
                + ", ".join(sorted(missing))
            )
            continue
        kind = fields["type"]
        solid_types.add(kind if type(kind) is str else repr(kind))
        if not _is_hexa8_type(kind):
            errors.append(
                f"SOLID {entity_id} type {kind!r} is not a first-order HEXA8"
            )
        if fields["EID"] != entity_id:
            errors.append(f"SOLID {entity_id} EID does not match its identity")
        if fields["PID"] != property_id:
            errors.append(
                f"SOLID {entity_id} PID {fields['PID']!r} does not match SOLID_SECTION {property_id}"
            )
        nodes = []
        for field in _SOLID_NODE_FIELDS:
            try:
                nodes.append(_positive_int(fields[field], f"SOLID {entity_id} {field}"))
            except Exception as exc:
                errors.append(_error(exc))
        if len(nodes) != 8 or len(set(nodes)) != 8:
            errors.append(f"SOLID {entity_id} does not have eight unique node IDs")
            continue
        for field in _HIGHER_ORDER_FIELDS:
            if field in fields and fields[field] not in (None, "", 0, "0"):
                errors.append(
                    f"SOLID {entity_id} has non-empty higher-order field {field}"
                )
                break
        missing_nodes = [node_id for node_id in nodes if node_id not in coordinates]
        if missing_nodes:
            errors.append(
                f"SOLID {entity_id} references missing NODE(s): "
                + ", ".join(str(value) for value in missing_nodes)
            )
            continue
        used_nodes.update(nodes)
        try:
            cell_volume, _minimum_jacobian, local_scale = _hexa_integration(
                [coordinates[node_id] for node_id in nodes]
            )
        except Exception as exc:
            errors.append(f"SOLID {entity_id}: {_error(exc)}")
            continue
        volume_sum += cell_volume
        volume_scale_sum += local_scale ** 3

    orphan_nodes = sorted(new_node_ids - used_nodes)
    if orphan_nodes:
        errors.append(
            "new NODE entities are unused by new SOLID entities: "
            + ", ".join(str(value) for value in orphan_nodes[:20])
        )
    unused_source_nodes = sorted(
        set(resolved["source_nodes"]) - set(coordinates)
    )
    if unused_source_nodes:
        errors.append(
            "source region NODE entities disappeared: "
            + ", ".join(str(value) for value in unused_source_nodes[:20])
        )

    output = {
        "volume": None,
        "hexa8_count": len(new_solids),
        "new_solid_ids": sorted(new_solid_ids),
        "new_node_ids": sorted(new_node_ids),
        "used_node_ids": sorted(used_nodes),
        "source_region_node_ids": resolved["source_nodes"],
        "solid_types": sorted(solid_types),
        "volume_sum": volume_sum,
        "property_id": property_id,
        "material_id": resolved["material"][1]["id"],
    }
    classified_types = {
        entity_type for entity_type, _expected in _CATEGORY_TYPES.values()
    }
    unclassified = [
        reference for reference in after["entity_references"]
        if reference["type"] not in classified_types
    ]
    output["scope_coverage"] = {
        "card_verified_types": sorted(classified_types - {"ANSAPART"}),
        "metadata_verified_types": (
            ["ANSAPART"] if resolved["part"] is not None else []
        ),
        "unclassified_entities_identity_only": unclassified[:100],
        "unclassified_count": len(unclassified),
        "unclassified_truncated": len(unclassified) > 100,
        "claim": (
            "identity and references are checked for every entity; card equality "
            "is checked only for card_verified_types; ANSAPART is metadata-only; "
            "unclassified entities are identity-only"
        ),
    }
    part_metadata = None
    if resolved["part"] is not None:
        part_reference = resolved["part"][1]
        part_metadata = _row_map(after["parts"]).get(part_reference["id"])
        if part_metadata is None:
            errors.append(
                f"ANSAPART {part_reference['id']} metadata disappeared after meshing"
            )
    output["part_metadata"] = part_metadata

    if len(new_volumes) == 1:
        volume_row = new_volumes[0]
        volume_id = volume_row["entity"]["id"]
        fields = volume_row["fields"]
        # ANSA 2026.1's GetVolumesMapParameters returned "Thin parts" for a
        # volume whose own card reported "Normal Parts".  The volume card is
        # the authoritative readback here; the helper is intentionally not
        # used to accept or reject this operation.
        output["volume"] = {
            "entity": volume_row["entity"],
            "card_fields": volume_row["card_fields"],
            "fields": fields,
        }
        if fields.get("ID") != volume_id:
            errors.append("new VOLUME card ID does not match its identity")
        if fields.get("Type") != "Map":
            errors.append(f"new VOLUME Type is {fields.get('Type')!r}, expected 'Map'")
        if fields.get("Status") != "Meshed":
            errors.append(
                f"new VOLUME Status is {fields.get('Status')!r}, expected 'Meshed'"
            )
        if fields.get("PID") != property_id:
            errors.append(
                f"new VOLUME PID {fields.get('PID')!r} does not match SOLID_SECTION {property_id}"
            )
        method = fields.get("Method")
        if type(method) is not str or _strict_method(method) != _strict_method(params["method"]):
            errors.append(
                f"new VOLUME Method is {method!r}, expected {params['method']!r}"
            )
        if fields.get("Map steps") != steps_info["steps"]:
            errors.append(
                f"new VOLUME Map steps is {fields.get('Map steps')!r}, expected {steps_info['steps']}"
            )
        if fields.get("Order") not in (None, "1st"):
            errors.append(f"new VOLUME Order is {fields.get('Order')!r}, expected '1st'")
        light = fields.get("Light volume representation")
        if light is not None:
            expected_light = (
                "yes" if params["light_volume_representation"] else "no"
            )
            if type(light) is not str or light.strip().casefold() != expected_light:
                errors.append(
                    "new VOLUME Light volume representation does not match the request"
                )
        if fields.get("Hexas") != len(new_solids):
            errors.append(
                f"new VOLUME Hexas is {fields.get('Hexas')!r}, expected {len(new_solids)}"
            )
        for name in ("Tetras", "Pentas", "Pyramids", "Polyhedrals"):
            if fields.get(name) not in (None, 0):
                errors.append(
                    f"new VOLUME {name} is {fields.get(name)!r}, expected 0"
                )
        if fields.get("Grids") != len(used_nodes):
            errors.append(
                f"new VOLUME Grids is {fields.get('Grids')!r}, expected {len(used_nodes)}"
            )
        internal = fields.get("Internal grids")
        if type(internal) is not int or internal < 0 or internal > len(used_nodes):
            errors.append(
                f"new VOLUME Internal grids is {internal!r}, outside 0..{len(used_nodes)}"
            )
        try:
            reported_volume = _finite_number(
                fields.get("Volume"), "new VOLUME Volume"
            )
        except Exception as exc:
            errors.append(_error(exc))
            reported_volume = None
        if reported_volume is not None:
            if reported_volume <= 0:
                errors.append("new VOLUME Volume must be positive")
            tolerance = max(
                reported_volume * _VOLUME_REL_TOL,
                volume_scale_sum * 1e-9,
            )
            if abs(volume_sum - reported_volume) > tolerance:
                errors.append(
                    f"new HEXA8 volume sum {volume_sum!r} does not match VOLUME Volume {reported_volume!r}"
                )

        volume_entity, _reference = catalog._get_entity(
            base, deck, "VOLUME", volume_id
        )
        try:
            volume_shells = base.CollectEntities(deck, volume_entity, "SHELL")
        except Exception as exc:
            errors.append("new VOLUME boundary readback: " + _error(exc))
            volume_shells = 0
        if volume_shells is None or (type(volume_shells) is int and volume_shells == 0):
            volume_shell_refs = []
        elif type(volume_shells) not in (list, tuple):
            errors.append("new VOLUME boundary is not a SHELL list")
            volume_shell_refs = []
        else:
            volume_shell_refs = [
                catalog._entity_reference(base, deck, item, "SHELL")
                for item in volume_shells
            ]
        requested_shells = [
            reference
            for name in ("master", "slave", "round")
            for _entity, reference in resolved["regions"][name]
        ]
        if (
            {(item["type"], item["id"]) for item in volume_shell_refs}
            != {(item["type"], item["id"]) for item in requested_shells}
        ):
            errors.append(
                "new VOLUME boundary SHELL membership does not match master/slave/round"
            )
        output["boundary_shells"] = volume_shell_refs

        try:
            volume_solids = base.CollectEntities(deck, volume_entity, "SOLID")
        except Exception as exc:
            errors.append("new VOLUME SOLID readback: " + _error(exc))
            volume_solids = 0
        if volume_solids is None or (type(volume_solids) is int and volume_solids == 0):
            volume_solid_refs = []
        elif type(volume_solids) not in (list, tuple):
            errors.append("new VOLUME SOLID membership is not a SOLID list")
            volume_solid_refs = []
        else:
            volume_solid_refs = [
                catalog._entity_reference(base, deck, item, "SOLID")
                for item in volume_solids
            ]
        if {item["id"] for item in volume_solid_refs} != new_solid_ids:
            errors.append(
                "new VOLUME SOLID membership does not match the new SOLID entities"
            )
        output["volume_solids"] = volume_solid_refs

        if resolved["part"] is not None:
            part_entity, part_reference = resolved["part"]
            try:
                part_volumes = base.CollectEntities(deck, part_entity, "VOLUME")
            except Exception as exc:
                output["part_membership_error"] = _error(exc)
                errors.append(
                    f"ANSAPART {part_reference['id']} VOLUME membership readback failed: "
                    + _error(exc)
                )
            else:
                if type(part_volumes) in (list, tuple):
                    try:
                        part_volume_refs = [
                            catalog._entity_reference(base, deck, item, "VOLUME")
                            for item in part_volumes
                        ]
                    except Exception as exc:
                        output["part_membership_error"] = _error(exc)
                        errors.append(
                            f"ANSAPART {part_reference['id']} VOLUME membership "
                            "identity readback failed: " + _error(exc)
                        )
                    else:
                        output["part_volume_membership"] = part_volume_refs
                        contains_volume = (
                            volume_id in {item["id"] for item in part_volume_refs}
                        )
                        output["part_volume_membership_contains_volume"] = contains_volume
                        if not contains_volume:
                            errors.append(
                                f"new VOLUME {volume_id} is absent from ANSAPART "
                                f"{part_reference['id']} VOLUME membership"
                            )
                elif type(part_volumes) is int and part_volumes == 0:
                    errors.append(
                        f"ANSAPART {part_reference['id']} returned no VOLUME membership"
                    )
                else:
                    output["part_membership_error"] = (
                        "ANSAPART VOLUME membership is not a list"
                    )
                    errors.append(
                        f"ANSAPART {part_reference['id']} VOLUME membership is not a list"
                    )
    return errors, output


def _returned_entities(base, deck, status):
    if isinstance(status, (list, tuple)):
        values = status
    elif catalog._is_entity(status, base):
        values = [status]
    else:
        return None
    references = []
    for entity in values:
        references.append(catalog._entity_reference(base, deck, entity))
    return references


def _validate_api_return(base, deck, status, new_volume):
    info = {
        "name": "ansa.mesh.VolumesMap",
        "returned_entities": None,
        "return_code": None,
        "return_type": type(status).__name__ if status is not None else None,
    }
    errors = []
    accepted = False
    try:
        returned = _returned_entities(base, deck, status)
    except Exception as exc:
        returned = None
        errors.append("could not read VolumesMap return entities: " + _error(exc))
    if returned is not None:
        info["returned_entities"] = returned
        if len(returned) == 1 and returned[0]["type"] == "VOLUME":
            accepted = True
        else:
            errors.append(
                "VolumesMap with ret_ents=True must return exactly one VOLUME entity"
            )
        if new_volume is None:
            errors.append("VolumesMap returned a VOLUME but no new VOLUME was read back")
        elif len(returned) != 1 or returned[0] != new_volume:
            errors.append(
                "VolumesMap returned VOLUME identity does not match the new VOLUME readback"
            )
    elif type(status) is int:
        info["return_code"] = status
        accepted = status == 1
        if not accepted:
            errors.append(
                f"VolumesMap returned {status!r}; exact success value is int 1"
            )
    else:
        errors.append("VolumesMap returned no entity list or integer status")
    return errors, info, accepted


def _public_request(params, steps_info):
    return {
        "master": params["master"],
        "slave": params["slave"],
        "round": params["round"],
        "method": params["method"],
        "steps": steps_info["steps"],
        "steps_source": steps_info["source"],
        "target_length": params["target_length"],
        "sweep_length": steps_info.get("sweep_length"),
        "target_length_planarity": steps_info.get("planarity"),
        "part": params["part"],
        "property": params["property"],
        "light_volume_representation": params["light_volume_representation"],
        "backup_path": params["backup_path"],
    }


def _has_effects(before, after):
    if before is None or after is None:
        return after is not None
    return {
        (item["type"], item["id"]) for item in before["entity_references"]
    } != {
        (item["type"], item["id"]) for item in after["entity_references"]
    }


def _failed_result(params, steps_info, deck, backup, before, after, errors, *,
                   possible_effects, model_scope_matches=None, api=None,
                   output=None):
    result = {
        "ok": False,
        "tool": _TOOL,
        "status": "partial" if _has_effects(before, after) else "failed",
        "deck": deck,
        "requested": _public_request(params, steps_info),
        "backup": backup,
        "before": before,
        "after": after,
        "api": api,
        "map_readback": output.get("volume") if output else None,
        "mesh_readback": output,
        "scope_card_coverage": output.get("scope_coverage") if output else None,
        "scope_verified": False,
        "model_scope_matches": model_scope_matches,
        "possible_effects": possible_effects,
        "error": "; ".join(errors) if errors else "Map volume meshing was not verified",
        "errors": errors,
    }
    return element_types._bounded_result(result)


def _success_result(params, steps_info, deck, backup, before, after, output, api):
    result = element_types._bounded_result({
        "ok": True,
        "tool": _TOOL,
        "status": "succeeded",
        "deck": deck,
        "requested": _public_request(params, steps_info),
        "backup": backup,
        "before": before,
        "after": after,
        "api": api,
        "map_readback": output.get("volume"),
        "mesh_readback": output,
        "scope_card_coverage": output.get("scope_coverage"),
        "scope_verified": True,
        "model_scope_matches": True,
        "possible_effects": False,
        "error": None,
        "errors": [],
    })
    if result.get("ok") is not True:
        result["status"] = "partial"
    return result


def execute(state, tool, params):
    """Mesh one ABAQUS Map volume from explicit SHELL boundary regions."""
    state.assert_main()
    if type(tool) is not str or tool != _TOOL:
        raise ValueError(f"unsupported hexa meshing operation: {tool!r}")
    params = _validate_inputs(params)
    from ansa import base, constants, mesh

    deck = catalog._deck(base, params)
    abaqus = getattr(constants, "ABAQUS", None)
    if type(abaqus) is not int or deck != abaqus:
        raise ValueError("mesh_hex_map supports only the ABAQUS deck")
    if not callable(getattr(mesh, "VolumesMap", None)):
        raise NotImplementedError("ANSA mesh.VolumesMap is unavailable")
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    context = volume_meshing._execution_context(state, deck)

    backup_path, _prior = element_types._save_path(params["backup_path"], False)
    current_path = element_types._database_name(base)
    if current_path and model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_fingerprint = (
        model_files._fingerprint(Path(current_path)) if current_path else None
    )

    before = _capture_snapshot(base, deck)
    resolved = _resolve_conditions(base, deck, params, before)
    region_keys = {
        (reference["type"], reference["id"])
        for name in ("master", "slave", "round")
        for _entity, reference in resolved["regions"][name]
    }
    _reject_existing_boundary(base, deck, before, region_keys)
    steps_info = _derive_steps(base, deck, params, resolved, before)
    if len(_encoded(before)) > _SNAPSHOT_BUDGET:
        return _failed_result(
            params,
            steps_info,
            deck,
            None,
            before,
            None,
            [
                "preflight snapshot exceeds the 256 KiB evidence budget; "
                "no backup or VolumesMap call was made"
            ],
            possible_effects=False,
        )
    volume_meshing._same_context(state, deck, context)

    backup = None
    try:
        backup = element_types._verify_backup(
            base,
            constants,
            backup_path,
            current_path,
            current_fingerprint,
        )
    except Exception as exc:
        backup = element_types._unconfirmed_backup(backup_path)
        try:
            after_save = _capture_snapshot(base, deck)
        except Exception as read_exc:
            after_save = None
            read_error = _error(read_exc)
        else:
            read_error = None
        possible = after_save is None or _has_effects(before, after_save)
        try:
            current_after = element_types._database_name(base)
            possible = possible or not element_types._same_database_name(
                current_path, current_after
            )
            if current_path:
                possible = possible or not catalog._strict_equal(
                    current_fingerprint,
                    model_files._fingerprint(Path(current_path)),
                )
            volume_meshing._same_context(state, deck, context)
        except Exception:
            possible = True
        errors = [_error(exc)]
        if read_error:
            errors.append("post-save readback: " + read_error)
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            after_save,
            errors,
            possible_effects=possible,
        )

    try:
        volume_meshing._same_context(state, deck, context)
        after_backup = _capture_snapshot(base, deck)
        _resolve_conditions(base, deck, params, after_backup)
    except Exception as exc:
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            locals().get("after_backup"),
            [_error(exc)],
            possible_effects=True,
        )
    if len(_encoded(after_backup)) > _SNAPSHOT_BUDGET:
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            after_backup,
            [
                "post-backup snapshot exceeds the 256 KiB evidence budget; "
                "VolumesMap was not called"
            ],
            possible_effects=_has_effects(before, after_backup),
            model_scope_matches=not _has_effects(before, after_backup),
        )
    if not catalog._strict_equal(before, after_backup):
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            after_backup,
            ["model snapshot changed after backup and before VolumesMap"],
            possible_effects=True,
            model_scope_matches=False,
        )
    try:
        volume_meshing._same_context(state, deck, context)
    except Exception as exc:
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            after_backup,
            [_error(exc)],
            possible_effects=True,
            model_scope_matches=False,
        )

    master_entities = [
        entity for entity, _reference in resolved["regions"]["master"]
    ]
    slave_entities = [
        entity for entity, _reference in resolved["regions"]["slave"]
    ]
    round_entities = [
        entity for entity, _reference in resolved["regions"]["round"]
    ]
    part_entity = resolved["part"][0] if resolved["part"] is not None else None
    property_entity = resolved["property"][0]

    call_options = {
        "method": params["method"],
        "steps": steps_info["steps"],
        "prop": property_entity,
        "ret_ents": True,
        "light_volume_representation": params["light_volume_representation"],
    }
    if part_entity is not None:
        call_options["part"] = part_entity

    api_status = None
    api_error = None
    try:
        api_status = mesh.VolumesMap(
            master_entities,
            slave_entities,
            round_entities,
            **call_options,
        )
    except Exception as exc:
        api_error = _error(exc)

    try:
        after = _capture_snapshot(base, deck)
    except Exception as exc:
        after = None
        readback_error = _error(exc)
    else:
        readback_error = None
    try:
        volume_meshing._same_context(state, deck, context)
        context_error = None
    except Exception as exc:
        context_error = _error(exc)

    output = None
    scope_errors = []
    if after is None:
        scope_errors.append("post-mesh full model readback is unknown")
    else:
        try:
            scope_errors, output = _validate_scope(
                base, deck, before, after, params, resolved, steps_info
            )
        except Exception as exc:
            scope_errors.append("post-mesh scope validation: " + _error(exc))
    if readback_error:
        scope_errors.append("post-mesh readback: " + readback_error)
    if context_error:
        scope_errors.append("post-mesh context: " + context_error)
    if after is not None and len(_encoded(after)) > _SNAPSHOT_BUDGET:
        scope_errors.append("post-mesh snapshot exceeds the 256 KiB evidence budget")

    new_volume = None
    if output is not None and output.get("volume") is not None:
        new_volume = output["volume"]["entity"]
    api_errors, api_info, api_accepted = _validate_api_return(
        base, deck, api_status, new_volume
    )
    if api_error:
        api_errors.insert(0, api_error)
    scope_errors.extend(api_errors)
    model_scope_matches = not scope_errors
    if not api_accepted or not model_scope_matches:
        return _failed_result(
            params,
            steps_info,
            deck,
            backup,
            before,
            after,
            scope_errors,
            possible_effects=True,
            model_scope_matches=model_scope_matches,
            api=api_info,
            output=output,
        )

    return _success_result(
        params,
        steps_info,
        deck,
        backup,
        before,
        after,
        output,
        api_info,
    )


__all__ = ["execute"]
