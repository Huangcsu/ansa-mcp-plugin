"""Explicit FACE macro meshing and guarded SHELL deletion on the GUI thread."""

from __future__ import annotations

import math
import uuid
from pathlib import Path

from . import _params
from . import entity_catalog as catalog


_SETTINGS = ("mesh_change_option", "element_type", "element_order", "target_element_length")
_FACE_REQUIRED_FIELDS = ("ID", "PID", "Frozen")
_FACE_OPTIONAL_FIELDS = ("Thickness",)
_SHELL_REQUIRED_FIELDS = ("type", "PID", "G1", "G2", "G3", "G4")
_SHELL_OPTIONAL_FIELDS = ("EID",)
_SHELL_HIGH_ORDER_FIELDS = ("G5", "G6", "G7", "G8", "G9")
_FACE_DERIVED_FIELDS = ("Elem.Length", "Num.Quads", "Num.Trias")
_NODE_FIELDS = ("X", "Y", "Z")
_EDGE_REL_TOL = 1e-9
_EDGE_ABS_TOL = 1e-12
_EDGE_LENGTH_UPPER_FACTOR = 1.5
_ELEMENT_TYPES = ("tria", "quad")
_MAX_SHELL_IDS = 2000


def _ids(value, label, maximum, *, empty=False):
    if type(value) is not list or len(value) > maximum or (not value and not empty):
        raise ValueError(f"{label} must contain {'0' if empty else '1'} to {maximum} IDs")
    ids = [catalog._positive_int(item, label) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} must not contain duplicate IDs")
    return ids


def _references(base, deck, entities, entity_type, label):
    if not isinstance(entities, (list, tuple)):
        raise RuntimeError(f"{label} did not return an entity list")
    references = [catalog._entity_reference(base, deck, entity, entity_type) for entity in entities]
    ids = [reference["id"] for reference in references]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"{label} returned duplicate entity IDs")
    return references


def _all_shell_ids(base, deck):
    references = _references(base, deck, base.CollectEntities(deck, None, "SHELL"), "SHELL", "CollectEntities")
    return {reference["id"] for reference in references}


def _available_fields(entity, deck, required, optional, label):
    labels = catalog._entity_card_labels(entity, deck)
    missing = [name for name in required if name not in labels]
    if missing:
        raise RuntimeError(
            f"{label} card does not expose required field(s): " + ", ".join(missing)
        )
    return tuple(name for name in (*required, *optional) if name in labels)


def _field_id(value, label):
    if type(value) is int and value > 0:
        return value
    if type(value) is dict and set(value) == {"deck", "type", "id"}:
        entity_id = value["id"]
        if type(entity_id) is int and entity_id > 0:
            return entity_id
    raise RuntimeError(f"{label} did not return a positive entity ID")


def _number(value, label):
    if type(value) is bool or type(value) not in (int, float, str):
        raise RuntimeError(f"{label} did not return a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} did not return a finite number") from exc
    if not math.isfinite(result):
        raise RuntimeError(f"{label} did not return a finite number")
    return result


def _integer(value, label):
    result = _number(value, label)
    if not result.is_integer() or result < 0:
        raise RuntimeError(f"{label} did not return a non-negative integer")
    return int(result)


def _face_derived(base, deck, face, reference):
    fields = _available_fields(face, deck, _FACE_DERIVED_FIELDS, (), "FACE")
    values = catalog._read_card(base, deck, face, reference, fields)
    return {
        "element_length": _number(values["Elem.Length"], "Elem.Length"),
        "num_quads": _integer(values["Num.Quads"], "Num.Quads"),
        "num_trias": _integer(values["Num.Trias"], "Num.Trias"),
    }


