"""Controlled ABAQUS kinematics bodies, joints, and configurations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import _save_path, element_types as guards, entity_catalog as catalog, model_files


_MAX_MEMBERS = 200
_MAX_JOINTS = 100
_MAX_INSPECT_IDS = 200
_MAX_RESOLVED_MEMBERS = 10000
_MAX_EXISTING_BODIES = 200
_MAX_EXISTING_MEMBERS = 10000
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_RESULT_BUDGET = MAX_FRAME_BYTES * 3 // 4

_MEMBER_TYPES = frozenset({
    "NODE", "SHELL", "SOLID", "SECTION", "SHELL_SECTION", "SOLID_SECTION",
    "ANSAPART",
})
_BODY_FIELDS = frozenset({"EID", "GEN_TYPE", "Name"})
_JOINT_FIELDS = frozenset({
    "ID", "ACTIVE", "TYPE", "DEFINITION", "KIN_BODY_1", "KIN_BODY_2",
    "DIRECTION_TYPE", "ANCHOR_x", "ANCHOR_y", "ANCHOR_z", "Z_x", "Z_y", "Z_z",
    "Name",
})
_CONFIG_FIELDS = frozenset({"EID", "ACTUATOR_JOINT", "ACTUATOR_JOINT_TYPE", "Name"})
_KEYWORD_TYPE_MAP = {
    "KIN_RBODY": "KIN_BODY",
    "KIN_JOINT": "KIN_JOINT",
    "KIN_CONFIG": "KIN_CONFIG",
}
_FIXED_UNITS = {
    "length": "mm",
    "mass": "tonne",
    "force": "newton",
    "time": "sec",
    "angle": "deg",
}


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _bounded_result(result):
    """Keep verified write receipts transportable even when evidence is large."""
    original = _encoded(result)
    if len(original) <= _RESULT_BUDGET:
        return result
    for limit in (20, 5, 1):
        summaries = {}
        def shrink(value, path):
            if type(value) is list:
                if len(value) > limit and not path.endswith(".card_fields"):
                    summaries[path] = {
                        "total": len(value), "returned": limit, "truncated": True,
                        "sha256": hashlib.sha256(_encoded(value)).hexdigest(),
                    }
                    value = value[:limit]
                return [shrink(item, f"{path}[{i}]") for i, item in enumerate(value)]
            if type(value) is dict:
                return {key: shrink(item, f"{path}.{key}") for key, item in value.items()}
            if type(value) is str and len(value.encode("utf-8")) > 4096:
                summaries[path] = {"total_bytes": len(value.encode("utf-8")), "truncated": True,
                                   "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
                return value[:1024]
            return value
        bounded = shrink(result, "result")
        bounded.update(evidence_truncated=True, evidence_summaries=summaries,
                       full_result_sha256=hashlib.sha256(original).hexdigest(),
                       full_result_bytes=len(original))
        if len(_encoded(bounded)) <= _RESULT_BUDGET:
            return bounded
    minimal = {key: result[key] for key in (
        "ok", "tool", "deck", "backup", "possible_effects", "before_counts", "after_counts"
    ) if key in result}
    for key in ("body", "joint", "config", "new_entity"):
        if type(result.get(key)) is dict:
            minimal[key] = {"entity": result[key].get("entity")}
    if "error" in result:
        minimal["error"] = str(result["error"])[:2048]
    minimal.update(evidence_truncated=True, full_result_bytes=len(original),
                   full_result_sha256=hashlib.sha256(original).hexdigest(),
                   evidence_summary="Evidence exceeded the response budget; narrow the independent readback scope.")
    return minimal


def _name(value, label="name"):
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{label} must not have leading or trailing whitespace")
    if len(value) > 128:
        raise ValueError(f"{label} must contain at most 128 characters")
    return value


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _vector(value, label, *, nonzero=False):
    if type(value) is not list or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    result = []
    for index, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise TypeError(f"{label}[{index}] must be a number")
        component = float(component)
        if not math.isfinite(component):
            raise ValueError(f"{label}[{index}] must be finite")
        result.append(component)
    if nonzero:
        scale = max(abs(component) for component in result)
        if scale <= 1.0e-12:
            raise ValueError(f"{label} must be a non-zero vector")
        scaled = [component / scale for component in result]
        length = math.hypot(*scaled)
        if not math.isfinite(length) or length <= 1.0e-12:
            raise ValueError(f"{label} could not be normalized safely")
        result = [item / length for item in scaled]
    return tuple(result)


def _close_vector(actual, expected, label):
    if type(actual) not in (list, tuple) or len(actual) != 3:
        raise RuntimeError(f"{label} did not read back as a three-component vector")
    for index, (left, right) in enumerate(zip(actual, expected)):
        if type(left) not in (int, float) or not math.isfinite(left):
            raise RuntimeError(f"{label}[{index}] did not read back as a finite number")
        if not math.isclose(float(left), right, rel_tol=0.0, abs_tol=1.0e-12):
            raise RuntimeError(
                f"{label} readback differs at component {index}: "
                f"{float(left)!r} != {right!r}"
            )


def _deck(base, constants, value):
    if type(value) is not int:
        raise TypeError("deck must be an integer")
    current = base.CurrentDeck()
    if type(current) is not int:
        raise RuntimeError("CurrentDeck did not return an integer")
    if value != current:
        raise ValueError("requested deck is not the current ANSA deck")
    if value != constants.ABAQUS:
        raise ValueError("kinematics operations require the ABAQUS deck")
    return value


def _require_capabilities(owner, names, owner_name):
    for name in names:
        if not callable(getattr(owner, name, None)):
            raise NotImplementedError(f"ANSA {owner_name}.{name} is unavailable")


def _card_fields(entity, deck, label):
    getter = getattr(entity, "card_fields", None)
    if not callable(getter):
        raise NotImplementedError(f"{label} card_fields capability is unavailable")
    fields = getter(deck)
    if type(fields) not in (list, tuple) or not fields:
        raise RuntimeError(f"{label} card_fields did not return a non-empty field list")
    if any(type(name) is not str or not name for name in fields):
        raise RuntimeError(f"{label} card_fields returned an invalid field name")
    if len(fields) != len(set(fields)):
        raise RuntimeError(f"{label} card_fields returned duplicate field names")
    return tuple(fields)


def _read_full_card(base, deck, entity, label):
    fields = _card_fields(entity, deck, label)
    values = base.GetEntityCardValues(deck, entity, fields)
    if type(values) is not dict:
        raise RuntimeError(f"{label} GetEntityCardValues did not return an object")
    missing = [name for name in fields if name not in values]
    if missing:
        raise RuntimeError(
            f"{label} did not return card field(s): {', '.join(missing)}"
        )
    reference = catalog._entity_reference(base, deck, entity)
    serialized = {
        name: catalog._json_value(values[name], base, deck) for name in fields
    }
    nonempty = {
        name: value for name, value in serialized.items()
        if value not in (None, "", [], {})
    }
    return {
        "entity": reference,
        "card_fields": list(fields),
        "fields": serialized,
        "nonempty_fields": nonempty,
    }


def _require_fields(card, required, label):
    missing = sorted(set(required) - set(card["card_fields"]))
    if missing:
        raise NotImplementedError(
            f"{label} lacks required stable card field(s): {', '.join(missing)}"
        )


def _cog_marker_readback(card, label):
    """Validate COG_MARKER when the current ANSA card exposes it."""
    if "COG_MARKER" not in card["card_fields"]:
        return {
            "available": False,
            "field": "COG_MARKER",
            "reason": "current ANSA KIN_BODY card does not expose COG_MARKER",
        }
    value = card["fields"].get("COG_MARKER")
    if type(value) is not int or value <= 0:
        raise RuntimeError(f"{label} exposes an invalid automatic COG marker")
    return {
        "available": True,
        "field": "COG_MARKER",
        "value": value,
    }


def _preflight_keyword_fields(base, deck, create_type, required):
    query_type = _KEYWORD_TYPE_MAP.get(create_type)
    if query_type is None:
        raise ValueError(f"unsupported kinetics keyword type: {create_type!r}")
    labels = base.GetKeywordFieldLabels(deck, query_type)
    if type(labels) not in (list, tuple):
        raise RuntimeError(
            f"GetKeywordFieldLabels({query_type}) returned an invalid response"
        )
    if any(type(name) is not str for name in labels):
        raise RuntimeError(
            f"GetKeywordFieldLabels({query_type}) returned an invalid label"
        )
    # Warm 2026.1 keyword metadata includes '' and ' ' layout placeholders.
    # They are not card labels; preserve every nonblank label verbatim.
    ignored_blank_labels = sum(not name.strip() for name in labels)
    labels = [name for name in labels if name.strip()]
    if not labels:
        return {
            "available": False,
            "query_type": query_type,
            "labels": [],
            "required_fields": list(required),
            "source": (
                "verified 2026.1 kinematic creation schema; "
                "GetKeywordFieldLabels empty"
            ),
        }
    missing = sorted(set(required) - set(labels))
    if missing:
        raise ValueError(
            f"{query_type} does not define required field(s): {', '.join(missing)}"
        )
    return {
        "available": True,
        "query_type": query_type,
        "labels": list(labels),
        "required_fields": list(required),
        "source": f"GetKeywordFieldLabels({query_type})",
        "ignored_blank_labels": ignored_blank_labels,
    }


def _collect(base, deck, entity_type):
    entities = base.CollectEntities(
        deck, None, entity_type, False, filter_visible=False, hidden_entities=True
    )
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({entity_type}) did not return a list")
    return list(entities)


def _counts(base, deck):
    return {
        entity_type: len(_collect(base, deck, entity_type))
        for entity_type in ("KIN_BODY", "KIN_JOINT", "KIN_CONFIG", "KIN_MARKER")
    }


def _ensure_unique_name(base, deck, entity_type, wanted):
    for entity in _collect(base, deck, entity_type):
        card = _read_full_card(base, deck, entity, f"{entity_type} {entity._id}")
        if "Name" not in card["fields"]:
            raise NotImplementedError(f"{entity_type} has no Name card field")
        if card["fields"]["Name"] == wanted:
            raise ValueError(f"{entity_type} name already exists: {wanted}")


def _member_reference(value, deck, index):
    item = catalog._object(
        value, ("deck", "type", "id"), label=f"members[{index}]"
    )
    if type(item["deck"]) is not int or item["deck"] != deck:
        raise ValueError(f"members[{index}].deck must match the requested deck")
    member_type = item["type"]
    if type(member_type) is not str or member_type not in _MEMBER_TYPES:
        raise ValueError(
            f"members[{index}].type must be NODE, SHELL, SOLID, SECTION, "
            "SHELL_SECTION, SOLID_SECTION, or ANSAPART"
        )
    return {
        "deck": deck,
        "type": member_type,
        "id": _positive_id(item["id"], f"members[{index}].id"),
    }


def _resolve_part_contents(base, deck, part):
    result = []
    requests = (
        ("NODE", {}),
        ("SHELL", {}),
        ("SOLID", {}),
        ("__PROPERTIES__", {"prop_from_entities": True}),
    )
    for entity_type, extra in requests:
        entities = base.CollectEntities(
            deck,
            part,
            entity_type,
            True,
            filter_visible=False,
            hidden_entities=True,
            **extra,
        )
        if type(entities) not in (list, tuple):
            raise RuntimeError(
                f"CollectEntities(ANSAPART, {entity_type}) did not return a list"
            )
        result.extend(entities)
    return result


def _resolve_members(base, deck, references):
    properties = {
        entity._id: entity for entity in _collect(base, deck, "__PROPERTIES__")
    }
    resolved = []
    seen = set()
    for reference in references:
        member_type, member_id = reference["type"], reference["id"]
        if member_type in {"SECTION", "SHELL_SECTION", "SOLID_SECTION"}:
            entity = properties.get(member_id)
            if entity is None:
                raise LookupError(
                    f"{member_type}/property {member_id} does not exist"
                )
            if (
                member_type != "SECTION"
                and entity.ansa_type(deck) != member_type
            ):
                raise RuntimeError(
                    f"{member_type} {member_id} resolved as "
                    f"{entity.ansa_type(deck)}"
                )
            entities = [entity]
        elif member_type == "ANSAPART":
            entity, _reference = catalog._get_entity(
                base, deck, "ANSAPART", member_id
            )
            entities = _resolve_part_contents(base, deck, entity)
            if not entities:
                raise ValueError(f"ANSAPART {member_id} has no rigid-body member contents")
        else:
            entity, _reference = catalog._get_entity(
                base, deck, member_type, member_id
            )
            entities = [entity]
        for item in entities:
            item_reference = catalog._entity_reference(base, deck, item)
            key = (item_reference["type"], item_reference["id"])
            if key not in seen:
                seen.add(key)
                resolved.append((item, item_reference))
                if len(resolved) > _MAX_RESOLVED_MEMBERS:
                    raise ValueError(
                        f"resolved members exceed the {_MAX_RESOLVED_MEMBERS}-entity limit"
                    )
    return resolved


def _membership_entities(base, deck, body):
    entities = base.CollectEntities(
        deck,
        body,
        "__ALL_ENTITIES__",
        False,
        filter_visible=False,
        hidden_entities=True,
        no_expand_types=("SET",),
    )
    if type(entities) not in (list, tuple):
        raise RuntimeError("body membership did not return an entity list")
    members, markers = [], []
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity)
        (markers if reference["type"] == "KIN_MARKER" else members).append(entity)
    return members, markers


def _reference_list(entities, base, deck):
    references = [catalog._entity_reference(base, deck, entity) for entity in entities]
    references.sort(key=lambda item: (item["type"], item["id"]))
    return references


def _membership(base, deck, body):
    members, markers = _membership_entities(base, deck, body)
    member_references = _reference_list(members, base, deck)
    marker_references = _reference_list(markers, base, deck)
    return member_references, marker_references


def _reference_key(reference):
    return (reference["type"], reference["id"])


def _expand_body_members(base, deck, entities):
    expanded = []
    seen = set()
    expanded_parts = set()
    pending = list(entities)
    while pending:
        entity = pending.pop(0)
        reference = catalog._entity_reference(base, deck, entity)
        if reference["type"] == "ANSAPART":
            part_key = _reference_key(reference)
            if part_key in expanded_parts:
                raise RuntimeError(
                    f"cyclic ANSAPART membership at {part_key!r}"
                )
            expanded_parts.add(part_key)
            pending.extend(_resolve_part_contents(base, deck, entity))
            continue
        key = _reference_key(reference)
        if key not in seen:
            seen.add(key)
            expanded.append(entity)
            if len(expanded) > _MAX_EXISTING_MEMBERS:
                raise ValueError(
                    "existing rigid-body members exceed the "
                    f"{_MAX_EXISTING_MEMBERS}-entity scan limit"
                )
    return expanded


def _scan_existing_bodies(base, deck):
    bodies = _collect(base, deck, "KIN_BODY")
    if len(bodies) > _MAX_EXISTING_BODIES:
        raise ValueError(
            "existing KIN_BODY count exceeds the "
            f"{_MAX_EXISTING_BODIES}-body scan limit"
        )
    states = {}
    entities = {}
    total = 0
    for body in bodies:
        members, markers = _membership_entities(base, deck, body)
        expanded = _expand_body_members(base, deck, members)
        total += len(expanded)
        if total > _MAX_EXISTING_MEMBERS:
            raise ValueError(
                "existing rigid-body members exceed the "
                f"{_MAX_EXISTING_MEMBERS}-entity scan limit"
            )
        entities[body._id] = expanded
        states[str(body._id)] = {
            "members": _reference_list(members, base, deck),
            "markers": _reference_list(markers, base, deck),
        }
    return states, entities


def _relation_data(base, deck, entities, property_keys):
    exact, nodes, properties = set(), set(), set()
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity)
        exact.add(_reference_key(reference))
        entity_id = reference["id"]
        if reference["type"] == "NODE":
            nodes.add(entity_id)
            continue
        if _reference_key(reference) in property_keys:
            properties.add(entity_id)
            continue
        fields = _card_fields(entity, deck, f"{reference['type']} {entity_id}")
        requested = [
            field for field in fields
            if field == "PID" or re.fullmatch(r"G\d+", field)
        ]
        if not requested:
            continue
        values = base.GetEntityCardValues(deck, entity, requested)
        if type(values) is not dict:
            raise RuntimeError(
                f"{reference['type']} {entity_id} relation read returned no object"
            )
        property_id = values.get("PID")
        if type(property_id) is int and property_id > 0:
            properties.add(property_id)
        for field in requested:
            node_id = values.get(field)
            if field != "PID" and type(node_id) is int and node_id > 0:
                nodes.add(node_id)
    return exact, nodes, properties


def _member_conflicts(base, deck, requested_entities, existing_entities):
    property_keys = {
        _reference_key(catalog._entity_reference(base, deck, entity))
        for entity in _collect(base, deck, "__PROPERTIES__")
    }
    requested = _relation_data(base, deck, requested_entities, property_keys)
    conflicts = []
    for body_id, entities in existing_entities.items():
        existing = _relation_data(base, deck, entities, property_keys)
        overlap = {
            "exact": sorted(requested[0] & existing[0]),
            "nodes": sorted(requested[1] & existing[1]),
            "properties": sorted(requested[2] & existing[2]),
        }
        if any(overlap.values()):
            conflicts.append({"body_id": body_id, **overlap})
    return conflicts


def _read_old_body_states(base, deck, body_ids):
    states, errors = {}, {}
    for body_id in sorted(body_ids):
        try:
            body, _reference = catalog._get_entity(
                base, deck, "KIN_BODY", body_id
            )
            members, markers = _membership(base, deck, body)
            states[str(body_id)] = {
                "members": members,
                "markers": markers,
                "unknown": False,
            }
        except Exception as exc:
            errors[str(body_id)] = _error(exc)
            states[str(body_id)] = {
                "members": None,
                "markers": None,
                "unknown": True,
                "error": errors[str(body_id)],
            }
    return states, errors


def _old_body_changes(before, after):
    changed = []
    for body_id, expected in before.items():
        observed = after.get(body_id)
        if observed is None or observed.get("unknown") is True:
            changed.append({
                "body_id": int(body_id),
                "error": "old KIN_BODY state could not be read",
            })
            continue
        if (
            not catalog._strict_equal(expected["members"], observed["members"])
            or not catalog._strict_equal(expected["markers"], observed["markers"])
        ):
            changed.append({
                "body_id": int(body_id),
                "expected": expected,
                "observed": observed,
            })
    return changed


def _best_effort_after_state(base, deck, entity_type, entity_id, old_body_states):
    evidence = {
        "after_counts": None,
        "after_counts_unknown": True,
        "after_counts_error": None,
        "new_entity": None,
        "new_entity_unknown": True,
        "new_entity_error": None,
        "new_members": None,
        "new_members_unknown": True,
        "new_members_error": None,
        "new_markers": None,
        "new_markers_unknown": True,
        "new_markers_error": None,
        "old_bodies_after": None,
        "old_bodies_after_unknown": True,
    }
    try:
        evidence.update({
            "after_counts": _counts(base, deck),
            "after_counts_unknown": False,
        })
    except Exception as exc:
        evidence["after_counts_error"] = _error(exc)

    if entity_id is None:
        evidence["new_entity_error"] = "new entity ID is unknown"
    else:
        try:
            entity, _reference = catalog._get_entity(
                base, deck, entity_type, entity_id
            )
            evidence.update({
                "new_entity": _read_full_card(
                    base, deck, entity, f"{entity_type} {entity_id}"
                ),
                "new_entity_unknown": False,
            })
            if entity_type == "KIN_BODY":
                try:
                    members, markers = _membership(base, deck, entity)
                    evidence.update({
                        "new_members": members,
                        "new_members_unknown": False,
                        "new_markers": markers,
                        "new_markers_unknown": False,
                    })
                except Exception as exc:
                    evidence["new_members_error"] = _error(exc)
                    evidence["new_markers_error"] = _error(exc)
        except Exception as exc:
            evidence["new_entity_error"] = _error(exc)

    if old_body_states is not None:
        states, errors = _read_old_body_states(
            base, deck, [int(body_id) for body_id in old_body_states]
        )
        evidence["old_bodies_after"] = states
        evidence["old_bodies_after_unknown"] = bool(errors)
        if errors:
            evidence["old_bodies_after_error"] = errors
    return evidence


def _create_entity(base, constants, deck, create_type, fields):
    report_mode = getattr(constants, "REPORT_ALL", None)
    if type(report_mode) is not int:
        raise NotImplementedError("ANSA constants.REPORT_ALL is unavailable")
    result = base.CreateEntity(
        deck, create_type, fields, mbcontainer=None, debug=report_mode
    )
    report = {}
    if type(result) is tuple and len(result) == 2:
        entity, report = result
    else:
        entity = result
    if type(report) is not dict:
        raise RuntimeError(f"{create_type} CreateEntity returned an invalid report")
    errors, warnings = [], []
    for field, item in report.items():
        if type(item) is not dict:
            continue
        kind, message = item.get("type"), item.get("message", "")
        text = f"{field}: {message}"
        if kind == "error":
            errors.append(text)
        elif kind == "warning":
            warnings.append(text)
    if errors:
        raise RuntimeError(f"{create_type} creation reported errors: {'; '.join(errors)}")
    if entity is None:
        raise RuntimeError(f"{create_type} creation returned no entity")
    entity_id = getattr(entity, "_id", None)
    if type(entity_id) is not int or entity_id <= 0:
        raise RuntimeError(f"{create_type} creation returned an invalid entity ID")
    return entity, warnings


def _backup_target(base, backup_path):
    path, _prior = _save_path(backup_path, False)
    current_path = guards._database_name(base)
    if current_path and model_files._same_path(path, Path(current_path)):
        raise ValueError("backup_path must differ from the current database path")
    fingerprint = (
        model_files._fingerprint(Path(current_path)) if current_path else None
    )
    return path, current_path, fingerprint


def _failure(tool, deck, backup, exc, *, details=None, possible_effects=True):
    message = _error(exc)
    result = {
        "ok": False,
        "tool": tool,
        "deck": deck,
        "backup": backup,
        "possible_effects": possible_effects,
        "error": message,
        "errors": [message],
    }
    if details:
        result.update(details)
    return _bounded_result(result)


def _backup_or_failure(base, constants, tool, deck, path, current_path, fingerprint,
                       before_counts):
    try:
        return guards._verify_backup(
            base, constants, path, current_path, fingerprint
        ), None
    except Exception as exc:
        return None, _failure(
            tool,
            deck,
            guards._unconfirmed_backup(path),
            exc,
            details={"before_counts": before_counts},
        )


def _body_create(state, params):
    params = catalog._object(
        params,
        ("deck", "name", "ground", "members", "backup_path"),
        label="params",
    )
    if type(params["ground"]) is not bool:
        raise TypeError("ground must be a boolean")
    if type(params["members"]) is not list or len(params["members"]) > _MAX_MEMBERS:
        raise ValueError(f"members must contain at most {_MAX_MEMBERS} references")
    if params["ground"] and params["members"]:
        raise ValueError("ground bodies must have an empty members list")
    if not params["ground"] and not params["members"]:
        raise ValueError("non-ground bodies require at least one member")
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    name = _name(params["name"])
    from ansa import base, constants, kinetics

    deck = _deck(base, constants, params["deck"])
    _require_capabilities(
        base,
        ("CollectEntities", "CreateEntity", "GetEntity", "GetEntityCardValues",
         "GetKeywordFieldLabels", "SaveAs"),
        "base",
    )
    _require_capabilities(kinetics, ("AddToRigidBody",), "kinetics")
    members = [
        _member_reference(value, deck, index)
        for index, value in enumerate(params["members"])
    ]
    if len({(item["type"], item["id"]) for item in members}) != len(members):
        raise ValueError("members must not contain duplicate references")

    context = guards._execution_context(state, deck)
    before_counts = _counts(base, deck)
    _ensure_unique_name(base, deck, "KIN_BODY", name)
    preflight_metadata = _preflight_keyword_fields(
        base, deck, "KIN_RBODY", ("Name", "GEN_TYPE")
    )
    resolved = _resolve_members(base, deck, members)
    existing_states, existing_entities = _scan_existing_bodies(base, deck)
    conflicts = _member_conflicts(
        base, deck, [item for item, _reference in resolved], existing_entities
    )
    if conflicts:
        raise ValueError(
            "requested members overlap existing KIN_BODY membership; "
            f"conflicts={conflicts!r}"
        )
    if len(_encoded({"existing": existing_states, "requested": [ref for _, ref in resolved]})) > _RESULT_BUDGET:
        raise ValueError("rigid-body preflight evidence exceeds the budget; narrow the member scope before retrying")
    path, current_path, fingerprint = _backup_target(base, params["backup_path"])

    backup, result = _backup_or_failure(
        base, constants, "kinematics_body_create", deck, path, current_path,
        fingerprint, before_counts,
    )
    if result is not None:
        return result

    body_id = None
    try:
        guards._same_context(state, deck, context)
        _ensure_unique_name(base, deck, "KIN_BODY", name)
        resolved = _resolve_members(base, deck, members)
        existing_states, existing_entities = _scan_existing_bodies(base, deck)
        conflicts = _member_conflicts(
            base, deck, [item for item, _reference in resolved], existing_entities
        )
        if conflicts:
            raise ValueError(
                "requested members overlap existing KIN_BODY membership after "
                f"backup; conflicts={conflicts!r}"
            )
        entity, warnings = _create_entity(
            base,
            constants,
            deck,
            "KIN_RBODY",
            {
                "Name": name,
                "GEN_TYPE": "GROUND" if params["ground"] else "RIG_BODY",
            },
        )
        body_id = entity._id
        body, _reference = catalog._get_entity(
            base, deck, "KIN_BODY", body_id
        )
        if resolved:
            status = kinetics.AddToRigidBody(
                body, [item for item, _item_reference in resolved]
            )
            if type(status) is not int or status != 0:
                raise RuntimeError(
                    "AddToRigidBody did not return the exact success value int 0"
                )
        else:
            status = None

        card = _read_full_card(base, deck, body, f"KIN_BODY {body_id}")
        _require_fields(card, _BODY_FIELDS, f"KIN_BODY {body_id}")
        fields = card["fields"]
        cog_marker_readback = _cog_marker_readback(
            card, f"KIN_BODY {body_id}"
        )
        expected_gen_type = "GROUND" if params["ground"] else "RIG_BODY"
        if fields["Name"] != name:
            raise RuntimeError("created KIN_BODY name does not match the request")
        if fields["GEN_TYPE"] != expected_gen_type:
            raise RuntimeError("created KIN_BODY GEN_TYPE does not match the request")

        actual_members, markers = _membership(base, deck, body)
        expected_members = {
            _reference_key(item_reference)
            for _item, item_reference in resolved
        }
        actual_keys = {_reference_key(item) for item in actual_members}
        if actual_keys != expected_members:
            missing = sorted(expected_members - actual_keys)
            extra = sorted(actual_keys - expected_members)
            raise RuntimeError(
                "KIN_BODY membership does not match the requested members; "
                f"missing={missing!r}, extra={extra!r}"
            )
        old_bodies_after, old_body_errors = _read_old_body_states(
            base, deck, [int(body_id) for body_id in existing_states]
        )
        if old_body_errors:
            raise RuntimeError(
                "old KIN_BODY membership readback is unknown: "
                f"{old_body_errors!r}"
            )
        old_changes = _old_body_changes(existing_states, old_bodies_after)
        if old_changes:
            raise RuntimeError(
                "old KIN_BODY membership changed outside the new body: "
                f"{old_changes!r}"
            )
        guards._same_context(state, deck, context)
        after_counts = _counts(base, deck)
        return _bounded_result({
            "ok": True,
            "tool": "kinematics_body_create",
            "deck": deck,
            "body": card,
            "automatic_cog_marker": cog_marker_readback,
            "requested_members": [item for item in members],
            "members": actual_members,
            "markers": markers,
            "preflight_metadata": preflight_metadata,
            "existing_bodies_before": existing_states,
            "existing_bodies_after": old_bodies_after,
            "existing_bodies_unchanged": True,
            "add_to_rigid_body_return_code": status,
            "backup": backup,
            "before_counts": before_counts,
            "after_counts": after_counts,
            "possible_effects": False,
            "warnings": warnings,
        })
    except Exception as exc:
        evidence = _best_effort_after_state(
            base, deck, "KIN_BODY", body_id, existing_states
        )
        return _failure(
            "kinematics_body_create",
            deck,
            backup,
            exc,
            details={
                "body_id": body_id,
                "before_counts": before_counts,
                "possible_effects": True,
                "preflight_metadata": preflight_metadata,
                "existing_bodies_before": existing_states,
                **evidence,
            },
        )


def _joint_bodies(base, deck, body1_id, body2_id):
    body1, _reference1 = catalog._get_entity(base, deck, "KIN_BODY", body1_id)
    body2, _reference2 = catalog._get_entity(base, deck, "KIN_BODY", body2_id)
    for body_id, body in ((body1_id, body1), (body2_id, body2)):
        card = _read_full_card(base, deck, body, f"KIN_BODY {body_id}")
        _require_fields(card, ("EID", "GEN_TYPE", "Name"), f"KIN_BODY {body_id}")
    return body1, body2


def _joint_create(state, params):
    params = catalog._object(
        params,
        (
            "deck", "name", "joint_type", "body1_id", "body2_id",
            "anchor", "axis", "backup_path",
        ),
        label="params",
    )
    name = _name(params["name"])
    joint_type = params["joint_type"]
    if type(joint_type) is not str or joint_type not in {"REVOLUTE", "SLIDER"}:
        raise ValueError("joint_type must be REVOLUTE or SLIDER")
    body1_id = _positive_id(params["body1_id"], "body1_id")
    body2_id = _positive_id(params["body2_id"], "body2_id")
    if body1_id == body2_id:
        raise ValueError("body1_id and body2_id must be different")
    anchor = _vector(params["anchor"], "anchor")
    axis = _vector(params["axis"], "axis", nonzero=True)
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    from ansa import base, constants

    deck = _deck(base, constants, params["deck"])
    _require_capabilities(
        base,
        ("CollectEntities", "CreateEntity", "GetEntity", "GetEntityCardValues",
         "GetKeywordFieldLabels", "SaveAs"),
        "base",
    )
    context = guards._execution_context(state, deck)
    before_counts = _counts(base, deck)
    _ensure_unique_name(base, deck, "KIN_JOINT", name)
    preflight_metadata = _preflight_keyword_fields(
        base,
        deck,
        "KIN_JOINT",
        (
            "Name", "TYPE", "ACTIVE", "KIN_BODY_1", "KIN_BODY_2",
            "DIRECTION_TYPE", "ANCHOR_x", "ANCHOR_y", "ANCHOR_z",
            "Z_x", "Z_y", "Z_z",
        ),
    )
    _joint_bodies(base, deck, body1_id, body2_id)
    path, current_path, fingerprint = _backup_target(base, params["backup_path"])
    backup, result = _backup_or_failure(
        base, constants, "kinematics_joint_create", deck, path, current_path,
        fingerprint, before_counts,
    )
    if result is not None:
        return result

    joint_id = None
    try:
        guards._same_context(state, deck, context)
        _ensure_unique_name(base, deck, "KIN_JOINT", name)
        _joint_bodies(base, deck, body1_id, body2_id)
        entity, warnings = _create_entity(
            base,
            constants,
            deck,
            "KIN_JOINT",
            {
                "Name": name,
                "TYPE": joint_type,
                "ACTIVE": "YES",
                "KIN_BODY_1": body1_id,
                "KIN_BODY_2": body2_id,
                "DIRECTION_TYPE": "1Point_Axis",
                "ANCHOR_x": anchor[0],
                "ANCHOR_y": anchor[1],
                "ANCHOR_z": anchor[2],
                "Z_x": axis[0],
                "Z_y": axis[1],
                "Z_z": axis[2],
            },
        )
        joint_id = entity._id
        joint, _reference = catalog._get_entity(
            base, deck, "KIN_JOINT", joint_id
        )
        card = _read_full_card(base, deck, joint, f"KIN_JOINT {joint_id}")
        _require_fields(card, _JOINT_FIELDS, f"KIN_JOINT {joint_id}")
        fields = card["fields"]
        expected = {
            "Name": name,
            "TYPE": joint_type,
            "ACTIVE": "YES",
            "DEFINITION": "BY_BODIES",
            "KIN_BODY_1": body1_id,
            "KIN_BODY_2": body2_id,
            "DIRECTION_TYPE": "1Point_Axis",
        }
        changed = [
            field for field, value in expected.items()
            if not catalog._strict_equal(fields[field], value)
        ]
        if changed:
            raise RuntimeError(
                "created KIN_JOINT fields do not match the request: "
                + ", ".join(changed)
            )
        _close_vector(
            [fields["ANCHOR_x"], fields["ANCHOR_y"], fields["ANCHOR_z"]],
            anchor,
            "KIN_JOINT anchor",
        )
        _close_vector(
            [fields["Z_x"], fields["Z_y"], fields["Z_z"]],
            axis,
            "KIN_JOINT axis",
        )
        guards._same_context(state, deck, context)
        after_counts = _counts(base, deck)
        return _bounded_result({
            "ok": True,
            "tool": "kinematics_joint_create",
            "deck": deck,
            "joint": card,
            "body1_id": body1_id,
            "body2_id": body2_id,
            "anchor": list(anchor),
            "axis": list(axis),
            "normalized_axis": list(axis),
            "preflight_metadata": preflight_metadata,
            "backup": backup,
            "before_counts": before_counts,
            "after_counts": after_counts,
            "possible_effects": False,
            "warnings": warnings,
        })
    except Exception as exc:
        evidence = _best_effort_after_state(
            base, deck, "KIN_JOINT", joint_id, None
        )
        return _failure(
            "kinematics_joint_create",
            deck,
            backup,
            exc,
            details={
                "joint_id": joint_id,
                "before_counts": before_counts,
                "possible_effects": True,
                "preflight_metadata": preflight_metadata,
                **evidence,
            },
        )


def _config_joints(base, deck, joint_ids):
    joints = []
    for joint_id in joint_ids:
        joint, _reference = catalog._get_entity(
            base, deck, "KIN_JOINT", joint_id
        )
        joints.append(joint)
    return joints


def _config_body_closure(base, deck, native_bodies, joint_cards):
    """Include ground bodies referenced by joints, which the native list omits."""
    referenced = set()
    for card in joint_cards:
        _require_fields(card, ("KIN_BODY_1", "KIN_BODY_2"), "configuration joint")
        for field in ("KIN_BODY_1", "KIN_BODY_2"):
            referenced.add(_positive_id(card["fields"][field], field))
    native_ids = sorted(catalog._entity_reference(base, deck, body, "KIN_BODY")["id"] for body in native_bodies)
    if set(native_ids) - referenced:
        raise RuntimeError("native configuration body list contains an unreferenced body")
    bodies = [catalog._get_entity(base, deck, "KIN_BODY", body_id)[0] for body_id in sorted(referenced)]
    return bodies, native_ids


def _config_create(state, params):
    params = catalog._object(
        params,
        ("deck", "name", "joint_ids", "backup_path"),
        ("actuator_joint_id",),
        label="params",
    )
    name = _name(params["name"])
    joint_ids = params["joint_ids"]
    if type(joint_ids) is not list or not 1 <= len(joint_ids) <= _MAX_JOINTS:
        raise ValueError(f"joint_ids must contain 1 to {_MAX_JOINTS} IDs")
    joint_ids = [_positive_id(value, "joint_ids entry") for value in joint_ids]
    if len(joint_ids) != len(set(joint_ids)):
        raise ValueError("joint_ids must not contain duplicates")
    actuator_id = params.get("actuator_joint_id")
    if actuator_id is not None:
        actuator_id = _positive_id(actuator_id, "actuator_joint_id")
        if actuator_id not in joint_ids:
            raise ValueError("actuator_joint_id must belong to joint_ids")
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    from ansa import base, constants, kinetics

    deck = _deck(base, constants, params["deck"])
    _require_capabilities(
        base,
        ("CollectEntities", "CreateEntity", "GetEntity", "GetEntityCardValues",
         "GetKeywordFieldLabels", "SaveAs"),
        "base",
    )
    _require_capabilities(
        kinetics,
        ("AddJointToConfig", "SetUnits", "GetJointsFromConfig",
         "GetBodiesFromConfig", "GetModelInfo"),
        "kinetics",
    )
    if actuator_id is not None:
        _require_capabilities(base, ("SetEntityCardValues",), "base")

    context = guards._execution_context(state, deck)
    before_counts = _counts(base, deck)
    _ensure_unique_name(base, deck, "KIN_CONFIG", name)
    preflight_metadata = _preflight_keyword_fields(
        base, deck, "KIN_CONFIG", ("Name",)
    )
    selected_joints = _config_joints(base, deck, joint_ids)
    actuator_type = None
    if actuator_id is not None:
        selected = next(joint for joint in selected_joints if joint._id == actuator_id)
        selected_card = _read_full_card(base, deck, selected, f"KIN_JOINT {actuator_id}")
        _require_fields(selected_card, ("TYPE",), f"KIN_JOINT {actuator_id}")
        actuator_type = selected_card["fields"]["TYPE"]
        if actuator_type not in {"REVOLUTE", "SLIDER"}:
            raise ValueError("actuator joint must be REVOLUTE or SLIDER")
    path, current_path, fingerprint = _backup_target(base, params["backup_path"])
    backup, result = _backup_or_failure(
        base, constants, "kinematics_config_create", deck, path, current_path,
        fingerprint, before_counts,
    )
    if result is not None:
        return result

    config_id = None
    try:
        guards._same_context(state, deck, context)
        _ensure_unique_name(base, deck, "KIN_CONFIG", name)
        joints = _config_joints(base, deck, joint_ids)
        entity, warnings = _create_entity(
            base, constants, deck, "KIN_CONFIG", {"Name": name}
        )
        config_id = entity._id
        for joint in joints:
            status = kinetics.AddJointToConfig(entity, joint)
            if type(status) is not int or status != 0:
                raise RuntimeError(
                    f"AddJointToConfig({joint._id}) did not return int 0"
                )
        unit_status = kinetics.SetUnits(
            length="mm",
            mass="tonne",
            force="newton",
            time="sec",
            angle="deg",
            kin_config=entity,
        )
        if type(unit_status) is not int or unit_status != 0:
            raise RuntimeError("SetUnits did not return int 0")
        actuator_status = None
        if actuator_id is not None:
            # ConfigSetParameters controls the motion session; on 2026.1 a
            # zero displacement returns 0 without saving ACTUATOR_JOINT.
            # Define the persisted card explicitly without executing motion.
            actuator_status = base.SetEntityCardValues(
                deck, entity, {"ACTUATOR_JOINT": actuator_id}
            )
            if type(actuator_status) is not int or actuator_status != 0:
                raise RuntimeError("setting KIN_CONFIG ACTUATOR_JOINT did not return int 0")

        config, _reference = catalog._get_entity(
            base, deck, "KIN_CONFIG", config_id
        )
        config_card = _read_full_card(
            base, deck, config, f"KIN_CONFIG {config_id}"
        )
        _require_fields(config_card, _CONFIG_FIELDS, f"KIN_CONFIG {config_id}")
        fields = config_card["fields"]
        if fields["Name"] != name:
            raise RuntimeError("created KIN_CONFIG name does not match the request")
        expected_actuator = 0 if actuator_id is None else actuator_id
        if not catalog._strict_equal(fields["ACTUATOR_JOINT"], expected_actuator):
            raise RuntimeError("created KIN_CONFIG actuator joint does not match")
        expected_actuator_type = "NONE" if actuator_id is None else actuator_type
        if fields["ACTUATOR_JOINT_TYPE"] != expected_actuator_type:
            raise RuntimeError("created KIN_CONFIG actuator joint type does not match")

        config_joints = kinetics.GetJointsFromConfig(config, recursive=False)
        if type(config_joints) not in (list, tuple):
            raise RuntimeError("GetJointsFromConfig did not return a list")
        actual_joint_ids = sorted(
            catalog._entity_reference(base, deck, joint)["id"]
            for joint in config_joints
        )
        if actual_joint_ids != sorted(joint_ids):
            raise RuntimeError(
                "KIN_CONFIG joint membership does not match the requested joint_ids"
            )
        config_bodies = kinetics.GetBodiesFromConfig(config, False)
        if type(config_bodies) not in (list, tuple):
            raise RuntimeError("GetBodiesFromConfig did not return a list")
        joint_cards = [
            _read_full_card(base, deck, joint, f"KIN_JOINT {joint._id}")
            for joint in config_joints
        ]
        config_bodies, native_body_ids = _config_body_closure(base, deck, config_bodies, joint_cards)
        body_cards = []
        for body in config_bodies:
            body_card = _read_full_card(
                base, deck, body, f"KIN_BODY {body._id}"
            )
            members, markers = _membership(base, deck, body)
            body_card.update(members=members, markers=markers)
            body_cards.append(body_card)
        model_info = kinetics.GetModelInfo(config)
        model_info = catalog._json_value(model_info, base, deck)
        guards._same_context(state, deck, context)
        after_counts = _counts(base, deck)
        return _bounded_result({
            "ok": True,
            "tool": "kinematics_config_create",
            "deck": deck,
            "config": config_card,
            "joints": joint_cards,
            "bodies": body_cards,
            "native_body_ids": native_body_ids,
            "body_membership_scope": "joint-referenced bodies including ground; native_body_ids retains GetBodiesFromConfig scope",
            "joint_ids": joint_ids,
            "actuator_joint_id": actuator_id,
            "units": dict(_FIXED_UNITS),
            "preflight_metadata": preflight_metadata,
            "add_joint_return_codes": [0 for _joint in joints],
            "set_units_return_code": unit_status,
            "actuator_definition_api": "ansa.base.SetEntityCardValues" if actuator_id is not None else None,
            "set_actuator_return_code": actuator_status,
            "config_set_parameters_return_code": None,
            "model_info": model_info,
            "backup": backup,
            "before_counts": before_counts,
            "after_counts": after_counts,
            "possible_effects": False,
            "warnings": warnings,
        })
    except Exception as exc:
        evidence = _best_effort_after_state(
            base, deck, "KIN_CONFIG", config_id, None
        )
        if config_id is not None:
            try:
                config, _reference = catalog._get_entity(
                    base, deck, "KIN_CONFIG", config_id
                )
                joints = kinetics.GetJointsFromConfig(config, recursive=False)
                bodies = kinetics.GetBodiesFromConfig(config, False)
                evidence["config_joints_after"] = [
                    _read_full_card(base, deck, joint, f"KIN_JOINT {joint._id}")
                    for joint in joints
                ]
                evidence["config_bodies_after"] = []
                for body in bodies:
                    card = _read_full_card(
                        base, deck, body, f"KIN_BODY {body._id}"
                    )
                    members, markers = _membership(base, deck, body)
                    card.update(members=members, markers=markers)
                    evidence["config_bodies_after"].append(card)
            except Exception as read_exc:
                evidence["config_state_error"] = _error(read_exc)
        return _failure(
            "kinematics_config_create",
            deck,
            backup,
            exc,
            details={
                "config_id": config_id,
                "before_counts": before_counts,
                "possible_effects": True,
                "preflight_metadata": preflight_metadata,
                **evidence,
            },
        )


def _inspect_ids(base, deck, entity_type, entity_ids):
    rows = []
    for entity_id in entity_ids:
        entity, _reference = catalog._get_entity(
            base, deck, entity_type, entity_id
        )
        card = _read_full_card(
            base, deck, entity, f"{entity_type} {entity_id}"
        )
        if entity_type == "KIN_BODY":
            members, markers = _membership(base, deck, entity)
            card.update(members=members, markers=markers)
        rows.append(card)
    return rows


def _inspect(base, constants, params):
    from ansa import kinetics

    params = catalog._object(
        params,
        ("deck",),
        ("config_id", "body_ids", "joint_ids", "config_ids"),
        label="params",
    )
    deck = _deck(base, constants, params["deck"])
    config_id = params.get("config_id")
    body_ids = params.get("body_ids", [])
    joint_ids = params.get("joint_ids", [])
    config_ids = params.get("config_ids", [])
    for label, values, maximum in (
        ("body_ids", body_ids, _MAX_INSPECT_IDS),
        ("joint_ids", joint_ids, _MAX_INSPECT_IDS),
        ("config_ids", config_ids, 100),
    ):
        if type(values) is not list or len(values) > maximum:
            raise ValueError(f"{label} must be an array with at most {maximum} IDs")
        values = [_positive_id(value, f"{label} entry") for value in values]
        if len(values) != len(set(values)):
            raise ValueError(f"{label} must not contain duplicates")
    if config_id is not None:
        config_id = _positive_id(config_id, "config_id")
        if body_ids or joint_ids or config_ids:
            raise ValueError(
                "config_id cannot be combined with body_ids, joint_ids, or config_ids"
            )
    elif not (body_ids or joint_ids or config_ids):
        raise ValueError("provide config_id or at least one bounded ID list")

    _require_capabilities(
        base,
        ("CollectEntities", "GetEntity", "GetEntityCardValues"),
        "base",
    )
    before_counts = _counts(base, deck)
    result = {
        "ok": True,
        "tool": "kinematics_inspect",
        "deck": deck,
        "selection": "config" if config_id is not None else "ids",
        "configs": [],
        "joints": [],
        "bodies": [],
        "counts": before_counts,
        "possible_effects": False,
    }
    if config_id is not None:
        config, _reference = catalog._get_entity(
            base, deck, "KIN_CONFIG", config_id
        )
        config_card = _read_full_card(
            base, deck, config, f"KIN_CONFIG {config_id}"
        )
        joints = kinetics.GetJointsFromConfig(config, recursive=False)
        bodies = kinetics.GetBodiesFromConfig(config, False)
        if type(joints) not in (list, tuple):
            raise RuntimeError("GetJointsFromConfig did not return a list")
        if type(bodies) not in (list, tuple):
            raise RuntimeError("GetBodiesFromConfig did not return a list")
        result["configs"] = [config_card]
        result["joints"] = [
            _read_full_card(base, deck, joint, f"KIN_JOINT {joint._id}")
            for joint in joints
        ]
        bodies, native_body_ids = _config_body_closure(base, deck, bodies, result["joints"])
        result["native_body_ids"] = native_body_ids
        result["body_membership_scope"] = "joint-referenced bodies including ground; native_body_ids retains GetBodiesFromConfig scope"
        result["bodies"] = _inspect_ids(
            base, deck, "KIN_BODY", sorted(body._id for body in bodies)
        )
        result["model_info"] = catalog._json_value(
            kinetics.GetModelInfo(config), base, deck
        )
    else:
        result["bodies"] = _inspect_ids(base, deck, "KIN_BODY", body_ids)
        result["joints"] = _inspect_ids(base, deck, "KIN_JOINT", joint_ids)
        result["configs"] = _inspect_ids(base, deck, "KIN_CONFIG", config_ids)
    return result


def execute(state, tool, params):
    """Execute one kinematics operation on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if len(_encoded(params)) > _REQUEST_BUDGET:
        raise ValueError("kinematics request exceeds the reply budget")
    if tool == "kinematics_body_create":
        return _body_create(state, params)
    if tool == "kinematics_joint_create":
        return _joint_create(state, params)
    if tool == "kinematics_config_create":
        return _config_create(state, params)
    if tool == "kinematics_inspect":
        from ansa import base, constants, kinetics

        _require_capabilities(
            kinetics,
            ("GetJointsFromConfig", "GetBodiesFromConfig", "GetModelInfo"),
            "kinetics",
        )
        try:
            result = _inspect(base, constants, params)
        except Exception as exc:
            requested_deck = params.get("deck") if type(params) is dict else None
            return _failure(
                "kinematics_inspect",
                requested_deck if type(requested_deck) is int else None,
                None,
                exc,
                possible_effects=False,
            )
        if len(_encoded(result)) > _RESULT_BUDGET:
            return _failure(
                "kinematics_inspect",
                result.get("deck"),
                None,
                ValueError(
                    "kinematics inspection exceeds the reply budget; "
                    "narrow the requested ID lists"
                ),
                possible_effects=False,
            )
        return result
    raise ValueError(f"unsupported kinematics operation: {tool!r}")


__all__ = ["execute"]
