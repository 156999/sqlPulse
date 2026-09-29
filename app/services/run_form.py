"""Shared, database-free form conversion, validation and preview."""
import ast
import re
from datetime import date, datetime
from decimal import Decimal

from app.services.sql_params import tokens, parse_generator, compile_variables, compile_tasks
from app.services.runner import parse_sql_tasks, split_statements, WEIGHT_RE


class FormError(ValueError):
    def __init__(self, message, **location):
        super().__init__(message)
        self.detail = {"message": message, **location}


def located_error(exc, source, **location):
    match = re.search(r'第 (\d+) 行第 (\d+) 列', str(exc))
    if match:
        line, column = map(int, match.groups())
        location.update(line=line, column=column,
                        offset=sum(len(s) for s in source.splitlines(keepends=True)[:line-1]) + column-1)
    return FormError(str(exc), **location)


def statement_spans(sql):
    start = 0
    result = []
    for kind, value, offset in tokens(sql):
        if kind == "code" and value == ";":
            if split_statements(sql[start:offset]):
                result.append((start, offset, offset + 1))
            start = offset + 1
    if split_statements(sql[start:]):
        result.append((start, len(sql), len(sql)))
    return result


def code_text(sql):
    return " ".join("".join(v if k == "code" else " " for k, v, _ in tokens(sql)).upper().split())


def transaction_control(sql):
    code = code_text(sql)
    return bool(re.match(r"^(BEGIN\b|START\s+TRANSACTION\b|COMMIT\b|ROLLBACK\b|SAVEPOINT\b|RELEASE\s+SAVEPOINT\b|SET\b.*\bAUTOCOMMIT\b|XA\b)", code))


def normalize(sql):
    spans = statement_spans(sql)
    if not spans:
        return {"sql": sql, "execution_mode": "autocommit", "normalized": False}
    texts = [sql[a:b] for a, b, _ in spans]
    controls = [i for i, text in enumerate(texts) if transaction_control(text)]
    if not controls:
        return {"sql": sql, "execution_mode": "autocommit", "normalized": False}
    if len(spans) >= 3 and controls == [0, len(spans) - 1] and code_text(texts[0]) in ("BEGIN", "START TRANSACTION") and code_text(texts[-1]) == "COMMIT":
        # Remove only control tokens and delimiters; retain surrounding comments.
        removals = []
        for index in (0, len(spans) - 1):
            a, b, end = spans[index]
            for kind, value, offset in tokens(sql[a:b]):
                if kind == "code":
                    removals.append((a + offset, a + offset + len(value)))
            removals.append((b, end))
        for a, b in sorted(removals, reverse=True):
            sql = sql[:a] + sql[b:]
        return {"sql": sql.strip(), "execution_mode": "transaction", "normalized": True}
    raise ValueError("包含复杂或不完整的事务控制，请保留原文并使用文本模式")


def group_tasks(groups):
    tasks, normalized, sections = [], [], []
    ids = set()
    for group in groups:
        g = group.model_dump() if hasattr(group, "model_dump") else dict(group)
        if g['id'] in ids:
            raise FormError("任务组标识重复", group_id=g['id'])
        ids.add(g['id'])
        if not g['sql'].strip():
            continue
        try:
            for kind, value, offset in tokens(g['sql']):
                if kind == 'comment' and WEIGHT_RE.match(value):
                    raise ValueError("表单 SQL 内不能包含 weight 分组指令，请使用权重字段或文本模式")
                if kind == 'comment' and value.startswith('/*!'):
                    raise ValueError("MySQL 可执行注释请使用文本模式，以保留执行语义")
            normal = normalize(g['sql'])
            if normal['normalized'] and g['execution_mode'] != 'transaction':
                raise ValueError("SQL 包含事务边界，与逐条提交冲突；请识别事务或切换到文本模式")
            sql = normal['sql']
            statements = split_statements(sql)
            if not statements:
                continue
            transaction = g['execution_mode'] == 'transaction'
            if transaction:
                statements = ['BEGIN', *statements, 'COMMIT']
            tasks.append({'sql_id': f'sql_{len(tasks)+1}', 'weight': g['weight'], 'statements': statements})
            normalized.append({**g, 'sql': sql})
            # A newline before the delimiter prevents trailing -- comments swallowing it.
            sql_tokens = list(tokens(sql))
            significant = [(k, v, pos) for k, v, pos in sql_tokens if k != 'comment' and v.strip()]
            terminated = significant and significant[-1][:2] == ('code', ';')
            # Isolate trailing block comments from the generated COMMIT statement.
            block_tail = terminated and any(k == 'comment' and v.startswith('/*') and pos > significant[-1][2]
                                            for k, v, pos in sql_tokens)
            body = sql + ('\n' if terminated and not block_tail else '\n;')
            if transaction:
                body = 'BEGIN;\n' + body + '\nCOMMIT;'
            sections.append(f"-- weight: {g['weight']}\n{body}")
        except ValueError as exc:
            raise located_error(exc, g['sql'], group_id=g['id'], group_name=g['name']) from exc
    if not tasks:
        raise FormError("请至少填写一个有可执行 SQL 的任务组")
    return tasks, normalized, '\n\n'.join(sections)


