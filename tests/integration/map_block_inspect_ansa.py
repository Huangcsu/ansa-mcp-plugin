"""Read existing MapBlock checkpoints and verify bounded large-script paging.

Runs once on a newly owned instance. It never invokes MapBlock or replays the
original oversized-result task. Inputs must be the saved probe checkpoints.
"""
import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/integration')]
import stage_three_ansa as stage

SNAPSHOT_HASH="""
import builtins
from ansa import base
from ansa_mcp_bridge.operations.cad_map import _snapshot
from ansa_mcp_bridge.operations.geometry_features import _hash
s=_snapshot(builtins._ansa_mcp_plugin_bridge.state,base,base.CurrentDeck())
RESULT={'sha256':_hash(s),'entities':len(s['entities'])}
"""


async def verify(run,cfg,identity,helpers,report,checkpoints):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',cfg['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    output=run/'map-block-inspect-report.json'
    with (run/'map-block-inspect.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema();await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,fields,expected='succeeded',operation=None):
                    op=operation or 'map-block-inspect-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':op};stage._json_write(output,report)
                    sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
                    if not sub.get('task_id'):raise RuntimeError(str(sub))
                    report['pending']['task_id']=sub['task_id'];stage._json_write(output,report)
                    done=await h.wait_task(sub['task_id'],expected,55)
                    value=done['data']['result'] if expected=='succeeded' else done
                    report['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'status':done['status'],'result':value})
                    report['pending']=None;stage._json_write(output,report)
                    print(json.dumps({'tool':name,'status':done['status']}),flush=True)
                    return value
                async def script(code):
                    result=await call('ansa_script_execute',{'script':code,'validate':True})
                    if result.get('result_paged'):raise RuntimeError('helper unexpectedly paged; inspect original result descriptor')
                    return result['result']
                async def page(desc,section,offset=0,expected='succeeded'):
                    return await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':section,'offset':offset,'limit':100},expected)
                previous=None
                for label,path in checkpoints:
                    await call('ansa_model_open',{'path':str(path),'preservation':'backup','backup_path':str(run/('before-'+label+'.ansa'))})
                    await h.snapshot_context()
                    if previous is not None:report['old_context_rejection']=await page(previous,'volumes',expected='failed')
                    before=await script(SNAPSHOT_HASH)
                    desc=await call('ansa_mesh_map_block_inspect',{'deck':1,'volume_ids':[1]})
                    volumes=(await page(desc,'volumes'))['rows']
                    after=await script(SNAPSHOT_HASH)
                    if before!=after:raise AssertionError('read-only Map Block inspection changed model')
                    if label=='box' and desc['summary']['native_ready_count']!=1:raise AssertionError('box native ready not confirmed')
                    if label=='i10' and desc['summary']['native_non_mappable_count']!=1:raise AssertionError('i10 non-mappable lost')
                    report['cases'].append({'label':label,'descriptor':desc,'volumes':volumes,'before':before,'after':after})
                    previous=desc
                before_paging=await script(SNAPSHOT_HASH)
                # Real >1 MiB result; same operation must return the same compact receipt.
                code="import builtins\nbuiltins._map_block_paging_runs=getattr(builtins,'_map_block_paging_runs',0)+1\nRESULT={'runs':builtins._map_block_paging_runs,'rows':[{'i':i,'text':'界'*512} for i in range(1000)]}\n"
                op='map-block-large-result-'+uuid.uuid4().hex
                large=await call('ansa_script_execute',{'script':code,'validate':True},operation=op)
                if not large.get('result_paged'):raise AssertionError('large script result was not paged')
                rows=[];offset=0
                while True:
                    p=await page(large['result'],'/result/rows',offset)
                    rows.extend(p['rows'])
                    if p['complete']:break
                    offset=p['next_offset']
                if rows!=[{'i':i,'text':'界'*512} for i in range(1000)]:raise AssertionError('large nested list incomplete')
                duplicate=await call('ansa_script_execute',{'script':code,'validate':True},operation=op)
                if duplicate!=large:raise AssertionError('idempotent paged receipt changed')
                runs=await script("import builtins\nRESULT={'runs':builtins._map_block_paging_runs}\n")
                if runs['runs']!=1:raise AssertionError('large script was executed more than once')
                scalar=await call('ansa_script_execute',{'script':"RESULT={'blob':'界\\n'*250000}",'validate':True})
                if not scalar.get('result_paged'):raise AssertionError('large scalar was not paged')
                chunks=[];offset=0
                while True:
                    p=await page(scalar['result'],'/result/blob',offset)
                    if p['paging_unit']!='string_chunks':raise AssertionError('large scalar chunk contract missing')
                    chunks.extend(r['text'] for r in p['rows'])
                    if p['complete']:break
                    offset=p['next_offset']
                blob=''.join(chunks)
                if blob!='界\n'*250000:raise AssertionError('large scalar incomplete')
                after_paging=await script(SNAPSHOT_HASH)
                if before_paging!=after_paging:raise AssertionError('paging tests changed engineering model')
                report['large_results']={'list_descriptor':large,'scalar_descriptor':scalar,'list_rows':len(rows),'list_utf8_bytes':len(json.dumps(rows,ensure_ascii=False).encode('utf-8')),
                    'scalar_characters':len(blob),'scalar_sha256':hashlib.sha256(blob.encode('utf-8')).hexdigest(),'single_execution_confirmed':True,'model_unchanged':True}
                final=run/'map-block-final.ansa'
                await call('ansa_model_save',{'path':str(final)})
                await call('ansa_model_open',{'path':str(final),'preservation':'backup','backup_path':str(run/'before-final-reopen.ansa')})
                await h.snapshot_context()
                reopened=await script(SNAPSHOT_HASH)
                report['persistence']={'before':after_paging,'reopened':reopened,'full_snapshot_exact':reopened==after_paging}
                if reopened!=after_paging:raise AssertionError('final save/reopen full engineering snapshot differs')
                report['complete']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path)
    p.add_argument('--box',type=Path,required=True);p.add_argument('--i10',type=Path,required=True)
    args=p.parse_args();run=args.run_dir.resolve();output=run/'map-block-inspect-report.json'
    if output.exists():raise FileExistsError('query original pending task; do not replay acceptance')
    cfg=json.loads((run/'launch_instructions.json').read_text('utf-8'));report={'complete':False,'pending':None,'calls':[],'cases':[]};helpers=None
    try:
        identity,_,_,helpers=stage._prepare_owned_ansa(run,cfg);report['identity']=identity
        stage._json_write(output,report)
        asyncio.run(verify(run,cfg,identity,helpers,report,[('box',args.box.resolve(strict=True)),('i10',args.i10.resolve(strict=True))]))
        report['cleanup']=stage._cleanup_owned_ansa(identity,cfg,helpers)
    except Exception:report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:
        stage._json_write(output,report)
        if helpers:
            import grpc
            grpc.insecure_channel=helpers[2]
    print(json.dumps({'complete':report['complete'],'report':str(output)}),flush=True)
    return 0 if report['complete'] and report.get('cleanup',{}).get('stopped_owned_process') else 1


if __name__=='__main__':raise SystemExit(main())
