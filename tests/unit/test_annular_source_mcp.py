"""Risk-focused optional annular-template schema and shared-capture forwarding."""
import copy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_bridge.operations import structured_meshing, structured_native, structured_evidence


OPTIONS={'center':[0.,0.,0.],'axis':[0.,0.,1.],'inner_radius':10.,'outer_radius':20.,
         'max_chord_error':.2,'tolerance':.001}


class AnnularSourceMcpTests(unittest.TestCase):
    def test_optional_schema_is_strict_and_legacy_payload_stays_unchanged(self):
        calls=[]
        def submit(tool,params,operation_id,expected_target,expected_model,ttl):
            calls.append((tool,params))
            return {'task':{'target':expected_target,'model':expected_model},'state':'queued',
                    'result':None,'dispatch':None,'cancel_pending':False,'acked':False,
                    'operation_id':operation_id,'task_id':'t'}
        tool=create_server(SimpleNamespace(submit_task=submit))._tool_manager.get_tool('ansa_mesh_source_audit')
        model=tool.fn_metadata.arg_model
        fields={'operation_id':'annular','expected_target':{'backend_id':'b','instance_id':'i','session_id':'s','generation':1},
                'expected_model':{'epoch':1},'deck':1,'source_ids':[1],'direction':[0.,0.,1.],
                'layers':2,'target_length':5.}
        def invoke(extra):
            validated=model.model_validate({**fields,**extra})
            args=validated.model_dump();args.update(expected_target=validated.expected_target,expected_model=validated.expected_model)
            args['annular_options']=validated.annular_options
            return tool.fn(**args)
        invoke({});self.assertNotIn('annular_options',calls[-1][1])
        invoke({'annular_options':OPTIONS});self.assertEqual(calls[-1][1]['annular_options'],OPTIONS)
        for changed in ({'tolerance':.01},{'inner_radius':True},{'outer_radius':float('inf')},{'extra':1}):
            with self.subTest(changed=changed),self.assertRaises(ValidationError):
                model.model_validate({**fields,'annular_options':{**OPTIONS,**changed}})

    def test_annular_check_uses_only_source_subset_from_one_joint_native_capture(self):
        from ansa_mcp_bridge.operations import annular_source, mesh_planning, structured_batch
        snapshot={'snapshot_sha256':'a'*64,'nodes':[],'elements':[]}
        source={**snapshot,'snapshot_sha256':'b'*64}
        params={'deck':1,'source_ids':[1],'target_ids':[2],'direction':[0.,0.,1.],
                'layers':2,'target_length':5.,'annular_options':OPTIONS}
        for status in ('verified_mesh_template','unsupported'):
            with self.subTest(status=status), \
                 patch.dict(sys.modules,{'ansa':SimpleNamespace(base=object(),constants=object())}), \
                 patch.object(structured_meshing.catalog,'_deck',return_value=1), \
                 patch.object(structured_native,'capture_source',return_value=snapshot) as capture, \
                 patch.object(structured_batch,'source_subset',return_value=source) as subset, \
                 patch.object(mesh_planning,'audit_source',side_effect=lambda *a,**kw:{'status':'pass','issues':[]}), \
                 patch.object(annular_source,'audit',return_value={'status':status}) as audit, \
                 patch.object(structured_evidence,'store_report',side_effect=lambda s,d,k,r:r):
                result=structured_meshing.execute(SimpleNamespace(assert_main=Mock()),'mesh_source_audit',copy.deepcopy(params))
                capture.assert_called_once();self.assertEqual(capture.call_args.args[-1],[1,2])
                subset.assert_called_once_with(snapshot,[1]);audit.assert_called_once_with(source,OPTIONS)
                self.assertEqual(result['source_snapshot_sha256'],'b'*64)
                self.assertEqual(result['summary']['annular_template_status'],status)
                self.assertEqual(result['status'],'pass' if status=='verified_mesh_template' else 'failed')


if __name__=='__main__':unittest.main()
