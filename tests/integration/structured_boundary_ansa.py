#!/usr/bin/env python3
"""真实 stdio MCP 验收：CAD FACE 原生归属与关联节点投影。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(INTEGRATION_DIR)]

import stage_three_ansa as stage
import structured_meshing_ansa as structured_stage


REPORT_NAME = "structured-boundary-report.json"
REPORT_SCHEMA = 1
DECK = 1  # NASTRAN
CAD_TOLERANCE = 0.02
OFFSET_DISTANCE = 0.1
TARGET_LENGTH = 5.0
PROTECTED_USER_PID = 15580


FIXTURE = """
from ansa import base, constants

deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError("structured boundary fixture requires NASTRAN")
if base.CollectEntities(deck, None, "SHELL") or base.CollectEntities(deck, None, "FACE"):
    raise RuntimeError("owned baseline must not already contain SHELL or FACE entities")

def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None:
        raise RuntimeError("CreateEntity failed: " + kind)
    return entity

material = create("MAT1", {"DEFINED": "YES", "E": 210000.0, "NU": 0.3, "RHO": 7.8e-9})
pshell = create("PSHELL", {
    "MID1": material._id, "MID2": material._id, "MID3": material._id, "T": 1.0,
})
part = base.NewPart("structured-cad-boundary-fixture")
if part is None:
    raise RuntimeError("NewPart failed")
if base.SetCurrentEntity(pshell) != 0 or base.SetCurrentEntity(part) != 0:
    raise RuntimeError("could not set fixture PSHELL and Part current")

points = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0]]
curves = [base.CurvesNew([points[i], points[(i + 1) % 4]]) for i in range(4)]
if any(curve is None for curve in curves):
    raise RuntimeError("planar fixture curve creation failed")
created_faces = base.FacesNewPlanar(curves, ret_ents=True)
if not created_faces or len(created_faces) != 1:
    raise RuntimeError("FacesNewPlanar did not create exactly one FACE")
face = created_faces[0]
base.SetEntityCardValues(deck, face, {"PID": pshell._id})
assigned = base.GetEntityCardValues(deck, face, ("PID",))
if assigned.get("PID") != pshell._id:
    raise RuntimeError("fixture FACE did not retain its current PSHELL")

