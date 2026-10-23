# 任务与 API 工具契约

本文说明持久任务、查询恢复、日志、本机记忆、脚本和 ANSA API 查询的共同语义。ABAQUS 工具见[ABAQUS 工具契约](abaqus.md)，模型与检查工具见[通用操作](operations.md)。

## 连接状态与恢复

`ansa_connection_status` 区分共享后端不可用、桥接失败、主线程未响应和未绑定目标：

- 后端未就绪时返回 `status=blocked`，错误码为 `BACKEND_STARTING`、`BACKEND_BUSY`、`BACKEND_OFFLINE` 或 `BACKEND_AUTH`；`data.retryable` 表示是否适合稍后重试状态查询。认证或私有目录校验失败需先检查配置与权限。
- 后端就绪时，`data.instances[].connection_state` 为 `bound`、`unbound` 或 `unavailable`；失败实例同时返回 `failure_stage`（`connect` 或 `main_thread_probe`）与 `error`。主线程探测超时不证明 ANSA 进程断开。
- `data.connection_diagnostics` 按实例保留当前连接失败阶段、原因、次数和 `retry_in_seconds`。连接超时为2秒，认证超时为5秒，失败退避为1、2、4、8、15秒，之后上限15秒；连接恢复或实例失效后清除该记录。
- 共享后端重启会改变 `backend_id`，旧目标上下文不能继续用于新操作；先在所属 ANSA 面板重新绑定，再获取模型快照。传输失败不自动重发模型请求，须用原 `operation_id` 查询任务。

## 任务回包

以下封装适用于持久模型任务；连接状态、同步快照和本机记忆等入口直接在 `data` 返回各自结果。

| 字段 | 含义 |
| --- | --- |
| `ok`、`status` | `status` 为任务状态；`failed`、`cancelled`、`expired`、`unknown` 时 `ok=false` |
| `data.result` | 处理器或执行器的业务结果；`queued`、`running` 时通常尚无终态结果 |
| `data.dispatch`、`data.cancel_pending`、`data.acked` | 投递、待取消和结果确认状态 |
| `operation_id`、`task_id` | 原操作键和持久任务 ID |
| `target`、`model` | 提交时的目标与模型上下文 |
| `error`、`warnings`、`artifacts` | 结构化错误、警告和附件 |

任务成功表示 ANSA 调用和持久化流程完成；业务问题仍由 `data.result` 中的检查、错误和完整性字段表达。

共享后端的提交请求可能已经送达但回包丢失或无效时，返回 `status=unknown`、`error.code=TASK_UNKNOWN`，保留原 `operation_id`、目标与模型；尚未获知的 `task_id` 为 `null`。此时先用原 `operation_id` 查询并独立回读，不能视为任务被拒绝或改用新操作键重试。请求在本地校验或建连前失败、以及后端明确拒绝时，仍返回失败。

## 上下文与操作键

- `expected_target` 包含 `backend_id`、`instance_id`、`session_id` 和正整数 `generation`；`expected_model` 包含正整数 `epoch`。目标、会话或代次过期时，新写入被拒绝。
- `operation_id` 为 1–128 字符字符串。请求身份由工具名、参数、目标、模型和 `ttl_seconds` 共同组成。
- 相同 `operation_id` 和相同请求返回原任务及原结果，不因客户端超时、模型切换或后端重启而重放执行。
- 相同 `operation_id` 配不同请求会被拒绝；操作键同时被任务和本机记忆使用，不能跨写入类别复用。
- `ttl_seconds` 为严格整数 0–86400，默认 300。它限制未投递任务的排队寿命；已发送任务不会因该计时器自动撤销。
- 各工具按自己的 schema 校验参数；严格入口会拒绝未声明字段、错误原始类型和字符串形式的布尔值或数字。

## 状态与取消

| 状态 | 含义 |
| --- | --- |
| `queued` | 已持久化，尚未由执行器领取 |
| `running` | 已领取并可能正在产生模型写入 |
| `succeeded` | 执行器已持久化成功结果 |
| `failed` | 正常失败，结果或错误已持久化 |
| `cancelled` | 未领取任务已取消，或已发送任务经执行器确认未领取 |
| `expired` | 尚未发送且超过排队寿命，或目标/后端身份变化导致不能投递 |
| `unknown` | 已发送但原执行会话不可用、持久化失败或结果无法确认；不是成功，也不是可安全重放的失败 |

`ansa_task_query(task_id=..., operation_id=...)` 接受任一 ID 或一致的双键。缺少键为 `QUERY_REJECTED`，任务不存在或双键不匹配为 `TASK_NOT_FOUND`，存储或查询故障为 `QUERY_UNAVAILABLE`。查询已发送任务会尝试同步原执行器 Journal；查询本身不执行模型操作。

