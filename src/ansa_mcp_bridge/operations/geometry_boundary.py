"""Backed-up, one-CONS-at-a-time auxiliary curve conversion.

A native conversion provenance record is not a proof of CAD equivalence.
"""
import math

from . import geometry_features as features, inspection, structured_native as native, volume_meshing

_BOUNDS_FIELDS = ("Min x", "Min y", "Min z", "Max x", "Max y", "Max z",
                  "Size x", "Size y", "Size z", "Size min", "Size max", "Diagonal")
_PARAMS = ("deck", "face_ids", "cons_ids", "auxiliary_part_id",
           "expected_source_snapshot_sha256", "backup_path")


def _entities(base, deck, container, kind, *, recursive=False):
    result = base.CollectEntities(deck, container, kind, recursive=recursive)
    if not isinstance(result, (list, tuple)):
        raise ValueError("boundary conversion entity collection failed; no empty-scope inference")
    rows = list(result)
    if len(rows) > 10000:
        raise ValueError("boundary conversion exceeds the 10000-entity collection budget")
    ids = [e._id for e in rows]
    if (any(type(i) is not int or i < 1 for i in ids) or len(ids) != len(set(ids))
            or kind != "__ALL_ENTITIES__" and any(e.ansa_type(deck) != kind for e in rows)):
        raise ValueError("invalid boundary conversion entity collection")
    return rows


def _snapshot(base, deck):
    snapshot = native._capture_scope(base, deck)
    # The existing generic CURVE card snapshot does not include Part ownership.
    for row in snapshot["entities"]:
        if row["entity"]["type"] == "CURVE":
            curve = features._entity(base, deck, "CURVE", row["entity"]["id"])
            part = base.GetEntityPart(curve)
            row["part_id"] = part._id if part is not None else None
    return snapshot


def preflight(state, base, constants, params):
    state.assert_main()
    inspection._params(params, _PARAMS)
    deck = params["deck"]
    context = volume_meshing._execution_context(state, deck)
    face_ids = features._ids(params["face_ids"], 20, "face_ids")
    cons_ids = features._ids(params["cons_ids"], 32, "cons_ids")
    if not face_ids or not cons_ids:
        raise ValueError("boundary conversion requires nonempty FACE and CONS scopes")
    expected = params["expected_source_snapshot_sha256"]
    if type(expected) is not str or native._SHA256_RE.fullmatch(expected) is None:
        raise ValueError("expected_source_snapshot_sha256 requires 64 lowercase hex digits")
    part_id = native._positive_int(params["auxiliary_part_id"], "auxiliary_part_id")
    stats = {"scoped_export_calls": 0}
    witness, _ = features._witness(state, base, deck, {"face_ids": face_ids, "curve_ids": []}, stats)
    if witness["source_snapshot_sha256"] != expected:
        raise ValueError("boundary source changed; obtain fresh FACE-only geometry features")
    boundaries = {row["id"]: row for row in witness["boundaries"]}
    if not set(cons_ids) <= boundaries.keys():
        raise ValueError("cons_ids must belong to the explicit FACE boundary scope")
    part = features._entity(base, deck, "ANSAPART", part_id)
    if part_id in {row["part_id"] for row in witness["parts"]}:
        raise ValueError("auxiliary Part must differ from source FACE Parts")
    if _entities(base, deck, part, "__ALL_ENTITIES__", recursive=True):
        raise ValueError("auxiliary Part must be empty")
    before = _snapshot(base, deck)
    part_row = native._scope_map(before).get(("ANSAPART", part_id))
    if part_row is None or part_row["fields"].get("Is Empty") != "YES":
        raise ValueError("auxiliary Part emptiness is not confirmed by its native card")
    current = base.GetCurrentEntity(deck, "ANSAPART")
    current_part_id = current._id if current is not None else None
    backup_preflight = native._backup_preflight(base, params)
    volume_meshing._same_context(state, deck, context)
    return {"witness": witness, "boundaries": boundaries, "before": before,
            "part": part, "part_id": part_id, "cons_ids": cons_ids, "context": context,
            "current_part_id": current_part_id, "backup_preflight": backup_preflight}


def _auxiliary_bounds(base, deck, part, curves, fields):
    contents = _entities(base, deck, part, "__ALL_ENTITIES__", recursive=True)
    if {(e.ansa_type(deck), e._id) for e in contents} != {("CURVE", e._id) for e in curves}:
        raise RuntimeError("auxiliary Part membership differs from newly observed CURVEs")
    if not curves:
        return {}
    bounds = base.BoundBox(curves)
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 6:
        raise RuntimeError("auxiliary CURVE bounds are unavailable")
    bounds = [features._number(value, "auxiliary bound") for value in bounds]
    sizes = [bounds[i + 3] - bounds[i] for i in range(3)]
    if any(value < 0 or not math.isfinite(value) for value in sizes):
        raise RuntimeError("auxiliary CURVE bounds are invalid")
    values = bounds + sizes + [min(sizes), max(sizes), math.hypot(*sizes)]
    expected = dict(zip(_BOUNDS_FIELDS, values))
    if fields.get("Is Empty") != "NO":
        raise RuntimeError("populated auxiliary Part still reports empty")
    for name, value in expected.items():
        actual = features._number(fields.get(name), "auxiliary Part " + name)
        if not math.isclose(actual, value, rel_tol=1e-12, abs_tol=0.):
            raise RuntimeError("auxiliary Part derived bound does not match its CURVEs: " + name)
    return {**expected, "Is Empty": "NO"}


