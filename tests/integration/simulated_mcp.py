"""当前 MCP stdio → 后端 → TCP → 模拟主线程桥接的最小闭环检查。"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stage_two_simulated import FakeBridge
from stage_three_ansa import EXPECTED_TOOLS
from ansa_mcp_plugin.backend.service import Service

ROOT = Path(__file__).resolve().parents[2]


async def check(runtime_home):
    seed = Service(runtime_home)
    token = seed.token
    seed.close()
    bridge = FakeBridge(runtime_home, token)
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(runtime_home)],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
                assert all(tool.outputSchema and "ok" in tool.outputSchema["properties"] for tool in listed.tools)
                status = await session.call_tool("ansa_connection_status", {})
                assert not status.isError
                content = status.structuredContent
                assert content is not None, status.content
                assert content["data"]["active_target"] is None
                assert any(item["instance_id"] == bridge.state.instance_id for item in content["data"]["instances"])
                unbound = await session.call_tool("ansa_model_snapshot", {"expected_target": {
                    "backend_id": content["data"]["backend_id"], "instance_id": bridge.state.instance_id,
                    "session_id": bridge.state.session_id, "generation": 1}})
                assert unbound.isError
                assert unbound.structuredContent["error"]["code"] == "SNAPSHOT_REJECTED"
                try:
                    Service(runtime_home)
                    raise AssertionError("Second backend unexpectedly started")
                except RuntimeError:
                    pass
                bridge.transport.request_bind()
                active = None
                for _ in range(60):
                    status = await session.call_tool("ansa_connection_status", {})
                    active = status.structuredContent["data"]["active_target"]
                    if active:
                        break
                    await asyncio.sleep(0.1)
                assert active is not None, status.structuredContent
                snapshot = await session.call_tool("ansa_model_snapshot", {"expected_target": active})
                assert not snapshot.isError, snapshot.content
                assert snapshot.structuredContent["data"]["instance_id"] == bridge.state.instance_id
                assert snapshot.structuredContent["data"]["main_thread"]
                print(json.dumps({"ok": True, "tools": [tool.name for tool in listed.tools],
                                  "instance_id": bridge.state.instance_id,
                                  "second_backend_rejected": True, "unbound_execution_rejected": True}))
    finally:
        bridge.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-home", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.runtime_home))


if __name__ == "__main__":
    main()
