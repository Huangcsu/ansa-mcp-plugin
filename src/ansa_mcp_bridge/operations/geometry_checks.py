"""Run the verified Basic geometry check on an explicit NASTRAN FACE scope."""

import json

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import inspection


_API = "ansa.base.checks.geometry.Basic"
_OPTIONS = {
    "unchecked_faces": True,
    "collapsed_cons": True,
    "triple_cons": False,
    "cracks": True,
}
_MAX_FACES = 100
_MAX_CONTEXT_REFERENCES = 10000
_MAX_REPORT_REFERENCES = 10000
_MAX_REPORT_DEPTH = 8
_MAX_REPORT_NODES = 1000
_STATUS_PRIORITY = {"ok": 0, "warning": 1, "error": 2}
_MISSING = object()


def _encoded_json(value, label):
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be strict JSON") from exc


def _check_options(check, single_cons):
    requested = dict(_OPTIONS)
    requested["single_cons"] = single_cons

    parameters = getattr(check, "parameters", None)
    if not callable(parameters):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} parameters() is missing"
        )
    try:
        raw_parameters = parameters()
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} parameters() failed"
        ) from exc
    if type(raw_parameters) is not dict:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} parameters() did not return a dictionary"
        )
    parameter_values = {
        name: raw_parameters.get(name) for name in requested
    }
    _encoded_json(parameter_values, "ANSA check parameter readback")

    for name in requested:
        try:
            getattr(check, name)
        except AttributeError as exc:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} is missing"
            ) from exc

    attributes_after_set = {}
    for name, value in requested.items():
        try:
            setattr(check, name, value)
            actual = getattr(check, name)
        except Exception as exc:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} cannot be configured"
            ) from exc
        if type(actual) is not bool or actual is not value:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: {_API} option {name} did not retain a strict boolean"
            )
        attributes_after_set[name] = actual

    return attributes_after_set, {
        "parameters_before_set": parameter_values,
        "attributes_after_set": attributes_after_set,
        "authoritative": "attributes_after_set",
    }


def _boundary_context(base, deck, faces, face_references):
    collect = inspection._require_api(
        base, "CollectEntities", "ansa.base.CollectEntities"
    )
    get_faces_of_cons = inspection._require_api(
        base, "GetFacesOfCons", "ansa.base.GetFacesOfCons"
    )

    selected_face_ids = {reference["id"] for reference in face_references}
    cons_entities = {}
    cons_references = {}
    collected_owners = {}
    context = []
    boundary_reference_count = 0

    for face, face_reference in zip(faces, face_references):
        try:
            raw_cons = collect(deck, face, "CONS", recursive=False)
        except Exception as exc:
            raise RuntimeError(
                f"CollectEntities could not read FACE {face_reference['id']} boundary CONS"
            ) from exc
        if type(raw_cons) not in (list, tuple):
            raise RuntimeError("CollectEntities returned an invalid FACE boundary CONS list")
        if not raw_cons:
            raise RuntimeError(
                f"FACE {face_reference['id']} has no verifiable boundary CONS"
            )
        if boundary_reference_count + len(raw_cons) > _MAX_CONTEXT_REFERENCES:
            raise RuntimeError(
                f"FACE boundary context exceeds {_MAX_CONTEXT_REFERENCES} CONS references"
            )

        face_cons_ids = set()
        references = []
        for cons in raw_cons:
            reference = inspection._entity_reference(
                base, deck, cons, expected_type="CONS"
            )
            cons_id = reference["id"]
            if cons_id in face_cons_ids:
                raise RuntimeError(
                    f"CollectEntities repeated CONS {cons_id} for FACE {face_reference['id']}"
                )
            face_cons_ids.add(cons_id)
            references.append(reference)
            if cons_id in cons_references and cons_references[cons_id] != reference:
                raise RuntimeError(f"CONS {cons_id} identity changed within boundary context")
            cons_entities.setdefault(cons_id, cons)
            cons_references[cons_id] = reference
            collected_owners.setdefault(cons_id, set()).add(face_reference["id"])

        boundary_reference_count += len(references)
        context.append({"face": face_reference, "cons": references})

    if len(cons_entities) > _MAX_CONTEXT_REFERENCES:
        raise RuntimeError(
            f"FACE boundary context exceeds {_MAX_CONTEXT_REFERENCES} unique CONS"
        )

    reverse_owners = {}
    reverse_reference_count = 0
    for cons_id, cons in cons_entities.items():
        try:
            raw_faces = get_faces_of_cons(cons)
        except Exception as exc:
            raise RuntimeError(f"GetFacesOfCons failed for boundary CONS {cons_id}") from exc
        if type(raw_faces) not in (list, tuple):
            raise RuntimeError(f"GetFacesOfCons returned an invalid FACE list for CONS {cons_id}")
        if not raw_faces:
            raise RuntimeError(f"GetFacesOfCons returned no FACE for boundary CONS {cons_id}")
        if reverse_reference_count + len(raw_faces) > _MAX_CONTEXT_REFERENCES:
            raise RuntimeError(
                f"CONS reverse lookup exceeds {_MAX_CONTEXT_REFERENCES} entity references"
            )

        reverse_reference_count += len(raw_faces)
        seen_face_ids = set()
        owners = set()
        for reverse_face in raw_faces:
            reference = inspection._entity_reference(
                base, deck, reverse_face, expected_type="FACE"
            )
            face_id = reference["id"]
            if face_id in seen_face_ids:
                raise RuntimeError(
                    f"GetFacesOfCons repeated FACE {face_id} for CONS {cons_id}"
                )
            seen_face_ids.add(face_id)
            if face_id in selected_face_ids:
                owners.add(face_id)

        required_owners = collected_owners[cons_id]
        if owners != required_owners:
            missing = sorted(required_owners - owners)
            unexpected = sorted(owners - required_owners)
            raise RuntimeError(
                "GetFacesOfCons selected FACE owners do not match the collected "
                f"boundary for CONS {cons_id}: missing={missing}, unexpected={unexpected}"
            )
        reverse_owners[cons_id] = owners

    return context, cons_references, reverse_owners


