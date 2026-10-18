"""Read-only, explicitly scoped NASTRAN shell penetration checks."""

import math

from . import inspection, mesh_checks


_API = "ansa.base.CheckPenetration2Sets"
_MAX_GROUP_IDS = 200
_MAX_NATIVE_ENTITIES = 400
_MAX_NATIVE_COUNT = (1 << 63) - 1
_MISSING = object()


def _positive_finite_float(value, label, error_type=ValueError):
    if type(value) not in (int, float):
        raise error_type(f"{label} must be a positive finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise error_type(f"{label} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise error_type(f"{label} must be a positive finite number")
    return number


def _card_field_names(entity, deck, required, label):
    card_fields = getattr(entity, "card_fields", None)
    if not callable(card_fields):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {label}.card_fields is required"
        )
    try:
        fields = card_fields(deck)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {label}.card_fields could not verify fields"
        ) from exc
    if (type(fields) not in (list, tuple)
            or any(type(field) is not str or not field for field in fields)
            or len(set(fields)) != len(fields)):
        raise RuntimeError(f"{label}.card_fields returned an invalid field-name list")
    missing = [field for field in required if field not in fields]
    if missing:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {label} is missing required card fields {missing}"
        )
    return fields


def _property_context(base, deck, shells, shell_references, cache):
    context = []
    property_ids = set()
    for shell, shell_reference in zip(shells, shell_references):
        shell_id = shell_reference["id"]
        label = f"SHELL {shell_id}"
        _card_field_names(shell, deck, ("PID",), label)
        values = base.GetEntityCardValues(deck, shell, ("PID",))
        if type(values) is not dict or "PID" not in values:
            raise RuntimeError(f"ANSA did not return the explicit PID field for {label}")
        property_id = values["PID"]
        if type(property_id) is not int or property_id <= 0:
            raise RuntimeError(f"ANSA returned an invalid PSHELL PID for {label}")

        if property_id not in cache:
            prop, _ = inspection._get_entity(base, deck, "PSHELL", property_id)
            if not inspection._is_entity(prop, base):
                raise RuntimeError(f"ANSA returned an ambiguous PSHELL {property_id} entity")
            property_label = f"PSHELL {property_id}"
            _card_field_names(prop, deck, ("T",), property_label)
            property_values = base.GetEntityCardValues(deck, prop, ("T",))
            if type(property_values) is not dict or "T" not in property_values:
                raise RuntimeError(
                    f"ANSA did not return the explicit T field for {property_label}"
                )
            cache[property_id] = _positive_finite_float(
                property_values["T"], f"{property_label}.T", RuntimeError
            )

        property_ids.add(property_id)
        context.append({
            "shell_id": shell_id,
            "property_id": property_id,
            "thickness": cache[property_id],
        })
    return context, property_ids


def _native_category(base, deck, raw_entities, label, allowed_ids, first_ids, second_ids):
    references_by_id = {}
    for entity in raw_entities:
        if not inspection._is_entity(entity, base):
            raise RuntimeError(f"{_API} returned an ambiguous {label} entity object")
        reference = inspection._entity_reference(base, deck, entity, expected_type="SHELL")
        entity_id = reference["id"]
        if entity_id not in allowed_ids:
            raise RuntimeError(f"{_API} returned SHELL {entity_id} outside the explicit scope")
        references_by_id[entity_id] = reference

    entity_ids = sorted(references_by_id)
    first_hits = [entity_id for entity_id in entity_ids if entity_id in first_ids]
    second_hits = [entity_id for entity_id in entity_ids if entity_id in second_ids]
    if entity_ids and (not first_hits or not second_hits):
        raise RuntimeError(f"{_API} returned {label} SHELLs from only one input group")
    return {
        "entity_ids": entity_ids,
        "count": len(entity_ids),
        "first_ids": first_hits,
        "second_ids": second_hits,
        "references": [references_by_id[entity_id] for entity_id in entity_ids],
    }, references_by_id