def _bounds(base, entities, label):
    method = getattr(base, "BoundBox", None)
    if not callable(method):
        raise RuntimeError("ansa.base.BoundBox is unavailable")
    raw = method(list(entities))
    if type(raw) not in (list, tuple) or len(raw) != 6:
        raise RuntimeError(f"{label} BoundBox did not return six coordinates")
    values = [_number(value, f"{label} BoundBox") for value in raw]
    if any(values[index] > values[index + 3] for index in range(3)):
        raise RuntimeError(f"{label} BoundBox minimum exceeds maximum")
    return values


def _point_bounds(points):
    if not points:
        raise RuntimeError("mesh readback did not return any nodes")
    return [
        min(point[axis] for point in points)
        for axis in range(3)
    ] + [
        max(point[axis] for point in points)
        for axis in range(3)
    ]


def _node_position(base, deck, node_id):
    node, reference = catalog._get_entity(base, deck, "NODE", node_id)
    fields = _available_fields(node, deck, _NODE_FIELDS, (), "NODE")
    values = catalog._read_card(base, deck, node, reference, fields)
    return [_number(values[name], f"NODE {node_id} {name}") for name in _NODE_FIELDS]


def _distance(first, second):
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _close(left, right, scale=1.0):
    return math.isclose(left, right, rel_tol=_EDGE_REL_TOL,
                        abs_tol=max(_EDGE_ABS_TOL, _EDGE_REL_TOL * abs(scale)))


def _empty_field(value):
    if value is None:
        return True
    if type(value) is bool:
        return not value
    if type(value) in (int, float):
        return value == 0
    if type(value) is str:
        text = value.strip()
        if not text:
            return True
        try:
            return float(text) == 0
        except ValueError:
            return False
    if type(value) in (list, tuple, dict):
        return not value
    return False


def _cons_scope(base, deck, faces, selected_face_ids):
    if not callable(getattr(base, "GetFacesOfCons", None)):
        raise RuntimeError("ansa.base.GetFacesOfCons is unavailable")
    cons_by_id = {}
    for face in faces:
        collected = base.CollectEntities(deck, face, "CONS")
        if collected is None or collected == 0:
            collected = []
        if type(collected) not in (list, tuple):
            raise RuntimeError("CollectEntities did not return a CONS list")
        for cons in collected:
            reference = catalog._entity_reference(base, deck, cons, "CONS")
            cons_by_id[reference["id"]] = cons
    if not cons_by_id:
        raise ValueError("no CONS were found for the selected FACE macros")

    cons_list = [cons_by_id[cons_id] for cons_id in sorted(cons_by_id)]
    scope_rows = []
    for cons_id, cons in zip(sorted(cons_by_id), cons_list):
        adjacent = base.GetFacesOfCons(cons)
        if type(adjacent) not in (list, tuple) or not adjacent:
            raise RuntimeError(f"CONS {cons_id} did not return adjacent FACE entities")
        references = _references(base, deck, adjacent, "FACE", "GetFacesOfCons")
        face_ids = sorted(reference["id"] for reference in references)
        outside = sorted(set(face_ids) - selected_face_ids)
        if outside:
            raise ValueError(
                f"CONS {cons_id} is shared with FACE(s) outside the selected macros: "
                + ", ".join(str(face_id) for face_id in outside)
            )
        scope_rows.append({"cons_id": cons_id, "face_ids": face_ids})
    return cons_list, {
        "cons_ids": sorted(cons_by_id),
        "cons_count": len(cons_list),
        "selected_face_ids": sorted(selected_face_ids),
        "adjacent_faces": scope_rows,
    }


