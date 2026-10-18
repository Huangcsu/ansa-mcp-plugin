"""在独占 ANSA GUI 中验证真实 Include 查询、分页与保存重开。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import traceback
import uuid

import stage_three_ansa as stage


def remote_prepare_includes(expected_pid, expected_arguments, seed_path, seed_id, fixture_path):
    import builtins
    import os
    from pathlib import Path
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Include fixture requires the owned ANSA main thread")
    if (not Path(seed_path).is_file() or Path(fixture_path).exists()
            or builtins._ansa_mcp_plugin_bridge.state.journal.busy()):
        raise RuntimeError("Include fixture requires a saved baseline, a new path and no pending tasks")
    # Initialize the owned visible MDI before reopening the common saved seed.
    session.New("discard")
    status = base.Open(seed_path)
    deck = constants.NASTRAN
    nodes = base.CollectEntities(deck, None, "GRID")
    if (status != 0 or base.CurrentDeck() != deck or len(nodes) != 1
            or nodes[0]._id != seed_id or not nodes[0].is_usable()
            or Path(base.DataBaseName()).resolve() != Path(seed_path).resolve()
            or base.CollectEntities(deck, None, "INCLUDE")):
        raise RuntimeError("The isolated baseline does not match the expected one-node fixture")
    includes = [base.CreateEntity(deck, "INCLUDE", {"Name": name})
                for name in ("mcp_parent.nas", "mcp_child.nas", "mcp_empty.nas")]
    extra_nodes = [base.CreateEntity(deck, "GRID", {"X1": x, "X2": x + 1, "X3": x + 2})
                   for x in (4.0, 7.0)]
    if any(entity is None or not entity.is_usable() for entity in includes + extra_nodes):
        raise RuntimeError("Include fixture creation failed; retain the owned instance, do not replay")
    additions = [base.AddToInclude(includes[0], includes[1]),
                 base.AddToInclude(includes[0], extra_nodes[0]),
                 base.AddToInclude(includes[1], extra_nodes[1])]
    if any(type(code) is not int or code != 1 for code in additions):
        raise RuntimeError("Include fixture assignment failed; effects may exist")
    status = base.SaveAs(fixture_path)
    if (status != 0 or not Path(fixture_path).is_file() or Path(fixture_path).stat().st_size == 0
            or Path(base.DataBaseName()).resolve() != Path(fixture_path).resolve()):
        raise RuntimeError("Include fixture was not saved to its unique path")
    return {"deck": deck, "seed_id": seed_id, "include_ids": [item._id for item in includes],
            "extra_node_ids": [item._id for item in extra_nodes], "add_to_include_codes": additions,
            "save_code": status, "fixture_path": fixture_path}


def remote_include_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Include oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    def reference(entity):
        return None if entity is None else {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}
    includes = []
    for entity in sorted(base.CollectEntities(deck, None, "INCLUDE"), key=lambda item: item._id):
        labels = list(entity.card_fields(deck))
        if "Name" not in labels:
            raise RuntimeError("The 2026.1 Include fixture has no Name card field")
        includes.append({"entity": reference(entity), "labels": labels,
                         "fields": base.GetEntityCardValues(deck, entity, ("Name",)),
                         "parent": reference(base.GetEntityInclude(entity)),
                         "grid_direct_ids": sorted(item._id for item in base.CollectEntities(deck, entity, "GRID", False)),
                         "grid_recursive_ids": sorted(item._id for item in base.CollectEntities(deck, entity, "GRID", True))})
    nodes = [{"entity": reference(entity), "include": reference(base.GetEntityInclude(entity)),
              "fields": base.GetEntityCardValues(deck, entity, ("X1", "X2", "X3"))}
             for entity in sorted(base.CollectEntities(deck, None, "GRID"), key=lambda item: item._id)]
    return {"pid": os.getpid(), "version": str(constants.app_version), "deck": deck,
            "model_path": base.DataBaseName(), "includes": includes, "nodes": nodes,
            "shell_ids": sorted(item._id for item in base.CollectEntities(deck, None, "SHELL")),
            "face_ids": sorted(item._id for item in base.CollectEntities(deck, None, "FACE"))}


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "includes-report.json", report)
        print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)
    def oracle():
        return two.rpc(identity["port"], remote_include_readback, identity["pid"], identity["program_args"])
    def rows(value):
        return [{"entity": item["entity"], "fields": item["fields"]} for item in value["includes"]]

    before = oracle()
    report["initial_oracle"] = before
    parent, child, empty = report["fixture"]["include_ids"]
    parent_node, child_node = report["fixture"]["extra_node_ids"]
    by_id = {item["entity"]["id"]: item for item in before["includes"]}
    assert len(by_id) == 3 and by_id[child]["parent"] == by_id[parent]["entity"]
    for include_id, name in zip((parent, child, empty), ("mcp_parent.nas", "mcp_child.nas", "mcp_empty.nas")):
        assert by_id[include_id]["fields"] == {"Name": name}
    assert by_id[parent]["parent"] is None and by_id[empty]["parent"] is None
    assert by_id[parent]["grid_direct_ids"] == [parent_node]
    assert by_id[parent]["grid_recursive_ids"] == sorted((parent_node, child_node))
    assert by_id[child]["grid_direct_ids"] == by_id[child]["grid_recursive_ids"] == [child_node]
    assert by_id[empty]["grid_direct_ids"] == by_id[empty]["grid_recursive_ids"] == []
    nodes_by_id = {item["entity"]["id"]: item for item in before["nodes"]}
    assert len(nodes_by_id) == 3
    for node_id, include_id, x in ((parent_node, parent, 4.0), (child_node, child, 7.0)):
        assert nodes_by_id[node_id]["include"] == by_id[include_id]["entity"]
        assert nodes_by_id[node_id]["fields"] == {"X1": x, "X2": x + 1, "X3": x + 2}
    seed = next(item for item in before["nodes"] if item["entity"]["id"] == identity["seed_entity_id"])
    assert seed["include"] is None and seed["fields"] == {"X1": 1.0, "X2": 2.0, "X3": 3.0}
    record("real_parent_child_and_empty_include_fixture", before)

    with (run / "includes-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                record("registered_tool_schemas", await h.list_schema())
                report["target"] = h.target
                full = await h.direct_result("ansa_model_includes", {"deck": before["deck"], "limit": 200})
                assert full["total"] == 3 and full["includes"] == rows(before) and oracle() == before
                record("include_ids_and_names_match_independent_oracle_without_changes", full)
                pages = [await h.direct_result("ansa_model_includes", {"offset": offset, "limit": 1})
                         for offset in range(4)]
                assert all(page["total"] == 3 and page["offset"] == index and page["limit"] == 1
                           for index, page in enumerate(pages))
                assert [row for page in pages for row in page["includes"]] == rows(before)
                assert pages[-1]["includes"] == [] and oracle() == before
                record("bounded_pagination_includes_child_and_empty_container", {"pages": pages})
                invalid = await client.call_tool("ansa_model_includes", {
                    "operation_id": "invalid-page-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": h.model, "offset": -1, "limit": 201,
                })
                assert invalid.isError and oracle() == before
                wrong_deck = await h.direct_result("ansa_model_includes", {"deck": 999}, expected="failed")
                assert oracle() == before
                record("invalid_page_and_wrong_deck_rejected_without_changes", {"schema_error": invalid.isError, "task": wrong_deck})

                saved_path = run / "includes-saved-copy.ansa"
                saved = await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                after_save = oracle()
                assert saved_path.is_file() and saved_path.stat().st_size > 0 and after_save == before
                await h.snapshot_context()
                previous_model = h.model.copy()
                opened = await h.direct_result("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(run / "before-include-reopen.ansa"),
                })
                await h.snapshot_context()
                reopened = oracle()
                report["saved_copy_open_diagnostics"] = {"saved": saved, "opened": opened,
                       "model_before": previous_model, "model_after": h.model, "independent": reopened}
                stage._json_write(run / "includes-report.json", report)
                assert h.model["epoch"] > previous_model["epoch"]
                assert Path(reopened["model_path"]).resolve() == saved_path.resolve()
                assert {key: value for key, value in reopened.items() if key != "model_path"} == {
                    key: value for key, value in after_save.items() if key != "model_path"}
                full = await h.direct_result("ansa_model_includes", {})
                assert full["total"] == 3 and full["includes"] == rows(reopened)
                record("save_copy_and_open_preserve_names_hierarchy_members_and_coordinates", {
                    "saved": saved, "opened": opened, "model_before": previous_model,
                    "model_after": h.model, "read": full, "independent": reopened,
                })

                previous_model = h.model.copy()
                same_path = await h.direct_result("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(run / "before-same-path-include-reopen.ansa"),
                })
                await h.snapshot_context()
                assert h.model["epoch"] > previous_model["epoch"] and oracle() == reopened
                full = await h.direct_result("ansa_model_includes", {})
                assert full["includes"] == rows(reopened)
                record("same_path_reopen_refreshes_model_context_and_preserves_include_rows", {
                    "opened": same_path, "model_before": previous_model, "model_after": h.model, "read": full,
                })

                if wait_ui:
                    marker = run / "includes-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale Include GUI evidence")
                    stage._json_write(run / "includes-ui-instruction.json", {
                        "identity": identity, "window_file": Path(reopened["model_path"]).name,
                        "model_path": reopened["model_path"],
                        "expected_rows": rows(reopened),
                        "expected_parent_links": [{"id": item["entity"]["id"],
                                                   "parent_id": None if item["parent"] is None else item["parent"]["id"]}
                                                  for item in reopened["includes"]],
                        "instruction": "在所属 ANSA 主窗口的数据库 Include 列表核对三个名称、ID 及父子关系；关闭查询窗口后记录本轮确认。",
                    })
                    print(json.dumps({"status": "waiting_for_includes_ui", "run_dir": str(run)}, ensure_ascii=True), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Include GUI check timed out; retain the original report and owned instance")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("expected_rows") != rows(reopened)
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("observed_parent_links") != [
                                {"id": item["entity"]["id"], "parent_id": None if item["parent"] is None else item["parent"]["id"]}
                                for item in reopened["includes"]]):
                        raise RuntimeError("Include GUI evidence does not match this process or data")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_database_include_list_and_post_ui_readback", visual)
                report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wait-ui", action="store_true")
    parser.add_argument("--keep-ansa", action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    if (run / "seed-baseline.ansa").exists() or (run / "includes-report.json").exists():
        raise ValueError("仅接受新准备目录；结果未知时核对原报告，不重放 Include 夹具")
    report = {"scope": "ANSA 2026.1 GUI/NASTRAN 真实 Include 查询；MCP stdio → 自研 TCP → 所属主线程",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "ribbon_lifecycle_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        fixture_path = run / "includes-fixture.ansa"
        report["fixture"] = helpers[1].rpc(identity["port"], remote_prepare_includes, identity["pid"],
                                          identity["program_args"], identity["seed_path"],
                                          identity["seed_entity_id"], str(fixture_path))
        shutil.copy2(fixture_path, run / "includes-fixture-backup.ansa")
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["success"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "includes-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "includes-report.json", report)


if __name__ == "__main__":
    main()
