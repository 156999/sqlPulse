# 压测 MCP 设计方案

## 一、背景与目标

SQL Pulse 已具备 SQL 压测执行、实时指标采集、任务停止和 L0/L1 报告能力。当前压测主要通过 Web 表单启动，压测 MCP 将这些能力以 MCP tools 暴露给 agent，使 agent 能在用户明确给定连接、SQL 和负载参数后创建任务、跟踪运行、读取指标与报告。

本方案遵循现有造数 MCP 的结构：MCP server 负责协议和参数适配，业务逻辑复用 SQL Pulse 服务；数据库连接只使用用户已保存的连接；创建任务后返回 `run_id`，后续通过独立 tools 跟踪。

首版目标：

- 支持查询当前用户的已保存连接。
- 支持不执行 SQL 的解析与占位符预览。
- 支持以已保存连接创建 SQL 压测任务。
- 支持查询状态、实时指标、停止任务、读取最终报告。
- 保持 MCP 调用与 Web 入口具有一致的 SQL 校验和运行参数约束。

首版不开放任意 DSN、Shell、任意 Python/Locust 脚本、任意 SQL explain/探测，也不提供跨用户任务查询。

## 二、设计原则

1. **复用现有压测执行链路**：MCP 不实现第二套 Locust runner，不直接操作目标数据库。
2. **认证用户贯穿任务生命周期**：创建、查状态、查指标、停止、取报告均验证 `owner_user_id`。
3. **创建前预览，服务端创建时重新校验**：预览只是辅助；正式创建仍运行现有 `prepare()`、校验并检查连接。
4. **负载上限适合自动化调用**：MCP 使用不高于 Web API 的上限，首版建议并发不超过 100、时长不超过 1800 秒；单用户并发运行任务数建议默认 1，可配置调整。
5. **压测属于有影响的操作**：agent 必须让用户明确给出压测意图、目标库和负载范围；如果目标环境或负载范围不清楚，不创建任务。对生产环境或高并发/长时任务，需在创建前向用户说明参数并确认。
6. **最小披露**：不返回 DSN、密码、原始连接快照；SQL 回显仅返回任务原始输入中必要内容，日志和异常信息需脱敏。

## 三、架构

```text
SQL Pulse Web / 内嵌 Agent / MCP Client
                   |
                   v
             MCP server adapter
       固定用户上下文 / token 认证（后续）
                   |
                   v
             authenticated user_id
                   |
                   v
       run service（待抽取/新增）
   prepare + connection_manager + runner
       /          |             \
      v           v              v
     SQLite     LocustRunner    MySQL target
   runs/metrics/reports             |
                                   v
                         collector / report
```

建议将 `app/routers/tasks.py` 中创建、查询、停止任务的业务逻辑逐步下沉到 `app/services/run_jobs.py`。MCP adapter 只做 schema、用户上下文和结果格式适配；Web router 与 MCP 共用服务。

建议服务接口：

```python
list_my_connections(user_id)
preview_run(user_id, payload)
create_run(user_id, payload) -> {"run_id": ...}
get_run(user_id, run_id)
get_run_metrics(user_id, run_id, since_ts=None, limit=300)
stop_run(user_id, run_id)
get_run_report(user_id, run_id)
```

现有 `prepare()`、`connection_manager.resolve_connection()`、`connection_manager.test_dsn()`、`runner.start()/stop()`、`db.get_metrics()`、`report.generate_report()` 可复用。报告结果优先由 `reports` 表中的 `l0_json` / `l1_json` 组成结构化输出；不向 MCP 暴露本地文件路径。

## 四、身份、连接和数据归属

### 4.1 身份来源

首版按已实现的 `app/mcp/server.py` 模式，为 MCP 进程绑定一个 SQL Pulse 用户（`--user-id` / `--user` 或 `MCP_USER_ID` / `MCP_USERNAME`）。MCP 请求不能在每次 tool 调用时自行指定 `user_id`。

后续如需多用户远程 MCP，再增加与造数 MCP 一致的 token/OAuth 身份适配；MCP tools 本身继续只接收已认证用户上下文。

### 4.2 连接选择

- `list_my_connections` 只列当前用户连接，字段限定为 `id/name/host/port/database/user/has_password`。
- `create_sql_run` 只接收 `connection_id`；服务端校验连接归属后解析 DSN。
- 不接收 `db_dsn`、数据库密码、主机覆盖参数或任意 URL。
- 正式创建前测试连接。若用户修改或删除连接，已创建任务使用加密的 DSN 快照，行为与现有任务相同。

### 4.3 runs 所有权迁移

当前 `runs` 表没有 `owner_user_id`，`GET /api/runs/{run_id}` 和停止接口也没有统一的用户归属校验。MCP 接入前必须：

1. 给 `runs` 增加 `owner_user_id TEXT`，并为新任务写入当前用户 ID。
2. 新增 `db.get_run_for_user(user_id, run_id)` 等带归属过滤的 DB 方法。
3. 通过服务层对状态、指标、停止和报告查询执行同一归属检查。
4. HTTP 路由同步使用归属校验，不能只保护 MCP 而留下可猜测 `run_id` 的旁路。
5. 历史无归属任务默认不经普通 MCP 暴露；管理员兼容策略另行定义，不把空归属解释为当前用户。

