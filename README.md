# SQL Pulse（MVP）

SQL 压测 + 造数 + 实时可视化 + L0/L1 规则报告 + L2 AI 诊断的一站式本地工具：粘贴/上传 SQL → 设置并发与时长 → 实时看 QPS / P99 / 连接数曲线 → 结束自动生成指标报告与规则建议（Markdown 可下载），并可进一步让 AI 模型结合报告、时序与执行计划给出诊断。

> 完整服务器部署步骤见 [LINUX_DEPLOY.md](LINUX_DEPLOY.md)。
> MySQL Docker 部署与外部连接见 [MYSQL_DEPLOY.md](MYSQL_DEPLOY.md)。

## 5 分钟上手

### 方式一：腾讯云 Ubuntu 一键部署（推荐）

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"                             # APP_SECRET_KEY
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # SQLPULSE_CREDENTIAL_KEY
# 把两个输出分别填入 .env 后执行：
bash deploy.sh
```

脚本会自动安装 Docker 与 Compose 插件（若缺失）、复用 `.env` 中已填写的密钥、构建并后台启动全部容器，最后等待健康检查通过。

腾讯云服务器上还需要：

1. 在云控制台安全组放行 `8080`（若改过 `WEB_PORT` 则放行对应端口）
2. 打开 `http://<服务器公网IP>:8080`，首次启动用测试账号 `root / root` 登录
3. **目标 MySQL 自行准备**（本机、局域网、云数据库均可），登录后在「新建连接」里填 ip / port / 凭证。本仓库的 compose **不包含** MySQL，见下方「为什么 compose 里没有 MySQL」
4. 有 HTTPS 反向代理时设 `AUTH_COOKIE_SECURE=true`，不需要开放注册时设 `AUTH_ALLOW_REGISTER=false`

数据存储在 Docker 命名卷中：容器随服务器重启自动拉起，`docker compose down` 不会丢数据，只有 `docker compose down -v` 才会清空。
后续更新代码后，在项目目录重新执行 `bash deploy.sh` 即可重建并更新容器。

### 为什么 compose 里没有 MySQL

被测库由使用者在页面里填（ip + port + 凭证），可以是任意一台 MySQL —— 它属于**使用者的环境**，不属于本产品的部署单元。把 MySQL 绑进 compose 会让人误以为「必须用我们发的那个库」，也让部署多等 30–60 秒的建表灌数。所以产品 compose 只有 `web`。

需要一份带演示数据的库时（本地试用、演示、跑集成测试），叠加一个附加文件即可：

```bash
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d --build
```

它会额外起一个 `mysql:8.0`，宿主端口 **3307**（`MYSQL_HOST_PORT` 可调），自动建库 `sqlpulse_demo` 并执行 [scripts/seed_demo.sql](scripts/seed_demo.sql)。`.github/workflows/sqlpulse-e2e.yml` 用的就是这套。

### 方式二：本地 docker compose 启动

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"                             # APP_SECRET_KEY
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # SQLPULSE_CREDENTIAL_KEY
docker compose up -d --build
```

两个输出分别填入 `.env` 后启动。

- 打开 http://localhost:8080（宿主端口可在 `.env` 用 `WEB_PORT` 修改），首次启动可直接用测试账号 `root / root` 登录
- 只起 SQL Pulse 本体。目标库在页面里填；想要开箱即用的演示库，把 `docker-compose.mysql.yml` 一起带上（见上）
- 若启动报 `ports are not available: ... bind: An attempt was made to access a socket in a way forbidden by its access permissions`，说明该宿主端口被占用或**被 Windows 保留**（Hyper-V/WinNAT 会保留一段动态端口，与是否有进程占用无关）：改 `.env` 的 `WEB_PORT` 换一个端口即可
- 结束后 `docker compose down`；加 `-v` 会连同命名卷一起清空。叠加了 `docker-compose.mysql.yml` 时，`-v` 还会重置演示库并重新执行 seed

### 方式三：本地 Python 运行（被测库自备或复用已起的 MySQL）

```bash
# Python 3.12
pip install -r requirements.txt
cp .env.example .env          # TARGET_DB_* 指向你的 MySQL
python -c "import secrets; print(secrets.token_urlsafe(32))"                             # APP_SECRET_KEY
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # SQLPULSE_CREDENTIAL_KEY
mysql -h 127.0.0.1 -u root -p < scripts/seed_demo.sql   # 初始化示例库
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

