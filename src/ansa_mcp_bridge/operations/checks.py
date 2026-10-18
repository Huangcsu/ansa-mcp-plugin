"""Controlled, read-only ANSA checks scoped to explicit entities."""

import json

from . import inspection


_API = "ansa.base.checks.general.FreeNodes"
_MAX_REPORT_DEPTH = 8
_MAX_REPORT_NODES = 1000
_MAX_CONTEXT_ENTITIES = 10000
_MISSING = object()


def _option_values(consider_nodes_with_mass_free):
    return {
        "consider_nodes_with_mass_free": consider_nodes_with_mass_free,
        "do_not_report_free_nodes_attached_to_non_visible_entities": False,
        "do_not_report_master_nodes_of_rigid_bodies": True,
        "consider_auxiliary_nodes_as_free": True,
        "report_scalar_nodes": False,
        "consider_nodes_with_load_free": True,
    }


def _configure_check(check, options):
    for name, value in options.items():
        try:
            getattr(check, name)
        except AttributeError as exc:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} is missing"
            ) from exc
        try:
            setattr(check, name, value)
            actual = getattr(check, name)
        except (AttributeError, TypeError, ValueError) as exc:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} cannot be configured"
            ) from exc
        if actual is not value:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} did not retain its value"
            )


def _report_field(report, name):
    try:
        return getattr(report, name)
    except AttributeError:
        return _MISSING


def _children(report):
    value = _report_field(report, "issues")
    if type(value) not in (list, tuple):
        raise RuntimeError("ANSA CheckReport issues must be a list or tuple")
    return value


def _connection_context(base, deck, nodes, requested_ids):
    connections = inspection._require_api(base, "NodesToElements", "ansa.base.NodesToElements")(
        nodes, sources=["entities", "bcs"])
    if type(connections) is not dict:
        raise RuntimeError("NodesToElements must return a complete node mapping")
    seen_nodes, attached_node_ids, context_by_key = set(), set(), {}
    for node, attached in connections.items():
        ref = inspection._entity_reference(base, deck, node, "GRID")
        if ref["id"] not in requested_ids or ref["id"] in seen_nodes:
            raise RuntimeError("NodesToElements returned an unexpected or duplicate node")
        seen_nodes.add(ref["id"])
        if type(attached) not in (list, tuple):
            raise RuntimeError("NodesToElements attachments must be a list or tuple")
        if len(attached) > _MAX_CONTEXT_ENTITIES:
            raise RuntimeError("NodesToElements attachments exceed the context limit")
        if attached:
            attached_node_ids.add(ref["id"])
        for entity in attached:
            reference = inspection._entity_reference(base, deck, entity)
            if reference["type"] == "GRID":
                raise RuntimeError("NodesToElements returned a node as an attachment")
            key = (reference["type"], reference["id"])
            context_by_key.setdefault(key, (entity, reference))
            if len(context_by_key) > _MAX_CONTEXT_ENTITIES:
                raise RuntimeError("FreeNodes connection context exceeds 10000 entities")
    if seen_nodes != requested_ids:
        raise RuntimeError("NodesToElements omitted requested nodes; connectivity is unknown")
    pairs = [context_by_key[key] for key in sorted(context_by_key)]
    context_entities = [entity for entity, _reference in pairs]
    neighbor_nodes = []
    neighbor_ids = set()
    if context_entities:
        values = inspection._require_api(base, "CollectEntities", "ansa.base.CollectEntities")(
            deck, context_entities, "GRID")
        if type(values) not in (list, tuple) or len(values) > _MAX_CONTEXT_ENTITIES:
            raise RuntimeError("FreeNodes connection context has invalid or excessive GRID nodes")
        for entity in values:
            reference = inspection._entity_reference(base, deck, entity, "GRID")
            if reference["id"] in neighbor_ids:
                raise RuntimeError("FreeNodes connection context repeats a GRID node")
            neighbor_ids.add(reference["id"])
            if reference["id"] not in requested_ids:
                neighbor_nodes.append(entity)
        if not attached_node_ids.issubset(neighbor_ids):
            raise RuntimeError("FreeNodes connection context omitted attached GRID nodes; connectivity is unknown")
    if len(requested_ids | neighbor_ids) + len(context_entities) > _MAX_CONTEXT_ENTITIES:
        raise RuntimeError("FreeNodes connection context exceeds 10000 entities")
    return context_entities + neighbor_nodes, requested_ids | neighbor_ids, {
        "api": "ansa.base.NodesToElements",
        "sources": ["entities", "bcs"],
        "entities": [reference for _entity, reference in pairs],
        "node_ids": sorted(requested_ids | neighbor_ids),
    }


