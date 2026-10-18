"""Owned-process acceptance for the controlled file-set benchmark operation.

The driver attaches only to stage_three_api_catalog_ansa's separately owned ANSA
process. It never launches ANSA or inspects a canvas. Model observations use
direct ANSA APIs through the owned main-thread gRPC script; MCP calls use stdio.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from datetime import timedelta
from pathlib import Path
import shutil
import sys
import traceback
import uuid

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned


TOOL = "ansa_benchmark_run"
REPORT_NAME = "benchmark-report.json"
SCOPE = "owned ANSA benchmark file-set, backup restore, and durable task acceptance"
COUNT_TYPES = (
    ("parts", "ANSAPART"), ("faces", "FACE"), ("nodes", "GRID"),
    ("shells", "SHELL"), ("solids", "SOLID"), ("elements", "__ELEMENTS__"),
)
GRID_META = ("__id__", "__type__")


def _check(condition, message):
    if not condition:
        raise AssertionError(message)


def _write(path: Path, report: dict) -> None:
    common._json_write(path, report)


def _record(report: dict, path: Path, name: str, evidence: dict) -> None:
    report.setdefault("cases", []).append({"name": name, "ok": True, "evidence": evidence})
    _write(path, report)
    print(json.dumps({"case": name, "ok": True}), flush=True)


def _fingerprint(path: Path) -> dict:
    payload = path.read_bytes()
    return {"path": str(path.resolve()), "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}


def _same_path(left, right) -> bool:
    if not left or not right:
        return left == right
    norm = lambda value: os.path.normcase(os.path.normpath(os.path.abspath(str(value))))
    return norm(left) == norm(right)


def _model_signature(value: dict) -> dict:
    return {key: value[key] for key in ("deck", "database", "model_epoch", "grid_count", "grid_cards")}


def _assert_seed_card(value: dict, seed_id: int, expected=None) -> dict:
    _check(value["main_thread"] is True and value["deck"] == value["nastran_deck"],
           "Independent readback is not on the owned NASTRAN main thread")
    _check(value["grid_count"] == 1 and len(value["grid_cards"]) == 1,
           "Owned seed must contain exactly one GRID")
    card = value["grid_cards"][0]
    _check(card["id"] == seed_id and card["card"]["__id__"] == seed_id
           and card["card"]["__type__"] == "GRID", "Owned seed GRID identity differs")
    _check({"X1", "X2", "X3"} <= set(card["card_fields"])
           and [card["card"][key] for key in ("X1", "X2", "X3")] == [1.0, 2.0, 3.0],
           "Owned seed GRID coordinates differ")
    _check(set(card["card"]) == set(GRID_META) | set(card["card_fields"]),
           "Independent GRID readback omitted active card fields")
    if expected is not None:
        _check(card == expected, "Restored backup GRID card differs from the original full card")
    return card


def remote_benchmark_oracle(expected_pid, expected_arguments, input_paths=(), restore_path=None):
    """Read case summaries and full GRID cards with ANSA APIs on its main thread."""
    import builtins
    import math
    import os
    import threading

    from ansa import base, constants, session

    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Benchmark oracle requires the exact owned ANSA main thread")
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    if bridge is None:
        raise RuntimeError("Benchmark oracle cannot find the owned bridge")
    state = bridge.state
    count_types = (("parts", "ANSAPART"), ("faces", "FACE"), ("nodes", "GRID"),
                   ("shells", "SHELL"), ("solids", "SOLID"), ("elements", "__ELEMENTS__"))
    grid_meta = ("__id__", "__type__")

    def same_path(left, right):
        norm = lambda value: os.path.normcase(os.path.normpath(os.path.abspath(str(value))))
        return norm(left) == norm(right)

    state.assert_main()
    state.check_conflict()
    if state.journal is not None and state.journal.busy():
        raise RuntimeError("Benchmark oracle requires an idle owned task journal")

    def grid_cards(deck):
        entities = base.CollectEntities(deck, None, "GRID")
        if type(entities) not in (list, tuple):
            raise RuntimeError("CollectEntities(GRID) did not return an entity list")
        rows = []
        for entity in sorted(entities, key=lambda item: item._id):
            labels = entity.card_fields(deck)
            if (type(labels) not in (list, tuple) or not labels
                    or any(type(name) is not str or not name for name in labels)):
                raise RuntimeError("GRID.card_fields did not return all active card labels")
            fields = list(dict.fromkeys((*grid_meta, *labels)))
            values = base.GetEntityCardValues(deck, entity, fields)
            if type(values) is not dict or any(name not in values for name in fields):
                raise RuntimeError("GetEntityCardValues omitted an explicit GRID card field")
            rows.append({"id": entity._id, "card_fields": list(labels),
                         "card": {name: values[name] for name in fields}})
        return rows

    def counts(deck):
        result = {}
        for label, entity_type in count_types:
            entities = base.CollectEntities(deck, None, entity_type, False)
            if type(entities) not in (list, tuple):
                raise RuntimeError("CollectEntities(%s) did not return a list" % entity_type)
            result[label] = len(entities)
        return result

    def face_ranking(deck):
        faces = base.CollectEntities(deck, None, "FACE")
        if type(faces) not in (list, tuple):
            raise RuntimeError("CollectEntities(FACE) did not return a list")
        rows = []
        for face in faces:
            area, center, bounds = base.GetFaceArea(face), base.Cog(face), base.BoundBox([face])
            if type(area) not in (int, float) or not math.isfinite(area) or area < 0:
                raise RuntimeError("GetFaceArea returned an invalid area")
            if type(center) not in (list, tuple) or len(center) != 3:
                raise RuntimeError("Cog returned an invalid center")
            if type(bounds) not in (list, tuple) or len(bounds) != 6:
                raise RuntimeError("BoundBox returned an invalid face bound box")
            center_z, max_z = float(center[2]), float(bounds[5])
            if not math.isfinite(center_z) or not math.isfinite(max_z):
                raise RuntimeError("FACE Z readback is not finite")
            rows.append({"id": face._id, "area": float(area),
                         "center_z": center_z, "max_z": max_z})
        largest = sorted(rows, key=lambda row: (-row["area"], row["id"]))
        highest = sorted(rows, key=lambda row: (-row["center_z"], row["id"]))
        return {
            "face_count": len(rows),
            "largest_face_id": largest[0]["id"] if largest else None,
            "largest_face_area": largest[0]["area"] if largest else None,
            "top_face_id": highest[0]["id"] if highest else None,
            "top_face_max_z": highest[0]["max_z"] if highest else None,
        }

    def open_and_probe(path):
        before = state.probe()["model"]["epoch"]
        state.invalidate_model()
        status = base.Open(path)
        if type(status) is not int or status != 0:
            raise RuntimeError("Independent base.Open failed: %r" % (status,))
        database, deck = base.DataBaseName(), base.CurrentDeck()
        probe = state.probe()
        epoch = probe["model"]["epoch"]
        if epoch <= before or deck != constants.NASTRAN or not same_path(database, path):
            raise RuntimeError("Independent Open path, deck, or model epoch did not verify")
        return {"deck": deck, "database": database, "epoch_before": before,
                "model_epoch": epoch}

    observations = []
    for item in input_paths:
        opened = open_and_probe(item["path"])
        observations.append({"label": item["label"], **opened,
                             "counts": counts(opened["deck"]),
                             **face_ranking(opened["deck"])})
    if restore_path is not None:
        restored = open_and_probe(restore_path)
    else:
        epoch = state.probe()["model"]["epoch"]
        restored = {"deck": base.CurrentDeck(), "database": base.DataBaseName(),
                    "epoch_before": epoch, "model_epoch": epoch}
    deck, cards, probe = base.CurrentDeck(), None, state.probe()
    cards = grid_cards(deck)
    return {
        "pid": os.getpid(), "main_thread": True, "version": str(constants.app_version),
        "nastran_deck": constants.NASTRAN, "deck": deck,
        "database": base.DataBaseName(), "model_epoch": probe["model"]["epoch"],
        "grid_count": len(cards), "grid_cards": cards,
        "counts": counts(deck),
        "session_dir": str(state.session_dir), "input_observations": observations,
        "restore_open": restored,
    }


def _remote(two, identity, input_paths=(), restore_path=None):
    return two.rpc(identity["port"], remote_benchmark_oracle, identity["pid"],
                   identity["program_args"], input_paths, restore_path)


def _response_value(response) -> dict | None:
    if isinstance(response.structuredContent, dict):
        return response.structuredContent
    for item in response.content:
        if getattr(item, "type", None) == "text":
            try:
                value = json.loads(item.text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
    return None


def _arguments(h, operation_id: str, **fields) -> dict:
    return {"operation_id": operation_id, "expected_target": dict(h.target),
            "expected_model": dict(h.model), **fields}


def _persist_task(report, path, label, arguments, task_id, tool_name=TOOL):
    report.setdefault("task_ids", {})[label] = {
        "tool": tool_name, "operation_id": arguments["operation_id"], "task_id": task_id,
    }
    _write(path, report)


async def _wait_persisted(h, report, path, label, task_id, expected="succeeded", timeout=300):
    try:
        completed = await h.wait_task(task_id, None, timeout)
        if expected is not None and completed.get("status") != expected:
            report.setdefault("unexpected_terminal_results", {})[label] = completed
            _write(path, report)
            raise AssertionError("Original task reached unexpected state: " + str(completed.get("status")))
        return completed
    except TimeoutError:
        row = {"label": label, "task_id": task_id,
               "query": "ansa_task_query by the original task_id only"}
        report.setdefault("timeout_queries", []).append(row)
        _write(path, report)
        latest = await h.call("ansa_task_query", {"task_id": task_id}, allow_error=True)
        row["last_status"] = latest.get("status")
        _write(path, report)
        raise


async def _submit(h, report, path, label, arguments, expected="succeeded", tool_name=TOOL):
    response = await h.client.call_tool(tool_name, arguments)
    value = _response_value(response)
    task_id = value.get("task_id") if isinstance(value, dict) else None
    _check(type(task_id) is str and bool(task_id),
           "Benchmark submission did not return a durable task_id: %r" % value)
    _persist_task(report, path, label, arguments, task_id, tool_name)
    completed = await _wait_persisted(h, report, path, label, task_id, expected)
    report["task_ids"][label]["terminal_status"] = completed.get("status")
    _write(path, report)
    return value, completed


async def _rejection(h, report, path, label, arguments, *, task_expected=None):
    response = await h.client.call_tool(TOOL, arguments)
    value = _response_value(response)
    task_id = value.get("task_id") if isinstance(value, dict) else None
    completed = None
    if task_id:
        _persist_task(report, path, label, arguments, task_id)
        completed = await _wait_persisted(h, report, path, label, task_id, None)
        report["task_ids"][label]["terminal_status"] = completed.get("status")
        _write(path, report)
    rejected = (bool(response.isError) or (isinstance(value, dict) and value.get("ok") is False)
                or (completed is not None and completed.get("status") == "failed"))
    valid = rejected and (task_expected is not False or not task_id) and (
        task_expected is not True or bool(task_id)
    )
    return {"label": label, "task_id": task_id,
            "status": completed.get("status") if completed else "rejected_before_task",
            "error": (completed or value or {}).get("error") if isinstance(completed or value, dict) else None,
            "rejected": valid}


def _assert_schema(tool) -> dict:
    schema = tool.inputSchema
    props = schema.get("properties") if isinstance(schema, dict) else None
    required = {"operation_id", "expected_target", "expected_model", "root_path", "backup_path"}
    _check(isinstance(props, dict) and required <= set(props)
           and required <= set(schema.get("required") or ()), "Benchmark schema lacks required fields")
    for name in ("root_path", "backup_path"):
        _check(props[name].get("type") == "string" and props[name].get("minLength", 0) >= 1,
               "%s must be a required non-empty string" % name)
    limit = props.get("max_cases", {})
    _check(limit.get("type") == "integer" and limit.get("minimum") == 1
           and limit.get("maximum") == 10 and limit.get("default") == 10,
           "max_cases schema must remain strict 1..10 with default 10")
    ttl_name = next((name for name in ("ttl", "ttl_seconds") if name in props), None)
    _check(ttl_name is not None, "Benchmark TTL schema is missing")
    ttl = props[ttl_name]
    _check(ttl.get("type") == "integer" and ttl.get("minimum") == 0
           and ttl.get("maximum") == 86400 and ttl.get("default") == 300,
           "Benchmark TTL schema must remain bounded with default 300")
    return {"required": sorted(required), "max_cases": limit,
            "ttl_field": ttl_name, "ttl": ttl,
            "additionalProperties": schema.get("additionalProperties")}


def _prepare_inputs(run_dir: Path, identity: dict):
    root = run_dir / "benchmark-inputs"
    root.mkdir(parents=True, exist_ok=False)
    sources = {
        "owned_seed": Path(identity["seed_path"]),
        "rectangle_step": common.PROJECT_ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step",
    }
    _check(all(path.is_file() for path in sources.values()), "Seed or rectangle STEP fixture is missing")
    targets = {"owned_seed_copy": root / "ownedseed.ansa",
               "rectangle_step_copy": root / "rectangle-20x10-mm.step"}
    for source, target in zip(sources.values(), targets.values()):
        shutil.copy2(source, target)
    fingerprints = {key: _fingerprint(path) for key, path in {**sources, **targets}.items()}
    _check(fingerprints["owned_seed"]["sha256"] == fingerprints["owned_seed_copy"]["sha256"]
           and fingerprints["rectangle_step"]["sha256"] == fingerprints["rectangle_step_copy"]["sha256"],
           "Independent root copies changed their sources")
    return root, list(targets.values()), fingerprints


def _ordered(paths):
    return sorted(paths, key=lambda path: (path.stat().st_size, str(path).casefold()))


def _check_backup(path: Path, receipt: dict) -> dict:
    actual = _fingerprint(path)
    _check(_same_path(receipt.get("path"), path)
           and (receipt.get("size_bytes"), receipt.get("sha256"))
           == (actual["size_bytes"], actual["sha256"]), "Backup receipt differs from its real file")
    return actual


def _check_artifacts(result: dict, session_dir: str, expected_rows: list[dict]) -> dict:
    artifacts = result.get("artifacts")
    _check(type(artifacts) is list and len(artifacts) == 2
           and {item.get("kind") for item in artifacts} == {"json", "csv"},
           "Successful run must return exactly one JSON and one CSV report")
    private_dir = Path(session_dir).resolve(strict=True)
    verified = {}
    for item in artifacts:
        path = Path(item["path"])
        resolved = path.resolve(strict=True)
        _check(not path.is_symlink() and resolved.parent == private_dir,
               "Report artifact is outside the private session directory")
        data = resolved.read_bytes()
        sha, size = hashlib.sha256(data).hexdigest(), len(data)
        _check((item.get("sha256"), item.get("size_bytes")) == (sha, size),
               "Report artifact's returned SHA/size differs from its real file")
        verified[item["kind"]] = {"path": str(resolved), "sha256": sha,
                                  "size_bytes": size, "readable_by_driver": True}
        if item["kind"] == "json":
            decoded = json.loads(data.decode("utf-8"))
            executor_fields = {"executor", "main_thread", "model_after", "thread_id"}
            _check(decoded == {key: value for key, value in result.items()
                               if key not in executor_fields | {"artifacts"}},
                   "Private JSON content differs from the returned benchmark result")
            continue
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        rows = list(reader)
        required = {"input_path", "ok", "import_ok", *(key for key, _ in COUNT_TYPES),
                    "largest_face_id", "largest_face_area", "top_face_id", "top_face_max_z",
                    "sha256", "size_bytes", "import_status", "error"}
        _check(required <= set(reader.fieldnames or ()) and len(rows) == len(expected_rows),
               "Private CSV lacks required summary data or case rows")
        for actual, expected in zip(rows, expected_rows):
            for key in required:
                value = expected.get(key)
                matches = (_same_path(actual[key], value) if key == "input_path"
                           else actual[key] == ("" if value is None else str(value)))
                _check(matches, "Private CSV %s differs from its case row" % key)
    return verified


def _check_case(row: dict, observed: dict, path: Path) -> None:
    for name, _entity_type in COUNT_TYPES:
        _check(type(row.get(name)) is int and row[name] == observed["counts"][name],
               "Benchmark %s count differs from independent ANSA readback" % name)
    _check(row["faces"] == observed["face_count"], "FACE count differs from geometry readback")
    for name in ("largest_face_id", "largest_face_area", "top_face_id", "top_face_max_z"):
        left, right = row.get(name), observed[name]
        same = left == right or (
            isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)
            and math.isclose(float(left), float(right), rel_tol=1e-8, abs_tol=1e-8)
        )
        _check(same, "Benchmark %s differs from independent geometry readback" % name)
    if path.suffix.casefold() in {".step", ".stp"}:
        _check(observed["face_count"] == 1
               and math.isclose(row["largest_face_area"], 200.0, rel_tol=1e-8, abs_tol=1e-8)
               and math.isclose(row["top_face_max_z"], 0.0, abs_tol=1e-8),
               "Known 20x10 mm STEP must contain one FACE with area/top Z 200/0")


async def _unchanged_after(h, two, identity, before):
    current = _remote(two, identity)
    _check(_model_signature(current) == _model_signature(before),
           "Rejected request changed model contents, database path, or epoch")
    snapshot = await h.snapshot_context()
    _check(snapshot["model"]["epoch"] == before["model_epoch"],
           "Rejected request changed the MCP model epoch")
    return current


async def _reject_and_check(h, two, identity, report, report_path, label, arguments,
                            *, task_expected=None, backup_path=None):
    before = _remote(two, identity)
    old_backup = _fingerprint(backup_path) if backup_path and Path(backup_path).exists() else None
    outcome = await _rejection(h, report, report_path, label, arguments,
                               task_expected=task_expected)
    after = await _unchanged_after(h, two, identity, before)
    _check(outcome["rejected"], "%s was not rejected" % label)
    if old_backup is not None:
        _check(_fingerprint(backup_path) == old_backup, "%s changed its existing backup" % label)
    elif backup_path:
        _check(not Path(backup_path).exists(), "%s created a backup" % label)
    return outcome, after


async def _verify_cases(instructions, identity, helpers, run_dir, report, report_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stage_one, two, _original_channel = helpers
    report.update(visual_acceptance=False, cases=[], task_ids={})
    report_path = run_dir / REPORT_NAME
    seed = _remote(two, identity)
    seed_card = _assert_seed_card(seed, identity["seed_entity_id"])
    _check(_same_path(seed["database"], identity["seed_path"]), "Owned seed is not the active database")
    # Stabilize only the owned test fixture's native serialization lifecycle.
    normalized = _remote(two, identity, restore_path=identity["seed_path"])
    _assert_seed_card(normalized, identity["seed_entity_id"], seed_card)
    report["fixture_initial_open"] = {"before_counts": seed["counts"],
        "after_counts": normalized["counts"], "open": normalized["restore_open"],
        "full_grid_card_preserved": True}
    seed = normalized
    seed_file_before = _fingerprint(Path(identity["seed_path"]))
    root, inputs, source_records = _prepare_inputs(run_dir, identity)
    ordered = _ordered(inputs)
    input_evidence = [{
        "name": path.name, "name_length": len(path.name), "path": str(path.resolve()),
        "size_bytes": path.stat().st_size, "sha256": _fingerprint(path)["sha256"],
        "sort_key": [path.stat().st_size, str(path).casefold()],
    } for path in ordered]
    _check({row["name"] for row in input_evidence} == {"ownedseed.ansa", "rectangle-20x10-mm.step"}
           and all(row["name_length"] == len(row["name"]) for row in input_evidence)
           and [row["sort_key"] for row in input_evidence]
           == sorted(row["sort_key"] for row in input_evidence),
           "Independent root copy names, lengths, or size/path ordering differ")
    _record(report, report_path, "owned_seed_card_and_independent_root_copy", {
        "seed_path": seed["database"], "seed_id": identity["seed_entity_id"],
        "seed_card_fields": seed_card["card_fields"], "seed_card": seed_card,
        "source_fingerprints": source_records, "root_path": str(root.resolve()),
        "input_order": input_evidence,
    })

    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
    task_to_resume = None
    with (run_dir / "benchmark-backend.stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                schema_inventory = await h.list_schema()
                listed = await client.list_tools()
                tool = next((item for item in listed.tools if item.name == TOOL), None)
                _check(tool is not None, "ansa_benchmark_run is not registered")
                schema = _assert_schema(tool)
                await common._bind_for_run(h, identity, stage_one, two, identity["port"])
                baseline = _remote(two, identity)
                _assert_seed_card(baseline, identity["seed_entity_id"], seed_card)
                _check(_same_path(baseline["database"], identity["seed_path"]),
                       "Owned seed changed during MCP binding")
                baseline_signature = _model_signature(baseline)

                rejected = []
                invalid = (
                    ("max_cases_null", {"max_cases": None}),
                    ("max_cases_boolean", {"max_cases": True}),
                    ("max_cases_zero", {"max_cases": 0}),
                    ("max_cases_above_limit", {"max_cases": 11}),
                    ("ttl_boolean", {schema["ttl_field"]: True}),
                    ("backup_path_null", {"backup_path": None}),
                    ("unknown_argument", {"unexpected": True}),
                    ("empty_root", {"root_path": ""}),
                    ("traversal_root", {"root_path": str(root / ".." / "outside")}),
                )
                for index, (label, override) in enumerate(invalid):
                    fields = {"root_path": str(root.resolve()),
                              "backup_path": str(run_dir / ("rejected-%02d.ansa" % index)),
                              "max_cases": 10, **override}
                    arguments = _arguments(h, "benchmark-schema-" + uuid.uuid4().hex, **fields)
                    if label == "unknown_argument":
                        arguments["unexpected"] = True
                    outcome, _ = await _reject_and_check(
                        h, two, identity, report, report_path, label, arguments,
                        task_expected=False, backup_path=fields.get("backup_path"),
                    )
                    rejected.append(outcome)

                empty_root, unsupported_root = run_dir / "benchmark-empty", run_dir / "benchmark-unsupported"
                empty_root.mkdir(exist_ok=False)
                unsupported_root.mkdir(exist_ok=False)
                (unsupported_root / "not-a-model.csv").write_text("not an ANSA model\n", encoding="utf-8")
                preflight = (
                    ("empty_directory", empty_root, run_dir / "rejected-empty.ansa"),
                    ("unsupported_format", unsupported_root, run_dir / "rejected-format.ansa"),
                    ("existing_backup_collision", root, Path(identity["seed_path"])),
                )
                for label, input_root, backup in preflight:
                    arguments = _arguments(
                        h, "benchmark-preflight-" + uuid.uuid4().hex,
                        root_path=str(input_root.resolve()), backup_path=str(backup.resolve()), max_cases=10,
                    )
                    outcome, _ = await _reject_and_check(
                        h, two, identity, report, report_path, label, arguments,
                        backup_path=backup,
                    )
                    rejected.append(outcome)
                seed_file_after = _fingerprint(Path(identity["seed_path"]))
                _check(seed_file_after == seed_file_before, "Rejected inputs changed the owned seed file")
                _record(report, report_path, "strict_schema_and_preflight_rejections", {
                    "schema": schema, "registered_tool_count": schema_inventory["count"],
                    "rejections": rejected, "baseline": baseline_signature,
                    "seed_file_before": seed_file_before, "seed_file_after": seed_file_after,
                })

                success_backup = run_dir / "benchmark-success-backup.ansa"
                _check(not success_backup.exists(), "Success backup path already exists")
                operation_id = "benchmark-success-" + uuid.uuid4().hex
                success_request = _arguments(
                    h, operation_id, root_path=str(root.resolve()),
                    backup_path=str(success_backup.resolve()), max_cases=10,
                )
                submitted, completed = await _submit(
                    h, report, report_path, "successful_two_case_batch", success_request
                )
                success_task_id = submitted["task_id"]
                success = completed.get("data", {}).get("result")
                _check(isinstance(success, dict) and success.get("ok") is True,
                       "Successful benchmark task did not return an ok result")
                _check(_same_path(success.get("root_path"), root)
                       and success.get("case_count") == 2
                       and success.get("attempted_case_count") == 2
                       and success.get("all_cases_verified") is True
                       and success.get("source_hash_unchanged") is True,
                       "Successful batch summary is incomplete")
                _check(success.get("backup", {}).get("verified") is True
                       and success.get("restore", {}).get("confirmed") is True
                       and _same_path(success.get("original_model_path"), identity["seed_path"])
                       and _same_path(success.get("current_model_path"), success_backup)
                       and _same_path(success["restore"].get("current_model_path"), success_backup),
                       "Success backup or restore receipt does not confirm the backup path")
                probe_model = success.get("current_model_probe", {}).get("model")
                _check(isinstance(probe_model, dict)
                       and _same_path(probe_model.get("database"), success_backup),
                       "Current-model probe does not confirm the success backup")
                backup_evidence = _check_backup(success_backup, success["backup"])
                live_restore = _remote(two, identity)
                _assert_seed_card(live_restore, identity["seed_entity_id"], seed_card)
                _check(_same_path(live_restore["database"], success_backup)
                       and live_restore["model_epoch"] == success["restore"].get("model_epoch")
                       and live_restore["model_epoch"] > baseline["model_epoch"],
                       "Read-only live state does not confirm the product's completed backup restore")
                rows = success.get("cases")
                _check(type(rows) is list and len(rows) == 2, "Successful batch omitted case rows")
                for row, path in zip(rows, ordered):
                    fingerprint = _fingerprint(path)
                    _check(_same_path(row.get("input_path"), path)
                           and row.get("ok") is True and row.get("import_ok") is True
                           and row.get("import_status") == "inspected"
                           and (row.get("sha256"), row.get("size_bytes"))
                           == (fingerprint["sha256"], fingerprint["size_bytes"]),
                           "Successful case order, import, or source fingerprint differs")

                case_paths = [{"label": path.name, "path": str(path.resolve())} for path in ordered]
                readback = _remote(two, identity, case_paths, str(success_backup.resolve()))
                observations = readback["input_observations"]
                _check(len(observations) == len(rows), "Independent readback missed a batch case")
                for row, path, observation in zip(rows, ordered, observations):
                    _check_case(row, observation, path)
                _assert_seed_card(readback, identity["seed_entity_id"], seed_card)
                _check(_same_path(readback["database"], success_backup)
                       and readback["model_epoch"] > success["restore"]["model_epoch"],
                       "Actual independent backup Open did not restore the GRID/path and advance epoch")
                after_sources = {key: _fingerprint(Path(value["path"]))
                                 for key, value in source_records.items()}
                _check(all(after_sources[key]["sha256"] == value["sha256"]
                           for key, value in source_records.items()),
                       "Seed, fixture, or independent root-copy SHA-256 changed")
                artifact_evidence = _check_artifacts(success, readback["session_dir"], rows)
                _record(report, report_path, "successful_batch_geometry_restore_and_private_reports", {
                    "result": success, "backup_fingerprint": backup_evidence,
                    "product_live_restore_readback": live_restore,
                    "independent_inputs": observations, "independent_restore": readback["restore_open"],
                    "source_fingerprints_after": after_sources, "artifacts": artifact_evidence,
                })

                before_replay = _remote(two, identity)
                duplicate = await h.client.call_tool(TOOL, success_request)
                duplicate_value = _response_value(duplicate)
                after_replay = _remote(two, identity)
                _check(isinstance(duplicate_value, dict)
                       and duplicate_value.get("task_id") == success_task_id
                       and _model_signature(after_replay) == _model_signature(before_replay),
                       "Idempotent operation replay created a task or reopened the model")
                original_query = await h.call("ansa_task_query", {"task_id": success_task_id})
                _check(original_query.get("task_id") == success_task_id,
                       "Original benchmark task query returned another task")

                conflict_backup = _fingerprint(success_backup)
                conflict = dict(success_request, max_cases=9)
                await h.snapshot_context()
                conflict_outcome, after_conflict = await _reject_and_check(
                    h, two, identity, report, report_path, "conflicting_operation_id",
                    conflict, task_expected=False, backup_path=success_backup,
                )

                stale = _arguments(
                    h, "benchmark-stale-" + uuid.uuid4().hex,
                    root_path=str(root.resolve()),
                    backup_path=str((run_dir / "stale-context-must-not-save.ansa").resolve()),
                    max_cases=10,
                )
                stale["expected_model"] = {"epoch": baseline["model_epoch"]}
                stale_outcome, after_stale = await _reject_and_check(
                    h, two, identity, report, report_path,
                    "stale_model_context", stale, backup_path=stale["backup_path"],
                )
                _record(report, report_path, "backup_card_epoch_and_operation_id_guards", {
                    "restored_seed_card": readback["grid_cards"][0],
                    "backup_path": readback["database"],
                    "task_restore_epoch": success["restore"]["model_epoch"],
                    "independent_open_epoch": readback["model_epoch"],
                    "same_operation_task_id": success_task_id,
                    "repeat_did_not_open": True, "conflict": conflict_outcome,
                    "conflict_model_after": _model_signature(after_conflict),
                    "conflict_backup": conflict_backup, "stale_context": stale_outcome,
                    "stale_model_after": _model_signature(after_stale),
                })

                failed_root = run_dir / "benchmark-partial-inputs"
                failed_root.mkdir(exist_ok=False)
                failed_step = failed_root / "rectangle-20x10-mm.step"
                shutil.copy2(common.PROJECT_ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step",
                             failed_step)
                invalid_ansa = failed_root / "invalid-after-step.ansa"
                invalid_bytes = b"not an ANSA database\n"
                while len(invalid_bytes) <= failed_step.stat().st_size:
                    invalid_bytes += b"not an ANSA database\n"
                invalid_ansa.write_bytes(invalid_bytes)
                failure_paths = _ordered([failed_step, invalid_ansa])
                _check(failure_paths == [failed_step, invalid_ansa],
                       "Failure fixture must sort valid STEP before invalid ANSA by size")
                failure_hashes_before = {str(path): _fingerprint(path)
                                         for path in (failed_step, invalid_ansa)}
                failure_backup = run_dir / "benchmark-failure-backup.ansa"
                failure_args = _arguments(
                    h, "benchmark-partial-" + uuid.uuid4().hex,
                    root_path=str(failed_root.resolve()),
                    backup_path=str(failure_backup.resolve()), max_cases=10,
                )
                failure_submission, failure_task = await _submit(
                    h, report, report_path, "valid_step_then_invalid_ansa", failure_args,
                    expected="failed",
                )
                failure = failure_task.get("data", {}).get("result")
                if isinstance(failure, dict) and isinstance(failure.get("operation_result"), dict):
                    failure = failure["operation_result"]
                _check(isinstance(failure, dict) and failure.get("ok") is False
                       and failure.get("case_count") == 2
                       and failure.get("attempted_case_count") == 2
                       and failure.get("all_cases_verified") is False
                       and failure.get("source_hash_unchanged") is True,
                       "Invalid second file did not produce a real partial failure result")
                failure_rows = failure.get("cases")
                _check(type(failure_rows) is list and len(failure_rows) == 2,
                       "Partial failure omitted attempted case rows")
                first, second = failure_rows
                expected_step_hash = failure_hashes_before[str(failed_step)]
                expected_ansa_hash = failure_hashes_before[str(invalid_ansa)]
                _check(_same_path(first.get("input_path"), failed_step)
                       and first.get("ok") is True and first.get("import_ok") is True
                       and first.get("import_status") == "inspected"
                       and (first.get("sha256"), first.get("size_bytes"))
                       == (expected_step_hash["sha256"], expected_step_hash["size_bytes"])
                       and _same_path(second.get("input_path"), invalid_ansa)
                       and second.get("ok") is False and second.get("import_ok") is False
                       and second.get("import_status") in {"failed", "partial"}
                       and (second.get("sha256"), second.get("size_bytes"))
                       == (expected_ansa_hash["sha256"], expected_ansa_hash["size_bytes"])
                       and bool(second.get("error")),
                       "Partial rows do not prove STEP inspection before invalid ANSA failure")
                _check(failure.get("backup", {}).get("verified") is True
                       and failure.get("restore", {}).get("attempted") is False
                       and failure.get("restore", {}).get("confirmed") is False,
                       "Unknown context must retain the verified backup without claiming an automatic restore")
                _check(bool(failure.get("error") or failure["restore"].get("error")),
                       "Partial failure result omitted its case or unknown-restore error")
                backup_evidence = _check_backup(failure_backup, failure["backup"])
                live_after_failure = _remote(two, identity)
                _check(type(failure.get("current_model_path")) is str
                       and _same_path(live_after_failure["database"], failure["current_model_path"])
                       and _same_path(live_after_failure["database"],
                                      failure["restore"].get("current_model_path")),
                       "Read-only live path differs from the failed batch's observed current path")
                _check(live_after_failure["restore_open"]["model_epoch"]
                       == live_after_failure["model_epoch"],
                       "Failure live readback unexpectedly changed the model epoch")
                failed_artifacts = _check_artifacts(
                    failure, live_after_failure["session_dir"], failure_rows)

                step_readback = _remote(
                    two, identity,
                    [{"label": failed_step.name, "path": str(failed_step.resolve())}],
                )
                partial_observation = step_readback["input_observations"][0]
                _check_case(first, partial_observation, failed_step)

                await h.snapshot_context()
                recovery_preservation = run_dir / "benchmark-failure-pre-recovery.ansa"
                _check(not recovery_preservation.exists(),
                       "Manual recovery preservation path already exists")
                recovery_args = _arguments(
                    h, "benchmark-manual-recovery-" + uuid.uuid4().hex,
                    path=str(failure_backup.resolve()), preservation="backup",
                    backup_path=str(recovery_preservation.resolve()),
                )
                recovery_submission, recovery_task = await _submit(
                    h, report, report_path, "explicit_model_open_failure_backup",
                    recovery_args, tool_name="ansa_model_open",
                )
                recovery_result = recovery_task.get("data", {}).get("result")
                _check(isinstance(recovery_result, dict)
                       and recovery_result.get("action") == "model_open"
                       and _same_path(recovery_result.get("path"), failure_backup)
                       and type(recovery_result.get("return_code")) is int
                       and recovery_result.get("return_code") == 0,
                       "Explicit ansa_model_open did not report opening the verified benchmark backup")
                preserved = recovery_result.get("backup")
                _check(isinstance(preserved, dict) and preserved.get("saved") is True
                       and _same_path(preserved.get("path"), recovery_preservation),
                       "Explicit recovery did not preserve the pre-open live model")
                recovery_preservation_fingerprint = _fingerprint(recovery_preservation)
                await h.snapshot_context()
                failure_readback = _remote(two, identity)
                _assert_seed_card(failure_readback, identity["seed_entity_id"], seed_card)
                _check(_same_path(failure_readback["database"], failure_backup)
                       and failure_readback["model_epoch"] > step_readback["model_epoch"],
                       "Separate explicit model_open did not restore the seed card and backup path")
                failure_hashes_after = {str(path): _fingerprint(path)
                                        for path in (failed_step, invalid_ansa)}
                _check(failure_hashes_after == failure_hashes_before,
                       "Failed-batch inputs changed during execution or independent restore")
                _record(report, report_path, "real_partial_failure_and_independent_restore", {
                    "submission": failure_submission, "result": failure,
                    "backup_fingerprint": backup_evidence,
                    "artifacts": failed_artifacts,
                    "product_live_readback_before_manual_recovery": live_after_failure,
                    "partial_step_readback": partial_observation,
                    "restored_grid_card": failure_readback["grid_cards"][0],
                    "manual_recovery": {
                        "not_product_auto_restore": True,
                        "tool": "ansa_model_open", "submission": recovery_submission,
                        "result": recovery_result,
                        "preservation_backup": str(recovery_preservation.resolve()),
                        "preservation_fingerprint": recovery_preservation_fingerprint,
                        "readback": _model_signature(failure_readback),
                    },
                    "input_hashes": failure_hashes_after,
                })
                task_to_resume = {
                    "task_id": success_task_id, "operation_id": operation_id,
                    "result": success, "model_before_restart": _model_signature(failure_readback),
                }

    _check(task_to_resume is not None, "No completed benchmark task is available for restart query")
    before_restart = _remote(two, identity)
    _check(_model_signature(before_restart) == task_to_resume["model_before_restart"],
           "Model changed after the original backend session closed")
    restart = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
    with (run_dir / "benchmark-backend-restart.stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(restart, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                restarted = common.McpHarness(client, report, run_dir)
                inventory = await restarted.list_schema()
                queried = await restarted.call("ansa_task_query", {"task_id": task_to_resume["task_id"]})
                _check(queried.get("task_id") == task_to_resume["task_id"]
                       and queried.get("status") == "succeeded"
                       and queried.get("data", {}).get("result") == task_to_resume["result"],
                       "Backend restart did not return the original persisted task without replay")
    after_restart = _remote(two, identity)
    _check(_model_signature(after_restart) == task_to_resume["model_before_restart"],
           "Backend restart replayed the task or changed the active model")
    _record(report, report_path, "backend_restart_queries_original_task_without_replay", {
        "task_id": task_to_resume["task_id"], "operation_id": task_to_resume["operation_id"],
        "status": queried["status"], "task_result_unchanged": True,
        "model_before_restart": task_to_resume["model_before_restart"],
        "model_after_restart": _model_signature(after_restart),
        "registered_tool_count": inventory["count"],
    })
    _check(len(report["cases"]) == 6, "Expected six native benchmark acceptance groups")


async def verify(instructions, identity, helpers, run_dir, report, report_path):
    try:
        await _verify_cases(instructions, identity, helpers, run_dir, report, report_path)
    except BaseException:
        report["error_details"] = traceback.format_exc()[-32768:]
        _write(report_path, report)
        raise


def main(argv=None) -> int:
    return owned.main(argv, report_name=REPORT_NAME, scope=SCOPE, verify_callback=verify)


if __name__ == "__main__":
    raise SystemExit(main())
