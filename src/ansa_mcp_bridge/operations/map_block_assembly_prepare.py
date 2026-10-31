"""Prepare shared-face rectangular CAD assemblies as native Map Blocks."""
import copy
import math
import re

from . import cad_map, cons_seed as seeds, geometry_features as features, inspection
from . import map_block_batch as batch, map_block_inspection, map_block_prepare as single
from . import meshing, meshing_context, part_aggregates
from . import structured_native as native, volume_meshing as guards
from .cad_sweep_candidates import discover
from .cad_sweep_geometry import capture_many as capture_geometry_many
from .mapped_surface import _check_new_grids
from .structured_batch import source_subset
from .mapped_surface_topology import four_sides


_PARAMS = ("deck", "face_groups", "target_length", "property_id")
_MAX_FACE_GROUPS = 20
_MAX_ASSEMBLY_FACES = 100
_MAX_NEW_SHELLS = 400  # Also required by map_block_batch.preflight readback.
_MAX_NEW_GRIDS = 10000
_MAX_MODEL_ENTITIES = 10000


def normalize(params, applying=False):
    inspection._params(
        params,
        _PARAMS + (("expected_plan_sha256", "backup_path") if applying else ()),
    )
    groups = params["face_groups"]
    if type(groups) is not list or not 2 <= len(groups) <= _MAX_FACE_GROUPS:
        raise ValueError("face_groups must contain 2..20 six-FACE groups")
    normalized_groups = []
    for index, group in enumerate(groups):
        if type(group) not in (list, tuple):
            raise ValueError(f"face_groups[{index}] must be a list of six FACE IDs")
        ids = sorted(native._id_list(group, f"face_groups[{index}]", 6))
        if len(ids) != 6 or len(set(ids)) != 6:
            raise ValueError(f"face_groups[{index}] must contain six unique FACE IDs")
        normalized_groups.append(ids)
    group_keys = [tuple(group) for group in normalized_groups]
    if len(group_keys) != len(set(group_keys)):
        raise ValueError("face_groups must not repeat a group")
    for index, first in enumerate(normalized_groups):
        for second in normalized_groups[index + 1:]:
            if len(set(first) & set(second)) > 1:
                raise ValueError("groups may share one complete FACE only")
    all_faces = sorted({face_id for group in normalized_groups for face_id in group})
    if len(all_faces) > _MAX_ASSEMBLY_FACES:
        raise ValueError("shared-face assembly exceeds the 100-FACE witness budget")

    clean = {
        "deck": native._positive_int(params["deck"], "deck"),
        "face_groups": sorted(normalized_groups, key=tuple),
        "target_length": features._number(params["target_length"], "target_length", True),
        "property_id": native._positive_int(params["property_id"], "property_id"),
    }
    if applying:
        clean["expected_plan_sha256"] = native._validate_digest(
            params["expected_plan_sha256"], "expected_plan_sha256"
        )
        clean["backup_path"] = params["backup_path"]
    return clean


def _raw_scope_map(snapshot, label):
    if type(snapshot) is not dict or type(snapshot.get("entities")) is not list:
        raise ValueError(f"{label} must be a complete raw entity snapshot")
    result = {}
    for row in snapshot["entities"]:
        if type(row) is not dict or type(row.get("entity")) is not dict:
            raise ValueError(f"{label} contains a malformed raw entity row")
        reference = row["entity"]
        kind, ident = reference.get("type"), reference.get("id")
        if type(kind) is not str or not kind:
            raise ValueError(f"{label} contains an invalid entity type")
        native._positive_int(ident, f"{label} entity ID")
        fields, labels = row.get("fields"), row.get("card_fields")
        if (type(fields) is not dict or type(labels) not in (list, tuple) or not labels
                or any(type(name) is not str or not name for name in labels)
                or len(labels) != len(set(labels)) or not set(labels) <= set(fields)):
            raise ValueError(f"{label} entity lacks a complete raw card inventory")
        if "ignored_derived_fields" in row:
            raise ValueError(f"{label} omits raw derived card fields")
        key = (kind, ident)
        if key in result:
            raise ValueError(f"{label} contains duplicate entity identity {key}")
        result[key] = row
    digest = snapshot.get("cad_sha256")
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{label} lacks a valid full CAD export hash")
    grid_rows = snapshot.get("grids")
    if type(grid_rows) is not list:
        raise ValueError(f"{label} lacks the complete GRID summary inventory")
    grid_map = {}
    for row in grid_rows:
        if type(row) is not dict:
            raise ValueError(f"{label} contains a malformed GRID summary")
        ident = native._positive_int(row.get("id"), f"{label} GRID summary ID")
        raw = result.get(("GRID", ident))
        if raw is None or row.get("card") != {
                name: raw[name] for name in ("entity", "card_fields", "fields")}:
            raise RuntimeError(f"{label} GRID summary differs from its complete raw card")
        if ident in grid_map:
            raise ValueError(f"{label} contains duplicate GRID summary ID {ident}")
        grid_map[ident] = row
    if set(grid_map) != {ident for kind, ident in result if kind == "GRID"}:
        raise RuntimeError(f"{label} GRID summary does not cover every raw GRID")
    return result


def _required_field(row, name, label):
    fields = row.get("fields")
    labels = row.get("card_fields")
    if (type(fields) is not dict or type(labels) not in (list, tuple)
            or name not in fields or name not in labels):
        raise ValueError(f"{label} lacks raw card field {name}")
    return fields[name]


def _segments_for_length(length, target_length, label):
    length = features._number(length, label, True)
    ratio = length / target_length
    count = round(ratio)
    if (not 1 <= count <= 20
            or not math.isclose(ratio, count, rel_tol=1e-9, abs_tol=1e-9)
            or not math.isclose(length / count, target_length, rel_tol=1e-9, abs_tol=1e-12)):
        raise ValueError(f"{label} must be an integer 1..20 multiple of target_length")
    return count


