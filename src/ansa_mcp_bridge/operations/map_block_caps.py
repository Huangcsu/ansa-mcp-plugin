"""受限地为现有 MapBlock 设置 master/slave caps 并独立回读。"""
from pathlib import Path

from . import cad_map, cons_seed as seeds, geometry_features as features, inspection
from . import map_block_inspection, meshing_context, structured_native as native
from . import volume_meshing as guards


_PARAMS = ("deck", "volume_id", "master_face_ids", "slave_face_ids")
_MAX_MAP_BLOCKS = 20
_MAX_BOUNDARY_FACES = 100
_MAX_BOUNDARY_SHELLS = 5000
_MAX_BOUNDARY_GRIDS = 10000
_MAX_MODEL_ENTITIES = 10000
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_CAP_METADATA_FIELDS = frozenset({
    "cap1", "cap2", "cap1face", "cap2face", "cap1faces", "cap2faces",
    "cap1faceid", "cap2faceid", "cap1faceids", "cap2faceids",
    "cap1facelist", "cap2facelist", "master", "slave", "round",
    "masterface", "slaveface", "roundface",
    "masterfaces", "slavefaces", "roundfaces",
    "masterfaceid", "slavefaceid", "roundfaceid",
    "masterfaceids", "slavefaceids", "roundfaceids",
    "masterfacelist", "slavefacelist", "roundfacelist",
})


def normalize(params, applying=False):
    optional = ("expected_plan_sha256", "backup_path") if applying else ()
    inspection._params(params, _PARAMS + optional)
    clean = {
        "deck": native._positive_int(params["deck"], "deck"),
        "volume_id": native._positive_int(params["volume_id"], "volume_id"),
    }
    for name in ("master_face_ids", "slave_face_ids"):
        values = native._id_list(params[name], name, _MAX_BOUNDARY_FACES)
        if len(values) != len(set(values)):
            raise ValueError(name + " must contain unique FACE IDs")
        clean[name] = sorted(values)
    if set(clean["master_face_ids"]) & set(clean["slave_face_ids"]):
        raise ValueError("master_face_ids and slave_face_ids must be disjoint")
    if applying:
        clean["expected_plan_sha256"] = native._validate_digest(
            params["expected_plan_sha256"], "expected_plan_sha256"
        )
        clean["backup_path"] = params["backup_path"]
    return clean


def _capture(state, base, mesh, constants, deck):
    state.assert_main()
    native._check_nastran(constants, deck)
    if base.CurrentDeck() != deck:
        raise ValueError("requested deck must be active")

    context = guards._execution_context(state, deck)
    model_context = features._context(state, base, deck)
    if model_context.get("units") != "millimeter":
        raise ValueError("MapBlock caps require a NASTRAN millimeter model")
    database = model_context.get("database")
    if type(database) is not str or not database or not Path(database).is_file():
        raise ValueError("MapBlock caps require an already saved model")

    volumes = seeds._collect(base, deck, None, "VOLUME")
    if type(volumes) not in (list, tuple) or not 1 <= len(volumes) <= _MAX_MAP_BLOCKS:
        raise ValueError("scope must contain 1..20 existing MapBlock VOLUMEs")
    volume_ids = sorted(native._positive_int(entity._id, "VOLUME ID") for entity in volumes)
    if len(volume_ids) != len(set(volume_ids)):
        raise ValueError("VOLUME inventory contains duplicate IDs")

    # This read-only whole-model snapshot is bounded and protects FE cards,
    # GRID positions, CAD definitions, frozen states, and unrelated entities.
    scope = cad_map._snapshot(state, base, deck)
    if len(scope.get("entities", ())) > _MAX_MODEL_ENTITIES:
        raise ValueError("model exceeds the 10000-entity protection budget")
    if len(guards._encoded(scope)) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("model snapshot exceeds the 8 MiB protection budget")

    blocks = map_block_inspection.capture(state, base, mesh, constants, deck, volume_ids)
    summary = blocks["summary"]
    if (summary["face_count"] > _MAX_BOUNDARY_FACES
            or summary["surface_shell_count"] > _MAX_BOUNDARY_SHELLS
            or summary["surface_node_count"] > _MAX_BOUNDARY_GRIDS):
        raise ValueError("MapBlock boundary exceeds the FACE/SHELL/GRID evidence budget")
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)
    if features._context(state, base, deck) != model_context:
        raise RuntimeError("model context changed during MapBlock caps capture")
    return {
        "context": context,
        "model_context": model_context,
        "scope": scope,
        "blocks": blocks,
        "settings": settings,
    }