def _face_snapshot(base, mesh, deck, entity_id):
    face, reference = catalog._get_entity(base, deck, "FACE", entity_id)
    fields = catalog._read_card(
        base, deck, face, reference,
        _available_fields(face, deck, _FACE_REQUIRED_FIELDS, _FACE_OPTIONAL_FIELDS, "FACE"),
    )
    macro = base.GetFaceMacro(face)
    if macro is None:
        raise RuntimeError(f"FACE {entity_id} has no confirmed macro")
    macro_reference = catalog._entity_reference(base, deck, macro, "FACE")
    macro_faces = _references(base, deck, base.GetFacesOfMacros([macro]), "FACE", "GetFacesOfMacros")
    macro_ids = sorted(item["id"] for item in macro_faces)
    if entity_id not in macro_ids or macro_reference["id"] not in macro_ids:
        raise RuntimeError("FACE and macro identity were not confirmed in the macro member list")
    shells = mesh.GetMacroShells([macro])
    # ANSA 2026.1 returns integer 0, rather than [], for an unmeshed macro.
    if type(shells) is int and shells == 0:
        shells = []
    shell_ids = sorted(item["id"] for item in _references(base, deck, shells, "SHELL", "GetMacroShells"))
    return face, {
        "entity": reference,
        "fields": fields,
        "macro": macro_reference,
        "macro_face_ids": macro_ids,
        "shell_count": len(shell_ids),
        "shell_ids": shell_ids[:_MAX_SHELL_IDS],
        "shell_ids_truncated": len(shell_ids) > _MAX_SHELL_IDS,
    }, shell_ids


def _mesh_faces(base, mesh, deck, params):
    face_ids = _ids(params["face_ids"], "face_ids", 100)
    rows = []
    for entity_id in face_ids:
        _face, row, shell_ids = _face_snapshot(base, mesh, deck, entity_id)
        if not row["shell_ids_truncated"]:
            row["condition"] = {
                "id": entity_id,
                "expected_fields": row["fields"],
                "expected_macro_id": row["macro"]["id"],
                "expected_shell_ids": shell_ids,
            }
        rows.append(row)
    return {"deck": deck, "read_only": True, "faces": rows}


def _settings(base):
    value = base.BCSettingsGetValues(_SETTINGS)
    if type(value) is not dict or any(type(value.get(key)) is not str for key in _SETTINGS):
        raise RuntimeError("BCSettingsGetValues did not confirm all mesh parameters")
    return {key: value[key] for key in _SETTINGS}


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def _quad_shell(base, deck, shell_id):
    shell, reference = catalog._get_entity(base, deck, "SHELL", shell_id)
    fields = _available_fields(
        shell, deck, _SHELL_REQUIRED_FIELDS,
        (*_SHELL_OPTIONAL_FIELDS, *_SHELL_HIGH_ORDER_FIELDS), "SHELL"
    )
    values = catalog._read_card(base, deck, shell, reference, fields)
    shape = values["type"]
    if type(shape) is not str or shape.casefold() != "quad":
        raise RuntimeError(
            f"SHELL {shell_id} is not a four-node QUAD element (type={shape!r})"
        )
    if "EID" in values and _field_id(values["EID"], f"SHELL {shell_id} EID") != shell_id:
        raise RuntimeError(f"SHELL {shell_id} EID does not match its entity ID")
    for name in _SHELL_HIGH_ORDER_FIELDS:
        if name in values and not _empty_field(values[name]):
            raise RuntimeError(
                f"SHELL {shell_id} has non-empty higher-order node field {name}"
            )
    node_ids = [_field_id(values[name], f"SHELL {shell_id} {name}") for name in ("G1", "G2", "G3", "G4")]
    if len(set(node_ids)) != 4:
        raise RuntimeError(f"SHELL {shell_id} QUAD connectivity does not contain four distinct nodes")
    for node_id in node_ids:
        if base.GetEntity(deck, "NODE", node_id) is None:
            raise RuntimeError(f"SHELL {shell_id} references missing node {node_id}")
    return {
        "id": shell_id,
        "type": shape,
        "node_ids": node_ids,
        "property_id": _field_id(values["PID"], f"SHELL {shell_id} PID"),
    }


