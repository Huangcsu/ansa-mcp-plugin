"""Controlled native Map of one explicit, premeshed, finite planar CAD prism."""
import copy
import json
import math

from . import cons_seed as seeds, geometry_features as features, inspection, meshing
from . import structured_native as native, volume_meshing as guards
from .cad_sweep_topology import plan_translation
from .cad_sweep_geometry import capture as capture_geometry
from .cad_map_mesh import verify_translation, _SpatialIndex
from .mapped_surface_topology import four_sides, verify_lattice
from .structured_batch import source_subset

TOLERANCE=1e-7
_PARAMS=('deck','face_ids','source_face_id','target_face_id','direction','distance','steps','property_id','output_part_id')


def normalize(params, applying=False):
    inspection._params(params,_PARAMS+(('expected_plan_sha256','backup_path') if applying else ()))
    clean={k:params[k] for k in _PARAMS}
    for key in ('deck','source_face_id','target_face_id','property_id','output_part_id'):
        clean[key]=native._positive_int(clean[key],key)
    clean['face_ids']=sorted(native._id_list(clean['face_ids'],'face_ids',6))
    if len(clean['face_ids'])!=6 or clean['source_face_id']==clean['target_face_id']:
        raise ValueError('CAD Map requires six distinct FACEs and distinct caps')
    if not {clean['source_face_id'],clean['target_face_id']}<=set(clean['face_ids']):
        raise ValueError('both caps must belong to face_ids')
    vector=native._point(clean['direction'],'direction')
    # Keep an already-unit float vector verbatim: normalizing it repeatedly is
    # not bitwise idempotent and would invalidate plan/backup fingerprints.
    clean['direction']=(vector if abs(math.hypot(*vector)-1.)<=1e-14 else
                        native._unit_vector(vector,'direction'))
    clean['distance']=features._number(clean['distance'],'distance',True)
    if type(clean['steps']) is not int or not 1<=clean['steps']<=100:
        raise ValueError('steps must be an integer in 1..100')
    if applying:
        clean['expected_plan_sha256']=native._validate_digest(params['expected_plan_sha256'],'expected_plan_sha256')
        clean['backup_path']=params['backup_path']
    return clean


def _snapshot(state,base,deck):
    result=seeds._guard_snapshot(state,base,deck)
    # The generic guard omits Num. Elem and Part inertia. Restore EVERY raw
    # card here, then permit only independently checked changes in readback.
    for row in result['entities']:
        reference=row['entity'];entity=features._entity(base,deck,reference['type'],reference['id'])
        raw=guards._read_full_card(base,deck,entity,reference,'CAD Map protected card',omit_derived=False)
        row.update(raw);row.pop('ignored_derived_fields',None)
    by_id={row['entity']['id']:row for row in result['entities'] if row['entity']['type']=='GRID'}
    for row in result['grids']:
        row['card']={k:copy.deepcopy(by_id[row['id']][k]) for k in ('entity','card_fields','fields')}
    if len(guards._encoded(result))>8*1024*1024:raise ValueError('CAD Map model snapshot exceeds 8 MiB')
    return result


def _reserve_budget(before, predicted_solids, predicted_grids):
    if predicted_grids<0 or len(before['entities'])+predicted_solids+predicted_grids+1>10000:
        raise ValueError('predicted Map additions exceed the 10000-entity model protection budget')


