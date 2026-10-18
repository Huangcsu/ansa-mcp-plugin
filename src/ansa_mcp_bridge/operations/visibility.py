"""Controlled NASTRAN visibility operations for FACE and SHELL entities."""

import json
import math

from .inspection import (
    CapabilityUnavailableError,
    _entity_reference,
    _get_entity,
    _id_list,
    _params,
    _require_api,
)


_MONITORED_TYPES = ("FACE", "SHELL")
_MAX_SNAPSHOT_ENTITIES = 10000
_MAX_SELECTED_ENTITIES = 200
_VISIBILITY_ACTIONS = {"show_only", "show_also", "hide"}


def _check_nastran_deck(deck, requested_deck):
    if type(deck) is not int or type(requested_deck) is not int:
        raise ValueError("deck must be an integer")
    if deck != requested_deck:
        raise ValueError("deck does not match the requested deck")

    from ansa import constants

    nastran = getattr(constants, "NASTRAN", None)
    if nastran is None or deck != nastran:
        raise CapabilityUnavailableError(
            "capability unavailable: visibility is supported only for the NASTRAN deck"
        )


def _entity_ids(base, deck, entity_type, entities, label):
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"{label} did not return a list or tuple")
    ids = []
    for entity in entities:
        reference = _entity_reference(base, deck, entity, entity_type)
        ids.append(reference["id"])
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"{label} contains duplicate {entity_type} IDs")
    return sorted(ids)


