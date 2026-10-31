from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from ansa_mcp_bridge.operations import map_block_caps as caps
from ansa_mcp_bridge.operations import mesh_provenance, structured_meshing
import ansa_mcp_bridge.operations as bridge_operations
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import writes_model


def _capture(target_caps, target_cap_fields, *, settings_sha="settings"):
    target_fields = {
        "Type": "Map Block",
        "State": "Ready to Mesh",
        "Status": "Unmeshed",
        "Frozen": "NO",
        **target_cap_fields,
    }
    other_fields = {
        "Type": "Map Block",
        "State": "Ready to Mesh",
        "Status": "Unmeshed",
        "Frozen": "NO",
        "Cap1_FACE_list": [11],
        "Cap2_FACE_list": [14],
    }
    scope = {
        "deck": 1,
        "entities": [
            {
                "entity": {"type": "VOLUME", "id": 10},
                "card_fields": ["Type", "State", "Status", "Frozen", "Cap1_FACE_list", "Cap2_FACE_list"],
                "fields": target_fields,
                "part_id": 9,
                "solid_ids": [],
            },
            {
                "entity": {"type": "VOLUME", "id": 11},
                "card_fields": ["Type", "State", "Status", "Frozen", "Cap1_FACE_list", "Cap2_FACE_list"],
                "fields": other_fields,
                "part_id": 9,
                "solid_ids": [],
            },
            {
                "entity": {"type": "GRID", "id": 1},
                "card_fields": ["NID", "X1", "X2", "X3"],
                "fields": {"NID": 1, "X1": 0.0, "X2": 0.0, "X3": 0.0},
            },
        ],
        "grids": [{"id": 1, "xyz": [0.0, 0.0, 0.0]}],
    }
    volumes = [
        {
            "volume_id": 10,
            "card": {"fields": target_fields},
            "part_id": 9,
            "face_ids": [1, 2, 3, 4, 5, 6],
            "caps": target_caps,
            "caps_match_complete_boundary": True,
            "native_mapping_status": "native_ready",
        },
        {
            "volume_id": 11,
            "card": {"fields": other_fields},
            "part_id": 9,
            "face_ids": [11, 12, 13, 14, 15, 16],
            "caps": {"master": [11], "slave": [14], "round": [12, 13, 15, 16]},
            "caps_match_complete_boundary": True,
            "native_mapping_status": "native_ready",
        },
    ]
    return {
        "context": {"model_epoch": 3},
        "model_context": {"units": "millimeter", "database": "fixture.ansa"},
        "scope": scope,
        "blocks": {
            "volumes": volumes,
            "source_snapshot_sha256": "block-snapshot",
            "face_membership": [],
            "surface_mesh": {"elements": [], "nodes": []},
            "cad_witness": {"source_snapshot_sha256": "cad"},
        },
        "settings": {"snapshot_sha256": settings_sha},
    }


class MapBlockCapsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            self.calls.append((tool, params))
            return {"state": "queued", "operation_id": operation_id}

        self.tool = create_server(SimpleNamespace(submit_task=submit))._tool_manager.get_tool(
            "ansa_mesh_map_block_caps"
        )
        self.model = self.tool.fn_metadata.arg_model
        self.common = {
            "operation_id": "caps",
            "expected_target": {
                "backend_id": "b",
                "instance_id": "i",
                "session_id": "s",
                "generation": 1,
            },
            "expected_model": {"epoch": 3},
            "deck": 1,
            "volume_id": 10,
            "master_face_ids": [1, 2],
            "slave_face_ids": [4],
        }

    def _invoke(self, delta):
        request = self.model.model_validate({**self.common, **delta})
        fields = request.model_dump()
        fields.update(expected_target=request.expected_target, expected_model=request.expected_model)
        return self.tool.fn(**fields)

    def test_plan_apply_schema_dispatch_and_classification(self):
        self._invoke({"action": "plan"})
        self.assertEqual(
            self.calls[-1],
            (
                "mesh_map_block_caps_plan",
                {
                    "deck": 1,
                    "volume_id": 10,
                    "master_face_ids": [1, 2],
                    "slave_face_ids": [4],
                },
            ),
        )
        backup = str(Path.cwd() / "caps-backup.ansa")
        self._invoke({
            "action": "apply",
            "expected_plan_sha256": "a" * 64,
            "backup_path": backup,
        })
        self.assertEqual(self.calls[-1][0], "mesh_map_block_caps_apply")
        self.assertEqual(self.calls[-1][1]["backup_path"], backup)
        self.assertFalse(writes_model("mesh_map_block_caps_plan"))
        self.assertTrue(writes_model("mesh_map_block_caps_apply"))

        for delta in (
            {"action": "apply"},
            {"action": "plan", "expected_plan_sha256": "a" * 64},
            {"action": "plan", "master_face_ids": [1], "slave_face_ids": [1]},
        ):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self._invoke(delta)
        for delta in (
            {"master_face_ids": [1, 1]},
            {"volume_id": True},
            {"unexpected": True},
        ):
            with self.subTest(delta=delta), self.assertRaises(ValidationError):
                self.model.model_validate({**self.common, "action": "plan", **delta})

    def test_native_none_return_is_accepted_only_after_independent_readback(self):
        old = _capture(
            {"master": [1], "slave": [4], "round": [2, 3, 5, 6]},
            {"Cap1_FACE_list": [1], "Cap2_FACE_list": [4]},
        )
        expected_caps = {"master": [1, 2], "slave": [4], "round": [3, 5, 6]}
        after = _capture(
            expected_caps,
            {"Cap1_FACE_list": [1, 2], "Cap2_FACE_list": [4]},
        )
        clean = {
            "deck": 1,
            "volume_id": 10,
            "master_face_ids": [1, 2],
            "slave_face_ids": [4],
        }
        prepared = {
            "clean": clean,
            "context": old["context"],
            "capture": old,
            "target": old["blocks"]["volumes"][0],
            "caps_requested": expected_caps,
            "plan_sha256": "b" * 64,
        }
        params = {
            **clean,
            "expected_plan_sha256": "b" * 64,
            "backup_path": str(Path.cwd() / "caps-backup.ansa"),
        }
        mesh = SimpleNamespace(DefineCaps=Mock(return_value=None))
        with (
            patch.object(caps.native, "_backup_preflight", return_value=("backup", "model", "sha")),
            patch.object(caps.native, "_verify_backup", return_value={"verified": True}),
            patch.object(caps, "preflight", return_value=prepared),
            patch.object(caps.guards, "_same_context"),
            patch.object(caps.features, "_entity", side_effect=lambda *args: object()),
            patch.object(caps, "_capture", return_value=after),
        ):
            result = caps.apply_prepared(
                SimpleNamespace(assert_main=Mock()), Mock(), mesh, SimpleNamespace(NASTRAN=1),
                params, prepared,
            )

        self.assertEqual(mesh.DefineCaps.call_count, 1)
        self.assertIsNone(result["native_return"])
        self.assertEqual(result["native_return_type"], "NoneType")
        self.assertTrue(result["ok"])
        self.assertTrue(result["scope_verified"])
        self.assertFalse(result["recovery_required"])

    def test_any_existing_solid_is_rejected_before_caps_write(self):
        captured = _capture(
            {"master": [1], "slave": [4], "round": [2, 3, 5, 6]},
            {"Cap1_FACE_list": [1], "Cap2_FACE_list": [4]},
        )
        captured["scope"]["entities"].append(
            {"entity": {"type": "SOLID", "id": 90}, "fields": {"PID": 1}}
        )
        mesh = SimpleNamespace(
            DefineCaps=Mock(), GetVolumeBoundary=Mock(), GetMapVolumeBoundaries=Mock()
        )
        params = {
            "deck": 1,
            "volume_id": 10,
            "master_face_ids": [1, 2],
            "slave_face_ids": [4],
        }
        with (
            patch.object(caps.inspection, "_deck", return_value=1),
            patch.object(caps.native, "_check_nastran"),
            patch.object(caps, "_capture", return_value=captured),
            self.assertRaisesRegex(ValueError, "no existing SOLID"),
        ):
            caps.preflight(
                SimpleNamespace(assert_main=Mock()), Mock(), mesh,
                SimpleNamespace(NASTRAN=1), params,
            )
        mesh.DefineCaps.assert_not_called()

    def test_scope_change_outside_target_caps_fails_closed(self):
        before = _capture(
            {"master": [1], "slave": [4], "round": [2, 3, 5, 6]},
            {"Cap1_FACE_list": [1], "Cap2_FACE_list": [4]},
        )
        changed = deepcopy(before)
        changed["scope"]["grids"][0]["xyz"][0] = 0.1
        with self.assertRaisesRegex(RuntimeError, "snapshot section changed"):
            caps._assert_scope_unchanged_except_target_caps(
                before["scope"], changed["scope"], 10
            )

    def test_target_round_must_equal_original_boundary_remainder(self):
        before = _capture(
            {"master": [1], "slave": [4], "round": [2, 3, 5, 6]},
            {"Cap1_FACE_list": [1], "Cap2_FACE_list": [4]},
        )
        wrong = _capture(
            {"master": [1, 2], "slave": [4], "round": [3, 5]},
            {"Cap1_FACE_list": [1, 2], "Cap2_FACE_list": [4]},
        )
        prepared = {
            "clean": {"deck": 1, "volume_id": 10},
            "capture": before,
            "caps_requested": {"master": [1, 2], "slave": [4], "round": [3, 5, 6]},
        }
        with self.assertRaisesRegex(RuntimeError, "GetMapVolumeBoundaries"):
            caps._assert_postconditions(prepared, wrong)

    def test_bridge_registry_routes_caps_plan_and_apply(self):
        state = SimpleNamespace(assert_main=Mock())
        for tool in ("mesh_map_block_caps_plan", "mesh_map_block_caps_apply"):
            with self.subTest(tool=tool), patch.object(
                structured_meshing, "execute", return_value=tool
            ) as execute:
                self.assertEqual(
                    bridge_operations.execute(state, tool, {"deck": 1}),
                    tool,
                )
                execute.assert_called_once_with(state, tool, {"deck": 1})

    def test_apply_uses_the_existing_durable_operation_ledger(self):
        from types import ModuleType

        fake_ansa = ModuleType("ansa")
        fake_ansa.base = Mock()
        fake_ansa.constants = SimpleNamespace()
        fake_ansa.mesh = Mock()
        state = SimpleNamespace(model_epoch=3, session_dir="session")
        params = {
            "deck": 1,
            "volume_id": 10,
            "master_face_ids": [1, 2],
            "slave_face_ids": [4],
            "expected_plan_sha256": "c" * 64,
            "backup_path": str(Path.cwd() / "caps-backup.ansa"),
        }
        prepared = {"plan_sha256": params["expected_plan_sha256"]}
        stages = []

        def record_stage(_session, _ledger_id, payload, _expected_hash):
            stages.append(payload)
            return {"ledger_hash": "hash-" + str(len(stages))}

        with (
            patch.dict("sys.modules", {"ansa": fake_ansa}),
            patch.object(caps, "preflight", return_value=prepared),
            patch.object(structured_meshing, "_binding", return_value={"model": {"epoch": 3}}) as binding,
            patch.object(mesh_provenance, "create_ledger", return_value={"ledger_hash": "hash-0"}),
            patch.object(mesh_provenance, "record_stage", side_effect=record_stage),
            patch.object(
                caps, "apply_prepared", return_value={"ok": True, "scope_verified": True}
            ) as apply,
            patch.object(
                structured_meshing, "_bounded_operation_result", side_effect=lambda _s, _d, result: result
            ),
        ):
            result = structured_meshing._with_operation_ledger(
                state, 1, "mesh_map_block_caps_apply", params
            )

        self.assertEqual(binding.call_args.args[2]["type"], "VOLUME_SCOPE")
        apply.assert_called_once_with(
            state, fake_ansa.base, fake_ansa.mesh, fake_ansa.constants, params, prepared
        )
        self.assertEqual(stages[-1]["stage"], "verified")
        self.assertEqual(result["provenance_stage"], "verified")


if __name__ == "__main__":
    unittest.main()
