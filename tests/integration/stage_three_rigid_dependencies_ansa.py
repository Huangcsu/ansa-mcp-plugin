"""独立 NASTRAN RBE2 依赖验收；核心检查与所属主窗口验收分别记录。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture


RIGID_IDS = [2001, 2002, 2003, 2101, 2102, 2103]
CORE_CASE_NAMES = [
    'schemas_owned_fixture_and_independent_dof_connectivity',
    'fault_clean_disjoint_dof_multiple_dependents_and_selection_controls',
    'explicit_hidden_rigid_and_no_card_or_visibility_changes',
    'invalid_scope_deck_schema_and_fix_rejected_without_changes',
    'original_operation_idempotency_and_leaf_entity_count_separation',
    'save_reopen_preserves_dependencies_and_rejects_stale_model',
]


def fixture_script(path):
    return """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('RBE2 fixture requires NASTRAN')
for kind in ('RBE2','RBE3','SHELL','SOLID','CONM2','FACE','__PROPERTIES__','__MATERIALS__'):
    if base.CollectEntities(deck,None,kind):
        raise RuntimeError('RBE2 fixture requires a fresh owned seed')
if len(base.CollectEntities(deck,None,'GRID')) != 1:
    raise RuntimeError('RBE2 fixture requires one owned seed GRID')
if base.InputNastran(FIXTURE_PATH,nodes_id='nooffset',elements_id='nooffset',
                    header='skip',model_action='merge_model') != 1:
    raise RuntimeError('RBE2 fixture import failed; do not replay without independent readback')
rigids = base.CollectEntities(deck,None,'RBE2')
if sorted(e._id for e in rigids) != [2001,2002,2003,2101,2102,2103]:
    raise RuntimeError('RBE2 fixture ID mismatch')
if base.Or(rigids) != 1:
    raise RuntimeError('RBE2 fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'rigid_ids':[2001,2002,2003,2101,2102,2103],
          'fault_ids':[2001,2002],'clean_ids':[2003],
          'disjoint_ids':[2101,2102],'multiple_dependents_ids':[2103]}
