"""Native ANSA control panel with callbacks kept at module scope."""

from __future__ import annotations

from typing import Any
import time
from html import escape
import textwrap


_WINDOW_TITLE = "ANSA MCP Plugin"
_PANEL_KEY = "native_panel"
_REFRESH_MS = 500
_WINDOW_WIDTH = 1000
_WINDOW_HEIGHT = 720

# Only the currently executing BCShow owns native widgets. A Ribbon script's
# exit destroys its windows, including BCOnExitHide windows; never reuse them.
GUI_HANDLES: dict[str, Any] = {_PANEL_KEY: None}


def _guitk():
    from ansa import guitk

    return guitk


def _require_window_lookup(guitk):
    missing = [name for name in ("BCGetTopLevelWidget", "BCGetTopWidget")
               if not callable(getattr(guitk, name, None))]
    if missing:
        raise RuntimeError("ANSA 原生面板缺少窗口查找能力：" + ", ".join(missing))


def _registered_window(context, guitk):
    """Use the registry only as a string existence check, never as a BCWindow handle."""
    _require_window_lookup(guitk)
    proxy = guitk.BCGetTopLevelWidget(_WINDOW_TITLE)
    if proxy is None:
        return None
    tabs = context.get("tabs")
    if tabs is None:
        raise RuntimeError("ANSA 已登记原生面板，但缺少本模块的 Tabs 句柄")
    window = guitk.BCGetTopWidget(tabs)
    if window is None:
        raise RuntimeError("无法从原生面板 Tabs 获取有效的 BCWindow 句柄")
    context["window"] = window
    return window


def _info(context):
    return context["info"]


def _kernel_timer_id(bridge):
    return getattr(getattr(bridge, "timer", None), "_timer_id", None)


def _attach(bridge, context):
    context["bridge"] = bridge
    context["session_id"] = getattr(bridge.state, "session_id", None)
    context["info"].update(
        session_id=context["session_id"],
        timer_id=_kernel_timer_id(bridge),
        alive=True,
    )
    bridge._native_panel_context = context
    bridge._native_panel_enabled = True
    bridge.state.panel_info = context["info"]


def _detach(context):
    bridge = context.get("bridge")
    if bridge is not None and getattr(bridge, "_native_panel_context", None) is context:
        bridge._native_panel_context = None
    context["bridge"] = None


def _unbind_bridge(context, reason):
    """Detach this reusable window without leaving a stale bridge panel state."""
    bridge = context.get("bridge")
    if bridge is None:
        return
    state = bridge.state
    if hasattr(state, "panel_refresh"):
        state.panel_refresh = None
    current_info = getattr(state, "panel_info", None)
    if isinstance(current_info, dict):
        detached_info = dict(current_info)
        detached_info.update(alive=False, visible=False, close_reason=reason)
        state.panel_info = detached_info
    _detach(context)


def _forget_destroyed_context(context):
    """Drop handles after ANSA has removed the named window; never touch stale GUI objects."""
    context["visible"] = False
    context["alive"] = False
    context["timer_active"] = False
    context["close_requested"] = False
    context["info"].update(alive=False, visible=False, close_reason="destroyed")
    if context.get("show_returned"):
        _unbind_bridge(context, "destroyed")
        if GUI_HANDLES.get(_PANEL_KEY) is context:
            GUI_HANDLES[_PANEL_KEY] = None


def _stop_timer(context):
    if not context.get("timer_active"):
        return None
    try:
        _guitk().BCTimerStop(context["timer"])
        context["timer_active"] = False
        return None
    except Exception as exc:
        context["info"]["error"] = "面板刷新定时器停止失败：%s" % exc
        return exc


def _start_timer(context):
    if context.get("timer_active"):
        return
    timer_id = _guitk().BCTimerStart(context["timer"], _REFRESH_MS, False)
    context["timer_active"] = True
    context["refresh_timer_id"] = timer_id
    context["info"].update(timer_id=_kernel_timer_id(context["bridge"]), error=None)