psolid = create("PSOLID", {"MID": material._id})
RESULT = {
    "deck": "NASTRAN",
    "face_ids": [face._id],
    "part_id": part._id,
    "pshell_id": pshell._id,
    "property_id": psolid._id,
    "plane": {"origin": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
}
"""


NATIVE_MEMBERSHIP = """
from ansa import base, mesh

deck = base.CurrentDeck()
face_id = __FACE_ID__
face = base.GetEntity(deck, "FACE", face_id)
if face is None:
    raise RuntimeError("fixture FACE is unavailable")
macro = base.GetFaceMacro(face)
if macro is None:
    RESULT = {
        "face_id": face_id, "macro_id": None, "macro_face_ids": [],
        "macro_shell_ids": [], "shells": [], "node_ids": [],
        "membership_complete": False, "membership_origin": "native_GetMacroShells_single_FACE",
    }
else:
    macro_faces = base.GetFacesOfMacros([macro]) or []
    macro_face_ids = sorted(entity._id for entity in macro_faces)
    shells = mesh.GetMacroShells([macro])
    if type(shells) is int and shells == 0:
        shells = []
    shells = list(shells or [])
    shell_rows = []
    node_ids = set()
    for shell in shells:
        if "type" not in shell.card_fields(deck):
            raise RuntimeError("SHELL type field unavailable")
        shell_type = base.GetEntityCardValues(deck, shell, ("type",))["type"]
        if shell_type == "CTRIA3":
            fields = ("G1", "G2", "G3")
        elif shell_type == "CQUAD4":
            fields = ("G1", "G2", "G3", "G4")
        else:
            raise RuntimeError("unexpected native FACE shell type: " + str(shell_type))
        values = base.GetEntityCardValues(deck, shell, fields)
        conn = [int(values[name]) for name in fields]
        node_ids.update(conn)
        shell_rows.append({"id": shell._id, "type": shell_type, "nodes": conn})
    node_rows = []
    for node_id in sorted(node_ids):
        node = base.GetEntity(deck, "GRID", node_id)
        if node is None:
            raise RuntimeError("native shell references missing GRID " + str(node_id))
        values = base.GetEntityCardValues(deck, node, ("X1", "X2", "X3"))
        node_rows.append({"id": node_id, "xyz": [float(values[name]) for name in ("X1", "X2", "X3")]})
    face_labels = set(face.card_fields(deck))
    declared = len(shells)
    if {"Num.Quads", "Num.Trias"} <= face_labels:
        declared = None
        counts = base.GetEntityCardValues(deck, face, ("Num.Quads", "Num.Trias"))
        q, t = counts["Num.Quads"], counts["Num.Trias"]
        if all(type(value) in (int, float) and value >= 0 and int(value) == value for value in (q, t)):
            declared = int(q) + int(t)
    unique_face_macro = macro_face_ids == [face_id]
    RESULT = {
        "face_id": face_id,
        "macro_id": macro._id,
        "macro_face_ids": macro_face_ids,
        "macro_shell_ids": sorted(shell._id for shell in shells),
        "shells": sorted(shell_rows, key=lambda row: row["id"]),
        "node_ids": sorted(node_ids),
        "nodes": node_rows,
        "declared_shell_count": declared,
        "membership_complete": bool(unique_face_macro and declared is not None and declared == len(shells) and shells),
        "membership_origin": "native_GetMacroShells_single_FACE" if unique_face_macro else "ambiguous_multi_FACE_macro",
    }
"""


MOVE_GRID = """
from ansa import base

deck = base.CurrentDeck()
node_id = __NODE_ID__
offset = __OFFSET__
node = base.GetEntity(deck, "GRID", node_id)
if node is None:
    raise RuntimeError("source GRID is unavailable")
before_values = base.GetEntityCardValues(deck, node, ("X1", "X2", "X3"))
before = [float(before_values[name]) for name in ("X1", "X2", "X3")]
status = base.SetEntityCardValues(deck, node, {"X3": before[2] + offset})
if status != 0:
    raise RuntimeError("SetEntityCardValues failed for source GRID")
after_values = base.GetEntityCardValues(deck, node, ("X1", "X2", "X3"))
after = [float(after_values[name]) for name in ("X1", "X2", "X3")]
RESULT = {"grid_id": node_id, "before": before, "after": after, "offset": offset, "return_code": status}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_report(report_path: Path, report: dict) -> None:
    stage._json_write(report_path, report)


def _create_report_exclusive(report_path: Path, report: dict) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(report_path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _record(report: dict, report_path: Path, name: str, status: str, data: dict) -> None:
    report["cases"].append({"name": name, "status": status, "at": _now(), "data": data})
    report["last_case"] = name
    _write_report(report_path, report)
    print(json.dumps({"case": name, "status": status}, ensure_ascii=False), flush=True)


def _load_owned_run(path: Path) -> tuple[Path, dict]:
    if path.is_symlink():
        raise ValueError("owned-run-dir 不得是符号链接")
    run_dir = path.expanduser().resolve(strict=True)
    verification_root = (ROOT / "runtime" / "verification").resolve()
    if not run_dir.is_dir() or not run_dir.is_relative_to(verification_root):
        raise ValueError("owned-run-dir 必须位于仓库 runtime/verification 下")
    if not run_dir.name.startswith("stage-three-"):
        raise ValueError("owned-run-dir 必须是 stage-three-* 独立运行目录")
    instructions_path = run_dir / "launch_instructions.json"
    if not instructions_path.is_file():
        raise ValueError("owned-run-dir 缺少 launch_instructions.json")
    instructions = json.loads(instructions_path.read_text(encoding="utf-8"))
    if Path(instructions["ansa_root"]).resolve() != Path(
        r"C:\Users\huang\AppData\Local\Apps\BETA_CAE_Systems\ansa_2026.1.0"
    ).resolve():
        raise ValueError("owned-run-dir ANSA 安装路径与本验收目标不符")
    translator = Path(instructions["translator"]).resolve(strict=True)
    if not translator.is_relative_to(run_dir):
        raise ValueError("唯一 translator 不属于当前 owned-run-dir")
    if type(instructions.get("port")) is not int or instructions["port"] <= 0:
        raise ValueError("launch_instructions.json 缺少有效端口")
    if not isinstance(instructions.get("runtime_home"), str) or not instructions["runtime_home"]:
        raise ValueError("launch_instructions.json 缺少 runtime_home")
    return run_dir, instructions


def _schema_properties(tool) -> dict:
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise AssertionError(f"{tool.name} 缺少可读的 MCP inputSchema")
    return schema["properties"]


def _schema_enum_values(schema) -> set:
    if not isinstance(schema, dict):
        return set()
    found = set(schema.get("enum", [])) if isinstance(schema.get("enum"), list) else set()
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []) if isinstance(schema.get(key), list) else []:
            found.update(_schema_enum_values(child))
    return found


def _dict_from_page(rows: list) -> dict:
    return {
        row["key"]: row.get("value")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("key"), str) and "value" in row
    }


