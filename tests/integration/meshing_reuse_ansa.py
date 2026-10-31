"""在专属 ANSA 验证参数化分段、多块预检、合批生成和共享接口。"""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage
from structured_meshing_ansa import FIXTURE,ORACLE


async def verify(run,instructions,identity,helpers,report):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'meshing-reuse.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h=stage.McpHarness(client,report,run)
                schema=await h.list_schema();assert schema['count']==83
                report['schema']=schema
                await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,args):
                    operation='meshing-reuse-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':operation}
                    stage._json_write(run/'meshing-reuse-report.json',report)
                    start=time.monotonic()
                    submitted,completed=await h.task(name,args,operation_id=operation)
                    value=completed['data']['result']
                    report['calls'].append({'tool':name,'operation_id':operation,'task_id':submitted['task_id'],
                                            'elapsed_seconds':time.monotonic()-start,'result':value})
                    report['pending']=None;stage._json_write(run/'meshing-reuse-report.json',report)
                    return value
                async def script(source):return (await call('ansa_script_execute',{'script':source,'validate':True}))['result']
                async def page(desc,section):
                    result=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],'section':section,'offset':0,'limit':200})
                    assert result['next_offset'] is None
                    return result['rows']
                seed=await call('ansa_mesh_seed_plan',{'deck':1,'plan':{'edges':[
                    {'id':'hole-quarter','kind':'circular_arc','length':4.71238898038469,'radius':3.,'max_chord_error':.02,'target_length':1.},
                    {'id':'matching-edge','kind':'line','length':4.,'target_length':1.}],
                    'equal_groups':[['hole-quarter','matching-edge']]}})
                assert seed['status']=='complete'
                constraints=await page(seed,'seed_constraints');assert sum(len(c['edge_ids']) for c in constraints)==2
                report['cases'].append({'name':'curvature_seed_plan','result':seed,'constraints':constraints})
                for index,(scale,steps) in enumerate(((1.,2),(3.,3))):
                    await call('ansa_model_new',{'preservation':'backup','backup_path':str(run/f'before-new-{index}.ansa')})
                    await h.snapshot_context()
                    fixture=await script(FIXTURE)
                    if scale!=1.:
                        await script("from ansa import base\nd=base.CurrentDeck()\n"
                                     "for n in base.CollectEntities(d,None,'GRID'):\n"
                                     "    c=base.GetEntityCardValues(d,n,['X1','X2','X3'])\n"
                                     f"    base.SetEntityCardValues(d,n,{{k:v*{scale!r} for k,v in c.items()}})\n"
                                     "RESULT={'scaled':True}\n")
                    fixture_path=run/f'reuse-source-{index}.ansa'
                    await call('ansa_model_save',{'path':str(fixture_path)})
                    # SaveAs 的成功不能证明当前数据库名已经切换；显式重开后再建立写上下文。
                    await call('ansa_model_open',{'path':str(fixture_path),'preservation':'backup',
                                                'backup_path':str(run/f'before-source-open-{index}.ansa')})
                    await h.snapshot_context()
                    blocks=[{'id':name,'shell_ids':[eid],'direction':[0.,0.,1.],
                             'distance':2.*scale,'steps':steps,'property_id':fixture['property_id']}
                            for name,eid in zip(('quad-block','triangle-transition'),fixture['shell_ids'])]
                    before=await script(ORACLE)
                    incompatible=json.loads(json.dumps(blocks));incompatible[1]['steps']=steps+1
                    reject=await call('ansa_mesh_sweep_prepare',{'deck':1,'blocks':incompatible,'max_penta_fraction':1.})
                    assert reject['status']=='blocked'
                    rejected_groups=await page(reject,'groups');assert all('sweep_params' not in g for g in rejected_groups)
                    assert before==await script(ORACLE)
                    reject_fraction=await call('ansa_mesh_sweep_prepare',{'deck':1,'blocks':blocks,'max_penta_fraction':.1})
                    assert reject_fraction['status']=='blocked';assert before==await script(ORACLE)
                    prepared=await call('ansa_mesh_sweep_prepare',{'deck':1,'blocks':blocks,'max_penta_fraction':.5})
                    assert prepared['status']=='ready_for_existing_sweep'
                    groups=await page(prepared,'groups');assert len(groups)==1
                    assert prepared['summary']['source_capture_calls']==1
                    assert prepared['summary']['separate_block_sweep_calls']==2
                    assert before==await script(ORACLE)
                    result=await call('ansa_mesh_sweep',{**groups[0]['sweep_params'],'backup_path':str(run/f'before-sweep-{index}.ansa')})
                    assert result['ok'] and result['provenance_stage']=='verified'
                    assert len(result['new_solid_ids'])==2*steps
                    oracle=await script("""
from ansa import base
from collections import Counter
d=base.CurrentDeck()
nodes={}
for n in base.CollectEntities(d,None,'GRID'):
    c=base.GetEntityCardValues(d,n,['X1','X2','X3'])
    nodes[n._id]=tuple(c[k] for k in ('X1','X2','X3'))
faces=Counter();types=Counter()
for e in base.CollectEntities(d,None,'SOLID'):
    typ=base.GetEntityCardValues(d,e,['type'])['type'];types[typ]+=1
    count=8 if typ=='CHEXA' else 6
    c=base.GetEntityCardValues(d,e,['G'+str(i) for i in range(1,count+1)])
    ids=[c['G'+str(i)] for i in range(1,count+1)]
    local=([(0,1,2,3),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
           if count==8 else [(0,1,2),(3,4,5),(0,1,4,3),(1,2,5,4),(2,0,3,5)])
    for f in local:faces[tuple(sorted(ids[i] for i in f))]+=1
interface=[list(f) for f,n in faces.items() if n==2 and len(f)==4 and
           all(abs(nodes[i][0]-SCALE)<1e-9 for i in f) and max(nodes[i][2] for i in f)>min(nodes[i][2] for i in f)]
RESULT={'types':dict(types),'grid_count':len(nodes),'coordinate_count':len(set(nodes.values())),
        'shared_vertical_facets':interface,'nonmanifold_facets':sum(n>2 for n in faces.values())}
""".replace('SCALE',repr(scale)))
                    assert oracle['types']=={'CHEXA':steps,'CPENTA':steps}
                    assert oracle['grid_count']==oracle['coordinate_count']==5*(steps+1)
                    assert len(oracle['shared_vertical_facets'])==steps and oracle['nonmanifold_facets']==0
                    fingerprint=await script(ORACLE)
                    final=run/f'meshing-reuse-final-{index}.ansa'
                    await call('ansa_model_save',{'path':str(final)});await h.snapshot_context()
                    await call('ansa_model_open',{'path':str(final),'preservation':'backup','backup_path':str(run/f'before-reopen-{index}.ansa')})
                    await h.snapshot_context();assert fingerprint==await script(ORACLE)
                    report['cases'].append({'name':f'coalesced_scale_{scale}_layers_{steps}','prepared':prepared,
                                            'actual_readback':oracle,'saved_model':str(final),'fingerprint':fingerprint['fingerprint']})
                    stage._json_write(run/'meshing-reuse-report.json',report)
                    print(json.dumps({'case':report['cases'][-1]['name'],'passed':True}),flush=True)


