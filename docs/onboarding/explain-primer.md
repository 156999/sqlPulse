# EXPLAIN 入门与在 sqlpulse 中的接入点

> 面向：本项目初学者。目标不是背 MySQL 手册，而是搞清两件事——
> ① EXPLAIN 到底在说什么；② 它接进这个项目该放在哪儿。
>
> 本文件里的表数据均为**容器内实测确认**过的真实规模（2026-09-25 复核，
> MySQL 8.0.46）：`orders` 10000 行（id 1–10000，主键 + `idx_uid`）、
> `stock` 500 行、`access_log` **56733** 行（`path` 列**无索引**）。
>
> ⚠️ 注意区分两个数：`COUNT(*)` 的真实行数，与 EXPLAIN 用的
> **统计信息估算值**（`access_log` 报 48931、`orders` 报 9837）。
> 两者不一致是常态，详见 §七 坑 2。

---

## 一、EXPLAIN 是什么

一句话：**让 MySQL 只讲计划、不动手的开关。**

一条 SQL 在 MySQL 内部要走两步：

1. **优化器**决定"怎么取这批数据"——走哪个索引、先读哪张表、预计扫多少行；
2. **执行器**按计划真的去读数据。

在 SQL 前面加 `EXPLAIN`，MySQL **走完第 1 步就停住**，把那份计划以表格返回。
它不读数据、不改数据，所以**可以放心在用户的库上跑**。

这一点是理解 EXPLAIN 的分水岭：

| | 普通 `SELECT` | `EXPLAIN SELECT` |
|---|---|---|
| 优化器 | 会走 | 会走 |
| 执行器 | 会真的读数据 | **不启动** |
| 返回什么 | 数据行 | 1 行计划描述 |
| 有副作用吗 | 有（读 IO、占资源） | **没有** |

---

## 二、怎么跑（照着敲）

### 2.1 直接连容器里的 MySQL 手玩

```bash
docker exec -it sqlpulse-mysql-1 mysql -uroot -ps3cret sqlpulse_demo
```

> 注意：本机 Git Bash 里 `docker exec` 有时会崩，改用 PowerShell 执行同一条命令即可。
> 容器内 root 密码是 `s3cret`（compose 的 `MYSQL_ROOT_PASSWORD` 覆盖了 `.env` 里的值）。

进去之后：

```sql
EXPLAIN SELECT * FROM orders WHERE id = 5000;
```

### 2.2 用 Python 从平台侧跑

平台已经有"连目标库取元信息"的先例——`app/services/report.py` **L47**
的 `query_max_connections(dsn)`。EXPLAIN 完全可以复用同一套连接方式，
**不需要新建连接管理**（采集就是这么做的）：

```python
import json, pymysql
from app import db

run = db.get_run("<run_id>")
dsn = json.loads(run["db_dsn_json"])
conn = pymysql.connect(host=dsn["host"], port=int(dsn["port"]), user=dsn["user"],
                       password=dsn["password"], database=dsn["database"],
                       connect_timeout=3, cursorclass=pymysql.cursors.DictCursor)
with conn.cursor() as cur:
    cur.execute("EXPLAIN " + sql_text)
    for row in cur.fetchall():
        print(row)
```

`DictCursor` 是必要的——默认游标返回元组，取不到 `type` / `key` 这些列名。

---

## 三、输出怎么读

### 3.1 列的优先级

只看这四列就够了：**`type` → `key` → `rows` → `Extra`**。
其余（`id` `select_type` `table` `possible_keys` `key_len` `ref` `filtered`）
是排查细节时才回来看的。

| 列 | 含义 |
|---|---|
| `id` | 查询序号，多表 join 会有多行 |
| `select_type` | `SIMPLE` / `PRIMARY` / `SUBQUERY` 等 |
| `table` | 这一步读哪张表 |
| **`type`** | **访问类型，最重要的判断依据** |
| `possible_keys` | 理论上可用的索引 |
| **`key`** | **实际选中的索引，`NULL` = 没走索引** |
| `key_len` | 用了索引的多少字节（联合索引用了几列看这里） |
| `ref` | 拿什么跟索引比（`const` 或某个列名） |
| **`rows`** | **预估要扫多少行，与实测延迟高度正相关** |
| `filtered` | 过滤后剩下百分之几 |
| **`Extra`** | **额外动作，坑都在这儿** |

