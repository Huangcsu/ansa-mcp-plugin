"""Native replacement of bounded existing surface meshes, preserving all perimeters."""

import copy
import math
import time

from . import connections, cons_seed as seeds, entity_catalog as catalog, geometry_features as features
from . import inspection, mapped_surface, mesh_scope, meshing, meshing_context, perimeter_edit
from . import structured_boundary, structured_native as native, volume_meshing as guards
from .mapped_surface_topology import four_sides, verify_lattice
from .surface_remesh_audit import audit_surface

_MAX_SHELLS = 2000
_TOLERANCE = 1e-7
_OPTIONS = ("algorithm", "element_type", "element_order", "boundary_policy", "target_length",
            "expected_scope_sha256", "expected_seed_snapshot_sha256", "backup_path")
_FACE_MESH_FIELDS = ("Meshed With", "Type", "Num.Trias", "Num.Quads")


def normalize(params):
    inspection._params(params, ("deck", "faces", "remesh"))
    deck = native._positive_int(params["deck"], "deck")
    rows = params["faces"]
    if type(rows) is not list or not 1 <= len(rows) <= 4:
        raise ValueError("remesh requires 1..4 complete single-FACE Macro conditions")
    for row in rows:
        inspection._params(row, ("id", "expected_fields", "expected_macro_id", "expected_shell_ids"))
        native._positive_int(row["expected_macro_id"], "expected_macro_id")
        meshing._ids(row["expected_shell_ids"], "expected_shell_ids", _MAX_SHELLS)
        if type(row["expected_fields"]) is not dict or not row["expected_fields"]:
            raise ValueError("complete FACE expected_fields are required")
    ids = sorted(native._id_list([r["id"] for r in rows], "FACE IDs", 4))
    options = dict(inspection._params(params["remesh"], _OPTIONS))
    if options["algorithm"] not in ("FREE", "mapped"):
        raise ValueError("remesh algorithm must be FREE or mapped")
    if options["element_type"] not in ("quad", "mixed", "tria"):
        raise ValueError("remesh requires an explicit quad/mixed/tria element_type")
    if options["algorithm"] == "mapped" and options["element_type"] != "quad":
        raise ValueError("mapped remesh requires quad")
    if options["element_order"] != "1st Order" or options["boundary_policy"] != "preserve":
        raise ValueError("remesh supports only first order and preserved native perimeters")
    options["target_length"] = native._finite(options["target_length"], "target_length")
    if options["target_length"] <= 0:
        raise ValueError("target_length must be positive")
    for key in ("expected_scope_sha256", "expected_seed_snapshot_sha256"):
        native._validate_digest(options[key], key)
    if type(options["backup_path"]) is not str or not options["backup_path"]:
        raise ValueError("backup_path must be a new absolute .ansa path")
    return {"deck": deck, "faces": sorted(rows, key=lambda r: r["id"]),
            "face_ids": ids, "remesh": options}


def _selection(clean):
    return {"deck": clean["deck"], "entity_type": "FACE", "entity_ids": clean["face_ids"]}


def _shell_definition(row):
    """Replacing an entity must not silently discard its engineering definition."""
    fields, labels = row["fields"], row["card_fields"]
    kind = fields.get("type")
    if kind not in ("CQUAD4", "CTRIA3"):
        raise ValueError("remesh supports only first-order CQUAD4/CTRIA3")
    size = 4 if kind == "CQUAD4" else 3
    defaults = {"mat.orient": "MCID", "MCID": None, "ZOFFS": None, "TFLAG": " ",
                "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "FROZEN": "NO",
                "FREEZE ON DEPENETRATION": "NO", **{f"T{i}": 0 for i in range(1, size+1)}}
    if not set(defaults) <= set(labels) or not set(defaults) <= fields.keys():
        raise ValueError("SHELL engineering definition is incomplete")
    for key, expected in defaults.items():
        if not catalog._strict_equal(fields[key], expected):
            raise ValueError("SHELL has nondefault or frozen engineering definition: " + key)
    connectivity = {"type", "EID", "PID", *(f"G{i}" for i in range(1, size+1))}
    for key, value in fields.items():
        if key not in defaults and key not in connectivity and value not in (None, "", [], 0):
            raise ValueError("SHELL has nondefault metadata or membership: " + key)


