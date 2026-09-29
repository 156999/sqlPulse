"""SQLPulse 只读诊断工具验证入口。

用法：
    python -m app.ai.verify_ai_tools --run-id <任务ID> [--tool context|metrics|explain] [--data-dir DIR]

- --run-id   必填：8 位十六进制任务 ID（仓库真实规则：uuid4().hex[:8]，如 bb01ea65）。
- --tool     可选：只验证单个工具；缺省三个工具全部验证。
- --data-dir 可选：只读数据目录（须含 sqlpulse.db、explain/、locust/）。缺省使用
             应用配置的 data_dir（容器内为 /app/data，宿主机为仓库下 data/）。

行为约束（与 app/ai/tools.py 一致）：
- 只读：不启动/停止压测、不造数、不连接目标 MySQL、不执行 EXPLAIN、
  不初始化/迁移 SQLite、不恢复任务状态、不生成报告、不写数据文件。
- 不 import app.main、不跑应用 lifespan。
- 单个工具失败不影响其余工具执行；最后统一输出格式化 JSON（中文原样显示），
  含实际数据目录与 sqlpulse.db 是否存在，便于发现读错环境。
- run_id 按用户输入原样传给工具（大小写/首尾空格归一化由工具负责）。
- JSON 严格序列化（allow_nan=False）；若结果里出现 NaN/Inf，兜底替换为 null
  并标注，退出码至少为 1。
- 对三个结果的 run_id 与 SQL ID 做一致性核对，不一致时给出提示。

退出码：
    0  全部工具成功且无警告（数据完整）
    1  部分可用：有工具失败，或结果带警告/缺失标注
    2  执行错误：全部工具失败，或参数/环境错误（如数据库文件不存在）

容器内用法（源码用 docker compose run -v 挂载，或重建镜像后 exec）：
    docker compose exec web python -m app.ai.verify_ai_tools --run-id <任务ID>
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path


def _replace_nonfinite(obj):
    """NaN/Inf 无法被严格 JSON 序列化；兜底替换为 null（结果里会标注）。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _replace_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_nonfinite(v) for v in obj]
    return obj


def _dump(obj) -> tuple:
    """严格序列化（allow_nan=False）；出现非有限浮点数时降级为替换 null 后重试。"""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), False
    except ValueError:
        return json.dumps(_replace_nonfinite(obj), ensure_ascii=False, indent=2, allow_nan=False), True


