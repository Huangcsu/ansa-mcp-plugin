# ANSA 原生 Feature 识别

工具提交计划和应用任务。调用提供 `operation_id`、`expected_target`、`expected_model`、`deck`、`part_ids`。Part 范围为 1 至 100 个唯一的显式 ANSAPART ID。`ttl_seconds` 默认 300。公共身份和任务状态见[任务与 API](tasks-api.md)。

## 受控特征识别

`ansa_model_features_recognize` 复用公共上下文、deck、Part 和 TTL 参数，默认 `action="plan"`，只读取计划；`action="apply"` 实际调用一次原生识别。

| 参数 | 要求 |
| --- | --- |
| `feature_types` | 从下表显式选择同一建模模式内的唯一类别 |
| `modelling` | `Shell` 或 `Solid`。仅选择 FILLET、CHAMFER 或 HOLE 2D 时允许省略，沿用当前 ANSA 设置。其他类别要求显式提供 |
| `input_kind` | 默认 `cad`。仅 `modelling="Shell"` 且单选 `RIB 2D` 时允许 `shell_mesh`，计划和应用使用相同输入类型 |
| `parameters` | 所选每类的完整参数组，见下表；不提供未选类别的有效参数 |
| `action` | `plan` 或 `apply`，默认 `plan` |
| `expected_plan_sha256` | apply 必须提供本请求最近计划返回的 64 位小写 SHA-256；plan 不接受 |
| `backup_path` | apply 必须提供新的绝对 `.ansa` 路径，不得覆盖现有文件或当前数据库；plan 不接受 |

| 类别 | 模式 | 必须明确提供的参数 |
| --- | --- | --- |
| `FILLET` | 两者 | `fillet_max_radius`、`fillet_min_angle` |
| `CHAMFER` | 两者 | `chamfer_max_width`、`chamfer_min_angle`、`chamfer_max_angle` |
| `HOLE 2D` | Shell | `hole_max_diameter`，首版范围为标准圆孔 |
| `HOLE 3D` | 两者 | 无附加参数 |
| `SPECIAL PERIMETER` | 两者 | `special_fe_break_angle`、`special_geom_break_angle`、`special_sharp` |
| `STAMP` | 两者 | `stamp_max_height`、`stamp_max_size` |
| `FLANGE 2D` | Shell | `flange_2d_min_width`、`flange_2d_max_width` |
| `FLANGE 3D` | Solid | 无附加参数 |
| `RIB 2D` | Shell | 无附加参数 |
| `LOGO` | 两者 | `logo_max_height`、`logo_max_size`、`logo_allow_base_fillets`、`logo_base_fillet_min_angle` |
| `FASTENER` | Solid | `fastener_embedded_bolts`、`fastener_embedded_clips`。计划检查 ANSA DM 根及可查询的 Fastener `common` 表示 |

没有附加参数的类别可传 `parameters={}`。角度用度且小于 180，长度采用模型单位。最小和最大值有严格顺序，二维翻边的最小宽度可为 0。数值须有限，布尔开关须显式填写。按实际调用读取生效设置，保持严格垫圈模式，暂关闭孔连接、翻边邻近和其他图中未开放的识别变体。特殊周界首版采用 Shell／Solid 的 Sharp 开关，以及几何和 FE 转折角。始终使用 `lock_fe_perimeters=True`。

### 前置范围与计划

使用显式 NASTRAN Part。默认 `cad` 要求所选 Part 含 CAD FACE 且没有 FE 单元。`shell_mesh` 要求所选 Part 没有 CAD FACE、至少有一个 SHELL 且所有单元均为 SHELL。组外既有模型实体始终纳入保护。对于依赖正确法向的三维孔、三维翻边和凹凸判定，输入应使用已正确定向的几何。识别操作验证现有 Feature 的卡片、归属和原生成员。ANSA 2026.1.0 的 Python 识别会清除目标 Part 中未启用的类别，因此计划、写入以及备份后的前置条件检查会拒绝遗漏已有类别的请求。

[Ribs 2D 与 Fasteners 真机补验](../acceptance.md#2026-11-18-ribs-2d-与-fasteners-真机补验)记录了不同输入的原生行为。独立紧固件可以在提供 DM `common` 条目的情况下得到原生 `FASTENER FEATURE`，该夹具的公开 apply 在原生识别后的面域回读失败，模型可能已有变化。Fasteners 的公开 MCP 修复和验收暂缓。

计划指纹绑定请求、当前目标授权/模型、原有实体和 Feature 内容、相关全局设置。apply 重新计算并核对，备份后再次核对；模型、设置或请求改变即要求重新计划。模型必须已有可校验的数据库路径。范围快照复用既有完整捕获，受 10000 模型实体、配置的 scope 字节预算及 24 MiB 复合识别证据预算限制。

### 执行与回读

识别前创建并核验新模型备份，再保存 Mesh Parameters 文件。临时设置应用后独立回读有效值；结束或异常时分别尝试 `ReadMeshParams` 和原始 BCSettings 恢复并回读。文件恢复失败时不能因部分字段一致而报告恢复成功。

原生 `recognize()` 返回 `None`；结果以独立回读为准。允许所选 Part 的 `Features Status` 改变以及新增所选类型 Feature。`shell_mesh` 另允许 ANSA 新建仅与所选 Part 的壳单元相邻的 FE Perimeter，`created_fe_perimeters` 回读其实体、相邻 SHELL 和 Part ID。识别前后的既有 FE Perimeter 卡片及相邻 SHELL 均参与保护。原有 Feature 的类型/ID、卡片、Part 归属和 contents 必须保持，其他模型实体仍受保护。启用的类别可以返回零匹配。

Feature→contents 使用 `CollectEntities(feature, "__ALL_ENTITIES__")`，返回实际原生引用及 `contents_read_method`。已有类别的保护对比包含整个记录。新 Feature 的 FACE 和 SHELL 等实体按原生 Part 验证，CONS 和 Hot Point 通过相邻 FACE 核对归属。壳网格上的 FE Perimeter 成员通过 `mesh.GetFEPerimeterShells` 和相邻 SHELL 的 Part 归属核对。空成员、读取失败、成员越界或已有 Feature 改变时传回失败及可知效果。

成功回执包含 `plan_sha256`、`parameters_sha256`、`backup`、`settings_used/settings_after`、`settings_restored`、`scope_verified` 和按类型计数。 `created_features` 包含新建 Feature 的类型、ID、Part、卡片及成员。 `summary.observed_matches_by_type` 表示选定 Part 当前读到的各请求类别数量，可以为 0。大结果用 `ansa_mesh_evidence_page` 查询完整回执。

失败保留已知阶段及前后回读，`possible_effects/recovery_required` 表达可能的模型变化；配置恢复不是模型回滚。不自动 clear、重试识别或打开备份。超时或结果未知先查询原 operation_id/task_id。