def _face_lattice(snapshot, sides):
    coords={r['id']:r['xyz'] for r in snapshot['nodes']}
    paths=[]
    for side in sides:
        start,end=side['start_xyz'],side['end_xyz'];segments=side['segments']
        delta=[end[i]-start[i] for i in range(3)]
        length2=math.fsum(x*x for x in delta)
        if length2<=TOLERANCE*TOLERANCE:raise ValueError('degenerate CONS')
        path=[]
        for j in range(segments+1):
            expected=[start[i]+delta[i]*j/segments for i in range(3)]
            matches=[i for i,p in coords.items() if math.dist(p,expected)<=TOLERANCE]
            if len(matches)!=1:raise ValueError('mapped boundary does not match uniform native CONS seeds')
            path.append(matches[0])
        paths.append(path)
    lattice=verify_lattice(snapshot['elements'],paths)
    nx,ny=lattice['segments'];p=[side['start_xyz'] for side in sides]
    error=0.
    for j,row in enumerate(lattice['lattice_node_ids']):
        for i,node_id in enumerate(row):
            u,v=i/nx,j/ny
            weights=((1-u)*(1-v),u*(1-v),u*v,(1-u)*v)
            expected=[math.fsum(weights[n]*p[n][axis] for n in range(4)) for axis in range(3)]
            error=max(error,math.dist(expected,coords[node_id]))
    if error>TOLERANCE:raise ValueError('FACE mesh is not the complete uniform bilinear lattice')
    return {**lattice,'maximum_coordinate_error_mm':error}


def _read_faces(base,mesh,constants,clean,seed):
    deck=clean['deck'];faces=[];used=set();by_id={r['cons_id']:r for r in seed['boundaries']}
    face_cons={r['face_id']:r['cons_ids'] for r in seed['geometry_witness']['face_boundaries']}
    for ident in clean['face_ids']:
        face,row,ids=meshing._face_snapshot(base,mesh,deck,ident)
        if row['macro_face_ids']!=[ident] or row['fields'].get('Frozen')!='NO' or not ids:
            raise ValueError('CAD Map requires six individually meshed, unfrozen FACE macros')
        # mesh_faces intentionally exposes a smaller field set; Num. Volumes
        # must be read from the explicit full FACE card, never defaulted to 0.
        if native._volume_count(seeds._card(base,deck,face)['fields'].get('Num. Volumes'))!=0:
            raise ValueError('selected FACE is already assigned to a VOLUME')
        if used.intersection(ids):raise ValueError('a SHELL belongs to multiple selected FACE macros')
        used.update(ids)
        if len(used)>400:raise ValueError('CAD Map boundary exceeds 400 SHELL budget')
        sides=four_sides(face_cons[ident],seed['boundaries'])
        for side in sides:
            points={r['id']:r['xyz'] for r in by_id[side['cons_id']]['hot_points']}
            side['start_xyz'],side['end_xyz']=points[side['start']],points[side['end']]
        faces.append({'face_id':ident,'snapshot':row,'shell_ids':ids,'sides':sides})
    boundary=native.capture_source(base,constants,deck,sorted(used))
    index=_SpatialIndex(TOLERANCE)
    for node in boundary['nodes']:index.add_unique(tuple(node['xyz']),node['id'],'CAD boundary nodes')
    for face in faces:
        subset=source_subset(boundary,face['shell_ids'])
        face['lattice']=_face_lattice(subset,face['sides'])
        if any(r['type']!='CQUAD4' for r in subset['elements']):raise ValueError('only CQUAD4 boundary meshes supported')
    return faces,boundary


