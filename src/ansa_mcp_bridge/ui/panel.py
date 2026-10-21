"""ANSA 原生状态面板；面板只读状态并排入控制请求，不直接执行模型操作。"""

from __future__ import annotations

import json
import threading
import time
from itertools import islice

from ansa_mcp_shared import PROTOCOL_VERSION


_WINDOW_TITLE = "ANSA MCP Plugin"
_JOURNAL_LIMIT = 20
_BACKEND_TASK_LIMIT = 50
_OVERVIEW_STALE_SECONDS = 30
_IDENTITY_FIELDS = ("pid", "process_start", "instance_id", "session_id")
# 桥接执行器 Timer 的默认周期；只在设置页作为说明显示。
_TIMER_DEFAULT_MS = 100
# 面板刷新只走窗口自己的 BCTimer：控件的 Python 句柄只在面板窗口的回调上下文里可用。
_REFRESH_MS = 500
GUI_HANDLES = {"native_panels": []}


def _short(value, limit=180):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact(value, secret):
    text = "" if value is None else str(value)
    if isinstance(secret, str) and secret:
        text = text.replace(secret, "[已隐藏]")
    return _short(text)


def _identity_text(identity):
    if not isinstance(identity, dict):
        return "未知"
    return "PID {pid} / 实例 {instance} / 会话 {session}".format(
        pid=identity.get("pid", "未知"),
        instance=identity.get("instance_id", "未知"),
        session=identity.get("session_id", "未知"),
    )


def _authorization_text(authorization, secret):
    """仅显示授权状态与非凭据标识，绝不格式化完整授权对象。"""
    if not isinstance(authorization, dict):
        return "未授权"
    if not authorization.get("active"):
        return "未授权"
    values = ["已授权"]
    backend_id = authorization.get("backend_id")
    generation = authorization.get("generation")
    if backend_id:
        values.append("后端 " + _redact(backend_id, secret))
    if generation is not None:
        values.append("代次 " + str(generation))
    return "；".join(values)


def _write_status(model):
    if model.get("write_enabled_confirmed") is False:
        if model.get("snapshot_cached"):
            if model.get("write_tools"):
                return "质量操作执行前校验"
            return "执行前校验" if model.get("write_enabled") else "最近检查不支持"
        return "待模型识别"
    if not model.get("write_enabled") and model.get("write_tools"):
        return "仅网格质量定位／修复"
    return "开启" if model.get("write_enabled") else "关闭"


def _model_text(model, version=None, deck=None, secret=None):
    if not isinstance(model, dict):
        return "模型状态未知"
    values = ["ANSA " + _redact(version or "未知", secret)]
    if deck is not None:
        values.append("Deck " + _redact(deck, secret))
    if model.get("database"):
        values.append("模型 " + _redact(model["database"], secret))
    if model.get("epoch") is not None:
        values.append("代次 " + str(model["epoch"]))
    values.append("受控写入 " + _write_status(model))
    if model.get("detection"):
        values.append("检测 " + _redact(model["detection"], secret))
    return "；".join(values)


def _target_model_text(target, local_identity, snapshot, secret):
    target_model = target.get("model") if isinstance(target, dict) else None
    target_deck = target.get("deck") if isinstance(target, dict) else None
    target_version = target.get("version") if isinstance(target, dict) else None
    same_session = isinstance(target, dict) and all(
        target.get(key) == local_identity.get(key)
        for key in ("pid", "instance_id", "session_id")
    )
    if same_session:
        return _model_text(
            snapshot.get("model"), snapshot.get("version"), snapshot.get("deck"), secret
        )
    if isinstance(target_model, dict):
        return _model_text(target_model, target_version, target_deck, secret) + "（最近已确认快照）"
    if target:
        return "目标实例未发布模型详情；本面板只读取本实例模型"
    return "无已授权目标"


def _target_authorization_text(target, local_identity, snapshot, secret):
    if not isinstance(target, dict):
        return "无已授权目标"
    authorization = target.get("authorization")
    same_session = all(
        target.get(key) == local_identity.get(key)
        for key in ("pid", "instance_id", "session_id")
    )
    if same_session:
        authorization = snapshot.get("authorization")
    if isinstance(authorization, dict):
        return _authorization_text(authorization, secret)
    state = target.get("state", "active")
    generation = target.get("generation")
    detail = "后端绑定状态 " + _redact(state, secret)
    if generation is not None:
        detail += "；代次 " + str(generation)
    return detail + "；详细授权仅在目标实例本地显示"