def _references(value):
    if type(value) is dict:
        if set(value) == {"deck", "type", "id"}:
            return {(value["type"], value["id"])}
        return set().union(*(_references(v) for v in value.values())) if value else set()
    if type(value) is list:
        return set().union(*(_references(v) for v in value)) if value else set()
    return set()


def _reference_index(base, deck, snapshot, replaceable):
    """Read actual entity references, including SET tables, rather than guessing numeric IDs."""
    result, total = {}, 0
    # Do not reacquire mixed native connection families with GetEntity before
    # card_fields: that handle path is known to terminate ANSA 26.10.000.
    handles = {(e.ansa_type(deck), e._id): e for e in seeds._collect(base, deck, None, "__ALL_ENTITIES__")}
    if set(handles) != set(native._scope_map(snapshot)):
        raise RuntimeError("entity inventory changed before engineering reference readback")
    for row in snapshot["entities"]:
        key = (row["entity"]["type"], row["entity"]["id"])
        if key in replaceable:
            continue
        entity = handles[key]
        labels = list(entity.card_fields(deck))
        values = entity.get_entity_values(deck, labels)
        if type(values) is not dict or set(values) != set(labels):
            raise ValueError("external engineering reference fields are incomplete: " + str(key))
        refs = _references(catalog._json_value(values, base, deck))
        if key[0] == "SET":
            for kind in ("SHELL", "GRID"):
                members = mesh_scope._entities(base, deck, base.CollectEntities(
                    deck, entity, kind, True, filter_visible=False, hidden_entities=True),
                    kind, "SET " + kind + " members")
                refs.update((kind, i) for i in members)
        if refs & replaceable:
            raise ValueError("external entity references replaced SHELL/interior GRID: " + str(key))
        total += len(refs)
        if total > 200000:
            raise ValueError("engineering reference protection exceeds 200000 memberships")
        result[key] = sorted(refs)
    return result


def _membership_gate(base, deck):
    """Reject unadapted non-card membership before any protected card scan/write."""
    kinds = connections._available_types(base, deck) | connections._POINT_TYPE_SET | {connections._CONNECTOR_TYPE}
    raw = base.CollectEntities(deck, None, "__ALL_ENTITIES__", False,
                               filter_visible=False, hidden_entities=True)
    if type(raw) not in (list, tuple) or len(raw) > 10000:
        raise ValueError("remesh membership protection requires at most 10000 entities")
    for entity in raw:
        # Read only native type/ID, never card_fields or a GetEntity handle.
        kind = entity.ansa_type(deck)
        if type(kind) is not str or not kind:
            raise RuntimeError("native entity type is unavailable for membership protection")
        if kind in kinds:
            raise ValueError("remesh currently rejects models containing connections: FE representation membership is not adapted")
        if kind == "INCLUDE":
            raise ValueError("remesh currently rejects models containing INCLUDE: replacement ownership is not adapted")


