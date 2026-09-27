# EXPLAIN 采集设计（落点 A）

> 状态：**已实现并在实机验证通过**（2026-09-25，基线 `feature-0916` / `3596854`）
> 归属：压测引擎侧（`app/services/runner.py`、`app/locust_tpl/`）
> 产物契约版本：`probe_version = 3`
>
> **2026-09-25 改动**：采集时机从「压测启动前」改为「**压测结束后**」（见 §3）。
> 文件名与配置项名里的 `probe`、以及文档旧名 "precheck"，都是历史叫法，
> 保留以避免无谓的改动面。

## 1. 为什么做

`app/services/rule_engine.py` **L12** 的规则 `p99_high` 建议文案原文是：

```
"P99={p99_ms:.0f}ms。检查是否有未走索引的查询，用 EXPLAIN 确认执行计划。"
```

即：**EXPLAIN 早就写在产品文案里，但项目从来没有真的执行过一次。**
诊断链路是"只看结果不看原因"：

```
report.py::build_l0()       各条 SQL 按 avg_ms 降序（L92）
        ↓
l0["per_sql"][0]            取最慢那条（report.py L192）
        ↓
rule_engine.evaluate()      命中阈值 → 输出「用 EXPLAIN 确认执行计划」
        ↓
                            到此为止（让用户自己去跑）
```

采集把最后这句空话变成一段真实采集。

## 2. 三条硬约束

改动时别破，每条都有测试守着：

| 约束 | 含意 | 代价 / 守门测试 |
|---|---|---|
| **只读** | 只发 `EXPLAIN`，绝不 `EXPLAIN ANALYZE`、绝不 `ANALYZE TABLE` | 看不到真实行数与锁等待；`TestProbeHappyPath::test_only_read_only_statements_are_sent` + 实机 general log 断言 |
| **不阻断** | 连不上、编译失败、语法错、取值失败都只记账 | 采集失败时用户无感知、报告照常生成；`TestProbeErrors` 全部用例 |
| **不碰 schema** | 结果落 `data/explain/{run_id}.json` | 得靠文件传递，不是数据库 |

唯一的例外是 `sample(...)` 占位符：绑定时需要先从目标表读样本
（`SELECT ... LIMIT n`，只读、有上限）。没有 `sample` 占位符时不会发生。

## 3. 采集时机与接入点

调用点只有一个：`app/services/runner.py::LocustRunner._probe_explain_after_run()`。
它由 `_watch()` 在 **run 状态落库之后、`_on_finish`（生成报告）之前** 调用：

```python
code = proc.wait()                     # _watch()：压测进程退出
...                                    # 状态先落库（finished / failed / cancelled）
logger.info("run {} exited code={} status={}", ...)
self._probe_explain_after_run(run_id)  # ① 采集
if self._on_finish:
    self._on_finish(run_id)            # ② 生成报告 —— 报告侧要读产物，顺序不能反
```

方法内部（`tasks` / `dsn` / `variables` 全部从 run 行**重新推导**，
不依赖 `start()` 传进来的状态，所以服务重启也不会因为丢了内存状态而采不到新 run）：

```python
if not settings.explain_probe_enabled:                 # 开关
    return
run = db.get_run(run_id)
tasks = resolve_tasks(run)                             # 与压测共用同一份分组逻辑
explain_probe.probe(run_id, tasks, json.loads(run["db_dsn_json"]),
                    variables=..., phase=explain_probe.PHASE_POST_RUN)   # ← 不抛异常
```

### 为什么时机从「前」挪到了「后」

`EXPLAIN` 问的是"优化器打算怎么执行"，**优化器走完就停、执行器不启动** ——
所以它的答案在压测前后是同一个对象，放在前面**并不更准**。
而放在前面有两个实打实的代价：

- **同步阻塞启动**：`probe()` 是在 `Popen` 之前直接调的，点"开始"之后最多要多等
  `EXPLAIN_BUDGET_SEC`（默认 15 秒）+ 3 秒连接超时。
- **游离状态**：产物会在用户点"开始"之前就出现在 `data/explain/` 里，
  那一刻压测还没跑，页面上会同时存在"计划"和"没有耗时"。

