"""Generic current-model entity queries and guarded card CRUD."""

from __future__ import annotations

import json
import math


_META_FIELDS = frozenset({"__id__", "__type__", "__prop__", "__part__", "MBContainer"})
_MAX_PAGE = 200
_MAX_FIELDS = 100
_MAX_BATCH = 200
_MAX_TYPE_NAMES_IN_ERROR = 100
_NASTRAN_NODE_FIELDS = ("X1", "X2", "X3")


def _object(value, required, optional=(), label="params"):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    required, optional = set(required), set(optional)
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing:
        raise ValueError(f"{label} missing key(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{label} has unsupported key(s): {', '.join(sorted(extra))}")
    return value


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _deck(base, params):
    actual = base.CurrentDeck()
    if type(actual) is not int:
        raise RuntimeError("CurrentDeck did not return an integer")
    if "deck" in params:
        requested = params["deck"]
        if type(requested) is not int:
            raise TypeError("deck must be an integer")
        if requested != actual:
            raise ValueError("requested deck is not the current ANSA deck")
    return actual


def _entity_type(value):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("entity_type must be a non-empty exact ANSA type name")
    return value


def _entity_card_labels(entity, deck):
    card_fields = getattr(entity, "card_fields", None)
    if not callable(card_fields):
        raise NotImplementedError("ANSA entity.card_fields is unavailable")
    labels = card_fields(deck)
    if type(labels) not in (list, tuple):
        raise RuntimeError("Entity.card_fields did not return a field-name list")
    if any(type(label) is not str for label in labels):
        raise RuntimeError("Entity.card_fields returned an invalid field name")
    return frozenset(labels)


def _keyword_card_labels(base, deck, entity_type):
    get_labels = getattr(base, "GetKeywordFieldLabels", None)
    if not callable(get_labels):
        raise NotImplementedError("ANSA base.GetKeywordFieldLabels is unavailable")
    labels = get_labels(deck, entity_type)
    if labels is None or type(labels) not in (list, tuple):
        raise RuntimeError(f"ANSA did not return a field definition for {entity_type}")
    if any(type(label) is not str for label in labels):
        raise RuntimeError(f"ANSA returned an invalid field definition for {entity_type}")
    return frozenset(labels)


def _available_entity_types(base, deck):
    types_in_category = getattr(base, "TypesInCategory", None)
    if not callable(types_in_category):
        raise NotImplementedError("ANSA base.TypesInCategory is unavailable")
    names = types_in_category(deck, "__ALL_ENTITIES__")
    if names is None:
        raise RuntimeError("ANSA TypesInCategory(__ALL_ENTITIES__) failed")
    if type(names) not in (list, tuple) or any(type(name) is not str for name in names):
        raise RuntimeError("ANSA TypesInCategory returned an invalid entity-type list")
    return tuple(dict.fromkeys(names))


def _ensure_entity_type(base, deck, entity_type):
    available = _available_entity_types(base, deck)
    if entity_type not in available:
        shown = available[:_MAX_TYPE_NAMES_IN_ERROR]
        detail = ", ".join(shown) if shown else "<none>"
        if len(available) > len(shown):
            detail += f", ... ({len(available)} types total)"
        raise ValueError(
            f"entity type {entity_type!r} is unavailable in the current deck; "
            f"TypesInCategory(__ALL_ENTITIES__) returned: {detail}"
        )


def _field_names(value, labels, *, allow_meta=True, label="fields", nonempty=False):
    if type(value) not in (list, tuple):
        raise TypeError(f"{label} must be an array of field names")
    if len(value) > _MAX_FIELDS:
        raise ValueError(f"{label} may contain at most {_MAX_FIELDS} fields")
    if any(type(item) is not str or not item for item in value):
        raise ValueError(f"{label} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not contain duplicate field names")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    if labels is not None:
        allowed = set(labels) | (set(_META_FIELDS) if allow_meta else set())
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unsupported card field(s): {', '.join(sorted(unknown))}")
    return tuple(value)