def _report_field(report, name):
    try:
        return getattr(report, name)
    except AttributeError:
        return _MISSING


def _serialize_reports(base, deck, reports, selected_face_ids, cons_references, reverse_owners):
    if type(reports) is not list:
        raise RuntimeError("ANSA Check.execute must return a list of CheckReport objects")
    if not reports:
        raise RuntimeError("ANSA Basic returned no reports; refusing to claim a clean result")

    issue_counts = {"ok": 0, "error": 0, "warning": 0}
    failed_cons_ids = set()
    affected_face_ids = set()
    active = set()
    visited = set()
    report_node_count = 0
    report_reference_count = 0

    def serialize(report, depth):
        nonlocal report_node_count, report_reference_count
        if depth > _MAX_REPORT_DEPTH:
            raise RuntimeError(f"ANSA CheckReport tree exceeds depth {_MAX_REPORT_DEPTH}")
        identity = id(report)
        if identity in active:
            raise RuntimeError("ANSA CheckReport tree contains a cycle")
        if identity in visited:
            raise RuntimeError("ANSA CheckReport tree reuses a CheckReport object")
        report_node_count += 1
        if report_node_count > _MAX_REPORT_NODES:
            raise RuntimeError(f"ANSA CheckReport tree exceeds {_MAX_REPORT_NODES} nodes")
        visited.add(identity)
        active.add(identity)

        try:
            report_type = _report_field(report, "type")
            description = _report_field(report, "description")
            status = _report_field(report, "status")
            has_fix = _report_field(report, "has_fix")
            is_fixed = _report_field(report, "is_fixed")
            raw_entities = _report_field(report, "entities")
            raw_issues = _report_field(report, "issues")

            if type(report_type) is not str or not report_type:
                raise RuntimeError("ANSA CheckReport is missing a valid type")
            if type(description) is not str:
                raise RuntimeError("ANSA CheckReport is missing a valid description")
            if type(status) is not str or status not in _STATUS_PRIORITY:
                raise RuntimeError(f"ANSA CheckReport has missing or unknown status: {status!r}")
            if type(has_fix) is not bool:
                raise RuntimeError("ANSA CheckReport has_fix is missing or invalid")
            if type(is_fixed) is not bool:
                raise RuntimeError("ANSA CheckReport is_fixed is missing or invalid")
            if is_fixed:
                raise RuntimeError("ANSA CheckReport is_fixed is true")
            if type(raw_entities) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport entities must be a list or tuple")
            if type(raw_issues) not in (list, tuple):
                raise RuntimeError("ANSA CheckReport issues must be a list or tuple")
            if report_reference_count + len(raw_entities) > _MAX_REPORT_REFERENCES:
                raise RuntimeError(
                    f"ANSA CheckReport tree exceeds {_MAX_REPORT_REFERENCES} entity references"
                )

            entities = []
            entity_keys = set()
            for entity in raw_entities:
                reference = inspection._entity_reference(base, deck, entity)
                key = (reference["type"], reference["id"])
                if key in entity_keys:
                    raise RuntimeError("ANSA CheckReport repeats an entity within one report")
                entity_keys.add(key)
                if reference["type"] == "FACE":
                    if reference["id"] not in selected_face_ids:
                        raise RuntimeError(
                            "ANSA CheckReport contains a FACE outside the selected scope"
                        )
                elif reference["type"] == "CONS":
                    if reference["id"] not in cons_references:
                        raise RuntimeError(
                            "ANSA CheckReport contains a CONS outside selected FACE boundaries"
                        )
                else:
                    raise RuntimeError(
                        "ANSA CheckReport contains an entity outside selected FACE/CONS scope"
                    )
                entities.append(reference)
            report_reference_count += len(entities)

            issues = [serialize(issue, depth + 1) for issue in raw_issues]
            if issues:
                expected_status = max(
                    (issue["status"] for issue in issues),
                    key=_STATUS_PRIORITY.__getitem__,
                )
                if status != expected_status:
                    raise RuntimeError(
                        "ANSA CheckReport root status disagrees with child issue statuses"
                    )
            else:
                issue_counts[status] += 1
                if status in {"error", "warning"} and not entities:
                    raise RuntimeError(
                        "ANSA Basic warning/error leaf has no entities; affected FACE is unknown"
                    )
                if status in {"error", "warning"}:
                    for reference in entities:
                        if reference["type"] == "FACE":
                            affected_face_ids.add(reference["id"])
                        else:
                            cons_id = reference["id"]
                            failed_cons_ids.add(cons_id) if status == "error" else None
                            owners = reverse_owners.get(cons_id, set())
                            if not owners:
                                raise RuntimeError(
                                    f"boundary CONS {cons_id} has no verified selected FACE owner"
                                )
                            affected_face_ids.update(owners)

            return {
                "type": report_type,
                "description": description,
                "status": status,
                "has_fix": has_fix,
                "is_fixed": is_fixed,
                "entities": entities,
                "issues": issues,
            }
        finally:
            active.remove(identity)

    serialized = [serialize(report, 0) for report in reports]
    return {
        "reports": serialized,
        "report_node_count": report_node_count,
        "issue_counts": issue_counts,
        "issue_count": issue_counts["error"] + issue_counts["warning"],
        "failed_cons_ids": sorted(failed_cons_ids),
        "affected_face_ids": sorted(affected_face_ids),
    }


