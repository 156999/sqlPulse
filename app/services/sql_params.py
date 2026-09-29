"""SQL value templates: compile once, generate typed parameters per execution."""
import ast
import math
import random
import re
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext


def tokens(text):
    """Yield (kind, text, offset), keeping quoted text and comments opaque."""
    i = 0
    def error(message, offset):
        line = text.count("\n", 0, offset) + 1
        column = offset - text.rfind("\n", 0, offset)
        return ValueError(f"第 {line} 行第 {column} 列：{message}")

    while i < len(text):
        start = i
        c = text[i]
        if c in "'\"`":
            quote = c
            i += 1
            while i < len(text):
                if text[i] == "\\" and quote != "`":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    if i < len(text) and text[i] == quote:
                        i += 1
                    else:
                        break
                else:
                    i += 1
            else:
                raise error("未闭合的 SQL 引号", start)
            yield "quoted", text[start:i], start
        elif c == "#" or (text.startswith("--", i) and
                              (i + 2 == len(text) or text[i + 2].isspace())):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            yield "comment", text[start:i], start
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise error("未闭合的 SQL 注释", start)
            i = end + 2
            yield "comment", text[start:i], start
        elif text.startswith("{{", i):
            i += 2
            quote = None
            while i < len(text):
                c = text[i]
                if quote:
                    if c == "\\":
                        i += 2
                        continue
                    if c == quote:
                        quote = None
                elif c in "'\"":
                    quote = c
                elif text.startswith("}}", i):
                    i += 2
                    break
                i += 1
            else:
                raise error("占位符缺少结束符 }}", start)
            yield "param", text[start:i], start
        else:
            i += 1
            yield "code", c, start