def _mesh_surface(state, base, mesh, constants, deck, params):
    length = params["target_length"]
    if type(length) not in (int, float) or not math.isfinite(length) or length <= 0:
        raise ValueError("target_length must be finite and positive")
    element_type = params.get("element_type", "tria")
    if element_type not in _ELEMENT_TYPES:
        raise ValueError("element_type must be 'tria' or 'quad'")
    if element_type == "quad" and deck != constants.ABAQUS:
        raise ValueError("element_type 'quad' is supported only for the ABAQUS deck")
    conditions = params["faces"]
    if type(conditions) is not list or not 1 <= len(conditions) <= 100:
        raise ValueError("faces must contain 1 to 100 preconditions")
    selected_ids = _ids([item.get("id") if type(item) is dict else None for item in conditions], "faces.id", 100)
    selected = set(selected_ids)
    faces = []
    before_faces = []
    for item in conditions:
        item = _params(item, ("id", "expected_fields", "expected_macro_id", "expected_shell_ids"))
        catalog._positive_int(item["expected_macro_id"], "expected_macro_id")
        expected_shells = _ids(item["expected_shell_ids"], "expected_shell_ids", _MAX_SHELL_IDS, empty=True)
        catalog._validate_entity_values(base, deck, "FACE", item["id"], item["expected_fields"])
        face, row, shell_ids = _face_snapshot(base, mesh, deck, item["id"])
        # Always compare the mandatory snapshot fields, even for hand-written requests.
        for key, value in row["fields"].items():
            if key not in item["expected_fields"] or not catalog._strict_equal(value, item["expected_fields"][key]):
                raise RuntimeError(f"FACE meshing requires current expected_fields for {key}")
        if row["macro"]["id"] != item["expected_macro_id"] or set(shell_ids) != set(expected_shells):
            raise RuntimeError("FACE macro or attached mesh changed since it was read")
        if not set(row["macro_face_ids"]).issubset(selected):
            raise ValueError("selection must include every FACE of each selected macro")
        if shell_ids:
            raise ValueError("surface meshing only accepts unmeshed macros; existing mesh must be inspected first")
        if row["fields"]["Frozen"] != "NO":
            raise ValueError("surface meshing only accepts unfrozen FACE entities")
        faces.append(face)
        before_faces.append(row)

    macro_pids = {}
    if element_type == "quad":
        for row in before_faces:
            macro_pids.setdefault(row["macro"]["id"], set()).add(
                _field_id(row["fields"]["PID"], "FACE PID")
            )
    cons_list = perimeter_scope = face_derived_before = face_bounds = None
    if element_type == "quad":
        cons_list, perimeter_scope = _cons_scope(base, deck, faces, selected)
        face_derived_before = {
            str(entity_id): _face_derived(base, deck, face, row["entity"])
            for entity_id, face, row in zip(selected_ids, faces, before_faces)
        }
        face_bounds = _bounds(base, faces, "selected FACE")
    before_ids = _all_shell_ids(base, deck)
    before_settings = _settings(base)
    snapshot = Path(state.session_dir) / ("mesh-parameters-" + uuid.uuid4().hex + ".ansa_mpar")
    saved = mesh.SaveMeshParams(str(snapshot))
    if type(saved) is not int or saved != 1 or not snapshot.is_file() or snapshot.stat().st_size == 0:
        raise RuntimeError("SaveMeshParams did not produce a non-empty parameter backup; mesh was not started")

    result = {"ok": False, "deck": deck, "element_type": element_type, "target_length": float(length),
              "faces_before": before_faces, "shell_count_before": len(before_ids),
              "parameter_backup": str(snapshot), "settings_before": before_settings,
              "mesh_started": False, "parameters_restored": False}
    if element_type == "quad":
        result.update({"perimeter_scope": perimeter_scope,
                       "face_derived_before": face_derived_before,
                       "face_bounds": face_bounds})
    errors = []
    try:
        status = base.BCSettingsSetValues({
            "mesh_change_option": "mesh",
            "element_type": element_type,
            "element_order": "1st Order",
        })
        if type(status) is not int or status != 0:
            raise RuntimeError(f"BCSettingsSetValues returned {status!r}")
        status = mesh.SetMeshParamTargetLength("absolute", float(length))
        if type(status) is not int or status != 1:
            raise RuntimeError(f"SetMeshParamTargetLength returned {status!r}")
        settings = _settings(base)
        result["settings_used"] = settings
        if (settings["mesh_change_option"] != "mesh" or settings["element_type"].casefold() != element_type
                or settings["element_order"] != "1st Order" or float(settings["target_element_length"]) != length):
            raise RuntimeError("temporary mesh parameters were not confirmed by readback")
        if element_type == "quad":
            element_length = format(float(length), ".15g")
            apply_length = getattr(mesh, "ApplyNewLengthToMacros", None)
            if not callable(apply_length):
                raise RuntimeError("ansa.mesh.ApplyNewLengthToMacros is unavailable")
            status = apply_length(element_length, cons_list, False)
            applied = {
                str(entity_id): _face_derived(base, deck, face, row["entity"])
                for entity_id, face, row in zip(selected_ids, faces, before_faces)
            }
            for entity_id, row in applied.items():
                if not _close(row["element_length"], float(length), length):
                    raise RuntimeError(
                        f"FACE {entity_id} Elem.Length was not applied as target_length"
                    )
            result["length_application"] = {
                "method": "mesh.ApplyNewLengthToMacros",
                "element_length": element_length,
                "cons_count": len(cons_list),
                "return_code_ignored": True,
            }
            if type(status) is int:
                result["length_application"]["return_code"] = status
            result["face_derived_after_apply"] = applied
        result["mesh_started"] = True
        status = mesh.Mesh(faces)
        result["return_code"] = status if type(status) is int else None
        if element_type == "quad":
            try:
                result["face_derived_after_mesh"] = {
                    str(entity_id): _face_derived(base, deck, face, row["entity"])
                    for entity_id, face, row in zip(selected_ids, faces, before_faces)
                }
            except Exception as exc:
                errors.append("FACE derived readback: " + _error(exc))
        if type(status) is not int or status != 1:
            raise RuntimeError(f"Mesh returned {status!r}; partial model effects are possible")
    except Exception as exc:
        errors.append(_error(exc))
    finally:
        restored = mode_status = None
        try:
            restored = mesh.ReadMeshParams(str(snapshot))
        except Exception as exc:
            errors.append("ReadMeshParams: " + _error(exc))
        # These are independent restoration attempts: file failure must not leave
        # the operator's mesh_change_option at the temporary 'mesh' setting.
        try:
            mode_status = base.BCSettingsSetValues({"mesh_change_option": before_settings["mesh_change_option"]})
        except Exception as exc:
            errors.append("mesh_change_option restoration: " + _error(exc))
        try:
            result["settings_after"] = _settings(base)
            result["parameters_restored"] = (type(restored) is int and restored == 1
                                              and type(mode_status) is int and mode_status == 0
                                              and result["settings_after"] == before_settings)
            if not result["parameters_restored"]:
                raise RuntimeError("mesh parameter restoration was not confirmed; inspect parameter_backup before any new mesh")
        except Exception as exc:
            errors.append(_error(exc))

    # Read back even if the native API failed, retaining evidence instead of replaying.
    try:
        after_ids = _all_shell_ids(base, deck)
        created_ids = sorted(after_ids - before_ids)
        removed_ids = sorted(before_ids - after_ids)
        result.update({"shell_count_after": len(after_ids),
                       "created_shell_count": len(created_ids), "created_shell_ids": created_ids[:_MAX_SHELL_IDS],
                       "created_shell_ids_truncated": len(created_ids) > _MAX_SHELL_IDS,
                       "removed_shell_count": len(removed_ids), "removed_shell_ids": removed_ids[:_MAX_SHELL_IDS]})
        rows = []
        post_shell_ids = []
        result["faces_after"] = rows
        attached_ids = set()
        for entity_id, prior in zip(selected_ids, before_faces):
            try:
                _face, row, shell_ids = _face_snapshot(base, mesh, deck, entity_id)
                rows.append(row)
                post_shell_ids.append(shell_ids)
                attached_ids.update(shell_ids)
                if row["macro"] != prior["macro"] or row["macro_face_ids"] != prior["macro_face_ids"] or row["fields"] != prior["fields"]:
                    raise RuntimeError("FACE fields or macro membership changed during mesh generation")
            except Exception as exc:
                result.setdefault("face_readback_errors", []).append({"id": entity_id, "error": _error(exc)})
        if result.get("face_readback_errors") or removed_ids or attached_ids != set(created_ids) or any(row["shell_count"] == 0 for row in rows):
            raise RuntimeError("mesh readback did not confirm positive mesh confined to the selected macros and preservation of existing SHELL IDs")
        if element_type == "quad":
            derived_after_mesh = result.get("face_derived_after_mesh")
            if type(derived_after_mesh) is not dict:
                raise RuntimeError("FACE derived counts were not confirmed after meshing")
            if any(row["num_trias"] != 0 for row in derived_after_mesh.values()):
                raise RuntimeError("FACE derived counts report triangles after quad meshing")
            if sum(row["num_quads"] for row in derived_after_mesh.values()) != len(created_ids):
                raise RuntimeError("FACE derived Num.Quads does not match the created SHELL count")
            if any(not _close(row["element_length"], float(length), length)
                   for row in derived_after_mesh.values()):
                raise RuntimeError("FACE Elem.Length changed before parameter restoration")
            result["perimeter_target_verified"] = True
            quad_shells = {}
            positions = {}
            all_edges = []
            for prior, shell_ids in zip(before_faces, post_shell_ids):
                allowed_pids = macro_pids[prior["macro"]["id"]]
                for shell_id in shell_ids:
                    if shell_id in quad_shells:
                        continue
                    verified = _quad_shell(base, deck, shell_id)
                    if verified["property_id"] not in allowed_pids:
                        raise RuntimeError(
                            f"SHELL {shell_id} property {verified['property_id']} does not match its FACE property"
                        )
                    coordinates = []
                    for node_id in verified["node_ids"]:
                        if node_id not in positions:
                            positions[node_id] = _node_position(base, deck, node_id)
                        coordinates.append(positions[node_id])
                    verified["edge_lengths"] = [
                        _distance(coordinates[index], coordinates[(index + 1) % 4])
                        for index in range(4)
                    ]
                    all_edges.extend(verified["edge_lengths"])
                    quad_shells[shell_id] = verified
            if len(quad_shells) != len(set(created_ids)):
                raise RuntimeError("QUAD readback did not cover every created SHELL exactly once")
            result["quad_shell_count"] = len(quad_shells)
            result["quad_shells"] = sorted(quad_shells.values(), key=lambda item: item["id"])[:_MAX_SHELL_IDS]
            result["quad_shells_truncated"] = len(quad_shells) > _MAX_SHELL_IDS
            upper_limit = float(length) * _EDGE_LENGTH_UPPER_FACTOR
            edge_tolerance = max(_EDGE_ABS_TOL, _EDGE_REL_TOL * upper_limit)
            result["edge_length_upper_factor"] = _EDGE_LENGTH_UPPER_FACTOR
            result["edge_length_upper_limit"] = upper_limit
            result["target_length_interpretation"] = (
                "target_length is applied to the explicitly scoped CONS; mesh edges "
                f"need not equal it and are accepted when positive and <= {_EDGE_LENGTH_UPPER_FACTOR}x target"
            )
            if (not all_edges
                    or any(not math.isfinite(edge) or edge <= 0
                           or edge > upper_limit + edge_tolerance for edge in all_edges)):
                raise RuntimeError(
                    "QUAD edge lengths must be positive and no greater than "
                    f"{_EDGE_LENGTH_UPPER_FACTOR}x target_length"
                )
            result["edge_length_min"] = min(all_edges)
            result["edge_length_max"] = max(all_edges)
            result["edge_length_bound_verified"] = True
            result["target_length_verified"] = True
            mesh_bounds = _point_bounds(list(positions.values()))
            span = max(face_bounds[index + 3] - face_bounds[index] for index in range(3))
            bounds_tolerance = max(_EDGE_ABS_TOL, _EDGE_REL_TOL * abs(span))
            if any(mesh_bounds[index] < face_bounds[index] - bounds_tolerance
                   or mesh_bounds[index + 3] > face_bounds[index + 3] + bounds_tolerance
                   for index in range(3)):
                raise RuntimeError("mesh nodes extend outside the selected FACE bounds")
            result["mesh_bounds"] = mesh_bounds
            result["actual_size"] = [mesh_bounds[index + 3] - mesh_bounds[index] for index in range(3)]
            result["bounds_scope"] = "mesh node bounds contained within selected FACE bounds"
            result["bounds_verified"] = True
        result["scope_verified"] = True
    except Exception as exc:
        errors.append(_error(exc))
    result["ok"] = not errors
    if errors:
        result["error"] = "; ".join(errors)
        result["possible_effects"] = True
    return result


