"""Guarded deletion of isolated, unmeshed NASTRAN FACE entities."""

from . import entity_catalog as catalog
from .geometry import _bounds, _finite_number, _unit_normal
from .inspection import _deck, _id_list, _params, _require_api
from .meshing import _face_snapshot


_MAX_BATCH = 100
_MAX_SCAN = 10000
_MAX_CONS = 1000
_FACE_FIELDS = frozenset({"ID", "PID", "Thickness", "Frozen"})
_GEOMETRY_FIELDS = frozenset({"area", "center", "bounds", "normal"})
_PROTECTED_TYPES = ("GRID", "SHELL", "CURVE", "__PROPERTIES__", "__MATERIALS__")


def _supported(constants, deck):
    if deck != getattr(constants, "NASTRAN", None):
        raise ValueError("controlled FACE deletion is supported only in the NASTRAN deck")


def _collection(base, deck, entity_type, containers=None, maximum=_MAX_SCAN):
    entities = _require_api(base, "CollectEntities", "ansa.base.CollectEntities")(
        deck, containers, entity_type)
    if type(entities) not in (list, tuple) or len(entities) > maximum:
        raise RuntimeError(f"{entity_type} collection is invalid or exceeds {maximum} entities")
    references = [catalog._entity_reference(base, deck, entity,
                  None if entity_type.startswith("__") else entity_type) for entity in entities]
    keys = [(ref["type"], ref["id"]) for ref in references]
    if len(set(keys)) != len(keys):
        raise RuntimeError(f"{entity_type} collection contains duplicate identities")
    return list(entities), keys


def _geometry(base, face):
    area = _finite_number(_require_api(base, "GetFaceArea", "ansa.base.GetFaceArea")(face), "FACE area")
    if area <= 0:
        raise RuntimeError("controlled FACE deletion requires a positive area")
    raw = _require_api(base, "Cog", "ansa.base.Cog")(face)
    if type(raw) not in (list, tuple) or len(raw) != 3:
        raise RuntimeError("FACE center must contain three finite coordinates")
    return {"area": area, "center": [_finite_number(value, "FACE center") for value in raw],
            "bounds": _bounds(base, [face]), "normal": _unit_normal(base, face)}


def _snapshot(base, mesh, deck, entity_id):
    face, row, shell_ids = _face_snapshot(base, mesh, deck, entity_id)
    if row["fields"]["Frozen"] != "NO":
        raise ValueError("controlled FACE deletion requires an unfrozen FACE")
    if row["macro_face_ids"] != [entity_id] or row["macro"]["id"] != entity_id:
        raise ValueError("controlled FACE deletion requires a single-FACE macro")
    if shell_ids:
        raise ValueError("controlled FACE deletion requires an unmeshed FACE")
    cons, keys = _collection(base, deck, "CONS", face, _MAX_CONS)
    cons_ids = sorted(key[1] for key in keys)
    if not cons_ids:
        raise RuntimeError("FACE boundary CONS could not be confirmed")
    adjacent = _require_api(base, "GetFacesOfCons", "ansa.base.GetFacesOfCons")(cons)
    if type(adjacent) not in (list, tuple):
        raise RuntimeError("GetFacesOfCons returned an invalid FACE collection")
    neighbour_ids = {catalog._entity_reference(base, deck, entity, "FACE")["id"] for entity in adjacent}
    if neighbour_ids != {entity_id}:
        raise ValueError("controlled FACE deletion requires boundary CONS without adjacent FACEs")
    return face, {"id": entity_id, "expected_fields": row["fields"],
                  "expected_geometry": _geometry(base, face), "expected_cons_ids": cons_ids}


