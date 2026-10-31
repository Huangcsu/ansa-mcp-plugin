"""Restricted native preparation of an explicitly partitioned annular source.

One complete, unmeshed, saved NASTRAN Part is the entire input domain.  This
module never discovers holes, partitions CAD, fills volumes, or proves continuous
CAD coverage.  Only the target length is changed before the single MapBlock call.
"""
import copy
import json
import math
from collections import Counter
from pathlib import Path

from . import annular_source, cad_map, cons_seed as seeds, geometry_features as features
from . import inspection, map_block_inspection, meshing
from . import meshing_context, part_aggregates, structured_boundary, structured_evidence
from . import structured_native as native, volume_meshing as guards


_PARAMS = ("deck", "face_ids", "source_face_ids", "center", "axis", "inner_radius",
           "outer_radius", "distance", "target_length", "max_chord_error", "property_id")
_OLD_TYPES = frozenset({"ANSAPART", "FACE", "CONS", "HOT POINT", "GRID", "PSHELL", "PSOLID", "MAT1", "WPLANE"})
_NEW_TYPES = frozenset({"GRID", "SHELL", "VOLUME", "PSOLID", "MAT1"})
_TOLERANCE = 1e-6
_MAX_SOURCE_SHELLS = 200
_MAX_BOUNDARY_SHELLS = 5000
_SOURCE_MESH_BUDGET_FORMULA = (
    "4*ceil(2*pi*outer_radius/(4*target_length))"
    "*ceil((outer_radius-inner_radius)/target_length)"
)
_PART_SUMMARY_FIELDS = {"PID", "PIDs Number", "Thickness", "Material Name", "Properties Info",
                        "Materials Info", "Last Edit"}


def _source_mesh_budget(params):
    """Estimate a bounded source-face shell count before invoking native MapBlock."""
    inner_radius = params["inner_radius"]
    outer_radius = params["outer_radius"]
    distance = params["distance"]
    target_length = params["target_length"]
    # The source is an axial annulus: its second dimension is radial width,
    # not sweep distance. Quarter-sector counts cover the four explicit blocks.
    # Check ratios before ceil/product; these estimates are not native bounds.
    def segments(length, limit, message):
        ratio = length / target_length
        if not math.isfinite(ratio) or ratio > limit:
            raise ValueError(message)
        return max(1, math.ceil(ratio))

    source_error = "annular source shell estimate exceeds the existing 200-shell limit"
    outer_ratio = outer_radius / target_length
    if not math.isfinite(outer_ratio) or outer_ratio > _MAX_SOURCE_SHELLS / math.tau:
        raise ValueError(source_error)
    circumferential_segments = 4 * max(1, math.ceil(math.tau * outer_ratio / 4))
    radial_segments = segments(outer_radius - inner_radius, _MAX_SOURCE_SHELLS, source_error)
    if circumferential_segments > _MAX_SOURCE_SHELLS // radial_segments:
        raise ValueError(source_error)
    estimated_source_shells = circumferential_segments * radial_segments

    boundary_error = "annular boundary shell estimate exceeds the existing 5000-shell inspection limit"
    sweep_segments = segments(distance, _MAX_BOUNDARY_SHELLS, boundary_error)
    inner_segments = 4 * max(1, math.ceil(math.tau * (inner_radius / target_length) / 4))
    # Two caps, inner/outer walls and four shared radial faces (counted once).
    side_segments = circumferential_segments + inner_segments + 4 * radial_segments
    if sweep_segments > (_MAX_BOUNDARY_SHELLS - 2 * estimated_source_shells) // side_segments:
        raise ValueError(boundary_error)
    estimated_boundary_shells = 2 * estimated_source_shells + side_segments * sweep_segments
    if estimated_source_shells > _MAX_SOURCE_SHELLS:
        raise ValueError("annular source shell estimate exceeds the existing 200-shell limit")
    return {
        "formula": _SOURCE_MESH_BUDGET_FORMULA,
        "circumferential_segments": circumferential_segments,
        "radial_segments": radial_segments,
        "sweep_segments": sweep_segments,
        "estimated_source_shells": estimated_source_shells,
        "maximum_source_shells": _MAX_SOURCE_SHELLS,
        "estimated_boundary_shells": estimated_boundary_shells,
        "maximum_boundary_shells": _MAX_BOUNDARY_SHELLS,
        "estimate_is_native_upper_bound": False,
    }


def _plan_sha256(clean, context, database, before, settings, source_mesh_budget):
    return features._hash({
        "params": clean,
        "context": context,
        "database": database,
        "scope_sha256": features._hash(before),
        "meshing_context_sha256": settings.snapshot_sha256,
        "source_mesh_budget": source_mesh_budget,
    })


def normalize(params, applying=False):
    inspection._params(params, _PARAMS + (("expected_plan_sha256", "backup_path") if applying else ()))
    clean = {key: params[key] for key in _PARAMS}
    for key in ("deck", "property_id"):
        clean[key] = native._positive_int(clean[key], key)
    clean["face_ids"] = sorted(native._id_list(clean["face_ids"], "face_ids", 64))
    clean["source_face_ids"] = sorted(native._id_list(clean["source_face_ids"], "source_face_ids", 4))
    if len(clean["source_face_ids"]) != 4 or not set(clean["source_face_ids"]) <= set(clean["face_ids"]):
        raise ValueError("four distinct source_face_ids must belong to the complete face_ids scope")
    for key in ("center", "axis"):
        clean[key] = native._point(clean[key], key)
    if not math.isclose(math.hypot(*clean["axis"]), 1., rel_tol=0., abs_tol=1e-12):
        raise ValueError("axis must be an explicit unit vector")
    for key in ("inner_radius", "outer_radius", "distance", "target_length", "max_chord_error"):
        try:
            clean[key] = features._number(clean[key], key, True)
        except OverflowError as exc:
            raise ValueError(f"{key} is outside the finite numeric range") from exc
    if clean["inner_radius"] >= clean["outer_radius"]:
        raise ValueError("radii must satisfy 0 < inner_radius < outer_radius")
    if not all(math.isfinite(c + a * clean["distance"]) for c, a in zip(clean["center"], clean["axis"])):
        raise ValueError("declared sweep target cannot be represented by finite coordinates")
    _source_mesh_budget(clean)
    if applying:
        clean["expected_plan_sha256"] = native._validate_digest(params["expected_plan_sha256"], "expected_plan_sha256")
        clean["backup_path"] = params["backup_path"]
    return clean


