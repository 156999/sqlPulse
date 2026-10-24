# SQL Pulse L1 降级报告设计（硬规则 · 无 LLM）

- 状态：**Draft — 待组会评审**
- 日期：2026-09-29
- 范围：压测结束后的**诊断报告降级路径**（LLM 未配置 / 调用失败时的默认报告）
- 关联实现：`app/services/report.py`、`app/services/rule_engine.py`、`app/services/collector.py`、`app/services/explain_probe.py`、`app/services/explain_view.py`、`app/templates/report.html`
- 关联文档：`sqlpulse/docs/feature_gap_analysis.md`、`docs/design/explain-collection.md`、`docs/onboarding/explain-primer.md`

> **本文档的双重身份**：① 组会讨论稿 —— 第 5、11、12 节的规则与阈值需要团队确认；② 编码指导书 —— 第 4、8、10、14 节是实现的接口契约与验收标准。
> 本文档**不含实现代码**，只定义"要什么"与"怎么接"。

---

## 1. 背景与目标

### 1.1 为什么必须有 L1

平台的诊断链路设计为三层：

| 层 | 名称 | 依赖 | 现状 |
|---|---|---|---|
| L0 | 指标总览 | 无 | **已实现**（`report.build_l0`） |
| L1 | 本地硬规则诊断 | 无（零外部依赖） | **部分实现**（`rule_engine.py` 仅 5 条规则） |
| L2 | LLM 智能诊断 | DeepSeek / GLM API Key | **未实现**（`app/ai/` 仍是占位） |

L1 存在的三个理由：

1. **降级兜底**：用户没配 API Key，或 Key 失效 / 网络不通 / 额度耗尽时，报告不能退化成"只有几张表"。立案书明确要求"LLM 调用失败自动回退规则报告"。
2. **确定性**：L1 输出可复现、可单测、可审计。同一份压测数据永远给同一个结论，适合作为 L2 的"事实底座"。
3. **交付物完整性**：报告要作为项目交付物（report + 演示），不能出现"因为没配 Key 所以报告是空的"。

### 1.2 目标

- 用**已有指标**（不新增采集、不改压测引擎）产出结构化、可解释的压测解读报告。
- 每条结论必须带**证据三联**：压测账本证据 + MySQL 引擎证据 + EXPLAIN 计划证据。
- 规则、阈值、等级全部**显式可配置、可单测**。
- 输出同时服务两个消费方：Markdown 下载（`report.md`）与 Web 报告页（`report.html`）。

### 1.3 非目标

- 不做 LLM 推理（属 L2）。
- 不自动建索引、不自动改写 SQL、不自动调 MySQL 参数。
- 不引入 Prometheus / 时序数据库 / 新采集指标。
- 不承诺"唯一根因"。L1 给的是"**最可能根因 + 支撑证据**"，多因推理留给 L2。

---

## 2. 现状盘点

### 2.1 可用数据源（全部已存在，无需新增采集）

**A. 压测账本（汇总）** — `data/locust/{run_id}_stats.csv`
已由 `report.build_l0()` 解析为 `l0` dict：

| 字段 | 含义 | 来源列 |
|---|---|---|
| `total_requests` / `total_failures` | 总请求 / 失败数 | Aggregated |
| `avg_ms` / `p50_ms` / `p95_ms` / `p99_ms` / `max_ms` | 汇总延迟分位 | Aggregated |
| `qps_avg` / `qps_peak` | QPS 均值（对 metrics 求均）/ 峰值 | metrics 表 |
| `err_rate` | 失败率 | 计算 |
| `threads_running_p95` | 运行线程数 P95 | metrics 表 |
| `mysql_slow_total` | 慢查询增量合计（近似条数） | metrics.slow_inc 求和 |
| `mysql_lock_waits_total` | 行锁等待增量合计 | metrics.lock_waits_inc 求和 |
| `actual_duration_sec` | 实际时长 | started_at / ended_at |
| `per_sql[]` | 分语句：`sql_id`、`sql`(80 字预览)、`requests`、`failures`、`avg_ms`、`p50_ms`、`p95_ms`、`p99_ms`、`max_ms`、`qps` | 每个 `sql_N` 行 |

**A'. 压测账本（时序）** — `data/locust/{run_id}_stats_history.csv`（`--csv-full-history`）

`collector.read_per_sql_history()` 已能解析为 `{sql_id: [{ts, rps, fails, avg_ms, p95_ms, p99_ms}]}`。
⚠️ **目前只有监控页在用，报告侧完全没用**。这是 L1 可以做"抖动 / 长尾 / 平台期"判断的现成来源。

**B. MySQL 运行时指标（时序）** — SQLite `metrics` 表，collector 每秒 1 点

