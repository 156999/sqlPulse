# 造数 MCP 设计方案

## 一、背景

SQL Pulse 已经具备运行时造数能力，包括 SQL 造数、字段规则、数据库元数据读取、依赖表补数计划、任务状态、日志和停止能力。后续引入 agent 后，造数能力不应只通过页面表单使用，也应通过 MCP tool 暴露给 agent，让 agent 可以在用户授权范围内完成“理解需求、分析表结构、生成规则、提交任务、跟踪结果”的完整流程。

本方案目标是让造数能力同时支持两类调用方：

- SQL Pulse 内嵌 agent：运行在产品内部，可读取当前登录用户上下文。
- 外部 MCP client：例如 Codex、Claude、Cursor 等，通过 token 认证访问 SQL Pulse 的 MCP server。

两类调用方都遵循 MCP 协议，差异只在认证与用户上下文来源。

## 二、设计原则

1. **一套业务能力，两套认证入口**
   MCP tool 不重复实现造数逻辑，而是调用统一的 datagen service。内嵌 agent 和外部 client 只负责把调用者解析成已认证的 `user_id`。

2. **权限边界落在用户和连接**
   MCP 默认只接受 `connection_id`，不接受数据库密码。服务端根据 `user_id + connection_id` 校验连接归属，并从用户保存的连接中解析 DSN。

3. **异步任务模型**
   创建造数任务后立即返回 `job_id`，agent 通过查询任务状态和日志跟踪执行结果，避免长时间阻塞 MCP 调用。

4. **先开放 SQL 造数，暂缓 Shell 造数**
   Shell 造数风险更高，容易变成远程脚本执行入口。第一版 MCP 只开放 SQL 模式和单表字段规则模式；Shell 模式继续仅受后台配置控制，不默认暴露给 MCP。

5. **创建任务前必须分析和校验**
   agent 创建任务前应先读取目标表元数据，并在涉及外键、唯一索引和依赖表写入时生成依赖计划。服务端创建任务时仍要重新读取元数据并复验规则，不能信任 agent 的中间结果。

## 三、总体架构

```text
SQL Pulse 页面 / 内嵌 Agent / 外部 MCP Client
              |
              v
        MCP auth adapter
      /                  \
session current_user    token/api key
      \                  /
              v
        authenticated user_id
              |
              v
       datagen domain service
              |
              v
 db + connection_manager + datagen_executor + MySQL
```

建议把当前 `app/routers/datagen.py` 中偏业务的逻辑下沉到 service 层，例如：

```text
app/services/datagen_jobs.py
```

统一 service 接口接收已认证的 `user_id`：

```python
create_datagen_sql_job(user_id, connection_id, payload)
list_datagen_tables(user_id, connection_id, query)
get_datagen_table_metadata(user_id, connection_id, table)
apply_datagen_rules(user_id, connection_id, table, rules, row_count)
build_datagen_dependency_plan(user_id, connection_id, table, target_rows, strategy)
get_datagen_job(user_id, job_id)
get_datagen_job_log(user_id, job_id, tail_chars)
stop_datagen_job(user_id, job_id)
```

HTTP 页面、内嵌 agent 和外部 MCP server 都调用同一套 service。

## 四、认证与上下文

### 1. 内嵌 agent 模式

内嵌 agent 运行在 SQL Pulse 内部，可复用当前 Web 会话：

```text
SQL Pulse session -> current_user -> user_id -> connection_id
```

特点：

- 不需要用户再次输入数据库密码。
- agent 只能读取当前用户保存的连接。
- agent 创建、查询、停止任务时都按当前用户过滤。
- 更适合作为产品内置智能助手的默认模式。

### 2. 外部 MCP client 模式

外部 client 没有 SQL Pulse 页面登录态，需要独立认证：

```text
MCP auth token / api key -> user_id -> connection_id
```

可选方案：

- Personal access token：用户在 SQL Pulse 中创建 token，token 映射到该用户。
- 管理员 token：仅用于本地开发或受控环境。
- OAuth：后续需要多端授权时再引入。

