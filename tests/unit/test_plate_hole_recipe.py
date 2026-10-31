import ast
import copy
from pathlib import Path
import unittest

from ansa_mcp_shared.plate_hole import build_plan, seed_for_edge
from ansa_mcp_plugin.workflows.plate_hole import plate_hole_script


def params():
    return {"length":60.,"width":40.,"thickness":6.,"hole_radius":6.,"transition_half_size":9.,
        "target_length":1.5,"thickness_layers":4,"hole_segments":48,"radial_segments":4,"center":[0.,0.,0.]}


class PlateHoleRecipeTests(unittest.TestCase):
    def test_verified_case_counts_and_thickness_variation(self):
        value = params(); before = copy.deepcopy(value)
        plan = build_plan(value)
        self.assertEqual((plan["predicted_source_quads"],plan["predicted_solids"],plan["predicted_nodes"],
            plan["predicted_boundary_shells"],plan["predicted_outer_shells"]),(1168,4672,6300,3920,3072))
        self.assertFalse(plan["usable_for_mutation"])
        self.assertEqual(value,before)
        other=build_plan({**value,"thickness":9.,"thickness_layers":6})
        self.assertEqual(other["predicted_solids"],7008)
        self.assertNotEqual(other["recipe_sha256"],plan["recipe_sha256"])

    def test_nominal_seed_matching_ignores_native_cut_roundoff_only(self):
        points=[[-1.288310667e-6,-9.,3.],[9.,-9.,3.]]
        self.assertEqual(seed_for_edge(points,params()),{"segments":6,"kind":"outer_straight"})
        moved=[[q[i]+[5.,-8.,2.][i] for i in range(3)] for q in points]
        self.assertEqual(seed_for_edge(moved,{**params(),"center":[5.,-8.,2.]})["segments"],6)
        with self.assertRaises(ValueError):seed_for_edge([[0.,-9.,3.],[9.01,-9.,3.]],params())
        self.assertEqual(seed_for_edge([[6.,0.,-3.],[9.,0.,-3.]],params())["kind"],"ring_radial")
        self.assertEqual(seed_for_edge([[6.,0.,-3.],[6.,0.,3.]],params())["segments"],4)

    def test_unsupported_geometry_strict_inputs_and_budgets(self):
        for change in ({"length":True},{"center":[0.,0.,float('nan')]},{"hole_segments":47},
                {"hole_radius":10.},{"target_length":1.},{"thickness_layers":100},{"extra":1}):
            with self.subTest(change=change),self.assertRaises(ValueError):build_plan({**params(),**change})

    def test_fixed_mcp_scripts_do_not_execute_and_apply_requires_model_plan(self):
        ast.parse(plate_hole_script('plan',params()))
        root=Path.cwd()
        code=plate_hole_script('apply',params(),backup_path=str(root/'before.ansa'),
            output_path=str(root/"a'quote.ansa"),expected_plan_sha256='a'*64)
        ast.parse(code)
        self.assertIn('workflow.execute',code)
        for fields in ({'expected_plan_sha256':'recipe'}, {'backup_path':'relative.ansa','expected_plan_sha256':'a'*64}):
            with self.assertRaises(ValueError):plate_hole_script('apply',params(),**fields)
        with self.assertRaises(ValueError):plate_hole_script('plan',params(),backup_path=str(root/'before.ansa'))


if __name__=='__main__':unittest.main()
