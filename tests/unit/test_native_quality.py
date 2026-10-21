"""Native rules stay authoritative and missing checks cannot pass."""

import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import native_quality, quality_workflow


def criteria_for(template, *, active=True):
    from ansa_mcp_shared.quality_templates import get_template
    policy = get_template(template)
    family = "shells" if policy["element_family"] == "shell" else "solids"
    lines = ["ANSA_Version = 26.10.000", "failed_index_shells = 2,", "failed_index_solids = 2,"]
    for name in policy["required_criteria"]:
        calculation = "LS-DYNA" if name in {"crash time step", "characteristic length"} else "ANSA"
        limit = "BLANK" if name in {"incomplete element", "negative volume", "left handed"} else "0.5"
        line = f"{name} [{family}] = {'ON' if active else 'OFF'}, {calculation} : 0XFFFFFF | 1. | "
        lines.append(line + " ".join(limit+", -1./" for _ in range(4)))
    return "\n".join(lines)


class Entity:
    def __init__(self, eid=11, kind="SHELL"):
        self._id, self.kind = eid, kind


class NativeReport:
    def __init__(self, description, entities=(), children=(), status="error"):
        self.type, self.description, self.status = "Mesh: Mesh Quality", description, status
        self.entities, self.issues, self.has_fix = list(entities), list(children), True


class NativeBase:
    Check = types.SimpleNamespace(EXEC_ON_SELECTED=4, REPORT_NONE=0, KEEP_OLD=8)
    def __init__(self, text, reports):
        self.text, self.reports, self.timestep, self.executed = text, reports, 2., []
        self.checks = types.SimpleNamespace(mesh=types.SimpleNamespace(MeshQuality=self.make_check))
    def CurrentDeck(self):
        return 1
    def ElementQuality(self, entity, metrics):
        return [self.timestep]
    def make_check(self):
        base = self
        class Check:
            use_criteria = "Use global criteria"
            quality_criteria = base.text
            fix_method = 2
            def parameters(self):
                return {"quality_criteria": base.text}
            def is_available_in_deck(self, deck):
                return True
            def execute(self, **kwargs):
                base.executed.append(kwargs)
                return base.reports
        return Check()


class NativeQualityTests(unittest.TestCase):
    def profile(self, template, **extra):
        from ansa_mcp_shared.quality_templates import normalize_template_profile
        raw = {"template":template, **extra}
        if template.startswith("explicit_crash"):
            raw["target_time_step"] = 1.
        return normalize_template_profile(raw)

    def run_report(self, base, profile, dependencies="d1"):
        entity = Entity(kind=profile["element_family"].upper())
        snapshot = {"elements":[{"entity":{"id":11},"node_ids":[101]}],"node_positions":{"101":[0.,0.,0.]}}
        with patch.object(quality_workflow,"_scope",return_value=([entity],{101:Entity(101,"GRID")},snapshot,"geometry")), \
             patch.object(native_quality,"_dependencies",return_value=dependencies), \
             patch.object(native_quality.inspection,"_entity_reference",side_effect=lambda _b,d,e:{"deck":d,"type":e.kind,"id":e._id}):
            return native_quality.report(base,types.SimpleNamespace(NASTRAN=1),1,profile,[11])[0]

    def test_four_templates_execute_native_check_and_preserve_global_criteria(self):
        for template in ("linear_shell","linear_solid","explicit_crash_shell","explicit_crash_solid"):
            with self.subTest(template=template):
                text = criteria_for(template)
                base = NativeBase(text,[])
                result = self.run_report(base,self.profile(template))
                self.assertTrue(result["complete"])
                self.assertTrue(result["passed"])
                self.assertEqual(result["engine"],"ANSA_MeshQuality")
                self.assertEqual(base.text,text)
                self.assertEqual(len(base.executed),1)
                self.assertEqual(base.executed[0]["exec_mode"],4)

    def test_all_off_or_missing_required_native_checks_do_not_pass(self):
        text = criteria_for("linear_shell",active=False)
        result = self.run_report(NativeBase(text,[]),self.profile("linear_shell"))
        self.assertFalse(result["complete"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["unknown_entity_ids"],[11])
        self.assertIn("jacobian",result["criteria_snapshot"]["missing_criteria"])

    def test_unknown_calculation_name_is_rejected_before_native_execute(self):
        text = criteria_for("linear_shell")
        changed = text.replace("ON, ANSA :","ON, INVALID :",1)
        base = NativeBase(text,[])
        with self.assertRaisesRegex(ValueError,"calculation must match"):
            self.run_report(base,self.profile("linear_shell",quality_criteria=changed))
        self.assertEqual(base.executed,[])
        crash = criteria_for("explicit_crash_shell")
        changed = crash.replace("crash time step [shells] = ON, LS-DYNA", "crash time step [shells] = OFF, INVALID")
        base = NativeBase(crash,[])
        with self.assertRaisesRegex(ValueError,"calculation must match"):
            self.run_report(base,self.profile("explicit_crash_shell",quality_criteria=changed))
        self.assertEqual(base.executed,[])

    def test_native_failure_remains_authoritative_over_supplementary_timestep(self):
        text = criteria_for("explicit_crash_shell")
        leaf = NativeReport("aspect ratio shells 1 off in Failed Range",[Entity()])
        base = NativeBase(text,[NativeReport("Mesh: Mesh Quality",children=[leaf])])
        result = self.run_report(base,self.profile("explicit_crash_shell"))
        self.assertTrue(result["complete"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["failed_entity_ids"],[11])
        self.assertEqual(result["rows"][0]["violations"],["aspect ratio"])
        self.assertEqual(result["rows"][0]["metrics"]["CRASH"],2.)

    def test_material_dependent_hash_changes_and_missing_timestep_is_unknown(self):
        text = criteria_for("explicit_crash_solid")
        profile = self.profile("explicit_crash_solid")
        base = NativeBase(text,[])
        first = self.run_report(base,profile,"d1")
        second = self.run_report(base,profile,"d2")
        self.assertNotEqual(first["scope_sha256"],second["scope_sha256"])
        base.timestep = 0.
        third = self.run_report(base,profile)
        self.assertFalse(third["complete"])
        self.assertFalse(third["passed"])
        self.assertEqual(third["rows"][0]["unknown"],["CRASH"])

    def test_outside_entities_are_rejected_and_unmapped_issues_are_unknown(self):
        text = criteria_for("linear_shell")
        outside = NativeReport("aspect ratio shells 1 off in Failed Range",[Entity(99)])
        base = NativeBase(text,[NativeReport("Mesh: Mesh Quality",children=[outside])])
        with self.assertRaisesRegex(RuntimeError,"outside"):
            self.run_report(base,self.profile("linear_shell"))
        base.reports = [NativeReport("unknown diagnostic",[Entity()])]
        result = self.run_report(base,self.profile("linear_shell"))
        self.assertFalse(result["complete"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