外部 MCP client 仍不应直接传数据库密码。除非开启显式开发模式或管理员能力，否则只允许使用当前 token 所属用户的 `connection_id`。

## 五、数据归属改造

当前 `datagen_jobs` 如果没有用户归属字段，MCP 化后需要补齐权限边界。

建议给 `datagen_jobs` 增加：

```text
owner_user_id TEXT
```

规则：

1. 创建任务时写入当前 `user_id`。
2. 查询任务、读日志、停止任务时校验 `owner_user_id`。
3. 历史任务列表默认只返回当前用户任务。
4. 管理员视角可另行提供跨用户查询能力，但不作为普通 MCP tool 默认能力。

如果为了兼容历史任务，迁移时可以允许旧记录 `owner_user_id` 为空，但 MCP 查询旧任务时应按策略限制，例如只允许管理员访问，或将旧任务标记为未归属不可通过 MCP 操作。

## 六、MCP 工具清单

### 1. `list_my_connections`

列出当前用户可用连接。

输入：

```json
{}
```

输出：

```json
{
  "connections": [
    {
      "id": "conn-id",
      "name": "测试库",
      "host": "127.0.0.1",
      "port": 3306,
      "database": "shop",
      "user": "root",
      "has_password": true
    }
  ]
}
```

不返回数据库密码。

### 2. `list_datagen_tables`

查询当前用户某个连接下的数据表。

输入：

```json
{
  "connection_id": "conn-id",
  "query": "order"
}
```

输出：

```json
{
  "tables": ["ec_orders", "ec_order_items"]
}
```

### 3. `get_datagen_table_metadata`

读取目标表元数据。

输入：

```json
{
  "connection_id": "conn-id",
  "table": "ec_order_items"
}
```

输出包括：

- columns
- unique_indexes
- foreign_keys
- checks
- unrecognized

### 4. `apply_datagen_rules`

根据数据库元数据归一化字段规则。

输入：

```json
{
  "connection_id": "conn-id",
  "table": "orders",
  "row_count": 10000,
  "rules": [
    {
      "column": "amount",
      "generator": "randf",
      "params": {"min": 10, "max": 5000, "digits": 2}
    }
  ]
}
```

输出：

```json
{
  "ok": true,
  "rules": [],
  "changes": [],
  "errors": [],
  "warnings": [],
  "metadata": {}
}
```

### 5. `validate_datagen_rules`

只校验字段规则，不回写归一化结果。

适用于 agent 想先判断当前规则是否可用，而不希望自动调整参数的场景。

### 6. `build_datagen_dependency_plan`

生成依赖表补数计划。

输入：

```json
{
  "connection_id": "conn-id",
  "table": "ec_order_items",
  "target_rows": 10000,
  "strategy": "direct_dependencies"
}
```

`strategy` 取值：

```text
target_only
direct_dependencies
full_dependencies
```

输出包括：

- dependencies
- unique_spaces
- execution_order
- warnings
- planned_rows

### 7. `create_datagen_sql_job`

创建 SQL 造数任务。

输入：

```json
{
  "connection_id": "conn-id",
  "name": "orders 造数 10000",
  "sql": "INSERT INTO orders (status, amount) VALUES ({{var('status')}}, {{var('amount')}});",
  "variables": {
    "status": "pick('new','paid','cancelled')",
    "amount": "randf(10,5000,2)"
  },
  "row_count": 10000,
  "target_table": "orders",
  "field_rules": [],
  "dependency_strategy": "target_only",
  "confirm_dependency_writes": false
}
```

输出：

```json
{
  "job_id": "abc12345",
  "status": "running"
}
```

服务端必须执行：

1. 校验 `connection_id` 属于当前用户。
2. 测试数据库连接。
3. 校验 SQL 占位符和变量。
4. 若有 `target_table`，重新读取 metadata 并复验 field rules。
5. 若 `dependency_strategy != target_only`，要求 `confirm_dependency_writes=true`。
6. 创建任务并启动 `datagen_executor`。

### 8. `get_datagen_job`

查询任务状态。

输入：

```json
{
  "job_id": "abc12345"
}
```

输出：

