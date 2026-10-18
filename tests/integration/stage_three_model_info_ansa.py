"""NASTRAN 真机模型信息验收；进程准备和清理复用 API 目录驱动。"""
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa


COUNT_TYPES = {
    "parts": "ANSAPART",
    "materials": "__MATERIALS__",
    "properties": "__PROPERTIES__",
    "faces": "FACE",
    "nodes": "GRID",
    "shells": "SHELL",
    "solids": "SOLID",
    "elements": "__ELEMENTS__",
}
CATEGORY_NAMES = {
    "materials": "__MATERIALS__",
    "properties": "__PROPERTIES__",
    "elements": "__ELEMENTS__",
}
REQUIRED_ALL_TYPES = ("ANSAPART", "FACE", "GRID", "SHELL", "SOLID")


FIXTURE_SCRIPT = """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('model-info fixture requires NASTRAN')

def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None:
        raise RuntimeError('model-info fixture creation failed: ' + kind)
    labels = entity.card_fields(deck)
    if type(labels) not in (list, tuple):
        raise RuntimeError('card_fields did not return labels for ' + kind)
    missing = sorted(set(fields) - set(labels))
    if missing:
        raise RuntimeError('card fields unavailable for ' + kind + ': ' + repr(missing))
    actual = base.GetEntityCardValues(deck, entity, tuple(fields))
    if any(field not in actual or actual[field] != value for field, value in fields.items()):
        raise RuntimeError('model-info fixture card mismatch: ' + kind + ' ' + repr(actual))
    return entity

material = create('MAT1', {
    'DEFINED': 'YES', 'E': 200000.0, 'NU': 0.3, 'RHO': 0.01,
})
prop = create('PSHELL', {
    'MID1': material._id, 'MID2': material._id, 'MID3': material._id,
    'T': 2.0, 'NSM': 0.0,
})
solid_prop = create('PSOLID', {'MID': material._id})
points = ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0),
          (20.0, 10.0, 0.0), (0.0, 10.0, 0.0))
nodes = [
    create('GRID', dict(zip(('X1', 'X2', 'X3'), point)))
    for point in points
]
fields = {'type': 'CQUAD4', 'PID': prop._id}
fields.update({'G' + str(index + 1): node._id for index, node in enumerate(nodes)})
shell = create('SHELL', fields)
RESULT = {
    'material_id': material._id,
    'property_id': prop._id,
    'solid_property_id': solid_prop._id,
    'shell_id': shell._id,
    'node_ids': [node._id for node in nodes],
}
"""


