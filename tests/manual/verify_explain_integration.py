"""EXPLAIN 采集的实机验收（需要 MySQL 在 127.0.0.1:3307 + 本地服务在 :8080）。

跑法：
    1) docker start sqlpulse-mysql-1
    2) ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
    3) ./.venv/Scripts/python.exe tests/manual/verify_explain_integration.py

它验证六件事：

1. **采集时机**：压测还在跑时不该有产物；跑完收尾后产物才出现（2026-09-25 的改动点）。
2. **只发 EXPLAIN**：临时打开 MySQL general log 抓整个「压测 + 收尾」窗口，
   逐条检查真正到达数据库的语句 —— 这比读代码可信。
3. 产物契约：字段齐、`phase=post_run`、`sql_id` 与压测账本对得上、**不含 password**。
4. 判定正确性：慢查询被标 `table_scan`，主键点查干净。
5. `/api/runs/{id}/explain` 与文件内容一致。
6. 页面 200 且渲染出预期文案；未登录时被拦。

注意窗口的范围：采集发生在压测**收尾**，所以 general log 必须一直开到产物出现为止，
不能在 `POST /api/runs` 之后就关（那样一条 EXPLAIN 都抓不到）。

**这个脚本会真的建一次压测**（写一条 run 记录 + 跑几秒 locust），属于手工验收工具。
"""
import csv as csv_mod
import html as html_mod
import json
import pathlib
import re
import sys
import time

import httpx
import pymysql


def csv_cutter(path):
    """读 locust 的 stats.csv（列名含空格，用 DictReader 即可）。"""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv_mod.DictReader(f))

BASE = "http://127.0.0.1:8080"
MYSQL = dict(host="127.0.0.1", port=3307, user="root",
             password="s3cret", database="sqlpulse_demo", autocommit=True)
DSN = {"host": "127.0.0.1", "port": 3307, "user": "root",
       "password": "s3cret", "database": "sqlpulse_demo"}

SQL = """-- weight: 50
SELECT * FROM orders WHERE id = {{rand(1,10000)}};

-- weight: 30
SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';

-- weight: 20
SELECT * FROM orders WHERE uid = {{var('uid')}};
"""
VARIABLES = {"uid": "rand(1,10000)"}

failures = []


def check(label, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
    if not ok:
        failures.append(label)
    return ok


def admin():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **MYSQL)