def _audit(base, deck, prepared, after, created_ids):
    old, new = native._scope_map(prepared["before"]), native._scope_map(after)
    expected_added = {("CURVE", ident) for ident in created_ids}
    errors = []
    if old.keys() - new.keys():
        errors.append("original entities were removed")
    if new.keys() - old.keys() != expected_added:
        errors.append("unexpected entities were created or new CURVEs were not fully observed")
    curves = [features._entity(base, deck, "CURVE", i) for i in created_ids]
    aux_key = ("ANSAPART", prepared["part_id"])
    aggregates = _auxiliary_bounds(base, deck, prepared["part"], curves, new[aux_key]["fields"])
    for key in old.keys() & new.keys():
        left, right = old[key], new[key]
        if key == aux_key and curves:
            # Only recomputed, independently checked auxiliary bounds/emptiness may change.
            def definition(row):
                return {**row, "fields": {k:v for k,v in row["fields"].items() if k not in aggregates}}
            same = definition(left) == definition(right)
        else:
            same = left == right
        if not same:
            errors.append(f"original definition, assignment or geometry changed: {key}")
    for curve in curves:
        part = base.GetEntityPart(curve)
        if part is None or part._id != prepared["part_id"]:
            errors.append(f"new CURVE {curve._id} has incorrect Part ownership")
    current = base.GetCurrentEntity(deck, "ANSAPART")
    if (current._id if current is not None else None) != prepared["current_part_id"]:
        errors.append("current Part changed during conversion")
    return errors, aggregates


def _analysis(state, base, deck, curve, boundary, witness, stats):
    from ansa import calc
    from .geometry_fidelity import capture
    from .geometry_features_iges import analytic_curve, canonical_digest
    length = features._number(base.GetCurveLength(curve), "converted CURVE length", True)
    raw = features._export(state, base, [curve], stats)
    analysis = analytic_curve(raw)
    if analysis.get("status") == "supported" and not math.isclose(
            analysis["length"], length, rel_tol=1e-6, abs_tol=1e-8):
        analysis = {"status": "unsupported", "reason": "analytic export and native CURVE lengths differ"}
    endpoint = {"status": "incomplete", "reason": "analytic curve or native endpoint pair unavailable"}
    topology = boundary["endpoint_topology"]
    if analysis.get("status") == "supported" and topology["status"] == "available":
        vertices = {row["id"]: row["position"] for row in witness["vertices"]}
        first, second = (vertices[i] for i in topology["hot_point_ids"])
        direct = max(math.dist(analysis["start"], first), math.dist(analysis["end"], second))
        reverse = max(math.dist(analysis["start"], second), math.dist(analysis["end"], first))
        error = min(direct, reverse)
        endpoint = {"status": "matched" if error <= 1e-7 else "mismatch",
                    "maximum_error": error, "tolerance": 1e-7,
                    "direction_against_sorted_hot_point_ids": 1 if direct <= reverse else -1}
        if error > 1e-7:
            analysis = {"status": "unsupported", "reason": "converted analytic endpoints differ from native CONS endpoints"}
    fidelity = capture(base, calc, features._entity(base, deck, "CONS", boundary["id"]), curve)
    stats["curve_evaluation_calls"] += fidelity["evaluate_curve_calls"]
    stats["cons_projection_calls"] += fidelity["project_point_to_cons_calls"]
    return {"native_curve_length": length, "curve_export_sha256": canonical_digest(raw),
            "analysis": analysis, "endpoint_check": endpoint,
            "conversion_fidelity": fidelity,
            "source_geometry_equivalence": "not_proven"}


