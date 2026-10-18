# ABAQUS 工具契约

本文概括 ABAQUS deck 的网格、机构和接触工具。任务封装、持久状态与恢复语义见[任务与 API](tasks-api.md)，其他模型和检查工具见[通用操作](operations.md)。支持边界以[验收索引](../acceptance.md)和[工具映射](../tools.md)为准；本文只描述契约，不声明新的求解或画布验收。

## 共同约定

- 调用使用当前绑定目标的 `expected_target`、`expected_model`、显式 `deck` 和唯一 `operation_id`。写操作先核对目标、模型代次和输入卡片条件。
- 任务型工具的业务回包位于 `data.result`。写操作成功要求独立回读；`scope_verified=true` 表示本次声明的范围已核对，`possible_effects=true` 表示可能已有模型变化或存在未覆盖范围。
- 需要 `backup_path` 的写工具必须使用指向新文件的绝对 `.ansa` 路径，且不能等于当前模型路径。`ansa_mesh_surface` 另以会话级 `.ansa_mpar` 快照恢复网格参数，该快照不是模型备份。
- 网格、机构和接触工具不调用 Abaqus 求解器；原生 API 调用成功不能替代模型回读。
- 结果可能包含 `partial`、`complete`、`errors`、`warnings` 或证据截断字段。调用方须先检查这些字段，再决定是否继续写入。

## 四边形表面网格

先调用 `ansa_mesh_faces` 读取 1–100 个 FACE 的卡片、完整宏、附着 SHELL 和未网格前置条件，再把返回的条件交给 `ansa_mesh_surface`。

| 参数 | 契约 |
| --- | --- |
| `faces` | 1–100 个 `{id, expected_fields, expected_macro_id, expected_shell_ids}`；每个宏的 SHELL ID 最多 2000 项 |
| `target_length` | 有限正数，使用当前模型长度单位 |
| `element_type` | `tria` 或 `quad`；默认 `tria`，`quad` 仅支持 ABAQUS |
| `deck` | 省略时使用当前 deck；显式值必须与当前 deck 一致 |

quad 路径只接受完整宏内未网格、未冻结的 FACE。目标长度应用于显式范围的 CONS；边界关联范围外 FACE 时在写入前拒绝。回包包含 `perimeter_scope`、实际单元类型、逐 SHELL 四节点连通与 PID、真实边长、`edge_length_min/max`、包围盒、`created_shell_ids`、参数恢复状态和 `scope_verified`。边长须为正数且不超过目标长度的 1.5 倍；目标长度不保证每条边精确等于该值。

该入口不覆盖已有 SHELL 重划、相邻多宏、高阶单元或任意曲面质量。完成后仍应独立检查工程质量和网格分布。

## Map HEXA8 网格

`ansa_mesh_hex_map` 以一个明确 Map volume 为范围，调用 `mesh.VolumesMap` 后核对新增 VOLUME、HEXA8、节点、属性、体积和 Part 归属。

| 参数 | 契约 |
| --- | --- |
| `master`、`slave`、`round` | 各 1–200 个 SHELL 条件；三组不得重复，总计最多 400 项 |
| `property` | 必须为当前 deck 的 `SOLID_SECTION`，`expected_fields` 至少包含 `MID` |
| `part` | 可选 `ANSAPART` 条件；显式给出时 deck 必须匹配 |
| `method` | `Normal parts` 或 `Thin parts`，默认前者 |
| `steps` | 严格整数 1–10000；与 `target_length` 二选一 |
| `target_length` | 有限正数；只支持可确认平行端面与直扫掠间距的边界 |
| `light_volume_representation` | 严格布尔值，默认 `false` |

横向离散由边界网格决定。`target_length` 只用于推导层数，不能证明三个方向都满足目标尺寸。FACE 边界会在写入前拒绝；复杂曲面和高阶单元不在当前入口的验收范围内。

回包包含 `requested`（解析后的 steps 来源、扫掠长度和平面性）、`before/after`、`map_readback`、`mesh_readback`、`scope_card_coverage`、`scope_verified` 和 `possible_effects`。成功要求逐项回读 VOLUME 的 Type、Status、Method、steps、PID、Grids、Hexas 和体积，以及新增 HEXA8 的节点、属性、中心与 Gauss 点 Jacobian。

## 网格后续操作

`ansa_mesh_workflow` 通过 `request.action` 选择一个动作。`request` 必须包含当前 ABAQUS `deck` 和新的绝对 `.ansa` `backup_path`；原生写入只调用一次。

| 动作 | 必要范围与回读重点 |
| --- | --- |
| `extrude_quad` | 1–200 个 QUAD SHELL、非零有限 `vector`、1–1000 `steps`、Part 和 SOLID_SECTION；`shells × steps ≤ 10000`；核对层间节点、HEXA8、体积、属性及 Part 归属 |
| `mirror_copy` | 1–200 个 SHELL/SOLID、三点镜面和 Part；核对坐标映射、边面拓扑及正向 HEXA8；保留原单元 |
| `merge_nodes` | 1–200 对互不交叠的 NODE；源/目标坐标须精确重合且不塌缩；核对 FE 连接、SET 等引用及源节点删除范围 |
| `release_mesh` | 1–200 个显式 SHELL/SOLID 条件；只删除选定 FE，保留节点，不清理孤立节点 |

