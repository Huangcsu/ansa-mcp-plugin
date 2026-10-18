"""Read-only ABAQUS TIE and CONTACT_PAIR card and surface inspection."""

from __future__ import annotations

import json
import hashlib
import math

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from .abaqus_surfaces import surface_members


_TOOL = "abaqus_contact_inspect"
_PAGE_SIZE = 200
_MAX_MEMBERS_PER_SET = 2000
_MAX_FACE_RECORDS_PER_SET = 20
_MAX_FACE_RECORDS_PER_REQUEST = 200
_MAX_ANALYZED_FACES_PER_REQUEST = 10000
_MAX_PROJECTION_TESTS_PER_REQUEST = 100000
_RESULT_BUDGET = MAX_FRAME_BYTES * 3 // 4
_EPSILON = 1.0e-12

_TIE_OPTIONS = (
    "REGION TYPE_S", "TRIM_S", "REGION TYPE_M", "TRIM_M", "PARAM",
    "DISTANCE", "ADJUST", "ROTATION", "CYCLIC SYMMETRY",
    "TYPE_tie_coefficients", "NO THICKNESS",
)
_CONTACT_OPTIONS = (
    "STEP", "TYPE", "INTERFERENCE", "REGION TYPE_S", "TRIM_S", "OFFSET_S",
    "THICK_S", "MAX RATIO_S", "SCL.THICK_S", "REGION TYPE_M", "TRIM_M",
    "OFFSET_M", "THICK_M", "MAX RATIO_M", "SCL.THICK_M", "HCRIT",
    "EXT.ZONE", "SMOOTH", "ADJUST", "TIED", "SMALL SLIDING",
    "MECHANICAL CONSTRAINT", "MINIMUM DISTANCE", "GEOM. CORRECTION", "WEIGHT",
    "TYPE_tie_coefficients", "MIDFACE NODES", "NO THICKNESS", "CONSTR.POS.",
    "SUPPL.CONST.", "TRACKING",
)


class _ReadError(RuntimeError):
    pass


def _positive_id(value, label):
    if type(value) is int:
        result = value
    elif type(value) is str and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise _ReadError(f"{label} is not a positive integer")
    if result <= 0:
        raise _ReadError(f"{label} is not a positive integer")
    return result