def _serialize_reports(
    base, deck, reports, requested_ids, context_keys, context_node_ids,
    primary_type="GRID", check_name="FreeNodes",
):
    if type(reports) is not list:
        raise RuntimeError("ANSA Check.execute must return a list of CheckReport objects")

    issue_counts = {"ok": 0, "error": 0, "warning": 0}
    failed_entity_ids = set()
    active = set()
    node_count = 0

    def serialize(report, depth):
        nonlocal node_count
        if depth > _MAX_REPORT_DEPTH:
            raise RuntimeError(f"ANSA CheckReport tree exceeds depth {_MAX_REPORT_DEPTH}")
        identity = id(report)
        if identity in active:
            raise RuntimeError("ANSA CheckReport tree contains a cycle")
        node_count += 1
        if node_count > _MAX_REPORT_NODES:
            raise RuntimeError(f"ANSA CheckReport tree exceeds {_MAX_REPORT_NODES} nodes")

        report_type = _report_field(report, "type")
        description = _report_field(report, "description")
        raw_status = _report_field(report, "status")
        raw_entities = _report_field(report, "entities")
        if report_type is _MISSING or type(report_type) is not str or not report_type:
            raise RuntimeError("ANSA CheckReport is missing a valid type")
        if description is _MISSING or type(description) is not str:
            raise RuntimeError("ANSA CheckReport is missing a valid description")
        if type(raw_entities) not in (list, tuple):
            raise RuntimeError("ANSA CheckReport entities must be a list or tuple")
        child_reports = _children(report)

        if (raw_status is _MISSING or raw_status is None
                or (type(raw_status) is str and raw_status == "")):
            status = None if raw_status is _MISSING else raw_status
        elif type(raw_status) is str:
            status = raw_status.lower()
            if status not in issue_counts:
                raise RuntimeError(f"ANSA CheckReport has unknown status: {raw_status!r}")
        else:
            raise RuntimeError("ANSA CheckReport status must be a string, empty, or None")

        entities = []
        entity_ids = []
        seen_entity_ids = set()
        raw_primary_count = 0
        scope_filtered = False
        for entity in raw_entities:
            reference = inspection._entity_reference(base, deck, entity)
            entity_id = reference["id"]
            key = (reference["type"], entity_id)
            if reference["type"] == "GRID":
                if primary_type == "GRID" and entity_id not in context_node_ids:
                    raise RuntimeError("ANSA CheckReport contains a GRID outside the verified context")
                if primary_type != "GRID" and key not in context_keys:
                    raise RuntimeError("ANSA CheckReport contains a GRID outside the verified context")
            elif key not in context_keys:
                raise RuntimeError("ANSA CheckReport contains an entity outside the verified context")
            if key in seen_entity_ids:
                raise RuntimeError("ANSA CheckReport repeats an entity within one issue")
            seen_entity_ids.add(key)
            if reference["type"] == primary_type:
                raw_primary_count += 1
                if entity_id in requested_ids:
                    entity_ids.append(entity_id)
                    entities.append(reference)
                elif primary_type == "GRID":
                    scope_filtered = True
                else:
                    raise RuntimeError("ANSA CheckReport contains a primary entity outside the verified scope")
            else:
                entities.append(reference)

        if status is None or status == "":
            if raw_entities:
                raise RuntimeError("ANSA CheckReport has entities but no status")

        active.add(identity)
        try:
            children = [row for child in child_reports
                        if (row := serialize(child, depth + 1)) is not None]
        finally:
            active.remove(identity)

        if child_reports and not children:
            return None
        if (not child_reports and primary_type != "GRID" and raw_primary_count == 0
                and not (status == "ok" and not raw_entities)):
            raise RuntimeError(
                f"ANSA {check_name} leaf has no {primary_type} entities; affected entities are unknown"
            )
        if not child_reports and status in {"error", "warning"} and raw_primary_count == 0:
            if primary_type == "GRID" and check_name == "FreeNodes":
                raise RuntimeError("ANSA FreeNodes issue has no GRID nodes; affected nodes are unknown")
            raise RuntimeError(
                f"ANSA {check_name} issue has no {primary_type} entities; affected entities are unknown"
            )
        if not child_reports and raw_entities and not entity_ids:
            return None
        if status in issue_counts and not child_reports:
            issue_counts[status] += 1
            if status in {"error", "warning"}:
                failed_entity_ids.update(entity_ids)

        return {
            "type": report_type,
            "description": description,
            "status": status,
            "entities": entities,
            "children": children,
            "scope_filtered": scope_filtered,
        }

    serialized = [row for report in reports if (row := serialize(report, 0)) is not None]
    issue_count = sum(issue_counts.values())
    return serialized, issue_count, issue_counts, sorted(failed_entity_ids)