成功回包给出 `before/after` 摘要、元素卡片 SHA-256、`api_invoked`、`scope_verified`、`verification_scope` 和 `unverified_model_scope`。成功时仍保守返回 `possible_effects=true`，因为非 FE 几何卡片和组/集合关联未纳入全量核对。

VOLUME 成员的 FE 镜像会在写入前拒绝；VOLUME 释放尚未开放。CAD 关联释放、任意曲面扫掠、高阶单元和大型装配扩展不在当前契约内。

## 机构定义

| 工具 | 参数与回包 |
| --- | --- |
| `ansa_kinematics_body_create` | `name`、严格布尔 `ground`、最多 200 个 NODE/SHELL/SOLID/SECTION/SHELL_SECTION/SOLID_SECTION/ANSAPART 成员和备份；回读 KIN_BODY 卡片、实际成员、自动 COG marker、成员标记及既有刚体未变化 |
| `ansa_kinematics_joint_create` | `joint_type="REVOLUTE"` 或 `"SLIDER"`、两个不同 KIN_BODY、有限锚点和非零轴；轴在写入前归一化；回读 KIN_JOINT 完整卡片、锚点与轴 |
| `ansa_kinematics_config_create` | `joint_ids` 1–100 个唯一 ID，可选 `actuator_joint_id`（必须属于该集合）；回读 KIN_CONFIG、关节、刚体和原生模型摘要 |
| `ansa_kinematics_inspect` | 一个 `config_id`，或 `body_ids`/`joint_ids` 各最多 200 个、`config_ids` 最多 100 个；返回完整卡片、成员和模型计数 |

配置固定使用 `mm / tonne / newton / sec / deg`。地面刚体不接受显式成员；非地面刚体至少需要一个成员。配置关节集合最多 100 个。创建配置会持久保存可选驱动副定义，但不调用 `MoveConfig`、`SaveAsInitialPosition` 或求解。

`config_id` 模式通过关节引用补齐地面闭包；`native_body_ids` 保留原生配置列表范围。检查成功只说明定义和归属可回读，不证明自由度、运动轨迹或动力学有效。

## 接触回读

`ansa_contact_inspect` 为只读入口：显式 `deck`、`relation_type="TIE"` 或 `"CONTACT_PAIR"`、1–20 个唯一关系 ID，以及有限正数 `tolerance`。

回包包含关系完整卡片、主从面引用、TIE 选项、`interaction`、`surfaces`、`geometry_comparison`、`face_record_summary`、`projection_budget` 和关系级 `status/complete/errors`。CONTACT_PAIR 沿 INTERACTION 回读 SURFACE_INTERACTION 与 FRICTION，常数摩擦系数与依赖数据分开。

表面展开支持嵌套 SET、带方向的 SHELL_SECTION、四节点 SHELL 和 HEXA8 SOLIDFACET；面数据包含节点坐标、面积、法向、SPOS/SNEG、壳厚和显式偏置。凹、交叉或退化四边形不能作为完整有效面接受。

`reference_gap` 使用 NODE 参考坐标，返回双向节点到三角面的距离及从面节点投影覆盖率。`uniform_section_physical_gap` 仅在均匀壳厚和显式截面偏置可读时计算物理表面距离。投影覆盖是节点采样，不代表 Abaqus 求解器的 TIE 判定或面积覆盖。

每表面最多展开 2000 个成员；每请求最多分析 10000 个面、100000 次投影测试，并保留最多 200 条面记录样本。数据不完整时返回 `partial`，明细省略量附 SHA-256。大装配性能、复杂厚度、非零单元 ZOFFS 和求解器接触规则不在当前契约内。

## 质量与表面成员

`ansa_model_inspect(action="mass_properties")` 接受 1–200 个 ABAQUS MATERIAL、SHELL_SECTION、SOLID_SECTION、ANSAPART、ANSAGROUP、SHELL 或 SOLID 根，可选 `element_types=["SHELL","SOLID"]` 和 `no_nsm`。返回标量质量、质心、惯性、主轴、纳入类型、去重单元数、属性/材料范围和范围 SHA-256；最多扫描 1000000 个引用、选择 500000 个单元和 2000 个属性。

该质量入口要求可核实且密度为正的常数材料；空范围、材料不明或非正质量失败。单位沿用当前模型，不换算，也不代表全模型质量。

`ansa_model_inspect(action="surface_members")` 接受一个 ABAQUS SET 的 `set_id`、`offset`、`limit`（1–200）和可选 `expand_nested`。回包包含分页成员、`total`、`complete`、`all_members_readable`、`has_more` 和错误。直接成员保留 SOLIDFACET 身份及 SPOS/SNEG；嵌套展开最多 16 层、200000 个引用。该结果只证明成员可读，不是接触间隙或 TIE 覆盖验收。
