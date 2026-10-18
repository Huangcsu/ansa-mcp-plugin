"""Read ABAQUS surface SET members and supported facet connectivity."""

import json


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200
_DIRECT_MEMBER_SCAN_LIMIT = 200_000
_HEX8_FACE_POSITIONS = {
    "S1": (0, 1, 2, 3),
    "S2": (4, 5, 6, 7),
    "S3": (0, 1, 4, 5),
    "S4": (1, 2, 5, 6),
    "S5": (2, 3, 6, 7),
    "S6": (0, 3, 4, 7),
}
_SUPPORTED_SOLID_TYPES = {"HEXA"}
_COORDINATE_SCOPE = "node connectivity only; node coordinates are not included"


class _ReadError(Exception):
    pass


def _params(value, deck):
    if type(value) is not dict:
        raise TypeError("params must be an object")
    required = {"deck", "set_id"}
    optional = {"offset", "limit", "expand_nested"}
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ValueError("missing parameter(s): " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(extra)))

    request_deck = value["deck"]
    if type(request_deck) is not int or request_deck <= 0 or request_deck != deck:
        raise ValueError("params.deck must be a positive integer matching deck")
    set_id = _positive_id(value["set_id"], "set_id")
    offset = value.get("offset", 0)
    limit = value.get("limit", _DEFAULT_LIMIT)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    expand_nested = value.get("expand_nested", False)
    if type(expand_nested) is not bool:
        raise TypeError("expand_nested must be a boolean")
    return set_id, offset, limit, expand_nested


def _positive_id(value, label):
    if type(value) is int:
        result = value
    elif type(value) is str and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise _ReadError(f"{label} is not a positive integer")
    if result <= 0:
        raise _ReadError(f"{label} is not a positive integer")
    return result


def _result(deck, set_id, offset, limit, *, status, complete, members=None,
            total=None, has_more=None, next_offset=None, set_resolved=False,
            direct_membership=False, all_members_readable=False,
            scanned_members=0, scan_limit_reached=False, errors=None, partial_members=0,
            duplicate_members=0, expand_nested=False):
    result = {
        "read_only": True,
        "deck": deck,
        "set": {"type": "SET", "id": set_id, "resolved": set_resolved},
        "scope": {
            "membership": "direct SET members",
            "nested_sets": "reported as members; not expanded",
            "geometry": _COORDINATE_SCOPE,
            "solid_element_types": sorted(_SUPPORTED_SOLID_TYPES),
            "solid_topology": "HEXA with exactly eight unique node IDs",
            "solid_face_numbers": list(_HEX8_FACE_POSITIONS),
            "complete_definition": (
                "true only when offset is zero, the response contains the full SET, "
                "and all direct members are readable"
            ),
        },
        "direct_membership": direct_membership,
        "expanded_nested_sets": expand_nested,
        "status": status,
        "complete": complete,
        "all_members_readable": all_members_readable,
        "scan_limit": _DIRECT_MEMBER_SCAN_LIMIT,
        "scanned_members": scanned_members,
        "scan_limit_reached": scan_limit_reached,
        "offset": offset,
        "limit": limit,
        "total": total,
        "members": members or [],
        "has_more": has_more,
        "next_offset": next_offset,
        "partial_members": partial_members,
        "duplicate_members_removed": duplicate_members,
        "not_provided": ["node_coordinates", "distance_check", "coverage_check"],
        "errors": errors or [],
    }
    if expand_nested:
        result["scope"]["membership"] = "expanded SET and SHELL_SECTION leaf members"
        result["scope"]["nested_sets"] = "expanded with inherited explicit orientation; depth <= 16 and bounded references"
        result["scope"]["shell_properties"] = "SHELL_SECTION expands only its own PID-verified SHELL members"
        result["scope"]["complete_definition"] = (
            "true only when offset is zero, the response contains all expanded leaf members, "
            "and every member is readable"
        )
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"surface member result is not strict JSON: {exc}") from exc
    return result


