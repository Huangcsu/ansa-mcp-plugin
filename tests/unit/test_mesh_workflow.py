import asyncio
import copy
import unittest
from pathlib import Path

from pydantic import TypeAdapter

from ansa_mcp_bridge.operations import mesh_workflow
from ansa_mcp_plugin.mcp.schemas import ToolResult
from ansa_mcp_plugin.mcp.mesh_workflow import ActionRequest, register


def _ref(entity_type, entity_id):
    return {"deck": 1, "type": entity_type, "id": entity_id}


def _node_row(entity_id, x, y=0.0, z=0.0):
    return {
        "entity": {"type": "NODE", "id": entity_id},
        "card_fields": ["X", "Y", "Z"],
        "fields": {"X": x, "Y": y, "Z": z},
    }


def _shell_row(node_ids):
    return {
        "entity": {"type": "SHELL", "id": 10},
        "card_fields": ["type", "PID", "G1", "G2", "G3", "G4"],
        "fields": {
            "type": "QUAD",
            "PID": _ref("SOLID_SECTION", 20),
            **{f"G{index}": _ref("NODE", node_id) for index, node_id in enumerate(node_ids, 1)},
        },
    }


def _solid_row(entity_id, node_ids):
    fields = {
        "type": "HEXA",
        "EID": entity_id,
        "PID": _ref("SOLID_SECTION", 20),
        **{f"G{index}": _ref("NODE", node_id) for index, node_id in enumerate(node_ids, 1)},
    }
    return {
        "entity": {"type": "SOLID", "id": entity_id},
        "card_fields": list(fields),
        "fields": fields,
    }


def _volume_row(entity_id, volume=4000.0):
    fields = {
        "ID": entity_id,
        "PID": _ref("SOLID_SECTION", 20),
        "Type": "Extrude",
        "Algorithm": "Translate",
        "Distribution 1": "Steps         ",
        "Extrude steps": 2,
        "Distance": 20.0,
        "Stitch unconnected source": "NO",
        "Disconnect volume from source": "NO",
        "Connect to target": "NO",
        "Collapse free edges": "NO",
        "Biasing": "None",
        "Status": "Meshed",
        "Order": "1st",
        "Volume": volume,
        "Hexas": 2,
        "Grids": 12,
        "Internal grids": 8,
        "Tetras": 0,
        "Pentas": 0,
        "Pyramids": 0,
        "Polyhedrals": 0,
    }
    return {
        "entity": {"type": "VOLUME", "id": entity_id},
        "card_fields": list(fields),
        "fields": fields,
    }


def _model_snapshot(nodes, shells, solids=(), volumes=()):
    references = [row["entity"] for row in [*nodes, *shells, *solids, *volumes]]
    model = {
        "complete": True,
        "deck": 1,
        "entity_count": len(references),
        "entity_references": references,
        "counts": {},
    }
    categories = {
        "nodes": nodes,
        "shells": shells,
        "solids": list(solids),
        "volumes": list(volumes),
        "faces": [],
        "parts": [],
        "sections": [],
        "materials": [],
    }
    model.update(categories)
    model["counts"] = {name: len(rows) for name, rows in categories.items()}
    return model


class MeshWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.backup = str(Path.cwd() / "mesh-workflow-backup.ansa")
        self.part = {
            "deck": 1,
            "type": "ANSAPART",
            "id": 30,
            "expected_fields": {"Name": "part-a"},
        }
        self.property = {
            "deck": 1,
            "type": "SOLID_SECTION",
            "id": 20,
            "expected_fields": {"MID": _ref("MATERIAL", 40)},
        }

    def _shell(self, entity_id=10, node_ids=(1, 2, 3, 4)):
        return {
            "type": "SHELL",
            "id": entity_id,
            "expected_fields": {
                "type": "QUAD",
                "PID": _ref("SOLID_SECTION", 20),
                **{f"G{index}": _ref("NODE", node_id) for index, node_id in enumerate(node_ids, 1)},
            },
        }

    def _node(self, entity_id, x):
        return {
            "type": "NODE",
            "id": entity_id,
            "expected_fields": {"X": x, "Y": 0.0, "Z": 0.0},
        }

    def _requests(self):
        return [
            {
                "action": "extrude_quad", "deck": 1, "backup_path": self.backup,
                "shells": [self._shell()], "vector": [0.0, 0.0, 2.0], "steps": 2,
                "part": self.part, "property": self.property,
            },
            {
                "action": "mirror_copy", "deck": 1, "backup_path": self.backup,
                "sources": [self._shell()],
                "plane": {"point1": [0, 0, 0], "point2": [1, 0, 0], "point3": [0, 1, 0]},
                "part": self.part,
            },
            {
                "action": "merge_nodes", "deck": 1, "backup_path": self.backup,
                "pairs": [{"source": self._node(1, 0), "target": self._node(2, 0)}],
            },
            {
                "action": "release_mesh", "deck": 1, "backup_path": self.backup,
                "entities": [self._shell()],
            },
        ]

    def _assert_preflight_rejects_before_backup(self, request, resolved, message, error_type=ValueError):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        fake_base = SimpleNamespace(SaveAs=Mock(), PasteNodes=Mock())
        extruder = Mock()
        extruder.translate = Mock(return_value=1)
        fake_mesh = SimpleNamespace(VolumesExtrude=Mock(return_value=extruder))
        fake_ansa = SimpleNamespace(
            base=fake_base,
            constants=SimpleNamespace(ABAQUS=1),
            mesh=fake_mesh,
        )
        state = SimpleNamespace(assert_main=Mock())
        with (
            patch.dict("sys.modules", {"ansa": fake_ansa}),
            patch.object(mesh_workflow.catalog, "_deck", return_value=1),
            patch.object(mesh_workflow.volume_meshing, "_execution_context", return_value=object()),
            patch.object(mesh_workflow.element_types, "_save_path", return_value=(Path(self.backup), None)),
            patch.object(mesh_workflow.element_types, "_database_name", return_value=""),
            patch.object(mesh_workflow, "_capture", return_value={"model": {}, "element_cards": []}),
            patch.object(mesh_workflow, "_resolve_action", return_value=resolved),
            patch.object(mesh_workflow, "_part_membership_snapshot", return_value={"SHELL": [], "SOLID": []}),
            patch.object(mesh_workflow.element_types, "_verify_backup") as verify_backup,
        ):
            with self.assertRaisesRegex(error_type, message):
                mesh_workflow.execute(state, "mesh_workflow", request)
            verify_backup.assert_not_called()
            fake_base.PasteNodes.assert_not_called()
            extruder.translate.assert_not_called()

    def test_all_four_actions_have_explicit_validated_requests(self):
        adapter = TypeAdapter(ActionRequest)
        for request in self._requests():
            with self.subTest(action=request["action"]):
                parsed = adapter.validate_python(request)
                self.assertEqual(parsed.model_dump(mode="json"), request)

    def test_tool_submits_the_action_and_rejects_undeclared_scope(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        submitted = []
        target = {
            "backend_id": "backend", "instance_id": "instance",
            "session_id": "session", "generation": 1,
        }

        def submit(tool, params, operation_id, expected_target, expected_model, ttl):
            submitted.append((tool, params, operation_id, ttl))
            return ToolResult(
                ok=True, status="queued", operation_id=operation_id,
                target=expected_target, model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("mesh-workflow-schema")
        register(server, submit)
        args = {
            "operation_id": "mesh-1", "expected_target": target,
            "expected_model": {"epoch": 1}, "request": self._requests()[0],
        }
        asyncio.run(server.call_tool("ansa_mesh_workflow", args))
        self.assertEqual(submitted[0][0], "mesh_workflow")
        self.assertEqual(submitted[0][1]["action"], "extrude_quad")
        self.assertEqual(submitted[0][2:], ("mesh-1", 300))

        invalid = copy.deepcopy(args)
        invalid["request"]["arbitrary_native_argument"] = "value"
        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("ansa_mesh_workflow", invalid))
        self.assertEqual(len(submitted), 1)

    def test_mirror_native_call_omits_optional_existing_part(self):
        class Base:
            def __init__(self):
                self.kwargs = None

            def GeoMirrorPlane(self, **kwargs):
                self.kwargs = kwargs
                return 0

        base = Base()
        params = {
            "plane": {
                "point1": (0.0, 0.0, 0.0),
                "point2": (1.0, 0.0, 0.0),
                "point3": (0.0, 1.0, 0.0),
            }
        }
        resolved = {"sources": [{"entity": object()}]}
        self.assertEqual(
            mesh_workflow._invoke(base, None, "mirror_copy", params, resolved), 0
        )
        self.assertEqual(base.kwargs["group_offset"], "SAME PART")
        self.assertNotIn("existing_part", base.kwargs)

    def test_extrude_native_call_omits_unverified_connect_target_argument(self):
        from unittest.mock import Mock

        extruder = Mock()
        extruder.translate.return_value = 1
        mesh = Mock()
        mesh.VolumesExtrude.return_value = extruder
        params = {"vector": (0.0, 0.0, 20.0), "steps": 2}
        resolved = {
            "vector_length": 20.0,
            "shells": [{"entity": object()}],
            "part": (object(),),
            "property": (object(),),
        }

        self.assertEqual(mesh_workflow._invoke(None, mesh, "extrude_quad", params, resolved), 1)
        kwargs = extruder.translate.call_args.kwargs
        self.assertNotIn("connect_target", kwargs)
        self.assertEqual(kwargs["source"], [resolved["shells"][0]["entity"]])
        self.assertEqual(kwargs["steps"], 2)
        self.assertEqual(kwargs["distance"], 20.0)
        self.assertIs(kwargs["part"], resolved["part"][0])
        self.assertIs(kwargs["property"], resolved["property"][0])
        self.assertFalse(kwargs["disconnect_from_source"])
        self.assertFalse(kwargs["stitch_sources"])
        self.assertFalse(kwargs["collapse_free_edges"])

    def test_bridge_input_validation_rejects_unsafe_scopes(self):
        for request in self._requests():
            with self.subTest(action=request["action"]):
                normalised = mesh_workflow._normalise_inputs(request)
                self.assertEqual(normalised["action"], request["action"])
                self.assertEqual(normalised["deck"], 1)

        merge = self._requests()[2]
        merge["pairs"][0]["target"] = self._node(1, 0)
        with self.assertRaisesRegex(ValueError, "must differ"):
            mesh_workflow._normalise_inputs(merge)

        extrude = self._requests()[0]
        extrude["vector"] = [0.0, 0.0, 0.0]
        with self.assertRaisesRegex(ValueError, "positive finite length"):
            mesh_workflow._normalise_inputs(extrude)

    def test_release_verification_requires_orphan_nodes_to_remain(self):
        nodes = [_node_row(entity_id, float(entity_id)) for entity_id in (1, 2, 3, 4)]
        shell = _shell_row((1, 2, 3, 4))
        before = {
            "model": _model_snapshot(nodes, [shell]),
            "element_cards": [shell],
        }
        after = {
            "model": _model_snapshot(copy.deepcopy(nodes), []),
            "element_cards": [],
        }
        params = {"action": "release_mesh"}
        resolved = {"entities": [{"reference": {"type": "SHELL", "id": 10}}]}

        errors, result = mesh_workflow._verify_release(params, before, after, resolved)
        self.assertEqual(errors, [])
        self.assertFalse(result["orphan_node_cleanup"])
        self.assertEqual(result["remaining_node_count"], 4)

        after["model"]["nodes"].pop()
        after["model"]["entity_references"].pop()
        after["model"]["entity_count"] -= 1
        after["model"]["counts"]["nodes"] -= 1
        errors, _result = mesh_workflow._verify_release(params, before, after, resolved)
        self.assertTrue(any("NODE set or coordinates" in error for error in errors))

    def test_merge_verification_allows_only_declared_reference_rewrite(self):
        nodes = [_node_row(entity_id, float(entity_id)) for entity_id in (1, 2, 3, 4, 5)]
        shell = _shell_row((1, 3, 4, 5))
        mapped_shell = _shell_row((2, 3, 4, 5))
        before = {
            "model": _model_snapshot(nodes, [shell]),
            "element_cards": [shell],
        }
        after = {
            "model": _model_snapshot(copy.deepcopy(nodes[1:]), [mapped_shell]),
            "element_cards": [mapped_shell],
        }
        resolved = {
            "mapping": {1: 2},
            "pairs": [{"source_id": 1, "target_id": 2}],
        }
        errors, result = mesh_workflow._verify_merge(
            None, 1, {"action": "merge_nodes"}, before, after, resolved
        )
        self.assertEqual(errors, [])
        self.assertEqual(result["updated_element_count"], 1)

        after["element_cards"][0]["fields"]["G2"] = _ref("NODE", 99)
        errors, _result = mesh_workflow._verify_merge(
            None, 1, {"action": "merge_nodes"}, before, after, resolved
        )
        self.assertTrue(any("outside the declared node map" in error for error in errors))

    def test_merge_requires_complete_non_fe_reference_clearance(self):
        resolved = {"mapping": {1: 2}}
        with self.assertRaisesRegex(ValueError, "complete non-FE NODE-reference scan"):
            mesh_workflow._require_merge_reference_clearance({}, resolved)

        clear = {"non_fe_node_references": {"complete": True, "by_node": {1: []}}}
        mesh_workflow._require_merge_reference_clearance(clear, resolved)

        referenced = {
            "non_fe_node_references": {
                "complete": True,
                "by_node": {1: [{"type": "SET", "id": 90}]},
            }
        }
        with self.assertRaisesRegex(ValueError, "has non-FE references"):
            mesh_workflow._require_merge_reference_clearance(referenced, resolved)

    def test_merge_gate_rejects_unverified_references_before_backup(self):
        self._assert_preflight_rejects_before_backup(
            self._requests()[2], {"mapping": {1: 2}}, "ReferenceEntities", NotImplementedError
        )

    def test_merge_native_reference_scan_cross_checks_fe_and_rejects_sets(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        source = object()
        shell = SimpleNamespace(ansa_type=lambda deck: "SHELL", id=10)
        node_set = SimpleNamespace(ansa_type=lambda deck: "SET", id=90)
        base = SimpleNamespace(ReferenceEntities=Mock(return_value=[shell]))
        snapshot = {"element_cards": [_shell_row((1, 3, 4, 5))]}
        resolved = {"mapping": {1: 2}, "pairs": [{"source_id": 1, "source": source}]}
        with patch.object(mesh_workflow.catalog, "_entity_reference", side_effect=lambda b, d, e, t: {"type": t, "id": e.id}):
            evidence = mesh_workflow._merge_reference_evidence(base, 1, snapshot, resolved)
            mesh_workflow._require_merge_reference_clearance({"non_fe_node_references": evidence}, resolved)
            self.assertEqual(evidence["by_node"], {"1": []})
            base.ReferenceEntities.assert_called_once_with(source, group_results=False)
            base.ReferenceEntities.return_value = [shell, node_set]
            evidence = mesh_workflow._merge_reference_evidence(base, 1, snapshot, resolved)
            with self.assertRaisesRegex(ValueError, "has non-FE references"):
                mesh_workflow._require_merge_reference_clearance({"non_fe_node_references": evidence}, resolved)
            base.ReferenceEntities.return_value = []
            with self.assertRaisesRegex(RuntimeError, "disagree with the FE"):
                mesh_workflow._merge_reference_evidence(base, 1, snapshot, resolved)

    def test_mirror_topology_uses_edges_and_faces_not_only_node_set(self):
        quad = {"fields": {"type": "QUAD"}}
        original = mesh_workflow._element_topology("SHELL", quad, (1, 2, 3, 4))
        rotated = mesh_workflow._element_topology("SHELL", quad, (2, 3, 4, 1))
        reversed_for_mirror = mesh_workflow._element_topology("SHELL", quad, (1, 4, 3, 2))
        crossed = mesh_workflow._element_topology("SHELL", quad, (1, 3, 2, 4))
        self.assertEqual(original, rotated)
        self.assertEqual(original, reversed_for_mirror)
        self.assertNotEqual(original, crossed)

        hexa = {"fields": {"type": "HEXA"}}
        mapped = tuple(node_id + 100 for node_id in range(1, 9))
        original_hexa = mesh_workflow._element_topology("SOLID", hexa, mapped)
        rotated_hexa = mesh_workflow._element_topology(
            "SOLID", hexa, (102, 103, 104, 101, 106, 107, 108, 105)
        )
        reversed_hexa = mesh_workflow._element_topology(
            "SOLID", hexa, (101, 104, 103, 102, 105, 108, 107, 106)
        )
        crossed_hexa = mesh_workflow._element_topology(
            "SOLID", hexa, (101, 103, 102, 104, 105, 107, 106, 108)
        )
        self.assertEqual(original_hexa, rotated_hexa)
        self.assertEqual(original_hexa, reversed_hexa)
        self.assertNotEqual(original_hexa, crossed_hexa)

    def test_part_members_include_fe_nested_under_volumes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        part = object()
        base = SimpleNamespace(CollectEntities=Mock(return_value=[]))
        self.assertEqual(mesh_workflow._part_members(base, 1, part, "SOLID"), [])
        base.CollectEntities.assert_called_once_with(1, part, "SOLID", recursive=True)

    def test_part_membership_diff_allows_only_declared_additions(self):
        before = {"part_membership": {"SHELL": [10], "SOLID": [20]}}
        allowed = {"part_membership": {"SHELL": [10, 11], "SOLID": [20, 21]}}
        errors = mesh_workflow._part_membership_errors(
            before, allowed, {"SHELL": {11}, "SOLID": {21}}
        )
        self.assertEqual(errors, [])

        unexpected = {"part_membership": {"SHELL": [10, 11, 12], "SOLID": [20, 21]}}
        errors = mesh_workflow._part_membership_errors(
            before, unexpected, {"SHELL": {11}, "SOLID": {21}}
        )
        self.assertTrue(any("SHELL additions" in error for error in errors))

        removed = {"part_membership": {"SHELL": [], "SOLID": [20, 21]}}
        errors = mesh_workflow._part_membership_errors(
            before, removed, {"SOLID": {21}}
        )
        self.assertTrue(any("SHELL members were removed" in error for error in errors))
        self.assertFalse(any("SOLID additions" in error for error in errors))

    def test_snapshot_capacity_gate_rejects_large_extrusion_before_backup(self):
        params = self._requests()[0]
        resolved = {
            "source_node_ids": list(range(1000)),
            "shells": [{}],
            "part": (object(),),
        }
        self._assert_preflight_rejects_before_backup(
            params,
            resolved,
            "projected post-operation snapshot exceeds the 256 KiB evidence budget",
        )

    def test_snapshot_capacity_projection_includes_the_new_volume(self):
        budget = mesh_workflow._SNAPSHOT_BUDGET
        params = {"action": "extrude_quad", "steps": 1}
        resolved = {"source_node_ids": [1], "shells": [{}]}
        base_projection = 4096 + 512 + 2048
        before = {"padding": "x" * (budget - base_projection - 1024)}
        self.assertLessEqual(len(mesh_workflow._encoded(before)) + base_projection, budget)
        with self.assertRaisesRegex(ValueError, "no backup or native mesh call was made"):
            mesh_workflow._check_snapshot_capacity(before, params, resolved)

    def test_extrusion_verifies_new_volume_card_membership_and_old_volume(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        coordinates = {
            1: (0.0, 0.0, 0.0), 2: (10.0, 0.0, 0.0),
            3: (10.0, 20.0, 0.0), 4: (0.0, 20.0, 0.0),
            5: (0.0, 0.0, 10.0), 6: (10.0, 0.0, 10.0),
            7: (10.0, 20.0, 10.0), 8: (0.0, 20.0, 10.0),
            9: (0.0, 0.0, 20.0), 10: (10.0, 0.0, 20.0),
            11: (10.0, 20.0, 20.0), 12: (0.0, 20.0, 20.0),
        }
        nodes = [_node_row(node_id, *point) for node_id, point in coordinates.items()]
        shell = _shell_row((1, 2, 3, 4))
        solids = [
            _solid_row(101, (1, 2, 3, 4, 5, 6, 7, 8)),
            _solid_row(102, (5, 6, 7, 8, 9, 10, 11, 12)),
        ]
        old_volume = _volume_row(90)
        new_volume = _volume_row(1)
        source_nodes = nodes[:4]
        before = {
            "model": _model_snapshot(source_nodes, [shell], volumes=[old_volume]),
            "element_cards": [shell],
            "part_membership": {"SHELL": [10], "SOLID": [], "VOLUME": [90]},
            "volume_solid_memberships": {"90": []},
        }
        after = {
            "model": _model_snapshot(nodes, [shell], solids, [old_volume, new_volume]),
            "element_cards": [shell, *solids],
            "part_membership": {"SHELL": [10], "SOLID": [101, 102], "VOLUME": [90, 1]},
            "volume_solid_memberships": {"90": [], "1": [101, 102]},
        }
        params = {
            "action": "extrude_quad", "steps": 2, "vector": [0.0, 0.0, 20.0],
            "property": {"id": 20}, "part": {"id": 30},
        }
        resolved = {
            "shells": [{"reference": _ref("SHELL", 10), "node_ids": [1, 2, 3, 4]}],
            "source_node_ids": [1, 2, 3, 4],
            "coordinates": {node_id: coordinates[node_id] for node_id in range(1, 5)},
            "delta": (0.0, 0.0, 10.0),
            "vector_length": 20.0,
            "tolerance": 1e-9,
            "expected_cells": {
                (10, 0): {"volume": 2000.0},
                (10, 1): {"volume": 2000.0},
            },
        }
        volume_entity = object()
        solid_entities = [object(), object()]
        member_ids = {solid_entities[0]: 101, solid_entities[1]: 102}

        def verify(candidate_after, members=solid_entities):
            base = SimpleNamespace(CollectEntities=Mock(return_value=members))
            with (
                patch.object(
                    mesh_workflow.catalog,
                    "_get_entity",
                    return_value=(volume_entity, _ref("VOLUME", 1)),
                ),
                patch.object(
                    mesh_workflow.catalog,
                    "_entity_reference",
                    side_effect=lambda _base, _deck, entity, kind: {
                        "type": kind, "id": member_ids[entity],
                    },
                ),
            ):
                result = mesh_workflow._verify_extrusion(
                    base, 1, params, before, candidate_after, resolved
                )
            return base, result

        base, (errors, output) = verify(after)
        self.assertEqual(errors, [])
        self.assertEqual(output["new_volume_id"], 1)
        self.assertEqual(output["new_volume_solid_count"], 2)
        base.CollectEntities.assert_called_once_with(1, volume_entity, "SOLID")

        wrong_part = copy.deepcopy(after)
        wrong_part["part_membership"]["VOLUME"] = [90]
        _base, (errors, _output) = verify(wrong_part)
        self.assertTrue(any("VOLUME additions" in error for error in errors))

        bad_card = copy.deepcopy(after)
        bad_card["model"]["volumes"][1]["fields"]["Distance"] = 19.0
        _base, (errors, _output) = verify(bad_card)
        self.assertTrue(any("Distance" in error for error in errors))

        _base, (errors, _output) = verify(after, members=solid_entities[:1])
        self.assertTrue(any("SOLID members" in error for error in errors))

        changed_old_volume = copy.deepcopy(after)
        changed_old_volume["model"]["volumes"][0]["fields"]["Status"] = "Unmeshed"
        _base, (errors, _output) = verify(changed_old_volume)
        self.assertTrue(any("original volume 90 card changed" in error for error in errors))

        changed_old_members = copy.deepcopy(after)
        changed_old_members["volume_solid_memberships"]["90"] = [101]
        _base, (errors, _output) = verify(changed_old_members)
        self.assertTrue(any("existing VOLUME 90 SOLID members changed" in error for error in errors))

    def test_volume_member_swap_is_detected_with_equal_counts_and_cards(self):
        before = {"model": {"volumes": [{"entity": {"id": 90}}, {"entity": {"id": 91}}]},
                  "volume_solid_memberships": {"90": [1], "91": [2]}}
        after = copy.deepcopy(before)
        after["volume_solid_memberships"] = {"90": [2], "91": [1]}
        self.assertEqual(len(mesh_workflow._volume_membership_errors(before, after)), 2)

    def test_mirror_rejects_volume_owned_sources_before_call(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        solid = object()
        base = SimpleNamespace(CollectEntities=Mock(side_effect=lambda d, v, kind: [solid] if kind == "SOLID" else []))
        with patch.object(mesh_workflow.catalog, "_get_entity", return_value=(object(), {})), \
             patch.object(mesh_workflow.catalog, "_entity_reference", return_value={"type": "SOLID", "id": 2}):
            with self.assertRaisesRegex(ValueError, "mirror_copy refuses elements owned by a VOLUME"):
                mesh_workflow._reject_volume_membership(base, 1, {"volumes": [{"entity": {"id": 1}}]},
                    [{"reference": {"type": "SOLID", "id": 2}}], action="mirror_copy")


if __name__ == "__main__":
    unittest.main()
