"""Named synchronous script entrypoints through real MCP and an owned ANSA."""
import asyncio
from datetime import timedelta
from hashlib import sha256
import os
from pathlib import Path
import sys
import uuid

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned


def remote_script_state(expected_pid, expected_arguments, marker):
    import builtins
    import copy
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or threading.current_thread() is not threading.main_thread()
            or list(session.ProgramArguments()) != expected_arguments):
        raise RuntimeError("Owned ANSA identity changed before script readback")
    rows = []
    for node in base.CollectEntities(constants.NASTRAN, None, "GRID"):
        fields = ("X1", "X2", "X3")
        if not set(fields).issubset(node.card_fields(constants.NASTRAN)):
            raise RuntimeError("GRID coordinate fields unavailable")
        rows.append({"id": node._id, "fields": base.GetEntityCardValues(constants.NASTRAN, node, fields)})
    return {"events": copy.deepcopy(getattr(builtins, marker, {})), "deck": base.CurrentDeck(),
            "cards": sorted(rows, key=lambda row: row["id"]), "main_thread": True}


async def verify(instructions, identity, helpers, run_dir, report, path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _ = helpers
    port = identity["port"]
    two.rpc(port, owned.remote_refresh_catalog, identity["pid"], identity["program_args"])
    marker = "_ansa_entrypoint_" + uuid.uuid4().hex
    prefix = ("import builtins\n"
              f"events = getattr(builtins, {marker!r}, {{}})\n"
              f"setattr(builtins, {marker!r}, events)\n")
    def readback():
        return two.rpc(port, remote_script_state, identity["pid"], identity["program_args"], marker)
    before = readback()
    common._json_write(run_dir / "script-entrypoint-before.json", before)
    baseline_hash = sha256(Path(identity["seed_path"]).read_bytes()).hexdigest()
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    originals = []
    with (run_dir / "script-entrypoint-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h, identity, one, two, port)
                first_script = prefix + (
                    "events['top'] = events.get('top', 0) + 1\n"
                    "import threading\n"
                    "def main():\n"
                    "    events['calls'] = events.get('calls', 0) + 1\n"
                    "    print('named-main-ran')\n"
                    "    return {'calls': events['calls'], 'deck': base.CurrentDeck(),\n"
                    "            'main_thread': threading.current_thread() is threading.main_thread()}\n"
                    "if __name__ == '__main__':\n"
                    "    raise RuntimeError('main guard must not run automatically')\n")
                op = "entrypoint-main-" + uuid.uuid4().hex
                _, first = await h.task("ansa_script_execute", {"script": first_script, "function_name": "main"}, operation_id=op)
                data = first["data"]["result"]
                assert data["result"] == {"calls": 1, "deck": before["deck"], "main_thread": True}
                assert data["stdout"] == "named-main-ran\n" and not data["stdout_truncated"]
                _, repeat = await h.task("ansa_script_execute", {"script": first_script, "function_name": "main"}, operation_id=op)
                assert repeat["task_id"] == first["task_id"]
                assert readback()["events"] == {"top": 1, "calls": 1}
                originals.append(first)
                owned.record(report, path, "named_main_owning_thread_stdout_and_idempotency", first)

                priority = await h.direct_result("ansa_script_execute", {
                    "script": "RESULT={'selected':'RESULT'}\ndef main():\n    return {'selected':'return'}\n", "function_name": "main"})
                assert priority["result"] == {"selected": "RESULT"}
                nothing = await h.direct_result("ansa_script_execute", {"script": "def main():\n    pass\n", "function_name": "main"})
                assert nothing["result"] is None
                literal = await h.direct_result("ansa_script_execute", {"script": "def null():\n    return {'called':'null'}\n", "function_name": "null"})
                assert literal["result"] == {"called": "null"}
                no_function = await h.direct_result("ansa_script_execute", {
                    "script": "RESULT={'called':False}\ndef main():\n    RESULT['called']=True\n", "function_name": None})
                assert no_function["result"] == {"called": False}
                owned.record(report, path, "result_priority_none_return_literal_null_and_explicit_null", {
                    "priority": priority["result"], "none_return": nothing["result"], "literal_null": literal["result"],
                    "no_automatic_main": no_function["result"]})

                invalid_script = prefix + "events['invalid_top']=events.get('invalid_top',0)+1\nRESULT={}\n"
                for name in ("package.main", "class", " main"):
                    await h.task("ansa_script_execute", {"script": invalid_script, "function_name": name}, expected="failed")
                invalid_arguments = ({"function_name": ""}, {"function_name": True}, {"function_name": 1},
                    {"function_name": "m"*129}, {"validate": "false"}, {"allow_model_change": 1}, {"extra": True})
                for fields in invalid_arguments:
                    response = await client.call_tool("ansa_script_execute", {
                        "operation_id": "entrypoint-rejected-" + uuid.uuid4().hex, "expected_target": h.target,
                        "expected_model": h.model, "script": invalid_script, **fields})
                    assert response.isError, fields
                assert "invalid_top" not in readback()["events"]
                owned.record(report, path, "invalid_entrypoint_and_strict_schema_before_top_level", {"format_rejections": 3, "schema_rejections": len(invalid_arguments)})

                failures = []
                variants = ("main=123\n", "def other():\n    pass\n", "def main(required):\n    pass\n",
                    "def main():\n    raise SystemExit(3)\n",
                    "async def main():\n    events['async_body']=1\n",
                    "def main():\n    events['generator_body']=1\n    yield 1\n")
                for body in variants:
                    _, failure = await h.task("ansa_script_execute", {
                        "script": prefix + "events['failure_top']=events.get('failure_top',0)+1\nRESULT={}\n" + body,
                        "function_name": "main"}, expected="failed")
                    assert "effects may exist" in failure["error"]["message"]
                    failures.append(failure["task_id"])
                events = readback()["events"]
                assert events["failure_top"] == len(variants)
                assert "async_body" not in events and "generator_body" not in events
                owned.record(report, path, "resolution_call_and_async_failures_retain_top_level_effects", {"task_ids": failures, "events": events})

                partial = prefix + (
                    "def main():\n"
                    "    events['partial_calls']=events.get('partial_calls',0)+1\n"
                    "    node=base.CreateEntity(constants.NASTRAN,'GRID',{'X1':10.0,'X2':20.0,'X3':30.0})\n"
                    "    if node is None:\n"
                    "        raise RuntimeError('GRID fixture creation failed')\n"
                    "    events['created_id']=node._id\n"
                    "    raise RuntimeError('controlled entrypoint failure after GRID creation')\n")
                partial_op = "entrypoint-partial-" + uuid.uuid4().hex
                _, failed = await h.task("ansa_script_execute", {"script": partial, "function_name": "main"},
                                         operation_id=partial_op, expected="failed")
                effects = failed["data"]["result"]
                assert effects["possible_effects"] is True and effects["model_after"] is not None
                assert effects["main_thread"] is True
                after_partial = readback()
                assert len(after_partial["cards"]) == len(before["cards"]) + 1
                assert all(row in after_partial["cards"] for row in before["cards"])
                assert next(row["fields"] for row in after_partial["cards"] if row["id"] == after_partial["events"]["created_id"]) == {"X1": 10.0, "X2": 20.0, "X3": 30.0}
                _, failed_repeat = await h.task("ansa_script_execute", {"script": partial, "function_name": "main"},
                                                operation_id=partial_op, expected="failed")
                assert failed_repeat["task_id"] == failed["task_id"] and readback() == after_partial
                originals.append(failed)
                owned.record(report, path, "partial_grid_write_failure_original_receipt_and_no_replay", {"task": failed, "independent_readback": after_partial})

                _, bad_json = await h.task("ansa_script_execute", {"script": "def main():\n    return (1,2)\n", "function_name": "main"}, expected="failed")
                assert "effects may exist" in bad_json["error"]["message"]
                stale = await h.call("ansa_script_execute", {"operation_id": "entrypoint-stale-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": {"epoch": h.model["epoch"] + 1},
                    "script": invalid_script, "function_name": "main"}, allow_error=True)
                assert stale["ok"] is False and stale["error"]["code"] == "TASK_REJECTED"
                assert readback() == after_partial
                saved_path = run_dir / "script-entrypoint-model.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                assert saved_path.is_file() and saved_path.stat().st_size > 0
                assert sha256(Path(identity["seed_path"]).read_bytes()).hexdigest() == baseline_hash
                stable = readback()
                common._json_write(run_dir / "script-entrypoint-after.json", stable)
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                for original in originals:
                    result = await h.call("ansa_task_query", {"operation_id": original["operation_id"]}, allow_error=True)
                    assert result["task_id"] == original["task_id"] and result["status"] == original["status"]
                    assert result["data"]["result"] == original["data"]["result"]
                assert readback() == stable
                owned.record(report, path, "strict_result_stale_model_and_restart_original_queries", {
                    "strict_result_task": bad_json["task_id"], "stale_error": stale["error"],
                    "original_task_ids": [r["task_id"] for r in originals], "after": stable,
                    "baseline_sha256": baseline_hash, "saved_model": str(saved_path),
                    "saved_sha256": sha256(saved_path.read_bytes()).hexdigest()})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="script-entrypoint-report.json",
        scope="named synchronous script entrypoints", verify_callback=verify))
