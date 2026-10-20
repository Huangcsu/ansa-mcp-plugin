"""从无源码工作目录验证已安装的外部 wheel；不连接或修改 ANSA。"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stage_three_ansa import EXPECTED_TOOLS, _json_write


async def verify(installation, report_path):
    installation = installation.resolve()
    manifest = json.loads((installation / "install-manifest.json").read_text(encoding="utf-8"))
    python = installation / manifest["venv_python"]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    report = {"success": False, "scope": "installed_external_wheel_only", "cases": [],
              "version": manifest["version"], "installation": str(installation),
              "runtime_home": manifest["runtime_home"], "ansa_executed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="ansa-mcp-release-smoke-") as unrelated:
            metadata = subprocess.run([str(python), "-I", "-c", "\n".join([
                "import importlib.metadata as md, importlib.util, json",
                "import ansa_mcp_plugin, ansa_mcp_shared",
                "print(json.dumps({'version':md.version('ansa-mcp-plugin'),",
                " 'plugin_file':ansa_mcp_plugin.__file__, 'shared_file':ansa_mcp_shared.__file__,",
                " 'excluded_modules':{n:importlib.util.find_spec(n) is None for n in",
                " ('ansa','ansa_mcp_bridge','bcsrpc','grpc')}}))"])],
                check=True, cwd=unrelated, env=env, capture_output=True, text=True, encoding="utf-8")
            evidence = json.loads(metadata.stdout)
            if evidence["version"] != "0.1.1" or not all(evidence["excluded_modules"].values()):
                raise AssertionError("发布包版本或 SDK/桥接隔离不符")
            for key in ("plugin_file", "shared_file"):
                Path(evidence[key]).resolve().relative_to(installation)
            report["cases"].append({"name": "isolated_installed_imports_and_no_sdk", "ok": True, "evidence": evidence})
            params = StdioServerParameters(command=str(python),
                args=["-I", str(installation / manifest["launcher"])], cwd=unrelated, env=env)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
                async with stdio_client(params, errlog=stderr) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                        await client.initialize()
                        listed = await client.list_tools()
                        if {tool.name for tool in listed.tools} != EXPECTED_TOOLS:
                            raise AssertionError("安装环境工具表不符")
                        if not all(tool.inputSchema and tool.outputSchema for tool in listed.tools):
                            raise AssertionError("安装环境 schema 不完整")
                        report["cases"].append({"name": "installed_stdio_and_65_schemas", "ok": True, "count": len(listed.tools)})
                        result = await client.call_tool("ansa_connection_status", {})
                        value = result.structuredContent
                        if result.isError or value["ok"] is not True:
                            raise AssertionError("安装环境状态调用失败")
                        status = value["data"]
                        if status["active_target"] is not None or status["instances"]:
                            raise AssertionError("安装验收应使用无实例独立 runtime")
                        report["cases"].append({"name": "installed_private_runtime_status", "ok": True,
                                                "active_target": None, "instances": []})
        report["success"] = True
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        _json_write(report_path, report)
    print(json.dumps({"success": True, "cases": len(report["cases"]), "report": str(report_path)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.installation, args.report.resolve()))
