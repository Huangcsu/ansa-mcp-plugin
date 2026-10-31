"""Owning-thread adapters for structured-mesh plans, audits and provenance."""

import hashlib
import json
import time
import uuid

from . import entity_catalog as catalog, inspection, structured_evidence


def _digest(value):
    return hashlib.sha256(structured_evidence._encoded(value)).hexdigest()


def _bounded_operation_result(state, deck, result):
    """Persist full large write evidence while retaining the operation outcome.

    A successful native write must not become an oversized transport reply.
    Historical result pages never provide a new mutation authorization.
    """
    result = structured_evidence._json_result(result)
    raw = structured_evidence._encoded(result)
    if len(raw) <= 192 * 1024:
        return result
    descriptor = structured_evidence.store_report(state, deck, 'mesh_native_operation_result', result)
    descriptor.update(result_paged=True, result_bytes=len(raw), result_sha256=descriptor['sha256'])
    for key in ('ok', 'mode', 'scope_verified', 'api_called', 'possible_effects', 'recovery_required',
                'error', 'observation_error', 'new_solid_ids', 'new_grid_ids', 'created_shell_ids',
                'new_volume_ids', 'filled_volume_id', 'filled_volume_ids', 'all_blocks_filled',
                'solid_mesh_generated', 'solid_count', 'solid_type_counts', 'continuous_export_domain_verified',
                'exact_native_CAD_equivalence_proven', 'continuous_CAD_coverage_proven', 'project_quality_acceptance',
                'provenance_mapping_complete', 'provenance_stage',
                'source_ids', 'shell_ids', 'source_sha256', 'source_evidence'):
        if key in result:
            descriptor[key] = result[key]
    if len(structured_evidence._encoded(descriptor)) > 192 * 1024:
        raise ValueError('structured operation receipt exceeds its bounded ID/diagnostic budget')
    return descriptor


def _snapshot_references(kind, snapshot, epoch):
    coordinates = {node["id"]: node["xyz"] for node in snapshot["nodes"]}
    return {row["id"]: {"type": kind, "id": row["id"], "epoch": epoch,
        "fingerprint": _digest({"element": row,
            "coordinates": {str(n): coordinates[n] for n in row["nodes"]}})}
        for row in snapshot["elements"]}


def _binding(state, deck, source):
    auth = state.authorization or {}
    target = {**state.identity(), "backend_id": auth.get("backend_id"),
              "generation": auth.get("generation")}
    if type(source) is not dict or source.get("epoch") != state.model_epoch:
        raise ValueError("source provenance must match the current model epoch")
    model = {"deck": deck, "epoch": state.model_epoch,
             "instance_id": state.instance_id, "session_id": state.session_id}
    return {"target": {"type": "target", "id": state.instance_id,
                       "fingerprint": _digest(target), "epoch": auth.get("generation", 0)},
            "model": {"type": "model", "id": state.model_epoch,
                      "fingerprint": _digest(model), "epoch": state.model_epoch},
            "source": source}


def _provenance(state, deck, params):
    from . import mesh_provenance
    inspection._params(params, ("deck", "action", "ledger_id", "payload", "expected_hash"))
    action = params["action"]
    ledger_id = params["ledger_id"]
    if action == "create" and ledger_id is None:
        ledger_id = uuid.uuid4().hex
    if ledger_id is None:
        raise ValueError("ledger_id is required for existing provenance")
    payload = dict(params["payload"] or {})
    if action != "read":
        source = payload.get("source")
        binding = _binding(state, deck, source)
        for key in ("target", "model"):
            if key in payload and payload[key] != binding[key]:
                raise ValueError("provenance binding differs from the current " + key)
        payload.update(binding)
    if action == "create":
        if params["expected_hash"] is not None:
            raise ValueError("create cannot specify an existing ledger hash")
        result = mesh_provenance.create_ledger(state.session_dir, ledger_id, payload)
    elif action == "read":
        result = mesh_provenance.read_ledger(state.session_dir, ledger_id)
        if params["expected_hash"] is not None and result["ledger_hash"] != params["expected_hash"]:
            raise ValueError("ledger changed since the expected hash")
    elif action == "record_stage":
        result = mesh_provenance.record_stage(state.session_dir, ledger_id, payload, params["expected_hash"])
    elif action == "validate_resume":
        result = mesh_provenance.validate_resume(state.session_dir, ledger_id, payload, params["expected_hash"])
    else:
        raise ValueError("unsupported provenance action")
    # Caller declarations are auditable records, never a model-write capability.
    result.update(ok=True, ledger_id=ledger_id, declaration_only=True,
                  usable_for_mutation=False, regeneration_status="incomplete")
    return structured_evidence.store_report(state, deck, "mesh_provenance", result)


