"""Bounded, read-only NASTRAN duplicate-connection checks."""

import json
import math

from . import connections, entity_catalog, inspection


_API = "ansa.base.checks.general.Connections"
_POINT_TYPE = "SpotweldPoint_Type"
_MAX_POINTS = 200
_MAX_COLLECTED_POINTS = connections._MAX_CONNECTIONS
_MAX_REPORT_DEPTH = 8
_MAX_REPORT_NODES = 1000
_MAX_REPORT_REFERENCES = 1000
_MISSING = object()

_CHECK_FLAGS = (
    "check_connections_outside_flange",
    "check_double_connections",
    "check_fe_rep_height",
    "check_fe_rep_positions",
    "check_fe_rep_quad_angle",
    "check_fe_rep_quality",
    "check_fe_rep_step_length",
    "check_fe_rep_weld_thickness",
    "check_fe_rep_width",
    "check_flanges_angle",
    "check_haz_positions",
    "check_haz_quad_angle",
    "check_haz_width",
    "check_intersecting_parts",
    "check_max_distance",
    "check_min_distance",
    "check_num_of_parts",
    "check_projections_proximity",
    "check_solid_flanges",
    "check_spider_positioning",
    "check_sufficient_nodes_near_connection",
    "check_symmetric_connections",
    "check_total_thickness",
)
_CHECK_SWITCHES = (*_CHECK_FLAGS, "checkPartialConnLineRealization")
_CONFIGURATION_NAMES = (
    *_CHECK_SWITCHES,
    "double_connections_method",
    "equal_types",
    "spot_distance",
)


def _positive_ids(value):
    if type(value) is not list or not 1 <= len(value) <= _MAX_POINTS:
        raise ValueError(f"point_ids must be a list of 1 to {_MAX_POINTS} positive IDs")
    if any(type(item) is not int or item <= 0 for item in value):
        raise ValueError("point_ids must contain only positive integers")
    if len(set(value)) != len(value):
        raise ValueError("point_ids must not contain duplicate IDs")
    return value


def _positive_finite(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a positive finite number")
    try:
        number = float(value)
        valid = math.isfinite(number) and number > 0
    except OverflowError:
        valid = False
        number = 0.0
    if not valid:
        raise ValueError(f"{label} must be a positive finite number")
    return number


def _switch_value(value, label):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    raise inspection.CapabilityUnavailableError(
        f"capability unavailable: {_API} option {label} is not a boolean or 0/1"
    )


def _parameter_values(check, phase):
    parameters = getattr(check, "parameters", None)
    if not callable(parameters):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.parameters is missing"
        )
    try:
        raw = parameters()
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.parameters failed during {phase}"
        ) from exc
    if type(raw) is not dict:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.parameters must return a dictionary"
        )

    normalized = {}
    for raw_name, value in raw.items():
        if type(raw_name) is not str or not raw_name.strip():
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API}.parameters contains an invalid option name"
            )
        name = raw_name.strip()
        if name in normalized:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API}.parameters repeats option {name} after trimming"
            )
        if name.startswith("check_") and name not in _CHECK_FLAGS:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} has an unverified check option {name}"
            )
        normalized[name] = value

    missing = sorted(set(_CONFIGURATION_NAMES) - normalized.keys())
    if missing:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.parameters is missing options: "
            + ", ".join(missing)
        )
    return normalized


def _option_attribute(check, name):
    try:
        return getattr(check, name)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} is not readable"
        ) from exc


def _set_option(check, name, value):
    _option_attribute(check, name)
    try:
        setattr(check, name, value)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {name} cannot be configured"
        ) from exc


def _method_value(value, label):
    if type(value) is not str or not value.strip():
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {label} is not a readable string"
        )
    return value.strip()


def _distance_value(value, label):
    try:
        return _positive_finite(value, label)
    except ValueError as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option {label} is invalid"
        ) from exc


