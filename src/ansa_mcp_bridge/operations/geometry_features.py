"""Bounded native CAD features; a fresh scoped export precedes every cache use.

IGES is an export-precision witness, not a proof of exact CAD equivalence.
Only explicit CURVE analytic primitives can feed automatic seed suggestions.
"""

from collections import OrderedDict
import copy
import hashlib
import math
from pathlib import Path
import stat
import time
import uuid

from . import _component_has_reparse_point, structured_evidence
from .geometry_features_iges import canonical_digest, analytic_curve
from .geometry_contours import build_contours


_EXPORT_LIMIT = 8 * 1024 * 1024
_CACHE_BYTES = 4 * 1024 * 1024
_CACHE_ENTRIES = 8


def _hash(value):
    return hashlib.sha256(structured_evidence._encoded(value)).hexdigest()


def _ids(value, limit, name):
    if type(value) is not list or len(value) > limit or any(type(x) is not int or x < 1 for x in value):
        raise ValueError(f"{name} requires at most {limit} positive integer IDs")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contains duplicate IDs")
    return sorted(value)


def _number(value, label, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{label} is not a finite {'positive ' if positive else ''}number")
    return float(value)


def _entity(base, deck, kind, ident):
    result = base.GetEntity(deck, kind, ident)
    if result is None or result._id != ident or result.ansa_type(deck) != kind:
        raise ValueError(f"missing or mismatched {kind} {ident}")
    return result


def _context(state, base, deck):
    auth = state.authorization or {}
    return {**structured_evidence._context(state, deck), "identity": state.identity(),
            "backend_id": auth.get("backend_id"), "generation": auth.get("generation"),
            "database": str(base.DataBaseName()), "units": base.GetLengthUnits()}


def _export(state, base, entities, stats):
    # No CONS here: 2026.1 silently exports visible geometry for CONS input.
    root = Path(state.session_dir) / "geometry-feature-exports"
    if _component_has_reparse_point(root):
        raise ValueError("geometry export directory cannot traverse reparse points")
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / (uuid.uuid4().hex + ".igs")
    try:
        stats["scoped_export_calls"] += 1
        try:
            rc = base.SaveFileAsIGES(str(path), "MILLIMETERS", entities)
        except Exception as exc:
            raise RuntimeError("scoped IGES export failed; no automatic save, reopen or retry: " + str(exc)) from exc
        if rc != 1:
            raise RuntimeError(f"scoped IGES export failed with {rc!r}")
        if _component_has_reparse_point(path):
            raise ValueError("geometry export path became a reparse point")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _EXPORT_LIMIT:
            raise ValueError("scoped IGES exceeds the 8 MiB export budget or is empty")
        with path.open("rb") as stream:
            raw = stream.read(_EXPORT_LIMIT + 1)
        if len(raw) > _EXPORT_LIMIT:
            raise ValueError("scoped IGES exceeds the export read budget")
        return raw
    finally:
        if path.exists() and not _component_has_reparse_point(path):
            path.unlink()


def _endpoints(base, deck, edge, vertices, stats):
    """Retain native identity; never reconstruct topology from rounded cards."""
    stats["cons_hot_point_calls"] = stats.get("cons_hot_point_calls", 0) + 1
    points = list(base.CollectEntities(deck, edge, "HOT POINT") or [])
    ids = []
    if len(points) > 1000:
        raise ValueError("CONS HOT POINT collection exceeds the 1000 vertex budget")
    for point in points:
        ident = point._id
        if point.ansa_type(deck) != "HOT POINT" or type(ident) is not int or ident < 1:
            raise ValueError("invalid CONS HOT POINT reference")
        ids.append(ident)
        if ident not in vertices:
            if len(vertices) >= 1000:
                raise ValueError("total HOT POINT budget exceeded")
            position = point.position
            if not isinstance(position, (list, tuple)) or len(position) != 3:
                raise ValueError("HOT POINT position must contain three coordinates")
            position = [_number(v, "HOT POINT coordinate") for v in position]
            stats["hot_point_adjacency_calls"] = stats.get("hot_point_adjacency_calls", 0) + 1
            adjacent = list(base.GetConsOfHotPoints(point) or [])
            adjacent_ids = [e._id for e in adjacent]
            if (len(adjacent_ids) > 100 or len(set(adjacent_ids)) != len(adjacent_ids)
                    or any(e.ansa_type(deck) != "CONS" or type(e._id) is not int or e._id < 1 for e in adjacent)):
                raise ValueError("invalid or over-budget HOT POINT/CONS adjacency")
            vertices[ident] = {"id": ident, "position": position, "cons_ids": sorted(adjacent_ids)}
        if edge._id not in vertices[ident]["cons_ids"]:
            raise ValueError("inconsistent CONS/HOT POINT adjacency")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate CONS HOT POINT reference")
    return {"status": "available" if len(ids) == 2 else "incomplete", "hot_point_ids": sorted(ids),
        "orientation_basis": "ascending native HOT POINT ID, not native CONS parameter direction",
        "reason": None if len(ids) == 2 else "requires exactly two distinct native HOT POINTs; no endpoint inference"}


def _witness(state, base, deck, scope, stats, *, face_limit=20):
    if deck != 1 or base.CurrentDeck() != deck:
        raise ValueError("geometry features currently require active NASTRAN deck")
    if face_limit not in (20, 100):
        raise ValueError("unsupported geometry witness FACE budget")
    faces = _ids(scope.get("face_ids"), face_limit, "face_ids")
    curves = _ids(scope.get("curve_ids"), 50, "curve_ids")
    if not faces and not curves:
        raise ValueError("explicit nonempty FACE or CURVE scope is required")
    context = _context(state, base, deck)
    if context["units"] != "millimeter":
        raise ValueError("geometry features currently require millimeter model units")
    if not context["database"] or not Path(context["database"]).is_file():
        raise ValueError("geometry export requires an already saved database; this read tool will not save or reopen")
    entities = {"FACE": [_entity(base, deck, "FACE", i) for i in faces],
                "CURVE": [_entity(base, deck, "CURVE", i) for i in curves]}
    parts = []
    # Orphan CURVE export can create a Part. Reject before the first export.
    for kind in ("FACE", "CURVE"):
        for e in entities[kind]:
            part = base.GetEntityPart(e)
            if part is None or type(part._id) is not int or part._id < 1:
                raise ValueError(f"{kind} {e._id} requires an assigned Part before read-only export")
            parts.append({"type": kind, "id": e._id, "part_id": part._id})
    topology, cons = [], {}
    for e in entities["FACE"]:
        edges = list(base.CollectEntities(deck, e, "CONS") or [])
        if not edges or len(edges) > 500:
            raise ValueError("FACE boundary is empty or exceeds the 500 CONS budget")
        ids = []
        for edge in edges:
            if edge.ansa_type(deck) != "CONS" or type(edge._id) is not int or edge._id < 1:
                raise ValueError("invalid FACE boundary entity")
            cons[edge._id] = edge
            ids.append(edge._id)
        if len(ids) != len(set(ids)) or len(cons) > 500:
            raise ValueError("duplicate FACE boundary or total CONS budget exceeded")
        topology.append({"face_id": e._id, "cons_ids": sorted(ids)})
    boundaries, vertices = [], {}
    for ident, edge in sorted(cons.items()):
        adjacent = list(base.GetFacesOfCons(edge) or [])
        ids = [e._id for e in adjacent]
        if len(ids) > 100 or len(set(ids)) != len(ids) or any(e.ansa_type(deck) != "FACE" for e in adjacent):
            raise ValueError("invalid or over-budget CONS adjacency")
        if not ids or any(row["face_id"] not in ids for row in topology if ident in row["cons_ids"]):
            raise ValueError("inconsistent FACE/CONS adjacency")
        card = base.GetEntityCardValues(deck, edge, ["Length", "Min Radius"])
        radius=card.get("Min Radius")
        radius_available=type(radius) in (int,float) and math.isfinite(radius) and radius>0
        boundary={"id": ident, "face_ids": sorted(ids),
            "native_card_length": _number(card.get("Length"), "CONS Length", True),
            "native_card_min_radius": float(radius) if radius_available else None,
            "measurement_source": "CONS card; may be discretized, not an analytic classification",
            "endpoint_topology": _endpoints(base, deck, edge, vertices, stats)}
        if not radius_available:
            boundary.update(native_card_min_radius_status="unavailable",native_card_min_radius_raw=repr(radius))
        boundaries.append(boundary)
    lengths = [{"id": e._id, "native_length": _number(base.GetCurveLength(e), "CURVE length", True)}
               for e in entities["CURVE"]]
    raw = _export(state, base, entities["FACE"] + entities["CURVE"], stats)
    witness = {"feature_schema": 2, "source_scope": {"face_ids": faces, "curve_ids": curves},
        "feature_context": context, "parts": parts, "face_boundaries": topology,
        "boundaries": boundaries, "vertices": [vertices[k] for k in sorted(vertices)], "curve_lengths": lengths,
        "cad_export_sha256": canonical_digest(raw)}
    witness["source_snapshot_sha256"] = _hash(witness)
    return witness, entities


def _vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(label + " must have three components")
    result = [_number(v, label) for v in value]
    norm = math.hypot(*result)
    if not math.isclose(norm, 1., rel_tol=1e-5, abs_tol=1e-5):
        raise ValueError(label + " is not a unit vector")
    return result


def _radius(value):
    if type(value) not in (int, float) or math.isnan(value) or value == 0:
        raise ValueError("SurfaceInfo radius is invalid")
    if math.isinf(value):
        return {"value": None, "infinite": True, "curvature": 0.}
    return {"value": float(value), "infinite": False, "curvature": _number(1. / value, "curvature")}


def _derive(state, base, witness, entities, stats):
    faces = entities["FACE"]
    surfaces = []
    if faces:
        stats["surface_info_calls"] += 1
        rows = base.SurfaceInfo(faces)
        if not isinstance(rows, (list, tuple)) or len(rows) != len(faces):
            raise ValueError("SurfaceInfo does not align with explicit FACE scope")
        for e, row in zip(faces, rows):
            if not isinstance(row, (list, tuple)) or len(row) != 8:
                raise ValueError("unsupported SurfaceInfo record shape")
            rs, rt = _radius(row[3]), _radius(row[4])
            us, ut, normal = (_vector(row[i], "SurfaceInfo direction") for i in (5, 6, 7))
            surfaces.append({"id": e._id, "surface_index": int(row[0]),
                "patches_s": int(row[1]), "patches_t": int(row[2]),
                "radius_s": rs, "radius_t": rt, "unit_s": us, "unit_t": ut, "normal": normal,
                "axis_candidate": (us if rs["infinite"] else ut) if rs["infinite"] != rt["infinite"] else None,
                "interpretation": "local surface data; axis candidate does not establish a cylinder or hole"})
    curves = []
    for e, length in zip(entities["CURVE"], witness["curve_lengths"]):
        raw = _export(state, base, [e], stats)
        analysis = analytic_curve(raw)
        if analysis.get("status") == "supported":
            if not math.isclose(analysis["length"], length["native_length"], rel_tol=1e-6, abs_tol=1e-8):
                analysis = {"status": "unsupported", "reason": "analytic IGES length differs from native CURVE length"}
        curves.append({**length, "analysis": analysis, "source": "explicit CURVE scoped IGES analytic primitive"})
    vertices = {v["id"]: v for v in witness["vertices"]}
    contour_edges = []
    for boundary in witness["boundaries"]:
        topology = boundary["endpoint_topology"]
        if topology["status"] != "available":
            continue  # Full FACE membership remains in build_contours: missing edges are incomplete.
        start, end = topology["hot_point_ids"]
        contour_edges.append({"id": boundary["id"], "vertex_ids": [start, end], "analysis": {
            "status": "topology_only", "kind": "native_cons", "start": vertices[start]["position"],
            "end": vertices[end]["position"], "length": boundary["native_card_length"]}})
    contours = build_contours(witness["face_boundaries"], contour_edges) if faces else {
        "status": "not_requested", "geometry_status": "not_requested", "faces": []}
    return {"surfaces": surfaces, "curves": curves, "contours": contours}


def _seed(derived, options, source_hash):
    if options is None:
        return {"status": "not_requested"}
    allowed = {"target_length", "max_chord_error", "min_segments", "multiple", "max_segments"}
    if type(options) is not dict or set(options) != allowed:
        raise ValueError("seed_options requires the normalized geometry seed fields")
    target = _number(options["target_length"], "target_length", True)
    chord = _number(options["max_chord_error"], "max_chord_error", True)
    for key in ("min_segments", "multiple", "max_segments"):
        if type(options[key]) is not int or not 1 <= options[key] <= 10000:
            raise ValueError("invalid seed option: " + key)
    curves = derived["curves"]
    unsupported = [row["id"] for row in curves if row["analysis"].get("status") != "supported"]
    if not curves or unsupported:
        return {"status": "incomplete", "unsupported_curve_ids": unsupported,
                "reason": "all requested CURVEs must be verified lines or circular arcs; CONS are not seed inputs"}
    edges = []
    for row in curves:
        a = row["analysis"]
        edge = {"id": "CURVE:" + str(row["id"]), "kind": a["kind"], "length": a["length"],
                "target_length": target, "min_segments": options["min_segments"], "multiple": options["multiple"]}
        if a["kind"] == "circular_arc":
            edge.update(radius=a["radius"], max_chord_error=chord)
        edges.append(edge)
    from .mesh_seed import recommend_seeds
    return {**recommend_seeds({"edges": edges, "max_segments": options["max_segments"]}),
            "input_origin": "verified native CURVE analytic export", "source_snapshot_sha256": source_hash,
            "usable_for_mutation": False}


def _sweep_options(options, face_ids, curve_ids):
    if options is None:
        return None
    from .inspection import _params
    _params(options, ("source_face_id", "target_face_id", "direction", "distance"), ("tolerance",))
    first, second = options["source_face_id"], options["target_face_id"]
    if any(type(v) is not int or v < 1 for v in (first, second)) or first == second:
        raise ValueError("sweep source/target must be distinct positive FACE IDs")
    if curve_ids or first not in face_ids or second not in face_ids:
        raise ValueError("sweep diagnostic requires a FACE-only scope containing both caps")
    tolerance = _number(options.get("tolerance", 1e-7), "sweep tolerance", True)
    if tolerance > 0.001:
        raise ValueError("sweep tolerance must not exceed 0.001 millimeter")
    return {"source_face_id": first, "target_face_id": second,
            "direction": _vector(options["direction"], "sweep direction"),
            "distance": _number(options["distance"], "sweep distance", True), "tolerance": tolerance}


def _sweep_plan(witness, options):
    from .cad_sweep_topology import plan_translation
    result = {"template": "T1_planar_quad_straight_sweep", "options": options,
              "source_snapshot_sha256": witness["source_snapshot_sha256"],
              "geometry_status": "incomplete", "native_execution_status": "unverified",
              "cad_surface_geometry_verified": False, "usable_for_mutation": False}
    try:
        correspondence = plan_translation(witness, **options)
    except ValueError as exc:
        result.update(status="unsupported", topology_status="unsupported", reason=str(exc))
    else:
        result.update(status="incomplete", topology_status="pass", correspondence=correspondence,
                      reason="corner/topology correspondence only; continuous CAD and native sweep contract remain unverified")
    result["plan_sha256"] = _hash(result)
    return result


def capture(state, base, deck, face_ids, curve_ids, seed_options=None, sweep_options=None):
    state.assert_main()
    sweep_options = _sweep_options(sweep_options, face_ids, curve_ids)
    started = time.monotonic()
    stats = {"scoped_export_calls": 0, "surface_info_calls": 0,
             "cons_hot_point_calls": 0, "hot_point_adjacency_calls": 0}
    witness, entities = _witness(state, base, deck, {"face_ids": face_ids, "curve_ids": curve_ids}, stats)
    key = witness["source_snapshot_sha256"]
    cache = getattr(state, "_geometry_features_cache", None)
    if cache is None:
        cache = state._geometry_features_cache = OrderedDict()
    hit = key in cache
    if hit:
        derived = copy.deepcopy(cache[key])
        cache.move_to_end(key)
    else:
        derived = _derive(state, base, witness, entities, stats)
        if len(structured_evidence._encoded(derived)) <= _CACHE_BYTES:
            cache[key] = copy.deepcopy(derived)
            while len(cache) > _CACHE_ENTRIES or sum(len(structured_evidence._encoded(v)) for v in cache.values()) > _CACHE_BYTES:
                cache.popitem(last=False)
    seed = _seed(derived, seed_options, key)
    result = {"status": "captured", **witness, **derived, "seed_plan": seed,
        "usable_for_mutation": False, "freshness": "scoped CAD export precision plus native topology and context",
        "summary": {"face_count": len(face_ids), "curve_count": len(curve_ids),
                    "cons_count": len(witness["boundaries"]), "cache_status": "hit" if hit else "miss",
                    "vertex_count": len(witness["vertices"]), "contour_status": derived["contours"]["status"],
                    **stats, "seed_status": seed["status"], "elapsed_seconds": time.monotonic() - started},
        "limitations": ["not an exact CAD equivalence or full-model coverage proof",
                        "outside-scope geometry is not validated; adjacent FACE IDs are recorded only",
                        "SurfaceInfo is local; CONS card lengths may be approximate",
                        "CONS loops prove native connectivity only; no analytic curve or circle classification",
                        "no automatic hole detection, CAD partitioning or native edge-seed writes"]}
    if sweep_options is not None:
        result["sweep_plan"] = _sweep_plan(witness, sweep_options)
        result["summary"]["sweep_status"] = result["sweep_plan"]["status"]
        result["summary"]["sweep_topology_status"] = result["sweep_plan"]["topology_status"]
    return result


def validate_report(state, base, deck, kind, result):
    if kind != "geometry_features":
        return
    state.assert_main()
    if result.get("feature_context") != _context(state, base, deck):
        raise ValueError("geometry feature evidence is stale: target or model context changed")
    witness, _ = _witness(state, base, deck, result["source_scope"], {"scoped_export_calls": 0})
    if witness["source_snapshot_sha256"] != result.get("source_snapshot_sha256"):
        raise ValueError("geometry feature evidence is stale: scoped geometry or topology changed")
