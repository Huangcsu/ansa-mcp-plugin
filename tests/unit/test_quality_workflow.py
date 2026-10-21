"""Business-boundary tests for scoped mesh quality report, locate and repair."""

import copy
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import native_quality, quality_workflow, quality_options


class FakeState:
    def __init__(self):
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1


class FakeEntity:
    def __init__(self, entity_id, entity_type="SHELL"):
        self._id = entity_id
        self._type = entity_type

    def ansa_type(self, _deck):
        return self._type


class FakeNode:
    def __init__(self, node_id, position=(0.0, 0.0, 0.0)):
        self._id = node_id
        self._type = "GRID"
        self.position = tuple(position)

    def ansa_type(self, _deck):
        return self._type


class FakeCheckAPI:
    EXEC_ON_SELECTED = 4
    REPORT_NONE = 0
    KEEP_OLD = 8


class FakeHeader:
    def __init__(self, base, has_fix=True):
        self.base = base
        self.entities = []
        self.issues = []
        self.has_fix = has_fix
        self.fix_method = 2
        self.try_fix_calls = 0

    def try_fix(self, request_gui=False):
        self.try_fix_calls += 1
        self.request_gui = request_gui
        self.base.fix_calls += 1
        if self.base.on_fix is not None:
            self.base.on_fix()


class FakeQualityCheck(FakeCheckAPI):
    def __init__(self, base):
        self.base = base
        self.calls = []
        self.try_fix_calls = 0
        self.fix_method = None
        self.use_criteria = None
        self.quality_criteria = None

    def is_available_in_deck(self, _deck):
        return True

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        self.base.last_header = FakeHeader(self.base, self.base.has_fix)
        return [self.base.last_header]

    def try_fix(self, request_gui=False):
        self.try_fix_calls += 1
        self.request_gui = request_gui
        if self.base.on_fix is not None:
            self.base.on_fix()


class FakeBase:
    Check = FakeCheckAPI

    def __init__(self):
        self.deck = 1
        self.checks_created = []
        self.on_fix = None
        self.has_fix = True
        self.or_calls = 0
        self.redraw_calls = 0
        self.fix_calls = 0
        self.visible = []
        self.selected = []
        self.checks = types.SimpleNamespace(
            mesh=types.SimpleNamespace(MeshQuality=self._create_check)
        )

    def _create_check(self):
        check = FakeQualityCheck(self)
        self.checks_created.append(check)
        return check

    def CurrentDeck(self):
        return self.deck

    def DataBaseName(self):
        return r"C:\current.ansa"

    def Or(self, entities, apply_on_contents=False):
        self.or_calls += 1
        self.apply_on_contents = apply_on_contents
        self.selected = list(entities)
        return 1

    def CollectEntities(self, _deck, _containers, _types, filter_visible=False):
        self.filter_visible = filter_visible
        return list(self.visible) if filter_visible else []

    def RedrawAll(self):
        self.redraw_calls += 1

    def ZoomAll(self):
        self.redraw_calls += 1


class NativeQualityBase:
    def __init__(self):
        self.element_quality_calls = []
        self.length_calls = []
        self.shell_getter_calls = []
        self.solid_getter_calls = []
        self.fail = set()

    def CurrentDeck(self):
        return 1

    def F11ShellsOptionsGet(self, name):
        self.shell_getter_calls.append(name)
        if name in self.fail:
            raise RuntimeError("unsupported native metric")
        return {"calculation": "average"}

    def F11SolidsOptionsGet(self, name):
        self.solid_getter_calls.append(name)
        if name in self.fail:
            raise RuntimeError("unsupported native metric")
        return {"calculation": "average"}

    def ElementQuality(self, entity, metrics):
        self.element_quality_calls.append((entity._id, tuple(metrics)))
        token = metrics[0]
        if token in self.fail:
            raise RuntimeError("unsupported native metric")
        values = {"ASPECT": 4.0, "WARP": 1.0, "COLLAPSE": 0.5}
        return (values[token],)

    def CalculateAverageMinMaxElementLength(self, entities):
        self.length_calls.append([entity._id for entity in entities])
        return {"min": 2.0, "max": 8.0}


