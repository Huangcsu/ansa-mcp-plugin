"""Explicit native geometry snapshots and cache freshness through real MCP.

Only a new uniquely owned GUI is eligible; this never attaches to a user model.
"""
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

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage


FIXTURE = """
from ansa import base,constants
d=base.CurrentDeck()
if d!=constants.NASTRAN or base.CollectEntities(d,None,'FACE') or base.CollectEntities(d,None,'CURVE'):
    raise RuntimeError('requires fresh owned NASTRAN instance')
material=base.CreateEntity(d,'MAT1',{'DEFINED':'YES','E':210000.,'NU':.3})
prop=base.CreateEntity(d,'PSHELL',{'MID1':material._id,'T':1.})
part=base.NewPart('geometry-feature-acceptance')
if base.SetCurrentEntity(prop)!=0 or base.SetCurrentEntity(part)!=0:raise RuntimeError('fixture context failed')
base.CreateCurve(2,[10.,14.],[0.,0.],[0.,0.])
base.CreateCircleCenter2PointsRadius((20.,0.,0.),(1.,0.,0.),(0.,1.,0.),3.,True,False)
curves=list(base.CollectEntities(d,None,'CURVE'))
for e in curves:
    p=base.GetEntityPart(e)
    if p is None or p._id!=part._id:raise RuntimeError('curve part assignment failed')
faces=base.CreateVolumeCylinder([0.,0.,0.],[0.,0.,5.],5.,2.,part,prop,False)
if not faces:raise RuntimeError('cylinder fixture failed')
RESULT={'face_ids':sorted(e._id for e in faces),'curve_ids':sorted(e._id for e in curves)}
"""


ORACLE = """
import hashlib,json,math
from ansa import base
d=base.CurrentDeck();rows=[]
for kind in ('FACE','CONS','CURVE','HOT POINT','GRID','SHELL','SOLID','ANSAPART','PSHELL','MAT1'):
    for e in sorted(base.CollectEntities(d,None,kind) or [],key=lambda x:x._id):
        fields=tuple(e.card_fields(d))
        c=base.GetEntityCardValues(d,e,fields) if fields else {}
        vals={k:v for k,v in c.items() if v is None or type(v) in (str,bool,int) or type(v) is float and math.isfinite(v)}
        p=base.GetEntityPart(e) if kind in ('FACE','CURVE') else None
        rows.append({'type':kind,'id':e._id,'fields':vals,'part_id':p._id if p else None})
        if kind=='HOT POINT':
            rows[-1].update(position=list(e.position),cons_ids=sorted(c._id for c in base.GetConsOfHotPoints(e)))
RESULT={'fingerprint':hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'rows':rows}
"""


