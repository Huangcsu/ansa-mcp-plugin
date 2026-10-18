"""Read-only NASTRAN dependency checks on explicit RBE2 entities."""

import json

from . import inspection


_FACTORY = "base.checks.general.NastranDependency"
_CHECK_NAME = "Dependency Nastran"
_MAX_RIGIDS = 200
_MAX_DEPENDENT_GRIDS = 100
_MAX_GRID_IDS = 2000
_MAX_REPORT_DEPTH = 12
_MAX_REPORT_NODES = 200
_MAX_REPORT_REFERENCES = 1000
_MISSING = object()


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _component_mask(value, rigid_id):
    if type(value) is not int:
        raise RuntimeError(f"RBE2 {rigid_id} CM must be a strict integer")
    text = str(value)
    if (not text or any(char not in "123456" for char in text)
            or len(set(text)) != len(text) or text != "".join(sorted(text))):
        raise RuntimeError(f"RBE2 {rigid_id} CM must contain ascending unique digits 1-6")
    return [int(char) for char in text]


def _rbe2_connectivity(base, deck, rigid, rigid_id):
    card_fields = getattr(rigid, "card_fields", None)
    if not callable(card_fields):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: RBE2.card_fields is required to verify connectivity"
        )
    try:
        labels = card_fields(deck)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: RBE2.card_fields could not verify connectivity"
        ) from exc
    if (type(labels) not in (list, tuple) or len(labels) > 256
            or any(type(label) is not str or not label for label in labels)
            or len(set(labels)) != len(labels)):
        raise RuntimeError("RBE2.card_fields returned an invalid field-name list")

    fixed_fields = ("GN", "CM", "No.of.Nodes")
    if any(field not in labels for field in fixed_fields):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: NASTRAN RBE2 connectivity fields are missing"
        )
    values = base.GetEntityCardValues(deck, rigid, fixed_fields)
    if type(values) is not dict or any(field not in values for field in fixed_fields):
        raise RuntimeError(f"ANSA did not return complete RBE2 {rigid_id} connectivity")

    gn = _positive_int(values["GN"], f"RBE2 {rigid_id} GN")
    components = _component_mask(values["CM"], rigid_id)
    node_count = values["No.of.Nodes"]
    if (type(node_count) is not int or not 2 <= node_count <= _MAX_DEPENDENT_GRIDS + 1):
        raise RuntimeError(
            f"RBE2 {rigid_id} No.of.Nodes must describe 1-{_MAX_DEPENDENT_GRIDS} dependent GRID nodes"
        )

    gm_fields = tuple(f"GM{index}" for index in range(1, node_count))
    if any(field not in labels for field in gm_fields):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: RBE2 {rigid_id} is missing active GM card fields"
        )
    gm_values = base.GetEntityCardValues(deck, rigid, gm_fields)
    if type(gm_values) is not dict or any(field not in gm_values for field in gm_fields):
        raise RuntimeError(f"ANSA did not return complete RBE2 {rigid_id} dependent GRID IDs")
    gm_ids = [_positive_int(gm_values[field], f"RBE2 {rigid_id} {field}")
              for field in gm_fields]
    if gn in gm_ids or len(set(gm_ids)) != len(gm_ids):
        raise RuntimeError(f"RBE2 {rigid_id} GN and GM nodes must be distinct")

    return {
        "rigid_id": rigid_id,
        "gn": gn,
        "gm_ids": gm_ids,
        "cm": values["CM"],
        "cm_components": components,
        "node_count": node_count,
    }


def _grid_context(base, deck, rigids, rigid_references, connected_grid_ids):
    collect = inspection._require_api(
        base, "CollectEntities", "ansa.base.CollectEntities"
    )
    grids = collect(deck, rigids, "GRID")
    if type(grids) not in (list, tuple) or len(grids) > _MAX_GRID_IDS:
        raise RuntimeError("CollectEntities must return a complete bounded GRID list or tuple")

    grid_ids = set()
    for grid in grids:
        reference = inspection._entity_reference(base, deck, grid, "GRID")
        grid_id = reference["id"]
        if grid_id in grid_ids:
            raise RuntimeError("CollectEntities returned a duplicate GRID")
        usable = getattr(grid, "is_usable", None)
        if not callable(usable):
            raise inspection.CapabilityUnavailableError(
                "capability unavailable: GRID.is_usable is required to verify connectivity"
            )
        is_usable = usable()
        if type(is_usable) is not bool or not is_usable:
            raise RuntimeError(f"GRID {grid_id} is not confirmed usable")
        grid_ids.add(grid_id)

    if grid_ids != connected_grid_ids:
        raise RuntimeError(
            "CollectEntities GRID scope does not exactly match verified RBE2 GN/GM connectivity"
        )
    if len(connected_grid_ids) > _MAX_GRID_IDS:
        raise RuntimeError(f"RBE2 GRID context exceeds {_MAX_GRID_IDS} unique entities")

    allowed_keys = {
        (reference["type"], reference["id"]) for reference in rigid_references
    }
    allowed_keys.update(("GRID", grid_id) for grid_id in grid_ids)
    return allowed_keys


