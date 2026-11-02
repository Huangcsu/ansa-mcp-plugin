"""Owned ANSA acceptance: original CAD CONS -> native integer subdivisions."""

import asyncio
import copy
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage
from geometry_features_ansa import FIXTURE, ORACLE


def verify_saved_cards(before, after):
    """Compare exact native rows; log tightly bounded Part display roundoff."""
    rows=copy.deepcopy(after['rows']);changes=[]
    assert len(before['rows'])==len(rows),'save/reopen changed native entity count'
    allowed={'Mass','Area','COG x','COG y','COG z','Ixx','Iyy','Izz','Ixy','Ixz','Iyz'}
    for old,new in zip(before['rows'],rows):
        assert (old['type'],old['id'])==(new['type'],new['id']),'save/reopen changed entity identity'
        if old['type']!='ANSAPART':continue
        assert old['fields'].keys()==new['fields'].keys(),'save/reopen changed Part card fields'
        for key in old['fields']:
            x,y=old['fields'][key],new['fields'][key]
            if x==y:continue
            assert (key in allowed and type(x) in (int,float) and type(y) in (int,float)
                    and math.isfinite(x) and math.isfinite(y) and math.isclose(x,y,rel_tol=1e-12,abs_tol=0.)), \
                   f'save/reopen changed protected Part field {key}'
            changes.append({'part_id':old['id'],'field':key,'before':x,'after':y})
            new['fields'][key]=x
    assert before['rows']==rows,'save/reopen changed native definitions, geometry or FE'
    return {'before_raw_sha256':before['fingerprint'],'after_raw_sha256':after['fingerprint'],
            'part_display_roundoff':changes,'relative_tolerance':1e-12,'absolute_tolerance':0.}


def counts_script(ids):
    return """
from ansa import base
d=base.CurrentDeck();rows=[]
for ident in IDS:
    edge=base.GetEntity(d,'CONS',ident)
    c=base.GetEntityCardValues(d,edge,['Num. of Nodes','Order of Nodes'])
    nodes=base.CollectEntities(d,edge,'GRID')
    rows.append({'cons_id':ident,'internal_nodes':c['Num. of Nodes'],'order':c['Order of Nodes'],
                 'point_ids':sorted(e._id for e in nodes),
                 'points':sorted([e._id,list(e.position)] for e in nodes)})
RESULT={'rows':rows,'shell_count':len(base.CollectEntities(d,None,'SHELL')),
        'solid_count':len(base.CollectEntities(d,None,'SOLID'))}
""".replace('IDS', repr(ids))


