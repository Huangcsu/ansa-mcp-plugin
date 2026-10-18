"""Read-only MeshQuality failure counts for an explicit NASTRAN SHELL scope."""

import hashlib
import json

from . import checks, inspection, mesh_checks


_API = "ansa.base.checks.mesh.MeshQuality"
_MISSING = object()


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
    if type(actual) is not type(value) or actual != value:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} did not retain its value"
        )


def _criteria_snapshot(check):
    parameters = getattr(check, "parameters", None)
    if not callable(parameters):
        raise inspection.CapabilityUnavailableError(f"capability unavailable: {_API}.parameters is missing")
    values = parameters()
    if (type(values) is not dict or values.get("use_criteria") != "Use global criteria"
            or type(values.get("quality_criteria")) is not str or not values["quality_criteria"]):
        raise inspection.CapabilityUnavailableError("MeshQuality did not confirm non-empty current global criteria")
    text = values["quality_criteria"]
    return {"use_criteria": values["use_criteria"], "quality_criteria": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def execute(state, tool, params):
    """Run MeshQuality silently on explicitly requested NASTRAN SHELL IDs."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "mesh_failed_elements":
        raise ValueError(f"unsupported failed-elements operation: {tool!r}")

    params = inspection._params(params, ("deck", "entity_ids"))
    deck_value = params["deck"]
    if type(deck_value) is not int or deck_value <= 0:
        raise ValueError("deck must be a positive integer")
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
        raise ValueError("mesh_failed_elements supports only the NASTRAN deck")

    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(
        base, "GetEntityCardValues", "ansa.base.GetEntityCardValues"
    )
    shells, shell_references = [], []
    for entity_id in entity_ids:
        shell, reference = inspection._get_entity(base, deck, "SHELL", entity_id)
        shells.append(shell)
        shell_references.append(reference)

    context_keys, context_node_ids, context = mesh_checks._connection_context(
        base, deck, shells, shell_references, check_name="MeshQuality"
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

    factory = inspection._factory(base, "mesh", "MeshQuality", _API)
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

    # This exact runtime attribute/value was verified on 26.10.000.
    # The bundled read_f11_values documentation does not match that build.
    options = {"use_criteria": "Use global criteria"}
    for name, value in options.items():
        _configure_option(check, name, value)
    criteria = _criteria_snapshot(check)

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
    current = factory()
    _configure_option(current, "use_criteria", "Use global criteria")
    if _criteria_snapshot(check) != criteria or _criteria_snapshot(current) != criteria:
        raise RuntimeError("MeshQuality changed its check or global quality criteria during execution")
    report_rows, issue_count, issue_counts, failed_entity_ids = checks._serialize_reports(
        base,
        deck,
        reports,
        set(entity_ids),
        context_keys,
        context_node_ids,
        primary_type="SHELL",
        check_name="MeshQuality",
    )
    result = {
        "deck": deck,
        "check": "failed_elements",
        "api": _API,
        "entity_type": "SHELL",
        "entities": shell_references,
        "options": options,
        "scope": "explicit",
        "read_only": True,
        "auto_fix": False,
        "criteria_source": "current_session",
        "criteria_snapshot": criteria,
        "criteria_note": (
            "使用当前 ANSA 会话中的 MeshQuality 准则。failed_element_count 为 0 "
            "仅表示本次显式范围内未报告失败单元，不表示全工程没有质量问题。"
            "准则全部关闭时也可能返回 0。"
        ),
        "connection_context": context,
        "native_report_count": len(reports),
        "report_count": len(report_rows),
        "issue_count": issue_count,
        "issue_counts": issue_counts,
        "failed_entity_ids": failed_entity_ids,
        "failed_element_count": len(failed_entity_ids),
        "reports": report_rows,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"failed-elements result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
