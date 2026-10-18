"""Read and update shell mesh parameters for the current ANSA session."""

import math
from pathlib import Path
import uuid

from . import _params
from . import meshing


_WRITABLE_SETTINGS = ("element_type", "element_order", "target_length")
_ELEMENT_TYPES = {"tria": "Tria", "quad": "Quad", "mixed": "Mixed"}
_ELEMENT_ORDERS = {"1st Order", "2nd Order"}


def _expected_settings(value):
    _params(value, meshing._SETTINGS)
    for key in meshing._SETTINGS:
        item = value[key]
        if type(item) is not str or not item or len(item) > 128:
            raise ValueError(f"expected_settings.{key} must be a non-empty string of at most 128 characters")
    return value


def _validate_settings(value):
    _params(value, (), _WRITABLE_SETTINGS)
    if not value:
        raise ValueError("settings must contain at least one supported change")

    normalized = {}
    if "element_type" in value:
        element_type = value["element_type"]
        if type(element_type) is not str or element_type not in _ELEMENT_TYPES:
            raise ValueError("element_type must be 'tria', 'quad', or 'mixed'")
        normalized["element_type"] = _ELEMENT_TYPES[element_type]
    if "element_order" in value:
        element_order = value["element_order"]
        if type(element_order) is not str or element_order not in _ELEMENT_ORDERS:
            raise ValueError("element_order must be '1st Order' or '2nd Order'")
        normalized["element_order"] = element_order
    if "target_length" in value:
        target_length = value["target_length"]
        if type(target_length) not in (int, float):
            raise ValueError("target_length must be a finite positive number")
        try:
            target_length = float(target_length)
        except (OverflowError, ValueError):
            raise ValueError("target_length must be a finite positive number") from None
        if not math.isfinite(target_length) or target_length <= 0:
            raise ValueError("target_length must be a finite positive number")
        normalized["target_length"] = target_length
    return normalized


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _same_setting(key, left, right):
    if key == "element_type":
        return left.casefold() == right.casefold()
    if key == "target_element_length":
        left_number = _finite_number(left)
        right_number = _finite_number(right)
        if left_number is not None and right_number is not None:
            return math.isclose(left_number, right_number, rel_tol=1e-9)
    return left == right


def _settings_changed(before, after):
    return [key for key in meshing._SETTINGS if not _same_setting(key, before[key], after[key])]


def _update_readback_errors(before, after, changes):
    errors = []
    if after["mesh_change_option"] != before["mesh_change_option"]:
        errors.append("mesh_change_option changed during the update")

    selected = {"mesh_change_option"}
    if "element_type" in changes:
        selected.add("element_type")
        if after["element_type"].casefold() != changes["element_type"].casefold():
            errors.append("element_type readback did not match the requested value")
    if "element_order" in changes:
        selected.add("element_order")
        if after["element_order"] != changes["element_order"]:
            errors.append("element_order readback did not match the requested value")
    if "target_length" in changes:
        selected.add("target_element_length")
        actual = _finite_number(after["target_element_length"])
        if actual is None or not math.isclose(actual, changes["target_length"], rel_tol=1e-9):
            errors.append("target_element_length readback did not match the requested target_length")

    for key in meshing._SETTINGS:
        if key not in selected and not _same_setting(key, before[key], after[key]):
            errors.append(f"unspecified mesh setting {key} changed during the update")
    return errors


def _reported_code(value):
    return value if type(value) is int else repr(value)


