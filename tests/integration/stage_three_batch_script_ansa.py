"""通过命名脚本入口，对旧 run_batch_mesh 能力做独立真实网格基线验收。"""

from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned
from stage_three_connections_ansa import independent_script


def remote_mesh_oracle(expected_pid, expected_arguments, expected_face_id):
    import math
    import os
    import threading
    from ansa import base, constants, mesh, session

    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Mesh oracle is not on the owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Mesh oracle requires the current NASTRAN deck")

    def collect(kind):
        entities = base.CollectEntities(deck, None, kind)
        if type(entities) not in (list, tuple):
            raise RuntimeError("CollectEntities returned an invalid " + kind + " collection")
        return sorted(entities, key=lambda entity: entity._id)

    def json_value(value):
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise RuntimeError("Card contains a non-finite value")
            return value
        if type(value) in (list, tuple):
            return [json_value(item) for item in value]
        if type(value) is dict:
            return {str(key): json_value(item) for key, item in value.items()}
        if hasattr(value, "_id") and callable(getattr(value, "ansa_type", None)):
            return {"id": value._id, "type": value.ansa_type(deck)}
        raise RuntimeError("Card contains a value that is not strict JSON")

    def card(entity):
        labels = entity.card_fields(deck)
        if type(labels) not in (list, tuple) or not labels or any(type(label) is not str for label in labels):
            raise RuntimeError("Entity card fields are unavailable")
        fields = tuple(label for label in labels if label not in ("MBContainer", "MBContainers"))
        values = base.GetEntityCardValues(deck, entity, fields)
        if type(values) is not dict or set(values) != set(fields):
            raise RuntimeError("Entity card readback is incomplete")
        return {label: json_value(values[label]) for label in fields}, values, fields

    faces = collect("FACE")
    face_ids = sorted(entity._id for entity in faces)
    if face_ids != [expected_face_id]:
        raise RuntimeError("Fixture FACE set changed or became ambiguous")
    face = faces[0]
    parts = collect("ANSAPART")
    if len(parts) != 1 or parts[0].ansa_type(deck) != "ANSAPART":
        raise RuntimeError("STEP fixture must contain exactly one ANSAPART")
    part_faces = base.CollectEntities(deck, parts[0], "FACE", True)
    if type(part_faces) not in (list, tuple):
        raise RuntimeError("Could not read FACE membership of the fixture Part")
    part_face_ids = sorted(entity._id for entity in part_faces)

    face_card, _face_values, _face_fields = card(face)
    face_area = base.GetFaceArea(face)
    macro = base.GetFaceMacro(face)
    if macro is None:
        raise RuntimeError("Fixture FACE has no readable macro")
    macro_face_entities = base.GetFacesOfMacros([macro])
    if type(macro_face_entities) not in (list, tuple):
        raise RuntimeError("GetFacesOfMacros returned invalid data")
    macro_face_ids = sorted(entity._id for entity in macro_face_entities)
    macro_shells = mesh.GetMacroShells([macro])
    if type(macro_shells) is int and macro_shells == 0:
        macro_shells = []
    if type(macro_shells) not in (list, tuple):
        raise RuntimeError("GetMacroShells returned invalid data")
    macro_shell_ids = sorted(entity._id for entity in macro_shells)

    shells = collect("SHELL")
    shell_ids = sorted(entity._id for entity in shells)
    shell_cards = []
    referenced_grid_ids = set()
    tria_cards_ok = True
    for entity in shells:
        values_json, values, labels = card(entity)
        if not {"G1", "G2", "G3"}.issubset(labels):
            raise RuntimeError("SHELL card lacks first-order triangular connectivity fields")
        node_ids = []
        for label in ("G1", "G2", "G3"):
            value = values[label]
            node_id = value._id if hasattr(value, "_id") else value
            if type(node_id) is not int or node_id < 1:
                raise RuntimeError("SHELL connectivity contains an invalid GRID ID")
            node_ids.append(node_id)
        if len(set(node_ids)) != 3:
            tria_cards_ok = False
        if "G4" in labels and values.get("G4") not in (None, "", 0):
            tria_cards_ok = False
        referenced_grid_ids.update(node_ids)
        shell_cards.append({"id": entity._id, "ansa_type": entity.ansa_type(deck),
                            "fields": values_json, "tria_node_ids": node_ids})

    coordinate_fields = ("X1", "X2", "X3")
    grids = collect("GRID")
    grid_ids = sorted(entity._id for entity in grids)
    grid_cards = []
    coordinates = {}
    for entity in grids:
        labels = entity.card_fields(deck)
        if type(labels) not in (list, tuple) or not set(coordinate_fields).issubset(labels):
            raise RuntimeError("NASTRAN GRID coordinate fields are unavailable")
        values = base.GetEntityCardValues(deck, entity, coordinate_fields)
        if (type(values) is not dict or set(values) != set(coordinate_fields)
                or any(type(values[name]) not in (int, float) or not math.isfinite(values[name])
                       for name in coordinate_fields)):
            raise RuntimeError("GRID coordinate card readback is incomplete")
        point = {name: float(values[name]) for name in coordinate_fields}
        coordinates[entity._id] = point
        grid_cards.append({"id": entity._id, "fields": point})

    tolerance = 1e-5
    boundary_node_ids = {name: [] for name in ("x_min", "x_max", "y_min", "y_max")}
    outside_boundary_ids = []
    for entity_id, point in coordinates.items():
        x, y, z = point["X1"], point["X2"], point["X3"]
        if (x < -tolerance or x > 20.0 + tolerance or y < -tolerance
                or y > 10.0 + tolerance or abs(z) > tolerance):
            outside_boundary_ids.append(entity_id)
        if abs(x) <= tolerance:
            boundary_node_ids["x_min"].append(entity_id)
        if abs(x - 20.0) <= tolerance:
            boundary_node_ids["x_max"].append(entity_id)
        if abs(y) <= tolerance:
            boundary_node_ids["y_min"].append(entity_id)
        if abs(y - 10.0) <= tolerance:
            boundary_node_ids["y_max"].append(entity_id)
    boundary_coverage = bool(grid_ids) and all(boundary_node_ids.values())
    scope_ok = (
        expected_face_id in part_face_ids
        and macro_face_ids == face_ids
        and macro_shell_ids == shell_ids
        and referenced_grid_ids == set(grid_ids)
    )
    settings_fields = ("mesh_change_option", "element_type", "element_order", "target_element_length")
    settings = base.BCSettingsGetValues(settings_fields)
    if type(settings) is not dict or any(type(settings.get(name)) is not str for name in settings_fields):
        raise RuntimeError("Mesh settings readback is incomplete")
    return {
        "pid": os.getpid(), "main_thread": True, "deck": deck,
        "model_path": base.DataBaseName(), "face_ids": face_ids,
        "face": {"id": face._id, "area": face_area, "card": face_card,
                 "part_id": parts[0]._id, "part_type": parts[0].ansa_type(deck),
                 "part_face_ids": part_face_ids, "macro_id": macro._id,
                 "macro_face_ids": macro_face_ids},
        "part_ids": sorted(entity._id for entity in parts),
        "shell_ids": shell_ids, "macro_shell_ids": macro_shell_ids,
        "shell_cards": shell_cards, "grid_ids": grid_ids, "grid_cards": grid_cards,
        "referenced_grid_ids": sorted(referenced_grid_ids), "tria_cards_ok": tria_cards_ok,
        "scope_ok": scope_ok, "boundary_inside": not outside_boundary_ids,
        "boundary_outside_grid_ids": sorted(outside_boundary_ids),
        "boundary_node_ids": {key: sorted(value) for key, value in boundary_node_ids.items()},
        "boundary_coverage": boundary_coverage, "settings": settings,
    }


