"""独立两组壳厚度穿透验收；先用项目 Skill 查 API，再运行所属窗口。"""

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
    raise RuntimeError('penetration fixture requires NASTRAN')
for kind in ('SHELL','SOLID','CONM2','FACE','__PROPERTIES__','__MATERIALS__'):
    if base.CollectEntities(deck,None,kind):
        raise RuntimeError('penetration fixture requires a fresh owned seed')
if len(base.CollectEntities(deck,None,'GRID')) != 1:
    raise RuntimeError('penetration fixture requires one owned seed GRID')
def create(kind, fields):
    entity = base.CreateEntity(deck,kind,fields)
    if entity is None or any(k not in entity.card_fields(deck) for k in fields):
        raise RuntimeError('penetration fixture fields unavailable')
    values = base.GetEntityCardValues(deck,entity,tuple(fields))
    if any(values.get(k) != v for k,v in fields.items()):
        raise RuntimeError('penetration fixture card mismatch')
    return entity
mat = create('MAT1',{'DEFINED':'YES','E':200000.,'NU':.3,'RHO':.01})
props = [create('PSHELL',{'MID1':mat._id,'MID2':mat._id,'MID3':mat._id,'T':t,'NSM':0.}) for t in (1.,2.,1.)]
def shell(points, prop):
    nodes = [create('GRID',dict(zip(('X1','X2','X3'),p))) for p in points]
    fields = {'type':'CQUAD4' if len(nodes)==4 else 'CTRIA3','PID':prop._id}
    fields.update({'G'+str(i+1):n._id for i,n in enumerate(nodes)})
    return create('SHELL',fields)._id
def quad(z,prop,x=0.):
    return shell([(x,0.,z),(x+10.,0.,z),(x+10.,10.,z),(x,10.,z)],prop)
quad_a = quad(0.,props[0])
quad_b = quad(.5,props[1])
tria_a = shell([(30.,0.,0.),(40.,0.,0.),(35.,10.,0.)],props[0])
tria_b = shell([(30.,0.,.5),(40.,0.,.5),(35.,10.,.5)],props[1])
outside = quad(.4,props[1])
clean = quad(0.,props[2],60.)
same = quad(.5,props[0])
thicker = quad(1.2,props[1])
cross = shell([(5.,0.,-5.),(5.,10.,-5.),(5.,10.,5.),(5.,0.,5.)],props[1])
if base.Or(base.CollectEntities(deck,None,'SHELL')) != 1:
    raise RuntimeError('penetration fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'first_ids':[quad_a,tria_a],'second_ids':[quad_b,tria_b,cross],
          'outside_id':outside,'clean_id':clean,'same_property_id':same,'thicker_id':thicker}
