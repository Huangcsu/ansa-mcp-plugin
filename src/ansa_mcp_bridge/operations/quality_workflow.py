"""Analysis-oriented quality reports and bounded, independently checked repairs."""

import hashlib
import json
import math
import re

from ansa_mcp_shared.mesh_quality import evaluate, normalize_profile
from . import entity_catalog as catalog, inspection


_MAX_SCOPE = 2000  # Native work batch / display scope, not a total inspection limit.
_MAX_MODEL_FE = 50000  # Switch to disk evidence above this in-memory threshold.
_MAX_RESULT_BYTES = 256 * 1024  # Leave headroom for task and TCP envelopes.
_METRICS = {
    "ASPECT": "aspect ratio", "WARP": "warping", "SKEW": "skewness",
    "JACOB": "jacobian", "MINL": "min length", "MAXL": "max length",
    "CRASH": "crash time step", "TETRA_COLLAPSE": "collapse",
}
_LOWER = {"JACOB", "MINL", "CRASH", "TETRA_COLLAPSE"}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _report_summary(report):
    """Summarize full decisions before detail rows are trimmed for transport."""
    groups = {}
    for row in report["rows"]:
        for name in set(row.get("violations", ())):
            groups.setdefault(name, set()).add(row["id"])
    failed = set(report["failed_entity_ids"])
    unknown = set(report.get("unknown_entity_ids", ()))
    report["summary"] = {
        "status": "incomplete" if not report["complete"] else "failed" if failed or unknown else "passed",
        "checked_count": len(report["rows"]),
        "failed_count": len(failed),
        "unknown_count": len(unknown),
        "passed_count": len(report["rows"]) - len(failed | unknown),
    }
    report["violation_summary"] = [
        {"criterion": name, "failed_count": len(entity_ids)}
        for name, entity_ids in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def _bounded_result(result):
    """Keep full decisions/IDs, and explicitly limit only per-element details."""
    reports = []
    seen = set()
    def collect(value):
        if not isinstance(value, dict) or id(value) in seen:
            return
        seen.add(id(value))
        if "rows" in value and "failed_entity_ids" in value:
            _report_summary(value)
            value.update(rows_total=len(value["rows"]), rows_returned=len(value["rows"]), rows_truncated=False)
            reports.append(value)
        for key in ("before", "after", "report"):
            collect(value.get(key))
    collect(result)
    encoded = _encoded(result)
    if len(encoded) <= _MAX_RESULT_BYTES:
        return result
    # JSON serializes shared before/after objects twice. Break aliases before
    # allocating the detail budget so it reflects occurrences on the wire.
    result = json.loads(encoded)
    reports.clear()
    seen.clear()
    collect(result)
    originals = [r["rows"] for r in reports]
    for report in reports:
        report.update(rows=[], rows_returned=0, rows_truncated=True)
    remaining = _MAX_RESULT_BYTES - len(_encoded(result)) - 1024
    if remaining < 0 or not reports:
        raise RuntimeError("quality result summary exceeds its transport budget; use a smaller explicit scope")
    share = remaining // len(reports)
    for report, rows in zip(reports, originals):
        used = 0
        for row in rows:
            size = len(_encoded(row)) + 1
            if used + size > share:
                break
            report["rows"].append(row)
            used += size
        report["rows_returned"] = len(report["rows"])
        report["rows_truncated"] = report["rows_returned"] < report["rows_total"]
    if len(_encoded(result)) > _MAX_RESULT_BYTES:
        raise RuntimeError("quality result exceeds its transport budget after limiting details")
    return result


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError("expected a finite numeric value")
    return float(value)


def _card(base, deck, entity):
    ref = catalog._entity_reference(base, deck, entity)
    labels = catalog._entity_card_labels(entity, deck)
    if not labels or len(labels) > 256:
        raise ValueError("entity card is empty or exceeds the quality snapshot limit")
    fields = catalog._read_card(base, deck, entity, ref, sorted(labels))
    def complete(value):
        if isinstance(value, dict):
            return value.get("truncated") is not True and all(complete(v) for v in value.values())
        if isinstance(value, list):
            return all(complete(v) for v in value)
        return True
    if not complete(fields):
        raise ValueError("quality snapshot refuses truncated card values")
    return {"entity": ref, "fields": fields}


def _collect(base, deck, container, kind):
    result = base.CollectEntities(deck, container, kind)
    if type(result) not in (list, tuple):
        raise RuntimeError("CollectEntities did not return a complete entity list")
    return list(result)


def _position(node):
    point = node.position
    if type(point) not in (tuple, list) or len(point) != 3:
        raise RuntimeError("node global position is unavailable")
    return [_number(x) for x in point]


def _element_kind(constants, deck, family):
    if deck == getattr(constants, "LSDYNA", None):
        return "ELEMENT_" + family.upper()
    return family.upper()


def _scope(base, constants, deck, family, ids):
    kind = _element_kind(constants, deck, family)
    node_kind = "GRID" if deck == constants.NASTRAN else "NODE"
    entities, rows, nodes = [], [], {}
    for eid in ids:
        entity, _ = catalog._get_entity(base, deck, kind, eid)
        attached = _collect(base, deck, entity, node_kind)
        if not attached:
            raise ValueError(f"{kind} {eid} has no independently resolved nodes")
        for node in attached:
            ref = catalog._entity_reference(base, deck, node, node_kind)
            nodes[ref["id"]] = node
        row = _card(base, deck, entity)
        row["node_ids"] = sorted(n._id for n in attached)
        rows.append(row)
        entities.append(entity)
    positions = {str(eid): _position(node) for eid, node in sorted(nodes.items())}
    cards = {str(eid): _card(base, deck, node) for eid, node in sorted(nodes.items())}
    payload = {"deck": deck, "family": family, "elements": rows,
               "node_positions": positions, "node_cards": cards}
    return entities, nodes, payload, _digest(payload)


def _criteria(base):
    check = base.checks.mesh.MeshQuality()
    params = check.parameters()
    if type(params) is not dict or not isinstance(params.get("quality_criteria"), str) or not params["quality_criteria"]:
        raise inspection.CapabilityUnavailableError("native quality criteria text is unavailable")
    return params["quality_criteria"]


def _definitions(base, profile):
    getter = base.F11ShellsOptionsGet if profile["element_family"] == "shell" else base.F11SolidsOptionsGet
    names = {x["name"] for x in profile["criteria"]}
    if profile["minimum_length"] is not None:
        names.add("MINL")
    result = {}
    for name in sorted(names):
        try:
            value = getter(_METRICS[name])
            if type(value) is not dict or "calculation" not in value:
                raise ValueError("calculation method is unavailable")
            result[name] = {"native_name": _METRICS[name], "calculation": value["calculation"],
                            "source": "current_session", "complete": True}
        except Exception as exc:
            result[name] = {"native_name": _METRICS[name], "complete": False,
                            "error": f"{type(exc).__name__}: {exc}"[:512]}
    return result


def _measure(base, entities, profile, definitions):
    names = [x["name"] for x in profile["criteria"]]
    if profile["minimum_length"] is not None and "MINL" not in names:
        names.append("MINL")
    rows = []
    for entity in entities:
        values, errors = {}, {}
        lengths = None
        for name in names:
            try:
                if name in {"MINL", "MAXL"}:
                    if lengths is None:
                        lengths = base.CalculateAverageMinMaxElementLength([entity])
                    if type(lengths) is not dict:
                        raise ValueError("native element lengths are unavailable")
                    value = lengths["min" if name == "MINL" else "max"]
                else:
                    if not definitions[name]["complete"]:
                        raise ValueError("native metric calculation is not confirmed")
                    token = "COLLAPSE" if name == "TETRA_COLLAPSE" else name
                    value = base.ElementQuality(entity, [token])
                    if type(value) not in (tuple, list) or len(value) != 1:
                        raise ValueError("ElementQuality did not return one explicit metric")
                    value = value[0]
                values[name] = _number(value)
            except Exception as exc:
                values[name] = None
                errors[name] = f"{type(exc).__name__}: {exc}"[:512]
        rows.append({"id": entity._id, "metrics": values, "metric_errors": errors})
    return rows


def _report(base, constants, deck, profile, ids):
    before_criteria = _criteria(base)
    entities, nodes, snapshot, fingerprint = _scope(base, constants, deck, profile["element_family"], ids)
    definitions = _definitions(base, profile)
    measured = _measure(base, entities, profile, definitions)
    decision = evaluate(profile, [{"id": r["id"], "metrics": r["metrics"]} for r in measured])
    by_id = {r["id"]: r for r in measured}
    element_by_id = {x["entity"]["id"]: x for x in snapshot["elements"]}
    for row in decision["rows"]:
        row.update(by_id[row["id"]])
        element = element_by_id[row["id"]]
        points = [snapshot["node_positions"][str(n)] for n in element["node_ids"]]
        row["node_ids"] = element["node_ids"]
        row["center"] = [sum(p[i] for p in points) / len(points) for i in range(3)]
        row["bounds"] = {"min": [min(p[i] for p in points) for i in range(3)],
                         "max": [max(p[i] for p in points) for i in range(3)]}
    if _scope(base, constants, deck, profile["element_family"], ids)[3] != fingerprint or _criteria(base) != before_criteria:
        raise RuntimeError("model scope or quality settings changed during read-only measurement")
    criteria_hash = _digest(before_criteria)
    measurement_hash = _digest({"values": [{"id": r["id"], "metrics": r["metrics"]} for r in measured],
                                "definitions": definitions})
    scope_hash = _digest({"geometry": fingerprint, "profile": profile["profile_sha256"],
                          "native_criteria": criteria_hash, "measurement": measurement_hash})
    decision.update({"deck": deck, "entity_type": _element_kind(constants, deck, profile["element_family"]),
                     "entity_ids": ids, "scope_sha256": scope_hash, "geometry_sha256": fingerprint, "profile": profile,
                     "definitions": definitions, "native_criteria_sha256": criteria_hash,
                     "measurement_sha256": measurement_hash,
                     "read_only": True, "scope": "explicit", "solver_verified": False,
                     "crash_time_step_is_estimate": profile["analysis_type"] == "explicit_crash"})
    return decision, entities, nodes, snapshot


def _custom_criteria(text, profile):
    """Build per-check criteria; never load or change the session's global settings."""
    bounds = {x["name"]: x for x in profile["criteria"]}
    if profile["minimum_length"] is not None:
        current = bounds.get("MINL", {})
        bounds["MINL"] = {**current, "name": "MINL", "min": max(current.get("min", 0), profile["minimum_length"])}
    wanted = {f"{_METRICS[name]} [{'shells' if profile['element_family']=='shell' else 'solids'}]": name for name in bounds}
    found, lines = set(), []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip()
        if re.search(r"\[(?:shells|solids)\]\s*=\s*(?:ON|OFF),", line):
            line = re.sub(r"=\s*(?:ON|OFF),", "= OFF,", line, count=1)
        if name in wanted:
            metric = wanted[name]
            key = "min" if metric in _LOWER else "max"
            if key not in bounds[metric] or len(set(bounds[metric]) - {"name", key}):
                raise ValueError(f"native repair requires a single {key} bound for {metric}")
            pieces = line.split("|", 2)
            if len(pieces) != 3:
                raise inspection.CapabilityUnavailableError("native quality criteria format is unsupported")
            line = pieces[0].replace("= OFF,", "= ON,", 1) + "|" + pieces[1] + "| "
            line += " ".join(f"{bounds[metric][key]:.17g}, -1./" for _ in range(4))
            found.add(name)
        lines.append(line)
    if found != set(wanted):
        raise inspection.CapabilityUnavailableError("native criteria do not expose all requested repair metrics")
    return "\n".join(lines)


def _fe_snapshot(base, constants, deck):
    node_kind = "GRID" if deck == constants.NASTRAN else "NODE"
    elements = _collect(base, deck, None, "__ELEMENTS__")
    nodes = _collect(base, deck, None, node_kind)
    if len(elements) + len(nodes) > _MAX_MODEL_FE:
        from .quality_snapshot import Evidence
        evidence = Evidence()
        for entity in sorted(elements, key=lambda e: (e.ansa_type(deck), e._id)):
            row = _card(base, deck, entity)
            row["node_ids"] = sorted(n._id for n in _collect(base, deck, entity, node_kind))
            evidence.add_element(row)
        for node in nodes:
            evidence.add_node(str(node._id), {"card": _card(base, deck, node), "position": _position(node)})
        return evidence.snapshot()
    element_cards = []
    for entity in elements:
        row = _card(base, deck, entity)
        row["node_ids"] = sorted(n._id for n in _collect(base, deck, entity, node_kind))
        element_cards.append(row)
    element_cards.sort(key=lambda r: (r["entity"]["type"], r["entity"]["id"]))
    return {"elements": element_cards,
            "nodes": {str(n._id): {"card": _card(base, deck, n), "position": _position(n)} for n in nodes}}


def _restore_positions(base, constants, deck, before, current):
    if before["elements"] != current["elements"] or set(before["nodes"]) != set(current["nodes"]):
        return False
    kind = "GRID" if deck == constants.NASTRAN else "NODE"
    for eid, row in before["nodes"].items():
        if current["nodes"][eid]["position"] != row["position"]:
            node, _ = catalog._get_entity(base, deck, kind, int(eid))
            node.position = tuple(row["position"])
    return _fe_snapshot(base, constants, deck) == before


def _validity(base, entities, family):
    result = []
    measure = base.CalcShellArea if family == "shell" else base.CalcSolidVolume
    for entity in entities:
        value = _number(measure(entity))
        jac = base.ElementQuality(entity, ["JACOB"])
        if type(jac) not in (list, tuple) or len(jac) != 1:
            raise inspection.CapabilityUnavailableError("repair validity Jacobian is unavailable")
        result.append({"id": entity._id, "measure": value, "jacobian": _number(jac[0])})
    return result


def _repair(state, base, constants, deck, params, profile, ids, before, entities, nodes):
    if len(ids) > 200:
        raise ValueError("repair accepts at most 200 explicit elements")
    allowed = (set(inspection._id_list(params["movable_node_ids"], "movable_node_ids", maximum=2000))
               if params.get("movable_node_ids") is not None else None)
    if allowed is not None and not allowed <= set(nodes):
        raise ValueError("movable nodes must belong to the explicit element scope")
    maximum = _number(params["maximum_displacement"])
    if maximum <= 0:
        raise ValueError("maximum_displacement must be positive")
    if not before["complete"]:
        raise ValueError("repair refuses unknown or unsupported quality metrics")
    native_before = _criteria(base)
    native_template = "template" in profile
    method = params.get("method") or ("fix_quality" if native_template else "smooth")
    if method not in {"fix_quality", "smooth"}:
        raise ValueError("method must be smooth or fix_quality")
    reports = []
    if method == "fix_quality":
        if native_template:
            from . import native_quality
            check, text, _ = native_quality.configure_check(base, profile, native_before)
        else:
            text = _custom_criteria(native_before, profile)
            check = base.checks.mesh.MeshQuality()
        if check.is_available_in_deck(deck) is not True:
            raise inspection.CapabilityUnavailableError("MeshQuality does not support the current deck")
        check.use_criteria = "Use custom criteria"
        check.quality_criteria = text
        reports = check.execute(exec_mode=base.Check.EXEC_ON_SELECTED, report=base.Check.REPORT_NONE,
                                history=base.Check.KEEP_OLD, entities=entities)
    else:
        from ansa import mesh
        api_name = "SmoothShells" if profile["element_family"] == "shell" else "SolidSmooth"
        smoother = inspection._require_api(mesh, api_name, "ansa.mesh." + api_name)
    if type(reports) is not list:
        raise RuntimeError("native quality check returned an invalid report list")
    def verify_report(row, depth=0):
        if depth > 12:
            raise RuntimeError("native repair report nesting exceeds the supported depth")
        for entity in row.entities:
            ref = catalog._entity_reference(base, deck, entity)
            if ref["type"] == before["entity_type"] and ref["id"] in ids:
                continue
            if ref["type"] in {"NODE", "GRID"} and ref["id"] in nodes:
                continue
            raise RuntimeError("native repair report includes an entity outside the selected scope")
        for child in row.issues:
            verify_report(child, depth + 1)
    for header in reports:
        verify_report(header)
    headers = [r for r in reports if r.has_fix is True]
    if len(headers) > 1:
        raise inspection.CapabilityUnavailableError("repair requires one bounded native fix header")
    if headers:
        from .failed_elements import _configure_option
        _configure_option(headers[0], "fix_method", 2)
    full_before = _fe_snapshot(base, constants, deck)
    validity_before = _validity(base, entities, profile["element_family"])
    scope_keys = {(before["entity_type"], eid) for eid in ids}
    external_nodes = set()
    for element in full_before["elements"]:
        ref = element["entity"]
        if (ref["type"], ref["id"]) not in scope_keys:
            external_nodes.update(set(element["node_ids"]).intersection(nodes))
    automatic_nodes = allowed is None
    if automatic_nodes:
        allowed = set(nodes) - external_nodes
        attachment_api = inspection._require_api(base, "NodesToElements", "ansa.base.NodesToElements")
        attachments = attachment_api(list(nodes.values()), sources=["bcs"])
        if type(attachments) is not dict:
            raise RuntimeError("automatic movable-node boundary-condition lookup was incomplete")
        seen = set()
        for node, attached in attachments.items():
            if node._id not in nodes or node._id in seen or type(attached) not in (list, tuple):
                raise RuntimeError("automatic movable-node boundary-condition lookup was invalid")
            seen.add(node._id)
            if attached:
                allowed.discard(node._id)
        if seen != set(nodes):
            raise RuntimeError("automatic movable-node boundary-condition lookup missed nodes")
    for element in full_before["elements"]:
        ref = element["entity"]
        if (ref["type"], ref["id"]) not in scope_keys and allowed.intersection(element["node_ids"]):
            raise ValueError("movable nodes are referenced by FE outside the repair scope")
    if _scope(base, constants, deck, profile["element_family"], ids)[3] != before["geometry_sha256"] or _criteria(base) != native_before:
        raise RuntimeError("repair preconditions changed during native check")
    if native_template:
        from .native_quality import _dependencies
        if _dependencies(base, deck) != before["dependencies_sha256"]:
            raise RuntimeError("native repair property/material preconditions changed")
    if not before["failed_entity_ids"]:
        return {"ok": True, "applied": False, "reason": "already_passed", "before": before,
                "repair_outcome": "already_passed", "quality_passed": True,
                "after": before, "read_only": False, "scope_verified": True, "possible_effects": False}
    if method == "fix_quality" and not headers:
        return {"ok": True, "applied": False, "reason": "native_fix_unavailable", "before": before,
                "repair_outcome": "unchanged", "quality_passed": False,
                "after": before, "read_only": False, "scope_verified": True, "possible_effects": False}
    if not allowed:
        return {"ok": True, "applied": False, "reason": "no_movable_nodes", "before": before,
                "repair_outcome": "unchanged", "quality_passed": False,
                "after": before, "read_only": False, "scope_verified": True, "possible_effects": False}
    from . import _model_save
    path = params["backup_path"]
    if path == base.DataBaseName():
        raise ValueError("backup must differ from the current database path")
    backup = _model_save(base, constants, {"path": path, "overwrite": False})
    if _fe_snapshot(base, constants, deck) != full_before or _criteria(base) != native_before:
        raise RuntimeError("backup changed FE state or quality settings; repair was not invoked")
    if native_template and _dependencies(base, deck) != before["dependencies_sha256"]:
        raise RuntimeError("backup changed property/material cards; repair was not invoked")
    result = {"ok": False, "applied": False, "backup": backup, "before": before,
              "scope_verified": False, "possible_effects": True, "read_only": False,
              "method": method,
              "api": "ansa.base.CheckReport.try_fix" if method == "fix_quality" else "ansa.mesh." + api_name,
              "fix_method": 2 if method == "fix_quality" else None, "api_invoked": False,
              "criteria_synchronized": False, "criteria_restored": False,
              "automatic_movable_nodes": automatic_nodes, "movable_node_ids": sorted(allowed),
              "validity_before": validity_before,
              "verification_scope": "all FE cards/nodes and current quality criteria",
              "unverified_model_scope": "non-FE geometry, model organization, solver result accuracy"}
    try:
        if method == "fix_quality":
            from .native_quality import _run_fix_with_criteria
            from .quality_options import run_with_options
            run_with_options(base, profile["element_family"], maximum,
                             lambda: _run_fix_with_criteria(base, headers[0], text, native_before, result), result)
        elif profile["element_family"] == "shell":
            result["api_invoked"] = True
            smoother(entities)
        else:
            result["api_invoked"] = True
            smoother(entities, freeze_skin=True)
        current = _fe_snapshot(base, constants, deck)
        if current["elements"] != full_before["elements"] or set(current["nodes"]) != set(full_before["nodes"]):
            raise RuntimeError("native repair changed FE topology or node identities")
        # Native repair has no fixed-node argument. Treat its result as a
        # coordinate proposal, restore scoped fixed nodes, then validate the
        # constrained result. Never keep a native move outside the chosen mesh.
        restored_fixed = []
        for eid in sorted(set(nodes) - allowed):
            key = str(eid)
            if current["nodes"][key]["position"] != full_before["nodes"][key]["position"]:
                nodes[eid].position = tuple(full_before["nodes"][key]["position"])
                restored_fixed.append(eid)
        if restored_fixed:
            current = _fe_snapshot(base, constants, deck)
        result["restored_fixed_node_ids"] = restored_fixed
        changed = []
        for eid, row in current["nodes"].items():
            prior = full_before["nodes"][eid]
            if row != prior:
                if int(eid) not in allowed:
                    raise RuntimeError(f"native repair changed protected node {eid}")
                if math.dist(row["position"], prior["position"]) > maximum:
                    raise RuntimeError(f"native repair exceeded the displacement limit for node {eid}")
                baseline = params.get("_cumulative_baseline")
                if baseline is not None and math.dist(row["position"], baseline[eid]) > params["maximum_total_displacement"]:
                    raise RuntimeError(f"native repair exceeded the cumulative displacement limit for node {eid}")
                prior_card, current_card = prior["card"]["fields"].copy(), row["card"]["fields"].copy()
                for label in ("X1", "X2", "X3", "X", "Y", "Z"):
                    prior_card.pop(label, None)
                    current_card.pop(label, None)
                if prior_card != current_card:
                    raise RuntimeError("native repair changed non-coordinate node fields")
                changed.append(int(eid))
        if native_template:
            from .native_quality import report as recheck
        else:
            recheck = _report
        after = recheck(base, constants, deck, profile, ids)[0]
        if native_template and after["dependencies_sha256"] != before["dependencies_sha256"]:
            raise RuntimeError("native repair changed property/material cards")
        validity_after = _validity(base, entities, profile["element_family"])
        if any(r["measure"] <= 0 or r["jacobian"] <= 0 for r in validity_after):
            raise RuntimeError("repair left a non-positive element measure or Jacobian")
        result["validity_after"] = validity_after
        from .native_quality import _criteria_matches
        if not _criteria_matches(native_before, _criteria(base)):
            raise RuntimeError("native repair changed global quality criteria")
        if not after["complete"] or not set(after["failed_entity_ids"]) <= set(before["failed_entity_ids"]):
            raise RuntimeError("repair introduced unknown metrics or new failing elements")
        original_violations = {r["id"]: set(r["violations"]) for r in before["rows"]}
        if any(not set(r["violations"]) <= original_violations[r["id"]] for r in after["rows"]):
            raise RuntimeError("repair introduced a new criterion violation")
        improved = len(after["failed_entity_ids"]) < len(before["failed_entity_ids"])
        if changed and not improved and native_template:
            from .quality_values import progress
            result["metric_progress"] = progress(before, after)
            improved = result["metric_progress"].get("improved") is True
            for name in ("improvements", "regressions"):
                records = result["metric_progress"].get(name, [])
                result["metric_progress"][name + "_total"] = len(records)
                result["metric_progress"][name + "_truncated"] = len(records) > 32
                result["metric_progress"][name] = records[:32]
        if changed and not improved:
            raise RuntimeError("repair moved nodes without verifiable quality improvement")
        quality_passed = after["complete"] and not after["failed_entity_ids"] and not after.get("unknown_entity_ids")
        result.update({"ok": True, "applied": bool(changed), "improved": improved,
                       "quality_passed": quality_passed,
                       "repair_outcome": "fully_repaired" if quality_passed else "partially_repaired" if improved else "unchanged",
                       "reason": "improved" if improved else "no_improvement",
                       "after": after, "changed_node_ids": sorted(changed), "scope_verified": True})
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:2048]
        try:
            current = _fe_snapshot(base, constants, deck)
            result["coordinates_rolled_back"] = _restore_positions(base, constants, deck, full_before, current)
            from .native_quality import _criteria_matches
            result["recovery_required"] = (not result["coordinates_rolled_back"]
                                           or result.get("options_restored") is False
                                           or not _criteria_matches(native_before, _criteria(base)))
            if native_template:
                from .native_quality import report as recovery_report
            else:
                recovery_report = _report
            result["after"] = recovery_report(base, constants, deck, profile, ids)[0]
            if native_template and result["after"]["dependencies_sha256"] != before["dependencies_sha256"]:
                result["recovery_required"] = True
        except Exception as readback:
            result["recovery_required"] = True
            result["readback_error"] = f"{type(readback).__name__}: {readback}"[:1024]
        return result