```json
{
  "job_id": "abc12345",
  "name": "orders 造数 10000",
  "mode": "sql",
  "status": "finished",
  "rows_affected": 10000,
  "error_msg": null,
  "created_at": "...",
  "started_at": "...",
  "ended_at": "..."
}
```

### 9. `get_datagen_job_log`

读取任务日志尾部。

输入：

```json
{
  "job_id": "abc12345",
  "tail_chars": 20000
}
```

输出：

```json
{
  "job_id": "abc12345",
  "log": "..."
}
```

### 10. `stop_datagen_job`

停止当前用户自己的运行中任务。

输入：

```json
{
  "job_id": "abc12345"
}
```

输出：

```json
{
  "status": "cancelled"
}
```

## 七、Agent 调用流程

工具多时，agent 不应自由乱调，而应遵循固定工作流。

### 1. 标准流程

```text
用户提出造数需求
  |
  v
识别连接、目标表、行数、依赖策略和业务字段规则
  |
  v
list_my_connections
  |
  v
list_datagen_tables
  |
  v
get_datagen_table_metadata
  |
  v
apply_datagen_rules / validate_datagen_rules
  |
  v
build_datagen_dependency_plan
  |
  v
必要时向用户确认依赖表写入
  |
  v
create_datagen_sql_job
  |
  v
get_datagen_job 轮询
  |
  v
失败时 get_datagen_job_log 并给出修复建议
```

### 2. 示例

用户说：

```text
给测试库的 orders 表造 1 万条订单数据，尽量满足外键依赖。
```

agent 调用：

```json
{
  "tool": "list_my_connections",
  "arguments": {}
}
```

选择连接后查表：

```json
{
  "tool": "list_datagen_tables",
  "arguments": {
    "connection_id": "c1",
    "query": "orders"
  }
}
```

读取表结构：

```json
{
  "tool": "get_datagen_table_metadata",
  "arguments": {
    "connection_id": "c1",
    "table": "orders"
  }
}
```

生成依赖计划：

```json
{
  "tool": "build_datagen_dependency_plan",
  "arguments": {
    "connection_id": "c1",
    "table": "orders",
    "target_rows": 10000,
    "strategy": "direct_dependencies"
  }
}
```

应用字段规则：

```json
{
  "tool": "apply_datagen_rules",
  "arguments": {
    "connection_id": "c1",
    "table": "orders",
    "row_count": 10000,
    "rules": [
      {
        "column": "status",
        "generator": "pick",
        "params": {"values": ["new", "paid", "cancelled"]}
      },
      {
        "column": "amount",
        "generator": "randf",
        "params": {"min": 10, "max": 5000, "digits": 2}
      }
    ]
  }
}
```

如果依赖计划会写入父表，agent 必须向用户确认：

```text
本次会向 ec_customers、ec_products 等依赖表补充数据，是否继续？
```

用户确认后创建任务：

```json
{
  "tool": "create_datagen_sql_job",
  "arguments": {
    "connection_id": "c1",
    "name": "orders 造数 10000",
    "target_table": "orders",
    "row_count": 10000,
    "dependency_strategy": "direct_dependencies",
    "confirm_dependency_writes": true,
    "field_rules": [],
    "sql": "INSERT INTO orders (status, amount) VALUES ({{var('status')}}, {{var('amount')}});",
    "variables": {
      "status": "pick('new','paid','cancelled')",
      "amount": "randf(10,5000,2)"
    }
  }
}
```

查询任务：

```json
{
  "tool": "get_datagen_job",
  "arguments": {
    "job_id": "abc12345"
  }
}
```

失败时读取日志：

```json
{
  "tool": "get_datagen_job_log",
  "arguments": {
    "job_id": "abc12345",
    "tail_chars": 20000
  }
}
```

## 八、Agent 行为约束

建议给 agent 配置以下系统级策略：