def _batchmesh_script(expected_pid, expected_arguments, face_id, model_path, backup_path):
    template = '''def main():
    import json
    import math
    import os
    import threading
    from pathlib import Path
    from ansa import base, constants, mesh, session

    expected_pid = __EXPECTED_PID__
    expected_arguments = __EXPECTED_ARGUMENTS__
    face_id = __FACE_ID__
    expected_model_path = __MODEL_PATH__
    backup_path = __BACKUP_PATH__
    settings_fields = ("mesh_change_option", "element_type", "element_order", "target_element_length")
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Batch mesh requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Batch mesh requires the current NASTRAN deck")
    if Path(base.DataBaseName()).resolve() != Path(expected_model_path).resolve():
        raise RuntimeError("Batch mesh source model path changed")
    if Path(backup_path).exists():
        raise RuntimeError("Refusing to overwrite a prior mesh-parameter backup or repeat the mesh")

    def collect(kind):
        entities = base.CollectEntities(deck, None, kind)
        if type(entities) not in (list, tuple):
            raise RuntimeError("CollectEntities returned an invalid " + kind + " collection")
        return sorted(entities, key=lambda entity: entity._id)

    faces = collect("FACE")
    parts = collect("ANSAPART")
    if len(faces) != 1 or faces[0]._id != face_id or len(parts) != 1:
        raise RuntimeError("Batch mesh requires the single selected fixture FACE and its Part")
    part_faces = base.CollectEntities(deck, parts[0], "FACE", True)
    if (type(part_faces) not in (list, tuple)
            or [entity._id for entity in part_faces] != [face_id]):
        raise RuntimeError("The selected FACE is not the sole FACE in the fixture Part")
    face = faces[0]
    face_area_before = base.GetFaceArea(face)
    if not math.isfinite(face_area_before) or not math.isclose(face_area_before, 200.0, rel_tol=1e-8, abs_tol=1e-8):
        raise RuntimeError("The selected FACE does not match the 20 by 10 mm fixture boundary")
    macro = base.GetFaceMacro(face)
    if macro is None or [entity._id for entity in base.GetFacesOfMacros([macro])] != [face_id]:
        raise RuntimeError("The selected FACE macro is absent or ambiguous")
    before_shell_ids = [entity._id for entity in collect("SHELL")]
    before_grid_ids = [entity._id for entity in collect("GRID")]
    if before_shell_ids or before_grid_ids:
        raise RuntimeError("Batch mesh fixture must start with no SHELL or GRID entities")
    settings_before = base.BCSettingsGetValues(settings_fields)
    if type(settings_before) is not dict or any(type(settings_before.get(name)) is not str for name in settings_fields):
        raise RuntimeError("Original mesh settings could not be fully read")
    save_rc = mesh.SaveMeshParams(backup_path)
    backup = Path(backup_path)
    if type(save_rc) is not int or save_rc != 1 or not backup.is_file() or backup.stat().st_size == 0:
        raise RuntimeError("SaveMeshParams did not produce a non-empty backup; mesh was not started")

    result = {
        "ok": False, "deck": deck, "selected_face_ids": [face_id],
        "mesh_input_face_ids": [face_id], "mesh_call_count": 0, "mesh_started": False,
        "face_area_before": face_area_before, "before_shell_ids": before_shell_ids,
        "before_grid_ids": before_grid_ids, "settings_before": settings_before,
        "settings_used": None, "settings_after": None, "save_rc": save_rc,
        "set_settings_rc": None, "set_length_rc": None, "rc": None,
        "restore_file_rc": None, "restore_mode_rc": None,
        "parameters_restored": False, "new_shell_ids": [], "new_grid_ids": [],
        "parameter_backup": backup_path,
    }
    try:
        result["set_settings_rc"] = base.BCSettingsSetValues({
            "mesh_change_option": "mesh", "element_type": "tria", "element_order": "1st Order"})
        if type(result["set_settings_rc"]) is not int or result["set_settings_rc"] != 0:
            raise RuntimeError("BCSettingsSetValues did not apply the temporary mesh settings")
        result["set_length_rc"] = mesh.SetMeshParamTargetLength("absolute", 2.0)
        if type(result["set_length_rc"]) is not int or result["set_length_rc"] != 1:
            raise RuntimeError("SetMeshParamTargetLength did not apply absolute length 2")
        settings_used = base.BCSettingsGetValues(settings_fields)
        result["settings_used"] = settings_used
        if (type(settings_used) is not dict or settings_used.get("mesh_change_option") != "mesh"
                or str(settings_used.get("element_type", "")).casefold() != "tria"
                or settings_used.get("element_order") != "1st Order"):
            raise RuntimeError("Temporary mesh settings did not match their requested values")
        try:
            target_length = float(settings_used.get("target_element_length", "nan"))
        except (TypeError, ValueError):
            target_length = float("nan")
        if not math.isfinite(target_length) or not math.isclose(target_length, 2.0, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError("Temporary absolute target length did not read back as 2")
        result["mesh_started"] = True
        result["mesh_call_count"] = 1
        result["rc"] = mesh.Mesh([face])
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            result["restore_file_rc"] = mesh.ReadMeshParams(backup_path)
        except Exception as exc:
            result["restore_file_error"] = type(exc).__name__ + ": " + str(exc)
        try:
            result["restore_mode_rc"] = base.BCSettingsSetValues({
                "mesh_change_option": settings_before["mesh_change_option"]})
        except Exception as exc:
            result["restore_mode_error"] = type(exc).__name__ + ": " + str(exc)
        try:
            result["settings_after"] = base.BCSettingsGetValues(settings_fields)
        except Exception as exc:
            result["settings_after_error"] = type(exc).__name__ + ": " + str(exc)
        result["parameters_restored"] = (
            type(result["restore_file_rc"]) is int and result["restore_file_rc"] == 1
            and type(result["restore_mode_rc"]) is int and result["restore_mode_rc"] == 0
            and result["settings_after"] == settings_before)
        try:
            result["new_shell_ids"] = sorted(
                {entity._id for entity in collect("SHELL")} - set(before_shell_ids))
            result["new_grid_ids"] = sorted(
                {entity._id for entity in collect("GRID")} - set(before_grid_ids))
            result["face_ids_after"] = [entity._id for entity in collect("FACE")]
            result["face_area_after"] = base.GetFaceArea(face)
        except Exception as exc:
            result["model_readback_error"] = type(exc).__name__ + ": " + str(exc)
    result["ok"] = (
        result["mesh_started"] and result["mesh_call_count"] == 1
        and type(result["rc"]) is int and result["rc"] == 1
        and result["parameters_restored"] and bool(result["new_shell_ids"])
        and result.get("face_ids_after") == [face_id]
        and type(result.get("face_area_after")) in (int, float)
        and math.isfinite(result["face_area_after"])
        and math.isclose(result["face_area_after"], face_area_before, rel_tol=1e-8, abs_tol=1e-8))
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result
'''
    return (template.replace("__EXPECTED_PID__", repr(expected_pid))
            .replace("__EXPECTED_ARGUMENTS__", repr(expected_arguments))
            .replace("__FACE_ID__", repr(face_id))
            .replace("__MODEL_PATH__", repr(model_path))
            .replace("__BACKUP_PATH__", repr(backup_path)))