| 列 | 含义 | 口径 |
|---|---|---|
| `threads_running` / `threads_connected` | 瞬时线程数 | 瞬时值 |
| `mysql_qps` | 目标库 Questions/s | 差分 |
| `slow_inc` | 慢查询增量 | 差分 |
| `lock_waits_inc` | 行锁等待增量 | 差分 |
| `tmp_disk_inc` | 磁盘临时表增量 | 差分 |
| `bufpool_hit` | 缓冲池命中率 | 累计口径 |

⚠️ 两个已知缺口：
- `COUNTER_KEYS` 里有 `Com_commit` 但**没有映射输出**（TPS 缺口，见 `feature_gap_analysis.md` 第二节）。
- `bufpool_hit` **已入库但未上图、未进报告**。

**C. EXPLAIN 计划产物** — `data/explain/{run_id}.json`（`probe_version=3`、`phase=post_run`）

```
summary: {tasks, statements, probed, skipped_not_explainable, dropped_by_limit,
          errors, findings, by_code{}, worst_level, max_rows, max_rows_ref, elapsed_ms}
tasks[]: {sql_id, weight, statements[]: {
            index, original, filled, parameters[], ok,
            plan[{id,select_type,table,type,possible_keys,key,key_len,ref,rows,filtered,Extra}],
            max_rows, findings[{code, level, table, detail}]
          }}
```

`by_code` 取值：
- **计划质量类**（参与 `worst_level`）：`table_scan` / `index_ignored` / `full_index_scan` / `filesort` / `temporary` / `join_buffer` / `compile_error` / `explain_error`
- **事实记录类**（不参与）：`skipped_not_explainable` / `not_probed`

**D. 目标库环境**
- `max_connections`：报告生成时实时 `SHOW VARIABLES`（`report.query_max_connections`）
- `server_version`：EXPLAIN 产物内

**E. 任务上下文**
- `run` 行：`concurrency`、`spawn_rate`、`duration_sec`、`sql_source`、`status`、`error_msg`、`variables_json`、`groups_json`
- `tasks`：`sql_id → statements` 映射（`resolve_tasks(run)`，与压测同源）

### 2.2 现有 `rule_engine.py` 的问题

现有 5 条规则：`p99_high`、`slow_stmts`、`conn_high`、`err_rate`、`qps_low`。问题：

1. **与 EXPLAIN 自相矛盾**：`p99_high` 的文案写"用 EXPLAIN 确认执行计划"，但整个 L1 从未读过 EXPLAIN 产物。EXPLAIN 采集（`explain_probe`）已经做了，产物就躺在 `data/explain/`，报告侧零消费。
2. **只吃聚合标量**：`metrics_in` 只有 7 个标量。`slowest_sql_avg_ms` 只取了"平均最慢的一条"，**丢掉了 per-SQL 的 P95/P99**，也丢掉了"哪条语句失败最多"。
3. **引擎指标只用了 2 个**：`slow_inc` / `lock_waits_inc` 只求和后被当成孤立数字，没参与任何归因；`tmp_disk_inc`、`bufpool_hit`、`mysql_qps`、`threads_connected` **完全没用**。
4. **无时序视角**：P99 是否随并发爬坡恶化、是否抖动、QPS 是否平台期 —— 都看不到。
5. **输出是扁平 bullet**：`[{rule_id, level, title, detail}]`，没有"定位 → 证据 → 根因 → 修复"结构，不能作为交付物，也无法喂给 L2。
6. **阈值硬编码在 `RULES` 元组里**，不可配置、不可按项目调整、不便于分级测试。
7. **没有置信度概念**：样本不足（请求数很少）、压测未跑满、采集缺失时，仍然照样给结论。

### 2.3 前置小修（L1 开工前必须处理）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| P1 | `col_sum()` 用 `sum(...) or None`，导致"0 条慢查询"与"数据缺失"都变成 `None` | `report.py:94-95` | L1 无法区分"确实没有慢查询"与"没采到"。必须区分，否则 B 层规则全部无法给出"正常"结论 |
| P2 | `percentile()` 用 `round()` 取索引，样本少时偏差大 | `report.py:39-44` | 影响 `threads_running_p95` 的可信度；建议补线性插值或标注口径 |
| P3 | per-SQL 时序（A'）没有进报告链路 | `report.py` | L1 的抖动/长尾规则需要它 |
| P4 | `bufpool_hit` 未进报告 | `report.py` | B 层需要它 |

> P1 是最关键的一条：**没有"0 与缺失"的区分，B 层规则写不出来**。建议把 `col_sum` 改为返回 `(value, present)` 或统一用 `None=缺失 / 0=真实为零`，并在 L0 里显式标注数据可用性。

---

## 3. L1 设计原则与边界

