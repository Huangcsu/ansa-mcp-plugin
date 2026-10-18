"""查询原锐边任务，在新独立窗口重开已保存夹具并补验画布。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint
from stage_three_sharp_edges_ansa import remote_sharp_edges_readback, verify_result


CORE_CASES = (
    "schemas_scoped_shell_connectivity_angle_fixture_and_backup",
    "folded_flat_threshold_and_explicit_scope_match_independent_angles",
    "hidden_explicit_shells_checked_without_changing_visibility",
    "invalid_scope_ids_angle_options_and_deck_reject_without_changes",
    "original_sharp_edges_operation_idempotency_preserves_cards_and_visibility",
    "saved_model_reopen_preserves_sharp_edges_and_rejects_old_context",
)


def gui_timeout_only(node):
    if type(node) is not dict:
        return False
    children = node.get("exceptions")
    if children is None:
        return (node.get("type") == "TimeoutError"
                and node.get("message") == "Keep original sharp edge task and owned process for GUI inspection")
    return type(children) is list and bool(children) and all(gui_timeout_only(child) for child in children)


def load_source(source_run):
    from ansa_mcp_shared.identity import same_process_alive
    source_run = source_run.resolve()
    if source_run.parent != stage.VERIFICATION_ROOT.resolve():
        raise ValueError("原报告必须来自本项目验收目录")
    source_path = source_run / "sharp-edges-report.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (not source.get("core_acceptance") or source.get("visual_acceptance")
            or tuple(row["name"] for row in source["cases"]) != CORE_CASES
            or not all(row["ok"] for row in source["cases"])):
        raise ValueError("只接受已通过六项核心、尚未通过画布的原报告")
    if source.get("success") or not gui_timeout_only(source.get("exception_tree", {})):
        raise ValueError("原报告须保留画布等待超时，不修改原成功或其他失败证据")
    identity = source["identity"]
    if same_process_alive(identity["pid"], identity["process_start"]):
        raise RuntimeError("原独立窗口仍存活，应先在原窗口补验")
    saved = source["cases"][5]["evidence"]["saved"]
    saved_path = Path(saved["path"]).resolve()
    if saved_path.parent != source_run or saved_path.name != "sharp-edges-after.ansa":
        raise ValueError("原夹具路径与验收目录不符")
    if fingerprint(saved_path) != saved:
        raise RuntimeError("原保存夹具指纹变化，拒绝按旧结果补验")
    instructions = json.loads((source_run / "launch_instructions.json").read_text(encoding="utf-8"))
    return source, instructions, fingerprint(source_path), saved_path


async def verify(run, instructions, identity, helpers, source_run, source, source_instructions, report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    def parameters(home):
        return StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(home)],
            env={**os.environ,"PYTHONPATH":str(stage.PROJECT_ROOT / "src"),"PYTHONDONTWRITEBYTECODE":"1"})
    def record(name, evidence):
        report["cases"].append({"name":name,"ok":True,"evidence":evidence})
        stage._json_write(run / "sharp-edges-followup-report.json", report)
        print(json.dumps({"case":name,"ok":True}),flush=True)
    # Query immutable historical results without binding or resubmitting tasks.
    with (run / "sharp-edges-history.stderr.log").open("w",encoding="utf-8") as log:
        async with stdio_client(parameters(source_instructions["runtime_home"]),errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                original = source["original_check_task"]["completed"]
                queried = await h.call("ansa_task_query",{"task_id":original["task_id"]})
                for key in ("task_id","operation_id","status","target","model"):
                    assert queried[key] == original[key]
                assert queried["status"] == "succeeded" and queried["data"]["result"] == original["data"]["result"]
                record("original_task_query_and_saved_fixture_preserved",{
                    "task":queried,"source_report":str(source_run / "sharp-edges-report.json"),
                    "saved":source["cases"][5]["evidence"]["saved"],"original_process_exited":True})
    def oracle():
        return two.rpc(identity["port"],remote_sharp_edges_readback,identity["pid"],identity["program_args"])
    with (run / "sharp-edges-followup.stderr.log").open("w",encoding="utf-8") as log:
        async with stdio_client(parameters(instructions["runtime_home"]),errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                await stage._bind_for_run(h,identity,one,two,identity["port"])
                schemas = await h.list_schema()
                saved = source["cases"][5]["evidence"]["saved"]
                await h.direct_result("ansa_model_open",{"path":saved["path"],
                    "backup_path":str(run / "owned-seed-before-reopen.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                expected = source["cases"][5]["evidence"]["independent"]
                assert reopened == expected
                fixture = source["cases"][0]["evidence"]["fixture"]["result"]
                ids = fixture["folded_ids"]+fixture["flat_ids"]+fixture["tri_folded_ids"]
                result = await h.direct_result("ansa_mesh_check",{"check":"sharp_edges",
                    "deck":reopened["deck"],"entity_ids":ids,"angle":80.})
                verify_result(result,reopened,ids,80.)
                assert result["failed_entity_ids"] == sorted(fixture["folded_ids"]+fixture["tri_folded_ids"])
                assert oracle() == reopened and fingerprint(Path(saved["path"])) == saved
                record("saved_mixed_shells_reopen_and_scoped_check_match_original_readback",{
                    "schemas":schemas,"independent":reopened,"result":result,"saved":saved,
                    "owned_seed_backup":fingerprint(run / "owned-seed-before-reopen.ansa")})
                report["core_acceptance"] = True
                stage._json_write(run / "sharp-edges-followup-report.json",report)
                marker = run / "sharp-edges-ui-confirmation.json"
                if marker.exists():
                    raise RuntimeError("拒绝已有画布确认记录")
                stage._json_write(run / "sharp-edges-ui-instruction.json",{
                    "identity":identity,"model_path":reopened["model_path"],"visible_ids":reopened["visible_ids"],
                    "instruction":"打开的原混合夹具包含六个四边形和两个三角形壳；GRID 23、ELEMENT 8；核对四组画布及 Quality Monitor。"})
                print(json.dumps({"status":"waiting_for_sharp_edges_followup_ui","run_dir":str(run)}),flush=True)
                deadline = asyncio.get_running_loop().time()+stage.PANEL_WAIT_SECONDS
                while not marker.is_file():
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("补验画布等待超时，保留原任务与所有证据")
                    await asyncio.sleep(.5)
                visual = json.loads(marker.read_text(encoding="utf-8"))
                if (visual.get("checked") is not True or visual.get("fixture_visible") is not True
                        or visual.get("pid") != identity["pid"]
                        or visual.get("process_start") != identity["process_start"]
                        or visual.get("model_path") != reopened["model_path"]):
                    raise RuntimeError("补验画布证据不属于该独立进程和原夹具")
                assert oracle() == reopened and fingerprint(Path(saved["path"])) == saved
                report["visual_acceptance"] = True
                record("new_owned_main_window_and_post_ui_independent_readback",visual)
    report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root",required=True,type=Path)
    parser.add_argument("--runtime-home",required=True,type=Path)
    parser.add_argument("--run-dir",required=True,type=Path)
    parser.add_argument("--source-run-dir",required=True,type=Path)
    args = parser.parse_args()
    sys.path.insert(0,str(stage.PROJECT_ROOT / "src"))
    run,instructions = stage._load_run(args)
    if (run / "seed-baseline.ansa").exists() or (run / "sharp-edges-followup-report.json").exists():
        raise ValueError("补验只接受新准备的独立目录")
    source_run = args.source_run_dir.resolve()
    source,source_instructions,source_fingerprint,saved_path = load_source(source_run)
    report = {"success":False,"core_acceptance":False,"visual_acceptance":False,"cases":[],
        "started_at":datetime.now(timezone.utc).isoformat(),"scope":"原六项锐边核心结果的任务查询、已保存夹具重开及新独立主窗口补验",
        "source_report":source_fingerprint,"source_saved":fingerprint(saved_path)}
    identity = helpers = None
    try:
        identity,_ansa,_grpc,helpers = stage._prepare_owned_ansa(run,instructions)
        report["identity"] = identity
        stage._json_write(run / "sharp-edges-followup-report.json",report)
        asyncio.run(verify(run,instructions,identity,helpers,source_run,source,source_instructions,report))
        if fingerprint(source_run / "sharp-edges-report.json") != source_fingerprint:
            raise RuntimeError("补验期间原核心报告发生变化")
    except BaseException as exc:
        report["success"] = False
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "sharp-edges-followup.error.log").write_text(traceback.format_exc(),encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"]:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity,instructions,helpers)
            except Exception as exc:
                report["success"] = False
                report["cleanup_error"] = str(exc)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        stage._json_write(run / "sharp-edges-followup-report.json",report)
    if not report["success"]:
        raise RuntimeError("锐边补验或独立进程清理失败")
    print(json.dumps({"success":True,"report":str(run / "sharp-edges-followup-report.json")}),flush=True)


if __name__ == "__main__":
    main()
