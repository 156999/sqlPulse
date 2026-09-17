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

> Chart.js / HTMX 已内置到 `app/static/vendor/`，部署不依赖公网 CDN。

## 使用流程

1. 新建压测页填写：任务名、目标库（点"测试连接"验证）、SQL（粘贴或上传 `.sql`）、并发/ Ramp-up / 时长
   - DB 连接与 SQL 会自动记忆（浏览器 localStorage），下次打开自动回填
   - 没有现成 SQL？点 **"载入示例 SQL"** 一键填入基于示例库的演示脚本（权重 + 事务 + 慢查询混合），源文件在 [app/static/examples/demo.sql](app/static/examples/demo.sql)
2. SQL 格式：分号分隔；`-- weight: N` 行注释指定权重（缺省 1，注释后可跟说明文字）；同一权重段内多条语句按顺序执行（可放事务）
3. 提交后跳转监控页：QPS+错误率（次/秒 / %）、avg/P95/P99（ms）、Threads_running/connected（个）三张实时图（SSE，1s 粒度），并可展开查看本次提交的 SQL
4. 停止或跑完后进报告页：L0 指标 + 分语句统计 + L1 规则建议 + 已提交 SQL，可下载 `sqlpulse_report_<run_id>.md`

## 造数

1. 侧边栏进入“造数”：填写任务名和目标库，测试连接后选择 SQL 或 Shell 方式
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
  db.py              SQLite schema（runs / metrics / reports / datagen_jobs / users）
  routers/           auth / pages / datagen / tasks / monitor(SSE) / reports
  services/          runner(LocustRunner) / datagen / sql_executor / script_runner / collector / report / rule_engine
  locust_tpl/        sql_user.py.j2 locustfile 模板
  ai/ mcp/           Sprint 2-3 预留桩
tests/               unit / integration / assets(good|slow|bad.sql)
scripts/seed_demo.sql  示例库建库造数（orders 1w / stock 500 / access_log 5w）
```

数据落盘：本地运行在 `data/`（SQLite + locustfile + locust CSV）、`logs/app.log`、`reports/<run_id>/`；docker compose 部署时对应目录为命名卷 `sqlpulse_data` / `sqlpulse_logs` / `sqlpulse_reports`。
