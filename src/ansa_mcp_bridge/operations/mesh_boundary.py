"""Pure-data audit of CAD FACE to shell to solid free-facet membership."""

from collections import defaultdict
from itertools import combinations

from .mesh_audit import (
    MAX_ELEMENTS,
    _build_face_uses,
    _finite_number,
    _normalize_facets,
    _positive_id,
    _prepare_snapshot,
    _sequence,
)


MAX_FACE_RECORDS = 1_000
MAX_SHELLS = 50_000
MAX_FACET_NODE_REFERENCES = 300_000


def _combine_status(*statuses):
    if "fail" in statuses:
        return "fail"
    if "incomplete" in statuses:
        return "incomplete"
    return "pass"


def _issue(diagnostics, face_statuses, severity, code, message, *, face_ids=(), **details):
    diagnostics.append({
        "section": "membership",
        "severity": severity,
        "code": code,
        "message": message,
        **({"details": details} if details else {}),
    })
    for face_id in face_ids:
        if face_id in face_statuses:
            face_statuses[face_id] = _combine_status(face_statuses[face_id], severity)


def _face_shells(raw_face, face_index, face_id, diagnostics, face_statuses):
    raw_shells = raw_face.get("shells")
    if raw_shells is None:
        _issue(
            diagnostics, face_statuses, "incomplete", "face_shells_unavailable",
            "CAD FACE shell membership was not supplied.", face_ids=(face_id,),
            face_id=face_id,
        )
        return []
    return _sequence(raw_shells, f"face_records[{face_index}].shells")


def _triangle_pair_for_quad(quad_key, free_facets):
    quad_nodes = set(quad_key)
    candidates = [
        tuple(sorted(nodes))
        for nodes in combinations(quad_key, 3)
        if tuple(sorted(nodes)) in free_facets
    ]
    for first, second in combinations(candidates, 2):
        first_set, second_set = set(first), set(second)
        if first_set | second_set == quad_nodes and len(first_set & second_set) == 2:
            return [list(first), list(second)]
    return None


