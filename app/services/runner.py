import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader
from loguru import logger

from app import db
from app.config import settings

WEIGHT_RE = re.compile(r"^\s*--\s*weight:\s*(\d+)", re.IGNORECASE)


def split_statements(text: str) -> list:
    stmts, buf = [], []
    i, n, q = 0, len(text), None
    while i < n:
        c = text[i]
        if q:
            buf.append(c)
            if c == "\\" and i + 1 < n and q != "`":
                buf.append(text[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "-" and text[i : i + 2] == "--":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        if c == "#":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        if c == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


def parse_sql_tasks(sql_text: str) -> list:
    """weight 注释段：整段一个 task（事务支持）；无注释段：事务块整体、普通语句各自成 task。"""
    segments = []
    current = {"weight": None, "lines": []}
    for line in sql_text.splitlines():
        m = WEIGHT_RE.match(line)
        if m:
            if any(l.strip() and not l.strip().startswith("#") for l in current["lines"]) or current["weight"] is not None:
                segments.append(current)
            current = {"weight": int(m.group(1)), "lines": []}
        else:
            current["lines"].append(line)
    segments.append(current)

    def is_begin(s: str) -> bool:
        u = s.upper()
        return u == "BEGIN" or u == "START TRANSACTION"

    def is_commit(s: str) -> bool:
        return s.upper() == "COMMIT"

    tasks = []

    def add(stmts, weight):
        if stmts:
            tasks.append({"sql_id": f"sql_{len(tasks) + 1}", "weight": weight, "statements": stmts})

    for seg in segments:
        stmts = split_statements("\n".join(seg["lines"]))
        if not stmts:
            continue
        if seg["weight"] is not None:
            add(stmts, seg["weight"])
            continue
        buf = []
        in_txn = False
        for s in stmts:
            if is_begin(s):
                buf = [s]
                in_txn = True
            elif in_txn:
                buf.append(s)
                if is_commit(s):
                    add(buf, 1)
                    buf, in_txn = [], False
            else:
                add([s], 1)
        if buf:
            add(buf, 1)
    return tasks


def render_locustfile(tasks: list, out_path: Path) -> str:
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "locust_tpl")),
        autoescape=False,
        keep_trailing_newline=True,
    )
    content = env.get_template("sql_user.py.j2").render(tasks=tasks)
    out_path.write_text(content, encoding="utf-8")
    return content


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail(path: Path, size: int = 2048) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


class LocustRunner:
    def __init__(self):
        self._procs: dict = {}
        self._on_finish = None  # callable(run_id)，在 watcher 线程触发

    def set_on_finish(self, cb) -> None:
        self._on_finish = cb

    def start(self, run_id: str) -> None:
        run = db.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        tasks = parse_sql_tasks(run["sql_content"])
        if not tasks:
            db.update_run(run_id, {"status": "failed", "error_msg": "没有可执行的 SQL 语句", "ended_at": now_iso()})
            raise ValueError("没有可执行的 SQL 语句")

        locustfile = settings.data_dir / "locustfiles" / f"{run_id}.py"
        render_locustfile(tasks, locustfile)

        dsn = json.loads(run["db_dsn_json"])
        env = os.environ.copy()
        env.update(
            {
                "TARGET_DB_HOST": dsn["host"],
                "TARGET_DB_PORT": str(dsn["port"]),
                "TARGET_DB_USER": dsn["user"],
                "TARGET_DB_PASSWORD": dsn["password"],
                "TARGET_DB_NAME": dsn["database"],
            }
        )
        csv_prefix = str(settings.data_dir / "locust" / run_id)
        log_path = settings.logs_dir / f"locust_{run_id}.log"
        cmd = [
            sys.executable, "-m", "locust", "-f", str(locustfile), "--headless",
            "-u", str(run["concurrency"]), "-r", str(run["spawn_rate"]),
            "-t", f"{run['duration_sec']}s", "--csv", csv_prefix, "--csv-full-history",
            "--only-summary", "--loglevel", "WARNING",
        ]
        logger.info("run {} start: {}", run_id, " ".join(cmd))
        try:
            log_f = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, env=env)
        except Exception as e:
            db.update_run(run_id, {"status": "failed", "error_msg": str(e), "ended_at": now_iso()})
            raise
        self._procs[run_id] = proc
        db.update_run(run_id, {"status": "running", "started_at": now_iso()})
        threading.Thread(target=self._watch, args=(run_id,), daemon=True).start()

    def stop(self, run_id: str) -> str:
        run = db.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "running":
            raise PermissionError(f"run {run_id} 状态为 {run['status']}，无法停止")
        proc = self._procs.get(run_id)
        db.update_run(run_id, {"status": "cancelled", "ended_at": now_iso()})
        if proc:
            try:
                proc.terminate()
                proc.wait(5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(2)
                except Exception:
                    pass
        return "cancelled"

    def _watch(self, run_id: str) -> None:
        proc = self._procs.get(run_id)
        if not proc:
            return
        code = proc.wait()
        self._procs.pop(run_id, None)
        run = db.get_run(run_id)
        if not run:
            return
        if run["status"] == "running":
            if code == 0:
                all_fail = self._all_failed(run_id)
                if all_fail:
                    db.update_run(run_id, {"status": "failed", "error_msg": "所有请求均失败：SQL 语法/权限/表不存在错误", "ended_at": now_iso()})
                else:
                    db.update_run(run_id, {"status": "finished", "ended_at": now_iso()})
            else:
                err = _tail(settings.logs_dir / f"locust_{run_id}.log")
                db.update_run(run_id, {"status": "failed", "error_msg": err or f"locust 退出码 {code}", "ended_at": now_iso()})
        logger.info("run {} exited code={} status={}", run_id, code, db.get_run(run_id)["status"])
        if self._on_finish:
            try:
                self._on_finish(run_id)
            except Exception as e:
                logger.exception("report generation failed for {}: {}", run_id, e)

    def _all_failed(self, run_id: str) -> bool:
        import csv as csvmod

        path = settings.data_dir / "locust" / f"{run_id}_stats.csv"
        try:
            with open(path, newline="", encoding="utf-8") as f:
                rows = [r for r in csvmod.DictReader(f) if r.get("Name") == "Aggregated"]
            if not rows:
                return False
            total = float(rows[-1].get("Request Count") or 0)
            fail = float(rows[-1].get("Failure Count") or 0)
            return total > 0 and fail >= total
        except OSError:
            return False


runner = LocustRunner()
