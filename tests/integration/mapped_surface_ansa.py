"""Owned-instance MCP acceptance: original CAD seeds -> native mapped CQUAD4."""

import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage
from geometry_features_ansa import ORACLE
from cons_seed_ansa import verify_saved_cards


SELECTION = """
from ansa import base
import math
d=base.CurrentDeck();chosen=[]
for face in base.CollectEntities(d,None,'FACE'):
    cons=base.CollectEntities(d,face,'CONS')
    bounds=list(base.BoundBox([face]))
    if len(cons)!=4:continue
    if CASE==0 and (abs(bounds[0]-20.)>1e-7 or abs(bounds[3]-20.)>1e-7):continue
    if CASE==1 and not(abs(bounds[0]-55.601273)<1e-5 and abs(bounds[3]-130.)<1e-7):continue
    adjacent={face._id};assignments=[]
    for edge in cons:
        adjacent.update(e._id for e in base.GetFacesOfCons(edge))
        hp=base.CollectEntities(d,edge,'HOT POINT')
        if len(hp)!=2:raise RuntimeError('source has unexpected closed CONS')
        length=math.dist(hp[0].position,hp[1].position)
        segments=round(length/5.) if CASE==0 else (4 if abs(length-40.)<1e-6 else 8)
        assignments.append({'cons_id':edge._id,'segments':segments})
    chosen.append({'source_face_id':face._id,'face_ids':sorted(adjacent),'bounds':bounds,
                   'assignments':sorted(assignments,key=lambda x:x['cons_id'])})
if len(chosen)!=1:raise RuntimeError('native source FACE binding is ambiguous')
RESULT=chosen[0]
"""


