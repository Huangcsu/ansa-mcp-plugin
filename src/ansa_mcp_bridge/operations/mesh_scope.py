"""Explicit native mesh relationships and independently verified freeze operations."""

import copy
import re

from . import entity_catalog as catalog, geometry_delete, inspection, structured_evidence as evidence
from . import structured_native as native, volume_meshing as guards


_READ_TYPES = {"FACE", "CONS", "SHELL", "SOLID", "VOLUME"}
_FREEZE_TYPES = {"FACE", "SHELL", "SOLID"}
_LIMITS = {"FACE": 1000, "CONS": 8000, "SHELL": 20000, "SOLID": 20000,
           "GRID": 50000, "VOLUME": 1000, "FE PERIMETER": 8000}
_MAX_MODEL_ENTITIES = 100000
_MAX_MEMBERSHIPS = 200000
_MAX_SCOPE_BYTES = 32 * 1024 * 1024
_MAX_GUARD_BYTES = 64 * 1024 * 1024
_REQUEST = ("deck", "entity_type", "entity_ids")


def normalize(params, *, writing=False):
    extra = ("action", "expected_scope_sha256", "backup_path") if writing else ()
    inspection._params(params, _REQUEST + extra)
    deck = native._positive_int(params["deck"], "deck")
    kind = params["entity_type"]
    if type(kind) is not str or kind not in (_FREEZE_TYPES if writing else _READ_TYPES):
        raise ValueError("unsupported explicit mesh scope entity_type")
    clean = {"deck": deck, "entity_type": kind,
             "entity_ids": sorted(native._id_list(params["entity_ids"], "entity_ids", 200))}
    if writing:
        if params["action"] not in ("freeze", "unfreeze"):
            raise ValueError("action must be freeze or unfreeze")
        clean.update(action=params["action"], expected_scope_sha256=native._validate_digest(
            params["expected_scope_sha256"], "expected_scope_sha256"), backup_path=params["backup_path"])
        if type(clean["backup_path"]) is not str or not clean["backup_path"]:
            raise ValueError("backup_path must be a non-empty absolute .ansa path")
    return clean


def _entities(base, deck, values, kind, label, *, zero_empty=False):
    if zero_empty and type(values) is int and values == 0:
        values = []
    if type(values) not in (list, tuple):
        raise RuntimeError(label + " did not return a complete entity list")
    limit = _LIMITS.get(kind, _MAX_MEMBERSHIPS)
    if len(values) > limit:
        raise ValueError(label + " exceeds the entity budget")
    found = {}
    for entity in values:
        ref = catalog._entity_reference(base, deck, entity, kind)
        if ref["id"] in found:
            raise RuntimeError(label + " returned duplicate entity identities")
        found[ref["id"]] = entity
    return dict(sorted(found.items()))


def _collect(base, deck, container, kind):
    # These are membership queries, never CONS -> GRID (which can instantiate seeds).
    return _entities(base, deck, base.CollectEntities(
        deck, container, kind, filter_visible=False, hidden_entities=True), kind, "CollectEntities " + kind)


def _card(base, deck, entity, kind):
    ref = catalog._entity_reference(base, deck, entity, kind)
    row = guards._read_full_card(base, deck, entity, ref, kind + " scope card")
    field = "FROZEN" if kind in {"SHELL", "SOLID"} else "Frozen"
    value = row["fields"].get(field)
    row["freeze_state"] = {"field": field, "value": value,
                           "known": field in row["card_fields"] and value in ("YES", "NO")}
    return row


