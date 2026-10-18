"""独立 NASTRAN 显式 SHELL 失败统计；记录当前质量准则，不修复。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import sys
import shutil
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture
from stage_three_sharp_edges_ansa import remote_sharp_edges_readback


FIXTURE_SCRIPT = """from ansa import base, constants
if base.CurrentDeck() != constants.NASTRAN:
    raise RuntimeError('failed elements fixture requires NASTRAN')
deck = base.CurrentDeck()
for kind in ('SHELL', 'SOLID', 'CONM2', 'FACE', '__PROPERTIES__', '__MATERIALS__'):
    if base.CollectEntities(deck, None, kind):
        raise RuntimeError('failed elements fixture requires a fresh owned seed')
if len(base.CollectEntities(deck, None, 'GRID')) != 1:
    raise RuntimeError('failed elements fixture requires one owned seed GRID')

def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None or any(key not in entity.card_fields(deck) for key in fields):
        raise RuntimeError('failed elements fixture cannot verify '+kind)
    values = base.GetEntityCardValues(deck, entity, tuple(fields))
    if any(values.get(key) != value for key,value in fields.items()):
        raise RuntimeError('failed elements fixture card mismatch '+repr(values))
    return entity

mat = create('MAT1', {'DEFINED':'YES','E':200000.,'NU':.3,'RHO':.01})
prop = create('PSHELL', {'MID1':mat._id,'MID2':mat._id,'MID3':mat._id,'T':2.,'NSM':0.})
def shell(points):
    nodes = [create('GRID', dict(zip(('X1','X2','X3'), p))) for p in points]
    fields = {'type':'CQUAD4' if len(nodes)==4 else 'CTRIA3','PID':prop._id}
    fields.update({'G'+str(i+1):node._id for i,node in enumerate(nodes)})
    return create('SHELL', fields)._id
good_quad = shell([(0.,0.,0.),(10.,0.,0.),(10.,10.,0.),(0.,10.,0.)])
bad_quad = shell([(25.,0.,0.),(125.,0.,0.),(125.,.1,0.),(25.,.1,0.)])
good_tri = shell([(0.,25.,0.),(10.,25.,0.),(5.,33.660254037844386,0.)])
bad_tri = shell([(25.,25.,0.),(125.,25.,0.),(25.,25.1,0.)])
outside = shell([(25.,50.,0.),(125.,50.,0.),(125.,50.05,0.),(25.,50.05,0.)])
if base.Or(base.CollectEntities(deck, None, 'SHELL')) != 1:
    raise RuntimeError('failed elements fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'good_ids':[good_quad,good_tri], 'bad_ids':[bad_quad,bad_tri],
          'outside_ids':[outside]}
