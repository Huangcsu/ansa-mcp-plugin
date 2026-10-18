"""真实 ANSA 第三阶段验收。

脚本先生成专属运行目录和启动说明，不会启动或停止 ANSA。主 Agent 使用
launch_instructions.json 启动独立 GUI 后，再带 --run-dir 执行验收。
正式功能操作与 GUI 面板验收全部经 stdio MCP -> TCP -> ANSA 主线程；gRPC 仅用于
独立实例核对和桥接初始化。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import traceback
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFICATION_ROOT = PROJECT_ROOT / "runtime" / "verification"
EXPECTED_TOOLS = frozenset({
    "ansa_connection_status",
    "ansa_model_snapshot",
    "ansa_grid_create",
    "ansa_grid_read",
    "ansa_grid_update",
    "ansa_grid_delete",
    "ansa_model_save",
    "ansa_task_query",
    "ansa_task_cancel",
    "ansa_entity_count",
    "ansa_entity_list",
    "ansa_entity_get",
    "ansa_entity_create",
    "ansa_entity_update",
    "ansa_entity_delete",
    "ansa_entity_find",
    "ansa_model_summary",
    "ansa_model_includes",
    "ansa_entity_bounds",
    "ansa_node_coordinates",
    "ansa_model_new",
    "ansa_model_open",
    "ansa_script_validate",
    "ansa_script_execute",
    "ansa_api_reference",
    "ansa_model_inspect",
    "ansa_task_list",
    "ansa_panel_show",
    "ansa_model_export",
    "ansa_model_import",
    "ansa_model_autosave",
    "ansa_model_save_current",
    "ansa_part_create",
    "ansa_mesh_faces",
    "ansa_mesh_surface",
    "ansa_mesh_delete",
    "ansa_mesh_settings",
    "ansa_set_members",
    "ansa_set_add",
    "ansa_geometry_select",
    "ansa_geometry_preview",
    "ansa_geometry_delete",
    "ansa_check_run",
    "ansa_mesh_check",
    "ansa_rigid_dependencies",
    "ansa_mesh_failed_elements",
    "ansa_mesh_intersections",
    "ansa_mesh_penetrations",
    "ansa_memory_read",
    "ansa_memory_write",
    "ansa_connection_list",
    "ansa_connection_create",
    "ansa_connection_check",
    "ansa_element_change_type",
    "ansa_mesh_volume",
    "ansa_mesh_hex_map",
    "ansa_kinematics_body_create",
    "ansa_kinematics_joint_create",
    "ansa_kinematics_config_create",
    "ansa_kinematics_inspect",
    "ansa_contact_inspect",
    "ansa_mesh_workflow",
    "ansa_benchmark_run",
    "ansa_connection_apply",
    "ansa_log_read",
})
TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled", "expired", "unknown"})
PANEL_WAIT_SECONDS = 900


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _runtime_home(value: Path) -> Path:
    home = Path.home().resolve()
    resolved = value.expanduser().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError as exc:
        raise ValueError("--runtime-home 必须位于当前 C 用户目录下") from exc
    if not relative.parts:
        raise ValueError("--runtime-home 不能直接指向用户目录")
    if resolved.exists() and resolved.is_symlink():
        raise ValueError("--runtime-home 不能是符号链接")
    return resolved


def _ansa_root(value: Path) -> Path:
    root = value.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("--ansa-root 必须是 ANSA 安装目录")
    if not (root / "ansa64.bat").is_file():
        raise ValueError("--ansa-root 下找不到 ansa64.bat")
    return root


def _free_port() -> int:
    # ANSA rejects low ports that Windows may allocate for bind(..., 0).
    # Prefer its documented default and nearby unused high ports.
    for port in range(50051, 50151):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No available ANSA test port in 50051..50150; specify --port")


def _prepare(args: argparse.Namespace) -> int:
    root = _ansa_root(args.ansa_root)
    home = _runtime_home(args.runtime_home)
    port = args.port or _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError(f"gRPC 端口 {port} 已被占用") from exc

    run_dir = VERIFICATION_ROOT / ("stage-three-" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    translator = run_dir / "stage_three_isolated_transl.py"
    translator.write_text(
        "# Stage-three isolated ANSA gRPC test loader; bridge starts after identity checks.\n",
        encoding="utf-8",
    )
    command = [
        str(root / "ansa64.bat"),
        "-grpcport", str(port),
        "-grpchost", "127.0.0.1",
        "-translfp", str(translator),
        "-nolauncher",
        "--skip-release-highlights",
    ]
    instructions = {
        "stage": "launch_owned_gui",
        "run_dir": str(run_dir),
        "ansa_root": str(root),
        "runtime_home": str(home),
        "runtime_home_requirement": "位于当前 C 用户目录；MCP backend 创建并验证令牌与目录 ACL",
        "port": port,
        "translator": str(translator),
        "launch_command": command,
        "environment": {
            "ANSA_MCP_SUM_AUTO_POLL": "0",
            "ANSA_MCP_SUM_HOME": str(run_dir / "unused-legacy-runtime"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "ownership_gate": [
            "脚本先确认 ANSA 主线程、PID、-grpcport、-grpchost、-translfp 与本运行目录一致",
            "核验通过前不会写入模型或发送 Stop",
            "通过后只对该唯一 translator 所属进程执行测试；成功结束时清理前会再次核对 PID 与完整参数",
        ],
        "operator_steps": [
            "主 Agent 使用 environment 和 launch_command 启动独立 GUI；本脚本本身不会启动 ANSA",
            "等待独立 GUI 就绪后，执行 stage_three_ansa.py --ansa-root <ANSA安装目录> --runtime-home <C用户私有运行目录> --run-dir <run_dir>",
            "遇到 ui_stage.json 时按 ui_instruction.json 操作对应 ANSA 模型窗口",
        ],
        "window_rule": "UI 指令使用实际模型窗口文件名；不会用 PID 充当窗口标题",
    }
    _json_write(run_dir / "launch_instructions.json", instructions)
    _json_write(run_dir / "ui_instruction.json", {
        "stage": "launch_owned_gui",
        "window_file": None,
        "instructions": instructions["operator_steps"],
    })
    report = {
        "stage": "three",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "runtime_home": str(home),
        "ansa_root": str(root),
        "port": port,
        "launch_instructions": str(run_dir / "launch_instructions.json"),
        "report_path": str(run_dir / "report.json"),
        "success": False,
        "status": "waiting_for_owned_gui",
        "cases": [],
        "unverified": [],
    }
    _json_write(run_dir / "report.json", report)
    print(json.dumps({
        "status": report["status"],
        "run_dir": str(run_dir),
        "launch_instructions": str(run_dir / "launch_instructions.json"),
        "next": "主 Agent 启动独立 GUI 后，使用 --run-dir 执行自动核心验收",
    }, ensure_ascii=False, indent=2), flush=True)
    return 0


def _load_run(args: argparse.Namespace) -> tuple[Path, dict]:
    if args.run_dir is None:
        raise ValueError("--run-dir is required after the independent GUI has been launched")
    run_dir = args.run_dir.expanduser().resolve()
    try:
        run_dir.relative_to(VERIFICATION_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("--run-dir 必须位于仓库 runtime/verification 下") from exc
    if not run_dir.is_dir() or not run_dir.name.startswith("stage-three-"):
        raise ValueError("--run-dir 必须是本脚本创建的 stage-three-* 目录")
    instructions_path = run_dir / "launch_instructions.json"
    if not instructions_path.is_file():
        raise ValueError("run-dir 缺少 launch_instructions.json")
    instructions = json.loads(instructions_path.read_text(encoding="utf-8"))
    if Path(instructions["ansa_root"]).resolve() != _ansa_root(args.ansa_root):
        raise ValueError("--ansa-root 与准备阶段记录不一致")
    if Path(instructions["runtime_home"]).resolve() != _runtime_home(args.runtime_home):
        raise ValueError("--runtime-home 与准备阶段记录不一致")
    return run_dir, instructions


def _append_case(report: dict, run_dir: Path, name: str, evidence: dict | None = None) -> None:
    entry = {"name": name, "ok": True}
    if evidence:
        entry["evidence"] = evidence
    report["cases"] = [case for case in report.get("cases", []) if case["name"] != name]
    report["cases"].append(entry)
    _json_write(run_dir / "report.json", report)
    print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)


def _exception_tree(exc: BaseException) -> dict:
    node = {"type": type(exc).__name__, "message": str(exc)}
    children = getattr(exc, "exceptions", None)
    if isinstance(children, (tuple, list)):
        node["exceptions"] = [_exception_tree(child) for child in children]
    return node


def _record_failure(report: dict, run_dir: Path, exc: BaseException) -> None:
    error_path = run_dir / "error.log"
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        error_path.write_text(trace, encoding="utf-8")
    except OSError as write_error:
        report["error_log_write_error"] = f"{type(write_error).__name__}: {write_error}"
    report["success"] = False
    report["fatal_error"] = f"{type(exc).__name__}: {exc}"
    report["exception_tree"] = _exception_tree(exc)
    report["failed_at"] = datetime.now(timezone.utc).isoformat()
    report["error_log"] = str(error_path)
    _json_write(run_dir / "report.json", report)


def _clear_prior_failure(report: dict) -> None:
    for key in ("fatal_error", "exception_tree", "failed_at", "error_log"):
        report.pop(key, None)


def _write_ui_stage(run_dir: Path, stage: str, window_file: str, **fields) -> None:
    value = {
        "stage": stage,
        "window_file": window_file,
        "status": "waiting_for_operator",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    _json_write(run_dir / "ui_stage.json", value)


def _import_runtime_helpers():
    integration_dir = str(Path(__file__).resolve().parent)
    if integration_dir not in sys.path:
        sys.path.insert(0, integration_dir)
    # stage_two_ansa.rpc assigns a fresh remote module/function name for each
    # call; its fixtures and stage-one helpers are reused without shared names.
    import stage_one_ansa
    import stage_two_ansa
    return stage_one_ansa, stage_two_ansa


def _bounded_grpc(grpc, deadline_interceptor):
    original = grpc.insecure_channel

    def bounded(target, options=None, **kwargs):
        channel = original(
            target,
            options=[*(options or []), ("grpc.enable_http_proxy", 0)],
            **kwargs,
        )
        return grpc.intercept_channel(channel, deadline_interceptor(15))

    grpc.insecure_channel = bounded
    return original


def _initialize_runtime_home(runtime_home: Path) -> None:
    source = str(PROJECT_ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from ansa_mcp_plugin.backend.service import Service

    service = Service(runtime_home)
    try:
        token_path = runtime_home / "config" / "token.txt"
        if not token_path.is_file() or len(token_path.read_text(encoding="ascii").strip()) != 64:
            raise RuntimeError("runtime private token initialization was not confirmed")
    finally:
        service.close()


def _normalize_argument(value: str) -> str:
    return str(value).strip().strip('"').replace("/", "\\").casefold()


def _verify_program_arguments(basic: dict, translator: Path, port: int) -> None:
    if not basic.get("main_thread"):
        raise RuntimeError("远程探针没有在 ANSA 主线程运行")
    arguments = [str(item) for item in basic.get("program_args", [])]
    if not isinstance(basic.get("pid"), int) or basic["pid"] <= 0:
        raise RuntimeError("ANSA 探针没有返回有效 PID")
    if not any(_normalize_argument(item) == _normalize_argument(str(translator)) for item in arguments):
        raise RuntimeError("ANSA -translfp 与本次唯一 translator 不一致")
    lower = [item.casefold() for item in arguments]
    try:
        grpc_index = lower.index("-grpcport")
        host_index = lower.index("-grpchost")
    except ValueError as exc:
        raise RuntimeError("ANSA 参数缺少 -grpcport 或 -grpchost") from exc
    if grpc_index + 1 >= len(arguments) or arguments[grpc_index + 1] != str(port):
        raise RuntimeError("ANSA gRPC 端口与本次运行目录记录不一致")
    if host_index + 1 >= len(arguments) or arguments[host_index + 1] != "127.0.0.1":
        raise RuntimeError("ANSA gRPC host 不是回环地址")


def stage_three_remote_refresh_operations(expected_pid, expected_arguments):
    import builtins
    import importlib
    import os
    import sys
    import threading
    from ansa import session
    if (os.getpid() != expected_pid or not (threading.current_thread() is threading.main_thread())
            or [str(item) for item in session.ProgramArguments()] != expected_arguments):
        raise RuntimeError("Owned development process identity changed before module refresh")
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    if bridge is not None and bridge.state.journal.busy():
        raise RuntimeError("Development module refresh requires no queued/running/unknown tasks")
    name = "ansa_mcp_bridge.operations.entity_catalog"
    if name in sys.modules:
        importlib.reload(sys.modules[name])
        return {"module": name, "reloaded": True}
    return {"module": name, "reloaded": False}


def _prepare_owned_ansa(run_dir: Path, instructions: dict) -> tuple[dict, object, object, object]:
    import ansa
    import grpc

    stage_one, stage_two = _import_runtime_helpers()
    original_channel = _bounded_grpc(grpc, stage_one.DeadlineInterceptor)
    port = int(instructions["port"])
    translator = Path(instructions["translator"]).resolve()
    channel = original_channel("127.0.0.1:" + str(port), options=[("grpc.enable_http_proxy", 0)])
    try:
        grpc.channel_ready_future(channel).result(timeout=PANEL_WAIT_SECONDS)
    finally:
        channel.close()

    # This is the required write/stop safety gate: exact PID and launch args,
    # ANSA main thread, and our unique translator are confirmed before seeding.
    basic = stage_two.rpc(port, stage_one.remote_basic)
    _verify_program_arguments(basic, translator, port)
    stage_two.rpc(port, stage_three_remote_refresh_operations, basic["pid"], basic["program_args"])
    identity = {
        "pid": basic["pid"],
        "program_args": list(basic["program_args"]),
        "port": port,
        "translator": str(translator),
    }

    # Initialize private ACLs, token, and SQLite state before the ANSA bridge
    # starts and reads config/token.txt.
    _initialize_runtime_home(Path(instructions["runtime_home"]))

    seed_path = run_dir / "seed-baseline.ansa"
    seed = stage_two.rpc(port, stage_two.remote_seed, str(seed_path))
    if not seed.get("usable") or not seed_path.is_file() or seed_path.stat().st_size == 0:
        raise RuntimeError("独立 GUI 的 NASTRAN baseline fixture 保存失败")
    shutil.copy2(seed_path, run_dir / "seed-baseline-backup.ansa")

    checked_again = stage_two.rpc(port, stage_one.remote_basic)
    _verify_program_arguments(checked_again, translator, port)
    if checked_again["pid"] != identity["pid"] or checked_again["program_args"] != identity["program_args"]:
        raise RuntimeError("baseline fixture 写入前后 ANSA PID/参数发生变化")
    started = stage_two.rpc(
        port,
        stage_one.remote_start,
        str(PROJECT_ROOT),
        str(instructions["runtime_home"]),
        str(run_dir / "bridge-start.marker"),
    )
    if started.get("pid") != identity["pid"] or not started.get("main_thread"):
        raise RuntimeError("桥接没有在已核验的 ANSA 主线程中启动")
    from ansa_mcp_shared.identity import process_start_filetime
    identity.update({
        "instance_id": started["instance_id"],
        "session_id": started["session_id"],
        "process_start": process_start_filetime(identity["pid"]),
        "bridge_port": started["port"],
        "seed_path": str(seed_path),
        "seed_entity_id": seed["id"],
    })
    identity["capabilities"] = stage_two.rpc(port, stage_two.remote_capabilities)
    return identity, ansa, grpc, (stage_one, stage_two, original_channel)


class McpHarness:
    def __init__(self, client, report: dict, run_dir: Path):
        self.client = client
        self.report = report
        self.run_dir = run_dir
        self.target = None
        self.model = None
        self.snapshot = None

    async def call(self, name: str, arguments: dict | None = None, *, allow_error: bool = False) -> dict:
        response = await self.client.call_tool(name, arguments or {})
        value = response.structuredContent
        if value is None:
            text_parts = [item.text for item in response.content if getattr(item, "type", None) == "text"]
            if text_parts:
                try:
                    value = json.loads(text_parts[0])
                except json.JSONDecodeError:
                    value = None
        if not isinstance(value, dict):
            raise RuntimeError(f"{name} 没有返回结构化 JSON：{response.content!r}")
        if not allow_error and (response.isError or value.get("ok") is False):
            raise RuntimeError(f"{name} 失败：{value.get('error') or value}")
        return value

    async def list_schema(self) -> dict:
        listed = await self.client.list_tools()
        names = {tool.name for tool in listed.tools}
        if names != EXPECTED_TOOLS or len(listed.tools) != len(EXPECTED_TOOLS):
            raise AssertionError(f"MCP 工具表不符：预期 {len(EXPECTED_TOOLS)} 个，实际 {len(listed.tools)} 个")
        missing_input = [tool.name for tool in listed.tools if not isinstance(tool.inputSchema, dict) or not tool.inputSchema]
        missing_output = [tool.name for tool in listed.tools if not isinstance(tool.outputSchema, dict) or not tool.outputSchema]
        if missing_input or missing_output:
            raise AssertionError(f"工具缺 schema：input={missing_input}, output={missing_output}")
        return {
            "count": len(listed.tools),
            "input_schemas": len(listed.tools) - len(missing_input),
            "output_schemas": len(listed.tools) - len(missing_output),
            "names": sorted(names),
        }

    async def wait_task(self, task_id: str, expected: str | None = "succeeded", timeout_seconds: float = 300) -> dict:
        deadline = time.monotonic() + timeout_seconds
        last = None
        while time.monotonic() < deadline:
            last = await self.call("ansa_task_query", {"task_id": task_id}, allow_error=True)
            state = last.get("status")
            if state in TERMINAL_TASK_STATES:
                if expected is not None and state != expected:
                    raise AssertionError(f"任务 {task_id} 预期 {expected}，实际 {state}: {last.get('error')}")
                return last
            await asyncio.sleep(0.2)
        raise TimeoutError(f"任务 {task_id} 未在 {timeout_seconds} 秒内结束；最后状态={last}")

    async def task(self, name: str, fields: dict, *, expected: str = "succeeded",
                   operation_id: str | None = None, timeout_seconds: float = 300) -> tuple[dict, dict]:
        op = operation_id or ("stage-three-" + uuid.uuid4().hex)
        arguments = {
            "operation_id": op,
            "expected_target": self.target,
            "expected_model": self.model,
            **fields,
        }
        submitted = await self.call(name, arguments, allow_error=True)
        task_id = submitted.get("task_id")
        if not task_id:
            raise AssertionError(f"{name} 未创建任务：{submitted.get('error') or submitted}")
        completed = await self.wait_task(task_id, expected, timeout_seconds)
        return submitted, completed

    async def snapshot_context(self) -> dict:
        if self.target is None:
            raise RuntimeError("尚未绑定 ANSA target")
        value = await self.call("ansa_model_snapshot", {"expected_target": self.target})
        self.snapshot = value["data"]
        self.model = {"epoch": self.snapshot["model"]["epoch"]}
        return self.snapshot

    async def direct_result(self, name: str, fields: dict, *, expected: str = "succeeded") -> dict:
        _submitted, completed = await self.task(name, fields, expected=expected)
        if expected == "succeeded":
            result = completed.get("data", {}).get("result")
            if not isinstance(result, dict):
                raise AssertionError(f"{name} 没有成功结果对象：{completed}")
            return result
        return completed


async def _wait_active(h: McpHarness, expected_pid: int, stage_one, stage_two,
                      port: int, timeout_seconds: float = 60) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = await h.call("ansa_connection_status")
        active = last.get("data", {}).get("active_target")
        if active:
            if active.get("pid") != expected_pid:
                raise RuntimeError("当前绑定目标不是本次独立 ANSA GUI")
            h.target = active
            return active
        await asyncio.sleep(0.25)
    raise TimeoutError(f"独立 ANSA 尚未通过 MCP bind：{last}")


async def _bind_for_run(h: McpHarness, identity: dict, stage_one, stage_two,
                        port: int, *, gui_only: bool = False) -> dict:
    status = await h.call("ansa_connection_status")
    data = status["data"]
    for instance in data.get("instances", []):
        if instance.get("pid") == identity["pid"] and instance.get("instance_id") != identity["instance_id"]:
            raise RuntimeError("runtime 中的 ANSA 实例与本轮记录的 instance_id 不一致")
    active = data.get("active_target")
    if active and active.get("pid") != identity["pid"]:
        raise RuntimeError("runtime 已绑定到其他 ANSA 进程；为避免切换外部目标，停止验收")
    if not active:
        stage_two.rpc(port, stage_one.remote_click_bind)
        active = await _wait_active(h, identity["pid"], stage_one, stage_two, port)
    else:
        h.target = active
    snap = await h.snapshot_context()
    if snap.get("pid") != identity["pid"] or not snap.get("main_thread"):
        raise RuntimeError("MCP model snapshot PID 或主线程验证失败")
    if not snap["model"].get("write_enabled"):
        raise RuntimeError("当前模型写入保护门未开启；不尝试绕过")
    if gui_only and h.target.get("instance_id") != identity["instance_id"]:
        raise RuntimeError("GUI-only 目标实例与核心验收实例不一致")
    return snap


async def _api_and_script_probe(h: McpHarness, report: dict, run_dir: Path) -> dict:
    modules = await h.direct_result("ansa_api_reference", {
        "action": "modules", "query": "base", "limit": 10,
    })
    version = str(modules.get("version", ""))
    if not (version.startswith("26") or version.startswith("2026")):
        raise AssertionError(f"API reference 不是当前 26/2026 文档版本：{version!r}")
    if "base" not in modules.get("modules", []):
        raise AssertionError("API reference 未列出 base 模块")
    lookup = await h.direct_result("ansa_api_reference", {
        "action": "lookup", "module": "base", "query": "GetKeywordFieldLabels", "limit": 10,
    })
    search = await h.direct_result("ansa_api_reference", {
        "action": "search", "module": "base", "query": "GetKeywordFieldLabels", "limit": 10,
    })
    if lookup.get("api") != "ansa.base.GetKeywordFieldLabels":
        raise AssertionError(f"API lookup 未定位到 GetKeywordFieldLabels：{lookup}")
    if not any(item.get("api") == "ansa.base.GetKeywordFieldLabels" for item in search.get("matches", [])):
        raise AssertionError(f"API search 没有命中 GetKeywordFieldLabels：{search}")
    _append_case(report, run_dir, "api_reference_lookup_search_current_26", {
        "version": version,
        "lookup": lookup["api"],
        "search_matches": search.get("total"),
    })

    probe_mid = 100_000_000 + int(uuid.uuid4().hex[:7], 16)
    probe_script = (
        "from ansa import base, constants\n"
        "def field_labels(entity, deck):\n"
        "  fields = entity.card_fields(deck)\n"
        "  if hasattr(fields, 'keys'):\n"
        "    return [str(name) for name in fields.keys()]\n"
        "  labels = []\n"
        "  for item in fields:\n"
        "    if isinstance(item, (tuple, list)) and item:\n"
        "      item = item[0]\n"
        "    labels.append(str(item))\n"
        "  return labels\n"
        "RESULT = {\n"
        "  'current_deck': base.CurrentDeck(),\n"
        "  'nastran_deck': constants.NASTRAN,\n"
        "  'abaqus_deck': constants.ABAQUS,\n"
        "  'mat1_keyword_labels': list(base.GetKeywordFieldLabels(constants.NASTRAN, 'MAT1')),\n"
        "  'abaqus_node_keyword_labels': list(base.GetKeywordFieldLabels(constants.ABAQUS, 'NODE')),\n"
        "}\n"
        f"probe_mid = {probe_mid}\n"
        "while base.GetEntity(constants.NASTRAN, 'MAT1', probe_mid) is not None:\n"
        "  probe_mid += 1\n"
        "probe_entity = None\n"
        "try:\n"
        "  probe_entity = base.CreateEntity(constants.NASTRAN, 'MAT1', {'MID': probe_mid})\n"
        "  if probe_entity is None:\n"
        "    raise RuntimeError('MAT1 field probe CreateEntity returned None')\n"
        "  RESULT['mat1_entity_labels'] = field_labels(probe_entity, constants.NASTRAN)\n"
        "finally:\n"
        "  if probe_entity is not None:\n"
        "    base.DeleteEntity(probe_entity, force=False, compress=False)\n"
        "    RESULT['mat1_probe_absent'] = base.GetEntity(constants.NASTRAN, 'MAT1', probe_mid) is None\n"
        "    if not RESULT['mat1_probe_absent']:\n"
        "      raise RuntimeError('MAT1 field probe entity remained after DeleteEntity')\n"
        "grid_entities = base.CollectEntities(constants.NASTRAN, None, 'GRID') or []\n"
        "if grid_entities:\n"
        "  try:\n"
        "    RESULT['abaqus_node_labels'] = field_labels(grid_entities[0], constants.ABAQUS)\n"
        "  except Exception as exc:\n"
        "    RESULT['abaqus_node_probe_error'] = type(exc).__name__ + ': ' + str(exc)\n"
        "else:\n"
        "  RESULT['abaqus_node_probe_error'] = 'No existing NASTRAN GRID was available'\n"
    )
    probe = await h.direct_result("ansa_script_execute", {"script": probe_script, "validate": True})
    raw = probe.get("result")
    if not isinstance(raw, dict):
        raise AssertionError(f"GetKeywordFieldLabels probe did not return an object: {probe}")
    mat1_keyword_labels = set(raw.get("mat1_keyword_labels", []))
    mat1_entity_labels = set(raw.get("mat1_entity_labels", []))
    if raw.get("mat1_probe_absent") is not True:
        raise AssertionError(f"MAT1 card_fields 探针实体未确认删除：{raw}")
    if not {"MID", "E", "NU"}.issubset(mat1_entity_labels):
        raise AssertionError(
            "真实 MAT1 Entity.card_fields 未返回 MID/E/NU："
            f"keyword_labels={sorted(mat1_keyword_labels)}, entity_labels={sorted(mat1_entity_labels)}"
        )
    _append_case(report, run_dir, "keyword_field_labels_probe", {
        "current_deck": raw.get("current_deck"),
        "nastran_mat1_keyword_labels": sorted(mat1_keyword_labels),
        "nastran_mat1_entity_labels": sorted(mat1_entity_labels),
        "mat1_probe_absent": raw["mat1_probe_absent"],
        "abaqus_node_keyword_labels": raw.get("abaqus_node_keyword_labels", []),
        "abaqus_node_fields": len(raw.get("abaqus_node_labels", [])),
        "abaqus_node_probe_error": raw.get("abaqus_node_probe_error"),
    })
    return raw


async def _generic_mat1(h: McpHarness, report: dict, run_dir: Path, deck: int) -> list[dict]:
    existing = await h.direct_result("ansa_entity_list", {
        "entity_type": "MAT1", "deck": deck, "fields": ["MID"], "offset": 0, "limit": 200,
    })
    used = {
        int(row["fields"]["MID"])
        for row in existing.get("entities", [])
        if type(row.get("fields", {}).get("MID")) in (int, float)
    }
    base_mid = 100_000_000 + int(uuid.uuid4().hex[:7], 16)
    while base_mid in used or base_mid + 1 in used:
        base_mid += 2

    created = []
    for mid, modulus, poisson in ((base_mid, 210000.0, 0.29), (base_mid + 1, 180000.0, 0.27)):
        default_create = existing.get("total", 0) == 0 and not created
        result = await h.direct_result("ansa_entity_create", {
            "entity_type": "MAT1",
            "deck": deck,
            "fields": {} if default_create else {"MID": mid, "E": modulus, "NU": poisson},
        })
        reference = result.get("entity", {})
        if default_create:
            mid = reference["id"]
            await h.direct_result("ansa_entity_update", {
                "entity_type": "MAT1", "entity_id": mid, "deck": deck,
                "expected_fields": {"MID": mid}, "fields": {"E": modulus, "NU": poisson},
            })
        if reference.get("type") != "MAT1" or reference.get("id") != mid:
            raise AssertionError(f"MAT1 create ID/readback mismatch: {result}")
        readback = await h.direct_result("ansa_entity_get", {
            "entity_type": "MAT1", "entity_id": reference["id"], "deck": deck,
            "fields": ["MID", "E", "NU"],
        })
        if readback.get("fields") != {"MID": mid, "E": modulus, "NU": poisson}:
            raise AssertionError(f"MAT1 create/readback mismatch: {readback}")
        created.append({"id": reference["id"], "fields": readback["fields"]})

    first = created[0]
    updated = await h.direct_result("ansa_entity_update", {
        "entity_type": "MAT1",
        "entity_id": first["id"],
        "deck": deck,
        "expected_fields": {"E": first["fields"]["E"], "NU": first["fields"]["NU"]},
        "fields": {"E": 220000.0},
    })
    if updated.get("fields", {}).get("E") != 220000.0:
        raise AssertionError(f"MAT1 update readback mismatch: {updated}")
    stale = await h.task("ansa_entity_update", {
        "entity_type": "MAT1",
        "entity_id": first["id"],
        "deck": deck,
        "expected_fields": {"E": first["fields"]["E"]},
        "fields": {"E": 230000.0},
    }, expected="failed")
    stale_error = stale[1].get("error", {}).get("message", "")
    if "changed" not in stale_error.casefold():
        raise AssertionError(f"MAT1 stale-field rejection lacked expected evidence: {stale[1]}")
    after_stale = await h.direct_result("ansa_entity_get", {
        "entity_type": "MAT1", "entity_id": first["id"], "deck": deck, "fields": ["E", "NU"],
    })
    if after_stale.get("fields", {}).get("E") != 220000.0:
        raise AssertionError("Stale MAT1 update changed the current E field")

    before_delete_count = (await h.direct_result("ansa_entity_count", {
        "entity_type": "MAT1", "deck": deck,
    }))["count"]
    removed = await h.direct_result("ansa_entity_delete", {
        "entity_type": "MAT1",
        "deck": deck,
        "entities": [
            {"entity_id": first["id"], "expected_fields": {"E": 220000.0}},
            {"entity_id": created[1]["id"], "expected_fields": {"E": created[1]["fields"]["E"]}},
        ],
    })
    if len(removed.get("deleted", [])) != 2:
        raise AssertionError(f"MAT1 bulk delete count mismatch: {removed}")
    after_delete_count = (await h.direct_result("ansa_entity_count", {
        "entity_type": "MAT1", "deck": deck,
    }))["count"]
    if after_delete_count != before_delete_count - 2:
        raise AssertionError("MAT1 count did not fall by two after bulk delete")
    for item in created:
        missing = await h.task("ansa_entity_get", {
            "entity_type": "MAT1", "entity_id": item["id"], "deck": deck, "fields": ["MID"],
        }, expected="failed")
        if "not exist" not in missing[1].get("error", {}).get("message", "").casefold():
            raise AssertionError(f"MAT1 absence readback did not confirm deletion: {missing[1]}")
    _append_case(report, run_dir, "generic_mat1_create_read_update_stale_bulk_delete_absence", {
        "ids": [item["id"] for item in created],
        "updated_E": updated["fields"]["E"],
        "deleted_count": len(removed["deleted"]),
        "count_before_after": [before_delete_count, after_delete_count],
    })
    return created


async def _grid_catalog(h: McpHarness, report: dict, run_dir: Path, deck: int) -> list[int]:
    specs = [([-10.0, 2.0, 3.0]), ([10.0, 5.0, 8.0])]
    ids = []
    for position in specs:
        created = await h.direct_result("ansa_grid_create", {"position": position, "deck": deck})
        if created.get("coordinates") != position:
            raise AssertionError(f"GRID create readback mismatch: {created}")
        ids.append(created["entity"]["id"])

    coordinates = await h.direct_result("ansa_node_coordinates", {
        "node_ids": ids, "deck": deck,
    })
    rows = coordinates.get("nodes", [])
    if {row.get("entity", {}).get("id") for row in rows} != set(ids):
        raise AssertionError(f"GRID coordinate lookup omitted entities: {coordinates}")
    coordinate_by_id = {row["entity"]["id"]: row.get("coordinates") for row in rows}
    if coordinate_by_id.get(ids[0]) != specs[0] or coordinate_by_id.get(ids[1]) != specs[1]:
        raise AssertionError(f"GRID coordinate lookup mismatch: {coordinates}")

    first_page = await h.direct_result("ansa_entity_list", {
        "entity_type": "GRID", "deck": deck, "fields": ["X1", "X2", "X3"],
        "offset": 0, "limit": 1,
    })
    second_page = await h.direct_result("ansa_entity_list", {
        "entity_type": "GRID", "deck": deck, "fields": ["X1", "X2", "X3"],
        "offset": 1, "limit": 1,
    })
    first_rows, second_rows = first_page.get("entities", []), second_page.get("entities", [])
    if len(first_rows) != 1 or len(second_rows) != 1:
        raise AssertionError("GRID pagination did not return one row per page")
    if first_rows[0]["entity"]["id"] == second_rows[0]["entity"]["id"]:
        raise AssertionError("GRID pagination repeated the same entity")

    bounds = await h.direct_result("ansa_entity_bounds", {
        "entity_type": "GRID", "entity_ids": ids, "deck": deck,
    })
    if bounds.get("bounds") != {"min": [-10.0, 2.0, 3.0], "max": [10.0, 5.0, 8.0]}:
        raise AssertionError(f"GRID bounds mismatch: {bounds}")

    _append_case(report, run_dir, "grid_coordinates_pagination_bounds", {
        "ids": ids,
        "coordinates": coordinate_by_id,
        "page_ids": [first_rows[0]["entity"]["id"], second_rows[0]["entity"]["id"]],
        "bounds": bounds["bounds"],
    })
    return ids


async def _summary_inventory(h: McpHarness, report: dict, run_dir: Path, deck: int) -> None:
    summary = await h.direct_result("ansa_model_summary", {"deck": deck})
    includes = await h.direct_result("ansa_model_includes", {
        "deck": deck, "offset": 0, "limit": 20,
    })
    grid_count = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
    mat_count = (await h.direct_result("ansa_entity_count", {"entity_type": "MAT1", "deck": deck}))["count"]
    inventory = await h.direct_result("ansa_model_inspect", {
        "action": "geometry_inventory",
        "deck": deck,
        "options": {"entity_types": ["GRID", "MAT1"], "offset": 0, "limit": 20},
    })
    counts = {item["entity_type"]: item["count"] for item in inventory.get("counts", [])}
    if counts.get("GRID") != grid_count or counts.get("MAT1") != mat_count:
        raise AssertionError(f"geometry_inventory 与实体计数不同：{inventory}")
    if not isinstance(summary.get("entity_count"), int) or summary["entity_count"] < grid_count:
        raise AssertionError(f"model_summary entity_count 不合理：{summary}")
    if not isinstance(includes.get("includes"), list) or includes.get("total") < len(includes["includes"]):
        raise AssertionError(f"model_includes 分页结果不合理：{includes}")
    report["geometry_validation"] = (
        "GRID/MAT1 inventory counts compared with entity_count; CAD face/curve/solid checks "
        "are unverified because this acceptance fixture does not create real CAD geometry."
    )
    report.setdefault("unverified", []).append(
        "实际 CAD 几何面/曲线/实体的几何属性与质量检查；本轮只核验 GRID/MAT1 计数"
    )
    _append_case(report, run_dir, "model_summary_includes_geometry_inventory_counts", {
        "entity_count": summary["entity_count"],
        "include_total": includes["total"],
        "grid_count": grid_count,
        "mat1_count": mat_count,
        "inventory_counts": counts,
        "geometry_boundary": "实际 CAD 几何检查未验证",
    })


async def _script_cases(h: McpHarness, report: dict, run_dir: Path, deck: int) -> None:
    invalid_script = (
        "from ansa import base\n"
        "base.__stage_three_missing_api__()\n"
        "RESULT = None\n"
    )
    validation = await h.direct_result("ansa_script_validate", {"script": invalid_script})
    if validation.get("valid") is not False or not validation.get("missing"):
        raise AssertionError(f"script_validate 没有报告无效 API：{validation}")
    _append_case(report, run_dir, "script_validate_invalid_api", {
        "valid": validation["valid"],
        "missing": [item.get("api") for item in validation["missing"]],
    })

    rejected, rejected_task = await h.task("ansa_script_execute", {
        "script": invalid_script, "validate": True,
    }, expected="failed")
    if "Missing ANSA API" not in rejected_task.get("error", {}).get("message", ""):
        raise AssertionError(f"script_execute 未拒绝缺失 API：{rejected_task}")

    main_script = (
        "import threading\n"
        "RESULT = {'main_thread': threading.current_thread() is threading.main_thread(), "
        "'thread_id': threading.get_ident()}\n"
    )
    main_result = await h.direct_result("ansa_script_execute", {"script": main_script, "validate": True})
    if not main_result.get("result", {}).get("main_thread"):
        raise AssertionError(f"script_execute 没有在主线程执行：{main_result}")

    non_json, non_json_task = await h.task("ansa_script_execute", {
        "script": "RESULT = (1, 2)\n", "validate": True,
    }, expected="failed")
    if "non-JSON" not in non_json_task.get("error", {}).get("message", ""):
        raise AssertionError(f"tuple RESULT 没有按严格 JSON 拒绝：{non_json_task}")
    echo = await h.direct_result("ansa_script_execute", {
        "script": "RESULT = {'continued': True}\n", "validate": True,
    })
    if echo.get("result") != {"continued": True}:
        raise AssertionError(f"tuple RESULT 失败后后续脚本任务未执行：{echo}")
    _append_case(report, run_dir, "script_main_thread_strict_result_and_recovery", {
        "invalid_api_task": rejected_task.get("status"),
        "main_thread": main_result["result"]["main_thread"],
        "tuple_result_task": non_json_task.get("status"),
        "subsequent_task": echo["result"],
    })

    before = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
    partial_script = (
        "from ansa import base, constants\n"
        "base.CreateEntity(constants.NASTRAN, 'GRID', "
        "{'X1': 31.25, 'X2': 32.5, 'X3': 33.75})\n"
        "raise RuntimeError('stage-three-partial-write')\n"
    )
    operation_id = "stage-three-partial-" + uuid.uuid4().hex
    immutable_request = {
        "operation_id": operation_id,
        "expected_target": h.target,
        "expected_model": h.model,
        "script": partial_script,
        "validate": True,
    }
    submitted = await h.call("ansa_script_execute", immutable_request, allow_error=True)
    if not submitted.get("task_id"):
        raise AssertionError(f"部分写入脚本未入队：{submitted}")
    failed = await h.wait_task(submitted["task_id"], "failed")
    repeated = await h.call("ansa_script_execute", immutable_request, allow_error=True)
    after = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
    if repeated.get("task_id") != submitted["task_id"] or after != before + 1:
        raise AssertionError(
            f"同 operation_id 重放了部分写入脚本：first={submitted}, retry={repeated}, count={before}->{after}"
        )
    _append_case(report, run_dir, "partial_write_failure_same_operation_not_replayed", {
        "task_id": failed["task_id"],
        "state": failed["status"],
        "grid_count_before_after": [before, after],
    })


async def _abaqus_node_crud(h: McpHarness, report: dict, run_dir: Path, probe: dict) -> None:
    abaqus_deck = probe.get("abaqus_deck")
    current_deck = probe.get("current_deck")
    labels = probe.get("abaqus_node_labels")
    if current_deck != 4 or abaqus_deck != 4:
        report.setdefault("unverified", []).append(
            f"ABAQUS deck 4 通用 NODE CRUD 未运行：current_deck={current_deck}, ABAQUS={abaqus_deck}"
        )
        _append_case(report, run_dir, "abaqus_node_generic_crud", {
            "verified": False,
            "reason": "当前 deck 不是 4",
            "current_deck": current_deck,
            "abaqus_deck": abaqus_deck,
        })
        return
    if not isinstance(labels, list):
        report.setdefault("unverified", []).append("ABAQUS NODE 字段读取失败，通用 CRUD 未运行")
        _append_case(report, run_dir, "abaqus_node_generic_crud", {
            "verified": False,
            "reason": probe.get("abaqus_node_probe_error", "GetKeywordFieldLabels 未返回字段"),
        })
        return

    label_set = set(labels)
    id_field = next((field for field in ("NID", "ID", "NODEID") if field in label_set), None)
    coord_fields = next((candidate for candidate in (("X", "Y", "Z"), ("X1", "X2", "X3"))
                         if set(candidate).issubset(label_set)), None)
    if id_field is None or coord_fields is None:
        report.setdefault("unverified", []).append(
            "ABAQUS NODE 字段集未暴露可确认的 ID 和三维坐标字段，通用 CRUD 未运行"
        )
        _append_case(report, run_dir, "abaqus_node_generic_crud", {
            "verified": False,
            "reason": "字段集缺少受支持的 ID/坐标字段",
            "labels": labels,
        })
        return

    entity_id = 700_000_000 + int(uuid.uuid4().hex[:7], 16)
    coords = [0.125, 0.25, 0.375]
    fields = {id_field: entity_id, **dict(zip(coord_fields, coords))}
    created = await h.direct_result("ansa_entity_create", {
        "entity_type": "NODE", "deck": abaqus_deck, "fields": fields,
    })
    actual_id = created.get("entity", {}).get("id")
    if not isinstance(actual_id, int) or actual_id <= 0:
        raise AssertionError(f"ABAQUS NODE create 未返回实体 ID：{created}")
    read = await h.direct_result("ansa_entity_get", {
        "entity_type": "NODE", "entity_id": actual_id, "deck": abaqus_deck,
        "fields": [id_field, *coord_fields],
    })
    actual_fields = read.get("fields", {})
    if any(actual_fields.get(field) != value for field, value in fields.items()):
        raise AssertionError(f"ABAQUS NODE generic readback mismatch: {read}")
    changed = [0.5, 0.625, 0.75]
    updated = await h.direct_result("ansa_entity_update", {
        "entity_type": "NODE", "entity_id": actual_id, "deck": abaqus_deck,
        "expected_fields": dict(zip(coord_fields, coords)),
        "fields": dict(zip(coord_fields, changed)),
    })
    if any(updated.get("fields", {}).get(field) != value for field, value in zip(coord_fields, changed)):
        raise AssertionError(f"ABAQUS NODE update readback mismatch: {updated}")
    await h.direct_result("ansa_entity_delete", {
        "entity_type": "NODE", "deck": abaqus_deck,
        "entities": [{"entity_id": actual_id, "expected_fields": dict(zip(coord_fields, changed))}],
    })
    missing = await h.task("ansa_entity_get", {
        "entity_type": "NODE", "entity_id": actual_id, "deck": abaqus_deck, "fields": [id_field],
    }, expected="failed")
    if "not exist" not in missing[1].get("error", {}).get("message", "").casefold():
        raise AssertionError(f"ABAQUS NODE 删除后缺少 absence 证据：{missing[1]}")
    _append_case(report, run_dir, "abaqus_node_generic_crud", {
        "verified": True,
        "deck": abaqus_deck,
        "fields": labels,
        "entity_id": actual_id,
    })


async def _model_lifecycle(h: McpHarness, report: dict, run_dir: Path, deck: int) -> tuple[Path, dict]:
    before = await h.snapshot_context()
    before_model = dict(h.model)
    before_database = before["model"].get("database")
    missing_backup = await h.task("ansa_model_new", {
        "preservation": "backup",
    }, expected="failed")
    after_rejection = await h.snapshot_context()
    if h.model != before_model or after_rejection["model"].get("database") != before_database:
        raise AssertionError("缺少 backup_path 的 New 改变了当前模型")
    if not missing_backup[1].get("error", {}).get("message"):
        raise AssertionError("缺少 backup_path 的 New 未返回拒绝理由")
    _append_case(report, run_dir, "new_default_missing_backup_rejected_unchanged", {
        "epoch": before_model["epoch"],
        "database_unchanged": True,
        "error": missing_backup[1]["error"]["message"],
    })

    backup_path = run_dir / ("before-new-" + uuid.uuid4().hex[:8] + ".ansa")
    _submitted, new_task = await h.task("ansa_model_new", {
        "preservation": "backup",
        "backup_path": str(backup_path),
    })
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise AssertionError("New 没有生成要求的模型备份")
    old_target = dict(h.target)
    old_model = dict(h.model)
    empty_snapshot = await h.snapshot_context()
    if not empty_snapshot["model"].get("write_enabled"):
        raise AssertionError("New 后空模型没有保持受控写入就绪")
    stale = await h.call("ansa_grid_create", {
        "operation_id": "stage-three-old-context-" + uuid.uuid4().hex,
        "expected_target": old_target,
        "expected_model": old_model,
        "position": [44.0, 45.0, 46.0],
        "deck": deck,
    }, allow_error=True)
    if stale.get("task_id") is not None or stale.get("status") != "failed":
        raise AssertionError(f"New 后旧 model context 没有拒绝：{stale}")
    _append_case(report, run_dir, "new_backup_empty_model_writable_old_context_rejected", {
        "backup": str(backup_path),
        "new_task": new_task["task_id"],
        "empty_epoch": h.model["epoch"],
        "write_enabled": empty_snapshot["model"]["write_enabled"],
    })

    unknown = await h.task("ansa_entity_create", {
        "entity_type": "MAT1", "deck": deck, "fields": {"STAGE_THREE_BAD_FIELD": 1},
    }, expected="failed")
    if (await h.direct_result("ansa_entity_count", {"entity_type": "MAT1", "deck": deck}))["count"] != 0:
        raise AssertionError("未知字段首次创建留下了 MAT1")
    default_material = await h.direct_result("ansa_entity_create", {
        "entity_type": "MAT1", "deck": deck, "fields": {},
    })
    material_id = default_material["entity"]["id"]
    default_fields = (await h.direct_result("ansa_entity_get", {
        "entity_type": "MAT1", "entity_id": material_id, "deck": deck, "fields": ["MID"],
    }))["fields"]
    configured = await h.direct_result("ansa_entity_update", {
        "entity_type": "MAT1", "entity_id": material_id, "deck": deck,
        "expected_fields": default_fields, "fields": {"E": 210000.0, "NU": 0.3},
    })
    await h.direct_result("ansa_entity_delete", {
        "entity_type": "MAT1", "deck": deck,
        "entities": [{"entity_id": material_id, "expected_fields": {"E": 210000.0}}],
    })
    _append_case(report, run_dir, "first_entity_unknown_fields_rejected_default_create_configure", {
        "unknown_field_task": unknown[1]["status"], "no_invalid_entity_created": True,
        "default_entity_id": material_id, "configured_fields": configured["fields"],
    })

    empty_grid = await h.direct_result("ansa_grid_create", {
        "position": [1.25, 2.5, 3.75],
        "deck": deck,
    })
    if empty_grid.get("coordinates") != [1.25, 2.5, 3.75]:
        raise AssertionError(f"空模型 GRID 创建失败：{empty_grid}")
    _append_case(report, run_dir, "empty_model_grid_create", {
        "grid_id": empty_grid["entity"]["id"],
        "coordinates": empty_grid["coordinates"],
    })

    open_backup = run_dir / ("empty-before-open-" + uuid.uuid4().hex[:8] + ".ansa")
    _submitted, open_task = await h.task("ansa_model_open", {
        "path": str(backup_path),
        "preservation": "backup",
        "backup_path": str(open_backup),
    })
    if not open_backup.is_file() or open_backup.stat().st_size == 0:
        raise AssertionError("Open 没有按默认 backup preservation 写入当前模型备份")
    restored = await h.snapshot_context()
    if not restored["model"].get("write_enabled"):
        raise AssertionError("Open 备份恢复后模型写入没有就绪")
    restored_grids = await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck})
    if restored_grids.get("count", 0) < 1:
        raise AssertionError("Open 没有恢复原始 baseline GRID")
    database = str(restored["model"].get("database", ""))
    try:
        same_path = Path(database).resolve() == backup_path.resolve()
    except (OSError, RuntimeError):
        same_path = False
    if not same_path:
        raise AssertionError(f"Open 后 DataBaseName 不是已恢复的 backup：{database!r}")
    _append_case(report, run_dir, "open_default_backup_restores_model", {
        "path": str(backup_path),
        "open_task": open_task["task_id"],
        "backup_written": str(open_backup),
        "restored_grid_count": restored_grids["count"],
        "epoch": h.model["epoch"],
    })
    report["gui_open_path"] = str(backup_path)
    report["core_last_epoch"] = h.model["epoch"]
    report["restored_database"] = database
    return backup_path, restored


async def _core_acceptance(h: McpHarness, report: dict, run_dir: Path, identity: dict,
                           stage_one, stage_two) -> None:
    schemas = await h.list_schema()
    _append_case(report, run_dir, "all_28_mcp_tools_have_input_output_schema", {
        "count": schemas["count"],
        "input_schemas": schemas["input_schemas"],
        "output_schemas": schemas["output_schemas"],
    })
    snapshot = await _bind_for_run(h, identity, stage_one, stage_two, identity["port"])
    if not snapshot["model"].get("write_enabled"):
        raise RuntimeError("初始独立模型写入门关闭")
    report["target"] = h.target
    report["initial_model"] = {
        "epoch": h.model["epoch"],
        "deck": snapshot["deck"],
        "database": snapshot["model"].get("database"),
        "write_enabled": snapshot["model"]["write_enabled"],
    }
    _append_case(report, run_dir, "owned_target_main_thread_write_gate", {
        "pid": snapshot["pid"],
        "instance_id": snapshot["instance_id"],
        "main_thread": snapshot["main_thread"],
        "write_enabled": snapshot["model"]["write_enabled"],
    })
    deck = snapshot["deck"]
    if not isinstance(deck, int):
        raise AssertionError(f"ANSA 当前 deck 不是整数：{deck!r}")

    probe = await _api_and_script_probe(h, report, run_dir)
    if deck != probe["current_deck"]:
        raise RuntimeError(
            f"ANSA snapshot deck 与主线程 API 探针不一致：{deck} != {probe['current_deck']}"
        )
    # GRID/inspection acceptance requires NASTRAN to be the current GUI deck.
    # Run the deck-4 CRUD probe first when applicable, then stop before sending
    # any request with a deck that differs from ANSA's current deck.
    if deck != probe["nastran_deck"]:
        raise RuntimeError(
            "自动核心验收需要当前 ANSA deck 为 NASTRAN；"
            f"当前 deck={deck}，NASTRAN={probe['nastran_deck']}。"
            "deck 4 NODE 专项（若字段可探查）已先行完成，请切换到 NASTRAN deck 后重新运行核心验收。"
        )
    await _generic_mat1(h, report, run_dir, deck=probe["nastran_deck"])
    await _grid_catalog(h, report, run_dir, deck=probe["nastran_deck"])
    await _summary_inventory(h, report, run_dir, deck=probe["nastran_deck"])
    await _script_cases(h, report, run_dir, deck=probe["nastran_deck"])
    await h.direct_result("ansa_script_execute", {
        "script": "from ansa import base, constants\nbase.SetCurrentDeck(constants.ABAQUS)\nRESULT = {'deck': base.CurrentDeck()}\n",
        "validate": True, "allow_model_change": True,
    })
    await h.snapshot_context()
    await _abaqus_node_crud(h, report, run_dir, {**probe, "current_deck": h.snapshot["deck"]})
    await h.direct_result("ansa_script_execute", {
        "script": "from ansa import base, constants\nbase.SetCurrentDeck(constants.NASTRAN)\nRESULT = {'deck': base.CurrentDeck()}\n",
        "validate": True, "allow_model_change": True,
    })
    await h.snapshot_context()
    report["unverified"] = [value for value in report.get("unverified", [])
                            if not value.startswith("ABAQUS deck 4 通用 NODE CRUD 未运行")]
    await _model_lifecycle(h, report, run_dir, deck=probe["nastran_deck"])
    report["core_complete"] = True
    report["identity"] = identity
    _json_write(run_dir / "report.json", report)


async def _wait_snapshot(h: McpHarness, predicate, description: str, timeout: int = PANEL_WAIT_SECONDS) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await h.call("ansa_model_snapshot", {"expected_target": h.target}, allow_error=True)
        if last.get("ok") and isinstance(last.get("data"), dict):
            data = last["data"]
            if predicate(data):
                h.snapshot = data
                h.model = {"epoch": data["model"]["epoch"]}
                return data
        await asyncio.sleep(0.5)
    raise TimeoutError(f"等待 {description} 超时；最后观测={last}")


def _snapshot_ui(snapshot: dict, stage: str) -> dict:
    ui = snapshot.get("ui")
    if not isinstance(ui, dict):
        raise AssertionError(f"{stage}: ansa_model_snapshot 缺少 ui 对象：{snapshot!r}")
    required = {
        "alive", "visible", "error", "close_reason", "session_id", "timer_id",
        "paint_count", "last_paint_tick",
    }
    missing = sorted(required - ui.keys())
    if missing:
        raise AssertionError(f"{stage}: snapshot.ui 缺少字段 {missing}: {ui!r}")
    if ui["error"]:
        raise RuntimeError(f"{stage}: 面板报告错误：{ui['error']}")
    if not isinstance(ui["alive"], bool) or not isinstance(ui["visible"], bool):
        raise AssertionError(f"{stage}: ui.alive/ui.visible 不是布尔值：{ui!r}")
    if ui["session_id"] is None or ui["timer_id"] is None:
        raise AssertionError(f"{stage}: snapshot.ui 缺少 session_id/timer_id：{ui!r}")
    for key in ("paint_count", "last_paint_tick"):
        if isinstance(ui[key], bool) or not isinstance(ui[key], int) or ui[key] < 0:
            raise AssertionError(f"{stage}: ui.{key} 不是非负整数：{ui!r}")
    return ui


def _assert_ui_identity(ui: dict, session_id, timer_id, stage: str) -> None:
    if ui["session_id"] != session_id or ui["timer_id"] != timer_id:
        raise AssertionError(
            f"{stage}: session/timer 改变：session={ui['session_id']!r}, timer={ui['timer_id']!r}"
        )


async def _wait_ui_snapshot(h: McpHarness, predicate, description: str,
                            timeout: int = PANEL_WAIT_SECONDS) -> tuple[dict, dict]:
    def matches(snapshot):
        ui = _snapshot_ui(snapshot, description)
        return predicate(snapshot, ui)

    snapshot = await _wait_snapshot(h, matches, description, timeout=timeout)
    return snapshot, _snapshot_ui(snapshot, description)


async def _show_panel_mcp(h: McpHarness, run_dir: Path, window_file: str, stage: str,
                          session_id, timer_id) -> tuple[dict, dict, dict]:
    _write_ui_stage(
        run_dir, stage, window_file,
        expected="snapshot.ui.alive=true, visible=true, paint_count>=2, and error is empty",
        instructions="脚本通过 MCP 提交面板显示请求，并轮询 snapshot.ui；面板出现后按 ui_instruction.json 操作。",
    )
    submitted = await h.call("ansa_panel_show", {"expected_target": h.target})
    snapshot, ui = await _wait_ui_snapshot(
        h, lambda _value, state: state["alive"] and state["visible"] and state["paint_count"] >= 2,
        f"{stage} 面板可见且至少刷新两次", timeout=PANEL_WAIT_SECONDS,
    )
    _assert_ui_identity(ui, session_id, timer_id, stage)
    return submitted, snapshot, ui


async def _wait_panel_closed(h: McpHarness, session_id, timer_id, description: str
                             ) -> tuple[dict, dict]:
    allowed = {"x", "accept", "reject", "returned"}
    snapshot, ui = await _wait_ui_snapshot(
        h,
        lambda _value, state: not state["alive"] and state["close_reason"] in allowed,
        description, timeout=PANEL_WAIT_SECONDS,
    )
    if ui["visible"]:
        raise AssertionError(f"{description}: 面板已关闭但 snapshot.ui.visible 仍为 true：{ui!r}")
    _assert_ui_identity(ui, session_id, timer_id, description)
    return snapshot, ui


async def _gui_acceptance(h: McpHarness, report: dict, run_dir: Path, identity: dict) -> None:
    window_file = Path(report.get("gui_open_path") or report.get("restored_database", "model.ansa")).name
    baseline = await h.snapshot_context()
    baseline_ui = _snapshot_ui(baseline, "GUI 验收开始")
    session_id, timer_id = baseline_ui["session_id"], baseline_ui["timer_id"]
    if baseline_ui["alive"] or baseline_ui["visible"]:
        raise RuntimeError("GUI 阶段开始时面板已打开；请先关闭该面板并重新运行 GUI-only")

    # Cache a read-only model count before Stop, when MCP tasks are still accepted.
    before_count = (await h.direct_result("ansa_entity_count", {
        "entity_type": "GRID", "deck": int(baseline["deck"]),
    }))["count"]
    shown, _shown_snapshot, shown_ui = await _show_panel_mcp(
        h, run_dir, window_file, "show_four_page_panel", session_id, timer_id,
    )

    ui_instructions = {
        "window_file": window_file,
        "window_title_rule": "使用实际模型窗口文件名；不要使用 PID 作为标题",
        "panel_control": "所有面板开关与状态观察均通过 MCP；面板打开时不执行 gRPC 调用",
        "stages": [
            "在 ANSA MCP Plugin 面板点击 Stop（停止接收新任务）；脚本会观察 stopped=true 并验证新写任务被拒绝",
            "随后点击 Start（恢复接收）；脚本会观察 stopped=false，并比较 Stop 前后的 GRID 数量",
            "按提示关闭小型 ANSA MCP Plugin 面板；脚本通过 MCP snapshot.ui 确认关闭原因和 session/timer",
            "脚本经 MCP 热开面板一次；面板出现后按提示关闭，随后经 MCP 执行 script_echo",
            "最后在该模型窗口通过 GUI Open 当前文件并处理提示；脚本等待 MCP snapshot 的 model epoch 增加，并核对 UI 状态",
        ],
        "same_path_to_open": report.get("gui_open_path"),
        "wait_each_phase_seconds": PANEL_WAIT_SECONDS,
    }
    _json_write(run_dir / "ui_instruction.json", ui_instructions)
    _write_ui_stage(run_dir, "manual_stop", window_file,
                    expected="snapshot.stopped=true; snapshot.ui.alive=true",
                    instructions="手动点击面板 Stop；等待脚本确认拒绝写任务后，再点击 Start。")
    stopped, stopped_ui = await _wait_ui_snapshot(
        h, lambda value, ui: value.get("stopped") is True and ui["alive"] and ui["visible"],
        "面板 Stop",
    )
    _assert_ui_identity(stopped_ui, session_id, timer_id, "面板 Stop")
    rejection = await h.call("ansa_script_execute", {
        "operation_id": "stage-three-panel-stop-" + uuid.uuid4().hex,
        "expected_target": h.target,
        "expected_model": h.model,
        "script": "RESULT = {'must_not_run': True}",
        "validate": True,
    }, allow_error=True)
    if rejection.get("status") != "failed" or rejection.get("task_id") is not None:
        raise AssertionError(f"Stop 后新写任务没有在入队前被拒绝：{rejection}")
    _append_case(report, run_dir, "panel_stop_rejects_new_write", {
        "stopped": stopped["stopped"],
        "task_id": rejection.get("task_id"),
        "rejected_before_enqueue": True,
    })

    _write_ui_stage(run_dir, "manual_start", window_file,
                    expected="snapshot.stopped=false; snapshot.ui.alive=true",
                    instructions="手动点击面板 Start；脚本会等待任务接收恢复。")
    resumed, resumed_ui = await _wait_ui_snapshot(
        h, lambda value, ui: value.get("stopped") is False and ui["alive"] and ui["visible"],
        "面板 Start",
    )
    _assert_ui_identity(resumed_ui, session_id, timer_id, "面板 Start")
    if resumed.get("pid") != identity["pid"]:
        raise AssertionError("Stop/Start 后 ANSA 目标进程发生变化")
    after_count = (await h.direct_result("ansa_entity_count", {
        "entity_type": "GRID", "deck": int(resumed["deck"]),
    }))["count"]
    if after_count != before_count:
        raise AssertionError("Stop 拒绝写任务期间 GRID 数量发生变化")
    _append_case(report, run_dir, "panel_start_restores_admission_same_session", {
        "session_id_unchanged": True,
        "timer_id_unchanged": True,
        "grid_count_unchanged": True,
    })

    _write_ui_stage(run_dir, "close_panel_first", window_file,
                    expected="snapshot.ui.alive=false with an allowed close_reason",
                    instructions="请关闭小型 ANSA MCP Plugin 面板。")
    closed, closed_ui = await _wait_panel_closed(
        h, session_id, timer_id, "等待首次关闭面板",
    )
    _append_case(report, run_dir, "four_page_panel_shown_on_owned_gui", {
        "window_file": window_file,
        "show_status": shown.get("status"),
        "visible": shown_ui["visible"],
        "paint_count": shown_ui["paint_count"],
        "last_paint_tick": shown_ui["last_paint_tick"],
        "observed_via": "ansa_model_snapshot.ui over MCP",
    })
    _append_case(report, run_dir, "panel_close_keeps_session_and_timer", {
        "alive": closed_ui["alive"],
        "close_reason": closed_ui["close_reason"],
        "session_id_unchanged": True,
        "timer_id_unchanged": True,
        "observed_via": "ansa_model_snapshot.ui over MCP",
    })

    hot, _hot_snapshot, hot_ui = await _show_panel_mcp(
        h, run_dir, window_file, "hot_reopen_panel", session_id, timer_id,
    )
    _write_ui_stage(run_dir, "close_hot_reopened_panel", window_file,
                    expected="snapshot.ui.alive=false with an allowed close_reason",
                    instructions="热开面板已显示；请关闭小型 ANSA MCP Plugin 面板。")
    hot_closed, hot_closed_ui = await _wait_panel_closed(
        h, session_id, timer_id, "等待热开后关闭面板",
    )
    if hot_closed.get("connected") is not True:
        raise AssertionError("关闭热开面板后 TCP backend 连接没有保持")
    echo = await h.direct_result("ansa_script_execute", {
        "script": "import threading\nRESULT = {'echo': 'stage-three', 'main_thread': threading.current_thread() is threading.main_thread()}\n",
        "validate": True,
    })
    if echo.get("result") != {"echo": "stage-three", "main_thread": True}:
        raise AssertionError(f"热开面板关闭后 script_echo 未成功：{echo}")
    _append_case(report, run_dir, "hot_reopen_close_then_mcp_script_echo", {
        "show_status": hot.get("status"),
        "close_reason": hot_closed_ui["close_reason"],
        "alive_after_close": hot_closed_ui["alive"],
        "session_id_unchanged": True,
        "timer_id_unchanged": True,
        "script_echo": echo["result"],
    })

    previous_epoch = h.model["epoch"]
    same_path = str(report["gui_open_path"])
    _write_ui_stage(run_dir, "manual_same_path_open", window_file,
                    path=same_path,
                    previous_epoch=previous_epoch,
                    instructions="在模型窗口中通过 GUI Open 再次打开 path 指定的同一个文件；处理 ANSA 提示。")
    _json_write(run_dir / "ui_instruction.json", {
        **ui_instructions,
        "current_stage": "manual_same_path_open",
        "same_path_to_open": same_path,
        "expected_epoch_greater_than": previous_epoch,
    })
    gui_snapshot = await _wait_snapshot(
        h,
        lambda value: value.get("model", {}).get("epoch", 0) > previous_epoch,
        "GUI 同路径 Open 的 model epoch 增加",
    )
    final_ui = _snapshot_ui(gui_snapshot, "GUI 同路径 Open")
    if final_ui["alive"] or final_ui["visible"] or final_ui["close_reason"] not in {"x", "accept", "reject", "returned"}:
        raise AssertionError(f"GUI 同路径 Open 后面板状态不符合关闭要求：{final_ui!r}")
    _assert_ui_identity(final_ui, session_id, timer_id, "GUI 同路径 Open")
    _append_case(report, run_dir, "manual_gui_same_path_open_epoch_observed", {
        "path": same_path,
        "epoch_before": previous_epoch,
        "epoch_after": gui_snapshot["model"]["epoch"],
        "panel_alive": final_ui["alive"],
        "panel_close_reason": final_ui["close_reason"],
        "session_id_unchanged": True,
        "timer_id_unchanged": True,
        "observed_via": "ansa_model_snapshot polling; no completion marker",
    })
    report["gui_complete"] = True
    _json_write(run_dir / "report.json", report)


def _cleanup_owned_ansa(identity: dict, instructions: dict, helper_bundle) -> dict:
    import ansa
    import grpc
    stage_one, stage_two, original_channel = helper_bundle
    port = int(identity["port"])
    translator = Path(instructions["translator"]).resolve()
    checked = stage_two.rpc(port, stage_one.remote_basic)
    _verify_program_arguments(checked, translator, port)
    if checked["pid"] != identity["pid"] or checked["program_args"] != identity["program_args"]:
        raise RuntimeError("清理前 ANSA PID/完整启动参数与本运行目录不一致，拒绝停止")
    source = str(PROJECT_ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from ansa_mcp_shared.identity import process_start_filetime, same_process_alive
    process_start = identity.get("process_start") or process_start_filetime(identity["pid"])
    if not same_process_alive(identity["pid"], process_start):
        raise RuntimeError("清理前已核验的 ANSA 进程身份发生变化，拒绝停止")
    connection = ansa.connect(host="127.0.0.1", port=port)
    try:
        final_check = ansa.execute_function(stage_one.remote_basic)
        _verify_program_arguments(final_check, translator, port)
        if final_check["pid"] != identity["pid"] or final_check["program_args"] != identity["program_args"]:
            raise RuntimeError("Stop 前复核 PID/参数失败，拒绝停止")
        ansa.stop(connection)
        connection = None
    finally:
        if connection is not None:
            ansa.disconnect(connection)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not same_process_alive(identity["pid"], process_start):
            break
        time.sleep(0.25)
    else:
        grpc.insecure_channel = original_channel
        raise RuntimeError("已核验的 ANSA 进程在 Stop 后仍存活")
    grpc.insecure_channel = original_channel
    return {"pid": identity["pid"], "stopped_owned_process": True}


async def _run_async(args: argparse.Namespace, run_dir: Path, instructions: dict,
                     report: dict, identity: dict, helper_bundle, *, gui_only: bool,
                     core_only: bool) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stage_one, stage_two, _original_channel = helper_bundle
    home = Path(instructions["runtime_home"])
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(home)],
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    log_name = "gui-backend.stderr.log" if gui_only else "backend.stderr.log"
    with (run_dir / log_name).open("w", encoding="utf-8") as error_log:
        async with stdio_client(parameters, errlog=error_log) as (reader, writer):
            async with ClientSession(
                reader,
                writer,
                read_timeout_seconds=timedelta(seconds=30),
            ) as client:
                await client.initialize()
                harness = McpHarness(client, report, run_dir)
                if args.panel_only:
                    await harness.list_schema()
                    await _bind_for_run(harness, identity, stage_one, stage_two, identity["port"])
                    report["panel_only"] = True
                    report["target"] = harness.target
                    report["gui_open_path"] = identity["seed_path"]
                elif not gui_only:
                    await _core_acceptance(harness, report, run_dir, identity, stage_one, stage_two)
                else:
                    if not report.get("core_complete"):
                        raise RuntimeError("--gui-only 要求 report.json 中 core_complete=true")
                    await harness.list_schema()
                    snapshot = await _bind_for_run(
                        harness, identity, stage_one, stage_two, identity["port"], gui_only=True,
                    )
                    expected_epoch = report.get("core_last_epoch")
                    if expected_epoch is not None and snapshot["model"]["epoch"] != expected_epoch:
                        raise RuntimeError(
                            f"GUI-only 开始时模型 epoch 已变化：{snapshot['model']['epoch']} != {expected_epoch}"
                        )
                if core_only:
                    _clear_prior_failure(report)
                    report["success"] = True
                    report["gui_pending"] = True
                    report["status"] = "core_complete_gui_pending"
                    _json_write(run_dir / "report.json", report)
                    return
                await _gui_acceptance(harness, report, run_dir, identity)
                _clear_prior_failure(report)
                report["success"] = True
                report["gui_pending"] = False
                report["status"] = "complete"
                _json_write(run_dir / "report.json", report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--port", type=int, default=None,
                        help="准备阶段的专属 gRPC 端口；省略时自动选择可用回环端口")
    parser.add_argument("--run-dir", type=Path,
                        help="准备阶段生成的 runtime/verification/stage-three-* 目录")
    parser.add_argument("--keep-owned-ansa", action="store_true",
                        help="验收成功后保留独立 ANSA，供可见面板检查")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare-only", action="store_true",
                       help="仅生成启动与 UI 指引；不会启动 ANSA")
    modes.add_argument("--core-only", action="store_true",
                       help="运行自动核心验收并保留 GUI，之后可用 --gui-only 补验")
    modes.add_argument("--gui-only", action="store_true",
                       help="在已完成核心验收的同一 run-dir 中补做最后的 GUI 验收")
    modes.add_argument("--panel-only", action="store_true",
                       help="在独立进程仅验收面板与 GUI，不声称完成核心验收")
    args = parser.parse_args(argv)
    run_dir = None
    report = None
    try:
        if args.prepare_only or args.run_dir is None:
            return _prepare(args)
        run_dir, instructions = _load_run(args)
        report_path = run_dir / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if args.gui_only:
            identity = report.get("identity")
            if not isinstance(identity, dict):
                raise ValueError("report.json 中没有已核验的独立 ANSA identity")
            stage_one, stage_two = _import_runtime_helpers()
            import grpc
            import ansa
            original_channel = _bounded_grpc(grpc, stage_one.DeadlineInterceptor)
            # Recheck the existing process before starting backend access.
            basic = stage_two.rpc(identity["port"], stage_one.remote_basic)
            _verify_program_arguments(basic, Path(instructions["translator"]).resolve(), identity["port"])
            if basic["pid"] != identity["pid"] or basic["program_args"] != identity["program_args"]:
                raise RuntimeError("GUI-only 目标 PID/参数与核心验收记录不一致")
            helper_bundle = (stage_one, stage_two, original_channel)
        else:
            identity, _ansa, _grpc, helper_bundle = _prepare_owned_ansa(run_dir, instructions)
            report.update({
                "stage": "three",
                "run_dir": str(run_dir),
                "runtime_home": str(instructions["runtime_home"]),
                "ansa_root": str(instructions["ansa_root"]),
                "identity": identity,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "cases": report.get("cases", []),
                "unverified": report.get("unverified", []),
                "success": False,
            })
            _json_write(report_path, report)

        asyncio.run(_run_async(
            args, run_dir, instructions, report, identity, helper_bundle,
            gui_only=args.gui_only,
            core_only=args.core_only,
        ))

        if report.get("success") and not args.core_only and args.keep_owned_ansa:
            report["cleanup"] = {"pid": identity["pid"], "stopped_owned_process": False,
                                 "kept_for_visible_review": True}
            _json_write(report_path, report)
        elif report.get("success") and not args.core_only:
            try:
                report["cleanup"] = _cleanup_owned_ansa(identity, instructions, helper_bundle)
                report["cleanup"]["pid_and_args_rechecked"] = True
            except Exception as exc:
                report["cleanup"] = {
                    "stopped_owned_process": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                _record_failure(report, run_dir, exc)
            _json_write(report_path, report)

        print(json.dumps({
            "report": str(report_path),
            "success": bool(report.get("success")),
            "core_complete": bool(report.get("core_complete")),
            "gui_complete": bool(report.get("gui_complete")),
            "cases": len(report.get("cases", [])),
            "unverified": report.get("unverified", []),
            "cleanup": report.get("cleanup"),
            "error": report.get("fatal_error"),
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if report.get("success") else 1
    except Exception as exc:
        run = run_dir or (args.run_dir.expanduser().resolve() if args.run_dir else None)
        report_path = run / "report.json" if run and run.is_dir() else None
        if report_path and report is None and report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
        error_log = None
        if run and isinstance(report, dict):
            try:
                _record_failure(report, run, exc)
                error_log = str(run / "error.log")
            except Exception:
                pass
        print(json.dumps({
            "success": False,
            "run_dir": str(run) if run else None,
            "report": str(report_path) if report_path else None,
            "error": f"{type(exc).__name__}: {exc}",
            "exception_tree": _exception_tree(exc),
            "error_log": error_log,
        }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
