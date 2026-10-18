"""用真实 MCP stdio 和私有 SQLite 验证 runtime memory；不连接 ANSA。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stage_three_ansa import EXPECTED_TOOLS


ROOT = Path(__file__).resolve().parents[2]
MEMORY_TOOLS = {"ansa_memory_read", "ansa_memory_write"}


def _payload(result, *, required=True):
    value = result.structuredContent
    if value is None:
        for item in result.content:
            if item.type == "text":
                try:
                    value = json.loads(item.text)
                except (TypeError, ValueError):
                    continue
                break
    if required and not isinstance(value, dict):
        raise AssertionError(f"MCP 响应缺少结构化结果：{result.content!r}")
    return value


def _ok(result):
    if result.isError:
        raise AssertionError(f"MCP 工具调用失败：{_payload(result, required=False)!r}")
    value = _payload(result)
    assert value.get("ok") is True, value
    return value


def _error(result, code=None):
    assert result.isError is True, _payload(result, required=False)
    value = _payload(result, required=False)
    if code is not None:
        assert value and value.get("error", {}).get("code") == code, value
    return value


async def _call(session, name, arguments=None):
    return await session.call_tool(name, arguments or {})


@asynccontextmanager
async def _session(runtime_home):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(runtime_home)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    # MCP stdout 专用于协议；stderr 丢弃，避免服务诊断混入报告或凭据输出。
    with open(os.devnull, "w", encoding="utf-8") as error_log:
        async with stdio_client(parameters, errlog=error_log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                await client.initialize()
                yield client


def _record(report, name, evidence):
    report["cases"].append({"name": name, "ok": True, "evidence": evidence})
    path = Path(report["report_path"])
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _first_group(client, prefix):
    listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    assert len(EXPECTED_TOOLS) == len(listed.tools) == 58
    assert names == EXPECTED_TOOLS
    assert MEMORY_TOOLS <= names
    assert all(isinstance(tool.inputSchema, dict) and tool.inputSchema and
               isinstance(tool.outputSchema, dict) and tool.outputSchema for tool in listed.tools)
    schemas = {tool.name: tool.inputSchema for tool in listed.tools}
    assert set(schemas["ansa_memory_read"]["properties"]) == {
        "key", "limit", "after_key", "before_note_sequence"}
    assert set(schemas["ansa_memory_write"]["properties"]) == {"operation_id", "change"}
    assert set(schemas["ansa_memory_write"]["required"]) == {"operation_id", "change"}
    assert schemas["ansa_memory_read"]["additionalProperties"] is False
    assert schemas["ansa_memory_write"]["additionalProperties"] is False

    status = _ok(await _call(client, "ansa_connection_status"))["data"]
    assert status["active_target"] is None and status["instances"] == [], status
    empty_read = _ok(await _call(client, "ansa_memory_read", {"limit": 100}))
    assert empty_read["task_id"] is None
    empty = empty_read["data"]
    assert empty["scope"] == "runtime" and empty["preferences"] == [] and empty["notes"] == []
    assert empty["has_more_preferences"] is False and empty["has_more_notes"] is False
    missing_key = prefix + "-missing"
    missing = _ok(await _call(client, "ansa_memory_read", {"key": missing_key}))["data"]
    assert missing == {"scope": "runtime", "key": missing_key, "found": False, "record": None}

    literals = {"literal-null": "null", "literal-true": "true", "literal-one": "1",
                "json-null": None}
    for suffix, value in literals.items():
        key = prefix + "-" + suffix
        change = {"kind": "preference", "key": key, "value": value, "expected_revision": 0}
        receipt = _ok(await _call(client, "ansa_memory_write", {
            "operation_id": prefix + "-literal-" + suffix, "change": change}))
        assert receipt["operation_id"] == prefix + "-literal-" + suffix and receipt["task_id"] is None
        readback = _ok(await _call(client, "ansa_memory_read", {"key": key}))["data"]
        actual = readback["record"]["value"]
        assert readback["found"] and type(actual) is type(value) and actual == value, readback
    return {"tool_count": len(listed.tools), "names": sorted(names), "missing_key": missing_key,
            "literal_values_checked": len(literals)}


async def _second_group(client, prefix):
    key = prefix + "-cas"
    create = {"kind": "preference", "key": key, "value": None, "expected_revision": 0}
    created = _ok(await _call(client, "ansa_memory_write", {
        "operation_id": prefix + "-cas-create", "change": create}))
    assert created["data"]["record"]["revision"] == 1
    update = {"kind": "preference", "key": key, "value": {"enabled": True}, "expected_revision": 1}
    updated = _ok(await _call(client, "ansa_memory_write", {
        "operation_id": prefix + "-cas-update", "change": update}))
    record = updated["data"]["record"]
    assert record["revision"] == 2 and record["value"] == {"enabled": True}
    assert "previous_value" in record and record["previous_value"] is None

    stale = {"kind": "preference", "key": key, "value": "must-not-commit", "expected_revision": 1}
    _error(await _call(client, "ansa_memory_write", {
        "operation_id": prefix + "-cas-stale", "change": stale}), "MEMORY_CONFLICT")
    after = _ok(await _call(client, "ansa_memory_read", {"key": key}))["data"]["record"]
    assert after == record
    return {"key": key, "revision": record["revision"], "stale_cas_rejected": True}


async def _write_preference(client, operation_id, key, value):
    return _ok(await _call(client, "ansa_memory_write", {"operation_id": operation_id,
        "change": {"kind": "preference", "key": key, "value": value, "expected_revision": 0}}))


async def _third_group(client, prefix):
    op = prefix + "-note-idempotent"
    original = {"kind": "note", "text": prefix + " synthetic note 1"}
    first = _ok(await _call(client, "ansa_memory_write", {"operation_id": op, "change": original}))
    replay = _ok(await _call(client, "ansa_memory_write", {
        "operation_id": op, "change": {**original, "tag": ""}}))
    assert replay["data"] == first["data"] and replay["operation_id"] == op
    assert first["task_id"] is None
    _error(await _call(client, "ansa_memory_write", {"operation_id": op,
        "change": {"kind": "note", "text": prefix + " conflicting note"}}), "MEMORY_CONFLICT")

    extra_notes = []
    for index in (2, 3):
        note = _ok(await _call(client, "ansa_memory_write", {
            "operation_id": f"{prefix}-note-{index}",
            "change": {"kind": "note", "text": f"{prefix} synthetic note {index}"}}))
        extra_notes.append(note["data"]["note"])

    base = prefix + "-zzsnapshot-"
    anchor, key_a, key_b, key_c = (base + part for part in ("0", "a", "b", "c"))
    for key in (anchor, key_a, key_b, key_c):
        await _write_preference(client, prefix + "-page-" + key[-1], key, key)
    page = _ok(await _call(client, "ansa_memory_read", {"limit": 2, "after_key": anchor}))["data"]
    assert [row["key"] for row in page["preferences"]] == [key_a, key_b], page
    assert page["has_more_preferences"] is True and page["next_after_key"] == key_b
    first_sequences = [row["sequence"] for row in page["notes"]]
    assert len(first_sequences) == 2 and first_sequences == sorted(first_sequences, reverse=True)
    assert page["has_more_notes"] is True
    note_cursor = page["next_before_note_sequence"]
    assert type(note_cursor) is int and note_cursor == min(first_sequences)

    inserted_key = base + "aa"
    await _write_preference(client, prefix + "-page-insert", inserted_key, "inserted-between-pages")
    inserted_note = _ok(await _call(client, "ansa_memory_write", {
        "operation_id": prefix + "-note-insert", "change": {"kind": "note",
        "text": prefix + " synthetic note inserted between pages"}}))["data"]["note"]

    next_page = _ok(await _call(client, "ansa_memory_read", {
        "limit": 2, "after_key": page["next_after_key"],
        "before_note_sequence": note_cursor}))["data"]
    assert [row["key"] for row in next_page["preferences"]] == [key_c], next_page
    assert [row["sequence"] for row in next_page["notes"]] == [first["data"]["note"]["sequence"]]
    assert inserted_note["sequence"] > max(first_sequences)
    assert next_page["has_more_preferences"] is False and next_page["has_more_notes"] is False

    _error(await _call(client, "ansa_memory_read", {"key": key_a, "after_key": key_b}),
           "MEMORY_REJECTED")
    tail = _ok(await _call(client, "ansa_memory_read", {
        "limit": 2, "after_key": base + "zz", "before_note_sequence": 1}))["data"]
    assert tail["preferences"] == [] and tail["notes"] == []
    assert tail["has_more_preferences"] is False and tail["has_more_notes"] is False
    return {"operation_id": op, "receipt": first, "note_sequences": [
        first["data"]["note"]["sequence"], *(item["sequence"] for item in extra_notes),
        inserted_note["sequence"]], "preference_cursor": page["next_after_key"],
        "note_cursor": note_cursor, "inserted_rows_excluded": True, "empty_tail": True}


async def _fourth_group(client, prefix):
    before = _ok(await _call(client, "ansa_memory_read", {"limit": 100}))["data"]
    valid_change = {"kind": "preference", "key": prefix + "-invalid-target",
                    "value": "must-not-commit", "expected_revision": 0}
    invalid = [
        ("limit-bool", "ansa_memory_read", {"limit": True}, None),
        ("limit-string", "ansa_memory_read", {"limit": "2"}, None),
        ("limit-null", "ansa_memory_read", {"limit": None}, None),
        ("revision-bool", "ansa_memory_write", {"operation_id": prefix + "-bad-bool",
         "change": {**valid_change, "expected_revision": True}}, None),
        ("revision-string", "ansa_memory_write", {"operation_id": prefix + "-bad-string",
         "change": {**valid_change, "expected_revision": "0"}}, None),
        ("missing-value", "ansa_memory_write", {"operation_id": prefix + "-missing-value",
         "change": {k: v for k, v in valid_change.items() if k != "value"}}, None),
        ("change-extra", "ansa_memory_write", {"operation_id": prefix + "-change-extra",
         "change": {**valid_change, "extra": True}}, None),
        ("tool-extra", "ansa_memory_write", {"operation_id": prefix + "-tool-extra",
         "change": valid_change, "extra": True}, None),
        ("invalid-kind", "ansa_memory_write", {"operation_id": prefix + "-invalid-kind",
         "change": {**valid_change, "kind": "other"}}, None),
        ("key-whitespace", "ansa_memory_write", {"operation_id": prefix + "-key-whitespace",
         "change": {**valid_change, "key": " " + prefix + "-invalid"}}, "MEMORY_REJECTED"),
        ("blank-note", "ansa_memory_write", {"operation_id": prefix + "-blank-note",
         "change": {"kind": "note", "text": "   "}}, "MEMORY_REJECTED"),
        ("oversized-value", "ansa_memory_write", {"operation_id": prefix + "-oversized",
         "change": {**valid_change, "value": "x" * 4001}}, "MEMORY_REJECTED"),
    ]
    for _, tool, arguments, code in invalid:
        _error(await _call(client, tool, arguments), code)
    after = _ok(await _call(client, "ansa_memory_read", {"limit": 100}))["data"]
    assert after == before
    missing = _ok(await _call(client, "ansa_memory_read", {"key": valid_change["key"]}))["data"]
    assert missing["found"] is False and missing["record"] is None
    return {"rejected_inputs": [name for name, *_ in invalid], "state_unchanged": True}


async def _fifth_group(runtime_home, isolated_home, prefix, cas_key, note_receipt):
    async with _session(runtime_home) as client:
        replay = _ok(await _call(client, "ansa_memory_write", {
            "operation_id": note_receipt["operation_id"], "change": {
                "kind": "note", "text": prefix + " synthetic note 1", "tag": ""}}))
        assert replay["operation_id"] == note_receipt["operation_id"]
        assert replay["data"] == note_receipt["receipt"]["data"]

        preference = _ok(await _call(client, "ansa_memory_read", {"key": cas_key}))["data"]["record"]
        assert preference["revision"] == 2 and preference["value"] == {"enabled": True}
        snapshot = _ok(await _call(client, "ansa_memory_read", {"limit": 100}))["data"]
        sequences = [row["sequence"] for row in snapshot["notes"]]
        assert sequences == sorted(sequences, reverse=True)
        assert set(note_receipt["note_sequences"]) <= set(sequences)

        history = _ok(await _call(client, "ansa_task_list"))["data"]
        assert history["tasks"] == [], history
        assert replay["task_id"] is None

        async with _session(isolated_home) as isolated:
            absent = _ok(await _call(isolated, "ansa_memory_read", {"key": cas_key}))["data"]
            empty = _ok(await _call(isolated, "ansa_memory_read", {"limit": 100}))["data"]
            assert absent["found"] is False and absent["record"] is None
            assert empty["preferences"] == [] and empty["notes"] == []
            other_key = prefix + "-other-runtime"
            other = _ok(await _call(isolated, "ansa_memory_write", {
                "operation_id": prefix + "-other-runtime-op",
                "change": {"kind": "preference", "key": other_key,
                           "value": "isolated", "expected_revision": 0}}))
            assert other["task_id"] is None
            primary_absent = _ok(await _call(client, "ansa_memory_read", {"key": other_key}))["data"]
            assert primary_absent["found"] is False and primary_absent["record"] is None
    return {"receipt_replayed_after_restart": True, "latest_revision": preference["revision"],
            "note_sequences": sequences, "ansa_task_history_empty": True,
            "second_runtime_isolated": True}


def _private_runtime_home(value):
    home = value.expanduser().resolve()
    user_home = Path.home().resolve()
    if home.drive.upper() != "C:" or not home.is_relative_to(user_home) or home == user_home:
        raise ValueError("--runtime-home 必须位于当前用户的 C 盘私有目录内")
    if home.exists():
        raise ValueError("集成验收要求 --runtime-home 是一个全新的私有目录")
    return home


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-home", type=Path, required=True)
    args = parser.parse_args()

    run_dir = ROOT / "runtime" / "verification" / ("memory-" + str(uuid.uuid4()))
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "report.json"
    report = {"success": False, "scope": "合成数据、真实 MCP stdio 与私有 SQLite；不连接 ANSA",
              "started_at": datetime.now(timezone.utc).isoformat(), "report_path": str(report_path),
              "cases": []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        runtime_home = _private_runtime_home(args.runtime_home)
        isolated_home = runtime_home.with_name(runtime_home.name + "-isolated-" + uuid.uuid4().hex)
        if isolated_home.exists():
            raise ValueError("隔离 runtime-home 意外已存在")
        prefix = "memory-it-" + uuid.uuid4().hex
        async def run():
            async with _session(runtime_home) as client:
                schema = await _first_group(client, prefix)
                _record(report, "schema_no_ansa_empty_and_json_types", schema)
                cas = await _second_group(client, prefix)
                _record(report, "preference_create_update_and_cas", cas)
                notes = await _third_group(client, prefix)
                _record(report, "note_idempotency_and_snapshot_cursors", {
                    key: value for key, value in notes.items() if key != "receipt"})
                _record(report, "invalid_arguments_rejected_without_mutation",
                        await _fourth_group(client, prefix))
            restart = await _fifth_group(runtime_home, isolated_home, prefix, cas["key"], notes)
            _record(report, "restart_task_history_and_runtime_isolation", restart)
        asyncio.run(run())
        report["success"] = True
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
    except BaseException as exc:
        report["success"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["trace"] = traceback.format_exc()
        report["failed_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"success": report["success"], "report": str(report_path),
                      "cases": [case["name"] for case in report["cases"]]}, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
