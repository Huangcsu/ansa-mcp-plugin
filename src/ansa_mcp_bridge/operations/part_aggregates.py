"""Native Part aggregate consistency and narrowly bounded persistence checks.

CalcElementMass(Part) measures ANSA's mixed CAD/FE/volume aggregate. It is not
an independent analytical FE mass calculation. No definition field is ignored.
"""
import copy
import math

from . import geometry_features as features, structured_native as native

MASS_FIELDS = ('Mass', 'COG x', 'COG y', 'COG z', 'Ixx', 'Iyy', 'Izz', 'Ixy', 'Ixz', 'Iyz')


def _scales(fields):
    mass = features._number(fields.get('Mass'), 'Part mass', True)
    diagonal = features._number(fields.get('Diagonal'), 'Part diagonal', True)
    moments = [abs(features._number(fields.get(k), 'Part '+k)) for k in ('Ixx', 'Iyy', 'Izz')]
    inertia = max(moments)
    if inertia == 0:
        raise ValueError('nonzero native Part inertia is required')
    return [mass]+[diagonal]*3+[inertia]*6


def capture(base, deck, part, row):
    """Read the documented native Part calculation and verify ten raw card fields."""
    values = base.CalcElementMass(part, no_nsm=False, deck=deck)
    if type(values) not in (list, tuple) or len(values) != 22:
        raise RuntimeError('native Part mass readback requires 22 finite values')
    values = [features._number(v, 'native Part mass value') for v in values]
    fields = row['fields']; scales = _scales(fields)
    for key, expected, scale in zip(MASS_FIELDS, values[:10], scales):
        actual = features._number(fields.get(key), 'Part '+key)
        if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=scale*1e-14):
            raise RuntimeError('Part '+key+' differs from native CalcElementMass(Part)')
    return {'part_id': part._id, 'values': dict(zip(MASS_FIELDS, values[:10])),
            'native_values': values, 'origin': 'CalcElementMass(Part)',
            'meaning': 'native mixed CAD/FE/volume aggregate consistency'}


def compare_persistence(before, after, before_aggregates, after_aggregates):
    """Compare complete snapshots; only ten recomputed Part fields get tolerance.

    Callers must capture both sides in ANSA, including their raw full snapshots.
    Missing or changed material, geometry, grids, membership, counts and every
    other card field fail exact comparison. Keep raw deltas in returned evidence.
    """
    a, b = copy.deepcopy(before), copy.deepcopy(after)
    left, right = native._scope_map(a), native._scope_map(b)
    if left.keys() != right.keys():
        raise RuntimeError('persistence changed entity identities')
    part_ids = {i for kind, i in left if kind == 'ANSAPART'}
    if set(before_aggregates) != part_ids or set(after_aggregates) != part_ids:
        raise ValueError('persistence requires both native aggregate readbacks for every Part')
    changes = []
    for ident in sorted(part_ids):
        l, r = left[('ANSAPART', ident)]['fields'], right[('ANSAPART', ident)]['fields']
        for data, fields in ((before_aggregates[ident], l), (after_aggregates[ident], r)):
            if data.get('part_id') != ident or data.get('origin') != 'CalcElementMass(Part)':
                raise ValueError('native Part aggregate evidence binding mismatch')
            if set(data.get('values', {})) != set(MASS_FIELDS):
                raise ValueError('incomplete native Part aggregate evidence')
            for key, scale in zip(MASS_FIELDS, _scales(fields)):
                observed = features._number(data['values'][key], 'Part native value')
                if not math.isclose(features._number(fields.get(key), 'Part '+key), observed,
                                    rel_tol=1e-12, abs_tol=scale*1e-14):
                    raise RuntimeError('raw Part card not consistent with native readback')
        for key, scale in zip(MASS_FIELDS, _scales(l)):
            x, y = l[key], r[key]
            if not math.isclose(x, y, rel_tol=1e-12, abs_tol=scale*1e-14):
                raise RuntimeError('persistence changed Part '+key+' beyond the numerical bound')
            if x != y:
                changes.append({'part_id': ident, 'field': key, 'before': x, 'after': y,
                                'delta': y-x, 'absolute_tolerance': scale*1e-14})
            l[key] = r[key] = 0.0
    if a != b:
        raise RuntimeError('persistence changed protected snapshot data')
    return {'status': 'pass', 'raw_snapshot_exact': before == after,
            'derived_roundoff': changes, 'relative_tolerance': 1e-12,
            'normalized_fields': list(MASS_FIELDS),
            'underlying_definition_and_geometry_exact': True}