def _exception_text(exc):
    message = str(exc)
    if isinstance(exc, _ReadError):
        return message
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _entity_type(entity, deck):
    method = getattr(entity, "ansa_type", None)
    if not callable(method):
        raise _ReadError("Entity.ansa_type is unavailable")
    value = method(deck)
    if type(value) is not str or not value:
        raise _ReadError("Entity.ansa_type did not return a type name")
    return value


def _special_id(base, deck, entity):
    reader = getattr(base, "GetEntityCardValues", None)
    if not callable(reader):
        raise _ReadError("ansa.base.GetEntityCardValues is unavailable")
    values = reader(deck, entity, ("__id__",))
    if type(values) is not dict or "__id__" not in values:
        raise _ReadError("GetEntityCardValues did not return __id__")
    return _positive_id(values["__id__"], "entity __id__")


def _card_values(base, deck, entity, required_fields, optional_fields=()):
    card_fields = getattr(entity, "card_fields", None)
    if not callable(card_fields):
        raise _ReadError("Entity.card_fields is unavailable; ordinary card fields were not read")
    labels = card_fields(deck)
    if type(labels) not in (list, tuple) or any(type(label) is not str for label in labels):
        raise _ReadError("Entity.card_fields did not return a list of field names")
    available = set(labels)
    missing = [field for field in required_fields if field not in available]
    if missing:
        raise _ReadError("card_fields is missing required field(s): " + ", ".join(missing))

    fields = tuple(required_fields) + tuple(
        field for field in optional_fields
        if field in available and field not in required_fields
    )
    reader = getattr(base, "GetEntityCardValues", None)
    if not callable(reader):
        raise _ReadError("ansa.base.GetEntityCardValues is unavailable")
    values = reader(deck, entity, fields)
    if type(values) is not dict:
        raise _ReadError("GetEntityCardValues did not return a dictionary")
    missing_values = [field for field in required_fields if field not in values]
    if missing_values:
        raise _ReadError(
            "GetEntityCardValues omitted required field(s): " + ", ".join(missing_values)
        )
    return available, values


def _field_id(values, field):
    if field not in values:
        raise _ReadError(f"card field {field} is unavailable")
    return _positive_id(values[field], f"card field {field}")


def _oriented_value(base, set_entity, entity, *, allow_missing=False):
    method = getattr(base, "GetEntityAttributeFromSet", None)
    if not callable(method):
        raise _ReadError("ansa.base.GetEntityAttributeFromSet is unavailable")
    value = method(set_entity, entity, "Oriented")
    if type(value) is not str:
        raise _ReadError("GetEntityAttributeFromSet(Oriented) did not return a string")
    if value == "not found" and allow_missing:
        return ""
    if value in ("not found", "invalid attribute"):
        raise _ReadError(f"GetEntityAttributeFromSet(Oriented) returned {value!r}")
    return value


def _new_row(entity_type, entity_id):
    return {
        "type": entity_type,
        "id": entity_id,
        "parent_element_id": None,
        "parent_element_type": None,
        "face_number": None,
        "side": None,
        "direction": None,
        "oriented": None,
        "node_ids": None,
        "geometry": None,
        "key": None,
        "supported": False,
        "complete": False,
        "errors": [],
    }


def _add_error(row, message):
    if message not in row["errors"]:
        row["errors"].append(message)


def _set_connectivity(row, node_ids, topology):
    row["node_ids"] = list(node_ids)
    row["geometry"] = {"topology": topology, "node_ids": list(node_ids)}


def _partial_shell_key(row):
    if row["id"] is None or row["oriented"] not in ("SPOS", "SNEG"):
        return None, None, False
    row["key"] = {
        "entity_type": "SHELL",
        "entity_id": row["id"],
        "oriented": row["oriented"],
    }
    return (
        ("SHELL_PARTIAL", row["id"], row["oriented"]),
        (1, row["id"], row["oriented"], ()),
        True,
    )


def _read_direction(row, base, set_entity, entity, inherited=None):
    try:
        oriented = _oriented_value(base, set_entity, entity, allow_missing=inherited in ("SPOS", "SNEG"))
    except Exception as exc:
        _add_error(row, _exception_text(exc))
        return

    if oriented in ("", " ") and inherited in ("SPOS", "SNEG"):
        oriented = inherited
        row["orientation_source"] = "inherited SET orientation"
    row["oriented"] = oriented
    if oriented in ("SPOS", "SNEG"):
        row["side"] = oriented
        row["direction"] = oriented
    elif oriented in ("", " "):
        _add_error(row, "SHELL member has no explicit SPOS/SNEG direction")
    else:
        _add_error(row, f"unsupported SHELL Oriented value: {oriented!r}")