def execute(state, tool, params):
    """Run a silent FreeNodes check on an explicit, verified GRID selection."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "check_run":
        raise ValueError(f"unsupported ANSA check operation: {tool!r}")

    params = inspection._params(
        params,
        ("deck", "check", "entity_ids"),
        ("consider_nodes_with_mass_free",),
    )
    if type(params["check"]) is not str or params["check"] != "free_nodes":
        raise ValueError("check must be the exact string 'free_nodes'")
    deck_value = params["deck"]
    if type(deck_value) is not int or deck_value <= 0:
        raise ValueError("deck must be a positive integer")
    consider_nodes_with_mass_free = params.get("consider_nodes_with_mass_free", True)
    if type(consider_nodes_with_mass_free) is not bool:
        raise ValueError("consider_nodes_with_mass_free must be a boolean")
    entity_ids = inspection._id_list(params["entity_ids"], "entity_ids", maximum=200)

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    deck = inspection._deck(base, params)
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    if deck != nastran:
        raise ValueError("check_run supports only the NASTRAN deck")

    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(base, "GetEntityCardValues", "ansa.base.GetEntityCardValues")
    entities, references = [], []
    for entity_id in entity_ids:
        entity, reference = inspection._get_entity(base, deck, "GRID", entity_id)
        entities.append(entity)
        references.append(reference)

    context_entities, context_node_ids, context = _connection_context(base, deck, entities, set(entity_ids))

    check_class = getattr(base, "Check", None)
    if not isinstance(check_class, type):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.base.Check is missing"
        )
    try:
        exec_selected = check_class.EXEC_ON_SELECTED
        report_none = check_class.REPORT_NONE
        keep_old = check_class.KEEP_OLD
    except AttributeError as exc:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: selected, silent, history-preserving ANSA checks are missing"
        ) from exc

    factory = inspection._factory(base, "general", "FreeNodes", _API)
    check = factory()
    if not isinstance(check, check_class):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} did not return an ansa.base.Check"
        )
    availability = getattr(check, "is_available_in_deck", None)
    if not callable(availability):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API}.is_available_in_deck is missing"
        )
    available = availability(deck)
    if available is False:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} does not support the requested deck"
        )
    if available is not True:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} does not confirm support for the requested deck"
        )

    options = _option_values(consider_nodes_with_mass_free)
    _configure_check(check, options)
    execute_check = getattr(check, "execute", None)
    if not callable(execute_check):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} Check.execute is missing"
        )
    reports = execute_check(
        exec_mode=exec_selected,
        report=report_none,
        history=keep_old,
        entities=entities + context_entities,
    )
    report_rows, issue_count, issue_counts, failed_entity_ids = _serialize_reports(
        base, deck, reports, set(entity_ids),
        {(reference["type"], reference["id"]) for reference in context["entities"]}, context_node_ids
    )
    result = {
        "deck": deck,
        "check": "free_nodes",
        "api": _API,
        "entity_type": "GRID",
        "entities": references,
        "options": options,
        "scope": "explicit",
        "read_only": True,
        "auto_fix": False,
        "connection_context": context,
        "native_report_count": len(reports),
        "report_count": len(report_rows),
        "issue_count": issue_count,
        "issue_counts": issue_counts,
        "failed_entity_ids": failed_entity_ids,
        "reports": report_rows,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"check result is not strict JSON: {exc}") from exc
    return result


__all__ = ["execute"]