1. **纯函数**：`L1Input` 进，`L1Report` 出。不读文件、不连库、不碰 LLM（与 `explain_view.py` 同风格）。读产物由调用方完成。
2. **证据优先**：任何一条 finding 必须能指回具体证据（哪个 `sql_id`、哪张表、哪个 EXPLAIN finding、哪个 metrics 列）。禁止无证据的结论。
3. **缺失即降级，不是报错**：输入缺失时，相关规则静默跳过并在"采集完整性"章节声明，绝不抛异常（与 `explain_probe` 的"不阻断"承诺一致）。
4. **阈值集中、可配置**：全部阈值走 `config.py`，默认值写在第 11 节，便于组会调整。
5. **可复现**：相同输入 → 相同输出（无随机、无时间依赖）。
6. **粒度诚实**：EXPLAIN 与压测账本的粒度是 **task / `sql_id`**（一个 task 内多条语句只上报一次计时）。归因到 `sql_id` 后要展开 `statements[]`，**不能假设一对一**。

---

## 4. 数据契约

### 4.1 输入

```text
L1Input
├── run:        {run_id, name, status, concurrency, spawn_rate,
│                duration_sec, actual_duration_sec, sql_source, error_msg}
├── ledger:     L0 聚合（A 的全部标量）+ per_sql[]（A 的分语句）
├── series:     {sql_id: [{ts, rps, fails, avg_ms, p95_ms, p99_ms}]}  ← A'
├── engine:     {points, threads_running_p95, threads_connected_max,
│                mysql_qps_avg, slow_total, lock_waits_total, tmp_disk_total,
│                bufpool_hit_last, lock_waits_rate_max}                  ← B
├── plan:       EXPLAIN 产物（C）或 None
├── target:     {max_connections, server_version}                       ← D
└── tasks:      [{sql_id, weight, statements[]}]                        ← E
```

### 4.2 输出

```text
Finding
├── rule_id:    str            # 如 "X1_root_index_missing"
├── layer:      "ledger" | "engine" | "plan" | "cross"
├── severity:   "error" | "warn" | "info"
├── title:      str            # 一句话结论
├── scope:      {sql_id?, table?}     # 定位
├── evidence:   [{source: "ledger"|"engine"|"plan", label, value, detail}]
├── root_cause: str            # 确定性根因假设
└── actions:    [{layer: "sql"|"index"|"config"|"benchmark", text}]

L1Report
├── verdict:    {level, title, confidence, summary}
├── findings:   [Finding]（按 severity → 证据强度排序）
├── per_sql:    [{sql_id, sql, requests, failures, avg/p95/p99/max, qps,
│                 plan_findings[], hit_rules[]}]
├── engine_view:{slow_total, lock_waits_total, tmp_disk_total, bufpool_hit,
│                 threads_running_p95, mysql_qps_avg, points}
├── completeness:{plan_available, plan_reason, metrics_points,
│                 sample_sufficient, duration_ratio, series_available}
└── thresholds: 生效的阈值快照（可追溯）
```

### 4.3 与 L2 的契约

`L1Report` 是 L2 的**事实底座**。L2 只做两件事：① 把 `findings` 翻译成自然语言叙述；② 做 L1 无法做的多因推理与优先级权衡。
因此 `Finding` 的 `rule_id` / `severity` / `evidence` 必须稳定 —— 它们会被 L2 引用，也会被报告页用于分组渲染。

---

## 5. 规则目录

四层：**A 账本层**（只看压测结果）、**B 引擎层**（只看 MySQL 运行时）、**C 计划层**（只看 EXPLAIN）、**X 交叉归因层**（≥2 来源同时命中，产出根因）。
阈值代号见第 11 节。

### 5.1 A 层 · 压测账本规则

| rule_id | 触发条件 | 等级 | 证据 | 建议方向 |
|---|---|---|---|---|
| `A1_p99_high` | `p99_ms > T_P99` | warn | 汇总 P99 | 查最慢语句的计划 |
| `A2_p99_severe` | `p99_ms > T_P99_SEV` | error | 汇总 P99 | 同上，升级 |
| `A3_p95_high` | `p95_ms > T_P95` | warn | 汇总 P95 | 中位数之外的尾部普遍偏慢 |
| `A4_long_tail` | `p99/p50 > T_TAIL` 且 `p99_ms > T_P99` | warn | 比值 | 尾部远高于中位数 → 偶发锁/刷盘/GC |
| `A5_slow_statement` | 存在 `per_sql.avg_ms > T_SQL_AVG` | warn | 该语句 avg + 排名 | 列出 Top3，逐条看计划 |
| `A6_slow_statement_severe` | 存在 `per_sql.p99_ms > T_SQL_P99` | error | 该语句 P99 | 优先修复 |
| `A7_err_rate` | `err_rate > T_ERR`(error) / `> T_ERR_WARN`(warn) | error/warn | 失败率 | 见 X7 |
| `A8_failures_concentrated` | 单条语句失败占比 `> T_FAIL_SHARE` 且 `failures>0` | warn | `sql_id` + 占比 | 比聚合错误率更可操作 |
| `A9_throughput_below_expectation` | `qps_peak < concurrency × T_QPS_FACTOR` | info | QPS 峰值 vs 并发 | 存在等待，需 B 层区分锁/IO |
| `A10_throughput_plateau` | 前 50% 窗口有爬坡证据（归一化斜率 ≥ T_PLATEAU）且后 50% 窗口斜率 < T_PLATEAU；全程平稳（无爬坡）不判 | info | 时序 | 已达瓶颈 |
| `A11_stability_jitter` | 时序 `p99` 的变异系数 `CV > T_CV` | info | 时序 | 稳定性差，偶发抖动 |
| `A12_duration_short` | `actual_duration_sec / duration_sec < T_DUR_RATIO` | info | 时长比 | 压测未跑满，置信度下降 |
| `A13_low_sample` | `total_requests < T_MIN_REQ` | info | 请求数 | **样本不足，分位数不可信**（抑制 A 层其它结论的语气） |