对指标、报告的 SQL 查询也应在同一 service 内先校验任务归属，再返回关联记录。

## 五、MCP tools

所有名称建议采用 `run` 术语，避免与造数 job 混淆。

### 1. `list_my_connections`

列出当前用户保存的压测目标连接。响应不含密码。

### 2. `preview_sql_run`

对 SQL 文本或结构化任务组进行静态解析、参数占位符检查和示例渲染；不连接目标库、不执行 SQL、不创建任务。输入沿用 `SqlInput` 的互斥约束：`sql_content` 与 `groups` 二选一，支持 `variables`。输出包含 `ok/groups/errors`，错误结构与现有 `/api/runs/preview` 一致。

### 3. `create_sql_run`

创建并启动压测。输入：

```json
{
  "connection_id": "conn-id",
  "name": "orders 查询基准",
  "sql_content": "SELECT * FROM orders WHERE id = {{rand(1,100000)}}",
  "variables": {},
  "concurrency": 20,
  "spawn_rate": 5,
  "duration_sec": 120
}
```

SQL 来源可以是 `sql_content` 或 `groups`，两者必须且只能提供一个；其他字段遵循现有 `TaskCreate`。MCP 建议首版使用收紧后的 schema：`concurrency: 1..100`、`spawn_rate: 1..100`、`duration_sec: 5..1800`，且限制 SQL 总长度和组数。服务端创建时再次执行 `prepare()`、校验连接归属并测试连接。成功响应：

```json
{"run_id":"abc12345","status":"pending"}
```

状态可很快转为 running，因此客户端应以 `get_sql_run` 的结果为准。

### 4. `get_sql_run`

输入 `run_id`，返回当前用户自己的任务状态和关键配置：

```json
{
  "run_id":"abc12345",
  "name":"orders 查询基准",
  "status":"running",
  "concurrency":20,
  "spawn_rate":5,
  "duration_sec":120,
  "created_at":"...",
  "started_at":"...",
  "ended_at":null,
  "error_msg":null,
  "observability":{"request_count":1200,"request_failures":2}
}
```

不默认回传包含敏感数据的完整 DSN，也不把任务输入中的凭据或内部文件路径带入响应。

### 5. `get_sql_run_metrics`

查询已采集时间序列。输入支持 `run_id`、可选 `since_ts`、`limit`（默认 120，最大 600）。返回采样点中的 `ts/qps/avg_ms/p95_ms/p99_ms/err_rate/threads_running/threads_connected` 等字段，以及指标范围和缺失情况。MCP 不使用 SSE 长连接；agent 按需轮询，并遵守合理轮询间隔（建议至少 2 秒）。

### 6. `stop_sql_run`

停止当前用户自己的 pending/running 任务。已完成任务返回当前状态；不存在或不属于该用户统一返回 404，避免泄露任务是否存在。

### 7. `get_sql_run_report`

仅在任务结束且报告已生成时读取 L0/L1 结构化结果；报告生成中返回 `status: "not_ready"`。输出包括总体指标、逐 SQL 指标、L1 verdict/findings 和数据完整性提示。MCP 不返回服务器本地 markdown 文件路径。若需要 Markdown，可后续新增受控文本字段或专用导出 tool，并限制响应大小。

## 六、任务状态与生命周期

```text
pending -> running -> finished
                    -> failed
                    -> cancelled
```

- MCP 创建遵循现有 `runner.start()` 流程，返回 `run_id` 后不保持长调用。
- 完成状态包括 `finished/failed/cancelled`；`failed` 同时返回安全化错误摘要。
- 成功/失败/取消后，沿用 runner 的最终指标归档和报告生成流程。
- MCP 在短时轮询窗口内读取实时状态和指标，不要求 agent 高频轮询至整个长任务结束。
- 同一用户同时运行数受配置控制；创建超过上限时返回稳定的 `RUN_LIMIT_REACHED` 错误，不排队启动隐藏负载。

## 七、Agent 调用流程与操作约束

```text
明确用户压测意图
  -> list_my_connections
  -> 选择用户指定的目标连接
  -> preview_sql_run
  -> 展示/复述 SQL、并发、升压速率和时长
  -> 必要时确认高负载或生产目标
  -> create_sql_run
  -> get_sql_run / get_sql_run_metrics（低频轮询）
  -> stop_sql_run（用户要求或需中止时）
  -> get_sql_run_report
```

Agent 行为规则：

- 用户明确要求执行压测才创建任务；“帮我看看/准备一下”只做预览。
- 不自行猜测目标数据库或把压测指向生产连接。
- 不因 SQL 看起来是 `SELECT` 就推断无负载风险；依然是真实目标库请求。
- 对高并发、长时长、可能写入的 SQL，在创建前说明影响并取得明确确认。
- 不使用 MCP 构造 `DROP/TRUNCATE/ALTER` 等破坏性 SQL；服务端策略默认拒绝高风险语句。若未来允许写压测，应采用单独显式开关、隔离目标库和额外确认策略。
- 创建后告知用户 `run_id` 和当前状态；完成后总结指标、错误和报告结论，不只报告任务已结束。