async def verify(run, instructions, identity, helpers, report, sample_root):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    path = run / 'cons-seed-report.json'
    params = StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--direct-backend','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'cons-seed.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize();h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema()
                await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,args,expected='succeeded'):
                    op='cons-seed-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':op}
                    stage._json_write(path,report);start=time.monotonic()
                    submitted,completed=await h.task(name,args,operation_id=op,expected=expected)
                    value=completed['data']['result'] if completed['status']=='succeeded' else completed
                    report['calls'].append({'tool':name,'operation_id':op,'task_id':submitted['task_id'],
                        'state':completed['status'],'elapsed_seconds':time.monotonic()-start,'result':value})
                    report['pending']=None;stage._json_write(path,report)
                    return value
                async def script(code):return (await call('ansa_script_execute',{'script':code,'validate':True}))['result']
                async def page(desc,section,expected='succeeded'):
                    return await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],
                        'section':section,'offset':0,'limit':100},expected)
                fixture=await script(FIXTURE)
                await call('ansa_model_save',{'path':str(run/'synthetic-source.ansa')})
                await call('ansa_model_open',{'path':str(run/'synthetic-source.ansa'),'preservation':'backup',
                    'backup_path':str(run/'before-synthetic-source-reopen.ansa')})
                await h.snapshot_context()
                scope={'deck':1,'face_ids':fixture['face_ids'],'cons_ids':[1,3,4,5,7,8]}
                oracle_before=await script(ORACLE)
                initial=await call('ansa_mesh_seed_read',scope)
                assert oracle_before==await script(ORACLE),'seed read changed native cards'
                initial_rows=(await page(initial,'boundaries'))['rows']
                assert all(r['status']=='supported' for r in initial_rows)
                assert all(r['points'] is None and r['point_count'] is None for r in initial_rows)
                assert oracle_before==await script(ORACLE),'seed pagination changed native cards'
                edges=[]
                for i in scope['cons_ids']:
                    edge={'id':str(i),'kind':'line' if i in (4,8) else 'circular_arc',
                          'length':5. if i in (4,8) else math.tau,'target_length':1.,'multiple':2}
                    if i not in (4,8):edge.update(radius=2.,max_chord_error=.02)
                    edges.append(edge)
                planned=await call('ansa_mesh_seed_plan',{'deck':1,'plan':{'edges':edges,
                    'equal_groups':[['1','3','5','7'],['4','8']]}})
                planned_rows=(await page(planned,'edge_segments'))['rows']
                assignments=[{'cons_id':int(r['edge_id']),'segments':r['count']} for r in planned_rows]
                args={'deck':1,'face_ids':scope['face_ids'],'assignments':assignments,
                      'expected_seed_snapshot_sha256':'0'*64,'backup_path':str(run/'bad-seed-must-not-exist.ansa')}
                await call('ansa_mesh_seed_apply',args,'failed');assert not Path(args['backup_path']).exists()
                assert oracle_before==await script(ORACLE)
                args.update(expected_seed_snapshot_sha256=initial['seed_snapshot_sha256'],
                            backup_path=str(run/'before-synthetic-seeds.ansa'))
                applied=await call('ansa_mesh_seed_apply',args)
                assert applied['ok'] and applied['scope_verified'] and applied['source_geometry_unchanged']
                assert applied['provenance_stage']=='verified'
                assert applied['summary']['number_perimeters_calls']==2
                actual=await script(counts_script(scope['cons_ids']))
                wanted={r['cons_id']:r['segments'] for r in assignments}
                assert all(r['internal_nodes']==wanted[r['cons_id']]-1 and len(r['point_ids'])==wanted[r['cons_id']]+1 for r in actual['rows'])
                assert actual['shell_count']==actual['solid_count']==0
                await page(initial,'boundaries','failed')
                args['backup_path']=str(run/'stale-seed-must-not-exist.ansa')
                await call('ansa_mesh_seed_apply',args,'failed');assert not Path(args['backup_path']).exists()
                saved=run/'synthetic-final.ansa';await call('ansa_model_save',{'path':str(saved)})
                final_oracle=await script(ORACLE)
                await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-synthetic-reopen.ansa')})
                await h.snapshot_context();saved_cards=verify_saved_cards(final_oracle,await script(ORACLE))
                assert actual==await script(counts_script(scope['cons_ids']))
                report['cases'].append({'name':'synthetic_declared_geometry_plan_to_native_cons_save_reopen',
                    'planner':planned,'assignments':assignments,'applied':applied,'independent_readback':actual,
                    'save_reopen_cards':saved_cards})
                # Already materialized interior GRID remain protected. Reseeding
                # them is a separate, deliberately unopened capability.
                current=await call('ansa_mesh_seed_read',scope)
                reseed={**args,'assignments':[{'cons_id':r['cons_id'],'segments':r['segments']+2} for r in assignments],
                        'expected_seed_snapshot_sha256':current['seed_snapshot_sha256'],
                        'backup_path':str(run/'before-protected-interior-reseed.ansa')}
                unchanged=await script(ORACLE)
                rejected=await call('ansa_mesh_seed_apply',reseed,'failed')
                rejected_result=rejected['data']['result']['operation_result']
                assert rejected_result['api_called'] is False and rejected_result['summary']['number_perimeters_calls']==0
                assert unchanged==await script(ORACLE),'protected-interior rejection changed original model'
                report['cases'].append({'name':'preexisting_interior_grid_reseed_rejected_before_number_perimeters',
                                        'rejection':rejected})
                # Sample import is explicitly backed up; original CAD files are never saved over.
                for filename in ('i10o_simp.step','i02o_m2.step','i08o_m8.step','i23o_s31.step'):
                    source=sample_root/filename;source_hash=hashlib.sha256(source.read_bytes()).hexdigest();stem=source.stem
                    imported=await call('ansa_model_import',{'format':'STEP','path':str(source),'mode':'replace',
                        'preservation':'backup','backup_path':str(run/('before-import-'+stem+'.ansa'))})
                    await h.snapshot_context()
                    await call('ansa_model_save',{'path':str(run/(stem+'-source.ansa'))})
                    await call('ansa_model_open',{'path':str(run/(stem+'-source.ansa')),'preservation':'backup',
                        'backup_path':str(run/(stem+'-before-source-reopen.ansa'))})
                    await h.snapshot_context()
                    # Select three explicit open perimeters, retaining all their adjacent FACE.
                    selection=await script("""
from ansa import base
d=base.CurrentDeck();chosen=[];faces=set()
for edge in sorted(base.CollectEntities(d,None,'CONS'),key=lambda e:e._id):
    c=base.GetEntityCardValues(d,edge,['Order of Nodes','Frozen','Collapsed','Biasing'])
    if len(base.CollectEntities(d,edge,'HOT POINT'))!=2 or c!={'Order of Nodes':'1st','Frozen':'NO','Collapsed':'NO','Biasing':'LINEAR'}:continue
    adjacent=base.GetFacesOfCons(edge)
    if len(faces|{e._id for e in adjacent})>20:continue
    chosen.append(edge._id);faces.update(e._id for e in adjacent)
    if len(chosen)==3:break
RESULT={'cons_ids':chosen,'face_ids':sorted(faces),'face_count':len(base.CollectEntities(d,None,'FACE'))}
""")
                    assert len(selection['cons_ids'])==3
                    subset={'deck':1,'face_ids':selection['face_ids'],'cons_ids':selection['cons_ids']}
                    old=await call('ansa_mesh_seed_read',subset)
                    assign=[{'cons_id':i,'segments':6 if j<2 else 8} for j,i in enumerate(subset['cons_ids'])]
                    mutation={'deck':1,'face_ids':subset['face_ids'],'assignments':assign,
                              'expected_seed_snapshot_sha256':old['seed_snapshot_sha256'],
                              'backup_path':str(run/(stem+'-before-seeds.ansa'))}
                    start=time.monotonic()
                    out=await call('ansa_mesh_seed_apply',mutation,
                                   expected='succeeded' if filename in ('i10o_simp.step','i02o_m2.step') else None)
                    if out.get('status')=='failed':
                        diagnosis=out['data']['result']['operation_result']
                        assert diagnosis['recovery_required'] and diagnosis['backup']['verified']
                        assert diagnosis['source_geometry_unchanged'] and diagnosis['api_called']
                        assert len(diagnosis['native_calls'])==2 and all(c['return_code']==1 for c in diagnosis['native_calls'])
                        assert 'boundary point projection failed' in diagnosis['error']
                        assert any(p['maximum_observed_distance']>p['tolerance'] for p in diagnosis['point_projections'])
                        recovery_path=run/(stem+'-recovery-required.ansa')
                        await call('ansa_model_save',{'path':str(recovery_path)})
                        assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                        report['cases'].append({'name':'industrial_native_point_precision_requires_recovery',
                            'source':str(source),'source_sha256':source_hash,'selection':selection,
                            'rejection':out,'saved_recovery_model':str(recovery_path),
                            'acceptance':'not_accepted_as_successful_subdivision'})
                        stage._json_write(path,report)
                        print(json.dumps({'sample':filename,'verified':False,'recovery_required':True}),flush=True)
                        continue
                    assert out['ok'] and out['scope_verified'] and out['source_geometry_unchanged']
                    direct=await script(counts_script(subset['cons_ids']))
                    assert [r['internal_nodes'] for r in direct['rows']]==[r['segments']-1 for r in assign]
                    assert [len(r['point_ids']) for r in direct['rows']]==[r['segments']+1 for r in assign]
                    assert direct['shell_count']==direct['solid_count']==0
                    final_path=run/(stem+'-seeded.ansa');await call('ansa_model_save',{'path':str(final_path)})
                    independent=await script(ORACLE)
                    await call('ansa_model_open',{'path':str(final_path),'preservation':'backup',
                                                'backup_path':str(run/(stem+'-before-reopen.ansa'))})
                    await h.snapshot_context();saved_cards=verify_saved_cards(independent,await script(ORACLE))
                    assert direct==await script(counts_script(subset['cons_ids']))
                    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                    report['cases'].append({'name':'industrial_original_cons_seed_save_reopen','source':str(source),
                        'source_sha256':source_hash,'selection':selection,'applied':out,'readback':direct,
                        'save_reopen_cards':saved_cards,
                        'saved_model':str(final_path),'elapsed_seconds':time.monotonic()-start})
                    print(json.dumps({'sample':filename,'cons_count':3,'verified':True}),flush=True)
                    stage._json_write(path,report)
                # Unit refusal is exercised through the dedicated tool on an
                # actual inch CAD import, without silently scaling dimensions.
                source=sample_root/'i29o_bracket.step'
                source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
                await call('ansa_model_import',{'format':'STEP','path':str(source),'mode':'replace',
                    'preservation':'backup','backup_path':str(run/'before-inch-import.ansa')})
                await h.snapshot_context()
                await call('ansa_model_save',{'path':str(run/'inch-source.ansa')})
                await call('ansa_model_open',{'path':str(run/'inch-source.ansa'),'preservation':'backup',
                    'backup_path':str(run/'before-inch-source-reopen.ansa')})
                await h.snapshot_context()
                selected=await script("from ansa import base\nd=base.CurrentDeck()\nf=base.GetEntity(d,'FACE',1)\n"
                    "RESULT={'units':base.GetLengthUnits(),'cons_id':base.CollectEntities(d,f,'CONS')[0]._id}")
                assert selected['units']=='inch'
                compact_oracle=ORACLE.replace("'rows':rows", "'row_count':len(rows)")
                unchanged=await script(compact_oracle)
                refusal=await call('ansa_mesh_seed_read',{'deck':1,'face_ids':[1],'cons_ids':[selected['cons_id']]},'failed')
                assert 'millimeter' in json.dumps(refusal)
                assert unchanged==await script(compact_oracle)
                assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                report['cases'].append({'name':'inch_cad_rejected_without_implicit_scaling','source':str(source),
                    'source_sha256':source_hash,'units':selected['units'],'rejection':refusal})


