"""Read-only native CAD membership and complete associated-node measurements.

No tolerance is used to select the members under test. Macro membership is only
attributed to an individual FACE when its macro contains exactly that FACE.
Nodal projection is explicitly distinct from continuous CAD boundary coverage.
"""

import math

from . import entity_catalog as catalog, inspection, structured_native as native


MAX_SHELLS = 5000
MAX_PROJECTIONS = 5000


def normalize_request(value):
    inspection._params(value, ("face_ids", "tolerance", "cylinders"))
    ids = native._id_list(value["face_ids"], "cad_boundary.face_ids", 100)
    tolerance = native._finite(value["tolerance"], "cad_boundary.tolerance")
    if tolerance <= 0:
        raise ValueError("cad_boundary.tolerance must be positive")
    cylinders = value["cylinders"]
    if type(cylinders) is not list or len(cylinders) > 100:
        raise ValueError("cad_boundary.cylinders must contain at most 100 entries")
    labels = set()
    normalized = []
    for cylinder in cylinders:
        inspection._params(cylinder, ("label", "face_ids", "center", "axis", "radius", "tolerance"))
        label = cylinder["label"]
        if type(label) is not str or not 1 <= len(label) <= 128 or label in labels:
            raise ValueError("CAD cylinder labels must be distinct nonempty strings")
        labels.add(label)
        faces = native._id_list(cylinder["face_ids"], "CAD cylinder face_ids", 100)
        if not set(faces) <= set(ids):
            raise ValueError("CAD cylinder faces must be included in cad_boundary.face_ids")
        radius = native._finite(cylinder["radius"], "CAD cylinder radius")
        limit = native._finite(cylinder["tolerance"], "CAD cylinder tolerance")
        if radius <= 0 or limit < 0:
            raise ValueError("CAD cylinder radius must be positive and tolerance nonnegative")
        normalized.append({"label": label, "face_ids": faces,
            "center": native._point(cylinder["center"], "CAD cylinder center"),
            "axis": native._unit_vector(cylinder["axis"], "CAD cylinder axis"),
            "radius": radius, "tolerance": limit})
    return {"face_ids": ids, "tolerance": tolerance, "cylinders": normalized}


def _references(base, deck, entities, kind, label):
    if type(entities) not in (list, tuple):
        raise RuntimeError(label + " did not return an entity list")
    rows = [catalog._entity_reference(base, deck, entity, kind) for entity in entities]
    ids = [row["id"] for row in rows]
    if any(row["type"] != kind for row in rows) or len(ids) != len(set(ids)):
        raise RuntimeError(label + " returned duplicate or incorrectly typed entities")
    return sorted(ids)


def capture_membership(base, mesh, constants, deck, face_ids):
    """Capture full native macro members before testing any coordinates."""
    native._check_nastran(constants, deck)
    face_ids = native._id_list(face_ids, "CAD face_ids", 100)
    records, entities, shell_total = [], {}, 0
    macro_cache, expanded_faces = {}, 0
    for face_id in sorted(face_ids):
        face, ref = catalog._get_entity(base, deck, "FACE", face_id)
        entities[face_id] = face
        macro = base.GetFaceMacro(face)
        if macro is None:
            raise RuntimeError(f"FACE {face_id} has no readable macro")
        macro_ref = catalog._entity_reference(base, deck, macro, "FACE")
        if macro_ref["id"] not in macro_cache:
            raw_faces = base.GetFacesOfMacros([macro])
            if type(raw_faces) not in (list, tuple):
                raise RuntimeError("GetFacesOfMacros did not return an entity list")
            if expanded_faces + len(raw_faces) > 100:
                raise ValueError("CAD macro expansion exceeds 100 FACE references")
            expanded_faces += len(raw_faces)
            macro_faces = _references(base, deck, raw_faces, "FACE", "GetFacesOfMacros")
            shells = mesh.GetMacroShells([macro])
            if type(shells) is int and shells == 0:
                shells = []
            if type(shells) in (list, tuple) and len(shells) > MAX_SHELLS:
                raise ValueError(f"CAD membership exceeds {MAX_SHELLS} SHELL occurrences")
            shell_ids = _references(base, deck, shells, "SHELL", "GetMacroShells")
            macro_cache[macro_ref["id"]] = (macro_faces, shell_ids)
        macro_faces, shell_ids = macro_cache[macro_ref["id"]]
        if face_id not in macro_faces or macro_ref["id"] not in macro_faces:
            raise RuntimeError("CAD FACE and macro membership identity could not be verified")
        shell_total += len(shell_ids)
        if shell_total > MAX_SHELLS:
            raise ValueError(f"CAD membership exceeds {MAX_SHELLS} SHELL occurrences")
        labels = tuple(face.card_fields(deck))
        # NASTRAN FACE cards in 2026.1 do not expose Num.Quads/Num.Trias.
        # GetMacroShells is documented to return *all* members, with no paging.
        declared = len(shell_ids)
        count_origin = "complete_GetMacroShells_return"
        if {"Num.Quads", "Num.Trias"} <= set(labels):
            declared = None
            count_origin = "FACE_Num_Quads_plus_Num_Trias"
            values = catalog._read_card(base, deck, face, ref, ("Num.Quads", "Num.Trias"))
            counts = [values[key] for key in ("Num.Quads", "Num.Trias")]
            if all(type(x) in (int, float) and math.isfinite(x) and x >= 0 and int(x) == x for x in counts):
                declared = sum(int(x) for x in counts)
        area = native._finite(base.GetFaceArea(face), f"FACE {face_id} CAD area")
        if area <= 0:
            raise RuntimeError(f"FACE {face_id} CAD area is unavailable or nonpositive")
        unique_face = macro_faces == [face_id]
        shell_rows = native._read_elements(base, deck, "SHELL", shell_ids, {"CQUAD4", "CTRIA3"}) if unique_face else []
        records.append({"face_id": face_id, "macro_face_ids": macro_faces,
            "macro_id": macro_ref["id"], "macro_shell_ids": shell_ids,
            "shells": shell_rows, "declared_shell_count": declared,
            "shell_count_origin": count_origin,
            "cad_area": area,
            "area_is_continuous_CAD_certificate": False,
            "membership_complete": unique_face and declared is not None and declared == len(shell_ids) and bool(shell_ids),
            "membership_origin": "native_GetMacroShells_single_FACE" if unique_face else "ambiguous_multi_FACE_macro"})
    return records, entities