def _write_fields(value, label, *, labels=None, allow_empty=False):
    if type(value) is not dict or (not value and not allow_empty):
        qualifier = "object" if allow_empty else "non-empty object"
        raise ValueError(f"{label} must be an {qualifier} of card fields")
    if len(value) > _MAX_FIELDS:
        raise ValueError(f"{label} may contain at most {_MAX_FIELDS} fields")
    names = tuple(value)
    if any(type(name) is not str or not name for name in names):
        raise ValueError(f"{label} keys must be non-empty strings")
    if set(names) & _META_FIELDS:
        raise ValueError(f"{label} must not write ANSA metadata fields")
    return _field_names(names, labels, allow_meta=False, label=label, nonempty=not allow_empty)


def _read_fields(value, labels, label):
    if value is None:
        value = []
    return _field_names(value, labels, label=label)


def _entity_metadata(base, deck, entity):
    values = base.GetEntityCardValues(deck, entity, ("__id__", "__type__"))
    if type(values) is not dict or "__id__" not in values or "__type__" not in values:
        raise RuntimeError("ANSA did not return entity ID and type metadata")
    entity_id, entity_type = values["__id__"], values["__type__"]
    if type(entity_id) is not int or entity_id <= 0:
        raise RuntimeError("ANSA returned an invalid entity ID")
    if type(entity_type) is not str or not entity_type:
        raise RuntimeError("ANSA returned an invalid entity type")
    return {"deck": deck, "type": entity_type, "id": entity_id}


def _entity_reference(base, deck, entity, expected_type=None, expected_id=None):
    reference = _entity_metadata(base, deck, entity)
    if expected_type is not None and reference["type"] != expected_type:
        raise RuntimeError("ANSA entity type does not match the requested type")
    if expected_id is not None and reference["id"] != expected_id:
        raise RuntimeError("ANSA entity ID does not match the requested ID")
    return reference


def _get_entity(base, deck, entity_type, entity_id):
    entity_id = _positive_int(entity_id, "entity_id")
    entity = base.GetEntity(deck, entity_type, entity_id)
    if entity is None:
        raise LookupError(f"{entity_type} {entity_id} does not exist in deck {deck}")
    reference = _entity_reference(base, deck, entity, entity_type, entity_id)
    return entity, reference


def _is_entity(value, base):
    entity_class = getattr(base, "Entity", None)
    if isinstance(entity_class, type) and isinstance(value, entity_class):
        return True
    return (type(getattr(value, "_id", None)) is int
            and callable(getattr(value, "ansa_type", None)))


def _reference_input(value, base, deck):
    if type(value) is not dict or set(value) != {"deck", "type", "id"}:
        return None
    if type(value["deck"]) is not int or value["deck"] != deck:
        raise ValueError("entity reference deck must match the current deck")
    entity_type = _entity_type(value["type"])
    entity_id = _positive_int(value["id"], "entity reference id")
    entity, reference = _get_entity(base, deck, entity_type, entity_id)
    return entity, reference


def _json_value(value, base, deck, active=None, depth=0):
    if depth > 32:
        raise ValueError("ANSA card value exceeds the JSON nesting limit")
    if _is_entity(value, base):
        return _entity_reference(base, deck, value)
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("ANSA returned a non-finite number")
        return value

    if active is None:
        active = set()
    if type(value) in (list, tuple):
        marker = id(value)
        if marker in active:
            raise ValueError("ANSA returned a cyclic vector")
        active.add(marker)
        try:
            return [_json_value(item, base, deck, active, depth + 1) for item in value]
        finally:
            active.remove(marker)
    if type(value) is dict:
        marker = id(value)
        if marker in active:
            raise ValueError("ANSA returned a cyclic mapping")
        active.add(marker)
        try:
            if any(type(key) is not str for key in value):
                raise TypeError("ANSA returned an object with a non-string key")
            return {key: _json_value(item, base, deck, active, depth + 1)
                    for key, item in value.items()}
        finally:
            active.remove(marker)
    raise TypeError(f"unsupported ANSA value type: {type(value).__name__}")


