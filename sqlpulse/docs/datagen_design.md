# SQL Pulse 造数功能需求与设计方案

- 状态：Draft
- 日期：2026-09-17
- 范围：造数功能需求、总体设计、执行器设计、接口与 UI、安全、测试、分期实施
- 关联文档：`sqlpulse/docs/feature_gap_analysis.md`

## 1. 背景与目标

当前项目只有部署期固定脚本 `scripts/seed_demo.sql`，没有面向目标库的运行时造数能力。`feature_gap_analysis.md` 的 4.1 节仍将 Lua 脚本、Shell 脚本、`.sql` 数据文件和 LLM 生成 INSERT 列为未实现。

由于压测引擎已经确定为 Locust + pymysql，本方案不再引入 sysbench，也不再支持 sysbench 风格 Lua 脚本。造数能力统一围绕 Locust/Python/pymysql 技术栈实现。

目标：

- 提供独立的造数任务，不把造数逻辑耦合进压测执行器。
- 支持 `.sql` 数据文件、Shell 脚本、LLM 生成 INSERT 三种方式。
- 造数任务具备状态、历史、日志、停止和失败处理能力。
- 保证 DSN 凭据不进入命令行、日志和代码仓库。

## 2. 范围

### 2.1 本期实现

- `.sql` 数据文件或文本造数。
- Shell 脚本造数。
- LLM 按表结构生成 INSERT，并通过预览、编辑、校验后执行。
- 目标库连接测试和表结构读取。
- 造数任务生命周期管理和历史列表。

### 2.2 明确取消

- Lua 脚本造数。
- sysbench 集成。
- 为 Lua 额外引入解释器或第二套脚本协议。

### 2.3 暂不实现

- 造数完成后自动发起压测任务。
- 多目标库并行造数。
- 多用户权限和审计体系。
- 面向生产环境的任意脚本沙箱。

## 3. 需求方案

### 3.1 功能需求

#### FR-1 目标库配置与连接测试

用户可以为造数任务配置目标库 DSN，包含 host、port、user、password、database。提交前可通过“测试连接”验证连通性。

#### FR-2 `.sql` 数据文件造数

用户可以粘贴 SQL，也可以上传 `.sql` 文件。系统应在目标库执行 SQL，并累计受影响行数。

#### FR-3 SQL 文件解析

SQL 执行器必须支持：

- 单行注释 `--` 和 `#`。
- 多行注释 `/* ... */`。
- 字符串和反引号标识符内的分号。
- `DELIMITER $$ ... $$` 自定义分隔符。
- `CREATE PROCEDURE` 等包含分号的过程体。

#### FR-4 Shell 脚本造数

用户可以粘贴或上传 Bash 脚本。系统应通过独立子进程执行脚本，并将 DSN 通过环境变量注入。

#### FR-5 LLM 生成 INSERT

用户应能：

- 读取目标库表和字段结构。
- 选择一个或多个目标表。
- 指定目标行数和可选业务描述。
- 调用 LLM 生成 SQL。
- 在页面上预览和编辑 SQL。
- 确认后执行。

#### FR-6 造数任务生命周期

造数任务应具备以下状态：

```text
pending -> running -> finished
                   -> failed
                   -> cancelled
```

用户可查看任务状态、开始时间、结束时间、影响行数和错误信息。

#### FR-7 执行日志

每个任务应有独立日志文件，前端可读取日志内容用于排查失败。

#### FR-8 取消任务

运行中的任务可以停止。SQL 执行停止当前语句后的后续执行，Shell 任务终止整个进程组。

#### FR-9 平台重启恢复

应用启动时应扫描未结束的造数任务，将 `pending` 和 `running` 标记为 `failed`，错误信息为“平台重启中断”。

#### FR-10 结果行数

SQL 模式通过 `cursor.rowcount` 累计影响行数。Shell 模式可通过可选 `result.json` 上报影响行数，未提供则保持为空。

### 3.2 非功能需求