def _snapshot(state, base, deck, context):
    guards._same_context(state, deck, context)
    result = cad_map._snapshot(state, base, deck)
    guards._same_context(state, deck, context)
    return result


def _part_aggregate(base, deck, part, row):
    """Verify the native display aggregate, including valid zero-density inputs."""
    values = base.CalcElementMass(part, no_nsm=False, deck=deck)
    if type(values) not in (list, tuple) or len(values) != 22:
        raise RuntimeError("Part aggregate requires 22 native values")
    values = [features._number(value, "native Part aggregate") for value in values]
    diagonal = features._number(row["fields"].get("Diagonal"), "Part diagonal", True)
    scales = [abs(values[0])] + [diagonal] * 3 + [max(abs(v) for v in values[4:7])] * 6
    for key, expected, scale in zip(part_aggregates.MASS_FIELDS, values, scales):
        actual = features._number(row["fields"].get(key), "Part " + key)
        if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=scale * 1e-14):
            raise RuntimeError("Part " + key + " differs from native CalcElementMass")
    return {"part_id": part._id, "origin": "CalcElementMass(Part)",
            "values": dict(zip(part_aggregates.MASS_FIELDS, values[:10])),
            "meaning": "native mixed CAD/FE/unfilled-volume aggregate consistency"}


def _expected_part_summary(scope, source_pids, target_pid):
    props, materials, names, thicknesses = [], [], {}, []
    entries = [(pid, "PSHELL") for pid in source_pids] + [(target_pid, "PSOLID")]
    for ident, kind in sorted(entries):
        fields = scope[(kind, ident)]["fields"]
        props.append({"Id": str(ident), "Name": fields["Name"], "Type": kind})
        mids = {native._positive_int(fields[key], key)
                for key in (("MID1", "MID2", "MID3", "MID4") if kind == "PSHELL" else ("MID",))
                if fields.get(key) is not None}
        mats = []
        for mid in sorted(mids):
            name = scope[("MAT1", mid)]["fields"]["Name"]
            mats.append({"Id": str(mid), "Name": name, "Type": "MAT1"})
            names[mid] = name
        materials.append({str(ident): mats})
        thicknesses.append(format(features._number(fields["T"], "PSHELL thickness", True), "g")
                           if kind == "PSHELL" else "-")
    return {"PID": ", ".join(p["Id"] for p in props), "PIDs Number": len(props),
            "Thickness": ", ".join(thicknesses),
            "Material Name": ", ".join(names[mid] for mid in sorted(names)),
            "Properties Info": props, "Materials Info": materials}


