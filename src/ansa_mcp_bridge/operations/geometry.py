"""Read-only FACE ranking and deletion preview operations."""

import math

from .inspection import (
    _deck,
    _entity_reference,
    _get_entity,
    _id_list,
    _params,
    _require_api,
)


_MAX_FACE_IDS = 10000
_MAX_LIMIT = 200
_QUERIES = {
    "largest_area",
    "smallest_area",
    "highest_z",
    "lowest_z",
    "nearest_center",
    "normal_pos_z",
    "normal_neg_z",
}
_NORMAL_QUERIES = {"normal_pos_z", "normal_neg_z"}


def _finite_number(value, label):
    if type(value) not in (int, float):
        raise RuntimeError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be a finite number")
    return number


def _point(value):
    if type(value) is not list or len(value) != 3:
        raise ValueError("point must contain exactly three finite numbers")
    try:
        return [_finite_number(item, "point coordinate") for item in value]
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _options(params, tool):
    optional = ("face_ids", "point", "limit")
    if tool == "geometry_preview":
        optional += ("include_delete_conditions",)
    params = _params(
        params,
        ("deck", "query"),
        optional,
    )
    query = params["query"]
    if type(query) is not str or query not in _QUERIES:
        raise ValueError("query must be one of: " + ", ".join(sorted(_QUERIES)))

    limit = params.get("limit", 5)
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_LIMIT}")

    include_delete_conditions = params.get("include_delete_conditions", False)
    if type(include_delete_conditions) is not bool:
        raise ValueError("include_delete_conditions must be a boolean")
    if include_delete_conditions and limit > 100:
        raise ValueError("limit must be at most 100 when include_delete_conditions is true")

    point = params.get("point")
    if query == "nearest_center":
        if point is None:
            raise ValueError("point is required for nearest_center")
        point = _point(point)
    elif point is not None:
        raise ValueError("point is only supported for nearest_center")

    face_ids = params.get("face_ids")
    if face_ids is None:
        scope = "all"
    else:
        face_ids = _id_list(face_ids, "face_ids", _MAX_FACE_IDS)
        scope = "explicit"
    return params, query, point, limit, face_ids, scope, include_delete_conditions


def _unit_normal(base, face):
    """Return a finite unit normal from the documented ANSA face-orientation API."""
    get_orientation = _require_api(
        base, "GetFaceOrientation", "ansa.base.GetFaceOrientation"
    )
    value = get_orientation(face)
    if type(value) is not list or len(value) != 3:
        raise RuntimeError("GetFaceOrientation returned an invalid three-component normal")
    try:
        components = [_finite_number(item, "GetFaceOrientation component") for item in value]
    except RuntimeError as exc:
        raise RuntimeError("GetFaceOrientation returned an invalid normal") from exc

    scale = max(abs(component) for component in components)
    if scale == 0:
        raise RuntimeError("GetFaceOrientation returned a zero normal")
    scaled = [component / scale for component in components]
    magnitude = math.hypot(*scaled)
    if not math.isfinite(magnitude) or magnitude == 0:
        raise RuntimeError("GetFaceOrientation returned an invalid normal magnitude")
    normal = [float(component / magnitude) for component in scaled]
    if not all(math.isfinite(component) for component in normal):
        raise RuntimeError("GetFaceOrientation returned an invalid normalized normal")
    return normal


def _source_faces(base, deck, face_ids, scope):
    if scope == "all":
        collected = base.CollectEntities(deck, None, "FACE")
        if type(collected) not in (list, tuple):
            raise RuntimeError("CollectEntities returned an invalid FACE collection")
        if len(collected) > _MAX_FACE_IDS:
            raise ValueError(
                f"global FACE selection exceeds the {_MAX_FACE_IDS} entity limit"
            )
        pairs = [(entity, _entity_reference(base, deck, entity, "FACE"))
                 for entity in collected]
        if len({reference["id"] for _, reference in pairs}) != len(pairs):
            raise RuntimeError("CollectEntities returned duplicate FACE IDs")
        pairs.sort(key=lambda pair: pair[1]["id"])
        return pairs

    pairs = []
    for entity_id in sorted(face_ids):
        entity, reference = _get_entity(base, deck, "FACE", entity_id)
        pairs.append((entity, reference))
    return pairs


