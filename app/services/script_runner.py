import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional


def build_script_env(dsn: dict) -> dict:
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
    return env


def write_script(job_dir: Path, script_text: str) -> Path:
    script_path = job_dir / "run.sh"
    script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script_text)
    try:
        os.chmod(script_path, 0o700)
    except OSError:
        pass
    return script_path


def read_result_rows(job_dir: Path) -> Optional[int]:
    result_path = job_dir / "result.json"
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("rows_affected")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def tail_file(path: Path, size: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def run_script_job(
    job_id: str,
    dsn: dict,
    script_text: str,
    job_dir: Path,
    log_path: Path,
    cancel_event: threading.Event,
    timeout_sec: int,
) -> tuple:
    script_path = write_script(job_dir, script_text)
    env = build_script_env(dsn)
    log_f = None
    try:
        log_f = open(log_path, "w", encoding="utf-8", newline="\n")
        try:
            proc = subprocess.Popen(
                ["/bin/bash", str(script_path)],
                cwd=str(job_dir),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_f.close()
            raise
    except OSError as e:
        return "failed", None, f"无法启动 bash：{e}"

    start = time.monotonic()
    code = None
    reason = None
    while code is None:
        if cancel_event.is_set():
            reason = "cancelled"
            _terminate_process_group(proc)
            code = proc.wait()
            break
        if time.monotonic() - start > timeout_sec:
            reason = "timeout"
            _terminate_process_group(proc)
            code = proc.wait()
            break
        try:
            code = proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    if log_f is not None:
        try:
            log_f.close()
        except Exception:
            pass

    rows = read_result_rows(job_dir)
    if reason == "cancelled":
        return "cancelled", rows, None
    if reason == "timeout":
        return "failed", rows, f"执行超时（{timeout_sec}s）"
    if code == 0:
        return "finished", rows, None
    error = tail_file(log_path) or f"bash 退出码 {code}"
    return "failed", rows, error
