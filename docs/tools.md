# MCP 工具参考

当前工作源码注册 93 个 MCP 工具。结构化网格、几何特征、受控 CONS 辅助曲线转换、原始 CONS 分段设置、有限六面 CAD Map、孔环源面准备及 MapBlock caps 设置的验收范围见[验收记录](acceptance.md)，尚未完成发行集成。工具的实际输入与输出 schema 由服务返回；下表提供能力入口，具体范围见对应参考。

## 当前入口

| 分组 | 工具 |
| --- | --- |
| 连接与面板（3） | `ansa_connection_status`、`ansa_model_snapshot`、`ansa_panel_show` |
| 任务（3） | `ansa_task_query`、`ansa_task_cancel`、`ansa_task_list` |
| 日志（1） | `ansa_log_read` |
| 本机记忆（2） | `ansa_memory_read`、`ansa_memory_write` |
| NASTRAN GRID（4） | `ansa_grid_create`、`ansa_grid_read`、`ansa_grid_update`、`ansa_grid_delete` |
| 通用实体与节点（9） | `ansa_entity_count`、`ansa_entity_list`、`ansa_entity_get`、`ansa_entity_create`、`ansa_entity_update`、`ansa_entity_delete`、`ansa_entity_find`、`ansa_entity_bounds`、`ansa_node_coordinates` |
| 模型（10） | `ansa_model_save`、`ansa_model_summary`、`ansa_model_includes`、`ansa_model_new`、`ansa_model_open`、`ansa_model_inspect`、`ansa_model_export`、`ansa_model_import`、`ansa_model_autosave`、`ansa_model_save_current` |
| 文件集（1） | `ansa_benchmark_run` |
| 连接（4） | `ansa_connection_list`、`ansa_connection_create`、`ansa_connection_check`、`ansa_connection_apply` |
| 元素类型（1） | `ansa_element_change_type` |
| Part/SET（3） | `ansa_part_create`、`ansa_set_members`、`ansa_set_add` |
| 网格（9） | `ansa_mesh_scope_read`、`ansa_mesh_freeze`、`ansa_mesh_faces`、`ansa_mesh_surface`、`ansa_mesh_delete`、`ansa_mesh_settings`、`ansa_mesh_volume`、`ansa_mesh_hex_map`、`ansa_mesh_workflow` |
| 结构化网格（22） | `ansa_mesh_interface_audit`、`ansa_mesh_block_plan`、`ansa_mesh_source_audit`、`ansa_geometry_partition_plan`、`ansa_geometry_partition_apply`、`ansa_mesh_sweep`、`ansa_mesh_provenance`、`ansa_mesh_evidence_page`、`ansa_mesh_seed_plan`、`ansa_mesh_sweep_prepare`、`ansa_geometry_features`、`ansa_geometry_boundary_prepare`、`ansa_mesh_seed_read`、`ansa_mesh_seed_apply`、`ansa_mesh_cad_map`、`ansa_geometry_sweep_candidates`、`ansa_mesh_map_block_inspect`、`ansa_mesh_map_block_prepare`、`ansa_mesh_map_blocks_prepare`、`ansa_mesh_map_block_fill`、`ansa_mesh_map_block_caps`、`ansa_mesh_annular_source_prepare` |
| 机构（4） | `ansa_kinematics_body_create`、`ansa_kinematics_joint_create`、`ansa_kinematics_config_create`、`ansa_kinematics_inspect` |
| 接触（1） | `ansa_contact_inspect` |
| 几何（3） | `ansa_geometry_select`、`ansa_geometry_preview`、`ansa_geometry_delete` |
| 检查（6） | `ansa_check_run`、`ansa_mesh_check`、`ansa_mesh_failed_elements`、`ansa_mesh_intersections`（SHELL 或 CHEXA8/CPENTA6 SOLID）、`ansa_mesh_penetrations`、`ansa_rigid_dependencies` |
| 分析类型网格质量（4） | `ansa_mesh_quality_report`、`ansa_mesh_quality_page`、`ansa_mesh_quality_locate`、`ansa_mesh_quality_repair` |
| 脚本与 API（3） | `ansa_script_validate`、`ansa_script_execute`、`ansa_api_reference` |

`ansa_model_inspect` 的 `action` 合并几何统计、FACE 属性、实体度量、ABAQUS 质量/表面成员、几何/网格检查及显示操作。不同 action 的支持 deck 与参数独立验证。

`ansa_mesh_scope_read`与`ansa_mesh_freeze`提供[关联范围回读及独立冻结/解冻](reference/mesh-scope.md)：区分FE冻结与FACE/Macro及共享CONS锁定，使用新鲜指纹、备份和独立状态回读。

`ansa_mesh_seed_apply`的可选`edit`提供[已有边界重新分段、定长和分布对齐](reference/perimeter-edit.md)，保留旧首次分段模式；已有面网格须先显式处理。

`ansa_mesh_surface(method="remesh_existing")`提供[已有面网格重划](reference/surface-remesh.md)：完整单FACE Macro范围、显式FREE或mapped及单元类型，保护周界、邻面FE和工程引用，并恢复全局配置。

## 使用契约

