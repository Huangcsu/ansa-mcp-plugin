"""第一阶段历史 ANSA 2026 探针；主入口保留旧工具清单，当前验收见 stage_three_ansa.py。

辅助函数仍供后续探针复用；gRPC 仅用于独立测试实例加载与回读。
"""

import argparse
import asyncio
from collections import namedtuple
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import ansa
import grpc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
Details = namedtuple("Details", "method timeout metadata credentials wait_for_ready compression")


class DeadlineInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, seconds=15):
        self.seconds = seconds

    def intercept_unary_unary(self, continuation, details, request):
        timeout = self.seconds if details.timeout is None else min(details.timeout, self.seconds)
        bounded = Details(details.method, timeout, details.metadata, details.credentials,
                          details.wait_for_ready, getattr(details, "compression", None))
        return continuation(bounded, request)


def remote_basic():
    import os
    import threading
    from ansa import base, session
    return {"pid": os.getpid(), "deck": base.CurrentDeck(),
            "main_thread": threading.current_thread() is threading.main_thread(),
            "program_args": [str(item) for item in session.ProgramArguments()]}


def remote_start(project_root, runtime_home, marker_path):
    import builtins
    import sys
    from pathlib import Path
    Path(marker_path).write_text("entered\n", encoding="utf-8")
    sys.path.insert(0, str(Path(project_root) / "src"))
    from ansa_mcp_bridge.bootstrap import start
    Path(marker_path).write_text("imported\n", encoding="utf-8")
    bridge = start(runtime_home=runtime_home, show=False)
    Path(marker_path).write_text("started\n", encoding="utf-8")
    return {"pid": bridge.state.pid, "instance_id": bridge.state.instance_id,
            "session_id": bridge.state.session_id, "port": bridge.transport.port,
            "main_thread": bridge.state.probe()["main_thread"]}


def remote_click_bind():
    import builtins
    builtins._ansa_mcp_plugin_bridge.transport.request_bind()
    return True


def remote_show_panel():
    import builtins
    bridge = builtins._ansa_mcp_plugin_bridge
    bridge.show_panel()
    return {"session_id": bridge.state.session_id, "timer_id": bridge.timer._timer_id}


def remote_repeat_start(project_root, runtime_home):
    from ansa_mcp_bridge.bootstrap import start
    bridge = start(runtime_home=runtime_home, show=False)
    return {"session_id": bridge.state.session_id, "timer_id": bridge.timer._timer_id}


def remote_restart_bridge(runtime_home):
    import builtins
    from ansa_mcp_bridge.bootstrap import start
    old = builtins._ansa_mcp_plugin_bridge
    old_session = old.state.session_id
    old.close()
    new = start(runtime_home, show=False)
    return {"old_session_id": old_session, "instance_id": new.state.instance_id,
            "session_id": new.state.session_id, "pid": new.state.pid}


def remote_model_action(action, path=None):
    from ansa import base, constants, session
    if action == "create_fixture":
        entity = base.CreateEntity(constants.NASTRAN, "GRID", {"X1": 4.0, "X2": 5.0, "X3": 6.0})
        if entity is None:
            raise RuntimeError("Could not create isolated fixture")
        return {"id": entity._id, "save": base.SaveAs(path)}
    if action == "new":
        return session.New("discard")
    if action == "open":
        return base.Open(path)
    if action == "reset":
        return base.ClearIncludeConfiguration(2)
    raise ValueError(action)


def remote_verify_node(node_id):
    from ansa import base, constants
    entity = base.GetEntity(constants.NASTRAN, "GRID", node_id)
    if entity is None:
        return None
    return base.GetEntityCardValues(constants.NASTRAN, entity, ("X1", "X2", "X3"))


def add_case(report, name, action):
    try:
        value = action()
        report["cases"].append({"name": name, "ok": True, "value": value})
        return value
    except Exception as exc:
        report["cases"].append({"name": name, "ok": False, "error": repr(exc)})
        raise