> `A13` 是**置信度开关**，不是性能问题。它命中时应把 `verdict.confidence` 降为"低"，并给 A 层分位数结论加"仅供参考"标注。

### 5.2 B 层 · MySQL 引擎规则

| rule_id | 触发条件 | 等级 | 证据 | 建议方向 |
|---|---|---|---|---|
| `B1_slow_queries` | `slow_total > T_SLOW_TOTAL` | warn | 慢查询增量 | 目标库确实记了慢查询（强信号） |
| `B2_lock_waits` | `lock_waits_total > T_LOCK_TOTAL` | warn | 锁等待增量 | 行锁竞争 |
| `B3_lock_waits_heavy` | `lock_waits_rate_max > T_LOCK_RATE` | error | 峰值速率 /s | 严重锁竞争 |
| `B4_tmp_disk_tables` | `tmp_disk_total > T_TMP_TOTAL` | warn | 磁盘临时表增量 | GROUP BY / ORDER BY / DISTINCT 无索引 |
| `B5_bufpool_hit_low` | `bufpool_hit_last < T_HIT` | info | 命中率 | 缓冲池偏小或大量冷读 |
| `B6_bufpool_hit_bad` | `bufpool_hit_last < T_HIT_BAD` | warn | 命中率 | 同上，升级 |
| `B7_conn_near_limit` | `threads_running_p95 / max_connections > T_CONN` | warn | 比值 | 连接接近上限 |
| `B8_threads_exceed_concurrency` | `threads_running_p95 > concurrency × T_TR_FACTOR` | info | 比值 | 有额外连接/后台流量，归因需谨慎 |
| `B9_mysql_qps_amplification` | `mysql_qps_avg / qps_avg > T_AMP` | info | 比值 | 一请求多语句或存在压测外流量 |
| `B10_metrics_missing` | `points < T_MIN_POINTS` | info | 采样点数 | **引擎指标缺失，B 层不可用**（降级声明） |

> `B1` 的绝对量受目标库 `long_query_time` 影响，**只能同库纵向对比，不能跨库横向比较**。报告里要写明这一点。
> `B10` 是降级开关：命中时 B 层其余规则全部跳过，并在完整性章节声明。

### 5.3 C 层 · EXPLAIN 计划规则

| rule_id | 触发条件 | 等级 | 证据 | 建议方向 |
|---|---|---|---|---|
| `C1_plan_table_scan` | `by_code.table_scan > 0` | warn | 表名 + 预估行数 | 全表扫描 |
| `C2_plan_index_ignored` | `by_code.index_ignored > 0` | warn | `possible_keys` | **有索引却没用**（强信号） |
| `C3_plan_full_index_scan` | `by_code.full_index_scan > 0` | info | 表名 | 扫整棵索引树 |
| `C4_plan_filesort` | `by_code.filesort > 0` | warn | 表名 | ORDER BY 未走索引 |
| `C5_plan_temporary` | `by_code.temporary > 0` | warn | 表名 | 临时表（GROUP BY/DISTINCT） |
| `C6_plan_join_buffer` | `by_code.join_buffer > 0` | warn | 表名 | join 无索引 |
| `C7_plan_large_scan` | `summary.max_rows > T_ROWS` | warn | 最大预估行数 + 定位 | 扫描量级大（rows 为估算） |
| `C8_plan_worst_error` | `summary.worst_level == "error"` | error | worst_level | 计划层已判严重 |
| `C9_plan_explain_error` | `by_code.explain_error > 0` | warn | 错误详情 | 语句可能本身有问题 |
| `C10_plan_unavailable` | 产物缺失 | info | 缺失原因 | **计划证据不可用**（降级声明） |

> `C10` 的"缺失原因"要区分三态：`EXPLAIN_PROBE_ENABLED=false`（配置关闭）、孤儿 run（服务重启/进程被杀）、产物被清理。措辞与 `explain_view.build_view()` 的空态提示保持一致，不要另造一套说法。

### 5.4 X 层 · 交叉归因（L1 的核心价值）

X 层规则需要**至少两个来源**同时命中，输出"根因 + 证据三联"。关联方式：`per_sql.sql_id` → `plan.tasks[].sql_id` → `statements[].findings/plan`。

