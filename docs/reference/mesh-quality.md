# 四类原生网格质量模板

优先通过 ANSA 原生 `MeshQuality` 完成检查、问题定位和修复复查。MCP 负责选择模板、传递项目准则、控制范围、保存备份和汇总结果。模板按分析类型选择，deck 用于原生实体和卡片适配。

## 模板与准则来源

| `profile.template` | 主要依据 | 必检项目 |
| --- | --- | --- |
| `linear_shell` | Abaqus 线性分析建模与 Mesh Verify | 长宽比、Jacobian、不完整单元、四边形／三角形内角 |
| `linear_solid` | Abaqus 实体建模与 Mesh Verify | 长宽比、Jacobian、不完整单元、负体积、左手单元、四面体／楔形／六面体角度 |
| `explicit_crash_shell` | LS-DYNA 壳公式与时间步 | 长宽比、Jacobian、不完整单元、翘曲、歪斜、内角、原生特征长度与时间步 |
| `explicit_crash_solid` | LS-DYNA 实体与时间步 | 长宽比、Jacobian、不完整单元、负体积、左手单元、塌缩、原生特征长度与时间步 |

**模板规定必检项目，数值阈值来自调用方提供的 ANSA 项目准则。** 不内置未核实的统一工程合格线。先在 ANSA 配好项目的质量准则，或导出 `.ansa_qual` 后将其 UTF-8 全文放入 `quality_criteria`；省略该字段则使用当前会话。原生属性名与算法以当前版本导出为准。

