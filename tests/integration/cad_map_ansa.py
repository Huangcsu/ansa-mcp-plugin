"""Public plan/apply acceptance on an explicitly owned, backed-up Map fixture.

Usage: cad_map_ansa.py <owned-run-dir>. Requires this run's completed output-Part
probe; never seeds a model or attaches to an unrelated instance.
"""
import asyncio
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/integration')]
import stage_three_ansa as stage


def remote_refresh(pid,arguments):
    import builtins,importlib,os,threading
    from ansa import session
    if os.getpid()!=pid or list(session.ProgramArguments())!=arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError('owned main-thread identity changed')
    bridge=builtins._ansa_mcp_plugin_bridge
    if bridge.state.journal.busy():raise RuntimeError('unresolved tasks prevent module refresh')
    names=['ansa_mcp_shared.operations','ansa_mcp_bridge.operations','ansa_mcp_bridge.operations.cad_sweep_geometry',
           'ansa_mcp_bridge.operations.cad_map_mesh','ansa_mcp_bridge.operations.cad_map',
           'ansa_mcp_bridge.operations.structured_evidence','ansa_mcp_bridge.operations.structured_meshing']
    for name in names:importlib.reload(importlib.import_module(name))
    return {'pid':pid,'modules':names}


SNAPSHOT='''
from ansa import base
from ansa_mcp_bridge.operations.cad_map import _snapshot
import builtins
RESULT=_snapshot(builtins._ansa_mcp_plugin_bridge.state,base,base.CurrentDeck())
'''


def audit_persistence(before,after,output_part_id):
    """Separate one observed ANSA color-display refresh from engineering cards."""
    left,right=copy.deepcopy(before),copy.deepcopy(after);display=[]
    a=next(r for r in left['entities'] if r['entity']['type']=='ANSAPART' and r['entity']['id']==output_part_id)
    b=next(r for r in right['entities'] if r['entity']['type']=='ANSAPART' and r['entity']['id']==output_part_id)
    if a['fields'].get('Is Color Active')=='NO' and b['fields'].get('Is Color Active')=='YES':
        display.append({'type':'ANSAPART','id':output_part_id,'field':'Is Color Active','before':'NO','after':'YES'})
        a['fields']['Is Color Active']=b['fields']['Is Color Active']
    return {'full_snapshot_exact':before==after,'engineering_snapshot_exact':left==right,'display_changes':display}