def _shell_member(base, deck, set_entity, entity, row, inherited=None):
    try:
        available, values = _card_values(
            base, deck, entity, ("type", "G1", "G2", "G3"),
            tuple(f"G{index}" for index in range(4, 9)),
        )
    except Exception as exc:
        _add_error(row, _exception_text(exc))
        _read_direction(row, base, set_entity, entity, inherited)
        return _partial_shell_key(row)

    try:
        element_type = values["type"]
        if type(element_type) is not str or element_type.upper() != "QUAD":
            raise _ReadError(
                f"SHELL shape {element_type!r} is outside the supported four-node QUAD scope"
            )
        node_ids = [_field_id(values, field) for field in ("G1", "G2", "G3")]
        if "G4" not in available or "G4" not in values:
            raise _ReadError("four-node QUAD SHELL connectivity is missing G4")
        node_ids.append(_field_id(values, "G4"))
        extra_nodes = [field for field in ("G5", "G6", "G7", "G8") if field in values]
        if extra_nodes:
            raise _ReadError(
                "higher-order QUAD SHELL connectivity is outside the supported scope: "
                + ", ".join(extra_nodes)
            )
        if len(set(node_ids)) != len(node_ids):
            raise _ReadError("SHELL connectivity contains duplicate node IDs")
    except Exception as exc:
        _add_error(row, _exception_text(exc))
        _read_direction(row, base, set_entity, entity, inherited)
        return _partial_shell_key(row)

    topology = "quad4"
    _set_connectivity(row, node_ids, topology)
    _read_direction(row, base, set_entity, entity, inherited)
    row["supported"] = True
    row["complete"] = not row["errors"] and row["id"] is not None
    if row["id"] is None:
        _add_error(row, "SHELL member ID is unavailable")
        row["complete"] = False

    orientation = row["oriented"]
    if row["id"] is None or orientation not in ("SPOS", "SNEG"):
        return None, None, False
    stable_key = ("SHELL", row["id"], orientation, tuple(sorted(node_ids)))
    row["key"] = {
        "entity_type": "SHELL",
        "entity_id": row["id"],
        "oriented": orientation,
        "node_ids": sorted(node_ids),
    }
    return stable_key, (1, row["id"], orientation, tuple(sorted(node_ids))), True