def _preflight(state, base, mesh, constants, clean):
    deck, options = clean["deck"], clean["remesh"]
    native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    _membership_gate(base, deck)
    scope = mesh_scope.capture(state, base, mesh, constants, _selection(clean))
    if scope["scope_sha256"] != options["expected_scope_sha256"]:
        raise ValueError("mesh scope is stale; obtain fresh mesh_scope_read for the selected FACE IDs")
    primary = [r for r in scope["macros"] if r["primary"]]
    if (len(primary) != len(clean["face_ids"]) or
            any(r["face_ids"] != [r["macro_id"]] or not r["shell_ids"] for r in primary)):
        raise ValueError("remesh requires complete single-FACE Macros with existing SHELL mesh")
    if any(ref["type"] != "CONS" for row in primary for ref in row["perimeters"]):
        raise ValueError("remesh requires CAD CONS perimeters; FE PERIMETER rework is unsupported")
    target_shells = {i for r in primary for i in r["shell_ids"]}
    if len(target_shells) > _MAX_SHELLS:
        raise ValueError("existing target mesh exceeds 2000 SHELL")
    targets = set(clean["face_ids"])
    if any(r["volume_ids"] for r in scope["faces"] if r["entity"]["id"] in targets):
        raise ValueError("remeshing VOLUME-related FACE requires explicit volume rework, currently unsupported")
    for row in scope["faces"]:
        if row["entity"]["id"] in targets and row["effective_macro_protection"]["value"] is not False:
            raise ValueError("target Macro is frozen or its protection is unknown")
    face_ids = sorted(r["entity"]["id"] for r in scope["faces"])
    cons_ids = scope["freeze_effects"]["cons_ids"]
    witness = seeds.capture(state, base, deck, face_ids, cons_ids)
    if witness["seed_snapshot_sha256"] != options["expected_seed_snapshot_sha256"]:
        raise ValueError("native seeds changed; obtain mesh_seed_read with all scope FACE and freeze_effects CONS IDs")
    cons = {r["entity"]["id"]: r for r in scope["cons"]}
    for row in witness["boundaries"]:
        if (row["status"] != "supported" or cons[row["cons_id"]]["effective_macro_protection"]["value"] is not False
                or any(row["native_fields"].get(k) != v for k, v in
                       (("Frozen", "NO"), ("Collapsed", "NO"), ("Biasing", "LINEAR")))):
            raise ValueError("remesh requires unfrozen first-order open LINEAR CONS and unprotected adjacency")
    before = seeds._guard_snapshot(state, base, deck)
    old = native._scope_map(before)
    topology = {r["face_id"]: r["cons_ids"] for r in witness["geometry_witness"]["face_boundaries"]}
    sources, reservation = [], 0
    for condition in clean["faces"]:
        ident = condition["id"]
        catalog._validate_entity_values(base, deck, "FACE", ident, condition["expected_fields"])
        face, snap, ids = meshing._face_snapshot(base, mesh, deck, ident)
        if set(_FACE_MESH_FIELDS) & set(snap["fields"]):
            raise NotImplementedError("remesh FACE adapter does not support native mesh display fields")
        if (snap["macro_face_ids"] != [ident] or snap["macro"]["id"] != condition["expected_macro_id"]
                or ids != condition["expected_shell_ids"] or not ids
                or not catalog._strict_equal(snap["fields"], condition["expected_fields"])):
            raise ValueError("complete FACE fields, Macro or SHELL conditions changed")
        part = base.GetEntityPart(face)
        if part is None:
            raise ValueError("target FACE requires explicit Part ownership")
        part_id = catalog._entity_reference(base, deck, part, "ANSAPART")["id"]
        pid = meshing._field_id(snap["fields"]["PID"], "FACE PID")
        for shell_id in ids:
            row = old[("SHELL", shell_id)]
            _shell_definition(row)
            if row["part_id"] != part_id or row["fields"]["PID"] != pid:
                raise ValueError("target SHELL Part/property must match its FACE")
        sides = four_sides(topology[ident], witness["boundaries"]) if options["algorithm"] == "mapped" else None
        if sides:
            estimate = sides[0]["segments"] * sides[1]["segments"]
        else:
            # Budget reservation only; no promise about the native generator's count.
            ratio = old[("FACE", ident)]["geometry"]["area"] / options["target_length"]
            estimate = ratio / options["target_length"] * 8
            if not math.isfinite(estimate):
                raise ValueError("target length exceeds the remesh output budget")
            estimate = max(len(ids), math.ceil(estimate), sum(r["segments"] for r in witness["boundaries"]))
        reservation += estimate
        sources.append({"id": ident, "entity": face, "snapshot": snap, "shell_ids": ids,
                        "part_id": part_id, "property_id": pid, "cons_ids": topology[ident], "sides": sides,
                        "normal": old[("FACE", ident)]["geometry"]["normal"]})
    if reservation > _MAX_SHELLS:
        raise ValueError("requested remesh exceeds the 2000-SHELL reservation budget")
    seeds._point_budget(base, deck, seeds._collect(base, deck, None, "CONS"),
                        len(before["entities"])+reservation*5, len(before["grids"])+reservation*4)
    inspection._require_api(mesh, "RemeshShells" if options["algorithm"] == "FREE" else "Create4SidedMesh", "native remesh")
    backup = native._backup_preflight(base, options)
    guards._same_context(state, deck, context)
    return {"context": context, "scope": scope, "witness": witness, "before": before,
            "source_faces": sources, "old_shell_ids": sorted(target_shells), "backup": backup}