def measure_nodes(base, calc, deck, snapshot, records, face_entities, tolerance):
    """Project every associated node to its independently assigned CAD FACE."""
    pairs = sorted({(row["face_id"], node) for row in records for shell in row["shells"] for node in shell["nodes"]})
    result = {"status": "incomplete", "scope": "all_native_FACE_associated_nodes",
        "continuous_surface_coverage_proven": False, "rows": [],
        "requested_node_face_pairs": len(pairs), "measured_node_face_pairs": 0,
        "tolerance": tolerance, "maximum_distance": None, "diagnostics": []}
    if len(pairs) > MAX_PROJECTIONS:
        result["diagnostics"].append({"code": "projection_budget_exceeded", "limit": MAX_PROJECTIONS})
        return result
    coordinates = {row["id"]: row["xyz"] for row in snapshot["nodes"]}
    missing = sorted({node for _face, node in pairs} - set(coordinates))
    for row in native._read_grids(base, deck, missing):
        coordinates[row["id"]] = row["xyz"]
    for face_id, node_id in pairs:
        point = coordinates[node_id]
        raw = calc.ProjectPointToContainer(point, [face_entities[face_id]])
        if type(raw) not in (list, tuple) or len(raw) != 5 or raw[4] is None:
            result["rows"].append({"face_id": face_id, "node_id": node_id, "status": "incomplete", "reason": "projection_unavailable"})
            continue
        projected = native._point(list(raw[0]), "CAD projected point")
        distance = native._finite(raw[1], "CAD projection distance")
        if distance < 0:
            raise RuntimeError("CAD projection returned a negative distance")
        found = catalog._entity_reference(base, deck, raw[4], "FACE")
        if found.get("type") != "FACE" or found.get("id") != face_id:
            raise RuntimeError("CAD projection returned an entity outside the assigned FACE")
        actual = math.dist(point, projected)
        if not math.isclose(actual, distance, rel_tol=1e-7, abs_tol=1e-9):
            raise RuntimeError("CAD projection coordinates and reported distance disagree")
        result["rows"].append({"face_id": face_id, "node_id": node_id,
            "position": point, "projected_point": projected, "distance": distance,
            "within_tolerance": distance <= tolerance, "status": "pass" if distance <= tolerance else "fail"})
    measured = [row for row in result["rows"] if "distance" in row]
    result["measured_node_face_pairs"] = len(measured)
    result["maximum_distance"] = max((row["distance"] for row in measured), default=None)
    result["status"] = ("fail" if any(row["status"] == "fail" for row in result["rows"]) else
                        "pass" if pairs and len(measured) == len(pairs) and all(row["membership_complete"] for row in records) else "incomplete")
    return result


def capture(base, mesh, calc, constants, deck, snapshot, request):
    from .mesh_boundary import audit_boundary_membership
    request = normalize_request(request)
    records, face_entities = capture_membership(base, mesh, constants, deck, request["face_ids"])
    membership = audit_boundary_membership(snapshot, records)
    known_nodes = {row["id"] for row in snapshot["nodes"]}
    missing_nodes = sorted({node for row in records for shell in row["shells"] for node in shell["nodes"]} - known_nodes)
    augmented = {**snapshot, "nodes": snapshot["nodes"] + native._read_grids(base, deck, missing_nodes)}
    projection = measure_nodes(base, calc, deck, augmented, records, face_entities, request["tolerance"])
    by_face = {row["face_id"]: row for row in records}
    face_statuses = {row["face_id"]: row["status"] for row in membership["faces"]}
    cylinders = []
    cylinder_membership = []
    for cylinder in request["cylinders"]:
        facets = list({tuple(sorted(shell["nodes"])): shell["nodes"]
            for face_id in cylinder["face_ids"] for shell in by_face[face_id]["shells"]}.values())
        cylinders.append({key: value for key, value in cylinder.items() if key != "face_ids"} | {"facets": facets})
        statuses = [face_statuses[face_id] for face_id in cylinder["face_ids"]]
        cylinder_membership.append({"label": cylinder["label"], "face_ids": cylinder["face_ids"],
            "status": "fail" if "fail" in statuses else "incomplete" if "incomplete" in statuses else "pass"})
    return {"membership": membership, "projection": projection, "native_faces": records,
        "protected_cylinders": cylinders, "expected_boundary": membership["expected_boundary"],
        "membership_origin": "ANSA_native_macro_SHELL_association",
        "cylinder_membership": cylinder_membership,
        "audit_snapshot": augmented,
        "continuous_cad_coverage_status": "incomplete",
        "cad_to_FE_continuous_coverage_proven": False}
