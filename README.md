# SQL Pulse（MVP）

SQL 压测 + 造数 + 实时可视化 + L0/L1 报告的一站式本地工具：粘贴/上传 SQL → 设置并发与时长 → 实时看 QPS / P99 / 连接数曲线 → 结束自动生成指标报告与规则建议（Markdown 可下载）。

## 5 分钟上手

### 方式一：docker compose 一键启动（推荐）

```bash
cp .env.example .env   # 按需改目标库配置（compose 内置示例库，通常不用改）
docker compose up -d --build
```

- 打开 http://localhost:8080
- compose 会同时启动被测 MySQL（mysql:8.0，首次启动自动建库 `sqlpulse_demo` 并造数）
- 宿主机已占用 3306 的情况下不冲突：compose 的 MySQL 映射到宿主机 **3307**（web 容器走内部网络访问 mysql:3306 不受影响）
- 结束后 `docker compose down`（加 `-v` 连同数据卷一起清）

### 方式二：本地 Python 运行（被测库自备或复用已起的 MySQL）

```bash
# Python 3.12
pip install -r requirements.txt
cp .env.example .env          # TARGET_DB_* 指向你的 MySQL
mysql -h 127.0.0.1 -u root -p < scripts/seed_demo.sql   # 初始化示例库
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

打开 http://localhost:8080

> Chart.js / HTMX 走 jsdelivr CDN。离线环境请自行下载到 `app/static/` 并修改 `app/templates/base.html` 引用。

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
  db.py              SQLite schema（runs / metrics / reports / datagen_jobs）
  routers/           pages / datagen / tasks / monitor(SSE) / reports
  services/          runner(LocustRunner) / datagen / sql_executor / script_runner / collector / report / rule_engine
  locust_tpl/        sql_user.py.j2 locustfile 模板
  ai/ mcp/           Sprint 2-3 预留桩
tests/               unit / integration / assets(good|slow|bad.sql)
scripts/seed_demo.sql  示例库建库造数（orders 1w / stock 500 / access_log 5w）
```

数据落盘：`data/`（SQLite + locustfile + locust CSV）、`logs/app.log`、`reports/<run_id>/`。
