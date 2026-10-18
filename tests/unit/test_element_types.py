"""Focused risk tests for guarded CBAR-to-CROD conversion."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import element_types
from test_entity_catalog import FakeBase, FakeState as CatalogFakeState


_BAR_DEFAULTS = {
    "PID": 100,
    "Name": "",
    "FROZEN": "NO",
    "FROZEN_ID": "NO",
    "FROZEN_DELETE": "NO",
    "Orient": "With Vector",
    "X1": 0.0,
    "X2": 1.0,
    "X3": 0.0,
    "OFFT": "",
    "PA": None,
    "PB": None,
    "W1A": None,
    "W2A": None,
    "W3A": None,
    "W1B": None,
    "W2B": None,
    "W3B": None,
}


class FakeState(CatalogFakeState):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.epoch = 1
        self.write_enabled = True
        self.generation = 1
        self.connected_backend_id = "backend"

    def check_conflict(self):
        self.assert_main()

    def probe(self):
        self.assert_main()
        return {"pid": 1, "process_start": 1, "instance_id": "instance", "session_id": "session",
                "deck": self.base.CurrentDeck(), "model": {"epoch": self.epoch, "write_enabled": self.write_enabled},
                "authorization": {"active": True, "backend_id": "backend", "generation": self.generation},
                "connected": True, "stopped": False, "persistence_blocked": False}


class ElementTypeBase(FakeBase):
    def __init__(self, current_path):
        super().__init__()
        self.database_path = Path(current_path)
        self.database_path.write_bytes(b"current model")
        self.mode = "success"
        self.change_calls = []
        self.save_calls = []
        self.fail_readback = False

        self.add("MAT1", 1, MID=1, E=210000.0, G=80000.0, NU=0.3, RHO=7.85e-9)
        self.add("PBAR", 100, MID=1, A=0.01, I1=0.0001, I2=0.0001,
                 J=0.0002, NSM=0.0, **{"Num.Elem": 4})
        self.add("PROD", 101, MID=1, A=0.01, J=0.0001, C=0.0,
                 NSM=None, **{"Num.Elem": 1})
        for node_id in (1, 11, 12, 13, 14, 15, 16, 17, 18):
            self.add("GRID", node_id, CP=0, X1=float(node_id), X2=0.0,
                     X3=0.0, CD=0)
        for entity_id, node_a, node_b in (
            (201, 11, 12), (202, 13, 14), (203, 15, 16), (204, 17, 18),
        ):
            self.add("CBAR", entity_id, **{
                **_BAR_DEFAULTS, "Name": f"bar-{entity_id}",
                "GA": node_a, "GB": node_b,
            })
        self.add("CROD", 210, PID=101, G1=11, G2=18, Name="existing",
                 FROZEN="NO", FROZEN_ID="NO", FROZEN_DELETE="NO")
        self.add("RBE2", 300, EID=300)

    def DataBaseName(self):
        return str(self.database_path)

    def SaveAs(self, path, silent=True):
        self.save_calls.append((path, silent))
        Path(path).write_bytes(b"verified backup")
        return 0

    def CollectEntities(self, deck, containers, entity_type, recursive=False,
                        filter_visible=False, hidden_entities=True):
        if self.fail_readback and self.change_calls and entity_type == "__ELEMENTS__":
            raise RuntimeError("injected FE readback failure")
        if entity_type == "__ELEMENTS__":
            excluded = {"GRID", "PBAR", "PROD", "MAT1"}
            return [entity for (kind, _), entity in self.entities.items()
                    if kind not in excluded]
        if entity_type in {"GRID", "PBAR", "PROD"}:
            return [entity for (kind, _), entity in self.entities.items()
                    if kind == entity_type]
        return super().CollectEntities(deck, containers, entity_type, recursive)

    def ChangeElemType(self, deck, elements, target_name, pid):
        self.change_calls.append({
            "deck": deck,
            "ids": [entity._id for entity in elements],
            "target_name": target_name,
            "pid": pid,
        })
        if self.mode == "raise_before":
            raise RuntimeError("injected ChangeElemType failure")

        for index, entity in enumerate(elements):
            if self.mode == "partial" and index == 1:
                break
            source = dict(entity.fields)
            self.entities.pop(("CBAR", entity._id))
            self.add(
                "CROD",
                entity._id,
                PID=pid,
                G1=source["GA"],
                G2=source["GB"],
                Name=source["Name"],
                FROZEN=source["FROZEN"],
                FROZEN_ID=source["FROZEN_ID"],
                FROZEN_DELETE=source["FROZEN_DELETE"],
            )
        if self.mode == "partial":
            raise RuntimeError("injected failure after a partial conversion")
        if self.mode == "outside":
            self.entities[("GRID", 18)].fields["X1"] += 0.5
        return 1 if self.mode == "return_1" else 0


class ElementTypeOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base = ElementTypeBase(root / "current.ansa")
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.state = FakeState(self.base)
        self.root = root

    def params(self, ids=(201,), backup_name="conversion.ansa"):
        bars = []
        for entity_id in ids:
            source = self.base.GetEntity(1, "CBAR", entity_id)
            bars.append({
                "entity_id": entity_id,
                "expected_fields": {
                    name: source.fields[name] for name in ("PID", "GA", "GB", "Name")
                },
            })
        prod = self.base.GetEntity(1, "PROD", 101)
        return {
            "deck": 1,
            "source_type": "CBAR",
            "target_type": "CROD",
            "elements": bars,
            "target_property_id": 101,
            "expected_property_fields": {
                name: prod.fields[name] for name in ("MID", "A", "J", "C", "NSM")
            },
            "backup_path": str(self.root / backup_name),
        }

    def run_operation(self, params):
        return element_types.execute(self.state, "element_change_type", params)

    def test_success_verifies_one_batch_and_returns_handle_free_json_snapshots(self):
        params = self.params((201, 202))
        result = self.run_operation(params)

        self.assertTrue(result["ok"])
        self.assertTrue(result["scope_verified"])
        self.assertTrue(result["model_scope_matches"])
        self.assertFalse(result["possible_effects"])
        self.assertEqual(result["api_return_code"], 0)
        self.assertEqual(self.base.change_calls, [{
            "deck": 1, "ids": [201, 202], "target_name": "CROD", "pid": 101,
        }])
        self.assertEqual(self.base.save_calls, [(params["backup_path"], True)])
        self.assertTrue(Path(params["backup_path"]).is_file())
        self.assertEqual(result["backup"]["path"], params["backup_path"])
        self.assertEqual(result["backup"]["saved"], True)
        self.assertEqual(
            [row["entity"]["id"] for row in result["after"]["element_cards"]["CBAR"]],
            [203, 204],
        )
        rods = {
            row["entity"]["id"]: row["fields"]
            for row in result["after"]["element_cards"]["CROD"]
        }
        self.assertEqual((rods[201]["G1"], rods[201]["G2"], rods[201]["PID"]), (11, 12, 101))
        self.assertEqual(rods[201]["Name"], "bar-201")
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertNotIn("entity_handle", json.dumps(result))

    def test_stale_later_batch_condition_rejects_before_backup_and_conversion(self):
        params = self.params((201, 202))
        params["elements"][1]["expected_fields"]["PID"] = 999

        with self.assertRaisesRegex(RuntimeError, "changed since they were read"):
            self.run_operation(params)

        self.assertEqual(self.base.change_calls, [])
        self.assertEqual(self.base.save_calls, [])
        self.assertFalse(Path(params["backup_path"]).exists())

    def test_partial_conversion_exception_keeps_backup_and_before_after_evidence(self):
        self.base.mode = "partial"
        params = self.params((201, 202))
        result = self.run_operation(params)

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["backup"]["saved"])
        self.assertTrue(result["before"]["complete"])
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(result["api_return_code"], None)
        self.assertIn("partial conversion", result["error"])
        self.assertEqual(
            [row["entity"]["id"] for row in result["after"]["element_cards"]["CROD"]],
            [201, 210],
        )
        json.dumps(result, allow_nan=False)

    def test_incomplete_post_call_readback_keeps_backup_and_marks_unknowns(self):
        self.base.fail_readback = True
        params = self.params((201,))
        result = self.run_operation(params)

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["backup"]["saved"])
        self.assertFalse(result["after"]["complete"])
        self.assertIsNone(result["after"]["fe_references"])
        self.assertIn("readback failure", result["error"])
        json.dumps(result, allow_nan=False)

    def test_out_of_scope_change_never_verifies_success(self):
        self.base.mode = "outside"
        result = self.run_operation(self.params((201,)))

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["backup"]["saved"])
        self.assertFalse(result["model_scope_matches"])
        self.assertIn("GRID identity or coordinates changed", result["error"])

    def test_model_epoch_or_write_authorization_change_during_backup_stops_conversion(self):
        original = self.base.SaveAs
        for attr, changed in (("epoch", 2), ("write_enabled", False), ("generation", 2)):
            with self.subTest(attr=attr):
                self.state.epoch, self.state.write_enabled, self.state.generation = 1, True, 1
                def save_and_change(path, silent=True):
                    status = original(path, silent)
                    setattr(self.state, attr, changed)
                    return status
                with patch.object(self.base, "SaveAs", save_and_change):
                    result = self.run_operation(self.params(backup_name=f"context-{attr}.ansa"))
                self.assertFalse(result["ok"])
                self.assertTrue(result["backup"]["verified"])
                self.assertTrue(result["possible_effects"])
                self.assertEqual(self.base.change_calls, [])

    def test_backup_failure_keeps_actual_file_and_conservative_model_effects(self):
        original = self.base.SaveAs
        original_snapshot = element_types._capture_snapshot
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                def save_then_fail(path, silent=True):
                    original(path, silent)
                    self.base.entities[("GRID", 18)].fields["X1"] += .25
                    raise RuntimeError("injected backup failure")
                calls = []
                def snapshot(base, deck):
                    value = original_snapshot(base, deck)
                    calls.append(1)
                    if incomplete and len(calls) > 1:
                        value.update(complete=False, grids=None)
                    return value
                params = self.params(backup_name=f"failed-backup-{incomplete}.ansa")
                with patch.object(self.base, "SaveAs", save_then_fail), patch.object(element_types, "_capture_snapshot", snapshot):
                    result = self.run_operation(params)
                self.assertFalse(result["ok"])
                self.assertTrue(result["possible_effects"])
                self.assertEqual(result["backup"]["path"], params["backup_path"])
                self.assertTrue(result["backup"]["exists"])
                self.assertFalse(result["backup"]["verified"])
                self.assertFalse(result["backup"]["saved"])
                self.assertEqual(self.base.change_calls, [])

    def test_transmission_budget_rejects_before_mutation_and_bounds_unexpected_growth(self):
        from ansa_mcp_shared.framing import MAX_FRAME_BYTES, send_frame
        from types import SimpleNamespace
        for node_id in range(1000, 10500):
            self.base.add("GRID", node_id, CP=0, X1=float(node_id), X2=0., X3=0., CD=0)
        with self.assertRaisesRegex(ValueError, "reply budget"):
            self.run_operation(self.params())
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.change_calls, [])
        for node_id in range(1000, 10500):
            self.base.entities.pop(("GRID", node_id))
        original = self.base.ChangeElemType
        def unexpected_growth(*args, **kwargs):
            status = original(*args, **kwargs)
            for node_id in range(1000, 10500):
                self.base.add("GRID", node_id, CP=0, X1=float(node_id), X2=0., X3=0., CD=0)
            return status
        with patch.object(self.base, "ChangeElemType", unexpected_growth):
            result = self.run_operation(self.params(backup_name="growing.ansa"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["evidence_truncated"])
        self.assertTrue(result["after"]["details_omitted"])
        self.assertTrue(result["backup"]["verified"])
        frames = []
        send_frame(SimpleNamespace(sendall=frames.append), {"ok": True, "data": {
            "task": {"tool": "element_change_type", "params": self.params((204,), "query.ansa")},
            "state": "failed", "result": result, "error": result["error"]}})
        self.assertLess(len(frames[0]) - 4, MAX_FRAME_BYTES)

    def test_large_card_precondition_is_rejected_before_backup(self):
        from ansa_mcp_shared.framing import MAX_FRAME_BYTES
        params = self.params()
        comment = "x" * 800000
        self.base.entities[("CBAR", 201)].fields["Comment"] = comment
        params["elements"][0]["expected_fields"]["Comment"] = comment
        encoded = json.dumps(params, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(encoded), MAX_FRAME_BYTES)
        with self.assertRaisesRegex(ValueError, "request exceeds the reply budget"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.change_calls, [])

    def test_nonzero_native_return_never_verifies_success(self):
        self.base.mode = "return_1"
        result = self.run_operation(self.params((201,)))

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["backup"]["saved"])
        self.assertTrue(result["model_scope_matches"])
        self.assertEqual(result["api_return_code"], 1)
        self.assertIn("exact success value int 0", result["error"])


class ElementTypeMcpSchemaTests(unittest.TestCase):
    def test_schema_rejects_extra_empty_duplicate_boolean_and_illegal_inputs(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from ansa_mcp_plugin.mcp.element_types import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        submitted = []
        target = {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1}

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, expected_target, expected_model, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("element-type-schema")
        register(server, submit)
        args = {
            "operation_id": "element-types",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **{
                "deck": 1,
                "source_type": "CBAR",
                "target_type": "CROD",
                "elements": [{
                    "entity_id": 201,
                    "expected_fields": {"PID": 100, "GA": 11, "GB": 12, "Name": "bar-201"},
                }],
                "target_property_id": 101,
                "expected_property_fields": {
                    "MID": 1, "A": 0.01, "J": 0.0001, "C": 0.0, "NSM": None,
                },
                "backup_path": str(Path(tempfile.gettempdir()) / "element-types.ansa"),
            },
        }
        invalid = (
            {"elements": []},
            {"elements": [args["elements"][0], args["elements"][0]]},
            {"elements": [{"entity_id": True, "expected_fields": {"PID": 100, "GA": 11, "GB": 12}}]},
            {"elements": [{"entity_id": 201, "expected_fields": {"PID": 100, "GA": 11}}]},
            {"elements": [{"entity_id": 201, "expected_fields": {"PID": 100, "GA": 11, "GB": 12, "Comment": "x" * 800000}}]},
            {"target_property_id": True},
            {"source_type": "SHELL"},
            {"target_type": "CBAR"},
            {"deck": True},
            {"expected_property_fields": {"MID": 1, "A": 0.01, "J": 0.1, "C": 0.0}},
            {"backup_path": "relative.ansa"},
            {"backup_path": str(Path(tempfile.gettempdir()) / "element-types.nas")},
            {"unexpected": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    asyncio.run(server.call_tool("ansa_element_change_type", {**args, **changed}))
        self.assertEqual(submitted, [])

        tool = next(
            item for item in asyncio.run(server.list_tools())
            if item.name == "ansa_element_change_type"
        )
        self.assertIs(tool.inputSchema["additionalProperties"], False)
        self.assertEqual(tool.inputSchema["properties"]["source_type"]["const"], "CBAR")
        self.assertEqual(tool.inputSchema["properties"]["target_type"]["const"], "CROD")

        asyncio.run(server.call_tool("ansa_element_change_type", args))
        self.assertEqual(len(submitted), 1)
        name, params, operation_id, submitted_target, submitted_model, ttl = submitted[0]
        self.assertEqual(name, "element_change_type")
        self.assertEqual(operation_id, "element-types")
        self.assertEqual(submitted_target.backend_id, "b")
        self.assertEqual(submitted_model.epoch, 1)
        self.assertEqual(ttl, 300)
        self.assertEqual(params, {
            "deck": 1,
            "source_type": "CBAR",
            "target_type": "CROD",
            "elements": [{
                "entity_id": 201,
                "expected_fields": {"PID": 100, "GA": 11, "GB": 12, "Name": "bar-201"},
            }],
            "target_property_id": 101,
            "expected_property_fields": {
                "MID": 1, "A": 0.01, "J": 0.0001, "C": 0.0, "NSM": None,
            },
            "backup_path": args["backup_path"],
        })


if __name__ == "__main__":
    unittest.main()
