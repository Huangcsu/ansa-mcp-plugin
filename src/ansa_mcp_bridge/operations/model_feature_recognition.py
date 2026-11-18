"""显式 Part 的受控原生特征识别及独立范围回读。"""

from collections import Counter
from pathlib import Path
import hashlib
import math
import uuid

from ansa_mcp_shared.feature_recognition import (
    MODELLING_VALUES, RECOGNITION_OPTIONS, validate_recognition_request,
)

from . import _params, _component_has_reparse_point, entity_catalog as catalog, model_features as reads
from . import structured_evidence as evidence, structured_native as native, volume_meshing as volume
from .structured_meshing import _bounded_operation_result


_CATEGORY_TOGGLE = {
    "FILLET": "recognize_fillets",
    "CHAMFER": "recognize_chamfers",
    "HOLE 2D": "recognize_holes_2d",
    "HOLE 3D": "recognize_holes_3d",
    "FLANGE 2D": "recognize_flanges_2d",
    "FLANGE 3D": "recognize_flanges_3d",
    "LOGO": "recognize_logos",
    "STAMP": "recognize_stamps",
    "RIB 2D": "recognize_ribs_2d",
    "SPECIAL PERIMETER": "recognize_special_perimeters",
    "FASTENER": "recognize_fasteners",
}
_TOGGLES = (
    *_CATEGORY_TOGGLE.values(), "recognize_sharp_perimeters", "recognize_leading_perimeters",
    "recognize_free_perimeters", "recognize_trailing",
)
_FIXED = {
    "recognize_holes_2d_external_perimeters": False,
    "recognize_holes_2d_filled": False,
    "recognize_holes_2d_connections": False,
    "recognize_holes_2d_proximity": False,
    "washers_recognition_strictness_level": "Strict",
    "recognize_holes_3d_connections": False,
    "recognize_flanges_2d_connections": False,
    "recognize_flanges_2d_proximity": False,
    "recognize_flanges_3d_proximity": False,
    "recognize_fasteners_in_parts_with_existing": False,
}
_SETTING_KEYS = tuple(dict.fromkeys((
    *_TOGGLES, *(name for values in RECOGNITION_OPTIONS.values() for name in values.values()),
    *_FIXED, "modelling_mesh_type", "mesh_change_option", "element_type",
    "element_order", "target_element_length",
)))


def _hash(value):
    return hashlib.sha256(evidence._encoded(value)).hexdigest()


def _clean(params, applying):
    required = ("deck", "part_ids", "feature_types", "parameters")
    _params(params, (*required, "expected_plan_sha256", "backup_path") if applying else required,
            ("modelling", "input_kind"))
    reads._validate("model_features_status", {k: params[k] for k in ("deck", "part_ids")})
    kinds = params["feature_types"]
    modelling = params.get("modelling")
    input_kind = params.get("input_kind", "cad")
    values = validate_recognition_request(kinds, params["parameters"], modelling, input_kind=input_kind)
    clean = {"deck": params["deck"], "part_ids": sorted(params["part_ids"]),
             "feature_types": sorted(kinds), "parameters": values}
    if modelling is not None:
        clean["modelling"] = modelling
    if input_kind != "cad":
        clean["input_kind"] = input_kind
    if applying:
        native._validate_digest(params["expected_plan_sha256"], "expected_plan_sha256")
        if type(params["backup_path"]) is not str or not params["backup_path"]:
            raise ValueError("backup_path is required")
    return clean


def _settings(base):
    values = base.BCSettingsGetValues(tuple(_SETTING_KEYS))
    if type(values) is not dict or any(key not in values or values[key] is None for key in _SETTING_KEYS):
        raise RuntimeError("recognition/global settings are not completely readable")
    return {key: catalog._json_value(values[key], base, base.CurrentDeck()) for key in _SETTING_KEYS}


def _canonical(value):
    if type(value) is str:
        text = value.strip().casefold()
        if text in ("true", "false"):
            return text == "true"
        try:
            number = float(text)
        except ValueError:
            return value
        return number if math.isfinite(number) else value
    return value


def _settings_match(actual, expected):
    return all(key in actual and type(_canonical(actual[key])) is type(_canonical(value))
               and _canonical(actual[key]) == _canonical(value) for key, value in expected.items())


