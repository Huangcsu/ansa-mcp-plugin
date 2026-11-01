"""Explicit reuse of existing CAD perimeter points, with unchanged legacy seeds."""

import copy
import math
import time

from . import cons_seed as seeds, entity_catalog as catalog, geometry_features as features
from . import inspection, mesh_scope, meshing_context, structured_native as native
from . import volume_meshing as guards


_TOLERANCE = seeds._PROJECTION_TOLERANCE
_BIAS_FIELDS = ("Biasing", "Bias. Start Factor", "Bias. End Factor", "Bias. Steps",
                "Bias. Grids U Parameters")
_ALIGN_FIELDS = {"Selection_Source": "Perimeters", "Align_using": "Source CONS",
                 "Align_Perform_Cuts": False, "Align_Insert_HotPoints": "parametrically",
                 "Freeze source Cons": False, "auto_remesh": False}


def _ids(value, label):
    result = features._ids(value, 32, label)
    if not result:
        raise ValueError(label + " must not be empty")
    return result


def normalize(params):
    inspection._params(params, ("deck", "face_ids", "edit", "expected_seed_snapshot_sha256", "backup_path"))
    deck = native._positive_int(params["deck"], "deck")
    faces = features._ids(params["face_ids"], 20, "face_ids")
    if not faces:
        raise ValueError("explicit FACE scope must not be empty")
    edit = params["edit"]
    if type(edit) is not dict:
        raise ValueError("edit must be an explicit object")
    action = edit.get("action")
    fields = {"number": ("count_type", "assignments"), "length": ("cons_ids", "target_length"),
              "align": ("source_cons_id", "source_start_hot_point_id", "targets")}
    if type(action) is not str or action not in fields:
        raise ValueError("edit.action must be number, length or align")
    inspection._params(edit, ("action", "expected_scope_sha256") + fields[action])
    edit = copy.deepcopy(edit)
    native._validate_digest(edit["expected_scope_sha256"], "expected_scope_sha256")
    if action == "number":
        if edit["count_type"] not in ("edges", "nodes"):
            raise ValueError("count_type must be edges or nodes")
        rows = edit["assignments"]
        if type(rows) is not list or not 1 <= len(rows) <= 32:
            raise ValueError("number assignments require 1..32 explicit CONS")
        for row in rows:
            inspection._params(row, ("cons_id", "count"))
            native._positive_int(row["cons_id"], "cons_id")
            count = native._positive_int(row["count"], "count")
            if count > 10000 or edit["count_type"] == "nodes" and count < 2:
                raise ValueError("count exceeds budget or total node count is below two")
        targets = _ids([r["cons_id"] for r in rows], "assignments CONS")
        edit["assignments"] = sorted(rows, key=lambda row: row["cons_id"])
    elif action == "length":
        targets = _ids(edit["cons_ids"], "cons_ids")
        edit["cons_ids"] = targets
        length = features._number(edit["target_length"], "target_length", True)
        edit["target_length"] = length
    else:
        source = native._positive_int(edit["source_cons_id"], "source_cons_id")
        native._positive_int(edit["source_start_hot_point_id"], "source_start_hot_point_id")
        rows = edit["targets"]
        if type(rows) is not list or not 1 <= len(rows) <= 31:
            raise ValueError("align requires 1..31 target CONS plus one source")
        for row in rows:
            inspection._params(row, ("cons_id", "start_hot_point_id"))
            native._positive_int(row["cons_id"], "cons_id")
            native._positive_int(row["start_hot_point_id"], "start_hot_point_id")
        targets = _ids([r["cons_id"] for r in rows], "target CONS")
        if source in targets:
            raise ValueError("source CONS must not also be a target")
        edit["targets"] = sorted(rows, key=lambda row: row["cons_id"])
    selected = sorted(targets + ([edit["source_cons_id"]] if action == "align" else []))
    if len(selected) > 32:
        raise ValueError("at most 32 target/source CONS are supported")
    native._validate_digest(params["expected_seed_snapshot_sha256"], "expected_seed_snapshot_sha256")
    if type(params["backup_path"]) is not str or not params["backup_path"]:
        raise ValueError("backup_path must be a new absolute .ansa path")
    return {**params, "deck": deck, "face_ids": faces, "edit": edit,
            "target_ids": targets, "selected_ids": selected}


