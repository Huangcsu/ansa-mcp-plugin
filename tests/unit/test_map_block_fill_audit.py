import copy
import unittest

from ansa_mcp_bridge.operations.map_block_fill_audit import audit_changes, audit_many_changes


PART_VALUES = {
    "Mass": 2.5,
    "COG x": 0.0,
    "COG y": 0.25,
    "COG z": 0.5,
    "Ixx": 1.0,
    "Iyy": 2.0,
    "Izz": 3.0,
    "Ixy": 0.0,
    "Ixz": 0.0,
    "Iyz": 0.0,
}


def _entity(kind, ident, fields, *, part_id=None, solid_ids=None):
    row = {
        "card_fields": list(fields),
        "entity": {"deck": 1, "id": ident, "type": kind},
        "fields": fields,
    }
    if part_id is not None:
        row["part_id"] = part_id
    if solid_ids is not None:
        row["solid_ids"] = solid_ids
    return row


def _grid(ident, xyz):
    fields = {
        "TYPE": "GRID",
        "NID": ident,
        "CP": 0,
        "CD": None,
        "PS": None,
        "SEID": None,
        "X1": xyz[0],
        "X2": xyz[1],
        "X3": xyz[2],
    }
    return _entity("GRID", ident, fields)


def _snapshot(entities):
    grid_rows = [row for row in entities if row["entity"]["type"] == "GRID"]
    return {
        "cad_sha256": "a" * 64,
        "entities": entities,
        "grids": [
            {
                "card": {
                    key: row[key] for key in ("entity", "card_fields", "fields")
                },
                "id": row["entity"]["id"],
            }
            for row in grid_rows
        ],
        "perimeters": [{"shell_ids": [20], "grid_ids": [1, 2, 3, 4]}],
    }


def _fixture():
    before_part = dict(PART_VALUES)
    before_part.update({"COG x": -2e-16, "Ixx": 1.1, "Last Edit": "26-10月-2026 08:55:35", "Area": 12.0})
    after_part = {**PART_VALUES, "Last Edit": "26-10月-2026 09:24:14", "Area": 12.0}
    volume_common = {
        "ID": 1,
        "PID": 2,
        "Type": "Map Block",
        "Frozen": "NO",
        "Volume": 8.0,
        "Mass": 2.5,
        "Pentas": 0,
        "Tetras": 0,
        "Pyramids": 0,
        "Polyhedrals": 0,
        "Comment": "",
    }
    before_volume = {
        **volume_common,
        "State": "Ready to Mesh",
        "Status": "Unmeshed",
        "Order": "",
        "Hexas": 0,
        "Grids": 0,
        "Internal grids": 0,
    }
    after_volume = {
        **volume_common,
        "State": "Meshed",
        "Status": "Meshed",
        "Order": "1st",
        "Hexas": 1,
        "Grids": 8,
        "Internal grids": 4,
    }
    volume_card = list(before_volume)

    before_entities = [
        _entity("ANSAPART", 1, before_part),
        _entity("PSOLID", 2, {"PID": 2, "MID": 9, "Num.Elem": 0, "Name": "target"}),
        _entity("PSHELL", 3, {"PID": 3, "MID1": 8, "T": 0.2, "Num.Elem": 88}),
        _entity("MAT1", 8, {"MID": 8, "E": 70000.0, "NU": 0.3, "Name": "shell material"}),
        _entity("MAT1", 9, {"MID": 9, "E": 210000.0, "NU": 0.29, "Name": "unused material"}),
        _entity("SHELL", 20, {"EID": 20, "PID": 3, "type": "CQUAD4", "G1": 1, "G2": 2, "G3": 3, "G4": 4}),
        _entity("VOLUME", 1, before_volume, part_id=1, solid_ids=[]),
        *[_grid(i, (float(i), float(i + 1), 0.0)) for i in range(1, 5)],
    ]
    after_entities = copy.deepcopy(before_entities)
    next(row for row in after_entities if row["entity"]["type"] == "ANSAPART")["fields"] = after_part
    next(row for row in after_entities if row["entity"]["type"] == "PSOLID")["fields"]["Num.Elem"] = 1
    volume_row = next(row for row in after_entities if row["entity"]["type"] == "VOLUME")
    volume_row["fields"] = after_volume
    volume_row["solid_ids"] = [100]

    coordinates = {
        5: (5.0, 0.0, 0.0),
        6: (6.0, 0.0, 0.0),
        7: (7.0, 0.0, 0.0),
        8: (8.0, 0.0, 0.0),
    }
    after_entities.extend(_grid(i, xyz) for i, xyz in coordinates.items())
    solid_fields = {"type": "CHEXA", "EID": 100, "PID": 2}
    solid_fields.update({f"G{i}": i for i in range(1, 9)})
    after_entities.append(_entity("SOLID", 100, solid_fields, part_id=1))

    before = _snapshot(before_entities)
    after = _snapshot(after_entities)
    output = {
        "complete": True,
        "deck": 1,
        "elements": [{
            "id": 100,
            "type": "CHEXA",
            "nodes": list(range(1, 9)),
            "property_id": 2,
            "block_id": "VOLUME:1",
        }],
        "integrity": {
            "complete": True,
            "requested_element_count": 1,
            "element_count": 1,
            "node_count": 8,
            "missing_node_ids": [],
            "element_types": {"CHEXA": 1},
        },
        "nodes": [
            {"id": i, "xyz": [float(i), float(i + 1), 0.0]} if i < 5
            else {"id": i, "xyz": list(coordinates[i])}
            for i in range(1, 9)
        ],
        "ownership": {
            "origin": "native_VOLUME_membership",
            "complete": True,
            "known_count": 1,
        },
        "snapshot_sha256": "b" * 64,
    }
    aggregate = {
        "origin": "CalcElementMass(Part)",
        "part_id": 1,
        "values": dict(PART_VALUES),
    }
    args = {
        "volume_id": 1,
        "part_id": 1,
        "property_id": 2,
        "solid_ids": [100],
        "output": output,
        "part_aggregate": aggregate,
        "expected_volume": 8.0,
        "expected_mass": 2.5,
    }
    return before, after, args


