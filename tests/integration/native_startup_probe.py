"""由 ANSA -execpy 运行的分层诊断；不读写模型，不代表 Ribbon 验收。"""

import builtins
import json
from pathlib import Path
import sys
import threading
import time


_RUN = None
_EVENTS = []
_TIMER = None
_TICKS = 0
_GUI = None


def _mark(stage, **fields):
    _EVENTS.append({"stage": stage, "time": time.time(), **fields})
    path = _RUN / "markers.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(_EVENTS, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _timer_tick():
    global _TICKS
    _TICKS += 1
    if _TICKS <= 3 or _TICKS == 10:
        _mark("timer_tick", ticks=_TICKS, main_thread=threading.current_thread() is threading.main_thread())
    if _TICKS == 10:
        _TIMER.close()
        _mark("timer_stopped", ticks=_TICKS)


def _gui_reject(window, context):
    from ansa import guitk
    guitk.BCTimerStop(context["timer"])
    _mark("gui_rejected")
    return 1


def _gui_tick(timer, context):
    from ansa import guitk
    context["ticks"] += 1
    _mark("gui_tick", ticks=context["ticks"])
    if context["ticks"] >= 2:
        guitk.BCTimerStop(timer)
        guitk.BCWindowReject(context["window"])
    return 0


def _show_minimal_gui():
    global _GUI
    from ansa import guitk
    context = {"ticks": 0}
    _GUI = context
    builtins._ansa_mcp_isolation_gui = context
    window = guitk.BCWindowCreate("MCP startup isolation", guitk.constants.BCOnExitHide)
    context["window"] = window
    guitk.BCWindowSetModal(window, False)
    guitk.BCLabelCreate(window, "原生生命周期诊断：约 1 秒后自动关闭")
    guitk.BCWindowSetRejectFunction(window, _gui_reject, context)
    timer = guitk.BCTimerCreate(window)
    context["timer"] = timer
    guitk.BCTimerSetTimeoutFunction(timer, _gui_tick, context)
    guitk.BCTimerStart(timer, 500, False)
    _mark("gui_before_show")
    guitk.BCShow(window)
    _mark("gui_closed")


def _request_native_close(bridge):
    context = getattr(bridge, "_native_panel_context", None)
    _mark("native_close_requested", ui=dict(bridge.state.panel_info))
    if context is not None:
        context["close_requested"] = True
    return 0


def _request_entry_close(data):
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    if bridge is not None:
        _request_native_close(bridge)
    else:
        _mark("entry_bridge_missing")
    return 0


def NativeEntryReturned():
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    _mark("entry_returned", ui=dict(bridge.state.panel_info) if bridge else None)


def NativeEntryReloaded():
    from ansa import guitk
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    _mark("entry_reloaded", session_id=bridge.state.session_id if bridge else None)
    guitk.BCTimerSingleShot(1600, _request_entry_close, None)


def NativeEntryStopped():
    _mark("entry_stopped", bridge_present=getattr(builtins, "_ansa_mcp_plugin_bridge", None) is not None)


def NativeStartupProbe(run_dir, case):
    global _RUN, _TIMER
    from ansa import session
    run = Path(run_dir).resolve()
    params = json.loads((run / "params.json").read_text(encoding="utf-8"))
    if case != params["case"]:
        raise ValueError("Diagnostic case mismatch")
    arguments = [str(arg).replace("\\", "/") for arg in session.ProgramArguments()]
    translator = str(run / "isolated_transl.py").replace("\\", "/")
    if translator not in arguments or not threading.current_thread() is threading.main_thread():
        raise RuntimeError("Not the uniquely owned diagnostic ANSA main thread")
    _RUN = run
    _mark("function_entered", case=case, main_thread=True)
    try:
        if case in {"timer_only", "bridge_only", "combined", "native_panel"}:
            sys.path.insert(0, params["source_root"] + "/src")
        if case == "timer_only":
            from ansa_mcp_bridge.platform.timer import MainThreadTimer
            _TIMER = MainThreadTimer(_timer_tick)
            builtins._ansa_mcp_isolation_timer = _TIMER
            _mark("start_finished", timer_id=_TIMER._timer_id)
        elif case in {"bridge_only", "combined", "native_panel"}:
            from ansa_mcp_bridge import bootstrap
            bridge = bootstrap.start(params["runtime_home"], show=False)
            _mark("start_finished", session_id=bridge.state.session_id, timer_id=bridge.timer._timer_id)
        if case in {"gui_only", "combined"}:
            _show_minimal_gui()
        elif case == "native_panel":
            from ansa import guitk
            from ansa_mcp_bridge.ui import native_panel
            guitk.BCTimerSingleShot(1600, _request_native_close, bridge)
            _mark("native_before_show")
            native_panel.show_panel(bridge)
            _mark("native_show_returned", ui=dict(bridge.state.panel_info))
            _mark("native_close_confirmed", confirmed=native_panel.close_panel(bridge))
        elif case in {"entry", "entry_reload"}:
            from ansa import guitk
            guitk.BCTimerSingleShot(1600, _request_entry_close, None)
            _mark("entry_close_armed")
        _mark("function_returning")
    except BaseException as exc:
        _mark("python_error", error_type=type(exc).__name__)
        # 诊断记录故障，不把 Python 异常交给 ANSA 原生入口。
        print("[native isolation] Python failure:", type(exc).__name__)
    return None