def main():
    if len(sys.argv)!=2:raise ValueError('usage: meshing_reuse_ansa.py <new-owned-run-dir>')
    run=Path(sys.argv[1]).resolve();path=run/'meshing-reuse-report.json'
    if path.exists():raise FileExistsError('refuse to replay prior native operations')
    instructions=json.loads((run/'launch_instructions.json').read_text(encoding='utf-8'))
    report={'complete':False,'scope':'bounded explicit source blocks; no S25 remesh or arbitrary CAD partitioning',
            'cases':[],'calls':[],'pending':None}
    source_paths=[Path(__file__).resolve(),*(ROOT/'src'/p for p in (
        'ansa_mcp_bridge/operations/mesh_seed.py','ansa_mcp_bridge/operations/structured_batch.py',
        'ansa_mcp_bridge/operations/structured_meshing.py','ansa_mcp_bridge/operations/structured_native.py',
        'ansa_mcp_plugin/mcp/structured_meshing.py','ansa_mcp_shared/operations.py'))]
    def hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    report['source_sha256']=hashes()
    helpers=None;grpc=None
    try:
        stage.PANEL_WAIT_SECONDS=60
        identity,_,grpc,helpers=stage._prepare_owned_ansa(run,instructions)
        report['identity']=identity;stage._json_write(path,report)
        asyncio.run(verify(run,instructions,identity,helpers,report))
        assert report['source_sha256']==hashes(),'source changed during native validation'
        report['complete']=True
        report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
    except Exception:
        report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:
        stage._json_write(path,report)
        if grpc is not None and helpers is not None:grpc.insecure_channel=helpers[2]
    return 0 if report['complete'] and report.get('cleanup',{}).get('stopped_owned_process') else 1


if __name__=='__main__':raise SystemExit(main())
