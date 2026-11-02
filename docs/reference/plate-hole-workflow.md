# 单孔矩形板原生网格流程

本流程固化已验证的孔板方法：孔周环带与外围矩形分区 → Define Map Block初划 → 四边形面重划 → Freeze → 厚度方向caps → 实体填充。网格生成使用ANSA原生API；参数、范围保护和结果回读由项目代码组织。

## 调用方式

流程由 `ansa_mcp_shared.plate_hole` 生成参数计划，`ansa_mcp_bridge.operations.plate_hole_workflow` 执行原生操作，`ansa_mcp_plugin.workflows.plate_hole_script` 构造调用脚本。客户端通过已有 `ansa_script_execute` 提交，完整工具入口见[工具参考](../tools.md)。

以下示例直接在代码中定义参数，长度单位为毫米：

```python
from ansa_mcp_plugin.workflows import plate_hole_script

params = {
    "length": 60.0,
    "width": 40.0,
    "thickness": 6.0,
    "hole_radius": 6.0,
    "transition_half_size": 9.0,
    "target_length": 1.5,
    "thickness_layers": 4,
    "hole_segments": 48,
    "radial_segments": 4,
    "center": [0.0, 0.0, 0.0],
}
preview_script = plate_hole_script("plan", params)
# 提交preview_script给ansa_script_execute，带当前expected_target、expected_model及新operation_id。
# 查询同一task_id到succeeded，再读取RESULT内的plan_sha256。
apply_script = plate_hole_script(
    "apply", params,
    expected_plan_sha256=plan_sha256,
    backup_path="F:/work/before-plate.ansa",
    output_path="F:/work/plate-result.ansa",
)
# 提交apply_script时设置allow_model_change=True，允许流程重开自身保存的检查点。
# 查询原task；超时或未知状态不能换operation_id重放。
```

脚本构造器不发送任务或操作模型。预检函数只读模型，承载它的通用脚本工具仍遵守既有写入门禁。预检返回的 `plan_sha256` 绑定实际模型与配置；纯 `build_plan` 的 `recipe_sha256` 只标识参数，不能替代模型计划。

## 有限输入与参数

输入为已保存、毫米单位的NASTRAN隔离原始CAD，单Part、恰好8个原始FACE、无既有网格和VOLUME：矩形外轮廓、一个中心圆形贯通孔、恒厚，长度沿X、宽度沿Y、厚度沿Z。`center`允许平移，不代表任意姿态或任意孔识别。原始CAD须符合该声明；多孔、孔偏置、圆角、非矩形外形和母体相连的工业局部域须另立明确方案。

| 参数 | 含义 | 示例 |
| --- | --- | --- |
| length、width、thickness | 毫米尺寸 | 60、40、6 |
| center | 板中心坐标 | [0,0,0] |
| hole_radius | 中心圆孔半径 | 6 |
| transition_half_size | 孔周方形过渡区半边长 | 9 |
| target_length | 外围名义网格尺寸 | 1.5 |
| hole_segments | 孔周总分段，须为8的倍数 | 48 |
| radial_segments | 每个孔周环带的径向分段 | 4 |
| thickness_layers | 厚度层数 | 4 |

孔周每45度弧的分段数须与其对应过渡区直边相同，避免四边映射改变边界分段。以名义几何长度匹配原生CONS，允许已声明的几何数值误差，不能移动节点来凑分段。示例会产生20块、92 FACE，预计1168源四边形、4672六面体、3920面四边形、6300关联节点；这些数量由参数推导，不把某个历史单元数写成通用验收条件。

单次预算为10000实体、5000边界SHELL、15000关联GRID；不修改其他工具的通用预算。完整网格证据存独立目录，MCP只返回摘要与文件位置。写入前备份，异常保留具体阶段与部分模型，不自动重试网格生成。

备份调用本身也可能改变当前数据库路径。开始调用备份后，若备份或随后的预检异常，返回 `recovery_required=true`，即使尚未进行切分或网格生成；调用方须查询原任务并核对当前模型，不能把“尚未生成网格”当作无副作用。模型计划不匹配等备份前拒绝仍不执行备份或原生网格操作。

## 验收与后续

最低验收包含原生块/面/节点完整归属、全六面体、完整外皮和共享接口、正体积、ASPECT≤5及JACOB≥0.2、Freeze前后与填充后的面网格保持、填充前后CAD导出保持、原配置恢复、保存重开。质量阈值是本案例声明，不作为任意工程的推荐标准。

CAD持久性采用按原生FACE ID绑定的逐面IGES D/P摘要；ANSA重开后可能改变整体导出的内部排序，不用整体文件摘要误判几何变化。Part的面积、质量、重心、包围盒和材料/属性汇总复用已有派生字段定义，原材料和属性完整卡片仍独立保护。最终结果须在临时Mesh Parameters恢复后保存，再重开核对完整配置。

2026-10-28案例已通过：20块、4672 CHEXA、3920 CQUAD4、6300 GRID；完整3072外皮和28组共享接口，ASPECT最大2.143193、JACOB最小0.919226。固化期间的回读实现问题在同一实例修正，复用已生成网格完成公开MCP保存重开；没有为收尾重复生成实体。非默认参数尚未逐例真机验收。组合证据及修正边界见[验收记录](../acceptance.md)。

定向单测为 `test_plate_hole_recipe` 和 `test_plate_hole_workflow`。独立源码模式MCP入口的 `--options` 接受 JSON 文本或 JSON 文件路径。使用文件时，将上方 `params` 序列化为自己的 JSON 文件，并替换下方路径占位符：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/integration/plate_hole_workflow_ansa.py --ansa-root "<ANSA安装目录>" --input "<已保存的原始孔板.ansa>" --options "<用户参数JSON文件绝对路径>"
```

每次使用独立实例与新目录。既有实体或不匹配的几何应在写入前拒绝；不为文档或回执整理重划已成功的模型。当前实现和真机状态以[验收记录](../acceptance.md)为准。后续工业局部域先核对原文件的ANSA实体身份、母体接口和几何约束，不能把OCC编号直接当ANSA FACE ID，也不能把矩形板模板套到不同的外形。