def preflight(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize({key: params[key] for key in _PARAMS})
    source_mesh_budget = _source_mesh_budget(clean)
    deck = inspection._deck(base, clean)
    native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    for owner, names in ((base, ("CalcElementMass", "BoundBox", "GetFacesOfCons", "SetEntityCardValues", "GetEntityCardValues", "DeleteEntity", "GetEntity")),
                         (mesh, ("SetMeshParamTargetLength", "MapBlock", "GetVolumeBoundary", "GetMapVolumeBoundaries"))):
        for name in names:
            if not callable(getattr(owner, name, None)):
                raise NotImplementedError("required native API unavailable: " + name)
    database = guards._database_name(base)
    if not database or not Path(database).is_file():
        raise ValueError("annular source preparation requires an already saved database")
    if features._context(state, base, deck)["units"] != "millimeter":
        raise ValueError("annular source preparation requires millimeter model units")
    before = _snapshot(state, base, deck, context)
    scope = native._scope_map(before)
    if any(kind not in _OLD_TYPES for kind, _ in scope):
        raise ValueError("annular preparation requires an unmeshed CAD-only local model with supported cards")
    if sorted(ident for kind, ident in scope if kind == "FACE") != clean["face_ids"]:
        raise ValueError("face_ids must explicitly cover every current model FACE (at most 64)")
    for kind, ident in scope:
        if kind == "CONS":
            owners = base.GetFacesOfCons(features._entity(base, deck, kind, ident))
            if type(owners) not in (list, tuple) or not owners or any(e._id not in clean["face_ids"] for e in owners):
                raise ValueError("every CONS must belong to the complete explicit FACE domain")
    parts = [ident for kind, ident in scope if kind == "ANSAPART"]
    if len(parts) != 1:
        raise ValueError("annular preparation requires exactly one complete source Part")
    part_id = parts[0]
    fields = scope[("ANSAPART", part_id)]["fields"]
    if fields.get("Freeze") != "NO" or fields.get("Is Group") != "NO":
        raise ValueError("source Part must be unfrozen and not a group")
    source_pids = set()
    for ident in clean["face_ids"]:
        face, macro, shell_ids = meshing._face_snapshot(base, mesh, deck, ident)
        row = scope[("FACE", ident)]
        assignment = native._face_assignments(base, deck, face, ident)
        if (macro["macro_face_ids"] != [ident] or shell_ids or row["fields"].get("Frozen") != "NO"
                or native._volume_count(row["fields"].get("Num. Volumes")) != 0
                or assignment["part_id"] != part_id):
            raise ValueError("all FACEs must be independent, unfrozen, unmeshed and owned by the sole Part")
        source_pids.add(assignment["property_id"])
    _, property_evidence = native._psolid_preflight(base, deck, clean["property_id"])
    if str(property_evidence["mat1"]["fields"].get("DEFINED")).casefold() != "yes":
        raise ValueError("target PSOLID must reference an explicitly defined MAT1")
    source_pids = sorted(source_pids)
    _expected_part_summary(scope, source_pids, clean["property_id"])
    part = features._entity(base, deck, "ANSAPART", part_id)
    _part_aggregate(base, deck, part, scope[("ANSAPART", part_id)])
    settings = meshing_context._capture_context(state, base, mesh)
    guards._same_context(state, deck, context)
    plan_sha256 = _plan_sha256(clean, context, database, before, settings, source_mesh_budget)
    return {"clean": clean, "context": context, "before": before, "settings": settings,
            "part_id": part_id, "part": part, "source_pids": source_pids,
            "property_evidence": property_evidence, "source_mesh_budget": source_mesh_budget,
            "plan_sha256": plan_sha256}


def public_plan(prepared):
    # Inline and bounded: this plan never creates an unvalidated evidence kind.
    return {"ok": True, "status": "ready_for_controlled_apply", "params": prepared["clean"],
            "plan_sha256": prepared["plan_sha256"], "scope_sha256": features._hash(prepared["before"]),
            "meshing_context_sha256": prepared["settings"].snapshot_sha256,
            "source_mesh_budget": prepared["source_mesh_budget"],
            "annular_tolerance_mm": _TOLERANCE,
            "native_perimeter_policy": {"mode": "absolute_length_with_native_refinement", "maximum_local_target_length": prepared["clean"]["target_length"],
                                        "distortion_angle": 0., "existing_local_seeds_preserved": False},
            "summary": {"face_count": len(prepared["clean"]["face_ids"]), "expected_new_volume_count": 4,
                        "source_shell_limit": _MAX_SOURCE_SHELLS},
            "usable_for_mutation": False, "continuous_CAD_coverage_proven": False,
            "solid_mesh_generated": False}


def _inventory(before, after):
    old, new = native._scope_map(before), native._scope_map(after)
    if old.keys() - new.keys():
        raise RuntimeError("MapBlock removed original entities")
    added = new.keys() - old.keys()
    if any(kind not in _NEW_TYPES for kind, _ in added):
        raise RuntimeError("MapBlock created an unexpected entity type")
    return old, new, {kind: sorted(ident for k, ident in added if k == kind) for kind in _NEW_TYPES}


def _map_once(state, base, mesh, prepared, result):
    snapshot = prepared["settings"]
    config = {**meshing_context._summary(snapshot),
              "mpar": {"restore_applied": False, "verified": False},
              "criteria": {"restore_applied": False, "restored": False, "verified": False},
              "f11": {"restored": False, "verified": False},
              "settings": {"mesh_change_option_restored": False, "verified": False},
              "errors": {"operation_error": None, "restore_errors": []},
              "restore_errors": [], "restored": False, "restoration_attempted": False}
    result["meshing_context"] = config
    operation_error, returned = None, None
    try:
        guards._same_context(state, prepared["clean"]["deck"], prepared["context"])
        result["settings_api_called"] = True
        config["restoration_attempted"] = True
        rc = mesh.SetMeshParamTargetLength("absolute", prepared["clean"]["target_length"])
        if type(rc) is not int or rc != 1:
            raise RuntimeError("SetMeshParamTargetLength did not return success")
        actual = meshing_context._settings(state, base)
        length = features._number(float(actual["target_element_length"]), "native target length", True)
        if (not math.isclose(length, prepared["clean"]["target_length"], rel_tol=1e-9, abs_tol=1e-12)
                or any(actual[key] != snapshot.settings[key]
                       for key in ("element_type", "element_order", "mesh_change_option"))):
            raise RuntimeError("native settings do not confirm the sole target-length change")
        criteria = meshing_context._criteria(state, base)
        meshing_context.native_quality._require_criteria_match(snapshot.current_criteria, criteria, "binding annular preparation criteria")
        f11, errors = meshing_context._read_f11_states(state, base, snapshot.rows)
        if set(meshing_context._parse_criteria(criteria)[0]) != set(snapshot.rows) or errors or f11 != snapshot.f11:
            raise RuntimeError("quality criteria/F11 changed before MapBlock")
        guards._same_context(state, prepared["clean"]["deck"], prepared["context"])
        faces = [features._entity(base, prepared["clean"]["deck"], "FACE", ident)
                 for ident in prepared["clean"]["face_ids"]]
        result.update(api_called=True, possible_effects=True)
        returned = mesh.MapBlock(faces)
    except BaseException as exc:
        operation_error = exc
        config["errors"]["operation_error"] = meshing_context._error_record(exc)
    finally:
        if config["restoration_attempted"]:
            try:
                errors = meshing_context._restore_context(state, base, mesh, snapshot, config)
            except BaseException as exc:
                errors = [{"step": "restore_context", **meshing_context._error_record(exc)}]
                config["restore_errors"] = errors
                config["errors"]["restore_errors"] = errors
            if errors or not config.get("restored"):
                raise meshing_context.MeshingContextError(operation_error, errors or [{"step": "restoration_not_confirmed"}])
    if operation_error is not None:
        raise operation_error
    return returned


def _source_readback(block, prepared, additions):
    clean = prepared["clean"]
    volumes = block["volumes"]
    if len(volumes) != 4 or sorted(v["volume_id"] for v in volumes) != additions["VOLUME"]:
        raise RuntimeError("exactly four independently inventoried MapBlock volumes are required")
    face_uses, selected = Counter(), []
    for volume in volumes:
        fields = volume["card"]["fields"]
        if (volume["native_mapping_status"] != "native_ready" or not volume["caps_match_complete_boundary"]
                or not volume["face_mesh_membership_complete"] or volume["part_id"] != prepared["part_id"]
                or fields.get("State") != "Ready to Mesh" or fields.get("Status") != "Unmeshed"):
            raise RuntimeError("native Ready to Mesh, complete boundaries/caps and Part ownership are required")
        for key in ("Hexas", "Pentas", "Tetras", "Pyramids", "Polyhedrals", "Grids", "Internal grids"):
            if native._volume_count(fields.get(key)) != 0:
                raise RuntimeError("MapBlock unexpectedly contains a filled volume")
        face_uses.update(volume["face_ids"])
        source_faces = set(volume["face_ids"]) & set(clean["source_face_ids"])
        if len(source_faces) != 1:
            raise RuntimeError("each new volume must own exactly one declared source FACE")
        selected.extend(source_faces)
    if sorted(face_uses) != clean["face_ids"] or sorted(selected) != clean["source_face_ids"]:
        raise RuntimeError("new blocks do not explain the complete FACE domain and four source FACEs")
    membership = {row["face_id"]: row for row in block["face_membership"]}
    if sorted(membership) != clean["face_ids"]:
        raise RuntimeError("boundary membership does not cover every explicit FACE")
    all_members = [ident for row in membership.values() for ident in row["macro_shell_ids"]]
    if len(all_members) != len(set(all_members)) or sorted(all_members) != additions["SHELL"]:
        raise RuntimeError("SHELLs have missing or ambiguous individual FACE ownership")
    old = native._scope_map(prepared["before"])
    for face_id, row in membership.items():
        expected_pid = old[("FACE", face_id)]["assignments"]["__prop__"]
        if not row["membership_complete"] or row["macro_face_ids"] != [face_id] or any(
                shell["property_id"] != expected_pid for shell in row["shells"]):
            raise RuntimeError("boundary FACE membership or original PSHELL assignment is incomplete")
    ids = []
    for face_id in clean["source_face_ids"]:
        row = membership[face_id]
        if not row["membership_complete"] or row["macro_face_ids"] != [face_id] or not row["macro_shell_ids"]:
            raise RuntimeError("source FACE SHELL membership is incomplete")
        ids.extend(row["macro_shell_ids"])
    if not 0 < len(ids) <= _MAX_SOURCE_SHELLS or len(ids) != len(set(ids)):
        raise RuntimeError("source must contain at most 200 distinct, completely attributed SHELLs")
    surface_ids = sorted(row["id"] for row in block["surface_mesh"]["elements"])
    if surface_ids != additions["SHELL"] or not set(ids) <= set(surface_ids):
        raise RuntimeError("new SHELL inventory differs from complete FACE membership")
    used = {row["id"] for row in block["surface_mesh"]["nodes"]}
    if not set(additions["GRID"]) <= used:
        raise RuntimeError("MapBlock created orphan or non-boundary GRID")
    return sorted(ids), face_uses


def _surface_projection(base, prepared, block):
    from ansa import calc
    clean = prepared["clean"]
    faces = {ident: features._entity(base, clean["deck"], "FACE", ident) for ident in clean["face_ids"]}
    result = structured_boundary.measure_nodes(base, calc, clean["deck"], block["surface_mesh"],
                block["face_membership"], faces, clean["max_chord_error"])
    if result["status"] != "pass":
        error = RuntimeError("complete native surface-node projection is not within the declared CAD approximation budget")
        error.diagnostics = {"surface_projection": result}
        raise error
    return result


def _face_tessellation_display(before, after, membership, surface_points, budget):
    """Keep native measurement changes distinct from exact CAD definitions.

    Unchanged complete CAD export and all native node/FACE projections are
    required by the caller before this comparison cleanup, following the
    existing cons_seed treatment of affected FACE area/center displays.
    """
    points = [surface_points[i] for i in {n for e in membership["shells"] for n in e["nodes"]}]
    point_bounds = [min(p[k] for p in points) for k in range(3)] + [max(p[k] for p in points) for k in range(3)]
    left, right = before["geometry"], after["geometry"]
    for geometry in (left, right):
        features._number(geometry["area"], "native FACE area", True)
        center = native._point(geometry["center"], "native FACE center")
        native._point(geometry["normal"], "native FACE normal display")
        bounds = geometry["bounds"]
        if len(bounds) != 6 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds):
            raise RuntimeError("FACE tessellation bounds are incomplete")
        if any(not bounds[k]-budget <= center[k] <= bounds[k+3]+budget for k in range(3)):
            raise RuntimeError("FACE display center is outside its bounded native domain")
    if any(abs(a-b) > budget for a,b in zip(left["bounds"],right["bounds"])) or any(abs(a-b) > budget for a,b in zip(right["bounds"],point_bounds)):
        raise RuntimeError("FACE display bounds exceed the native-point approximation budget")
    result = {"face_id": before["entity"]["id"], "before": copy.deepcopy(left), "after": copy.deepcopy(right),
              "actual_node_bounds": point_bounds, "approximation_budget_mm": budget,
              "meaning": "native tessellation displays; CAD definitions and node projections checked separately",
              "analytic_area_centroid_normal_proven": False}
    for field in ("area", "center", "normal", "bounds"):
        left[field] = right[field] = None
    return result