def _selection(clean):
    return {"deck": clean["deck"], "entity_type": "CONS", "entity_ids": clean["selected_ids"]}


def _relations(scope):
    return {"context": scope["context"], "selection": scope["selection"],
            "faces": [{key: row[key] for key in ("macro_id", "cons_ids", "volume_ids")} |
                      {"id": row["entity"]["id"]} for row in scope["faces"]],
            "macros": scope["macros"],
            "cons": [{"id": row["entity"]["id"], "face_ids": row["face_ids"],
                      "unexpanded_face_ids": row["unexpanded_face_ids"]} for row in scope["cons"]],
            "volumes": [{"id": row["entity"]["id"], **{key: row[key] for key in
                         ("face_ids", "shell_ids", "solid_ids")}} for row in scope["volumes"]]}


def _direction(row, start):
    points = {v["id"]: v["xyz"] for v in row["hot_points"]}
    if start not in points or len(points) != 2:
        raise ValueError("declared start HOT POINT must be a native endpoint of its CONS")
    end = next(i for i in points if i != start)
    vector = [b-a for a, b in zip(points[start], points[end])]
    length = math.sqrt(sum(x*x for x in vector))
    if length <= _TOLERANCE:
        raise ValueError("alignment requires distinct endpoints")
    return points[start], [x/length for x in vector], length


def _fractions(row, start):
    origin, direction, length = _direction(row, start)
    result = []
    for point in row["points"]:
        delta = [v-a for v, a in zip(point["xyz"], origin)]
        along = sum(a*b for a, b in zip(delta, direction))
        if math.dist(delta, [along*v for v in direction]) > _TOLERANCE:
            raise ValueError("alignment currently requires straight perimeter point distributions")
        result.append(along/length)
    result.sort()
    if (not result or abs(result[0]) > 1e-9 or abs(result[-1]-1) > 1e-9
            or any(b-a <= 1e-12 for a, b in zip(result, result[1:]))):
        raise ValueError("alignment distribution is incomplete or not strictly ordered")
    return result


def _expected(clean, witness):
    edit = clean["edit"]
    if edit["action"] == "number":
        return {r["cons_id"]: r["count"]-(edit["count_type"] == "nodes") for r in edit["assignments"]}
    if edit["action"] == "align":
        source = next(r for r in witness["boundaries"] if r["cons_id"] == edit["source_cons_id"])
        return dict.fromkeys(clean["target_ids"], source["segments"])
    lengths = {r["id"]: r["native_card_length"] for r in witness["geometry_witness"]["boundaries"]}
    result = {}
    for ident in clean["target_ids"]:
        estimate = lengths[ident]/edit["target_length"]
        if not math.isfinite(estimate) or estimate > 9998:
            raise ValueError("target length exceeds the perimeter point budget")
        # A reservation, not a claim about ANSA's curvature/discretization rule.
        result[ident] = max(1, math.ceil(estimate)+1)
    return result


