"""Controlled, single-point NASTRAN connection realization."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import _model_save, _save_path, connections as connection_operations
from . import entity_catalog as catalog
from . import model_files


_MAX_MODEL_ENTITIES = 10000
_MAX_CARD_FIELDS = 512
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_SNAPSHOT_BUDGET = MAX_FRAME_BYTES // 4
_RESULT_BUDGET = MAX_FRAME_BYTES * 3 // 4
_REL_TOL = 1e-9
_DERIVED_FIELDS = frozenset({"numelem"})
_PART_REALIZE_FIELDS = frozenset({
    "area", "mass", "cogx", "cogy", "cogz", "ixx", "ixy", "ixz", "iyy", "iyz", "izz",
    "featuresstatus", "lastedit",
})
_GRID_CONTROL_FIELDS = ("CP", "CD", "PS", "SEID")
_POINT_TYPE = "SpotweldPoint_Type"
_POINT_EXPECTED_FIELDS = frozenset({
    "id", "numofparts", "p1", "p2", "x", "y", "z", "d", "status", "fereptype",
    "frozenid", "frozendelete",
})
_POINT_MUTABLE_FIELDS = frozenset({
    "status", "errorclass", "fereptype", "cna", "cnb", "cma", "cmb",
    "searchdist", "usenearestnode", "fourquadsaroundprojectionpoint",
    "cutoffadhesives",
})
_POINT_RBAR_CARD_FIELDS = frozenset({
    "CNA", "CNB", "CMA", "CMB", "Search Dist", "Use Nearest Node",
    "Four Quads around Projection point", "Cut off Adhesives",
})
_TOPOLOGY_FIELDS = frozenset({
    "type", "eid", "g1", "g2", "g3", "g4", "t1", "t2", "t3", "t4",
})
_PERIMETER_CARD_FIELDS = frozenset({
    "id", "sides", "angle", "propbound", "partsbound", "frozen", "name", "comment",
})
_FIXED_SETTINGS = {
    "SpotweldPoint_Type": "RBAR",
    "SpotweldPoint_RBAR_CNA": "123456",
    "SpotweldPoint_RBAR_CNB": "0",
    "SpotweldPoint_RBAR_CMA": "0",
    "SpotweldPoint_RBAR_CMB": "0",
    "SpotweldPoint_RBAR_SearchDist": "",
    "SpotweldPoint_RBAR_UseNearestNode": "n",
    "SpotweldPoint_RBAR_FourQuadsAroundProjectionPoint": "n",
    "SpotweldPoint_RBAR_CutOffAdhesives": "0",
}


def _error(exc):
    try:
        return f"{type(exc).__name__}: {exc}"[:2048]
    except Exception:
        return type(exc).__name__


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _api_return_summary(value):
    value_type = type(value).__name__
    summary = {"type": value_type if type(value_type) is str else "unknown"}
    if value is None:
        summary["value"] = None
    elif type(value) is str:
        digest = hashlib.sha256()
        for offset in range(0, len(value), 4096):
            digest.update(value[offset:offset + 4096].encode("utf-8", "replace"))
        summary.update(
            length=len(value),
            preview=value[:128].encode("utf-8", "replace").decode("utf-8"),
            sha256=digest.hexdigest(),
        )
    elif type(value) is float:
        summary.update(finite=math.isfinite(value), preview=repr(value)[:64])
    elif type(value) is bool:
        summary["preview"] = "true" if value else "false"
    elif type(value) is int:
        if value.bit_length() <= 256:
            summary["preview"] = str(value)
        else:
            summary["bit_length"] = value.bit_length()
    elif type(value) in (list, tuple, dict):
        summary["length"] = len(value)
    elif type(value) is bytes:
        summary.update(
            length=len(value), preview=value[:32].hex(),
            sha256=hashlib.sha256(value).hexdigest(),
        )
    return summary


def _field_key(name):
    return "".join(char.casefold() for char in name if char.isalnum())


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_value(value, label, depth=0):
    if depth > 32:
        raise ValueError(f"{label} exceeds the JSON nesting limit")
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if type(value) in (list, tuple):
        return [
            _json_value(item, label, depth + 1)
            for item in value
        ]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{label} contains a non-string key")
        return {
            key: _json_value(item, label, depth + 1)
            for key, item in value.items()
        }
    raise ValueError(f"{label} contains a non-JSON value")


def _expected_fields(value, label):
    if type(value) is not dict or not 1 <= len(value) <= _MAX_CARD_FIELDS:
        raise ValueError(f"{label} must contain 1 to {_MAX_CARD_FIELDS} card fields")
    normalized = set()
    for name, field_value in value.items():
        if type(name) is not str or not name:
            raise ValueError(f"{label} keys must be non-empty field names")
        key = _field_key(name)
        if not key or key in normalized:
            raise ValueError(f"{label} contains duplicate or invalid field names")
        normalized.add(key)
        _json_value(field_value, f"{label}.{name}")
    return value


def _validate_inputs(params):
    required = ("deck", "point_id", "expected_fields", "target_parts", "backup_path")
    catalog._object(params, required, label="params")
    deck = _positive_int(params["deck"], "deck")
    point_id = _positive_int(params["point_id"], "point_id")
    expected = _expected_fields(params["expected_fields"], "expected_fields")
    missing = sorted(_POINT_EXPECTED_FIELDS - {_field_key(key) for key in expected})
    if missing:
        raise ValueError(
            "expected_fields must include: "
            + ", ".join(sorted({
                "id": "ID", "numofparts": "Num of Parts", "p1": "P1", "p2": "P2",
                "x": "X", "y": "Y",
                "z": "Z", "d": "D", "status": "Status", "fereptype": "FE Rep Type",
                "frozenid": "FROZEN_ID", "frozendelete": "FROZEN_DELETE",
            }[item] for item in missing))
        )
    targets = params["target_parts"]
    if type(targets) is not list or len(targets) != 2:
        raise ValueError("target_parts must contain exactly two explicit parts")
    target_ids = {key: set() for key in ("part_id", "shell_id", "property_id")}
    clean_targets = []
    for index, target in enumerate(targets):
        fields = (
            "part_id", "part_expected_fields",
            "shell_id", "shell_expected_fields",
            "property_id", "property_expected_fields",
            "material_id", "material_expected_fields",
        )
        catalog._object(target, fields, label=f"target_parts[{index}]")
        clean = {}
        for key in fields:
            if not key.endswith("_id"):
                continue
            clean[key] = _positive_int(target[key], f"target_parts[{index}].{key}")
            if key in target_ids:
                target_ids[key].add(clean[key])
        for key in fields:
            if key.endswith("_expected_fields"):
                clean[key] = _expected_fields(
                    target[key], f"target_parts[{index}].{key}"
                )
        clean_targets.append(clean)
    duplicate_kinds = [key for key, values in target_ids.items() if len(values) != 2]
    if duplicate_kinds:
        raise ValueError(
            "target_parts must use distinct IDs for: " + ", ".join(duplicate_kinds)
        )
    backup_path = params["backup_path"]
    if type(backup_path) is not str or not backup_path:
        raise ValueError("backup_path must be an absolute .ansa file path")
    return {
        "deck": deck,
        "point_id": point_id,
        "expected_fields": expected,
        "target_parts": clean_targets,
        "backup_path": backup_path,
    }


def _collect_pairs(base, deck, search_type, *, container=None, expected_type=None):
    collect = getattr(base, "CollectEntities", None)
    if not callable(collect):
        raise NotImplementedError("ANSA base.CollectEntities is unavailable")
    if container is None:
        entities = collect(
            deck, None, search_type, False,
            filter_visible=False, hidden_entities=True,
        )
    else:
        entities = collect(deck, container, search_type)
    if type(entities) not in (list, tuple):
        raise RuntimeError(
            f"CollectEntities({search_type}) did not return a complete entity list"
        )
    if len(entities) > _MAX_MODEL_ENTITIES:
        raise ValueError(
            f"CollectEntities({search_type}) exceeds the {_MAX_MODEL_ENTITIES}-entity limit"
        )
    pairs = []
    seen = set()
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity, expected_type)
        key = (reference["type"], reference["id"])
        if key in seen:
            raise RuntimeError(f"CollectEntities({search_type}) contains duplicate IDs")
        seen.add(key)
        pairs.append((entity, reference))
    pairs.sort(key=lambda pair: (pair[1]["type"], pair[1]["id"]))
    return pairs


def _read_full_card(base, deck, entity, reference, label):
    labels = catalog._entity_card_labels(entity, deck)
    if not labels:
        raise RuntimeError(f"{label} has no readable card fields")
    if len(labels) > _MAX_CARD_FIELDS:
        raise ValueError(f"{label} exceeds the {_MAX_CARD_FIELDS}-field limit")
    selected = tuple(sorted(labels))
    values = catalog._read_card(base, deck, entity, reference, selected)
    return {
        "entity": reference,
        "card_fields": list(selected),
        "fields": values,
    }


def _capture_snapshot(base, deck, native_connections, native_mesh):
    """Read a complete, bounded model scope; retain partial evidence on read errors."""
    snapshot = {
        "complete": False,
        "deck": deck,
        "entity_references": [],
        "fe_references": [],
        "fe_cards": [],
        "fe_perimeters": [],
        "grids": [],
        "property_cards": [],
        "material_cards": [],
        "parts": [],
        "connections": [],
        "errors": [],
    }
    handles = {"point_entities": {}, "part_entities": {}}

    def fail(scope, exc):
        snapshot["errors"].append({"scope": scope, "error": _error(exc)})

    try:
        all_pairs = _collect_pairs(base, deck, "__ALL_ENTITIES__")
        snapshot["entity_references"] = [reference for _, reference in all_pairs]
    except Exception as exc:
        all_pairs = []
        fail("__ALL_ENTITIES__", exc)

    all_keys = {
        (reference["type"], reference["id"])
        for reference in snapshot["entity_references"]
    }

    def read_collection(name, search_type, *, expected_type=None):
        try:
            pairs = _collect_pairs(
                base, deck, search_type, expected_type=expected_type
            )
        except Exception as exc:
            fail(name, exc)
            return []
        rows = []
        for entity, reference in pairs:
            try:
                rows.append(
                    _read_full_card(
                        base, deck, entity, reference,
                        f"{name} {reference['type']} {reference['id']}",
                    )
                )
            except Exception as exc:
                fail(f"{name}:{reference['type']}:{reference['id']}", exc)
        return rows

    snapshot["fe_cards"] = read_collection("FE", "__ELEMENTS__")
    snapshot["fe_references"] = [
        row["entity"] for row in snapshot["fe_cards"]
    ]
    try:
        perimeter_pairs = _collect_pairs(
            base, deck, "FE PERIMETER", expected_type="FE PERIMETER"
        )
    except Exception as exc:
        perimeter_pairs = []
        fail("FE PERIMETER", exc)
    get_perimeter_shells = getattr(native_mesh, "GetFEPerimeterShells", None)
    for perimeter, reference in perimeter_pairs:
        try:
            row = _read_full_card(
                base, deck, perimeter, reference,
                f"FE PERIMETER {reference['id']}",
            )
        except Exception as exc:
            fail(f"FE PERIMETER:{reference['id']}", exc)
            continue
        try:
            if not callable(get_perimeter_shells):
                raise NotImplementedError(
                    "ANSA mesh.GetFEPerimeterShells is unavailable"
                )
            edge_entities = get_perimeter_shells([perimeter], False)
            if type(edge_entities) is not list:
                raise RuntimeError(
                    "GetFEPerimeterShells([perimeter], False) did not return a list"
                )
            if len(edge_entities) > _MAX_MODEL_ENTITIES:
                raise ValueError(
                    "GetFEPerimeterShells([perimeter], False) exceeds the "
                    f"{_MAX_MODEL_ENTITIES}-entity limit"
                )
            edge_shells = []
            seen_shells = set()
            for shell in edge_entities:
                shell_reference = catalog._entity_reference(
                    base, deck, shell, "SHELL"
                )
                shell_key = (shell_reference["type"], shell_reference["id"])
                if shell_key in seen_shells:
                    raise RuntimeError(
                        f"FE PERIMETER {reference['id']} returned duplicate SHELL IDs"
                    )
                seen_shells.add(shell_key)
                edge_shells.append(shell_reference)
            edge_shells.sort(key=lambda item: (item["type"], item["id"]))
            row["edge_shells"] = edge_shells
        except Exception as exc:
            row["edge_shells"] = []
            fail(f"FE PERIMETER:{reference['id']}:edge_shells", exc)
        snapshot["fe_perimeters"].append(row)
    snapshot["grids"] = read_collection("GRID", "GRID", expected_type="GRID")
    snapshot["property_cards"] = read_collection("properties", "__PROPERTIES__")
    snapshot["material_cards"] = read_collection("materials", "__MATERIALS__")

    try:
        part_pairs = _collect_pairs(
            base, deck, "ANSAPART", expected_type="ANSAPART"
        )
    except Exception as exc:
        part_pairs = []
        fail("parts", exc)
    for entity, reference in part_pairs:
        handles["part_entities"][reference["id"]] = entity
        try:
            row = _read_full_card(
                base, deck, entity, reference, f"ANSAPART {reference['id']}"
            )
        except Exception as exc:
            fail(f"ANSAPART:{reference['id']}", exc)
            continue
        try:
            members = _collect_pairs(
                base, deck, "__ELEMENTS__", container=entity
            )
            row["members"] = [member_ref for _, member_ref in members]
        except Exception as exc:
            row["members"] = []
            fail(f"ANSAPART:{reference['id']}:members", exc)
        snapshot["parts"].append(row)

    point_pairs = []
    for entity_type in connection_operations._POINT_TYPES:
        try:
            point_pairs.extend(
                _collect_pairs(
                    base, deck, entity_type, expected_type=entity_type
                )
            )
        except Exception as exc:
            fail(f"connections:{entity_type}", exc)
    point_pairs.sort(key=lambda pair: (pair[1]["type"], pair[1]["id"]))
    seen_points = set()
    get_settings = getattr(native_connections, "GetAllConnectionSettings", None)
    if not callable(get_settings):
        fail("GetAllConnectionSettings", NotImplementedError(
            "ANSA connections.GetAllConnectionSettings is unavailable"
        ))
    for entity, reference in point_pairs:
        key = (reference["type"], reference["id"])
        if key in seen_points:
            fail("connections", RuntimeError("connection point appears in multiple collections"))
            continue
        seen_points.add(key)
        handles["point_entities"][key] = entity
        try:
            row = _read_full_card(
                base, deck, entity, reference,
                f"{reference['type']} {reference['id']}",
            )
        except Exception as exc:
            fail(f"connection:{reference['type']}:{reference['id']}", exc)
            continue
        if callable(get_settings):
            try:
                settings = get_settings(entity)
                if settings is not None and type(settings) is not dict:
                    raise RuntimeError(
                        "GetAllConnectionSettings returned neither dict nor None"
                    )
                row["settings"] = _json_value(
                    settings, f"settings for {reference['type']} {reference['id']}"
                )
            except Exception as exc:
                row["settings"] = None
                fail(f"connection-settings:{reference['type']}:{reference['id']}", exc)
        else:
            row["settings"] = None
        if reference["type"] == _POINT_TYPE:
            row["part_references"] = []
            try:
                resolve = getattr(native_connections, "ConnectivityStringToEnts", None)
                if not callable(resolve):
                    raise NotImplementedError("ANSA connections.ConnectivityStringToEnts is unavailable")
                for field in ("P1", "P2"):
                    value = _field(row, field)
                    entities = resolve(value) if type(value) is str and value.strip() else []
                    if type(entities) not in (list, tuple) or len(entities) > _MAX_MODEL_ENTITIES:
                        raise RuntimeError("connection Part references are incomplete or over limit")
                    row["part_references"].append([
                        catalog._entity_reference(base, deck, part, "ANSAPART")
                        for part in entities
                    ])
            except Exception as exc:
                fail(f"connection-parts:{reference['type']}:{reference['id']}", exc)
        snapshot["connections"].append(row)

    typed_refs = (
        snapshot["fe_references"]
        + [row["entity"] for row in snapshot["fe_perimeters"]]
        + [
            shell_reference
            for row in snapshot["fe_perimeters"]
            for shell_reference in row.get("edge_shells", [])
        ]
        + [row["entity"] for row in snapshot["grids"]]
        + [row["entity"] for row in snapshot["property_cards"]]
        + [row["entity"] for row in snapshot["material_cards"]]
        + [row["entity"] for row in snapshot["parts"]]
        + [row["entity"] for row in snapshot["connections"]]
    )
    for reference in typed_refs:
        if (reference["type"], reference["id"]) not in all_keys:
            fail(
                "__ALL_ENTITIES__",
                RuntimeError(
                    f"typed entity {reference['type']} {reference['id']} is missing "
                    "from the complete entity collection"
                ),
            )

    fe_keys = {
        (reference["type"], reference["id"])
        for reference in snapshot["fe_references"]
    }
    for perimeter_row in snapshot["fe_perimeters"]:
        for shell_reference in perimeter_row.get("edge_shells", []):
            shell_key = (shell_reference["type"], shell_reference["id"])
            if shell_key not in fe_keys:
                fail(
                    f"FE PERIMETER:{perimeter_row['entity']['id']}:edge_shells",
                    RuntimeError(
                        f"associated SHELL {shell_reference['id']} is outside "
                        "the complete FE collection"
                    ),
                )

    snapshot["errors"] = snapshot["errors"][:32]
    snapshot["complete"] = not snapshot["errors"]
    return snapshot, handles


def _snapshot_digest(snapshot):
    encoded = _encoded(snapshot)
    return {
        "complete": snapshot.get("complete") is True,
        "details_omitted": True,
        "serialized_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _evidence(snapshot):
    if snapshot is None:
        return None
    if len(_encoded(snapshot)) <= _SNAPSHOT_BUDGET:
        return snapshot
    return _snapshot_digest(snapshot)


def _result_fits(params, result):
    try:
        request_size = len(_encoded(params))
        result_size = len(_encoded(result))
    except (TypeError, ValueError):
        return False
    return (
        result_size <= _RESULT_BUDGET
        and request_size + result_size < MAX_FRAME_BYTES
    )


def _bounded_result(params, result):
    result = dict(result)
    result["errors"] = [str(item)[:2048] for item in result.get("errors", [])[:16]]
    result["error"] = "; ".join(result["errors"])[:4096]
    if _result_fits(params, result):
        return result

    possible_effects = (
        result.get("possible_effects") is True
        or result.get("api_invoked") is True
    )
    for key in ("before", "after"):
        evidence = result.get(key)
        if type(evidence) is dict and "sha256" not in evidence:
            result[key] = _snapshot_digest(evidence)

    minimal = {
        "ok": False,
        "tool": "connection_apply",
        "deck": result.get("deck"),
        "point_id": result.get("point_id"),
        "before": result.get("before"),
        "after": result.get("after"),
        "api_invoked": result.get("api_invoked") is True,
        "api_return_type": result.get("api_return_type"),
        "api_return_value": None,
        "api_return_summary": result.get("api_return_summary"),
        "scope_verified": False,
        "model_scope_matches": False,
        "possible_effects": possible_effects,
        "evidence_truncated": True,
        "error": "connection apply result exceeds the bounded reply budget",
        "errors": ["connection apply result exceeds the bounded reply budget"],
    }
    if _result_fits(params, minimal):
        return minimal

    minimal = {
        "ok": False,
        "after": result.get("after"),
        "api_return_summary": result.get("api_return_summary"),
        "scope_verified": False,
        "possible_effects": possible_effects,
    }
    if _result_fits(params, minimal):
        return minimal

    minimal = {"ok": False}
    if not _result_fits(params, minimal):
        raise ValueError("connection apply cannot fit its minimal reply in the frame budget")
    return minimal


def _execution_context(state, deck):
    state.check_conflict()
    probe = state.probe()
    model, grant = probe.get("model"), probe.get("authorization")
    if (
        type(model) is not dict
        or type(model.get("epoch")) is not int
        or model["epoch"] <= 0
        or model.get("write_enabled") is not True
        or probe.get("deck") != deck
        or probe.get("connected") is not True
        or probe.get("stopped") is not False
        or probe.get("persistence_blocked") is not False
        or type(grant) is not dict
        or grant.get("active") is not True
        or not grant.get("backend_id")
        or type(grant.get("generation")) is not int
        or grant["generation"] <= 0
        or getattr(state, "connected_backend_id", None) != grant["backend_id"]
    ):
        raise RuntimeError("connection apply model context or write authorization is unavailable")
    identity = {
        key: probe.get(key)
        for key in ("pid", "process_start", "instance_id", "session_id")
    }
    if any(value is None for value in identity.values()):
        raise RuntimeError("connection apply process context is incomplete")
    return {
        **identity,
        "deck": deck,
        "epoch": model["epoch"],
        "backend_id": grant["backend_id"],
        "generation": grant["generation"],
    }


def _same_context(state, deck, expected):
    if _execution_context(state, deck) != expected:
        raise RuntimeError("model context or write authorization changed during connection apply")


def _database_name(base):
    getter = getattr(base, "DataBaseName", None)
    if not callable(getter):
        raise NotImplementedError("ANSA base.DataBaseName is unavailable")
    value = getter()
    if type(value) is not str or not value:
        raise ValueError("the current model must have a saved database path")
    return value


def _backup_record(path):
    fingerprint = model_files._fingerprint(path)
    return {
        "path": str(path),
        "saved": True,
        "verified": True,
        "exists": True,
        **fingerprint,
    }


def _unconfirmed_backup(path):
    record = {
        "path": str(path),
        "saved": False,
        "verified": False,
        "exists": None,
        "observation_errors": [],
    }
    try:
        path.lstat()
    except FileNotFoundError:
        record["exists"] = False
    except Exception as exc:
        record["observation_errors"].append(_error(exc))
    else:
        record["exists"] = True
        try:
            record.update(model_files._public_file_record(model_files._fingerprint(path)))
        except Exception as exc:
            record["observation_errors"].append(_error(exc))
    return record


def _verify_backup(base, constants, backup_path, current_path, current_fingerprint):
    saved = _model_save(
        base, constants, {"path": str(backup_path), "overwrite": False}
    )
    if type(saved) is not dict or saved.get("saved") is not True:
        raise RuntimeError("model save helper did not confirm the requested backup")
    saved_path = saved.get("path")
    if type(saved_path) is not str or not model_files._same_path(saved_path, backup_path):
        raise RuntimeError("model save helper returned a different backup path")
    backup = _backup_record(backup_path)
    if not model_files._same_path(current_path, _database_name(base)):
        raise RuntimeError("SaveAs changed the current database path")
    current_after = model_files._fingerprint(Path(current_path))
    if not catalog._strict_equal(current_fingerprint, current_after):
        raise RuntimeError("SaveAs changed the current database file")
    try:
        if os.path.samefile(backup_path, Path(current_path)):
            raise RuntimeError("backup resolves to the current database file")
    except OSError as exc:
        raise RuntimeError(f"could not verify backup identity: {exc}") from exc
    return backup


def _rows_by_reference(rows, label):
    result = {}
    for row in rows:
        reference = row["entity"]
        key = (reference["type"], reference["id"])
        if key in result:
            raise RuntimeError(f"{label} contains duplicate entity references")
        result[key] = row
    return result


def _field(row, name):
    fields = row.get("fields")
    if type(fields) is not dict:
        raise RuntimeError("card row has no complete field map")
    key = _field_key(name)
    matches = [value for label, value in fields.items() if _field_key(label) == key]
    if len(matches) != 1:
        raise RuntimeError(
            f"{row['entity']['type']} {row['entity']['id']} lacks unique card field {name}"
        )
    return matches[0]


def _grid_control_default(value):
    return (
        value is None
        or (type(value) is int and value == 0)
        or (type(value) is str and value == "")
    )


def _ensure_grid_control_default(row, field_name, label):
    if not _grid_control_default(_field(row, field_name)):
        raise ValueError(f"{label} {field_name} is not a verified default value")


def _ensure_grid_control_defaults(row, label):
    for field_name in _GRID_CONTROL_FIELDS:
        _ensure_grid_control_default(row, field_name, label)


def _ensure_default_shell_thickness(row, thickness_fields, label):
    tflag = _field(row, "TFLAG")
    if type(tflag) is not str or tflag.strip():
        raise ValueError(f"{label} TFLAG must use the blank default")
    for field_name in thickness_fields:
        value = _field(row, field_name)
        if (type(value) not in (int, float) or value != 0
                or not math.isfinite(value)):
            raise ValueError(f"{label} {field_name} must be a finite zero default")


def _complete_expected(row, expected, label, *, excluded=()):
    actual_names = set(row["card_fields"])
    stable_names = {
        name for name in actual_names
        if _field_key(name) not in excluded
    }
    expected_names = set(expected)
    if expected_names != stable_names:
        missing = sorted(stable_names - expected_names)
        extra = sorted(expected_names - stable_names)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing[:12]))
        if extra:
            detail.append("unknown " + ", ".join(extra[:12]))
        raise ValueError(
            f"{label} expected_fields must cover every stable card field"
            + (": " + "; ".join(detail) if detail else "")
        )
    catalog._matches_expected(row["fields"], expected)


def _unfrozen(value):
    return value == "NO" or (type(value) is int and value == 0)


def _ensure_unfrozen(row, label, fields):
    for field_name in fields:
        if _field_key(field_name) in {_field_key(name) for name in row["card_fields"]}:
            if not _unfrozen(_field(row, field_name)):
                raise ValueError(f"{label} is frozen")


def _point_preconditions(snapshot, params):
    points = _rows_by_reference(snapshot["connections"], "connections")
    point_key = (_POINT_TYPE, params["point_id"])
    point = points.get(point_key)
    if point is None:
        raise LookupError(f"SpotweldPoint_Type {params['point_id']} is absent")
    expected = params["expected_fields"]
    _complete_expected(point, expected, "selected SpotweldPoint_Type")
    for required in _POINT_EXPECTED_FIELDS:
        _field(point, required)
    if _field(point, "ID") != params["point_id"]:
        raise RuntimeError("selected point card ID does not match point_id")
    _ensure_unfrozen(point, "selected point", ("FROZEN_ID", "FROZEN_DELETE"))
    fe_type = _field_key(str(_field(point, "FE Rep Type")).strip())
    if fe_type not in ("", "none", "nonerep"):
        raise ValueError("selected point already has an FE representation")

    p1 = _field(point, "P1")
    p2 = _field(point, "P2")
    if type(p1) is not str or not p1.strip() or type(p2) is not str or not p2.strip():
        raise ValueError("selected point P1 and P2 must name two explicit ANSAPARTs")
    if p1 == p2:
        raise ValueError("selected point P1 and P2 must identify different ANSAPARTs")
    if _field(point, "Num of Parts") != 2:
        raise ValueError("selected point must connect exactly two parts")
    point_field_names = {_field_key(name) for name in point["card_fields"]}
    for index in range(3, 11):
        if f"p{index}" in point_field_names and _field(point, f"P{index}") not in (None, ""):
            raise ValueError("selected point has an unsupported connection beyond P1/P2")
    point_position = [
        _number(_field(point, axis), f"point {axis}")
        for axis in ("X", "Y", "Z")
    ]
    diameter = _number(_field(point, "D"), "point D")
    if diameter <= 0:
        raise ValueError("selected point D must be positive")

    parts = _rows_by_reference(snapshot["parts"], "ANSAPART")
    shells = _rows_by_reference(snapshot["fe_cards"], "FE cards")
    grids = _rows_by_reference(snapshot["grids"], "GRID cards")
    properties = _rows_by_reference(snapshot["property_cards"], "property cards")
    materials = _rows_by_reference(snapshot["material_cards"], "material cards")
    perimeter_shell_keys = {
        (shell_reference["type"], shell_reference["id"])
        for perimeter in snapshot["fe_perimeters"]
        for shell_reference in perimeter["edge_shells"]
    }

    target_by_id = {}
    geometries = []
    for target in params["target_parts"]:
        part_id = target["part_id"]
        part_key = ("ANSAPART", part_id)
        part = parts.get(part_key)
        if part is None:
            raise LookupError(f"ANSAPART {part_id} is absent")
        _complete_expected(
            part, target["part_expected_fields"], f"ANSAPART {part_id}",
            excluded=_DERIVED_FIELDS,
        )
        _ensure_unfrozen(part, f"ANSAPART {part_id}", ("FROZEN_ID", "FROZEN_DELETE"))
        part_name = _field(part, "Name")
        if type(part_name) is not str or not part_name:
            raise ValueError(f"ANSAPART {part_id} has no stable Name")

        shell_id = target["shell_id"]
        shell = shells.get(("SHELL", shell_id))
        if shell is None:
            raise LookupError(f"SHELL {shell_id} is absent from the complete FE collection")
        if ("SHELL", shell_id) in perimeter_shell_keys:
            raise ValueError(
                f"source SHELL {shell_id} already belongs to an FE PERIMETER"
            )
        _complete_expected(
            shell, target["shell_expected_fields"], f"SHELL {shell_id}",
            excluded=_DERIVED_FIELDS,
        )
        if str(_field(shell, "type")).strip().upper() != "CQUAD4":
            raise ValueError(f"SHELL {shell_id} must be one CQUAD4")
        _ensure_default_shell_thickness(
            shell, ("T1", "T2", "T3", "T4"), f"CQUAD4 {shell_id}"
        )
        _ensure_unfrozen(
            shell, f"CQUAD4 {shell_id}",
            ("FROZEN_ID", "FROZEN_DELETE", "FROZEN"),
        )
        if _field(shell, "PID") != target["property_id"]:
            raise ValueError(f"CQUAD4 {shell_id} does not use its conditioned PSHELL")
        members = part.get("members")
        if type(members) is not list or len(members) != 1 or members[0] != shell["entity"]:
            raise ValueError(
                f"ANSAPART {part_id} must contain exactly its conditioned CQUAD4"
            )

        property_id = target["property_id"]
        prop = properties.get(("PSHELL", property_id))
        if prop is None:
            raise LookupError(f"PSHELL {property_id} is absent from the complete properties")
        _complete_expected(
            prop, target["property_expected_fields"], f"PSHELL {property_id}",
            excluded=_DERIVED_FIELDS,
        )
        if _field(prop, "PID") != property_id:
            raise RuntimeError(f"PSHELL {property_id} card PID is inconsistent")
        thickness = _number(_field(prop, "T"), f"PSHELL {property_id} T")
        if thickness <= 0:
            raise ValueError(f"PSHELL {property_id} T must be positive")
        if _field(prop, "Num.Elem") != 1:
            raise ValueError(f"PSHELL {property_id} must initially contain one element")

        material_id = target["material_id"]
        material = materials.get(("MAT1", material_id))
        if material is None:
            raise LookupError(f"MAT1 {material_id} is absent from the complete materials")
        _complete_expected(
            material, target["material_expected_fields"], f"MAT1 {material_id}",
            excluded=_DERIVED_FIELDS,
        )
        if _field(prop, "MID1") != material_id:
            raise ValueError(f"PSHELL {property_id} MID1 does not match MAT1 {material_id}")
        if str(_field(material, "DEFINED")).strip().casefold() not in ("yes", "true", "1"):
            raise ValueError(f"MAT1 {material_id} is not defined")

        geometry = _quad_geometry(shell, grids)
        density = _number(_field(material, "RHO"), f"MAT1 {material_id} RHO")
        if density <= 0:
            raise ValueError(f"MAT1 {material_id} RHO must be positive")
        nsm = _field(prop, "NSM")
        if nsm is not None and (type(nsm) not in (int, float) or not math.isfinite(nsm) or nsm != 0):
            raise ValueError(f"PSHELL {property_id} NSM must use its zero default")
        expected_mass = density * thickness * geometry["width"] * geometry["height"]
        _verify_part_measures(part, geometry, expected_mass, triangulated=False)
        if _field(part, "Features Status") != "Not Recognized":
            raise ValueError(f"ANSAPART {part_id} feature recognition is outside the verified source state")
        _verify_edit_stamp(part)
        target_by_id[part_id] = {
            **target,
            "part": part,
            "part_name": part_name,
            "shell": shell,
            "property": prop,
            "material": material,
            "geometry": geometry,
            "expected_mass": expected_mass,
        }
        geometries.append(geometry)

    resolved_parts = point.get("part_references")
    if (type(resolved_parts) is not list or len(resolved_parts) != 2
            or any(type(group) is not list or len(group) != 1 for group in resolved_parts)):
        raise ValueError("point P1 and P2 must each resolve to one ANSAPART")
    p1_part_id, p2_part_id = (group[0]["id"] for group in resolved_parts)
    if {p1_part_id, p2_part_id} != set(target_by_id):
        raise ValueError("target_parts must be exactly the ANSAPARTs resolved from P1 and P2")

    first, second = geometries
    for axis in ("xmin", "xmax", "ymin", "ymax"):
        if not _close(first[axis], second[axis], max(first["width"], first["height"], 1.0)):
            raise ValueError("the two target CQUAD4s must share the same XY rectangle")
    if _close(first["z"], second["z"], max(first["width"], first["height"], 1.0)):
        raise ValueError("the two target CQUAD4s must lie on distinct parallel Z planes")
    center_x = (first["xmin"] + first["xmax"]) / 2.0
    center_y = (first["ymin"] + first["ymax"]) / 2.0
    middle_z = (first["z"] + second["z"]) / 2.0
    scale = max(first["width"], first["height"], abs(second["z"] - first["z"]), 1.0)
    if not all((
        _close(point_position[0], center_x, scale),
        _close(point_position[1], center_y, scale),
        _close(point_position[2], middle_z, scale),
    )):
        raise ValueError("selected point must lie at the shared rectangle center and Z midplane")

    for grid in snapshot["grids"]:
        if not _grid_control_default(_field(grid, "CP")):
            continue
        xyz = _grid_position(grid)
        if any(
            _close(xyz[0], center_x, scale)
            and _close(xyz[1], center_y, scale)
            and _close(xyz[2], geometry["z"], scale)
            for geometry in geometries
        ):
            raise ValueError("a projected center GRID already exists on a target plane")

    return {
        "point": point,
        "point_position": point_position,
        "point_diameter": diameter,
        "p1": p1,
        "p2": p2,
        "p1_part_id": p1_part_id,
        "p2_part_id": p2_part_id,
        "target_by_id": target_by_id,
    }


def _verify_edit_stamp(part):
    value = _field(part, "Last Edit")
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError("ANSAPART Last Edit is not a readable bounded timestamp")


def _verify_part_measures(part, geometry, mass, *, triangulated):
    """Check native lumped-node summaries against the conditioned rectangle mesh."""
    width, height = geometry["width"], geometry["height"]
    divisor = 6.0 if triangulated else 4.0
    ixx, iyy = mass * height ** 2 / divisor, mass * width ** 2 / divisor
    expected = {"Area": width * height, "Mass": mass,
        "Ixx": ixx, "Iyy": iyy, "Izz": ixx + iyy, "Ixy": 0.0, "Ixz": 0.0, "Iyz": 0.0}
    for name, value in expected.items():
        actual = _number(_field(part, name), f"ANSAPART {name}")
        if not math.isfinite(value) or not math.isclose(actual, value, rel_tol=_REL_TOL, abs_tol=1e-20):
            raise RuntimeError(f"ANSAPART {name} does not match the verified lumped mesh measure")
    center = ((geometry["xmin"] + geometry["xmax"]) / 2.0,
              (geometry["ymin"] + geometry["ymax"]) / 2.0, geometry["z"])
    for name, value in zip(("COG x", "COG y", "COG z"), center):
        actual = _number(_field(part, name), f"ANSAPART {name}")
        if not _close(actual, value, max(width, height)):
            raise RuntimeError(f"ANSAPART {name} does not match the target rectangle center")


def _close(left, right, scale=1.0):
    return math.isclose(
        float(left), float(right),
        rel_tol=_REL_TOL,
        abs_tol=_REL_TOL * max(1.0, abs(float(scale))),
    )


def _grid_position(row):
    return [
        _number(_field(row, axis), f"GRID {_field(row, 'NID')} {axis}")
        for axis in ("X1", "X2", "X3")
    ]


def _quad_geometry(shell, grids):
    node_ids = [
        _positive_int(_field(shell, f"G{index}"), f"CQUAD4 G{index}")
        for index in range(1, 5)
    ]
    if len(set(node_ids)) != 4:
        raise ValueError("CQUAD4 must use four distinct GRID IDs")
    points = []
    for node_id in node_ids:
        row = grids.get(("GRID", node_id))
        if row is None:
            raise ValueError(f"CQUAD4 references missing GRID {node_id}")
        if not _grid_control_default(_field(row, "CP")):
            raise ValueError("target CQUAD4 GRID coordinates must use the basic coordinate system")
        points.append(_grid_position(row))
    scale = max(
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
        1.0,
    )
    z = points[0][2]
    if any(not _close(point[2], z, scale) for point in points[1:]):
        raise ValueError("target CQUAD4 must be planar in an XY plane")
    xmin, xmax = min(point[0] for point in points), max(point[0] for point in points)
    ymin, ymax = min(point[1] for point in points), max(point[1] for point in points)
    width, height = xmax - xmin, ymax - ymin
    if width <= _REL_TOL * scale or height <= _REL_TOL * scale:
        raise ValueError("target CQUAD4 rectangle is degenerate")
    corners = {
        (0, 0): (xmin, ymin), (1, 0): (xmax, ymin),
        (1, 1): (xmax, ymax), (0, 1): (xmin, ymax),
    }
    found = set()
    for x, y, _ in points:
        ix = 0 if _close(x, xmin, scale) else 1 if _close(x, xmax, scale) else None
        iy = 0 if _close(y, ymin, scale) else 1 if _close(y, ymax, scale) else None
        if ix is None or iy is None or (ix, iy) in found:
            raise ValueError("target CQUAD4 must have exactly the four corners of an XY rectangle")
        found.add((ix, iy))
    if found != set(corners):
        raise ValueError("target CQUAD4 must have exactly the four corners of an XY rectangle")

    edge_axes = []
    for index in range(4):
        left, right = points[index], points[(index + 1) % 4]
        dx, dy = right[0] - left[0], right[1] - left[1]
        x_edge, y_edge = abs(dx) > _REL_TOL * scale, abs(dy) > _REL_TOL * scale
        if x_edge == y_edge:
            raise ValueError("target CQUAD4 edges must be axis-aligned and non-degenerate")
        edge_axes.append("x" if x_edge else "y")
    if any(edge_axes[index] == edge_axes[(index + 1) % 4] for index in range(4)):
        raise ValueError("target CQUAD4 edges must alternate between X and Y")
    signed_twice_area = sum(
        points[index][0] * points[(index + 1) % 4][1]
        - points[(index + 1) % 4][0] * points[index][1]
        for index in range(4)
    )
    if abs(signed_twice_area) <= 2.0 * _REL_TOL * scale * scale:
        raise ValueError("target CQUAD4 has zero projected area")
    return {
        "node_ids": node_ids,
        "points": points,
        "xmin": xmin,
        "xmax": xmax,
        "ymin": ymin,
        "ymax": ymax,
        "z": z,
        "width": width,
        "height": height,
        "area": abs(signed_twice_area) / 2.0,
        "orientation": 1 if signed_twice_area > 0 else -1,
    }


def _stable_fields(row, excluded):
    return {
        _field_key(name): value
        for name, value in row["fields"].items()
        if _field_key(name) not in excluded
    }


def _row_equal(left, right, *, exclude_fields=()):
    return (
        left["entity"] == right["entity"]
        and left["card_fields"] == right["card_fields"]
        and catalog._strict_equal(
            _stable_fields(left, frozenset(_field_key(name) for name in exclude_fields)),
            _stable_fields(right, frozenset(_field_key(name) for name in exclude_fields)),
        )
    )


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _int_value(value, label):
    if type(value) is int:
        return value
    if type(value) is str and value.isdigit():
        return int(value)
    raise RuntimeError(f"{label} must be an integer card value")


def _false_value(value):
    if type(value) is bool:
        return value is False
    if type(value) is int:
        return value == 0
    if type(value) is str:
        return value.strip().casefold() in ("no", "n", "false", "0", "")
    return False


def _point_rows_equal(before, after, selected_key):
    before_rows = _rows_by_reference(before["connections"], "before connections")
    after_rows = _rows_by_reference(after["connections"], "after connections")
    _require(before_rows.keys() == after_rows.keys(), "connection point identities changed")
    for key, old in before_rows.items():
        new = after_rows[key]
        if key != selected_key:
            _require(catalog._strict_equal(old, new),
                     f"unselected connection point {key[1]} changed")
            continue
        _require(set(new["card_fields"]) == set(old["card_fields"]) | _POINT_RBAR_CARD_FIELDS,
                 "selected point field changes exceed the verified RBAR realization card fields")
        _require(
            catalog._strict_equal(
                _stable_fields(old, _POINT_MUTABLE_FIELDS),
                _stable_fields(new, _POINT_MUTABLE_FIELDS),
            ),
            "selected point identity, P1/P2, geometry, diameter, or stable fields changed",
        )
        _require(catalog._strict_equal(old.get("part_references"), new.get("part_references")),
                 "selected point resolved Part connectivity changed")
        _require(
            catalog._strict_equal(new["settings"], _FIXED_SETTINGS),
            "selected point settings do not exactly match the fixed RBAR settings",
        )
        _require(str(_field(new, "Status")).strip().casefold() == "ok",
                 "selected point Status is not Ok")
        _require(str(_field(new, "Error Class")).strip().casefold() == "rbar",
                 "selected point Error Class is not RBAR")
        _require(str(_field(new, "FE Rep Type")).strip().casefold() == "rbar",
                 "selected point FE Rep Type is not RBAR")
        for name, expected in (("CNA", 123456), ("CNB", 0), ("CMA", 0), ("CMB", 0)):
            _require(_int_value(_field(new, name), f"point {name}") == expected,
                     f"selected point {name} does not match the fixed RBAR setting")
        for name in ("Use Nearest Node", "Four Quads around Projection point",
                     "Cut off Adhesives"):
            _require(_false_value(_field(new, name)),
                     f"selected point {name} does not match the fixed RBAR setting")
        search_distance = _field(new, "Search Dist")
        _require(search_distance in (None, ""),
                 "selected point Search Dist does not match the fixed RBAR setting")


def _triangle_nodes(row):
    values = [
        _positive_int(_field(row, f"G{index}"), f"CTRIA3 G{index}")
        for index in range(1, 4)
    ]
    if len(set(values)) != 3:
        raise RuntimeError("CTRIA3 must use three distinct GRID IDs")
    return tuple(values)


def _verify_new_perimeter_card(row):
    reference = row["entity"]
    _require(
        reference.get("type") == "FE PERIMETER",
        "new perimeter entity is not an FE PERIMETER",
    )
    card_fields = row.get("card_fields")
    _require(
        type(card_fields) is list
        and len(card_fields) == len(_PERIMETER_CARD_FIELDS)
        and all(type(name) is str for name in card_fields)
        and {_field_key(name) for name in card_fields} == _PERIMETER_CARD_FIELDS,
        f"FE PERIMETER {reference['id']} card fields differ from the verified card",
    )
    expected = {
        "Id": reference["id"],
        "Sides": 1,
        "Angle": None,
        "Prop. Bound": "NO",
        "Parts Bound": "NO",
        "Frozen": "NO",
        "Name": "",
        "Comment": "",
    }
    for name, value in expected.items():
        actual = _field(row, name)
        if name in ("Id", "Sides"):
            _require(
                type(actual) is int and actual == value,
                f"FE PERIMETER {reference['id']} {name} differs from its verified default",
            )
        else:
            _require(
                catalog._strict_equal(actual, value),
                f"FE PERIMETER {reference['id']} {name} differs from its verified default",
            )


def _triangle_signed_twice_area(nodes, grid_map):
    points = []
    for node_id in nodes:
        row = grid_map.get(("GRID", node_id))
        if row is None:
            raise RuntimeError(f"CTRIA3 references absent GRID {node_id}")
        points.append(_grid_position(row))
    if any(not _close(point[2], points[0][2], 1.0) for point in points[1:]):
        raise RuntimeError("CTRIA3 does not lie in one target XY plane")
    signed = (
        points[0][0] * (points[1][1] - points[2][1])
        + points[1][0] * (points[2][1] - points[0][1])
        + points[2][0] * (points[0][1] - points[1][1])
    )
    if abs(signed) <= _REL_TOL:
        raise RuntimeError("CTRIA3 has zero area")
    return signed, points


def _verify_fan(target, part_after, after_fe, after_grids, center_grid):
    part_id = target["part_id"]
    old_shell = target["shell"]
    geometry = target["geometry"]
    member_refs = part_after["members"]
    _require(len(member_refs) == 4, f"ANSAPART {part_id} must contain four fan CTRIA3s")
    _require(
        all(reference["type"] == "SHELL" for reference in member_refs),
        f"ANSAPART {part_id} fan contains an unsupported element type",
    )
    member_keys = [(item["type"], item["id"]) for item in member_refs]
    _require(len(set(member_keys)) == 4, f"ANSAPART {part_id} contains duplicate fan elements")
    _require(
        ("SHELL", old_shell["entity"]["id"]) in member_keys,
        f"original CQUAD4 ID {old_shell['entity']['id']} was not preserved",
    )
    rows = []
    for key in member_keys:
        row = after_fe.get(key)
        _require(row is not None, f"ANSAPART {part_id} member is absent from __ELEMENTS__")
        _require(str(_field(row, "type")).strip().upper() == "CTRIA3",
                 f"ANSAPART {part_id} contains a non-CTRIA3 after realization")
        _require(_field(row, "PID") == target["property_id"],
                 f"CTRIA3 in ANSAPART {part_id} changed its source PID")
        _ensure_default_shell_thickness(
            row, ("T1", "T2", "T3"), f"CTRIA3 {row['entity']['id']}"
        )
        _require(
            catalog._strict_equal(
                _field(old_shell, "TFLAG"), _field(row, "TFLAG")
            ),
            f"CTRIA3 {row['entity']['id']} changed the source TFLAG",
        )
        rows.append(row)

    original_nodes = geometry["node_ids"]
    center_id = center_grid["entity"]["id"]
    expected_triangles = {
        frozenset((original_nodes[index], original_nodes[(index + 1) % 4], center_id))
        for index in range(4)
    }
    actual_triangles = [frozenset(_triangle_nodes(row)) for row in rows]
    _require(
        len(set(actual_triangles)) == 4 and set(actual_triangles) == expected_triangles,
        f"ANSAPART {part_id} is not the exact four-edge center fan",
    )

    edge_counts = Counter()
    total_area = 0.0
    for row in rows:
        nodes = _triangle_nodes(row)
        signed, points = _triangle_signed_twice_area(nodes, after_grids)
        _require(
            (1 if signed > 0 else -1) == geometry["orientation"],
            f"CTRIA3 {row['entity']['id']} reverses the source CQUAD4 orientation",
        )
        _require(_close(abs(signed) / 2.0, geometry["area"] / 4.0, geometry["area"]),
                 f"CTRIA3 {row['entity']['id']} has an invalid fan area")
        _require(all(_close(point[2], geometry["z"], geometry["width"]) for point in points),
                 f"CTRIA3 {row['entity']['id']} is outside its target plane")
        total_area += abs(signed) / 2.0
        for index in range(3):
            edge = tuple(sorted((nodes[index], nodes[(index + 1) % 3])))
            edge_counts[edge] += 1

    boundary = {
        tuple(sorted((original_nodes[index], original_nodes[(index + 1) % 4])))
        for index in range(4)
    }
    internal = {
        tuple(sorted((center_id, node_id))) for node_id in original_nodes
    }
    expected_counts = {edge: 1 for edge in boundary}
    expected_counts.update({edge: 2 for edge in internal})
    _require(dict(edge_counts) == expected_counts,
             f"ANSAPART {part_id} fan edge incidence is incomplete")
    _require(_close(total_area, geometry["area"], geometry["area"]),
             f"ANSAPART {part_id} fan area does not equal its source quad")

    topology_excluded = _TOPOLOGY_FIELDS
    for row in rows:
        _require(
            catalog._strict_equal(
                _stable_fields(old_shell, topology_excluded),
                _stable_fields(row, topology_excluded),
            ),
            f"CTRIA3 {row['entity']['id']} changed stable source shell fields",
        )
    return {("GRID", center_id)}


def _verify_transition(before, after, preflight):
    _require(after.get("complete") is True, "post-realization model snapshot is incomplete")

    old_refs = {
        (item["type"], item["id"]) for item in before["entity_references"]
    }
    new_refs = {
        (item["type"], item["id"]) for item in after["entity_references"]
    }
    _require(old_refs <= new_refs, "an existing model entity disappeared")
    added = new_refs - old_refs
    _require(
        Counter(kind for kind, _ in added) == Counter({
            "GRID": 2, "SHELL": 6, "RBAR": 1, "FE PERIMETER": 8,
        }),
        "model identity changes exceed two GRID, six CTRIA3 SHELLs, one RBAR, "
        "and eight FE PERIMETERs",
    )

    old_perimeters = _rows_by_reference(
        before["fe_perimeters"], "before FE PERIMETER cards"
    )
    new_perimeters = _rows_by_reference(
        after["fe_perimeters"], "after FE PERIMETER cards"
    )
    _require(
        old_perimeters.keys() <= new_perimeters.keys(),
        "an existing FE PERIMETER disappeared",
    )
    for key, old in old_perimeters.items():
        _require(
            catalog._strict_equal(old, new_perimeters[key]),
            f"existing FE PERIMETER {key[1]} card or edge-shell relation changed",
        )
    added_perimeter_rows = [
        new_perimeters[key] for key in new_perimeters.keys() - old_perimeters.keys()
    ]
    _require(
        len(added_perimeter_rows) == 8,
        "realization must create exactly eight FE PERIMETER entities",
    )

    old_grids = _rows_by_reference(before["grids"], "before GRID cards")
    new_grids = _rows_by_reference(after["grids"], "after GRID cards")
    _require(old_grids.keys() <= new_grids.keys(), "an existing GRID disappeared")
    for key, old in old_grids.items():
        _require(catalog._strict_equal(old, new_grids[key]),
                 f"existing GRID {key[1]} card, including PS/SEID, changed")
    added_grid_keys = new_grids.keys() - old_grids.keys()
    _require(len(added_grid_keys) == 2, "realization must create exactly two GRID entities")
    for key in added_grid_keys:
        _ensure_grid_control_defaults(new_grids[key], f"projected GRID {key[1]}")

    old_fe = _rows_by_reference(before["fe_cards"], "before FE cards")
    new_fe = _rows_by_reference(after["fe_cards"], "after FE cards")
    _require(old_fe.keys() <= new_fe.keys(), "an existing FE entity disappeared")
    added_fe = new_fe.keys() - old_fe.keys()
    _require(
        Counter(kind for kind, _ in added_fe) == Counter({"SHELL": 6, "RBAR": 1}),
        "FE scope must contain six new CTRIA3s and exactly one new RBAR",
    )

    target_shell_keys = {
        ("SHELL", item["shell_id"])
        for item in preflight["target_by_id"].values()
    }
    for key, old in old_fe.items():
        if key not in target_shell_keys:
            _require(catalog._strict_equal(old, new_fe[key]),
                     f"unselected FE entity {key[0]} {key[1]} changed")
            continue
        new = new_fe[key]
        _require(str(_field(old, "type")).strip().upper() == "CQUAD4",
                 f"source shell {key[1]} was not a CQUAD4 before realization")
        _require(str(_field(new, "type")).strip().upper() == "CTRIA3",
                 f"source shell ID {key[1]} was not retained as a CTRIA3")
        _require(
            catalog._strict_equal(
                _stable_fields(old, _TOPOLOGY_FIELDS),
                _stable_fields(new, _TOPOLOGY_FIELDS),
            ),
            f"source shell {key[1]} changed non-topology card fields",
        )

    old_parts = _rows_by_reference(before["parts"], "before ANSAPART cards")
    new_parts = _rows_by_reference(after["parts"], "after ANSAPART cards")
    _require(old_parts.keys() == new_parts.keys(), "ANSAPART identities changed")
    target_part_ids = set(preflight["target_by_id"])
    part_after_by_id = {}
    for key, old in old_parts.items():
        new = new_parts[key]
        excluded = _DERIVED_FIELDS | _PART_REALIZE_FIELDS if key[1] in target_part_ids else frozenset()
        _require(
            catalog._strict_equal(
                _stable_fields(old, excluded),
                _stable_fields(new, excluded),
            ),
            f"ANSAPART {key[1]} stable card fields changed",
        )
        if key[1] not in target_part_ids:
            _require(old["members"] == new["members"],
                     f"unselected ANSAPART {key[1]} membership changed")
        else:
            target = preflight["target_by_id"][key[1]]
            _verify_part_measures(new, target["geometry"], target["expected_mass"], triangulated=True)
            _require(_field(new, "Features Status") == "Recognized (FE Perimeters)",
                     f"ANSAPART {key[1]} FE perimeter recognition was not confirmed")
            _verify_edit_stamp(new)
        part_after_by_id[key[1]] = new

    old_properties = _rows_by_reference(before["property_cards"], "before properties")
    new_properties = _rows_by_reference(after["property_cards"], "after properties")
    _require(old_properties.keys() == new_properties.keys(), "property identities changed")
    source_property_ids = {
        target["property_id"] for target in preflight["target_by_id"].values()
    }
    for key, old in old_properties.items():
        new = new_properties[key]
        if key[0] == "PSHELL" and key[1] in source_property_ids:
            _require(
                catalog._strict_equal(
                    _stable_fields(old, _DERIVED_FIELDS),
                    _stable_fields(new, _DERIVED_FIELDS),
                ),
                f"source PSHELL {key[1]} changed non-derived fields",
            )
            _require(_field(old, "Num.Elem") == 1 and _field(new, "Num.Elem") == 4,
                     f"source PSHELL {key[1]} Num.Elem must change from 1 to 4")
        else:
            _require(catalog._strict_equal(old, new),
                     f"unselected property {key[0]} {key[1]} changed")

    old_materials = _rows_by_reference(before["material_cards"], "before materials")
    new_materials = _rows_by_reference(after["material_cards"], "after materials")
    _require(old_materials.keys() == new_materials.keys(), "material identities changed")
    for key, old in old_materials.items():
        _require(catalog._strict_equal(old, new_materials[key]),
                 f"material {key[0]} {key[1]} changed")

    selected_key = (_POINT_TYPE, preflight["point"]["entity"]["id"])
    _point_rows_equal(before, after, selected_key)

    center_grid_by_part = {}
    for part_id, target in preflight["target_by_id"].items():
        geometry = target["geometry"]
        center = (
            (geometry["xmin"] + geometry["xmax"]) / 2.0,
            (geometry["ymin"] + geometry["ymax"]) / 2.0,
            geometry["z"],
        )
        matches = []
        for key in added_grid_keys:
            row = new_grids[key]
            if not _grid_control_default(_field(row, "CP")):
                continue
            position = _grid_position(row)
            if all(_close(position[i], center[i], geometry["width"]) for i in range(3)):
                matches.append(row)
        _require(len(matches) == 1, f"ANSAPART {part_id} lacks one projected center GRID")
        center_grid_by_part[part_id] = matches[0]

    fan_grid_keys = set()
    fan_shell_keys = []
    for part_id, target in preflight["target_by_id"].items():
        part_after = part_after_by_id[part_id]
        fan_grid_keys.update(
            _verify_fan(
                target, part_after, new_fe, new_grids, center_grid_by_part[part_id]
            )
        )
        fan_shell_keys.extend(
            (reference["type"], reference["id"])
            for reference in part_after["members"]
        )
    _require(fan_grid_keys == set(added_grid_keys),
             "the two new center GRIDs are not exactly the target fan projection nodes")
    _require(
        len(fan_shell_keys) == 8 and len(set(fan_shell_keys)) == 8,
        "the two verified target fans must contain eight distinct SHELLs",
    )
    perimeter_edge_keys = []
    for perimeter_row in added_perimeter_rows:
        _verify_new_perimeter_card(perimeter_row)
        edge_shells = perimeter_row.get("edge_shells")
        _require(
            type(edge_shells) is list and len(edge_shells) == 1,
            f"FE PERIMETER {perimeter_row['entity']['id']} must have exactly one edge SHELL",
        )
        shell_reference = edge_shells[0]
        _require(
            shell_reference.get("type") == "SHELL",
            f"FE PERIMETER {perimeter_row['entity']['id']} edge is not a SHELL",
        )
        perimeter_edge_keys.append(
            (shell_reference["type"], shell_reference["id"])
        )
    _require(
        Counter(perimeter_edge_keys) == Counter(fan_shell_keys),
        "the eight new FE PERIMETERs must cover each verified target fan SHELL exactly once",
    )

    new_rbars = [new_fe[key] for key in added_fe if key[0] == "RBAR"]
    _require(len(new_rbars) == 1, "realization must create exactly one RBAR")
    rbar = new_rbars[0]
    _require(rbar["entity"].get("type") == "RBAR",
             "new rigid element is not an RBAR")
    part_to_center = {
        part_id: row["entity"]["id"] for part_id, row in center_grid_by_part.items()
    }
    _require(
        _int_value(_field(rbar, "GA"), "RBAR GA") == part_to_center[preflight["p1_part_id"]]
        and _int_value(_field(rbar, "GB"), "RBAR GB") == part_to_center[preflight["p2_part_id"]],
        "RBAR GA/GB do not follow the selected point P1/P2 part order",
    )
    for field_name, expected in (("CNA", 123456), ("CNB", 0), ("CMA", 0), ("CMB", 0)):
        _require(_int_value(_field(rbar, field_name), f"RBAR {field_name}") == expected,
                 f"RBAR {field_name} does not match the fixed settings")


def _failed_result(params, deck, backup, before, after, errors, *, possible_effects,
                   api_invoked=False, api_return_type=None, api_return_value=None,
                   model_scope_matches=False):
    details = [str(item)[:2048] for item in errors[:16]]
    return {
        "ok": False,
        "tool": "connection_apply",
        "deck": deck,
        "point_id": params.get("point_id"),
        "target_part_ids": [item["part_id"] for item in params.get("target_parts", [])],
        "backup": backup,
        "requested_settings": dict(_FIXED_SETTINGS),
        "api_invoked": api_invoked,
        "api_return_type": api_return_type,
        "api_return_value": api_return_value,
        "before": _evidence(before),
        "after": _evidence(after),
        "scope_verified": False,
        "model_scope_matches": model_scope_matches,
        "possible_effects": possible_effects,
        "error": "; ".join(details)[:4096] or "connection apply was not verified",
        "errors": details or ["connection apply was not verified"],
    }


def execute(state, tool, params):
    """Realize one explicit SpotweldPoint_Type under fixed, verified RBAR settings."""
    state.assert_main()
    if type(tool) is not str or tool != "connection_apply":
        raise ValueError(f"unsupported connection apply operation: {tool!r}")
    params = _validate_inputs(params)
    if len(_encoded(params)) > _REQUEST_BUDGET:
        return _bounded_result(params, _failed_result(
            params, params["deck"], None, None, None,
            ["connection apply request exceeds the 128 KiB request budget"],
            possible_effects=False,
        ))

    from ansa import base, connections, constants, mesh

    deck = catalog._deck(base, params)
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int or deck != nastran:
        raise ValueError("controlled connection apply is supported only in the NASTRAN deck")
    if not callable(getattr(connections, "RealizeConnections", None)):
        raise NotImplementedError("ANSA connections.RealizeConnections is unavailable")
    if not callable(getattr(connections, "GetAllConnectionSettings", None)):
        raise NotImplementedError("ANSA connections.GetAllConnectionSettings is unavailable")
    if not callable(getattr(mesh, "GetFEPerimeterShells", None)):
        raise NotImplementedError("ANSA mesh.GetFEPerimeterShells is unavailable")
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    context = _execution_context(state, deck)

    backup_path, _prior = _save_path(params["backup_path"], False)
    current_path = _database_name(base)
    if model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_file = Path(current_path)
    if not current_file.is_file():
        raise ValueError("the current database file is unavailable for fingerprint verification")
    current_fingerprint = model_files._fingerprint(current_file)

    before, _before_handles = _capture_snapshot(base, deck, connections, mesh)
    if before.get("complete") is not True:
        return _bounded_result(params, _failed_result(
            params, deck, None, before, None,
            ["preflight snapshot is incomplete"]
            + [item["error"] for item in before["errors"][:8]],
            possible_effects=False,
        ))
    try:
        preflight = _point_preconditions(before, params)
    except Exception as exc:
        return _bounded_result(params, _failed_result(
            params, deck, None, before, None, [_error(exc)],
            possible_effects=False,
        ))
    if len(_encoded(before)) > _SNAPSHOT_BUDGET:
        return _bounded_result(params, _failed_result(
            params, deck, None, before, None,
            ["preflight snapshot exceeds the 256 KiB evidence budget; no backup or API call was made"],
            possible_effects=False,
        ))
    _same_context(state, deck, context)

    try:
        backup = _verify_backup(
            base, constants, backup_path, current_path, current_fingerprint
        )
    except Exception as exc:
        backup = _unconfirmed_backup(backup_path)
        try:
            after_save, _ = _capture_snapshot(base, deck, connections, mesh)
        except Exception as read_exc:
            after_save = {"complete": False, "errors": [{"scope": "post-backup", "error": _error(read_exc)}]}
        possible = (
            after_save.get("complete") is not True
            or not catalog._strict_equal(before, after_save)
        )
        try:
            possible = possible or not model_files._same_path(
                current_path, _database_name(base)
            )
            possible = possible or not catalog._strict_equal(
                current_fingerprint, model_files._fingerprint(current_file)
            )
            _same_context(state, deck, context)
        except Exception:
            possible = True
        return _bounded_result(params, _failed_result(
            params, deck, backup, before, after_save, [_error(exc)],
            possible_effects=possible,
        ))

    try:
        _same_context(state, deck, context)
        after_backup, after_backup_handles = _capture_snapshot(
            base, deck, connections, mesh
        )
        if after_backup.get("complete") is not True:
            raise RuntimeError("post-backup snapshot is incomplete")
        if not catalog._strict_equal(before, after_backup):
            raise RuntimeError("model contents changed while creating the backup")
        _point_preconditions(after_backup, params)
        if len(_encoded(after_backup)) > _SNAPSHOT_BUDGET:
            raise ValueError("post-backup snapshot exceeds the 256 KiB evidence budget")
        _same_context(state, deck, context)
    except Exception as exc:
        return _bounded_result(params, _failed_result(
            params, deck, backup, before, locals().get("after_backup"), [_error(exc)],
            possible_effects=True,
        ))

    point_key = (_POINT_TYPE, params["point_id"])
    point_entity = after_backup_handles["point_entities"].get(point_key)
    if point_entity is None:
        return _bounded_result(params, _failed_result(
            params, deck, backup, before, after_backup,
            ["selected point could not be resolved from the fresh complete collection"],
            possible_effects=False,
        ))

    api_return = None
    api_return_type = None
    api_error = None
    try:
        _same_context(state, deck, context)
        api_return = connections.RealizeConnections([point_entity], dict(_FIXED_SETTINGS))
        api_return_type = type(api_return).__name__
    except Exception as exc:
        api_error = _error(exc)
        api_return_type = "exception"

    try:
        after, _after_handles = _capture_snapshot(base, deck, connections, mesh)
    except Exception as exc:
        after = {
            "complete": False,
            "deck": deck,
            "errors": [{"scope": "post-realization", "error": _error(exc)}],
        }
    errors = []
    scope_matches = False
    if api_error:
        errors.append("RealizeConnections raised: " + api_error)
    api_return_summary = None
    if not api_error and (type(api_return) is not int or api_return != 1):
        api_return_summary = _api_return_summary(api_return)
        errors.append(
            "RealizeConnections must return strict int 1; received "
            + _encoded(api_return_summary).decode("utf-8")[:512]
        )
    try:
        _verify_transition(before, after, preflight)
        scope_matches = True
    except Exception as exc:
        errors.append(_error(exc))
    if after.get("complete") is not True or len(_encoded(after)) > _SNAPSHOT_BUDGET:
        errors.append("post-realization snapshot is incomplete or exceeds the 256 KiB evidence budget")
        scope_matches = False
    try:
        if not model_files._same_path(current_path, _database_name(base)):
            raise RuntimeError("current database path changed during connection apply")
        if not catalog._strict_equal(
            current_fingerprint, model_files._fingerprint(current_file)
        ):
            raise RuntimeError("current database file changed during connection apply")
        if not catalog._strict_equal(
            backup, _backup_record(backup_path)
        ):
            raise RuntimeError("verified backup changed during connection apply")
        _same_context(state, deck, context)
    except Exception as exc:
        errors.append(_error(exc))
        scope_matches = False

    ok = type(api_return) is int and api_return == 1 and not api_error and not errors
    result = {
        "ok": ok,
        "tool": "connection_apply",
        "deck": deck,
        "point_id": params["point_id"],
        "target_part_ids": [item["part_id"] for item in params["target_parts"]],
        "backup": backup,
        "requested_settings": dict(_FIXED_SETTINGS),
        "api_invoked": True,
        "api_return_type": api_return_type,
        "api_return_value": (
            api_return if type(api_return) is int and api_return == 1 else None
        ),
        "before": _evidence(before),
        "after": _evidence(after),
        "evidence_truncated": (
            len(_encoded(before)) > _SNAPSHOT_BUDGET
            or len(_encoded(after)) > _SNAPSHOT_BUDGET
        ),
        "scope_verified": scope_matches and not errors,
        "model_scope_matches": scope_matches,
        "possible_effects": not ok,
        "error": "; ".join(errors)[:4096] if errors else None,
        "errors": errors[:16],
        "api_return_summary": api_return_summary,
    }
    return _bounded_result(params, result)


__all__ = ["execute"]