def _task_items(value):
    if isinstance(value, dict):
        return value.values(), len(value)
    if isinstance(value, (list, tuple)):
        return value, len(value)
    return None, 0


def _backend_tasks_text(status, secret):
    tasks, count = _task_items(status.get("tasks"))
    if tasks is None:
        return "后端任务摘要：当前状态未提供 tasks 字段"
    if count == 0:
        return "后端任务摘要：无待处理或待确认任务"
    lines = ["后端任务摘要（最多显示 {0} 项）：".format(_BACKEND_TASK_LIMIT)]
    for record in islice(tasks, _BACKEND_TASK_LIMIT):
        if not isinstance(record, dict):
            lines.append("• 未知任务记录")
            continue
        task = record.get("task")
        task = task if isinstance(task, dict) else record
        state = _redact(record.get("state", "unknown"), secret)
        operation_id = task.get("operation_id", record.get("operation_id", "-"))
        tool = task.get("tool", record.get("tool", "-"))
        task_id = record.get("task_id", task.get("task_id", "-"))
        dispatch = record.get("dispatch")
        cancel_pending = bool(record.get("cancel_pending"))
        flags = []
        if state == "unknown":
            flags.append("执行结果未知")
        if cancel_pending:
            flags.append("取消待确认")
        if dispatch:
            flags.append("派发 " + _redact(dispatch, secret))
        suffix = "；" + "；".join(flags) if flags else ""
        lines.append(
            "• {state} / {tool} / task {task_id} / op {operation_id}{suffix}".format(
                state=state,
                tool=_redact(tool, secret),
                task_id=_redact(str(task_id)[:16], secret),
                operation_id=_redact(str(operation_id)[:20], secret),
                suffix=suffix,
            )
        )
    if count > _BACKEND_TASK_LIMIT:
        lines.append("• 其余 {0} 项未显示".format(count - _BACKEND_TASK_LIMIT))
    return "\n".join(lines)


def _journal_text(records, secret):
    if not records:
        return "本实例 Journal：暂无任务记录"
    lines = ["本实例 Journal（最近 {0} 项）：".format(len(records))]
    for record in records:
        task = record.get("task") if isinstance(record, dict) else None
        task = task if isinstance(task, dict) else {}
        created = record.get("created_at")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)) if created else "时间未知"
        error = record.get("error")
        line = "• {when} / {state} / {tool} / task {task_id} / op {operation_id}".format(
            when=when,
            state=_redact(record.get("state", "unknown"), secret),
            tool=_redact(task.get("tool", "-"), secret),
            task_id=_redact(str(record.get("task_id", "-"))[:16], secret),
            operation_id=_redact(str(task.get("operation_id", "-"))[:20], secret),
        )
        if record.get("acked"):
            line += " / 已确认"
        if error:
            line += " / 错误：" + _redact(error, secret)
        lines.append(line)
    return "\n".join(lines)


def _append_error(lines, label, value, secret):
    if value:
        lines.append(label + _redact(value, secret))


def _diagnostic_value(value, secret):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if "token" in name.lower() or "secret" in name.lower():
                result[name] = "[已隐藏]"
            else:
                result[name] = _diagnostic_value(item, secret)
        return result
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[已隐藏]") if isinstance(secret, str) and secret else value
    return value


def _diagnostic_json(value, secret):
    return json.dumps(_diagnostic_value(value, secret), ensure_ascii=False, sort_keys=True, default=str)


def _full_redact(value, secret):
    text = "未知" if value is None else str(value)
    if isinstance(secret, str) and secret:
        text = text.replace(secret, "[已隐藏]")
    return text


def _model_name(model):
    if not isinstance(model, dict):
        return "尚未读取"
    database = model.get("database")
    if isinstance(database, str) and not database.strip():
        return "未命名模型"
    if not isinstance(database, str):
        return "尚未读取"
    filename = database.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    return filename or "未知（文件名不可用）"


