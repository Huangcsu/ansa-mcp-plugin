"""Narrow, explicitly conditioned ABAQUS mesh operations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import (
    element_types,
    entity_catalog as catalog,
    hexa_meshing,
    model_files,
    volume_meshing,
)


_TOOL = "mesh_workflow"
_MAX_ITEMS = 200
_MAX_STEPS = 1000
_MAX_NEW_SOLIDS = 10000
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_SNAPSHOT_BUDGET = MAX_FRAME_BYTES // 4
_INTERNAL_ELEMENT_BUDGET = 8 * 1024 * 1024
_NODE_FIELD = re.compile(r"^(?:G|N)\d+$", re.IGNORECASE)
_SHELL_FIELDS = tuple(f"G{index}" for index in range(1, 5))
_SOLID_FIELDS = tuple(f"G{index}" for index in range(1, 9))
_HIGH_ORDER_SHELL_FIELDS = tuple(f"G{index}" for index in range(5, 10))
_HIGH_ORDER_SOLID_FIELDS = tuple(f"G{index}" for index in range(9, 29))
_HEXA_FACES = (
    (0, 1, 2, 3), (4, 5, 6, 7),
    (0, 1, 5, 4), (1, 2, 6, 5),
    (2, 3, 7, 6), (3, 0, 4, 7),
)


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _condition(value, label, entity_types, required_fields):
    catalog._object(value, ("type", "id", "expected_fields"), label=label)
    entity_type = value["type"]
    if entity_type not in entity_types:
        raise ValueError(f"{label}.type must be one of {sorted(entity_types)}")
    entity_id = _positive_int(value["id"], f"{label}.id")
    fields = value["expected_fields"]
    if type(fields) is not dict or not fields:
        raise ValueError(f"{label}.expected_fields must be a non-empty object")
    missing = sorted(set(required_fields) - set(fields))
    if missing:
        raise ValueError(
            f"{label}.expected_fields must include: " + ", ".join(missing)
        )
    return {"type": entity_type, "id": entity_id, "expected_fields": fields}


def _conditions(values, label, entity_types, required_fields, *, maximum=_MAX_ITEMS):
    if type(values) is not list or not 1 <= len(values) <= maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} conditions")
    result = [
        _condition(item, f"{label}[{index}]", entity_types, required_fields)
        for index, item in enumerate(values)
    ]
    keys = [(item["type"], item["id"]) for item in result]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must not contain duplicate entity IDs")
    return result


def _part_condition(value, deck, label="part"):
    catalog._object(value, ("deck", "type", "id", "expected_fields"), label=label)
    item = _condition(
        {key: value[key] for key in ("type", "id", "expected_fields")},
        label,
        {"ANSAPART"},
        ("Name",),
    )
    if type(value.get("deck")) is not int or value["deck"] != deck:
        raise ValueError(f"{label}.deck must match the ABAQUS deck")
    return item


def _property_condition(value, deck):
    catalog._object(value, ("deck", "type", "id", "expected_fields"), label="property")
    item = _condition(
        {key: value[key] for key in ("type", "id", "expected_fields")},
        "property",
        {"SOLID_SECTION"},
        ("MID",),
    )
    if type(value.get("deck")) is not int or value["deck"] != deck:
        raise ValueError("property.deck must match the ABAQUS deck")
    return item


def _point(value, label):
    if type(value) not in (list, tuple) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three coordinates")
    return tuple(_number(number, f"{label}[{index}]") for index, number in enumerate(value))


def _normalise_inputs(params):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    action = params.get("action")
    if action not in {"extrude_quad", "mirror_copy", "merge_nodes", "release_mesh"}:
        raise ValueError("action must be extrude_quad, mirror_copy, merge_nodes, or release_mesh")
    common = {"action", "deck", "backup_path"}
    fields_by_action = {
        "extrude_quad": {"shells", "vector", "steps", "part", "property"},
        "mirror_copy": {"sources", "plane", "part"},
        "merge_nodes": {"pairs"},
        "release_mesh": {"entities"},
    }
    required = common | fields_by_action[action]
    catalog._object(params, required, label="params")
    deck = _positive_int(params["deck"], "deck")
    backup_path = params["backup_path"]
    if type(backup_path) is not str or not backup_path:
        raise ValueError("backup_path must be a non-empty absolute .ansa path")
    path = Path(backup_path)
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")

    result = {"action": action, "deck": deck, "backup_path": backup_path}
    if action == "extrude_quad":
        result["shells"] = _conditions(
            params["shells"],
            "shells",
            {"SHELL"},
            ("type", "PID", *_SHELL_FIELDS),
        )
        result["vector"] = _point(params["vector"], "vector")
        magnitude = math.sqrt(sum(value * value for value in result["vector"]))
        if not math.isfinite(magnitude) or magnitude <= 0:
            raise ValueError("vector must have a positive finite length")
        steps = params["steps"]
        if type(steps) is not int or not 1 <= steps <= _MAX_STEPS:
            raise ValueError(f"steps must be an integer from 1 to {_MAX_STEPS}")
        if len(result["shells"]) * steps > _MAX_NEW_SOLIDS:
            raise ValueError("requested extrusion exceeds the new HEXA8 limit")
        result["steps"] = steps
        result["part"] = _part_condition(params["part"], deck)
        result["property"] = _property_condition(params["property"], deck)
    elif action == "mirror_copy":
        if type(params["sources"]) is not list or not 1 <= len(params["sources"]) <= _MAX_ITEMS:
            raise ValueError(f"sources must contain 1 to {_MAX_ITEMS} conditions")
        sources = []
        seen = set()
        for index, value in enumerate(params["sources"]):
            entity_type = value.get("type") if type(value) is dict else None
            required_fields = (
                ("type", "PID", *_SHELL_FIELDS)
                if entity_type == "SHELL"
                else ("type", "EID", "PID", *_SOLID_FIELDS)
            )
            item = _condition(
                value, f"sources[{index}]", {"SHELL", "SOLID"}, required_fields
            )
            key = (item["type"], item["id"])
            if key in seen:
                raise ValueError("sources must not contain duplicate entity IDs")
            seen.add(key)
            sources.append(item)
        plane = params["plane"]
        catalog._object(plane, ("point1", "point2", "point3"), label="plane")
        result["sources"] = sources
        result["plane"] = {
            name: _point(plane[name], f"plane.{name}")
            for name in ("point1", "point2", "point3")
        }
        result["part"] = _part_condition(params["part"], deck)
    elif action == "merge_nodes":
        pairs = params["pairs"]
        if type(pairs) is not list or not 1 <= len(pairs) <= _MAX_ITEMS:
            raise ValueError(f"pairs must contain 1 to {_MAX_ITEMS} node pairs")
        normalised_pairs = []
        sources, targets = set(), set()
        for index, pair in enumerate(pairs):
            label = f"pairs[{index}]"
            catalog._object(pair, ("source", "target"), label=label)
            source = _condition(pair["source"], label + ".source", {"NODE"}, ("X", "Y", "Z"))
            target = _condition(pair["target"], label + ".target", {"NODE"}, ("X", "Y", "Z"))
            if source["id"] == target["id"]:
                raise ValueError(f"{label} source and target IDs must differ")
            if source["id"] in sources or target["id"] in targets:
                raise ValueError("merge source and target IDs must each be unique")
            sources.add(source["id"])
            targets.add(target["id"])
            normalised_pairs.append({"source": source, "target": target})
        if sources & targets:
            raise ValueError("merge source and target ID sets must be disjoint")
        result["pairs"] = normalised_pairs
    else:
        values = params["entities"]
        if type(values) is not list or not 1 <= len(values) <= _MAX_ITEMS:
            raise ValueError(f"entities must contain 1 to {_MAX_ITEMS} conditions")
        entities = []
        seen = set()
        for index, value in enumerate(values):
            entity_type = value.get("type") if type(value) is dict else None
            required_fields = (
                ("type", "PID")
                if entity_type == "SHELL"
                else ("type", "EID", "PID")
            )
            item = _condition(
                value, f"entities[{index}]", {"SHELL", "SOLID"}, required_fields
            )
            node_field_count = sum(
                1 for name in item["expected_fields"] if _NODE_FIELD.fullmatch(name)
            )
            minimum_nodes = 3 if entity_type == "SHELL" else 4
            if node_field_count < minimum_nodes:
                raise ValueError(
                    f"entities[{index}].expected_fields must include at least {minimum_nodes} node card fields"
                )
            key = (item["type"], item["id"])
            if key in seen:
                raise ValueError("entities must not contain duplicate entity IDs")
            seen.add(key)
            entities.append(item)
        result["entities"] = entities

    try:
        size = len(_encoded(result))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mesh workflow request is not strict JSON: {exc}") from exc
    if size > _REQUEST_BUDGET:
        raise ValueError("mesh workflow request exceeds the 128 KiB budget")
    return result


def _row_map(rows):
    return {row["entity"]["id"]: row for row in rows}


def _reference_keys(snapshot):
    return {(item["type"], item["id"]) for item in snapshot["entity_references"]}


def _field_id(value, label, expected_type=None):
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isdecimal() and int(value) > 0:
        return int(value)
    if type(value) is dict and set(value) == {"deck", "type", "id"}:
        if expected_type is not None and value["type"] != expected_type:
            raise ValueError(f"{label} does not reference {expected_type}")
        return _positive_int(value["id"], label)
    raise ValueError(f"{label} is not a positive entity ID")


def _empty(value):
    return value is None or value == "" or value == 0 or value == "0"


def _element_nodes(row, fields, label):
    values = row["fields"]
    nodes = []
    for field in fields:
        if field not in values:
            raise ValueError(f"{label} lacks card field {field}")
        nodes.append(_field_id(values[field], f"{label} {field}", "NODE"))
    if len(nodes) != len(set(nodes)):
        raise ValueError(f"{label} connectivity contains repeated NODE IDs")
    return nodes


def _check_high_order(row, fields, label):
    for field in fields:
        if field in row["fields"] and not _empty(row["fields"][field]):
            raise ValueError(f"{label} has unsupported higher-order node {field}")


def _resolve_condition(base, deck, snapshot, item, category):
    entity, reference, _expected, _labels = catalog._validate_entity_values(
        base, deck, item["type"], item["id"], item["expected_fields"]
    )
    row = _row_map(snapshot[category]).get(item["id"])
    if row is None:
        raise RuntimeError(f"{item['type']} {item['id']} is absent from the complete snapshot")
    return entity, reference, row


def _resolve_part(base, deck, snapshot, item):
    entity, reference, row = _resolve_condition(base, deck, snapshot, item, "parts")
    return entity, reference, row


def _resolve_property(base, deck, snapshot, item):
    entity, reference, row = _resolve_condition(base, deck, snapshot, item, "sections")
    mid = _field_id(row["fields"]["MID"], f"SOLID_SECTION {reference['id']} MID", "MATERIAL")
    material, material_ref = catalog._get_entity(base, deck, "MATERIAL", mid)
    if mid not in _row_map(snapshot["materials"]):
        raise RuntimeError(f"SOLID_SECTION {reference['id']} references missing MATERIAL {mid}")
    return entity, reference, row, material, material_ref


def _node_coordinates(snapshot):
    return hexa_meshing._node_coordinates(snapshot)


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _subtract(left, right):
    return tuple(left[index] - right[index] for index in range(3))


def _dot(left, right):
    return sum(left[index] * right[index] for index in range(3))


def _unit(vector, label):
    length = math.sqrt(_dot(vector, vector))
    if not math.isfinite(length) or length <= 1e-14:
        raise ValueError(f"{label} is degenerate")
    return tuple(value / length for value in vector)


def _plane_normal(plane):
    first = _subtract(plane["point2"], plane["point1"])
    second = _subtract(plane["point3"], plane["point1"])
    return _unit(_cross(first, second), "mirror plane points")


def _reflect(point, origin, normal):
    distance = _dot(_subtract(point, origin), normal)
    return tuple(point[index] - 2.0 * distance * normal[index] for index in range(3))


def _coordinate_tolerance(points, scale):
    ulp = max((math.ulp(value) for point in points for value in point), default=0.0)
    return max(float(scale) * 1e-9, ulp * 64.0, 1e-12)


def _same_point(left, right, tolerance):
    return all(abs(left[index] - right[index]) <= tolerance for index in range(3))


def _resolve_action(base, deck, snapshot, params):
    action = params["action"]
    element_cards = snapshot.get("element_cards") if "model" in snapshot else None
    if "model" in snapshot:
        snapshot = snapshot["model"]
    coordinates = _node_coordinates(snapshot)
    if action == "extrude_quad":
        part = _resolve_part(base, deck, snapshot, params["part"])
        prop = _resolve_property(base, deck, snapshot, params["property"])
        shells = []
        footprints = set()
        used_nodes = set()
        vector = params["vector"]
        vector_length = math.sqrt(_dot(vector, vector))
        delta = tuple(value / params["steps"] for value in vector)
        expected_cells = {}
        for item in params["shells"]:
            entity, reference, row = _resolve_condition(base, deck, snapshot, item, "shells")
            kind = row["fields"].get("type")
            if type(kind) is not str or kind.strip().casefold() != "quad":
                raise ValueError(f"SHELL {reference['id']} must be a linear QUAD")
            _check_high_order(row, _HIGH_ORDER_SHELL_FIELDS, f"SHELL {reference['id']}")
            node_ids = _element_nodes(row, _SHELL_FIELDS, f"SHELL {reference['id']}")
            missing = sorted(set(node_ids) - coordinates.keys())
            if missing:
                raise RuntimeError(f"SHELL {reference['id']} references missing NODEs {missing}")
            footprint = frozenset(node_ids)
            if footprint in footprints:
                raise ValueError("extrusion source SHELLs must have unique QUAD connectivity")
            footprints.add(footprint)
            used_nodes.update(node_ids)
            points = [coordinates[node_id] for node_id in node_ids]
            for layer in range(params["steps"]):
                lower = [tuple(point[i] + delta[i] * layer for i in range(3)) for point in points]
                upper = [tuple(point[i] + delta[i] * (layer + 1) for i in range(3)) for point in points]
                volume, _jacobian, _local_scale = hexa_meshing._hexa_integration(lower + upper)
                expected_cells[(reference["id"], layer)] = {"volume": volume}
            shells.append({"entity": entity, "reference": reference, "row": row, "node_ids": node_ids})

        shell_ids = {item["id"] for item in _part_members(base, deck, part[0], "SHELL")}
        if any(item["reference"]["id"] not in shell_ids for item in shells):
            raise ValueError("all extrusion source SHELLs must belong to the explicit ANSAPART")

        if len(expected_cells) > _MAX_NEW_SOLIDS:
            raise ValueError("requested extrusion exceeds the new HEXA8 limit")
        source_coords = [coordinates[node_id] for node_id in sorted(used_nodes)]
        scale = max(vector_length, 1.0)
        tolerance = _coordinate_tolerance(source_coords, scale)
        if any(
            _same_point(left, right, tolerance)
            for index, left in enumerate(source_coords)
            for right in source_coords[index + 1 :]
        ):
            raise ValueError("extrusion source NODE coordinates must be unique")
        for node_id in used_nodes:
            for layer in range(1, params["steps"] + 1):
                expected = tuple(coordinates[node_id][i] + delta[i] * layer for i in range(3))
                if any(_same_point(expected, point, tolerance) for point in coordinates.values()):
                    raise ValueError("extrusion would reuse an existing NODE coordinate")
        return {
            "part": part,
            "property": prop,
            "shells": shells,
            "source_node_ids": sorted(used_nodes),
            "coordinates": coordinates,
            "delta": delta,
            "vector_length": vector_length,
            "expected_cells": expected_cells,
            "tolerance": tolerance,
        }

    if action == "mirror_copy":
        part = _resolve_part(base, deck, snapshot, params["part"])
        normal = _plane_normal(params["plane"])
        sources = []
        source_nodes = set()
        source_coordinate_keys = []
        for item in params["sources"]:
            category = "shells" if item["type"] == "SHELL" else "solids"
            entity, reference, row = _resolve_condition(base, deck, snapshot, item, category)
            if item["type"] == "SHELL":
                kind = row["fields"].get("type")
                if type(kind) is not str or kind.strip().upper() not in {"QUAD", "TRIA"}:
                    raise ValueError(f"SHELL {reference['id']} must be a linear QUAD or TRIA")
                node_fields = _SHELL_FIELDS if kind.strip().upper() == "QUAD" else _SHELL_FIELDS[:3]
                node_ids = _element_nodes(row, node_fields, f"SHELL {reference['id']}")
                _check_high_order(row, _HIGH_ORDER_SHELL_FIELDS, f"SHELL {reference['id']}")
                if kind.strip().upper() == "TRIA" and not _empty(row["fields"].get("G4")):
                    raise ValueError(f"SHELL {reference['id']} TRIA G4 must be empty")
            else:
                kind = row["fields"].get("type")
                if not hexa_meshing._is_hexa8_type(kind):
                    raise ValueError(f"SOLID {reference['id']} must be a first-order HEXA8")
                node_ids = _element_nodes(row, _SOLID_FIELDS, f"SOLID {reference['id']}")
                _check_high_order(row, _HIGH_ORDER_SOLID_FIELDS, f"SOLID {reference['id']}")
                if row["fields"].get("EID") != reference["id"]:
                    raise ValueError(f"SOLID {reference['id']} EID does not match its identity")
            if set(node_ids) - coordinates.keys():
                raise RuntimeError(f"{item['type']} {reference['id']} references a missing NODE")
            if item["type"] == "SOLID":
                hexa_meshing._hexa_integration([coordinates[node_id] for node_id in node_ids])
            source_nodes.update(node_ids)
            sources.append({"entity": entity, "reference": reference, "row": row, "node_ids": node_ids, "kind": kind})

        if any(len(source["node_ids"]) != len(set(source["node_ids"])) for source in sources):
            raise ValueError("mirror sources must use distinct node IDs")
        _reject_volume_membership(base, deck, snapshot, sources, action="mirror_copy")
        for node_id in source_nodes:
            point = coordinates[node_id]
            if abs(_dot(_subtract(point, params["plane"]["point1"]), normal)) <= 1e-12:
                raise ValueError("mirror source nodes on the mirror plane are outside this copy scope")
            source_coordinate_keys.append(point)
        if any(
            _same_point(left, right, _coordinate_tolerance([left, right], 1.0))
            for index, left in enumerate(source_coordinate_keys)
            for right in source_coordinate_keys[index + 1 :]
        ):
            raise ValueError("mirror source NODE coordinates must be unique")
        reflected = {
            node_id: _reflect(coordinates[node_id], params["plane"]["point1"], normal)
            for node_id in source_nodes
        }
        tolerance = _coordinate_tolerance(
            [*source_coordinate_keys, *reflected.values()], 1.0
        )
        for point in reflected.values():
            if any(_same_point(point, existing, tolerance) for existing in coordinates.values()):
                raise ValueError("mirror output would coincide with an existing NODE")
        shell_ids = {item["id"] for item in _part_members(base, deck, part[0], "SHELL")}
        solid_ids = {item["id"] for item in _part_members(base, deck, part[0], "SOLID")}
        if any(
            source["reference"]["id"] not in (shell_ids if source["reference"]["type"] == "SHELL" else solid_ids)
            for source in sources
        ):
            raise ValueError("all mirror source entities must belong to the explicit ANSAPART")
        return {
            "part": part,
            "sources": sources,
            "source_node_ids": sorted(source_nodes),
            "coordinates": coordinates,
            "reflected": reflected,
            "normal": normal,
            "tolerance": tolerance,
        }

    if action == "merge_nodes":
        pairs = []
        mapping = {}
        for index, pair in enumerate(params["pairs"]):
            source, source_ref, source_row = _resolve_condition(
                base, deck, snapshot, pair["source"], "nodes"
            )
            target, target_ref, target_row = _resolve_condition(
                base, deck, snapshot, pair["target"], "nodes"
            )
            source_coords = tuple(_number(source_row["fields"][name], f"NODE {source_ref['id']} {name}") for name in ("X", "Y", "Z"))
            target_coords = tuple(_number(target_row["fields"][name], f"NODE {target_ref['id']} {name}") for name in ("X", "Y", "Z"))
            if source_coords != target_coords:
                raise ValueError(f"node pair {index} must have exactly coincident XYZ coordinates")
            mapping[source_ref["id"]] = target_ref["id"]
            pairs.append({"source": source, "target": target, "source_id": source_ref["id"], "target_id": target_ref["id"]})
        cards = element_cards
        if cards is None:
            raise RuntimeError("node merge requires a complete FE connectivity snapshot")
        touched = set()
        for row in cards:
            fields = _node_connections(row)
            before_ids = [node_id for _name, node_id in fields]
            after_ids = [mapping.get(node_id, node_id) for node_id in before_ids]
            if len(after_ids) != len(set(after_ids)):
                raise ValueError(
                    f"node merge would collapse element {row['entity']['type']} {row['entity']['id']}"
                )
            touched.update(set(before_ids) & set(mapping))
        if touched != set(mapping):
            missing = sorted(set(mapping) - touched)
            raise ValueError(f"merge source NODEs have no explicit FE element connections: {missing}")
        return {"pairs": pairs, "mapping": mapping}

    entities = []
    for item in params["entities"]:
        category = "shells" if item["type"] == "SHELL" else "solids"
        entity, reference, row = _resolve_condition(base, deck, snapshot, item, category)
        node_fields = {
            name for name in row["fields"] if _NODE_FIELD.fullmatch(name)
        }
        if not node_fields or not node_fields.issubset(item["expected_fields"]):
            raise ValueError(
                f"{item['type']} {reference['id']} expected_fields must include every node card field"
            )
        nodes = [node_id for _name, node_id in _node_connections(row)]
        if not nodes:
            raise ValueError(f"{item['type']} {reference['id']} has no readable NODE connectivity")
        if set(nodes) - coordinates.keys():
            raise RuntimeError(f"{item['type']} {reference['id']} references a missing NODE")
        if item["type"] == "SOLID" and row["fields"].get("EID") != reference["id"]:
            raise ValueError(f"SOLID {reference['id']} EID does not match its identity")
        entities.append({"entity": entity, "reference": reference, "row": row})
    _reject_volume_membership(base, deck, snapshot, entities)
    return {"entities": entities}


def _part_members(base, deck, part, entity_type):
    members = base.CollectEntities(deck, part, entity_type, recursive=True)
    if members is None or (type(members) is int and members == 0):
        return []
    if type(members) not in (list, tuple):
        raise RuntimeError(f"ANSAPART {entity_type} membership is not an entity list")
    return [catalog._entity_reference(base, deck, entity, entity_type) for entity in members]


def _part_membership_snapshot(base, deck, part):
    return {
        entity_type: sorted(item["id"] for item in _part_members(base, deck, part, entity_type))
        for entity_type in ("SHELL", "SOLID", "VOLUME")
    }


def _part_membership_errors(before, after, expected_added):
    errors = []
    old_members = before.get("part_membership")
    new_members = after.get("part_membership")
    if type(old_members) is not dict or type(new_members) is not dict:
        return ["ANSAPART membership snapshot is unavailable"]
    for entity_type in ("SHELL", "SOLID", "VOLUME"):
        old_ids, new_ids = set(old_members.get(entity_type, ())), set(new_members.get(entity_type, ()))
        added, removed = new_ids - old_ids, old_ids - new_ids
        allowed = set(expected_added.get(entity_type, ()))
        if added != allowed:
            errors.append(f"ANSAPART {entity_type} additions differ from the declared mesh output")
        if removed:
            errors.append(f"ANSAPART {entity_type} members were removed: {sorted(removed)[:20]}")
    return errors


def _volume_solid_ids(base, deck, volume_id):
    volume, _reference = catalog._get_entity(base, deck, "VOLUME", volume_id)
    members = base.CollectEntities(deck, volume, "SOLID")
    if members is None or (type(members) is int and members == 0):
        return []
    if type(members) not in (list, tuple):
        raise RuntimeError(f"VOLUME {volume_id} SOLID membership is not an entity list")
    references = [
        catalog._entity_reference(base, deck, entity, "SOLID")
        for entity in members
    ]
    ids = [reference["id"] for reference in references]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"VOLUME {volume_id} SOLID membership contains duplicate identities")
    return sorted(ids)


def _merge_reference_evidence(base, deck, snapshot, resolved):
    reader = getattr(base, "ReferenceEntities", None)
    if not callable(reader):
        raise NotImplementedError("base.ReferenceEntities is required before merging NODEs")
    by_node, fe_by_node = {}, {}
    scanned = 0
    for pair in resolved["pairs"]:
        source_id = pair["source_id"]
        # Omitting deck avoids filtering out references invalid in the current deck.
        references = reader(pair["source"], group_results=False)
        if type(references) not in (list, tuple):
            raise RuntimeError(f"NODE {source_id} reference list is unavailable")
        scanned += len(references)
        if scanned > 10000:
            raise ValueError("merge NODE-reference scan exceeds the 10000-reference budget")
        expected_fe = {
            (row["entity"]["type"], row["entity"]["id"])
            for row in snapshot["element_cards"]
            if row["entity"]["type"] in {"SHELL", "SOLID"}
            and any(node_id == source_id for _field, node_id in _node_connections(row))
        }
        actual_fe, other = set(), set()
        for entity in references:
            kind = entity.ansa_type(deck)
            reference = catalog._entity_reference(base, deck, entity, kind)
            key = (reference["type"], reference["id"])
            (actual_fe if kind in {"SHELL", "SOLID"} else other).add(key)
        if actual_fe != expected_fe:
            raise RuntimeError(f"NODE {source_id} native references disagree with the FE connectivity snapshot")
        by_node[str(source_id)] = [{"type": kind, "id": entity_id} for kind, entity_id in sorted(other)]
        fe_by_node[str(source_id)] = [{"type": kind, "id": entity_id} for kind, entity_id in sorted(actual_fe)]
    return {"complete": True, "api": "base.ReferenceEntities", "deck_filter": False,
            "scanned_references": scanned, "by_node": by_node, "fe_by_node": fe_by_node}


def _require_merge_reference_clearance(snapshot, resolved):
    """Reject missing evidence or any source NODE reference outside supported FE."""
    evidence = snapshot.get("non_fe_node_references")
    if (
        type(evidence) is not dict
        or evidence.get("complete") is not True
        or type(evidence.get("by_node")) is not dict
    ):
        raise ValueError(
            "merge_nodes rejected before backup: complete non-FE NODE-reference scan is unavailable"
        )
    by_node = evidence["by_node"]
    for source_id in resolved["mapping"]:
        references = by_node.get(source_id, by_node.get(str(source_id)))
        if type(references) is not list:
            raise ValueError(
                f"merge_nodes rejected before backup: non-FE references for source NODE {source_id} are unverified"
            )
        if references:
            raise ValueError(
                f"merge_nodes source NODE {source_id} has non-FE references"
            )


def _check_snapshot_capacity(before, params, resolved):
    action = params["action"]
    if action == "extrude_quad":
        steps = params["steps"]
        node_count = len(resolved["source_node_ids"]) * steps
        shell_count = 0
        solid_count = len(resolved["shells"]) * steps
        volume_count = 1
    elif action == "mirror_copy":
        node_count = len(resolved["source_node_ids"])
        shell_count = sum(item["reference"]["type"] == "SHELL" for item in resolved["sources"])
        solid_count = sum(item["reference"]["type"] == "SOLID" for item in resolved["sources"])
        volume_count = 0
    else:
        return
    # Include the whole-model rows, independent FE-card readback, and the
    # native VOLUME row/card produced by an extrusion.
    projected = (
        len(_encoded(before)) + 4096 + node_count * 512
        + (shell_count + solid_count + volume_count) * 2048
    )
    if projected > _SNAPSHOT_BUDGET:
        raise ValueError(
            "projected post-operation snapshot exceeds the 256 KiB evidence budget; "
            "no backup or native mesh call was made"
        )


def _reject_volume_membership(base, deck, snapshot, entities, *, action="release_mesh"):
    selected = {(item["reference"]["type"], item["reference"]["id"]) for item in entities}
    for volume_id in (row["entity"]["id"] for row in snapshot["volumes"]):
        volume, _reference = catalog._get_entity(base, deck, "VOLUME", volume_id)
        for entity_type in ("SHELL", "SOLID"):
            members = base.CollectEntities(deck, volume, entity_type)
            if members is None or (type(members) is int and members == 0):
                continue
            if type(members) not in (list, tuple):
                raise RuntimeError(f"VOLUME {volume_id} {entity_type} membership is unreadable")
            keys = {
                (entity_type, catalog._entity_reference(base, deck, member, entity_type)["id"])
                for member in members
            }
            overlap = selected & keys
            if overlap:
                raise ValueError(
                    f"{action} refuses elements owned by a VOLUME; its implicit closure is outside this FE-only operation"
                )


def _node_connections(row):
    result = []
    for name, value in row["fields"].items():
        is_node_reference = type(value) is dict and value.get("type") == "NODE"
        if not _NODE_FIELD.fullmatch(name) and not is_node_reference:
            continue
        if _empty(value):
            continue
        result.append((name, _field_id(value, f"{row['entity']['type']} {row['entity']['id']} {name}", "NODE")))
    return result


def _capture_element_cards(base, deck):
    pairs = hexa_meshing._collect(base, deck, "__ELEMENTS__")
    if len(pairs) > 100000:
        raise ValueError("FE connectivity snapshot exceeds 100000 entities")
    rows = []
    for entity, reference in pairs:
        labels = hexa_meshing._labels(entity, deck, f"{reference['type']} {reference['id']}")
        fields = hexa_meshing._stable_fields(reference["type"], labels)
        if not fields:
            raise RuntimeError(f"{reference['type']} {reference['id']} has no stable card fields")
        rows.append({
            "entity": reference,
            "card_fields": list(fields),
            "fields": catalog._read_card(base, deck, entity, reference, fields),
        })
    rows.sort(key=lambda row: (row["entity"]["type"], row["entity"]["id"]))
    if len(_encoded(rows)) > _INTERNAL_ELEMENT_BUDGET:
        raise ValueError("FE connectivity snapshot exceeds the 8 MiB internal budget")
    return rows


def _capture(base, deck, *, include_element_cards=False):
    model = hexa_meshing._capture_snapshot(base, deck)
    if model.get("complete") is not True:
        raise RuntimeError("whole-model snapshot is incomplete")
    if len(_encoded(model)) > _SNAPSHOT_BUDGET:
        raise ValueError("whole-model snapshot exceeds the 256 KiB evidence budget")
    result = {"model": model}
    memberships = {}
    reference_count = 0
    for row in model["volumes"]:
        volume_id = row["entity"]["id"]
        ids = _volume_solid_ids(base, deck, volume_id)
        reference_count += len(ids)
        if reference_count > 10000:
            raise ValueError("VOLUME membership snapshot exceeds 10000 SOLID references")
        memberships[str(volume_id)] = ids
    result["volume_solid_memberships"] = memberships
    if len(_encoded(result)) > _SNAPSHOT_BUDGET:
        raise ValueError("model and VOLUME membership snapshot exceeds the 256 KiB evidence budget")
    if include_element_cards:
        result["element_cards"] = _capture_element_cards(base, deck)
    return result


def _state_equal(left, right):
    return catalog._strict_equal(left["model"], right["model"]) and catalog._strict_equal(
        left.get("element_cards"), right.get("element_cards")
    ) and catalog._strict_equal(left.get("volume_solid_memberships"), right.get("volume_solid_memberships"))


def _volume_membership_errors(before, after):
    old_ids = [str(row["entity"]["id"]) for row in before["model"]["volumes"]]
    if not old_ids:
        return []
    old = before.get("volume_solid_memberships")
    new = after.get("volume_solid_memberships")
    if type(old) is not dict or type(new) is not dict:
        return ["existing VOLUME SOLID membership snapshot is unavailable"]
    return [f"existing VOLUME {key} SOLID members changed or are unreadable"
            for key in old_ids if key not in old or key not in new or old[key] != new[key]]


def _digest(rows):
    return hashlib.sha256(_encoded(rows if rows is not None else [])).hexdigest()


def _map_row_node_refs(row, mapping):
    result = {
        "entity": dict(row["entity"]),
        "card_fields": list(row["card_fields"]),
        "fields": dict(row["fields"]),
    }
    connections = {name for name, _node_id in _node_connections(row)}
    for name in connections:
        value = result["fields"][name]
        node_id = _field_id(value, f"{row['entity']['type']} {row['entity']['id']} {name}", "NODE")
        target_id = mapping.get(node_id)
        if target_id is None:
            continue
        if type(value) is dict:
            result["fields"][name] = {**value, "id": target_id}
        elif type(value) is str:
            result["fields"][name] = str(target_id)
        else:
            result["fields"][name] = target_id
    return result


def _category_errors(before, after, *, added=None, removed=None, node_mapping=None):
    added = added or set()
    removed = removed or set()
    node_mapping = node_mapping or {}
    errors = []
    for category in hexa_meshing._CATEGORY_TYPES:
        old_rows, new_rows = _row_map(before[category]), _row_map(after[category])
        for entity_id, old in old_rows.items():
            key = (category, entity_id)
            if key in removed:
                if entity_id in new_rows:
                    errors.append(f"released {category[:-1]} {entity_id} still exists")
                continue
            expected = _map_row_node_refs(old, node_mapping) if node_mapping else old
            if entity_id not in new_rows:
                errors.append(f"original {category[:-1]} {entity_id} disappeared")
            elif not catalog._strict_equal(expected, new_rows[entity_id]):
                errors.append(f"original {category[:-1]} {entity_id} card changed unexpectedly")
        allowed_new_category = {
            entity_id for key_type, entity_id in added if key_type == category
        }
        unexpected = set(new_rows) - set(old_rows) - allowed_new_category
        if unexpected:
            errors.append(f"unexpected new {category} IDs: {sorted(unexpected)[:20]}")
    return errors


def _identity_errors(before, after, *, allowed_added=(), allowed_removed=()):
    old = _reference_keys(before)
    new = _reference_keys(after)
    added = new - old
    removed = old - new
    allowed_added, allowed_removed = set(allowed_added), set(allowed_removed)
    errors = []
    if added != allowed_added:
        errors.append(f"unexpected entity additions: {sorted(added ^ allowed_added)[:20]}")
    if removed != allowed_removed:
        errors.append(f"unexpected entity removals: {sorted(removed ^ allowed_removed)[:20]}")
    return errors


def _element_card_errors(before, after, *, removed=(), node_mapping=None, new_allowed=()):
    old_rows = {(row["entity"]["type"], row["entity"]["id"]): row for row in before}
    new_rows = {(row["entity"]["type"], row["entity"]["id"]): row for row in after}
    removed, new_allowed = set(removed), set(new_allowed)
    node_mapping = node_mapping or {}
    errors = []
    for key, row in old_rows.items():
        if key in removed:
            if key in new_rows:
                errors.append(f"released FE entity {key} remains")
            continue
        expected = _map_row_node_refs(row, node_mapping) if node_mapping else row
        if key not in new_rows or not catalog._strict_equal(expected, new_rows[key]):
            errors.append(f"FE entity {key} card changed outside the declared node map")
    if (set(new_rows) - set(old_rows)) != new_allowed:
        errors.append("unexpected FE entity additions after mesh operation")
    return errors


def _api_value(value):
    if type(value) in (int, float, str, bool) or value is None:
        return value
    return repr(value)[:512]


def _snapshot_digest(snapshot):
    if snapshot is None:
        return None
    encoded = _encoded(snapshot)
    return {
        "complete": snapshot.get("complete") is True,
        "details_omitted": True,
        "serialized_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _bounded_result(params, result):
    if len(_encoded(params)) + len(_encoded(result)) < MAX_FRAME_BYTES:
        return result
    compact = {
        "ok": False,
        "tool": _TOOL,
        "action": result.get("action"),
        "deck": result.get("deck"),
        "backup": result.get("backup"),
        "api_invoked": result.get("api_invoked", False),
        "api_return_type": result.get("api_return_type"),
        "api_return_value": result.get("api_return_value"),
        "before": _snapshot_digest(result.get("before")),
        "after": _snapshot_digest(result.get("after")),
        "element_cards_before_sha256": result.get("element_cards_before_sha256"),
        "element_cards_after_sha256": result.get("element_cards_after_sha256"),
        "scope_verified": False,
        "possible_effects": True,
        "evidence_truncated": True,
        "error": "mesh workflow result exceeded the response budget; inspect the model before any retry",
        "errors": ["mesh workflow evidence was truncated; scope is not reported as verified"],
    }
    return compact


def _failed(params, deck, backup, before, after, errors, *, api_invoked, api_status=None, api_error=None, possible_effects):
    details = [str(item)[:2048] for item in errors[:16]]
    if api_error:
        details.insert(0, str(api_error)[:2048])
    result = {
        "ok": False,
        "tool": _TOOL,
        "action": params["action"],
        "deck": deck,
        "backup": backup,
        "api_invoked": api_invoked,
        "api_return_type": type(api_status).__name__ if api_status is not None else None,
        "api_return_value": _api_value(api_status),
        "before": volume_meshing._evidence(before["model"]) if before else None,
        "after": volume_meshing._evidence(after["model"]) if after else None,
        "element_cards_before_sha256": _digest(before.get("element_cards")) if before and "element_cards" in before else None,
        "element_cards_after_sha256": _digest(after.get("element_cards")) if after and "element_cards" in after else None,
        "scope_verified": False,
        "possible_effects": bool(possible_effects),
        "error": "; ".join(details)[:4096] or "mesh workflow did not satisfy its postconditions",
        "errors": details or ["mesh workflow did not satisfy its postconditions"],
    }
    return _bounded_result(params, result)


def _success(params, deck, backup, before, after, data, api_status):
    result = {
        "ok": True,
        "tool": _TOOL,
        "action": params["action"],
        "deck": deck,
        "backup": backup,
        "api_invoked": True,
        "api_return_type": type(api_status).__name__,
        "api_return_value": _api_value(api_status),
        "before": volume_meshing._evidence(before["model"]),
        "after": volume_meshing._evidence(after["model"]),
        "element_cards_before_sha256": _digest(before.get("element_cards")) if "element_cards" in before else None,
        "element_cards_after_sha256": _digest(after.get("element_cards")) if "element_cards" in after else None,
        "scope_verified": True,
        "verification_scope": [
            "complete model entity identity inventory",
            "typed NODE/SHELL/SOLID/VOLUME/FACE/ANSAPART/SOLID_SECTION/MATERIAL cards",
            "all FE element card values and connectivity",
            "existing VOLUME SOLID member sets",
            "explicit ANSAPART membership for extrusion/mirror and VOLUME ownership precondition for release",
        ],
        "unverified_model_scope": [
            "non-FE geometry card values",
            "ANSA group/set and other non-FE association memberships",
        ],
        "possible_effects": True,
        "data": data,
        "errors": [],
    }
    return _bounded_result(params, result)


def _expected_api_success(action, value, count):
    if type(value) is not int:
        return False
    if action == "extrude_quad":
        return value == 1
    if action == "mirror_copy":
        return value == 0
    if action == "merge_nodes":
        return value == count
    return value == 0


def _invoke(base, mesh, action, params, resolved):
    if action == "extrude_quad":
        vector = params["vector"]
        length = resolved["vector_length"]
        direction = tuple(value / length for value in vector)
        extruder = mesh.VolumesExtrude()
        return extruder.translate(
            source=[item["entity"] for item in resolved["shells"]],
            axis=((0.0, 0.0, 0.0), direction),
            steps=params["steps"],
            distance=length,
            biasing="no",
            disconnect_from_source=False,
            stitch_sources=False,
            collapse_free_edges=False,
            part=resolved["part"][0],
            property=resolved["property"][0],
        )
    if action == "mirror_copy":
        points = params["plane"]
        return base.GeoMirrorPlane(
            input_function_type="COPY",
            pid_offset=0,
            group_offset="SAME PART",
            input_sets_type="NONE",
            fx=points["point1"][0], fy=points["point1"][1], fz=points["point1"][2],
            fx2=points["point2"][0], fy2=points["point2"][1], fz2=points["point2"][2],
            fx3=points["point3"][0], fy3=points["point3"][1], fz3=points["point3"][2],
            entities=[item["entity"] for item in resolved["sources"]],
            draw_results=False,
            keep_connectivity=False,
            # This optional Entity is required only with group_offset="EXISTING PART".
        )
    if action == "merge_nodes":
        return base.PasteNodes(
            source_nodes=[pair["source"] for pair in resolved["pairs"]],
            target_nodes=[pair["target"] for pair in resolved["pairs"]],
            allow_collapse=False,
            move_target="fe_position",
            delete_remaining_nodes=True,
        )
    return base.DeleteEntity(
        [item["entity"] for item in resolved["entities"]],
        force=False,
        compress=False,
    )


def _verify_extrusion(base, deck, params, before, after, resolved):
    errors = _volume_membership_errors(before, after)
    old = before["model"]
    new = after["model"]
    old_keys, new_keys = _reference_keys(old), _reference_keys(new)
    added = new_keys - old_keys
    new_nodes = set(_row_map(new["nodes"])) - set(_row_map(old["nodes"]))
    new_solids = set(_row_map(new["solids"])) - set(_row_map(old["solids"]))
    new_volumes = {entity_id for kind, entity_id in added if kind == "VOLUME"}
    expected_solids = len(resolved["shells"]) * params["steps"]
    errors.extend(_identity_errors(old, new, allowed_added={
        *(('NODE', i) for i in new_nodes),
        *(('SOLID', i) for i in new_solids),
        *(('VOLUME', i) for i in new_volumes),
    }))
    allowed_category_additions = {
        *(('nodes', entity_id) for entity_id in new_nodes),
        *(('solids', entity_id) for entity_id in new_solids),
        *(('volumes', entity_id) for entity_id in new_volumes),
    }
    errors.extend(_category_errors(old, new, added=allowed_category_additions))
    errors.extend(_element_card_errors(before["element_cards"], after["element_cards"], new_allowed={('SOLID', i) for i in new_solids}))
    if len(new_solids) != expected_solids:
        errors.append(f"expected {expected_solids} new SOLID entities, found {len(new_solids)}")
    new_solid_rows = [row for row in new["solids"] if row["entity"]["id"] in new_solids]
    new_node_coords = {node_id: _node_coordinates(new)[node_id] for node_id in new_nodes}
    expected_node_map = {}
    for node_id in resolved["source_node_ids"]:
        base_coord = resolved["coordinates"][node_id]
        for layer in range(1, params["steps"] + 1):
            point = tuple(base_coord[i] + resolved["delta"][i] * layer for i in range(3))
            matches = [item_id for item_id, coord in new_node_coords.items() if _same_point(point, coord, resolved["tolerance"])]
            if len(matches) != 1:
                errors.append(f"extrusion layer {layer} NODE for source {node_id} has {len(matches)} matches")
            else:
                expected_node_map[(node_id, layer)] = matches[0]
    if set(expected_node_map.values()) != new_nodes:
        errors.append("new NODE entities do not exactly match the requested uniform vector layers")

    source_by_footprint = {frozenset(item["node_ids"]): item for item in resolved["shells"]}
    cell_counts = {}
    used_nodes = set()
    volume_sum = 0.0
    for row in new_solid_rows:
        entity_id = row["entity"]["id"]
        fields = row["fields"]
        if not hexa_meshing._is_hexa8_type(fields.get("type")):
            errors.append(f"SOLID {entity_id} is not a first-order HEXA8")
            continue
        if fields.get("EID") != entity_id:
            errors.append(f"SOLID {entity_id} EID does not match its identity")
        if _field_id(fields.get("PID"), f"SOLID {entity_id} PID", "SOLID_SECTION") != params["property"]["id"]:
            errors.append(f"SOLID {entity_id} PID does not match the requested SOLID_SECTION")
        try:
            node_ids = _element_nodes(row, _SOLID_FIELDS, f"SOLID {entity_id}")
            _check_high_order(row, _HIGH_ORDER_SOLID_FIELDS, f"SOLID {entity_id}")
            points = [_node_coordinates(new)[node_id] for node_id in node_ids]
            actual_volume, _jacobian, local_scale = hexa_meshing._hexa_integration(points)
        except Exception as exc:
            errors.append(f"SOLID {entity_id} verification: {_error(exc)}")
            continue
        used_nodes.update(node_ids)
        volume_sum += actual_volume
        layers = []
        source_ids = []
        for node_id in node_ids:
            source_match = next((source_id for source_id in resolved["source_node_ids"] if source_id == node_id), None)
            if source_match is not None:
                source_ids.append(source_match)
                layers.append(0)
                continue
            match = next(((source_id, layer) for (source_id, layer), generated_id in expected_node_map.items() if generated_id == node_id), None)
            if match is None:
                errors.append(f"SOLID {entity_id} references an unexpected NODE {node_id}")
                continue
            source_ids.append(match[0])
            layers.append(match[1])
        if len(source_ids) != 8 or len(set(source_ids)) != 4 or len(set(layers)) != 2:
            errors.append(f"SOLID {entity_id} does not represent one translated QUAD layer")
            continue
        lower, upper = sorted(set(layers))
        if upper != lower + 1 or layers.count(lower) != 4 or layers.count(upper) != 4:
            errors.append(f"SOLID {entity_id} does not span exactly one uniform extrusion layer")
            continue
        footprint = frozenset(source_ids)
        source = source_by_footprint.get(footprint)
        if source is None:
            errors.append(f"SOLID {entity_id} does not map to a requested source QUAD")
            continue
        key = (source["reference"]["id"], lower)
        cell_counts[key] = cell_counts.get(key, 0) + 1
        expected = resolved["expected_cells"].get(key)
        if expected is None or not math.isclose(actual_volume, expected["volume"], rel_tol=1e-6, abs_tol=max(local_scale**3 * 1e-9, 1e-12)):
            errors.append(f"SOLID {entity_id} volume does not match its source QUAD layer")
    expected_keys = set(resolved["expected_cells"])
    if set(cell_counts) != expected_keys or any(value != 1 for value in cell_counts.values()):
        errors.append("new HEXA8 entities do not cover every source QUAD and layer exactly once")
    if new_nodes - used_nodes:
        errors.append(f"new NODE entities are unused by new HEXA8: {sorted(new_nodes - used_nodes)[:20]}")
    expected_volume = sum(item["volume"] for item in resolved["expected_cells"].values())
    if not math.isclose(volume_sum, expected_volume, rel_tol=1e-6, abs_tol=max(expected_volume * 1e-9, 1e-12)):
        errors.append("sum of independently integrated HEXA8 volumes does not match the requested extrusion")

    new_volume_rows = [row for row in new["volumes"] if row["entity"]["id"] in new_volumes]
    if len(new_volumes) != 1 or len(new_volume_rows) != 1:
        errors.append(f"expected exactly one new VOLUME entity, found {len(new_volumes)}")
    volume_solid_count = None
    for row in new_volume_rows:
        volume_id = row["entity"]["id"]
        fields = row["fields"]
        expected_fields = {
            "ID": volume_id,
            "Type": "Extrude",
            "Algorithm": "Translate",
            "Distribution 1": "Steps         ",
            "Extrude steps": params["steps"],
            "Status": "Meshed",
            "Order": "1st",
            "Stitch unconnected source": "NO",
            "Disconnect volume from source": "NO",
            "Connect to target": "NO",
            "Collapse free edges": "NO",
            "Biasing": "None",
            "Hexas": len(new_solids),
            "Grids": len(used_nodes),
            "Internal grids": len(new_nodes),
            "Tetras": 0,
            "Pentas": 0,
            "Pyramids": 0,
            "Polyhedrals": 0,
        }
        for name, expected in expected_fields.items():
            if fields.get(name) != expected:
                errors.append(
                    f"new VOLUME {volume_id} {name} is {fields.get(name)!r}, expected {expected!r}"
                )
        try:
            property_id = _field_id(fields.get("PID"), f"VOLUME {volume_id} PID", "SOLID_SECTION")
        except Exception as exc:
            errors.append(_error(exc))
        else:
            if property_id != params["property"]["id"]:
                errors.append(f"new VOLUME {volume_id} PID does not match the requested SOLID_SECTION")
        try:
            distance = _number(fields.get("Distance"), f"VOLUME {volume_id} Distance")
        except Exception as exc:
            errors.append(_error(exc))
        else:
            tolerance = max(resolved["vector_length"] * 1e-9, 1e-12)
            if not math.isclose(distance, resolved["vector_length"], rel_tol=1e-9, abs_tol=tolerance):
                errors.append(f"new VOLUME {volume_id} Distance does not match the requested vector length")
        try:
            reported_volume = _number(fields.get("Volume"), f"VOLUME {volume_id} Volume")
        except Exception as exc:
            errors.append(_error(exc))
        else:
            tolerance = max(abs(volume_sum) * 1e-9, 1e-12)
            if not math.isclose(reported_volume, volume_sum, rel_tol=1e-6, abs_tol=tolerance):
                errors.append(f"new VOLUME {volume_id} Volume does not match the independently integrated HEXA8 volume")
        try:
            member_ids = _volume_solid_ids(base, deck, volume_id)
        except Exception as exc:
            errors.append(f"new VOLUME {volume_id} SOLID membership readback: {_error(exc)}")
        else:
            volume_solid_count = len(member_ids)
            if member_ids != sorted(new_solids):
                errors.append(
                    f"new VOLUME {volume_id} SOLID members do not match the newly created SOLID entities"
                )
    errors.extend(_part_membership_errors(before, after, {"SOLID": new_solids, "VOLUME": new_volumes}))
    return errors, {
        "source_shell_ids": [item["reference"]["id"] for item in resolved["shells"]],
        "new_solid_ids": sorted(new_solids),
        "new_node_ids": sorted(new_nodes),
        "new_volume_id": next(iter(new_volumes)) if len(new_volumes) == 1 else None,
        "new_volume_solid_count": volume_solid_count,
        "hexa8_count": len(new_solids),
        "property_id": params["property"]["id"],
        "part_id": params["part"]["id"],
        "vector": list(params["vector"]),
        "steps": params["steps"],
        "volume_sum": volume_sum,
    }


def _element_attributes(row):
    ignored = {"eid", "id"}
    return {
        name: value
        for name, value in row["fields"].items()
        if name.casefold() not in ignored and not _NODE_FIELD.fullmatch(name)
    }


def _element_topology(entity_type, row, node_ids):
    if entity_type == "SHELL":
        shell_type = str(row["fields"].get("type", "")).strip().upper()
        if shell_type == "QUAD" and len(node_ids) == 4:
            edges, faces = ((0, 1), (1, 2), (2, 3), (3, 0)), ((0, 1, 2, 3),)
        elif shell_type in {"TRIA", "TRIA3"} and len(node_ids) == 3:
            edges, faces = ((0, 1), (1, 2), (2, 0)), ((0, 1, 2),)
        else:
            raise ValueError(f"unsupported mirrored SHELL topology {shell_type!r}")
    elif entity_type == "SOLID":
        if len(node_ids) != 8 or not hexa_meshing._is_hexa8_type(row["fields"].get("type")):
            raise ValueError("mirrored SOLID is not a first-order HEXA8")
        edges, faces = hexa_meshing._HEXA_EDGES, _HEXA_FACES
    else:
        raise ValueError(f"unsupported mirrored element type {entity_type!r}")
    edge_set = frozenset(frozenset((node_ids[left], node_ids[right])) for left, right in edges)
    face_set = frozenset(frozenset(node_ids[index] for index in face) for face in faces)
    return edge_set, face_set


def _shell_normal(node_ids, coordinates):
    first, second, third = (coordinates[node_id] for node_id in node_ids[:3])
    return _unit(_cross(_subtract(second, first), _subtract(third, first)), "SHELL normal")


def _verify_mirror(base, deck, params, before, after, resolved):
    errors = _volume_membership_errors(before, after)
    old, new = before["model"], after["model"]
    old_keys, new_keys = _reference_keys(old), _reference_keys(new)
    added = new_keys - old_keys
    new_node_ids = set(_row_map(new["nodes"])) - set(_row_map(old["nodes"]))
    new_element_keys = {key for key in added if key[0] in {"SHELL", "SOLID"}}
    expected_types = [item["reference"]["type"] for item in resolved["sources"]]
    actual_types = [key[0] for key in new_element_keys]
    if sorted(expected_types) != sorted(actual_types):
        errors.append("mirror did not create exactly one copy of each requested SHELL/SOLID")
    allowed_added = {("NODE", node_id) for node_id in new_node_ids} | new_element_keys
    errors.extend(_identity_errors(old, new, allowed_added=allowed_added))
    allowed_category_additions = {
        *(('nodes', entity_id) for entity_id in new_node_ids),
        *(('shells' if kind == 'SHELL' else 'solids', entity_id) for kind, entity_id in new_element_keys),
    }
    errors.extend(_category_errors(old, new, added=allowed_category_additions))
    errors.extend(_element_card_errors(before["element_cards"], after["element_cards"], new_allowed=new_element_keys))
    if len(new_node_ids) != len(resolved["source_node_ids"]):
        errors.append("mirror NODE count does not match the unique source NODE count")

    after_coords = _node_coordinates(new)
    source_to_copy = {}
    for source_id, expected_point in resolved["reflected"].items():
        matches = [node_id for node_id in new_node_ids if _same_point(expected_point, after_coords[node_id], resolved["tolerance"])]
        if len(matches) != 1:
            errors.append(f"mirror NODE for source {source_id} has {len(matches)} coordinate matches")
        else:
            source_to_copy[source_id] = matches[0]
    if len(set(source_to_copy.values())) != len(source_to_copy):
        errors.append("mirror output NODE mapping is not one-to-one")

    source_signatures = {}
    for source in resolved["sources"]:
        try:
            mapped_nodes = [source_to_copy[node_id] for node_id in source["node_ids"]]
            topology = _element_topology(source["reference"]["type"], source["row"], mapped_nodes)
        except Exception as exc:
            errors.append(f"source element topology could not be mapped: {_error(exc)}")
            continue
        signature = (
            source["reference"]["type"],
            _encoded(_element_attributes(source["row"])),
            topology,
        )
        source_signatures.setdefault(signature, []).append(source)
    output_rows = [row for category in ("shells", "solids") for row in new[category] if (row["entity"]["type"], row["entity"]["id"]) in new_element_keys]
    output_counts = {}
    for row in output_rows:
        element_type = row["entity"]["type"]
        if element_type == "SHELL":
            shell_type = str(row["fields"].get("type", "")).strip().upper()
            node_fields = _SHELL_FIELDS if shell_type == "QUAD" else _SHELL_FIELDS[:3]
            if shell_type == "TRIA" and not _empty(row["fields"].get("G4")):
                errors.append(f"mirrored SHELL {row['entity']['id']} TRIA G4 is not empty")
        else:
            node_fields = _SOLID_FIELDS
        try:
            node_ids = _element_nodes(row, node_fields, f"{element_type} {row['entity']['id']}")
            topology = _element_topology(element_type, row, node_ids)
        except Exception as exc:
            errors.append(f"mirrored {element_type} topology: {_error(exc)}")
            continue
        signature = (element_type, _encoded(_element_attributes(row)), topology)
        output_counts[signature] = output_counts.get(signature, 0) + 1
        if signature not in source_signatures:
            errors.append(f"mirrored {element_type} {row['entity']['id']} attributes/connectivity do not match a source")
            continue
        if element_type == "SHELL":
            try:
                source = source_signatures[signature][0]
                source_normal = _shell_normal(source["node_ids"], resolved["coordinates"])
                expected_normal = tuple(source_normal[i] - 2.0 * _dot(source_normal, resolved["normal"]) * resolved["normal"][i] for i in range(3))
                actual_normal = _shell_normal(node_ids, after_coords)
                if _dot(actual_normal, expected_normal) < 1.0 - 1e-6:
                    errors.append(f"mirrored SHELL {row['entity']['id']} direction does not preserve the reflected normal")
            except Exception as exc:
                errors.append(f"mirrored SHELL {row['entity']['id']} direction readback: {_error(exc)}")
        else:
            try:
                hexa_meshing._hexa_integration([after_coords[node_id] for node_id in node_ids])
            except Exception as exc:
                errors.append(f"mirrored SOLID {row['entity']['id']} has invalid HEXA8 Jacobian: {_error(exc)}")
    for signature, sources in source_signatures.items():
        if output_counts.get(signature, 0) != len(sources):
            errors.append("mirror output element multiplicity does not match explicit source multiplicity")
    expected_part_additions = {"SHELL": set(), "SOLID": set()}
    for kind, entity_id in new_element_keys:
        expected_part_additions[kind].add(entity_id)
    errors.extend(_part_membership_errors(before, after, expected_part_additions))
    return errors, {
        "source_ids": [item["reference"] for item in resolved["sources"]],
        "new_entity_ids": [{"type": kind, "id": entity_id} for kind, entity_id in sorted(new_element_keys)],
        "new_node_ids": sorted(new_node_ids),
        "part_id": params["part"]["id"],
    }


def _verify_merge(base, deck, params, before, after, resolved):
    errors = _volume_membership_errors(before, after)
    old, new = before["model"], after["model"]
    mapping = resolved["mapping"]
    removed_refs = {("NODE", node_id) for node_id in mapping}
    errors.extend(_identity_errors(old, new, allowed_removed=removed_refs))
    errors.extend(_category_errors(old, new, removed={("nodes", node_id) for node_id in mapping}, node_mapping=mapping))
    errors.extend(_element_card_errors(before["element_cards"], after["element_cards"], node_mapping=mapping))
    after_node_ids = set(_row_map(new["nodes"]))
    if set(mapping) & after_node_ids:
        errors.append("PasteNodes left one or more source NODE entities in the model")
    if set(mapping.values()) - after_node_ids:
        errors.append("PasteNodes removed one or more target NODE entities")
    for row in after["element_cards"]:
        node_ids = [node_id for _name, node_id in _node_connections(row)]
        if len(node_ids) != len(set(node_ids)):
            errors.append(f"FE entity {row['entity']} has collapsed connectivity after node merge")
            break
    before_nodes, after_nodes = _row_map(old["nodes"]), _row_map(new["nodes"])
    for target_id in mapping.values():
        if target_id in before_nodes and target_id in after_nodes and not catalog._strict_equal(before_nodes[target_id], after_nodes[target_id]):
            errors.append(f"target NODE {target_id} card changed during merge")
    return errors, {
        "pairs": [{"source_id": source_id, "target_id": target_id} for source_id, target_id in sorted(mapping.items())],
        "removed_source_node_ids": sorted(mapping),
        "updated_element_count": sum(
            1 for row in before["element_cards"]
            if any(node_id in mapping for _name, node_id in _node_connections(row))
        ),
    }


def _verify_release(params, before, after, resolved):
    errors = _volume_membership_errors(before, after)
    old, new = before["model"], after["model"]
    removed_refs = {(item["reference"]["type"], item["reference"]["id"]) for item in resolved["entities"]}
    removed_categories = {
        ("shells" if kind == "SHELL" else "solids", entity_id)
        for kind, entity_id in removed_refs
    }
    errors.extend(_identity_errors(old, new, allowed_removed=removed_refs))
    errors.extend(_category_errors(old, new, removed=removed_categories))
    errors.extend(_element_card_errors(before["element_cards"], after["element_cards"], removed=removed_refs))
    if _row_map(old["nodes"]) != _row_map(new["nodes"]):
        errors.append("release_mesh changed the NODE set or coordinates; orphan nodes must remain")
    if not catalog._strict_equal(old["faces"], new["faces"]):
        errors.append("release_mesh changed FACE geometry cards")
    return errors, {
        "released_entities": [item["reference"] for item in resolved["entities"]],
        "remaining_node_count": len(new["nodes"]),
        "geometry_face_count": len(new["faces"]),
        "orphan_node_cleanup": False,
    }


def _verify_transition(base, deck, params, before, after, resolved):
    if params["action"] == "extrude_quad":
        return _verify_extrusion(base, deck, params, before, after, resolved)
    if params["action"] == "mirror_copy":
        return _verify_mirror(base, deck, params, before, after, resolved)
    if params["action"] == "merge_nodes":
        return _verify_merge(base, deck, params, before, after, resolved)
    return _verify_release(params, before, after, resolved)


def execute(state, tool, params):
    """Run one controlled mesh action; native writes are never retried here."""
    state.assert_main()
    if type(tool) is not str or tool != _TOOL:
        raise ValueError(f"unsupported mesh workflow operation: {tool!r}")
    params = _normalise_inputs(params)
    from ansa import base, constants, mesh

    deck = catalog._deck(base, params)
    abaqus = getattr(constants, "ABAQUS", None)
    if type(abaqus) is not int or deck != abaqus:
        raise ValueError("mesh_workflow supports only the ABAQUS deck")
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    if params["action"] == "extrude_quad":
        if not callable(getattr(mesh, "VolumesExtrude", None)):
            raise NotImplementedError("ANSA mesh.VolumesExtrude is unavailable")
        if not callable(getattr(getattr(mesh, "VolumesExtrude"), "translate", None)):
            raise NotImplementedError("ANSA mesh.VolumesExtrude.translate is unavailable")
    elif params["action"] == "mirror_copy" and not callable(getattr(base, "GeoMirrorPlane", None)):
        raise NotImplementedError("ANSA base.GeoMirrorPlane is unavailable")
    elif params["action"] == "merge_nodes" and not callable(getattr(base, "PasteNodes", None)):
        raise NotImplementedError("ANSA base.PasteNodes is unavailable")
    elif params["action"] == "release_mesh" and not callable(getattr(base, "DeleteEntity", None)):
        raise NotImplementedError("ANSA base.DeleteEntity is unavailable")

    context = volume_meshing._execution_context(state, deck)
    backup_path, _prior = element_types._save_path(params["backup_path"], False)
    current_path = element_types._database_name(base)
    if current_path and model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_fingerprint = model_files._fingerprint(Path(current_path)) if current_path else None

    # Capture every FE card for all four actions so unrelated connectivity edits fail closed.
    include_elements = True
    before = _capture(base, deck, include_element_cards=include_elements)
    resolved = _resolve_action(
        base, deck, before if include_elements else before["model"], params
    )
    if params["action"] == "merge_nodes":
        before["non_fe_node_references"] = _merge_reference_evidence(base, deck, before, resolved)
        _require_merge_reference_clearance(before, resolved)
    elif params["action"] in {"extrude_quad", "mirror_copy"}:
        before["part_membership"] = _part_membership_snapshot(
            base, deck, resolved["part"][0]
        )
        _check_snapshot_capacity(before, params, resolved)
    volume_meshing._same_context(state, deck, context)

    backup = None
    try:
        backup = element_types._verify_backup(
            base, constants, backup_path, current_path, current_fingerprint
        )
    except Exception as exc:
        try:
            after_save = _capture(base, deck, include_element_cards=include_elements)
        except Exception:
            after_save = None
        possible = after_save is None or not _state_equal(before, after_save)
        return _failed(
            params, deck, element_types._unconfirmed_backup(backup_path), before,
            after_save, [_error(exc), "backup was not confirmed; native mesh API was not called"],
            api_invoked=False, possible_effects=possible,
        )

    try:
        volume_meshing._same_context(state, deck, context)
        after_backup = _capture(base, deck, include_element_cards=include_elements)
        if params["action"] in {"extrude_quad", "mirror_copy"}:
            after_backup["part_membership"] = _part_membership_snapshot(
                base, deck, resolved["part"][0]
            )
            if before["part_membership"] != after_backup["part_membership"]:
                raise RuntimeError("ANSAPART membership changed while creating the backup")
        if not _state_equal(before, after_backup):
            return _failed(
                params, deck, backup, before, after_backup,
                ["model snapshot changed after backup and before the native mesh call"],
                api_invoked=False, possible_effects=True,
            )
        resolved = _resolve_action(
            base, deck,
            after_backup if include_elements else after_backup["model"],
            params,
        )
        if params["action"] == "merge_nodes":
            after_backup["non_fe_node_references"] = _merge_reference_evidence(base, deck, after_backup, resolved)
            _require_merge_reference_clearance(after_backup, resolved)
            if not catalog._strict_equal(before["non_fe_node_references"], after_backup["non_fe_node_references"]):
                raise RuntimeError("NODE references changed while creating the backup")
        volume_meshing._same_context(state, deck, context)
    except Exception as exc:
        return _failed(
            params, deck, backup, before, locals().get("after_backup"), [_error(exc)],
            api_invoked=False, possible_effects=True,
        )

    api_status = None
    api_error = None
    try:
        api_status = _invoke(base, mesh, params["action"], params, resolved)
    except Exception as exc:
        api_error = _error(exc)

    try:
        after = _capture(base, deck, include_element_cards=include_elements)
        readback_error = None
    except Exception as exc:
        after = None
        readback_error = _error(exc)
    part_membership_error = None
    if after is not None and params["action"] in {"extrude_quad", "mirror_copy"}:
        try:
            after["part_membership"] = _part_membership_snapshot(
                base, deck, resolved["part"][0]
            )
        except Exception as exc:
            part_membership_error = _error(exc)
    try:
        volume_meshing._same_context(state, deck, context)
        context_error = None
    except Exception as exc:
        context_error = _error(exc)

    errors = []
    output = None
    if after is None:
        errors.append("post-operation model readback is unknown")
    else:
        try:
            errors, output = _verify_transition(base, deck, params, before, after, resolved)
        except Exception as exc:
            errors = [_error(exc)]
    if readback_error:
        errors.append("post-operation readback: " + readback_error)
    if part_membership_error:
        errors.append("post-operation ANSAPART membership readback: " + part_membership_error)
    if context_error:
        errors.append("post-operation target/model context: " + context_error)
    if api_error:
        errors.insert(0, api_error)
    count = len(params.get("pairs", ())) if params["action"] == "merge_nodes" else 0
    api_ok = api_error is None and _expected_api_success(params["action"], api_status, count)
    if not api_ok:
        errors.insert(0, "native mesh API did not return its exact documented success value")
    if errors:
        return _failed(
            params, deck, backup, before, after, errors, api_invoked=True,
            api_status=api_status,
            possible_effects=after is None or not _state_equal(before, after),
        )
    return _success(params, deck, backup, before, after, output or {}, api_status)


__all__ = ["execute"]