def prepare(body):
    if body.groups is not None:
        tasks, groups, sql = group_tasks(body.groups)
    else:
        sql = body.sql_content
        try:
            tasks = parse_sql_tasks(sql)
        except ValueError as exc:
            raise located_error(exc, sql) from exc
        groups = []
    if not tasks:
        raise FormError("没有可执行的 SQL 语句")
    for task in tasks:
        if task['weight'] <= 0:
            raise FormError("权重必须为正整数")
    for name, expression in body.variables.items():
        try:
            compile_variables({name: expression})
        except ValueError as exc:
            raise FormError(str(exc), variable=name) from exc
    if not groups:
        for number, (a, b, _) in enumerate(statement_spans(sql), 1):
            for kind, value, offset in tokens(sql[a:b]):
                if kind != 'param':
                    continue
                try:
                    parse_generator(value[2:-2], body.variables)
                except (ValueError, OverflowError) as exc:
                    pos = a + offset
                    raise FormError(str(exc), statement=number, line=sql.count('\n', 0, pos)+1,
                                    column=pos-sql.rfind('\n', 0, pos), offset=pos) from exc
    # Validate against editor text so locations refer to original editor coordinates.
    for index, task in enumerate(tasks):
        source = groups[index]['sql'] if groups else None
        if source is not None:
            spans = statement_spans(source)
            for number, (a, b, _) in enumerate(spans, 1):
                for kind, value, offset in tokens(source[a:b]):
                    if kind != 'param':
                        continue
                    try:
                        parse_generator(value[2:-2], body.variables)
                    except (ValueError, OverflowError) as exc:
                        pos = a + offset
                        raise FormError(str(exc), group_id=groups[index]['id'], group_name=groups[index]['name'],
                                        statement=number, line=source.count('\n', 0, pos)+1,
                                        column=pos-source.rfind('\n', 0, pos), offset=pos) from exc
    definitions, compiled = compile_tasks(tasks, body.variables)
    return tasks, groups, sql, definitions, compiled


def typed(value):
    if value is None:
        return {'type': 'null', 'value': 'NULL'}
    if isinstance(value, (Decimal, datetime, date)):
        return {'type': type(value).__name__, 'value': str(value)}
    if type(value) is int:
        return {'type': 'int', 'value': str(value)}
    return {'type': type(value).__name__, 'value': value}


def references(sql):
    result = []
    for kind, value, offset in tokens(sql):
        if kind != 'param':
            continue
        try:
            node = ast.parse(value[2:-2].strip(), mode='eval').body
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'var' and len(node.args) == 1 and not node.keywords:
                name = ast.literal_eval(node.args[0])
                if isinstance(name, str):
                    result.append({'name': name, 'offset': offset, 'length': len(value)})
        except (SyntaxError, ValueError):
            continue
    return result


def preview(body):
    tasks, groups, _, definitions, compiled = prepare(body)
    total = sum(task['weight'] for task in tasks)
    result = []
    for i, task in enumerate(tasks):
        values = {name: generator.sample({}) for name, generator in definitions.items()}
        warnings = []
        used = set()
        for s in task['statements']:
            used.update(ref['name'] for ref in references(s))
            if re.match(r'^(CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE|LOCK|UNLOCK)\b', code_text(s)):
                warnings.append('包含可能隐式提交的语句，不能保证整体回滚。')
            if any(k == 'quoted' and '{{' in v for k, v, _ in tokens(s)):
                warnings.append('引号内的占位符文本保持原样，不会生成随机值。')
        statements = []
        for statement in compiled[task['sql_id']]:
            query, args = statement.bind(values)
            statements.append({'sql': query, 'parameters': [typed(v) for v in args]})
        result.append({
            'id': groups[i]['id'] if groups else task['sql_id'],
            'name': groups[i]['name'] if groups else task['sql_id'],
            'execution_mode': groups[i]['execution_mode'] if groups else 'text',
            'share': round(task['weight']/total*100, 2),
            'variables': {k: typed(values[k]) for k in sorted(used)},
            'statements': statements, 'warnings': sorted(set(warnings)),
        })
    return {'ok': True, 'groups': result, 'errors': []}