## 八、安全、资源与错误处理

1. 身份固定在 MCP server 上下文，不能从 tool 参数覆盖。
2. 所有任务相关查询与操作按 `owner_user_id` 过滤。
3. 仅允许已保存连接；不暴露密码、DSN、密钥或连接快照。
4. SQL 按 `prepare()` 现有语法与占位符规则解析，禁止任意脚本执行；模板值仍走参数绑定。
5. 生产环境建议通过连接标签或用户维护的环境标记加入生产连接防护；没有可靠标记字段前，Agent 必须要求用户指定目标，不自行选择。
6. 任务并发数、持续时间、SQL 长度、groups 数、单用户运行数及 MCP 响应指标数均设硬上限。
7. 停止只作用于当前用户任务；不可用 `run_id` 推断其他用户的任务状态。
8. 错误返回稳定 code 与简短 message；堆栈、DSN、密码、完整内部路径仅写服务端受控日志且进行脱敏。
9. 为 MCP 创建、停止和失败事件保留审计字段：user_id、run_id、连接 ID、参数摘要、调用时间和结果；禁止记录密码。
10. 为压测服务加速率限制，防止快速连续创建任务。

建议错误码：`INVALID_SQL`、`INVALID_LOAD_PROFILE`、`CONNECTION_NOT_FOUND`、`CONNECTION_FAILED`、`RUN_NOT_FOUND`、`RUN_LIMIT_REACHED`、`RUN_NOT_STOPPABLE`、`REPORT_NOT_READY`。

## 九、与当前代码的对应关系

| MCP 能力 | 当前实现 | 接入改造 |
|---|---|---|
| `list_my_connections` | `app.services.datagen_jobs.list_my_connections` | 可抽到共享 connection service，或首版复用 |
| `preview_sql_run` | `app.services.run_form.preview` / `/api/runs/preview` | 包装为 user-aware service，保持只做静态校验 |
| `create_sql_run` | `app.routers.tasks.create_run`、`app.services.runner` | 下沉业务逻辑；增加 owner_user_id 与限额 |
| `get_sql_run` | `app.routers.tasks.get_run`、`app.services.observability.build_snapshot` | 带用户归属的服务方法 |
| `get_sql_run_metrics` | `app.db.get_metrics`、`metrics` 表 | 先校验 run 归属，限制时间窗和返回点数 |
| `stop_sql_run` | `/api/runs/{run_id}/stop`、`runner.stop` | HTTP 与 MCP 共用归属检查 |
| `get_sql_run_report` | `app.services.report.generate_report`、`reports` 表 | 带用户归属读取 JSON，隐藏本地文件路径 |

建议新增：

```text
app/services/run_jobs.py
app/mcp/run_server.py
app/mcp/run_tools.py
tests/unit/test_run_mcp.py
```

共享 FastMCP server 可沿用现有 `app/mcp/server.py` 入口并增加压测工具，或按独立 server 部署。首选先扩展同一 server，以保持单一认证上下文；若工具体量、部署权限或用户确需隔离，再拆分独立 `run_server.py`。

## 十、实施阶段

### 阶段一：归属与 service

1. 给 `runs` 增加 `owner_user_id` 和迁移逻辑。
2. 抽取创建、状态、指标、停止、报告服务，HTTP 路由改为调用服务。
3. 对无归属历史 run 制定不可通过普通用户 API/MCP 访问的兼容行为。
4. 加入每用户活动任务限额和审计事件。

### 阶段二：MCP tools

1. 将压测 tools 注册到现有 FastMCP server。
2. 实现预览、创建、状态、指标、停止、报告工具。
3. 仅使用绑定用户的已保存连接。
4. 限制 MCP 的负载范围和响应规模，并实现稳定错误结构。

### 阶段三：操作策略与增强

1. 添加连接环境标签和生产目标防护策略。
2. 增加可配置审批/确认策略与 MCP 审计查询。
3. 评估授权的写压测、专用隔离库和高级 Locust profile；不可将任意脚本作为普通 MCP 参数开放。
4. 需要多用户远程 MCP 时，复用造数 MCP 的 token/OAuth 认证适配。

## 十一、验收标准

1. MCP 可列出当前用户连接，且响应无密码和 DSN。
2. 预览 tool 不连接目标数据库、不创建 run。
3. MCP 创建任务使用 Web 相同的 SQL 解析、占位符校验和 runner。
4. MCP 无法创建任意 DSN 任务，负载超出上限时服务端拒绝。
5. 用户不能读取、停止或获取其他用户的 run、指标和报告；HTTP API 同样受保护。
6. 创建返回 `run_id`，状态、实时指标、停止和最终报告均可按归属查询。
7. 报告未生成时返回明确的 `not_ready`，任务结束后提供 L0/L1 结构化结果。
8. 响应与日志不泄漏数据库密码、完整 DSN、内部文件路径或堆栈。
9. 活动任务数、轮询返回点数和创建速率限制可配置并有明确错误反馈。
10. 现有页面创建压测、监控、停止和报告行为保持一致。
