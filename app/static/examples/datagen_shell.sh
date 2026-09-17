#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import json
import os
import pymysql

conn = pymysql.connect(
    host=os.environ["TARGET_DB_HOST"],
    port=int(os.environ["TARGET_DB_PORT"]),
    user=os.environ["TARGET_DB_USER"],
    password=os.environ["TARGET_DB_PASSWORD"],
    database=os.environ["TARGET_DB_NAME"],
    autocommit=True,
)

with conn.cursor() as cur:
    for i in range(1, 101):
        cur.execute(
            "INSERT INTO orders (uid, amount, status, created_at) VALUES (%s, %s, %s, NOW())",
            (i, 10.50 + i, "new"),
        )

with open("result.json", "w", encoding="utf-8") as f:
    json.dump({"rows_affected": 100}, f)

conn.close()
PY
