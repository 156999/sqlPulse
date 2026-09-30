# 慢 SQL 压测优化方案

## 1. 背景

当前监控页的“慢查询”来自 MySQL 全局状态 `Slow_queries` 的采样增量：

```sql
SHOW GLOBAL STATUS LIKE 'Slow_queries';
```

该指标存在两个限制：

1. 它是整个 MySQL 实例的全局数据，无法确认是否由当前压测任务产生。
2. 它只返回数量，无法定位到具体任务组或 SQL。

本方案增加“当前压测任务慢 SQL”指标，以 Locust 执行每条 SQL 的实际耗时为准。用户可在压测任务中配置慢 SQL 判定阈值，系统按 `sql_id` 统计慢 SQL 数量和耗时，并展示来源 SQL 标识，不展示完整 SQL 文本。

## 2. 目标

- 新建压测时可设置慢 SQL 阈值，例如 `500ms`、`1s`。
- 只统计当前压测任务产生的慢 SQL，不受同一数据库其他业务流量影响。
- 能定位到任务组和 SQL 编号，例如 `任务组：订单查询，sql_2`。
- 统计慢 SQL 次数、慢 SQL 比例、平均耗时、P95、P99、最大耗时。
- 压测过程中实时展示慢 SQL数量，压测结束后在报告中保留汇总。
- 不在慢 SQL列表中直接展示完整 SQL，避免泄漏业务 SQL 和敏感参数。
- 保留 MySQL 全局慢查询作为独立的数据库引擎参考指标，不与任务级慢 SQL 混为一个指标。

## 3. 口径定义

### 3.1 任务级慢 SQL

每次 SQL 执行从发送数据库请求前开始计时，到该 SQL 完成或抛出异常为止：

```text
elapsed_ms = (结束时间 - 开始时间) * 1000
```

当满足以下条件时计为一次任务级慢 SQL：

```text
elapsed_ms >= slow_sql_threshold_ms
```

默认阈值建议为 `500ms`，可配置范围为 `1ms～600000ms`。

失败 SQL 是否计入慢 SQL由配置决定，默认不计入慢 SQL，仅计入失败请求：

```text
慢 SQL数量 = 成功执行且 elapsed_ms >= 阈值的 SQL 次数
```

如用户开启“失败请求也统计慢 SQL”，则异常 SQL按实际耗时额外计入，但在报告中与成功慢 SQL分开显示。

### 3.2 统计维度

每次压测请求可能包含多条 SQL。建议同时保留两个维度：

- 请求级：整个任务组执行耗时，用于 Locust 当前请求统计。
- SQL级：任务组内部每条 SQL 的单独耗时，用于慢 SQL定位。

事务组中每一条 SQL 都独立计时，事务提交失败时不改变已采集的 SQL 耗时记录。

## 4. 表单设计

在“压测参数”区域新增“慢 SQL 监控”配置：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| 慢 SQL 阈值 | 数字输入 | 500 | 单位毫秒 |
| 开启慢 SQL统计 | 开关 | 开启 | 关闭时不生成 SQL级慢查询数据 |
| 失败 SQL计入慢 SQL | 开关 | 关闭 | 失败请求单独统计，避免重复解读 |

建议表单文案：

```text
慢 SQL 阈值：500 ms
仅统计当前任务中执行耗时达到阈值的 SQL，不使用数据库全局慢查询阈值。
```

任务提交参数增加：

```json
{
  "slow_sql_enabled": true,
  "slow_sql_threshold_ms": 500,
  "slow_sql_include_failures": false
}
```

旧任务没有上述字段时使用默认值，保证历史任务和旧 API 兼容。

## 5. 执行器改造

### 5.1 生成 Locust 运行配置

创建任务时将慢 SQL配置写入运行快照，并传递给生成的 Locust 文件：

```python
SLOW_SQL_ENABLED = True
SLOW_SQL_THRESHOLD_MS = 500
SLOW_SQL_INCLUDE_FAILURES = False
```

不把完整 SQL写入监控事件。每条 SQL使用稳定标识：

```text
run_id + group_id + sql_id + statement_index
```

其中：

