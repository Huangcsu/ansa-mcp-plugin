"""Focused tests for the explicitly scoped, read-only NASTRAN RBE2 check."""

import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import rigid_dependencies
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError


class FakeEntity:
    def __init__(self, base, entity_id, entity_type, fields=None, usable=True):
        self.base = base
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})
        self.usable = usable

    def card_fields(self, _deck):
        self.base.events.append(("card_fields", self._id))
        return list(self.fields)

    def is_usable(self):
        return self.usable


class FakeReport:
    def __init__(self, status="ok", entities=(), issues=(), *, report_type="issue",
                 description="sample", has_fix=False, is_fixed=False):
        self.type = report_type
        self.description = description
        self.status = status
        self.has_fix = has_fix
        self.is_fixed = is_fixed
        self.entities = list(entities)
        self.issues = list(issues)


class FakeCheckAPI:
    EXEC_ON_SELECTED = 4
    REPORT_NONE = 0
    KEEP_OLD = 8


class FakeCheck(FakeCheckAPI):
    def __init__(self, base):
        self.base = base
        self.calls = []
        self.try_fix_calls = 0
        if base.options_attribute:
            self.options = {}

    def is_available_in_deck(self, _deck):
        return self.base.available

    def get_name(self):
        return self.base.check_name

    def parameters(self):
        return self.base.check_parameters

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.base.reports

    def try_fix(self):
        self.try_fix_calls += 1


class FakeState:
    def __init__(self, main=True):
        self.main = main
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1
        if not self.main:
            raise RuntimeError("not ANSA main thread")


class FakeBase:
    Check = FakeCheckAPI

    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.events = []
        self.get_entity_calls = []
        self.card_value_calls = []
        self.collect_calls = []
        self.collect_override = None
        self.reports = [FakeReport()]
        self.available = True
        self.check_name = "Dependency Nastran"
        self.check_parameters = {}
        self.options_attribute = False
        self.checks_created = []
        self.factory_calls = 0

        def nastran_dependency():
            self.factory_calls += 1
            check = FakeCheck(self)
            self.checks_created.append(check)
            return check

        self.checks = types.SimpleNamespace(
            general=types.SimpleNamespace(NastranDependency=nastran_dependency)
        )

    def add_grid(self, entity_id, *, usable=True):
        key = ("GRID", entity_id)
        if key not in self.entities:
            self.entities[key] = FakeEntity(self, entity_id, "GRID", usable=usable)
        else:
            self.entities[key].usable = usable
        return self.entities[key]

    def add_rigid(self, entity_id, gn, gm_ids, cm=123456, *, node_count=None):
        fields = {
            "EID": entity_id,
            "GN": gn,
            "CM": cm,
            "No.of.Nodes": len(gm_ids) + 1 if node_count is None else node_count,
        }
        for index, grid_id in enumerate(gm_ids, 1):
            fields[f"GM{index}"] = grid_id
        rigid = FakeEntity(self, entity_id, "RBE2", fields)
        self.entities[("RBE2", entity_id)] = rigid
        self.add_grid(gn)
        for grid_id in gm_ids:
            self.add_grid(grid_id)
        return rigid

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, deck, entity_type, entity_id):
        self.get_entity_calls.append((deck, entity_type, entity_id))
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        labels = tuple(fields)
        self.events.append(("card_values", entity._id, labels))
        self.card_value_calls.append((deck, entity._id, labels))
        values = {}
        for field in labels:
            if field == "__id__":
                values[field] = entity._id
            elif field == "__type__":
                values[field] = entity._type
            elif field in entity.fields:
                values[field] = entity.fields[field]
        return values

    def CollectEntities(self, deck, containers, entity_type):
        self.collect_calls.append((deck, list(containers), entity_type))
        if self.collect_override is not None:
            return self.collect_override
        ids = set()
        for rigid in containers:
            ids.add(rigid.fields["GN"])
            ids.update(
                rigid.fields[f"GM{index}"]
                for index in range(1, rigid.fields["No.of.Nodes"])
            )
        return [self.entities[(entity_type, entity_id)] for entity_id in sorted(ids)]


def fake_ansa(base):
    module = types.ModuleType("ansa")
    module.base = base
    module.constants = types.SimpleNamespace(NASTRAN=1)
    return module