async def _page_all(h, report_id: str, section: str, *, limit: int) -> dict:
    offset = 0
    pages = []
    rows = []
    while True:
        page = await h.direct_result("ansa_mesh_evidence_page", {
            "deck": DECK, "report_id": report_id, "section": section,
            "offset": offset, "limit": limit,
        })
        if page.get("section") != section or page.get("offset") != offset:
            raise AssertionError(f"evidence page cursor mismatch for {section}: {page}")
        page_rows = page.get("rows")
        if not isinstance(page_rows, list):
            raise AssertionError(f"evidence page omitted rows for {section}: {page}")
        pages.append(page)
        rows.extend(page_rows)
        next_offset = page.get("next_offset")
        if next_offset is None:
            if page.get("complete") is not True:
                raise AssertionError(f"final evidence page is not complete for {section}: {page}")
            break
        if type(next_offset) is not int or next_offset <= offset:
            raise AssertionError(f"invalid evidence page continuation for {section}: {page}")
        offset = next_offset
    total = pages[0].get("total", 0) if pages else 0
    if len(rows) != total:
        raise AssertionError(f"evidence pagination row count mismatch for {section}: {len(rows)} != {total}")
    return {"section": section, "total": total, "rows": rows, "pages": pages,
            "complete": True, "page_count": len(pages)}


async def _read_boundary_evidence(h, report_id: str) -> dict:
    membership = await _page_all(
        h, report_id, "/sections/cad_boundary/membership", limit=200,
    )
    unassigned = await _page_all(
        h, report_id,
        "/sections/cad_boundary/membership/unassigned_free_facets", limit=50,
    )
    native_faces = await _page_all(
        h, report_id, "/sections/cad_boundary/native_faces", limit=200,
    )
    projection_rows = await _page_all(
        h, report_id, "/sections/cad_boundary/projection/rows", limit=1,
    )
    projection_status = await _page_all(
        h, report_id, "/sections/cad_boundary/projection/status", limit=1,
    )
    coverage_status = await _page_all(
        h, report_id,
        "/sections/cad_boundary/continuous_cad_coverage_status", limit=1,
    )
    return {
        "membership": membership,
        "membership_values": _dict_from_page(membership["rows"]),
        "unassigned_free_facets": unassigned,
        "native_faces": native_faces,
        "projection_rows": projection_rows,
        "projection_status": projection_status,
        "continuous_cad_coverage_status": coverage_status,
    }


def _native_face_for(evidence: dict, face_id: int) -> dict:
    rows = evidence["native_faces"]["rows"]
    matches = [row for row in rows if isinstance(row, dict) and row.get("face_id") == face_id]
    if len(matches) != 1:
        raise AssertionError(f"expected one native FACE evidence row for {face_id}, got {matches}")
    return matches[0]


async def _verify_persistence(h, run_dir: Path, report: dict, report_path: Path, face_id: int) -> None:
    """Save/reopen the owned offset fixture and independently read its CAD association and FE cards."""
    async def read(source):
        return (await h.direct_result("ansa_script_execute", {"script": source, "validate": True}))["result"]

    native_before = await read(_native_source(face_id))
    cards_before = await read(structured_stage.ORACLE)
    path = run_dir / ("cad-boundary-offset-" + uuid.uuid4().hex + ".ansa")
    await h.direct_result("ansa_model_save", {"path": str(path)})
    await h.snapshot_context()
    await h.direct_result("ansa_model_open", {"path": str(path), "preservation": "backup",
        "backup_path": str(run_dir / ("before-offset-reopen-" + uuid.uuid4().hex + ".ansa"))})
    await h.snapshot_context()
    native_after = await read(_native_source(face_id))
    cards_after = await read(structured_stage.ORACLE)
    if native_before != native_after or cards_before != cards_after:
        raise AssertionError("saved/reopened CAD/SHELL membership or independent FE cards changed")
    _record(report, report_path, "save_reopen_CAD_and_FE_readback", "passed", {
        "path": str(path), "native_membership": native_after,
        "independent_FE_card_fingerprint": cards_after["fingerprint"],
        "offset_fixture_only": True,
    })


def _audit_summary(audit: dict) -> dict:
    summary = audit.get("summary")
    if not isinstance(summary, dict):
        raise AssertionError(f"mesh_interface_audit did not return its summary: {audit}")
    for key in ("cad_membership_status", "cad_nodal_projection_status"):
        if not isinstance(summary.get(key), str):
            raise AssertionError(f"mesh_interface_audit summary omitted {key}: {summary}")
    return summary


def _native_shell_ids(native_face: dict) -> list[int]:
    ids = native_face.get("macro_shell_ids")
    return sorted(ids) if isinstance(ids, list) else []


def _native_node_ids(native_face: dict) -> list[int]:
    ids = native_face.get("node_ids")
    if isinstance(ids, list):
        return sorted(ids)
    return sorted({node_id for shell in native_face.get("shells", [])
                   for node_id in shell["nodes"]})


def _native_source(face_id: int) -> str:
    return NATIVE_MEMBERSHIP.replace("__FACE_ID__", str(int(face_id)))


def _move_source(node_id: int) -> str:
    return MOVE_GRID.replace("__NODE_ID__", str(int(node_id))).replace("__OFFSET__", repr(OFFSET_DISTANCE))


