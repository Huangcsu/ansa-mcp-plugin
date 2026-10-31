"""Read-only, explicitly scoped NASTRAN shell or first-order solid checks."""

from . import inspection, mesh_checks


_API = "ansa.base.CheckPenetration2Sets"
_MAX_GROUP_IDS = 200
_MAX_NATIVE_ENTITIES = 400
_MAX_NATIVE_COUNT = (1 << 63) - 1
_MISSING = object()


def _native_result(base, deck, result, first_ids, second_ids, entity_type="SHELL"):
    if result is None:
        raise RuntimeError(f"{_API} failed and returned None")
    if (type(result) is not dict
            or any(key not in result for key in ("intersected", "penetrated", "penetrations"))):
        raise RuntimeError(f"{_API} returned an incomplete result dictionary")

    native_count = result["penetrations"]
    if (type(native_count) is not int or native_count < 0
            or native_count > _MAX_NATIVE_COUNT):
        raise RuntimeError(f"{_API} returned an invalid penetration count")

    raw_intersected = result["intersected"]
    penetrated = result["penetrated"]
    if type(raw_intersected) is not list or type(penetrated) is not list:
        raise RuntimeError(f"{_API} returned an invalid entity list")
    if penetrated:
        raise RuntimeError(f"{_API} returned penetrated entities in intersections mode")
    if len(raw_intersected) > _MAX_NATIVE_ENTITIES:
        raise RuntimeError(
            f"{_API} returned more than {_MAX_NATIVE_ENTITIES} intersected entities"
        )
    if bool(raw_intersected) != (native_count > 0):
        raise RuntimeError(f"{_API} returned inconsistent intersection entities and count")

    allowed_ids = set(first_ids) | set(second_ids)
    first_id_set = set(first_ids)
    second_id_set = set(second_ids)
    references_by_id = {}
    for entity in raw_intersected:
        if not inspection._is_entity(entity, base):
            raise RuntimeError(f"{_API} returned an ambiguous entity object")
        reference = inspection._entity_reference(base, deck, entity, expected_type=entity_type)
        entity_id = reference["id"]
        if entity_id not in allowed_ids:
            raise RuntimeError(f"{_API} returned {entity_type} {entity_id} outside the explicit scope")
        references_by_id[entity_id] = reference

    intersected_ids = sorted(references_by_id)
    first_hit_ids = [entity_id for entity_id in intersected_ids if entity_id in first_id_set]
    second_hit_ids = [entity_id for entity_id in intersected_ids if entity_id in second_id_set]
    if intersected_ids and (not first_hit_ids or not second_hit_ids):
        raise RuntimeError(f"{_API} did not return intersected {entity_type}s from both input groups")

    return {
        "check": "intersections",
        "deck": deck,
        "read_only": True,
        "auto_fix": False,
        "scope": {"first_ids": first_ids, "second_ids": second_ids},
        "native_options": {"mode": 1, "factor": 0.0, "fast_run": 0},
        "native_count": native_count,
        "native_intersected_list_count": len(raw_intersected),
        "intersected": {
            "entity_ids": intersected_ids,
            "count": len(intersected_ids),
            "first_ids": first_hit_ids,
            "second_ids": second_hit_ids,
            "references": [references_by_id[entity_id] for entity_id in intersected_ids],
        },
        "result_limits": {"native_intersected_list_items": _MAX_NATIVE_ENTITIES},
        "note": (
            "native_count 是 ANSA 原生 penetrations 计数，尚未核实为相交对数；"
            "native_intersected_list_count 统计原生列表项（包含重复项），"
            f"intersected.count 按 {entity_type} ID 去重；只检查两组之间，不检查组内。"
        ),
    }


def execute(state, tool, params):
    """Run one two-set intersection check on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "mesh_intersections":
        raise ValueError(f"unsupported intersections operation: {tool!r}")

    params = inspection._params(params, ("deck", "first_ids", "second_ids"), ("entity_type",))
    entity_type = params.get("entity_type", "SHELL")
    if type(entity_type) is not str or entity_type not in {"SHELL", "SOLID"}:
        raise ValueError("entity_type must be SHELL or SOLID")
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

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(
        base, "GetEntityCardValues", "ansa.base.GetEntityCardValues"
    )
    inspection._require_api(base, "CollectEntities", "ansa.base.CollectEntities")
    check_intersections = inspection._require_api(base, "CheckPenetration2Sets", _API)

    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    deck = inspection._deck(base, params)
    if deck != nastran:
        raise ValueError("mesh_intersections supports only the NASTRAN deck")

    first_shells, first_references = [], []
    second_shells, second_references = [], []
    for entity_id in first_ids:
        shell, reference = inspection._get_entity(base, deck, entity_type, entity_id)
        if not inspection._is_entity(shell, base):
            raise RuntimeError(f"ANSA returned an ambiguous {entity_type} entity object")
        first_shells.append(shell)
        first_references.append(reference)
    for entity_id in second_ids:
        shell, reference = inspection._get_entity(base, deck, entity_type, entity_id)
        if not inspection._is_entity(shell, base):
            raise RuntimeError(f"ANSA returned an ambiguous {entity_type} entity object")
        second_shells.append(shell)
        second_references.append(reference)

    if entity_type == "SHELL":
        mesh_checks._connection_context(
            base, deck, first_shells + second_shells,
            first_references + second_references, check_name="CheckPenetration2Sets",
        )
    else:
        # Reuse the bounded, strict CHEXA8/CPENTA6 card and basic-coordinate
        # GRID reader. Never reinterpret shell connectivity or PSHELL thickness.
        from .structured_native import capture_mesh
        snapshot = capture_mesh(base, constants, deck, first_ids + second_ids, collect_ownership=False)
        rows = {row["id"]: row for row in snapshot["elements"]}
        for solid in first_shells + second_shells:
            count = len(rows[solid._id]["nodes"])
            midside = [name for name in solid.card_fields(deck)
                       if name.startswith("G") and name[1:].isdigit() and int(name[1:]) > count]
            if midside:
                values = base.GetEntityCardValues(deck, solid, midside)
                if (type(values) is not dict or any(name not in values for name in midside)
                        or any(type(values[name]) is bool or values[name] not in (None, "", 0)
                               for name in midside)):
                    raise ValueError("mesh_intersections supports only first-order CHEXA8/CPENTA6")
    result = check_intersections(first_shells, second_shells, 1, 0.0, 0)
    value = _native_result(base, deck, result, first_ids, second_ids, entity_type)
    if entity_type == "SOLID":
        value["scope"]["entity_type"] = entity_type
        value["supported_element_types"] = ["CHEXA8", "CPENTA6"]
    return value


__all__ = ["execute"]
