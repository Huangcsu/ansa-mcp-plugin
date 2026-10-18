"""本机源码调试启动入口；先用 install_dev_plugin.py 初始化私有运行目录。"""

from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ansa_mcp_bridge.bootstrap import start

BRIDGE = start(runtime_home=os.environ.get("ANSA_MCP_PLUGIN_HOME") or Path.home() / "ANSA-MCP-Plugin-Runtime/development", show=False)
BRIDGE._native_panel_enabled = True
print("ANSA MCP Plugin instance:", BRIDGE.state.instance_id, "port:", BRIDGE.transport.port)
BRIDGE.show_panel()  # 与 Ribbon 入口共用原生面板，桥接执行器独立运行。