"""


def analytic_fixture(snapshot, groups):
    """由独立卡片核算平行壳间距与标准属性厚度，不调用原生检查。"""
    grids = {r['id']:tuple(r['fields'][k] for k in ('X1','X2','X3')) for r in snapshot['cards']['GRID']}
    shells = {r['id']:r['fields'] for r in snapshot['cards']['SHELL']}
    props = {r['id']:r['fields']['T'] for r in snapshot['cards']['PSHELL']}
    def points(entity_id):
        card = shells[entity_id]
        return [grids[card['G'+str(i+1)]] for i in range(4 if card['type']=='CQUAD4' else 3)]
    qa,ta = groups['first_ids']
    qb,tb,cross = groups['second_ids']
    gaps = []
    for a,b in ((qa,qb),(ta,tb),(qa,groups['thicker_id'])):
        pa,pb = points(a),points(b)
        assert [p[:2] for p in pa]==[p[:2] for p in pb]
        assert len({p[2] for p in pa})==len({p[2] for p in pb})==1
        gap = abs(pa[0][2]-pb[0][2])
        property_limit = (props[shells[a]['PID']]+props[shells[b]['PID']])/2
        assert property_limit==1.5 and gap in (.5,1.2)
        gaps.append({'ids':[a,b],'gap':gap,'property_factor_one_limit':property_limit,
                     'property_overlap':gap<property_limit,'user_one_overlap':gap<1.})
    assert all(p[0]==5 for p in points(cross)) and {p[2] for p in points(cross)}=={-5.,5.}
    assert all(p[0]>=60 for p in points(groups['clean_id']))
    assert shells[groups['same_property_id']]['PID']==shells[qa]['PID']
    return {'parallel_pairs':gaps,'cross_intersection':[(5.,0.,0.),(5.,10.,0.)],
            'same_property_rejection_ids':[qa,groups['same_property_id']]}


def verify_result(result, first, second, mode, factor, intersected, penetrated):
    assert result['check']=='penetrations' and result['read_only'] is True and result['auto_fix'] is False
    assert result['scope']=={'first_ids':first,'second_ids':second}
    assert result['thickness_mode']==mode
    assert result['native_options']=={'mode':2 if mode=='property' else 3,'factor':factor,'fast_run':0}
    expected = {'intersected':set(intersected),'penetrated':set(penetrated),
                'affected':set(intersected)|set(penetrated)}
    for name,ids in expected.items():
        hit = result[name]
        assert hit['entity_ids']==sorted(ids) and hit['count']==len(ids)
        assert hit['first_ids']==sorted(ids&set(first)) and hit['second_ids']==sorted(ids&set(second))
        assert sorted(r['id'] for r in hit['references'])==sorted(ids)
        assert all(r['type']=='SHELL' for r in hit['references'])
    assert type(result['native_count']) is int and (result['native_count']>0)==bool(expected['affected'])
    context = result['property_context']
    assert [r['shell_id'] for r in context['first']]==first
    assert [r['shell_id'] for r in context['second']]==second
    assert not {r['property_id'] for r in context['first']}&{r['property_id'] for r in context['second']}
    assert all(r['thickness']>0 for rows in context.values() for r in rows)


async def wait_for_gui(run, identity, expected, oracle, *, followup=False):
    prefix = 'penetrations-followup' if followup else 'penetrations'
    marker = run/(prefix+'-ui-confirmation.json')
    if marker.exists(): raise RuntimeError('Refuse stale penetration GUI evidence')
    stage._json_write(run/(prefix+'-ui-instruction.json'),{'identity':identity,
        'model_path':expected['model_path'],'visible_ids':expected['visible_ids'],
        'instruction':'所属主窗口九个壳：平行Quad/Tria、竖直交叉壳、其他厚度/范围对照和右侧独立Quad；GRID 35、ELEMENT 9、Quad 7、Tria 2。'})
    print(json.dumps({'status':'waiting_for_penetrations_ui','run_dir':str(run),'marker':str(marker)}),flush=True)
    deadline = asyncio.get_running_loop().time()+600
    while not marker.is_file():
        if asyncio.get_running_loop().time()>=deadline:
            raise TimeoutError('Keep original penetration task for GUI inspection')
        await asyncio.sleep(.5)
    visual = json.loads(marker.read_text(encoding='utf-8'))
    if (visual.get('checked') is not True or visual.get('pid')!=identity['pid']
            or visual.get('process_start')!=identity['process_start']
            or visual.get('model_path')!=expected['model_path'] or visual.get('fixture_visible') is not True):
        raise RuntimeError('Penetration GUI evidence mismatches owned model')
    assert oracle()==expected
    return visual


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
        stage._json_write(run/'penetrations-report.json',report)
        print(json.dumps({'case':name,'ok':True}),flush=True)
    with (run/'penetrations-backend.stderr.log').open('w',encoding='utf-8') as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                await stage._bind_for_run(h,identity,one,two,identity['port'])
                schemas = await h.list_schema()
                fixture = await h.direct_result('ansa_script_execute',{'script':FIXTURE_SCRIPT})
                await h.direct_result('ansa_model_save',{'path':str(run/'penetrations-before.ansa')})
                groups = fixture['result']
                before = oracle()
                assert len(before['cards']['GRID'])==35 and len(before['cards']['SHELL'])==9
                analytic = analytic_fixture(before,groups)
                record('schemas_owned_fixture_and_independent_thickness_geometry',{
                    'schemas':schemas,'fixture':groups,'independent':before,'analytic':analytic,
                    'backup':fingerprint(run/'penetrations-before.ansa')})
                fields = {'deck':before['deck'],'first_ids':groups['first_ids'],'second_ids':groups['second_ids'],
                          'thickness_mode':'property','factor':1.}
                qa,ta = fields['first_ids']
                qb,tb,cross = fields['second_ids']
                original_target,original_model = h.target.copy(),h.model.copy()
                op = 'penetrations-'+uuid.uuid4().hex
                submitted,completed = await h.task('ansa_mesh_penetrations',fields,operation_id=op)
                report['original_check_task']={'submitted':submitted,'completed':completed,'operation_id':op}
                stage._json_write(run/'penetrations-report.json',report)
                result = completed['data']['result']
                verify_result(result,[qa,ta],[qb,tb,cross],'property',1.,[qa,cross],[qa,qb,ta,tb])
                assert groups['outside_id'] not in result['affected']['entity_ids']
                controls = []
                cases = [([qa],[qb],'property',.25,[],[]),([qa],[qb],'property',1.,[],[qa,qb]),
                         ([ta],[tb],'user',1.,[],[ta,tb]),([qa],[qb],'user',.25,[],[]),
                         ([qa],[groups['thicker_id']],'property',1.,[],[qa,groups['thicker_id']]),
                         ([qa],[groups['thicker_id']],'user',1.,[],[]),
                         ([qa],[groups['thicker_id']],'user',2.,[],[qa,groups['thicker_id']]),
                         ([qa],[cross],'user',1.,[qa,cross],[]),
                         ([qa,ta],[groups['clean_id']],'property',1.,[],[]),
                         ([qa,qb],[groups['clean_id']],'property',1.,[],[]),
                         ([qb,tb,cross],[qa,ta],'user',1.,[qa,cross],[qa,qb,ta,tb])]
                for first,second,mode,factor,intersected,penetrated in cases:
                    response = await h.direct_result('ansa_mesh_penetrations',{
                        **fields,'first_ids':first,'second_ids':second,'thickness_mode':mode,'factor':factor})
                    verify_result(response,first,second,mode,factor,intersected,penetrated)
                    controls.append(response)
                assert oracle()==before
                record('modes_thresholds_quad_triangle_crossing_and_scope_exclusion',{'result':result,'controls':controls})
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                display = {'action':'entity_visibility','deck':before['deck'],'options':{'entity_type':'SHELL',
                    'entity_ids':fields['second_ids'],'action':'hide','expected_visible':visibility['visible_ids'],
                    'apply_on_contents':False}}
                await h.direct_result('ansa_model_inspect',display)
                hidden = oracle()
                assert hidden['cards']==before['cards']
                assert not set(hidden['visible_ids']['SHELL'])&set(fields['second_ids'])
                hidden_result = await h.direct_result('ansa_mesh_penetrations',fields)
                verify_result(hidden_result,[qa,ta],[qb,tb,cross],'property',1.,[qa,cross],[qa,qb,ta,tb])
                assert oracle()==hidden
                visibility = await h.direct_result('ansa_model_inspect',{'action':'visibility_snapshot','deck':before['deck']})
                await h.direct_result('ansa_model_inspect',{**display,'options':{**display['options'],
                    'action':'show_also','expected_visible':visibility['visible_ids']}})
                assert oracle()==before
                record('explicit_hidden_shells_and_no_card_or_visibility_changes',hidden_result)
                rejected = []
                for changes in ({'first_ids':[qa,99999999]},{'first_ids':[qa,qa]},
                                {'second_ids':[qa]},{'deck':before['deck']+1},
                                {'first_ids':[qa],'second_ids':[groups['same_property_id']]}):
                    rejected.append(await h.direct_result('ansa_mesh_penetrations',{**fields,**changes},expected='failed'))
                for changes in ({'first_ids':[]},{'second_ids':[True]},{'deck':True},{'factor':True},
                                {'factor':0.},{'factor':'1'},{'thickness_mode':'intersections'},{'fix':True}):
                    response = await client.call_tool('ansa_mesh_penetrations',{'operation_id':uuid.uuid4().hex,
                        'expected_target':h.target,'expected_model':h.model,**fields,**changes})
                    assert response.isError is True
                    rejected.append({'input':changes,'is_error':True})
                assert oracle()==before
                record('invalid_scope_schema_and_shared_property_rejection',rejected)
                repeated = await h.call('ansa_mesh_penetrations',{'operation_id':op,
                    'expected_target':original_target,'expected_model':original_model,**fields})
                assert repeated['task_id']==submitted['task_id'] and repeated['data']['result']==result
                assert oracle()==before
                record('original_operation_idempotency_and_no_model_changes',{'task_id':submitted['task_id']})
                saved_path = run/'penetrations-after.ansa'
                await h.direct_result('ansa_model_save',{'path':str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result('ansa_model_open',{'path':str(saved_path),
                    'backup_path':str(run/'penetrations-reopen-backup.ansa')})
                await h.snapshot_context()
                reopened = oracle()
                assert {k:v for k,v in reopened.items() if k!='model_path'}=={k:v for k,v in before.items() if k!='model_path'}
                after = await h.direct_result('ansa_mesh_penetrations',fields)
                verify_result(after,[qa,ta],[qb,tb,cross],'property',1.,[qa,cross],[qa,qb,ta,tb])
                assert oracle()==reopened and fingerprint(saved_path)==saved
                assert h.model['epoch']!=original_model['epoch']
                stale = await h.call('ansa_mesh_penetrations',{'operation_id':'penetrations-stale-'+uuid.uuid4().hex,
                    'expected_target':h.target,'expected_model':original_model,**fields},allow_error=True)
                assert stale['ok'] is False and stale['task_id'] is None
                record('save_reopen_preserves_thickness_scope_and_rejects_stale_model',{
                    'independent':reopened,'saved':saved,'stale':stale})
                report['core_acceptance'] = True
                stage._json_write(run/'penetrations-report.json',report)
                if wait_ui:
                    visual = await wait_for_gui(run,identity,reopened,oracle)
                    report['visual_acceptance'] = True
                    record('owned_main_window_and_post_ui_independent_readback',visual)
    report['success'] = True


def gui_followup_source(run):
    source_path = run/'penetrations-report.json'
    source = json.loads(source_path.read_text(encoding='utf-8'))
    names = ['schemas_owned_fixture_and_independent_thickness_geometry',
             'modes_thresholds_quad_triangle_crossing_and_scope_exclusion',
             'explicit_hidden_shells_and_no_card_or_visibility_changes',
             'invalid_scope_schema_and_shared_property_rejection',
             'original_operation_idempotency_and_no_model_changes',
             'save_reopen_preserves_thickness_scope_and_rejects_stale_model']
    def leaves(error):
        return sum((leaves(child) for child in error['exceptions']),[]) if 'exceptions' in error else [error]
    errors = leaves(source.get('exception_tree',{}))
    if (source.get('core_acceptance') is not True or source.get('visual_acceptance') is not False
            or source.get('success') is not False or [c['name'] for c in source['cases']]!=names
            or any(c['ok'] is not True for c in source['cases'])
            or len(errors)!=1 or errors[0].get('type')!='TimeoutError'
            or errors[0].get('message')!='Keep original penetration task for GUI inspection'):
        raise RuntimeError('GUI-only followup requires exactly six passed core cases and the original GUI timeout')
    if (run/'penetrations-followup-report.json').exists():
        raise RuntimeError('Refuse replay of prior GUI followup; inspect its existing report')
    return source,fingerprint(source_path)


async def verify_gui_only(run, instructions, identity, helpers, report, original):
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
        stage._json_write(run/'penetrations-followup-report.json',report)
        print(json.dumps({'case':name,'ok':True}),flush=True)
    expected = original['cases'][-1]['evidence']['independent']
    saved = original['cases'][-1]['evidence']['saved']
    with (run/'penetrations-followup-backend.stderr.log').open('w',encoding='utf-8') as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client,report,run)
                await stage._bind_for_run(h,identity,one,two,identity['port'])
                prior = original['original_check_task']
                task_id = prior['submitted']['task_id']
                queried = await h.call('ansa_task_query',{'task_id':task_id})
                assert queried['task_id']==task_id and queried['status']=='succeeded'
                assert queried['data']['result']==prior['completed']['data']['result']
                record('original_task_query_without_replaying_fixture_or_check',{'task_id':task_id,'queried':queried})
                assert oracle()==expected and fingerprint(Path(saved['path']))==saved
                record('same_owned_saved_model_cards_thickness_and_visibility',{'independent':expected,'saved':saved})
                visual = await wait_for_gui(run,identity,expected,oracle,followup=True)
                assert fingerprint(run/'penetrations-report.json')==report['original_report']
                assert fingerprint(Path(saved['path']))==saved
                report['visual_acceptance'] = True
                record('owned_main_window_post_ui_readback_and_original_evidence_preserved',visual)
    report['success'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ansa-root','runtime-home','run-dir'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--wait-ui',action='store_true')
    parser.add_argument('--keep-ansa',action='store_true')
    parser.add_argument('--gui-only',action='store_true',help='只查询原任务并补验仍在运行的所属主窗口；不重放夹具和检查')
    args = parser.parse_args()
    run,instructions = stage._load_run(args)
    if not args.gui_only and ((run/'seed-baseline.ansa').exists() or (run/'penetrations-report.json').exists()):
        raise ValueError('只接受新准备目录；未知结果先查询原任务，不重放夹具')
    original,original_fingerprint = gui_followup_source(run) if args.gui_only else (None,None)
    report_name = 'penetrations-followup-report' if args.gui_only else 'penetrations-report'
    report = {'scope':'ANSA 2026.1 Windows GUI/NASTRAN 两组显式 SHELL 厚度穿透检查',
              'started_at':datetime.now(timezone.utc).isoformat(),'success':False,'cases':[],
              'core_acceptance':False,'visual_acceptance':False}
    identity = helpers = None
    try:
        if args.gui_only:
            import grpc
            from ansa_mcp_shared.identity import same_process_alive
            one,two = stage._import_runtime_helpers()
            channel_factory = stage._bounded_grpc(grpc,one.DeadlineInterceptor)
            helpers = one,two,channel_factory
            identity = original['identity']
            report['original_report'] = original_fingerprint
            checked = two.rpc(identity['port'],one.remote_basic)
            stage._verify_program_arguments(checked,Path(instructions['translator']),identity['port'])
            if (checked['pid']!=identity['pid'] or checked['program_args']!=identity['program_args']
                    or not same_process_alive(identity['pid'],identity['process_start'])):
                raise RuntimeError('GUI followup refuses changed owned process identity')
            report['core_acceptance'] = True
        else:
            identity,_,_,helpers = stage._prepare_owned_ansa(run,instructions)
        report['identity'] = identity
        stage._json_write(run/(report_name+'.json'),report)
        if args.gui_only:
            asyncio.run(verify_gui_only(run,instructions,identity,helpers,report,original))
        else:
            report['fixture'] = helpers[1].rpc(identity['port'],remote_initialize_fixture,
                identity['pid'],identity['program_args'],identity['seed_path'],identity['seed_entity_id'])
            asyncio.run(verify(run,instructions,identity,helpers,report,wait_ui=args.wait_ui))
    except BaseException as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc)}
        report['exception_tree']=stage._exception_tree(exc)
        (run/(report_name+'.error.log')).write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        if identity is not None and helpers is not None and report['success'] and not args.keep_ansa:
            try:
                report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
            except Exception as exc:
                report['success']=False
                report['cleanup_error']={'type':type(exc).__name__,'message':str(exc)}
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        stage._json_write(run/(report_name+'.json'),report)
    if not report['success']: raise RuntimeError('Penetration acceptance or owned process cleanup failed')
    print(json.dumps({'success':True,'report':str(run/(report_name+'.json'))}),flush=True)


if __name__=='__main__':
    main()