### 这个位置的代价（刻意接受，不是疏漏）

产物依赖 run 走到 `_watch()` 的收尾，因此**采不到**的只有两种：

- **服务重启**：`main.py` 的 `db.recover_orphans()` 在启动时把孤儿 run 直接标 `failed`；
  而 `_watch()` 只对 `self._procs` 里**本进程 Popen 出来的**进程有效，取不到就 `return`，
  采集与报告都不会跑。
- **进程硬崩溃**（`kill -9`、断电）：同理。

**但"用户取消"是能采到的，而且是故意保留的**：`_watch()` 里 `_on_finish` 不判断状态
（`if run["status"] == "running"` 只包住了状态更新那几行），所以 `cancelled` 的 run
也会走一轮采集。这是特意的 —— 语句本身没变，用户中途取消不代表计划没有价值，
而这正是原来"启动前采集"的行为。

### 两个刻意的设计选择

- **抽 `resolve_tasks(run)`**：`start()` 原本内联了"表单分组 / 文本模式"两条来源
  （`groups_json` 优先，否则 `parse_sql_tasks(sql_content)`）。采集必须看**同一份 tasks**，
  否则产物里的 `sql_id` 与压测账本对不上账。抽出来后 `start()` 与采集共用，避免分叉。
- **产物加 `phase` 字段**（`"post_run"`）：目前只有一个取值，留出来是为了以后
  若要补一轮"压测前"的采集时，下游能区分两份产物的来源。

## 4. 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `EXPLAIN_PROBE_ENABLED` | `true` | 总开关，关掉即整条采集不执行 |
| `EXPLAIN_MAX_STATEMENTS` | `50` | 单轮最多发多少条 EXPLAIN |
| `EXPLAIN_BUDGET_SEC` | `15` | 单轮采集总时间上限 |

额度与预算都在**发出 EXPLAIN 之前**判，超出的记 `not_probed` 而不是硬发 ——
因为这是在用户的库上跑的，不能没上限。

> 已知的粗糙之处：超额时**从后往前砍**（按书写顺序），不按重要性。
> 理想做法是按 `weight` 降序、或优先保有坏味道的语句。
> 相关想法见 `explain-primer.md` 里的"靶向采集"讨论。

## 5. 取值链路（模板与编译链路同源，取值不同源）

`feature-0916` 起占位符已从「运行期文本替换」改为「编译期参数绑定」，
所以采集不再自己拼字符串，而是复用压测的同一条编译链路：

```
db.get_run(run_id) → resolve_tasks(run)  →  原始 SQL 模板（与 start() 共用同一函数）
    ↓
compile_tasks(tasks, variables)          →  definitions, compiled
    ↓
values = {"__sample__": sample_cache.sample, **{变量}}      ← 现场重新取值
Statement.bind(values)                   →  (query_with_%s, args)
    ↓
cursor.mogrify(query, args)              →  一条完整可发送的 SQL
    ↓
cursor.execute("EXPLAIN " + full_sql)
```

### 5.1 是「重放模板」，不是「抓取压测跑过的那条 SQL」

压测期间发出的 SQL **一条都没有被记录**：locust 子进程只回传统计
（`stats.csv` 里是 `sql_N` 的耗时/失败数），不回传语句文本，更不回传取值。
所以采集做的是**按 run 行里存的原文重放一次**，不是从运行痕迹里抓一条出来。

⇒ 送进 EXPLAIN 的是「同一条 SQL 模板的**另一个采样点**」，
而不是「压测中某一次请求的原样复现」。这条差别正是 §10 那条局限的由来。

### 5.2 取值的粒度：一 task 一组，与运行期对齐

`_values_for()` **每个 task 调一次**，task 内所有语句共用这一组值 ——
这与运行期语义一致（`sql_user.py.j2` 的 `@task` 方法每次请求只 `values` 一次，
供该 task 内所有语句复用；`var` 引用在组内因此能保持同一个值）。

⚠️ 但**取值容器并不相同**，别把两侧当成同一份实现：