def _paths(boundaries):
    result = {}
    for row in boundaries:
        if row["status"] != "supported":
            raise ValueError("native perimeter points do not match their cards/endpoints")
        start = row["hot_points"][0]["id"]
        perimeter_edit._fractions(row, start)
        origin, direction, _ = perimeter_edit._direction(row, start)
        ordered = sorted(row["points"], key=lambda p: sum((b-a)*d for a,b,d in zip(origin, p["xyz"], direction)))
        result[row["cons_id"]] = [p["id"] for p in ordered]
    return result


def _mesh_data(base, mesh, constants, calc, deck, prepared, *, requested_type=None):
    records, faces = structured_boundary.capture_membership(base, mesh, constants, deck,
                                                         [r["id"] for r in prepared["source_faces"]])
    elements = [s for r in records for s in r["shells"]]
    ids = [r["id"] for r in elements]
    if not ids or len(ids) != len(set(ids)) or len(ids) > _MAX_SHELLS or not all(r["membership_complete"] for r in records):
        raise RuntimeError("remesh target SHELL membership is empty, duplicated, ambiguous or over budget")
    allowed = {"quad": {"CQUAD4"}, "tria": {"CTRIA3"}, "mixed": {"CQUAD4", "CTRIA3"}}
    if requested_type and any(e["type"] not in allowed[requested_type] for e in elements):
        raise RuntimeError("native output element types disagree with explicit remesh selection")
    node_ids = sorted({i for e in elements for i in e["nodes"]})
    nodes = native._read_grids(base, deck, node_ids)
    coordinates = {r["id"]: r["xyz"] for r in nodes}
    projections = structured_boundary.measure_nodes(base, calc, deck, {"nodes": nodes}, records, faces, _TOLERANCE)
    if projections["status"] != "pass":
        raise RuntimeError("complete node-to-source-FACE projection was not verified")
    summaries = []
    for source, record in zip(prepared["source_faces"], records):
        if record["face_id"] != source["id"]:
            raise RuntimeError("native FACE ordering changed")
        own = record["shells"]
        if any(e["property_id"] != source["property_id"] for e in own):
            raise RuntimeError("result SHELL property differs from source FACE")
        paths = [prepared["paths"][i] for i in source["cons_ids"]]
        point_ids = {i for e in own for i in e["nodes"]}
        audit = audit_surface([{"id": e["id"], "element_type": e["type"], "node_ids": e["nodes"]} for e in own],
                              {i: coordinates[i] for i in point_ids}, paths, source["normal"], tolerance=_TOLERANCE)
        if requested_type and source["sides"]:
            side_paths = []
            boundary_rows = {r["cons_id"]: r for r in prepared["boundaries"]}
            for side in source["sides"]:
                path = prepared["paths"][side["cons_id"]]
                row = boundary_rows[side["cons_id"]]
                side_paths.append(path if row["hot_points"][0]["id"] == side["start"] else list(reversed(path)))
            audit["lattice"] = verify_lattice(own, side_paths)
        summaries.append({"face_id": source["id"], "shell_ids": record["macro_shell_ids"], "topology": audit})
    return {"shell_ids": sorted(ids), "node_ids": node_ids, "elements": elements,
            "nodes": nodes, "point_projections": projections, "faces_after": summaries}


