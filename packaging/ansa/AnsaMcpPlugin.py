# -*- coding: utf-8 -*-
"""可随普通目录或官方 Packager ZIP 安装的 ANSA MCP Plugin 原生入口。"""

import builtins
import importlib
import json
import os
from pathlib import Path
import sys


_ROOT_PACKAGE = "AnsaMcpPlugin_root"
_NESTED_ROOT = _ROOT_PACKAGE + "." + _ROOT_PACKAGE
_ENTRY_MODULE = _ROOT_PACKAGE + ".AnsaMcpPlugin"
_NESTED_ENTRY_MODULE = _NESTED_ROOT + ".app"
_BRIDGE_MODULES = ("ansa_mcp_bridge", "ansa_mcp_shared")
_CONFIG_ENV = "ANSA_MCP_PLUGIN_CONFIG"
_CLOSE_RETRY_MS = 250
_CLOSE_RETRY_LIMIT = 20


def _package_locations():
    """返回入口所在包目录和导入父目录，兼容目录及 ZIP 安装。"""
    origin = globals().get("__file__")
    if not isinstance(origin, str) or not origin:
        raise RuntimeError("无法确定原生入口所在的安装包目录")
    if not os.path.isabs(origin):
        raise RuntimeError("原生入口路径必须是绝对路径，拒绝依赖当前工作目录")

    origin_path = os.path.normpath(origin)
    package_root = os.path.dirname(origin_path)
    if not package_root or package_root == origin_path:
        raise RuntimeError("无法确定原生入口所在的安装包目录")

    for entry in tuple(sys.path):
        if not isinstance(entry, str) or not entry or not os.path.isabs(entry):
            continue
        archive_path = os.path.normpath(entry)
        if not os.path.isfile(archive_path):
            continue
        boundary = archive_path.rstrip("\\/") + os.sep
        normalized_origin = os.path.normcase(origin_path)
        normalized_boundary = os.path.normcase(boundary)
        if normalized_origin.startswith(normalized_boundary):
            return package_root, archive_path

    for parent in Path(origin_path).parents:
        if parent.is_file() and parent.suffix.casefold() == ".zip":
            return package_root, str(parent)

    return package_root, os.path.dirname(package_root)


def _normalized_path(path):
    value = os.fspath(path)
    if not os.path.isabs(value):
        raise ValueError("路径必须是绝对路径")
    return os.path.normcase(os.path.normpath(value))


def _add_import_path(path):
    normalized = _normalized_path(path)
    for current in sys.path:
        if (
            isinstance(current, str)
            and current
            and os.path.isabs(current)
            and _normalized_path(current) == normalized
        ):
            return
    sys.path.insert(0, str(path))


def _is_from_package(module, package_root):
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or not origin:
        return False
    normalized_origin = _normalized_path(origin)
    normalized_root = _normalized_path(package_root).rstrip("\\/")
    return normalized_origin == normalized_root or normalized_origin.startswith(
        normalized_root + os.sep
    )


def _is_outer_root_package(module, package_root):
    """允许官方 Packager ZIP 中入口包的祖先 __init__ 模块。"""
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or not origin:
        return False
    normalized_origin = _normalized_path(origin)
    inner_root = Path(package_root)
    outer_root = inner_root.parent
    archive_path = outer_root.parent
    # ANSA 的脚本回调可能临时移除 ZIP 的 sys.path 项；归属依据是当前入口
    # 的确切 root/root 目录，而不是回调时可变的导入搜索路径。
    if (
        inner_root.name != _ROOT_PACKAGE
        or outer_root.name != _ROOT_PACKAGE
        or archive_path.suffix.casefold() != ".zip"
        or not archive_path.is_file()
    ):
        return False
    return normalized_origin in (
        _normalized_path(outer_root),
        _normalized_path(outer_root / "__init__.py"),
    )


def _is_allowed_module(name, module, package_root):
    if _is_from_package(module, package_root):
        return True
    return name == _ROOT_PACKAGE and _is_outer_root_package(module, package_root)


def _reject_foreign_modules(package_root):
    prefixes = (_ROOT_PACKAGE, *_BRIDGE_MODULES)
    for name, module in tuple(sys.modules.items()):
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            continue
        if module is None or not _is_allowed_module(name, module, package_root):
            raise RuntimeError(
                "检测到从其他位置加载的同名 ANSA MCP Plugin 模块 %s；"
                "请先重启 ANSA，避免不同版本模块混用" % name
            )