def _requested(clean):
    values = {key: False for key in _TOGGLES}
    values.update(_FIXED)
    if "modelling" in clean:
        values["modelling_mesh_type"] = MODELLING_VALUES[clean["modelling"]]
    for kind in clean["feature_types"]:
        values[_CATEGORY_TOGGLE[kind]] = True
        for key, native_key in RECOGNITION_OPTIONS[kind].items():
            values[native_key] = clean["parameters"][key]
    return values


def _capture_shell_perimeters(base, mesh, deck, scope):
    rows = native._scope_map(scope)
    perimeters = volume._collect(base, deck, "FE PERIMETER", expected_type="FE PERIMETER")
    if {ref["id"] for _, ref in perimeters} != {ident for kind, ident in rows if kind == "FE PERIMETER"}:
        raise RuntimeError("FE perimeter inventory differs from the model scope")
    for perimeter, ref in perimeters:
        shells = mesh.GetFEPerimeterShells([perimeter])
        if type(shells) not in (list, tuple) or not shells:
            raise RuntimeError("FE perimeter has no readable adjacent shells")
        shell_ids = {catalog._entity_reference(base, deck, shell, "SHELL")["id"] for shell in shells}
        if any(("SHELL", ident) not in rows for ident in shell_ids):
            raise RuntimeError("FE perimeter has adjacent shells outside the captured model")
        rows[("FE PERIMETER", ref["id"])]["adjacent_shell_ids"] = sorted(shell_ids)


def _perimeter_part_ids(rows, perimeter):
    return {rows[("SHELL", ident)]["part_id"] for ident in perimeter["adjacent_shell_ids"]}


def _capture(base, deck, *, shell_mesh_api=None):
    scope = native._capture_scope(base, deck)
    if shell_mesh_api is not None:
        _capture_shell_perimeters(base, shell_mesh_api, deck, scope)
    parts = [(catalog._get_entity(base, deck, "ANSAPART", row["entity"]["id"]))
             for row in scope["entities"] if row["entity"]["type"] == "ANSAPART"]
    if not parts:
        raise ValueError("recognition requires existing Parts")
    handler = base.FeatureHandler([part for part, _ in parts])
    rows, entries = reads._inventory(base, deck, parts, handler)
    if any(row["query_status"] != "ok" or row["errors"] for row in rows):
        raise RuntimeError("Part status/Feature inventory is incomplete")
    features = []
    for part_id, ref, entity in entries:
        card = volume._read_full_card(base, deck, entity, ref, "Feature")
        members = volume._collect(base, deck, "__ALL_ENTITIES__", container=entity)
        if not members:
            raise RuntimeError("Feature contents are not readable; preservation cannot be proven")
        features.append({"part_id": part_id, **card, "contents": [reference for _, reference in members],
                         "contents_read_method": "CollectEntities(feature, __ALL_ENTITIES__)"})
    result = {"scope": scope, "parts": rows, "features": features}
    if len(evidence._encoded(result)) > 24 * 1024 * 1024:
        raise ValueError("recognition evidence exceeds 24 MiB")
    return result


def _fastener_library_identity():
    from ansa import dm

    get_root = getattr(dm, "GetRoot", None)
    query = getattr(dm, "QueryDMObjects", None)
    if not callable(get_root) or not callable(query):
        raise NotImplementedError("FASTENER recognition requires ANSA DM root and library query APIs")
    root = get_root()
    if type(root) is not str or not root.strip():
        raise ValueError("FASTENER recognition requires a configured ANSA DM root with a Fastener library")
    library = query([["Representation", "equals", "common"]], type="Fastener", dm_root=root)
    if type(library) is not dict or type(library.get("error")) is not int or library["error"] != 0:
        raise ValueError("FASTENER recognition could not verify the ANSA DM Fastener library")
    entries = library.get("output")
    if type(entries) is not list or not entries:
        raise ValueError("FASTENER recognition requires a populated DM Fastener common representation")
    return _hash(root)