async def verify(run, instructions, identity, helpers, report, source):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    path = run / "mapped-surface-report.json"
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--direct-backend", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    with (run / "mapped-surface.stderr.log").open("w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                report["schema"] = await h.list_schema()
                await stage._bind_for_run(h, identity, *helpers[:2], identity["port"])

                async def call(name, args, expected="succeeded", operation=None):
                    op = operation or "mapped-surface-" + uuid.uuid4().hex
                    report["pending"] = {"tool": name, "operation_id": op}
                    stage._json_write(path, report)
                    start = time.monotonic()
                    submitted, completed = await h.task(name, args, operation_id=op, expected=expected)
                    value = completed["data"]["result"] if completed["status"] == "succeeded" else completed
                    report["calls"].append({"tool": name, "operation_id": op, "task_id": submitted["task_id"],
                        "state": completed["status"], "elapsed_seconds": time.monotonic()-start, "result": value})
                    report["pending"] = None
                    stage._json_write(path, report)
                    return value

                async def script(code):
                    return (await call("ansa_script_execute", {"script": code, "validate": True}))["result"]

                for case in (0, 1):
                    label = "i10-endcap" if case == 0 else "i10-oblique-rectangle"
                    await call("ansa_model_import", {"format": "STEP", "path": str(source), "mode": "replace",
                        "preservation": "backup", "backup_path": str(run / (label + "-before-import.ansa"))})
                    await h.snapshot_context()
                    await call("ansa_model_save", {"path": str(run / (label + "-source.ansa"))})
                    await call("ansa_model_open", {"path": str(run / (label + "-source.ansa")),
                        "preservation": "backup", "backup_path": str(run / (label + "-before-source-reopen.ansa"))})
                    await h.snapshot_context()
                    selected = await script(SELECTION.replace("CASE", str(case)))
                    scope = {"deck": 1, "face_ids": selected["face_ids"],
                             "cons_ids": [v["cons_id"] for v in selected["assignments"]]}
                    original = await script(ORACLE)
                    old = await call("ansa_mesh_seed_read", scope)
                    assert original == await script(ORACLE), "native seed read materialized or changed entities"
                    seeded = await call("ansa_mesh_seed_apply", {"deck": 1, "face_ids": scope["face_ids"],
                        "assignments": selected["assignments"], "expected_seed_snapshot_sha256": old["seed_snapshot_sha256"],
                        "backup_path": str(run / (label + "-before-seeds.ansa"))})
                    assert seeded["ok"] and seeded["scope_verified"]
                    fresh = await call("ansa_mesh_seed_read", scope)
                    faces = await call("ansa_mesh_faces", {"deck": 1, "face_ids": [selected["source_face_id"]]})
                    args = {"deck": 1, "faces": [r["condition"] for r in faces["faces"]],
                        "element_type": "quad", "method": "mapped_existing_seeds", "seed_scope": {
                            "face_ids": scope["face_ids"], "cons_ids": scope["cons_ids"],
                            "expected_seed_snapshot_sha256": old["seed_snapshot_sha256"],
                            "backup_path": str(run / (label + "-stale-must-not-exist.ansa"))}}
                    unchanged = await script(ORACLE)
                    refused = await call("ansa_mesh_surface", args, "failed")
                    assert not Path(args["seed_scope"]["backup_path"]).exists()
                    assert unchanged == await script(ORACLE), "stale-seed refusal changed native model"
                    args["seed_scope"].update(expected_seed_snapshot_sha256=fresh["seed_snapshot_sha256"],
                        backup_path=str(run / (label + "-before-map.ansa")))
                    operation = "mapped-surface-" + uuid.uuid4().hex
                    out = await call("ansa_mesh_surface", args, expected=None, operation=operation)
                    if out.get("status") == "failed":
                        report["diagnosis"] = out
                        raise AssertionError("mapped native operation failed; preserve original task and backup")
                    assert out["ok"] and out["scope_verified"] and out["seeds_preserved"]
                    assert out["parameters_restored"] and out["source_geometry_unchanged"] and out["lattice_verified"]
                    assert out["created_shell_count"] == 32
                    assert out["provenance_stage"] == "verified" and out["provenance_mapping_complete"]
                    after = await script(ORACLE)
                    old_grid = [r for r in unchanged["rows"] if r["type"] == "GRID"]
                    new_grid = {r["id"]: r for r in after["rows"] if r["type"] == "GRID"}
                    assert all(new_grid[r["id"]] == r for r in old_grid), "original GRID changed"
                    replay = await call("ansa_mesh_surface", args, operation=operation)
                    assert replay == out and after == await script(ORACLE), "same operation was replayed as a new write"
                    quality = await call("ansa_mesh_quality_report", {"deck": 1, "entity_ids": out["created_shell_ids"],
                        "profile": {"analysis_type": "linear", "element_family": "shell", "criteria": [
                            {"name": "ASPECT", "max": 5.}, {"name": "JACOB", "min": .7},
                            {"name": "SKEW", "max": 30.}, {"name": "WARP", "max": 5.}]}})
                    assert quality["complete"] and quality["passed"], "scoped native quality did not pass"
                    assert after == await script(ORACLE), "quality read changed model definitions"
                    ledger = await call("ansa_mesh_provenance", {"deck": 1, "action": "read", "ledger_id": out["ledger_id"]})
                    mapping = await call("ansa_mesh_evidence_page", {"deck": 1, "report_id": ledger["report_id"],
                        "section": "/ledger/fe_mapping/new_fe", "offset": 0, "limit": 100})
                    assert len(mapping["rows"]) == 32
                    final = run / (label + "-mapped.ansa")
                    await call("ansa_model_save", {"path": str(final)})
                    saved = await script(ORACLE)
                    await call("ansa_model_open", {"path": str(final), "preservation": "backup",
                        "backup_path": str(run / (label + "-before-final-reopen.ansa"))})
                    await h.snapshot_context()
                    reopened = await script(ORACLE)
                    verification = verify_saved_cards(saved, reopened)
                    native = await call("ansa_mesh_faces", {"deck": 1, "face_ids": [selected["source_face_id"]]})
                    assert native["faces"][0]["shell_ids"] == out["created_shell_ids"]
                    assert not [r for r in reopened["rows"] if r["type"] == "SOLID"]
                    assert report["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
                    report["cases"].append({"name": label, "selection": selected, "seed_apply": seeded,
                        "stale_seed_rejection": refused, "mapping": out, "native_quality": quality,
                        "new_fe_provenance": mapping, "save_reopen": verification, "saved_model": str(final),
                        "source_grid_exactly_preserved": len(old_grid), "final_grid_count": len(new_grid),
                        "solver_grid_and_shell_readback_sha256": reopened["fingerprint"]})
                    stage._json_write(path, report)
                    print(json.dumps({"case": label, "cquad4": 32, "verified": True}), flush=True)


def main():
    if len(sys.argv) != 3:
        raise ValueError("usage: mapped_surface_ansa.py <new-owned-run-dir> <i10o_simp.step>")
    run, source = (Path(p).resolve() for p in sys.argv[1:])
    path = run / "mapped-surface-report.json"
    if path.exists():
        raise FileExistsError("refuse replay of previous native acceptance")
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    files = [Path(__file__).resolve(), *(ROOT / "src" / p for p in (
        "ansa_mcp_bridge/operations/mapped_surface.py", "ansa_mcp_bridge/operations/mapped_surface_topology.py",
        "ansa_mcp_bridge/operations/meshing.py", "ansa_mcp_bridge/operations/cons_seed.py",
        "ansa_mcp_bridge/operations/structured_meshing.py", "ansa_mcp_bridge/operations/structured_native.py",
        "ansa_mcp_bridge/operations/geometry_features.py", "ansa_mcp_bridge/operations/geometry_features_iges.py",
        "ansa_mcp_plugin/mcp/meshing.py", "ansa_mcp_shared/operations.py"))]
    def hashes():
        return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report = {"complete": False, "pending": None, "calls": [], "cases": [], "source_sha256": hashes(),
              "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "input_path": str(source),
              "scope": "two explicitly bound four-sided source FACE; no volume mesh or continuous CAD coverage claim"}
    helpers = grpc = None
    try:
        stage.PANEL_WAIT_SECONDS = 60
        identity, _, grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(path, report)
        asyncio.run(verify(run, instructions, identity, helpers, report, source))
        assert report["source_sha256"] == hashes(), "source changed during native acceptance"
        report["complete"] = True
        report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], flush=True)
    finally:
        stage._json_write(path, report)
        if grpc is not None and helpers is not None:
            grpc.insecure_channel = helpers[2]
    return 0 if report["complete"] and report.get("cleanup", {}).get("stopped_owned_process") else 1


if __name__ == "__main__":
    raise SystemExit(main())
