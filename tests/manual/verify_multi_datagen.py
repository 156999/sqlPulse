"""多表并发导入的端到端验收脚本（真连 MySQL + 真走 HTTP 接口）。

覆盖四件事：
1. /api/datagen/analyze 能按目标表正确分组
2. mode=multi 的任务能把数据并发落到多张表
3. 单张表失败时其余表照常完成（状态 partial）
4. 三张表各自的 SLEEP(0.4) 总耗时明显小于串行耗时——证明并发是真的

用法：
    PYTHONPATH=. ./.venv/Scripts/python.exe tests/manual/verify_multi_datagen.py

可用环境变量覆盖目标库：VERIFY_DB_HOST / PORT / USER / PASSWORD / NAME
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pymysql  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

HOST = os.getenv("VERIFY_DB_HOST", "127.0.0.1")
PORT = int(os.getenv("VERIFY_DB_PORT", "3307"))
USER = os.getenv("VERIFY_DB_USER", "root")
PASSWORD = os.getenv("VERIFY_DB_PASSWORD", "s3cret")
DATABASE = os.getenv("VERIFY_DB_NAME", "sqlpulse_demo")

TABLES = ["_multi_orders", "_multi_stock", "_multi_slow1", "_multi_slow2", "_multi_slow3"]

FAILURES = []
PASSED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def setup_tables(conn) -> None:
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute(f"CREATE TABLE `{table}` (id INT PRIMARY KEY, tag VARCHAR(32))")


def teardown_tables(conn) -> None:
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")


def count_rows(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])


def wait_job(client, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/datagen/jobs/{job_id}").json()
        if data.get("status") not in ("pending", "running"):
            return data
        time.sleep(0.15)
    return {"status": "timeout"}


def submit(client, name: str, content: str, dsn: dict, concurrency: int = 4) -> dict:
    body = {"name": name, "mode": "multi", "source": "paste", "content": content,
            "row_count": 1, "concurrency": concurrency, "db_dsn": dsn}
    response = client.post("/api/datagen/jobs", json=body)
    if response.status_code != 201:
        return {"_error": f"HTTP {response.status_code}: {response.text[:200]}"}
    return wait_job(client, response.json()["job_id"])


def main() -> int:
    print(f"目标库 {USER}@{HOST}:{PORT}/{DATABASE}\n")

    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           database=DATABASE, autocommit=True)
    dsn = {"host": HOST, "port": PORT, "user": USER, "password": PASSWORD, "database": DATABASE}
    setup_tables(conn)

    try:
        with TestClient(app) as client:
            client.post("/login", data={"username": "root", "password": "root"})
            if "多表导入" not in client.get("/datagen").text:
                print("登录失败：请确认 AUTH_SEED_ROOT=true 且 root/root 可用")
                return 1

            print("[1] 分组预览 /api/datagen/analyze")
            content = ("INSERT INTO _multi_orders (id, tag) VALUES (1, 'a');\n"
                       "INSERT INTO _multi_stock (id, tag) VALUES (1, 'b');\n"
                       "INSERT INTO _multi_orders (id, tag) VALUES (2, 'c');\n"
                       "SET @ignored = 1;")
            response = client.post("/api/datagen/analyze", json={"content": content})
            data = response.json()
            groups = {item["table"]: item["statements"] for item in data.get("groups", [])}
            check("analyze 接口返回 200", response.status_code == 200, response.text[:120])
            check("按目标表分组正确", groups == {"_multi_orders": 2, "_multi_stock": 1}, str(groups))
            check("识别不出的语句计入 global", data.get("global_statements") == 1,
                  f"global={data.get('global_statements')}")

            print("\n[2] 多表并发落库 mode=multi")
            result = submit(client, "verify-multi-basic", content, dsn)
            check("任务状态为 finished", result.get("status") == "finished",
                  f"status={result.get('status')} error={result.get('error_msg')}")
            check("_multi_orders 落库 2 行", count_rows(conn, "_multi_orders") == 2)
            check("_multi_stock 落库 1 行", count_rows(conn, "_multi_stock") == 1)

            print("\n[3] 单表失败不影响其他表")
            failing = ("INSERT INTO _multi_orders (id, tag) VALUES (100, 'ok');\n"
                       "INSERT INTO _multi_orders (id, tag) VALUES (100, 'dup');\n"
                       "INSERT INTO _multi_stock (id, tag) VALUES (200, 's');")
            before_stock = count_rows(conn, "_multi_stock")
            result = submit(client, "verify-multi-partial", failing, dsn)
            check("重复主键时状态为 partial", result.get("status") == "partial",
                  f"status={result.get('status')} error={result.get('error_msg')}")
            check("error_msg 指出失败的表", "_multi_orders" in (result.get("error_msg") or ""),
                  str(result.get("error_msg"))[:160])
            check("失败表已提交的部分保留", count_rows(conn, "_multi_orders") == 3,
                  f"实际 {count_rows(conn, '_multi_orders')}")
            check("其他表照常写入", count_rows(conn, "_multi_stock") == before_stock + 1,
                  f"实际 {count_rows(conn, '_multi_stock')}")

            print("\n[4] 并发是真实发生的（三表各 SLEEP 0.4s）")
            slow = "".join(f"INSERT INTO _multi_slow{i} (id, tag) SELECT 1, SLEEP(0.4);\n" for i in (1, 2, 3))
            started = time.perf_counter()
            result = submit(client, "verify-multi-parallel", slow, dsn, concurrency=3)
            elapsed = time.perf_counter() - started
            check("三表任务成功", result.get("status") == "finished",
                  f"status={result.get('status')} error={result.get('error_msg')}")
            check("三表各自写入 1 行",
                  all(count_rows(conn, f"_multi_slow{i}") == 1 for i in (1, 2, 3)))
            check(f"总耗时 {elapsed:.2f}s 明显低于串行约 1.20s", elapsed < 1.0,
                  f"elapsed={elapsed:.2f}s")
    finally:
        teardown_tables(conn)
        conn.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项未通过：" + "；".join(FAILURES))
        return 1
    print(f"全部 {PASSED} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