def _derive_protection(body):
    """Explain Macro protection separately from literal FACE/CONS card flags."""
    faces = {row["entity"]["id"]: row for row in body["faces"]}
    for row in body["faces"]:
        own = row["freeze_state"]
        macro = faces[row["macro_id"]]["freeze_state"]
        locked = any(s["known"] and s["value"] == "YES" for s in (own, macro))
        row["effective_macro_protection"] = {
            "value": True if locked else False if own["known"] and macro["known"] else None,
            "macro_id": row["macro_id"], "origin": "native_macro_membership_and_Frozen_flags"}
    for row in body["cons"]:
        neighbors = [faces[ident] for ident in row["face_ids"] if ident in faces]
        locking = sorted({f["macro_id"] for f in neighbors
                          if f["effective_macro_protection"]["value"] is True})
        raw = row["freeze_state"]
        known = (not row["unexpanded_face_ids"] and raw["known"]
                 and all(f["effective_macro_protection"]["value"] is not None for f in neighbors))
        locked = bool(locking) or raw["known"] and raw["value"] == "YES"
        row["effective_macro_protection"] = {
            "value": True if locked else False if known else None,
            "protecting_macro_ids": locking,
            "origin": "topology_and_macro_flags; not_a_change_to_CONS_Frozen"}