def remote_model_info_oracle(expected_pid, expected_arguments):
    """经已归属 gRPC 主线程独立统计，不调用生产 model_summary。"""
    import math
    import os
    import threading
    from ansa import base, constants, session

    required_all_types = ("ANSAPART", "FACE", "GRID", "SHELL", "SOLID")
    category_names = {
        "materials": "__MATERIALS__",
        "properties": "__PROPERTIES__",
        "elements": "__ELEMENTS__",
    }

    if (os.getpid() != expected_pid
            or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Model-info oracle requires the owned ANSA main thread")

    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Model-info acceptance is scoped to NASTRAN")
    deck_name = session.DeckName(deck)
    database = base.DataBaseName()
    if type(deck_name) is not str or type(database) is not str:
        raise RuntimeError("ANSA returned invalid deck or database metadata")

    def collect(entity_type):
        entities = base.CollectEntities(
            deck, None, entity_type, False,
            filter_visible=False, hidden_entities=True,
        )
        if type(entities) not in (list, tuple):
            raise RuntimeError("CollectEntities returned a non-list for " + entity_type)
        return list(entities)

    all_types = base.TypesInCategory(deck, "__ALL_ENTITIES__")
    if (type(all_types) not in (list, tuple)
            or any(type(name) is not str for name in all_types)):
        raise RuntimeError("TypesInCategory(__ALL_ENTITIES__) returned invalid data")

    required_present = sorted(set(required_all_types).intersection(all_types))
    simple_types = {
        "parts": "ANSAPART",
        "faces": "FACE",
        "nodes": "GRID",
        "shells": "SHELL",
        "solids": "SOLID",
    }
    simple_counts = {
        category: len(collect(entity_type))
        for category, entity_type in simple_types.items()
    }

    category_oracle = {}
    for category in category_names.values():
        names = base.TypesInCategory(deck, category)
        if (type(names) not in (list, tuple)
                or any(type(name) is not str for name in names)):
            raise RuntimeError("TypesInCategory returned invalid data for " + category)
        names = sorted(set(names))
        by_type = {}
        named_entities = set()
        for name in names:
            entities = collect(name)
            by_type[name] = len(entities)
            for entity in entities:
                named_entities.add((entity.ansa_type(deck), entity._id))
        category_oracle[category] = {
            "types": names,
            "by_type": by_type,
            "raw_type_total": sum(by_type.values()),
            "named_total": len(named_entities),
            "keyword_total": len(collect(category)),
        }

    counts = dict(simple_counts)
    counts.update({
        category: category_oracle[name]["named_total"]
        for category, name in category_names.items()
    })
    counts = {
        "parts": counts["parts"],
        "materials": counts["materials"],
        "properties": counts["properties"],
        "faces": counts["faces"],
        "nodes": counts["nodes"],
        "shells": counts["shells"],
        "solids": counts["solids"],
        "elements": counts["elements"],
    }

    coordinate_fields = ("X1", "X2", "X3")
    node_cards = []
    for node in sorted(collect("GRID"), key=lambda entity: entity._id):
        labels = node.card_fields(deck)
        if type(labels) not in (list, tuple) or not set(coordinate_fields).issubset(labels):
            raise RuntimeError("GRID coordinate fields are unavailable")
        values = base.GetEntityCardValues(deck, node, coordinate_fields)
        if type(values) is not dict or any(
                field not in values or type(values[field]) not in (int, float)
                or not math.isfinite(values[field]) for field in coordinate_fields):
            raise RuntimeError("GRID coordinate readback is incomplete")
        node_cards.append({
            "id": node._id,
            "fields": {field: values[field] for field in coordinate_fields},
        })

    return {
        "pid": os.getpid(),
        "main_thread": True,
        "version": getattr(constants, "app_version", None),
        "deck": deck,
        "deck_name": deck_name,
        "alternate_deck": constants.ABAQUS,
        "database": database,
        "entity_count": len(collect("__ALL_ENTITIES__")),
        "all_entity_type_count": len(all_types),
        "required_types_present": required_present,
        "simple_type_counts": simple_counts,
        "category_oracle": category_oracle,
        "entity_counts": counts,
        "node_cards": node_cards,
    }


def _oracle_counts(oracle):
    if oracle.get("main_thread") is not True:
        raise AssertionError("独立 oracle 未确认 ANSA 主线程")
    if oracle.get("deck_name") != "NASTRAN":
        raise AssertionError(f"独立 oracle deck_name 不符：{oracle.get('deck_name')!r}")
    if oracle.get("required_types_present") != sorted(REQUIRED_ALL_TYPES):
        raise AssertionError(f"TypesInCategory 全表缺少验收类型：{oracle.get('required_types_present')}")
    for category in CATEGORY_NAMES.values():
        item = oracle["category_oracle"][category]
        if item["named_total"] != item["keyword_total"]:
            raise AssertionError(f"{category} 类型清单与直接类别查询不符：{item}")
    return oracle["entity_counts"]


def _assert_summary_context(completed, harness, snapshot):
    expected_target = {name: harness.target[name] for name in
                       ("backend_id", "instance_id", "session_id", "generation")}
    if completed.get("target") != expected_target or completed.get("model") != harness.model:
        raise AssertionError("summary 任务没有使用当前 expected_target/model")
    result = completed.get("data", {}).get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"model summary 缺少结果：{completed}")
    if result.get("model_after") != snapshot.get("model"):
        raise AssertionError("summary 的 model_after 与配对 snapshot 不一致")
    return result