async def mcp_check(runtime_home, report, connection, run_dir, fixture_path, panel_check=False):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(runtime_home)],
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    with (run_dir / "backend.stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                assert names == ["ansa_connection_status", "ansa_model_snapshot"] or set(names) == {"ansa_connection_status", "ansa_model_snapshot"}
                report["tools"] = names

                async def call(name, arguments=None):
                    result = await session.call_tool(name, arguments or {})
                    if result.isError:
                        raise RuntimeError(str(result.content))
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(part.text for part in result.content if part.type == "text"))
                    return data

                initial = await call("ansa_connection_status")
                assert len(initial["data"]["instances"]) >= 1
                assert initial["data"]["active_target"] is None
                add_case(report, "MCP_candidate_unbound", lambda: initial["data"]["instances"])
                panel_task = None
                if panel_check:
                    panel_task = asyncio.create_task(asyncio.to_thread(ansa.execute_function, remote_show_panel))
                    (run_dir / "ui_stage.txt").write_text("click_connect_then_close_panel", encoding="utf-8")
                else:
                    add_case(report, "ANSA_panel_bind_event", lambda: ansa.execute_function(remote_click_bind))
                active = None
                for _ in range(160 if panel_check else 40):
                    status = await call("ansa_connection_status")
                    active = status["data"]["active_target"]
                    if active:
                        break
                    await asyncio.sleep(0.25)
                assert active is not None, "Panel bind was not confirmed"
                add_case(report, "MCP_panel_binding_confirmed", lambda: active)
                if panel_task:
                    closed_panel = await panel_task
                    report["cases"].append({"name": "visible_panel_connect_and_close", "ok": True, "value": closed_panel})
                first = await call("ansa_model_snapshot", {"expected_target": active})
                assert first["data"]["main_thread"]
                add_case(report, "MCP_real_main_thread_snapshot", lambda: first["data"])
                fixture = add_case(report, "fixture_save", lambda: ansa.execute_function(remote_model_action, "create_fixture", str(fixture_path)))
                assert fixture_path.is_file() and fixture_path.stat().st_size > 0
                node_id = fixture["id"]
                before = await call("ansa_model_snapshot", {"expected_target": active})
                add_case(report, "New_discard", lambda: ansa.execute_function(remote_model_action, "new"))
                after_new = await call("ansa_model_snapshot", {"expected_target": active})
                assert ansa.execute_function(remote_verify_node, node_id) is None
                add_case(report, "Open_fixture", lambda: ansa.execute_function(remote_model_action, "open", str(fixture_path)))
                after_open = await call("ansa_model_snapshot", {"expected_target": active})
                assert ansa.execute_function(remote_verify_node, node_id) == {"X1": 4.0, "X2": 5.0, "X3": 6.0}
                add_case(report, "Open_same_path", lambda: ansa.execute_function(remote_model_action, "open", str(fixture_path)))
                same_path = await call("ansa_model_snapshot", {"expected_target": active})
                add_case(report, "database_reset", lambda: ansa.execute_function(remote_model_action, "reset"))
                after_reset = await call("ansa_model_snapshot", {"expected_target": active})
                assert ansa.execute_function(remote_verify_node, node_id) is None
                report["model_detection"] = {label: snap["model"] for label, snap in (
                    ("before", before), ("after_new", after_new), ("after_open", after_open),
                    ("same_path", same_path), ("after_reset", after_reset))}
                add_case(report, "model_context_observed", lambda: report["model_detection"])
                if panel_check:
                    for label in ("new", "open", "same_path_open"):
                        (run_dir / "ui_stage.txt").write_text("model_" + label, encoding="utf-8")
                        completed = run_dir / ("ui_" + label + ".done")
                        deadline = time.monotonic() + 180
                        while not completed.exists():
                            if time.monotonic() > deadline:
                                raise TimeoutError("GUI verification did not confirm " + label)
                            await asyncio.sleep(0.5)
                        snap = await call("ansa_model_snapshot", {"expected_target": active})
                        if label != "new":
                            assert ansa.execute_function(remote_verify_node, node_id) == {"X1": 4.0, "X2": 5.0, "X3": 6.0}
                        report["cases"].append({"name": "GUI_" + label, "ok": True, "value": snap["model"]})
                recreated = ansa.execute_function(remote_restart_bridge, str(runtime_home))
                status = await call("ansa_connection_status")
                assert status["data"]["active_target"] is None and status["data"]["recovery_required"]
                ansa.execute_function(remote_click_bind)
                for _ in range(40):
                    status = await call("ansa_connection_status")
                    rebound = status["data"]["active_target"]
                    if rebound:
                        break
                    await asyncio.sleep(0.25)
                assert rebound and rebound["session_id"] == recreated["session_id"]
                assert rebound["generation"] > active["generation"]
                report["cases"].append({"name": "bridge_close_restart_rebinding", "ok": True, "value": rebound})