def preflight(state,base,mesh,constants,params):
    state.assert_main();clean=normalize({k:params[k] for k in _PARAMS})
    deck=inspection._deck(base,clean);native._check_nastran(constants,deck)
    if not callable(getattr(mesh,'VolumesMap',None)) or not callable(getattr(base,'CalcElementMass',None)):
        raise NotImplementedError('native VolumesMap and CalcElementMass are required')
    context=guards._execution_context(state,deck)
    # The first contract accepts one unfilled domain; existing solids need overlap handling.
    if seeds._collect(base,deck,None,'SOLID') or seeds._collect(base,deck,None,'VOLUME'):
        raise ValueError('first CAD Map contract requires a model without existing SOLID/VOLUME')
    witness,_=features._witness(state,base,deck,{'face_ids':clean['face_ids'],'curve_ids':[]},{'scoped_export_calls':0})
    topology=plan_translation(witness,clean['source_face_id'],clean['target_face_id'],clean['direction'],clean['distance'],TOLERANCE)
    geometry=capture_geometry(state,base,deck,witness,topology)
    if geometry['status']!='verified_at_export_precision':
        raise ValueError('finite continuous CAD export domain is unsupported: '+str(geometry['faces']))
    source_parts={r['part_id'] for r in witness['parts']}
    output_part=features._entity(base,deck,'ANSAPART',clean['output_part_id'])
    if clean['output_part_id'] in source_parts or seeds._collect(base,deck,output_part,'__ALL_ENTITIES__'):
        raise ValueError('output_part_id must refer to a distinct existing empty Part')
    output_fields=seeds._card(base,deck,output_part)['fields']
    if output_fields.get('Is Empty')!='YES' or output_fields.get('Freeze')!='NO' or output_fields.get('Is Group')!='NO':
        raise ValueError('output Part must be empty, unfrozen and not a group')
    prop,property_evidence=native._psolid_preflight(base,deck,clean['property_id'])
    cons_ids=sorted(r['id'] for r in witness['boundaries'])
    seed=seeds.capture(state,base,deck,clean['face_ids'],cons_ids)
    if seed['geometry_witness']['source_snapshot_sha256']!=witness['source_snapshot_sha256']:
        raise ValueError('CAD changed during preflight')
    faces,boundary=_read_faces(base,mesh,constants,clean,seed)
    source=source_subset(boundary,next(r['shell_ids'] for r in faces if r['face_id']==clean['source_face_id']))
    if len(source['elements'])*clean['steps']>5000:raise ValueError('CAD Map exceeds 5000 predicted solids')
    if len(source['nodes'])*(clean['steps']+1)>10000:raise ValueError('CAD Map exceeds 10000 predicted nodes')
    segments={r['cons_id']:r['segments'] for r in seed['boundaries']}
    for side in topology['side_pairs']:
        if segments[side['source_cons_id']]!=segments[side['target_cons_id']]:raise ValueError('cap CONS counts differ')
        if any(segments[i]!=clean['steps'] for i in side['connector_cons_ids']):raise ValueError('longitudinal seed count differs from steps')
    target_ids=set(next(r['shell_ids'] for r in faces if r['face_id']==clean['target_face_id']))
    target_nodes={n for r in boundary['elements'] if r['id'] in target_ids for n in r['nodes']}
    target_index=_SpatialIndex(TOLERANCE)
    for n in boundary['nodes']:
        if n['id'] in target_nodes:target_index.add_unique(tuple(n['xyz']),n['id'],'target nodes')
    mapped=set()
    for n in source['nodes']:
        expected=tuple(n['xyz'][i]+clean['direction'][i]*clean['distance'] for i in range(3))
        matches=target_index.matches(expected)
        if len(matches)!=1:raise ValueError('source/target node grids do not translate one-to-one')
        mapped.add(matches[0][0])
    if mapped!=target_nodes:raise ValueError('target contains unmatched nodes')
    before=_snapshot(state,base,deck)
    predicted_grids=len(source['nodes'])*(clean['steps']+1)-len(boundary['nodes'])
    _reserve_budget(before,len(source['elements'])*clean['steps'],predicted_grids)
    settings=meshing._settings(base)
    guards._same_context(state,deck,context)
    fingerprint={'params':clean,'context':context,'geometry':geometry,'seed_sha256':seed['seed_snapshot_sha256'],
                 'scope_sha256':features._hash(before),'faces':faces,'source_sha256':source['snapshot_sha256'],
                 'property':property_evidence,'settings':settings}
    return {'clean':clean,'context':context,'witness':witness,'topology':topology,'geometry':geometry,'seed':seed,
            'faces':faces,'boundary':boundary,'source':source,'before':before,'settings':settings,
            'part':output_part,'property':prop,'property_evidence':property_evidence,
            'predicted_new_grid_count':predicted_grids,
            'plan_sha256':features._hash(fingerprint)}


