"""Narrow Part display aggregate validation for CAD-only internal-plane cuts."""
import math

from . import geometry_features as features, part_aggregates, structured_native as native

FIELDS = ('Mass', 'COG x', 'COG y', 'COG z', 'Area')


def capture(base, deck, snapshot, part_id):
    rows = native._scope_map(snapshot); part_row = rows[('ANSAPART', part_id)]
    if any(kind in ('SHELL','SOLID','VOLUME') and row.get('part_id') == part_id for (kind,_),row in rows.items()):
        raise ValueError('internal-plane aggregate contract requires a CAD-only source Part')
    faces = [row for (kind,_),row in rows.items() if kind == 'FACE' and row['assignments']['__part__'] == part_id]
    if not faces or len(faces)>500:
        raise ValueError('CAD-only Part aggregate requires 1..500 complete FACE members')
    part = features._entity(base, deck, 'ANSAPART', part_id)
    elements = base.CollectEntities(deck, part, '__ELEMENTS__')
    if type(elements) not in (list,tuple) or elements:
        raise ValueError('CAD-only source Part requires an explicitly empty native __ELEMENTS__ collection')
    full = native._card(base, deck, part, part_row['entity'], 'partition Part aggregate')
    aggregate = part_aggregates.capture(base, deck, part, full)
    masses = []; centers = []; areas = []
    for row in faces:
        face = features._entity(base, deck, 'FACE', row['entity']['id'])
        values = base.CalcElementMass(face, no_nsm=False, deck=deck)
        if type(values) not in (list,tuple) or len(values)!=22:
            raise RuntimeError('native FACE aggregate requires 22 finite values')
        values = [native._finite(v,'FACE aggregate') for v in values]
        if values[0]<0:raise ValueError('FACE aggregate mass must be nonnegative')
        masses.append(values[0]); centers.append(values[1:4])
        area = native._finite(base.GetFaceArea(face),'native FACE area')
        if area<=0 or not math.isclose(area,row['geometry']['area'],rel_tol=1e-12,abs_tol=1e-12):
            raise RuntimeError('FACE area differs from complete scope capture')
        areas.append(area)
    mass=math.fsum(masses); area=math.fsum(areas)
    if mass<=0:raise ValueError('internal-plane Part aggregate requires positive readable CAD mass')
    values={'Mass':mass,'Area':area,**{'COG '+a:math.fsum(m*p[k] for m,p in zip(masses,centers))/mass for k,a in enumerate('xyz')}}
    diagonal=native._finite(full['fields'].get('Diagonal'),'Part diagonal')
    for key,value in values.items():
        scale=mass if key=='Mass' else area if key=='Area' else diagonal
        for label,fields in (('scope',part_row['fields']),('raw',full['fields'])):
            if not math.isclose(native._finite(fields.get(key),'Part '+key),value,rel_tol=1e-12,abs_tol=abs(scale)*1e-14):
                raise RuntimeError(label+' Part '+key+' differs from independent complete FACE aggregation')
    return {'part_id':part_id,'part_scope_sha256':native._hash(part_row), 'values':values,
            'native_part':aggregate,'face_ids':sorted(r['entity']['id'] for r in faces),
            'basis':'complete native FACE areas and mass-weighted centers; CalcElementMass(Part) cross-check',
            'definition_fields_ignored':False}


def validated_change(before_row, after_row, evidence):
    if type(evidence) is not dict:return False
    for side,row in (('before',before_row),('after',after_row)):
        item=evidence.get(side,{})
        if item.get('part_id')!=row['entity']['id'] or item.get('part_scope_sha256')!=native._hash(row):return False
        values=item.get('values')
        if type(values) is not dict or set(values)!=set(FIELDS):return False
        for name in FIELDS:
            if type(values[name]) not in (int,float) or not math.isfinite(values[name]):return False
    left={**before_row,'fields':{k:v for k,v in before_row['fields'].items() if k not in FIELDS}}
    right={**after_row,'fields':{k:v for k,v in after_row['fields'].items() if k not in FIELDS}}
    return left==right
