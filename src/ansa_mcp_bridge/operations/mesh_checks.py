"""Controlled, read-only mesh checks on an explicit SHELL selection."""

import json
import math

from . import checks, inspection


_API = "ansa.base.checks.mesh.SharpEdges"
_MAX_CONTEXT_ENTITIES = 10000
_MISSING = object()


def _angle(value):
    if type(value) not in (int, float):
        raise ValueError("angle must be a finite number strictly between 0 and 180")
    try:
        valid = math.isfinite(value) and 0 < value < 180
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("angle must be a finite number strictly between 0 and 180")
    return value


def _configure_option(check, name, value):
    try:
        getattr(check, name)
    except AttributeError as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} is missing"
        ) from exc
    try:
        setattr(check, name, value)
        actual = getattr(check, name)
    except (AttributeError, TypeError, ValueError) as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} cannot be configured"
        ) from exc

    if name == "angle":
        try:
            retained = (type(actual) in (int, float) and math.isfinite(actual)
                        and actual == value)
        except OverflowError:
            retained = False
    else:
        retained = type(actual) is int and actual == value
    if not retained:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} did not retain its value"
        )


def _shell_connectivity(base, deck, shell):
    card_fields = getattr(shell, "card_fields", None)
    if not callable(card_fields):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: SHELL.card_fields is required to verify connectivity"
        )
    try:
        labels = card_fields(deck)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: SHELL.card_fields could not verify connectivity"
        ) from exc
    if (type(labels) not in (list, tuple)
            or any(type(label) is not str or not label for label in labels)
            or len(set(labels)) != len(labels)):
        raise RuntimeError("SHELL.card_fields returned an invalid field-name list")
    if "type" not in labels:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: NASTRAN SHELL type card field is missing"
        )

    type_values = base.GetEntityCardValues(deck, shell, ("type",))
    if type(type_values) is not dict or type_values.get("type") not in ("CQUAD4", "CTRIA3"):
        shell_type = type_values.get("type") if type(type_values) is dict else None
        raise inspection.CapabilityUnavailableError(
            f"unsupported NASTRAN SHELL subtype {shell_type!r}; only CQUAD4 and CTRIA3 are supported"
        )
    shell_type = type_values["type"]
    node_fields = (
        ("G1", "G2", "G3", "G4") if shell_type == "CQUAD4"
        else ("G1", "G2", "G3")
    )
    if any(field not in labels for field in node_fields):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {shell_type} is missing its required connectivity fields"
        )

    node_values = base.GetEntityCardValues(deck, shell, node_fields)
    if (type(node_values) is not dict
            or any(field not in node_values for field in node_fields)):
        raise RuntimeError(f"ANSA did not return complete {shell_type} connectivity")
    node_ids = [node_values[field] for field in node_fields]
    if any(type(node_id) is not int or node_id <= 0 for node_id in node_ids):
        raise RuntimeError(f"ANSA returned an invalid {shell_type} GRID ID")
    if len(set(node_ids)) != len(node_ids):
        raise RuntimeError(f"ANSA returned duplicate GRID IDs on a {shell_type}")
    return node_ids


def _connection_context(base, deck, shells, shell_references, check_name="SharpEdges"):
    connected_node_ids = set()
    for shell in shells:
        connected_node_ids.update(_shell_connectivity(base, deck, shell))

    collect_entities = inspection._require_api(
        base, "CollectEntities", "ansa.base.CollectEntities"
    )
    grids = collect_entities(deck, shells, "GRID")
    if type(grids) not in (list, tuple):
        raise RuntimeError("CollectEntities must return a complete GRID list or tuple")
    if len(shells) + len(grids) > _MAX_CONTEXT_ENTITIES:
        raise RuntimeError(f"{check_name} connection context exceeds 10000 entities")

    grid_references = []
    grid_ids = set()
    context_keys = {
        (reference["type"], reference["id"]) for reference in shell_references
    }
    for grid in grids:
        reference = inspection._entity_reference(base, deck, grid, "GRID")
        key = (reference["type"], reference["id"])
        if key in context_keys or reference["id"] in grid_ids:
            raise RuntimeError(f"{check_name} connection context repeats an entity")
        context_keys.add(key)
        grid_ids.add(reference["id"])
        grid_references.append(reference)

    if grid_ids != connected_node_ids:
        message = "CollectEntities GRID scope does not exactly match verified SHELL connectivity"
        if check_name != "SharpEdges":
            message = (
                "CollectEntities GRID scope does not exactly match verified "
                f"{check_name} SHELL connectivity"
            )
        raise RuntimeError(message)

    grid_references.sort(key=lambda reference: reference["id"])
    ordered_grid_ids = sorted(grid_ids)
    return context_keys, grid_ids, {
        "api": "ansa.base.CollectEntities",
        "entities": grid_references,
        "node_ids": ordered_grid_ids,
    }