def _prepare(state, base, constants, clean, *, shell_mesh_api=None):
    deck = catalog._deck(base, clean)
    native._check_nastran(constants, deck)
    for name in ("FeatureHandler", "BCSettingsGetValues", "BCSettingsSetValues"):
        if not callable(getattr(base, name, None)):
            raise NotImplementedError("required ANSA API is unavailable: " + name)
    context = volume._execution_context(state, deck)
    fastener_dm_identity = _fastener_library_identity() if "FASTENER" in clean["feature_types"] else None
    shell_mesh = clean.get("input_kind") == "shell_mesh"
    if shell_mesh and not callable(getattr(shell_mesh_api, "GetFEPerimeterShells", None)):
        raise NotImplementedError("shell_mesh recognition requires ANSA mesh.GetFEPerimeterShells")
    for part_id in clean["part_ids"]:
        part, _ = catalog._get_entity(base, deck, "ANSAPART", part_id)
        if shell_mesh:
            if volume._collect(base, deck, "FACE", container=part, expected_type="FACE"):
                raise ValueError("shell_mesh input requires selected Parts without CAD FACE contents")
            elements = volume._collect(base, deck, "__ELEMENTS__", container=part)
            if not elements or any(ref["type"] != "SHELL" for _, ref in elements):
                raise ValueError("shell_mesh input requires selected Parts containing only shell elements")
            continue
        if not volume._collect(base, deck, "FACE", container=part, expected_type="FACE"):
            raise ValueError("selected Part has no explicit CAD FACE contents")
        if volume._collect(base, deck, "__ELEMENTS__", container=part):
            raise ValueError("first recognition contract requires CAD-only selected Parts")
    before = _capture(base, deck, shell_mesh_api=shell_mesh_api)
    part_ids = set(clean["part_ids"])
    feature_types = set(clean["feature_types"])
    inactive = sorted({
        (row["part_id"], row["entity"]["type"])
        for row in before["features"]
        if row["part_id"] in part_ids and row["entity"]["type"] not in feature_types
    })
    if inactive:
        details = ", ".join(f"Part {part_id}: {kind}" for part_id, kind in inactive[:8])
        raise ValueError(
            "ANSA recognition can clear inactive Feature types on selected Parts. "
            "Include their existing categories or choose different Parts: " + details
        )
    settings = _settings(base)
    binding = {"request": clean, "context": context, "before_sha256": _hash(before), "settings": settings}
    if fastener_dm_identity is not None:
        binding["fastener_dm_identity"] = fastener_dm_identity
    return {"before": before, "settings": settings, "context": context, "plan_sha256": _hash(binding)}


def _transition(before, after, clean):
    errors = []
    selected = set(clean["part_ids"])
    old_rows, new_rows = native._scope_map(before["scope"]), native._scope_map(after["scope"])
    old_features = {(r["part_id"], r["entity"]["type"], r["entity"]["id"]): r for r in before["features"]}
    new_features = {(r["part_id"], r["entity"]["type"], r["entity"]["id"]): r for r in after["features"]}
    for key, row in old_features.items():
        if new_features.get(key) != row:
            errors.append("existing Feature changed or disappeared: " + str(key))
    created = [row for key, row in new_features.items() if key not in old_features]
    allowed = set()
    for row in created:
        ref = row["entity"]
        if row["part_id"] not in selected or ref["type"] not in clean["feature_types"]:
            errors.append("Feature created outside the requested Part/category")
        else:
            allowed.add((ref["type"], ref["id"]))
        if ref["type"] == "HOLE 2D" and row["fields"].get("Shape") != "Round":
            errors.append("HOLE 2D result is outside the validated round-hole shape")
    for key, row in old_rows.items():
        actual = new_rows.get(key)
        if key[0] == "ANSAPART" and key[1] in selected and actual is not None:
            row = {**row, "fields": {k: v for k, v in row["fields"].items() if k != "Features Status"}}
            actual = {**actual, "fields": {k: v for k, v in actual["fields"].items() if k != "Features Status"}}
        if actual != row:
            errors.append("protected model entity changed or disappeared: " + str(key))
    created_fe_perimeters = []
    for key in sorted(new_rows.keys() - old_rows.keys()):
        if key[0] == "FE PERIMETER" and clean.get("input_kind") == "shell_mesh":
            row = new_rows[key]
            owners = _perimeter_part_ids(new_rows, row)
            if owners and owners <= selected:
                created_fe_perimeters.append({
                    "entity": row["entity"], "adjacent_shell_ids": row["adjacent_shell_ids"],
                    "part_ids": sorted(owners),
                })
                continue
            errors.append("created FE perimeter has unverified or unselected Part ownership: " + str(key))
            continue
        if key not in allowed:
            errors.append("unexpected model entity was created: " + str(key))
    for row in after["parts"]:
        if row["part"]["id"] in selected and row["features_status"] != "Recognized":
            errors.append("selected Part did not reach Recognized status")
    result = {"scope_verified": not errors, "errors": errors, "created_features": created,
              "created_counts_by_type": dict(sorted(Counter(r["entity"]["type"] for r in created).items()))}
    if clean.get("input_kind") == "shell_mesh":
        result["created_fe_perimeters"] = created_fe_perimeters
    return result


