# -*- coding: utf-8 -*-
"""本机 source link 的 ANSA MCP Plugin 开发入口；导入时只注册 Ribbon 回调。"""

import builtins
import importlib
import json
import os
from pathlib import Path
import sys


_ENTRY_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _ENTRY_DIR / "development.json"
_BRIDGE_MODULES = ("ansa_mcp_bridge", "ansa_mcp_shared")
_CLOSE_RETRY_MS = 250
_CLOSE_RETRY_LIMIT = 20
_PERSISTENT_NAME = "_ansa_mcp_plugin_dev_entry"


def _persistent_entry():
    """Ribbon 包装函数委托给 sys.modules 中长期存活的普通 Python 模块。"""
    module = sys.modules.get(_PERSISTENT_NAME)
    if module is not None:
        return module
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location(_PERSISTENT_NAME, str(_ENTRY_DIR / "AnsaMcpPluginDev.py"))
    module = module_from_spec(spec)
    sys.modules[_PERSISTENT_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PERSISTENT_NAME, None)
        raise
    return module


def _read_config():
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception as exc:
        raise RuntimeError("无法读取开发配置 %s: %s" % (_CONFIG_PATH, exc)) from exc
    if not isinstance(config, dict):
        raise RuntimeError("development.json 必须是 JSON 对象")

    paths = {}
    for key in ("source_root", "runtime_home"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("development.json 缺少非空字符串字段 %s" % key)
        path = Path(os.path.expandvars(os.path.expanduser(value.strip())))
        if not path.is_absolute():
            path = _ENTRY_DIR / path
        paths[key] = path.resolve()

    source = paths["source_root"] / "src"
    if not (source / "ansa_mcp_bridge" / "bootstrap.py").is_file():
        raise RuntimeError("开发源码目录无效，找不到 src/ansa_mcp_bridge/bootstrap.py: %s" % source)
    if not (source / "ansa_mcp_shared").is_dir():
        raise RuntimeError("开发源码目录无效，找不到 src/ansa_mcp_shared: %s" % source)
    if not paths["runtime_home"].is_dir():
        raise RuntimeError("开发运行目录不存在: %s" % paths["runtime_home"])
    return paths


def _source_path(config):
    return str((config["source_root"] / "src").resolve())


def _use_source(config):
    source_path = _source_path(config)
    sys.path[:] = [entry for entry in sys.path if os.path.normcase(os.path.abspath(entry or os.curdir))
                   != os.path.normcase(source_path)]
    sys.path.insert(0, source_path)
    importlib.invalidate_caches()


def _clear_dev_modules():
    for name in list(sys.modules):
        # 原生 Hide 窗口仍持有模块级回调；重载业务源码时保留其长期 GUI 所有者。
        if name == "ansa_mcp_bridge.ui.native_panel":
            continue
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _BRIDGE_MODULES):
            del sys.modules[name]
    importlib.invalidate_caches()


def _legacy_sum_is_active():
    old_plugin = sys.modules.get("ansa_mcp_sum.plugin")
    if old_plugin is None:
        return False
    state = getattr(old_plugin, "GUI_HANDLES", {}).get("bridge_runtime_state", {})
    return bool(state and not state.get("stop_requested") and
                (state.get("poller") is not None or state.get("timer") is not None))


def _require_legacy_sum_stopped():
    if _legacy_sum_is_active():
        raise RuntimeError(
            "检测到旧版 ANSA MCP-Sum 正在运行。请先在 ANSA MCP-Sum 面板点击“停止桥接”，"
            "再点击 ANSA MCP Plugin 入口；本开发入口不会自动停止或修改旧版桥接。"
        )


def _bridge():
    return getattr(builtins, "_ansa_mcp_plugin_bridge", None)


def _require_idle(bridge):
    journal = getattr(getattr(bridge, "state", None), "journal", None)
    if journal is None:
        raise RuntimeError("无法确认桥接任务队列，拒绝停止或重载")
    busy = journal.busy()
    if busy:
        raise RuntimeError("桥接仍有排队或运行中的任务，已拒绝停止或重载；请先等待任务完成")

    pending = getattr(getattr(bridge, "executor", None), "pending", None)
    if pending is None or not callable(getattr(pending, "empty", None)):
        raise RuntimeError("无法确认 ANSA 主线程控制队列，拒绝停止或重载")
    if not pending.empty():
        raise RuntimeError("ANSA 主线程仍有排队控制，已拒绝停止或重载；请稍后重试")