打开 http://localhost:8080（端口被占用或保留时可换成别的，见上），首次启动可用 `root / root` 登录；也可以进入“注册”创建新账号。

## 认证

- `AUTH_ENABLED=true`（默认）时，业务页面和 `/api/*` 均需登录；未登录页面跳转 `/login`，未登录 API 返回 `401`。
- `AUTH_ENABLED=false` 可完全放行，仅建议在隔离的本地或演示环境使用。
- 密码使用 Argon2 哈希，会话为 Starlette 签名 Cookie（默认 7 天），服务重启后会话仍有效。
- 生产环境使用强随机 `APP_SECRET_KEY`（会话签名、AI key 加密）与 `SQLPULSE_CREDENTIAL_KEY`（连接密码加密），有 HTTPS 时开启 `AUTH_COOKIE_SECURE=true`。

## 连接管理

- 侧边栏“连接管理”可创建、编辑、删除、测试自己的 MySQL 连接，同一用户名下不能重复保存同名连接。
- 压测页和造数页都支持“已保存连接 / 临时连接”两种模式：已保存连接提交 `connection_id`，临时连接继续提交 `db_dsn`。
- 后端解析出完整 DSN 后写入任务快照；之后修改或删除连接不影响历史任务的展示与执行。
- 连接列表与详情接口不返回密码，编辑时密码留空表示保持原密码；浏览器 `localStorage` 也只记忆连接选择，不再保存明文密码。
- 连接密码使用 Fernet 加密后存入本地 SQLite（`password_ciphertext`），密钥来自 `SQLPULSE_CREDENTIAL_KEY`；历史明文数据启动时自动迁移。更换密钥后，已有连接密码需要重新填写。

> Chart.js / HTMX 已内置到 `app/static/vendor/`，部署不依赖公网 CDN。

## 使用流程

1. 新建压测页填写：任务名、目标库（点"测试连接"验证）、SQL（粘贴或上传 `.sql`）、并发/ Ramp-up / 时长
   - 目标库可在“已保存连接”和“临时连接”之间切换；已保存连接在“连接管理”页维护，临时连接保持原有 DSN 表单
   - DB 表单与 SQL 会自动记忆（浏览器 localStorage，密码除外），下次打开自动回填
   - 没有现成 SQL？点 **"载入示例 SQL"** 一键填入基于示例库的演示脚本（权重 + 事务 + 慢查询混合），源文件在 [app/static/examples/demo.sql](app/static/examples/demo.sql)
2. SQL 格式：分号分隔；`-- weight: N` 行注释指定权重（缺省 1，注释后可跟说明文字）；同一权重段内多条语句按顺序执行（可放事务）
3. 提交后跳转监控页：QPS+错误率（次/秒 / %）、avg/P95/P99（ms）、Threads_running/connected（个）三张实时图（SSE，1s 粒度），并可展开查看本次提交的 SQL
4. 停止或跑完后进报告页：L0 指标 + 分语句统计 + L1 规则建议 + 已提交 SQL，可下载 `sqlpulse_report_<run_id>.md`

## AI 诊断（L2）

- 报告页可对已完成的压测发起 AI 诊断：模型结合 L0/L1、逐秒时序、失败明细与 EXPLAIN 计划给出结论，结果经证据引用校验后展示。
- 在「用户信息」页按厂商配置 API Key（Fernet 加密存储，页面只显示掩码）并选择模型：
  - DeepSeek：`deepseek-flash` / `deepseek-v4-pro`
  - 阿里云百炼 Qwen：`qwen3.8-flash`
  - 小米 MiMo：`mimo-v2.6-flash`