def _face_rows(base, deck, query, point, pairs):
    area_fn = _require_api(base, "GetFaceArea", "ansa.base.GetFaceArea")
    cog_fn = _require_api(base, "Cog", "ansa.base.Cog")
    rows = []
    for entity, reference in pairs:
        area = _finite_number(area_fn(entity), "GetFaceArea result")
        if area < 0:
            raise RuntimeError(f"GetFaceArea failed for FACE {reference['id']} (negative area)")

        center_value = cog_fn(entity)
        if type(center_value) not in (list, tuple) or len(center_value) != 3:
            raise RuntimeError(f"Cog returned an invalid center for FACE {reference['id']}")
        center = [_finite_number(value, "Cog coordinate") for value in center_value]

        if query in {"largest_area", "smallest_area"}:
            score = area
        elif query in {"highest_z", "lowest_z"}:
            score = center[2]
        elif query in _NORMAL_QUERIES:
            try:
                normal = _unit_normal(base, entity)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"FACE {reference['id']} has an invalid orientation: {exc}"
                ) from exc
            score = normal[2]
        else:
            try:
                score = math.dist(center, point)
            except (OverflowError, ValueError) as exc:
                raise RuntimeError(
                    f"FACE {reference['id']} produced an invalid nearest-center score"
                ) from exc
        if not math.isfinite(score):
            raise RuntimeError(f"FACE {reference['id']} produced a non-finite ranking score")
        row = {
            "face": reference,
            "area": area,
            "center": center,
            "score": float(score),
            "_id": reference["id"],
            "_entity": entity,
        }
        if query in _NORMAL_QUERIES:
            row["normal"] = normal
        rows.append(row)

    descending = query in {"largest_area", "highest_z", "normal_pos_z"}
    rows.sort(key=lambda row: ((-row["score"]) if descending else row["score"], row["_id"]))
    return rows


def _bounds(base, entities):
    if not entities:
        return None
    raw = _require_api(base, "BoundBox", "ansa.base.BoundBox")(entities)
    if type(raw) not in (list, tuple) or len(raw) != 6:
        raise RuntimeError("BoundBox returned an invalid six-coordinate result")
    return [_finite_number(value, "BoundBox coordinate") for value in raw]


def _execute(base, deck, tool, query, point, limit, face_ids, scope,
             include_delete_conditions=False):
    pairs = _source_faces(base, deck, face_ids, scope)
    rows = _face_rows(base, deck, query, point, pairs)
    selected = rows[:limit]
    selected_ids = [row["face"]["id"] for row in selected]
    result = {
        "deck": deck,
        "query": query,
        "point": point,
        "source_count": len(pairs),
        "matched_count": len(pairs),
        "limit": limit,
        "read_only": True,
        "selection_changed": False,
        "candidates": [
            {key: row[key] for key in ("face", "area", "center", "score", "normal")
             if key in row}
            for row in selected
        ],
        "face_ids": selected_ids,
        "scope": scope,
    }
    if tool == "geometry_preview":
        try:
            total_area = math.fsum(row["area"] for row in selected)
        except OverflowError as exc:
            raise RuntimeError("selected FACE area total is not finite") from exc
        if not math.isfinite(total_area):
            raise RuntimeError("selected FACE area total is not finite")
        result.update({
            "preview_only": True,
            "would_delete_face_ids": selected_ids,
            "requires_confirmation": True,
            "total_area": total_area,
            "bounds": _bounds(base, [row["_entity"] for row in selected]),
        })
        if include_delete_conditions:
            from ansa import mesh, constants
            from . import geometry_delete

            result.update(geometry_delete.prepare_preview(
                base, mesh, constants, deck, result["face_ids"]
            ))
    return result


def execute(state, tool, params):
    """Rank FACE entities or return a deletion preview without changing ANSA."""
    state.assert_main()
    from ansa import base

    if type(tool) is not str or tool not in {"geometry_select", "geometry_preview"}:
        raise ValueError(f"unsupported geometry operation: {tool!r}")
    params, query, point, limit, face_ids, scope, include_delete_conditions = _options(
        params, tool
    )
    deck = _deck(base, params)
    return _execute(base, deck, tool, query, point, limit, face_ids, scope,
                    include_delete_conditions)