def _configure_check(check, distance):
    before = _parameter_values(check, "initial read")
    for name in _CHECK_SWITCHES:
        _switch_value(before[name], name)
        _switch_value(_option_attribute(check, name), name)
    _switch_value(before["equal_types"], "equal_types")
    _switch_value(_option_attribute(check, "equal_types"), "equal_types")
    _method_value(before["double_connections_method"], "double_connections_method")
    _method_value(_option_attribute(check, "double_connections_method"),
                  "double_connections_method")
    _distance_value(before["spot_distance"], "spot_distance")
    _distance_value(_option_attribute(check, "spot_distance"), "spot_distance")

    expected_switches = {name: name == "check_double_connections"
                         for name in _CHECK_SWITCHES}
    for name, value in expected_switches.items():
        if name != "check_double_connections":
            _set_option(check, name, value)
    _set_option(check, "equal_types", True)
    _set_option(check, "double_connections_method", "By absolute distance")
    _set_option(check, "spot_distance", distance)
    _set_option(check, "check_double_connections", True)

    attribute_values = {}
    for name, expected in expected_switches.items():
        actual = _switch_value(_option_attribute(check, name), name)
        if actual is not expected:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} did not retain its value"
            )
        attribute_values[name] = actual

    equal_types = _switch_value(_option_attribute(check, "equal_types"), "equal_types")
    if not equal_types:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option equal_types did not retain its value"
        )
    method_attribute = _option_attribute(check, "double_connections_method")
    if _method_value(method_attribute, "double_connections_method") != "By absolute distance":
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option double_connections_method did not retain its value"
        )
    distance_attribute = _distance_value(_option_attribute(check, "spot_distance"), "spot_distance")
    if not math.isclose(distance_attribute, distance, rel_tol=1e-9, abs_tol=0.0):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} option spot_distance did not retain its value"
        )

    configured = dict(attribute_values)
    configured.update(
        equal_types=True,
        double_connections_method=_method_value(method_attribute, "double_connections_method"),
        spot_distance=distance_attribute,
    )
    # ANSA 2026.1.0 can return the initial metadata from parameters() after
    # documented attribute assignments. Keep that discrepancy visible; verify
    # the actual assigned attributes and the resulting native check behavior.
    after = _parameter_values(check, "metadata readback")
    parameter_options = {name: _switch_value(after[name], name)
                         for name in (*_CHECK_SWITCHES, "equal_types")}
    parameter_options["double_connections_method"] = _method_value(
        after["double_connections_method"], "double_connections_method")
    parameter_options["spot_distance"] = _distance_value(after["spot_distance"], "spot_distance")
    mismatches = [name for name, value in configured.items()
                  if parameter_options[name] != value]
    return configured, {"source": "check_attributes", "parameters_match": not mismatches,
                        "parameter_mismatches": mismatches}


def _point_parts(row, deck, point_id):
    raw = row.get("connectivity")
    if type(raw) is not list:
        raise RuntimeError(f"SpotweldPoint {point_id} has an invalid connectivity readback")
    by_field = {}
    for entry in raw:
        if type(entry) is not dict or type(entry.get("field")) is not str:
            raise RuntimeError(f"SpotweldPoint {point_id} has an invalid Part connection row")
        field = entry["field"]
        if field in by_field:
            raise RuntimeError(f"SpotweldPoint {point_id} repeats connectivity field {field}")
        by_field[field] = entry
    if set(by_field) != {"P1", "P2"}:
        raise RuntimeError(f"SpotweldPoint {point_id} must belong to exactly two Parts")

    parts = []
    for field in ("P1", "P2"):
        entities = by_field[field].get("entities")
        if type(entities) is not list or len(entities) != 1:
            raise RuntimeError(f"SpotweldPoint {point_id} {field} must resolve to exactly one Part")
        reference = entities[0]
        if (type(reference) is not dict or reference.get("deck") != deck
                or reference.get("type") != "ANSAPART"
                or type(reference.get("id")) is not int or reference["id"] <= 0):
            raise RuntimeError(f"SpotweldPoint {point_id} {field} is not a valid NASTRAN Part")
        parts.append(reference)
    if parts[0]["id"] == parts[1]["id"]:
        raise RuntimeError(f"SpotweldPoint {point_id} must belong to two different Parts")
    return parts