def convert(sql):
    """Keep original comments and SQL slices; reject any ambiguous transaction mapping."""
    segments, buf, weight = [], [], None
    for kind, value, offset in tokens(sql):
        if kind == 'comment' and WEIGHT_RE.match(value) and not sql[sql.rfind('\n', 0, offset)+1:offset].strip():
            segments.append((''.join(buf), weight))
            m = WEIGHT_RE.match(value)
            weight = int(m.group(1))
            suffix = value[m.end():].strip()
            buf = ['-- ' + suffix] if suffix else []
        else:
            buf.append(value)
    segments.append((''.join(buf), weight))
    groups, pending = [], ''
    for text, weight in segments:
        if not split_statements(text):
            pending += text
            continue
        text = pending + text
        pending = ''
        spans = statement_spans(text)
        pieces = [text] if weight is not None else []
        if weight is None:
            start = 0
            opened = False
            for a, b, end in spans:
                code = code_text(text[a:b])
                if code in ('BEGIN', 'START TRANSACTION'):
                    if opened:
                        raise ValueError('嵌套事务不能转换为表单，请使用文本模式')
                    opened = True
                if not opened or code == 'COMMIT':
                    pieces.append(text[start:end])
                    start = end
                    opened = False
            if opened:
                raise ValueError('事务边界不完整，请使用文本模式')
            if pieces:
                pieces[-1] += text[start:]
        for piece in pieces:
            normal = normalize(piece)
            groups.append({'id': f'group-{len(groups)+1}', 'name': f'任务组 {len(groups)+1}',
                           'weight': weight if weight is not None else 1,
                           'execution_mode': normal['execution_mode'], 'sql': normal['sql']})
    if pending and groups:
        groups[-1]['sql'] += pending
    if not groups and sql.strip():
        raise ValueError('内容没有可执行 SQL，请保留在文本模式')
    if groups:
        group_tasks(groups)
    return groups


def variable_rows(variables):
    compile_variables(variables)
    rows = []
    def scalar(v):
        typ = 'null' if v is None else 'boolean' if type(v) is bool else 'integer' if type(v) is int else 'number' if type(v) is float else 'string'
        return {'type': typ, 'value': '' if v is None else str(v) if type(v) is not bool else str(v).lower()}
    for name, expression in variables.items():
        node = ast.parse(expression.strip(), mode='eval').body
        args = [ast.literal_eval(a) for a in node.args]
        row = {'name': name, 'type': node.func.id, 'args': [], 'choices': []}
        if row['type'] in ('pick', 'pickw'):
            row['choices'] = [dict(scalar(a[0]), weight=str(a[1])) if row['type'] == 'pickw' else scalar(a) for a in args]
        else:
            row['args'] = [str(a) for a in args]
            if row['type'] == 'randf' and len(args) == 2:
                row['args'].append('2')
        rows.append(row)
    return rows


def form_tools(body):
    if body.action == 'import':
        try:
            for kind, value, offset in tokens(body.sql_content):
                if kind == 'comment' and re.match(r'^--\s*weight:', value, re.I) and not body.sql_content[body.sql_content.rfind('\n', 0, offset)+1:offset].strip():
                    directive = re.match(r'^--\s*weight:\s*(\d+)(?=\s|$)', value, re.I)
                    if not directive or int(directive[1]) <= 0:
                        raise ValueError('weight 必须为正整数，请修改后重新导入')
            groups = convert(body.sql_content)
        except ValueError as exc:
            message = str(exc).replace('请保留原文并使用文本模式', '请修改原文后重新导入').replace(
                '请使用文本模式', '请修改脚本后重新导入')
            location = {k: v for k, v in getattr(exc, 'detail', {}).items() if k != 'message'}
            raise FormError('本次导入未应用到表单。' + message, **location) from exc
        if not groups:
            raise ValueError('请输入包含可执行语句的 SQL')
        summary = []
        for g in groups:
            if type(g['weight']) is not int or g['weight'] <= 0:
                raise FormError('权重必须为正整数', group_id=g['id'], group_name=g['name'])
            refs = sorted({r['name'] for r in references(g['sql'])})
            warnings = []
            for kind, value, offset in tokens(g['sql']):
                if kind == 'param':
                    try:
                        # Missing definitions may be supplied after applying to the form.
                        parse_generator(value[2:-2], refs)
                    except (ValueError, OverflowError) as exc:
                        raise FormError(str(exc), group_name=g['name'],
                                        line=g['sql'].count('\n', 0, offset)+1,
                                        column=offset-g['sql'].rfind('\n', 0, offset)) from exc
                if kind == 'quoted' and '{{' in value:
                    warnings.append('引号内的占位符文本不会生成随机值。')
            if any(re.match(r'^(CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE|LOCK|UNLOCK)\b', code_text(s))
                   for s in split_statements(g['sql'])):
                warnings.append('包含可能隐式提交的语句，不能保证整体回滚。')
            summary.append({'id': g['id'], 'statement_count': len(split_statements(g['sql'])),
                            'references': refs, 'warnings': sorted(set(warnings))})
        return {'groups': groups, 'summary': summary}
    if body.action == 'convert':
        return {'groups': convert(body.sql_content)}
    if body.action == 'serialize':
        return {'sql_content': group_tasks(body.groups)[2]} if any(g.sql.strip() for g in body.groups) else {'sql_content': ''}
    if body.action == 'normalize':
        return normalize(body.sql_content)
    if body.action == 'variables':
        return {'rows': variable_rows(body.variables)}
    if body.action == 'analyze':
        return {'references': references(body.sql_content)}
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', body.new_name):
        raise ValueError('新变量名不合法')
    sql = body.sql_content
    for ref in reversed(references(sql)):
        if ref['name'] == body.old_name:
            sql = sql[:ref['offset']] + '{{var(' + repr(body.new_name) + ')}}' + sql[ref['offset']+ref['length']:]
    return {'sql_content': sql}
