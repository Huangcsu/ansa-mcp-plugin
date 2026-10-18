"""安装本机源码调试入口；独立注册到 ANSA Plugins，不自动启动桥接。"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def install(version_id, runtime_home, enable=False):
    if version_id != "26.10.000":
        raise ValueError("开发入口当前只验收 ANSA 2026.1.0 / 26.10.000")
    user_home = Path.home().resolve()
    home = Path(runtime_home).expanduser().resolve()
    home.relative_to(user_home)
    from ansa_mcp_plugin.backend.service import Service
    Service(home).close()  # 初始化并核验私有运行目录、令牌及 SQLite，不启动常驻服务。

    profile = user_home / ".BETA" / ("version_" + version_id) / "ANSA"
    gui = profile / "profiles/default/gui/ANSA.xml"
    if not gui.is_file():
        raise FileNotFoundError("找不到当前 default ANSA GUI 配置：" + str(gui))
    original = gui.read_bytes()
    ET.fromstring(original)
    source = ROOT / "packaging/ansa"
    names = ("AnsaMcpPluginDev.ppl", "AnsaMcpPluginDev.py", "AnsaMcpPluginDev.svg")
    for name in names:
        if not (source / name).is_file():
            raise FileNotFoundError(source / name)

    destination = profile / "plugins"
    entry_dir = destination / "AnsaMcpPluginDev"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = profile / ("ansa-mcp-plugin-backup-" + stamp)
    backup_dir.mkdir(parents=True)
    shutil.copy2(gui, backup_dir / "ANSA.xml")
    descriptor = destination / names[0]
    if descriptor.exists():
        shutil.copy2(descriptor, backup_dir / descriptor.name)
    if entry_dir.exists():
        shutil.copytree(entry_dir, backup_dir / "AnsaMcpPluginDev")
    entry_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / names[0], descriptor)
    for name in names[1:]:
        shutil.copy2(source / name, entry_dir / name)
    (entry_dir / "development.json").write_text(json.dumps({
        "mode": "local_source_development", "source_root": str(ROOT), "runtime_home": str(home),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 只添加自身登记，不重写整个 XML，也不改变其他插件或启动脚本。
    text = original.decode("utf-8")
    loaded = "1" if enable else "0"
    registration = '   <plugin name="' + descriptor.as_posix() + '" loaded="' + loaded + '" addedFrom="3" />'
    if descriptor.as_posix() not in text:
        end = "  </Script_Plugins>"
        if text.count(end) != 1:
            raise ValueError("ANSA XML Script_Plugins 结构不符，未修改配置")
        newline = "\r\n" if b"\r\n" in original else "\n"
        text = text.replace(end, registration + newline + end, 1)
    else:
        pattern = r'(<plugin\s+name="' + re.escape(descriptor.as_posix()) + r'"\s+loaded=")[01](")'
        text, changed = re.subn(pattern, lambda match: match.group(1) + loaded + match.group(2), text)
        if changed != 1:
            raise ValueError("已有开发入口登记结构不符，未修改配置")
    updated = text.encode("utf-8")
    parsed = ET.fromstring(updated)
    found = [item for item in parsed.iter("plugin") if item.get("name") == descriptor.as_posix()]
    if len(found) != 1 or found[0].get("loaded") != loaded:
        raise ValueError("开发入口登记验证失败")
    temp = gui.with_name("ANSA.xml.ansa-mcp-plugin.tmp")
    temp.write_bytes(updated)
    temp.replace(gui)
    result = {"descriptor": str(descriptor), "entry": str(entry_dir / names[1]),
              "runtime_home": str(home), "source_root": str(ROOT), "backup": str(backup_dir),
              "registration_enabled": enable, "restart_required": True}
    evidence = ROOT / "runtime/verification" / ("native-dev-install-" + stamp + ".json")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-id", default="26.10.000")
    parser.add_argument("--runtime-home", default=str(Path.home() / "ANSA-MCP-Plugin-Runtime/development"))
    parser.add_argument("--enable", action="store_true", help="启用已经完成原生验收的开发入口")
    args = parser.parse_args()
    print(json.dumps(install(args.version_id, args.runtime_home, enable=args.enable), ensure_ascii=False, indent=2))