- `group_id`：任务组标识。
- `sql_id`：现有 `sql_1`、`sql_2` 标识。
- `statement_index`：任务组内第几条 SQL，从 1 开始。

### 5.2 单条 SQL计时

Locust 模板中的任务执行器应将当前代码：

```python
for statement in _TASKS[sql_id]:
    cur.execute(*statement.bind(values))
```

改为逐条计时：

```python
for index, statement in enumerate(_TASKS[sql_id], 1):
    started = time.perf_counter()
    failed = False
    try:
        cur.execute(*statement.bind(values))
    except Exception:
        failed = True
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if SLOW_SQL_ENABLED and elapsed_ms >= SLOW_SQL_THRESHOLD_MS:
            if not failed or SLOW_SQL_INCLUDE_FAILURES:
                slow_sql_event(
                    sql_id=sql_id,
                    statement_index=index,
                    elapsed_ms=elapsed_ms,
                )
```

这里的 `statement_index` 只用于定位，不发送完整 SQL和绑定参数。

### 5.3 事件传输

Locust 子进程不能直接修改 Web 进程内存，建议使用独立的轻量事件文件或本地队列：

- 每个 run 使用一个 `slow_sql_events.jsonl` 文件。
- 每条慢 SQL写入一行事件。
- 写入内容仅包含 run_id、sql_id、statement_index、耗时和是否失败。
- 写入采用追加方式，单条 JSON 不超过固定大小。
- Web 采集器每秒读取新增内容并聚合，不重复读取历史行。

事件示例：

```json
{
  "run_id": "a1b2c3d4",
  "sql_id": "sql_2",
  "statement_index": 1,
  "elapsed_ms": 823.4,
  "failed": false,
  "ts": 1780224123
}
```

并发 Locust 用户同时写入时应避免多个进程共享文件句柄；当前 Locust采用单个 headless 进程时可直接追加。后续扩展分布式压测时，每个 worker 写独立文件，Web 采集器统一汇总。

## 6. 数据模型

### 6.1 实时指标表

在现有 `metrics` 表增加任务级慢 SQL字段：

```sql
ALTER TABLE metrics ADD COLUMN slow_sql_count INTEGER;
ALTER TABLE metrics ADD COLUMN slow_sql_failed_count INTEGER;
ALTER TABLE metrics ADD COLUMN slow_sql_rate REAL;
```

字段含义：

- `slow_sql_count`：本采样周期新增的成功慢 SQL数量。
- `slow_sql_failed_count`：本采样周期新增的失败慢 SQL数量。
- `slow_sql_rate`：慢 SQL数量 / 当前周期 SQL执行总数。

### 6.2 慢 SQL 汇总表

新增 `slow_sql_stats` 表：

```sql
CREATE TABLE IF NOT EXISTS slow_sql_stats (
  run_id TEXT NOT NULL,
  sql_id TEXT NOT NULL,
  statement_index INTEGER NOT NULL,
  slow_count INTEGER NOT NULL DEFAULT 0,
  failed_slow_count INTEGER NOT NULL DEFAULT 0,
  total_elapsed_ms REAL NOT NULL DEFAULT 0,
  max_elapsed_ms REAL NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (run_id, sql_id, statement_index)
);
```

`samples_json` 只保存有限数量的耗时样本，例如最近 100 个样本，用于计算近似 P95/P99；不保存完整 SQL和绑定参数。

## 7. API 设计

### 7.1 实时点

现有：

```text
GET /api/runs/{run_id}/stream
```

事件点增加：

```json
{
  "slow_sql_count": 3,
  "slow_sql_failed_count": 0,
  "slow_sql_rate": 0.08
}
```

### 7.2 慢 SQL 明细汇总

新增：

```text
GET /api/runs/{run_id}/slow-sql
```

响应示例：

```json
{
  "threshold_ms": 500,
  "total_slow_count": 128,
  "items": [
    {
      "group_id": "order-read",
      "group_name": "订单查询",
      "sql_id": "sql_2",
      "statement_index": 1,
      "slow_count": 128,
      "slow_rate": 0.42,
      "avg_ms": 823.4,
      "p95_ms": 1104.2,
      "p99_ms": 1480.7,
      "max_ms": 1712.5
    }
  ]
}
```