def _assert_summary(summary, oracle, snapshot):
    expected_counts = _oracle_counts(oracle)
    expected = {
        "deck": oracle["deck"],
        "deck_name": oracle["deck_name"],
        "database": oracle["database"],
        "entity_count": oracle["entity_count"],
        "node_type": "GRID",
        "entity_counts": expected_counts,
        "count_types": COUNT_TYPES,
        "counts_complete": True,
        "count_errors": {},
    }
    for field, value in expected.items():
        if summary.get(field) != value:
            raise AssertionError(
                f"model summary {field} 与独立回读不符："
                f"expected={value!r}, actual={summary.get(field)!r}"
            )
    if summary["model_after"] != snapshot["model"]:
        raise AssertionError("summary 的 model_after 与配对 snapshot 不一致")
    if any(type(value) is not int or value < 0 for value in expected_counts.values()):
        raise AssertionError(f"独立计数无效：{expected_counts}")
    return expected_counts


async def _summary(harness, oracle, snapshot, *, fields=None, operation_id=None):
    _, completed = await harness.task(
        "ansa_model_summary",
        fields if fields is not None else {},
        operation_id=operation_id,
    )
    result = _assert_summary_context(completed, harness, snapshot)
    _assert_summary(result, oracle, snapshot)
    return completed, result


