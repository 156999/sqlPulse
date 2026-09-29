"""`explain_view` 视图模型的单元测试（展示层）。

重点锁两处最容易错的地方：
1. `type` 的大小写 —— 产物里 MySQL 原样输出，`ALL` 大写而 `index`/`const` 小写；
2. `Extra` 的前缀关系 —— `Using index condition` 包含 `Using index` 子串，
   顺序写反会把"索引下推"错标成"覆盖索引"。
"""
import pytest

from app.services import explain_view as ev


# ---------------------------------------------------------------- 等级映射

@pytest.mark.parametrize("raw,level", [
    ("const", "good"), ("CONST", "good"),
    ("eq_ref", "good"), ("system", "good"),
    ("ref", "ok"), ("range", "ok"), ("ref_or_null", "ok"),
    ("index", "warn"), ("INDEX", "warn"),
    ("all", "bad"), ("ALL", "bad"),
    (None, "unknown"), ("", "unknown"), ("whatever", "unknown"),
])
def test_access_of_is_case_insensitive(raw, level):
    """大小写不一致曾让 `classify_plan` 漏判 `type=index`，这里必须钉住。"""
    assert ev.access_of(raw)[0] == level


def test_access_text_is_chinese():
    assert ev.access_of("ALL")[1] == "全表扫描"
    assert ev.access_of("const")[1] == "常量定位"
    assert ev.access_of("nonsense")[1] == ""


# ---------------------------------------------------------------- Extra 标签

def test_extra_empty():
    assert ev.extra_tags(None) == []
    assert ev.extra_tags("") == []


def test_extra_covering_index():
    tags = ev.extra_tags("Using index")
    assert [t["text"] for t in tags] == ["覆盖索引"]
    assert tags[0]["cls"] == "ex-good"


def test_extra_index_condition_is_not_covering():
    """ICP 必须与覆盖索引区分开：前者中性、后者是好的。"""
    tags = ev.extra_tags("Using index condition")
    assert [t["text"] for t in tags] == ["索引下推"]
    assert tags[0]["cls"] == "ex-ok"


def test_extra_index_condition_with_where():
    """真实形态：`Using index condition; Using where`。"""
    tags = ev.extra_tags("Using index condition; Using where")
    assert [t["text"] for t in tags] == ["索引下推", "where 过滤"]


def test_extra_bad_tokens():
    tags = ev.extra_tags("Using where; Using filesort; Using temporary")
    texts = [t["text"] for t in tags]
    assert "额外排序" in texts and "临时表" in texts and "where 过滤" in texts
    assert all(t["cls"] == "ex-warn" for t in tags if t["text"] in ("额外排序", "临时表"))


def test_extra_join_buffer_case_insensitive():
    assert [t["text"] for t in ev.extra_tags("Using join buffer (hash join)")] == ["join 缓冲"]


def test_extra_no_matching_row():
    """const 查不到行时 MySQL 会带这句，属正常现象，不该当成坏味道。"""
    tags = ev.extra_tags("no matching row in const table")
    assert tags[0]["text"] == "无匹配行"
    assert tags[0]["cls"] == "ex-ok"


def test_extra_table_scan_with_where():
    assert [t["text"] for t in ev.extra_tags("Using where")] == ["where 过滤"]


# ---------------------------------------------------------------- 语句状态

def test_status_probed():
    assert ev._status_of({"ok": True}) == "probed"


@pytest.mark.parametrize("code", [
    "skipped_not_explainable", "not_probed", "compile_error", "explain_error",
])
def test_status_from_findings(code):
    assert ev._status_of({"ok": False, "findings": [{"code": code}]}) == code


def test_status_unknown_when_no_findings():
    assert ev._status_of({"ok": False, "findings": []}) == "unknown"


def test_status_priority_is_fixed_not_list_order():
    """findings 多条时按固定优先级取（而非列表顺序），保证徽章稳定。"""
    e = {"ok": False, "findings": [{"code": "compile_error"}, {"code": "not_probed"}]}
    assert ev._status_of(e) == "not_probed"


def test_status_labels_are_chinese():
    for key, (label, badge) in ev._STATUS.items():
        assert label and badge.startswith("ex-badge")


def test_every_finding_code_has_a_label():
    """`classify_plan` 可能产出的 code 都要有中文标签，否则页面会露出英文枚举。"""
    from app.services import explain_probe
    produced = {"table_scan", "index_ignored", "full_index_scan",
                "filesort", "temporary", "join_buffer",
                "skipped_not_explainable", "not_probed",
                "explain_error", "compile_error"}
    assert produced <= set(ev.CODE_LABEL)
    assert explain_probe.PROBE_VERSION == 3


