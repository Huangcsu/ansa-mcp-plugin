"""有界单孔板 O-grid 流程；网格生成始终由 ANSA 原生接口完成。"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

from ansa_mcp_shared.plate_hole import build_plan, normalize, seed_for_edge

from . import _component_has_reparse_point, _model_save, _save_path
from . import entity_catalog as catalog
from . import geometry_features as features
from .geometry_features_iges import canonical_digest
from . import meshing_context
from . import mesh_audit
from . import model_files
from . import structured_boundary
from . import structured_native as native
from . import volume_meshing


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_FACE_COUNT = 8
_MAX_FACES = 100
_MAX_SHELLS = 5000
_MAX_SOLIDS = 10000
_QUALITY_LIMITS = {"ASPECT": ("max", 5.0), "JACOB": ("min", 0.2)}
_CONTEXT_IDENTITY = (
    "pid", "process_start", "instance_id", "session_id", "deck", "backend_id", "generation"
)


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _collect(base, deck, kind, container=None):
    entities = base.CollectEntities(deck, container, kind)
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({kind}) did not return a complete entity list")
    rows = []
    ids = set()
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity, kind)
        if reference["id"] in ids:
            raise RuntimeError(f"CollectEntities({kind}) returned duplicate IDs")
        ids.add(reference["id"])
        rows.append(entity)
    return sorted(rows, key=lambda entity: entity._id)


def _ids(entities):
    return [entity._id for entity in entities]


def _bbox(base, entity, label):
    raw = base.BoundBox(entity)
    if type(raw) not in (list, tuple) or len(raw) != 6:
        raise RuntimeError(f"{label} has no complete native BoundBox")
    return [_finite(value, f"{label} BoundBox") for value in raw]


def _source_face_sha256(witness, surfaces, geometry, scope_rows):
    """Hash the original FACE topology and scoped CAD witness, not its transient handles."""
    return _digest({
        "witness": witness,
        "surfaces": surfaces,
        "geometry": geometry,
        "source_scope_rows": scope_rows,
    })


def _full_definition_cards(base, deck, scope):
    kinds = sorted({
        row["entity"]["type"] for row in scope["entities"]
        if row["entity"]["type"].startswith(("MAT", "PS"))
    })
    result = {}
    for kind in kinds:
        rows = []
        for entity in _collect(base, deck, kind):
            reference = catalog._entity_reference(base, deck, entity, kind)
            card = volume_meshing._read_full_card(
                base, deck, entity, reference, f"original {kind} {reference['id']}"
            )
            rows.append(card)
        result[kind] = rows
    return result


def _source_topology(base, deck, witness):
    """Require a connected, closed genus-one FACE boundary using native IDs."""
    face_rows = witness["face_boundaries"]
    face_ids = {row["face_id"] for row in face_rows}
    if len(face_ids) != _SOURCE_FACE_COUNT:
        raise ValueError("the finite plate source requires exactly eight distinct FACEs")

    boundary_by_id = {row["id"]: row for row in witness["boundaries"]}
    if len(boundary_by_id) != len(witness["boundaries"]):
        raise RuntimeError("source FACE witness contains duplicate CONS identities")
    points_by_id = {row["id"]: row["position"] for row in witness["vertices"]}
    face_graph = {face_id: set() for face_id in face_ids}
    face_loop_counts = {}
    all_vertices = set()

    for cons_id, row in boundary_by_id.items():
        adjacent = row["face_ids"]
        if len(adjacent) != 2 or len(set(adjacent)) != 2 or not set(adjacent) <= face_ids:
            raise ValueError(f"source CONS {cons_id} must bound exactly two source FACEs")
        first, second = adjacent
        face_graph[first].add(second)
        face_graph[second].add(first)
        endpoint_ids = row["endpoint_topology"]["hot_point_ids"]
        if len(endpoint_ids) not in (1, 2) or any(ident not in points_by_id for ident in endpoint_ids):
            raise ValueError(f"source CONS {cons_id} has no complete native HOT POINT topology")
        all_vertices.update(endpoint_ids)

    for face_row in face_rows:
        face_id = face_row["face_id"]
        degree = Counter()
        adjacency = defaultdict(set)
        cons_ids = face_row["cons_ids"]
        if not cons_ids or len(cons_ids) != len(set(cons_ids)):
            raise ValueError(f"source FACE {face_id} has an empty or duplicate boundary")
        for cons_id in cons_ids:
            boundary = boundary_by_id.get(cons_id)
            if boundary is None or face_id not in boundary["face_ids"]:
                raise ValueError(f"source FACE {face_id} boundary membership is incomplete")
            endpoint_ids = boundary["endpoint_topology"]["hot_point_ids"]
            if len(endpoint_ids) == 1:
                vertex = endpoint_ids[0]
                degree[vertex] += 2
            else:
                first, second = endpoint_ids
                degree[first] += 1
                degree[second] += 1
                adjacency[first].add(second)
                adjacency[second].add(first)
        if any(value != 2 for value in degree.values()):
            raise ValueError(f"source FACE {face_id} contains an open or branched contour")
        remaining = set(degree)
        loops = 0
        while remaining:
            loops += 1
            start = next(iter(remaining))
            stack = [start]
            component = set()
            while stack:
                vertex = stack.pop()
                if vertex in component:
                    continue
                component.add(vertex)
                stack.extend(set(adjacency.get(vertex, ())) - component)
            remaining.difference_update(component)
        face_loop_counts[face_id] = loops

    reached = set()
    stack = [min(face_ids)]
    while stack:
        face_id = stack.pop()
        if face_id in reached:
            continue
        reached.add(face_id)
        stack.extend(face_graph[face_id] - reached)
    if reached != face_ids:
        raise ValueError("source plate FACEs are not one connected closed component")
    # A planar FACE with two boundary loops is an annulus, not a disk cell.
    # The raw plate's two holed caps therefore each contribute zero to Euler.
    euler = (len(all_vertices) - len(boundary_by_id)
             + sum(2 - loops for loops in face_loop_counts.values()))
    if euler != 0:
        raise ValueError("source plate closed topology must have Euler characteristic zero")
    return {
        "face_count": len(face_ids),
        "cons_count": len(boundary_by_id),
        "hot_point_count": len(all_vertices),
        "face_loop_counts": {str(key): value for key, value in sorted(face_loop_counts.items())},
        "euler_characteristic": euler,
        "connected": True,
        "closed": True,
    }


def _validate_source_geometry(base, deck, recipe, witness, derived, face_entities):
    params = recipe["params"]
    tol = max(recipe["geometry_tolerance"] * 4.0, 1e-7)
    faces = face_entities["FACE"]
    bounds = {entity._id: _bbox(base, entity, f"source FACE {entity._id}")
              for entity in faces}
    face_areas = {}
    for entity in faces:
        face_id = entity._id
        area = _finite(base.GetFaceArea(entity), f"source FACE {face_id} area")
        if area <= 0:
            raise ValueError(f"source FACE {face_id} area is unavailable or nonpositive")
        face_areas[face_id] = area
    expected = [
        params["center"][0] - params["length"] / 2,
        params["center"][1] - params["width"] / 2,
        params["center"][2] - params["thickness"] / 2,
        params["center"][0] + params["length"] / 2,
        params["center"][1] + params["width"] / 2,
        params["center"][2] + params["thickness"] / 2,
    ]
    actual = [
        min(row[0] for row in bounds.values()), min(row[1] for row in bounds.values()),
        min(row[2] for row in bounds.values()), max(row[3] for row in bounds.values()),
        max(row[4] for row in bounds.values()), max(row[5] for row in bounds.values()),
    ]
    if any(abs(a - b) > tol for a, b in zip(actual, expected)):
        raise ValueError("source FACE bounds do not match the declared axis-aligned plate")

    candidates = []
    for row in derived["surfaces"]:
        rs, rt = row["radius_s"], row["radius_t"]
        if rs["infinite"] == rt["infinite"]:
            continue
        finite_radius = rt["value"] if rs["infinite"] else rs["value"]
        if abs(finite_radius - params["hole_radius"]) <= tol:
            candidates.append(row)
    if not 1 <= len(candidates) <= 2:
        raise ValueError("source CAD must contain one centered cylinder, represented by one or two FACEs")
    for cylinder in candidates:
        axis = cylinder.get("axis_candidate")
        if type(axis) not in (list, tuple) or len(axis) != 3:
            raise ValueError("native SurfaceInfo did not provide the cylindrical axis direction")
        if abs(axis[0]) > 1e-6 or abs(axis[1]) > 1e-6 or abs(abs(axis[2]) - 1.0) > 1e-6:
            raise ValueError("source hole axis must be parallel to the declared plate thickness axis")

    # Curved FACE tessellation bounds can undershoot the analytic radius. Bind the
    # centered hole with native SurfaceInfo plus its native CONS/HOT POINT endpoints.
    cx, cy, cz = params["center"]
    top_z, bottom_z = cz + params["thickness"] / 2, cz - params["thickness"] / 2
    cylinder_face_ids = {row["id"] for row in candidates}
    face_boundaries = {row["face_id"]: row["cons_ids"] for row in witness["face_boundaries"]}
    boundary_by_id = {row["id"]: row for row in witness["boundaries"]}
    hole_cons_ids = sorted({cons_id for face_id in cylinder_face_ids
                            for cons_id in face_boundaries.get(face_id, [])})
    if not hole_cons_ids:
        raise ValueError("native cylindrical FACE has no complete CONS boundary")
    cap_angles = {"bottom": set(), "top": set()}
    full_circle_caps = set()
    checked_hot_points = set()
    for cons_id in hole_cons_ids:
        boundary = boundary_by_id.get(cons_id)
        if boundary is None:
            raise ValueError("native cylinder CONS is missing from the complete source witness")
        endpoint_ids = boundary["endpoint_topology"]["hot_point_ids"]
        if len(endpoint_ids) not in (1, 2):
            raise ValueError(f"cylinder CONS {cons_id} has unsupported native HOT POINT endpoints")
        endpoint_caps = []
        for endpoint_id in endpoint_ids:
            point = next(row["position"] for row in witness["vertices"] if row["id"] == endpoint_id)
            radius = math.hypot(point[0] - cx, point[1] - cy)
            if abs(radius - params["hole_radius"]) > tol:
                raise ValueError("native hole-boundary HOT POINT does not match the declared center/radius")
            if abs(point[2] - bottom_z) <= tol:
                cap = "bottom"
            elif abs(point[2] - top_z) <= tol:
                cap = "top"
            else:
                raise ValueError("native hole-boundary HOT POINT is outside the two planar cap elevations")
            endpoint_caps.append(cap)
            cap_angles[cap].add(math.atan2(point[1] - cy, point[0] - cx) % math.tau)
            checked_hot_points.add(endpoint_id)
        if len(endpoint_ids) == 1:
            full_circle_caps.add(endpoint_caps[0])

    angular_tolerance = max(1e-7, 4.0 * tol / params["hole_radius"])
    for cap in ("bottom", "top"):
        angles = sorted(cap_angles[cap])
        if not angles:
            raise ValueError(f"native hole boundary has no {cap} cap endpoints")
        if cap not in full_circle_caps:
            if len(angles) < 2:
                raise ValueError(f"native {cap} hole boundary does not span a closed circular contour")
            gaps = [angles[index + 1] - angles[index] for index in range(len(angles) - 1)]
            gaps.append(angles[0] + math.tau - angles[-1])
            if max(gaps) > math.pi + angular_tolerance:
                raise ValueError(f"native {cap} hole CONS endpoints do not cover the centered circle")

    topology = _source_topology(base, deck, witness)
    return {"bounds": {str(key): value for key, value in bounds.items()},
            "face_areas": {str(key): value for key, value in face_areas.items()},
            "overall_bounds": actual,
            "hole_face_ids": sorted(cylinder_face_ids),
            "hole_radius_mm": params["hole_radius"],
            "hole_axis": [list(row["axis_candidate"]) for row in candidates],
            "hole_boundary_cons_ids": hole_cons_ids,
            "hole_boundary_hot_point_count": len(checked_hot_points),
            "hole_boundary_caps": {
                cap: {"angles_rad": sorted(cap_angles[cap]),
                      "full_loop_cons_present": cap in full_circle_caps}
                for cap in ("bottom", "top")
            },
            "topology": topology}


def _prepare_preflight(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize(params)
    recipe = build_plan(clean)
    if recipe.get("params") != clean:
        raise RuntimeError("shared plate-hole plan returned parameters different from normalization")
    deck = getattr(constants, "NASTRAN", None)
    native._check_nastran(constants, deck)
    if base.CurrentDeck() != deck:
        raise ValueError("the active ANSA deck must be NASTRAN")
    context = volume_meshing._execution_context(state, deck)
    for module, names in (
        (base, ("PlaneCut", "BoundBox", "SurfaceInfo", "GetFaceArea", "FreezeElements", "Open", "SaveAs",
                "GetEntityCardValues", "GetEntity", "GetEntityPart", "CollectEntities")),
        (mesh, ("MapBlock", "NumberPerimeters", "Create4SidedMesh", "GetMapVolumeBoundaries",
                "DefineCaps", "VolumesRemesh", "SetMeshParamTargetLength")),
    ):
        for name in names:
            if not callable(getattr(module, name, None)):
                raise NotImplementedError(f"required native API unavailable: {name}")

    database = volume_meshing._database_name(base)
    if not database or not Path(database).is_file():
        raise ValueError("the raw source model must already be saved to an available .ansa file")
    faces = _collect(base, deck, "FACE")
    face_ids = _ids(faces)
    if len(face_ids) != _SOURCE_FACE_COUNT:
        raise ValueError("plate-hole preflight accepts exactly eight raw CAD FACEs")
    for kind in ("GRID", "SHELL", "SOLID", "VOLUME"):
        if _collect(base, deck, kind):
            raise ValueError(f"raw plate source must contain no existing {kind} entities")
    parts = _collect(base, deck, "ANSAPART")
    if len(parts) != 1:
        raise ValueError("raw plate source must contain exactly one ANSAPART")

    part_id = parts[0]._id
    witness, face_entities = features._witness(
        state, base, deck, {"face_ids": face_ids, "curve_ids": []}, {"scoped_export_calls": 0}
    )
    source_parts = {row["part_id"] for row in witness["parts"]}
    if source_parts != {part_id}:
        raise ValueError("all eight source FACEs must belong to the sole ANSAPART")
    derived = features._derive(state, base, witness, face_entities, {"surface_info_calls": 0})
    geometry = _validate_source_geometry(base, deck, recipe, witness, derived, face_entities)

    # Read native surface/area/bounds and scoped CAD data before the broad scope hash,
    # so lazy geometry display fields are materialized before they enter the snapshot.
    database_fingerprint = model_files._fingerprint(Path(database))
    scope = native._capture_scope(base, deck)
    if scope.get("complete") is not True or scope.get("deck") != deck:
        raise RuntimeError("complete raw model scope could not be captured")
    scope["entities"].sort(key=lambda row: (row["entity"]["type"], row["entity"]["id"]))
    scope_map = native._scope_map(scope)
    type_counts = Counter(row["entity"]["type"] for row in scope["entities"])
    if type_counts.get("FACE", 0) != _SOURCE_FACE_COUNT or type_counts.get("ANSAPART", 0) != 1:
        raise ValueError("raw model scope differs from the isolated eight-FACE, one-Part source")
    if any(type_counts.get(kind, 0) for kind in ("GRID", "SHELL", "SOLID", "VOLUME")):
        raise ValueError("raw model scope contains pre-existing FE or VOLUME entities")
    face_rows = [row for row in scope["entities"] if row["entity"]["type"] == "FACE"]
    cons_rows = [row for row in scope["entities"] if row["entity"]["type"] == "CONS"]
    witness_cons = {row["id"] for row in witness["boundaries"]}
    scope_cons = {row["entity"]["id"] for row in cons_rows}
    if scope_cons != witness_cons:
        raise ValueError("source model contains CONS outside the complete eight-FACE boundary")
    source_face_sha256 = _source_face_sha256(witness, derived["surfaces"], geometry,
                                             face_rows + cons_rows)
    part_row = scope_map.get(("ANSAPART", part_id))
    if part_row is None:
        raise RuntimeError("sole source ANSAPART definition is missing from the complete scope")
    definitions = _full_definition_cards(base, deck, scope)
    definitions_sha256 = _digest(definitions)
    settings = meshing_context._capture_context(state, base, mesh)
    volume_meshing._same_context(state, deck, context)
    input_snapshot = {
        "database": database_fingerprint,
        "model_scope_sha256": native._hash(scope),
        "source_face_sha256": source_face_sha256,
        "part_id": part_id,
        "part_definition_sha256": native._hash(part_row),
        "material_property_sha256": definitions_sha256,
        "entity_counts": dict(sorted(type_counts.items())),
        "geometry": geometry,
    }
    body = {
        "schema": "ansa-plate-hole-model-plan",
        "version": 1,
        "recipe": recipe,
        "context": context,
        "input_snapshot": input_snapshot,
        "meshing_context_sha256": settings.snapshot_sha256,
    }
    return {
        "clean": clean,
        "recipe": recipe,
        "context": context,
        "input_snapshot": input_snapshot,
        "scope": scope,
        "part_row": part_row,
        "definitions": definitions,
        "settings": settings,
        "face_entities": face_entities,
        "source_face_sha256": source_face_sha256,
        "plan_sha256": _digest(body),
    }


def plan(params):
    """Return the pure shared recipe; this function does not import ANSA or write files."""
    return build_plan(params)


def preflight(state, base, mesh, constants, params):
    """Return a small JSON-safe, model-bound preflight record without Entity handles."""
    prepared = _prepare_preflight(state, base, mesh, constants, params)
    return {
        "ok": True,
        "plan": prepared["recipe"],
        "plan_sha256": prepared["plan_sha256"],
        "context": prepared["context"],
        "input_snapshot": prepared["input_snapshot"],
        "meshing_context_sha256": prepared["settings"].snapshot_sha256,
    }


def _validate_output_paths(base, backup_path, output_path):
    backup, _ = _save_path(backup_path, False)
    output, _ = _save_path(output_path, False)
    current = volume_meshing._database_name(base)
    if model_files._same_path(backup, output):
        raise ValueError("backup_path and output_path must be distinct")
    if model_files._same_path(backup, current) or model_files._same_path(output, current):
        raise ValueError("backup_path and output_path must differ from the current source database")
    return backup, output


def _new_artifact_directory(state):
    root = Path(state.session_dir)
    if not root.is_dir() or _component_has_reparse_point(root):
        raise ValueError("state.session_dir must be an existing directory without reparse points")
    for _ in range(16):
        candidate = root / ("plate-" + uuid.uuid4().hex[:12])
        try:
            candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
            return candidate.resolve(strict=True)
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a unique plate-hole evidence directory")


def _write_json(path, value):
    path = Path(path)
    if _component_has_reparse_point(path):
        raise ValueError("evidence path cannot traverse a symlink or reparse point")
    payload = _encoded(value) + b"\n"
    # ANSA's embedded Windows Python can still enforce MAX_PATH.
    temporary = path.with_name("tmp-" + uuid.uuid4().hex[:12])
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def _write_stage(artifact_dir, facts, stage, base=None, deck=None):
    facts["stage"] = stage
    if base is not None:
        counts = {}
        for kind in ("FACE", "CONS", "VOLUME", "SHELL", "SOLID", "GRID"):
            rows = base.CollectEntities(deck, None, kind)
            if type(rows) not in (list, tuple):
                raise RuntimeError(f"progress count for {kind} is unreadable")
            counts[kind] = len(rows)
        facts["counts"] = counts
    return _write_json(artifact_dir / "workflow-evidence.json", facts)


def _save_copy(base, constants, path):
    saved = _model_save(base, constants, {"path": str(path), "overwrite": False})
    if type(saved) is not dict or saved.get("saved") is not True:
        raise RuntimeError(f"native save helper did not verify checkpoint {path}")
    if not model_files._same_path(saved.get("path"), path):
        raise RuntimeError("native save helper returned a different checkpoint path")
    return {**saved, **model_files._public_file_record(model_files._fingerprint(Path(path)))}


def _native_call_once(state, deck, context, function, *args, before_call=None, **kwargs):
    """Guard and call one native write exactly once; errors are never replayed."""
    volume_meshing._same_context(state, deck, context)
    if before_call is not None:
        before_call()
    return function(*args, **kwargs)


def _plane_cut(state, base, deck, context, point, second, third, faces, before_call=None):
    if not faces:
        raise ValueError("native plane cut selection is empty")
    points = (*point, *second, *third)
    result = _native_call_once(
        state, deck, context, base.PlaneCut, *points, faces, True, True, before_call=before_call
    )
    if type(result) is not int or result != 1:
        raise RuntimeError(f"native PlaneCut returned {result!r}")
    return result


def _central_faces(base, deck, params, tolerance):
    cx, cy = params["center"][:2]
    a = params["transition_half_size"]
    result = []
    for face in _collect(base, deck, "FACE"):
        bounds = _bbox(base, face, f"central selection FACE {face._id}")
        if (bounds[0] >= cx - a - tolerance and bounds[3] <= cx + a + tolerance
                and bounds[1] >= cy - a - tolerance and bounds[4] <= cy + a + tolerance):
            result.append(face)
    return result


def _classify_cons_edges(base, deck, params):
    groups = defaultdict(list)
    rows = []
    for cons in _collect(base, deck, "CONS"):
        points = base.CollectEntities(deck, cons, "HOT POINT")
        if type(points) not in (list, tuple) or len(points) != 2:
            raise ValueError(f"post-partition CONS {cons._id} must have two native HOT POINT endpoints")
        endpoints = []
        for point in points:
            xyz = point.position
            if type(xyz) not in (list, tuple) or len(xyz) != 3:
                raise RuntimeError(f"CONS {cons._id} HOT POINT has no complete position")
            endpoints.append([_finite(value, f"CONS {cons._id} HOT POINT") for value in xyz])
        # The shared pure helper matches only declared nominal geometry and its tolerance.
        seed = seed_for_edge(endpoints, params)
        groups[seed["segments"]].append(cons)
        rows.append({"cons_id": cons._id, "endpoints": endpoints, **seed})
    if not rows:
        raise ValueError("partition produced no CONS perimeters")
    return groups, sorted(rows, key=lambda row: row["cons_id"])


def _surface_capture(base, deck):
    shells = _collect(base, deck, "SHELL")
    shell_ids = _ids(shells)
    if len(shell_ids) > _MAX_SHELLS:
        raise ValueError("surface capture exceeds the 5000-SHELL acceptance budget")
    elements = native._read_elements(base, deck, "SHELL", shell_ids, {"CQUAD4", "CTRIA3"})
    node_ids = {node_id for row in elements for node_id in row["nodes"]}
    nodes = native._read_grids(base, deck, node_ids)
    if {row["id"] for row in nodes} != node_ids:
        raise RuntimeError("surface capture did not read every referenced GRID")
    return {"deck": deck, "elements": elements, "nodes": nodes,
            "shell_ids": shell_ids, "shell_count": len(elements), "node_count": len(nodes)}


def _volume_boundaries(base, mesh, deck, volumes):
    face_owners = defaultdict(list)
    caps = {}
    face_entities = {}
    for volume in volumes:
        volume_id = catalog._entity_reference(base, deck, volume, "VOLUME")["id"]
        raw = mesh.GetMapVolumeBoundaries(volume)
        if type(raw) is not dict or set(raw) != {"master", "slave", "round"}:
            raise RuntimeError(f"VOLUME {volume_id} MapBlock boundaries are incomplete")
        groups = {}
        for name in ("master", "slave", "round"):
            if type(raw[name]) not in (list, tuple):
                raise RuntimeError(f"VOLUME {volume_id} {name} boundary is not an entity list")
            group_ids = []
            for face in raw[name]:
                reference = catalog._entity_reference(base, deck, face, "FACE")
                group_ids.append(reference["id"])
                face_owners[reference["id"]].append(volume_id)
                face_entities[reference["id"]] = face
            if len(group_ids) != len(set(group_ids)):
                raise RuntimeError(f"VOLUME {volume_id} has duplicate {name} boundary faces")
            groups[name] = sorted(group_ids)
        if set(groups["master"]) & set(groups["slave"]) or not groups["master"] or not groups["slave"]:
            raise ValueError(f"VOLUME {volume_id} has invalid native caps")
        caps[str(volume_id)] = groups
    owner_rows = {str(face_id): sorted(ids) for face_id, ids in sorted(face_owners.items())}
    if any(len(ids) not in (1, 2) for ids in face_owners.values()):
        raise ValueError("every partition FACE must belong to one exterior or two adjacent MapBlocks")
    return owner_rows, caps, face_entities


def _assign_axial_caps(state, base, mesh, deck, context, volumes, params, tolerance):
    cz = params["center"][2]
    half = params["thickness"] / 2
    before = {}
    for volume in volumes:
        volume_id = volume._id
        boundaries = mesh.GetMapVolumeBoundaries(volume)
        all_faces = {face._id: face for name in ("master", "slave", "round")
                     for face in boundaries[name]}
        bottom = [face for face in all_faces.values()
                  if abs(_bbox(base, face, f"VOLUME {volume_id} FACE")[2] - (cz - half)) <= tolerance
                  and abs(_bbox(base, face, f"VOLUME {volume_id} FACE")[5] - (cz - half)) <= tolerance]
        top = [face for face in all_faces.values()
               if abs(_bbox(base, face, f"VOLUME {volume_id} FACE")[2] - (cz + half)) <= tolerance
               and abs(_bbox(base, face, f"VOLUME {volume_id} FACE")[5] - (cz + half)) <= tolerance]
        if len(bottom) != 1 or len(top) != 1:
            raise ValueError(f"VOLUME {volume_id} requires one complete bottom and top axial cap")
        before[str(volume_id)] = {"master": [bottom[0]._id], "slave": [top[0]._id]}
        returned = _native_call_once(state, deck, context, mesh.DefineCaps, volume, bottom, top)
        if returned is not None:
            raise RuntimeError(f"native DefineCaps returned {returned!r}, expected None")
        reread = mesh.GetMapVolumeBoundaries(volume)
        master_ids = sorted(face._id for face in reread["master"])
        slave_ids = sorted(face._id for face in reread["slave"])
        if master_ids != [bottom[0]._id] or slave_ids != [top[0]._id]:
            raise RuntimeError(f"VOLUME {volume_id} cap readback differs from the requested axial faces")
    return before


def _capture_complete_fe(base, constants, deck, volumes, expected_solids, expected_nodes):
    all_solids = _collect(base, deck, "SOLID")
    solid_ids = _ids(all_solids)
    if not 1 <= len(solid_ids) <= _MAX_SOLIDS or len(solid_ids) != expected_solids:
        raise ValueError("native SOLID count is incomplete or differs from the bounded recipe")
    elements = native._read_elements(base, deck, "SOLID", solid_ids, {"CHEXA"})
    if len(elements) != len(solid_ids) or any(row["type"] != "CHEXA" for row in elements):
        raise ValueError("finite refined plate requires a complete CHEXA-only SOLID capture")

    owners = {}
    for volume in volumes:
        volume_id = catalog._entity_reference(base, deck, volume, "VOLUME")["id"]
        members = base.CollectEntities(deck, volume, "SOLID")
        if type(members) not in (list, tuple):
            raise RuntimeError(f"VOLUME {volume_id} SOLID membership is unreadable")
        for member in members:
            reference = catalog._entity_reference(base, deck, member, "SOLID")
            owners.setdefault(reference["id"], []).append(volume_id)
    if set(owners) != set(solid_ids) or any(len(values) != 1 for values in owners.values()):
        raise ValueError("full native VOLUME/SOLID membership is incomplete or ambiguous")
    for row in elements:
        row["block_id"] = "VOLUME:" + str(owners[row["id"]][0])

    referenced_nodes = {node_id for row in elements for node_id in row["nodes"]}
    all_grids = _collect(base, deck, "GRID")
    grid_ids = _ids(all_grids)
    if len(grid_ids) != expected_nodes or set(grid_ids) != referenced_nodes:
        raise ValueError("native GRID scope differs from the complete SOLID connectivity")
    nodes = native._read_grids(base, deck, grid_ids)
    capture = _complete_capture(deck, elements, nodes, owners, expected_solids, expected_nodes)
    return capture, {str(solid_id): "VOLUME:" + str(owners[solid_id][0]) for solid_id in sorted(owners)}


def _complete_capture(deck, elements, nodes, owners, expected_solids, expected_nodes):
    element_ids = [row["id"] for row in elements]
    node_ids = {row["id"] for row in nodes}
    if len(element_ids) != len(set(element_ids)) or len(element_ids) != expected_solids:
        raise ValueError("independent SOLID capture has missing or duplicate entity IDs")
    if len(nodes) != expected_nodes or len(node_ids) != expected_nodes:
        raise ValueError("independent GRID capture has missing or duplicate entity IDs")
    missing = sorted({node for row in elements for node in row["nodes"]} - node_ids)
    if missing:
        raise ValueError("independent SOLID capture references missing GRID IDs")
    if set(owners) != set(element_ids) or any(len(values) != 1 for values in owners.values()):
        raise ValueError("independent native VOLUME membership is not complete")
    return {
        "deck": deck,
        "complete": True,
        "elements": elements,
        "nodes": nodes,
        "integrity": {
            "complete": True,
            "requested_element_count": expected_solids,
            "element_count": len(elements),
            "node_count": len(nodes),
            "missing_node_ids": [],
            "element_types": dict(sorted(Counter(row["type"] for row in elements).items())),
            "basis": "all native SOLID IDs, exact VOLUME membership, all referenced GRID",
        },
        "ownership": {
            "origin": "native_VOLUME_membership",
            "complete": True,
            "known_count": len(owners),
        },
    }


def _face_interfaces(base, deck, volumes, face_owners, membership):
    volume_ids = {volume._id for volume in volumes}
    memberships = {row["face_id"]: row for row in membership}
    interfaces = []
    outer_faces = []
    for face_text, owners in sorted(face_owners.items(), key=lambda item: int(item[0])):
        face_id = int(face_text)
        if not set(owners) <= volume_ids or face_id not in memberships:
            raise RuntimeError("native volume FACE ownership is incomplete")
        record = memberships[face_id]
        if record.get("membership_complete") is not True:
            raise ValueError(f"FACE {face_id} does not have complete native SHELL membership")
        facets = [row["nodes"] for row in record["shells"]]
        if len(owners) == 1:
            outer_faces.append(face_id)
        elif len(owners) == 2:
            interfaces.append({
                "id": "FACE:" + str(face_id),
                "blocks": ["VOLUME:" + str(value) for value in owners],
                "facets": facets,
            })
        else:
            raise ValueError(f"FACE {face_id} has unsupported native volume owner count")
    return interfaces, sorted(outer_faces)


def _definition_snapshot(base, deck, part_id, original_definition_ids):
    part, part_ref = catalog._get_entity(base, deck, "ANSAPART", part_id)
    part_row = native._scope_row(base, deck, part, part_ref)
    definitions = {}
    for kind, ids in original_definition_ids.items():
        rows = []
        for entity_id in ids:
            entity, reference = catalog._get_entity(base, deck, kind, entity_id)
            rows.append(volume_meshing._read_full_card(
                base, deck, entity, reference, f"rechecked {kind} {entity_id}"
            ))
        definitions[kind] = rows
    return part_row, definitions


def _compare_definitions(original_part, original_cards, current_part, current_cards):
    def part_definition(row):
        return {**row, "fields": {key: value for key, value in row["fields"].items()
                                  if key not in native._PART_AGGREGATE_FIELDS}}

    if not catalog._strict_equal(part_definition(original_part), part_definition(current_part)):
        raise ValueError("original ANSAPART definition changed outside its documented derived fields")
    original_map = {
        (kind, row["entity"]["id"]): row
        for kind, rows in original_cards.items() for row in rows
    }
    current_map = {
        (kind, row["entity"]["id"]): row
        for kind, rows in current_cards.items() for row in rows
    }
    differences = []
    for key, before in original_map.items():
        after = current_map.get(key)
        if after is None:
            differences.append({"entity": key, "reason": "original definition missing"})
            continue
        left, right = deepcopy(before), deepcopy(after)
        if key[0].startswith("PS"):
            for card in (left, right):
                card["card_fields"] = [name for name in card["card_fields"]
                                        if re.sub(r"[^a-z0-9]", "", name.casefold()) != "numelem"]
                card["fields"] = {name: value for name, value in card["fields"].items()
                                  if re.sub(r"[^a-z0-9]", "", name.casefold()) != "numelem"}
        if not catalog._strict_equal(left, right):
            differences.append({"entity": key, "reason": "original full card changed"})
    if differences:
        raise ValueError("original material/property definitions changed: " + str(differences[:8]))
    return {
        "part_definition_unchanged": True,
        "original_material_property_definitions_unchanged": True,
        "original_definition_count": len(original_map),
        "allowed_derived_field": "PS* Num.Elem only",
        "part_derived_fields": sorted(native._PART_AGGREGATE_FIELDS),
    }


def _cad_sha256(state, base, deck):
    faces = _collect(base, deck, "FACE")
    if not 1 <= len(faces) <= _MAX_FACES:
        raise ValueError("CAD equivalence capture requires 1..100 explicit FACEs")
    # ANSA can reorder the aggregate IGES entities on reopen. Each scoped FACE
    # export remains exact; bind its D/P digest to the persistent native FACE ID.
    stats = {"scoped_export_calls": 0}
    witness = {str(face._id): canonical_digest(features._export(state, base, [face], stats))
               for face in faces}
    return _digest(witness), witness


def _rebind_after_open(state, base, deck, previous_context, path):
    state.assert_main()
    state.invalidate_model()
    status = base.Open(str(path))
    if type(status) is not int or status != 0:
        raise RuntimeError(f"native base.Open returned {status!r} for the saved checkpoint")
    if not model_files._same_path(base.DataBaseName(), path):
        raise RuntimeError("ANSA did not open the requested saved checkpoint")
    if base.CurrentDeck() != deck:
        raise RuntimeError("saved checkpoint reopened in a different deck")
    context = volume_meshing._execution_context(state, deck)
    if any(context.get(key) != previous_context.get(key) for key in _CONTEXT_IDENTITY):
        raise RuntimeError("ANSA target, session, backend authorization or deck changed during reopen")
    return context


def _quality(base, solids):
    summary = {
        name: {"minimum": None, "maximum": None, "checked": 0, "unknown": 0, "failed_ids": []}
        for name in _QUALITY_LIMITS
    }
    for solid in solids:
        result = base.ElementQuality(solid, ["ASPECT", "JACOB"])
        if type(result) not in (list, tuple) or len(result) != 2:
            for name in summary:
                summary[name]["unknown"] += 1
            continue
        for name, value in zip(("ASPECT", "JACOB"), result):
            if type(value) not in (int, float) or not math.isfinite(value):
                summary[name]["unknown"] += 1
                continue
            value = float(value)
            row = summary[name]
            row["checked"] += 1
            row["minimum"] = value if row["minimum"] is None else min(row["minimum"], value)
            row["maximum"] = value if row["maximum"] is None else max(row["maximum"], value)
            direction, limit = _QUALITY_LIMITS[name]
            failed = value > limit if direction == "max" else value < limit
            if failed:
                row["failed_ids"].append(solid._id)
    for row in summary.values():
        row["failed_count"] = len(row["failed_ids"])
    return summary


def _positive_volumes(snapshot):
    coordinates = {row["id"]: row["xyz"] for row in snapshot["nodes"]}
    minimum = None
    invalid = []
    for element in snapshot["elements"]:
        signed, zero_tolerance = mesh_audit._signed_polyhedral_volume(element, coordinates)
        if signed <= zero_tolerance:
            invalid.append({"element_id": element["id"], "signed_volume": signed,
                            "zero_tolerance": zero_tolerance})
        minimum = signed if minimum is None else min(minimum, signed)
    return {"complete": not invalid and bool(snapshot["elements"]),
            "checked": len(snapshot["elements"]), "minimum_signed_volume": minimum,
            "nonpositive_or_degenerate": invalid}


def _audit_output(state, base, mesh, calc, constants, deck, recipe, volumes,
                  volume_face_owners, capture, surface, caps_expected):
    face_ids = _ids(_collect(base, deck, "FACE"))
    if len(face_ids) != recipe["faces"]:
        raise ValueError("post-fill CAD FACE count differs from the bounded recipe")
    if len(volumes) != recipe["block_count"]:
        raise ValueError("post-fill native MapBlock count differs from the bounded recipe")
    membership, _face_entities = structured_boundary.capture_membership(
        base, mesh, constants, deck, face_ids
    )
    if len(membership) != recipe["faces"] or any(row.get("membership_complete") is not True for row in membership):
        raise ValueError("structured_boundary did not capture all 92 FACE memberships completely")
    shell_ids = {row["id"] for row in surface["elements"]}
    mapped_shell_ids = {shell["id"] for face in membership for shell in face["shells"]}
    if shell_ids != mapped_shell_ids or len(shell_ids) != recipe["predicted_boundary_shells"]:
        raise ValueError("complete 92-FACE membership does not match the all-SHELL capture")
    interfaces, outer_face_ids = _face_interfaces(
        base, deck, volumes, volume_face_owners, membership
    )
    if len(interfaces) != 28 or len(outer_face_ids) != 64:
        raise ValueError("native VOLUME/FACE membership differs from 28 shared and 64 exterior FACEs")
    cad = structured_boundary.capture(
        base, mesh, calc, constants, deck, capture,
        {"face_ids": outer_face_ids, "tolerance": recipe["geometry_tolerance"] * 4.0,
         "cylinders": []},
    )
    projection = cad["projection"]
    if projection["requested_node_face_pairs"] > structured_boundary.MAX_PROJECTIONS:
        raise ValueError("external CAD projection exceeds the 5000 FACE/GRID-pair budget")
    if (cad["membership"]["status"] != "pass" or projection["status"] != "pass"
            or projection["measured_node_face_pairs"] != projection["requested_node_face_pairs"]):
        raise ValueError("full external FACE/SHELL membership or CAD projection did not pass")
    audit = mesh_audit.audit_snapshot(
        cad["audit_snapshot"], expected_boundary=cad["expected_boundary"],
        expected_components=1, interfaces=interfaces, protected_cylinders=[],
    )
    if audit["topology_status"] != "pass" or audit["geometry_status"] != "pass":
        raise ValueError("complete solid exterior/shared-interface audit did not pass")
    boundary_facets = len(cad["expected_boundary"])
    if boundary_facets != recipe["predicted_outer_shells"]:
        raise ValueError("complete free exterior skin count differs from the bounded recipe")
    if caps_expected != _volume_boundaries(base, mesh, deck, volumes)[1]:
        raise ValueError("native MapBlock caps changed during fill or save/reopen")
    return {
        "membership": cad["membership"],
        "native_face_membership": membership,
        "projection": projection,
        "audit": audit,
        "interfaces": interfaces,
        "outer_face_ids": outer_face_ids,
        "outer_shell_count": boundary_facets,
        "integrity": {"complete": True, "face_count": len(membership),
                      "shell_count": len(shell_ids), "face_ids": face_ids},
    }


def _execute_mutation(state, base, mesh, calc, constants, prepared, artifact_dir, output_path,
                      facts, mark_mutation):
    recipe = prepared["recipe"]
    params = prepared["clean"]
    deck = constants.NASTRAN
    context = prepared["context"]
    tol = max(recipe["geometry_tolerance"], 1e-7)
    cx, cy, cz = params["center"]
    facts["native_calls"] = []

    def call(label, function, *args, **kwargs):
        facts["native_calls"].append({"name": label, "attempted": True, "replayed": False})
        model_writes = {"MapBlock", "NumberPerimeters", "Create4SidedMesh", "DefineCaps",
                        "FreezeElements", "VolumesRemesh"}
        return _native_call_once(
            state, deck, context, function, *args,
            before_call=mark_mutation if label in model_writes else None,
            **kwargs,
        )

    for axis, offsets in ((0, recipe["x_cuts"]), (1, recipe["y_cuts"])):
        for coordinate in offsets:
            point = [cx, cy, cz]
            point[axis] = coordinate
            second = [point[0], point[1], point[2] + 1.0]
            third = point.copy()
            third[1 - axis] += 1.0
            faces = _collect(base, deck, "FACE")
            facts["native_calls"].append({"name": "PlaneCut", "attempted": True, "replayed": False})
            _plane_cut(state, base, deck, context, point, second, third, faces, mark_mutation)
            facts["native_calls"][-1]["returned"] = 1
            _write_stage(artifact_dir, facts, f"cad_axis_cut_{axis}_{coordinate:g}", base, deck)

    for direction in recipe["diagonal_directions"]:
        point = [cx, cy, cz]
        second = [cx, cy, cz + 1.0]
        third = [cx + direction[0], cy + direction[1], cz]
        central = _central_faces(base, deck, params, tol)
        if not central:
            raise ValueError("no central transition faces are available for the two diagonal cuts")
        facts["native_calls"].append({"name": "PlaneCut", "attempted": True, "replayed": False})
        _plane_cut(state, base, deck, context, point, second, third, central, mark_mutation)
        facts["native_calls"][-1]["returned"] = 1
        _write_stage(artifact_dir, facts, "cad_diagonal_cut", base, deck)

    faces = _collect(base, deck, "FACE")
    if len(faces) != recipe["faces"]:
        raise ValueError("native CAD partition did not produce the planned 92 FACEs")
    for kind in ("GRID", "SHELL", "SOLID", "VOLUME"):
        if _collect(base, deck, kind):
            raise RuntimeError(f"CAD partition unexpectedly produced {kind}")
    facts["post_partition_face_ids"] = _ids(faces)
    _write_stage(artifact_dir, facts, "cad_partitioned", base, deck)
    facts["checkpoints"]["partition"] = _save_copy(
        base, constants, artifact_dir / "01-plate-partitioned.ansa"
    )

    settings_rc = call("BCSettingsSetValues", base.BCSettingsSetValues,
                       {"mesh_change_option": "mesh", "element_type": "quad",
                        "element_order": "1st Order"})
    if type(settings_rc) is not int or settings_rc != 0:
        raise RuntimeError(f"native BCSettingsSetValues returned {settings_rc!r}")
    facts["native_calls"][-1]["returned"] = settings_rc
    target_rc = call("SetMeshParamTargetLength", mesh.SetMeshParamTargetLength,
                     "absolute", params["target_length"])
    if type(target_rc) is not int or target_rc != 1:
        raise RuntimeError(f"native SetMeshParamTargetLength returned {target_rc!r}")
    facts["native_calls"][-1]["returned"] = target_rc

    map_volumes = call("MapBlock", mesh.MapBlock, faces)
    facts["native_calls"][-1]["returned_count"] = len(map_volumes) if type(map_volumes) in (list, tuple) else None
    if type(map_volumes) not in (list, tuple) or len(map_volumes) != recipe["block_count"]:
        raise ValueError("native MapBlock did not create exactly twenty bounded volumes")
    volumes = sorted(map_volumes, key=lambda entity: entity._id)
    volume_ids = [catalog._entity_reference(base, deck, v, "VOLUME")["id"] for v in volumes]
    if len(set(volume_ids)) != recipe["block_count"]:
        raise RuntimeError("native MapBlock returned duplicate volume identities")
    face_owners, caps_before, _boundary_faces = _volume_boundaries(base, mesh, deck, volumes)
    if set(map(int, face_owners)) != set(_ids(faces)) or sum(map(len, face_owners.values())) != 120:
        raise ValueError("twenty native MapBlocks do not cover all 92 FACEs and 120 face incidences")
    if sum(len(owners) == 2 for owners in face_owners.values()) != 28:
        raise ValueError("initial MapBlock topology does not contain the planned 28 shared FACEs")
    facts.update(volume_ids=volume_ids, initial_face_owners=face_owners,
                 initial_caps=caps_before)
    _write_stage(artifact_dir, facts, "mapblock_created", base, deck)

    groups, seed_rows = _classify_cons_edges(base, deck, params)
    facts["seeds"] = seed_rows
    facts["seed_group_counts"] = {str(key): len(value) for key, value in sorted(groups.items())}
    _write_json(artifact_dir / "cons-seeds.json", {"complete": True, "rows": seed_rows,
                                                       "group_counts": facts["seed_group_counts"]})
    for count, entities in sorted(groups.items()):
        returned = call("NumberPerimeters", mesh.NumberPerimeters, entities, str(count),
                         apply_multiple_number=False, remesh_macros=False, apply_number_of="edges",
                         use_ansa_defaults_values=False)
        facts["native_calls"][-1]["entity_count"] = len(entities)
        facts["native_calls"][-1]["segments"] = count
        if type(returned) is not int or returned != 1:
            raise RuntimeError(f"native NumberPerimeters({count}) returned {returned!r}")
        facts["native_calls"][-1]["returned"] = returned
    _write_stage(artifact_dir, facts, "compatible_integer_seeds", base, deck)

    mapped = call(
        "Create4SidedMesh", mesh.Create4SidedMesh, faces,
        only_4sided=True, only_aligned=True, corner_angle=60.0,
        smooth_type="isoparametric", split_method="none", spacing="off", elem_type="quad",
        quad_pattern="directional", align_hots="off", only_unmeshed=False,
        protect_selection_bounds=True,
    )
    meshed = getattr(mapped, "meshed_ents", None)
    if type(meshed) not in (list, tuple) or sorted(_ids(meshed)) != _ids(faces):
        raise RuntimeError("native 4-sided remesher did not report all 92 FACEs")
    surface = _surface_capture(base, deck)
    if surface["shell_count"] != recipe["predicted_boundary_shells"]:
        raise ValueError("native 4-sided remesh SHELL count differs from the recipe")
    if any(row["type"] != "CQUAD4" for row in surface["elements"]):
        raise ValueError("all 92 partition FACEs must be remeshed to CQUAD4")
    membership, _ = structured_boundary.capture_membership(base, mesh, constants, deck, _ids(faces))
    if (len(membership) != recipe["faces"]
            or any(row.get("membership_complete") is not True for row in membership)):
        raise ValueError("native FACE-to-SHELL membership is incomplete after remeshing")
    member_shell_ids = {shell["id"] for face in membership for shell in face["shells"]}
    if member_shell_ids != set(surface["shell_ids"]):
        raise ValueError("92-FACE native membership does not cover every remeshed SHELL")
    facts["surface_before_freeze_sha256"] = _digest(surface)
    facts["surface_shell_count"] = surface["shell_count"]
    facts["surface_node_count"] = surface["node_count"]
    _write_stage(artifact_dir, facts, "all_faces_mapped", base, deck)
    facts["checkpoints"]["mapped_surface"] = _save_copy(
        base, constants, artifact_dir / "02-plate-surface-mapped.ansa"
    )

    requested_caps = _assign_axial_caps(state, base, mesh, deck, context, volumes, params, tol)
    caps = _volume_boundaries(base, mesh, deck, volumes)[1]
    for volume_id, requested in requested_caps.items():
        if (caps.get(volume_id, {}).get("master") != requested["master"]
                or caps.get(volume_id, {}).get("slave") != requested["slave"]):
            raise RuntimeError(f"VOLUME {volume_id} cap groups differ from explicit DefineCaps request")
    facts["caps_before_freeze"] = caps
    before_frozen, errors = native._read_source_frozen(base, deck, surface["shell_ids"])
    if errors or set(before_frozen) != {str(value) for value in surface["shell_ids"]}:
        raise RuntimeError("source SHELL FROZEN state could not be completely captured before FreezeElements")
    if any(value != "NO" for value in before_frozen.values()):
        raise ValueError("newly mapped source SHELLs must be unfrozen before the bounded FreezeElements call")
    freeze_rc = call("FreezeElements", base.FreezeElements, deck, "0", _collect(base, deck, "SHELL"))
    if type(freeze_rc) is not bool or freeze_rc is not True:
        raise RuntimeError(f"native FreezeElements returned {freeze_rc!r}")
    after_surface = _surface_capture(base, deck)
    after_frozen, errors = native._read_source_frozen(base, deck, after_surface["shell_ids"])
    if errors or after_surface != surface:
        raise RuntimeError("FreezeElements changed the full source SHELL/GRID snapshot")
    if any(value != "YES" for value in after_frozen.values()):
        raise RuntimeError("FreezeElements did not set every source SHELL FROZEN=YES")
    facts["freeze_return"] = freeze_rc
    facts["frozen_shell_count"] = len(after_frozen)
    facts["surface_after_freeze_sha256"] = _digest(after_surface)
    facts["source_frozen"] = after_frozen
    _write_stage(artifact_dir, facts, "surface_frozen", base, deck)
    facts["checkpoints"]["frozen_surface"] = _save_copy(
        base, constants, artifact_dir / "03-plate-ready-frozen.ansa"
    )

    context = _rebind_after_open(
        state, base, deck, context, artifact_dir / "03-plate-ready-frozen.ansa"
    )
    facts["context_after_checkpoint_reopen"] = context
    volumes = _collect(base, deck, "VOLUME")
    faces = _collect(base, deck, "FACE")
    reread_surface = _surface_capture(base, deck)
    reread_frozen, errors = native._read_source_frozen(base, deck, reread_surface["shell_ids"])
    if errors or reread_surface != surface or reread_frozen != after_frozen:
        raise RuntimeError("saved frozen checkpoint did not preserve source SHELLs and FROZEN fields")
    current_caps = _volume_boundaries(base, mesh, deck, volumes)[1]
    if current_caps != caps:
        raise RuntimeError("saved frozen checkpoint did not preserve explicit axial caps")
    cad_before_fill, _ = _cad_sha256(state, base, deck)
    facts["cad_before_fill_sha256"] = cad_before_fill

    fill_rc = call("VolumesRemesh", mesh.VolumesRemesh, volumes)
    facts["volumes_remesh_call_count"] = 1
    if type(fill_rc) is not int or fill_rc != 1:
        raise RuntimeError(f"single native VolumesRemesh call returned {fill_rc!r}")
    facts["native_calls"][-1]["returned"] = fill_rc
    face_owners, caps_after_fill, _ = _volume_boundaries(base, mesh, deck, volumes)
    if caps_after_fill != caps:
        raise RuntimeError("native VolumesRemesh changed the explicit MapBlock caps")
    capture, owner_map = _capture_complete_fe(
        base, constants, deck, volumes, recipe["predicted_solids"], recipe["predicted_nodes"]
    )
    if any(row["type"] != "CHEXA" for row in capture["elements"]):
        raise ValueError("bounded O-grid fill must produce CHEXA only")
    surface_after_fill = _surface_capture(base, deck)
    if surface_after_fill != surface:
        raise RuntimeError("VolumesRemesh changed the frozen source surface mesh")
    frozen_after_fill, errors = native._read_source_frozen(base, deck, surface["shell_ids"])
    if errors or frozen_after_fill != after_frozen:
        raise RuntimeError("VolumesRemesh changed or obscured source SHELL FROZEN fields")
    cad_after_fill, _ = _cad_sha256(state, base, deck)
    if cad_after_fill != cad_before_fill:
        raise RuntimeError("normalized CAD IGES changed between pre-fill and post-fill readback")
    positive = _positive_volumes(capture)
    if positive["complete"] is not True:
        raise ValueError("one or more CHEXA have non-positive or degenerate signed volume")
    facts.update(
        solid_element_types=capture["integrity"]["element_types"],
        solid_count=len(capture["elements"]), grid_count=len(capture["nodes"]),
        source_shell_count=len(surface_after_fill["elements"]),
        positive_volume=positive,
        cad_after_fill_sha256=cad_after_fill,
        solid_snapshot_sha256=_digest(capture),
        surface_after_fill_sha256=_digest(surface_after_fill),
        volume_ownership=owner_map,
    )
    _write_json(artifact_dir / "native-solid-capture.json", capture)
    _write_json(artifact_dir / "source-surface-capture.json", surface_after_fill)
    _write_json(artifact_dir / "positive-volume-audit.json", positive)
    _write_stage(artifact_dir, facts, "solid_fill_verified", base, deck)
    facts["in_memory_output"] = {
        "capture_sha256": _digest(capture),
        "surface_sha256": _digest(surface_after_fill),
        "cad_sha256": cad_after_fill,
        "caps": caps_after_fill,
        "frozen": frozen_after_fill,
        "positive_volume": positive,
    }
    return {"context": context, "capture": capture, "surface": surface_after_fill,
            "cad_sha256": cad_after_fill, "caps": caps, "frozen": frozen_after_fill,
            "positive_volume": positive, "volumes": volumes,
            "face_owners": face_owners}


def execute(state, base, mesh, constants, params, *, backup_path, output_path,
            expected_plan_sha256):
    """一次性执行受限原生孔板流程；失败回执保留阶段和恢复证据，不自动重试。"""
    state.assert_main()
    if type(expected_plan_sha256) is not str or _SHA256_RE.fullmatch(expected_plan_sha256) is None:
        raise ValueError("expected_plan_sha256 must be 64 lowercase hexadecimal characters")
    prepared = _prepare_preflight(state, base, mesh, constants, params)
    if prepared["plan_sha256"] != expected_plan_sha256:
        raise ValueError("model-bound expected_plan_sha256 differs from current preflight")
    backup_target, output_target = _validate_output_paths(base, backup_path, output_path)
    from ansa import calc

    backup_args = native._backup_preflight(base, {**prepared["clean"], "backup_path": str(backup_target)})

    artifact_dir = _new_artifact_directory(state)
    facts = {
        "schema": "ansa-plate-hole-workflow-evidence",
        "version": 1,
        "stage": "backup_pending",
        "plan_sha256": prepared["plan_sha256"],
        "recipe_sha256": prepared["recipe"]["recipe_sha256"],
        "source_face_sha256": prepared["source_face_sha256"],
        "input_snapshot": prepared["input_snapshot"],
        "expected_scope": "NASTRAN millimeter axis-aligned, one-Part, eight-FACE, single centered circular through-hole raw CAD plate",
        "native_calls": [],
        "checkpoints": {},
        "recovery_required": False,
        "replayed_native_calls": False,
    }
    paths = {
        "plan": _write_json(artifact_dir / "plan.json", prepared["recipe"]),
        "preflight": _write_json(artifact_dir / "preflight.json", {
            "plan_sha256": prepared["plan_sha256"], "context": prepared["context"],
            "input_snapshot": prepared["input_snapshot"],
            "meshing_context_sha256": prepared["settings"].snapshot_sha256,
        }),
        "input_scope": _write_json(artifact_dir / "original-input-scope.json", prepared["scope"]),
        "original_material_property_definitions": _write_json(
            artifact_dir / "original-material-property-definitions.json", prepared["definitions"]
        ),
        "original_part_definition": _write_json(
            artifact_dir / "original-part-definition.json", prepared["part_row"]
        ),
    }
    facts["artifacts"] = paths
    operation_result = {"ok": False, "accepted": False, "quality_accepted": False,
                        "recovery_required": False, "stage": "backup_pending"}
    backup_attempted = False
    mutation_attempted = False
    settings_result = {}
    try:
        backup_attempted = True
        backup_record = native._verify_backup(base, constants, *backup_args)
        facts["backup"] = backup_record
        _write_stage(artifact_dir, facts, "backup_verified", base, constants.NASTRAN)

        refreshed = _prepare_preflight(state, base, mesh, constants, prepared["clean"])
        if refreshed["plan_sha256"] != prepared["plan_sha256"]:
            raise ValueError("model, source CAD, configuration or settings changed after backup; no CAD write was made")
        prepared = refreshed

        def mutate():
            return _execute_mutation(
                state, base, mesh, calc, constants, prepared, artifact_dir, output_target,
                facts, lambda: _mark_mutation_attempted(),
            )

        def _mark_mutation_attempted():
            nonlocal mutation_attempted
            mutation_attempted = True

        result = meshing_context.run_with_context(
            state, base, mesh, prepared["clean"]["target_length"], mutate, settings_result,
            expected_snapshot_sha256=prepared["settings"].snapshot_sha256,
        )
        if settings_result.get("restored") is not True:
            raise RuntimeError("native meshing context did not confirm full parameter restoration")

        facts["meshing_context"] = settings_result
        facts["checkpoints"]["output"] = _save_copy(base, constants, output_target)
        _write_stage(artifact_dir, facts, "output_saved_before_reopen", base, constants.NASTRAN)
        context = _rebind_after_open(state, base, constants.NASTRAN, result["context"], output_target)
        saved_settings = meshing_context._capture_context(state, base, mesh)
        if saved_settings.snapshot_sha256 != prepared["settings"].snapshot_sha256:
            raise RuntimeError("saved/reopened output changed the restored meshing configuration")
        final_volumes = _collect(base, constants.NASTRAN, "VOLUME")
        final_surface = _surface_capture(base, constants.NASTRAN)
        final_capture, final_owners = _capture_complete_fe(
            base, constants, constants.NASTRAN, final_volumes,
            prepared["recipe"]["predicted_solids"], prepared["recipe"]["predicted_nodes"],
        )
        final_frozen, freeze_errors = native._read_source_frozen(
            base, constants.NASTRAN, final_surface["shell_ids"]
        )
        if freeze_errors or final_frozen != result["frozen"]:
            raise RuntimeError("saved/reopened output did not preserve complete source FROZEN fields")
        if _digest(final_surface) != _digest(result["surface"]):
            raise RuntimeError("saved/reopened output changed source SHELL/GRID identities or connectivity")
        if _digest(final_capture) != _digest(result["capture"]):
            raise RuntimeError("saved/reopened output changed complete SOLID/GRID capture")
        final_face_owners, final_caps, _ = _volume_boundaries(
            base, mesh, constants.NASTRAN, final_volumes
        )
        if final_caps != result["caps"] or final_face_owners != result["face_owners"]:
            raise RuntimeError("saved/reopened output changed native VOLUME FACE ownership or caps")
        final_cad_sha256, _ = _cad_sha256(state, base, constants.NASTRAN)
        if final_cad_sha256 != result["cad_sha256"]:
            raise RuntimeError("normalized CAD IGES changed after final save/reopen")

        part_id = prepared["input_snapshot"]["part_id"]
        current_part, current_definitions = _definition_snapshot(
            base, constants.NASTRAN, part_id,
            {kind: [row["entity"]["id"] for row in rows]
             for kind, rows in prepared["definitions"].items()},
        )
        definition_audit = _compare_definitions(
            prepared["part_row"], prepared["definitions"], current_part, current_definitions
        )
        quality = _quality(base, _collect(base, constants.NASTRAN, "SOLID"))
        if any(row["checked"] != prepared["recipe"]["predicted_solids"]
               or row["unknown"] != 0 or row["failed_count"] != 0 for row in quality.values()):
            raise ValueError("native ASPECT/JACOB quality acceptance did not pass for every CHEXA")
        positive = _positive_volumes(final_capture)
        if positive["complete"] is not True:
            raise ValueError("saved/reopened output contains non-positive or degenerate CHEXA")

        boundary = _audit_output(
            state, base, mesh, calc, constants, constants.NASTRAN,
            prepared["recipe"], final_volumes, final_face_owners, final_capture,
            final_surface, result["caps"],
        )
        cad_report = {
            "integrity": {"complete": True, "face_count": boundary["integrity"]["face_count"],
                          "shell_count": boundary["integrity"]["shell_count"],
                          "projection_pairs": boundary["projection"]["requested_node_face_pairs"]},
            "membership_status": boundary["membership"]["status"],
            "projection_status": boundary["projection"]["status"],
            "topology_status": boundary["audit"]["topology_status"],
            "geometry_status": boundary["audit"]["geometry_status"],
            "outer_shell_count": boundary["outer_shell_count"],
            "shared_interface_count": len(boundary["interfaces"]),
        }
        if not all(cad_report[key] == "pass" for key in
                   ("membership_status", "projection_status", "topology_status", "geometry_status")):
            raise ValueError("saved/reopened CAD coverage, interface, projection or geometry audit did not pass")

        _write_json(artifact_dir / "native-solid-capture-reopened.json", final_capture)
        _write_json(artifact_dir / "source-surface-capture-reopened.json", final_surface)
        _write_json(artifact_dir / "boundary-audit-reopened.json", boundary)
        _write_json(artifact_dir / "positive-volume-audit-reopened.json", positive)
        _write_json(artifact_dir / "quality-audit.json", quality)
        _write_json(artifact_dir / "definition-recheck.json", {
            "audit": definition_audit, "original": prepared["definitions"],
            "reopened": current_definitions,
        })
        facts.update(
            stage="verified_refined_mesh",
            context_after_output_reopen=context,
            reopened_capture_sha256=_digest(final_capture),
            reopened_surface_sha256=_digest(final_surface),
            reopened_cad_sha256=final_cad_sha256,
            reopened_caps=final_caps,
            definitions=definition_audit,
            quality=quality,
            positive_volume=positive,
            boundary_summary=cad_report,
            meshing_context_restored=settings_result.get("restored") is True,
            recovery_required=False,
        )
        _write_stage(artifact_dir, facts, "verified_refined_mesh", base, constants.NASTRAN)
        operation_result = {
            "ok": True,
            "accepted": True,
            "quality_accepted": True,
            "recovery_required": False,
            "stage": "verified_refined_mesh",
            "plan_sha256": prepared["plan_sha256"],
            "recipe_sha256": prepared["recipe"]["recipe_sha256"],
            "backup": backup_record,
            "output_path": str(output_target),
            "evidence_path": str(artifact_dir),
            "evidence": {key: value["path"] for key, value in paths.items()},
            "counts": {
                "blocks": prepared["recipe"]["block_count"],
                "faces": boundary["integrity"]["face_count"],
                "source_quads": prepared["recipe"]["predicted_source_quads"],
                "shells": boundary["integrity"]["shell_count"],
                "outer_shells": boundary["outer_shell_count"],
                "shared_interfaces": len(boundary["interfaces"]),
                "solids": len(final_capture["elements"]),
                "grids": len(final_capture["nodes"]),
            },
            "integrity_complete": final_capture["integrity"]["complete"],
            "cad": cad_report,
            "positive_volume": {key: value for key, value in positive.items()
                                if key != "nonpositive_or_degenerate"},
            "quality": {name: {key: value for key, value in row.items() if key != "failed_ids"}
                        for name, row in quality.items()},
            "definitions": definition_audit,
            "meshing_context_restored": settings_result.get("restored") is True,
            "volumes_remesh_call_count": facts.get("volumes_remesh_call_count"),
            "replayed_native_calls": False,
        }
    except Exception as exc:
        recovery_required = (
            backup_attempted or mutation_attempted or settings_result.get("restored") is False
        )
        facts.update(
            stage=facts.get("stage", "backup_pending"),
            error={"type": type(exc).__name__, "message": str(exc)[:4096]},
            recovery_required=recovery_required,
            replayed_native_calls=False,
            meshing_context={
                "restored": settings_result.get("restored"),
                "restore_errors": settings_result.get("restore_errors", []),
            },
        )
        partial_path = artifact_dir / "partial-after-failure.ansa"
        if mutation_attempted:
            try:
                facts["partial_model"] = _save_copy(base, constants, partial_path)
            except Exception as save_error:
                facts["partial_model_save_error"] = {
                    "type": type(save_error).__name__, "message": str(save_error)[:2048]
                }
        try:
            _write_stage(artifact_dir, facts, "attempt_stopped", base, constants.NASTRAN)
        except Exception as evidence_error:
            facts["evidence_write_error"] = {
                "type": type(evidence_error).__name__, "message": str(evidence_error)[:2048]
            }
        operation_result = {
            "ok": False,
            "accepted": False,
            "quality_accepted": False,
            "recovery_required": recovery_required,
            "stage": facts.get("stage"),
            "plan_sha256": prepared["plan_sha256"],
            "backup_path": str(backup_target),
            "output_path": str(output_target),
            "evidence_path": str(artifact_dir),
            "error": facts.get("error"),
            "partial_model_path": facts.get("partial_model", {}).get("path"),
            "volumes_remesh_call_count": facts.get("volumes_remesh_call_count", 0),
            "replayed_native_calls": False,
        }
    facts["result"] = operation_result
    try:
        _write_json(artifact_dir / "result.json", operation_result)
    except Exception as exc:
        operation_result["evidence_write_error"] = {"type": type(exc).__name__, "message": str(exc)[:1024]}
    return operation_result


__all__ = ["execute", "plan", "preflight"]