def _preflight(state, base, mesh, constants, clean, result):
    deck = clean["deck"]
    native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    scope = mesh_scope.capture(state, base, mesh, constants, _selection(clean))
    if scope["scope_sha256"] != clean["edit"]["expected_scope_sha256"]:
        raise ValueError("mesh scope changed; obtain fresh mesh_scope_read for all target/source CONS")
    witness = seeds.capture(state, base, deck, clean["face_ids"], clean["selected_ids"])
    if witness["seed_snapshot_sha256"] != clean["expected_seed_snapshot_sha256"]:
        raise ValueError("native seed source changed; obtain fresh mesh_seed_read")
    primary = [r for r in scope["macros"] if r["primary"]]
    if not primary or any(not set(r["face_ids"]) <= set(clean["face_ids"]) for r in primary):
        raise ValueError("face_ids must contain the complete affected Macro FACE closure")
    blocked = sorted({i for r in primary for i in r["shell_ids"]})
    if blocked:
        result["blocked_macro_shell_ids"] = blocked
        result["affected_macro_ids"] = sorted(r["macro_id"] for r in primary)
        raise ValueError("affected Macros have existing mesh; explicitly erase the complete affected mesh before boundary editing")
    primary_faces = {i for r in primary for i in r["face_ids"]}
    if any(v["solid_ids"] and set(v["face_ids"]) & primary_faces for v in scope["volumes"]):
        raise ValueError("affected CAD belongs to meshed VOLUME; local solid rework is not supported")
    rows = {r["cons_id"]: r for r in witness["boundaries"]}
    relations = {r["entity"]["id"]: r for r in scope["cons"]}
    for ident in clean["selected_ids"]:
        row = rows[ident]
        if row["status"] != "supported" or not set(row["face_ids"]) <= set(clean["face_ids"]):
            raise ValueError("requires first-order open CONS and all adjacent FACE in the declared scope")
        if relations[ident]["effective_macro_protection"]["value"] is not False:
            raise ValueError("CONS is frozen, protected by an adjacent Macro, or protection is unknown")
        fields = row["native_fields"]
        if fields["Collapsed"] != "NO" or fields["Frozen"] != "NO":
            raise ValueError("perimeter editing requires uncollapsed, unfrozen CONS")
        if clean["edit"]["action"] != "align" and fields["Biasing"] != "LINEAR":
            raise ValueError("number/length editing currently requires LINEAR perimeter distributions")
    if clean["edit"]["action"] == "align":
        edit = clean["edit"]
        _, vector, length = _direction(rows[edit["source_cons_id"]], edit["source_start_hot_point_id"])
        for target in edit["targets"]:
            _, direction, _ = _direction(rows[target["cons_id"]], target["start_hot_point_id"])
            if sum(a*b for a, b in zip(vector, direction)) < 1-1e-10:
                raise ValueError("alignment currently requires declared, parallel, same-direction endpoint pairs")
    requested = _expected(clean, witness)
    if sum(v+1 for v in requested.values()) > seeds._MAX_POINTS:
        raise ValueError("requested perimeter points exceed 10000")
    api_name = {"number": "NumberPerimeters", "length": "ApplyNewLengthToMacros", "align": "PerimetersSpacingAlign"}[clean["edit"]["action"]]
    inspection._require_api(mesh, api_name, "ansa.mesh." + api_name)
    before = seeds._guard_snapshot(state, base, deck)
    seeds._point_budget(base, deck, seeds._collect(base, deck, None, "CONS"),
                        len(before["entities"]), len(before["grids"]), requested)
    backup = native._backup_preflight(base, clean)
    guards._same_context(state, deck, context)
    return {"context": context, "scope": scope, "witness": witness, "before": before,
            "requested": requested, "backup": backup}


def _set_nodes(base, deck, snapshot):
    result, total = {}, 0
    for row in snapshot["entities"]:
        if row["entity"]["type"] != "SET":
            continue
        ident = row["entity"]["id"]
        entity = features._entity(base, deck, "SET", ident)
        values = mesh_scope._entities(base, deck, base.CollectEntities(
            deck, entity, "GRID", True, filter_visible=False, hidden_entities=True), "GRID", "SET GRID members")
        total += len(values)
        if total > 200000:
            raise ValueError("SET node membership exceeds the protection budget")
        result[ident] = sorted(values)
    return result