def _repair_iterations(state, base, constants, deck, params, profile, ids, before, entities, nodes):
    iterations = params.get("max_iterations", 1)
    if type(iterations) is not int or not 1 <= iterations <= 10:
        raise ValueError("max_iterations must be an integer in 1..10")
    maximum_total = _number(params.get("maximum_total_displacement", params["maximum_displacement"]))
    if maximum_total <= 0:
        raise ValueError("maximum_total_displacement must be positive")
    baseline = {str(eid): _position(node) for eid, node in nodes.items()}
    initial, current = before, before
    history, backups, changed = [], [], set()
    last = None
    from pathlib import Path
    path = Path(params["backup_path"])
    for index in range(iterations):
        step = dict(params, _cumulative_baseline=baseline, maximum_total_displacement=maximum_total)
        if index:
            step["backup_path"] = str(path.with_name(path.stem + f"-iteration-{index + 1}" + path.suffix))
        round_snapshot = round_criteria = None
        try:
            if index:
                round_snapshot = _fe_snapshot(base, constants, deck)
                round_criteria = _criteria(base)
            value = _repair(state, base, constants, deck, step, profile, ids, current, entities, nodes)
        except Exception as exc:
            if last is None:
                raise
            preserved = False
            try:
                preserved = (round_snapshot is not None and _fe_snapshot(base, constants, deck) == round_snapshot
                             and _criteria(base) == round_criteria)
                if preserved and "template" in profile:
                    from .native_quality import _dependencies
                    preserved = _dependencies(base, deck) == current["dependencies_sha256"]
            except Exception:
                pass
            value = {"ok": False, "applied": False, "error": f"{type(exc).__name__}: {exc}"[:2048],
                     "round_state_preserved": preserved, "recovery_required": not preserved,
                     "scope_verified": preserved, "after": current,
                     "possible_effects": not preserved, "read_only": False}
        history.append({"iteration": index + 1, "ok": value["ok"], "applied": value["applied"],
                        "reason": value.get("reason"), "error": value.get("error"),
                        "failed_before": len(current["failed_entity_ids"]),
                        "failed_after": len(value.get("after", current)["failed_entity_ids"]),
                        "metric_progress": value.get("metric_progress"),
                        "criteria_restored": value.get("criteria_restored"), "options_restored": value.get("options_restored"),
                        "coordinates_rolled_back": value.get("coordinates_rolled_back")})
        if value.get("backup"):
            backups.append(value["backup"])
        if not value["ok"]:
            if last is not None and (value.get("coordinates_rolled_back") is True or value.get("round_state_preserved") is True) and not value.get("recovery_required"):
                last["termination_reason"] = "round_rolled_back"
                break
            last = value
            break
        changed.update(value.get("changed_node_ids", []))
        last = value
        current = value["after"]
        if value.get("quality_passed") or not value.get("improved"):
            last["termination_reason"] = "quality_passed" if value.get("quality_passed") else value.get("reason", "no_improvement")
            break
        if getattr(state, "stopped", None) is not None and state.stopped.is_set():
            last["termination_reason"] = "stopped_between_iterations"
            break
    else:
        last["termination_reason"] = "iteration_limit"
    if iterations > 1 or "max_iterations" in params or "maximum_total_displacement" in params:
        cumulative = None
        if not last.get("recovery_required"):
            try:
                cumulative = max((math.dist(_position(node), baseline[str(eid)]) for eid, node in nodes.items()), default=0.0)
            except Exception as exc:
                last.update(ok=False, recovery_required=True, scope_verified=False,
                            cumulative_readback_error=f"{type(exc).__name__}: {exc}"[:1024])
        last.update(before=initial, iterations=history, backups=backups,
                    changed_node_ids=sorted(changed | set(last.get("changed_node_ids", []))),
                    maximum_total_displacement=maximum_total,
                    maximum_cumulative_displacement=cumulative)
        if last.get("ok"):
            quality_passed = current.get("complete") is True and not current["failed_entity_ids"] and not current.get("unknown_entity_ids")
            last.update(after=current, applied=bool(changed), improved=bool(changed), quality_passed=quality_passed,
                        repair_outcome="already_passed" if not initial["failed_entity_ids"] else
                        "fully_repaired" if quality_passed else "partially_repaired" if changed else "unchanged",
                        criteria_restored=all(row.get("criteria_restored") is not False for row in history),
                        options_restored=all(row.get("options_restored") is not False for row in history),
                        automatic_movable_nodes=params.get("movable_node_ids") is None)
            if backups:
                last["backup"] = backups[0]
    return last