def _cached_probe_is_current(state):
    if not state.connected.is_set() or not state.connected_backend_id:
        return False
    if getattr(state, "cached_probe_backend_id", None) != state.connected_backend_id:
        return False
    probe = getattr(state, "startup_probe", {})
    if not isinstance(probe, dict) or probe.get("backend_id") != state.connected_backend_id:
        return False
    started = probe.get("started_at")
    observed = getattr(state, "cached_probe_monotonic", None)
    return (isinstance(started, (int, float)) and isinstance(observed, (int, float))
            and observed >= started)


def _snapshot_from_cache(state):
    cached = getattr(state, "cached_probe", None)
    if isinstance(cached, dict):
        snapshot = dict(cached)
        snapshot["authorization"] = state.authorization
        cached_model = snapshot.get("model")
        if isinstance(cached_model, dict):
            model = dict(cached_model)
            model["write_enabled_confirmed"] = False
            model["detection"] = "缓存快照（实时写入状态由后端校验）"
            model["snapshot_cached"] = True
            observed_at = getattr(state, "cached_probe_at", None)
            if isinstance(observed_at, (int, float)):
                model["snapshot_age_seconds"] = max(0, int(time.time() - observed_at))
            if not _cached_probe_is_current(state):
                model["detection"] = "旧连接快照，等待重新读取"
        else:
            model = {
                "write_enabled": False,
                "write_enabled_confirmed": False,
                "detection": "等待自动读取模型状态",
            }
        snapshot["model"] = model
        return snapshot
    return {
        "model": {
            "write_enabled": False,
            "write_enabled_confirmed": False,
            "detection": "等待自动读取模型状态",
        },
        "authorization": state.authorization,
    }


def _refresh(context):
    bridge = context.get("bridge")
    if (not context.get("alive") or not context.get("visible")
            or bridge is None or context.get("close_requested")):
        return False
    state = bridge.state
    state.assert_main()
    snapshot = _snapshot_from_cache(state)
    from . import panel as core_panel

    rendered = core_panel._render(bridge, context["widgets"], snapshot=snapshot)
    guitk = _guitk()
    for key, value in rendered.items():
        widget = context["widgets"].get(key)
        if widget is not None:
            guitk.BCTextEditSetText(widget, value)
    overview = core_panel._overview_data(bridge, snapshot)
    context["info"]["overview_data"] = overview
    for key in ("model_name", "solver_name", "target_label", "task_label", "updated_label"):
        widget = context["overview_widgets"].get(key)
        if widget is not None:
            guitk.BCLabelSetText(widget, _label_text(overview.get(key, "未知")))
    other = overview.get("other_target")
    guitk.BCSetVisible(context["other_target_frame"], isinstance(other, dict))
    if isinstance(other, dict):
        for key in ("model_name", "solver_name", "window_label"):
            guitk.BCLabelSetText(context["overview_widgets"]["other_" + key], _label_text(other.get(key, "未知")))
    _refresh_controls(context, guitk)
    context["paint_count"] += 1
    context["last_paint_tick"] = getattr(bridge.executor, "ticks", 0)
    context["info"].update(
        alive=True,
        visible=True,
        error=None,
        session_id=context["session_id"],
        timer_id=_kernel_timer_id(bridge),
        paint_count=context["paint_count"],
        last_paint_tick=context["last_paint_tick"],
    )
    return True


def _owns_target(state):
    status = state.backend_status if isinstance(state.backend_status, dict) else {}
    target = status.get("active_target")
    authorization = state.authorization
    return bool(
        state.connected.is_set() and isinstance(target, dict)
        and isinstance(authorization, dict) and authorization.get("active")
        and target.get("backend_id") == state.connected_backend_id == authorization.get("backend_id")
        and target.get("generation") == authorization.get("generation")
        and all(target.get(key) == value for key, value in state.identity().items())
    )