class MapBlockFillAuditTests(unittest.TestCase):
    def test_new_grid_cannot_acquire_solver_constraints_or_local_displacement_system(self):
        for field, value in (("PS", "123"), ("CD", 42), ("SEID", 9)):
            before, after, args = _fixture()
            _row(after, "GRID", 5)["fields"][field] = value
            next(row for row in after['grids'] if row['id'] == 5)['card']['fields'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'nondefault'):
                audit_changes(before, after, **args)

    def test_accepts_only_the_verified_native_fill_delta(self):
        before, after, args = _fixture()

        result = audit_changes(before, after, **args)

        self.assertEqual(result["new_solid_ids"], [100])
        self.assertEqual(result["new_grid_ids"], [5, 6, 7, 8])
        self.assertEqual(result["filled_volume_id"], 1)
        self.assertEqual(result["captured_grid_count"], 8)
        self.assertTrue(result["scope_verified"])
        self.assertRegex(result["after_scope_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("ok", result)

    def test_rejects_original_material_and_grid_card_mutations(self):
        for kind, ident, field, value in (
            ("MAT1", 9, "E", 200000.0),
            ("GRID", 1, "X1", 1.5),
        ):
            with self.subTest(kind=kind, field=field):
                before, after, args = _fixture()
                row = next(row for row in after["entities"]
                           if row["entity"]["type"] == kind and row["entity"]["id"] == ident)
                row["fields"][field] = value
                if kind == "GRID":
                    summary = next(row for row in after["grids"] if row["id"] == ident)
                    summary["card"]["fields"][field] = value
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_changes(before, after, **args)

    def test_rejects_entity_additions_deletions_and_new_volumes(self):
        for mutation in ("new_material", "deleted_shell", "new_volume"):
            with self.subTest(mutation=mutation):
                before, after, args = _fixture()
                if mutation == "new_material":
                    after["entities"].append(_entity("MAT1", 10, {"MID": 10, "E": 1.0}))
                elif mutation == "deleted_shell":
                    after["entities"] = [row for row in after["entities"]
                                         if row["entity"]["type"] != "SHELL"]
                else:
                    fields = {"ID": 2, "PID": 2, "Type": "Map Block"}
                    after["entities"].append(_entity("VOLUME", 2, fields, part_id=1, solid_ids=[]))
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_changes(before, after, **args)

    def test_rejects_inconsistent_volume_counts_membership_and_solid_ownership(self):
        mutations = (
            ("wrong_hexa_count", lambda after: _row(after, "VOLUME", 1)["fields"].update(Hexas=2)),
            ("missing_volume_membership", lambda after: _row(after, "VOLUME", 1).update(solid_ids=[])),
            ("wrong_property_count", lambda after: _row(after, "PSOLID", 2)["fields"].update({"Num.Elem": 2})),
            ("wrong_part", lambda after: _row(after, "SOLID", 100).update(part_id=7)),
            ("wrong_property", lambda after: _row(after, "SOLID", 100)["fields"].update(PID=3)),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                before, after, args = _fixture()
                mutate(after)
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_changes(before, after, **args)
        for field, value in (("expected_volume", 9.0), ("expected_mass", 3.0)):
            with self.subTest(independent_value=field):
                before, after, args = _fixture()
                args[field] = value
                with self.assertRaises(RuntimeError):
                    audit_changes(before, after, **args)

    def test_requires_complete_bound_fresh_part_aggregate(self):
        mutations = (
            ("missing", lambda aggregate: None),
            ("wrong_origin", lambda aggregate: aggregate.update(origin="user supplied")),
            ("wrong_part", lambda aggregate: aggregate.update(part_id=2)),
            ("missing_value", lambda aggregate: aggregate["values"].pop("Iyz")),
            ("non_finite", lambda aggregate: aggregate["values"].update(Mass=float("nan"))),
            ("mismatch", lambda aggregate: aggregate["values"].update(Ixx=100.0)),
        )
        for label, mutate in mutations:
            with self.subTest(aggregate=label):
                before, after, args = _fixture()
                aggregate = copy.deepcopy(args["part_aggregate"])
                mutate(aggregate)
                if label == "missing":
                    args["part_aggregate"] = None
                else:
                    args["part_aggregate"] = aggregate
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_changes(before, after, **args)

    def test_rejects_unverified_element_types_node_references_and_grid_inventory(self):
        cases = (
            ("wrong_element_type", lambda args, after: args["output"]["elements"][0].update(type="CPENTA")),
            ("missing_node", lambda args, after: args["output"]["nodes"].pop()),
            ("unreferenced_new_grid", lambda args, after: _append_unused_grid(after)),
            ("wrong_output_ids", lambda args, after: args.update(solid_ids=[101])),
            ("wrong_captured_id", lambda args, after: args["output"]["elements"][0].update(id=101)),
        )
        for label, mutate in cases:
            with self.subTest(output=label):
                before, after, args = _fixture()
                mutate(args, after)
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_changes(before, after, **args)


def _row(snapshot, kind, ident):
    return next(row for row in snapshot["entities"]
                if row["entity"]["type"] == kind and row["entity"]["id"] == ident)


def _append_unused_grid(snapshot):
    row = _grid(9, (9.0, 0.0, 0.0))
    snapshot["entities"].append(row)
    snapshot["grids"].append({
        "card": {key: row[key] for key in ("entity", "card_fields", "fields")},
        "id": 9,
    })


def _volume(ident, *, meshed, solid_ids=(), volume=8.0, mass=2.5):
    fields = {
        "ID": ident,
        "PID": 2,
        "Type": "Map Block",
        "State": "Meshed" if meshed else "Ready to Mesh",
        "Status": "Meshed" if meshed else "Unmeshed",
        "Order": "1st" if meshed else "",
        "Frozen": "NO",
        "Volume": volume,
        "Mass": mass,
        "Hexas": len(solid_ids) if meshed else 0,
        "Grids": 8 if meshed else 0,
        "Internal grids": 4 if meshed else 0,
        "Pentas": 0,
        "Tetras": 0,
        "Pyramids": 0,
        "Polyhedrals": 0,
        "Comment": "",
    }
    return _entity("VOLUME", ident, fields, part_id=1, solid_ids=sorted(solid_ids))


def _set_grid_value(snapshot, ident, field, value):
    row = _row(snapshot, "GRID", ident)
    row["fields"][field] = value
    summary = next(item for item in snapshot["grids"] if item["id"] == ident)
    summary["card"]["fields"][field] = value


def _multi_output(volume_id, solid_id, node_ids, coordinates):
    return {
        "complete": True,
        "deck": 1,
        "elements": [{
            "id": solid_id,
            "type": "CHEXA",
            "nodes": list(node_ids),
            "property_id": 2,
            "block_id": f"VOLUME:{volume_id}",
        }],
        "integrity": {
            "complete": True,
            "requested_element_count": 1,
            "element_count": 1,
            "node_count": len(node_ids),
            "missing_node_ids": [],
            "element_types": {"CHEXA": 1},
        },
        "nodes": [{"id": ident, "xyz": list(coordinates[ident])} for ident in node_ids],
        "ownership": {
            "origin": "native_VOLUME_membership",
            "complete": True,
            "known_count": 1,
        },
        "snapshot_sha256": "c" * 64,
    }


def _add_second_fill(after, volume_id=4, solid_id=101):
    volume = _volume(volume_id, meshed=True, solid_ids=[solid_id])
    after["entities"].append(volume)
    coordinates = {
        9: (9.0, 0.0, 0.0),
        10: (10.0, 0.0, 0.0),
        11: (11.0, 0.0, 0.0),
        12: (12.0, 0.0, 0.0),
    }
    for ident, xyz in coordinates.items():
        row = _grid(ident, xyz)
        after["entities"].append(row)
        after["grids"].append({
            "card": {key: row[key] for key in ("entity", "card_fields", "fields")},
            "id": ident,
        })
    solid_fields = {"type": "CHEXA", "EID": solid_id, "PID": 2}
    solid_fields.update({f"G{i}": node_id for i, node_id in enumerate((1, 2, 3, 4, 9, 10, 11, 12), 1)})
    after["entities"].append(_entity("SOLID", solid_id, solid_fields, part_id=1))
    return _multi_output(volume_id, solid_id, [1, 2, 3, 4, 9, 10, 11, 12], {
        **{ident: (float(ident), float(ident + 1), 0.0) for ident in range(1, 5)},
        **coordinates,
    })


def _updated_part_values():
    return {
        **PART_VALUES,
        "Mass": 5.0,
        "Ixx": 2.0,
        "Iyy": 4.0,
        "Izz": 6.0,
    }


def _two_block_fixture():
    before, after, first = _fixture()
    before["entities"].append(_volume(4, meshed=False))
    second_output = _add_second_fill(after)
    _row(after, "PSOLID", 2)["fields"]["Num.Elem"] = 2
    part_values = _updated_part_values()
    part_values["Last Edit"] = "26-10月-2026 10:24:14"
    _row(after, "ANSAPART", 1)["fields"].update(part_values)
    aggregate = copy.deepcopy(first["part_aggregate"])
    aggregate["values"] = _updated_part_values()
    blocks = [
        {key: first[key] for key in (
            "volume_id", "part_id", "property_id", "solid_ids", "output",
            "expected_volume", "expected_mass",
        )},
        {
            "volume_id": 4,
            "part_id": 1,
            "property_id": 2,
            "solid_ids": [101],
            "output": second_output,
            "expected_volume": 8.0,
            "expected_mass": 2.5,
        },
    ]
    return before, after, blocks, aggregate


def _incremental_fixture():
    _, filled, first = _fixture()
    before = copy.deepcopy(filled)
    after = copy.deepcopy(filled)
    before["entities"].append(_volume(4, meshed=False))
    second_output = _add_second_fill(after)
    _row(after, "PSOLID", 2)["fields"]["Num.Elem"] = 2
    part_values = _updated_part_values()
    part_values["Last Edit"] = "26-10月-2026 10:24:14"
    _row(after, "ANSAPART", 1)["fields"].update(part_values)
    aggregate = copy.deepcopy(first["part_aggregate"])
    aggregate["values"] = _updated_part_values()
    block = {
        "volume_id": 4,
        "part_id": 1,
        "property_id": 2,
        "solid_ids": [101],
        "output": second_output,
        "expected_volume": 8.0,
        "expected_mass": 2.5,
    }
    return before, after, [block], aggregate


class MapBlockFillManyAuditTests(unittest.TestCase):
    def test_accepts_two_blocks_sharing_one_psolid(self):
        before, after, blocks, aggregate = _two_block_fixture()

        result = audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)

        self.assertEqual(result["new_solid_ids"], [100, 101])
        self.assertEqual(result["new_grid_ids"], [5, 6, 7, 8, 9, 10, 11, 12])
        self.assertEqual(result["filled_volume_ids"], [1, 4])
        self.assertEqual([row["internal_grid_count"] for row in result["block_results"]], [4, 4])
        self.assertTrue(result["scope_verified"])

    def test_accepts_incremental_fill_while_protecting_existing_filled_block(self):
        before, after, blocks, aggregate = _incremental_fixture()

        result = audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)

        self.assertEqual(result["new_solid_ids"], [101])
        self.assertEqual(result["new_grid_ids"], [9, 10, 11, 12])
        self.assertEqual(result["filled_volume_ids"], [4])
        self.assertEqual(result["block_results"][0]["boundary_grid_ids"], [1, 2, 3, 4])

    def test_rejects_a_new_grid_used_internally_by_two_blocks(self):
        before, after, blocks, aggregate = _two_block_fixture()
        second_output = blocks[1]["output"]
        second_output["elements"][0]["nodes"] = [1, 2, 3, 4, 5, 10, 11, 12]
        second_output["nodes"] = [
            copy.deepcopy(blocks[0]["output"]["nodes"][4])
            if row["id"] == 9 else row
            for row in second_output["nodes"]
        ]
        _row(after, "SOLID", 101)["fields"].update(
            {f"G{i}": node_id for i, node_id in enumerate((1, 2, 3, 4, 5, 10, 11, 12), 1)}
        )

        with self.assertRaisesRegex(RuntimeError, "internal node by multiple"):
            audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)

    def test_rejects_old_solid_unselected_volume_wrong_new_owner_and_count_mutations(self):
        mutations = (
            ("old_solid", lambda after: _row(after, "SOLID", 100)["fields"].update(G1=99)),
            ("unselected_volume", lambda after: _row(after, "VOLUME", 1)["fields"].update(Comment="changed")),
            ("new_owner", lambda after: _row(after, "SOLID", 101).update(part_id=7)),
            ("property_count", lambda after: _row(after, "PSOLID", 2)["fields"].update({"Num.Elem": 3})),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                before, after, blocks, aggregate = _incremental_fixture()
                mutate(after)
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)

    def test_rejects_missing_raw_grid_fields_and_new_grid_constraints(self):
        for label in ("missing_raw_field", "constraint"):
            with self.subTest(mutation=label):
                before, after, blocks, aggregate = _incremental_fixture()
                row = _row(after, "GRID", 9)
                if label == "missing_raw_field":
                    row["card_fields"].remove("X3")
                    row["fields"].pop("X3")
                    summary = next(item for item in after["grids"] if item["id"] == 9)
                    summary["card"] = {key: row[key] for key in ("entity", "card_fields", "fields")}
                else:
                    _set_grid_value(after, 9, "CD", 17)
                with self.assertRaises((RuntimeError, ValueError)):
                    audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)


if __name__ == "__main__":
    unittest.main()
