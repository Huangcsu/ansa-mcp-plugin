"""Bounded native geometry partition and NASTRAN shell sweep operations."""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
from pathlib import Path
import re

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import entity_catalog as catalog
from . import geometry_delete, inspection, model_files, meshing, volume_meshing


_MAX_PARTITION_FACES = 100
_MAX_PROTECTED_FACES = 1000
_MAX_SWEEP_SHELLS = 200
_MAX_SOURCE_CAPTURE_ELEMENTS = 400
_MAX_NEW_SOLIDS = 500
_MAX_NEW_VOLUMES = 500
_MAX_VOLUME_BOUNDARY_ENTITIES = 400
_MAX_TOTAL_VOLUME_BOUNDARY_ENTITIES = 4000
_MAX_CAPTURE_ELEMENTS = 5000
_MAX_MODEL_ENTITIES = 10000
_SCOPE_EVIDENCE_MAX_BYTES_ENV = "ANSA_MCP_SCOPE_EVIDENCE_MAX_BYTES"
_SCOPE_EVIDENCE_MIN_BUDGET = 256 * 1024
_SNAPSHOT_BUDGET = 16 * 1024 * 1024
_SCOPE_EVIDENCE_MAX_BUDGET = 64 * 1024 * 1024
_SWEEP_SNAPSHOT_BUDGET = MAX_FRAME_BYTES * 2
_CAPTURE_BUDGET = MAX_FRAME_BYTES // 2
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GRID_FIELDS = ("NID", "CP", "X1", "X2", "X3")
_PART_DERIVED_FIELDS = ("Last Edit", "Ixx", "Iyy", "Izz", "Ixy", "Ixz", "Iyz")
_PART_AGGREGATE_FIELDS = frozenset(("Mass", "Area", "COG x", "COG y", "COG z", "Diagonal",
    "Min x", "Min y", "Min z", "Max x", "Max y", "Max z", "Size x", "Size y", "Size z", "Size min", "Size max",
    "PIDs Number", "PID", "Thickness", "Properties Info", "Material Name", "Materials Info"))


def _params(value, required, optional=()):
    return inspection._params(value, required, optional)


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _scope_evidence_limit(budget):
    if budget is not None:
        return budget
    raw = os.environ.get(_SCOPE_EVIDENCE_MAX_BYTES_ENV)
    if raw is None:
        return _SNAPSHOT_BUDGET
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise ValueError(
            f"{_SCOPE_EVIDENCE_MAX_BYTES_ENV} must be an integer byte count "
            f"from {_SCOPE_EVIDENCE_MIN_BUDGET} to {_SCOPE_EVIDENCE_MAX_BUDGET}; got {raw!r}"
        )
    limit = int(raw)
    if not _SCOPE_EVIDENCE_MIN_BUDGET <= limit <= _SCOPE_EVIDENCE_MAX_BUDGET:
        raise ValueError(
            f"{_SCOPE_EVIDENCE_MAX_BYTES_ENV} must be an integer byte count "
            f"from {_SCOPE_EVIDENCE_MIN_BUDGET} to {_SCOPE_EVIDENCE_MAX_BUDGET}; got {raw!r}"
        )
    return limit


def _id_list(value, label, maximum, *, allow_empty=False):
    return inspection._id_list(value, label, maximum, allow_empty=allow_empty)


