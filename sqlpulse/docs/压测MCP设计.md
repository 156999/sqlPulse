# 压测 MCP 能力差距与设计

## 1. 结论

当前仓库已有完整的 Web 压测执行链路和独立的造数 MCP server，但没有压测 MCP tools。压测核心可复用，不需要重写 Locust、指标采集或报告逻辑。新增一层受控 MCP adapter 和归属校验即可完成第一版。

估算：MVP 约 6 项功能改造（其中 4 项必须，2 项建议）；完整可运营版本约 10 项。当前已有的表单预览、任务执行、状态快照、停止、指标查询、报告生成可复用，但需要由 MCP 暴露。

## 2. 现状盘点

| 能力 | 当前状态 | MCP 缺口 |
|---|---|---|
| MCP 协议服务 | `app/mcp/server.py` 注册 10 个造数工具 | 新增压测工具集及 server 启动入口；不要把两类工具无条件混在一起 |
| 身份与连接 | 造数 MCP 固定 `user_id`/`username`，只允许保存连接 | 可复用身份解析；压测创建也必须拒绝任意 DSN，校验连接归属 |
| 压测输入 | `TaskCreate`、`prepare()` 支持 SQL 文本/分组/变量及并发、ramp-up、时长 | 建立 MCP 友好的 schema，明确限制并调用同一 `prepare()` 校验 |
| 执行 | `/api/runs` 创建记录并启动 `runner` | 抽出可复用的 run application service，供 HTTP/MCP 共用 |
| 所有权 | `runs` 当前没有 `owner_user_id`；停止、状态、指标、报告路由未统一按用户授权 | 必须新增 run 归属和所有读写路径授权（MVP 核心缺口） |
| 监控 | `/api/runs/{id}` 快照、metrics、事件和 per-SQL 数据 | MCP 提供状态及最终/当前指标读取；不建议 MCP 长轮询 SSE |
| 停止 | `runner.stop(run_id)` 可停止运行任务 | 通过 owner 校验后暴露 stop tool |
| 报告 | 自动生成 Markdown，HTTP 可下载 | MCP 返回结构化 L0/L1 与报告文本/摘要 |
| SQL 预览 | `/api/runs/preview` 校验表单，连接不执行 SQL | 暴露 preview/validate tool，创建前调用 |
| 审计与限额 | 有基础任务参数校验；无 MCP 专属审计/配额策略 | 建议加入调用来源、参数上限、并发策略和审计事件 |

## 3. 需要补齐的功能

### MVP 必须（4 项）

1. **压测任务归属**：给 `runs` 增加 `owner_user_id`（旧记录可为空，按策略只允许管理员/本地兼容访问）；创建时写入用户，查询、停止、指标、事件、逐 SQL 指标、报告、EXPLAIN 及 AI 诊断入口统一校验归属。优先封装 `require_owned_run(user_id, run_id)`，避免某个旁路接口遗漏。
2. **共享任务服务**：把 `tasks.py` 创建路径的解析、连接解析/验证、快照加密、落库和 runner 启动整理为 `app/services/run_jobs.py`；HTTP 与 MCP 复用，HTTP 行为保持兼容。
3. **压测 MCP tools**：最小工具集为 `list_my_connections`、`preview_run`、`create_run`、`get_run`、`get_run_metrics`、`stop_run`、`get_run_report`。列表连接工具可从造数模块复用，但共享身份与权限逻辑。
4. **安全边界和参数约束**：MCP 创建只接受 `connection_id`；复用任务表单校验；并发、持续时间、SQL 字符数、组数等配置设置硬上限；禁止返回 DSN/密码；访问任务必须 owner 匹配。

### MVP 建议（2 项）

5. **轻量审计**：记录 user、工具名、run_id、时间、结果类别和耗时；不记录 DSN、凭证或完整敏感 SQL。
6. **工具使用说明/工作流**：MCP instructions 明确先预览再启动、创建后轮询、结束后取报告；工具返回建议的轮询间隔与终态定义。

### 完整版追加（4 项）

7. Personal access token、过期/撤销、最小权限 scope；当前固定用户启动参数不适合作为多租户远程服务认证。
8. 幂等键与重复提交保护、按用户并发任务额度、全局资源保护。
9. 任务列表/历史检索、分页和过滤。
10. 结构化失败分类与 MCP 端诊断上下文（失败日志只返回受限尾部并脱敏）。

