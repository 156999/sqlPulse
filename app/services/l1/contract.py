"""L1 报告的数据契约。纯数据结构，不含业务逻辑。

`L1Report.to_json()` 的形状落 `reports.l1_json`，是 L2（LLM 诊断）的
事实底座：rule_id / severity / evidence 会被下游引用，改字段名属于破坏性变更。
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}

# 元规则：参与 findings 输出（完整性章节引用），但不参与 X 层去重。
META_RULES = frozenset({"A13_low_sample", "B10_metrics_missing", "C10_plan_unavailable"})


@dataclass
class Evidence:
    source: str          # "ledger" | "engine" | "plan" | "target"
    label: str
    value: Any           # 原始数值/字符串，可追溯到输入
    detail: str = ""


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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class L1Input:
    run: dict
    ledger: dict          # report.build_l0 的产物
    series: dict          # {sql_id: [{ts, rps, fails, avg_ms, p95_ms, p99_ms}]}
    engine: dict          # evidence._engine_view 的产物
    plan: Optional[dict]  # explain_probe 产物或 None
    target: dict          # {max_connections?, server_version?}
    tasks: list


@dataclass
class L1Report:
    verdict: dict
    findings: list                 # [Finding]
    per_sql: list
    engine_view: dict
    completeness: dict
    thresholds: dict
    legacy: bool = False

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "per_sql": self.per_sql,
            "engine_view": self.engine_view,
            "completeness": self.completeness,
            "thresholds": self.thresholds,
        }


def normalize_l1(raw: Any) -> dict:
    """读取侧兼容：旧 l1_json 是 list[dict]，新的是带 verdict 的 dict。

    历史报告不重算，只做形状包装（legacy=True），渲染层据此降级为
    "旧版规则建议"样式。新形状原样返回。
    """
    if isinstance(raw, dict) and "verdict" in raw:
        out = dict(raw)
        out.setdefault("legacy", False)
        return out
    return {
        "legacy": True,
        "verdict": {
            "level": "legacy", "title": "旧版规则建议",
            "confidence": "-", "summary": "本报告由旧版规则引擎生成，未包含证据三联与根因分析。",
        },
        "findings": [
            {
                "rule_id": (a or {}).get("rule_id", "legacy"),
                "layer": "legacy",
                "severity": (a or {}).get("level", "info"),
                "title": (a or {}).get("title", ""),
                "scope": {}, "evidence": [],
                "root_cause": "", "actions": [],
                "detail": (a or {}).get("detail", ""),   # 旧渲染依赖的字段
            }
            for a in (raw or [])
        ],
        "per_sql": [], "engine_view": {}, "completeness": {}, "thresholds": {},
    }
