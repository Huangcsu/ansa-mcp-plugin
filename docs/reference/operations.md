# 通用操作工具契约

本文概括模型、实体、几何、检查和连接工具的共同契约。ABAQUS 网格与接触见[ABAQUS 工具契约](abaqus.md)，任务持久状态、脚本和 API 查询见[任务与 API](tasks-api.md)。每项工具的限定验收范围见[工具映射](../tools.md)和[验收索引](../acceptance.md)。

## 共同前提

- 模型任务先取得当前 `expected_target` 和 `expected_model`；目标或模型代次过期时写入会失败。
- 模型写入使用唯一 `operation_id`，并在执行前核对目标、模型代次、实体身份及显式前置字段。
- 任务型工具结果位于 `data.result`；连接状态及同步快照等入口直接返回 `data`。任务成功表示接口执行完成，问题计数和业务结论仍须读取结果字段。
- 模型写入遵循单活动目标、单模型和所属 ANSA 主线程；工具不自动修复、重放或清理失败后的部分效果。

## 模型生命周期与文件

| 工具 | 关键参数 | 回包与限制 |
| --- | --- | --- |
| `ansa_model_new` | `preservation="backup"`（默认）或 `"discard"`；备份模式要求 `backup_path` | 创建新模型；返回动作、路径、原生返回码和备份记录 |
| `ansa_model_open` | 绝对 `path`、备份模式和 `backup_path` | 只打开存在的绝对 `.ansa`；拒绝路径穿越和重解析点；返回 `model_after` 所需上下文 |
| `ansa_model_save` | 绝对 `path`、`overwrite` | 另存副本；返回保存成功和路径 |
| `ansa_model_save_current` | `expected_path`、严格 `overwrite=true` | 当前 `DataBaseName()` 必须与路径一致；返回文件大小、SHA-256 和实际当前路径 |
| `ansa_model_autosave` | 已存在的绝对 `directory` | 生成唯一 `.ansa` 副本，保持当前数据库路径，返回文件指纹 |
| `ansa_model_export` | `NASTRAN`/`ABAQUS`/`LSDYNA`/`STEP`、绝对 `path`、`overwrite`、`scope` | 求解器只支持 `scope="all"`；STEP 只支持显式 `scope="visible"`，并在所属窗口确认参数 |
| `ansa_model_import` | solver 或 STEP 文件、`merge`/`replace`、备份模式和 `backup_path` | 求解器文件只支持 `merge` 并固定 ID 偏移；STEP 只支持显式 `replace`；返回备份、导入数量、`model_after` 和文件指纹 |

路径必须是绝对路径，父目录须存在，已有目标必须显式覆盖。备份不能覆盖源文件或当前模型。导入失败可能有模型副作用；先查询原任务并重新读取模型。

## 模型信息

`ansa_connection_status` 列出注册实例和面板绑定目标。`ansa_model_snapshot` 返回目标、模型代次、版本、deck、路径和写入保护状态；`ansa_panel_show` 返回 `queued` 只表示请求已受理，可见性需另行检查。`ansa_model_summary` 读取当前模型的原生总数、八类计数、deck 名称和节点类型。

| 工具 | 回包重点 |
| --- | --- |
| `ansa_model_summary` | `deck`、`deck_name`、`database`、`entity_count`、`node_type`、`entity_counts`、`count_types`、`counts_complete`、`count_errors` |
| `ansa_model_includes` | 按 ID 升序的 INCLUDE 分页；`total`、`offset`、`limit`、`includes` |
| `ansa_model_inspect(action="geometry_inventory")` | 当前 deck 的可用实体类型计数；`counts`、`has_more` |
| `ansa_model_inspect(action="face_properties")` | 1–100 个 FACE 的面积、中心及可选方向/单位方向/包围盒；逐字段错误和完整性 |
| `ansa_model_inspect(action="entity_metrics")` | 单个显式实体的面积、体积或质量；NASTRAN/OPTISTRUCT 质量为三方向分量，ABAQUS 质量为标量 |

`entity_count` 是原生全实体集合大小，包含内部对象，不能当作分类计数之和或“模型已加载”标志。分类不可读取时计数为 `null` 且 `counts_complete=false`；不得把未知数量解释为零。

FACE 方向来自 `GetFaceOrientation`，不是曲面局部法向；长度和面积/体积沿用当前模型单位。质量查询可能忽略不支持或未网格实体，零值不能单独证明计算完整。

## 通用实体