async def verify(run, instructions, identity, helpers, report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    with (run/'geometry-features.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema()
                assert report['schema']['count']==83
                await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,args,expected='succeeded'):
                    operation='geometry-features-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':operation}
                    stage._json_write(run/'geometry-features-report.json',report)
                    start=time.monotonic()
                    submitted,completed=await h.task(name,args,operation_id=operation,expected=expected)
                    value=completed['data']['result'] if expected=='succeeded' else completed
                    report['calls'].append({'tool':name,'task_id':submitted['task_id'],'operation_id':operation,
                                           'state':completed['status'],'elapsed_seconds':time.monotonic()-start,'result':value})
                    report['pending']=None
                    stage._json_write(run/'geometry-features-report.json',report)
                    return value
                async def script(code):return (await call('ansa_script_execute',{'script':code,'validate':True}))['result']
                async def page(desc,section,expected='succeeded'):
                    return await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],
                        'section':section,'offset':0,'limit':100},expected)
                fixture=await script(FIXTURE)
                initial=run/'geometry-features-source.ansa'
                await call('ansa_model_save',{'path':str(initial)})
                await call('ansa_model_open',{'path':str(initial),'preservation':'backup','backup_path':str(run/'before-open.ansa')})
                await h.snapshot_context()
                scope={'deck':1,**fixture,'seed_options':{'target_length':1.,'max_chord_error':.02}}
                before=await script(ORACLE)
                first=await call('ansa_geometry_features',scope)
                assert first['summary']['cache_status']=='miss' and first['summary']['seed_status']=='complete'
                curves=(await page(first,'curves'))['rows']
                assert len(curves)==3 and all(r['analysis']['status']=='supported' for r in curves)
                arcs=[r['analysis'] for r in curves if r['analysis']['kind']=='circular_arc']
                assert len(arcs)==2 and all(abs(r['radius']-3.)<1e-9 for r in arcs)
                surfaces=(await page(first,'surfaces'))['rows']
                assert len(surfaces)==4
                assert any(r['radius_s']['infinite'] or r['radius_t']['infinite'] for r in surfaces)
                vertices=(await page(first,'vertices'))['rows']
                contours=(await page(first,'/contours/faces'))['rows']
                boundaries=(await page(first,'boundaries'))['rows']
                assert len(vertices)==4 and len(boundaries)==6 and len(contours)==4
                assert first['summary']['contour_status']=='complete'
                assert first['summary']['cons_hot_point_calls']==6
                assert first['summary']['hot_point_adjacency_calls']==4
                for face in contours:
                    assert face['status']=='complete' and face['geometry_status']=='incomplete'
                    assert len(face['loops'])==1 and face['loops'][0]['kind']=='closed_contour'
                    assert face['loops'][0]['length_source']=='native_card_approximation'
                assert all(row['endpoint_topology']['status']=='available' for row in boundaries)
                seed=(await page(first,'/seed_plan/seed_constraints'))['rows']
                assert seed and all(row['segments']>0 for row in seed)
                second=await call('ansa_geometry_features',scope)
                assert second['summary']['cache_status']=='hit'
                assert second['summary']['scoped_export_calls']==1 and second['summary']['surface_info_calls']==0
                assert first['source_snapshot_sha256']==second['source_snapshot_sha256']
                assert before==await script(ORACLE),'read-only feature operations changed native model'
                report['cases'].append({'name':'native_features_seed_cache_and_readonly','first':first,'cached':second,
                    'curve_analyses':curves,'surfaces':surfaces,'seed_constraints':seed,'native_before_after':before['fingerprint']})
                report['cases'].append({'name':'native_cons_topology_is_not_analytic_geometry',
                    'vertices':vertices,'boundaries':boundaries,'contours':contours})
                epoch=h.model['epoch']
                await script("from ansa import base\nd=base.CurrentDeck()\nfaces=[base.GetEntity(d,'FACE',i) for i in "+repr(fixture['face_ids'])+"]\n"
                    "rc=base.GeoTranslate('MOVE',0,'SAME PART','NONE',0.,3.,0.,faces,draw_results=False,keep_connectivity=True)\n"
                    "if rc!=0:raise RuntimeError('owned boundary move failed')\nRESULT={'moved_faces':[e._id for e in faces]}\n")
                await h.snapshot_context()
                assert h.model['epoch']==epoch
                boundary_stale=await page(first,'vertices',expected='failed')
                assert 'stale' in json.dumps(boundary_stale).lower()
                boundary_changed=await call('ansa_geometry_features',scope)
                assert boundary_changed['summary']['cache_status']=='miss'
                assert boundary_changed['source_snapshot_sha256']!=first['source_snapshot_sha256']
                shifted=(await page(boundary_changed,'vertices'))['rows']
                assert [v['id'] for v in shifted]==[v['id'] for v in vertices]
                for old,new in zip(vertices,shifted):
                    assert max(abs(new['position'][k]-old['position'][k]-(3. if k==1 else 0.)) for k in range(3))<1e-10
                report['cases'].append({'name':'same_epoch_boundary_move_invalidates_topology_evidence',
                    'epoch':epoch,'new':boundary_changed,'vertices':shifted,'rejection':boundary_stale})
                moving=fixture['curve_ids'][0]
                await script("from ansa import base\nd=base.CurrentDeck()\ne=base.GetEntity(d,'CURVE',"+str(moving)+")\n"
                    "rc=base.GeoTranslate('MOVE',0,'SAME PART','NONE',0.,2.,0.,[e],draw_results=False,keep_connectivity=True)\n"
                    "if rc!=0:raise RuntimeError('owned geometry move failed')\nRESULT={'moved':e._id}\n")
                await h.snapshot_context()
                assert h.model['epoch']==epoch,'same-lifetime edit test requires unchanged epoch'
                stale=await page(boundary_changed,'curves',expected='failed')
                assert 'stale' in json.dumps(stale).lower()
                changed=await call('ansa_geometry_features',scope)
                assert changed['summary']['cache_status']=='miss'
                assert changed['source_snapshot_sha256']!=first['source_snapshot_sha256']
                report['cases'].append({'name':'same_epoch_translation_rejects_old_evidence','epoch':epoch,'new':changed,'rejection':stale})
                saved=run/'geometry-features-final.ansa'
                await call('ansa_model_save',{'path':str(saved)})
                native_before=await script(ORACLE)
                await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-final-open.ansa')})
                await h.snapshot_context()
                assert native_before==await script(ORACLE)
                reopened=await call('ansa_geometry_features',scope)
                assert reopened['summary']['cache_status']=='miss' and reopened['summary']['seed_status']=='complete'
                reopened_contours=(await page(reopened,'/contours/faces'))['rows']
                for original,reloaded in zip(contours,reopened_contours):
                    assert original['face_id']==reloaded['face_id']
                    assert reloaded['status']=='complete' and reloaded['geometry_status']=='incomplete'
                    assert [(v['cons_ids'],v['directions']) for v in original['loops']]==[
                        (v['cons_ids'],v['directions']) for v in reloaded['loops']]
                report['cases'].append({'name':'save_reopen_new_context','saved_model':str(saved),'features':reopened,
                                         'native_fingerprint':native_before['fingerprint']})
                stage._json_write(run/'geometry-features-report.json',report)


