"""NASTRAN GRID operations with strict card preconditions and readback."""

from __future__ import annotations

import math


_GRID_FIELDS = ("X1", "X2", "X3")
_GRID_READ_FIELDS = ("__id__", "__type__", *_GRID_FIELDS)


def _params(value, required):
    if type(value) is not dict:
        raise TypeError("params must be an object")
    keys = set(value)
    required = set(required)
    missing = required - keys
    extra = keys - required
    if missing:
        raise ValueError("missing parameter(s): " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(extra)))
    return value


def _deck(value, base, constants):
    deck = value.get("deck")
    if type(deck) is not int:
        raise TypeError("deck must be an integer")
    if deck != constants.NASTRAN:
        raise ValueError("only the NASTRAN deck is supported")
    if base.CurrentDeck() != deck:
        raise ValueError("requested deck is not the current ANSA deck")
    return deck


def _vector(value, name):
    if type(value) not in (list, tuple) or len(value) != 3:
        raise ValueError(f"{name} must contain exactly three coordinates")
    result = []
    for index, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise TypeError(f"{name}[{index}] must be a number")
        component = float(component)
        if not math.isfinite(component):
            raise ValueError(f"{name}[{index}] must be finite")
        result.append(component)
    return tuple(result)


def _entity_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("id must be a positive integer")
    return value


def _card_values(base, deck, entity, expected_id):
    values = base.GetEntityCardValues(deck, entity, _GRID_READ_FIELDS)
    if type(values) is not dict or any(field not in values for field in _GRID_READ_FIELDS):
        raise RuntimeError("ANSA did not return the GRID identity and coordinate card fields")
    entity_id = values["__id__"]
    if type(entity_id) is not int or entity_id <= 0 or entity_id != expected_id:
        raise RuntimeError("ANSA returned a different or invalid entity ID")
    if values["__type__"] != "GRID":
        raise RuntimeError("entity type changed; expected GRID")
    coordinates = _vector([values[field] for field in _GRID_FIELDS], "ANSA coordinates")
    return {"entity": {"id": entity_id, "type": "GRID", "deck": deck},
            "coordinates": list(coordinates)}


def _read_grid(base, deck, entity_id):
    entity = base.GetEntity(deck, "GRID", entity_id)
    if entity is None:
        raise LookupError(f"GRID {entity_id} does not exist")
    return _card_values(base, deck, entity, entity_id), entity


def _confirmed_grid(base, deck, entity_id, expected_coordinates=None):
    result, _entity = _read_grid(base, deck, entity_id)
    if expected_coordinates is not None:
        actual = tuple(result["coordinates"])
        if actual != expected_coordinates:
            raise RuntimeError(
                f"GRID {entity_id} readback differs from the requested coordinates: {actual!r}"
            )
    return result


def _grid_create(base, deck, params):
    position = _vector(params["position"], "position")
    created = base.CreateEntity(
        deck,
        "GRID",
        {field: value for field, value in zip(_GRID_FIELDS, position)},
    )
    if created is None:
        raise RuntimeError("ANSA failed to create GRID")
    entity_id = getattr(created, "_id", None)
    if type(entity_id) is not int or entity_id <= 0:
        raise RuntimeError("ANSA created GRID without returning a valid ID")
    # Resolve it again by deck/type/ID and read its card independently of CreateEntity.
    return _confirmed_grid(base, deck, entity_id, position)


def _grid_read(base, deck, params):
    entity_id = _entity_id(params["id"])
    result, _entity = _read_grid(base, deck, entity_id)
    return result


def _grid_update(base, deck, params):
    entity_id = _entity_id(params["id"])
    expected = _vector(params["expected_position"], "expected_position")
    position = _vector(params["position"], "position")
    current, entity = _read_grid(base, deck, entity_id)
    if tuple(current["coordinates"]) != expected:
        raise RuntimeError(f"GRID {entity_id} changed since it was read")

    status = None
    api_error = None
    try:
        status = base.SetEntityCardValues(
            deck,
            entity,
            {field: value for field, value in zip(_GRID_FIELDS, position)},
        )
    except Exception as exc:
        api_error = exc

    # Always resolve and read the entity again after the attempted write.
    result = _confirmed_grid(base, deck, entity_id, position)
    warnings = []
    if api_error is not None:
        warnings.append("ANSA raised during update; the requested coordinates were confirmed by readback")
    elif status != 0:
        warnings.append(f"SetEntityCardValues returned {status!r}; requested coordinates were confirmed by readback")
    if warnings:
        result["warnings"] = warnings
    return result


def _grid_delete(base, deck, params):
    entity_id = _entity_id(params["id"])
    expected = _vector(params["expected_position"], "expected_position")
    current, entity = _read_grid(base, deck, entity_id)
    if tuple(current["coordinates"]) != expected:
        raise RuntimeError(f"GRID {entity_id} changed since it was read")

    status = None
    api_error = None
    try:
        status = base.DeleteEntity(entity, force=False, compress=False)
    except Exception as exc:
        api_error = exc

    # Verify absence even when DeleteEntity reported an error or raised.
    remaining = base.GetEntity(deck, "GRID", entity_id)
    if remaining is not None:
        raise RuntimeError(f"GRID {entity_id} still exists after the delete attempt") from api_error
    result = {"entity": {"id": entity_id, "type": "GRID", "deck": deck},
              "coordinates": current["coordinates"], "deleted": True}
    if api_error is not None:
        result["warnings"] = ["ANSA raised during delete; absence was confirmed by readback"]
    elif status != 0:
        result["warnings"] = [f"DeleteEntity returned {status!r}; absence was confirmed by readback"]
    return result


def execute(base, constants, tool, params):
    """Execute one GRID operation; caller must have checked main thread and write access."""
    if tool == "grid_create":
        params = _params(params, ("deck", "position"))
        return _grid_create(base, _deck(params, base, constants), params)
    if tool == "grid_read":
        params = _params(params, ("deck", "id"))
        return _grid_read(base, _deck(params, base, constants), params)
    if tool == "grid_update":
        params = _params(params, ("deck", "id", "expected_position", "position"))
        return _grid_update(base, _deck(params, base, constants), params)
    if tool == "grid_delete":
        params = _params(params, ("deck", "id", "expected_position"))
        return _grid_delete(base, _deck(params, base, constants), params)
    raise ValueError(f"unsupported ANSA entity operation: {tool!r}")


__all__ = ["execute"]