def main():
    client = httpx.Client(base_url=BASE, timeout=60)

    # ---------------------------------------------------------------- 登录
    r = client.post("/login", data={"username": "root", "password": "root", "next": "/runs"})
    print(f"登录: {r.status_code}")
    if r.status_code not in (200, 303):
        print("登录失败，终止")
        return 1

    print("\n=== 1. 未登录访问执行计划页应被拦截 ===")
    anon = httpx.Client(base_url=BASE, timeout=10, follow_redirects=False)
    r = anon.get("/runs/whatever/explain")
    check("未登录 → 跳登录页", r.status_code in (303, 307) and "/login" in r.headers.get("location", ""),
          f"{r.status_code} → {r.headers.get('location')}")

    # ---------------------------------------------------------------- 开 general log
    print("\n=== 2. 打开 general log，抓「压测 + 收尾」整段窗口里真正发出的语句 ===")
    conn = admin()
    with conn.cursor() as cur:
        cur.execute("SET GLOBAL log_output = 'TABLE'")
        cur.execute("SET GLOBAL general_log = 'OFF'")
        cur.execute("TRUNCATE TABLE mysql.general_log")
        cur.execute("SET GLOBAL general_log = 'ON'")
    started = time.time()
    print("  general_log: ON")

    r = client.post("/api/runs", json={
        "name": "explain-integration", "sql_source": "paste", "sql_content": SQL,
        "variables": VARIABLES, "concurrency": 3, "spawn_rate": 2,
        "duration_sec": 6, "db_dsn": DSN,
    })
    print(f"  POST /api/runs → {r.status_code} {r.text[:120]}")
    if r.status_code != 201:
        print("  建压测失败，终止")
        return 1
    run_id = r.json()["run_id"]
    print(f"  run_id = {run_id}")

    # ------------------------------------------- 采集时机的回归点（2026-09-25 改动）
    print("\n=== 2b. 压测还在跑 → 不该有产物 ===")
    mid = client.get(f"/api/runs/{run_id}/explain")
    check("跑的过程中 /api/runs/{id}/explain → 404", mid.status_code == 404, str(mid.status_code))
    mid_page = client.get(f"/runs/{run_id}/explain")
    check("跑的过程中页面 200、显示空态",
          mid_page.status_code == 200 and "还没有可用" in html_mod.unescape(mid_page.text),
          str(mid_page.status_code))

    print("\n=== 2c. 等压测收尾：采集就发生在这里 ===")
    d = {}
    for _ in range(40):
        time.sleep(2)
        d = client.get(f"/api/runs/{run_id}").json()
        if d["status"] in ("finished", "failed", "cancelled"):
            break
    print(f"  压测最终状态: {d['status']}  err={d['error_msg']!r}")
    check("压测正常结束", d["status"] == "finished", d["status"])

    # 状态先落库、产物随后才出现 —— 这是收尾路径的顺序特征，不是竞态
    api = None
    for _ in range(30):
        api = client.get(f"/api/runs/{run_id}/explain")
        if api.status_code == 200:
            break
        time.sleep(1)
    check("收尾后产物出现", api is not None and api.status_code == 200,
          str(getattr(api, "status_code", None)))

    with conn.cursor() as cur:
        cur.execute("SET GLOBAL general_log = 'OFF'")
        cur.execute(
            "SELECT argument, event_time FROM mysql.general_log "
            "WHERE event_time >= FROM_UNIXTIME(%s - 3) ORDER BY event_time", (started,))
        # mysql.general_log.argument 是 MEDIUMBLOB → pymysql 回 bytes
        logged = [row["argument"].decode("utf-8", "replace") if isinstance(row["argument"], bytes)
                  else (row["argument"] or "") for row in cur.fetchall()]

    # 去掉 general_log 自身的开关语句
    stmts = [s for s in logged if s and "general_log" not in s.lower()]
    explains = [s for s in stmts if s.strip().upper().startswith("EXPLAIN")]
    others = [s for s in stmts if s not in explains]
    print(f"  抓到 {len(stmts)} 条语句：EXPLAIN {len(explains)} 条、其他 {len(others)} 条")
    for s in explains:
        print(f"    · {s[:110]}")
    print("  同窗口其他语句抽样（压测自己发的 SELECT + 连接探测，不该有写操作）：")
    for s in others[:6]:
        print(f"    - {s[:100]}")
    if len(others) > 6:
        print(f"    … 其余 {len(others) - 6} 条")

    check("确实发出了 EXPLAIN", len(explains) == 3, f"{len(explains)}/3")
    check("没有 EXPLAIN ANALYZE",
          not any("ANALYZE" in s.upper() for s in explains))
    check("没有 ANALYZE TABLE", not any("ANALYZE TABLE" in s.upper() for s in stmts))
    check("没有 EXPLAIN 写语句",
          not any(s.strip().upper().startswith(("EXPLAIN INSERT", "EXPLAIN UPDATE",
                                                "EXPLAIN DELETE", "EXPLAIN REPLACE"))
                  for s in explains))
    check("EXPLAIN 里没有未绑定的占位符", not any("{{" in s or "%s" in s for s in explains))
    check("发的是绑定后的字面量 SQL",
          any("LIKE '%api%'" in s for s in explains))
    writes = [s for s in stmts if re.match(
        r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|ANALYZE)\b",
        s, re.I)]
    check("采集窗口内没有任何写/DDL 语句", not writes, str(writes[:3]))

    # ---------------------------------------------------------------- 产物
    print("\n=== 3. 产物契约 ===")
    api = client.get(f"/api/runs/{run_id}/explain")
    check("GET /api/runs/{id}/explain → 200", api.status_code == 200, str(api.status_code))
    if api.status_code != 200:
        return 1
    art = api.json()

    path = pathlib.Path("data/explain") / f"{run_id}.json"
    check("产物已落盘", path.is_file(), str(path))
    check("接口内容 == 文件内容", path.is_file() and json.loads(path.read_text(encoding="utf-8")) == art)

    raw = path.read_text(encoding="utf-8")
    check("产物不含 password", "s3cret" not in raw and '"password"' not in raw)
    check("probe_version = 3", art["probe_version"] == 3, str(art["probe_version"]))
    check("phase = post_run（采集在压测之后）", art["phase"] == "post_run", str(art.get("phase")))
    check("ok = True", art["ok"] is True, str(art["error"]))
    check("目标库记录正确", art["target"]["database"] == "sqlpulse_demo"
          and art["target"]["port"] == 3307)

    s = art["summary"]
    print(f"  summary: {json.dumps({k: v for k, v in s.items() if k != 'by_code'}, ensure_ascii=False)}")
    print(f"  by_code: {json.dumps(s['by_code'], ensure_ascii=False)}")
    check("3 个 task / 3 条语句 / 3 条已采集",
          (s["tasks"], s["statements"], s["probed"]) == (3, 3, 3),
          f"{s['tasks']}/{s['statements']}/{s['probed']}")
    check("sql_id 与压测账本同名", [t["sql_id"] for t in art["tasks"]] == ["sql_1", "sql_2", "sql_3"])
    with_params = [st for t in art["tasks"] for st in t["statements"] if "{{" in st["original"]]
    check("带占位符的语句都记了绑定结果",
          bool(with_params) and all(st["parameters"] for st in with_params),
          f"{len(with_params)} 条带占位符")
    check("不带占位符的语句参数为空",
          all(not st["parameters"] for t in art["tasks"] for st in t["statements"]
              if "{{" not in st["original"]))
    check("每条语句都记了 plan",
          all(st["plan"] for t in art["tasks"] for st in t["statements"]))
    check("plan 列已归一",
          all(set(p) == {"id", "select_type", "table", "type", "possible_keys",
                         "key", "key_len", "ref", "rows", "filtered", "Extra"}
              for t in art["tasks"] for st in t["statements"] for p in st["plan"]))

    # ---------------------------------------------------------------- 判定
    print("\n=== 4. 判定正确性（对照真实计划）===")
    by_id = {t["sql_id"]: t["statements"][0] for t in art["tasks"]}
    for t in art["tasks"]:
        st = t["statements"][0]
        p = st["plan"][0]
        print(f"  {t['sql_id']}: {st['filled'][:70]}")
        print(f"        type={p['type']} key={p['key']} rows={p['rows']} "
              f"Extra={p['Extra']!r}")
        for f in st["findings"]:
            print(f"        [{f['level']}] {f['code']}: {f['detail']}")

    check("主键点查 ← 无坏味道",
          by_id["sql_1"]["ok"] and not by_id["sql_1"]["findings"],
          f"type={by_id['sql_1']['plan'][0]['type']}")
    check("LIKE '%api%' 全表扫描 ← table_scan",
          any(f["code"] == "table_scan" for f in by_id["sql_2"]["findings"]))
    check("全表扫描的 key 为 NULL", by_id["sql_2"]["plan"][0]["key"] is None)
    check("worst_level = warn", s["worst_level"] == "warn", str(s["worst_level"]))
    check("max_rows 指向那条慢查询",
          s["max_rows"] == by_id["sql_2"]["plan"][0]["rows"]
          and s["max_rows_ref"] == {"sql_id": "sql_2", "index": 0},
          f"{s['max_rows']} / {s['max_rows_ref']}")
    check("样本值落在 rand(1,10000) 区间",
          all(1 <= int(st["parameters"][0]["value"]) <= 10000 for st in with_params))

    # ---------------------------------------------------------------- 页面
    print("\n=== 5. 展示页 ===")
    page = client.get(f"/runs/{run_id}/explain")
    html = page.text
    # Jinja 开了自动转义：`'` 会变成 `&#39;`，比对前先还原
    plain = html_mod.unescape(html)
    check("页面 200", page.status_code == 200, str(page.status_code))
    check("渲染出一句话结论", "发现" in plain and "计划问题" in plain)
    check("渲染出表名 access_log", "access_log" in plain)
    check("渲染出全表扫描标签", "全表扫描" in plain)
    check("渲染出 plan 表头", "查询类型" in plain and "命中索引" in plain)
    check("渲染出绑定后的 SQL", "LIKE '%api%'" in plain
          or "LIKE &#39;%api%&#39;" in html)
    check("渲染出绑定参数", "绑定参数" in plain)
    check("渲染出实际发送的语句（有占位符的那两条）",
          plain.count("压测实际会发送的语句") == 2,
          f"{plain.count('压测实际会发送的语句')} 处")
    check("渲染出三条局限", "已知局限" in plain)
    check("页面写明采集时机在压测之后", "压测结束后采集" in plain)
    check("没有裸露的 markdown 星号", "**" not in plain)

    empty = client.get("/runs/nonexistent/explain")
    check("不存在的 run → 404", empty.status_code == 404, str(empty.status_code))

    # ---------------------------------------------------------------- 压测本身
    print("\n=== 6. 压测本身没被采集弄脏（采集发生在它结束之后）===")
    csv_path = pathlib.Path("data/locust") / f"{run_id}_stats.csv"
    total = fails = None
    if csv_path.is_file():
        # 表头是 Type,Name,...，所以 Aggregated 行以逗号开头
        rows = [r for r in csv_cutter(csv_path) if r.get("Name") == "Aggregated"]
        if rows:
            total, fails = int(float(rows[-1]["Request Count"])), int(float(rows[-1]["Failure Count"]))
            print(f"  stats: 请求 {total}，失败 {fails}，平均 {float(rows[-1]['Average Response Time']):.1f} ms")
    check("压测发出了请求", bool(total) and total > 0, str(total))
    check("采集没有让压测变脏（失败数 0）", fails == 0, f"{total} 请求 / {fails} 失败")

    print(f"\n{'=' * 60}")
    print(f"结论：{'全部通过' if not failures else str(len(failures)) + ' 项失败'}"
          f"   run_id={run_id}")
    for f in failures:
        print(f"  ✗ {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