接口不返回完整 SQL。若需要辅助识别，可返回用户定义的任务组名称和 SQL编号；SQL文本查看仍由用户回到任务配置或报告中的受控入口完成。

## 8. 页面展示

### 8.1 实时监控

将现有监控图表拆分为两个概念：

- 数据库引擎状态：MySQL QPS、全局 `Slow_queries`、行锁等待、磁盘临时表。
- 当前任务 SQL：任务级慢 SQL数量、慢 SQL率。

新增卡片：

```text
慢 SQL：128 次
阈值：500 ms
慢 SQL率：8.0%
```

新增表格：

| 任务组 | SQL编号 | 慢 SQL次数 | 慢 SQL率 | 平均耗时 | P95 | P99 | 最大耗时 |
|---|---|---:|---:|---:|---:|---:|---:|
| 订单查询 | sql_2 / 第1条 | 128 | 42.0% | 823ms | 1104ms | 1481ms | 1713ms |

### 8.2 报告页

报告中增加：

- 慢 SQL阈值和是否包含失败 SQL。
- 当前任务总慢 SQL数和慢 SQL率。
- 按任务组、SQL编号排序的慢 SQL排行。
- 任务级慢 SQL与 MySQL全局 `Slow_queries` 的差异提示。

当两个指标差异较大时提示：

```text
数据库全局慢查询包含其他业务连接产生的慢 SQL，不等同于当前压测任务慢 SQL。
```

## 9. 与现有指标的关系

| 指标 | 来源 | 范围 | 用途 |
|---|---|---|---|
| 任务级慢 SQL | Locust 单条 SQL计时 | 当前压测任务 | 定位哪条压测 SQL慢 |
| MySQL `Slow_queries` | `SHOW GLOBAL STATUS` | 整个数据库实例 | 观察数据库全局慢查询变化 |
| 请求错误率 | Locust 请求事件 | 当前压测请求 | 观察失败请求比例 |
| P95/P99 | Locust 统计 | 当前任务或 SQL | 观察尾延迟 |

两类慢查询指标必须使用不同名称，避免用户误认为它们是同一口径。

## 10. 边界与性能要求

- 慢 SQL统计默认开启，但只在超过阈值时写事件，正常 SQL不额外写明细。
- 事件文件写入失败不能阻断 SQL执行，只记录应用日志并增加采集丢失计数。
- 慢 SQL明细必须限制最大行数和样本数量，避免压测过程中日志无限增长。
- 阈值低于 `10ms` 时给出提示，避免把计时误差和网络抖动大量统计为慢 SQL。
- 事务中的每条 SQL单独统计；事务整体耗时继续由请求级指标统计。
- 预览接口不执行 SQL，也不产生慢 SQL指标。
- 完整 SQL只保存在当前任务已有的运行快照中，慢 SQL统计表不复制完整 SQL。

## 11. 实现步骤

1. 在 `TaskCreate` 和运行快照中增加慢 SQL配置字段。
2. 修改 Locust 模板，对每条 statement 增加耗时采集。
3. 增加慢 SQL事件文件和采集器聚合逻辑。
4. 扩展 `metrics` 表和新增 `slow_sql_stats` 表。
5. 新增 `/api/runs/{run_id}/slow-sql` 接口。
6. 修改实时监控图表，区分任务级慢 SQL和 MySQL全局慢查询。
7. 修改报告页，增加慢 SQL排行和阈值说明。
8. 增加单元测试、Locust模板测试和真实 MySQL集成测试。

## 12. 验收标准

- 用户可以在压测表单设置慢 SQL阈值。
- 当前任务产生的慢 SQL数量不受其他数据库连接的慢 SQL影响。
- 慢 SQL统计能够定位到任务组、`sql_id` 和组内 SQL序号。
- 慢 SQL统计不展示完整 SQL和实际参数值。
- 同一任务的实时监控与最终报告统计口径一致。
- 事务任务组的每条 SQL都有独立耗时统计。
- SQL执行失败不会导致慢 SQL事件采集器中断。
- 运行服务使用 `--reload-dir app` 时，Locust生成文件不会触发 Web 服务重启。
- 旧任务和旧 API 请求在未提供慢 SQL配置时继续正常执行。
