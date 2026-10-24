# SQL Pulse L1 报告 · 代码修改指导方案

- 状态：Draft
- 日期：2026-09-29
- 配套文档：`sqlpulse/docs/l1_report_design.md`（**先读设计文档，再看本文**）
- 本文性质：**逐文件改造指导书**。包含 before/after、函数签名、关键代码骨架。
- 前置约定：本文只给改造方案，**不修改 `app/` 下任何现有文件**。执行时按第 6 节的 PR 顺序推进。

---

## 0. 改动总览

| # | 文件 | 类型 | 预估行数 | 阶段 |
|---|---|---|---|---|
| 1 | `app/services/report.py` | 修改 | ~40 行变更 | 0 + 3 |
| 2 | `app/config.py` | 修改 | +30 行 | 1 |
| 3 | `app/services/l1/__init__.py` | 新增 | ~15 | 2 |
| 4 | `app/services/l1/contract.py` | 新增 | ~120 | 2 |
| 5 | `app/services/l1/thresholds.py` | 新增 | ~80 | 2 |
| 6 | `app/services/l1/evidence.py` | 新增 | ~130 | 2 |
| 7 | `app/services/l1/rules_ledger.py` | 新增 | ~160 | 2 |
| 8 | `app/services/l1/rules_engine.py` | 新增 | ~130 | 2 |
| 9 | `app/services/l1/rules_plan.py` | 新增 | ~130 | 2 |
| 10 | `app/services/l1/rules_cross.py` | 新增 | ~200 | 2 |
| 11 | `app/services/l1/render.py` | 新增 | ~220 | 2 |
| 12 | `app/services/rule_engine.py` | 修改（降级为兼容壳） | ~30 行变更 | 3 |
| 13 | `app/db.py` | 修改（读取兼容） | +5 行 | 3 |
| 14 | `app/routers/pages.py` | 修改 | +3 行 | 3 |
| 15 | `app/templates/report.html` | 修改 | ~70 行变更 | 3 |
| 16 | `tests/unit/test_l1_*.py` | 新增 ×7 | ~450 | 4 |

> **命名冲突提醒**：新模块目录 `app/services/l1/rules_engine.py` 与现有 `app/services/rule_engine.py` 名字接近但含义不同（前者是 L1 的 **B 层 MySQL 引擎规则**，后者是旧的顶层规则引擎）。为避免混淆，建议把 B 层文件命名为 `rules_mysql.py`。**下文统一用 `rules_mysql.py`。**

---

## 1. 阶段 0：前置小修（P1–P4）

这四项是 L1 的地基，**必须在写规则之前完成**，否则 B 层规则无法区分"正常"与"缺失"。

### P1 · `col_sum` 的 0 / 缺失区分

**文件**：`app/services/report.py:94-95`

**before**

```python
    def col_sum(key):
        return sum(m[key] for m in metrics if m.get(key) is not None) or None
```

**after**

```python
    def col_sum(key):
        """返回 (合计, 是否有数据)。0 与"缺失"必须区分：
        慢查询增量真实为 0 是"健康"证据，缺失是"无法判断"。"""
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return (sum(vals), True) if vals else (None, False)
```

调用处（`report.py:125-126`）同步改：

```python
    slow_total, slow_present = col_sum("slow_inc")
    lock_total, lock_present = col_sum("lock_waits_inc")
```

并在 `l0` 里新增：

```python
        "mysql_slow_total": slow_total,
        "mysql_lock_waits_total": lock_total,
        "mysql_slow_present": slow_present,
        "mysql_lock_waits_present": lock_present,
```

### P2 · `percentile` 改线性插值

**文件**：`app/services/report.py:39-44`

**before**

```python
def percentile(values: list, pct: float) -> Optional[float]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    idx = min(len(vs) - 1, max(0, round(pct * (len(vs) - 1))))
    return vs[idx]
```

**after**

```python
def percentile(values: list, pct: float) -> Optional[float]:
    """线性插值分位。样本少时 round() 取整会明显偏移，L1 的
    threads_running_p95 直接依赖它，所以改为插值。"""
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    if len(vs) == 1:
        return vs[0]
    pos = pct * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)
```

> 影响面：`threads_running_p95` 的数值会变（更准）。现有 `tests/unit/` 里若有断言该值，需同步更新。

### P3 · 把引擎侧标量补进 `l0`

**文件**：`app/services/report.py`，`build_l0` 末尾（`return l0` 之前）

```python
    # --- L1 引擎层输入（B 层规则依赖；缺失时为 None，由 L1 声明降级）---
    def _col_max(key):
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return max(vals) if vals else None

    def _col_avg(key):
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    l0["metrics_points"] = len(metrics)
    l0["tmp_disk_total"] = col_sum("tmp_disk_inc")[0]
    l0["tmp_disk_present"] = col_sum("tmp_disk_inc")[1]
    l0["lock_waits_rate_max"] = _col_max("lock_waits_inc")
    l0["mysql_qps_avg"] = _col_avg("mysql_qps")
    l0["threads_connected_max"] = _col_max("threads_connected")
    l0["bufpool_hit_last"] = metrics[-1]["bufpool_hit"] if metrics else None
```

### P4 · per-SQL 时序接入报告链路

**文件**：`app/services/report.py`，`generate_report` 内（读 `l0` 之后）

```python
    from app.services.collector import read_per_sql_history
    series = read_per_sql_history(settings.data_dir / "locust" / f"{run_id}_stats_history.csv")
```

`series` 传给 `evidence.collect(...)`（见 3.3）。

> `read_per_sql_history` 已存在（`collector.py:58`），无需改动，只是此前报告侧从未调用。

---

## 2. 阶段 1：配置层

**文件**：`app/config.py`

在 `Settings` 类里、`explain_*` 字段之后插入（保持平铺、`l1_` 前缀，便于 grep 与测试注入）：

