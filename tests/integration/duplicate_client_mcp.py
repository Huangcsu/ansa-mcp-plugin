"""Windows 真正 stdio 双客户端竞争与原会话恢复；不连接用户 ANSA。"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ansa_mcp_plugin.backend.service import Service

ROOT = Path(__file__).resolve().parents[2]


async def check(runtime_home):
    if runtime_home.exists():
        raise ValueError('必须使用全新的独立验收目录')
    owner = Service(runtime_home)
    params = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'ansa_mcp_plugin', '--direct-backend', '--runtime-home', str(runtime_home)],
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 69
                busy = await session.call_tool('ansa_connection_status', {})
                assert busy.isError, busy
                assert busy.structuredContent['error']['code'] == 'BACKEND_BUSY', busy
                assert owner.status()['backend_id'] == owner.backend_id
                owner.close()
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    ready = await session.call_tool('ansa_connection_status', {})
                    if not ready.isError:
                        break
                assert not ready.isError, ready
                assert ready.structuredContent['data']['backend_id'] != owner.backend_id
                assert ready.structuredContent['data']['active_target'] is None
                assert ready.structuredContent['data']['instances'] == []
                assert ready.structuredContent['data']['tasks'] == []
                print(json.dumps({'ok': True, 'tools': 69, 'busy_initialize': True,
                    'same_session_recovered': True, 'unbound': True}))
    finally:
        owner.close()
    successor = Service(runtime_home)
    successor.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-home', type=Path, required=True)
    asyncio.run(check(parser.parse_args().runtime_home))