def public_plan(prepared):
    clean=prepared['clean']
    return {'status':'ready_for_controlled_apply','plan_sha256':prepared['plan_sha256'],'params':clean,
            'source_snapshot_sha256':prepared['witness']['source_snapshot_sha256'],
            'seed_snapshot_sha256':prepared['seed']['seed_snapshot_sha256'],
            'geometry':prepared['geometry'],'topology':prepared['topology'],
            'face_meshes':prepared['faces'],'scope_sha256':features._hash(prepared['before']),
            'summary':{'source_quads':len(prepared['source']['elements']),
                       'predicted_solids':len(prepared['source']['elements'])*clean['steps'],
                       'predicted_nodes':len(prepared['source']['nodes'])*(clean['steps']+1),
                       'predicted_new_grid_count':prepared['predicted_new_grid_count'],
                       'continuous_export_domain_verified':True,'exact_native_CAD_equivalence_proven':False},
            'usable_for_mutation':False}


def validate_report(state,base,mesh,constants,deck,result):
    current=preflight(state,base,mesh,constants,result['params'])
    if current['plan_sha256']!=result.get('plan_sha256'):raise ValueError('CAD Map plan evidence is stale')


def _close_number(actual,expected,label):
    actual=features._number(actual,label)
    if not math.isclose(actual,expected,rel_tol=1e-9,abs_tol=1e-12):raise RuntimeError(label+' differs from independent readback')


def _part_derived(base,deck,part,output,prop_evidence):
    """Recompute the permitted output-Part summaries from its explicit new solids."""
    fields=seeds._card(base,deck,part)['fields']
    solids=[features._entity(base,deck,'SOLID',r['id']) for r in output['elements']]
    mass=base.CalcElementMass(solids,deck=deck)
    if type(mass) not in (list,tuple) or len(mass)!=22:raise RuntimeError('explicit SOLID mass readback unavailable')
    computed={name:features._number(v,'mass property') for name,v in zip(
        ('Mass','COG x','COG y','COG z','Ixx','Iyy','Izz','Ixy','Ixz','Iyz'),mass[:10])}
    points=[n['xyz'] for n in output['nodes']]
    lower=[min(p[i] for p in points) for i in range(3)];upper=[max(p[i] for p in points) for i in range(3)]
    sizes=[upper[i]-lower[i] for i in range(3)]
    computed.update({**dict(zip(('Min x','Min y','Min z'),lower)),**dict(zip(('Max x','Max y','Max z'),upper)),
                     **dict(zip(('Size x','Size y','Size z'),sizes)),'Size min':min(sizes),'Size max':max(sizes),
                     'Diagonal':math.hypot(*sizes),'Area':0.})
    for name,value in computed.items():_close_number(fields.get(name),value,'output Part '+name)
    prop,mat=prop_evidence['psolid']['fields'],prop_evidence['mat1']['fields']
    pid,mid=prop['PID'],prop_evidence['material_id']
    expected={'PID':str(pid),'PIDs Number':1,'Thickness':'-','Material Name':mat['Name'],
              'Is Empty':'NO','Features Status':'Not Recognized'}
    for key,value in expected.items():
        if fields.get(key)!=value:raise RuntimeError('output Part '+key+' does not match new SOLID membership')
    props=[{'Id':str(pid),'Name':prop['Name'],'Type':'PSOLID'}]
    materials=[{str(pid):[{'Id':str(mid),'Name':mat['Name'],'Type':'MAT1'}]}]
    if json.loads(fields.get('Properties Info',''))!=props or json.loads(fields.get('Materials Info',''))!=materials:
        raise RuntimeError('output Part property/material summary differs from protected cards')
    stamp=fields.get('Last Edit')
    if type(stamp) is not str or not stamp.strip() or len(stamp)>128:raise RuntimeError('output Part edit stamp unreadable')
    return set(computed)|set(expected)|{'Properties Info','Materials Info','Last Edit'},computed,mass