def _asset_references(scope, kind, ident):
    """The preflight type whitelist makes these all supported material/property uses."""
    uses = []
    for (entity_kind, entity_id), row in scope.items():
        fields = row["fields"]
        keys = (("PID",) if entity_kind in ("SHELL", "SOLID", "VOLUME") else ()) if kind == "PSOLID" else (
            ("MID1", "MID2", "MID3", "MID4") if entity_kind == "PSHELL" else
            ("MID",) if entity_kind == "PSOLID" else ())
        for key in keys:
            value = fields.get(key)
            if value in (None, "", 0):
                continue
            if native._positive_int(value, entity_kind + " " + key) == ident:
                uses.append({"type": entity_kind, "id": entity_id, "field": key})
        if kind == "PSOLID" and entity_kind == "FACE" and row.get("assignments", {}).get("__prop__") == ident:
            uses.append({"type": entity_kind, "id": entity_id, "field": "__prop__"})
    return uses


def _bind_and_clean(state, base, prepared, interim, additions, result):
    deck, target = prepared["clean"]["deck"], prepared["clean"]["property_id"]
    scope = native._scope_map(interim)
    auto_pids, auto_mids = set(additions["PSOLID"]), set(additions["MAT1"])
    used_pids = {native._positive_int(scope[("VOLUME", ident)]["fields"].get("PID"), "new VOLUME PID")
                 for ident in additions["VOLUME"]}
    if used_pids - auto_pids - {target} or auto_pids - used_pids:
        raise RuntimeError("automatic PSOLID provenance cannot be proved")
    mids = {native._positive_int(scope[("PSOLID", ident)]["fields"].get("MID"), "automatic PSOLID MID")
            for ident in auto_pids}
    if auto_mids - mids or any(("MAT1", mid) not in scope for mid in mids):
        raise RuntimeError("automatic MAT1 provenance cannot be proved")
    result["native_asset_cleanup"] = []
    for ident in additions["VOLUME"]:
        guards._same_context(state, deck, prepared["context"])
        volume = features._entity(base, deck, "VOLUME", ident)
        rc = base.SetEntityCardValues(deck, volume, {"PID": target})
        if type(rc) is not int or rc != 0 or base.GetEntityCardValues(deck, volume, ["PID"]).get("PID") != target:
            raise RuntimeError("new VOLUME property binding is not independently confirmed")
    for kind, ids in (("PSOLID", auto_pids), ("MAT1", auto_mids)):
        current = _snapshot(state, base, deck, prepared["context"])
        _, current_scope, _ = _inventory(prepared["before"], current)
        for ident in sorted(ids):
            if (kind, ident) not in current_scope or _asset_references(current_scope, kind, ident):
                raise RuntimeError("new native asset is missing or cannot be proved unused: " + str((kind, ident)))
        for ident in sorted(ids):
            guards._same_context(state, deck, prepared["context"])
            entity = features._entity(base, deck, kind, ident)
            rc = base.DeleteEntity(entity, force=False, compress=False)
            absent = base.GetEntity(deck, kind, ident) is None
            result["native_asset_cleanup"].append({"type": kind, "id": ident, "return_code": rc, "absent": absent})
            if type(rc) is not int or rc != 0 or not absent:
                raise RuntimeError("automatic native asset cleanup is not confirmed")