def _model_phase(state):
    if not state.connected.is_set():
        return "offline", "等待后端"
    cached = getattr(state, "cached_probe", None)
    if (isinstance(cached, dict) and isinstance(cached.get("model"), dict)
            and _cached_probe_is_current(state)):
        return "ready", "已识别"
    probe = getattr(state, "startup_probe", {})
    if not isinstance(probe, dict) or probe.get("backend_id") != state.connected_backend_id:
        return "pending", "正在读取"
    if probe.get("state") == "failed":
        return "failed", "读取失败"
    started = probe.get("started_at")
    if (probe.get("state") == "retrying"
            or isinstance(started, (int, float)) and time.monotonic() - started >= 5):
        return "waiting", "等待响应"
    return "pending", "正在读取"


def _refresh_controls(context, guitk):
    state = context["bridge"].state
    status = state.backend_status if isinstance(state.backend_status, dict) else {}
    connected = state.connected.is_set()
    connection_error = context.get("connection_error")
    if connected and connection_error:
        probe = getattr(state, "startup_probe", {})
        probe = probe if isinstance(probe, dict) else {}
        if (connection_error["backend_id"] != state.connected_backend_id
                or connection_error["started_at"] != probe.get("started_at")):
            if context.get("action") == connection_error["text"]:
                context["action"] = None
            if context["info"].get("error") == connection_error["text"]:
                context["info"]["error"] = None
            context["connection_error"] = None
    pending = context.get("bind_pending")
    if pending:
        if not state.connected.is_set() or state.connected_backend_id != pending["backend_id"]:
            context["action"] = "连接结果未确认：后端已断开；重新连接后请检查当前目标。"
            probe = getattr(state, "startup_probe", {})
            probe = probe if isinstance(probe, dict) else {}
            context["connection_error"] = {
                "backend_id": state.connected_backend_id,
                "started_at": probe.get("started_at"), "text": context["action"],
            }
            context["bind_pending"] = None
        elif _owns_target(state):
            context["action"] = "连接成功：AI 当前目标已绑定到此 ANSA。"
            context["bind_pending"] = None
        elif status is not pending["prior_status"] and status.get("error"):
            from .panel import _redact
            context["action"] = "连接失败：" + _redact(status["error"], getattr(context["bridge"], "token", None))
            context["bind_pending"] = None
        elif time.monotonic() - pending["started"] >= 10:
            context["action"] = "连接结果尚未确认：请查看诊断日志和当前目标；请求不会自动重试。"
            context["bind_pending"] = None
    own_target = _owns_target(state)
    phase, model_status = _model_phase(state)
    connection = "已连接到此窗口" if own_target else "等待连接此窗口" if connected else "等待后端"
    if state.stopped.is_set():
        admission = "已暂停"
    elif not connected:
        admission = "等待连接"
    elif phase != "ready":
        admission = "等待模型"
    else:
        admission = "可受理" if own_target else "待绑定"
    text = "连接：{0}  |  模型：{1}  |  任务：{2}".format(connection, model_status, admission)
    if not connected:
        default_action = "请在 AI 客户端启用 ANSA MCP Plugin，然后点击“连接此窗口”。"
    elif phase == "ready":
        overview = context["info"].get("overview_data", {})
        default_action = overview.get("next_step") or (
            "AI 正在操作此窗口；可在 AI 客户端发送任务。" if own_target
            else "模型已识别，请点击“连接此窗口”。")
    elif phase == "failed":
        default_action = "模型状态读取失败，请查看诊断日志；点击连接可重新确认。"
    elif phase == "waiting":
        default_action = "正在等待 ANSA 响应；如有对话框，请先处理，或等待当前操作完成。"
    else:
        default_action = "正在自动读取模型状态，无需先让 AI 调用查询工具。"
    action = context.get("action") or default_action
    context["info"].update(control_status=text, last_action=action)
    guitk.BCLabelSetText(context["control_status"], text)
    guitk.BCLabelSetText(context["action_status"], _label_text(action))


