"""Fill four explicit, already prepared annular Map Blocks with VolumesRemesh.

The finite FE boundary is preserved and audited. Native Ready state and nodal
projection do not prove continuous CAD coverage or native mesh quality.
"""
import math
from collections import Counter

from . import annular_map_prepare, annular_source, cad_map, cons_seed as seeds
from . import geometry_features as features, inspection, map_block_batch
from . import map_block_fill_audit, map_block_inspection, meshing_context, part_aggregates
from . import structured_native as native, volume_meshing as guards
from .structured_batch import source_subset


_PARAMS = ("deck", "volume_ids", "annular_options")
_OPTIONS = ("source_face_ids", "center", "axis", "inner_radius", "outer_radius", "distance", "max_chord_error")
_TOLERANCE = 1e-6
_COUNTS = ("Hexas", "Pentas", "Tetras", "Pyramids", "Polyhedrals", "Grids", "Internal grids")


def normalize(params, applying=False):
    inspection._params(params, _PARAMS + (("expected_plan_sha256", "backup_path") if applying else ()))
    clean = {"deck": native._positive_int(params["deck"], "deck"),
             "volume_ids": sorted(native._id_list(params["volume_ids"], "volume_ids", 4))}
    if len(clean["volume_ids"]) != 4:
        raise ValueError("annular fill requires exactly four explicit volume_ids")
    options = params["annular_options"]
    inspection._params(options, _OPTIONS)
    options = {key: options[key] for key in _OPTIONS}
    options["source_face_ids"] = sorted(native._id_list(options["source_face_ids"], "source_face_ids", 4))
    if len(options["source_face_ids"]) != 4:
        raise ValueError("annular fill requires four explicit source_face_ids")
    for key in ("center", "axis"):
        options[key] = native._point(options[key], key)
    if not math.isclose(math.hypot(*options["axis"]), 1., rel_tol=0., abs_tol=1e-12):
        raise ValueError("annular axis must be an explicit unit vector")
    for key in ("inner_radius", "outer_radius", "distance", "max_chord_error"):
        options[key] = features._number(options[key], key, True)
    if options["inner_radius"] >= options["outer_radius"]:
        raise ValueError("radii must satisfy 0 < inner_radius < outer_radius")
    if not all(math.isfinite(c + a*options["distance"]) for c, a in zip(options["center"], options["axis"])):
        raise ValueError("declared annular target coordinates must be finite")
    clean["annular_options"] = options
    if applying:
        clean.update(expected_plan_sha256=native._validate_digest(params["expected_plan_sha256"], "expected_plan_sha256"),
                     backup_path=params["backup_path"])
    return clean


def _node_envelope(snapshot, options):
    axial, radii = {}, []
    for row in snapshot["nodes"]:
        delta = [row["xyz"][i]-options["center"][i] for i in range(3)]
        z = math.fsum(delta[i]*options["axis"][i] for i in range(3))
        radius = math.hypot(*(delta[i]-z*options["axis"][i] for i in range(3)))
        if (not -_TOLERANCE <= z <= options["distance"]+_TOLERANCE
                or not options["inner_radius"]-options["max_chord_error"]-_TOLERANCE <= radius <= options["outer_radius"]+_TOLERANCE):
            raise ValueError("mesh node lies outside the declared finite annular envelope")
        axial[row["id"]] = z
        radii.append(radius)
    if not radii:
        raise ValueError("annular mesh has no nodes")
    return axial, {"minimum_node_radius": min(radii), "maximum_node_radius": max(radii),
                   "axial_range": [min(axial.values()), max(axial.values())],
                   "continuous_CAD_coverage_proven": False}


def _cap_audit(snapshot, options, center, *, tolerance=_TOLERANCE):
    result = annular_source.audit(snapshot, {key: options[key] for key in
        ("axis", "inner_radius", "outer_radius", "max_chord_error")} | {"center": center, "tolerance": tolerance},
        max_elements=400)
    if result["status"] != "verified_mesh_template":
        raise ValueError("annular cap template is unsupported: " + result.get("reason", "incomplete"))
    return result


