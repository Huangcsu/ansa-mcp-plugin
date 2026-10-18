"""Verify runtime API catalogs through MCP in an independently owned ANSA.

Use stage_three_ansa.py --prepare-only and launch its unique translator first.
No model or canvas acceptance is inferred from these metadata checks.
"""
import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as common


MODULES = ("base", "mesh", "session", "constants", "connections", "cad", "morph", "guitk", "utils", "batchmesh", "kinetics")


def remote_refresh_catalog(expected_pid, expected_arguments):
    import builtins
    import importlib
    import os
    import threading
    from ansa import session
    if (os.getpid() != expected_pid or threading.current_thread() is not threading.main_thread()
            or list(session.ProgramArguments()) != expected_arguments):
        raise RuntimeError("Owned process identity changed before catalog refresh")
    bridge = builtins._ansa_mcp_plugin_bridge
    if bridge.state.journal.busy():
        raise RuntimeError("Catalog refresh requires an idle isolated bridge")
    names = ("ansa_mcp_bridge.operations.api_catalog", "ansa_mcp_bridge.operations.scripts")
    for name in names:
        importlib.reload(importlib.import_module(name))
    return {"modules": list(names), "main_thread": True}


def remote_catalog_oracle(expected_pid, expected_arguments):
    """Read runtime names independently with inspect.getmembers, never call them."""
    import importlib
    import inspect
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or threading.current_thread() is not threading.main_thread()
            or list(session.ProgramArguments()) != expected_arguments):
        raise RuntimeError("Owned process identity changed before catalog oracle")
    names = ("base", "mesh", "session", "constants", "connections", "cad", "morph", "guitk", "utils", "batchmesh")
    modules = {}
    for name in names:
        try:
            source = constants if name == "constants" else importlib.import_module("ansa." + name)
            modules[name] = sorted(n for n, _ in inspect.getmembers(source, inspect.isroutine) if not n.startswith("_"))
        except ImportError as exc:
            modules[name] = {"error_type": type(exc).__name__}
    cards = []
    for node in base.CollectEntities(constants.NASTRAN, None, "GRID"):
        cards.append({"id": node._id, "fields": base.GetEntityCardValues(
            constants.NASTRAN, node, ("X1", "X2", "X3"))})
    return {"version": constants.app_version, "main_thread": True,
            "modules": modules, "deck": base.CurrentDeck(), "cards": sorted(cards, key=lambda row: row["id"])}


def record(report, path, name, evidence):
    report["cases"].append({"name": name, "ok": True, "evidence": evidence})
    common._json_write(path, report)
    print(json.dumps({"case": name, "ok": True}), flush=True)