def _part_surface_display(base, prepared, row, surface):
    """Validate the native Part's FE area and roundoff-only bounding display.

    In the verified 2026.1 contract Part Area uses diagonal 1-3 triangles,
    whereas CalcShellArea uses a different warped-quad measure. Neither is
    used here as an analytic CAD coverage certificate.
    """
    deck = prepared["clean"]["deck"]
    fields = row["fields"]
    old = native._scope_map(prepared["before"])[("ANSAPART", prepared["part_id"])]["fields"]
    box = base.BoundBox(prepared["part"])
    if type(box) not in (list, tuple) or len(box) != 6:
        raise RuntimeError("native Part BoundBox is unavailable")
    box = [features._number(v, "native Part BoundBox") for v in box]
    sizes = [box[i+3] - box[i] for i in range(3)]
    if min(sizes) <= 0:
        raise RuntimeError("native Part BoundBox has nonpositive dimensions")
    diagonal = math.hypot(*sizes)
    epsilon = 128 * math.ulp(max(1., diagonal, *(abs(v) for v in box)))
    expected = {**dict(zip(("Min x", "Min y", "Min z", "Max x", "Max y", "Max z"), box)),
                **dict(zip(("Size x", "Size y", "Size z"), sizes)),
                "Size min": min(sizes), "Diagonal": diagonal}
    for field, value in expected.items():
        actual = features._number(fields.get(field), "Part " + field)
        previous = features._number(old.get(field), "original Part " + field)
        if not math.isclose(actual, value, rel_tol=0., abs_tol=epsilon):
            raise RuntimeError("Part bounding display differs from native BoundBox: " + field)
        if not math.isclose(actual, previous, rel_tol=0., abs_tol=epsilon):
            raise RuntimeError("Part bounding display changed beyond roundoff: " + field)
    points = {node["id"]: node["xyz"] for node in surface["nodes"]}
    for point in points.values():
        if any(not box[i]-epsilon <= point[i] <= box[i+3]+epsilon for i in range(3)):
            raise RuntimeError("new surface GRID is outside the protected Part bounds")
    def triangle(a, b, c):
        u, v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
        area = math.hypot(u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])/2
        if not math.isfinite(area) or area <= 0:
            raise RuntimeError("surface contains a degenerate area triangle")
        return area
    areas = []
    for element in surface["elements"]:
        p = [points[ident] for ident in element["nodes"]]
        if len(p) not in (3, 4):
            raise RuntimeError("Part area requires linear triangle/quad surface elements")
        areas.append(triangle(*p[:3]))
        if len(p) == 4:
            areas.append(triangle(p[0], p[2], p[3]))
    area = math.fsum(areas)
    actual = features._number(fields.get("Area"), "Part Area", True)
    if not area or not math.isclose(actual, area, rel_tol=1e-12, abs_tol=1e-9):
        raise RuntimeError("Part Area differs from complete independently triangulated SHELLs")
    return {"allowed_fields": sorted(set(expected) | {"Area"}),
            "native_bound_box": box, "roundoff_limit_mm": epsilon,
            "part_area": actual, "FE_triangle_area": area,
            "area_origin": "complete native SHELL connectivity, quad diagonal 1-3",
            "CAD_area_equivalence_proven": False}