def _complete_identity(identity):
    return isinstance(identity, dict) and all(
        identity.get(key) is not None and identity.get(key) != ""
        for key in _IDENTITY_FIELDS
    )


def _same_identity(left, right):
    return _complete_identity(left) and _complete_identity(right) and all(
        left.get(key) == right.get(key) for key in _IDENTITY_FIELDS
    )


def _target_model_details(status, target):
    target_model = target.get("model") if isinstance(target, dict) else None
    if not isinstance(target_model, dict):
        target_model = status.get("active_model")
    instance = None
    instances = status.get("instances")
    if isinstance(instances, (list, tuple)) and isinstance(target, dict):
        instance = next(
            (item for item in instances if _same_identity(item, target)), None
        )
    if not isinstance(target_model, dict) and isinstance(instance, dict):
        target_model = instance.get("model")
    if not isinstance(target_model, dict):
        target_model = {}
    deck_name = None
    for source in (target, target_model, instance, status):
        if isinstance(source, dict):
            deck_name = source.get("deck_name") or source.get("solver_name")
            if deck_name:
                break
    deck = target.get("deck") if isinstance(target, dict) else None
    if deck is None and isinstance(instance, dict):
        deck = instance.get("deck")
    return target_model, deck, deck_name


def _overview_data(bridge, snapshot):
    """Build user-facing overview values from bridge state and an existing snapshot."""
    state = bridge.state
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    model = snapshot.get("model") if isinstance(snapshot.get("model"), dict) else None
    status = state.backend_status if isinstance(state.backend_status, dict) else {}
    target = status.get("active_target")
    target = target if isinstance(target, dict) else None
    connected_event = getattr(state, "connected", None)
    connected = bool(connected_event and connected_event.is_set())
    stopped_event = getattr(state, "stopped", None)
    stopped = bool(stopped_event and stopped_event.is_set())
    backend_id = getattr(state, "connected_backend_id", None)
    current_identity = state.identity()

    observed_at = getattr(state, "cached_probe_at", None)
    observed_timestamp = None
    try:
        observed_timestamp = float(observed_at) if observed_at is not None else None
        age = max(0.0, time.time() - observed_timestamp) if observed_timestamp is not None else None
    except (TypeError, ValueError, OverflowError):
        age = None
    cached_backend = getattr(state, "cached_probe_backend_id", None)
    backend_changed = cached_backend != backend_id
    old_connection = cached_backend is not None and (not connected or backend_changed)
    startup_probe = getattr(state, "startup_probe", {})
    startup_probe = startup_probe if isinstance(startup_probe, dict) else {}
    probe_backend = startup_probe.get("backend_id")
    try:
        cached_probe_monotonic = float(getattr(state, "cached_probe_monotonic", None))
        probe_started_at = float(startup_probe.get("started_at"))
        cache_matches_round = cached_probe_monotonic >= probe_started_at
    except (TypeError, ValueError, OverflowError):
        cache_matches_round = False
    current_round_matches = bool(
        connected
        and backend_id
        and probe_backend == backend_id
        and cached_backend == backend_id
        and cache_matches_round
    )
    if age is None:
        updated_label = "最近读取：时间未知"
    else:
        read_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(observed_timestamp))
        updated_label = "最近读取：" + read_time
    if old_connection:
        updated_label += "（来自旧连接，需重新读取）"
    elif connected and not current_round_matches:
        updated_label += "（尚未读取当前连接）"
    if age is not None and age > _OVERVIEW_STALE_SECONDS:
        updated_label += "（模型缓存较旧，需重新读取）"

    local_authorization = snapshot.get("authorization")
    target_label = "尚未连接" if not connected or target is None else "目标待确认"
    verified_target = False
    if connected and target is not None:
        target_backend = target.get("backend_id")
        target_generation = target.get("generation")
        status_backend = status.get("backend_id")
        snapshot_identity = {key: snapshot.get(key) for key in _IDENTITY_FIELDS}
        target_auth = target.get("authorization")
        target_auth_matches = target_auth is None or (
            isinstance(target_auth, dict)
            and target_auth.get("active") is True
            and target_auth.get("backend_id") == backend_id
            and target_auth.get("generation") == target_generation
        )
        target_model, target_deck, _ = _target_model_details(status, target)
        target_is_local = _same_identity(target, current_identity)
        backend_matches = bool(
            backend_id
            and target_backend == backend_id
            and (status_backend is None or status_backend == backend_id)
        )
        local_target_matches = True
        if target_is_local:
            auth_matches = (
                isinstance(local_authorization, dict)
                and local_authorization.get("active") is True
                and local_authorization.get("backend_id") == backend_id
                and local_authorization.get("generation") == target_generation
            )
            local_epoch = model.get("epoch") if model else None
            target_epoch = target_model.get("epoch")
            same_model_generation = (
                target_epoch is None or local_epoch is None or target_epoch == local_epoch
            )
            same_deck = (
                target_deck is None or snapshot.get("deck") is None
                or target_deck == snapshot.get("deck")
            )
            local_target_matches = auth_matches and same_model_generation and same_deck
        verified_target = bool(
            current_round_matches
            and backend_matches
            and target.get("state") == "active"
            and target_generation is not None
            and target_auth_matches
            and _same_identity(snapshot_identity, current_identity)
            and _complete_identity(target)
            and local_target_matches
        )
        if verified_target:
            target_label = "此窗口" if _same_identity(target, current_identity) else "其他窗口"

    other_target = None
    if verified_target and target_label == "其他窗口":
        target_model, _, target_solver = _target_model_details(status, target)
        pid = target.get("pid")
        other_model_name = _model_name(target_model)
        local_model_name = _model_name(model)
        same_model_name = (
            not other_model_name.startswith("未知")
            and other_model_name.casefold() == local_model_name.casefold()
        )
        window_label = (
            "其他窗口（PID {0}）".format(pid)
            if same_model_name and pid is not None else "其他窗口"
        )
        other_target = {
            "model_name": other_model_name,
            "solver_name": str(target_solver) if target_solver else "未知",
            "window_label": window_label,
        }

    journal = getattr(state, "journal", None)
    persistence_blocked = bool(getattr(journal, "blocked", False))
    dispatch_blocked = status.get("dispatch_blocked") is True
    startup_probe = getattr(state, "startup_probe", {})
    probe_state = startup_probe.get("state") if isinstance(startup_probe, dict) else None
    if stopped:
        task_label = "已暂停"
        next_step = "点击“恢复任务”重新开始任务接收"
    elif not connected:
        task_label = "待连接"
        next_step = "启动 MCP 后端，并在目标窗口点击“连接此窗口”"
    elif persistence_blocked or dispatch_blocked:
        task_label = "任务受阻"
        next_step = "查看诊断中的持久化或派发错误并排除后再提交任务"
    elif probe_state in ("pending", "retrying") or model is None:
        task_label = "等待模型"
        next_step = "等待模型自动读取完成；失败时查看诊断中的读取错误"
    elif target_label == "此窗口":
        task_label = "等待任务"
        next_step = "可在 AI 客户端提交任务；执行前会重新校验目标与模型"
    elif target_label == "其他窗口":
        task_label = "等待任务"
        next_step = "核对其他窗口及目标模型后，再向 AI 客户端提交任务"
    elif target_label == "目标待确认":
        if target is None:
            task_label = "待连接"
            next_step = "在目标窗口点击“连接此窗口”"
        else:
            task_label = "等待目标确认"
            next_step = "在目标窗口点击“连接此窗口”，并重新读取模型"
    else:
        task_label = "待连接"
        next_step = "在目标窗口点击“连接此窗口”"

    return {
        "model_name": _model_name(model),
        "solver_name": str(snapshot.get("deck_name") or "未知"),
        "target_label": target_label,
        "task_label": task_label,
        "next_step": next_step,
        "updated_label": updated_label,
        "other_target": other_target,
    }


