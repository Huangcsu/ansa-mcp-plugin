"""Guarded Part and direct SET membership operations."""

import json

from . import entity_catalog


_MAX_SET_MEMBERS = 200


def _expected_fields(value, label):
    if type(value) is not dict or not value:
        raise ValueError(f"{label} must be a non-empty object")
    return value


def _entity_key(reference):
    return reference["deck"], reference["type"], reference["id"]


def _entity_ref_input(base, deck, value, label):
    entity_catalog._object(value, ("deck", "type", "id"), label=label)
    if type(value["deck"]) is not int or value["deck"] != deck:
        raise ValueError(f"{label}.deck must match the current deck")
    entity_type = entity_catalog._entity_type(value["type"])
    entity_id = entity_catalog._positive_int(value["id"], f"{label}.id")
    _entity, reference = entity_catalog._get_entity(base, deck, entity_type, entity_id)
    return reference


def _expected_member_refs(base, deck, values):
    if type(values) is not list:
        raise TypeError("expected_members must be a list")
    if len(values) > _MAX_SET_MEMBERS:
        raise ValueError(f"expected_members may contain at most {_MAX_SET_MEMBERS} items")
    references = [
        _entity_ref_input(base, deck, value, f"expected_members[{index}]")
        for index, value in enumerate(values)
    ]
    keys = [_entity_key(reference) for reference in references]
    if len(set(keys)) != len(keys):
        raise ValueError("expected_members must not contain duplicate references")
    return references


def _read_direct_members(base, deck, set_entity, set_reference):
    collect = getattr(base, "CollectEntities", None)
    if not callable(collect):
        raise NotImplementedError(
            "direct SET membership cannot be verified: documented CollectEntities is unavailable"
        )
    members = collect(deck, set_entity, "__ALL_ENTITIES__", recursive=False,
                      no_expand_types=("SET",))
    if type(members) not in (list, tuple):
        raise RuntimeError("ANSA CollectEntities returned an invalid direct-members result")
    if len(members) > _MAX_SET_MEMBERS:
        raise ValueError(f"SET direct membership exceeds the {_MAX_SET_MEMBERS}-member limit")

    references = []
    for member in members:
        reference = entity_catalog._entity_reference(base, deck, member)
        if _entity_key(reference) == _entity_key(set_reference):
            raise RuntimeError("self-containing SET membership is not supported")
        _resolved, verified = entity_catalog._get_entity(
            base, deck, reference["type"], reference["id"]
        )
        references.append(verified)

    keys = [_entity_key(reference) for reference in references]
    if len(set(keys)) != len(keys):
        raise RuntimeError("ANSA returned duplicate direct SET members")
    return references


def _same_members(actual, expected):
    return {_entity_key(reference) for reference in actual} == {
        _entity_key(reference) for reference in expected
    }


def _part_create(base, deck, params):
    params = entity_catalog._object(params, ("name",), ("module_id", "deck"))
    name = params["name"]
    module_id = params.get("module_id", "")
    if type(name) is not str or not name.strip():
        raise ValueError("name must be a non-empty string")
    if type(module_id) is not str:
        raise TypeError("module_id must be a string")

    create = getattr(base, "NewPart", None)
    if not callable(create):
        raise NotImplementedError("ansa.base.NewPart is unavailable; no part was created")
    created = create(name, module_id)
    if created is None:
        raise RuntimeError("ANSA NewPart returned None; no part was created")

    try:
        initial_reference = entity_catalog._entity_reference(base, deck, created)
        entity, reference = entity_catalog._get_entity(
            base, deck, initial_reference["type"], initial_reference["id"]
        )
        actual = entity_catalog._read_card(base, deck, entity, reference, ("Name", "Module Id"))
        entity_catalog._matches_expected(actual, {"Name": name})
        if module_id:
            entity_catalog._matches_expected(actual, {"Module Id": module_id})
    except Exception as exc:
        raise RuntimeError(
            "ANSA created a part but its name, module ID and identity could not be independently confirmed; "
            "the part may remain in the model"
        ) from exc

    return {"entity": reference, "name": actual["Name"], "module_id": actual["Module Id"]}


def _set_members(base, deck, params):
    params = entity_catalog._object(params, ("set_id",), ("deck",))
    set_id = entity_catalog._positive_int(params["set_id"], "set_id")
    set_entity, set_reference = entity_catalog._get_entity(base, deck, "SET", set_id)
    members = _read_direct_members(base, deck, set_entity, set_reference)
    return {"deck": deck, "set": set_reference, "members": members,
            "count": len(members), "direct_membership": True}


