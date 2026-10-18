import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import geometry_checks, inspection
from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError


class FakeEntity:
    def __init__(self, entity_id, entity_type):
        self._id = entity_id
        self._type = entity_type


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


class FakeCheckConstants:
    EXEC_ON_SELECTED = 4
    REPORT_NONE = 0
    KEEP_OLD = 8


class FakeCheck(FakeCheckConstants):
    _OPTION_NAMES = {
        "unchecked_faces", "collapsed_cons", "triple_cons", "cracks", "single_cons"
    }

    def __setattr__(self, name, value):
        base = getattr(self, "base", None)
        if base is not None and base.option_mode == "int" and name in self._OPTION_NAMES:
            value = int(value)
        object.__setattr__(self, name, value)

    def __init__(self, base):
        self.base = base
        self.calls = []
        self.try_fix_calls = 0
        self.unchecked_faces = True
        self.collapsed_cons = True
        self.triple_cons = False
        self.cracks = True
        self.single_cons = False

    def parameters(self):
        # The API metadata can remain the initial int values after property writes.
        return {
            "unchecked_faces": 1,
            "collapsed_cons": 1,
            "triple_cons": 0,
            "cracks": 1,
            "single_cons": 0,
        }

    def is_available_in_deck(self, deck):
        return self.base.availability

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.base.reports

    def try_fix(self):
        self.try_fix_calls += 1


class FakeState:
    def __init__(self):
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1


class FakeBase:
    Check = FakeCheck

    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.boundaries = {1: [11, 12], 2: [21, 22]}
        self.owners = {11: [1], 12: [1], 21: [2], 22: [2]}
        self.collect_calls = []
        self.reverse_calls = []
        self.checks_created = []
        self.availability = True
        self.reports = [FakeReport(status="ok", report_type="Geometry: Basic")]
        self.option_mode = "bool"

        for face_id in (1, 2):
            self.add("FACE", face_id)
        for cons_id in (11, 12, 21, 22):
            self.add("CONS", cons_id)

        def basic():
            check = FakeCheck(self)
            self.checks_created.append(check)
            return check

        self.checks = types.SimpleNamespace(
            geometry=types.SimpleNamespace(Basic=basic)
        )

    def add(self, entity_type, entity_id):
        entity = FakeEntity(entity_id, entity_type)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, deck, entity_type, entity_id):
        if deck != self.deck:
            return None
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type}
        return {field: values[field] for field in fields if field in values}

    def CollectEntities(self, deck, containers, search_types, recursive=False):
        self.collect_calls.append((deck, containers, search_types, recursive))
        if deck != 1 or containers is None or containers._type != "FACE":
            raise AssertionError("geometry checks must collect only within an explicit FACE")
        if search_types != "CONS" or recursive is not False:
            raise AssertionError("geometry checks must request direct CONS only")
        return [self.entities[("CONS", cons_id)] for cons_id in self.boundaries[containers._id]]

    def GetFacesOfCons(self, cons):
        self.reverse_calls.append(cons._id)
        return [self.entities[("FACE", face_id)] for face_id in self.owners[cons._id]]