def _render(bridge, widgets, snapshot=None):
    state = bridge.state
    state.assert_main()
    if snapshot is None:
        snapshot = state.probe()
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    status = state.backend_status if isinstance(state.backend_status, dict) else {}
    secret = getattr(bridge, "token", None)
    identity = state.identity()
    model = snapshot.get("model") if isinstance(snapshot, dict) else None
    target = status.get("active_target")
    if not isinstance(target, dict):
        target = None
    target_model, target_deck, target_deck_name = (
        _target_model_details(status, target) if target else ({}, None, None)
    )
    target_identity = {key: target.get(key) for key in _IDENTITY_FIELDS} if target else None
    target_authorization = {
        "authorization": target.get("authorization"),
        "backend_id": target.get("backend_id"),
        "generation": target.get("generation"),
        "state": target.get("state"),
    } if target else None
    overview_data = _overview_data(bridge, snapshot)
    overview = [
        "任务状态：" + overview_data["task_label"],
        "AI 操作目标：" + overview_data["target_label"],
        "当前模型：" + overview_data["model_name"],
        "求解器：" + overview_data["solver_name"],
    ]
    if overview_data["other_target"] is not None:
        other = overview_data["other_target"]
        overview.append(
            "目标模型：{0}（{1}，{2}）".format(
                other["model_name"], other["solver_name"], other["window_label"]
            )
        )
    overview.extend([
        "下一步：" + overview_data["next_step"],
        overview_data["updated_label"],
    ])

    journal_error = None
    try:
        recent = state.journal.recent(_JOURNAL_LIMIT)
    except Exception as exc:
        recent = []
        journal_error = str(exc)
    commands = [_journal_text(recent, secret), "", _backend_tasks_text(status, secret)]
    _append_error(commands, "Journal 读取错误：", journal_error, secret)
    dispatch_blocked = status.get("dispatch_blocked")
    if dispatch_blocked is not None:
        commands.extend(["", "后端派发：" + ("已阻断" if dispatch_blocked else "正常")])

    current_thread = threading.get_ident()
    main_thread = threading.main_thread()
    diagnostics = [
        "连接：" + ("在线" if state.connected.is_set() else "离线"),
        "连接的后端：" + _redact(state.connected_backend_id or "无", secret),
        "ANSA 主线程：{0}（当前回调 {1}）".format(
            "一致" if current_thread == state.main_thread_id == main_thread.ident else "不一致",
            current_thread,
        ),
        "任务接收：" + ("Stop，拒绝新任务" if state.stopped.is_set() else "Start，可受理"),
        "执行记录持久化：" + ("故障，保护性停止新任务" if state.journal.blocked else "正常"),
    ]
    if dispatch_blocked is not None:
        diagnostics.append("后端派发持久化：" + ("已阻断" if dispatch_blocked else "正常"))
    try:
        state.check_conflict()
        diagnostics.append("旧版 MCP-Sum 冲突：未检测到")
    except RuntimeError as exc:
        diagnostics.append("旧版 MCP-Sum 冲突：检测到 / " + _redact(exc, secret))
    except Exception as exc:
        diagnostics.append("旧版 MCP-Sum 冲突：无法确认 / " + _redact(exc, secret))
    diagnostics.extend([
        "",
        "当前窗口完整身份：" + _diagnostic_json(identity, secret),
        "当前快照身份：" + _diagnostic_json(
            {key: snapshot.get(key) for key in _IDENTITY_FIELDS}, secret
        ),
        "当前窗口授权：" + _diagnostic_json(snapshot.get("authorization"), secret),
        "当前模型完整路径：" + _full_redact((model or {}).get("database"), secret),
        "当前模型 deck：" + _full_redact(snapshot.get("deck"), secret)
        + "（" + _full_redact(snapshot.get("deck_name"), secret) + "）",
        "当前模型代次：" + _full_redact((model or {}).get("epoch"), secret),
        "当前模型更替检测：" + _full_redact((model or {}).get("detection"), secret),
        "模型保护范围：" + _full_redact((model or {}).get("write_scope"), secret),
        "最近探测的写入门禁（任务仍会执行前校验）：" + _write_status(model or {}),
        "当前后端：" + _diagnostic_json(
            {"connected_backend_id": state.connected_backend_id,
             "status_backend_id": status.get("backend_id")}, secret
        ),
        "后端目标完整身份：" + _diagnostic_json(target_identity, secret),
        "后端目标授权与绑定：" + _diagnostic_json(target_authorization, secret),
        "后端目标模型：" + (_diagnostic_json(target_model, secret) if target else "无"),
        "后端目标 ANSA 版本：" + _full_redact(target.get("version") if target else None, secret),
        "后端目标 deck：" + _full_redact(target_deck, secret)
        + "（" + _full_redact(target_deck_name, secret) + ")",
        "令牌：始终隐藏",
        "最近错误（历史记录）：",
    ])
    errors = []
    _append_error(errors, "本实例：", getattr(state, "last_error", None), secret)
    _append_error(errors, "后端：", status.get("error"), secret)
    startup_probe = getattr(state, "startup_probe", {})
    if isinstance(startup_probe, dict):
        _append_error(errors, "启动模型读取：", startup_probe.get("error"), secret)
    diagnostics.extend(errors or ["无"])
    if (model or {}).get("snapshot_age_seconds") is not None:
        diagnostics.append("模型信息最近读取：{0} 秒前（执行前重新校验）".format(model["snapshot_age_seconds"]))

    settings = [
        "实际 ANSA runtime_home：" + _redact(bridge.runtime_home, secret),
        "运行目录：只读；此面板不提供热切换",
        "协议版本：" + str(PROTOCOL_VERSION),
        "ANSA 版本：" + _redact(snapshot.get("version", "未知"), secret),
        "桥接版本：" + _redact(getattr(bridge, "version", "运行时未声明"), secret),
        "面板刷新：窗口自身 BCTimer {0} ms；控件只在面板窗口回调上下文更新".format(_REFRESH_MS),
        "桥接执行器 Timer：约 {0} ms；面板不使用它触碰 GUI 控件".format(_TIMER_DEFAULT_MS),
    ]

    return {
        "overview": "\n".join(overview),
        "commands": "\n".join(commands),
        "diagnostics": "\n".join(diagnostics),
        "settings": "\n".join(settings),
    }