| | 变量定义 | `values["__sample__"]` |
|---|---|---|
| 运行期 `sql_user.py.j2` L31 | `g.sample({})` | **无** |
| 采集侧 `explain_probe._values_for` | `g.sample(values)` | **有**（`SampleCache`） |

差别只在 `sample(...)` 上，且是刻意的 —— 采集侧自己持有目标库连接、可以安全地
只读取样本；压测 worker 没有这个容器，于是 `sample(...)` 在**压测侧 100% 失败**、
在**采集侧正常**。这是上游缺陷（见 §11），纯 CPython 复现见
`tests/manual/verify_param_parity.py`。

**送进 EXPLAIN 的就是压测真正会执行的那条语句**（只是取值是样本）。
这一点让产物里的 `filled` 字段有了可直接对照的价值。

样本值的来源（`app/services/sql_executor.py` 的现成件，未新写）：
- `SampleCache` —— `sample(...)` 类占位符的取样与缓存，与造数功能共用
- 模块级 `random` —— `rand` / `pick` / `randstr` 等

**种子的处理**（`_seeded` 上下文管理器）：`Generator.sample()` 用的是模块级 `random`，
而采集跑在服务进程里。所以临时定种、退出时**原样还原**，避免扰动同进程里并发的造数取值。
效果是**同一个 `run_id` 反复采集结果可复现**（`sample(...)` 类除外，它取决于表里的数据）。
有测试锁住"还原"这个不变量。

顺带记一条语法事实：`{{var(x)}}` 的参数是**字符串字面量**，必须写成
`{{var('x')}}`；写成裸标识符会在编译期被拒（"参数必须是字面量"）。
`run_form.py` L350 用 `repr()` 生成，所以表单里插进去的链接是对的。

## 6. 产物契约

写 `data/explain/{run_id}.json`，**原子替换**（`os.replace`），下游不会读到半截 JSON。
读取用 `explain_probe.load_artifact(run_id)`，不存在或损坏返回 `None` —— 消费方必须容忍。

```jsonc
{
  "run_id": "…",
  "probe_version": 2,
  "generated_at": "2026-09-25T21:35:00",
  "target": { "host", "port", "database", "user" },   // 刻意不含 password
  "ok": true,                  // 连上目标库且编译成功
  "error": null,               // 失败原因（字符串）
  "server_version": "8.0.46",
  "notes": [ "…三条已知局限…" ],
  "truncated": false,
  "truncated_reason": null,    // "max_statements=50" | "budget_sec=15"
  "summary": {
    "tasks", "statements", "probed", "skipped_not_explainable",
    "dropped_by_limit", "errors", "findings",
    "by_code": { "table_scan": 1 },
    "worst_level": "warn",     // null = 被采集的语句都没坏味道
    "max_rows": 48931,
    "max_rows_ref": { "sql_id": "sql_2", "index": 0 },
    "elapsed_ms": 53
  },
  "tasks": [{
    "sql_id": "sql_1", "weight": 50,
    "statements": [{
      "index": 0,
      "original": "SELECT * FROM orders WHERE id = {{rand(1,10000)}}",  // 原文
      "filled": "SELECT * FROM orders WHERE id = 7012",                 // 实际发出
      "parameters": [{ "type": "int", "value": "7012" }],
      "ok": true,
      "plan": [ /* 列白名单，见下 */ ],
      "max_rows": 1,
      "findings": [ /* 见 §7 */ ]
    }]
  }]
}
```

`plan[]` 只保留白名单列（`_PLAN_COLS`），MySQL 加列不会影响产物结构：

```
id, select_type, table, type, possible_keys, key, key_len, ref, rows, filtered, Extra
```

### 粒度：必须双层（`tasks[].statements[]`）

两本账的粒度**天生不同**：

| | 粒度 | 原因 |
|---|---|---|
| 压测账本（metrics / stats.csv） | **task / `sql_id`** | `sql_user.py.j2` 一个 task 一个 `@task`，跑完 task 内所有语句**只上报一次计时** |
| EXPLAIN | **单条语句** | MySQL 的 EXPLAIN 一次只接一条语句，这是接口约束 |

