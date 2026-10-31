"""共享源面合批、预算和单次回读的风险验证。"""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys

from ansa_mcp_bridge.operations import structured_native as native,structured_batch as batch
from test_structured_native import FakeBase


def fixture():
    base=FakeBase()
    for n,p in enumerate([(0,0,0),(1,0,0),(1,1,0),(0,1,0),(2,0,0)],1):
        base.add("GRID",n,NID=n,CP=0,X1=float(p[0]),X2=float(p[1]),X3=float(p[2]))
    base.add("SHELL",10,EID=10,PID=8,type="CQUAD4",G1=1,G2=2,G3=3,G4=4)
    base.add("SHELL",20,EID=20,PID=8,type="CTRIA3",G1=2,G2=5,G3=3)
    blocks=[{"id":name,"shell_ids":[eid],"direction":[0.,0.,1.],"distance":2.,"steps":2,"property_id":12}
            for name,eid in (("quad",10),("tri",20))]
    return base,SimpleNamespace(NASTRAN=1),blocks


class BatchTests(unittest.TestCase):
    def test_shared_edge_coalesces_with_one_capture_and_fresh_hash_compatible_with_write(self):
        base,constants,blocks=fixture()
        with patch.object(native,"capture_source",wraps=native.capture_source) as capture, \
                patch.object(native,"_psolid_preflight") as prop:
            result=batch.prepare(base,constants,1,blocks)
        self.assertEqual(capture.call_count,1);self.assertEqual(prop.call_count,1)
        self.assertEqual(result["status"],"ready_for_existing_sweep")
        self.assertEqual(len(result["groups"]),1)
        self.assertEqual(result["groups"][0]["estimated_solids"],{"CHEXA":2,"CPENTA":2})
        actual=native.capture_source(base,constants,1,[10,20])
        self.assertEqual(result["groups"][0]["sweep_params"]["expected_source_sha256"],actual["snapshot_sha256"])
        self.assertFalse(result["target_geometry_verified"])

    def test_shared_GRID_with_different_execution_parameters_blocks_all_execution(self):
        for key,value in (("steps",3),("distance",3.),("property_id",13),("direction",[0.,0.,-1.])):
            with self.subTest(key=key):
                base,constants,blocks=fixture();blocks[1][key]=value
                with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
                self.assertEqual(result["status"],"blocked")
                self.assertIn("shared_GRID_has_incompatible_sweep_parameters",[r["code"] for r in result["issues"]])
                self.assertTrue(all("sweep_params" not in g for g in result["groups"]))

    def test_disjoint_planes_are_separate_and_source_slice_matches_direct_capture(self):
        base,constants,blocks=fixture()
        for n,p in enumerate([(3,0,1),(4,0,1),(3,1,1)],6):
            base.add("GRID",n,NID=n,CP=0,X1=float(p[0]),X2=float(p[1]),X3=float(p[2]))
        base.entities[("SHELL",20)].fields.update(G1=6,G2=7,G3=8)
        full=native.capture_source(base,constants,1,[10,20])
        self.assertEqual(batch.source_subset(full,[20]),native.capture_source(base,constants,1,[20]))
        with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
        self.assertEqual(result["status"],"ready_for_existing_sweep");self.assertEqual(len(result["groups"]),2)

    def test_coincident_nodes_duplicate_facets_and_duplicate_ownership_cannot_be_planned(self):
        base,constants,blocks=fixture()
        base.add("GRID",6,NID=6,CP=0,X1=1.,X2=0.,X3=0.)
        base.entities[("SHELL",20)].fields["G1"]=6
        with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
        self.assertIn("coincident_source_GRIDs_have_distinct_IDs",[r["code"] for r in result["issues"]])
        blocks[1]["shell_ids"]=[10]
        with self.assertRaisesRegex(ValueError,"multiple blocks"):batch.prepare(base,constants,1,blocks)
        base,constants,blocks=fixture()
        base.entities[("SHELL",20)].fields.update(type="CQUAD4",G1=1,G2=2,G3=3,G4=4)
        with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
        self.assertIn("duplicate_source_facets",[r["code"] for r in result["issues"]])

    def test_penta_limit_and_shared_component_over_budget_never_return_apply_parameters(self):
        base,constants,blocks=fixture()
        with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks,max_penta_fraction=.1)
        self.assertEqual(result["status"],"blocked")
        # Budget is a write-path constraint; merging cannot bypass it.
        with patch.object(native,"_psolid_preflight"),patch.object(native,"_MAX_NEW_SOLIDS",3):
            result=batch.prepare(base,constants,1,blocks)
        self.assertEqual(result["status"],"blocked")
        self.assertTrue(all("sweep_params" not in g for g in result["groups"]))

    def test_failed_shared_property_is_read_once_and_blocks_all_groups(self):
        base,constants,blocks=fixture()
        with patch.object(native,"_psolid_preflight",side_effect=ValueError("invalid PSOLID")) as prop:
            result=batch.prepare(base,constants,1,blocks)
        self.assertEqual(prop.call_count,1);self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["summary"]["property_preflight_calls"],1)

    def test_bad_declared_inputs_reject_before_capture(self):
        base,constants,blocks=fixture()
        for change in ({"distance":True},{"steps":False},{"unknown":1}):
            invalid=copy.deepcopy(blocks);invalid[0].update(change)
            with patch.object(native,"capture_source") as capture:
                with self.assertRaises((ValueError,TypeError)):batch.prepare(base,constants,1,invalid)
                capture.assert_not_called()

    def test_hanging_node_blocks_even_when_blocks_share_endpoint_GRIDs(self):
        base=FakeBase();constants=SimpleNamespace(NASTRAN=1)
        points=[(0,0),(1,0),(1,2),(0,2),(1,1),(2,0),(2,1),(2,2)]
        for n,(x,y) in enumerate(points,1):
            base.add("GRID",n,NID=n,CP=0,X1=float(x),X2=float(y),X3=0.)
        for eid,ids in ((10,[1,2,3,4]),(20,[2,6,7,5]),(21,[5,7,8,3])):
            base.add("SHELL",eid,EID=eid,PID=8,type="CQUAD4",**{f"G{i}":n for i,n in enumerate(ids,1)})
        blocks=[{"id":name,"shell_ids":ids,"direction":[0.,0.,1.],"distance":2.,"steps":2,"property_id":12}
                for name,ids in (("coarse",[10]),("fine",[20,21]))]
        with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
        self.assertEqual(result["status"],"blocked")
        issue=next(r for r in result["issues"] if r["code"]=="hanging_source_GRID_on_edge")
        self.assertIn({"edge_node_ids":[2,3],"interior_node_id":5},issue["locations"])
        self.assertTrue(all("sweep_params" not in g for g in result["groups"]))
        # 直接调用原有写入口也必须经过同一检查，不能绕开新增合批准备。
        source=native.capture_source(base,constants,1,[10,20,21])
        with self.assertRaisesRegex(ValueError,"hanging_source_GRID_on_edge"):
            native._source_preflight(source,{**blocks[0],"shell_ids":[10,20,21]})
        # 整体大坐标偏移不应静默越过接口检查；精度不足须保守阻止。
        for (typ,_),entity in base.entities.items():
            if typ=="GRID":entity.fields["X3"]=float(2**49)
        with patch.object(native,"_psolid_preflight"):shifted=batch.prepare(base,constants,1,blocks)
        self.assertEqual(shifted["status"],"blocked")
        self.assertIn("source_interface_precision_insufficient",[r["code"] for r in shifted["issues"]])
        self.assertTrue(all("sweep_params" not in g for g in shifted["groups"]))

    def test_shared_components_pack_before_independent_blocks_regardless_of_names(self):
        base=FakeBase();constants=SimpleNamespace(NASTRAN=1);nodes={}
        def quad(eid,x,y):
            ids=[]
            for p in ((x,y),(x+1,y),(x+1,y+1),(x,y+1)):
                if p not in nodes:
                    n=len(nodes)+1;nodes[p]=n
                    base.add("GRID",n,NID=n,CP=0,X1=float(p[0]),X2=float(p[1]),X3=0.)
                ids.append(nodes[p])
            base.add("SHELL",eid,EID=eid,PID=8,type="CQUAD4",**{f"G{i}":n for i,n in enumerate(ids,1)})
        quad(10,0,0);quad(30,1,0)
        for eid,(x,y) in enumerate(((10,0),(11,0),(10,1),(11,1)),20):quad(eid,x,y)
        blocks=[{"id":name,"shell_ids":ids,"direction":[0.,0.,1.],"distance":2.,"steps":100,"property_id":12}
                for name,ids in (("A",[10]),("B",[20,21,22,23]),("C",[30]))]
        results=[]
        for name in ("B","Z"):
            blocks[1]["id"]=name
            with patch.object(native,"_psolid_preflight"):result=batch.prepare(base,constants,1,blocks)
            self.assertEqual(result["status"],"ready_for_existing_sweep")
            results.append(sorted(g["sweep_params"]["shell_ids"] for g in result["groups"]))
        self.assertEqual(results,[[[10,30],[20,21,22,23]]]*2)

    def test_existing_source_target_audit_slices_union_instead_of_second_native_capture(self):
        from ansa_mcp_bridge.operations import structured_meshing,structured_evidence
        base,constants,_=fixture()
        state=SimpleNamespace(assert_main=lambda:None)
        request={"deck":1,"source_ids":[10],"target_ids":[20],"direction":[0.,0.,1.],"layers":2,"target_length":1.}
        with patch.dict(sys.modules,{"ansa":SimpleNamespace(base=base,constants=constants)}), \
                patch.object(native,"capture_source",wraps=native.capture_source) as capture, \
                patch.object(structured_evidence,"store_report",side_effect=lambda s,d,k,r:r):
            result=structured_meshing.execute(state,"mesh_source_audit",request)
        self.assertEqual(capture.call_count,1)
        self.assertEqual(result["source_snapshot_sha256"],native.capture_source(base,constants,1,[10])["snapshot_sha256"])