def execute(state, tool, params):
    """Run Basic geometry checks on explicit, verified NASTRAN FACE entities."""
    state.assert_main()
    if type(tool) is not str:
        raise TypeError("tool must be a string")
    if tool != "geometry_check":
        raise ValueError(f"unsupported geometry check operation: {tool!r}")
    if type(params) is not dict:
        raise TypeError("params must be an object")
    if len(_encoded_json(params, "params")) > MAX_FRAME_BYTES // 8:
        raise ValueError("geometry check request exceeds its input budget")

    params = inspection._params(
        params, ("deck", "entity_type", "entity_ids"), ("check", "single_cons")
    )
    check_name = params.get("check", "basic")
    if type(check_name) is not str or check_name != "basic":
        raise ValueError("check must be the exact string 'basic'")
    entity_type = params["entity_type"]
    if type(entity_type) is not str or entity_type != "FACE":
        raise ValueError("entity_type must be the exact string 'FACE'")
    single_cons = params.get("single_cons", False)
    if type(single_cons) is not bool:
        raise ValueError("single_cons must be a boolean")
    entity_ids = inspection._id_list(
        params["entity_ids"], "entity_ids", maximum=_MAX_FACES
    )

    from ansa import base, constants

    inspection._require_api(base, "CurrentDeck", "ansa.base.CurrentDeck")
    nastran = getattr(constants, "NASTRAN", _MISSING)
    if type(nastran) is not int or nastran <= 0:
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.constants.NASTRAN is missing or invalid"
        )
    deck = inspection._deck(base, params)
    if deck != nastran:
        raise ValueError("geometry_check supports only the current NASTRAN deck")

    inspection._require_api(base, "GetEntity", "ansa.base.GetEntity")
    inspection._require_api(
        base, "GetEntityCardValues", "ansa.base.GetEntityCardValues"
    )
    faces = []
    face_references = []
    for entity_id in entity_ids:
        face, reference = inspection._get_entity(base, deck, "FACE", entity_id)
        faces.append(face)
        face_references.append(reference)

    boundary_context, cons_references, reverse_owners = _boundary_context(
        base, deck, faces, face_references
    )
    selected_face_ids = {reference["id"] for reference in face_references}

    check_class = getattr(base, "Check", None)
    if not isinstance(check_class, type):
        raise inspection.CapabilityUnavailableError(
            "capability unavailable: ansa.base.Check is missing"
        )
    factory = inspection._factory(base, "geometry", "Basic", _API)
    check = factory()
    if not isinstance(check, check_class):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} did not return an ansa.base.Check"
        )
    available = getattr(check, "is_available_in_deck", None)
    if not callable(available):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} is_available_in_deck() is missing"
        )
    try:
        available_value = available(deck)
    except Exception as exc:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} deck availability could not be verified"
        ) from exc
    if type(available_value) is not bool:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} returned a non-boolean deck availability"
        )
    if not available_value:
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} is unavailable in NASTRAN"
        )

    options, option_readback = _check_options(check, single_cons)
    required_constants = {}
    for name in ("EXEC_ON_SELECTED", "REPORT_NONE", "KEEP_OLD"):
        value = getattr(check_class, name, _MISSING)
        if type(value) is not int:
            raise inspection.CapabilityUnavailableError(
                f"capability unavailable: ansa.base.Check.{name} is missing or invalid"
            )
        required_constants[name] = value

    execute_check = getattr(check, "execute", None)
    if not callable(execute_check):
        raise inspection.CapabilityUnavailableError(
            f"capability unavailable: {_API} Check.execute() is missing"
        )
    reports = execute_check(
        exec_mode=required_constants["EXEC_ON_SELECTED"],
        report=required_constants["REPORT_NONE"],
        history=required_constants["KEEP_OLD"],
        entities=faces,
    )
    report_result = _serialize_reports(
        base, nastran, reports, selected_face_ids, cons_references, reverse_owners
    )

    result = {
        "deck": deck,
        "check": "basic",
        "api": _API,
        "entity_type": "FACE",
        "entities": face_references,
        "options": options,
        "option_readback": option_readback,
        "scope": "explicit",
        "read_only": True,
        "auto_fix": False,
        "boundary_context": boundary_context,
        "native_report_count": len(reports),
        "report_count": len(report_result["reports"]),
        "report_node_count": report_result["report_node_count"],
        "issue_count": report_result["issue_count"],
        "issue_counts": report_result["issue_counts"],
        "failed_cons_ids": report_result["failed_cons_ids"],
        "affected_face_ids": report_result["affected_face_ids"],
        "reports": report_result["reports"],
        "reports_truncated": False,
    }
    if len(_encoded_json(result, "geometry check result")) > MAX_FRAME_BYTES // 2:
        raise RuntimeError("geometry check result exceeds its output budget")
    return result


__all__ = ["execute"]