def _prepare_owned_target(run_dir: Path, instructions: dict):
    verify_arguments = stage._verify_program_arguments

    def verify_unprotected(basic: dict, translator: Path, port: int) -> None:
        verify_arguments(basic, translator, port)
        if basic["pid"] == PROTECTED_USER_PID:
            raise RuntimeError("refusing to prepare protected user PID 15580")

    stage._verify_program_arguments = verify_unprotected
    try:
        return stage._prepare_owned_ansa(run_dir, instructions)
    finally:
        stage._verify_program_arguments = verify_arguments


def _assert_initial_audit(audit: dict, evidence: dict, fixture: dict, native_readback: dict) -> dict:
    summary = _audit_summary(audit)
    if audit.get("status") == "pass":
        raise AssertionError("partial CAD FACE declaration must not produce an overall pass")
    if summary["cad_membership_status"] != "fail":
        raise AssertionError(f"expected unassigned free facets to fail membership: {summary}")
    if summary["cad_nodal_projection_status"] != "pass":
        raise AssertionError(f"source nodes on the planar FACE should project within tolerance: {summary}")
    membership = evidence["membership_values"]
    if membership.get("status") != "fail":
        raise AssertionError(f"require_all_boundary should report partial membership as fail: {membership}")
    unassigned = evidence["unassigned_free_facets"]["rows"]
    if not unassigned:
        raise AssertionError("the five unselected solid faces must leave unassigned free facets")
    native_face = _native_face_for(evidence, fixture["face_ids"][0])
    expected_shell_ids = sorted(native_readback.get("macro_shell_ids", []))
    if (native_face.get("membership_complete") is not True
            or native_face.get("membership_origin") != "native_GetMacroShells_single_FACE"
            or native_face.get("macro_face_ids") != [fixture["face_ids"][0]]
            or sorted(native_face.get("macro_shell_ids", [])) != expected_shell_ids
            or not expected_shell_ids):
        raise AssertionError({"native_FACE_mapping": native_face, "independent_readback": native_readback})
    if native_readback.get("membership_complete") is not True:
        raise AssertionError(f"independent native FACE/SHELL readback is incomplete: {native_readback}")
    if evidence["projection_status"]["rows"] != ["pass"]:
        raise AssertionError(f"paged projection section did not report pass: {evidence['projection_status']}")
    if evidence["continuous_cad_coverage_status"]["rows"] != ["incomplete"]:
        raise AssertionError("nodal projection must not claim continuous CAD coverage")
    projected_ids = sorted({row.get("node_id") for row in evidence["projection_rows"]["rows"]
                            if isinstance(row, dict) and isinstance(row.get("node_id"), int)})
    if projected_ids != _native_node_ids(native_face):
        raise AssertionError({"projected_node_ids": projected_ids, "native_node_ids": _native_node_ids(native_face)})
    if projected_ids != _native_node_ids(native_readback):
        raise AssertionError({"projected_node_ids": projected_ids, "independent_node_ids": _native_node_ids(native_readback)})
    if evidence["projection_rows"]["page_count"] < 2:
        raise AssertionError("projection evidence did not exercise more than one MCP page")
    return summary