def _match_volume_groups(volumes, face_groups):
    """Bind inspected native VOLUMEs to explicit groups by complete FACE set."""
    if type(volumes) is not list or len(volumes) != len(face_groups):
        raise RuntimeError("native MapBlock VOLUME count differs from face_groups")
    expected = {tuple(group) for group in face_groups}
    matched = {}
    ids = set()
    for row in volumes:
        if type(row) is not dict:
            raise RuntimeError("native MapBlock inspection returned a malformed VOLUME")
        ident = native._positive_int(row.get("volume_id"), "inspected VOLUME ID")
        face_ids = row.get("face_ids")
        if type(face_ids) is not list or any(type(value) is not int or value <= 0 for value in face_ids):
            raise RuntimeError("inspected VOLUME lacks complete FACE boundary IDs")
        key = tuple(sorted(face_ids))
        if len(key) != len(set(key)) or key not in expected or key in matched or ident in ids:
            raise RuntimeError("native VOLUME boundaries do not uniquely match face_groups")
        matched[key] = row
        ids.add(ident)
    if set(matched) != expected:
        raise RuntimeError("native VOLUME boundaries omit an explicit FACE group")
    return matched


def _choose_t1_proofs(state, base, deck, local_witnesses, target_length):
    proofs = {}; requests = []; groups = sorted(local_witnesses)
    for group_id in groups:
        witness = local_witnesses[group_id]
        bounds = batch.rectangular_bounds(witness)
        found = discover(witness, target_length)
        if (found['coverage']['complete'] is not True or len(found['components']) != 1
                or found['components'][0]['status'] != 'topology_candidate'):
            raise ValueError(f'FACE group {group_id} has no complete T1 topology candidate')
        feasible = []
        for alternative in found['components'][0]['alternatives']:
            topology = alternative['topology']; direction = topology.get('direction')
            if type(direction) not in (list, tuple) or len(direction) != 3:continue
            vector = [features._number(value, 'T1 direction') for value in direction]
            axes = [axis for axis, value in enumerate(vector) if abs(value) >= 1.0-1e-8]
            if len(axes) != 1 or any(abs(value)>1e-8 for axis,value in enumerate(vector) if axis!=axes[0]):continue
            options = alternative['map_options']
            feasible.append({'options':options,'topology':topology,
                             'cap_pair':sorted((options['source_face_id'],options['target_face_id']))})
        if not feasible:raise ValueError(f'FACE group {group_id} has no axis-aligned T1 plan')
        if len({row['topology'].get('tolerance',1e-7) for row in feasible})!=1:
            raise ValueError('alternative T1 domains require separate geometry tolerances')
        proofs[group_id]={'bounds':bounds,'discovery':found,'feasible_t1':feasible}
        requests.append((witness,feasible[0]['topology']))
    # A six-FACE domain proof is independent of which valid cap pair is chosen.
    # Each native FACE is exported once, including shared interfaces; this cache
    # dies at the end of this call and is never used across preflights or writes.
    for group_id,geometry in zip(groups,capture_geometry_many(state,base,deck,requests)):
        if geometry['status']!='verified_at_export_precision':
            raise ValueError(f'FACE group {group_id} has no finite-domain T1 MapBlock plan')
        for row in proofs[group_id]['feasible_t1']:row['geometry']=geometry
        proofs[group_id]['selected_t1']=proofs[group_id]['feasible_t1'][0]
    return proofs


def _mesh_budget(before, layout, local_witnesses, cons_segments, target_length):
    # Count each native FACE once; adjacent volumes share the same interface mesh.
    face_rows = {}
    volume_shell_upper = 0
    volume_grid_upper = 0
    predicted_fill_solids = 0
    predicted_fill_grids = 0
    predicted_total_nodes = 0
    for group_id, witness in local_witnesses.items():
        bounds = layout["bounds"][group_id]
        dimensions = [bounds["max"][axis]-bounds["min"][axis] for axis in range(3)]
        counts = [_segments_for_length(value, target_length, "rectangular block dimension")
                  for value in dimensions]
        nx, ny, nz = counts
        volume_shell_upper += 2*(nx*ny + ny*nz + nz*nx)
        volume_grid_upper += ((nx+1)*(ny+1)*(nz+1)
                              - max(nx-1, 0)*max(ny-1, 0)*max(nz-1, 0))
        predicted_fill_solids += nx*ny*nz
        predicted_fill_grids += max(nx-1, 0)*max(ny-1, 0)*max(nz-1, 0)
        predicted_total_nodes += (nx+1)*(ny+1)*(nz+1)
        for face in witness["face_boundaries"]:
            sides = [cons_segments[ident] for ident in face["cons_ids"]]
            distinct = sorted(set(sides))
            if not 1 <= len(distinct) <= 2:
                raise ValueError("rectangular FACE edges do not define one or two lattice counts")
            if len(distinct) == 2 and sorted(sides) != [distinct[0]]*2+[distinct[1]]*2:
                raise ValueError('rectangular FACE opposite-edge lattice counts must match')
            face_rows[face["face_id"]] = distinct[0]**2 if len(distinct) == 1 else math.prod(distinct)
    face_shell_count = sum(face_rows.values())
    if face_shell_count > volume_shell_upper:
        raise RuntimeError("unique FACE shell estimate exceeds its per-volume upper bound")
    if face_shell_count > _MAX_NEW_SHELLS:
        raise ValueError("prepared assembly exceeds the 400-SHELL MapBlock readback budget")
    if volume_grid_upper > _MAX_NEW_GRIDS:
        raise ValueError("predicted assembly boundary exceeds the 10000-GRID budget")
    if predicted_total_nodes > _MAX_NEW_GRIDS or predicted_fill_solids > 5000:
        raise ValueError("predicted follow-up MapBlock fill exceeds native GRID/SOLID budgets")
    new_asset_upper = face_shell_count + volume_grid_upper + 3*len(local_witnesses)
    final_entity_upper = (len(before["entities"])+new_asset_upper
                          +predicted_fill_solids+predicted_fill_grids+1)
    if final_entity_upper > _MAX_MODEL_ENTITIES:
        raise ValueError("predicted MapBlock assembly exceeds the 10000-entity model budget")
    return {
        "predicted_boundary_shells": face_shell_count,
        "conservative_per_volume_shell_upper_bound": volume_shell_upper,
        "conservative_new_grid_upper_bound": volume_grid_upper,
        "reserved_volume_and_auto_asset_count": 3*len(local_witnesses),
        "predicted_followup_solids": predicted_fill_solids,
        "predicted_followup_internal_grids": predicted_fill_grids,
        "model_entity_upper_bound": final_entity_upper,
    }