def capture(state, base, mesh, constants, params):
    """Read root macros, their one-hop CONS neighbors, FE nodes and volume membership."""
    state.assert_main()
    clean = normalize(params)
    deck, kind = clean["deck"], clean["entity_type"]
    native._check_nastran(constants, deck)
    if base.CurrentDeck() != deck:
        raise ValueError("requested deck must be active")
    context = native._context_for_preview(state, deck)
    roots = {ident: catalog._get_entity(base, deck, kind, ident)[0] for ident in clean["entity_ids"]}
    faces, macros, cons, perimeters = {}, {}, {}, {}
    shells = dict(roots) if kind == "SHELL" else {}
    solids = dict(roots) if kind == "SOLID" else {}
    primary_faces, primary_macros = set(), set()
    face_cons = {}

    def add_macro(face, primary=False):
        macro = base.GetFaceMacro(face)
        if macro is None:
            raise RuntimeError("FACE has no readable native Macro")
        ident = catalog._entity_reference(base, deck, macro, "FACE")["id"]
        if ident not in macros:
            members = _entities(base, deck, base.GetFacesOfMacros([macro]), "FACE", "GetFacesOfMacros")
            if face._id not in members or ident not in members:
                raise RuntimeError("FACE/Macro membership identity is inconsistent")
            attached = _entities(base, deck, mesh.GetMacroShells([macro]), "SHELL", "GetMacroShells", zero_empty=True)
            raw = mesh.PerimetersOfMacro([macro])
            if type(raw) not in (list, tuple) or not raw:
                raise RuntimeError("Macro perimeters are unavailable")
            refs = [catalog._entity_reference(base, deck, entity) for entity in raw]
            keys = [(ref["type"], ref["id"]) for ref in refs]
            if len(keys) != len(set(keys)) or len(keys) > _LIMITS["CONS"]:
                raise ValueError("Macro perimeters are duplicate or exceed the budget")
            for entity, ref in zip(raw, refs):
                if ref["type"] not in {"CONS", "FE PERIMETER"}:
                    raise NotImplementedError("unsupported native Macro perimeter type: " + ref["type"])
                perimeters[(ref["type"], ref["id"])] = entity
            macros[ident] = {"macro_id": ident, "face_ids": sorted(members),
                             "shell_ids": sorted(attached), "perimeters": sorted(refs, key=lambda r: (r["type"], r["id"]))}
            faces.update(members)
            shells.update(attached)
        if face._id not in macros[ident]["face_ids"]:
            raise RuntimeError("inconsistent Macro membership on a subsequent FACE")
        if primary:
            primary_macros.add(ident)
            primary_faces.update(macros[ident]["face_ids"])
        if len(faces) > _LIMITS["FACE"] or len(shells) > _LIMITS["SHELL"]:
            raise ValueError("Macro expansion exceeds FACE/SHELL budget")

    if kind == "FACE":
        for entity in roots.values():
            add_macro(entity, True)
    elif kind == "CONS":
        cons.update(roots)
        for entity in roots.values():
            adjacent = _entities(base, deck, base.GetFacesOfCons(entity), "FACE", "GetFacesOfCons")
            if not adjacent:
                raise RuntimeError("selected CONS has no native FACE membership")
            for face in adjacent.values():
                add_macro(face, True)
    elif kind == "SHELL":
        for shell in roots.values():
            owners = _entities(base, deck, mesh.GetShellMacro([shell]), "FACE", "GetShellMacro", zero_empty=True)
            if len(owners) > 1:
                raise RuntimeError("a SHELL belongs to multiple native Macros")
            for owner in owners.values():
                add_macro(owner, True)
                if not any(shell._id in row["shell_ids"] for row in macros.values()):
                    raise RuntimeError("SHELL/Macro reverse membership is inconsistent")
    elif kind == "VOLUME":
        for volume in roots.values():
            for face in _collect(base, deck, volume, "FACE").values():
                add_macro(face, True)
            shells.update(_collect(base, deck, volume, "SHELL"))
            solids.update(_collect(base, deck, volume, "SOLID"))

    # Expand only the root macros' CAD boundaries and one adjacent Macro hop.
    # No recursive flood fill through the entire part or all volume boundaries.
    for face_id in sorted(primary_faces):
        local = _collect(base, deck, faces[face_id], "CONS")
        face_cons[face_id] = sorted(local)
        cons.update(local)
    for entity in list(cons.values()):
        adjacent = _entities(base, deck, base.GetFacesOfCons(entity), "FACE", "GetFacesOfCons")
        for face in adjacent.values():
            add_macro(face)
    for face_id, face in sorted(faces.items()):
        local = _collect(base, deck, face, "CONS")
        face_cons[face_id] = sorted(local)
        cons.update(local)
    if len(cons) > _LIMITS["CONS"]:
        raise ValueError("CONS neighborhood exceeds the budget")

    cons_rows = []
    for ident, entity in sorted(cons.items()):
        adjacent = _entities(base, deck, base.GetFacesOfCons(entity), "FACE", "GetFacesOfCons")
        if not adjacent:
            raise RuntimeError("CONS has no native FACE membership")
        cons_rows.append({**_card(base, deck, entity, "CONS"), "face_ids": sorted(adjacent),
                          "unexpanded_face_ids": sorted(set(adjacent) - faces.keys())})

    # Native reverse ownership is obtained from complete VOLUME membership,
    # never inferred from proximity, bounds or the number of elements.
    volumes, occurrences = [], 0
    for ident, volume in _collect(base, deck, None, "VOLUME").items():
        vf = _collect(base, deck, volume, "FACE")
        vs = _collect(base, deck, volume, "SHELL")
        ve = _collect(base, deck, volume, "SOLID")
        occurrences += len(vf) + len(vs) + len(ve)
        if occurrences > _MAX_MEMBERSHIPS:
            raise ValueError("VOLUME membership scan exceeds the budget")
        if (kind == "VOLUME" and ident in roots or set(vf) & faces.keys()
                or set(vs) & shells.keys() or set(ve) & solids.keys()):
            volumes.append({**_card(base, deck, volume, "VOLUME"), "face_ids": sorted(vf),
                            "shell_ids": sorted(vs), "solid_ids": sorted(ve)})
    if kind == "VOLUME" and not set(roots) <= {v["entity"]["id"] for v in volumes}:
        raise RuntimeError("selected VOLUME is missing from the complete native inventory")

    face_macros, shell_macros = {}, {}
    for mid, macro in macros.items():
        for ident in macro["face_ids"]:
            if ident in face_macros and face_macros[ident] != mid:
                raise RuntimeError("FACE belongs to conflicting native Macros")
            owner = base.GetFaceMacro(faces[ident])
            if owner is None or catalog._entity_reference(base, deck, owner, "FACE")["id"] != mid:
                raise RuntimeError("FACE/Macro reverse membership is inconsistent")
            face_macros[ident] = mid
        for ident in macro["shell_ids"]:
            shell_macros.setdefault(ident, []).append(mid)
            if len(shell_macros[ident]) > 1:
                raise RuntimeError("SHELL belongs to conflicting native Macros")
    volume_membership = {kind: {} for kind in ("FACE", "SHELL", "SOLID")}
    for volume in volumes:
        for entity_kind, field in (("FACE", "face_ids"), ("SHELL", "shell_ids"), ("SOLID", "solid_ids")):
            for ident in volume[field]:
                volume_membership[entity_kind].setdefault(ident, []).append(volume["entity"]["id"])

    shell_rows, solid_rows, grid_ids = [], [], set()
    for element_kind, entities, rows in (("SHELL", shells, shell_rows), ("SOLID", solids, solid_rows)):
        if len(entities) > _LIMITS[element_kind]:
            raise ValueError(element_kind + " expansion exceeds the budget")
        for ident, entity in sorted(entities.items()):
            row = _card(base, deck, entity, element_kind)
            node_ids = []
            for field, value in row["fields"].items():
                if re.fullmatch(r"G[1-9][0-9]*", field) and value not in (None, "", 0):
                    node_ids.append(native._positive_int(value, element_kind + " " + field))
            if not node_ids or len(node_ids) != len(set(node_ids)):
                raise NotImplementedError("element has no complete distinct NASTRAN G* node fields")
            row["node_ids"] = sorted(node_ids)
            if element_kind == "SHELL":
                row["macro_ids"] = sorted(shell_macros.get(ident, []))
            row["volume_ids"] = volume_membership[element_kind].get(ident, [])
            rows.append(row)
            grid_ids.update(node_ids)
    if len(grid_ids) > _LIMITS["GRID"]:
        raise ValueError("related GRID scope exceeds the budget")
    grids = {ident: catalog._get_entity(base, deck, "GRID", ident)[0] for ident in sorted(grid_ids)}
    attached = base.NodesToElements(list(grids.values()), sources=["entities", "bcs"]) if grids else {}
    if type(attached) is not dict:
        raise RuntimeError("NodesToElements did not return a complete mapping")
    node_context, link_count = {}, 0
    for node, values in attached.items():
        ref = catalog._entity_reference(base, deck, node, "GRID")
        ident = ref["id"]
        if ident not in grids or ident in node_context or type(values) not in (list, tuple):
            raise RuntimeError("NodesToElements returned invalid node membership")
        refs = [catalog._entity_reference(base, deck, entity) for entity in values]
        keys = [(r["type"], r["id"]) for r in refs]
        if len(keys) != len(set(keys)) or any(r["type"] == "GRID" for r in refs):
            raise RuntimeError("NodesToElements returned duplicate or invalid attachments")
        link_count += len(refs)
        if link_count > _MAX_MEMBERSHIPS:
            raise ValueError("node attachments exceed the budget")
        node_context[ident] = sorted(refs, key=lambda r: (r["type"], r["id"]))
    if set(node_context) != set(grids):
        raise RuntimeError("NodesToElements omitted requested nodes")

    target_faces = sorted(primary_macros) if kind == "FACE" else []
    target_cons = sorted({ref["id"] for mid in primary_macros for ref in macros[mid]["perimeters"]
                          if ref["type"] == "CONS"}) if kind == "FACE" else []
    body = {"version": 1, "context": context, "selection": clean,
            "scope_policy": "root_macros_and_one_CONS_neighbor_hop; volumes_are_membership_only; node_attachments_not_recursively_expanded",
            "faces": [{**_card(base, deck, face, "FACE"), "cons_ids": face_cons[ident],
                       "macro_id": face_macros[ident],
                       "volume_ids": volume_membership["FACE"].get(ident, [])}
                      for ident, face in sorted(faces.items())],
            "macros": [{**row, "primary": mid in primary_macros} for mid, row in sorted(macros.items())],
            "cons": cons_rows, "shells": shell_rows, "solids": solid_rows, "volumes": volumes,
            "fe_perimeters": [_card(base, deck, entity, "FE PERIMETER") for (t, _), entity in sorted(perimeters.items()) if t == "FE PERIMETER"],
            "nodes": [{"card": guards._read_full_card(base, deck, node, catalog._entity_reference(base, deck, node, "GRID"), "related GRID"),
                       "attached_entities": node_context[ident]} for ident, node in grids.items()],
            "freeze_effects": {"face_ids": sorted(primary_faces) if kind == "FACE" else [],
                               "cons_ids": target_cons},
            "freeze_targets": {"FACE": target_faces, "CONS": [],
                               "SHELL": clean["entity_ids"] if kind == "SHELL" else [],
                               "SOLID": clean["entity_ids"] if kind == "SOLID" else []}}
    _derive_protection(body)
    if native._context_for_preview(state, deck) != context:
        raise RuntimeError("model or target changed during relationship readback")
    if len(evidence._encoded(body)) > _MAX_SCOPE_BYTES:
        raise ValueError("mesh relationship scope exceeds the 32 MiB evidence budget")
    return {"ok": True, "read_only": True, **body, "scope_sha256": native._hash(body),
            "summary": {name: len(body[name]) for name in ("faces", "macros", "cons", "shells", "solids", "nodes", "volumes")}}


