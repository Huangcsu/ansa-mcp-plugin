"""Focused tests for schema-2, read-only CAD sweep candidate diagnostics."""

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TEST_DIR))

from ansa_mcp_bridge.operations.cad_sweep_candidates import discover, subset_witness
from tests.unit.test_cad_sweep_topology import _cube_witness


_SOURCE_FIELDS = (
    "feature_schema", "source_scope", "feature_context", "parts",
    "face_boundaries", "boundaries", "vertices", "curve_lengths",
    "cad_export_sha256",
)


def _rehash(witness):
    payload = {key: witness[key] for key in _SOURCE_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    witness["source_snapshot_sha256"] = hashlib.sha256(encoded).hexdigest()
    return witness


def _feature_witness(native, face_ids=None):
    result = copy.deepcopy({k: native[k] for k in _SOURCE_FIELDS})
    if face_ids is not None:
        selected = set(face_ids)
        result['source_scope']['face_ids'] = sorted(selected)
        result['face_boundaries'] = [r for r in result['face_boundaries'] if r['face_id'] in selected]
        result['parts'] = [r for r in result['parts'] if r['id'] in selected]
        cons = {i for r in result['face_boundaries'] for i in r['cons_ids']}
        result['boundaries'] = [r for r in result['boundaries'] if r['id'] in cons]
        points = {i for r in result['boundaries'] for i in r['endpoint_topology']['hot_point_ids']}
        result['vertices'] = [r for r in result['vertices'] if r['id'] in points]
    return _rehash(result)


def _two_disjoint_boxes():
    first, second = _feature_witness(_cube_witness()), _feature_witness(_cube_witness())
    for r in second['parts']:
        r['id'] += 100
    for r in second['face_boundaries']:
        r['face_id'] += 100
        r['cons_ids'] = [i+100 for i in r['cons_ids']]
    for r in second['boundaries']:
        r['id'] += 100
        r['face_ids'] = [i+100 for i in r['face_ids']]
        r['endpoint_topology']['hot_point_ids'] = [i+1000 for i in r['endpoint_topology']['hot_point_ids']]
    for r in second['vertices']:
        r['id'] += 1000
        r['cons_ids'] = [i+100 for i in r['cons_ids']]
        r['position'][0] += 10.
    first['source_scope']['face_ids'] += [i+100 for i in second['source_scope']['face_ids']]
    for key in ('parts','face_boundaries','boundaries','vertices'):
        first[key] += second[key]
    return _rehash(first)


def _refresh_chord_lengths(witness):
    vertices = {row["id"]: row["position"] for row in witness["vertices"]}
    for boundary in witness["boundaries"]:
        endpoints = boundary["endpoint_topology"]["hot_point_ids"]
        if len(endpoints) == 2:
            boundary["native_card_length"] = math.dist(
                vertices[endpoints[0]], vertices[endpoints[1]])
    return _rehash(witness)


class CadSweepCandidateTests(unittest.TestCase):
    def test_rotated_box_finds_all_three_unordered_cap_pairs(self):
        witness = _feature_witness(_cube_witness(direction=(2.0, 4.0, 6.0)))
        result = discover(witness, 0.75)

        self.assertEqual(result["coverage"]["classified_face_ids"],
                         [11, 12, 13, 14, 15, 16])
        self.assertEqual(result["coverage"]["unclassified_face_ids"], [])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(len(result["components"]), 1)
        component = result["components"][0]
        self.assertEqual(component["status"], "topology_candidate")
        self.assertEqual(len(component["alternatives"]), 3)
        self.assertTrue(all(row["map_options"]["source_face_id"]
                            < row["map_options"]["target_face_id"]
                            for row in component["alternatives"]))
        for key in ("usable_for_mutation", "exact_native_CAD_equivalence_proven",
                    "continuous_export_domain_verified"):
            self.assertFalse(result[key])
            self.assertFalse(component[key])

    def test_two_disjoint_boxes_in_the_same_part_remain_two_components(self):
        result = discover(_feature_witness(_two_disjoint_boxes()), 1.0)

        self.assertEqual(len(result["components"]), 2)
        self.assertEqual([row["status"] for row in result["components"]],
                         ["topology_candidate", "topology_candidate"])
        self.assertEqual([row["part_ids"] for row in result["components"]],
                         [[9], [9]])
        self.assertEqual(result["coverage"]["unclassified_face_ids"], [])

    def test_partial_scope_keeps_external_face_adjacency_and_is_unsupported(self):
        legacy = _cube_witness()
        scope = [11, 13, 14, 15, 16]
        witness = _feature_witness(legacy, scope)
        result = discover(witness, 1.0)

        component = result["components"][0]
        self.assertEqual(component["status"], "unsupported")
        self.assertIn("exactly six FACEs", component["reason"])
        self.assertIn("outside the component", component["reason"])
        self.assertEqual(component["alternatives"], [])
        self.assertEqual(result["coverage"]["unsupported_face_ids"], scope)

    def test_hot_point_cons_adjacency_damage_raises_value_error(self):
        witness = _feature_witness(_cube_witness())
        witness["vertices"][0]["cons_ids"].append(2)
        _rehash(witness)

        with self.assertRaisesRegex(ValueError, "HOT POINT/CONS adjacency"):
            discover(witness, 1.0)

    def test_nonplanar_and_variable_section_six_face_components_are_rejected(self):
        nonplanar = _feature_witness(_cube_witness())
        point = next(row for row in nonplanar["vertices"] if row["id"] == 101)
        point["position"][2] += 0.125
        _refresh_chord_lengths(nonplanar)

        variable = _feature_witness(_cube_witness())
        for point in variable["vertices"]:
            if point["id"] in (201, 202, 203, 204):
                point["position"][0] *= 1.25
                point["position"][1] *= 1.25
        _refresh_chord_lengths(variable)

        for witness in (nonplanar, variable):
            result = discover(witness, 1.0)
            self.assertEqual(result["components"][0]["status"], "unsupported")
            self.assertEqual(result["components"][0]["alternatives"], [])

    def test_seed_groups_cover_each_cons_once_and_keep_over_budget_candidate(self):
        witness = _feature_witness(_cube_witness())
        result = discover(witness, 0.75)
        alternative = result["components"][0]["alternatives"][0]
        seed = alternative["seed_proposal"]

        self.assertEqual(sorted(group["steps"] for group in seed["edge_groups"]),
                         [3, 4])
        self.assertEqual(seed["predicted_source_quads"], 12)
        self.assertEqual(seed["predicted_solids"], 48)
        self.assertEqual(seed["predicted_nodes"], 100)
        assignments = seed["cons_steps"]
        self.assertEqual(len(assignments), 12)
        self.assertEqual(len({row["cons_id"] for row in assignments}), 12)
        self.assertTrue(seed["supported_by_current_map_limits"])
        for group in seed["edge_groups"]:
            self.assertEqual(len(group["source_cons_ids"]), 2)
            self.assertEqual(len(group["target_cons_ids"]), 2)
            for cons_id in group["cons_ids"]:
                assigned = next(row["steps"] for row in assignments
                                if row["cons_id"] == cons_id)
                self.assertEqual(assigned, group["steps"])

        over_budget = discover(witness, 0.01)["components"][0]
        self.assertEqual(over_budget["status"], "topology_candidate")
        self.assertEqual(len(over_budget["alternatives"]), 3)
        self.assertFalse(over_budget["alternatives"][0]["seed_proposal"]
                         ["supported_by_current_map_limits"])

    def test_order_is_deterministic_and_subset_hash_is_bound_to_parent(self):
        witness = _feature_witness(_cube_witness())
        first = discover(witness, 1.0)
        self.assertEqual(discover(copy.deepcopy(witness), 1.0), first)

        reordered = copy.deepcopy(witness)
        for key in ("face_boundaries", "parts", "boundaries", "vertices"):
            reordered[key].reverse()
        reordered["source_scope"]["face_ids"].reverse()
        _rehash(reordered)
        second = discover(reordered, 1.0)
        self.assertEqual(second["components"], first["components"])
        self.assertNotEqual(second["source_snapshot_sha256"],
                            first["source_snapshot_sha256"])

        subset = subset_witness(witness, [16, 15, 14, 13, 12, 11])
        self.assertTrue(subset["derived_topology_only"])
        self.assertEqual(subset["parent_source_snapshot_sha256"],
                         witness["source_snapshot_sha256"])
        self.assertNotIn("source_snapshot_sha256", subset)
        source_boundaries = {row["id"]: row for row in witness["boundaries"]}
        for boundary in subset["boundaries"]:
            self.assertEqual(boundary["face_ids"],
                             source_boundaries[boundary["id"]]["face_ids"])
        source_vertices = {row["id"]: row for row in witness["vertices"]}
        for vertex in subset["vertices"]:
            self.assertEqual(vertex["cons_ids"],
                             source_vertices[vertex["id"]]["cons_ids"])

        other_parent = copy.deepcopy(witness)
        other_parent["feature_context"]["database"] = "other.ansa"
        _rehash(other_parent)
        other_subset = subset_witness(other_parent, [11, 12, 13, 14, 15, 16])
        self.assertNotEqual(subset["derived_topology_sha256"],
                            other_subset["derived_topology_sha256"])

    def test_source_digest_and_target_length_are_strictly_validated(self):
        witness = _feature_witness(_cube_witness())
        damaged = copy.deepcopy(witness)
        damaged["vertices"][0]["position"][0] += 1.0
        with self.assertRaisesRegex(ValueError, "source_snapshot_sha256"):
            discover(damaged, 1.0)

        for target_length in (True, False, 0, -1.0, float("nan"), float("inf")):
            with self.subTest(target_length=target_length):
                with self.assertRaises(ValueError):
                    discover(witness, target_length)


if __name__ == "__main__":
    unittest.main()