async def verify(instructions, identity, helpers, run_dir, report, path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    stage_one, stage_two, _ = helpers
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    report["module_refresh"] = stage_two.rpc(
        identity["port"], remote_refresh_catalog, identity["pid"], identity["program_args"])
    oracle = stage_two.rpc(identity["port"], remote_catalog_oracle, identity["pid"], identity["program_args"])
    common._json_write(run_dir / "api-catalog-oracle-before.json", oracle)
    baseline_hash = hashlib.sha256(Path(identity["seed_path"]).read_bytes()).hexdigest()
    with (run_dir / "api-catalog-backend.stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(parameters, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                before_snapshot = await common._bind_for_run(h, identity, stage_one, stage_two, identity["port"])
                report["expected_target"] = h.target
                report["expected_model"] = h.model
                modules = await h.direct_result("ansa_api_reference", {"action": "modules"})
                assert modules["version"] == oracle["version"]
                assert modules["modules"] == list(MODULES)
                rows = modules["module_details"]
                assert [r["module"] for r in rows] == sorted(MODULES)
                for row in rows:
                    actual = oracle["modules"][row["module"]]
                    assert isinstance(actual, list), (row, actual)
                    assert row["available"] is True and row["function_count"] == len(actual)
                assert modules["complete"] is True
                assert modules["function_count"] == sum(len(v) for v in oracle["modules"].values())
                filtered = await h.direct_result("ansa_api_reference", {"action": "modules", "module": "base"})
                assert [r["module"] for r in filtered["module_details"]] == ["base"]
                assert filtered["modules"] == list(MODULES)
                assert filtered["function_count"] == len(oracle["modules"]["base"])
                record(report, path, "runtime_modules_counts_filter_and_version", {"all": modules, "base": filtered})

                categories = await h.direct_result("ansa_api_reference", {"action": "categories"})
                groups = categories["categories"]
                assert groups and groups == sorted(groups, key=lambda r: (r["category"], r["module"]))
                assert categories["function_count"] == modules["function_count"]
                assert sum(g["function_count"] for g in groups) == modules["function_count"]
                for name in MODULES:
                    assert sum(g["function_count"] for g in groups if g["module"] == name) == len(oracle["modules"][name])
                assert "official" in categories["classification_source"].casefold()
                base = await h.direct_result("ansa_api_reference", {"action": "categories", "module": "base"})
                expected_queries = sum(any(word in n.lower() for word in ("get", "collect", "find", "filter"))
                                       for n in oracle["modules"]["base"])
                assert next(g["function_count"] for g in base["categories"] if g["category"] == "base_query") == expected_queries
                assert all(g["module"] == "base" for g in base["categories"])
                record(report, path, "runtime_category_partition_counts_and_explicit_rule_source", categories)

                lookup = await h.direct_result("ansa_api_reference", {"query": "base.CurrentDeck"})
                assert lookup["api"] == "ansa.base.CurrentDeck" and lookup["documentation"]
                search = await h.direct_result("ansa_api_reference", {"action": "search", "module": "base", "query": "CurrentDeck", "limit": 1})
                assert search["matches"][0]["api"] == lookup["api"]
                assert lookup["version"] == search["version"] == oracle["version"]
                record(report, path, "lookup_search_compatibility", {"lookup": lookup, "search": search})

                invalid_tasks = []
                for fields in ({"action": "modules", "module": "null"}, {"action": "categories", "module": "os"},
                               {"action": "categories", "query": "filter"}):
                    _, result = await h.task("ansa_api_reference", fields, expected="failed")
                    invalid_tasks.append({"fields": fields, "task_id": result["task_id"], "error": result["error"]})
                for fields in ({"limit": True}, {"limit": "1"}, {"limit": 0}, {"limit": 21}, {"unknown": True}):
                    response = await client.call_tool("ansa_api_reference", {
                        "operation_id": "api-catalog-rejected-" + uuid.uuid4().hex,
                        "expected_target": h.target, "expected_model": h.model, "action": "modules", **fields})
                    assert response.isError, fields
                record(report, path, "invalid_scope_query_and_raw_strict_mcp_arguments", {"failed_tasks": invalid_tasks, "schema_rejections": 5})

                operation_id = "api-catalog-idempotent-" + uuid.uuid4().hex
                _, first = await h.task("ansa_api_reference", {"action": "modules"}, operation_id=operation_id)
                _, repeat = await h.task("ansa_api_reference", {"action": "modules"}, operation_id=operation_id)
                assert first["task_id"] == repeat["task_id"]
                assert first["data"]["result"] == repeat["data"]["result"] == modules
                queried = await h.call("ansa_task_query", {"operation_id": operation_id})
                assert queried["task_id"] == first["task_id"]
                after_snapshot = await h.snapshot_context()
                assert after_snapshot["model"] == before_snapshot["model"]
                after = stage_two.rpc(identity["port"], remote_catalog_oracle, identity["pid"], identity["program_args"])
                common._json_write(run_dir / "api-catalog-oracle-after.json", after)
                assert after == oracle
                assert hashlib.sha256(Path(identity["seed_path"]).read_bytes()).hexdigest() == baseline_hash
                record(report, path, "idempotent_original_task_and_independent_unchanged_model", {
                    "task_id": first["task_id"], "operation_id": operation_id, "seed_sha256": baseline_hash,
                    "cards": after["cards"], "snapshot_model": after_snapshot["model"]})


def main(argv=None, *, report_name="api-catalog-report.json",
         scope="runtime API module/category metadata", verify_callback=None):
    parser = argparse.ArgumentParser(description=scope)
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    run_dir, instructions = common._load_run(args)
    if Path(report_name).name != report_name:
        raise ValueError("Acceptance report must be a filename in the prepared run directory")
    path = run_dir / report_name
    if path.exists():
        raise ValueError("Use a fresh prepared run directory; previous reports are immutable")
    report = {"success": False, "scope": scope, "cases": [],
              "run_dir": str(run_dir), "runtime_home": str(args.runtime_home), "visual_acceptance": False}
    common._json_write(path, report)
    identity = helpers = None
    grpc_module = original_channel = None
    try:
        # Normal-user CIM evidence must precede the helper's fixture write.
        proof = json.loads((run_dir / "normal-user-process-before.json").read_text(encoding="utf-8-sig"))
        proof_rows = proof if isinstance(proof, list) else [proof]
        owned = [r for r in proof_rows if r.get("CommandLine") and
                 common._normalize_argument(instructions["translator"]) in common._normalize_argument(r["CommandLine"])]
        if len(owned) != 1 or not owned[0].get("CreationDate"):
            raise RuntimeError("Missing unique normal-user PID/start/argument evidence")
        one, two = common._import_runtime_helpers()
        import grpc
        grpc_module = grpc
        original_channel = common._bounded_grpc(grpc, one.DeadlineInterceptor)
        helpers = (one, two, original_channel)
        basic = two.rpc(instructions["port"], one.remote_basic)
        common._verify_program_arguments(basic, Path(instructions["translator"]), instructions["port"])
        if basic["pid"] != owned[0]["ProcessId"]:
            raise RuntimeError("Normal-user CIM and remote PID differ before fixture creation")
        from ansa_mcp_shared.identity import process_start_filetime
        identity = {"pid": basic["pid"], "program_args": basic["program_args"],
                    "port": instructions["port"], "translator": instructions["translator"],
                    "process_start": process_start_filetime(basic["pid"])}
        report["identity"] = identity
        common._json_write(path, report)
        # Retain the cleanup context before the helper's first fixture write.
        # The helper installs its own bounded channel and returns the same root.
        grpc.insecure_channel = original_channel
        identity, _, _, helpers = common._prepare_owned_ansa(run_dir, instructions)
        report["identity"] = identity
        common._json_write(path, report)
        asyncio.run((verify_callback or verify)(instructions, identity, helpers, run_dir, report, path))
        report["success"] = True
    except BaseException as exc:
        report["success"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        error_name = report_name.removesuffix("-report.json") + "-error.log"
        (run_dir / error_name).write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if identity is not None and helpers is not None:
            try:
                report["cleanup"] = common._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["success"] = False
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if grpc_module is not None and original_channel is not None:
            grpc_module.insecure_channel = original_channel
        common._json_write(path, report)
    print(json.dumps({"success": report["success"], "cases": len(report["cases"]),
                      "report": str(path), "error": report.get("error"), "cleanup": report.get("cleanup")}), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