class QualityWorkflowBusinessTests(unittest.TestCase):
    def setUp(self):
        self.state = FakeState()
        self.ansa = types.ModuleType("ansa")
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

        def legacy_fix(_base, header, _effective, _original, result):
            result["criteria_synchronized"] = True
            result["api_invoked"] = True
            try:
                return header.try_fix(request_gui=False)
            finally:
                result["criteria_restored"] = True

        self.criteria_fix_patch = patch.object(
            native_quality, "_run_fix_with_criteria", side_effect=legacy_fix
        )
        self.criteria_fix_patch.start()
        self.addCleanup(self.criteria_fix_patch.stop)
        self.options_patch = patch.object(quality_options, "run_with_options", side_effect=lambda base, family, maximum, callback, result: callback())
        self.options_patch.start()
        self.addCleanup(self.options_patch.stop)

    def report_profile(self, family, criteria):
        return {
            "analysis_type": "linear",
            "element_family": family,
            "criteria": criteria,
        }

    def run_report(self, base, profile, entity_ids=(11,)):
        self.ansa.base = base
        entity = FakeEntity(entity_ids[0], profile["element_family"].upper())
        snapshot = {
            "deck": 1,
            "family": profile["element_family"],
            "elements": [{
                "entity": {"deck": 1, "type": entity._type, "id": entity._id},
                "node_ids": [101],
            }],
            "node_positions": {"101": [0.0, 0.0, 0.0]},
            "node_cards": {},
        }
        with patch.object(
            quality_workflow,
            "_scope",
            return_value=([entity], {101: FakeNode(101)}, snapshot, "g" * 64),
        ), patch.object(quality_workflow, "_criteria", return_value="native criteria"):
            return quality_workflow.execute(
                self.state,
                "quality_report",
                {"deck": 1, "entity_ids": list(entity_ids), "profile": profile},
            )

    def repair_params(self, expected_scope_sha256, **overrides):
        params = {
            "deck": 1,
            "entity_ids": [11],
            "profile": self.report_profile("shell", [{"name": "ASPECT", "max": 5.0}]),
            "expected_scope_sha256": expected_scope_sha256,
            "backup_path": r"F:\backup.ansa",
            "movable_node_ids": [101],
            "maximum_displacement": 0.1,
            "method": "fix_quality",
        }
        params.update(overrides)
        return params

    def repair_before(self, complete=True):
        return {
            "scope_sha256": "s" * 64,
            "geometry_sha256": "g" * 64,
            "complete": complete,
            "failed_entity_ids": [11],
            "entity_type": "SHELL",
            "rows": [{"id": 11, "violations": ["ASPECT"]}],
        }

    @staticmethod
    def full_snapshot(node, entity_type="SHELL", extra_elements=()):
        return {
            "elements": [
                {
                    "entity": {"deck": 1, "type": entity_type, "id": 11},
                    "node_ids": [101],
                    "fields": {},
                },
                *extra_elements,
            ],
            "nodes": {"101": {
                "card": {
                    "entity": {"deck": 1, "type": "GRID", "id": 101},
                    "fields": {"X1": 0.0, "X2": 0.0, "X3": 0.0},
                },
                "position": list(node.position),
            }},
        }

    def test_report_uses_native_element_quality_and_length_apis_for_both_families(self):
        cases = (
            (
                "shell",
                [{"name": "ASPECT", "max": 5.0}, {"name": "MINL", "min": 1.0}],
            ),
            ("solid", [{"name": "TETRA_COLLAPSE", "max": 1.0}]),
        )
        for family, criteria in cases:
            with self.subTest(family=family):
                base = NativeQualityBase()
                result = self.run_report(
                    base, self.report_profile(family, criteria)
                )

                self.assertTrue(result["read_only"])
                self.assertTrue(result["complete"])
                self.assertTrue(result["passed"])
                self.assertEqual(result["entity_type"], family.upper())
                if family == "shell":
                    self.assertEqual(
                        base.shell_getter_calls, ["aspect ratio", "min length"]
                    )
                    self.assertEqual(base.solid_getter_calls, [])
                    self.assertEqual(
                        base.element_quality_calls, [(11, ("ASPECT",))]
                    )
                    self.assertEqual(base.length_calls, [[11]])
                else:
                    self.assertEqual(base.shell_getter_calls, [])
                    self.assertEqual(base.solid_getter_calls, ["collapse"])
                    self.assertEqual(
                        base.element_quality_calls, [(11, ("COLLAPSE",))]
                    )
                    self.assertEqual(base.length_calls, [])

    def test_unsupported_metric_is_unknown_and_cannot_pass(self):
        base = NativeQualityBase()
        base.fail = {"warping", "WARP"}
        profile = self.report_profile("shell", [{"name": "WARP", "max": 2.0}])

        result = self.run_report(base, profile)

        row = result["rows"][0]
        self.assertFalse(result["complete"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["unknown_entity_ids"], [11])
        self.assertEqual(row["unknown"], ["WARP"])
        self.assertIsNone(row["metrics"]["WARP"])
        self.assertIn("WARP", row["metric_errors"])
        self.assertFalse(result["definitions"]["WARP"]["complete"])
        self.assertEqual(base.element_quality_calls, [])

    def test_scope_fingerprint_changes_when_material_dependent_measurement_changes(self):
        base = NativeQualityBase()
        base.ElementQuality = lambda _entity, _metrics: [0.1]
        profile = self.report_profile("shell", [{"name": "CRASH", "min": 0.05}])
        before = self.run_report(base, profile)
        base.ElementQuality = lambda _entity, _metrics: [0.01]
        after = self.run_report(base, profile)
        self.assertEqual(before["geometry_sha256"], after["geometry_sha256"])
        self.assertNotEqual(before["scope_sha256"], after["scope_sha256"])
        self.assertTrue(before["passed"])
        self.assertFalse(after["passed"])

    def test_explicit_fix_refuses_two_sided_minimum_length_instead_of_dropping_max(self):
        profile = {"element_family": "shell", "minimum_length": 0.5,
                   "criteria": [{"name": "MINL", "min": 1., "max": 2.}]}
        text = "min length [shells] = OFF, 1 | method | 1, -1./"
        with self.assertRaisesRegex(ValueError, "single min bound"):
            quality_workflow._custom_criteria(text, profile)

    def test_large_unknown_report_keeps_all_decisions_within_transport_budget(self):
        rows = [{"id": eid, "metrics": {name: None for name in quality_workflow._METRICS},
                 "metric_errors": {name: "x"*512 for name in quality_workflow._METRICS}}
                for eid in range(1, 2001)]
        report = {"rows": rows, "failed_entity_ids": [], "unknown_entity_ids": list(range(1, 2001)),
                  "complete": False, "passed": False, "scope_sha256": "a"*64}
        for shared in (False, True):
            with self.subTest(shared_before_after=shared):
                result = quality_workflow._bounded_result({"before": report, "after": report if shared else copy.deepcopy(report)})
                self.assertLessEqual(len(quality_workflow._encoded(result)), quality_workflow._MAX_RESULT_BYTES)
                for value in (result["before"], result["after"]):
                    self.assertFalse(value["complete"])
                    self.assertFalse(value["passed"])
                    self.assertEqual(value["unknown_entity_ids"], list(range(1, 2001)))
                    self.assertEqual(value["rows_total"], 2000)
                    self.assertTrue(value["rows_truncated"])
                    self.assertEqual(value["rows_returned"], len(value["rows"]))
                    self.assertLess(value["rows_returned"], 2000)

    def test_locate_rejects_stale_scope_before_display(self):
        base = FakeBase()
        self.ansa.base = base
        guitk_calls = []
        self.ansa.guitk = types.SimpleNamespace(
            BCApplicationIsGuiMode=lambda: guitk_calls.append(True) or True
        )
        before = {"scope_sha256": "b" * 64, "entity_type": "SHELL",
                  "failed_entity_ids": [11], "geometry_sha256": "g" * 64}
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [FakeEntity(11)], {101: FakeNode(101)}, {}),
        ):
            with self.assertRaisesRegex(ValueError, "scope changed"):
                quality_workflow.execute(
                    self.state,
                    "quality_locate",
                    {
                        "deck": 1,
                        "entity_ids": [11],
                        "profile": self.report_profile(
                            "shell", [{"name": "ASPECT", "max": 5.0}]
                        ),
                        "expected_scope_sha256": "a" * 64,
                    },
                )
        self.assertEqual(guitk_calls, [])
        self.assertEqual(base.or_calls, 0)
        self.assertEqual(base.redraw_calls, 0)

    def test_locate_requires_gui_mode_before_isolation(self):
        from ansa_mcp_bridge.operations.inspection import CapabilityUnavailableError

        base = FakeBase()
        self.ansa.base = base
        guitk_calls = []
        self.ansa.guitk = types.SimpleNamespace(
            BCApplicationIsGuiMode=lambda: guitk_calls.append(False) or False
        )
        before = {
            "scope_sha256": "s" * 64,
            "geometry_sha256": "g" * 64,
            "entity_type": "SHELL",
            "failed_entity_ids": [11],
        }
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [FakeEntity(11)], {101: FakeNode(101)}, {}),
        ):
            with self.assertRaisesRegex(
                CapabilityUnavailableError, "requires an ANSA GUI session"
            ):
                quality_workflow.execute(
                    self.state,
                    "quality_locate",
                    {
                        "deck": 1,
                        "entity_ids": [11],
                        "profile": self.report_profile(
                            "shell", [{"name": "ASPECT", "max": 5.0}]
                        ),
                        "expected_scope_sha256": "s" * 64,
                    },
                )
        self.assertEqual(guitk_calls, [False])
        self.assertEqual(base.or_calls, 0)
        self.assertEqual(base.redraw_calls, 0)

    def test_locate_in_gui_mode_isolates_failed_elements(self):
        base = FakeBase()
        self.ansa.base = base
        guitk_calls = []
        self.ansa.guitk = types.SimpleNamespace(
            BCApplicationIsGuiMode=lambda: guitk_calls.append(True) or True
        )
        entity = FakeEntity(11)
        base.visible = [entity]
        before = {
            "scope_sha256": "s" * 64,
            "geometry_sha256": "g" * 64,
            "entity_type": "SHELL",
            "failed_entity_ids": [11],
        }
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [entity], {101: FakeNode(101)}, {}),
        ), patch.object(
            quality_workflow,
            "_scope",
            return_value=([entity], {101: FakeNode(101)}, {}, before["geometry_sha256"]),
        ):
            result = quality_workflow.execute(
                self.state,
                "quality_locate",
                {
                    "deck": 1,
                    "entity_ids": [11],
                    "profile": self.report_profile(
                        "shell", [{"name": "ASPECT", "max": 5.0}]
                    ),
                    "expected_scope_sha256": "s" * 64,
                },
            )

        self.assertEqual(guitk_calls, [True])
        self.assertEqual(base.or_calls, 1)
        self.assertEqual(base.selected, [entity])
        self.assertEqual(base.redraw_calls, 2)
        self.assertTrue(result["display_changed"])
        self.assertEqual(result["located_entity_ids"], [11])
        self.assertFalse(result["model_written"])

    def test_repair_rejects_stale_scope_before_native_check(self):
        base = FakeBase()
        self.ansa.base = base
        before = self.repair_before()
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [FakeEntity(11)], {101: FakeNode(101)}, {}),
        ):
            with self.assertRaisesRegex(ValueError, "scope changed"):
                quality_workflow.execute(
                    self.state,
                    "quality_repair",
                    self.repair_params("a" * 64),
                )
        self.assertEqual(base.checks_created, [])

    def test_repair_preflight_rejects_unknown_metrics(self):
        base = FakeBase()
        self.ansa.base = base
        before = self.repair_before(complete=False)
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [FakeEntity(11)], {101: FakeNode(101)}, {}),
        ):
            with self.assertRaisesRegex(ValueError, "unknown or unsupported"):
                quality_workflow.execute(
                    self.state,
                    "quality_repair",
                    self.repair_params(before["scope_sha256"]),
                )
        self.assertEqual(base.checks_created, [])

    def test_repair_preflight_rejects_movable_node_outside_scope(self):
        base = FakeBase()
        self.ansa.base = base
        before = self.repair_before()
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [FakeEntity(11)], {101: FakeNode(101)}, {}),
        ):
            with self.assertRaisesRegex(ValueError, "movable nodes must belong"):
                quality_workflow.execute(
                    self.state,
                    "quality_repair",
                    self.repair_params(
                        before["scope_sha256"], movable_node_ids=[999]
                    ),
                )
        self.assertEqual(base.checks_created, [])

    def test_native_fix_without_change_is_not_reported_as_applied(self):
        base = FakeBase()
        self.ansa.base = base
        node = FakeNode(101)
        before = self.repair_before()
        after = copy.deepcopy(before)
        snapshot = self.full_snapshot(node)
        criteria_text = "aspect ratio [shells] = OFF,|before|after"
        with patch.object(
            quality_workflow,
            "_report",
            side_effect=[
                (before, [FakeEntity(11)], {101: node}, {}),
                (after, [FakeEntity(11)], {101: node}, {}),
            ],
        ), patch.object(
            quality_workflow,
            "_scope",
            return_value=(
                [FakeEntity(11)],
                {101: node},
                {},
                before["geometry_sha256"],
            ),
        ), patch.object(
            quality_workflow, "_criteria", return_value=criteria_text
        ), patch.object(
            quality_workflow,
            "_validity",
            return_value=[{"id": 11, "measure": 1.0, "jacobian": 0.5}],
        ), patch.object(
            quality_workflow, "_fe_snapshot", return_value=snapshot
        ), patch(
            "ansa_mcp_bridge.operations._model_save",
            return_value={"path": r"F:\backup.ansa", "saved": True},
        ) as save:
            result = quality_workflow.execute(
                self.state,
                "quality_repair",
                self.repair_params(before["scope_sha256"]),
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "no_improvement")
        self.assertEqual(result["method"], "fix_quality")
        self.assertEqual(result["fix_method"], 2)
        self.assertEqual(result["api"], "ansa.base.CheckReport.try_fix")
        self.assertEqual(base.last_header.fix_method, 2)
        self.assertEqual(
            base.checks_created[0].use_criteria, "Use custom criteria"
        )
        self.assertEqual(base.fix_calls, 1)
        save.assert_called_once()

    def _assert_movement_is_rejected(self, cumulative=False):
        base = FakeBase()
        self.ansa.base = base
        node = FakeNode(101, position=(0.06, 0.0, 0.0) if cumulative else (0.0, 0.0, 0.0))
        before = self.repair_before()
        after = copy.deepcopy(before)
        criteria_text = "aspect ratio [shells] = OFF,|before|after"

        def snapshot(*_args):
            return self.full_snapshot(node)

        base.on_fix = lambda: setattr(node, "position", (0.12 if cumulative else 1.0, 0.0, 0.0))
        with patch.object(
            quality_workflow,
            "_report",
            side_effect=[
                (before, [FakeEntity(11)], {101: node}, {}),
                (after, [FakeEntity(11)], {101: node}, {}),
            ],
        ), patch.object(
            quality_workflow,
            "_scope",
            return_value=(
                [FakeEntity(11)],
                {101: node},
                {},
                before["geometry_sha256"],
            ),
        ), patch.object(
            quality_workflow, "_criteria", return_value=criteria_text
        ), patch.object(
            quality_workflow,
            "_validity",
            return_value=[{"id": 11, "measure": 1.0, "jacobian": 0.5}],
        ), patch.object(
            quality_workflow, "_fe_snapshot", side_effect=snapshot
        ), patch.object(
            quality_workflow.catalog,
            "_get_entity",
            return_value=(node, {"deck": 1, "type": "GRID", "id": 101}),
        ), patch(
            "ansa_mcp_bridge.operations._model_save",
            return_value={"path": r"F:\backup.ansa", "saved": True},
        ):
            params = self.repair_params(before["scope_sha256"])
            if cumulative:
                params.update(_cumulative_baseline={"101": [0.0, 0.0, 0.0]}, maximum_total_displacement=0.1)
                profile = quality_workflow.normalize_profile(**params["profile"])
                result = quality_workflow._repair(self.state, base, self.ansa.constants, 1, params, profile,
                                                  [11], before, [FakeEntity(11)], {101: node})
            else:
                result = quality_workflow.execute(self.state, "quality_repair", params)

        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"])
        self.assertIn("exceeded the cumulative displacement limit" if cumulative else "exceeded the displacement limit", result["error"])
        self.assertTrue(result["coordinates_rolled_back"])
        self.assertFalse(result["recovery_required"])
        self.assertEqual(node.position, (0.06, 0.0, 0.0) if cumulative else (0.0, 0.0, 0.0))
        self.assertEqual(base.fix_calls, 1)

    def test_over_displacement_restores_coordinates_and_returns_not_ok(self):
        self._assert_movement_is_rejected()

    def test_cumulative_limit_rejects_a_step_within_the_per_step_limit(self):
        self._assert_movement_is_rejected(cumulative=True)

    def test_solid_smooth_improves_internal_node_with_freeze_skin(self):
        base = FakeBase()
        self.ansa.base = base
        node = FakeNode(101)
        calls = []

        def smooth_solids(entities, freeze_skin=False):
            calls.append(([entity._id for entity in entities], freeze_skin))
            node.position = (0.05, 0.0, 0.0)

        self.ansa.mesh = types.SimpleNamespace(SolidSmooth=smooth_solids)
        ids = list(range(11, 19))
        entities = [FakeEntity(entity_id, "SOLID") for entity_id in ids]
        before = {
            "scope_sha256": "s" * 64,
            "geometry_sha256": "g" * 64,
            "complete": True,
            "failed_entity_ids": ids,
            "entity_type": "SOLID",
            "rows": [{"id": entity_id, "violations": ["TETRA_COLLAPSE"]}
                     for entity_id in ids],
        }
        after = copy.deepcopy(before)
        after["failed_entity_ids"] = []
        after["rows"] = [{"id": entity_id, "violations": []} for entity_id in ids]
        profile = self.report_profile(
            "solid", [{"name": "TETRA_COLLAPSE", "max": 1.0}]
        )
        validity_value = [
            {"id": entity_id, "measure": 1.0, "jacobian": 0.5}
            for entity_id in ids
        ]
        with patch.object(
            quality_workflow,
            "_report",
            side_effect=[
                (before, entities, {101: node}, {}),
                (after, entities, {101: node}, {}),
            ],
        ), patch.object(
            quality_workflow,
            "_scope",
            return_value=(
                entities,
                {101: node},
                {},
                before["geometry_sha256"],
            ),
        ), patch.object(
            quality_workflow, "_criteria", return_value="native criteria"
        ), patch.object(
            quality_workflow,
            "_validity",
            return_value=validity_value,
        ) as validity, patch.object(
            quality_workflow,
            "_fe_snapshot",
            side_effect=lambda *_args: self.full_snapshot(node, "SOLID"),
        ), patch(
            "ansa_mcp_bridge.operations._model_save",
            return_value={"path": r"F:\backup.ansa", "saved": True},
        ):
            result = quality_workflow.execute(
                self.state,
                "quality_repair",
                self.repair_params(
                    before["scope_sha256"],
                    entity_ids=ids,
                    method="smooth",
                    profile=profile,
                ),
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["applied"])
        self.assertTrue(result["improved"])
        self.assertEqual(result["reason"], "improved")
        self.assertEqual(result["method"], "smooth")
        self.assertIsNone(result["fix_method"])
        self.assertEqual(result["api"], "ansa.mesh.SolidSmooth")
        self.assertEqual(result["changed_node_ids"], [101])
        self.assertEqual(validity.call_count, 2)
        self.assertEqual(result["validity_before"], validity_value)
        self.assertEqual(result["validity_after"], validity_value)
        for call in validity.call_args_list:
            self.assertEqual(call.args[1], entities)
            self.assertEqual(call.args[2], "solid")
        self.assertEqual(calls, [(ids, True)])

    def test_shell_smooth_restores_scoped_fixed_node_and_keeps_movable_move(self):
        self._assert_scoped_fixed_nodes_are_restored("smooth")

    def test_native_fix_restores_scoped_fixed_node_and_keeps_movable_move(self):
        self._assert_scoped_fixed_nodes_are_restored("fix_quality")

    def _assert_scoped_fixed_nodes_are_restored(self, method):
        custom_patch = patch.object(quality_workflow, "_custom_criteria", return_value="custom criteria")
        custom_patch.start()
        self.addCleanup(custom_patch.stop)
        base = FakeBase()
        self.ansa.base = base
        boundary = FakeNode(101)
        center = FakeNode(102)
        calls = []

        def smooth_shells(entities):
            calls.append([entity._id for entity in entities])
            boundary.position = (1.0, 0.0, 0.0)
            center.position = (0.05, 0.0, 0.0)

        self.ansa.mesh = types.SimpleNamespace(SmoothShells=smooth_shells)
        ids = [11, 12, 13, 14]
        entities = [FakeEntity(entity_id) for entity_id in ids]
        base.on_fix = lambda: smooth_shells(entities)
        nodes = {101: boundary, 102: center}
        before = {
            "scope_sha256": "s" * 64,
            "geometry_sha256": "g" * 64,
            "complete": True,
            "failed_entity_ids": ids,
            "entity_type": "SHELL",
            "rows": [{"id": entity_id, "violations": ["ASPECT"]}
                     for entity_id in ids],
        }
        after = copy.deepcopy(before)
        after["failed_entity_ids"] = []
        after["rows"] = [{"id": entity_id, "violations": []} for entity_id in ids]
        validity_value = [
            {"id": entity_id, "measure": 1.0, "jacobian": 0.5}
            for entity_id in ids
        ]

        def snapshot(*_args):
            return {
                "elements": [{
                    "entity": {"deck": 1, "type": "SHELL", "id": 11},
                    "node_ids": [101, 102],
                    "fields": {},
                }],
                "nodes": {
                    "101": {
                        "card": {
                            "entity": {"deck": 1, "type": "GRID", "id": 101},
                            "fields": {"X1": 0.0, "X2": 0.0, "X3": 0.0},
                        },
                        "position": list(boundary.position),
                    },
                    "102": {
                        "card": {
                            "entity": {"deck": 1, "type": "GRID", "id": 102},
                            "fields": {"X1": 0.0, "X2": 0.0, "X3": 0.0},
                        },
                        "position": list(center.position),
                    },
                },
            }

        with patch.object(
            quality_workflow,
            "_report",
            side_effect=[
                (before, entities, nodes, {}),
                (after, entities, nodes, {}),
            ],
        ), patch.object(
            quality_workflow,
            "_scope",
            return_value=(
                entities,
                nodes,
                {},
                before["geometry_sha256"],
            ),
        ), patch.object(
            quality_workflow, "_criteria", return_value="native criteria"
        ), patch.object(
            quality_workflow,
            "_validity",
            return_value=validity_value,
        ), patch.object(
            quality_workflow, "_fe_snapshot", side_effect=snapshot
        ), patch(
            "ansa_mcp_bridge.operations._model_save",
            return_value={"path": r"F:\backup.ansa", "saved": True},
        ):
            result = quality_workflow.execute(
                self.state,
                "quality_repair",
                self.repair_params(
                    before["scope_sha256"],
                    entity_ids=ids,
                    method=method,
                    movable_node_ids=[102],
                ),
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["applied"])
        self.assertTrue(result["improved"])
        self.assertEqual(result["reason"], "improved")
        self.assertEqual(result["method"], method)
        self.assertEqual(result["api"], "ansa.mesh.SmoothShells" if method == "smooth" else "ansa.base.CheckReport.try_fix")
        self.assertEqual(result["changed_node_ids"], [102])
        self.assertEqual(result["restored_fixed_node_ids"], [101])
        self.assertEqual(boundary.position, (0.0, 0.0, 0.0))
        self.assertEqual(center.position, (0.05, 0.0, 0.0))
        self.assertEqual(calls, [ids])

    def test_repair_rejects_allowed_node_shared_with_out_of_scope_fe(self):
        base = FakeBase()
        self.ansa.base = base
        calls = []
        self.ansa.mesh = types.SimpleNamespace(
            SmoothShells=lambda entities: calls.append(entities),
            SolidSmooth=lambda entities, freeze_skin=False: calls.append(entities),
        )
        node = FakeNode(101)
        before = self.repair_before()
        entity = FakeEntity(11)
        outside = {
            "entity": {"deck": 1, "type": "SHELL", "id": 99},
            "node_ids": [101],
            "fields": {},
        }
        snapshot = self.full_snapshot(node, extra_elements=[outside])
        with patch.object(
            quality_workflow,
            "_report",
            return_value=(before, [entity], {101: node}, {}),
        ), patch.object(
            quality_workflow, "_criteria", return_value="native criteria"
        ), patch.object(
            quality_workflow,
            "_validity",
            return_value=[{"id": 11, "measure": 1.0, "jacobian": 0.5}],
        ), patch.object(
            quality_workflow, "_fe_snapshot", return_value=snapshot
        ), patch(
            "ansa_mcp_bridge.operations._model_save"
        ) as save:
            with self.assertRaisesRegex(ValueError, "referenced by FE outside"):
                quality_workflow.execute(
                    self.state,
                    "quality_repair",
                    self.repair_params(
                        before["scope_sha256"], method="smooth"
                    ),
                )
        self.assertEqual(calls, [])
        save.assert_not_called()


class QualityWorkflowSchemaTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, submitted):
        from mcp.server.fastmcp import FastMCP
        from ansa_mcp_plugin.mcp.quality_workflow import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("quality-workflow-schema")
        register(server, submit)
        return server

    def _args(self):
        return {
            "operation_id": "quality-schema-int",
            "expected_target": {
                "backend_id": "b",
                "instance_id": "i",
                "session_id": "s",
                "generation": 1,
            },
            "expected_model": {"epoch": 1},
            "deck": 1,
            "entity_ids": [11],
            "profile": {
                "analysis_type": "linear",
                "element_family": "shell",
                "criteria": [{"name": "ASPECT", "max": 5}],
            },
            "expected_scope_sha256": "a" * 64,
            "backup_path": r"F:\backup.ansa",
            "movable_node_ids": [102],
            "maximum_displacement": 1,
            "method": "smooth",
        }

    async def test_numeric_inputs_accept_int_or_float_and_reject_bool_or_str(self):
        from mcp.server.fastmcp.exceptions import ToolError

        submitted = []
        server = self._server(submitted)
        args = self._args()
        await server.call_tool("ansa_mesh_quality_repair", args)
        params = submitted[-1][1]
        self.assertEqual(params["maximum_displacement"], 1.0)
        self.assertEqual(params["profile"]["criteria"][0]["max"], 5.0)
        self.assertEqual(params["movable_node_ids"], [102])

        float_args = copy.deepcopy(args)
        float_args["operation_id"] = "quality-schema-float"
        float_args["profile"]["criteria"][0]["max"] = 5.5
        float_args["movable_node_ids"] = [102, 103]
        float_args["maximum_displacement"] = 0.5
        await server.call_tool("ansa_mesh_quality_repair", float_args)
        params = submitted[-1][1]
        self.assertEqual(params["maximum_displacement"], 0.5)
        self.assertEqual(params["profile"]["criteria"][0]["max"], 5.5)
        self.assertEqual(params["movable_node_ids"], [102, 103])

        invalid = (
            {"maximum_displacement": True},
            {"maximum_displacement": "1"},
            {"profile": {
                **args["profile"],
                "criteria": [{"name": "ASPECT", "max": True}],
            }},
            {"profile": {
                **args["profile"],
                "criteria": [{"name": "ASPECT", "max": "5"}],
            }},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool(
                        "ansa_mesh_quality_repair", {**args, **changed}
                    )
        self.assertEqual(len(submitted), 2)

    async def test_duplicate_movable_node_ids_are_rejected_before_submission(self):
        from mcp.server.fastmcp.exceptions import ToolError

        submitted = []
        server = self._server(submitted)
        args = self._args()
        args["movable_node_ids"] = [102, 102]

        with self.assertRaises(ToolError):
            await server.call_tool("ansa_mesh_quality_repair", args)
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
