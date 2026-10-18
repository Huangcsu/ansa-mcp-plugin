"""Explicit preservation before controlled model replacement."""
from pathlib import Path


def execute(state, tool, params):
    state.assert_main()
    from ansa import base, constants, session
    from . import _component_has_reparse_point, _model_save
    path = None
    if tool == "model_open":
        value = params["path"]
        path = Path(value)
        if not path.is_absolute() or path.suffix.casefold() != ".ansa" or not path.is_file():
            raise ValueError("Open requires an existing absolute .ansa file")
        if _component_has_reparse_point(path) or ".." in path.parts:
            raise ValueError("Open refuses reparse or traversal paths")
    preservation = params.get("preservation", "backup")
    backup = None
    if preservation == "backup":
        if not params.get("backup_path"):
            raise ValueError("backup_path is required; default preservation never discards unsaved data")
        if path and Path(params["backup_path"]).resolve() == path.resolve():
            raise ValueError("Backup must not overwrite the file to open")
        backup = _model_save(base, constants, {"path": params["backup_path"], "overwrite": False})
    elif preservation != "discard":
        raise ValueError("preservation must be backup or explicit discard")
    state.invalidate_model()
    if tool == "model_new":
        status = session.New("discard")
        if status != 0:
            raise RuntimeError(f"New returned {status}; inspect model_after before any further operation")
    elif tool == "model_open":
        status = base.Open(str(path))
        if status != 0:
            raise RuntimeError(f"Open returned {status}; inspect model_after before any further operation")
        if Path(base.DataBaseName()).resolve() != path.resolve():
            raise RuntimeError("Open did not select the requested database")
    else:
        raise ValueError("Unsupported model replacement operation")
    return {"action": tool, "path": str(path) if path else None, "return_code": status, "backup": backup}