def _materialize(state, base, mesh, calc, constants, clean, prepared, result):
    deck = clean["deck"]
    result["materialization_attempted"] = True
    before = prepared["before"]
    initialized = seeds._guard_snapshot(state, base, deck, materialize=True, budget_base=before)
    old, new = native._scope_map(before), native._scope_map(initialized)
    added = new.keys()-old.keys()
    perimeter_ids = {i for values in initialized["perimeters"].values() for i in values}
    if (old.keys()-new.keys() or any(new[k] != v for k, v in old.items())
            or any(t != "GRID" or i not in perimeter_ids for t, i in added)
            or before["cad_sha256"] != initialized["cad_sha256"]):
        raise RuntimeError("perimeter materialization changed existing model definitions or CAD")
    boundaries = [seeds._boundary(base, deck, features._entity(base, deck, "CONS", r["cons_id"]), include_points=True)
                  for r in prepared["witness"]["boundaries"]]
    prepared = {**prepared, "before": initialized, "boundaries": boundaries, "paths": _paths(boundaries)}
    data = _mesh_data(base, mesh, constants, calc, deck, prepared)
    interior = set(data["node_ids"])-perimeter_ids
    grids = {r["id"]: r for r in initialized["grids"]}
    for ident in sorted(interior):
        perimeter_edit._virtual_definition(grids[ident])
    replaceable = {("SHELL", i) for i in prepared["old_shell_ids"]} | {("GRID", i) for i in interior}
    references = _reference_index(base, deck, initialized, replaceable)
    candidate_nodes = [features._entity(base, deck, "GRID", i) for i in sorted(interior)]
    attachments = base.NodesToElements(candidate_nodes, sources=["entities", "bcs"]) if candidate_nodes else {}
    if type(attachments) is not dict:
        raise RuntimeError("interior node attachment map is unavailable")
    seen = set()
    for node, attached in attachments.items():
        ident = catalog._entity_reference(base, deck, node, "GRID")["id"]
        if ident not in interior or ident in seen or type(attached) not in (list, tuple):
            raise RuntimeError("interior node attachment map is incomplete or ambiguous")
        seen.add(ident)
        if any((r["type"], r["id"]) not in replaceable for r in
               (catalog._entity_reference(base, deck, e) for e in attached)):
            raise ValueError("interior node is referenced by non-target FE or boundary condition")
    if seen != interior:
        raise RuntimeError("interior node attachment map omits candidate nodes")
    result["old_shell_ids"] = prepared["old_shell_ids"]
    result["replaceable_interior_grid_ids"] = sorted(interior)
    return {**prepared, "old_interior_ids": sorted(interior), "reference_index": references}


def _invoke(state, base, mesh, clean, prepared, result):
    guards._same_context(state, clean["deck"], prepared["context"])
    result["api_called"] = True
    if clean["remesh"]["algorithm"] == "FREE":
        shells = [features._entity(base, clean["deck"], "SHELL", i) for i in prepared["old_shell_ids"]]
        returned = mesh.RemeshShells(shells, "FREE")
        refs = mesh_scope._entities(base, clean["deck"], returned, "SHELL", "RemeshShells return")
        result["native_return"] = {"shell_ids": sorted(refs)}
    else:
        returned = mesh.Create4SidedMesh([r["entity"] for r in prepared["source_faces"]],
            only_4sided=True, only_aligned=True, corner_angle=60, smooth_type="standard",
            split_method="none", spacing="off", elem_type="quad", quad_pattern="directional",
            align_hots="off", only_unmeshed=False, protect_selection_bounds=True)
        refs = mesh_scope._entities(base, clean["deck"], getattr(returned, "meshed_ents", None),
                                   "FACE", "Create4SidedMesh meshed_ents")
        result["native_return"] = {"face_ids": sorted(refs)}


def _relations(scope):
    result = perimeter_edit._relations(scope)
    result = copy.deepcopy(result)
    for row in result["macros"]:
        if row["primary"]:
            row.pop("shell_ids")
    return result


