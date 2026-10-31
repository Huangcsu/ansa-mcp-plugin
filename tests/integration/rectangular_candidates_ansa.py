"""Owned public T2 discovery/cache validation and bounded industrial diagnostics."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/integration')]
import stage_three_ansa as stage
from map_block_batch_ansa import harness

SNAPSHOT=r'''
import builtins,json,hashlib
from pathlib import Path
from ansa import base
from ansa_mcp_bridge.operations import cad_map,geometry_features as f
s=cad_map._snapshot(builtins._ansa_mcp_plugin_bridge.state,base,base.CurrentDeck())
raw=json.dumps({'snapshot':s},ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
with Path(SNAPSHOT_PATH).open('xb') as stream:stream.write(raw)
RESULT={'path':SNAPSHOT_PATH,'sha256':hashlib.sha256(raw).hexdigest(),'scope_sha256':f._hash(s),'CAD_sha256':s['cad_sha256']}
'''
INFO="from ansa import base\nd=base.CurrentDeck()\nRESULT={'deck':d,'units':base.GetLengthUnits(),'face_ids':sorted(e._id for e in base.CollectEntities(d,None,'FACE')),'counts':{k:len(base.CollectEntities(d,None,k)) for k in ('FACE','CONS','GRID','SHELL','SOLID','VOLUME')}}\n"


async def verify(run,cfg,identity,helpers,r,samples):
    out=run/'rectangular-candidates-report.json'
    async with harness(run,cfg,identity,helpers,r) as h:
        async def call(name,fields):
            op='rectangular-candidates-'+uuid.uuid4().hex;r['pending']={'tool':name,'operation_id':op};stage._json_write(out,r)
            sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
            if not sub.get('task_id'):raise RuntimeError(str(sub))
            r['pending']['task_id']=sub['task_id'];stage._json_write(out,r)
            done=await h.wait_task(sub['task_id'],None,55)
            r['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'result':done});r['pending']=None;stage._json_write(out,r)
            print(json.dumps({'tool':name,'status':done['status']}),flush=True)
            if done['status']!='succeeded':raise RuntimeError(str(done.get('error')))
            return done['data']['result']
        async def script(code):
            reply=await call('ansa_script_execute',{'script':code,'validate':True})
            if reply.get('result_paged'):raise RuntimeError('query original paged result')
            return reply['result']
        async def snapshot(label):return await script('SNAPSHOT_PATH='+repr(str(run/(label+'.json')))+'\n'+SNAPSHOT)
        async def diagnose(label):
            info=await script(INFO)
            if info['deck']!=1 or info['units']!='millimeter':raise RuntimeError('finite diagnostics require NASTRAN millimeter scope')
            before=await snapshot(label+'-before')
            desc=await call('ansa_geometry_sweep_candidates',{'deck':1,'face_ids':info['face_ids'],'target_length':5.})
            page=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':'/classification/rectangular_assembly','offset':0,'limit':100})
            result={row['key']:row['value'] for row in page['rows']}
            cached=await call('ansa_geometry_sweep_candidates',{'deck':1,'face_ids':info['face_ids'],'target_length':5.})
            after=await snapshot(label+'-after')
            if before['scope_sha256']!=after['scope_sha256']:raise RuntimeError('diagnostics changed complete native model snapshot')
            if cached['summary']['cache_status']!='hit' or cached['summary']['scoped_export_calls']!=1:raise RuntimeError('cached proof did not revalidate parent CAD')
            row={'label':label,'native_info':info,'descriptor':desc,'rectangular_assembly':result,'cached':cached,
                 'before':before,'after':after,'full_snapshot_exact':True}
            r['cases'].append(row);stage._json_write(out,r)
            return row
        source=run/'before-map-blocks.ansa'
        await call('ansa_model_open',{'path':str(source),'preservation':'backup','backup_path':str(run/'before-raw-template-diagnostics.ansa')});await h.snapshot_context()
        await call('ansa_model_save',{'path':str(run/'rectangular-raw-input.ansa')})
        positive=await diagnose('native-two-block-partition')
        result=positive['rectangular_assembly']
        if result['status']!='verified_at_export_precision' or len(result['face_groups'])!=2 or result['prepare_options'] is None:
            raise RuntimeError('T2 finite template was not automatically discovered')
        bad=await call('ansa_geometry_sweep_candidates',{'deck':1,'face_ids':positive['native_info']['face_ids'],'target_length':3.})
        r['incompatible_target_length']=bad
        if bad['summary']['rectangular_assembly_status']!='unsupported':raise RuntimeError('incompatible integer edge lengths were accepted')
        for index,path in enumerate(samples):
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            await call('ansa_model_import',{'format':'STEP','path':str(path),'mode':'replace','preservation':'backup','backup_path':str(run/('before-industrial-template-'+str(index)+'.ansa'))});await h.snapshot_context()
            saved=run/(path.stem+'-template-input.ansa');await call('ansa_model_save',{'path':str(saved)})
            await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/('before-industrial-template-reopen-'+str(index)+'.ansa'))});await h.snapshot_context()
            row=await diagnose(path.stem)
            if row['rectangular_assembly']['status']=='unsupported' and row['rectangular_assembly']['prepare_options'] is not None:
                raise RuntimeError('unsupported industrial geometry received executable preparation options')
            if any(row['native_info']['counts'][k]!=0 for k in ('SHELL','SOLID','VOLUME')):raise RuntimeError('read-only diagnostic generated FE')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError('industrial source changed')
            row['source']={'path':str(path),'sha256':digest,'unchanged':True};stage._json_write(out,r)
        await call('ansa_model_open',{'path':str(run/'combined-solid.ansa'),'preservation':'backup','backup_path':str(run/'before-template-final-restore.ansa')});await h.snapshot_context()
        r['restored_solid']=await snapshot('template-restored-solid')
        original=json.loads((run/'oracle-reopened.json').read_text('utf-8'))['snapshot']
        current=json.loads((run/'template-restored-solid.json').read_text('utf-8'))['snapshot']
        r['restored_raw_snapshot_exact']=original==current
        if original!=current:raise RuntimeError('restored final mesh differs from accepted native checkpoint')
        r['complete']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path);p.add_argument('--samples',type=Path,nargs='+',required=True);a=p.parse_args()
    run=a.run_dir.resolve();out=run/'rectangular-candidates-report.json'
    if out.exists():raise FileExistsError('query original tasks, do not replay diagnostic imports')
    prior=json.loads((run/'map-block-batch-report.json').read_text('utf-8'));cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'))
    if not prior['complete'] or prior['pending']:raise RuntimeError('completed owned native baseline required')
    identity=prior['identity'];r={'identity':identity,'complete':False,'pending':None,'calls':[],'cases':[]}
    import grpc
    from ansa_mcp_shared.identity import same_process_alive
    one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor);helpers=(one,two,original)
    try:
        if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned process changed')
        basic=two.rpc(identity['port'],one.remote_basic);stage._verify_program_arguments(basic,Path(cfg['translator']),identity['port'])
        if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned launch changed')
        asyncio.run(verify(run,cfg,identity,helpers,r,[p.resolve(strict=True) for p in a.samples]))
    except Exception:r['error']=traceback.format_exc();print(r['error'],flush=True)
    finally:stage._json_write(out,r);grpc.insecure_channel=original
    print(json.dumps({'complete':r['complete'],'report':str(out)}),flush=True)
    return 0 if r['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
