"""Safe model import and export operations for ANSA's owning GUI thread."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
import uuid
from pathlib import Path, PureWindowsPath

from . import _component_has_reparse_point, _model_save, _save_path


_REPARSE_POINT = 0x0400
_CAD_ID_LIMIT = 200
_OUTPUT_APIS = {
    "NASTRAN": "OutputNastran",
    "ABAQUS": "OutputAbaqus",
    "LSDYNA": "OutputLSDyna",
}
_INPUT_APIS = {
    "NASTRAN": "InputNastran",
    "ABAQUS": "InputAbaqus",
    "LSDYNA": "InputLSDyna",
}
_OUTPUT_SUFFIXES = {
    "NASTRAN": {".nas", ".bdf", ".dat"},
    "ABAQUS": {".inp"},
    "LSDYNA": {".key", ".k", ".dyn", ".dat"},
    "STEP": {".step", ".stp"},
}


def _params(value, required, optional=()):
    if type(value) is not dict:
        raise TypeError("params must be an object")
    required = set(required)
    optional = set(optional)
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ValueError("missing parameter(s): " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(extra)))
    return value


def _path_string(value, label):
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a non-empty absolute path string")
    windows_parts = PureWindowsPath(value).parts
    native_parts = Path(value).parts
    if any(part in (".", "..") for part in (*windows_parts, *native_parts)):
        raise ValueError(f"{label} must not contain traversal components")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if _component_has_reparse_point(path):
        raise ValueError(f"{label} must not traverse symlinks or reparse points")
    return path


def _same_path(left, right):
    if not left or not right:
        return left == right
    return os.path.normcase(os.path.normpath(os.path.abspath(str(left)))) == os.path.normcase(
        os.path.normpath(os.path.abspath(str(right)))
    )


def _check_parent(path, label):
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"{label} parent directory must already exist")
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"could not resolve the {label} parent: {exc}") from exc
    if not _same_path(resolved_parent, parent):
        raise ValueError(f"{label} parent resolves outside its specified location")


def _lstat_regular_file(path, label):
    if _component_has_reparse_point(path):
        raise ValueError(f"{label} path traverses a symlink or reparse point")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"{label} file is unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise ValueError(f"{label} must not be a symlink or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")
    return info


def _output_path(value, overwrite, label="path"):
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean")
    target = _path_string(value, label)
    _check_parent(target, label)
    try:
        prior_info = os.lstat(target)
    except FileNotFoundError:
        prior_info = None
    if prior_info is not None:
        if stat.S_ISLNK(prior_info.st_mode) or getattr(prior_info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError(f"{label} must not be a symlink or reparse point")
        if not stat.S_ISREG(prior_info.st_mode):
            raise ValueError(f"existing {label} must be a regular file")
        if not overwrite:
            raise FileExistsError(f"{label} already exists; set overwrite=true to replace it")
    return target, prior_info


def _input_path(value):
    path = _path_string(value, "path")
    info = _lstat_regular_file(path, "source")
    if info.st_size <= 0:
        raise ValueError("source file must not be empty")
    return path


def _directory_path(value):
    path = _path_string(value, "directory")
    if not path.is_dir():
        raise ValueError("directory must already exist")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"could not resolve directory: {exc}") from exc
    if not _same_path(resolved, path):
        raise ValueError("directory resolves outside its specified location")
    return path


def _fingerprint(path, *, allow_empty=False):
    info = _lstat_regular_file(path, "output")
    if not allow_empty and info.st_size <= 0:
        raise RuntimeError("ANSA produced an empty output file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise RuntimeError(f"could not read output file for verification: {exc}") from exc
    after = _lstat_regular_file(path, "output")
    stable_before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    stable_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stable_before != stable_after:
        raise RuntimeError("output file changed while it was being verified")
    return {
        "path": str(path),
        "size_bytes": after.st_size,
        "sha256": digest.hexdigest(),
        "mtime_ns": after.st_mtime_ns,
    }


def _public_file_record(fingerprint):
    return {key: fingerprint[key] for key in ("path", "size_bytes", "sha256")}


def _step_file_is_valid(path):
    with path.open("rb") as stream:
        head = stream.read(65536)
        stream.seek(max(0, path.stat().st_size - 4096))
        tail = stream.read(4096)
    head = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    tail = tail.rstrip()
    return (
        head.startswith(b"ISO-10303-21;")
        and b"HEADER;" in head
        and b"DATA;" in head
        and tail.endswith(b"END-ISO-10303-21;")
    )


def _export_options(path):
    # Set every include/multi-file control explicitly; ANSA otherwise reuses
    # settings from its previous interactive output operation.
    return {
        "filename": str(path),
        "mode": "all",
        "disregard_includes": "on",
        "include_output_mode": "contents",
        "output_only_main_file": "on",
        "output_parts_in_xml": "off",
        "update_include_fname": "off",
        "output_all_same_directory": "off",
        "create_include_output_directory": "off",
        "monolithic_output": "on",
    }


def _model_export(state, base, params):
    params = _params(params, ("format", "path"), ("overwrite", "scope"))
    file_format = params["format"]
    if type(file_format) is not str or file_format not in _OUTPUT_SUFFIXES:
        raise ValueError("format must be NASTRAN, ABAQUS, LSDYNA, or STEP")
    scope = params.get("scope", "all")
    if scope not in ("all", "visible"):
        raise ValueError("scope must be 'all' or 'visible'")
    overwrite = params.get("overwrite", False)
    target, prior_info = _output_path(params["path"], overwrite)
    if target.suffix.casefold() not in _OUTPUT_SUFFIXES[file_format]:
        raise ValueError("output extension does not match the requested format")

    if file_format in _OUTPUT_APIS:
        if scope != "all":
            raise ValueError("solver exports support scope='all' only")
        api_name = _OUTPUT_APIS[file_format]
        api = getattr(base, api_name, None)
        if not callable(api):
            raise RuntimeError(f"documented ANSA API is unavailable: ansa.base.{api_name}")
        before = _fingerprint(target, allow_empty=True) if prior_info is not None else None
        status = api(**_export_options(target))
        if type(status) is not int or status != 1:
            raise RuntimeError(f"{api_name} returned {status!r}; output may be partial")
        after = _fingerprint(target)
        if before is not None and all(after[key] == before[key]
                                      for key in ("size_bytes", "sha256", "mtime_ns")):
            raise RuntimeError(f"{api_name} left the existing target unchanged")
        record = _public_file_record(after)
        return {"format": file_format, "scope": "all", "return_code": status, **record}

    if file_format != "STEP":
        raise ValueError("format must be NASTRAN, ABAQUS, LSDYNA, or STEP")
    if "scope" not in params or scope != "visible":
        raise ValueError("STEP export requires scope='visible' explicitly")
    if target.suffix.casefold() not in (".step", ".stp"):
        raise ValueError("STEP path must use the .step or .stp extension")
    visible_cad = base.CollectEntities(
        base.CurrentDeck(), None, ("FACE", "CONS"), recursive=False,
        filter_visible=True, hidden_entities=False,
    )
    if visible_cad is None or type(visible_cad) not in (list, tuple) or not visible_cad:
        raise ValueError("STEP export requires visible CAD FACE or CONS entities")
    api = getattr(base, "SaveFileAsStep", None)
    if not callable(api):
        raise RuntimeError("documented ANSA API is unavailable: ansa.base.SaveFileAsStep")

    before = _fingerprint(target, allow_empty=True) if prior_info is not None else None
    try:
        status = api(str(target))  # Official API accepts filename only positionally.
    except Exception as exc:
        raise RuntimeError(f"SaveFileAsStep raised; inspect any partial output at {target}: {exc}") from exc
    after = _fingerprint(target)
    if not _step_file_is_valid(target):
        raise RuntimeError("SaveFileAsStep did not produce a valid ISO-10303-21 STEP file")
    changed = before is None or any(
        after[key] != before[key] for key in ("size_bytes", "sha256", "mtime_ns")
    )
    if not changed:
        raise RuntimeError("SaveFileAsStep left the existing target unchanged")
    record = _public_file_record(after)
    return {
        "format": "STEP", "scope": "visible", "return_code": status,
        "return_code_is_diagnostic": False, "verified_format": "ISO-10303-21",
        "native_output_parameters_dialog": True,
        "visible_cad_count": len(visible_cad), **record,
    }


def _import_options(file_format, source):
    options = {
        "filename": str(source),
        "nodes_id": "offset-freeid",
        "elements_id": "offset-freeid",
        "properties_id": "offset-freeid",
        "materials_id": "offset-freeid",
        "sets_id": "offset",
        "coords_id": "offset",
        "merge_sets_by_name": "off",
        "paste_nodes_by_name": "off",
        "paste_nodes_by_id": "off",
        "merge_parts": "off",
        "header": "merge",
        "new_include": "off",
        "model_action": "merge_model",
    }
    if file_format == "NASTRAN":
        options["bc_sets_offset"] = "offset"
    elif file_format == "ABAQUS":
        options["merge_steps_by_name"] = "off"
    elif file_format == "LSDYNA":
        options.update({
            "propsections_id": "offset-freeid",
            "eos_id": "offset",
            "hourglass_id": "offset",
            "functions_id": "offset",
        })
    return options


def _entity_summary(base, deck):
    entities = base.CollectEntities(deck, None, "__ALL_ENTITIES__", False)
    if type(entities) not in (list, tuple):
        raise RuntimeError("CollectEntities(__ALL_ENTITIES__) did not return an entity list")
    return {"deck": deck, "entity_count": len(entities)}


def _error_text(exc):
    return f"{type(exc).__name__}: {exc}"


def _cad_summary(base):
    deck = base.CurrentDeck()
    summary = _entity_summary(base, deck)
    faces = base.CollectEntities(deck, None, "FACE", False)
    if type(faces) not in (list, tuple):
        raise RuntimeError("CollectEntities(FACE) did not return an entity list")
    ids, areas = [], []
    for face in faces:
        if not face.is_usable() or type(face._id) is not int or face._id <= 0:
            raise RuntimeError("CAD readback returned an unusable FACE")
        area = base.GetFaceArea(face)
        if type(area) not in (int, float) or not math.isfinite(area) or area < 0:
            raise RuntimeError("GetFaceArea returned an invalid area")
        ids.append(face._id)
        areas.append(area)
    if len(set(ids)) != len(ids):
        raise RuntimeError("CAD readback returned duplicate FACE IDs")
    total = math.fsum(areas)
    if not math.isfinite(total):
        raise RuntimeError("CAD total area is not finite")
    return {**summary, "current_model_path": base.DataBaseName(),
            "face_count": len(ids), "face_ids": sorted(ids)[:_CAD_ID_LIMIT],
            "face_ids_limit": _CAD_ID_LIMIT, "face_ids_truncated": len(ids) > _CAD_ID_LIMIT,
            "face_area_total": total}


def _import_backup(base, constants, params, source):
    preservation = params.get("preservation", "backup")
    if preservation not in ("backup", "discard"):
        raise ValueError("preservation must be 'backup' or 'discard'")
    if preservation == "discard":
        return None
    if params.get("backup_path") is None:
        raise ValueError("backup_path is required when preservation='backup'")
    backup_value = _path_string(params["backup_path"], "backup_path")
    database = base.DataBaseName()
    if _same_path(backup_value, source) or (type(database) is str and _same_path(backup_value, database)):
        raise ValueError("backup_path must differ from both source and current model paths")
    backup_target, _ = _save_path(str(backup_value), False)
    return _model_save(base, constants, {"path": str(backup_target), "overwrite": False})


def _step_import(state, base, constants, params):
    if params.get("mode", "merge") != "replace":
        raise ValueError("STEP import requires mode='replace' explicitly; CAD merge is not supported")
    source = _input_path(params["path"])
    if source.suffix.casefold() not in _OUTPUT_SUFFIXES["STEP"]:
        raise ValueError("STEP path must use the .step or .stp extension")
    if not _step_file_is_valid(source):
        raise ValueError("source is not an ISO-10303-21 STEP container")
    api = getattr(base, "Open", None)
    if not callable(api) or not callable(getattr(base, "GetFaceArea", None)):
        raise RuntimeError("documented ANSA Open/GetFaceArea APIs are unavailable")
    source_before = _fingerprint(source)
    before = _cad_summary(base)
    backup = _import_backup(base, constants, params, source)
    if backup is not None:
        backup.update(_public_file_record(_fingerprint(Path(backup["path"]))))
    # Backup can be slow. Recheck the source before invalidating model context.
    if _fingerprint(source) != source_before:
        raise ValueError("STEP source changed before import; model was not replaced")

    state.invalidate_model()
    api_result = api_error = None
    try:
        api_result = api(str(source))
    except Exception as exc:
        api_error = _error_text(exc)
    try:
        after = _cad_summary(base)
    except Exception as exc:
        after = {"error": _error_text(exc)}
    try:
        model_after = state.probe()
    except Exception as exc:
        model_after = {"error": _error_text(exc)}
    source_error = None
    try:
        source_unchanged = _fingerprint(source) == source_before
    except Exception as exc:
        source_unchanged = False
        source_error = _error_text(exc)
    api_succeeded = type(api_result) is int and api_result == 0 and api_error is None
    geometry_verified = ("error" not in after and after["face_count"] > 0
                         and after["face_area_total"] > 0)
    path_verified = (type(after.get("current_model_path")) is str
                     and _same_path(after["current_model_path"], source))
    result = {
        "ok": (api_succeeded and geometry_verified and path_verified
               and source_unchanged and "error" not in model_after),
        "format": "STEP", "mode": "replace", "imported": api_succeeded,
        "return_code": api_result if type(api_result) is int else None,
        "before": before, "after": after, "geometry_verified": geometry_verified,
        "current_model_path_verified": path_verified,
        "source": _public_file_record(source_before), "source_unchanged": source_unchanged,
        "effect_assessment": ("geometry_readback_completed" if api_succeeded and geometry_verified
                              else "partial_changes_possible"),
        "backup": backup, "model_after": model_after,
    }
    if api_error is not None:
        result["error"] = api_error
    elif not api_succeeded:
        result["error"] = f"Open returned {api_result!r}; inspect model_after before any retry"
    elif not result["ok"]:
        result["error"] = "STEP Open returned success but geometry, path, source, or model readback was not verified"
    if source_error is not None:
        result["source_error"] = source_error
    return result


def _model_import(state, base, constants, params):
    params = _params(params, ("format", "path"), ("mode", "preservation", "backup_path"))
    file_format = params["format"]
    if file_format == "STEP":
        return _step_import(state, base, constants, params)
    if type(file_format) is not str or file_format not in _INPUT_APIS:
        raise ValueError("format must be NASTRAN, ABAQUS, LSDYNA, or STEP")
    if params.get("mode", "merge") != "merge":
        raise ValueError("solver import supports mode='merge' only")
    source = _input_path(params["path"])
    api_name = _INPUT_APIS[file_format]
    api = getattr(base, api_name, None)
    if not callable(api):
        raise RuntimeError(f"documented ANSA API is unavailable: ansa.base.{api_name}")
    deck_name = {"NASTRAN": "NASTRAN", "ABAQUS": "ABAQUS", "LSDYNA": "LSDYNA"}[file_format]
    deck = getattr(constants, deck_name, None)
    if deck is None:
        raise RuntimeError(f"documented ANSA deck constant is unavailable: ansa.constants.{deck_name}")

    preservation = params.get("preservation", "backup")
    if preservation not in ("backup", "discard"):
        raise ValueError("preservation must be 'backup' or 'discard'")
    backup = None
    if preservation == "backup":
        if "backup_path" not in params or params["backup_path"] is None:
            raise ValueError("backup_path is required when preservation='backup'")
        backup_value = _path_string(params["backup_path"], "backup_path")
        database = base.DataBaseName()
        if _same_path(backup_value, source) or (type(database) is str and _same_path(backup_value, database)):
            raise ValueError("backup_path must differ from both source and current model paths")
        backup_target, _ = _save_path(str(backup_value), False)
        before = _entity_summary(base, deck)
        backup = _model_save(base, constants, {"path": str(backup_target), "overwrite": False})
    else:
        before = _entity_summary(base, deck)

    state.invalidate_model()
    api_result = None
    api_error = None
    try:
        api_result = api(**_import_options(file_format, source))
    except Exception as exc:
        api_error = _error_text(exc)

    try:
        after = _entity_summary(base, deck)
    except Exception as exc:
        after = {"error": _error_text(exc)}
    try:
        model_after = state.probe()
    except Exception as exc:
        model_after = {"error": _error_text(exc)}

    api_succeeded = type(api_result) is int and api_result == 1 and api_error is None
    count_delta = None
    if "entity_count" in before and "entity_count" in after:
        count_delta = after["entity_count"] - before["entity_count"]
    effect_assessment = (
        "partial_changes_possible" if not api_succeeded else
        "entity_summary_readback_completed" if count_delta is not None else
        "entity_summary_readback_unavailable"
    )
    result = {
        "ok": api_succeeded and count_delta is not None and "error" not in model_after,
        "format": file_format,
        "mode": "merge",
        "imported": api_succeeded,
        "return_code": api_result if type(api_result) is int else None,
        "before": before,
        "after": after,
        "entity_count_delta": count_delta,
        "entity_summary_verified": count_delta is not None,
        "effect_assessment": effect_assessment,
        "backup": backup,
        "model_after": model_after,
    }
    if api_error is not None:
        result["error"] = api_error
    elif not api_succeeded:
        result["error"] = f"{api_name} returned {api_result!r}; model effects were independently read back"
    elif not result["ok"]:
        result["error"] = "Import returned success but independent model readback is unavailable"
    return result


def _model_autosave(base, constants, params):
    params = _params(params, ("directory",))
    directory = _directory_path(params["directory"])
    current_before = base.DataBaseName()
    target = None
    for _ in range(32):
        candidate = directory / f"ansa-autosave-{time.time_ns()}-{uuid.uuid4().hex[:12]}.ansa"
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            target = candidate
            break
    if target is None:
        raise RuntimeError("could not allocate a unique autosave filename")
    saved = _model_save(base, constants, {"path": str(target), "overwrite": False})
    current_after = base.DataBaseName()
    if not _same_path(current_before, current_after):
        raise RuntimeError("silent autosave changed the current model path")
    record = _public_file_record(_fingerprint(target))
    return {"saved": True, "current_model_path": current_after, **saved, **record}


def _model_save_current(base, params):
    params = _params(params, ("expected_path", "overwrite"))
    if type(params["overwrite"]) is not bool:
        raise TypeError("overwrite must be a boolean")
    if params["overwrite"] is not True:
        raise ValueError("model_save_current requires overwrite=true")
    target, prior_info = _save_path(params["expected_path"], True)
    if prior_info is None:
        raise FileNotFoundError("expected_path must be an existing .ansa file")
    current_before = base.DataBaseName()
    if type(current_before) is not str or not Path(current_before).is_absolute() or not _same_path(current_before, target):
        raise ValueError("DataBaseName() does not strictly match expected_path")
    status = base.Save()
    if type(status) is not int or status != 0:
        raise RuntimeError(f"Save returned {status!r}")
    current_after = base.DataBaseName()
    if type(current_after) is not str or not _same_path(current_after, target):
        raise RuntimeError("Save completed but the current model path changed")
    record = _public_file_record(_fingerprint(target))
    return {"saved": True, "return_code": status, "current_model_path": current_after, **record}


def execute(state, tool, params):
    """Execute a model-file operation on the owning ANSA main thread."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool not in {"model_export", "model_import", "model_autosave", "model_save_current"}:
        raise ValueError(f"unsupported model-file operation: {tool!r}")

    from ansa import base, constants

    if tool == "model_export":
        return _model_export(state, base, params)
    if tool == "model_import":
        return _model_import(state, base, constants, params)
    if tool == "model_autosave":
        return _model_autosave(base, constants, params)
    return _model_save_current(base, params)
