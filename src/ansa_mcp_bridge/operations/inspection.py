"""Bounded geometry inspection and display operations for ANSA.

All ANSA calls happen on the executor's main thread. This module deliberately
uses only documented APIs and never invokes a check result's ``try_fix`` method.
"""

from collections.abc import Mapping
import math


_MAX_TYPE_PAGE = 50
_MAX_TYPE_INPUT = 256
_MAX_ENTITY_IDS = 200
_MAX_REPORTS = 50
_MAX_JSON_ITEMS = 100


class CapabilityUnavailableError(RuntimeError):
    """Raised when this ANSA installation has no verified API for an action."""


def _params(value, required, optional=()):
    if type(value) is not dict:
        raise TypeError("params must be an object")
    required = set(required)
    optional = set(optional)
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValueError(f"missing required parameters: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unsupported parameters: {sorted(unknown)}")
    return value


def _deck(base, params):
    deck = params.get("deck")
    if type(deck) is not int or deck < 0:
        raise ValueError("deck must be an explicit non-negative integer")
    current = base.CurrentDeck()
    if type(current) is not int or current != deck:
        raise ValueError("requested deck is not the current ANSA deck")
    return deck


def _entity_type(value, label="entity_type"):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty exact ANSA type name")
    return value


def _positive_id(value, label="entity_id"):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _id_list(value, label, maximum=_MAX_ENTITY_IDS, *, allow_empty=False):
    if type(value) is not list:
        raise ValueError(f"{label} must be a list of positive integers")
    if len(value) > maximum:
        raise ValueError(f"{label} may contain at most {maximum} items")
    if not value and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    ids = [_positive_id(item, label) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} must not contain duplicate IDs")
    return ids


def _require_api(module, name, path):
    function = getattr(module, name, None)
    if not callable(function):
        raise CapabilityUnavailableError(
            f"capability unavailable: documented API {path} is missing"
        )
    return function


def _is_entity(value, base):
    entity_class = getattr(base, "Entity", None)
    if isinstance(entity_class, type) and isinstance(value, entity_class):
        return True
    return (type(getattr(value, "_id", None)) is int
            and (callable(getattr(value, "ansa_type", None))
                 or type(getattr(value, "_type", None)) is str))


def _entity_reference(base, deck, entity, expected_type=None, expected_id=None):
    values = base.GetEntityCardValues(deck, entity, ("__id__", "__type__"))
    if type(values) is not dict or "__id__" not in values or "__type__" not in values:
        raise RuntimeError("ANSA did not return entity ID and type metadata")
    entity_id, entity_type = values["__id__"], values["__type__"]
    if type(entity_id) is not int or entity_id <= 0:
        raise RuntimeError("ANSA returned an invalid entity ID")
    if type(entity_type) is not str or not entity_type:
        raise RuntimeError("ANSA returned an invalid entity type")
    if expected_type is not None and entity_type != expected_type:
        raise RuntimeError("ANSA entity type does not match the requested type")
    if expected_id is not None and entity_id != expected_id:
        raise RuntimeError("ANSA entity ID does not match the requested ID")
    return {"deck": deck, "type": entity_type, "id": entity_id}


def _get_entity(base, deck, entity_type, entity_id):
    entity_id = _positive_id(entity_id)
    entity = base.GetEntity(deck, entity_type, entity_id)
    if entity is None:
        raise LookupError(f"{entity_type} {entity_id} does not exist in deck {deck}")
    reference = _entity_reference(base, deck, entity, entity_type, entity_id)
    return entity, reference