- NFR-1 安全：密码不进入命令行，日志不记录明文密码。
- NFR-2 安全：Shell 模式默认受开关控制，避免意外暴露任意代码执行能力。
- NFR-3 可靠：脚本执行必须有超时，避免任务无限挂起。
- NFR-4 可靠：SQL 或 LLM 生成内容执行失败时，任务状态和错误信息可追踪。
- NFR-5 性能：造数在后台线程执行，不阻塞 FastAPI 请求线程。
- NFR-6 兼容：Docker 部署为主要目标；SQL 和 LLM 模式也应支持本地 Python 环境。
- NFR-7 可扩展：LLM Provider 使用统一抽象，可切换 DeepSeek、GLM 或其它 OpenAI 兼容接口。
- NFR-8 可审计：任务输入、生成 SQL 和日志落盘，便于复现和排查。

## 4. 总体设计

### 4.1 模块划分

| 模块 | 职责 |
|---|---|
| `app/services/target_db.py` | 目标库连接、连接测试、表结构读取 |
| `app/services/sql_executor.py` | SQL 文件解析和执行 |
| `app/services/script_runner.py` | Shell 子进程执行、超时、停止、日志 |
| `app/ai/llm_client.py` | DeepSeek/GLM Provider 调用 |
| `app/ai/datagen.py` | 造数任务编排和状态管理 |
| `app/routers/datagen.py` | 页面路由和 REST API |
| `app/models.py` | 造数请求和响应模型 |
| `app/db.py` | `datagen_jobs` 表及持久化函数 |
| `app/templates/datagen.html` | 造数页面 |

### 4.2 运行流程

```text
Browser
  -> POST /api/datagen/jobs
  -> DataGenExecutor 创建后台线程
  -> 根据 mode 选择执行器
  -> 写入 datagen_jobs 状态和日志
  -> 前端轮询 GET /api/datagen/jobs/{id}
```

执行器分支：

```text
mode=sql
  -> SQL 文件执行器
  -> 连接目标库
  -> 逐条执行

mode=shell
  -> Shell 执行器
  -> 写脚本到 data/datagen/<job_id>/run.sh
  -> 启动 bash 子进程

mode=llm
  -> LLM 生成器
  -> 读取表结构
  -> 调用 LLM
  -> 预览后转为 SQL 执行
```

## 5. 数据模型

在 SQLite 中新增 `datagen_jobs` 表：

```sql
CREATE TABLE IF NOT EXISTS datagen_jobs (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  mode              TEXT NOT NULL,
  status            TEXT NOT NULL,
  target_dsn_json   TEXT NOT NULL,
  input_json        TEXT NOT NULL,
  rows_affected     INTEGER,
  log_path          TEXT,
  error_msg         TEXT,
  created_at        TEXT NOT NULL,
  started_at        TEXT,
  ended_at          TEXT
);
```

字段说明：

- `mode`：`sql`、`shell`、`llm`。
- `target_dsn_json`：目标库连接信息。
- `input_json`：模式参数，例如 SQL 内容、Shell 内容、LLM 参数或确认后的 SQL。
- `log_path`：日志文件路径。

文件落盘：

```text
data/datagen/<job_id>/input.sql
data/datagen/<job_id>/run.sh
data/datagen/<job_id>/result.json
logs/datagen_<job_id>.log
```

## 6. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/datagen` | 造数页面 |
| `GET` | `/api/datagen/jobs` | 造数任务历史列表 |
| `POST` | `/api/datagen/jobs` | 创建并启动造数任务 |
| `GET` | `/api/datagen/jobs/{id}` | 查询任务状态 |
| `POST` | `/api/datagen/jobs/{id}/stop` | 停止任务 |
| `GET` | `/api/datagen/jobs/{id}/log` | 读取执行日志 |
| `POST` | `/api/datagen/inspect` | 读取目标库表和字段结构 |
| `POST` | `/api/datagen/llm/preview` | 生成 SQL 预览，不执行 |

创建任务请求示例：