def prepare_preview(base, mesh, constants, deck, face_ids):
    """Return fresh conditions for eligible candidates; never delete or select."""
    _supported(constants, deck)
    if len(face_ids) > _MAX_BATCH:
        raise ValueError(f"deletion preconditions may cover at most {_MAX_BATCH} FACEs")
    conditions, rejected = [], []
    for entity_id in face_ids:
        try:
            _face, condition = _snapshot(base, mesh, deck, entity_id)
            conditions.append(condition)
        except Exception as exc:
            rejected.append({"id": entity_id, "error": f"{type(exc).__name__}: {exc}"})
    return {"delete_ready": bool(conditions) and not rejected,
            "delete_conditions": conditions, "delete_rejections": rejected}


def _validate_geometry(value):
    value = _params(value, _GEOMETRY_FIELDS)
    if _finite_number(value["area"], "expected FACE area") <= 0:
        raise ValueError("expected FACE area must be positive")
    for key, size in (("center", 3), ("bounds", 6), ("normal", 3)):
        vector = value[key]
        if type(vector) is not list or len(vector) != size:
            raise ValueError(f"expected {key} must contain {size} finite numbers")
        for item in vector:
            _finite_number(item, "expected " + key)
    return value


def _scope(base, deck, excluded_faces):
    faces, keys = _collection(base, deck, "FACE")
    outside = {}
    for face, (_, entity_id) in zip(faces, keys):
        if entity_id not in excluded_faces:
            reference = catalog._entity_reference(base, deck, face, "FACE", entity_id)
            outside[entity_id] = {"fields": catalog._read_card(base, deck, face, reference, tuple(sorted(_FACE_FIELDS))),
                                  "geometry": _geometry(base, face)}
    _cons, cons_keys = _collection(base, deck, "CONS")
    protected = {kind: set(_collection(base, deck, kind)[1]) for kind in _PROTECTED_TYPES}
    return {"face_ids": {key[1] for key in keys}, "outside": outside,
            "cons_ids": {key[1] for key in cons_keys}, "protected": protected}


def _readback(base, deck, requested, boundary_ids, before, result):
    """Persist each independent observation before attempting later API reads."""
    errors = []
    try:
        faces, keys = _collection(base, deck, "FACE")
        after_ids = {key[1] for key in keys}
        removed, added = before["face_ids"] - after_ids, after_ids - before["face_ids"]
        result.update({"face_count_after": len(after_ids), "deleted_face_ids": sorted(removed & requested),
                       "remaining_requested_face_ids": sorted(requested & after_ids),
                       "outside_removed_face_count": len(removed - requested),
                       "outside_removed_face_ids": sorted(removed - requested)[:100],
                       "unexpected_added_face_count": len(added), "unexpected_added_face_ids": sorted(added)[:100]})
        if removed != requested or added:
            errors.append("FACE deletion was partial or changed FACE identities outside the requested IDs")
        changed, unconfirmed, details = [], [], []
        for face, (_, entity_id) in zip(faces, keys):
            if entity_id in requested or entity_id not in before["outside"]:
                continue
            try:
                reference = catalog._entity_reference(base, deck, face, "FACE", entity_id)
                actual = {"fields": catalog._read_card(base, deck, face, reference, tuple(sorted(_FACE_FIELDS))),
                          "geometry": _geometry(base, face)}
                if not catalog._strict_equal(actual, before["outside"][entity_id]):
                    changed.append(entity_id)
            except Exception as exc:
                unconfirmed.append(entity_id)
                if len(details) < 8:
                    details.append(f"FACE {entity_id}: {type(exc).__name__}: {exc}")
        result.update({"changed_outside_face_count": len(changed), "changed_outside_face_ids": sorted(changed)[:100],
                       "unconfirmed_outside_face_count": len(unconfirmed),
                       "unconfirmed_outside_face_ids": sorted(unconfirmed)[:100]})
        if changed:
            errors.append("geometry or card fields changed on outside FACEs")
        if unconfirmed:
            errors.append("outside FACE readback incomplete: " + "; ".join(details))
    except Exception as exc:
        errors.append(f"FACE identity readback: {type(exc).__name__}: {exc}")
    try:
        _cons, keys = _collection(base, deck, "CONS")
        after_cons = {key[1] for key in keys}
        removed_cons, added_cons = before["cons_ids"] - after_cons, after_cons - before["cons_ids"]
        result.update({"removed_boundary_cons_count": len(removed_cons & boundary_ids),
                       "outside_removed_cons_count": len(removed_cons - boundary_ids),
                       "unexpected_added_cons_count": len(added_cons)})
        if removed_cons - boundary_ids or added_cons:
            errors.append("CONS identities changed outside the requested FACE boundary scope")
    except Exception as exc:
        errors.append(f"CONS readback: {type(exc).__name__}: {exc}")
    changed_protected, unconfirmed_types = [], []
    for kind in _PROTECTED_TYPES:
        try:
            after_keys = set(_collection(base, deck, kind)[1])
            if before["protected"][kind] != after_keys:
                changed_protected.append(kind)
        except Exception as exc:
            unconfirmed_types.append(kind)
            errors.append(f"{kind} readback: {type(exc).__name__}: {exc}")
    result.update({"changed_protected_types": changed_protected, "unconfirmed_protected_types": unconfirmed_types})
    if changed_protected:
        errors.append("protected node, mesh, curve, property or material identities changed")
    result["scope_verified"] = not errors
    return errors


