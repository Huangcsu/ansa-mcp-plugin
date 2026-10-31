"""Capture, bind, and temporarily restore native meshing configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, MutableMapping
import uuid

from . import (
    _component_has_reparse_point,
    mesh_settings,
    meshing,
    native_quality,
    quality_options,
    quality_workflow,
    structured_evidence,
)


_COVERAGE = "all_current_exported_criteria_rows"
_FAMILIES = {
    "shells": ("F11ShellsOptionsGet", "F11ShellsOptionsSet"),
    "solids": ("F11SolidsOptionsGet", "F11SolidsOptionsSet"),
}
_GETTER_KEYS = {"status", "calculation", "value"}
_MAX_FILE_BYTES = structured_evidence._MAX_FILE_BYTES


class MeshingContextError(RuntimeError):
    """A failed temporary operation, including its independent restore errors."""

    def __init__(
        self,
        operation_error: BaseException | None,
        restore_errors: list[dict[str, str]],
    ) -> None:
        self.operation_error = operation_error
        self.restore_errors = list(restore_errors)
        parts = []
        if operation_error is not None:
            parts.append(f"operation failed: {type(operation_error).__name__}: {operation_error}")
        if restore_errors:
            parts.append(f"{len(restore_errors)} restoration error(s)")
        super().__init__("; ".join(parts) or "meshing context failed")


@dataclass(frozen=True)
class _Snapshot:
    directory: Path
    mesh_path: Path
    quality_path: Path
    mesh_file_sha256: str
    quality_file_sha256: str
    mesh_values: dict[str, list[str]]
    current_criteria: str
    exported_criteria: str
    rows: dict[tuple[str, str], dict[str, Any]]
    f11: dict[str, dict[str, dict[str, bool | str | float | int]]]
    settings: dict[str, str]
    snapshot_sha256: str


def _error_record(exc: BaseException) -> dict[str, str]:
    message = str(exc)
    if len(message) > 1024:
        message = message[:1021] + "..."
    return {"type": type(exc).__name__, "message": message}


def _api(state: Any, owner: Any, name: str, *args: Any) -> Any:
    state.assert_main()
    method = getattr(owner, name, None)
    if not callable(method):
        raise RuntimeError(f"required ANSA API is unavailable: {name}")
    return method(*args)


def _settings(state: Any, base: Any) -> dict[str, str]:
    state.assert_main()
    return meshing._settings(base)


def _criteria(state: Any, base: Any) -> str:
    state.assert_main()
    return quality_workflow._criteria(base)


def _settings_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    return all(
        mesh_settings._same_setting(key, left[key], right[key])
        for key in meshing._SETTINGS
    )


def _session_directory(state: Any) -> Path:
    raw = getattr(state, "session_dir", None)
    if not isinstance(raw, (str, os.PathLike)):
        raise ValueError("private session directory is unavailable")
    directory = Path(raw)
    if not directory.is_absolute():
        raise ValueError("private session directory must be absolute")
    if _component_has_reparse_point(directory):
        raise ValueError("private session directory traverses a symlink or reparse point")
    try:
        info = os.lstat(directory)
    except OSError as exc:
        raise ValueError("private session directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("private session path is not a directory")
    return directory


def _suffix_names(directory: Path, suffix: str) -> set[str]:
    names = set()
    for entry in directory.iterdir():
        if not entry.name.casefold().endswith(suffix.casefold()):
            continue
        if _component_has_reparse_point(entry):
            raise ValueError("snapshot directory contains a reparse-point file")
        names.add(entry.name)
    return names


def _new_path(directory: Path, suffix: str) -> Path:
    for _ in range(8):
        target = directory / ("meshing-context-" + uuid.uuid4().hex + suffix)
        if not os.path.lexists(target) and not _component_has_reparse_point(target):
            return target
    raise RuntimeError("could not allocate a unique native snapshot path")


def _read_text(path: Path, directory: Path, suffix: str) -> tuple[str, str]:
    if path.parent != directory or path.suffix.casefold() != suffix.casefold():
        raise ValueError("native snapshot path escaped its private session directory")
    if _component_has_reparse_point(path):
        raise ValueError("native snapshot file traverses a symlink or reparse point")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("native snapshot file was not created") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise RuntimeError("native snapshot is not a non-empty regular file")
    if info.st_size > _MAX_FILE_BYTES:
        raise RuntimeError("native snapshot exceeds the existing file storage budget")
    raw = path.read_bytes()
    if len(raw) != info.st_size or len(raw) > _MAX_FILE_BYTES:
        raise RuntimeError("native snapshot changed while it was being read")
    if _component_has_reparse_point(path):
        raise ValueError("native snapshot file changed to a reparse point")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("native snapshot is not UTF-8") from exc
    return text, hashlib.sha256(raw).hexdigest()


def _save_native_file(
    state: Any,
    api: Any,
    method_name: str,
    suffix: str,
    directory: Path,
) -> tuple[Path, str, str]:
    before = _suffix_names(directory, suffix)
    target = _new_path(directory, suffix)
    status = _api(state, api, method_name, str(target))
    if type(status) is not int or status != 1:
        raise RuntimeError(f"{method_name} returned {status!r}")
    after = _suffix_names(directory, suffix)
    if after - before != {target.name}:
        raise RuntimeError(f"{method_name} did not create one unique {suffix} file")
    text, digest = _read_text(target, directory, suffix)
    return target, text, digest


def _parse_criteria(text: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str]]:
    rows, assignments = native_quality._parse(text)
    line_count = sum(
        native_quality._ROW.match(line) is not None for line in text.splitlines()
    )
    if line_count != len(rows):
        raise ValueError("native criteria contain duplicate shell/solid row identities")
    for family, name in rows:
        if family not in _FAMILIES or type(name) is not str or not name.strip():
            raise ValueError("native criteria contain an unsupported row identity")
    return rows, assignments


def _json_signature(value: str) -> list[dict[str, str]]:
    signature = native_quality._metadata_signature(value)
    return [
        {"number": str(item.normalize())}
        if isinstance(item, Decimal)
        else {"text": item}
        for item in signature
    ]


def _criteria_semantics(
    rows: dict[tuple[str, str], dict[str, Any]], assignments: dict[str, str]
) -> dict[str, Any]:
    row_keys = {row["key"] for row in rows.values()}
    metadata = [
        [key, _json_signature(value)]
        for key, value in sorted(assignments.items())
        if key not in row_keys
    ]
    criteria_rows = []
    for (family, name), row in sorted(rows.items()):
        criteria_rows.append(
            {
                "family": family,
                "name": name,
                "active": row["active"],
                "calculation": " ".join(row["calculation"].split()),
                "levels": row["levels"],
                "line": _json_signature(row["line"]),
            }
        )
    return {"metadata": metadata, "rows": criteria_rows}


def _getter_state(state: Any, base: Any, family: str, name: str) -> dict[str, bool | str | float | int]:
    getter_name = _FAMILIES[family][0]
    raw = _api(state, base, getter_name, name)
    if type(raw) is not dict or set(raw) != _GETTER_KEYS:
        raise ValueError(f"{family} F11 getter did not return exactly status/calculation/value for {name!r}")

    status = raw["status"]
    if type(status) is bool:
        normalized_status = status
    elif type(status) is int and status in (0, 1):
        normalized_status = bool(status)
    else:
        raise ValueError(f"F11 status is not bool or exact 0/1 for {name!r}")

    calculation = raw["calculation"]
    if not (type(calculation) is str or (type(calculation) is int and calculation == 0)):
        raise ValueError(f"F11 calculation is not a string or 0 for {name!r}")

    value = raw["value"]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"F11 value is not a finite number for {name!r}")
    return {
        "status": normalized_status,
        "calculation": calculation,
        "value": float(value),
    }


def _read_f11_states(
    state: Any,
    base: Any,
    rows: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, bool | str | float | int]]], list[tuple[str, str, BaseException]]]:
    values: dict[str, dict[str, dict[str, bool | str | float | int]]] = {
        "shells": {},
        "solids": {},
    }
    errors = []
    for family, name in sorted(rows):
        try:
            values[family][name] = _getter_state(state, base, family, name)
        except BaseException as exc:
            errors.append((family, name, exc))
    return values, errors


def _f11_count(values: dict[str, dict[str, Any]]) -> int:
    return sum(len(rows) for rows in values.values())


def _snapshot_digest(
    mesh_values: dict[str, list[str]],
    criteria_semantics: dict[str, Any],
    f11: dict[str, dict[str, dict[str, bool | str | float | int]]],
    settings: dict[str, str],
) -> str:
    content = {
        "coverage": _COVERAGE,
        "mesh_parameters": mesh_values,
        "quality_criteria": criteria_semantics,
        "f11": f11,
        "meshing_settings": settings,
    }
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capture_context(state: Any, base: Any, mesh: Any) -> _Snapshot:
    state.assert_main()
    directory = _session_directory(state)
    settings_before = _settings(state, base)
    criteria_before = _criteria(state, base)
    rows_before, _ = _parse_criteria(criteria_before)
    f11_before, getter_errors = _read_f11_states(state, base, rows_before)
    if getter_errors:
        details = ", ".join(
            f"{family}:{name} ({type(exc).__name__}: {exc})"
            for family, name, exc in getter_errors[:8]
        )
        raise RuntimeError("native F11 state is incomplete: " + details)

    mesh_path, mesh_text, mesh_file_sha = _save_native_file(
        state, mesh, "SaveMeshParams", ".ansa_mpar", directory
    )
    mesh_values = quality_options._parameter_values(mesh_text)
    if type(mesh_values) is not dict or not mesh_values:
        raise RuntimeError("Mesh Parameters export has no complete assignment values")

    quality_path, quality_text, quality_file_sha = _save_native_file(
        state, mesh, "SaveQualityCriteria", ".ansa_qual", directory
    )
    checks = []
    rows_export = {}
    assignments_export = {}
    try:
        rows_export, assignments_export = _parse_criteria(quality_text)
        if set(rows_export) != set(rows_before):
            raise RuntimeError("exported quality rows differ from current quality text")
        native_quality._require_criteria_match(
            criteria_before, quality_text, "capturing current quality criteria"
        )
    except BaseException as exc:
        checks.append("quality export: " + str(exc))

    try:
        criteria_after = _criteria(state, base)
        native_quality._require_criteria_match(
            criteria_before, criteria_after, "saving configuration snapshot"
        )
        if set(_parse_criteria(criteria_after)[0]) != set(rows_before):
            raise RuntimeError("quality row identities changed while capturing")
    except BaseException as exc:
        checks.append("quality text: " + str(exc))

    f11_after, getter_errors = _read_f11_states(state, base, rows_before)
    checks.extend(
        f"F11 {family}:{name}: {type(exc).__name__}: {exc}"
        for family, name, exc in getter_errors
    )
    if not getter_errors and f11_after != f11_before:
        checks.append("F11 getter states changed while capturing")

    try:
        settings_after = _settings(state, base)
        if not _settings_equal(settings_before, settings_after):
            checks.append("four meshing settings changed while capturing")
    except BaseException as exc:
        checks.append("meshing settings: " + str(exc))
    if checks:
        raise RuntimeError("configuration changed or became unreadable during capture: " + "; ".join(checks[:8]))

    semantics = _criteria_semantics(rows_export, assignments_export)
    digest = _snapshot_digest(mesh_values, semantics, f11_before, settings_before)
    return _Snapshot(
        directory=directory,
        mesh_path=mesh_path,
        quality_path=quality_path,
        mesh_file_sha256=mesh_file_sha,
        quality_file_sha256=quality_file_sha,
        mesh_values=mesh_values,
        current_criteria=criteria_before,
        exported_criteria=quality_text,
        rows=rows_export,
        f11=f11_before,
        settings=settings_before,
        snapshot_sha256=digest,
    )


def _summary(snapshot: _Snapshot) -> dict[str, Any]:
    return {
        "snapshot_sha256": snapshot.snapshot_sha256,
        "coverage": _COVERAGE,
        "counts": {
            "mesh_parameter_keys": len(snapshot.mesh_values),
            "criteria_rows": len(snapshot.rows),
            "f11_options": _f11_count(snapshot.f11),
            "meshing_settings": len(meshing._SETTINGS),
        },
        "recovery_paths": {
            "mesh_parameters": str(snapshot.mesh_path),
            "quality_criteria": str(snapshot.quality_path),
        },
    }


def capture(state: Any, base: Any, mesh: Any) -> dict[str, Any]:
    """Persist and return a bounded, path-independent configuration fingerprint."""
    return _summary(_capture_context(state, base, mesh))


def _restore_f11_item(
    state: Any,
    base: Any,
    family: str,
    name: str,
    desired: dict[str, bool | str | float | int],
) -> bool:
    getter_error = None
    try:
        if _getter_state(state, base, family, name) == desired:
            return False
    except BaseException as exc:
        getter_error = exc

    setter_name = _FAMILIES[family][1]
    calculation = desired["calculation"]
    setter_calculation = "" if type(calculation) is int and calculation == 0 else calculation
    status = _api(
        state,
        base,
        setter_name,
        name,
        desired["status"],
        setter_calculation,
        desired["value"],
    )
    if type(status) is not int or status != 1:
        raise RuntimeError(f"{setter_name} returned {status!r} for {name!r}")
    confirmed = _getter_state(state, base, family, name)
    if confirmed != desired:
        raise RuntimeError(f"F11 getter did not confirm restored state for {name!r}")
    return True


def _restore_context(
    state: Any,
    base: Any,
    mesh: Any,
    snapshot: _Snapshot,
    result: MutableMapping[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    mpar_result = result["mpar"]
    criteria_result = result["criteria"]
    f11_result = result["f11"]
    settings_result = result["settings"]

    def record(step: str, exc: BaseException) -> None:
        entry = {"step": step, **_error_record(exc)}
        errors.append(entry)

    try:
        text, digest = _read_text(snapshot.mesh_path, snapshot.directory, ".ansa_mpar")
        if digest != snapshot.mesh_file_sha256 or quality_options._parameter_values(text) != snapshot.mesh_values:
            raise RuntimeError("original Mesh Parameters backup changed after capture")
        status = _api(state, mesh, "ReadMeshParams", str(snapshot.mesh_path))
        if type(status) is not int or status != 1:
            raise RuntimeError(f"ReadMeshParams returned {status!r}")
        mpar_result["restore_applied"] = True
    except BaseException as exc:
        record("mpar_restore", exc)

    try:
        status = _api(
            state,
            base,
            "BCSettingsSetValues",
            {"mesh_change_option": snapshot.settings["mesh_change_option"]},
        )
        if type(status) is not int or status != 0:
            raise RuntimeError(f"BCSettingsSetValues mesh_change_option returned {status!r}")
        settings_result["mesh_change_option_restored"] = True
    except BaseException as exc:
        record("mesh_change_option_restore", exc)

    try:
        text, digest = _read_text(snapshot.quality_path, snapshot.directory, ".ansa_qual")
        if digest != snapshot.quality_file_sha256:
            raise RuntimeError("original quality criteria backup changed after capture")
        native_quality._require_criteria_match(
            snapshot.exported_criteria, text, "checking original quality backup"
        )
        status = _api(state, mesh, "ReadQualityCriteria", str(snapshot.quality_path))
        if type(status) is not int or status != 1:
            raise RuntimeError(f"ReadQualityCriteria returned {status!r}")
        criteria_result["restore_applied"] = True
    except BaseException as exc:
        record("criteria_restore", exc)

    restored_count = 0
    set_count = 0
    f11_total = _f11_count(snapshot.f11)
    for family in ("shells", "solids"):
        for name, desired in sorted(snapshot.f11[family].items()):
            try:
                changed = _restore_f11_item(state, base, family, name, desired)
                restored_count += 1
                set_count += int(changed)
            except BaseException as exc:
                record(f"f11_restore:{family}:{name}", exc)
    f11_result["restore_attempted_count"] = f11_total
    f11_result["restore_set_count"] = set_count
    f11_result["restored_count"] = restored_count
    f11_result["restored"] = restored_count == f11_total

    try:
        path, text, _digest = _save_native_file(
            state, mesh, "SaveMeshParams", ".ansa_mpar", snapshot.directory
        )
        mpar_result["verification_path"] = str(path)
        actual = quality_options._parameter_values(text)
        if actual != snapshot.mesh_values:
            raise RuntimeError("complete Mesh Parameters key/value readback differs from capture")
        mpar_result["verified"] = True
    except BaseException as exc:
        record("mpar_verify", exc)

    criteria_checks = []
    try:
        current = _criteria(state, base)
        native_quality._require_criteria_match(
            snapshot.current_criteria, current, "verifying restored criteria text"
        )
        if set(_parse_criteria(current)[0]) != set(snapshot.rows):
            raise RuntimeError("restored quality row identities differ from capture")
    except BaseException as exc:
        criteria_checks.append(exc)
        record("criteria_text_verify", exc)

    try:
        path, text, _digest = _save_native_file(
            state, mesh, "SaveQualityCriteria", ".ansa_qual", snapshot.directory
        )
        criteria_result["verification_path"] = str(path)
        rows, _assignments = _parse_criteria(text)
        if set(rows) != set(snapshot.rows):
            raise RuntimeError("re-exported quality row identities differ from capture")
        native_quality._require_criteria_match(
            snapshot.exported_criteria, text, "verifying restored quality export"
        )
    except BaseException as exc:
        criteria_checks.append(exc)
        record("criteria_export_verify", exc)

    try:
        current_after_save = _criteria(state, base)
        native_quality._require_criteria_match(
            snapshot.current_criteria, current_after_save, "verifying criteria after export"
        )
    except BaseException as exc:
        criteria_checks.append(exc)
        record("criteria_after_save_verify", exc)
    criteria_result["verified"] = not criteria_checks

    current_f11, getter_errors = _read_f11_states(state, base, snapshot.rows)
    for family, name, exc in getter_errors:
        record(f"f11_verify:{family}:{name}", exc)
    f11_result["verified"] = not getter_errors and current_f11 == snapshot.f11
    if not getter_errors and current_f11 != snapshot.f11:
        record("f11_verify", RuntimeError("one or more F11 states differ from capture"))

    try:
        settings_after = _settings(state, base)
        settings_result["verified"] = _settings_equal(snapshot.settings, settings_after)
        if not settings_result["verified"]:
            raise RuntimeError("one or more of the four meshing settings differ from capture")
    except BaseException as exc:
        settings_result["verified"] = False
        record("settings_verify", exc)

    mpar_result["verified"] = bool(mpar_result.get("verified"))
    criteria_result["restored"] = bool(criteria_result.get("restore_applied"))
    result["restore_errors"] = errors
    result["errors"]["restore_errors"] = errors
    result["restored"] = (
        mpar_result["restore_applied"]
        and mpar_result["verified"]
        and criteria_result["restored"]
        and criteria_result["verified"]
        and f11_result["restored"]
        and f11_result["verified"]
        and settings_result["mesh_change_option_restored"]
        and settings_result["verified"]
        and not errors
    )
    return errors


def _target_length(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("target_length must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("target_length must be a finite positive number")
    return number


def _expected_hash(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("expected_snapshot_sha256 must be a lowercase SHA-256 hex digest")
    return value


def _requested_element_type(value: Any) -> str:
    if type(value) is not str or value not in {"Quad", "Mixed", "Tria"}:
        raise ValueError("element_type must be exactly one of: Quad, Mixed, Tria")
    return value


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000)


def _apply_temporary_settings(
    state: Any,
    base: Any,
    mesh: Any,
    snapshot: _Snapshot,
    target_length: float,
    element_type: str,
) -> None:
    status = _api(
        state,
        base,
        "BCSettingsSetValues",
        {"element_type": element_type, "element_order": "1st Order"},
    )
    if type(status) is not int or status != 0:
        raise RuntimeError(f"BCSettingsSetValues returned {status!r}")
    status = _api(state, mesh, "SetMeshParamTargetLength", "absolute", target_length)
    if type(status) is not int or status != 1:
        raise RuntimeError(f"SetMeshParamTargetLength returned {status!r}")

    applied = _settings(state, base)
    if applied["mesh_change_option"] != snapshot.settings["mesh_change_option"]:
        raise RuntimeError("temporary meshing settings changed mesh_change_option")
    if (
        applied["element_type"].casefold() != element_type.casefold()
        or applied["element_order"] != "1st Order"
    ):
        raise RuntimeError(
            f"native getter did not confirm temporary {element_type}/1st settings"
        )
    try:
        actual_length = float(applied["target_element_length"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("native getter did not return an absolute target length") from exc
    if not math.isfinite(actual_length) or not math.isclose(
        actual_length, target_length, rel_tol=1e-9, abs_tol=1e-12
    ):
        raise RuntimeError("native getter did not confirm the absolute target length")

    current_criteria = _criteria(state, base)
    native_quality._require_criteria_match(
        snapshot.current_criteria, current_criteria, "binding current quality criteria"
    )
    if set(_parse_criteria(current_criteria)[0]) != set(snapshot.rows):
        raise RuntimeError("current quality rows changed before the callback")
    current_f11, getter_errors = _read_f11_states(state, base, snapshot.rows)
    if getter_errors:
        family, name, exc = getter_errors[0]
        raise RuntimeError(f"F11 state could not be bound for {family}:{name}: {exc}")
    if current_f11 != snapshot.f11:
        raise RuntimeError("F11 states changed before the callback")


def run_with_context(
    state: Any,
    base: Any,
    mesh: Any,
    target_length: float,
    callback: Callable[[], Any],
    result: MutableMapping[str, Any],
    *,
    expected_snapshot_sha256: str | None = None,
    element_type: str = "Quad",
) -> Any:
    """Run one callback under temporary native settings and independently restore them."""
    total_started_ns = time.monotonic_ns()
    timings = {
        "capture": None,
        "apply": None,
        "callback": None,
        "restore": None,
        "total": None,
    }
    result.update(
        {
            "snapshot_sha256": None,
            "requested_element_type": element_type if type(element_type) is str else None,
            "timings_ms": timings,
            "coverage": _COVERAGE,
            "counts": {},
            "recovery_paths": {},
            "mpar": {"restore_applied": False, "verified": False},
            "criteria": {"restore_applied": False, "restored": False, "verified": False},
            "f11": {"restore_attempted_count": 0, "restore_set_count": 0, "verified": False},
            "settings": {"mesh_change_option_restored": False, "verified": False},
            "errors": {"operation_error": None, "restore_errors": []},
            "restore_errors": [],
            "restoration_attempted": False,
            "restored": False,
        }
    )
    operation_error: BaseException | None = None
    snapshot: _Snapshot | None = None
    callback_result: Any = None
    mutation_started = False

    try:
        requested_type = _requested_element_type(element_type)
        if not callable(callback):
            raise TypeError("callback must be callable")
        requested_length = _target_length(target_length)
        expected = _expected_hash(expected_snapshot_sha256)
        capture_started_ns = time.monotonic_ns()
        try:
            snapshot = _capture_context(state, base, mesh)
        finally:
            timings["capture"] = _elapsed_ms(capture_started_ns)
        result.update(_summary(snapshot))
        result.update(
            {
                "mpar": {
                    "key_count": len(snapshot.mesh_values),
                    "backup_path": str(snapshot.mesh_path),
                    "restore_applied": False,
                    "verified": False,
                },
                "criteria": {
                    "row_count": len(snapshot.rows),
                    "coverage": _COVERAGE,
                    "backup_path": str(snapshot.quality_path),
                    "restore_applied": False,
                    "restored": False,
                    "verified": False,
                },
                "f11": {
                    "count": _f11_count(snapshot.f11),
                    "restore_attempted_count": 0,
                    "restore_set_count": 0,
                    "restored_count": 0,
                    "restored": False,
                    "verified": False,
                },
                "settings": {
                    "count": len(meshing._SETTINGS),
                    "mesh_change_option_restored": False,
                    "verified": False,
                },
            }
        )
        if expected is not None and expected != snapshot.snapshot_sha256:
            raise ValueError("configuration snapshot changed since the expected SHA-256")

        mutation_started = True
        result["restoration_attempted"] = True
        apply_started_ns = time.monotonic_ns()
        try:
            _apply_temporary_settings(
                state, base, mesh, snapshot, requested_length, requested_type
            )
        finally:
            timings["apply"] = _elapsed_ms(apply_started_ns)
        state.assert_main()
        callback_started_ns = time.monotonic_ns()
        try:
            callback_result = callback()
        finally:
            timings["callback"] = _elapsed_ms(callback_started_ns)
    except BaseException as exc:
        operation_error = exc
        result["errors"]["operation_error"] = _error_record(exc)
    finally:
        try:
            if snapshot is not None and mutation_started:
                restore_started_ns = time.monotonic_ns()
                try:
                    restore_errors = _restore_context(
                        state, base, mesh, snapshot, result
                    )
                finally:
                    timings["restore"] = _elapsed_ms(restore_started_ns)
            else:
                restore_errors = []
                if snapshot is not None:
                    result["restored"] = True
                result["restore_errors"] = []
                result["errors"]["restore_errors"] = []
        finally:
            timings["total"] = _elapsed_ms(total_started_ns)

    if operation_error is not None or restore_errors:
        error = MeshingContextError(operation_error, restore_errors)
        cause = operation_error or (RuntimeError(str(restore_errors[0])) if restore_errors else error)
        timings["total"] = _elapsed_ms(total_started_ns)
        raise error from cause
    timings["total"] = _elapsed_ms(total_started_ns)
    return callback_result
