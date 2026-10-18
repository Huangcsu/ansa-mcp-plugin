"""Focused tests for the explicit, read-only SharpEdges operation and tool schema."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import mesh_checks
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields=None):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})

    def card_fields(self, _deck):
        return list(self.fields)


class FakeReport:
    def __init__(self, status=None, entities=(), children=(), description="sample", report_type="issue"):
        self.type = report_type
        self.description = description
        self.status = status
        self.entities = list(entities)
        self.issues = list(children)


class FakeCheckAPI:
    EXEC_ON_SELECTED = 4
    REPORT_NONE = 0
    KEEP_OLD = 8


class FakeCheck(FakeCheckAPI):
    def __init__(self, base):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_ignore_writes", set(base.ignore_writes))
        object.__setattr__(self, "angle", 30.0)
        object.__setattr__(self, "sharp_edges_type", 1)
        object.__setattr__(self, "exclude_edges", 1)
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "try_fix_calls", 0)

    def __setattr__(self, name, value):
        if name not in self.__dict__.get("_ignore_writes", set()):
            object.__setattr__(self, name, value)

    def is_available_in_deck(self, deck):
        return self._base.available

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._base.reports

    def try_fix(self):
        self.try_fix_calls += 1


class FakeState:
    def __init__(self):
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1


class FakeBase:
    Check = FakeCheckAPI

    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.get_entity_calls = []
        self.card_value_calls = []
        self.collect_calls = []
        self.grids = []
        self.collect_override = False
        self.reports = []
        self.available = True
        self.missing_options = ()
        self.ignore_writes = ()
        self.checks_created = []
        self.factory_calls = 0

        def sharp_edges():
            self.factory_calls += 1
            check = FakeCheck(self)
            for name in self.missing_options:
                delattr(check, name)
            self.checks_created.append(check)
            return check

        self.checks = types.SimpleNamespace(mesh=types.SimpleNamespace(SharpEdges=sharp_edges))

    def add(self, entity_id, entity_type="SHELL", actual_type=None, card_type=None,
            node_ids=None, register_context=True):
        fields = {}
        if entity_type == "SHELL":
            card_type = card_type or "CQUAD4"
            if node_ids is None:
                known_nodes = {
                    11: (101, 102, 103, 104),
                    12: (102, 105, 106, 103),
                }
                count = 3 if card_type == "CTRIA3" else 4
                node_ids = (known_nodes.get(entity_id) if card_type == "CQUAD4" else None)
                if node_ids is None:
                    node_ids = tuple(entity_id * 10 + index for index in range(1, count + 1))
            fields = {"type": card_type}
            fields.update({f"G{index}": node_id for index, node_id in enumerate(node_ids, 1)})
        entity = FakeEntity(entity_id, actual_type or entity_type, fields)
        self.entities[(entity_type, entity_id)] = entity
        if entity_type == "SHELL":
            for node_id in node_ids:
                key = ("GRID", node_id)
                if key not in self.entities:
                    grid = FakeEntity(node_id, "GRID")
                    self.entities[key] = grid
                    if register_context:
                        self.grids.append(grid)
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, deck, entity_type, entity_id):
        self.get_entity_calls.append((deck, entity_type, entity_id))
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        self.card_value_calls.append((deck, entity, tuple(fields)))
        values = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {field: values[field] for field in fields if field in values}

    def CollectEntities(self, deck, containers, entity_type):
        self.collect_calls.append((deck, list(containers), entity_type))
        if self.collect_override:
            return self.grids
        node_ids = []
        for shell in containers:
            node_ids.extend(value for field, value in shell.fields.items() if field.startswith("G"))
        return [self.entities[("GRID", node_id)] for node_id in dict.fromkeys(node_ids)]


class MeshChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(NASTRAN=1, OPTISTRUCT=2)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def params(self, entity_ids=(11,), **overrides):
        params = {"check": "sharp_edges", "deck": 1, "entity_ids": list(entity_ids)}
        params.update(overrides)
        return params

    def run_check(self, params=None):
        return mesh_checks.execute(self.state, "mesh_check", params or self.params())

    def prepare_scope(self):
        shell_11 = self.base.add(11)
        shell_12 = self.base.add(12)
        grid_21 = self.base.entities[("GRID", 101)]
        grid_22 = self.base.entities[("GRID", 102)]
        self.base.grids.reverse()
        return shell_11, shell_12, grid_21, grid_22

    def test_success_is_explicit_silent_history_preserving_and_never_fixes(self):
        shell_11, shell_12, grid_21, grid_22 = self.prepare_scope()
        self.base.reports = [
            FakeReport(
                status="ERROR",
                entities=[shell_11],
                children=[
                    FakeReport(status="error", entities=[shell_12, grid_21]),
                    FakeReport(status="warning", entities=[shell_11, grid_22]),
                    FakeReport(status="ok", entities=[]),
                ],
                report_type="header",
            )
        ]

        result = self.run_check(self.params((12, 11), angle=45.5))

        self.assertEqual(result["api"], "ansa.base.checks.mesh.SharpEdges")
        self.assertEqual(result["check"], "sharp_edges")
        self.assertEqual(result["entity_type"], "SHELL")
        self.assertEqual(result["entities"], [
            {"deck": 1, "type": "SHELL", "id": 12},
            {"deck": 1, "type": "SHELL", "id": 11},
        ])
        self.assertEqual(result["options"], {
            "angle": 45.5, "sharp_edges_type": 0, "exclude_edges": 0,
        })
        self.assertEqual(result["connection_context"]["node_ids"], [101, 102, 103, 104, 105, 106])
        self.assertEqual(result["connection_context"]["entities"], [
            {"deck": 1, "type": "GRID", "id": 101},
            {"deck": 1, "type": "GRID", "id": 102},
            {"deck": 1, "type": "GRID", "id": 103},
            {"deck": 1, "type": "GRID", "id": 104},
            {"deck": 1, "type": "GRID", "id": 105},
            {"deck": 1, "type": "GRID", "id": 106},
        ])
        self.assertEqual(result["scope"], "explicit")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])
        self.assertEqual(result["native_report_count"], 1)
        self.assertEqual(result["report_count"], 1)
        self.assertEqual(result["issue_counts"], {"ok": 1, "error": 1, "warning": 1})
        self.assertEqual(result["issue_count"], 3)
        self.assertEqual(result["failed_entity_ids"], [11, 12])

        call = self.base.checks_created[0].calls[0]
        self.assertEqual(call, {
            "exec_mode": FakeCheckAPI.EXEC_ON_SELECTED,
            "report": FakeCheckAPI.REPORT_NONE,
            "history": FakeCheckAPI.KEEP_OLD,
            "entities": [shell_12, shell_11],
        })
        self.assertNotIn(grid_21, call["entities"])
        self.assertNotIn(grid_22, call["entities"])
        self.assertEqual(self.base.checks_created[0].try_fix_calls, 0)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertIn((1, shell_11, ("type",)), self.base.card_value_calls)
        self.assertIn((1, shell_11, ("G1", "G2", "G3", "G4")), self.base.card_value_calls)

        self.base.reports = []
        clean_result = self.run_check(self.params((12, 11), angle=45.5))
        self.assertEqual(clean_result["reports"], [])
        self.assertEqual(clean_result["issue_count"], 0)
        self.assertEqual(clean_result["failed_entity_ids"], [])

    def test_invalid_batch_and_missing_or_wrong_shell_fail_before_factory(self):
        self.base.add(11)
        for ids in ([], [True], [0], [11, 11], list(range(1, 202))):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    self.run_check(self.params(ids))
                self.assertEqual(self.base.factory_calls, 0)

        with self.assertRaisesRegex(LookupError, "SHELL 12 does not exist"):
            self.run_check(self.params((11, 12)))
        self.assertEqual(self.base.factory_calls, 0)

        self.base.add(12, "SHELL", actual_type="GRID")
        with self.assertRaisesRegex(RuntimeError, "type does not match"):
            self.run_check(self.params((11, 12)))
        self.assertEqual(self.base.factory_calls, 0)

    def test_strict_angles_and_deck_constraints(self):
        self.base.add(11)
        for angle in (True, "120", 0, 180, float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            with self.subTest(angle=str(angle)[:20]):
                with self.assertRaisesRegex(ValueError, "angle"):
                    self.run_check(self.params(angle=angle))
                self.assertEqual(self.base.factory_calls, 0)

        for deck in (True, 0, -1):
            with self.subTest(deck=deck):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    self.run_check(self.params(deck=deck))
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "only the NASTRAN deck"):
            self.run_check(self.params(deck=2))
        self.base.deck = 1
        with self.assertRaisesRegex(ValueError, "current ANSA deck"):
            self.run_check(self.params(deck=3))
        self.assertEqual(self.base.factory_calls, 0)

    def test_capability_and_option_readback_are_required(self):
        self.base.add(11)
        self.base.missing_options = ("exclude_edges",)
        with self.assertRaisesRegex(CapabilityUnavailableError, "exclude_edges is missing"):
            self.run_check()
        self.assertEqual(self.base.factory_calls, 1)

        self.base.missing_options = ()
        self.base.ignore_writes = ("angle",)
        with self.assertRaisesRegex(CapabilityUnavailableError, "angle did not retain"):
            self.run_check()

        self.base.ignore_writes = ()
        self.base.available = False
        with self.assertRaisesRegex(CapabilityUnavailableError, "does not confirm support"):
            self.run_check()
        self.base.available = None
        with self.assertRaisesRegex(CapabilityUnavailableError, "does not confirm support"):
            self.run_check()

        self.base.available = True
        self.base.Check = None
        with self.assertRaisesRegex(CapabilityUnavailableError, "ansa.base.Check is missing"):
            self.run_check()
        self.base.Check = type("MissingModes", (), {})
        with self.assertRaisesRegex(CapabilityUnavailableError, "selected, silent"):
            self.run_check()

    def test_connection_context_shape_identity_and_limit_are_strict(self):
        self.base.add(11)
        grid = self.base.add(21, "GRID")
        complete_context = list(self.base.grids)
        incomplete_context = complete_context[:-1]
        extra_context = complete_context + [grid]
        invalid_contexts = (
            {"not": "a list"},
            [],
            incomplete_context,
            extra_context,
            [FakeEntity(22, "SHELL")],
            [grid, grid],
            [FakeEntity(index, "GRID") for index in range(1, 10001)],
        )
        for context in invalid_contexts:
            with self.subTest(context_type=type(context).__name__, size=len(context)):
                self.base.grids = context
                self.base.collect_override = True
                with self.assertRaises(RuntimeError):
                    self.run_check()
                self.assertEqual(self.base.factory_calls, 0)

        unsupported = self.base.add(13, card_type="CQUADR")
        self.base.grids = [*complete_context, *[self.base.entities[("GRID", i)]
                                                 for i in (131, 132, 133, 134)]]
        with self.assertRaisesRegex(CapabilityUnavailableError, "only CQUAD4 and CTRIA3"):
            self.run_check(self.params((13,)))
        self.assertEqual(self.base.factory_calls, 0)
        self.assertIn((1, unsupported, ("type",)), self.base.card_value_calls)

        tri_shell = self.base.add(14, card_type="CTRIA3")
        self.base.collect_override = False
        tri_result = self.run_check(self.params((14,)))
        self.assertEqual(tri_result["connection_context"]["node_ids"], [141, 142, 143])
        self.assertIn((1, tri_shell, ("G1", "G2", "G3")), self.base.card_value_calls)

    def test_reports_aggregate_shells_and_reject_invalid_or_unscoped_entities(self):
        shell_11, _shell_12, grid_21, _grid_22 = self.prepare_scope()
        invalid_reports = (
            [FakeReport(status="error", entities=[grid_21])],
            [FakeReport(status="error", entities=[shell_11, self.base.add(99, register_context=False)])],
            [FakeReport(status="error", entities=[shell_11, grid_21, self.base.add(23, "GRID")])],
            [FakeReport(status="error", entities=[shell_11, shell_11])],
            [FakeReport(status="unknown", entities=[shell_11])],
            [FakeReport(status=None, entities=[shell_11])],
            [FakeReport(status="ok", entities=[grid_21])],
            [FakeReport(status=None)],
        )
        for reports in invalid_reports:
            with self.subTest(reports=reports):
                self.base.reports = reports
                with self.assertRaises(RuntimeError):
                    self.run_check()

    def test_report_cycle_depth_and_node_bounds_remain_enforced(self):
        self.prepare_scope()
        cyclic = FakeReport(report_type="header")
        cyclic.issues.append(cyclic)
        self.base.reports = [cyclic]
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            self.run_check()

        root = FakeReport(report_type="header")
        current = root
        for _ in range(9):
            child = FakeReport(report_type="header")
            current.issues.append(child)
            current = child
        self.base.reports = [root]
        with self.assertRaisesRegex(RuntimeError, "depth"):
            self.run_check()

        self.base.reports = [FakeReport(status="ok", entities=[self.base.entities[("SHELL", 11)]])
                             for _ in range(1001)]
        with self.assertRaisesRegex(RuntimeError, "1000 nodes"):
            self.run_check()


class MeshCheckMcpSchema(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_schema_is_strict_and_submits_without_direct_ansa_calls(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.mcp.mesh_checks import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        submitted = []
        target = {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1}

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("mesh-check-schema")
        register(server, submit)
        args = {
            "operation_id": "mesh-check",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "check": "sharp_edges",
            "deck": 1,
            "entity_ids": [11, 12],
        }
        invalid = (
            {"check": "free_nodes"}, {"deck": True}, {"deck": "1"},
            {"entity_ids": []}, {"entity_ids": [True]}, {"entity_ids": [0]},
            {"entity_ids": list(range(1, 202))}, {"angle": True}, {"angle": "120"},
            {"angle": 0}, {"angle": 180}, {"angle": float("nan")},
            {"angle": float("inf")}, {"ttl_seconds": True}, {"ttl_seconds": 86401},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_mesh_check", {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_mesh_check", args)
        self.assertEqual(submitted[-1], (
            "mesh_check",
            {"check": "sharp_edges", "deck": 1, "entity_ids": [11, 12], "angle": 120.0},
            "mesh-check",
            300,
        ))
        await server.call_tool("ansa_mesh_check", {**args, "angle": 60})
        self.assertEqual(submitted[-1][1]["angle"], 60.0)


if __name__ == "__main__":
    unittest.main()
