"""网格质量 profile 的规范化与逐实体判定。

本模块只依赖 Python 标准库，不内置任何通用阈值。调用方必须显式给出每个
质量准则的上下界；缺少指标或指标为 ``None`` 时不会按 0 处理。

``explicit_crash`` 的 ``minimum_length`` 是必需的正数。可同时给出
``wave_speed`` 与 ``target_time_step``，模块会据此给出辅助估算值并标记
``estimate_only``；该标记只表示估算，不代表求解器通过。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


SCHEMA_VERSION = 1
ANALYSIS_TYPES = ("linear", "explicit_crash")
ELEMENT_FAMILIES = ("shell", "solid")

# 原生 token 由上层与 ANSA 版本对齐；这里只做白名单校验。
CRITERION_NAMES = (
    "ASPECT",
    "WARP",
    "SKEW",
    "JACOB",
    "MINL",
    "MAXL",
    "CRASH",
    "TETRA_COLLAPSE",
)

_CRITERION_KEYS = frozenset({"name", "min", "max"})
_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "analysis_type",
        "element_family",
        "criteria",
        "minimum_length",
        "wave_speed",
        "target_time_step",
        "estimate_only",
        "estimated_minimum_length",
        "estimated_time_step",
        "profile_sha256",
    }
)
_CRITERION_ORDER = {name: index for index, name in enumerate(CRITERION_NAMES)}


def _invalid(message: str) -> None:
    raise ValueError(message)


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    """Return a finite float, rejecting bool, non-numbers and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _invalid(f"{field} must be a finite number")
    if not math.isfinite(number):
        _invalid(f"{field} must be finite")
    if positive and number <= 0.0:
        _invalid(f"{field} must be positive")
    return number


def _optional_positive(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field, positive=True)


def _choice(value: Any, choices: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(choices)
        _invalid(f"{field} must be one of: {allowed}")
    return value


def _criterion_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or value not in _CRITERION_ORDER:
        allowed = ", ".join(CRITERION_NAMES)
        _invalid(f"{field} must be one of: {allowed}")
    return value


def _normalize_criteria(criteria: Any) -> list[dict[str, Any]]:
    if not isinstance(criteria, (list, tuple)):
        _invalid("criteria must be a list or tuple")
    if not 1 <= len(criteria) <= 16:
        _invalid("criteria must contain between 1 and 16 entries")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(criteria):
        field = f"criteria[{index}]"
        if not isinstance(item, dict):
            _invalid(f"{field} must be a dict")
        unknown_keys = set(item) - _CRITERION_KEYS
        if unknown_keys:
            _invalid(f"{field} contains unknown keys: {sorted(unknown_keys)}")
        if "name" not in item:
            _invalid(f"{field}.name is required")

        name = _criterion_name(item["name"], f"{field}.name")
        if name in seen:
            _invalid(f"duplicate criterion name: {name}")
        seen.add(name)

        has_min = "min" in item
        has_max = "max" in item
        if not has_min and not has_max:
            _invalid(f"{field} must define min, max, or both")

        lower = _finite_number(item["min"], f"{field}.min") if has_min else None
        upper = _finite_number(item["max"], f"{field}.max") if has_max else None
        if lower is not None and upper is not None and lower > upper:
            _invalid(f"{field}.min must not be greater than {field}.max")

        entry: dict[str, Any] = {"name": name}
        if lower is not None:
            entry["min"] = lower
        if upper is not None:
            entry["max"] = upper
        normalized.append(entry)

    normalized.sort(key=lambda entry: _CRITERION_ORDER[entry["name"]])
    return normalized


def _positive_product(left: float, right: float, field: str) -> float:
    result = left * right
    if not math.isfinite(result) or result <= 0.0:
        _invalid(f"{field} must be positive and finite")
    return result


def _positive_quotient(numerator: float, denominator: float, field: str) -> float:
    result = numerator / denominator
    if not math.isfinite(result) or result <= 0.0:
        _invalid(f"{field} must be positive and finite")
    return result


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def normalize_profile(
    analysis_type: str,
    criteria: Any,
    minimum_length: Any = None,
    wave_speed: Any = None,
    target_time_step: Any = None,
    element_family: str = "shell",
) -> dict[str, Any]:
    """校验并规范化质量 profile，返回含规范 JSON SHA-256 的字典。"""
    analysis = _choice(analysis_type, ANALYSIS_TYPES, "analysis_type")
    family = _choice(element_family, ELEMENT_FAMILIES, "element_family")
    normalized_criteria = _normalize_criteria(criteria)

    min_length = _optional_positive(minimum_length, "minimum_length")
    speed = _optional_positive(wave_speed, "wave_speed")
    time_step = _optional_positive(target_time_step, "target_time_step")

    estimated_minimum_length: float | None = None
    estimated_time_step: float | None = None
    estimate_only = False

    if analysis == "linear":
        if min_length is not None or speed is not None or time_step is not None:
            _invalid(
                "minimum_length, wave_speed and target_time_step are only valid for explicit_crash; use a MINL criterion for linear analysis"
            )
    else:
        if min_length is None:
            _invalid("explicit_crash requires a positive minimum_length")
        if (speed is None) != (time_step is None):
            _invalid(
                "wave_speed and target_time_step must be provided together or omitted"
            )
        if speed is not None and time_step is not None:
            estimated_minimum_length = _positive_product(
                speed, time_step, "estimated_minimum_length"
            )
            estimated_time_step = _positive_quotient(
                min_length, speed, "estimated_time_step"
            )
            estimate_only = True

    profile: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "analysis_type": analysis,
        "element_family": family,
        "criteria": normalized_criteria,
        "minimum_length": min_length,
        "wave_speed": speed,
        "target_time_step": time_step,
        "estimate_only": estimate_only,
        "estimated_minimum_length": estimated_minimum_length,
        "estimated_time_step": estimated_time_step,
    }
    profile["profile_sha256"] = _sha256(profile)
    return profile


