# 开发说明

本页介绍开发环境、源码边界和验证方式。工具参数与支持范围见[工具参考](tools.md)，模型操作约束见[架构说明](architecture.md)，历史验收结果见[验证范围](acceptance.md)。

## 环境

开发使用 Windows 和 64 位 CPython 3.12；真实 API 验证需要有效授权的 ANSA 2026.1.0 GUI。在仓库根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-runtime.lock
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

运行依赖由 `pyproject.toml` 声明，Windows/Python 3.12 的精确版本集在 `packaging/requirements-runtime.lock`；开发依赖补充构建和测试所需的包。当前运行锁未包含下载哈希。

普通文档、源码及离线单测无需启动 ANSA。原生验收使用独立实例；官方帮助、SDK 和用于离线 CAD 检查的额外环境由开发者自行准备，不进入运行依赖或源码发布。

## 目录

| 位置 | 职责 |
| --- | --- |
| `src/ansa_mcp_plugin/mcp/` | MCP 参数、结果 schema 与工具入口 |
| `src/ansa_mcp_plugin/backend/` | 连接、目标绑定、持久任务和调度 |
| `src/ansa_mcp_bridge/operations/` | ANSA 主线程内的模型操作、保护与回读 |
| `src/ansa_mcp_bridge/ui/`、`platform/` | 原生面板、定时器、窗口和进程保护 |
| `src/ansa_mcp_shared/` | 协议、身份及操作分类，仅依赖标准库 |
| `scripts/`、`packaging/` | 安装、构建、依赖清单与原生插件入口 |
| `tests/unit/` | 离线行为回归及必要夹具 |
| `tests/integration/` | 模拟链路、独立原生验收和共用验收助手 |
| `tests/fixtures/` | 可公开、可复用的自制输入 |

原生验收共用的身份回读、绑定、超时及 RPC 助手在 `tests/integration/ansa_runtime_helpers.py`；现用执行骨架为 `stage_three_ansa.py`，模拟桥接由 `stage_two_simulated.py` 提供。纯校验助手可供单测和验收驱动共同使用，单测应直接加载助手，避免导入完整真机驱动。

正式运行源码放在 `src/`。一次性探针、专项恢复、临时分析工具、用户模型及原始报告保留在被忽略的本机档案中。

## 几何前处理第一步：检查对称性

几何清理、切分、分块和网格设计前，先只读核对镜面对称、旋转／周期和回转特征，记录轴或平面、周期角、重复关系、容差和未知项。核对范围包括外形、孔道、厚度、局部特征与实体关系；材料、连接及已知载荷和约束单独判断。

确认对称且任务允许后，先备份整件，选择最小代表域或扇区，完成并验证后再复制。多个镜面或与旋转周期组合时，核对实际重复关系和变换顺序；未确认的几何保留原状。几何和网格可复制不自动授权半模型求解。