def preflight(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize({key: params[key] for key in _PARAMS})
    deck = inspection._deck(base, clean)
    native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    for module, names in (
        (base, ("CalcElementMass", "DeleteEntity", "SetEntityCardValues",
                "GetEntityCardValues", "GetEntity")),
        (mesh, ("MapBlock", "GetVolumeBoundary", "GetMapVolumeBoundaries")),
    ):
        for name in names:
            if not callable(getattr(module, name, None)):
                raise NotImplementedError("required native API unavailable: "+name)

    all_faces = sorted(entity._id for entity in seeds._collect(base, deck, None, "FACE"))
    requested_faces = sorted({face_id for group in clean["face_groups"] for face_id in group})
    if (not all_faces or len(all_faces) > _MAX_ASSEMBLY_FACES
            or requested_faces != all_faces):
        raise ValueError("face_groups must cover every model FACE within the 100-FACE budget")
    for kind in ("SHELL", "SOLID", "VOLUME", "CURVE"):
        if seeds._collect(base, deck, None, kind):
            raise ValueError("assembly preparation requires no existing SHELL/SOLID/VOLUME/CURVE")
    parts = seeds._collect(base, deck, None, "ANSAPART")
    if len(parts) != 1:
        raise ValueError("assembly preparation requires exactly one source Part")
    part = parts[0]
    part_id = native._positive_int(part._id, "source Part ID")
    part_fields = seeds._card(base, deck, part)["fields"]
    if part_fields.get("Freeze") != "NO" or part_fields.get("Is Group") != "NO":
        raise ValueError("source Part must be unfrozen and not a group")

    stats = {"scoped_export_calls": 0}
    parent, _entities = features._witness(
        state, base, deck, {"face_ids": all_faces, "curve_ids": []}, stats,
        face_limit=_MAX_ASSEMBLY_FACES,
    )
    parent_parts = {row["id"]: row["part_id"] for row in parent["parts"]}
    if set(parent_parts) != set(all_faces) or set(parent_parts.values()) != {part_id}:
        raise ValueError("all assembly FACEs must belong to the single source Part")

    before = cad_map._snapshot(state, base, deck)
    old = _raw_scope_map(before, "before")
    if any(kind in {"SHELL", "SOLID", "VOLUME", "CURVE"} for kind, _ in old):
        raise RuntimeError("complete raw snapshot contains a prohibited pre-existing FE/volume entity")
    if {ident for kind, ident in old if kind == "FACE"} != set(all_faces):
        raise RuntimeError("complete raw snapshot FACE inventory differs from the CAD witness")
    synthetic_block = {
        "volumes": [
            {"volume_id": index, "face_ids": group}
            for index, group in enumerate(clean["face_groups"], 1)
        ]
    }
    local = {
        index: batch.local_witness(parent, group)
        for index, group in enumerate(clean["face_groups"], 1)
    }
    layout = batch.assembly_layout(synthetic_block, local)
    t1_proofs = _choose_t1_proofs(state, base, deck, local, clean["target_length"])

    source_property_ids = set()
    for face_id in all_faces:
        face, face_snapshot, shell_ids = meshing._face_snapshot(base, mesh, deck, face_id)
        card = seeds._card(base, deck, face)["fields"]
        assignments = native._face_assignments(base, deck, face, face_id)
        if (face_snapshot["macro_face_ids"] != [face_id] or shell_ids
                or card.get("Frozen") != "NO"
                or native._volume_count(card.get("Num. Volumes")) != 0
                or assignments["part_id"] != part_id):
            raise ValueError("every FACE must be an unfrozen, unmeshed, single-FACE macro in the source Part")
        source_property_ids.add(assignments["property_id"])
        _required_field(old[("FACE", face_id)], "Num. Volumes", f"FACE {face_id}")
    if len(source_property_ids) != 1:
        raise ValueError("assembly preparation requires one source PSHELL for every FACE")
    source_pid = next(iter(source_property_ids))
    source_key = ("PSHELL", source_pid)
    if source_key not in old:
        raise ValueError("source PSHELL is absent from the complete raw snapshot")
    for name in ("PID", "Name", "T", "Num.Elem"):
        _required_field(old[source_key], name, "source PSHELL")
    if old[source_key]["fields"]["PID"] != source_pid:
        raise RuntimeError("source PSHELL raw PID does not match its assigned identity")
    if native._volume_count(_required_field(old[source_key], "Num.Elem", "source PSHELL")) != 0:
        raise ValueError("source PSHELL Num.Elem must be explicitly zero")
    source_material_ids = {
        native._positive_int(old[source_key]["fields"][name], f"source PSHELL {name}")
        for name in ("MID1", "MID2", "MID3", "MID4")
        if old[source_key]["fields"].get(name) is not None
    }
    for material_id in source_material_ids:
        material_key = ("MAT1", material_id)
        if material_key not in old:
            raise ValueError(f"source PSHELL MAT1 {material_id} is absent from the complete snapshot")
        _required_field(old[material_key], "Name", f"source MAT1 {material_id}")

    target_key = ("PSOLID", clean["property_id"])
    if target_key not in old:
        raise ValueError("target PSOLID must already exist in the complete raw snapshot")
    target_property, property_evidence = native._psolid_preflight(base, deck, clean["property_id"])
    target_fields = old[target_key]["fields"]
    for name in ("PID", "Name", "MID", "Num.Elem"):
        _required_field(old[target_key], name, "target PSOLID")
    if native._volume_count(_required_field(old[target_key], "Num.Elem", "target PSOLID")) != 0:
        raise ValueError("target PSOLID Num.Elem must be explicitly zero in the SOLID-free model")
    density = features._number(property_evidence["mat1"]["fields"].get("RHO"),
                               "target MAT1 RHO", True)
    if density <= 0:
        raise ValueError("target PSOLID requires a positive material density")
    if (target_fields.get("PID") != clean["property_id"]
            or property_evidence["psolid"]["fields"].get("PID") != clean["property_id"]
            or target_fields.get("MID") != property_evidence["material_id"]):
        raise RuntimeError("target PSOLID identity differs from its complete raw card")
    material_id = property_evidence["material_id"]
    material_key = ("MAT1", material_id)
    if material_key not in old:
        raise ValueError("target PSOLID MAT1 is absent from the complete raw snapshot")
    for name in ("MID", "Name", "RHO"):
        _required_field(old[material_key], name, "target MAT1")
    if old[material_key]["fields"].get("MID") != material_id:
        raise RuntimeError("target MAT1 raw MID does not match its identity")
    for name in ("Name", "RHO"):
        if old[material_key]["fields"].get(name) != property_evidence["mat1"]["fields"].get(name):
            raise RuntimeError(f"target MAT1 raw {name} differs from its independent property capture")

    cons_ids = sorted(row["id"] for row in parent["boundaries"])
    cons_rows = {}
    cons_segments = {}
    for cons_id in cons_ids:
        edge = seeds._boundary(base, deck, features._entity(base, deck, "CONS", cons_id))
        fields = edge["native_fields"]
        if (edge["status"] != "supported" or fields["Frozen"] != "NO"
                or fields["Collapsed"] != "NO" or fields["Biasing"] != "LINEAR"
                or fields["Order of Nodes"] != "1st"):
            raise ValueError(f"CONS {cons_id} must be an unfrozen first-order linear boundary")
        length = math.dist(edge["hot_points"][0]["xyz"], edge["hot_points"][1]["xyz"])
        cons_rows[cons_id] = edge
        cons_segments[cons_id] = _segments_for_length(
            length, clean["target_length"], f"CONS {cons_id} length"
        )

    budget = _mesh_budget(before, layout, local, cons_segments, clean["target_length"])
    part_aggregates.capture(base, deck, part, old[("ANSAPART", part_id)])
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)

    proofs = []
    for index, group in enumerate(clean["face_groups"], 1):
        plan = t1_proofs[index]
        topology = plan["selected_t1"]["topology"]
        volume = features._number(topology.get("expected_volume"), f"group {index} expected volume", True)
        if volume <= 0:
            raise ValueError(f"FACE group {index} has no positive finite T1 volume")
        proofs.append({
            "group_index": index,
            "face_ids": group,
            "cons_ids": sorted({cons_id for face in group
                                for cons_id in next(row["cons_ids"] for row in local[index]["face_boundaries"]
                                                    if row["face_id"] == face)}),
            "local_witness": local[index],
            "bounds": plan["bounds"],
            "topology": topology,
            "geometry": plan["selected_t1"]["geometry"],
            "selected_t1": plan["selected_t1"],
            "feasible_t1": plan["feasible_t1"],
            "expected_volume": volume,
            "expected_mass": volume*density,
        })

    body = {
        "params": clean,
        "context": context,
        "before_scope_sha256": features._hash(before),
        "parent_witness_sha256": parent["source_snapshot_sha256"],
        "parent_cad_export_sha256": parent["cad_export_sha256"],
        "local_witnesses": local,
        "layout": layout,
        "proofs": proofs,
        "cons_seed_sha256": features._hash(cons_rows),
        "cons_segments": cons_segments,
        "source_pshell": old[source_key],
        "target_property": property_evidence,
        "target_density": density,
        "budget": budget,
        "meshing_context_sha256": settings["snapshot_sha256"],
    }
    return {
        "clean": clean,
        "context": context,
        "before": before,
        "parent_witness": parent,
        "local_witnesses": local,
        "layout": layout,
        "proofs": proofs,
        "cons_rows": cons_rows,
        "cons_segments": cons_segments,
        "part": part,
        "part_id": part_id,
        "source_pid": source_pid,
        "target_property": target_property,
        "property_evidence": property_evidence,
        "density": density,
        "settings": settings,
        "budget": budget,
        "plan_sha256": features._hash(body),
    }