def _update(state, base, mesh, params):
    params = _params(params, ("expected_settings", "settings"))
    expected = _expected_settings(params["expected_settings"])
    changes = _validate_settings(params["settings"])

    before = meshing._settings(base)
    if any(expected[key] != before[key] for key in meshing._SETTINGS):
        raise RuntimeError("mesh settings changed since they were read")

    backup = Path(state.session_dir) / ("mesh-settings-" + uuid.uuid4().hex + ".ansa_mpar")
    saved = mesh.SaveMeshParams(str(backup))
    if type(saved) is not int or saved != 1 or not backup.is_file() or backup.stat().st_size == 0:
        raise RuntimeError("SaveMeshParams did not produce a non-empty parameter backup; settings were not updated")

    result = {
        "ok": False,
        "settings_before": before,
        "parameter_backup": str(backup),
        "defaults_file_written": False,
    }
    errors = []

    native_settings = {key: changes[key] for key in ("element_type", "element_order") if key in changes}
    try:
        if native_settings:
            status = base.BCSettingsSetValues(native_settings)
            result["settings_set_code"] = _reported_code(status)
            if type(status) is not int or status != 0:
                raise RuntimeError(f"BCSettingsSetValues returned {status!r}")
        if "target_length" in changes:
            status = mesh.SetMeshParamTargetLength("absolute", changes["target_length"])
            result["target_length_set_code"] = _reported_code(status)
            if type(status) is not int or status != 1:
                raise RuntimeError(f"SetMeshParamTargetLength returned {status!r}")
    except Exception as exc:
        errors.append(meshing._error(exc))

    after_update = None
    try:
        after_update = meshing._settings(base)
        result["settings_after_update"] = after_update
        errors.extend(_update_readback_errors(before, after_update, changes))
        result["changed"] = _settings_changed(before, after_update)
    except Exception as exc:
        result["settings_after_update_error"] = meshing._error(exc)
        errors.append("settings readback: " + meshing._error(exc))

    if not errors:
        result.update({
            "ok": True,
            "settings_after": after_update,
            "changed": _settings_changed(before, after_update),
            "persisted_in_session": True,
        })
        return result

    restore_errors = []
    restore_file_code = None
    restore_mode_code = None
    try:
        restore_file_code = mesh.ReadMeshParams(str(backup))
        result["restore_file_code"] = _reported_code(restore_file_code)
        if type(restore_file_code) is not int or restore_file_code != 1:
            restore_errors.append(f"ReadMeshParams returned {restore_file_code!r}")
    except Exception as exc:
        result["restore_file_error"] = meshing._error(exc)
        restore_errors.append("ReadMeshParams: " + meshing._error(exc))

    try:
        restore_mode_code = base.BCSettingsSetValues({"mesh_change_option": before["mesh_change_option"]})
        result["restore_mode_code"] = _reported_code(restore_mode_code)
        if type(restore_mode_code) is not int or restore_mode_code != 0:
            restore_errors.append(f"mesh_change_option restoration returned {restore_mode_code!r}")
    except Exception as exc:
        result["restore_mode_error"] = meshing._error(exc)
        restore_errors.append("mesh_change_option restoration: " + meshing._error(exc))

    after_restore = None
    try:
        after_restore = meshing._settings(base)
        result["settings_after"] = after_restore
    except Exception as exc:
        result["settings_after_error"] = meshing._error(exc)
        restore_errors.append("final settings readback: " + meshing._error(exc))

    result["parameters_restored"] = (
        type(restore_file_code) is int and restore_file_code == 1
        and type(restore_mode_code) is int and restore_mode_code == 0
        and after_restore is not None
        and all(_same_setting(key, before[key], after_restore[key]) for key in meshing._SETTINGS)
    )
    if not result["parameters_restored"]:
        restore_errors.append("mesh parameter restoration was not confirmed; inspect parameter_backup")
    result.update({
        "ok": False,
        "error": "; ".join(errors + restore_errors),
        "possible_effects": True,
    })
    return result


def execute(state, tool, params):
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool not in {"mesh_settings_read", "mesh_settings_update"}:
        raise ValueError(f"unsupported mesh settings operation: {tool!r}")

    from ansa import base, mesh

    if tool == "mesh_settings_read":
        _params(params, ())
        settings = meshing._settings(base)
        return {"read_only": True, "settings": settings, "condition": dict(settings)}
    return _update(state, base, mesh, params)