async def _verify(run_dir: Path, instructions: dict, identity: dict, helper_bundle,
                  report: dict, report_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    stderr_path = run_dir / ("structured-boundary-backend-" + uuid.uuid4().hex + ".stderr.log")
    report["stdio_stderr"] = str(stderr_path)
    _write_report(report_path, report)
    with stderr_path.open("w", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run_dir)
                schema = await h.list_schema()
                listed = await client.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                required_tools = {
                    "ansa_mesh_faces", "ansa_mesh_surface", "ansa_mesh_source_audit",
                    "ansa_mesh_sweep", "ansa_mesh_interface_audit", "ansa_mesh_evidence_page",
                    "ansa_script_execute",
                }
                if not required_tools <= set(by_name):
                    raise AssertionError(f"MCP schema is missing required tools: {required_tools - set(by_name)}")
                interface_schema = by_name["ansa_mesh_interface_audit"].inputSchema
                interface_properties = _schema_properties(by_name["ansa_mesh_interface_audit"])
                if "cad_boundary" not in interface_properties:
                    raise AssertionError("real MCP interface schema omitted cad_boundary")
                if "cad_boundary" in interface_schema.get("required", []):
                    raise AssertionError("cad_boundary must remain optional")
                surface_properties = _schema_properties(by_name["ansa_mesh_surface"])
                if "tria" not in _schema_enum_values(surface_properties.get("element_type")):
                    raise AssertionError("ansa_mesh_surface schema does not expose element_type='tria'")
                page_properties = _schema_properties(by_name["ansa_mesh_evidence_page"])
                if not {"section", "offset", "limit"} <= set(page_properties):
                    raise AssertionError("evidence page schema omitted section/offset/limit")
                _record(report, report_path, "real_stdio_schema", "passed", {
                    "tool_count": schema["count"],
                    "cad_boundary_optional": True,
                    "mesh_surface_element_type": sorted(_schema_enum_values(surface_properties["element_type"])),
                    "evidence_page_fields": sorted({"section", "offset", "limit"}),
                })

                snapshot = await stage._bind_for_run(h, identity, *helper_bundle[:2], identity["port"])
                _record(report, report_path, "owned_target_binding", "passed", {
                    "pid": snapshot.get("pid"),
                    "main_thread": snapshot.get("main_thread"),
                    "instance_id": h.target.get("instance_id"),
                    "model": snapshot.get("model"),
                })

                async def script(source: str, *, allow_model_change: bool = False) -> dict:
                    executed = await h.direct_result("ansa_script_execute", {
                        "script": source, "validate": True,
                        "allow_model_change": allow_model_change,
                    })
                    return executed["result"]

                _record(report, report_path, "fixture_create", "started", {
                    "kind": "single planar NASTRAN FACE", "write_ahead": True,
                })
                fixture = await script(FIXTURE, allow_model_change=True)
                _record(report, report_path, "fixture_create", "completed", fixture)
                await h.snapshot_context()

                face_id = fixture["face_ids"][0]
                before_mesh = await h.direct_result("ansa_mesh_faces", {"deck": DECK, "face_ids": [face_id]})
                if not isinstance(before_mesh.get("faces"), list) or len(before_mesh["faces"]) != 1:
                    raise AssertionError(f"ansa_mesh_faces did not return the fixture FACE: {before_mesh}")
                condition = before_mesh["faces"][0].get("condition")
                if not isinstance(condition, dict) or condition.get("expected_shell_ids") != []:
                    raise AssertionError(f"fixture FACE was not confirmed unmeshed: {before_mesh}")
                _record(report, report_path, "unmeshed_face_precondition", "passed", before_mesh)

                _record(report, report_path, "surface_mesh", "started", {
                    "element_type": "tria", "target_length": TARGET_LENGTH,
                    "face_id": face_id, "write_ahead": True,
                })
                surface = await h.direct_result("ansa_mesh_surface", {
                    "deck": DECK, "faces": [condition],
                    "target_length": TARGET_LENGTH, "element_type": "tria",
                })
                created_shell_ids = surface.get("created_shell_ids")
                if (surface.get("ok") is not True or surface.get("scope_verified") is not True or not isinstance(created_shell_ids, list)
                        or not created_shell_ids or len(created_shell_ids) > 200):
                    raise AssertionError(f"ansa_mesh_surface did not verify a small scoped mesh: {surface}")
                _record(report, report_path, "surface_mesh", "completed", surface)
                await h.snapshot_context()

                after_mesh = await h.direct_result("ansa_mesh_faces", {"deck": DECK, "face_ids": [face_id]})
                after_condition = after_mesh["faces"][0].get("condition")
                if not isinstance(after_condition, dict):
                    raise AssertionError(f"post-mesh FACE condition is missing: {after_mesh}")
                source_shell_ids = sorted(after_condition.get("expected_shell_ids", []))
                if not source_shell_ids or source_shell_ids != sorted(created_shell_ids):
                    raise AssertionError({"created_shell_ids": created_shell_ids, "FACE_shell_ids": source_shell_ids})
                _record(report, report_path, "post_mesh_face_membership", "passed", after_mesh)

                native_before = await script(_native_source(face_id))
                if (native_before.get("face_id") != face_id
                        or native_before.get("membership_origin") != "native_GetMacroShells_single_FACE"
                        or sorted(native_before.get("macro_shell_ids", [])) != source_shell_ids
                        or not native_before.get("membership_complete")):
                    raise AssertionError({"ansa_mesh_faces": after_mesh, "native_GetMacroShells": native_before})
                if not native_before.get("node_ids"):
                    raise AssertionError(f"native source FACE has no associated GRID nodes: {native_before}")
                _record(report, report_path, "independent_native_shell_readback", "passed", native_before)

                _record(report, report_path, "source_audit", "started", {"source_shell_ids": source_shell_ids})
                source_audit = await h.direct_result("ansa_mesh_source_audit", {
                    "deck": DECK, "source_ids": source_shell_ids,
                    "direction": [0.0, 0.0, 1.0], "layers": 1,
                    "target_length": TARGET_LENGTH,
                })
                source_hash = source_audit.get("snapshot_sha256")
                if not isinstance(source_hash, str) or len(source_hash) != 64:
                    raise AssertionError(f"source audit did not return a fresh source fingerprint: {source_audit}")
                _record(report, report_path, "source_audit", "completed", source_audit)

                backup_path = run_dir / ("before-cad-boundary-sweep-" + uuid.uuid4().hex + ".ansa")
                _record(report, report_path, "sweep", "started", {
                    "source_shell_ids": source_shell_ids, "direction": [0.0, 0.0, 1.0],
                    "distance": 1.0, "steps": 1, "backup_path": str(backup_path),
                    "write_ahead": True,
                })
                sweep = await h.direct_result("ansa_mesh_sweep", {
                    "deck": DECK, "shell_ids": source_shell_ids,
                    "direction": [0.0, 0.0, 1.0], "distance": 1.0, "steps": 1,
                    "property_id": fixture["property_id"],
                    "expected_source_sha256": source_hash,
                    "backup_path": str(backup_path),
                })
                type_counts = sweep.get("new_mesh", {}).get("verification", {}).get("solid_type_counts", {})
                if (sweep.get("ok") is not True or sweep.get("scope_verified") is not True
                        or not isinstance(type_counts, dict) or not type_counts
                        or set(type_counts) != {"CPENTA"} or sum(type_counts.values()) > 200):
                    raise AssertionError(f"fixture sweep did not create a small CPENTA-only mesh: {sweep}")
                solid_ids = sweep.get("new_solid_ids")
                if not isinstance(solid_ids, list) or not solid_ids:
                    raise AssertionError(f"sweep omitted its created SOLID IDs: {sweep}")
                _record(report, report_path, "sweep", "completed", {
                    **sweep, "cpenta_fixture_only": True,
                    "note": "本小型 fixture 的 CPENTA 数量不代表工业模型网格要求。",
                })
                await h.snapshot_context()

                cad_boundary = {
                    "face_ids": [face_id], "tolerance": CAD_TOLERANCE, "cylinders": [],
                }
                _record(report, report_path, "audit_before_offset", "started", {
                    "entity_ids": solid_ids, "cad_boundary": cad_boundary,
                })
                before_audit = await h.direct_result("ansa_mesh_interface_audit", {
                    "deck": DECK, "entity_ids": solid_ids, "cad_boundary": cad_boundary,
                })
                before_report_id = before_audit.get("report_id")
                if not isinstance(before_report_id, str) or len(before_report_id) != 32:
                    raise AssertionError(f"mesh_interface_audit omitted its evidence report ID: {before_audit}")
                before_evidence = await _read_boundary_evidence(h, before_report_id)
                before_summary = _assert_initial_audit(before_audit, before_evidence, fixture, native_before)
                _record(report, report_path, "audit_before_offset", "completed", {
                    "audit": before_audit, "evidence": before_evidence,
                    "asserted": {
                        "unassigned_free_facets_nonempty": True,
                        "source_face_native_membership_complete": True,
                        "nodal_projection_pass": True,
                        "continuous_cad_coverage_incomplete": True,
                        "overall_status_not_pass": True,
                    },
                })

                moved_node_id = native_before["node_ids"][0]
                before_node = next(row for row in native_before["nodes"] if row["id"] == moved_node_id)
                if abs(before_node["xyz"][2]) > 1e-9:
                    raise AssertionError(f"selected source GRID is not on the known CAD plane: {before_node}")
                _record(report, report_path, "offset_source_grid", "started", {
                    "grid_id": moved_node_id, "offset": OFFSET_DISTANCE, "write_ahead": True,
                })
                moved = await script(_move_source(moved_node_id), allow_model_change=True)
                if (moved.get("grid_id") != moved_node_id
                        or abs(moved["after"][2] - moved["before"][2] - OFFSET_DISTANCE) > 1e-9):
                    raise AssertionError(f"source GRID offset did not read back as requested: {moved}")
                _record(report, report_path, "offset_source_grid", "completed", moved)
                await h.snapshot_context()

                native_after = await script(_native_source(face_id))
                _record(report, report_path, "native_shell_readback_after_offset", "observed", native_after)
                retained_shell_ids = sorted(native_after.get("macro_shell_ids", []))
                membership_retained = retained_shell_ids == source_shell_ids

                _record(report, report_path, "audit_after_offset", "started", {
                    "entity_ids": solid_ids, "cad_boundary": cad_boundary,
                })
                after_audit = await h.direct_result("ansa_mesh_interface_audit", {
                    "deck": DECK, "entity_ids": solid_ids, "cad_boundary": cad_boundary,
                })
                after_report_id = after_audit.get("report_id")
                if not isinstance(after_report_id, str) or len(after_report_id) != 32:
                    raise AssertionError(f"post-offset audit omitted its evidence report ID: {after_audit}")
                after_evidence = await _read_boundary_evidence(h, after_report_id)
                after_summary = _audit_summary(after_audit)
                if after_audit.get("status") == "pass":
                    raise AssertionError("offset or partial CAD coverage must not produce an overall pass")
                if after_evidence["continuous_cad_coverage_status"]["rows"] != ["incomplete"]:
                    raise AssertionError("post-offset audit must keep continuous CAD coverage incomplete")
                if not after_evidence["unassigned_free_facets"]["rows"]:
                    raise AssertionError("the five unselected solid faces must remain explicitly unassigned")

                projection_status_after = after_summary["cad_nodal_projection_status"]
                offset_row = [row for row in after_evidence["projection_rows"]["rows"]
                              if isinstance(row, dict) and row.get("node_id") == moved_node_id]
                if membership_retained:
                    if native_after.get("membership_complete") is not True:
                        raise AssertionError(f"native shell IDs stayed equal but FACE membership is not complete: {native_after}")
                    if projection_status_after != "fail":
                        raise AssertionError(f"retained source membership must expose the known offset as fail: {after_summary}")
                    if len(offset_row) != 1 or offset_row[0].get("status") != "fail":
                        raise AssertionError({"moved_grid": moved, "projection_rows": offset_row})
                    if offset_row[0].get("within_tolerance") is not False:
                        raise AssertionError(f"known offset was filtered or passed tolerance: {offset_row}")
                    if abs(float(offset_row[0].get("distance", -1.0)) - OFFSET_DISTANCE) > 1e-6:
                        raise AssertionError(f"reported projection distance did not match the moved GRID: {offset_row}")
                    before_projected = before_evidence["projection_rows"]["rows"]
                    after_projected_ids = sorted(row["node_id"] for row in after_evidence["projection_rows"]["rows"]
                                                 if isinstance(row, dict) and isinstance(row.get("node_id"), int))
                    before_projected_ids = sorted(row["node_id"] for row in before_projected
                                                  if isinstance(row, dict) and isinstance(row.get("node_id"), int))
                    if after_projected_ids != before_projected_ids:
                        raise AssertionError({"before_node_ids": before_projected_ids,
                                              "after_node_ids": after_projected_ids})
                    offset_observation = "native membership retained; all nodes still measured; moved GRID fails projection"
                else:
                    if projection_status_after == "pass":
                        raise AssertionError("lost native association must not be reported as a projection pass")
                    offset_observation = "native SHELL association changed; actual membership/projection evidence retained without inference"

                _record(report, report_path, "audit_after_offset", "completed", {
                    "audit": after_audit,
                    "evidence": after_evidence,
                    "native_readback": native_after,
                    "membership_retained": membership_retained,
                    "projection_status": projection_status_after,
                    "moved_node_projection_rows": offset_row,
                    "observation": offset_observation,
                })
                await _verify_persistence(h, run_dir, report, report_path, face_id)
                report["acceptance_observations"] = {
                    "tool_count": schema["count"],
                    "fixture_solid_family": "CPENTA",
                    "cpenta_count_is_fixture_only": True,
                    "initial_membership_status": before_summary["cad_membership_status"],
                    "initial_projection_status": before_summary["cad_nodal_projection_status"],
                    "initial_unassigned_free_facet_count": before_evidence["unassigned_free_facets"]["total"],
                    "continuous_cad_coverage_status": "incomplete",
                    "offset_membership_retained": membership_retained,
                    "offset_projection_status": projection_status_after,
                    "offset_observation": offset_observation,
                    "projection_pagination_verified": before_evidence["projection_rows"]["page_count"] > 1,
                    "save_reopen_CAD_and_FE_readback": True,
                }
                report["complete"] = True
                report["owned_ansa_retained"] = True
                report["completed_at"] = _now()
                _write_report(report_path, report)


def _cleanup_owned(run_dir: Path, instructions: dict, report: dict, report_path: Path) -> int:
    if report.get("complete") is not True or not isinstance(report.get("identity"), dict):
        raise RuntimeError("cleanup requires a complete report and its verified owned identity")
    if Path(report.get("run_dir", "")).resolve() != run_dir:
        raise RuntimeError("cleanup report does not belong to this exact owned-run-dir")
    if report["identity"].get("pid") == PROTECTED_USER_PID:
        raise RuntimeError("cleanup refused: PID 15580 is the protected user ANSA instance")
    if report.get("cleanup_attempt_started"):
        raise RuntimeError("a cleanup attempt is already recorded; inspect its result before any retry")
    one, two = stage._import_runtime_helpers()
    import grpc

    original_channel = stage._bounded_grpc(grpc, one.DeadlineInterceptor)
    report["cleanup_attempt_started"] = True
    report["cleanup_started_at"] = _now()
    _write_report(report_path, report)
    try:
        report["cleanup"] = stage._cleanup_owned_ansa(
            report["identity"], instructions, (one, two, original_channel),
        )
        report["cleanup"]["pid_and_full_program_arguments_rechecked"] = True
        report["owned_ansa_retained"] = False
        report["cleanup_completed_at"] = _now()
        _write_report(report_path, report)
        print(json.dumps({"cleanup": report["cleanup"], "report": str(report_path)},
                         ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as exc:
        report["cleanup"] = {"stopped_owned_process": False,
                             "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()}
        _write_report(report_path, report)
        print(json.dumps({"cleanup": report["cleanup"], "report": str(report_path)},
                         ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 1
    finally:
        grpc.insecure_channel = original_channel


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--keep", action="store_true", help="保留自有 ANSA（默认行为）")
    modes.add_argument("--cleanup", action="store_true", help="仅在完成报告后精确核验并停止自有 ANSA")
    parser.add_argument("--owned-run-dir", type=Path, required=True,
                        help="stage_three_ansa.py 生成的 runtime/verification/stage-three-* 目录")
    args = parser.parse_args(argv)

    run_dir = None
    report = None
    report_path = None
    grpc_module = None
    grpc_original_channel = None
    try:
        run_dir, instructions = _load_owned_run(args.owned_run_dir)
        report_path = run_dir / REPORT_NAME
        if args.cleanup:
            if not report_path.is_file():
                raise RuntimeError(f"cleanup report is missing: {report_path}")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            return _cleanup_owned(run_dir, instructions, report, report_path)
        if report_path.exists():
            raise FileExistsError(
                f"{REPORT_NAME} already exists; preserve its cases and do not replay unknown fixture writes"
            )
        if (run_dir / "seed-baseline.ansa").exists() or (run_dir / "bridge-start.marker").exists():
            raise FileExistsError("owned runtime preparation already has artifacts; refusing to repeat it")

        one, two = stage._import_runtime_helpers()
        port = instructions["port"]
        translator = Path(instructions["translator"]).resolve()
        basic = two.rpc(port, one.remote_basic)
        stage._verify_program_arguments(basic, translator, port)
        if basic["pid"] == PROTECTED_USER_PID:
            raise RuntimeError("owned translator resolved to protected user PID 15580; refusing all model operations")
        identity = {
            "pid": basic["pid"],
            "program_args": list(basic["program_args"]),
            "port": port,
            "translator": str(translator),
        }
        from ansa_mcp_shared.identity import process_start_filetime
        identity["process_start"] = process_start_filetime(identity["pid"])
        report = {
            "schema": REPORT_SCHEMA,
            "run_dir": str(run_dir),
            "runtime_home": instructions["runtime_home"],
            "scope": "one owned NASTRAN planar FACE; TRI source sweep to a small CPENTA fixture; no S25 model access",
            "created_at": _now(),
            "identity": identity,
            "cases": [],
            "complete": False,
            "owned_ansa_retained": True,
        }
        _create_report_exclusive(report_path, report)
        _record(report, report_path, "owned_identity_preflight", "passed", {
            "pid": identity["pid"], "port": port, "translator": identity["translator"],
            "main_thread": basic.get("main_thread"),
        })

        import grpc
        grpc_module = grpc
        grpc_original_channel = grpc.insecure_channel
        _record(report, report_path, "owned_runtime_prepare", "started", {
            "write_ahead": True,
            "note": "stage_three helper seeds only this private owned run and starts its bridge; no ANSA process is launched by this entrypoint.",
        })
        try:
            prepared_identity, _ansa, _grpc, helper_bundle = _prepare_owned_target(run_dir, instructions)
        finally:
            grpc.insecure_channel = grpc_original_channel
        if (prepared_identity.get("pid") != identity["pid"]
                or prepared_identity.get("program_args") != identity["program_args"]
                or prepared_identity.get("port") != identity["port"]
                or prepared_identity.get("pid") == PROTECTED_USER_PID):
            raise RuntimeError("owned identity changed during stage_three preparation")
        identity = prepared_identity
        report["identity"] = identity
        _record(report, report_path, "owned_runtime_prepare", "completed", {
            "pid": identity["pid"], "instance_id": identity.get("instance_id"),
            "session_id": identity.get("session_id"), "bridge_port": identity.get("bridge_port"),
        })

        refreshed = two.rpc(port, structured_stage.remote_refresh_structured,
                            identity["pid"], identity["program_args"])
        if refreshed.get("refreshed") is not True:
            raise RuntimeError(f"structured bridge refresh was not confirmed: {refreshed}")
        _record(report, report_path, "structured_native_module_refresh", "passed", refreshed)

        asyncio.run(_verify(run_dir, instructions, identity, helper_bundle, report, report_path))
        print(json.dumps({
            "complete": report["complete"], "report": str(report_path),
            "cases": len(report["cases"]), "owned_ansa_retained": report["owned_ansa_retained"],
            "acceptance_observations": report.get("acceptance_observations"),
        }, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as exc:
        if report is not None and report_path is not None:
            report["fatal_error"] = f"{type(exc).__name__}: {exc}"
            report["traceback"] = traceback.format_exc()
            report["complete"] = False
            try:
                _write_report(report_path, report)
            except Exception:
                pass
        print(json.dumps({
            "complete": False,
            "run_dir": str(run_dir) if run_dir else None,
            "report": str(report_path) if report_path else None,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 1
    finally:
        if grpc_module is not None and grpc_original_channel is not None:
            grpc_module.insecure_channel = grpc_original_channel


if __name__ == "__main__":
    raise SystemExit(main())
