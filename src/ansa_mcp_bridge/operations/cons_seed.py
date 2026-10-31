"""Read and set explicit native CONS edge counts without converting CAD curves."""

import copy
import math

from . import entity_catalog as catalog, geometry_features as features
from . import inspection, structured_native as native, volume_meshing as guards

_MAX_POINTS = 10000
_MAX_SCOPE_BYTES = 8 * 1024 * 1024
_PROJECTION_TOLERANCE = 1e-7
# Native tessellation bounds are display-derived. Keep their nanometer-scale
# allowance separate from node-to-CAD projection and actual GRID protection.
_BOUNDING_DISPLAY_TOLERANCE = 1e-6
_SEED_FIELDS = ("Num. of Nodes", "Order of Nodes", "Frozen", "Collapsed", "Biasing")
_CONS_DERIVED = ("Num. of Nodes", "Start Vector", "End Vector")
_PART_TESSELLATION = ("Mass", "Area", "COG x", "COG y", "COG z")
_PART_BOUNDS = ("Min x", "Min y", "Min z", "Max x", "Max y", "Max z",
                "Size x", "Size y", "Size z", "Diagonal")


def _collect(base, deck, container, kind):
    values = base.CollectEntities(deck, container, kind)
    if not isinstance(values, (list, tuple)):
        raise RuntimeError(f"unreadable {kind} collection; no empty-scope inference")
    if len(values) > 20000:
        raise ValueError(f"{kind} collection exceeds 20000 entities")
    seen = set()
    for entity in values:
        ref = catalog._entity_reference(base, deck, entity)
        key = (ref["type"], ref["id"])
        if key in seen or kind != "__ALL_ENTITIES__" and ref["type"] != kind:
            raise RuntimeError(f"invalid or duplicate {kind} entity")
        seen.add(key)
    return sorted(values, key=lambda e: (e.ansa_type(deck), e._id))


def _card(base, deck, entity):
    ref = catalog._entity_reference(base, deck, entity)
    return native._card(base, deck, entity, ref, f"{ref['type']} {ref['id']}")


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an explicit integer >= {minimum}")
    return value


def _grid(base, deck, entity):
    row = _card(base, deck, entity)
    fields = row["fields"]
    if fields.get("CP") not in (None, "", 0):
        raise ValueError("native seed GRID requires basic coordinates")
    xyz = [native._finite(fields.get(k), "seed GRID " + k) for k in ("X1", "X2", "X3")]
    if fields.get("NID") != entity._id:
        raise ValueError("seed GRID card identity mismatch")
    return {"id": entity._id, "xyz": xyz, "card": row}


def _boundary(base, deck, cons, *, include_points=False):
    row = _card(base, deck, cons)
    fields = row["fields"]
    if any(k not in fields for k in _SEED_FIELDS):
        raise ValueError("CONS lacks native subdivision fields")
    internal = _integer(fields["Num. of Nodes"], "CONS Num. of Nodes")
    # CollectEntities(CONS, GRID) instantiates native virtual nodes on 2026.1.
    # It is permitted only inside an already backed-up write, never in a read
    # or preflight. The card reports internal points, excluding open endpoints.
    points = None
    if include_points:
        points = [_grid(base, deck, e) for e in _collect(base, deck, cons, "GRID")]
        if len(points) > _MAX_POINTS:
            raise ValueError("CONS seed point budget exceeded")
    hot_points = _collect(base, deck, cons, "HOT POINT")
    vertices = [{"id": e._id, "xyz": [native._finite(v, "HOT POINT coordinate") for v in e.position]}
                for e in hot_points]
    if any(len(v["xyz"]) != 3 for v in vertices):
        raise ValueError("HOT POINT coordinates are incomplete")
    faces = base.GetFacesOfCons(cons)
    if not isinstance(faces, (list, tuple)) or not faces:
        raise ValueError("CONS has unreadable FACE adjacency")
    face_ids = sorted(catalog._entity_reference(base, deck, e, "FACE")["id"] for e in faces)
    if len(face_ids) != len(set(face_ids)):
        raise ValueError("duplicate CONS FACE adjacency")
    valid = (len(vertices) == 2 and fields["Order of Nodes"] == "1st"
             and vertices[0]["id"] != vertices[1]["id"]
             and math.dist(vertices[0]["xyz"], vertices[1]["xyz"]) > _PROJECTION_TOLERANCE)
    endpoint_grid_ids = []
    if points is not None:
        valid = valid and len(points) == internal + 2
    if valid and points is not None:
        for vertex in vertices:
            matches = [p["id"] for p in points if math.dist(p["xyz"], vertex["xyz"]) <= _PROJECTION_TOLERANCE]
            if len(matches) != 1:
                valid = False
                break
            endpoint_grid_ids.append(matches[0])
        valid = valid and len(set(endpoint_grid_ids)) == 2
    return {"cons_id": cons._id, "face_ids": face_ids,
            "native_fields": {k: fields[k] for k in _SEED_FIELDS},
            "native_internal_node_count": internal,
            "expected_point_count": internal + 2 if valid else None,
            "point_count": len(points) if points is not None else None,
            "point_validation": "native_points_checked" if points is not None else "deferred_to_backed_up_write",
            "segments": internal + 1 if valid else None,
            "status": "supported" if valid else "unsupported",
            "reason": None if valid else "requires first-order open CONS, two distinct native endpoints and, when requested, consistent boundary points",
            "points": [{"id": p["id"], "xyz": p["xyz"]} for p in points] if points is not None else None,
            "hot_points": vertices, "endpoint_grid_ids": sorted(endpoint_grid_ids)}


