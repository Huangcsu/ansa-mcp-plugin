"""多 stdio 客户端共享后端、单次执行与断线恢复；只使用模拟 ANSA。"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
import signal
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ansa_mcp_plugin.backend.service import Service
from ansa_mcp_plugin.backend.shared_client import SharedService, _read_endpoint, _process_is_live
from task_history_mcp import simulated_read_operations, terminal
from stage_two_simulated import FakeBridge

ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def client(home):
    params = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'ansa_mcp_plugin', '--runtime-home', str(home)],
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 69
            yield session


async def status(session, bound=False):
    for _ in range(40):
        response = await session.call_tool('ansa_connection_status', {})
        value = response.structuredContent
        if value['ok'] and (not bound or value['data']['active_target']):
            return value['data']
        await asyncio.sleep(.2)
    raise AssertionError(value)


async def check(home):
    if home.exists():
        raise ValueError('必须使用全新的独立验收目录')
    seed = Service(home)
    token = seed.token
    seed.close()
    bridge = FakeBridge(home, token)
    admin = None
    try:
        with simulated_read_operations():
            async with client(home) as first:
                async with client(home) as second:
                    left, right = await asyncio.gather(status(first), status(second))
                    assert left['backend_id'] == right['backend_id']
                    backend_id = left['backend_id']
                    for _ in range(100):
                        if bridge.state.connected.is_set():
                            break
                        await asyncio.sleep(.05)
                    assert bridge.state.connected.is_set()
                    bridge.transport.request_bind()
                    active = (await status(first, bound=True))['active_target']
                    snapshot = await first.call_tool('ansa_model_snapshot', {'expected_target': active})
                    assert not snapshot.isError, snapshot
                    model = {'epoch': snapshot.structuredContent['model']['epoch']}
                    fields = {'operation_id': 'shared-exactly-once', 'expected_target': active,
                              'expected_model': model, 'entity_id': 1}
                    one, two = await asyncio.gather(first.call_tool('ansa_grid_read', fields),
                                                     second.call_tool('ansa_grid_read', fields))
                    assert not one.isError and not two.isError, (one, two)
                    task_id = one.structuredContent['task_id']
                    assert task_id == two.structuredContent['task_id']
                    await terminal(second, task_id, 'succeeded')
                    assert len(bridge.state.executions) == 1, bridge.state.executions
                    missing = await second.call_tool('ansa_task_query', {'operation_id': 'absent'})
                    assert missing.isError
                    # 有未决任务时管理入口不能关闭后端，也不能丢弃队列。
                    admin = SharedService(str(home))
                    bridge.state.pause_claim.set()
                    try:
                        queued = await first.call_tool('ansa_grid_read', {**fields, 'operation_id': 'pending-stop'})
                        assert not queued.isError, queued
                        try:
                            await asyncio.to_thread(admin.shutdown)
                        except RuntimeError:
                            pass
                        else:
                            raise AssertionError('shutdown accepted while a task was pending')
                        cancelled = await second.call_tool('ansa_task_cancel', {'task_id': queued.structuredContent['task_id']})
                        assert cancelled.structuredContent['status'] in ('queued', 'running', 'cancelled'), cancelled
                        await terminal(second, queued.structuredContent['task_id'], 'cancelled')
                    finally:
                        bridge.state.pause_claim.clear()
                # 第二个客户端退出不改变后端身份、目标绑定和已完成任务。
                retained = await status(first, bound=True)
                assert retained['backend_id'] == backend_id
                assert retained['active_target'] == active
                await terminal(first, task_id, 'succeeded')
            # 所有 stdio 客户端退出后再次接入，仍复用后端。
            async with client(home) as third:
                retained = await status(third, bound=True)
                assert retained['backend_id'] == backend_id
                await asyncio.to_thread(admin.shutdown)
                # 首次失联调用允许返回未就绪；不重发任何模型操作。
                recovered = await status(third)
                assert recovered['backend_id'] != backend_id
                assert recovered['active_target'] is None
                stale = await third.call_tool('ansa_grid_read', {**fields, 'operation_id': 'stale-target'})
                assert stale.isError, stale
                await terminal(third, task_id, 'succeeded')
                history = await third.call_tool('ansa_task_list', {'limit': 30})
                assert not history.isError, history
                rows = history.structuredContent['data']['tasks']
                assert any(row['task_id'] == task_id for row in rows), rows
                assert all(set(row) == {'task_id', 'operation_id', 'state', 'tool',
                    'dispatch', 'cancel_pending', 'target', 'model'} for row in rows), rows
                assert len(bridge.state.executions) == 1
                # 只终止本测试新目录中已核实身份的后端，模拟异常退出。
                endpoint = _read_endpoint(home)
                assert endpoint['backend_id'] == recovered['backend_id']
                assert _process_is_live(endpoint['pid'], endpoint['process_start'])
                os.kill(endpoint['pid'], signal.SIGTERM)
                after_crash = await status(third)
                assert after_crash['backend_id'] != recovered['backend_id']
                assert after_crash['active_target'] is None
                await terminal(third, task_id, 'succeeded')
                assert len(bridge.state.executions) == 1
                print(json.dumps({'ok': True, 'tools': 69, 'same_backend': True,
                    'duplicate_executions': 0, 'client_exit_preserves_binding': True,
                    'backend_restart_requires_binding': True, 'history_retained': True,
                    'abrupt_exit_recovered': True}))
    finally:
        bridge.close()
        if admin is None:
            admin = SharedService(str(home))
        try:
            await asyncio.to_thread(admin.shutdown)
        finally:
            admin.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-home', type=Path, required=True)
    asyncio.run(check(parser.parse_args().runtime_home))