def _fail(message, cause=None):
    if cause is not None:
        message = "%s: %s: %s" % (message, type(cause).__name__, cause)
    line = "[ANSA MCP Plugin Dev] " + message
    print(line)
    if cause is None:
        raise RuntimeError(message)
    raise RuntimeError(message) from cause


def _run(action):
    try:
        _require_legacy_sum_stopped()
        return action()
    except Exception as exc:
        print("[ANSA MCP Plugin Dev] %s: %s" % (type(exc).__name__, exc))
        raise


def _start_or_show():
    existing = _bridge()
    if existing is not None and not getattr(existing, "closed", False):
        if getattr(existing, "_panel_close_retry_required", False):
            raise RuntimeError("旧面板关闭仍在确认中；请等待关闭完成后再打开控制面板")
        from ansa_mcp_bridge.ui.native_panel import show_panel
        show_panel(existing)
        return

    config = _read_config()
    _use_source(config)
    bootstrap = importlib.import_module("ansa_mcp_bridge.bootstrap")
    bridge = bootstrap.start(str(config["runtime_home"]), show=False)
    # Ribbon 本身就是原生 GUI 回调。保持其 BCShow 生命周期，避免脚本返回时
    # ANSA 清理异步创建的 GUI 资源；MCP 的排队显示入口仍独立使用。
    from ansa_mcp_bridge.ui.native_panel import show_panel
    show_panel(bridge)


def _reload_after_close():
    config = _read_config()
    _use_source(config)
    _clear_dev_modules()
    bootstrap = importlib.import_module("ansa_mcp_bridge.bootstrap")
    bridge = bootstrap.start(str(config["runtime_home"]), show=False)
    bridge._native_panel_enabled = True
    return bridge


def _finish_close(bridge, *, reload_code):
    if not getattr(bridge, "closed", False):
        raise RuntimeError("桥接尚未确认关闭")
    if reload_code:
        _reload_after_close()
        print("[ANSA MCP Plugin Dev] 开发代码已重新加载；点击控制面板显示")
    else:
        print("[ANSA MCP Plugin Dev] 本机桥接已停止")


def _schedule_close_retry(bridge, *, reload_code):
    from ansa import guitk

    def retry(attempt_data):
        attempt = attempt_data["attempt"] + 1
        if getattr(bridge, "closed", False):
            _finish_close(bridge, reload_code=reload_code)
            return 0

        try:
            _require_idle(bridge)
            bridge.close()
        except Exception as exc:
            if not getattr(bridge, "_panel_close_retry_required", False):
                _fail("重试关闭桥接失败", exc)

        if getattr(bridge, "closed", False):
            _finish_close(bridge, reload_code=reload_code)
            return 0
        if not getattr(bridge, "_panel_close_retry_required", False):
            _fail("关闭没有完成，且桥接未报告面板关闭待确认状态")
        if attempt >= _CLOSE_RETRY_LIMIT:
            _fail("20 次异步重试后仍未确认旧面板关闭；桥接和进程锁仍保留，请在 ANSA 中关闭面板后重试")

        guitk.BCTimerSingleShot(_CLOSE_RETRY_MS, retry, {"attempt": attempt})
        return 0

    guitk.BCTimerSingleShot(_CLOSE_RETRY_MS, retry, {"attempt": 0})


def _close_bridge(*, reload_code):
    bridge = _bridge()
    if bridge is None or getattr(bridge, "closed", False):
        if reload_code:
            _reload_after_close()
        else:
            print("[ANSA MCP Plugin Dev] 当前没有运行中的本机桥接")
        return

    _require_idle(bridge)
    try:
        bridge.close()
    except Exception as exc:
        if getattr(bridge, "_panel_close_retry_required", False):
            _schedule_close_retry(bridge, reload_code=reload_code)
            return
        raise

    if getattr(bridge, "closed", False):
        _finish_close(bridge, reload_code=reload_code)
        return
    if getattr(bridge, "_panel_close_retry_required", False):
        _schedule_close_retry(bridge, reload_code=reload_code)
        return
    raise RuntimeError("桥接关闭未完成，且未报告面板关闭待确认状态")


def AnsaMcpPluginDevControlPanel():
    """打开或复用本机开发桥接控制面板。"""
    module = _persistent_entry()
    module._run(module._start_or_show)


def AnsaMcpPluginDevReload():
    """等待空闲桥接及其旧面板关闭，再从 source link 重载。"""
    module = _persistent_entry()
    module._run(lambda: module._close_bridge(reload_code=True))


def AnsaMcpPluginDevStop():
    """安全停止空闲的本机桥接，不影响旧版 MCP-Sum。"""
    module = _persistent_entry()
    module._run(lambda: module._close_bridge(reload_code=False))