def _input_value(value, base, deck, active=None, depth=0):
    if depth > 32:
        raise ValueError("input card value exceeds the JSON nesting limit")
    reference = _reference_input(value, base, deck)
    if reference is not None:
        return reference
    if value is None or type(value) in (str, int, bool):
        return value, value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("input numbers must be finite")
        return value, value

    if active is None:
        active = set()
    if type(value) is list:
        marker = id(value)
        if marker in active:
            raise ValueError("input contains a cyclic array")
        active.add(marker)
        try:
            pairs = [_input_value(item, base, deck, active, depth + 1) for item in value]
            return [item[0] for item in pairs], [item[1] for item in pairs]
        finally:
            active.remove(marker)
    if type(value) is dict:
        marker = id(value)
        if marker in active:
            raise ValueError("input contains a cyclic object")
        active.add(marker)
        try:
            if any(type(key) is not str for key in value):
                raise TypeError("input object keys must be strings")
            pairs = {key: _input_value(item, base, deck, active, depth + 1)
                     for key, item in value.items()}
            return ({key: item[0] for key, item in pairs.items()},
                    {key: item[1] for key, item in pairs.items()})
        finally:
            active.remove(marker)
    raise TypeError(f"unsupported input value type: {type(value).__name__}")


def _strict_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left is right
    if type(left) in (int, float) and type(right) in (int, float):
        return ((type(left) is not float or math.isfinite(left))
                and (type(right) is not float or math.isfinite(right))
                and left == right)
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_strict_equal(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _normalize_card_input(base, deck, values, label, *, labels=None, expected=False, allow_empty=False):
    names = _write_fields(values, label, labels=labels, allow_empty=allow_empty)
    if expected:
        if not names:
            raise ValueError(f"{label} must include at least one ordinary card field")
    prepared, wire = {}, {}
    for name in names:
        prepared[name], wire[name] = _input_value(values[name], base, deck)
    return prepared, wire


def _read_card(base, deck, entity, reference, fields):
    requested = tuple(dict.fromkeys(("__id__", "__type__", *fields)))
    values = base.GetEntityCardValues(deck, entity, requested)
    if type(values) is not dict or any(name not in values for name in requested):
        absent = [name for name in requested if type(values) is not dict or name not in values]
        raise ValueError("ANSA did not return requested card field(s): " + ", ".join(absent))
    if values["__id__"] != reference["id"] or values["__type__"] != reference["type"]:
        raise RuntimeError("entity identity changed while reading its card")
    return {name: _json_value(values[name], base, deck) for name in fields}


def _matches_expected(actual, expected):
    changed = [name for name, value in expected.items()
               if name not in actual or not _strict_equal(actual[name], value)]
    if changed:
        raise RuntimeError("entity fields changed since they were read: " + ", ".join(changed))


def _collect(base, deck, entity_type):
    entities = base.CollectEntities(deck, None, entity_type, False)
    if entities is None:
        return []
    if type(entities) not in (list, tuple):
        raise RuntimeError("CollectEntities did not return an entity list")
    return list(entities)


def _page(params):
    offset = _nonnegative_int(params.get("offset", 0), "offset")
    limit = params.get("limit", 100)
    if type(limit) is not int or limit < 1 or limit > _MAX_PAGE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE}")
    return offset, limit


def _validate_entity_values(base, deck, entity_type, entity_id, expected_fields):
    entity, reference = _get_entity(base, deck, entity_type, entity_id)
    labels = _entity_card_labels(entity, deck)
    expected, expected_wire = _normalize_card_input(
        base, deck, expected_fields, "expected_fields", labels=labels, expected=True
    )
    actual = _read_card(base, deck, entity, reference, tuple(expected))
    _matches_expected(actual, expected_wire)
    return entity, reference, expected_wire, labels


