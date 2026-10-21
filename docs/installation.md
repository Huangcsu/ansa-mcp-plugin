# 安装与回退

适用环境：Windows、64 位 CPython 3.12、拥有有效许可证的 ANSA 2026.1.0 GUI。外部服务和 ANSA 原生桥接分别安装，并使用同一个私有 `runtime_home`。

当前源码版本为 `0.1.2` 预览版。仓库首页提供源码开发安装；独立发行需要配套 wheel 和官方 `.bpkg`，不能只安装其中一端。

## 源码开发安装

按[开发说明](development.md#环境)准备环境。先关闭 ANSA，并停止使用同一运行目录的 MCP 后端，再在源码根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/install_dev_plugin.py --enable
```

安装器备份现有开发入口与 default profile 的 GUI 配置，安装并启用 ANSA 端插件面板入口，同时初始化当前用户目录下的私有运行数据。

| 安装内容 | 默认位置 |
| --- | --- |
| 插件描述 | `%USERPROFILE%/.BETA/version_26.10.000/ANSA/plugins/AnsaMcpPluginDev.ppl` |
| 入口脚本、图标与 `development.json` | 上述 `plugins/` 下的 `AnsaMcpPluginDev/` |
| 启用登记 | `%USERPROFILE%/.BETA/version_26.10.000/ANSA/profiles/default/gui/ANSA.xml` |
| 运行数据 | `%USERPROFILE%/ANSA-MCP-Plugin-Runtime/development` |

安装输出包含 `descriptor`、`entry`、`runtime_home`、`backup` 和 `registration_enabled`。使用 `--enable` 时，`registration_enabled` 为 `true`；省略该参数会登记为停用。

1. 重新启动 ANSA 2026.1.0，并使用安装器修改的 **default profile**。
2. 在 Ribbon 的 **Custom → Plugins** 中找到 **ANSA MCP Plugin**，点击**控制面板**。
3. 确认面板显示**概览、命令记录、诊断日志、设置**页签。加载插件描述只注册入口；首次点击控制面板才启动本机桥接。
4. 配置并启动外部 MCP 服务，保持两端 `runtime_home` 一致。在面板点击**连接此窗口**后，查询 `ansa_connection_status` 与 `ansa_model_snapshot` 核对目标。

该入口依赖当前源码目录，移动源码后需重新运行安装器。通用客户端配置示例见 [README](../README.md#快速开始)。

## 面板入口未出现

- 若安装器提示找不到 default GUI 配置，先使用 ANSA 2026.1.0 的 **default profile** 启动，保存 ANSA GUI Settings 并退出，再运行安装命令。
- 确认重启的是同一 Windows 用户的 ANSA 2026.1.0，且正在使用 **default profile**。安装器只修改该 profile。
- 在 **Tools → Development → Plugins Manager** 中检查 **ANSA MCP Plugin** 的启用勾选；修改后保存 ANSA GUI Settings 并重启 ANSA。
- 若列表中没有该插件，检查安装输出的 `descriptor`、`entry` 文件及 `development.json` 是否存在，再关闭 ANSA，使用 `--enable` 重新安装。
- 若按钮存在但打开失败，查看 ANSA 控制台错误。开发配置中的源码目录必须可读；若提示旧版 ANSA MCP-Sum 正在运行，先在旧插件中停止桥接，再打开新面板。

面板出现后若后端离线，核对 MCP 客户端是否已启动服务，以及客户端与 ANSA 端的 `runtime_home` 是否一致。面板的连接按钮不会启动外部 MCP 服务。

## 外部 wheel

在仓库根目录构建，先安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/build_external_release.py
```

输出 `dist/v0.1.2/ansa_mcp_plugin-0.1.2-py3-none-any.whl` 和包含文件清单及 SHA256 的 manifest。构建器审计包边界；wheel 包含外部服务、共享包及发行元数据。

用 Python 3.12 执行安装器，安装目录必须尚不存在：

```powershell
py -3.12 scripts/install_external_release.py `
  --wheel dist/v0.1.2/ansa_mcp_plugin-0.1.2-py3-none-any.whl `
  --lock packaging/requirements-runtime.lock `
  --install-dir "$env:USERPROFILE/ANSA-MCP-Plugin-Installations/v0.1.2" `
  --runtime-home "$env:USERPROFILE/ANSA-MCP-Plugin-Runtime/v0.1.2"
```

安装器创建独立 venv、安装固定版本依赖、执行 `pip check` 并初始化私有目录。下载失败时保留失败回执，不放宽依赖绕过错误。

| 生成文件 | 用途 |
| --- | --- |
| `install-manifest.json` | wheel/lock 哈希、解释器、实际版本和运行目录 |
| `launcher.py` | 启动已安装外部服务 |
| `mcp-config.json` | 按实际安装位置生成的 MCP stdio 配置 |

将生成的配置登记到 MCP 客户端。安装器不会自动修改客户端设置；同一运行目录只允许一个活动后端。

## ANSA 原生包

从同一源码暂存桥接包、共享包、入口和许可：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/stage_native_release.py `
  --output-dir build/native-0.1.2
```

输出目录必须不存在。脚本按显式源码清单复制并生成 SHA256 清单，随后由 ANSA 官方 Packager 根据暂存的 `.ppl` 构建 `.bpkg`，再通过官方 Installer 安装。脚本本身不生成 `.bpkg`。

Windows Packager 如需已验证的 GBK 文本兼容副本，可在新的目录生成：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/stage_native_packager_compat.py `
  --source-dir build/native-0.1.2 `
  --output-dir build/native-0.1.2-packager
```

兼容过程保留源文件，记录逐文件哈希和编码转换；不能把普通 ZIP 改名为 `.bpkg`。官方 Packager/Installer 的具体界面及参数以用户安装版本附带的帮助为准。

正式入口从 ANSA 进程环境变量 `ANSA_MCP_PLUGIN_CONFIG` 指定的绝对 JSON 路径读取配置。可指向外部安装生成的 `install-manifest.json`，复用其中的 `runtime_home`。未设置时读取用户目录的 `.ansa-mcp-plugin/installation.json`。环境变量须在启动 ANSA 前设置，例如：

```powershell
$env:ANSA_MCP_PLUGIN_CONFIG = Join-Path $env:USERPROFILE 'ANSA-MCP-Plugin-Installations/v0.1.2/install-manifest.json'
```

通过官方 Installer 安装配套 `.bpkg` 后，在 **Tools → Development → Plugins Manager** 中确认 **ANSA MCP Plugin** 已启用；若更改启用状态，保存 ANSA GUI Settings 并重启。随后从 **Custom → Plugins → ANSA MCP Plugin → Control Panel** 打开面板。首次点击时读取上述配置并启动桥接；MCP 后端启动后，在目标 ANSA 面板点击连接，再通过连接状态和模型快照核对活动目标。

## 验证与回退

先检查客户端发现的 69 项工具、面板绑定及模型快照。完整独立安装验证还包括明确范围的模型读写、幂等、保存重开、独立回读和所属窗口画布；见[验证范围](acceptance.md)。

新版本采用新目录安装。切换前等待旧任务结束并停止旧后端，将客户端指向新安装，再从 ANSA 面板重新绑定。回退时恢复旧客户端配置及其配套原生包和运行目录。

不要让新旧后端同时使用同一运行目录，也不要直接替换运行中的数据库或令牌。安装失败时保留失败回执，修复后使用新的安装目录。
