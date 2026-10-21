"""Owned public MCP inspection over 2000 elements; full coverage and pages."""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]


def remote_seed(pid, arguments, directory, criteria):
    import importlib
    import os
    import threading
    from pathlib import Path
    from ansa import base, constants, session
    if os.getpid() != pid or list(session.ProgramArguments()) != arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError("owned large fixture identity mismatch")
    from ansa_mcp_bridge.operations import quality_reports
    importlib.reload(quality_reports)
    session.New("discard")
    base.SetCurrentDeck(constants.ABAQUS)
    n = 51
    lines = ["*HEADING", "large quality synthetic fixture", "*NODE"]
    for y in range(n + 1):
        for x in range(n + 1):
            lines.append(f"{1 + x + y * (n + 1)},{float(x)},{float(y)},0.")
    lines.append("*ELEMENT, TYPE=S4R, ELSET=E")
    for y in range(n):
        for x in range(n):
            a = 1 + x + y * (n + 1)
            lines.append(f"{1 + x + y * n},{a},{a+1},{a+n+2},{a+n+1}")
    lines += ["*MATERIAL, NAME=M", "*ELASTIC", "210000.,0.3", "*DENSITY", "7.8e-9", "*SHELL SECTION, ELSET=E, MATERIAL=M", "1.0"]
    path = Path(directory) / "large.inp"
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    if base.InputAbaqus(str(path), model_action="overwrite_model", nodes_id="nooffset", elements_id="nooffset") != 1:
        raise RuntimeError("large fixture input failed")
    base.SaveAs(str(Path(directory) / "large-before.ansa"))
    if len(base.CollectEntities(constants.ABAQUS, None, "SHELL")) != n * n:
        raise RuntimeError("large fixture count mismatch")
    return {"deck": constants.ABAQUS, "count": n * n,
            "profile": {"template": "linear_shell", "quality_criteria": criteria}}


def remote_evidence(pid, arguments):
    import os
    import threading
    from ansa import base, constants, session
    from ansa_mcp_bridge.operations import quality_workflow as quality
    if os.getpid() != pid or list(session.ProgramArguments()) != arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError("owned large evidence identity mismatch")
    return {"scope": quality._scope(base, constants, base.CurrentDeck(), "shell", list(range(1, 2602)))[3],
            "criteria": quality._criteria(base)}


async def verify(run, fixtures, identity, info, output):
    import grpc
    import stage_one_ansa as one, stage_two_ansa as two, stage_three_ansa as stage
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    stage._initialize_runtime_home(Path(info["runtime_home"]))
    two.rpc(info["port"], one.remote_start, str(ROOT), info["runtime_home"], str(output / "bridge.marker"))
    criteria = (fixtures / "linear_shell.ansa_qual").read_text(encoding="utf-8")
    fixture = two.rpc(info["port"], remote_seed, identity["pid"], identity["program_args"], str(output), criteria)
    evidence = two.rpc(info["port"], remote_evidence, identity["pid"], identity["program_args"])
    report = {"count": fixture["count"], "solver_verified": False}
    server = StdioServerParameters(command=sys.executable, args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", info["runtime_home"]],
                                   env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        with (output / "backend.stderr.log").open("w", encoding="utf-8") as err:
            async with stdio_client(server, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=90)) as client:
                    await client.initialize()
                    harness = stage.McpHarness(client, report, output)
                    report["schema"] = await harness.list_schema()
                    for _ in range(80):
                        status = await harness.call("ansa_connection_status")
                        if any(i.get("pid") == identity["pid"] and i.get("ready") for i in status["data"]["instances"]):
                            break
                        await asyncio.sleep(.25)
                    two.rpc(info["port"], one.remote_click_bind)
                    await stage._wait_active(harness, identity["pid"], one, two, info["port"])
                    await harness.snapshot_context()
                    params = {"deck": fixture["deck"], "profile": fixture["profile"], "scope": "all", "batch_size": 500, "max_seconds": 300}
                    value = await harness.direct_result("ansa_mesh_quality_report", params)
                    if not value["complete"] or value["summary"]["checked_count"] != fixture["count"]:
                        raise RuntimeError("large inspection did not cover every requested shell")
                    ids, offset, page_count = [], 0, 0
                    while offset is not None:
                        page = await harness.direct_result("ansa_mesh_quality_page", {"deck": fixture["deck"], "report_id": value["report_id"], "status": "all", "offset": offset, "limit": 200})
                        ids.extend(row["id"] for row in page["rows"])
                        page_count += 1
                        if any(row["element_type"] != "QUAD4" or not row.get("quality_details") for row in page["rows"]):
                            raise RuntimeError("large quality page lacks mesh type or values")
                        offset = page["next_offset"]
                    if ids != list(range(1, fixture["count"] + 1)):
                        raise RuntimeError("quality pages lost or repeated element IDs")
                    after = two.rpc(info["port"], remote_evidence, identity["pid"], identity["program_args"])
                    if evidence != after:
                        raise RuntimeError("large read-only inspection changed the model or criteria")
                    report.update(report=value, page_count=page_count,
                                  paged_ids_verified=True, unchanged_mesh_and_criteria=True, passed=True)
                    print(json.dumps({"count": fixture["count"], "checked": value["summary"]["checked_count"], "batch_count": value["batch_count"], "elapsed_seconds": value["elapsed_seconds"], "passed": True}), flush=True)
    except BaseException as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve(strict=True)
    fixtures = args.fixtures.resolve(strict=True)
    if not run.is_relative_to(ROOT / "runtime" / "verification") or not fixtures.is_relative_to(run):
        raise ValueError("large acceptance must use an owned verification directory")
    identity = json.loads((run / "identity.json").read_text(encoding="utf-8"))
    info = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    import grpc
    import stage_one_ansa as one, stage_two_ansa as two, stage_three_ansa as stage
    original = grpc.insecure_channel
    def bounded(target, options=None, **kwargs):
        return grpc.intercept_channel(original(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs), one.DeadlineInterceptor(90))
    grpc.insecure_channel = bounded
    basic = two.rpc(info["port"], one.remote_basic)
    stage._verify_program_arguments(basic, Path(info["translator"]), info["port"])
    if any(basic[k] != identity[k] for k in ("pid", "program_args")):
        raise RuntimeError("owned large process identity mismatch")
    output = run / ("quality-large-" + uuid.uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    asyncio.run(verify(run, fixtures, identity, info, output))


if __name__ == "__main__":
    main()