```json
{
  "name": "orders-1w",
  "mode": "sql",
  "db_dsn": {
    "host": "mysql",
    "port": 3306,
    "user": "root",
    "password": "s3cret",
    "database": "sqlpulse_demo"
  },
  "content": "INSERT INTO orders (uid, amount, status, created_at) VALUES (1, 10.5, 'new', NOW());",
  "source": "paste"
}
```

LLM 预览请求示例：

```json
{
  "db_dsn": {
    "host": "mysql",
    "port": 3306,
    "user": "root",
    "password": "s3cret",
    "database": "sqlpulse_demo"
  },
  "table_names": ["orders"],
  "row_count": 100,
  "business_hint": "电商订单数据"
}
```

## 7. UI 设计

### 7.1 页面结构

在侧边栏新增“造数”入口，页面复用现有目标库表单和卡片样式。

页面区块：

1. 任务名。
2. 目标数据库 DSN。
3. 测试连接按钮。
4. 模式切换 tabs。
5. 模式参数区。
6. 开始造数按钮。
7. 历史任务表。

### 7.2 模式参数区

- SQL 模式：粘贴文本和上传文件两个入口。
- Shell 模式：脚本编辑器，展示环境变量说明。
- LLM 模式：读取表结构、勾选表、填写行数、生成预览、编辑 SQL、执行。

### 7.3 交互约定

- 造数页和压测页使用独立的 localStorage key，避免数据串扰。
- 任务运行时页面每 3 到 5 秒轮询一次状态。
- Shell 和 LLM 生成的 SQL 在执行前展示确认提示。

## 8. 执行器设计

### 8.1 SQL 执行器

职责：

- 解析 `DELIMITER`。
- 将 SQL 文件拆分为语句。
- 逐条执行，记录日志和受影响行数。

执行器不使用现有 `split_statements`，因为现有函数只服务于压测 SQL，不处理 `DELIMITER` 和过程体。

示例解析逻辑：

```text
读取 DELIMITER 指令
  -> 更新当前语句分隔符
  -> 扫描注释和字符串
  -> 按当前分隔符切分语句
  -> 返回语句列表
```

执行逻辑：

```text
连接目标库
  -> 逐条执行语句
  -> 累加 cursor.rowcount
  -> 写日志
  -> 任一条失败则终止
```

### 8.2 Shell 执行器

后端将脚本写入隔离目录：

```text
data/datagen/<job_id>/run.sh
```

然后执行：

```python
env = os.environ.copy()
env.update({
    "TARGET_DB_HOST": dsn["host"],
    "TARGET_DB_PORT": str(dsn["port"]),
    "TARGET_DB_USER": dsn["user"],
    "TARGET_DB_PASSWORD": dsn["password"],
    "TARGET_DB_NAME": dsn["database"],
})

proc = subprocess.Popen(
    ["/bin/bash", str(script_path)],
    cwd=script_dir,
    env=env,
    stdout=log_file,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
```

用户脚本示例：

```bash
#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import os
import pymysql

conn = pymysql.connect(
    host=os.environ["TARGET_DB_HOST"],
    port=int(os.environ["TARGET_DB_PORT"]),
    user=os.environ["TARGET_DB_USER"],
    password=os.environ["TARGET_DB_PASSWORD"],
    database=os.environ["TARGET_DB_NAME"],
    autocommit=True,
)

with conn.cursor() as cur:
    for i in range(1, 101):
        cur.execute(
            "INSERT INTO orders (uid, amount, status, created_at) VALUES (%s, %s, %s, NOW())",
            (i, 10.50, "new"),
        )

print("inserted=100")
conn.close()
PY
```

可选结果文件：

```bash
echo '{"rows_affected": 100}' > result.json
```

退出码处理：

- `0`：`finished`。
- 非零：`failed`，错误信息取日志尾部。
- 超时：终止进程组，`failed`。

### 8.3 LLM 执行器

流程：

```text
读取 information_schema
  -> 构建表结构提示词
  -> 调用 LLM
  -> 剥离 Markdown 代码块
  -> 校验表名和语句类型
  -> 前端预览和编辑
  -> 转为 SQL 执行任务
```