def _json_value(value, base, deck, depth=0):
    """Convert ANSA values to strict JSON, replacing every Entity by a reference."""
    if depth > 8:
        raise ValueError("ANSA result nesting exceeds the supported JSON depth")
    if _is_entity(value, base):
        return _entity_reference(base, deck, value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > _MAX_JSON_ITEMS:
            raise ValueError("ANSA result object exceeds the supported field limit")
        result = {}
        for key, item in items:
            if type(key) is not str:
                raise TypeError("ANSA result object has a non-string key")
            result[key] = _json_value(item, base, deck, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = list(value)
        converted = [_json_value(item, base, deck, depth + 1)
                     for item in items[:_MAX_JSON_ITEMS]]
        if len(items) > _MAX_JSON_ITEMS:
            return {"items": converted, "truncated": True, "total": len(items)}
        return converted
    raise TypeError(f"unsupported ANSA result value type: {type(value).__name__}")


def _available_types(base, deck):
    types_in_category = _require_api(base, "TypesInCategory", "ansa.base.TypesInCategory")
    values = types_in_category(deck, "__ALL_ENTITIES__")
    if not isinstance(values, (list, tuple)):
        raise RuntimeError("ANSA TypesInCategory returned an invalid result")
    result = []
    seen = set()
    for value in values:
        if type(value) is not str or not value:
            raise RuntimeError("ANSA TypesInCategory returned an invalid entity type")
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _geometry_inventory(base, deck, params):
    params = _params(params, ("deck",), ("entity_types", "offset", "limit"))
    all_types = _available_types(base, deck)
    requested_types = params.get("entity_types")
    if requested_types is None:
        types = all_types
    else:
        if type(requested_types) is not list or not requested_types:
            raise ValueError("entity_types must be a non-empty list")
        if len(requested_types) > _MAX_TYPE_INPUT:
            raise ValueError(f"entity_types may contain at most {_MAX_TYPE_INPUT} items")
        types = [_entity_type(item, "entity_types item") for item in requested_types]
        if len(set(types)) != len(types):
            raise ValueError("entity_types must not contain duplicates")
        unavailable = [item for item in types if item not in set(all_types)]
        if unavailable:
            raise ValueError(f"unsupported entity types for this deck: {unavailable}")

    offset = params.get("offset", 0)
    limit = params.get("limit", 20)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_TYPE_PAGE:
        raise ValueError(f"limit must be between 1 and {_MAX_TYPE_PAGE}")

    collect = _require_api(base, "CollectEntities", "ansa.base.CollectEntities")
    page = types[offset:offset + limit]
    counts = []
    for entity_type in page:
        entities = collect(deck, None, [entity_type], recursive=True)
        if entities is None or not isinstance(entities, (list, tuple)):
            raise RuntimeError(f"ANSA returned an invalid collection for {entity_type}")
        counts.append({"entity_type": entity_type, "count": len(entities)})
    return {"deck": deck, "offset": offset, "limit": limit,
            "total_types": len(types), "counts": counts,
            "has_more": offset + len(page) < len(types)}


def _orientation_components(value):
    if type(value) is not list or len(value) != 3:
        raise RuntimeError("GetFaceOrientation must return a list of three finite numbers")
    components = []
    for item in value:
        if type(item) not in (int, float):
            raise RuntimeError("GetFaceOrientation returned a non-numeric component")
        try:
            number = float(item)
        except (OverflowError, ValueError) as exc:
            raise RuntimeError("GetFaceOrientation returned a non-finite component") from exc
        if not math.isfinite(number):
            raise RuntimeError("GetFaceOrientation returned a non-finite component")
        components.append(number)
    if not any(components):
        raise RuntimeError("GetFaceOrientation returned a zero-length vector")
    return list(value), components


def _unit_normal(components):
    scale = max(abs(value) for value in components)
    if scale == 0:
        raise RuntimeError("GetFaceOrientation returned a zero-length vector")
    scaled = [value / scale for value in components]
    length = math.hypot(*scaled)
    if not math.isfinite(length) or length == 0:
        raise RuntimeError("GetFaceOrientation could not be normalized")
    normal = [value / length for value in scaled]
    if not all(math.isfinite(value) for value in normal):
        raise RuntimeError("GetFaceOrientation produced a non-finite unit normal")
    return normal


def _face_bounds(base, face):
    raw = _require_api(base, "BoundBox", "ansa.base.BoundBox")([face])
    if type(raw) not in (list, tuple) or len(raw) != 6:
        raise RuntimeError("BoundBox must return six finite coordinates")
    bounds = []
    for item in raw:
        if type(item) not in (int, float):
            raise RuntimeError("BoundBox returned a non-numeric coordinate")
        try:
            number = float(item)
        except (OverflowError, ValueError) as exc:
            raise RuntimeError("BoundBox returned a non-finite coordinate") from exc
        if not math.isfinite(number):
            raise RuntimeError("BoundBox returned a non-finite coordinate")
        bounds.append(number)
    if any(bounds[index] > bounds[index + 3] for index in range(3)):
        raise RuntimeError("BoundBox minimum coordinates exceed maximum coordinates")
    return bounds


def _face_properties(base, constants, deck, params):
    params = _params(params, ("deck", "face_ids"), ("include_geometry",))
    face_ids = _id_list(params["face_ids"], "face_ids", 100)
    include_geometry = params.get("include_geometry", False)
    if type(include_geometry) is not bool:
        raise ValueError("include_geometry must be a boolean")
    if include_geometry and deck != constants.NASTRAN:
        raise ValueError("include_geometry is supported only for the NASTRAN deck")

    area_fn = _require_api(base, "GetFaceArea", "ansa.base.GetFaceArea")
    cog_fn = _require_api(base, "Cog", "ansa.base.Cog")
    rows = []
    for face_id in face_ids:
        face = base.GetEntity(deck, "FACE", face_id)
        if face is None:
            row = {"face": {"deck": deck, "type": "FACE", "id": face_id},
                   "found": False}
            if include_geometry:
                row["complete"] = False
            rows.append(row)
            continue
        reference = _entity_reference(base, deck, face, "FACE", face_id)
        row = {"face": reference, "found": True}
        try:
            area = area_fn(face)
            if type(area) not in (int, float) or type(area) is bool or not math.isfinite(float(area)):
                raise RuntimeError("GetFaceArea returned a non-finite or non-numeric value")
            if area < 0:
                row["area"] = None
                row["area_error"] = "GetFaceArea reported an error (-1.0)"
            else:
                row["area"] = float(area)
        except Exception as exc:
            row["area"] = None
            row["area_error"] = f"{type(exc).__name__}: {exc}"
        try:
            center = cog_fn(face)
            if center is None:
                row["center"] = None
            elif isinstance(center, (list, tuple)) and len(center) == 3:
                coordinates = []
                for value in center:
                    if type(value) not in (int, float) or type(value) is bool:
                        raise RuntimeError("Cog returned a non-numeric coordinate")
                    number = float(value)
                    if not math.isfinite(number):
                        raise RuntimeError("Cog returned a non-finite coordinate")
                    coordinates.append(number)
                row["center"] = coordinates
            else:
                raise RuntimeError("Cog returned an invalid center")
        except Exception as exc:
            row["center"] = None
            row["center_error"] = f"{type(exc).__name__}: {exc}"
        if include_geometry:
            components = None
            try:
                raw_orientation = _require_api(
                    base, "GetFaceOrientation", "ansa.base.GetFaceOrientation"
                )(face)
                row["orientation"], components = _orientation_components(raw_orientation)
            except Exception as exc:
                row["orientation"] = None
                row["orientation_error"] = f"{type(exc).__name__}: {exc}"

            if components is None:
                row["unit_normal"] = None
                row["unit_normal_error"] = "RuntimeError: orientation is unavailable"
            else:
                try:
                    row["unit_normal"] = _unit_normal(components)
                except Exception as exc:
                    row["unit_normal"] = None
                    row["unit_normal_error"] = f"{type(exc).__name__}: {exc}"

            try:
                row["bounds"] = _face_bounds(base, face)
            except Exception as exc:
                row["bounds"] = None
                row["bounds_error"] = f"{type(exc).__name__}: {exc}"

            row["complete"] = all(
                row.get(field) is not None and f"{field}_error" not in row
                for field in ("area", "center", "orientation", "unit_normal", "bounds")
            )
        rows.append(row)
    result = {"deck": deck, "faces": rows}
    if include_geometry:
        result.update({
            "read_only": True,
            "scope": "explicit",
            "include_geometry": True,
            "complete": all(row["found"] and row["complete"] for row in rows),
        })
    return result


def _mass_components(value):
    """Keep GPWG's three directional masses without inventing a scalar mass."""
    if type(value) not in (list, tuple) or len(value) != 3:
        raise RuntimeError("GPWGInfo mass must contain exactly three numeric components")
    components = []
    for component in value:
        if type(component) not in (int, float):
            raise RuntimeError("GPWGInfo mass components must be finite numbers, not booleans")
        try:
            number = float(component)
        except (OverflowError, ValueError) as exc:
            raise RuntimeError("GPWGInfo mass components must be finite numbers") from exc
        if not math.isfinite(number):
            raise RuntimeError("GPWGInfo mass components must be finite numbers")
        components.append(number)
    return components


def _entity_metrics(base, constants, deck, params):
    params = _params(params, ("deck", "action", "entity_type", "entity_id"))
    action = params["action"]
    if type(action) is not str or action not in {"mass", "area", "volume"}:
        raise ValueError("action must be one of: mass, area, volume")
    entity_type = _entity_type(params["entity_type"])
    entity_id = _positive_id(params["entity_id"])
    entity, reference = _get_entity(base, deck, entity_type, entity_id)

    if action == "area":
        if entity_type == "FACE":
            value = _require_api(base, "GetFaceArea", "ansa.base.GetFaceArea")(entity)
            api_name = "ansa.base.GetFaceArea"
            if type(value) not in (int, float) or type(value) is bool or not math.isfinite(float(value)) or value < 0:
                raise RuntimeError("GetFaceArea could not calculate this entity's area")
        elif entity_type == "SHELL":
            value = _require_api(base, "CalcShellArea", "ansa.base.CalcShellArea")(entity)
            api_name = "ansa.base.CalcShellArea"
        else:
            raise CapabilityUnavailableError(
                "capability unavailable: area metrics are verified only for FACE and SHELL"
            )
    elif action == "volume":
        if entity_type not in {"SOLID", "VOLUME"}:
            raise CapabilityUnavailableError(
                "capability unavailable: volume metrics are verified only for SOLID and VOLUME"
            )
        value = _require_api(base, "CalcSolidVolume", "ansa.base.CalcSolidVolume")(entity)
        api_name = "ansa.base.CalcSolidVolume"
    else:
        if deck == getattr(constants, "ABAQUS", None):
            from . import abaqus_mass
            result = abaqus_mass.mass_properties(base, constants, deck, {
                "deck": deck, "entities": [reference],
            })
            return {**result, "entity": reference, "action": action}
        supported_decks = {getattr(constants, "NASTRAN", object()),
                           getattr(constants, "OPTISTRUCT", object())}
        if deck not in supported_decks:
            raise CapabilityUnavailableError(
                "capability unavailable: GPWGInfo is documented for NASTRAN and OPTISTRUCT decks"
            )
        result = _require_api(base, "GPWGInfo", "ansa.base.GPWGInfo")("custom", entity)
        if result is None or not hasattr(result, "mass"):
            raise RuntimeError("GPWGInfo returned no mass result")
        return {"deck": deck, "entity": reference, "action": action,
                "api": "ansa.base.GPWGInfo", "value": _mass_components(result.mass)}

    if type(value) not in (int, float) or type(value) is bool or not math.isfinite(float(value)):
        raise RuntimeError(f"{api_name} returned a non-finite or non-numeric value")
    return {"deck": deck, "entity": reference, "action": action,
            "api": api_name, "value": float(value)}


def _selection(base, deck, entity_type, entity_ids):
    ids = _id_list(entity_ids, "entity_ids")
    entities, references = [], []
    for entity_id in ids:
        entity, reference = _get_entity(base, deck, entity_type, entity_id)
        entities.append(entity)
        references.append(reference)
    return entities, references


def _check_reports(base, deck, factory, entities):
    check_class = getattr(base, "Check", None)
    if check_class is None:
        raise CapabilityUnavailableError("capability unavailable: ansa.base.Check is missing")
    try:
        exec_selected = check_class.EXEC_ON_SELECTED
        report_none = check_class.REPORT_NONE
    except AttributeError as exc:
        raise CapabilityUnavailableError(
            "capability unavailable: selected, silent ANSA check execution is not documented here"
        ) from exc
    check = factory()
    # The Check API reports findings from execute(); fixes require a separate
    # CheckReport.try_fix() call. This module never calls that method.
    reports = check.execute(exec_mode=exec_selected, report=report_none, entities=entities)
    if reports is None:
        reports = []
    if not isinstance(reports, (list, tuple)):
        raise RuntimeError("ANSA Check.execute returned an invalid report list")
    rows = []
    fields = ("name", "title", "message", "errors", "warnings", "status")
    for report in reports[:_MAX_REPORTS]:
        row = {}
        for field in fields:
            if hasattr(report, field):
                row[field] = _json_value(getattr(report, field), base, deck)
        if not row:
            row["report_type"] = type(report).__name__
        rows.append(row)
    return rows, len(reports) > _MAX_REPORTS, len(reports)


def _factory(base, group_name, factory_name, path):
    group = getattr(getattr(base, "checks", None), group_name, None)
    factory = getattr(group, factory_name, None) if group is not None else None
    if not callable(factory):
        raise CapabilityUnavailableError(f"capability unavailable: documented check {path} is missing")
    return factory


def _mesh_quality(base, deck, params):
    params = _params(params, ("deck", "entity_type", "entity_ids"))
    entity_type = _entity_type(params["entity_type"])
    if entity_type not in {"SHELL", "FACE", "PSHELL"}:
        raise CapabilityUnavailableError(
            "capability unavailable: mesh quality is verified for SHELL, FACE, or PSHELL selections"
        )
    entities, references = _selection(base, deck, entity_type, params["entity_ids"])
    factory = _factory(base, "mesh", "MeshQuality", "ansa.base.checks.mesh.MeshQuality")
    reports, truncated, total_reports = _check_reports(base, deck, factory, entities)
    return {"deck": deck, "check": "ansa.base.checks.mesh.MeshQuality",
            "entities": references, "read_only": True, "auto_fix": False,
            "report_count": total_reports, "reports": reports,
            "reports_truncated": truncated}


def _entity_visibility(base, deck, params):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    action = params.get("action")
    if type(action) is not str:
        raise ValueError("action must be a string")
    if action in {"near", "neighb"} or "expected_visible" in params or params.get("entity_ids"):
        from . import visibility
        return visibility.execute_base(base, deck, params)
    params = _params(params, ("deck", "action", "entity_type"), ("entity_ids",))
    action = params["action"]
    if action not in {"show_only", "show_also", "hide"}:
        raise ValueError("action must be one of: show_only, show_also, hide, near, neighb")
    entity_type = _entity_type(params["entity_type"])
    ids = _id_list(params.get("entity_ids", []), "entity_ids", allow_empty=True)
    if ids:
        raise CapabilityUnavailableError(
            "capability unavailable: ANSA visibility API operates on entity types, not individual IDs"
        )
    if entity_type not in set(_available_types(base, deck)):
        raise ValueError(f"unsupported entity type for this deck: {entity_type}")
    visibility = _require_api(base, "SetEntityVisibilityValues",
                              "ansa.base.SetEntityVisibilityValues")
    if action == "show_only":
        fields = {"all": "off", entity_type: "on"}
    elif action == "show_also":
        fields = {entity_type: "on"}
    else:
        fields = {entity_type: "off"}
    return_code = visibility(deck, fields)
    applied = return_code == 1
    result = {"deck": deck, "action": action, "scope": "entity_type",
              "entity_type": entity_type, "changed_flags": fields,
              "return_code": return_code, "applied": applied}
    if not applied:
        result.update(ok=False, error="ANSA rejected the visibility change")
    return result


def execute(state, tool, params):
    """Run a bounded inspection or explicit display change on ANSA's main thread."""
    state.assert_main()
    from ansa import base, constants

    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if type(params) is not dict or "deck" not in params:
        raise ValueError("params must be an object with an explicit deck")
    deck = _deck(base, params)
    if tool == "geometry_inventory":
        return _geometry_inventory(base, deck, params)
    if tool == "face_properties":
        return _face_properties(base, constants, deck, params)
    if tool == "entity_metrics":
        return _entity_metrics(base, constants, deck, params)
    if tool == "mass_properties":
        from . import abaqus_mass
        return abaqus_mass.mass_properties(base, constants, deck, params)
    if tool == "surface_members":
        from . import abaqus_surfaces
        return abaqus_surfaces.surface_members(base, constants, deck, params)
    if tool == "mesh_quality":
        return _mesh_quality(base, deck, params)
    if tool == "geometry_check":
        from . import geometry_checks
        return geometry_checks.execute(state, tool, params)
    if tool == "entity_visibility":
        return _entity_visibility(base, deck, params)
    if tool == "visibility_snapshot":
        from . import visibility
        return visibility.snapshot_base(base, deck, params)
    raise ValueError(f"unsupported ANSA inspection operation: {tool!r}")


__all__ = ["CapabilityUnavailableError", "execute"]
