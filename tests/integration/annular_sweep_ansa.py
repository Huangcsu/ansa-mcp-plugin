"""Owned annular template: explicit partition, public seeds/map/sweep and readback."""
import argparse,asyncio,json,sys,traceback,uuid,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/integration')]
import stage_three_ansa as stage
from map_block_batch_ansa import harness
from rectangular_candidates_ansa import SNAPSHOT
from map_block_prepare_ansa import ORACLE as PERSISTENCE_SNAPSHOT
from annular_persistence import complete_persistence

CUT_AND_SELECT=r'''
import importlib,math
from ansa import base
from ansa_mcp_bridge.operations import cons_seed,structured_meshing,annular_source,mapped_surface,geometry_features
for module in (geometry_features,cons_seed,structured_meshing,annular_source,mapped_surface):importlib.reload(module)
d=base.CurrentDeck()
if base.CollectEntities(d,None,'SHELL') or base.CollectEntities(d,None,'VOLUME'):raise RuntimeError('raw annulus copy required')
cuts=[];center=CASE['center'];axis=CASE['axis']
for radial in (CASE['u'],CASE['v']):
    p=tuple(center)+tuple(center[i]+axis[i] for i in range(3))+tuple(center[i]+radial[i] for i in range(3))
    cuts.append(base.PlaneCut(*p,base.CollectEntities(d,None,'FACE'),True,True))
selected=[];cons={};adjacent=set()
for face in base.CollectEntities(d,None,'FACE'):
    points=[hp.position for edge in base.CollectEntities(d,face,'CONS') for hp in base.CollectEntities(d,edge,'HOT POINT')]
    if points and all(abs(sum((point[i]-center[i])*axis[i] for i in range(3)))<=.001 for point in points):
        selected.append(face._id)
        for edge in base.CollectEntities(d,face,'CONS'):
            # Explicit known annular fixture only. Min Radius may be uncomputed
            # (zero) in the native card, so it is not an analytic classifier.
            hp=base.CollectEntities(d,edge,'HOT POINT')
            if len(hp)!=2:raise RuntimeError('known annular source requires open CONS')
            radii=[math.sqrt(sum((e.position[i]-center[i])**2 for i in range(3))) for e in hp]
            if any(min(abs(radius-CASE['inner_radius']),abs(radius-CASE['outer_radius']))>.001 for radius in radii):raise RuntimeError('explicit source cap radii mismatch')
            cons[edge._id]=6 if abs(radii[0]-radii[1])<.001 else 2
            adjacent.update(f._id for f in base.GetFacesOfCons(edge))
if len(selected)!=4 or len(cons)!=12:raise RuntimeError('known four-sector source mismatch: '+str((selected,cons)))
shell=base.CollectEntities(d,None,'PSHELL')
if len(shell)!=1:raise RuntimeError('one source property required')
mid=base.GetEntityCardValues(d,shell[0],['MID1'])['MID1']
material=base.GetEntity(d,'MAT1',mid)
if material is None or base.GetEntityCardValues(d,material,['DEFINED'])['DEFINED']!='YES':
    material=base.CreateEntity(d,'MAT1',{'DEFINED':'YES','E':210000.,'NU':.3,'RHO':7.85e-9})
    if material is None:raise RuntimeError('explicit fixture material failed')
    mid=material._id
prop=base.CreateEntity(d,'PSOLID',{'MID':mid})
if prop is None:raise RuntimeError('explicit fixture PSOLID failed')
RESULT={'cuts':cuts,'source_face_ids':sorted(selected),'face_ids':sorted(adjacent),
        'assignments':[{'cons_id':i,'segments':n} for i,n in sorted(cons.items())],'property_id':prop._id}
'''