def readback(state,base,mesh,constants,prepared,returned):
    clean=prepared['clean'];deck=clean['deck'];guards._same_context(state,deck,prepared['context'])
    after=_snapshot(state,base,deck);old,new=native._scope_map(prepared['before']),native._scope_map(after)
    if old.keys()-new.keys():raise RuntimeError('Map removed original entities')
    added=new.keys()-old.keys()
    if any(k not in ('GRID','SOLID','VOLUME') for k,_ in added):raise RuntimeError('Map created unexpected entity types')
    solid_ids=sorted(i for k,i in added if k=='SOLID');grid_ids=sorted(i for k,i in added if k=='GRID')
    volume_ids=sorted(i for k,i in added if k=='VOLUME')
    if len(volume_ids)!=1 or type(returned) not in (list,tuple) or len(returned)!=1:
        raise RuntimeError('VolumesMap did not return exactly one new VOLUME')
    if returned[0].ansa_type(deck)!='VOLUME' or returned[0]._id!=volume_ids[0]:raise RuntimeError('returned VOLUME differs from independently observed addition')
    output=native.capture_mesh(base,constants,deck,solid_ids)
    verification=verify_translation(prepared['source'],output,clean['direction'],clean['distance'],clean['steps'],TOLERANCE)
    _close_number(verification['total_volume'],prepared['topology']['expected_volume'],'total swept volume')
    if any(r['property_id']!=clean['property_id'] for r in output['elements']):raise RuntimeError('new SOLID property mismatch')
    if any(new[('SOLID',i)].get('part_id')!=clean['output_part_id'] for i in solid_ids):raise RuntimeError('new SOLID output Part mismatch')
    if not set(grid_ids)<={r['id'] for r in output['nodes']}:raise RuntimeError('new GRID is outside generated SOLID mesh')
    from .mapped_surface import _check_new_grids
    _check_new_grids(base,deck,grid_ids)
    allowed,derived,mass=_part_derived(base,deck,prepared['part'],output,prepared['property_evidence'])
    for key,left in old.items():
        a,b=copy.deepcopy(left),copy.deepcopy(new[key])
        if key==('ANSAPART',clean['output_part_id']):
            for name in allowed:a['fields'].pop(name,None);b['fields'].pop(name,None)
        if key[0]=='FACE' and key[1] in clean['face_ids']:
            before_count=native._volume_count(a['fields'].pop('Num. Volumes'))
            after_count=native._volume_count(b['fields'].pop('Num. Volumes'))
            if before_count!=0 or after_count!=1:raise RuntimeError('CAD FACE volume membership count mismatch')
        if key==('PSOLID',clean['property_id']):
            for name in left['card_fields']:
                if guards._field_key(name)=='numelem':
                    if native._volume_count(a['fields'].pop(name))!=0 or native._volume_count(b['fields'].pop(name))!=len(solid_ids):
                        raise RuntimeError('target PSOLID element count differs from complete new SOLID inventory')
        if a!=b:raise RuntimeError('Map changed a protected original entity: '+str(key))
    old_grids={r['id']:r for r in prepared['before']['grids']};new_grids={r['id']:r for r in after['grids']}
    if any(row!=new_grids.get(i) for i,row in old_grids.items()):raise RuntimeError('Map changed existing GRID cards')
    if prepared['before']['cad_sha256']!=after['cad_sha256']:raise RuntimeError('Map changed original CAD export')
    from . import structured_boundary
    from ansa import calc
    cad=structured_boundary.capture(base,mesh,calc,constants,deck,output,{'face_ids':clean['face_ids'],'tolerance':TOLERANCE,'cylinders':[]})
    if cad['membership']['status']!='pass' or cad['projection']['status']!='pass':raise RuntimeError('CAD boundary attribution/projection failed')
    for face in prepared['faces']:
        _,row,ids=meshing._face_snapshot(base,mesh,deck,face['face_id'])
        if ids!=face['shell_ids'] or row['macro_face_ids']!=[face['face_id']]:raise RuntimeError('Map changed native FACE macro SHELL membership')
    volume=new[('VOLUME',volume_ids[0])];v=features._entity(base,deck,'VOLUME',volume_ids[0]);f=volume['fields']
    if volume.get('part_id')!=clean['output_part_id'] or volume['solid_ids']!=solid_ids:raise RuntimeError('VOLUME membership mismatch')
    if sorted(e._id for e in base.CollectEntities(deck,v,'FACE'))!=clean['face_ids']:raise RuntimeError('new VOLUME boundary differs from original six FACEs')
    expected={'PID':clean['property_id'],'Type':'Map','Order':'1st','Status':'Meshed','Frozen':'NO','Method':'Normal Parts',
              'Map steps':clean['steps'],'Hexas':len(solid_ids),'Pentas':0,'Tetras':0,'Pyramids':0,'Polyhedrals':0,
              'Grids':len(output['nodes']),'Internal grids':len(grid_ids),'Respect target':'YES'}
    for name,value in expected.items():
        if f.get(name)!=value:raise RuntimeError('new VOLUME '+name+' differs from verified mesh')
    _close_number(f.get('Volume'),verification['total_volume'],'VOLUME volume');_close_number(f.get('Mass'),mass[0],'VOLUME mass')
    if meshing._settings(base)!=prepared['settings']:raise RuntimeError('Map changed protected meshing settings')
    guards._same_context(state,deck,prepared['context'])
    return {'new_solid_ids':solid_ids,'new_grid_ids':grid_ids,'new_volume_ids':volume_ids,
            'verification':verification,'volume_fields':f,'output_part_derived':derived,
            'cad_boundary':{k:v for k,v in cad.items() if k!='audit_snapshot'},'after_scope_sha256':features._hash(after)}


