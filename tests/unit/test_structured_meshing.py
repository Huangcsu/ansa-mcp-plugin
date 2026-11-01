"""Risk-focused contracts for structured mesh registration and evidence."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

import ansa_mcp_bridge.operations as bridge_operations
from ansa_mcp_bridge.operations import structured_evidence, structured_meshing, structured_native, mesh_provenance
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model


NEW_TOOLS = {
    "ansa_mesh_seed_plan", "ansa_mesh_sweep_prepare",
    "ansa_mesh_interface_audit", "ansa_mesh_block_plan", "ansa_mesh_source_audit",
    "ansa_geometry_partition_plan", "ansa_geometry_partition_apply", "ansa_mesh_sweep",
    "ansa_mesh_provenance", "ansa_mesh_evidence_page", "ansa_geometry_features",
    "ansa_geometry_boundary_prepare", "ansa_mesh_seed_read", "ansa_mesh_seed_apply",
    "ansa_mesh_cad_map", "ansa_geometry_sweep_candidates", "ansa_mesh_map_block_inspect",
    "ansa_mesh_map_block_prepare", "ansa_mesh_map_block_fill", "ansa_mesh_map_blocks_prepare",
    "ansa_mesh_annular_source_prepare",
}


class StructuredMeshingTests(unittest.TestCase):
    def test_sweep_freeze_source_schema_is_strict_and_defaults_false(self):
        submitted = []
        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            submitted.append((tool, params))
            return {"task": {"target": expected_target, "model": expected_model},
                    "state": "queued", "result": None, "dispatch": None,
                    "cancel_pending": False, "acked": False,
                    "operation_id": operation_id, "task_id": "task-" + operation_id}
        server = create_server(SimpleNamespace(submit_task=submit))
        tool = server._tool_manager.get_tool("ansa_mesh_sweep")
        model = tool.fn_metadata.arg_model
        fields = {"operation_id": "sweep-schema", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "deck": 1, "shell_ids": [10],
            "direction": [0., 0., 1.], "distance": 1., "steps": 1,
            "property_id": 12, "expected_source_sha256": "a" * 64, "backup_path": "backup.ansa"}
        self.assertIs(model.model_validate(fields).freeze_source, False)
        self.assertIs(model.model_validate({**fields, "freeze_source": True}).freeze_source, True)
        def invoke(change):
            values = {**fields, **change}
            validated = model.model_validate(values)
            values.update(expected_target=validated.expected_target,
                          expected_model=validated.expected_model)
            tool.fn(**values)
            return submitted[-1][1]
        self.assertNotIn("freeze_source", invoke({}))
        self.assertNotIn("freeze_source", invoke({"freeze_source": False}))
        self.assertIs(invoke({"freeze_source": True})["freeze_source"], True)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                model.model_validate({**fields, "freeze_source": value})

    def test_integer_native_id_map_has_stable_json_hash_and_rejects_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            state=SimpleNamespace(session_dir=Path(directory),model_epoch=1,instance_id='i',session_id='s')
            result={'layout':{'face_owners':{2:[1],10:[2],1:[1,2]}}}
            desc=structured_evidence.store_report(state,1,'mesh_native_operation_result',result)
            page=structured_evidence.read_page(state,1,desc['report_id'],'/layout/face_owners',0,20)
            self.assertEqual([row['key'] for row in page['rows']],['1','10','2'])
            self.assertEqual(result['layout']['face_owners'][1],[1,2])
            with self.assertRaisesRegex(ValueError,'ambiguous'):
                structured_evidence.store_report(state,1,'mesh_native_operation_result',{'map':{1:'a','1':'b'}})
            small=structured_meshing._bounded_operation_result(state,1,{'ok':True,'layout':{1:[2],10:(3,4)}})
            from ansa_mcp_bridge.journal import Journal
            Journal._canonical(small,'result')
            self.assertEqual(small['layout'],{'1':[2],'10':[3,4]})

    def test_large_native_write_receipt_preserves_outcome_and_complete_pages(self):
        for ok in (True, False):
            with self.subTest(ok=ok), tempfile.TemporaryDirectory() as directory:
                state = SimpleNamespace(session_dir=Path(directory), model_epoch=1, instance_id='i', session_id='s')
                result = {'ok':ok,'scope_verified':ok,'possible_effects':not ok,'recovery_required':not ok,
                          'new_solid_ids':[1,2],'ledger_id':'l','ledger_hash':'a'*64,'verification':{'rows':['x'*4096]*80}}
                desc = structured_meshing._bounded_operation_result(state,1,result)
                self.assertEqual(desc['ok'],ok); self.assertEqual(desc['recovery_required'],not ok)
                self.assertEqual(desc['new_solid_ids'],[1,2]); self.assertTrue(desc['result_paged'])
                first = structured_evidence.read_page(state,1,desc['report_id'],'/verification/rows',0,40)
                second = structured_evidence.read_page(state,1,desc['report_id'],'/verification/rows',40,40)
                self.assertEqual(first['rows']+second['rows'],result['verification']['rows'])
                self.assertEqual(desc['result_sha256'],structured_meshing._digest(result))

    def test_both_map_block_plan_pages_require_native_freshness(self):
        from ansa_mcp_bridge.operations import map_block_prepare, map_block_fill
        for kind, module in (('mesh_map_block_plan', map_block_prepare), ('mesh_map_block_fill_plan', map_block_fill)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                state = SimpleNamespace(session_dir=Path(directory), model_epoch=1, instance_id='i', session_id='s', assert_main=Mock())
                desc = structured_evidence.store_report(state, 1, kind, {'plan_sha256': 'a'*64, 'params': {'deck': 1}})
                with self.assertRaisesRegex(ValueError, 'freshness'):
                    structured_evidence.read_page(state, 1, desc['report_id'], 'plan_sha256')
                with patch.dict(sys.modules, {'ansa': SimpleNamespace(base=object(), constants=object(), mesh=object())}), \
                     patch.object(structured_meshing.catalog, '_deck', return_value=1), \
                     patch.object(module, 'validate_report', side_effect=ValueError('native plan is stale')) as validate:
                    with self.assertRaisesRegex(ValueError, 'stale'):
                        structured_meshing.execute(state, 'mesh_evidence_page', {'deck': 1, 'report_id': desc['report_id'],
                                                    'section': 'plan_sha256', 'offset': 0, 'limit': 1})
                validate.assert_called_once()

    def test_fill_provenance_failure_sets_consistent_recovery_flag(self):
        from ansa_mcp_bridge.operations import map_block_fill
        points = [{'id': i+1, 'xyz': p} for i, p in enumerate([[0,0,0],[1,0,0],[1,1,0],[0,1,0]])]
        source = {'nodes': points, 'elements': [{'id': 10, 'nodes': [1,2,3,4], 'type': 'CQUAD4'}]}
        state = SimpleNamespace(model_epoch=1, instance_id='i', session_id='s', session_dir=Path.cwd(),
                 authorization={'backend_id':'b','generation':1}, identity=lambda: {'instance_id':'i'})
        successful = {'ok': True, 'scope_verified': True, 'possible_effects': False, 'recovery_required': False, 'new_solid_ids': [99]}
        with patch.dict(sys.modules, {'ansa': SimpleNamespace(base=object(), constants=object(), mesh=object())}), \
             patch.object(map_block_fill, 'preflight', return_value={'plan_sha256': 'a'*64, 'source': source}), \
             patch.object(map_block_fill, 'apply_prepared', return_value=successful), \
             patch.object(mesh_provenance, 'create_ledger', return_value={'ledger_hash': 'b'*64}), \
             patch.object(mesh_provenance, 'record_stage', return_value={'ledger_hash': 'c'*64}), \
             patch.object(structured_native, 'capture_mesh', side_effect=RuntimeError('provenance readback failed')):
            result = structured_meshing._with_operation_ledger(state, 1, 'mesh_map_block_fill_apply', {'expected_plan_sha256': 'a'*64})
        self.assertFalse(result['ok']); self.assertTrue(result['possible_effects'])
        self.assertTrue(result['recovery_required']); self.assertEqual(result['provenance_stage'], 'recovery_required')

    def test_map_block_prepare_schema_action_requirements_and_dispatch(self):
        calls = []
        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            calls.append((tool, params))
            return {'task': {'target': expected_target, 'model': expected_model}, 'state': 'queued',
                    'result': None, 'dispatch': None, 'cancel_pending': False, 'acked': False,
                    'operation_id': operation_id, 'task_id': 'task-'+operation_id}
        server = create_server(SimpleNamespace(submit_task=submit))
        tool = server._tool_manager.get_tool('ansa_mesh_map_block_prepare'); model = tool.fn_metadata.arg_model
        common = {'operation_id': 'map-block-schema', 'expected_target': {
            'backend_id': 'b', 'instance_id': 'i', 'session_id': 's', 'generation': 1},
            'expected_model': {'epoch': 1}, 'deck': 1, 'face_ids': [1,2,3,4,5,6],
            'source_face_id': 1, 'target_face_id': 2, 'direction': [0.,0.,1.],
            'distance': 10., 'target_length': 5., 'property_id': 2}
        def invoke(changes):
            validated = model.model_validate({**common, **changes})
            fields = validated.model_dump()
            fields.update(expected_target=validated.expected_target, expected_model=validated.expected_model)
            return tool.fn(**fields)
        invoke({'action': 'plan'}); self.assertEqual(calls[-1][0], 'mesh_map_block_plan')
        invoke({'action': 'apply', 'backup_path': str(Path.cwd()/'map-block-backup.ansa'), 'expected_plan_sha256': 'a'*64})
        self.assertEqual(calls[-1][0], 'mesh_map_block_apply')
        for change in ({'action':'apply'}, {'action':'plan','expected_plan_sha256':'a'*64},
                       {'action':'plan','source_face_id':2}, {'action':'plan','target_face_id':7}):
            with self.subTest(change=change), self.assertRaises(ValueError): invoke(change)
        for change in ({'face_ids':[1,1,2,3,4,5]}, {'target_length':True}, {'unknown':True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({**common,'action':'plan',**change})

    def test_map_block_inspect_schema_and_readonly_dispatch(self):
        server = create_server(SimpleNamespace())
        model = server._tool_manager.get_tool("ansa_mesh_map_block_inspect").fn_metadata.arg_model
        fields = {"operation_id": "map-block-schema", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "deck": 1, "volume_ids": [1]}
        self.assertEqual(model.model_validate(fields).volume_ids, [1])
        for change in ({"volume_ids": []}, {"volume_ids": [1, 1]}, {"volume_ids": [True]},
                       {"volume_ids": list(range(1, 22))}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({**fields, **change})
        from ansa_mcp_bridge.operations import map_block_inspection
        with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=object(), constants=object(), mesh=object())}), \
             patch.object(structured_meshing.catalog, "_deck", return_value=1), \
             patch.object(map_block_inspection, "capture", return_value={"status": "inspected"}) as capture, \
             patch.object(structured_evidence, "store_report", return_value={"ok": True}):
            result = bridge_operations.execute(SimpleNamespace(assert_main=Mock()), "mesh_map_block_inspect", {"deck": 1, "volume_ids": [1]})
        self.assertTrue(result["ok"])
        capture.assert_called_once()
        self.assertFalse(writes_model("mesh_map_block_inspect"))

    def test_sweep_candidate_schema_and_readonly_dispatch(self):
        server = create_server(SimpleNamespace())
        model = server._tool_manager.get_tool("ansa_geometry_sweep_candidates").fn_metadata.arg_model
        fields = {"operation_id": "candidate-schema", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "deck": 1, "face_ids": list(range(1, 101)), "target_length": 5.}
        self.assertEqual(len(model.model_validate(fields).face_ids), 100)
        for change in ({"face_ids": []}, {"face_ids": [1, 1]}, {"face_ids": list(range(1, 102))},
                       {"target_length": True}, {"target_length": 0.}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({**fields, **change})
        from ansa_mcp_bridge.operations import geometry_sweep_candidates
        state = SimpleNamespace(assert_main=Mock())
        with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=object(), constants=object())}), \
             patch.object(structured_meshing.catalog, "_deck", return_value=1), \
             patch.object(geometry_sweep_candidates, "capture", return_value={"status": "classified"}) as capture, \
             patch.object(structured_evidence, "store_report", return_value={"ok": True}) as store:
            result = bridge_operations.execute(state, "geometry_sweep_candidates", {"deck": 1, "face_ids": [1], "target_length": 5.})
        self.assertTrue(result["ok"])
        capture.assert_called_once()
        self.assertEqual(store.call_args.args[2], "geometry_sweep_candidates")
        self.assertFalse(writes_model("geometry_sweep_candidates"))

    def test_cad_map_uses_face_binding_and_preserves_source_to_solid_mapping(self):
        from ansa_mcp_bridge.operations import cad_map
        with tempfile.TemporaryDirectory() as directory:
            state=SimpleNamespace(session_dir=Path(directory),model_epoch=1,instance_id='i',session_id='s',
                authorization={'backend_id':'b','generation':1},identity=lambda:{'instance_id':'i','session_id':'s'})
            source={'nodes':[{'id':i,'xyz':[float(i),0.,0.]} for i in range(1,5)],
                    'elements':[{'id':10,'nodes':[1,2,3,4]}]}
            output={'nodes':[{'id':i,'xyz':[float(i),0.,0.]} for i in range(1,9)],
                    'elements':[{'id':20,'nodes':list(range(1,9))}]}
            prepared={'plan_sha256':'a'*64,'source':source}
            result={'ok':True,'scope_verified':True,'new_solid_ids':[20],
                    'verification':{'source_cell_matches':[{'source_element_id':10,'output_element_id':20,'layer':1}]}}
            with patch.dict(sys.modules,{'ansa':SimpleNamespace(base=object(),constants=object(),mesh=object())}), \
                 patch.object(cad_map,'preflight',return_value=prepared),patch.object(cad_map,'apply_prepared',return_value=result), \
                 patch.object(structured_native,'capture_mesh',return_value=output):
                response=structured_meshing._with_operation_ledger(state,1,'mesh_cad_map_apply',{'expected_plan_sha256':'a'*64})
            ledger=mesh_provenance.read_ledger(Path(directory),response['ledger_id'])['ledger']
            self.assertEqual(ledger['binding']['source']['type'],'FACE_SCOPE')
            self.assertEqual(ledger['stage'],'verified')
            self.assertTrue(response['provenance_mapping_complete'])
            self.assertEqual(ledger['fe_mapping']['pairs'][0]['created'][0]['id'],20)

    def test_boundary_ledger_uses_preflight_and_marks_partial_result_for_recovery(self):
        from ansa_mcp_bridge.operations import geometry_boundary
        with tempfile.TemporaryDirectory() as directory:
            state=SimpleNamespace(session_dir=Path(directory),model_epoch=1,instance_id='i',session_id='s',
                authorization={'backend_id':'b','generation':1},identity=lambda:{'instance_id':'i','session_id':'s'})
            prepared={'witness':{'source_snapshot_sha256':'a'*64}}
            with patch.dict(sys.modules,{'ansa':SimpleNamespace(base=object(),constants=object())}), \
                 patch.object(geometry_boundary,'preflight',return_value=prepared) as preflight, \
                 patch.object(geometry_boundary,'apply_prepared',return_value={
                     'ok':False,'scope_verified':False,'new_curve_ids':[12],'mappings':[{'cons_id':1}],
                     'recovery_required':True,'possible_effects':True}) as apply:
                result=structured_meshing._with_operation_ledger(state,1,'geometry_boundary_prepare',{
                    'deck':1,'expected_source_snapshot_sha256':'a'*64})
            preflight.assert_called_once();apply.assert_called_once()
            self.assertIs(apply.call_args.args[-1],prepared)
            self.assertEqual(result['new_curve_ids'],[12])
            self.assertEqual(result['provenance_stage'],'recovery_required')
            ledger=mesh_provenance.read_ledger(Path(directory),result['ledger_id'])['ledger']
            self.assertEqual(ledger['stage'],'recovery_required')
            self.assertEqual(ledger['binding']['source']['type'],'FACE_SCOPE')

    def test_failed_partial_write_retains_confirmed_fe_without_claiming_source_mapping(self):
        points=[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]]
        source={"snapshot_sha256":"a"*64,"nodes":[{"id":i+1,"xyz":p} for i,p in enumerate(points[:4])],
                "elements":[{"id":10,"type":"CQUAD4","nodes":[1,2,3,4],"property_id":2}]}
        output={"nodes":[{"id":i+1,"xyz":p} for i,p in enumerate(points)],
                "elements":[{"id":101,"type":"CHEXA","nodes":list(range(1,9)),"property_id":3}]}
        with tempfile.TemporaryDirectory() as directory:
            state=SimpleNamespace(session_dir=Path(directory),model_epoch=1,instance_id="i",session_id="s",
                authorization={"backend_id":"b","generation":1},identity=lambda:{"instance_id":"i"})
            with patch.dict(sys.modules,{"ansa":SimpleNamespace(base=object(),constants=object())}), \
                 patch.object(structured_native,"capture_source",return_value=source), \
                 patch.object(structured_native,"capture_mesh",return_value=output), \
                 patch.object(structured_native,"execute",return_value={"ok":False,"scope_verified":False,
                     "new_solid_ids":[101],"new_mesh":None,"error":"partial readback failure"}):
                result=structured_meshing._with_operation_ledger(state,1,"mesh_sweep",{
                    "deck":1,"shell_ids":[10],"expected_source_sha256":"a"*64})
            ledger=mesh_provenance.read_ledger(Path(directory),result["ledger_id"])["ledger"]
            self.assertEqual(ledger["stage"],"recovery_required")
            self.assertEqual([r["id"] for r in ledger["fe_mapping"]["original_fe"]],[10])
            self.assertEqual([r["id"] for r in ledger["fe_mapping"]["new_fe"]],[101])
            self.assertEqual(ledger["fe_mapping"]["pairs"][0]["created"],[])
            self.assertFalse(result["provenance_mapping_complete"])

    def test_scope_diagnostics_survive_executor_journal_failure_result(self):
        from ansa_mcp_bridge.executor import MainThreadExecutor
        error=ValueError("scope mismatch")
        error.diagnostics={"changed_components":["geometry"],"expected_scope_sha256":"a"*64}
        task={"tool":"quality_locate","params":{},"task_id":"diagnostic","model":{"epoch":1}}
        state=SimpleNamespace(assert_main=Mock(), journal=Mock(blocked=False),
            stopped=Mock(is_set=Mock(return_value=False)), connected=Mock(is_set=Mock(return_value=True)),
            authorization={"backend_id":"b"}, connected_backend_id="b",validate_task=Mock(),
            probe=Mock(return_value={"model":{"epoch":1}}),identity=Mock(return_value={"pid":1}),
            _witness=None,main_thread_id=1)
        state.journal.next_queued.return_value=task
        state.journal.claim.return_value={"state":"running"}
        with patch("ansa_mcp_bridge.operations.execute",side_effect=error):
            MainThreadExecutor(state)._task_tick()
        self.assertEqual(state.journal.finish.call_args.kwargs["result"]["diagnostics"],error.diagnostics)

    def test_successful_scope_operation_preserves_unknown_native_state_and_finishes_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1,
                instance_id="i", session_id="s", authorization={"backend_id":"b","generation":1},
                identity=lambda: {"instance_id":"i","session_id":"s"})
            def native(_state, tool, _params):
                return ({"plan_sha256":"a"*64} if tool=="geometry_partition_plan"
                        else {"ok":True,"scope_verified":True})
            with patch.object(structured_native,"execute",side_effect=native):
                result=structured_meshing._with_operation_ledger(state,1,"geometry_partition_apply",
                    {"deck":1,"expected_plan_sha256":"a"*64,"backup_path":"unused.ansa"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["provenance_stage"],"verified")
            self.assertEqual(result["regeneration_status"],"incomplete")
            ledger=mesh_provenance.read_ledger(Path(directory),result["ledger_id"])["ledger"]
            self.assertEqual(ledger["native_state"]["ownership"],"unknown")
            stored=structured_meshing._provenance(state,1,{"deck":1,"action":"read",
                "ledger_id":result["ledger_id"],"payload":None,"expected_hash":result["ledger_hash"]})
            self.assertEqual(stored["ledger_id"],result["ledger_id"])
            page=structured_evidence.read_page(state,1,stored["report_id"],"/ledger/native_state")
            self.assertIn({"key":"regeneration","value":"unknown"},page["rows"])
            with self.assertRaisesRegex(RuntimeError,"cannot re-enter applying"):
                mesh_provenance.validate_resume(Path(directory),result["ledger_id"],ledger["binding"])

    def test_registration_and_write_gates_are_consistent(self):
        server = create_server(SimpleNamespace())
        tools = asyncio.run(server.list_tools())
        self.assertTrue(NEW_TOOLS <= {item.name for item in tools})
        self.assertEqual(len(NEW_TOOLS), 21)
        self.assertEqual(len(tools), 93)
        for name in NEW_TOOLS - {"ansa_mesh_cad_map", "ansa_mesh_map_block_prepare", "ansa_mesh_map_block_fill", "ansa_mesh_map_blocks_prepare", "ansa_mesh_annular_source_prepare"}:
            self.assertEqual(writes_model(name.removeprefix("ansa_")),
                             name in {"ansa_geometry_partition_apply", "ansa_mesh_sweep",
                                      "ansa_geometry_boundary_prepare", "ansa_mesh_seed_apply"})
        self.assertIn("mesh_provenance", READ_ONLY_TOOLS)
        self.assertIn("geometry_features", READ_ONLY_TOOLS)
        self.assertIn("mesh_seed_read", READ_ONLY_TOOLS)
        self.assertNotIn("mesh_seed_apply", READ_ONLY_TOOLS)

    def test_cad_map_schema_action_requirements_dispatch_and_classification(self):
        submitted = []

        def submit_task(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            submitted.append((tool, params, operation_id, expected_target, expected_model, ttl_seconds))
            return {
                "task": {"target": expected_target, "model": expected_model},
                "state": "queued", "result": None, "dispatch": None,
                "cancel_pending": False, "acked": False,
                "operation_id": operation_id, "task_id": "task-" + operation_id,
            }

        server = create_server(SimpleNamespace(submit_task=submit_task))
        tool = server._tool_manager.get_tool("ansa_mesh_cad_map")
        model = tool.fn_metadata.arg_model
        schema = model.model_json_schema()["properties"]
        self.assertEqual(schema["action"]["enum"], ["plan", "apply"])
        self.assertEqual((schema["face_ids"]["minItems"], schema["face_ids"]["maxItems"]), (6, 6))
        self.assertTrue(schema["face_ids"]["uniqueItems"])
        self.assertEqual((schema["steps"]["minimum"], schema["steps"]["maximum"]), (1, 100))
        self.assertEqual(schema["ttl_seconds"]["default"], 300)

        common = {
            "operation_id": "cad-map-plan",
            "expected_target": {
                "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
            "face_ids": [1, 2, 3, 4, 5, 6],
            "source_face_id": 1,
            "target_face_id": 6,
            "direction": [0.0, 0.0, 1.0],
            "distance": 10.0,
            "steps": 1,
            "property_id": 7,
            "output_part_id": 8,
        }

        def invoke(values):
            validated = model.model_validate(values)
            kwargs = validated.model_dump()
            kwargs["expected_target"] = validated.expected_target
            kwargs["expected_model"] = validated.expected_model
            return tool.fn(**kwargs)

        invoke({**common, "action": "plan"})
        plan_params = {
            key: value for key, value in common.items()
            if key in {"deck", "face_ids", "source_face_id", "target_face_id", "direction",
                       "distance", "steps", "property_id", "output_part_id"}
        }
        self.assertEqual(submitted[-1][0:3], ("mesh_cad_map_plan", plan_params, "cad-map-plan"))
        self.assertEqual(submitted[-1][-1], 300)

        backup_path = str(Path.cwd() / "cad-map-backup.ansa")
        invoke({**common, "action": "apply", "operation_id": "cad-map-apply",
                "expected_plan_sha256": "a" * 64, "backup_path": backup_path})
        self.assertEqual(submitted[-1][0], "mesh_cad_map_apply")
        self.assertEqual(submitted[-1][1], {
            **plan_params, "expected_plan_sha256": "a" * 64, "backup_path": backup_path,
        })

        for changes in (
            {"action": "plan", "expected_plan_sha256": "a" * 64},
            {"action": "plan", "backup_path": backup_path},
            {"action": "apply"},
            {"action": "apply", "expected_plan_sha256": "a" * 64},
            {"action": "apply", "backup_path": backup_path},
            {"action": "plan", "source_face_id": 6, "target_face_id": 6},
            {"action": "plan", "target_face_id": 7},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                invoke({**common, **changes})
        for changes in (
            {"face_ids": [1, 2, 3, 4, 5]},
            {"face_ids": [1, 2, 3, 4, 5, 5]},
            {"face_ids": [1, 2, 3, 4, 5, True]},
            {"steps": 101}, {"property_id": 0}, {"unexpected": True},
        ):
            with self.subTest(schema_changes=changes), self.assertRaises(ValidationError):
                model.model_validate({**common, "action": "plan", **changes})

        self.assertIn("mesh_cad_map_plan", READ_ONLY_TOOLS)
        self.assertNotIn("mesh_cad_map_apply", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("mesh_cad_map_plan"))
        self.assertTrue(writes_model("mesh_cad_map_apply"))
        with self.assertRaises(ValueError):
            writes_model("mesh_cad_map")

    def test_seed_read_and_apply_schemas_are_strict_bounded_and_classified(self):
        server = create_server(SimpleNamespace())
        read_model = server._tool_manager.get_tool("ansa_mesh_seed_read").fn_metadata.arg_model
        apply_model = server._tool_manager.get_tool("ansa_mesh_seed_apply").fn_metadata.arg_model
        common = {
            "expected_target": {
                "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
        }
        read_args = {
            **common, "operation_id": "seed-read", "face_ids": [1, 20], "cons_ids": [2, 32],
        }
        self.assertEqual(read_model.model_validate(read_args).face_ids, [1, 20])
        for change in (
            {"face_ids": [1, 1]}, {"face_ids": list(range(1, 22))},
            {"face_ids": [True]}, {"cons_ids": [2, 2]},
            {"cons_ids": list(range(1, 34))}, {"cons_ids": [False]},
            {"extra": True},
        ):
            with self.subTest(read_change=change), self.assertRaises(ValidationError):
                read_model.model_validate({**read_args, **change})

        apply_args = {
            **common,
            "operation_id": "seed-apply",
            "face_ids": [1, 20],
            "assignments": [{"cons_id": 32, "segments": 10000}],
            "expected_seed_snapshot_sha256": "a" * 64,
            "backup_path": str(Path.cwd() / "cons-seed-backup.ansa"),
        }
        self.assertEqual(apply_model.model_validate(apply_args).assignments[0].segments, 10000)
        for change in (
            {"face_ids": [1, 1]}, {"face_ids": list(range(1, 22))},
            {"assignments": []}, {"assignments": [{"cons_id": 1, "segments": True}]},
            {"assignments": [{"cons_id": 1, "segments": 0}]},
            {"assignments": [{"cons_id": 1, "segments": 1, "extra": 1}]},
            {"assignments": [{"cons_id": 1, "segments": 1}, {"cons_id": 1, "segments": 2}]},
            {"expected_seed_snapshot_sha256": "A" * 64},
            {"backup_path": "relative.ansa"}, {"unexpected": True},
        ):
            with self.subTest(apply_change=change), self.assertRaises(ValidationError):
                apply_model.model_validate({**apply_args, **change})

        self.assertFalse(writes_model("mesh_seed_read"))
        self.assertTrue(writes_model("mesh_seed_apply"))

    def test_seed_read_dispatch_stores_paged_evidence_under_seed_kind(self):
        base, constants = object(), object()
        state = SimpleNamespace(assert_main=Mock(), model_epoch=3)
        snapshot = {"snapshot_sha256": "a" * 64, "rows": []}
        helper = SimpleNamespace(capture=Mock(return_value=snapshot))
        params = {"deck": 4, "face_ids": [5], "cons_ids": [7]}
        with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=base, constants=constants)}), \
             patch.object(bridge_operations, "cons_seed", helper, create=True), \
             patch.object(structured_meshing.catalog, "_deck", return_value=4), \
             patch.object(structured_evidence, "store_report", return_value={"report_id": "r"}) as store:
            result = structured_meshing.execute(state, "mesh_seed_read", params)
        helper.capture.assert_called_once_with(state, base, 4, [5], [7])
        store.assert_called_once_with(state, 4, "mesh_seed_read", snapshot)
        self.assertEqual(result, {"report_id": "r"})

    def test_seed_snapshot_hash_is_exposed_without_mutation_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=2,
                instance_id="i", session_id="s")
            snapshot = {"seed_snapshot_sha256": "b" * 64, "cons": []}
            descriptor = structured_evidence.store_report(state, 1, "mesh_seed_read", snapshot)
            self.assertEqual(descriptor["seed_snapshot_sha256"], "b" * 64)
            self.assertFalse(descriptor["usable_for_mutation"])
            validator = Mock()
            page = structured_evidence.read_page(
                state, 1, descriptor["report_id"], "seed_snapshot_sha256",
                freshness_validator=validator,
            )
            validator.assert_called_once_with("mesh_seed_read", snapshot)
            self.assertEqual(page["rows"], ["b" * 64])
            self.assertFalse(page["usable_for_mutation"])

    def test_seed_evidence_page_checks_native_freshness_in_current_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=2,
                instance_id="i", session_id="s", assert_main=Mock())
            snapshot = {"seed_snapshot_sha256": "c" * 64, "boundaries": []}
            descriptor = structured_evidence.store_report(state, 1, "mesh_seed_read", snapshot)
            base, constants = object(), object()
            helper = SimpleNamespace(validate_report=Mock())
            with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=base, constants=constants)}), \
                 patch.object(bridge_operations, "cons_seed", helper, create=True), \
                 patch.object(structured_meshing.catalog, "_deck", return_value=1):
                page = structured_meshing.execute(state, "mesh_evidence_page", {
                    "deck": 1, "report_id": descriptor["report_id"],
                    "section": "seed_snapshot_sha256", "offset": 0, "limit": 100,
                })
            helper.validate_report.assert_called_once_with(state, base, 1, "mesh_seed_read", snapshot)
            self.assertEqual(page["rows"], ["c" * 64])
            self.assertFalse(page["usable_for_mutation"])

    def test_seed_apply_ledger_uses_dedicated_preflight_and_cons_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1,
                instance_id="i", session_id="s", authorization={"backend_id": "b", "generation": 1},
                identity=lambda: {"instance_id": "i", "session_id": "s"})
            base, constants = object(), object()
            prepared = {"witness": {"seed_snapshot_sha256": "a" * 64},
                        "before": {"snapshot_sha256": "a" * 64, "cons": []}}
            native_result = {"ok": True, "scope_verified": True, "recovery_required": False, "errors": []}
            helper = SimpleNamespace(
                preflight=Mock(return_value=prepared),
                apply_prepared=Mock(return_value=native_result),
            )
            params = {"deck": 1, "face_ids": [2], "assignments": [{"cons_id": 3, "segments": 8}],
                      "expected_seed_snapshot_sha256": "a" * 64,
                      "backup_path": str(Path(directory) / "backup.ansa")}
            with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=base, constants=constants)}), \
                 patch.object(bridge_operations, "cons_seed", helper, create=True):
                result = structured_meshing._with_operation_ledger(state, 1, "mesh_seed_apply", params)
            helper.preflight.assert_called_once_with(state, base, constants, params)
            helper.apply_prepared.assert_called_once_with(state, base, constants, params, prepared)
            self.assertIs(helper.apply_prepared.call_args.args[-1]["before"], prepared["before"])
            self.assertEqual(result["provenance_stage"], "verified")
            ledger = mesh_provenance.read_ledger(Path(directory), result["ledger_id"])["ledger"]
            self.assertEqual(ledger["binding"]["source"]["type"], "CONS_SCOPE")
            self.assertEqual(ledger["binding"]["source"]["fingerprint"], "a" * 64)

    def test_bridge_registry_routes_both_seed_operations(self):
        state = SimpleNamespace(assert_main=Mock())
        result = {"routed": True}
        with patch.dict(sys.modules, {"ansa": SimpleNamespace(base=object(), constants=object())}), \
             patch.object(structured_meshing, "execute", return_value=result) as execute:
            for tool in ("mesh_seed_read", "mesh_seed_apply", "mesh_cad_map_plan", "mesh_cad_map_apply"):
                self.assertEqual(bridge_operations.execute(state, tool, {"deck": 1}), result)
                execute.assert_called_with(state, tool, {"deck": 1})

    def test_geometry_boundary_prepare_schema_is_strict_bounded_and_write_classified(self):
        server = create_server(SimpleNamespace())
        model = server._tool_manager.get_tool("ansa_geometry_boundary_prepare").fn_metadata.arg_model
        common = {
            "operation_id": "geometry-boundary",
            "expected_target": {
                "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
            "face_ids": [1, 20],
            "cons_ids": [1, 32],
            "auxiliary_part_id": 3,
            "expected_source_snapshot_sha256": "a" * 64,
            "backup_path": str(Path.cwd() / "geometry-boundary-backup.ansa"),
        }
        self.assertEqual(model.model_validate(common).face_ids, [1, 20])
        self.assertTrue(writes_model("geometry_boundary_prepare"))
        self.assertNotIn("geometry_boundary_prepare", READ_ONLY_TOOLS)

        invalid = (
            {"expected_source_snapshot_sha256": "A" * 64},
            {"face_ids": list(range(1, 22))},
            {"cons_ids": list(range(1, 34))},
            {"face_ids": [1.0]},
            {"cons_ids": [True]},
            {"auxiliary_part_id": True},
            {"backup_path": ""},
            {"backup_path": "relative.ansa"},
            {"unexpected": True},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({**common, **change})

    def test_geometry_features_input_schema_is_strict_and_bounded(self):
        server = create_server(SimpleNamespace())
        model = server._tool_manager.get_tool("ansa_geometry_features").fn_metadata.arg_model
        common = {
            "operation_id": "feature-schema",
            "expected_target": {
                "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
        }
        defaults = model.model_validate(common)
        self.assertEqual(defaults.face_ids, [])
        self.assertEqual(defaults.curve_ids, [])
        self.assertIsNone(defaults.seed_options)
        self.assertIsNone(defaults.sweep_options)

        seeded = model.model_validate({**common, "seed_options": {"target_length": 1.0}})
        self.assertEqual(seeded.seed_options.model_dump(), {
            "target_length": 1.0, "max_chord_error": 0.02, "min_segments": 1,
            "multiple": 1, "max_segments": 10000,
        })
        sweep = {"source_face_id": 1, "target_face_id": 2,
                 "direction": [0., 0., 1.], "distance": 3.}
        planned = model.model_validate({**common, "sweep_options": sweep})
        self.assertEqual(planned.sweep_options.tolerance, 1e-7)

        invalid = (
            {"face_ids": [True]},
            {"face_ids": [0]},
            {"face_ids": list(range(1, 22))},
            {"curve_ids": list(range(1, 52))},
            {"seed_options": {}},
            {"seed_options": {"target_length": 0.0}},
            {"seed_options": {"target_length": 1.0, "max_chord_error": 0.0}},
            {"seed_options": {"target_length": 1.0, "multiple": 1.0}},
            {"seed_options": {"target_length": 1.0, "max_segments": 10001}},
            {"seed_options": {"target_length": 1.0, "unexpected": True}},
            {"sweep_options": {**sweep, "source_face_id": True}},
            {"sweep_options": {**sweep, "distance": float("nan")}},
            {"sweep_options": {**sweep, "tolerance": 0.01}},
            {"sweep_options": {**sweep, "steps": 2}},
        )
        for extra in invalid:
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError):
                    model.model_validate({**common, **extra})

    def test_static_preview_rejects_coerced_bool_and_extra_write_arguments(self):
        server = create_server(SimpleNamespace())
        model = server._tool_manager.get_tool("ansa_geometry_partition_plan").fn_metadata.arg_model
        args = {"operation_id": "preview", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "deck": 1, "face_ids": [1],
            "plane": {"origin": [0., 0., 0.], "normal": [0., 0., 1.]}}
        self.assertFalse(model.model_validate(args).produce_plane_faces)
        for extra in ({"produce_plane_faces": "false"}, {"fix": True}):
            with self.assertRaises(ValidationError):
                model.model_validate({**args, **extra})

    def test_full_report_is_preserved_and_paged_without_mutation_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1,
                                    instance_id="i", session_id="s")
            rows = [{"id": i, "failure": "long diagnostic " * 20} for i in range(450)]
            report = structured_evidence.store_report(state, 1, "audit", {
                "status": "failed", "diagnostics": rows, "summary": {"failed": 450}})
            actual = []
            offset = 0
            while offset is not None:
                page = structured_evidence.read_page(state, 1, report["report_id"], "diagnostics", offset, 200)
                self.assertFalse(page["usable_for_mutation"])
                actual.extend(page["rows"])
                offset = page["next_offset"]
            self.assertEqual(actual, rows)
            nested = structured_evidence.store_report(state, 1, "audit", {
                "sections": {"geometry": {"cylinders": [{"nodes": rows}]}}})
            page = structured_evidence.read_page(state, 1, nested["report_id"],
                "/sections/geometry/cylinders/0/nodes", 200, 200)
            self.assertEqual(page["rows"], rows[200:400])
            state.model_epoch = 2
            with self.assertRaisesRegex(ValueError, "different model context"):
                structured_evidence.read_page(state, 1, report["report_id"], "diagnostics")

    def test_evidence_rejects_traversal_and_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SimpleNamespace(session_dir=Path(directory), model_epoch=1,
                                    instance_id="i", session_id="s")
            with self.assertRaises(ValueError):
                structured_evidence.read_page(state, 1, "../outside", "diagnostics")
            report = structured_evidence.store_report(state, 1, "audit", {"diagnostics": ["before"]})
            path = Path(directory) / "structured-mesh-evidence" / (report["report_id"] + ".json")
            path.write_text(path.read_text().replace('"before"', '"after"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                structured_evidence.read_page(state, 1, report["report_id"], "diagnostics")


if __name__ == "__main__":
    unittest.main()