"""


def remote_quality_state(expected_pid, expected_arguments, criteria_path):
    import os
    from pathlib import Path
    import threading
    from ansa import base, mesh, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError('Quality oracle requires the exact owned ANSA main thread')
    # Only the isolated verification fixture writes this diagnostic export.
    # The product operation neither exports nor loads quality criteria.
    if mesh.SaveQualityCriteria(criteria_path) != 1:
        raise RuntimeError('Could not independently snapshot the current quality criteria')
    content = Path(criteria_path).read_text(encoding='utf-8', errors='strict')
    return {'criteria_text':content,
            'check_parameters':base.checks.mesh.MeshQuality().parameters(),
            'aspect_values':{str(e._id):base.ElementQuality(e, 'ASPECT')
                             for e in base.CollectEntities(base.CurrentDeck(), None, 'SHELL')}}


def remote_load_test_criteria(expected_pid, expected_arguments, criteria_path, expected_text):
    import builtins
    import os
    from pathlib import Path
    import threading
    from ansa import mesh, session
    if (os.getpid()!=expected_pid or list(session.ProgramArguments())!=expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError('Test criteria change requires exact owned ANSA identity')
    bridge = getattr(builtins, '_ansa_mcp_plugin_bridge', None)
    if bridge is None or bridge.state.journal.busy():
        raise RuntimeError('Test criteria change requires an idle owned queue')
    if Path(criteria_path).read_text(encoding='utf-8')!=expected_text:
        raise RuntimeError('Test criteria file fingerprint changed')
    if mesh.ReadQualityCriteria(criteria_path)!=1:
        raise RuntimeError('Native criteria file did not load')
    return {'loaded':True,'path':criteria_path}


def active_criteria(text):
    names = ('aspect ratio [shells]', 'min angle trias [shells]')
    lines = text.splitlines(keepends=True)
    changed = []
    for index,line in enumerate(lines):
        name = line.split('=',1)[0].strip()
        if name in names:
            assert '= OFF,' in line and name not in changed
            lines[index] = line.replace('= OFF,','= ON,',1)
            changed.append(name)
    assert set(changed)==set(names)
    assert 'failed_index_shells' in text and 'NASTRAN' in text and 'IDEAS' in text
    return ''.join(lines)


def analytic_shapes(snapshot):
    points = {row['id']:tuple(row['fields'][name] for name in ('X1','X2','X3'))
              for row in snapshot['cards']['GRID']}
    rows = []
    for shell in snapshot['cards']['SHELL']:
        card = shell['fields']
        count = 4 if card['type']=='CQUAD4' else 3
        p = [points[card['G'+str(i)]] for i in range(1,count+1)]
        lengths = [math.dist(a,b) for a,b in zip(p,p[1:]+p[:1])]
        rows.append({'id':shell['id'], 'edge_lengths':lengths,
                     'edge_ratio':max(lengths)/min(lengths)})
    return rows


def verify_result(result, snapshot, ids, expected_failed, quality_state):
    assert result['api']=='ansa.base.checks.mesh.MeshQuality'
    assert result['check']=='failed_elements' and result['criteria_source']=='current_session'
    assert result['options']=={'use_criteria':'Use global criteria'}
    criteria = quality_state['check_parameters']['quality_criteria']
    assert result['criteria_snapshot']=={'use_criteria':'Use global criteria',
        'quality_criteria':criteria,'sha256':hashlib.sha256(criteria.encode('utf-8')).hexdigest()}
    assert result['scope']=='explicit' and result['read_only'] is True and result['auto_fix'] is False
    assert [e['id'] for e in result['entities']]==ids
    assert result['failed_entity_ids']==sorted(expected_failed)
    assert result['failed_element_count']==len(set(expected_failed))
    counts = {'ok':0,'error':0,'warning':0}
    failures = set()
    nodes = set(result['connection_context']['node_ids'])
    shell_cards = {row['id']:row['fields'] for row in snapshot['cards']['SHELL']}
    expected_nodes = {value for entity_id in ids for key,value in shell_cards[entity_id].items()
                      if key in ('G1','G2','G3','G4')}
    assert nodes==expected_nodes
    def visit(row):
        affected = set()
        for entity in row['entities']:
            assert entity['deck']==snapshot['deck']
            if entity['type']=='SHELL':
                assert entity['id'] in ids
                affected.add(entity['id'])
            else:
                assert entity['type']=='GRID' and entity['id'] in nodes
        if row['children']:
            for child in row['children']:
                visit(child)
        else:
            assert row['status'] in counts
            counts[row['status']] += 1
            if row['status'] in ('error','warning'):
                assert affected
                failures.update(affected)
    for row in result['reports']:
        visit(row)
    assert sorted(failures)==result['failed_entity_ids']
    assert counts==result['issue_counts'] and sum(counts.values())==result['issue_count']
    assert result['report_count']==len(result['reports'])==result['native_report_count']


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one,two,_ = helpers
    params = StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(stage.PROJECT_ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    def oracle():
        return two.rpc(identity['port'],remote_sharp_edges_readback,identity['pid'],identity['program_args'])
    def quality():
        return two.rpc(identity['port'],remote_quality_state,identity['pid'],identity['program_args'],
                       str(run/'current-criteria.ansa_qual'))
    def record(name,evidence):
        report['cases'].append({'name':name,'ok':True,'evidence':evidence})
        stage._json_write(run/'failed-elements-report.json',report)
        print(json.dumps({'case':name,'ok':True}),flush=True)
    with (run/'failed-elements-backend.stderr.log').open('w',encoding='utf-8') as log:
        async with stdio_client(params,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                await stage._bind_for_run(h,identity,one,two,identity['port'])
                schemas = await h.list_schema()
                fixture = await h.direct_result('ansa_script_execute',{'script':FIXTURE_SCRIPT})
                await h.direct_result('ansa_model_save',{'path':str(run/'failed-elements-before.ansa')})
                before,original_quality = oracle(),quality()
                # Native ReadQualityCriteria may normalize legacy disabled options.
                # Preserve the raw initial file, then require an exact stable roundtrip
                # before treating the normalized export as the test restore baseline.
                original_path = run/'criteria-original.ansa_qual'
                shutil.copy2(run/'current-criteria.ansa_qual',original_path)
                initial_load = two.rpc(identity['port'],remote_load_test_criteria,
                    identity['pid'],identity['program_args'],str(original_path),original_quality['criteria_text'])
                baseline_quality = quality()
                assert oracle()==before and baseline_quality['aspect_values']==original_quality['aspect_values']
                baseline_path = run/'criteria-baseline.ansa_qual'
                shutil.copy2(run/'current-criteria.ansa_qual',baseline_path)
                stable_load = two.rpc(identity['port'],remote_load_test_criteria,
                    identity['pid'],identity['program_args'],str(baseline_path),baseline_quality['criteria_text'])
                assert quality()==baseline_quality and oracle()==before
                groups = fixture['result']
                ids = groups['good_ids']+groups['bad_ids']
                shapes = analytic_shapes(before)
                assert len(before['cards']['GRID'])==19 and len(before['cards']['SHELL'])==5
                ratios = {row['id']:row['edge_ratio'] for row in shapes}
                assert all(ratios[i]<1.01 for i in groups['good_ids'])
                assert all(ratios[i]>999 for i in groups['bad_ids']+groups['outside_ids'])
                record('schemas_owned_fixture_geometry_and_current_criteria_backup',{
                    'schemas':schemas,'fixture':fixture,'independent':before,'shapes':shapes,
                    'quality':baseline_quality,'original_quality':original_quality,
                    'initial_criteria_load':initial_load,'stable_criteria_load':stable_load,
                    'original_criteria_backup':fingerprint(original_path),
                    'backup':fingerprint(run/'failed-elements-before.ansa')})
                fields = {'deck':before['deck'],'entity_ids':ids}
                assert not any('= ON,' in line for line in baseline_quality['criteria_text'].splitlines())
                disabled = await h.direct_result('ansa_mesh_failed_elements',fields)
                verify_result(disabled,before,ids,[],baseline_quality)
                assert oracle()==before and quality()==baseline_quality
                test_text = active_criteria(baseline_quality['criteria_text'])
                test_path = run/'criteria-controlled.ansa_qual'
                test_path.write_text(test_text,encoding='utf-8',newline='\n')
                loaded = two.rpc(identity['port'],remote_load_test_criteria,identity['pid'],identity['program_args'],
                                 str(test_path),test_text)
                current_quality = quality()
                assert oracle()==before
                assert all(name+' ' in current_quality['criteria_text'] for name in ('aspect ratio [shells]','min angle trias [shells]'))
                record('disabled_criteria_zero_and_explicit_test_criteria_loaded',{
                    'disabled':disabled,'loaded':loaded,'active_quality':current_quality,
                    'original_criteria_backup':fingerprint(run/'criteria-baseline.ansa_qual')})
                op = 'failed-elements-'+uuid.uuid4().hex
                original_target,original_model = h.target.copy(),h.model.copy()
                submitted,completed = await h.task('ansa_mesh_failed_elements',fields,operation_id=op)
                report['original_check_task']={'submitted':submitted,'completed':completed}
                stage._json_write(run/'failed-elements-report.json',report)
                result = completed['data']['result']
                verify_result(result,before,ids,groups['bad_ids'],current_quality)
                controls = []
                for scope in (groups['good_ids'],[groups['bad_ids'][0]],[groups['bad_ids'][1]]):
                    response = await h.direct_result('ansa_mesh_failed_elements',{**fields,'entity_ids':scope})
                    verify_result(response,before,scope,set(scope)&set(groups['bad_ids']),current_quality)
                    controls.append(response)
                assert oracle()==before and quality()==current_quality
                record('mixed_scope_distinct_failures_clean_quad_and_triangle_subsets',{'result':result,'controls':controls})
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                display = {'action':'entity_visibility','deck':before['deck'],'options':{
                    'entity_type':'SHELL','entity_ids':groups['bad_ids'],'action':'hide',
                    'expected_visible':visibility['visible_ids'],'apply_on_contents':False}}
                await h.direct_result('ansa_model_inspect',display)
                hidden = oracle()
                assert hidden['cards']==before['cards'] and not set(hidden['visible_ids']['SHELL'])&set(groups['bad_ids'])
                hidden_result = await h.direct_result('ansa_mesh_failed_elements',fields)
                verify_result(hidden_result,hidden,ids,groups['bad_ids'],current_quality)
                assert oracle()==hidden and quality()==current_quality
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                await h.direct_result('ansa_model_inspect',{**display,'options':{**display['options'],
                    'action':'show_also','expected_visible':visibility['visible_ids']}})
                assert oracle()==before
                record('hidden_explicit_shells_and_criteria_preserved',hidden_result)
                rejected = []
                for changes in ({'entity_ids':[ids[0],99999999]},{'entity_ids':[ids[0],ids[0]]},
                                {'deck':before['deck']+1}):
                    rejected.append(await h.direct_result('ansa_mesh_failed_elements',{**fields,**changes},expected='failed'))
                for changes in ({'entity_ids':[]},{'entity_ids':[True]},{'deck':True},{'deck':'1'}):
                    response = await client.call_tool('ansa_mesh_failed_elements',{'operation_id':uuid.uuid4().hex,
                        'expected_target':h.target,'expected_model':h.model,**fields,**changes})
                    assert response.isError is True
                    rejected.append({'input':changes,'is_error':True})
                assert oracle()==before and quality()==current_quality
                record('invalid_ids_types_and_deck_reject_without_changes',rejected)
                repeated = await h.call('ansa_mesh_failed_elements',{'operation_id':op,
                    'expected_target':original_target,'expected_model':original_model,**fields})
                assert repeated['task_id']==submitted['task_id'] and repeated['data']['result']==result
                assert oracle()==before and quality()==current_quality
                record('original_operation_idempotency_and_native_criteria_preserved',{
                    'task_id':submitted['task_id'],'repeated_task_id':repeated['task_id']})
                saved_path = run/'failed-elements-after.ansa'
                await h.direct_result('ansa_model_save',{'path':str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result('ansa_model_open',{'path':str(saved_path),
                    'backup_path':str(run/'failed-elements-reopen-backup.ansa')})
                await h.snapshot_context()
                reopened = oracle()
                assert {k:v for k,v in reopened.items() if k!='model_path'}=={
                    k:v for k,v in before.items() if k!='model_path'}
                after = await h.direct_result('ansa_mesh_failed_elements',fields)
                verify_result(after,reopened,ids,groups['bad_ids'],current_quality)
                assert oracle()==reopened and quality()==current_quality and fingerprint(saved_path)==saved
                assert h.model['epoch']!=original_model['epoch']
                stale = await h.call('ansa_mesh_failed_elements',{'operation_id':'failed-stale-'+uuid.uuid4().hex,
                    'expected_target':h.target,'expected_model':original_model,**fields},allow_error=True)
                assert stale['ok'] is False and stale['task_id'] is None
                record('saved_model_reopen_preserves_failures_and_rejects_old_context',{
                    'independent':reopened,'result':after,'saved':saved,'stale':stale})
                report['core_acceptance'] = True
                stage._json_write(run/'failed-elements-report.json',report)
                if wait_ui:
                    marker = run/'failed-elements-ui-confirmation.json'
                    if marker.exists():
                        raise RuntimeError('Refuse stale failed elements GUI evidence')
                    stage._json_write(run/'failed-elements-ui-instruction.json',{'identity':identity,
                        'model_path':reopened['model_path'],'visible_ids':reopened['visible_ids'],
                        'instruction':'所属主窗口显示五个壳：良好四边形/等边三角形、细长四边形/三角形及范围外细长壳；GRID 19、ELEMENT 5。'})
                    print(json.dumps({'status':'waiting_for_failed_elements_ui','run_dir':str(run)}),flush=True)
                    deadline = asyncio.get_running_loop().time()+stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time()>=deadline:
                            raise TimeoutError('Keep original failed elements task for GUI inspection')
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding='utf-8'))
                    if (visual.get('checked') is not True or visual.get('pid')!=identity['pid']
                            or visual.get('process_start')!=identity['process_start']
                            or visual.get('model_path')!=reopened['model_path']
                            or visual.get('fixture_visible') is not True):
                        raise RuntimeError('Failed elements GUI evidence mismatches owned model')
                    assert oracle()==reopened and quality()==current_quality
                    report['visual_acceptance'] = True
                    record('owned_main_window_fixture_and_post_ui_independent_readback',visual)
                restored = two.rpc(identity['port'],remote_load_test_criteria,identity['pid'],identity['program_args'],
                                   str(run/'criteria-baseline.ansa_qual'),baseline_quality['criteria_text'])
                assert quality()==baseline_quality and oracle()==reopened
                record('original_session_quality_criteria_restored',restored)
    report['success'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ansa-root','runtime-home','run-dir'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--wait-ui',action='store_true')
    parser.add_argument('--keep-ansa',action='store_true')
    args = parser.parse_args()
    run,instructions = stage._load_run(args)
    if (run/'seed-baseline.ansa').exists() or (run/'failed-elements-report.json').exists():
        raise ValueError('只接受新准备目录；未知结果先查询原任务，不重放夹具')
    report = {'scope':'ANSA 2026.1 Windows GUI/NASTRAN 显式 SHELL 当前准则失败统计',
              'started_at':datetime.now(timezone.utc).isoformat(),'success':False,'cases':[],
              'core_acceptance':False,'visual_acceptance':False}
    identity = helpers = None
    try:
        identity,_ansa,_grpc,helpers = stage._prepare_owned_ansa(run,instructions)
        report['identity'] = identity
        stage._json_write(run/'failed-elements-report.json',report)
        report['fixture'] = helpers[1].rpc(identity['port'],remote_initialize_fixture,
            identity['pid'],identity['program_args'],identity['seed_path'],identity['seed_entity_id'])
        asyncio.run(verify(run,instructions,identity,helpers,report,wait_ui=args.wait_ui))
    except BaseException as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc)}
        report['exception_tree']=stage._exception_tree(exc)
        (run/'failed-elements-report.error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        if identity is not None and helpers is not None and report['success'] and not args.keep_ansa:
            try:
                report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
            except Exception as exc:
                report['success']=False
                report['cleanup_error']={'type':type(exc).__name__,'message':str(exc)}
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        stage._json_write(run/'failed-elements-report.json',report)
    if not report['success']:
        raise RuntimeError('Failed element acceptance or owned process cleanup failed')
    print(json.dumps({'success':True,'report':str(run/'failed-elements-report.json')}),flush=True)


if __name__=='__main__':
    main()