def _report_field(report, name):
    return getattr(report, name, _MISSING)


def _serialize_reports(base, deck, reports, allowed_keys):
    if type(reports) is not list or not reports:
        raise RuntimeError("ANSA Check.execute must return a non-empty list of CheckReport roots")

    counts = {"error": 0, "warning": 0, "ok": 0}
    affected_rigids = set()
    affected_grids = set()
    active = set()
    # Keep wrappers alive: native getters may create temporary child objects.
    visited = {}
    node_count = 0
    reference_count = 0

    def serialize(report, depth):
        nonlocal node_count, reference_count
        if depth > _MAX_REPORT_DEPTH:
            raise RuntimeError(f"ANSA CheckReport tree exceeds depth {_MAX_REPORT_DEPTH}")
        identity = id(report)
        if identity in active:
            raise RuntimeError("ANSA CheckReport tree contains a cycle")
        if identity in visited:
            raise RuntimeError("ANSA CheckReport tree reuses a report object")
        node_count += 1
        if node_count > _MAX_REPORT_NODES:
            raise RuntimeError(f"ANSA CheckReport tree exceeds {_MAX_REPORT_NODES} nodes")
        active.add(identity)
        visited[identity] = report
        try:
            report_type = _report_field(report, "type")
            description = _report_field(report, "description")
            status = _report_field(report, "status")
            has_fix = _report_field(report, "has_fix")
            is_fixed = _report_field(report, "is_fixed")
            raw_entities = _report_field(report, "entities")
            issues = _report_field(report, "issues")
            if type(report_type) is not str or not report_type:
                raise RuntimeError("ANSA CheckReport is missing a valid type")
            if type(description) is not str:
                raise RuntimeError("ANSA CheckReport is missing a valid description")
            if type(status) is not str or status not in {"ok", "error", "warning"}:
                raise RuntimeError(f"ANSA CheckReport has unknown status: {status!r}")
            if type(has_fix) is not bool or type(is_fixed) is not bool:
                raise RuntimeError("ANSA CheckReport has_fix and is_fixed must be booleans")
            if is_fixed:
                raise RuntimeError("ANSA CheckReport is_fixed is true; refusing a previously fixed result")
            if type(raw_entities) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport entities must be a list or tuple")
            if type(issues) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport issues must be a list or tuple")
            if reference_count + len(raw_entities) > _MAX_REPORT_REFERENCES:
                raise RuntimeError(
                    f"ANSA CheckReport tree exceeds {_MAX_REPORT_REFERENCES} entity references"
                )

            entities = []
            seen_keys = set()
            for entity in raw_entities:
                reference = inspection._entity_reference(base, deck, entity)
                key = (reference["type"], reference["id"])
                if key not in allowed_keys:
                    raise RuntimeError(
                        "ANSA CheckReport contains an entity outside the explicit RBE2/GN/GM scope"
                    )
                if key in seen_keys:
                    raise RuntimeError("ANSA CheckReport repeats an entity within one report")
                seen_keys.add(key)
                entities.append(reference)
            reference_count += len(entities)

            child_rows = []
            subtree_has_problem = status in {"error", "warning"}
            for issue in issues:
                child_row, child_has_problem = serialize(issue, depth + 1)
                child_rows.append(child_row)
                subtree_has_problem = subtree_has_problem or child_has_problem

            if not issues and status in counts:
                counts[status] += 1
            if subtree_has_problem:
                for reference in entities:
                    if reference["type"] == "RBE2":
                        affected_rigids.add(reference["id"])
                    else:
                        affected_grids.add(reference["id"])

            return ({
                "type": report_type,
                "description": description,
                "status": status,
                "has_fix": has_fix,
                "is_fixed": is_fixed,
                "entities": entities,
                "issues": child_rows,
            }, subtree_has_problem)
        finally:
            active.remove(identity)

    rows = [serialize(report, 1)[0] for report in reports]
    return rows, counts, sorted(affected_rigids), sorted(affected_grids), node_count, reference_count


