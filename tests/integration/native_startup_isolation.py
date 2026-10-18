"""准备独立 ANSA 原生脚本诊断；只生成参数，不启动或清理进程。"""

import argparse
import json
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[2]
VERIFICATION = ROOT / "runtime" / "verification"
CASES = ("empty", "timer_only", "bridge_only", "gui_only", "combined", "native_panel", "entry", "entry_reload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--prepare-only", action="store_true", required=True)
    parser.add_argument("--ansa-root", type=Path, required=True, help="含 ansa64.bat 的 ANSA 安装目录")
    parser.add_argument("--runtime-home", type=Path, default=Path.home() / "ANSA-MCP-Plugin-Runtime/development",
                        help="已初始化的私有运行目录；entry 场景仍使用已安装入口的开发配置")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--settings-profile", help="可选的独立 ANSA 测试配置名称")
    parser.add_argument("--license-server", help="可选 ANSA_SRV；省略时沿用启动环境")
    args = parser.parse_args()
    ansa_root = args.ansa_root.resolve()
    if not (ansa_root / "ansa64.bat").is_file():
        parser.error("--ansa-root 必须指向含 ansa64.bat 的目录")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1..65535 范围内")
    runtime_home = args.runtime_home.resolve()
    run = VERIFICATION / ("native-isolation-" + args.case + "-" + uuid.uuid4().hex[:10])
    run.mkdir(parents=True)
    probe = run / "native_startup_probe.py"
    probe.write_bytes(Path(__file__).with_name("native_startup_probe.py").read_bytes())
    translator = run / "isolated_transl.py"
    translator.write_text("# 独立原生启动诊断，不加载用户模型。\n", encoding="utf-8")
    params = {"case": args.case, "source_root": ROOT.as_posix(), "runtime_home": str(runtime_home)}
    (run / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [str(ansa_root / "ansa64.bat"),
               "-grpcport", str(args.port), "-grpchost", "127.0.0.1",
               "-translfp", str(translator), "-nolauncher", "--skip-release-highlights",
               "-changedir", str(Path.home())]
    if args.settings_profile:
        command.extend(["--settings-profile", args.settings_profile])
    command.extend(["-execpy", "load_script: " + repr(probe.as_posix()),
                    "-execpy", "NativeStartupProbe(" + repr(run.as_posix()) + ", " + repr(args.case) + ")"])
    if args.case in {"entry", "entry_reload"}:
        entry = Path.home() / ".BETA/version_26.10.000/ANSA/plugins/AnsaMcpPluginDev/AnsaMcpPluginDev.py"
        # 同一次原生脚本函数执行中安排自闭回调，避免 load_script 切换后
        # 上一个脚本的 SingleShot 被 ANSA 清理。入口正文保持安装副本内容。
        entry_copy = run / "entry_isolation.py"
        wrapper = "\n\ndef NativeEntryIsolation(run_dir):\n"
        wrapper += "    name = '_mcp_native_isolation_probe'\n"
        wrapper += "    spec = importlib.util.spec_from_file_location(name, " + repr(str(probe)) + ")\n"
        wrapper += "    diagnostic = importlib.util.module_from_spec(spec)\n"
        wrapper += "    sys.modules[name] = diagnostic\n"
        wrapper += "    spec.loader.exec_module(diagnostic)\n"
        wrapper += "    diagnostic.NativeStartupProbe(run_dir, " + repr(args.case) + ")\n"
        wrapper += "    AnsaMcpPluginDevControlPanel()\n"
        wrapper += "    diagnostic.NativeEntryReturned()\n"
        if args.case == "entry_reload":
            wrapper += "    AnsaMcpPluginDevReload()\n"
            wrapper += "    diagnostic.NativeEntryReloaded()\n"
            wrapper += "    AnsaMcpPluginDevControlPanel()\n"
            wrapper += "    diagnostic.NativeEntryReturned()\n"
            wrapper += "    AnsaMcpPluginDevStop()\n"
            wrapper += "    diagnostic.NativeEntryStopped()\n"
        entry_copy.write_text(entry.read_text(encoding="utf-8") + wrapper, encoding="utf-8")
        command[-4:] = ["-execpy", "load_script: " + repr(entry_copy.as_posix()),
                        "-execpy", "NativeEntryIsolation(" + repr(run.as_posix()) + ")"]
    environment = {"ANSA_MCP_SUM_AUTO_POLL": "0", "ANSA_MCP_SUM_HOME": str(run / "unused-legacy-runtime"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    if args.license_server:
        environment["ANSA_SRV"] = args.license_server
    instructions = {"run_dir": str(run), "case": args.case, "ansa_root": str(ansa_root),
                    "settings_profile": args.settings_profile, "port": args.port,
                    "translator": str(translator), "runtime_home": str(runtime_home),
                    "launch_command": command, "environment": environment,
                    "acceptance_scope": "原生脚本分层诊断；自动关闭的 GUI 不计作 Ribbon 点击验收"}
    (run / "launch_instructions.json").write_text(json.dumps(instructions, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(run))


if __name__ == "__main__":
    main()