def _solid_facet_member(base, deck, set_entity, entity, row):
    facet_entity_id = row["id"]
    try:
        _available, values = _card_values(
            base, deck, entity, ("EID", "G1", "G2", "G3", "G4"),
            tuple(f"G{i}" for i in range(5, 21)),
        )
        if any(values.get(f"G{i}") not in (None, "", 0, "0") for i in range(5, 21)):
            raise _ReadError("higher-order solid facets are outside the quad4 scope")
        parent_id = _field_id(values, "EID")
        node_ids = [_field_id(values, field) for field in ("G1", "G2", "G3", "G4")]
        if len(set(node_ids)) != 4:
            raise _ReadError("SOLIDFACET connectivity contains duplicate node IDs")
    except Exception as exc:
        _add_error(row, _exception_text(exc))
        return None, None, False

    row["id"] = parent_id
    row["parent_element_id"] = parent_id
    _set_connectivity(row, node_ids, "quad4")
    stable_key = ("SOLIDFACET", parent_id, tuple(sorted(node_ids)))
    row["key"] = {"parent_element_id": parent_id, "face_node_ids": sorted(node_ids)}

    if facet_entity_id != parent_id:
        _add_error(row, "SOLIDFACET __id__ does not match its EID card field")

    get_entity = getattr(base, "GetEntity", None)
    if not callable(get_entity):
        _add_error(row, "ansa.base.GetEntity is unavailable for the parent SOLID")
    else:
        try:
            parent = get_entity(deck, "SOLID", parent_id)
            if parent is None:
                raise _ReadError(f"parent SOLID {parent_id} was not found")
            parent_entity_type = _entity_type(parent, deck)
            if parent_entity_type != "SOLID":
                raise _ReadError(
                    f"parent element lookup returned {parent_entity_type}, expected SOLID"
                )
            _available, parent_values = _card_values(
                base, deck, parent,
                ("type", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"),
                tuple(f"G{i}" for i in range(9, 28)),
            )
            if any(parent_values.get(f"G{i}") not in (None, "", 0, "0") for i in range(9, 28)):
                raise _ReadError("higher-order HEXA is outside the eight-node topology scope")
            element_type = parent_values["type"]
            if type(element_type) is not str or not element_type:
                raise _ReadError("parent SOLID type card field is empty or invalid")
            element_type = element_type.upper()
            row["parent_element_type"] = element_type
            if element_type not in _SUPPORTED_SOLID_TYPES:
                raise _ReadError(
                    f"solid card type {element_type!r} is outside the supported HEXA topology scope"
                )

            parent_nodes = [_field_id(parent_values, f"G{index}") for index in range(1, 9)]
            if len(set(parent_nodes)) != 8:
                raise _ReadError("HEXA connectivity does not contain eight unique node IDs")
            facet_nodes = set(node_ids)
            matching_faces = [
                face_number
                for face_number, positions in _HEX8_FACE_POSITIONS.items()
                if facet_nodes == {parent_nodes[position] for position in positions}
            ]
            if len(matching_faces) != 1:
                raise _ReadError(
                    "SOLIDFACET node IDs do not match exactly one HEXA S1-S6 face"
                )
            row["face_number"] = matching_faces[0]
        except Exception as exc:
            _add_error(row, _exception_text(exc))

    if row["face_number"] is not None:
        row["side"] = row["face_number"]
        row["direction"] = row["face_number"]
    row["supported"] = row["face_number"] is not None
    row["complete"] = row["supported"] and not row["errors"]
    return stable_key, (0, parent_id, tuple(sorted(node_ids))), True


def _read_member(base, deck, set_entity, entity, index, inherited=None):
    try:
        entity_type = _entity_type(entity, deck)
    except Exception as exc:
        row = _new_row(None, None)
        _add_error(row, _exception_text(exc))
        return row, None, (2, "", 0, index), False

    try:
        entity_id = _special_id(base, deck, entity)
    except Exception as exc:
        entity_id = None
        id_error = _exception_text(exc)
    else:
        id_error = None
    row = _new_row(entity_type, entity_id)
    if id_error:
        _add_error(row, id_error)

    if entity_type == "SOLIDFACET":
        stable_key, sort_key, reliable = _solid_facet_member(
            base, deck, set_entity, entity, row
        )
        return row, stable_key, sort_key or (2, entity_type, entity_id or 0, index), reliable
    if entity_type == "SHELL":
        stable_key, sort_key, reliable = _shell_member(
            base, deck, set_entity, entity, row, inherited
        )
        if id_error:
            row["complete"] = False
            return row, None, (2, entity_type, entity_id or 0, index), False
        return row, stable_key, sort_key or (2, entity_type, entity_id or 0, index), reliable

    if entity_type == "SET":
        _add_error(row, "nested SET member is not expanded; only direct members are read")
    else:
        _add_error(
            row,
            f"direct member type {entity_type!r} is outside the supported SHELL/SOLIDFACET scope",
        )
    if entity_id is None:
        return row, None, (2, entity_type, 0, index), False
    row["key"] = {"entity_type": entity_type, "entity_id": entity_id}
    return row, (entity_type, entity_id), (2, entity_type, entity_id, index), True


def _error_summary(rows, maximum=20):
    summaries = []
    for row in rows:
        for error in row["errors"]:
            summaries.append({"type": row["type"], "id": row["id"], "error": error})
            if len(summaries) >= maximum:
                return summaries
    return summaries


def _expanded_leaves(base, deck, root):
    scanned = [0]

    def walk(container, inherited, ancestry):
        sid = _special_id(base, deck, container)
        if sid in ancestry:
            raise _ReadError("cyclic nested SET reference")
        if len(ancestry) >= 16:
            raise _ReadError("nested SET depth exceeds 16")
        labels = container.card_fields(deck)
        if "ORIENTATION" in labels:
            values = base.GetEntityCardValues(deck, container, ("ORIENTATION",))
            direction = values.get("ORIENTATION")
            if direction in ("SPOS", "SNEG"):
                inherited = direction
        members = base.CollectEntities(deck, container, "__ALL_ENTITIES__", recursive=False,
                                      model_browser_filter={}, no_expand_types=("SET",))
        if type(members) not in (list, tuple):
            raise _ReadError("nested SET direct membership is not a list")
        for member in members:
            scanned[0] += 1
            if scanned[0] > _DIRECT_MEMBER_SCAN_LIMIT:
                raise _ReadError("nested SET reference scan budget exceeded")
            member_type = _entity_type(member, deck)
            if member_type == "SET":
                direction = base.GetEntityAttributeFromSet(container, member, "Oriented")
                child_orientation = direction if direction in ("SPOS", "SNEG") else inherited
                yield from walk(member, child_orientation, ancestry + (sid,))
            elif member_type == "SHELL_SECTION":
                section_id = _special_id(base, deck, member)
                direction = _oriented_value(base, container, member, allow_missing=True)
                section_orientation = direction if direction in ("SPOS", "SNEG") else inherited
                shells = base.CollectEntities(deck, member, "SHELL", recursive=False, model_browser_filter={})
                if type(shells) not in (list, tuple) or not shells:
                    raise _ReadError(f"SHELL_SECTION {section_id} did not resolve a nonempty SHELL list")
                for shell in shells:
                    scanned[0] += 1
                    if scanned[0] > _DIRECT_MEMBER_SCAN_LIMIT:
                        raise _ReadError("nested SET/property reference scan budget exceeded")
                    if _entity_type(shell, deck) != "SHELL":
                        raise _ReadError("SHELL_SECTION collection returned a non-SHELL entity")
                    _labels, values = _card_values(base, deck, shell, ("PID",))
                    if _field_id(values, "PID") != section_id:
                        raise _ReadError(f"SHELL collection escaped section {section_id}")
                    yield container, shell, section_orientation
            else:
                yield container, member, inherited

    yield from walk(root, None, ())


def surface_members(base, constants, deck, params):
    """Read a paged ABAQUS SET surface snapshot without changing the model.

    Args:
        base: ANSA base API namespace.
        constants: ANSA constants namespace.
        deck: Explicit active deck value.
        params: Object with deck, set_id, and optional offset/limit/expand_nested.

    Returns:
        A strict-JSON result with total, members, complete, and explicit scope.
        ``complete`` is true only when this response contains the entire SET;
        ``all_members_readable`` separately reports member read completeness
        across the source SET, independent of pagination.
    """
    if type(deck) is not int or deck <= 0:
        raise ValueError("deck must be a positive integer")
    set_id, offset, limit, expand_nested = _params(params, deck)

    abaqus = getattr(constants, "ABAQUS", None)
    if type(abaqus) is not int or deck != abaqus:
        return _result(
            deck, set_id, offset, limit, status="unsupported", complete=False,
            errors=["surface_members supports only the ABAQUS deck"],
        )

    for name in ("GetEntity", "GetEntityCardValues", "CollectEntities"):
        if not callable(getattr(base, name, None)):
            return _result(
                deck, set_id, offset, limit, status="unsupported", complete=False,
                errors=[f"ansa.base.{name} is unavailable"],
            )

    try:
        set_entity = base.GetEntity(deck, "SET", set_id)
    except Exception as exc:
        return _result(
            deck, set_id, offset, limit, status="partial", complete=False,
            errors=["could not resolve SET: " + _exception_text(exc)],
        )
    if set_entity is None:
        return _result(
            deck, set_id, offset, limit, status="not_found", complete=False,
            errors=[f"ABAQUS SET {set_id} was not found"],
        )

    try:
        set_type = _entity_type(set_entity, deck)
        actual_set_id = _special_id(base, deck, set_entity)
        if set_type != "SET" or actual_set_id != set_id:
            raise _ReadError("resolved entity does not match the requested SET identity")
    except Exception as exc:
        return _result(
            deck, set_id, offset, limit, status="partial", complete=False,
            errors=["could not verify SET identity: " + _exception_text(exc)],
        )

    try:
        native_members = base.CollectEntities(
            deck,
            set_entity,
            "__ALL_ENTITIES__",
            recursive=False,
            filter_visible=False,
            prop_from_entities=False,
            mat_from_entities=False,
            model_browser_filter={},
            no_expand_types=("SET",),
            hidden_entities=True,
        )
        if type(native_members) not in (list, tuple):
            raise _ReadError("CollectEntities did not return a direct member list")
        # CollectEntitiesI loses SOLIDFACET identity on this ANSA build.
        if expand_nested:
            direct = iter(_expanded_leaves(base, deck, set_entity))
            native_members = None
        else:
            direct = iter((set_entity, member, None) for member in native_members)
    except Exception as exc:
        return _result(
            deck, set_id, offset, limit, status="partial", complete=False,
            set_resolved=True,
            errors=["could not read direct SET members: " + _exception_text(exc)],
        )

    records = []
    unique = {}
    unreliable_keys = False
    duplicate_count = 0
    scanned_members = 0
    scan_limit_reached = False
    scan_error = None
    iterator_exhausted = False
    try:
        while scanned_members < _DIRECT_MEMBER_SCAN_LIMIT:
            try:
                owner, entity, inherited = next(direct)
            except StopIteration:
                iterator_exhausted = True
                break
            index = scanned_members
            scanned_members += 1
            row, stable_key, sort_key, reliable = _read_member(
                base, deck, owner, entity, index, inherited
            )
            if reliable and stable_key is not None:
                if stable_key in unique:
                    duplicate_count += 1
                    continue
                unique[stable_key] = (row, sort_key)
            else:
                unreliable_keys = True
                records.append((row, sort_key))
    except Exception as exc:
        scan_error = "direct SET member iteration failed: " + _exception_text(exc)
    if native_members is not None and scanned_members == len(native_members) and scan_error is None:
        iterator_exhausted = True
    if not iterator_exhausted and scan_error is None:
        scan_limit_reached = scanned_members >= _DIRECT_MEMBER_SCAN_LIMIT

    if scanned_members == 0 and scan_error is None and not scan_limit_reached:
        return _result(
            deck, set_id, offset, limit, status="empty", complete=True,
            set_resolved=True, direct_membership=not expand_nested, total=0, has_more=False,
            all_members_readable=True, scanned_members=0,
            expand_nested=expand_nested,
        )

    records.extend(unique.values())
    records.sort(key=lambda record: record[1])

    scan_complete = scan_error is None and not scan_limit_reached
    total = None if unreliable_keys or not scan_complete else len(records)
    page_records = records[offset:offset + limit]
    page = [row for row, _sort_key in page_records]
    has_more = None if total is None else offset + len(page) < total
    next_offset = offset + len(page) if has_more else None
    all_members_readable = scan_complete and not unreliable_keys and all(
        row["complete"] for row, _key in records
    )
    complete = (
        all_members_readable
        and offset == 0
        and total is not None
        and len(page) == total
    )
    status = "ok" if all_members_readable else "partial"
    errors = _error_summary([row for row, _key in records])
    if scan_limit_reached:
        errors.append({
            "error": (
                f"direct SET member scan reached the {_DIRECT_MEMBER_SCAN_LIMIT} member "
                "budget before iterator exhaustion; result may be truncated"
            )
        })
    if scan_error:
        errors.append({"error": scan_error})
    return _result(
        deck, set_id, offset, limit, status=status, complete=complete,
        members=page, total=total, has_more=has_more, next_offset=next_offset,
        set_resolved=True, direct_membership=not expand_nested,
        all_members_readable=all_members_readable,
        scanned_members=scanned_members, scan_limit_reached=scan_limit_reached,
        errors=errors,
        partial_members=sum(not row["complete"] for row, _key in records),
        duplicate_members=duplicate_count, expand_nested=expand_nested,
    )


__all__ = ["surface_members"]