| 工具 | 参数 | 回包 |
| --- | --- | --- |
| `ansa_entity_count` | `entity_type`、可选 `deck` | `entity_type`、`count` |
| `ansa_entity_list` | `entity_type`、可选 `fields`、`offset`、1–200 `limit` | `total`、`entities`（引用与字段） |
| `ansa_entity_get` | `entity_type`、正整数 `entity_id`、可选 `fields` | `entity` 与已核实字段 |
| `ansa_entity_find` | `pattern`、`match`、分页 | `total`、`entities` |
| `ansa_entity_bounds` | `entity_type`、1–200 个唯一 ID | `bounds.min/max` |
| `ansa_node_coordinates` | 1–200 个 NASTRAN GRID ID | `nodes` 与 `missing` |

`ansa_entity_create` 接受当前 deck 的 `fields`；首次创建非空字段时必须有可信字段定义。`ansa_entity_update` 同时要求 `expected_fields` 和 `fields`，只在全部旧值匹配后更新。`ansa_entity_delete` 接受 1–200 个 `{entity_id, expected_fields}`，整批预检后使用 `force=false/compress=false` 删除。

通用删除明确拒绝 FACE；FACE 删除必须使用几何删除入口。查询、字段名称和实体类型均以当前 deck 的原生卡片为准。真机验收只覆盖已列明的实体类型，其他类型需要单独核对。

## Part 与 SET

- `ansa_part_create`：创建 ANSAPART，`name` 必填，`module_id` 可选；回读实体身份、`Name` 和 `Module Id`。
- `ansa_set_members`：读取一个 SET 的直接成员，返回 `{deck,type,id}`；嵌套 SET 只作为成员保留，不展开。
- `ansa_set_add`：要求 SET 的非空 `expected_fields`、完整 `expected_members` 和 1–200 个待加入普通成员；拒绝重复、自包含和新增嵌套 SET，加入后核对成员恰为旧集合与新增集合的并集。

SET 直接成员最多 200 项。分页读取不能替代写入所需的完整成员条件。

## GRID 与补充网格

| 工具 | 契约 |
| --- | --- |
| `ansa_grid_create`、`ansa_grid_read`、`ansa_grid_update`、`ansa_grid_delete` | 仅 NASTRAN GRID；创建/读取/更新/删除使用显式坐标，更新和删除要求当前 `expected_position`；回读实体身份和坐标 |
| `ansa_mesh_settings` | `action="read"` 或 `"update"`；更新必须同时给完整 `expected_settings` 和显式变更项；只影响当前 ANSA 会话，不写默认配置 |
| `ansa_mesh_delete` | 1–200 个显式 SHELL 条件；仅删除这些 SHELL，不清理节点或几何 |
| `ansa_mesh_volume` | 仅已有未网格 NASTRAN VOLUME、六个 CQUAD4 轴对齐封闭盒体、既有 PSOLID/已定义 MAT1；先核实新 `.ansa` 备份，单次 TETRA RAPID，回读完整 FE、拓扑和体积 |

`ansa_mesh_volume` 的原生结果可能包含 PYRAMID 和内部节点，不能按纯四面体或固定单元数量解释。`ansa_grid_*` 和上述网格入口的当前验收范围以其工具映射记录为准。

## 几何排序、预览与删除

`ansa_geometry_select` 和 `ansa_geometry_preview` 支持 `largest_area`、`smallest_area`、`highest_z`、`lowest_z`、`nearest_center`、`normal_pos_z`、`normal_neg_z`。显式 `face_ids` 最多 10000 个；省略时扫描全模型，超过上限拒绝。`limit` 为 1–200；`nearest_center` 必须给 `point`，该点是面中心距离，不是面表面距离。

预览返回候选 FACE、面积、中心、分数、可选单位法向、拟删除 ID、总面积和包围盒。`include_delete_conditions=true` 时 `limit` 最多 100，并附加完整删除条件；预览不改变模型、选择或显示。

`ansa_geometry_delete` 接受 1–100 个由预览取得的完整条件。每个 FACE 必须是 NASTRAN 中独立、未冻结、未网格且边界 CONS 不邻接其他 FACE 的单面宏。删除使用 `force=false/compress=false`，回读指定 FACE、范围外 FACE、边界 CONS 及受保护实体身份。

回包包括 `requested_face_ids`、删除前后 FACE 数、残留/新增/范围外变化、CONS 变化、`scope_verified` 和 `possible_effects`。失败后可能存在部分删除；先查询原任务并回读，不能改键重放。

## 检查工具