复制前核对对称／周期边界，复制后检查完整接口连接、单元方向、重复节点和单元、几何覆盖及保存重开。仅合并已确认对应的节点。具体块组设计、冻结和返工规则见[复杂实体网格流程](reference/complex-solid-meshing.md#关联块组执行规则)。

## 原生能力优先

新增或修改几何、网格、检查和模型操作时，按以下顺序确定方案：

1. 核对当前版本 ANSA 的原生功能、Python API 和已有 MCP 工具，确认签名、参数、返回值、deck 及实体字段。文档缺项或函数名称不能直接证明能力有无。
2. 原生能力可用时直接调用或做薄封装，复用任务、范围、备份、配置恢复和独立回读。
3. 文档与运行时行为不一致时，使用独立最小夹具验证；API、序列化和配置问题先在离线环境解决。
4. 确认原生能力及已有实现不能满足需求后，再增加必要的自定义算法或外部依赖，并记录具体缺口和实现范围。

ANSA API、GUI 和模型探测均在所属 ANSA 主线程执行，通信线程只校验、收发和排队。看似只读的原生集合查询也可能实例化节点，须先核对实际副作用。实体卡片读取先查 `card_fields`，再显式读取字段；缺失值不能当作零。

网格生成复用原生算法，参数规划、拓扑审计和回读负责界定输入与验证实际结果。原生派生的面积、重心或质量汇总发生变化时，先确认其物理含义与重算证据；允许的显示或舍入差异须有明确字段范围，不能扩展到坐标、连接、材料或工程定义。

## 求解器无关的几何与网格核心

共用几何／拓扑逻辑与 deck 适配分层维护：

- 几何身份、分块关系、整数边分段、源／目标／侧壁对应和格点拓扑可按一致的输入输出契约复用。
- 实体类型、卡片字段、属性与材料引用、单元公式和阶次、导入导出格式属于 deck 适配。
- 质量算法、阈值、时间步和分析要求按目标 deck 与项目准则核对，不随几何逻辑自动共用。
- 扩展 deck 时核实实际差异，不能通过删除执行门禁代替适配；分别记录 ANSA 原生能力、MCP 可调用能力和已验收范围。

共同模型基础与跨求解器互操作的产品背景见 [ANSA 产品资料](https://www.beta-cae.com/ansa.htm)、[Cadence ANSA 产品页](https://www.cadence.com/en_US/home/tools/engineering-simulation/ansa-preprocessor.html)及[官方手册概览](https://www.beta-cae.com/brochure/ansa_brochure.pdf)。各工具当前支持范围仍以仓库契约为准。

## 新增或修改工具

开始前明确目标、修改范围、最小验收和停止条件，检查 Git 状态并保留已有改动。工具变更同时检查：

- MCP 参数和结果 schema。
- `ansa_mcp_shared/operations.py` 中的读写、模型变更和 deck 分类。
- 桥接分派、执行前门禁、备份、配置恢复、失败回读及原操作幂等。
- `scripts/stage_native_release.py` 的显式源码清单。
- [工具参考](tools.md)及对应专项契约。

几何及网格操作的输入边界分别见[结构化网格](reference/structured-meshing.md)、[关联范围与冻结](reference/mesh-scope.md)、[已有边界编辑](reference/perimeter-edit.md)、[已有面网格重划](reference/surface-remesh.md)和[质量工作流](reference/mesh-quality.md)。使用案例用于发现缺口和限定验证，通用工具中不写死案例编号、尺寸或分块方案。

## 最小验证

**默认只执行与本次改动直接相关的最小测试集。** 执行前简述要验证的行为、所选检查和通过条件。

- 优先复用已有用例和夹具，从具体测试方法开始，必要时才扩大到测试类或模块。缺陷修复仅补缺失的最小复现，新功能仅补主路径和确有风险的失败分支；不穷举参数或重复实现细节。
- 按证据缺口选择验证层。离线测试用于 schema、参数和状态行为；模拟 MCP 用于通信和持久任务；原生小夹具用于真实 API 效果。无需每次逐层跑齐或重复相同断言。
- 只有用户要求、共享契约影响、实际失败或可定位的未解风险，才扩大范围并说明原因。默认不运行全目录回归、跨 deck、压力或工业模型验收。
- 最小验收通过即停止。仅在相关实现、依赖、运行条件变化或新证据使原结果失效时，重跑受影响检查；失败先定位，再定向复验。
- 纯文档、注释和不改变行为的低影响修改只检查内容、链接及差异格式，不新增自动化测试，不运行模拟或原生验收。模型写入所需的目标、范围、备份和独立回读始终保留。

定向单测命令模板：

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
.\.venv\Scripts\python.exe -B -X utf8 -m unittest 'tests.unit.<模块名>.<测试类名>.<测试方法名>' -q
git diff --check -- <本次修改的文件路径>
```

## 模拟与原生验收入口

以下为按需选用的入口，不构成每次开发的必跑清单。模拟检查也使用全新的用户私有运行目录：

```powershell
$smokeHome = Join-Path $env:USERPROFILE ('ANSA-MCP-Plugin-Runtime\smoke-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/simulated_mcp.py --runtime-home $smokeHome
```

多客户端竞争与共享后端分别使用 `duplicate_client_mcp.py`、`shared_backend_mcp.py`；各自使用新的 `--runtime-home`，按对应生命周期模式核对。模拟模型效果不能替代 ANSA 原生验收。

| 入口或分组 | 用途 |
| --- | --- |
| `stage_three_ansa.py` | 准备、绑定与核验独立 ANSA 实例 |
| `geometry_features_ansa.py`、`geometry_boundary_ansa.py` | 几何特征、辅助边界与独立回读 |
| `cons_seed_ansa.py`、`mapped_surface_ansa.py` | 原始边界整数分段与四边映射 |
| `cad_map_ansa.py`、`map_block_prepare_ansa.py`、`map_block_fill_ansa.py` | 有限 CAD Map、MapBlock 准备与填充 |
| `annular_freeze_mcp_ansa.py`、`annular_map_fill_ansa.py`、`annular_sweep_ansa.py` | 有限孔环的源面、冻结、填充与扫掠 |
| `mesh_scope_ansa.py`、`perimeter_edit_ansa.py`、`surface_remesh_ansa.py` | 关联范围、冻结、边界编辑与已有面网格重划 |
| `quality_workflow_ansa.py`、`quality_templates_ansa.py`、`quality_mcp_ansa.py`、`quality_large_ansa.py` | 原生质量、模板、公开 MCP 及分页报告 |
| `stage_three_ribbon_lifecycle.py` | 所属窗口的原生面板生命周期 |
| `stage_four_external_install.py`、`stage_four_native_install.py` | 独立安装产物的外部服务与原生入口 |
| `cad_domain_offline.py`、`annular_boundary_offline.py` | 离线 CAD 清单和孔环边界审计 |

从 `tests/integration/` 调用入口前，阅读其 `--help` 和输入要求。部分入口使用已准备的实例及指定检查点，不能把旧报告目录作为新验收覆盖，也不能将整段入口作为失败重试器。离线 CAD 清单审计所需 Gmsh 使用独立 Python 环境，不安装到 ANSA 或正式运行环境。

CAD Map 明确传入现有隔离模型的绝对路径：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/cad_map_ansa.py "<owned-run-dir>" --input "<原始盒体.ansa绝对路径>" --from-cad
```

普通及恢复运行也需 `--input`；只读 `--finalize-persistence` 不接受该参数。路径在连接和模型任务前校验，报告记录来源。几何、实体编号和属性等有限夹具前提仍需按脚本契约准备。

孔环扫掠在保存前和重开后保留完整快照及原生 Part 重算证据，仅在现有持久性核验通过后设置完成状态。原始差异单独记录；已有派生量舍入规则不放宽节点、连接、CAD 或材料的保护。

原生验收执行前核对唯一 translator、PID、进程启动时间、完整参数、会话和模型上下文；仅写入已确认归属的独立模型。失败或超时后先查询原任务与执行记录。报告分别说明离线、模拟 MCP、真机回读、保存重开及所属窗口画布的证据范围。

## 运行配置

ANSA 进程可通过 `ANSA_MCP_SCOPE_EVIDENCE_MAX_BYTES` 调整完整 scope 快照编码预算：默认 16 MiB，允许 256 KiB 至 64 MiB 的整数字节数，非法值在读取模型前拒绝。它不改变显式专项预算、10000 实体范围上限或 1 MiB TCP 帧上限；完整证据仍通过摘要、分页或文件使用。

例如，在启动 ANSA 前的 PowerShell 环境中设置：

```powershell
$env:ANSA_MCP_SCOPE_EVIDENCE_MAX_BYTES = [string](64 * 1MB)
```

## 文档与发行维护

[工具参考](tools.md)维护能力入口与契约链接；实际 schema 由服务提供。迁移映射保存在 `docs/tool-mapping.json` 和 `docs/reference/legacy-mapping.md`，修改映射后运行 `scripts/update_tool_mapping.py`。版本变化写入[更新记录](../CHANGELOG.md)，验收条件、结果和未覆盖范围写入[验证范围](acceptance.md)，开发指南保留可持续使用的流程与规则。

功能提交包含正式源码、使用与维护文档、必要构建脚本及可复用测试。移动或归档文件前，核对静态导入、动态加载和文档引用；需要时提取最小共用助手，避免本机残留掩盖提交缺失的依赖。暂存使用明确路径，不将全部未跟踪文件自动纳入提交。

源码开发入口、外部 wheel、原生包构建和回退见[安装说明](installation.md)。正式源码、开发配置与运行控制数据分别管理，发布产物进入 Releases；构建与验收规则见[发布说明](releasing.md)。