def _button_bridge(context):
    if not isinstance(context, dict) or not context.get("alive") or not context.get("visible") or context.get("close_requested"):
        return None
    return context.get("bridge")


def _button_error(context, message, exc):
    from .panel import _redact
    bridge = context.get("bridge")
    error = message + _redact(exc, getattr(bridge, "token", None))
    context["action"] = error
    context["info"]["error"] = error
    if bridge is not None:
        bridge.state.last_error = error
        if isinstance(exc, ConnectionError):
            probe = getattr(bridge.state, "startup_probe", {})
            probe = probe if isinstance(probe, dict) else {}
            context["connection_error"] = {
                "backend_id": bridge.state.connected_backend_id,
                "started_at": probe.get("started_at"), "text": error,
            }
    # 直接保留反馈，后续状态刷新不会抹去按钮错误。
    if context.get("action_status") is not None:
        _guitk().BCLabelSetText(context["action_status"], _label_text(error))


def _hide_context(context, reason):
    # Stop first while the native window callback still owns valid GUI handles.
    error = _stop_timer(context)
    context["alive"] = False
    context["visible"] = False
    context["info"].update(alive=False, visible=False, close_reason=reason)
    if error is not None:
        context["info"]["error"] = "面板刷新定时器停止失败：%s" % error
    # Keep the owner until BCShow returns, including ordinary X/Reject paths.
    # A shutdown arriving during callback unwind must still wait for it.


def _on_timer(timer, context):
    """Window-owned BCTimer callback; data is the stable context, never a closure."""
    try:
        if not isinstance(context, dict) or not context.get("alive"):
            return 0
        if not context.get("visible"):
            return 0
        guitk = _guitk()
        window = _registered_window(context, guitk)
        if window is None:
            _forget_destroyed_context(context)
            return 0
        if context.get("close_requested"):
            _stop_timer(context)
            guitk.BCWindowReject(window)
            if context.get("visible"):
                _hide_context(context, "bridge_close")
            return 0
        if not context.get("size_applied"):
            guitk.BCWindowSetSize(window, _WINDOW_WIDTH, _WINDOW_HEIGHT)
            guitk.BCWindowSetPosition(window, 40, 80)
            context["size_applied"] = True
        _refresh(context)
    except Exception as exc:
        if isinstance(context, dict):
            context["info"]["error"] = "面板刷新失败：%s" % exc
        print("[ANSA MCP Plugin] native panel timer failed: %s" % exc)
    return 0


def _on_window_close(window, context):
    try:
        _hide_context(context, "x")
    except Exception as exc:
        context["info"]["error"] = "关闭面板失败：%s" % exc
        print("[ANSA MCP Plugin] native panel close failed: %s" % exc)
    return 0


def _on_window_accept(window, context):
    try:
        _hide_context(context, "accept")
    except Exception as exc:
        context["info"]["error"] = "关闭面板失败：%s" % exc
        print("[ANSA MCP Plugin] native panel accept failed: %s" % exc)
    return 1


def _on_window_reject(window, context):
    try:
        reason = "bridge_close" if context.get("close_requested") else "reject"
        _hide_context(context, reason)
    except Exception as exc:
        context["info"]["error"] = "关闭面板失败：%s" % exc
        print("[ANSA MCP Plugin] native panel reject failed: %s" % exc)
    return 1


def _on_bind(button, context):
    try:
        bridge = _button_bridge(context)
        if bridge is not None:
            if context.get("bind_pending"):
                return 0
            bridge.transport.request_bind()
            context["bind_pending"] = {
                "started": time.monotonic(),
                "backend_id": bridge.state.connected_backend_id,
                "prior_status": bridge.state.backend_status,
            }
            context["action"] = "连接请求已发送，正在等待后端确认…"
            _refresh(context)
    except Exception as exc:
        _button_error(context, "连接未完成：", exc)
    return 0