所以产物用双层结构把细的挂回粗的那层。**消费方看到"sql_2 慢"，要按 `sql_id` 取
`statements[]` 再逐条展开，不能假设一对一。** 这也是"逐条全覆盖而不抽样"的决定性理由：
抽样会让报告里出现查不到计划的 `sql_id`，且无法区分"没坏味道"和"没抽到"。

## 7. `findings[].code` 语义

| code | level | 参与 `worst_level` | 触发条件 |
|---|---|---|---|
| `table_scan` | warn | ✅ | `type=ALL` |
| `index_ignored` | warn | ✅ | `key` 为空但 `possible_keys` 有值 |
| `full_index_scan` | info | ✅ | `type=index`（扫整个索引树） |
| `filesort` | warn | ✅ | `Extra` 含 `Using filesort` |
| `temporary` | warn | ✅ | `Extra` 含 `Using temporary` |
| `join_buffer` | warn | ✅ | `Extra` 含 `Using join buffer` |
| `explain_error` | warn | ✅ | MySQL 拒绝这条 EXPLAIN（表不存在、语法错…） |
| `compile_error` | warn | ✅ | 占位符取值失败 / 没有编译结果 |
| `skipped_not_explainable` | info | ❌ **bookkeeping** | `BEGIN` / `COMMIT` / `SET` 等没有执行计划 |
| `not_probed` | info | ❌ **bookkeeping** | 超出 `max_statements` / `budget_sec` |

**为什么 bookkeeping 不计入 `worst_level`**：否则每一轮只要出现 `BEGIN` 就会把等级顶到
`info`，这个字段就废了。`worst_level: null` 的准确含义是
**"被采集的语句都没有坏味道"**，而不是"采集没跑"（那是 `probed == 0`）。

### 7.1 为什么 `BEGIN` / `COMMIT` 是"不发"而不是"发了失败"

`_EXPLAINABLE` 是**白名单**（`SELECT` / `WITH` / `INSERT` / `UPDATE` / `DELETE` / `REPLACE`），
而不是"黑名单排除事务控制词"。这个选择是被 MySQL 的一个语义细节逼出来的，**别化简**：

`EXPLAIN <名字>` 同时是 `DESCRIBE <名字>` 的同义词。所以 `BEGIN` 不会被拒成
"不支持 EXPLAIN"，而是被当成**表名**去解析。实测 MySQL 8.0.46：

| 语句 | 真发 `EXPLAIN` 会得到 | 会被记成 |
|---|---|---|
| `BEGIN` / `COMMIT` / `START TRANSACTION` / `ROLLBACK` / `SAVEPOINT` | `1146 Table '…BEGIN' doesn't exist`（当表名 DESCRIBE 了） | `explain_error` (warn) |
| `SET` / `LOCK TABLES` / `SHOW` / `CALL` / `TRUNCATE` / `USE` | `1064` 语法错 | `explain_error` (warn) |
| `SELECT` / `INSERT` / `UPDATE` / `DELETE` / `REPLACE` / `WITH`（含 CTE） | 正常给出计划 | 正常判定 |

`explain_error` 是 **warn 且参与 `worst_level`**。所以"照发"的后果不是"多一条无害记录"，
而是**每一轮带事务的压测都会凭空报出"发现 2 处计划问题"** —— 而实际上什么都没坏。
白名单 + `skipped_not_explainable`(info/bookkeeping) 才是对的组合。

跳过也不损失覆盖面：事务块里的 `BEGIN` / `COMMIT` 不探，但**段内的 `UPDATE` 照样逐条探**
（`tests/unit/test_explain_probe.py::test_transaction_control_is_skipped_but_update_is_probed`
锁住了这条）。

> 事务本身并非"没问题"，只是**答案不在 EXPLAIN 里**：长事务、锁等待、死锁属于执行侧事实，
> 要看压测采集的 `lock_waits_inc` 与耗时。EXPLAIN 只讲读路径（见 `NOTES`）。

## 8. 展示层（落点 B 的直出部分）