def _delete(base, mesh, constants, deck, params):
    _supported(constants, deck)
    items = params["faces"]
    if type(items) is not list or not 1 <= len(items) <= _MAX_BATCH:
        raise ValueError(f"faces must contain 1 to {_MAX_BATCH} complete preconditions")
    ids = _id_list([item.get("id") if type(item) is dict else None for item in items], "faces.id", _MAX_BATCH)
    requested = set(ids)
    validated, boundary_ids = [], set()
    # Complete all validation and scope readback before the first possible write.
    for item in items:
        item = _params(item, ("id", "expected_fields", "expected_geometry", "expected_cons_ids"))
        if type(item["expected_fields"]) is not dict or set(item["expected_fields"]) != _FACE_FIELDS:
            raise ValueError("expected_fields must contain exactly ID, PID, Thickness and Frozen")
        expected_geometry = _validate_geometry(item["expected_geometry"])
        expected_cons = sorted(_id_list(item["expected_cons_ids"], "expected_cons_ids", _MAX_CONS))
        catalog._validate_entity_values(base, deck, "FACE", item["id"], item["expected_fields"])
        face, actual = _snapshot(base, mesh, deck, item["id"])
        if not catalog._strict_equal(actual["expected_geometry"], expected_geometry):
            raise RuntimeError(f"FACE {item['id']} geometry changed since it was read")
        if actual["expected_cons_ids"] != expected_cons:
            raise RuntimeError(f"FACE {item['id']} boundary CONS changed since they were read")
        validated.append(face)
        boundary_ids.update(expected_cons)
    before = _scope(base, deck, requested)
    delete = _require_api(base, "DeleteEntity", "ansa.base.DeleteEntity")
    result = {"deck": deck, "requested_face_ids": sorted(requested), "scope_verified": False,
              "force": False, "compress": False, "face_count_before": len(before["face_ids"])}
    errors = []
    try:
        result["return_code"] = delete(validated, force=False, compress=False)
    except Exception as exc:
        errors.append(f"DeleteEntity: {type(exc).__name__}: {exc}")
    errors.extend(_readback(base, deck, requested, boundary_ids, before, result))
    if errors:
        result.update({"ok": False, "error": "; ".join(errors), "possible_effects": True})
    elif result.get("return_code") != 0:
        result["warnings"] = ["DeleteEntity returned a nonzero status; exact scope was confirmed by independent readback"]
    return result


def execute(state, tool, params):
    state.assert_main()
    from ansa import base, constants, mesh
    if tool != "geometry_delete":
        raise ValueError("unsupported geometry deletion operation")
    params = _params(params, ("deck", "faces"))
    return _delete(base, mesh, constants, _deck(base, params), params)
