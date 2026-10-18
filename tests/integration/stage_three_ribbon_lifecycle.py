"""独立原生 Ribbon 的冷启动、长任务、模态框及换模验收。

UI 操作由 Computer Use/操作者完成并写入观察标记；不以 MCP 显示面板代替 Ribbon。
只接受带唯一 translator、正常用户 CIM 归属记录的独立测试进程。
"""

import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as core
from native_entry_probe import remote_entry_state


def remote_profile_name():
    from ansa import base
    return base.BCSettingsGetCurrentProfileName()


def prepare_profile(run):
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    home = Path.home() / ".BETA/version_26.10.000/ANSA/profiles"
    original = home / "default/gui/ANSA.xml"
    raw = original.read_bytes()
    profile_ini = original.parent.parent / "profile.ini"
    ini_bytes = profile_ini.read_bytes()
    if b"application=ANSA" not in ini_bytes or b"version=26.10.000" not in ini_bytes:
        raise ValueError("默认 profile.ini 的应用或版本不符")
    name = "mcp-ribbon-" + run.name.rsplit("-", 1)[1][:12]
    profile = home / name
    if profile.exists():
        raise FileExistsError(profile)
    source = core.PROJECT_ROOT / "packaging/ansa"
    stage = run / "native-plugin"
    entry = stage / "AnsaMcpPluginDev"
    entry.mkdir(parents=True)
    shutil.copy2(source / "AnsaMcpPluginDev.ppl", stage)
    for filename in ("AnsaMcpPluginDev.py", "AnsaMcpPluginDev.svg"):
        shutil.copy2(source / filename, entry)
    core._initialize_runtime_home(Path(instructions["runtime_home"]))
    core._json_write(entry / "development.json", {
        "source_root": str(core.PROJECT_ROOT), "runtime_home": instructions["runtime_home"]})
    descriptor = (stage / "AnsaMcpPluginDev.ppl").as_posix()
    replacement = '<Script_Plugins>\n   <plugin name="' + descriptor + '" loaded="1" addedFrom="3" />\n  </Script_Plugins>'
    text, count = re.subn(r"<Script_Plugins>.*?</Script_Plugins>", replacement,
                         raw.decode("utf-8"), flags=re.S)
    if count != 1:
        raise ValueError("独立配置 Script_Plugins 结构不唯一")
    parsed = ET.fromstring(text)
    plugins = list(parsed.iter("plugin"))
    if len(plugins) != 1 or plugins[0].get("name") != descriptor:
        raise AssertionError("隔离配置含其他插件")
    gui = profile / "gui/ANSA.xml"
    gui.parent.mkdir(parents=True)
    (profile / "profile.ini").write_bytes(ini_bytes)
    gui.write_text(text, encoding="utf-8")
    record = {"settings_profile": name, "profile": str(profile), "default_xml": str(original),
              "default_sha256": hashlib.sha256(raw).hexdigest(), "descriptor": descriptor}
    core._json_write(run / "ribbon-profile.json", record)
    instructions["settings_profile"] = name
    instructions["launch_command"].extend(["--settings-profile", name])
    core._json_write(run / "launch_instructions.json", instructions)
    print(json.dumps(record), flush=True)


async def operator(run, stage):
    marker = run / ("ribbon-ui-" + stage + ".json")
    if marker.exists():
        raise FileExistsError("不得复用旧操作观察：" + str(marker))
    core._json_write(run / "ribbon-ui-stage.json", {"stage": stage, "marker": str(marker)})
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if marker.is_file():
            value = json.loads(marker.read_text(encoding="utf-8-sig"))
            if value.get("native_ui_observed") is not True:
                raise AssertionError("缺少原生 UI 观察")
            return value
        await asyncio.sleep(.25)
    raise TimeoutError("等待 UI 观察超时：" + stage)