```python
    # ---- L1 硬规则报告阈值（详见 sqlpulse/docs/l1_report_design.md §11）----
    l1_p99_ms: float = 100.0
    l1_p99_severe_ms: float = 500.0
    l1_p95_ms: float = 50.0
    l1_tail_ratio: float = 10.0
    l1_sql_avg_ms: float = 50.0
    l1_sql_p99_ms: float = 500.0
    l1_err_rate: float = 0.01
    l1_err_rate_warn: float = 0.001
    l1_fail_share: float = 0.8
    l1_qps_factor: float = 0.5
    l1_plateau_slope: float = 0.05
    l1_cv: float = 0.5
    l1_duration_ratio: float = 0.8
    l1_min_requests: int = 1000
    l1_slow_total: float = 0.0
    l1_lock_total: float = 0.0
    l1_lock_rate: float = 10.0
    l1_tmp_total: float = 0.0
    l1_bufpool_hit: float = 0.95
    l1_bufpool_hit_bad: float = 0.90
    l1_conn_ratio: float = 0.8
    l1_tr_factor: float = 1.0
    l1_qps_amp: float = 3.0
    l1_min_points: int = 3
    l1_max_rows: int = 10000
```

> 为什么平铺而不是嵌套模型：`pydantic-settings` 的嵌套模型需要 `env_nested_delimiter`，且 `.env` 写法变成 `L1__P99_MS`，对组员不直观。平铺后 `.env` 里直接写 `L1_P99_MS=120` 即可，也能被 `Settings(l1_p99_ms=120)` 在单测里覆盖。

---

## 3. 阶段 2：新模块 `app/services/l1/`

### 3.1 `contract.py` — 数据契约

```python
"""L1 报告的数据契约。纯数据结构，不含业务逻辑。"""
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Evidence:
    source: str          # "ledger" | "engine" | "plan" | "target"
    label: str           # 如 "行锁等待增量"
    value: Any           # 原始数值/字符串（可追溯）
    detail: str = ""     # 人读补充


@dataclass
class Action:
    layer: str           # "sql" | "index" | "config" | "benchmark"
    text: str


@dataclass
class Finding:
    rule_id: str
    layer: str           # "ledger" | "engine" | "plan" | "cross"
    severity: str        # "error" | "warn" | "info"
    title: str
    scope: dict = field(default_factory=dict)      # {sql_id?, table?}
    evidence: list = field(default_factory=list)   # [Evidence]
    root_cause: str = ""
    actions: list = field(default_factory=list)    # [Action]


@dataclass
class L1Input:
    run: dict
    ledger: dict          # l0
    series: dict          # {sql_id: [...]}
    engine: dict          # 见 3.3
    plan: Optional[dict]  # EXPLAIN 产物或 None
    target: dict          # {max_connections, server_version}
    tasks: list


@dataclass
class L1Report:
    verdict: dict
    findings: list                 # [Finding]
    per_sql: list
    engine_view: dict
    completeness: dict
    thresholds: dict
    legacy: bool = False           # 由旧 l1_json 归一化而来

    def to_json(self) -> dict:
        """落 reports.l1_json 用。必须 JSON 可序列化。"""
        return {
            "verdict": self.verdict,
            "findings": [asdict(f) for f in self.findings],
            "per_sql": self.per_sql,
            "engine_view": self.engine_view,
            "completeness": self.completeness,
            "thresholds": self.thresholds,
        }


def normalize_l1(raw) -> dict:
    """读取侧兼容：旧 l1_json 是 list[dict]，新的是 dict。

    返回统一的新形状 dict。历史报告不重算，只做形状包装，
    所以 legacy=True，渲染层据此降级为"旧版规则建议"。
    """
    if isinstance(raw, dict) and "verdict" in raw:
        return raw
    return {
        "legacy": True,
        "verdict": {
            "level": "legacy", "title": "旧版规则建议",
            "confidence": "-",
            "summary": "本报告由旧版规则引擎生成，未包含证据三联与根因分析。",
        },
        "findings": [
            {
                "rule_id": a.get("rule_id", "legacy"),
                "layer": "legacy",
                "severity": a.get("level", "info"),
                "title": a.get("title", ""),
                "scope": {}, "evidence": [],
                "root_cause": "", "actions": [],
                "detail": a.get("detail", ""),   # 兼容旧渲染
            }
            for a in (raw or [])
        ],
        "per_sql": [], "engine_view": {}, "completeness": {}, "thresholds": {},
    }
```

### 3.2 `thresholds.py`

```python
"""L1 阈值快照。冻结后随报告一起落库，保证结论可追溯。"""
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Thresholds:
    p99_ms: float = 100.0
    p99_severe_ms: float = 500.0
    p95_ms: float = 50.0
    tail_ratio: float = 10.0
    sql_avg_ms: float = 50.0
    sql_p99_ms: float = 500.0
    err_rate: float = 0.01
    err_rate_warn: float = 0.001
    fail_share: float = 0.8
    qps_factor: float = 0.5
    plateau_slope: float = 0.05
    cv: float = 0.5
    duration_ratio: float = 0.8
    min_requests: int = 1000
    slow_total: float = 0.0
    lock_total: float = 0.0
    lock_rate: float = 10.0
    tmp_total: float = 0.0
    bufpool_hit: float = 0.95
    bufpool_hit_bad: float = 0.90
    conn_ratio: float = 0.8
    tr_factor: float = 1.0
    qps_amp: float = 3.0
    min_points: int = 3
    max_rows: int = 10000

    @classmethod
    def from_settings(cls, s) -> "Thresholds":
        return cls(
            p99_ms=s.l1_p99_ms, p99_severe_ms=s.l1_p99_severe_ms,
            p95_ms=s.l1_p95_ms, tail_ratio=s.l1_tail_ratio,
            sql_avg_ms=s.l1_sql_avg_ms, sql_p99_ms=s.l1_sql_p99_ms,
            err_rate=s.l1_err_rate, err_rate_warn=s.l1_err_rate_warn,
            fail_share=s.l1_fail_share, qps_factor=s.l1_qps_factor,
            plateau_slope=s.l1_plateau_slope, cv=s.l1_cv,
            duration_ratio=s.l1_duration_ratio, min_requests=s.l1_min_requests,
            slow_total=s.l1_slow_total, lock_total=s.l1_lock_total,
            lock_rate=s.l1_lock_rate, tmp_total=s.l1_tmp_total,
            bufpool_hit=s.l1_bufpool_hit, bufpool_hit_bad=s.l1_bufpool_hit_bad,
            conn_ratio=s.l1_conn_ratio, tr_factor=s.l1_tr_factor,
            qps_amp=s.l1_qps_amp, min_points=s.l1_min_points,
            max_rows=s.l1_max_rows,
        )

    def snapshot(self) -> dict:
        return asdict(self)
```

