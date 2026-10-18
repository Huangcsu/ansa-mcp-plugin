"""Guarded NASTRAN CBAR-to-CROD conversion with verified model scope."""

import hashlib
import json
import math
import os
import re
from pathlib import Path

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import _model_save, _save_path, entity_catalog as catalog, model_files


_MAX_BATCH = 100
_MAX_FE_REFERENCES = 10000
_SNAPSHOT_BUDGET = MAX_FRAME_BYTES // 4
_RESULT_BUDGET = MAX_FRAME_BYTES * 3 // 4
_REQUEST_BUDGET = MAX_FRAME_BYTES // 8
_REQUIRED_SOURCE_FIELDS = frozenset({"PID", "GA", "GB"})
_REQUIRED_PROPERTY_FIELDS = frozenset({"MID", "A", "J", "C", "NSM"})
_BAR_FIELDS = (
    "PID", "GA", "GB", "Name", "FROZEN", "FROZEN_ID", "FROZEN_DELETE",
    "Orient", "X1", "X2", "X3", "OFFT", "PA", "PB",
    "W1A", "W2A", "W3A", "W1B", "W2B", "W3B",
)
_ROD_FIELDS = (
    "PID", "G1", "G2", "Name", "FROZEN", "FROZEN_ID", "FROZEN_DELETE",
)
_GRID_FIELDS = ("CP", "X1", "X2", "X3", "CD")
_RELEASE_FIELDS = ("PA", "PB", "W1A", "W2A", "W3A", "W1B", "W2B", "W3B")
_FREEZE_FIELDS = ("FROZEN", "FROZEN_ID", "FROZEN_DELETE")


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _execution_context(state, deck):
    state.check_conflict()
    probe = state.probe()
    model, grant = probe.get("model"), probe.get("authorization")
    if (type(model) is not dict or type(model.get("epoch")) is not int
            or model["epoch"] <= 0 or model.get("write_enabled") is not True
            or probe.get("deck") != deck or probe.get("connected") is not True
            or probe.get("stopped") is not False or probe.get("persistence_blocked") is not False
            or type(grant) is not dict or grant.get("active") is not True
            or not grant.get("backend_id") or type(grant.get("generation")) is not int
            or grant["generation"] <= 0
            or getattr(state, "connected_backend_id", None) != grant["backend_id"]):
        raise RuntimeError("element conversion model context or write authorization is unavailable")
    identity = {key: probe.get(key) for key in ("pid", "process_start", "instance_id", "session_id")}
    if any(value is None for value in identity.values()):
        raise RuntimeError("element conversion process context is incomplete")
    return {**identity, "deck": deck, "epoch": model["epoch"],
            "backend_id": grant["backend_id"], "generation": grant["generation"]}


def _same_context(state, deck, expected):
    current = _execution_context(state, deck)
    if current != expected:
        raise RuntimeError("model context changed before element conversion")


def _bounded_result(result):
    result = dict(result)
    if type(result.get("error")) is str:
        result["error"] = result["error"][:4096]
    result["errors"] = [str(item)[:2048] for item in result.get("errors", [])[:16]]
    if len(_encoded(result)) <= _RESULT_BUDGET:
        return result
    result = dict(result)
    for name in ("before", "after"):
        snapshot = result.get(name)
        if snapshot is not None:
            encoded = _encoded(snapshot)
            result[name] = {"complete": snapshot.get("complete"), "details_omitted": True,
                            "serialized_bytes": len(encoded),
                            "sha256": hashlib.sha256(encoded).hexdigest()}
    result.update(ok=False, scope_verified=False, possible_effects=True, evidence_truncated=True)
    errors = [str(item)[:2048] for item in result.get("errors", [])[:16]]
    errors.append("conversion evidence exceeds the reply budget; snapshot digests retained; independently read back before further writes")
    result.update(errors=errors, error="; ".join(errors)[:4096])
    return result