def preflight(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize({name: params[name] for name in _PARAMS})
    deck = inspection._deck(base, clean)
    native._check_nastran(constants, deck)
    for name in ("DefineCaps", "GetVolumeBoundary", "GetMapVolumeBoundaries"):
        if not callable(getattr(mesh, name, None)):
            raise NotImplementedError("required native API unavailable: " + name)

    captured = _capture(state, base, mesh, constants, deck)
    volume_rows = {row["volume_id"]: row for row in captured["blocks"]["volumes"]}
    target = volume_rows.get(clean["volume_id"])
    if target is None:
        raise ValueError("volume_id must identify an existing MapBlock")
    fields = target["card"]["fields"]
    if (target["native_mapping_status"] != "native_ready"
            or fields.get("State") != "Ready to Mesh"
            or fields.get("Status") != "Unmeshed"
            or fields.get("Frozen") != "NO"):
        raise ValueError("target MapBlock must be Ready to Mesh, Unmeshed, and unfrozen")

    scope_rows = native._scope_map(captured["scope"])
    if any(kind == "SOLID" for kind, _ident in scope_rows):
        raise ValueError("MapBlock caps require an unfilled model with no existing SOLID")
    target_scope = scope_rows.get(("VOLUME", clean["volume_id"]))
    if target_scope is None:
        raise ValueError("target MapBlock is missing from the complete model snapshot")
    if target_scope.get("solid_ids") != []:
        raise ValueError("target MapBlock must not contain already-filled SOLID elements")

    boundary = set(target["face_ids"])
    master = set(clean["master_face_ids"])
    slave = set(clean["slave_face_ids"])
    if not master or not slave or not master <= boundary or not slave <= boundary:
        raise ValueError("master/slave FACE IDs must be nonempty subsets of the target boundary")
    remaining = sorted(boundary - master - slave)
    if not remaining:
        raise ValueError("master/slave caps must leave at least one round FACE")
    requested = {
        "master": sorted(master),
        "slave": sorted(slave),
        "round": remaining,
    }
    body = {
        "params": clean,
        "context": captured["context"],
        "model_context": captured["model_context"],
        "scope_sha256": features._hash(captured["scope"]),
        "map_blocks_sha256": captured["blocks"]["source_snapshot_sha256"],
        "caps_before": target["caps"],
        "caps_requested": requested,
        "meshing_context_sha256": captured["settings"]["snapshot_sha256"],
    }
    return {
        "clean": clean,
        "context": captured["context"],
        "capture": captured,
        "target": target,
        "caps_requested": requested,
        "plan_sha256": features._hash(body),
    }


def public_plan(prepared):
    captured = prepared["capture"]
    return {
        "status": "ready_for_controlled_apply",
        "params": prepared["clean"],
        "plan_sha256": prepared["plan_sha256"],
        "target_volume_id": prepared["clean"]["volume_id"],
        "caps_before": prepared["target"]["caps"],
        "caps_requested": prepared["caps_requested"],
        "scope_sha256": features._hash(captured["scope"]),
        "map_blocks_sha256": captured["blocks"]["source_snapshot_sha256"],
        "meshing_context_sha256": captured["settings"]["snapshot_sha256"],
        "budget": {
            "map_blocks": _MAX_MAP_BLOCKS,
            "boundary_faces": _MAX_BOUNDARY_FACES,
            "boundary_shells": _MAX_BOUNDARY_SHELLS,
            "boundary_grids": _MAX_BOUNDARY_GRIDS,
            "model_entities": _MAX_MODEL_ENTITIES,
            "snapshot_bytes": _MAX_SNAPSHOT_BYTES,
        },
        "usable_for_mutation": False,
    }


def validate_report(state, base, mesh, constants, deck, result):
    params = result.get("params")
    if type(params) is not dict or params.get("deck") != deck:
        raise ValueError("MapBlock caps plan evidence is malformed")
    current = preflight(state, base, mesh, constants, params)
    if current["plan_sha256"] != result.get("plan_sha256"):
        raise ValueError("MapBlock caps plan evidence is stale")


def _is_cap_metadata_field(name):
    normalized = "".join(character for character in str(name).casefold() if character.isalnum())
    return normalized in _CAP_METADATA_FIELDS


def _assert_scope_unchanged_except_target_caps(before, after, volume_id):
    if type(before) is not dict or type(after) is not dict:
        raise RuntimeError("complete model snapshots are unavailable")
    if set(before) != set(after):
        raise RuntimeError("model snapshot sections changed")
    if before.get("deck") != after.get("deck"):
        raise RuntimeError("model deck changed")
    before_rows = native._scope_map(before)
    after_rows = native._scope_map(after)
    if before_rows.keys() != after_rows.keys():
        raise RuntimeError("model entity inventory changed")
    for key in before_rows:
        old, new = before_rows[key], after_rows[key]
        if key != ("VOLUME", volume_id):
            if old != new:
                raise RuntimeError("protected model entity changed: " + str(key))
            continue
        if set(old) != set(new):
            raise RuntimeError("target VOLUME card structure changed")
        for name in old:
            if name != "fields" and old[name] != new[name]:
                raise RuntimeError("target VOLUME non-cap metadata changed: " + name)
        old_fields, new_fields = old.get("fields"), new.get("fields")
        if type(old_fields) is not dict or type(new_fields) is not dict:
            raise RuntimeError("target VOLUME card fields are unavailable")
        for field in set(old_fields) | set(new_fields):
            if old_fields.get(field) != new_fields.get(field) and not _is_cap_metadata_field(field):
                raise RuntimeError("target VOLUME non-cap field changed: " + str(field))
    for name in before:
        if name != "entities" and before[name] != after[name]:
            raise RuntimeError("protected model snapshot section changed: " + name)


def _assert_postconditions(prepared, after):
    before_capture = prepared["capture"]
    clean = prepared["clean"]
    volume_id = clean["volume_id"]
    _assert_scope_unchanged_except_target_caps(
        before_capture["scope"], after["scope"], volume_id
    )
    if before_capture["context"] != after["context"]:
        raise RuntimeError("model context changed during DefineCaps")
    if before_capture["model_context"] != after["model_context"]:
        raise RuntimeError("saved model identity or units changed during DefineCaps")
    if (before_capture["settings"]["snapshot_sha256"]
            != after["settings"]["snapshot_sha256"]):
        raise RuntimeError("Mesh Parameters or Quality Criteria changed during DefineCaps")

    before_volumes = {row["volume_id"]: row for row in before_capture["blocks"]["volumes"]}
    after_volumes = {row["volume_id"]: row for row in after["blocks"]["volumes"]}
    if before_volumes.keys() != after_volumes.keys():
        raise RuntimeError("MapBlock inventory changed during DefineCaps")
    for section in ("face_membership", "surface_mesh", "cad_witness"):
        if before_capture["blocks"][section] != after["blocks"][section]:
            raise RuntimeError("protected MapBlock boundary/CAD data changed: " + section)
    for ident, old in before_volumes.items():
        new = after_volumes[ident]
        if ident != volume_id:
            if old["caps"] != new["caps"]:
                raise RuntimeError("caps changed on another MapBlock VOLUME " + str(ident))
            continue
        if old["face_ids"] != new["face_ids"] or old["part_id"] != new["part_id"]:
            raise RuntimeError("target MapBlock boundary or Part ownership changed")
        expected = prepared["caps_requested"]
        if new["caps"] != expected:
            raise RuntimeError("GetMapVolumeBoundaries did not return the requested caps")
        if new["caps"]["round"] != sorted(set(old["face_ids"]) - set(expected["master"]) - set(expected["slave"])):
            raise RuntimeError("target round cap differs from the original boundary remainder")
        fields = new["card"]["fields"]
        if (new["native_mapping_status"] != "native_ready"
                or new["caps_match_complete_boundary"] is not True
                or fields.get("State") != "Ready to Mesh"
                or fields.get("Status") != "Unmeshed"
                or fields.get("Frozen") != "NO"):
            raise RuntimeError("target MapBlock is no longer Ready to Mesh/Unmeshed/unfrozen")


def apply_prepared(state, base, mesh, constants, params, prepared):
    clean = normalize(params, applying=True)
    result = {
        "ok": False,
        "scope_verified": False,
        "api_called": False,
        "backup_attempted": False,
        "backup_verified": False,
        "possible_effects": False,
        "recovery_required": False,
        "target_volume_id": clean["volume_id"],
        "plan_sha256": prepared["plan_sha256"],
    }
    after = None
    try:
        plan_params = {name: clean[name] for name in _PARAMS}
        if (clean["expected_plan_sha256"] != prepared["plan_sha256"]
                or plan_params != prepared["clean"]):
            raise ValueError("MapBlock caps plan or parameters changed")
        guards._same_context(state, clean["deck"], prepared["context"])
        backup_args = native._backup_preflight(base, clean)
        result["backup_attempted"] = True
        result["backup"] = native._verify_backup(base, constants, *backup_args)
        result["backup_verified"] = True

        refreshed = preflight(state, base, mesh, constants, prepared["clean"])
        if refreshed["plan_sha256"] != prepared["plan_sha256"]:
            raise RuntimeError("model or configuration changed during backup; DefineCaps not called")
        prepared = refreshed
        guards._same_context(state, clean["deck"], prepared["context"])
        volume = features._entity(base, clean["deck"], "VOLUME", clean["volume_id"])
        master = [features._entity(base, clean["deck"], "FACE", ident)
                  for ident in clean["master_face_ids"]]
        slave = [features._entity(base, clean["deck"], "FACE", ident)
                 for ident in clean["slave_face_ids"]]
        result["api_called"] = True
        result["possible_effects"] = True
        native_result = mesh.DefineCaps(volume, master, slave)
        result["native_return_type"] = type(native_result).__name__
        result["native_return"] = (
            native_result if native_result is None or type(native_result) in (bool, int, float, str)
            else repr(native_result)[:256]
        )

        guards._same_context(state, clean["deck"], prepared["context"])
        after = _capture(state, base, mesh, constants, clean["deck"])
        result["after_scope_sha256"] = features._hash(after["scope"])
        result["caps_after"] = {
            row["volume_id"]: row["caps"] for row in after["blocks"]["volumes"]
        }
        _assert_postconditions(prepared, after)
        result.update(
            ok=True,
            scope_verified=True,
            possible_effects=False,
            recovery_required=False,
            status="verified",
        )
    except Exception as exc:
        result.update(
            ok=False,
            scope_verified=False,
            possible_effects=bool(result["api_called"] or (
                result["backup_attempted"] and not result["backup_verified"]
            )),
            recovery_required=bool(result["api_called"] or (
                result["backup_attempted"] and not result["backup_verified"]
            )),
            error=type(exc).__name__ + ": " + str(exc)[:4000],
        )
        if result["api_called"] and after is None:
            try:
                guards._same_context(state, clean["deck"], prepared["context"])
                observed = _capture(state, base, mesh, constants, clean["deck"])
                result["observed_caps"] = {
                    row["volume_id"]: row["caps"] for row in observed["blocks"]["volumes"]
                }
                result["observed_scope_sha256"] = features._hash(observed["scope"])
            except Exception as observation_error:
                result["observation_error"] = str(observation_error)[:2000]
    return result
