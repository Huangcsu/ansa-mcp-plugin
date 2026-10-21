"""ANSA 原生 ElementQuality 数值与保守的逐指标进度比较。"""

from __future__ import annotations

import json
import math
import re


_NATIVE_METRICS = (
    "ASPECT",
    "WARP",
    "SKEW",
    "JACOB",
    "MINL",
    "MAXL",
    "CRASH",
    "COLLAPSE",
)

_CRITERION_METRICS = {
    "aspect": "ASPECT",
    "aspect ratio": "ASPECT",
    "warp": "WARP",
    "warping": "WARP",
    "skew": "SKEW",
    "skewness": "SKEW",
    "jacob": "JACOB",
    "jacobian": "JACOB",
    "minl": "MINL",
    "min length": "MINL",
    "minimum length": "MINL",
    "maxl": "MAXL",
    "max length": "MAXL",
    "maximum length": "MAXL",
    "crash": "CRASH",
    "crash time step": "CRASH",
    "collapse": "COLLAPSE",
    "tetra collapse": "COLLAPSE",
}

_METRIC_DIRECTIONS = {
    "ASPECT": "max",
    "WARP": "max",
    "SKEW": "max",
    "MAXL": "max",
    "JACOB": "min",
    "MINL": "min",
    "CRASH": "min",
    "COLLAPSE": "min",
}

_ABSOLUTE_TOLERANCE = 1e-12
_RELATIVE_TOLERANCE = 1e-9


def _family_name(family):
    if type(family) is not str:
        raise ValueError("family must be shell or solid")
    value = family.strip().lower()
    if value in {"shell", "shells", "element_shell", "element_shells"}:
        return "shell"
    if value in {"solid", "solids", "element_solid", "element_solids"}:
        return "solid"
    raise ValueError("family must be shell or solid")


def _criterion_name(value):
    if type(value) is not str:
        return None
    return re.sub(r"[_\s-]+", " ", value.strip().lower())


def _finite_number(value):
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quality_levels(snapshot, criterion):
    """保留四组原始 pair；只把失败等级第一列解释为阈值。"""
    raw_levels = criterion.get("levels")
    if not isinstance(raw_levels, (list, tuple)) or len(raw_levels) != 4:
        return None, None, None

    levels = []
    for pair in raw_levels:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None, None, None
        preserved = []
        for value in pair:
            if value is not None and _finite_number(value) is None:
                return None, None, None
            preserved.append(value)
        levels.append(preserved)

    index = snapshot.get("failed_range_index")
    if type(index) is not int or index not in range(4):
        return levels, None, None
    threshold = _finite_number(levels[index][0])
    return levels, index, threshold


def _limits_for(direction, threshold):
    if direction not in {"min", "max"}:
        return None
    return {
        "min": threshold if direction == "min" else None,
        "max": threshold if direction == "max" else None,
    }


def _algorithm_token(value):
    if type(value) is str:
        return value.strip().casefold()
    if type(value) is int and value == 0:
        return ""
    return None


def _algorithm_error(base, family, criterion):
    getter_name = "F11ShellsOptionsGet" if family == "shell" else "F11SolidsOptionsGet"
    getter = getattr(base, getter_name, None)
    if not callable(getter):
        return "current F11 calculation is unavailable"
    try:
        options = getter(criterion["name"])
    except Exception as exc:
        return "F11 calculation lookup failed: " + _error_text(exc)
    if not isinstance(options, dict) or "calculation" not in options:
        return "current F11 calculation is unavailable"
    expected = _algorithm_token(criterion.get("calculation"))
    current = _algorithm_token(options["calculation"])
    if expected is None or current is None:
        return "criterion calculation is invalid or unavailable"
    if expected != current:
        return "current F11 calculation differs from criteria_snapshot"
    return None


def _error_text(exc):
    message = str(exc).strip()
    suffix = ": " + message[:200] if message else ""
    return type(exc).__name__ + suffix