### 3.3 `evidence.py` — 唯一的输入组装处

```python
"""把 A/A'/B/C/D/E 组装成 L1Input。

本模块是 L1 里**唯一**接触外部数据形状的地方；不读文件、不连库，
所有原始数据由调用方（report.generate_report）读好传进来 —— 这样
l1.build() 与 collect() 都能离线单测。
"""
from app.services.l1.contract import L1Input


def _engine_view(l0: dict) -> dict:
    return {
        "points": l0.get("metrics_points") or 0,
        "slow_total": l0.get("mysql_slow_total"),
        "slow_present": l0.get("mysql_slow_present", False),
        "lock_waits_total": l0.get("mysql_lock_waits_total"),
        "lock_waits_present": l0.get("mysql_lock_waits_present", False),
        "lock_waits_rate_max": l0.get("lock_waits_rate_max"),
        "tmp_disk_total": l0.get("tmp_disk_total"),
        "tmp_disk_present": l0.get("tmp_disk_present", False),
        "bufpool_hit": l0.get("bufpool_hit_last"),
        "threads_running_p95": l0.get("threads_running_p95"),
        "threads_connected_max": l0.get("threads_connected_max"),
        "mysql_qps_avg": l0.get("mysql_qps_avg"),
    }


def collect(run, l0, metrics_rows, series, plan_artifact, target) -> L1Input:
    tasks = _tasks_of(run)          # 复用 runner.resolve_tasks（见下方注意）
    return L1Input(
        run=run, ledger=l0, series=series or {},
        engine=_engine_view(l0), plan=plan_artifact,
        target=target or {}, tasks=tasks,
    )


def _tasks_of(run: dict) -> list:
    # 延迟 import：runner 会 import explain_probe → sql_executor，链条较长，
    # 延迟可以避免 evidence 被单测 import 时触发整条依赖。
    from app.services.runner import resolve_tasks
    try:
        return resolve_tasks(run)
    except Exception:
        return []
```

**完整性判定**（放在 `build()` 里，或单独 `completeness()` 函数，供 render 与 verdict 共用）：

```python
def completeness(inp: L1Input, th) -> dict:
    led = inp.ledger
    plan = inp.plan or {}
    summary = plan.get("summary") or {}
    duration = led.get("duration_sec") or 0
    actual = led.get("actual_duration_sec") or 0
    ratio = (actual / duration) if duration else 0.0
    return {
        "plan_available": bool(inp.plan),
        "plan_reason": _plan_reason(inp.plan),
        "plan_probed": summary.get("probed"),
        "plan_statements": summary.get("statements"),
        "plan_truncated": plan.get("truncated"),
        "plan_truncated_reason": plan.get("truncated_reason"),
        "metrics_points": inp.engine.get("points") or 0,
        "series_available": bool(inp.series),
        "total_requests": led.get("total_requests") or 0,
        "sample_sufficient": (led.get("total_requests") or 0) >= th.min_requests,
        "duration_ratio": round(ratio, 3),
        "duration_sufficient": ratio >= th.duration_ratio,
    }


def _plan_reason(plan) -> str:
    if plan:
        return ""
    # 三态区分，措辞与 explain_view.build_view() 的空态保持一致
    from app.config import settings
    if not settings.explain_probe_enabled:
        return "EXPLAIN 采集被配置关闭（EXPLAIN_PROBE_ENABLED=false）"
    return "产物缺失：可能是压测进程异常退出（孤儿 run）或产物已被清理"
```

### 3.4 `rules_ledger.py` — A 层（13 条）

**统一模式**：每个规则是一个小函数，返回 `list[Finding]`（可能 0 或 1 条）。不用 DSL、不用元组表，便于逐条单测与断点调试。

```python
from app.services.l1.contract import Finding, Evidence, Action


def rules(inp, th) -> list:
    out = []
    led = inp.ledger
    out += _a1_p99_high(led, th)
    out += _a2_p99_severe(led, th)
    out += _a3_p95_high(led, th)
    out += _a4_long_tail(led, th)
    out += _a5_slow_statement(led, th)
    out += _a6_slow_statement_severe(led, th)
    out += _a7_err_rate(led, th)
    out += _a8_failures_concentrated(led, th)
    out += _a9_throughput_below_expectation(led, th)
    out += _a10_throughput_plateau(inp.series, led, th)
    out += _a11_stability_jitter(inp.series, led, th)
    out += _a12_duration_short(led, th)
    out += _a13_low_sample(led, th)
    return out
```

**两个代表性实现**（其余按设计文档 §5.1 的表逐条照写）：

```python
def _a2_p99_severe(led, th):
    v = led.get("p99_ms")
    if not v or v <= th.p99_severe_ms:
        return []
    return [Finding(
        rule_id="A2_p99_severe", layer="ledger", severity="error",
        title=f"P99 延迟严重超标（{v:.0f}ms > {th.p99_severe_ms:.0f}ms）",
        evidence=[Evidence("ledger", "汇总 P99", v, f"阈值 {th.p99_severe_ms:.0f}ms")],
        root_cause="尾部延迟由少数慢语句主导，需定位到具体语句与执行计划",
        actions=[Action("benchmark", "查看第 3 节分语句明细，定位 P99 最高的语句"),
                 Action("sql", "对该语句做 EXPLAIN，确认是否走索引")],
    )]


def _a8_failures_concentrated(led, th):
    per = led.get("per_sql") or []
    total_fail = sum(s.get("failures") or 0 for s in per)
    if total_fail <= 0:
        return []
    worst = max(per, key=lambda s: s.get("failures") or 0)
    share = (worst.get("failures") or 0) / total_fail
    if share < th.fail_share:
        return []
    return [Finding(
        rule_id="A8_failures_concentrated", layer="ledger", severity="warn",
        title=f"失败集中在 {worst['sql_id']}（占全部失败的 {share:.0%}）",
        scope={"sql_id": worst["sql_id"]},
        evidence=[Evidence("ledger", "该语句失败数", worst.get("failures"),
                           f"占 {share:.0%}"),
                  Evidence("ledger", "该语句 SQL", worst.get("sql"), "")],
        root_cause="失败不是全局性的，大概率是该语句自身的语法/表名/权限/约束问题",
        actions=[Action("sql", "单独执行该语句，确认报错信息"),
                 Action("benchmark", "修正后复跑同一压测")],
    )]
```

