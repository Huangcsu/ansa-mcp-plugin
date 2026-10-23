"""任务历史真实 stdio/TCP/SQLite 验收；模型 API 由所属模拟线程替代。"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ansa_mcp_plugin.backend.service import Service
from ansa_mcp_plugin.backend.tasks import TaskStore
from stage_three_ansa import EXPECTED_TOOLS
from stage_two_simulated import FakeBridge


ROOT = Path(__file__).resolve().parents[2]
RESULT_MARKER = 'task-history-fixture-result-excluded-from-summary'


@contextmanager
def simulated_read_operations():
    """只模拟 GRID 读取/失败/阻塞，不修改模拟模型，也不绕过运行目录 ACL。"""
    name = 'ansa_mcp_bridge.operations'
    previous = sys.modules.get(name)
    module = types.ModuleType(name)
    def execute(state, tool, params):
        state.assert_main()
        if tool != 'grid_read':
            raise AssertionError('History acceptance only submits simulated reads')
        state.executions.append({'tool': tool, 'id': params['id']})
        if params['id'] == 999:
            raise LookupError('Fixture GRID is absent')
        if params['id'] == 2:
            state.operation_entered.set()
            if not state.operation_release.wait(20):
                raise TimeoutError('Fixture held read was not released')
        return {'entity_id': params['id'], 'result_marker': RESULT_MARKER}
    module.execute = execute
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def independent_rows(home):
    with sqlite3.connect('file:' + (home / 'state.sqlite3').as_posix() + '?mode=ro', uri=True) as db:
        return [{'sequence': seq, 'task_id': task_id, 'operation_id': operation_id, 'state': state}
                for seq, task_id, operation_id, state in db.execute(
                    'SELECT seq,task_id,operation_id,state FROM tasks ORDER BY seq DESC')]


async def call(client, name, fields=None):
    response = await client.call_tool(name, fields or {})
    value = response.structuredContent
    if value is None:
        parts = [c.text for c in response.content if c.type == 'text']
        if parts:
            try:
                value = json.loads(parts[0])
            except json.JSONDecodeError:
                pass
    assert type(value) is dict, (name, response.content)
    return value


async def terminal(client, task_id, expected):
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        value = await call(client, 'ansa_task_query', {'task_id': task_id})
        if value['status'] in {'succeeded', 'failed', 'expired', 'cancelled'}:
            assert value['status'] == expected, value
            return value
        await asyncio.sleep(.05)
    raise TimeoutError('Query the original task instead of submitting it again: ' + task_id)


async def wait_condition(predicate):
    deadline = asyncio.get_running_loop().time() + 20
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('Simulated owner thread did not reach the expected state')
        await asyncio.sleep(.05)


def parameters(home):
    return StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'ansa_mcp_plugin', '--direct-backend', '--runtime-home', str(home)],
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})


def assert_page(page, expected):
    assert page['ok'] is True
    rows = page['data']['tasks']
    assert [r['task_id'] for r in rows] == [r['task_id'] for r in expected]
    assert [r['state'] for r in rows] == [r['state'] for r in expected]
    assert all(set(r) == {'task_id', 'operation_id', 'state', 'tool', 'dispatch',
                          'cancel_pending', 'target', 'model'} for r in rows)
    assert RESULT_MARKER not in json.dumps(page)
    return page['data']


async def first_backend(home, run, bridge, record):
    with (run / 'first-backend.stderr.log').open('x', encoding='utf-8') as log:
        async with stdio_client(parameters(home), errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                for tool in tools.tools:
                    if tool.name in {'ansa_task_query', 'ansa_task_list'}:
                        assert tool.inputSchema['additionalProperties'] is False
                await wait_condition(bridge.state.connected.is_set)
                bridge.transport.request_bind()
                target = None
                for _ in range(100):
                    status = await call(client, 'ansa_connection_status')
                    target = status['data']['active_target']
                    if target:
                        break
                    await asyncio.sleep(.05)
                assert target and target['instance_id'] == bridge.state.instance_id
                snap = await call(client, 'ansa_model_snapshot', {'expected_target': target})
                model = {'epoch': snap['data']['model']['epoch']}
                before_model = json.loads(json.dumps(bridge.state.entities))
                async def submit(entity_id=1, ttl=300):
                    fields = {'operation_id': 'history-' + uuid.uuid4().hex,
                              'expected_target': target, 'expected_model': model,
                              'entity_id': entity_id, 'ttl_seconds': ttl}
                    value = await call(client, 'ansa_grid_read', fields)
                    assert value['task_id'], value
                    return value
                succeeded = []
                for _ in range(3):
                    submitted = await submit()
                    succeeded.append(await terminal(client, submitted['task_id'], 'succeeded'))
                failed = await submit(999)
                await terminal(client, failed['task_id'], 'failed')
                record('real_stdio_tcp_journal_success_and_failure', {'tools': len(tools.tools),
                    'succeeded_ids': [v['task_id'] for v in succeeded], 'failed_id': failed['task_id']})

                bridge.state.pause_claim.set()
                try:
                    queued = await submit()
                    listed = await call(client, 'ansa_task_list', {'state': 'queued'})
                    assert queued['task_id'] in [r['task_id'] for r in listed['data']['tasks']]
                    await call(client, 'ansa_task_cancel', {'task_id': queued['task_id']})
                    await terminal(client, queued['task_id'], 'cancelled')
                    expired = await submit(ttl=0)
                    await terminal(client, expired['task_id'], 'expired')
                finally:
                    bridge.state.pause_claim.clear()
                bridge.state.operation_entered.clear()
                bridge.state.operation_release.clear()
                try:
                    held = await submit(2)
                    await wait_condition(bridge.state.operation_entered.is_set)
                    running = await call(client, 'ansa_task_query', {'task_id': held['task_id']})
                    assert running['status'] == 'running'
                    listed = await call(client, 'ansa_task_list', {'state': 'running'})
                    assert held['task_id'] in [r['task_id'] for r in listed['data']['tasks']]
                finally:
                    bridge.state.operation_release.set()
                await terminal(client, held['task_id'], 'succeeded')
                record('queued_running_cancelled_expired_public_states', {
                    'queued_then_cancelled': queued['task_id'], 'expired': expired['task_id'], 'held': held['task_id']})

                original_rows = independent_rows(home)
                first_page = await call(client, 'ansa_task_list', {'limit': 2})
                data = assert_page(first_page, original_rows[:2])
                cursor = data['next_before_sequence']
                assert data['has_more'] is True and type(cursor) is int
                inserted = await submit()
                await terminal(client, inserted['task_id'], 'succeeded')
                seen = [r['task_id'] for r in data['tasks']]
                while cursor is not None:
                    page = await call(client, 'ansa_task_list', {'limit': 2, 'before_sequence': cursor})
                    expected = [r for r in original_rows if r['sequence'] < cursor][:2]
                    current = assert_page(page, expected)
                    seen.extend(r['task_id'] for r in current['tasks'])
                    cursor = current['next_before_sequence']
                assert seen == [r['task_id'] for r in original_rows] and len(seen) == len(set(seen))
                empty = await call(client, 'ansa_task_list', {'before_sequence': 1})
                assert assert_page(empty, [])['has_more'] is False
                for state in ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired', 'unknown'):
                    page = await call(client, 'ansa_task_list', {'state': state})
                    expected = [r for r in independent_rows(home) if r['state'] == state]
                    assert_page(page, expected)
                record('stable_cursor_new_insert_state_filters_empty_tail_and_summary_privacy', {
                    'old_page_ids': seen, 'new_id_excluded_from_continuation': inserted['task_id'],
                    'restart_cursor': first_page['data']['next_before_sequence']})

                chosen = succeeded[0]
                by_operation = await call(client, 'ansa_task_query', {'operation_id': chosen['operation_id']})
                by_both = await call(client, 'ansa_task_query', {'task_id': chosen['task_id'], 'operation_id': chosen['operation_id']})
                assert by_operation['task_id'] == by_both['task_id'] == chosen['task_id']
                missing = await call(client, 'ansa_task_query', {'task_id': 'not-present'})
                mismatch = await call(client, 'ansa_task_query', {'task_id': chosen['task_id'], 'operation_id': 'different-operation'})
                no_key = await call(client, 'ansa_task_query')
                assert missing['error']['code'] == mismatch['error']['code'] == 'TASK_NOT_FOUND'
                assert no_key['error']['code'] == 'QUERY_REJECTED'
                for tool, fields in [('ansa_task_list', {'limit': True}), ('ansa_task_list', {'limit': '2'}),
                                     ('ansa_task_list', {'before_sequence': False}),
                                     ('ansa_task_list', {'before_sequence': 'null'}),
                                     ('ansa_task_list', {'state': 'null'}),
                                     ('ansa_task_list', {'before_sequence': 2**63}),
                                     ('ansa_task_list', {'state': 'not-a-state'}),
                                     ('ansa_task_list', {'offset': 0}),
                                     ('ansa_task_query', {'task_id': ''}),
                                     ('ansa_task_query', {'task_id': True}),
                                     ('ansa_task_query', {'task_id': chosen['task_id'], 'extra': True})]:
                    response = await client.call_tool(tool, fields)
                    assert response.isError is True, (tool, fields, response)
                nullable = await call(client, 'ansa_task_list', {'state': None, 'before_sequence': None})
                assert_page(nullable, independent_rows(home))
                assert bridge.state.entities == before_model
                record('operation_and_consistent_dual_keys_distinct_errors_strict_schema', {
                    'task_id': chosen['task_id'], 'missing': missing, 'mismatch': mismatch, 'no_key': no_key})
                return {'task': chosen, 'target': target, 'model': model,
                        'identity': bridge.state.identity(),
                        'cursor': first_page['data']['next_before_sequence'],
                        'model_before': before_model}


async def restarted_backend(home, run, bridge, prior, unknown, record):
    executions = len(bridge.state.executions)
    with (run / 'restarted-backend.stderr.log').open('x', encoding='utf-8') as log:
        async with stdio_client(parameters(home), errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                await client.initialize()
                status = await call(client, 'ansa_connection_status')
                assert status['data']['backend_id'] != prior['target']['backend_id']
                assert status['data']['active_target'] is None
                chosen = prior['task']
                queried = await call(client, 'ansa_task_query', {'operation_id': chosen['operation_id']})
                assert queried['task_id'] == chosen['task_id'] and queried['data']['result'] == chosen['data']['result']
                page = await call(client, 'ansa_task_list', {'limit': 2, 'before_sequence': prior['cursor']})
                assert_page(page, [r for r in independent_rows(home) if r['sequence'] < prior['cursor']][:2])
                uncertain = await call(client, 'ansa_task_query', {'task_id': unknown['task_id']})
                assert uncertain['status'] == 'unknown' and uncertain['error']['code'] == 'TASK_UNKNOWN' and uncertain['warnings']
                unknown_page = await call(client, 'ansa_task_list', {'state': 'unknown'})
                assert [r['task_id'] for r in unknown_page['data']['tasks']] == [unknown['task_id']]
                assert len(bridge.state.executions) == executions and bridge.state.entities == prior['model_before']
                record('backend_restart_original_receipt_cursor_unknown_and_no_reexecution', {
                    'original_task_id': chosen['task_id'], 'unknown_task_id': unknown['task_id'],
                    'backend_id': status['data']['backend_id'], 'cursor_page': page,
                    'unknown': uncertain, 'model_execution_count_unchanged': executions})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-home', required=True, type=Path)
    args = parser.parse_args()
    home = args.runtime_home.resolve()
    if home.exists():
        raise RuntimeError('Acceptance requires a fresh private runtime home')
    run = ROOT / 'runtime/verification' / ('task-history-' + uuid.uuid4().hex)
    run.mkdir(parents=True)
    report = {'success': False, 'simulation': True, 'scope': __doc__,
              'started_at': datetime.now(timezone.utc).isoformat(), 'cases': []}
    def record(name, evidence):
        report['cases'].append({'name': name, 'ok': True, 'evidence': evidence})
        (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'case': name, 'ok': True}), flush=True)
    seed = Service(home)
    token = seed.token
    seed.close()
    bridge = None
    try:
        with simulated_read_operations():
            bridge = FakeBridge(home, token)
            prior = asyncio.run(first_backend(home, run, bridge, record))
            store = TaskStore(home / 'state.sqlite3')
            try:
                offline = {**prior['identity'], 'session_id': 'offline-' + uuid.uuid4().hex}
                target = {**prior['target'], 'session_id': offline['session_id']}
                request = {'tool': 'grid_read', 'params': {'id': 1, 'deck': 1},
                           'target': target, 'model': prior['model'], 'ttl_seconds': 300}
                unknown = store.create('history-offline-' + uuid.uuid4().hex, request, offline, 300)
                store.dispatch(unknown['task_id'])
                store.unknown(unknown['task_id'], 'Fixture original executor session is unavailable')
            finally:
                store.close()
            asyncio.run(restarted_backend(home, run, bridge, prior, unknown, record))
            report['success'] = True
    except BaseException as exc:
        import traceback
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        (run / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        if bridge is not None:
            bridge.close()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'success': True, 'simulation': True, 'cases': len(report['cases']),
                      'report': str(run / 'report.json')}), flush=True)


if __name__ == '__main__':
    main()