def _entity_count(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    entities = _collect(base, deck, entity_type)
    if not entities:
        _ensure_entity_type(base, deck, entity_type)
    return {"deck": deck, "entity_type": entity_type,
            "count": len(entities)}


def _entity_list(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    offset, limit = _page(params)
    requested_fields = _read_fields(params.get("fields"), None, "fields")
    entities = _collect(base, deck, entity_type)
    if not entities:
        _ensure_entity_type(base, deck, entity_type)
        return {"deck": deck, "entity_type": entity_type, "total": 0,
                "offset": offset, "limit": limit, "entities": [],
                "fields_validated": not requested_fields}

    page = entities[offset:offset + limit]
    page_fields = []
    if requested_fields:
        if page:
            for entity in page:
                labels = _entity_card_labels(entity, deck)
                page_fields.append(_read_fields(requested_fields, labels, "fields"))
        else:
            _read_fields(requested_fields, _entity_card_labels(entities[0], deck), "fields")
    rows = []
    for index, entity in enumerate(page):
        reference = _entity_reference(base, deck, entity, entity_type)
        if requested_fields:
            values = _read_card(base, deck, entity, reference, page_fields[index])
        else:
            values = {}
        rows.append({"entity": reference, "fields": values})
    return {"deck": deck, "entity_type": entity_type, "total": len(entities),
            "offset": offset, "limit": limit, "entities": rows,
            "fields_validated": True}


def _entity_get(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    entity_id = _positive_int(params["entity_id"], "entity_id")
    entity, reference = _get_entity(base, deck, entity_type, entity_id)
    requested_fields = _read_fields(params.get("fields"), None, "fields")
    fields = (_read_fields(requested_fields, _entity_card_labels(entity, deck), "fields")
              if requested_fields else ())
    values = _read_card(base, deck, entity, reference, fields) if fields else {}
    return {"entity": reference, "fields": values}


def _entity_create(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    _ensure_entity_type(base, deck, entity_type)
    prepared, expected = _normalize_card_input(
        base, deck, params["fields"], "fields", allow_empty=True
    )
    if expected:
        existing = _collect(base, deck, entity_type)
        if existing:
            _entity_reference(base, deck, existing[0], entity_type)
            field_definition = _entity_card_labels(existing[0], deck)
        else:
            field_definition = _keyword_card_labels(base, deck, entity_type)
            if not field_definition:
                raise ValueError(
                    f"cannot verify non-empty fields for first {entity_type} creation: "
                    "there is no existing entity and GetKeywordFieldLabels returned no fields; "
                    "create with fields={} first, then query or update that entity"
                )
        _read_fields(tuple(expected), field_definition, "fields")

    create_entity = getattr(base, "CreateEntity", None)
    if not callable(create_entity):
        raise NotImplementedError("ANSA base.CreateEntity is unavailable; no entity was created")
    try:
        created = create_entity(deck, entity_type, prepared)
    except Exception as exc:
        raise RuntimeError(
            f"CreateEntity outcome is uncertain for {entity_type}; it was called once and not retried, "
            "so a side effect may remain"
        ) from exc
    if created is None:
        raise RuntimeError(
            f"CreateEntity returned no entity for {entity_type}; the outcome cannot be confirmed, "
            "a side effect may remain, and the operation was not retried"
        )
    try:
        created_reference = _entity_reference(base, deck, created)
    except Exception as exc:
        raise RuntimeError(
            f"CreateEntity returned an entity for {entity_type}, but its ID could not be confirmed; "
            "a side effect may remain and the operation was not retried"
        ) from exc
    if created_reference["type"] != entity_type:
        raise RuntimeError(
            f"CreateEntity returned {created_reference['type']} {created_reference['id']} "
            f"instead of {entity_type}; the entity may remain in the model"
        )

    try:
        created_labels = _entity_card_labels(created, deck)
        _read_fields(tuple(expected), created_labels, "fields")
        entity, reference = _get_entity(base, deck, entity_type, created_reference["id"])
        values = _read_card(base, deck, entity, reference, tuple(expected))
        _matches_expected(values, expected)
    except Exception as exc:
        raise RuntimeError(
            f"CreateEntity verification failed for {entity_type} {created_reference['id']}; "
            f"the entity may remain in the model and was not deleted or retried: {exc}"
        ) from exc
    return {"entity": reference, "fields": values}


def _entity_update(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    entity_id = _positive_int(params["entity_id"], "entity_id")
    entity, reference, _before, labels = _validate_entity_values(
        base, deck, entity_type, entity_id, params["expected_fields"]
    )
    prepared, expected_after = _normalize_card_input(
        base, deck, params["fields"], "fields", labels=labels
    )
    status = None
    api_error = None
    try:
        status = base.SetEntityCardValues(deck, entity, prepared)
    except Exception as exc:
        api_error = exc
    try:
        refreshed, reference = _get_entity(base, deck, entity_type, entity_id)
        values = _read_card(base, deck, refreshed, reference, tuple(expected_after))
        _matches_expected(values, expected_after)
    except Exception as verify_error:
        if api_error is not None:
            raise RuntimeError("entity update outcome could not be confirmed") from api_error
        raise verify_error
    result = {"entity": reference, "fields": values}
    if api_error is not None:
        result["warnings"] = ["ANSA raised during update; the requested fields were confirmed by readback"]
    elif status != 0:
        result["warnings"] = [f"SetEntityCardValues returned {status!r}; requested fields were confirmed by readback"]
    return result


def _entity_delete(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    if entity_type == "FACE":
        raise ValueError("FACE deletion requires geometry_delete with fresh geometry and boundary conditions")
    items = params["entities"]
    if type(items) is not list or not items or len(items) > _MAX_BATCH:
        raise ValueError(f"entities must contain between 1 and {_MAX_BATCH} entries")
    validated = []
    seen = set()
    # Validate every entity and its precondition before the first mutation.
    for index, item in enumerate(items):
        item = _object(item, ("entity_id", "expected_fields"), label=f"entities[{index}]")
        entity_id = _positive_int(item["entity_id"], f"entities[{index}].entity_id")
        if entity_id in seen:
            raise ValueError("entities must not contain duplicate IDs")
        seen.add(entity_id)
        entity, reference, _expected, _labels = _validate_entity_values(
            base, deck, entity_type, entity_id, item["expected_fields"]
        )
        validated.append((entity, reference))

    status = None
    api_error = None
    try:
        status = base.DeleteEntity([entity for entity, _ in validated], force=False, compress=False)
    except Exception as exc:
        api_error = exc

    remaining = []
    read_errors = []
    for _, reference in validated:
        try:
            entity = base.GetEntity(deck, entity_type, reference["id"])
            if entity is not None:
                _entity_reference(base, deck, entity, entity_type, reference["id"])
                remaining.append(reference["id"])
        except Exception as exc:
            read_errors.append(f"{reference['id']}: {type(exc).__name__}")
    if read_errors or remaining:
        detail = []
        if remaining:
            detail.append("remaining IDs: " + ", ".join(map(str, remaining)))
        if read_errors:
            detail.append("readback errors: " + ", ".join(read_errors))
        if api_error is not None:
            raise RuntimeError("batch delete outcome is uncertain (" + "; ".join(detail) + ")") from api_error
        raise RuntimeError("batch delete was not confirmed (" + "; ".join(detail) + ")")

    result = {"deleted": [reference for _, reference in validated]}
    if api_error is not None:
        result["warnings"] = ["ANSA raised during delete; absence was confirmed for every requested entity"]
    elif status != 0:
        result["warnings"] = [f"DeleteEntity returned {status!r}; absence was confirmed for every requested entity"]
    return result


def _entity_find(base, constants, deck, params):
    pattern = params["pattern"]
    if type(pattern) is not str or not pattern:
        raise ValueError("pattern must be a non-empty string")
    match_mode = params.get("match", "substring")
    if type(match_mode) is not str:
        raise TypeError("match must be a string")
    constant_names = {
        "regex": "ENM_REGEX",
        "exact": "ENM_EXACT",
        "substring": "ENM_SUBSTRING",
        "substring_ignorecase": "ENM_SUBSTRING_IGNORECASE",
    }
    if match_mode not in constant_names:
        raise ValueError("match must be regex, exact, substring, or substring_ignorecase")
    match_constant = getattr(constants, constant_names[match_mode], None)
    if match_constant is None:
        raise NotImplementedError(f"ANSA does not expose the {constant_names[match_mode]} match mode")
    offset, limit = _page(params)
    found = base.NameToEnts(pattern, deck, match_constant) or []
    if type(found) not in (list, tuple):
        raise RuntimeError("NameToEnts did not return an entity list")
    rows = [_entity_reference(base, deck, entity) for entity in found[offset:offset + limit]]
    return {"deck": deck, "pattern": pattern, "match": match_mode,
            "total": len(found), "offset": offset, "limit": limit, "entities": rows}


def _entity_bounds(base, deck, params):
    entity_type = _entity_type(params["entity_type"])
    ids = params["entity_ids"]
    if type(ids) is not list or not ids or len(ids) > _MAX_BATCH:
        raise ValueError(f"entity_ids must contain between 1 and {_MAX_BATCH} IDs")
    ids = [_positive_int(entity_id, "entity_id") for entity_id in ids]
    if len(set(ids)) != len(ids):
        raise ValueError("entity_ids must not contain duplicates")
    entities, references = [], []
    for entity_id in ids:
        entity, reference = _get_entity(base, deck, entity_type, entity_id)
        entities.append(entity)
        references.append(reference)
    raw = base.BoundBox(entities)
    if type(raw) not in (list, tuple) or len(raw) != 6:
        raise ValueError("BoundBox is unsupported for the requested entity set")
    bounds = []
    for value in raw:
        if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError("BoundBox returned a non-finite or non-numeric coordinate")
        bounds.append(float(value))
    return {"entities": references,
            "bounds": {"min": bounds[:3], "max": bounds[3:]}}


def _node_coordinates(base, constants, deck, params):
    if deck != constants.NASTRAN:
        raise NotImplementedError("node_coordinates is verified only for NASTRAN GRID")
    ids = params["node_ids"]
    if type(ids) is not list or not ids or len(ids) > _MAX_BATCH:
        raise ValueError(f"node_ids must contain between 1 and {_MAX_BATCH} IDs")
    ids = [_positive_int(node_id, "node_id") for node_id in ids]
    if len(set(ids)) != len(ids):
        raise ValueError("node_ids must not contain duplicates")
    nodes, missing = [], []
    for node_id in ids:
        try:
            entity, reference = _get_entity(base, deck, "GRID", node_id)
        except LookupError:
            missing.append(node_id)
            continue
        labels = _entity_card_labels(entity, deck)
        missing_fields = set(_NASTRAN_NODE_FIELDS) - labels
        if missing_fields:
            raise NotImplementedError(
                f"NASTRAN GRID {node_id} coordinate fields are unavailable: "
                + ", ".join(sorted(missing_fields))
            )
        fields = _read_card(base, deck, entity, reference, _NASTRAN_NODE_FIELDS)
        coordinates = [fields[name] for name in _NASTRAN_NODE_FIELDS]
        if any(type(value) not in (int, float) or isinstance(value, bool)
               or not math.isfinite(float(value)) for value in coordinates):
            raise ValueError(f"GRID {node_id} has invalid coordinate values")
        nodes.append({"entity": reference, "coordinates": [float(v) for v in coordinates]})
    return {"deck": deck, "nodes": nodes, "missing": missing}


def _model_summary(base, constants, deck):
    from ansa import session

    database = base.DataBaseName()
    if type(database) is not str:
        raise TypeError("DataBaseName did not return a string")
    deck_name = session.DeckName(deck)
    if type(deck_name) is not str or not deck_name:
        raise TypeError("DeckName did not return a non-empty string")
    all_entities = base.CollectEntities(deck, None, "__ALL_ENTITIES__", False)
    if type(all_entities) not in (list, tuple):
        raise RuntimeError("CollectEntities(__ALL_ENTITIES__) did not return an entity list")
    entity_count = len(all_entities)
    del all_entities
    node_type = next((name for constant, name in (("NASTRAN", "GRID"), ("ABAQUS", "NODE"))
                      if type(getattr(constants, constant, None)) is int
                      and getattr(constants, constant) == deck), None)
    count_types = {"parts": "ANSAPART", "materials": "__MATERIALS__", "properties": "__PROPERTIES__",
                   "faces": "FACE", "nodes": node_type, "shells": "SHELL", "solids": "SOLID",
                   "elements": "__ELEMENTS__"}
    counts, errors = {}, {}
    try:
        available = _available_entity_types(base, deck)
        type_error = None
    except Exception as exc:
        available = ()
        type_error = f"{type(exc).__name__}: {exc}"[:512]
    for category, entity_type in count_types.items():
        try:
            if entity_type is None:
                raise NotImplementedError("Node type is only verified for NASTRAN and ABAQUS")
            if not entity_type.startswith("__"):
                if type_error is not None:
                    raise RuntimeError(type_error)
                if entity_type not in available:
                    raise NotImplementedError(f"Entity type {entity_type} is unavailable in deck {deck}")
            entities = base.CollectEntities(deck, None, entity_type, False)
            if type(entities) not in (list, tuple):
                raise RuntimeError(f"CollectEntities({entity_type}) did not return an entity list")
            counts[category] = len(entities)
        except Exception as exc:
            counts[category] = None
            errors[category] = f"{type(exc).__name__}: {exc}"[:512]
    return {"deck": deck, "deck_name": deck_name, "database": database, "entity_count": entity_count,
            "node_type": node_type, "entity_counts": counts, "count_types": count_types,
            "counts_complete": not errors, "count_errors": errors}


def _model_includes(base, deck, params):
    offset, limit = _page(params)
    includes = [(_entity_reference(base, deck, include, "INCLUDE"), include)
                for include in _collect(base, deck, "INCLUDE")]
    includes.sort(key=lambda item: item[0]["id"])
    if len({reference["id"] for reference, _include in includes}) != len(includes):
        raise RuntimeError("ANSA returned duplicate INCLUDE identities")
    if not includes:
        return {"deck": deck, "total": 0, "offset": offset,
                "limit": limit, "includes": []}
    rows = []
    for reference, include in includes[offset:offset + limit]:
        labels = _entity_card_labels(include, deck)
        fields = ("Name",) if "Name" in labels else ()
        values = _read_card(base, deck, include, reference, fields) if fields else {}
        rows.append({"entity": reference, "fields": values})
    return {"deck": deck, "total": len(includes), "offset": offset,
            "limit": limit, "includes": rows}


def _dispatch(base, constants, deck, tool, params):
    if tool == "entity_count":
        params = _object(params, ("entity_type",), ("deck",))
        return _entity_count(base, deck, params)
    if tool == "entity_list":
        params = _object(params, ("entity_type",), ("deck", "fields", "offset", "limit"))
        return _entity_list(base, deck, params)
    if tool == "entity_get":
        params = _object(params, ("entity_type", "entity_id"), ("deck", "fields"))
        return _entity_get(base, deck, params)
    if tool == "entity_create":
        params = _object(params, ("entity_type", "fields"), ("deck",))
        return _entity_create(base, deck, params)
    if tool == "entity_update":
        params = _object(params, ("entity_type", "entity_id", "expected_fields", "fields"), ("deck",))
        return _entity_update(base, deck, params)
    if tool == "entity_delete":
        params = _object(params, ("entity_type", "entities"), ("deck",))
        return _entity_delete(base, deck, params)
    if tool == "entity_find":
        params = _object(params, ("pattern",), ("deck", "match", "offset", "limit"))
        return _entity_find(base, constants, deck, params)
    if tool == "entity_bounds":
        params = _object(params, ("entity_type", "entity_ids"), ("deck",))
        return _entity_bounds(base, deck, params)
    if tool == "node_coordinates":
        params = _object(params, ("node_ids",), ("deck",))
        return _node_coordinates(base, constants, deck, params)
    if tool == "model_summary":
        params = _object(params, (), ("deck",))
        return _model_summary(base, constants, deck)
    if tool == "model_includes":
        params = _object(params, (), ("deck", "offset", "limit"))
        return _model_includes(base, deck, params)
    raise ValueError(f"unsupported entity catalog operation: {tool!r}")


def execute(state, tool, params):
    """Run one entity catalog operation on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if type(params) is not dict:
        raise TypeError("params must be an object")
    from ansa import base, constants

    deck = _deck(base, params)
    result = _dispatch(base, constants, deck, tool, params)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"operation result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