def _import_bridge_packages():
    package_root, import_parent = _package_locations()
    _reject_foreign_modules(package_root)
    _add_import_path(import_parent)
    _add_import_path(package_root)
    importlib.invalidate_caches()

    shared = importlib.import_module("ansa_mcp_shared")
    bridge = importlib.import_module("ansa_mcp_bridge")
    _reject_foreign_modules(package_root)
    return bridge, shared


def _persistent_entry():
    """通过包名导入长期存活模块，避免脚本回调返回后被 ANSA 回收。"""
    package_root, import_parent = _package_locations()
    current_name = globals().get("__name__")
    candidates = []
    if (
        isinstance(current_name, str)
        and current_name.startswith(_ROOT_PACKAGE + ".")
        and current_name != _ROOT_PACKAGE
    ):
        candidates.append(current_name)
    for name in (_NESTED_ENTRY_MODULE, _ENTRY_MODULE):
        if name not in candidates:
            candidates.append(name)

    for name in candidates:
        existing = sys.modules.get(name)
        if existing is None:
            continue
        if not _is_from_package(existing, package_root):
            raise RuntimeError(
                "ANSA MCP Plugin 原生入口已从其他位置加载；请重启 ANSA 后再试"
            )
        return existing

    _add_import_path(import_parent)
    _add_import_path(package_root)
    importlib.invalidate_caches()
    for name in candidates:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = exc.name
            if missing == name or (
                isinstance(missing, str) and name.startswith(missing + ".")
            ):
                continue
            raise
        if not _is_from_package(module, package_root):
            raise RuntimeError("导入的 ANSA MCP Plugin 原生入口不属于当前安装包")
        return module
    raise RuntimeError("无法加载 ANSA MCP Plugin 原生入口")


def _read_config():
    configured_path = os.environ.get(_CONFIG_ENV)
    if configured_path is None:
        config_path = Path.home() / ".ansa-mcp-plugin" / "installation.json"
    else:
        if not configured_path.strip():
            raise RuntimeError("环境变量 %s 不能为空" % _CONFIG_ENV)
        config_path = Path(configured_path.strip())
        if not config_path.is_absolute():
            raise RuntimeError("环境变量 %s 必须指定绝对 JSON 路径" % _CONFIG_ENV)

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception as exc:
        raise RuntimeError("无法读取安装配置 %s: %s" % (config_path, exc)) from exc
    if not isinstance(config, dict):
        raise RuntimeError("安装配置必须是 JSON 对象")

    runtime_value = config.get("runtime_home")
    if not isinstance(runtime_value, str) or not runtime_value.strip():
        raise RuntimeError("安装配置缺少非空字符串字段 runtime_home")
    runtime_home = Path(runtime_value.strip())
    if not runtime_home.is_absolute():
        raise RuntimeError("安装配置中的 runtime_home 必须是绝对路径")
    try:
        runtime_home = runtime_home.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("安装运行目录不存在或不可访问: %s" % runtime_home) from exc
    if not runtime_home.is_dir():
        raise RuntimeError("安装运行目录不是文件夹: %s" % runtime_home)
    return {"runtime_home": runtime_home}


def _legacy_sum_is_active():
    old_plugin = sys.modules.get("ansa_mcp_sum.plugin")
    if old_plugin is None:
        return False
    state = getattr(old_plugin, "GUI_HANDLES", {}).get("bridge_runtime_state", {})
    return bool(
        state
        and not state.get("stop_requested")
        and (state.get("poller") is not None or state.get("timer") is not None)
    )


def _require_legacy_sum_stopped():
    if _legacy_sum_is_active():
        raise RuntimeError(
            "检测到旧版 ANSA MCP-Sum 正在运行。请先在旧版面板点击“停止桥接”，"
            "再使用 ANSA MCP Plugin；本入口不会停止或修改旧版桥接。"
        )


def _bridge():
    return getattr(builtins, "_ansa_mcp_plugin_bridge", None)


def _same_runtime(bridge, runtime_home):
    existing_home = getattr(bridge, "runtime_home", None)
    if existing_home is None:
        raise RuntimeError("无法核对当前桥接的 runtime_home，拒绝复用")
    return _normalized_path(existing_home) == _normalized_path(runtime_home)