def _guard(base, deck):
    """Keep raw cards and native geometry/ownership while bounding the whole-model read."""
    raw = base.CollectEntities(deck, None, "__ALL_ENTITIES__", False, filter_visible=False, hidden_entities=True)
    if type(raw) not in (list, tuple) or len(raw) > _MAX_MODEL_ENTITIES:
        raise ValueError("freeze protection requires at most 100000 model entities")
    rows, size = {}, 0
    for entity in raw:
        ref = catalog._entity_reference(base, deck, entity)
        key = (ref["type"], ref["id"])
        if key in rows:
            raise RuntimeError("duplicate model entity identity in freeze protection")
        row = guards._read_full_card(base, deck, entity, ref, "freeze protected card")
        if ref["type"] == "FACE":
            row["geometry"] = geometry_delete._geometry(base, entity)
        elif ref["type"] in {"CONS", "CURVE"}:
            bounds = base.BoundBox(entity)
            if type(bounds) not in (list, tuple) or len(bounds) != 6:
                raise RuntimeError("protected CAD bounds are unavailable")
            row["bounds"] = [native._finite(v, "protected CAD bound") for v in bounds]
        if ref["type"] in {"FACE", "SHELL", "SOLID", "VOLUME"}:
            # Unassigned geometry is valid input: preserve the actual nullable
            # assignments instead of imposing a meshing property's prerequisites.
            row["assignments"] = catalog._read_card(base, deck, entity, ref, ("__part__", "__prop__"))
        if ref["type"] == "VOLUME":
            row["members"] = {kind: sorted(_collect(base, deck, entity, kind)) for kind in ("FACE", "SHELL", "SOLID")}
        size += len(evidence._encoded(row))
        if size > _MAX_GUARD_BYTES:
            raise ValueError("freeze protection exceeds the 64 MiB raw-card budget")
        rows[key] = row
    return rows


