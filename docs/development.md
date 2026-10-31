# 开发说明

开发环境使用 Windows 和 64 位 CPython 3.12。普通源码、文档和单元检查不需要启动 ANSA；真实 API 验证需要已授权的 ANSA 2026.1.0 安装。

## 几何前处理第一步：检查对称性

几何清理、切分、分块和网格设计均先检查镜面对称、旋转/周期对称以及轴对称或回转特征：只读核对候选对称面、旋转轴、最小周期角、重复份数，以及对应外形、孔道、厚度、局部特征和实体关系。记录证据、使用的容差及未知项，不以包围盒、外观相似或局部圆孔代替整件核验。原生能力与已有 MCP 工具的使用顺序仍按下节执行。

旋转/周期对称确认后，任务允许时先划分最小代表扇区，再按已核实的旋转轴和周期角复制；同时验证周期边界节点/单元面对应、全部交界连接、方向和重复。连续回转几何、离散旋转周期与镜面对称分别判断，并可按实际几何组合复用。

镜面对称须继续核对多个候选面是否共同适用于整件：两个正交面可先划分1/4，三个正交面可取1/8。记录各平面、代表区域和依次镜像的顺序；非正交镜面或与旋转周期组合时，依据已核实的重复关系选择最小代表域，不机械按对称面数量折半。

确认几何对称且任务允许时，优先采用“完整备份 → 明确保留侧与对称面 → 删除另一半 → 完成并验证半模型 → 镜像 → 验证对称面连接、单元方向、重复及保存重开”的流程。材料、连接和已知载荷/约束另行判断；几何网格可镜像不等于可用半模型求解。未确认时保留完整几何，不擅自删半。本要求不新增自动识别或自动删半工具，也不放宽现有范围和备份保护。

## 原生能力优先

用户2026-10-26明确的开发范围：几何和网格核心按求解器无关方式完善；不新增跨deck专项验证或求解器工程验收阶段。必要deck实体/卡片读写差异作为底层适配细节，仍须正确读写。网格能力按几何保持、覆盖完整、接口共形和单元质量验收。

