"""Bounded structured tools through real stdio MCP in an explicitly owned ANSA.

Uses stage-three's unique translator/PID/main-thread identity gate before any
fixture write. Never selects, replaces or stops an existing user ANSA instance.
"""

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage


FIXTURE = """
from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('requires NASTRAN')
if base.CollectEntities(deck, None, 'SHELL') or base.CollectEntities(deck, None, 'FACE'):
    raise RuntimeError('requires fresh owned fixture')
def create(kind, fields):
    e = base.CreateEntity(deck, kind, fields)
    if e is None: raise RuntimeError('create failed: '+kind)
    return e
material = create('MAT1', {'DEFINED':'YES','E':210000.,'NU':.3,'RHO':7.8e-9})
pshell = create('PSHELL', {'MID1':material._id,'MID2':material._id,'MID3':material._id,'T':1.})
part=base.NewPart('structured-native-fixture')
if part is None: raise RuntimeError('NewPart failed')
if base.SetCurrentEntity(pshell)!=0 or base.SetCurrentEntity(part)!=0:
    raise RuntimeError('current fixture property/part failed')
faces=[]
for x in (10.,50.):
    points=[[x,0.,0.],[x+20.,0.,0.],[x+20.,10.,0.],[x,10.,0.]]
    curves=[base.CurvesNew([points[i],points[(i+1)%4]]) for i in range(4)]
    if any(curve is None for curve in curves): raise RuntimeError('curve fixture creation failed')
    created=base.FacesNewPlanar(curves,ret_ents=True)
    if not created or len(created)!=1: raise RuntimeError('face fixture creation failed')
    faces.append(created[0])
psolid = create('PSOLID', {'MID':material._id})
nodes = [create('GRID', dict(zip(('X1','X2','X3'), p))) for p in
         [(0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.),(2.,0.,0.)]]
shells=[]
for conn in [(0,1,2,3),(1,4,2)]:
    fields={'type':'CQUAD4' if len(conn)==4 else 'CTRIA3','PID':pshell._id}
    fields.update({'G'+str(i+1):nodes[n]._id for i,n in enumerate(conn)})
    shells.append(create('SHELL', fields))
for face in faces:
    base.SetEntityCardValues(deck,face,{'__part__':part._id,'__prop__':pshell._id})
RESULT={'shell_ids':[e._id for e in shells], 'property_id':psolid._id,
        'face_ids':[e._id for e in faces]}
"""

ORACLE = """
import hashlib,json
from ansa import base
d=base.CurrentDeck()
rows=[]
for kind in ('GRID','SHELL','SOLID','PSHELL','PSOLID','MAT1'):
    for e in sorted(base.CollectEntities(d,None,kind) or [],key=lambda e:e._id):
        names=tuple(e.card_fields(d))
        values=base.GetEntityCardValues(d,e,names)
        clean={k:v for k,v in values.items() if isinstance(v,(str,int,float,bool)) or v is None}
        rows.append({'type':kind,'id':e._id,'fields':clean})
RESULT={'fingerprint':hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'rows':rows, 'faces':len(base.CollectEntities(d,None,'FACE') or [])}
"""


def remote_diagnostics():
    import builtins
    import traceback
    from ansa import base
    bridge = builtins._ansa_mcp_plugin_bridge
    return {"probe": bridge.state.probe(), "stack": traceback.format_stack()[-18:],
            "executor": {k: repr(v)[:512] for k, v in vars(bridge.executor).items()
                         if k != "state"},
            "counts": {kind: len(base.CollectEntities(base.CurrentDeck(), None, kind) or [])
                       for kind in ("GRID", "SHELL", "FACE", "SOLID")}}


def remote_partition_rows():
    from ansa import base
    from ansa_mcp_bridge.operations import structured_native as native
    d=base.CurrentDeck()
    return [row for row in native._capture_scope(base,d)["entities"]
            if row["entity"]["type"] in ("VOLUME","PSOLID","MAT1","SOLID")]


