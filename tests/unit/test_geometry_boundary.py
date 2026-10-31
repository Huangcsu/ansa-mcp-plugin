"""Risk-based coverage for backed-up auxiliary curve conversion."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import geometry_boundary as boundary


class Entity:
    def __init__(self, kind, ident, owner=None):
        self.kind, self._id, self.owner = kind, ident, owner

    def ansa_type(self, deck):
        return self.kind


class GeometryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(assert_main=Mock())
        self.part, self.aux = Entity("ANSAPART", 1), Entity("ANSAPART", 2)
        self.entities = {("ANSAPART", 1):self.part, ("ANSAPART", 2):self.aux,
                         ("CONS", 10):Entity("CONS", 10), ("FACE", 1):Entity("FACE", 1, self.part),
                         ("CURVE", 99):Entity("CURVE", 99, self.part)}
        self.aux_fields = {**dict.fromkeys(boundary._BOUNDS_FIELDS, 0.), "Is Empty":"YES", "Name":"aux"}
        self.grid_x = 0.
        self.calls = 0
        self.conversion_behavior = None
        self.base = SimpleNamespace(GetEntity=lambda d,k,i:self.entities.get((k,i)),
            GetEntityPart=lambda e:e.owner, GetCurrentEntity=lambda d,k:self.part,
            CollectEntities=self.collect, ConsToCurves=Mock(side_effect=self.convert),
            SetEntityPart=Mock(side_effect=self.assign), BoundBox=lambda e:[0.,0.,0.,4.,0.,0.],
            GetCurveLength=lambda e:4., EvaluateCurvePoint=lambda e,u:[4.*u,0.,0.])
        fake_ansa=patch.dict(sys.modules,{'ansa':SimpleNamespace(calc=SimpleNamespace(
            ProjectPointToCons=lambda p,e:(tuple(p),)))})
        fake_ansa.start();self.addCleanup(fake_ansa.stop)
        self.witness = {"source_scope":{"face_ids":[1],"curve_ids":[]},
            "source_snapshot_sha256":"a"*64,"parts":[{"part_id":1}],
            "boundaries":[{"id":10,"endpoint_topology":{"status":"available","hot_point_ids":[1,2]}}],
            "vertices":[{"id":1,"position":[0.,0.,0.]},{"id":2,"position":[4.,0.,0.]}]}
        self.params = {"deck":1,"face_ids":[1],"cons_ids":[10],"auxiliary_part_id":2,
            "expected_source_snapshot_sha256":"a"*64,"backup_path":"backup.ansa"}
        self.add_patch(boundary.native, "_capture_scope", side_effect=lambda b,d:self.snapshot())
        self.add_patch(boundary.features, "_witness", side_effect=lambda *a:(copy.deepcopy(self.witness),{}))
        self.backup = self.add_patch(boundary.native, "_verify_backup", return_value={"verified":True})
        self.add_patch(boundary.native, "_backup_preflight", return_value=(Path("backup.ansa"),"source.ansa",{}))
        self.add_patch(boundary.native, "_unconfirmed_backup", return_value={"verified":False})
        self.add_patch(boundary.volume_meshing, "_execution_context", return_value={"epoch":1})
        self.context = self.add_patch(boundary.volume_meshing, "_same_context")
        self.add_patch(boundary.features, "_export", return_value=b"scoped curve")
        from ansa_mcp_bridge.operations import geometry_features_iges as iges
        self.analysis = self.add_patch(iges, "analytic_curve", return_value={"status":"supported", "kind":"line",
            "start":[0.,0.,0.],"end":[4.,0.,0.],"length":4.})
        self.add_patch(iges, "canonical_digest", return_value="c"*64)

    def add_patch(self, owner, name, **kwargs):
        p=patch.object(owner,name,**kwargs);self.addCleanup(p.stop);return p.start()

    def collect(self, deck, container, kind, recursive=False):
        if container is not None:
            return [e for e in self.entities.values() if e.owner is container]
        return [e for e in self.entities.values() if e.kind==kind]

    def snapshot(self):
        rows=[]
        for (kind,ident),entity in self.entities.items():
            fields=self.aux_fields.copy() if entity is self.aux else {"Name":kind+str(ident)}
            rows.append({"entity":{"type":kind,"id":ident},"fields":fields})
        rows.append({"entity":{"type":"GRID","id":50},"fields":{"X1":self.grid_x}})
        return {"entities":copy.deepcopy(rows)}

    def convert(self, edges):
        self.calls+=1
        curve=Entity("CURVE",99+self.calls,self.part)
        self.entities["CURVE",curve._id]=curve
        if self.conversion_behavior:
            return self.conversion_behavior(curve)
        return [curve]

    def assign(self, curves, part):
        for curve in curves:curve.owner=part
        self.aux_fields.update(dict(zip(boundary._BOUNDS_FIELDS,[0.,0.,0.,4.,0.,0.,4.,0.,0.,0.,4.,4.])))
        self.aux_fields["Is Empty"]="NO"
        return 1

    def run_operation(self):
        prepared=boundary.preflight(self.state,self.base,object(),self.params)
        return boundary.apply_prepared(self.state,self.base,object(),self.params,prepared)

    def test_one_to_one_scope_and_endpoints_verified_without_claiming_cons_equivalence(self):
        result=self.run_operation()
        self.assertTrue(result["ok"])
        self.assertTrue(result["scope_verified"])
        self.assertEqual(result["new_curve_ids"],[100])
        row=result["mappings"][0]
        self.assertEqual(row["provenance_status"],"verified")
        self.assertEqual(row["endpoint_check"]["status"],"matched")
        self.assertEqual(row["source_geometry_equivalence"],"not_proven")
        self.assertEqual(row['conversion_fidelity']['status'],'sampled_only')
        self.assertEqual(row['conversion_fidelity']['maximum_observed_distance'],0.)
        self.assertFalse(row['conversion_fidelity']['continuous_equivalence_proven'])
        self.assertEqual(result["geometry_status"],"incomplete")
        self.assertFalse(result["usable_for_mutation"])
        self.backup.assert_called_once()

    def test_unsupported_bspline_is_retained_without_analytic_seed_claim(self):
        self.analysis.return_value={"status":"unsupported","reason":"type 126"}
        result=self.run_operation()
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["supported_converted_curves"],0)
        self.assertEqual(result["mappings"][0]["analysis"]["status"],"unsupported")
        self.assertNotIn("seed_constraints",result)

    def test_unavailable_projection_is_retained_as_incomplete_without_geometry_authority(self):
        with patch.object(sys.modules['ansa'].calc,'ProjectPointToCons',return_value=0):
            result=self.run_operation()
        self.assertTrue(result['scope_verified'])
        self.assertEqual(result['mappings'][0]['conversion_fidelity']['status'],'incomplete')
        self.assertEqual(result['mappings'][0]['source_geometry_equivalence'],'not_proven')
        self.assertFalse(result['usable_for_mutation'])

    def test_bad_hash_nonempty_part_and_outside_cons_reject_before_backup_or_conversion(self):
        self.params["expected_source_snapshot_sha256"]="b"*64
        with self.assertRaisesRegex(ValueError,"source changed"):self.run_operation()
        self.params["expected_source_snapshot_sha256"]="a"*64
        self.params["cons_ids"]=[11]
        with self.assertRaisesRegex(ValueError,"FACE boundary"):self.run_operation()
        self.params["cons_ids"]=[10]
        self.entities["CURVE",99].owner=self.aux
        with self.assertRaisesRegex(ValueError,"must be empty"):self.run_operation()
        self.base.ConsToCurves.assert_not_called();self.backup.assert_not_called()

    def test_failed_backup_prevents_conversion_and_requires_recovery_readback(self):
        self.backup.side_effect=RuntimeError("backup failed")
        result=self.run_operation()
        self.assertFalse(result["ok"])
        self.assertFalse(result["api_called"])
        self.assertTrue(result["recovery_required"])
        self.base.ConsToCurves.assert_not_called()

    def test_effective_call_returning_none_stops_and_preserves_observed_partial_mapping(self):
        self.conversion_behavior=lambda curve:None
        result=self.run_operation()
        self.assertFalse(result["ok"])
        self.assertEqual(result["new_curve_ids"],[100])
        self.assertEqual(result["mappings"][0]["created_curve_ids"],[100])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["original_operation_must_be_queried"])
        self.base.ConsToCurves.assert_called_once()

    def test_failed_curve_collection_does_not_infer_empty_baseline_or_convert(self):
        original_collect=self.base.CollectEntities
        self.base.CollectEntities=lambda d,c,k,**kw:None if k=='CURVE' and c is None else original_collect(d,c,k,**kw)
        result=self.run_operation()
        self.assertFalse(result['ok']);self.assertFalse(result['api_called'])
        self.assertEqual(result['new_curve_ids'],[])
        self.base.ConsToCurves.assert_not_called();self.base.SetEntityPart.assert_not_called()

    def test_context_change_during_conversion_prevents_followup_assignment(self):
        def effective(curve):
            self.context.side_effect=RuntimeError('model context changed')
            return [curve]
        self.conversion_behavior=effective
        result=self.run_operation()
        self.assertFalse(result['ok']);self.assertTrue(result['recovery_required'])
        self.assertEqual(result['new_curve_ids'],[100])
        self.base.SetEntityPart.assert_not_called()

    def test_unselected_geometry_or_part_definition_change_fails_scope_verification(self):
        def effective(curve):
            self.grid_x=1.
            self.aux_fields["Name"]="unexpectedly renamed"
            return [curve]
        self.conversion_behavior=effective
        result=self.run_operation()
        self.assertFalse(result["scope_verified"])
        self.assertTrue(any("GRID" in e for e in result["errors"]))
        self.assertTrue(any("ANSAPART" in e for e in result["errors"]))

    def test_unverified_auxiliary_aggregate_and_original_curve_reassignment_are_not_ignored(self):
        original_assign=self.base.SetEntityPart.side_effect
        def bad_assign(curves,part):
            rc=original_assign(curves,part)
            self.aux_fields["Diagonal"]=100.
            self.entities["CURVE",99].owner=None
            return rc
        self.base.SetEntityPart.side_effect=bad_assign
        result=self.run_operation()
        self.assertFalse(result["scope_verified"])
        self.assertTrue(any("derived bound" in e for e in result["errors"]))

    def test_original_curve_part_change_is_detected_with_valid_auxiliary_bounds(self):
        original_assign=self.base.SetEntityPart.side_effect
        def bad_assign(curves,part):
            rc=original_assign(curves,part)
            self.entities["CURVE",99].owner=None
            return rc
        self.base.SetEntityPart.side_effect=bad_assign
        result=self.run_operation()
        self.assertFalse(result["scope_verified"])
        self.assertTrue(any("CURVE" in e and "99" in e for e in result["errors"]))


if __name__=="__main__":unittest.main()
