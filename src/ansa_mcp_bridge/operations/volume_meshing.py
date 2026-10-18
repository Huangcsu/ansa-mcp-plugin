"""Guarded NASTRAN meshing of one existing, axis-aligned box volume."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import _model_save, _save_path, entity_catalog as catalog, model_files


_MAX_MODEL_ENTITIES = 10000
_MAX_SHELL_IDS = 200
_MAX_CARD_FIELDS = 512
_SNAPSHOT_BUDGET = MAX_FRAME_BYTES // 4
_RESULT_BUDGET = MAX_FRAME_BYTES * 3 // 4
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_GEOMETRY_REL_TOL = 1e-9
_VOLUME_REL_TOL = 1e-8
_TETRA_REL_TOL = 1e-12

_EXPECTED_VOLUME_FIELDS = frozenset({"PID", "Type", "Order", "Status", "Frozen"})
_GRID_REQUIRED_FIELDS = ("NID", "CP", "X1", "X2", "X3", "CD", "PS", "SEID")
_FACE_NODE_FIELDS = ("G1", "G2", "G3", "G4")
_SOLID_NODE_FIELDS = ("G1", "G2", "G3", "G4", "G5")
_DERIVED_CARD_FIELDS = frozenset({"numelem"})
_VOLUME_DERIVED_FIELDS = frozenset({
    "type", "order", "status", "volume", "mass", "grids", "internalgrids",
    "tetras", "pentas", "pyramids", "hexas", "polyhedrals", "numelem",
})

# These settings are the values observed when VolumesMeshV first assigns its
# Tetra Rapid algorithm to an Undefined volume. Existing values are read-only.
_NEW_MESH_SETTINGS = {
    "Growth rate": 1.2,
    "Element length": "0.0",
    "Criterion": "NASTRAN Max. Aspect Ratio",
    "Criterion value": 4.0,
    "Create pyramids": "YES",
    "Light volume representation": "NO ",
    "Allow reconstruction of volume skin": "NO ",
    "Frozen entities": 0,
    "Force rows of tetras in thin walls": 1,
    "Do not violate maximum element length": "NO ",
    "Autodefine coordinate system": "NO ",
    "Coordinate system": 0,
    "Buffer zones": 0,
    "Transition point offset": "NO ",
    "Polyhedral connection": "NO ",
    "Connect to symmetry": "NO ",
    "Smooth transition": "NO ",
}


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _field_key(name):
    return "".join(char.casefold() for char in name if char.isalnum())


def _snapshot_digest(snapshot):
    encoded = _encoded(snapshot)
    return {
        "complete": snapshot.get("complete") is True,
        "details_omitted": True,
        "serialized_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _evidence(snapshot):
    if snapshot is None:
        return None
    encoded = _encoded(snapshot)
    if len(encoded) <= _SNAPSHOT_BUDGET:
        return snapshot
    return _snapshot_digest(snapshot)


def _bounded_result(params, result):
    result = dict(result)
    result["errors"] = [str(item)[:2048] for item in result.get("errors", [])[:16]]
    if type(result.get("error")) is str:
        result["error"] = result["error"][:4096]

    request_size = len(_encoded(params))
    result_size = len(_encoded(result))
    if result_size <= _RESULT_BUDGET and request_size + result_size < MAX_FRAME_BYTES:
        return result

    errors = list(result.get("errors", []))
    errors.append(
        "volume meshing evidence exceeds the reply budget; before/after snapshot digests retained"
    )
    result.update(
        ok=False,
        scope_verified=False,
        possible_effects=True,
        evidence_truncated=True,
        before=_snapshot_digest(result["before"])
        if type(result.get("before")) is dict and "sha256" not in result["before"]
        else result.get("before"),
        after=_snapshot_digest(result["after"])
        if type(result.get("after")) is dict and "sha256" not in result["after"]
        else result.get("after"),
        errors=errors[:16],
    )
    result["error"] = "; ".join(result["errors"])[:4096]
    if len(_encoded(result)) <= _RESULT_BUDGET and request_size + len(_encoded(result)) < MAX_FRAME_BYTES:
        return result

    minimal = {
        "ok": False,
        "tool": "mesh_volume",
        "deck": result.get("deck"),
        "volume_id": result.get("volume_id"),
        "backup": result.get("backup"),
        "before": result.get("before"),
        "after": result.get("after"),
        "scope_verified": False,
        "model_scope_matches": False,
        "possible_effects": True,
        "api_return_code": result.get("api_return_code"),
        "api_return_type": result.get("api_return_type"),
        "error": "volume meshing result exceeds the bounded persistence budget",
        "errors": ["volume meshing result exceeds the bounded persistence budget"],
        "evidence_truncated": True,
    }
    return minimal


def _execution_context(state, deck):
    state.check_conflict()
    probe = state.probe()
    model, grant = probe.get("model"), probe.get("authorization")
    if (
        type(model) is not dict
        or type(model.get("epoch")) is not int
        or model["epoch"] <= 0
        or model.get("write_enabled") is not True
        or probe.get("deck") != deck
        or probe.get("connected") is not True
        or probe.get("stopped") is not False
        or probe.get("persistence_blocked") is not False
        or type(grant) is not dict
        or grant.get("active") is not True
        or not grant.get("backend_id")
        or type(grant.get("generation")) is not int
        or grant["generation"] <= 0
        or getattr(state, "connected_backend_id", None) != grant["backend_id"]
    ):
        raise RuntimeError("volume meshing model context or write authorization is unavailable")
    identity = {key: probe.get(key) for key in ("pid", "process_start", "instance_id", "session_id")}
    if any(value is None for value in identity.values()):
        raise RuntimeError("volume meshing process context is incomplete")
    return {
        **identity,
        "deck": deck,
        "epoch": model["epoch"],
        "backend_id": grant["backend_id"],
        "generation": grant["generation"],
    }


def _same_context(state, deck, expected):
    if _execution_context(state, deck) != expected:
        raise RuntimeError("model context or write authorization changed during volume meshing")


def _card_fields(entity, deck, label):
    getter = getattr(entity, "card_fields", None)
    if not callable(getter):
        raise NotImplementedError(f"{label} card_fields capability is unavailable")
    fields = getter(deck)
    if type(fields) not in (list, tuple):
        raise RuntimeError(f"{label} card_fields did not return a field-name list")
    if len(fields) > _MAX_CARD_FIELDS:
        raise ValueError(f"{label} card_fields exceeds the {_MAX_CARD_FIELDS}-field limit")
    if any(type(name) is not str or not name for name in fields):
        raise RuntimeError(f"{label} card_fields returned an invalid field name")
    if len(fields) != len(set(fields)):
        raise RuntimeError(f"{label} card_fields returned duplicate field names")
    return tuple(fields)


def _read_full_card(base, deck, entity, reference, label, *, omit_derived=False,
                    required_fields=()):
    labels = _card_fields(entity, deck, label)
    if not labels:
        raise RuntimeError(f"{label} has no readable card fields")
    missing = sorted(set(required_fields) - set(labels))
    if missing:
        raise NotImplementedError(
            f"{label} lacks required card field(s): {', '.join(missing)}"
        )
    selected = tuple(
        name for name in labels
        if not (omit_derived and _field_key(name) in _DERIVED_CARD_FIELDS)
    )
    values = catalog._read_card(base, deck, entity, reference, selected)
    return {"entity": reference, "card_fields": list(selected), "fields": values}


def _collect(base, deck, search_type, *, container=None, expected_type=None):
    collect = getattr(base, "CollectEntities", None)
    if not callable(collect):
        raise NotImplementedError("ANSA base.CollectEntities is unavailable")
    if container is None:
        entities = collect(
            deck, None, search_type, False, filter_visible=False, hidden_entities=True
        )
    else:
        entities = collect(deck, container, search_type)
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({search_type}) did not return a complete entity list")
    if len(entities) > _MAX_MODEL_ENTITIES:
        raise ValueError(f"{search_type} collection exceeds the {_MAX_MODEL_ENTITIES}-entity limit")

    pairs = []
    seen = set()
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity, expected_type)
        key = (reference["type"], reference["id"])
        if key in seen:
            raise RuntimeError(f"CollectEntities({search_type}) contains duplicate identities")
        seen.add(key)
        pairs.append((entity, reference))
    pairs.sort(key=lambda pair: (pair[1]["type"], pair[1]["id"]))
    return pairs


def _refs(pairs):
    return [reference for _, reference in pairs]


def _card_rows(base, deck, pairs, label, *, omit_derived=False):
    rows = []
    for entity, reference in pairs:
        rows.append(
            _read_full_card(
                base,
                deck,
                entity,
                reference,
                f"{label} {reference['id']}",
                omit_derived=omit_derived,
            )
        )
    return rows


def _capture_snapshot(base, deck, volume_id, expected_shell_ids):
    """Capture actual boundary membership; expectation checks must preserve this evidence."""
    all_pairs = _collect(base, deck, "__ALL_ENTITIES__")
    if len(all_pairs) > _MAX_MODEL_ENTITIES:
        raise ValueError(f"model exceeds the {_MAX_MODEL_ENTITIES}-entity limit")
    all_refs = _refs(all_pairs)
    all_keys = {(item["type"], item["id"]) for item in all_refs}

    fe_pairs = _collect(base, deck, "__ELEMENTS__")
    shell_pairs = _collect(base, deck, "SHELL", expected_type="SHELL")
    grid_pairs = _collect(base, deck, "GRID", expected_type="GRID")
    volume_pairs = _collect(base, deck, "VOLUME", expected_type="VOLUME")
    property_pairs = _collect(base, deck, "__PROPERTIES__")
    material_pairs = _collect(base, deck, "__MATERIALS__")
    psolid_pairs = _collect(base, deck, "PSOLID", expected_type="PSOLID")
    mat1_pairs = _collect(base, deck, "MAT1", expected_type="MAT1")
    solid_pairs = _collect(base, deck, "SOLID", expected_type="SOLID")

    categories = (fe_pairs, shell_pairs, grid_pairs, volume_pairs, property_pairs,
                  material_pairs, psolid_pairs, mat1_pairs, solid_pairs)
    for pairs in categories:
        if any((ref["type"], ref["id"]) not in all_keys for _, ref in pairs):
            raise RuntimeError("a typed entity collection is missing from __ALL_ENTITIES__")

    selected_volumes = [
        (entity, reference) for entity, reference in volume_pairs
        if reference["id"] == volume_id
    ]
    if len(selected_volumes) != 1:
        raise LookupError(f"VOLUME {volume_id} is absent or ambiguous in the current model")
    volume_entity, volume_reference = selected_volumes[0]

    boundary_entities = base.CollectEntities(deck, volume_entity, "SHELL")
    if type(boundary_entities) not in (list, tuple):
        raise RuntimeError("CollectEntities(deck, volume, 'SHELL') did not return a shell list")
    if len(boundary_entities) > _MAX_SHELL_IDS:
        raise ValueError(f"VOLUME boundary exceeds the {_MAX_SHELL_IDS}-shell limit")
    boundary_refs = []
    boundary_seen = set()
    for entity in boundary_entities:
        reference = catalog._entity_reference(base, deck, entity, "SHELL")
        key = (reference["type"], reference["id"])
        if key in boundary_seen:
            raise RuntimeError("VOLUME boundary collection contains duplicate SHELL identities")
        boundary_seen.add(key)
        boundary_refs.append(reference)
    boundary_refs.sort(key=lambda item: item["id"])

    shell_cards = _card_rows(base, deck, shell_pairs, "SHELL")
    shell_ids = {row["entity"]["id"] for row in shell_cards}
    if any(reference["id"] not in shell_ids for reference in boundary_refs):
        raise RuntimeError("VOLUME boundary SHELL is missing from the global SHELL collection")

    grid_rows = []
    for entity, reference in grid_pairs:
        grid_rows.append(_read_full_card(
            base, deck, entity, reference, f"GRID {reference['id']}",
            omit_derived=True, required_fields=_GRID_REQUIRED_FIELDS,
        ))

    snapshot = {
        "complete": True,
        "deck": deck,
        "entity_count": len(all_refs),
        "entity_references": all_refs,
        "fe_references": _refs(fe_pairs),
        "fe_cards": _card_rows(base, deck, fe_pairs, "FE", omit_derived=True),
        "boundary_shell_references": boundary_refs,
        "shell_cards": shell_cards,
        "grids": grid_rows,
        "volumes": _card_rows(base, deck, volume_pairs, "VOLUME"),
        "property_references": _refs(property_pairs),
        "property_cards": _card_rows(
            base, deck, property_pairs, "property", omit_derived=True
        ),
        "material_references": _refs(material_pairs),
        "psolid_cards": _card_rows(
            base, deck, psolid_pairs, "PSOLID", omit_derived=True
        ),
        "mat1_cards": _card_rows(
            base, deck, mat1_pairs, "MAT1", omit_derived=True
        ),
        "solid_cards": _card_rows(base, deck, solid_pairs, "SOLID"),
    }
    return snapshot, volume_entity


def _validate_inputs(params):
    required = (
        "deck", "volume_id", "expected_fields", "expected_shell_ids", "backup_path"
    )
    catalog._object(params, required, label="params")
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")
    volume_id = catalog._positive_int(params["volume_id"], "volume_id")
    expected_fields = params["expected_fields"]
    if type(expected_fields) is not dict or not 5 <= len(expected_fields) <= 100:
        raise ValueError("expected_fields must contain 5 to 100 card values")
    if any(type(name) is not str or not name for name in expected_fields):
        raise ValueError("expected_fields keys must be non-empty strings")
    missing = _EXPECTED_VOLUME_FIELDS - expected_fields.keys()
    if missing:
        raise ValueError(
            "expected_fields must include: " + ", ".join(sorted(missing))
        )

    shell_ids = params["expected_shell_ids"]
    if type(shell_ids) is not list or not 1 <= len(shell_ids) <= _MAX_SHELL_IDS:
        raise ValueError(f"expected_shell_ids must contain 1 to {_MAX_SHELL_IDS} IDs")
    shell_ids = [
        catalog._positive_int(entity_id, f"expected_shell_ids[{index}]")
        for index, entity_id in enumerate(shell_ids)
    ]
    if len(shell_ids) != len(set(shell_ids)):
        raise ValueError("expected_shell_ids must not contain duplicate IDs")
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    normalized = {
        "deck": params["deck"],
        "volume_id": volume_id,
        "expected_fields": dict(expected_fields),
        "expected_shell_ids": shell_ids,
        "backup_path": params["backup_path"],
    }
    try:
        request_size = len(_encoded(normalized))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"volume meshing request is not strict JSON: {exc}") from exc
    if request_size > _REQUEST_BUDGET:
        raise ValueError("volume meshing request exceeds the 128 KiB budget")
    return normalized


def _row_map(rows):
    result = {}
    for row in rows:
        entity_id = row["entity"]["id"]
        if entity_id in result:
            raise RuntimeError(f"duplicate entity ID {entity_id} in snapshot")
        result[entity_id] = row
    return result


def _finite_number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _coord(row, label):
    fields = row["fields"]
    if fields.get("CP") not in (None, "", 0):
        raise ValueError(f"{label} must use the basic NASTRAN coordinate system CP=0")
    return tuple(_finite_number(fields[name], f"{label} {name}") for name in ("X1", "X2", "X3"))


def _subtract(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _norm(vector):
    return math.sqrt(_dot(vector, vector))


def _validate_box(snapshot, expected_shell_ids):
    shell_rows = _row_map(snapshot["shell_cards"])
    grid_rows = _row_map(snapshot["grids"])
    shell_ids = sorted(expected_shell_ids)
    if len(shell_ids) != 6:
        raise ValueError("the first volume meshing batch supports exactly six CQUAD4 faces")

    faces = []
    node_ids = set()
    for shell_id in shell_ids:
        row = shell_rows.get(shell_id)
        if row is None:
            raise RuntimeError(f"boundary SHELL {shell_id} is absent from the full model snapshot")
        fields = row["fields"]
        if fields.get("type") != "CQUAD4":
            raise ValueError(f"boundary SHELL {shell_id} must be a CQUAD4")
        if fields.get("EID") != shell_id:
            raise RuntimeError(f"boundary SHELL {shell_id} EID does not match its entity ID")
        nodes = [_positive_int(fields[name], f"SHELL {shell_id} {name}") for name in _FACE_NODE_FIELDS]
        if len(nodes) != len(set(nodes)):
            raise ValueError(f"boundary SHELL {shell_id} has repeated GRID IDs")
        for node_id in nodes:
            if node_id not in grid_rows:
                raise RuntimeError(f"boundary SHELL {shell_id} references missing GRID {node_id}")
        faces.append((shell_id, nodes))
        node_ids.update(nodes)

    if len(node_ids) != 8:
        raise ValueError("six CQUAD4 faces must use exactly eight distinct box-corner GRID IDs")
    coords = {node_id: _coord(grid_rows[node_id], f"GRID {node_id}") for node_id in node_ids}
    mins = tuple(min(point[axis] for point in coords.values()) for axis in range(3))
    maxs = tuple(max(point[axis] for point in coords.values()) for axis in range(3))
    dims = tuple(maxs[axis] - mins[axis] for axis in range(3))
    scale = max(dims)
    if not math.isfinite(scale) or scale <= 0 or any(
        not math.isfinite(value) or value <= scale * _GEOMETRY_REL_TOL for value in dims
    ):
        raise ValueError("the selected volume must have positive finite box dimensions")
    coord_tol = scale * _GEOMETRY_REL_TOL
    corner_ids = {}
    for node_id, point in coords.items():
        bits = []
        for axis in range(3):
            low, high = abs(point[axis] - mins[axis]) <= coord_tol, abs(point[axis] - maxs[axis]) <= coord_tol
            if low == high:
                raise ValueError("boundary GRID coordinates must be the eight box corners")
            bits.append(0 if low else 1)
        key = tuple(bits)
        if key in corner_ids:
            raise ValueError("two boundary GRID IDs occupy the same box corner")
        corner_ids[key] = node_id
    expected_corners = {
        (x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)
    }
    if set(corner_ids) != expected_corners:
        raise ValueError("boundary GRID coordinates do not form all eight box corners")

    center = tuple((mins[axis] + maxs[axis]) / 2.0 for axis in range(3))
    planes = {}
    edge_uses = {}
    face_neighbors = {shell_id: set() for shell_id in shell_ids}
    for shell_id, nodes in faces:
        points = [coords[node_id] for node_id in nodes]
        candidates = []
        for axis in range(3):
            if all(abs(point[axis] - mins[axis]) <= coord_tol for point in points):
                candidates.append((axis, 0))
            if all(abs(point[axis] - maxs[axis]) <= coord_tol for point in points):
                candidates.append((axis, 1))
        if len(candidates) != 1:
            raise ValueError(f"boundary SHELL {shell_id} is not one axis-aligned box face")
        plane = candidates[0]
        if plane in planes:
            raise ValueError("the box boundary contains more than one face on a box plane")
        planes[plane] = shell_id

        corner_sequence = []
        for node_id in nodes:
            corner = next((key for key, value in corner_ids.items() if value == node_id), None)
            if corner is None or corner[plane[0]] != plane[1]:
                raise ValueError(f"boundary SHELL {shell_id} does not use the corners of its plane")
            corner_sequence.append(corner)
        free_axes = [axis for axis in range(3) if axis != plane[0]]
        face_corners = {tuple(corner[axis] for axis in free_axes) for corner in corner_sequence}
        if face_corners != {(0, 0), (0, 1), (1, 0), (1, 1)}:
            raise ValueError(f"boundary SHELL {shell_id} does not cover a rectangular box face")
        for index in range(4):
            left, right = corner_sequence[index], corner_sequence[(index + 1) % 4]
            if sum(a != b for a, b in zip(left, right)) != 1:
                raise ValueError(f"boundary SHELL {shell_id} has a crossed or degenerate node order")
            edge = tuple(sorted((nodes[index], nodes[(index + 1) % 4])))
            edge_uses.setdefault(edge, []).append(
                (shell_id, nodes[index], nodes[(index + 1) % 4])
            )

        edge_a = _subtract(points[1], points[0])
        edge_b = _subtract(points[3], points[0])
        normal = _cross(edge_a, edge_b)
        if _norm(normal) <= scale * scale * _TETRA_REL_TOL:
            raise ValueError(f"boundary SHELL {shell_id} has zero area")
        outward = _subtract(
            tuple(sum(point[axis] for point in points) / 4.0 for axis in range(3)), center
        )
        if _dot(normal, outward) <= _norm(normal) * scale * _GEOMETRY_REL_TOL:
            raise ValueError(f"boundary SHELL {shell_id} does not point outward")

    expected_planes = {(axis, side) for axis in range(3) for side in (0, 1)}
    if set(planes) != expected_planes:
        raise ValueError("the six CQUAD4 faces do not cover all six box planes")
    if len(edge_uses) != 12:
        raise ValueError("the six-face boundary does not contain exactly twelve shared edges")
    for edge, uses in edge_uses.items():
        if len(uses) != 2:
            raise ValueError(f"box edge {edge} is not shared by exactly two faces")
        first, second = uses
        if (first[1], first[2]) != (second[2], second[1]):
            raise ValueError(f"box edge {edge} is not traversed in opposite directions")
        face_neighbors[first[0]].add(second[0])
        face_neighbors[second[0]].add(first[0])
    reached = set()
    pending = [shell_ids[0]]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(face_neighbors[current] - reached)
    if reached != set(shell_ids):
        raise ValueError("the six CQUAD4 boundary faces are disconnected")

    return {
        "mins": mins,
        "maxs": maxs,
        "dimensions": dims,
        "scale": scale,
        "coord_tolerance": coord_tol,
        "volume": dims[0] * dims[1] * dims[2],
        "corner_node_ids": sorted(node_ids),
        "shell_ids": shell_ids,
    }


def _validate_preconditions(snapshot, params):
    if snapshot.get("complete") is not True:
        raise RuntimeError("preflight model snapshot is incomplete")
    if [item["id"] for item in snapshot["boundary_shell_references"]] != sorted(
        params["expected_shell_ids"]
    ):
        raise ValueError("VOLUME SHELL boundary does not exactly match expected_shell_ids")
    if snapshot["solid_cards"]:
        raise ValueError("volume meshing currently requires a model with no pre-existing SOLID entities")

    volume_rows = _row_map(snapshot["volumes"])
    volume = volume_rows.get(params["volume_id"])
    if volume is None or volume["entity"]["type"] != "VOLUME":
        raise LookupError(f"VOLUME {params['volume_id']} is absent from the full snapshot")
    fields = volume["fields"]
    missing = _EXPECTED_VOLUME_FIELDS - fields.keys()
    if missing:
        raise NotImplementedError("VOLUME card lacks required fields: " + ", ".join(sorted(missing)))
    for name, expected in params["expected_fields"].items():
        if name not in fields or not catalog._strict_equal(fields[name], expected):
            raise RuntimeError(f"VOLUME {params['volume_id']} field {name} changed since it was read")
    if fields["Type"] != "Undefined" or fields["Status"] != "Unmeshed":
        raise ValueError("VOLUME must have Type='Undefined' and Status='Unmeshed'")
    if fields["Order"] not in ("", None):
        raise ValueError("VOLUME Order must be empty before meshing")
    if type(fields["Frozen"]) is not str or fields["Frozen"].casefold() != "no":
        raise ValueError("VOLUME is frozen or its Frozen state is unknown")

    property_id = _positive_int(fields["PID"], "VOLUME PID")
    psolid_rows = _row_map(snapshot["psolid_cards"])
    psolid = psolid_rows.get(property_id)
    if psolid is None:
        raise ValueError(f"VOLUME PID {property_id} does not resolve to an existing PSOLID")
    psolid_fields = psolid["fields"]
    if psolid_fields.get("PID") != property_id or "MID" not in psolid_fields:
        raise RuntimeError(f"PSOLID {property_id} has incomplete identity or MID fields")
    material_id = _positive_int(psolid_fields["MID"], f"PSOLID {property_id} MID")
    mat1 = _row_map(snapshot["mat1_cards"]).get(material_id)
    if mat1 is None:
        raise ValueError(f"PSOLID {property_id} MID {material_id} does not resolve to an existing MAT1")
    mat1_fields = mat1["fields"]
    if mat1_fields.get("MID") != material_id:
        raise RuntimeError(f"MAT1 {material_id} MID does not match its entity ID")
    if type(mat1_fields.get("DEFINED")) is not str or mat1_fields["DEFINED"].casefold() != "yes":
        raise ValueError(f"MAT1 {material_id} must have DEFINED='YES'")
    if psolid["entity"] not in snapshot["property_references"]:
        raise RuntimeError(f"PSOLID {property_id} is absent from the complete property references")
    if mat1["entity"] not in snapshot["material_references"]:
        raise RuntimeError(f"MAT1 {material_id} is absent from the complete material references")

    geometry = _validate_box(snapshot, params["expected_shell_ids"])
    return {
        "volume": volume,
        "property_id": property_id,
        "material_id": material_id,
        "geometry": geometry,
    }


def _database_name(base):
    getter = getattr(base, "DataBaseName", None)
    if not callable(getter):
        raise NotImplementedError("ANSA base.DataBaseName is unavailable")
    value = getter()
    if type(value) is not str:
        raise RuntimeError("ANSA DataBaseName did not return a path string")
    return value


def _backup_record(path, saved=None):
    fingerprint = model_files._fingerprint(path)
    record = {
        "path": str(path), "saved": True, "verified": True, "exists": True,
        **fingerprint,
    }
    if type(saved) is dict and type(saved.get("warnings")) is list:
        record["warnings"] = [item for item in saved["warnings"] if type(item) is str]
    return record


def _unconfirmed_backup(path):
    record = {
        "path": str(path), "saved": False, "verified": False,
        "exists": None, "observation_errors": [],
    }
    try:
        path.lstat()
    except FileNotFoundError:
        record["exists"] = False
    except Exception as exc:
        record["observation_errors"].append(_error(exc))
    else:
        record["exists"] = True
        try:
            record.update(model_files._public_file_record(model_files._fingerprint(path)))
        except Exception as exc:
            record["observation_errors"].append(_error(exc))
    return record


def _verify_backup(base, constants, backup_path, current_path, current_fingerprint):
    saved = _model_save(base, constants, {"path": str(backup_path), "overwrite": False})
    if type(saved) is not dict or saved.get("saved") is not True:
        raise RuntimeError("model save helper did not confirm the requested backup")
    saved_path = saved.get("path")
    if type(saved_path) is not str or not model_files._same_path(saved_path, backup_path):
        raise RuntimeError("model save helper returned a different backup path")

    backup = _backup_record(backup_path, saved)
    if not model_files._same_path(current_path, _database_name(base)):
        raise RuntimeError("SaveAs changed the current database path")
    current_after_fingerprint = model_files._fingerprint(Path(current_path))
    if not catalog._strict_equal(current_fingerprint, current_after_fingerprint):
        raise RuntimeError("SaveAs changed the current database file")
    try:
        if os.path.samefile(backup_path, Path(current_path)):
            raise RuntimeError("backup resolves to the current database file")
    except OSError as exc:
        raise RuntimeError(f"could not verify backup identity: {exc}") from exc
    return backup


def _volume_transition_errors(before, after, volume_id):
    errors = []
    before_rows, after_rows = _row_map(before["volumes"]), _row_map(after["volumes"])
    if before_rows.keys() != after_rows.keys():
        return ["VOLUME identity set changed"]
    for entity_id in sorted(before_rows):
        left, right = before_rows[entity_id], after_rows[entity_id]
        if entity_id != volume_id:
            if not catalog._strict_equal(left, right):
                errors.append(f"unselected VOLUME {entity_id} changed")
            continue

        left_labels, right_labels = left["card_fields"], right["card_fields"]
        left_fields, right_fields = left["fields"], right["fields"]
        left_stable = [name for name in left_labels if _field_key(name) not in _VOLUME_DERIVED_FIELDS
                       and name not in _NEW_MESH_SETTINGS]
        right_stable = [name for name in right_labels if _field_key(name) not in _VOLUME_DERIVED_FIELDS
                        and name not in _NEW_MESH_SETTINGS]
        if left_stable != right_stable:
            errors.append("selected VOLUME stable card field set changed")
        for name in left_stable:
            if name not in left_fields or name not in right_fields or not catalog._strict_equal(
                left_fields.get(name), right_fields.get(name)
            ):
                errors.append(f"selected VOLUME stable field {name} changed")
        for name in _NEW_MESH_SETTINGS:
            if name in left_fields:
                if name not in right_fields or not catalog._strict_equal(left_fields[name], right_fields[name]):
                    errors.append(f"existing VOLUME mesh setting {name} changed")
            elif name in right_fields and not catalog._strict_equal(
                right_fields[name], _NEW_MESH_SETTINGS[name]
            ):
                errors.append(f"new VOLUME mesh setting {name} differs from the observed default")
        allowed = _VOLUME_DERIVED_FIELDS | {_field_key(name) for name in _NEW_MESH_SETTINGS}
        unexpected = [name for name in right_labels if _field_key(name) not in allowed
                      and name not in left_labels]
        if unexpected:
            errors.append("selected VOLUME gained unverified card field(s): " + ", ".join(unexpected))
    return errors


def _tetra_volume(a, b, c, d):
    return _dot(_subtract(b, a), _cross(_subtract(c, a), _subtract(d, a))) / 6.0


def _validate_pyramid_base(node_ids, coords, scale, tolerance, label):
    points = [coords[node_id] for node_id in node_ids]
    for index in range(4):
        if _norm(_subtract(points[(index + 1) % 4], points[index])) <= tolerance:
            raise ValueError(f"{label} has a zero-length base edge")
    normal_a = _cross(_subtract(points[1], points[0]), _subtract(points[2], points[0]))
    normal_b = _cross(_subtract(points[2], points[0]), _subtract(points[3], points[0]))
    normal_length = _norm(normal_a)
    if normal_length <= scale * scale * _TETRA_REL_TOL or _norm(normal_b) <= scale * scale * _TETRA_REL_TOL:
        raise ValueError(f"{label} has a degenerate quadrilateral base")
    if _dot(normal_a, normal_b) <= 0:
        raise ValueError(f"{label} base node order is crossed or concave")
    plane_normal = tuple(value / normal_length for value in normal_a)
    distance = abs(_dot(_subtract(points[3], points[0]), plane_normal))
    if distance > tolerance:
        raise ValueError(f"{label} base nodes are not coplanar")


def _validate_mesh_topology(cells, coords, before, shell_ids):
    """Require a conforming cell complex with exactly the six source faces."""
    source_shells = _row_map(before["shell_cards"])
    source_quads = {
        shell_id: tuple(source_shells[shell_id]["fields"][name] for name in _FACE_NODE_FIELDS)
        for shell_id in shell_ids
    }
    source_nodes = {node for quad in source_quads.values() for node in quad}
    connectivity = set()
    face_uses = {}
    neighbors = {entity_id: set() for entity_id, _ in cells}
    for entity_id, nodes in cells:
        canonical = tuple(sorted(nodes))
        if canonical in connectivity:
            raise ValueError(f"duplicate SOLID connectivity at SOLID {entity_id}")
        connectivity.add(canonical)
        origin = coords[nodes[0]]
        center = tuple(
            origin[axis] + sum(coords[node][axis] - origin[axis] for node in nodes) / len(nodes)
            for axis in range(3)
        )
        face_indices = (
            ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))
            if len(nodes) == 4 else
            ((0, 1, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4))
        )
        for indices in face_indices:
            face = tuple(nodes[index] for index in indices)
            face_uses.setdefault(tuple(sorted(face)), []).append((entity_id, face, center))

    boundary = {shell_id: set() for shell_id in shell_ids}
    interior_count = 0
    for canonical, uses in face_uses.items():
        if len(uses) > 2:
            raise ValueError("new mesh has a non-manifold face shared by more than two SOLIDs")
        if len(uses) == 2:
            first, second = uses
            points = [coords[node] for node in first[1][:3]]
            normal = _cross(_subtract(points[1], points[0]), _subtract(points[2], points[0]))
            sides = [_dot(normal, _subtract(use[2], points[0])) for use in uses]
            if (any(not math.isfinite(side) or side == 0 for side in sides)
                    or (sides[0] > 0) == (sides[1] > 0)):
                raise ValueError("new mesh has overlapping SOLIDs on the same side of an internal face")
            neighbors[first[0]].add(second[0])
            neighbors[second[0]].add(first[0])
            interior_count += 1
            continue

        if not set(canonical).issubset(source_nodes):
            raise ValueError(
                "new mesh has an unsupported skin node or an unmatched internal face (internal cavity)"
            )
        matches = [shell_id for shell_id, quad in source_quads.items()
                   if set(canonical).issubset(quad)]
        if len(matches) != 1:
            raise ValueError("new mesh has an exterior face outside the six source quads (internal cavity)")
        boundary[matches[0]].add(canonical)

    for shell_id, quad in source_quads.items():
        a, b, c, d = quad
        allowed = (
            {tuple(sorted(quad))},
            {tuple(sorted((a, b, c))), tuple(sorted((a, c, d)))},
            {tuple(sorted((a, b, d))), tuple(sorted((b, c, d)))},
        )
        if boundary[shell_id] not in allowed:
            raise ValueError(
                f"new mesh exterior does not exactly cover source SHELL {shell_id} "
                "with one quad or two triangles on a legal diagonal"
            )

    reached = set()
    pending = [cells[0][0]]
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(neighbors[current] - reached)
    if reached != set(neighbors):
        raise ValueError("new SOLID entities are not connected through shared faces")
    return {
        "interior_face_count": interior_count,
        "exterior_face_count": sum(len(faces) for faces in boundary.values()),
        "source_quad_count": len(source_quads),
    }


def _validate_new_solids(before, after, preflight, params):
    before_solids = _row_map(before["solid_cards"])
    after_solids = _row_map(after["solid_cards"])
    if before_solids:
        raise ValueError("pre-existing SOLID entities prevent a complete first-batch readback")
    if not after_solids:
        raise ValueError("VolumesMeshV created no SOLID entities")

    grid_rows = _row_map(after["grids"])
    coords = {}

    def coordinate(node_id):
        if node_id not in grid_rows:
            raise RuntimeError(f"SOLID references missing GRID {node_id}")
        if node_id not in coords:
            coords[node_id] = _coord(grid_rows[node_id], f"GRID {node_id}")
        return coords[node_id]

    geometry = preflight["geometry"]
    shell_node_ids = set(geometry["corner_node_ids"])
    used_nodes = set()
    cells = []
    tetra_count = pyramid_count = 0
    total_volume = 0.0
    volume_floor = max(geometry["volume"] * _TETRA_REL_TOL,
                       geometry["scale"] ** 3 * _TETRA_REL_TOL * 1e-3)
    for entity_id, row in after_solids.items():
        fields = row["fields"]
        labels = set(row["card_fields"])
        required = {"type", "EID", "PID", *_SOLID_NODE_FIELDS}
        missing = required - labels
        if missing:
            raise NotImplementedError(
                f"SOLID {entity_id} card lacks required field(s): {', '.join(sorted(missing))}"
            )
        if fields.get("EID") != entity_id:
            raise RuntimeError(f"SOLID {entity_id} EID does not match its entity ID")
        if fields.get("PID") != preflight["property_id"]:
            raise ValueError(f"SOLID {entity_id} does not use the selected PSOLID")

        kind = fields["type"].strip().upper() if type(fields.get("type")) is str else ""
        first_four = [_positive_int(fields[name], f"SOLID {entity_id} {name}")
                      for name in _FACE_NODE_FIELDS]
        if len(set(first_four)) != 4:
            raise ValueError(f"SOLID {entity_id} has repeated G1..G4 node IDs")
        for name in ("G6", "G7", "G8", "G9", "G10"):
            if name in fields and fields[name] not in (None, "", 0):
                raise ValueError(f"SOLID {entity_id} is not a first-order element")

        if kind in {"TETRA", "CTETRA"}:
            if fields["G5"] not in (None, "", 0):
                raise ValueError(f"SOLID {entity_id} has a fifth node but is typed as a first-order tetra")
            node_ids = first_four
            tetra_count += 1
            points = [coordinate(node_id) for node_id in node_ids]
            element_volume = _tetra_volume(*points)
            if not math.isfinite(element_volume) or element_volume <= volume_floor:
                raise ValueError(f"SOLID {entity_id} has non-positive or degenerate signed tetra volume")
            total_volume += element_volume
        elif kind == "PYRAMID":
            apex = _positive_int(fields["G5"], f"SOLID {entity_id} G5")
            node_ids = [*first_four, apex]
            if len(set(node_ids)) != 5:
                raise ValueError(f"SOLID {entity_id} has repeated pyramid node IDs")
            for node_id in node_ids:
                coordinate(node_id)
            _validate_pyramid_base(first_four, coords, geometry["scale"],
                                   geometry["coord_tolerance"], f"SOLID {entity_id}")
            a, b, c, d, top = (coords[node_id] for node_id in node_ids)
            first_volume = _tetra_volume(a, b, c, top)
            second_volume = _tetra_volume(a, c, d, top)
            if (not math.isfinite(first_volume) or not math.isfinite(second_volume)
                    or first_volume <= volume_floor or second_volume <= volume_floor):
                raise ValueError(f"SOLID {entity_id} has non-positive or degenerate signed pyramid volume")
            total_volume += first_volume + second_volume
            pyramid_count += 1
        else:
            raise ValueError(f"SOLID {entity_id} type {kind!r} is outside the supported TETRA/PYRAMID scope")

        used_nodes.update(node_ids)
        cells.append((entity_id, node_ids))
        for node_id in node_ids:
            point = coordinate(node_id)
            if any(point[axis] < geometry["mins"][axis] - geometry["coord_tolerance"]
                   or point[axis] > geometry["maxs"][axis] + geometry["coord_tolerance"]
                   for axis in range(3)):
                raise ValueError(f"SOLID {entity_id} GRID {node_id} lies outside the boundary box")

    if not shell_node_ids.issubset(used_nodes):
        raise ValueError("new SOLID entities do not use every source boundary corner GRID")
    topology = _validate_mesh_topology(cells, coords, before, params["expected_shell_ids"])
    new_grid_ids = grid_rows.keys() - _row_map(before["grids"]).keys()
    if not new_grid_ids.issubset(used_nodes):
        raise ValueError("one or more new GRID entities are unused by the new SOLID entities")
    if abs(total_volume - geometry["volume"]) > max(
        geometry["volume"] * _VOLUME_REL_TOL,
        geometry["scale"] ** 3 * _TETRA_REL_TOL,
    ):
        raise ValueError("sum of new TETRA/PYRAMID volumes does not match the boundary-box volume")

    volume = _row_map(after["volumes"]).get(params["volume_id"])
    if volume is None:
        raise RuntimeError("selected VOLUME disappeared during mesh readback")
    fields = volume["fields"]
    counts = {
        "Tetras": tetra_count,
        "Pyramids": pyramid_count,
        "Pentas": 0,
        "Hexas": 0,
        "Polyhedrals": 0,
    }
    if not tetra_count and not pyramid_count:
        raise ValueError("no supported first-order TETRA or PYRAMID elements were created")
    if fields.get("Type") != "Tetra Rapid" or fields.get("Order") != "1st" or fields.get("Status") != "Meshed":
        raise ValueError("VOLUME readback does not report Tetra Rapid, first order, and Meshed")
    for name, expected in counts.items():
        if type(fields.get(name)) is not int or fields[name] != expected:
            raise ValueError(f"VOLUME {name} count does not match the SOLID readback")
    unique_solid_nodes = len(used_nodes)
    for name, expected in (("Grids", unique_solid_nodes), ("Internal grids", len(new_grid_ids))):
        if type(fields.get(name)) is not int or fields[name] != expected:
            raise ValueError(f"VOLUME {name} count does not match the SOLID/GRID readback")
    reported_volume = _finite_number(fields.get("Volume"), "VOLUME Volume")
    if abs(reported_volume - geometry["volume"]) > max(
        geometry["volume"] * _VOLUME_REL_TOL,
        geometry["scale"] ** 3 * _TETRA_REL_TOL,
    ):
        raise ValueError("VOLUME reported size does not match the boundary-box volume")

    return {
        "solid_count": len(after_solids),
        "solid_type_counts": {"TETRA": tetra_count, "PYRAMID": pyramid_count},
        "new_grid_ids": sorted(new_grid_ids),
        "used_grid_ids": sorted(used_nodes),
        "tetra_volume_sum": total_volume,
        "boundary_volume": geometry["volume"],
        "volume_counts": counts,
        "mesh_topology": topology,
    }


def _scope_errors(before, after, params, preflight, output):
    errors = []
    before_all = {(item["type"], item["id"]) for item in before["entity_references"]}
    after_all = {(item["type"], item["id"]) for item in after["entity_references"]}
    before_solids = {(item["type"], item["id"]) for item in before["fe_references"]
                     if item["type"] == "SOLID"}
    if before_solids:
        errors.append("preflight model had pre-existing SOLID entities")
    new_solids = {(row["entity"]["type"], row["entity"]["id"])
                  for row in after["solid_cards"]}
    before_grids = _row_map(before["grids"])
    after_grids = _row_map(after["grids"])
    new_grid_ids = set(after_grids) - set(before_grids)
    if set(before_grids) - set(after_grids):
        errors.append("one or more original GRID entities were removed")
    for entity_id, row in before_grids.items():
        if entity_id in after_grids and not catalog._strict_equal(row, after_grids[entity_id]):
            errors.append(f"original GRID {entity_id} stable card changed")
    expected_all = before_all | new_solids | {("GRID", entity_id) for entity_id in new_grid_ids}
    if after_all != expected_all:
        errors.append("model entity references changed outside new SOLID and GRID entities")

    before_fe = {(item["type"], item["id"]) for item in before["fe_references"]}
    after_fe = {(item["type"], item["id"]) for item in after["fe_references"]}
    if after_fe != before_fe | new_solids:
        errors.append("FE references changed outside the new SOLID entities")
    after_fe_cards = {
        (row["entity"]["type"], row["entity"]["id"]): row for row in after["fe_cards"]
    }
    for row in before["fe_cards"]:
        reference = row["entity"]
        key = (reference["type"], reference["id"])
        if not catalog._strict_equal(row, after_fe_cards.get(key)):
            errors.append(f"original {key[0]} {key[1]} stable FE card changed")
    if not catalog._strict_equal(before["shell_cards"], after["shell_cards"]):
        errors.append("one or more original SHELL cards changed")
    expected_boundary = sorted(params["expected_shell_ids"])
    if [item["id"] for item in after["boundary_shell_references"]] != expected_boundary:
        errors.append("selected VOLUME boundary SHELL membership changed")
    if before["property_references"] != after["property_references"]:
        errors.append("property references changed during volume meshing")
    if not catalog._strict_equal(before["property_cards"], after["property_cards"]):
        errors.append("property definitions changed outside derived element counts")
    if before["material_references"] != after["material_references"]:
        errors.append("material references changed during volume meshing")
    if not catalog._strict_equal(before["psolid_cards"], after["psolid_cards"]):
        errors.append("PSOLID definitions changed outside derived element counts")
    if not catalog._strict_equal(before["mat1_cards"], after["mat1_cards"]):
        errors.append("MAT1 definitions changed")
    errors.extend(_volume_transition_errors(before, after, params["volume_id"]))
    if output is not None:
        if set(output["new_grid_ids"]) != new_grid_ids:
            errors.append("new GRID readback changed while checking SOLID ownership")
        if not set(output["used_grid_ids"]).issubset(set(after_grids)):
            errors.append("new SOLID GRID references are absent from the complete GRID collection")
    return errors


def _failed_result(params, deck, backup, before, after, errors, *,
                   possible_effects, api_status=None, api_error=None,
                   model_scope_matches=None, output=None):
    details = [str(item)[:2048] for item in errors[:16]]
    if api_error:
        details.insert(0, str(api_error)[:2048])
    result = {
        "ok": False,
        "tool": "mesh_volume",
        "deck": deck,
        "volume_id": params["volume_id"],
        "expected_shell_ids": params["expected_shell_ids"],
        "backup": backup,
        "before": _evidence(before),
        "after": _evidence(after),
        "scope_verified": False,
        "model_scope_matches": model_scope_matches,
        "possible_effects": possible_effects,
        "api_return_code": api_status if type(api_status) is int else None,
        "api_return_type": type(api_status).__name__ if api_status is not None else None,
        "api_error": str(api_error)[:2048] if api_error else None,
        "mesh_readback": output,
        "error": "; ".join(details) if details else "volume meshing was not verified",
        "errors": details,
    }
    return _bounded_result(params, result)


def execute(state, tool, params):
    """Mesh one existing NASTRAN VOLUME and independently verify its full model scope."""
    state.assert_main()
    if type(tool) is not str or tool != "mesh_volume":
        raise ValueError(f"unsupported volume meshing operation: {tool!r}")
    params = _validate_inputs(params)

    from ansa import base, constants, mesh

    deck = catalog._deck(base, params)
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int or deck != nastran:
        raise ValueError("single-volume meshing is supported only in the NASTRAN deck")
    if not callable(getattr(mesh, "VolumesMeshV", None)):
        raise NotImplementedError("ANSA mesh.VolumesMeshV is unavailable")
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    context = _execution_context(state, deck)

    backup_path, _prior = _save_path(params["backup_path"], False)
    current_path = _database_name(base)
    if not current_path:
        raise ValueError("the current model must have a saved database path for backup verification")
    if model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_file = Path(current_path)
    if not current_file.is_file():
        raise ValueError("the current database file is unavailable for fingerprint verification")
    current_fingerprint = model_files._fingerprint(current_file)

    before, _volume_entity = _capture_snapshot(
        base, deck, params["volume_id"], params["expected_shell_ids"]
    )
    preflight = _validate_preconditions(before, params)
    if len(_encoded(before)) > _SNAPSHOT_BUDGET:
        return _failed_result(
            params, deck, None, before, None,
            ["preflight snapshot exceeds the 256 KiB evidence budget; no backup or mesh call was made"],
            possible_effects=False,
        )
    _same_context(state, deck, context)

    backup = None
    try:
        backup = _verify_backup(
            base, constants, backup_path, current_path, current_fingerprint
        )
    except Exception as exc:
        backup = _unconfirmed_backup(backup_path)
        try:
            after_save, _ = _capture_snapshot(
                base, deck, params["volume_id"], params["expected_shell_ids"]
            )
        except Exception as read_exc:
            after_save = None
            read_error = _error(read_exc)
        else:
            read_error = None
        possible = after_save is None
        try:
            if after_save is not None:
                possible = not catalog._strict_equal(before, after_save)
            current_after = _database_name(base)
            possible = possible or not model_files._same_path(current_path, current_after)
            possible = possible or not catalog._strict_equal(
                current_fingerprint, model_files._fingerprint(Path(current_path))
            )
            _same_context(state, deck, context)
        except Exception:
            possible = True
        details = [_error(exc)]
        if read_error:
            details.append("post-save readback: " + read_error)
        return _failed_result(
            params, deck, backup, before, after_save, details,
            possible_effects=possible,
        )

    try:
        _same_context(state, deck, context)
        after_backup, volume_entity = _capture_snapshot(
            base, deck, params["volume_id"], params["expected_shell_ids"]
        )
        _validate_preconditions(after_backup, params)
        _same_context(state, deck, context)
    except Exception as exc:
        return _failed_result(
            params, deck, backup, before, locals().get("after_backup"),
            [_error(exc)], possible_effects=True,
        )
    if len(_encoded(after_backup)) > _SNAPSHOT_BUDGET:
        return _failed_result(
            params, deck, backup, before, after_backup,
            ["post-backup snapshot exceeds the 256 KiB evidence budget; mesh call was not made"],
            possible_effects=not catalog._strict_equal(before, after_backup),
            model_scope_matches=catalog._strict_equal(before, after_backup),
        )
    if not catalog._strict_equal(before, after_backup):
        return _failed_result(
            params, deck, backup, before, after_backup,
            ["model snapshot changed after backup and before VolumesMeshV"],
            possible_effects=True,
            model_scope_matches=False,
        )

    try:
        _same_context(state, deck, context)
    except Exception as exc:
        return _failed_result(
            params, deck, backup, before, after_backup, [_error(exc)],
            possible_effects=True, model_scope_matches=False,
        )

    api_status = None
    api_error = None
    try:
        api_status = mesh.VolumesMeshV(volume_entity, "TETRA RAPID")
    except Exception as exc:
        api_error = _error(exc)

    try:
        after, _ = _capture_snapshot(
            base, deck, params["volume_id"], params["expected_shell_ids"]
        )
        readback_error = None
    except Exception as exc:
        after = None
        readback_error = _error(exc)
    try:
        _same_context(state, deck, context)
        context_error = None
    except Exception as exc:
        context_error = _error(exc)

    output = None
    scope_errors = []
    if after is None:
        scope_errors.append("post-mesh full model readback is unknown")
    else:
        try:
            output = _validate_new_solids(before, after, preflight, params)
        except Exception as exc:
            scope_errors.append(_error(exc))
        if output is not None:
            scope_errors.extend(_scope_errors(before, after, params, preflight, output))
        else:
            scope_errors.extend(_scope_errors(before, after, params, preflight, None))
    if readback_error:
        scope_errors.append("post-mesh readback: " + readback_error)
    if context_error:
        scope_errors.append("post-mesh context: " + context_error)
    if after is not None and len(_encoded(after)) > _SNAPSHOT_BUDGET:
        scope_errors.append("post-mesh snapshot exceeds the 256 KiB evidence budget")

    model_scope_matches = not scope_errors
    api_ok = api_error is None and type(api_status) is int and api_status == 1
    if not api_ok:
        if api_error is None:
            scope_errors.insert(0, "VolumesMeshV did not return the exact success value int 1")
        return _failed_result(
            params, deck, backup, before, after, scope_errors,
            possible_effects=True,
            api_status=api_status,
            api_error=api_error,
            model_scope_matches=model_scope_matches,
            output=output,
        )
    if not model_scope_matches:
        return _failed_result(
            params, deck, backup, before, after, scope_errors,
            possible_effects=True,
            api_status=api_status,
            model_scope_matches=False,
            output=output,
        )

    return _bounded_result(params, {
        "ok": True,
        "tool": "mesh_volume",
        "deck": deck,
        "volume_id": params["volume_id"],
        "expected_shell_ids": params["expected_shell_ids"],
        "psolid_id": preflight["property_id"],
        "mat1_id": preflight["material_id"],
        "backup": backup,
        "before": _evidence(before),
        "after": _evidence(after),
        "scope_verified": True,
        "model_scope_matches": True,
        "possible_effects": False,
        "api_return_code": 1,
        "api_return_type": "int",
        "mesh_readback": output,
        "error": None,
        "errors": [],
    })


__all__ = ["execute"]
