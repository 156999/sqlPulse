# SQL Pulse（MVP）

SQL 压测 + 造数 + 实时可视化 + L0/L1 报告的一站式本地工具：粘贴/上传 SQL → 设置并发与时长 → 实时看 QPS / P99 / 连接数曲线 → 结束自动生成指标报告与规则建议（Markdown 可下载）。

> 完整服务器部署步骤见 [LINUX_DEPLOY.md](LINUX_DEPLOY.md)。
> MySQL Docker 部署与外部连接见 [MYSQL_DEPLOY.md](MYSQL_DEPLOY.md)。

## 5 分钟上手

### 方式一：腾讯云 Ubuntu 一键部署（推荐）

```bash
bash deploy.sh
```

脚本会自动安装 Docker 与 Compose 插件（若缺失）、生成 `.env` 并写入随机 `APP_SECRET_KEY`、构建并后台启动全部容器，最后等待健康检查通过。

腾讯云服务器上还需要：

1. 在云控制台安全组放行 `8080`（若改过 `WEB_PORT` 则放行对应端口）；示例库 MySQL 的宿主机端口 `3307` 不需要对公网放行
2. 打开 `http://<服务器公网IP>:8080`，首次启动用测试账号 `root / root` 登录
3. 首次启动时 compose 自动建库 `sqlpulse_demo` 并执行 [scripts/seed_demo.sql](scripts/seed_demo.sql) 造数
4. 对外部署前请改 `.env` 中的 `MYSQL_ROOT_PASSWORD`；有 HTTPS 反向代理时设 `AUTH_COOKIE_SECURE=true`，不需要开放注册时设 `AUTH_ALLOW_REGISTER=false`

数据存储在 Docker 命名卷中：容器随服务器重启自动拉起，`docker compose down` 不会丢数据，只有 `docker compose down -v` 才会清空并重新初始化。
后续更新代码后，在项目目录重新执行 `bash deploy.sh` 即可重建并更新容器。

### 方式二：本地 docker compose 启动

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"  # 生成 APP_SECRET_KEY 并填入 .env
docker compose up -d --build
```

- 打开 http://localhost:8080，首次启动可直接用测试账号 `root / root` 登录
- 宿主机已占用 3306 的情况下不冲突：compose 的 MySQL 默认映射到宿主机 **3307**（web 容器走内部网络访问 mysql:3306 不受影响）
- 结束后 `docker compose down`；加 `-v` 会连同命名卷一起清空并重新执行 seed 脚本

### 方式三：本地 Python 运行（被测库自备或复用已起的 MySQL）

```bash
# Python 3.12
pip install -r requirements.txt
cp .env.example .env          # TARGET_DB_* 指向你的 MySQL
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成 APP_SECRET_KEY 并填入 .env
mysql -h 127.0.0.1 -u root -p < scripts/seed_demo.sql   # 初始化示例库
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

打开 http://localhost:8080，首次启动可用 `root / root` 登录；也可以进入“注册”创建新账号。

## 认证

- `AUTH_ENABLED=true`（默认）时，业务页面和 `/api/*` 均需登录；未登录页面跳转 `/login`，未登录 API 返回 `401`。
- `AUTH_ENABLED=false` 可完全放行，仅建议在隔离的本地或演示环境使用。
- 密码使用 Argon2 哈希，会话为 Starlette 签名 Cookie（默认 7 天），服务重启后会话仍有效。
- 生产环境必须设置强随机 `APP_SECRET_KEY`，必要时开启 `AUTH_COOKIE_SECURE=true`。

## 连接管理

- 侧边栏“连接管理”可创建、编辑、删除、测试自己的 MySQL 连接，同一用户名下不能重复保存同名连接。
- 压测页和造数页都支持“已保存连接 / 临时连接”两种模式：已保存连接提交 `connection_id`，临时连接继续提交 `db_dsn`。
- 后端解析出完整 DSN 后写入任务快照；之后修改或删除连接不影响历史任务的展示与执行。
- 连接列表与详情接口不返回密码，编辑时密码留空表示保持原密码；浏览器 `localStorage` 也只记忆连接选择，不再保存明文密码。
- 连接密码按 V1 设计以明文存于本地 SQLite（与历史任务快照一致），加密存储留给 V2。

> Chart.js / HTMX 已内置到 `app/static/vendor/`，部署不依赖公网 CDN。

## 使用流程

1. 新建压测页填写：任务名、目标库（点"测试连接"验证）、SQL（粘贴或上传 `.sql`）、并发/ Ramp-up / 时长
   - 目标库可在“已保存连接”和“临时连接”之间切换；已保存连接在“连接管理”页维护，临时连接保持原有 DSN 表单
   - DB 表单与 SQL 会自动记忆（浏览器 localStorage，密码除外），下次打开自动回填
   - 没有现成 SQL？点 **"载入示例 SQL"** 一键填入基于示例库的演示脚本（权重 + 事务 + 慢查询混合），源文件在 [app/static/examples/demo.sql](app/static/examples/demo.sql)
2. SQL 格式：分号分隔；`-- weight: N` 行注释指定权重（缺省 1，注释后可跟说明文字）；同一权重段内多条语句按顺序执行（可放事务）
3. 提交后跳转监控页：QPS+错误率（次/秒 / %）、avg/P95/P99（ms）、Threads_running/connected（个）三张实时图（SSE，1s 粒度），并可展开查看本次提交的 SQL
4. 停止或跑完后进报告页：L0 指标 + 分语句统计 + L1 规则建议 + 已提交 SQL，可下载 `sqlpulse_report_<run_id>.md`

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

## 测试

```bash
pytest tests/unit -v            # 单测：SQL 解析 / 规则引擎 / 模板渲染（无外部依赖）
pytest tests/integration -v     # 集成：需 app 在 :8080 运行 + MySQL 在 127.0.0.1:3306
```

## 目录速览

```
app/
  main.py            FastAPI 入口（healthz / lifespan 启动 collector）
  config.py          pydantic-settings 读 .env
  auth.py            密码哈希、当前用户、统一鉴权依赖
  db.py              SQLite schema（runs / metrics / reports / datagen_jobs / users / mysql_connections）
  routers/           auth / pages / connections / datagen / tasks / monitor(SSE) / reports
  services/          runner(LocustRunner) / datagen / connection_manager / sql_executor / script_runner / collector / report / rule_engine
  locust_tpl/        sql_user.py.j2 locustfile 模板
  ai/ mcp/           Sprint 2-3 预留桩
tests/               unit / integration / assets(good|slow|bad.sql)
scripts/seed_demo.sql  示例库建库造数（orders 1w / stock 500 / access_log 5w）
```

数据落盘：本地运行在 `data/`（SQLite + locustfile + locust CSV）、`logs/app.log`、`reports/<run_id>/`；docker compose 部署时对应目录为命名卷 `sqlpulse_data` / `sqlpulse_logs` / `sqlpulse_reports`。