""".replace('FIXTURE_PATH', repr(str(path)))


def remote_rigid_readback(expected_pid, expected_arguments):
    """测试专用官方 gRPC 回读，不调用生产检查或原生依赖检查。"""
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError('Owned RBE2 oracle process/main thread changed')
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError('Owned RBE2 oracle deck changed')
    cards = {'GRID': [], 'RBE2': []}
    visible = {}
    for kind in cards:
        entities = sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id)
        for entity in entities:
            if entity.is_usable() is not True or entity.ansa_type(deck) != kind:
                raise RuntimeError('Oracle found an unusable or mismatched entity')
            labels = entity.card_fields(deck)
            fields = ('X1', 'X2', 'X3') if kind == 'GRID' else tuple(
                f for f in labels if f not in ('MBContainer', 'MBContainers'))
            if any(f not in labels for f in fields):
                raise RuntimeError('Oracle card fields unavailable')
            values = base.GetEntityCardValues(deck, entity, fields)
            if type(values) is not dict or any(f not in values for f in fields):
                raise RuntimeError('Oracle did not return the full requested card')
            cards[kind].append({'id': entity._id, 'type': kind, 'fields': values})
        visible[kind] = sorted(e._id for e in base.CollectEntities(
            deck, None, kind, filter_visible=True))
    connections = []
    for rigid in sorted(base.CollectEntities(deck, None, 'RBE2'), key=lambda e: e._id):
        connections.append({'rigid_id': rigid._id, 'grid_ids': sorted(
            e._id for e in base.CollectEntities(deck, rigid, 'GRID'))})
    return {'deck': deck, 'model_path': base.DataBaseName(), 'cards': cards,
            'connections': connections, 'visible_ids': visible}


def remote_refresh_rigid_dependencies(expected_pid, expected_arguments, expected_source_hash):
    """任务已结束时，仅在所属开发实例重载本批业务模块。"""
    import builtins
    import hashlib
    import importlib
    import os
    from pathlib import Path
    import sys
    import threading
    from ansa import session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError('Owned development process/main thread changed before reload')
    bridge = getattr(builtins, '_ansa_mcp_plugin_bridge', None)
    if bridge is None or bridge.state.journal.busy():
        raise RuntimeError('Module reload requires the owned bridge and no busy journal tasks')
    name = 'ansa_mcp_bridge.operations.rigid_dependencies'
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError('Previously executed RBE2 module is missing; no alternate import')
    source = Path(module.__file__).resolve()
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_source_hash:
        raise RuntimeError('Owned RBE2 source differs from the reviewed source')
    importlib.reload(module)
    return {'pid': os.getpid(), 'module': name, 'source': str(source),
            'source_sha256': expected_source_hash, 'reloaded': True, 'journal_busy': False}


def analytic_fixture(snapshot):
    """用独立卡片核算每个从节点的自由度归属，不使用原生检查。"""
    assert len(snapshot['cards']['GRID']) == 12
    rigids = {row['id']: row['fields'] for row in snapshot['cards']['RBE2']}
    grids = {row['id'] for row in snapshot['cards']['GRID']}
    assert sorted(rigids) == RIGID_IDS
    connections = {row['rigid_id']: row['grid_ids'] for row in snapshot['connections']}
    assignments = {}
    verified = []
    for rigid_id, card in rigids.items():
        assert type(card['No.of.Nodes']) is int and 2 <= card['No.of.Nodes'] <= 3
        gn = card['GN']
        gm = [card['GM'+str(i)] for i in range(1, card['No.of.Nodes'])]
        assert type(gn) is int and gn in grids and gn not in gm
        assert len(set(gm)) == len(gm) and all(type(n) is int and n in grids for n in gm)
        cm = str(card['CM'])
        assert cm and list(cm) == sorted(set(cm)) and all(c in '123456' for c in cm)
        assert connections[rigid_id] == sorted([gn, *gm])
        for node_id in gm:
            for dof in map(int, cm):
                assignments.setdefault((node_id, dof), []).append(rigid_id)
        verified.append({'rigid_id': rigid_id, 'independent_grid_id': gn,
                         'dependent_grid_ids': gm, 'cm': card['CM'],
                         'cm_components': list(map(int, cm)), 'node_count': card['No.of.Nodes']})
    conflicts = [{'grid_id': n, 'dof': d, 'rigid_ids': sorted(ids)}
                 for (n, d), ids in sorted(assignments.items()) if len(ids) > 1]
    assert conflicts == [{'grid_id': 1003, 'dof': d, 'rigid_ids': [2001, 2002]}
                         for d in range(1, 7)]
    assert rigids[2101]['CM'] == 123 and rigids[2102]['CM'] == 456
    assert rigids[2103]['No.of.Nodes'] == 3
    return {'connections': verified, 'duplicate_dof_assignments': conflicts}


def verify_result(result, rigid_ids, analytic, *, faulty):
    assert result['read_only'] is True and result['auto_fix'] is False
    assert result['native_check']['factory'] == 'base.checks.general.NastranDependency'
    assert result['native_check']['name'] == 'Dependency Nastran'
    assert result['native_check']['parameters'] == {}
    assert result['native_options'] == {'exec_mode': 'EXEC_ON_SELECTED',
        'report': 'REPORT_NONE', 'history': 'KEEP_OLD'}
    expected = {c['rigid_id']: c for c in analytic['connections']}
    grids = sorted({n for i in rigid_ids for n in [expected[i]['independent_grid_id'],
                                                  *expected[i]['dependent_grid_ids']]})
    assert result['scope'] == {'rigid_ids': rigid_ids, 'grid_ids': grids}
    assert len(result['connections']) == len(rigid_ids)
    for actual, rigid_id in zip(result['connections'], rigid_ids):
        assert all(actual[k] == v for k, v in expected[rigid_id].items())
        assert actual['rigid']['id'] == rigid_id and actual['rigid']['type'] == 'RBE2'
    leaves = []
    def visit(rows):
        for row in rows:
            assert row['is_fixed'] is False
            if row['issues']:
                visit(row['issues'])
            elif row['status'] in ('error', 'warning'):
                leaves.append(row)
    visit(result['reports'])
    count = 6 if faulty else 0
    assert result['count'] == result['stats']['issue_count'] == len(leaves) == count
    assert result['stats']['error'] == count and result['stats']['warning'] == 0
    assert result['affected_rigid_ids'] == ([2001, 2002] if faulty else [])
    assert result['affected_grid_ids'] == ([1003] if faulty else [])
    if faulty:
        assert sorted(row['description'] for row in leaves) == sorted(
            "dof %s set in 'm-set' more than once" % d for d in range(1, 7))


async def wait_for_gui(run, identity, expected, oracle, *, prefix='rigid-dependencies'):
    marker = run / (prefix + '-ui-confirmation.json')
    if marker.exists():
        raise RuntimeError('Refuse stale RBE2 GUI evidence')
    stage._json_write(run / (prefix + '-ui-instruction.json'), {
        'identity': identity, 'model_path': expected['model_path'],
        'visible_ids': expected['visible_ids'],
        'instruction': '所属主窗口四组刚性连接：左侧重复从节点、中间干净单元、分离自由度、右侧双从节点；GRID 12、RBE2 6。'})
    print(json.dumps({'status': 'waiting_for_rigid_dependencies_ui', 'marker': str(marker)}), flush=True)
    deadline = asyncio.get_running_loop().time() + 600
    while not marker.is_file():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('Keep the original RBE2 tasks/model for GUI-only followup')
        await asyncio.sleep(.5)
    visual = json.loads(marker.read_text(encoding='utf-8'))
    if (visual.get('checked') is not True or visual.get('pid') != identity['pid']
            or visual.get('process_start') != identity['process_start']
            or visual.get('model_path') != expected['model_path']
            or visual.get('fixture_visible') is not True):
        raise RuntimeError('RBE2 GUI evidence mismatches the owned process/model')
    assert oracle() == expected
    return visual


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _ = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'ansa_mcp_plugin', '--runtime-home', instructions['runtime_home']],
        env={**os.environ, 'PYTHONPATH': str(stage.PROJECT_ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    def oracle():
        return two.rpc(identity['port'], remote_rigid_readback, identity['pid'], identity['program_args'])
    def record(name, evidence):
        report['cases'].append({'name': name, 'ok': True, 'evidence': evidence})
        stage._json_write(run / 'rigid-dependencies-report.json', report)
        print(json.dumps({'case': name, 'ok': True}), flush=True)
    fixture_path = run / 'rigid-dependencies.nas'
    shutil.copyfile(stage.PROJECT_ROOT / 'tests/fixtures/rigid-dependencies.nas', fixture_path)
    with (run / 'rigid-dependencies-backend.stderr.log').open('w', encoding='utf-8') as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity['port'])
                schemas = await h.list_schema()
                fixture = await h.direct_result('ansa_script_execute', {'script': fixture_script(fixture_path)})
                await h.direct_result('ansa_model_save', {'path': str(run / 'rigid-dependencies-before.ansa')})
                before = oracle()
                analytic = analytic_fixture(before)
                record('schemas_owned_fixture_and_independent_dof_connectivity', {
                    'schemas': schemas, 'fixture': fixture, 'independent': before,
                    'analytic': analytic, 'backup': fingerprint(run / 'rigid-dependencies-before.ansa')})
                fields = {'deck': before['deck'], 'rigid_ids': RIGID_IDS}
                original_target, original_model = h.target.copy(), h.model.copy()
                op = 'rigid-dependencies-' + uuid.uuid4().hex
                submitted, completed = await h.task('ansa_rigid_dependencies', fields, operation_id=op)
                report['original_check_task'] = {'submitted': submitted, 'completed': completed, 'operation_id': op}
                stage._json_write(run / 'rigid-dependencies-report.json', report)
                result = completed['data']['result']
                verify_result(result, RIGID_IDS, analytic, faulty=True)
                controls = []
                for ids in ([2001, 2002], [2003], [2001], [2101, 2102], [2103],
                            [2002, 2001], [2103, 2102, 2101, 2003, 2002, 2001]):
                    answer = await h.direct_result('ansa_rigid_dependencies', {**fields, 'rigid_ids': ids})
                    verify_result(answer, ids, analytic, faulty={2001, 2002} <= set(ids))
                    controls.append(answer)
                assert oracle() == before
                record('fault_clean_disjoint_dof_multiple_dependents_and_selection_controls', {
                    'result': result, 'controls': controls})
                hidden_script = """from ansa import base, constants