def execute(state, tool, params):
    state.assert_main()
    if tool == "quality_page":
        inspection._params(params, ("deck", "report_id"), ("offset", "limit", "status", "criterion"))
        from ansa import base
        from .quality_reports import page
        return page(state, inspection._deck(base, params), params)
    if tool not in {"quality_report", "quality_locate", "quality_repair"}:
        raise ValueError("unsupported quality workflow operation")
    optional = {"quality_report": (), "quality_locate": ("expected_scope_sha256",),
                "quality_repair": ("expected_scope_sha256", "backup_path", "maximum_displacement")}[tool]
    required = ("deck", "profile", *optional) + (() if tool == "quality_report" else ("entity_ids",))
    extra = (("method", "movable_node_ids", "max_iterations", "maximum_total_displacement") if tool == "quality_repair"
             else ("criterion",) if tool == "quality_locate" else
             ("entity_ids", "scope", "batch_size", "max_seconds", "max_report_megabytes", "paged"))
    inspection._params(params, required, extra)
    raw_profile = params["profile"]
    if type(raw_profile) is dict and "template" in raw_profile:
        from ansa_mcp_shared.quality_templates import normalize_template_profile
        from .native_quality import report as quality_report
        profile = normalize_template_profile(raw_profile)
    else:
        inspection._params(raw_profile, ("analysis_type", "criteria"),
                           ("element_family", "minimum_length", "wave_speed", "target_time_step"))
        profile = normalize_profile(**raw_profile)
        quality_report = _report
    from ansa import base, constants
    deck = inspection._deck(base, params)
    if tool == "quality_report" and (params.get("scope") is not None or params.get("paged") is True
                                     or len(params.get("entity_ids", [])) > _MAX_SCOPE):
        from .quality_reports import build
        return build(state, base, constants, deck, profile, params, quality_report)
    ids = inspection._id_list(params["entity_ids"], "entity_ids", maximum=_MAX_SCOPE)
    if tool != "quality_report" and deck == getattr(constants, "LSDYNA", None):
        if profile.get("template") not in {"explicit_crash_shell", "explicit_crash_solid"}:
            raise ValueError("LS-DYNA quality writes require an explicit_crash native template")
    before, entities, nodes, snapshot = quality_report(base, constants, deck, profile, ids)
    if tool == "quality_report":
        return _bounded_result(before)
    if params["expected_scope_sha256"] != before["scope_sha256"]:
        raise ValueError("quality scope changed; obtain a fresh quality report")
    if tool == "quality_repair":
        if deck == getattr(constants, "LSDYNA", None):
            expected_nodes = 4 if profile["element_family"] == "shell" else 8
            if any(len(row["node_ids"]) != expected_nodes for row in snapshot["elements"]):
                raise ValueError("LS-DYNA repair currently supports QUAD4/HEXA8 only")
        return _bounded_result(_repair_iterations(state, base, constants, deck, params, profile, ids, before, entities, nodes))
    failed = set(before["failed_entity_ids"])
    criterion = params.get("criterion")
    if "criterion" in params:
        if not isinstance(criterion, str) or not criterion.strip() or len(criterion) > 128:
            raise ValueError("criterion must be a non-empty string within 128 characters")
        if "template" in profile:
            supported = {row["name"] for row in before["criteria_snapshot"]["active_criteria"]}
        else:
            supported = {row["name"] for row in profile["criteria"]}
            if profile["minimum_length"] is not None:
                supported.add("MINL")
        if criterion not in supported:
            raise ValueError("criterion must exactly match an active criterion in the quality report")
        failed &= {row["id"] for row in before["rows"] if criterion in row["violations"]}
    selected = [e for e in entities if e._id in failed]
    result = {"report": before, "display_changed": False, "located_entity_ids": sorted(failed),
              "criterion": criterion,
              "read_only": False, "model_written": False,
              "visibility_verification_scope": "/".join(_element_kind(constants, deck, family)
                                                          for family in ("shell", "solid"))}
    if not selected:
        result["reason"] = "no_matching_failures"
        return _bounded_result(result)
    from ansa import guitk
    if inspection._require_api(guitk, "BCApplicationIsGuiMode", "ansa.guitk.BCApplicationIsGuiMode")() is not True:
        raise inspection.CapabilityUnavailableError("quality display location requires an ANSA GUI session")
    if base.Or(selected, apply_on_contents=False) != 1:
        raise RuntimeError("ANSA did not confirm isolating the failed elements")
    visible = base.CollectEntities(deck, None, [_element_kind(constants, deck, "shell"), _element_kind(constants, deck, "solid")], filter_visible=True)
    expected = {(before["entity_type"], eid) for eid in failed}
    actual = {(e.ansa_type(deck), e._id) for e in visible}
    if actual != expected:
        raise RuntimeError("failed-element display isolation was not confirmed")
    if _scope(base, constants, deck, profile["element_family"], ids)[3] != before["geometry_sha256"]:
        raise RuntimeError("display operation changed the mesh scope")
    base.RedrawAll()
    base.ZoomAll()
    result["display_changed"] = True
    return _bounded_result(result)


__all__ = ["execute"]