async def verify(run,cfg,identity,helpers,r):
    r['complete']=False
    out=run/'annular-sweep-report.json'
    case=r.get('case_params',{'center':[0.,0.,0.],'axis':[0.,0.,1.],'u':[1.,0.,0.],'v':[0.,1.,0.],
             'inner_radius':10.,'outer_radius':20.,'distance':10.,'steps':2})
    async with harness(run,cfg,identity,helpers,r) as h:
        async def call(name,fields):
            op='annular-sweep-'+uuid.uuid4().hex;r['pending']={'tool':name,'operation_id':op};stage._json_write(out,r)
            sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
            if not sub.get('task_id'):raise RuntimeError(str(sub))
            r['pending']['task_id']=sub['task_id'];stage._json_write(out,r)
            done=await h.wait_task(sub['task_id'],None,55)
            r['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'result':done});r['pending']=None;stage._json_write(out,r)
            print(json.dumps({'tool':name,'status':done['status']}),flush=True)
            if done['status']!='succeeded':raise RuntimeError(str(done.get('error')))
            return done['data']['result']
        async def script(code):
            value=await call('ansa_script_execute',{'script':code,'validate':True})
            if value.get('result_paged'):raise RuntimeError('query original paged result; do not repeat')
            return value['result']
        async def snapshot(name, *, persistence=False):
            path_name='ORACLE_PATH' if persistence else 'SNAPSHOT_PATH'
            source=PERSISTENCE_SNAPSHOT if persistence else SNAPSHOT
            return await script(path_name+'='+repr(str(run/(name+'.json')))+'\n'+source)
        if r.get('prior_failed_seed'):
            prior=r['prior_failed_seed'];original=await h.call('ansa_task_query',{'task_id':prior['task_id']},allow_error=True)
            if original['status']!='failed' or original['operation_id']!=prior['operation_id']:
                raise RuntimeError('original seed failure not confirmed; no new branch allowed')
            r['confirmed_original_seed_failure']=original
        if r.get('resume_before_seed'):
            initial=r['selection']
            code=CUT_AND_SELECT[:CUT_AND_SELECT.index('cuts=[]')]
            code+="\nif base.CollectEntities(d,None,'GRID'):raise RuntimeError('fresh pre-seed CAD required')\ncuts=[]\n"
            code+=CUT_AND_SELECT[CUT_AND_SELECT.index('selected=[]'):].replace("prop=base.CreateEntity(d,'PSOLID',{'MID':mid})", "prop=base.GetEntity(d,'PSOLID',"+repr(initial['property_id'])+")")
            r['selection']=await script('CASE='+repr(case)+'\n'+code)
            if r['selection']['source_face_ids']!=initial['source_face_ids']:
                raise RuntimeError('pre-seed source FACE binding changed')
        else:
            if r.get('source_step'):
                await call('ansa_model_import',{'format':'STEP','path':r['source_step'],'mode':'replace','preservation':'backup','backup_path':str(run/'before-annular-sweep-branch.ansa')})
                await h.snapshot_context();await call('ansa_model_save',{'path':str(run/'industrial-raw-input.ansa')})
            else:
                await call('ansa_model_open',{'path':r['source_checkpoint'],'preservation':'backup','backup_path':str(run/'before-annular-sweep-branch.ansa')});await h.snapshot_context()
            r['selection']=await script('CASE='+repr(case)+'\n'+CUT_AND_SELECT)
        await call('ansa_model_save',{'path':str(run/'annular-sweep-partitioned.ansa')})
        selection=r['selection'];scope={'deck':1,'face_ids':selection['face_ids'],'cons_ids':[v['cons_id'] for v in selection['assignments']]}
        before=await call('ansa_mesh_seed_read',scope)
        r['seed']=await call('ansa_mesh_seed_apply',{'deck':1,'face_ids':scope['face_ids'],
            'assignments':selection['assignments'],'expected_seed_snapshot_sha256':before['seed_snapshot_sha256'],
            'backup_path':str(run/'before-annular-sweep-seeds.ansa')})
        if not r['seed'].get('ok') or not r['seed'].get('scope_verified'):raise RuntimeError('native seed verification failed')
        fresh=await call('ansa_mesh_seed_read',scope)
        faces=await call('ansa_mesh_faces',{'deck':1,'face_ids':selection['source_face_ids']})
        surface_scope={
                'face_ids':scope['face_ids'],'cons_ids':scope['cons_ids'],
                'expected_seed_snapshot_sha256':fresh['seed_snapshot_sha256'],
                'backup_path':str(run/'before-annular-mapped-source.ansa')}
        if r.get('max_boundary_restore_mm') is not None:surface_scope['max_boundary_restore_mm']=r['max_boundary_restore_mm']
        if r.get('allow_unmeshed_tessellation_updates'):surface_scope['allow_unmeshed_tessellation_updates']=True
        r['surface']=await call('ansa_mesh_surface',{'deck':1,'faces':[f['condition'] for f in faces['faces']],
            'element_type':'quad','method':'mapped_existing_seeds','seed_scope':{
                **surface_scope}})
        if not r['surface'].get('ok') or not r['surface'].get('scope_verified'):raise RuntimeError('mapped annular source verification failed')
        r['source']=await snapshot('annular-public-source')
        ids=r['surface']['created_shell_ids']
        desc=await call('ansa_mesh_source_audit',{'deck':1,'source_ids':ids,'direction':case['axis'],
                       'layers':case['steps'],'target_length':case['distance']/case['steps'],'annular_options':{
                           'center':case['center'],'axis':case['axis'],'inner_radius':case['inner_radius'],'outer_radius':case['outer_radius'],
                           'max_chord_error':.2,'tolerance':.001}})
        r['annular_source_descriptor']=desc
        if desc['summary']['annular_template_status']!='verified_mesh_template':raise RuntimeError('annular source unsupported')
        page=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':desc['report_id'],
                       'section':'source_snapshot_sha256','offset':0,'limit':1})
        r['sweep']=await call('ansa_mesh_sweep',{'deck':1,'shell_ids':ids,'direction':case['axis'],
            'distance':case['distance'],'steps':case['steps'],'property_id':selection['property_id'],
            'expected_source_sha256':page['rows'][0],'backup_path':str(run/'before-annular-public-sweep.ansa')})
        if not r['sweep'].get('ok') or not r['sweep'].get('scope_verified'):raise RuntimeError('annular sweep independent verification failed')
        r['final']=await snapshot('annular-public-final',persistence=True)
        profile={'analysis_type':'linear','element_family':'solid','criteria':[
            {'name':'ASPECT','max':5.},{'name':'JACOB','min':.2}]}
        r['quality']=await call('ansa_mesh_quality_report',{'deck':1,'profile':profile,'entity_ids':r['sweep']['new_solid_ids']})
        if r['quality'].get('report',r['quality'])['summary']['status']!='passed':raise RuntimeError('declared mesh quality criteria not passed')
        await call('ansa_model_save',{'path':str(run/'annular-public-solid.ansa')})
        await call('ansa_model_open',{'path':str(run/'annular-public-solid.ansa'),'preservation':'backup','backup_path':str(run/'before-annular-public-reopen.ansa')});await h.snapshot_context()
        r['reopened']=await snapshot('annular-public-reopened',persistence=True)
        a=json.loads(Path(r['final']['path']).read_text('utf-8'));b=json.loads(Path(r['reopened']['path']).read_text('utf-8'))
        complete_persistence(r,a,b)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_dir',type=Path);parser.add_argument('--corrected',action='store_true');parser.add_argument('--restore-budget',type=float);parser.add_argument('--resume-read',action='store_true');parser.add_argument('--public-final',action='store_true');parser.add_argument('--native-display',action='store_true');parser.add_argument('--public-case');parser.add_argument('--previous');parser.add_argument('--industrial-case',type=Path);args=parser.parse_args()
    owner=args.run_dir.resolve();run=owner/'annular-final-case' if args.resume_read else (owner/'annular-restoration-case' if args.restore_budget else (owner/'annular-corrected-case' if args.corrected else owner));out=run/'annular-sweep-report.json'
    if args.public_final:run=owner/'annular-public-final-case';out=run/'annular-sweep-report.json'
    if args.native_display:run=owner/'annular-native-display-case';out=run/'annular-sweep-report.json'
    if args.public_case:
        if Path(args.public_case).name!=args.public_case or args.public_case in ('.','..'):raise ValueError('run-local new case name required')
        run=owner/args.public_case;out=run/'annular-sweep-report.json'
    if args.industrial_case:run=owner/'annular-industrial-case';out=run/'annular-sweep-report.json'
    if out.exists():raise FileExistsError('query original task; do not repeat the annular branch')
    prior=json.loads((owner/'annular-seeded-report.json').read_text('utf-8'))
    if not prior['complete'] or prior['pending']:raise RuntimeError('confirmed terminal previous experiment required')
    identity=prior['identity'];cfg=json.loads((owner/'launch_instructions.json').read_text('utf-8'));r={'identity':identity,'complete':False,'pending':None,'calls':[],'source_checkpoint':str(owner/'annular-source.ansa')}
    if args.industrial_case:
        case=json.loads(args.industrial_case.resolve(strict=True).read_text('utf-8'))
        path=Path(case['path']).resolve(strict=True)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=case['sha256']:raise RuntimeError('industrial local STEP changed')
        r.update(case_params={k:case[k] for k in ('center','axis','inner_radius','outer_radius','distance','steps')},
                 source_step=str(path),industrial_extraction=case,max_boundary_restore_mm=1e-5,
                 allow_unmeshed_tessellation_updates=True)
        r['case_params'].update(u=[0.,1.,0.],v=[0.,0.,1.])
        run.mkdir(exist_ok=False)
    elif args.public_case:
        if not args.previous or Path(args.previous).name!=args.previous or args.previous in ('.','..'):raise ValueError('explicit prior run-local receipt directory required')
        failed=json.loads((owner/args.previous/'annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['result']['status']!='failed':raise RuntimeError('original failed task must be terminal')
        r.update(max_boundary_restore_mm=1e-5,allow_unmeshed_tessellation_updates=True,
                 prior_failed_seed={k:failed['calls'][-1][k] for k in ('task_id','operation_id')})
        run.mkdir(exist_ok=False)
    elif args.native_display:
        failed=json.loads((owner/'annular-public-final-case/annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['result']['status']!='failed':raise RuntimeError('prior mapped failure not terminal')
        r.update(max_boundary_restore_mm=1e-5,allow_unmeshed_tessellation_updates=True,
                 prior_failed_seed={k:failed['calls'][-1][k] for k in ('task_id','operation_id')})
        run.mkdir(exist_ok=False)
    elif args.public_final:
        failed=json.loads((owner/'annular-final-case/annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['result']['status']!='failed':raise RuntimeError('prior failure not terminal')
        r.update(max_boundary_restore_mm=1e-5,prior_failed_seed={k:failed['calls'][-1][k] for k in ('task_id','operation_id')})
        run.mkdir(exist_ok=False)
    elif args.resume_read:
        failed=json.loads((owner/'annular-restoration-case/annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['tool']!='ansa_mesh_seed_read' or failed['calls'][-1]['result']['status']!='failed':
            raise RuntimeError('original read-only diagnostic refusal required')
        r.update(resume_before_seed=True,selection=failed['selection'],max_boundary_restore_mm=1e-5,
                 prior_failed_seed={k:failed['calls'][-1][k] for k in ('task_id','operation_id')})
        run.mkdir(exist_ok=False)
    elif args.restore_budget is not None:
        if not 1e-12<=args.restore_budget<=1e-5:raise ValueError('explicit restoration budget outside public limits')
        r['max_boundary_restore_mm']=args.restore_budget
        failed=json.loads((owner/'annular-corrected-case/annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['result']['status']!='failed':
            raise RuntimeError('confirmed terminal mapped failure required')
        r['prior_failed_seed']={k:failed['calls'][-1][k] for k in ('task_id','operation_id')}
        run.mkdir(exist_ok=False)
    elif args.corrected:
        failed=json.loads((owner/'annular-sweep-report.json').read_text('utf-8'))
        if failed['complete'] or failed['pending'] or failed['calls'][-1]['result']['status']!='failed':
            raise RuntimeError('original terminal failed seed required')
        r['prior_failed_seed']={k:failed['calls'][-1][k] for k in ('task_id','operation_id')}
        run.mkdir(exist_ok=False)
    import grpc
    from ansa_mcp_shared.identity import same_process_alive
    one,two=stage._import_runtime_helpers();original=stage._bounded_grpc(grpc,one.DeadlineInterceptor)
    try:
        if not same_process_alive(identity['pid'],identity['process_start']):raise RuntimeError('owned identity changed')
        basic=two.rpc(identity['port'],one.remote_basic);stage._verify_program_arguments(basic,Path(cfg['translator']),identity['port'])
        if basic['pid']!=identity['pid'] or basic['program_args']!=identity['program_args']:raise RuntimeError('owned launch changed')
        asyncio.run(verify(run,cfg,identity,(one,two,original),r))
    except Exception:r['error']=traceback.format_exc();print(r['error'],flush=True)
    finally:stage._json_write(out,r);grpc.insecure_channel=original
    print(json.dumps({'complete':r['complete'],'report':str(out)}),flush=True)
    return 0 if r['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