`ansa_task_cancel(task_id=...)` 对未发送的 `queued` 任务立即取消；对已发送的 `queued/unknown` 任务写入 `cancel_pending`，等待执行器确认。`running` 任务明确拒绝取消。待取消不等于已取消。

## 历史与日志

`ansa_task_list(limit=30, state=None, before_sequence=None)` 直接读取当前运行目录的持久历史，不需要活动 ANSA 目标。

- `limit` 为严格整数 1–100；`state` 可选七种状态之一；`before_sequence` 为持久序号游标。
- `data.tasks` 返回 `task_id`、`operation_id`、`state`、`tool`、`dispatch`、`cancel_pending`、`target` 和 `model` 摘要，不返回请求参数、脚本或结果正文；共享后端在传输前提取摘要，避免多个大任务结果使历史列表超过通信帧限制。
- `has_more` 和 `next_before_sequence` 支持从新到旧的稳定续页；新任务不会挤入已开始的续页。游标只适用于同一运行目录。

`ansa_log_read(max_chars=65536)` 读取当前运行目录最新模型任务的持久结果/错误和最近最多 200 项任务元数据，不连接或执行 ANSA，不接受文件路径。`max_chars` 为 1–65536。回包用 `found/available` 区分无任务与无正文；最新任务没有结果时不退回旧结果。正文被截断时，内联 `result/error` 为 `null`，完整内容进入内容寻址的私有 JSON 附件，并返回 SHA-256。该工具不恢复未持久化的 stdout、外部控制台或安装日志。

## 本机记忆

`ansa_memory_read` 和 `ansa_memory_write` 在当前 `runtime_home` 的 SQLite 中读写明确指定的偏好和笔记，不连接 ANSA，不访问用户全局记忆。

- 读取：`key` 可选。按键返回 `found` 和 `record`；省略 key 时读取分页快照，`limit` 为 1–100。偏好使用 `after_key`，笔记使用 `before_note_sequence`。
- 偏好写入：`kind="preference"`、`key`、`value`、`expected_revision` 和可选 `note`。`expected_revision=0` 仅允许首次创建；更新须使用刚读取的 revision。
- 笔记写入：`kind="note"`、非空 `text` 和可选 `tag`；每次追加，返回持久 `sequence`。
- 记忆写入使用唯一 `operation_id`。相同请求和键返回原回执，换请求冲突；本地记忆写入不创建 ANSA `task_id`。

key 为 1–120 字符；文本和序列化 JSON 值最多 4000 字符；严格 JSON、有限数值和声明字段会被校验。错误码为 `MEMORY_CONFLICT`、`MEMORY_REJECTED` 或 `MEMORY_UNAVAILABLE`。

## 脚本预检与执行

`ansa_script_validate` 只解析 Python 并静态检查可见的 ANSA API 调用；它不执行脚本，也不保证参数、返回值和副作用适用。

`ansa_script_execute` 的参数为：

| 参数 | 契约 |
| --- | --- |
| `script` | 1–65536 字符的 UTF-8 文本；语法始终校验 |
| `validate` | 严格布尔值，默认 `true`；为 false 时仅跳过缺失 API 的前置拒绝 |
| `function_name` | 可选 Python 标识符，1–128 字符；不解析点分路径 |
| `allow_model_change` | 严格布尔值，默认 `false` |

脚本先执行顶层代码，再按名称查找并同步、零实参调用一次入口。脚本命名空间中的 `__name__` 为 `__ansa_mcp_task__`。有 `RESULT` 时优先使用；没有入口且没有 `RESULT` 为失败；入口返回 `None` 是合法的 JSON `null`。异步函数、生成器和返回值中的 awaitable/生成器会被拒绝。stdout/stderr 最多保留 65536 字符并标记 `stdout_truncated`。

入口不存在、参数不匹配或函数异常都可能发生在顶层副作用之后；错误文本包含“effects may exist”。默认不允许更换模型；失败或超时后查询原 `task_id/operation_id` 并独立回读。

## ANSA API 查询

`ansa_api_reference` 的 `action` 为 `lookup`、`search`、`modules` 或 `categories`，`limit` 为严格整数 1–20。

- `lookup` 和 `search` 的 `query` 必填；`lookup` 支持 `ansa.base.GetEntity` 形式，`search` 返回有界候选。
- `modules` 和 `categories` 要求空 `query`；可传精确 `module` 筛选。
- 支持的模块白名单为 `base`、`mesh`、`session`、`constants`、`connections`、`cad`、`morph`、`guitk`、`utils`、`batchmesh`、`kinetics`。
- 文档查询优先使用版本匹配的已安装官方帮助，缺失时回退运行时 docstring，并明确 `source_type`、`complete` 和截断字段。目录只统计公开顶层函数；分类是插件名称规则，不是官方分类或读写保证。

`validate` 和 API 目录都不执行发现的函数、不证明写入安全，也不替代目标/模型代次检查。
