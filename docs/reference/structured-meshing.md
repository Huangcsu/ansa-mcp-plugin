# 结构化网格工具契约

本文描述当前工作源码的 22 个结构化网格工具。首期 8 个工具后增加尺寸/曲率种子规划、多块合批预检、显式 CAD 特征读取、受控 CONS 辅助曲线转换、原生 CONS 分段读取和写入，以及有限六面 CAD Map、只读扫掠候选分类、原生 Map Block 准备/填充、显式caps设置和有限孔环源面准备。各阶段受限验收及未覆盖范围见[验收记录](../acceptance.md)，不代表任意工业几何整件网格或发行包验收。

## 通用调用约定

这些工具都要求 operation_id、expected_target、expected_model 和正整数 deck；ttl_seconds 缺省为 300，范围为 0–86400。目标和模型字段必须取自当前绑定上下文。结构化原生操作只支持 NASTRAN deck；长度、坐标和容差使用模型自身单位。

这里的 NASTRAN 限制是当前 MCP 卡片读写和执行门禁的实现范围，不是 ANSA 原生网格算法的限制。几何与网格核心按求解器无关方式开发，必要实体/卡片差异保留在底层适配；按用户本轮要求，不将跨deck专项验证或求解器工程验收列为本任务阶段。验收以几何保持、覆盖完整、接口共形和单元质量为准。官方证据与开发原则见[开发说明](../development.md#求解器无关的几何与网格核心)。

工具分两类：

- `ansa_mesh_block_plan`、`ansa_mesh_interface_audit`、`ansa_mesh_source_audit`、`ansa_geometry_partition_plan`、`ansa_mesh_provenance`、`ansa_mesh_evidence_page`、`ansa_mesh_seed_plan`、`ansa_mesh_seed_read`、`ansa_mesh_sweep_prepare` 、`ansa_geometry_features` 、`ansa_geometry_sweep_candidates` 和 `ansa_mesh_map_block_inspect` 不生成或修改网格。审计、计划和证据以所属会话及模型代次绑定。
- `ansa_geometry_partition_apply`、`ansa_geometry_boundary_prepare`、`ansa_mesh_seed_apply` 与 `ansa_mesh_sweep` 会写入模型；必须传新备份路径和由当前源/计划得到的 SHA-256。ANSA 原生调用发生异常或结果不明时，先查询原任务并独立回读，不自动重试。

`ansa_mesh_cad_map`、`ansa_mesh_map_block_prepare`、`ansa_mesh_map_blocks_prepare`、`ansa_mesh_annular_source_prepare` 与 `ansa_mesh_map_block_fill` 各自用 `action="plan"`（内部只读操作）与 `action="apply"`（内部写操作）区分行为，绑定目标上下文和计划指纹。MapBlock prepare 生成面网格和未填充体，fill 填充已有块，CAD Map 从已有边界创建新的 Map 体，不能混同。

大型审计和计划结果会存入所属私有会话，工具先返回有界描述符，再用 `ansa_mesh_evidence_page` 读取完整记录。证据带模型上下文及校验和，`usable_for_mutation` 固定为 `false`；历史 `report_id`、证据摘要或哈希都不是模型修改授权令牌。分页读取 `mesh_seed_read` 证据时会重新核验来源范围，但仍不收集 CONS 的 GRID。

## 工具和参数边界

| 工具 | 行为 | 主要输入与边界 |
| --- | --- | --- |
| ansa_mesh_annular_source_prepare | 显式四块孔环源面计划 / 原生准备 | 已保存NASTRAN毫米、完整单Part未网格局部域；全部face_ids≤64、恰好4个source_face_ids，显式center/单位axis/inner_radius/outer_radius/distance/target_length/max_chord_error和已有PSOLID。一次MapBlock生成4个Ready未填充体，核验混合源面≤200项；随后用freeze sweep。 |
| ansa_mesh_map_block_fill | 已有 MapBlock 计划 / 受控实体填充 | `deck`，且 `volume_id` 或 `volume_ids` 二选一。单块沿用原契约；多块限同一 Part 的完整矩形组合，允许已完成的其他块。边界总计≤400 CQUAD4，全部实体≤5000 SOLID；一次 VolumesRemesh，保护旧块和共享节点。 |
| ansa_mesh_map_block_prepare | 六面域计划 / 原生 MapBlock 受控准备 | 单 Part、单源 PSHELL、完整六个独立平面 FACE；全模型无 SHELL/SOLID/VOLUME/CURVE。plan 绑定有限 CAD 导出域、完整卡片及配置；apply 验证新备份，调用一次原生 MapBlock，绑定指定已有 PSOLID/MAT1，核验规则面网格并恢复配置。不生成最终 SOLID。 |
| ansa_mesh_map_blocks_prepare | 矩形多块计划 / 联合 MapBlock 准备 | 2–20 个明确的六 FACE 分组，完整覆盖单 Part CAD，每组为坐标轴对齐矩形体。统一正 target_length、已有 PSOLID；一次 MapBlock，仅生成共享面网格和未填充体。 |
| ansa_mesh_map_block_inspect | 只读 Map Block 状态与边界回读 | 显式 1–20 个原生 Type=Map Block 的 VOLUME；100 FACE、5000 SHELL、10000 GRID、8 MiB 证据预算。回读 State/Status、端盖、侧壁、原生 FACE 宏网格及节点；原生 ready 不是独立几何/质量验收。 |
| ansa_mesh_map_block_caps | 计划并受控编辑一个已有MapBlock的caps | 显式volume_id、master_face_ids、slave_face_ids，支持复合面组；已保存NASTRAN毫米、无SOLID，目标Ready/Unmeshed且未冻结。备份后调用一次DefineCaps，独立保护FE/CAD、其他块及配置。 |
| ansa_geometry_sweep_candidates | 只读有限候选分类 | 显式 1–100 个 FACE、正 target_length；全部 CONS 连通组件逐项分类，六面闭域验证有限连续导出域，返回全部可用无序端盖对及相容分段建议；不分割、不生成网格、不调用 MapBlock。 |
| ansa_mesh_cad_map | 六面 CAD Map 计划 / 受控实体生成 | plan 验证有限导出域、整数种子、相容的六面规则 CQUAD4 及独立空输出 Part；apply 重新核验计划，先备份，再调用原生 VolumesMap，独立回读 GRID、SHELL、SOLID、VOLUME、输出 Part 和 CAD 归属。首版仅限 NASTRAN 毫米制，模型不得已有 SOLID/VOLUME。 |
| ansa_mesh_seed_plan | 只读种子规划 | 显式边长、目标长度及圆弧半径/弦误差；协调共享边等分组和倍数，输出整数约束；不自动读取或写入 CAD 边分段。 |
| ansa_mesh_seed_read | 只读原生 CONS 分段快照 | 显式 1–20 个唯一 FACE、1–32 个唯一 CONS；返回卡片分段、HOT POINT 端点、FACE 邻接和来源/分段哈希。`points` 与 `point_count` 为 `null`，`expected_point_count` 仅由卡片推算；读取、分页和写前预检都不收集 CONS 的 GRID。 |
| ansa_mesh_seed_apply | 首次分段时备份后写入明确 CONS 分段数 | 使用同一 FACE/CONS 范围的分段哈希和绝对新建备份路径；assignments 为 1–32 个唯一 CONS，每项 `segments` 为严格整数 1–10000，且 `Σ(segments+1)≤10000`。按相同段数分组调用原生 `NumberPerimeters`，写后核验点、投影、CAD 哈希、非目标 FE 和最终边数。目标 CONS 已实例化内部 GRID 且请求改变分段数时拒绝；当前不提供已有 perimeter 内点的重新分段契约。 |
| ansa_geometry_features | 只读显式 CAD 特征读取 | 最多 20 个 FACE、50 个 CURVE；FACE 使用原生 CONS/HOT POINT 邻接检查闭环，`SurfaceInfo` 报告局部曲率；显式 CURVE 的 IGES 100/110 及严格线性 112 子集可用于 seed 建议，先与原生长度交叉核验。 |
| ansa_geometry_boundary_prepare | 写入受控辅助曲线 | FACE 范围最多 20、CONS 最多 32；要求当前 FACE-only 特征指纹、已存在且为空的独立辅助 Part、新绝对备份路径。逐条调用 ConsToCurves 并比较返回实体和实际新增实体；不证明原 CONS 解析等价。 |
| ansa_mesh_sweep_prepare | 只读多块预检和合批 | 最多32个已就绪块、合计200个源SHELL；一次联合读取；共享GRID的块须同次扫掠，参数不兼容则blocked；输出已有mesh_sweep可复用的组参数。 |
| ansa_mesh_interface_audit | 只读审计 | entity_ids 为显式实体 ID，最多 5000 个；只支持 NASTRAN 线性 CHEXA8/CPENTA6。interfaces 最多 500 组；expected_boundary 是所选范围完整外表面三/四节点面片清单；protected_cylinders 最多 100 个，面片归属须显式提供；expected_components 缺省为 1。 |
| ansa_mesh_block_plan | 只读计划 | plan.template 必须是 straight_sweep；块最多 128 个、接口最多 512 个、种子约束最多 512 个。块须显式给出 source、target、depends_on、sweep_direction 和正整数 layers。返回确定性执行顺序与整数边分段解，不验证当前几何。 |
| ansa_mesh_source_audit | 只读源面预检 | 仅显式 NASTRAN CQUAD4/CTRIA3；source_ids、target_ids 各最多 200 个，direction 为非零三维向量，layers 为 1–10000，target_length 必填且须为正数。目标面缺省时仍可检查源面角度、短边及分段约束，但对应关系状态为 incomplete。 |
| ansa_geometry_partition_plan | 只读静态预览 | face_ids 最多 100 个，plane 包含 origin 和非零 normal，protected_face_ids 最多 200 个；返回当前来源指纹和 plan_sha256。不执行试切，也不保证真实切后拓扑。 |
| ansa_geometry_partition_apply | 写入一次显式切分 | 与预览使用相同的 FACE、平面和保护范围，另须提供新 backup_path 与 expected_plan_sha256。仅在重新计算的计划哈希匹配时执行；不把新 FACE 数量当成切分完成证据。 |
| ansa_mesh_sweep | 写入一次直线扫掠 | shell_ids 最多 200 个；direction 非零；distance 为正数；steps 为 1–100；property_id 必须指向有效 PSOLID；另须提供新备份和 expected_source_sha256。可选严格布尔 freeze_source 默认 false；true 时先验证并冻结源网格。CQUAD4 生成 CHEXA8，CTRIA3 生成 CPENTA6。 |
| ansa_mesh_provenance | 只读或更新会话来源记录 | action 为 create、read、record_stage 或 validate_resume。创建时可省略 ledger_id 由服务生成；后续操作使用当前 ledger_hash 做并发前置条件。记录来源、FE 对应、原生归属声明和阶段，不改变 ANSA 模型。 |
| ansa_mesh_evidence_page | 只读证据分页 | report_id 为 32 位小写十六进制 ID；section 支持顶层字段名或 JSON Pointer；offset 不小于 0，limit 为 1–200，缺省 100。返回 rows、total、next_offset 和 complete。 |

## 有限孔环源面准备

`ansa_mesh_annular_source_prepare` 不识别孔或切分CAD。输入应已明确分为四个原生可映射块，`face_ids` 必须完整覆盖当前单Part模型，`source_face_ids` 恰好四个且属于该范围。模型不得已有SHELL、SOLID、VOLUME或CURVE；原GRID、材料/属性及切分保留的WPLANE构造平面须完整不变。中心、单位轴向、内外半径、正直扫距离和弦误差均由调用方显式声明，`property_id` 引用已有PSOLID及明确DEFINED的MAT1。

生成前以声明的尺寸和 `target_length` 检查分段预算：外圆周分段向上取4的倍数，乘径向宽度分段作为源面SHELL估计，沿用200项源面上限；另按扫掠方向分段估计两端盖、内外壁和4个共享径向面，沿用5000项边界SHELL回读预算。极小尺寸、非有限比值或估计超限在备份和原生配置写入前拒绝。`source_mesh_budget` 随计划返回并参与计划摘要；该估计用于请求规模预筛，不是原生实际网格数量上界，生成后的真实来源、数量与完整范围检查仍须通过。

plan只读，绑定完整模型/CAD导出、配置及目标上下文，返回 `plan_sha256`；它不保证原生MapBlock一定成功。apply另需匹配哈希及绝对新备份路径，重新验证备份与当前计划，临时设置绝对目标长度后调用一次原生MapBlock，恢复完整MeshParams、QualityCriteria及F11设置。返回必须与实际新增4个Ready未填充体相符，端盖/侧壁及全部FACE/SHELL归属完整，源面必须通过有限混合CTRIA3/CQUAD4环带审计。

局部CONS种子是本次网格生成的产物，会按明确的绝对长度策略更新：`Elem.Length`允许原生兼容细化，但不得大于请求值，`Distort.Angle`为0；节点数按实际原生perimeter GRID回读，不保留原局部种子。plan返回 `native_perimeter_policy`，全局网格配置恢复与这些局部分段变化分别报告。回读只在已验证备份的写阶段执行，要求perimeter全部GRID身份/坐标属于实际完整面网格，并在最后再次比较完整模型，避免把读取隐含创建节点当成无副作用。CAD定义哈希及其他原卡片继续精确保护。

沿用现有CONS审计对Start/End Vector的派生显示分类，保留有限单位向量及端段比较诊断，不把它们当解析CAD切向量证明。实际perimeter点逐一用原生 `ProjectPointToCons` 投影，距离不得超过显式 `max_chord_error` 的几何近似预算；该点到CAD预算与源环半径/平面的1e-6 mm数值容差分开记录。点投影通过不证明连续双向CAD覆盖。

完整面网格还复用 `structured_boundary.measure_nodes`，将每个实际节点投影到其原生归属的唯一FACE，全部点/FACE对都须在同一显式几何预算内。FACE面积、中心、法向及局部包围盒按原生细分显示量保留前后值，与完整CAD导出定义及实际节点分开比较；不声明解析面积、重心或法向等价。Part面积按实际全部SHELL的三角化独立重算，Part整体包围盒只允许128 ULP末位变化。局部CONS旧细分显示框的变化受几何预算约束，当前框还须与实际点框在1e-6 mm内一致。

新体绑定到给定的已有PSOLID，仅删除本次新增且经独立引用核验证明未使用的自动PSOLID/MAT1。完整CAD导出、原节点/卡片和全模型新增实体逐项核验；未知变化或配置恢复失败进入恢复状态，不自动重放。固定环带审计容差为1e-6 mm，返回 `annular_tolerance_mm`；这是本阶段对原生数值舍入的明确契约，不改写节点。成功返回 `source_ids`、`source_sha256`、`source_evidence` 和不含层数/备份的 `sweep_parameters`；调用方给出正整数steps、新操作ID及新备份，使用 `ansa_mesh_sweep(freeze_source=true)` 继续。

该准备证明面网格来源及有限环带FE域，`continuous_CAD_coverage_proven=false`、`solid_mesh_generated=false`。原生Ready、面网格数量或成功回执不能替代最终实体覆盖、孔壁、质量和保存重开验收。

## 扫掠前冻结源网格

`ansa_mesh_sweep(freeze_source=true)` 在备份验证、模型绑定和源面指纹复核后，要求所选 SHELL 的 `FROZEN` 均明确为 `NO`，再调用原生 `base.FreezeElements`。独立回读必须确认返回成功、全部目标变为 `YES`、只有所选 SHELL 的冻结字段改变，且源 GRID 坐标与单元连接指纹精确不变；任一项异常就停止，不继续 Extrude。原生调用已发生而效果不明时保留恢复状态，查询原任务与现场，不自动重试。

冻结状态在扫掠后保留，不自动解冻。保存重开分别核对源面指纹、实体网格指纹、SOLID ID 和 `FROZEN`，不能用原生函数返回值代替验证。此处冻结的是 FE 源网格；CAD FACE 的 Freeze/Unfreeze 是另一类操作。未传或显式传 `false` 时保留旧请求与执行行为。

扫掠内部完整模型快照的资源预算为 2 MiB，原有 200 个源 SHELL、500 个新增 SOLID 上限保持。该内部预算不改变 1 MiB TCP 帧限制或其他操作的预算。

## 原生 Map Block 回读

`ansa_mesh_map_block_inspect` 接受明确 `volume_ids`，只读取已有 `Type="Map Block"` 体，不调用 `mesh.MapBlock`、`VolumesMap` 或任何网格生成器。`GetVolumeBoundary` 与 `CollectEntities(VOLUME, FACE)` 的完整 FACE 清单须一致；`GetMapVolumeBoundaries` 的 master/slave/round 原样回读，缺项、重复或范围外边界不能算端盖完整。

原生 `State="Ready to Mesh"` 且端盖/侧壁为完整不相交分组时报告 `native_ready`；`State="Non Mappable"` 始终报告 `native_non_mappable`，即使 MapBlock 曾成功返回 VOLUME。其余状态为 `incomplete`。这只是原生状态诊断，`usable_for_mutation=false`；不证明 CAD 连续覆盖、端盖节点一一对应、层数、块间共形、实体质量或可安全执行后续写操作。

结果分开存储 `volumes`、`face_membership`、`surface_mesh` 及 `cad_witness`。单 FACE 宏才确认逐面 SHELL 归属，多 FACE 宏保留原始成员及歧义；全部宏 SHELL、连接、PID 和基本坐标 GRID 都纳入来源指纹。分页重新读取体卡片、分组、CAD 导出与实际面网格，不靠相同计数判定新鲜性。当前限已保存的 NASTRAN 毫米、FACE 定义的 Map Block；普通 Type=Map 体不混用该契约。

2026.1.0 原生探针表明，MapBlock 可在生成面网格的同时新增 PSOLID、MAT1 和 VOLUME，并改变源 CONS 种子、FACE 体计数、PSHELL 单元计数及源 Part 汇总量。仅凭“返回一个体”不足以确认生成正确。以下首版受控准备入口逐项核验这些副作用；通用复杂几何的自动分块尚未开放。

## 受控 MapBlock 准备

`ansa_mesh_map_block_prepare` 的公共参数为 `action`、`deck`、六个唯一 `face_ids`、不同且属于该范围的 `source_face_id/target_face_id`、非零 `direction`、正 `distance/target_length` 和已有 `property_id`。长度使用毫米；方向会稳定归一化。目标长度不得超过最短边，每边最多 20 个名义分段。首版要求单个非冻结 Part、统一源 PSHELL、十二条非冻结线性一阶 CONS；只接受完整有限六面平面直扫域。指定 PSOLID 引用已定义 MAT1，并要求 RHO 为显式非负有限数，供未填充体质量核验。该门禁是 MCP 当前契约，不能推断 ANSA 其他 deck 或网格算法缺少能力。

`plan` 不修改模型，结果经 `ansa_mesh_evidence_page` 读取，例如 `section="plan_sha256"`。`apply` 须增加相同参数、`expected_plan_sha256` 和新的绝对 `backup_path`。完整模型、CAD、原生种子、当前导出配置都重新绑定；备份完成后再检查一次。计划不保证原生算法必然返回 ready；实际失败会保留副作用、原任务及恢复路径，不自动重试。

执行仅调用一次原生 `mesh.MapBlock(faces)`。它生成的默认 MAT1 可能与源材料不同，不能视为继承：工具先确认新 VOLUME/PSOLID/MAT1 的实际增量，再显式绑定调用者提供的既有 PSOLID，独立回读生效后，仅以 `force=False, compress=False` 删除本次新增且未使用的默认属性与材料。全量卡片核验保护已有实体和未使用资产。

约 `1e-16 mm` 的浮点舍入差异按数值一致处理，不作为几何问题或修复任务；不追求这些派生显示值的逐位相同。原始数据可以保留用于追溯，验收集中于实际尺寸、CAD 定义、拓扑、材料及生成结果。

结果须为一个 `Ready to Mesh`、`Unmeshed` 的 Map Block，端盖与侧壁分别 1/1/4 FACE，完整覆盖原六面。实际 SHELL 须形成每面完整的规则 CQUAD4 格点；完整节点、连接、归属及种子点受核验。原始 CAD 导出、面积、边界、法向与原有 GRID 保持精确；FACE 重心的已观察舍入仅在原生 `CalcElementMass(FACE)` 复核、且前后差不超过 Part 对角线的 `1e-14` 时接受。记录原值、当前值与限值，不全局忽略几何字段。

`meshing_context.py` 保存当前全部已导出的 MeshParams 赋值、QualityCriteria 行与元数据、这些行对应的 F11 getter 三元组以及四项会话设置；它不声称枚举 ANSA 隐藏或未导出的全部产品配置。临时设置 Quad/一阶和绝对目标长度后，成功、异常均分别尝试恢复各部分并重新导出比对，恢复失败不得返回成功。`calculation=0` 的 setter 使用空字符串，但必须 getter 回读仍为 0。所有原始恢复文件保留。

Part 的 Mass、COG 和六项惯量由 `CalcElementMass(Part)` 独立回读。它表示 ANSA 混合 CAD/FE/未填充体的原生汇总一致性，不是解析实体或求解器质量验证。属性、材料、计数及其他 Part 定义仍逐项校验。`part_aggregates.compare_persistence` 只对这十项已有前后原生重算证据的字段给出有界数值容差，原始完整快照及差异保留；2026-10-26 修正用例保存重开实际达到完整快照精确相等，没有使用该容差抹平差异。

当前入口不是任意工业模型的通用分块或最终体网格生成器；`solid_mesh_generated=false`，项目质量、块间共形及求解器验收仍未完成。真实调用与边界见[验收记录](../acceptance.md)。

## 已有 MapBlock 的受控填充

`ansa_mesh_map_block_fill(action="plan"/"apply", deck, volume_id)` 填充当前已存在的块，不重新创建 MapBlock 或调用 VolumesMap。官方 `VolumesRemesh` 支持已定义体；2026.1.0 隔离探针确认 `VolumesRemesh([volume])` 在原 Type=Map Block/ID 上生成 CHEXA，返回 1，保留原有面网格与边界节点。`VolumesMeshV` 文档列出的生成器不包含 Map，本入口不猜测额外 mesh_type 字符串。

首版接受保存的 NASTRAN 毫米模型：唯一 Part/唯一 MapBlock，Ready to Mesh、Unmeshed、未冻结；所有六个 FACE 均属于该块；原模型无 SOLID/CURVE，所有 SHELL 均为这六面完整、唯一归属的 CQUAD4 边界，最多400个。统一源 PSHELL、已有 PSOLID/已定义 MAT1 且 RHO>0。每面及十二条线性一阶非冻结 CONS 必须形成相容、均匀的规则格点；首版要求各边的目标长度设置一致。

端盖采用原生 master/slave/round 的实际分组，不假定全局轴或 FACE 编号；仅接受1/1/4完整分组，并独立验证它们是有限平面直扫域。纵向层数取四条连接 CONS 的共同整数，范围1–100；端盖分段与节点须一一平移对应。最多5000个预测实体、10000个输出节点、10000个受保护模型实体，并受既有快照及捕获字节预算限制。

`plan` 不生成实体，也不实例化 CONS 虚拟节点。`apply` 还须相同参数、计划哈希及新绝对备份路径。完整模型、有限 CAD 导出域、实际源面连接、种子及配置都绑定在计划内；备份后再次核验。只调用一次 VolumesRemesh，成功和异常均复用完整配置恢复；超时、原生返回失败或回读失败不能自动换键重放。失败观察也先验证原模型代次，防止把另一模型的实体归属到旧操作。

新增仅允许 SOLID/GRID，不新增或替换 VOLUME、属性或材料。既有 SHELL/GRID 和 CAD 定义精确保护；新 GRID 的 CD/PS/SEID 必须可读且为默认值。逐项验证同一个体的状态转换、全部实体归属、PSOLID计数以及Part的十项原生汇总重算。每个 CHEXA 对应源四边形的一层，源端和末层均须保留原端盖节点ID；独立检查体积与采样雅可比。

全部自由面须与原生 FACE 宏的 SHELL 面片按节点ID匹配，并检查全部边界节点投影、单组件和完整期望外边界。该已证明的单平面块显式声明没有块间接口或保护圆柱，使用空集合；这不允许把复杂几何中的未知接口或孔壁也视为空。纯数据审计仍保留原生质量及非相邻实体相交未检查的状态，不能以纯拓扑通过宣称整体质量通过。

质量报告复用独立 `ansa_mesh_quality_report`。合成盒体可用明确声明的夹具准则验收；适用范围外的工业整件网格仍须检查几何、共形和质量。上述矩形模式只生成 CHEXA；显式孔环的 CHEXA/CPENTA 分支见下节。通用自动过渡模板、跨deck专项及求解器工程验收不属于当前任务。完整契约和实际结果见[验收记录](../acceptance.md)。

### 已有孔环 MapBlock 直接填充

同一 `ansa_mesh_map_block_fill` 通过 `volume_ids` 与显式 `annular_options` 选择有限孔环分支；省略选项时沿用原矩形契约，不增加工具。参数示例见[工具参考](../tools.md)。

仅接受已保存的 NASTRAN 毫米局部模型：完整单 Part、恰好四个 Ready/Unmeshed 且未冻结的 MapBlock、完整共享 FACE 连接，以及已有 PSOLID/已定义 MAT1。四个 `source_face_ids` 须分别属于四块的外边界；`center`、单位 `axis`、内外半径、正 `distance` 和 `max_chord_error` 均须明确给出。最多64 FACE、2000边界SHELL，源端与对应端各最多400个线性SHELL，沿用模型和捕获字节预算。400单元预算只用于本填充分支；公开源面审计及源面准备的默认200单元限制不变。

源端复用多边形孔环模板审计，几何匹配容差为1e-6 mm；对应端原生近似周界点的半径匹配容差为 `min(0.001, max_chord_error)` mm，原始坐标不改写。两个端面没有逐节点、逐层对应承诺。全局网格/质量配置绑定计划，填充不临时修改目标长度，完成后核对配置不变。

新备份与计划哈希核验后，只调用一次 `VolumesRemesh(四块列表)`，接受原生 CHEXA8/CPENTA6。既有原始卡片、SHELL/GRID、CAD及周界精确保护；仅新增GRID/SOLID，分别核对Hexas/Pentas计数、全部实体归属、PSOLID计数及原生Part汇总。按全部单侧FACE核对完整自由外皮，按双侧FACE核对共享节点与面片，检查连接组件、正体积与声明的节点几何范围。

`ok`/`scope_verified` 表示受控填充及以上回读通过，`project_quality_acceptance="not_evaluated"`；仍须另调质量报告。来源台账保留源FE与新实体指纹，但 `provenance_mapping_complete=false`，不能当作源层映射。原始CAD连续覆盖、非相邻实体相交和GUI验收不在该分支的证明范围。

## 只读扫掠候选

2026-10-26 增加 T2 坐标轴对齐矩形多块候选；下文 T1 的组件和端盖描述继续仅指单个完整六面组件。T2 的输入、证明和写入边界见本文末尾“矩形多块准备与增量填充”。

`ansa_geometry_sweep_candidates` 接受显式 `face_ids`（1–100 个，不重复）及正有限 `target_length`，沿用当前目标、模型、deck 和 operation_id。首版限已保存、已归属 Part 的 NASTRAN 毫米模型；既有 `ansa_geometry_features` 的 20 FACE 上限保持不变。

~~~json
{
  "deck": 1,
  "face_ids": [1, 2, 3, 4, 5, 6],
  "target_length": 5.0
}
~~~

该工具先按原生 CONS 连接建立选定范围的全部 FACE 组件，不按近邻坐标粘接，不自动搜索复杂组件的任意六面子集，也不补面或切分。只有完整六面同 Part 组件才枚举无序端盖对，并复用现有六面拓扑诊断；域外 FACE 邻接、缺面、内环、非四边边界、非直扫形状等保留逐组件原因。全部选定 FACE 都须在组件清单中解释；这不证明选定范围就是整件原始 CAD，也不证明已经找到复杂实体的全部可分块域。

拓扑通过的组件再用六次单 FACE 原生导出复用有限 IGES 支撑面及修剪域检查。相同组件的多个扫掠方向共用一次六面证明，避免重复导出。`verified_at_export_precision` 与 `continuous_export_domain_verified=true` 只针对受限导出表示；`exact_native_CAD_equivalence_proven=false` 和 `usable_for_mutation=false` 保留。拓扑相容但导出形式不受支持时，不升级为已验证候选；原始几何证明和方向建议分别保留。

完整记录位于 `/classification/components`。每个方向包含 `map_options`（六面、端盖、方向及距离）和 `seed_proposal`。分段建议将源面相对边、对应目标边设为等值组，以组内最大角点弦长除以 target_length 向上取整；四条纵向边具有相同层数。它不是当前原生种子，不证明曲边长度或网格质量。Map 的层数/单元/节点预算是否满足单独报告；后续仍需原生 seed_read/apply、规则面网格和新鲜的 Map plan/apply。候选摘要不能绕过它们。

每次读取及候选证据分页都重新捕获指定原生拓扑、HOT POINT 坐标及范围 CAD 导出哈希。缓存键包含来源与 target_length，只复用派生分类及有限证明；不跳过原生新鲜性检查。范围内同 epoch 变化或保存重开使旧证据失效；不相关范围变化是否影响证据由实际来源指纹判断，不强制全库失效。

原生 `mesh.MapBlock(macros)` 本身具有自动 caps/sides 识别能力，也会创建表面网格；文档没有只读开关。`VolumesDetect` 以已有表面网格为前提，返回数量/列表不是 dry-run 保证。本工具不调用这两个写接口。后续自动分块应优先受控封装原生能力；当前分类只是可审计的保守预筛，不是 ANSA 通用 Map Block 算法的替代实现。

## 有限六面 CAD Map

`ansa_mesh_cad_map` 将同一组参数用于 `action="plan"` 和 `action="apply"`。原生生成复用 `mesh.VolumesMap`；MCP 负责范围、备份、来源指纹和独立回读，未编写替代网格生成算法。

~~~json
{
  "action": "plan",
  "deck": 1,
  "face_ids": [1, 2, 3, 4, 5, 6],
  "source_face_id": 1,
  "target_face_id": 2,
  "direction": [0.0, 0.0, 1.0],
  "distance": 10.0,
  "steps": 2,
  "property_id": 2,
  "output_part_id": 2
}
~~~

示例省略标准目标、模型和 operation_id。六个 FACE ID 必须唯一，两个端盖不同且在范围内。`direction` 是非零有限向量，规范化后用于真实距离；已归一化的浮点向量保持不变，避免计划、分页和备份后重检发生摘要漂移。`distance` 为正有限数，`steps` 是 1–100 的严格整数。参数中的 ID 都由当前模型读取，示例数字不是模板固定 ID。

首版要求 NASTRAN、毫米、已保存数据库和同 Part 的封闭六面直扫域，模型中没有既有 SOLID/VOLUME。六面均为独立、未冻结、已生成相容规则 CQUAD4 的宏，目标和侧壁网格须已就绪；纵向 CONS 段数与 steps 相等，两端对应边段数相同。原生几何分段以 LINEAR、一阶、开放 CONS 为限。输出 Part 必须已有、独立、为空且非组；源 Part 和所有原 GRID/SHELL 保持保护。`property_id` 指向有效 PSOLID/MAT1，不能照搬其他 deck 的卡片。

### 有限连续几何验证

先查了当前版本 `SurfaceInfo`、邻接及投影接口；它们不足以读取普通 CAD FACE 的完整支撑/修剪定义。`IgaPatch.get_surface_data/get_loops` 用于 IGA 对象，不能当作通用 FACE 接口。当前补充验证仍通过原生 `SaveFileAsIGES` 逐 FACE 导出，仅解析以下有限形式：未变换、单位权重、degree 1 的 2×2 IGES 128 曲面，144 单一显式外环、无内环，142 关联的 102 四段组合、严格线性 112 参数曲线和 degree 1 的 126 空间边。参数环须精确覆盖单位方形，两种曲线表示均须与曲面映射及原生 HOT POINT/CONS 对应；不忽略优先表示。未知形式、孔、非线性系数、额外几何或引用不完整均拒绝。

由控制网和完整修剪定义验证整个导出域，几何容差固定为 `1e-7 mm`，不用有限采样拟合平面或直线。依据为 [NIST IGES 4.0](https://nvlpubs.nist.gov/nistpubs/Legacy/IR/nbsir88-3813.pdf) 与 [NASA 几何数据交换规范 §4.8、§4.11](https://ntrs.nasa.gov/api/citations/19950005054/downloads/19950005054.pdf)。112 的冗余末端元数据一致性单列；实际几何由其定义系数判定。本机导出含冗余末端不一致，未将其隐藏，也不据此改变定义曲线。

结果 `continuous_export_domain_verified=true` 仅指这种有限导出表示；`exact_native_CAD_equivalence_proven=false` 始终保留。它不证明任意原始内核几何等价，也不代表其他样条、孔或全部工业模板已经支持。原 `geometry_features.sweep_options` 仍是角点/拓扑诊断，不自动升级为该几何证明。

### 计划、写入及回读

plan 返回 `plan_sha256` 和证据描述符，通过 `ansa_mesh_evidence_page` 读取几何、面网格等记录；每页重新核验计划来源。plan 不能携带 `expected_plan_sha256` 或 `backup_path`。apply 必须携带同一参数、刚获得的摘要及绝对新备份路径。摘要绑定 CAD、种子、面网格、全模型完整卡片、上下文、属性和相关网格设置。写前预留“既有实体＋预计新增 GRID/SOLID＋1 VOLUME”的 10000 实体预算；源面/边界合计最多 400 SHELL，输出最多 5000 SOLID、10000 节点，仍受完整快照大小预算约束。

备份验证及再次预检通过后才调用一次原生 Map。失败不自动重试；回包尽可能保留观察到的新增实体和恢复状态。操作复用现有 Journal、provenance 和源 CQUAD4→各层 CHEXA 对应关系；同键重复提交返回原回执。

写后独立检查所有源节点×层的一一对应、每个源单元×相邻层恰有一个 CHEXA、无遗漏/重复、正体积及中心/八高斯点 Jacobian。所有原 GRID/SHELL 卡片和其他受保护定义保持一致；只有选中 FACE 的体成员数 0→1、目标 PSOLID 的元素计数 0→完整新 SOLID 数，以及经明确核对的输出 Part 派生字段允许变化。Part 的包围框由真实节点重算，质量/惯量调用原生 `CalcElementMass` 对显式新 SOLID 重算，属性/材料摘要与完整卡片交叉核对。全卡片保护不沿用通用 `Num.Elem` 或 Part 惯量过滤。

继续核对唯一 VOLUME 的 SOLID、Part、属性、步骤、类型及六个原始 FACE 边界；原宏 SHELL 成员不变，全部外皮面片与 CAD 成员双向匹配，关联节点投影通过。采样 Jacobian、节点投影和规则占位不等于任意扭曲单元的全域证明或项目质量验收，回包保留 `project_quality_acceptance="not_evaluated"`。

独立夹具从未网格 CAD 经分段、88 CQUAD4 到本入口生成 48 CHEXA 已验证。保存重开单列了输出 Part 的 `Is Color Active: NO→YES` 显示刷新；除此以外，完整工程卡片、节点、CAD 哈希一致。生产写入范围检查未因该保存重开显示差异而放宽。

## 显式 CAD 特征读取

`ansa_geometry_features` 只处理调用方显式选择的几何 ID。`face_ids` 默认为空列表、最多 20 项；`curve_ids` 默认为空列表、最多 50 项。两者的每个 ID 都必须是严格正整数。FACE 通过显式 FACE→CONS 拓扑读取，并使用 `SurfaceInfo` 获取局部曲率；不扫描全模型寻找特征。

CURVE 从所选范围的有界 IGES 导出中解析实体 100（圆弧）、110（直线）及 112 的严格单段线性多项式子集，再用 ANSA 原生曲线长度交叉核验后才用于种子建议。112 仅接受未变换、三维、CTYPE=3、单段、全部二次/三次系数严格为零且非退化的定义，整个参数区间由系数证明为直线；多段或 126 样条保持 unsupported。末端冗余值的一致性单列 `redundant_terminal_evaluations_status`，不一致不会隐藏，也不宣称完整 IGES 记录一致。多项式语义依据 [NISTIR 88-3813 §3.8](https://nvlpubs.nist.gov/nistpubs/Legacy/IR/nbsir88-3813.pdf)。不拟合任意曲线、自动识别孔或自动切分几何。初版仅支持 NASTRAN、毫米单位；实体必须已有 Part，当前数据库必须已保存。导出失败不会自动保存或重开数据库。

CONS 卡片长度可能为离散近似值，不将其冒充解析弧长。导入 CAD 只有 FACE/CONS 而无独立 CURVE 时，本工具仍返回邻接和局部曲率证据，尚不能自动为这些 CONS 生成解析圆弧种子。圆弧轴及表面轴候选也不等于孔轴已确认。每次导出上限 8 MiB，总 CONS 上限 500；超限失败，不截断后宣称完整。已保存数据库是首版保守前提：本机新建模型曾出现导出异常，显式保存重开后恢复；这不是官方 API 文档列出的强制条件。

`feature_schema=2` 增加原生边界闭环证据：`vertices` 保存 HOT POINT 的实际编号、双精度 `position` 和相邻 CONS；`boundaries[].endpoint_topology` 保存每条 CONS 的端点编号。读取时检查 CONS→HOT POINT 与 HOT POINT→CONS 双向关系，同一请求最多 1000 个端点、单端点最多 100 条相邻 CONS，超限失败。每条 CONS 恰有两个不同原生端点时才进入闭环检查；0、1 或多于 2 个端点会保留原记录并将相关 FACE 标为不完整，不推测单边整圆。

`contours.faces` 返回有序 CONS 环和方向。方向相对于递增 HOT POINT 编号定义的边方向，不代表 CONS 参数方向或 FACE 法向。拓扑完整要求 FACE 中全部边恰使用一次、端点度数为 2；开口、分叉、重复边、缺边及同编号坐标冲突都会标记不完整。坐标接近但编号不同的端点不会自动合并。允许多个独立环，但不推断内环、外环或孔。`status=complete` 仅表示拓扑连通闭合；原生 CONS 的 `geometry_status` 仍为 `incomplete`，环长标记 `length_source=native_card_approximation`，不会进入解析种子建议。

可通过 `ansa_mesh_evidence_page` 的 `vertices`、`boundaries`、`/contours/faces` 分页读取。端点坐标和双向邻接已纳入每次新鲜度指纹，缓存命中仍重新读取；端点邻接在单次调用内按编号去重查询。未调用会创建辅助实体的 `ConsToCurves`，也未从圆弧采样拟合解析类型。

可选 `seed_options` 是严格模型，未知字段会被拒绝；必须提供正数 `target_length`，其余默认值为 `max_chord_error=0.02`、`min_segments=1`、`multiple=1`、`max_segments=10000`。三个段数参数均为严格整数，范围 1–10000。自动 seed 只处理显式选中的受支持 CURVE；FACE 局部曲率作为特征证据返回，不自动派生 seed。示例（另须提供当前绑定上下文）：

~~~json
{
  "operation_id": "geometry-features-01",
  "expected_target": {
    "backend_id": "backend-id",
    "instance_id": "instance-id",
    "session_id": "session-id",
    "generation": 1
  },
  "expected_model": {"epoch": 1},
  "deck": 1,
  "face_ids": [101, 102],
  "curve_ids": [201, 202],
  "seed_options": {
    "target_length": 1.0,
    "max_chord_error": 0.02,
    "min_segments": 1,
    "multiple": 1,
    "max_segments": 10000
  },
  "ttl_seconds": 300
}
~~~

派生缓存最多 8 项、合计 4 MiB。每次缓存访问和每页新特征证据都仍会导出一次当前显式范围，并重新校验该范围的 CAD 指纹和 FACE→CONS 拓扑；缓存命中只省去 `SurfaceInfo` 查询和每条 CURVE 的单独导出。证据始终 `usable_for_mutation=false`，不构成模型修改授权。新鲜度指纹受导出精度限制，只覆盖请求范围；范围外几何不检查，不能据此证明整库未变化。本次 4 FACE、3 CURVE 合成夹具中，冷读导出 4 次、SurfaceInfo 1 次；命中后导出 1 次、SurfaceInfo 0 次。内部计算单次约 0.0916 秒与 0.0259 秒，仅为本次观察，不代表工业流程加速幅度。

接口审计的 interfaces 每项包含唯一 id、两个块名和对应面片；面片为三角形或四边形节点 ID。圆柱保护项包含 label、center、axis、正数 radius、非负 tolerance 和完整 facets。未提供完整期望边界、接口或保护孔壁集合时，相关覆盖不能推定为已通过。

### 有限六面直扫对应诊断

在 `ansa_geometry_features` 中可选提供 `sweep_options`，复用同一次原生拓扑读取及 CAD 指纹，不增加公开工具。只接受 FACE-only 范围，源、目标必须是范围内不同的 FACE；方向须为单位向量，距离为正数。`tolerance` 缺省 `1e-7 mm`，允许范围 `(0, 0.001] mm`，未知字段、布尔 ID 和非有限数拒绝。

~~~json
{
  "deck": 1,
  "face_ids": [1, 2, 3, 4, 5, 6],
  "sweep_options": {
    "source_face_id": 1,
    "target_face_id": 2,
    "direction": [0.0, 0.0, 1.0],
    "distance": 10.0
  }
}
~~~

检查限定为同一 Part 的 6 FACE、12 CONS、8 HOT POINT。各 FACE 须为四边闭环，每条 CONS 恰好邻接域内两面；按原生编号建立四组角点平移、四条纵向边和四个侧壁的完整对应，并检查角点的平面性、凸性和包络。坐标接近的不同编号不会合并，面数不符或边界不闭合返回 `sweep_plan.status=unsupported` 及原因，不从工业模型中自动猜选子域。

拓扑通过仍返回 `status=incomplete`、`topology_status=pass`、`geometry_status=incomplete`、`native_execution_status=unverified` 和 `cad_surface_geometry_verified=false`。角点面积/预期体积是候选诊断量，不能证明修剪面内部、曲边、原始 CAD 的连续覆盖、实际 Map/扫掠兼容或网格质量。结果不授权写入；`plan_sha256` 绑定当前来源指纹、请求和诊断。通过 `/sweep_plan/correspondence/side_pairs` 等 JSON Pointer 分页时仍执行原有 CAD/拓扑新鲜度检查；同一几何缓存中的不同距离或方向会分别计算诊断。

尺寸种子、共享源面与合批生成的完整流程、输入示例及未覆盖范围见[复杂实体网格复用流程](complex-solid-meshing.md)。

## 原生 CONS 分段读取与写入

这组工具处理“原始 CAD 边界已映射到原生 CONS，准备为该 CONS 设置边分段”的单一阶段。它不自动识别原始 CAD 边，不根据近似卡片长度反推精确圆弧，也不生成源面网格或整件体网格。调用方必须显式给出 FACE/CONS 对应关系、共享边组和等分段组。

初版只支持 ANSA 2026.1.0 GUI、NASTRAN deck、毫米单位及已保存数据库；参与范围的实体必须已有 Part。某个 CONS 只有在开放、一阶、两端为两个不同 HOT POINT、`Frozen=NO`、`Collapsed=NO` 且 `Biasing=LINEAR` 时才可用于写入。所有相邻 FACE 必须包含在请求的 `face_ids` 中，受影响的 FACE 宏必须未生成 SHELL。

### 读取

`ansa_mesh_seed_read` 接受 1–20 个唯一 FACE 和 1–32 个唯一 CONS，CONS 必须属于这些 FACE 的边界。它只读取 CONS 卡片、`HOT POINT` 端点、FACE 邻接及有界 CAD 几何哈希，返回 `seed_snapshot_sha256` 和可由 `ansa_mesh_evidence_page` 分页读取的证据。

读取结果中的 `Num. of Nodes` 表示开放 CONS 的内部点数，不含两个端点。`segments=Num. of Nodes+1`，`expected_point_count=Num. of Nodes+2` 都只是由当前卡片推算；各条推算点数合计超过 10000 时读取直接拒绝。`points` 和 `point_count` 固定为 `null`，不代表边界点缺失。读取及每次证据分页只重复上述只读核验，不能对 CONS 调用 `CollectEntities(CONS, GRID)`。2026.1.0 会在该调用中实例化虚拟节点，因此不得把它用于 read、pagination 或写前 preflight。

读取示例仍需补齐当前绑定字段：

~~~json
{
  "deck": 1,
  "face_ids": [101, 102],
  "cons_ids": [201, 202]
}
~~~

### 写入

`ansa_mesh_seed_apply` 的 `assignments` 必须逐项列出目标 CONS 和期望边数，例如：

~~~json
{
  "deck": 1,
  "face_ids": [101, 102],
  "assignments": [
    {"cons_id": 201, "segments": 8},
    {"cons_id": 202, "segments": 12}
  ],
  "expected_seed_snapshot_sha256": "<与同一FACE/CONS范围mesh_seed_read结果一致>",
  "backup_path": "F:/work/before-cons-seed.ansa"
}
~~~

`expected_seed_snapshot_sha256` 必须来自 `face_ids` 和 `assignments[].cons_id` 完全相同的 `ansa_mesh_seed_read` 结果。每个 `segments` 是严格整数 1–10000，CONS ID 不得重复，边界点预算按 `Σ(segments+1)≤10000` 检查。`backup_path` 必须是绝对新建路径，不得覆盖当前数据库。

执行顺序如下：

1. 写前重新读取相同 FACE/CONS 的卡片、HOT POINT、邻接和几何哈希，比较分段哈希；同时保护全模型实体、已有 GRID 和已存在 CONS 的节点引用。
2. 核验新备份、当前目标/模型上下文和来源哈希后，才显式收集全部 CONS 的虚拟点引用。原生新增 virtual ref 也会进入 `__ALL_ENTITIES__`。备份后保护只接受相对备份前新增、且属于某个 CONS perimeter 集合的 GRID；新增 ID 在 `perimeter_materialization.new_virtual_point_ids` 单独列出。所有原有 GRID 继续冻结，包括已有 CONS perimeter 内点、端点和 solver GRID。
3. 首次分段入口会检查目标 CONS 是否在操作前已有内部 GRID。若存在且请求 `segments` 与该 CONS 当前边数不同，则在调用 `NumberPerimeters` 前拒绝。当前尚未核实 ANSA 2026.1.0 存在可可靠区分已有 virtual ref 与 solver GRID 的同版本接口，因此不能安全授权复用或改写这些内点；该点不等于断言 ANSA 本身不具备此能力。重新分段需另行查找、核实并验证复用契约。
4. 对各目标 CONS 逐条复核虚拟边界点数量、端点身份和坐标。随后按 `segments` 相同与否分组，每个数量组调用一次 `mesh.NumberPerimeters`，参数固定为 `apply_multiple_number=false`、`remesh_macros=false`、`apply_number_of="edges"`、`use_ansa_defaults_values=false`。不要把“不使用 ANSA 默认值”简写为任意默认行为。
5. 原生返回码必须是严格整数 `1`；否则立即停止后续数量组。任一原生调用已开始后失败，结果保留调用记录并进入恢复检查，不自动回滚、重开或换键重试。
6. 写后重新读取目标边数、全部边界点及 HOT POINT 端点，用 `ProjectPointToCons` 检查点到原 CONS 的投影；最大已观测距离必须不超过 `1e-7` 模型单位。还要核对全 CAD 导出哈希未变、非目标 CONS 节点身份未变、所有原有 GRID 与非目标 GRID 卡片未变、目标/范围外 FE 未发生未授权变化。
7. 只有上述检查全部通过才返回 `status="applied"`、`ok=true` 和 `scope_verified=true`。失败返回 `recovery_required`，并以 `possible_effects`、`native_calls`、`point_projections`、`before_scope_sha256`、`after_scope_sha256` 及 `seed_after_sha256` 等字段供原任务核查。

写入保护快照最多覆盖 5000 个 CONS，编码后上限 8 MiB。潜在点预算始终以操作前快照 `budget_base` 的 entities、GRID 数量为基数，不拿已经物化后的计数再次叠加推断点。首次 `CONS→GRID` 前先遍历全模型 CONS，每条都必须是 first-order；按每条卡片 `Num. of Nodes+2` 累计保守物化点数，并在每次累加后检查“`budget_base` entities+累计点数≤10000”和“`budget_base` GRID+累计点数≤20000”。写前 preflight 另按每条 `max(卡片内部点数+2, 请求 segments+1)` 检查同一组上限。由于共享端点会重复计数，操作前已存在的 perimeter ref 也计入潜在点数，该核算可能保守拒绝本来可复用的模型。

备份后 materialized readback 继续使用同一操作前 `budget_base` 计算潜在点数，同时独立读取当时的实际 entities 和 GRID 数量并与 10000/20000 上限比较。实际数量不再加一遍已物化虚拟节点，因此不会把同批 point refs 重复计入；两套检查分别回答“最坏物化预算是否超限”和“当前实际模型计数是否超限”。

允许的目标 CONS 派生字段仅为卡片点数及起止向量；目标 CONS 真正邻接 FACE 的面积/中心和这些 FACE 所属 Part 的显示质量、面积、COG 可随原生细分显示更新。额外传入但未被目标 CONS 邻接的 `face_ids` 不获得 Part 派生字段豁免。bounds 类显示派生值另有受限例外，见下节；其他实体定义、归属和 FE 仍受保护。

`ansa_mesh_seed_apply` 当前是“原始 CAD 首次分段”的受限入口，不承诺对已有 perimeter 内点重新分段。已有内点只可作为冻结对象保存；本次新建且 perimeter-owned 的 virtual ref 才可在后续原生写回中按目标 CONS 的允许范围更新。即使拒绝发生在 `NumberPerimeters` 前，备份后虚拟点物化也可能已经发生，调用方仍须检查原任务、备份、`new_virtual_point_ids` 和 `possible_effects`，不能把该拒绝解释为模型完全未变化。

### 结果边界

`CONS` 卡片长度是原生近似值，不能据此宣称圆弧解析等价。这里的 `segments` 是原生边分段数，不是目标单元长度，也不能证明源面网格、扫掠实体、端盖/侧壁贴合或整件网格已经完成。读取和写入只确认指定 CONS 的分段与边界点关系；连续 CAD 覆盖、圆孔识别、源面质量及体网格质量仍须分别验证。

### 受限真机实测范围

i10、i02、i23 各三条原始边的分段与保存重开通过。i02 中，原生 CAD canonical hash 在分段设置前后完全不变。目标 CONS 的 `BoundBox` 随原生 tessellation 变化约 `2.14e-8 mm`；邻接 FACE 的 bounds 和 Part 的包围盒极值、尺寸和对角线等派生值变化约 `5e-13`。`_audit` 仅对目标 CONS 真正邻接 FACE 的派生 bounds，以及这些 FACE 所属 Part 的 包围盒极值、尺寸和对角线字段接受不超过 `1e-7 mm` 的数值漂移；额外 `face_ids` 所属 Part 不进入该白名单。该阈值与边界点投影使用同一个数值容差。

上述 bounds 例外只会在 CAD canonical hash 完全相等时成立，并且不隐藏原始差异：`tessellation_changes` 仍保留每个变化实体的变更前后 raw SHA-256。原始 CAD、HOT POINT、既有 GRID、非目标 CONS 和非目标 FE 的保护没有放宽。`1e-7 mm` 只是本实现用于点投影和显示派生量核验的数值容差，不能解释为工程质量容差，也不能证明连续 CAD 等价。

i08 的原 CAD canonical hash 也未变化，但边界点投影误差或范围检查失败，结果必须保留 `status="recovery_required"`、`recovery_required=true` 和原任务查询要求，不计入通过。没有为 i08 放宽 `1e-7 mm` 投影或范围精度；i10、i02 的通过不能覆盖该失败。

## 合同示例

以下片段展示每项工具的专有参数；实际调用还须提供通用绑定字段 operation_id、expected_target、expected_model 和 deck。

### 显式直线扫掠块计划

~~~json
{
  "deck": 1,
  "plan": {
    "template": "straight_sweep",
    "blocks": [
      {
        "id": "A",
        "source": {"type": "FACE", "id": 101},
        "target": {"type": "FACE", "id": 102},
        "depends_on": [],
        "sweep_direction": [0.0, 0.0, 1.0],
        "layers": 8
      },
      {
        "id": "B",
        "source": {"type": "FACE", "id": 201},
        "target": {"type": "FACE", "id": 202},
        "depends_on": ["A"],
        "sweep_direction": [0.0, 0.0, 1.0],
        "layers": 8
      }
    ],
    "interfaces": [
      {
        "id": "A-B",
        "blocks": ["A", "B"],
        "facets": [
          {"type": "FACE", "id": 103},
          {"type": "FACE", "id": 203}
        ]
      }
    ],
    "seed_constraints": [
      {"id": "shared-edge", "edge_id": "CONS-17", "segments": 8}
    ]
  }
}
~~~

计划要求 blocks、interfaces，可选 seed_constraints。相互矛盾的边分段约束、缺失依赖和依赖环会被拒绝；欠定的整数约束返回 status: "incomplete"。计划结果带 execution_order、plan_sha256、input_origin: "caller_declared_plan" 及 model_geometry_verified: false。

### 平面切分：先预览，再提交

预览请求：

~~~json
{
  "deck": 1,
  "face_ids": [11, 12],
  "plane": {"origin": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]},
  "protected_face_ids": [20],
  "produce_plane_faces": false
}
~~~

只有预览结果仍匹配时，才将返回的 plan_sha256 作为 expected_plan_sha256 提交写入请求：

~~~json
{
  "deck": 1,
  "face_ids": [11, 12],
  "plane": {"origin": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]},
  "protected_face_ids": [20],
  "produce_plane_faces": false,
  "expected_plan_sha256": "<替换为预览返回的64位小写SHA-256>",
  "backup_path": "C:\\models\\partition-backup.ansa"
}
~~~

该预览是当前几何状态下的静态计划，不是隔离副本上的真实试切。执行结果须检查错误、scope_verified、变化范围和独立回读；possible_effects 或 recovery_required 表示需要按原操作和备份核查。

### 接口审计和完整覆盖输入

~~~json
{
  "deck": 1,
  "entity_ids": [9001, 9002],
  "interfaces": [
    {
      "id": "joint-1",
      "blocks": ["A", "B"],
      "facets": [[1, 2, 3, 4], [5, 6, 7]]
    }
  ],
  "expected_boundary": [[1, 2, 3, 4], [8, 9, 10]],
  "expected_components": 1,
  "protected_cylinders": [
    {
      "label": "bore-A",
      "center": [0.0, 0.0, 0.0],
      "axis": [0.0, 0.0, 1.0],
      "radius": 20.0,
      "tolerance": 0.05,
      "facets": [[11, 12, 13, 14]]
    }
  ]
}
~~~

expected_boundary 必须表示所选 FE 范围完整的自由外表面，而不是抽样面片；孔壁集合应先按来源或拓扑完整归属，再测量全部面片。缺少期望边界或保护集合会如实标记为 incomplete。拓扑、几何、质量分别报告 pass、fail 或 incomplete；本纯数据审计没有原生 ANSA 质量结果，也不检查非相邻实体相交，因此总状态不会因此变成完整质量验收。带符号体积不是 Jacobian 或质量判定。

### 源面预检和扫掠

源面预检参数示例：

~~~json
{
  "deck": 1,
  "source_ids": [501, 502],
  "target_ids": [601, 602],
  "direction": [0.0, 0.0, 1.0],
  "layers": 8,
  "target_length": 4.0
}
~~~

提交扫掠时 expected_source_sha256 必须匹配本次 shell_ids 的源 SHELL 快照：

~~~json
{
  "deck": 1,
  "shell_ids": [501, 502],
  "direction": [0.0, 0.0, 1.0],
  "distance": 40.0,
  "steps": 8,
  "property_id": 71,
  "expected_source_sha256": "<替换为对应源SHELL快照的64位小写SHA-256>",
  "backup_path": "C:\\models\\sweep-backup.ansa"
}
~~~

推荐使用 ansa_mesh_source_audit 的 source_snapshot_sha256 作为扫掠前置值，source_ids 必须与 shell_ids 相同；snapshot_sha256 覆盖源与目标的组合范围，只有省略 target_ids 时可直接复用。目标面省略会使目标对应关系状态为 incomplete。扫掠不自动粘接既有目标，不调用 TETRA 或 Hexa Fit，也不承诺弯曲、扭转或任意变截面扫掠。

### 来源阶段记录与 JSON Pointer 分页

创建来源记录的参数示例（fingerprint、epoch 应替换成真实当前来源值）：

~~~json
{
  "deck": 1,
  "action": "create",
  "payload": {
    "source": {
      "type": "PLAN",
      "id": "plan-20261024-01",
      "fingerprint": "<64位小写SHA-256>",
      "epoch": 12
    }
  }
}
~~~

阶段依次为 planned → source_checked → applying → verified → checkpointed。失败或结果不明进入 recovery_required；恢复后仅允许经独立验证转到 verified 或 checkpointed，不能在同一 ledger 下重新进入 applying。应用工具会自动记录 source_checked 和 applying；仅当原生结果 ok=true 且 scope_verified=true 时记为 verified。checkpointed 不由应用工具自动写入。来源声明的 regeneration_status 保持 incomplete，不证明 ANSA 原生再生能力。

分页嵌套测量分量的示例：

~~~json
{
  "report_id": "0123456789abcdef0123456789abcdef",
  "section": "/sections/topology/components",
  "offset": 0,
  "limit": 100
}
~~~

section 可用普通顶层名称（如 sections），也可用以 / 开头的 JSON Pointer（如 /sections/topology/components）；路径中的 ~1 和 ~0 分别解码为 / 和 ~。JSON Pointer 只选择要分页的证据数据，不授予模型修改权限。证据文件上限为 64 MiB，单页最多 200 行且受 192 KiB 字节预算约束。

## 当前限制与状态解释

扫掠写前重新检查源面角度、退化边及垂直于方向的平面条件；短边的参考长度取 `distance/steps`，不会借此删除几何。源目标对应未知不会掩盖已发现的源面失败。扫掠后按每个源面、每一层逐单元匹配，并保存源节点到各层节点映射及实际 FE 来源指纹。

原生 Extrude 生成 VOLUME；要求其类型、算法、属性、层数、距离、一阶状态和全部新 SOLID 成员与请求一致。实际块名采用 `VOLUME:<id>`；没有唯一原生父归属时保持未知。ANSA 2026.1 还会附带未使用的默认 PSOLID/MAT1：仅允许数量不超过新增 VOLUME 数、属性不被几何/FE 使用、材料仅被这些新属性引用的辅助对象，并在回执列出，保留用于审计。

范围保护逐项比较底层几何、FE、坐标、材料/属性卡片及既有 FE 的 Part 归属。新网格引起的 Part 质量、面积、包围盒及属性列表等派生显示统计可更新；切分时 Part 的 `Last Edit` 与六个显示惯性字段不作为定义字段比较。排除字段在源码白名单中明确列出；这不改变质量工具的既有 `scope_sha256` 算法，不舍入节点坐标。

CAD 源面的直线扫掠可增加 FACE 卡片的 `Num. Volumes`。仅在 `GetVolumeBoundary` 证实该 FACE 属于本次新增 VOLUME、单 FACE 宏的完整非空 SHELL 清单属于本次源网格、计数增量与原生关联数精确一致时接受这一个字段变化。其他 FACE 字段、几何与属性/Part 归属仍严格比较；缺少证据或无关面变化继续失败。原生新增体边界最多 500 个 VOLUME、每体 400 个边界实体、累计 4,000 个，不放宽原质量 scope hash。

`verified` 只表示此次 FE/几何操作及允许变化范围通过回读；台账中的 native ownership/regeneration 仍可为 `unknown`，对外 `regeneration_status=incomplete`。这类记录即使保存为 checkpoint 也不能重新授权 applying。原生再生能力须另行试验。

NASTRAN 原生快照最多捕获 5000 个实体；写入前的全模型保护快照最多 10000 个实体；源面最多 200 个；一次最多生成 500 个实体；显式平面分割最多 100 个 FACE。超出限制会拒绝或进入不完整结果，不自动缩小范围来声称完整。工具输入还受各自 MCP schema 上限约束。

接口审计只实现 CHEXA8/CPENTA6 面片拓扑、给定接口/边界及给定圆柱面片的几何检查。它不读取 ANSA 原生质量准则，不检查非相邻实体穿插、不证明完整 CAD 双向覆盖，也不验证求解器收敛或工程精度。pass 仅说明对应已提供数据上的检查；任何缺失覆盖项均须保留 incomplete。

写入调用遇到超时、possible_effects=true、recovery_required=true 或结果无法确认时，先查询原 operation_id，检查 ledger、备份和模型回读；不得以新 ID 重放。若 ANSA 已执行原生调用，协议记录不能让模型写入回滚成原子事务。

受限真机结果见验收范围；旧报告、离线算法结果、工具注册数和本页示例不构成工业模型或发行部署验收。

## 保留整数 CONS 种子的四边映射源面

既有 `ansa_mesh_surface` 增加 `method="mapped_existing_seeds"`，不新增 MCP 工具。默认 `method="length"` 保留原有长度模式；新模式要求 `element_type="quad"`，禁止同时传入 `target_length`，不会调用 `ApplyNewLengthToMacros` 或重新分段。

先用 `ansa_mesh_seed_read` / `ansa_mesh_seed_apply` 完成显式分段，再重新读取同一范围的 `seed_snapshot_sha256`。用 `ansa_mesh_faces` 取得源 FACE 的完整 `condition`，作为 `faces` 输入。`seed_scope.face_ids` 包含全部源面及其目标 CONS 的邻接面；`faces` 只包含真正要生成网格的源面。相邻侧壁无需同时网格化。`seed_scope.cons_ids` 必须恰好覆盖所有源面边界。

除 `operation_id`、目标、模型及完整 `faces` 条件外，新模式增加以下参数片段。示例编号只解释范围关系，运行时必须重新绑定：

```json
{
  "deck": 1,
  "method": "mapped_existing_seeds",
  "element_type": "quad",
  "seed_scope": {
    "face_ids": [1, 2, 3, 8, 10],
    "cons_ids": [38, 40, 41, 50],
    "expected_seed_snapshot_sha256": "<本次重新读取的64位小写SHA256>",
    "backup_path": "F:/verification/before-mapped-source.ansa"
  }
}
```

范围限已保存、毫米单位的 NASTRAN CAD：每个宏只能包含一个未冻结、未网格的 FACE，边界恰为四条 first-order、LINEAR、未冻结且未塌缩的开放 CONS，并以四个原生 HOT POINT 构成一个简单闭环。相对边整数分段必须已经相等；最多 4 个源 FACE、20 个上下文 FACE、32 个 CONS，生成总数不超过 200 个 CQUAD4。曲线转换结果、近似卡片长度和 `SurfaceInfo[0]` 不用于推断解析圆弧、平面或扫掠可行性。

实现使用当前版本官方原生 `Create4SidedMesh`，显式设置 `only_4sided=True`、`only_aligned=True`、`spacing="off"`、`split_method="none"`、`elem_type="quad"`，不自动调整边数或退化为三角形。先验证新备份，再实例化周界点；全模型实体/GRID 与周界物化预算沿用分段工具，另在预检预留新网格空间。保存并恢复完整 Mesh Parameters，单独恢复和回读 `mesh_change_option`。

回读逐条保护全部周界的点数、编号、坐标和设置，原有 GRID 最终卡片精确一致。2026.1.0 的映射器可能产生约 `1e-14 mm` 的边界数值漂移：只有目标 CONS 节点、只有坐标字段、且实测距离不超过固定 `1e-12 mm` 时，才显式恢复原坐标并再次精确回读，所有变化放入 `roundoff_restorations`；超过该范围或其他字段变化即失败。这是数值恢复，不是放宽质量、坐标比较或 CAD 公差。

新单元须为 CQUAD4，PID/Part 和原生宏归属正确。新增 GRID 的 `CD/PS/SEID` 必须可读且为空或数值零，防止带入位移坐标系、永久约束或超单元归属。独立拓扑核验按实际单元边重建每条 CONS 路径，并遍历整个二维格点，验证相对边乘积、全部格点、自由边、统一单元方向和逐格连接；检查不是只比较单元数。所有使用的节点用原生 `calc.ProjectPointToContainer` 投影到各自源 FACE，固定数值核验阈值为 `1e-7 mm`。全 CAD canonical export 指纹和全部非目标定义/几何/FE 保持；目标 FACE 及其 Part 已注明的显示派生量可以重算。只有目标 CONS 真正邻接 FACE 的 `center` 每分量不超过 `1e-12 mm`、正面积相对变化不超过 `1e-12` 时允许显示舍入；前提是同操作 CAD hash 完全一致，原始值与原因写入 `tessellation_changes`。非邻接 FACE 及全部 bounds、法向仍精确比较。

结果包括 `seeds_preserved`、`lattice_verified`、`faces_after[].topology`、`created_shell_ids`、节点/连接、投影结果、参数恢复和来源 ledger。ledger 的新 FE 指纹绑定当前模型代次与节点坐标；`provenance_mapping_complete` 不表示已有 CAD→SOLID 全过程映射。新增/删除实体编号在元素类型及质量等后续核验前单独保存；坐标恢复失败也继续独立观察实际增量。失败保留原任务、已知实体、备份和恢复标志，不自动回滚、重开、重新分段或换键重试。

`quality_verified=false` 明确要求再调用质量报告；源节点贴面及完整离散格点不证明连续 CAD 覆盖、曲面弦误差或扫掠体完整性。目标端盖/侧壁对应、分块覆盖和高质量实体网格仍属后续模板验证范围。真实入口为 `tests/integration/mapped_surface_ansa.py <new-owned-run-dir> <i10o_simp.step>`，仅接受新的独立实例运行目录。

## 受控 CONS 辅助曲线转换

`ansa_geometry_boundary_prepare` 是显式写操作。先创建一个与源 FACE 分离的空 Part，再用 `ansa_geometry_features` 读取 `face_ids`、`curve_ids=[]` 的来源指纹；每次最多转换 32 条属于该 FACE 边界的 CONS。辅助 Part 必须已存在、原生成员为空且卡片 `Is Empty=YES`，源模型必须已保存；备份路径要求绝对、新建、不覆盖当前文件。示例仍须补齐当前绑定的 operation_id、expected_target 和 expected_model：

~~~json
{
  "deck": 1,
  "face_ids": [1, 2, 3, 4],
  "cons_ids": [1, 3, 4, 5, 7, 8],
  "auxiliary_part_id": 2,
  "expected_source_snapshot_sha256": "<当前FACE-only特征的64位小写SHA-256>",
  "backup_path": "F:/work/before-boundary-conversion.ansa"
}
~~~

实现逐条调用 `ConsToCurves([cons])`，把返回 CURVE 与本次实际新增 CURVE 清单交叉核对；当前只接受一条 CONS 对应恰一条新 CURVE。每次设置归属前再次检查上下文，随后回读指定辅助 Part 的真实归属。全部既有卡片、几何边界和 CURVE 归属受快照保护；只允许新增 CURVE，以及辅助 Part 的成员空状态和由这些 CURVE 独立重算的包围盒、尺寸、对角线变化。既有通用快照忽略的 Part 显示时间/惯性字段仍按原规则处理。保护预算为 10000 实体，并非任意规模模型的完整 CAD 等价证明。

成功结果直接返回 `mappings`、新增 ID、各 CURVE 的 IGES 解析/原生长度和端点检查、前后来源指纹及 ledger。`provenance_status=verified` 只证明本次观察到的 CONS→CURVE 创建对应；`source_geometry_equivalence=not_proven`、`geometry_status=incomplete` 和 `usable_for_mutation=false` 明确保留几何等价边界。严格线性 112 的末端冗余值不一致会单独披露。

转换复用 ANSA 自带 `base.ConsToCurves(cons)`；2026.1.0 官方签名只接受 CONS 列表，成功返回新 CURVE 列表，失败返回 None，没有公开精度/容差参数。MCP 在其外层补充受控执行及核验，不另写转换算法，遵循[原生能力优先准则](../development.md#原生能力优先)。

官方另有 `MatchCurvesAndCons(curves, cons)`，按 tolerance mode 返回匹配实体对，不返回距离值。本轮仅完成该接口的文档核对，未将其匹配结果等同于连续等价证明，也未为此修改全局容差；以下采样诊断仍复用原生评估和投影。

每条映射的 `conversion_fidelity` 另以 `EvaluateCurvePoint` 在辅助 CURVE 上读取九个固定 U 位置，再用官方 `calc.ProjectPointToCons` 投影回对应原 CONS。U 为 CURVE 评估 API 的归一化位置，不是原 CONS 的原生参数；单条最多 9 次评估和 9 次投影。投影落在 CONS 延长线上时，按 API 返回的最近端点计算距离，保留延长线投影与吸附位置，避免以延长线距离误报贴合。结果包含全部样本、最大已观测距离、RMS、有效样本数和调用数；非法、非有限、0 哨兵及异常均保留为逐点诊断，部分有效数据不丢弃。

九点全部有效时状态为 `sampled_only`，有缺项时为 `incomplete`，不设置工程公差或给出整体 pass。`continuous_equivalence_proven=false`、`reverse_coverage_proven=false`、`usable_for_mutation=false` 明确采样不证明连续等价及反向覆盖。转换/范围成功可与采样不完整并存，调用方必须分别查看这些状态；不能由 ledger 的 verified 推断几何精度通过。不支持的样条仍可记录采样偏差，但不会因此变成解析圆弧或自动绑定原 CONS 分段。

ANSA 2026.1.0 合成圆柱探针中，4 条半圆 CONS 转换为 126 样条，曲线长度与理论圆弧存在差异。因此不能用转换成功或端点相同证明原圆弧被精确保留，不能据此恢复孔径或自动布置原 CONS 圆弧种子。辅助 CURVE 是持久模型实体；不自动删除、拟合、重开、回滚或重试。调用失败立即停止后续转换并保留已观察到的部分 ID；每条中间归属目前不独立落盘，进程崩溃时仍需查原任务、备份及原生模型，禁止凭内存记录重新授权或用新 ID 重放。

## CAD 归属驱动的边界检查

`ansa_mesh_interface_audit` 可选 `cad_boundary`，该阶段未增加工具（当时为 77 项）。此模式不接受同时提供调用方的 `expected_boundary` 或 `protected_cylinders`，避免混淆两种归属来源。例如：

~~~json
{
  "cad_boundary": {
    "face_ids": [1, 2],
    "tolerance": 0.02,
    "cylinders": [{
      "label": "protected-bore",
      "face_ids": [1, 2],
      "center": [40.0, -26.0, 0.0],
      "axis": [0.0, 0.0, 1.0],
      "radius": 8.0,
      "tolerance": 0.02
    }]
  }
}
~~~

这是请求的局部参数；仍需显式传入目标、模型、deck、SOLID 范围与 operation_id。FACE 列表表示该 SOLID 范围的预期设计边界。内部切分接口不能声明为外边界；仅提供一个孔壁时，其余未覆盖自由面会明确列出。

归属来自 `GetFaceMacro` / `GetFacesOfMacros` / `GetMacroShells`，先得到成员再检查坐标。当前仅在宏恰好包含该一个 FACE，且取得非空完整 SHELL 清单时确认逐 FACE 归属。GetMacroShells 的官方契约是返回全部成员；在 NASTRAN FACE 卡片没有 `Num.Quads/Num.Trias` 时，计数来源明确标为完整原生返回列表。若卡片提供这两个字段，则另行核对计数，缺值或不一致保持 incomplete。多 FACE 宏保留原始宏成员，但逐 FACE 归属保持 incomplete，不把同一宏的全部网格重复分配给每个 FACE。宏展开同样受累计 100 FACE 预算约束，并缓存共享宏。

完整 CAD SHELL 与实际实体自由面按节点 ID 双向核对：重复归属、错误的内部面或不共形面失败，额外自由面与无网格 FACE 保留缺口。CAD SHELL 中未出现在目标 SOLID 的节点也会保留，不按径向误差或最近距离筛除。保护圆柱由 FACE 成员派生全部待测节点。

`calc.ProjectPointToContainer` 将所有已归属节点投影到它们所属的 CAD FACE，报告完整节点对、距离、极值、失败和未知结果。最多 5,000 个节点/FACE 对；超预算时该步骤明确 incomplete，不只取前若干点。原生 FACE/SHELL 提取最多 100 FACE、5,000 SHELL 出现次数；不自动扩大选择。

证据存于 `/sections/cad_boundary`，其中 `membership`、`projection`、`native_faces` 分别记录双向拓扑归属、全部节点投影及原生来源。`summary.cad_membership_status` 与 `summary.cad_nodal_projection_status` 单列。节点检查通过仍将 `continuous_cad_coverage_proven=false`，整体几何保持 incomplete；这尚未证明任意曲面内部偏差、连续 CAD→FE 覆盖和空间交叠。

## CAD 分割完整性的离线复用

正式模块 `cad_domain_audit.py` 的 `audit_manifest` 接收清单路径和调用方注入的 CAD 内核；回归入口 `tests/integration/cad_domain_offline.py` 在独立 Python/Gmsh 环境验证完整、缺片、重复和多余区域。它不连接 ANSA，不在 ANSA 进程加载 Gmsh DLL，也未注册为独立 MCP 工具。运行时依赖不增加 Gmsh；已验收的离线版本为 4.15.2。

清单要求 `schema_version=1`、`units="mm"`、`expected_source_face_ids` 和 `groups`。每组包含 `label`、`source_face_ids`、`candidate_face_ids`、`reference_files`、`candidate_files`；每个文件项包含 `path` 与 `sha256`。路径相对清单目录解析，也可明确使用绝对路径。原始面清单须由原生模型全量回读提供，不能只列准备检查的面。各组必须恰好覆盖该清单，源面或候选面不能重复跨组；若多个源面被合并，应放在同一组。上限为 2,000 个原始 FACE、1,000 组，每组最多 2,000 个源/候选 FACE。

每组原始/分片 CAD 各自求并集，检查“原始减分片”的缺失域、“分片减原始”的额外域，以及“分片面积和减并集面积”的重复域。再用交集校验两边面积守恒。文件或清单变化、面数与声明不符、布尔异常、不守恒均保持 incomplete。默认布尔线性容差 `1e-5 mm`，面积容差 `1e-3 mm² + 1e-8 × 原始面积`，均可显式调整。不能把曲面面积总和相等或支撑面参数相同单独作为覆盖证据。

结果分别报告 `missing`、`excess`、`duplicate_area` 和 `boolean_conservation_error`，并保留残差区域包围框。`native_model_binding_verified=false` 明确表示原生来源及完整性不由离线文件自行证明。CAD 分割通过也不证明 FE 面片内部误差、体网格完整性、共形或质量通过；曲面节点全部贴合仍可能存在弦误差。

在后续 MCP 编排中，可把它用作每轮切分的检查关卡：先回读和导出绑定到同一检查点的完整源面/结果面，再核验；只对已通过部分推进源面网格和扫掠。检查失败应定位残差区域后局部修正；自动原生导出、父子关系持久化、撤销恢复及这种统一编排仍待接入，当前离线审计不会执行这些模型写操作。

## 实体交叠的分层检查

`solid_overlap.audit_solid_overlaps` 为标准库离线算法，支持线性 CHEXA8/CPENTA6；AABB BVH 保留共享节点与共享面的候选对。有效凸、平面多面体使用 SAT；扭曲或非凸实体只在节点凸包证明可排除时清除，其余返回 incomplete。接触结果包含某一轴的 `witness_axis_overlap`，不冒充所有轴的最小值。投影临界值或近似平面余量覆盖阈值时保留 unknown；浮点保护不等同于任意尺度的精确谓词证明。

`solid_overlap_batches.plan_native_overlap_batches` 将完整未解决清单组织为两组互不相交、各最多 200 实体的批次。每个候选对在 `covered_pairs` 中只分配一次，且必须跨组出现；实际两组的笛卡尔积可能在其他批次重复包含该对，并包含额外对，不承诺原生逐对去重。超出预算明确保留未处理对。调用既有 `ansa_mesh_intersections` 并指定 `entity_type="SOLID"`，详细字段见[操作契约](operations.md)。每轮仍须核对目标实例、模型上下文和全量 FE 指纹，不能把原生零返回推广为所有版本、公式及容差的数学证明。

最终独立真机完成 75 次 MCP 任务（69 成功、6 次预期拒绝或恢复诊断）；780 项单元、83 工具模拟、84 文件 staging 通过。i23 的 Part 显示量保存重开舍入单列，其他卡片及坐标精确比较；完整范围见[验收记录](../acceptance.md#2026-10-25-原始-cad-边界到原生-cons-分段受限验收)。

## 矩形多块准备与增量填充

`ansa_mesh_map_blocks_prepare(action="plan"/"apply", deck, face_groups, target_length, property_id)` 接受 2–20 个明确六 FACE 分组。同一 FACE 可作为两个块的共享界面，但完整选面必须恰好覆盖当前单 Part CAD。每块必须是坐标轴对齐矩形体，完整原生 FACE/CONS/HOT POINT 身份与有限导出域同时通过。只共边或共点而没有合法实体邻域的组合、不同 ID 的重合节点、重复面、相交体和遗漏面均不进入写入。

初始模型须无 SHELL/SOLID/VOLUME/CURVE，单源 PSHELL；指定已有 PSOLID/MAT1 且密度为正。统一 `target_length` 必须将每条边分成 1–20 个整数段。全部模型≤10000个实体，边界合集≤400 CQUAD4，最终 SOLID≤5000。共享面只计一次，端盖方格的预算按两个方向段数乘积计算。有限预算是当前封装的保护条件，不是 ANSA 原生算法规模上限。

apply 要求新备份和 plan 指纹，只调用一次 `mesh.MapBlock(完整显式FACEs)`。新 VOLUME 按实际完整 FACE 集匹配分组，不依赖返回顺序。仅将本次新体绑定指定已有属性，核验后清理本次新建且未使用的默认属性/材料，保护其他实体；逐面独立核对规则格点和共用节点，恢复 Mesh Parameters、Quality Criteria 与 F11 配置。输出是规则面网格和未填充体。

`ansa_mesh_map_block_fill` 的新多块模式为 `volume_ids=[...]`，与旧 `volume_id` 严格二选一。它要求当前模型全部 VOLUME 均为上述同 Part、完整矩形组合的原生 MapBlock；允许其他块已填充，所选块必须尚未填充。全模型原有 SOLID 必须有唯一已填块归属；既有实体卡片、连接、坐标和共享边界节点严格保护。一次 `VolumesRemesh(所选体列表)` 后，按各块方向/层数独立匹配全部新增 CHEXA，核对体积、完整格点、归属和 PSOLID/Part 汇总。

增量验收使用“当前已填块”形成外皮：与尚未填充邻块相接的共享面，此时是合法外表面；两侧均填充后成为明确双侧接口，要求相同 FACE、相同 GRID 身份和完整成对面片。不会把中间状态误判为最终装配缺口，也不会把最终内部界面当自由面漏检。`all_blocks_filled` 只报告所有原生块状态；项目质量仍需独立 quality report。

原生几何可能出现派生显示值舍入。工具先依据原始 HOT POINT 重新计算 CONS 包围框、平面 FACE 面积/包围框和重心，仅在限定 8 ULP 数值包络内对比较副本做一致化，保留原始卡片和差值。单位切向分量使用同样受限的机器精度包络。实际 HOT POINT/GRID 坐标、材料定义、连接和完整 CAD 导出哈希不参与这种一致化；这不是可放宽真实几何尺寸的工程容差。

### T2 候选与有限连续域

`ansa_geometry_sweep_candidates` 对多于六 FACE 的选择增加 `classification.rectangular_assembly`。先从现有原生矩形 FACE 及八角点身份组成最多20块，再验证全选面覆盖、接口邻接、整数分段和预算。单靠角点只返回 `topology_candidate`；每块全部六 FACE 的有限连续导出域验证通过后，才提供 `prepare_options={face_groups,target_length}`。它仍为只读提示，`usable_for_mutation=false`；prepare 会重新预检，不复用只读候选作为写权限。

连续域验证扩展为低阶仿射平面上完整的凸四边形局部 UV 修剪域，必须同时核对三维/参数域曲线、偏好语义及完整闭环。未知曲面、洞、额外环和未知表示继续拒绝。此证明位于导出精度层级，`exact_native_CAD_equivalence_proven=false`；不能推广为任意 CAD 内核等价。

`cad_sweep_geometry.capture_many` 在一次主线程读取内复用完全相同 FACE、角点身份/坐标和容差的证明，不跨操作保存局部导出缓存。候选的持久派生缓存最多8项/4MiB；每次命中和证据分页都先重新捕获所选完整 CAD/原生拓扑及目标上下文。T2 只覆盖当前选择；原始 CAD→切分后模型的完整性必须另做证明，不能从当前块全部填满倒推。

### 原始矩形域到分片覆盖

`rectangular_coverage.audit_partition` 是有限证据核查函数，不是新 MCP 工具或切分器。输入原始完整 witness/有限 FACE 导出证明，以及结果完整 witness/每块有限证明/六 FACE 分组。它将全部矩形端点形成的区间单元逐格核对，分别拒绝缺失、额外及多重覆盖，避免不同位置的缺失和额外体积相互抵消。零体积或浮点无法区分的区间拒绝；只在受限派生端点包络内视舍入一致。

纯函数保留 `native_model_binding_verified=false`；调用方必须将实际原始/结果全量 FACE 清单、导出文件哈希和同一检查点的原生身份绑定起来。本轮独立集成记录完成此绑定，但未把任意工业曲面纳入该有限证明。通用 CAD 布尔覆盖仍可用前文离线 CAD 内核核查。

### 结果持久化与恢复语义

结构化写结果在持久化前统一转换为有限 JSON：整数对象键转字符串、tuple转数组，字符串化键冲突拒绝。超过192KiB时完整写入 `mesh_native_operation_result` 证据并返回有界描述符，保留真实 `ok/scope_verified/possible_effects/recovery_required`、台账和实体增量，不能将分页描述符当成功标志。

执行器若已完成原生调用但结果无法序列化，会把原任务如实关闭为 `failed`，注明结果序列化阶段及可能已生效/需要恢复，不自动重放原生操作。磁盘或 SQLite 持久化失败不套用这种结果序列化回退；状态不明时继续查原任务、原 operation ID 和独立模型回读。

## 客户端检查点编排

这是外部Python客户端库，不增加MCP工具，也不安装到ANSA主线程。注入 `async call_tool(name, arguments)`，其返回值为公开MCP的 `structuredContent` 字典。必须传完整四字段目标绑定 `backend_id/instance_id/session_id/generation` 和模型 `epoch`，可从实际任务回执或模型snapshot的信封读取；不能只传连接状态中的部分目标字段。

```python
from ansa_mcp_plugin.workflows import MapBlockJobClient

job = MapBlockJobClient(call_tool)
result = await job.run(
    deck=1,
    fill_groups=[[volume_a], [volume_b]],
    quality_profile=explicit_project_profile,
    job_dir=absolute_new_job_directory,
    expected_target=full_target_binding,
    expected_model={"epoch": current_model_epoch},
)
# 新客户端；保持同一目标、代次和当前模型，不自动重开检查点。
result = await MapBlockJobClient(call_tool).resume(
    result["manifest_path"],
    expected_target=full_target_binding,
    expected_model={"epoch": current_model_epoch},
)
```

组数和唯一体数各最多20，体不能跨组重复，累计新增实体≤5000。库不进行几何切分或MapBlock准备；调用方先完成这些阶段。`job_dir` 必须是父目录已存在的绝对新目录，拒绝覆盖和符号链接/重解析路径；manifest采用原子替换及文件锁。每次公开调用的operation ID、请求和绑定先落盘；派发后丢回执时保留原operation ID查询，绝不换ID自动重试。

状态包括 `planned/applying/verified/checkpointed/awaiting_original_result/needs_attention/complete`。未终止任务有界查询后保留等待；未知任务、失败、可能副作用、原结果/台账校验不符或目标模型变化时停止。每组完整回执及新增实体ID、provenance ledger ID/hash、独立保存回执和检查点文件哈希持久化。完成前要求全部组已检查点化，并对本job所有新增实体得到明确profile的全量质量 `passed`、0 failed、0 unknown。此处complete仅指显式填充工作流；它不声明任意CAD完整性、所有工程质量项或求解验收。
