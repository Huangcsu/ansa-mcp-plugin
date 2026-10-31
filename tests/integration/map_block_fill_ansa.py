"""One-shot public MCP CAD -> MapBlock -> existing-volume fill acceptance."""
import argparse
import asyncio
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


async def verify(run,cfg,identity,helpers,report,cad):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    output=run/'map-block-fill-report.json'
    args=StdioServerParameters(command=sys.executable,args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'map-block-fill.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(args,errlog=err) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema();await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,fields,expected='succeeded',operation=None):
                    op=operation or 'map-block-fill-'+uuid.uuid4().hex
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
                    if r.get('result_paged'):raise RuntimeError('unexpected paging; query original result descriptor')
                    return r['result']
                async def oracle(name):return await script('ORACLE_PATH='+repr(str(run/name))+'\n'+ORACLE)
                async def plan_hash(desc):
                    return (await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':'plan_sha256','offset':0,'limit':1}))['rows'][0]
                await call('ansa_model_open',{'path':str(cad),'preservation':'backup','backup_path':str(run/'before-cad-open.ansa')});await h.snapshot_context()
                report['material_fixture']=await script("from ansa import base\nd=base.CurrentDeck()\nassert base.SetEntityCardValues(d,base.GetEntity(d,'MAT1',1),{'E':123456.,'NU':.27,'RHO':1.23e-9,'DEFINED':'YES'})==0\nassert base.SetEntityCardValues(d,base.GetEntity(d,'PSHELL',1),{'MID1':1,'MID2':1,'MID3':1})==0\nRESULT={'non_default_source_material':True}\n")
                await call('ansa_model_save',{'path':str(run/'raw-custom-cad.ansa')})
                prepare={'deck':1,'face_ids':[1,2,3,4,5,6],'source_face_id':1,'target_face_id':2,
                         'direction':[0.,0.,1.],'distance':10.,'target_length':5.,'property_id':2}
                desc=await call('ansa_mesh_map_block_prepare',{'action':'plan',**prepare})
                prepare_sha=await plan_hash(desc)
                report['prepare']=await call('ansa_mesh_map_block_prepare',{'action':'apply',**prepare,'expected_plan_sha256':prepare_sha,'backup_path':str(run/'before-prepare.ansa')})
                if not report['prepare'].get('ok'):raise AssertionError('MapBlock prepare did not pass')
                before=await oracle('oracle-before-fill-plan.json')
                desc=await call('ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_id':1});report['plan']=desc
                fill_sha=await plan_hash(desc)
                after=await oracle('oracle-after-fill-plan.json')
                if before['scope_sha256']!=after['scope_sha256']:raise AssertionError('fill plan changed model')
                report['stale_plan']=await call('ansa_mesh_map_block_fill',{'action':'apply','deck':1,'volume_id':1,'expected_plan_sha256':'0'*64,'backup_path':str(run/'unused-stale-fill.ansa')},'failed')
                if (run/'unused-stale-fill.ansa').exists():raise AssertionError('stale fill wrote backup')
                fields={'action':'apply','deck':1,'volume_id':1,'expected_plan_sha256':fill_sha,'backup_path':str(run/'before-controlled-fill.ansa')}
                operation='controlled-map-block-fill-'+uuid.uuid4().hex
                report['fill']=await call('ansa_mesh_map_block_fill',fields,operation=operation)
                task_id=report['calls'][-1]['task_id'];result=report['fill']
                if not result.get('ok') or not result.get('scope_verified') or len(result['new_solid_ids'])!=48:
                    raise AssertionError('controlled fill not independently verified')
                repeated=await h.call('ansa_mesh_map_block_fill',{**fields,'operation_id':operation,'expected_target':h.target,'expected_model':h.model})
                if repeated.get('task_id')!=task_id:raise AssertionError('idempotent fill did not return original task')
                report['idempotent_task_id']=task_id
                report['before_rejections']=await oracle('oracle-before-rejections.json')
                report['filled_rejection']=await call('ansa_mesh_map_block_fill',{'action':'plan','deck':1,'volume_id':1},'failed')
                report['stale_evidence']=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':'plan_sha256','offset':0,'limit':1},'failed')
                after_rejections=await oracle('oracle-after-rejections.json')
                if report['before_rejections']['scope_sha256']!=after_rejections['scope_sha256']:raise AssertionError('rejected filled/stale operations changed model')
                profile={'analysis_type':'linear','element_family':'solid','criteria':[
                    {'name':'ASPECT','max':5.},{'name':'JACOB','min':.2},{'name':'MINL','min':4.5},{'name':'MAXL','max':5.5}]}
                report['quality_scope']='explicit available synthetic fixture metrics; not a project quality profile'
                report['quality']=await call('ansa_mesh_quality_report',{'deck':1,'profile':profile,'entity_ids':result['new_solid_ids']})
                quality=report['quality'].get('report',report['quality'])
                if quality['summary']['status']!='passed' or quality['summary']['checked_count']!=48:
                    raise AssertionError('explicit synthetic-fixture quality profile did not pass')
                before_quality=after_rejections
                after_quality=await oracle('oracle-after-quality.json')
                if before_quality['scope_sha256']!=after_quality['scope_sha256']:raise AssertionError('read-only quality measurement changed model')
                saved=run/'controlled-map-block-solid.ansa'
                await call('ansa_model_save',{'path':str(saved)})
                await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-final-reopen.ansa')});await h.snapshot_context()
                await oracle('oracle-after-reopen.json')
                from ansa_mcp_bridge.operations import part_aggregates
                a=json.loads((run/'oracle-after-quality.json').read_text('utf-8'));b=json.loads((run/'oracle-after-reopen.json').read_text('utf-8'))
                report['persistence']=part_aggregates.compare_persistence(a['snapshot'],b['snapshot'],{int(k):v for k,v in a['aggregates'].items()},{int(k):v for k,v in b['aggregates'].items()})
                report['complete']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path);p.add_argument('--cad',type=Path,required=True);a=p.parse_args()
    run=a.run_dir.resolve();output=run/'map-block-fill-report.json'
    if output.exists():raise FileExistsError('query original task; never replay this one-shot acceptance')
    cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'));r={'complete':False,'calls':[],'pending':None};helpers=None
    try:
        identity,_,_,helpers=stage._prepare_owned_ansa(run,cfg);r['identity']=identity;stage._json_write(output,r)
        asyncio.run(verify(run,cfg,identity,helpers,r,a.cad.resolve(strict=True)))
    except Exception:r['error']=traceback.format_exc();print(r['error'],flush=True)
    finally:
        stage._json_write(output,r)
        if helpers:
            import grpc
            grpc.insecure_channel=helpers[2]
    print(json.dumps({'complete':r['complete'],'report':str(output)}),flush=True)
    return 0 if r['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
