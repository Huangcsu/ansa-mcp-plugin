# MCP 工具参考

当前注册 65 个 MCP 工具。工具的实际输入与输出 schema 由服务返回；下表提供能力入口，具体范围见对应参考。

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
| 网格（7） | `ansa_mesh_faces`、`ansa_mesh_surface`、`ansa_mesh_delete`、`ansa_mesh_settings`、`ansa_mesh_volume`、`ansa_mesh_hex_map`、`ansa_mesh_workflow` |
| 机构（4） | `ansa_kinematics_body_create`、`ansa_kinematics_joint_create`、`ansa_kinematics_config_create`、`ansa_kinematics_inspect` |
| 接触（1） | `ansa_contact_inspect` |
| 几何（3） | `ansa_geometry_select`、`ansa_geometry_preview`、`ansa_geometry_delete` |
| 检查（6） | `ansa_check_run`、`ansa_mesh_check`、`ansa_mesh_failed_elements`、`ansa_mesh_intersections`、`ansa_mesh_penetrations`、`ansa_rigid_dependencies` |
| 脚本与 API（3） | `ansa_script_validate`、`ansa_script_execute`、`ansa_api_reference` |

`ansa_model_inspect` 的 `action` 合并几何统计、FACE 属性、实体度量、ABAQUS 质量/表面成员、几何/网格检查及显示操作。不同 action 的支持 deck 与参数独立验证。

## 使用契约

- 模型任务先取得当前 `expected_target` 与 `expected_model`，保留原 `operation_id` 和返回的 `task_id`。
- 删改提交最近回读的预期字段、坐标或范围；需要备份的操作提供新的 `.ansa` 备份路径。
- `queued` 只表示任务已登记，需查询终态。超时和断线不代表取消；不确定时查询原任务并独立回读。
- 参数中的长度、面积、体积和质量遵循模型单位；接口不会替用户转换单位体系。
- 工具已注册不表示所有实体类型、deck、复杂几何或求解行为均已验收。

## 专项参考

| 文档 | 范围 |
| --- | --- |
| [ABAQUS](reference/abaqus.md) | QUAD、HEXA8、网格后续操作、机构、接触与质量 |
| [模型与操作](reference/operations.md) | 模型文件、实体、组织、几何、检查和连接 |
| [任务、脚本及 API](reference/tasks-api.md) | 状态、幂等、取消、恢复、日志、记忆和脚本 |
| [验证范围](acceptance.md) | 当前环境、既有证据和未验证范围 |

旧 79 项是迁移功能口径，65 项是当前注册接口口径，两者不能直接比较。历史入口对应保留在[迁移映射](reference/legacy-mapping.md)和机器可读 [tool-mapping.json](tool-mapping.json)，不作为当前完整 schema。