### 3.2 `type` 的档位（记这个顺序）

完整顺序是：

```
system > const > eq_ref > ref > range > index > ALL
```

| 档位 | 含义 |
|---|---|
| `const` / `eq_ref` | 主键或唯一索引命中，直接定位到一行 |
| `ref` | 走索引等值匹配，可能命中多行 |
| `range` | 索引范围扫描（`BETWEEN`、`>`、`IN`） |
| `index` | 扫整个索引树（比全表略好，因为只读索引） |
| `ALL` | **全表扫描，看到这个就要警觉** |

> 采集代码里有个**真实踩过的坑**：MySQL 原样输出时 `ALL` 是大写、
> `index`/`const` 是小写。`classify_plan()` 把值 `.upper()` 之后比较，
> 却拿小写字面量 `"index"` 去比 → `type=index` 永远匹配不上、被静默漏判，
> 而 `ALL` 那条因为本身就是大写，一直是对的 —— 肉眼极难发现，是单元测试抓出来的。
> **同一个值有两种大小写写法时，务必靠测试，不要靠读代码。**

### 3.3 `Extra` 里的红绿灯

| 出现的词 | 含义 | 判断 |
|---|---|---|
| `Using index` | 只用索引就取到数据，无需回表（覆盖索引） | 好 |
| `Using index condition` | 索引下推，过滤尽量在索引层完成（ICP） | 偏好 |
| `Using where` | 取回数据后还要再过滤一遍 | 一般 |
| `Using filesort` | **额外做了一次排序**（`ORDER BY` 没走索引） | 坏 |
| `Using temporary` | **建了临时表**（`GROUP BY` / `DISTINCT` 常见） | 坏 |
| `Using join buffer` | join 没走索引，靠内存缓冲硬凑 | 坏 |

> 展示层还有一个坑：`Using index condition` **包含** `Using index` 这个子串。
> 先判短的会把"索引下推"（中性）错标成"覆盖索引"（好）。**必须先判长的那个。**

### 3.4 一个诀窍：`possible_keys` 有值但 `key` 是 `NULL`

说明"**索引就在那儿，但优化器没用它**"。常见原因：

- 类型不匹配：字符串列拿数字去比（`WHERE phone = 13800000000`）
- 函数包住了列：`WHERE DATE(ts) = '2026-09-23'`（改成 `ts >= ... AND ts < ...`）
- 前置通配：`LIKE '%api%'`（左侧有 `%` 就用不上索引）

这类是**最容易修、收益最大**的一类。

> 注意：采集只把这三条并列写进 `index_ignored` 的文案里，**不判断具体是哪一个**。
> 要确定是哪个、以及怎么改，得读懂那条 SQL 的实际写法 —— 那是 L2（LLM）该做的事。

### 3.5 反直觉案例：长得像点查，其实是全表扫描

`tests/assets/good.sql` 是压测用的"正常负荷"素材：

```sql
SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);
```

看着是拿主键 `id` 做等值查询，"应该走 PRIMARY 吧"。**2026-09-25 实测**：

```sql
EXPLAIN SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);
-- type=ALL  key=NULL  rows=9837  Extra='Using where'   ← 全表扫描！

EXPLAIN SELECT * FROM orders WHERE id = 5000;   -- 换成字面量
-- type=const  key=PRIMARY  rows=1  Extra=NULL         ← 完美
```

**原因**：`RAND()` 是非确定性函数，优化器没法在编译计划时把它算成一个具体值，
也就无法用主键去"定位"，只能退化成逐行比较。

**两条结论**：

- 判断"会不会走索引"，**看的是谓词能不能被优化器当成常量**，不是看它出现在哪一列上。
  同类还有 `WHERE DATE(created_at) = CURDATE()`（函数包住了列）、
  `WHERE id = (SELECT ...)`（子查询结果非确定）。
