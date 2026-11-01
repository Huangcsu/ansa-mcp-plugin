"""Owned-instance native probe and public mesh-scope MCP acceptance."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "integration")]
import stage_three_ansa as stage


FIXTURE_SCRIPT = r'''import json
from ansa import base, mesh, constants

deck = constants.NASTRAN

def ids(value):
    if value is None or value == 0:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            result.append(int(item))
        else:
            result.append(int(item._id))
    return sorted(set(result))

def eid(entity):
    return None if entity is None else int(entity._id)

def entities(value):
    if value is None or value == 0:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]

def card_state(entity):
    reference = {"type": entity.ansa_type(deck), "id": eid(entity)}
    try:
        names = list(entity.card_fields(deck))
    except Exception as exc:
        return {"entity": reference, "card_fields_error": type(exc).__name__ + ": " + str(exc),
                "card_fields": [], "fields": {}}
    frozen_names = [name for name in names if "frozen" in str(name).casefold()]
    try:
        values = base.GetEntityCardValues(deck, entity, frozen_names) if frozen_names else {}
        values = {str(key): value for key, value in values.items()}
    except Exception as exc:
        values = {"__read_error__": type(exc).__name__ + ": " + str(exc)}
    return {"entity": reference, "card_fields": frozen_names, "fields": values}

def perimeters(face):
    return entities(base.PerimetersOfFace(face, "all"))

def macro_of(face):
    return base.GetFaceMacro(face)

def freeze_snapshot(face_entities, extra_cons=()):
    faces_now = list(face_entities)
    macros_now = []
    cons_now = list(extra_cons)
    shells_now = []
    for face in faces_now:
        macro = macro_of(face)
        if macro is not None and eid(macro) not in [eid(item) for item in macros_now]:
            macros_now.append(macro)
        cons_now.extend(perimeters(face))
    for macro in macros_now:
        shells_now.extend(entities(mesh.GetMacroShells([macro])))
    unique_cons = {eid(item): item for item in cons_now if item is not None}
    unique_shells = {eid(item): item for item in shells_now if item is not None}
    return {
        "faces": [card_state(item) for item in sorted(faces_now, key=eid)],
        "macros": [card_state(item) for item in sorted(macros_now, key=eid)],
        "cons": [card_state(unique_cons[key]) for key in sorted(unique_cons)],
        "shells": [card_state(unique_shells[key]) for key in sorted(unique_shells)],
    }

def states_by_entity(snapshot):
    result = {}
    for section in ("faces", "macros", "cons", "shells", "solids", "grids"):
        for row in snapshot.get(section, []):
            ref = row["entity"]
            result[(ref["type"], ref["id"])] = row.get("fields", {})
    return result

def state_changes(before, after):
    old = states_by_entity(before)
    new = states_by_entity(after)
    changes = []
    for key in sorted(set(old) | set(new)):
        if key not in old or key not in new:
            changes.append({"entity": {"type": key[0], "id": key[1]},
                            "before_present": key in old, "after_present": key in new})
            continue
        for field in sorted(set(old[key]) | set(new[key])):
            if old[key].get(field) != new[key].get(field):
                changes.append({"entity": {"type": key[0], "id": key[1]}, "field": field,
                                "before": old[key].get(field), "after": new[key].get(field)})
    return changes

def call_face_freeze(label, selected, unfreeze=False, observed_faces=None, extra_cons=()):
    selected = list(selected)
    observed_faces = list(observed_faces or selected)
    before = freeze_snapshot(observed_faces, extra_cons)
    returned = base.FacesFreezeUnFreeze(selected, unfreeze=unfreeze)
    after = freeze_snapshot(observed_faces, extra_cons)
    return {"label": label, "unfreeze": unfreeze, "input_face_ids": ids(selected),
            "api_return": returned, "before": before, "after": after,
            "changes": state_changes(before, after)}

def volume_membership(volume):
    result = {}
    for kind in ("FACE", "SOLID", "SHELL"):
        try:
            result[kind] = ids(base.CollectEntities(deck, volume, kind, True))
        except Exception as exc:
            result[kind] = {"error": type(exc).__name__ + ": " + str(exc)}
    return result

def next_id(kind):
    current = entities(base.CollectEntities(deck, None, "__PROPERTIES__" if kind in ("PSHELL", "PSOLID") else "__ELEMENTS__" if kind in ("SHELL", "SOLID") else kind))
    return max([eid(item) for item in current] + [0]) + 1

def make_entity(kind, fields):
    value = base.CreateEntity(deck, kind, fields)
    if value is None:
        raise RuntimeError("CreateEntity returned None for " + kind + ": " + repr(fields))
    return value

faces = entities(base.CollectEntities(deck, None, "FACE"))
if len(faces) == 1:
    # The checked-in STEP is one rectangle. Two adjacent CAD macros are
    # sufficient to verify shared CONS and their later joined Macro.
    bounds = list(base.BoundBox(faces))
    axis = max(range(3), key=lambda i: bounds[i + 3] - bounds[i])
    for fraction in (0.5,):
        origin = [(bounds[i] + bounds[i + 3]) / 2.0 for i in range(3)]
        origin[axis] = bounds[axis] + fraction * (bounds[axis + 3] - bounds[axis])
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        current = entities(base.CollectEntities(deck, None, "FACE"))
        crossing = [face for face in current if base.BoundBox([face])[axis] + 1e-8 < origin[axis]
                    < base.BoundBox([face])[axis + 3] - 1e-8]
        if not crossing:
            continue
        returned = base.FacesPlaneCut(working_plane=[origin, normal], entities=crossing,
                                     produce_plane_faces=False, perform_topology=True,
                                     part=base.GetEntityPart(current[0]), property=None)
        if returned != 1:
            raise RuntimeError("synthetic rectangle split failed: " + repr(returned))
    faces = entities(base.CollectEntities(deck, None, "FACE"))
if len(faces) < 2:
    raise RuntimeError("synthetic STEP import produced fewer than two FACEs")
face_by_id = {eid(face): face for face in faces}
pair = None
for face in sorted(faces, key=eid):
    face_macro = macro_of(face)
    if face_macro is None:
        continue
    for cons in perimeters(face):
        adjacent = entities(base.GetFacesOfCons(cons))
        if len(adjacent) < 2:
            continue
        for other in adjacent:
            if eid(other) == eid(face):
                continue
            other_macro = macro_of(other)
            if other_macro is not None and eid(other_macro) != eid(face_macro):
                pair = (face, other, cons)
                break
        if pair:
            break
    if pair:
        break
if pair is None:
    raise RuntimeError("could not find two adjacent FACEs in distinct Macro areas")

face_a, face_b, shared_cons = pair
macro_a, macro_b = macro_of(face_a), macro_of(face_b)
outside = next((face for face in sorted(faces, key=eid)
                if eid(face) not in (eid(face_a), eid(face_b))
                and eid(macro_of(face)) not in (eid(macro_a), eid(macro_b))), None)
volume_entities = entities(base.CollectEntities(deck, None, "VOLUME"))
volume = volume_entities[0] if volume_entities else None
macro_mesh = {"api_return_type": None, "shell_ids": [], "error": None, "macro_id": eid(macro_a)}
try:
    mesh_result = mesh.Create4SidedMesh(
        [face_a], True, True, 60.0, "standard", "none", "off", "quad", "directional",
        only_unmeshed=True,
    )
    macro_mesh["api_return_type"] = type(mesh_result).__name__
    macro_mesh["shell_ids"] = ids(mesh.GetMacroShells([macro_a]))
except Exception as exc:
    macro_mesh["error"] = type(exc).__name__ + ": " + str(exc)
initial_relations = {
    "face_macro": {str(eid(face_a)): eid(macro_a), str(eid(face_b)): eid(macro_b)},
    "faces_of_macros": ids(base.GetFacesOfMacros([macro_a, macro_b])),
    "faces_of_shared_cons": ids(base.GetFacesOfCons(shared_cons)),
    "macros_of_shared_cons": ids(base.GetMacrosOfCons(shared_cons)),
    "perimeters_of_face_a": ids(base.PerimetersOfFace(face_a, "all")),
    "perimeters_of_macro_a": ids(mesh.PerimetersOfMacro([face_a])),
    "unmeshed_macro_id": eid(macro_b),
    "unmeshed_macro_shells": ids(mesh.GetMacroShells([macro_b])),
    "macro_mesh": macro_mesh,
    "volume_id": eid(volume),
    "volume_membership_before_fe": volume_membership(volume) if volume is not None else None,
}

freeze_probe = []
if RUN_NATIVE_PROBE:
    freeze_probe.append(call_face_freeze("single_FACE_freeze", [face_a], observed_faces=[face_a, face_b]))
    freeze_probe.append(call_face_freeze("neighbor_FACE_freeze", [face_b], observed_faces=[face_a, face_b]))
    freeze_probe.append(call_face_freeze("unfreeze_FACE_A_while_neighbor_B_frozen", [face_a],
                                         unfreeze=True, observed_faces=[face_a, face_b]))
    freeze_probe.append(call_face_freeze("unfreeze_FACE_B", [face_b], unfreeze=True,
                                         observed_faces=[face_a, face_b]))
    freeze_probe.append(call_face_freeze("double_FACE_freeze", [face_a, face_b],
                                         observed_faces=[face_a, face_b]))
    freeze_probe.append(call_face_freeze("double_FACE_unfreeze", [face_a, face_b], unfreeze=True,
                                         observed_faces=[face_a, face_b]))

shared_cons_id = eid(shared_cons)
macro_topology = {
    "native_join_applied": False,
    "macro_ids_for_faces": {str(eid(face_a)): eid(macro_a), str(eid(face_b)): eid(macro_b)},
    "external_shared_cons": [{"cons_id": shared_cons_id, "adjacent_face_ids": ids(base.GetFacesOfCons(shared_cons))}],
}
# JOIN changes topology and is a later capability. The freeze acceptance
# intentionally keeps these two native Macro areas independent.
joined_macro = macro_a

grid_positions = {
    "qa": [100.0, 0.0, 0.0], "qb": [101.0, 0.0, 0.0],
    "qc": [101.0, 1.0, 0.0], "qd": [100.0, 1.0, 0.0],
    "qe": [102.0, 0.0, 0.0], "qf": [102.0, 1.0, 0.0],
    "ta": [110.0, 0.0, 0.0], "tb": [111.0, 0.0, 0.0],
    "tc": [110.0, 1.0, 0.0], "td": [110.0, 0.0, 1.0],
}
mat_id = next_id("MAT1")
make_entity("MAT1", {"MID": mat_id, "E": 210000.0, "NU": 0.3, "RHO": 7.8e-9})
pshell_id = next_id("PSHELL")
make_entity("PSHELL", {"PID": pshell_id, "MID1": mat_id, "T": 1.0})
psolid_id = next_id("PSOLID")
make_entity("PSOLID", {"PID": psolid_id, "MID": mat_id})
grids = {}
for name, point in grid_positions.items():
    grids[name] = make_entity("GRID", {"X1": point[0], "X2": point[1], "X3": point[2]})
grid_ids = {name: eid(entity) for name, entity in grids.items()}
shell_a = make_entity("SHELL", {"PID": pshell_id, "type": "CQUAD4",
                                 "G1": grid_ids["qa"], "G2": grid_ids["qb"],
                                 "G3": grid_ids["qc"], "G4": grid_ids["qd"]})
shell_b = make_entity("SHELL", {"PID": pshell_id, "type": "CQUAD4",
                                 "G1": grid_ids["qb"], "G2": grid_ids["qe"],
                                 "G3": grid_ids["qf"], "G4": grid_ids["qc"]})
shell_attached = make_entity("SHELL", {"PID": pshell_id, "type": "CTRIA3",
                                        "G1": grid_ids["ta"], "G2": grid_ids["tb"],
                                        "G3": grid_ids["tc"]})
solid = make_entity("SOLID", {"PID": psolid_id, "type": "CTETRA",
                               "G1": grid_ids["ta"], "G2": grid_ids["tb"],
                               "G3": grid_ids["tc"], "G4": grid_ids["td"]})

def element_state(entity):
    result = card_state(entity)
    try:
        names = list(entity.card_fields(deck))
        selected = [name for name in names if name in ("EID", "PID", "type", "G1", "G2", "G3", "G4",
                                                       "G5", "G6", "G7", "G8")]
        result["connectivity_fields"] = base.GetEntityCardValues(deck, entity, selected)
    except Exception as exc:
        result["connectivity_error"] = type(exc).__name__ + ": " + str(exc)
    return result

def fe_snapshot(elements, node_entities):
    return {"shells": [element_state(item) for item in elements if item.ansa_type(deck) == "SHELL"],
            "solids": [element_state(item) for item in elements if item.ansa_type(deck) == "SOLID"],
            "grids": [card_state(item) for item in sorted(node_entities, key=eid)]}

def collect_element_grids(entity):
    try:
        value = base.CollectEntities(deck, entity, "GRID", True)
        return {"ids": ids(value), "returned_type": type(value).__name__}
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)}

all_test_shells = [shell_a, shell_b, shell_attached]
all_test_elements = all_test_shells + [solid]
all_test_grids = list(grids.values())
fe_initial = fe_snapshot(all_test_elements, all_test_grids)
relations_after_fe = {
    "macro_shell_ids": macro_mesh["shell_ids"],
    "shell_macro_for_macro_mesh": ids(mesh.GetShellMacro(
        [base.GetEntity(deck, "SHELL", ident) for ident in macro_mesh["shell_ids"]])),
    "shell_macro_for_detached_shells": ids(mesh.GetShellMacro(all_test_shells)),
    "shells_of_joined_unmeshed_macro": ids(mesh.GetMacroShells([joined_macro])),
    "element_to_grid": {str(eid(shell_a)): collect_element_grids(shell_a),
                        str(eid(shell_b)): collect_element_grids(shell_b),
                        str(eid(shell_attached)): collect_element_grids(shell_attached),
                        str(eid(solid)): collect_element_grids(solid)},
    "volume_membership_after_detached_fe": volume_membership(volume) if volume is not None else None,
}

element_freeze_probe = []
if RUN_NATIVE_PROBE:
    before = fe_snapshot(all_test_elements, all_test_grids)
    returned = base.FreezeElements(deck, "SHELL", [shell_a])
    after = fe_snapshot(all_test_elements, all_test_grids)
    element_freeze_probe.append({"label": "explicit_SHELL_freeze", "api_return": returned,
                                 "input_shell_id": eid(shell_a), "before": before, "after": after,
                                 "changes": state_changes(before, after)})
    before = after
    returned = base.UnFreezeElements(deck, "SHELL", [shell_a])
    after = fe_snapshot(all_test_elements, all_test_grids)
    element_freeze_probe.append({"label": "explicit_SHELL_unfreeze", "api_return": returned,
                                 "input_shell_id": eid(shell_a), "before": before, "after": after,
                                 "changes": state_changes(before, after)})
    before = after
    returned = base.FreezeElements(deck, "SOLID", [solid])
    after = fe_snapshot(all_test_elements, all_test_grids)
    element_freeze_probe.append({"label": "explicit_SOLID_freeze", "api_return": returned,
                                 "input_solid_id": eid(solid), "before": before, "after": after,
                                 "changes": state_changes(before, after)})
    before = after
    returned = base.UnFreezeElements(deck, "SOLID", [solid])
    after = fe_snapshot(all_test_elements, all_test_grids)
    element_freeze_probe.append({"label": "explicit_SOLID_unfreeze", "api_return": returned,
                                 "input_solid_id": eid(solid), "before": before, "after": after,
                                 "changes": state_changes(before, after)})

RESULT = {
    "deck": int(deck),
    "fixture": {"source": "tests/fixtures/rectangle-20x10-mm.step",
                "face_ids": ids(faces), "face_a_id": eid(face_a), "face_b_id": eid(face_b),
                "shared_cons_id_before_join": shared_cons_id,
                "outside_face_id": eid(outside), "volume_id": eid(volume),
                "macro_mesh": macro_mesh},
    "initial_relations": initial_relations,
    "face_freeze_probe": freeze_probe,
    "macro_topology": macro_topology,
    "fe_fixture": {"grid_ids": grid_ids, "shell_ids": [eid(item) for item in all_test_shells],
                   "solid_id": eid(solid), "initial_state": fe_initial},
    "relations_after_fe": relations_after_fe,
    "element_freeze_probe": element_freeze_probe,
}
'''


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _report_file(run_dir: Path, report: dict) -> Path:
    return run_dir / report.get("report_name", "mesh-scope-probe-report.json")


@asynccontextmanager
async def _harness(run_dir: Path, instructions: dict, identity: dict, helpers: tuple, report: dict):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    runtime_home = instructions["runtime_home"]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--direct-backend", "--runtime-home", runtime_home],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    with (run_dir / "mesh-scope-mcp.stderr.log").open("a", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                harness = stage.McpHarness(client, report, run_dir)
                listed = await client.list_tools()
                names = sorted(item.name for item in listed.tools)
                required = {"ansa_model_open", "ansa_model_save", "ansa_script_execute", "ansa_task_query",
                            "ansa_mesh_scope_read", "ansa_mesh_freeze"}
                if not required <= set(names):
                    raise RuntimeError("independent MCP session lacks required tools: " + repr(sorted(required - set(names))))
                if len(names) != 93:
                    raise RuntimeError(f"expected 93 tools in the current MCP server, received {len(names)}")
                report["mcp_schema"] = {"count": len(names), "required": sorted(required)}
                await stage._bind_for_run(harness, identity, *helpers[:2], identity["port"])
                yield harness


async def _task(harness, run_dir: Path, report: dict, name: str, fields: dict,
                *, timeout: int = 300, expected_status: str | None = "succeeded",
                operation_id: str | None = None, expected_target: dict | None = None,
                expected_model: dict | None = None) -> dict:
    operation_id = operation_id or "mesh-scope-" + report.get("phase", "probe") + "-" + uuid.uuid4().hex
    arguments = {
        "operation_id": operation_id,
        "expected_target": expected_target if expected_target is not None else harness.target,
        "expected_model": expected_model if expected_model is not None else harness.model,
        **fields,
    }
    report["pending"] = {"tool": name, "operation_id": operation_id}
    _write_json(_report_file(run_dir, report), report)
    submitted = await harness.call(name, arguments, allow_error=True)
    task_id = submitted.get("task_id")
    if not task_id:
        report["pending"] = None
        report["calls"].append({"tool": name, "operation_id": operation_id,
                                "submission": submitted, "status": submitted.get("status")})
        _write_json(_report_file(run_dir, report), report)
        raise RuntimeError(f"{name} returned no task ID: {submitted}")
    report["pending"]["task_id"] = task_id
    _write_json(_report_file(run_dir, report), report)
    completed = await harness.wait_task(task_id, expected=None, timeout_seconds=timeout)
    filename = f"{report.get('phase', 'probe')}-{len(report['calls']):02d}-{name}.json"
    _write_json(run_dir / filename, completed)
    report["calls"].append({"tool": name, "operation_id": operation_id,
                            "task_id": task_id, "status": completed["status"],
                            "receipt": filename})
    if completed["status"] in {"succeeded", "failed", "cancelled", "expired"}:
        report["pending"] = None
    _write_json(_report_file(run_dir, report), report)
    if expected_status is not None and completed["status"] != expected_status:
        raise RuntimeError(f"{name} finished {completed['status']}: {completed.get('error')}")
    return completed


def _result(completed: dict) -> dict:
    data = completed.get("data") or {}
    value = data.get("result") if isinstance(data, dict) else None
    if isinstance(value, dict) and isinstance(value.get("operation_result"), dict):
        return value["operation_result"]
    return value


async def _scope_read(harness, run_dir: Path, report: dict, deck: int,
                      entity_type: str, entity_ids: list[int], label: str) -> dict:
    completed = await _task(harness, run_dir, report, "ansa_mesh_scope_read", {
        "deck": deck, "entity_type": entity_type, "entity_ids": entity_ids,
    })
    result = _result(completed)
    if not isinstance(result, dict) or result.get("result_paged"):
        raise RuntimeError(f"{label} did not return inline mesh-scope evidence")
    digest = result.get("scope_sha256")
    if type(digest) is not str or len(digest) != 64:
        raise RuntimeError(f"{label} has no scope_sha256")
    report.setdefault("scope_reads", {})[label] = {
        "entity_type": entity_type, "entity_ids": list(entity_ids),
        "scope_sha256": digest,
        "summary": result.get("summary"),
        "freeze_targets": result.get("freeze_targets"),
    }
    _write_json(_report_file(run_dir, report), report)
    return result


def _freeze_state(scope: dict, section: str, entity_id: int) -> dict:
    row = next((item for item in scope.get(section, [])
                if item.get("entity", {}).get("id") == entity_id), None)
    if row is None:
        raise RuntimeError(f"{section} {entity_id} is absent from mesh-scope readback")
    state = row.get("freeze_state")
    if not isinstance(state, dict) or state.get("known") is not True or state.get("value") not in ("YES", "NO"):
        raise RuntimeError(f"{section} {entity_id} has no explicit YES/NO frozen state")
    return state


async def _freeze_call(harness, run_dir: Path, report: dict, deck: int,
                       entity_type: str, entity_ids: list[int], action: str,
                       scope_sha256: str, backup_path: Path, *, duplicate: bool = False) -> tuple[dict, dict, str]:
    operation_id = "mesh-scope-verify-" + uuid.uuid4().hex
    target, model = dict(harness.target), dict(harness.model)
    fields = {
        "deck": deck, "entity_type": entity_type, "entity_ids": entity_ids,
        "action": action, "expected_scope_sha256": scope_sha256,
        "backup_path": str(backup_path.resolve()),
    }
    arguments = {"operation_id": operation_id, "expected_target": target,
                 "expected_model": model, **fields}
    completed = await _task(
        harness, run_dir, report, "ansa_mesh_freeze", fields,
        operation_id=operation_id, expected_target=target, expected_model=model,
    )
    result = _result(completed)
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("scope_verified") is not True:
        raise RuntimeError(f"{entity_type} {action} was not scope-verified: {result or completed}")
    task_id = report["calls"][-1].get("task_id")
    if not task_id:
        raise RuntimeError("mesh freeze receipt lacks its original task_id")
    if duplicate:
        repeated = await harness.call("ansa_mesh_freeze", arguments, allow_error=True)
        if repeated.get("task_id") != task_id:
            raise RuntimeError("repeated operation_id did not return the original mesh-freeze task")
        report.setdefault("idempotency", []).append({
            "operation_id": operation_id, "original_task_id": task_id,
            "repeated_task_id": repeated.get("task_id"), "same_task": True,
        })
        _write_json(_report_file(run_dir, report), report)
    return completed, result, operation_id


def _verify_states(scope: dict, section: str, entity_ids: list[int], expected: str) -> None:
    for ident in entity_ids:
        state = _freeze_state(scope, section, ident)
        if state["value"] != expected:
            raise RuntimeError(f"{section} {ident} expected frozen={expected}, observed {state}")


async def _run_public_verify(run_dir: Path, instructions: dict, identity: dict,
                             helpers: tuple, report: dict, probe_report: dict) -> None:
    probe = probe_report.get("native_probe")
    if not isinstance(probe, dict):
        raise ValueError("probe report has no structured native fixture evidence")
    deck = probe["deck"]
    fixture = probe["fixture"]
    face_a, face_b = fixture["face_a_id"], fixture["face_b_id"]
    volume_id = fixture.get("volume_id")
    fe = probe["fe_fixture"]
    shell_a, shell_b, shell_attached = fe["shell_ids"]
    solid_id = fe["solid_id"]
    post_join = probe["macro_topology"]
    shared_rows = post_join.get("external_shared_cons", [])
    shared_cons_id = shared_rows[0]["cons_id"] if shared_rows else None
    if shared_cons_id is None:
        shared_after = post_join.get("shared_cons_after_join")
        if isinstance(shared_after, dict) and len(shared_after.get("adjacent_face_ids", [])) > 1:
            shared_cons_id = shared_after["cons_id"]
    if shared_cons_id is None:
        candidates = probe["initial_relations"].get("perimeters_of_face_a", [])
        before_join_id = fixture.get("shared_cons_id_before_join")
        shared_cons_id = next((ident for ident in candidates if ident != before_join_id), None)
    if shared_cons_id is None:
        raise RuntimeError("native probe did not leave a readable shared CONS for MCP scope read")

    async with _harness(run_dir, instructions, identity, helpers, report) as harness:
        face_before = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "face_initial")
        face_targets = face_before["freeze_targets"]["FACE"]
        if face_a not in face_before["freeze_effects"]["face_ids"]:
            raise RuntimeError(f"root Macro was not returned as the complete FACE freeze target: {face_targets}")
        cons_before = await _scope_read(harness, run_dir, report, deck, "CONS", [shared_cons_id], "shared_cons")
        shared = next((row for row in cons_before.get("cons", []) if row["entity"]["id"] == shared_cons_id), None)
        if shared is None or len(shared.get("face_ids", [])) < 2:
            raise RuntimeError("CONS scope did not return its shared FACE membership")
        shell_before = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_a], "explicit_shell")
        solid_before = await _scope_read(harness, run_dir, report, deck, "SOLID", [solid_id], "explicit_solid")
        outside_face_id = fixture.get("outside_face_id")
        if outside_face_id is not None:
            outside_before = await _scope_read(harness, run_dir, report, deck, "FACE",
                                               [outside_face_id], "unmeshed_macro")
            primary = [row for row in outside_before["macros"] if row.get("primary")]
            if primary and any(row.get("shell_ids") for row in primary):
                raise RuntimeError("unmeshed fixture Macro unexpectedly has native SHELL members")
        if volume_id is not None:
            await _scope_read(harness, run_dir, report, deck, "VOLUME", [volume_id], "volume_membership")

        outside_id = next((row["entity"]["id"] for row in face_before["faces"]
                           if row["entity"]["id"] not in face_targets), None)
        outside_state_before = (_freeze_state(face_before, "faces", outside_id)
                                if outside_id is not None else None)
        _, freeze_result, freeze_operation_id = await _freeze_call(
            harness, run_dir, report, deck, "FACE", [face_a], "freeze",
            face_before["scope_sha256"], run_dir / "before-public-face-freeze.ansa", duplicate=True,
        )
        if freeze_result.get("freeze_targets", {}).get("FACE") != face_targets:
            raise RuntimeError("FACE freeze receipt differs from the readback Macro closure")
        await harness.snapshot_context()
        face_frozen = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "face_frozen")
        _verify_states(face_frozen, "faces", face_targets, "YES")
        _verify_states(face_frozen, "cons", freeze_result["freeze_targets"]["CONS"], "YES")
        old_cons = {row["entity"]["id"]: row["freeze_state"] for row in face_before["cons"]}
        for row in face_frozen["cons"]:
            if row["freeze_state"] != old_cons[row["entity"]["id"]]:
                raise RuntimeError("Macro freeze changed a literal CONS Frozen flag")
            if row["entity"]["id"] in face_before["freeze_effects"]["cons_ids"] and row["effective_macro_protection"]["value"] is not True:
                raise RuntimeError("Macro boundary protection was not reflected in scope readback")
        if outside_id is not None and outside_state_before is not None:
            outside_state_after = _freeze_state(face_frozen, "faces", outside_id)
            if outside_state_after["value"] != outside_state_before["value"]:
                raise RuntimeError("freezing the selected Macro changed an outside FACE")
        if face_frozen["scope_sha256"] != freeze_result.get("scope_sha256_after"):
            raise RuntimeError("FACE freeze receipt hash differs from independent scope readback")

        await _task(harness, run_dir, report, "ansa_model_save", {
            "path": str(run_dir / "mesh-scope-frozen-checkpoint.ansa"),
        })
        await harness.snapshot_context()
        await _task(harness, run_dir, report, "ansa_model_open", {
            "path": str(run_dir / "mesh-scope-frozen-checkpoint.ansa"),
            "preservation": "backup",
            "backup_path": str(run_dir / "before-mesh-scope-reopen.ansa"),
        })
        await harness.snapshot_context()
        face_reopened = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "face_after_reopen")
        _verify_states(face_reopened, "faces", face_targets, "YES")
        _verify_states(face_reopened, "cons", freeze_result["freeze_targets"]["CONS"], "YES")

        stale_result = await _task(harness, run_dir, report, "ansa_mesh_freeze", {
            "deck": deck, "entity_type": "FACE", "entity_ids": [face_a], "action": "unfreeze",
            "expected_scope_sha256": face_before["scope_sha256"],
            "backup_path": str((run_dir / "must-not-be-used-stale-face.ansa").resolve()),
        }, expected_status=None)
        stale_value = _result(stale_result)
        if isinstance(stale_value, dict):
            if stale_value.get("ok") is not False or stale_value.get("api_called") is not False or stale_value.get("backup_attempted") is not False:
                raise RuntimeError(f"stale FACE scope was not rejected before backup/API: {stale_value}")
        elif "stale" not in str(stale_result.get("error", "")).casefold():
            raise RuntimeError("stale FACE scope rejection has no matching diagnostic")
        await harness.snapshot_context()
        still_frozen = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "face_after_stale_reject")
        _verify_states(still_frozen, "faces", face_targets, "YES")

        _, unfreeze_result, _ = await _freeze_call(
            harness, run_dir, report, deck, "FACE", [face_a], "unfreeze",
            still_frozen["scope_sha256"], run_dir / "before-public-face-unfreeze.ansa",
        )
        await harness.snapshot_context()
        face_unfrozen = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "face_unfrozen")
        _verify_states(face_unfrozen, "faces", unfreeze_result["freeze_targets"]["FACE"], "NO")
        _verify_states(face_unfrozen, "cons", unfreeze_result["freeze_targets"]["CONS"], "NO")

        shell_peer_before = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_b], "shell_peer_before")
        shell_before = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_a], "shell_before_freeze")
        _, shell_freeze, _ = await _freeze_call(
            harness, run_dir, report, deck, "SHELL", [shell_a], "freeze",
            shell_before["scope_sha256"], run_dir / "before-public-shell-freeze.ansa",
        )
        await harness.snapshot_context()
        shell_frozen = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_a], "shell_frozen")
        _verify_states(shell_frozen, "shells", [shell_a], "YES")
        shell_peer_after = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_b], "shell_peer_after")
        if _freeze_state(shell_peer_before, "shells", shell_b)["value"] != _freeze_state(shell_peer_after, "shells", shell_b)["value"]:
            raise RuntimeError("explicit SHELL freeze changed the adjacent outside SHELL")
        _, _, _ = await _freeze_call(
            harness, run_dir, report, deck, "SHELL", [shell_a], "unfreeze",
            shell_frozen["scope_sha256"], run_dir / "before-public-shell-unfreeze.ansa",
        )
        await harness.snapshot_context()

        solid_peer_before = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_attached], "solid_shell_before")
        solid_before = await _scope_read(harness, run_dir, report, deck, "SOLID", [solid_id], "solid_before_freeze")
        _, _, _ = await _freeze_call(
            harness, run_dir, report, deck, "SOLID", [solid_id], "freeze",
            solid_before["scope_sha256"], run_dir / "before-public-solid-freeze.ansa",
        )
        await harness.snapshot_context()
        solid_frozen = await _scope_read(harness, run_dir, report, deck, "SOLID", [solid_id], "solid_frozen")
        _verify_states(solid_frozen, "solids", [solid_id], "YES")
        solid_shell_after = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_attached], "solid_shell_after")
        if _freeze_state(solid_peer_before, "shells", shell_attached)["value"] != _freeze_state(solid_shell_after, "shells", shell_attached)["value"]:
            raise RuntimeError("explicit SOLID freeze changed an adjacent SHELL")
        _, _, _ = await _freeze_call(
            harness, run_dir, report, deck, "SOLID", [solid_id], "unfreeze",
            solid_frozen["scope_sha256"], run_dir / "before-public-solid-unfreeze.ansa",
        )
        await harness.snapshot_context()

        await _task(harness, run_dir, report, "ansa_model_save", {
            "path": str(run_dir / "mesh-scope-public-final.ansa"),
        })
        await harness.snapshot_context()
        await _task(harness, run_dir, report, "ansa_model_open", {
            "path": str(run_dir / "mesh-scope-public-final.ansa"),
            "preservation": "backup",
            "backup_path": str(run_dir / "before-mesh-scope-final-reopen.ansa"),
        })
        await harness.snapshot_context()
        final_face = await _scope_read(harness, run_dir, report, deck, "FACE", [face_a], "final_face_reopen")
        _verify_states(final_face, "faces", final_face["freeze_targets"]["FACE"], "NO")
        final_shell = await _scope_read(harness, run_dir, report, deck, "SHELL", [shell_a], "final_shell_reopen")
        _verify_states(final_shell, "shells", [shell_a], "NO")
        final_solid = await _scope_read(harness, run_dir, report, deck, "SOLID", [solid_id], "final_solid_reopen")
        _verify_states(final_solid, "solids", [solid_id], "NO")
        report["complete"] = True
        report["face_freeze_operation_id"] = freeze_operation_id
        _write_json(_report_file(run_dir, report), report)


async def _run_probe(run_dir: Path, instructions: dict, identity: dict, helpers: tuple,
                     report: dict, fixture_path: Path) -> None:
    async with _harness(run_dir, instructions, identity, helpers, report) as harness:
        await _task(harness, run_dir, report, "ansa_model_import", {
            "path": str(fixture_path), "format": "STEP", "mode": "replace", "preservation": "backup",
            "backup_path": str(run_dir / "before-synthetic-step-import.ansa"),
        })
        await harness.snapshot_context()
        await _task(harness, run_dir, report, "ansa_model_save", {
            "path": str(run_dir / "mesh-scope-probe-before-native.ansa"),
        })
        await harness.snapshot_context()

        script = "RUN_NATIVE_PROBE = True\n" + FIXTURE_SCRIPT
        report["native_probe_started"] = True
        _write_json(_report_file(run_dir, report), report)
        try:
            completed = await _task(harness, run_dir, report, "ansa_script_execute", {
                "script": script, "validate": True,
            }, timeout=300)
            script_result = _result(completed)
            probe_result = script_result.get("result") if isinstance(script_result, dict) else None
            if not isinstance(probe_result, dict):
                raise RuntimeError("native probe did not return a structured RESULT")
            report["native_probe"] = probe_result
            report["native_probe_finished"] = True
        finally:
            if report.get("pending") is None:
                await harness.snapshot_context()
                await _task(harness, run_dir, report, "ansa_model_save", {
                    "path": str(run_dir / "mesh-scope-probe-after-native.ansa"),
                })
                report["model_saved_after_native"] = True
                await harness.snapshot_context()
        _write_json(_report_file(run_dir, report), report)


def _resume_owned_ansa(instructions: dict, probe_report: dict) -> tuple[dict, tuple, dict]:
    import grpc
    from ansa_mcp_shared.identity import process_start_filetime, same_process_alive

    identity = probe_report.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("probe report has no owned ANSA identity")
    stage_one, stage_two = stage._import_runtime_helpers()
    original_channel = stage._bounded_grpc(grpc, stage_one.DeadlineInterceptor)
    try:
        pid, process_start = identity.get("pid"), identity.get("process_start")
        if not same_process_alive(pid, process_start):
            raise RuntimeError("saved owned ANSA PID/start time is no longer alive")
        basic = stage_two.rpc(int(identity["port"]), stage_one.remote_basic)
        stage._verify_program_arguments(basic, Path(instructions["translator"]), int(instructions["port"]))
        if basic.get("pid") != pid or basic.get("program_args") != identity.get("program_args"):
            raise RuntimeError("live ANSA PID or complete launch arguments differ from the probe identity")
        if process_start_filetime(pid) != process_start:
            raise RuntimeError("live ANSA process start time differs from the probe identity")
        return identity, (stage_one, stage_two, original_channel), basic
    except Exception:
        grpc.insecure_channel = original_channel
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("probe", "verify"), default="probe")
    parser.add_argument("--run-dir", required=True, type=Path,
                        help="stage_three_ansa.py --prepare-only 生成的同一 owned-instance 运行目录")
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step")
    parser.add_argument("--runtime-home", type=Path,
                        help="用户私有 ANSA MCP 控制目录；必须位于当前用户目录")
    parser.add_argument("--keep-owned-ansa", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=60,
                        help="等待本次独立实例就绪的秒数（1—60）；不影响原生任务的查询期限")
    args = parser.parse_args()
    if not 1 <= args.startup_timeout <= 60:
        parser.error("--startup-timeout必须为1—60秒")
    stage.PANEL_WAIT_SECONDS = args.startup_timeout

    run_dir = args.run_dir.expanduser().resolve(strict=True)
    instructions_path = run_dir / "launch_instructions.json"
    if not instructions_path.is_file():
        raise FileNotFoundError("run-dir lacks launch_instructions.json")
    instructions = json.loads(instructions_path.read_text(encoding="utf-8"))
    if Path(instructions["run_dir"]).resolve() != run_dir:
        raise ValueError("launch instructions belong to a different run directory")
    if args.runtime_home is not None:
        instructions["runtime_home"] = str(stage._runtime_home(args.runtime_home))
        _write_json(instructions_path, instructions)
    fixture_path = args.fixture.expanduser().resolve(strict=True)
    probe_report_path = run_dir / "mesh-scope-probe-report.json"
    report_path = run_dir / ("mesh-scope-probe-report.json" if args.phase == "probe"
                             else "mesh-scope-mcp-report.json")
    if report_path.exists():
        raise FileExistsError("phase report already exists; query its original tasks instead of replaying")
    probe_report = None
    if args.phase == "verify":
        if not probe_report_path.is_file():
            raise FileNotFoundError("verify phase requires the completed probe report in this run-dir")
        probe_report = json.loads(probe_report_path.read_text(encoding="utf-8"))
        if (probe_report.get("status") != "complete" or probe_report.get("native_probe_finished") is not True
                or probe_report.get("model_saved_after_native") is not True
                or probe_report.get("cleanup", {}).get("stopped_owned_process") is True):
            raise ValueError("verify phase requires a completed, saved probe with the same owned ANSA still running")
    report = {
        "status": "running", "phase": args.phase, "report_name": report_path.name,
        "run_dir": str(run_dir), "runtime_home": instructions["runtime_home"],
        "fixture": str(fixture_path),
        "started_at": stage.datetime.now(stage.timezone.utc).isoformat(),
        "calls": [], "pending": None, "native_probe_started": False,
        "native_probe_finished": False, "cleanup": None,
    }
    _write_json(report_path, report)
    helpers = None
    try:
        if args.phase == "probe":
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run_dir, instructions)
        else:
            identity, helpers, basic = _resume_owned_ansa(instructions, probe_report)
            report["owned_process_reverified"] = {
                "pid": basic["pid"], "process_start": identity["process_start"],
                "translator": instructions["translator"], "port": instructions["port"],
                "main_thread": basic.get("main_thread"),
            }
        report["identity"] = identity
        _write_json(report_path, report)
        if args.phase == "probe":
            asyncio.run(_run_probe(run_dir, instructions, identity, helpers, report, fixture_path))
        else:
            asyncio.run(_run_public_verify(run_dir, instructions, identity, helpers, report, probe_report))
            report["model_saved_after_verify"] = True
        report["status"] = "complete"
        return_code = 0
    except Exception:
        report["error"] = traceback.format_exc()
        report["status"] = "failed"
        return_code = 1
    finally:
        safe_to_stop = (report.get("pending") is None and (
            (args.phase == "probe" and
             (not report.get("native_probe_started") or report.get("model_saved_after_native") is True))
            or (args.phase == "verify" and report.get("complete") is True
                and report.get("model_saved_after_verify") is True)
        ))
        if helpers and report.get("identity") and safe_to_stop and not args.keep_owned_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(report["identity"], instructions, helpers)
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
                report["status"] = "cleanup_failed"
                return_code = 1
        elif report.get("identity"):
            report["cleanup"] = {"stopped_owned_process": False,
                                 "reason": "keep requested, operation remains nonterminal, or modified model was not saved"}
        _write_json(report_path, report)
    print(json.dumps({"status": report["status"], "report": str(report_path),
                      "run_dir": str(run_dir), "cleanup": report.get("cleanup")}, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
