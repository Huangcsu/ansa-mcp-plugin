# 关联范围回读与独立冻结

`ansa_mesh_scope_read`读取显式选择的原生关系与冻结状态，`ansa_mesh_freeze`根据新鲜指纹独立冻结或解冻。工程上的保护范围由调用方决定；两项工具不生成或重划网格。均复用目标/模型绑定、任务Journal和主线程执行。

## 参数与适用范围

首版适配当前激活的NASTRAN deck，长度单位不参与冻结策略。两项工具均要求`operation_id/expected_target/expected_model/deck/entity_type/entity_ids`，可传`ttl_seconds`。

| 参数 | 回读 | 冻结／解冻 |
| --- | --- | --- |
| `entity_type` | `FACE/CONS/SHELL/SOLID/VOLUME` | `FACE/SHELL/SOLID` |
| `entity_ids` | 1—200个该类型的唯一正整数 | 与对应回读相同，顺序不影响选择 |
| `action` | 无 | `freeze/unfreeze` |
| `expected_scope_sha256` | 无 | 对应回读返回的64位小写SHA256 |
| `backup_path` | 无 | 新的绝对`.ansa`路径；父目录存在，且与当前已保存数据库不同 |

拒绝隐式`all/visible`、布尔或重复ID和额外参数。不以取消门禁代替其他deck适配。

## 关联回读

结果包含`selection/context/scope_sha256/summary`及以下关系；未网格Macro也可读取，不通过`CONS → GRID`查询实例化种子。

- `faces/macros`：FACE的Macro、完整Macro的FACE/SHELL及原生周界；多FACE Macro的SHELL归属只在Macro层表达。
- `cons`：FACE的CONS、CONS的相邻FACE和冻结状态。先展开根选择的完整Macro，再沿其CAD边界展开一层相邻Macro；更外侧FACE列为`unexpanded_face_ids`，不递归遍历整件。
- `shells/solids/nodes`：明确FE及相关Macro的FE卡片、实际`G*`节点、GRID卡片和原生节点关联的元素/边界条件引用。节点关联不再递归展开；GRID保留原坐标系定义，不把局部卡片坐标当全局坐标。
- `volumes`：通过原生成员关系列出关联Volume的FACE/SHELL/SOLID，不把其全部其他边界扩成操作目标。独立FE可以没有Macro或Volume归属。
- `freeze_targets`：实际需要修改冻结字段的实体。FACE选择对应根Macro的代表FACE；SHELL/SOLID选择只对应显式FE，关联单元、节点和Volume不会自动成为写入目标。
- `freeze_effects`：完整根Macro的FACE及周界CONS影响范围。`effective_macro_protection`根据原生Macro归属与冻结标志推导有效保护，保留来源和未知项，与原始卡片标志分开表达。

FE读`FROZEN`，FACE/CONS读`Frozen`；`freeze_state.field/value/known`区分实际值与未知。缺失不能当未冻结，写前须能独立回读。指纹绑定目标、模型代次、deck、选择、关系及实际卡片；改变选择、状态或重开模型后重新读取。大结果用既有`ansa_mesh_evidence_page`分页，并检查新鲜性；历史证据不自动授予写入权限。

## 冻结语义与保护

FE使用`FreezeElements/UnFreezeElements`处理显式列表；FACE使用`FacesFreezeUnFreeze`设置根Macro代表FACE的原生`Frozen`标志，完整Macro及周界列在`freeze_effects`。独立2026.1夹具显示该操作不会同步改写CONS的`Frozen`字段，不能要求两者一起变为YES。

共享CONS是一个实体，邻接FACE及其Macro会在回读中列出。解冻只清除选定Macro标志，保持范围外Macro与原始CONS标志；若共享边仍受另一个冻结Macro保护，有效保护仍为真。有效保护是基于关系及标志的推导，不等于对所有原生编辑功能逐项试操作的证明。首版FACE写入要求Macro周界为CAD CONS；FE Perimeter可回读，其冻结语义尚未限定时拒绝写入。

写入顺序为新鲜性和授权检查→完整保护快照→验证新备份→再次确认状态→一次原生调用→独立状态及范围回读。只允许声明目标的冻结字段改变，返回`changes`及保护指纹。目标已全部处于请求状态时返回`no_op=true/api_called=false`，不调用原生写入或新建备份。

异常、部分执行或范围外变化保留失败回执、`possible_effects/recovery_required`和可取得的状态观察；目标/模型变化后不将新模型归于本次结果。不自动回滚或重放，先查原`operation_id/task_id`及备份。保护检查覆盖实体、原始卡片、坐标、归属和捕获的原生几何量，不等于CAD连续等价、网格质量或求解器验收。

## 资源与验证

除200个根ID外，展开限1000 FACE、8000 CONS、每类20000 SHELL/SOLID、50000 GRID、1000 VOLUME；Volume成员扫描和节点关联各限200000项。关联证据限32 MiB。写入保护限100000个模型实体和64 MiB原始卡片。超出即明确拒绝，不静默截断或自动拆成多个写任务；不改变其他工具的门禁。

使用顺序：先`ansa_mesh_scope_read`检查关系与`freeze_targets`，再以同一deck/type/ID和刚读取的指纹调用`ansa_mesh_freeze`；检查任务终态、`scope_verified/changes/recovery_required`。解冻前重新读取，并提供新备份路径。

离线入口：`python -m unittest tests.unit.test_mesh_scope tests.unit.test_mesh_scope_schema -v`（`PYTHONPATH=src`）。独立真机入口为`tests/integration/mesh_scope_ansa.py`，实际通过范围见[验收记录](../acceptance.md)。