def _targets(scope):
    result = {}
    for section, kind in (("faces", "FACE"), ("cons", "CONS"), ("shells", "SHELL"), ("solids", "SOLID")):
        ids = set(scope["freeze_targets"][kind])
        for row in scope[section]:
            if row["entity"]["id"] in ids:
                frozen = row["freeze_state"]
                if not frozen["known"]:
                    raise NotImplementedError(kind + " has no explicit YES/NO frozen state")
                result[(kind, row["entity"]["id"])] = frozen["field"]
        if ids != {ident for entity_kind, ident in result if entity_kind == kind}:
            raise RuntimeError("freeze targets are missing from relationship readback")
    if not result:
        raise ValueError("selection has no supported freeze targets")
    return result


def _verify_guard(before, after, targets, desired):
    if before.keys() != after.keys():
        raise RuntimeError("model entity inventory changed during freeze operation")
    changed = []
    for key, old in before.items():
        expected = copy.deepcopy(old) if key in targets else old
        if key in targets:
            field = targets[key]
            if field not in old["card_fields"] or old["fields"].get(field) not in ("YES", "NO"):
                raise RuntimeError("target freeze state unavailable in protected snapshot")
            expected["fields"][field] = desired
            if old["fields"][field] != desired:
                changed.append({"entity": old["entity"], "field": field,
                                "before": old["fields"][field], "after": desired})
        if not catalog._strict_equal(expected, after[key]):
            raise RuntimeError("unexpected protected change or incomplete freeze: " + str(key))
    if not set(targets) <= before.keys():
        raise RuntimeError("freeze targets missing from model protection snapshot")
    return changed


