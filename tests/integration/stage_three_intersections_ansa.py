"""独立 NASTRAN 两组壳相交检查；先查项目 API Skill，再运行所属窗口验收。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture
from stage_three_sharp_edges_ansa import remote_sharp_edges_readback


FIXTURE_SCRIPT = """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('intersection fixture requires NASTRAN')
for kind in ('SHELL','SOLID','CONM2','FACE','__PROPERTIES__','__MATERIALS__'):
    if base.CollectEntities(deck,None,kind):
        raise RuntimeError('intersection fixture requires a fresh owned seed')
if len(base.CollectEntities(deck,None,'GRID')) != 1:
    raise RuntimeError('intersection fixture requires one owned seed GRID')
def create(kind, fields):
    entity = base.CreateEntity(deck,kind,fields)
    if entity is None or any(k not in entity.card_fields(deck) for k in fields):
        raise RuntimeError('intersection fixture fields unavailable')
    values = base.GetEntityCardValues(deck,entity,tuple(fields))
    if any(values.get(k) != v for k,v in fields.items()):
        raise RuntimeError('intersection fixture card mismatch')
    return entity
mat = create('MAT1',{'DEFINED':'YES','E':200000.,'NU':.3,'RHO':.01})
prop = create('PSHELL',{'MID1':mat._id,'MID2':mat._id,'MID3':mat._id,'T':1.,'NSM':0.})
def shell(points):
    nodes = [create('GRID',dict(zip(('X1','X2','X3'),p))) for p in points]
    fields = {'type':'CQUAD4' if len(nodes)==4 else 'CTRIA3','PID':prop._id}
    fields.update({'G'+str(i+1):n._id for i,n in enumerate(nodes)})
    return create('SHELL',fields)._id
quad_a = shell([(0.,0.,0.),(10.,0.,0.),(10.,10.,0.),(0.,10.,0.)])
quad_b = shell([(5.,0.,-5.),(5.,10.,-5.),(5.,10.,5.),(5.,0.,5.)])
tria_a = shell([(30.,0.,0.),(40.,0.,0.),(35.,10.,0.)])
tria_b = shell([(35.,-1.,-5.),(35.,11.,-5.),(35.,5.,5.)])
outside = shell([(7.,0.,-5.),(7.,10.,-5.),(7.,10.,5.),(7.,0.,5.)])
clean = shell([(60.,0.,0.),(70.,0.,0.),(70.,10.,0.),(60.,10.,0.)])
if base.Or(base.CollectEntities(deck,None,'SHELL')) != 1:
    raise RuntimeError('intersection fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'first_ids':[quad_a,tria_a],'second_ids':[quad_b,tria_b],
          'outside_id':outside,'clean_id':clean}