def _on_stop(button, context):
    try:
        bridge = _button_bridge(context)
        if bridge is not None:
            bridge.state.stopped.set()
            context["action"] = "已暂停任务接收和队列执行；正在执行的任务不会被强制中断。"
            _refresh(context)
    except Exception as exc:
        _button_error(context, "暂停任务失败：", exc)
    return 0


def _on_start(button, context):
    try:
        bridge = _button_bridge(context)
        if bridge is not None:
            bridge.state.stopped.clear()
            context["action"] = "已恢复任务接收和队列执行。"
            if not bridge.state.connected.is_set():
                context["action"] += "MCP 后端仍未连接。"
            _refresh(context)
    except Exception as exc:
        _button_error(context, "恢复任务失败：", exc)
    return 0


def _label_text(value):
    """在原生标签内安全显示并换行，避免长名称撑宽窗口。"""
    lines = []
    for line in str(value).replace("\r", "").split("\n"):
        lines.extend(textwrap.wrap(line, width=48, replace_whitespace=False) or [""])
    return "<span>" + "<br>".join(escape(line) for line in lines) + "</span>"


def _overview_grid(parent, rows, context, guitk, prefix=""):
    grid = guitk.BCGridLayoutCreate(parent)
    guitk.BCGridLayoutSetMargin(grid, 8)
    guitk.BCGridLayoutSetSpacing(grid, 12)
    guitk.BCGridLayoutSetColStretch(grid, 1, 1)
    for row, (key, title) in enumerate(rows):
        label = guitk.BCLabelCreate(grid, title)
        value = guitk.BCLabelCreate(grid, "正在读取…")
        guitk.BCGridLayoutAddWidget(grid, label, row, 0, guitk.constants.BCAlignLeft)
        guitk.BCGridLayoutAddWidget(grid, value, row, 1, guitk.constants.BCAlignLeft)
        context["overview_widgets"][prefix + key] = value


def _on_diagnostics(_button, context):
    try:
        if _button_bridge(context) is not None:
            _guitk().BCTabWidgetSetCurrentTab(context["tabs"], context["pages"]["diagnostics"])
    except Exception as exc:
        _button_error(context, "无法打开诊断详情：", exc)
    return 0


def _build_overview(layout, context, guitk):
    current = guitk.BCFrameCreate(layout)
    body = guitk.BCBoxLayoutCreate(current, guitk.constants.BCVertical)
    guitk.BCLabelCreate(body, "<b>当前模型</b>")
    _overview_grid(body, (
        ("model_name", "模型名称"), ("solver_name", "求解器"),
        ("target_label", "AI 操作目标"), ("task_label", "任务状态"),
        ("updated_label", "模型信息"),
    ), context, guitk)
    other = guitk.BCFrameCreate(layout)
    context["other_target_frame"] = other
    body = guitk.BCBoxLayoutCreate(other, guitk.constants.BCVertical)
    guitk.BCLabelCreate(body, "<b>AI 当前操作的其他窗口</b>")
    _overview_grid(body, (
        ("model_name", "目标模型"), ("solver_name", "求解器"),
        ("window_label", "目标窗口"),
    ), context, guitk, "other_")
    guitk.BCSetVisible(other, False)
    guitk.BCLabelCreate(layout, "执行任务前会自动核对模型与操作范围。")
    guitk.BCSpacerCreate(layout)
    footer = guitk.BCBoxLayoutCreate(layout, guitk.constants.BCHorizontal)
    guitk.BCLabelCreate(footer, "完整路径、连接标识和错误详情可在诊断页查看。")
    context["buttons"]["diagnostics"] = guitk.BCPushButtonCreate(footer, "查看诊断详情", _on_diagnostics, context)