def _virtual_definition(row):
    """Only disposable perimeter points may lose their original GRID identity."""
    card = row.get("card", {})
    fields = card.get("fields", {})
    labels = card.get("card_fields", [])
    for key in ("CP", "CD", "PS", "SEID"):
        if key not in labels or key not in fields:
            raise ValueError("GRID engineering definition is incomplete: " + key)
        value = fields[key]
        if value is not None and not (type(value) is str and value in ("", "0")) and not (
                type(value) in (int, float) and value == 0):
            raise ValueError("perimeter GRID carries a coordinate system, permanent constraint or superelement: " + key)
    defaults = {"TYPE": "GRID", "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
                "DEFINED": "YES", "AUXILIARY": "NO"}
    for key, value in fields.items():
        if key in {"NID", "CP", "CD", "PS", "SEID", "X1", "X2", "X3"}:
            continue
        if key in defaults:
            if value != defaults[key]:
                raise ValueError("perimeter GRID has a protected definition flag: " + key)
        elif value not in (None, "", [], 0):
            raise ValueError("perimeter GRID has non-default metadata or membership: " + key)


def _materialize(state, base, clean, prepared, result):
    deck = clean["deck"]
    original = prepared["before"]
    result["materialization_attempted"] = True
    initialized = seeds._guard_snapshot(state, base, deck, materialize=True, budget_base=original)
    old_grids = {r["id"]: r for r in original["grids"]}
    new_grids = {r["id"]: r for r in initialized["grids"]}
    added = set(new_grids)-old_grids.keys()
    result["new_virtual_point_ids"] = sorted(added)
    if any(new_grids.get(i) != row for i, row in old_grids.items()):
        raise RuntimeError("perimeter materialization changed an existing GRID")
    owned = {i for ids in initialized["perimeters"].values() for i in ids}
    original_rows = [r for r in initialized["entities"]
                     if not (r["entity"]["type"] == "GRID" and r["entity"]["id"] in added)]
    if (not added <= owned or original_rows != original["entities"]
            or initialized["cad_sha256"] != original["cad_sha256"]):
        raise RuntimeError("materialization changed original definitions, topology or CAD")
    boundaries = [seeds._boundary(base, deck, features._entity(base, deck, "CONS", i), include_points=True)
                  for i in clean["selected_ids"]]
    if any(r["status"] != "supported" for r in boundaries):
        raise ValueError("native perimeter points disagree with their cards or endpoints")
    fixed = {i for r in boundaries for i in r["endpoint_grid_ids"]}
    fixed.update(i for ident, ids in initialized["perimeters"].items() if ident not in clean["target_ids"] for i in ids)
    target_interiors = {p["id"] for row in boundaries if row["cons_id"] in clean["target_ids"]
                        for p in row["points"] if p["id"] not in row["endpoint_grid_ids"]}
    if target_interiors & fixed:
        result["blocked_shared_grid_ids"] = sorted(target_interiors & fixed)
        raise ValueError("target interior GRID are shared with protected perimeters or endpoints")
    candidate = {i for ident in clean["target_ids"] for i in initialized["perimeters"][ident]}-fixed
    for ident in sorted(candidate):
        try:
            _virtual_definition(new_grids[ident])
        except ValueError:
            result.setdefault("blocked_defined_grid_ids", []).append(ident)
            raise
    nodes = [features._entity(base, deck, "GRID", i) for i in sorted(candidate)]
    attached = base.NodesToElements(nodes, sources=["entities", "bcs"]) if nodes else {}
    if type(attached) is not dict:
        raise RuntimeError("node attachment map is unavailable")
    seen, referenced = set(), set()
    for node, entities in attached.items():
        ident = catalog._entity_reference(base, deck, node, "GRID")["id"]
        if ident not in candidate or ident in seen or type(entities) not in (list, tuple):
            raise RuntimeError("node attachment map is incomplete or ambiguous")
        seen.add(ident)
        if entities:
            referenced.add(ident)
    if seen != candidate:
        raise RuntimeError("node attachment map omits candidate perimeter points")
    set_nodes = _set_nodes(base, deck, initialized)
    referenced.update(candidate & {i for values in set_nodes.values() for i in values})
    if referenced:
        result["blocked_referenced_grid_ids"] = sorted(referenced)
        raise ValueError("perimeter interior GRID have solver/BC/SET references; explicit rework is required")
    result["editable_interior_grid_ids"] = sorted(candidate)
    if clean["edit"]["action"] == "align":
        rows = {r["cons_id"]: r for r in boundaries}
        edit = clean["edit"]
        distribution = _fractions(rows[edit["source_cons_id"]], edit["source_start_hot_point_id"])
        for target in edit["targets"]:
            _fractions(rows[target["cons_id"]], target["start_hot_point_id"])
        result["source_distribution"] = distribution
    return {**prepared, "before": initialized, "protected_grid_ids": sorted(set(new_grids)-candidate),
            "boundaries": boundaries, "set_nodes": set_nodes,
            "witness": {**prepared["witness"], "boundaries": boundaries}}