依据：[Abaqus Mesh Verify](https://docs.software.vt.edu/abaqusv2025/English/SIMACAECAERefMap/simacae-c-mgnconcpartitionverify.htm)、[Abaqus 实体建议](https://docs.software.vt.edu/abaqusv2025/English/SIMACAEGSARefMap/simagsa-c-ctmsummary.htm)、[LS-DYNA 壳公式](https://lsdyna.ansys.com/shell-formulations/)、[LS-DYNA 时间步](https://lsdyna.ansys.com/time-step-size-2/)。Abaqus/CAE 默认筛选值、公式相关建议和用户单位的案例阈值是不同性质的依据，不可混作求解器强制标准。Abaqus Shape factor 不等同于 ANSA Tetra collapse。

ANSA 官方 [Checks Manager 资料](https://www.beta-cae.com/pdf/ansa_model_checks_for_ls-dyna.pdf)介绍了检查分组模板、问题定位和自动修复；[2025.1.1 发布说明](https://www.beta-cae.com/news/20251104_announcement_suite_2025.1.1.htm)记录修复过二阶实体的 Fix Quality 异常。这是该版本的历史修复记录，不表示该问题在 ANSA 2026.1 仍存在。

[官方 Crash & Safety 资料](https://www.beta-cae.com/brochure/ansa_meta_for_crash_safety_brochure.pdf)说明检查可以作用于用户选定实体，并在支持时提供自动修复。本插件据此采用显式范围工作流；是否允许修改某个模型仍由本版本实测的生命周期保护和每项工具契约决定。

必检项关闭、缺少有效阈值或原生问题无法映射时，返回 `complete=false`、`passed=false` 和缺失／未知信息；不会把全 OFF 的零失败结果计为通过。当前四模板不等价于完整 Abaqus Analysis checks 或 LS-DYNA 初始化检查，也不自动验证 Shape factor、相对几何偏离、厚度离散、小时玻璃、接触或求解后网格收敛。

## 输入与输出

四项工具：`ansa_mesh_quality_report`、`ansa_mesh_quality_page`、`ansa_mesh_quality_locate`、`ansa_mesh_quality_repair`。这些是 Quality Inspector 对应的检查、明细和受控修复工作流，不代表控制整个原生 Inspector 窗口。

**公开 MCP 已限定开放 LS-DYNA 质量定位与受控修复。** 要求 ANSA 2026.1.0 GUI、单窗口／单模型和可用的模型生命周期对象；同路径重开、New 或 deck 切换会使旧模型代次失效，出现多模型后继续关闭写入。LS-DYNA 写操作只接受 `explicit_crash_shell`／`explicit_crash_solid` 原生模板；修复仅覆盖 QUAD4／HEXA8，三角形、四面体及高阶单元明确拒绝。

模型快照的 `write_enabled` 仍表示通用写入门控，LS-DYNA 中保持 `false`；`write_tools` 列出当前单独允许的 `quality_locate`、`quality_repair`。后端受理与 ANSA 主线程领取采用相同规则。不得将“通用写入未开启”解释为质量操作必定不可用，也不得由质量许可推断脚本、实体创建或其他写操作已开放。NASTRAN／ABAQUS 沿用既有通用写入规则。

线性壳使用当前已配置准则的最小 profile：

```json
{"template": "linear_shell"}
```

显式实体还须明确给出模型时间单位下的目标时间步：

```json
{
  "template": "explicit_crash_solid",
  "target_time_step": 1e-7,
  "criteria_label": "项目准则版本1",
  "formulation": "ELFORM=1"
}
```

示例 `1e-7` 是输入示意，不能直接当作工程标准。`target_time_step` 将写入本次检查的原生 `crash time step` 阈值；特征长度必须在项目准则中启用且具有正下限。二者要求当前原生算法为 `LS-DYNA`。短边长可以另行启用，但不替代特征长度。

`quality_criteria` 可选，内容最多 65536 个 UTF-8 字节，是原生准则全文而非路径。`criteria_label` 最多 128 字符，`formulation` 最多 64 字符；公式字段仅记录调用方声明，回包 `formulation_verified=false`。线性模板禁止传 `target_time_step`，原生模板与旧 profile 字段不得混用。

只读报告执行范围内的原生 `MeshQuality`，返回失败单元、原生失败准则、未知项、节点、中心、包围盒、有效准则摘要与来源／计算定义。`rows` 增加实际拓扑类型和 `quality_details`：准则名、算法、原生量测键、实测值、阈值方向和上下界。当前确认映射为 ASPECT/WARP/SKEW/JACOB/MINL/MAXL/CRASH/COLLAPSE；其余准则仍由原生检查判定，数值明细明确标为不可用，不将缺值当零。Volume ID 不作为必填项。显式另外回读原生 `CRASH`，缺失、非有限或非正时间步记未知；时间步仍是前处理估算，`solver_verified=false`。

只读报告与定位使用独立自定义检查准则，不调用 `ReadQualityCriteria` 改全局设置。由于原生对象可能接受错误算法名而不报错，激活准则的算法必须与当前 ANSA 会话匹配，Jacobian 用户计算定义也必须一致；不一致时拒绝，不能凭文本回显认定算法可用。

`scope_sha256` 绑定实体卡片／坐标、profile、有效准则、全局准则、原生报告、时间步及材料／属性卡片摘要。后两项操作必须提交该值作为 `expected_scope_sha256`。模型、材料、准则或报告变化后需重新取得报告；它不替代 `expected_target` 与模型代次校验。

大报告保留全部失败／未知 ID 和总体判定，只限制单元明细；`rows_total/rows_returned/rows_truncated` 标记省略。`complete` 表示质量检查完整性，与明细省略不同。需要全部数值时缩小范围或在 ANSA 原生界面查看。

所有报告包含 `summary`：`status` 为 `passed`、`failed` 或 `incomplete`，并提供 `checked_count`、`failed_count`、`unknown_count`、`passed_count`。这些摘要计数按元素统计；失败与未知可以重叠，不能直接相加。`violation_summary` 按准则列出 `failed_count`；同一元素违反多个准则时会出现在多个准则组中。该汇总先按完整检查结果计算，再截断 `rows`。

## 定位与修复

定位重新验证范围摘要，只显示失败单元并回读可见集合。NASTRAN／ABAQUS 使用 `SHELL/SOLID`，LS-DYNA 使用实际的 `ELEMENT_SHELL/ELEMENT_SOLID`。需要 GUI 会话；无 GUI 时明确拒绝显示操作。

定位可选 `criterion`，按当前激活准则名精确匹配；原生准则名例如 `aspect ratio`，旧 profile 名例如 `ASPECT`。省略时显示所有失败；没有匹配项时不改变当前显示。定位前仍先验证 scope，scope 失效时拒绝操作。

修复必须给出新 `.ansa` `backup_path` 和正数 `maximum_displacement`。`movable_node_ids` 可显式限定允许节点；省略时自动排除范围外 FE 共用节点，以及 `NodesToElements(..., sources=["bcs"])` 发现的边界条件／载荷关联节点，查询不完整则拒绝。自动模式仍遵守原生已有的冻结和几何边界约束；不保证所有节点都可移动。

- 新模板省略 `method` 时使用 `fix_quality`：在原生报告头设置 `fix_method=2`；先保存当前完整全局准则，临时加载本次有效准则并回读确认，再执行 `try_fix(request_gui=False)`。结束或异常时恢复原始全局准则并回读，避免自定义检查与原生修复使用不同的准则。
- Fix Quality 另对完整 Mesh Parameters 做保存、临时应用、回读和恢复：启用相应壳／实体修复总开关，明确禁止剩余四边形拆分；保留所有已有冻结、影响区域、距离表达式和边界选项。`options_synchronized/options_restored` 记录确认状态。最大节点位移由独立坐标回读核算，不把原生距离表达式误作全局位移上限。回调和参数恢复错误分别保留，恢复失败返回 `recovery_file`。
- 显式指定 `smooth` 时调用原生 `SmoothShells` 或 `SolidSmooth(..., freeze_skin=True)`。壳平滑后先恢复范围内固定节点，再按同一原生准则复查。
- 原生修复产生的坐标作为候选结果；拓扑和节点身份核对后，恢复选中范围内的固定节点，再校验允许节点的位移、卡片及同准则质量结果。范围外节点的变化仍拒绝并回滚。
- 不自动切换方法。Fix Quality 无实际变化时返回 `applied=false, reason=no_improvement`；调用正常不等于网格已改善。
- 仍限定拓扑和节点身份不变、非坐标节点卡片／材料／属性不变、位移不越界、面积／体积与原生 Jacobian 为正、无新增失败单元或失败准则。失败数减少时认定改善；失败数相同时，只有所有当前违规指标都有同配置的完整可比数值、没有违规程度恶化且至少一项改善，才保留 `metric_progress`。不可比时继续拒绝，合格区内波动不当作违规程度回退。
- 错误时尝试精确恢复节点坐标。拓扑或材料／属性发生异常变化、回读不明或无法恢复时标记 `recovery_required=true` 并保留备份，不自动打开文件覆盖当前状态。

`api_invoked` 记录修复 API 是否实际调用；`criteria_synchronized` 和 `criteria_restored` 分别记录临时准则和原始准则的回读结果。`ReadQualityCriteria` 返回成功不能单独证明准则生效；应用或恢复未通过回读时不得报告修复成功。平滑路径不临时加载全局准则。

`max_iterations` 缺省为1，显式设为2–10时允许一次任务内有界续修；每轮重新检查，后续备份名追加 `-iteration-N`。`maximum_total_displacement` 缺省等于 `maximum_displacement`，始终以该次任务开始坐标核算累计位移，而非每轮重新放开限额。全部通过、无改善、约束阻止、异常或轮数耗尽时停止；不自动切换算法、扩大范围或放宽阈值。后续轮次回滚且确认精确恢复时保留此前已验证改善；状态未知时标记恢复要求。`iterations/backups/termination_reason` 提供每轮过程。新任务之间仍需新的报告、操作 ID 与备份，跨任务累计位移由调用方按项目基线控制。

修复结果提供 `quality_passed`；`repair_outcome` 仅在成功路径返回，取值为 `already_passed`、`fully_repaired`、`partially_repaired` 或 `unchanged`。`ok=true` 表示操作路径成功，不代表质量已全部通过。

未启用算法在原生文件重读后发生变化时，通过 F11 setter/getter 保持其原计算方式，再比较完整准则；不忽略算法、行内设置或实际阈值差异。恢复失败时返回 `criteria_backup_path` 保留原始准则；原操作与恢复同时失败时分别保留 `repair_error`／`criteria_apply_error` 和 `criteria_restore_error`。`improved=true` 表示已验证的数量或指标改善，是否全部合格仍须检查 `after.passed` 及剩余失败 ID。

原生 `Reshape/Reconstruct` 没有在本轮开放。其可能涉及拓扑、连接及载荷关联，需要单独验证契约。四模板的 `scope_verified` 指全部 FE 卡片／节点、材料／属性和质量准则的已述检查；旧配置保留原有 FE／准则检查。非 FE 几何、组织及物理结果不由此保证，`possible_effects` 保留该边界。

本机 ANSA 2026.1 `MeshQuality` 的 `fix_method` 映射为 `0` 重构、`1` `reshape`、`2` Fix Quality。方法 `0` 和 `1` 可能改变拓扑，本轮不开放。

## 兼容配置与范围

原有 `analysis_type + element_family + criteria` profile 保留：逐单元原生量测后比较显式上下界；旧显式配置仍要求 `minimum_length`，`wave_speed × target_time_step` 仅作规划估算。旧配置省略修复方法仍为 `smooth`。新工作优先使用上述四模板，避免把短边估算作为碰撞主判据。

报告不设固定单元总数上限。`entity_ids` 与 `scope="all"/"visible"` 二选一；超过2000个显式单元、使用 scope 或 `paged=true` 时分批检查并落入当前私有会话的 SQLite 证据库，返回 `report_id` 和完整覆盖摘要。默认 `batch_size=1000`（可选1–2000），原生报告树过大时自动缩小只读批次；`max_seconds=120`、`max_report_megabytes=256` 是可调整资源预算。原生调用本身无法强制中断，预算与暂停在批次之间检查。预算不足、批次失败或检查期间模型变化时 `complete=false`，明确列出 requested/checked 数量，不能把部分结果当全模型通过。

`ansa_mesh_quality_page` 按 report_id、offset、limit（1–200，默认100）、status（all/failed/unknown，默认failed）和可选 criterion 查询冻结的报告明细；每页另受128 KiB预算约束，按 `next_offset` 继续。完整ID和明细在证据库中，不把巨大ID数组塞进MCP回包。报告只属于原会话和模型代次；历史快照不是当前重新检查。分批报告的 `usable_for_mutation=false`，修复前须对目标小范围取得新的直接报告。

例如检查当前模型全部壳单元：

```json
{"deck": 4, "profile": {"template": "linear_shell"}, "scope": "all"}
```

以上省略每项工具仍须提供的 operation_id、expected_target 和 expected_model，数值准则仍须在ANSA中正确启用。大型显式ID请求仍受通用TCP帧预算约束，宜使用 scope。定位仍限定2000个显式单元；修复单批最多200个、显式允许节点最多2000个。全模型FE／节点超过50000时切换为磁盘快照，保留全范围比对和精确恢复；材料／属性改为流式摘要，不再按5000个实体拒绝。磁盘、内存和执行时间仍是实际边界，工业模型及复杂材料需要专项验证。

验证分别记录离线回归、模拟 MCP、原生主线程、保存重开、GUI 画布与求解。当前证据见[验证范围](../acceptance.md)；不能以原生接口或单元测试通过代替全部层次。