def _build_window(bridge, guitk):
    _require_window_lookup(guitk)
    if guitk.BCGetTopLevelWidget(_WINDOW_TITLE) is not None:
        raise RuntimeError("已存在同名 ANSA 原生窗口但缺少可复用上下文；拒绝重复创建")

    info = {
        "alive": True,
        "visible": False,
        "error": None,
        "close_reason": None,
        "session_id": getattr(bridge.state, "session_id", None),
        "timer_id": _kernel_timer_id(bridge),
        "paint_count": 0,
        "last_paint_tick": 0,
    }
    context = {
        "alive": True,
        "visible": False,
        "bridge": bridge,
        "session_id": info["session_id"],
        "close_requested": False,
        "show_returned": True,
        "timer_active": False,
        "refresh_timer_id": None,
        "paint_count": 0,
        "last_paint_tick": 0,
        "info": info,
        "widgets": {},
        "overview_widgets": {},
        "pages": {},
        "buttons": {},
        "window": None,
        "tabs": None,
        "timer": None,
        "size_applied": False,
        "action": None,
        "bind_pending": None,
    }
    GUI_HANDLES[_PANEL_KEY] = context
    bridge._native_panel_context = context
    bridge.state.panel_info = info

    window = guitk.BCWindowCreate(_WINDOW_TITLE, guitk.constants.BCOnExitDestroy)
    context["window"] = window
    guitk.BCWindowSetModal(window, False)
    guitk.BCWindowShowTitleBarButtons(window, guitk.constants.BCMinimizeButton |
                                    guitk.constants.BCMaximizeButton | guitk.constants.BCCloseButton)
    guitk.BCWindowSetOnCloseFunction(window, _on_window_close, context)
    guitk.BCWindowSetAcceptFunction(window, _on_window_accept, context)
    guitk.BCWindowSetRejectFunction(window, _on_window_reject, context)

    root = guitk.BCBoxLayoutCreate(window, guitk.constants.BCVertical)
    guitk.BCLabelCreate(root, "ANSA MCP Plugin · 本机控制面板")
    buttons = guitk.BCBoxLayoutCreate(root, guitk.constants.BCHorizontal)
    context["buttons"]["connect"] = guitk.BCPushButtonCreate(buttons, "连接此窗口", _on_bind, context)
    context["buttons"]["pause"] = guitk.BCPushButtonCreate(buttons, "暂停任务", _on_stop, context)
    context["buttons"]["resume"] = guitk.BCPushButtonCreate(buttons, "恢复任务", _on_start, context)
    context["control_status"] = guitk.BCLabelCreate(root, "正在读取连接和任务状态…")
    context["action_status"] = guitk.BCLabelCreate(root, "")

    tabs = guitk.BCTabWidgetCreate(root)
    context["tabs"] = tabs
    first_page = None
    for key, title in (
        ("overview", "概览"),
        ("commands", "命令记录"),
        ("diagnostics", "诊断日志"),
        ("settings", "设置"),
    ):
        page = guitk.BCFrameCreate(tabs)
        context["pages"][key] = page
        if first_page is None:
            first_page = page
        layout = guitk.BCBoxLayoutCreate(page, guitk.constants.BCVertical)
        if key == "overview":
            _build_overview(layout, context, guitk)
        else:
            text = guitk.BCTextEditCreate(layout, "正在读取状态…")
            guitk.BCTextEditSetReadOnly(text, True)
            guitk.BCTextEditSetWordWrap(text, guitk.constants.BCWidgetWidth)
            guitk.BCTextEditSetWrapPolicy(text, guitk.constants.BCWrapAtWordBoundaryOrAnywhere)
            context["widgets"][key] = text
        guitk.BCTabWidgetAddTab(tabs, page, title)
    guitk.BCTabWidgetSetCurrentTab(tabs, first_page)
    if callable(getattr(guitk, "BCWindowSetInitSize", None)):
        guitk.BCWindowSetInitSize(window, _WINDOW_WIDTH, _WINDOW_HEIGHT)

    timer = guitk.BCTimerCreate(window)
    context["timer"] = timer
    guitk.BCTimerSetTimeoutFunction(timer, _on_timer, context)
    # ANSA can retain a queued callback beyond script completion. Mirror the
    # proven MCP panel's process anchor: keep callbacks, data and Python handle
    # wrappers alive, while BCOnExitDestroy releases native window resources.
    import ctypes
    retained = getattr(ctypes, "_ansa_mcp_plugin_native_panel_callbacks", None)
    if retained is None:
        retained = []
        ctypes._ansa_mcp_plugin_native_panel_callbacks = retained
    retained.append((context, window, tabs, timer, dict(context["widgets"]),
                     context["control_status"], context["action_status"],
                     _on_timer, _on_window_close, _on_window_accept,
                     _on_window_reject, _on_bind, _on_stop, _on_start,
                     dict(context["overview_widgets"]), dict(context["pages"]),
                     context["other_target_frame"], _on_diagnostics, dict(context["buttons"])))
    return context


