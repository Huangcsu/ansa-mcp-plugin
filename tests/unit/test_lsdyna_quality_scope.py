"""LS-DYNA writes remain limited to verified native profiles and element order."""

import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import quality_workflow, native_quality


class LsdynaQualityScopeTests(unittest.TestCase):
    def ansa(self):
        module = types.ModuleType("ansa")
        module.base = types.SimpleNamespace(CurrentDeck=lambda: 2)
        module.constants = types.SimpleNamespace(NASTRAN=1, ABAQUS=4, LSDYNA=2)
        return module

    def test_unverified_profile_is_rejected_before_report_or_backup(self):
        profiles = ({"analysis_type": "linear", "criteria": [{"name": "ASPECT", "max": 3.}]},
                    {"template": "linear_shell"}, {"template": "linear_solid"})
        for tool in ("quality_locate", "quality_repair"):
            for profile in profiles:
                with self.subTest(tool=tool, profile=profile), \
                     patch.dict(sys.modules, {"ansa": self.ansa()}), \
                     patch.object(native_quality, "report") as native, \
                     patch.object(quality_workflow, "_report") as legacy:
                    params = {"deck": 2, "entity_ids": [1], "profile": profile,
                              "expected_scope_sha256": "a" * 64}
                    if tool == "quality_repair":
                        params.update(backup_path="C:/test.ansa", movable_node_ids=[1], maximum_displacement=1.)
                    with self.assertRaisesRegex(ValueError, "explicit_crash native template"):
                        quality_workflow.execute(types.SimpleNamespace(assert_main=lambda: None), tool, params)
                    native.assert_not_called()
                    legacy.assert_not_called()

    def test_unsupported_element_order_is_rejected_before_native_repair(self):
        for family, count in (("shell", 3), ("shell", 8), ("solid", 4), ("solid", 20)):
            with self.subTest(family=family, count=count), \
                 patch.dict(sys.modules, {"ansa": self.ansa()}), \
                 patch.object(native_quality, "report", return_value=(
                    {"scope_sha256": "a" * 64}, [], {}, {"elements": [{"node_ids": list(range(count))}]})), \
                 patch.object(quality_workflow, "_repair") as repair:
                params = {"deck": 2, "entity_ids": [1], "profile": {
                    "template": "explicit_crash_" + family, "target_time_step": 1.e-7},
                    "expected_scope_sha256": "a" * 64, "backup_path": "C:/test.ansa",
                    "movable_node_ids": [1], "maximum_displacement": 1.}
                with self.assertRaisesRegex(ValueError, "QUAD4/HEXA8 only"):
                    quality_workflow.execute(types.SimpleNamespace(assert_main=lambda: None), "quality_repair", params)
                repair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
