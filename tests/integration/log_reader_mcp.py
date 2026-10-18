"""用真实 stdio、Service 与 SQLite 验证日志附件；不启动或连接 ANSA。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import traceback
import uuid

from ansa_mcp_plugin.backend.security import ensure_safe_parent, verify_private_path
from ansa_mcp_plugin.backend.tasks import TaskStore
from memory_mcp import ROOT, _call, _error, _ok, _private_runtime_home, _record, _session
from stage_three_ansa import EXPECTED_TOOLS


REQUEST_SECRET = "REQUEST_SECRET_LOG_READER_FIXTURE"
OLDER_RESULT_SECRET = "OLDER_RESULT_MUST_NOT_REPLACE_LATEST"
METADATA_FIELDS = {
    "task_id", "operation_id", "seq", "sequence", "tool", "state", "created", "updated",
    "target", "model",
}
TARGET_FIELDS = {"backend_id", "instance_id", "session_id", "generation"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_database_paths(home):
    ensure_safe_parent(home)
    verify_private_path(home)
    for name in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
        path = home / name
        if path.exists():
            ensure_safe_parent(path)
            verify_private_path(path)
    assert (home / "state.sqlite3").is_file(), "验收运行目录缺少真实 SQLite 数据库"


@contextmanager
def _database(home):
    _check_database_paths(home)
    connection = sqlite3.connect((home / "state.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


@contextmanager
def _task_store(home):
    _check_database_paths(home)
    store = TaskStore(home / "state.sqlite3")
    try:
        yield store
    finally:
        store.close()


def _snapshot(home):
    """只把合成夹具表的指纹写入证据，绝不把请求正文写入报告。"""
    snapshot = {}
    with _database(home) as connection:
        for table, order in (("tasks", "seq"), ("operation_keys", "operation_id")):
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            snapshot[table + "_count"] = len(rows)
            snapshot[table + "_sha256"] = hashlib.sha256(_canonical(rows).encode("utf-8")).hexdigest()
    return snapshot


def _rows(home):
    with _database(home) as connection:
        rows = connection.execute(
            "SELECT seq,task_id,operation_id,state,dispatch,task_json FROM tasks ORDER BY seq DESC"
        ).fetchall()
    return [{"sequence": seq, "task_id": task_id, "operation_id": operation_id,
             "state": state, "dispatch": dispatch, "tool": json.loads(task_json)["tool"]}
            for seq, task_id, operation_id, state, dispatch, task_json in rows]


def _latest_body(home):
    with _database(home) as connection:
        row = connection.execute(
            "SELECT result_json,error_json FROM tasks ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return tuple(None if value is None else json.loads(value) for value in row)


def _seed_task(store, home, operation_id, *, tool="script_execute", state="succeeded",
               result=None, error=None, backend_id="offline-log-reader-fixture"):
    request = {
        "tool": tool,
        "params": {"script": REQUEST_SECRET, "path": str(home / "request-selected.json"),
                   "filename": "request-selected.json"},
        "target": {"backend_id": backend_id, "instance_id": "offline-instance",
                   "session_id": "offline-session", "generation": 1, "private_extra": REQUEST_SECRET},
        "model": {"epoch": 7, "private_extra": REQUEST_SECRET},
        "ttl_seconds": None,
    }
    identity = {"pid": 1234, "process_start": "offline-fixture-only",
                "instance_id": "offline-instance", "session_id": "offline-session",
                "private_extra": REQUEST_SECRET}
    record = store.create(operation_id, request, identity, None)
    if state != "queued":
        record = store.merge({"task_id": record["task_id"], "state": state,
                              "result": result, "error": error})
    assert record["state"] == state, "合成任务未持久化为预期状态"
    return record["task_id"]


def _seed_history(home, prefix):
    result = {"result": {"model_path": str(home / "synthetic-model.ansa"), "value": 42},
              "stdout": "真实 SQLite 持久 stdout", "stdout_truncated": True}
    error = "offline persisted failure"
    with _task_store(home) as store:
        for index in range(201):
            latest = index == 200
            state = "failed" if index % 3 == 2 else "succeeded"
            task_id = _seed_task(
                store, home, f"{prefix}-history-{index}",
                tool="script_execute" if index % 2 == 0 else "grid_read", state=state,
                result=result if latest else {"result": OLDER_RESULT_SECRET},
                error=error if latest else ("older failure" if state == "failed" else None),
            )
    assert _latest_body(home) == (result, error)
    with _database(home) as connection:
        count = connection.execute(
            "SELECT count(*) FROM tasks WHERE instr(request_json,?)>0", (REQUEST_SECRET,)
        ).fetchone()[0]
    assert count == 201, "请求隐私夹具必须确实存在于数据库内"
    return {"task_id": task_id, "result": result, "error": error}


def _seed_large(home, prefix):
    result = {"result": {"payload": "X" * 70000 + "RESULT_BODY_SECRET"},
              "stdout": "large persisted stdout", "stdout_truncated": False}
    error = "E" * 96 + "ERROR_BODY_SECRET"
    with _task_store(home) as store:
        task_id = _seed_task(store, home, prefix + "-large", state="failed", result=result, error=error)
    assert _latest_body(home) == (result, error)
    return {"task_id": task_id, "result": result, "error": error}


async def _unbound(client, home):
    # status 会探测已登记实例；先确认独立运行目录从未登记任何实例。
    assert not list((home / "instances").glob("*/connection.json")), "离线夹具意外出现 ANSA 登记"
    status = _ok(await _call(client, "ansa_connection_status"))["data"]
    assert status["active_target"] is None and status["active_model"] is None
    assert status["instances"] == [] and status["recovery_required"] is False
    assert status["dispatch_blocked"] is False
    assert type(status["backend_id"]) is str and status["backend_id"]
    return status


async def _read_log(client, *, max_chars=65536):
    response = await _call(client, "ansa_log_read", {"max_chars": max_chars})
    value = _ok(response)
    assert value["status"] == "succeeded"
    assert value["task_id"] is None and value["operation_id"] is None
    assert value["target"] is None and value["model"] is None
    data = value["data"]
    assert data["audit_scope"] == "task_history"
    assert value["artifacts"] == ([data["artifact"]] if data["artifact"] else [])
    serialized = _canonical(value)
    forbidden = [REQUEST_SECRET]
    if data["truncated"]:
        assert data["body_inline"] is False and data["result"] is None and data["error"] is None
        forbidden.extend(("RESULT_BODY_SECRET", "ERROR_BODY_SECRET"))
    assert all(marker not in serialized for marker in forbidden), "MCP 返回泄漏了未允许的正文"
    for item in response.content:
        if item.type == "text":
            assert all(marker not in item.text for marker in forbidden), "MCP 文本返回泄漏了未允许的正文"
    return data


def _attachment(home, data):
    artifact = data["artifact"]
    assert type(artifact) is dict
    digest = artifact["sha256"]
    assert type(digest) is str and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    path = Path(artifact["path"])
    assert path.is_absolute() and path == home / "logs" / (digest + ".json")
    assert artifact["mime_type"] == "application/json"
    ensure_safe_parent(path)
    verify_private_path(path.parent)
    verify_private_path(path)
    info = path.stat(follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
    raw = path.read_bytes()
    ensure_safe_parent(path)
    verify_private_path(path)
    assert hashlib.sha256(raw).hexdigest() == digest
    assert type(artifact["bytes"]) is int and artifact["bytes"] == len(raw)
    text = raw.decode("utf-8")
    document = json.loads(text)
    assert text == _canonical(document), "附件不是规范 UTF-8 JSON"
    assert REQUEST_SECRET not in text, "附件泄漏了合成任务请求"
    assert OLDER_RESULT_SECRET not in text, "附件包含了更旧任务的结果正文"
    assert not (home / "request-selected.json").exists(), "任务参数改变了附件输出位置"
    return path, raw, document, info


def _check_history(data, document, rows):
    expected = rows[:200]
    history = document["history"]
    assert data["history"] == history
    assert document["audit_scope"] == data["audit_scope"] == "task_history"
    assert document["has_more"] is data["has_more"] is (len(rows) > 200)
    assert history["task_count"] == len(history["tasks"]) == len(expected)
    assert [item["task_id"] for item in history["tasks"]] == [item["task_id"] for item in expected]
    assert history["state_counts"] == dict(Counter(item["state"] for item in expected))
    assert history["tool_counts"] == dict(Counter(item["tool"] for item in expected))
    assert document["latest"] == data["latest"] == history["tasks"][0]
    for actual, stored in zip(history["tasks"], expected):
        assert set(actual) <= METADATA_FIELDS
        assert {"task_id", "operation_id", "state", "tool"} <= set(actual)
        for key in ("task_id", "operation_id", "state", "tool"):
            assert actual[key] == stored[key]
        if "target" in actual:
            assert set(actual["target"]) <= TARGET_FIELDS
        if "model" in actual:
            assert set(actual["model"]) <= {"epoch"}


def _check_preview(data, result, error, max_chars):
    body = _canonical({"result": result, "error": error})
    truncated = len(body) > max_chars
    assert data["preview_scope"] == "latest_result_and_error"
    assert data["total_chars"] == len(body)
    assert data["content_excerpt"] == body[:max_chars]
    assert len(data["content_excerpt"]) <= max_chars
    assert data["truncated"] is truncated and data["body_inline"] is (not truncated)
    assert data["result"] == (None if truncated else result)
    assert data["error"] == (None if truncated else error)
    assert data["available"] is (result is not None or error is not None)


async def _first_group(client, home):
    status = await _unbound(client, home)
    listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    assert len(listed.tools) == len(names) == 58
    assert names == set(EXPECTED_TOOLS) | {"ansa_log_read"}
    schema = next(tool.inputSchema for tool in listed.tools if tool.name == "ansa_log_read")
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"max_chars"}
    maximum = schema["properties"]["max_chars"]
    assert maximum["type"] == "integer" and maximum["minimum"] == 1 and maximum["maximum"] == 65536
    assert maximum["default"] == 65536
    before = _snapshot(home)
    assert before["tasks_count"] == before["operation_keys_count"] == 0
    empty = _ok(await _call(client, "ansa_log_read"))
    assert empty["artifacts"] == [] and empty["task_id"] is None
    data = empty["data"]
    assert data["found"] is False and data["available"] is False and data["artifact"] is None
    assert data["audit_scope"] == "task_history" and data["has_more"] is False
    assert data["history"]["tasks"] == [] and data["history"]["task_count"] == 0
    assert data["total_chars"] == 0 and data["content_excerpt"] == ""
    assert not list((home / "logs").glob("*.json"))

    extras = [
        {"path": str(home / "must-not-create.json")}, {"filename": "must-not-create.json"},
        {"runtime_home": str(home)}, {"expected_target": {"backend_id": status["backend_id"]}},
        {"expected_model": {"epoch": 1}}, {"operation_id": "must-not-create-a-task"},
    ]
    for arguments in extras:
        _error(await _call(client, "ansa_log_read", {"max_chars": 32, **arguments}))
    assert _snapshot(home) == before
    assert not (home / "must-not-create.json").exists()
    assert not list((home / "logs").glob("*.json"))
    return {"tool_count": 58, "strict_extra_rejections": len(extras), "empty_task_count": 0,
            "no_registered_instances": True, "no_active_target": True}


async def _second_group(client, home, fixture):
    await _unbound(client, home)
    before = _snapshot(home)
    data = await _read_log(client)
    path, raw, document, _info = _attachment(home, data)
    assert data["found"] is True and data["available"] is True
    assert data["latest"]["task_id"] == fixture["task_id"] and data["latest"]["state"] == "failed"
    assert document["result"] == fixture["result"] and document["error"] == fixture["error"]
    assert document["result"]["stdout"] == fixture["result"]["stdout"]
    assert document["result"]["stdout_truncated"] is True
    _check_preview(data, fixture["result"], fixture["error"], 65536)
    _check_history(data, document, _rows(home))
    assert data["has_more"] is True and data["history"]["task_count"] == 200
    assert _snapshot(home) == before
    await _unbound(client, home)
    return {"seeded_tasks": before["tasks_count"], "projected_tasks": 200, "has_more": True,
            "state_counts": data["history"]["state_counts"], "tool_counts": data["history"]["tool_counts"],
            "artifact": data["artifact"], "body_chars": data["total_chars"],
            "artifact_chars": len(raw.decode("utf-8")), "private_path": str(path)}


async def _third_group(client, home, fixture):
    status = await _unbound(client, home)
    before = _snapshot(home)
    data = await _read_log(client, max_chars=32)
    path, raw, document, info = _attachment(home, data)
    _check_history(data, document, _rows(home))
    _check_preview(data, fixture["result"], fixture["error"], 32)
    assert document["result"] == fixture["result"] and document["error"] == fixture["error"]
    assert data["truncated"] is True and data["body_inline"] is False
    for marker in ("RESULT_BODY_SECRET", "ERROR_BODY_SECRET"):
        assert marker not in _canonical(data), "完整正文绕过了 inline 字符上限"
    await asyncio.sleep(0.05)
    repeated = await _read_log(client, max_chars=32)
    repeated_path, repeated_raw, repeated_document, repeated_info = _attachment(home, repeated)
    assert repeated == data and repeated_path == path and repeated_raw == raw and repeated_document == document
    assert repeated_info.st_mtime_ns == info.st_mtime_ns and repeated_info.st_ino == info.st_ino
    assert _snapshot(home) == before
    baseline = {"data": data, "raw": raw, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino,
                "backend_id": status["backend_id"], "database": before}
    evidence = {"max_chars": 32, "body_chars": data["total_chars"], "body_inline": False,
                "artifact": data["artifact"], "reused_without_rewrite": True,
                "mtime_ns": info.st_mtime_ns}
    return baseline, evidence


async def _restart_check(client, home, baseline):
    status = await _unbound(client, home)
    assert status["backend_id"] != baseline["backend_id"], "stdio 子进程没有真正重启"
    data = await _read_log(client, max_chars=32)
    _path, raw, _document, info = _attachment(home, data)
    assert data == baseline["data"] and raw == baseline["raw"]
    assert info.st_mtime_ns == baseline["mtime_ns"] and info.st_ino == baseline["inode"]
    assert _snapshot(home) == baseline["database"]
    return {"backend_id_changed": True, "same_artifact_and_data": True,
            "artifact": data["artifact"], "mtime_ns": info.st_mtime_ns}


async def _fourth_group(client, home, prefix):
    status = await _unbound(client, home)
    task_ids = {}
    latest_data = None
    for state in ("queued", "running"):
        # 未绑定仍会过期旧 backend_id 的待处理任务，因此使用当前真实后端 ID；
        # ttl=None、dispatch=not_sent，且从不写实例登记或绑定记录。
        with _task_store(home) as store:
            task_ids[state] = _seed_task(store, home, prefix + "-" + state,
                                         state=state, backend_id=status["backend_id"])
        await asyncio.sleep(0.6)
        rows = _rows(home)
        assert rows[0]["task_id"] == task_ids[state]
        assert rows[0]["state"] == state and rows[0]["dispatch"] == "not_sent"
        before = _snapshot(home)
        latest_data = await _read_log(client)
        _path, _raw, document, _info = _attachment(home, latest_data)
        assert latest_data["found"] is True and latest_data["available"] is False
        assert latest_data["latest"]["task_id"] == task_ids[state]
        assert latest_data["latest"]["state"] == state
        assert document["result"] is None and document["error"] is None
        _check_preview(latest_data, None, None, 65536)
        _check_history(latest_data, document, rows)
        assert "RESULT_BODY_SECRET" not in _canonical(latest_data)
        assert "ERROR_BODY_SECRET" not in _canonical(latest_data)
        assert _snapshot(home) == before
        await _unbound(client, home)

    invalid = (True, False, 0, -1, 65537, 32.0, "32", "", "null", None, [], {})
    before = _snapshot(home)
    files = {path.name for path in (home / "logs").glob("*.json")}
    for value in invalid:
        _error(await _call(client, "ansa_log_read", {"max_chars": value}))
    await asyncio.sleep(0.6)
    assert _snapshot(home) == before
    assert {path.name for path in (home / "logs").glob("*.json")} == files
    assert all(row["dispatch"] == "not_sent" for row in _rows(home))
    await _unbound(client, home)
    return latest_data, {"task_ids": task_ids, "latest_states_preserved": True,
                         "dispatch": "not_sent", "invalid_value_rejections": len(invalid),
                         "tasks_not_created_by_reads": True}


async def _tamper_check(client, home, data):
    path, _raw, _document, _info = _attachment(home, data)
    before = _snapshot(home)
    files = {item.name for item in (home / "logs").glob("*.json")}
    tampered = b'{"fixture":"tampered-content-must-remain"}\n'
    path.write_bytes(tampered)
    changed = path.stat()
    value = _error(await _call(client, "ansa_log_read"), "LOG_UNAVAILABLE")
    assert value["data"] is None and value["artifacts"] == []
    assert path.read_bytes() == tampered, "读取日志覆盖了既有不同内容"
    assert path.stat().st_mtime_ns == changed.st_mtime_ns
    assert {item.name for item in (home / "logs").glob("*.json")} == files
    assert _snapshot(home) == before
    await _unbound(client, home)
    return {"error_code": "LOG_UNAVAILABLE", "tampered_path": str(path),
            "original_sha256": data["artifact"]["sha256"],
            "tampered_sha256": hashlib.sha256(tampered).hexdigest(), "not_overwritten": True}


async def _run(home, report):
    prefix = "log-reader-it-" + uuid.uuid4().hex
    report["active_case"] = "empty_schema_strict_extras_and_no_ansa"
    async with _session(home) as client:
        _record(report, report["active_case"], await _first_group(client, home))

    report["active_case"] = "persisted_sqlite_body_private_attachment_and_history_200"
    fixture = _seed_history(home, prefix)
    async with _session(home) as client:
        _record(report, report["active_case"], await _second_group(client, home, fixture))

    report["active_case"] = "bounded_body_full_attachment_and_idempotent_reuse"
    large = _seed_large(home, prefix)
    async with _session(home) as client:
        baseline, evidence = await _third_group(client, home, large)
        _record(report, report["active_case"], evidence)

    report["active_case"] = "stdio_restart_same_artifact_and_tamper_refusal"
    async with _session(home) as client:
        restart = await _restart_check(client, home, baseline)
        report["active_case"] = "latest_pending_no_fallback_and_invalid_arguments_no_tasks"
        latest, evidence = await _fourth_group(client, home, prefix)
        _record(report, report["active_case"], evidence)
        report["active_case"] = "stdio_restart_same_artifact_and_tamper_refusal"
        tamper = await _tamper_check(client, home, latest)
        _record(report, report["active_case"], {"restart": restart, "tamper": tamper})
    assert len(report["cases"]) == 5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-home", type=Path, required=True,
                        help="当前用户 C 盘内尚不存在的私有运行目录")
    args = parser.parse_args()
    run = ROOT / "runtime" / "verification" / ("log-reader-" + str(uuid.uuid4()))
    run.mkdir(parents=True, exist_ok=False)
    report_path = run / "report.json"
    report = {"success": False, "scope": "合成数据、真实 stdio/Service/TaskLogReader 与 SQLite；无 ANSA 调用",
              "started_at": datetime.now(timezone.utc).isoformat(), "report_path": str(report_path),
              "cases": []}
    try:
        # 在共用参数辅助函数 resolve 前，先拒绝原始路径链中的 reparse。
        ensure_safe_parent(args.runtime_home.expanduser().absolute())
        home = _private_runtime_home(args.runtime_home)
        report["runtime_home"] = str(home)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        asyncio.run(_run(home, report))
        report["success"] = True
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
    except BaseException as exc:
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
