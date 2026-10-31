"""Read-only, topology-limited CAD sweep candidate diagnostics.

The module consumes a schema-2 ``geometry_features`` report.  It never reads
or mutates ANSA state; accepted records are corner-topology proposals only.
"""

import copy
from fractions import Fraction
import hashlib
from itertools import combinations
import json
import math

from .cad_sweep_topology import plan_translation


_SOURCE_FIELDS = (
    "feature_schema", "source_scope", "feature_context", "parts",
    "face_boundaries", "boundaries", "vertices", "curve_lengths",
    "cad_export_sha256",
)
_MAX_FACES = 100
_MAX_CONS = 500
_MAX_HOT_POINTS = 1000
_TOPOLOGY_TOLERANCE = 1e-7
_FALSE_PROOFS = {
    "usable_for_mutation": False,
    "exact_native_CAD_equivalence_proven": False,
    "continuous_export_domain_verified": False,
}


def _object(value, label):
    if type(value) is not dict:
        raise ValueError("%s must be an object" % label)
    return value


def _list(value, label, maximum=None):
    if type(value) is not list:
        raise ValueError("%s must be a list" % label)
    if maximum is not None and len(value) > maximum:
        raise ValueError("%s exceeds the supported limit of %d" % (label, maximum))
    return value


def _identifier(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError("%s must be a positive integer, not bool" % label)
    return value


def _identifier_list(value, label, maximum=None):
    values = _list(value, label, maximum)
    result = [_identifier(item, label) for item in values]
    if len(result) != len(set(result)):
        raise ValueError("%s contains duplicate IDs" % label)
    return result


def _finite_number(value, label, positive=False):
    if type(value) not in (int, float):
        raise ValueError("%s must be a finite number, not bool" % label)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("%s must be finite" % label) from exc
    if not math.isfinite(number):
        raise ValueError("%s must be finite" % label)
    if positive and number <= 0.0:
        raise ValueError("%s must be positive" % label)
    return number


def _sha256_json(value, label):
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s must be finite standard JSON data" % label) from exc
    return hashlib.sha256(encoded).hexdigest()


def _check_digest(value, label):
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError("%s must be a lowercase SHA-256 digest" % label)
    return value


def _source_payload(witness):
    missing = [key for key in _SOURCE_FIELDS if key not in witness]
    if missing:
        raise ValueError("witness is missing source fields: %s" % ", ".join(missing))
    return {key: witness[key] for key in _SOURCE_FIELDS}


def _validate_witness(witness):
    witness = _object(witness, "witness")
    if witness.get("status", "captured") != "captured":
        raise ValueError("witness.status must be captured")
    if type(witness.get("feature_schema")) is not int or witness["feature_schema"] != 2:
        raise ValueError("witness.feature_schema must be 2")

    source_hash = _check_digest(
        witness.get("source_snapshot_sha256"), "witness.source_snapshot_sha256")
    cad_hash = _check_digest(witness.get("cad_export_sha256"),
                              "witness.cad_export_sha256")
    payload = _source_payload(witness)
    if _sha256_json(payload, "witness source payload") != source_hash:
        raise ValueError("witness.source_snapshot_sha256 does not match its source payload")

    source_scope = _object(witness.get("source_scope"), "witness.source_scope")
    face_ids = _identifier_list(source_scope.get("face_ids"),
                                "witness.source_scope.face_ids", _MAX_FACES)
    if not face_ids:
        raise ValueError("witness.source_scope.face_ids must not be empty")
    curve_ids = _list(source_scope.get("curve_ids"),
                      "witness.source_scope.curve_ids")
    if curve_ids:
        raise ValueError("candidate diagnostics require a FACE-only scope")
    scope_faces = set(face_ids)
    _object(witness.get("feature_context"), "witness.feature_context")

    raw_parts = _list(witness.get("parts"), "witness.parts", _MAX_FACES)
    face_parts = {}
    for index, raw_part in enumerate(raw_parts):
        label = "witness.parts[%d]" % index
        part = _object(raw_part, label)
        if part.get("type") != "FACE":
            raise ValueError("FACE-only scope contains a non-FACE Part assignment")
        face_id = _identifier(part.get("id"), label + ".id")
        part_id = _identifier(part.get("part_id"), label + ".part_id")
        if face_id not in scope_faces:
            raise ValueError("%s.id is outside source_scope.face_ids" % label)
        if face_id in face_parts:
            raise ValueError("duplicate FACE part assignment for %d" % face_id)
        face_parts[face_id] = part_id
    if set(face_parts) != scope_faces:
        raise ValueError("FACE Part assignments must cover the complete source scope")

    raw_faces = _list(witness.get("face_boundaries"),
                      "witness.face_boundaries", _MAX_FACES)
    face_cons = {}
    expected_cons_faces = {}
    for index, raw_face in enumerate(raw_faces):
        label = "witness.face_boundaries[%d]" % index
        face = _object(raw_face, label)
        face_id = _identifier(face.get("face_id"), label + ".face_id")
        if face_id not in scope_faces:
            raise ValueError("%s.face_id is outside source_scope.face_ids" % label)
        if face_id in face_cons:
            raise ValueError("duplicate FACE boundary for %d" % face_id)
        cons_ids = _identifier_list(face.get("cons_ids"), label + ".cons_ids",
                                    _MAX_CONS)
        if not cons_ids:
            raise ValueError("%s.cons_ids must not be empty" % label)
        face_cons[face_id] = cons_ids
        for cons_id in cons_ids:
            expected_cons_faces.setdefault(cons_id, set()).add(face_id)
    if set(face_cons) != scope_faces:
        raise ValueError("FACE boundaries must cover the complete source scope")
    if len(expected_cons_faces) > _MAX_CONS:
        raise ValueError("witness CONS count exceeds the supported limit of 500")

    raw_boundaries = _list(witness.get("boundaries"),
                           "witness.boundaries", _MAX_CONS)
    boundaries = {}
    for index, raw_boundary in enumerate(raw_boundaries):
        label = "witness.boundaries[%d]" % index
        boundary = _object(raw_boundary, label)
        cons_id = _identifier(boundary.get("id"), label + ".id")
        if cons_id in boundaries:
            raise ValueError("duplicate CONS boundary for %d" % cons_id)
        if cons_id not in expected_cons_faces:
            raise ValueError("CONS %d is outside the complete FACE boundary scope" % cons_id)
        adjacent_faces = _identifier_list(
            boundary.get("face_ids"), label + ".face_ids", _MAX_FACES)
        if not adjacent_faces:
            raise ValueError("%s.face_ids must not be empty" % label)
        selected_adjacencies = set(adjacent_faces).intersection(scope_faces)
        if selected_adjacencies != expected_cons_faces[cons_id]:
            raise ValueError(
                "CONS %d FACE adjacency is inconsistent with face_boundaries" % cons_id)
        endpoint = _object(boundary.get("endpoint_topology"),
                           label + ".endpoint_topology")
        endpoint_status = endpoint.get("status")
        if endpoint_status not in ("available", "incomplete"):
            raise ValueError("%s.status must be available or incomplete" %
                             (label + ".endpoint_topology"))
        hot_point_ids = _identifier_list(
            endpoint.get("hot_point_ids"),
            label + ".endpoint_topology.hot_point_ids", _MAX_HOT_POINTS)
        if endpoint_status == "available" and len(hot_point_ids) != 2:
            raise ValueError("available CONS endpoint topology must contain two HOT POINTs")
        if endpoint_status == "incomplete" and len(hot_point_ids) == 2:
            raise ValueError("incomplete CONS endpoint topology cannot contain exactly two HOT POINTs")
        boundaries[cons_id] = {
            "face_ids": adjacent_faces,
            "selected_face_ids": selected_adjacencies,
            "hot_point_ids": hot_point_ids,
            "endpoint_status": endpoint_status,
            "raw": boundary,
        }
    if set(boundaries) != set(expected_cons_faces):
        raise ValueError("CONS boundaries must cover the complete FACE scope")

    if type(witness.get("curve_lengths")) is not list or witness["curve_lengths"]:
        raise ValueError("FACE-only scope must have an empty curve_lengths list")

    raw_vertices = _list(witness.get("vertices"),
                         "witness.vertices", _MAX_HOT_POINTS)
    vertices = {}
    for index, raw_vertex in enumerate(raw_vertices):
        label = "witness.vertices[%d]" % index
        vertex = _object(raw_vertex, label)
        point_id = _identifier(vertex.get("id"), label + ".id")
        if point_id in vertices:
            raise ValueError("duplicate HOT POINT id %d" % point_id)
        position = vertex.get("position")
        if type(position) is not list or len(position) != 3:
            raise ValueError("%s.position must contain three coordinates" % label)
        xyz = tuple(_finite_number(value, "%s.position[%d]" % (label, axis))
                    for axis, value in enumerate(position))
        cons_ids = _identifier_list(vertex.get("cons_ids"), label + ".cons_ids", 100)
        vertices[point_id] = {"position": xyz, "cons_ids": set(cons_ids),
                              "raw": vertex}

    point_cons = {}
    for cons_id, boundary in boundaries.items():
        for point_id in boundary["hot_point_ids"]:
            if point_id not in vertices:
                raise ValueError("CONS %d references missing HOT POINT %d" %
                                 (cons_id, point_id))
            if cons_id not in vertices[point_id]["cons_ids"]:
                raise ValueError("HOT POINT/CONS adjacency is inconsistent")
            point_cons.setdefault(point_id, set()).add(cons_id)
    for point_id, vertex in vertices.items():
        if point_id not in point_cons:
            raise ValueError("unreferenced HOT POINT %d" % point_id)
        for cons_id in vertex["cons_ids"].intersection(boundaries):
            if point_id not in boundaries[cons_id]["hot_point_ids"]:
                raise ValueError("HOT POINT/CONS adjacency is inconsistent")

    return {
        "source_snapshot_sha256": source_hash,
        "cad_export_sha256": cad_hash,
        "face_ids": sorted(face_ids),
        "face_cons": face_cons,
        "cons_faces": expected_cons_faces,
        "face_parts": face_parts,
        "boundaries": boundaries,
        "vertices": vertices,
        "raw": witness,
    }


def _components(parsed):
    unseen = set(parsed["face_ids"])
    components = []
    while unseen:
        start = min(unseen)
        unseen.remove(start)
        pending = [start]
        component_faces = set()
        while pending:
            face_id = pending.pop()
            component_faces.add(face_id)
            for cons_id in parsed["face_cons"][face_id]:
                for adjacent_face in parsed["cons_faces"][cons_id]:
                    if adjacent_face in unseen:
                        unseen.remove(adjacent_face)
                        pending.append(adjacent_face)
        component_cons = set()
        for face_id in component_faces:
            component_cons.update(parsed["face_cons"][face_id])
        components.append({
            "face_ids": sorted(component_faces),
            "cons_ids": sorted(component_cons),
        })
    components.sort(key=lambda component: component["face_ids"])
    return components


def _component_preconditions(parsed, component):
    face_ids = component["face_ids"]
    cons_ids = component["cons_ids"]
    face_set = set(face_ids)
    reasons = []
    if len(face_ids) != 6:
        reasons.append("component must contain exactly six FACEs")
    if any(len(parsed["face_cons"][face_id]) != 4 for face_id in face_ids):
        reasons.append("every component FACE must contain exactly four CONS")
    part_ids = sorted({parsed["face_parts"][face_id] for face_id in face_ids})
    if len(part_ids) != 1:
        reasons.append("all component FACEs must belong to the same Part")
    if len(cons_ids) != 12:
        reasons.append("component must contain exactly twelve CONS")

    for cons_id in cons_ids:
        boundary = parsed["boundaries"][cons_id]
        native_faces = set(boundary["face_ids"])
        if not native_faces.issubset(face_set):
            reasons.append("CONS %d has native FACE adjacency outside the component" % cons_id)
        if boundary["selected_face_ids"] != face_set.intersection(
                parsed["cons_faces"][cons_id]):
            reasons.append("CONS %d has inconsistent component adjacency" % cons_id)
        if len(boundary["face_ids"]) != 2:
            reasons.append("CONS %d is not shared by exactly two native FACEs" % cons_id)
        if boundary["endpoint_status"] != "available":
            reasons.append("CONS %d has incomplete HOT POINT endpoints" % cons_id)

    hot_point_ids = set()
    point_degree = {}
    for cons_id in cons_ids:
        for point_id in parsed["boundaries"][cons_id]["hot_point_ids"]:
            hot_point_ids.add(point_id)
            point_degree[point_id] = point_degree.get(point_id, 0) + 1
    if len(hot_point_ids) != 8:
        reasons.append("component must contain exactly eight HOT POINTs")
    for point_id in sorted(hot_point_ids):
        expected_cons = {cons_id for cons_id in cons_ids
                         if point_id in parsed["boundaries"][cons_id]["hot_point_ids"]}
        actual_cons = parsed["vertices"][point_id]["cons_ids"]
        if not actual_cons.issubset(set(cons_ids)):
            reasons.append("HOT POINT %d has CONS adjacency outside the component" % point_id)
        if actual_cons != expected_cons:
            reasons.append("HOT POINT %d CONS adjacency is incomplete" % point_id)
        if point_degree.get(point_id) != 3:
            reasons.append("HOT POINT %d must be incident to exactly three CONS" % point_id)

    face_points = {}
    for face_id in face_ids:
        points = set()
        for cons_id in parsed["face_cons"][face_id]:
            points.update(parsed["boundaries"][cons_id]["hot_point_ids"])
        face_points[face_id] = sorted(points)
    return list(dict.fromkeys(reasons)), part_ids, face_points


def _plan_witness(parsed, component):
    # The production planner consumes schema-2 native identities directly.
    # The digest here identifies derived topology, not a new native export.
    result = subset_witness(parsed["raw"], component["face_ids"])
    result["source_snapshot_sha256"] = result["derived_topology_sha256"]
    return result


def _distance(first, second):
    return math.hypot(*(first[index] - second[index] for index in range(3)))


def _mean_points(points):
    count = float(len(points))
    return tuple(math.fsum(point[axis] / count for point in points)
                 for axis in range(3))


def _ceil_ratio(length, target_length):
    ratio = Fraction.from_float(float(length)) / Fraction.from_float(float(target_length))
    return (ratio.numerator + ratio.denominator - 1) // ratio.denominator


def _seed_proposal(parsed, component, topology, target_length):
    source_loop = topology["source_loop"]
    target_loop = topology["target_loop"]
    side_pairs = topology["side_pairs"]
    if len(source_loop) != 4 or len(target_loop) != 4 or len(side_pairs) != 4:
        raise ValueError("strict topology plan did not return four cap edges")

    source_cons = [pair["source_cons_id"] for pair in side_pairs]
    target_cons = [pair["target_cons_id"] for pair in side_pairs]
    if len(set(source_cons + target_cons)) != 8:
        raise ValueError("strict topology plan returned duplicate cap CONS")

    edge_groups = []
    cap_steps = {}
    for group_index, first_index in enumerate((0, 1)):
        edge_indices = (first_index, first_index + 2)
        chord_lengths = []
        group_cons = []
        for edge_index in edge_indices:
            next_index = (edge_index + 1) % 4
            source_first = parsed["vertices"][source_loop[edge_index]]["position"]
            source_second = parsed["vertices"][source_loop[next_index]]["position"]
            target_first = parsed["vertices"][target_loop[edge_index]]["position"]
            target_second = parsed["vertices"][target_loop[next_index]]["position"]
            chord_lengths.extend((
                _distance(source_first, source_second),
                _distance(target_first, target_second),
            ))
            group_cons.extend((source_cons[edge_index], target_cons[edge_index]))
        max_chord = max(chord_lengths)
        steps = _ceil_ratio(max_chord, target_length)
        for cons_id in group_cons:
            cap_steps[cons_id] = steps
        edge_groups.append({
            "group_id": group_index,
            "source_cons_ids": [source_cons[index] for index in edge_indices],
            "target_cons_ids": [target_cons[index] for index in edge_indices],
            "cons_ids": sorted(group_cons),
            "max_endpoint_chord": max_chord,
            "steps": steps,
        })

    longitudinal_cons = [row["connector_cons_id"]
                         for row in topology["vertex_pairs"]]
    if len(longitudinal_cons) != 4 or len(set(longitudinal_cons)) != 4:
        raise ValueError("strict topology plan did not return four distinct longitudinal CONS")
    longitudinal_steps = _ceil_ratio(topology["distance"], target_length)
    for cons_id in longitudinal_cons:
        cap_steps[cons_id] = longitudinal_steps
    if set(cap_steps) != set(component["cons_ids"]):
        raise ValueError("seed suggestion must assign every component CONS exactly once")

    group_steps = [edge_groups[index]["steps"] for index in range(2)]
    predicted_source_quads = group_steps[0] * group_steps[1]
    predicted_solids = predicted_source_quads * longitudinal_steps
    predicted_nodes = ((group_steps[0] + 1) * (group_steps[1] + 1)
                       * (longitudinal_steps + 1))
    predicted_boundary_quads = 2 * (predicted_source_quads + sum(group_steps) * longitudinal_steps)
    violations = []
    if any(steps > 100 for steps in cap_steps.values()):
        violations.append("steps_exceed_100")
    if predicted_solids > 5000:
        violations.append("predicted_solids_exceed_5000")
    if predicted_nodes > 10000:
        violations.append("predicted_nodes_exceed_10000")
    if predicted_boundary_quads > 400:
        violations.append("predicted_boundary_quads_exceed_400")

    return {
        "origin": "corner_chord_and_translation_estimate",
        "counts_are_corner_suggestions": True,
        "edge_groups": edge_groups,
        "longitudinal_cons_ids": sorted(longitudinal_cons),
        "longitudinal_steps": longitudinal_steps,
        "cons_steps": [
            {"cons_id": cons_id, "steps": cap_steps[cons_id]}
            for cons_id in sorted(cap_steps)
        ],
        "predicted_source_quads": predicted_source_quads,
        "predicted_solids": predicted_solids,
        "predicted_nodes": predicted_nodes,
        "predicted_boundary_quads": predicted_boundary_quads,
        "whole_model_entity_budget": "not_evaluated",
        "supported_by_current_map_limits": not violations,
        "limit_violations": violations,
        "limitations": [
            "corner chord counts do not prove curved-edge length or mesh quality",
            "suggested counts are not native CONS seed assignments",
        ],
        **_FALSE_PROOFS,
    }


def _unsupported_component(component, reasons, part_ids):
    return {
        "face_ids": component["face_ids"],
        "cons_ids": component["cons_ids"],
        "part_ids": part_ids,
        "status": "unsupported",
        "reason": "; ".join(reasons) if reasons else "no strict cap mapping passed",
        "alternatives": [],
        **_FALSE_PROOFS,
    }


def discover(witness, target_length):
    """Find topology-only sweep candidates in every FACE component.

    ``target_length`` is a positive finite length.  Successful alternatives
    pass ``plan_translation`` with its fixed 1e-7 corner tolerance; they are
    not proof of exact CAD geometry, export coverage, or mesh quality.
    """
    target_length = _finite_number(target_length, "target_length", positive=True)
    parsed = _validate_witness(witness)
    components = _components(parsed)
    reports = []
    classified_faces = []
    for component in components:
        face_ids = component["face_ids"]
        classified_faces.extend(face_ids)
        reasons, part_ids, face_points = _component_preconditions(parsed, component)
        if reasons:
            reports.append(_unsupported_component(component, reasons, part_ids))
            continue

        plan_witness = _plan_witness(parsed, component)
        face_centers = {
            face_id: _mean_points([
                parsed["vertices"][point_id]["position"]
                for point_id in face_points[face_id]
            ])
            for face_id in face_ids
        }
        alternatives = []
        failures = []
        for source_face_id, target_face_id in combinations(face_ids, 2):
            source_center = face_centers[source_face_id]
            target_center = face_centers[target_face_id]
            delta = tuple(target_center[axis] - source_center[axis]
                          for axis in range(3))
            distance = math.hypot(*delta)
            if not math.isfinite(distance) or distance <= 0.0:
                failures.append("FACE pair %d/%d has no finite positive center distance" %
                                (source_face_id, target_face_id))
                continue
            direction = tuple(component_value / distance for component_value in delta)
            try:
                topology = plan_translation(
                    plan_witness, source_face_id, target_face_id,
                    direction, distance, tolerance=_TOPOLOGY_TOLERANCE,
                )
            except ValueError as exc:
                failures.append(str(exc))
                continue
            map_options = {
                "face_ids": list(face_ids),
                "source_face_id": source_face_id,
                "target_face_id": target_face_id,
                "direction": list(topology["direction"]),
                "distance": topology["distance"],
            }
            seed_proposal = _seed_proposal(
                parsed, component, topology, target_length)
            alternatives.append({
                "topology": topology,
                "map_options": map_options,
                "seed_proposal": seed_proposal,
                **_FALSE_PROOFS,
            })

        if alternatives:
            reports.append({
                "face_ids": face_ids,
                "cons_ids": component["cons_ids"],
                "part_ids": part_ids,
                "status": "topology_candidate",
                "reason": "corner topology passed strict translation checks; CAD surface equivalence and export continuity remain unverified",
                "alternatives": alternatives,
                **_FALSE_PROOFS,
            })
        else:
            unique_failures = list(dict.fromkeys(failures))
            details = "; ".join(unique_failures[:3])
            if len(unique_failures) > 3:
                details += "; additional cap directions rejected"
            reason = "no cap pair passed strict translation topology"
            if details:
                reason += ": " + details
            reports.append(_unsupported_component(component, [reason], part_ids))

    classified_faces = sorted(classified_faces)
    scope_faces = parsed["face_ids"]
    unclassified_faces = sorted(set(scope_faces) - set(classified_faces))
    unsupported_faces = sorted({
        face_id for report in reports if report["status"] == "unsupported"
        for face_id in report["face_ids"]
    })
    return {
        "source_snapshot_sha256": parsed["source_snapshot_sha256"],
        "target_length": target_length,
        "coverage": {
            "requested_face_ids": scope_faces,
            "classified_face_ids": classified_faces,
            "unclassified_face_ids": unclassified_faces,
            "unsupported_face_ids": unsupported_faces,
            "complete": not unclassified_faces,
        },
        "components": reports,
        "limitations": [
            "only complete native six-FACE components are considered for cap mapping",
            "topology candidates do not prove exact native CAD equivalence or continuous export coverage",
            "seed proposals are corner-count estimates and do not prove curved-edge length or mesh quality",
        ],
        **_FALSE_PROOFS,
    }


def subset_witness(witness, face_ids):
    """Return a hash-bound topology-only subset for one whole FACE component.

    The derived hash identifies this filtered topology and its parent snapshot;
    it is not an independent native CAD range-export hash.
    """
    parsed = _validate_witness(witness)
    selected_ids = _identifier_list(face_ids, "face_ids", _MAX_FACES)
    if not selected_ids:
        raise ValueError("face_ids must not be empty")
    selected = set(selected_ids)
    matching = [component for component in _components(parsed)
                if set(component["face_ids"]) == selected]
    if len(matching) != 1:
        raise ValueError("face_ids must match exactly one complete CONS-connected component")
    component = matching[0]
    selected_cons = set(component["cons_ids"])
    selected_hot_points = {
        point_id for point_id, vertex in parsed["vertices"].items()
        if vertex["cons_ids"].intersection(selected_cons)
    }
    for cons_id in selected_cons:
        selected_hot_points.update(parsed["boundaries"][cons_id]["hot_point_ids"])

    raw = parsed["raw"]
    parts = sorted(
        (copy.deepcopy(part) for part in raw["parts"]
         if part["type"] == "FACE" and part["id"] in selected),
        key=lambda row: row["id"],
    )
    face_boundaries = sorted(
        (copy.deepcopy(face) for face in raw["face_boundaries"]
         if face["face_id"] in selected),
        key=lambda row: row["face_id"],
    )
    boundaries = sorted(
        (copy.deepcopy(boundary) for boundary in raw["boundaries"]
         if boundary["id"] in selected_cons),
        key=lambda row: row["id"],
    )
    vertices = sorted(
        (copy.deepcopy(vertex) for vertex in raw["vertices"]
         if vertex["id"] in selected_hot_points),
        key=lambda row: row["id"],
    )
    body = {
        "feature_schema": 2,
        "source_scope": {"face_ids": component["face_ids"], "curve_ids": []},
        "feature_context": copy.deepcopy(raw["feature_context"]),
        "parts": parts,
        "face_boundaries": face_boundaries,
        "boundaries": boundaries,
        "vertices": vertices,
        "curve_lengths": [],
        "parent_source_snapshot_sha256": parsed["source_snapshot_sha256"],
        "derived_topology_only": True,
    }
    return {
        **body,
        "derived_topology_sha256": _sha256_json(body, "derived topology payload"),
        "limitations": [
            "derived from the parent snapshot; no independent native range export was performed",
            "boundary.face_ids and vertex.cons_ids retain out-of-scope native adjacency",
        ],
        **_FALSE_PROOFS,
    }