def _entity_id(entity):
    try:
        value = entity._id
    except Exception:
        raise ValueError("each entity must expose its ANSA _id") from None
    if type(value) is int:
        return value
    if type(value) is str and value:
        return value
    raise ValueError("each entity must expose a JSON-compatible ANSA _id")


def _criteria_records(base, family, snapshot):
    active = snapshot.get("active_criteria")
    if not isinstance(active, (list, tuple)):
        raise ValueError("criteria_snapshot.active_criteria must be a list")

    records = []
    seen = set()
    for criterion in active:
        if not isinstance(criterion, dict):
            raise ValueError("each active criterion must be a dictionary")
        name = criterion.get("name")
        if type(name) is not str or not name.strip():
            raise ValueError("each active criterion must have a name")
        if name in seen:
            raise ValueError("criteria_snapshot contains a duplicate active criterion")
        seen.add(name)

        metric = _CRITERION_METRICS.get(_criterion_name(name))
        direction = _METRIC_DIRECTIONS.get(metric)
        levels, failed_index, threshold = _quality_levels(snapshot, criterion)
        calculation = criterion.get("calculation")
        if type(calculation) is not str:
            calculation = None
        if metric is None:
            error = "no confirmed ElementQuality mapping for this criterion"
        elif calculation is None:
            error = "criterion calculation is missing"
        else:
            error = _algorithm_error(base, family, criterion)
        records.append({
            "criterion": name,
            "native_metric": metric,
            "direction": direction,
            "calculation": calculation,
            "levels": levels,
            "failed_range_index": failed_index,
            "threshold": threshold,
            "limits": _limits_for(direction, threshold),
            "error": error,
        })
    return records


def measure(base, entities, family, criteria_snapshot):
    """按当前 F11 算法回读活动准则对应的原生 ElementQuality 数值。

    未知标准、算法不匹配、ANSA 返回 error、坏类型及非有限数值均保留为
    available=False/value=None；本函数不产生 passed 判定。
    """
    family = _family_name(family)
    if not isinstance(criteria_snapshot, dict):
        raise ValueError("criteria_snapshot must be a dictionary")
    records = _criteria_records(base, family, criteria_snapshot)

    entity_list = list(entities)
    ids = [_entity_id(entity) for entity in entity_list]
    if len(ids) != len(set(ids)):
        raise ValueError("entities must not contain duplicate ANSA IDs")

    metric_errors = {}
    for record in records:
        metric = record["native_metric"]
        if metric is not None and record["error"]:
            metric_errors.setdefault(metric, record["error"])

    requested = [
        metric for metric in _NATIVE_METRICS
        if any(record["native_metric"] == metric for record in records)
        and metric not in metric_errors
    ]

    result = {}
    for entity, entity_id in zip(entity_list, ids):
        raw_values = {}
        call_error = None
        if requested:
            try:
                values = base.ElementQuality(entity, requested)
            except Exception as exc:
                values = None
                call_error = "ElementQuality call failed: " + _error_text(exc)
            if call_error is None:
                if not isinstance(values, (list, tuple)) or len(values) != len(requested):
                    call_error = "ElementQuality returned an invalid result shape"
                else:
                    raw_values = dict(zip(requested, values))

        metrics = {
            record["native_metric"]: None
            for record in records
            if record["native_metric"] is not None
        }
        details = []
        for record in records:
            metric = record["native_metric"]
            value = None
            error = record["error"]
            if metric is not None and error is None:
                if metric in metric_errors:
                    error = metric_errors[metric]
                elif call_error:
                    error = call_error
                else:
                    value = _finite_number(raw_values.get(metric))
                    if value is None:
                        error = "ElementQuality value is unavailable or non-numeric"
            available = error is None and value is not None
            if metric is not None:
                metrics[metric] = value if available else None
            detail = {
                "criterion": record["criterion"],
                "native_metric": metric,
                "calculation": record["calculation"],
                "levels": record["levels"],
                "failed_range_index": record["failed_range_index"],
                "direction": record["direction"],
                "threshold": record["threshold"],
                "limits": record["limits"],
                "value": value if available else None,
                "available": available,
            }
            if error:
                detail["error"] = error
            details.append(detail)
        result[entity_id] = {"family": family, "metrics": metrics, "quality_details": details}
    return result