def capture(state, base, deck, face_ids, cons_ids):
    state.assert_main()
    faces = features._ids(face_ids, 20, "face_ids")
    cons = features._ids(cons_ids, 32, "cons_ids")
    if not faces or not cons:
        raise ValueError("explicit nonempty FACE and CONS scopes are required")
    stats = {"scoped_export_calls": 0}
    geometry, _ = features._witness(state, base, deck, {"face_ids": faces, "curve_ids": []}, stats)
    allowed = {v["id"] for v in geometry["boundaries"]}
    if not set(cons) <= allowed:
        raise ValueError("cons_ids must belong to explicit FACE boundaries")
    rows = [_boundary(base, deck, features._entity(base, deck, "CONS", i)) for i in cons]
    expected_points = sum(row["native_internal_node_count"] + 2 for row in rows)
    if expected_points > _MAX_POINTS:
        raise ValueError("seed scope exceeds the 10000 boundary-point budget")
    result = {"source_scope": {"face_ids": faces, "cons_ids": cons},
              "source_snapshot_sha256": geometry["source_snapshot_sha256"],
              "geometry_witness": geometry, "boundaries": rows}
    result["seed_snapshot_sha256"] = features._hash(result)
    result.update(ok=True, usable_for_mutation=False,
                  summary={"cons_count": len(rows), "supported_cons_count": sum(r["status"] == "supported" for r in rows),
                           "expected_point_count": expected_points, "point_collection_calls": 0, **stats},
                  limitations=["CONS card lengths are approximate; no automatic analytic curve classification",
                               "read and pagination do not collect perimeter GRID: native collection can instantiate virtual nodes",
                               "point IDs and positions are verified only after backup inside seed apply",
                               "readback supports first-order open CONS only; edge counts exclude endpoint duplication"])
    return result


def validate_report(state, base, deck, kind, result):
    if kind != "mesh_seed_read":
        return
    scope = result["source_scope"]
    current = capture(state, base, deck, scope["face_ids"], scope["cons_ids"])
    if current["seed_snapshot_sha256"] != result["seed_snapshot_sha256"]:
        raise ValueError("stale native seed evidence; obtain fresh mesh_seed_read")


def _point_budget(base, deck, cons, entity_count, grid_count, requested=None):
    requested = requested or {}
    total = 0
    for edge in cons:
        fields = _card(base, deck, edge)["fields"]
        if fields.get("Order of Nodes") != "1st":
            raise ValueError("global perimeter point budget requires first-order CONS")
        internal = _integer(fields.get("Num. of Nodes"), "CONS Num. of Nodes")
        total += max(internal + 2, requested.get(edge._id, 0) + 1)
        # Counts include repeated shared endpoints and existing point IDs. This
        # deliberately conservative bound is checked before any instantiation.
        if entity_count + total > 10000 or grid_count + total > 20000:
            raise ValueError("global perimeter point materialization exceeds model entity/GRID budget")
    return total


