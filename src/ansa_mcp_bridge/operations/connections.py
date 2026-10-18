"""Bounded NASTRAN connector and connection-point operations."""

import json
import math

from . import entity_catalog


_MAX_CONNECTIONS = 10000
_MAX_PAGE_SIZE = 200
_MAX_ENTITY_ID = 2147483647
_POINT_TYPES = (
    "SpotweldPoint_Type",
    "Bolt_Type",
    "GumDrop_Type",
    "Rivet_Type",
    "Screw_Type",
    "Clip_Type",
)
_POINT_TYPE_SET = frozenset(_POINT_TYPES)
_CONNECTOR_TYPE = "CONNECTOR_ENTITY"
_CONNECTIVITY_FIELDS = tuple(f"P{index}" for index in range(1, 11))
_POSITION_FIELDS = ("X", "Y", "Z")
_MISSING = object()


def _error_text(error):
    try:
        value = f"{type(error).__name__}: {error}"
    except Exception:
        value = repr(error)
    return value[:240]


def _api(owner, name, label):
    value = getattr(owner, name, None)
    if not callable(value):
        raise NotImplementedError(f"ansa.{label}.{name} is unavailable")
    return value


def _require_base_apis(base):
    for name in ("TypesInCategory", "CollectEntities", "GetEntity", "GetEntityCardValues"):
        _api(base, name, "base")


def _check_deck(deck, constants):
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or deck != nastran:
        raise ValueError("connection operations require the current NASTRAN deck")


def _finite_number(value, label, *, positive=False, nonnegative=False):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    if positive and number <= 0:
        raise ValueError(f"{label} must be positive")
    if nonnegative and number < 0:
        raise ValueError(f"{label} must be nonnegative")
    return number