## 4. 建议架构

```text
MCP client -> MCP auth/context -> run_jobs service -> db + runner + observability/report
                                      ^
                                      |
                                 HTTP router
```

新增建议文件：

```text
app/mcp/run_server.py       压测 MCP tools/schema
app/mcp/run_tools.py        用户上下文适配及安全输出
app/services/run_jobs.py   共享压测任务用例
```

可先在现有 `app/mcp/server.py` 加载工具，或提供 `python -m app.mcp.run_server` 独立入口。推荐先独立入口，避免现有造数 MCP 客户端工具面和权限发生意外变化；后续可再提供统一 server。

## 5. MCP 工具契约草案

| 工具 | 输入 | 输出/行为 |
|---|---|---|
| `list_my_connections` | 无 | 当前用户连接的 id/name/host/database 等安全字段，不含密码 |
| `preview_run` | `connection_id`、`sql_content` 或 `groups`、`variables` | 复用 `prepare/preview`，返回校验错误、解析后的 SQL 组和执行摘要；不触库执行 |
| `create_run` | `connection_id`、`name`、SQL 或 groups、variables、concurrency、spawn_rate、duration_sec | 服务端复验、连接测试、记录 owner、启动任务，立即返回 `run_id/status` |
| `get_run` | `run_id` | 安全任务状态、配置摘要、错误摘要、起止时间、终态指标；不含 DSN |
| `get_run_metrics` | `run_id`、可选时间范围 | 返回压缩后的时序指标或聚合统计，限制点数并支持分页/窗口 |
| `stop_run` | `run_id` | 只停止当前用户的运行任务，返回状态 |
| `get_run_report` | `run_id` | 终态时生成/读取报告，返回 L0、L1 和 Markdown；运行中返回明确未完成状态 |

输入兼容现有 `TaskCreate`：文本模式采用 `sql_content`，表单模式采用 `groups + variables`，拒绝两者混用。MCP 不提供任意 DSN、EXPLAIN 探测选项或任意本地文件路径。

## 6. 权限改造要点

- `runs` 增加 `owner_user_id TEXT`，迁移不删除历史任务。
- 写入用户归属必须在 HTTP/MCP 共用创建服务中完成。
- `stop_run`、`get_run`、metrics/events/stream、per-sql metrics、report/download、explain、AI diagnose 与其轮询接口均需鉴权并检查归属。
- 未授权任务统一表现为 404，避免枚举 run_id。
- 当前部分任务路由没有 `require_user`，因此即使只加 MCP 端权限也不够；必须补齐 HTTP 路由，否则可绕开 MCP 的归属控制。
- 旧历史行 owner 为空时建议仅由管理员可读；若产品需要用户继续访问旧任务，需定义明确迁移归属来源，不能猜测任务所有人。

## 7. 验收标准

1. MCP 可预览、创建、跟踪、停止并读取压测报告，且使用现有 Locust 执行与观测结果。
2. 用户只能列出和操作自己的压测任务；通过 HTTP 及 MCP 都无法跨用户读取、停止或下载报告。
3. MCP 调用不能提交任意 DSN，也不能从任何返回内容取得数据库密码。
4. 越界负载在启动前被拒绝；预览不启动任务；创建响应快速返回 `run_id`。
5. 任务运行中可查状态/有限窗口指标，终态可查最终指标和报告。
6. 现有 Web 压测流程与造数 MCP 回归行为保持一致。

## 8. 实施顺序与工作量

建议按 4 个工作包实施，约 6 个功能项：

1. **权限底座（2 项）**：schema migration、run owner service、统一授权和所有 HTTP 路由补齐。
2. **共享任务用例（1 项）**：提取 `run_jobs`，保持 `/api/runs` 兼容。
3. **MCP 最小闭环（2 项）**：preview/create/status/metrics/stop/report tools、独立启动入口及工具说明。
4. **运行治理（1 项）**：硬上限、审计、指标返回裁剪、跨用户与参数边界验收。

完整版另加 4 项：PAT 授权、历史列表、幂等/配额、脱敏诊断。以上是按代码现状的功能拆分估算，实际人日取决于对历史无 owner 任务的兼容政策和远程认证需求。