def _mesh_delete(base, deck, params):
    conditions = params["entities"]
    if type(conditions) is not list or not 1 <= len(conditions) <= 200:
        raise ValueError("entities must contain 1 to 200 SHELL preconditions")
    converted = []
    for item in conditions:
        item = _params(item, ("id", "expected_fields"))
        converted.append({"entity_id": item["id"], "expected_fields": item["expected_fields"]})
    requested = set(_ids([item["entity_id"] for item in converted], "entities.id", 200))
    # Complete the batch's preflight before entering the possible-effects block.
    for item in converted:
        catalog._validate_entity_values(base, deck, "SHELL", item["entity_id"], item["expected_fields"])
    before_ids = _all_shell_ids(base, deck)
    result = {"deck": deck, "shell_count_before": len(before_ids), "requested_shell_ids": sorted(requested),
              "scope_verified": False, "node_cleanup": False, "geometry_cleanup": False}
    errors = []
    try:
        result.update(catalog._entity_delete(base, deck, {"entity_type": "SHELL", "entities": converted}))
    except Exception as exc:
        errors.append(_error(exc))
    try:
        after_ids = _all_shell_ids(base, deck)
        deleted = before_ids - after_ids
        outside = sorted(deleted - requested)
        added = sorted(after_ids - before_ids)
        remaining = sorted(requested & after_ids)
        result.update({"shell_count_after": len(after_ids), "deleted_shell_ids": sorted(deleted & requested),
                       "remaining_requested_shell_ids": remaining, "outside_removed_shell_count": len(outside),
                       "outside_removed_shell_ids": outside[:_MAX_SHELL_IDS], "unexpected_added_shell_count": len(added),
                       "unexpected_added_shell_ids": added[:_MAX_SHELL_IDS]})
        if deleted != requested or added:
            raise RuntimeError("SHELL deletion was partial or changed entities outside the requested IDs")
        result["scope_verified"] = True
    except Exception as exc:
        errors.append(_error(exc))
    if errors:
        result.update({"ok": False, "error": "; ".join(errors), "possible_effects": True})
    return result


def execute(state, tool, params):
    state.assert_main()
    from ansa import base, constants, mesh

    required = {"mesh_faces": ("face_ids",), "mesh_surface": ("faces", "target_length"), "mesh_delete": ("entities",)}
    if tool not in required:
        raise ValueError(f"unsupported meshing operation: {tool!r}")
    params = _params(params, required[tool], ("deck", "element_type") if tool == "mesh_surface" else ("deck",))
    deck = catalog._deck(base, params)
    if tool == "mesh_faces":
        return _mesh_faces(base, mesh, deck, params)
    if tool == "mesh_surface":
        return _mesh_surface(state, base, mesh, constants, deck, params)
    return _mesh_delete(base, deck, params)