def apply_prepared(state,base,mesh,constants,params,prepared):
    clean=normalize(params,True);result={'ok':False,'scope_verified':False,'api_called':False,'possible_effects':False,
        'new_solid_ids':[],'new_grid_ids':[],'new_volume_ids':[],'plan_sha256':prepared['plan_sha256']}
    try:
        if clean['expected_plan_sha256']!=prepared['plan_sha256']:raise ValueError('CAD Map plan changed')
        backup_args=native._backup_preflight(base,clean)
        result['possible_effects']=True
        result['backup']=native._verify_backup(base,constants,*backup_args)
        refreshed=preflight(state,base,mesh,constants,prepared['clean'])
        if refreshed['plan_sha256']!=prepared['plan_sha256']:raise RuntimeError('model changed during backup; native Map not called')
        prepared=refreshed
        guards._same_context(state,clean['deck'],prepared['context'])
        faces={i:features._entity(base,clean['deck'],'FACE',i) for i in clean['face_ids']}
        result['api_called']=True
        returned=mesh.VolumesMap([faces[clean['source_face_id']]],[faces[clean['target_face_id']]],
            [faces[i] for i in prepared['topology']['side_face_ids']],'Normal parts',clean['steps'],
            prepared['part'],prepared['property'],True,False)
        result.update(readback(state,base,mesh,constants,prepared,returned))
        result.update(ok=True,scope_verified=True,possible_effects=False,continuous_export_domain_verified=True,
                      exact_native_CAD_equivalence_proven=False,project_quality_acceptance='not_evaluated')
    except Exception as exc:
        result.update(error=type(exc).__name__+': '+str(exc),recovery_required=result['possible_effects'])
        try:
            after=_snapshot(state,base,clean['deck']);added=native._scope_map(after).keys()-native._scope_map(prepared['before']).keys()
            result.update(new_solid_ids=sorted(i for k,i in added if k=='SOLID'),
                          new_grid_ids=sorted(i for k,i in added if k=='GRID'),new_volume_ids=sorted(i for k,i in added if k=='VOLUME'))
        except Exception as read_error:result['observation_error']=str(read_error)
    return result