**A13 是元规则**，不进 `findings` 去重逻辑，但要在 `verdict` 里降置信度（见 3.7）：

```python
def _a13_low_sample(led, th):
    n = led.get("total_requests") or 0
    if n >= th.min_requests:
        return []
    return [Finding(
        rule_id="A13_low_sample", layer="ledger", severity="info",
        title=f"样本不足（{n} 次请求 < {th.min_requests}）",
        evidence=[Evidence("ledger", "总请求数", n, f"阈值 {th.min_requests}")],
        root_cause="分位数在样本不足时不可信，本报告的分位结论仅供参考",
        actions=[Action("benchmark", "提高并发或延长时长后复跑")],
    )]
```

### 3.5 `rules_mysql.py` — B 层（10 条）

```python
def rules(inp, th) -> list:
    eng = inp.engine
    if (eng.get("points") or 0) < th.min_points:
        return [_b10_metrics_missing(eng, th)]     # 降级：其余 B 层规则全部跳过
    out = []
    out += _b1_slow_queries(eng, th)
    out += _b2_lock_waits(eng, th)
    out += _b3_lock_waits_heavy(eng, th)
    out += _b4_tmp_disk_tables(eng, th)
    out += _b5_bufpool_hit_low(eng, th)
    out += _b6_bufpool_hit_bad(eng, th)
    out += _b7_conn_near_limit(eng, inp.target, th)
    out += _b8_threads_exceed_concurrency(eng, inp.run, th)
    out += _b9_mysql_qps_amplification(eng, inp.ledger, th)
    return out
```

**关键实现细节（容易写错的两处）**：

```python
def _b1_slow_queries(eng, th):
    # 必须用 present 区分"真实 0"与"缺失"（见阶段 0 · P1）
    if not eng.get("slow_present"):
        return []
    v = eng.get("slow_total") or 0
    if v <= th.slow_total:          # 默认阈值 0 → 只要 >0 就报
        return []
    return [Finding(
        rule_id="B1_slow_queries", layer="engine", severity="warn",
        title=f"目标库记录了 {v:.0f} 条慢查询",
        evidence=[Evidence("engine", "Slow_queries 增量合计", v,
                           "受目标库 long_query_time 影响，只能同库纵向对比")],
        root_cause="目标库自身认定存在慢查询，与压测观测一致时可信度高",
        actions=[Action("config", "确认目标库 long_query_time 取值，再解读该数值"),
                 Action("sql", "结合第 3 节定位最慢语句")],
    )]


def _b5_bufpool_hit_low(eng, th):
    v = eng.get("bufpool_hit")
    if v is None:
        return []
    if v >= th.bufpool_hit:
        return []
    sev = "warn" if v < th.bufpool_hit_bad else "info"
    return [Finding(
        rule_id="B6_bufpool_hit_bad" if sev == "warn" else "B5_bufpool_hit_low",
        layer="engine", severity=sev,
        title=f"缓冲池命中率偏低（{v:.1%}）",
        evidence=[Evidence("engine", "Innodb_buffer_pool 命中率", v,
                           "累计口径，非差分")],
        root_cause="缓冲池不足以容纳工作集，或存在大量冷读（大范围扫描）",
        actions=[Action("config", "评估 innodb_buffer_pool_size"),
                 Action("index", "收窄扫描范围，减少冷读")],
    )]
```

> `B5`/`B6` 是同一指标的两档，**只输出命中档位最高的那一条**（上面用 `rule_id` 二选一实现）。不要两条都出。

### 3.6 `rules_plan.py` — C 层（10 条）

```python
def rules(inp, th) -> list:
    plan = inp.plan
    if not plan:
        return [_c10_plan_unavailable(inp, th)]
    out = []
    summary = plan.get("summary") or {}
    by_code = summary.get("by_code") or {}
    out += _c1_table_scan(plan, by_code, th)
    out += _c2_index_ignored(plan, by_code, th)
    out += _c3_full_index_scan(by_code)
    out += _c4_filesort(by_code)
    out += _c5_temporary(by_code)
    out += _c6_join_buffer(by_code)
    out += _c7_large_scan(summary, th)
    out += _c8_worst_error(summary)
    out += _c9_explain_error(plan, by_code)
    return out
```

**按 `sql_id` 聚合证据**（C 层规则要能指回具体语句，这是 X 层关联的基础）：

```python
def _plan_indexes(plan: dict) -> dict:
    """{code: [{sql_id, table, detail, rows}]}，供 C 层与 X 层复用。"""
    out: dict = {}
    for t in plan.get("tasks") or []:
        sql_id = t.get("sql_id")
        for st in t.get("statements") or []:
            for f in st.get("findings") or []:
                code = f.get("code")
                out.setdefault(code, []).append({
                    "sql_id": sql_id, "table": f.get("table"),
                    "detail": f.get("detail") or "",
                    "rows": st.get("max_rows"),
                })
    return out


def _c2_index_ignored(plan, by_code, th):
    items = _plan_indexes(plan).get("index_ignored") or []
    if not items:
        return []
    sql_ids = sorted({i["sql_id"] for i in items if i["sql_id"]})
    tables = sorted({i["table"] for i in items if i["table"]})
    return [Finding(
        rule_id="C2_plan_index_ignored", layer="plan", severity="warn",
        title=f"{len(items)} 处存在可用索引但未被使用",
        scope={"sql_id": sql_ids[0] if sql_ids else None,
               "table": tables[0] if tables else None},
        evidence=[Evidence("plan", "涉及语句", ", ".join(sql_ids), ""),
                  Evidence("plan", "涉及表", ", ".join(tables), "")] +
                 [Evidence("plan", i["table"], i["detail"], "") for i in items[:3]],
        root_cause="有可用索引却 key=NULL：类型不匹配、函数包裹列、或前置通配 LIKE '%x'",
        actions=[Action("sql", "检查谓词是否对索引列做了函数/隐式类型转换"),
                 Action("index", "按 WHERE / JOIN 列评估索引"),
                 Action("benchmark", "改后复跑，对比 P99")],
    )]
```