def test_probe_version_bumps_with_phase_field():
    """`phase` 是 v3 新增的契约字段：下游靠它区分产物是哪个时机采的。"""
    from app.services import explain_probe
    assert explain_probe.PHASE_POST_RUN == "post_run"
    a = _artifact()
    a["phase"] = explain_probe.PHASE_POST_RUN
    assert ev.build_view(a, None)["phase"] == "post_run"


# ---------------------------------------------------------------- 数字格式化

@pytest.mark.parametrize("raw,shown", [
    (48931, "48,931"), (0, "0"), (None, "—"), ("N/A", "—"),
])
def test_fmt_rows(raw, shown):
    assert ev._fmt_rows(raw) == shown


def test_fmt_target():
    assert ev._fmt_target({"host": "127.0.0.1", "port": 3307, "database": "demo"}) == "127.0.0.1:3307/demo"
    assert ev._fmt_target({}) == "—"


# ---------------------------------------------------------------- 绑定参数

def test_params_view_truncates_long_values():
    v = ev._params_view([{"type": "str", "value": "x" * 200}])
    assert v[0]["type"] == "str"
    assert v[0]["value"].endswith("…")
    assert len(v[0]["value"]) == 61


def test_params_view_tolerates_non_dict():
    assert ev._params_view(["7"]) == [{"type": "?", "value": "7"}]


def test_params_view_empty():
    assert ev._params_view(None) == []


# ---------------------------------------------------------------- 结论口径

def _summary(**over):
    base = {"probed": 2, "statements": 3, "worst_level": "warn", "by_code": {"table_scan": 1}}
    base.update(over)
    return base


def test_headline_warn_counts_only_quality_findings():
    """bookkeeping 类（跳过了 BEGIN / 未采集）不能算进"值得关注"的处数。"""
    s = _summary(by_code={
        "table_scan": 1, "filesort": 1,
        "skipped_not_explainable": 5, "not_probed": 3,
    })
    h = ev._headline(s, ok=True, error=None)
    assert "2 处" in h["title"]
    assert h["cls"] == "advice-warn"


def test_headline_healthy_when_worst_is_none():
    h = ev._headline(_summary(worst_level=None, by_code={}), ok=True, error=None)
    assert h["title"] == "计划健康"
    assert h["cls"] == "advice-ok"


def test_headline_nothing_probed():
    h = ev._headline(_summary(probed=0, worst_level=None, by_code={}), ok=True, error=None)
    assert h["title"] == "没有语句被采集"
    assert h["cls"] == "advice-info"


def test_headline_connect_failure_reports_reason():
    h = ev._headline(_summary(), ok=False, error="OperationalError: 连不上")
    assert h["title"] == "采集未完成"
    assert "连不上" in h["detail"]


def test_headline_error_level():
    h = ev._headline(_summary(worst_level="error", by_code={"explain_error": 1}), ok=True, error=None)
    assert h["cls"] == "advice-error"


def test_headline_has_no_markdown_artifacts():
    """模板是 HTML：文案里混进 markdown 星号会被原样显示出来（踩过一次）。"""
    for s in (_summary(), _summary(worst_level=None, by_code={})):
        h = ev._headline(s, ok=True, error=None)
        assert "**" not in h["title"] and "**" not in h["detail"]


# ---------------------------------------------------------------- build_view

def test_build_view_empty_artifact():
    """产物缺失时必须是可渲染的空态，而不是抛异常。"""
    v = ev.build_view(None, {"id": "abc123", "name": "演示"})
    assert v["available"] is False
    assert v["run_id"] == "abc123"
    assert v["run_name"] == "演示"
    assert v["hint"]


def test_build_view_falls_back_to_artifact_run_id():
    """任务行被删但产物还在：run_id 从产物里取。"""
    v = ev.build_view({"run_id": "dead01", "summary": {}}, None)
    assert v["run_id"] == "dead01"
    assert v["run_name"] is None