def preflight(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize({k: params[k] for k in _PARAMS})
    deck = inspection._deck(base, clean)
    native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    if not callable(getattr(mesh, "VolumesRemesh", None)):
        raise NotImplementedError("native VolumesRemesh is required")
    if seeds._collect(base, deck, None, "SOLID"):
        raise ValueError("annular fill requires an unfilled local model")
    if sorted(e._id for e in seeds._collect(base, deck, None, "VOLUME")) != clean["volume_ids"]:
        raise ValueError("volume_ids must cover every current model VOLUME")
    block = map_block_inspection.capture(state, base, mesh, constants, deck, clean["volume_ids"])
    if block["feature_context"]["units"] != "millimeter":
        raise ValueError("annular fill requires millimeter units")
    faces = sorted({i for v in block["volumes"] for i in v["face_ids"]})
    shells = sorted(e["id"] for e in block["surface_mesh"]["elements"])
    if (len(faces) > 64 or not 1 <= len(shells) <= 2000
            or sorted(e._id for e in seeds._collect(base, deck, None, "FACE")) != faces
            or sorted(e._id for e in seeds._collect(base, deck, None, "SHELL")) != shells):
        raise ValueError("annular fill requires the complete bounded FACE/SHELL boundary")
    if any(not v["face_mesh_membership_complete"] or v["native_mapping_status"] != "native_ready"
           or v["card"]["fields"].get("Status") != "Unmeshed" or v["card"]["fields"].get("Frozen") != "NO"
           or any(native._volume_count(v["card"]["fields"].get(k)) != 0 for k in _COUNTS) for v in block["volumes"]):
        raise ValueError("all four Map Blocks must have complete boundary membership and be ready, empty and unfrozen")
    before = cad_map._snapshot(state, base, deck)
    rows = native._scope_map(before)
    allowed = annular_map_prepare._OLD_TYPES | {"SHELL", "VOLUME"}
    if any(kind not in allowed for kind, ident in rows):
        raise ValueError("annular fill requires an isolated local model with supported cards")
    parts = seeds._collect(base, deck, None, "ANSAPART")
    if len(parts) != 1 or any(v["part_id"] != parts[0]._id for v in block["volumes"]):
        raise ValueError("annular fill requires one complete source Part")
    part = parts[0]
    if any(rows[("ANSAPART", part._id)]["fields"].get(k) != "NO" for k in ("Freeze", "Is Group")):
        raise ValueError("annular Part must be unfrozen and not a group")
    owners = {}
    for volume in block["volumes"]:
        for face in volume["face_ids"]:
            owners.setdefault(face, []).append(volume["volume_id"])
    if any(len(ids) not in (1, 2) for ids in owners.values()):
        raise ValueError("each boundary FACE must have one or two block owners")
    connected = {clean["volume_ids"][0]}
    for _ in range(3):
        connected.update(i for ids in owners.values() if len(ids) == 2 and connected.intersection(ids) for i in ids)
    if connected != set(clean["volume_ids"]):
        raise ValueError("the four annular blocks must form one FACE-connected assembly")
    for face, ids in owners.items():
        row = rows[("FACE", face)]
        if (row["assignments"]["__part__"] != part._id or row["fields"].get("Frozen") != "NO"
                or native._volume_count(row["fields"].get("Num. Volumes")) != len(ids)):
            raise ValueError("annular FACE ownership, freeze state or volume count differs")
    properties = {}
    for volume in block["volumes"]:
        prop_id = native._positive_int(volume["card"]["fields"].get("PID"), "MapBlock PID")
        if prop_id not in properties:
            _, evidence = native._psolid_preflight(base, deck, prop_id)
            if str(evidence["mat1"]["fields"].get("DEFINED")).casefold() != "yes":
                raise ValueError("annular PSOLID requires an explicitly defined MAT1")
            if native._volume_count(rows[("PSOLID", prop_id)]["fields"].get("Num.Elem")) != 0:
                raise ValueError("annular PSOLID must have zero SOLID count")
            properties[prop_id] = evidence
        if rows[("VOLUME", volume["volume_id"])].get("solid_ids") != []:
            raise ValueError("annular MapBlock already has SOLID members")
        for name in ("Volume", "Mass"):
            features._number(volume["card"]["fields"].get(name), "MapBlock " + name, True)
    options = clean["annular_options"]
    source_faces = options["source_face_ids"]
    if (not set(source_faces) <= set(faces) or any(len(owners[f]) != 1 for f in source_faces)
            or len({owners[f][0] for f in source_faces}) != 4):
        raise ValueError("four exterior source FACEs must belong to four distinct Map Blocks")
    members = {r["face_id"]: r for r in block["face_membership"]}
    source_ids = sorted({s["id"] for f in source_faces for s in members[f]["shells"]})
    if not 1 <= len(source_ids) <= 400:
        raise ValueError("annular source exceeds the 400-SHELL template budget")
    source = native.capture_source(base, constants, deck, source_ids)
    source_template = _cap_audit(source, options, options["center"])
    surface = {"deck": deck, **block["surface_mesh"], "ownership": source["ownership"]}
    axial, envelope = _node_envelope(surface, options)
    target_ids = [e["id"] for e in surface["elements"]
                  if all(abs(axial[n]-options["distance"]) <= _TOLERANCE for n in e["nodes"])]
    target = source_subset(surface, target_ids)
    # The opposite native cap may contain approximated CAD perimeter points.
    # Keep their cards/coordinates exact; bound radius matching separately from
    # the source definition tolerance and never exceed the declared budget.
    target_template = _cap_audit(target, options,
        [c+a*options["distance"] for c, a in zip(options["center"], options["axis"])],
        tolerance=min(.001, options["max_chord_error"]))
    part_aggregates.capture(base, deck, part, rows[("ANSAPART", part._id)])
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)
    body = {"params": clean, "context": context, "scope_sha256": features._hash(before),
            "block_sha256": block["source_snapshot_sha256"], "settings_sha256": settings["snapshot_sha256"]}
    return {"annular": True, "clean": clean, "context": context, "before": before,
            "block": block, "source": source, "part": part, "properties": properties,
            "source_template": source_template, "target_template": target_template, "envelope": envelope,
            "settings": settings, "plan_sha256": features._hash(body),
            "proofs": [{"volume_id": i} for i in clean["volume_ids"]], "layout": {"face_owners": owners}}