### 3.7 `rules_cross.py` — X 层 + 去重 + verdict

**去重契约**：每条 X 规则声明它"吸收"哪些 rule_id；命中后把这些子规则从 `findings` 里摘掉，改挂到 X 的 `evidence` 上。

```python
# X 规则 → 被它吸收的基础规则
CONSUMES = {
    "X1_root_index_missing": {"A5_slow_statement", "A6_slow_statement_severe",
                              "C1_plan_table_scan", "C2_plan_index_ignored",
                              "C3_plan_full_index_scan"},
    "X2_root_lock_contention": {"B2_lock_waits", "B3_lock_waits_heavy", "A9_throughput_below_expectation"},
    "X3_root_tmp_disk": {"B4_tmp_disk_tables", "C4_plan_filesort", "C5_plan_temporary"},
    "X4_root_buffer_pool": {"B5_bufpool_hit_low", "B6_bufpool_hit_bad", "C7_plan_large_scan"},
    "X6_root_conn_saturation": {"B7_conn_near_limit", "B8_threads_exceed_concurrency"},
}


def apply_cross(inp, th, base_findings: list) -> list:
    hits = {f.rule_id for f in base_findings}
    out = []
    out += _x1(inp, th, hits)
    out += _x2(inp, th, hits)
    out += _x3(inp, th, hits)
    out += _x4(inp, th, hits)
    out += _x5(inp, th, hits)
    out += _x6(inp, th, hits)
    out += _x7(inp, th)
    out += _x8(inp, th, base_findings)
    return out


def dedup(base: list, cross: list) -> list:
    """摘掉被 X 吸收的基础规则；被摘掉的内容以 evidence 形式保留在 X 里。"""
    absorbed = set()
    for x in cross:
        absorbed |= CONSUMES.get(x.rule_id, set())
    kept = [f for f in base if f.rule_id not in absorbed]
    return kept + cross
```

**X1 实现（示范"证据三联"怎么拼）**：

```python
def _x1(inp, th, hits):
    slow = [s for s in (inp.ledger.get("per_sql") or [])
            if (s.get("avg_ms") or 0) > th.sql_avg_ms
            or (s.get("p99_ms") or 0) > th.sql_p99_ms]
    if not slow:
        return []
    plan_idx = _plan_indexes(inp.plan or {})
    plan_codes = ("table_scan", "index_ignored", "full_index_scan")
    slow_ids = {s["sql_id"] for s in slow}
    bad = [i for c in plan_codes for i in plan_idx.get(c, [])
           if i["sql_id"] in slow_ids]
    if not bad:
        return []
    worst = max(slow, key=lambda s: s.get("p99_ms") or 0)
    sql_id = worst["sql_id"]
    tables = sorted({i["table"] for i in bad if i["table"]})
    ev = [
        Evidence("ledger", f"{sql_id} 延迟", worst.get("avg_ms"),
                 f"P95 {worst.get('p95_ms')}ms / P99 {worst.get('p99_ms')}ms"),
        Evidence("plan", "计划问题", ", ".join(sorted({c for c in plan_codes
                 if plan_idx.get(c)})), f"涉及表：{', '.join(tables)}"),
    ]
    if inp.engine.get("lock_waits_present") and (inp.engine.get("lock_waits_total") or 0) > 0:
        ev.append(Evidence("engine", "行锁等待增量",
                           inp.engine["lock_waits_total"], "与全表扫描同源"))
    return [Finding(
        rule_id="X1_root_index_missing", layer="cross", severity="warn",
        title=f"{sql_id} 慢的根因：{', '.join(tables) or '目标表'} 未有效使用索引",
        scope={"sql_id": sql_id, "table": tables[0] if tables else None},
        evidence=ev,
        root_cause="该语句既慢、执行计划又存在全表扫描/索引未被选择，"
                   "两者互为因果，判定为索引问题",
        actions=[Action("sql", "检查 WHERE / JOIN 谓词是否对索引列做函数或类型转换"),
                 Action("index", f"按 {', '.join(tables)} 的 WHERE / JOIN 列评估索引"),
                 Action("benchmark", "加索引后用同一 SQL 复跑，对比 P99")],
    )]
```

**verdict 与置信度**：

```python
def verdict(findings: list, comp: dict) -> dict:
    ids = {f.rule_id for f in findings}
    if "X7_root_not_executable" in ids:
        level, title = "not_usable", "SQL 不可执行，本轮不构成有效压测"
    else:
        sev = {f.severity for f in findings}
        if "error" in sev:
            level, title = "severe", "存在严重问题，需优先修复"
        elif "warn" in sev:
            level, title = "needs_optimization", "存在可优化项"
        elif "info" in sev:
            level, title = "attention", "整体可用，有若干观察项"
        else:
            level, title = "healthy", "未发现性能问题"

    missing = 0
    if not comp.get("sample_sufficient"):
        missing += 1
    if not comp.get("plan_available"):
        missing += 1
    if (comp.get("metrics_points") or 0) < 3:
        missing += 1
    if not comp.get("duration_sufficient"):
        missing += 1
    confidence = "高" if missing == 0 else ("中" if missing == 1 else "低")

    summary = title
    if confidence != "高":
        reasons = []
        if not comp.get("sample_sufficient"):
            reasons.append("样本不足")
        if not comp.get("plan_available"):
            reasons.append("无执行计划证据")
        if (comp.get("metrics_points") or 0) < 3:
            reasons.append("引擎指标缺失")
        if not comp.get("duration_sufficient"):
            reasons.append("未跑满时长")
        summary += f"（置信度{confidence}：{'、'.join(reasons)}）"
    return {"level": level, "title": title, "confidence": confidence, "summary": summary}
```

