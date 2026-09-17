# SQL Pulse 功能实现情况对照清单

- 对照文档：CS5351_SQLBoys_TeamRoster_ProjectOverview.pdf（SQL Boys 团队立案书，2026-09-10）
- 代码基线：commit `affc8f0`（first commit）
- 整理日期：2026-09-17
- 统计：已实现 14 项 / 部分实现 6 项 / 未实现 10 项

## 前提说明

立案书 4.2 节与技术栈第 5 节写的压测引擎是 sysbench，当前实现已改为 Locust + pymysql。
本清单不把引擎差异计为功能缺口，只按功能层面对照。

## 一、已实现

| 立案书功能点 | 章节 | 实现情况 |
|---|---|---|
| 被测 SQL 两种输入：粘贴 / 上传 `.sql` | 4.1 | 都有，另加表单模式（权重、事务、占位符参数） |
| 配置目标库连接、并发、时长 | 4.1 | Web UI 完整支持，含“测试连接”校验 |
| 并发驱动 SQL 执行 | 4.2 | Locust 子进程 + pymysql 长连接，并发上限 500 |
| 每秒采样关键指标 | 4.2 | QPS、延迟分位（avg/P50/P95/P99）、错误率、Threads_running/connected、慢查询、行锁等待、磁盘临时表 |
| 实时推送 Web UI 图表 | 4.2 | SSE 1s 推送，Chart.js 五张图：QPS+错误率、汇总延迟、分语句延迟、连接数、引擎状态 |
| 采样与日志本地持久化 | 4.2 | SQLite `metrics` 表 + `logs/app.log` + `logs/locust_<run_id>.log` + Locust CSV |
| 可下载报告（表格部分） | 4.3 | Markdown 报告含任务参数、L0 指标表、分语句统计表 |
| 规则引擎诊断（默认、零外部依赖） | 4.3 | 5 条规则：P99 过高、慢语句、连接接近上限、错误率超 1%、吞吐低于并发预期 |
| Web UI：配置 + 实时监控 + 报告查看下载 | 4.4 | 新建压测、历史任务、监控、报告四个页面 |
| REST API：提交、启动、停止、查状态、下载报告 | 4.4 | `POST /api/runs`（提交即启动）、`POST /api/runs/{id}/stop`、`GET /api/runs/{id}`、`GET /api/runs/{id}/report/download`，另有 metrics 与 per-sql-metrics 查询 |
| 实时 + 历史性能指标 | 4.5 | SSE 实时流 + SQLite 历史查询，页面刷新会回填历史曲线 |
| 规则报告作为默认诊断 | 4.5 | 已实现 |
| `docker compose up` 单命令启动，Web 在 8080 | 4.6 | 已实现，含内置 MySQL 8 示例库与健康检查 |
| 凭据不提交进代码仓库 | 4.6 | `.env`、`data/`、`logs/`、`reports/`、`secrets.key` 均已忽略 |

## 二、部分实现

| 立案书功能点 | 章节 | 缺口 |
|---|---|---|
| 指标含 TPS | 4.2 | 采集了 `Com_commit` 但未映射输出，`app/services/collector.py:17` 有键无值 |
| 报告包含“图表和表格” | 4.3 | 只有表格，Markdown 报告无图表 |
| 每次运行的可下载报告包 | 4.5 | 落盘了 `report.md` + `metrics.json`，但下载接口只给 `.md`，没有打包，也没有图表产物 |
| 结构化日志 | 4.2 | 是 loguru 文本日志，不是结构化（JSON）格式 |
| 凭据通过 Web UI 录入 | 4.6 | 数据库凭据可以；LLM API Key 无录入入口 |
| REST API 覆盖完整生命周期 | 4.4 | 五个动作都在，但缺 `GET /api/runs` 列表查询，接口也未统一 `response_model` |

## 三、未实现

| 立案书功能点 | 章节 | 当前状况 |
|---|---|---|
| 造数方式一：用户提供 Lua 脚本 | 4.1 | 无任何造数功能，只有部署期固定的 `scripts/seed_demo.sql` |
| 造数方式二：用户提供 Shell 脚本 | 4.1 | 缺失 |
| 造数方式三：用户提供 `.sql` 数据文件 | 4.1 | 缺失，现有上传入口只针对被测 SQL |
| 造数方式四：LLM 按表结构生成 INSERT | 4.1 | 缺失，`app/ai/__init__.py` 只有一行占位注释 |
| LLM 诊断（DeepSeek / GLM） | 4.3 | 缺失，报告尾部标注“L2 LLM 诊断段落（预留）” |
| LLM 调用失败自动回退规则报告 | 4.3 | 缺失，因为没有 LLM 链路，也就谈不上回退 |
| Web Settings 页面配置 LLM API Key | 4.3 / 4.6 | 全站无设置页 |
| MCP Server：`start_benchmark` / `get_metrics` / `get_report` | 4.4 | `app/mcp/__init__.py` 仅占位，无 MCP SDK 依赖 |
| MCP Server 监听 8765 端口 | 4.6 | compose 中只有 web 与 mysql 两个服务 |
| 数据库凭据与 API Key 本地加密 | 4.6 | 密码明文写入 SQLite `runs.db_dsn_json`（`app/routers/tasks.py:56`）；`cryptography` 依赖已声明但未使用 |

## 四、超出立案书的额外能力

- SQL 权重比例与事务块支持
- `{{rand(a,b)}}` / `{{pick('a','b')}}` 参数化
- 分语句 P95/P99 独立曲线
- 失败任务自动判定（全部请求失败标记为 `failed`）
- 平台重启后孤儿任务恢复
- 表单与连接信息的 localStorage 记忆
- 缓冲池命中率采集（已入库，但未上图、未进报告）

## 五、认证 V1（2026-09-17 已实现）

- 注册、登录、退出完整闭环；密码 Argon2 哈希存储。
- Starlette 签名 Cookie 会话，默认 7 天有效，多 worker 与服务重启后保持可用。
- `AUTH_ENABLED=true` 时业务页面与 `/api/*` 统一经 `require_user` 依赖保护。
- `AUTH_ENABLED=false` 时鉴权完全放行，保持原有“端口即门禁”行为。
- 首次启动自动创建测试账号 `root / root`，重复启动不覆盖账号密码。
- 注册入口可由 `AUTH_ALLOW_REGISTER` 关闭；种子账号可由 `AUTH_SEED_ROOT` 关闭。

## 六、下一步优先级建议

缺口集中在三块，正好对应代码里已预留的桩位：

1. 造数功能：`app/ai/datagen.py`，覆盖 Lua / Shell / `.sql` 数据文件 / LLM 生成四种方式
2. LLM 诊断与 Settings 页：`app/ai/llm_client.py`、`app/ai/diagnose.py`，含失败回退规则报告
3. MCP Server：`app/mcp/server.py`，暴露三个工具并监听 8765

剩下属于小工作量收尾项：TPS 指标输出、报告图表与打包下载、凭据 Fernet 加密、结构化日志、`GET /api/runs` 与统一 `response_model`。