def _verify_created_member_scope(base, deck, created, part_ids, *, shell_scope=None):
    allowed = set(part_ids)
    owners = {}
    for feature in created:
        for member in feature["contents"]:
            kind, ident = member["type"], member["id"]
            key = (kind, ident)
            if key not in owners:
                if shell_scope is not None and kind == "FE PERIMETER":
                    owners[key] = _perimeter_part_ids(shell_scope, shell_scope[key])
                else:
                    entity, _ = catalog._get_entity(base, deck, kind, ident)
                    if kind == "CONS":
                        entities = base.GetFacesOfCons(entity)
                    elif kind == "HOT POINT":
                        entities = base.GetFacesOfHotPoints(entity)
                    else:
                        entities = [entity]
                    if type(entities) not in (list, tuple) or not entities:
                        raise RuntimeError(f"Feature member {key} has no verifiable Part ownership")
                    owners[key] = {
                        catalog._entity_reference(base, deck, base.GetEntityPart(item), "ANSAPART")["id"]
                        for item in entities
                    }
            if not owners[key]:
                raise RuntimeError(f"Feature member {key} has no verifiable Part ownership")
            if not owners[key] <= allowed:
                raise RuntimeError(f"Feature member {key} belongs to an unselected Part")


def _apply(state, base, constants, mesh, clean, params, prepared, *, shell_mesh_api=None):
    deck, before = clean["deck"], prepared["before"]
    if prepared["plan_sha256"] != params["expected_plan_sha256"]:
        raise ValueError("recognition plan is stale or request/settings differ")
    backup_path, current_path, fingerprint = native._backup_preflight(base, params)
    result = {"ok": False, "mode": "apply", "read_only": False, "api_called": False,
              "possible_effects": False, "recovery_required": False, "scope_verified": False,
              "plan_sha256": prepared["plan_sha256"], "part_ids": clean["part_ids"],
              "feature_types": clean["feature_types"], "modelling": clean.get("modelling"),
              "input_kind": clean.get("input_kind", "cad"),
              "errors": [], "before": before,
              "parameters_sha256": _hash(_requested(clean)), "settings_before": prepared["settings"]}
    parameter_path = Path(state.session_dir) / ("features-parameters-" + uuid.uuid4().hex + ".ansa_mpar")
    settings_touched = False
    try:
        volume._same_context(state, deck, prepared["context"])
        result["backup"] = native._verify_backup(base, constants, backup_path, current_path, fingerprint)
        result["possible_effects"] = True  # backup/config/native errors require independent evidence.
        rechecked = _prepare(state, base, constants, clean, shell_mesh_api=shell_mesh_api)
        if rechecked["plan_sha256"] != prepared["plan_sha256"]:
            raise RuntimeError("recognition preconditions changed after backup")
        if _component_has_reparse_point(parameter_path) or parameter_path.exists():
            raise ValueError("unsafe mesh parameter backup path")
        saved = mesh.SaveMeshParams(str(parameter_path))
        if type(saved) is not int or saved != 1 or not parameter_path.is_file() or not parameter_path.stat().st_size:
            raise RuntimeError("SaveMeshParams did not produce a nonempty parameter snapshot")
        result["parameter_backup"] = str(parameter_path)
        result["parameter_backup_sha256"] = hashlib.sha256(parameter_path.read_bytes()).hexdigest()
        requested = _requested(clean)
        settings_touched = True
        rc = base.BCSettingsSetValues(requested)
        if type(rc) is not int or rc != 0:
            raise RuntimeError("recognition settings were not accepted")
        result["settings_used"] = _settings(base)
        if not _settings_match(result["settings_used"], requested):
            raise RuntimeError("effective recognition parameters differ from the request")
        volume._same_context(state, deck, prepared["context"])
        parts = [catalog._get_entity(base, deck, "ANSAPART", i)[0] for i in clean["part_ids"]]
        handler = base.FeatureHandler(parts)
        result["api_called"] = True
        result["api_return"] = handler.recognize(lock_fe_perimeters=True)
        if result["api_return"] is not None:
            raise RuntimeError("recognize returned an unexpected result; independent readback is still required")
    except Exception as exc:
        result["errors"].append(reads._error(exc))
        if "backup" not in result:
            result["backup"] = native._unconfirmed_backup(backup_path)
            result["possible_effects"] = True
    finally:
        if settings_touched:
            result["mesh_parameters_restored"] = False
            try:
                restored = mesh.ReadMeshParams(str(parameter_path))
                if type(restored) is not int or restored != 1:
                    raise RuntimeError("ReadMeshParams did not confirm restoration")
                result["mesh_parameters_restored"] = True
            except Exception as exc:
                result["errors"].append("mesh parameter restoration: " + reads._error(exc))
            try:
                rc = base.BCSettingsSetValues(prepared["settings"])
                if type(rc) is not int or rc != 0:
                    raise RuntimeError("BCSettings restoration failed")
                result["settings_after"] = _settings(base)
                result["settings_restored"] = (result["mesh_parameters_restored"]
                                               and _settings_match(result["settings_after"], prepared["settings"]))
                if not result["settings_restored"]:
                    raise RuntimeError("settings readback differs after restoration")
            except Exception as exc:
                result["settings_restored"] = False
                result["errors"].append("settings restoration: " + reads._error(exc))
        else:
            result["settings_restored"] = True
    try:
        after = _capture(base, deck, shell_mesh_api=shell_mesh_api)
        result["after"] = after
        change = _transition(before, after, clean)
        result.update({key: value for key, value in change.items() if key != "errors"})
        result["errors"].extend(change["errors"])
        shell_scope = native._scope_map(after["scope"]) if shell_mesh_api is not None else None
        _verify_created_member_scope(base, deck, change["created_features"], clean["part_ids"], shell_scope=shell_scope)
        volume._same_context(state, deck, prepared["context"])
    except Exception as exc:
        result["scope_verified"] = False
        result["errors"].append("post-recognition readback: " + reads._error(exc))
    result["ok"] = not result["errors"] and result["api_called"] and result["scope_verified"]
    result["possible_effects"] = not result["ok"] and result["possible_effects"]
    result["recovery_required"] = result["possible_effects"]
    result["error"] = "; ".join(result["errors"])[:4096] or None
    result["summary"] = {"ok": result["ok"], "settings_restored": result["settings_restored"],
                         "created_counts_by_type": result.get("created_counts_by_type", {}),
                         "scope_verified": result["scope_verified"]}
    if shell_mesh_api is not None:
        result["summary"]["created_fe_perimeter_count"] = len(result.get("created_fe_perimeters", ()))
    if result["ok"]:
        selected = set(clean["part_ids"])
        matches = Counter(
            row["entity"]["type"] for row in result["after"]["features"] if row["part_id"] in selected
        )
        result["summary"]["observed_matches_by_type"] = {kind: matches[kind] for kind in clean["feature_types"]}
    return _bounded_operation_result(state, deck, result)