- 这解释了为什么它叫 "good"：**"good" 指的是延迟**。
  1 万行全扫在内存里也就 1.6 ms，压测指标很好看；但计划本身不干净。
  **延迟好看 ≠ 计划好** —— 这正是要单独做 EXPLAIN 的理由：
  压测只能告诉你"慢了"，EXPLAIN 才能告诉你"为什么"，反过来也能告诉你
  "现在没慢，但以后表大了会慢"。

---

## 四、用本项目真实数据做的三个对照（2026-09-25 实测）

```sql
-- ① 主键点查
EXPLAIN SELECT * FROM orders WHERE id = 5000;
-- ② 走普通索引
EXPLAIN SELECT * FROM orders WHERE uid = 900;
-- ③ 无索引 + 前置通配 LIKE
EXPLAIN SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';
```

| | ① | ② | ③ |
|---|---|---|---|
| `type` | `const` | `ref` | **`ALL`** |
| `key` | `PRIMARY` | `idx_uid` | **`NULL`** |
| `rows` | `1` | `7` | **`48931`** |
| `Extra` | `NULL` | `NULL` | `Using where` |
| 压测实测平均耗时 | **1.4 ms** | 1 ms 级 | **8.5 ms** |

> ③ 的 `48931` 是统计信息估算值，`COUNT(*)` 真实是 56733 —— 估算值偏小是常见方向。
>
> **② 有个历史对照很值得记**：2026-09-24 时 `orders` 的 `TABLE_ROWS` 只有 `4`
> （实际 1 万行），同一条查询被判成 `type=ALL`、`key=NULL`，
> `ANALYZE TABLE orders` 之后才改走 `ref`。现在统计信息还没过期，所以是干净的 `ref`。
> **看到"一张小表被判全表扫描"时，先怀疑统计信息，不要先怀疑语句。**

**这就是 EXPLAIN 的核心价值**：把压测跑完才知道的 `8.5ms`，
提前翻译成"**扫了 4.9 万行、没用任何索引**"这个原因。

> 补充：`rows` 是**每一步**的估算值，不是总数。多表 join 时
> 要把各步的 `rows` 乘起来看量级。

---

## 五、已经写在项目里、但从没被执行过的那条规则

`app/services/rule_engine.py` **L12**，规则 `p99_high` 的建议文案原文：

```
"P99={p99_ms:.0f}ms。检查是否有未走索引的查询，用 EXPLAIN 确认执行计划。"
```

即：**EXPLAIN 早就出现在产品文案里了，但过去没有任何代码真的去执行它。**
原先的诊断链路是"只看结果不看原因"：

```
report.py::build_l0()      各条 SQL 按 avg_ms 降序（L92）
        ↓
l0["per_sql"][0]           取最慢那条（report.py L192）
        ↓
rule_engine.evaluate()     命中阈值 → 输出「用 EXPLAIN 确认执行计划」
        ↓
                           到此为止（让用户自己去跑）
```

**落点 A 已补上这一环**，把最后那句空话变成了一段真实采集。

---

## 六、接入的三个落点

### 落点 A — 压测结束后采集（引擎侧）· **已实现并实机验证**

**位置**：`app/services/runner.py::LocustRunner._probe_explain_after_run()`，
由 `_watch()` 在 **run 状态落库之后、`_on_finish`（生成报告）之前** 调用。

```python
code = proc.wait()                              # _watch()：压测进程退出
...                                             # 状态先落库（finished/failed/cancelled）
self._probe_explain_after_run(run_id)           # ×  新增：采集
if self._on_finish:
    self._on_finish(run_id)                     # 生成报告 —— 报告要读产物，顺序不能反
```

方法内部（tasks / dsn / variables 从 run 行重新推导，不用 `start()` 传状态）：

```python
if not settings.explain_probe_enabled:          # 一个开关就能整体关掉
    return
tasks = resolve_tasks(run)                      # 表单分组 / 文本模式两条来源，与压测共用
explain_probe.probe(run_id, tasks, json.loads(run["db_dsn_json"]), ...)   # ← 不抛异常
```