def _audit(prepared, after, data, result):
    """Reuse strict existing CAD/outside-FE protection with a declared replacement set."""
    previous = copy.deepcopy(prepared["before"])
    observed = copy.deepcopy(after)
    discard = {("SHELL", i) for i in prepared["old_shell_ids"]} | {("GRID", i) for i in prepared["old_interior_ids"]}
    previous["entities"] = [r for r in previous["entities"] if (r["entity"]["type"], r["entity"]["id"]) not in discard]
    previous["grids"] = [r for r in previous["grids"] if r["id"] not in prepared["old_interior_ids"]]
    source_ids = {r["id"] for r in prepared["source_faces"]}
    # These optional solver-specific display fields are independently checked
    # from complete Macro membership, never treated as engineering definitions.
    for snapshot in (previous, observed):
        for row in snapshot["entities"]:
            if row["entity"]["type"] == "FACE" and row["entity"]["id"] in source_ids:
                for key in _FACE_MESH_FIELDS:
                    if key in row["fields"]:
                        raise NotImplementedError("remesh FACE adapter does not yet support native mesh display fields: " + key)
    perimeters = {i for ids in previous["perimeters"].values() for i in ids}
    interior = set(data["node_ids"])-perimeters
    changes = mapped_surface._audit({**prepared, "before": previous}, observed, data["shell_ids"], sorted(interior), observed=result)
    old_grids = {r["id"]: r for r in previous["grids"]}
    new_grids = {r["id"]: r for r in after["grids"]}
    if any(new_grids.get(i) != row for i, row in old_grids.items()):
        raise RuntimeError("remesh changed a boundary or unrelated GRID card/coordinate")
    if set(new_grids)-old_grids.keys() != interior:
        raise RuntimeError("result interior GRID do not exactly account for the allowed replacement")
    # Newly generated or reused interior identities must still be disposable.
    for ident in interior:
        perimeter_edit._virtual_definition(new_grids[ident])
    return changes, interior


def _verify(state, base, mesh, calc, constants, clean, prepared, result):
    deck = clean["deck"]
    guards._same_context(state, deck, prepared["context"])
    data = _mesh_data(base, mesh, constants, calc, deck, prepared, requested_type=clean["remesh"]["element_type"])
    result["result_shell_ids"] = data["shell_ids"]
    after = seeds._guard_snapshot(state, base, deck, materialize=True, budget_base=prepared["before"])
    rows = native._scope_map(after)
    for source in prepared["source_faces"]:
        face, snapshot, ids = meshing._face_snapshot(base, mesh, deck, source["id"])
        if (snapshot["macro_face_ids"] != [source["id"]] or snapshot["macro"] != source["snapshot"]["macro"]
                or not catalog._strict_equal(snapshot["fields"], source["snapshot"]["fields"])):
            raise RuntimeError("source FACE definition or Macro identity changed")
        for ident in ids:
            row = rows[("SHELL", ident)]
            _shell_definition(row)
            if row["part_id"] != source["part_id"] or row["fields"]["PID"] != source["property_id"]:
                raise RuntimeError("result SHELL has incorrect Part/property ownership")
    boundaries = [seeds._boundary(base, deck, features._entity(base, deck, "CONS", r["cons_id"]), include_points=True)
                  for r in prepared["boundaries"]]
    if boundaries != prepared["boundaries"]:
        raise RuntimeError("remesh changed native boundary seeds, identities or coordinates")
    changes, interior = _audit(prepared, after, data, result)
    replaceable = {("SHELL", i) for i in data["shell_ids"]} | {("GRID", i) for i in interior}
    if _reference_index(base, deck, after, replaceable) != prepared["reference_index"]:
        raise RuntimeError("external engineering references changed during remeshing")
    scope = mesh_scope.capture(state, base, mesh, constants, _selection(clean))
    if _relations(scope) != _relations(prepared["scope"]):
        raise RuntimeError("non-target Macro, CONS or VOLUME relationships changed")
    returned = result.get("native_return", {})
    if ((clean["remesh"]["algorithm"] == "FREE" and returned.get("shell_ids") != data["shell_ids"])
            or (clean["remesh"]["algorithm"] == "mapped" and returned.get("face_ids") != clean["face_ids"])):
        raise RuntimeError("native return disagrees with complete independent Macro membership")
    old_shells, new_shells = set(prepared["old_shell_ids"]), set(data["shell_ids"])
    old_nodes = set(prepared["old_interior_ids"])
    result.update(scope_verified=True, boundary_preserved=True, relations_verified=True,
        source_geometry_unchanged=True, continuous_surface_coverage_proven=False,
        result_shell_ids=data["shell_ids"], result_shell_count=len(data["shell_ids"]),
        added_shell_ids=sorted(new_shells-old_shells), removed_shell_ids=sorted(old_shells-new_shells),
        reused_shell_ids=sorted(old_shells & new_shells), result_interior_grid_ids=sorted(interior),
        added_grid_ids=sorted(interior-old_nodes), removed_grid_ids=sorted(old_nodes-interior),
        reused_interior_grid_ids=sorted(old_nodes & interior), faces_after=data["faces_after"],
        element_counts={kind: sum(e["type"] == kind for e in data["elements"]) for kind in ("CQUAD4", "CTRIA3")},
        point_projections=data["point_projections"], tessellation_changes=changes,
        scope_sha256_after=scope["scope_sha256"])
    guards._same_context(state, deck, prepared["context"])


