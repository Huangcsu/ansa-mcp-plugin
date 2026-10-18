"""ANSA 原生状态面板；面板只读状态并排入控制请求，不直接执行模型操作。"""

from __future__ import annotations

import threading
import time
from itertools import islice

from ansa_mcp_shared import PROTOCOL_VERSION


_WINDOW_TITLE = "ANSA MCP Plugin"
_JOURNAL_LIMIT = 20
_BACKEND_TASK_LIMIT = 50
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
        return "待后端确认"
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


def _render(bridge, widgets, snapshot=None):
    state = bridge.state
    state.assert_main()
    if snapshot is None:
        snapshot = state.probe()
    status = state.backend_status if isinstance(state.backend_status, dict) else {}
    secret = getattr(bridge, "token", None)
    identity = state.identity()
    model = snapshot.get("model") if isinstance(snapshot, dict) else None
    target = status.get("active_target")
    if not isinstance(target, dict):
        target = None

    overview = [
        "本实例",
        "进程：" + _identity_text(identity),
        "进程启动标识：" + _redact(identity.get("process_start"), secret),
        "模型：" + _model_text(model, snapshot.get("version"), snapshot.get("deck"), secret),
        "授权：" + _authorization_text(snapshot.get("authorization"), secret),
        "",
        "AI 当前目标",
    ]
    if target:
        overview.extend([
            "进程：" + _identity_text(target),
            "后端：" + _redact(target.get("backend_id", "未知"), secret),
            "绑定代次 / 状态：{0} / {1}".format(
                target.get("generation", "未知"), _redact(target.get("state", "active"), secret)
            ),
            "模型：" + _target_model_text(target, identity, snapshot, secret),
            "授权：" + _target_authorization_text(target, identity, snapshot, secret),
        ])
    else:
        overview.extend(["状态：未绑定", "模型：无已授权目标", "授权：未授权"])

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
        "保护范围：" + _redact((model or {}).get("write_scope", "未知"), secret),
        "模型更替检测：" + _redact((model or {}).get("detection", "未知"), secret),
        "受控写入：" + _write_status(model or {}),
        "令牌：始终隐藏",
        "最近错误：",
    ])
    errors = []
    _append_error(errors, "本实例：", getattr(state, "last_error", None), secret)
    _append_error(errors, "后端：", status.get("error"), secret)
    diagnostics.extend(errors or ["无"])

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