def _record(report, path, name, evidence, *, ok=True):
    report["cases"].append({"name": name, "ok": bool(ok), "evidence": evidence})
    report["last_confirmed_case"] = name if ok else report.get("last_confirmed_case")
    common._json_write(path, report)
    print(json.dumps({"case": name, "ok": bool(ok)}, ensure_ascii=True), flush=True)


def _file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _same_mesh_state(left, right):
    return {key: value for key, value in left.items() if key != "model_path"} == {
        key: value for key, value in right.items() if key != "model_path"}


async def _submit_and_wait(harness, arguments, report, path, label):
    report["current_stage"] = label
    common._json_write(path, report)
    submitted = await harness.call("ansa_script_execute", arguments, allow_error=True)
    task_id = submitted.get("task_id")
    if not task_id:
        report["last_script_submission"] = submitted
        common._json_write(path, report)
        raise RuntimeError("ansa_script_execute did not create a task; no retry was attempted")
    report["pending_script_task"] = {
        "operation_id": arguments["operation_id"], "task_id": task_id, "stage": label}
    common._json_write(path, report)
    try:
        completed = await harness.wait_task(task_id, expected=None, timeout_seconds=300)
    except TimeoutError as exc:
        status = await harness.call("ansa_task_query", {"task_id": task_id}, allow_error=True)
        report["unknown_or_running_script_task"] = {"submitted": submitted, "last_status": status}
        report.pop("pending_script_task", None)
        common._json_write(path, report)
        raise RuntimeError("Script task did not reach a terminal state; retained its original ID and did not replay") from exc
    report.pop("pending_script_task", None)
    common._json_write(path, report)
    return submitted, completed


