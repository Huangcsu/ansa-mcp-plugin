"""Small, bounded ANSA operations executed by the owning GUI thread."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath

from . import entities


_REPARSE_POINT = 0x0400


def _params(value, required, optional=()):
    if type(value) is not dict:
        raise TypeError("params must be an object")
    keys = set(value)
    required = set(required)
    optional = set(optional)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ValueError("missing parameter(s): " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(extra)))
    return value


def _component_has_reparse_point(path):
    """Reject symlinks and Windows reparse points in an existing path chain."""
    path = Path(path)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            return True
    return False


def _save_path(value, overwrite):
    if type(value) is not str or not value:
        raise TypeError("path must be a non-empty absolute path string")
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean")

    windows_parts = PureWindowsPath(value).parts
    if any(component in (".", "..") for component in windows_parts):
        raise ValueError("path must not contain traversal components")

    target = Path(value)
    if not target.is_absolute():
        raise ValueError("path must be absolute")
    if target.suffix.casefold() != ".ansa":
        raise ValueError("path must end in .ansa")
    if _component_has_reparse_point(target):
        raise ValueError("path must not traverse symlinks or reparse points")

    parent = target.parent
    if not parent.is_dir():
        raise ValueError("path parent directory must already exist")
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"could not resolve the path parent: {exc}") from exc
    if os.path.normcase(os.path.normpath(str(resolved_parent))) != os.path.normcase(
        os.path.normpath(str(parent))
    ):
        raise ValueError("path parent resolves outside its specified location")

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError("target must not be a symlink or reparse point")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("existing target must be a regular file")
        if not overwrite:
            raise FileExistsError("target already exists; set overwrite=true to replace it")
    return target, info


def _model_save(base, constants, params):
    overwrite = params.get("overwrite", False)
    target, prior_info = _save_path(params["path"], overwrite)

    api_error = None
    status = None
    try:
        # silent=True preserves the current database path while saving this copy.
        status = base.SaveAs(str(target), silent=True)
    except Exception as exc:
        api_error = exc

    if _component_has_reparse_point(target):
        raise RuntimeError("SaveAs target path now traverses a symlink or reparse point") from api_error
    try:
        result_info = os.lstat(target)
    except OSError as exc:
        raise RuntimeError("SaveAs did not create the requested file") from (api_error or exc)
    if stat.S_ISLNK(result_info.st_mode) or getattr(result_info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise RuntimeError("SaveAs target became a symlink or reparse point")
    if not stat.S_ISREG(result_info.st_mode):
        raise RuntimeError("SaveAs target is not a regular file")
    if api_error is not None:
        raise RuntimeError("SaveAs raised even though a file exists at the target") from api_error
    if status != 0:
        raise RuntimeError(f"SaveAs returned {status!r}")

    saved = {"path": str(target), "saved": True}
    if prior_info is not None and result_info.st_mtime_ns <= prior_info.st_mtime_ns:
        saved["warnings"] = ["SaveAs succeeded and the target exists, but its modification time did not advance"]
    return saved


def execute(state, tool, params):
    """Execute one supported operation on ANSA's main thread and return JSON data."""
    state.assert_main()
    from ansa import base, constants

    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool in {"grid_create", "grid_read", "grid_update", "grid_delete"}:
        return entities.execute(base, constants, tool, params)
    if tool == "model_save":
        params = _params(params, ("path",), ("overwrite",))
        return _model_save(base, constants, params)
    if tool in {"model_new", "model_open"}:
        from . import models
        return models.execute(state, tool, params)
    if tool in {"model_export", "model_import", "model_autosave", "model_save_current"}:
        from . import model_files
        return model_files.execute(state, tool, params)
    if tool in {"part_create", "set_members", "set_add"}:
        from . import structures
        return structures.execute(state, tool, params)
    if tool in {"mesh_faces", "mesh_surface", "mesh_delete"}:
        from . import meshing
        return meshing.execute(state, tool, params)
    if tool in {"mesh_settings_read", "mesh_settings_update"}:
        from . import mesh_settings
        return mesh_settings.execute(state, tool, params)
    if tool in {"geometry_select", "geometry_preview"}:
        from . import geometry
        return geometry.execute(state, tool, params)
    if tool == "geometry_delete":
        from . import geometry_delete
        return geometry_delete.execute(state, tool, params)
    if tool in {"script_validate", "script_execute", "api_lookup"}:
        from . import scripts
        return scripts.execute(state, tool, params)
    if tool == "check_run":
        from . import checks
        return checks.execute(state, tool, params)
    if tool == "mesh_check":
        from . import mesh_checks
        return mesh_checks.execute(state, tool, params)
    if tool == "rigid_dependencies":
        from . import rigid_dependencies
        return rigid_dependencies.execute(state, tool, params)
    if tool == "mesh_failed_elements":
        from . import failed_elements
        return failed_elements.execute(state, tool, params)
    if tool == "mesh_intersections":
        from . import intersections
        return intersections.execute(state, tool, params)
    if tool == "mesh_penetrations":
        from . import penetrations
        return penetrations.execute(state, tool, params)
    if tool in {"connection_list", "connection_create"}:
        from . import connections
        return connections.execute(state, tool, params)
    if tool == "connection_check":
        from . import connection_checks
        return connection_checks.execute(state, tool, params)
    if tool == "connection_apply":
        from . import connection_apply
        return connection_apply.execute(state, tool, params)
    if tool == "element_change_type":
        from . import element_types
        return element_types.execute(state, tool, params)
    if tool == "mesh_volume":
        from . import volume_meshing
        return volume_meshing.execute(state, tool, params)
    if tool == "mesh_hex_map":
        from . import hexa_meshing
        return hexa_meshing.execute(state, tool, params)
    if tool in {"kinematics_body_create", "kinematics_joint_create", "kinematics_config_create", "kinematics_inspect"}:
        from . import kinematics
        return kinematics.execute(state, tool, params)
    if tool == "abaqus_contact_inspect":
        from . import abaqus_contact
        return abaqus_contact.execute(state, tool, params)
    if tool == "mesh_workflow":
        from . import mesh_workflow
        return mesh_workflow.execute(state, tool, params)
    if tool in {"quality_report", "quality_page", "quality_locate", "quality_repair"}:
        from . import quality_workflow
        return quality_workflow.execute(state, tool, params)
    if tool == "benchmark_run":
        from . import benchmark
        return benchmark.execute(state, tool, params)
    if tool in {"geometry_inventory", "face_properties", "entity_metrics", "mass_properties", "surface_members", "mesh_quality", "geometry_check", "entity_visibility", "visibility_snapshot"}:
        from . import inspection
        return inspection.execute(state, tool, params)
    if tool.startswith("entity_") or tool in {"model_summary", "model_includes", "node_coordinates"}:
        from . import entity_catalog
        return entity_catalog.execute(state, tool, params)
    raise ValueError(f"unsupported ANSA operation: {tool!r}")


__all__ = ["execute"]