def public_plan(prepared):
    return {"status": "ready_for_controlled_apply", "mode": "annular_native_fill",
            "params": prepared["clean"], "plan_sha256": prepared["plan_sha256"],
            "scope_sha256": features._hash(prepared["before"]),
            "meshing_context_sha256": prepared["settings"]["snapshot_sha256"],
            "boundary_summary": prepared["block"]["summary"],
            "source_template": prepared["source_template"], "target_template": prepared["target_template"],
            "native_api": "mesh.VolumesRemesh", "source_layer_mapping_proven": False,
            "continuous_CAD_coverage_proven": False, "project_quality_acceptance": "not_evaluated",
            "usable_for_mutation": False}


def readback(state, base, mesh, constants, prepared):
    deck = prepared["clean"]["deck"]
    guards._same_context(state, deck, prepared["context"])
    after = cad_map._snapshot(state, base, deck)
    rows = native._scope_map(after)
    blocks = []
    for volume in prepared["block"]["volumes"]:
        ident = volume["volume_id"]
        solid_ids = rows[("VOLUME", ident)]["solid_ids"]
        output = native.capture_mesh(base, constants, deck, solid_ids)
        _node_envelope(output, prepared["clean"]["annular_options"])
        fields = volume["card"]["fields"]
        blocks.append({"volume_id": ident, "part_id": prepared["part"]._id, "property_id": fields["PID"],
                       "solid_ids": solid_ids, "output": output, "expected_volume": fields["Volume"], "expected_mass": fields["Mass"]})
    aggregate = part_aggregates.capture(base, deck, prepared["part"], rows[("ANSAPART", prepared["part"]._id)])
    checked = map_block_fill_audit.audit_many_changes(prepared["before"], after, blocks=blocks,
                                                    part_aggregate=aggregate, allow_penta=True)
    current = map_block_inspection.capture(state, base, mesh, constants, deck, prepared["clean"]["volume_ids"])
    if (current["face_membership"] != prepared["block"]["face_membership"]
            or current["surface_mesh"] != prepared["block"]["surface_mesh"]
            or [v["caps"] for v in current["volumes"]] != [v["caps"] for v in prepared["block"]["volumes"]]):
        raise RuntimeError("annular fill changed protected native boundaries or surface mesh")
    audit = map_block_batch._active_audit(state, base, mesh, constants, prepared, after,
                                        tolerance=prepared["clean"]["annular_options"]["max_chord_error"])
    if not audit["all_blocks_filled"] or audit["expected_components"] != 1:
        raise RuntimeError("annular fill must form one connected assembly of all four blocks")
    settings = meshing_context.capture(state, base, mesh)
    if settings["snapshot_sha256"] != prepared["settings"]["snapshot_sha256"]:
        raise RuntimeError("annular fill changed global meshing/quality settings")
    guards._same_context(state, deck, prepared["context"])
    return {**checked, **audit, "native_part_aggregates": aggregate,
            "solid_type_counts": dict(sorted(Counter(e["type"] for b in blocks for e in b["output"]["elements"]).items())),
            "meshing_context": {"before": prepared["settings"], "after": settings, "unchanged": True},
            "verification": {"source_layer_mapping_proven": False, "boundary_mesh_unchanged": True},
            "continuous_CAD_coverage_proven": False, "solid_mesh_generated": True,
            "project_quality_acceptance": "not_evaluated"}


