"""ANSA 原生网格质量准则模板与严格 profile 规范化。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any


_ABAQUS_MESH_VERIFY_URL = (
    "https://docs.software.vt.edu/abaqusv2025/English/"
    "SIMACAECAERefMap/simacae-c-mgnconcpartitionverify.htm"
)
_LS_DYNA_TIME_STEP_URL = "https://lsdyna.ansys.com/time-step-size-2/"

_TEMPLATES: dict[str, dict[str, Any]] = {
    "linear_shell": {
        "id": "linear_shell",
        "analysis_type": "linear",
        "element_family": "shell",
        "reference_solver": "ABAQUS",
        "source_url": _ABAQUS_MESH_VERIFY_URL,
        "required_criteria": [
            "aspect ratio",
            "jacobian",
            "incomplete element",
            "min angle quads",
            "max angle quads",
            "min angle trias",
            "max angle trias",
        ],
        "notes": [
            "ABAQUS Mesh Verify 默认值仅作参考，不是强制标准或 ANSA 默认值。",
            "不内置数值阈值；阈值来自调用方的 ANSA 原生准则。",
        ],
    },
    "linear_solid": {
        "id": "linear_solid",
        "analysis_type": "linear",
        "element_family": "solid",
        "reference_solver": "ABAQUS",
        "source_url": _ABAQUS_MESH_VERIFY_URL,
        "required_criteria": [
            "aspect ratio",
            "jacobian",
            "incomplete element",
            "negative volume",
            "left handed",
            "min angle tetras",
            "max angle tetras",
            "min angle pentas",
            "max angle pentas",
            "min angle hexas",
            "max angle hexas",
        ],
        "notes": [
            "ABAQUS Mesh Verify 默认值仅作参考，不是强制标准或 ANSA 默认值。",
            "不内置数值阈值；阈值来自调用方的 ANSA 原生准则。",
        ],
    },
    "explicit_crash_shell": {
        "id": "explicit_crash_shell",
        "analysis_type": "explicit_crash",
        "element_family": "shell",
        "reference_solver": "LS-DYNA",
        "source_url": _LS_DYNA_TIME_STEP_URL,
        "required_criteria": [
            "aspect ratio",
            "jacobian",
            "incomplete element",
            "warping",
            "skewness",
            "min angle quads",
            "max angle quads",
            "min angle trias",
            "max angle trias",
            "characteristic length",
            "crash time step",
        ],
        "notes": [
            "LS-DYNA 特征长度与稳定时间步公式仅作参考，不构成统一数值限值。",
            "特征长度使用 ANSA 当前 LS-DYNA 原生定义，不能用最短边替代。",
            "target_time_step 必须由调用方明确提供；本模板不推导或预设时间步和长度阈值。",
            "准则及阈值来自调用方的 ANSA 原生准则。",
        ],
    },
    "explicit_crash_solid": {
        "id": "explicit_crash_solid",
        "analysis_type": "explicit_crash",
        "element_family": "solid",
        "reference_solver": "LS-DYNA",
        "source_url": _LS_DYNA_TIME_STEP_URL,
        "required_criteria": [
            "aspect ratio",
            "jacobian",
            "incomplete element",
            "negative volume",
            "left handed",
            "collapse",
            "characteristic length",
            "crash time step",
        ],
        "notes": [
            "LS-DYNA 特征长度与稳定时间步公式仅作参考，不构成统一数值限值。",
            "特征长度使用 ANSA 当前 LS-DYNA 原生定义，不能用最短边替代。",
            "target_time_step 必须由调用方明确提供；本模板不推导或预设时间步和长度阈值。",
            "准则及阈值来自调用方的 ANSA 原生准则。",
        ],
    },
}

_PROFILE_KEYS = frozenset(
    {"template", "quality_criteria", "criteria_label", "target_time_step", "formulation"}
)
_TEMPLATE_IDS = frozenset(_TEMPLATES)


def _string(value: Any, field: str, *, maximum: int, nonempty: bool) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    if len(value) > maximum or (nonempty and not value):
        if nonempty:
            raise ValueError(f"{field} must contain 1 to {maximum} characters")
        raise ValueError(f"{field} must contain at most {maximum} characters")
    return value


def _positive_finite_number(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a positive finite JSON number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{field} must be a positive finite JSON number") from None
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field} must be a positive finite JSON number")
    return number


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
        raise ValueError("profile cannot be represented as strict JSON") from None


def get_template(template: str) -> dict[str, Any]:
    """返回模板目录的独立副本，防止调用方修改共享目录。"""
    if type(template) is not str or template not in _TEMPLATE_IDS:
        raise ValueError("template must be one of: " + ", ".join(sorted(_TEMPLATE_IDS)))
    return copy.deepcopy(_TEMPLATES[template])


def normalize_template_profile(raw: dict) -> dict[str, Any]:
    """严格校验原生模板输入，并返回带稳定 SHA-256 的纯 JSON profile。"""
    if type(raw) is not dict:
        raise ValueError("profile must be a dict")
    if any(type(key) is not str for key in raw):
        raise ValueError("profile keys must be strings")
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        raise ValueError("profile contains unknown keys: " + ", ".join(sorted(unknown)))
    if "template" not in raw:
        raise ValueError("template is required")

    template = _string(raw["template"], "template", maximum=64, nonempty=True)
    if template not in _TEMPLATE_IDS:
        raise ValueError("template must be one of: " + ", ".join(sorted(_TEMPLATE_IDS)))
    definition = _TEMPLATES[template]

    if "quality_criteria" in raw:
        quality_criteria = _string(
            raw["quality_criteria"], "quality_criteria", maximum=65536, nonempty=True
        )
        if not quality_criteria.strip() or len(quality_criteria.encode("utf-8")) > 65536:
            raise ValueError("quality_criteria must be non-empty UTF-8 within 65536 bytes")
    else:
        quality_criteria = None

    if "criteria_label" in raw:
        criteria_label = _string(
            raw["criteria_label"], "criteria_label", maximum=128, nonempty=False
        )
    else:
        criteria_label = "current_session"

    if definition["analysis_type"] == "linear":
        if "target_time_step" in raw:
            raise ValueError("target_time_step is forbidden for linear templates")
        target_time_step = None
    else:
        if "target_time_step" not in raw:
            raise ValueError("explicit_crash templates require target_time_step")
        target_time_step = _positive_finite_number(
            raw["target_time_step"], "target_time_step"
        )

    if "formulation" in raw:
        formulation = _string(raw["formulation"], "formulation", maximum=64, nonempty=True)
    else:
        formulation = None

    profile: dict[str, Any] = {
        "schema_version": 1,
        "template": template,
        "analysis_type": definition["analysis_type"],
        "element_family": definition["element_family"],
        "quality_criteria": quality_criteria,
        "criteria_label": criteria_label,
        "target_time_step": target_time_step,
        "formulation": formulation,
    }
    profile["profile_sha256"] = hashlib.sha256(_canonical_json(profile)).hexdigest()
    return profile


__all__ = ["get_template", "normalize_template_profile"]