def _scalar(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    raise ValueError("候选值仅支持字符串、有限数字、布尔值和 None")


@dataclass(frozen=True)
class Generator:
    name: str
    args: tuple

    def sample(self, values):
        a = self.args
        if self.name == "var":
            return values[a[0]]
        if self.name == "sample":
            options = a[2]
            sample_ratio = options.get("sample_ratio", 100)
            if sample_ratio < 100 and random.random() * 100 >= sample_ratio:
                fallback = options.get("random_generator")
                if fallback is None:
                    raise ValueError("sample 随机比例低于 100% 时必须配置随机生成器")
                return fallback.sample(values)
            sampler = values.get("__sample__")
            if sampler is None:
                raise ValueError("sample 占位符需要任务级采样缓存")
            return sampler(a[0], a[1], a[2])
        if self.name == "rand":
            return random.randint(*a)
        if self.name == "randf":
            value = random.randint(a[0], a[1])
            with localcontext() as ctx:
                ctx.prec = max(28, len(str(abs(value))) + a[2] + 2)
                return Decimal(value).scaleb(-a[2])
        if self.name == "pick":
            return random.choice(a)
        if self.name == "pickw":
            return random.choices(a[0], weights=a[1])[0]
        if self.name == "randstr":
            return "".join(random.choices(string.ascii_lowercase + string.digits, k=a[0]))
        if self.name in ("randdate", "randdt"):
            delta = random.randint(0, a[1])
            return a[0] + (timedelta(days=delta) if self.name == "randdate" else timedelta(seconds=delta))
        return str(uuid.uuid4())


def parse_generator(expression, variables=(), allow_var=True):
    try:
        node = ast.parse(expression.strip(), mode="eval").body
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("需要函数调用，且不支持关键字参数")
        name = node.func.id
        args = tuple(ast.literal_eval(arg) for arg in node.args)
    except (SyntaxError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("占位符语法错误，参数必须是字面量") from exc
    counts = {"rand": (2,), "randf": (2, 3), "randstr": (1,),
              "randdate": (2,), "randdt": (2,), "uuid": (0,), "var": (1,),
              "sample": (2, 3)}
    if name not in (*counts, "pick", "pickw"):
        raise ValueError(f"未知占位符函数：{name}")
    if name in counts and len(args) not in counts[name]:
        raise ValueError(f"{name} 参数数量错误")
    if name == "var":
        if not allow_var:
            raise ValueError("变量生成规则不允许引用变量")
        if type(args[0]) is not str or args[0] not in variables:
            raise ValueError(f"未定义变量：{args[0]}")
    elif name == "sample":
        if any(type(a) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a) for a in args[:2]):
            raise ValueError("sample 表名和列名必须为安全标识符")
        options = args[2] if len(args) == 3 else {}
        if type(options) is not dict:
            raise ValueError("sample 第三个参数必须是选项字典")
        allowed = {"mode", "sample_size", "where", "time_column", "sample_ratio", "random"}
        unknown = set(options) - allowed
        if unknown:
            raise ValueError(f"sample 不支持的选项：{', '.join(sorted(unknown))}")
        mode = options.get("mode", "uniform")
        if mode not in ("uniform", "weighted", "recent"):
            raise ValueError("sample mode 仅支持 uniform、weighted、recent")
        sample_size = options.get("sample_size", 10000)
        if type(sample_size) is not int or not 1 <= sample_size <= 10000:
            raise ValueError("sample_size 必须为 1～10000 的整数")
        sample_ratio = options.get("sample_ratio", 100)
        if type(sample_ratio) not in (int, float) or not 0 <= sample_ratio <= 100:
            raise ValueError("sample_ratio 必须为 0～100 的数字")
        random_expression = options.get("random")
        random_generator = None
        if sample_ratio < 100:
            if type(random_expression) is not str:
                raise ValueError("sample_ratio 低于 100% 时必须配置 random 生成表达式")
            random_generator = parse_generator(random_expression, allow_var=False)
        where = options.get("where")
        if where is not None and (type(where) is not str or not re.fullmatch(r"[\w\s.`=<>!()'\"%-]+", where)):
            raise ValueError("sample where 只能包含受限的静态条件字符")
        time_column = options.get("time_column")
        if time_column is not None and (type(time_column) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", time_column)):
            raise ValueError("sample time_column 必须为安全标识符")
        args = (args[0], args[1], {"mode": mode, "sample_size": sample_size,
                                   "sample_ratio": sample_ratio,
                                   **({"where": where} if where else {}),
                                   **({"time_column": time_column} if time_column else {}),
                                   **({"random_generator": random_generator} if random_generator else {})})
    elif name in ("rand", "randstr"):
        if any(type(a) is not int for a in args):
            raise ValueError(f"{name} 参数必须是整数")
        if name == "rand" and args[0] > args[1]:
            raise ValueError("rand 下限不能大于上限")
        if name == "randstr" and not 1 <= args[0] <= 4096:
            raise ValueError("randstr 长度必须为 1～4096")
    elif name == "randf":
        if any(type(a) not in (int, float) or not math.isfinite(a) for a in args[:2]):
            raise ValueError("randf 边界必须是有限数字")
        digits = args[2] if len(args) == 3 else 2
        if type(digits) is not int or not 0 <= digits <= 10:
            raise ValueError("randf 小数位必须为 0～10 的整数")
        lo, hi = (Decimal(str(a)) for a in args[:2])
        if lo > hi:
            raise ValueError("randf 下限不能大于上限")
        with localcontext() as ctx:
            ctx.prec = 340
            low = int(lo.scaleb(digits).to_integral_value(rounding=ROUND_CEILING))
            high = int(hi.scaleb(digits).to_integral_value(rounding=ROUND_FLOOR))
        if low > high:
            raise ValueError("randf 区间内没有符合指定精度的值")
        args = (low, high, digits)
    elif name == "pick":
        if not args:
            raise ValueError("pick 至少需要一个候选值")
        for a in args:
            _scalar(a)
    elif name == "pickw":
        if not args:
            raise ValueError("pickw 至少需要一个候选值")
        values, weights = [], []
        for pair in args:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("pickw 参数必须是 (值, 权重) 元组")
            value, weight = pair
            _scalar(value)
            if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0:
                raise ValueError("pickw 权重必须是非负有限数字")
            values.append(value)
            weights.append(weight)
        total = sum(weights)
        if not math.isfinite(total) or total <= 0:
            raise ValueError("pickw 权重合计必须是大于零的有限数字")
        args = (tuple(values), tuple(weights))
    elif name in ("randdate", "randdt"):
        fmt = "%Y-%m-%d" if name == "randdate" else "%Y-%m-%d %H:%M:%S"
        pattern = r"\d{4}-\d{2}-\d{2}" + (r" \d{2}:\d{2}:\d{2}" if name == "randdt" else "")
        if any(type(a) is not str or not re.fullmatch(pattern, a) for a in args):
            raise ValueError(f"{name} 日期格式错误")
        start, end = (datetime.strptime(a, fmt) for a in args)
        if end < start:
            raise ValueError(f"{name} 结束时间不能早于开始时间")
        args = ((start.date(), (end - start).days) if name == "randdate"
                else (start, int((end - start).total_seconds())))
    return Generator(name, args)


def compile_variables(variables):
    result = {}
    for name, expression in variables.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise ValueError(f"变量名 {name!r} 必须为 1～64 位字母、数字、下划线，且不能以数字开头")
        try:
            result[name] = parse_generator(expression, allow_var=False)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"变量 {name}：{exc}") from exc
    return result


@dataclass
class Statement:
    sql: str
    generators: list

    def bind(self, values):
        return self.sql, tuple(g.sample(values) for g in self.generators)


def compile_statement(text, variables=(), label="SQL"):
    parts, generators = [], []
    offset = 0
    try:
        for kind, value, offset in tokens(text):
            if kind == "param":
                generators.append(parse_generator(value[2:-2], variables))
                parts.append("%s")
            else:
                parts.append(value.replace("%", "%%"))
    except (ValueError, OverflowError) as exc:
        line = text.count("\n", 0, offset) + 1
        column = offset - text.rfind("\n", 0, offset)
        raise ValueError(f"{label}，第 {line} 行第 {column} 列：{exc}") from exc
    return Statement("".join(parts), generators)


def compile_tasks(tasks, variables):
    definitions = compile_variables(variables)
    compiled = {}
    for task in tasks:
        compiled[task["sql_id"]] = [
            compile_statement(s, definitions, f"{task['sql_id']} 第 {i} 条 SQL")
            for i, s in enumerate(task["statements"], 1)
        ]
    return definitions, compiled