def main():
    if len(sys.argv)!=3:raise ValueError('usage: cons_seed_ansa.py <new-owned-run-dir> <sample-root>')
    run=Path(sys.argv[1]).resolve();samples=Path(sys.argv[2]).resolve();path=run/'cons-seed-report.json'
    if path.exists():raise FileExistsError('refuse replay of previous native acceptance')
    instructions=json.loads((run/'launch_instructions.json').read_text(encoding='utf-8'))
    sources=[Path(__file__).resolve(),*(ROOT/'src'/p for p in (
        'ansa_mcp_bridge/operations/cons_seed.py','ansa_mcp_bridge/operations/geometry_features.py',
        'ansa_mcp_bridge/operations/geometry_features_iges.py','ansa_mcp_bridge/operations/geometry_fidelity.py',
        'ansa_mcp_bridge/operations/structured_meshing.py','ansa_mcp_bridge/operations/structured_native.py',
        'ansa_mcp_bridge/operations/structured_evidence.py','ansa_mcp_bridge/operations/__init__.py',
        'ansa_mcp_plugin/mcp/structured_meshing.py','ansa_mcp_shared/operations.py'))]
    def hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    report={'complete':False,'pending':None,'calls':[],'cases':[],'source_sha256':hashes()}
    grpc=helpers=None
    try:
        stage.PANEL_WAIT_SECONDS=60
        identity,_,grpc,helpers=stage._prepare_owned_ansa(run,instructions)
        report['identity']=identity;stage._json_write(path,report)
        asyncio.run(verify(run,instructions,identity,helpers,report,samples))
        assert report['source_sha256']==hashes(),'source changed during native acceptance'
        report['complete']=True;report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
    except Exception:
        report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:
        stage._json_write(path,report)
        if grpc is not None and helpers is not None:grpc.insecure_channel=helpers[2]
    return 0 if report['complete'] and report.get('cleanup',{}).get('stopped_owned_process') else 1


if __name__=='__main__':raise SystemExit(main())