def apply_prepared(state, base, constants, params, prepared):
    """No automatic deletion, reopen, retry or rollback after a possibly effective call."""
    deck = params["deck"]
    created_ids, mappings, errors = [], [], []
    backup = None
    stats = {"scoped_export_calls": 0, "cons_to_curves_calls": 0,
             "curve_evaluation_calls": 0, "cons_projection_calls": 0}
    before_hash = features._hash(prepared["before"])
    api_called = False
    after = None
    aggregates = {}
    source_after = None
    try:
        volume_meshing._same_context(state, deck, prepared["context"])
        backup = native._verify_backup(base, constants, *prepared["backup_preflight"])
        if _snapshot(base, deck) != prepared["before"]:
            raise RuntimeError("model changed after backup; no conversion was called")
        fresh, _ = features._witness(state, base, deck, prepared["witness"]["source_scope"], stats)
        if fresh != prepared["witness"]:
            raise RuntimeError("boundary source changed after backup; no conversion was called")
        for cons_id in prepared["cons_ids"]:
            volume_meshing._same_context(state, deck, prepared["context"])
            original_ids = {e._id for e in _entities(base, deck, None, "CURVE")}
            edge = features._entity(base, deck, "CONS", cons_id)
            mapping = {"cons_id": cons_id, "provenance_status": "incomplete", "created_curve_ids": []}
            mappings.append(mapping)
            api_called = True
            stats["cons_to_curves_calls"] += 1
            try:
                returned = base.ConsToCurves([edge])
            finally:
                observed = sorted({e._id for e in _entities(base, deck, None, "CURVE")} - original_ids)
                created_ids.extend(observed)
                mapping["created_curve_ids"] = observed
            if not isinstance(returned, (list, tuple)) or len(returned) != 1 or len(observed) != 1:
                raise RuntimeError("single CONS conversion did not return exactly one newly observed CURVE")
            curve = returned[0]
            if curve.ansa_type(deck) != "CURVE" or type(curve._id) is not int or curve._id != observed[0]:
                raise RuntimeError("returned CURVE identity differs from the observed new entity")
            volume_meshing._same_context(state, deck, prepared["context"])
            if base.SetEntityPart([curve], prepared["part"]) != 1:
                raise RuntimeError("auxiliary CURVE Part assignment failed")
            owner = base.GetEntityPart(curve)
            if owner is None or owner._id != prepared["part_id"]:
                raise RuntimeError("auxiliary CURVE Part assignment readback failed")
            mapping.update(curve_id=curve._id, auxiliary_part_id=owner._id, provenance_status="verified")
            mapping.update(_analysis(state, base, deck, curve, prepared["boundaries"][cons_id], prepared["witness"], stats))
    except Exception as exc:
        errors.append(native._error_text(exc))
        if backup is None:
            backup = native._unconfirmed_backup(prepared["backup_preflight"][0])
    try:
        after = _snapshot(base, deck)
        audit_errors, aggregates = _audit(base, deck, prepared, after, created_ids)
        errors.extend(audit_errors)
        source_after, _ = features._witness(state, base, deck, prepared["witness"]["source_scope"], stats)
        if source_after != prepared["witness"]:
            errors.append("source FACE/CONS geometry or topology changed during conversion")
        volume_meshing._same_context(state, deck, prepared["context"])
    except Exception as exc:
        errors.append("independent postcondition readback failed: " + native._error_text(exc))
    complete = len(mappings) == len(prepared["cons_ids"]) and all(m["provenance_status"] == "verified" for m in mappings)
    verified = not errors and complete
    supported = sum(m.get("analysis", {}).get("status") == "supported" for m in mappings)
    return {"ok": verified, "status": "prepared" if verified else "recovery_required",
        "tool": "geometry_boundary_prepare", "deck": deck, "backup": backup, "api_called": api_called,
        "scope_verified": verified, "possible_effects": api_called or bool(errors),
        "recovery_required": not verified, "original_operation_must_be_queried": not verified,
        "error": "; ".join(errors) if errors else None, "errors": errors,
        "source_scope": prepared["witness"]["source_scope"],
        "source_snapshot_sha256": prepared["witness"]["source_snapshot_sha256"],
        "source_after_sha256": source_after["source_snapshot_sha256"] if source_after else None,
        "before_scope_sha256": before_hash, "after_scope_sha256": features._hash(after) if after else None,
        "auxiliary_part_id": prepared["part_id"], "verified_auxiliary_aggregates": aggregates,
        "new_curve_ids": created_ids, "mappings": mappings, "geometry_status": "incomplete",
        "summary": {"requested_cons_count": len(prepared["cons_ids"]), "new_curve_count": len(created_ids),
                    "provenance_complete": complete, "supported_converted_curves": supported,
                    "sampled_fidelity_curves": sum(m.get("conversion_fidelity", {}).get("status") == "sampled_only" for m in mappings),
                    "unsupported_or_unread_curves": len(mappings)-supported, **stats},
        "usable_for_mutation": False,
        "limitations": ["conversion provenance does not establish original CONS analytic equivalence",
                        "curve-to-CONS projections sample nine points only; no continuous or reverse coverage proof",
                        "unsupported splines are retained; no fitting or automatic CONS seed constraints",
                        "full-model protection checks recorded cards, bounds and assignments, not exact CAD equivalence",
                        "auxiliary CURVEs persist; no implicit cleanup, rollback, reopen or retries"]}