| rule_id | 组合条件 | 等级 | 根因 | 修复建议（分层） |
|---|---|---|---|---|
| `X1_root_index_missing` | A5/A6（语句慢）∧ C1/C2/C3（该语句计划有问题） | warn | 缺索引 / 索引失效 | SQL：改写谓词避免函数包裹列；索引：按 EXPLAIN 的 `table` + WHERE/JOIN 列建复合索引；压测：改后复跑同一 SQL |
| `X2_root_lock_contention` | (A5/A6 或 A9) ∧ B2/B3 ∧ C 层无致命计划问题 | warn | 行锁竞争 | SQL：缩短事务、拆批；配置：检查隔离级别；压测：提高并发复现 |
| `X3_root_tmp_disk` | (A5/A6) ∧ B4 ∧ (C4/C5) | warn | 排序/分组无索引 → 落盘 | 索引：为 ORDER BY/GROUP BY 建索引；SQL：减少 DISTINCT |
| `X4_root_buffer_pool` | (A1/A4) ∧ (B5/B6) ∧ (C1/C7) | warn | 缓冲池不足 / 大范围扫描 | 配置：评估 `innodb_buffer_pool_size`；索引：收窄扫描范围 |
| `X5_root_write_path` | (B2/B3) ∧ C 层读路径健康 | info | **写路径**锁竞争（EXPLAIN 只讲读路径） | 需另查写语句与事务；用 `lock_waits_inc` 定位时间窗 |
| `X6_root_conn_saturation` | (B7/B8) ∧ A9 ∧ C 层健康 | warn | 连接/线程饱和 | 配置：连接池与 `max_connections`；压测：核对并发设定 |
| `X7_root_not_executable` | `err_rate ≈ 1` 或 `status=failed` ∧ `error_msg` 命中语法/权限/表不存在 | error | SQL 本身不可执行（**不是性能问题**） | SQL：先修语法/表名/权限，再谈压测 |
| `X8_root_healthy` | A/B/C 三层无命中 ∧ 样本充足 ∧ 采集完整 | info | 本轮未发现性能问题 | 提高并发或放大数据量再测 |

> **`X8` 必须带前提**：只有"样本充足 + 采集完整"时才敢下"健康"结论；否则输出的是"未发现问题，但证据不足"。

### 5.5 规则求值顺序与去重

1. 先算 A / B / C 三层基础规则（各自独立、互不依赖）。
2. 再算 X 层（依赖前者的命中结果）。
3. **去重**：若 `Xn` 已命中，则其构成它的 A/B/C 子规则**不再单独输出**，改为挂在 `Xn.evidence` 里。否则报告会被"同一件事说三遍"。
4. `A13` / `B10` / `C10` 是**元规则**，不参与去重，始终输出在"采集完整性"章节。

---

## 6. 总评与置信度

### 6.1 等级聚合

```
error 命中 → verdict.level = "severe"
否则 warn 命中 → "needs_optimization"
否则 info 命中 → "attention"
否则          → "healthy"
特例：X7 命中 → "not_usable"（优先于以上全部）
```

### 6.2 置信度（L1 相对 L2 的诚实之处）

```
confidence = 高  当：total_requests ≥ T_MIN_REQ ∧ plan_available ∧ points ≥ T_MIN_POINTS
                  ∧ duration_ratio ≥ T_DUR_RATIO
confidence = 中  当：上述缺失 1 项
confidence = 低  当：缺失 ≥ 2 项（或 status ∈ {failed, cancelled}）
```

低置信度时，`verdict.summary` 必须显式写明"证据不足"及其原因（缺 EXPLAIN / 样本少 / 未跑满）。

---

## 7. 降级矩阵

| 场景 | L1 行为 |
|---|---|
| LLM 未配置 | L1 完整输出，报告头标注 `L1 · 本地规则报告（未配置 LLM）` |
| LLM 配置但调用失败 | 回退 L1，报告头标注 `L2 不可用，已回退 L1`，并保留失败原因（不暴露 Key） |
| EXPLAIN 产物缺失 | C 层跳过，X 层去掉计划证据 → 输出 A/B 层，完整性章节声明"无执行计划证据" |
| `EXPLAIN_PROBE_ENABLED=false` | 同上，声明原因="配置关闭"（与孤儿 run 区分） |
| `metrics` 表为空 / 点数不足 | B 层跳过，声明"引擎指标缺失" |
| per-SQL 时序缺失 | 跳过 A4/A10/A11（时序类），其余照常 |
| 样本不足（`A13`） | 分位数结论加"仅供参考"，置信度降级 |
| 压测未跑满（`A12`） | 声明"时长未达计划"，置信度降级 |
| `status=failed` / `cancelled` | 优先输出失败归因（`X7`），其余结论标注"基于不完整数据" |
| 目标库连不上（`max_connections` 取不到） | `B7` 跳过，声明"未取到 max_connections" |

**原则**：任何降级都只影响"输出什么"，不影响"能否出报告"。L1 永远能出一份结构完整的报告。

---

## 8. 报告输出结构