def _invoke(state, base, mesh, clean, prepared, result):
    edit = clean["edit"]
    deck = clean["deck"]
    def call(name, ids, args, kwargs, success):
        guards._same_context(state, deck, prepared["context"])
        record = {"api": name, "cons_ids": ids, "arguments": args, "options": kwargs}
        result["native_calls"].append(record)
        result["api_called"] = True
        entities = [features._entity(base, deck, "CONS", i) for i in ids]
        if name == "NumberPerimeters":
            value = mesh.NumberPerimeters(entities, args[0], **kwargs)
        elif name == "ApplyNewLengthToMacros":
            value = mesh.ApplyNewLengthToMacros(args[0], entities, **kwargs)
        else:
            source = features._entity(base, deck, "CONS", edit["source_cons_id"])
            value = mesh.PerimetersSpacingAlign(entities, [source], **kwargs)
        record["return_code"] = catalog._json_value(value, base, deck)
        if type(value) is not int or value != success:
            raise RuntimeError(name + " returned an unexpected status")
    if edit["action"] == "number":
        groups = {}
        for ident, segments in prepared["requested"].items():
            groups.setdefault(segments, []).append(ident)
        for segments, ids in sorted(groups.items()):
            # API 'nodes' means internal points on 2026.1. Public 'nodes' means
            # the complete open perimeter including both endpoints.
            call("NumberPerimeters", ids, [str(segments)], {"apply_multiple_number": False,
                 "remesh_macros": False, "apply_number_of": "edges", "use_ansa_defaults_values": False}, 1)
    elif edit["action"] == "length":
        call("ApplyNewLengthToMacros", clean["target_ids"], [format(edit["target_length"], ".17g")],
             {"use_ansa_defaults_values": False}, 0)
    else:
        call("PerimetersSpacingAlign", clean["selected_ids"], [], {"fields": dict(_ALIGN_FIELDS)}, 0)