- 模型任务先取得当前 `expected_target` 与 `expected_model`，保留原 `operation_id` 和返回的 `task_id`。
- 删改提交最近回读的预期字段、坐标或范围；需要备份的操作提供新的 `.ansa` 备份路径。
- `queued` 只表示任务已登记，需查询终态。超时和断线不代表取消；不确定时查询原任务并独立回读。
- 参数中的长度、面积、体积和质量遵循模型单位；接口不会替用户转换单位体系。
- 工具已注册不表示所有实体类型、deck、复杂几何或求解行为均已验收。

`ansa_mesh_map_block_caps(action="plan"/"apply")` 只编辑一个显式 `volume_id` 的 master/slave FACE 集合，允许每个 cap 包含多个 FACE。限已保存 NASTRAN 毫米有限模型：当前所有 VOLUME 均须为 MapBlock，且模型内无 SOLID；目标须为 Ready to Mesh、Unmeshed、未冻结。预算为最多 20 个 MapBlock、100 个边界 FACE、5000 个 SHELL、10000 个 GRID/模型实体和 8 MiB 快照；写入前备份，回读要求 target caps 与输入一致、round 等于原边界剩余 FACE，且 CAD、FE/节点、冻结状态、其他 MapBlock caps 和网格配置不变。原生 `DefineCaps` 返回 `None` 时仍以独立回读判定结果；本工具不生成或重划网格。

`ansa_mesh_map_block_fill` 保留原有 `volume_id` 单块和 `volume_ids` 矩形块组契约。只有显式传入 `annular_options` 才选择孔环路径；它必须与恰好四个 `volume_ids` 一起使用，不能与 `volume_id` 同用。选项键为 `source_face_ids`（4 个 FACE ID）、`center`（3 个坐标）、`axis`（3 个分量的单位向量）、`inner_radius`、`outer_radius`、`distance` 和 `max_chord_error`，由后端严格校验。

该路径限完整、已保存的 NASTRAN mm 单 Part 局部孔环，且块必须为恰好 4 个 Ready/Unmeshed MapBlock；范围上限为 64 FACE、2000 个边界 SHELL，源端和对应端各最多 400 个 SHELL。计划绑定全局网格设置，并在应用后回读确认；此路径不设置 target length。应用只调用一次原生 `VolumesRemesh`，可生成 CHEXA/CPENTA；保护原边界网格与 CAD，并审计完整外皮、共享 FACE 和正体积实体回读。几何匹配容差及返回语义见[孔环填充契约](reference/structured-meshing.md#已有孔环-mapblock-直接填充)。此有限范围不承诺源层对应、连续 CAD、质量验收或 GUI 验收。

孔环填充参数片段示例（其余公共任务参数仍按工具 schema 提供）：

```json
{
  "action": "plan",
  "deck": 1,
  "volume_ids": [101, 102, 103, 104],
  "annular_options": {
    "source_face_ids": [201, 202, 203, 204],
    "center": [0.0, 0.0, 0.0],
    "axis": [0.0, 0.0, 1.0],
    "inner_radius": 10.0,
    "outer_radius": 20.0,
    "distance": 5.0,
    "max_chord_error": 0.02
  }
}
```

`ansa_mesh_surface` 的 `method="mapped_existing_seeds"` 复用原生四边映射生成器，保留已有 CONS 整数种子；用独立 `seed_scope` 表达邻接保护范围，`faces` 表达实际源面。默认长度模式保留原契约，详细限制见[结构化网格说明](reference/structured-meshing.md#保留整数-cons-种子的四边映射源面)。

`ansa_geometry_features` 可选 `sweep_options` 诊断显式六面域的源面、目标面及四侧壁对应。拓扑通过仍保持连续 CAD 几何未验证和整体 `incomplete`，不授权实体生成；输入、证据分页和适用范围见[有限六面直扫对应诊断](reference/structured-meshing.md#有限六面直扫对应诊断)。

## 专项参考

| 文档 | 范围 |
| --- | --- |
| [ABAQUS](reference/abaqus.md) | QUAD、HEXA8、网格后续操作、机构、接触与质量 |
| [模型与操作](reference/operations.md) | 模型文件、实体、组织、几何、检查和连接 |
| [网格质量](reference/mesh-quality.md) | 四类原生模板、ANSA 项目准则、报告／定位／受控修复与旧 profile 兼容 |
| [结构化网格](reference/structured-meshing.md) | NASTRAN 显式块规划、CAD 特征读取、整数分段、合批预检、接口审计、受控分割与直线扫掠、来源阶段记录和证据分页 |
| [复杂实体网格复用流程](reference/complex-solid-meshing.md) | 将分块、种子、共形源面和安全合批串联为可复用操作流程 |
| [任务、脚本及 API](reference/tasks-api.md) | 状态、幂等、取消、恢复、日志、记忆和脚本 |
| [验证范围](acceptance.md) | 当前环境、既有证据和未验证范围 |

旧 79 项是迁移功能口径，当前 91 项是工作源码注册接口口径；数字不同且不代表一一对应。历史入口对应保留在[迁移映射](reference/legacy-mapping.md)和机器可读 [tool-mapping.json](tool-mapping.json)，不作为当前完整 schema。