def _cross_check(norm_run_id: str, results: dict) -> dict:
    """核对三个结果的 run_id 与 SQL ID 对应关系。"""
    notes: list = []

    def data_of(result: dict):
        if not result.get("ok"):
            return None
        return result.get("data") or {}

    def task_ids(result: dict) -> set:
        data = data_of(result)
        if not data:
            return set()
        return {
            t["sql_id"] for t in data.get("tasks") or []
            if isinstance(t, dict) and t.get("sql_id")
        }

    checks: list = []
    context_ids = task_ids(results.get("context", {}))
    metrics_ids = set((data_of(results.get("metrics", {})) or {}).get("per_sql") or {})
    explain_ids = task_ids(results.get("explain", {}))

    if results.get("context", {}).get("ok") and results.get("explain", {}).get("ok"):
        if context_ids == explain_ids:
            checks.append({"pair": "context/explain", "consistent": True, "sql_ids": sorted(context_ids)})
        else:
            checks.append({
                "pair": "context/explain", "consistent": False,
                "context_only": sorted(context_ids - explain_ids),
                "explain_only": sorted(explain_ids - context_ids),
            })
    if results.get("context", {}).get("ok") and results.get("metrics", {}).get("ok"):
        if context_ids == metrics_ids:
            checks.append({"pair": "context/metrics", "consistent": True, "sql_ids": sorted(context_ids)})
        else:
            checks.append({
                "pair": "context/metrics", "consistent": False,
                "context_only": sorted(context_ids - metrics_ids),
                "metrics_only": sorted(metrics_ids - context_ids),
            })

    for name, result in results.items():
        data = data_of(result)
        if not data:
            continue
        got = data.get("run_id")
        if got and got != norm_run_id:
            notes.append(f"{name} 返回的 run_id（{got}）与请求（{norm_run_id}）不一致")
    return {"checks": checks, "notes": notes}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ai.verify_ai_tools",
        description=__doc__.split("\n\n")[1] if len(__doc__.split("\n\n")) > 1 else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0=全部成功无警告；1=部分可用（有失败/警告/缺失）；2=执行错误",
    )
    parser.add_argument(
        "--run-id", required=True, metavar="RUN_ID",
        help="8 位十六进制任务 ID（如 bb01ea65）",
    )
    parser.add_argument(
        "--tool", choices=("context", "metrics", "explain"), default=None,
        help="只验证一个工具；缺省验证全部三个",
    )
    parser.add_argument(
        "--data-dir", metavar="DIR", default=None,
        help="只读数据目录（含 sqlpulse.db）；缺省用应用配置的 data_dir",
    )
    args = parser.parse_args(argv)

    # 必须在任何 app.* import 之前设置：Settings 在 import 时实例化。
    if args.data_dir:
        os.environ["DATA_DIR"] = os.path.abspath(args.data_dir)
    if "APP_SECRET_KEY" not in os.environ:
        # app.config 在 AUTH_ENABLED=true 时要求 APP_SECRET_KEY；无密钥环境需先关掉，否则 import 抛 ValueError。
        os.environ.setdefault("AUTH_ENABLED", "false")

    try:
        from app.ai.tools import get_run_context, get_run_explain, get_run_metrics
        from app.config import settings
    except Exception as e:  # 依赖缺失 / 配置错误
        payload = {
            "run_id": args.run_id,
            "data_dir": args.data_dir,
            "error": {"code": "CLI_ENV_ERROR", "message": f"{type(e).__name__}: {e}", "retryable": False},
            "exit_code": 2,
        }
        print(_dump(payload)[0])
        print("退出码 2（环境/依赖错误）：无法导入应用模块", file=sys.stderr)
        return 2

    norm_run_id = args.run_id.strip().lower()
    tools = {"context": get_run_context, "metrics": get_run_metrics, "explain": get_run_explain}
    requested = [args.tool] if args.tool else ["context", "metrics", "explain"]

    results: dict = {}
    for name in requested:
        try:
            # 传原始输入，验证工具自身的归一化
            results[name] = tools[name](args.run_id)
        except Exception as e:  # 工具自身不应抛异常；兜底防止 CLI 中断
            results[name] = {
                "ok": False, "run_id": norm_run_id, "data": None, "warnings": [],
                "error": {"code": "TOOL_ERROR", "message": f"{type(e).__name__}: {e}", "retryable": False},
            }

    ok_flags = [r.get("ok") for r in results.values()]

    def _has_warnings(result: dict) -> bool:
        return bool(result.get("ok") and result.get("warnings"))

    def _has_gaps(result: dict) -> bool:
        # metrics 工具用 data.missing 标注部分缺失（不全是 warnings）
        return bool(result.get("ok") and (result.get("data") or {}).get("missing"))

    if all(ok_flags) and not any(_has_warnings(r) or _has_gaps(r) for r in results.values()):
        exit_code = 0
    elif any(ok_flags):
        exit_code = 1
    else:
        exit_code = 2

    db_path = Path(settings.data_dir) / "sqlpulse.db"
    payload = {
        "run_id": norm_run_id,
        "data_dir": str(settings.data_dir),
        "sqlpulse_db": {"path": str(db_path), "exists": db_path.is_file()},
        "tools_requested": requested,
        "results": results,
        "cross_check": _cross_check(norm_run_id, results),
        "exit_code": exit_code,
    }
    text, sanitized = _dump(payload)
    print(text)
    if sanitized:
        exit_code = max(exit_code, 1)
        print("注意：结果含非有限浮点数（NaN/Inf），已替换为 null", file=sys.stderr)
    print(
        f"退出码 {exit_code}（0=全部成功无警告，1=部分可用，2=执行错误）；数据目录：{settings.data_dir}",
        file=sys.stderr,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