def execute(state, tool, params):
    state.assert_main()
    if tool not in {"model_features_recognize_plan", "model_features_recognize"}:
        raise ValueError("unsupported Feature recognition operation")
    applying = tool == "model_features_recognize"
    clean = _clean(params, applying)
    from ansa import base, constants, mesh
    shell_mesh_api = mesh if clean.get("input_kind") == "shell_mesh" else None
    prepared = _prepare(state, base, constants, clean, shell_mesh_api=shell_mesh_api)
    if applying:
        return _apply(state, base, constants, mesh, clean, params, prepared, shell_mesh_api=shell_mesh_api)
    return {"ok": True, "read_only": True, "mode": "plan", "usable_for_mutation": False,
            "plan_sha256": prepared["plan_sha256"], "part_ids": clean["part_ids"],
            "feature_types": clean["feature_types"], "parameters": clean["parameters"],
            "modelling": clean.get("modelling"),
            "input_kind": clean.get("input_kind", "cad"),
            "parameters_sha256": _hash(_requested(clean)), "settings_to_apply": _requested(clean),
            "source_sha256": _hash(prepared["before"]), "context": prepared["context"],
            "existing_feature_count": len(prepared["before"]["features"]),
            "lock_fe_perimeters": True, "existing_features_policy": "preserve_and_verify"}


__all__ = ["execute"]