def _artifact():
    return {
        "run_id": "r1", "probe_version": 2, "generated_at": "2026-09-25T21:20:00",
        "target": {"host": "127.0.0.1", "port": 3307, "database": "sqlpulse_demo", "user": "root"},
        "ok": True, "error": None, "server_version": "8.0.36",
        "notes": ["n1", "n2"], "truncated": False, "truncated_reason": None,
        "summary": {
            "probed": 1, "statements": 1, "worst_level": "warn",
            "by_code": {"table_scan": 1}, "max_rows": 48931,
            "max_rows_ref": {"sql_id": "sql_1", "index": 0}, "elapsed_ms": 43,
        },
        "tasks": [{
            "sql_id": "sql_1", "weight": 60,
            "statements": [{
                "index": 0,
                "original": "SELECT * FROM access_log WHERE path LIKE '%api%'",
                "filled": "SELECT * FROM access_log WHERE path LIKE '%api%'",
                "parameters": [],
                "ok": True, "max_rows": 48931,
                "plan": [{
                    "id": 1, "select_type": "SIMPLE", "table": "access_log", "type": "ALL",
                    "possible_keys": None, "key": None, "key_len": None, "ref": None,
                    "rows": 48931, "filtered": 100.0, "Extra": "Using where",
                }],
                "findings": [{
                    "code": "table_scan", "level": "warn", "table": "access_log",
                    "detail": "表 access_log 全表扫描（type=ALL，预估 48931 行）。",
                }],
            }],
        }],
    }


def test_build_view_maps_findings_and_plan():
    v = ev.build_view(_artifact(), {"id": "r1", "name": "演示"})
    assert v["available"] is True and v["ok"] is True
    st = v["tasks"][0]["statements"][0]
    assert st["status"] == "probed"
    assert st["findings"][0]["cls"] == "advice-warn"
    assert st["findings"][0]["label"] == "全表扫描"
    p = st["plan"][0]
    assert p["access"] == "ALL" and p["access_cls"] == "ex-bad"
    assert p["key_is_null"] is True
    assert p["rows"] == "48,931"
    assert [t["text"] for t in p["extra_tags"]] == ["where 过滤"]
    assert p["possible_keys"] == "—"


def test_build_view_hides_identical_filled():
    """绑定结果与原文相同时不重复展示。"""
    v = ev.build_view(_artifact(), None)
    assert v["tasks"][0]["statements"][0]["filled"] is None


def test_build_view_shows_filled_when_changed():
    a = _artifact()
    a["tasks"][0]["statements"][0]["filled"] = "SELECT * FROM t WHERE id = 7"
    v = ev.build_view(a, None)
    assert v["tasks"][0]["statements"][0]["filled"] == "SELECT * FROM t WHERE id = 7"


def test_build_view_shows_bound_parameters():
    a = _artifact()
    a["tasks"][0]["statements"][0]["parameters"] = [
        {"type": "int", "value": "1434"}, {"type": "str", "value": "SKU7"}]
    st = ev.build_view(a, None)["tasks"][0]["statements"][0]
    assert [p["value"] for p in st["params"]] == ["1434", "SKU7"]


def test_build_view_stats_include_target_and_version():
    v = ev.build_view(_artifact(), None)
    labels = [s["label"] for s in v["stats"]]
    assert labels == ["已采集语句", "预估最大扫描行数", "目标库", "MySQL 版本", "采集耗时"]
    by_label = {s["label"]: s["value"] for s in v["stats"]}
    assert by_label["已采集语句"] == "1 / 1"
    assert by_label["目标库"] == "127.0.0.1:3307/sqlpulse_demo"
    assert by_label["MySQL 版本"] == "8.0.36"
    assert by_label["预估最大扫描行数"] == "48,931"
    max_rows_stat = next(s for s in v["stats"] if s["label"] == "预估最大扫描行数")
    assert max_rows_stat["sub"] == "sql_1 第 1 条"


def test_build_view_statement_numbering_starts_at_one():
    a = _artifact()
    a["tasks"][0]["statements"] = a["tasks"][0]["statements"] * 3
    v = ev.build_view(a, None)
    assert [s["no"] for s in v["tasks"][0]["statements"]] == [1, 2, 3]


def test_build_view_tolerates_missing_optional_fields():
    """产物结构允许缺字段（老版本产物 / 人工编辑），视图层不能崩。"""
    v = ev.build_view({"run_id": "x", "ok": True, "summary": {}, "tasks": [{}]}, None)
    assert v["available"] is True
    assert v["tasks"][0]["statements"] == []


def test_build_view_double_layer_granularity_is_preserved():
    """一个 sql_id 下可以有多条语句（压测账本按 task 记、EXPLAIN 只能逐条发）。"""
    a = _artifact()
    a["tasks"][0]["statements"] = [
        {"index": 0, "original": "BEGIN", "ok": False,
         "findings": [{"code": "skipped_not_explainable", "level": "info"}]},
        {"index": 1, "original": "SELECT 1", "filled": "SELECT 1", "ok": True,
         "max_rows": 1, "plan": [], "findings": []},
    ]
    sts = ev.build_view(a, None)["tasks"][0]["statements"]
    assert len(sts) == 2
    assert [s["status"] for s in sts] == ["skipped_not_explainable", "probed"]


