import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from ansa_mcp_bridge.operations import partition_aggregates as aggregate,structured_native as n


class PartitionAggregateTests(unittest.TestCase):
    def fixture(self):
        fields={'Mass':2.,'Area':2.,'COG x':1.,'COG y':0.,'COG z':0.,'Diagonal':3.,'Name':'keep'}
        part={'entity':{'type':'ANSAPART','id':1,'deck':1},'card_fields':list(fields),'fields':fields}
        faces=[{'entity':{'type':'FACE','id':i,'deck':1},'assignments':{'__part__':1},'geometry':{'area':1.},'fields':{}} for i in (2,3)]
        return {'entities':[part]+faces},part

    def test_native_part_and_all_face_recomputation_are_both_required(self):
        snapshot,part=self.fixture();base=Mock();base.CollectEntities.return_value=[]
        def mass(e,**kwargs):return [1.,0. if e._id==2 else 2.,0.,0.]+[0.]*18
        base.CalcElementMass.side_effect=mass;base.GetFaceArea.return_value=1.
        with patch.object(aggregate.features,'_entity',side_effect=lambda b,d,k,i:SimpleNamespace(_id=i)), \
             patch.object(aggregate.native,'_card',return_value=part), patch.object(aggregate.part_aggregates,'capture',return_value={'origin':'fresh'}):
            result=aggregate.capture(base,1,snapshot,1)
            self.assertEqual(result['values']['COG x'],1.)
            part['fields']['Area']=3.
            with self.assertRaisesRegex(RuntimeError,'Area'):aggregate.capture(base,1,snapshot,1)

    def test_only_bound_aggregate_fields_can_change(self):
        snapshot,before=self.fixture();after=copy.deepcopy(before);after['fields']['Area']=3.;after['fields']['Mass']=3.
        def proof(row):return {'part_id':1,'part_scope_sha256':n._hash(row),'values':{k:row['fields'][k] for k in aggregate.FIELDS}}
        evidence={'before':proof(before),'after':proof(after)}
        self.assertTrue(aggregate.validated_change(before,after,evidence))
        after['fields']['Name']='changed';self.assertFalse(aggregate.validated_change(before,after,evidence))
        evidence['after']=proof(after);self.assertFalse(aggregate.validated_change(before,after,evidence))
        before['fields']['Mass']=7.;self.assertFalse(aggregate.validated_change(before,after,evidence))

    def test_existing_part_fe_prevents_aggregate_exception(self):
        snapshot,_=self.fixture();snapshot['entities'].append({'entity':{'type':'SOLID','id':9},'part_id':1})
        base=Mock()
        with self.assertRaisesRegex(ValueError,'CAD-only'):aggregate.capture(base,1,snapshot,1)
        base.CalcElementMass.assert_not_called()
        snapshot,_=self.fixture();base.CollectEntities.return_value=[SimpleNamespace(_id=42,kind='BEAM')]
        with patch.object(aggregate.features,'_entity',return_value=object()):
            with self.assertRaisesRegex(ValueError,'__ELEMENTS__'):aggregate.capture(base,1,snapshot,1)
        base.CalcElementMass.assert_not_called()


if __name__=='__main__':unittest.main()