def remote_refresh_structured(pid, arguments):
    import builtins
    import importlib
    import os
    import threading
    from ansa import session
    if (os.getpid()!=pid or list(session.ProgramArguments())!=arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError('owned refresh mismatch')
    if builtins._ansa_mcp_plugin_bridge.executor._in_tick:
        raise RuntimeError('cannot refresh while executor is active')
    from ansa_mcp_bridge.operations import structured_native, structured_meshing, mesh_provenance, structured_evidence
    for module in (structured_native, mesh_provenance, structured_evidence, structured_meshing):
        importlib.reload(module)
    return {'refreshed':True}


async def verify(run, instructions, identity, helpers, report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    def record(name,data):
        report['cases'].append({'name':name,'data':data})
        stage._json_write(run/'structured-meshing-report.json',report)
        print(json.dumps({'case':name,'ok':True}),flush=True)
    with (run/'structured-backend.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h=stage.McpHarness(client,report,run)
                record('schema',await h.list_schema())
                await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def script(source):
                    value=await h.direct_result('ansa_script_execute',{'script':source,'validate':True})
                    return value['result']
                if report.get('resume_fixture'):
                    fixture=report['resume_fixture']
                    await h.direct_result('ansa_model_open',{'path':str(run/'structured-fixture.ansa'),
                        'preservation':'backup','backup_path':str(run/('failed-readback-'+uuid.uuid4().hex+'.ansa'))})
                else:
                    fixture=await script(FIXTURE)
                    record('fixture',fixture)
                    await h.direct_result('ansa_model_save',{'path':str(run/'structured-fixture.ansa')})
                await h.snapshot_context()
                plane={'origin':[20.,0.,0.],'normal':[1.,0.,0.]}
                args={'deck':1,'face_ids':[fixture['face_ids'][0]],'plane':plane,
                      'protected_face_ids':[fixture['face_ids'][1]],'produce_plane_faces':False}
                preview=await h.direct_result('ansa_geometry_partition_plan',args)
                record('partition_preview',preview)
                before=await script(ORACLE)
                rejected=await h.direct_result('ansa_geometry_partition_apply',{
                    **args,'expected_plan_sha256':'0'*64,'backup_path':str(run/'never-stale.ansa')},expected='failed')
                assert (await script(ORACLE))==before
                record('stale_partition_rejected_unchanged',rejected)
                partition=await h.direct_result('ansa_geometry_partition_apply',{
                    **args,'expected_plan_sha256':preview['plan_sha256'],
                    'backup_path':str(run/('before-partition-'+uuid.uuid4().hex+'.ansa'))})
                assert partition['ok'] and partition['scope_verified'], partition
                record('partition_apply',partition)
                audit=await h.direct_result('ansa_mesh_source_audit',{
                    'deck':1,'source_ids':fixture['shell_ids'],'direction':[0.,0.,1.],
                    'layers':2,'target_length':1.})
                record('source_audit',audit)
                plan=await h.direct_result('ansa_mesh_block_plan',{'deck':1,'plan':{
                    'template':'straight_sweep','blocks':[{'id':'mixed-source',
                        'source':{'type':'SHELL','id':fixture['shell_ids'][0]},
                        'target':{'type':'PLANNED_SECTION','id':'translated-end'},
                        'depends_on':[],'sweep_direction':[0.,0.,1.],'layers':2}],
                    'interfaces':[], 'seed_constraints':[
                        {'id':'e1','edge_id':'e1','segments':2},
                        {'id':'e2','edge_id':'e2','segments':3},
                        {'id':'sum','left_edges':['e1','e2'],'right_edges':['e3']}]}})
                record('integer_seed_plan',plan)
                sweep_args={'deck':1,'shell_ids':fixture['shell_ids'],'direction':[0.,0.,1.],
                    'distance':2.,'steps':2,'property_id':fixture['property_id']}
                rejected=await h.direct_result('ansa_mesh_sweep',{
                    **sweep_args,'expected_source_sha256':'0'*64,
                    'backup_path':str(run/'never-stale-sweep.ansa')},expected='failed')
                record('stale_source_rejected',rejected)
                sweep=await h.direct_result('ansa_mesh_sweep',{
                    **sweep_args,'expected_source_sha256':audit['snapshot_sha256'],
                    'backup_path':str(run/('before-sweep-'+uuid.uuid4().hex+'.ansa'))})
                assert sweep['ok'] and sweep['scope_verified'],sweep
                assert sweep['new_mesh']['verification']['solid_type_counts']=={'CHEXA':2,'CPENTA':2}
                record('mixed_sweep',sweep)
                ledger=await h.direct_result('ansa_mesh_provenance',{
                    'deck':1,'action':'read','ledger_id':sweep['ledger_id']})
                mapping=await h.direct_result('ansa_mesh_evidence_page',{
                    'deck':1,'report_id':ledger['report_id'],'section':'/ledger/fe_mapping/pairs','limit':200})
                assert len(mapping['rows'])==2 and all(len(row['created'])==2 for row in mapping['rows']),mapping
                record('native_fe_provenance_pairs',mapping)
                full=await h.direct_result('ansa_mesh_interface_audit',{
                    'deck':1,'entity_ids':sweep['new_solid_ids']})
                assert full['status']=='incomplete',full
                record('unknown_geometry_not_passed',full)
                page=await h.direct_result('ansa_mesh_evidence_page',{
                    'deck':1,'report_id':full['report_id'],'section':'summary','limit':200})
                record('evidence_page',page)
                quality=await h.direct_result('ansa_mesh_quality_report',{
                    'deck':1,'entity_ids':sweep['new_solid_ids'],
                    'profile':{'analysis_type':'linear','element_family':'solid',
                        'criteria':[{'name':'ASPECT','max':5.},{'name':'JACOB','min':.3}]}})
                record('native_quality',quality)
                before=await script(ORACLE)
                final=run/('structured-final-'+uuid.uuid4().hex+'.ansa')
                await h.direct_result('ansa_model_save',{'path':str(final)})
                await h.snapshot_context()
                await h.direct_result('ansa_model_open',{'path':str(final),'preservation':'backup',
                    'backup_path':str(run/('before-reopen-'+uuid.uuid4().hex+'.ansa'))})
                await h.snapshot_context()
                after=await script(ORACLE)
                assert before==after,{'before':before,'after':after}
                record('save_reopen_independent_cards',{'fingerprint':after['fingerprint'],'path':str(final)})


def main():
    run=Path(sys.argv[1]).resolve()
    instructions=json.loads((run/'launch_instructions.json').read_text(encoding='utf-8'))
    if '--cleanup' in sys.argv:
        import grpc
        saved=json.loads((run/'structured-meshing-report.json').read_text(encoding='utf-8'))
        if saved.get('complete') is not True:
            raise RuntimeError('cleanup requires completed owned acceptance evidence')
        one,two=stage._import_runtime_helpers()
        original=stage._bounded_grpc(grpc,one.DeadlineInterceptor)
        try:
            saved['cleanup']=stage._cleanup_owned_ansa(saved['identity'],instructions,(one,two,original))
            stage._json_write(run/'structured-meshing-report.json',saved)
            print(json.dumps(saved['cleanup']),flush=True)
        finally:
            grpc.insecure_channel=original
        return 0
    report={'cases':[],'complete':False,'scope':'ANSA 2026.1 NASTRAN bounded tools; no solver/CAD-wide acceptance'}
    grpc=None
    helpers=None
    try:
        if '--resume' in sys.argv:
            import grpc
            helpers0=stage._import_runtime_helpers()
            original=stage._bounded_grpc(grpc,helpers0[0].DeadlineInterceptor)
            helpers=(*helpers0,original)
            candidates=[run/'structured-meshing-report.json']+sorted(
                run.glob('structured-prior-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            previous=None
            for candidate in candidates:
                content=json.loads(candidate.read_text(encoding='utf-8'))
                fixture=content.get('resume_fixture') or next(
                    (c['data'] for c in content.get('cases',[]) if c['name']=='fixture'),None)
                if content.get('identity') and fixture:
                    previous=content
                    report['resume_fixture']=fixture
                    break
            if previous is None: raise RuntimeError('no recoverable owned fixture evidence')
            identity=previous['identity']
            from ansa_mcp_shared.identity import same_process_alive
            if not same_process_alive(identity.get('pid'),identity.get('process_start')):
                raise RuntimeError('owned resume process exited or PID was reused')
            basic=helpers0[1].rpc(identity['port'],helpers0[0].remote_basic)
            stage._verify_program_arguments(basic,Path(identity['translator']),identity['port'])
            if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:
                raise RuntimeError('owned resume identity changed')
            helpers0[1].rpc(identity['port'],remote_refresh_structured,identity['pid'],identity['program_args'])
            stage._json_write(run/('structured-prior-'+uuid.uuid4().hex+'.json'),previous)
        else:
            identity,_,grpc,helpers=stage._prepare_owned_ansa(run,instructions)
        report['identity']=identity
        asyncio.run(verify(run,instructions,identity,helpers,report))
        report['complete']=True
    except Exception:
        report['error']=traceback.format_exc()
        print(report['error'],flush=True)
    finally:
        stage._json_write(run/'structured-meshing-report.json',report)
        if grpc is not None and helpers is not None:
            grpc.insecure_channel=helpers[2]
    return 0 if report['complete'] else 1


if __name__=='__main__':
    raise SystemExit(main())