def _with_operation_ledger(state, deck, tool, params):
    from . import mesh_provenance, structured_native
    prepared = None
    if tool == "mesh_surface":
        from ansa import base, constants, mesh
        from . import mapped_surface
        prepared = mapped_surface.preflight(state, base, mesh, constants, params)
        key = "expected_seed_snapshot_sha256"
        actual = prepared["witness"]["seed_snapshot_sha256"]
    elif tool == "mesh_annular_source_apply":
        from ansa import base, constants, mesh
        from . import annular_map_prepare
        prepared = annular_map_prepare.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
    elif tool == "mesh_map_blocks_apply":
        from ansa import base, constants, mesh
        from . import map_block_assembly_prepare
        prepared = map_block_assembly_prepare.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
    elif tool == "mesh_map_block_fill_apply":
        from ansa import base, constants, mesh
        from . import map_block_fill
        prepared = map_block_fill.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
        source_snapshot = prepared["source"]
    elif tool == "mesh_map_block_caps_apply":
        from ansa import base, constants, mesh
        from . import map_block_caps
        prepared = map_block_caps.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
    elif tool == "mesh_map_block_apply":
        from ansa import base, constants, mesh
        from . import map_block_prepare
        prepared = map_block_prepare.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
    elif tool == "mesh_cad_map_apply":
        from ansa import base, constants, mesh
        from . import cad_map
        prepared = cad_map.preflight(state, base, mesh, constants, params)
        key = "expected_plan_sha256"
        actual = prepared["plan_sha256"]
        source_snapshot = prepared["source"]
    elif tool == "geometry_boundary_prepare":
        from ansa import base, constants
        from . import geometry_boundary
        prepared = geometry_boundary.preflight(state, base, constants, params)
        key = "expected_source_snapshot_sha256"
        actual = prepared["witness"]["source_snapshot_sha256"]
    elif tool == "mesh_seed_apply":
        from ansa import base, constants
        from . import cons_seed
        prepared = cons_seed.preflight(state, base, constants, params)
        key = "expected_seed_snapshot_sha256"
        actual = prepared["witness"]["seed_snapshot_sha256"]
    elif tool == "geometry_partition_apply":
        key = "expected_plan_sha256"
        preview_params = {key: value for key, value in params.items()
                          if key not in {"expected_plan_sha256", "backup_path"}}
        preview = structured_native.execute(state, "geometry_partition_plan", preview_params)
        actual = preview.get("plan_sha256")
    else:
        key = "expected_source_sha256"
        from ansa import base, constants
        source_snapshot = structured_native.capture_source(base, constants, deck, params["shell_ids"])
        actual = source_snapshot["snapshot_sha256"]
    expected = params["seed_scope"][key] if tool == "mesh_surface" else params.get(key)
    if actual != expected:
        raise ValueError("structured mesh source/plan changed; obtain a fresh preflight")
    source_type = ("CONS_SCOPE" if tool in {"mesh_seed_apply", "mesh_surface"} else
                   "VOLUME_SCOPE" if tool == "mesh_map_block_caps_apply" else
                   "FACE_SCOPE" if tool in {"geometry_partition_apply", "geometry_boundary_prepare", "mesh_cad_map_apply", "mesh_map_block_apply", "mesh_map_block_fill_apply", "mesh_map_blocks_apply", "mesh_annular_source_apply"} else
                   "SHELL_SCOPE")
    source = {"type": source_type,
              "id": _digest(params), "fingerprint": expected, "epoch": state.model_epoch}
    binding = _binding(state, deck, source)
    ledger_id = uuid.uuid4().hex
    initial = dict(binding)
    if tool in {"mesh_sweep", "mesh_cad_map_apply", "mesh_map_block_fill_apply"}:
        old = _snapshot_references("SHELL", source_snapshot, state.model_epoch)
        initial["fe_mapping"] = {"original_fe": list(old.values()), "new_fe": [],
            "pairs": [{"original": ref, "created": []} for ref in old.values()]}
    current = mesh_provenance.create_ledger(state.session_dir, ledger_id, initial)
    for stage in ("source_checked", "applying"):
        current = mesh_provenance.record_stage(state.session_dir, ledger_id,
                    {**binding, "stage": stage}, current["ledger_hash"])
    started = time.monotonic()
    try:
        if tool == "mesh_surface":
            result = mapped_surface.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_annular_source_apply":
            result = annular_map_prepare.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_map_blocks_apply":
            result = map_block_assembly_prepare.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_map_block_fill_apply":
            result = map_block_fill.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_map_block_caps_apply":
            from . import map_block_caps
            result = map_block_caps.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_map_block_apply":
            result = map_block_prepare.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "mesh_cad_map_apply":
            result = cad_map.apply_prepared(state, base, mesh, constants, params, prepared)
        elif tool == "geometry_boundary_prepare":
            result = geometry_boundary.apply_prepared(state, base, constants, params, prepared)
        elif tool == "mesh_seed_apply":
            result = cons_seed.apply_prepared(state, base, constants, params, prepared)
        else:
            result = structured_native.execute(state, tool, params)
    except Exception as exc:
        recovery = mesh_provenance.record_stage(state.session_dir, ledger_id,
                       {**binding, "stage": "recovery_required",
                        "elapsed_seconds": time.monotonic() - started}, current["ledger_hash"])
        detail = dict(getattr(exc, "diagnostics", {}) or {})
        detail.update(ledger_id=ledger_id, ledger_hash=recovery["ledger_hash"],
                      recovery_required=True, original_operation_must_be_queried=True)
        try:
            exc.diagnostics = detail
        except AttributeError:
            pass
        raise
    stage = "verified" if result.get("ok") is True and result.get("scope_verified") is True else "recovery_required"
    completion = {**binding, "stage": stage, "elapsed_seconds": time.monotonic() - started,
                  "native_state": {"ownership": "unknown", "owner": None, "regeneration": "unknown"}}
    shell_ids = result.get("source_ids") if tool == "mesh_annular_source_apply" else result.get("created_shell_ids")
    if tool in {"mesh_surface", "mesh_map_block_apply", "mesh_map_blocks_apply", "mesh_annular_source_apply"} and shell_ids:
        try:
            if state.model_epoch != binding["model"]["epoch"]:
                raise ValueError("model epoch changed before mapped SHELL attribution")
            output = structured_native.capture_source(base, constants, deck, shell_ids)
            if tool == "mesh_annular_source_apply" and output["snapshot_sha256"] != result.get("source_sha256"):
                raise ValueError("annular source changed before provenance attribution")
            new = _snapshot_references("SHELL", output, state.model_epoch)
            completion["fe_mapping"] = {"original_fe": [], "new_fe": list(new.values()), "pairs": []}
            result["provenance_mapping_complete"] = stage == "verified"
        except Exception as exc:
            completion["stage"] = stage = "recovery_required"
            result.update(ok=False, status="recovery_required", scope_verified=False,
                          provenance_mapping_complete=False, possible_effects=True, recovery_required=True,
                          error="mapped SHELL provenance readback failed: " + str(exc))
    if tool in {"mesh_sweep", "mesh_cad_map_apply", "mesh_map_block_fill_apply"} and result.get("new_solid_ids"):
        try:
            if state.model_epoch != binding["model"]["epoch"]:
                raise ValueError("model epoch changed; cannot attribute current FE to the original operation")
            output = structured_native.capture_mesh(base, constants, deck, result["new_solid_ids"])
            new = _snapshot_references("SOLID", output, state.model_epoch)
            mappings = (result.get("new_mesh") or {}).get("verification", {}).get("source_layer_map", [])
            if tool in {"mesh_cad_map_apply", "mesh_map_block_fill_apply"}:
                mappings = [{"source_shell_id": row["source_element_id"], "solid_id": row["output_element_id"]}
                            for row in result.get("verification", {}).get("source_cell_matches", [])]
            completion["fe_mapping"] = {"original_fe": list(old.values()), "new_fe": list(new.values()),
                "pairs": [{"original": ref, "created": [new[m["solid_id"]] for m in mappings
                           if m["source_shell_id"] == source_id]} for source_id, ref in old.items()]}
            result["provenance_mapping_complete"] = bool(mappings) and stage == "verified"
        except Exception as exc:
            completion["stage"] = stage = "recovery_required"
            result.update(ok=False, possible_effects=True, recovery_required=True,
                          error="FE provenance readback failed: " + str(exc))
    try:
        current = mesh_provenance.record_stage(state.session_dir, ledger_id,
                      completion, current["ledger_hash"])
    except Exception as exc:
        return {"ok": False, "possible_effects": True, "recovery_required": True,
                "error": "native operation returned but provenance persistence failed: " + str(exc),
                "ledger_id": ledger_id, "ledger_hash": current["ledger_hash"],
                "operation_result": result}
    result.update(ledger_id=ledger_id, ledger_hash=current["ledger_hash"],
                  provenance_stage=stage, regeneration_status="incomplete")
    return _bounded_operation_result(state, deck, result)


