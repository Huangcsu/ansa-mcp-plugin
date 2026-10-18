import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import connection_checks
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError


CHECK_FLAGS = connection_checks._CHECK_SWITCHES


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields=None, position=None):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})
        self.position = position

    def ansa_type(self):
        return self._type

    def card_fields(self, _deck):
        return list(self.fields)


class FakeReport:
    def __init__(self, status, entities=(), issues=(), *, report_type="Error",
                 description="report", has_fix=None, is_fixed=False):
        self.type = report_type
        self.description = description
        self.status = status
        self.has_fix = has_fix
        self.is_fixed = is_fixed
        self.entities = list(entities)
        self.issues = list(issues)


class FakeCheck:
    EXEC_ON_SELECTED = 1
    REPORT_NONE = 2
    KEEP_OLD = 3

    def __init__(self, reports):
        object.__setattr__(self, "_reports", reports)
        object.__setattr__(self, "_execute_calls", [])
        values = {name: int(name == "check_double_connections") for name in CHECK_FLAGS}
        values.update({
            "double_connections_method": "By distance between projection centers",
            "equal_types": 1,
            "spot_distance": 5.0,
        })
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "fix_calls", 0)

    def __getattr__(self, name):
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        values = object.__getattribute__(self, "_values")
        if name in values:
            values[name] = value
        else:
            object.__setattr__(self, name, value)

    def is_available_in_deck(self, deck):
        return deck == 1

    def parameters(self):
        values = dict(self._values)
        method = values["double_connections_method"]
        values["double_connections_method"] = method + "  "
        return values

    def execute(self, **kwargs):
        self._execute_calls.append(kwargs)
        return self._reports

    def try_fix(self, *_args, **_kwargs):
        self.fix_calls += 1
        raise AssertionError("connection checks must never call ANSA auto-fix")


class FakeConnections:
    def __init__(self, base):
        self.base = base

    def ConnectivityStringToEnts(self, value):
        return [entity for (kind, _), entity in self.base.entities.items()
                if kind == "ANSAPART" and entity.fields.get("Module Id") == value]


