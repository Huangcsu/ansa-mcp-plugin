# 已有面网格重划

`ansa_mesh_surface(method="remesh_existing")`在明确的完整Macro范围内替换已有SHELL，复用原生网格器与现有任务、备份和配置恢复机制。仍为93项公开工具；原来的`length`与`mapped_existing_seeds`参数及门禁保持。

## 参数与读取顺序

1. 用`ansa_mesh_faces`读取目标FACE的完整`condition`，包含全部`expected_fields`、`expected_macro_id`与非空`expected_shell_ids`。
2. 用`ansa_mesh_scope_read(entity_type="FACE", entity_ids=[目标FACE])`取得`scope_sha256`。该选择必须与重划的全部目标FACE一致。
3. 用`ansa_mesh_seed_read`读取周界：`face_ids`取上一步`faces`中的全部FACE ID（含一跳邻域），`cons_ids`取`freeze_effects.cons_ids`。取得`seed_snapshot_sha256`，不要只读一个共享CONS或排除相邻FACE。
4. 使用新的`operation_id`提交下列参数，并保留原回执及`task_id`。`expected_target/expected_model`仍按公共绑定契约提供。

```python
{
    "deck": 1,
    "faces": conditions,  # 直接使用完整的最近回读条件
    "method": "remesh_existing",
    "remesh": {
        "algorithm": "FREE",
        "element_type": "mixed",
        "element_order": "1st Order",
        "boundary_policy": "preserve",
        "target_length": 2.0,
        "expected_scope_sha256": scope["scope_sha256"],
        "expected_seed_snapshot_sha256": seeds["seed_snapshot_sha256"],
        "backup_path": "F:/model-backups/before-remesh-unique.ansa"
    }
}
```

新模式不接受外层`target_length`、`seed_scope`或非默认外层`element_type`；实际类型在`remesh.element_type`明确选择。`remesh`各字段均必填，不自动推导算法、阶次或边界策略。

| 参数 | 当前支持 |
| --- | --- |
| `algorithm="FREE"` | 一次原生`mesh.RemeshShells(shells, "FREE")`；显式`quad/mixed/tria`分别映射当前原生`Quad/Mixed/Tria`设置 |
| `algorithm="mapped"` | 仅`quad`；一次`Create4SidedMesh`，要求四条CONS简单闭环及相对边分段一致，关闭重新间距和自动三角拆分，保护选择边界 |
| `element_order` | 仅`1st Order` |
| `boundary_policy` | 仅`preserve`；保持全部周界GRID身份、卡片和坐标，不自动改边界分段 |
| `target_length` | 有限正数，单位mm；用于临时网格设置和生成预算，不保证每条实际边等长，也不替代既有周界分段 |

`Mixed`允许两种单元，不保证每次一定同时出现三角形和四边形。`mapped`的`meshed_ents`表示成功FACE，工具另行读取完整Macro的SHELL清单，不将其当作新SHELL返回。

## 首版范围与保护

- 已保存、激活NASTRAN、毫米模型；1～4个完整单FACE Macro，每个已有非空一阶CQUAD4/CTRIA3网格。首版仅接受平面网格、开放直线且LINEAR的一阶CONS。
- 允许与选区外已网格Macro共享CONS；目标Macro、旧SHELL及其周界不得冻结，邻接Macro导致的有效保护也必须明确为否。FE PERIMETER和VOLUME相关目标暂不支持。
- 首版在备份和卡片扫描前拒绝含原生连接或INCLUDE的模型。连接FE representation成员及Include替换归属尚未适配，不能仅凭普通卡片引用推断安全；后续需独立扩展相应成员保护。引用扫描沿用原生收集句柄，不走已知可能使混合连接模型退出的`GetEntity → card_fields`路径。
- 仅替换目标旧SHELL和其独占、无外部工程引用的内部GRID。旧单元须与FACE具有相同Part/PID且无非默认方向、偏置、局部厚度、冻结、名称或注释；可替换GRID的CP/CD/PS/SEID须完整可读且为空或零，也不得有非默认元数据。
- 扫描真实Entity引用、节点附着及SET的SHELL/GRID成员；外部载荷、约束、其他单元或集合引用候选实体时拒绝，不自动迁移工程定义。全部周界GRID、非目标FE、CAD及其他定义独立保护。
- 最多2000个目标SHELL及2000个生成预算预留；整个保护快照最多10000实体、5000条CONS和8 MiB。极小长度在原生生成前拒绝。预算估计不构成原生输出数量上界，输出仍独立核验；配置及证据沿用现有独立预算。
- 备份验证后才允许原生CONS节点实例化；不先删除旧网格，不对异常自动换算法或重试。临时Mesh Parameters、Quality Criteria/F11及设置由`meshing_context`完整恢复。

## 结果与失败处理

成功要求`ok/scope_verified/boundary_preserved/relations_verified`为真且`context_result.restored=true`。结果含完整`result_shell_ids`、`element_counts`、各FACE拓扑与节点投影、后续范围指纹及实测阶段耗时。

原生可复用旧编号，因此`added_shell_ids`不是全部结果。工具分别返回新增、移除和复用的SHELL/内部GRID清单；通过完整Macro归属、实际连接及卡片审计确定结果。平面FE审计检查方向、凸四边形角点Jacobian符号、边流形性、完整周界、共享边连通、边界环和Euler关系；mapped另验证完整张量格点。

这些检查不代替项目质量准则或任意CAD连续覆盖证明，结果明确保留`continuous_surface_coverage_proven=false`。工程质量判断继续使用既有质量报告工具与显式准则。大结果使用现有证据分页。

失败时检查`api_called/possible_effects/recovery_required`、备份、配置恢复及已观察结果。超时或未知结果先查询原`operation_id/task_id`，再独立回读；不换键重放，也不自动打开备份覆盖模型。

可复用验收入口为`tests/integration/surface_remesh_ansa.py --run-dir <独立运行目录>`；该入口不启动ANSA，拒绝已有报告的整链重跑，只有核验归属且全部任务终态、保存重开通过后才关闭实例。实际验收范围见[验收记录](../acceptance.md)。
