import copy
from types import SimpleNamespace
import unittest

from ansa_mcp_bridge.operations import part_aggregates as pa


def snapshot():
    fields = dict(zip(pa.MASS_FIELDS, [2., 0., 0., 0., 12., 15., 17., 0., 0., 0.]))
    fields.update(Diagonal=10., Name='protected', **{'Last Edit': 'stamp'})
    return {'entities': [{'entity': {'type': 'ANSAPART', 'id': 1}, 'fields': fields},
                         {'entity': {'type': 'MAT1', 'id': 1}, 'fields': {'E': 123456.}}],
            'grids': [{'id': 1, 'xyz': [0., 0., 0.]}], 'cad_sha256': 'a'*64}


def aggregate(s):
    return {1: {'part_id': 1, 'origin': 'CalcElementMass(Part)',
                'values': {k: s['entities'][0]['fields'][k] for k in pa.MASS_FIELDS}}}


class PartAggregateTests(unittest.TestCase):
    def test_only_recomputed_part_roundoff_is_tolerated(self):
        a = snapshot(); b = copy.deepcopy(a); b['entities'][0]['fields']['Ixy'] = 1e-15
        r = pa.compare_persistence(a, b, aggregate(a), aggregate(b))
        self.assertEqual(r['status'], 'pass'); self.assertFalse(r['raw_snapshot_exact'])
        self.assertEqual(len(r['derived_roundoff']), 1)
        self.assertEqual(a['entities'][0]['fields']['Ixy'], 0.)

    def test_definitions_geometry_and_unrelated_part_fields_remain_exact(self):
        a = snapshot()
        for change in ('material', 'cad', 'node', 'name', 'edit', 'count'):
            b = copy.deepcopy(a)
            if change == 'material': b['entities'][1]['fields']['E'] += 1
            elif change == 'cad': b['cad_sha256'] = 'b'*64
            elif change == 'node': b['grids'][0]['xyz'][0] = 1e-16
            elif change == 'name': b['entities'][0]['fields']['Name'] = 'changed'
            elif change == 'edit': b['entities'][0]['fields']['Last Edit'] = 'changed'
            else: b['entities'][0]['fields']['Num.Elem'] = 999
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                pa.compare_persistence(a, b, aggregate(a), aggregate(b))

    def test_material_aggregate_change_and_missing_native_evidence_fail(self):
        a = snapshot(); b = copy.deepcopy(a); b['entities'][0]['fields']['Mass'] *= 1.01
        with self.assertRaises(RuntimeError): pa.compare_persistence(a, b, aggregate(a), aggregate(b))
        with self.assertRaises(ValueError): pa.compare_persistence(a, a, {}, aggregate(a))
        wrong = aggregate(a); wrong[1]['values']['Mass'] *= 1.01
        with self.assertRaises(RuntimeError): pa.compare_persistence(a, a, aggregate(a), wrong)

    def test_native_nan_empty_or_inconsistent_calculation_is_never_zero(self):
        row = snapshot()['entities'][0]; part = SimpleNamespace(_id=1)
        valid = [row['fields'][k] for k in pa.MASS_FIELDS]+[1.]*12
        for values in ([], [0.]*22, [float('nan')]*22, valid[:3]+[float('inf')]+valid[4:]):
            with self.subTest(values=values[:2]), self.assertRaises((RuntimeError, ValueError)):
                pa.capture(SimpleNamespace(CalcElementMass=lambda *a, **k: values), 1, part, row)
        self.assertEqual(pa.capture(SimpleNamespace(CalcElementMass=lambda *a, **k: valid), 1, part, row)['values']['Mass'], 2.)


if __name__ == '__main__': unittest.main()