rigid = base.GetEntity(constants.NASTRAN,'RBE2',2002)
if rigid is None or not rigid.is_usable() or base.Not(rigid,apply_on_contents=False) != 1:
    raise RuntimeError('Could not hide owned RBE2 control')
base.RedrawAll()
RESULT = {'hidden_rigid_id':2002}
"""
                await h.direct_result('ansa_script_execute', {'script': hidden_script})
                hidden = oracle()
                assert hidden['cards'] == before['cards'] and 2002 not in hidden['visible_ids']['RBE2']
                hidden_result = await h.direct_result('ansa_rigid_dependencies', fields)
                verify_result(hidden_result, RIGID_IDS, analytic, faulty=True)
                assert oracle() == hidden
                await h.direct_result('ansa_script_execute', {'script': """from ansa import base, constants
if base.Or(base.CollectEntities(constants.NASTRAN,None,'RBE2')) != 1:
    raise RuntimeError('Could not restore owned RBE2 display')
base.RedrawAll()
base.ZoomAll()
RESULT = {'restored':True}
"""})
                assert oracle() == before
                record('explicit_hidden_rigid_and_no_card_or_visibility_changes', hidden_result)
                rejected = []
                for changes in ({'rigid_ids': [2001, 99999999]}, {'deck': before['deck'] + 1}):
                    rejected.append(await h.direct_result('ansa_rigid_dependencies', {**fields, **changes}, expected='failed'))
                for changes in ({'rigid_ids': []}, {'rigid_ids': [2001, 2001]},
                                {'rigid_ids': [True]}, {'deck': True},
                                {'rigid_ids': ['2001']}, {'fix': True}):
                    answer = await client.call_tool('ansa_rigid_dependencies', {
                        'operation_id': uuid.uuid4().hex, 'expected_target': h.target,
                        'expected_model': h.model, **fields, **changes})
                    assert answer.isError is True
                    rejected.append({'input': changes, 'is_error': True})
                assert oracle() == before
                record('invalid_scope_deck_schema_and_fix_rejected_without_changes', rejected)
                repeated = await h.call('ansa_rigid_dependencies', {'operation_id': op,
                    'expected_target': original_target, 'expected_model': original_model, **fields})
                assert repeated['task_id'] == submitted['task_id'] and repeated['data']['result'] == result
                assert oracle() == before
                record('original_operation_idempotency_and_leaf_entity_count_separation', {
                    'task_id': submitted['task_id'], 'issue_count': result['count'],
                    'affected_rigid_ids': result['affected_rigid_ids'], 'affected_grid_ids': result['affected_grid_ids']})
                saved_path = run / 'rigid-dependencies-after.ansa'
                await h.direct_result('ansa_model_save', {'path': str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result('ansa_model_open', {'path': str(saved_path),
                    'backup_path': str(run / 'rigid-dependencies-reopen-backup.ansa')})
                await h.snapshot_context()
                reopened = oracle()
                assert {k: v for k, v in reopened.items() if k != 'model_path'} == {
                    k: v for k, v in before.items() if k != 'model_path'}
                after = await h.direct_result('ansa_rigid_dependencies', fields)
                verify_result(after, RIGID_IDS, analytic, faulty=True)
                assert oracle() == reopened and fingerprint(saved_path) == saved
                assert h.model['epoch'] != original_model['epoch']
                stale = await h.call('ansa_rigid_dependencies', {'operation_id': uuid.uuid4().hex,
                    'expected_target': h.target, 'expected_model': original_model, **fields}, allow_error=True)
                assert stale['ok'] is False and stale['task_id'] is None
                record('save_reopen_preserves_dependencies_and_rejects_stale_model', {
                    'independent': reopened, 'saved': saved, 'stale': stale})
                report['core_acceptance'] = True
                stage._json_write(run / 'rigid-dependencies-report.json', report)
                if wait_ui:
                    visual = await wait_for_gui(run, identity, reopened, oracle)
                    report['visual_acceptance'] = True
                    record('owned_main_window_and_post_ui_independent_readback', visual)
    report['success'] = True


async def verify_gui_only(run, instructions, identity, helpers, report, original):
    """只查询原任务和回读所属模型；不重放导入、检查、保存或打开操作。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _ = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'ansa_mcp_plugin', '--runtime-home', instructions['runtime_home']],
        env={**os.environ, 'PYTHONPATH': str(stage.PROJECT_ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    expected = original['cases'][-1]['evidence']['independent']
    saved = original['cases'][-1]['evidence']['saved']
    def oracle():
        return two.rpc(identity['port'], remote_rigid_readback, identity['pid'], identity['program_args'])
    with (run / (report['gui_prefix'] + '-backend.stderr.log')).open('x', encoding='utf-8') as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity['port'])
                prior = original['original_check_task']
                task_id = prior['submitted']['task_id']
                queried = await h.call('ansa_task_query', {'task_id': task_id})
                assert queried['status'] == 'succeeded' and queried['task_id'] == task_id
                assert queried['data']['result'] == prior['completed']['data']['result']
                assert oracle() == expected and fingerprint(Path(saved['path'])) == saved
                visual = await wait_for_gui(run, identity, expected, oracle, prefix=report['gui_prefix'])
                assert fingerprint(run / 'rigid-dependencies-report.json') == report['original_report']
                assert fingerprint(Path(saved['path'])) == saved
                report['cases'].append({'name': 'original_task_query_and_owned_gui_without_replay',
                    'ok': True, 'evidence': {'task_id': task_id, 'queried': queried, 'visual': visual}})
                report['visual_acceptance'] = True
    report['success'] = True


def gui_followup_source(run):
    source_path = run / 'rigid-dependencies-report.json'
    original = json.loads(source_path.read_text(encoding='utf-8'))
    def leaves(error):
        return sum((leaves(child) for child in error['exceptions']), []) if 'exceptions' in error else [error]
    errors = leaves(original.get('exception_tree', {}))
    clean_core = original.get('success') is True and not original.get('error')
    gui_timeout = (original.get('success') is False and len(errors) == 1
                   and errors[0].get('type') == 'TimeoutError'
                   and errors[0].get('message') == 'Keep the original RBE2 tasks/model for GUI-only followup')
    if (original.get('core_acceptance') is not True or original.get('visual_acceptance') is not False
            or [c['name'] for c in original['cases']] != CORE_CASE_NAMES
            or any(c['ok'] is not True for c in original['cases'])
            or not (clean_core or gui_timeout) or original.get('cleanup')):
        raise RuntimeError('GUI followup requires six passed core cases and the still-owned process')
    for previous_path in run.glob('rigid-dependencies-followup*report.json'):
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        error = previous.get('error')
        finished_at = previous.get('finished_at')
        ended = False
        if type(finished_at) is str and finished_at:
            try:
                ended = datetime.fromisoformat(finished_at).utcoffset() is not None
            except ValueError:
                pass
        explicit_error = (type(error) is dict and type(error.get('type')) is str
                          and bool(error['type']) and type(error.get('message')) is str)
        if (previous.get('success') is not False or previous.get('visual_acceptance') is not False
                or not ended or not explicit_error):
            raise RuntimeError('GUI already accepted or prior followup is uncertain; inspect its report')
    return original, fingerprint(source_path)


def new_gui_attempt(run):
    prefix = 'rigid-dependencies-followup-' + uuid.uuid4().hex
    report_name = prefix + '-report'
    if (run / (report_name + '.json')).exists():
        raise RuntimeError('Refuse overwriting a prior GUI attempt')
    return prefix, report_name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ansa-root', 'runtime-home', 'run-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--wait-ui', action='store_true')
    parser.add_argument('--keep-ansa', action='store_true')
    parser.add_argument('--gui-only', action='store_true')
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    if not args.gui_only and ((run / 'seed-baseline.ansa').exists()
                             or (run / 'rigid-dependencies-report.json').exists()):
        raise RuntimeError('Use a fresh run; query original tasks before considering replay')
    original, source_fingerprint = gui_followup_source(run) if args.gui_only else (None, None)
    gui_prefix, report_name = new_gui_attempt(run) if args.gui_only else (None, 'rigid-dependencies-report')
    report = {'scope': 'ANSA 2026.1 Windows GUI/NASTRAN 显式 RBE2 只读依赖检查',
              'started_at': datetime.now(timezone.utc).isoformat(), 'success': False,
              'cases': [], 'core_acceptance': False, 'visual_acceptance': False}
    if args.gui_only:
        report['gui_prefix'] = gui_prefix
    identity = helpers = None
    try:
        if args.gui_only:
            import grpc
            from ansa_mcp_shared.identity import same_process_alive
            one, two = stage._import_runtime_helpers()
            helpers = one, two, stage._bounded_grpc(grpc, one.DeadlineInterceptor)
            identity = original['identity']
            checked = two.rpc(identity['port'], one.remote_basic)
            stage._verify_program_arguments(checked, Path(instructions['translator']), identity['port'])
            if (checked['pid'] != identity['pid'] or checked['program_args'] != identity['program_args']
                    or not same_process_alive(identity['pid'], identity['process_start'])):
                raise RuntimeError('GUI followup refuses changed owned process identity')
            report['original_report'] = source_fingerprint
            report['core_acceptance'] = True
        else:
            identity, _, _, helpers = stage._prepare_owned_ansa(run, instructions)
        report['identity'] = identity
        stage._json_write(run / (report_name + '.json'), report)
        if args.gui_only:
            asyncio.run(verify_gui_only(run, instructions, identity, helpers, report, original))
        else:
            report['fixture'] = helpers[1].rpc(identity['port'], remote_initialize_fixture,
                identity['pid'], identity['program_args'], identity['seed_path'], identity['seed_entity_id'])
            asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        report['exception_tree'] = stage._exception_tree(exc)
        (run / (report_name + '.error.log')).write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        if identity is not None and helpers is not None and report['success'] and not args.keep_ansa:
            try:
                report['cleanup'] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report['success'] = False
                report['cleanup_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        stage._json_write(run / (report_name + '.json'), report)
    if not report['success']:
        raise RuntimeError('RBE2 core acceptance or owned process cleanup failed')
    print(json.dumps({'success': True, 'core_acceptance': report['core_acceptance'],
                     'visual_acceptance': report['visual_acceptance'],
                     'report': str(run / (report_name + '.json'))}), flush=True)


if __name__ == '__main__':
    main()
