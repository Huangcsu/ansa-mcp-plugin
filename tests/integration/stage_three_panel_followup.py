"""独立 ANSA 面板的长任务和模态文件框补验；复用已有验收实例。"""

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as core


async def wait_operator(run, stage):
    marker = run / ("followup-" + stage + ".json")
    marker.unlink(missing_ok=True)
    core._json_write(run / "ui_followup_stage.json", {"stage": stage, "marker": str(marker)})
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if marker.is_file():
            evidence = json.loads(marker.read_text(encoding="utf-8"))
            if evidence.get("native_ui_observed") is not True:
                raise AssertionError("操作确认缺少原生界面观察")
            return evidence
        await asyncio.sleep(0.25)
    raise TimeoutError("等待原生界面操作超时：" + stage)


async def run_followup(run):
    import grpc
    identity = json.loads((run / "report.json").read_text(encoding="utf-8"))["identity"]
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    stage_one, stage_two = core._import_runtime_helpers()
    original = core._bounded_grpc(grpc, stage_one.DeadlineInterceptor)
    basic = stage_two.rpc(identity["port"], stage_one.remote_basic)
    core._verify_program_arguments(basic, Path(identity["translator"]), identity["port"])
    if basic["pid"] != identity["pid"]:
        raise RuntimeError("测试进程身份已变化")
    report = {"scope": "panel_long_task_and_modal", "success": False, "cases": [], "identity": identity}
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(core.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        with (run / "followup-backend.stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = core.McpHarness(client, report, run)
                    baseline = await core._bind_for_run(h, identity, stage_one, stage_two, identity["port"])
                    session_id, timer_id = baseline["ui"]["session_id"], baseline["ui"]["timer_id"]
                    if baseline["ui"]["alive"]:
                        raise RuntimeError("补验开始前请关闭面板")
                    await h.call("ansa_panel_show", {"expected_target": h.target})
                    before, before_ui = await core._wait_ui_snapshot(h,
                        lambda snapshot, ui: ui["alive"] and ui["visible"] and ui["paint_count"] >= 3,
                        "面板补验显示")
                    op = "panel-long-task-" + uuid.uuid4().hex
                    submitted = await h.call("ansa_script_execute", {
                        "operation_id": op, "expected_target": h.target, "expected_model": h.model,
                        "script": "import time, threading\ntime.sleep(6)\nRESULT = {'long_task': True, 'main_thread': threading.current_thread() is threading.main_thread()}\n",
                        "validate": True})
                    task_id = submitted["task_id"]
                    saw_running = False
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        result = await h.call("ansa_task_query", {"task_id": task_id})
                        saw_running |= result["status"] == "running"
                        if result["status"] in core.TERMINAL_TASK_STATES:
                            break
                        await asyncio.sleep(0.2)
                    else:
                        raise TimeoutError("长任务结果不明；保留原任务，不重放")
                    if result["status"] != "succeeded" or not saw_running:
                        raise AssertionError("长任务未观察到 running → succeeded")
                    if result["data"]["result"]["result"] != {"long_task": True, "main_thread": True}:
                        raise AssertionError("长任务实际结果不符")
                    after, after_ui = await core._wait_ui_snapshot(h,
                        lambda snapshot, ui: ui["alive"] and ui["visible"] and ui["paint_count"] > before_ui["paint_count"],
                        "长任务后面板继续刷新")
                    core._assert_ui_identity(after_ui, session_id, timer_id, "长任务")
                    report["cases"].append({"name": "long_task_then_panel_refresh", "ok": True,
                        "operation_id": op, "task_id": task_id, "observed_running": saw_running,
                        "paint_before": before_ui["paint_count"], "paint_after": after_ui["paint_count"]})
                    core._json_write(run / "panel-followup-report.json", report)
                    opened = await wait_operator(run, "open_file_dialog")
                    modal_before = await h.snapshot_context()
                    modal_ui = core._snapshot_ui(modal_before, "文件框打开")
                    await asyncio.sleep(3)
                    modal_after = await h.snapshot_context()
                    active_ui = core._snapshot_ui(modal_after, "文件框保持打开")
                    report["modal_ui"] = {"before": modal_ui, "after": active_ui}
                    core._assert_ui_identity(active_ui, session_id, timer_id, "模态确认框")
                    # ANSA 的 Open 在保存确认前会清理脚本窗口。窗口可保持刷新，
                    # 也可由 ANSA 正常收尾；控件错误不能伪装成正常关闭。
                    refreshed = (active_ui["alive"] and active_ui["visible"]
                                 and active_ui["paint_count"] > modal_ui["paint_count"])
                    closed_by_ansa = (not active_ui["alive"]
                                      and active_ui["close_reason"] in {"reject", "returned", "destroyed"})
                    if not (refreshed or closed_by_ansa) or modal_after.get("main_thread") is not True:
                        raise AssertionError("模态期间既无正常面板状态，也无可确认的桥接主线程")
                    closed = await wait_operator(run, "cancel_file_dialog")
                    final = await h.snapshot_context()
                    final_ui = core._snapshot_ui(final, "文件框取消后")
                    core._assert_ui_identity(final_ui, session_id, timer_id, "文件框取消后")
                    if final["model"]["epoch"] != after["model"]["epoch"]:
                        raise AssertionError("取消确认框后模型上下文变化")
                    if not final_ui["alive"]:
                        await h.call("ansa_panel_show", {"expected_target": h.target})
                        final, final_ui = await core._wait_ui_snapshot(h,
                            lambda snapshot, ui: ui["alive"] and ui["visible"] and ui["paint_count"] >= 2,
                            "模态结束后重开面板")
                        core._assert_ui_identity(final_ui, session_id, timer_id, "模态结束后重开")
                    echo = await h.direct_result("ansa_script_execute", {
                        "script": "RESULT = {'after_modal': True}", "validate": True})
                    if echo["result"] != {"after_modal": True}:
                        raise AssertionError("模态结束后脚本失败")
                    report["cases"].append({"name": "open_confirmation_then_panel_reopen", "ok": True,
                        "open_observation": opened, "cancel_observation": closed,
                        "paint_before": modal_ui["paint_count"], "paint_after": active_ui["paint_count"],
                        "panel_closed_by_ansa": closed_by_ansa, "panel_alive_after_dialog": final_ui["alive"],
                        "model_epoch_unchanged": True, "session_and_timer_unchanged": True,
                        "script_after_modal": echo["result"]})
                    report["success"] = True
                    core._json_write(run / "ui_followup_stage.json", {"stage": "complete", "panel_kept_visible": True})
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        core._json_write(run / "panel-followup-report.json", report)
        grpc.insecure_channel = original
    print(json.dumps({"success": report["success"], "cases": len(report["cases"]), "report": str(run / "panel-followup-report.json")}))


if __name__ == "__main__":
    run_dir = Path(sys.argv[1]).resolve()
    run_dir.relative_to(core.VERIFICATION_ROOT)
    asyncio.run(run_followup(run_dir))