async def mcp_recovery_check(runtime_home, report, run_dir):
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(runtime_home)],
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    with (run_dir / "restarted-backend.stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                initial = await session.call_tool("ansa_connection_status", {})
                assert initial.structuredContent["data"]["active_target"] is None
                assert initial.structuredContent["data"]["recovery_required"]
                ansa.execute_function(remote_click_bind)
                active = None
                for _ in range(40):
                    status = await session.call_tool("ansa_connection_status", {})
                    active = status.structuredContent["data"]["active_target"]
                    if active:
                        break
                    await asyncio.sleep(0.25)
                assert active
                snap = await session.call_tool("ansa_model_snapshot", {"expected_target": active})
                assert not snap.isError and snap.structuredContent["data"]["main_thread"]
                report["cases"].append({"name": "external_backend_restart_recovery", "ok": True, "value": active})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--panel-check", action="store_true")
    args = parser.parse_args()
    run_dir = PROJECT_ROOT / "runtime" / "verification" / ("stage-one-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True)
    report = {"run_dir": str(run_dir), "runtime_home": str(args.runtime_home), "cases": []}
    from ansa_mcp_plugin.backend.service import Service
    initial_service = Service(args.runtime_home)
    initial_service.close()
    with socket.socket() as guard:
        guard.bind(("127.0.0.1", 50051))
    translator = run_dir / "isolated_transl.py"
    translator.write_text("# Isolated ANSA MCP bridge validation.\n", encoding="utf-8")
    command = [str(args.ansa_root / "ansa64.bat"), "-grpcport", "50051", "-grpchost", "127.0.0.1",
               "-changedir", str(Path.home()), "-translfp", str(translator), "-nolauncher", "--iconify", "--skip-release-highlights"]
    if args.panel_check:
        command.remove("--iconify")
    report["launch_command"] = command
    child_env = {**os.environ, "ANSA_MCP_PLUGIN_HOME": str(args.runtime_home),
                 "ANSA_MCP_SUM_HOME": str(run_dir / "unused-legacy-runtime"), "ANSA_MCP_SUM_AUTO_POLL": "0",
                 "PYTHONDONTWRITEBYTECODE": "1"}
    connection = None
    target_pid = None
    original_channel = grpc.insecure_channel
    def bounded_channel(target, options=None, **kwargs):
        channel = original_channel(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs)
        return grpc.intercept_channel(channel, DeadlineInterceptor(90 if args.panel_check else 15))
    grpc.insecure_channel = bounded_channel
    with (run_dir / "ansa.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=run_dir, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        report["launcher_pid"] = process.pid
        try:
            channel = original_channel("127.0.0.1:50051", options=[("grpc.enable_http_proxy", 0)])
            try:
                grpc.channel_ready_future(channel).result(timeout=75)
            finally:
                channel.close()
            connection = ansa.connect(host="127.0.0.1", port=50051)
            basic = add_case(report, "gRPC_isolated_readiness", lambda: ansa.execute_function(remote_basic))
            assert basic["main_thread"]
            assert str(translator) in basic["program_args"] and "50051" in basic["program_args"]
            target_pid = basic["pid"]
            started = add_case(report, "bridge_bootstrap", lambda: ansa.execute_function(remote_start, str(PROJECT_ROOT), str(args.runtime_home), str(run_dir / "bootstrap_marker.txt")))
            assert started["pid"] == target_pid
            assert started["main_thread"]
            duplicate = add_case(report, "hot_reload_reuses_executor", lambda: ansa.execute_function(remote_repeat_start, str(PROJECT_ROOT), str(args.runtime_home)))
            assert duplicate["session_id"] == started["session_id"]
            asyncio.run(mcp_check(args.runtime_home, report, connection, run_dir, run_dir / "fixture.ansa", args.panel_check))
            asyncio.run(mcp_recovery_check(args.runtime_home, report, run_dir))
        except Exception as exc:
            report["fatal_error"] = repr(exc)
        finally:
            if connection is not None:
                if target_pid is not None:
                    try:
                        ansa.stop(connection)
                        report["cleanup"] = "stopped isolated ANSA"
                    except Exception as exc:
                        report["cleanup_error"] = repr(exc)
                else:
                    ansa.disconnect(connection)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                report["launcher_still_running"] = True
            grpc.insecure_channel = original_channel
    report["success"] = all(case["ok"] for case in report["cases"]) and "fatal_error" not in report
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run_dir / "report.json"), "success": report["success"],
                      "case_count": len(report["cases"]), "fatal_error": report.get("fatal_error")}, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