def close_panel(bridge):
    """请求窗口自身的回调关闭面板；BCShow 返回后才确认可释放桥接。"""
    bridge.state.assert_main()
    if getattr(bridge, "_native_panel_context", None) is not None:
        from .native_panel import close_panel as close_native_panel
        return close_native_panel(bridge)
    context = getattr(bridge, "_panel_close_context", None)
    if context is None:
        context = getattr(bridge, "_panel_context", None)
    if not isinstance(context, dict):
        if getattr(bridge, "panel", None) is not None or getattr(bridge.state, "panel_info", {}).get("alive"):
            raise RuntimeError("Cannot confirm the owned panel lifecycle")
        return True
    if context.get("show_returned"):
        # BCOnExitDestroy 的 BCShow 已返回：不再把旧控件交给任何 GUI API。
        context["detach"]("bridge_close")
        bridge._panel_close_context = None
        return True
    context["close_requested"] = True
    bridge._panel_close_context = context
    return False


def show_panel(bridge, persistent=False):
    """显示四页面板；热开时复用现有窗口；周期刷新由窗口自己的 BCTimer 承担。"""
    from ansa import guitk

    state = bridge.state
    state.assert_main()
    if getattr(bridge, "_native_panel_enabled", False) or getattr(bridge, "_native_panel_context", None) is not None:
        from .native_panel import show_panel as show_native_panel
        return show_native_panel(bridge)
    existing = getattr(bridge, "_panel_context", None)
    if isinstance(existing, dict) and existing.get("alive"):
        try:
            if not existing["window_alive"]():
                # 窗口确实没了：丢掉旧引用后照常重建，别让这次调用白跑。
                existing["detach"]("destroyed")
                existing = None
            else:
                guitk.BCSetVisible(existing["window"], True)
                bridge.panel = existing["window"]
                return existing["window"]
        except Exception as exc:
            state.last_error = "Panel reopen failed: " + str(exc)
            return None

    constants = guitk.constants
    window = None
    context = {"alive": True, "window": None, "shown": False, "visible": False, "widgets": {},
               "last_values": {}, "timer": None, "refresh_busy": False, "refresh": None,
               "detach": None, "window_alive": None, "close_requested": False, "show_returned": False}
    info = {"alive": True, "visible": False, "error": None, "close_reason": None,
            "session_id": state.session_id, "timer_id": bridge.timer._timer_id,
            "paint_count": 0, "last_paint_tick": 0}
    state.panel_info = info

    def detach(reason):
        context["alive"] = False
        info.update(alive=False, visible=False, close_reason=reason)
        if state.panel_refresh is context["refresh"]:
            state.panel_refresh = None
        if getattr(bridge, "_panel_context", None) is context:
            bridge._panel_context = None
        if getattr(bridge, "panel", None) is context["window"]:
            bridge.panel = None
        # 只丢引用：窗口及其子定时器可能已被 ANSA 或用户销毁，旧句柄不能再交给 guitk。
        context["timer"] = None

    context["detach"] = detach

    def stop_refresh_timer():
        timer = context.get("timer")
        if timer is not None:
            guitk.BCTimerStop(timer)

    def close_window(_window, _data):
        stop_refresh_timer()
        detach("x")
        return 0

    def accept_window(_window, _data):
        stop_refresh_timer()
        detach("accept")
        return 1

    def reject_window(_window, _data):
        stop_refresh_timer()
        detach("reject")
        return 1

    def window_alive():
        """仅在存活窗口的回调里读取可见性；关闭后由 BCShow 返回证明销毁。"""
        try:
            context["visible"] = bool(guitk.BCIsVisible(context["window"]))
        except Exception:
            context["visible"] = False
            return False
        return context["visible"]

    context["window_alive"] = window_alive

    def refresh():
        """在面板窗口自己的回调上下文里更新控件；返回面板是否仍然存活。"""
        if not context["alive"]:
            return False
        if context["shown"] and not window_alive():
            # ANSA 的 New/Open 会清理脚本窗口，用户关闭也会销毁控件；此后旧句柄
            # 一律不可用，只能丢掉引用，等下一次 show_panel 重建。
            detach("destroyed")
            return False
        try:
            rendered = _render(bridge, context["widgets"])
        except Exception as exc:
            # 读状态失败与控件无关：保留面板，下一次定时器再试。
            state.last_error = "Panel render failed: " + str(exc)
            info.update(error=state.last_error)
            return False
        failure = None
        for page, value in rendered.items():
            if context["last_values"].get(page) == value:
                continue
            widget = context["widgets"].get(page)
            if widget is None:
                continue
            try:
                guitk.BCTextEditSetText(widget, value)
            except Exception as exc:
                failure = failure or str(exc)
                continue
            context["last_values"][page] = value
        if failure:
            state.last_error = "Panel refresh failed: " + failure
            info.update(error=state.last_error)
            # 控件写失败说明这批句柄已经不可用；同样丢引用，重开时重建。
            detach("error")
            return False
        if isinstance(state.last_error, str) and state.last_error.startswith("Panel "):
            state.last_error = None
        info.update(visible=context["visible"], error=None,
                    paint_count=info["paint_count"] + 1, last_paint_tick=bridge.executor.ticks)
        return True

    context["refresh"] = refresh

    def on_refresh_timer(_timer, _data):
        """窗口内 BCTimer 回调：控件更新只能发生在这个上下文里。"""
        if not context["alive"] or context["show_returned"]:
            return 0
        if context["close_requested"]:
            try:
                guitk.BCWindowReject(context["window"])
            except Exception as exc:
                state.last_error = "Panel close failed: " + str(exc)
                info["error"] = state.last_error
            return 0
        if not context["alive"] or context["refresh_busy"]:
            return 0
        context["refresh_busy"] = True
        try:
            refresh()
        except Exception as exc:
            state.last_error = "Panel refresh failed: " + str(exc)
        finally:
            context["refresh_busy"] = False
        return 0

    def request_bind(_button, _data):
        try:
            bridge.transport.request_bind()
        except Exception as exc:
            state.last_error = "Bind request could not be queued: " + str(exc)
        return 0

    def stop(_button, _data):
        state.stopped.set()
        refresh()
        return 0

    def start(_button, _data):
        state.stopped.clear()
        refresh()
        return 0

    try:
        window = guitk.BCWindowCreate(_WINDOW_TITLE,
            constants.BCOnExitHide if persistent else constants.BCOnExitDestroy)
        guitk.BCWindowSetModal(window, False)
        guitk.BCWindowSetInitSize(window, 880, 620)
        root = guitk.BCBoxLayoutCreate(window, constants.BCVertical)
        tabs = guitk.BCTabWidgetCreate(root)
        page_specs = (
            ("overview", "概览"),
            ("commands", "命令"),
            ("diagnostics", "诊断"),
            ("settings", "设置"),
        )
        first_page = None
        for key, title in page_specs:
            page = guitk.BCFrameCreate(tabs)
            if first_page is None:
                first_page = page
            layout = guitk.BCBoxLayoutCreate(page, constants.BCVertical)
            text_edit = guitk.BCTextEditCreate(layout, "正在读取状态…")
            guitk.BCTextEditSetReadOnly(text_edit, True)
            context["widgets"][key] = text_edit
            guitk.BCTabWidgetAddTab(tabs, page, title)
        guitk.BCTabWidgetSetCurrentTab(tabs, first_page)

        controls = guitk.BCBoxLayoutCreate(root, constants.BCHorizontal)
        guitk.BCPushButtonCreate(controls, "连接 / 切换到此 ANSA", request_bind, None)
        guitk.BCPushButtonCreate(controls, "Stop（停止接收新任务）", stop, None)
        guitk.BCPushButtonCreate(controls, "Start（恢复接收）", start, None)
        guitk.BCWindowSetOnCloseFunction(window, close_window, None)
        guitk.BCWindowSetAcceptFunction(window, accept_window, None)
        guitk.BCWindowSetRejectFunction(window, reject_window, None)

        context["window"] = window
        bridge._panel_context = context
        # panel_refresh 只是“面板刷新回调已登记”的存活凭据：桥接执行器不得调用它，
        # 否则会在没有 GUI 窗口上下文的 ctypes 定时器回调里触碰控件句柄。
        state.panel_refresh = refresh
        if not refresh():
            return None
        def size_after_show(_data):
            if context["alive"]:
                guitk.BCWindowSetSize(window, 1600, 1000)
            return 0
        guitk.BCTimerSingleShot(0, size_after_show, None)
        if isinstance(state.last_error, str) and state.last_error.startswith("Panel "):
            state.last_error = None
        bridge.panel = window
        # 周期刷新用窗口自己的 BCTimer：控件句柄只在面板窗口的回调上下文里有效。
        # 桥接执行器的 Win32 Timer 与无父窗口的 BCTimerSingleShot 都曾被 ANSA 以
        # “Parameter 1 is of wrong type” 拒绝，所以面板不再借用它们刷新。
        timer = guitk.BCTimerCreate(window)
        guitk.BCTimerSetTimeoutFunction(timer, on_refresh_timer, None)
        guitk.BCTimerStart(timer, _REFRESH_MS, False)
        context["timer"] = timer
        context["shown"] = True
        if persistent:
            # 与现有原生插件一致：ANSA Ribbon 脚本退出后仍保存隐藏窗口的控件引用。
            # 当前 26.1 原生脚本在 Destroy 后 GC 曾发生访问违规；MCP 路径保持原行为。
            GUI_HANDLES["native_panels"].append(context)
        # 原生队列可能保留晚到回调；C++ 不保证持有 Python 强引用。
        # 使用不会随开发包重载而移除的进程锚，回调关闭后均由 alive 守卫变为惰性。
        import ctypes
        retained = getattr(ctypes, "_ansa_mcp_plugin_gui_callbacks", None)
        if retained is None:
            retained = []
            ctypes._ansa_mcp_plugin_gui_callbacks = retained
        retained.append((close_window, accept_window, reject_window, on_refresh_timer,
                         size_after_show, request_bind, stop, start))
        # 这里进入 ANSA 原生 GUI 回调：面板的控件在 BCShow 返回前一直存在。
        try:
            guitk.BCShow(window)
        finally:
            context["show_returned"] = True
            if context["alive"]:
                detach("returned")
        return None
    except Exception as exc:
        context["alive"] = False
        if state.panel_refresh is refresh:
            state.panel_refresh = None
        if getattr(bridge, "_panel_context", None) is context:
            bridge._panel_context = None
        if getattr(bridge, "panel", None) is window:
            bridge.panel = None
        state.last_error = "Panel creation failed: " + str(exc)
        info.update(alive=False, visible=False, error=state.last_error)
        return None