async def verify(run,cfg,identity,helpers,report,output,prior=None,from_cad=False):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'cad-map-public.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,report,run)
                await h.list_schema();await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                if prior is not None:
                    previous=await h.call('ansa_task_query',{'task_id':report['resume_failed_task']},allow_error=True)
                    if previous['status']!='failed' or previous['operation_id']!=prior['pending']['operation_id']:
                        raise RuntimeError('prior plan task not a confirmed matching failed task')
                    report['previous_task']=previous
                def artifact(name):return str(run/(output.stem+'-'+name+'.ansa'))
                async def call(name,fields,expected='succeeded',operation=None):
                    op=operation or 'cad-map-public-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':op};stage._json_write(output,report)
                    submitted,completed=await h.task(name,fields,operation_id=op,expected=expected,timeout_seconds=55)
                    report['calls'].append({'tool':name,'operation_id':op,'task_id':submitted['task_id'],'result':completed})
                    report['pending']=None;stage._json_write(output,report)
                    print(json.dumps({'tool':name,'status':completed['status']}),flush=True)
                    return completed['data']['result'] if expected=='succeeded' else completed
                async def script(code):return (await call('ansa_script_execute',{'script':code,'validate':True}))['result']
                if prior is not None and await script(SNAPSHOT)!=prior['before']:
                    raise RuntimeError('model changed since failed read-only plan')
                source_path=('F:/ANSA/ansa-mcp-plugin/runtime/verification/stage-three-d6cc26be58594a4f8e1bc63edbaf7244/box-source.ansa'
                             if from_cad else str(run/'before-distinct-part-map.ansa'))
                await call('ansa_model_open',{'path':source_path,
                    'preservation':'backup','backup_path':artifact('before-public-fixture-open')})
                await h.snapshot_context()
                output_part_id=2
                if from_cad:
                    report['raw_cad']=await script('''
from ansa import base
import math
d=base.CurrentDeck()
if base.CollectEntities(d,None,'SHELL') or base.CollectEntities(d,None,'SOLID'):raise RuntimeError('unmeshed CAD required')
rows=[]
for c in base.CollectEntities(d,None,'CONS'):
    hp=base.CollectEntities(d,c,'HOT POINT')
    if len(hp)!=2:raise RuntimeError('two endpoints required')
    rows.append({'cons_id':c._id,'segments':round(math.dist(hp[0].position,hp[1].position)/5.)})
RESULT={'assignments':rows,'faces':[{'face_id':f._id,'cons_ids':sorted(c._id for c in base.CollectEntities(d,f,'CONS'))} for f in base.CollectEntities(d,None,'FACE')]}
''')
                    seed=await call('ansa_mesh_seed_read',{'deck':1,'face_ids':[1,2,3,4,5,6],
                        'cons_ids':sorted(r['cons_id'] for r in report['raw_cad']['assignments'])})
                    report['seed_apply']=await call('ansa_mesh_seed_apply',{'deck':1,'face_ids':[1,2,3,4,5,6],
                        'assignments':report['raw_cad']['assignments'],'expected_seed_snapshot_sha256':seed['seed_snapshot_sha256'],
                        'backup_path':artifact('before-seed-apply')})
                    for selected in ([1,2],[3,4,5,6]):
                        cons=sorted({i for f in report['raw_cad']['faces'] if f['face_id'] in selected for i in f['cons_ids']})
                        seed=await call('ansa_mesh_seed_read',{'deck':1,'face_ids':[1,2,3,4,5,6],'cons_ids':cons})
                        conditions=await call('ansa_mesh_faces',{'deck':1,'face_ids':selected})
                        mapped=await call('ansa_mesh_surface',{'deck':1,'method':'mapped_existing_seeds','element_type':'quad',
                            'faces':[r['condition'] for r in conditions['faces']],'seed_scope':{'face_ids':[1,2,3,4,5,6],
                            'cons_ids':cons,'expected_seed_snapshot_sha256':seed['seed_snapshot_sha256'],
                            'backup_path':artifact('before-mapped-'+str(selected[0]))}})
                        report.setdefault('mapped',[]).append(mapped)
                    new_part=await script("from ansa import base\np=base.NewPart('controlled-pipeline-output')\nRESULT={'id':p._id}")
                    output_part_id=new_part['id']
                    await call('ansa_model_save',{'path':artifact('mapped-boundary')})
                report['before']=await script(SNAPSHOT)
                fields={'deck':1,'face_ids':[1,2,3,4,5,6],'source_face_id':1,'target_face_id':2,
                        'direction':[0.,0.,1.],'distance':10.,'steps':2,'property_id':2,'output_part_id':output_part_id}
                report['plan']=await call('ansa_mesh_cad_map',{'action':'plan',**fields})
                report['geometry']=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':report['plan']['report_id'],'section':'geometry'})
                report['after_plan']=await script(SNAPSHOT)
                if report['before']!=report['after_plan']:raise AssertionError('plan changed model')
                report['wrong_steps']=await call('ansa_mesh_cad_map',{'action':'plan',**fields,'steps':3},'failed')
                rejected_backup=Path(artifact('must-not-exist-stale-map'))
                report['stale_hash']=await call('ansa_mesh_cad_map',{'action':'apply',**fields,
                    'expected_plan_sha256':'0'*64,'backup_path':str(rejected_backup)},'failed')
                if rejected_backup.exists():raise AssertionError('stale plan created a backup')
                if await script(SNAPSHOT)!=report['before']:raise AssertionError('rejected preflight changed model')
                apply_op='cad-map-apply-'+uuid.uuid4().hex
                apply_args={'action':'apply',**fields,'expected_plan_sha256':report['plan']['plan_sha256'],
                            'backup_path':artifact('before-public-map-apply')}
                report['apply']=await call('ansa_mesh_cad_map',apply_args,operation=apply_op)
                if not report['apply'].get('scope_verified') or len(report['apply']['new_solid_ids'])!=48:
                    raise AssertionError('controlled Map not verified')
                report['after_apply']=await script(SNAPSHOT)
                report['idempotent']=await call('ansa_mesh_cad_map',apply_args,operation=apply_op)
                if report['apply']!=report['idempotent']:raise AssertionError('idempotent receipt changed')
                if await script(SNAPSHOT)!=report['after_apply']:raise AssertionError('idempotent replay mutated model')
                report['stale_page']=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':report['plan']['report_id'],'section':'summary'},'failed')
                path=Path(artifact('controlled-cad-map'))
                await call('ansa_model_save',{'path':str(path)})
                await call('ansa_model_open',{'path':str(path),'preservation':'backup','backup_path':artifact('before-public-map-reopen')})
                await h.snapshot_context()
                report['reopened']=await script(SNAPSHOT)
                report['persistence']=audit_persistence(report['after_apply'],report['reopened'],output_part_id)
                if not report['persistence']['engineering_snapshot_exact']:raise AssertionError('engineering save/reopen snapshot differs')
                report['complete']=True