def main():
    if len(sys.argv)!=2:raise ValueError('usage: geometry_features_ansa.py <new-owned-run-dir>')
    run=Path(sys.argv[1]).resolve();path=run/'geometry-features-report.json'
    if path.exists():raise FileExistsError('refuse to replay a prior native validation')
    instructions=json.loads((run/'launch_instructions.json').read_text(encoding='utf-8'))
    report={'complete':False,'cases':[],'calls':[],'pending':None}
    source_paths=[Path(__file__).resolve(),*(ROOT/'src'/p for p in (
        'ansa_mcp_bridge/operations/geometry_features.py','ansa_mcp_bridge/operations/geometry_features_iges.py',
        'ansa_mcp_bridge/operations/geometry_contours.py',
        'ansa_mcp_bridge/operations/structured_meshing.py','ansa_mcp_bridge/operations/structured_evidence.py',
        'ansa_mcp_plugin/mcp/structured_meshing.py','ansa_mcp_shared/operations.py'))]
    def hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    report['source_sha256']=hashes();helpers=None;grpc=None
    try:
        stage.PANEL_WAIT_SECONDS=60
        identity,_,grpc,helpers=stage._prepare_owned_ansa(run,instructions)
        report['identity']=identity;stage._json_write(path,report)
        asyncio.run(verify(run,instructions,identity,helpers,report))
        assert report['source_sha256']==hashes(),'source changed during native acceptance'
        report['complete']=True
        report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
    except Exception:
        report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:
        stage._json_write(path,report)
        if grpc is not None and helpers is not None:grpc.insecure_channel=helpers[2]
    return 0 if report['complete'] and report.get('cleanup',{}).get('stopped_owned_process') else 1


if __name__=='__main__':raise SystemExit(main())