def execute(state, tool, params):
    """Run a silent SharpEdges check on explicit, verified NASTRAN SHELL IDs."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "mesh_check":
        raise ValueError(f"unsupported ANSA mesh check operation: {tool!r}")

    params = inspection._params(params, ("check", "deck", "entity_ids"), ("angle",))
    if type(params["check"]) is not str or params["check"] != "sharp_edges":
        raise ValueError("check must be the exact string 'sharp_edges'")
    deck_value = params["deck"]
    if type(deck_value) is not int or deck_value <= 0:
        raise ValueError("deck must be a positive integer")
    angle = _angle(params.get("angle", 120.0))
    entity_ids = inspection._id_list(params["entity_ids"], "entity_ids", maximum=200)

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    deck = inspection._deck(base, params)
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    if deck != nastran:
        raise ValueError("mesh_check supports only the NASTRAN deck")

    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(
        base, "GetEntityCardValues", "ansa.base.GetEntityCardValues"
    )
    shells, shell_references = [], []
    for entity_id in entity_ids:
        shell, reference = inspection._get_entity(base, deck, "SHELL", entity_id)
        shells.append(shell)
        shell_references.append(reference)

    context_keys, context_node_ids, context = _connection_context(
        base, deck, shells, shell_references
    )

    check_class = getattr(base, "Check", None)
    if not isinstance(check_class, type):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.base.Check is missing"
        )
    try:
        exec_selected = check_class.EXEC_ON_SELECTED
        report_none = check_class.REPORT_NONE
        keep_old = check_class.KEEP_OLD
    except AttributeError as exc:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: selected, silent, history-preserving ANSA checks are missing"
        ) from exc

    factory = inspection._factory(base, "mesh", "SharpEdges", _API)
    check = factory()
    if not isinstance(check, check_class):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} did not return an ansa.base.Check"
        )
    availability = getattr(check, "is_available_in_deck", None)
    if not callable(availability):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.is_available_in_deck is missing"
        )
    if availability(deck) is not True:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} does not confirm support for the requested deck"
        )

    options = {"angle": angle, "sharp_edges_type": 0, "exclude_edges": 0}
    for name, value in options.items():
        _configure_option(check, name, value)

    execute_check = getattr(check, "execute", None)
    if not callable(execute_check):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} Check.execute is missing"
        )
    reports = execute_check(
        exec_mode=exec_selected,
        report=report_none,
        history=keep_old,
        entities=shells,
    )
    report_rows, issue_count, issue_counts, failed_entity_ids = checks._serialize_reports(
        base,
        deck,
        reports,
        set(entity_ids),
        context_keys,
        context_node_ids,
        primary_type="SHELL",
        check_name="SharpEdges",
    )
    result = {
        "deck": deck,
        "check": "sharp_edges",
        "api": _API,
        "entity_type": "SHELL",
        "entities": shell_references,
        "options": options,
        "scope": "explicit",
        "read_only": True,
        "auto_fix": False,
        "connection_context": context,
        "native_report_count": len(reports),
        "report_count": len(report_rows),
        "issue_count": issue_count,
        "issue_counts": issue_counts,
        "failed_entity_ids": failed_entity_ids,
        "reports": report_rows,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"mesh check result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