- 产出落 `data/explain/{run_id}.json`；只读接口 `GET /api/runs/{run_id}/explain`；
  展示页 `GET /runs/{run_id}/explain`（历史任务列表里有入口，只在 run 到达终态后才显示）。
- 好处：**不碰任何跨岗字段、不改 DB schema**，纯新增。
- **为什么在"后"而不是"前"**：`EXPLAIN` 只问优化器打算怎么执行，答案在压测前后是同一个，
  放在前面并不会更准，却会同步阻塞启动最多 15 秒、还会让产物先于压测出现。
  代价是**服务重启 / 进程硬崩溃的 run 采不到**（孤儿 run 被 `recover_orphans()` 直接标
  `failed`，`_watch()` 不会跑）—— 这是刻意接受的取舍。
- 完整设计（产物契约、findings 语义、决策与已知局限）：
  **`docs/design/explain-collection.md`**
- 验证方式：`tests/manual/verify_explain_integration.py`（47 项断言，含抓 MySQL
  general log 证明"只发过 EXPLAIN"）。实测记录见设计文档 §9。

### 落点 B — 报告里查根因

**位置**：`app/services/report.py::build_l0()`。

- `per_sql` 已按 `avg_ms` 降序排好（L92），最慢那条就是 `per_sql[0]`（L192）。
- 产出：从「语句 sql_2 平均 8.5ms，优先优化」
  升级为「`type=ALL`、`key=NULL`、`rows=48931`，前置通配 `LIKE '%api%'` 导致索引失效」。
- **目前没做**：`report.py` / `report.html` 一行未动，所以报告里那句
  「用 EXPLAIN 确认执行计划」**仍是纯文字**，得从历史任务列表点过来才看得到答案。

**关键好消息：不用改数据库表结构。**
`reports` 表存的是 `l0_json` / `l1_json` 两个 JSON 字符串，
EXPLAIN 结果作为 `l0["explain"]` 塞进去即可。这一点很重要，因为
`runs` 表**没有迁移机制**（`db.py` 的 `_METRIC_ALTERS` 只覆盖 `metrics` 表），
能绕开 schema 变更就绕开。

### 落点 C — 前后对比形成闭环（产品价值，不写代码）

加索引前跑一次 → 加索引 → 再跑一次，两个维度对照：

| 维度 | 来源 |
|---|---|
| 计划变化 | EXPLAIN 的 `type` / `key` / `rows` |
| 效果变化 | 已有的 `per_sql` 实测 `avg_ms` / `p95_ms` |

**"计划变了、延迟也降了"才是优化有效的完整证据。**
产物是纯 JSON、同构可比，所以这个对比是**纯机械比较，也不需要 LLM**。

---

## 七、六个一定会踩的坑

### 坑 1　占位符必须先处理掉

项目里的 SQL 长这样：

```sql
SELECT * FROM orders WHERE id = {{rand(1,10000)}};
```

直接拿去 EXPLAIN 是**语法错误** —— `{{...}}` 是项目自定义语法，MySQL 不认识。

**`feature-0916` 起，占位符已从「运行期文本替换」改成「编译期参数绑定」**：

```
app/services/sql_params.py
    tokens()                 词法分析（识引号、注释、{{...}}）
    parse_generator()        把 {{rand(1,10000)}} 解析成 Generator 对象
    compile_statement()      产出 (带 %s 的 SQL, [生成器...])
    compile_tasks()          批量编译
app/locust_tpl/sql_user.py.j2
    cur.execute(*statement.bind(values))     ← 真正执行时绑值
```

对 EXPLAIN 的影响：**不能拿 `SELECT ... = %s` 去 EXPLAIN**（那本身不是完整语句），
必须先渲染成字面量。采集用的就是压测同款的那一步：