def _verify(state, base, calc, clean, prepared, result):
    deck = clean["deck"]
    guards._same_context(state, deck, prepared["context"])
    readback = seeds.capture(state, base, deck, clean["face_ids"], clean["selected_ids"])
    result["readback"] = readback
    result["seed_after_sha256"] = readback["seed_snapshot_sha256"]
    # Read the actual new card counts and check budgets before collecting GRID.
    after = seeds._guard_snapshot(state, base, deck, materialize=True, budget_base=prepared["before"])
    points = [seeds._boundary(base, deck, features._entity(base, deck, "CONS", i), include_points=True)
              for i in clean["selected_ids"]]
    result["boundary_point_readback"] = points
    if any(r["status"] != "supported" for r in points):
        raise RuntimeError("final perimeter point counts or endpoints are inconsistent")
    rows = {r["cons_id"]: r for r in points}
    old_rows = {r["cons_id"]: r for r in prepared["boundaries"]}
    for ident in clean["selected_ids"]:
        if rows[ident]["endpoint_grid_ids"] != old_rows[ident]["endpoint_grid_ids"]:
            raise RuntimeError("perimeter endpoint GRID identity changed")
    grid_rows = {row["id"]: row for row in after["grids"]}
    for ident in clean["target_ids"]:
        for point in rows[ident]["points"]:
            if point["id"] not in rows[ident]["endpoint_grid_ids"]:
                _virtual_definition(grid_rows[point["id"]])
    if clean["edit"]["action"] != "length":
        if any(rows[i]["segments"] != count for i, count in prepared["requested"].items()):
            raise RuntimeError("native edge/total-node count readback differs from the request")
    else:
        for ident in clean["target_ids"]:
            fields = seeds._card(base, deck, features._entity(base, deck, "CONS", ident))["fields"]
            if fields.get("Elem.Length") != clean["edit"]["target_length"]:
                raise RuntimeError("native perimeter target length was not applied")
    if clean["edit"]["action"] == "align":
        edit = clean["edit"]
        source = _fractions(rows[edit["source_cons_id"]], edit["source_start_hot_point_id"])
        if source != result["source_distribution"]:
            raise RuntimeError("alignment changed its reference distribution")
        aligned = []
        for target in edit["targets"]:
            observed = _fractions(rows[target["cons_id"]], target["start_hot_point_id"])
            if len(observed) != len(source) or any(abs(a-b) > 1e-9 for a, b in zip(source, observed)):
                raise RuntimeError("aligned node distribution differs in the declared direction")
            aligned.append({"cons_id": target["cons_id"], "start_hot_point_id": target["start_hot_point_id"],
                            "normalized_positions": observed})
        result["aligned_distributions"] = aligned
    if _set_nodes(base, deck, after) != prepared["set_nodes"]:
        raise RuntimeError("SET node membership changed during perimeter editing")
    # Reuse the legacy complete CAD/FE/perimeter audit, with only the explicit
    # unreferenced interior points editable. Its legacy callers remain intact.
    audit = copy.deepcopy(prepared)
    adjusted = copy.deepcopy(after)
    audit["assignments"] = [{"cons_id": i, "segments": rows[i]["segments"]} for i in clean["target_ids"]]
    if clean["edit"]["action"] in {"length", "align"}:
        allowed = ("Elem.Length",) if clean["edit"]["action"] == "length" else _BIAS_FIELDS
        for snapshot in (audit["before"], adjusted):
            for row in snapshot["entities"]:
                if row["entity"]["type"] == "CONS" and row["entity"]["id"] in clean["target_ids"]:
                    for field in allowed:
                        row["fields"].pop(field, None)
    errors, changes = seeds._audit(audit, adjusted, readback)
    if errors:
        raise RuntimeError("; ".join(errors))
    from .geometry_fidelity import _projection_points
    for ident in clean["target_ids"]:
        edge = features._entity(base, deck, "CONS", ident)
        distances = []
        for point in rows[ident]["points"]:
            _, projected, _ = _projection_points(calc.ProjectPointToCons(point["xyz"], edge))
            distances.append(math.dist(point["xyz"], projected))
        if not distances or any(not math.isfinite(v) or v > _TOLERANCE for v in distances):
            raise RuntimeError("perimeter points failed independent CAD projection")
        result.setdefault("point_projections", []).append({"cons_id": ident, "point_count": len(distances),
            "maximum_observed_distance": max(distances), "tolerance": _TOLERANCE})
    result.update(source_geometry_unchanged=True, before_scope_sha256=features._hash(prepared["before"]),
                  after_scope_sha256=features._hash(after), tessellation_changes=changes)
    guards._same_context(state, deck, prepared["context"])


