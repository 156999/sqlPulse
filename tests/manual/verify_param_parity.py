"""取值路径对照：压测运行期（sql_user.py.j2）vs EXPLAIN 采集侧（explain_probe）。

用途：回答"EXPLAIN 拿到的 SQL 是怎么来的、占位符怎么处理的"。不连任何数据库 ——
纯编译 + 取值，用 CPython 就能复现两条路径的差异。

    PYTHONPATH=. ./.venv/Scripts/python.exe tests/manual/verify_param_parity.py

期望输出：
  ① rand/pick     两侧都成功，取值各自独立（不是同一次取值）
  ② sample(...)   运行期 ValueError、采集侧 OK   ← 上游缺陷，见 explain_probe._values_for docstring
  ③ var('v1')     两侧一致（task 内所有语句共用同一组取值，与运行期语义相同）
  ③b var(v1)      两侧都在 compile 阶段失败（参数必须是字面量）
"""
import sys

from app.services.sql_params import compile_tasks

RUN = "运行期  sql_user.py.j2 L31/L34   values={name: g.sample({})}→ statement.bind(values)"
PROBE = "采集侧  explain_probe._values_for  注入 __sample__ → statement.bind(values)"


def _compile(stmts, variables):
    return compile_tasks(
        [{"sql_id": "sql_1", "weight": 100, "statements": stmts}], variables
    )


def _runtime_side(stmts, variables):
    definitions, compiled = _compile(stmts, variables)
    values = {name: g.sample({}) for name, g in definitions.items()}   # 模板原样：空 dict
    return [s.bind(values) for s in compiled["sql_1"]]


def _probe_side(stmts, variables):
    definitions, compiled = _compile(stmts, variables)
    values = {"__sample__": lambda table, column, options: 42}         # 模拟 SampleCache
    values.update({name: g.sample(values) for name, g in definitions.items()})
    return [s.bind(values) for s in compiled["sql_1"]]


def show(title, stmts, variables):
    print("=" * 78)
    print(title)
    print("  SQL:", stmts[0])
    print("  variables:", variables or "{}")
    try:
        _, compiled = _compile(stmts, variables)
    except Exception as exc:
        print("  compile_tasks 就失败了 -> %s: %s" % (type(exc).__name__, exc))
        print("  （两侧都在这一步倒下，不存在差异）")
        return
    print("  语句生成器数:", [len(s.generators) for s in compiled["sql_1"]])
    for label, fn in ((RUN, _runtime_side), (PROBE, _probe_side)):
        try:
            print("  %s\n      -> OK %s" % (label, fn(stmts, variables)))
        except Exception as exc:
            print("  %s\n      -> %s: %s" % (label, type(exc).__name__, exc))


def main():
    show("① rand / pick —— 不依赖 values 的占位符：两侧都成功，取值各自独立（不是同一次取值）",
         ["SELECT * FROM orders WHERE id = {{rand(1,10)}} AND uid = {{pick(1,2)}}"], {})
    show("② sample(...) —— 依赖 values['__sample__']，两侧必然分叉",
         ["SELECT * FROM orders WHERE uid = {{sample('orders','uid')}}"], {})
    show("③ var(...) —— 参数必须是加引号的字符串字面量（裸标识符会被拒）",
         ["SELECT * FROM orders WHERE id = {{var('v1')}}"], {"v1": "rand(1,10)"})
    show("③b 写成裸标识符 {{var(v1)}} —— 期望编译期就报错",
         ["SELECT * FROM orders WHERE id = {{var(v1)}}"], {"v1": "rand(1,10)"})
    show("④ 变量只被定义、未在语句里引用 —— 应当正常",
         ["SELECT * FROM orders WHERE id = {{rand(1,10)}}"], {"v1": "rand(1,10)"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
