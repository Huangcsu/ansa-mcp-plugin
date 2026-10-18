"""独立空白 ANSA 的原生按钮验收；输入操作由 Computer Use 或用户完成。"""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def NativeControlsStart(run_dir):
    """只在带唯一 translator 的验收实例中启动源码桥接，不载入用户模型。"""
    import os
    import threading
    from ansa import session
    run = Path(run_dir).resolve()
    params = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    arguments = [str(value) for value in session.ProgramArguments()]
    if (threading.current_thread() is not threading.main_thread()
            or params["translator"] not in arguments):
        raise RuntimeError("原生按钮验收进程身份不符")
    sys.path.insert(0, str(ROOT / "src"))
    from ansa_mcp_bridge import bootstrap
    from ansa_mcp_bridge.ui import native_panel
    bridge = bootstrap.start(params["runtime_home"], show=False)
    bridge._native_panel_enabled = True
    _write(run / "ansa-start.json", {"pid": os.getpid(), "program_args": arguments,
                                   **bridge.state.identity()})
    native_panel.show_panel(bridge)
    _write(run / "ansa-closed.json", {"pid": os.getpid(), "ticks": bridge.executor.ticks,
                                    "ui": bridge.state.panel_info})


def prepare(args):
    ansa_root = args.ansa_root.resolve()
    if not (ansa_root / "ansa64.bat").is_file():
        raise ValueError("--ansa-root 必须指向含 ansa64.bat 的 ANSA 安装目录")
    run = ROOT / "runtime/verification" / ("native-controls-" + uuid.uuid4().hex[:10])
    run.mkdir(parents=True)
    translator = run / "isolated_transl.py"
    translator.write_text("# 独立空白模型的按钮验收\n", encoding="utf-8")
    runtime = Path.home() / "ANSA-MCP-Plugin-Runtime" / run.name
    command = [str(ansa_root / "ansa64.bat"), "-grpcport", str(args.port), "-grpchost", "127.0.0.1",
               "-translfp", str(translator), "-nolauncher", "--skip-release-highlights", "-changedir", str(Path.home())]
    if args.settings_profile:
        command.extend(["--settings-profile", args.settings_profile])
    command.extend(["-execpy", "load_script: " + repr(Path(__file__).as_posix()),
                    "-execpy", "NativeControlsStart(" + repr(run.as_posix()) + ")"])
    environment = {"ANSA_MCP_SUM_AUTO_POLL": "0", "ANSA_MCP_SUM_HOME": str(run / "unused-legacy-runtime"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    if args.license_server:
        environment["ANSA_SRV"] = args.license_server
    params = {"run_dir": str(run), "ansa_root": str(ansa_root), "settings_profile": args.settings_profile,
              "runtime_home": str(runtime), "translator": str(translator), "port": args.port,
              "launch_command": command, "environment": environment}
    _write(run / "launch_instructions.json", params)
    print(str(run))


def backend(run):
    sys.path.insert(0, str(ROOT / "src"))
    from ansa_mcp_plugin.backend.service import Service
    params = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    service = Service(params["runtime_home"])
    try:
        print("isolated backend ready", flush=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not (run / "backend.stop").exists():
            with service._lock:
                connections = []
                for instance_id, connection in list(service.connections.items()):
                    item = {"instance_id": instance_id, "connected": not connection.closed}
                    if not connection.closed:
                        try:
                            item["task_status"] = connection.call("task_status")
                        except Exception as exc:
                            item["error"] = str(exc).replace(service.token, "[redacted]")
                    connections.append(item)
                _write(run / "backend-state.json", {"backend_id": service.backend_id,
                                                    "active_target": service.store.current(),
                                                    "connections": connections,
                                                    "error": service.last_error})
            time.sleep(.5)
    finally:
        service.close()
        print("isolated backend stopped", flush=True)


async def stdio_check(run):
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    runtime = params["runtime_home"] + "-stdio"
    server = StdioServerParameters(command=sys.executable,
                                  args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", runtime],
                                  env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"ansa_connection_status", "ansa_model_snapshot", "ansa_grid_read"} <= names
            result = await session.call_tool("ansa_connection_status", {})
            assert not result.isError, result.content
            assert result.structuredContent["data"]["instances"] == []
            value = {"success": True, "tool_count": len(names), "runtime_home": runtime,
                     "scope": "真实 MCP stdio 初始化、工具发现和空实例状态；不访问用户 ANSA"}
            _write(run / "stdio-report.json", value)
            print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__" and "ansa" not in sys.modules:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "backend", "stdio"))
    parser.add_argument("run", nargs="?", help="backend / stdio 使用 prepare 生成的验收目录")
    parser.add_argument("--ansa-root", type=Path, help="prepare 必填；含 ansa64.bat 的目录")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--settings-profile", help="可选的独立 ANSA 测试配置名称")
    parser.add_argument("--license-server", help="可选 ANSA_SRV；省略时沿用启动环境")
    args = parser.parse_args()
    if args.mode == "prepare":
        if args.ansa_root is None:
            parser.error("prepare 需要 --ansa-root")
        if not 1 <= args.port <= 65535:
            parser.error("--port 必须在 1..65535 范围内")
        prepare(args)
    elif args.mode == "backend":
        if args.run is None:
            parser.error("backend 需要验收目录 run")
        backend(Path(args.run).resolve())
    else:
        if args.run is None:
            parser.error("stdio 需要验收目录 run")
        import asyncio
        asyncio.run(stdio_check(Path(args.run).resolve()))