def _incomparable(reason):
    return {
        "improved": False,
        "comparable": False,
        "regressed": False,
        "regressions": [],
        "improvements": [],
        "reason": reason,
    }


def _report_family(report, row):
    values = []
    if "family" in row:
        values.append(row["family"])
    if "family" in report:
        values.append(report["family"])
    profile = report.get("profile")
    if isinstance(profile, dict) and "element_family" in profile:
        values.append(profile["element_family"])
    if "entity_type" in report:
        values.append(report["entity_type"])
    if not values:
        return None

    normalized = []
    for value in values:
        if type(value) is not str:
            return None
        token = value.strip().lower()
        if token in {"shell", "shells", "element_shell", "element_shells"}:
            normalized.append("shell")
        elif token in {"solid", "solids", "element_solid", "element_solids"}:
            normalized.append("solid")
        else:
            return None
    return normalized[0] if len(set(normalized)) == 1 else None


def _canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


def _snapshot_criteria(snapshot):
    active = snapshot.get("active_criteria")
    if not isinstance(active, list) or not active:
        return None
    snapshot_json = _canonical_json(snapshot)
    if snapshot_json is None:
        return None

    criteria = {}
    for item in active:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        normalized_name = _criterion_name(name)
        calculation = item.get("calculation")
        levels, index, threshold = _quality_levels(snapshot, item)
        if (normalized_name is None or not normalized_name or normalized_name in criteria
                or type(calculation) is not str or levels is None or index is None):
            return None
        metric = _CRITERION_METRICS.get(normalized_name)
        direction = _METRIC_DIRECTIONS.get(metric)
        criteria[normalized_name] = {
            "name": name,
            "calculation": calculation,
            "levels": levels,
            "failed_range_index": index,
            "threshold": threshold,
            "native_metric": metric,
            "direction": direction,
            "limits": _limits_for(direction, threshold),
        }
    return criteria, snapshot_json


def _equal_number(value, expected):
    if expected is None:
        return value is None
    return _finite_number(value) == expected


def _validated_detail(detail, row, expected):
    if not isinstance(detail, dict):
        return None
    if (detail.get("criterion") != expected["name"]
            or detail.get("native_metric") != expected["native_metric"]
            or detail.get("calculation") != expected["calculation"]
            or detail.get("levels") != expected["levels"]
            or detail.get("failed_range_index") != expected["failed_range_index"]
            or detail.get("direction") != expected["direction"]
            or not _equal_number(detail.get("threshold"), expected["threshold"])):
        return None

    expected_limits = expected["limits"]
    actual_limits = detail.get("limits")
    if expected_limits is None:
        if actual_limits is not None:
            return None
    elif (not isinstance(actual_limits, dict)
            or set(actual_limits) != {"min", "max"}
            or any(not _equal_number(actual_limits[key], expected_limits[key])
                   for key in ("min", "max"))):
        return None

    available = detail.get("available")
    value = _finite_number(detail.get("value"))
    if available is False:
        if detail.get("value") is not None:
            return None
        return {
            "available": False,
            "value": None,
            "threshold": expected["threshold"],
            "direction": expected["direction"],
            "native_metric": expected["native_metric"],
        }
    if available is not True or expected["native_metric"] not in _NATIVE_METRICS or value is None:
        return None

    metrics = row.get("metrics")
    if not isinstance(metrics, dict) or _finite_number(metrics.get(expected["native_metric"])) != value:
        return None
    return {
        "available": True,
        "value": value,
        "threshold": expected["threshold"],
        "direction": expected["direction"],
        "native_metric": expected["native_metric"],
    }