def execute(state, tool, params):
    """Run Dependency Nastran on an explicit, fully verified RBE2 selection."""
    state.assert_main()
    if type(tool) is not str or tool != "rigid_dependencies":
        raise ValueError(f"unsupported ANSA rigid dependency operation: {tool!r}")
    params = inspection._params(params, ("deck", "rigid_ids"))
    deck_value = params["deck"]
    if type(deck_value) is not int or deck_value <= 0:
        raise ValueError("deck must be a positive integer")
    rigid_ids = inspection._id_list(params["rigid_ids"], "rigid_ids", maximum=_MAX_RIGIDS)

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    deck = inspection._deck(base, params)
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    if deck != nastran:
        raise ValueError("rigid_dependencies supports only the NASTRAN deck")

    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(base, "GetEntityCardValues", "ansa.base.GetEntityCardValues")
    rigids, rigid_references, connections = [], [], []
    for rigid_id in rigid_ids:
        rigid, reference = inspection._get_entity(base, deck, "RBE2", rigid_id)
        rigids.append(rigid)
        rigid_references.append(reference)
        connections.append(_rbe2_connectivity(base, deck, rigid, rigid_id))

    connected_grid_ids = set()
    for connection in connections:
        connected_grid_ids.add(connection["gn"])
        connected_grid_ids.update(connection["gm_ids"])
    if len(connected_grid_ids) > _MAX_GRID_IDS:
        raise ValueError(f"RBE2 selection may reference at most {_MAX_GRID_IDS} unique GRID IDs")
    allowed_keys = _grid_context(
        base, deck, rigids, rigid_references, connected_grid_ids
    )

    check_class = getattr(base, "Check", None)
    if not isinstance(check_class, type):
        raise inspection.CapabilityUnavailableError("capability unavailable: ansa.base.Check is missing")
    try:
        exec_selected = check_class.EXEC_ON_SELECTED
        report_none = check_class.REPORT_NONE
        keep_old = check_class.KEEP_OLD
    except AttributeError as exc:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: selected, silent, history-preserving ANSA checks are missing"
        ) from exc
    if any(type(value) is not int for value in (exec_selected, report_none, keep_old)):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: selected, silent, history-preserving ANSA check options are invalid"
        )

    factory = inspection._factory(base, "general", "NastranDependency", _FACTORY)
    check = factory()
    if not isinstance(check, check_class):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY} did not return an ansa.base.Check"
        )
    availability = getattr(check, "is_available_in_deck", None)
    get_name = getattr(check, "get_name", None)
    parameters = getattr(check, "parameters", None)
    if not callable(availability) or availability(deck) is not True:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY} does not confirm NASTRAN support"
        )
    if not callable(get_name) or get_name() != _CHECK_NAME:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY} check name is not {_CHECK_NAME!r}"
        )
    if not callable(parameters):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY}.parameters is missing"
        )
    check_parameters = parameters()
    if type(check_parameters) is not dict or check_parameters:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY}.parameters must be an empty dictionary"
        )
    try:
        getattr(check, "options")
    except AttributeError:
        pass
    else:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY} unexpectedly exposes an options attribute"
        )

    execute_check = getattr(check, "execute", None)
    if not callable(execute_check):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_FACTORY}.Check.execute is missing"
        )
    reports = execute_check(
        exec_mode=exec_selected,
        report=report_none,
        history=keep_old,
        entities=rigids,
    )
    rows, counts, affected_rigids, affected_grids, node_count, reference_count = _serialize_reports(
        base, deck, reports, allowed_keys
    )

    native_options = {
        "exec_mode": "EXEC_ON_SELECTED",
        "report": "REPORT_NONE",
        "history": "KEEP_OLD",
    }
    result = {
        "deck": deck,
        "scope": {"rigid_ids": rigid_ids, "grid_ids": sorted(connected_grid_ids)},
        "connections": [
            {
                "rigid_id": item["rigid_id"],
                "rigid": rigid_references[index],
                "independent_grid_id": item["gn"],
                "dependent_grid_ids": item["gm_ids"],
                "cm": item["cm"],
                "cm_components": item["cm_components"],
                "node_count": item["node_count"],
            }
            for index, item in enumerate(connections)
        ],
        "native_options": native_options,
        "native_check": {
            "name": _CHECK_NAME,
            "factory": _FACTORY,
            "available_in_deck": True,
            "parameters": check_parameters,
            "options_attribute": False,
        },
        "native_report_count": len(reports),
        "report_reference_count": reference_count,
        "reports": rows,
        "stats": {
            "error": counts["error"],
            "warning": counts["warning"],
            "ok": counts["ok"],
            "issue_count": counts["error"] + counts["warning"],
            "report_count": node_count,
        },
        "count": counts["error"] + counts["warning"],
        "count_basis": "error_warning_leaf_reports_not_entity_count",
        "affected_rigid_ids": affected_rigids,
        "affected_grid_ids": affected_grids,
        "scope_note": "zero leaf issues applies only to the explicitly selected RBE2 IDs",
        "read_only": True,
        "auto_fix": False,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"rigid dependency result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