- 用户自填 key 优先；未填写时仅 DeepSeek 可使用服务器默认 `LLM_API_KEY`。费用由对应厂商的 key 承担。
- 需要读取目标库表结构（列 / 索引 / 表规模，只读）时设置 `AI_DB_PROBE_ENABLED=true`；DSN 只从任务快照读取，不会外发。
- 命令行验证：`python -m app.ai.verify_ai_tools --run-id <id>`（工具与证据） / `python -m app.ai.verify_llm_client --provider deepseek`（客户端连通性）。

## 压测表单与模式转换

- 每个卡片是一组 SQL：可命名、折叠、复制、删除并撤销，实时显示预计调度占比。
- 执行方式为“逐条提交”或“事务执行”。事务组自动管理外层 BEGIN / COMMIT；粘贴完整事务可识别，复杂事务控制保留在文本模式。
- 命名变量通过表格选择生成类型并填写参数，支持 8 种生成函数。先在 SQL 编辑框放置光标，再点击“插入占位符”。
- “校验与预览”不连接数据库、不执行 SQL，展示每组示例变量、参数化 SQL 和类型化参数。修改配置后预览失效。
- 文本和文件仅作为导入入口：先“解析并预览”，检查分组、权重、事务方式及缺失变量，再追加或替换任务组表单。支持撤销。不能完整转换的脚本保留原文，必须修改后重新导入，不能直接启动压测。
- 页面只有任务组表单能“开始压测”，统一提交 `groups + variables`；导入区与现有任务组独立保存，切换入口不会自动覆盖表单。
- 旧 API 的 `sql_content + variables` 继续可用；表单使用 `groups + variables`，两种 SQL 来源不可混用。预览接口为 `POST /api/runs/preview`。

设计及边界详见 [表单优化](sqlpulse/docs/表单优化.md)。

## 占位符参数

压测 SQL 支持以下占位符，由每个虚拟用户在**每次执行前**随机替换（不是整场压测只固定一次）。语法风格参考 JMeter 的 `${__Random}`、k6 随机函数等业界做法：随机性尽量贴近真实请求，同一并发下不同请求携带不同参数。

| 占位符 | 说明 | 示例 |
|---|---|---|
| `{{rand(a,b)}}` | 整数，闭区间 `[a,b]`，支持负数 | `WHERE id = {{rand(1,10000)}}` |
| `{{randf(min,max[,digits])}}` | 定点小数，在区间内按指定精度采样，默认 2 位小数，`digits` 取 0-10 | `WHERE amount >= {{randf(10.0,500.0,2)}}` |
| `{{pick('a','b')}}` | 从 Python 字面量列表中随机取一个；字符串自动补 SQL 引号 | `WHERE sku = {{pick('SKU1','SKU2')}}` |
| `{{pickw(('a',30),('b',70))}}` | 加权随机，参数为 `(值, 权重)` 元组列表 | `path = {{pickw(('/api/x',80),('/api/y',20))}}` |
| `{{randstr(n)}}` | 随机 n 位小写字母+数字字符串，自动补引号 | `token = {{randstr(16)}}` |
| `{{randdate('start','end')}}` | 区间内随机日期 `YYYY-MM-DD`，自动补引号 | `created_at >= {{randdate('2026-01-01','2026-12-31')}}` |
| `{{randdt('start','end')}}` | 区间内随机日期时间 `YYYY-MM-DD HH:MM:SS`，自动补引号 | `updated_at <= {{randdt('2026-01-01 00:00:00','2026-12-31 23:59:59')}}` |
| `{{uuid()}}` | 随机 UUID v4 字符串，自动补引号 | `request_id = {{uuid()}}` |

占位符仅用于数据值，外层不要加引号。值通过 PyMySQL 参数绑定处理，`pick` / `pickw` 保留候选类型，`None` 对应 `NULL`。SQL 字符串、标识符和注释里的类似文本不替换。非法函数、参数或未定义变量在启动前拒绝，不产生压测请求。