def _cons_perimeter(base, prepared, ident, fields, surface_points):
    """Read already meshed native perimeters only inside the backed-up write."""
    deck = prepared["clean"]["deck"]
    entity = features._entity(base, deck, "CONS", ident)
    edge = seeds._boundary(base, deck, entity, include_points=True)
    if edge["status"] != "supported" or not set(edge["face_ids"]) <= set(prepared["clean"]["face_ids"]):
        raise RuntimeError("CONS perimeter membership or actual GRID count is incomplete")
    if edge["point_count"] != seeds._integer(fields.get("Num. of Nodes"), "CONS Num. of Nodes") + 2:
        raise RuntimeError("CONS internal count differs from independently captured GRID points")
    for point in edge["points"]:
        if surface_points.get(point["id"]) != point["xyz"]:
            raise RuntimeError("CONS perimeter GRID differs from the complete native surface mesh")
    length = features._number(fields.get("Elem.Length"), "CONS Elem.Length", True)
    angle = features._number(fields.get("Distort.Angle"), "CONS Distort.Angle")
    target = prepared["clean"]["target_length"]
    if (length > target and not math.isclose(length, target, rel_tol=1e-12, abs_tol=1e-12)) or angle != 0.:
        raise RuntimeError("CONS local controls do not match the declared absolute-length perimeter policy")
    # Follow cons_seed's existing classification of native tessellation
    # vectors as derived displays. Preserve their raw values, independently
    # verify real perimeter points by native CAD projection, and keep chord
    # comparisons diagnostic: not every display uses the FE end chord.
    vectors = []
    for endpoint in edge["endpoint_grid_ids"]:
        origin = surface_points[endpoint]
        candidates = sorted((math.dist(origin, p["xyz"]), p["id"]) for p in edge["points"] if p["id"] != endpoint)
        if not candidates or candidates[0][0] <= 0 or (len(candidates) > 1 and math.isclose(candidates[0][0], candidates[1][0], rel_tol=1e-12, abs_tol=1e-12)):
            raise RuntimeError("CONS endpoint chord is degenerate or ambiguous")
        neighbor = surface_points[candidates[0][1]]
        vectors.append([(neighbor[i]-origin[i])/candidates[0][0] for i in range(3)])
    if len(vectors) != 2:
        raise RuntimeError("CONS endpoint chord vectors are incomplete")
    displayed = []
    for name in ("Start Vector", "End Vector"):
        value = fields.get(name)
        try:
            vector = [float(v.strip()) for v in value.split(",")] if type(value) is str else []
        except ValueError as exc:
            raise RuntimeError("CONS endpoint vector display is invalid") from exc
        if len(vector) != 3 or any(not math.isfinite(v) for v in vector) or abs(math.hypot(*vector)-1.) > 2e-6:
            raise RuntimeError("CONS endpoint vector display is not a finite unit vector")
        displayed.append(vector)
    error = min(max(abs(a-b) for actual, expected in zip(displayed, order) for a,b in zip(actual, expected))
                for order in (vectors, vectors[::-1]))
    from ansa import calc
    from .geometry_fidelity import _projection_points
    projection_limit = prepared["clean"]["max_chord_error"]
    distances = []
    for point in edge["points"]:
        _extension, closest, _mode = _projection_points(calc.ProjectPointToCons(point["xyz"], entity))
        distance = math.dist(point["xyz"], closest)
        if not math.isfinite(distance) or distance > projection_limit:
            error = RuntimeError("native perimeter GRID projection exceeds the declared geometric approximation budget")
            error.diagnostics = {"cons_id": ident, "grid_id": point["id"], "distance": distance,
                                 "tolerance_mm": projection_limit, "point": point["xyz"], "closest": list(closest)}
            raise error
        distances.append(distance)
    bounds = [min(p["xyz"][i] for p in edge["points"]) for i in range(3)] + [max(p["xyz"][i] for p in edge["points"]) for i in range(3)]
    return {"cons_id": ident, "point_count": edge["point_count"],
            "grid_ids": [p["id"] for p in edge["points"]], "target_length": length, "distortion_angle": angle,
            "requested_global_target_length": target, "native_local_refinement": length < target,
            "point_bounds": bounds, "endpoint_chord_vectors": vectors, "maximum_vector_display_error": error,
            "vector_display_tolerance": 1e-6, "vector_display_matches_FE_chords": error <= 1e-6,
            "analytic_CAD_tangent_proven": False,
            "native_point_projection_count": len(distances), "maximum_native_projection_distance": max(distances),
            "native_point_projection_tolerance_mm": projection_limit}


def _cons_bounds_display(left, right, point_bounds, approximation_budget):
    for value in (left, right, point_bounds):
        if type(value) not in (list, tuple) or len(value) != 6 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
            raise RuntimeError("CONS display bounds are incomplete")
    # The original CAD export is unchanged. Its unmeshed tessellation can be
    # coarser; current bounds must independently match actual perimeter points.
    if (any(abs(x-y) > approximation_budget for x,y in zip(left, right)) or
            any(abs(x-y) > seeds._BOUNDING_DISPLAY_TOLERANCE for x,y in zip(right, point_bounds))):
        raise RuntimeError("CONS bounds differ from the declared approximation budget or actual perimeter points")
    return {"bounds_before": list(left), "bounds_after": list(right), "point_bounds": list(point_bounds),
            "maximum_display_change_mm": max(abs(x-y) for x,y in zip(left,right)),
            "display_approximation_budget_mm": approximation_budget,
            "actual_point_bounds_tolerance_mm": seeds._BOUNDING_DISPLAY_TOLERANCE}