class RigidDependenciesOperation(unittest.TestCase):
    def run_operation(self, base, rigid_ids, *, state=None, deck=1, extra=None):
        params = {"deck": deck, "rigid_ids": rigid_ids}
        if extra:
            params.update(extra)
        with patch.dict(sys.modules, {"ansa": fake_ansa(base)}):
            return rigid_dependencies.execute(
                state or FakeState(), "rigid_dependencies", params
            )

    def test_input_is_strict_and_bounded(self):
        base = FakeBase()
        base.add_rigid(2103, 1104, [1105, 1106])
        invalid = (
            (True, [2103], {}), (0, [2103], {}), (1, [], {}),
            (1, [True], {}), (1, [0], {}), (1, [2103, 2103], {}),
            (1, tuple([2103]), {}), (1, list(range(1, 202)), {}),
            (1, [2103], {"fix": True}),
        )
        for deck, rigid_ids, extra in invalid:
            with self.subTest(deck=deck, rigid_ids=rigid_ids, extra=extra):
                with self.assertRaises(ValueError):
                    self.run_operation(base, rigid_ids, deck=deck, extra=extra)
        with self.assertRaises(ValueError):
            with patch.dict(sys.modules, {"ansa": fake_ansa(base)}):
                rigid_dependencies.execute(FakeState(), "rigid_dependencies", {"deck": 1})
        self.assertEqual(base.get_entity_calls, [])
        self.assertEqual(base.factory_calls, 0)

    def test_connectivity_reads_dynamic_gm_fields_and_gates_native_execution(self):
        base = FakeBase()
        rigid = base.add_rigid(2103, 1104, [1105, 1106], 123456)
        result = self.run_operation(base, [2103])
        self.assertEqual(result["connections"][0]["independent_grid_id"], 1104)
        self.assertEqual(result["connections"][0]["dependent_grid_ids"], [1105, 1106])
        self.assertEqual(result["connections"][0]["cm_components"], [1, 2, 3, 4, 5, 6])
        self.assertLess(
            base.events.index(("card_fields", 2103)),
            base.events.index(("card_values", 2103, ("GN", "CM", "No.of.Nodes"))),
        )
        self.assertIn(("card_values", 2103, ("GM1", "GM2")), base.events)
        self.assertEqual(base.collect_calls[0][1], [rigid])

        invalid_cases = (
            ("missing GM field", lambda b, r: r.fields.pop("GM2"), CapabilityUnavailableError),
            ("repeated GM", lambda _b, r: r.fields.__setitem__("GM2", 1105), RuntimeError),
            ("invalid CM", lambda _b, r: r.fields.__setitem__("CM", 321), RuntimeError),
            ("node count limit", lambda _b, r: r.fields.__setitem__("No.of.Nodes", 102), RuntimeError),
        )
        for label, change, error in invalid_cases:
            with self.subTest(label=label):
                case_base = FakeBase()
                case_rigid = case_base.add_rigid(2103, 1104, [1105, 1106])
                change(case_base, case_rigid)
                with self.assertRaises(error):
                    self.run_operation(case_base, [2103])
                self.assertEqual(case_base.factory_calls, 0)

        mismatched = FakeBase()
        mismatched.add_rigid(2103, 1104, [1105, 1106])
        mismatched.collect_override = []
        with self.assertRaisesRegex(RuntimeError, "exactly match"):
            self.run_operation(mismatched, [2103])
        self.assertEqual(mismatched.factory_calls, 0)

        unusable = FakeBase()
        unusable.add_rigid(2103, 1104, [1105, 1106])
        unusable.add_grid(1106, usable=False)
        with self.assertRaisesRegex(RuntimeError, "not confirmed usable"):
            self.run_operation(unusable, [2103])
        self.assertEqual(unusable.factory_calls, 0)

    def test_fixed_factory_metadata_and_selected_native_options(self):
        base = FakeBase()
        base.add_rigid(2103, 1104, [1105, 1106])
        result = self.run_operation(base, [2103])
        self.assertEqual(base.factory_calls, 1)
        self.assertEqual(result["native_check"], {
            "name": "Dependency Nastran",
            "factory": "base.checks.general.NastranDependency",
            "available_in_deck": True,
            "parameters": {},
            "options_attribute": False,
        })
        self.assertEqual(result["native_options"], {
            "exec_mode": "EXEC_ON_SELECTED",
            "report": "REPORT_NONE",
            "history": "KEEP_OLD",
        })
        self.assertEqual(base.checks_created[0].calls, [{
            "exec_mode": FakeCheckAPI.EXEC_ON_SELECTED,
            "report": FakeCheckAPI.REPORT_NONE,
            "history": FakeCheckAPI.KEEP_OLD,
            "entities": [base.entities[("RBE2", 2103)]],
        }])

        invalid_metadata = (
            ("unavailable", "available", False),
            ("wrong check name", "check_name", "Other"),
            ("non-empty parameters", "check_parameters", {"option": True}),
            ("unexpected options attribute", "options_attribute", True),
        )
        for label, attribute, value in invalid_metadata:
            with self.subTest(label=label):
                case_base = FakeBase()
                case_base.add_rigid(2103, 1104, [1105, 1106])
                setattr(case_base, attribute, value)
                with self.assertRaises(CapabilityUnavailableError):
                    self.run_operation(case_base, [2103])
                self.assertEqual(case_base.checks_created[0].calls, [])

    def test_report_tree_counts_only_leaves_and_deduplicates_affected_ids(self):
        base = FakeBase()
        rigid_a = base.add_rigid(2001, 1001, [1003])
        rigid_b = base.add_rigid(2002, 1002, [1003])
        shared_grid = base.entities[("GRID", 1003)]
        leaves = [
            FakeReport("error", [shared_grid], report_type="Error", description=f"dof {dof}")
            for dof in range(1, 7)
        ]
        header = FakeReport(
            "error", [rigid_b, rigid_a], leaves,
            report_type="Error", description="Chain Dependency Error", has_fix=True,
        )
        base.reports = [FakeReport("error", issues=[header], report_type="Dependency Nastran")]
        result = self.run_operation(base, [2001, 2002])

        self.assertEqual(result["stats"], {
            "error": 6, "warning": 0, "ok": 0, "issue_count": 6, "report_count": 8,
        })
        self.assertEqual(result["count"], 6)
        self.assertEqual(result["count_basis"], "error_warning_leaf_reports_not_entity_count")
        self.assertEqual(result["affected_rigid_ids"], [2001, 2002])
        self.assertEqual(result["affected_grid_ids"], [1003])
        self.assertEqual(result["reports"][0]["issues"][0]["entities"], [
            {"deck": 1, "type": "RBE2", "id": 2002},
            {"deck": 1, "type": "RBE2", "id": 2001},
        ])
        self.assertEqual(len(result["reports"][0]["issues"][0]["issues"]), 6)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])
        self.assertEqual(base.checks_created[0].try_fix_calls, 0)

    def test_temporary_report_wrappers_are_not_mistaken_for_reused_objects(self):
        base = FakeBase()
        rigid_a = base.add_rigid(2001, 1001, [1003])
        rigid_b = base.add_rigid(2002, 1002, [1003])
        grid = base.entities[("GRID", 1003)]

        class Wrapper:
            type = "Error"
            description = "temporary native wrapper"
            status = "error"
            has_fix = False
            is_fixed = False

            def __init__(self, depth):
                self.depth = depth
                self.entities = [] if depth == 0 else [rigid_a, rigid_b]

            @property
            def issues(self):
                if self.depth == 0:
                    return [Wrapper(1) for _ in range(3)]
                return [FakeReport("error", [grid], description=f"dof {dof}")
                        for dof in range(1, 7)]

        base.reports = [Wrapper(0)]
        result = self.run_operation(base, [2001, 2002])
        self.assertEqual(result["count"], 18)
        self.assertEqual(result["stats"]["report_count"], 22)
        self.assertEqual(result["affected_rigid_ids"], [2001, 2002])
        self.assertEqual(result["affected_grid_ids"], [1003])

    def test_ok_root_with_empty_issues_returns_zero_for_explicit_scope(self):
        base = FakeBase()
        base.add_rigid(2103, 1104, [1105, 1106])
        before = {key: dict(entity.fields) for key, entity in base.entities.items()}
        base.reports = [FakeReport("ok", report_type="Dependency Nastran")]

        result = self.run_operation(base, [2103])

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["stats"], {
            "error": 0, "warning": 0, "ok": 1, "issue_count": 0, "report_count": 1,
        })
        self.assertEqual(result["affected_rigid_ids"], [])
        self.assertEqual(result["affected_grid_ids"], [])
        self.assertEqual(result["scope"], {"rigid_ids": [2103], "grid_ids": [1104, 1105, 1106]})
        self.assertIn("explicitly selected RBE2 IDs", result["scope_note"])
        self.assertEqual(before, {key: dict(entity.fields) for key, entity in base.entities.items()})

    def test_report_tree_rejects_unknown_malformed_out_of_scope_and_unbounded_results(self):
        def run_with(report_builder, *, rigid_count=1):
            base = FakeBase()
            rigids = []
            for index in range(rigid_count):
                rigid_id = 2100 + index
                gn = 1100
                gm = 1200 + index
                rigids.append(base.add_rigid(rigid_id, gn, [gm]))
            base.reports = report_builder(base, rigids)
            return self.run_operation(base, [2100 + index for index in range(rigid_count)])

        invalid_results = (
            ("unknown status", lambda _b, _r: [FakeReport("pending")], 1, "unknown status"),
            ("non-boolean has_fix", lambda _b, _r: [FakeReport(has_fix=1)], 1, "must be booleans"),
            ("already fixed", lambda _b, _r: [FakeReport(is_fixed=True)], 1, "is_fixed is true"),
            ("out-of-scope entity", lambda b, _r: [FakeReport(
                "error", [b.add_grid(9999)])], 1, "outside the explicit"),
            ("cycle", lambda _b, _r: self._cycle_report(), 1, "contains a cycle"),
            ("depth", lambda _b, _r: self._deep_reports(13), 1, "exceeds depth 12"),
            ("node bound", lambda _b, _r: [FakeReport() for _ in range(201)], 1, "exceeds 200 nodes"),
            ("reference bound", lambda _b, rigids: [
                FakeReport("ok", rigids) for _ in range(6)
            ], 200, "exceeds 1000 entity references"),
        )
        for label, builder, rigid_count, message in invalid_results:
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, message):
                    run_with(builder, rigid_count=rigid_count)

    @staticmethod
    def _cycle_report():
        root = FakeReport("error")
        root.issues.append(root)
        return [root]

    @staticmethod
    def _deep_reports(depth):
        root = current = FakeReport()
        for _ in range(depth - 1):
            child = FakeReport()
            current.issues.append(child)
            current = child
        return [root]