| 文件 | 性质 |
|---|---|
| `app/services/explain_view.py` | 产物 → 视图模型，**纯函数**，不读文件、不连库、无 LLM |
| `app/routers/explain.py` | `GET /api/runs/{run_id}/explain`（原始 JSON）+ `GET /runs/{run_id}/explain`（页面） |
| `app/templates/explain.html` | 继承 `base.html`，复用既有 CSS 类 |
| `app/main.py` | 改 2 行：import + 注册路由（带 `Depends(require_user)`） |
| `app/templates/runs.html` | 改 1 行：历史任务列表的"操作"列加「执行计划」入口 |

**只做事实的直出**：`summary` + `findings` + `plan` 都由模板渲染，
没有一行是 LLM 生成的。"为什么慢 / 该怎么改"那类推理属于 L2（`app/ai/`，仍是预留状态）。

两个陷阱（都有测试）：

1. **`type` 的大小写** —— 产物里 MySQL 原样输出，`ALL` 是大写而 `index`/`const` 是小写。
   `classify_plan` 统一 `.upper()` 之后比较、`access_of` 统一 `.lower()` 之后查表；
   某一侧写字面量就会漏判（**真的踩过**：`type=index` 被静默漏判，是单元测试抓出来的）。
2. **`Extra` 的前缀关系** —— `Using index condition`（ICP，中性）**包含**
   `Using index`（覆盖索引，好）的子串，必须**先判长的**，否则绿标签贴错。

headline 的"发现 N 处"= `by_code` 求和**排除 bookkeeping**，与 `worst_level` 口径一致。

### 卡片密度：不产生执行计划的语句不占卡片位（2026-09-25 晚改）

`BEGIN` / `COMMIT` / `SET` 采集不到计划（见 §7.1）。最初把它们渲染成完整卡片，结果是
"徽章写着「不可 EXPLAIN」+ 正文再复述一遍「'BEGIN' 不可 EXPLAIN」"的零信息块 ——
一个 3 条语句的事务组被撑成 3 张卡片，真正有内容的只有中间那张。

**取舍：收进每组的折叠行，而不是删掉。**

| 方案 | 代价 |
|---|---|
| 完全删除 | 用户写了 3 条只看到 2 条，会以为漏采（这正是 `skipped_not_explainable` 当初要解决的问题） |
| 保留完整卡片 | 纯噪音，且与徽章重复 |
| **折叠成一行（采用）** | 版面清爽，透明度不丢 |

实现上：视图层给出 `shown`（占卡片）/ `skipped`（进折叠行）两组字段，
**`statements` 保持全量不变**（既有契约，`tests/unit/test_explain_view.py` 锁着），
模板只渲染前两组。`skipped_hint` 按原因分两种措辞 ——
「不产生执行计划」与「超出本轮采集额度」不是同一件事，不能共用一句话。

**边界**：`explain_error` / `compile_error` **不进折叠**。错误本身就是线索，
必须保持完整卡片（`test_error_statements_stay_in_the_card_grid` 锁着）。

## 9. 实机验证记录

### 9.1 采集时机改造**前**（启动前采集，run `8e7fbe42`）

环境：MySQL 容器 `sqlpulse-mysql-1`（8.0.46，宿主机 `127.0.0.1:3307`），
本地 uvicorn `127.0.0.1:8080`，登录 `root` / `root`。

`tests/manual/verify_explain_integration.py` —— **47 项断言全过**：

| 组 | 结果 |
|---|---|
| 未登录访问该页 | 303 → `/login?next=…`（鉴权生效） |
| **general log 抓真实语句** | 3 条 `EXPLAIN`，绑定后的字面量；**窗口内 0 条写/DDL 语句** |
| 产物契约 | `probe_version=2`（当时的版本号）、字段齐、`sql_id` 与账本同名、**不含 password**、接口 == 文件 |
| 判定正确性 | 主键点查 `const` 干净；`LIKE '%api%'` → `ALL` + `table_scan`；`max_rows_ref` 指向它 |
| 展示页 | 200，渲染出结论 / 计划表 / 绑定参数 / 三条局限；不存在的 run → 404 |
| 不阻断压测 | 压测 `finished`，**340 请求 / 0 失败**，平均 3.8 ms |

**"只发 EXPLAIN" 是抓 general log 验的**（不是读代码推断）：

```
EXPLAIN SELECT * FROM orders WHERE id = 383
EXPLAIN SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%'
EXPLAIN SELECT * FROM orders WHERE uid = 6295
```