> 注意 `verdict` 只在 `healthy` 且 `confidence == 高` 时才敢说"未发现性能问题"；否则措辞应退回"未发现问题，但证据不足"。这一点由 `X8` 与 `verdict` 共同保证。

### 3.8 `render.py` — 输出双通道

```python
"""L1Report → Markdown / HTML 视图模型。与 explain_view.py 同风格：
只做映射与排版，不做判断。"""
LEVEL_CLS = {"error": "advice-error", "warn": "advice-warn",
             "info": "advice-info", "legacy": "advice-info"}
ACTION_LABEL = {"sql": "SQL", "index": "索引", "config": "配置", "benchmark": "压测"}
LAYER_LABEL = {"ledger": "压测账本", "engine": "MySQL 引擎",
               "plan": "执行计划", "cross": "综合归因", "legacy": "旧版"}


def render_markdown(run: dict, l0: dict, report: dict) -> str:
    """完整报告 Markdown（含 L0 表 + L1 诊断）。替代 report.render_markdown。"""
    if report.get("legacy"):
        return _render_legacy(run, l0, report)
    lines = []
    lines += _sec_header(run, report)
    lines += _sec_verdict(report)
    lines += _sec_findings(report)
    lines += _sec_per_sql(l0, report)
    lines += _sec_engine(report)
    lines += _sec_completeness(report)
    lines += _sec_appendix(run, l0, report)
    return "\n".join(lines) + "\n"
```

**问题清单渲染（核心段落）**：

```python
def _sec_findings(report) -> list:
    out = ["## 2. 问题清单", ""]
    findings = report.get("findings") or []
    if not findings:
        out += ["本轮无命中规则。", ""]
        return out
    for f in findings:
        out.append(f"### [{f['severity']}] {f['title']}")
        scope = f.get("scope") or {}
        if scope.get("sql_id") or scope.get("table"):
            parts = [str(v) for v in (scope.get("sql_id"), scope.get("table")) if v]
            out.append(f"- 定位：{' / '.join(parts)}")
        if f.get("detail"):                      # 旧版兼容字段
            out.append(f"- {f['detail']}")
        if f.get("evidence"):
            out.append("- 证据：")
            for e in f["evidence"]:
                label = LAYER_LABEL.get(e.get("source"), e.get("source"))
                detail = f"（{e['detail']}）" if e.get("detail") else ""
                out.append(f"  - [{label}] {e['label']}：{e['value']}{detail}")
        if f.get("root_cause"):
            out.append(f"- 根因：{f['root_cause']}")
        if f.get("actions"):
            out.append("- 修复建议：")
            for a in f["actions"]:
                out.append(f"  - [{ACTION_LABEL.get(a['layer'], a['layer'])}] {a['text']}")
        out.append("")
    return out
```

**HTML 视图模型**：

```python
def build_view(report: dict, run: dict = None) -> dict:
    """给 report.html 用。legacy 时降级为旧的两列结构。"""
    findings = report.get("findings") or []
    return {
        "available": bool(report),
        "legacy": bool(report.get("legacy")),
        "verdict": report.get("verdict") or {},
        "verdict_cls": _verdict_cls((report.get("verdict") or {}).get("level")),
        "findings": [_finding_view(f) for f in findings],
        "engine_view": report.get("engine_view") or {},
        "completeness": report.get("completeness") or {},
        "thresholds": report.get("thresholds") or {},
        "error_count": sum(1 for f in findings if f["severity"] == "error"),
        "warn_count": sum(1 for f in findings if f["severity"] == "warn"),
    }
```

### 3.9 `__init__.py`

```python
"""L1 降级报告：硬规则、零外部依赖、LLM 不可用时的默认诊断。

对外只暴露两个函数：
    build(inp, thresholds) -> L1Report
    render_markdown(run, l0, report_dict) -> str
"""
from app.services.l1.contract import L1Input, L1Report, normalize_l1
from app.services.l1 import evidence, rules_ledger, rules_mysql, rules_plan, rules_cross, render
from app.services.l1.contract import SEV_ORDER


def build(inp: L1Input, th) -> L1Report:
    base = []
    base += rules_ledger.rules(inp, th)
    base += rules_mysql.rules(inp, th)
    base += rules_plan.rules(inp, th)
    cross = rules_cross.apply_cross(inp, th, base)
    findings = rules_cross.dedup(base, cross)

    meta = {"A13_low_sample", "B10_metrics_missing", "C10_plan_unavailable"}
    findings.sort(key=lambda f: (SEV_ORDER.get(f.severity, 9),
                                 0 if f.layer == "cross" else 1, f.rule_id))
    comp = evidence.completeness(inp, th)
    return L1Report(
        verdict=rules_cross.verdict(findings, comp),
        findings=findings,
        per_sql=_per_sql_view(inp, findings),
        engine_view=inp.engine,
        completeness=comp,
        thresholds=th.snapshot(),
    )
```

> `meta` 集合（`A13`/`B10`/`C10`）**参与 findings 输出**（放在完整性章节），但不参与去重 —— 与设计文档 §5.5 一致。

---

## 4. 阶段 3：集成改造

### 4.1 `report.py` · `generate_report`

**before**（`report.py:184-209` 的关键部分）

```python
    l0 = build_l0(run)
    dsn = json.loads(run["db_dsn_json"])
    max_conn = query_max_connections(dsn)

    slowest = l0["per_sql"][0] if l0["per_sql"] else {}
    metrics_in = {...}
    l1 = evaluate(metrics_in)

    out_dir = settings.reports_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(run, l0, l1)
    md_path = out_dir / "report.md"
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "metrics.json").write_text(...)

    db.save_report(run_id, l0, l1, str(md_path))
```

**after**