def _preflight_points(base, connection_api, deck, point_ids):
    collect = inspection._require_api(
        base, "CollectEntities", "ansa.base.CollectEntities"
    )
    fresh = collect(deck, None, _POINT_TYPE, False)
    if type(fresh) not in (list, tuple):
        raise RuntimeError("fresh bounded SpotweldPoint collection must be a list or tuple")
    if len(fresh) > _MAX_COLLECTED_POINTS:
        raise RuntimeError(
            f"fresh SpotweldPoint collection exceeds {_MAX_COLLECTED_POINTS} entities"
        )

    by_id = {}
    for entity in fresh:
        reference = entity_catalog._entity_reference(base, deck, entity, _POINT_TYPE)
        entity_id = reference["id"]
        if entity_id in by_id:
            raise RuntimeError("fresh SpotweldPoint collection contains a duplicate identity")
        by_id[entity_id] = (entity, reference)

    missing = [entity_id for entity_id in point_ids if entity_id not in by_id]
    if missing:
        raise LookupError("selected SpotweldPoint IDs do not exist: "
                          + ", ".join(map(str, missing)))

    selected_entities, references, selected_rows = [], [], []
    for entity_id in point_ids:
        _entity, reference = by_id[entity_id]
        row = connections._connection_row(base, connection_api, deck, reference)
        if type(row) is not dict or row.get("complete") is not True:
            errors = row.get("errors") if type(row) is dict else None
            raise RuntimeError(
                f"SpotweldPoint {entity_id} is not completely readable: {errors!r}"
            )
        if row.get("entity") != reference or row.get("kind") != "point":
            raise RuntimeError(f"SpotweldPoint {entity_id} identity changed during preflight")
        diameter = row.get("diameter")
        if type(diameter) not in (int, float) or type(diameter) is bool \
                or not math.isfinite(float(diameter)) or diameter <= 0:
            raise RuntimeError(f"SpotweldPoint {entity_id} must have a positive finite D")
        position = row.get("position")
        if (type(position) is not list or len(position) != 3
                or any(type(value) not in (int, float) or type(value) is bool
                       or not math.isfinite(float(value)) for value in position)):
            raise RuntimeError(f"SpotweldPoint {entity_id} has no complete finite position")
        parts = _point_parts(row, deck, entity_id)
        selected_entities.append(by_id[entity_id][0])
        references.append(reference)
        selected_rows.append({
            "entity": reference,
            "name": row["name"],
            "diameter": diameter,
            "position": position,
            "parts": parts,
        })
    return selected_entities, references, selected_rows


def _report_field(report, name):
    try:
        return getattr(report, name)
    except AttributeError:
        return _MISSING


def _serialize_reports(base, deck, reports, selected_references):
    if type(reports) is not list:
        raise RuntimeError("ANSA Check.execute must return a list of CheckReport objects")

    selected_keys = {(reference["type"], reference["id"])
                     for reference in selected_references}
    issue_counts = {"ok": 0, "error": 0, "warning": 0}
    failed_point_ids = set()
    active, visited = set(), set()
    node_count = 0
    reference_count = 0

    def serialize(report, depth):
        nonlocal node_count, reference_count
        if depth > _MAX_REPORT_DEPTH:
            raise RuntimeError(f"ANSA CheckReport tree exceeds depth {_MAX_REPORT_DEPTH}")
        identity = id(report)
        if identity in active:
            raise RuntimeError("ANSA CheckReport tree contains a cycle")
        if identity in visited:
            raise RuntimeError("ANSA CheckReport tree reuses a report object")
        node_count += 1
        if node_count > _MAX_REPORT_NODES:
            raise RuntimeError(f"ANSA CheckReport tree exceeds {_MAX_REPORT_NODES} nodes")
        active.add(identity)
        visited.add(identity)
        try:
            report_type = _report_field(report, "type")
            description = _report_field(report, "description")
            raw_status = _report_field(report, "status")
            has_fix = _report_field(report, "has_fix")
            is_fixed = _report_field(report, "is_fixed")
            raw_entities = _report_field(report, "entities")
            raw_issues = _report_field(report, "issues")
            if type(report_type) is not str or not report_type:
                raise RuntimeError("ANSA CheckReport is missing a valid type")
            if type(description) is not str:
                raise RuntimeError("ANSA CheckReport is missing a valid description")
            if has_fix is _MISSING or (has_fix is not None and type(has_fix) is not bool):
                raise RuntimeError("ANSA CheckReport has_fix is missing or invalid")
            if type(is_fixed) is not bool:
                raise RuntimeError("ANSA CheckReport is_fixed is missing or invalid")
            if is_fixed:
                raise RuntimeError("ANSA CheckReport is_fixed is true; refusing an auto-fixed result")
            if type(raw_entities) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport entities must be a list or tuple")
            if type(raw_issues) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport issues must be a list or tuple")
            if reference_count + len(raw_entities) > _MAX_REPORT_REFERENCES:
                raise RuntimeError(
                    f"ANSA CheckReport tree exceeds {_MAX_REPORT_REFERENCES} entity references"
                )

            entities = []
            seen_keys = set()
            for entity in raw_entities:
                reference = entity_catalog._entity_reference(base, deck, entity)
                key = (reference["type"], reference["id"])
                if key not in selected_keys or reference["type"] != _POINT_TYPE:
                    raise RuntimeError(
                        "ANSA CheckReport contains an entity outside the selected SpotweldPoint scope"
                    )
                if key in seen_keys:
                    raise RuntimeError("ANSA CheckReport repeats an entity within one report")
                seen_keys.add(key)
                entities.append(reference)
            reference_count += len(entities)

            issues = [serialize(issue, depth + 1) for issue in raw_issues]
            if raw_status is _MISSING or raw_status is None or raw_status == "":
                if not issues:
                    raise RuntimeError("ANSA CheckReport leaf is missing a legal status")
                child_statuses = [issue["status"] for issue in issues]
                if "error" in child_statuses:
                    status = "error"
                elif "warning" in child_statuses:
                    status = "warning"
                else:
                    status = "ok"
            elif type(raw_status) is str:
                status = raw_status.lower()
                if status not in issue_counts:
                    raise RuntimeError(f"ANSA CheckReport has unknown status: {raw_status!r}")
            else:
                raise RuntimeError("ANSA CheckReport status must be a legal string")

            if not raw_issues:
                issue_counts[status] += 1
                if status in {"error", "warning"}:
                    failed_point_ids.update(reference["id"] for reference in entities)

            return {
                "type": report_type,
                "description": description,
                "status": status,
                "has_fix": has_fix,
                "is_fixed": is_fixed,
                "entities": entities,
                "issues": issues,
            }
        finally:
            active.remove(identity)

    rows = [serialize(report, 1) for report in reports]
    return rows, node_count, issue_counts, sorted(failed_point_ids)