同窗口的其他语句只有连接层的 `SET NAMES utf8mb4` / `SET AUTOCOMMIT = 0` / `SELECT 1`
/ `SELECT VERSION()` —— 来自 `test_dsn`，无写操作。

单元测试：`pytest tests/unit` → **296 passed**（引入前基线 174）。

### 9.2 采集时机改造**后**的复验（2026-09-25 晚，run `abca4a24`）

同一套脚本，改成了"开着 general log 等到底"，**全部断言通过**：

| 组 | 结果 |
|---|---|
| **采集时机** | 压测进行中 `/api/runs/{id}/explain` → **404**、页面 200 但显示空态；收尾后产物才出现 |
| 收尾顺序 | run 状态先变 `finished`，产物随后落盘（`explain_probe.probe()` 在 `_on_finish` 之前） |
| **general log 抓真实语句** | 窗口覆盖「压测 + 收尾」共 402 条语句，其中 `EXPLAIN` **3 条**；窗口内 **0 条写/DDL** |
| 产物契约 | `probe_version=3`、`phase=post_run`、字段齐、`sql_id` 与账本同名、不含 password |
| 判定正确性 | 主键点查 `const`/`PRIMARY`；`LIKE '%api%'` → `ALL` + `table_scan`；`uid=` → `ref`/`idx_uid` |
| 展示页 | 200，新增断言"页面写明采集时机在压测之后"通过；不存在的 run → 404 |
| 压测本身 | `finished`，**352 请求 / 0 失败**，平均 3.5 ms |

**额外验了一条文档里承诺的行为**：用户**取消**的 run 也能采到。

```
run_id = cf70c365  →  stop → status = cancelled
取消前 产物 404  →  收尾后 产物 200，phase = post_run，probed = 1
```

这条重要，因为"不采取消的 run"曾经是我在评估里列出的一个候选取舍；
实测确认 `_watch()` 的分支不区分状态，`cancelled` 照样走到采集 ——
与 §3 的描述一致。

**"服务重启 / 进程硬崩溃采不到"没有做实机验证**，是从代码结构推定的：
`_watch()` 只对 `self._procs` 里本进程 Popen 出来的进程有效，而重启后根本不会再次
调用 `start()`，所以那个线程压根不会起来。要验证它得真的中途杀掉 uvicorn，
属于破坏性操作，没有执行。

### 9.3 实机发现的环境事实

- `access_log`：`COUNT(*)` = **56733**，统计信息估算 **48931**（估算偏小是常见方向）
- `orders`：`COUNT(*)` = 10000，`TABLE_ROWS` = **9837**（统计信息已被刷新过）
- `orders.uid` 现在走 `ref` / `idx_uid` / `rows=7`。
  **历史对照**：2026-09-24 时 `TABLE_ROWS` 只有 `4`，同一条查询被判 `type=ALL`，
  `ANALYZE TABLE orders` 之后才改走 `ref` —— 这是"统计信息过期"最真实的样本，
  也是产物 `notes` 里那条提醒的出处
- `tests/assets/good.sql` 的反直觉形态**完全复现**：
  `WHERE id = FLOOR(1 + RAND()*10000)` → `type=ALL` / `key=NULL` / `rows=9837`。
  它叫 "good" 是指**延迟**好，不是计划干净。**它是压测集成测试在用的共享素材，不要改。**

## 10. 已知局限（产物 `notes` 里原样返回给用户）

1. `rows` 是优化器的**估算值**，不是真实扫描行数；表刚批量导入后统计信息可能过期，
   必要时对目标表执行 `ANALYZE TABLE` 再复跑（采集**不会**自己执行 —— 那是写操作）。
2. EXPLAIN 只解释**读路径**。`INSERT`/`UPDATE`/`DELETE` 的 EXPLAIN 不会真的执行，
   所以看不到锁竞争；写路径的瓶颈要看压测已在采集的 `lock_waits_inc`。