async def run_check(run):
    import grpc
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    ownership = json.loads((run / "normal-user-process-before.json").read_text(encoding="utf-8-sig"))
    if isinstance(ownership, list):
        if len(ownership) != 1:
            raise AssertionError("CIM 进程归属不唯一")
        ownership = ownership[0]
    if instructions["translator"].casefold() not in ownership["CommandLine"].casefold():
        raise AssertionError("CIM 唯一 translator 不匹配")
    first, second = core._import_runtime_helpers()
    original_channel = core._bounded_grpc(grpc, first.DeadlineInterceptor)
    report = {"success": False, "scope": "native_ribbon_lifecycle", "cases": []}
    report_path = run / "ribbon-lifecycle-report.json"

    def record(name, **evidence):
        report["cases"].append({"name": name, "ok": True, **evidence})
        core._json_write(report_path, report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    try:
        channel = original_channel("127.0.0.1:" + str(instructions["port"]),
                                   options=[("grpc.enable_http_proxy", 0)])
        try:
            grpc.channel_ready_future(channel).result(timeout=60)
        finally:
            channel.close()
        basic = second.rpc(instructions["port"], remote_entry_state)
        core._verify_program_arguments(basic, Path(instructions["translator"]), instructions["port"])
        if basic["pid"] != ownership["ProcessId"] or basic["bridge_present"]:
            raise AssertionError("冷启动归属或桥接初态不符")
        if instructions["settings_profile"] not in basic["program_args"]:
            raise AssertionError("没有使用独立配置")
        active_profile = second.rpc(instructions["port"], remote_profile_name)
        if active_profile != instructions["settings_profile"]:
            raise AssertionError("ANSA 未启用预期的独立 profile：" + str(active_profile))
        report["identity"] = basic
        report["settings_profile"] = active_profile
        seed_path = run / "ribbon-baseline.ansa"
        seed = second.rpc(instructions["port"], second.remote_seed, str(seed_path))
        if not seed["usable"] or not seed_path.is_file():
            raise AssertionError("独立夹具保存失败")
        report["seed"] = seed
        params = StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
            env={**os.environ, "PYTHONPATH": str(core.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
        with (run / "ribbon-backend.stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = core.McpHarness(client, report, run)
                    report["schemas"] = await h.list_schema()
                    status = await h.call("ansa_connection_status")
                    if status["data"]["instances"] or status["data"]["active_target"]:
                        raise AssertionError("Ribbon 点击前已有桥接")
                    observed = await operator(run, "cold_start_and_bind")
                    for _ in range(80):
                        status = await h.call("ansa_connection_status")
                        if status["data"]["active_target"]:
                            break
                        await asyncio.sleep(.25)
                    else:
                        raise TimeoutError("Ribbon 连接未确认")
                    h.target = status["data"]["active_target"]
                    before, ui = await core._wait_ui_snapshot(h,
                        lambda snap, state: state["alive"] and state["visible"] and state["paint_count"] >= 2,
                        "原生 Ribbon 冷启动", timeout=40)
                    if before["pid"] != basic["pid"]:
                        raise AssertionError("Ribbon 目标进程错误")
                    session_id, timer_id = ui["session_id"], ui["timer_id"]
                    record("native_ribbon_cold_start_and_panel_binding", observation=observed,
                           target=h.target, ui=ui, bridge_absent_before_click=True)
                    submitted = await h.call("ansa_script_execute", {
                        "operation_id": "ribbon-long-" + run.name, "expected_target": h.target,
                        "expected_model": h.model, "validate": True,
                        "script": "import time, threading\ntime.sleep(6)\nRESULT = {'main_thread': threading.current_thread() is threading.main_thread()}"})
                    saw_running = False
                    for _ in range(160):
                        result = await h.call("ansa_task_query", {"task_id": submitted["task_id"]})
                        saw_running |= result["status"] == "running"
                        if result["status"] in core.TERMINAL_TASK_STATES:
                            break
                        await asyncio.sleep(.2)
                    if result["status"] != "succeeded" or not saw_running or result["data"]["result"]["result"] != {"main_thread": True}:
                        raise AssertionError("长任务未完成真实主线程运行")
                    after, active = await core._wait_ui_snapshot(h,
                        lambda snap, state: state["alive"] and state["visible"] and state["paint_count"] > ui["paint_count"],
                        "长任务后原生面板刷新", timeout=40)
                    core._assert_ui_identity(active, session_id, timer_id, "长任务")
                    record("native_ribbon_long_task_refresh", task_id=submitted["task_id"],
                           observed_running=True, ui=active)
                    modal_open = await operator(run, "open_modal")
                    modal_before = await h.snapshot_context()
                    modal_ui = core._snapshot_ui(modal_before, "原生模态框")
                    await asyncio.sleep(3)
                    modal_after = await h.snapshot_context()
                    modal_active = core._snapshot_ui(modal_after, "原生模态框保持")
                    core._assert_ui_identity(modal_active, session_id, timer_id, "模态框")
                    if not ((modal_active["alive"] and modal_active["paint_count"] > modal_ui["paint_count"])
                            or (not modal_active["alive"] and modal_active["close_reason"] in {"returned", "reject", "destroyed"})):
                        raise AssertionError("模态期间面板状态不正常")
                    modal_cancel = await operator(run, "cancel_modal")
                    final = await h.snapshot_context()
                    if final["model"]["epoch"] != after["model"]["epoch"]:
                        raise AssertionError("取消模态框改变模型代次")
                    if not final["ui"]["alive"]:
                        await operator(run, "reopen_after_modal")
                    final, active = await core._wait_ui_snapshot(h,
                        lambda snap, state: state["alive"] and state["visible"], "取消后原生面板", timeout=40)
                    core._assert_ui_identity(active, session_id, timer_id, "模态结束")
                    record("native_ribbon_modal_cancel_and_recovery", opened=modal_open, cancelled=modal_cancel,
                           ui=active, epoch_unchanged=True)
                    for action in ("new", "open"):
                        previous = dict(h.model)
                        fields = {"preservation": "discard"}
                        if action == "open":
                            fields["path"] = str(seed_path)
                        outcome = await h.direct_result("ansa_model_" + action, fields)
                        snapshot = await h.snapshot_context()
                        if h.model == previous:
                            raise AssertionError("换模代次未更新")
                        if not snapshot["ui"]["alive"]:
                            await operator(run, "reopen_after_" + action)
                        snapshot, active = await core._wait_ui_snapshot(h,
                            lambda snap, state: state["alive"] and state["visible"], "换模后原生面板", timeout=40)
                        core._assert_ui_identity(active, session_id, timer_id, action)
                        stale = await h.call("ansa_entity_count", {
                            "operation_id": "ribbon-stale-" + action + run.name,
                            "expected_target": h.target, "expected_model": previous, "entity_type": "GRID"}, allow_error=True)
                        if stale.get("ok") is not False:
                            raise AssertionError("旧模型上下文未被拒绝")
                        record("native_ribbon_model_" + action, before=previous, after=h.model,
                               ui=active, result=outcome, stale_response=stale)
                    await operator(run, "close_panel")
                    closed = await h.snapshot_context()
                    if closed["ui"]["alive"]:
                        raise AssertionError("原生面板未关闭")
                    readback = second.rpc(instructions["port"], first.remote_verify_node, seed["id"])
                    entry = second.rpc(instructions["port"], remote_entry_state)
                    if readback != {"X1": 1., "X2": 2., "X3": 3.}:
                        raise AssertionError("独立回读节点不符")
                    if entry["session_id"] != session_id or entry["timer_id"] != timer_id:
                        raise AssertionError("独立回读会话/定时器不一致")
                    record("independent_readback_and_no_duplicate_executor", node=readback, entry=entry)
        profile = json.loads((run / "ribbon-profile.json").read_text(encoding="utf-8"))
        if hashlib.sha256(Path(profile["default_xml"]).read_bytes()).hexdigest() != profile["default_sha256"]:
            raise AssertionError("默认用户配置发生变化")
        report["default_profile_unchanged"] = True
        report["success"] = True
        core._json_write(run / "ribbon-ui-stage.json", {"stage": "complete"})
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        core._json_write(report_path, report)
        grpc.insecure_channel = original_channel


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare-profile", "run"))
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.relative_to(core.VERIFICATION_ROOT.resolve())
    if args.mode == "prepare-profile":
        prepare_profile(run_dir)
    else:
        asyncio.run(run_check(run_dir))