def _guard_snapshot(state, base, deck, *, materialize=False, budget_base=None):
    cons = _collect(base, deck, None, "CONS")
    if len(cons) > 5000:
        raise ValueError("seed protection exceeds 5000 CONS")
    entities = _collect(base, deck, None, "__ALL_ENTITIES__")
    if len(entities) > 10000:
        raise ValueError("seed protection exceeds 10000 model entities")
    grids = _collect(base, deck, None, "GRID")
    perimeter_ids = {}
    if materialize:
        budget_entities = len(budget_base["entities"]) if budget_base is not None else len(entities)
        budget_grids = len(budget_base["grids"]) if budget_base is not None else len(grids)
        _point_budget(base, deck, cons, budget_entities, budget_grids)
        perimeter_ids = {e._id: sorted(x._id for x in _collect(base, deck, e, "GRID")) for e in cons}
        entities = _collect(base, deck, None, "__ALL_ENTITIES__")
        grids = _collect(base, deck, None, "GRID")
        if len(entities) > 10000:
            raise ValueError("native perimeter collection exceeded model entity budget")
    rows = [native._scope_row(base, deck, e, catalog._entity_reference(base, deck, e)) for e in entities]
    # Unrelated solver nodes may use local CP. Preserve their raw full cards;
    # only boundary points that we project require basic-coordinate readback.
    grids = [{"id": e._id, "card": _card(base, deck, e)} for e in grids]
    cad = _collect(base, deck, None, "FACE") + _collect(base, deck, None, "CURVE")
    for entity in cad:
        part = base.GetEntityPart(entity)
        if part is None:
            raise ValueError("all CAD entities require Part ownership before read-only export")
        catalog._entity_reference(base, deck, part, "ANSAPART")
    for row in rows:
        if row["entity"]["type"] == "CURVE":
            curve = features._entity(base, deck, "CURVE", row["entity"]["id"])
            row["part_id"] = base.GetEntityPart(curve)._id
    stats = {"scoped_export_calls": 0}
    raw = features._export(state, base, cad, stats)
    from .geometry_features_iges import canonical_digest
    snapshot = {"entities": rows, "grids": grids, "perimeters": perimeter_ids,
                "cad_sha256": canonical_digest(raw)}
    if len(guards._encoded(snapshot)) > _MAX_SCOPE_BYTES:
        raise ValueError("seed protection exceeds the 8 MiB snapshot budget")
    return snapshot


def _materialize_points(state, base, deck, prepared):
    """Collect virtual point references only after the caller verifies backup."""
    initialized = _guard_snapshot(state, base, deck, materialize=True)
    before = prepared["before"]
    old = {r["id"]: r for r in before["grids"]}
    new = {r["id"]: r for r in initialized["grids"]}
    if any(new.get(i) != row for i, row in old.items()):
        raise RuntimeError("perimeter point collection changed an existing GRID")
    added = set(new) - set(old)
    perimeter_points = {i for values in initialized["perimeters"].values() for i in values}
    if not added <= perimeter_points:
        raise RuntimeError("perimeter point collection created unrelated GRID")
    original_rows = [r for r in initialized["entities"]
                     if not (r["entity"]["type"] == "GRID" and r["entity"]["id"] in added)]
    if original_rows != before["entities"] or initialized["cad_sha256"] != before["cad_sha256"]:
        raise RuntimeError("perimeter point collection changed original model definitions or CAD")
    boundaries = [_boundary(base, deck, features._entity(base, deck, "CONS", r["cons_id"]), include_points=True)
                  for r in prepared["assignments"]]
    if any(r["status"] != "supported" for r in boundaries):
        raise ValueError("native boundary points do not match the open CONS card and endpoints")
    requested = {r["cons_id"]: r["segments"] for r in prepared["assignments"]}
    for row in boundaries:
        existing_interior = ({p["id"] for p in row["points"]} & old.keys()) - set(row["endpoint_grid_ids"])
        if existing_interior and row["segments"] != requested[row["cons_id"]]:
            raise ValueError("changing pre-existing perimeter interior GRID requires a separate reuse contract")
    audited = {**prepared, "before": initialized, "protected_grid_ids": sorted(old),
               "witness": {**prepared["witness"], "boundaries": boundaries}}
    return audited, {"attempted": True, "new_virtual_point_ids": sorted(added),
                     "cons_collection_count": len(initialized["perimeters"]),
                     "scope_sha256": features._hash(initialized)}