def execute(state, tool, params):
    """只读检查显式选中的 NASTRAN 点焊点重复连接。

    原生 Connections 检查只启用按绝对距离的重复连接选项；原生投影检查仍可能
    报告投影问题。任务成功只表示检查报告已生成，不代表问题数为零；不会自动修复。
    """
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "connection_check":
        raise ValueError(f"unsupported ANSA connection check operation: {tool!r}")
    params = inspection._params(params, ("deck", "point_ids"), ("distance",))
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")
    point_ids = _positive_ids(params["point_ids"])
    distance = _positive_finite(params.get("distance", 0.1), "distance")

    from ansa import base, connections as connection_api, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    deck = entity_catalog._deck(base, params)
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    if deck != nastran:
        raise ValueError("connection_check supports only the NASTRAN deck")

    selected_entities, references, selected_rows = _preflight_points(
        base, connection_api, deck, point_ids
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
    if any(type(value) is not int for value in (exec_selected, report_none, keep_old)):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ANSA selected/silent/history check constants are invalid"
        )

    factory = inspection._factory(base, "general", "Connections", _API)
    check = factory()
    if not isinstance(check, check_class):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} did not return an ansa.base.Check"
        )
    available = getattr(check, "is_available_in_deck", None)
    if not callable(available):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.is_available_in_deck is missing"
        )
    if available(deck) is not True:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} does not confirm support for the NASTRAN deck"
        )

    configured_options, option_readback = _configure_check(check, distance)
    execute_check = getattr(check, "execute", None)
    if not callable(execute_check):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.execute is missing"
        )
    reports = execute_check(
        exec_mode=exec_selected,
        report=report_none,
        history=keep_old,
        entities=selected_entities,
    )
    report_rows, report_count, issue_counts, failed_point_ids = _serialize_reports(
        base, deck, reports, references
    )
    result = {
        "deck": deck,
        "check": "double_connections",
        "api": _API,
        "entity_type": _POINT_TYPE,
        "entities": references,
        "selected_points": selected_rows,
        "distance": distance,
        "options": configured_options,
        "option_readback": option_readback,
        "scope": "explicit",
        "read_only": True,
        "auto_fix": False,
        "native_report_count": len(reports),
        "report_count": report_count,
        "issue_count": sum(issue_counts.values()),
        "issue_counts": issue_counts,
        "failed_point_ids": failed_point_ids,
        "reports": report_rows,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"connection check result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
