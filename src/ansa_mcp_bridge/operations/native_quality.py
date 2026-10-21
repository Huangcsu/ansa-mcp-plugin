"""Four analysis policies executed by ANSA's native MeshQuality check."""

import math
import hashlib
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation

from . import checks, failed_elements, inspection

_ROW = re.compile(r"^\s*(.*?)\s+\[(shells|solids)\]\s*=\s*(ON|OFF),\s*([^:]*):")
_METADATA_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![A-Za-z0-9_.])"
)


def _metadata_signature(value):
    value = re.sub(r"\s*([,|/:=\[\](){}<>])\s*", r"\1", value.strip())
    value = re.sub(r"\s+", " ", value)
    parts, offset = [], 0
    for match in _METADATA_NUMBER.finditer(value):
        parts.append(value[offset:match.start()])
        try:
            parts.append(Decimal(match.group()))
        except InvalidOperation:
            parts.append(match.group())
        offset = match.end()
    parts.append(value[offset:])
    return tuple(parts)


def _criteria_matches(expected, actual):
    if expected == actual:
        return True
    expected_rows, expected_assignments = _parse(expected)
    actual_rows, actual_assignments = _parse(actual)
    if set(expected_rows) != set(actual_rows):
        return False
    for key, expected_row in expected_rows.items():
        actual_row = actual_rows[key]
        if (expected_row["active"] != actual_row["active"]
                or " ".join(expected_row["calculation"].split())
                != " ".join(actual_row["calculation"].split())
                or expected_row["levels"] != actual_row["levels"]
                or _metadata_signature(expected_row["line"]) != _metadata_signature(actual_row["line"])):
            return False
    row_keys = {row["key"] for row in expected_rows.values()}
    expected_metadata = {key: value for key, value in expected_assignments.items() if key not in row_keys}
    actual_metadata = {key: value for key, value in actual_assignments.items() if key not in row_keys}
    return (set(expected_metadata) == set(actual_metadata)
            and all(_metadata_signature(value) == _metadata_signature(actual_metadata[key])
                    for key, value in expected_metadata.items()))


def _require_criteria_match(expected, actual, operation):
    try:
        matches = _criteria_matches(expected, actual)
    except Exception as exc:
        raise RuntimeError(f"could not verify ANSA quality criteria after {operation}: {exc}") from exc
    if not matches:
        raise RuntimeError(f"ANSA quality criteria did not match after {operation}")


def _align_inactive_calculations(base, expected, actual):
    """ReadQualityCriteria 可改写未启用的算法，用原生 setter/getter 精确恢复。"""
    wanted_rows, _ = _parse(expected)
    actual_rows, _ = _parse(actual)
    adjusted = []
    for (family, name), wanted in wanted_rows.items():
        observed = actual_rows.get((family, name))
        if (observed is None or wanted["active"] or observed["active"]
                or wanted["calculation"] == observed["calculation"]):
            continue
        stem = "F11ShellsOptions" if family == "shells" else "F11SolidsOptions"
        getter, setter = getattr(base, stem + "Get", None), getattr(base, stem + "Set", None)
        if not callable(getter) or not callable(setter):
            raise RuntimeError("cannot restore inactive native calculation: " + name)
        prior = getter(name)
        if (type(prior) is not dict or prior.get("status") not in (False, 0)
                or type(prior.get("value")) not in (int, float) or not math.isfinite(prior["value"])):
            raise RuntimeError("inactive native calculation options are unavailable: " + name)
        if setter(name, False, wanted["calculation"], prior["value"]) != 1:
            raise RuntimeError("native setter rejected inactive calculation: " + name)
        confirmed = getter(name)
        if (type(confirmed) is not dict or confirmed.get("status") not in (False, 0)
                or confirmed.get("calculation") != wanted["calculation"]
                or confirmed.get("value") != prior["value"]):
            raise RuntimeError("native getter did not confirm inactive calculation: " + name)
        adjusted.append(family + ":" + name)
    return adjusted


