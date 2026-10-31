"""One-shot owned-instance MCP MapBlock prepare and settings-restoration acceptance."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests/integration')]
import stage_three_ansa as stage

ORACLE = r'''
import builtins,json,hashlib
from pathlib import Path
from ansa import base
from ansa_mcp_bridge.operations import cad_map,part_aggregates,geometry_features as f
s=cad_map._snapshot(builtins._ansa_mcp_plugin_bridge.state,base,base.CurrentDeck())
a={}
for row in s['entities']:
    if row['entity']['type']=='ANSAPART':
        ident=row['entity']['id']
        a[ident]=part_aggregates.capture(base,base.CurrentDeck(),base.GetEntity(base.CurrentDeck(),'ANSAPART',ident),row)
raw=json.dumps({'snapshot':s,'aggregates':a},ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
path=Path(ORACLE_PATH)
with path.open('xb') as stream:stream.write(raw)
RESULT={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'scope_sha256':f._hash(s),'entities':len(s['entities'])}
'''

SETTINGS_FAILURE = r'''
import builtins
from ansa import base,mesh
from ansa_mcp_bridge.operations import meshing_context
state=builtins._ansa_mcp_plugin_bridge.state
before=meshing_context.capture(state,base,mesh);receipt={}
def fail():
    original=base.F11ShellsOptionsGet('max length')
    rc=base.F11ShellsOptionsSet('max length',not bool(original['status']),'',original['value']+1.)
    if rc!=1:raise RuntimeError('controlled F11 perturbation failed')
    raise RuntimeError('intentional operation failure for restoration acceptance')
try:meshing_context.run_with_context(state,base,mesh,4.,fail,receipt,expected_snapshot_sha256=before['snapshot_sha256'])
except Exception as exc:error=type(exc).__name__+': '+str(exc)
else:raise RuntimeError('intentional callback error lost')
after=meshing_context.capture(state,base,mesh)
if before['snapshot_sha256']!=after['snapshot_sha256']:raise RuntimeError('complete settings not restored after callback failure')
RESULT={'receipt':receipt,'error':error,'snapshot_sha256':after['snapshot_sha256'],'complete_context_equal':True}
'''


async def verify(run, cfg, identity, helpers, report, raw, output, resume=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'map-block-prepare.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema();await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                if report.get('confirmed_previous_task'):
                    original=await h.call('ansa_task_query',{'task_id':report['confirmed_previous_task']},allow_error=True)
                    if original.get('status')!='failed':raise RuntimeError('previous apply is not confirmed failed')
                    report['previous_task_readback']=original
                async def call(name,fields,expected='succeeded',operation=None):
                    op=operation or 'map-block-prepare-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':op};stage._json_write(output,report)
                    sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
                    if not sub.get('task_id'):raise RuntimeError(str(sub))
                    report['pending']['task_id']=sub['task_id'];stage._json_write(output,report)
                    done=await h.wait_task(sub['task_id'],expected,55)
                    report['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'status':done['status'],'result':done})
                    report['pending']=None;stage._json_write(output,report)
                    print(json.dumps({'tool':name,'status':done['status']}),flush=True)
                    return done['data']['result'] if expected=='succeeded' else done
                async def script(code):
                    r=await call('ansa_script_execute',{'script':code,'validate':True})
                    if r.get('result_paged'):raise RuntimeError('unexpected helper paging; query original report')
                    return r['result']
                if resume is None:
                    await call('ansa_model_open',{'path':str(raw),'preservation':'backup','backup_path':str(run/'before-source-open.ansa')});await h.snapshot_context()
                    report['settings_failure']=await script(SETTINGS_FAILURE)
                    await script("from ansa import base\nd=base.CurrentDeck()\nassert base.SetEntityCardValues(d,base.GetEntity(d,'MAT1',1),{'E':123456.,'NU':.27,'RHO':1.23e-9,'DEFINED':'YES'})==0\nassert base.SetEntityCardValues(d,base.GetEntity(d,'PSHELL',1),{'MID1':1,'MID2':1,'MID3':1})==0\nRESULT={'custom_material':True}\n")
                    await call('ansa_model_save',{'path':str(run/'custom-source.ansa')})
                else:
                    task=await h.call('ansa_task_query',{'task_id':resume['pending']['task_id']},allow_error=True)
                    if task.get('status')!='failed' or task.get('operation_id')!=resume['pending']['operation_id']:
                        raise RuntimeError('original read-only page task not confirmed failed')
                    report['original_page_failure']=task;report['settings_failure']=resume['settings_failure']
                before=await script('ORACLE_PATH='+repr(str(run/('oracle-resume-before-plan.json' if resume else 'oracle-before-plan.json')))+'\n'+ORACLE)
                p={'deck':1,'face_ids':[1,2,3,4,5,6],'source_face_id':1,'target_face_id':2,
                   'direction':[0.,0.,1.],'distance':10.,'target_length':5.,'property_id':2}
                # Obtain a real geometry-derived cap pair rather than assume fixture IDs.
                candidate=await call('ansa_geometry_sweep_candidates',{'deck':1,'face_ids':p['face_ids'],'target_length':5.})
                page=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':candidate['report_id'],'section':'/classification/components','offset':0,'limit':10})
                report['candidate_rows']=page['rows']
                choices=page['rows'][0]['alternatives']
                direction=choices[0]['map_options']
                p.update({k:direction[k] for k in ('source_face_id','target_face_id','direction','distance')})
                desc=await call('ansa_mesh_map_block_prepare',{'action':'plan',**p});report['plan']=desc
                sha=(await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':'plan_sha256','offset':0,'limit':1}))['rows'][0]
                plan={'plan_sha256':sha,'params':p}
                report['plan_body']=plan
                after_plan=await script('ORACLE_PATH='+repr(str(run/'oracle-after-plan.json'))+'\n'+ORACLE)
                if before['scope_sha256']!=after_plan['scope_sha256']:raise AssertionError('plan mutated original model')
                report['stale_plan_rejection']=await call('ansa_mesh_map_block_prepare',{'action':'apply',**p,'expected_plan_sha256':'0'*64,'backup_path':str(run/'unused-stale.ansa')},'failed')
                op='map-block-controlled-apply-'+uuid.uuid4().hex
                result=await call('ansa_mesh_map_block_prepare',{'action':'apply',**p,'expected_plan_sha256':plan['plan_sha256'],'backup_path':str(run/'before-native-map-block.ansa')},operation=op)
                report['apply']=result
                if not result.get('ok') or not result.get('scope_verified'):raise AssertionError('controlled apply did not pass: '+str(result))
                replay=await h.call('ansa_mesh_map_block_prepare',{'action':'apply',**p,'expected_plan_sha256':plan['plan_sha256'],'backup_path':str(run/'before-native-map-block.ansa'),'operation_id':op,'expected_target':h.target,'expected_model':h.model})
                if replay['task_id']!=report['calls'][-1]['task_id']:raise AssertionError('idempotent call changed task')
                report['idempotent_task_id']=replay['task_id']
                await script('ORACLE_PATH='+repr(str(run/'oracle-before-save.json'))+'\n'+ORACLE)
                saved=run/'controlled-map-block.ansa'
                await call('ansa_model_save',{'path':str(saved)})
                await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-reopen.ansa')});await h.snapshot_context()
                await script('ORACLE_PATH='+repr(str(run/'oracle-after-reopen.json'))+'\n'+ORACLE)
                from ansa_mcp_bridge.operations import part_aggregates
                a=json.loads((run/'oracle-before-save.json').read_text('utf-8'));b=json.loads((run/'oracle-after-reopen.json').read_text('utf-8'))
                report['persistence']=part_aggregates.compare_persistence(a['snapshot'],b['snapshot'],{int(k):v for k,v in a['aggregates'].items()},{int(k):v for k,v in b['aggregates'].items()})
                report['complete']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path);p.add_argument('--raw',type=Path,required=True)
    p.add_argument('--resume-before-first-task',action='store_true')
    p.add_argument('--resume-before-native')
    p.add_argument('--new-case-after-verified-followup',action='store_true')
    a=p.parse_args();run=a.run_dir.resolve();cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'))
    recovery=None
    if a.new_case_after_verified_followup:
        if a.resume_before_first_task or a.resume_before_native:raise ValueError('new case cannot resume an old operation')
        recovery=json.loads((run/'map-block-existing-followup.json').read_text('utf-8'))
        if not recovery.get('complete') or recovery.get('persistence',{}).get('status')!='pass' or recovery['original_task']['status']!='failed':
            raise RuntimeError('known terminal task, independent readback and preserved checkpoint are required')
        run=run/'corrected-case';run.mkdir(exist_ok=False)
        stage._json_write(run/'launch_instructions.json',cfg)
    output=run/('map-block-prepare-apply-followup.json' if a.resume_before_native else 'map-block-prepare-followup.json' if a.resume_before_first_task else 'map-block-prepare-report.json')
    if output.exists():raise FileExistsError('one-shot report exists; query original task before any further operation')
    r={'complete':False,'calls':[],'pending':None};helpers=None
    try:
        prior=None
        if recovery is not None or a.resume_before_first_task or a.resume_before_native:
            prior=recovery or json.loads((run/('map-block-prepare-followup.json' if a.resume_before_native else 'map-block-prepare-report.json')).read_text('utf-8'))
            if recovery is not None:
                r['confirmed_previous_task']=recovery['original_task']['task_id']
                r['new_case_from_preserved_checkpoint']=True
            elif a.resume_before_native:
                if (prior.get('apply') or prior.get('complete') or not prior.get('plan')
                        or prior['pending']['tool']!='ansa_mesh_evidence_page'
                        or prior['pending']['task_id']!=a.resume_before_native):
                    raise RuntimeError('resume is limited to the known pre-native read-only page failure')
            elif prior.get('calls') or prior.get('pending') or prior.get('complete'):
                raise RuntimeError('resume is limited to a schema failure before the first task')
            identity=prior['identity']
            import grpc
            from ansa_mcp_shared.identity import same_process_alive
            one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor);helpers=(one,two,original)
            if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned process identity changed')
            basic=two.rpc(identity['port'],one.remote_basic);stage._verify_program_arguments(basic,Path(cfg['translator']),identity['port'])
            if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned launch changed')
        else:
            identity,_,_,helpers=stage._prepare_owned_ansa(run,cfg)
        r['identity']=identity;stage._json_write(output,r)
        asyncio.run(verify(run,cfg,identity,helpers,r,a.raw.resolve(strict=True),output,prior if a.resume_before_native else None))
    except Exception:r['error']=traceback.format_exc();print(r['error'],flush=True)
    finally:
        stage._json_write(output,r)
        if helpers:
            import grpc
            grpc.insecure_channel=helpers[2]
    print(json.dumps({'complete':r['complete'],'report':str(output)}),flush=True)
    return 0 if r['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