class GeometryChecksTests(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def params(self, ids=(1, 2), **extra):
        result = {"deck": 1, "entity_type": "FACE", "entity_ids": list(ids)}
        result.update(extra)
        return result

    def run_check(self, params=None):
        return geometry_checks.execute(
            self.state, "geometry_check", self.params() if params is None else params
        )

    def test_selected_face_boundary_and_cons_issue_attribution_are_exact(self):
        self.base.add("FACE", 99)
        self.base.owners[22] = [2, 99]
        cons_issue = FakeReport(status="error", entities=[self.base.entities[("CONS", 11)]],
                                description="single CONS")
        face_issue = FakeReport(status="warning", entities=[self.base.entities[("FACE", 2)]])
        self.base.reports = [FakeReport(
            status="error", report_type="Geometry: Basic", has_fix=True,
            issues=[cons_issue, face_issue],
        )]

        result = self.run_check()

        self.assertEqual(result["entities"], [
            {"deck": 1, "type": "FACE", "id": 1},
            {"deck": 1, "type": "FACE", "id": 2},
        ])
        self.assertEqual(result["boundary_context"], [
            {"face": {"deck": 1, "type": "FACE", "id": 1}, "cons": [
                {"deck": 1, "type": "CONS", "id": 11},
                {"deck": 1, "type": "CONS", "id": 12},
            ]},
            {"face": {"deck": 1, "type": "FACE", "id": 2}, "cons": [
                {"deck": 1, "type": "CONS", "id": 21},
                {"deck": 1, "type": "CONS", "id": 22},
            ]},
        ])
        self.assertEqual(result["failed_cons_ids"], [11])
        self.assertEqual(result["affected_face_ids"], [1, 2])
        self.assertEqual(result["issue_counts"], {"ok": 0, "error": 1, "warning": 1})
        self.assertEqual(result["issue_count"], 2)
        self.assertEqual(result["native_report_count"], 1)
        self.assertEqual(result["report_count"], 1)
        self.assertEqual(result["report_node_count"], 3)
        self.assertEqual(result["reports"][0]["issues"][0]["entities"], [
            {"deck": 1, "type": "CONS", "id": 11}
        ])
        self.assertTrue(result["reports"][0]["has_fix"])
        self.assertFalse(result["reports"][0]["is_fixed"])
        self.assertEqual(len(self.base.collect_calls), 2)
        self.assertEqual([call[3] for call in self.base.collect_calls], [False, False])
        self.assertEqual(set(self.base.reverse_calls), {11, 12, 21, 22})
        self.assertNotIn('"id":99', json.dumps(result))

        check = self.base.checks_created[0]
        self.assertEqual(check.calls, [{
            "exec_mode": FakeCheckConstants.EXEC_ON_SELECTED,
            "report": FakeCheckConstants.REPORT_NONE,
            "history": FakeCheckConstants.KEEP_OLD,
            "entities": [self.base.entities[("FACE", 1)], self.base.entities[("FACE", 2)]],
        }])
        self.assertEqual(check.try_fix_calls, 0)
        self.assertEqual(self.state.main_calls, 1)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["auto_fix"])
        self.assertEqual(result["scope"], "explicit")
        self.assertFalse(result["reports_truncated"])
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_clean_root_is_valid_and_attribute_readback_overrides_stale_parameters(self):
        result = self.run_check(self.params((1,), single_cons=True))

        self.assertEqual(result["reports"][0]["status"], "ok")
        self.assertEqual(result["reports"][0]["entities"], [])
        self.assertEqual(result["reports"][0]["issues"], [])
        self.assertEqual(result["issue_count"], 0)
        self.assertEqual(result["issue_counts"], {"ok": 1, "error": 0, "warning": 0})
        self.assertEqual(result["options"], {
            "unchecked_faces": True,
            "collapsed_cons": True,
            "triple_cons": False,
            "cracks": True,
            "single_cons": True,
        })
        self.assertEqual(result["option_readback"]["parameters_before_set"]["single_cons"], 0)
        self.assertIs(result["option_readback"]["attributes_after_set"]["single_cons"], True)
        self.assertEqual(result["option_readback"]["authoritative"], "attributes_after_set")
        self.assertIs(self.base.checks_created[0].single_cons, True)

    def test_inspection_dispatch_passes_the_explicit_deck_to_geometry_check(self):
        result = inspection.execute(
            self.state, "geometry_check", self.params((1,))
        )

        self.assertEqual(result["entities"], [{"deck": 1, "type": "FACE", "id": 1}])
        self.assertEqual(result["check"], "basic")
        self.assertEqual(self.base.checks_created[0].calls[0]["entities"], [
            self.base.entities[("FACE", 1)]
        ])
        self.assertEqual(self.state.main_calls, 2)

    def test_invalid_parameters_are_rejected_before_check_factory(self):
        invalid = [
            {"entity_ids": [1]},
            self.params(entity_type="CONS"),
            self.params(check="extensive"),
            self.params(deck=2),
            self.params(single_cons=1),
            self.params(ids=(1, 1)),
            self.params(ids=(0,)),
            self.params(ids=(True,)),
            self.params(ids=tuple(range(1, 102))),
        ]
        for params in invalid:
            with self.subTest(params=params):
                with self.assertRaises((TypeError, ValueError)):
                    self.run_check(params)
        self.assertEqual(self.base.checks_created, [])
        self.assertEqual(self.base.collect_calls, [])

    def test_only_current_nastran_and_existing_faces_are_accepted(self):
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "current NASTRAN"):
            self.run_check(self.params(deck=2))
        self.base.deck = 1
        with self.assertRaises(LookupError):
            self.run_check(self.params((88,)))
        self.assertEqual(self.base.checks_created, [])

    def test_boundary_collection_and_reverse_face_ownership_fail_closed(self):
        malformed_collections = [
            ([], "no verifiable boundary"),
            ([self.base.entities[("FACE", 1)]], "does not match"),
            ([FakeEntity(True, "CONS")], "invalid entity ID"),
            ([self.base.entities[("CONS", 11)]] * 2, "repeated CONS"),
        ]
        for raw, message in malformed_collections:
            with self.subTest(collection=message):
                with patch.object(self.base, "CollectEntities", return_value=raw):
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.run_check()

        malformed_reverse = [
            ([], "no FACE"),
            ([self.base.entities[("FACE", 2)]], "do not match"),
            ([self.base.entities[("FACE", 1)], self.base.entities[("FACE", 2)]],
             "do not match"),
            ([self.base.entities[("CONS", 11)]], "does not match"),
            (None, "invalid FACE list"),
        ]
        for raw, message in malformed_reverse:
            with self.subTest(reverse=message):
                with patch.object(self.base, "GetFacesOfCons", return_value=raw):
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.run_check()

    def test_report_fields_status_scope_and_error_leaf_entities_are_required(self):
        outside_face = self.base.add("FACE", 99)
        outside_cons = self.base.add("CONS", 99)
        invalid_reports = [
            [FakeReport(status=None)],
            [FakeReport(status="unknown")],
            [FakeReport(
                status="error", is_fixed=True,
                entities=[self.base.entities[("FACE", 1)]],
            )],
            [FakeReport(status="error", entities=[])],
            [FakeReport(status="warning", entities=[])],
            [FakeReport(status="error", issues=[FakeReport(
                status="warning", entities=[self.base.entities[("FACE", 1)]]
            )])],
            [FakeReport(status="error", entities=[outside_face])],
            [FakeReport(status="error", entities=[outside_cons])],
        ]
        missing_field = FakeReport(status="ok")
        del missing_field.description
        invalid_reports.append([missing_field])

        for reports in invalid_reports:
            with self.subTest(report=reports):
                self.base.reports = reports
                with self.assertRaises(RuntimeError):
                    self.run_check(self.params((1,)))

    def test_report_tree_rejects_cycles_reuse_depth_nodes_and_reference_overflow(self):
        cyclic = FakeReport(status="error", entities=[self.base.entities[("FACE", 1)]])
        cyclic.issues.append(cyclic)
        self.base.reports = [cyclic]
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            self.run_check(self.params((1,)))

        reused = FakeReport(status="error", entities=[self.base.entities[("CONS", 11)]])
        self.base.reports = [FakeReport(status="error", issues=[reused, reused])]
        with self.assertRaisesRegex(RuntimeError, "reuses"):
            self.run_check(self.params((1,)))

        root = current = FakeReport(status="ok")
        for _ in range(9):
            child = FakeReport(status="ok")
            current.issues.append(child)
            current = child
        self.base.reports = [root]
        with self.assertRaisesRegex(RuntimeError, "depth"):
            self.run_check(self.params((1,)))

        self.base.reports = [FakeReport(status="ok") for _ in range(1001)]
        with self.assertRaisesRegex(RuntimeError, "1000 nodes"):
            self.run_check(self.params((1,)))

        face = self.base.entities[("FACE", 1)]
        self.base.reports = [FakeReport(status="ok", entities=[face] * 10001)]
        with self.assertRaisesRegex(RuntimeError, "10000 entity references"):
            self.run_check(self.params((1,)))

    def test_capability_option_and_frame_budgets_fail_closed(self):
        with patch.object(self.base.checks.geometry, "Basic", return_value=object()):
            with self.assertRaisesRegex(CapabilityUnavailableError, "did not return"):
                self.run_check(self.params((1,)))
        self.assertEqual(self.base.checks_created, [])

        with patch.object(self.base, "GetFacesOfCons", None):
            with self.assertRaises(CapabilityUnavailableError):
                self.run_check(self.params((1,)))
        self.assertEqual(self.base.checks_created, [])

        self.base.option_mode = "int"
        with self.assertRaisesRegex(CapabilityUnavailableError, "strict boolean"):
            self.run_check(self.params((1,)))
        self.assertEqual(self.base.checks_created[0].calls, [])

        self.base.option_mode = "bool"
        self.base.availability = 1
        with self.assertRaisesRegex(CapabilityUnavailableError, "non-boolean"):
            self.run_check(self.params((1,)))

        self.base.availability = True
        with patch.object(geometry_checks, "MAX_FRAME_BYTES", 512):
            with self.assertRaisesRegex(ValueError, "input budget"):
                self.run_check(self.params((1,), extra="x" * 100))
        with patch.object(geometry_checks, "MAX_FRAME_BYTES", 1024):
            with self.assertRaisesRegex(RuntimeError, "output budget"):
                self.run_check(self.params((1,)))


class GeometryChecksMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_inspect_bounds_options_before_submit_and_routes_geometry_check(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from ansa_mcp_plugin.mcp.extensions import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult
        from ansa_mcp_shared.framing import MAX_FRAME_BYTES

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

        server = FastMCP("geometry-check-inspect")
        register(server, object(), submit)
        args = {
            "operation_id": "geometry-check",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            "action": "geometry_check",
            "deck": 1,
            "options": {"entity_type": "FACE", "entity_ids": [1]},
        }

        oversized = {
            **args,
            "options": {
                **args["options"],
                "padding": "x" * (MAX_FRAME_BYTES // 8 + 1),
            },
        }
        with self.assertRaises(ToolError):
            await server.call_tool("ansa_model_inspect", oversized)
        self.assertEqual(submitted, [])

        for invalid_deck in (True, "1", 1.0):
            with self.subTest(deck=invalid_deck):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_model_inspect", {**args, "deck": invalid_deck})
                self.assertEqual(submitted, [])

        await server.call_tool("ansa_model_inspect", args)
        self.assertEqual(submitted, [(
            "geometry_check",
            {"entity_type": "FACE", "entity_ids": [1], "deck": 1},
            "geometry-check",
            300,
        )])


if __name__ == "__main__":
    unittest.main()
