"""Read-only finite sweep classification; never run native MapBlock here.

ANSA MapBlock also creates surface mesh/volumes. This narrower diagnostic
reuses native identities and the existing export-domain proof before offering
explicit inputs for a subsequent, independently preflighted Map operation.
"""
from collections import OrderedDict
import copy
import time

from . import cad_sweep_candidates, cad_sweep_geometry, geometry_features as features, structured_evidence


def _request(face_ids, target_length):
    return features._ids(face_ids, 100, "face_ids"), features._number(target_length, "target_length", True)


def capture(state, base, deck, face_ids, target_length):
    state.assert_main()
    face_ids, target_length = _request(face_ids, target_length)
    if not face_ids:
        raise ValueError("explicit nonempty FACE scope is required")
    started = time.monotonic()
    stats = {"scoped_export_calls": 0, "cons_hot_point_calls": 0, "hot_point_adjacency_calls": 0}
    witness, _ = features._witness(state, base, deck, {"face_ids": face_ids, "curve_ids": []}, stats, face_limit=100)
    key = features._hash({"classifier_version":3,"source": witness["source_snapshot_sha256"], "target_length": target_length})
    cache = getattr(state, "_geometry_sweep_candidates_cache", None)
    if cache is None:
        cache = state._geometry_sweep_candidates_cache = OrderedDict()
    hit = key in cache
    if hit:
        classification = copy.deepcopy(cache[key])
        cache.move_to_end(key)
    else:
        classification = cad_sweep_candidates.discover(witness, target_length)
        for component in classification["components"]:
            if component["status"] != "topology_candidate":
                continue
            subset = cad_sweep_candidates.subset_witness(witness, component["face_ids"])
            subset["source_snapshot_sha256"] = subset["derived_topology_sha256"]
            geometry = cad_sweep_geometry.capture(state, base, deck, subset, component["alternatives"][0]["topology"])
            geometry["derived_topology_sha256"] = geometry.pop("source_snapshot_sha256")
            geometry["parent_source_snapshot_sha256"] = witness["source_snapshot_sha256"]
            stats["scoped_export_calls"] += geometry["scoped_export_calls"]
            component["geometry"] = geometry
            component["continuous_export_domain_verified"] = geometry["status"] == "verified_at_export_precision"
            component["status"] = geometry["status"]
            component["reason"] = ("finite planar export domain verified; Map seed/mesh/write preflight still required"
                                   if component["continuous_export_domain_verified"] else
                                   "native topology fits; one or more FACE export forms are outside the finite proof")
        classification["coverage"]["template_family"] = "T1_complete_six_face_component"
        classification["coverage"]["unsupported_face_ids"] = sorted(
            face_id for component in classification["components"]
            if component.get("continuous_export_domain_verified") is not True
            for face_id in component["face_ids"])
        if len(face_ids)>6:
            from . import rectangular_candidates, map_block_batch
            rectangular=rectangular_candidates.discover(witness,target_length)
            if rectangular['status']=='topology_candidate':
                requests=[(map_block_batch.local_witness(witness,row['face_ids']),row['alternatives'][0]['topology']) for row in rectangular['blocks']]
                geometry=cad_sweep_geometry.capture_many(state,base,deck,requests)
                stats['scoped_export_calls']+=sum(g['scoped_export_calls'] for g in geometry)
                rectangular['geometry']=geometry
                rectangular['continuous_export_domain_verified']=all(g['status']=='verified_at_export_precision' for g in geometry)
                rectangular['status']='verified_at_export_precision' if rectangular['continuous_export_domain_verified'] else 'unsupported'
                if rectangular['continuous_export_domain_verified']:
                    rectangular['prepare_options']={'face_groups':rectangular['face_groups'],'target_length':target_length}
                else:rectangular['reason']='one or more rectangular FACE export domains are unsupported'
            classification['rectangular_assembly']=rectangular
            rectangular['coverage']={
                'template_family':'T2_axis_aligned_rectangular_assembly',
                'scope':'selected_native_FACE_ids',
                'requested_face_ids':list(face_ids),
                'verified_face_ids':list(face_ids) if rectangular.get('continuous_export_domain_verified') is True else [],
                'unsupported_face_ids':[] if rectangular.get('continuous_export_domain_verified') is True else list(face_ids),
                'original_prepartition_CAD_coverage_proven':False,
            }
        if len(structured_evidence._encoded(classification)) <= features._CACHE_BYTES:
            cache[key] = copy.deepcopy(classification)
            while len(cache) > features._CACHE_ENTRIES or sum(len(structured_evidence._encoded(v)) for v in cache.values()) > features._CACHE_BYTES:
                cache.popitem(last=False)
    components = classification["components"]
    # Preserve separate topology and export claims: neither grants write authority.
    verified = sum(c.get("continuous_export_domain_verified") is True for c in components)
    topology = sum(bool(c.get("alternatives")) for c in components)
    return {
        "status": "classified", **witness, "target_length": target_length,
        "classification": classification, "usable_for_mutation": False,
        "exact_native_CAD_equivalence_proven": False,
        "summary": {"face_count": len(face_ids), "component_count": len(components),
                    "legacy_component_statistics_template":"T1_complete_six_face_component",
                    "rectangular_assembly_status":classification.get('rectangular_assembly',{}).get('status','not_applicable'),
                    "rectangular_block_count":len(classification.get('rectangular_assembly',{}).get('face_groups',[])),
                    "topology_candidate_components": topology, "verified_export_components": verified,
                    "unsupported_components": len(components) - verified,
                    "topology_direction_count": sum(len(c.get("alternatives", [])) for c in components),
                    "verified_direction_count": sum(len(c.get("alternatives", [])) for c in components
                                                    if c.get("continuous_export_domain_verified") is True),
                    "cache_status": "hit" if hit else "miss", **stats,
                    "elapsed_seconds": time.monotonic() - started},
        "limitations": [
            "classifies complete T1 components and bounded axis-aligned rectangular T2 assemblies; no arbitrary decomposition or CAD cutting",
            "claims cover the selected native FACE scope; complete original pre-partition CAD coverage is not inferred",
            "export-precision finite proof is not exact native CAD kernel equivalence",
            "seed proposals are not current native seeds and grant no write authority",
            "existing SOLID overlap, interface conformity, mesh quality, and solver acceptance are not evaluated",
            "native MapBlock can discover more general blocks but also creates mesh/volumes; it is not called by this read tool",
        ],
    }


def validate_report(state, base, deck, kind, result):
    if kind != "geometry_sweep_candidates":
        raise ValueError("sweep candidate evidence kind mismatch")
    state.assert_main()
    face_ids, _ = _request(result["source_scope"]["face_ids"], result["target_length"])
    if result.get("feature_context") != features._context(state, base, deck):
        raise ValueError("sweep candidate evidence is stale: target or model context changed")
    witness, _ = features._witness(state, base, deck, {"face_ids": face_ids, "curve_ids": []},
                                   {"scoped_export_calls": 0}, face_limit=100)
    if witness["source_snapshot_sha256"] != result.get("source_snapshot_sha256"):
        raise ValueError("sweep candidate evidence is stale: scoped geometry or topology changed")