### 8.1 Markdown 章节（`report.md`）

```
# 压测诊断报告 · {name}
> 报告等级：L1 · 本地规则报告（未配置 LLM）    ← 或「L2 不可用，已回退 L1」
> 生成时间：{ts}   置信度：{高/中/低}

## 1. 结论摘要
   - 总评一句话（verdict.title + summary）
   - 关键数字卡片：QPS 均值/峰值、P95/P99、错误率、慢查询增量、锁等待增量、计划问题数
   - 置信度与理由

## 2. 问题清单（按等级排序）
   每条 Finding 渲染为：
   ### [error] 标题
   - 定位：sql_2 / orders
   - 证据：
     · 账本：sql_2 平均 120ms（最慢）、P99 480ms
     · 引擎：行锁等待增量 320 次，峰值 12/s
     · 计划：orders 表全表扫描（type=ALL，预估 210,000 行）
   - 根因：……
   - 修复建议：
     · [SQL] ……
     · [索引] ……
     · [压测] ……

## 3. 分语句明细
   表格：sql_id | 内容 | 请求 | 失败 | 平均 | P95 | P99 | 最大 | QPS | 计划问题 | 命中规则

## 4. MySQL 侧观察
   慢查询 / 行锁等待 / 磁盘临时表 / 缓冲池命中率 / Threads_running P95 / 目标库 QPS
   （附口径说明：差分量、long_query_time 限制）

## 5. 采集完整性与局限
   - EXPLAIN：probed/statements、worst_level、truncated 原因、错误数
   - 引擎指标：采样点数
   - 样本量、时长比
   - 已知局限清单（见 8.3）

## 6. 附录
   - 生效阈值快照
   - 任务参数（并发、spawn、时长、目标库、MySQL 版本）
   - 规则清单（rule_id → 名称）
```

### 8.2 HTML 视图模型

与 `explain_view.build_view()` 同风格：新增 `l1_view.build_view(l1_report)`，产出模板可直接渲染的结构（`level → CSS 类` 复用 `advice-*`）。
`report.html` 的"L1 规则建议"卡片升级为：**结论摘要卡 → 问题清单卡（含证据三联）→ 分语句表（加"计划问题/命中规则"两列）→ 完整性卡**。

### 8.3 必须在报告里写明的局限（硬性要求）

1. `EXPLAIN` 的 `rows` 是**优化器估算**，不是真实扫描行数；表刚批量导入后统计信息可能过期。
2. `EXPLAIN` **只解释读路径**；写路径的锁竞争要看 `lock_waits_inc`（与 `explain_probe.NOTES` 一致）。
3. 采集发生在**压测结束后**，反映的是那一刻的统计信息。
4. 压测期间发出的 SQL **一条都没被记录**；EXPLAIN 是按模板重放的另一次采样，代表"典型值"，不代表极端值。
5. 慢查询增量受目标库 `long_query_time` 影响，**只能纵向对比**。
6. 归因粒度是 `sql_id`（task 级），一个 task 内多条语句共享一次计时。

---

## 9. 示例输出（合成样例，供讨论体例）

```markdown
# 压测诊断报告 · 订单查询压测
> 报告等级：L1 · 本地规则报告（未配置 LLM）
> 置信度：高（样本充足、计划已采集、跑满时长）

## 1. 结论摘要
**需优化**：P99 延迟 480ms 显著超标，根因定位为 `sql_2` 在 `orders` 表上的
全表扫描，并伴随行锁等待。建议先加索引再复跑。

| 指标 | 值 | 指标 | 值 |
|---|---|---|---|
| QPS 均值/峰值 | 412 / 690 | P95 / P99 | 96 / 480 ms |
| 错误率 | 0.00% | 慢查询增量 | 0 条 |
| 行锁等待增量 | 320 次 | 计划问题 | 3 处 |

## 2. 问题清单

### [warn] sql_2 慢语句根因：orders 表缺索引导致全表扫描
- 定位：`sql_2` / 表 `orders`
- 证据：
  · 账本：`sql_2` 平均 118ms（最慢语句）、P99 480ms、QPS 210
  · 引擎：行锁等待增量 320 次（峰值 12/s）
  · 计划：`orders` 全表扫描（type=ALL，预估 210,000 行）；`possible_keys=customer_id` 但 `key=NULL`
- 根因：谓词未命中索引（有可用索引未被选择），全表扫描放大 IO 并加剧锁等待
- 修复建议：
  · [SQL] 检查 WHERE 是否对 `customer_id` 用了函数或类型不匹配
  · [索引] 按 `customer_id` 建索引（或按 WHERE+ORDER BY 列建复合索引）
  · [压测] 加索引后用同一 SQL 复跑，对比 P99

### [info] 吞吐低于并发预期
- 证据：账本 QPS 峰值 690 < 并发 100 × 0.5 的预期基线 → 存在等待
- 根因：与上一条同源（全表扫描 + 锁等待）
- 修复建议：[压测] 修复 `sql_2` 后复跑，观察 QPS 是否回升

## 5. 采集完整性与局限
- EXPLAIN：已采集 3/4 条语句（1 条为 BEGIN，不可 EXPLAIN）；worst_level=warn
- 引擎指标：采样 62 点；样本 25,600 请求；时长比 1.00
- 局限：rows 为估算值；EXPLAIN 只讲读路径；慢查询受 long_query_time 影响
```