复杂实体网格的当前矩形多块路线及客户端检查点库见[复用流程](reference/complex-solid-meshing.md#多块路线的当前落地)。生成算法继续调用原生MapBlock/VolumesRemesh；新代码负责有界预检、全范围核验、持久回执和公开工具编排。恢复流程只查询原任务，不自动重放不确定写入。派生显示量的小舍入须先独立重算，再限定允许比较字段，保留原值；不把原节点坐标或连接纳入归一化。

复杂块组的方案质量、视频决策迁移、执行复用及验证粒度见[方案判断与局部质量](reference/complex-solid-meshing.md#方案判断与局部质量)及其后续小节。内部`meshing_context.run_with_context(..., element_type="Quad")`可显式选择`Quad`、`Mixed`或`Tria`，默认值保留既有映射流程；结果记录`requested_element_type`和`timings_ms`的capture/apply/callback/restore/total阶段。非法类型在原生配置捕获前拒绝；原生回读不一致时不执行回调，恢复机制保持。定向入口为`python -m unittest tests.unit.test_meshing_context -v`（`PYTHONPATH=src`）。该源码改动未新增公共MCP参数；部署/进程重载及新版入口真机验证分别确认，不能用单元通过代替。

孔环直扫复用原生 MapBlock 的面网格和 `VolumesExtrude.translate`；`ansa_mesh_sweep(freeze_source=true)` 在新备份后调用 `base.FreezeElements`，独立核验冻结字段、全范围变化及源面指纹后才生成实体。`annular_source.py` 只审计实际线性 CTRIA3/CQUAD4 环带的拓扑、嵌入、内外环及弦误差，不生成网格，也不证明任意原始 CAD 连续域。定向回归为 `test_annular_source`、`test_structured_native` 和 `test_structured_meshing`；真机入口 `tests/integration/annular_freeze_mcp_ansa.py --help` 只操作新建独立实例及显式准备模型，`annular_boundary_offline.py` 从独立捕获重新推导完整外皮和孔壁，并将选定质量结果绑定到同一源面/实体指纹。

受控源面准备位于 `annular_map_prepare.py`，公开 `ansa_mesh_annular_source_prepare(action="plan"/"apply")` 复用主线程台账和完整配置恢复。它限定已明确四块的局部CAD，不自动识别孔；全局目标长度允许原生在局部细化，实际perimeter点数和所属SHELL节点独立核对。逐CONS及逐FACE的原生点投影使用调用方明确的几何近似预算；源环参数数值容差、原卡片定义、原节点、CAD导出哈希和显示细分量分别处理，完整当前契约见[工具参考](reference/structured-meshing.md#有限孔环源面准备)。定向回归另含 `test_annular_map_prepare` 和 `test_annular_boundary_offline`。

需要增加任何几何、网格、检查或模型操作功能时，先确认 ANSA 本身及仓库已有实现能否完成，再决定开发方式，减少重复造轮子。

1. 先查当前版本的官方功能说明、Python API 和项目既有工具，记录候选接口、输入输出及限制。使用本仓库 `ansa-python-api` 技能；文档未找到不等于产品没有该能力。
2. 原生能力可用时优先直接调用。若只是 MCP 尚未暴露，补充接口或做薄封装，复用原生算法；按实际风险增加目标/范围校验、备份、结果回读和错误处理。
3. 文档含糊、版本行为不一致或返回值不足以确认效果时，在独立小夹具验证，不用猜测代替能力判断，不在用户工作模型上试错。
4. 只有确认原生能力及现有实现不能满足需求时，才编写必要的自定义算法或引入外部工具。在方案或实施记录中说明已查的原生接口、不能满足的具体条件，以及自研范围；原生已覆盖部分继续复用。

例如 CONS→CURVE 转换应复用 `base.ConsToCurves`，偏差投影应复用 `calc.ProjectPointToCons`。MCP 负责调用编排和必要的核验，不另写一套同功能转换或投影算法。原生函数存在不等于其精度、范围或失败语义已满足需求，仍须按契约验证。

前一阶段复用原生评估/投影的有界诊断位于 `geometry_fidelity.py`，关键反例见 `test_geometry_fidelity.py`、`test_geometry_boundary.py`，真实 MCP 入口沿用 `tests/integration/geometry_boundary_ansa.py`。该阶段记录为 752 项单元、83 文件 staging 和 20 次独立原生任务，具体适用范围见[验收记录](acceptance.md#2026-10-25-原生能力优先与转换偏差诊断受限验收)。

原始 CAD 的整数边分段直接复用 `mesh.NumberPerimeters`，实现位于 `cons_seed.py`，专用入口为 `ansa_mesh_seed_read` / `ansa_mesh_seed_apply`。2026.1.0 真机确认 `CollectEntities(CONS, GRID)` 会实例化节点，因此只读及 preflight 禁止使用该组合；真实点核验延后至已验证备份的写阶段。新增原生调用既要查签名，也要验证这种隐藏副作用。

四边源面复用原生 `mesh.Create4SidedMesh`，通过既有 `ansa_mesh_surface(method="mapped_existing_seeds")` 接通整数 CONS 种子。原生设置显式关闭重新间距及三角形拆分；`mapped_surface.py` 负责备份、范围、数值恢复和独立回读，`mapped_surface_topology.py` 只审计实际格点连接，不生成网格。定向入口为 `test_mapped_surface_mcp`、`test_mapped_surface`；真机入口 `tests/integration/mapped_surface_ansa.py <new-owned-run-dir> <i10o_simp.step>` 要求独立实例和未使用过的报告目录。契约及连续几何/体网格边界见[结构化网格说明](reference/structured-meshing.md#保留整数-cons-种子的四边映射源面)。

有限直扫候选的拓扑诊断位于 `cad_sweep_topology.py`，直接使用已有 schema 2 几何见证，由 `ansa_geometry_features(sweep_options=...)` 暴露。它只验证显式六面域的原生 ID 和角点对应，不生成网格、不替代 ANSA 原生 Map 或连续 CAD 验证。定向验证为 `test_cad_sweep_topology`、`test_geometry_features` 和 `test_structured_meshing`；实际生成路径通过下述公开 CAD Map 验收入口核对。历史任务先查执行记录，不重放未知写入。

受控实体入口 `ansa_mesh_cad_map` 在 `cad_map.py` 编排原生 `VolumesMap`，`cad_sweep_geometry.py` 只证明受限 IGES 支撑/修剪域，`cad_map_mesh.py` 只审计实际实体占位和采样积分。新增原生网格算法仍优先复用，不把验证模块当成替代网格器。`test_cad_map`、`test_cad_map_mesh`、`test_cad_sweep_geometry` 与结构化网格测试覆盖完整卡片、斜向参数幂等、预算及异常不重试。真机入口 `tests/integration/cad_map_ansa.py <owned-run-dir> --from-cad` 针对本阶段已核实归属的六面盒夹具，串行执行 CAD→分段→六面四边映射→公开 plan/apply→保存重开；不是任意工业文件的通用自动重划入口。契约见[有限六面 CAD Map](reference/structured-meshing.md#有限六面-cad-map)。

只读 `ansa_geometry_sweep_candidates` 把最多 100 个显式 FACE 的全部 CONS 连通组件分类，复用 `cad_sweep_topology` 与 `cad_sweep_geometry`；`cad_sweep_candidates.py` 只做有限预筛和分段建议，`geometry_sweep_candidates.py` 负责主线程导出、缓存与证据新鲜性。官方 `mesh.MapBlock(macros)` 已能自动确定 caps/sides，但也会生成表面网格，不能当只读探测；`VolumesDetect` 则要求已有表面网格，返回形式参数不提供无副作用保证。下一步需要原生自动分块时应优先薄封装这些写接口，增加范围、备份和回读，而不是将本预筛扩展成替代原生分块器。

定向测试为 `test_cad_sweep_candidates`、`test_geometry_sweep_candidates` 和结构化工具 schema/分派测试。真实入口：`tests/integration/sweep_candidates_ansa.py <new-owned-run-dir> --box <saved-synthetic-box.ansa> --samples <i10.step> <i02.step> <i23.step>`。先按 stage-three 准备并独立启动唯一 translator；入口要求未使用的报告路径。失败后查询原任务，不重跑整个入口。样本路径不嵌入实现，工业模型只用于本机验证，不纳入发布内容。完整契约见[只读扫掠候选](reference/structured-meshing.md#只读扫掠候选)。

`map_block_inspection.py` 与公开 `ansa_mesh_map_block_inspect` 固化已有体的状态、端盖/侧壁、CAD 和面网格回读。`tests/integration/map_block_inspect_ansa.py` 在新实例读取已保存结果并验收分页。历史持续性差异通过原任务、证据和独立回读诊断，不能重放 MapBlock。严格保存重开失败保留，不因 Part 显示汇总的微小差异自动放宽生产保护。

2026-10-26 首版专用写入口 `ansa_mesh_map_block_prepare(action="plan"/"apply")` 由 `map_block_prepare.py` 薄封装原生 MapBlock；`meshing_context.py` 负责当前全部导出配置的指纹与异常恢复，`part_aggregates.py` 负责原生 Part 汇总重算及窄范围持续性比较。不要把 `CalcElementMass(Part)` 的混合汇总宣称为解析 FE 质量，也不要把显示摘要的材料名称当作材料定义。目标 VOLUME 必须显式绑定已有 PSOLID，原生自动新建的默认材料只在独立确认属于本次增量后安全清理。

已有 MapBlock 填充复用原生 `mesh.VolumesRemesh([volume])`，实现为 `map_block_fill.py`，纯范围差异审计为 `map_block_fill_audit.py`。保留原 VOLUME、SHELL、GRID 及材料，不重新创建或释放体。端盖/层数直接读取当前原生块后独立证明，复用 `cad_map_mesh.verify_translation`、`structured_boundary` 和 `mesh_audit`；这些模块核验实际网格，不生成替代网格。新节点必须检查 CD/PS/SEID；异常观察先核对原模型上下文，来源记录失败必须同步 recovery_required。

显式Edit Caps复用 `mesh.DefineCaps(volume, Cap1, Cap2)`，由 `ansa_mesh_map_block_caps(plan/apply)` 暴露；成功返回None后仍须独立核验完整master/slave/round、其他块、CAD、FE和全局配置。该函数缺于本机2026.1.0静态stub，合同来自同版本隔离主线程探针，不按其他函数签名猜测。定向测试为 `test_map_block_caps`；实际1732面单元副本的公共MCP与保存重开已验证，范围见[工具参考](tools.md)。

复杂实体通常先切分，再Define Map Block初划；需要时用Surface Mesh重划局部面并Freeze，按几何调整caps后填充，详见[通用顺序](reference/complex-solid-meshing.md#通用原生划分顺序)。验收用实际生成的完整、有界SOLID清单核对质量，不把某个旧夹具的610等单元数写成通用前置条件。文档、回执或图形显示收尾不得触发重新生成已有成功网格。

单孔矩形板流程由 `ansa_mcp_shared.plate_hole` 推导参数、整数分段与预算，`plate_hole_workflow.py` 负责原生调用及模型回读，客户端 `plate_hole_script` 只生成现有 `ansa_script_execute` 所需的固定脚本。完整有限契约与入口见[孔板流程](reference/plate-hole-workflow.md)。名义9 mm边的原生端点约1e-6误差应匹配声明的9 mm分段，不从带误差的长度直接ceil；不调整节点坐标。定向入口为 `test_plate_hole_recipe`、`test_plate_hole_workflow` 及一个 `plate_hole_workflow_ansa.py` 独立MCP案例。

定向测试为 `test_map_block_fill`、`test_map_block_fill_audit` 和结构化工具测试。公开链路 `tests/integration/map_block_fill_ansa.py <new-owned-run-dir> --cad <saved-raw-box.ansa>` 从原始合成 CAD 执行准备、填充、只读质量及保存重开。该入口用于隔离夹具验收，旧报告存在即拒绝整段重跑。完整边界见[已有 MapBlock 的受控填充](reference/structured-meshing.md#已有-mapblock-的受控填充)。

显式四块孔环通过同一fill入口的 `annular_options` 分派到 `annular_map_fill.py`，一次原生VolumesRemesh复用已准备边界；矩形默认契约不变。复用多块范围审计的显式CPENTA选项和通用外皮/接口审计，不复用矩形AABB或直扫层对应。新增定向入口为 `test_annular_map_fill`；公开验收 `tests/integration/annular_map_fill_ansa.py --ansa-root <ANSA目录> --input <已保存的未填孔环模型> --options <显式JSON参数>` 在新实例验证填充、独立质量和保存重开。`complete`表示该接口与持久性核验结束，另看 `quality_accepted` / `acceptance_complete`，不得把原生填充成功当成质量通过。

`tests/integration/annular_map_repair_ansa.py` 复用上述实例管理入口，对已保存610实体的明确夹具执行局部原生平滑；使用相同 `--ansa-root`、`--input`、`--options`，参数与scope在测试文件中明确列出，不代表自动选择任意工业修复范围。质量改善通过既有专用MCP工具，范围推导与额外证据由测试脚本采集；不是新增网格算法。未全部合格时保留独立实例供诊断，须核验归属后关闭；不得重放未知任务。

定向入口为 `test_map_block_prepare`、`test_meshing_context`、`test_part_aggregates` 与 `test_structured_meshing`。真机入口 `tests/integration/map_block_prepare_ansa.py <owned-run-dir> --raw <saved-unmeshed-box.ansa>` 只支持隔离实例及限定盒体；工具本身只接受已验证的完整六面域。历史失败须查询原任务、保全现场，再通过公开只读入口回读，不重放生成器。新用例须从已验证并保存的状态显式建立，不能把整个验收脚本当失败重试器。完整配置恢复文件始终保留；具体覆盖范围见[受控 MapBlock 准备](reference/structured-meshing.md#受控-mapblock-准备)。

`scripts.execute` 在执行一次后对大于 128 KiB 的完整执行包复用结构化证据存储，避免已有 1 MiB TCP 上限被大 RESULT 冲断；长字符串切片和 JSON Pointer 边界在 `structured_evidence` 验证。`test_script_result_paging` 覆盖单次执行、UTF-8 字节预算、嵌套列表、超大标量、页封装临界值、长键路径、模型代次刷新和存档失败。源脚本成功与结果可传输分别验收；旧 oversized unknown 仍先查原 Journal 和独立模型，不新建 operation_id 重放。

工业样本按源文件 SHA256 关联原生调查；STEP 类型 token 和单位提示不是 CAD 解析、模板判型或网格验收。B-spline token 也可能来自直线或平面表示，不能仅按名称自动排除几何。源 CAD 不写入、不复制到发布包；临时目录分析和报告汇总工具留在本机开发档案。正式分段真机入口为 `tests/integration/cons_seed_ansa.py <new-owned-run-dir> <sample-root>`，按 stage-three 准备并核对独立实例；该入口拒绝重放已有验收报告。

## 求解器无关的几何与网格核心

ANSA 官方产品资料将几何处理、壳/体网格和多求解器 pre-processing decks 放在统一的前处理环境中；Cadence 也明确描述了跨求解器互操作和共同模型基础。这支持把可复用的几何/拓扑工作与 deck 适配分层，但不表示所有 deck 使用相同的网格算法、单元、参数或质量准则。当前 MCP 在单个 deck 的验证只能界定本仓库工具的支持和验收范围，不能反推 ANSA 原生功能范围。

开发时遵守以下区分：

- 几何拓扑、分块、边界整数分段、源/目标/侧壁对应及规则格点关系可作为共用核心；只有输入、目标单元要求和原生 API 契约一致时才复用同一网格生成调用。不要只因 deck 不同复制几何判断或计划逻辑；对确有差异的网格参数和输出类型，应显式放入适配层。
- 核心数据尽量使用几何身份、节点坐标和 `quad4/tria3/hexa8/prism6` 等拓扑描述。求解器实体与卡片字段、属性/材料引用、单元公式与阶次、导入导出格式属于 deck 适配范围。
- 质量计算的几何量可能可复用，但检查算法、准则、项目阈值、时间步和单元公式等分析要求可能不同。不得因共用几何或网格逻辑而自动共用质量配置；按目标 deck 和显式项目准则核对。
- “当前仅支持/已验收 NASTRAN”等说明，应明确属于 MCP 卡片适配、执行门禁或验证范围；不能解释为 ANSA 无法在其他 deck 使用映射、扫掠等原生网格能力。
- 不以直接删除 deck 校验代替跨求解器适配。扩展 deck 前，核实实体类型/字段、属性和材料归属、单元要求、导出及质量语义，并按目标工具的验收契约验证；软件原生共用能力、MCP 可调用能力和已验收范围分别记录。

例如，本机 ANSA 2026.1.0 官方 API 自动补全索引中的 `ansa.mesh.Create4SidedMesh` 和 `ansa.mesh.VolumesExtrude.translate` 签名没有 `deck` 形参；后者仍接收实体和 `property` 等输入。这只说明签名未显式接收 deck 选择，不证明不同 deck 的实体、参数或结果相同。当前 NASTRAN `GRID/PSHELL/PSOLID` 的读写保护仍属于受限适配，不能原样套用到其他 deck。

依据：[BETA CAE ANSA 产品页](https://www.beta-cae.com/ansa.htm)列出壳/体网格及多个预处理 deck，并说明 deck 间模型修改与材料同步；[Cadence ANSA 产品页](https://www.cadence.com/en_US/home/tools/engineering-simulation/ansa-preprocessor.html)明确使用“Solver-Independent Model Interoperability”和“Common Modeling Foundation”，同时提及 solver-dependent integrity checks；[官方 ANSA brochure](https://www.beta-cae.com/brochure/ansa_brochure.pdf)说明共同模型基础和跨求解器互操作。证据范围是产品能力与定位，不证明所有 deck 的网格算法、单元公式、质量配置或 MCP 支持范围相同。

## 环境

在仓库根目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-runtime.lock
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

运行依赖由 `pyproject.toml` 声明，Windows/Python 3.12 的精确版本集在 `packaging/requirements-runtime.lock`。开发依赖补充构建所需的 setuptools 和测试所需的 packaging。当前运行锁没有下载哈希，不承诺跨环境逐字节重现。

ANSA 进程可设置 `ANSA_MCP_SCOPE_EVIDENCE_MAX_BYTES` 调整整模型 scope 证据预算；未设置时为 16 MiB，只接受 256 KiB 至 64 MiB 的整数字节数，非法值在读取模型前拒绝。显式 `budget` 和 1 MiB TCP 帧上限不受此变量影响。

例如，在启动ANSA的PowerShell环境中设置 `$env:ANSA_MCP_SCOPE_EVIDENCE_MAX_BYTES = [string](64 * 1MB)` 可使用64 MiB；不设置即可使用16 MiB。预算限制完整快照在主线程内的编码大小，快照仍通过现有摘要或证据文件使用，不扩大消息帧。10000实体范围上限和各专项显式预算保持独立。

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

功能提交保留正式源码、面向使用与维护的文档，以及可重复执行的回归测试；必要构建脚本和使用示例分别归入源码与文档。一次性探针、失败补验、特定历史任务恢复和临时报告汇总工具留在已忽略的本机开发档案。移出前先检查测试导入和动态加载，清理文档引用，不能让本机残留文件掩盖提交缺失的依赖。

结构化网格的纯算法/适配测试位于 `test_mesh_audit.py`、`test_mesh_planning.py`、`test_mesh_provenance.py`、`test_structured_meshing.py` 和 `test_structured_native.py`。独立真机入口为 `tests/integration/structured_meshing_ansa.py <owned-run-dir>`；先按 stage-three 准备独立启动目录并核对归属。`--resume` 只从明确已结束的测试任务及已保存夹具恢复，不能对结果未知的写操作重放。运行记录保留在私有运行目录，本机维护证据不随源码发布。

离线实体筛选和原生分批的风险回归见 `test_solid_overlap.py`、`test_solid_overlap_batches.py`、`test_intersections.py`。CAD 清单的哈希与完整性门禁见 `test_cad_domain_manifest.py`。CAD 内核放在单独虚拟环境，安装 Gmsh 4.15.2 后执行下列入口，不要把该依赖放进 ANSA Python 或正式桥接运行锁：

```powershell
# 使用独立 CAD 环境的 python；输出位置必须尚不存在。
python -B -X utf8 tests/integration/cad_domain_offline.py --output-dir runtime/verification/cad-domain-example
```

该回归入口独立构造带孔平面并验证完整、缺片、重复、多余四种情况；调用方已导出的显式分组可使用正式模块 `cad_domain_audit.audit_manifest` 检查，契约见[结构化网格说明](reference/structured-meshing.md#cad-分割完整性的离线复用)。离线检查不负责原生 FACE 导出、目标绑定或自动分块。

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

重复客户端回归使用另一全新用户私有目录：

```powershell
$duplicateHome = Join-Path $env:USERPROFILE ('ANSA-MCP-Plugin-Runtime\duplicate-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/duplicate_client_mcp.py --runtime-home $duplicateHome
```

该检查验证占用期间的真实 stdio 初始化、91项工具发现、明确占用错误、原会话恢复和恢复后未绑定状态，不连接用户 ANSA 模型。

上述历史流程使用 `--direct-backend` 验证独占后端生命周期。默认共享后端使用以下独立验收：

```powershell
$sharedHome = Join-Path $env:USERPROFILE ('ANSA-MCP-Plugin-Runtime\shared-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/shared_backend_mcp.py --runtime-home $sharedHome
```

验证两个真实 stdio 客户端共用后端、同操作只执行一次、前端退出后绑定保留、后端重启后要求重新绑定和历史任务可查询。模型调用由模拟桥接执行，不代表 ANSA 图形验收。

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
| `quality_workflow_ansa.py` | 明确归属的独立 ANSA 主线程，壳／实体 × 线性／显式的质量修复及保存重开 |
| `quality_templates_ansa.py` | 四模板原生主路径，ABAQUS S4R/C3D8R 与 LS-DYNA 壳／实体，原生问题、时间步、修复及保存重开 |
| `quality_mcp_ansa.py` | 四模板公开 MCP→TCP→ANSA 主线程质量链路，LS-DYNA 限定门控、旧模型上下文拒绝、位移回滚及独立回读 |
| `quality_large_ansa.py` | 独立2601个壳单元，经公开MCP分批报告和分页完整ID核对；确认网格与全局准则不变 |
| `stage_four_external_install.py` | 已安装外部服务的无源码启动 |
| `stage_four_native_install.py`、`stage_four_native_followup.py` | 独立原生安装与所属窗口续验 |

从 `tests/integration/` 调用这些入口，先读其 `--help` 及准备/续验条件。续验脚本依赖原实例和原任务，不能把旧报告目录作为新验收反复覆盖。主线程、目标归属及报告分层规则见[架构](architecture.md)。

`quality_workflow_ansa.py --identity <remote_basic.json> --port <port> --run-dir <新目录>` 要求与 `stage_three_ansa.py` 独立启动参数完全匹配的 PID 和 `program_args`，每次写入前在 ANSA 主线程重新核对。入口会清空该独立夹具并创建新模型；不得传入用户工作进程的身份。输出按四组分别记录原生回包、备份、独立复查和保存重开。它直接验证生产操作适配器，不等于三项新工具已完成 MCP 至原生 GUI 的全链路验收。

`quality_templates_ansa.py --verify-gui-location` 同时逐准则核对定位集合、完整 FE 快照及汇总数量，并保存原生画布；`--require-native-fix` 要求 Fix Quality 本身改善，不能用平滑替代。公开 MCP 与 LS-DYNA 写入门控的实际验收范围见[验证记录](acceptance.md)。

在同一已核验独立实例完成原生夹具后，运行 `quality_mcp_ansa.py --run-dir <stage-three准备目录> --identity <remote_basic.json> --fixtures <该准备目录内的原生夹具目录>`。入口核对唯一 translator、PID 与完整参数；它自行启动本轮私有 MCP 后端与桥接，不启动或停止 ANSA。只对该目录中的合成模型执行操作，输出新验证目录。结束后另行核对归属并关闭独立实例；不能指向用户工作进程。

`quality_mcp_ansa.py` 的壳夹具省略可移动节点以验证自动推导；实体夹具固定中心节点，`max_iterations=4` 验证一次任务内续修及累计位移。`quality_large_ansa.py --run-dir <同一准备目录> --fixtures <原生夹具目录>` 在所属实例创建独立2601单元合成模型，通过新增 `ansa_mesh_quality_page` 逐页核对完整明细；2601是验收规模，不是产品上限。两个入口串行运行，不与同一模型的其他写操作并发。

## 文档与发行维护

`quality_templates_ansa.py` 可用 `--require-native-fix` 要求 Fix Quality 自身改善并恢复准则；`--verify-gui-location` 在独立 GUI 先定位并回读可见集合、保留原生快照。仅使用已核验归属的 identity 和新的输出目录；出现 `recovery_required` 时停止后续夹具。改善不等于全部通过，画布与 MCP 全链路的验收口径见[验证范围](acceptance.md)。

`docs/tools.md` 维护当前工具分组；`docs/tool-mapping.json` 和 `docs/reference/legacy-mapping.md` 保留功能迁移对应。修改映射后运行 `scripts/update_tool_mapping.py`，不依赖本机原始报告。

2026-10-24 工作源码包含 10 个结构化网格工具（共 79 项），其范围与已验证边界见[结构化网格契约](reference/structured-meshing.md)。历史 69 工具模拟记录不覆盖这批工具，也不代表其真机或发行验收。

2026-10-25 工作源码最初新增只读 `ansa_geometry_features` 注册，当时共 11 个结构化网格工具、80 个 MCP 工具。接口 schema、桥接分派和原生模块已落盘；711 项单元回归、80 工具 stdio/TCP 模拟、7 份真实 IGES 样本解析及 80 文件原生 staging 均通过。`geometry_features_ansa.py` 在独立 ANSA 2026.1.0 中完成 18 次真实 MCP 任务：特征/分段、缓存、同 epoch 几何变更拒绝旧证据、读取前后卡片/Part/FE 一致及保存重开；源码哈希在当轮核对通过。

同日后续 CONS 闭环阶段扩展同一入口，`feature_schema=2` 绑定原生 HOT POINT 编号、坐标和双向邻接；解析状态与拓扑状态分开。新增纯 `geometry_contours.py`，先检查输入和全范围同编号坐标一致性，再按原生顶点身份追踪闭环；不按近似坐标合并。该阶段 726 项单元、80 工具模拟和 81 文件 staging 通过；更新后的 `geometry_features_ansa.py` 完成 26 次真实 MCP 任务（含两次预期拒绝），覆盖边界平移、只读 HOT POINT 指纹和保存重开。源码哈希在当轮核对通过，详见[验收范围](acceptance.md)。

此前阶段增加 `ansa_geometry_boundary_prepare`，该阶段共 12 个结构化网格、81 个 MCP 工具。定向入口 `test_geometry_boundary` 覆盖备份、空 Part、scope、返回值与实体差异、None 集合和两次写入间上下文失效；`test_structured_meshing` 覆盖对应 ledger 的恢复阶段。`geometry_boundary_ansa.py <新owned-run-dir>` 要求新建独立 ANSA，完成 20 次真实 MCP 任务及保存重开，结束关闭自建实例。首次失败目录不重放；来源哈希包含数据库/epoch，跨重开使用新指纹，并独立比较原生卡片和 CONS 记录。745 项单元、81 工具模拟及 82 文件 staging 通过，当前转换来源验收与原 CAD 等价性边界见[验收范围](acceptance.md)。

通用流程见[复杂实体网格复用流程](reference/complex-solid-meshing.md)。`mesh_seed.py` 仅计算调用方声明尺寸的整数分段；`structured_batch.py` 一次读取已就绪源面，先形成共享 GRID 的不可拆组件，再按现有预算合批。定向回归入口为 `test_mesh_seed` 和 `test_structured_batch`，涵盖 3000 边报告规模、悬挂节点及命名顺序不影响合批可行性。新增真机入口 `tests/integration/meshing_reuse_ansa.py <新建的owned-run-dir>` 使用两个缩放/层数不同的合成夹具，首次失败目录不得直接重放。SaveAs 后显式重开并更新上下文，才能将当前文件作为后续指纹保护基线。

`structured_boundary_ansa.py --owned-run-dir <新建且已核实的准备目录> --keep` 验证可选 CAD 模式：单 FACE 三角网格、CPENTA 扫掠、原生 SHELL 独立回读、全部节点投影及分页、移动 0.1 后成员保留且投影失败、保存重开的 CAD/FE 归属一致。入口复用独有 translator、PID、完整启动参数和主线程保护；不得传用户工作进程。默认保留自建实例，完成后用 `--cleanup` 精确核对并关闭。失败任务与报告不得覆盖或直接重放；需先确认任务终止、备份并恢复自建基线。本轮有明确终止失败后的独立恢复报告，不能把首次失败记录当作最终通过。

源码、测试、公开文档与构建输入在同一仓库维护。个人智能体配置（`.agents/`、`AGENTS.md`）、缓存和本机记录保持忽略。发行产物进入 Releases 附件；发行检查见[发布说明](releasing.md)。