def apply_prepared(state, base, mesh, constants, params, prepared):
    clean = normalize(params, True)
    result = {"ok": False, "mode": "annular_native_fill", "scope_verified": False, "api_called": False,
              "possible_effects": False, "recovery_required": False, "backup_attempted": False,
              "plan_sha256": prepared["plan_sha256"], "new_solid_ids": [], "new_grid_ids": [],
              "original_operation_must_be_queried": True}
    try:
        if clean["expected_plan_sha256"] != prepared["plan_sha256"] or {k: clean[k] for k in _PARAMS} != prepared["clean"]:
            raise ValueError("annular fill plan or parameters changed")
        args = native._backup_preflight(base, clean)
        result.update(backup_attempted=True, possible_effects=True)
        result["backup"] = native._verify_backup(base, constants, *args)
        refreshed = preflight(state, base, mesh, constants, prepared["clean"])
        if refreshed["plan_sha256"] != prepared["plan_sha256"]:
            raise RuntimeError("model/settings changed during backup; VolumesRemesh was not called")
        prepared = refreshed
        volumes = [features._entity(base, clean["deck"], "VOLUME", i) for i in clean["volume_ids"]]
        guards._same_context(state, clean["deck"], prepared["context"])
        result["api_called"] = True
        returned = mesh.VolumesRemesh(volumes)
        result["native_return"] = returned if type(returned) is int else repr(returned)
        if type(returned) is not int or returned != 1:
            raise RuntimeError("VolumesRemesh did not return exact success int 1")
        result.update(readback(state, base, mesh, constants, prepared))
        result.update(ok=True, status="verified_native_fill", scope_verified=True, possible_effects=False, recovery_required=False)
    except Exception as exc:
        result.update(error=native._error_text(exc), recovery_required=result["possible_effects"],
                      status="recovery_required" if result["possible_effects"] else "failed")
        if type(getattr(exc, "diagnostics", None)) is dict:
            result["diagnostics"] = exc.diagnostics
        if result["backup_attempted"] or result["api_called"]:
            result.update(new_solid_ids=None, new_grid_ids=None, inventory_observation_complete=False)
            try:
                guards._same_context(state, clean["deck"], prepared["context"])
                after = cad_map._snapshot(state, base, clean["deck"])
                old, new = native._scope_map(prepared["before"]), native._scope_map(after)
                added = new.keys()-old.keys()
                result.update(new_solid_ids=sorted(i for k, i in added if k == "SOLID"),
                              new_grid_ids=sorted(i for k, i in added if k == "GRID"), inventory_observation_complete=True,
                              removed_original_entities=[{"type": k, "id": i} for k, i in sorted(old.keys()-new.keys())],
                              unexpected_new_entities=[{"type": k, "id": i} for k, i in sorted(added) if k not in ("GRID", "SOLID")])
            except Exception as observation:
                result["observation_error"] = native._error_text(observation)
    return result