def _assignments(value):
    if type(value) is not list or not 1 <= len(value) <= 32:
        raise ValueError("assignments requires 1..32 explicit CONS counts")
    rows = []
    for item in value:
        inspection._params(item, ("cons_id", "segments"))
        ident = _integer(item["cons_id"], "cons_id", 1)
        count = _integer(item["segments"], "segments", 1)
        if count > 10000:
            raise ValueError("segments exceeds 10000")
        rows.append({"cons_id": ident, "segments": count})
    if len({r["cons_id"] for r in rows}) != len(rows) or sum(r["segments"] + 1 for r in rows) > _MAX_POINTS:
        raise ValueError("duplicate CONS or total seed point budget exceeded")
    return sorted(rows, key=lambda r: r["cons_id"])


def preflight(state, base, constants, params):
    state.assert_main()
    inspection._params(params, ("deck", "face_ids", "assignments", "expected_seed_snapshot_sha256", "backup_path"))
    deck = params["deck"]
    context = guards._execution_context(state, deck)
    wanted = _assignments(params["assignments"])
    witness = capture(state, base, deck, params["face_ids"], [r["cons_id"] for r in wanted])
    expected = params["expected_seed_snapshot_sha256"]
    native._validate_digest(expected, "expected_seed_snapshot_sha256")
    if witness["seed_snapshot_sha256"] != expected:
        raise ValueError("native seed source/settings changed; obtain fresh mesh_seed_read")
    from ansa import mesh
    if not callable(getattr(mesh, "NumberPerimeters", None)):
        raise NotImplementedError("native NumberPerimeters is unavailable")
    allowed_faces = set(params["face_ids"])
    for row in witness["boundaries"]:
        if row["status"] != "supported":
            raise ValueError("unsupported CONS subdivision readback: " + str(row["cons_id"]))
        if not set(row["face_ids"]) <= allowed_faces:
            raise ValueError("all adjacent FACE must be in the seed scope")
        settings = row["native_fields"]
        if settings["Frozen"] != "NO" or settings["Collapsed"] != "NO" or settings["Biasing"] != "LINEAR":
            raise ValueError("seed apply requires unfrozen, uncollapsed, LINEAR CONS")
    macros = {}
    for face_id in sorted(allowed_faces):
        macro = base.GetFaceMacro(features._entity(base, deck, "FACE", face_id))
        if macro is None:
            raise ValueError("FACE has no readable macro")
        macros[macro._id] = macro
    shells = mesh.GetMacroShells(list(macros.values()))
    if type(shells) is int and shells == 0:
        shells = []  # Documented native empty-macro sentinel; None is not empty.
    if not isinstance(shells, (list, tuple)) or shells:
        raise ValueError("seed apply requires all affected macros to be unmeshed")
    before = _guard_snapshot(state, base, deck)
    _point_budget(base, deck, _collect(base, deck, None, "CONS"),
                  len(before["entities"]), len(before["grids"]), {r["cons_id"]: r["segments"] for r in wanted})
    backup = native._backup_preflight(base, params)
    guards._same_context(state, deck, context)
    return {"witness": witness, "before": before, "assignments": wanted,
            "context": context, "backup_preflight": backup}


