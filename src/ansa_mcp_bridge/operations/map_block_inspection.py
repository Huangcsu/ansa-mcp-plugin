"""Bounded readback of existing native FACE-defined Map Block volumes.

Native state, complete caps, CAD membership and actual surface mesh are separate
observations. None of them alone authorizes a subsequent meshing operation.
"""
from collections import Counter

from . import entity_catalog as catalog, geometry_features as features, inspection
from . import structured_boundary, structured_native as native, volume_meshing as guards


def _references(base, deck, values, label, maximum):
    if type(values) not in (list, tuple) or len(values) > maximum:
        raise ValueError(label + " is unavailable or exceeds its entity budget")
    result = [catalog._entity_reference(base, deck, e, "FACE") for e in values]
    if any(r["type"] != "FACE" for r in result) or len({r["id"] for r in result}) != len(result):
        raise ValueError(label + " requires distinct FACE references")
    return sorted(r["id"] for r in result)


def capture(state, base, mesh, constants, deck, volume_ids):
    state.assert_main()
    native._check_nastran(constants, deck)
    ids = sorted(native._id_list(volume_ids, "volume_ids", 20))
    if base.CurrentDeck() != deck:
        raise ValueError("requested deck must be active")
    volumes, all_faces = [], set()
    for ident in ids:
        volume, ref = catalog._get_entity(base, deck, "VOLUME", ident)
        card = guards._read_full_card(base, deck, volume, ref, "Map Block VOLUME", omit_derived=False)
        fields = card["fields"]
        if fields.get("Type") != "Map Block":
            raise ValueError("inspect accepts existing native Map Block VOLUMEs only")
        if any(name not in fields for name in ("State", "Status", "PID")):
            raise ValueError("Map Block state/status/property fields are unavailable")
        boundary = _references(base, deck, mesh.GetVolumeBoundary(volume), "native volume boundary", 100)
        faces = _references(base, deck, base.CollectEntities(deck, volume, "FACE"), "native volume FACEs", 100)
        if not faces or faces != boundary:
            raise ValueError("native Map Block FACE and boundary membership differ or are empty")
        caps_raw = mesh.GetMapVolumeBoundaries(volume)
        if type(caps_raw) is not dict or set(caps_raw) != {"master", "slave", "round"}:
            raise ValueError("native Map Block cap readback is unavailable")
        caps = {k: _references(base, deck, caps_raw[k], "native Map Block " + k, 100) for k in ("master", "slave", "round")}
        flattened = [i for values in caps.values() for i in values]
        complete = (all(caps.values()) and len(flattened) == len(set(flattened)) and sorted(flattened) == faces)
        native_state = fields["State"]
        status = ("native_non_mappable" if native_state == "Non Mappable" else
                  "native_ready" if native_state == "Ready to Mesh" and complete else "incomplete")
        part = base.GetEntityPart(volume)
        if part is None:
            raise ValueError("Map Block VOLUME requires Part ownership")
        all_faces.update(faces)
        if len(all_faces) > 100:
            raise ValueError("combined Map Block boundary exceeds 100 FACE")
        volumes.append({"volume_id": ident, "card": card, "part_id": part._id,
                        "face_ids": faces, "caps": caps, "caps_match_complete_boundary": bool(complete),
                        "native_mapping_status": status, "usable_for_mutation": False})
    face_ids = sorted(all_faces)
    membership, _ = structured_boundary.capture_membership(base, mesh, constants, deck, face_ids)
    # Include every macro member in the fingerprint even when per-FACE attribution
    # is ambiguous. Never assign a multi-FACE macro's SHELLs to each FACE.
    shell_ids = sorted({i for row in membership for i in row["macro_shell_ids"]})
    if len(shell_ids) > 5000:
        raise ValueError("Map Block inspection exceeds 5000 boundary SHELLs")
    elements = native._read_elements(base, deck, "SHELL", shell_ids, {"CQUAD4", "CTRIA3"})
    node_ids = sorted({i for row in elements for i in row["nodes"]})
    if len(node_ids) > 10000:
        raise ValueError("Map Block inspection exceeds 10000 boundary GRID")
    nodes = native._read_grids(base, deck, node_ids)
    by_face = {row["face_id"]: row for row in membership}
    for row in volumes:
        row["face_mesh_membership_complete"] = all(by_face[i]["membership_complete"] for i in row["face_ids"])
        row["surface_shell_ids"] = sorted({i for f in row["face_ids"] for i in by_face[f]["macro_shell_ids"]})
    witness, _ = features._witness(state, base, deck, {"face_ids": face_ids, "curve_ids": []}, {"scoped_export_calls": 0}, face_limit=100)
    body = {"deck": deck, "volume_ids": ids, "feature_context": features._context(state, base, deck),
            "volumes": volumes, "face_membership": membership,
            "surface_mesh": {"nodes": nodes, "elements": elements}, "cad_witness": witness}
    if len(guards._encoded(body)) > 8*1024*1024:
        raise ValueError("Map Block inspection exceeds 8 MiB evidence budget")
    return {"status": "inspected", **body, "source_snapshot_sha256": features._hash(body),
            "summary": {"volume_count": len(volumes), "native_ready_count": sum(v["native_mapping_status"] == "native_ready" for v in volumes),
                        "native_non_mappable_count": sum(v["native_mapping_status"] == "native_non_mappable" for v in volumes),
                        "incomplete_count": sum(v["native_mapping_status"] == "incomplete" for v in volumes),
                        "face_count": len(face_ids), "surface_shell_count": len(shell_ids), "surface_node_count": len(nodes),
                        "surface_element_types": dict(sorted(Counter(e["type"] for e in elements).items()))},
            "usable_for_mutation": False, "continuous_CAD_coverage_proven": False,
            "limitations": ["native Ready to Mesh is an ANSA state, not independent CAD/FE coverage or mesh-quality acceptance",
                            "no SOLID generation, CAD splitting, node projection or quality repair is performed",
                            "full mesh connectivity, coordinates and CAD source are rebound on every evidence page",
                            "only saved NASTRAN millimeter FACE-defined Map Block volumes are supported"]}


def validate_report(state, base, mesh, constants, deck, kind, result):
    if kind != "mesh_map_block_inspect":
        raise ValueError("Map Block evidence kind mismatch")
    current = capture(state, base, mesh, constants, deck, result["volume_ids"])
    if current["source_snapshot_sha256"] != result.get("source_snapshot_sha256"):
        raise ValueError("Map Block inspection evidence is stale")