def _set_add(base, deck, params):
    params = entity_catalog._object(
        params, ("set_id", "expected_fields", "expected_members", "entities"), ("deck",)
    )
    set_id = entity_catalog._positive_int(params["set_id"], "set_id")
    expected_fields = _expected_fields(params["expected_fields"], "expected_fields")
    expected_members = _expected_member_refs(base, deck, params["expected_members"])

    set_entity, set_reference, _expected, _labels = entity_catalog._validate_entity_values(
        base, deck, "SET", set_id, expected_fields
    )
    old_members = _read_direct_members(base, deck, set_entity, set_reference)
    if not _same_members(old_members, expected_members):
        raise RuntimeError("SET direct membership changed since expected_members was read")
    if any(reference["type"] == "SET" for reference in old_members):
        raise ValueError("nested SET members are outside ordinary set_add operations")

    values = params["entities"]
    if type(values) is not list or not values:
        raise ValueError("entities must be a non-empty list")
    if len(values) > _MAX_SET_MEMBERS:
        raise ValueError(f"entities may contain at most {_MAX_SET_MEMBERS} items")

    old_keys = {_entity_key(reference) for reference in old_members}
    target_key = _entity_key(set_reference)
    additions, addition_entities = [], []
    for index, value in enumerate(values):
        label = f"entities[{index}]"
        entity_catalog._object(value, ("type", "id", "expected_fields"), label=label)
        entity_type = entity_catalog._entity_type(value["type"])
        entity_id = entity_catalog._positive_int(value["id"], f"{label}.id")
        fields = _expected_fields(value["expected_fields"], f"{label}.expected_fields")
        if entity_type == "SET":
            raise ValueError("adding SET entities is outside ordinary set_add operations")
        entity, reference, _expected, _labels = entity_catalog._validate_entity_values(
            base, deck, entity_type, entity_id, fields
        )
        key = _entity_key(reference)
        if key == target_key:
            raise ValueError("a SET cannot be added to itself")
        if key in old_keys:
            raise ValueError("entities must not already be direct members of the SET")
        if any(_entity_key(prior) == key for prior in additions):
            raise ValueError("entities must not contain duplicate references")
        additions.append(reference)
        addition_entities.append(entity)

    expected_after = old_members + additions
    if len(expected_after) > _MAX_SET_MEMBERS:
        raise ValueError(f"resulting SET membership may contain at most {_MAX_SET_MEMBERS} items")

    add_to_set = getattr(base, "AddToSet", None)
    if not callable(add_to_set):
        raise NotImplementedError("ansa.base.AddToSet is unavailable; the SET was not changed")

    api_error = None
    status = None
    try:
        status = add_to_set(set_entity, addition_entities,
                            as_excluded=False, as_intersected=False)
    except Exception as exc:
        api_error = exc

    try:
        actual_after = _read_direct_members(base, deck, set_entity, set_reference)
    except Exception as exc:
        cause = api_error or exc
        raise RuntimeError(
            "AddToSet was attempted but direct membership could not be read back; "
            "the model may have changed"
        ) from cause

    if api_error is not None:
        if not _same_members(actual_after, old_members):
            raise RuntimeError("AddToSet raised after changing SET membership; inspect the model") from api_error
        raise RuntimeError("AddToSet raised and direct membership was verified unchanged") from api_error
    if type(status) is not int or status != 1:
        if not _same_members(actual_after, old_members):
            raise RuntimeError("AddToSet reported failure after changing SET membership; inspect the model")
        raise RuntimeError(f"AddToSet returned {status!r}; direct membership was verified unchanged")
    if not _same_members(actual_after, expected_after):
        raise RuntimeError(
            "post-write SET membership differs from old members plus requested additions; "
            "unexpected model changes may remain"
        )

    return {"deck": deck, "set": set_reference, "added": additions,
            "members": actual_after, "count": len(actual_after),
            "direct_membership": True, "membership_verified": True}


def execute(state, tool, params):
    """Run one bounded Part or SET operation on ANSA's main thread."""
    state.assert_main()
    from ansa import base

    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if type(params) is not dict:
        raise TypeError("params must be an object")
    deck = entity_catalog._deck(base, params)

    if tool == "part_create":
        result = _part_create(base, deck, params)
    elif tool == "set_members":
        result = _set_members(base, deck, params)
    elif tool == "set_add":
        result = _set_add(base, deck, params)
    else:
        raise ValueError(f"unsupported ANSA structure operation: {tool!r}")

    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"operation result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