def show_panel(bridge):
    """Display one script-owned window; rebuild native widgets after it closes."""
    bridge.state.assert_main()
    guitk = _guitk()
    _require_window_lookup(guitk)
    context = GUI_HANDLES.get(_PANEL_KEY)
    if context is not None:
        if context.get("close_requested") or not context.get("visible"):
            raise RuntimeError("原生面板正在关闭，确认关闭后才能重新打开")
        if context.get("bridge") is not bridge:
            raise RuntimeError("原生面板仍绑定到另一个桥接会话")
        # A second Ribbon click must not enter a nested BCShow on the same GUI.
        return context["window"]

    context = _build_window(bridge, guitk)
    window = context["window"]

    _attach(bridge, context)
    context["alive"] = True
    context["visible"] = True
    context["close_requested"] = False
    context["show_returned"] = False
    context["info"].update(alive=True, visible=True, error=None, close_reason=None)
    if context.get("timer") is None:
        raise RuntimeError("原生面板缺少窗口自身 BCTimer")
    if not context.get("timer_active"):
        _start_timer(context)

    try:
        guitk.BCShow(window)
    finally:
        # Even if ANSA closed a script window without a close callback, BCShow
        # has now returned. Only update Python state: all native widgets may
        # already have been destroyed by Open/New or script cleanup.
        context["show_returned"] = True
        context["alive"] = False
        context["visible"] = False
        context["timer_active"] = False
        context["info"].update(alive=False, visible=False,
                               close_reason=context["info"].get("close_reason") or "returned")
        _unbind_bridge(context, context["info"]["close_reason"])
        if GUI_HANDLES.get(_PANEL_KEY) is context:
            GUI_HANDLES[_PANEL_KEY] = None
        for key in ("window", "tabs", "timer", "control_status", "action_status"):
            context[key] = None
        context["widgets"] = {}
        context["overview_widgets"] = {}
        context["pages"] = {}
        context["buttons"] = {}
        context["other_target_frame"] = None
    return None


def close_panel(bridge):
    """Request close from the panel timer; complete only after BCShow returned."""
    bridge.state.assert_main()
    context = getattr(bridge, "_native_panel_context", None)
    if not isinstance(context, dict) or context.get("bridge") is not bridge:
        return True

    if context.get("close_requested") and context.get("show_returned") and not context.get("visible"):
        context["info"].update(visible=False, close_reason="bridge_close")
        context["close_requested"] = False
        _unbind_bridge(context, "bridge_close")
        return True

    context["close_requested"] = True
    context["info"].update(alive=True, visible=bool(context.get("visible")),
                            close_reason="bridge_close_pending")
    if context.get("visible") and not context.get("timer_active"):
        _start_timer(context)
    return False