def _verify_relations(before, after, targets, desired):
    expected = copy.deepcopy(before)
    for section in ("faces", "cons", "shells", "solids"):
        for row in expected[section]:
            key = (row["entity"]["type"], row["entity"]["id"])
            if key in targets:
                row["fields"][targets[key]] = desired
                row["freeze_state"]["value"] = desired
    expected.pop("scope_sha256", None)
    _derive_protection(expected)
    observed = {key: value for key, value in after.items() if key != "scope_sha256"}
    if not catalog._strict_equal(expected, observed):
        raise RuntimeError("mesh relationships or non-target scope state changed during freeze")


def _observe_targets(state, base, deck, context, targets):
    """Read known target fields even when graph/mesh queries no longer work."""
    states, errors = [], []
    for (kind, ident), field in sorted(targets.items()):
        ref = {"deck": deck, "type": kind, "id": ident}
        try:
            guards._same_context(state, deck, context)
        except Exception as exc:
            errors.append({"entity": ref, "error": native._error_text(exc)})
            break
        try:
            entity, actual = catalog._get_entity(base, deck, kind, ident)
            labels = guards._card_fields(entity, deck, "freeze target observation")
            if field not in labels:
                raise NotImplementedError("freeze field is unavailable: " + field)
            value = catalog._read_card(base, deck, entity, actual, (field,))[field]
            guards._same_context(state, deck, context)
            states.append({"entity": actual, "freeze_state": {"field": field, "value": value,
                                                               "known": value in ("YES", "NO")}})
        except Exception as exc:
            errors.append({"entity": ref, "error": native._error_text(exc)})
    return states, errors