```python
query, args = statement.bind(values)          # → ("SELECT ... WHERE id = %s", (7012,))
full_sql = cursor.mogrify(query, args)        # → "SELECT ... WHERE id = 7012"
cursor.execute("EXPLAIN " + full_sql)
```

`mogrify()` 是 pymysql 的现成方法（它内部做的正是执行时的同一套转义），
所以**送进 EXPLAIN 的就是压测真正会发送的那条语句**。

> 取值从哪来：普通占位符（`rand`/`pick`/`randstr`…）走模块级 `random`，
> 采集临时按 `run_id` 定种、退出时还原，所以**同一个 run 反复采集结果可复现**；
> `sample(...)` 类需要从目标表读样本（`SampleCache`，只读、有 LIMIT 上限）。
>
> 顺带一提：非法的占位符（如 `{{rand(5,1)}}`，下限大于上限）现在**在提交时就被拦住**
> （`run_form.prepare()` 会编译一遍做校验），不会等到压测启动才炸。

### 坑 2　`rows` 是估算，会骗人

`rows` 来自**索引统计信息**，不是真数出来的。表刚被批量导入
（`scripts/seed_demo.sql` 正是这种）之后统计可能过期，`rows` 会离谱。

修法：先 `ANALYZE TABLE orders;` 再 EXPLAIN。

**本机实测（2026-09-24）**：示例库 `orders` 实际 1 万行，
但 `information_schema.tables.TABLE_ROWS` 报的是 **4**。后果：

```sql
EXPLAIN SELECT * FROM orders WHERE uid = 777;
-- ANALYZE 之前: type=ALL  key=NULL     rows=4       ← 明明有 idx_uid 却全表扫
-- ANALYZE 之后: type=ref  key=idx_uid  rows=8       ← 正常了
```

所以看到"**一张小表被判全表扫描**"时，**先怀疑统计信息，不要先怀疑语句**。
产物里的 `notes` 就是提醒这件事的。
（采集**不会**自己执行 `ANALYZE TABLE` —— 那是写操作，违反"只读"约束。）

### 坑 3　写操作要单独对待

项目示例里有 `INSERT` / `UPDATE`。MySQL 8 支持 `EXPLAIN INSERT ...`，
但它**不执行**，所以看不到锁竞争和真实写入代价。

**结论：EXPLAIN 只能解释"读路径"。写路径的瓶颈要靠压测时已经在采集的
`lock_waits_inc` 指标。**

### 坑 4　`EXPLAIN ANALYZE` 是另一回事

| | `EXPLAIN` | `EXPLAIN ANALYZE` |
|---|---|---|
| 真执行吗 | 不会 | **会真的跑一遍**（MySQL 8.0.18+） |
| 用在用户库上 | 安全 | **要谨慎**，有副作用、耗时长 |
| 给什么 | 优化器的估算 | 估算 vs 实际行数的对比 |

名字像，行为完全不同。**绝不能拿 `EXPLAIN ANALYZE` 去跑用户的 `UPDATE` / `DELETE`。**
采集的产物断言里有一条就是"语句列表里出现 `ANALYZE` 即失败"。

### 坑 5　`rows` 是每一步的，不是总数

多表 join 时每一行代表一步，总代价要把各步 `rows` 乘起来看量级，
不能只看单行的最大值。

### 坑 6　`EXPLAIN` 能用 ≠ 压测能跑

**2026-09-25 实测发现的一个上游缺陷**：`sample(...)` 占位符
**采集侧正常、压测侧 100% 失败**。

```sql
SELECT * FROM stock WHERE sku = {{sample('stock','sku')}};
```

- 采集：`filled = SELECT * FROM stock WHERE sku = 'SKU493'`，`ok=true`，计划干净
- 压测：**269 / 269 请求全部失败**，run 落到 `failed`

根因在 `app/locust_tpl/sql_user.py.j2` L31：

```python
values = {name: generator.sample({}) for name, generator in _DEFINITIONS.items()}
```

它**没有提供 `values["__sample__"]`**，而 `Generator.sample()` 对 `sample` 类
占位符要求这个键存在，否则抛 `sample 占位符需要任务级采样缓存`。
造数链路（`sql_executor.py` L323）提供了，所以造数是好的 —— 只有压测链路漏了。