def _record(report, path, name, evidence):
    report["cases"].append({"name": name, "ok": True, "evidence": evidence})
    common._json_write(path, report)
    print(json.dumps({"case": name, "ok": True}), flush=True)


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers
    port = identity["port"]

    def oracle():
        return two.rpc(
            port, remote_model_info_oracle, identity["pid"], identity["program_args"],
        )

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={
            **os.environ,
            "PYTHONPATH": str(common.PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    operation_id = None
    first_summary = None
    first_task_id = None
    first_oracle = None
    prior_backend_id = None

    with (run_dir / "model-info-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                harness = common.McpHarness(client, report, run_dir)
                report["schema"] = await harness.list_schema()
                baseline_snapshot = await common._bind_for_run(
                    harness, identity, one, two, port,
                )
                baseline_oracle = oracle()
                if (baseline_oracle["pid"] != identity["pid"]
                        or baseline_oracle["database"] == ""):
                    raise AssertionError("基线 oracle PID 或数据库路径不符")
                if Path(baseline_oracle["database"]).resolve() != Path(identity["seed_path"]).resolve():
                    raise AssertionError("基线数据库路径与本轮 seed 不符")
                if (baseline_oracle["entity_counts"]["nodes"] != 1
                        or [row["id"] for row in baseline_oracle["node_cards"]]
                        != [identity["seed_entity_id"]]):
                    raise AssertionError("公共准备没有留下唯一 seed GRID")
                baseline_task, baseline_summary = await _summary(
                    harness, baseline_oracle, baseline_snapshot,
                )
                _record(report, path, "baseline_snapshot_and_summary_match_independent_grpc", {
                    "schema": report["schema"],
                    "snapshot_model": baseline_snapshot["model"],
                    "independent": baseline_oracle,
                    "summary_task_id": baseline_task["task_id"],
                    "summary": baseline_summary,
                })

                old_target = dict(harness.target)
                old_model = dict(harness.model)
                new_backup = run_dir / "model-info-new-backup.ansa"
                if new_backup.exists():
                    raise RuntimeError("拒绝覆盖已有 New 备份")
                _, new_task = await harness.task(
                    "ansa_model_new", {"backup_path": str(new_backup)},
                )
                if not new_backup.is_file() or new_backup.stat().st_size == 0:
                    raise AssertionError("New 未生成有效备份")
                empty_snapshot = await harness.snapshot_context()
                empty_oracle = oracle()
                if (type(empty_oracle["entity_count"]) is not int
                        or empty_oracle["entity_count"] < 0
                        or any(count for category, count in empty_oracle["entity_counts"].items()
                               if category != "parts")
                        or empty_oracle["node_cards"]):
                    raise AssertionError(f"默认 New 后模型仍有几何/求解实体：{empty_oracle}")
                new_result = new_task.get("data", {}).get("result", {})
                if new_result.get("model_after") != empty_snapshot["model"]:
                    raise AssertionError("New 的 model_after 与新模型 snapshot 不一致")
                empty_task, empty_summary = await _summary(
                    harness, empty_oracle, empty_snapshot,
                )
                stale_after_new = await harness.call("ansa_model_summary", {
                    "operation_id": "model-info-stale-new-" + uuid.uuid4().hex,
                    "expected_target": old_target,
                    "expected_model": old_model,
                }, allow_error=True)
                if stale_after_new.get("ok") is not False or stale_after_new.get("task_id") is not None:
                    raise AssertionError(f"New 后旧 model context 未被拒绝：{stale_after_new}")
                _record(report, path, "default_new_backup_empty_geometry_and_stale_context_rejection", {
                    "backup": str(new_backup),
                    "new_task_id": new_task["task_id"],
                    "snapshot_model": empty_snapshot["model"],
                    "independent": empty_oracle,
                    "summary_task_id": empty_task["task_id"],
                    "summary": empty_summary,
                    "old_context_rejected": True,
                })

                fixture = await harness.direct_result(
                    "ansa_script_execute", {"script": FIXTURE_SCRIPT},
                )
                fixture_snapshot = await harness.snapshot_context()
                fixture_oracle = oracle()
                fixture_counts = _oracle_counts(fixture_oracle)
                for category, expected in (
                        ("materials", 1), ("properties", 2), ("nodes", 4),
                        ("shells", 1), ("elements", 1)):
                    if fixture_counts[category] != expected:
                        raise AssertionError(
                            f"fixture {category} 计数不符：{fixture_counts}"
                        )
                property_types = fixture_oracle["category_oracle"]["__PROPERTIES__"]["by_type"]
                if property_types.get("PSHELL") != 1 or property_types.get("PSOLID") != 1:
                    raise AssertionError(f"PSHELL/PSOLID 分类独立回读不符：{property_types}")
                coordinates = sorted(
                    tuple(card["fields"][field] for field in ("X1", "X2", "X3"))
                    for card in fixture_oracle["node_cards"]
                )
                if coordinates != sorted([
                        (0.0, 0.0, 0.0), (20.0, 0.0, 0.0),
                        (20.0, 10.0, 0.0), (0.0, 10.0, 0.0)]):
                    raise AssertionError(f"GRID card_fields/GetEntityCardValues 回读不符：{coordinates}")
                fixture_task, fixture_summary = await _summary(
                    harness, fixture_oracle, fixture_snapshot,
                )
                _record(report, path, "minimal_nastran_fixture_category_and_named_type_counts", {
                    "fixture_result": fixture,
                    "independent": fixture_oracle,
                    "summary_task_id": fixture_task["task_id"],
                    "summary": fixture_summary,
                })

                saved_path = run_dir / "model-info-same-path.ansa"
                first_open_backup = run_dir / "model-info-first-open-backup.ansa"
                reopen_backup = run_dir / "model-info-reopen-backup.ansa"
                if saved_path.exists() or first_open_backup.exists() or reopen_backup.exists():
                    raise RuntimeError("拒绝覆盖已有保存文件或 Open 备份")
                await harness.direct_result("ansa_model_save", {"path": str(saved_path)})
                if not saved_path.is_file() or saved_path.stat().st_size == 0:
                    raise AssertionError("模型保存没有生成有效文件")
                saved_hash = hashlib.sha256(saved_path.read_bytes()).hexdigest()
                copy_snapshot = await harness.snapshot_context()
                if copy_snapshot["model"] != fixture_snapshot["model"] or oracle() != fixture_oracle:
                    raise AssertionError("保存副本改变了当前模型路径或数据")
                _, first_open = await harness.task("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(first_open_backup),
                })
                if not first_open_backup.is_file() or first_open_backup.stat().st_size == 0:
                    raise AssertionError("首次打开保存副本没有生成当前模型备份")
                saved_snapshot = await harness.snapshot_context()
                if Path(saved_snapshot["model"]["database"]).resolve() != saved_path.resolve():
                    raise AssertionError("Open 保存副本后当前数据库路径不符")
                saved_oracle = oracle()
                if {k: v for k, v in saved_oracle.items() if k != "database"} != {
                        k: v for k, v in fixture_oracle.items() if k != "database"}:
                    raise AssertionError("打开保存副本后实体或卡片发生变化")
                saved_task, saved_summary = await _summary(
                    harness, saved_oracle, saved_snapshot,
                )
                reopen_target = dict(harness.target)
                reopen_model = dict(harness.model)
                _, open_task = await harness.task("ansa_model_open", {
                    "path": str(saved_path),
                    "backup_path": str(reopen_backup),
                })
                if not reopen_backup.is_file() or reopen_backup.stat().st_size == 0:
                    raise AssertionError("同路径 Open 未生成当前模型备份")
                reopened_snapshot = await harness.snapshot_context()
                if reopened_snapshot["model"]["epoch"] == reopen_model["epoch"]:
                    raise AssertionError("同路径 Open 未更新 model epoch")
                if Path(reopened_snapshot["model"]["database"]).resolve() != saved_path.resolve():
                    raise AssertionError("同路径 Open 后数据库路径不符")
                open_result = open_task.get("data", {}).get("result", {})
                if open_result.get("model_after") != reopened_snapshot["model"]:
                    raise AssertionError("Open 的 model_after 与重开后的 snapshot 不一致")
                reopened_oracle = oracle()
                reopened_task, reopened_summary = await _summary(
                    harness, reopened_oracle, reopened_snapshot,
                )
                if reopened_oracle != saved_oracle:
                    raise AssertionError("同路径 Open 前后独立实体与 card 读回发生变化")
                if hashlib.sha256(saved_path.read_bytes()).hexdigest() != saved_hash:
                    raise AssertionError("同路径 Open 改写了保存文件")
                stale_after_open = await harness.call("ansa_model_summary", {
                    "operation_id": "model-info-stale-open-" + uuid.uuid4().hex,
                    "expected_target": reopen_target,
                    "expected_model": reopen_model,
                }, allow_error=True)
                if stale_after_open.get("ok") is not False or stale_after_open.get("task_id") is not None:
                    raise AssertionError(f"同路径 Open 后旧 model context 未被拒绝：{stale_after_open}")
                _record(report, path, "save_same_path_reopen_and_context_epoch_validation", {
                    "saved_sha256": saved_hash,
                    "copy_preserved_current_path": True,
                    "first_open_task_id": first_open["task_id"],
                    "save_summary_task_id": saved_task["task_id"],
                    "save_summary": saved_summary,
                    "open_task_id": open_task["task_id"],
                    "reopen_backup": str(reopen_backup),
                    "snapshot_model": reopened_snapshot["model"],
                    "independent": reopened_oracle,
                    "summary_task_id": reopened_task["task_id"],
                    "summary": reopened_summary,
                    "old_context_rejected": True,
                })

                strict_snapshot = await harness.snapshot_context()
                strict_oracle = oracle()
                null_task, null_summary = await _summary(
                    harness, strict_oracle, strict_snapshot, fields={"deck": None},
                )
                rejected = []
                base_arguments = {
                    "expected_target": harness.target,
                    "expected_model": harness.model,
                }
                invalid_arguments = (
                    ("raw_null_string", {"deck": "null"}),
                    ("bool", {"deck": True}),
                    ("string_integer", {"deck": "1"}),
                    ("extra_argument", {"unexpected_model_info": True}),
                )
                for label, extra in invalid_arguments:
                    response = await client.call_tool("ansa_model_summary", {
                        "operation_id": "model-info-schema-" + uuid.uuid4().hex,
                        **base_arguments,
                        **extra,
                    })
                    if not response.isError:
                        raise AssertionError(f"严格 schema 未拒绝 {label}")
                    rejected.append(label)
                wrong_deck = await harness.task(
                    "ansa_model_summary",
                    {"deck": strict_oracle["alternate_deck"]},
                    expected="failed",
                )
                after_rejection_snapshot = await harness.snapshot_context()
                after_rejection_oracle = oracle()
                if (after_rejection_snapshot["model"] != strict_snapshot["model"]
                        or after_rejection_oracle != strict_oracle):
                    raise AssertionError("strict deck/schema 拒绝后模型发生变化")
                if wrong_deck[1].get("status") != "failed":
                    raise AssertionError(f"错误 deck 未以失败任务返回：{wrong_deck[1]}")
                _record(report, path, "strict_deck_null_and_wrong_deck_rejections_without_changes", {
                    "true_null_task_id": null_task["task_id"],
                    "true_null_summary": null_summary,
                    "schema_rejections": rejected,
                    "wrong_deck_task_id": wrong_deck[1]["task_id"],
                    "wrong_deck_error": wrong_deck[1].get("error"),
                    "unchanged_model": True,
                })

                final_snapshot = await harness.snapshot_context()
                final_oracle = oracle()
                operation_id = "model-info-summary-" + uuid.uuid4().hex
                first_task, first = await harness.task(
                    "ansa_model_summary", {}, operation_id=operation_id,
                )
                first_summary = _assert_summary_context(first, harness, final_snapshot)
                _assert_summary(first_summary, final_oracle, final_snapshot)
                _, repeated = await harness.task(
                    "ansa_model_summary", {}, operation_id=operation_id,
                )
                if (repeated["task_id"] != first["task_id"]
                        or repeated["data"]["result"] != first["data"]["result"]):
                    raise AssertionError("同 operation_id 未复用原 summary 任务")
                first_task_id = first_task["task_id"]
                first_oracle = oracle()
                if first_oracle != final_oracle:
                    raise AssertionError("幂等 summary 前后独立模型读回发生变化")
                prior_backend_id = harness.target["backend_id"]
                prior_summary_result = first["data"]["result"]

    with (run_dir / "model-info-backend-restarted.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                restarted = common.McpHarness(client, report, run_dir)
                status = await restarted.call("ansa_connection_status")
                backend_id = status["data"]["backend_id"]
                if backend_id == prior_backend_id:
                    raise AssertionError("stdio backend 没有重启")
                queried = await restarted.call(
                    "ansa_task_query", {"operation_id": operation_id},
                )
                if (queried.get("status") != "succeeded"
                        or queried.get("task_id") != first_task_id
                        or queried.get("data", {}).get("result") != prior_summary_result):
                    raise AssertionError(f"重启后未查回原 summary 任务：{queried}")
                after_restart_oracle = oracle()
                if after_restart_oracle != first_oracle:
                    raise AssertionError("后端重启/任务查询后独立模型读回发生变化")
                _record(report, path, "summary_idempotency_and_original_task_after_backend_restart", {
                    "operation_id": operation_id,
                    "task_id": first_task_id,
                    "repeated_task_id": queried["task_id"],
                    "prior_backend_id": prior_backend_id,
                    "restarted_backend_id": backend_id,
                    "summary_result": prior_summary_result,
                    "independent_after_restart": after_restart_oracle,
                })

    report["success"] = True


if __name__ == "__main__":
    raise SystemExit(stage_three_api_catalog_ansa.main(
        report_name="model-info-report.json",
        scope="ANSA 2026.1 NASTRAN model summary 与八类实体计数",
        verify_callback=verify,
    ))