def apply(state, base, mesh, calc, constants, params):
    state.assert_main()
    result = {"ok": False, "tool": "mesh_surface_remesh", "api_called": False, "backup_attempted": False,
              "materialization_attempted": False, "scope_verified": False, "boundary_preserved": False,
              "relations_verified": False, "possible_effects": False, "recovery_required": False,
              "context_result": {}, "timings_ms": {}}
    started = time.monotonic()
    clean = prepared = None
    try:
        clean = normalize(params)
        result.update(deck=clean["deck"], remesh=clean["remesh"], target_face_ids=clean["face_ids"])
        prepared = _preflight(state, base, mesh, constants, clean)
        result["timings_ms"]["preflight"] = (time.monotonic()-started)*1000
        phase = time.monotonic()
        result["backup_attempted"] = True
        result["backup"] = native._verify_backup(base, constants, *prepared["backup"])
        guards._same_context(state, clean["deck"], prepared["context"])
        if seeds._guard_snapshot(state, base, clean["deck"]) != prepared["before"]:
            raise RuntimeError("model changed during backup; remesh not called")
        if mesh_scope.capture(state, base, mesh, constants, _selection(clean))["scope_sha256"] != clean["remesh"]["expected_scope_sha256"]:
            raise RuntimeError("relationships changed during backup; remesh not called")
        result["timings_ms"]["backup"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        prepared = _materialize(state, base, mesh, calc, constants, clean, prepared, result)
        result["timings_ms"]["materialization"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        operation_error = None
        try:
            meshing_context.run_with_context(state, base, mesh, clean["remesh"]["target_length"],
                lambda: _invoke(state, base, mesh, clean, prepared, result), result["context_result"],
                element_type={"quad": "Quad", "mixed": "Mixed", "tria": "Tria"}[clean["remesh"]["element_type"]])
        except Exception as exc:
            operation_error = native._error_text(exc)
            result["native_error"] = operation_error
        result["timings_ms"]["native_and_context"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        _verify(state, base, mesh, calc, constants, clean, prepared, result)
        result["timings_ms"]["verification"] = (time.monotonic()-phase)*1000
        if operation_error:
            raise RuntimeError(operation_error)
        if result["context_result"].get("restored") is not True:
            raise RuntimeError("complete meshing configuration restoration was not verified")
        result["ok"] = True
    except Exception as exc:
        result["error"] = native._error_text(exc)
        if prepared is not None and clean is not None:
            try:
                guards._same_context(state, clean["deck"], prepared["context"])
                result["observed_faces"] = [meshing._face_snapshot(base, mesh, clean["deck"], i)[1] for i in clean["face_ids"]]
            except Exception as observation:
                result["observation_error"] = native._error_text(observation)
    finally:
        result["possible_effects"] = result["backup_attempted"] or result["materialization_attempted"] or result["api_called"]
        result["recovery_required"] = not result["ok"] and result["possible_effects"]
        result["original_operation_must_be_queried"] = result["recovery_required"]
        result["timings_ms"]["total"] = (time.monotonic()-started)*1000
    return result


def execute(state, tool, params):
    state.assert_main()
    if tool != "mesh_surface_remesh":
        raise ValueError("unsupported existing surface remesh operation")
    from ansa import base, mesh, calc, constants
    from .structured_meshing import _bounded_operation_result
    return _bounded_operation_result(state, params.get("deck"), apply(state, base, mesh, calc, constants, params))