```python
    l0 = build_l0(run)
    dsn = json.loads(run["db_dsn_json"])
    max_conn = query_max_connections(dsn)

    from app.services import explain_probe
    from app.services.collector import read_per_sql_history
    from app.services.l1 import build as l1_build, evidence as l1_evidence, render as l1_render
    from app.services.l1.thresholds import Thresholds

    plan_artifact = explain_probe.load_artifact(run_id)
    series = read_per_sql_history(settings.data_dir / "locust" / f"{run_id}_stats_history.csv")
    th = Thresholds.from_settings(settings)

    inp = l1_evidence.collect(
        run=run, l0=l0, metrics_rows=db.get_metrics(run_id), series=series,
        plan_artifact=plan_artifact,
        target={"max_connections": max_conn,
                "server_version": (plan_artifact or {}).get("server_version")},
    )
    l1_report = l1_build(inp, th)
    l1_dict = l1_report.to_json()

    out_dir = settings.reports_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    md = l1_render.render_markdown(run, l0, l1_dict)
    md_path = out_dir / "report.md"
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps({"l0": l0, "l1": l1_dict}, ensure_ascii=False, indent=2), encoding="utf-8")

    db.save_report(run_id, l0, l1_dict, str(md_path))
```

**同时删除** `report.py` 里旧的 `render_markdown`（L1 段被 `l1/render.py` 取代）。若要保守，可保留函数但改为委托：

```python
def render_markdown(run, l0, l1):
    from app.services.l1.render import render_markdown as _r
    return _r(run, l0, l1)
```

### 4.2 `rule_engine.py` · 降级为兼容壳

现有 `tests/unit/test_rule_engine.py` 依赖 `evaluate(metrics_in) -> list[dict]`。为不破坏测试，保留函数签名，内部委托新引擎：

```python
"""已废弃：L1 规则引擎迁至 app/services/l1/。
本模块仅保留 evaluate() 以兼容既有调用与测试，新代码请用 app.services.l1.build()。
"""
import warnings

from app.config import settings


def evaluate(metrics: dict) -> list:
    warnings.warn("rule_engine.evaluate is deprecated; use app.services.l1.build",
                  DeprecationWarning, stacklevel=2)
    from app.services.l1 import build as l1_build
    from app.services.l1.contract import L1Input
    from app.services.l1.thresholds import Thresholds

    inp = L1Input(run={}, ledger=metrics, series={}, engine={},
                  plan=None, target={}, tasks=[])
    report = l1_build(inp, Thresholds.from_settings(settings))
    return [{"rule_id": f.rule_id, "level": f.severity, "title": f.title,
             "detail": f.root_cause or f.title} for f in report.findings]
```

> ⚠️ 这个壳的规则**不完全等价**于旧 5 条（新引擎 A 层多 8 条）。若 `test_rule_engine.py` 断言"恰好 5 条"会失败。**建议在同一 PR 里把该测试改为断言"旧 5 条对应的新 rule_id 在结果中"**，而不是断言总数。这是刻意的行为变更，需在 PR 描述里写明。

### 4.3 `db.py` · 读取兼容

**before**（`db.py:373-380`）

```python
def get_report(run_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM reports WHERE run_id=?", (run_id,))
    if not row:
        return None
    d = dict(row)
    d["l0"] = json.loads(d.pop("l0_json"))
    d["l1"] = json.loads(d.pop("l1_json"))
    return d
```

**after**

```python
def get_report(run_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM reports WHERE run_id=?", (run_id,))
    if not row:
        return None
    d = dict(row)
    d["l0"] = json.loads(d.pop("l0_json"))
    from app.services.l1.contract import normalize_l1
    d["l1"] = normalize_l1(json.loads(d.pop("l1_json")))   # 兼容旧 list 形状
    return d
```

> 落库形状变了（list → dict），但**读取侧统一归一化**，所以历史报告页不会 500。不需要数据迁移。

### 4.4 `pages.py` · `report_page`

```python
    report = db.get_report(run_id)
    if not report:
        from app.services.report import generate_report
        report = generate_report(run_id)
    from app.services.l1.render import build_view
    l1_view = build_view(report["l1"], run) if report else None
    return templates.TemplateResponse(
        request, "report.html",
        {"run": run, "report": report, "l1_view": l1_view},
    )
```

### 4.5 `report.html` · L1 卡片改造

**替换** `report.html:62-74` 的整个"L1 规则建议"卡片为：

```html
{% if l1_view %}
<div class="card">
  <h3>L1 诊断
    <span class="tag">本地规则报告 · 未配置 LLM</span>
    {% if l1_view.verdict.confidence and l1_view.verdict.confidence != '-' %}
      <span class="tag">置信度 {{ l1_view.verdict.confidence }}</span>
    {% endif %}
  </h3>

  <div class="advice {{ l1_view.verdict_cls }}">
    <strong>{{ l1_view.verdict.title }}</strong>
    <p>{{ l1_view.verdict.summary }}</p>
    <p class="muted">
      严重 {{ l1_view.error_count }} · 警告 {{ l1_view.warn_count }}
    </p>
  </div>

  {% for f in l1_view.findings %}
  <div class="advice {{ f.cls }}">
    <strong>[{{ f.severity|upper }}] {{ f.title }}</strong>
    {% if f.scope_text %}<p class="muted">定位：{{ f.scope_text }}</p>{% endif %}
    {% if f.detail %}<p>{{ f.detail }}</p>{% endif %}
    {% if f.evidence %}
      <ul>
      {% for e in f.evidence %}
        <li><b>[{{ e.source_label }}]</b> {{ e.label }}：{{ e.value }}
            {% if e.detail %}<span class="muted">（{{ e.detail }}）</span>{% endif %}</li>
      {% endfor %}
      </ul>
    {% endif %}
    {% if f.root_cause %}<p>根因：{{ f.root_cause }}</p>{% endif %}
    {% if f.actions %}
      <ul>
      {% for a in f.actions %}
        <li>[{{ a.layer_label }}] {{ a.text }}</li>
      {% endfor %}
      </ul>
    {% endif %}
  </div>
  {% else %}
  <p class="muted">本轮无命中规则。</p>
  {% endfor %}
</div>

{% if l1_view.completeness %}
<div class="card">
  <h3>采集完整性与局限</h3>
  <table class="table">
    <tr><th>执行计划</th><td>
      {% if l1_view.completeness.plan_available %}
        已采集 {{ l1_view.completeness.plan_probed }} / {{ l1_view.completeness.plan_statements }} 条
      {% else %}
        {{ l1_view.completeness.plan_reason }}
      {% endif %}
    </td></tr>
    <tr><th>引擎指标</th><td>{{ l1_view.completeness.metrics_points }} 个采样点</td></tr>
    <tr><th>样本量</th><td>{{ l1_view.completeness.total_requests }} 次请求</td></tr>
    <tr><th>时长比</th><td>{{ l1_view.completeness.duration_ratio }}</td></tr>
  </table>
  <p class="muted">局限：EXPLAIN 的 rows 为估算值；EXPLAIN 只解释读路径；
     慢查询增量受目标库 long_query_time 影响，只能同库纵向对比。</p>
</div>
{% endif %}
{% endif %}
```