```text
造数时优先使用用户已保存连接，不请求、不展示、不记录数据库密码。
创建任务前必须读取目标表 metadata。
涉及外键、唯一索引或依赖表时，必须生成 dependency plan。
涉及依赖表写入时，必须向用户确认后才设置 confirm_dependency_writes=true。
创建任务后必须跟踪任务状态。
任务失败时读取日志，并给出可执行修复建议。
不要使用 Shell 造数，除非系统明确开启并且用户具有管理员权限。
不要生成 DROP、TRUNCATE、ALTER 等破坏性 SQL。
```

## 九、安全策略

1. MCP tool 不返回数据库密码。
2. 默认只允许 `connection_id`，不允许任意 DSN。
3. SQL 造数内容沿用 `datagen_max_sql_chars` 限制。
4. Shell 造数不在第一版 MCP 中开放。
5. 依赖表写入必须显式确认。
6. 创建任务时重新读取数据库元数据并复验规则。
7. 查询、停止、读日志必须校验任务归属。
8. 任务日志中避免输出完整 DSN 和密码。
9. 外部 MCP token 应支持撤销、过期和最小权限。
10. 可选增加 SQL 策略检查，默认拒绝 `DROP`、`TRUNCATE`、`ALTER`、`CREATE USER`、`GRANT` 等高风险语句。

## 十、与现有代码的对应关系

现有能力可复用：

| MCP tool | 现有模块 |
|---|---|
| `list_datagen_tables` | `app.services.datagen_rules.list_table_names` |
| `get_datagen_table_metadata` | `app.services.datagen_rules.read_table_metadata` |
| `apply_datagen_rules` | `app.services.datagen_rules.apply_database_rules` |
| `validate_datagen_rules` | `app.services.datagen_rules.validate_field_rules` |
| `build_datagen_dependency_plan` | `app.services.datagen_dependencies.build_dependency_plan` |
| `create_datagen_sql_job` | `app.routers.datagen.create_datagen_job` 中的业务逻辑下沉 |
| `get_datagen_job` | `app.db.get_datagen_job` + safe job view |
| `get_datagen_job_log` | `app.services.script_runner.tail_file` |
| `stop_datagen_job` | `app.services.datagen.datagen_executor.stop` |

建议新增：

```text
app/services/datagen_jobs.py
app/mcp/datagen_server.py
app/mcp/auth.py
```

其中 `datagen_jobs.py` 是业务服务，`datagen_server.py` 只负责 MCP tool schema 和入参出参转换，`auth.py` 负责将 session 或 token 解析为 `user_id`。

## 十一、实施阶段

### 阶段一：Service 下沉和任务归属

1. 给 `datagen_jobs` 增加 `owner_user_id`。
2. 创建任务时写入当前用户。
3. 查询、停止、日志按用户校验。
4. 把 router 中创建任务、查询任务、停止任务、读日志逻辑下沉到 service。
5. 现有 HTTP 页面行为保持不变。

### 阶段二：内嵌 agent MCP

1. 实现 MCP tools。
2. MCP 调用从 SQL Pulse session 解析当前用户。
3. 默认只暴露 SQL 造数。
4. 验证 agent 可完成查连接、查表、查元数据、创建任务、轮询任务。

### 阶段三：外部 MCP client

1. 增加 token / api key 认证。
2. token 映射到 SQL Pulse 用户。
3. 外部 client 使用同一套 MCP tools。
4. 支持 token 撤销、过期和审计日志。

### 阶段四：策略增强

1. SQL 风险语句拦截。
2. 更细粒度权限，例如只读 metadata、允许造数、允许依赖表写入。
3. 管理员视角的跨用户任务管理。
4. Shell 造数在管理员和显式开关下评估是否开放。

## 十二、验收标准

1. 内嵌 agent 和外部 MCP client 都使用 MCP 协议调用同一套造数 tools。
2. 两种调用方式都能被解析成明确的 `user_id`。
3. MCP 调用不能访问其他用户的连接、任务和日志。
4. MCP 创建 SQL 造数任务后能返回 `job_id`，并能查询到最终状态。
5. 涉及依赖表写入时，没有用户确认不能执行。
6. MCP 响应中不出现数据库密码。
7. HTTP 页面原有造数功能不受影响。
8. 造数任务失败时，agent 能读取日志并给出可执行修复建议。

