"""诊断指定的独立测试进程；--show 仅在面板关闭时排队显示。

面板显示期间使用 MCP snapshot.ui，避免 gRPC 等待 GUI 回调结束。
"""
import json
from pathlib import Path
import sys

import grpc

import stage_one_ansa
import stage_two_ansa


def remote_panel_probe(expected_pid, expected_arguments, show):
    import builtins
    import os
    import threading
    from ansa import guitk, session
    from ansa_mcp_bridge.ui import panel as panel_module
    if (os.getpid() != expected_pid
            or [str(arg) for arg in session.ProgramArguments()] != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("The diagnostic process identity does not match the owned test")
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    if bridge is None:
        return {"pid": os.getpid(), "bridge": False}
    if show:
        import importlib
        importlib.reload(panel_module)
        bridge.show_panel()
    context = getattr(bridge, "_panel_context", None)
    state = bridge.state
    result = {"pid": os.getpid(), "bridge": True, "closed": bridge.closed,
              "session_id": state.session_id, "ticks": bridge.executor.ticks,
              "timer_busy": bridge.timer._busy, "timer_id": bridge.timer._timer_id,
              "panel_context": bool(context), "panel_alive": bool(context and context.get("alive")),
              "panel_refresh": state.panel_refresh is not None,
              "pending_controls": bridge.executor.pending.qsize(),
              "connected": state.connected.is_set(), "stopped": state.stopped.is_set(),
              "last_error": str(state.last_error).replace(bridge.token, "[redacted]"),
              # 只读诊断：本进程实际加载的面板模块版本（_REFRESH_MS 属于窗口内 BCTimer 版本）。
              "panel_module_file": getattr(panel_module, "__file__", None),
              "panel_refresh_ms": getattr(panel_module, "_REFRESH_MS", None),
              "panel_executor_refresh_ticks": getattr(panel_module, "_REFRESH_EVERY_TICKS", None),
              "panel_window_timer": bool(context and context.get("timer"))}
    if context and context.get("window") is not None:
        try:
            result["panel_visible"] = bool(context.get("visible"))
        except Exception as exc:
            result["panel_error"] = type(exc).__name__ + ": " + str(exc)
    return result


def main():
    run = Path(sys.argv[1]).resolve()
    report = run / "core-report.json"
    if not report.is_file():
        report = run / "report.json"
    identity = json.loads(report.read_text(encoding="utf-8"))["identity"]
    original_channel = grpc.insecure_channel
    def bounded(target, options=None, **kwargs):
        channel = original_channel(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs)
        return grpc.intercept_channel(channel, stage_one_ansa.DeadlineInterceptor(8))
    grpc.insecure_channel = bounded
    value = stage_two_ansa.rpc(identity["port"], remote_panel_probe,
                               identity["pid"], identity["program_args"], "--show" in sys.argv[2:])
    (run / "panel-diagnostic.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps(value))


if __name__ == "__main__":
    main()