def _audit(prepared, after, readback):
    before = prepared["before"]
    errors = []
    if before["cad_sha256"] != after["cad_sha256"]:
        errors.append("native CAD export changed")
    targets = {r["cons_id"] for r in prepared["assignments"]}
    affected_faces = {i for r in prepared["witness"]["boundaries"] for i in r["face_ids"]}
    parts = {r["part_id"] for r in prepared["witness"]["geometry_witness"]["parts"]
             if r["type"] == "FACE" and r["id"] in affected_faces}
    bp, ap = before["perimeters"], after["perimeters"]
    fixed = set(prepared.get("protected_grid_ids", [r["entity"]["id"] for r in before["entities"]
                                                  if r["entity"]["type"] == "GRID"]))
    fixed.update(i for r in prepared["witness"]["boundaries"] for i in r["endpoint_grid_ids"])
    fixed.update(i for key, values in bp.items() if key not in targets for i in values)
    fixed.update(i for key, values in ap.items() if key not in targets for i in values)
    b_edit = {i for key in targets for i in bp.get(key, [])} - fixed
    a_edit = {i for key in targets for i in ap.get(key, [])} - fixed
    changes = []
    def normalized(snapshot, editable):
        result = copy.deepcopy([r for r in snapshot["entities"]
                                if not (r["entity"]["type"] == "GRID" and r["entity"]["id"] in editable)])
        for row in result:
            kind, ident = row["entity"]["type"], row["entity"]["id"]
            if kind == "CONS" and ident in targets:
                for key in _CONS_DERIVED:
                    row["fields"].pop(key, None)
            elif kind == "FACE" and ident in affected_faces:
                for key in ("area", "center"):
                    row.get("geometry", {}).pop(key, None)
            elif kind == "ANSAPART" and ident in parts:
                for key in _PART_TESSELLATION:
                    row["fields"].pop(key, None)
        return result
    left, right = normalized(before, b_edit), normalized(after, a_edit)
    original = {(r["entity"]["type"], r["entity"]["id"]): r for r in left}
    for row in right:
        kind, ident = row["entity"]["type"], row["entity"]["id"]
        old = original.get((kind, ident))
        if old is None:
            continue
        if (kind == "CONS" and ident in targets) or (kind == "FACE" and ident in affected_faces):
            a, b = old.get("geometry", {}).get("bounds"), row.get("geometry", {}).get("bounds")
            if (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == 6
                    and all(type(x) in (int, float) and type(y) in (int, float)
                            and math.isfinite(x) and math.isfinite(y)
                            and abs(x-y) <= _BOUNDING_DISPLAY_TOLERANCE for x, y in zip(a, b))):
                row["geometry"]["bounds"] = a
        elif kind == "ANSAPART" and ident in parts:
            for key in _PART_BOUNDS:
                x, y = old["fields"].get(key), row["fields"].get(key)
                if (key in old["fields"] and key in row["fields"]
                        and type(x) in (int, float) and type(y) in (int, float)
                        and math.isfinite(x) and math.isfinite(y) and abs(x-y) <= _BOUNDING_DISPLAY_TOLERANCE):
                    row["fields"][key] = x
    if left != right:
        errors.append("original entity definitions, assignments, FE or protected geometry changed")
    old_rows = {(r["entity"]["type"], r["entity"]["id"]): r for r in before["entities"]}
    new_rows = {(r["entity"]["type"], r["entity"]["id"]): r for r in after["entities"]}
    for key in sorted(old_rows.keys() & new_rows.keys()):
        old, new = old_rows[key], new_rows[key]
        if old != new:
            changes.append({"entity": old["entity"], "before_sha256": features._hash(old), "after_sha256": features._hash(new)})
    if bp.keys() != ap.keys() or any(bp[i] != ap[i] for i in bp if i not in targets):
        errors.append("non-target perimeter node identities changed")
    b_grid = {r["id"]: r for r in before["grids"] if r["id"] not in b_edit}
    a_grid = {r["id"]: r for r in after["grids"] if r["id"] not in a_edit}
    if b_grid != a_grid:
        errors.append("non-target or solver GRID definitions changed")
    actual = {r["cons_id"]: r for r in readback["boundaries"]}
    for row in prepared["assignments"]:
        got = actual[row["cons_id"]]
        if got["status"] != "supported" or got["segments"] != row["segments"]:
            errors.append(f"CONS {row['cons_id']} native edge-count readback mismatch")
    return errors, changes