def _verify_scope(base, prepared, after, block, face_uses, additions):
    old, current, final = _inventory(prepared["before"], after)
    if final != {kind: additions[kind] if kind in {"GRID", "SHELL", "VOLUME"} else [] for kind in _NEW_TYPES}:
        raise RuntimeError("final additions differ or automatic properties/materials remain")
    if prepared["before"]["cad_sha256"] != after["cad_sha256"]:
        raise RuntimeError("MapBlock changed the complete original CAD export")
    if any(row != next((r for r in after["grids"] if r["id"] == row["id"]), None)
           for row in prepared["before"]["grids"]):
        raise RuntimeError("MapBlock changed an original GRID definition")
    deck, part_id = prepared["clean"]["deck"], prepared["part_id"]
    part_row = current[("ANSAPART", part_id)]
    expected = _expected_part_summary(current, prepared["source_pids"], prepared["clean"]["property_id"])
    for key, value in expected.items():
        actual = part_row["fields"].get(key)
        if key in ("Properties Info", "Materials Info"):
            actual = json.loads(actual)
        if actual != value:
            raise RuntimeError("Part derived property/material summary mismatch: " + key)
    stamp = part_row["fields"].get("Last Edit")
    if type(stamp) is not str or not 0 < len(stamp) <= 128:
        raise RuntimeError("Part derived edit stamp is unavailable")
    aggregate = _part_aggregate(base, deck, prepared["part"], part_row)
    display = _part_surface_display(base, prepared, part_row, block["surface_mesh"])
    projection = _surface_projection(base, prepared, block)
    shell_counts = Counter(row["property_id"] for row in block["surface_mesh"]["elements"])
    surface_points = {n["id"]: n["xyz"] for n in block["surface_mesh"]["nodes"]}
    face_membership = {r["face_id"]: r for r in block["face_membership"]}
    changes, perimeters = [], []
    for key, row in old.items():
        a, b = copy.deepcopy(row), copy.deepcopy(current[key])
        kind, ident = key
        allowed = set()
        if kind == "ANSAPART":
            allowed = _PART_SUMMARY_FIELDS | set(part_aggregates.MASS_FIELDS) | set(display["allowed_fields"])
        elif kind == "FACE":
            if native._volume_count(a["fields"].get("Num. Volumes")) != 0 or native._volume_count(b["fields"].get("Num. Volumes")) != face_uses[ident]:
                raise RuntimeError("FACE native volume count disagrees with complete block boundaries")
            allowed = {"Num. Volumes"}
            changes.append({"type": kind, "id": ident, "tessellation_display": _face_tessellation_display(
                a, b, face_membership[ident], surface_points, prepared["clean"]["max_chord_error"])})
        elif kind == "PSHELL" and ident in prepared["source_pids"]:
            if native._volume_count(a["fields"].get("Num.Elem")) != 0 or native._volume_count(b["fields"].get("Num.Elem")) != shell_counts[ident]:
                raise RuntimeError("PSHELL derived count disagrees with the full new SHELL inventory")
            allowed = {"Num.Elem"}
        elif kind == "CONS":
            perimeter = _cons_perimeter(base, prepared, ident, b["fields"], surface_points)
            perimeters.append(perimeter)
            allowed = {"Num. of Nodes", "Elem.Length", "Distort.Angle", "Start Vector", "End Vector"}
            left, right = a["geometry"]["bounds"], b["geometry"]["bounds"]
            try:
                bounds = _cons_bounds_display(left, right, perimeter["point_bounds"], prepared["clean"]["max_chord_error"])
            except RuntimeError as error:
                error.diagnostics = {"entity": list(key), "before": row, "after": current[key], "perimeter": perimeter}
                raise
            if left != right:
                changes.append({"type": kind, "id": ident, **bounds})
            a["geometry"]["bounds"] = b["geometry"]["bounds"] = None
        for field in allowed:
            if a["fields"].get(field) != b["fields"].get(field):
                changes.append({"type": kind, "id": ident, "field": field,
                                "before": a["fields"].get(field), "after": b["fields"].get(field)})
            a["fields"].pop(field, None)
            b["fields"].pop(field, None)
        if a != b:
            error = RuntimeError("MapBlock changed a protected original definition or unsupported derived field: " + str(key))
            error.diagnostics = {"entity": list(key), "before": row, "after": current[key],
                                 "comparison_before": a, "comparison_after": b}
            raise error
    for ident in additions["VOLUME"]:
        if current[("VOLUME", ident)]["fields"].get("PID") != prepared["clean"]["property_id"]:
            raise RuntimeError("final VOLUME property binding changed")
    for ident in additions["SHELL"]:
        if current[("SHELL", ident)].get("part_id") != part_id:
            raise RuntimeError("new SHELL has unexpected Part ownership")
    return {"derived_changes": changes, "native_part_aggregate": aggregate, "native_part_surface_display": display,
            "native_perimeter_controls": perimeters, "native_surface_projection": projection}