def _validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        _invalid("profile must be a dict")
    if set(profile) != _PROFILE_KEYS:
        missing = sorted(_PROFILE_KEYS - set(profile))
        extra = sorted(set(profile) - _PROFILE_KEYS)
        _invalid(f"profile keys mismatch; missing={missing}, extra={extra}")

    rebuilt = normalize_profile(
        profile["analysis_type"],
        profile["criteria"],
        minimum_length=profile["minimum_length"],
        wave_speed=profile["wave_speed"],
        target_time_step=profile["target_time_step"],
        element_family=profile["element_family"],
    )
    if rebuilt != profile:
        _invalid("profile is not canonical or profile_sha256 does not match")
    return rebuilt


def _positive_entity_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _invalid(f"{field} must be a positive integer")
    return value


def _metric_value(value: Any, field: str) -> float:
    return _finite_number(value, field)


def evaluate(profile: Mapping[str, Any], values: Any) -> dict[str, Any]:
    """按规范 profile 判断实体质量；缺失或 None 指标只记入 unknown。"""
    canonical = _validate_profile(profile)
    if not isinstance(values, (list, tuple)) or not values:
        _invalid("values must be a list or tuple")

    checks: list[tuple[str, float | None, float | None]] = [
        (criterion["name"], criterion.get("min"), criterion.get("max"))
        for criterion in canonical["criteria"]
    ]
    if canonical["analysis_type"] == "explicit_crash":
        checks.append(("MINL", canonical["minimum_length"], None))

    rows: list[dict[str, Any]] = []
    failed_entity_ids: list[int] = []
    unknown_entity_ids: list[int] = []
    seen_ids: set[int] = set()

    for index, item in enumerate(values):
        field = f"values[{index}]"
        if not isinstance(item, dict):
            _invalid(f"{field} must be a dict")
        if set(item) != {"id", "metrics"}:
            _invalid(f"{field} must contain exactly id and metrics")

        entity_id = _positive_entity_id(item["id"], f"{field}.id")
        if entity_id in seen_ids:
            _invalid(f"duplicate entity id: {entity_id}")
        seen_ids.add(entity_id)

        metrics = item["metrics"]
        if not isinstance(metrics, dict):
            _invalid(f"{field}.metrics must be a dict")
        for name in metrics:
            if not isinstance(name, str) or not name:
                _invalid(f"{field}.metrics keys must be non-empty strings")

        violations: list[str] = []
        unknown: list[str] = []
        for name, lower, upper in checks:
            if name not in metrics or metrics[name] is None:
                if name not in unknown:
                    unknown.append(name)
                continue
            value = _metric_value(metrics[name], f"{field}.metrics[{name!r}]")
            failed = (lower is not None and value < lower) or (
                upper is not None and value > upper
            )
            if failed and name not in violations:
                violations.append(name)

        complete = not unknown
        passed = complete and not violations
        rows.append(
            {
                "id": entity_id,
                "violations": violations,
                "unknown": unknown,
                "complete": complete,
                "passed": passed,
            }
        )
        if violations:
            failed_entity_ids.append(entity_id)
        if unknown:
            unknown_entity_ids.append(entity_id)

    complete = all(row["complete"] for row in rows)
    passed = complete and not failed_entity_ids
    return {
        "rows": rows,
        "failed_entity_ids": failed_entity_ids,
        "unknown_entity_ids": unknown_entity_ids,
        "complete": complete,
        "passed": passed,
        "analysis_type": canonical["analysis_type"],
        "profile_sha256": canonical["profile_sha256"],
        "estimate_only": canonical["estimate_only"],
    }