class FakeBase:
    Check = FakeCheck

    def __init__(self, reports=None):
        self.deck = 1
        self.entities = {}
        self.collect_calls = []
        self.reports = reports or []
        self.check_instance = None
        self.missing_factory = False
        self.unknown_check_option = False
        general = types.SimpleNamespace(Connections=self.make_check)
        self.checks = types.SimpleNamespace(general=general)

    def make_check(self):
        if self.missing_factory:
            raise AssertionError("missing factory should be rejected before call")
        self.check_instance = FakeCheck(self.reports)
        if self.unknown_check_option:
            self.check_instance._values["check_future_option"] = 1
        return self.check_instance

    def add(self, entity_type, entity_id, fields=None, position=None):
        entity = FakeEntity(entity_id, entity_type, fields, position)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def add_part(self, entity_id, module_id):
        return self.add("ANSAPART", entity_id, {"Module Id": module_id})

    def add_point(self, entity_id):
        fields = {
            "Name": f"Spot {entity_id}", "D": 3.0,
            "X": float(entity_id), "Y": 1.0, "Z": -2.0,
            "P1": "part-a", "P2": "part-b",
        }
        fields.update({f"P{index}": "" for index in range(3, 11)})
        position = [fields["X"], fields["Y"], fields["Z"]]
        return self.add("SpotweldPoint_Type", entity_id, fields, position)

    def CurrentDeck(self):
        return self.deck

    def CollectEntities(self, deck, containers, entity_type, recursive=False):
        self.collect_calls.append((deck, containers, entity_type, recursive))
        if deck != 1 or containers is not None or recursive is not False:
            raise AssertionError("checks must use fresh, direct, typed collections")
        return [entity for (kind, _), entity in self.entities.items() if kind == entity_type]

    def GetEntity(self, *_args):
        raise AssertionError("connection checks must not use GetEntity before card_fields")

    def GetEntityCardValues(self, _deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {name: values[name] for name in fields if name in values}


class FakeState:
    def __init__(self):
        self.assertions = 0

    def assert_main(self):
        self.assertions += 1


class ConnectionChecksOperationTests(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.base.add_part(1, "part-a")
        self.base.add_part(2, "part-b")
        self.points = {entity_id: self.base.add_point(entity_id) for entity_id in (11, 12, 13)}
        self.connection_api = FakeConnections(self.base)
        self.state = FakeState()
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.connections = self.connection_api
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def run_check(self, point_ids=(11, 12), **overrides):
        params = {"deck": 1, "point_ids": list(point_ids)}
        params.update(overrides)
        return connection_checks.execute(self.state, "connection_check", params)

    def test_report_tree_counts_leaf_issues_and_deduplicates_failed_point_ids(self):
        self.base.reports = [FakeReport(
            None,
            report_type="Connections",
            description="Connections",
            issues=[
                FakeReport("error", [self.points[11], self.points[12]], has_fix=True),
                FakeReport("warning", [self.points[11]]),
                FakeReport("ok", []),
            ],
        )]

        result = self.run_check((12, 11), distance=0.25)

        self.assertEqual(result["report_count"], 4)
        self.assertEqual(result["issue_counts"], {"ok": 1, "error": 1, "warning": 1})
        self.assertEqual(result["issue_count"], 3)
        self.assertEqual(result["failed_point_ids"], [11, 12])
        self.assertEqual(result["reports"][0]["status"], "error")
        self.assertTrue(result["reports"][0]["issues"][0]["has_fix"])
        self.assertFalse(result["reports"][0]["issues"][0]["is_fixed"])
        self.assertEqual(result["options"]["double_connections_method"].strip(),
                         "By absolute distance")
        self.assertEqual(result["options"]["spot_distance"], 0.25)

        check = self.base.check_instance
        call = check._execute_calls[0]
        self.assertEqual(call["exec_mode"], FakeCheck.EXEC_ON_SELECTED)
        self.assertEqual(call["report"], FakeCheck.REPORT_NONE)
        self.assertEqual(call["history"], FakeCheck.KEEP_OLD)
        self.assertEqual(call["entities"], [self.points[12], self.points[11]])
        self.assertEqual(check.fix_calls, 0)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])

    def test_clean_result_and_report_entities_must_stay_in_selected_scope(self):
        self.base.reports = [FakeReport("ok", report_type="Connections", description="clean")]
        clean = self.run_check((11,))
        self.assertEqual(clean["issue_count"], 1)
        self.assertEqual(clean["failed_point_ids"], [])

        self.base.reports = [FakeReport("error", [self.points[12]])]
        with self.assertRaisesRegex(RuntimeError, "outside the selected SpotweldPoint scope"):
            self.run_check((11,))

        self.assertTrue(all(call[2] == "SpotweldPoint_Type" for call in self.base.collect_calls))
        self.assertTrue(all(call[3] is False for call in self.base.collect_calls))

    def test_stale_parameter_metadata_is_reported_but_ignored_attribute_writes_fail(self):
        original_parameters = FakeCheck.parameters

        def stale_parameters(check):
            if not hasattr(check, "_initial_parameters"):
                check._initial_parameters = original_parameters(check)
            return dict(check._initial_parameters)

        with patch.object(FakeCheck, "parameters", stale_parameters):
            result = self.run_check((11,), distance=0.25)
        self.assertEqual(result["options"]["spot_distance"], 0.25)
        self.assertEqual(result["option_readback"]["source"], "check_attributes")
        self.assertFalse(result["option_readback"]["parameters_match"])
        self.assertIn("spot_distance", result["option_readback"]["parameter_mismatches"])

        original_setattr = FakeCheck.__setattr__

        def ignored_distance(check, name, value):
            if name != "spot_distance":
                original_setattr(check, name, value)

        with patch.object(FakeCheck, "__setattr__", ignored_distance):
            with self.assertRaisesRegex(CapabilityUnavailableError, "spot_distance did not retain"):
                self.run_check((11,), distance=0.25)
        self.assertEqual(self.base.check_instance._execute_calls, [])

        make_check = self.base.make_check

        def tiny_initial_distance():
            check = make_check()
            check._values["spot_distance"] = 1e-12
            return check

        self.base.checks.general.Connections = tiny_initial_distance
        with patch.object(FakeCheck, "__setattr__", ignored_distance):
            with self.assertRaisesRegex(CapabilityUnavailableError, "spot_distance did not retain"):
                self.run_check((11,), distance=1e-15)
        self.assertEqual(self.base.check_instance._execute_calls, [])

    def test_strict_inputs_unknown_switch_and_missing_factory_fail_closed(self):
        invalid = (
            {"deck": True}, {"point_ids": [True]}, {"point_ids": [11, 11]},
            {"point_ids": []}, {"distance": True}, {"distance": "0.1"},
            {"distance": 0}, {"distance": float("nan")}, {"fix": True},
        )
        for changed in invalid:
            params = {"deck": 1, "point_ids": [11]}
            params.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                connection_checks.execute(self.state, "connection_check", params)
        self.assertEqual(self.base.collect_calls, [])

        self.base.unknown_check_option = True
        with self.assertRaisesRegex(CapabilityUnavailableError, "unverified check option"):
            self.run_check((11,))
        self.assertEqual(self.base.check_instance._execute_calls, [])

        self.base.unknown_check_option = False
        self.base.missing_factory = True
        self.base.checks.general.Connections = None
        with self.assertRaisesRegex(CapabilityUnavailableError, "Connections is missing"):
            self.run_check((11,))

    def test_cycles_invalid_reports_and_auto_fixed_results_fail_closed(self):
        cyclic = FakeReport("error", [self.points[11]])
        cyclic.issues.append(cyclic)
        self.base.reports = [cyclic]
        with self.assertRaisesRegex(RuntimeError, "contains a cycle"):
            self.run_check((11,))

        self.base.reports = [FakeReport("other", [self.points[11]])]
        with self.assertRaisesRegex(RuntimeError, "unknown status"):
            self.run_check((11,))

        self.base.reports = [FakeReport("error", [self.points[11]], is_fixed=True)]
        with self.assertRaisesRegex(RuntimeError, "auto-fixed result"):
            self.run_check((11,))


class ConnectionChecksMcpSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_is_strict_and_submits_only_the_explicit_read_scope(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from ansa_mcp_plugin.mcp.connection_checks import register
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

        server = FastMCP("connection-check-schema")
        register(server, submit)
        args = {
            "operation_id": "connection-check",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "deck": 1,
            "point_ids": [11, 12],
        }
        invalid = (
            {"deck": True}, {"deck": "1"}, {"point_ids": []},
            {"point_ids": [True]}, {"point_ids": [11, 11]},
            {"point_ids": list(range(1, 202))}, {"distance": True},
            {"distance": "0.1"}, {"distance": 0}, {"distance": float("nan")},
            {"ttl_seconds": True}, {"fix": True}, {"visible": True},
            {"allmodel": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(ToolError):
                await server.call_tool("ansa_connection_check", {**args, **changed})
        self.assertEqual(submitted, [])

        await server.call_tool("ansa_connection_check", args)
        self.assertEqual(submitted, [(
            "connection_check",
            {"deck": 1, "point_ids": [11, 12], "distance": 0.1},
            "connection-check",
            300,
        )])

    async def test_main_registration_and_read_only_classification(self):
        from types import SimpleNamespace

        from ansa_mcp_plugin.__main__ import create_server
        from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model

        server = create_server(SimpleNamespace())
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(len(tools), 65)
        self.assertIn("ansa_connection_check", names)
        self.assertIn("connection_check", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("connection_check"))


if __name__ == "__main__":
    unittest.main()