**教训**：EXPLAIN 只证明"语句的形状能被优化器接受"，**不证明它能被成功执行**。
两者是不同的失败面。

---

## 八、下次可以这样跟组长聊

带着这几个问题去，比"我没听懂"有效得多：

1. EXPLAIN 的结果是**采集下来存进报告**，还是只在**报告页展示**？（决定了要不要落库）
2. `type=ALL` 要不要**阻断提交**，还是只**提示**？（涉及对用户库的侵入程度）
3. 只 EXPLAIN **最慢那条**，还是**全部语句**？（前者 1 次查询，后者 N 次，会给用户库增加负担）
4. 要不要**并入报告页**？（现在的答案：只做了独立页面 + 列表入口，报告页没动）
5. 超额时该**砍谁**？（现在是按书写顺序从后往前砍，不按重要性 —— 见下）

> 第 2 条的结论：**逐条全覆盖**。理由是压测账本按 task / `sql_id` 记账、
> 而 EXPLAIN 只能逐条发，中间必须逐条铺满才能在报告里对账；
> 且抽样容易正好抽到干净的那几条 → `worst_level=null` → **"说没事，其实有事"**，
> 假阴性比多发几十次 EXPLAIN 致命。真正该省的是"抽多少条"（`max_statements`）。
>
> 第 5 条是**还没解决**的那个。第 3 条"只 EXPLAIN 最慢那条"看似被否掉了，
> 但如果换成"**采集已经在压测后**"这个前提，它就重新成立了：
> 那时 `data/locust/{run_id}_stats.csv` 里已经有实测的 per-sql 延迟
> （`Name` 列就是 `sql_1/sql_2`，带 `95%`/`99%` 列），
> 完全可以按实测慢到快排、优先探慢的 —— 这就把"砍尾巴"从"按书写顺序"
> 变成"按重要性"，正好补掉第 5 条。
> 代价是要多跑一轮、且和报告生成的顺序绑得更紧，所以还没做。

---

## 九、自测题

1. `EXPLAIN UPDATE orders SET uid = 1 WHERE id = 5;` 会真的改数据吗？为什么？
2. 一条 SQL 的 `possible_keys` 显示 `idx_uid`、`key` 是 `NULL`、`type` 是 `ALL`，
   最可能的原因是什么？
3. 为什么不能直接对 `SELECT * FROM orders WHERE id = {{rand(1,10000)}}` 跑 EXPLAIN？
   采集是怎么把它变成可执行语句的？
4. `reports` 表为什么**不需要**为 EXPLAIN 结果新增列？
5. `EXPLAIN` 和 `EXPLAIN ANALYZE` 最关键的一条区别是什么？
6. 一条语句的 `ok=true`（EXPLAIN 成功）能说明它在压测里一定能成功执行吗？

<details>
<summary>参考答案</summary>

1. 不会。EXPLAIN 只走到优化器，执行器不启动；但它会告诉你"这一步打算怎么改"。
2. 索引在、但优化器没用它——典型是类型不匹配、函数包住列、或前置通配 `LIKE '%x'`。
3. 因为 `{{...}}` 是项目自定义占位符，MySQL 不认识。采集用压测同款的
   `Statement.bind(values)` 先绑值，再用 `cursor.mogrify()` 把 `%s` 渲染成字面量，
   拼出完整 SQL 后才加 `EXPLAIN` 前缀发出去。
4. `reports` 表存的是 `l0_json` / `l1_json` 两个 JSON 字符串，加字段不用改表结构。
   对比 `metrics` 表才有 `PRAGMA table_info` + `ALTER TABLE` 的迁移逻辑。
5. `EXPLAIN ANALYZE` 会**真的执行**语句，有副作用；`EXPLAIN` 不会。
6. 不能。EXPLAIN 只证明优化器能给出计划，不证明执行期不报错 ——
   `sample(...)` 占位符就是一个现成反例（见 §七 坑 6）。

</details>
