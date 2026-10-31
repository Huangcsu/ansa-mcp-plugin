"""One-shot public partition/MapBlock batch/incremental/recovery acceptance."""
import argparse
import asyncio
from contextlib import asynccontextmanager
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
from map_block_prepare_ansa import ORACLE


@asynccontextmanager
async def harness(run,cfg,identity,helpers,r):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    args=StdioServerParameters(command=sys.executable,args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'map-block-batch.stderr.log').open('a',encoding='utf-8') as err:
        async with stdio_client(args,errlog=err) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,r,run)
                r['schema']=await h.list_schema();await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                yield h


async def verify(run,cfg,identity,helpers,r,cad,resume=False):
    out=run/r.get('receipt_name','map-block-batch-report.json')
    async def call(h,name,fields,expected='succeeded',operation=None):
        op=operation or 'map-block-batch-'+uuid.uuid4().hex
        r['pending']={'tool':name,'operation_id':op};stage._json_write(out,r)
        sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
        if not sub.get('task_id'):raise RuntimeError(str(sub))
        r['pending']['task_id']=sub['task_id'];stage._json_write(out,r)
        done=await h.wait_task(sub['task_id'],None,55)
        r['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'status':done['status'],'result':done})
        r['pending']=None;stage._json_write(out,r)
        print(json.dumps({'tool':name,'expected':expected,'status':done['status']}),flush=True)
        if done['status']!=expected:raise RuntimeError(str(done.get('error')))
        return done['data']['result'] if expected=='succeeded' else done
    async def script(h,code):
        value=await call(h,'ansa_script_execute',{'script':code,'validate':True})
        if value.get('result_paged'):raise RuntimeError('query original paged result; do not replay')
        return value['result']
    async def oracle(h,name):return await script(h,'ORACLE_PATH='+repr(str(run/name))+'\n'+ORACLE)
    async def plan_hash(h,desc):
        return (await call(h,'ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':'plan_sha256','offset':0,'limit':1}))['rows'][0]
    async def fill(h,ids,sha,label):
        fields={'action':'apply','deck':1,'volume_ids':ids,'expected_plan_sha256':sha,'backup_path':str(run/('before-'+label+'.ansa'))}
        op='public-batch-fill-'+uuid.uuid4().hex
        value=await call(h,'ansa_mesh_map_block_fill',fields,operation=op)
        if not value.get('ok') or not value.get('scope_verified'):raise RuntimeError(str(value.get('error')))
        task=r['calls'][-1]['task_id']
        repeated=await h.call('ansa_mesh_map_block_fill',{**fields,'operation_id':op,'expected_target':h.target,'expected_model':h.model})
        if repeated.get('task_id')!=task:raise RuntimeError('idempotent key did not retain original task')
        r[label]={'result':value,'task_id':task,'operation_id':op};stage._json_write(out,r)
        return value
    async with harness(run,cfg,identity,helpers,r) as h:
        if not resume:
            await call(h,'ansa_model_open',{'path':str(cad),'preservation':'backup','backup_path':str(run/'before-cad-open.ansa')});await h.snapshot_context()
            await call(h,'ansa_model_save',{'path':str(run/'original-cad.ansa')})
            r['original']=await oracle(h,'oracle-original.json')
            r['original_domain']=await script(h,"import builtins,json,hashlib\nfrom pathlib import Path\nfrom ansa import base\nfrom ansa_mcp_bridge.operations import geometry_features as f,cad_sweep_geometry as g,cad_sweep_topology as t\ns=builtins._ansa_mcp_plugin_bridge.state;d=base.CurrentDeck()\nw,_=f._witness(s,base,d,{'face_ids':sorted(e._id for e in base.CollectEntities(d,None,'FACE')),'curve_ids':[]},{'scoped_export_calls':0})\np=t.plan_translation(w,1,2,[0.,0.,1.],10.,1e-7);proof=g.capture(s,base,d,w,p)\nraw=json.dumps({'witness':w,'geometry':proof},ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')\npath=Path("+repr(str(run/'original-domain.json'))+")\nwith path.open('xb') as stream:stream.write(raw)\nRESULT={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'complete_face_ids':w['source_scope']['face_ids'],'geometry_status':proof['status']}\n")
            partition={'deck':1,'face_ids':[3,4,5,6],'protected_face_ids':[1,2],
                       'plane':{'origin':[0.,0.,0.],'normal':[0.,0.,1.]},'produce_plane_faces':True}
            preview=await call(h,'ansa_geometry_partition_plan',partition)
            r['partition']=await call(h,'ansa_geometry_partition_apply',{**partition,'expected_plan_sha256':preview['plan_sha256'],'backup_path':str(run/'before-partition.ansa')})
            if not r['partition'].get('ok'):raise RuntimeError('public partition was not verified')
            r['groups']=await script(h,"import builtins\nfrom ansa import base\nfrom ansa_mcp_bridge.operations import geometry_features as f\ns=builtins._ansa_mcp_plugin_bridge.state;d=base.CurrentDeck()\nw,_=f._witness(s,base,d,{'face_ids':sorted(e._id for e in base.CollectEntities(d,None,'FACE')),'curve_ids':[]},{'scoped_export_calls':0},face_limit=100)\npts={r['id']:r['position'] for r in w['vertices']};cons={r['id']:r['endpoint_topology']['hot_point_ids'] for r in w['boundaries']}\ngroups=[[],[]]\nfor face in w['face_boundaries']:\n z=[pts[p][2] for c in face['cons_ids'] for p in cons[c]]\n if max(z)<=1e-7:groups[0].append(face['face_id'])\n if min(z)>=-1e-7:groups[1].append(face['face_id'])\nRESULT={'face_groups':[sorted(g) for g in groups],'face_ids':w['source_scope']['face_ids']}\n")
        elif r.get('prepare_serialization_recovery'):
            recovered=r['prepare_serialization_recovery']
            original=await h.call('ansa_task_query',{'task_id':recovered['original_task_id']},allow_error=True)
            if original['status']!='failed' or original['operation_id']!=recovered['original_operation_id']:
                raise RuntimeError('original serialization task not confirmed failed')
            if original['data']['result'].get('error_phase')!='result_serialization':raise RuntimeError('wrong original failure phase')
            readback=Path(recovered['readback']['path']);raw=readback.read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest()!=recovered['readback']['sha256']:raise RuntimeError('recovery readback changed')
            r['recovered_original_task']=original
            r['fresh_recovery_preflight']=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':recovered['new_volume_ids']})
            observed=await oracle(h,'oracle-recovery-current.json')
            from ansa_mcp_bridge.operations.geometry_features import _hash
            if observed['scope_sha256']!=_hash(json.loads(raw)['snapshot']):raise RuntimeError('model changed since independent recovery readback')
        else:
            original=await h.call('ansa_task_query',{'task_id':r['resume_task']['task_id']},allow_error=True)
            partition_task=await h.call('ansa_task_query',{'task_id':r['resume_partition']['task_id']},allow_error=True)
            if (original['status']!='failed' or original['operation_id']!=r['resume_task']['operation_id']
                    or partition_task['status']!='succeeded' or partition_task['operation_id']!=r['resume_partition']['operation_id']):
                raise RuntimeError('original prepare-plan rejection or partition task not confirmed')
            r['original_terminal_tasks']={'plan':original,'partition':partition_task}
            expected_scope=r['partition']['after']['sha256']
            checked=await script(h,"import builtins,importlib\nfrom ansa import base\nfrom ansa_mcp_bridge.operations import structured_native as n,map_block_assembly_prepare as p\ns=builtins._ansa_mcp_plugin_bridge.state\ncurrent=n._scope_summary(n._capture_scope(base,base.CurrentDeck()))['sha256']\nassert current=="+repr(expected_scope)+"\nimport ansa_mcp_bridge.operations.structured_evidence as ev\nimport ansa_mcp_bridge.operations.structured_meshing as sm\nfrom ansa_mcp_bridge.operations import map_block_batch as mb,map_block_fill as mf,partition_aggregates as pa\nfor module in (ev,n,pa,mf,mb,p,sm):importlib.reload(module)\nRESULT={'current_scope_sha256':current,'reloaded':'map_block_assembly_prepare','native_write_replayed':False}\n")
            r['post_partition_resume_gate']=checked
        if not r.get('prepare_serialization_recovery'):
            prepare={'deck':1,'face_groups':r['groups']['face_groups'],'target_length':5.,'property_id':2}
            desc=await call(h,'ansa_mesh_map_blocks_prepare',{'action':'plan',**prepare});sha=await plan_hash(h,desc)
            r['prepare_plan']=desc
            r['prepare']=await call(h,'ansa_mesh_map_blocks_prepare',{'action':'apply',**prepare,'expected_plan_sha256':sha,'backup_path':str(run/'before-map-blocks.ansa')})
            if not r['prepare'].get('ok'):raise RuntimeError('public multi-block prepare not verified')
        await call(h,'ansa_model_save',{'path':str(run/'prepared-blocks.ansa')})
        r['prepared']=await oracle(h,'oracle-prepared.json')
        r['original_to_partition_coverage']=await script(h,"import builtins,json,hashlib\nfrom pathlib import Path\nfrom ansa import base,mesh,constants\nfrom ansa_mcp_bridge.operations import map_block_batch as b,rectangular_coverage as c\nraw=Path("+repr(str(run/'original-domain.json'))+").read_bytes()\nassert hashlib.sha256(raw).hexdigest()=="+repr(r['original_domain']['sha256'])+"\noriginal=json.loads(raw);s=builtins._ansa_mcp_plugin_bridge.state\np=b.preflight(s,base,mesh,constants,{'deck':1,'volume_ids':[1,2]})\nRESULT=c.audit_partition(original['witness'],original['geometry'],p['block']['cad_witness'],[row['geometry'] for row in p['proofs']],[v['face_ids'] for v in p['block']['volumes']])\n")
        if r['original_to_partition_coverage']['status']!='pass':raise RuntimeError('original CAD partition coverage incomplete')
        desc=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':[1,2]});r['initial_fill_plan']=desc
        r['initial_fill_sha']=await plan_hash(h,desc)
        second_desc=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':[2]})
        r['pre_incremental_second_sha']=await plan_hash(h,second_desc)
        before=await oracle(h,'oracle-after-plan.json')
        if r['prepared']['scope_sha256']!=before['scope_sha256']:raise RuntimeError('read-only plan changed model')
        one=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':[1]})
        await fill(h,[1],await plan_hash(h,one),'incremental_first')
        await call(h,'ansa_model_save',{'path':str(run/'first-block-checkpoint.ansa')})
        r['first_checkpoint']=await oracle(h,'oracle-first-checkpoint.json')
    # Restart only the MCP backend. Never replay either native operation.
    async with harness(run,cfg,identity,helpers,r) as h:
        original=await h.call('ansa_task_query',{'task_id':r['incremental_first']['task_id']})
        if original['status']!='succeeded' or original['operation_id']!=r['incremental_first']['operation_id']:raise RuntimeError('original first-fill task unavailable after backend restart')
        r['recovered_original_task']=original
        current=await oracle(h,'oracle-after-backend-restart.json')
        if current['scope_sha256']!=r['first_checkpoint']['scope_sha256']:raise RuntimeError('model changed across backend restart')
        r['stale_rejection']=await call(h,'ansa_mesh_map_block_fill',{'action':'apply','deck':1,'volume_ids':[2],
            'expected_plan_sha256':r['pre_incremental_second_sha'],'backup_path':str(run/'unused-stale-second.ansa')},'failed')
        if (run/'unused-stale-second.ansa').exists():raise RuntimeError('stale operation created backup')
        desc=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':[2]})
        final=await fill(h,[2],await plan_hash(h,desc),'incremental_second')
        if not final['all_blocks_filled'] or len(final['interfaces'])!=1:raise RuntimeError('final assembly interface incomplete')
        r['incremental_final']=await oracle(h,'oracle-incremental-final.json')
        await call(h,'ansa_model_save',{'path':str(run/'incremental-solid.ansa')})
        await call(h,'ansa_model_open',{'path':str(run/'prepared-blocks.ansa'),'preservation':'backup','backup_path':str(run/'before-batch-branch.ansa')});await h.snapshot_context()
        desc=await call(h,'ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_ids':[1,2]})
        result=await fill(h,[1,2],await plan_hash(h,desc),'combined')
        if len(result['new_solid_ids'])!=48 or not result['all_blocks_filled']:raise RuntimeError('combined result incomplete')
        r['combined_final']=await oracle(h,'oracle-combined-final.json')
        profile={'analysis_type':'linear','element_family':'solid','criteria':[{'name':'ASPECT','max':5.},{'name':'JACOB','min':.2},{'name':'MINL','min':4.5},{'name':'MAXL','max':5.5}]}
        r['quality']=await call(h,'ansa_mesh_quality_report',{'deck':1,'profile':profile,'entity_ids':result['new_solid_ids']})
        if r['quality'].get('report',r['quality'])['summary']['status']!='passed':raise RuntimeError('available fixture quality profile failed')
        saved=run/'combined-solid.ansa';await call(h,'ansa_model_save',{'path':str(saved)})
        await call(h,'ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-final-reopen.ansa')});await h.snapshot_context()
        await oracle(h,'oracle-reopened.json')
        from ansa_mcp_bridge.operations import part_aggregates
        a=json.loads((run/'oracle-combined-final.json').read_text('utf-8'));b=json.loads((run/'oracle-reopened.json').read_text('utf-8'))
        r['persistence']=part_aggregates.compare_persistence(a['snapshot'],b['snapshot'],{int(k):v for k,v in a['aggregates'].items()},{int(k):v for k,v in b['aggregates'].items()})
        r['complete']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path);p.add_argument('--cad',type=Path,required=True)
    p.add_argument('--resume-after-prepare-plan-rejection',action='store_true');p.add_argument('--resume-readonly-from');p.add_argument('--resume-after-serialization-recovery',action='store_true');a=p.parse_args()
    run=a.run_dir.resolve();resume=bool(a.resume_after_prepare_plan_rejection or a.resume_readonly_from or a.resume_after_serialization_recovery)
    source=run/(a.resume_readonly_from or 'map-block-batch-report.json')
    if source.parent!=run:raise ValueError('resume source must be run-local')
    name='map-block-batch-after-serialization-recovery.json' if a.resume_after_serialization_recovery else source.stem+'-continued.json' if a.resume_readonly_from else 'map-block-batch-resumed-report.json' if resume else 'map-block-batch-report.json';out=run/name
    if out.exists():raise FileExistsError('query original tasks; never replay one-shot acceptance')
    cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'));r={'complete':False,'calls':[],'pending':None,'receipt_name':name};helpers=None
    try:
        if a.resume_after_serialization_recovery:
            prior=json.loads((run/'map-block-batch-report.json').read_text('utf-8'));recovery=json.loads((run/'serialization-orphan-recovery.json').read_text('utf-8'))
            if (not recovery['complete'] or recovery['identity']!=prior['identity'] or recovery['result']['original_status']!='failed'
                    or recovery['result']['original_task_id']!=prior['pending']['task_id'] or recovery['result']['native_write_replayed']):raise RuntimeError('invalid original serialization recovery')
            import grpc
            from ansa_mcp_shared.identity import same_process_alive
            identity=prior['identity'];one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor);helpers=(one,two,original)
            if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned process changed')
            basic=two.rpc(identity['port'],one.remote_basic);stage._verify_program_arguments(basic,Path(cfg['translator']),identity['port'])
            if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned launch changed')
            r.update({k:prior[k] for k in ('partition','groups','original','original_domain')})
            r.update(identity=identity,prepare_serialization_recovery=recovery['result'])
        elif resume:
            prior=json.loads(source.read_text('utf-8'));last=prior['calls'][-1]
            if (prior.get('complete') or prior.get('pending') or last['tool'] not in ('ansa_mesh_map_blocks_prepare','ansa_mesh_evidence_page')
                    or last['status']!='failed' or not prior.get('partition',{}).get('ok') or prior.get('prepare')):
                raise RuntimeError('resume only accepts confirmed post-partition, read-only prepare-plan rejection')
            import grpc
            from ansa_mcp_shared.identity import same_process_alive
            identity=prior['identity'];one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor);helpers=(one,two,original)
            if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned process changed')
            basic=two.rpc(identity['port'],one.remote_basic);stage._verify_program_arguments(basic,Path(cfg['translator']),identity['port'])
            if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned process launch changed')
            r.update({k:prior[k] for k in ('partition','groups','original','original_domain')})
            r.update(identity=identity,resume_task=last,resume_partition=prior.get('resume_partition') or next(row for row in prior['calls'] if row['tool']=='ansa_geometry_partition_apply'))
        else:
            identity,_,_,helpers=stage._prepare_owned_ansa(run,cfg);r['identity']=identity
        stage._json_write(out,r)
        asyncio.run(verify(run,cfg,identity,helpers,r,a.cad.resolve(strict=True),resume))
    except Exception:r['error']=traceback.format_exc();print(r['error'],flush=True)
    finally:
        stage._json_write(out,r)
        if helpers:
            import grpc
            grpc.insecure_channel=helpers[2]
    print(json.dumps({'complete':r['complete'],'report':str(out)}),flush=True)
    return 0 if r['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
