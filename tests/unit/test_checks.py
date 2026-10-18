"""Focused validation for the controlled FreeNodes check operation."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import checks
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError


class FakeEntity:
    def __init__(self, entity_id, entity_type="GRID"):
        self._id = entity_id
        self._type = entity_type


class FakeCheckReport:
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
    def __init__(self, reports, *, available=True, missing_options=()):
        self._reports = reports
        self._available = available
        self.calls = []
        self.try_fix_calls = 0
        values = {
            "consider_nodes_with_mass_free": False,
            "do_not_report_free_nodes_attached_to_non_visible_entities": True,
            "do_not_report_master_nodes_of_rigid_bodies": False,
            "consider_auxiliary_nodes_as_free": False,
            "report_scalar_nodes": True,
            "consider_nodes_with_load_free": False,
        }
        for name, value in values.items():
            if name not in missing_options:
                setattr(self, name, value)

    def is_available_in_deck(self, deck):
        return self._available

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._reports

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
        self.reports = []
        self.available = True
        self.missing_options = ()
        self.checks_created = []
        self.factory_calls = 0
        self.connections = {}
        self.context_nodes = []

        def free_nodes():
            self.factory_calls += 1
            check = FakeCheck(
                self.reports,
                available=self.available,
                missing_options=self.missing_options,
            )
            self.checks_created.append(check)
            return check

        self.checks = types.SimpleNamespace(
            general=types.SimpleNamespace(FreeNodes=free_nodes),
        )

    def add(self, entity_id, entity_type="GRID"):
        entity = FakeEntity(entity_id, entity_type)
        self.entities[("GRID", entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, deck, entity_type, entity_id):
        self.get_entity_calls.append((deck, entity_type, entity_id))
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type}
        return {field: values[field] for field in fields if field in values}

    def NodesToElements(self, nodes, sources):
        assert sources == ["entities", "bcs"]
        return {node: self.connections.get(node._id, []) for node in nodes}

    def CollectEntities(self, deck, containers, entity_type):
        assert entity_type == "GRID" and containers
        return self.context_nodes


class ControlledFreeNodes(unittest.TestCase):
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

    def params(self, entity_ids=(11,)):
        return {"deck": 1, "check": "free_nodes", "entity_ids": list(entity_ids)}

    def run_check(self, params=None):
        if params is None:
            params = self.params()
        return checks.execute(self.state, "check_run", params)

    def test_nested_reports_count_only_leaf_issues_and_never_fix(self):
        grid_11 = self.base.add(11)
        grid_12 = self.base.add(12)
        self.base.reports = [
            FakeCheckReport(
                status="ERROR",
                entities=[grid_12],
                children=[
                    FakeCheckReport(status="Error", entities=[grid_11]),
                    FakeCheckReport(status="WARNING", entities=[grid_11, grid_12]),
                    FakeCheckReport(status="ok", entities=[]),
                ],
                report_type="header",
            ),
        ]

        result = self.run_check(self.params((11, 12)))

        self.assertEqual(result["api"], "ansa.base.checks.general.FreeNodes")
        self.assertEqual(result["entities"], [
            {"deck": 1, "type": "GRID", "id": 11},
            {"deck": 1, "type": "GRID", "id": 12},
        ])
        self.assertEqual(result["scope"], "explicit")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])
        self.assertEqual(result["report_count"], 1)
        self.assertEqual(result["issue_count"], 3)
        self.assertEqual(result["issue_counts"], {"ok": 1, "error": 1, "warning": 1})
        self.assertEqual(result["failed_entity_ids"], [11, 12])
        self.assertEqual(result["reports"][0]["status"], "error")
        self.assertEqual(len(result["reports"][0]["children"]), 3)
        self.assertEqual(result["options"], {
            "consider_nodes_with_mass_free": True,
            "do_not_report_free_nodes_attached_to_non_visible_entities": False,
            "do_not_report_master_nodes_of_rigid_bodies": True,
            "consider_auxiliary_nodes_as_free": True,
            "report_scalar_nodes": False,
            "consider_nodes_with_load_free": True,
        })
        self.assertEqual(self.base.checks_created[0].calls, [{
            "exec_mode": FakeCheckAPI.EXEC_ON_SELECTED,
            "report": FakeCheckAPI.REPORT_NONE,
            "history": FakeCheckAPI.KEEP_OLD,
            "entities": [grid_11, grid_12],
        }])
        self.assertEqual(self.base.checks_created[0].try_fix_calls, 0)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_empty_report_list_is_a_valid_clean_result(self):
        self.base.add(11)
        params = self.params()
        params["consider_nodes_with_mass_free"] = False

        result = self.run_check(params)

        self.assertEqual(result["reports"], [])
        self.assertEqual(result["report_count"], 0)
        self.assertEqual(result["issue_count"], 0)
        self.assertEqual(result["issue_counts"], {"ok": 0, "error": 0, "warning": 0})
        self.assertEqual(result["failed_entity_ids"], [])
        self.assertFalse(result["options"]["consider_nodes_with_mass_free"])
        self.assertFalse(self.base.checks_created[0].consider_nodes_with_mass_free)

        self.base.reports = [FakeCheckReport(status=None, report_type="header")]
        header_result = self.run_check()
        self.assertEqual(header_result["report_count"], 1)
        self.assertEqual(header_result["issue_count"], 0)
        self.assertIsNone(header_result["reports"][0]["status"])

    def test_connected_context_mixed_mass_report_and_neighbor_filtering(self):
        grid_11, grid_12, neighbor = (self.base.add(i) for i in (11, 12, 13))
        shell, mass = FakeEntity(21, "SHELL"), FakeEntity(22, "CONM2")
        self.base.connections = {11: [shell], 12: [mass]}
        self.base.context_nodes = [grid_11, grid_12, neighbor]
        self.base.reports = [FakeCheckReport(status="warning", report_type="header", children=[
            FakeCheckReport(status="warning", entities=[grid_12, mass]),
            FakeCheckReport(status="warning", entities=[neighbor]),
            FakeCheckReport(status="warning", entities=[grid_11, neighbor]),
        ])]
        result = self.run_check(self.params((11, 12)))
        self.assertEqual(self.base.checks_created[0].calls[0]["entities"],
                         [grid_11, grid_12, mass, shell, neighbor])
        self.assertEqual(result["connection_context"]["node_ids"], [11, 12, 13])
        self.assertEqual(result["issue_counts"], {"ok": 0, "error": 0, "warning": 2})
        self.assertEqual(result["failed_entity_ids"], [11, 12])
        leaves = result["reports"][0]["children"]
        self.assertEqual(leaves[0]["entities"][-1], {"deck": 1, "type": "CONM2", "id": 22})
        self.assertEqual(leaves[1]["entities"], [{"deck": 1, "type": "GRID", "id": 11}])
        self.assertTrue(leaves[1]["scope_filtered"])
        self.base.reports = [FakeCheckReport(status="warning", report_type="header", children=[
            FakeCheckReport(status="warning", entities=[neighbor])])]
        filtered = self.run_check(self.params((11, 12)))
        self.assertEqual(filtered["reports"], [])
        self.assertEqual(filtered["report_count"], 0)
        self.assertEqual(filtered["native_report_count"], 1)
        self.assertEqual(filtered["issue_count"], 0)
        for status in (None, ""):
            self.base.reports = [FakeCheckReport(status=status, entities=[neighbor])]
            with self.subTest(missing_neighbor_status=status):
                with self.assertRaisesRegex(RuntimeError, "entities but no status"):
                    self.run_check(self.params((11, 12)))
        missing_status = FakeCheckReport(entities=[neighbor])
        del missing_status.status
        self.base.reports = [missing_status]
        with self.assertRaisesRegex(RuntimeError, "entities but no status"):
            self.run_check(self.params((11, 12)))
        self.base.reports = [FakeCheckReport(status="warning", entities=[mass])]
        with self.assertRaisesRegex(RuntimeError, "no GRID nodes"):
            self.run_check(self.params((11, 12)))

    def test_incomplete_or_excessive_connection_context_fails_before_check_execution(self):
        node = self.base.add(11)
        for result in (None, {}, {node: None}, {FakeEntity(99): []},
                       {node: [FakeEntity(i + 1, "SHELL") for i in range(10001)]}):
            with self.subTest(result_type=type(result).__name__):
                with patch.object(self.base, "NodesToElements", return_value=result):
                    with self.assertRaises(RuntimeError):
                        self.run_check()
        with patch.object(self.base, "NodesToElements", None):
            with self.assertRaises(CapabilityUnavailableError):
                self.run_check()
        self.base.connections = {11: [FakeEntity(22, "CONM2")]}
        self.base.context_nodes = None
        with self.assertRaisesRegex(RuntimeError, "invalid or excessive"):
            self.run_check()
        for context_nodes in ([], [FakeEntity(99)]):
            self.base.context_nodes = context_nodes
            with self.subTest(missing_attached_node=context_nodes):
                with self.assertRaisesRegex(RuntimeError, "omitted attached GRID nodes"):
                    self.run_check()
        self.assertEqual(self.base.factory_calls, 0)

    def test_invalid_parameters_and_entity_batch_fail_before_factory(self):
        self.base.add(11)
        invalid = [
            {},
            {**self.params(), "check": "Free_Nodes"},
            {**self.params(), "consider_nodes_with_mass_free": 1},
            {**self.params(), "deck": True},
            {**self.params(), "deck": 0},
            {**self.params(), "entity_ids": [True]},
            {**self.params(), "entity_ids": [11, 11]},
            {**self.params(), "entity_ids": []},
            {**self.params(), "entity_ids": list(range(1, 202))},
            {**self.params(), "unexpected": True},
        ]
        for params in invalid:
            with self.subTest(params=params):
                with self.assertRaises((TypeError, ValueError)):
                    self.run_check(params)
        self.assertEqual(self.base.factory_calls, 0)

        with self.assertRaises(LookupError):
            self.run_check(self.params((11, 12)))
        self.assertEqual(self.base.get_entity_calls[-2:], [(1, "GRID", 11), (1, "GRID", 12)])
        self.assertEqual(self.base.factory_calls, 0)
        self.assertEqual(self.base.checks_created, [])

    def test_api_gaps_and_unsupported_deck_are_explicit(self):
        self.base.add(11)
        self.base.checks.general.FreeNodes = None
        with self.assertRaises(CapabilityUnavailableError):
            self.run_check()

        self.base.checks.general.FreeNodes = lambda: FakeCheck([])
        self.base.GetEntityCardValues = None
        with self.assertRaises(CapabilityUnavailableError):
            self.run_check()

        self.base.GetEntityCardValues = FakeBase.GetEntityCardValues.__get__(self.base)
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "only the NASTRAN"):
            self.run_check({"deck": 2, "check": "free_nodes", "entity_ids": [11]})
        self.assertEqual(self.base.factory_calls, 0)

        self.base.deck = 1
        self.base.checks.general.FreeNodes = lambda: FakeCheck([], available=False)
        with self.assertRaises(CapabilityUnavailableError):
            self.run_check()

        self.base.checks.general.FreeNodes = lambda: FakeCheck([], available=1)
        with self.assertRaises(CapabilityUnavailableError):
            self.run_check()

        self.base.checks.general.FreeNodes = lambda: FakeCheck([], missing_options=(
            "report_scalar_nodes",
        ))
        with self.assertRaises(CapabilityUnavailableError):
            self.run_check()

    def test_invalid_report_shapes_scope_status_and_issue_entities_fail(self):
        self.base.add(11)
        invalid_reports = [
            None,
            (),
            {"reports": []},
            [FakeCheckReport(status="unknown")],
            [FakeCheckReport(status=None, entities=[self.base.add(12)])],
            [FakeCheckReport(status="error", entities=[FakeEntity(99)])],
            [FakeCheckReport(status="error", entities=[FakeEntity(11, "SHELL")])],
            [FakeCheckReport(status="error", entities=[self.base.add(11), self.base.add(11)])],
        ]
        missing_description = FakeCheckReport(status="ok")
        del missing_description.description
        invalid_reports.append([missing_description])
        for reports in invalid_reports:
            with self.subTest(reports=reports):
                self.base.reports = reports
                with self.assertRaises(RuntimeError):
                    self.run_check()

    def test_report_tree_cycles_depth_and_size_limits_fail(self):
        self.base.add(11)
        cyclic = FakeCheckReport(status=None, report_type="header")
        cyclic.issues.append(cyclic)
        self.base.reports = [cyclic]
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            self.run_check()

        root = FakeCheckReport(status=None, report_type="header")
        current = root
        for _ in range(9):
            child = FakeCheckReport(status=None, report_type="header")
            current.issues.append(child)
            current = child
        self.base.reports = [root]
        with self.assertRaisesRegex(RuntimeError, "depth"):
            self.run_check()

        self.base.reports = [
            FakeCheckReport(status="ok") for _ in range(1001)
        ]
        with self.assertRaisesRegex(RuntimeError, "1000 nodes"):
            self.run_check()

if __name__ == "__main__":
    unittest.main()