def public_plan(prepared):
    return {
        "status": "ready_for_controlled_apply",
        "params": prepared["clean"],
        "plan_sha256": prepared["plan_sha256"],
        "scope_sha256": features._hash(prepared["before"]),
        "source_snapshot_sha256": prepared["parent_witness"]["source_snapshot_sha256"],
        "assembly_layout": prepared["layout"],
        "blocks": [
            {key: row[key] for key in (
                "group_index", "face_ids", "bounds", "topology", "geometry",
                "expected_volume", "expected_mass",
            )}
            | {"feasible_t1_count": len(row["feasible_t1"])}
            for row in prepared["proofs"]
        ],
        "source_property_id": prepared["source_pid"],
        "property_id": prepared["clean"]["property_id"],
        "meshing_context_sha256": prepared["settings"]["snapshot_sha256"],
        "budget": prepared["budget"],
        "usable_for_mutation": False,
        "solid_mesh_generated": False,
        "continuous_export_domain_verified": True,
        "exact_native_CAD_equivalence_proven": False,
    }


def validate_report(state, base, mesh, constants, deck, result):
    current = preflight(state, base, mesh, constants, result["params"])
    if current["plan_sha256"] != result.get("plan_sha256"):
        raise ValueError("MapBlock assembly prepare plan is stale")