def _load_report(report):
    if not isinstance(report, dict) or report.get("complete") is not True:
        return None, "incomplete_report"
    if report.get("rows_truncated") is True:
        return None, "incomplete_report"
    rows = report.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, "incomplete_report"
    if report.get("rows_returned", len(rows)) != len(rows):
        return None, "incomplete_report"
    if report.get("rows_total", len(rows)) != len(rows):
        return None, "incomplete_report"
    unknown_ids = report.get("unknown_entity_ids", [])
    if not isinstance(unknown_ids, (list, tuple)) or unknown_ids:
        return None, "incomplete_report"

    snapshot = report.get("criteria_snapshot")
    if not isinstance(snapshot, dict):
        return None, "criteria_configuration_missing"
    snapshot_data = _snapshot_criteria(snapshot)
    if snapshot_data is None:
        return None, "criteria_configuration_missing"
    expected_criteria, snapshot_json = snapshot_data

    loaded = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("complete") is not True:
            return None, "incomplete_report"
        element_id = row.get("id")
        if type(element_id) not in (int, str) or (type(element_id) is str and not element_id):
            return None, "incomplete_report"
        if element_id in loaded:
            return None, "incomplete_report"
        family = _report_family(report, row)
        if family is None:
            return None, "family_missing_or_inconsistent"

        details = row.get("quality_details")
        if not isinstance(details, list) or len(details) != len(expected_criteria):
            return None, "incomplete_metric_evidence"
        detail_map = {}
        for detail in details:
            if not isinstance(detail, dict):
                return None, "incomplete_metric_evidence"
            normalized_name = _criterion_name(detail.get("criterion"))
            expected = expected_criteria.get(normalized_name)
            if expected is None or normalized_name in detail_map:
                return None, "criteria_configuration_missing"
            validated = _validated_detail(detail, row, expected)
            if validated is None:
                return None, "incomplete_metric_evidence"
            detail_map[normalized_name] = validated
        if set(detail_map) != set(expected_criteria):
            return None, "criteria_configuration_missing"

        violations = row.get("violations", [])
        if (not isinstance(violations, (list, tuple, set))
                or any(type(item) is not str for item in violations)):
            return None, "incomplete_report"
        normalized_violations = {_criterion_name(item) for item in violations}
        if None in normalized_violations or not normalized_violations <= set(expected_criteria):
            return None, "unknown_violation"
        for criterion in normalized_violations:
            detail = detail_map[criterion]
            if (not detail["available"] or detail["threshold"] is None
                    or detail["direction"] not in {"min", "max"}):
                return None, "incomplete_metric_evidence"

        passed = row.get("passed")
        if passed is not None and type(passed) is not bool:
            return None, "incomplete_report"
        loaded[element_id] = {
            "family": family,
            "details": detail_map,
            "violations": normalized_violations,
            "passed": passed,
        }

    failed_ids = report.get("failed_entity_ids")
    if not isinstance(failed_ids, (list, tuple)):
        return None, "incomplete_report"
    if any(type(value) not in (int, str) for value in failed_ids):
        return None, "incomplete_report"
    if len(failed_ids) != len(set(failed_ids)) or not set(failed_ids) <= set(loaded):
        return None, "incomplete_report"
    failed_ids = set(failed_ids)
    for element_id, row in loaded.items():
        if row["passed"] is not None and row["passed"] == (element_id in failed_ids):
            return None, "incomplete_report"
    return {
        "rows": loaded,
        "criteria": expected_criteria,
        "snapshot": snapshot,
        "snapshot_json": snapshot_json,
        "report": report,
        "failed_ids": failed_ids,
    }, None