async def finalize_persistence(run,cfg,identity,helpers,report):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize();h=stage.McpHarness(client,report,run)
            await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
            submitted,completed=await h.task('ansa_script_execute',{'script':SNAPSHOT,'validate':True},
                operation_id='cad-map-persistence-'+uuid.uuid4().hex,timeout_seconds=55)
            current=completed['data']['result']['result']
            if current!=report['reopened']:raise RuntimeError('model changed since saved reopen readback')
            report['persistence_current_readback']={'task_id':submitted['task_id'],'snapshot_exact':True}
            report['persistence']=audit_persistence(report['after_apply'],current,2)
            if not report['persistence']['engineering_snapshot_exact']:raise RuntimeError('engineering snapshot mismatch')
            report['complete']=True


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir',type=Path);parser.add_argument('--resume-failed-task');parser.add_argument('--from-cad',action='store_true')
    parser.add_argument('--finalize-persistence',action='store_true')
    args=parser.parse_args();run=args.run_dir.resolve();prior=None
    if args.resume_failed_task:
        prior=json.loads((run/'cad-map-public-report.json').read_text('utf-8'))
        if 'apply' in prior or 'plan' in prior:raise RuntimeError('only a failed first read-only plan may resume here')
        if args.from_cad:raise ValueError('resume and new raw-CAD run are separate modes')
    output=run/('cad-map-pipeline-report.json' if args.from_cad else
                'cad-map-public-followup-report.json' if prior is not None else 'cad-map-public-report.json')
    if args.finalize_persistence:
        if args.from_cad or args.resume_failed_task:raise ValueError('finalize is a separate read-only mode')
        output=run/'cad-map-pipeline-final-report.json'
    if output.exists():raise FileExistsError('report exists; query original tasks, do not replay')
    cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'))
    probe=json.loads((run/'output-part-probe.json').read_text('utf-8'))
    if not probe['complete']:raise RuntimeError('original native probe incomplete')
    identity=probe['identity'];report={'complete':False,'identity':identity,'calls':[],'pending':None}
    if args.finalize_persistence:
        report=json.loads((run/'cad-map-pipeline-report.json').read_text('utf-8'))
        if report.get('pending') is not None or not report.get('apply',{}).get('scope_verified') or 'reopened' not in report:
            raise RuntimeError('cannot finalize an incomplete/uncertain model operation')
        report['previous_persistence_failure']=report.pop('error',None)
    if prior is not None:report['resume_failed_task']=args.resume_failed_task
    import grpc
    one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor)
    try:
        from ansa_mcp_shared.identity import same_process_alive
        if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned process start changed')
        basic=two.rpc(identity['port'],one.remote_basic)
        stage._verify_program_arguments(basic,Path(cfg['translator']),cfg['port'])
        if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned PID/args changed')
        report['refresh']=two.rpc(identity['port'],remote_refresh,identity['pid'],identity['program_args'])
        if args.finalize_persistence:
            asyncio.run(finalize_persistence(run,cfg,identity,(one,two,original),report))
        else:
            asyncio.run(verify(run,cfg,identity,(one,two,original),report,output,prior,args.from_cad))
    except Exception:report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:stage._json_write(output,report);grpc.insecure_channel=original
    print(json.dumps({'complete':report['complete'],'path':str(output)}),flush=True)
    return 0 if report['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