3. 展示的 SQL 是压测真正会发送的那条，但**取值是样本**：普通随机占位符由 `run_id`
   决定种子、同一个 run 可复现；`sample(...)` 类取决于目标表当时的数据。
   它代表「典型值」的计划形态，**不代表极端值**。

   **实证（2026-09-25 晚，容器内置库 `orders`）**：同一句 SQL、同一个索引，
   只有参数值不同，计划直接翻面 ——

   | SQL 形态 | 参数值 | type | key | rows |
   |---|---|---|---|---|
   | `SELECT * FROM orders WHERE uid > ?` | `1`（≈全部 1 万行） | `ALL` | **NULL**（`possible_keys` 明明有 `idx_uid`） | 10057 |
   | `SELECT * FROM orders WHERE uid > ?` | `999`（≈10 行） | `range` | `idx_uid` | 1 |
   | `SELECT * FROM orders WHERE id > ?` | `1` | `range` | `PRIMARY` | 5028 |
   | `SELECT * FROM orders WHERE id > ?` | `9990` | `range` | `PRIMARY` | 10 |

   可操作的判据：**等值命中唯一/主键**（`WHERE id = ?`）这类计划对取值几乎免疫，
   单点采样足够；**范围 / `IN` / `LIKE '?%'`** 这类计划随取值浮动，
   单点采样只代表一个点（第一行那个 `ALL` + 放弃索引，就是采样点落在"大范围"一侧的样子）。
   后者是将来做多采样 / 边界（min-max）采样的理由 —— **尚未做**。

### 尚未做

- **没有并入报告页**：`report.py` / `report.html` / `monitor.html` 一行未动，
  所以报告里那句「用 EXPLAIN 确认执行计划」**目前仍是纯文字**，用户得从历史任务列表
  点「执行计划」过来才看得到答案。要接的话技术上不用改表结构
  （`reports` 表存的是 `l0_json` / `l1_json` 两个 JSON 字符串）。
- **L2 LLM 诊断段**：`app/ai/__init__.py` 仍只有一行注释。
- **`not_probed` 的降级顺序是"砍尾巴"**：语句数超 `max_statements` 时被砍的永远靠后，
  而不是最不重要的。要优化应改成按 `weight` 降序保、或优先保有静态坏味道的语句。

## 11. 顺带发现的一个上游缺陷（在队友的代码里，未修）

**`sample(...)` 占位符在压测运行期取不到值**，但采集侧正常 —— 两边行为不一致。

- 采集（本次实现）：`SELECT * FROM stock WHERE sku = 'SKU493'`，`ok=true`，计划干净
- 压测（`app/locust_tpl/sql_user.py.j2` L31）：**269 / 269 请求全部失败**，run 落到 `failed`

根因：模板里

```python
values = {name: generator.sample({}) for name, generator in _DEFINITIONS.items()}
```

**没有提供 `values["__sample__"]`**，而 `sql_params.Generator.sample()` 对 `name == "sample"`
要求它存在，否则抛 `sample 占位符需要任务级采样缓存`。
造数链路（`sql_executor.py` L323）提供了这个键，所以那个功能是好的 —— 只有压测链路漏了。

复现：`POST /api/runs`，`sql_content = SELECT * FROM stock WHERE sku = {{sample('stock','sku')}};`
（run `f24aa749`）。**没有修**，因为它是压测引擎的语义变更（要在 locust 子进程里建采样通道），
需要先定"每个 worker 是否允许额外查询目标表"。

**不需要起容器、不需要 MySQL 的最小复现**：`tests/manual/verify_param_parity.py`
（纯编译 + 取值，跑两条路径做对照）。2026-09-25 晚跑出的事实：

| 占位符 | 运行期 `sql_user.py.j2` | 采集侧 `explain_probe` |
|---|---|---|
| `rand` / `pick` | OK（与 `values` 无关） | OK（各自独立取值） |
| `var('x')` | OK | OK |
| `sample(...)` | **ValueError: 需要任务级采样缓存** | OK |
| `var(x)`（漏引号） | 两侧都在 compile 期报错，无差异 | — |

注意最后一行：`{{var(x)}}` 的参数是字符串字面量，必须写 `{{var('x')}}`。
这一行曾被误读成"`var` 是死代码"，实际是测试用例写错了语法，**不是缺陷**。