LLM Provider 使用统一抽象：

```text
DeepSeek / GLM / OpenAI-compatible
  -> 同一 complete() 接口
```

配置项：

```text
LLM_PROVIDER
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
LLM_TIMEOUT_SEC
```

SQL 校验规则：

- 默认只允许 `INSERT` 和 `REPLACE`。
- 目标表必须在用户选择范围内。
- 禁止执行未选中的 DDL、DROP 或其它变更。
- 返回内容必须能解析为有效 SQL。

## 9. 安全设计

- Shell 模式由 `DATAGEN_SCRIPT_EXECUTION_ENABLED` 控制，默认关闭。
- 密码只通过子进程环境变量传递，不进入命令行。
- 日志写入前对 DSN 密码做脱敏。
- 脚本落盘后设置 `0700` 权限。
- 脚本工作目录隔离到 `data/datagen/<job_id>/`。
- 限制上传文件大小、SQL 长度和执行超时。
- 当前项目没有鉴权，Shell 模式仅推荐在本机或受信内网使用。
- LLM 输出不能直接执行，必须经过预览、目标表校验和语句类型校验。

## 10. 与现有模块集成

- `app/main.py` 的 lifespan 中增加造数孤儿任务恢复。
- `app/db.py` 的 schema 增加 `datagen_jobs` 表。
- `app/config.py` 增加造数相关开关、超时和 LLM 配置。
- `app/models.py` 增加造数请求模型。
- `base.html` 侧边栏增加“造数”入口。
- `Dockerfile` 确保 Bash 可用，不安装 sysbench。

## 11. 分阶段实施

### 阶段一：SQL 造数闭环

- 完成 `datagen_jobs` 表和基础任务状态。
- 完成 SQL 执行器和 `.sql` 文件模式。
- 完成造数页面和历史列表。
- 完成平台重启恢复。

### 阶段二：Shell 造数

- 完成 Shell 子进程执行器。
- 完成超时、停止、日志读取。
- 增加安全开关和密码脱敏。

### 阶段三：LLM 造数

- 完成 `llm_client.py`。
- 完成表结构读取和 LLM 预览接口。
- 完成前端预览、编辑、校验和执行。

### 阶段四：收尾

- 补充示例脚本和文档。
- 补充日志轮询优化。
- 补充自动化测试。

## 12. 测试方案

### 单元测试

- SQL 解析器：`DELIMITER`、字符串内分号、注释、存储过程。
- LLM 返回解析：Markdown 代码块剥离、表名和语句类型校验。
- 任务状态转换。
- Shell 日志尾部读取。

### 集成测试

- SQL 模式：创建临时表、插入数据、验证 `rows_affected`。
- LLM 模式：使用 mock LLM 返回固定 SQL，验证预览和校验流程。
- Shell 模式：启用开关后执行简单脚本，验证环境变量注入和结果文件。

### 安全测试

- 验证日志不包含密码。
- 验证 Shell 模式关闭时接口返回错误。
- 验证 LLM 返回未授权表名时被拦截。

## 13. 验收标准

- 用户可通过页面粘贴或上传 `.sql` 文件并完成造数。
- 用户可提交 Shell 脚本并查看执行日志。
- 用户可读取表结构、生成 LLM SQL、预览编辑后执行。
- 造数任务具备 `pending/running/finished/failed/cancelled` 状态。
- 运行中的 Shell 任务可以停止，停止时终止整个进程组。
- 平台重启后未结束任务被标记为 `failed`。
- 密码不出现在任务日志、错误信息和命令行中。

## 14. 风险与决策

- Lua 造数因与 Locust 技术栈不一致而取消。
- Shell 模式具有任意代码执行风险，因此默认关闭并限制在受信环境使用。
- LLM 生成 SQL 的质量不稳定，通过预览、编辑和校验降低风险。
- Shell 模式的行数统计依赖用户脚本主动提供 `result.json`，不提供时保持为空。

