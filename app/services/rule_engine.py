"""已废弃：L1 规则引擎迁至 app/services/l1/。

本模块仅保留 evaluate() 以兼容既有调用与测试（tests/unit/test_rule_engine.py）。
新代码请用 app.services.l1.build()。

兼容面刻意收窄：只返回账本层（A）与引擎层（B）的非元规则 ——
旧引擎没有 C（计划）/X（归因）层，也不输出"样本不足"这类置信度声明，
全量返回会破坏既有调用方对"无命中 = 空列表"的预期。
"""
import warnings

from app.config import settings

from app.services.l1.contract import META_RULES


def evaluate(metrics: dict) -> list:
    warnings.warn(
        "rule_engine.evaluate is deprecated; use app.services.l1.build",
        DeprecationWarning, stacklevel=2)
    from app.services.l1 import build as l1_build
    from app.services.l1.contract import L1Input
    from app.services.l1.thresholds import Thresholds

    m = metrics or {}
    inp = L1Input(
        run={"concurrency": m.get("concurrency"), "status": m.get("status")},
        ledger=m,
        series={},
        engine={
            # 旧输入不含引擎采样信息：给足点数跳过 B10（降级声明），
            # 其余引擎指标缺失时对应规则自然不触发
            "points": 999,
            "threads_running_p95": m.get("threads_running_p95"),
        },
        plan=None,
        target={"max_connections": m.get("max_connections")},
        tasks=[],
    )
    report = l1_build(inp, Thresholds.from_settings(settings))
    return [{"rule_id": f.rule_id, "level": f.severity, "title": f.title,
             "detail": f.root_cause or f.title}
            for f in report.findings
            if f.layer in ("ledger", "engine") and f.rule_id not in META_RULES]
