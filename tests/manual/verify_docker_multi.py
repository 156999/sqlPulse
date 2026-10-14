"""容器内多表并发导入的端到端验收（通过 HTTP 打容器里的 web 服务）。

对 Docker 容器内的 web 服务做一次真实调用：登录 -> 提交 multi 任务 ->
轮询到结束，并用耗时判断并发是否真的生效（两张表各 SLEEP 0.8s）。

用完的两个 _multi_docker_* 临时表不在这里清理，由调用方负责。
"""

import json
import os
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

# 宿主端口以 .env 的 WEB_PORT 为准（本机 8080 落在 Windows 保留段，已改用 18080）
BASE = os.environ.get("SQLPULSE_BASE", "http://127.0.0.1:8080")
TABLE_A = "_multi_docker_a"
TABLE_B = "_multi_docker_b"

CONTENT = (
    f"CREATE TABLE IF NOT EXISTS {TABLE_A} (id INT);\n"
    f"INSERT INTO {TABLE_A} VALUES (SLEEP(0.8));\n"
    f"CREATE TABLE IF NOT EXISTS {TABLE_B} (id INT);\n"
    f"INSERT INTO {TABLE_B} VALUES (SLEEP(0.8));\n"
)

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def post(path, data, json_body=False):
    if json_body:
        payload = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
    else:
        payload = urllib.parse.urlencode(data).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(BASE + path, data=payload, headers=headers)
    with opener.open(req, timeout=20) as resp:
        return resp.status, resp.read().decode()


def get(path):
    with opener.open(BASE + path, timeout=20) as resp:
        return resp.status, resp.read().decode()


def main():
    ok = True

    status, _ = post("/login", {"username": "root", "password": "root"})
    print(f"[1] 登录 root/root                -> {status}")
    ok &= status in (200, 303)

    body = {
        "name": "docker-multi-verify",
        "mode": "multi",
        "source": "paste",
        "content": CONTENT,
        "variables": {},
        "row_count": 1,
        "concurrency": 2,
        "db_dsn": {
            "host": "mysql",
            "port": 3306,
            "user": "root",
            "password": "s3cret",
            "database": "sqlpulse_demo",
        },
    }
    status, text = post("/api/datagen/jobs", body, json_body=True)
    job = json.loads(text)
    job_id = job.get("job_id")
    print(f"[2] 提交 multi 任务              -> {status} job_id={job_id}")
    ok &= status in (200, 201) and bool(job_id)

    started = time.perf_counter()
    final = {}
    for _ in range(120):
        time.sleep(0.25)
        _, text = get(f"/api/datagen/jobs/{job_id}")
        final = json.loads(text)
        if final.get("status") in ("finished", "failed", "partial", "cancelled"):
            break
    elapsed = time.perf_counter() - started

    print(f"[3] 任务结束                     -> status={final.get('status')} "
          f"rows={final.get('rows_affected')} 耗时={elapsed:.2f}s")
    ok &= final.get("status") == "finished"
    ok &= (final.get("rows_affected") or 0) == 2

    # 串行需要约 1.6s；并发应明显低于此
    faster = elapsed < 1.3
    print(f"[4] 并发生效判定                 -> {'PASS' if faster else 'FAIL'} "
          f"(串行需 ~1.6s, 实测 {elapsed:.2f}s)")
    ok &= faster

    if final.get("error_msg"):
        print(f"    错误信息: {final['error_msg']}")

    _, log_text = get(f"/api/datagen/jobs/{job_id}/log")
    for table in (TABLE_A, TABLE_B):
        hit = f"{table} finished" in log_text
        print(f"[5] 日志含 {table} finished    -> {'PASS' if hit else 'FAIL'}")
        ok &= hit

    print()
    print(f"==== 结论: {'全部通过' if ok else '存在失败项'} ====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