| 工具 | 显式范围 | 回包与语义 |
| --- | --- | --- |
| `ansa_check_run` | 1–200 个 NASTRAN GRID；`consider_nodes_with_mass_free` 为严格布尔值 | FreeNodes 静默检查；`issue_counts`、`failed_entity_ids`、连接上下文和报告树 |
| `ansa_mesh_check` | 1–200 个 NASTRAN CQUAD4/CTRIA3；`angle` 满足 `0 < angle < 180`，默认 120 | 固定 `sharp_edges_type=0/exclude_edges=0`；返回叶问题统计和失败 SHELL ID |
| `ansa_mesh_failed_elements` | 1–200 个 NASTRAN CQUAD4/CTRIA3 | 使用当前会话全局 MeshQuality 准则，返回准则全文/SHA-256、问题统计和去重失败 ID；零值只适用于本次范围 |
| `ansa_mesh_intersections` | 两组各 1–200 个唯一 NASTRAN SHELL，组间不重叠 | 只检查组间相交；原生计数、原生列表项数与去重 SHELL 分别返回 |
| `ansa_mesh_penetrations` | 两组各 1–200 个唯一 NASTRAN SHELL；`thickness_mode=property/user`，`factor` 为正有限数 | 两组 PSHELL 属性集合不能相交；分别返回相交、穿透和并集实体；类别可以重叠 |
| `ansa_rigid_dependencies` | 1–200 个 NASTRAN RBE2 | 核实 GN、GM、CM 和 GRID 连接闭包；返回问题叶、去重 RBE2/GRID 和报告树 |
| `ansa_model_inspect(action="geometry_check")` | 1–100 个 NASTRAN FACE；`check="basic"`，严格布尔 `single_cons` | 核验边界 CONS 归属，返回完整报告树、问题叶、`failed_cons_ids`、`affected_face_ids` |
| `ansa_model_inspect(action="mesh_quality")` | 显式 FACE/SHELL/PSHELL 选择 | 只返回原生质量报告；不自动修复 |

检查成功表示检查已执行。调用方须读取 `issue_counts`、`failed_entity_ids` 和 `reports` 判断问题；零问题仅适用于显式范围。所有检查拒绝全模型或隐含可见集，不修复、不改变显示，也不写入模型文件。

## 显示与文件集基准

- `ansa_model_inspect(action="visibility_snapshot")` 读取 FACE/SHELL 的类型和 `visible_ids`；监控范围最多 10000 个实体。
- `ansa_model_inspect(action="entity_visibility")` 的显式 `show_only/show_also/hide` 需要 1–200 个实体 ID 和刚从快照取得的 `expected_visible`；`near` 需要正半径，`neighb` 需要 1–20 步。Near/Neighb 是显示写入，可能联动其他类型。
- `ansa_benchmark_run` 对当前 NASTRAN 的 `.ansa/.step/.stp` 文件集做有界顺序统计，默认最多 10 个文件，输出分类计数和排名；成功后打开备份，Open 失败或身份未知时停止后续，不自动恢复。该工具不是工程性能或求解基准。

## 连接对象与连接实现

| 工具 | 关键契约 |
| --- | --- |
| `ansa_connection_list` | NASTRAN `CONNECTOR_ENTITY` 和六类连接点；`kind=all/connector/point`；稳定分页 1–200，收集范围最多 10000 项 |
| `ansa_connection_create` | 六类点、有限三维坐标、正直径、两个不同 ANSAPART；`entity_id=0` 自动分配；仅创建连接定义 |
| `ansa_connection_check` | 1–200 个 `SpotweldPoint_Type`；有限正 `distance`，默认 0.1 个模型长度单位；只启用绝对间距重复检查 |
| `ansa_connection_apply` | 一个显式点焊、两个各含单个 CQUAD4 的 Part、新备份和完整卡片条件；生成受控 RBAR 与八个 FE PERIMETER |

`ansa_connection_list` 的每行包含实体引用、类型、名称、坐标、直径、连接归属、`complete` 和 `errors`；缺失归属保留错误，不伪造空集合。`ansa_connection_create` 创建后回读身份、坐标、直径、名称和 Part 顺序，超时后按原 `operation_id` 查询。

`ansa_connection_check` 只支持 NASTRAN 点焊点，原生投影仍可能报告问题；任务成功不代表问题数为零。`ansa_connection_apply` 的范围限定为两个单四边形 Part、正厚度 PSHELL、已定义正密度 MAT1 和九个固定 RBAR 设置；不覆盖其他连接类型、复杂几何或其他 deck。

`ansa_element_change_type` 仅支持 NASTRAN 显式 1–100 个 CBAR 转为同 ID 的 CROD，并使用既有 PROD 和新 `.ansa` 备份。类型改变不代表物理等效；其他转换和 deck 未开放。

## 展示边界

几何、检查、连接和实体工具的当前验收集中在 ANSA 2026.1.0、Windows GUI、单活动目标单模型及已列明的 NASTRAN/ABAQUS 类型。其他版本、其他 deck、多模型写入、全模型检查和复杂关联场景仍需分别验证。