def freeze(state, base, mesh, constants, params):
    state.assert_main()
    clean = normalize(params, writing=True)
    selection = {key: clean[key] for key in _REQUEST}
    result = {"ok": False, "action": clean["action"], "selection": selection,
              "api_called": False, "backup_attempted": False, "scope_verified": False,
              "possible_effects": False, "recovery_required": False}
    context, targets = None, {}
    try:
        context = guards._execution_context(state, clean["deck"])
        before_scope = capture(state, base, mesh, constants, selection)
        if before_scope["scope_sha256"] != clean["expected_scope_sha256"]:
            raise ValueError("mesh scope is stale or belongs to a different selection")
        targets = _targets(before_scope)
        desired = "YES" if clean["action"] == "freeze" else "NO"
        result.update(scope_sha256_before=before_scope["scope_sha256"],
                      freeze_targets=before_scope["freeze_targets"])
        if clean["entity_type"] == "FACE":
            if any(ref["type"] != "CONS" for row in before_scope["macros"] if row["primary"] for ref in row["perimeters"]):
                raise NotImplementedError("FACE freeze currently requires CAD CONS macro perimeters")
        api_name = ("FacesFreezeUnFreeze" if clean["entity_type"] == "FACE" else
                    "FreezeElements" if clean["action"] == "freeze" else "UnFreezeElements")
        api = inspection._require_api(base, api_name, "ansa.base." + api_name)
        rows = {key: row for section in ("faces", "cons", "shells", "solids")
                for row in before_scope[section] for key in [(row["entity"]["type"], row["entity"]["id"])]}
        if all(rows[key]["fields"][field] == desired for key, field in targets.items()):
            guards._same_context(state, clean["deck"], context)
            return {**result, "ok": True, "scope_verified": True, "no_op": True,
                    "scope_sha256_after": before_scope["scope_sha256"], "changes": []}
        before = _guard(base, clean["deck"])
        # Check complete target availability before saving or calling a writer.
        if not set(targets) <= before.keys():
            raise RuntimeError("freeze targets missing from model protection snapshot")
        for key, field in targets.items():
            if field not in before[key]["card_fields"] or before[key]["fields"].get(field) not in ("YES", "NO"):
                raise RuntimeError("target freeze state unavailable in protected snapshot")
            if before[key]["fields"][field] != rows[key]["fields"][field]:
                raise RuntimeError("freeze state changed after relationship readback")
        result["protection_before"] = {"entity_count": len(before), "sha256": native._hash(
            [before[key] for key in sorted(before)])}
        args = native._backup_preflight(base, clean)
        guards._same_context(state, clean["deck"], context)
        result["backup_attempted"] = True
        result["backup"] = native._verify_backup(base, constants, *args)
        result["backup_verified"] = True
        guards._same_context(state, clean["deck"], context)
        if not catalog._strict_equal(_guard(base, clean["deck"]), before) or capture(state, base, mesh, constants, selection)["scope_sha256"] != before_scope["scope_sha256"]:
            raise RuntimeError("model changed during backup; freeze API not called")
        ids = before_scope["freeze_targets"]["FACE"] if clean["entity_type"] == "FACE" else clean["entity_ids"]
        entities = [catalog._get_entity(base, clean["deck"], clean["entity_type"], ident)[0] for ident in ids]
        result.update(api_called=True, api="ansa.base." + api_name, no_op=False)
        error = None
        try:
            returned = (api(entities, unfreeze=clean["action"] == "unfreeze")
                        if clean["entity_type"] == "FACE" else api(clean["deck"], clean["entity_type"], entities))
            result["api_return"] = catalog._json_value(returned, base, clean["deck"])
            if clean["entity_type"] == "FACE":
                if type(returned) is not int or returned <= 0:
                    raise RuntimeError("FacesFreezeUnFreeze returned an invalid result")
            elif returned is not True:
                raise RuntimeError(api_name + " did not return True")
        except Exception as exc:
            error = native._error_text(exc)
            result["native_error"] = error
        # Observe after exceptions too, but never attribute another model's entities.
        guards._same_context(state, clean["deck"], context)
        after = _guard(base, clean["deck"])
        result["protection_after"] = {"entity_count": len(after), "sha256": native._hash(
            [after[key] for key in sorted(after)])}
        after_scope = capture(state, base, mesh, constants, selection)
        result["scope_sha256_after"] = after_scope["scope_sha256"]
        result["changes"] = _verify_guard(before, after, targets, desired)
        _verify_relations(before_scope, after_scope, targets, desired)
        result["scope_verified"] = True
        if error:
            raise RuntimeError(error)
        result["ok"] = True
        return result
    except Exception as exc:
        result.update(error=native._error_text(exc), possible_effects=result["api_called"] or result["backup_attempted"])
        result["recovery_required"] = result["possible_effects"]
        if context is not None and result["possible_effects"]:
            states, errors = _observe_targets(state, base, clean["deck"], context, targets)
            result["observed_target_states"] = states
            if errors:
                result["target_observation_errors"] = errors
            try:
                guards._same_context(state, clean["deck"], context)
                observation = capture(state, base, mesh, constants, selection)
                result["observed_scope_sha256"] = observation["scope_sha256"]
                result["observed_freeze_states"] = {section: [{"entity": row["entity"], "freeze_state": row["freeze_state"]}
                    for row in observation[section]] for section in ("faces", "cons", "shells", "solids")}
            except Exception as observation_error:
                result["observation_error"] = native._error_text(observation_error)
        return result


def validate_report(state, base, mesh, constants, deck, result):
    current = capture(state, base, mesh, constants, result["selection"])
    if deck != result["selection"]["deck"] or current["scope_sha256"] != result.get("scope_sha256"):
        raise ValueError("mesh scope evidence is stale")


def execute(state, tool, params):
    state.assert_main()
    from ansa import base, mesh, constants
    if tool == "mesh_scope_read":
        result = capture(state, base, mesh, constants, params)
        if len(evidence._encoded(result)) <= 192 * 1024:
            return result
        descriptor = evidence.store_report(state, params["deck"], tool, result)
        descriptor.update(read_only=True, scope_sha256=result["scope_sha256"], selection=result["selection"],
                          freeze_targets=result["freeze_targets"], freeze_effects=result["freeze_effects"])
        return descriptor
    if tool != "mesh_freeze":
        raise ValueError("unsupported mesh scope operation")
    result = freeze(state, base, mesh, constants, params)
    from .structured_meshing import _bounded_operation_result
    return _bounded_operation_result(state, params["deck"], result)