def _position(value, label="position"):
    if type(value) is not list or len(value) != 3:
        raise ValueError(f"{label} must be a list of exactly three finite numbers")
    return [_finite_number(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _available_types(base, deck):
    value = base.TypesInCategory(deck, "__CONNECTIONS__")
    if type(value) not in (list, tuple):
        raise RuntimeError("ANSA TypesInCategory returned an invalid connection-type list")
    if any(type(item) is not str or not item or item != item.strip() for item in value):
        raise RuntimeError("ANSA TypesInCategory returned invalid connection-type metadata")
    return frozenset(value)


def _collection_types(kind, available, *, include_unavailable=False):
    result = []
    if kind in ("all", "connector"):
        # Native 2026.1 evidence confirms this query type even when the category
        # list omits it; it is queried but never created by this operation.
        result.append(_CONNECTOR_TYPE)
    if kind in ("all", "point"):
        missing = [entity_type for entity_type in _POINT_TYPES if entity_type not in available]
        if missing and not include_unavailable:
            raise RuntimeError(
                "TypesInCategory(__CONNECTIONS__) omitted supported connection type(s): "
                + ", ".join(missing)
            )
        result.extend(_POINT_TYPES if include_unavailable else
                      (entity_type for entity_type in _POINT_TYPES if entity_type in available))
    return tuple(dict.fromkeys(result))


def _snapshot(base, deck, kind="all", *, available=None, tolerate_errors=False):
    errors = {}
    category_ok = True
    if available is None:
        try:
            available = _available_types(base, deck)
        except Exception as exc:
            if not tolerate_errors:
                raise
            errors["TypesInCategory"] = _error_text(exc)
            category_ok = False
            available = frozenset()

    if not category_ok and kind in ("all", "point"):
        # After a write, query all known point types for positive evidence even
        # when category discovery failed. Such a scan remains incomplete.
        types = _collection_types(kind, available, include_unavailable=True)
    else:
        try:
            types = _collection_types(kind, available)
        except RuntimeError as exc:
            if not tolerate_errors:
                raise
            errors["TypesInCategory"] = _error_text(exc)
            category_ok = False
            types = _collection_types(kind, available, include_unavailable=True)

    references = {}
    observed_types = set()
    for entity_type in types:
        try:
            entities = base.CollectEntities(deck, None, entity_type, recursive=False)
        except Exception as exc:
            errors[entity_type] = _error_text(exc)
            continue
        if type(entities) not in (list, tuple):
            errors[entity_type] = (
                "ANSA CollectEntities returned "
                f"{type(entities).__name__}, expected a list or tuple"
            )
            continue

        type_ok = True
        exceeded_limit = False
        for entity in entities:
            try:
                reference = entity_catalog._entity_reference(base, deck, entity)
                if reference["type"] != entity_type:
                    raise RuntimeError("collected entity type does not match its query")
            except Exception as exc:
                errors[entity_type] = _error_text(exc)
                type_ok = False
                continue
            key = (reference["type"], reference["id"])
            if key not in references and len(references) >= _MAX_CONNECTIONS:
                errors["range"] = f"connection range exceeds {_MAX_CONNECTIONS} entities"
                type_ok = False
                exceeded_limit = True
                break
            references[key] = reference
        if type_ok and entity_type not in errors:
            observed_types.add(entity_type)
        if exceeded_limit:
            break

    if errors and not tolerate_errors:
        details = "; ".join(f"{key}: {value}" for key, value in sorted(errors.items()))
        raise RuntimeError("connection range could not be completely observed: " + details[:500])
    return {
        "references": [references[key] for key in sorted(references)],
        "by_key": references,
        "observed_types": observed_types,
        "available_types": available,
        "errors": errors,
    }


def _number_vector(values):
    if type(values) not in (list, tuple) or len(values) != 3:
        raise ValueError("position must contain exactly three coordinates")
    return [_finite_number(item, "coordinate") for item in values]


def _same_position(left, right):
    return (
        type(left) is list and type(right) is list and len(left) == len(right) == 3
        and all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
                for a, b in zip(left, right))
    )


def _resolve_connection(base, deck, reference):
    """Reacquire a connection handle from a fresh bounded native type collection."""
    candidates = base.CollectEntities(deck, None, reference["type"], recursive=False)
    if type(candidates) not in (list, tuple) or len(candidates) > _MAX_CONNECTIONS:
        raise RuntimeError("fresh connection collection is unavailable or exceeds the scope limit")
    matches = [item for item in candidates if getattr(item, "_id", None) == reference["id"]]
    if len(matches) != 1:
        raise RuntimeError("connection identity is absent or ambiguous in the fresh collection")
    entity = matches[0]
    actual = entity_catalog._entity_reference(base, deck, entity, reference["type"], reference["id"])
    return entity, actual


def _connection_row(base, connections, deck, reference):
    is_point = reference["type"] in _POINT_TYPE_SET
    row = {
        "entity": reference,
        "kind": "point" if is_point else "connector",
        "name": None,
        "position": None,
        "diameter": None,
        "connectivity": [],
        "complete": False,
        "errors": {},
    }
    try:
        # In mixed native connection families a GetEntity handle followed by
        # card_fields exited ANSA 26.10.000. Recollect each requested type.
        entity, actual_reference = _resolve_connection(base, deck, reference)
        if actual_reference != reference:
            raise RuntimeError("connection identity changed while re-resolving the entity")
    except Exception as exc:
        row["errors"]["entity"] = _error_text(exc)
        return row

    try:
        labels = entity_catalog._entity_card_labels(entity, deck)
        if len(labels) > 512:
            raise RuntimeError("card_fields returned an unreasonable number of fields")
    except Exception as exc:
        labels = frozenset()
        row["errors"]["card_fields"] = _error_text(exc)

    required = {"Name", "D", *_POSITION_FIELDS, "P1", "P2"} if is_point else set()
    missing = sorted(required - labels)
    if missing:
        row["errors"]["card_fields_missing"] = ", ".join(missing)

    requested = [name for name in ("Name", "D", *_POSITION_FIELDS, *_CONNECTIVITY_FIELDS)
                 if name in labels]
    values = {}
    if requested:
        try:
            values = entity_catalog._read_card(
                base, deck, entity, actual_reference, requested
            )
        except Exception as exc:
            row["errors"]["card"] = _error_text(exc)
            for name in requested:
                if name.startswith("P") and name[1:].isdigit():
                    row["errors"].setdefault(f"connectivity.{name}", "card value could not be read")
    elif is_point:
        row["errors"]["card"] = "connection point has no readable card fields"

    if "Name" in values:
        name = values["Name"]
        if name is None or type(name) is str:
            row["name"] = name
        else:
            row["errors"]["name"] = "Name card value is not a string or null"
    elif "Name" in labels:
        row["errors"]["name"] = "Name card value is unavailable"

    if "D" in values:
        try:
            row["diameter"] = _finite_number(values["D"], "D", nonnegative=True)
        except ValueError as exc:
            row["errors"]["diameter"] = _error_text(exc)
    elif "D" in labels:
        row["errors"]["diameter"] = "D card value is unavailable"

    card_position = None
    position_labels = [name for name in _POSITION_FIELDS if name in labels]
    if position_labels and len(position_labels) != len(_POSITION_FIELDS):
        row["errors"]["position_fields"] = "XYZ card fields are incomplete"
    elif len(position_labels) == len(_POSITION_FIELDS):
        if all(name in values for name in _POSITION_FIELDS):
            try:
                card_position = [
                    _finite_number(values[name], name) for name in _POSITION_FIELDS
                ]
            except ValueError as exc:
                row["errors"]["position_card"] = _error_text(exc)
        elif "card" not in row["errors"]:
            row["errors"]["position_card"] = "XYZ card values are unavailable"
    elif is_point:
        row["errors"]["position_fields"] = "connection point has no XYZ card fields"

    entity_position = _MISSING
    try:
        entity_position = getattr(entity, "position", _MISSING)
    except Exception as exc:
        row["errors"]["position"] = _error_text(exc)
    if entity_position is not _MISSING:
        try:
            entity_position = _number_vector(entity_position)
        except ValueError as exc:
            row["errors"].setdefault("position", _error_text(exc))
            entity_position = None
    if entity_position is not _MISSING and entity_position is not None:
        row["position"] = entity_position
    elif card_position is not None:
        row["position"] = card_position
    if (entity_position is not _MISSING and entity_position is not None
            and card_position is not None and not _same_position(entity_position, card_position)):
        row["errors"]["position_sources"] = "Entity.position and XYZ card values disagree"

    for field in _CONNECTIVITY_FIELDS:
        if field not in labels:
            if is_point and field in ("P1", "P2"):
                row["errors"][f"connectivity.{field}"] = "required card field is unavailable"
            continue
        if field not in values:
            if f"connectivity.{field}" not in row["errors"]:
                row["errors"][f"connectivity.{field}"] = "card value is unavailable"
            continue
        text = values[field]
        if type(text) is not str:
            row["errors"][f"connectivity.{field}"] = "connectivity card value is not a string"
            continue
        if not text:
            if is_point and field in ("P1", "P2"):
                row["errors"][f"connectivity.{field}"] = "required connectivity is empty"
            continue
        entry = {"field": field, "text": text, "entities": []}
        try:
            resolved = connections.ConnectivityStringToEnts(text)
            if type(resolved) not in (list, tuple) or not resolved:
                raise RuntimeError("ConnectivityStringToEnts returned no entities")
            entry["entities"] = [
                entity_catalog._entity_reference(base, deck, item) for item in resolved
            ]
        except Exception as exc:
            row["errors"][f"connectivity.{field}"] = _error_text(exc)
        row["connectivity"].append(entry)

    row["complete"] = not row["errors"]
    return row


def _connection_list(base, connections, deck, params):
    params = entity_catalog._object(params, (), ("deck", "kind", "offset", "limit"))
    kind = params.get("kind", "all")
    if type(kind) is not str or kind not in ("all", "connector", "point"):
        raise ValueError("kind must be all, connector, or point")
    offset = params.get("offset", 0)
    limit = params.get("limit", 100)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be an integer from 1 to {_MAX_PAGE_SIZE}")

    _require_base_apis(base)
    _api(connections, "ConnectivityStringToEnts", "connections")
    available = _available_types(base, deck)
    snapshot = _snapshot(base, deck, kind, available=available)
    page = snapshot["references"][offset:offset + limit]
    rows = [_connection_row(base, connections, deck, reference) for reference in page]
    result = {
        "deck": deck,
        "kind": kind,
        "count": len(snapshot["references"]),
        "returned": len(rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < len(snapshot["references"]),
        "connections": rows,
        "complete": all(row["complete"] for row in rows),
    }
    if any("entity" in row["errors"] for row in rows):
        result.update(ok=False, error="connection identity could not be re-read; inspect retained rows")
    return result


def _create_params(params):
    params = entity_catalog._object(
        params,
        ("position", "parts", "diameter"),
        ("connection_type", "entity_id", "name", "deck"),
    )
    position = _position(params["position"])
    diameter = _finite_number(params["diameter"], "diameter", positive=True)
    connection_type = params.get("connection_type", "Bolt_Type")
    if type(connection_type) is not str or connection_type not in _POINT_TYPE_SET:
        raise ValueError("connection_type must be one of the six supported point types")
    entity_id = params.get("entity_id", 0)
    if type(entity_id) is not int or not 0 <= entity_id <= _MAX_ENTITY_ID:
        raise ValueError(f"entity_id must be an integer from 0 to {_MAX_ENTITY_ID}")
    name = params.get("name")
    if name is not None and (type(name) is not str or len(name) > 128):
        raise ValueError("name must be a string of at most 128 characters or null")
    parts = params["parts"]
    if type(parts) is not list or len(parts) != 2:
        raise ValueError("parts must contain exactly two ANSAPART references")
    return position, diameter, connection_type, entity_id, name, parts


def _part_connectivity(base, connections, deck, values):
    entities, references = [], []
    for index, value in enumerate(values):
        label = f"parts[{index}]"
        entity_catalog._object(value, ("deck", "type", "id"), label=label)
        if value["type"] != "ANSAPART":
            raise ValueError(f"{label}.type must be ANSAPART")
        resolved = entity_catalog._reference_input(value, base, deck)
        if resolved is None:
            raise ValueError(f"{label} must be an exact entity reference")
        entity, reference = resolved
        if reference["type"] != "ANSAPART":
            raise ValueError(f"{label} does not resolve to an ANSAPART")
        entities.append(entity)
        references.append(reference)
    keys = [(item["deck"], item["type"], item["id"]) for item in references]
    if len(set(keys)) != 2:
        raise ValueError("parts must refer to two different ANSAPART entities")

    strings = []
    for entity, reference in zip(entities, references):
        text = connections.EntsToConnectivityString([entity])
        if type(text) is not str or not text:
            raise RuntimeError("EntsToConnectivityString did not return a nonempty string")
        resolved = connections.ConnectivityStringToEnts(text)
        if type(resolved) not in (list, tuple) or len(resolved) != 1:
            raise RuntimeError("connectivity string did not resolve to exactly one entity")
        round_trip = entity_catalog._entity_reference(base, deck, resolved[0])
        if round_trip != reference:
            raise RuntimeError("connectivity string did not resolve to its original ANSAPART")
        strings.append(text)
    if strings[0] == strings[1]:
        raise ValueError("parts resolve to the same connectivity string")
    return entities, references, strings


def _ranges(before, after, created):
    observed = after["observed_types"]
    added = [
        reference for reference in after["references"]
        if (reference["type"], reference["id"]) not in before["by_key"]
    ]
    removed = [
        reference for reference in before["references"]
        if reference["type"] in observed
        and (reference["type"], reference["id"]) not in after["by_key"]
    ]
    if created is not None and (created["type"], created["id"]) not in before["by_key"]:
        if not any(item == created for item in added):
            added.append(created)
    added.sort(key=lambda item: (item["type"], item["id"]))
    removed.sort(key=lambda item: (item["type"], item["id"]))
    if after["errors"]:
        return added or None, removed or None
    return added, removed


def _row_connectivity_matches(row, part_references):
    expected = [
        {"field": f"P{index}", "entities": [reference]}
        for index, reference in enumerate(part_references, start=1)
    ]
    actual = [
        {"field": item["field"], "entities": item["entities"]}
        for item in row["connectivity"]
    ]
    return actual == expected


def _create_failure(error, created, connection, added, removed, observation_errors):
    return {
        "ok": False,
        "error": str(error)[:240],
        "created": created,
        "connection": connection,
        "added": added,
        "removed": removed,
        "observation_errors": observation_errors,
    }


def _connection_create(base, connections, deck, params):
    position, diameter, connection_type, entity_id, name, parts = _create_params(params)
    _require_base_apis(base)
    create = _api(connections, "CreateConnectionPoint", "connections")
    _api(connections, "EntsToConnectivityString", "connections")
    _api(connections, "ConnectivityStringToEnts", "connections")
    set_values = _api(base, "SetEntityCardValues", "base")

    available = _available_types(base, deck)
    if connection_type not in available:
        raise NotImplementedError(f"connection type {connection_type} is unavailable in this deck")
    part_entities, part_references, strings = _part_connectivity(
        base, connections, deck, parts
    )
    before = _snapshot(base, deck, "all", available=available)
    if entity_id and any(reference["id"] == entity_id for reference in before["references"]):
        raise ValueError("entity_id is already used in the connection/connector range")

    created = None
    created_entity = None
    connection = None
    write_error = None
    observation_errors = {}
    try:
        # The 4-argument call is confirmed by the ANSA 2026.1 native probe;
        # the 5-argument stub form silently leaves D at zero on this build.
        created_entity = create(connection_type, position, entity_id, strings)
        if created_entity is None:
            raise RuntimeError("CreateConnectionPoint returned None")
        created = entity_catalog._entity_reference(base, deck, created_entity)
        if created["type"] != connection_type:
            raise RuntimeError("CreateConnectionPoint returned an unexpected entity type")
        if entity_id and created["id"] != entity_id:
            raise RuntimeError("CreateConnectionPoint returned an unexpected entity ID")
        created_entity, resolved = _resolve_connection(base, deck, created)
        if resolved != created:
            raise RuntimeError("created point identity changed during re-resolution")
        labels = entity_catalog._entity_card_labels(created_entity, deck)
        if "D" not in labels:
            raise RuntimeError("created point card_fields does not contain D")
        if name is not None and "Name" not in labels:
            raise RuntimeError("created point card_fields does not contain requested Name")
        fields = {"D": diameter}
        if name is not None:
            fields["Name"] = name
        status = set_values(deck, created_entity, fields)
        if type(status) is not int or status != 0:
            raise RuntimeError(
                "SetEntityCardValues returned "
                f"{repr(status)[:120]}; expected integer 0"
            )
    except Exception as exc:
        write_error = _error_text(exc)
        observation_errors["write"] = write_error

    added = (
        [created] if created is not None
        and (created["type"], created["id"]) not in before["by_key"]
        else None
    )
    removed = None
    try:
        after = _snapshot(base, deck, "all", tolerate_errors=True)
        if after["available_types"] != available:
            after["errors"].setdefault(
                "TypesInCategory", "available connection types changed during creation"
            )
        added, removed = _ranges(before, after, created)

        if created is None:
            new_points = [reference for reference in (added or ())
                          if reference["type"] in _POINT_TYPE_SET]
            if len(new_points) == 1:
                created = new_points[0]

        if after["errors"]:
            observation_errors["range"] = after["errors"]

        if created is not None and created["type"] in _POINT_TYPE_SET:
            try:
                connection = _connection_row(base, connections, deck, created)
                if connection["errors"]:
                    observation_errors["connection"] = connection["errors"]
            except Exception as exc:
                observation_errors["connection"] = _error_text(exc)

        mismatches = []
        if write_error is None:
            if created is None:
                mismatches.append("created entity identity could not be observed")
            if connection is None:
                mismatches.append("created connection could not be read back")
            else:
                if connection["entity"] != created or connection["kind"] != "point":
                    mismatches.append("created entity identity or kind did not read back")
                if not connection["complete"]:
                    mismatches.append("created connection row is incomplete")
                if not _same_position(connection["position"], position):
                    mismatches.append("created position did not read back")
                if (type(connection["diameter"]) not in (int, float)
                        or connection["diameter"] <= 0
                        or not math.isclose(connection["diameter"], diameter,
                                            rel_tol=1e-9, abs_tol=1e-9)):
                    mismatches.append("created diameter did not read back")
                if name is not None and connection["name"] != name:
                    mismatches.append("created name did not read back")
                if not _row_connectivity_matches(connection, part_references):
                    mismatches.append("created P1/P2 connectivity did not read back in order")

        if mismatches:
            observation_errors["verification"] = mismatches

        range_ok = (
            not after["errors"] and added == [created] and removed == []
            and created is not None and created["type"] == connection_type
        )
        if not range_ok:
            observation_errors.setdefault(
                "range_verification",
                "expected exactly the created point and no other additions or removals",
            )

        if write_error is not None or mismatches or observation_errors or not range_ok:
            reason = write_error
            if reason is None and mismatches:
                reason = mismatches[0]
            if reason is None and observation_errors:
                reason = "post-create observation failed"
            if reason is None:
                reason = "connection range did not match the request"
            return _create_failure(
                reason, created, connection, added, removed, observation_errors
            )

        return {
            "ok": True,
            "deck": deck,
            "created": created,
            "connection": connection,
            "added": added,
            "removed": removed,
        }
    except Exception as exc:
        observation_errors.setdefault("post_create", _error_text(exc))
        return _create_failure(
            write_error or _error_text(exc),
            created,
            connection,
            added,
            removed,
            observation_errors,
        )


def execute(state, tool, params):
    """Run one connection operation on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool not in ("connection_list", "connection_create"):
        raise ValueError(f"unsupported connection operation: {tool!r}")
    if type(params) is not dict:
        raise TypeError("params must be an object")

    from ansa import base, connections, constants

    deck = entity_catalog._deck(base, params)
    _check_deck(deck, constants)
    if tool == "connection_list":
        result = _connection_list(base, connections, deck, params)
    else:
        result = _connection_create(base, connections, deck, params)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"connection operation result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