def _bind_assets(state, base, mesh, constants, prepared, returned, result):
    deck = prepared["clean"]["deck"]
    interim = cad_map._snapshot(state, base, deck)
    old, new = _raw_scope_map(prepared["before"], "before"), _raw_scope_map(interim, "interim")
    if old.keys()-new.keys():
        raise RuntimeError("MapBlock removed original entities")
    added = new.keys()-old.keys()
    if any(kind not in ("GRID", "SHELL", "VOLUME", "PSOLID", "MAT1") for kind, _ in added):
        raise RuntimeError("MapBlock created an entity outside GRID/SHELL/VOLUME/PSOLID/MAT1")
    volume_ids = sorted(ident for kind, ident in added if kind == "VOLUME")
    psolid_ids = sorted(ident for kind, ident in added if kind == "PSOLID")
    mat1_ids = sorted(ident for kind, ident in added if kind == "MAT1")
    if len(volume_ids) != len(prepared["clean"]["face_groups"]):
        raise RuntimeError("actual new VOLUME inventory differs from face_groups")
    if not psolid_ids or not mat1_ids:
        raise RuntimeError("native MapBlock did not create its expected PSOLID/MAT1 assets")
    if type(returned) not in (list, tuple):
        raise RuntimeError("native MapBlock did not return a VOLUME list")
    returned_ids = []
    for entity in returned:
        if entity.ansa_type(deck) != "VOLUME":
            raise RuntimeError("native MapBlock returned an entity other than VOLUME")
        returned_ids.append(native._positive_int(entity._id, "returned VOLUME ID"))
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(volume_ids):
        raise RuntimeError("returned VOLUME IDs differ from the independent new VOLUME inventory")

    inspected = map_block_inspection.capture(state, base, mesh, constants, deck, volume_ids)
    matched = _match_volume_groups(inspected["volumes"], prepared["clean"]["face_groups"])
    volume_psolids = set()
    for key, row in matched.items():
        ident = row["volume_id"]
        if row["part_id"] != prepared["part_id"]:
            raise RuntimeError("new VOLUME has the wrong Part owner")
        raw = new[("VOLUME", ident)]
        if raw.get("part_id") != prepared["part_id"]:
            raise RuntimeError("new VOLUME raw Part owner differs from native boundary readback")
        pid = native._positive_int(_required_field(raw, "PID", f"VOLUME {ident}"), f"VOLUME {ident} PID")
        if pid not in psolid_ids:
            raise RuntimeError("new VOLUME does not reference a PSOLID created by this MapBlock call")
        volume_psolids.add(pid)

    psolid_materials = set()
    for ident in psolid_ids:
        row = new[("PSOLID", ident)]
        if native._positive_int(_required_field(row, "PID", f"PSOLID {ident}"), "new PSOLID PID") != ident:
            raise RuntimeError("new PSOLID raw PID does not match its identity")
        mid = native._positive_int(_required_field(row, "MID", f"PSOLID {ident}"), f"PSOLID {ident} MID")
        if mid not in mat1_ids:
            raise RuntimeError("new PSOLID does not reference a MAT1 created by this MapBlock call")
        psolid_materials.add(mid)
    for ident in mat1_ids:
        row = new[("MAT1", ident)]
        if native._positive_int(_required_field(row, "MID", f"MAT1 {ident}"), "new MAT1 MID") != ident:
            raise RuntimeError("new MAT1 raw MID does not match its identity")
    if volume_psolids != set(psolid_ids) or psolid_materials != set(mat1_ids):
        raise RuntimeError("new VOLUME/PSOLID/MAT1 references are not closed")

    result["created_auto_psolid_ids"] = psolid_ids
    result["created_auto_mat1_ids"] = mat1_ids
    result["native_asset_cleanup"] = []
    for ident in volume_ids:
        volume = features._entity(base, deck, "VOLUME", ident)
        status = base.SetEntityCardValues(deck, volume, {"PID": prepared["clean"]["property_id"]})
        if type(status) is not int or status != 0:
            raise RuntimeError(f"VOLUME {ident} target PSOLID assignment returned {status!r}")
        readback = base.GetEntityCardValues(deck, volume, ["PID"])
        if type(readback) is not dict or readback.get("PID") != prepared["clean"]["property_id"]:
            raise RuntimeError(f"VOLUME {ident} target PSOLID assignment was not confirmed")

    # Only IDs observed as additions in the pre-operation full snapshot are removed.
    for kind, ids in (("PSOLID", psolid_ids), ("MAT1", mat1_ids)):
        for ident in ids:
            entity = features._entity(base, deck, kind, ident)
            status = base.DeleteEntity(entity, force=False, compress=False)
            absent = base.GetEntity(deck, kind, ident) is None
            cleanup = {"type": kind, "id": ident, "return_code": status, "absent": absent}
            result["native_asset_cleanup"].append(cleanup)
            if type(status) is not int or status != 0 or not absent:
                raise RuntimeError(f"new unused {kind} {ident} cleanup was not confirmed")

    final = cad_map._snapshot(state, base, deck)
    final_scope = _raw_scope_map(final, "after native-asset cleanup")
    if old.keys()-final_scope.keys():
        raise RuntimeError("native asset cleanup removed an original entity")
    if any(key[0] in ("PSOLID", "MAT1") for key in final_scope.keys()-old.keys()):
        raise RuntimeError("native asset cleanup left an unbound auto PSOLID/MAT1")
    if {key for key in final_scope.keys()-old.keys() if key[0] == "VOLUME"} != {
            ("VOLUME", ident) for ident in volume_ids}:
        raise RuntimeError("VOLUME inventory changed during safe auto-asset cleanup")
    result["new_volume_ids"] = volume_ids
    result["returned_volume_ids"] = sorted(returned_ids)
    result["volume_group_map"] = [
        {"volume_id": row["volume_id"], "face_ids": list(key)}
        for key, row in sorted(matched.items())
    ]
    return volume_ids


