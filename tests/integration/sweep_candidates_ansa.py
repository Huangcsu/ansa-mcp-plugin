"""Finite sweep discovery through public MCP in a new owned ANSA instance.

Prepare/launch with stage_three_ansa.py first. This entry accepts explicit saved
synthetic CAD and industrial STEP paths. Reports are exclusive: never replay a
partially completed run. On failure, inspect the original pending task first.
"""
import argparse
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests/integration")]
import stage_three_ansa as stage

SNAPSHOT = """
import builtins
from ansa import base
from ansa_mcp_bridge.operations.cad_map import _snapshot
RESULT=_snapshot(builtins._ansa_mcp_plugin_bridge.state,base,base.CurrentDeck())
"""
INFO = """
from ansa import base
d=base.CurrentDeck()
RESULT={'face_ids':sorted(f._id for f in base.CollectEntities(d,None,'FACE')),
        'units':base.GetLengthUnits(),'deck':d,
        'counts':{k:len(base.CollectEntities(d,None,k) or []) for k in ('FACE','CONS','GRID','SHELL','SOLID','VOLUME')}}
"""


async def verify(run, cfg, identity, helpers, report, box, samples):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--direct-backend", "--runtime-home", cfg["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    output = run / "sweep-candidates-report.json"
    with (run / "sweep-candidates.stderr.log").open("w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                report["schema"] = await h.list_schema()
                await stage._bind_for_run(h, identity, *helpers[:2], identity["port"])

                async def call(name, fields, expected="succeeded"):
                    op = "sweep-candidates-" + uuid.uuid4().hex
                    report["pending"] = {"tool": name, "operation_id": op}
                    stage._json_write(output, report)
                    started = time.monotonic()
                    submitted, done = await h.task(name, fields, expected=expected, operation_id=op, timeout_seconds=55)
                    result = done["data"]["result"] if expected == "succeeded" else done
                    report["calls"].append({"tool": name, "operation_id": op, "task_id": submitted["task_id"],
                        "status": done["status"], "elapsed_seconds": time.monotonic() - started, "result": result})
                    report["pending"] = None
                    stage._json_write(output, report)
                    print(json.dumps({"tool": name, "status": done["status"]}), flush=True)
                    return result

                async def script(code):
                    return (await call("ansa_script_execute", {"script": code, "validate": True}))["result"]

                async def page(desc, section, expected="succeeded"):
                    return await call("ansa_mesh_evidence_page", {"deck": 1, "report_id": desc["report_id"], "section": section}, expected)

                async def classify(label, expected_verified=None):
                    info = await script(INFO)
                    if info["units"] != "millimeter" or info["deck"] != 1:
                        raise RuntimeError("acceptance requires saved NASTRAN millimeter model")
                    before = await script(SNAPSHOT)
                    scope = {"deck": 1, "face_ids": info["face_ids"], "target_length": 5.}
                    first = await call("ansa_geometry_sweep_candidates", scope)
                    components = (await page(first, "/classification/components"))["rows"]
                    cached = await call("ansa_geometry_sweep_candidates", scope)
                    if cached["summary"]["cache_status"] != "hit" or cached["summary"]["scoped_export_calls"] != 1:
                        raise AssertionError("cache did not revalidate scoped CAD before proof reuse")
                    after = await script(SNAPSHOT)
                    if before != after:
                        raise AssertionError("read-only candidate classification changed native model")
                    if expected_verified is not None and first["summary"]["verified_export_components"] != expected_verified:
                        raise AssertionError("unexpected number of verified finite domains")
                    row = {"label": label, "native_info": info, "first": first, "cached": cached,
                           "components": components, "full_native_snapshot_exact": True,
                           "native_before": before, "native_after": after}
                    report["cases"].append(row)
                    stage._json_write(output, report)
                    return row

                await call("ansa_model_open", {"path": str(box), "preservation": "backup", "backup_path": str(run / "before-box-open.ansa")})
                await h.snapshot_context()
                box_case = await classify("synthetic-unmeshed-box", 1)
                alternatives = box_case["components"][0]["alternatives"]
                if len(alternatives) != 3:
                    raise AssertionError("box must offer three unordered cap directions")
                report["candidate_handoff"] = [r["map_options"] for r in alternatives]
                partial = await call("ansa_geometry_sweep_candidates", {"deck": 1,
                    "face_ids": box_case["native_info"]["face_ids"][:-1], "target_length": 5.})
                if partial["summary"]["verified_export_components"] != 0:
                    raise AssertionError("partial CAD range was misclassified as closed domain")
                report["partial_box"] = {"descriptor": partial, "components": (await page(partial, "/classification/components"))["rows"]}
                if await script(SNAPSHOT) != box_case["native_after"]:
                    raise AssertionError("partial diagnostic changed model")
                # Create a disjoint native copy in the same Part, after a new backup.
                await call("ansa_model_save", {"path": str(run / "before-box-copy.ansa")})
                report["copy"] = await script("""
from ansa import base
d=base.CurrentDeck();faces=base.CollectEntities(d,None,'FACE')
if len(faces)!=6:raise RuntimeError('six FACE copy fixture required')
rc=base.GeoTranslate('COPY',0,'SAME PART','NONE',50.,0.,0.,faces,draw_results=False,keep_connectivity=False)
if rc!=0:raise RuntimeError('native copy failed')
RESULT={'face_ids':sorted(f._id for f in base.CollectEntities(d,None,'FACE'))}
""")
                await call("ansa_model_save", {"path": str(run / "two-boxes.ansa")})
                await classify("two-disjoint-boxes-same-Part", 2)
                report["outside_scope_copy_page"] = await page(box_case["first"], "/classification/components")
                await call("ansa_model_save", {"path": str(run / "before-source-move.ansa")})
                report["source_move"] = await script("from ansa import base\n"
                    "d=base.CurrentDeck();faces=[base.GetEntity(d,'FACE',i) for i in " + repr(box_case["native_info"]["face_ids"]) + "]\n"
                    "rc=base.GeoTranslate('MOVE',0,'SAME PART','NONE',0.,3.,0.,faces,draw_results=False,keep_connectivity=True)\n"
                    "if rc!=0:raise RuntimeError('owned source move failed')\nRESULT={'moved':True}\n")
                report["same_epoch_stale_page"] = await page(box_case["first"], "/classification/components", "failed")

                for index, source in enumerate(samples):
                    digest = hashlib.sha256(source.read_bytes()).hexdigest()
                    await call("ansa_model_import", {"format": "STEP", "path": str(source), "mode": "replace",
                        "preservation": "backup", "backup_path": str(run / f"before-industrial-{index}.ansa")})
                    await h.snapshot_context()
                    saved = run / (source.stem + "-candidate-input.ansa")
                    await call("ansa_model_save", {"path": str(saved)})
                    await call("ansa_model_open", {"path": str(saved), "preservation": "backup",
                        "backup_path": str(run / f"before-industrial-{index}-reopen.ansa")})
                    await h.snapshot_context()
                    row = await classify(source.name)
                    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                        raise AssertionError("industrial STEP file changed")
                    row["source"] = {"path": str(source), "sha256": digest, "unchanged": True}
                final_path = run / "candidate-final-checkpoint.ansa"
                await call("ansa_model_save", {"path": str(final_path)})
                await call("ansa_model_open", {"path": str(final_path), "preservation": "backup", "backup_path": str(run / "before-final-reopen.ansa")})
                await h.snapshot_context()
                report["reopened"] = await script(SNAPSHOT)
                if report["reopened"] != report["cases"][-1]["native_after"]:
                    raise AssertionError("final model save/reopen snapshot changed")
                await page(report["cases"][-1]["first"], "/classification/components", "failed")
                report["complete"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--box", type=Path, required=True)
    parser.add_argument("--samples", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    output = run / "sweep-candidates-report.json"
    if output.exists():
        raise FileExistsError("report exists; query original tasks instead of replaying")
    box = args.box.resolve(strict=True)
    samples = [p.resolve(strict=True) for p in args.samples]
    cfg = json.loads((run / "launch_instructions.json").read_text("utf-8"))
    report = {"complete": False, "pending": None, "calls": [], "cases": []}
    helpers = None
    try:
        identity, _, _, helpers = stage._prepare_owned_ansa(run, cfg)
        report["identity"] = identity
        stage._json_write(output, report)
        asyncio.run(verify(run, cfg, identity, helpers, report, box, samples))
        report["cleanup"] = stage._cleanup_owned_ansa(identity, cfg, helpers)
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], flush=True)
    finally:
        stage._json_write(output, report)
        if helpers is not None:
            import grpc
            grpc.insecure_channel = helpers[2]
    print(json.dumps({"complete": report["complete"], "report": str(output)}), flush=True)
    return 0 if report["complete"] and report.get("cleanup", {}).get("stopped_owned_process") else 1


if __name__ == "__main__":
    raise SystemExit(main())