def apply_prepared(state, base, constants, params, prepared):
    from ansa import calc, mesh
    deck = params["deck"]
    errors, calls, projections = [], [], []
    backup = None
    after = readback = None
    api_called = False
    changes = []
    audited = None
    materialization = {"attempted": False}
    try:
        guards._same_context(state, deck, prepared["context"])
        backup = native._verify_backup(base, constants, *prepared["backup_preflight"])
        if _guard_snapshot(state, base, deck) != prepared["before"]:
            raise RuntimeError("model changed after backup; no subdivision call made")
        scope = prepared["witness"]["source_scope"]
        fresh = capture(state, base, deck, scope["face_ids"], scope["cons_ids"])
        if fresh != prepared["witness"]:
            raise RuntimeError("native subdivision source changed after backup")
        materialization["attempted"] = True
        audited, materialization = _materialize_points(state, base, deck, prepared)
        groups = {}
        for row in prepared["assignments"]:
            groups.setdefault(row["segments"], []).append(row["cons_id"])
        for number, ids in sorted(groups.items()):
            guards._same_context(state, deck, prepared["context"])
            entities = [features._entity(base, deck, "CONS", i) for i in ids]
            record = {"cons_ids": ids, "segments": number, "return_code": None}
            calls.append(record)
            api_called = True
            record["return_code"] = mesh.NumberPerimeters(entities, str(number), apply_multiple_number=False,
                remesh_macros=False, apply_number_of="edges", use_ansa_defaults_values=False)
            if type(record["return_code"]) is not int or record["return_code"] != 1:
                raise RuntimeError("native NumberPerimeters did not confirm success")
    except Exception as exc:
        errors.append(native._error_text(exc))
        if backup is None:
            backup = native._unconfirmed_backup(prepared["backup_preflight"][0])
    try:
        guards._same_context(state, deck, prepared["context"])
        scope = prepared["witness"]["source_scope"]
        readback = capture(state, base, deck, scope["face_ids"], scope["cons_ids"])
        after = _guard_snapshot(state, base, deck, materialize=audited is not None,
                                budget_base=prepared["before"])
        point_rows = []
        if audited is not None:
            point_rows = [_boundary(base, deck, features._entity(base, deck, "CONS", i), include_points=True)
                          for i in scope["cons_ids"]]
            readback["boundary_point_readback"] = point_rows
            if any(r["status"] != "supported" for r in point_rows):
                errors.append("native boundary-point count or endpoint readback mismatch")
            wanted = {r["cons_id"]: r["segments"] for r in prepared["assignments"]}
            if any(r["segments"] != wanted[r["cons_id"]] or r["point_count"] != wanted[r["cons_id"]] + 1
                   for r in point_rows):
                errors.append("final materialized boundary points differ from requested edge count")
            audit_errors, changes = _audit(audited, after, readback)
            errors.extend(audit_errors)
        for row in point_rows:
            edge = features._entity(base, deck, "CONS", row["cons_id"])
            distances = []
            for point in row["points"]:
                projected = calc.ProjectPointToCons(point["xyz"], edge)
                from .geometry_fidelity import _projection_points
                _extension, closest, _mode = _projection_points(projected)
                distance = math.dist(point["xyz"], closest)
                if not math.isfinite(distance):
                    raise RuntimeError("non-finite native seed projection distance")
                distances.append(distance)
            maximum = max(distances) if distances else None
            projections.append({"cons_id": row["cons_id"], "point_count": len(distances),
                                "maximum_observed_distance": maximum, "tolerance": _PROJECTION_TOLERANCE})
            if maximum is None or maximum > _PROJECTION_TOLERANCE:
                errors.append(f"CONS {row['cons_id']} boundary point projection failed")
        guards._same_context(state, deck, prepared["context"])
    except Exception as exc:
        errors.append("independent seed readback failed: " + native._error_text(exc))
    verified = not errors
    return {"ok": verified, "scope_verified": verified,
            "status": "applied" if verified else "recovery_required", "tool": "mesh_seed_apply", "deck": deck,
            "api_called": api_called, "backup": backup,
            "possible_effects": api_called or materialization["attempted"] or bool(errors),
            "recovery_required": not verified, "original_operation_must_be_queried": not verified,
            "errors": errors, "error": "; ".join(errors) if errors else None,
            "seed_snapshot_sha256": prepared["witness"]["seed_snapshot_sha256"],
            "seed_after_sha256": readback["seed_snapshot_sha256"] if readback else None,
            "source_geometry_unchanged": after is not None and after["cad_sha256"] == prepared["before"]["cad_sha256"],
            "before_scope_sha256": features._hash(prepared["before"]),
            "after_scope_sha256": features._hash(after) if after else None,
            "assignments": prepared["assignments"], "native_calls": calls, "point_projections": projections,
            "readback": readback, "perimeter_materialization": materialization, "tessellation_changes": changes,
            "summary": {"requested_cons_count": len(prepared["assignments"]), "number_perimeters_calls": len(calls),
                        "projection_calls": sum(r["point_count"] for r in projections)},
            "usable_for_mutation": False,
            "limitations": ["explicit first-order open CONS counts on unmeshed macros only",
                "native CAD export invariance and point checks are not continuous CAD equivalence proof",
                "affected FACE area/center and Part display mass/area/COG may change with native tessellation",
                "affected native bounding-box display drift is limited to 1e-6 mm with unchanged CAD export; node projection remains 1e-7 mm",
                "all pre-existing GRID are protected; changing already materialized interior GRID is not supported",
                "no automatic rollback, reopen, retry, analytic arc inference or source-face meshing"]}