def audit_boundary_membership(snapshot, face_records, *, require_all_boundary=True):
    """Audit explicit CAD FACE/SHELL membership against a solid snapshot.

    Each FACE record requires ``face_id`` and ``shells``; its declared count is
    the combined CQUAD4/CTRIA3 count. A FACE is complete only when its source
    confirms ``membership_complete``, the count matches, and the list is
    nonempty. Additional FACE fields are optional source metadata and are
    returned under ``faces[*].source_metadata`` without affecting membership.

    The result contains ``status``, versioned ``summary``, per-FACE shell and
    facet records, ordered unique ``expected_boundary`` facets (including
    facets that cannot map to SOLID nodes), unassigned free facets, duplicate
    FACE assignments, non-free shell facets, and diagnostics.
    ``status`` is ``pass``, ``fail``, or ``incomplete``;
    ``expected_boundary`` is an ordered ``list[list[int]]``; each returned
    shell has its original node IDs and a ``facet.mapping_status``.

    ``require_all_boundary=True`` makes unassigned solid free facets an
    acceptance failure when both input inventories are complete. When false,
    those facets remain reported but do not fail an otherwise valid partial
    membership audit. Any confirmed contradiction still fails in either mode.

    Matching uses exact node-ID sets only. Coordinates do not choose or change
    membership. This checks topological attribution; it does not establish
    continuous CAD coverage, geometric distance, or solver validity.
    """
    if type(require_all_boundary) is not bool:
        raise ValueError("require_all_boundary must be a bool")

    coords, elements, supported, unsupported = _prepare_snapshot(snapshot)
    if len(elements) > MAX_ELEMENTS:
        raise ValueError(f"snapshot exceeds MAX_ELEMENTS={MAX_ELEMENTS}")
    solid_node_ids = {
        node_id for element in elements.values() for node_id in element["nodes"]
    }

    diagnostics = []
    face_statuses = {}
    face_inputs = []
    shell_id_owners = defaultdict(list)
    facet_claims = defaultdict(list)
    expected_facets = []
    expected_keys = set()
    normalized_input_facets = {}
    total_shells = 0
    facet_node_references = 0
    missing_solid_node_reference_count = 0

    snapshot_complete = snapshot.get("complete")
    if snapshot_complete is not None and type(snapshot_complete) is not bool:
        raise ValueError("snapshot.complete must be a bool or null")
    integrity = snapshot.get("integrity")
    if integrity is not None and type(integrity) is not dict:
        raise ValueError("snapshot.integrity must be a dict or null")
    integrity_complete = None if integrity is None else integrity.get("complete")
    if integrity_complete is not None and type(integrity_complete) is not bool:
        raise ValueError("snapshot.integrity.complete must be a bool or null")
    snapshot_scope_complete = snapshot_complete is True and integrity_complete is True
    if not snapshot_scope_complete:
        _issue(
            diagnostics, face_statuses, "incomplete", "solid_snapshot_scope_incomplete",
            "The snapshot does not confirm complete capture of the explicit solid scope.",
        )

    if face_records is None:
        raw_faces = []
    else:
        raw_faces = _sequence(face_records, "face_records")
    if len(raw_faces) > MAX_FACE_RECORDS:
        raise ValueError(f"face_records exceeds MAX_FACE_RECORDS={MAX_FACE_RECORDS}")
    if not raw_faces:
        _issue(
            diagnostics, face_statuses, "incomplete", "face_records_missing",
            "No CAD FACE inventory was supplied; whole-CAD membership cannot be confirmed.",
        )

    seen_face_ids = set()
    for face_index, raw_face in enumerate(raw_faces):
        if type(raw_face) is not dict:
            raise ValueError(f"face_records[{face_index}] must be a dict")
        face_id = _positive_id(raw_face.get("face_id"), f"face_records[{face_index}].face_id")
        if face_id in seen_face_ids:
            raise ValueError(f"duplicate face_id {face_id}")
        seen_face_ids.add(face_id)
        face_statuses[face_id] = "pass"

        declared_shell_count = raw_face.get("declared_shell_count")
        if declared_shell_count is not None and (
            type(declared_shell_count) is not int or declared_shell_count < 0
        ):
            raise ValueError(f"face {face_id}.declared_shell_count must be a nonnegative integer or null")
        membership_complete = raw_face.get("membership_complete")
        if membership_complete is not None and type(membership_complete) is not bool:
            raise ValueError(f"face {face_id}.membership_complete must be a bool or null")
        if "cad_area" in raw_face and raw_face["cad_area"] is not None:
            _finite_number(raw_face["cad_area"], f"face {face_id}.cad_area")

        raw_shells = _face_shells(raw_face, face_index, face_id, diagnostics, face_statuses)
        total_shells += len(raw_shells)
        if total_shells > MAX_SHELLS:
            raise ValueError(f"face_records exceeds MAX_SHELLS={MAX_SHELLS}")

        if declared_shell_count is None:
            _issue(
                diagnostics, face_statuses, "incomplete", "declared_shell_count_unknown",
                "The CAD FACE declared shell count is unknown.", face_ids=(face_id,),
                face_id=face_id,
            )
        elif declared_shell_count != len(raw_shells):
            _issue(
                diagnostics, face_statuses, "incomplete", "declared_shell_count_mismatch",
                "The declared shell count does not match the supplied shell records.",
                face_ids=(face_id,), face_id=face_id,
                declared_shell_count=declared_shell_count, supplied_shell_count=len(raw_shells),
            )
        if membership_complete is not True:
            _issue(
                diagnostics, face_statuses, "incomplete", "face_membership_incomplete",
                "The CAD FACE shell membership is not confirmed complete.", face_ids=(face_id,),
                face_id=face_id,
            )
        if not raw_shells or declared_shell_count == 0:
            _issue(
                diagnostics, face_statuses, "incomplete", "face_unmeshed",
                "The CAD FACE has no confirmed shell mesh.", face_ids=(face_id,),
                face_id=face_id,
            )

        shell_rows = []
        seen_shell_ids = set()
        for shell_index, raw_shell in enumerate(raw_shells):
            label = f"face {face_id}.shells[{shell_index}]"
            if type(raw_shell) is not dict:
                raise ValueError(f"{label} must be a dict")
            shell_id = _positive_id(raw_shell.get("id"), f"{label}.id")
            if shell_id in seen_shell_ids:
                raise ValueError(f"face {face_id} contains duplicate shell id {shell_id}")
            seen_shell_ids.add(shell_id)

            shell_type = raw_shell.get("type")
            if type(shell_type) is not str:
                raise ValueError(f"{label}.type must be CTRIA3 or CQUAD4")
            shell_type = shell_type.upper()
            if shell_type not in ("CTRIA3", "CQUAD4"):
                raise ValueError(f"{label}.type must be CTRIA3 or CQUAD4")
            nodes = _sequence(raw_shell.get("nodes"), f"{label}.nodes")
            expected_arity = 3 if shell_type == "CTRIA3" else 4
            if len(nodes) != expected_arity:
                raise ValueError(f"{label}.nodes must contain exactly {expected_arity} node IDs")
            node_ids = tuple(
                _positive_id(node_id, f"{label}.nodes[{node_index}]")
                for node_index, node_id in enumerate(nodes)
            )
            if len(set(node_ids)) != len(node_ids):
                raise ValueError(f"{label} contains duplicate node IDs")
            facet_node_references += len(node_ids)
            if facet_node_references > MAX_FACET_NODE_REFERENCES:
                raise ValueError(
                    "face_records exceeds "
                    f"MAX_FACET_NODE_REFERENCES={MAX_FACET_NODE_REFERENCES}"
                )

            facet_key = tuple(sorted(node_ids))
            missing_node_ids = sorted(set(node_ids) - coords.keys())
            missing_solid_node_ids = sorted(set(node_ids) - solid_node_ids)
            if not missing_node_ids:
                normalized_input_facets.setdefault(facet_key, node_ids)
            if facet_key not in expected_keys:
                expected_keys.add(facet_key)
                expected_facets.append(list(node_ids))

            claim = {
                "face_id": face_id,
                "shell_id": shell_id,
                "type": shell_type,
                "nodes": list(node_ids),
            }
            facet_claims[facet_key].append(claim)
            shell_id_owners[shell_id].append(claim)
            shell_rows.append({
                "id": shell_id,
                "type": shell_type,
                "nodes": list(node_ids),
                "facet_key": facet_key,
                "missing_node_ids": missing_node_ids,
                "missing_solid_node_ids": missing_solid_node_ids,
                "mapping_status": "pending",
                "solid_element_ids": [],
            })

        face_inputs.append({
            "face_id": face_id,
            "declared_shell_count": declared_shell_count,
            "membership_complete": membership_complete,
            "shell_count": len(shell_rows),
            "status": face_statuses[face_id],
            "source_metadata": {
                key: value for key, value in raw_face.items()
                if key not in {
                    "face_id", "shells", "declared_shell_count", "membership_complete",
                }
            },
            "shells": shell_rows,
        })

    # Normalize only facets whose node IDs exist in the solid snapshot. Unmapped
    # shell facets remain in the records and are diagnosed below.
    normalized_facets = _normalize_facets(
        list(normalized_input_facets.values()),
        "face_records.shells",
        coords,
        max_facets=MAX_SHELLS,
    )
    normalized_keys = {key for key, _nodes in normalized_facets}

    for shell_id, claims in shell_id_owners.items():
        distinct_faces = sorted({claim["face_id"] for claim in claims})
        if len(distinct_faces) > 1:
            _issue(
                diagnostics, face_statuses, "fail", "shell_assigned_to_multiple_faces",
                "One shell ID is listed under more than one CAD FACE.",
                face_ids=distinct_faces, shell_id=shell_id, face_ids_claimed=distinct_faces,
            )

    for facet_key, claims in facet_claims.items():
        by_face = defaultdict(list)
        for claim in claims:
            by_face[claim["face_id"]].append(claim)
        for face_id, same_face_claims in by_face.items():
            if len(same_face_claims) > 1:
                _issue(
                    diagnostics, face_statuses, "fail", "duplicate_shell_facet_on_face",
                    "A CAD FACE lists more than one shell with the same node-ID facet.",
                    face_ids=(face_id,), face_id=face_id, nodes=list(facet_key),
                    shell_ids=[claim["shell_id"] for claim in same_face_claims],
                )

    duplicate_face_assignments = []
    for facet_key, claims in facet_claims.items():
        distinct_faces = sorted({claim["face_id"] for claim in claims})
        if len(distinct_faces) > 1:
            assignment = {"nodes": list(facet_key), "assignments": claims}
            duplicate_face_assignments.append(assignment)
            _issue(
                diagnostics, face_statuses, "fail", "facet_assigned_to_multiple_faces",
                "The same node-ID facet is assigned to multiple CAD FACE records.",
                face_ids=distinct_faces, nodes=list(facet_key), face_ids_claimed=distinct_faces,
            )

    face_uses = _build_face_uses(supported)
    free_facets = {key: uses for key, uses in face_uses.items() if len(uses) == 1}
    non_free_facets = {key: uses for key, uses in face_uses.items() if len(uses) != 1}
    topology_complete = snapshot_scope_complete and bool(supported) and not unsupported

    if unsupported:
        _issue(
            diagnostics, face_statuses, "incomplete", "unknown_solid_topology",
            "Unsupported solid element types or orders prevent a complete free-facet inventory.",
            unsupported_elements=unsupported,
        )
    if not supported:
        _issue(
            diagnostics, face_statuses, "incomplete", "solid_elements_unavailable",
            "No supported CHEXA8 or CPENTA6 elements are available for topology matching.",
        )

    for facet_key, uses in non_free_facets.items():
        if len(uses) > 2:
            _issue(
                diagnostics, face_statuses, "fail", "nonmanifold_solid_facet",
                "A solid facet is used by more than two supported solid elements.",
                nodes=list(facet_key), solid_element_ids=[use[0] for use in uses],
            )

    free_assignments = defaultdict(list)
    not_free_shell_facets = []
    mapped_shell_facets = 0
    unmapped_shell_facet_count = 0
    missing_node_reference_count = 0
    for face in face_inputs:
        face_id = face["face_id"]
        for shell in face["shells"]:
            facet_key = shell["facet_key"]
            if shell["missing_solid_node_ids"]:
                shell["mapping_status"] = (
                    "missing_nodes" if shell["missing_node_ids"] else "unmapped_nodes"
                )
                unmapped_shell_facet_count += 1
                missing_node_reference_count += len(shell["missing_node_ids"])
                missing_solid_node_reference_count += len(shell["missing_solid_node_ids"])
                _issue(
                    diagnostics, face_statuses, "incomplete", "shell_nodes_missing_from_snapshot",
                    "A CAD shell facet references node IDs absent from the explicit SOLID mesh; "
                    "the facet was retained.",
                    face_ids=(face_id,), face_id=face_id, shell_id=shell["id"],
                    nodes=shell["nodes"], missing_node_ids=shell["missing_node_ids"],
                    missing_solid_node_ids=shell["missing_solid_node_ids"],
                )
                continue

            # The helper's canonical key is authoritative for mapped shell facets.
            if facet_key not in normalized_keys:
                raise RuntimeError("normalized shell facet key was not returned by mesh_audit")
            if facet_key in free_facets:
                shell["mapping_status"] = "free"
                shell["solid_element_ids"] = [free_facets[facet_key][0][0]]
                free_assignments[facet_key].append({
                    "face_id": face_id, "shell_id": shell["id"],
                })
                mapped_shell_facets += 1
                continue

            if facet_key in non_free_facets:
                uses = non_free_facets[facet_key]
                shell["mapping_status"] = "not_free"
                shell["solid_element_ids"] = [use[0] for use in uses]
                not_free_shell_facets.append({
                    "face_id": face_id,
                    "shell_id": shell["id"],
                    "type": shell["type"],
                    "nodes": shell["nodes"],
                    "solid_element_ids": [use[0] for use in uses],
                    "solid_use_count": len(uses),
                })
                _issue(
                    diagnostics, face_statuses, "fail", "shell_facet_not_free",
                    "A CAD shell facet maps to an interior or non-manifold solid facet.",
                    face_ids=(face_id,), face_id=face_id, shell_id=shell["id"],
                    nodes=shell["nodes"], solid_element_ids=[use[0] for use in uses],
                    solid_use_count=len(uses),
                )
                continue

            shell["mapping_status"] = "unmapped"
            unmapped_shell_facet_count += 1
            severity = "fail" if topology_complete else "incomplete"
            _issue(
                diagnostics, face_statuses, severity, "shell_facet_not_found",
                "No exact free or shared solid facet has this shell node-ID set.",
                face_ids=(face_id,), face_id=face_id, shell_id=shell["id"],
                nodes=shell["nodes"],
            )

            if len(facet_key) == 4:
                triangle_pair = _triangle_pair_for_quad(facet_key, free_facets)
                if triangle_pair is not None:
                    _issue(
                        diagnostics, face_statuses, "fail", "quad_shell_split_into_triangles",
                        "A quadrilateral CAD shell does not map to two triangle facets by node set.",
                        face_ids=(face_id,), face_id=face_id, shell_id=shell["id"],
                        quad_nodes=shell["nodes"], matching_free_triangles=triangle_pair,
                    )

    unassigned_free_facets = []
    for facet_key, uses in sorted(free_facets.items()):
        if facet_key not in free_assignments:
            unassigned_free_facets.append({
                "nodes": list(facet_key),
                "solid_element_ids": [uses[0][0]],
            })

    if require_all_boundary and unassigned_free_facets:
        severity = "fail" if topology_complete and bool(face_inputs) and all(
            face["status"] == "pass" for face in face_inputs
        ) and not any(
            shell["missing_solid_node_ids"]
            for face in face_inputs for shell in face["shells"]
        ) else "incomplete"
        _issue(
            diagnostics, face_statuses, severity, "unassigned_solid_free_facets",
            "Some explicit solid free facets have no exact CAD FACE shell assignment.",
            facets=unassigned_free_facets,
        )

    for face in face_inputs:
        face["status"] = face_statuses[face["face_id"]]
        for shell in face["shells"]:
            shell["facet"] = {
                "nodes": shell["nodes"],
                "key": list(shell["facet_key"]),
                "mapping_status": shell["mapping_status"],
                "solid_element_ids": shell["solid_element_ids"],
                "missing_node_ids": shell["missing_node_ids"],
                "missing_solid_node_ids": shell["missing_solid_node_ids"],
            }
            del shell["facet_key"]

    membership_evidence_complete = bool(face_inputs) and all(
        face["status"] == "pass" for face in face_inputs
    )
    boundary_coverage_complete = (
        topology_complete
        and membership_evidence_complete
        and not unassigned_free_facets
        and not not_free_shell_facets
        and not duplicate_face_assignments
        and not any(item["severity"] == "fail" for item in diagnostics)
    )
    status = _combine_status(*(item["severity"] for item in diagnostics))
    if status == "pass" and require_all_boundary and not boundary_coverage_complete:
        status = "incomplete"

    summary = {
        "face_count": len(face_inputs),
        "shell_count": total_shells,
        "solid_element_count": len(elements),
        "supported_solid_element_count": len(supported),
        "unsupported_solid_element_count": len(unsupported),
        "solid_free_facet_count": len(free_facets),
        "expected_boundary_facet_count": len(expected_facets),
        "mapped_shell_facet_count": mapped_shell_facets,
        "unmapped_shell_facet_count": unmapped_shell_facet_count,
        "missing_node_reference_count": missing_node_reference_count,
        "missing_solid_node_reference_count": missing_solid_node_reference_count,
        "unassigned_free_facet_count": len(unassigned_free_facets),
        "duplicate_face_assignment_count": len(duplicate_face_assignments),
        "not_free_shell_facet_count": len(not_free_shell_facets),
        "snapshot_scope_complete": snapshot_scope_complete,
        "topology_complete": topology_complete,
        "face_membership_evidence_complete": membership_evidence_complete,
        "boundary_coverage_required": require_all_boundary,
        "boundary_coverage_complete": boundary_coverage_complete,
        "membership_basis": "exact_node_id_set",
        "scope_limitations": [
            "This is a topological membership audit only; it does not prove continuous CAD "
            "coverage, geometric distance, or solver validity."
        ],
        "input_budgets": {
            "max_solid_elements": MAX_ELEMENTS,
            "max_face_records": MAX_FACE_RECORDS,
            "max_shells": MAX_SHELLS,
            "max_facet_node_references": MAX_FACET_NODE_REFERENCES,
        },
    }

    return {
        "schema_version": 1,
        "status": status,
        "summary": summary,
        "faces": face_inputs,
        "expected_boundary": expected_facets,
        "unassigned_free_facets": unassigned_free_facets,
        "duplicate_face_assignments": duplicate_face_assignments,
        "not_free_shell_facets": not_free_shell_facets,
        "diagnostics": diagnostics,
    }


__all__ = ["audit_boundary_membership"]