def _check_face_lattices(base, constants, deck, prepared, inspection_result):
    shell_ids = sorted({ident for row in inspection_result["face_membership"]
                        for ident in row["macro_shell_ids"]})
    if not shell_ids or len(shell_ids) > _MAX_NEW_SHELLS:
        raise RuntimeError("MapBlock surface SHELL inventory is empty or exceeds its readback budget")
    source = native.capture_source(base, constants, deck, shell_ids)
    if any(row["type"] != "CQUAD4" for row in source["elements"]):
        raise RuntimeError("rectangular MapBlock preparation requires CQUAD4 boundary SHELLs")
    face_cons = {
        row["face_id"]: row["cons_ids"]
        for row in prepared["parent_witness"]["face_boundaries"]
    }
    boundaries = [
        seeds._boundary(base, deck, features._entity(base, deck, "CONS", ident), include_points=True)
        for ident in sorted(prepared["cons_segments"])
    ]
    by_cons = {row["cons_id"]: row for row in boundaries}
    if len(by_cons) != len(prepared["cons_segments"]):
        raise RuntimeError("native perimeter readback omitted or duplicated a CONS")
    for ident, expected_segments in prepared["cons_segments"].items():
        row = by_cons[ident]
        if row["status"] != "supported" or row["segments"] != expected_segments:
            raise RuntimeError(f"CONS {ident} perimeter points differ from the planned segment count")
        if row["point_count"] != expected_segments+1:
            raise RuntimeError(f"CONS {ident} perimeter GRID count differs from its segments")
        raw = _raw_scope_map(prepared["after"], "after").get(("CONS", ident))
        if raw is None:
            raise RuntimeError(f"CONS {ident} is absent from the complete after snapshot")
        if native._volume_count(_required_field(raw, "Num. of Nodes", f"CONS {ident}")) != row["point_count"]-2:
            raise RuntimeError(f"CONS {ident} Num. of Nodes differs from all native perimeter points")
        length = math.dist(row["hot_points"][0]["xyz"], row["hot_points"][1]["xyz"])/expected_segments
        if not math.isclose(length, prepared["clean"]["target_length"], rel_tol=1e-9, abs_tol=1e-12):
            raise RuntimeError(f"CONS {ident} actual perimeter segment length differs from target_length")
        card_length = features._number(_required_field(raw, "Elem.Length", f"CONS {ident}"),
                                       f"CONS {ident} Elem.Length", True)
        if not math.isclose(card_length, length, rel_tol=1e-9, abs_tol=1e-12):
            raise RuntimeError(f"CONS {ident} Elem.Length differs from native perimeter points")

    face_lattices = []
    for face in inspection_result["face_membership"]:
        face_id = face["face_id"]
        if not face.get("membership_complete") or not face["macro_shell_ids"]:
            raise RuntimeError(f"FACE {face_id} lacks complete native SHELL attribution")
        sides = four_sides(face_cons[face_id], boundaries)
        for side in sides:
            points = {point["id"]: point["xyz"] for point in by_cons[side["cons_id"]]["hot_points"]}
            side["start_xyz"], side["end_xyz"] = points[side["start"]], points[side["end"]]
        subset = source_subset(source, face["macro_shell_ids"])
        face_lattices.append({"face_id": face_id, "lattice": cad_map._face_lattice(subset, sides)})
    return source, boundaries, face_lattices