def _point(value, label):
    if type(value) is not list or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three finite numbers")
    return [_finite(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _unit_vector(value, label):
    point = _point(value, label)
    scale = max(abs(item) for item in point)
    if scale == 0:
        raise ValueError(f"{label} must be non-zero")
    scaled = [item / scale for item in point]
    magnitude = math.hypot(*scaled)
    if not math.isfinite(magnitude) or magnitude == 0:
        raise ValueError(f"{label} must have a finite non-zero length")
    return [item / magnitude for item in scaled]


def _hash(value):
    return hashlib.sha256(volume_meshing._encoded(value)).hexdigest()


def _validate_digest(value, label):
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 64-character lowercase SHA-256 digest")
    return value


def _normalize_partition(params, *, applying):
    required = {"deck", "face_ids", "plane"}
    optional = {"protected_face_ids", "produce_plane_faces"}
    if applying:
        required.update(("expected_plan_sha256", "backup_path"))
    _params(params, required, optional)
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")
    face_ids = sorted(_id_list(params["face_ids"], "face_ids", _MAX_PARTITION_FACES))
    protected_value = params.get("protected_face_ids")
    if protected_value is None:
        protected_value = []
    protected_ids = sorted(_id_list(
        protected_value, "protected_face_ids", _MAX_PROTECTED_FACES, allow_empty=True
    ))
    overlap = sorted(set(face_ids) & set(protected_ids))
    if overlap:
        raise ValueError("protected_face_ids must not overlap face_ids")
    plane = params["plane"]
    _params(plane, ("origin", "normal"))
    normalized_plane = {
        "origin": _point(plane["origin"], "plane.origin"),
        "normal": _unit_vector(plane["normal"], "plane.normal"),
    }
    produce = params.get("produce_plane_faces", False)
    if type(produce) is not bool:
        raise ValueError("produce_plane_faces must be a boolean")
    result = {
        "deck": params["deck"],
        "face_ids": face_ids,
        "plane": normalized_plane,
        "protected_face_ids": protected_ids,
        "produce_plane_faces": produce,
    }
    if applying:
        result["expected_plan_sha256"] = _validate_digest(
            params["expected_plan_sha256"], "expected_plan_sha256"
        )
        if type(params["backup_path"]) is not str or not params["backup_path"]:
            raise TypeError("backup_path must be a non-empty absolute .ansa path")
        result["backup_path"] = params["backup_path"]
    return result


def _normalize_sweep(params):
    _params(params, (
        "deck", "shell_ids", "direction", "distance", "steps", "property_id",
        "expected_source_sha256", "backup_path",
    ), ("freeze_source",))
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")
    shell_ids = sorted(_id_list(params["shell_ids"], "shell_ids", _MAX_SWEEP_SHELLS))
    distance = _finite(params["distance"], "distance")
    if distance <= 0:
        raise ValueError("distance must be positive")
    steps = params["steps"]
    if type(steps) is not int or not 1 <= steps <= 100:
        raise ValueError("steps must be an integer between 1 and 100")
    if len(shell_ids) * steps > _MAX_NEW_SOLIDS:
        raise ValueError(f"sweep may create at most {_MAX_NEW_SOLIDS} SOLID elements")
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")
    freeze_source = params.get("freeze_source", False)
    if type(freeze_source) is not bool:
        raise ValueError("freeze_source must be a boolean")
    return {
        "deck": params["deck"],
        "shell_ids": shell_ids,
        "direction": _unit_vector(params["direction"], "direction"),
        "distance": distance,
        "steps": steps,
        "property_id": _positive_int(params["property_id"], "property_id"),
        "expected_source_sha256": _validate_digest(
            params["expected_source_sha256"], "expected_source_sha256"
        ),
        "backup_path": params["backup_path"],
        "freeze_source": freeze_source,
    }


def _check_nastran(constants, deck):
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int or deck != nastran:
        raise ValueError("structured native operations are supported only in the NASTRAN deck")


def _card(base, deck, entity, reference, label, required_fields=()):
    return volume_meshing._read_full_card(
        base, deck, entity, reference, label, omit_derived=True,
        required_fields=required_fields,
    )


def _read_elements(base, deck, element_type, entity_ids, allowed_types):
    rows = []
    for entity_id in sorted(entity_ids):
        entity, reference = catalog._get_entity(base, deck, element_type, entity_id)
        if reference["id"] != entity_id or reference["type"] != element_type:
            raise RuntimeError(f"{element_type} {entity_id} identity did not match its explicit lookup")
        labels = volume_meshing._card_fields(entity, deck, f"{element_type} {entity_id}")
        if "type" not in labels:
            raise NotImplementedError(f"{element_type} {entity_id} has no readable card type field")
        card_type = catalog._read_card(base, deck, entity, reference, labels)
        kind = card_type.get("type")
        if kind not in allowed_types:
            raise ValueError(
                f"{element_type} {entity_id} type {kind!r} is outside the supported linear element set"
            )
        node_count = {"CTRIA3": 3, "CQUAD4": 4, "CPENTA": 6, "CHEXA": 8}[kind]
        required = ("EID", "PID", "type", *(f"G{i}" for i in range(1, node_count + 1)))
        missing = sorted(set(required) - set(labels))
        if missing:
            raise NotImplementedError(
                f"{element_type} {entity_id} lacks required card field(s): {', '.join(missing)}"
            )
        card = _card(base, deck, entity, reference, f"{element_type} {entity_id}", required)
        fields = card["fields"]
        if _positive_int(fields["EID"], f"{element_type} {entity_id} EID") != entity_id:
            raise RuntimeError(f"{element_type} {entity_id} card EID does not match its entity ID")
        node_ids = [
            _positive_int(fields[f"G{index}"], f"{element_type} {entity_id} G{index}")
            for index in range(1, node_count + 1)
        ]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(f"{element_type} {entity_id} has repeated GRID IDs")
        rows.append({
            "id": entity_id,
            "type": kind,
            "nodes": node_ids,
            "property_id": _positive_int(fields["PID"], f"{element_type} {entity_id} PID"),
            "block_id": None,
        })
    return rows


def _read_grids(base, deck, node_ids):
    rows = []
    for node_id in sorted(node_ids):
        entity, reference = catalog._get_entity(base, deck, "GRID", node_id)
        if reference["id"] != node_id or reference["type"] != "GRID":
            raise RuntimeError(f"GRID {node_id} identity did not match its explicit lookup")
        card = _card(base, deck, entity, reference, f"GRID {node_id}", _GRID_FIELDS)
        fields = card["fields"]
        if _positive_int(fields["NID"], f"GRID {node_id} NID") != node_id:
            raise RuntimeError(f"GRID {node_id} card NID does not match its entity ID")
        cp = fields["CP"]
        if not (cp is None or cp == "" or (type(cp) in (int, float) and cp == 0)):
            raise ValueError(f"GRID {node_id} must use NASTRAN basic coordinate system CP=0")
        xyz = [_finite(fields[name], f"GRID {node_id} {name}") for name in ("X1", "X2", "X3")]
        rows.append({"id": node_id, "xyz": xyz})
    return rows


def _capture_elements(base, constants, deck, entity_ids, *, source, collect_ownership=True):
    _check_nastran(constants, deck)
    maximum = _MAX_SOURCE_CAPTURE_ELEMENTS if source else _MAX_CAPTURE_ELEMENTS
    ids = sorted(_id_list(entity_ids, "shell_ids" if source else "entity_ids", maximum))
    allowed = {"CTRIA3", "CQUAD4"} if source else {"CHEXA", "CPENTA"}
    rows = _read_elements(base, deck, "SHELL" if source else "SOLID", ids, allowed)
    ownership = {"origin": "not_applicable" if source else "native_VOLUME_membership",
                 "complete": False, "known_count": 0}
    if not source and collect_ownership:
        volumes = base.CollectEntities(deck, None, "VOLUME")
        if type(volumes) in (list, tuple) and len(volumes) <= 200:
            candidates = {entity_id: [] for entity_id in ids}
            for volume in volumes:
                members = base.CollectEntities(deck, volume, "SOLID")
                if type(members) not in (list, tuple):
                    raise RuntimeError("native VOLUME/SOLID membership is unreadable")
                for member in members:
                    if member._id in candidates:
                        candidates[member._id].append(volume._id)
            for row in rows:
                owners = candidates[row["id"]]
                if len(owners) == 1:
                    row["block_id"] = "VOLUME:" + str(owners[0])
            ownership["known_count"] = sum(row["block_id"] is not None for row in rows)
            ownership["complete"] = ownership["known_count"] == len(rows)
        else:
            ownership["reason"] = "VOLUME collection unavailable or exceeds 200-volume budget"
    node_ids = {node_id for row in rows for node_id in row["nodes"]}
    nodes = _read_grids(base, deck, node_ids)
    element_rows = rows
    body = {
        "deck": deck,
        "complete": True,
        "nodes": nodes,
        "elements": element_rows,
        "integrity": {
            "complete": True,
            "requested_element_count": len(ids),
            "element_count": len(rows),
            "node_count": len(nodes),
            "missing_node_ids": [],
            "element_types": dict(sorted(Counter(row["type"] for row in rows).items())),
        },
        "ownership": ownership,
    }
    encoded = volume_meshing._encoded(body)
    if len(encoded) > _CAPTURE_BUDGET:
        raise ValueError("mesh capture exceeds the 512 KiB evidence budget")
    return {**body, "snapshot_sha256": hashlib.sha256(encoded).hexdigest()}


def capture_mesh(base, constants, deck, entity_ids, *, collect_ownership=True):
    """Return explicit NASTRAN CHEXA/CPENTA cards and their basic-coordinate GRID rows."""
    if type(collect_ownership) is not bool:
        raise ValueError("collect_ownership must be a boolean")
    return _capture_elements(base, constants, deck, entity_ids, source=False,
                             collect_ownership=collect_ownership)


def capture_source(base, constants, deck, shell_ids):
    """Return an auditable NASTRAN CTRIA3/CQUAD4 source snapshot for sweep planning."""
    return _capture_elements(base, constants, deck, shell_ids, source=True)


def _source_interface_issues(snapshot):
    """共享 GRID 不足以证明共形：显式拦截粗边内部的悬挂节点。"""
    points={row["id"]:row["xyz"] for row in snapshot["nodes"]}
    edges=Counter()
    for row in snapshot["elements"]:
        ids=row["nodes"]
        edges.update(tuple(sorted((a,b))) for a,b in zip(ids,ids[1:]+ids[:1]))
    issues=[];hanging=[];uncertain=[]
    for (a,b),owners in sorted(edges.items()):
        start=points[a];end=points[b];length=math.dist(start,end)
        if not length:continue  # 单块源面审计另行拒绝退化边。
        # 仅吸收该边自身的浮点舍入误差，不用全模型跨度吞掉细节。
        tolerance=max(length*1e-10,8*max(math.ulp(v) for p in (start,end) for v in p))
        if tolerance>length*1e-7:
            uncertain.append([a,b]);continue
        vector=[(end[k]-start[k])/length for k in range(3)]
        for node,p in sorted(points.items()):
            if node in (a,b):continue
            distance=sum((p[k]-start[k])*vector[k] for k in range(3))
            if not tolerance<distance<length-tolerance:continue
            projection=[start[k]+distance*vector[k] for k in range(3)]
            if math.dist(p,projection)<=tolerance:
                hanging.append({"edge_node_ids":[a,b],"interior_node_id":node})
    if hanging:issues.append({"code":"hanging_source_GRID_on_edge","locations":hanging})
    if uncertain:issues.append({"code":"source_interface_precision_insufficient","edge_node_ids":uncertain})
    nonmanifold=[list(edge) for edge,count in sorted(edges.items()) if count>2]
    if nonmanifold:issues.append({"code":"nonmanifold_source_edges","edge_node_ids":nonmanifold})
    return issues


def _source_preflight(source, params):
    from .mesh_planning import audit_source
    interface_issues = _source_interface_issues(source)
    if interface_issues:
        raise ValueError("source interface audit failed before sweep: " + str(interface_issues))
    normalized = {
        "nodes": {row["id"]: {"position": row["xyz"]} for row in source["nodes"]},
        "elements": [{"entity": {"type": "SHELL", "id": row["id"]},
                      "element_type": row["type"], "node_ids": row["nodes"]}
                     for row in source["elements"]],
    }
    report = audit_source(normalized, source_ids=params["shell_ids"],
        direction=params["direction"], layers=params["steps"],
        target_length=params["distance"] / params["steps"])
    if report["status"] == "failed":
        raise ValueError("source audit failed before sweep: " + str(report["issues"]))
    # A translational sweep only supports a planar transverse source. This is
    # checked separately from target correspondence (which is unknown pre-write).
    points = [row["xyz"] for row in source["nodes"]]
    projections = [_dot(point, params["direction"]) for point in points]
    span = max(math.dist(a, b) for a in points for b in points)
    if max(projections) - min(projections) > max(span, 1.0) * 1e-8:
        raise ValueError("straight sweep requires a planar source perpendicular to direction")
    report["length_reference_origin"] = "distance_divided_by_steps"
    report["checks"]["planar_transverse_source"] = True
    return report


def _context_for_preview(state, deck):
    state.check_conflict()
    probe = state.probe()
    model, grant = probe.get("model"), probe.get("authorization")
    if (
        type(model) is not dict
        or type(model.get("epoch")) is not int
        or model["epoch"] <= 0
        or probe.get("deck") != deck
        or probe.get("connected") is not True
        or probe.get("stopped") is not False
        or type(grant) is not dict
        or grant.get("active") is not True
        or not grant.get("backend_id")
        or type(grant.get("generation")) is not int
        or grant["generation"] <= 0
        or getattr(state, "connected_backend_id", None) != grant["backend_id"]
    ):
        raise RuntimeError("structured native model and target context is unavailable")
    identity = {key: probe.get(key) for key in ("pid", "process_start", "instance_id", "session_id")}
    if any(value is None for value in identity.values()):
        raise RuntimeError("structured native process context is incomplete")
    return {
        **identity,
        "deck": deck,
        "epoch": model["epoch"],
        "backend_id": grant["backend_id"],
        "generation": grant["generation"],
    }


def _scope_row(base, deck, entity, reference):
    label = f"{reference['type']} {reference['id']}"
    row = _card(base, deck, entity, reference, label)
    result = {"entity": reference, "card_fields": row["card_fields"], "fields": row["fields"]}
    if reference["type"] in ("SHELL", "SOLID", "VOLUME"):
        values = catalog._read_card(base, deck, entity, reference, ("__part__",))
        result["part_id"] = _assignment_id(values.get("__part__"), "ANSAPART", "__part__")
    if reference["type"] == "FACE":
        result["geometry"] = geometry_delete._geometry(base, entity)
        assignments = catalog._read_card(
            base, deck, entity, reference, ("__part__", "__prop__")
        )
        result["assignments"] = {
            "__part__": _assignment_id(assignments.get("__part__"), "ANSAPART", "__part__"),
            "__prop__": _assignment_id(assignments.get("__prop__"), "PSHELL", "__prop__"),
        }
    elif reference["type"] == "ANSAPART":
        # Native Part display inertia is recomputed from its tessellation and
        # can change after a CAD face split. It is not a Part definition field.
        # Protect all underlying geometry/FE/material cards independently; do
        # not round coordinates or weaken the existing quality scope hash.
        for field in _PART_DERIVED_FIELDS:
            result["fields"].pop(field, None)
        result["ignored_derived_fields"] = list(_PART_DERIVED_FIELDS)
    elif reference["type"] == "HOT POINT":
        point = [_finite(row["fields"].get(axis), "HOT POINT " + axis) for axis in ("X", "Y", "Z")]
        result["geometry"] = {"bounds": point + point}
    elif reference["type"] == "VOLUME":
        members = base.CollectEntities(deck, entity, "SOLID")
        if type(members) not in (list, tuple):
            raise RuntimeError("VOLUME SOLID members are unreadable")
        result["solid_ids"] = sorted(member._id for member in members)
    elif reference["type"] in ("CONS", "CURVE"):
        bbox = inspection._require_api(base, "BoundBox", "ansa.base.BoundBox")(entity)
        if type(bbox) not in (list, tuple) or len(bbox) != 6:
            raise NotImplementedError(f"{label} has no complete documented bounding box")
        result["geometry"] = {
            "bounds": [_finite(value, f"{label} bound") for value in bbox]
        }
    return result


def _capture_scope(base, deck, *, budget=None):
    limit = _scope_evidence_limit(budget)
    pairs = volume_meshing._collect(base, deck, "__ALL_ENTITIES__")
    if len(pairs) > _MAX_MODEL_ENTITIES:
        raise ValueError(f"model exceeds the {_MAX_MODEL_ENTITIES}-entity scope limit")
    rows = [
        _scope_row(base, deck, entity, reference)
        for entity, reference in pairs
    ]
    snapshot = {"complete": True, "deck": deck, "entities": rows}
    encoded = volume_meshing._encoded(snapshot)
    if len(encoded) > limit:
        raise ValueError(
            f"full model scope exceeds the {limit} bytes ({limit / 1024:g} KiB) "
            "evidence budget"
        )
    return snapshot


def _scope_map(snapshot):
    result = {}
    for row in snapshot["entities"]:
        reference = row["entity"]
        key = (reference["type"], reference["id"])
        if key in result:
            raise RuntimeError(f"duplicate entity identity in model scope: {key}")
        result[key] = row
    return result


def _row_for(base, deck, entity_type, entity_id):
    entity, reference = catalog._get_entity(base, deck, entity_type, entity_id)
    return entity, reference, _scope_row(base, deck, entity, reference)


def _property_entity(base, deck, entity_type, entity_id, required_fields):
    entity, reference, row = _row_for(base, deck, entity_type, entity_id)
    if reference["type"] != entity_type or reference["id"] != entity_id:
        raise RuntimeError(f"{entity_type} {entity_id} lookup returned a different identity")
    missing = sorted(set(required_fields) - row["fields"].keys())
    if missing:
        raise NotImplementedError(
            f"{entity_type} {entity_id} lacks required card field(s): {', '.join(missing)}"
        )
    return entity, row


def _face_source(base, mesh, deck, face_id):
    face, row, shell_ids = meshing._face_snapshot(base, mesh, deck, face_id)
    if row["shell_ids_truncated"]:
        raise ValueError(f"FACE {face_id} has an incomplete associated SHELL list")
    if shell_ids:
        raise ValueError(f"FACE {face_id} is meshed; partition accepts unmeshed geometry only")
    if row["fields"].get("Frozen") != "NO":
        raise ValueError(f"FACE {face_id} must be unfrozen")
    _cons, keys = geometry_delete._collection(
        base, deck, "CONS", face, geometry_delete._MAX_CONS
    )
    cons_ids = sorted(key[1] for key in keys)
    if not cons_ids:
        raise RuntimeError(f"FACE {face_id} boundary CONS could not be confirmed")
    return face, {
        "id": face_id,
        "fields": row["fields"],
        "geometry": geometry_delete._geometry(base, face),
        "macro": row["macro"],
        "macro_face_ids": row["macro_face_ids"],
        "boundary_cons_ids": cons_ids,
        "shell_ids": shell_ids,
    }


def _assignment_id(value, entity_type, label):
    if type(value) is dict:
        if value.get("type") != entity_type:
            raise ValueError(f"FACE {label} does not resolve to {entity_type}")
        return _positive_int(value.get("id"), f"FACE {label} ID")
    if type(value) is int:
        return _positive_int(value, f"FACE {label} ID")
    raise ValueError(f"FACE {label} assignment is missing or unreadable")


def _face_assignments(base, deck, face, face_id):
    reference = catalog._entity_reference(base, deck, face, "FACE", face_id)
    values = catalog._read_card(base, deck, face, reference, ("__part__", "__prop__"))
    return {
        "part_id": _assignment_id(values.get("__part__"), "ANSAPART", "__part__"),
        "property_id": _assignment_id(values.get("__prop__"), "PSHELL", "__prop__"),
    }


def _plane_range(bounds, origin, normal):
    center = [(bounds[i] + bounds[i + 3]) * 0.5 for i in range(3)]
    radius = sum(abs(normal[i]) * (bounds[i + 3] - bounds[i]) * 0.5 for i in range(3))
    signed_center = sum((center[i] - origin[i]) * normal[i] for i in range(3))
    return signed_center - radius, signed_center + radius


def _scope_summary(snapshot):
    encoded = volume_meshing._encoded(snapshot)
    types = Counter(row["entity"]["type"] for row in snapshot["entities"])
    return {
        "complete": snapshot.get("complete") is True,
        "entity_count": len(snapshot["entities"]),
        "entity_type_counts": dict(sorted(types.items())),
        "serialized_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _partition_plan(state, base, mesh, constants, params):
    normalized = _normalize_partition(params, applying=False)
    deck = inspection._deck(base, normalized)
    _check_nastran(constants, deck)
    context = _context_for_preview(state, deck)
    scope = _capture_scope(base, deck)
    rows = _scope_map(scope)
    source_faces = []
    protected_faces = []
    assignments = []
    for face_id in normalized["face_ids"]:
        face, source = _face_source(base, mesh, deck, face_id)
        source_faces.append(source)
        assignments.append(_face_assignments(base, deck, face, face_id))
    for face_id in normalized["protected_face_ids"]:
        _entity, reference, row = _row_for(base, deck, "FACE", face_id)
        protected_faces.append({
            "id": face_id,
            "reference": reference,
            "fields": row["fields"],
            "geometry": row["geometry"],
        })
    if len({(row["part_id"], row["property_id"]) for row in assignments}) != 1:
        raise ValueError("all partition source FACEs must share one readable ANSAPART and PSHELL")
    part_id, property_id = assignments[0]["part_id"], assignments[0]["property_id"]
    part, part_row = _property_entity(base, deck, "ANSAPART", part_id, ("Name",))
    prop, prop_row = _property_entity(base, deck, "PSHELL", property_id, ("PID",))
    if part is None or prop is None or prop_row["fields"].get("PID") != property_id:
        raise RuntimeError("partition ANSAPART/PSHELL identity could not be verified")

    source_by_id = {row["id"]: row for row in source_faces}
    if set(source_by_id) != set(normalized["face_ids"]):
        raise RuntimeError("partition source FACE readback is incomplete")
    protected_by_id = {row["id"]: row for row in protected_faces}
    if set(protected_by_id) != set(normalized["protected_face_ids"]):
        raise RuntimeError("protected FACE readback is incomplete")

    origin, normal = normalized["plane"]["origin"], normalized["plane"]["normal"]
    candidates = []
    source_bounds = []
    for face_id in normalized["face_ids"]:
        geometry = source_by_id[face_id]["geometry"]
        lower, upper = _plane_range(geometry["bounds"], origin, normal)
        scale = max(1.0, *(abs(value) for value in geometry["bounds"]))
        tolerance = scale * 1e-10
        if not lower < -tolerance or not upper > tolerance:
            raise ValueError(f"plane does not strictly cross FACE {face_id}'s bounding box")
        candidates.append({
            "face_id": face_id,
            "classification": "bounding_box_crossing_candidate",
            "signed_distance_bounds": [lower, upper],
            "geometry_intersection_verified": False,
        })
        source_bounds.append(geometry["bounds"])
    for face_id in normalized["protected_face_ids"]:
        geometry = protected_by_id[face_id]["geometry"]
        lower, upper = _plane_range(geometry["bounds"], origin, normal)
        scale = max(1.0, *(abs(value) for value in geometry["bounds"]))
        tolerance = scale * 1e-10
        if lower <= tolerance and upper >= -tolerance:
            raise ValueError(f"partition plane may intersect protected FACE {face_id}")

    part_digest = _hash(part_row)
    part_aggregate = None
    if normalized['produce_plane_faces']:
        from . import partition_aggregates
        part_aggregate = partition_aggregates.capture(base, deck, scope, part_id)
    property_digest = _hash(prop_row)
    source_fingerprints = [
        {"face_id": row["id"], "sha256": _hash(row)} for row in source_faces
    ]
    source_model_fingerprints = [
        {"face_id": face_id, "sha256": _hash(rows[("FACE", face_id)])}
        for face_id in normalized["face_ids"]
    ]
    protected_fingerprints = [
        {"face_id": row["id"], "sha256": _hash(row)} for row in protected_faces
    ]
    scope_digest = _scope_summary(scope)
    hash_input = {
        "operation": "geometry_partition",
        "context": context,
        "request": normalized,
        "source_fingerprints": source_fingerprints,
        "source_model_fingerprints": source_model_fingerprints,
        "protected_fingerprints": protected_fingerprints,
        "part_sha256": part_digest,
        "property_sha256": property_digest,
        "assigned_part_id": part_id,
        "assigned_property_id": property_id,
        "model_scope_sha256": scope_digest["sha256"],
        "internal_plane_part_aggregate": part_aggregate,
    }
    return {
        "ok": True,
        "tool": "geometry_partition_plan",
        "deck": deck,
        "read_only": True,
        "static_preview": True,
        "plan_sha256": _hash(hash_input),
        "face_ids": normalized["face_ids"],
        "protected_face_ids": normalized["protected_face_ids"],
        "plane": normalized["plane"],
        "produce_plane_faces": normalized["produce_plane_faces"],
        "part_id": part_id,
        "property_id": property_id,
        "candidates": candidates,
        "source_fingerprints": source_fingerprints,
        "source_model_fingerprints": source_model_fingerprints,
        "protected_fingerprints": protected_fingerprints,
        "source_parent_mapping_supported": False,
        "model_scope": scope_digest,
        "warnings": [
            "bounding-box crossing is conservative and does not prove exact CAD surface intersection",
            "new FACE parent attribution is not available from the documented native return value",
        ],
        "_hash_input": hash_input,
        "_before_snapshot": scope,
        "_source_faces": source_faces,
        "_protected_faces": protected_faces,
        "_part_row": part_row,
        "_property_row": prop_row,
        "_part_id": part_id,
        "_property_id": property_id,
        "_target_bounds": source_bounds,
        "_part_aggregate": part_aggregate,
    }


def _public_plan(plan):
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def _bounds_union(bounds_list):
    return [
        min(bounds[axis] for bounds in bounds_list) if axis < 3
        else max(bounds[axis] for bounds in bounds_list)
        for axis in range(6)
    ]


def _bounds_local(row, target_bounds):
    geometry = row.get("geometry")
    bounds = geometry.get("bounds") if type(geometry) is dict else None
    if type(bounds) is not list or len(bounds) != 6:
        return False
    outer = _bounds_union(target_bounds)
    scale = max(1.0, *(abs(value) for value in outer))
    tolerance = scale * 1e-8
    return all(
        bounds[index] >= outer[index] - tolerance
        and bounds[index + 3] <= outer[index + 3] + tolerance
        for index in range(3)
    )


def _partition_diff(before, after, target_ids, source_faces, expected_assignments=None, part_aggregate=None):
    old, new = _scope_map(before), _scope_map(after)
    old_keys, new_keys = set(old), set(new)
    added, removed = new_keys - old_keys, old_keys - new_keys
    source_cons = {cons_id for row in source_faces for cons_id in row["boundary_cons_ids"]}
    bounds = [row["geometry"]["bounds"] for row in source_faces]
    errors = []

    for entity_type, entity_id in sorted(removed):
        if entity_type == "FACE" and entity_id in target_ids:
            continue
        if entity_type == "CONS" and entity_id in source_cons:
            continue
        errors.append(f"removed entity outside explicit partition scope: {entity_type} {entity_id}")

    for entity_type, entity_id in sorted(added):
        row = new[(entity_type, entity_id)]
        if entity_type in ("FACE", "CONS", "CURVE", "HOT POINT") and _bounds_local(row, bounds):
            if entity_type == "FACE" and expected_assignments is not None \
                    and row.get("assignments") != expected_assignments:
                errors.append(f"new FACE {entity_id} has an unverified ANSAPART/PSHELL assignment")
            continue
        errors.append(f"added entity outside verifiable partition bounds: {entity_type} {entity_id}")

    changed = []
    for key in sorted(old_keys & new_keys):
        if old[key] == new[key]:
            continue
        entity_type, entity_id = key
        if entity_type == 'ANSAPART' and part_aggregate is not None:
            from . import partition_aggregates
            if partition_aggregates.validated_change(old[key], new[key], part_aggregate):
                changed.append(key)
                continue
        if entity_type == "FACE" and entity_id in target_ids:
            if any(old[key].get(field) != new[key].get(field)
                   for field in ("card_fields", "fields", "assignments")):
                errors.append(f"selected source FACE {entity_id} card or assignment changed")
            changed.append(key)
            continue
        if entity_type == "CONS" and entity_id in source_cons:
            changed.append(key)
            continue
        changed_fields = {field: {"before": old[key].get("fields", {}).get(field),
                                  "after": new[key].get("fields", {}).get(field)}
                          for field in old[key].get("fields", {}).keys() | new[key].get("fields", {}).keys()
                          if old[key].get("fields", {}).get(field) != new[key].get("fields", {}).get(field)}
        errors.append(f"card or geometry changed outside explicit partition scope: {entity_type} {entity_id}; "
                      + str(changed_fields)[:2048])
    return {
        "errors": errors,
        "added_face_ids": sorted(entity_id for entity_type, entity_id in added if entity_type == "FACE"),
        "removed_target_face_ids": sorted(entity_id for entity_type, entity_id in removed
                                            if entity_type == "FACE" and entity_id in target_ids),
        "remaining_target_face_ids": sorted(entity_id for entity_type, entity_id in new_keys
                                             if entity_type == "FACE" and entity_id in target_ids),
        "added_cons_ids": sorted(entity_id for entity_type, entity_id in added if entity_type == "CONS"),
        "removed_boundary_cons_ids": sorted(entity_id for entity_type, entity_id in removed
                                             if entity_type == "CONS" and entity_id in source_cons),
        "changed_entity_ids": [list(key) for key in changed],
        "observable_changes": any(kind == "FACE" for kind, _ in added | removed | set(changed)),
        "outside_scope_verified": not errors,
    }


def _evidence(snapshot):
    if snapshot is None:
        return None
    summary = _scope_summary(snapshot)
    return summary


def _error_text(exc):
    return f"{type(exc).__name__}: {exc}"[:4096]


def _backup_preflight(base, params):
    backup_path, _prior = volume_meshing._save_path(params["backup_path"], False)
    current_path = volume_meshing._database_name(base)
    if not current_path:
        raise ValueError("the current model must have a saved database path for backup verification")
    if model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_file = Path(current_path)
    if not current_file.is_file():
        raise ValueError("the current database file is unavailable for fingerprint verification")
    current_fingerprint = model_files._fingerprint(current_file)
    return backup_path, current_path, current_fingerprint


def _verify_backup(base, constants, backup_path, current_path, current_fingerprint):
    return volume_meshing._verify_backup(
        base, constants, backup_path, current_path, current_fingerprint
    )


def _unconfirmed_backup(path):
    return volume_meshing._unconfirmed_backup(path)


def _backup_failure_result(tool, deck, backup, before, after, error, *, api_called=False,
                            possible_effects=False):
    return {
        "ok": False,
        "tool": tool,
        "deck": deck,
        "backup": backup,
        "before": _evidence(before),
        "after": _evidence(after),
        "api_called": api_called,
        "scope_verified": False,
        "possible_effects": possible_effects,
        "error": error,
        "errors": [error],
    }


def _run_partition(state, base, mesh, constants, params):
    clean = _normalize_partition(params, applying=True)
    deck = inspection._deck(base, clean)
    _check_nastran(constants, deck)
    context = volume_meshing._execution_context(state, deck)
    plan_params = {key: value for key, value in clean.items()
                   if key not in ("expected_plan_sha256", "backup_path")}
    planned = _partition_plan(state, base, mesh, constants, plan_params)
    if planned["plan_sha256"] != clean["expected_plan_sha256"]:
        raise ValueError("expected_plan_sha256 does not match the current partition plan")
    before = planned["_before_snapshot"]
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    backup_path, current_path, current_fingerprint = _backup_preflight(base, clean)
    volume_meshing._same_context(state, deck, context)

    backup = None
    try:
        backup = _verify_backup(base, constants, backup_path, current_path, current_fingerprint)
    except Exception as exc:
        backup = _unconfirmed_backup(backup_path)
        try:
            after_save = _capture_scope(base, deck)
            possible = after_save != before
            possible = possible or not model_files._same_path(current_path, volume_meshing._database_name(base))
            possible = possible or not catalog._strict_equal(
                current_fingerprint, model_files._fingerprint(Path(current_path))
            )
            volume_meshing._same_context(state, deck, context)
        except Exception:
            after_save, possible = None, True
        return _backup_failure_result(
            "geometry_partition_apply", deck, backup, before, after_save,
            _error_text(exc), possible_effects=possible,
        )

    try:
        volume_meshing._same_context(state, deck, context)
        after_backup = _capture_scope(base, deck)
        replan = _partition_plan(state, base, mesh, constants, plan_params)
        if replan["plan_sha256"] != clean["expected_plan_sha256"]:
            raise RuntimeError("partition plan changed after backup; native call was not made")
        if after_backup != before:
            raise RuntimeError("model scope changed after backup; native call was not made")
        volume_meshing._same_context(state, deck, context)
    except Exception as exc:
        return _backup_failure_result(
            "geometry_partition_apply", deck, backup, before,
            locals().get("after_backup"), _error_text(exc), possible_effects=True,
        )

    source_entities = [catalog._get_entity(base, deck, "FACE", face_id)[0]
                       for face_id in clean["face_ids"]]
    part, _part_ref, _part_row = _row_for(base, deck, "ANSAPART", planned["_part_id"])
    prop, _prop_ref, _prop_row = _row_for(base, deck, "PSHELL", planned["_property_id"])
    api_status = None
    api_error = None
    try:
        api_status = base.FacesPlaneCut(
            working_plane=[clean["plane"]["origin"], clean["plane"]["normal"]],
            entities=source_entities,
            produce_plane_faces=clean["produce_plane_faces"],
            perform_topology=True,
            part=part,
            property=prop,
        )
    except Exception as exc:
        api_error = _error_text(exc)

    try:
        after = _capture_scope(base, deck)
        expected_assignments = {
            "__part__": planned["_part_id"], "__prop__": planned["_property_id"],
        }
        part_readback = None
        if clean['produce_plane_faces']:
            from . import partition_aggregates
            part_readback = {'before': planned['_part_aggregate'],
                             'after': partition_aggregates.capture(base, deck, after, planned['_part_id'])}
        diff = _partition_diff(
            before, after, set(clean["face_ids"]), planned["_source_faces"], expected_assignments, part_readback
        )
        after_rows = _scope_map(after)
        source_fingerprints_after = [
            {"face_id": face_id,
             "sha256": _hash(after_rows[("FACE", face_id)])
             if ("FACE", face_id) in after_rows else None}
            for face_id in clean["face_ids"]
        ]
        readback_error = None
    except Exception as exc:
        after, diff, source_fingerprints_after, readback_error = None, None, None, _error_text(exc)
    try:
        volume_meshing._same_context(state, deck, context)
        context_error = None
    except Exception as exc:
        context_error = _error_text(exc)
    errors = list(diff["errors"] if diff is not None else [])
    if readback_error:
        errors.append("post-partition model readback: " + readback_error)
    if context_error:
        errors.append("post-partition model context: " + context_error)
    api_ok = api_error is None and type(api_status) is int and api_status == 1
    if not api_ok:
        errors.insert(0, api_error or "FacesPlaneCut did not return the exact success value int 1")
    elif diff is not None and not diff["observable_changes"]:
        message = "FacesPlaneCut returned success but no model partition change was observable"
        diff["errors"].append(message)
        diff["outside_scope_verified"] = False
        errors.append(message)
    scope_ok = diff is not None and diff["outside_scope_verified"] and readback_error is None
    ok = api_ok and scope_ok and context_error is None
    return {
        "ok": ok,
        "tool": "geometry_partition_apply",
        "deck": deck,
        "face_ids": clean["face_ids"],
        "protected_face_ids": clean["protected_face_ids"],
        "part_id": planned["_part_id"],
        "property_id": planned["_property_id"],
        "plan_sha256": clean["expected_plan_sha256"],
        "backup": backup,
        "before": _evidence(before),
        "after": _evidence(after),
        "source_fingerprints_before": planned["source_model_fingerprints"],
        "source_fingerprints_after": source_fingerprints_after,
        "source_topology_fingerprints": planned["source_fingerprints"],
        "api_called": True,
        "api_return_code": api_status,
        "api_return_type": type(api_status).__name__ if api_error is None else None,
        "api_error": api_error,
        "scope_verified": scope_ok,
        "possible_effects": not ok,
        "source_parent_mapping_supported": False,
        "source_face_ids": clean["face_ids"],
        "created_face_ids": diff["added_face_ids"] if diff else [],
        "removed_target_face_ids": diff["removed_target_face_ids"] if diff else [],
        "remaining_target_face_ids": diff["remaining_target_face_ids"] if diff else [],
        "topology_changes": diff,
        "part_aggregate_readback": locals().get('part_readback'),
        "error": "; ".join(errors) if errors else None,
        "errors": errors,
    }


def _entity_ids(snapshot, entity_type):
    return {row["entity"]["id"] for row in snapshot["entities"]
            if row["entity"]["type"] == entity_type}


def _face_volume_count_changed(before, after):
    old, new = _scope_map(before), _scope_map(after)
    missing = object()

    def value(row):
        fields = row.get("fields")
        return fields.get("Num. Volumes", missing) if type(fields) is dict else missing

    return any(
        value(old[key]) != value(new[key])
        for key in old.keys() & new.keys()
        if key[0] == "FACE"
    )


def _volume_count(value):
    if type(value) is int and value >= 0:
        return value
    if type(value) is str:
        text = value.strip()
        if text and text.isascii() and text.isdigit():
            return int(text)
    raise ValueError("Num. Volumes is not a nonnegative integer")


def _face_volume_count_delta(old_row, new_row):
    old_fields, new_fields = old_row.get("fields"), new_row.get("fields")
    field = "Num. Volumes"
    if type(old_fields) is not dict or type(new_fields) is not dict:
        return None
    if field not in old_fields or field not in new_fields:
        return None
    old_definition = {**old_row, "fields": {key: value for key, value in old_fields.items() if key != field}}
    new_definition = {**new_row, "fields": {key: value for key, value in new_fields.items() if key != field}}
    if old_definition != new_definition:
        return None

    try:
        delta = _volume_count(new_fields[field]) - _volume_count(old_fields[field])
    except (TypeError, ValueError):
        return None
    return delta if delta > 0 else None


def _collect_sweep_volume_boundary_faces(base, mesh, deck, volume_ids, source_shell_ids, before, after):
    volume_ids = sorted(_id_list(
        volume_ids, "new VOLUME IDs", _MAX_NEW_VOLUMES, allow_empty=True
    ))
    old, new = _scope_map(before), _scope_map(after)
    added_volumes = {entity_id for kind, entity_id in new.keys() - old.keys() if kind == "VOLUME"}
    if set(volume_ids) != added_volumes:
        raise RuntimeError("native boundary request does not match the complete new VOLUME scope")
    if not volume_ids:
        return {}

    source_shell_ids = set(_id_list(
        source_shell_ids, "source SHELL IDs", _MAX_SOURCE_CAPTURE_ELEMENTS
    ))

    get_volume_boundary = inspection._require_api(
        mesh, "GetVolumeBoundary", "ansa.mesh.GetVolumeBoundary"
    )
    get_face_macro = inspection._require_api(base, "GetFaceMacro", "ansa.base.GetFaceMacro")
    get_faces_of_macros = inspection._require_api(
        base, "GetFacesOfMacros", "ansa.base.GetFacesOfMacros"
    )
    get_macro_shells = inspection._require_api(
        mesh, "GetMacroShells", "ansa.mesh.GetMacroShells"
    )

    macro_cache = {}
    validated_faces = set()
    source_faces = set()
    source_face_count = 0
    source_shell_count = 0
    boundary_count = 0
    result = {}
    for volume_id in volume_ids:
        volume, _ = catalog._get_entity(base, deck, "VOLUME", volume_id)
        catalog._entity_reference(base, deck, volume, "VOLUME", volume_id)
        boundary = get_volume_boundary(volume)
        if type(boundary) is not list:
            raise RuntimeError(f"VOLUME {volume_id} boundary is unavailable or incomplete")
        if len(boundary) > _MAX_VOLUME_BOUNDARY_ENTITIES:
            raise ValueError(f"VOLUME {volume_id} boundary exceeds {_MAX_VOLUME_BOUNDARY_ENTITIES} entities")
        boundary_count += len(boundary)
        if boundary_count > _MAX_TOTAL_VOLUME_BOUNDARY_ENTITIES:
            raise ValueError(
                f"new VOLUME boundaries exceed {_MAX_TOTAL_VOLUME_BOUNDARY_ENTITIES} entities"
            )

        seen = set()
        face_ids = []
        for entity in boundary:
            reference = catalog._entity_reference(base, deck, entity)
            entity_type, entity_id = reference["type"], reference["id"]
            if entity_type not in ("FACE", "SHELL"):
                raise RuntimeError(f"VOLUME {volume_id} boundary returned unknown entity type {entity_type!r}")
            key = (entity_type, entity_id)
            if key in seen:
                raise RuntimeError(f"VOLUME {volume_id} boundary contains duplicate {entity_type} {entity_id}")
            seen.add(key)
            if entity_type != "FACE":
                continue
            if ("FACE", entity_id) not in old or ("FACE", entity_id) not in new:
                raise RuntimeError(f"new VOLUME {volume_id} boundary FACE {entity_id} is outside the existing scope")

            if entity_id not in validated_faces:
                source_faces.add(entity_id)
                if len(source_faces) > _MAX_SOURCE_CAPTURE_ELEMENTS:
                    raise ValueError(
                        f"source FACE evidence exceeds {_MAX_SOURCE_CAPTURE_ELEMENTS} entities"
                    )
                macro = get_face_macro(entity)
                if macro is None:
                    raise RuntimeError(f"source FACE {entity_id} has no readable macro")
                macro_reference = catalog._entity_reference(base, deck, macro, "FACE")
                macro_id = macro_reference["id"]
                if macro_id not in macro_cache:
                    macro_faces = get_faces_of_macros([macro])
                    if type(macro_faces) is not list:
                        raise RuntimeError("GetFacesOfMacros returned an unavailable or incomplete entity list")
                    if len(macro_faces) > _MAX_SOURCE_CAPTURE_ELEMENTS:
                        raise ValueError(
                            f"GetFacesOfMacros exceeds {_MAX_SOURCE_CAPTURE_ELEMENTS} FACE entities"
                        )
                    source_face_count += len(macro_faces)
                    if source_face_count > _MAX_SOURCE_CAPTURE_ELEMENTS:
                        raise ValueError(
                            f"source macro FACE evidence exceeds {_MAX_SOURCE_CAPTURE_ELEMENTS} entities"
                        )
                    macro_face_refs = [
                        catalog._entity_reference(base, deck, face, "FACE") for face in macro_faces
                    ]
                    macro_face_ids = [reference["id"] for reference in macro_face_refs]
                    if len(macro_face_ids) != len(set(macro_face_ids)):
                        raise RuntimeError("GetFacesOfMacros returned duplicate FACE identities")

                    macro_shells = get_macro_shells([macro])
                    if type(macro_shells) is int and macro_shells == 0:
                        raise RuntimeError(f"source FACE {entity_id} macro has no SHELL members (GetMacroShells returned 0)")
                    if type(macro_shells) is not list:
                        raise RuntimeError("GetMacroShells returned an unavailable or incomplete entity list")
                    if len(macro_shells) > _MAX_SOURCE_CAPTURE_ELEMENTS:
                        raise ValueError(
                            f"GetMacroShells exceeds {_MAX_SOURCE_CAPTURE_ELEMENTS} SHELL entities"
                        )
                    source_shell_count += len(macro_shells)
                    if source_shell_count > _MAX_SOURCE_CAPTURE_ELEMENTS:
                        raise ValueError(
                            f"source macro SHELL evidence exceeds {_MAX_SOURCE_CAPTURE_ELEMENTS} entities"
                        )
                    macro_shell_refs = [
                        catalog._entity_reference(base, deck, shell, "SHELL") for shell in macro_shells
                    ]
                    macro_shell_ids = [reference["id"] for reference in macro_shell_refs]
                    if len(macro_shell_ids) != len(set(macro_shell_ids)):
                        raise RuntimeError("GetMacroShells returned duplicate SHELL identities")
                    if not macro_shell_ids:
                        raise RuntimeError(f"source FACE {entity_id} macro has no SHELL members")
                    if not set(macro_shell_ids) <= source_shell_ids:
                        raise RuntimeError(
                            f"source FACE {entity_id} macro contains SHELLs outside the requested sweep scope"
                        )
                    macro_cache[macro_id] = (sorted(macro_face_ids), sorted(macro_shell_ids))

                macro_face_ids, _macro_shell_ids = macro_cache[macro_id]
                if macro_id != entity_id or macro_face_ids != [entity_id]:
                    raise RuntimeError(f"source FACE {entity_id} is not a single-FACE macro")
                validated_faces.add(entity_id)
            face_ids.append(entity_id)
        result[volume_id] = sorted(face_ids)
    return result


def _sweep_scope_diff(before, after, params=None, volume_boundary_faces=None):
    old, new = _scope_map(before), _scope_map(after)
    old_keys, new_keys = set(old), set(new)
    added, removed = new_keys - old_keys, old_keys - new_keys
    errors = []
    for entity_type, entity_id in sorted(removed):
        errors.append(f"existing {entity_type} {entity_id} was removed")
    allowed = {"GRID", "SOLID"} | ({"VOLUME", "PSOLID", "MAT1"} if params is not None else set())
    unexpected = sorted(key for key in added if key[0] not in allowed)
    errors.extend(f"unexpected new entity: {kind} {entity_id}" for kind, entity_id in unexpected)
    new_volume_ids = {entity_id for kind, entity_id in added if kind == "VOLUME"}
    if len(new_volume_ids) > _MAX_NEW_VOLUMES:
        errors.append(f"new VOLUME count exceeds the {_MAX_NEW_VOLUMES} entity limit")

    face_count_deltas = {
        key[1]: delta
        for key in sorted(old_keys & new_keys)
        if key[0] == "FACE"
        for delta in [_face_volume_count_delta(old[key], new[key])]
        if delta is not None
    }
    allowed_face_count_changes = set()
    if volume_boundary_faces is None:
        if params is not None:
            errors.extend(
                f"FACE {face_id} Num. Volumes changed without native VOLUME boundary evidence"
                for face_id in sorted(face_count_deltas)
            )
    elif params is None:
        errors.append("native VOLUME boundary evidence requires explicit sweep parameters")
    elif type(volume_boundary_faces) is not dict:
        errors.append("native VOLUME boundary evidence must be an object")
    else:
        evidence_valid = True
        evidence_volume_ids = set()
        evidence_face_counts = Counter()
        total_face_references = 0
        for volume_id, face_ids in volume_boundary_faces.items():
            if type(volume_id) is not int or volume_id <= 0:
                errors.append("native VOLUME boundary evidence contains an invalid VOLUME ID")
                evidence_valid = False
                continue
            evidence_volume_ids.add(volume_id)
            if volume_id not in new_volume_ids:
                errors.append(f"native VOLUME boundary evidence references non-new VOLUME {volume_id}")
                evidence_valid = False
            if type(face_ids) is not list:
                errors.append(f"native VOLUME {volume_id} boundary FACE evidence is incomplete")
                evidence_valid = False
                continue
            if len(face_ids) > _MAX_VOLUME_BOUNDARY_ENTITIES:
                errors.append(f"native VOLUME {volume_id} boundary evidence exceeds the per-volume limit")
                evidence_valid = False
            total_face_references += len(face_ids)
            if total_face_references > _MAX_TOTAL_VOLUME_BOUNDARY_ENTITIES:
                errors.append("native VOLUME boundary evidence exceeds the cumulative entity limit")
                evidence_valid = False
            seen_face_ids = set()
            for face_id in face_ids:
                if type(face_id) is not int or face_id <= 0:
                    errors.append(f"native VOLUME {volume_id} boundary evidence contains an invalid FACE ID")
                    evidence_valid = False
                    continue
                if face_id in seen_face_ids:
                    errors.append(f"native VOLUME {volume_id} boundary evidence duplicates FACE {face_id}")
                    evidence_valid = False
                    continue
                seen_face_ids.add(face_id)
                if ("FACE", face_id) not in old_keys or ("FACE", face_id) not in new_keys:
                    errors.append(f"native VOLUME {volume_id} boundary FACE {face_id} is not an existing FACE")
                    evidence_valid = False
                evidence_face_counts[face_id] += 1

        if evidence_volume_ids != new_volume_ids:
            errors.append("native VOLUME boundary evidence must cover exactly all added VOLUMEs")
            evidence_valid = False
        if evidence_valid:
            for face_id, expected_delta in sorted(evidence_face_counts.items()):
                old_count = old[("FACE", face_id)].get("fields", {}).get("Num. Volumes")
                new_count = new[("FACE", face_id)].get("fields", {}).get("Num. Volumes")
                try:
                    actual_delta = _volume_count(new_count) - _volume_count(old_count)
                    if actual_delta != expected_delta:
                        raise ValueError("count increment does not match native boundary references")
                except (TypeError, ValueError):
                    errors.append(
                        f"FACE {face_id} Num. Volumes increment does not match native VOLUME boundary evidence"
                    )
                    evidence_valid = False
            for face_id, delta in sorted(face_count_deltas.items()):
                if evidence_face_counts.get(face_id) != delta:
                    errors.append(
                        f"FACE {face_id} Num. Volumes increment does not match native VOLUME boundary evidence"
                    )
                    evidence_valid = False
        if evidence_valid:
            allowed_face_count_changes = {
                face_id for face_id, delta in face_count_deltas.items()
                if evidence_face_counts.get(face_id) == delta
            }

    def same_definition(key):
        if key[0] == "FACE" and key[1] in allowed_face_count_changes:
            return True
        if key[0] != "ANSAPART":
            return old[key] == new[key]
        def definition(row):
            return {**row, "fields": {k:v for k,v in row["fields"].items() if k not in _PART_AGGREGATE_FIELDS}}
        return definition(old[key]) == definition(new[key])
    changed = [key for key in sorted(old_keys & new_keys) if not same_definition(key)]
    errors.extend(f"existing card or geometry changed: {kind} {entity_id}" for kind, entity_id in changed)
    if params is not None:
        owners = Counter()
        solids = {eid for kind,eid in added if kind == "SOLID"}
        new_volumes = new_volume_ids
        aux_props = {eid for kind,eid in added if kind == "PSOLID"}
        aux_materials = {eid for kind,eid in added if kind == "MAT1"}
        if len(aux_props) > len(new_volumes) or len(aux_materials) > len(new_volumes):
            errors.append("native auxiliary property/material count exceeds the VOLUME budget")
        for pid in aux_props:
            if any(row["entity"]["type"] in ("FACE","SHELL","SOLID","VOLUME")
                   and row["fields"].get("PID") == pid for row in new.values()):
                errors.append(f"native auxiliary PSOLID {pid} is referenced by geometry or FE")
        for mid in aux_materials:
            for row in new.values():
                if row["entity"]["type"] in ("ANSAPART","MAT1"): continue
                for field in ("MID","MID1","MID2","MID3","MID4"):
                    if row["fields"].get(field) == mid and not (
                            row["entity"]["type"] == "PSOLID" and row["entity"]["id"] in aux_props):
                        errors.append(f"native auxiliary MAT1 {mid} has an outside reference")
        for key in sorted(added):
            if key[0] != "VOLUME": continue
            row = new[key]
            required = {"Type":"Extrude", "Algorithm":"Translate", "PID":params["property_id"],
                        "Extrude steps":params["steps"], "Distance":params["distance"], "Order":"1st", "Status":"Meshed"}
            if any(row["fields"].get(k) != v for k,v in required.items()):
                errors.append(f"new VOLUME {key[1]} definition differs from the sweep")
            members = row.get("solid_ids", [])
            if not members or not set(members) <= solids:
                errors.append(f"new VOLUME {key[1]} includes an empty or outside SOLID scope")
            owners.update(members)
        if owners != Counter({eid:1 for eid in solids}):
            errors.append("new SOLIDs do not have unique complete native VOLUME membership")
    return {
        "errors": errors,
        "new_grid_ids": sorted(entity_id for kind, entity_id in added if kind == "GRID"),
        "new_solid_ids": sorted(entity_id for kind, entity_id in added if kind == "SOLID"),
        "new_volume_ids": sorted(entity_id for kind, entity_id in added if kind == "VOLUME"),
        "native_auxiliary_properties": sorted(eid for kind,eid in added if kind == "PSOLID"),
        "native_auxiliary_materials": sorted(eid for kind,eid in added if kind == "MAT1"),
        "removed_ids": [list(key) for key in sorted(removed)],
        "changed_ids": [list(key) for key in changed],
        "unexpected_added_ids": [list(key) for key in unexpected],
        "scope_verified": not errors,
    }


def _subtract(left, right):
    return [left[index] - right[index] for index in range(3)]


def _cross(left, right):
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _dot(left, right):
    return sum(left[index] * right[index] for index in range(3))


def _tetra_volume(a, b, c, d):
    return _dot(_subtract(b, a), _cross(_subtract(c, a), _subtract(d, a))) / 6.0


def _positive_element_volume(element, coords):
    points = [coords[node_id] for node_id in element["nodes"]]
    tets = (
        ((0, 1, 3, 4), (1, 2, 3, 6), (1, 3, 4, 6), (1, 4, 5, 6), (3, 4, 6, 7))
        if element["type"] == "CHEXA" else
        ((0, 1, 2, 3), (1, 2, 3, 4), (2, 3, 4, 5))
    )
    scale = max(
        math.dist(points[i], points[j])
        for i in range(len(points)) for j in range(i + 1, len(points))
    )
    tolerance = max(1.0, scale) ** 3 * 1e-12
    volumes = [_tetra_volume(*(points[index] for index in tet)) for tet in tets]
    if any(not math.isfinite(value) or value <= tolerance for value in volumes):
        raise ValueError(f"SOLID {element['id']} has a non-positive or degenerate signed volume")
    total = sum(volumes)
    if not math.isfinite(total) or total <= tolerance:
        raise ValueError(f"SOLID {element['id']} has no positive finite volume")
    return total


def _coords_equal(left, right, tolerance):
    if len(left) != len(right):
        return False
    unmatched = list(right)
    for point in left:
        match = next((index for index, other in enumerate(unmatched)
                      if math.dist(point, other) <= tolerance), None)
        if match is None:
            return False
        unmatched.pop(match)
    return not unmatched


def _verify_sweep_mesh(source, output, new_grid_ids, params):
    source_elements = source["elements"]
    output_elements = output["elements"]
    expected_count = len(params["shell_ids"]) * params["steps"]
    if len(output_elements) != expected_count:
        raise ValueError(f"expected {expected_count} new SOLIDs; read {len(output_elements)}")
    if not source_elements:
        raise ValueError("sweep source snapshot is empty")
    source_coords = {row["id"]: row["xyz"] for row in source["nodes"]}
    output_coords = {row["id"]: row["xyz"] for row in output["nodes"]}
    delta = [value * params["distance"] / params["steps"] for value in params["direction"]]
    all_points = [row["xyz"] for row in output["nodes"]]
    if all_points:
        scale = max(
            max(point[axis] for point in all_points) - min(point[axis] for point in all_points)
            for axis in range(3)
        )
    else:
        scale = 0.0
    tolerance = max(1.0, scale, params["distance"]) * 1e-8

    expected_positions = []
    for source_row in source_elements:
        original = [source_coords[node_id] for node_id in source_row["nodes"]]
        for layer in range(1, params["steps"] + 1):
            expected_positions.extend([
                [point[axis] + delta[axis] * layer for axis in range(3)]
                for point in original
            ])
    new_node_positions = [output_coords[node_id] for node_id in new_grid_ids]
    for point in expected_positions:
        if not any(math.dist(point, candidate) <= tolerance for candidate in new_node_positions):
            raise ValueError("one or more expected translated GRID positions are missing")
    for node_id in new_grid_ids:
        point = output_coords.get(node_id)
        if point is None or not any(math.dist(point, expected) <= tolerance for expected in expected_positions):
            raise ValueError(f"new GRID {node_id} is outside the expected straight sweep layers")

    matches = Counter()
    volumes = {}
    source_layer_map = []
    for element in output_elements:
        if element["property_id"] != params["property_id"]:
            raise ValueError(f"SOLID {element['id']} has an unexpected PSOLID assignment")
        volume = _positive_element_volume(element, output_coords)
        volumes[element["id"]] = volume
        expected_source_type = "CQUAD4" if element["type"] == "CHEXA" else "CTRIA3"
        cell_coords = [output_coords[node_id] for node_id in element["nodes"]]
        candidates = []
        for source_row in source_elements:
            if source_row["type"] != expected_source_type:
                continue
            original = [source_coords[node_id] for node_id in source_row["nodes"]]
            for layer in range(1, params["steps"] + 1):
                lower = [
                    [point[axis] + delta[axis] * (layer - 1) for axis in range(3)]
                    for point in original
                ]
                upper = [
                    [point[axis] + delta[axis] * layer for axis in range(3)]
                    for point in original
                ]
                if _coords_equal(cell_coords, lower + upper, tolerance):
                    candidates.append((source_row["id"], layer))
        if len(candidates) != 1:
            raise ValueError(f"SOLID {element['id']} cannot be uniquely matched to a source face/layer")
        matches[candidates[0]] += 1
        source_layer_map.append({"source_shell_id": candidates[0][0],
                                 "layer": candidates[0][1], "solid_id": element["id"]})
    expected_matches = Counter(
        (source_row["id"], layer)
        for source_row in source_elements
        for layer in range(1, params["steps"] + 1)
    )
    if matches != expected_matches:
        raise ValueError("new SOLID source/layer coverage is incomplete or duplicated")
    node_map = []
    for source_id, point in source_coords.items():
        layers = []
        for layer in range(params["steps"] + 1):
            expected = [point[axis] + delta[axis] * layer for axis in range(3)]
            found = [node_id for node_id, actual in output_coords.items()
                     if math.dist(expected, actual) <= tolerance]
            if len(found) != 1:
                raise ValueError("sweep node mapping is missing, duplicated or nonconformal")
            if layer == 0 and found[0] != source_id:
                raise ValueError("sweep bottom layer must reuse the actual source GRID identity")
            layers.append({"layer": layer, "grid_id": found[0]})
        node_map.append({"source_grid_id": source_id, "layers": layers})
    return {
        "solid_count": len(output_elements),
        "solid_type_counts": dict(sorted(Counter(row["type"] for row in output_elements).items())),
        "positive_volume_count": len(volumes),
        "total_volume": sum(volumes.values()),
        "source_layer_coverage": len(matches),
        "position_tolerance": tolerance,
        "source_layer_map": source_layer_map,
        "node_map": node_map,
    }


def _psolid_preflight(base, deck, property_id):
    prop, prop_row = _property_entity(base, deck, "PSOLID", property_id, ("PID", "MID"))
    if prop_row["fields"].get("PID") != property_id:
        raise RuntimeError(f"PSOLID {property_id} card PID does not match its identity")
    material_id = _positive_int(prop_row["fields"]["MID"], f"PSOLID {property_id} MID")
    _material, mat_row = _property_entity(base, deck, "MAT1", material_id, ("MID",))
    if mat_row["fields"].get("MID") != material_id:
        raise RuntimeError(f"MAT1 {material_id} card MID does not match its identity")
    if "DEFINED" in mat_row["fields"] and str(mat_row["fields"]["DEFINED"]).casefold() != "yes":
        raise ValueError(f"MAT1 {material_id} must be defined")
    return prop, {"psolid": prop_row, "mat1": mat_row, "material_id": material_id}


def _capture_freeze_scope(base, deck):
    snapshot = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
    for row in snapshot["entities"]:
        reference = row["entity"]
        entity, actual = catalog._get_entity(base, deck, reference["type"], reference["id"])
        if actual != reference:
            raise RuntimeError("entity identity changed during freeze scope capture")
        raw = volume_meshing._read_full_card(base, deck, entity, reference,
                                              f"{reference['type']} {reference['id']}")
        row.update(raw)
        row.pop("ignored_derived_fields", None)
    if len(volume_meshing._encoded(snapshot)) > _SWEEP_SNAPSHOT_BUDGET:
        raise ValueError("raw freeze scope exceeds the 2048 KiB evidence budget")
    return snapshot


def _read_source_frozen(base, deck, shell_ids):
    states, errors = {}, []
    for shell_id in shell_ids:
        try:
            entity, reference = catalog._get_entity(base, deck, "SHELL", shell_id)
            if reference["type"] != "SHELL" or reference["id"] != shell_id:
                raise RuntimeError("SHELL identity mismatch")
            labels = volume_meshing._card_fields(entity, deck, f"SHELL {shell_id}")
            if "FROZEN" not in labels:
                raise RuntimeError("FROZEN card field is unavailable")
            values = catalog._read_card(base, deck, entity, reference, ("FROZEN",))
            value = values.get("FROZEN")
            states[str(shell_id)] = value
            if value not in ("NO", "YES"):
                raise RuntimeError("FROZEN value is not explicit YES or NO")
        except Exception as exc:
            errors.append(f"SHELL {shell_id}: {_error_text(exc)}")
    return states, errors


def _freeze_scope_errors(before, after, shell_ids):
    old, new = _scope_map(before), _scope_map(after)
    errors = []
    if set(old) != set(new):
        errors.append("entity identities changed during source freeze")
    for key in sorted(set(old) & set(new)):
        expected = old[key]
        if key[0] == "SHELL" and key[1] in shell_ids:
            if "FROZEN" not in expected.get("card_fields", []) or expected["fields"].get("FROZEN") != "NO":
                errors.append(f"SHELL {key[1]} was not explicitly unfrozen in the scope snapshot")
                continue
            expected = {**expected, "fields": {**expected["fields"], "FROZEN": "YES"}}
        if not catalog._strict_equal(expected, new[key]):
            errors.append(f"unexpected change during source freeze: {key[0]} {key[1]}")
    return errors


def _run_sweep(state, base, mesh, constants, params):
    clean = _normalize_sweep(params)
    deck = inspection._deck(base, clean)
    _check_nastran(constants, deck)
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    extrude_type = getattr(mesh, "VolumesExtrude", None)
    if not callable(extrude_type):
        raise NotImplementedError("ANSA mesh.VolumesExtrude is unavailable")
    if not callable(getattr(extrude_type, "translate", None)):
        raise NotImplementedError("ANSA mesh.VolumesExtrude.translate is unavailable")
    if clean["freeze_source"]:
        freeze = inspection._require_api(base, "FreezeElements", "ansa.base.FreezeElements")
    context = volume_meshing._execution_context(state, deck)
    before = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
    source = capture_source(base, constants, deck, clean["shell_ids"])
    if source["snapshot_sha256"] != clean["expected_source_sha256"]:
        raise ValueError("expected_source_sha256 does not match the explicit SHELL source snapshot")
    source_audit = _source_preflight(source, clean)
    prop, _property_snapshot = _psolid_preflight(base, deck, clean["property_id"])
    get_current = inspection._require_api(base, "GetCurrentEntity", "ansa.base.GetCurrentEntity")
    set_current = inspection._require_api(base, "SetCurrentEntity", "ansa.base.SetCurrentEntity")
    previous_property = get_current(deck, "PSOLID")
    before_source_shells = _entity_ids(before, "SHELL")
    if not set(clean["shell_ids"]).issubset(before_source_shells):
        raise RuntimeError("source SHELL IDs are not all present in the full model snapshot")
    backup_path, current_path, current_fingerprint = _backup_preflight(base, clean)
    volume_meshing._same_context(state, deck, context)

    backup = None
    try:
        backup = _verify_backup(base, constants, backup_path, current_path, current_fingerprint)
    except Exception as exc:
        backup = _unconfirmed_backup(backup_path)
        try:
            after_save = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
            possible = after_save != before
            possible = possible or not model_files._same_path(current_path, volume_meshing._database_name(base))
            possible = possible or not catalog._strict_equal(
                current_fingerprint, model_files._fingerprint(Path(current_path))
            )
            volume_meshing._same_context(state, deck, context)
        except Exception:
            after_save, possible = None, True
        return _backup_failure_result(
            "mesh_sweep", deck, backup, before, after_save, _error_text(exc),
            possible_effects=possible,
        )

    try:
        volume_meshing._same_context(state, deck, context)
        after_backup = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
        source_after_backup = capture_source(base, constants, deck, clean["shell_ids"])
        if after_backup != before or source_after_backup["snapshot_sha256"] != clean["expected_source_sha256"]:
            raise RuntimeError("model or source changed after backup; native call was not made")
        volume_meshing._same_context(state, deck, context)
    except Exception as exc:
        return _backup_failure_result(
            "mesh_sweep", deck, backup, before, locals().get("after_backup"),
            _error_text(exc), possible_effects=True,
        )

    source_entities = [catalog._get_entity(base, deck, "SHELL", shell_id)[0]
                       for shell_id in clean["shell_ids"]]
    sweep_before = before
    freeze_report = None
    if clean["freeze_source"]:
        try:
            freeze_before = _capture_freeze_scope(base, deck)
            volume_meshing._same_context(state, deck, context)
        except Exception as exc:
            result = _backup_failure_result("mesh_sweep", deck, backup, before, after_backup,
                                            "freeze preflight: " + _error_text(exc))
            result.update({"freeze_source": True, "freeze": {"api_called": False,
                "before": None, "after_freeze": None, "after_sweep": None},
                "recovery_required": False})
            return result
        states_before, frozen_errors = _read_source_frozen(base, deck, clean["shell_ids"])
        if any(value != "NO" for value in states_before.values()) or len(states_before) != len(clean["shell_ids"]):
            frozen_errors.append("all source SHELLs must be explicitly FROZEN=NO")
        scope_rows = _scope_map(freeze_before)
        for shell_id in clean["shell_ids"]:
            row = scope_rows[("SHELL", shell_id)]
            if "FROZEN" not in row.get("card_fields", []) or row["fields"].get("FROZEN") != "NO":
                frozen_errors.append(f"SHELL {shell_id} scope does not show FROZEN=NO")
        if frozen_errors:
            result = _backup_failure_result("mesh_sweep", deck, backup, before, after_backup,
                                            "; ".join(frozen_errors))
            result.update({"freeze_source": True, "freeze": {"api_called": False,
                "before": states_before, "after_freeze": None, "after_sweep": None},
                "recovery_required": False})
            return result
        freeze_report = {"api_called": True, "before": states_before,
                         "after_freeze": None, "after_sweep": None}
        freeze_errors = []
        try:
            volume_meshing._same_context(state, deck, context)
            freeze_status = freeze(deck, "0", source_entities)
            freeze_report["api_return"] = freeze_status if type(freeze_status) is bool else None
            freeze_report["api_return_type"] = type(freeze_status).__name__
            if freeze_status is not True:
                freeze_errors.append("FreezeElements did not return the exact success value bool true")
        except Exception as exc:
            freeze_errors.append("FreezeElements: " + _error_text(exc))
        try:
            states_after, read_errors = _read_source_frozen(base, deck, clean["shell_ids"])
            freeze_report["after_freeze"] = states_after
            freeze_errors.extend(read_errors)
            if len(states_after) != len(clean["shell_ids"]) or any(v != "YES" for v in states_after.values()):
                freeze_errors.append("not all source SHELLs read back FROZEN=YES")
            freeze_after = _capture_freeze_scope(base, deck)
            freeze_errors.extend(_freeze_scope_errors(freeze_before, freeze_after, set(clean["shell_ids"])))
            freeze_report["scope_sha256_before"] = _hash(freeze_before)
            freeze_report["scope_sha256_after_freeze"] = _hash(freeze_after)
            sweep_before = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
            frozen_source = capture_source(base, constants, deck, clean["shell_ids"])
            freeze_report["source_sha256_before"] = source["snapshot_sha256"]
            freeze_report["source_sha256_after_freeze"] = frozen_source["snapshot_sha256"]
            if frozen_source["snapshot_sha256"] != source["snapshot_sha256"]:
                freeze_errors.append("source node/connectivity snapshot changed during freeze")
            volume_meshing._same_context(state, deck, context)
        except Exception as exc:
            freeze_errors.append("freeze readback: " + _error_text(exc))
        if freeze_errors:
            result = _backup_failure_result("mesh_sweep", deck, backup, before,
                                            locals().get("freeze_after"), "; ".join(freeze_errors),
                                            possible_effects=True)
            result.update({"freeze_source": True, "freeze": freeze_report,
                           "recovery_required": True,
                           "recovery": "Inspect the original model and verified backup before any further write; do not retry blindly."})
            return result
    api_status = None
    api_error = None
    try:
        if set_current(prop) != 0:
            raise RuntimeError("cannot select requested PSOLID before creating native extrusion")
        extrude = extrude_type()
        translate = getattr(extrude, "translate", None)
        if not callable(translate):
            raise NotImplementedError("ANSA mesh.VolumesExtrude.translate is unavailable")
        api_status = translate(
            source=source_entities,
            axis=((0.0, 0.0, 0.0), tuple(clean["direction"])),
            steps=clean["steps"],
            distance=clean["distance"],
            property=prop,
        )
    except Exception as exc:
        api_error = _error_text(exc)
    finally:
        try:
            status = (set_current(previous_property) if previous_property is not None else
                      set_current("PSOLID", current=None, deck=deck))
            if status != 0:
                raise RuntimeError("current PSOLID restoration failed")
        except Exception as exc:
            api_error = (api_error + "; " if api_error else "") + _error_text(exc)

    volume_boundary_error = None
    try:
        after = _capture_scope(base, deck, budget=_SWEEP_SNAPSHOT_BUDGET)
        volume_boundary_faces = None
        if _face_volume_count_changed(sweep_before, after):
            old_keys, new_keys = set(_scope_map(sweep_before)), set(_scope_map(after))
            new_volume_ids = sorted(
                entity_id for kind, entity_id in new_keys - old_keys if kind == "VOLUME"
            )
            try:
                volume_boundary_faces = _collect_sweep_volume_boundary_faces(
                    base, mesh, deck, new_volume_ids, clean["shell_ids"], sweep_before, after
                )
            except Exception as exc:
                volume_boundary_error = _error_text(exc)
        diff = _sweep_scope_diff(
            sweep_before, after, clean, volume_boundary_faces=volume_boundary_faces
        )
        new_solid_ids = diff["new_solid_ids"]
        if len(new_solid_ids) > _MAX_NEW_SOLIDS:
            raise ValueError("post-sweep SOLID count exceeds the configured limit")
        output = capture_mesh(base, constants, deck, new_solid_ids) if new_solid_ids else None
        if output is None:
            raise ValueError("post-sweep readback found no new SOLID elements")
        mesh_report = _verify_sweep_mesh(source, output, diff["new_grid_ids"], clean)
        new_mesh_error = None
    except Exception as exc:
        after = locals().get("after")
        diff = locals().get("diff")
        output = locals().get("output")
        mesh_report = None
        new_mesh_error = _error_text(exc)
    try:
        source_after = capture_source(base, constants, deck, clean["shell_ids"])
        source_unchanged = source_after["snapshot_sha256"] == clean["expected_source_sha256"]
        source_error = None if source_unchanged else "source SHELL snapshot changed after sweep"
    except Exception as exc:
        source_after, source_unchanged, source_error = None, False, _error_text(exc)
    frozen_after_error = None
    if freeze_report is not None:
        states_after_sweep, read_errors = _read_source_frozen(base, deck, clean["shell_ids"])
        freeze_report["after_sweep"] = states_after_sweep
        freeze_report["source_sha256_after_sweep"] = source_after["snapshot_sha256"] if source_after else None
        if read_errors or len(states_after_sweep) != len(clean["shell_ids"]) or any(
                value != "YES" for value in states_after_sweep.values()):
            frozen_after_error = "; ".join(read_errors + ["source SHELLs are not all verified FROZEN=YES after sweep"])
    try:
        volume_meshing._same_context(state, deck, context)
        context_error = None
    except Exception as exc:
        context_error = _error_text(exc)

    errors = list(diff["errors"] if diff is not None else [])
    if api_error:
        errors.insert(0, api_error)
    elif type(api_status) is not int or api_status != 1:
        errors.insert(0, "VolumesExtrude.translate did not return the exact success value int 1")
    if new_mesh_error:
        errors.append("new SOLID geometry/connectivity verification: " + new_mesh_error)
    if volume_boundary_error:
        errors.append("new VOLUME boundary/source FACE verification: " + volume_boundary_error)
    if source_error:
        errors.append("source SHELL verification: " + source_error)
    if frozen_after_error:
        errors.append("post-sweep freeze verification: " + frozen_after_error)
    if context_error:
        errors.append("post-sweep model context: " + context_error)
    scope_ok = (diff is not None and diff["scope_verified"] and new_mesh_error is None
                and volume_boundary_error is None and source_unchanged and frozen_after_error is None)
    api_ok = api_error is None and type(api_status) is int and api_status == 1
    ok = api_ok and scope_ok and context_error is None
    result = {
        "ok": ok,
        "tool": "mesh_sweep",
        "deck": deck,
        "shell_ids": clean["shell_ids"],
        "direction": clean["direction"],
        "distance": clean["distance"],
        "steps": clean["steps"],
        "property_id": clean["property_id"],
        "expected_source_sha256": clean["expected_source_sha256"],
        "source_audit": source_audit,
        "backup": backup,
        "before": _evidence(before),
        "after": _evidence(after),
        "source_after": source_after,
        "api_called": True,
        "api_return_code": api_status,
        "api_return_type": type(api_status).__name__ if api_error is None else None,
        "api_error": api_error,
        "scope_verified": scope_ok,
        "possible_effects": not ok,
        "new_grid_ids": diff["new_grid_ids"] if diff else [],
        "new_solid_ids": diff["new_solid_ids"] if diff else [],
        "new_volume_ids": diff["new_volume_ids"] if diff else [],
        "native_auxiliary_properties": diff["native_auxiliary_properties"] if diff else [],
        "native_auxiliary_materials": diff["native_auxiliary_materials"] if diff else [],
        "new_mesh": {
            "snapshot_sha256": output["snapshot_sha256"],
            "integrity": output["integrity"],
            "verification": mesh_report,
        } if output is not None and mesh_report is not None else None,
        "error": "; ".join(errors) if errors else None,
        "errors": errors,
    }
    if freeze_report is not None:
        result.update({"freeze_source": True, "freeze": freeze_report,
                       "after_freeze": _evidence(sweep_before), "recovery_required": not ok})
        if not ok:
            result["recovery"] = "Inspect the original model and verified backup before any further write; do not retry blindly."
    return result


def execute(state, tool, params):
    """Dispatch read-only plan or one-shot native write; never retry an uncertain API call."""
    state.assert_main()
    if type(tool) is not str or tool not in {
        "geometry_partition_plan", "geometry_partition_apply", "mesh_sweep"
    }:
        raise ValueError(f"unsupported structured native operation: {tool!r}")
    from ansa import base, constants, mesh

    if tool == "geometry_partition_plan":
        return _public_plan(_partition_plan(state, base, mesh, constants, params))
    if tool == "geometry_partition_apply":
        return _run_partition(state, base, mesh, constants, params)
    return _run_sweep(state, base, mesh, constants, params)


__all__ = ["capture_mesh", "capture_source", "execute"]