async def verify(instructions, identity, helpers, run_dir, report, path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    one, two, _original_channel = helpers
    source_fixture = common.PROJECT_ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step"
    fixture = run_dir / source_fixture.name
    seed = Path(identity["seed_path"])
    seed_backup = run_dir / "seed-baseline-backup.ansa"
    parameter_backup = run_dir / "batchmesh-original-settings.ansa_mpar"
    script_path = run_dir / "batchmesh-face-main.py"
    import_backup = run_dir / "batchmesh-before-step-import.ansa"
    saved_model = run_dir / "batchmesh-meshed.ansa"
    first_open_backup = run_dir / "batchmesh-before-first-open.ansa"
    same_path_backup = run_dir / "batchmesh-before-same-path-open.ansa"
    for output in (fixture, parameter_backup, script_path, import_backup, saved_model,
                   first_open_backup, same_path_backup):
        if output.exists():
            raise RuntimeError("Refusing to overwrite a prior fixture, report, mesh, or backup: " + str(output))
    if not source_fixture.is_file() or not seed.is_file() or not seed_backup.is_file():
        raise RuntimeError("Required STEP fixture or the owned helper's seed backup is missing")

    fixture_source_hash = _file_hash(source_fixture)
    shutil.copy2(source_fixture, fixture)
    fixture_copy_hash = _file_hash(fixture)
    seed_hash = _file_hash(seed)
    seed_backup_hash = _file_hash(seed_backup)
    report["fixture_files"] = {
        "source": str(source_fixture), "source_sha256": fixture_source_hash,
        "owned_copy": str(fixture), "owned_copy_sha256": fixture_copy_hash,
        "owned_seed": str(seed), "owned_seed_sha256": seed_hash,
        "owned_seed_backup": str(seed_backup), "owned_seed_backup_sha256": seed_backup_hash,
    }
    common._json_write(path, report)

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    @asynccontextmanager
    async def backend(log_name):
        with (run_dir / log_name).open("w", encoding="utf-8") as log:
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    harness = common.McpHarness(client, report, run_dir)
                    report["schema"] = await harness.list_schema()
                    await common._bind_for_run(harness, identity, one, two, identity["port"])
                    yield harness

    async with backend("batchmesh-backend.stderr.log") as harness:
        report["target"] = harness.target
        seed_snapshot = await harness.snapshot_context()
        import_fields = {
            "format": "STEP", "path": str(fixture), "mode": "replace",
            "preservation": "backup", "backup_path": str(import_backup),
        }
        _import_submit, import_task = await harness.task("ansa_model_import", import_fields)
        imported = import_task.get("data", {}).get("result", {})
        fixture_snapshot = await harness.snapshot_context()
        if not import_backup.is_file() or import_backup.stat().st_size == 0:
            raise AssertionError("STEP replacement did not preserve the owned seed")
        face_ids = imported.get("after", {}).get("face_ids", [])
        if len(face_ids) != 1:
            raise AssertionError("STEP fixture import did not produce exactly one FACE")
        face_id = face_ids[0]
        before_mesh = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)

        source = _batchmesh_script(
            identity["pid"], identity["program_args"], face_id, str(fixture), str(parameter_backup))
        script_path.write_text(source, encoding="utf-8", newline="\n")
        script_bytes = script_path.read_bytes()
        script_content = script_bytes.decode("utf-8")
        compile(script_content, str(script_path), "exec")
        script_hash = sha256(script_bytes).hexdigest()
        report["native_script"] = {
            "path": str(script_path), "encoding": "UTF-8", "sha256": script_hash,
            "bytes": len(script_bytes), "entrypoint": "ansa_script_execute(function_name='main')",
            "mesh_api_calls_in_source": script_content.count("mesh.Mesh("),
        }

        fixture_case_checks = {
            "owned_seed_backup_matches_seed": seed_hash == seed_backup_hash,
            "import_succeeded": import_task.get("status") == "succeeded" and imported.get("ok") is True,
            "fixture_source_and_copy_hash_match": fixture_source_hash == fixture_copy_hash,
            "fixture_part_contains_face": face_id in before_mesh["face"]["part_face_ids"],
            "one_part_one_face": before_mesh["part_ids"] == [before_mesh["face"]["part_id"]]
            and before_mesh["face_ids"] == [face_id],
            "expected_face_area": math.isclose(before_mesh["face"]["area"], 200.0, rel_tol=1e-8, abs_tol=1e-8),
            "pre_mesh_is_empty_and_scoped": not before_mesh["shell_ids"] and not before_mesh["grid_ids"]
            and before_mesh["scope_ok"],
            "script_is_single_explicit_mesh_call": report["native_script"]["mesh_api_calls_in_source"] == 1,
            "fixture_context_advanced_after_replace": fixture_snapshot["model"]["epoch"] > seed_snapshot["model"]["epoch"],
        }
        _record(report, path, "owned_fixture_part_and_utf8_script_hash", {
            "checks": fixture_case_checks, "import_task": import_task, "independent_before_mesh": before_mesh,
            "seed_backup": str(seed_backup), "import_backup": str(import_backup),
            "fixture_files": report["fixture_files"], "native_script": report["native_script"],
        }, ok=all(fixture_case_checks.values()))
        if not all(fixture_case_checks.values()):
            raise AssertionError("Fixture or native script source did not match the baseline preconditions")

        operation_id = "run-batch-mesh-" + uuid.uuid4().hex
        original_arguments = {
            "operation_id": operation_id, "expected_target": dict(harness.target),
            "expected_model": dict(harness.model), "script": script_content,
            "validate": True, "function_name": "main", "allow_model_change": True,
        }
        submitted, completed = await _submit_and_wait(
            harness, original_arguments, report, path, "one native mesh.Mesh([selected_face]) call")
        report["original_mesh_operation"] = {
            "operation_id": operation_id, "task_id": submitted.get("task_id"),
            "submitted": submitted, "completed": completed,
            "expected_target": original_arguments["expected_target"],
            "expected_model": original_arguments["expected_model"],
        }
        common._json_write(path, report)
        after_mesh_snapshot = await harness.snapshot_context()
        after_mesh = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        native = completed.get("data", {}).get("result", {})
        native = native.get("result") if type(native) is dict else None
        native = native if type(native) is dict else {}
        generated_shell_ids = sorted(set(after_mesh["shell_ids"]) - set(before_mesh["shell_ids"]))
        generated_grid_ids = sorted(set(after_mesh["grid_ids"]) - set(before_mesh["grid_ids"]))
        used_settings = native.get("settings_used") if type(native.get("settings_used")) is dict else {}
        try:
            used_length_is_two = math.isclose(
                float(used_settings.get("target_element_length", "nan")), 2.0, rel_tol=1e-9, abs_tol=1e-9)
        except (TypeError, ValueError):
            used_length_is_two = False
        mesh_checks = {
            "task_succeeded": completed.get("status") == "succeeded",
            "main_returned_success": native.get("ok") is True,
            "mesh_api_called_once_for_one_face": native.get("mesh_call_count") == 1
            and native.get("mesh_input_face_ids") == [face_id]
            and native.get("selected_face_ids") == [face_id]
            and native.get("deck") == before_mesh["deck"],
            "native_return_code": native.get("rc") == 1,
            "actual_new_shell_and_grid_ids_match_native_result": bool(generated_shell_ids)
            and native.get("new_shell_ids") == generated_shell_ids
            and native.get("new_grid_ids") == generated_grid_ids,
            "independent_cards_and_face_scope": after_mesh["scope_ok"] and after_mesh["tria_cards_ok"]
            and set(after_mesh["macro_shell_ids"]) == set(generated_shell_ids),
            "independent_grid_coordinates_match_fixture_boundary": after_mesh["boundary_inside"]
            and after_mesh["boundary_coverage"],
            "temporary_settings_were_exact_and_restored": used_settings.get("mesh_change_option") == "mesh"
            and str(used_settings.get("element_type", "")).casefold() == "tria"
            and used_settings.get("element_order") == "1st Order" and used_length_is_two
            and native.get("parameters_restored") is True
            and native.get("settings_after") == before_mesh["settings"]
            and after_mesh["settings"] == before_mesh["settings"],
            "face_and_part_preserved": after_mesh["face"] == before_mesh["face"]
            and after_mesh["part_ids"] == before_mesh["part_ids"],
            "model_snapshot_and_source_hash_stable": after_mesh_snapshot["model"]["epoch"]
            == fixture_snapshot["model"]["epoch"]
            and _file_hash(script_path) == script_hash
            and _file_hash(source_fixture) == fixture_source_hash
            and _file_hash(fixture) == fixture_copy_hash,
            "parameter_backup_is_preserved": Path(native.get("parameter_backup", "")).is_file()
            and Path(native.get("parameter_backup", "")).stat().st_size > 0,
        }
        _record(report, path, "native_main_mesh_and_independent_shell_grid_face_boundary_readback", {
            "checks": mesh_checks, "task": completed, "native_main_result": native,
            "before": before_mesh, "after": after_mesh, "snapshot_after_main": after_mesh_snapshot["model"],
            "script_sha256_before_and_after": {"before": script_hash, "after": _file_hash(script_path)},
        }, ok=all(mesh_checks.values()))
        if not all(mesh_checks.values()):
            raise AssertionError("Native main or independent mesh readback did not meet the baseline")

        repeat = await harness.call("ansa_script_execute", original_arguments, allow_error=True)
        receipt = await harness.call("ansa_task_query", {"task_id": submitted["task_id"]}, allow_error=True)
        after_repeat = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        conflict_arguments = dict(original_arguments)
        conflict_arguments["script"] = "def main():\n    raise RuntimeError('conflicting request must not mesh')\n"
        conflict = await harness.call("ansa_script_execute", conflict_arguments, allow_error=True)
        conflict_task = None
        if conflict.get("task_id") not in (None, submitted.get("task_id")):
            conflict_task = await harness.wait_task(conflict["task_id"], expected=None, timeout_seconds=60)
        after_conflict = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        error_source = "def main():\n    raise RuntimeError('controlled failure before any mesh call')\n"
        error_arguments = {
            "operation_id": "run-batch-mesh-error-main-" + uuid.uuid4().hex,
            "expected_target": dict(harness.target), "expected_model": dict(harness.model),
            "script": error_source, "validate": True, "function_name": "main",
            "allow_model_change": True,
        }
        error_submitted, error_task = await _submit_and_wait(
            harness, error_arguments, report, path, "controlled failing main with no mesh API call")
        error_snapshot = await harness.snapshot_context()
        after_error = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        case_three_checks = {
            "same_operation_returns_original_task": repeat.get("task_id") == submitted.get("task_id"),
            "original_receipt_unchanged": receipt.get("task_id") == submitted.get("task_id")
            and receipt.get("status") == completed.get("status")
            and receipt.get("data", {}).get("result") == completed.get("data", {}).get("result"),
            "different_request_with_same_operation_id_rejected": conflict.get("ok") is False
            and conflict.get("task_id") in (None, submitted.get("task_id"))
            and conflict_task is None,
            "failing_main_is_terminal_failure": error_task.get("status") == "failed"
            and bool(error_submitted.get("task_id")),
            "no_remesh_after_repeat_conflict_or_failed_main": _same_mesh_state(after_mesh, after_repeat)
            and _same_mesh_state(after_mesh, after_conflict)
            and _same_mesh_state(after_mesh, after_error),
            "script_hash_still_unchanged": _file_hash(script_path) == script_hash,
            "failed_main_kept_model_context": error_snapshot["model"]["epoch"]
            == after_mesh_snapshot["model"]["epoch"],
        }
        _record(report, path, "same_operation_conflict_and_error_main_do_not_remesh", {
            "checks": case_three_checks, "repeat": repeat, "original_receipt": receipt,
            "conflict": conflict, "unexpected_conflict_task": conflict_task,
            "error_main_task": error_task,
            "independent_after_repeat": after_repeat, "independent_after_conflict": after_conflict,
            "independent_after_error": after_error,
        }, ok=all(case_three_checks.values()))
        if not all(case_three_checks.values()):
            raise AssertionError("Idempotency, conflict, or failing-main behavior changed the mesh")

        save_result = await harness.direct_result("ansa_model_save", {"path": str(saved_model)})
        if not saved_model.is_file() or saved_model.stat().st_size == 0:
            raise AssertionError("Mesh model copy was not saved")
        saved_hash = _file_hash(saved_model)
        saved_snapshot = await harness.snapshot_context()
        after_save = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        _, first_open = await harness.task("ansa_model_open", {
            "path": str(saved_model), "preservation": "backup", "backup_path": str(first_open_backup),
        })
        first_open_snapshot = await harness.snapshot_context()
        after_first_open = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        if not first_open_backup.is_file() or first_open_backup.stat().st_size == 0:
            raise AssertionError("Opening the saved mesh did not preserve the current model")
        target_before_same_path = dict(harness.target)
        model_before_same_path = dict(harness.model)
        _, same_path_open = await harness.task("ansa_model_open", {
            "path": str(saved_model), "preservation": "backup", "backup_path": str(same_path_backup),
        })
        reopened_snapshot = await harness.snapshot_context()
        after_reopen = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        stale = await harness.call("ansa_script_execute", {
            "operation_id": "run-batch-mesh-stale-" + uuid.uuid4().hex,
            "expected_target": target_before_same_path, "expected_model": model_before_same_path,
            "script": script_content, "validate": True, "function_name": "main",
            "allow_model_change": True,
        }, allow_error=True)
        receipt_after_open = await harness.call(
            "ansa_task_query", {"task_id": submitted["task_id"]}, allow_error=True)
        after_stale = await independent_script(
            harness, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        case_four_checks = {
            "save_copy_preserved_live_model": save_result.get("saved") is True
            and saved_snapshot["model"]["epoch"] == after_mesh_snapshot["model"]["epoch"]
            and after_save["model_path"] == before_mesh["model_path"],
            "open_then_same_path_reopen_preserved_mesh": first_open.get("status") == "succeeded"
            and same_path_open.get("status") == "succeeded"
            and reopened_snapshot["model"]["epoch"] != model_before_same_path["epoch"]
            and Path(reopened_snapshot["model"]["database"]).resolve() == saved_model.resolve()
            and _same_mesh_state(after_mesh, after_first_open)
            and _same_mesh_state(after_mesh, after_reopen),
            "open_backups_exist": first_open_backup.is_file() and same_path_backup.is_file()
            and first_open_backup.stat().st_size > 0 and same_path_backup.stat().st_size > 0,
            "old_model_context_rejected_without_task": stale.get("ok") is False
            and stale.get("task_id") is None,
            "original_mesh_receipt_survives_reopen": receipt_after_open.get("task_id") == submitted.get("task_id")
            and receipt_after_open.get("status") == completed.get("status")
            and receipt_after_open.get("data", {}).get("result") == completed.get("data", {}).get("result"),
            "stale_request_did_not_remesh_or_change_script": _same_mesh_state(after_mesh, after_stale)
            and _file_hash(script_path) == script_hash and _file_hash(saved_model) == saved_hash,
        }
        _record(report, path, "save_same_path_reopen_stale_context_and_original_receipt", {
            "checks": case_four_checks, "save": save_result, "first_open": first_open,
            "same_path_open": same_path_open, "stale_request": stale,
            "original_receipt": receipt_after_open, "saved_model": str(saved_model),
            "saved_model_sha256": saved_hash, "independent_after_reopen": after_reopen,
        }, ok=all(case_four_checks.values()))
        if not all(case_four_checks.values()):
            raise AssertionError("Same-path reopen, stale context, or original receipt check failed")

    async with backend("batchmesh-restarted-backend.stderr.log") as restarted:
        restarted_snapshot = await restarted.snapshot_context()
        after_restart = await independent_script(
            restarted, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        original_after_restart = await restarted.call(
            "ansa_task_query", {"task_id": submitted["task_id"]}, allow_error=True)
        replay_after_restart = await restarted.call(
            "ansa_script_execute", original_arguments, allow_error=True)
        after_replay = await independent_script(
            restarted, remote_mesh_oracle, identity["pid"], identity["program_args"], face_id)
        case_five_checks = {
            "model_context_survived_backend_restart": restarted_snapshot["model"]["epoch"]
            == reopened_snapshot["model"]["epoch"],
            "original_task_receipt_survived_restart": original_after_restart.get("task_id")
            == submitted.get("task_id") and original_after_restart.get("status") == completed.get("status")
            and original_after_restart.get("data", {}).get("result") == completed.get("data", {}).get("result"),
            "original_operation_id_maps_to_original_task": replay_after_restart.get("task_id")
            == submitted.get("task_id"),
            "restart_query_and_replay_did_not_remesh": _same_mesh_state(after_reopen, after_restart)
            and _same_mesh_state(after_reopen, after_replay),
            "source_script_and_saved_model_hashes_unchanged": _file_hash(script_path) == script_hash
            and _file_hash(saved_model) == saved_hash
            and _file_hash(source_fixture) == fixture_source_hash
            and _file_hash(fixture) == fixture_copy_hash,
        }
        _record(report, path, "backend_restart_reads_original_task_without_replay", {
            "checks": case_five_checks, "original_task": original_after_restart,
            "original_operation_replay": replay_after_restart,
            "independent_after_restart": after_restart,
            "independent_after_original_operation_query": after_replay,
            "script_sha256": script_hash, "saved_model_sha256": saved_hash,
        }, ok=all(case_five_checks.values()))
        if not all(case_five_checks.values()):
            raise AssertionError("Backend restart replayed or lost the original mesh task")

    report["visual_acceptance"] = False
    report["native_run_batch_mesh_mapping"] = "UTF-8 file content and synchronous main through ansa_script_execute; single FACE mesh.Mesh scope"


if __name__ == "__main__":
    raise SystemExit(owned.main(
        report_name="batch-script-report.json",
        scope="run_batch_mesh legacy baseline through UTF-8 file content and ansa_script_execute main",
        verify_callback=verify,
    ))
