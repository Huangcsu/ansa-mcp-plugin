"""Owned ANSA GUI quality workflow through public MCP, with independent readback.

Requires stage_three launch instructions/identity and quality_templates_ansa
fixtures inside the same run directory. It never starts/stops ANSA or uses a
user model. Native gRPC is used only for fixture setup and independent oracles.
"""

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


def remote_fixture(pid, arguments, filepath, deck):
    import os
    import threading
    from pathlib import Path
    from ansa import base, session
    if (os.getpid() != pid or list(session.ProgramArguments()) != arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("owned fixture identity mismatch")
    base.Open(filepath)
    base.SetCurrentDeck(deck)
    if Path(base.DataBaseName()).resolve() != Path(filepath).resolve():
        raise RuntimeError("owned fixture path mismatch")


def remote_oracle(pid, arguments, params, image_path=None, saved_path=None):
    import os
    import threading
    from pathlib import Path
    from ansa import base, constants, session, utils
    from ansa_mcp_bridge.operations import quality_workflow as quality
    if (os.getpid() != pid or list(session.ProgramArguments()) != arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("owned oracle identity mismatch")
    class State:
        def assert_main(self):
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("not main thread")
    result = quality.execute(State(), "quality_report", params)
    snapshot = quality._fe_snapshot(base, constants, params["deck"])
    visible = sorted(e._id for e in base.CollectEntities(
        params["deck"], None, result["entity_type"], filter_visible=True))
    if saved_path:
        base.SaveAs(saved_path)
        base.Open(saved_path)
        if (quality._fe_snapshot(base, constants, params["deck"]) != snapshot
                or quality.execute(State(), "quality_report", params) != result):
            raise RuntimeError("MCP result save/reopen mismatch")
    if image_path:
        base.RedrawAll()
        base.ZoomAll()
        if utils.SnapShot(image_path, image_format="PNG", image_size=(1200, 800)) != 0 or not Path(image_path).is_file():
            raise RuntimeError("MCP result graphics capture failed")
    return {"report": result, "snapshot": snapshot, "visible_ids": visible}


async def verify(run_dir, fixtures, output, info, identity):
    import stage_one_ansa as one
    import stage_two_ansa as two
    import stage_three_ansa as stage
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from ansa_mcp_shared.operations import model_write_allowed

    # Main has checked PID/translator before initialization or fixture writes.
    stage._initialize_runtime_home(Path(info["runtime_home"]))
    started = two.rpc(info["port"], one.remote_start, str(ROOT), info["runtime_home"], str(output / "bridge.marker"))
    if started["pid"] != identity["pid"] or not started["main_thread"]:
        raise RuntimeError("bridge identity mismatch")
    server = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", info["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    report = {"cases": [], "quality_operations_through_mcp": True, "solver_verified": False}
    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with (output / "backend.stderr.log").open("w", encoding="utf-8") as err:
            async with stdio_client(server, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = stage.McpHarness(client, report, output)
                    report["schema"] = await h.list_schema()
                    for _ in range(80):
                        status = await h.call("ansa_connection_status")
                        if any(i.get("pid") == identity["pid"] and i.get("ready") for i in status["data"]["instances"]):
                            break
                        await asyncio.sleep(.25)
                    else:
                        raise RuntimeError("owned bridge is not connected")
                    two.rpc(info["port"], one.remote_click_bind)
                    await stage._wait_active(h, identity["pid"], one, two, info["port"])

                    async def quality_report(params):
                        value = await h.direct_result("ansa_mesh_quality_report", params)
                        if not value["main_thread"] or value["executor"]["pid"] != identity["pid"]:
                            raise RuntimeError("MCP executor identity mismatch")
                        return {k: v for k, v in value.items() if k not in {"executor", "thread_id", "main_thread", "model_after"}}

                    for template in ("linear_shell", "linear_solid", "explicit_crash_shell", "explicit_crash_solid"):
                        case = json.loads((fixtures / (template + ".json")).read_text(encoding="utf-8"))
                        deck = case["deck"]
                        model_path = str(fixtures / (template + "-before.ansa"))
                        two.rpc(info["port"], remote_fixture, identity["pid"], identity["program_args"], model_path, deck)
                        snapshot = await h.snapshot_context()
                        if not model_write_allowed(snapshot["model"], "quality_repair"):
                            raise RuntimeError("quality repair was not allowed by the current operation gate")
                        profile = {"template": template, "quality_criteria": (fixtures / (template + ".ansa_qual")).read_text(encoding="utf-8")}
                        if template.startswith("explicit_crash"):
                            profile["target_time_step"] = 1.e-10
                        params = {"deck": deck, "entity_ids": case["before"]["entity_ids"], "profile": profile}
                        before = await quality_report(params)
                        oracle = two.rpc(info["port"], remote_oracle, identity["pid"], identity["program_args"], params)
                        if before != oracle["report"]:
                            raise RuntimeError("MCP report independent readback mismatch")
                        evidence = {"template": template, "before": before, "model_gate": snapshot["model"]}
                        if deck == 2:
                            # A model reopened at exactly the same path must invalidate old tasks.
                            old_epoch = h.model["epoch"]
                            two.rpc(info["port"], remote_fixture, identity["pid"], identity["program_args"], model_path, deck)
                            stale = await h.call("ansa_mesh_quality_locate", dict(params,
                                expected_scope_sha256=before["scope_sha256"], operation_id=uuid.uuid4().hex,
                                expected_target=h.target, expected_model=h.model), allow_error=True)
                            if stale.get("task_id") or stale.get("ok") is not False or "stale" not in str(stale).lower():
                                raise RuntimeError("same-path reload accepted stale context")
                            await h.snapshot_context()
                            if h.model["epoch"] <= old_epoch:
                                raise RuntimeError("same-path reload did not advance the model epoch")
                            blocked_path = output / (template + "-must-not-save.ansa")
                            blocked = await h.call("ansa_model_save", dict(path=str(blocked_path), operation_id=uuid.uuid4().hex,
                                expected_target=h.target, expected_model=h.model), allow_error=True)
                            if blocked.get("task_id") or blocked.get("ok") is not False or blocked_path.exists():
                                raise RuntimeError("limited quality gate admitted a general write")
                            evidence.update(stale_context_rejected=True, general_write_rejected=True)
                            before = await quality_report(params)
                        criterion = before["violation_summary"][-1]["criterion"]
                        expected = sorted(row["id"] for row in before["rows"] if criterion in row["violations"])
                        located = await h.direct_result("ansa_mesh_quality_locate", dict(params,
                            expected_scope_sha256=before["scope_sha256"], criterion=criterion))
                        located_oracle = two.rpc(info["port"], remote_oracle, identity["pid"], identity["program_args"], params,
                            str(output / (template + "-located.png")))
                        if (located["located_entity_ids"] != expected or located_oracle["visible_ids"] != expected
                                or located_oracle["snapshot"] != oracle["snapshot"]):
                            raise RuntimeError("MCP criterion visibility or FE preservation mismatch")
                        movable = [14 if template.endswith("solid") else 5]
                        if deck == 2:
                            limited = await h.direct_result("ansa_mesh_quality_repair", dict(params,
                                expected_scope_sha256=before["scope_sha256"], backup_path=str(output / (template + "-limit-backup.ansa")),
                                movable_node_ids=movable, maximum_displacement=1.e-6), expected="failed")
                            operation = limited["data"]["result"].get("operation_result", {})
                            recovered = two.rpc(info["port"], remote_oracle, identity["pid"], identity["program_args"], params)
                            if (not operation.get("coordinates_rolled_back") or operation.get("recovery_required")
                                    or recovered != located_oracle):
                                raise RuntimeError("displacement rejection did not preserve the exact model/display")
                            evidence["displacement_rollback_verified"] = True
                        repairs, current = [], before
                        for index in range(1):
                            await h.snapshot_context()
                            repaired = await h.direct_result("ansa_mesh_quality_repair", dict(params,
                                expected_scope_sha256=current["scope_sha256"], backup_path=str(output / (template + "-backup-" + str(index) + ".ansa")),
                                maximum_displacement=1., max_iterations=4, maximum_total_displacement=1.,
                                **({"movable_node_ids": movable} if template.endswith("solid") else {})))
                            if (not repaired.get("ok") or not repaired.get("scope_verified")
                                    or not repaired.get("criteria_restored") or not repaired.get("options_restored")
                                    or repaired.get("automatic_movable_nodes") != (not template.endswith("solid"))
                                    or repaired.get("maximum_cumulative_displacement", 2.) > 1.):
                                raise RuntimeError("MCP repair failed verification")
                            await h.snapshot_context()
                            current = await quality_report(params)
                            independent = two.rpc(info["port"], remote_oracle, identity["pid"], identity["program_args"], params)
                            if current != repaired["after"] or current != independent["report"]:
                                raise RuntimeError("MCP repair independent readback mismatch")
                            repairs.append(repaired)
                            if current["passed"]:
                                break
                        if not current["passed"]:
                            raise RuntimeError("synthetic fixture still has failures")
                        final = two.rpc(info["port"], remote_oracle, identity["pid"], identity["program_args"], params,
                            str(output / (template + "-after.png")), str(output / (template + "-after.ansa")))
                        if final["report"] != current:
                            raise RuntimeError("saved MCP result differs")
                        evidence.update(criterion=criterion, located_ids=expected, repairs=repairs, after=current,
                                        reopened_verified=True, independent_readback=True)
                        report["cases"].append(evidence)
                        save()
                        print(json.dumps({"template": template, "failed_counts": [before["summary"]["failed_count"]] +
                            [step["failed_after"] for r in repairs for step in r.get("iterations", [])], "reopened_verified": True}), flush=True)
        report["passed"] = len(report["cases"]) == 4
    except BaseException as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve(strict=True)
    fixtures = args.fixtures.resolve(strict=True)
    if not run.is_relative_to(ROOT / "runtime/verification") or not fixtures.is_relative_to(run):
        raise ValueError("acceptance run and synthetic fixtures must stay in the owned verification directory")
    info = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    import grpc
    import stage_one_ansa as one
    import stage_two_ansa as two
    import stage_three_ansa as stage
    stage._bounded_grpc(grpc, one.DeadlineInterceptor)
    basic = two.rpc(info["port"], one.remote_basic)
    stage._verify_program_arguments(basic, Path(info["translator"]), info["port"])
    if any(basic[k] != identity[k] for k in ("pid", "program_args")):
        raise RuntimeError("owned process changed")
    output = run / ("quality-mcp-" + uuid.uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    print(json.dumps({"output": str(output)}), flush=True)
    asyncio.run(verify(run, fixtures, output, info, identity))


if __name__ == "__main__":
    main()