def readback(state, base, mesh, constants, prepared, volume_ids, *, native_asset_cleanup=None):
    deck = prepared["clean"]["deck"]
    guards._same_context(state, deck, prepared["context"])
    after = cad_map._snapshot(state, base, deck)
    old, new = _raw_scope_map(prepared["before"], "before"), _raw_scope_map(after, "after")
    if old.keys()-new.keys():
        raise RuntimeError("MapBlock assembly removed original entities")
    additions = new.keys()-old.keys()
    if any(kind not in ("GRID", "SHELL", "VOLUME") for kind, _ in additions):
        raise RuntimeError("MapBlock assembly added an entity outside GRID/SHELL/VOLUME")
    new_volume_ids = sorted(ident for kind, ident in additions if kind == "VOLUME")
    new_shell_ids = sorted(ident for kind, ident in additions if kind == "SHELL")
    new_grid_ids = sorted(ident for kind, ident in additions if kind == "GRID")
    if new_volume_ids != sorted(volume_ids) or len(new_volume_ids) != len(prepared["clean"]["face_groups"]):
        raise RuntimeError("final VOLUME IDs differ from the returned native VOLUME set")
    if after.get("cad_sha256") != prepared["before"].get("cad_sha256"):
        raise RuntimeError("MapBlock assembly changed the protected CAD export hash")
    before_grids = {row["id"]: row for row in prepared["before"].get("grids", [])}
    after_grids = {row["id"]: row for row in after.get("grids", [])}
    if not before_grids.keys() <= after_grids.keys() or any(
            before_grids[ident] != after_grids[ident] for ident in before_grids):
        raise RuntimeError("MapBlock assembly changed an original GRID card or coordinate")

    inspected = map_block_inspection.capture(state, base, mesh, constants, deck, new_volume_ids)
    groups = _match_volume_groups(inspected["volumes"], prepared["clean"]["face_groups"])
    if inspected["cad_witness"]["source_snapshot_sha256"] != prepared["parent_witness"]["source_snapshot_sha256"]:
        raise RuntimeError("CAD witness changed while independently reading back the new MapBlocks")
    expected_face_ids = {face_id for group in prepared["clean"]["face_groups"] for face_id in group}
    if {row["face_id"] for row in inspected["face_membership"]} != expected_face_ids:
        raise RuntimeError("native FACE membership does not cover the complete assembly")

    # Reuse the batch fill preflight only after the native volumes exist. It
    # checks all selected VOLUMEs together, including ready state, geometry,
    # face lattices, property/Part ownership and their shared-face layout.
    batch_check = batch.preflight(
        state, base, mesh, constants,
        {"deck": deck, "volume_ids": new_volume_ids},
    )
    if (batch_check["clean"]["volume_ids"] != new_volume_ids
            or features._hash(batch_check["before"]) != features._hash(after)):
        raise RuntimeError("independent batch preflight did not bind the same complete after snapshot")
    batch_volumes = _match_volume_groups(batch_check["block"]["volumes"], prepared["clean"]["face_groups"])
    if any(batch_volumes[key]["volume_id"] != groups[key]["volume_id"] for key in groups):
        raise RuntimeError("batch preflight and independent VOLUME inspection disagree on FACE ownership")
    expected_volume_owner_count = {
        face_id: len(owners) for face_id, owners in prepared["layout"]["face_owners"].items()
    }
    for key, row in groups.items():
        ident = row["volume_id"]
        if (row["part_id"] != prepared["part_id"]
                or row["native_mapping_status"] != "native_ready"
                or not row["caps_match_complete_boundary"]
                or not row["face_mesh_membership_complete"]):
            raise RuntimeError(f"VOLUME {ident} is not a complete ready MapBlock in the source Part")
        fields = row["card"]["fields"]
        if (fields.get("Type") != "Map Block"
                or fields.get("Status") != "Unmeshed"
                or fields.get("PID") != prepared["clean"]["property_id"]):
            raise RuntimeError(f"VOLUME {ident} status or target PSOLID binding is incorrect")
        proof = next(item for item in prepared["proofs"] if tuple(item["face_ids"]) == key)
        expected_cap = {tuple(item["cap_pair"]) for item in proof["feasible_t1"]}
        actual_cap = tuple(sorted(row["caps"]["master"]+row["caps"]["slave"]))
        if actual_cap not in expected_cap:
            raise RuntimeError(f"VOLUME {ident} native caps do not match a finite-domain T1 plan")
        raw = new[("VOLUME", ident)]
        if raw.get("part_id") != prepared["part_id"]:
            raise RuntimeError(f"VOLUME {ident} raw Part owner changed")
        if (_required_field(raw, "ID", f"VOLUME {ident}") != ident
                or _required_field(raw, "PID", f"VOLUME {ident}") != prepared["clean"]["property_id"]
                or _required_field(raw, "Type", f"VOLUME {ident}") != "Map Block"):
            raise RuntimeError(f"VOLUME {ident} raw card identity/type/property differs from readback")
    for row in inspected["face_membership"]:
        face_id = row["face_id"]
        if not row.get("membership_complete") or len(row["macro_shell_ids"]) == 0:
            raise RuntimeError(f"FACE {face_id} does not have complete new SHELL ownership")
        if native._volume_count(_required_field(new[("FACE", face_id)], "Num. Volumes", f"FACE {face_id}")) != expected_volume_owner_count[face_id]:
            raise RuntimeError(f"FACE {face_id} Num. Volumes differs from its assembly owners")
    all_owned_shells = sorted({ident for row in inspected["face_membership"]
                               for ident in row["macro_shell_ids"]})
    if all_owned_shells != new_shell_ids:
        raise RuntimeError("new SHELL inventory is not exactly the complete FACE membership")
    source, cons_boundaries, face_lattices = _check_face_lattices(
        base, constants, deck, {**prepared, "after": after}, inspected
    )
    if not set(new_grid_ids) <= {node["id"] for node in source["nodes"]}:
        raise RuntimeError("new GRID inventory contains an orphan outside every new SHELL")
    _check_new_grids(base, deck, new_grid_ids)
    after_grid_map = {ident: row for (kind, ident), row in new.items() if kind == "GRID"}
    for node in source["nodes"]:
        row = after_grid_map.get(node["id"])
        if row is None:
            raise RuntimeError(f"captured surface GRID {node['id']} is absent from the full after snapshot")
        if (_required_field(row, "TYPE", f"GRID {node['id']}") != "GRID"
                or _required_field(row, "NID", f"GRID {node['id']}") != node["id"]):
            raise RuntimeError(f"GRID {node['id']} raw identity differs from surface capture")
        for axis, name in enumerate(("X1", "X2", "X3")):
            actual = features._number(_required_field(row, name, f"GRID {node['id']}"),
                                       f"GRID {node['id']} {name}")
            if not math.isclose(actual, node["xyz"][axis], rel_tol=0, abs_tol=1e-16):
                raise RuntimeError(f"GRID {node['id']} raw coordinate differs from surface capture")
    elements = source["elements"]
    for element in elements:
        if element["type"] != "CQUAD4" or element["property_id"] != prepared["source_pid"]:
            raise RuntimeError("new SHELL has a non-CQUAD4 type or incorrect source PSHELL")
        row = new[("SHELL", element["id"])]
        if row.get("part_id") != prepared["part_id"]:
            raise RuntimeError("new SHELL is not owned by the source Part")
    if native._volume_count(_required_field(new[("PSHELL", prepared["source_pid"])],
                                            "Num.Elem", "source PSHELL")) != len(new_shell_ids):
        raise RuntimeError("source PSHELL Num.Elem differs from the complete new SHELL inventory")

    part_key = ("ANSAPART", prepared["part_id"])
    allowed_part, part_aggregate = single._part_summaries(
        base, deck, prepared, new[part_key]
    )
    if part_aggregate.get("part_id") != prepared["part_id"]:
        raise RuntimeError("fresh Part aggregate is bound to another Part")
    diagonal = features._number(new[part_key]["fields"].get("Diagonal"), "Part Diagonal", True)
    face_center_readback = []; cons_vectors = []; cons_bounds = []; face_geometry = []
    for key in old:
        before_row, after_row = copy.deepcopy(old[key]), copy.deepcopy(new[key])
        if key == part_key:
            for name in allowed_part:
                before_row["fields"].pop(name, None)
                after_row["fields"].pop(name, None)
        elif key[0] == "FACE":
            before_count = native._volume_count(_required_field(before_row, "Num. Volumes", f"before FACE {key[1]}"))
            after_count = native._volume_count(_required_field(after_row, "Num. Volumes", f"after FACE {key[1]}"))
            if before_count != 0 or after_count != expected_volume_owner_count[key[1]]:
                raise RuntimeError(f"FACE {key[1]} Num. Volumes transition is incorrect")
            face = features._entity(base, deck, "FACE", key[1])
            mass = base.CalcElementMass(face, no_nsm=False, deck=deck)
            face_center_readback.append({
                "face_id": key[1],
                **single._check_face_center(before_row, after_row, mass, diagonal),
            })
            face_geometry.extend({'face_id':key[1],**v} for v in single._check_face_geometry_roundoff(before_row,after_row,prepared['parent_witness']))
            before_row["fields"].pop("Num. Volumes")
            after_row["fields"].pop("Num. Volumes")
        elif key == ("PSHELL", prepared["source_pid"]):
            before_count = native._volume_count(_required_field(before_row, "Num.Elem", "before source PSHELL"))
            after_count = native._volume_count(_required_field(after_row, "Num.Elem", "after source PSHELL"))
            if before_count != 0 or after_count != len(new_shell_ids):
                raise RuntimeError("source PSHELL Num.Elem transition differs from new SHELL inventory")
            before_row["fields"].pop("Num.Elem")
            after_row["fields"].pop("Num.Elem")
        elif key[0] == "CONS":
            cons_vectors.extend({'cons_id':key[1],**v} for v in single._check_cons_vector_roundoff(before_row,after_row))
            edge = next((row for row in cons_boundaries if row["cons_id"] == key[1]), None)
            if edge is None:
                raise RuntimeError(f"CONS {key[1]} perimeter evidence is missing")
            cons_bounds.extend({'cons_id':key[1],**v} for v in single._check_cons_bounds_roundoff(before_row,after_row,[p['xyz'] for p in edge['hot_points']]))
            for name in ("Num. of Nodes", "Elem.Length"):
                _required_field(before_row, name, f"before CONS {key[1]}")
                _required_field(after_row, name, f"after CONS {key[1]}")
            if native._volume_count(_required_field(after_row, "Num. of Nodes", f"after CONS {key[1]}")) != edge["point_count"]-2:
                raise RuntimeError(f"CONS {key[1]} Num. of Nodes differs from captured perimeter points")
            before_row["fields"].pop("Num. of Nodes", None)
            before_row["fields"].pop("Elem.Length", None)
            after_row["fields"].pop("Num. of Nodes", None)
            after_row["fields"].pop("Elem.Length", None)
        if before_row != after_row:
            error=RuntimeError(f"MapBlock assembly changed a protected original entity: {key}")
            error.diagnostics={'entity':list(key),'before':old[key],'after':new[key],
                               'comparison_before':before_row,'comparison_after':after_row}
            raise error

    if not (prepared["before"].get("cad_sha256") == after.get("cad_sha256")):
        raise RuntimeError("MapBlock assembly changed the protected CAD hash")
    guards._same_context(state, deck, prepared["context"])
    return {
        "new_volume_ids": new_volume_ids,
        "created_shell_ids": new_shell_ids,
        "new_grid_ids": new_grid_ids,
        "volume_group_map": [
            {"volume_id": row["volume_id"], "face_ids": list(key)}
            for key, row in sorted(groups.items())
        ],
        "after_scope_sha256": features._hash(after),
        "native_mapping_status": "native_ready",
        "batch_preflight": {
            "plan_sha256": batch_check["plan_sha256"],
            "source_snapshot_sha256": batch_check["block"]["source_snapshot_sha256"],
            "layout": batch_check["layout"],
            "volume_count": len(batch_check["proofs"]),
        },
        "face_lattices": face_lattices,
        "native_part_aggregates": part_aggregate,
        "native_asset_cleanup": native_asset_cleanup or [],
        "face_center_readback": face_center_readback,
        "cons_vector_roundoff": cons_vectors,
        "cons_bounds_roundoff": cons_bounds,
        "face_geometry_roundoff": face_geometry,
        "property_id": prepared["clean"]["property_id"],
        "solid_mesh_generated": False,
        "continuous_export_domain_verified": True,
        "exact_native_CAD_equivalence_proven": False,
        "project_quality_acceptance": "not_evaluated",
    }


