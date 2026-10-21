"""Temporarily synchronize native Mesh Parameters for a Fix Quality operation.

The supported parameter names below are present in the installed ANSA 2026.1.0
``.ansa_mpar`` templates. The active ANSA serializer remains the source of
truth: this module fails closed if its saved file does not contain the required
keys or if a Save/Read round trip does not preserve the requested values.
Native distance expressions and boundary/freeze settings are preserved verbatim;
the caller must enforce ``maximum_displacement`` by checking final coordinates.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping


_ASSIGNMENT_RE = re.compile(r"^(?P<left>[^#=\r\n]*?)=(?P<right>.*)$")
_FREEZE_KEY_FRAGMENT = ("freeze", "frozen")
_COMMON_REQUIRED_KEYS = frozenset(
    {
        "split_remain_viol_quads_flag",
        "freeze_single_perimeters",
        "freeze_triple_perimeters",
        "freeze_double_perimeters",
        "freeze_line_elements",
        "freeze_named_grids",
        "freeze_washers",
        "frozen_entities_set_id",
    }
)
_REQUIRED_KEYS_BY_FAMILY = {
    "shell": _COMMON_REQUIRED_KEYS
    | frozenset(
        {
            "fix_elements_general_flag",
            "zones_around_violating",
            "maximum_distance_from_surface",
            "maximum_distance_from_single_perimeters",
            "maximum_distance_from_double_perimeters",
            "maximum_distance_from_triple_perimeters",
            "maximum_distortion_at_double_perimeters",
            "maximum_distance_from_perimeter_for_2nd_order_nodes",
            "maximum_distance_from_surface_for_2nd_order_nodes",
        }
    ),
    "solid": _COMMON_REQUIRED_KEYS
    | frozenset(
        {
            "fix_solid_elements_general_flag",
            "freeze_non_visible_shells",
            "unconstrained_2nd_order_nodes",
            "fix_solid_freeze_line_elements",
            "fix_solid_constrained_layer_nodes",
            "number_of_affected_solid_zones",
            "fix_volume_external_bounds",
            "maximum_distance_from_external_bounds",
            "fix_volume_pid_bounds",
            "maximum_distance_from_pid_bounds",
        }
    ),
}


class QualityOptionsError(RuntimeError):
    """Raised when native mesh options cannot be synchronized safely."""


class QualityOptionsTransactionError(QualityOptionsError):
    """Carries the operation and restore errors independently."""

    def __init__(
        self,
        operation_error: BaseException | None,
        restore_error: BaseException,
        recovery_file: str | None,
    ) -> None:
        self.operation_error = operation_error
        self.restore_error = restore_error
        self.recovery_file = recovery_file
        details = []
        if operation_error is not None:
            details.append(f"operation failed: {operation_error!r}")
        details.append(f"Mesh Parameters restore failed: {restore_error!r}")
        if recovery_file:
            details.append(f"original parameters retained at {recovery_file}")
        super().__init__("; ".join(details))


def _mesh_api(base: Any) -> Any:
    """Resolve the mesh namespace, while allowing a small injectable test API."""
    candidates = (getattr(base, "mesh", None), base)
    for candidate in candidates:
        if callable(getattr(candidate, "SaveMeshParams", None)) and callable(
            getattr(candidate, "ReadMeshParams", None)
        ):
            return candidate

    try:
        from ansa import mesh
    except ImportError as exc:
        raise QualityOptionsError(
            "ANSA mesh API is unavailable; expected ansa.mesh.SaveMeshParams and "
            "ansa.mesh.ReadMeshParams"
        ) from exc

    if not callable(getattr(mesh, "SaveMeshParams", None)) or not callable(
        getattr(mesh, "ReadMeshParams", None)
    ):
        raise QualityOptionsError(
            "ANSA 2026.1 mesh API must provide SaveMeshParams and ReadMeshParams"
        )
    return mesh


def _temporary_path() -> str:
    descriptor, filename = tempfile.mkstemp(
        prefix="ansa_quality_options_", suffix=".ansa_mpar"
    )
    os.close(descriptor)
    os.unlink(filename)
    return filename


def _api_success(api: Any, method_name: str, filename: str) -> None:
    status = getattr(api, method_name)(filename)
    if status != 1:
        raise QualityOptionsError(
            f"ansa.mesh.{method_name} failed for {filename!r}: {status!r}"
        )


def _read_parameter_text(filename: str) -> str:
    try:
        return Path(filename).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise QualityOptionsError(
            f"could not read the native Mesh Parameters file {filename!r}: {exc}"
        ) from exc


def _assignment_parts(line: str) -> tuple[str, str, str] | None:
    content = line.rstrip("\r\n")
    match = _ASSIGNMENT_RE.match(content)
    if match is None:
        return None
    key = match.group("left").lstrip("\ufeff").strip()
    if not key:
        return None
    right = match.group("right")
    leading = right[: len(right) - len(right.lstrip())]
    trailing = right[len(right.rstrip()) :]
    return key, leading, trailing


def _parameter_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = _assignment_parts(line)
        if parts is None:
            continue
        key, _, _ = parts
        right = _ASSIGNMENT_RE.match(line.rstrip("\r\n"))
        assert right is not None
        values.setdefault(key, []).append(right.group("right").strip())
    return values


def _patch_parameters(text: str, changes: Mapping[str, str]) -> str:
    found: dict[str, int] = {key: 0 for key in changes}
    patched_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        parts = _assignment_parts(line)
        if parts is None:
            patched_lines.append(line)
            continue
        key, leading, trailing = parts
        if key not in changes:
            patched_lines.append(line)
            continue
        found[key] += 1
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        equals = content.index("=")
        left = content[: equals + 1]
        patched_lines.append(left + leading + changes[key] + trailing + ending)

    missing = [key for key, count in found.items() if count == 0]
    duplicate = [key for key, count in found.items() if count > 1]
    if missing or duplicate:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if duplicate:
            details.append(f"duplicate keys: {', '.join(duplicate)}")
        raise QualityOptionsError(
            "native Mesh Parameters file does not match the verified 2026.1.0 "
            "key contract (" + "; ".join(details) + ")"
        )
    return "".join(patched_lines)


def _normalize_value(value: str) -> str:
    return value.strip()


def _require_snapshot_keys(
    original: Mapping[str, list[str]], family: str
) -> None:
    missing = sorted(key for key in _REQUIRED_KEYS_BY_FAMILY[family] if key not in original)
    duplicated = sorted(
        key for key in _REQUIRED_KEYS_BY_FAMILY[family] if len(original.get(key, ())) != 1
    )
    if missing or duplicated:
        details = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if duplicated:
            details.append("keys must occur exactly once: " + ", ".join(duplicated))
        raise QualityOptionsError(
            "native Mesh Parameters snapshot does not contain the verified "
            "2026.1.0 safety keys (" + "; ".join(details) + ")"
        )


def _same_number(actual: str, expected: str) -> bool:
    try:
        return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
    except ValueError:
        return False


def _validate_applied(
    original: Mapping[str, list[str]],
    actual: Mapping[str, list[str]],
    desired: Mapping[str, str],
) -> None:
    if set(actual) != set(original):
        raise QualityOptionsError(
            "Save/Read verification changed the native Mesh Parameters key set"
        )

    for key, original_values in original.items():
        actual_values = actual[key]
        if len(actual_values) != len(original_values):
            raise QualityOptionsError(
                f"Save/Read verification changed the number of {key!r} values"
            )
        if key in desired:
            if len(actual_values) != 1:
                raise QualityOptionsError(
                    f"expected exactly one native Mesh Parameters value for {key!r}"
                )
            expected = desired[key]
            observed = actual_values[0]
            if expected in ("true", "false", "constrained"):
                matches = observed.casefold() == expected
            elif key.startswith("maximum_distance_"):
                matches = _same_number(observed, expected)
            else:
                matches = _normalize_value(observed).casefold() == expected.casefold()
            if not matches:
                raise QualityOptionsError(
                    f"Mesh Parameters readback mismatch for {key!r}: "
                    f"expected {expected!r}, got {observed!r}"
                )
        elif actual_values != original_values:
            raise QualityOptionsError(
                f"Mesh Parameters readback changed preserved option {key!r}"
            )

        if any(fragment in key.casefold() for fragment in _FREEZE_KEY_FRAGMENT):
            if actual_values != original_values:
                raise QualityOptionsError(
                    f"existing freeze protection changed for {key!r}"
                )


def _validate_restored(
    original: Mapping[str, list[str]], actual: Mapping[str, list[str]]
) -> None:
    if actual != original:
        differing = sorted(
            key
            for key in set(original) | set(actual)
            if original.get(key) != actual.get(key)
        )
        raise QualityOptionsError(
            "Mesh Parameters restore readback differs from the original snapshot: "
            + ", ".join(differing[:12])
        )


def _requested_options(family: str, maximum_displacement: float) -> dict[str, str]:
    if family not in ("shell", "solid"):
        raise ValueError("family must be 'shell' or 'solid'")
    if isinstance(maximum_displacement, bool):
        raise ValueError("maximum_displacement must be a finite non-negative number")
    try:
        displacement = float(maximum_displacement)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "maximum_displacement must be a finite non-negative number"
        ) from exc
    if not math.isfinite(displacement) or displacement < 0:
        raise ValueError("maximum_displacement must be a finite non-negative number")
    options = {"split_remain_viol_quads_flag": "false"}
    if family == "shell":
        options["fix_elements_general_flag"] = "true"
    else:
        options["fix_solid_elements_general_flag"] = "true"
    return options


def _error_record(error: BaseException | None) -> dict[str, str] | None:
    if error is None:
        return None
    return {"type": type(error).__name__, "message": str(error)}


def run_with_options(
    base: Any,
    family: str,
    maximum_displacement: float,
    callback: Callable[[], Any],
    result: MutableMapping[str, Any],
) -> Any:
    """Run ``callback`` with verified temporary Mesh Parameters and restore them.

    ``base`` is the ANSA base namespace. It may also expose the ``mesh``
    namespace (or the two serializer functions directly) for isolated tests.
    ``maximum_displacement`` is expressed in active ANSA length units and is
    validated here; the caller checks final coordinates against that limit.
    """
    result["options_synchronized"] = False
    result["options_restored"] = False
    result["operation_error"] = None
    result["restore_error"] = None
    result["recovery_file"] = None

    operation_error: BaseException | None = None
    operation_traceback = None
    restore_error: BaseException | None = None
    backup_file: str | None = None
    verification_file: str | None = None
    backup_ready = False
    callback_result: Any = None
    original_values: dict[str, list[str]] | None = None

    try:
        desired = _requested_options(family, maximum_displacement)
        mesh = _mesh_api(base)
        backup_file = _temporary_path()
        verification_file = _temporary_path()

        _api_success(mesh, "SaveMeshParams", backup_file)
        if not os.path.isfile(backup_file):
            raise QualityOptionsError(
                "ansa.mesh.SaveMeshParams reported success but created no backup file"
            )
        backup_ready = True

        original_text = _read_parameter_text(backup_file)
        original_values = _parameter_values(original_text)
        _require_snapshot_keys(original_values, family)
        patched_text = _patch_parameters(original_text, desired)
        with open(verification_file, "w", encoding="utf-8", newline="") as stream:
            stream.write(patched_text)

        _api_success(mesh, "ReadMeshParams", verification_file)
        _api_success(mesh, "SaveMeshParams", verification_file)
        applied_values = _parameter_values(_read_parameter_text(verification_file))
        _validate_applied(original_values, applied_values, desired)
        result["options_synchronized"] = True
        callback_result = callback()
    except BaseException as exc:
        operation_error = exc
        operation_traceback = exc.__traceback__
        result["operation_error"] = _error_record(exc)
    finally:
        if backup_ready and backup_file is not None:
            try:
                assert verification_file is not None
                _api_success(mesh, "ReadMeshParams", backup_file)
                _api_success(mesh, "SaveMeshParams", verification_file)
                restored_values = _parameter_values(
                    _read_parameter_text(verification_file)
                )
                if original_values is None:
                    raise QualityOptionsError(
                        "the original Mesh Parameters snapshot could not be parsed; "
                        "restoration was applied but could not be verified"
                    )
                _validate_restored(original_values, restored_values)
                result["options_restored"] = True
            except BaseException as exc:
                restore_error = exc
                result["restore_error"] = _error_record(exc)
                result["recovery_file"] = backup_file

        if verification_file is not None:
            try:
                os.unlink(verification_file)
            except FileNotFoundError:
                pass
        if backup_file is not None and restore_error is None:
            try:
                os.unlink(backup_file)
            except FileNotFoundError:
                pass

    if restore_error is not None:
        raise QualityOptionsTransactionError(
            operation_error, restore_error, backup_file
        ) from (operation_error or restore_error)
    if operation_error is not None:
        raise operation_error.with_traceback(operation_traceback)
    return callback_result
