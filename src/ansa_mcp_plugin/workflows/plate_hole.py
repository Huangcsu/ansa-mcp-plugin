"""Parameterised recipes executed through the existing public MCP script tool."""
from __future__ import annotations

import json
from pathlib import Path
import re

from ansa_mcp_shared.plate_hole import build_plan


def plate_hole_script(action, params, *, backup_path=None, output_path=None, expected_plan_sha256=None):
    """Build a fixed native workflow script; submit with target/model bindings.

    This does not send a task, open ANSA, or make an uncertain write repeatable.
    A plan binds the current saved input; an apply uses that fresh plan digest.
    Submit apply with allow_model_change=True for its saved checkpoint reopens.
    """
    recipe = build_plan(params)
    if action not in ("plan", "apply"):
        raise ValueError("action must be plan or apply")
    if action == "plan":
        if any(v is not None for v in (backup_path, output_path, expected_plan_sha256)):
            raise ValueError("plan cannot contain apply fields")
    else:
        if type(expected_plan_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
            raise ValueError("apply requires the current model plan SHA-256")
        for path in (backup_path, output_path):
            if type(path) is not str or not Path(path).is_absolute() or Path(path).suffix.lower() != ".ansa":
                raise ValueError("backup/output must be absolute .ansa paths")
        if Path(backup_path) == Path(output_path):
            raise ValueError("backup and output paths must differ")
    code = ("import builtins,json\nfrom ansa import base,mesh,constants\n"
        "from ansa_mcp_bridge.operations import plate_hole_workflow as workflow\n"
        "state=builtins._ansa_mcp_plugin_bridge.state\n"
        "params=json.loads(" + repr(json.dumps(recipe["params"], ensure_ascii=True, allow_nan=False)) + ")\n")
    if action == "plan":
        return code + "RESULT=workflow.preflight(state,base,mesh,constants,params)\n"
    return code + ("RESULT=workflow.execute(state,base,mesh,constants,params,backup_path=" + repr(backup_path)
        + ",output_path=" + repr(output_path) + ",expected_plan_sha256=" + repr(expected_plan_sha256) + ")\n")