def _configuration_reason(before, after):
    for name in set(before["criteria"]) & set(after["criteria"]):
        if before["criteria"][name]["calculation"] != after["criteria"][name]["calculation"]:
            return "algorithm_changed"
    if before["snapshot_json"] != after["snapshot_json"]:
        return "criteria_configuration_changed"

    before_rows = before["rows"]
    after_rows = after["rows"]
    if set(before_rows) != set(after_rows):
        return "element_scope_changed"
    for element_id in before_rows:
        if before_rows[element_id]["family"] != after_rows[element_id]["family"]:
            return "family_changed"

    for key in ("deck", "analysis_type", "entity_type"):
        if before["report"].get(key) != after["report"].get(key):
            return "report_configuration_changed"
    before_profile = before["report"].get("profile")
    after_profile = after["report"].get("profile")
    if isinstance(before_profile, dict) != isinstance(after_profile, dict):
        return "report_configuration_changed"
    if isinstance(before_profile, dict):
        for key in ("template", "analysis_type", "element_family", "target_time_step",
                    "formulation", "profile_sha256"):
            if before_profile.get(key) != after_profile.get(key):
                return "report_configuration_changed"
    return None


def _tolerance(*values):
    scale = max((abs(value) for value in values if value is not None), default=0.0)
    return _ABSOLUTE_TOLERANCE + _RELATIVE_TOLERANCE * scale


def _violation_degree(value, threshold, direction):
    if direction == "max":
        return max(0.0, value - threshold)
    return max(0.0, threshold - value)


def progress(before, after):
    """比较同范围、同快照报告中的失败严重度，不累计不同指标或单位。"""
    old, error = _load_report(before)
    if error:
        return _incomparable(error)
    new, error = _load_report(after)
    if error:
        return _incomparable(error)
    reason = _configuration_reason(old, new)
    if reason:
        return _incomparable(reason)
    if len(new["failed_ids"]) < len(old["failed_ids"]):
        return _incomparable("failure_count_decreased")

    regressions = []
    improvements = []
    for element_id in sorted(new["failed_ids"] - old["failed_ids"], key=str):
        regressions.append({"element_id": element_id, "kind": "new_failed_element"})

    for element_id in sorted(old["rows"], key=str):
        old_row = old["rows"][element_id]
        new_row = new["rows"][element_id]
        added_violations = sorted(new_row["violations"] - old_row["violations"])
        for violation in added_violations:
            regressions.append({
                "element_id": element_id,
                "kind": "new_violation",
                "violation": violation,
            })

        for criterion in sorted(old_row["details"]):
            old_detail = old_row["details"][criterion]
            new_detail = new_row["details"][criterion]
            if (not old_detail["available"] or not new_detail["available"]
                    or old_detail["threshold"] is None or new_detail["threshold"] is None):
                continue
            threshold = old_detail["threshold"]
            direction = old_detail["direction"]
            if direction != new_detail["direction"] or threshold != new_detail["threshold"]:
                return _incomparable("criteria_configuration_changed")
            old_risk = _violation_degree(old_detail["value"], threshold, direction)
            new_risk = _violation_degree(new_detail["value"], threshold, direction)
            tolerance = _tolerance(
                old_detail["value"], new_detail["value"], threshold, old_risk, new_risk
            )
            record = {
                "element_id": element_id,
                "criterion": old["criteria"][criterion]["name"],
                "native_metric": old_detail["native_metric"],
                "direction": direction,
                "threshold": threshold,
                "before_risk": old_risk,
                "after_risk": new_risk,
                "tolerance": tolerance,
            }
            if new_risk > old_risk + tolerance:
                regressions.append(dict(record, kind="metric_deterioration"))
                if old_risk <= tolerance and new_risk > tolerance:
                    regressions.append(dict(record, kind="new_threshold_violation"))
            elif (criterion in old_row["violations"]
                    and old_risk > tolerance and new_risk < old_risk - tolerance):
                improvements.append(record)

    regressed = bool(regressions)
    improved = not regressed and bool(improvements)
    if regressed:
        reason = "metric_or_violation_regression"
    elif improved:
        reason = "metrics_improved_without_regression"
    else:
        reason = "no_per_metric_improvement"
    return {
        "improved": improved,
        "comparable": True,
        "regressed": regressed,
        "regressions": regressions,
        "improvements": improvements,
        "reason": reason,
        "tolerance": {
            "absolute": _ABSOLUTE_TOLERANCE,
            "relative": _RELATIVE_TOLERANCE,
        },
    }


__all__ = ["measure", "progress"]