def _unconfirmed_backup(path):
    record = {"path": str(path), "saved": False, "verified": False,
              "exists": None, "observation_errors": []}
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


def _positive_finite(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a positive finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or value <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return value


def _zero_or_unset(value, label):
    if value is None:
        return
    if type(value) in (int, float):
        try:
            if math.isfinite(value) and value == 0:
                return
        except (OverflowError, TypeError):
            pass
    raise ValueError(f"{label} must be unset or zero for CBAR-to-CROD conversion")


def _card_fields(entity, deck, label):
    getter = getattr(entity, "card_fields", None)
    if not callable(getter):
        raise NotImplementedError(f"{label} card_fields capability is unavailable")
    fields = getter(deck)
    if type(fields) not in (list, tuple):
        raise RuntimeError(f"{label} card_fields did not return a field-name list")
    if any(type(name) is not str or not name for name in fields):
        raise RuntimeError(f"{label} card_fields returned an invalid field name")
    if len(fields) != len(set(fields)):
        raise RuntimeError(f"{label} card_fields returned duplicate field names")
    return tuple(fields)


def _read_fixed_card(base, deck, entity, reference, required_fields, label):
    fields = _card_fields(entity, deck, f"{label} {reference['id']}")
    missing = sorted(set(required_fields) - set(fields))
    if missing:
        raise NotImplementedError(
            f"{label} {reference['id']} lacks required stable field(s): {', '.join(missing)}"
        )
    values = catalog._read_card(base, deck, entity, reference, required_fields)
    return fields, values


def _collect(base, deck, entity_type, *, expected_type=None, maximum=None):
    collect = getattr(base, "CollectEntities", None)
    if not callable(collect):
        raise NotImplementedError("ANSA base.CollectEntities is unavailable")
    entities = collect(
        deck, None, entity_type, False, filter_visible=False, hidden_entities=True
    )
    if type(entities) not in (list, tuple):
        raise RuntimeError(f"CollectEntities({entity_type}) did not return a complete entity list")
    if maximum is not None and len(entities) > maximum:
        raise ValueError(f"{entity_type} collection exceeds the {maximum}-entity limit")

    pairs = []
    seen = set()
    for entity in entities:
        reference = catalog._entity_reference(base, deck, entity, expected_type)
        key = (reference["type"], reference["id"])
        if key in seen:
            raise RuntimeError(f"{entity_type} collection contains duplicate identities")
        seen.add(key)
        pairs.append((entity, reference))
    pairs.sort(key=lambda pair: (pair[1]["type"], pair[1]["id"]))
    return pairs


def _property_rows(base, deck, entity_type, errors):
    try:
        pairs = _collect(base, deck, entity_type, expected_type=entity_type)
    except Exception as exc:
        errors.append({"scope": entity_type, "error": _error(exc)})
        return None

    rows = []
    for entity, reference in pairs:
        try:
            fields = _card_fields(entity, deck, f"{entity_type} {reference['id']}")
            stable_fields = tuple(
                name for name in fields
                if re.sub(r"[^a-z0-9]", "", name.casefold()) != "numelem"
            )
            if not stable_fields:
                raise RuntimeError(f"{entity_type} has no stable card definition")
            values = catalog._read_card(base, deck, entity, reference, stable_fields)
            rows.append({
                "entity": reference,
                "card_fields": list(stable_fields),
                "fields": values,
            })
        except Exception as exc:
            errors.append({
                "scope": entity_type,
                "entity": reference,
                "error": _error(exc),
            })
    return rows


def _capture_snapshot(base, deck):
    snapshot = {
        "deck": deck,
        "complete": False,
        "fe_references": None,
        "element_cards": {"CBAR": None, "CROD": None},
        "grids": None,
        "properties": {"PBAR": None, "PROD": None},
        "errors": [],
    }
    errors = snapshot["errors"]

    try:
        fe_pairs = _collect(
            base, deck, "__ELEMENTS__", maximum=_MAX_FE_REFERENCES
        )
        snapshot["fe_references"] = [reference for _, reference in fe_pairs]
        cards = {"CBAR": [], "CROD": []}
        for entity, reference in fe_pairs:
            entity_type = reference["type"]
            if entity_type not in cards:
                continue
            required = _BAR_FIELDS if entity_type == "CBAR" else _ROD_FIELDS
            try:
                card_fields, values = _read_fixed_card(
                    base, deck, entity, reference, required, entity_type
                )
                cards[entity_type].append({
                    "entity": reference,
                    "card_fields": list(card_fields),
                    "fields": values,
                })
            except Exception as exc:
                errors.append({
                    "scope": entity_type,
                    "entity": reference,
                    "error": _error(exc),
                })
        for rows in cards.values():
            rows.sort(key=lambda row: row["entity"]["id"])
        snapshot["element_cards"] = cards
    except Exception as exc:
        errors.append({"scope": "fe_references", "error": _error(exc)})

    try:
        grid_pairs = _collect(base, deck, "GRID", expected_type="GRID")
        grids = []
        for entity, reference in grid_pairs:
            try:
                _fields, values = _read_fixed_card(
                    base, deck, entity, reference, _GRID_FIELDS, "GRID"
                )
                for name in ("X1", "X2", "X3"):
                    value = values[name]
                    if type(value) not in (int, float):
                        raise RuntimeError(f"GRID {reference['id']} {name} is not numeric")
                    try:
                        finite = math.isfinite(value)
                    except (OverflowError, TypeError):
                        finite = False
                    if not finite:
                        raise RuntimeError(f"GRID {reference['id']} {name} is not finite")
                grids.append({
                    "entity": reference,
                    "fields": values,
                })
            except Exception as exc:
                errors.append({"scope": "GRID", "entity": reference, "error": _error(exc)})
        snapshot["grids"] = grids
    except Exception as exc:
        errors.append({"scope": "GRID", "error": _error(exc)})

    for entity_type in ("PBAR", "PROD"):
        snapshot["properties"][entity_type] = _property_rows(
            base, deck, entity_type, errors
        )

    snapshot["complete"] = (
        not errors
        and snapshot["fe_references"] is not None
        and snapshot["element_cards"]["CBAR"] is not None
        and snapshot["element_cards"]["CROD"] is not None
        and snapshot["grids"] is not None
        and all(value is not None for value in snapshot["properties"].values())
    )
    return snapshot


def _require_complete(snapshot, label):
    if snapshot.get("complete") is not True:
        detail = "; ".join(
            item.get("error", "unknown snapshot error")
            for item in snapshot.get("errors", [])
        )
        raise RuntimeError(f"{label} snapshot is incomplete" + (f": {detail}" if detail else ""))


def _row_map(rows):
    return {row["entity"]["id"]: row for row in rows}


def _validate_inputs(params):
    required = (
        "deck", "source_type", "target_type", "elements", "target_property_id",
        "expected_property_fields", "backup_path",
    )
    params = catalog._object(params, required, label="params")
    if type(params["deck"]) is not int:
        raise TypeError("deck must be an integer")
    if type(params["source_type"]) is not str or params["source_type"] != "CBAR":
        raise ValueError("source_type must be CBAR")
    if type(params["target_type"]) is not str or params["target_type"] != "CROD":
        raise ValueError("target_type must be CROD")

    elements = params["elements"]
    if type(elements) is not list or not 1 <= len(elements) <= _MAX_BATCH:
        raise ValueError(f"elements must contain 1 to {_MAX_BATCH} CBAR conditions")
    normalized = []
    ids = []
    for index, item in enumerate(elements):
        item = catalog._object(
            item, ("entity_id", "expected_fields"), label=f"elements[{index}]"
        )
        entity_id = catalog._positive_int(item["entity_id"], f"elements[{index}].entity_id")
        expected = item["expected_fields"]
        if type(expected) is not dict or not expected:
            raise ValueError(f"elements[{index}].expected_fields must be a non-empty object")
        if not _REQUIRED_SOURCE_FIELDS.issubset(expected):
            missing = sorted(_REQUIRED_SOURCE_FIELDS - expected.keys())
            raise ValueError(
                f"elements[{index}].expected_fields must include: {', '.join(missing)}"
            )
        normalized.append({"entity_id": entity_id, "expected_fields": expected})
        ids.append(entity_id)
    if len(ids) != len(set(ids)):
        raise ValueError("elements must not contain duplicate entity IDs")

    property_id = catalog._positive_int(params["target_property_id"], "target_property_id")
    expected_property = params["expected_property_fields"]
    if type(expected_property) is not dict or not expected_property:
        raise ValueError("expected_property_fields must be a non-empty object")
    if not _REQUIRED_PROPERTY_FIELDS.issubset(expected_property):
        missing = sorted(_REQUIRED_PROPERTY_FIELDS - expected_property.keys())
        raise ValueError(
            "expected_property_fields must include: " + ", ".join(missing)
        )
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise TypeError("backup_path must be a non-empty absolute .ansa path")

    return {
        "deck": params["deck"],
        "source_type": params["source_type"],
        "target_type": params["target_type"],
        "elements": normalized,
        "target_property_id": property_id,
        "expected_property_fields": expected_property,
        "backup_path": params["backup_path"],
    }, ids


def _freeze_and_offset_preconditions(fields, entity_id):
    for name in _FREEZE_FIELDS:
        value = fields[name]
        if type(value) is not str or value.casefold() != "no":
            raise ValueError(f"CBAR {entity_id} is frozen or has an unknown {name} state")
    for name in _RELEASE_FIELDS:
        _zero_or_unset(fields[name], f"CBAR {entity_id} {name}")
    offt = fields["OFFT"]
    if offt not in (None, ""):
        raise ValueError(f"CBAR {entity_id} has a non-default OFFT")


def _validate_conditions(base, deck, params, snapshot):
    _require_complete(snapshot, "preflight")
    bars = _row_map(snapshot["element_cards"]["CBAR"])
    rods = _row_map(snapshot["element_cards"]["CROD"])
    grids = _row_map(snapshot["grids"])
    pbars = _row_map(snapshot["properties"]["PBAR"])
    prods = _row_map(snapshot["properties"]["PROD"])

    ids = [item["entity_id"] for item in params["elements"]]
    collision = sorted(set(ids) & rods.keys())
    if collision:
        raise ValueError(f"target CROD IDs already exist: {collision}")

    source_entities = []
    for index, item in enumerate(params["elements"]):
        entity_id = item["entity_id"]
        entity, reference, _expected, _labels = catalog._validate_entity_values(
            base, deck, "CBAR", entity_id, item["expected_fields"]
        )
        row = bars.get(entity_id)
        if row is None or row["entity"] != reference:
            raise RuntimeError(f"CBAR {entity_id} is missing from the complete FE snapshot")
        fields = row["fields"]
        _freeze_and_offset_preconditions(fields, entity_id)

        source_pid = catalog._positive_int(fields["PID"], f"CBAR {entity_id} PID")
        _property, property_reference = catalog._get_entity(
            base, deck, "PBAR", source_pid
        )
        if source_pid not in pbars or pbars[source_pid]["entity"] != property_reference:
            raise RuntimeError(f"CBAR {entity_id} PBAR {source_pid} is missing from the full snapshot")

        node_a = catalog._positive_int(fields["GA"], f"CBAR {entity_id} GA")
        node_b = catalog._positive_int(fields["GB"], f"CBAR {entity_id} GB")
        if node_a == node_b:
            raise ValueError(f"CBAR {entity_id} has identical GA and GB")
        for node_id, field in ((node_a, "GA"), (node_b, "GB")):
            _node, node_reference = catalog._get_entity(base, deck, "GRID", node_id)
            if node_id not in grids or grids[node_id]["entity"] != node_reference:
                raise RuntimeError(
                    f"CBAR {entity_id} {field} GRID {node_id} is missing from the full snapshot"
                )
        source_entities.append(entity)

    property_entity, property_reference, _expected, _labels = catalog._validate_entity_values(
        base, deck, "PROD", params["target_property_id"],
        params["expected_property_fields"],
    )
    target_property = prods.get(params["target_property_id"])
    if target_property is None or target_property["entity"] != property_reference:
        raise RuntimeError("target PROD is missing from the full property snapshot")
    target_fields = catalog._read_card(
        base, deck, property_entity, property_reference, ("MID", "A")
    )
    material_id = catalog._positive_int(target_fields["MID"], "target PROD MID")
    _positive_finite(target_fields["A"], "target PROD A")
    catalog._get_entity(base, deck, "MAT1", material_id)
    return source_entities


def _database_name(base):
    getter = getattr(base, "DataBaseName", None)
    if not callable(getter):
        raise NotImplementedError("ANSA base.DataBaseName is unavailable")
    value = getter()
    if value in (None, ""):
        return value
    if type(value) is not str:
        raise RuntimeError("ANSA DataBaseName did not return a path string")
    return value


def _same_database_name(left, right):
    if not left or not right:
        return not left and not right
    return model_files._same_path(left, right)


def _backup_record(path, saved=None):
    fingerprint = model_files._fingerprint(path)
    record = {"path": str(path), "saved": True, "verified": True, "exists": True, **fingerprint}
    if type(saved) is dict and type(saved.get("warnings")) is list:
        record["warnings"] = [item for item in saved["warnings"] if type(item) is str]
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

    backup = _backup_record(backup_path, saved)
    current_after = _database_name(base)
    if not _same_database_name(current_path, current_after):
        raise RuntimeError("SaveAs changed the current database path")
    if current_path:
        current_after_fingerprint = model_files._fingerprint(Path(current_path))
        if not catalog._strict_equal(current_fingerprint, current_after_fingerprint):
            raise RuntimeError("SaveAs changed the current database file")
        try:
            same_file = os.path.samefile(backup_path, Path(current_path))
        except OSError as exc:
            raise RuntimeError(f"could not verify backup identity: {exc}") from exc
        if same_file:
            raise RuntimeError("backup resolves to the current database file")
    return backup


def _conversion_scope_errors(before, after, selected_ids, target_property_id):
    errors = []
    if after.get("complete") is not True:
        errors.append("post-conversion model snapshot is incomplete")
    for item in after.get("errors", []):
        scope = item.get("scope", "snapshot")
        detail = item.get("error", "unknown readback error")
        errors.append(f"post-conversion readback ({scope}): {detail}")
    if before.get("fe_references") is None or after.get("fe_references") is None:
        errors.append("full FE reference set is unknown")
    else:
        before_refs = {
            (item["type"], item["id"]) for item in before["fe_references"]
        }
        after_refs = {
            (item["type"], item["id"]) for item in after["fe_references"]
        }
        expected_refs = set(before_refs)
        for entity_id in selected_ids:
            expected_refs.discard(("CBAR", entity_id))
            expected_refs.add(("CROD", entity_id))
        if after_refs != expected_refs:
            errors.append("FE references changed outside the selected CBAR-to-CROD IDs")

    if before.get("grids") is None or after.get("grids") is None:
        errors.append("GRID identity or coordinate readback is unknown")
    elif not catalog._strict_equal(before["grids"], after["grids"]):
        errors.append("GRID identity or coordinates changed")

    if before.get("properties") is None or after.get("properties") is None:
        errors.append("PBAR/PROD property definitions are unknown")
    elif not catalog._strict_equal(before["properties"], after["properties"]):
        errors.append("PBAR/PROD property definitions changed")

    before_cards = before.get("element_cards") or {}
    after_cards = after.get("element_cards") or {}
    if any(before_cards.get(name) is None or after_cards.get(name) is None
           for name in ("CBAR", "CROD")):
        errors.append("CBAR/CROD card readback is unknown")
        return errors

    before_bars = _row_map(before_cards["CBAR"])
    after_bars = _row_map(after_cards["CBAR"])
    before_rods = _row_map(before_cards["CROD"])
    after_rods = _row_map(after_cards["CROD"])
    selected = set(selected_ids)
    if set(after_bars) != set(before_bars) - selected:
        errors.append("CBAR set does not equal the expected unselected set")
    if set(after_rods) != set(before_rods) | selected:
        errors.append("CROD set does not equal the expected existing-plus-converted set")

    for entity_id in sorted(set(before_bars) - selected):
        if entity_id in after_bars and not catalog._strict_equal(
            before_bars[entity_id], after_bars[entity_id]
        ):
            errors.append(f"unselected CBAR {entity_id} changed")
    for entity_id in sorted(before_rods):
        if entity_id in after_rods and not catalog._strict_equal(
            before_rods[entity_id], after_rods[entity_id]
        ):
            errors.append(f"unselected CROD {entity_id} changed")

    for entity_id in selected:
        source = before_bars.get(entity_id)
        target = after_rods.get(entity_id)
        if source is None or target is None:
            errors.append(f"converted element {entity_id} identity is unknown")
            continue
        expected_fields = {
            "PID": target_property_id,
            "G1": source["fields"]["GA"],
            "G2": source["fields"]["GB"],
            "Name": source["fields"]["Name"],
            **{name: source["fields"][name] for name in _FREEZE_FIELDS},
        }
        if target["entity"] != {"deck": before["deck"], "type": "CROD", "id": entity_id}:
            errors.append(f"converted element {entity_id} CROD identity changed")
        changed = [
            name for name, value in expected_fields.items()
            if name not in target["fields"]
            or not catalog._strict_equal(target["fields"][name], value)
        ]
        if changed:
            errors.append(
                f"converted element {entity_id} has incorrect target field(s): "
                + ", ".join(changed)
            )
    return errors


def _failed_result(deck, ids, target_property_id, backup, before, after, errors,
                   *, possible_effects, api_return_code=None, api_return_type=None,
                   api_error=None, model_scope_matches=None):
    details = list(errors)
    if api_error:
        details.insert(0, api_error)
    result = {
        "ok": False,
        "deck": deck,
        "source_type": "CBAR",
        "target_type": "CROD",
        "requested_ids": ids,
        "target_property_id": target_property_id,
        "backup": backup,
        "before": before,
        "after": after,
        "scope_verified": False,
        "model_scope_matches": model_scope_matches,
        "possible_effects": possible_effects,
        "api_return_code": api_return_code,
        "api_return_type": api_return_type,
        "error": "; ".join(details) if details else "element type conversion was not verified",
        "errors": details,
    }
    return _bounded_result(result)


def execute(state, tool, params):
    """Convert only explicitly conditioned NASTRAN CBARs to CRODs."""
    state.assert_main()
    if type(tool) is not str or tool != "element_change_type":
        raise ValueError(f"unsupported element type operation: {tool!r}")
    params, ids = _validate_inputs(params)
    if len(_encoded(params)) > _REQUEST_BUDGET:
        raise ValueError("element conversion request exceeds the reply budget; no backup or conversion was performed")

    from ansa import base, constants

    deck = catalog._deck(base, params)
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int or deck != nastran:
        raise ValueError("controlled element type conversion is supported only in NASTRAN")
    if not callable(getattr(base, "ChangeElemType", None)):
        raise NotImplementedError("ANSA base.ChangeElemType is unavailable")
    if not callable(getattr(base, "SaveAs", None)):
        raise NotImplementedError("ANSA base.SaveAs is unavailable")
    context = _execution_context(state, deck)

    backup_path, _prior = _save_path(params["backup_path"], False)
    current_path = _database_name(base)
    if current_path and model_files._same_path(backup_path, current_path):
        raise ValueError("backup_path must differ from the current database path")
    current_fingerprint = (
        model_files._fingerprint(Path(current_path)) if current_path else None
    )

    before = _capture_snapshot(base, deck)
    _require_complete(before, "preflight")
    _validate_conditions(base, deck, params, before)
    if len(_encoded(before)) > _SNAPSHOT_BUDGET:
        raise ValueError("model snapshot exceeds the element conversion reply budget; no backup or conversion was performed")
    _same_context(state, deck, context)

    backup = None
    try:
        backup = _verify_backup(
            base, constants, backup_path, current_path, current_fingerprint
        )
    except Exception as exc:
        backup = _unconfirmed_backup(backup_path)
        try:
            after_save = _capture_snapshot(base, deck)
        except Exception as read_exc:
            after_save = None
            read_error = _error(read_exc)
        else:
            read_error = None
        try:
            current_after = _database_name(base)
            possible = (after_save is None or after_save.get("complete") is not True
                        or not catalog._strict_equal(before, after_save)
                        or not _same_database_name(current_path, current_after))
            if current_path:
                possible = possible or not catalog._strict_equal(
                    current_fingerprint, model_files._fingerprint(Path(current_path))
                )
            _same_context(state, deck, context)
        except Exception:
            possible = True
        backup_error = _error(exc)
        errors = [backup_error]
        if read_error:
            errors.append("post-save readback: " + read_error)
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after_save,
            errors, possible_effects=possible,
        )

    after_backup = None
    try:
        _same_context(state, deck, context)
        after_backup = _capture_snapshot(base, deck)
        _require_complete(after_backup, "post-backup")
        _validate_conditions(base, deck, params, after_backup)
    except Exception as exc:
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after_backup,
            [_error(exc)], possible_effects=True,
        )
    if not catalog._strict_equal(before, after_backup):
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after_backup,
            ["model snapshot changed after backup and before conversion"],
            possible_effects=True,
        )

    try:
        conversion_entities = _validate_conditions(base, deck, params, after_backup)
        _same_context(state, deck, context)
    except Exception as exc:
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after_backup,
            [_error(exc)], possible_effects=True,
        )

    api_error = None
    status = None
    try:
        status = base.ChangeElemType(
            deck,
            conversion_entities,
            target_name="CROD",
            pid=params["target_property_id"],
        )
    except Exception as exc:
        api_error = _error(exc)

    try:
        after = _capture_snapshot(base, deck)
    except Exception as exc:
        after = None
        readback_error = _error(exc)
    else:
        readback_error = None

    scope_errors = (
        ["post-conversion model snapshot is unknown"]
        if after is None
        else _conversion_scope_errors(before, after, ids, params["target_property_id"])
    )
    if readback_error:
        scope_errors.append("post-conversion readback: " + readback_error)
    model_scope_matches = not scope_errors
    api_ok = api_error is None and type(status) is int and status == 0
    if not api_ok:
        if api_error is None:
            scope_errors.insert(0, "ChangeElemType did not return the exact success value int 0")
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after,
            scope_errors, possible_effects=True,
            api_return_code=status if type(status) is int else None,
            api_return_type=type(status).__name__ if status is not None else None,
            api_error=api_error,
            model_scope_matches=model_scope_matches,
        )
    if not model_scope_matches:
        return _failed_result(
            deck, ids, params["target_property_id"], backup, before, after,
            scope_errors, possible_effects=True,
            api_return_code=0,
            api_return_type="int",
            model_scope_matches=False,
        )

    return _bounded_result({
        "ok": True,
        "deck": deck,
        "source_type": "CBAR",
        "target_type": "CROD",
        "requested_ids": ids,
        "target_property_id": params["target_property_id"],
        "backup": backup,
        "before": before,
        "after": after,
        "scope_verified": True,
        "model_scope_matches": True,
        "possible_effects": False,
        "api_return_code": 0,
        "api_return_type": "int",
        "error": None,
        "errors": [],
    })


__all__ = ["execute"]