def apply(state, base, mesh, calc, constants, params):
    state.assert_main()
    result = {"ok": False, "tool": "mesh_seed_edit", "api_called": False, "backup_attempted": False,
              "materialization_attempted": False, "scope_verified": False, "configuration_unchanged": False,
              "possible_effects": False, "recovery_required": False, "native_calls": [], "timings_ms": {}}
    clean = prepared = configuration = None
    started = time.monotonic()
    try:
        clean = normalize(params)
        result.update(deck=clean["deck"], edit=clean["edit"], target_cons_ids=clean["target_ids"])
        prepared = _preflight(state, base, mesh, constants, clean, result)
        configuration = meshing_context.capture(state, base, mesh)
        result["configuration_before"] = configuration
        result["timings_ms"]["preflight"] = (time.monotonic()-started)*1000
        phase = time.monotonic()
        result["backup_attempted"] = True
        result["backup"] = native._verify_backup(base, constants, *prepared["backup"])
        guards._same_context(state, clean["deck"], prepared["context"])
        if seeds._guard_snapshot(state, base, clean["deck"]) != prepared["before"]:
            raise RuntimeError("model changed during backup; no perimeter operation called")
        if (mesh_scope.capture(state, base, mesh, constants, _selection(clean))["scope_sha256"]
                != clean["edit"]["expected_scope_sha256"]):
            raise RuntimeError("relationships changed during backup")
        result["timings_ms"]["backup"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        prepared = _materialize(state, base, clean, prepared, result)
        result["timings_ms"]["materialization"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        operation_error = None
        try:
            _invoke(state, base, mesh, clean, prepared, result)
        except Exception as exc:
            operation_error = native._error_text(exc)
            result["native_error"] = operation_error
        result["timings_ms"]["native"] = (time.monotonic()-phase)*1000
        phase = time.monotonic()
        _verify(state, base, calc, clean, prepared, result)
        scope = mesh_scope.capture(state, base, mesh, constants, _selection(clean))
        if _relations(scope) != _relations(prepared["scope"]):
            raise RuntimeError("Macro, CONS or Volume membership changed during perimeter editing")
        result["relations_verified"] = True
        result["scope_sha256_after"] = scope["scope_sha256"]
        result["scope_verified"] = True
        result["timings_ms"]["verification"] = (time.monotonic()-phase)*1000
        if operation_error:
            raise RuntimeError(operation_error)
        result["ok"] = True
    except Exception as exc:
        result["error"] = native._error_text(exc)
    finally:
        if configuration is not None:
            try:
                guards._same_context(state, clean["deck"], prepared["context"])
                after_config = meshing_context.capture(state, base, mesh)
                result["configuration_after"] = after_config
                result["configuration_unchanged"] = after_config["snapshot_sha256"] == configuration["snapshot_sha256"]
                if not result["configuration_unchanged"]:
                    raise RuntimeError("global mesh/quality/F11 configuration changed; recovery snapshots retained")
            except Exception as exc:
                result["ok"] = False
                result["configuration_error"] = native._error_text(exc)
                result["error"] = result.get("error", result["configuration_error"])
        if not result["ok"] and prepared is not None and clean is not None:
            observed = []
            for ident in clean["selected_ids"]:
                try:
                    guards._same_context(state, clean["deck"], prepared["context"])
                    observed.append(seeds._boundary(base, clean["deck"], features._entity(base, clean["deck"], "CONS", ident)))
                except Exception as exc:
                    result.setdefault("observation_errors", []).append({"cons_id": ident, "error": native._error_text(exc)})
            result["observed_boundaries"] = observed
        result["possible_effects"] = result["backup_attempted"] or result["materialization_attempted"] or result["api_called"]
        result["recovery_required"] = not result["ok"] and result["possible_effects"]
        result["original_operation_must_be_queried"] = result["recovery_required"]
        result["timings_ms"]["total"] = (time.monotonic()-started)*1000
    return result


def execute(state, tool, params):
    state.assert_main()
    if tool != "mesh_seed_edit":
        raise ValueError("unsupported perimeter editing operation")
    from ansa import base, mesh, calc, constants
    from .structured_meshing import _bounded_operation_result
    return _bounded_operation_result(state, params.get("deck"), apply(state, base, mesh, calc, constants, params))