> 示例刻意展示：**同源问题合并**（"吞吐低"挂到 `X1` 下，不单独重复一遍）、**证据三联**、**建议按层标注**。

---

## 10. 实现方案（编码指导）

### 10.1 文件布局

```
app/services/l1/
├── __init__.py          # 导出 build(), render_markdown()
├── contract.py          # L1Input / Finding / L1Report dataclass + 构造辅助
├── thresholds.py        # 阈值 dataclass + 从 settings 读取 + 快照
├── evidence.py          # A/A'/B/C/D/E → L1Input 的组装（唯一碰外部数据的地方）
├── rules_ledger.py      # A 层
├── rules_engine.py      # B 层
├── rules_plan.py        # C 层
├── rules_cross.py       # X 层 + 去重
└── render.py            # render_markdown() + build_view()（HTML 视图模型）
```

### 10.2 关键接口

```text
evidence.collect(run, l0, metrics_rows, series, plan_artifact, target) -> L1Input
l1.build(inp: L1Input) -> L1Report          # 纯函数，L1 的唯一入口
l1.render_markdown(run, l1_report) -> str   # 替代现有 report.render_markdown 的 L1 段
l1_view.build_view(l1_report) -> dict       # HTML 视图模型
```

### 10.3 与现有代码的集成点

| 现有 | 改法 |
|---|---|
| `report.generate_report()` 里 `l1 = evaluate(metrics_in)` | 换成 `l1_report = l1.build(evidence.collect(...))` |
| `report.render_markdown(run, l0, l1)` | L1 段落改由 `l1.render_markdown()` 产出 |
| `rule_engine.evaluate()` | **保留为薄壳**（委托 `l1.build` 并返回旧结构），避免破坏 `tests/unit/test_rule_engine.py`；标注 deprecated，后续单独 PR 移除 |
| `reports.l1_json` | 结构从 `list[dict]` 升级为 `L1Report` dict。**读取侧必须兼容旧 list**（`db.get_report` 里做一次形状判断），否则历史报告页会 500 |
| `report.html` | "L1 规则建议"卡片按 8.2 升级 |
| `config.py` | 新增第 11 节全部阈值项（`l1_*` 前缀） |

### 10.4 测试计划

| 测试文件 | 覆盖 |
|---|---|
| `tests/unit/test_l1_rules_ledger.py` | A 层逐条规则的边界值（恰好在阈值上/下） |
| `tests/unit/test_l1_rules_engine.py` | B 层 + `B10` 缺失降级 |
| `tests/unit/test_l1_rules_plan.py` | C 层 + `C10` 三态缺失原因 |
| `tests/unit/test_l1_rules_cross.py` | X 层组合命中 + **去重**（子规则不再单独出现） |
| `tests/unit/test_l1_verdict.py` | 等级聚合与置信度四档 |
| `tests/unit/test_l1_render.py` | Markdown 章节完整性；空态/降级态不抛异常 |
| `tests/unit/test_l1_contract.py` | `L1Report` 可 JSON 序列化（要落 `l1_json`） |

**测试数据**：构造固定 fixture（`l0` + `metrics` 行 + EXPLAIN 产物），**不连库、不读真实文件**，与 `test_explain_view.py` 同风格。

### 10.5 工作量预估

| 项 | 人日 |
|---|---|
| 前置小修（P1–P4） | 0.5 |
| contract / thresholds / evidence | 1 |
| A / B / C 三层规则 | 1.5 |
| X 层 + 去重 + verdict | 1 |
| render（MD + HTML 视图） | 1 |
| 集成 + 落库兼容 + 报告页改造 | 1 |
| 测试 | 1.5 |
| **合计** | **约 7.5 人日** |

---

## 11. 阈值默认值汇总（**待组会确认**）