配套在 `render._finding_view` 里补模板要用的扁平字段：

```python
def _finding_view(f: dict) -> dict:
    scope = f.get("scope") or {}
    scope_text = " / ".join(str(v) for v in (scope.get("sql_id"), scope.get("table")) if v)
    return {
        **f,
        "cls": LEVEL_CLS.get(f.get("severity"), "advice-info"),
        "scope_text": scope_text,
        "evidence": [
            {**e, "source_label": LAYER_LABEL.get(e.get("source"), e.get("source"))}
            for e in (f.get("evidence") or [])
        ],
        "actions": [
            {**a, "layer_label": ACTION_LABEL.get(a.get("layer"), a.get("layer"))}
            for a in (f.get("actions") or [])
        ],
    }
```

---

## 5. 阶段 4：测试

| 文件 | 关键断言 |
|---|---|
| `test_l1_contract.py` | `L1Report.to_json()` 可 `json.dumps`；`normalize_l1([...])` 返回 `legacy=True`；`normalize_l1({...})` 原样返回 |
| `test_l1_rules_ledger.py` | A1–A13 各自"阈值下不触发 / 阈值上触发"；`A8` 的失败占比边界 |
| `test_l1_rules_mysql.py` | `B10` 在 `points<3` 时**独占输出**（其余 B 层不出现）；`B1` 在 `slow_present=False` 时不触发；`B5/B6` 只出档位高的一条 |
| `test_l1_rules_plan.py` | `C10` 三态原因措辞；`C2` 能按 `sql_id` 聚合出 `scope` |
| `test_l1_rules_cross.py` | **`X1` 命中后 `A5`/`C1`/`C2` 不再出现在 `findings`**（去重）；`X8` 仅在样本充足且采集完整时命中 |
| `test_l1_verdict.py` | 四档 level 聚合；置信度 高/中/低 三档；`X7` 覆盖为 `not_usable` |
| `test_l1_render.py` | 六个章节标题齐全；`legacy` 分支不抛异常；空 findings 输出"本轮无命中规则" |

**测试数据构造约定**：全部用内联 dict fixture，**不读文件、不连库**（与 `tests/unit/test_explain_view.py` 同风格）。建议在 `tests/unit/l1_fixtures.py` 放一个 `make_input(**overrides)` 工厂，避免 7 个文件各写一遍。

---

## 6. PR 拆分与提交顺序

| PR | 内容 | 依赖 | 可独立合并 |
|---|---|---|---|
| PR-A | 阶段 0（P1–P4）+ 对应单测更新 | 无 | ✅ |
| PR-B | 阶段 1（config）+ `l1/contract.py` + `l1/thresholds.py` | 无 | ✅ |
| PR-C | `l1/evidence.py` + A/B/C 三层规则 + 单测 | PR-B | ✅ |
| PR-D | `l1/rules_cross.py`（X 层 + 去重 + verdict）+ 单测 | PR-C | ✅ |
| PR-E | `l1/render.py` + `report.py` 集成 + `db.py` 兼容 + `pages.py` + `report.html` | PR-D | ✅ |
| PR-F | `rule_engine.py` 降级为壳 + 更新其测试 | PR-E | ✅ |

> 每个 PR 独立可合并、独立可回滚。PR-A 是纯修 bug，可以最先合。

---

## 7. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| `l1_json` 形状变更导致历史报告页 500 | 中 | `normalize_l1()` 双形状兼容；PR-E 里加一条针对旧形状的单测 |
| `percentile` 改插值后数值变化 | 低 | 属修正；同步更新相关断言 |
| 新引擎规则数远多于旧 5 条，`test_rule_engine.py` 断言失败 | 低 | PR-F 里把断言从"总数"改为"包含关系"，PR 描述写明行为变更 |
| 阈值默认值不合理导致误报 | 中 | 全部走 `config` 可热调；首版上线后用 2–3 次真实压测校准 `T_PLATEAU`/`T_CV`/`T_ROWS` |
| `evidence.collect` 里 `resolve_tasks` 抛异常 | 低 | 已 try/except 兜底返回 `[]`；C 层缺 tasks 时按 `sql_id` 关联会退化为"无计划证据"，不阻断 |
| 回滚方式 | — | L1 全部代码在 `app/services/l1/` 下，删除目录 + 还原 `report.py` 的 `evaluate()` 调用即可回到旧行为 |

---

## 8. 执行前检查清单

- [ ] 设计文档 §11 的阈值已由组会确认（尤其 `T_P99_SEV` / `T_PLATEAU` / `T_CV` / `T_ROWS`）
- [ ] 设计文档 §12 的 7 个开放问题已拍板
- [ ] `app/services/l1/` 目录名与现有 `app/ai/`、`app/mcp/` 不冲突（已确认）
- [ ] 明确 B 层文件名用 `rules_mysql.py`（避免与 `rule_engine.py` 混淆）
- [ ] 确认 `report.html` 里 `advice-*` CSS 类齐全（`app.css` 已有 `.advice-info/-warn/-error`）
- [ ] 确认 `l1_json` 兼容方案（读取侧归一化，不迁移数据）