def apply_prepared(state, base, mesh, constants, params, prepared):
    clean = normalize(params, True)
    result = {
        "ok": False,
        "scope_verified": False,
        "api_called": False,
        "possible_effects": False,
        "recovery_required": False,
        "plan_sha256": prepared["plan_sha256"],
        "new_volume_ids": [],
        "created_shell_ids": [],
        "new_grid_ids": [],
        "created_auto_psolid_ids": [],
        "created_auto_mat1_ids": [],
    }
    try:
        if clean["expected_plan_sha256"] != prepared["plan_sha256"]:
            raise ValueError("MapBlock assembly plan hash is stale")
        if {key: clean[key] for key in _PARAMS} != prepared["clean"]:
            raise ValueError("apply parameters differ from the prepared assembly plan")
        backup_args = native._backup_preflight(base, clean)
        result["possible_effects"] = True
        result["backup"] = native._verify_backup(base, constants, *backup_args)
        refreshed = preflight(state, base, mesh, constants, prepared["clean"])
        if refreshed["plan_sha256"] != prepared["plan_sha256"]:
            raise RuntimeError("model/settings changed during backup; MapBlock was not called")
        prepared = refreshed
        def callback():
            guards._same_context(state, clean["deck"], prepared["context"])
            result["api_called"] = True
            result["possible_effects"] = True
            returned = mesh.MapBlock([
                features._entity(base, clean["deck"], "FACE", face_id)
                for face_id in sorted({face for group in clean["face_groups"] for face in group})
            ])
            return _bind_assets(state, base, mesh, constants, prepared, returned, result)

        prepared["state"] = state
        volume_ids = meshing_context.run_with_context(
            state, base, mesh, clean["target_length"], callback, result,
            expected_snapshot_sha256=prepared["settings"]["snapshot_sha256"],
        )
        result.update(readback(
            state, base, mesh, constants, prepared, volume_ids,
            native_asset_cleanup=result.get("native_asset_cleanup"),
        ))
        result.update(ok=True, scope_verified=True, possible_effects=False, recovery_required=False)
    except Exception as exc:
        result.update(
            error=type(exc).__name__+": "+str(exc),
            recovery_required=result["possible_effects"],
        )
        if getattr(exc, "diagnostics", None):
            result["diagnostics"] = exc.diagnostics
        try:
            guards._same_context(state, clean["deck"], prepared["context"])
            observed = cad_map._snapshot(state, base, clean["deck"])
            old, new = _raw_scope_map(prepared["before"], "before"), _raw_scope_map(observed, "observed after failure")
            added = new.keys()-old.keys()
            result["new_volume_ids"] = sorted(ident for kind, ident in added if kind == "VOLUME")
            result["created_shell_ids"] = sorted(ident for kind, ident in added if kind == "SHELL")
            result["new_grid_ids"] = sorted(ident for kind, ident in added if kind == "GRID")
            result["created_auto_psolid_ids"] = sorted(ident for kind, ident in added if kind == "PSOLID")
            result["created_auto_mat1_ids"] = sorted(ident for kind, ident in added if kind == "MAT1")
        except Exception as read_error:
            result["observation_error"] = str(read_error)
    return result