def apply_prepared(state, base, mesh, constants, params, prepared):
    clean = normalize(params, True)
    result = {"ok": False, "scope_verified": False, "api_called": False, "settings_api_called": False,
              "possible_effects": False, "recovery_required": False, "backup_attempted": False,
              "plan_sha256": prepared["plan_sha256"], "annular_tolerance_mm": _TOLERANCE,
              "new_volume_ids": [], "created_shell_ids": [], "new_grid_ids": [], "solid_mesh_generated": False,
              "continuous_CAD_coverage_proven": False, "original_operation_must_be_queried": True}
    try:
        if clean["expected_plan_sha256"] != prepared["plan_sha256"] or {k: clean[k] for k in _PARAMS} != prepared["clean"]:
            raise ValueError("annular prepare plan or parameters changed")
        backup_args = native._backup_preflight(base, clean)
        result["backup"] = {"path": str(backup_args[0]), "verified": False}
        result["backup_source"] = {"path": backup_args[1], "fingerprint": backup_args[2]}
        result.update(backup_attempted=True, possible_effects=True)
        result["backup"] = native._verify_backup(base, constants, *backup_args)
        refreshed = preflight(state, base, mesh, constants, prepared["clean"])
        if refreshed["plan_sha256"] != prepared["plan_sha256"]:
            raise RuntimeError("model/settings changed during backup; MapBlock was not called")
        prepared = refreshed
        returned = _map_once(state, base, mesh, prepared, result)
        interim = _snapshot(state, base, clean["deck"], prepared["context"])
        _, _, additions = _inventory(prepared["before"], interim)
        if type(returned) not in (list, tuple) or len(returned) != 4:
            raise RuntimeError("MapBlock must return exactly four VOLUMEs")
        returned_ids = [native._positive_int(e._id, "returned VOLUME ID") for e in returned if e.ansa_type(clean["deck"]) == "VOLUME"]
        if sorted(returned_ids) != additions["VOLUME"] or len(set(returned_ids)) != 4:
            raise RuntimeError("MapBlock return differs from the independent full VOLUME inventory")
        block = map_block_inspection.capture(state, base, mesh, constants, clean["deck"], additions["VOLUME"])
        source_ids, face_uses = _source_readback(block, prepared, additions)
        source = native.capture_source(base, constants, clean["deck"], source_ids)
        options = {key: clean[key] for key in ("center", "axis", "inner_radius", "outer_radius", "max_chord_error")}
        template = annular_source.audit(source, {**options, "tolerance": _TOLERANCE})
        result["annular_template"] = template
        if template["status"] != "verified_mesh_template":
            raise RuntimeError("annular source audit failed: " + template.get("reason", "unsupported source"))
        _bind_and_clean(state, base, prepared, interim, additions, result)
        after = _snapshot(state, base, clean["deck"], prepared["context"])
        verification = _verify_scope(base, prepared, after, block, face_uses, additions)
        from .mapped_surface import _check_new_grids
        _check_new_grids(base, clean["deck"], additions["GRID"])
        # Rebind the source after property cleanup and all independent readbacks.
        final_source = native.capture_source(base, constants, clean["deck"], source_ids)
        if final_source != source:
            raise RuntimeError("source SHELLs or GRID coordinates changed during final verification")
        if _snapshot(state, base, clean["deck"], prepared["context"]) != after:
            raise RuntimeError("full model changed during final backed-up perimeter/source readback")
        guards._same_context(state, clean["deck"], prepared["context"])
        source_evidence = structured_evidence.store_report(state, clean["deck"], "mesh_native_operation_result", {
            "source_ids": source_ids, "shell_ids": source_ids, "source_sha256": source["snapshot_sha256"],
            "source_snapshot": source, "annular_template": template, "plan_sha256": prepared["plan_sha256"],
            "scope_sha256": features._hash(after), "source_face_ids": clean["source_face_ids"]})
        result.update(ok=True, status="verified_source_preparation", scope_verified=True,
                      possible_effects=False, recovery_required=False, source_ids=source_ids, shell_ids=source_ids,
                      source_sha256=source["snapshot_sha256"], source_evidence=source_evidence,
                      new_volume_ids=additions["VOLUME"], created_shell_ids=additions["SHELL"], new_grid_ids=additions["GRID"],
                      after_scope_sha256=features._hash(after), verification=verification,
                      property_id=clean["property_id"], source_face_ids=clean["source_face_ids"],
                      native_mapping_status="native_ready", project_quality_acceptance="not_evaluated",
                      sweep_parameters={"shell_ids": source_ids, "direction": clean["axis"], "distance": clean["distance"],
                                        "property_id": clean["property_id"], "expected_source_sha256": source["snapshot_sha256"],
                                        "freeze_source": True})
    except Exception as exc:
        result.update(status="recovery_required" if result["api_called"] else "failed",
                      error=native._error_text(exc))
        if type(getattr(exc, "diagnostics", None)) is dict:
            result["diagnostics"] = copy.deepcopy(exc.diagnostics)
        # SaveAs or the following preflight can fail after altering the database
        # path/file/model.  Absence of a MapBlock call is not a no-effect proof.
        result["possible_effects"] = result["backup_attempted"] or result["api_called"] or (
            result["settings_api_called"] and not result.get("meshing_context", {}).get("restored"))
        result["recovery_required"] = result["possible_effects"]
        if result["recovery_required"]:
            result["status"] = "recovery_required"
        if result["backup_attempted"] or result["api_called"]:
            result.update(inventory_observation_complete=False, new_volume_ids=None,
                          created_shell_ids=None, new_grid_ids=None,
                          remaining_new_psolid_ids=None, remaining_new_mat1_ids=None)
            try:
                after = _snapshot(state, base, clean["deck"], prepared["context"])
                old, current = native._scope_map(prepared["before"]), native._scope_map(after)
                added = current.keys() - old.keys()
                additions = {kind: sorted(ident for kind_now, ident in added if kind_now == kind)
                             for kind in _NEW_TYPES}
                result.update(new_volume_ids=additions["VOLUME"], created_shell_ids=additions["SHELL"],
                              new_grid_ids=additions["GRID"], after_scope_sha256=features._hash(after),
                              remaining_new_psolid_ids=additions["PSOLID"], remaining_new_mat1_ids=additions["MAT1"],
                              inventory_observation_complete=True,
                              removed_original_entities=[{"type": kind, "id": ident} for kind, ident in sorted(old.keys() - current.keys())],
                              unexpected_new_entities=[{"type": kind, "id": ident} for kind, ident in sorted(added) if kind not in _NEW_TYPES])
                result["current_database_path"] = guards._database_name(base)
                result["original_scope_unchanged"] = after == prepared["before"]
                source_file = Path(result["backup_source"]["path"])
                result["original_database_file_unchanged"] = source_file.is_file() and (
                    native.model_files._fingerprint(source_file) == result["backup_source"]["fingerprint"])
            except Exception as read_error:
                result["observation_error"] = native._error_text(read_error)
    return result