| 代号 | 含义 | 默认 | 依据 |
|---|---|---|---|
| `T_P99` | 汇总 P99 告警线 | 100 ms | 沿用现有 `p99_high` |
| `T_P99_SEV` | 汇总 P99 严重线 | 500 ms | 新增，需团队定 |
| `T_P95` | 汇总 P95 告警线 | 50 ms | 新增 |
| `T_TAIL` | P99/P50 长尾比 | 10 | 新增 |
| `T_SQL_AVG` | 单语句平均告警线 | 50 ms | 沿用现有 `slow_stmts` |
| `T_SQL_P99` | 单语句 P99 严重线 | 500 ms | 新增 |
| `T_ERR` / `T_ERR_WARN` | 错误率 error / warn | 1% / 0.1% | 沿用 + 细分 |
| `T_FAIL_SHARE` | 单语句失败占比 | 80% | 新增 |
| `T_QPS_FACTOR` | QPS 峰值/并发 下限 | 0.5 | 沿用现有 `qps_low` |
| `T_PLATEAU` | 平台期斜率阈值 | 0.05 | 新增，需实测校准 |
| `T_CV` | 时序 P99 变异系数 | 0.5 | 新增 |
| `T_DUR_RATIO` | 实际/计划时长下限 | 0.8 | 新增 |
| `T_MIN_REQ` | 最小样本请求数 | 1000 | 新增 |
| `T_SLOW_TOTAL` | 慢查询增量阈值 | 0 | 新增（>0 即报） |
| `T_LOCK_TOTAL` | 锁等待增量阈值 | 0 | 新增 |
| `T_LOCK_RATE` | 锁等待峰值速率 | 10 /s | 新增 |
| `T_TMP_TOTAL` | 磁盘临时表增量阈值 | 0 | 新增 |
| `T_HIT` / `T_HIT_BAD` | 缓冲池命中率 info / warn | 0.95 / 0.90 | 新增 |
| `T_CONN` | threads_running/max_conn | 0.8 | 沿用现有 `conn_high` |
| `T_TR_FACTOR` | threads_running/并发 | 1.0 | 新增 |
| `T_AMP` | 目标库 QPS/应用 QPS | 3 | 新增 |
| `T_MIN_POINTS` | 最小采样点数 | 3 | 新增 |
| `T_ROWS` | 预估扫描行数告警线 | 10000 | 新增 |

> 建议：**首版全部走默认值上线**，用 2–3 次真实压测校准 `T_PLATEAU` / `T_CV` / `T_ROWS`，再固化。

---

## 12. 开放问题（组会确认项）

1. **`l1_json` 结构升级的兼容策略**：一次性迁移历史报告，还是读取侧兼容双形状？（建议后者，成本低）
2. **`T_P99_SEV` 等新增阈值取值**：按"用户体验"还是"数据库承受力"定？不同被测业务差异大，是否需要"按连接预设"？
3. **是否需要"按语句权重归一化"**：权重 1 的语句和权重 100 的语句，同一个 P99 阈值是否公平？
4. **`X` 层规则的根因是否要标注"可能性"**（如"高/中"）？还是统一按"最可能根因"表述？
5. **报告页是否需要"只看问题"过滤**：问题多时（>10 条）是否需要折叠/筛选？
6. **TPS 指标**：是否借这次机会把 `Com_commit` 映射出来（`feature_gap_analysis.md` 已列为缺口）？它会增强 `X2`/`X5` 的写路径证据。
7. **L2 接入后 L1 的定位**：L2 可用时，L1 是仍完整输出（作为"事实清单"），还是折叠为附录？（建议：仍完整输出，L2 段落置于其后）

---

## 13. 非目标（再次强调）

- 不实现 L2（LLM）——本文档只定义 L1 与 L2 的接口边界。
- 不新增采集指标（除第 12 节第 6 项讨论的 TPS 可选增强）。
- 不做自动优化（不自动建索引、不改写 SQL）。
- 不引入外部依赖（`app/ai/` 的 LangChain 等与 L1 无关）。

---

## 14. 实施清单与验收标准

### 14.1 清单

- [ ] 前置小修 P1–P4（`col_sum` 的 0/缺失区分、`percentile` 口径、A' 时序接入、`bufpool_hit` 接入）
- [ ] `config.py` 新增第 11 节全部阈值
- [ ] `contract.py` / `thresholds.py` / `evidence.py`
- [ ] A 层 13 条规则
- [ ] B 层 10 条规则
- [ ] C 层 10 条规则
- [ ] X 层 8 条规则 + 去重
- [ ] `verdict` 等级聚合 + 置信度四档
- [ ] `render_markdown()` + `l1_view.build_view()`
- [ ] `report.generate_report()` 集成 + `l1_json` 兼容读取
- [ ] `report.html` 改造
- [ ] 7 个单测文件

### 14.2 验收标准

1. **零依赖**：断网、无 LLM、无 `cryptography` 环境下，L1 仍能产出完整报告。
2. **可复现**：同一份输入跑两次，`L1Report` 逐字节相同。
3. **降级不炸**：删掉 `data/explain/{run_id}.json`、清空 `metrics` 表、把 `total_requests` 设成 10，三种情况下报告都能生成，且完整性章节有明确声明。
4. **证据可追**：任意一条 finding 的每个 `evidence.value` 都能在输入里找到对应字段。
5. **去重生效**：`X1` 命中时，其子规则（`A5`+`C1`）不再单独出现在 `findings` 里。
6. **报告页与 Markdown 一致**：两者由同一个 `L1Report` 渲染，问题条数与等级一致。
7. **历史兼容**：打开一条旧 run 的报告页，不出现 500（`l1_json` 旧 list 形状可读）。