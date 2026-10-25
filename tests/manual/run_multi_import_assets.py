"""把 tests/assets/multi_import 下的用例文件逐个丢给真实的多表导入接口跑一遍。

走的是浏览器同一条链路：登录 -> POST /api/datagen/analyze（预览分组）-> POST
/api/datagen/jobs -> 轮询到结束 -> 读任务日志。用途有两个：
  1. 证明这些测试素材本身是可用的（不是纸上谈兵）；
  2. 给你一个「一键复跑」的入口，改完代码后可以直接再验一遍。

前置：测试表已建好（先执行 00_setup_tables.sql），web 容器跑着。
地址默认 http://127.0.0.1:8080，可用环境变量 SQLPULSE_BASE 覆盖（本机端口见 .env 的 WEB_PORT）。
用法：PYTHONPATH=. ./.venv/Scripts/python.exe tests/manual/run_multi_import_assets.py
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

# 宿主端口以 .env 的 WEB_PORT 为准（本机 8080 落在 Windows 保留段，已改用 18080）
BASE = os.environ.get("SQLPULSE_BASE", "http://127.0.0.1:8080")
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "multi_import"

# 主日志行：[时间戳] [job_id] 表名 finished|failed [影响 N 行...]
LOG_LINE = re.compile(r"\]\s+\[[0-9a-f]{8}\]\s+(\S+)\s+(finished|failed|cancelled)\b")

# 文件 -> (并发分组数, 期望状态, 期望成功分组数, 期望失败分组数)
CASES = [
    ("01_三表并发.sql",        4, "finished", 3, 0),
    ("02_并发实证_sleep.sql",  4, "finished", 4, 0),
    ("03_组内保序.sql",        4, "finished", 1, 0),
    ("04_失败隔离.sql",        4, "partial",  4, 3),
    ("05_六表并发上限.sql",    2, "finished", 6, 0),
    ("06_语法变体.sql",        4, "finished", 2, 0),
    ("08_演示库现有表.sql",    4, "finished", 3, 0),
    # 07 的外键结果本身不稳定，单独跑、不参与断言
]

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def post(path, data, json_body=False):
    if json_body:
        payload = json.dumps(data, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    else:
        payload = urllib.parse.urlencode(data).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(BASE + path, data=payload, headers=headers)
    with opener.open(req, timeout=30) as resp:
        return resp.status, resp.read().decode()


def get(path):
    with opener.open(BASE + path, timeout=30) as resp:
        return resp.status, resp.read().decode()


def run_case(name: str, content: str, concurrency: int, wait_sec: float = 90.0):
    body = {
        "name": f"asset-{name}",
        "mode": "multi",
        "source": "file",
        "content": content,
        "variables": {},
        "row_count": 1,
        "concurrency": concurrency,
        "db_dsn": {"host": "mysql", "port": 3306, "user": "root",
                   "password": "s3cret", "database": "sqlpulse_demo"},
    }
    status, text = post("/api/datagen/jobs", body, json_body=True)
    if status not in (200, 201):
        return {"error": f"提交失败 {status}: {text[:200]}"}
    job_id = json.loads(text)["job_id"]

    started = time.perf_counter()
    final = {}
    while time.perf_counter() - started < wait_sec:
        time.sleep(0.2)
        _, t = get(f"/api/datagen/jobs/{job_id}")
        final = json.loads(t)
        if final.get("status") in ("finished", "failed", "partial", "cancelled"):
            break
    elapsed = time.perf_counter() - started

    _, log_text = get(f"/api/datagen/jobs/{job_id}/log")
    # 主日志格式：[2026-10-11T13:16:13] [00944cce] mi_orders finished 影响 5 行
    ok_tables, bad_tables = [], []
    for m in LOG_LINE.finditer(log_text):
        table, state = m.group(1), m.group(2)
        if table == "__global__" or table.startswith("multi"):
            continue
        (ok_tables if state == "finished" else bad_tables).append(table)

    return {
        "job_id": job_id,
        "status": final.get("status"),
        "rows": final.get("rows_affected"),
        "elapsed": elapsed,
        "error": final.get("error_msg"),
        "ok_tables": sorted(set(ok_tables)),
        "bad_tables": sorted(set(bad_tables)),
        "log": log_text,
    }


def main():
    status, _ = post("/login", {"username": "root", "password": "root"})
    print(f"登录 root/root -> {status}\n")

    failures = []
    timings = {}

    for name, concurrency, want_status, want_ok, want_bad in CASES:
        path = ASSETS / name
        content = path.read_text(encoding="utf-8")

        _, preview_text = post("/api/datagen/analyze",
                               {"content": content, "mode": "multi", "source": "file"}, json_body=True)
        preview = json.loads(preview_text)

        result = run_case(name, content, concurrency)
        if not result.get("job_id"):
            print(f"✗ {name}: {result.get('error')}")
            failures.append(name)
            continue

        timings[name] = result["elapsed"]
        good = (result["status"] == want_status
                and len(result["ok_tables"]) == want_ok
                and len(result["bad_tables"]) == want_bad)
        mark = "✓" if good else "✗"
        if not good:
            failures.append(name)

        groups = "、".join(f"{g['table']}×{g['statements']}" for g in preview.get("groups", []))
        print(f"{mark} {name}")
        print(f"    分组预览   : {groups}"
              + (f" + 全局 {preview['global_statements']} 条" if preview.get("global_statements") else ""))
        print(f"    结果       : status={result['status']} 影响 {result['rows']} 行 "
              f"耗时 {result['elapsed']:.2f}s")
        print(f"    成功分组   : {', '.join(result['ok_tables']) or '(无)'}")
        if result["bad_tables"]:
            print(f"    失败分组   : {', '.join(result['bad_tables'])}")
        if result["error"]:
            print(f"    error_msg  : {result['error'][:160]}")
        if not good:
            print(f"    期望       : status={want_status} 成功{want_ok}个 失败{want_bad}个")
        print()

    # 并发对照：同一个文件，并发度 1 vs 4
    print("=" * 70)
    print("并发对照实验（02_并发实证_sleep.sql，4 张表各 SLEEP(0.6)）")
    content = (ASSETS / "02_并发实证_sleep.sql").read_text(encoding="utf-8")
    for c in (1, 4):
        r = run_case("sleep-contrast", content, c)
        print(f"  并发分组数={c} -> 耗时 {r['elapsed']:.2f}s  status={r['status']}")
        timings[f"sleep_c{c}"] = r["elapsed"]
    serial, parallel = timings.get("sleep_c1", 0), timings.get("sleep_c4", 0)
    if serial and parallel:
        speedup = serial / parallel
        verdict = "PASS" if speedup > 2 else "FAIL"
        print(f"  加速比 {speedup:.2f}x -> {verdict}（串行需 ~2.4s；>2x 才说明并行真的生效）")
        if verdict == "FAIL":
            failures.append("sleep-contrast")

    # 外键用例：只展示，不断言
    print()
    print("=" * 70)
    print("外键用例（07_外键父子表.sql，结果本就不稳定，只展示）")
    content = (ASSETS / "07_外键父子表.sql").read_text(encoding="utf-8")
    r = run_case("fk", content, 4)
    print(f"  status={r['status']} 影响 {r['rows']} 行")
    if r["bad_tables"]:
        print(f"  失败分组: {', '.join(r['bad_tables'])}")
    if r["error"]:
        print(f"  error_msg: {r['error'][:200]}")

    print()
    print("=" * 70)
    print(f"结论：{'全部用例符合预期' if not failures else '不符合预期 -> ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