新建压测页可配置命名变量，例如 `order_id = rand(1,10000)`，SQL 中通过 `{{var('order_id')}}` 引用。每次任务执行开始时生成一次，同一事务块或权重分组内复用；下次执行重新生成。普通随机占位符仍各自独立采样。API 使用 `variables: {"order_id": "rand(1,10000)"}`。`randstr` 长度限 1～4096，随机字符串不保证唯一。

完整规则与示例见 [占位符规则](sqlpulse/docs/占位符规则.md)。

## 造数

造数 SQL 与压测共用同一套占位符和参数规则。SQL 模式提供“INSERT 生成条数”，每条 SQL 按指定次数执行；每次执行重新生成直接占位符，命名变量在同一次 INSERT 内复用。造数提交前会校验占位符和变量配置，非法内容不会启动任务。Shell 模式仍由脚本自行控制循环和参数。


1. 侧边栏进入“造数”：填写任务名和目标库，测试连接后选择 SQL 或 Shell 方式
   - 目标库同样支持“已保存连接 / 临时连接”两种模式，与压测页共用连接管理
2. SQL 支持粘贴/上传 `.sql`，也支持 `DELIMITER` 与存储过程体；执行后累计 `rows_affected`
3. Shell 支持粘贴/上传 Bash 脚本，DSN 通过 `TARGET_DB_*` 环境变量注入；脚本写 `result.json` 可上报影响行数
4. Shell 默认由 `DATAGEN_SCRIPT_EXECUTION_ENABLED` 开关控制（默认 `false`），任务有独立日志、停止和重启恢复

## 造数 MCP

- `app/mcp` 是基于 FastMCP 的造数 server，提供 10 个工具：已保存连接与表元数据、规则校验/应用、依赖补数计划、SQL 造数任务的创建/查询/日志/停止。
- 只接受已保存连接的 `connection_id`，不返回数据库密码，不暴露 Shell 造数。
- 本地 stdio 启动（供桌面 MCP 客户端使用）：

  ```bash
  python -m app.mcp.server --user-id <user_id> --transport stdio
  ```

  用户上下文也可用 `MCP_USER_ID` / `MCP_USERNAME` 环境变量指定。
- 支持 `stdio` / `sse` / `streamable-http` 三种传输；桌面 MCP 客户端通常使用 `stdio`，HTTP 传输可通过 `--host` / `--port` 指定监听地址。

## 测试

```bash
pytest tests/unit -v            # 单测：SQL 解析 / L1 / 造数等，无外部依赖
pytest tests/integration -v     # 集成：需 app 在 :8080 运行，并设置 TEST_DB_* / TEST_MYSQL_*（参照 .github/workflows/sqlpulse-e2e.yml）
for f in tests/frontend/*.test.cjs; do node "$f"; done   # 前端模板脚本测试（Node 18+）
```

## 目录速览

```
app/
  main.py            FastAPI 入口（healthz / lifespan 启动 collector）
  config.py          pydantic-settings 读 .env
  auth.py            密码哈希、当前用户、统一鉴权依赖
  db.py              SQLite schema（runs / metrics / reports / sql_metrics / datagen_jobs / users / mysql_connections / ai_keys / ai_prefs / ai_jobs）
  routers/           auth / pages / connections / datagen / tasks / monitor(SSE) / reports / explain / ai
  services/          runner / collector / report / l1 / observability / explain_probe / sql_history / datagen_* / connection_manager / sql_executor / script_runner
  locust_tpl/        sql_user.py.j2 locustfile 模板
  ai/                L2 诊断：工具、模型客户端、多厂商注册表、诊断编排
  mcp/               造数 MCP server（FastMCP）
tests/               unit / integration / frontend / assets(good|slow|bad.sql)
scripts/seed_demo.sql  示例库建库造数（orders 1w / stock 500 / access_log 5w）
```

数据落盘：本地运行在 `data/`（SQLite + locustfile + locust CSV）、`logs/app.log`、`reports/<run_id>/`；docker compose 部署时对应目录为命名卷 `sqlpulse_data` / `sqlpulse_logs` / `sqlpulse_reports`。