"""


def analytic_fixture(snapshot, groups):
    """只由连接卡片和坐标核算本夹具的两条交线，不调用相交检查。"""
    grids = {r['id']:tuple(r['fields'][k] for k in ('X1','X2','X3'))
             for r in snapshot['cards']['GRID']}
    shells = {r['id']:r['fields'] for r in snapshot['cards']['SHELL']}
    def points(entity_id):
        card = shells[entity_id]
        n = 4 if card['type']=='CQUAD4' else 3
        return [grids[card['G'+str(i+1)]] for i in range(n)]
    qa,ta = groups['first_ids']
    qb,tb = groups['second_ids']
    assert points(qa)==[(0.,0.,0.),(10.,0.,0.),(10.,10.,0.),(0.,10.,0.)]
    assert points(qb)==[(5.,0.,-5.),(5.,10.,-5.),(5.,10.,5.),(5.,0.,5.)]
    assert points(ta)==[(30.,0.,0.),(40.,0.,0.),(35.,10.,0.)]
    triangle = points(tb)
    assert triangle==[(35.,-1.,-5.),(35.,11.,-5.),(35.,5.,5.)]
    # 两个三角形在 x=35,z=0 相交：水平三角形 y∈[0,10]，竖直三角形 y∈[2,8]。
    cuts = []
    for a,b in zip(triangle,triangle[1:]+triangle[:1]):
        if a[2]*b[2]<0:
            t = -a[2]/(b[2]-a[2])
            cuts.append(tuple(a[i]+t*(b[i]-a[i]) for i in range(3)))
    assert sorted(cuts)==[(35.,2.,0.),(35.,8.,0.)]
    assert all(p[0]>=60 for p in points(groups['clean_id']))
    assert all(p[0]==7 for p in points(groups['outside_id']))
    return {'quad_intersection':[(5.,0.,0.),(5.,10.,0.)],
            'triangle_intersection':sorted(cuts),'outside_crosses_quad':True,
            'clean_minimum_x':60.0}


def verify_result(result, first, second, expected):
    assert result['check']=='intersections' and result['read_only'] is True
    assert result['auto_fix'] is False
    assert result['native_options']=={'mode':1,'factor':0.0,'fast_run':0}
    assert result['scope']['first_ids']==first and result['scope']['second_ids']==second
    hit = result['intersected']
    ids = sorted(expected)
    assert hit['entity_ids']==ids and hit['count']==len(ids)
    assert hit['first_ids']==sorted(set(ids)&set(first))
    assert hit['second_ids']==sorted(set(ids)&set(second))
    assert type(result['native_count']) is int and result['native_count']>=0
    assert (result['native_count']==0)==(len(ids)==0)
    assert all(r['type']=='SHELL' for r in hit['references'])
    assert sorted(r['id'] for r in hit['references'])==ids


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one,two,_ = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(stage.PROJECT_ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    def oracle():
        return two.rpc(identity['port'],remote_sharp_edges_readback,identity['pid'],identity['program_args'])
    def record(name, evidence):
        report['cases'].append({'name':name,'ok':True,'evidence':evidence})
        stage._json_write(run/'intersections-report.json',report)
        print(json.dumps({'case':name,'ok':True}),flush=True)
    with (run/'intersections-backend.stderr.log').open('w',encoding='utf-8') as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                await stage._bind_for_run(h,identity,one,two,identity['port'])
                schemas = await h.list_schema()
                fixture = await h.direct_result('ansa_script_execute',{'script':FIXTURE_SCRIPT})
                await h.direct_result('ansa_model_save',{'path':str(run/'intersections-before.ansa')})
                groups = fixture['result']
                before = oracle()
                assert len(before['cards']['GRID'])==23 and len(before['cards']['SHELL'])==6
                analytic = analytic_fixture(before,groups)
                record('schemas_owned_fixture_cards_and_analytic_intersections',{
                    'schemas':schemas,'fixture':groups,'independent':before,'analytic':analytic,
                    'backup':fingerprint(run/'intersections-before.ansa')})
                fields = {'deck':before['deck'],'first_ids':groups['first_ids'],
                          'second_ids':groups['second_ids']}
                original_target,original_model = h.target.copy(),h.model.copy()
                op = 'intersections-'+uuid.uuid4().hex
                submitted,completed = await h.task('ansa_mesh_intersections',fields,operation_id=op)
                report['original_check_task']={'submitted':submitted,'completed':completed,'operation_id':op}
                stage._json_write(run/'intersections-report.json',report)
                result = completed['data']['result']
                verify_result(result,fields['first_ids'],fields['second_ids'],
                              fields['first_ids']+fields['second_ids'])
                assert groups['outside_id'] not in result['intersected']['entity_ids']
                controls = []
                scopes = [([fields['first_ids'][0]],[fields['second_ids'][0]],
                           [fields['first_ids'][0],fields['second_ids'][0]]),
                          ([fields['first_ids'][1]],[fields['second_ids'][1]],
                           [fields['first_ids'][1],fields['second_ids'][1]]),
                          (fields['first_ids'],[groups['clean_id']],[]),
                          ([fields['first_ids'][0],fields['second_ids'][0]],[groups['clean_id']],[]),
                          (fields['second_ids'],fields['first_ids'],
                           fields['first_ids']+fields['second_ids'])]
                for first,second,expected in scopes:
                    response = await h.direct_result('ansa_mesh_intersections',{
                        **fields,'first_ids':first,'second_ids':second})
                    verify_result(response,first,second,expected)
                    controls.append(response)
                assert oracle()==before
                record('two_groups_quad_triangle_clean_internal_and_outside_exclusion',{
                    'result':result,'controls':controls})
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                display = {'action':'entity_visibility','deck':before['deck'],'options':{
                    'entity_type':'SHELL','entity_ids':fields['second_ids'],'action':'hide',
                    'expected_visible':visibility['visible_ids'],'apply_on_contents':False}}
                await h.direct_result('ansa_model_inspect',display)
                hidden = oracle()
                assert hidden['cards']==before['cards']
                assert not set(hidden['visible_ids']['SHELL'])&set(fields['second_ids'])
                hidden_result = await h.direct_result('ansa_mesh_intersections',fields)
                verify_result(hidden_result,fields['first_ids'],fields['second_ids'],
                              fields['first_ids']+fields['second_ids'])
                assert oracle()==hidden
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                await h.direct_result('ansa_model_inspect',{**display,'options':{**display['options'],
                    'action':'show_also','expected_visible':visibility['visible_ids']}})
                assert oracle()==before
                record('hidden_explicit_groups_preserve_cards_and_visibility',hidden_result)
                rejected = []
                for changes in ({'first_ids':[fields['first_ids'][0],99999999]},
                                {'first_ids':[fields['first_ids'][0]]*2},
                                {'second_ids':fields['first_ids']},{'deck':before['deck']+1}):
                    rejected.append(await h.direct_result('ansa_mesh_intersections',{
                        **fields,**changes},expected='failed'))
                for changes in ({'first_ids':[]},{'second_ids':[True]},{'deck':True},
                                {'fix':True}):
                    response = await client.call_tool('ansa_mesh_intersections',{
                        'operation_id':uuid.uuid4().hex,'expected_target':h.target,
                        'expected_model':h.model,**fields,**changes})
                    assert response.isError is True
                    rejected.append({'input':changes,'is_error':True})
                assert oracle()==before
                record('invalid_missing_duplicate_overlapping_scope_and_schema_rejection',rejected)
                repeated = await h.call('ansa_mesh_intersections',{'operation_id':op,
                    'expected_target':original_target,'expected_model':original_model,**fields})
                assert repeated['task_id']==submitted['task_id'] and repeated['data']['result']==result
                assert oracle()==before
                record('original_operation_idempotency_and_independent_no_changes',{
                    'task_id':submitted['task_id'],'repeated_task_id':repeated['task_id']})
                saved_path = run/'intersections-after.ansa'
                await h.direct_result('ansa_model_save',{'path':str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result('ansa_model_open',{'path':str(saved_path),
                    'backup_path':str(run/'intersections-reopen-backup.ansa')})
                await h.snapshot_context()
                reopened = oracle()
                assert {k:v for k,v in reopened.items() if k!='model_path'}=={
                    k:v for k,v in before.items() if k!='model_path'}
                after = await h.direct_result('ansa_mesh_intersections',fields)
                verify_result(after,fields['first_ids'],fields['second_ids'],
                              fields['first_ids']+fields['second_ids'])
                assert oracle()==reopened and fingerprint(saved_path)==saved
                assert h.model['epoch']!=original_model['epoch']
                stale = await h.call('ansa_mesh_intersections',{'operation_id':'intersections-stale-'+uuid.uuid4().hex,
                    'expected_target':h.target,'expected_model':original_model,**fields},allow_error=True)
                assert stale['ok'] is False and stale['task_id'] is None
                record('save_reopen_preserves_scope_and_rejects_stale_model',{
                    'independent':reopened,'result':after,'saved':saved,'stale':stale})
                report['core_acceptance'] = True
                stage._json_write(run/'intersections-report.json',report)
                if wait_ui:
                    marker = run/'intersections-ui-confirmation.json'
                    if marker.exists():
                        raise RuntimeError('Refuse stale intersection GUI evidence')
                    stage._json_write(run/'intersections-ui-instruction.json',{'identity':identity,
                        'model_path':reopened['model_path'],'visible_ids':reopened['visible_ids'],
                        'instruction':'所属主窗口显示六个壳：两对相交Quad/Tria、额外范围外竖直Quad和右侧独立Quad；GRID 23、ELEMENT 6。'})
                    print(json.dumps({'status':'waiting_for_intersections_ui','run_dir':str(run)}),flush=True)
                    deadline = asyncio.get_running_loop().time()+600
                    while not marker.is_file():
                        if asyncio.get_running_loop().time()>=deadline:
                            raise TimeoutError('Keep original intersection task for GUI inspection')
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding='utf-8'))
                    if (visual.get('checked') is not True or visual.get('pid')!=identity['pid']
                            or visual.get('process_start')!=identity['process_start']
                            or visual.get('model_path')!=reopened['model_path']
                            or visual.get('fixture_visible') is not True):
                        raise RuntimeError('Intersection GUI evidence mismatches owned model')
                    assert oracle()==reopened
                    report['visual_acceptance'] = True
                    record('owned_main_window_and_post_ui_independent_readback',visual)
    report['success'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ansa-root','runtime-home','run-dir'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--wait-ui',action='store_true')
    parser.add_argument('--keep-ansa',action='store_true')
    args = parser.parse_args()
    run,instructions = stage._load_run(args)
    if (run/'seed-baseline.ansa').exists() or (run/'intersections-report.json').exists():
        raise ValueError('只接受新准备目录；未知结果先查询原任务，不重放夹具')
    report = {'scope':'ANSA 2026.1 Windows GUI/NASTRAN 两组显式 SHELL 相交检查',
              'started_at':datetime.now(timezone.utc).isoformat(),'success':False,'cases':[],
              'core_acceptance':False,'visual_acceptance':False}
    identity = helpers = None
    try:
        identity,_,_,helpers = stage._prepare_owned_ansa(run,instructions)
        report['identity'] = identity
        stage._json_write(run/'intersections-report.json',report)
        report['fixture'] = helpers[1].rpc(identity['port'],remote_initialize_fixture,
            identity['pid'],identity['program_args'],identity['seed_path'],identity['seed_entity_id'])
        asyncio.run(verify(run,instructions,identity,helpers,report,wait_ui=args.wait_ui))
    except BaseException as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc)}
        report['exception_tree']=stage._exception_tree(exc)
        (run/'intersections-report.error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        if identity is not None and helpers is not None and report['success'] and not args.keep_ansa:
            try:
                report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
            except Exception as exc:
                report['success']=False
                report['cleanup_error']={'type':type(exc).__name__,'message':str(exc)}
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        stage._json_write(run/'intersections-report.json',report)
    if not report['success']:
        raise RuntimeError('Intersection acceptance or owned process cleanup failed')
    print(json.dumps({'success':True,'report':str(run/'intersections-report.json')}),flush=True)


if __name__=='__main__':
    main()
