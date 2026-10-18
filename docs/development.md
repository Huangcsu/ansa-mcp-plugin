# 开发说明

开发环境使用 Windows 和 64 位 CPython 3.12。普通源码、文档和单元检查不需要启动 ANSA；真实 API 验证需要已授权的 ANSA 2026.1.0 安装。

## 环境

在仓库根目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-runtime.lock
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

运行依赖由 `pyproject.toml` 声明，Windows/Python 3.12 的精确版本集在 `packaging/requirements-runtime.lock`。开发依赖补充构建所需的 setuptools 和测试所需的 packaging。当前运行锁没有下载哈希，不承诺跨环境逐字节重现。

## 目录

| 位置 | 用途 |
| --- | --- |
| `src/ansa_mcp_plugin/mcp/` | 工具 schema 与提交入口 |
| `src/ansa_mcp_plugin/backend/` | 发现、绑定、持久任务和调度 |
| `src/ansa_mcp_bridge/operations/` | 主线程内的受控模型操作及回读 |
| `src/ansa_mcp_bridge/ui/`、`platform/` | 原生面板、Windows 定时器和进程保护 |
| `src/ansa_mcp_shared/` | 协议、身份与共享分类 |
| `packaging/ansa/` | 开发与发布的独立原生入口 |
| `scripts/` | 开发安装、启动、映射生成及发行构建 |
| `tests/unit/`、`tests/integration/` | 离线回归与按能力划分的验收入口 |

`stage_one`、`stage_two` 和 `stage_three` 等测试名保留了演进顺序，其中部分文件仍是其他集成入口的助手依赖，不应按名称直接删除。

## 最小验证

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests/unit -q
git diff --check
```

按变更风险选择对应测试。文档变更检查链接与内容；协议、任务或操作分派变更运行相关单元和模拟链路；ANSA API 行为变更使用独立夹具验证，不自动连接用户当前模型。

真实 stdio/TCP/SQLite 模拟不启动 ANSA，使用新的用户私有目录：

```powershell
$smokeHome = Join-Path $env:USERPROFILE ('ANSA-MCP-Plugin-Runtime\smoke-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/simulated_mcp.py --runtime-home $smokeHome
```

## 开发入口与构建

源码链接安装、外部 wheel、原生包暂存和回退均见[安装说明](installation.md)。源码开发入口会读取当前工作目录中的源码；独立安装验收需要从无源码目录启动已安装产物。

修改 ANSA API 调用前，查阅与目标 ANSA 版本一致的官方帮助，核对签名、返回值、deck 和实体字段。官方帮助与 SDK 由开发者自行准备，不随仓库分发；编辑器、AI 工具和文档检索方式由开发者自行选择。

新增工具同时检查：

- MCP 参数和回包 schema。
- `ansa_mcp_shared/operations.py` 中的读写及模型变更分类。
- 桥接分派、执行前门禁、备份、失败回读和原操作幂等。
- `scripts/stage_native_release.py` 的显式源码清单。
- [工具参考](tools.md)与对应公开契约。

## 常用真实验收入口

| 入口 | 范围 |
| --- | --- |
| `stage_three_p0_ansa.py` | API 文档、ABAQUS 质量和表面成员 |
| `stage_three_mechanism_mesh_ansa.py` | QUAD、Map HEXA8 及组合流程 |
| `stage_three_kinematics_ansa.py` | 刚体、副、配置与独立机构回读 |
| `stage_three_abaqus_contact_ansa.py` | 接触关系、厚度、距离与节点采样 |
| `stage_three_abaqus_mesh_workflow_ansa.py` | 拉伸、镜像、节点合并与释放 |
| `stage_three_ribbon_lifecycle.py` | 原生入口、模态窗口和生命周期 |
| `stage_four_external_install.py` | 已安装外部服务的无源码启动 |
| `stage_four_native_install.py`、`stage_four_native_followup.py` | 独立原生安装与所属窗口续验 |

从 `tests/integration/` 调用这些入口，先读其 `--help` 及准备/续验条件。续验脚本依赖原实例和原任务，不能把旧报告目录作为新验收反复覆盖。主线程、目标归属及报告分层规则见[架构](architecture.md)。

## 文档与发行维护

`docs/tools.md` 维护当前工具分组；`docs/tool-mapping.json` 和 `docs/reference/legacy-mapping.md` 保留功能迁移对应。修改映射后运行 `scripts/update_tool_mapping.py`，不依赖本机原始报告。

源码、测试、公开文档与构建输入在同一仓库维护。个人智能体配置（`.agents/`、`AGENTS.md`）、缓存和本机记录保持忽略。发行产物进入 Releases 附件；发行检查见[发布说明](releasing.md)。