def _snapshot_type(base, deck, entity_type, remaining):
    collect = _require_api(base, "CollectEntities", "ansa.base.CollectEntities")
    all_entities = collect(deck, None, entity_type)
    if type(all_entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({entity_type}) did not return a list or tuple")
    if len(all_entities) > remaining:
        raise RuntimeError(
            f"FACE/SHELL snapshot exceeds {_MAX_SNAPSHOT_ENTITIES} total entities"
        )

    visible_entities = collect(deck, None, entity_type, filter_visible=True)
    if type(visible_entities) not in (list, tuple):
        raise RuntimeError(
            f"CollectEntities({entity_type}, filter_visible=True) did not return a list or tuple"
        )
    if len(visible_entities) > len(all_entities):
        raise RuntimeError(f"visible {entity_type} collection exceeds the full collection")

    ids = _entity_ids(base, deck, entity_type, all_entities,
                      f"CollectEntities({entity_type})")
    visible_ids = _entity_ids(base, deck, entity_type, visible_entities,
                              f"CollectEntities({entity_type}, filter_visible=True)")
    if not set(visible_ids).issubset(ids):
        missing = sorted(set(visible_ids) - set(ids))
        raise RuntimeError(
            f"visible {entity_type} IDs are missing from the full collection: {missing}"
        )
    return {"ids": ids, "visible_ids": visible_ids}


def _read_snapshot(base, deck):
    data = {"ids": {}, "visible_ids": {}}
    errors = {}
    total = 0
    for entity_type in _MONITORED_TYPES:
        try:
            item = _snapshot_type(
                base, deck, entity_type, _MAX_SNAPSHOT_ENTITIES - total
            )
        except Exception as exc:
            errors[entity_type] = f"{type(exc).__name__}: {exc}"
            continue
        data["ids"][entity_type] = item["ids"]
        data["visible_ids"][entity_type] = item["visible_ids"]
        total += len(item["ids"])
    return data, errors


def _complete_snapshot(base, deck):
    data, errors = _read_snapshot(base, deck)
    if errors:
        details = "; ".join(f"{kind}: {error}" for kind, error in errors.items())
        if any("CapabilityUnavailableError" in error for error in errors.values()):
            raise CapabilityUnavailableError(details)
        raise RuntimeError(f"FACE/SHELL snapshot failed: {details}")
    return data


def _snapshot_payload(deck, data, *, partial=False, errors=None):
    observed = [kind for kind in _MONITORED_TYPES if kind in data["ids"]]
    unconfirmed = [kind for kind in _MONITORED_TYPES if kind not in observed]
    result = {
        "deck": deck,
        "monitored_types": list(_MONITORED_TYPES),
        "ids": {kind: data["ids"][kind] for kind in observed},
        "visible_ids": {kind: data["visible_ids"][kind] for kind in observed},
    }
    if partial:
        result["observed_types"] = observed
        result["unconfirmed_types"] = unconfirmed
        if errors:
            result["readback_errors"] = {
                kind: errors[kind] for kind in _MONITORED_TYPES if kind in errors
            }
    return result


def snapshot_base(base, deck, params):
    """Return a complete read-only visibility snapshot for FACE and SHELL."""
    params = _params(params, ("deck",))
    _check_nastran_deck(deck, params["deck"])
    data = _complete_snapshot(base, deck)
    result = _snapshot_payload(deck, data)
    result["read_only"] = True
    return result


def _expected_visible(value):
    if type(value) is not dict or set(value) != set(_MONITORED_TYPES):
        raise ValueError("expected_visible must contain exactly FACE and SHELL")
    result = {}
    total = 0
    for entity_type in _MONITORED_TYPES:
        ids = _id_list(value[entity_type], f"expected_visible.{entity_type}",
                       _MAX_SNAPSHOT_ENTITIES, allow_empty=True)
        total += len(ids)
        if total > _MAX_SNAPSHOT_ENTITIES:
            raise ValueError(
                f"expected_visible may contain at most {_MAX_SNAPSHOT_ENTITIES} IDs total"
            )
        result[entity_type] = set(ids)
    return result


def _validate_action_params(params):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    action = params.get("action")
    if type(action) is not str:
        raise ValueError("action must be a string")

    if action in _VISIBILITY_ACTIONS:
        params = _params(
            params,
            ("deck", "action", "entity_type", "entity_ids", "expected_visible"),
            ("apply_on_contents",),
        )
        entity_type = params["entity_type"]
        if type(entity_type) is not str or entity_type not in _MONITORED_TYPES:
            raise ValueError("entity_type must be FACE or SHELL")
        ids = _id_list(params["entity_ids"], "entity_ids", _MAX_SELECTED_ENTITIES)
        apply_on_contents = params.get("apply_on_contents", True)
        if type(apply_on_contents) is not bool:
            raise ValueError("apply_on_contents must be a boolean")
        return params, action, entity_type, ids, _expected_visible(params["expected_visible"]), apply_on_contents

    if action == "near":
        params = _params(
            params, ("deck", "action", "radius", "expected_visible"), ("dense_search",)
        )
        radius = params["radius"]
        if type(radius) not in (int, float):
            raise ValueError("radius must be a finite positive number")
        try:
            finite_radius = math.isfinite(float(radius))
        except (OverflowError, ValueError):
            finite_radius = False
        if not finite_radius or radius <= 0:
            raise ValueError("radius must be a finite positive number")
        dense_search = params.get("dense_search", False)
        if type(dense_search) is not bool:
            raise ValueError("dense_search must be a boolean")
        return params, action, None, None, _expected_visible(params["expected_visible"]), (radius, dense_search)

    if action == "neighb":
        params = _params(params, ("deck", "action", "steps", "expected_visible"))
        steps = params["steps"]
        if type(steps) is not int or not 1 <= steps <= 20:
            raise ValueError("steps must be an integer from 1 to 20")
        return params, action, None, None, _expected_visible(params["expected_visible"]), steps

    raise ValueError("action must be one of: show_only, show_also, hide, near, neighb")


def _display_diff(before, after):
    added = {}
    hidden = {}
    for entity_type in _MONITORED_TYPES:
        if entity_type not in after["visible_ids"]:
            added[entity_type] = None
            hidden[entity_type] = None
            continue
        old = set(before["visible_ids"][entity_type])
        new = set(after["visible_ids"][entity_type])
        added[entity_type] = sorted(new - old)
        hidden[entity_type] = sorted(old - new)
    return added, hidden


def _safe_return_code(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    try:
        return repr(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _effect_errors(action, entity_type, ids, before, after):
    errors = []
    before_ids = {kind: set(before["ids"][kind]) for kind in _MONITORED_TYPES}
    after_ids = {kind: set(after["ids"][kind]) for kind in after["ids"]}
    for kind in _MONITORED_TYPES:
        if kind in after_ids and before_ids[kind] != after_ids[kind]:
            errors.append(f"{kind} identities changed during the visibility operation")

    if action in _VISIBILITY_ACTIONS and entity_type in after["visible_ids"]:
        old = set(before["visible_ids"][entity_type])
        new = set(after["visible_ids"][entity_type])
        requested = set(ids)
        if action == "show_only" and new != requested:
            errors.append("show_only readback did not exactly match entity_ids")
        elif action == "show_also" and new != old | requested:
            errors.append("show_also did not exactly retain prior visibility and add entity_ids")
        elif action == "hide" and new != old - requested:
            errors.append("hide readback did not preserve the other visible entities")
    elif action in {"near", "neighb"}:
        for kind in _MONITORED_TYPES:
            if kind in after["visible_ids"]:
                old = set(before["visible_ids"][kind])
                new = set(after["visible_ids"][kind])
                if not old.issubset(new):
                    errors.append(f"{action} did not preserve the prior visible {kind} IDs")
    return errors


def execute_base(base, deck, params):
    """Execute and independently read back a controlled visibility operation."""
    params, action, entity_type, ids, expected, option = _validate_action_params(params)
    _check_nastran_deck(deck, params["deck"])

    if action == "show_only":
        api_name, expected_code = "Or", 1
    elif action == "show_also":
        api_name, expected_code = "And", 1
    elif action == "hide":
        api_name, expected_code = "Not", 1
    elif action == "near":
        api_name, expected_code = "Near", 0
    else:
        api_name, expected_code = "Neighb", 0
    api = _require_api(base, api_name, f"ansa.base.{api_name}")

    entities = []
    if action in _VISIBILITY_ACTIONS:
        for entity_id in ids:
            entity, _ = _get_entity(base, deck, entity_type, entity_id)
            entities.append(entity)

    before_data = _complete_snapshot(base, deck)
    before = _snapshot_payload(deck, before_data)
    for kind in _MONITORED_TYPES:
        if expected[kind] != set(before["visible_ids"][kind]):
            raise ValueError(f"expected_visible is stale for {kind}")
    if action in _VISIBILITY_ACTIONS:
        if not set(ids).issubset(before_data["ids"][entity_type]):
            raise LookupError(f"one or more requested {entity_type} IDs are absent from the snapshot")
    elif not any(before["visible_ids"][kind] for kind in _MONITORED_TYPES):
        raise ValueError("near/neighb requires at least one currently visible FACE or SHELL")

    api_exception = None
    return_code = None
    try:
        if action in _VISIBILITY_ACTIONS:
            apply_on_contents = option
            return_code = api(entities, apply_on_contents=apply_on_contents)
        elif action == "near":
            radius, dense_search = option
            return_code = api(radius, dense_search=dense_search)
        else:
            return_code = api(str(option))
    except Exception as exc:
        api_exception = f"{type(exc).__name__}: {exc}"

    after_data, readback_errors = _read_snapshot(base, deck)
    after = _snapshot_payload(deck, after_data, partial=True, errors=readback_errors)
    readback_complete = not readback_errors
    added, hidden = _display_diff(before, after)

    errors = []
    if api_exception is not None:
        errors.append(f"{api_name} raised {api_exception}")
    elif type(return_code) is not int or return_code != expected_code:
        errors.append(f"{api_name} returned unexpected status {return_code!r}; expected {expected_code}")
    for kind in _MONITORED_TYPES:
        if kind in readback_errors:
            errors.append(f"{kind} readback failed: {readback_errors[kind]}")
    if readback_complete:
        errors.extend(_effect_errors(action, entity_type, ids, before_data, after_data))

    result = {
        "deck": deck,
        "action": action,
        "scope": "仅监测 FACE/SHELL；相关实体类型可能联动",
        "api": f"ansa.base.{api_name}",
        "return_code": _safe_return_code(return_code),
        "before": before,
        "after": after,
        "added_visible_ids": added,
        "hidden_ids": hidden,
        "applied": not errors,
        "readback_complete": readback_complete,
        "possible_effects": bool(errors),
        "unconfirmed_types": after["unconfirmed_types"],
        "ok": not errors,
    }
    if action in _VISIBILITY_ACTIONS:
        result["apply_on_contents"] = option
    if errors:
        result["error"] = "; ".join(errors)
    if action in {"near", "neighb"} and not any(
        added[kind] for kind in _MONITORED_TYPES if added[kind] is not None
    ):
        result["message"] = "没有观察到新增的可见 FACE/SHELL ID"
    return result


def execute(state, tool, params):
    """Run one visibility operation on ANSA's owning main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    from ansa import base

    if type(params) is not dict:
        raise TypeError("params must be an object")
    deck = params.get("deck")
    if tool == "visibility_snapshot":
        result = snapshot_base(base, deck, params)
    elif tool == "entity_visibility":
        result = execute_base(base, deck, params)
    else:
        raise ValueError(f"unsupported visibility operation: {tool!r}")
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"visibility result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute", "execute_base", "snapshot_base"]