def _run_fix_with_criteria(base, header, effective, original, result):
    from ansa import mesh
    from . import quality_workflow as common

    with tempfile.TemporaryDirectory(prefix="ansa-quality-criteria-") as temp_dir:
        backup_path = os.path.join(temp_dir, "original.ansa_qual")
        effective_path = os.path.join(temp_dir, "effective.ansa_qual")
        save_result = mesh.SaveQualityCriteria(backup_path)
        if save_result != 1:
            raise RuntimeError("ANSA failed to save the original quality criteria")
        if not os.path.isfile(backup_path):
            raise RuntimeError("ANSA reported saving quality criteria but created no backup file")
        if common._criteria(base) != original:
            raise RuntimeError("saving quality criteria changed the active original criteria")

        with open(effective_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(effective)
        try:
            read_result = mesh.ReadQualityCriteria(effective_path)
            if read_result != 1:
                raise RuntimeError("ANSA failed to read the effective quality criteria")
            adjustments = _align_inactive_calculations(base, effective, common._criteria(base))
            if adjustments:
                result["criteria_apply_adjustments"] = adjustments
            _require_criteria_match(effective, common._criteria(base), "applying effective criteria")

            result["criteria_synchronized"] = True
            result["api_invoked"] = True
            return header.try_fix(request_gui=False)
        except Exception as exc:
            result["repair_error" if result.get("api_invoked") else "criteria_apply_error"] = f"{type(exc).__name__}: {exc}"[:2048]
            raise
        finally:
            if not os.path.isfile(backup_path):
                result["criteria_restored"] = False
                raise RuntimeError("failed to restore original ANSA quality criteria: backup file is missing")
            try:
                restore_result = mesh.ReadQualityCriteria(backup_path)
                if restore_result != 1:
                    raise RuntimeError("ANSA did not accept the saved criteria backup")
                adjustments = _align_inactive_calculations(base, original, common._criteria(base))
                if adjustments:
                    result["criteria_restore_adjustments"] = adjustments
                _require_criteria_match(original, common._criteria(base), "restoring original criteria")
            except Exception as exc:
                result["criteria_restored"] = False
                result["criteria_restore_error"] = f"{type(exc).__name__}: {exc}"[:2048]
                with open(backup_path, "rb") as source:
                    with tempfile.NamedTemporaryFile(prefix="ansa-quality-recovery-", suffix=".ansa_qual", delete=False) as recovery:
                        recovery.write(source.read())
                        result["criteria_backup_path"] = recovery.name
                raise RuntimeError(f"failed to restore original ANSA quality criteria: {exc}") from exc
            result["criteria_restored"] = True


def _parse(text):
    if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > 65536:
        raise ValueError("native quality criteria must be non-empty UTF-8 text within 65536 bytes")
    rows, assignments = {}, {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("native criteria contain an unsupported non-assignment line")
        key, value = (part.strip() for part in line.split("=", 1))
        if key in assignments:
            raise ValueError("native criteria contain a duplicate key: " + key)
        assignments[key] = value
        match = _ROW.match(line)
        if match:
            name, family, status, calculation = match.groups()
            parts = line.split("|", 2)
            if len(parts) != 3:
                raise ValueError("native criterion has no four quality ranges: " + name)
            levels = []
            for entry in parts[2].split("/"):
                if not entry.strip():
                    continue
                cells = [x.strip() for x in entry.split(",")]
                if len(cells) != 2:
                    raise ValueError("invalid native quality range: " + name)
                values = []
                for cell in cells:
                    number = None if cell == "BLANK" else float(cell)
                    if number is not None and not math.isfinite(number):
                        raise ValueError("native quality limits must be finite")
                    values.append(number)
                levels.append(values)
            if len(levels) != 4:
                raise ValueError("native criterion requires four quality ranges: " + name)
            rows[(family, name)] = {"key": key, "line": line, "active": status == "ON",
                                   "calculation": calculation.strip(), "levels": levels}
    if not rows:
        raise ValueError("native criteria contain no supported shell/solid rows")
    return rows, assignments


def compile_criteria(global_text, profile):
    """Preserve current algorithms; a text echo cannot validate algorithm names."""
    from ansa_mcp_shared.quality_templates import get_template
    from . import quality_workflow as common
    baseline, baseline_keys = _parse(global_text)
    requested, requested_keys = _parse(profile["quality_criteria"] or global_text)
    if set(requested_keys) - set(baseline_keys):
        raise inspection.CapabilityUnavailableError("native criteria include keys unavailable in the current ANSA version")
    for key, value in requested_keys.items():
        if key.startswith("jacobian_") or key == "ANSA_Version":
            if value != baseline_keys[key]:
                raise ValueError("native calculation definitions/version differ from the current session: " + key)
    family = "shells" if profile["element_family"] == "shell" else "solids"
    for (which, name), row in requested.items():
        if which == family and row["active"]:
            if (which, name) not in baseline or row["calculation"] != baseline[(which, name)]["calculation"]:
                raise ValueError("native calculation must match the current session: " + name)
    lines = []
    for line in global_text.splitlines():
        match = _ROW.match(line)
        if match:
            name, which, _, _ = match.groups()
            row = requested.get((which, name)) if which == family else None
            line = row["line"] if row else re.sub(r"=\s*(?:ON|OFF),", "= OFF,", line, count=1)
            if which == family and name == "crash time step" and profile["analysis_type"] == "explicit_crash":
                if baseline[(which, name)]["calculation"] != "LS-DYNA":
                    raise inspection.CapabilityUnavailableError("crash template requires the current LS-DYNA timestep calculation")
                parts = line.split("|", 2)
                prefix = re.sub(r"=\s*(?:ON|OFF),", "= ON,", parts[0], count=1)
                line = prefix + "|" + parts[1] + "| " + " ".join(
                    f"{profile['target_time_step']:.17g}, -1./" for _ in range(4))
        elif "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in requested_keys:
                line = key + " = " + requested_keys[key]
        lines.append(line)
    effective = "\n".join(lines)
    rows, metadata = _parse(effective)
    try:
        failed_index = int(metadata["failed_index_" + family].strip(" ,"))
    except (KeyError, ValueError) as exc:
        raise ValueError("native criteria lack an explicit failed range") from exc
    if failed_index not in range(4):
        raise ValueError("native failed range is invalid")
    active = {name: row for (which, name), row in rows.items() if which == family and row["active"]}
    for name, row in active.items():
        if row["calculation"] != baseline[(family, name)]["calculation"]:
            raise ValueError("native calculation must match the current session: " + name)
    if profile["analysis_type"] == "explicit_crash" and "characteristic length" in active:
        if active["characteristic length"]["calculation"] != "LS-DYNA":
            raise inspection.CapabilityUnavailableError("crash template requires the current LS-DYNA characteristic length calculation")
    if profile["analysis_type"] == "linear" and "crash time step" in active:
        raise ValueError("linear templates must not activate an explicit timestep criterion")
    template = get_template(profile["template"])
    missing = [name for name in template["required_criteria"] if name not in active]
    for name, row in active.items():
        value = row["levels"][failed_index][0]
        if name in template["required_criteria"] and name not in {"incomplete element", "negative volume", "left handed"} and value is None:
            missing.append(name + ": no numeric failure limit")
        if name == "characteristic length" and (value is None or value <= 0):
            missing.append(name + ": positive failure limit required")
    snapshot = {"sha256": common._digest(effective), "source": "custom_text" if profile["quality_criteria"] else "current_session",
                "label": profile["criteria_label"], "failed_range_index": failed_index,
                "active_criteria": [{"name": name, "calculation": row["calculation"], "levels": row["levels"]}
                                    for name, row in sorted(active.items())],
                "missing_criteria": sorted(set(missing)), "calculation_source": "current_ANSA_session"}
    return effective, snapshot


def configure_check(base, profile, global_text):
    text, snapshot = compile_criteria(global_text, profile)
    factory = inspection._factory(base, "mesh", "MeshQuality", "ansa.base.checks.mesh.MeshQuality")
    check = factory()
    if check.is_available_in_deck(base.CurrentDeck()) is not True:
        raise inspection.CapabilityUnavailableError("MeshQuality does not support the current deck")
    for name, value in {"use_criteria": "Use custom criteria", "quality_criteria": text}.items():
        failed_elements._configure_option(check, name, value)
    return check, text, snapshot


def _dependencies(base, deck):
    from . import quality_workflow as common
    result = hashlib.sha256()
    for kind in ("__PROPERTIES__", "__MATERIALS__"):
        entities = common._collect(base, deck, None, kind)
        for entity in sorted(entities, key=lambda e: (e.ansa_type(deck), e._id)):
            result.update(common._encoded(common._card(base, deck, entity)))
            result.update(b"\n")
    return result.hexdigest()


def report(base, constants, deck, profile, ids):
    from ansa_mcp_shared.quality_templates import get_template
    from . import quality_workflow as common
    original = common._criteria(base)
    entities, nodes, scope, geometry_hash = common._scope(base, constants, deck, profile["element_family"], ids)
    dependencies = _dependencies(base, deck)
    check, effective, criteria = configure_check(base, profile, original)
    raw = check.execute(exec_mode=base.Check.EXEC_ON_SELECTED, report=base.Check.REPORT_NONE,
                        history=base.Check.KEEP_OLD, entities=entities)
    if check.quality_criteria != effective or check.use_criteria != "Use custom criteria":
        raise RuntimeError("native check did not retain the requested per-call criteria")
    primary = common._element_kind(constants, deck, profile["element_family"])
    node_kind = "GRID" if deck == constants.NASTRAN else "NODE"
    context_keys = {(primary, eid) for eid in ids} | {(node_kind, eid) for eid in nodes}
    tree, issue_count, issue_counts, failed_ids = checks._serialize_reports(
        base, deck, raw, set(ids), context_keys, set(nodes), primary_type=primary, check_name="MeshQuality")
    violations = {eid: set() for eid in ids}
    active_names = sorted((r["name"] for r in criteria["active_criteria"]), key=len, reverse=True)
    unmapped = []
    def visit(row):
        if row["children"]:
            for child in row["children"]:
                visit(child)
        elif row["status"] in {"error", "warning"}:
            description = row["description"].lower()
            key = next((n for n in active_names if description.startswith(n + " ")), None)
            if key is None:
                unmapped.append(row["description"][:256])
                key = "unmapped_native_issue"
            for ref in row["entities"]:
                if ref["type"] == primary:
                    violations[ref["id"]].add(key)
    for row in tree:
        visit(row)
    timestep_values, metric_errors = {}, {}
    if profile["analysis_type"] == "explicit_crash":
        for entity in entities:
            try:
                values = base.ElementQuality(entity, ["CRASH"])
                if type(values) not in (tuple, list) or len(values) != 1:
                    raise ValueError("native timestep is unavailable")
                value = common._number(values[0])
                if value <= 0:
                    raise ValueError("native timestep must be positive; check material/property inputs")
                timestep_values[entity._id] = value
            except Exception as exc:
                timestep_values[entity._id] = None
                metric_errors[entity._id] = f"{type(exc).__name__}: {exc}"[:512]
    missing = criteria["missing_criteria"]
    complete = not missing and not unmapped and not metric_errors
    unknown_ids = list(ids) if missing or unmapped else sorted(metric_errors)
    element_map = {r["entity"]["id"]: r for r in scope["elements"]}
    from .quality_values import measure
    numeric = measure(base, entities, profile["element_family"], criteria)
    rows = []
    for eid in ids:
        node_ids = element_map[eid]["node_ids"]
        points = [scope["node_positions"][str(n)] for n in node_ids]
        rows.append({"id": eid, "violations": sorted(violations[eid]),
                     "unknown": list(missing) + (["unmapped_native_issue"] if unmapped else []) + (["CRASH"] if eid in metric_errors else []),
                     "complete": eid not in unknown_ids, "passed": eid not in failed_ids and eid not in unknown_ids,
                     "metrics": {**numeric[eid]["metrics"], **({"CRASH": timestep_values[eid]} if timestep_values else {})},
                     "quality_details": numeric[eid]["quality_details"],
                     "type": primary,
                     "element_type": ({3: "TRIA3", 4: "QUAD4", 6: "TRIA6", 8: "QUAD8"} if profile["element_family"] == "shell"
                                      else {4: "TETRA4", 5: "PYRAMID5", 6: "PENTA6", 8: "HEXA8", 10: "TETRA10", 20: "HEXA20"}).get(len(node_ids), "UNKNOWN"),
                     "metric_errors": {"CRASH": metric_errors[eid]} if eid in metric_errors else {},
                     "node_ids": node_ids, "center": [sum(p[i] for p in points)/len(points) for i in range(3)],
                     "bounds": {"min": [min(p[i] for p in points) for i in range(3)],
                                "max": [max(p[i] for p in points) for i in range(3)]}})
    if (common._scope(base, constants, deck, profile["element_family"], ids)[3] != geometry_hash
            or common._criteria(base) != original or _dependencies(base, deck) != dependencies):
        raise RuntimeError("mesh, materials/properties or global criteria changed during native quality report")
    profile_summary = {k: v for k, v in profile.items() if k != "quality_criteria"}
    decision_hash = common._digest({"reports": tree, "timestep": timestep_values, "dependencies": dependencies})
    result = {"deck": deck, "engine": "ANSA_MeshQuality", "template": get_template(profile["template"]),
              "profile": profile_summary, "analysis_type": profile["analysis_type"], "entity_type": primary,
              "entity_ids": ids, "rows": rows, "failed_entity_ids": failed_ids, "unknown_entity_ids": unknown_ids,
              "complete": complete, "passed": complete and not failed_ids, "scope": "explicit", "read_only": True,
              "scope_sha256": common._digest({"geometry": geometry_hash, "profile": profile["profile_sha256"],
                                               "global": common._digest(original), "effective": criteria["sha256"],
                                               "decision": decision_hash}),
              "geometry_sha256": geometry_hash, "measurement_sha256": decision_hash,
              "dependencies_sha256": dependencies, "native_criteria_sha256": common._digest(original),
              "criteria_snapshot": criteria, "native_report_count": len(raw), "issue_count": issue_count,
              "issue_counts": issue_counts, "unmapped_native_issues": unmapped,
              "native_reports_sha256": common._digest(tree), "solver_verified": False,
              "formulation_verified": False, "crash_time_step_is_estimate": profile["analysis_type"] == "explicit_crash",
              "acceptance_scope": "configured native checks only; not solver data-check or mesh convergence"}
    return result, entities, nodes, scope