def test_skipped_statements_leave_the_card_grid_but_not_the_view():
    """BEGIN/COMMIT 不占卡片位（shown），但仍留在 statements 里 —— 不能凭空消失。

    用户写了 3 条只看到 2 条，会以为漏采；所以它们进折叠行，而不是被丢掉。
    """
    a = _artifact()
    a["tasks"][0]["statements"] = [
        {"index": 0, "original": "BEGIN", "ok": False,
         "findings": [{"code": "skipped_not_explainable", "level": "info",
                       "detail": "'BEGIN' 不可 EXPLAIN（事务控制、SET 等没有执行计划），未采集。"}]},
        {"index": 1, "original": "UPDATE stock SET qty = qty - 1 WHERE sku = 'SKU2'", "ok": True,
         "max_rows": 1, "plan": [], "findings": []},
        {"index": 2, "original": "COMMIT", "ok": False,
         "findings": [{"code": "skipped_not_explainable", "level": "info",
                       "detail": "'COMMIT' 不可 EXPLAIN（事务控制、SET 等没有执行计划），未采集。"}]},
    ]
    t = ev.build_view(a, None)["tasks"][0]
    assert [s["no"] for s in t["statements"]] == [1, 2, 3]
    assert [s["no"] for s in t["shown"]] == [2]
    assert [s["no"] for s in t["skipped"]] == [1, 3]
    assert [s["sql"] for s in t["skipped"]] == ["BEGIN", "COMMIT"]
    assert t["skipped"][0]["status_label"] == "不可 EXPLAIN"
    assert "事务控制" in t["skipped"][0]["reason"]


def test_error_statements_stay_in_the_card_grid():
    """EXPLAIN 失败 / 取值失败本身就是线索，必须保留完整卡片，不能被折叠掉。"""
    a = _artifact()
    a["tasks"][0]["statements"] = [
        {"index": 0, "original": "SELECT * FROM nope", "ok": False,
         "findings": [{"code": "explain_error", "level": "warn", "detail": "1146 ..."}]},
        {"index": 1, "original": "SELECT * FROM t WHERE id = {{rand(1,9)}}", "ok": False,
         "findings": [{"code": "compile_error", "level": "warn", "detail": "..."}]},
    ]
    t = ev.build_view(a, None)["tasks"][0]
    assert [s["status"] for s in t["shown"]] == ["explain_error", "compile_error"]
    assert t["skipped"] == []


def test_skipped_hint_wording_differs_by_reason():
    """「不产生执行计划」与「超额度未采集」是两件事，措辞不能共用。"""
    def hint(codes):
        a = _artifact()
        a["tasks"][0]["statements"] = [
            {"index": i, "original": f"STMT{i}", "ok": False,
             "findings": [{"code": c, "level": "info", "detail": "d"}]}
            for i, c in enumerate(codes)]
        return ev.build_view(a, None)["tasks"][0]["skipped_hint"]

    assert hint(["skipped_not_explainable"] * 2) == \
        "另有 2 条语句不产生执行计划（事务控制、SET 等）：STMT0、STMT1"
    assert hint(["not_probed"]) == "另有 1 条语句超出本轮采集额度，未采集：STMT0"
    assert hint(["skipped_not_explainable", "not_probed"]) == "另有 2 条语句未采集：STMT0、STMT1"


def test_skipped_sql_is_flattened_and_truncated():
    """折叠行是一行文字：SQL 先折行再截断，否则多行脚本会把版面撑开。"""
    a = _artifact()
    a["tasks"][0]["statements"] = [
        {"index": 0, "original": "SET\n  autocommit = 0", "ok": False,
         "findings": [{"code": "skipped_not_explainable", "level": "info", "detail": "d"}]},
        {"index": 1, "original": "X" * 80, "ok": False,
         "findings": [{"code": "skipped_not_explainable", "level": "info", "detail": "d"}]},
    ]
    t = ev.build_view(a, None)["tasks"][0]
    assert t["skipped"][0]["sql"] == "SET autocommit = 0"
    assert t["skipped"][1]["sql"] == "X" * 24 + "…"


def test_empty_group_is_safe():
    """整组都是不产生执行计划的语句、或老产物缺字段时，模板拿到的仍是可渲染结构。"""
    t = ev.build_view({"run_id": "x", "ok": True, "summary": {}, "tasks": [{}]}, None)["tasks"][0]
    assert t["shown"] == []
    assert t["skipped"] == []
    assert t["skipped_hint"] == ""