def execute(state, tool, params):
    state.assert_main()
    from ansa import base, constants
    deck = catalog._deck(base, params)
    if tool == "mesh_evidence_page":
        inspection._params(params, ("deck", "report_id", "section", "offset", "limit"))
        from . import cons_seed, cad_map
        from .geometry_features import validate_report
        def validate_evidence(kind, result):
            if kind == "mesh_map_blocks_plan":
                from ansa import mesh
                from . import map_block_assembly_prepare
                return map_block_assembly_prepare.validate_report(state, base, mesh, constants, deck, result)
            if kind == "mesh_map_block_fill_plan":
                from ansa import mesh
                from . import map_block_fill
                return map_block_fill.validate_report(state, base, mesh, constants, deck, result)
            if kind == "mesh_map_block_caps_plan":
                from ansa import mesh
                from . import map_block_caps
                return map_block_caps.validate_report(state, base, mesh, constants, deck, result)
            if kind == "mesh_map_block_plan":
                from ansa import mesh
                from . import map_block_prepare
                return map_block_prepare.validate_report(state, base, mesh, constants, deck, result)
            if kind == "mesh_map_block_inspect":
                from ansa import mesh
                from . import map_block_inspection
                return map_block_inspection.validate_report(state, base, mesh, constants, deck, kind, result)
            if kind == "geometry_sweep_candidates":
                from . import geometry_sweep_candidates
                return geometry_sweep_candidates.validate_report(state, base, deck, kind, result)
            if kind == "mesh_cad_map_plan":
                from ansa import mesh
                return cad_map.validate_report(state, base, mesh, constants, deck, result)
            return (cons_seed.validate_report(state, base, deck, kind, result)
                    if kind == "mesh_seed_read" else validate_report(state, base, deck, kind, result))
        return structured_evidence.read_page(state, deck, params["report_id"],
                    params["section"], params["offset"], params["limit"],
                    freshness_validator=validate_evidence)
    if tool == "mesh_annular_source_plan":
        from ansa import mesh
        from . import annular_map_prepare
        clean = annular_map_prepare.normalize(params)
        return annular_map_prepare.public_plan(annular_map_prepare.preflight(state, base, mesh, constants, clean))
    if tool == "mesh_map_blocks_plan":
        from ansa import mesh
        from . import map_block_assembly_prepare
        clean = map_block_assembly_prepare.normalize(params)
        result = map_block_assembly_prepare.public_plan(map_block_assembly_prepare.preflight(state, base, mesh, constants, clean))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_map_block_fill_plan":
        from ansa import mesh
        from . import map_block_fill
        clean = map_block_fill.normalize(params)
        result = map_block_fill.public_plan(map_block_fill.preflight(state, base, mesh, constants, clean))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_map_block_caps_plan":
        from ansa import mesh
        from . import map_block_caps
        clean = map_block_caps.normalize(params)
        result = map_block_caps.public_plan(map_block_caps.preflight(state, base, mesh, constants, clean))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_map_block_plan":
        from ansa import mesh
        from . import map_block_prepare
        clean = map_block_prepare.normalize(params)
        result = map_block_prepare.public_plan(map_block_prepare.preflight(state, base, mesh, constants, clean))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_cad_map_plan":
        from ansa import mesh
        from . import cad_map
        clean = cad_map.normalize(params)
        result = cad_map.public_plan(cad_map.preflight(state, base, mesh, constants, clean))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_seed_read":
        from . import cons_seed
        inspection._params(params, ("deck", "face_ids", "cons_ids"))
        result = cons_seed.capture(state, base, deck, params["face_ids"], params["cons_ids"])
        return structured_evidence.store_report(state, deck, "mesh_seed_read", result)
    if tool == "geometry_features":
        from .geometry_features import capture
        inspection._params(params, ("deck", "face_ids", "curve_ids", "seed_options"), ("sweep_options",))
        result = capture(state, base, deck, params["face_ids"], params["curve_ids"], params["seed_options"],
                         params.get("sweep_options"))
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "geometry_sweep_candidates":
        from . import geometry_sweep_candidates
        inspection._params(params, ("deck", "face_ids", "target_length"))
        result = geometry_sweep_candidates.capture(state, base, deck, params["face_ids"], params["target_length"])
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_map_block_inspect":
        from ansa import mesh
        from . import map_block_inspection
        inspection._params(params, ("deck", "volume_ids"))
        result = map_block_inspection.capture(state, base, mesh, constants, deck, params["volume_ids"])
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_provenance":
        return _provenance(state, deck, params)
    if tool == "mesh_seed_plan":
        from .mesh_seed import recommend_seeds
        inspection._params(params,("deck","plan"))
        result=recommend_seeds(params["plan"])
        result["plan_sha256"]=_digest(params["plan"])
        return structured_evidence.store_report(state,deck,tool,result)
    if tool == "mesh_sweep_prepare":
        from .structured_batch import prepare
        inspection._params(params,("deck","blocks","max_penta_fraction"))
        result=prepare(base,constants,deck,params["blocks"],max_penta_fraction=params["max_penta_fraction"])
        return structured_evidence.store_report(state,deck,tool,result)
    if tool == "mesh_block_plan":
        from .mesh_planning import plan_blocks
        inspection._params(params, ("deck", "plan"))
        result = plan_blocks(params["plan"])
        result.update(input_origin="caller_declared_plan", model_geometry_verified=False,
                      plan_sha256=_digest(params["plan"]))
        return structured_evidence.store_report(state, deck, tool, result)
    from . import structured_native
    if tool in {"geometry_partition_apply", "mesh_sweep", "geometry_boundary_prepare", "mesh_seed_apply", "mesh_cad_map_apply", "mesh_map_block_apply", "mesh_map_block_fill_apply", "mesh_map_block_caps_apply", "mesh_map_blocks_apply", "mesh_annular_source_apply"}:
        return _with_operation_ledger(state, deck, tool, params)
    if tool == "geometry_partition_plan":
        return structured_native.execute(state, tool, params)
    if tool == "mesh_source_audit":
        from .mesh_planning import audit_source
        inspection._params(params, ("deck", "source_ids", "target_ids", "direction", "layers", "target_length"), ("annular_options",))
        snapshot = structured_native.capture_source(base, constants, deck,
                        params["source_ids"] + (params["target_ids"] or []))
        normalized = {**snapshot,
                      "nodes": {row["id"]: {"position": row["xyz"]} for row in snapshot["nodes"]},
                      "elements": [{"entity": {"type": "SHELL", "id": row["id"]},
                                    "element_type": row["type"], "node_ids": row["nodes"]}
                                   for row in snapshot["elements"]]}
        result = audit_source(normalized, source_ids=params["source_ids"],
                    target_ids=params["target_ids"] or [], direction=params["direction"],
                    layers=params["layers"], target_length=params["target_length"])
        from .structured_batch import source_subset
        source_snapshot = snapshot if not params["target_ids"] else source_subset(snapshot,params["source_ids"])
        if params.get("annular_options") is not None:
            from . import annular_source
            template = annular_source.audit(source_snapshot, params["annular_options"])
            result["annular_template"] = template
            result.setdefault("summary", {})["annular_template_status"] = template["status"]
            if template["status"] != "verified_mesh_template":
                result["status"] = "failed"
                result.setdefault("issues", []).append({"code":"annular_template_unsupported",
                    "message":template.get("reason", "annular FE source is unsupported")})
        result.update(snapshot_sha256=snapshot["snapshot_sha256"],
                      source_snapshot_sha256=source_snapshot["snapshot_sha256"], input_origin="ANSA_main_thread")
        return structured_evidence.store_report(state, deck, tool, result)
    if tool == "mesh_interface_audit":
        from .mesh_audit import audit_snapshot, _combine
        inspection._params(params, ("deck", "entity_ids", "interfaces", "protected_cylinders",
                                    "expected_boundary", "expected_components"), ("cad_boundary",))
        snapshot = structured_native.capture_mesh(base, constants, deck, params["entity_ids"])
        boundary, cylinders, cad = params["expected_boundary"], params["protected_cylinders"], None
        audit_input = snapshot
        if params.get("cad_boundary") is not None:
            if boundary is not None or cylinders is not None:
                raise ValueError("cad_boundary cannot be mixed with caller-declared boundary/cylinder facets")
            from ansa import mesh, calc
            from . import structured_boundary
            cad = structured_boundary.capture(base, mesh, calc, constants, deck, snapshot, params["cad_boundary"])
            audit_input = cad.pop("audit_snapshot")
            boundary, cylinders = cad["expected_boundary"], cad.pop("protected_cylinders")
        result = audit_snapshot(audit_input, interfaces=params["interfaces"],
                    protected_cylinders=cylinders,
                    expected_boundary=boundary,
                    expected_components=params["expected_components"])
        result.update(snapshot_sha256=snapshot["snapshot_sha256"], input_origin="ANSA_main_thread",
                      regeneration_status="incomplete", geometry_membership_origin="caller_declared_facets")
        if cad is not None:
            result["sections"]["cad_boundary"] = cad
            result["geometry_membership_origin"] = cad["membership_origin"]
            geometry = result["sections"]["geometry"]
            memberships = {row["label"]: row for row in cad["cylinder_membership"]}
            for cylinder in geometry["cylinders"]:
                source = memberships[cylinder["label"]]
                cylinder.update(source_face_ids=source["face_ids"], membership_status=source["status"],
                    measured_nodes_status=cylinder["status"])
                cylinder["status"] = _combine(cylinder["status"], source["status"])
                if source["status"] != "pass":
                    result["diagnostics"].append({"section": "geometry", "severity": source["status"],
                        "code": "cad_cylinder_membership_unverified", "label": cylinder["label"],
                        "face_ids": source["face_ids"]})
            radial_status = _combine("pass", *(row["status"] for row in geometry["cylinders"]))
            result["geometry_status"] = _combine(result["geometry_status"], cad["membership"]["status"],
                radial_status, cad["projection"]["status"], cad["continuous_cad_coverage_status"])
            result["topology_status"] = _combine(result["topology_status"], cad["membership"]["status"])
            result["status"] = _combine(result["status"], result["geometry_status"], result["topology_status"])
            result["sections"]["geometry"]["radial_checks_status"] = radial_status
            result["sections"]["geometry"]["status"] = result["geometry_status"]
            result["sections"]["topology"]["status"] = result["topology_status"]
            result["summary"].update(cad_membership_status=cad["membership"]["status"],
                cad_nodal_projection_status=cad["projection"]["status"], continuous_cad_coverage_proven=False)
        return structured_evidence.store_report(state, deck, tool, result)
    raise ValueError("unsupported structured mesh operation")
