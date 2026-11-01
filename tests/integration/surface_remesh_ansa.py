"""Owned-instance existing-Macro surface-remesh acceptance; never starts ANSA."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "integration")]

import mesh_scope_ansa as scope_api
import perimeter_edit_ansa as perimeter
import stage_three_ansa as stage


REPORT_NAME = "surface-remesh-report.json"
IDENTITY_NAME = "owned-identity.json"
RECTANGLE_STEP = ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step"
TARGET_LENGTH = 2.0


FIXTURE_SCRIPT = perimeter.FIXTURE_SCRIPT + r'''
created = mesh.Create4SidedMesh(
    [face_a, face_b], True, True, 60.0, "standard", "none", "off",
    "quad", "directional", only_unmeshed=False, protect_selection_bounds=True)
macro_mesh = {}
for label, macro in (("left", macro_a), ("right", macro_b)):
    shells = entities(mesh.GetMacroShells([macro]))
    ids_now = sorted(eid(shell) for shell in shells)
    types = sorted({str(base.GetEntityCardValues(deck, shell, ["type"])["type"]).upper()
                    for shell in shells})
    if len(ids_now) != 16 or types != ["CQUAD4"]:
        raise RuntimeError(label + " Macro is not 16 CQUAD4")
    macro_mesh[label] = {"macro_id": eid(macro), "shell_ids": ids_now}
RESULT["surface_remesh_fixture"] = {
    "api_return_type": type(created).__name__,
    "meshed_face_ids": ids([face_a, face_b]),
    "macro_mesh": macro_mesh,
    "fe_after_macro_mesh": fe_snapshot(all_test_elements, all_test_grids)}
json.dumps(RESULT["surface_remesh_fixture"], allow_nan=False)
'''


MESH_READ_SCRIPT = r'''import json
from ansa import base, constants, mesh
deck = constants.NASTRAN
spec = json.loads(SPEC_JSON)
def ids(value):
    if value is None or value == 0: return []
    if not isinstance(value, (list, tuple)): value = [value]
    return sorted({int(item._id if hasattr(item, "_id") else item) for item in value})
def card(entity):
    names = [str(name) for name in entity.card_fields(deck)]
    values = base.GetEntityCardValues(deck, entity, names)
    return {"entity": {"type": entity.ansa_type(deck), "id": int(entity._id)},
            "card_fields": names, "fields": {str(k): v for k, v in values.items()}}
result = {}
for label, face_id in spec["faces"].items():
    face = base.GetEntity(deck, "FACE", face_id)
    macro = base.GetFaceMacro(face)
    shells = [base.GetEntity(deck, "SHELL", ident)
              for ident in ids(mesh.GetMacroShells([macro]))]
    rows, nodes, owners = {}, set(), {}
    for shell in shells:
        row = card(shell)
        kind = str(row["fields"].get("type", "")).upper()
        connect = [row["fields"].get("G" + str(i)) for i in range(1, 9)]
        connect = [value for value in connect if type(value) is int and value > 0]
        if (kind == "CQUAD4" and len(connect) != 4) or (
                kind == "CTRIA3" and len(connect) != 3):
            raise RuntimeError(kind + " connectivity is incomplete")
        if kind not in ("CQUAD4", "CTRIA3"):
            raise RuntimeError("unsupported target SHELL type: " + kind)
        nodes.update(connect)
        rows[str(row["entity"]["id"])] = row
        owners[str(row["entity"]["id"])] = ids(mesh.GetShellMacro([shell]))
    result[label] = {
        "face_id": face_id, "macro_id": int(macro._id),
        "shell_ids": sorted(int(key) for key in rows), "shells": rows,
        "shell_macro_ids": owners, "counts": {
            "quad4": sum(row["fields"].get("type") == "CQUAD4" for row in rows.values()),
            "tria3": sum(row["fields"].get("type") == "CTRIA3" for row in rows.values())},
        "grids": {str(ident): card(base.GetEntity(deck, "GRID", ident))
                  for ident in sorted(nodes)}}
RESULT = result
'''


CONFIG_SCRIPT = r'''import builtins
from ansa import base, mesh
from ansa_mcp_bridge.operations import meshing_context
RESULT = meshing_context.capture(builtins._ansa_mcp_plugin_bridge.state, base, mesh)
'''


EMBEDDED_SCRIPTS = (FIXTURE_SCRIPT, MESH_READ_SCRIPT,
                    perimeter.FE_READ_SCRIPT, CONFIG_SCRIPT)
_leaf = scope_api._result
_digest = perimeter._digest
_task = perimeter._task
_script = perimeter._script


def _with_spec(source, spec):
    return "SPEC_JSON = " + repr(json.dumps(spec, separators=(",", ":"))) + "\n" + source


async def _condition(harness, run_dir, report, deck, faces, label):
    value = _leaf(await _task(
        harness, run_dir, report, "ansa_mesh_faces",
        {"deck": deck, "face_ids": faces}, "faces-" + label))
    conditions = [row.get("condition") for row in value.get("faces", [])]
    if len(conditions) != len(faces) or any(not isinstance(item, dict) for item in conditions):
        raise RuntimeError("ansa_mesh_faces returned incomplete conditions")
    return conditions


async def _scope(harness, run_dir, report, deck, face_id, label):
    value = _leaf(await _task(
        harness, run_dir, report, "ansa_mesh_scope_read",
        {"deck": deck, "entity_type": "FACE", "entity_ids": [face_id]},
        "scope-" + label))
    if value.get("ok") is not True:
        raise RuntimeError("mesh scope read failed")
    _digest(value, "scope_sha256")
    return value


async def _bindings(harness, run_dir, report, deck, face_id, label):
    condition = (await _condition(
        harness, run_dir, report, deck, [face_id], label))[0]
    scope = await _scope(harness, run_dir, report, deck, face_id, label)
    faces = sorted(row["entity"]["id"] for row in scope["faces"])
    cons = sorted(scope["freeze_effects"]["cons_ids"])
    if not faces or len(faces) > 20 or not cons or len(cons) > 32:
        raise RuntimeError("scope-derived seed range exceeds MCP bounds")
    seed = _leaf(await _task(
        harness, run_dir, report, "ansa_mesh_seed_read",
        {"deck": deck, "face_ids": faces, "cons_ids": cons}, "seed-" + label))
    _digest(seed, "seed_snapshot_sha256")
    report.setdefault("bindings", {})[label] = {
        "face_ids": faces, "cons_ids": cons,
        "scope_sha256": scope["scope_sha256"],
        "seed_snapshot_sha256": seed["seed_snapshot_sha256"]}
    return condition, scope, seed


async def _mesh(harness, run_dir, report, faces, label):
    return await _script(harness, run_dir, report,
                         _with_spec(MESH_READ_SCRIPT, {"faces": faces}), "mesh-" + label)


async def _fe(harness, run_dir, report, spec, label):
    return await _script(harness, run_dir, report,
                         _with_spec(perimeter.FE_READ_SCRIPT, spec), "fe-" + label)


async def _config(harness, run_dir, report, label):
    value = await _script(harness, run_dir, report, CONFIG_SCRIPT, "config-" + label)
    _digest(value, "snapshot_sha256")
    return value


def _verify_mesh(mesh, mode, result=None):
    target = mesh.get("target")
    if not isinstance(target, dict) or not target.get("shell_ids"):
        raise RuntimeError("target Macro mesh is empty")
    counts = target["counts"]
    allowed = {"quad": counts["tria3"] == 0 and counts["quad4"] > 0,
               "tria": counts["quad4"] == 0 and counts["tria3"] > 0,
               "mixed": counts["quad4"] + counts["tria3"] > 0}[mode]
    if not allowed:
        raise RuntimeError(mode + " case has incompatible element types")
    if any(ids != [target["macro_id"]] for ids in target["shell_macro_ids"].values()):
        raise RuntimeError("replacement SHELL ownership is not the target Macro")
    if result is None:
        return counts
    for key in ("ok", "scope_verified", "boundary_preserved", "relations_verified"):
        if result.get(key) is not True:
            raise RuntimeError("remesh result has " + key + "!=true")
    context_result = result.get("context_result")
    if not isinstance(context_result, dict) or context_result.get("restored") is not True:
        raise RuntimeError("meshing context was not restored")
    result_ids = result.get("result_shell_ids")
    if not isinstance(result_ids, list) or sorted(result_ids) != target["shell_ids"]:
        raise RuntimeError("result_shell_ids differ from independent ownership")
    if result.get("result_shell_count") != len(result_ids):
        raise RuntimeError("result_shell_count differs from result_shell_ids")
    _digest(result, "scope_sha256_after")
    reported = {"quad4": 0, "tria3": 0}
    for key, value in result.get("element_counts", {}).items():
        if type(value) is not int or value < 0:
            raise RuntimeError("element_counts values must be integers")
        if "quad" in str(key).casefold(): reported["quad4"] += value
        elif "tri" in str(key).casefold(): reported["tria3"] += value
    if reported != {"quad4": counts["quad4"], "tria3": counts["tria3"]}:
        raise RuntimeError("element_counts differ from independent shell cards")
    return counts


def _request(harness, deck, condition, scope, seed, algorithm, kind, backup,
             *, scope_sha256=None):
    return {
        "operation_id": perimeter._operation("surface-request"),
        "expected_target": dict(harness.target), "expected_model": dict(harness.model),
        "deck": deck, "faces": [condition], "method": "remesh_existing",
        "remesh": {
            "algorithm": algorithm, "element_type": kind,
            "element_order": "1st Order", "boundary_policy": "preserve",
            "target_length": TARGET_LENGTH,
            "expected_scope_sha256": scope_sha256 or scope["scope_sha256"],
            "expected_seed_snapshot_sha256": seed["seed_snapshot_sha256"],
            "backup_path": str(backup.resolve())}}


async def _remesh_task(harness, run_dir, report, request, *, expected="succeeded"):
    fields = {key: value for key, value in request.items()
              if key not in ("operation_id", "expected_target", "expected_model")}
    return await scope_api._task(
        harness, run_dir, report, "ansa_mesh_surface", fields,
        operation_id=request["operation_id"], expected_status=expected)


def _failed(completed, backup):
    value = _leaf(completed)
    if completed.get("status") != "failed" or value.get("ok") is not False:
        raise RuntimeError("expected failed preflight")
    if value.get("api_called") is True or value.get("possible_effects") is True:
        raise RuntimeError("preflight rejection reached native/effects")
    if backup.exists():
        raise RuntimeError("preflight rejection created its backup")
    return value


async def _case(harness, run_dir, report, deck, face_map, fe_spec,
                label, algorithm, kind, baseline):
    condition, scope, seed = await _bindings(
        harness, run_dir, report, deck, face_map["left"], label + "-before")
    backup = run_dir / ("before-surface-remesh-" + label + ".ansa")
    request = _request(
        harness, deck, condition, scope, seed, algorithm, kind, backup)
    completed = await _remesh_task(harness, run_dir, report, request)
    task_id = report["calls"][-1]["task_id"]
    result = _leaf(completed)
    fresh_scope = await _scope(
        harness, run_dir, report, deck, face_map["left"], label + "-after")
    if result.get("scope_sha256_after") != fresh_scope["scope_sha256"]:
        raise RuntimeError(label + " scope_sha256_after mismatch")
    post_condition = (await _condition(
        harness, run_dir, report, deck, [face_map["left"]], label + "-after"))[0]
    post = await _mesh(harness, run_dir, report, face_map, label + "-after")
    counts = _verify_mesh({"target": post["left"]}, kind, result)
    if post["right"] != baseline["mesh"]["right"]:
        raise RuntimeError(label + " changed the right Macro")
    if await _fe(harness, run_dir, report, fe_spec, label) != baseline["fe"]:
        raise RuntimeError(label + " changed independent FE")
    if sorted(post_condition["expected_shell_ids"]) != post["left"]["shell_ids"]:
        raise RuntimeError(label + " FACE membership changed")
    await harness.snapshot_context()
    return {
        "request": request, "task_id": task_id, "result": result, "counts": counts,
        "scope_sha256_after": fresh_scope["scope_sha256"],
        "backup": str(backup.resolve()), "mesh": post}


async def _reopen_check(harness, run_dir, report, deck, face_map, fe_spec,
                        baseline_mesh, baseline_fe, baseline_config):
    final_model = run_dir / "surface-remesh-final.ansa"
    await _task(harness, run_dir, report, "ansa_model_save",
                {"path": str(final_model.resolve())}, "save-final")
    await harness.snapshot_context()
    include = await _script(harness, run_dir, report, r'''from ansa import base, constants
entity = base.CreateEntity(constants.NASTRAN, "INCLUDE", {"Name": "remesh-rejection-fixture.nas"})
if entity is None: raise RuntimeError("INCLUDE fixture creation failed")
RESULT = {"id": int(entity._id), "type": entity.ansa_type(constants.NASTRAN)}
''', "include-fixture")
    if include.get("type") != "INCLUDE":
        raise RuntimeError("native Include fixture identity is unavailable")
    condition, scope, seed = await _bindings(
        harness, run_dir, report, deck, face_map["left"], "include-current")
    backup = run_dir / "must-not-exist-include-surface-remesh.ansa"
    request = _request(harness, deck, condition, scope, seed, "FREE", "quad", backup)
    rejected = _failed(await _remesh_task(
        harness, run_dir, report, request, expected="failed"), backup)
    if "INCLUDE" not in rejected.get("error", ""):
        raise RuntimeError("Include rejection did not identify the membership gate")
    report["include_rejection"] = rejected
    # Reopen the explicitly saved successful mesh; the test Include is retained
    # in the preservation backup instead of being removed by an unsafe cleanup.
    await _task(
        harness, run_dir, report, "ansa_model_open",
        {"path": str(final_model.resolve()), "preservation": "backup",
         "backup_path": str((run_dir / "before-surface-remesh-reopen.ansa").resolve())},
        "reopen-final")
    await harness.snapshot_context()
    condition = (await _condition(
        harness, run_dir, report, deck, [face_map["left"]], "reopened"))[0]
    mesh = await _mesh(harness, run_dir, report, face_map, "reopened")
    fe = await _fe(harness, run_dir, report, fe_spec, "reopened")
    config = await _config(harness, run_dir, report, "reopened")
    includes = _leaf(await _task(harness, run_dir, report, "ansa_model_includes",
                                {"deck": deck}, "reopened-includes"))
    if includes.get("total") != 0:
        raise RuntimeError("test Include remained after explicit checkpoint reopen")
    if mesh != baseline_mesh or fe != baseline_fe:
        raise RuntimeError("mesh cards/coordinates or independent FE changed on reopen")
    if config["snapshot_sha256"] != baseline_config["snapshot_sha256"]:
        raise RuntimeError("complete meshing configuration changed on reopen")
    if sorted(condition["expected_shell_ids"]) != mesh["left"]["shell_ids"]:
        raise RuntimeError("FACE ownership changed on reopen")
    return {"path": str(final_model.resolve()), "mesh": mesh, "fe": fe,
            "configuration": config, "face_condition": condition}


async def _verify(run_dir, instructions, identity, helpers, report):
    async with scope_api._harness(run_dir, instructions, identity, helpers, report) as h:
        await _task(h, run_dir, report, "ansa_model_import", {
            "path": str(RECTANGLE_STEP), "format": "STEP", "mode": "replace",
            "preservation": "backup",
            "backup_path": str((run_dir / "before-surface-remesh-step.ansa").resolve())},
            "step-import")
        await h.snapshot_context()
        await _task(h, run_dir, report, "ansa_model_save",
                    {"path": str((run_dir / "surface-remesh-fixture-start.ansa").resolve())},
                    "save-fixture-start")
        await h.snapshot_context()
        fixture = await _script(h, run_dir, report, FIXTURE_SCRIPT, "fixture")
        report["fixture"] = fixture
        await h.snapshot_context()
        await _task(h, run_dir, report, "ansa_model_save",
                    {"path": str((run_dir / "surface-remesh-fixture.ansa").resolve())},
                    "save-fixture")
        await h.snapshot_context()
        deck, left, right = fixture["deck"], fixture["fixture"]["face_a_id"], fixture["fixture"]["face_b_id"]
        face_map = {"left": left, "right": right}
        conditions = await _condition(h, run_dir, report, deck, [left, right], "fixture")
        if any(len(row["expected_shell_ids"]) != 16 for row in conditions):
            raise RuntimeError("fixture FACE conditions are not 16 SHELL each")
        mesh = await _mesh(h, run_dir, report, face_map, "baseline")
        _verify_mesh({"target": mesh["left"]}, "quad")
        _verify_mesh({"target": mesh["right"]}, "quad")
        fe_spec = {"shell_ids": fixture["fe_fixture"]["shell_ids"],
                   "solid_id": fixture["fe_fixture"]["solid_id"],
                   "grid_ids": sorted(set(fixture["fe_fixture"]["grid_ids"].values()))}
        baseline = {"mesh": mesh,
                    "fe": await _fe(h, run_dir, report, fe_spec, "baseline"),
                    "config": await _config(h, run_dir, report, "baseline")}
        stale_scope = None
        for label, algorithm, kind in (
                ("free-quad", "FREE", "quad"),
                ("free-mixed", "FREE", "mixed"),
                ("free-tria", "FREE", "tria"),
                ("mapped-quad", "mapped", "quad")):
            if label == "mapped-quad":
                stale_scope = await _scope(h, run_dir, report, deck, left, label + "-stale")
            case = await _case(
                h, run_dir, report, deck, face_map, fe_spec, label,
                algorithm, kind, baseline)
            report["cases"][label] = case
            if label == "free-quad":
                report["pending"] = {
                    "tool": "ansa_mesh_surface",
                    "operation_id": case["request"]["operation_id"],
                    "task_id": case["task_id"],
                    "idempotency_check": True}
                scope_api._write_json(run_dir / REPORT_NAME, report)
                repeated = await h.call("ansa_mesh_surface", case["request"], allow_error=True)
                if repeated.get("task_id") != case["task_id"]:
                    raise RuntimeError("same operation_id created another task")
                report["pending"] = None
                report["idempotency"] = {
                    "operation_id": case["request"]["operation_id"],
                    "task_id": repeated["task_id"]}

        condition, scope, seed = await _bindings(
            h, run_dir, report, deck, left, "stale-current")
        stale_backup = run_dir / "must-not-exist-stale-surface-remesh.ansa"
        request = _request(
            h, deck, condition, scope, seed, "FREE", "quad", stale_backup,
            scope_sha256=stale_scope["scope_sha256"])
        report["stale_scope_rejection"] = _failed(
            await _remesh_task(h, run_dir, report, request, expected="failed"),
            stale_backup)
        await h.snapshot_context()

        frozen_scope = await _scope(h, run_dir, report, deck, left, "freeze")
        _, _, _ = await scope_api._freeze_call(
            h, run_dir, report, deck, "FACE", [left], "freeze",
            frozen_scope["scope_sha256"],
            run_dir / "before-surface-remesh-freeze.ansa")
        await h.snapshot_context()
        condition, scope, seed = await _bindings(
            h, run_dir, report, deck, left, "frozen-current")
        frozen_face = next(row for row in scope["faces"]
                           if row["entity"]["id"] == left)
        if frozen_face["effective_macro_protection"]["value"] is not True:
            raise RuntimeError("fresh scope did not report the frozen target Macro")
        frozen_backup = run_dir / "must-not-exist-frozen-surface-remesh.ansa"
        request = _request(
            h, deck, condition, scope, seed, "FREE", "quad", frozen_backup)
        frozen = _failed(
            await _remesh_task(h, run_dir, report, request, expected="failed"),
            frozen_backup)
        if not any(word in json.dumps(frozen).casefold()
                   for word in ("frozen", "macro")):
            raise RuntimeError("frozen rejection lacks matching diagnostic")
        report["frozen_rejection"] = frozen
        await h.snapshot_context()
        current = await _scope(h, run_dir, report, deck, left, "unfreeze")
        _, _, _ = await scope_api._freeze_call(
            h, run_dir, report, deck, "FACE", [left], "unfreeze",
            current["scope_sha256"],
            run_dir / "before-surface-remesh-unfreeze.ansa")
        await h.snapshot_context()
        unfrozen = await _scope(h, run_dir, report, deck, left, "unfrozen")
        face = next(row for row in unfrozen["faces"]
                    if row["entity"]["id"] == left)
        if face["effective_macro_protection"]["value"] is not False:
            raise RuntimeError("target Macro remained protected after unfreeze")
        final_condition = (await _condition(
            h, run_dir, report, deck, [left], "before-save"))[0]
        final_mesh = await _mesh(h, run_dir, report, face_map, "before-save")
        final_fe = await _fe(h, run_dir, report, fe_spec, "before-save")
        final_config = await _config(h, run_dir, report, "before-save")
        if final_mesh["right"] != baseline["mesh"]["right"] or final_fe != baseline["fe"]:
            raise RuntimeError("right Macro or independent FE changed before save")
        if final_config["snapshot_sha256"] != baseline["config"]["snapshot_sha256"]:
            raise RuntimeError("configuration changed before save")
        if sorted(final_condition["expected_shell_ids"]) != final_mesh["left"]["shell_ids"]:
            raise RuntimeError("final FACE membership changed")
        report["save_reopen"] = await _reopen_check(
            h, run_dir, report, deck, face_map, fe_spec,
            final_mesh, final_fe, final_config)
        report.update(complete=True, final_model_saved=True,
                      post_reopen_verified=True, status="complete")
        scope_api._write_json(run_dir / REPORT_NAME, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path,
                        help="stage_three_ansa.py --prepare-only 生成的独立运行目录")
    parser.add_argument("--keep-owned-ansa", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=60)
    args = parser.parse_args(argv)
    if not 1 <= args.startup_timeout <= 60:
        parser.error("--startup-timeout must be 1..60")
    run_dir = args.run_dir.expanduser().resolve(strict=True)
    report_path = run_dir / REPORT_NAME
    if report_path.exists():
        raise FileExistsError("report exists; query original tasks instead of replaying")
    instructions = json.loads(
        (run_dir / "launch_instructions.json").read_text(encoding="utf-8"))
    if Path(instructions["run_dir"]).resolve() != run_dir:
        raise ValueError("launch instructions belong to another run")
    report = {"status": "preparing", "complete": False, "report_name": REPORT_NAME,
              "run_dir": str(run_dir), "phase": "surface-remesh",
              "calls": [], "pending": None, "cases": {}}
    scope_api._write_json(report_path, report)
    identity = helpers = None
    try:
        stage.PANEL_WAIT_SECONDS = args.startup_timeout
        identity_path = run_dir / IDENTITY_NAME
        if identity_path.is_file():
            stored = json.loads(identity_path.read_text(encoding="utf-8"))
            saved = stored.get("identity", stored)
            identity, helpers, basic = scope_api._resume_owned_ansa(
                instructions, {"identity": saved})
            report["identity_check"] = {
                "pid": basic["pid"], "main_thread": basic["main_thread"],
                "input_form": "wrapped" if "identity" in stored else "flat"}
        else:
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(
                run_dir, instructions)
            scope_api._write_json(identity_path, {"identity": identity})
        report.update(identity=identity, status="running")
        scope_api._write_json(report_path, report)
        asyncio.run(_verify(run_dir, instructions, identity, helpers, report))
    except Exception:
        report.update(status="failed", error=traceback.format_exc())
    finally:
        safe = (identity is not None and helpers is not None
                and report.get("complete") is True
                and report.get("final_model_saved") is True
                and report.get("post_reopen_verified") is True
                and report.get("pending") is None)
        if safe and not args.keep_owned_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(
                    identity, instructions, helpers)
            except Exception:
                report.update(status="cleanup_failed", cleanup_error=traceback.format_exc())
        elif identity is not None:
            report["cleanup"] = {"stopped_owned_process": False,
                                 "reason": "keep requested or verification incomplete"}
        scope_api._write_json(report_path, report)
    print(json.dumps({"status": report.get("status"), "complete": report.get("complete"),
                      "report": str(report_path), "cleanup": report.get("cleanup")},
                     ensure_ascii=False), flush=True)
    return 0 if report.get("status") == "complete" and report.get("complete") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