def _require_idle(bridge):
    journal = getattr(getattr(bridge, "state", None), "journal", None)
    if journal is None:
        raise RuntimeError("无法确认桥接任务队列，拒绝停止")
    if journal.busy():
        raise RuntimeError("桥接仍有排队或运行中的任务，已拒绝停止；请等待任务完成")

    pending = getattr(getattr(bridge, "executor", None), "pending", None)
    if pending is None or not callable(getattr(pending, "empty", None)):
        raise RuntimeError("无法确认 ANSA 主线程控制队列，拒绝停止")
    if not pending.empty():
        raise RuntimeError("ANSA 主线程仍有排队控制，已拒绝停止；请稍后重试")


def _run(action):
    try:
        _require_legacy_sum_stopped()
        return action()
    except Exception as exc:
        print("[ANSA MCP Plugin] %s: %s" % (type(exc).__name__, exc))
        raise


def _start_or_show():
    config = _read_config()
    _import_bridge_packages()

    existing = _bridge()
    if existing is not None and not getattr(existing, "closed", False):
        if getattr(existing, "_panel_close_retry_required", False):
            raise RuntimeError("旧面板关闭仍在确认中；请等待关闭完成后再打开控制面板")
        if not _same_runtime(existing, config["runtime_home"]):
            raise RuntimeError("当前 ANSA 进程已使用其他 runtime_home 的桥接，拒绝切换运行目录")
        native_panel = importlib.import_module("ansa_mcp_bridge.ui.native_panel")
        native_panel.show_panel(existing)
        return existing

    bootstrap = importlib.import_module("ansa_mcp_bridge.bootstrap")
    bridge = bootstrap.start(str(config["runtime_home"]), show=False)
    bridge._native_panel_enabled = True
    native_panel = importlib.import_module("ansa_mcp_bridge.ui.native_panel")
    native_panel.show_panel(bridge)
    return bridge


def _finish_stop(bridge):
    if not getattr(bridge, "closed", False):
        raise RuntimeError("桥接尚未确认关闭")
    print("[ANSA MCP Plugin] 本机桥接已停止")


def _schedule_close_retry(bridge):
    from ansa import guitk

    def retry(attempt_data):
        attempt = attempt_data["attempt"] + 1
        if getattr(bridge, "closed", False):
            _finish_stop(bridge)
            return 0

        try:
            _require_idle(bridge)
            bridge.close()
        except Exception as exc:
            if not getattr(bridge, "_panel_close_retry_required", False):
                raise RuntimeError("重试关闭桥接失败: %s: %s" % (type(exc).__name__, exc)) from exc

        if getattr(bridge, "closed", False):
            _finish_stop(bridge)
            return 0
        if not getattr(bridge, "_panel_close_retry_required", False):
            raise RuntimeError("关闭没有完成，且桥接未报告面板关闭待确认状态")
        if attempt >= _CLOSE_RETRY_LIMIT:
            raise RuntimeError(
                "20 次异步重试后仍未确认原生面板关闭；桥接和进程锁仍保留，"
                "请在 ANSA 中关闭面板后重试"
            )

        guitk.BCTimerSingleShot(_CLOSE_RETRY_MS, retry, {"attempt": attempt})
        return 0

    guitk.BCTimerSingleShot(_CLOSE_RETRY_MS, retry, {"attempt": 0})


def _stop_bridge():
    bridge = _bridge()
    if bridge is None or getattr(bridge, "closed", False):
        print("[ANSA MCP Plugin] 当前没有运行中的本机桥接")
        return

    config = _read_config()
    if not _same_runtime(bridge, config["runtime_home"]):
        raise RuntimeError("当前桥接属于其他 runtime_home，拒绝从本安装入口停止")
    package_root, _import_parent = _package_locations()
    _reject_foreign_modules(package_root)
    _require_idle(bridge)
    try:
        bridge.close()
    except Exception as exc:
        if getattr(bridge, "_panel_close_retry_required", False):
            _schedule_close_retry(bridge)
            return
        raise

    if getattr(bridge, "closed", False):
        _finish_stop(bridge)
    elif getattr(bridge, "_panel_close_retry_required", False):
        _schedule_close_retry(bridge)
    else:
        raise RuntimeError("桥接关闭未完成，且未报告面板关闭待确认状态")


def AnsaMcpPluginControlPanel():
    """打开或复用本机桥接控制面板。"""
    module = _persistent_entry()
    module._run(module._start_or_show)


def AnsaMcpPluginStop():
    """安全停止空闲的本机桥接。"""
    module = _persistent_entry()
    module._run(module._stop_bridge)