class RigidDependenciesDispatchAndSchema(unittest.IsolatedAsyncioTestCase):
    async def test_main_thread_dispatch_and_mcp_read_only_schema(self):
        from ansa_mcp_bridge.operations import execute as dispatch
        from ansa_mcp_plugin.__main__ import create_server
        from ansa_mcp_plugin.mcp.rigid_dependencies import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult
        from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        base = FakeBase()
        base.add_rigid(2103, 1104, [1105, 1106])
        state = FakeState()
        with patch.dict(sys.modules, {"ansa": fake_ansa(base)}):
            dispatched = dispatch(state, "rigid_dependencies", {"deck": 1, "rigid_ids": [2103]})
        self.assertEqual(dispatched["scope"]["rigid_ids"], [2103])
        self.assertEqual(state.main_calls, 2)
        self.assertIn("rigid_dependencies", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("rigid_dependencies"))
        registered_server = create_server(object())
        self.assertIsNotNone(
            registered_server._tool_manager.get_tool("ansa_rigid_dependencies")
        )
        with self.assertRaisesRegex(RuntimeError, "not ANSA main thread"):
            rigid_dependencies.execute(
                FakeState(main=False), "rigid_dependencies", {"deck": 1, "rigid_ids": [2103]}
            )

        submitted = []
        target = {"backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1}

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True, status="queued", operation_id=operation_id,
                target=expected_target, model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("rigid-dependency-schema")
        register(server, submit)
        schema = server._tool_manager.get_tool("ansa_rigid_dependencies").parameters
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {
            "operation_id", "expected_target", "expected_model", "deck", "rigid_ids", "ttl_seconds",
        })
        args = {
            "operation_id": "rigid-check",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "rigid_ids": [2103],
        }
        for changed in (
            {"deck": True}, {"deck": "1"}, {"rigid_ids": []},
            {"rigid_ids": [True]}, {"rigid_ids": [0]},
            {"rigid_ids": [2103, 2103]}, {"rigid_ids": list(range(1, 202))},
            {"fix": True}, {"ttl_seconds": True}, {"ttl_seconds": 86401},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_rigid_dependencies", {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_rigid_dependencies", args)
        self.assertEqual(submitted, [
            ("rigid_dependencies", {"deck": 1, "rigid_ids": [2103]}, "rigid-check", 300)
        ])


if __name__ == "__main__":
    unittest.main()