def _finite_number(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _ReadError(f"{label} is not a finite number")
    result = float(value)
    if positive and result <= 0:
        raise _ReadError(f"{label} must be positive")
    return result


def _json_value(value):
    """Return a strict-JSON copy, without converting unknown values to null."""
    if value is None or type(value) in (str, bool, int):
        return value, True
    if type(value) is float:
        return (value, True) if math.isfinite(value) else (None, False)
    if type(value) in (list, tuple):
        output = []
        complete = True
        for item in value:
            encoded, readable = _json_value(item)
            output.append(encoded if readable else {
                "available": False, "reason": "value is not strict JSON"
            })
            complete = complete and readable
        return output, complete
    if type(value) is dict and all(type(key) is str for key in value):
        output = {}
        complete = True
        for key, item in value.items():
            encoded, readable = _json_value(item)
            output[key] = encoded if readable else {
                "available": False, "reason": "value is not strict JSON"
            }
            complete = complete and readable
        return output, complete
    return None, False


def _read_full_entity(base, deck, entity_type, entity_id):
    entity_id = _positive_id(entity_id, f"{entity_type} id")
    get_entity = getattr(base, "GetEntity", None)
    reader = getattr(base, "GetEntityCardValues", None)
    if not callable(get_entity) or not callable(reader):
        raise _ReadError("GetEntity or GetEntityCardValues is unavailable")
    entity = get_entity(deck, entity_type, entity_id)
    if entity is None:
        raise _ReadError(f"{entity_type} {entity_id} was not found")

    identity = reader(deck, entity, ("__id__", "__type__"))
    if type(identity) is not dict or "__id__" not in identity or "__type__" not in identity:
        raise _ReadError(f"{entity_type} {entity_id} identity fields were not returned")
    actual_id = _positive_id(identity["__id__"], f"{entity_type} __id__")
    actual_type = identity["__type__"]
    if actual_id != entity_id or actual_type != entity_type:
        raise _ReadError(
            f"resolved entity identity is {actual_type!r}/{actual_id}, "
            f"expected {entity_type}/{entity_id}"
        )

    card_fields = getattr(entity, "card_fields", None)
    if not callable(card_fields):
        raise _ReadError(f"{entity_type} {entity_id} has no card_fields method")
    labels = card_fields(deck)
    if (type(labels) not in (list, tuple) or not labels
            or any(type(label) is not str or not label for label in labels)):
        raise _ReadError(f"{entity_type} {entity_id} card_fields is invalid")
    if len(set(labels)) != len(labels):
        raise _ReadError(f"{entity_type} {entity_id} card_fields contains duplicates")

    native = reader(deck, entity, tuple(labels))
    if type(native) is not dict:
        raise _ReadError(f"{entity_type} {entity_id} card values are not an object")
    values = {}
    complete = True
    for label in labels:
        if label not in native:
            values[label] = {
                "available": False,
                "reason": "GetEntityCardValues omitted a field returned by card_fields",
            }
            complete = False
            continue
        encoded, readable = _json_value(native[label])
        values[label] = encoded if readable else {
            "available": False, "reason": "native value is not strict JSON"
        }
        complete = complete and readable

    card = {"fields": list(labels), "values": values, "complete": complete}
    return entity, card, native, complete


def _vector_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vector_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vector_scale(vector, scale):
    return (vector[0] * scale, vector[1] * scale, vector[2] * scale)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def quad4_metrics(points):
    """Area and winding normal for a quad4 split along node diagonal 1-3."""
    if type(points) not in (list, tuple) or len(points) != 4:
        raise ValueError("quad4 requires exactly four node coordinates")
    coords = []
    for point in points:
        if type(point) not in (list, tuple) or len(point) != 3:
            raise ValueError("each node coordinate must contain three numbers")
        coords.append(tuple(_finite_number(v, "node coordinate") for v in point))
    first = _cross(_vector_sub(coords[1], coords[0]), _vector_sub(coords[2], coords[0]))
    second = _cross(_vector_sub(coords[2], coords[0]), _vector_sub(coords[3], coords[0]))
    first_length = math.hypot(*first)
    second_length = math.hypot(*second)
    scale = max((math.hypot(*_vector_sub(coords[i], coords[0])) for i in range(1, 4)), default=0.0)
    area_epsilon = _EPSILON * max(1.0, scale * scale)
    if first_length <= area_epsilon or second_length <= area_epsilon:
        raise ValueError("quad4 has a degenerate triangle in diagonal 1-3 split")
    alignment = _dot(first, second)
    if alignment <= _EPSILON * first_length * second_length:
        raise ValueError("quad4 split triangles are not consistently oriented/convex")

    normal_sum = _vector_add(first, second)
    normal_length = math.hypot(*normal_sum)
    if not math.isfinite(normal_length) or normal_length <= area_epsilon:
        raise ValueError("quad4 is degenerate or has no stable winding normal")
    normal = tuple(component / normal_length for component in normal_sum)
    turns = (
        _cross(_vector_sub(coords[1], coords[0]), _vector_sub(coords[2], coords[1])),
        _cross(_vector_sub(coords[2], coords[1]), _vector_sub(coords[3], coords[2])),
        _cross(_vector_sub(coords[3], coords[2]), _vector_sub(coords[0], coords[3])),
        _cross(_vector_sub(coords[0], coords[3]), _vector_sub(coords[1], coords[0])),
    )
    if any(_dot(turn, normal) <= area_epsilon for turn in turns):
        raise ValueError("quad4 boundary is concave, self-intersecting, or inconsistently ordered")

    area = 0.5 * (first_length + second_length)
    if (not math.isfinite(area) or area <= _EPSILON * max(1.0, scale * scale)
            or not math.isfinite(normal_length)
            or normal_length <= _EPSILON * max(1.0, area)):
        raise ValueError("quad4 is degenerate or has no stable winding normal")
    return area, normal


def _point_segment_closest(point, start, end):
    edge = _vector_sub(end, start)
    length_sq = _dot(edge, edge)
    if length_sq <= _EPSILON:
        return start
    ratio = max(0.0, min(1.0, _dot(_vector_sub(point, start), edge) / length_sq))
    return _vector_add(start, _vector_scale(edge, ratio))


def _triangle_projection(point, triangle):
    a, b, c = triangle
    cross = _cross(_vector_sub(b, a), _vector_sub(c, a))
    length = math.hypot(*cross)
    if not math.isfinite(length) or length <= _EPSILON:
        raise _ReadError("degenerate triangle in quad4 projection")
    normal = tuple(value / length for value in cross)
    signed_distance = _dot(_vector_sub(point, a), normal)
    projected = _vector_sub(point, _vector_scale(normal, signed_distance))

    v0, v1, v2 = _vector_sub(b, a), _vector_sub(c, a), _vector_sub(projected, a)
    d00, d01, d11 = _dot(v0, v0), _dot(v0, v1), _dot(v1, v1)
    d20, d21 = _dot(v2, v0), _dot(v2, v1)
    denominator = d00 * d11 - d01 * d01
    if abs(denominator) <= _EPSILON * max(1.0, d00 * d11):
        raise _ReadError("triangle projection is numerically singular")
    second = (d11 * d20 - d01 * d21) / denominator
    third = (d00 * d21 - d01 * d20) / denominator
    inside = second >= -1.0e-10 and third >= -1.0e-10 and second + third <= 1.0 + 1.0e-10
    if inside:
        closest = projected
    else:
        candidates = (
            _point_segment_closest(point, a, b),
            _point_segment_closest(point, b, c),
            _point_segment_closest(point, c, a),
        )
        closest = min(candidates, key=lambda candidate: math.hypot(*_vector_sub(point, candidate)))
    distance = math.hypot(*_vector_sub(point, closest))
    return distance, inside, abs(signed_distance), projected


def _read_node_coordinates(base, deck, node_id, cache):
    if node_id in cache:
        return cache[node_id]
    node, _card, _native, _complete = _read_full_entity(base, deck, "NODE", node_id)
    labels = node.card_fields(deck)
    if any(label not in labels for label in ("X", "Y", "Z")):
        raise _ReadError(f"NODE {node_id} card_fields does not provide X/Y/Z")
    native = base.GetEntityCardValues(deck, node, ("X", "Y", "Z"))
    if type(native) is not dict or any(label not in native for label in ("X", "Y", "Z")):
        raise _ReadError(f"NODE {node_id} did not return all X/Y/Z coordinates")
    point = tuple(_finite_number(native[label], f"NODE {node_id}.{label}") for label in ("X", "Y", "Z"))
    cache[node_id] = point
    return point


def _section_offset(section_values, section_fields):
    if "Offset Type" not in section_fields:
        return {"status": "unavailable", "resolved": False, "value": None}
    raw_kind = section_values.get("Offset Type")
    offset_present = "OFFSET" in section_fields
    raw_offset = section_values.get("OFFSET") if offset_present else None
    if raw_kind in ("SPOS", "SNEG") and (not offset_present or raw_offset is None):
        ratio = 0.5 if raw_kind == "SPOS" else -0.5
        return {
            "status": "known_symbolic", "resolved": True,
            "type": raw_kind, "raw_value": raw_offset,
            "offset_field_present": offset_present,
            "offset_ratio": ratio, "applied_to_reference_geometry": False,
        }
    if raw_kind in ("", " ") and offset_present:
        try:
            value = _finite_number(raw_offset, "SHELL_SECTION.OFFSET")
        except _ReadError:
            return {
                "status": "unknown", "resolved": False,
                "type": raw_kind, "offset_field_present": True,
                "raw_value": raw_offset, "offset_ratio": None,
                "applied_to_reference_geometry": False,
            }
        return {
            "status": "known_numeric", "resolved": True,
            "type": raw_kind, "offset_field_present": True,
            "raw_value": value, "offset_ratio": value,
            "applied_to_reference_geometry": False,
        }
    return {
        "status": "unknown", "resolved": False,
        "type": raw_kind, "offset_field_present": offset_present,
        "raw_value": raw_offset, "offset_ratio": None,
        "applied_to_reference_geometry": False,
    }


def _element_offset(shell_values, shell_fields):
    if "ZOFFS" not in shell_fields:
        return {
            "status": "unavailable", "raw_value": {
                "available": False, "reason": "ZOFFS is missing from SHELL card_fields",
            }, "value": None, "override_applied": None, "resolved": False,
            "supported_for_physical_geometry": False,
        }
    if "ZOFFS" not in shell_values:
        return {
            "status": "unavailable", "raw_value": {
                "available": False, "reason": "GetEntityCardValues omitted ZOFFS",
            }, "value": None, "override_applied": None, "resolved": False,
            "supported_for_physical_geometry": False,
        }

    raw_value = shell_values["ZOFFS"]
    if raw_value is None or (type(raw_value) is str and not raw_value.strip()):
        return {
            "status": "unset", "raw_value": None, "value": None,
            "override_applied": False, "resolved": True,
            "supported_for_physical_geometry": True,
        }

    try:
        value = _finite_number(raw_value, "SHELL.ZOFFS")
    except _ReadError:
        encoded, readable = _json_value(raw_value)
        return {
            "status": "unavailable",
            "raw_value": encoded if readable else {
                "available": False, "reason": "ZOFFS value is not strict JSON",
            },
            "value": encoded if readable else None,
            "override_applied": None, "resolved": False,
            "supported_for_physical_geometry": False,
        }

    supported = value == 0.0
    return {
        "status": "explicit_zero" if supported else "unsupported_nonzero",
        "raw_value": value, "value": value, "override_applied": True,
        "resolved": True, "supported_for_physical_geometry": supported,
    }


def _shell_details(base, deck, row, shell_cache, section_cache):
    shell_id = _positive_id(row.get("id"), "SHELL member id")
    if shell_id not in shell_cache:
        _entity, card, values, card_complete = _read_full_entity(base, deck, "SHELL", shell_id)
        required = ("type", "PID", "G1", "G2", "G3", "G4")
        missing = [field for field in required if field not in card["fields"] or field not in values]
        if missing:
            raise _ReadError("SHELL card_fields is missing " + ", ".join(missing))
        if values["type"] != "QUAD":
            raise _ReadError(f"SHELL {shell_id} shape {values['type']!r} is not four-node QUAD")
        connectivity = [_positive_id(values[f"G{i}"], f"SHELL {shell_id}.G{i}") for i in range(1, 5)]
        if connectivity != row.get("node_ids"):
            raise _ReadError(f"SHELL {shell_id} card connectivity does not match expanded SET member")
        pid = _positive_id(values["PID"], f"SHELL {shell_id}.PID")
        if pid not in section_cache:
            _section_entity, section_card, section_values, section_complete = _read_full_entity(
                base, deck, "SHELL_SECTION", pid
            )
            section_cache[pid] = (section_card, section_values, section_complete)
        section_card, section_values, section_complete = section_cache[pid]
        try:
            thickness = _finite_number(section_values.get("T"), f"SHELL_SECTION {pid}.T", positive=True)
        except _ReadError:
            thickness = None
        thickness_fields_present = all(
            f"t{i}" in card["fields"] and f"t{i}" in values for i in range(1, 5)
        )
        node_thickness = {f"t{i}": values.get(f"t{i}") for i in range(1, 5)}
        node_thickness_clear = thickness_fields_present and all(
            type(node_thickness[f"t{i}"]) in (int, float)
            and math.isfinite(node_thickness[f"t{i}"])
            and node_thickness[f"t{i}"] == 0
            for i in range(1, 5)
        )
        offset = _section_offset(section_values, section_card["fields"])
        element_offset = _element_offset(values, card["fields"])
        zoffs_supported = element_offset["supported_for_physical_geometry"]
        detail_complete = (
            card_complete and section_complete and thickness is not None
            and node_thickness_clear and offset["resolved"] and zoffs_supported
        )
        shell_cache[shell_id] = {
            "card": card,
            "values": values,
            "section_id": pid,
            "section_thickness": thickness,
            "node_thickness": node_thickness,
            "node_thickness_clear": node_thickness_clear,
            "section_offset": offset,
            "element_offset": element_offset,
            "zoffs_supported": zoffs_supported,
            "complete": detail_complete,
            "errors": [] if detail_complete else [
                label for label, okay in (
                    ("SHELL or SHELL_SECTION full card read", card_complete and section_complete),
                    ("positive section thickness T", thickness is not None),
                    ("nodal thickness fields t1-t4", node_thickness_clear),
                    ("section offset", offset["resolved"]),
                    ("element ZOFFS is unset or explicitly zero", zoffs_supported),
                ) if not okay
            ],
        }
    return shell_cache[shell_id]


def _uniform_physical_points(points, normal, detail, side):
    if not detail["complete"]:
        return None, None
    side_sign = 1.0 if side == "SPOS" else -1.0
    offset_ratio = detail["section_offset"].get("offset_ratio")
    thickness = detail.get("section_thickness")
    if (type(offset_ratio) not in (int, float) or type(thickness) not in (int, float)
            or not math.isfinite(offset_ratio) or not math.isfinite(thickness)):
        return None, None
    distance = (side_sign * 0.5 - offset_ratio) * thickness
    shifted = [
        _vector_add(point, _vector_scale(normal, distance))
        for point in points
    ]
    return shifted, distance


def _surface_member_geometry(
    base, deck, row, node_cache, shell_cache, section_cache,
    *, include_element_card,
):
    if type(row) is not dict or row.get("complete") is not True:
        raise _ReadError("expanded surface member is incomplete")
    member_type = row.get("type")
    node_ids = row.get("node_ids")
    geometry = row.get("geometry")
    if (type(node_ids) is not list or len(node_ids) != 4
            or len(set(node_ids)) != 4 or type(geometry) is not dict
            or geometry.get("topology") != "quad4"):
        raise _ReadError("surface member is not a verified four-node quad4 face")
    node_ids = [_positive_id(value, "face node id") for value in node_ids]
    points = [_read_node_coordinates(base, deck, node_id, node_cache) for node_id in node_ids]
    area, normal = quad4_metrics(points)
    member = {
        "member_type": member_type,
        "element_id": _positive_id(row.get("id"), "surface member id"),
        "parent_element_id": row.get("parent_element_id"),
        "parent_element_type": row.get("parent_element_type"),
        "face_number": row.get("face_number"),
        "direction": row.get("direction"),
        "orientation": row.get("oriented"),
        "node_ids": node_ids,
        "node_coordinates": [
            {"id": node_id, "xyz": list(point)} for node_id, point in zip(node_ids, points)
        ],
        "area": area,
        "unit_normal": list(normal),
        "geometry_complete": True,
        "complete": True,
        "errors": [],
        "_points": points,
    }
    if member_type == "SHELL":
        detail = _shell_details(base, deck, row, shell_cache, section_cache)
        direction = row.get("oriented")
        oriented_normal = (
            list(normal) if direction == "SPOS"
            else list(_vector_scale(normal, -1.0)) if direction == "SNEG"
            else None
        )
        member.update({
            "oriented_normal": oriented_normal,
            "shell_thickness": {
                "section_id": detail["section_id"],
                "section_value": detail["section_thickness"],
                "mode": "uniform SHELL_SECTION T",
                "nodal_values": detail["node_thickness"],
                "nodal_values_clear": detail["node_thickness_clear"],
                "status": "available" if detail["section_thickness"] is not None else "unavailable",
            },
            "section_offset": detail["section_offset"],
            "element_offset": {
                "field": "ZOFFS", **detail["element_offset"],
            },
            "complete": detail["complete"] and oriented_normal is not None,
            "errors": list(detail["errors"]),
        })
        member["section_offset"]["used_for_uniform_physical_geometry"] = detail["complete"]
        member["uniform_section_physical_nodes"] = {}
        physical_points_by_side = {}
        for physical_side in ("SPOS", "SNEG"):
            shifted, physical_distance = _uniform_physical_points(
                points, normal, detail, physical_side
            )
            if shifted is None:
                member["uniform_section_physical_nodes"][physical_side] = {
                    "available": False,
                    "node_coordinates": None,
                    "offset_distance_along_positive_normal": None,
                }
                physical_points_by_side[physical_side] = None
                continue
            member["uniform_section_physical_nodes"][physical_side] = {
                "available": True,
                "node_coordinates": [
                    {"id": node_id, "xyz": list(point)}
                    for node_id, point in zip(node_ids, shifted)
                ],
                "offset_distance_along_positive_normal": physical_distance,
            }
            physical_points_by_side[physical_side] = shifted
        if oriented_normal is None:
            member["errors"].append("SHELL surface orientation is not explicit as SPOS/SNEG")
        if include_element_card:
            member["element_card"] = detail["card"]
        member["section_id"] = detail["section_id"]
        member["_section_complete"] = detail["complete"]
        member["_physical_points_by_side"] = physical_points_by_side
    elif member_type == "SOLIDFACET" and row.get("parent_element_type") == "HEXA":
        member["oriented_normal"] = None
        member["shell_thickness"] = None
        member["section_offset"] = None
        member["element_offset"] = None
        member["uniform_section_physical_nodes"] = None
        member["_section_complete"] = True
        member["_physical_points_by_side"] = {"SPOS": points, "SNEG": points}
    else:
        raise _ReadError("member is neither supported SHELL QUAD4 nor verified HEXA face")
    member["_triangles"] = (
        (points[0], points[1], points[2]),
        (points[0], points[2], points[3]),
    )
    return member


def _hash_record(hasher, record):
    encoded = json.dumps(
        record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _public_face_record(face):
    return {key: value for key, value in face.items() if not key.startswith("_")}


def _record_omitted_face(face_record_budget, set_id, record):
    tracker = face_record_budget["per_surface"][set_id]
    _hash_record(tracker["omitted_hasher"], record)
    _hash_record(face_record_budget["omitted_hasher"], record)
    tracker["omitted_count"] += 1
    face_record_budget["omitted_count"] += 1


def _encode_result(result):
    return json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _stable_result_bytes(result):
    budget = result.get("output_budget")
    for _ in range(4):
        encoded = _encode_result(result)
        if type(budget) is not dict or budget.get("utf8_bytes") == len(encoded):
            return encoded
        budget["utf8_bytes"] = len(encoded)
    raise RuntimeError("contact inspection output byte count did not stabilize")


def _refresh_face_record_summary(result, face_record_budget):
    included_count = 0
    for surface in result.get("surfaces", []):
        set_id = surface.get("set_id")
        tracker = face_record_budget["per_surface"].get(set_id)
        if tracker is None:
            continue
        faces = surface.get("faces")
        if type(faces) is not list:
            faces = []
            surface["faces"] = faces
        tracker["included_count"] = len(faces)
        included_count += len(faces)
        surface["face_sample_count"] = len(faces)
        surface["face_sample_omitted_count"] = tracker["omitted_count"]
        surface["face_sample_truncated"] = tracker["omitted_count"] > 0
        surface["face_sample_omitted_sha256"] = (
            tracker["omitted_hasher"].hexdigest()
            if tracker["omitted_count"] else None
        )
    face_record_budget["included_count"] = included_count
    result["face_record_summary"] = {
        "limit": _MAX_FACE_RECORDS_PER_REQUEST,
        "included_count": included_count,
        "omitted_count": face_record_budget["omitted_count"],
        "omitted_sha256": (
            face_record_budget["omitted_hasher"].hexdigest()
            if face_record_budget["omitted_count"] else None
        ),
    }


def _mark_result_truncated(result, set_id, message):
    result["complete"] = False
    result["status"] = "partial"
    if message not in result["errors"]:
        result["errors"].append(message)
    for surface in result["surfaces"]:
        if surface.get("set_id") != set_id:
            continue
        surface["complete"] = False
        surface["status"] = "partial"
        if message not in surface["errors"]:
            surface["errors"].append(message)
    for relation in result["relationships"]:
        references_surface = any(
            type(reference) is dict and reference.get("set_id") == set_id
            for reference in (relation.get("master"), relation.get("slave"))
        )
        if not references_surface:
            continue
        relation["complete"] = False
        relation["status"] = "partial"
        if message not in relation["errors"]:
            relation["errors"].append(message)
        comparison = relation.get("geometry_comparison")
        if type(comparison) is dict:
            comparison["complete"] = False
            comparison["status"] = "partial"
            comparison.setdefault("errors", []).append(message)


def _compact_result(result, face_record_budget):
    omitted_message = "result details were omitted to satisfy the UTF-8 byte budget"
    surfaces = []
    for surface in result.get("surfaces", []):
        set_id = surface.get("set_id")
        tracker = face_record_budget["per_surface"].get(set_id, {})
        surfaces.append({
            "set_id": set_id,
            "face_count": surface.get("face_count"),
            "face_sample_count": 0,
            "face_sample_truncated": True,
            "face_sample_omitted_count": tracker.get("omitted_count", 0),
            "face_sample_omitted_sha256": (
                tracker["omitted_hasher"].hexdigest()
                if tracker.get("omitted_count") else None
            ),
            "complete": False,
            "status": "partial",
            "errors": [omitted_message],
        })
    relationships = []
    for relation in result.get("relationships", []):
        compact_relation = {
            "id": relation.get("id"),
            "entity_type": relation.get("entity_type"),
            "status": "partial",
            "complete": False,
            "errors": [omitted_message],
        }
        for key in ("master", "slave"):
            reference = relation.get(key)
            if type(reference) is dict:
                compact_relation[key] = {"set_id": reference.get("set_id")}
        relationships.append(compact_relation)
    compact = {
        "status": "partial",
        "complete": False,
        "read_only": True,
        "deck": result.get("deck"),
        "relation_type": result.get("relation_type"),
        "ids": result.get("ids", []),
        "tolerance": result.get("tolerance"),
        "distance_basis": result.get("distance_basis"),
        "coverage_method": result.get("coverage_method"),
        "relationships": relationships,
        "surfaces": surfaces,
        "face_record_summary": result.get("face_record_summary"),
        "projection_budget": result.get("projection_budget"),
        "errors": [omitted_message],
        "output_budget": result.get("output_budget"),
    }
    compact["output_budget"]["compact_fallback"] = True
    return compact


def _fit_result_budget(result, face_record_budget):
    result["output_budget"] = {
        "limit_bytes": _RESULT_BUDGET,
        "utf8_bytes": 0,
        "truncated": False,
    }
    encoded = _stable_result_bytes(result)
    if len(encoded) <= _RESULT_BUDGET:
        return result

    initial_omitted_count = face_record_budget["omitted_count"]
    result["output_budget"].update({
        "truncated": True,
        "pre_truncation_utf8_bytes": len(encoded),
        "pre_truncation_sha256": hashlib.sha256(encoded).hexdigest(),
        "pre_truncation_hash_scope": "full strict-JSON result before byte-budget truncation",
        "omitted_face_record_count": 0,
    })
    message = "face records were omitted to satisfy the UTF-8 byte budget"
    while len(encoded) > _RESULT_BUDGET:
        candidates = [
            surface for surface in result["surfaces"]
            if type(surface.get("faces")) is list and surface["faces"]
        ]
        remaining = sum(len(surface["faces"]) for surface in candidates)
        if remaining == 0:
            break
        to_drop = max(1, (remaining + 1) // 2)
        for surface in reversed(candidates):
            set_id = surface.get("set_id")
            while to_drop and surface["faces"]:
                record = surface["faces"].pop()
                _record_omitted_face(face_record_budget, set_id, record)
                to_drop -= 1
                _mark_result_truncated(result, set_id, message)
            if not to_drop:
                break
        result["output_budget"]["omitted_face_record_count"] = (
            face_record_budget["omitted_count"] - initial_omitted_count
        )
        _refresh_face_record_summary(result, face_record_budget)
        encoded = _stable_result_bytes(result)

    if len(encoded) > _RESULT_BUDGET:
        result = _compact_result(result, face_record_budget)
        encoded = _stable_result_bytes(result)
    if len(encoded) > _RESULT_BUDGET:
        raise RuntimeError("compact contact inspection result exceeds its UTF-8 byte budget")
    return result


def _read_surface(
    base, constants, deck, set_id, node_cache, shell_cache, section_cache,
    face_record_budget, analysis_budget,
):
    errors = []
    face_tracker = {
        "included_count": 0,
        "omitted_count": 0,
        "omitted_hasher": hashlib.sha256(),
    }
    face_record_budget["per_surface"][set_id] = face_tracker
    try:
        _set_entity, set_card, set_values, set_card_complete = _read_full_entity(
            base, deck, "SET", set_id
        )
    except Exception as exc:
        message = f"SET {set_id} card read failed: {type(exc).__name__}: {exc}"
        return {
            "set_id": set_id, "card": None, "expansion": {"expanded_nested_sets": True, "complete": False},
            "face_count": 0, "analyzed_member_count": 0, "faces": [],
            "face_sample_truncated": False, "face_area_sum": None,
            "shell_sections": [], "complete": False, "status": "partial", "errors": [message],
        }, {
            "complete": False, "reference_complete": False,
            "nodes": {}, "triangles": [], "faces": [], "errors": [message],
        }

    if set_values.get("SID") != set_id:
        errors.append("SET SID field does not match its requested ID")
    if set_values.get("Output as:") != "Surface":
        errors.append("SET Output as: is not the explicit Surface value")
    if not set_card_complete:
        errors.append("SET full card_fields read is incomplete")

    offset = 0
    expected_total = None
    expected_duplicates = None
    pages = 0
    members = []
    expansion_complete = True
    expansion_truncated = False
    while True:
        if offset >= _MAX_MEMBERS_PER_SET and expected_total is not None and expected_total > offset:
            expansion_complete = False
            expansion_truncated = True
            errors.append(f"SET expansion reached the {_MAX_MEMBERS_PER_SET} member inspection limit")
            break
        page = surface_members(base, constants, deck, {
            "deck": deck, "set_id": set_id, "offset": offset,
            "limit": _PAGE_SIZE, "expand_nested": True,
        })
        pages += 1
        if type(page) is not dict or page.get("expanded_nested_sets") is not True:
            expansion_complete = False
            errors.append("surface_members did not confirm nested SET expansion")
            break
        total = page.get("total")
        if type(total) is not int or total < 0:
            expansion_complete = False
            errors.append("surface_members did not return a stable expanded member total")
            break
        if expected_total is None:
            expected_total = total
            expected_duplicates = page.get("duplicate_members_removed")
            if total > _MAX_MEMBERS_PER_SET:
                expansion_complete = False
                expansion_truncated = True
                errors.append(f"SET has {total} expanded members; inspection limit is {_MAX_MEMBERS_PER_SET}")
        elif total != expected_total or page.get("duplicate_members_removed") != expected_duplicates:
            expansion_complete = False
            errors.append("SET membership changed or deduplication changed during pagination")
            break
        rows = page.get("members")
        if type(rows) is not list or page.get("offset") != offset:
            expansion_complete = False
            errors.append("surface_members returned an invalid page or offset")
            break
        if page.get("all_members_readable") is not True:
            expansion_complete = False
            errors.append("surface_members reports unreadable or unsupported members")
        members.extend(rows)
        if (page.get("status") not in ("ok", "empty")
                or any(type(row) is not dict or row.get("complete") is not True for row in rows)):
            expansion_complete = False
        has_more = page.get("has_more")
        if type(has_more) is not bool:
            expansion_complete = False
            errors.append("surface_members has_more state is unknown")
            break
        next_offset = page.get("next_offset")
        if has_more:
            if (type(next_offset) is not int or next_offset != offset + len(rows)
                    or next_offset <= offset or expansion_truncated):
                expansion_complete = False
                errors.append("surface_members pagination did not advance consistently")
                break
            offset = next_offset
            continue
        if next_offset is not None or offset + len(rows) != expected_total:
            expansion_complete = False
            errors.append("surface_members page exhaustion does not match the reported total")
        break

    if expected_total is None:
        expected_total = 0
    if len(members) != expected_total:
        expansion_complete = False

    internal_faces = []
    public_faces = []
    area_sum = 0.0
    member_errors = []
    face_geometry_complete = True
    face_card_complete = True
    for row in members:
        if analysis_budget[0] >= _MAX_ANALYZED_FACES_PER_REQUEST:
            face_geometry_complete = False
            errors.append(
                f"request geometry analysis reached {_MAX_ANALYZED_FACES_PER_REQUEST} face budget"
            )
            break
        analysis_budget[0] += 1
        include_card = (
            face_record_budget["included_count"] < _MAX_FACE_RECORDS_PER_REQUEST
            and len(public_faces) < _MAX_FACE_RECORDS_PER_SET
        )
        try:
            face = _surface_member_geometry(
                base, deck, row, node_cache, shell_cache, section_cache,
                include_element_card=include_card,
            )
            internal_faces.append(face)
            area_sum += face["area"]
            public_face = _public_face_record(face)
            if include_card:
                public_faces.append(public_face)
                face_tracker["included_count"] += 1
                face_record_budget["included_count"] += 1
            else:
                _record_omitted_face(face_record_budget, set_id, public_face)
            if face.get("complete") is not True:
                face_card_complete = False
                member_errors.extend(face.get("errors", []))
        except Exception as exc:
            face_geometry_complete = False
            face_card_complete = False
            member_errors.append(
                f"{row.get('type')} {row.get('id')} geometry unavailable: {type(exc).__name__}: {exc}"
            )

    sample_truncated = face_tracker["omitted_count"] > 0
    if sample_truncated:
        errors.append("face records are sampled; increase is required for a complete face listing")
    errors.extend(member_errors[:20])
    shell_cards = []
    section_ids = sorted({face.get("section_id") for face in internal_faces if face.get("section_id") is not None})
    for section_id in section_ids:
        cached = section_cache.get(section_id)
        if cached is not None:
            shell_cards.append({"id": section_id, "card": cached[0]})
    surface_complete = (
        expansion_complete and face_geometry_complete and face_card_complete
        and set_card_complete and not errors
        and expected_total > 0 and len(internal_faces) == expected_total
        and not sample_truncated
    )
    public = {
        "set_id": set_id,
        "card": set_card,
        "expansion": {
            "expanded_nested_sets": True,
            "complete": expansion_complete,
            "member_count": expected_total,
            "analyzed_member_count": len(internal_faces),
            "pages": pages,
            "limit_per_set": _MAX_MEMBERS_PER_SET,
            "truncated": expansion_truncated,
        },
        "face_count": len(internal_faces),
        "face_area_sum": area_sum if internal_faces else None,
        "faces": public_faces,
        "face_sample_count": len(public_faces),
        "face_sample_truncated": sample_truncated,
        "face_sample_omitted_count": face_tracker["omitted_count"],
        "face_sample_omitted_sha256": (
            face_tracker["omitted_hasher"].hexdigest()
            if face_tracker["omitted_count"] else None
        ),
        "shell_sections": shell_cards,
        "complete": surface_complete,
        "status": "ok" if surface_complete else "partial",
        "errors": errors,
    }
    nodes = {}
    triangles = []
    for face in internal_faces:
        for node_id, point in zip(face["node_ids"], face["_points"]):
            nodes[node_id] = point
        triangles.extend(face["_triangles"])
    internal = {
        "complete": surface_complete,
        "reference_complete": (
            expansion_complete and face_geometry_complete
            and expected_total > 0 and len(internal_faces) == expected_total
        ),
        "nodes": nodes,
        "triangles": triangles,
        "faces": internal_faces,
        "errors": errors,
    }
    return public, internal


def _distance_stats(source_nodes, target_triangles, tolerance, budget):
    distances = []
    projected_inside_count = 0
    within_tolerance = 0
    covered_count = 0
    projection_tests = 0
    errors = []
    ordered_nodes = sorted(source_nodes.items())
    complete = bool(target_triangles)
    for _node_id, point in ordered_nodes:
        nearest = None
        projected_hit = False
        projection_within = False
        for triangle in target_triangles:
            if budget["used"] >= budget["limit"]:
                budget["exhausted"] = True
                complete = False
                break
            budget["used"] += 1
            projection_tests += 1
            try:
                distance, inside, normal_gap, _projection = _triangle_projection(point, triangle)
            except _ReadError as exc:
                complete = False
                errors.append(str(exc))
                break
            nearest = distance if nearest is None else min(nearest, distance)
            if inside:
                projected_hit = True
                if normal_gap <= tolerance:
                    projection_within = True
        if not complete:
            break
        if nearest is None:
            complete = False
            break
        distances.append(nearest)
        if nearest <= tolerance:
            within_tolerance += 1
        if projected_hit:
            projected_inside_count += 1
        if projection_within:
            covered_count += 1
    omitted_source_nodes = len(ordered_nodes) - len(distances)
    complete = complete and omitted_source_nodes == 0
    return {
        "sampled_nodes": len(distances),
        "complete": complete,
        "projection_tests": projection_tests,
        "omitted_source_nodes": omitted_source_nodes,
        "errors": errors,
        "within_tolerance_nodes": within_tolerance,
        "minimum_distance": min(distances) if distances else None,
        "maximum_distance": max(distances) if distances else None,
        "mean_distance": sum(distances) / len(distances) if distances else None,
        "distances": distances,
        "projected_inside_nodes": projected_inside_count,
        "covered_nodes": covered_count,
        "coverage_ratio": covered_count / len(distances) if distances else None,
    }


def _distance_stats_cached(cache, budget, key, source_nodes, target_triangles, tolerance):
    cache_key = (*key, tolerance)
    if cache_key in cache:
        budget["cache_hits"] += 1
        return cache[cache_key]
    stats = _distance_stats(source_nodes, target_triangles, tolerance, budget)
    cache[cache_key] = stats
    return stats


def _physical_surface_geometry(surface, relation_orientation):
    nodes, triangles, sides, errors = {}, [], set(), []
    if relation_orientation not in ("SPOS", "SNEG", "", " "):
        errors.append("relationship orientation is not SPOS/SNEG/blank")
        relation_orientation = None
    for face in surface.get("faces", []):
        member_type = face.get("member_type")
        points = face.get("_points", [])
        if member_type == "SHELL":
            set_side = face.get("orientation")
            side = relation_orientation if relation_orientation in ("SPOS", "SNEG") else set_side
            if (relation_orientation in ("SPOS", "SNEG")
                    and set_side in ("SPOS", "SNEG") and relation_orientation != set_side):
                errors.append(
                    f"SHELL {face.get('element_id')} relationship side conflicts with expanded SET side"
                )
                continue
            if side not in ("SPOS", "SNEG"):
                errors.append(f"SHELL {face.get('element_id')} has no resolved physical surface side")
                continue
            sides.add(side)
            by_side = face.get("_physical_points_by_side", {})
            physical_points = by_side.get(side)
            if type(physical_points) is not list or len(physical_points) != 4:
                errors.append(
                    f"SHELL {face.get('element_id')} physical coordinates are unavailable "
                    "because section thickness/offset is unresolved or unsupported"
                )
                continue
            points = physical_points
        elif member_type != "SOLIDFACET" or len(points) != 4:
            errors.append(f"face {face.get('element_id')} has no supported physical geometry")
            continue
        sample_tag = (
            member_type, face.get("element_id"),
            face.get("orientation") if member_type == "SHELL" else "solid-face",
        )
        for node_id, point in zip(face.get("node_ids", ()), points):
            nodes[(*sample_tag, node_id)] = point
        triangles.extend(((points[0], points[1], points[2]), (points[0], points[2], points[3])))
    if not surface.get("reference_complete"):
        errors.append("surface expansion or reference node geometry is incomplete")
    return {
        "nodes": nodes,
        "triangles": triangles,
        "complete": not errors and bool(nodes) and bool(triangles),
        "sides": sorted(sides),
        "errors": errors,
    }


def _physical_limitations(relation_type, card_values):
    limitations = []
    if relation_type == "TIE":
        if card_values.get("NO THICKNESS") == "YES":
            limitations.append("TIE NO THICKNESS=YES is retained raw; solver-specific thickness behavior is not modeled")
    else:
        if card_values.get("NO THICKNESS") == "YES":
            limitations.append("CONTACT_PAIR NO THICKNESS=YES is retained raw; do not interpret as solver contact geometry")
        for field in ("OFFSET_M", "OFFSET_S", "THICK_M", "THICK_S"):
            value = card_values.get(field)
            if value not in (None, "", " "):
                limitations.append(f"CONTACT_PAIR {field}={value!r} is retained raw but not applied")
    return limitations


def _distance_only(stats):
    return {key: stats[key] for key in (
        "complete", "projection_tests", "omitted_source_nodes", "errors",
        "sampled_nodes", "within_tolerance_nodes", "minimum_distance",
        "maximum_distance", "mean_distance", "distances",
    )}


def _compare_surfaces(
    master, slave, tolerance, master_orientation, slave_orientation,
    relation_type, relation_card_values, master_set_id, slave_set_id,
    distance_cache, projection_budget,
):
    master_to_slave = _distance_stats_cached(
        distance_cache, projection_budget,
        ("reference", master_set_id, slave_set_id),
        master["nodes"], slave["triangles"], tolerance,
    )
    slave_to_master = _distance_stats_cached(
        distance_cache, projection_budget,
        ("reference", slave_set_id, master_set_id),
        slave["nodes"], master["triangles"], tolerance,
    )
    reference_complete = (
        master["reference_complete"] and slave["reference_complete"]
        and master_to_slave["complete"] and slave_to_master["complete"]
    )
    reference_gap = {
        "status": "ok" if reference_complete else "partial",
        "complete": reference_complete,
        "tolerance": tolerance,
        "units": "current ABAQUS model units; no conversion performed",
        "distance_method": "nearest Euclidean distance from unique mesh nodes to piecewise-linear quad4 triangles",
        "master_to_slave_node_distances": _distance_only(master_to_slave),
        "slave_to_master_node_distances": _distance_only(slave_to_master),
        "slave_node_projection_coverage": {
            "sampled_nodes": slave_to_master["sampled_nodes"],
            "projected_inside_master_nodes": slave_to_master["projected_inside_nodes"],
            "covered_nodes_within_tolerance": slave_to_master["covered_nodes"],
            "ratio": slave_to_master["coverage_ratio"],
            "tolerance": tolerance,
            "method": (
                "orthogonal projection of each unique slave mesh node onto master quad4 triangles; "
                "coverage is inside a projected triangle and within the supplied normal-gap tolerance"
            ),
            "claim_scope": "node-sampled projection only; not Abaqus solver TIE coverage or area coverage",
        },
        "distance_basis": "reference mesh NODE X/Y/Z coordinates; shell offsets are not applied",
    }

    master_physical = _physical_surface_geometry(master, master_orientation)
    slave_physical = _physical_surface_geometry(slave, slave_orientation)
    physical_errors = [*master_physical["errors"], *slave_physical["errors"]]
    limitations = _physical_limitations(relation_type, relation_card_values)
    physical_available = bool(
        master_physical["nodes"] and master_physical["triangles"]
        and slave_physical["nodes"] and slave_physical["triangles"]
    )
    master_side_token = master_orientation if type(master_orientation) is str else "<unsupported>"
    slave_side_token = slave_orientation if type(slave_orientation) is str else "<unsupported>"
    if physical_available:
        physical_master_to_slave = _distance_stats_cached(
            distance_cache, projection_budget,
            ("physical", master_set_id, slave_set_id, master_side_token, slave_side_token),
            master_physical["nodes"], slave_physical["triangles"], tolerance,
        )
        physical_slave_to_master = _distance_stats_cached(
            distance_cache, projection_budget,
            ("physical", slave_set_id, master_set_id, slave_side_token, master_side_token),
            slave_physical["nodes"], master_physical["triangles"], tolerance,
        )
    else:
        physical_master_to_slave = physical_slave_to_master = None
    physical_stats_complete = bool(
        physical_master_to_slave and physical_master_to_slave["complete"]
        and physical_slave_to_master and physical_slave_to_master["complete"]
    )
    physical_complete = (
        physical_available and master_physical["complete"] and slave_physical["complete"]
        and physical_stats_complete and not limitations
    )
    if limitations:
        physical_errors.extend(limitations)
    physical_gap = {
        "status": "ok" if physical_complete else "partial" if physical_available else "unavailable",
        "available": physical_available,
        "complete": physical_complete,
        "tolerance": tolerance,
        "units": "current ABAQUS model units; no conversion performed",
        "mode": "uniform SHELL_SECTION thickness T and explicit section offset only",
        "master_side": master_orientation,
        "slave_side": slave_orientation,
        "master_resolved_sides": master_physical["sides"],
        "slave_resolved_sides": slave_physical["sides"],
        "master_to_slave_node_distances": (
            _distance_only(physical_master_to_slave) if physical_master_to_slave else None
        ),
        "slave_to_master_node_distances": (
            _distance_only(physical_slave_to_master) if physical_slave_to_master else None
        ),
        "slave_node_projection_coverage": {
            "available": False,
            "status": "not_computed",
            "reason": "physical-surface coverage and contact-specific projection adjustments are outside this mode",
        },
        "formula": "physical node = reference node + (side*0.5 - offset_ratio)*T*positive_shell_normal",
        "offset_resolution": "SPOS=+0.5, SNEG=-0.5, or an explicitly read finite numeric OFFSET; blank/default is unknown",
        "solver_equivalence_claim": False,
        "limitations": physical_errors,
    }
    complete = reference_complete and physical_complete and master["complete"] and slave["complete"]
    return {
        "status": "ok" if complete else "partial",
        "complete": complete,
        "tolerance": tolerance,
        "reference_gap": reference_gap,
        "uniform_section_physical_gap": physical_gap,
    }


def _parse_relation_card(relation_type, card_values, card_output_values=None):
    errors = []
    card_output_values = card_output_values if type(card_output_values) is dict else {}

    def output_value(field):
        if field in card_output_values:
            return card_output_values[field]
        if field not in card_values:
            return {"available": False, "reason": "native card value is unavailable"}
        encoded, readable = _json_value(card_values[field])
        if readable:
            return encoded
        errors.append(f"{field} value is unavailable in strict JSON")
        return {"available": False, "reason": "native value is not strict JSON"}

    master_type = card_values.get("MAIN")
    slave_type = card_values.get("SECONDARY")
    if master_type != "ELEMENT":
        errors.append("MAIN must be the explicit ELEMENT reference type")
    if slave_type != "ELEMENT":
        errors.append("SECONDARY must be the explicit ELEMENT reference type")
    try:
        master_id = _positive_id(card_values.get("MSID"), "MSID")
    except _ReadError as exc:
        master_id = None
        errors.append(str(exc))
    try:
        slave_id = _positive_id(card_values.get("SSID"), "SSID")
    except _ReadError as exc:
        slave_id = None
        errors.append(str(exc))
    master_orientation = card_values.get("ORIENT_M")
    slave_orientation = card_values.get("ORIENT_S")
    for label, orientation in (("ORIENT_M", master_orientation), ("ORIENT_S", slave_orientation)):
        if orientation not in ("SPOS", "SNEG", "", " "):
            errors.append(f"{label} has an unsupported value")

    option_fields = _TIE_OPTIONS if relation_type == "TIE" else _CONTACT_OPTIONS
    options = {
        field: output_value(field)
        for field in option_fields
        if field in card_values or field in card_output_values
    }
    tie_tolerance = None
    if relation_type == "TIE":
        parameter = card_values.get("PARAM")
        distance = card_values.get("DISTANCE")
        applies = parameter == "POS_TOLER"
        solver_value = None
        if applies:
            try:
                solver_value = _finite_number(distance, "TIE DISTANCE")
            except _ReadError as exc:
                errors.append(str(exc))
        tie_tolerance = {
            "parameter": output_value("PARAM"),
            "distance_raw": output_value("DISTANCE"),
            "solver_position_tolerance_applies": applies,
            "solver_position_tolerance": solver_value,
        }
    return {
        "master_set_id": master_id if master_type == "ELEMENT" else None,
        "slave_set_id": slave_id if slave_type == "ELEMENT" else None,
        "master": {
            "set_id": master_id if master_type == "ELEMENT" else None,
            "member_type": output_value("MAIN"),
            "orientation": output_value("ORIENT_M"),
        },
        "slave": {
            "set_id": slave_id if slave_type == "ELEMENT" else None,
            "member_type": output_value("SECONDARY"),
            "orientation": output_value("ORIENT_S"),
        },
        "options": options,
        "tie_tolerance": tie_tolerance,
        "errors": errors,
    }


def _read_interaction(base, deck, contact_values):
    errors = []
    try:
        interaction_id = _positive_id(contact_values.get("INTERACTION"), "INTERACTION")
        _entity, interaction_card, interaction_values, interaction_complete = _read_full_entity(
            base, deck, "SURFACE_INTERACTION", interaction_id
        )
    except Exception as exc:
        return {
            "surface_interaction": None, "friction": None,
            "complete": False,
            "errors": [f"SURFACE_INTERACTION read failed: {type(exc).__name__}: {exc}"],
        }
    interaction = {
        "id": interaction_id,
        "card": interaction_card,
        "complete": interaction_complete,
    }
    friction_card = None
    friction_complete = True
    friction_switch = interaction_values.get("*FRICTION")
    if friction_switch == "YES":
        try:
            friction_id = _positive_id(interaction_values.get("FRICT_ID"), "FRICT_ID")
            _friction, friction_card, friction_values, friction_complete = _read_full_entity(
                base, deck, "FRICTION", friction_id
            )
            if "ID" not in friction_card["fields"] or friction_values.get("ID") != friction_id:
                raise _ReadError("FRICTION ID field does not match FRICT_ID")
            interaction["friction_id"] = friction_id
        except Exception as exc:
            friction_complete = False
            errors.append(f"FRICTION read failed: {type(exc).__name__}: {exc}")
    elif friction_switch not in ("NO", " ", ""):
        friction_complete = False
        errors.append("SURFACE_INTERACTION *FRICTION has an unsupported value")
    complete = interaction_complete and friction_complete and not errors
    return {
        "surface_interaction": interaction,
        "friction": friction_card,
        "complete": complete,
        "errors": errors,
    }


def _validate_params(params, deck, constants):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    required = {"deck", "relation_type", "ids", "tolerance"}
    extra, missing = params.keys() - required, required - params.keys()
    if missing or extra:
        raise ValueError(
            ("missing parameter(s): " + ", ".join(sorted(missing)) if missing else "")
            + ("; " if missing and extra else "")
            + ("unsupported parameter(s): " + ", ".join(sorted(extra)) if extra else "")
        )
    if type(deck) is not int or deck <= 0 or deck != getattr(constants, "ABAQUS", None):
        raise ValueError("abaqus_contact_inspect supports only the explicit ABAQUS deck")
    if type(params["deck"]) is not int or params["deck"] != deck:
        raise ValueError("params.deck must match the explicit ABAQUS deck")
    relation_type = params["relation_type"]
    if type(relation_type) is not str or relation_type not in ("TIE", "CONTACT_PAIR"):
        raise ValueError("relation_type must be TIE or CONTACT_PAIR")
    ids = params["ids"]
    if type(ids) is not list or not 1 <= len(ids) <= 20:
        raise ValueError("ids must contain 1 to 20 unique positive integers")
    if any(type(item) is not int or item <= 0 for item in ids):
        raise ValueError("ids must contain only positive strict integers")
    if len(set(ids)) != len(ids):
        raise ValueError("ids must not contain duplicates")
    try:
        tolerance = _finite_number(params["tolerance"], "tolerance", positive=True)
    except _ReadError as exc:
        raise ValueError(str(exc)) from exc
    return relation_type, ids, tolerance


def inspect_relationships(base, constants, deck, params):
    """Read specified TIE or CONTACT_PAIR cards and expanded surface geometry."""
    relation_type, ids, tolerance = _validate_params(params, deck, constants)
    surfaces_by_id = {}
    surface_internals = {}
    shell_cache, section_cache, node_cache = {}, {}, {}
    face_record_budget = {
        "included_count": 0,
        "omitted_count": 0,
        "omitted_hasher": hashlib.sha256(),
        "per_surface": {},
    }
    analysis_budget = [0]
    projection_budget = {
        "limit": _MAX_PROJECTION_TESTS_PER_REQUEST,
        "used": 0,
        "cache_hits": 0,
        "exhausted": False,
    }
    distance_cache = {}
    relations = []
    errors = []
    relation_complete = True

    for relation_id in ids:
        relation_errors = []
        try:
            _entity, card, card_values, card_complete = _read_full_entity(
                base, deck, relation_type, relation_id
            )
        except Exception as exc:
            message = f"{relation_type} {relation_id} card read failed: {type(exc).__name__}: {exc}"
            relations.append({
                "id": relation_id, "entity_type": relation_type, "card": None,
                "master": None, "slave": None, "options": {}, "tie_tolerance": None,
                "interaction": None, "geometry_comparison": None,
                "status": "partial", "complete": False, "errors": [message],
            })
            errors.append(message)
            relation_complete = False
            continue

        parsed = _parse_relation_card(relation_type, card_values, card["values"])
        relation_errors.extend(parsed["errors"])
        if not card_complete:
            relation_errors.append("relationship full card_fields read is incomplete")
        interaction = None
        if relation_type == "CONTACT_PAIR":
            interaction = _read_interaction(base, deck, card_values)
            relation_errors.extend(interaction["errors"])

        master_id, slave_id = parsed["master_set_id"], parsed["slave_set_id"]
        for set_id in (master_id, slave_id):
            if set_id is None or set_id in surfaces_by_id:
                continue
            public_surface, internal_surface = _read_surface(
                base, constants, deck, set_id, node_cache, shell_cache, section_cache,
                face_record_budget, analysis_budget,
            )
            surfaces_by_id[set_id] = public_surface
            surface_internals[set_id] = internal_surface

        comparison = None
        if master_id in surface_internals and slave_id in surface_internals:
            comparison = _compare_surfaces(
                surface_internals[master_id], surface_internals[slave_id], tolerance,
                parsed["master"]["orientation"], parsed["slave"]["orientation"],
                relation_type, card_values, master_id, slave_id,
                distance_cache, projection_budget,
            )
            if not comparison["complete"]:
                relation_errors.append("master/slave geometry or face expansion is partial")
        else:
            relation_errors.append("master or slave surface could not be resolved")

        if any(not surfaces_by_id.get(sid, {}).get("complete", False)
               for sid in (master_id, slave_id) if sid is not None):
            relation_errors.append("master or slave surface has incomplete geometry/card data")
        if interaction is not None and not interaction["complete"]:
            relation_errors.append("CONTACT_PAIR interaction read is partial")
        if relation_errors:
            relation_complete = False
            errors.extend(f"{relation_type} {relation_id}: {message}" for message in relation_errors)
        relations.append({
            "id": relation_id,
            "entity_type": relation_type,
            "card": card,
            "master": parsed["master"],
            "slave": parsed["slave"],
            "options": parsed["options"],
            "tie_tolerance": parsed["tie_tolerance"],
            "interaction": interaction,
            "geometry_comparison": comparison,
            "status": "ok" if not relation_errors else "partial",
            "complete": not relation_errors,
            "errors": relation_errors,
        })

    surfaces = [surfaces_by_id[set_id] for set_id in sorted(surfaces_by_id)]
    all_surfaces_complete = all(surface.get("complete") is True for surface in surfaces)
    complete = relation_complete and all_surfaces_complete
    result = {
        "status": "ok" if complete else "partial",
        "complete": complete,
        "read_only": True,
        "deck": deck,
        "relation_type": relation_type,
        "ids": list(ids),
        "tolerance": tolerance,
        "distance_basis": (
            "reference_gap uses ABAQUS NODE X/Y/Z; uniform_section_physical_gap additionally "
            "uses explicit SHELL_SECTION T/offset and resolved SPOS/SNEG sides; no solver geometry is claimed"
        ),
        "coverage_method": (
            "unique slave mesh-node orthogonal projection onto master quad4 triangles; "
            "node-sampled only, not Abaqus solver TIE coverage or area coverage"
        ),
        "relationships": relations,
        "surfaces": surfaces,
        "face_record_summary": {
            "limit": _MAX_FACE_RECORDS_PER_REQUEST,
            "included_count": face_record_budget["included_count"],
            "omitted_count": face_record_budget["omitted_count"],
            "omitted_sha256": (
                face_record_budget["omitted_hasher"].hexdigest()
                if face_record_budget["omitted_count"] else None
            ),
        },
        "projection_budget": dict(projection_budget),
        "errors": errors,
    }
    try:
        return _fit_result_budget(result, face_record_budget)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"contact inspection result is not strict JSON: {exc}") from exc


def execute(state, tool, params):
    """Run one read-only contact inspection on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str or tool != _TOOL:
        raise ValueError(f"unsupported ABAQUS contact operation: {tool!r}")
    from ansa import base, constants

    deck = base.CurrentDeck()
    if type(deck) is not int:
        raise RuntimeError("base.CurrentDeck did not return an integer")
    return inspect_relationships(base, constants, deck, params)


__all__ = ["execute", "inspect_relationships", "quad4_metrics"]
