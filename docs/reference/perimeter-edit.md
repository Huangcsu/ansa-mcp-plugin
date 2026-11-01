# 已有边界重新离散

`ansa_mesh_seed_apply`增加可选的`edit`模式，支持已有CAD周界内点的重新分段、原生定长和分布对齐。公开工具仍为93项。`assignments`与`edit`必须且只能提供一个；旧`assignments=[{cons_id,segments}]`保持首次分段契约与原有保护。

## 读取和调用

1. 明确目标CONS及相邻完整Macro的FACE范围；对齐另指定一条参考CONS。
2. 对目标与参考CONS的并集调用`ansa_mesh_seed_read(face_ids,cons_ids)`，取得`seed_snapshot_sha256`；读取不会实例化周界GRID。
3. 对同一CONS并集调用`ansa_mesh_scope_read(entity_type="CONS",entity_ids=...)`，核对Macro、冻结、FE和Volume关系，取得`scope_sha256`。
4. 调用`ansa_mesh_seed_apply`，共同参数为`operation_id/expected_target/expected_model/deck/face_ids/expected_seed_snapshot_sha256/backup_path`，另传下列一种`edit`。其中`expected_scope_sha256`放在`edit`内；使用新备份路径。

| `edit.action` | 其他字段 | 语义 |
| --- | --- | --- |
| `number` | `expected_scope_sha256`、`count_type="edges"/"nodes"`、`assignments=[{cons_id,count}]` | 每条CONS独立指定边数或包含两个端点的总节点数；不接受`+2/*2`等表达式，不自动按边链分配 |
| `length` | `expected_scope_sha256`、`cons_ids`、正`target_length` | 调用原生定长并回读`Elem.Length`、实际分段数及节点；长度是目标值，端点和曲率约束下实际段长由原生离散决定 |
| `align` | `expected_scope_sha256`、`source_cons_id`、`source_start_hot_point_id`、`targets=[{cons_id,start_hot_point_id}]` | 将参考边的节点数量和归一化位置对齐到目标边；端点方向必须显式声明，参考边保持不变 |

`count`为严格整数1—10000，`nodes`至少为2。首版支持当前激活的毫米制NASTRAN、已保存模型、1—20个FACE、合计最多32条目标/参考CONS（对齐最多31条目标）。仅支持有两个原生HOT POINT端点的一阶开放CONS；闭环、FE Perimeter和Hexa Box Edge尚未开放。`number/length`要求目标为`LINEAR`分布；对齐首版要求平行、同向的端点对和直线节点分布，不宣称曲线链、任意偏置生成或自动方向匹配。

## 已有网格和保护范围

原生`NumberPerimeters`即使显式关闭自动重划，也可能删除受影响Macro的面网格。新模式因此在备份和写入前拒绝已有SHELL的完整受影响Macro，并返回`blocked_macro_shell_ids/affected_macro_ids`。调用方先确认完整返工范围，再通过现有显式删除工具处理失效网格，重新读取两个指纹后编辑；工具不会自动删网格或重划面网格。已有实体网格的相关VOLUME也拒绝。

目标与参考CONS的所有邻接FACE及完整Macro必须在`face_ids`内。原始CONS冻结、相邻Macro有效冻结或冻结状态未知均拒绝。解冻须另行调用`ansa_mesh_freeze`，不能通过编辑模式绕过。

核验新备份后才允许`CONS → GRID`物化。只允许目标CONS独占、非端点且无原生元素/边界条件/SET引用的内部GRID被原生更新、增加或删除。内点的CP/CD/PS/SEID须完整可读且为空或零；永久约束、冻结标志和非默认名称、注释或成员信息均不能作为可丢弃虚拟点处理。端点、参考边、其他周界、其他GRID及FE继续保护；共享内部点、引用不完整或无法判断时拒绝。全模型卡片、Part归属、CAD导出摘要、周界身份以及Macro/CONS/Volume关系分别回读。

沿用原有分段保护预算：全模型最多10000实体、5000 CONS、10000周界点预算和8 MiB快照；物化前再次检查全模型节点预算。定长预估用于预留空间，不作为原生实际数量上界；原生操作后先读实际计数并检查预算，再收集结果点，超限保留恢复回执。

## 原生调用与结果

- 数量统一以`NumberPerimeters(...,apply_number_of="edges",apply_multiple_number=False,remesh_macros=False,use_ansa_defaults_values=False)`执行。公开总节点数转换为`count-1`条边，避免2026.1原生`nodes`实际按内部点计数的歧义。
- 定长显式传目标列表，`ApplyNewLengthToMacros(...,use_ansa_defaults_values=False)`；不使用隐式`all/visible`。
- 对齐第一参数包含目标与参考边，`align_using`仅含参考边；显式选择`Source CONS`、关闭cuts、参考边自动冻结和自动重划。返回0是定长/对齐的正常约定，不能单凭返回值证明成功。

成功要求实际点数、端点身份、CAD投影、既有完整范围审计、关系及完整Mesh Parameters/Quality Criteria/F11配置保持均通过。对齐另核对指定方向的全部归一化节点位置。结果含`scope_verified`、`relations_verified`、`configuration_unchanged`、两个结果指纹、`boundary_point_readback`和原生调用记录；耗时为实测阶段值。大结果复用既有证据分页。

原生异常或部分执行后仍尝试独立回读，并保留`possible_effects/recovery_required`、实际观察和配置恢复文件。配置意外改变时不报告成功。不会自动回滚、重开模型或换键重放；先查询原`operation_id/task_id`，再检查备份和实际状态。保存重开或上下文改变后，必须重新读取两个指纹。

离线回归入口为`tests/unit/test_perimeter_edit.py`和`test_perimeter_edit_schema.py`，独立真机入口为`tests/integration/perimeter_edit_ansa.py`。实际验收范围见[验收记录](../acceptance.md)。
