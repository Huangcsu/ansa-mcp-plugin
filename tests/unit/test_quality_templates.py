"""原生质量模板和 MCP profile 契约的离线测试。"""

import math
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from ansa_mcp_shared.quality_templates import get_template, normalize_template_profile
from ansa_mcp_plugin.mcp import quality_workflow
from ansa_mcp_plugin.mcp.quality_workflow import NativeQualityProfile, QualityProfile
from ansa_mcp_plugin.mcp.schemas import ExpectedModel, ExpectedTarget


class QualityTemplateTests(unittest.TestCase):
    def test_four_templates_derive_analysis_family_and_required_native_criteria(self):
        expected = {
            "linear_shell": ("linear", "shell"),
            "linear_solid": ("linear", "solid"),
            "explicit_crash_shell": ("explicit_crash", "shell"),
            "explicit_crash_solid": ("explicit_crash", "solid"),
        }
        for template, (analysis_type, family) in expected.items():
            with self.subTest(template=template):
                raw = {"template": template}
                if analysis_type == "explicit_crash":
                    raw["target_time_step"] = 0.0001
                profile = normalize_template_profile(raw)
                directory = get_template(template)
                self.assertEqual(profile["analysis_type"], analysis_type)
                self.assertEqual(profile["element_family"], family)
                self.assertEqual(directory["analysis_type"], analysis_type)
                self.assertEqual(directory["element_family"], family)
                self.assertEqual(profile["template"], directory["id"])
                self.assertTrue(directory["required_criteria"])
                self.assertTrue(all(type(name) is str for name in directory["required_criteria"]))
                self.assertEqual(directory["reference_solver"],
                                 "ABAQUS" if analysis_type == "linear" else "LS-DYNA")

        shell = get_template("linear_shell")["required_criteria"]
        self.assertIn("aspect ratio", shell)
        self.assertIn("min angle quads", shell)
        self.assertIn("min angle trias", shell)
        solid = get_template("linear_solid")["required_criteria"]
        self.assertIn("negative volume", solid)
        self.assertIn("left handed", solid)
        self.assertIn("max angle hexas", solid)
        crash_shell = get_template("explicit_crash_shell")["required_criteria"]
        self.assertIn("characteristic length", crash_shell)
        self.assertIn("crash time step", crash_shell)
        crash_solid = get_template("explicit_crash_solid")["required_criteria"]
        self.assertIn("collapse", crash_solid)
        self.assertIn("crash time step", crash_solid)

    def test_profile_hash_is_stable_after_numeric_and_key_order_normalization(self):
        first = normalize_template_profile({
            "template": "explicit_crash_shell",
            "target_time_step": 0.001,
            "quality_criteria": "aspect ratio [shells] = ON,|source|",
            "criteria_label": "project-A",
            "formulation": "ELFORM=2",
        })
        second = normalize_template_profile({
            "formulation": "ELFORM=2",
            "criteria_label": "project-A",
            "quality_criteria": "aspect ratio [shells] = ON,|source|",
            "target_time_step": 0.0010,
            "template": "explicit_crash_shell",
        })
        self.assertEqual(first, second)
        self.assertEqual(len(first["profile_sha256"]), 64)
        changed = normalize_template_profile({
            "template": "explicit_crash_shell",
            "target_time_step": 0.002,
            "quality_criteria": "aspect ratio [shells] = ON,|source|",
            "criteria_label": "project-A",
            "formulation": "ELFORM=2",
        })
        self.assertNotEqual(first["profile_sha256"], changed["profile_sha256"])

    def test_normalizer_rejects_legacy_mixed_fields_and_unknown_keys(self):
        invalid = (
            {"template": "linear_shell", "analysis_type": "linear"},
            {"template": "linear_solid", "element_family": "solid"},
            {"template": "linear_shell", "criteria": [{"name": "ASPECT", "max": 5}]},
            {"template": "linear_shell", "minimum_length": 0.1},
            {"template": "linear_shell", "unexpected": True},
            {"template": "linear_shell", "quality_criteria": None},
            {"template": "linear_shell", "formulation": ""},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_template_profile(raw)

    def test_target_time_step_is_required_only_for_crash_and_is_a_positive_json_number(self):
        with self.assertRaisesRegex(ValueError, "require target_time_step"):
            normalize_template_profile({"template": "explicit_crash_shell"})
        with self.assertRaisesRegex(ValueError, "forbidden"):
            normalize_template_profile({"template": "linear_shell", "target_time_step": 0.1})
        for value in (True, False, 0, -0.1, math.nan, math.inf, "0.1", 10**1000):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_template_profile({
                    "template": "explicit_crash_solid",
                    "target_time_step": value,
                })
        profile = normalize_template_profile({
            "template": "explicit_crash_solid",
            "target_time_step": 2,
        })
        self.assertEqual(profile["target_time_step"], 2.0)

    def test_templates_have_no_fabricated_limits_and_are_returned_as_deep_copies(self):
        for template in (
            "linear_shell", "linear_solid", "explicit_crash_shell", "explicit_crash_solid"
        ):
            with self.subTest(template=template):
                directory = get_template(template)
                self.assertEqual(
                    set(directory),
                    {"id", "analysis_type", "element_family", "reference_solver",
                     "source_url", "required_criteria", "notes"},
                )
                self.assertTrue(directory["source_url"].startswith("https://"))
                self.assertNotIn("thresholds", directory)
                self.assertNotIn("criteria", directory)
                self.assertTrue(all("=" not in criterion for criterion in directory["required_criteria"]))
                self.assertTrue(directory["notes"])

        self.assertTrue(any("Mesh Verify" in note for note in get_template("linear_shell")["notes"]))
        crash_shell = get_template("explicit_crash_shell")
        self.assertIn("lsdyna.ansys.com/time-step-size", crash_shell["source_url"])
        self.assertTrue(any("最短边" in note for note in crash_shell["notes"]))
        first = get_template("linear_shell")
        first["required_criteria"].clear()
        self.assertTrue(get_template("linear_shell")["required_criteria"])

        normalized = normalize_template_profile({"template": "linear_shell"})
        self.assertIsNone(normalized["quality_criteria"])
        self.assertIsNone(normalized["target_time_step"])
        self.assertEqual(normalized["criteria_label"], "current_session")

    def test_native_text_and_metadata_length_limits_are_enforced_without_rewriting_text(self):
        criteria = "x" * 65536
        profile = NativeQualityProfile.model_validate({
            "template": "linear_shell",
            "quality_criteria": criteria,
            "criteria_label": "",
            "formulation": "f" * 64,
        })
        self.assertEqual(profile.quality_criteria, criteria)
        self.assertEqual(profile.criteria_label, "")
        self.assertEqual(profile.formulation, "f" * 64)
        for payload in (
            {"template": "linear_shell", "quality_criteria": criteria + "x"},
            {"template": "linear_shell", "criteria_label": "c" * 129},
            {"template": "linear_shell", "formulation": "f" * 65},
            {"template": "linear_shell", "target_time_step": None},
        ):
            with self.subTest(payload={key: (len(value) if isinstance(value, str) else value)
                                       for key, value in payload.items()}), \
                    self.assertRaises(ValidationError):
                NativeQualityProfile.model_validate(payload)

    def test_mcp_union_rejects_mixed_profiles_and_forwards_legacy_and_native_inputs(self):
        profile_adapter = TypeAdapter(QualityProfile | NativeQualityProfile)
        native_payload = {
            "template": "explicit_crash_shell",
            "target_time_step": 0.0002,
            "quality_criteria": "full native criteria text",
        }
        native_profile = profile_adapter.validate_python(native_payload)
        self.assertIsInstance(native_profile, NativeQualityProfile)
        with self.assertRaises(ValidationError):
            profile_adapter.validate_python({**native_payload, "analysis_type": "explicit_crash"})
        with self.assertRaises(ValidationError):
            profile_adapter.validate_python({
                "analysis_type": "linear",
                "element_family": "shell",
                "criteria": [{"name": "ASPECT", "max": 5}],
                "formulation": "ELFORM=2",
            })

        class FakeServer:
            def __init__(self):
                self.tools = {}

            def tool(self, *, name, structured_output):
                def decorate(function):
                    self.tools[name] = function
                    return function
                return decorate

        submitted = []

        def submit(*args):
            submitted.append(args)
            return "queued"

        server = FakeServer()
        with patch.object(quality_workflow, "_forbid_extra_arguments"):
            quality_workflow.register(server, submit)

        target = ExpectedTarget(
            backend_id="backend", instance_id="instance", session_id="session", generation=1
        )
        model = ExpectedModel(epoch=1)
        report = server.tools["ansa_mesh_quality_report"](
            operation_id="report-1",
            expected_target=target,
            expected_model=model,
            deck=1,
            entity_ids=[11],
            profile=native_profile,
        )
        self.assertEqual(report, "queued")
        self.assertEqual(submitted[-1][1]["profile"], native_profile.model_dump(exclude_none=True))

        legacy_profile = QualityProfile(
            analysis_type="linear",
            element_family="shell",
            criteria=[{"name": "ASPECT", "max": 5}],
        )
        server.tools["ansa_mesh_quality_repair"](
            operation_id="repair-1",
            expected_target=target,
            expected_model=model,
            deck=1,
            entity_ids=[11],
            profile=legacy_profile,
            expected_scope_sha256="a" * 64,
            backup_path=r"C:\backup.ansa",
            movable_node_ids=[21],
            maximum_displacement=0.1,
        )
        repair_payload = submitted[-1][1]
        self.assertEqual(repair_payload["profile"], legacy_profile.model_dump(exclude_none=True))
        self.assertEqual(repair_payload["method"], "smooth")

        server.tools["ansa_mesh_quality_repair"](
            operation_id="repair-2",
            expected_target=target,
            expected_model=model,
            deck=1,
            entity_ids=[11],
            profile=native_profile,
            expected_scope_sha256="a" * 64,
            backup_path=r"C:\backup.ansa",
            movable_node_ids=[21],
            maximum_displacement=0.1,
            method="smooth",
        )
        self.assertEqual(submitted[-1][1]["method"], "smooth")


if __name__ == "__main__":
    unittest.main()
