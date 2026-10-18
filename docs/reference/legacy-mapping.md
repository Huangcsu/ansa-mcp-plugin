# 工具功能映射

更新：2026-10-18。以 2026-10-12 留存的旧主服务注册源码快照中的 79 项为功能基线；独立 `ansa-api` 的 1 项另列兼容映射。当前新服务注册 65 个 MCP 工具，口径不同；79 项映射为79 项限定验收、0 项部分实现、0 项待实现。

新入口可合并多个旧功能；规划入口尚未注册，不能调用。普通脚本能力不能代替每项业务工具的实现或验收。`已实现` 不等于所有实体类型或场景均已真机验收。

| 原功能 | 新入口/规划入口 | 读写属性 | 实现与验收状态 |
| --- | --- | --- | --- |
| `ping` | ansa_connection_status | 只读 | 已验收 |
| `check_ansa_connection` | ansa_connection_status + ansa_model_snapshot | 只读 | 已验收 |
| `get_command_status` | ansa_task_query / ansa_task_list | 任务状态读取/结果同步 | 已验收：本机后端按task_id/operation_id及一致双键查询，区分未给键/未找到/查询故障与已有unknown；七状态筛选、持久seq游标/新插入/空末页、后端重启、摘要不暴露正文，真实MCP stdio/TCP/SQLite及模拟执行器5项通过；不重执行模型，不作为新增ANSA建模或画布验收 |
| `recall` | ansa_memory_read；历史见 ansa_task_list | 本机只读 | 已验收：私有运行目录的按键读取/不存在键与双游标快照，严格原始JSON类型、稳定偏好/笔记顺序、空尾页和运行目录隔离；5组真实MCP/SQLite检查通过，任务历史另行读取，不连接ANSA或全局Basic Memory |
| `remember` | ansa_memory_write: preference | 本机状态写入 | 已验收：显式偏好写入、当前revision前置条件、保留上一值含null、严格JSON/大小/深度限制、跨记忆与模型任务的原子操作键互斥；并发仅一方成功、原回执幂等和重启持久化，本机后端能力，无ANSA建模验收 |
| `remember_note` | ansa_memory_write: note | 本机状态写入 | 已验收：显式笔记追加、tag/时间/持久sequence、原操作防重和换请求冲突、重启后原回执一致；原始参数/失败无修改、双游标及隔离已通过真实MCP验证；仅保存明确请求的数据，不自动记忆 |
| `execute_script` | ansa_script_execute | 写入/可换模 | 已验收：主线程、严格 RESULT、失败副作用与幂等查询 |
| `ansa_api_doc_search` | ansa_api_reference: search | 只读 | 已验收：原26版运行时文档字符串；本批扩展版本匹配的安装帮助、来源/完整性和分页总数，当前范围见P0扩展记录 |
| `ansa_api_doc_lookup` | ansa_api_reference: lookup | 只读 | 已验收：原26版运行时文档字符串；本批扩展官方HTML/stub的签名、参数、返回、示例及明确回退，当前范围见P0扩展记录 |
| `ansa_api_doc_modules` | ansa_api_reference: modules | 元数据只读 | 已验收：ANSA2026.1.0支持白名单10命名空间的公开顶层函数计数、单模块筛选、排序、当前版本和旧字段兼容；实际MCP/TCP主线程与独立运行时名称回读一致，不可用项明确不完整；constants原生对象读取已核实，非全部模块/类方法目录 |
| `ansa_api_doc_categories` | ansa_api_reference: categories | 元数据只读 | 已验收：同10命名空间公开顶层函数按透明插件名称规则分类，3872函数/16非空组计数守恒，模块筛选、严格拒绝、幂等和模型无改动5项真机检查通过；分类不是官方taxonomy或读写保证，不含类方法/递归子模块和其他模块 |
| `validate_script` | ansa_script_validate | 只读 | 已验收：缺失 API 拒绝，预检不执行 |
| `get_capabilities` | ansa_connection_status + ansa_model_snapshot | 只读 | 已验收 |
| `get_model_info` | ansa_model_snapshot + ansa_model_summary | 只读 | 已验收：ANSA2026.1.0/NASTRAN，同代次快照/摘要、原生总数与八类计数，MAT1/PSHELL/PSOLID/GRID/SHELL独立回读；New默认Part/Untitled、副本保存/打开/同路径重开、陈旧上下文/严格输入、幂等及后端重启原查询6组通过；分类不可用为null并报告完整性，总数不可读则失败；不新增画布，ABAQUS节点映射仅本批单元覆盖 |
| `geometry_inventory` | ansa_model_inspect: geometry_inventory | 只读 | GRID/MAT1 计数已验收；CAD 类型待验收 |
| `import_file` | ansa_model_open / ansa_model_import | 写入/换模 | 已验收：.ansa Open、三种 solver 备份后合并导入；STEP 显式 replace、默认备份/实际恢复、1 FACE/面积回读、幂等、保存重开及 GUI；CAD 合并/其他格式待实现 |
| `list_faces` | ansa_entity_list: FACE | 只读 | 已验收：单 FACE 的 ID/数量回读；其他字段/分页场景待验收 |
| `face_properties` | ansa_model_inspect: face_properties | 只读 | 已验收：NASTRAN 三个平面 FACE 的面积/中心及可选原始方向/单位方向/包围盒，完整性、默认兼容、显式子集、幂等、保存重开及GUI；字段异常证据已离线检查，名称/曲面信息/局部法向和其他deck待实现或验收 |
| `select_faces_by_query` | ansa_geometry_select | 只读 | 已验收：面积/中心五种排名及归一化法向Z升降序、显式范围与稳定同分排序；法向查询无符号过滤/阈值，曲面局部法向及其他deck待验收 |
| `preview_selection` | ansa_geometry_preview | 只读 | 已验收：五种基础排名及法向候选、拟删除ID/面积/包围盒、只读删除条件、空模型、幂等及保存重开；不改变模型/可见性/选择，实际删除须另行校验 |
| `delete_faces` | ansa_geometry_delete | 写入 | 已验收：NASTRAN 两个独立未冻结/未网格FACE的卡片/几何/边界条件、整批拒绝、精确范围、幂等、保存重开/实际备份恢复及GUI；共享边界/多FACE宏/强制删除/其他deck未开放 |
| `autosave_model` | ansa_model_autosave / ansa_model_save | 文件写入 | 已验收：唯一自动命名、副本回读、幂等重试及保留当前路径 |
| `restore_model` | ansa_model_open | 换模 | 已验收：.ansa 默认备份及独立回读 |
| `save_model_as_ws` | ansa_model_save | 文件写入 | 已验收；路径不再限定旧工作区 |
| `check_mesh_quality` | ansa_model_inspect: mesh_quality | 只读 | 已验收：NASTRAN 选定 FACE 只读质量报告，不自动修复；SHELL/PSHELL及其他质量场景待验收 |
| `surface_mesh` | ansa_mesh_faces + ansa_mesh_surface | 只读/写入 | 已验收：NASTRAN 单 FACE 完整未网格宏，Tria/1st Order、目标尺寸、参数恢复、SHELL范围回读及幂等；其他类型/多FACE待验收 |
| `export_solver_deck` | ansa_model_export | 文件写入 | 已验收：NASTRAN/ABAQUS/LSDYNA 单文件导出、独立内容及 SHA-256 回读 |
| `batch_benchmark_test` | ansa_benchmark_run | 写入/换模/文件 | 已验收（限定范围）：当前 NASTRAN 的 .ansa/.step/.stp 文件集，按大小/路径排序、严格最多10项及1000扫描条目，输入SHA保留；新备份实际重开核对、逐case计数/最大面积/最高中心Z排名、私有JSON/CSV与真实SHA、严格拒绝/原回执和后端恢复六组真机通过。成功结束实际打开备份；Open失败/身份未知停止，不自动恢复，保留备份及实际状态；验收的额外显式model_open恢复另行记录。未规范化新模型的默认Part计数差异仍严格拒绝；不是FPS/网格性能测试，不含其他格式/deck或画布 |
| `read_last_log` | ansa_log_read | 后端读取/附件写入 | 已验收：当前私有运行目录最新持久result/error及已有stdout、无正文不回退、最近200模型任务审计投影、严格1-65536字符正文预览、完整SHA256私有JSON附件/防覆盖/重启复用；五组真实MCP/SQLite与已有ANSA八对象结果读取通过，46原任务记录语义未变；不执行ANSA或读取任意路径，不冒充全局控制审计或恢复未持久输出 |
| `ping_ansa` | ansa_connection_status + ansa_model_snapshot | 只读 | 已验收 |
| `open_model` | ansa_model_open | 换模 | 已验收：默认备份及更新模型代次 |
| `new_model` | ansa_model_new | 换模 | 已验收：默认备份、空模型写入与陈旧上下文拒绝 |
| `save_model` | ansa_model_save / ansa_model_save_current | 文件写入 | 已验收：副本及原路径保存；原路径必须匹配 expected_path 且明确 overwrite=true |
| `save_model_as` | ansa_model_save | 文件写入 | 已验收 |
| `export_nastran` | ansa_model_export: NASTRAN | 文件写入 | 已验收：单文件产物、GRID 内容及显式覆盖保护 |
| `export_lsdyna` | ansa_model_export: LSDYNA | 文件写入 | 已验收：单文件产物及 NODE 内容；当前 NASTRAN 测试模型跨格式导出 |
| `export_step` | ansa_model_export: STEP | 文件写入 | 已验收：显式 visible、真实 ISO-10303-21/CAD 产物及原任务恢复；需在所属 ANSA 确认 Output Parameters |
| `run_python_script_in_ansa` | ansa_script_execute: function_name | 写入/可换模 | 已验收：同步无参全局入口、严格名称/原始参数、RESULT优先/None、异步与生成器拒绝、失败副作用/模型信息、SystemExit普通失败及原操作幂等；6组MCP/TCP/所属主线程真机通过，NASTRAN测试GRID卡片独立回读、保存和后端重启原查询无重放；不新增画布或其他deck写入验收 |
| `count_entities` | ansa_entity_count | 只读 | GRID/MAT1 已验收；其他类型待验收 |
| `list_entities` | ansa_entity_list | 只读 | GRID 分页、MAT1 字段已验收；其他类型待验收 |
| `get_entity` | ansa_entity_get | 只读 | MAT1 / ABAQUS NODE 已验收；其他类型待验收 |
| `set_entity_fields` | ansa_entity_update | 写入 | MAT1 / ABAQUS NODE 已验收；需预期字段 |
| `create_entity` | ansa_entity_create | 写入 | MAT1 / ABAQUS NODE 已验收；首次非空字段须有可信定义 |
| `delete_entities` | ansa_entity_delete | 写入 | MAT1 批量 / ABAQUS NODE 已验收；先校验全部前置条件 |
| `search_entities_by_name` | ansa_entity_find | 只读 | 已验收：ANSAPART 精确名称检索；其他匹配模式/实体类型待验收 |
| `get_bounding_box` | ansa_entity_bounds | 只读 | NASTRAN GRID 已验收；其他类型待验收 |
| `get_node_coordinates` | ansa_node_coordinates | 只读 | 已验收：NASTRAN GRID |
| `change_element_type` | ansa_element_change_type | 写入 | 已验收（限定范围）：仅 NASTRAN 显式 1–100 个 CBAR→CROD，使用既有 PROD 和已验证的 .ansa 备份；R1/R2 各六组独立回读通过。R2 后仅新增 128 KiB 请求门禁与失败文本预算，11 项定向 MCP/单测、最终 240 项全量单测及门禁后 55 项 MCP smoke 通过；新增门禁未在 R2 的 ANSA 运行中覆盖。主窗口画布、其他转换或 deck 未验收；类型改变不代表物理等效 |
| `create_part` | ansa_part_create | 写入 | 已验收：ANSAPART 身份、Name 和 Module Id 独立回读 |
| `create_set` | ansa_entity_create: SET | 写入 | 已验收：NASTRAN SET 默认创建、Name 配置及成员回读 |
| `add_to_set` | ansa_set_add + ansa_set_members | 写入/只读 | 已验收：NASTRAN GRID 普通成员加入、前置条件、幂等及回读；直接成员不展开嵌套 SET；最大 200 项 |
| `get_model_summary` | ansa_model_summary | 只读 | 已验收：NASTRAN 原字段兼容，新增deck名称/节点类型/八类计数及完整性；PSHELL与PSOLID合计、独立类型去重核算、空模型/默认Part、上下文与严格参数、幂等/重启原查询6组通过；总数含原生内部对象，不解释为类别总和或模型已加载标记 |
| `list_model_includes` | ansa_model_includes | 只读 | 已验收：NASTRAN 父/子/空 Include 的 ID/Name、稳定分页、查询无修改、保存/同路径重开及 GUI；其他 deck 和配置场景待验收 |
| `calc_element_mass` | ansa_model_inspect: entity_metrics/mass；mass_properties | 只读 | 已验收：原NASTRAN CQUAD4/CTRIA3/CHEXA8显式单实体GPWG三方向质量及保存重开/GUI；新增ABAQUS材料/属性等明确范围展开去重SHELL/SOLID后CalcElementMass标量，解析质量、壳板筛选及大模型副本经真机核对，见P0扩展。非壳/实体、复合材料和OPTISTRUCT未扩展验收；零/非有限或材料不明拒绝，不证明全部模型质量完整 |
| `calc_shell_area` | ansa_model_inspect: entity_metrics/area | 只读 | 已验收：NASTRAN CQUAD4/CTRIA3 SHELL 面积，与独立节点坐标计算一致，查询无修改、非法输入拒绝、保存重开及GUI；其他类型/deck待验收 |
| `calc_solid_volume` | ansa_model_inspect: entity_metrics/volume | 只读 | 已验收：NASTRAN CHEXA8 SOLID 体积，与独立节点坐标计算一致，查询无修改、非法输入拒绝、保存重开及GUI；其他SOLID类型/VOLUME/deck待验收 |
| `check_intersections` | ansa_mesh_intersections | 模型只读 | 已验收：NASTRAN 两组显式 CQUAD4/CTRIA3，独立卡片/坐标交线核算、混合/单类型/无问题、同组内部与范围外排除、反向组、隐藏、严格拒绝、幂等、保存重开和主窗口；固定相交模式，原生计数与去重实体分开，不修复；其他类型/deck及全模型自检待验收或实现 |
| `check_penetrations` | ansa_mesh_penetrations | 模型只读 | 已验收：NASTRAN 两组显式 CQUAD4/CTRIA3、正厚度 PSHELL 且两组属性集合不相交，属性倍率/用户厚度两模式、独立卡片/间距核算、隐藏、范围排除、严格拒绝、幂等与保存重开；6项核心及3项原任务/主窗口续验通过，原GUI超时报告保留；相交/穿透类别与去重实体分开，不修复；同属性、其他类型/deck及复杂厚度场景待实现或验收 |
| `check_free_nodes` | ansa_check_run: free_nodes | 模型只读 | 已验收：NASTRAN 显式 GRID，SHELL/CONM2 卡片独立连接核算、质量节点选项、单节点邻居补齐/子集/无问题、隐藏单元、严格拒绝、幂等、保存重开及 GUI；保留原生检查历史，不修复；其他 deck、刚性/辅助/载荷等场景待验收 |
| `run_quality_check` | ansa_mesh_failed_elements | 模型只读 | 已验收（限定范围）：旧工具标准路径固定执行MeshQuality，check_name不选择子检查且不自动修复；复用现有NASTRAN显式1–200个CQUAD4/CTRIA3的MeshQuality工具、当前全局准则全文/指纹、原生报告树和去重失败单元。既有8项核心/主窗口与3项准则恢复补验分别保留，原核心报告末尾恢复断言失败仍为false；本次核对旧源码和四个生产模块未变，不新增ANSA执行。全模型、其他deck/类型和自定义检查名未覆盖 |
| `count_failed_elements` | ansa_mesh_failed_elements | 模型只读 | 已验收：NASTRAN 显式 CQUAD4/CTRIA3，当前全局准则全文/指纹、关闭准则、良好/失败及单类型子集、隐藏单元、范围外排除、报告叶统计与失败ID去重、严格拒绝、幂等、保存重开及主窗口；稳定准则恢复补验通过、原失败报告保留；0不表示全工程通过，不加载准则、不修复；其他deck、实体及准则模式待验收 |
| `check_geometry` | ansa_model_inspect: geometry_check | 模型只读 | 已验收（限定范围）：当前 NASTRAN 显式 1–100 个 FACE 的 Basic 检查；逐项回读五个布尔选项、双向核验边界 CONS、完整原生报告树及问题叶/去重 CONS/受影响 FACE。默认/单边界问题、子集/隐藏/范围外排除、严格拒绝、幂等、保存同路径重开及后端重启六组通过；不修复、不改变显示、不代表全模型通过。CONS、Extensive、其他 deck 和复杂几何未验收 |
| `check_sharp_edges` | ansa_mesh_check: sharp_edges | 模型只读 | 已验收：NASTRAN 显式 CQUAD4/CTRIA3，共边连接卡片/坐标独立法向核算、80/120角度阈值、混合与三角子集、隐藏单元、范围外排除、报告叶统计、严格拒绝、幂等、保存重开及主窗口补验；固定原生枚举0/0、保留检查历史，不修复；其他deck、凸边/混合边、高阶/非平面/三重边及其他模式待验收 |
| `check_rigid_dependencies` | ansa_rigid_dependencies | 模型只读 | 已验收：NASTRAN 显式 RBE2，GN/CM/全部GM及GRID连接闭包核验、故障/干净/分离自由度/多从节点与单张子集、隐藏、严格拒绝、幂等和保存重开6项核心通过；修复后5个范围回归、原任务查询/所属主窗口及独立回读续验通过，原报告和文件指纹保留；固定NastranDependency工厂、保留历史、不修复，叶问题数与去重RBE2/GRID分开；其他类型/deck/复杂依赖及修复未完成 |
| `calc_mesh_quality` | ansa_model_inspect: mesh_quality | 只读 | 已验收：NASTRAN FACE 固定只读检查报告；其他类型及完整统计待验收 |
| `mesh_shells` | ansa_mesh_surface | 写入 | 已验收（限定范围）：NASTRAN 单个显式未网格 FACE 所属完整宏；Tria/1st Order、absolute 目标长度（已验收值 2）、44 个 CTRIA3、参数恢复和独立回读通过。多 FACE/宏、其他网格参数/类型及已有 SHELL 重划仍未验收；旧空 entity_ids 的全可见模式不纳入当前入口 |
| `mesh_volume` | ansa_mesh_volume | 写入 | 已验收（限定范围）：NASTRAN 单个已有未网格 VOLUME，六个 CQUAD4/八角轴对齐封闭盒体、既有 PSOLID/已定义 MAT1，先验证新 .ansa 备份，再单次 TETRA RAPID；真实输出六个 PYRAMID 和一个内部 GRID，边界/完整 FE 与节点卡/属性材料定义、拓扑闭合和体积独立核对，六组真机通过，含实际备份恢复、保存重开及后端原回执。API 名称不表示纯四面体，内部节点不要求几何中心；不创建 VOLUME，不含复杂几何、其他 deck 或画布验收 |
| `set_shell_mesh_params` | ansa_mesh_settings；ansa_mesh_surface 临时设置 | 只读/设置写入 | 已验收：会话内 absolute 尺寸、类型与阶次，完整当前值前置条件、备份回读、幂等、后端重启及 GUI；不写用户默认配置、不自动生成网格 |
| `delete_mesh` | ansa_mesh_delete | 写入 | 已验收：NASTRAN 指定 SHELL 前置字段、精确删除/幂等、其他 SHELL及节点/几何保留；其他类型待验收 |
| `run_batch_mesh` | ansa_script_execute: UTF-8 文件内容 + function_name=main | 写入/脚本 | 已验收（限定范围）：旧入口实际读取 UTF-8 Python 文件并执行可选同步零实参 main，不是专门的 batchmesh 求解接口；合并为客户端明确读取文件内容后提交命名脚本。NASTRAN 单 FACE 真实 mesh.Mesh、64 CTRIA3/35 GRID、尺寸2及原参数恢复、原操作回执/冲突/失败 main 无重划、保存同路径重开/陈旧模型及后端重启五组通过；脚本自行导入模块，不保留旧预注入 mesh/connections/json 或 __batch__ 名称，无画布、多模型批量或任意外部脚本验收 |
| `apply_connectors` | ansa_connection_apply | 写入 | 已验收（限定范围）：NASTRAN 单个显式未实现 SpotweldPoint_Type、两个明确单 CQUAD4 Part、正厚度/零 NSM PSHELL 和已定义正密度 MAT1、新备份和固定九项 RBAR 设置；单次实现、两投影 GRID、两个四三角形扇形、一个 RBAR 及八个单侧 FE PERIMETER 完整卡片/关联壳范围独立回读；严格拒绝、原回执不重放、实际备份恢复、保存同路径重开和后端重启六组通过。不含已有目标边界、非默认角点厚度、其它连接类型/复杂几何/deck或画布 |
| `check_connections` | ansa_connection_check | 模型只读 | 已验收（限定范围）：NASTRAN 显式 1–200 个 SpotweldPoint_Type；调用前核验完整卡片及两个不同 Part；仅按绝对距离方式启用 double_connections，并在执行前回读距离选项。属性未保留请求值（含极小值）即拒绝执行。原生 projection 仍可能报告问题；原生报告叶数与去重 failed_point_ids 分开返回。不自动修复，不含显示或画布验收 |
| `list_connectors` | ansa_connection_list | 模型只读 | 已验收：NASTRAN CONNECTOR_ENTITY与六类连接点，真实connector及七个点的独立身份/名称/坐标/直径/P字段归属、分类、稳定分页和空尾页、查询无修改、保存同路径重开及原任务恢复；七组正式MCP/TCP/主线程检查通过，不新增画布验收；线/面连接、通用connector的Part/接口点归属扩展及其他deck未开放或待验收 |
| `create_connection_point` | ansa_connection_create | 写入 | 已验收：NASTRAN六类连接两个不同ANSAPART的点、正直径/名称/坐标独立回读、默认Bolt/自动ID、混合connector模型、占用ID和整批预检、原操作防重及换请求拒绝、保存重开/重启原回执；创建后失败保留已知实体与范围证据，必要故障离线检查通过；不生成FE实现，不新增画布或其他deck验收 |
| `show_only` | ansa_model_inspect: visibility_snapshot + entity_visibility/show_only | 显示写入 | 已验收：NASTRAN 明确 FACE/SHELL ID 隔离、完整可见集前置条件与独立回读，模型内容保留；其他类型/deck和复杂联动待验收，GUI状态见显示记录 |
| `show_also` | ansa_model_inspect: visibility_snapshot + entity_visibility/show_also | 显示写入 | 已验收：NASTRAN 明确 FACE/SHELL ID 追加显示、保留原可见实体及独立回读；其他类型/deck和复杂联动待验收，GUI状态见显示记录 |
| `hide` | ansa_model_inspect: visibility_snapshot + entity_visibility/hide | 显示写入 | 已验收：NASTRAN 明确 FACE/SHELL ID 隐藏、其他同类可见实体保留、整批预检与独立回读；其他类型/deck和复杂联动待验收，GUI状态见显示记录 |
| `near` | ansa_model_inspect: visibility_snapshot + entity_visibility/near | 显示写入 | 已验收：NASTRAN 四边形 SHELL 当前可见范围、有限正半径/模型长度单位、dense_search、原生0返回码与实际新增显示回读；非只读空间查询，自定义种子及其他场景未完成，GUI状态见显示记录 |
| `neighb` | ansa_model_inspect: visibility_snapshot + entity_visibility/neighb | 显示写入 | 已验收：NASTRAN 四边形 SHELL 当前可见范围的1/2步连接邻居、独立节点拓扑和原操作幂等；接口步数1-20，ALL及其他场景未开放或待验收，GUI状态见显示记录 |

独立 `search_ansa_api` 合并到 `ansa_api_reference: search`。版本匹配的官方帮助、运行时回退及11个模块的边界见[任务与API](tasks-api.md)。

新增ABAQUS四边形、Map六面体、机构、接触及网格后续操作见[ABAQUS契约](abaqus.md)，不加入原79项历史映射的分母。

当前入口见[工具参考](../tools.md)，模型、几何、检查和连接的范围见[通用操作](operations.md)。机器清单见[tool-mapping.json](../tool-mapping.json)，验证边界见[验证范围](../acceptance.md)。本页由 `scripts/update_tool_mapping.py` 生成。