def _native_result(base, deck, result, first_ids, second_ids, thickness_mode, factor,
                   native_mode, property_context):
    if result is None:
        raise RuntimeError(f"{_API} failed and returned None")
    required = ("intersected", "penetrated", "penetrations")
    if type(result) is not dict or any(key not in result for key in required):
        raise RuntimeError(f"{_API} returned an incomplete result dictionary")

    native_count = result["penetrations"]
    if (type(native_count) is not int or native_count < 0
            or native_count > _MAX_NATIVE_COUNT):
        raise RuntimeError(f"{_API} returned an invalid penetration count")

    raw_intersected = result["intersected"]
    raw_penetrated = result["penetrated"]
    if type(raw_intersected) is not list or type(raw_penetrated) is not list:
        raise RuntimeError(f"{_API} returned an invalid entity list")
    if len(raw_intersected) > _MAX_NATIVE_ENTITIES:
        raise RuntimeError(
            f"{_API} returned more than {_MAX_NATIVE_ENTITIES} intersected entities"
        )
    if len(raw_penetrated) > _MAX_NATIVE_ENTITIES:
        raise RuntimeError(
            f"{_API} returned more than {_MAX_NATIVE_ENTITIES} penetrated entities"
        )

    allowed_ids = set(first_ids) | set(second_ids)
    first_id_set = set(first_ids)
    second_id_set = set(second_ids)
    intersected, intersected_references = _native_category(
        base, deck, raw_intersected, "intersected", allowed_ids, first_id_set, second_id_set
    )
    penetrated, penetrated_references = _native_category(
        base, deck, raw_penetrated, "penetrated", allowed_ids, first_id_set, second_id_set
    )
    affected_references = {**intersected_references, **penetrated_references}
    affected_ids = sorted(affected_references)
    if bool(native_count) != bool(affected_ids):
        raise RuntimeError(f"{_API} returned an inconsistent penetration count and entity lists")
    affected_first = [entity_id for entity_id in affected_ids if entity_id in first_id_set]
    affected_second = [entity_id for entity_id in affected_ids if entity_id in second_id_set]

    return {
        "check": "penetrations",
        "deck": deck,
        "read_only": True,
        "auto_fix": False,
        "scope": {"first_ids": first_ids, "second_ids": second_ids},
        "thickness_mode": thickness_mode,
        "native_options": {"mode": native_mode, "factor": factor, "fast_run": 0},
        "property_context": property_context,
        "native_count": native_count,
        "native_intersected_list_count": len(raw_intersected),
        "native_penetrated_list_count": len(raw_penetrated),
        "intersected": intersected,
        "penetrated": penetrated,
        "affected": {
            "entity_ids": affected_ids,
            "count": len(affected_ids),
            "first_ids": affected_first,
            "second_ids": affected_second,
            "references": [affected_references[entity_id] for entity_id in affected_ids],
        },
        "result_limits": {
            "native_intersected_list_items": _MAX_NATIVE_ENTITIES,
            "native_penetrated_list_items": _MAX_NATIVE_ENTITIES,
        },
        "note": (
            "native_count 保留 ANSA 原生 penetrations 计数，不解释为相交对数或壳对数；"
            "原生列表项计数包含重复项，分类后的 SHELL ID 已去重，"
            "intersected 与 penetrated 类别允许重叠。"
        ),
    }


def execute(state, tool, params):
    """Run one two-set penetration check on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "mesh_penetrations":
        raise ValueError(f"unsupported penetrations operation: {tool!r}")

    params = inspection._params(
        params,
        ("deck", "first_ids", "second_ids", "thickness_mode", "factor"),
    )
    deck_value = params["deck"]
    if type(deck_value) is not int or deck_value <= 0:
        raise ValueError("deck must be a positive integer")
    first_ids = inspection._id_list(
        params["first_ids"], "first_ids", maximum=_MAX_GROUP_IDS
    )
    second_ids = inspection._id_list(
        params["second_ids"], "second_ids", maximum=_MAX_GROUP_IDS
    )
    if set(first_ids) & set(second_ids):
        raise ValueError("first_ids and second_ids must not overlap")
    thickness_mode = params["thickness_mode"]
    if type(thickness_mode) is not str or thickness_mode not in {"property", "user"}:
        raise ValueError("thickness_mode must be exactly 'property' or 'user'")
    factor = _positive_finite_float(params["factor"], "factor")
    native_mode = 2 if thickness_mode == "property" else 3

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(
        base, "GetEntityCardValues", "ansa.base.GetEntityCardValues"
    )
    inspection._require_api(base, "CollectEntities", "ansa.base.CollectEntities")
    check_penetrations = inspection._require_api(base, "CheckPenetration2Sets", _API)

    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    deck = inspection._deck(base, params)
    if deck != nastran:
        raise ValueError("mesh_penetrations supports only the NASTRAN deck")

    first_shells, first_references = [], []
    second_shells, second_references = [], []
    for entity_id in first_ids:
        shell, reference = inspection._get_entity(base, deck, "SHELL", entity_id)
        if not inspection._is_entity(shell, base):
            raise RuntimeError("ANSA returned an ambiguous SHELL entity object")
        first_shells.append(shell)
        first_references.append(reference)
    for entity_id in second_ids:
        shell, reference = inspection._get_entity(base, deck, "SHELL", entity_id)
        if not inspection._is_entity(shell, base):
            raise RuntimeError("ANSA returned an ambiguous SHELL entity object")
        second_shells.append(shell)
        second_references.append(reference)

    property_cache = {}
    first_properties, first_property_ids = _property_context(
        base, deck, first_shells, first_references, property_cache
    )
    second_properties, second_property_ids = _property_context(
        base, deck, second_shells, second_references, property_cache
    )
    if first_property_ids & second_property_ids:
        raise ValueError(
            "first_ids and second_ids must not share any PSHELL property IDs: "
            "native thickness modes ignore same-property pairs"
        )

    mesh_checks._connection_context(
        base,
        deck,
        first_shells + second_shells,
        first_references + second_references,
        check_name="CheckPenetration2Sets",
    )
    result = check_penetrations(
        first_shells, second_shells, native_mode, factor, 0
    )
    return _native_result(
        base,
        deck,
        result,
        first_ids,
        second_ids,
        thickness_mode,
        factor,
        native_mode,
        {"first": first_properties, "second": second_properties},
    )


__all__ = ["execute"]
