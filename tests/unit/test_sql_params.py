from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pymysql
import pytest

from app.services.runner import parse_sql_tasks, render_locustfile, split_statements
from app.services.sql_params import compile_statement, compile_tasks, compile_variables


@pytest.mark.parametrize("expression", [
    "unknown()", "rand(2,1)", "rand(True,2)", "rand(1.5,2)", "rand(1)",
    "randstr(0)", "randstr(-1)", "randstr(4097)", "pick()", "pick([1])",
    "pick({'a':1})", "pick(1e999)", "pickw(('a',-1),('b',2))",
    "pickw(('a',0))", "pickw(('a',1e999))", "pickw(('a',1e308),('b',1e308))",
    "pickw(('a',))", "pickw(['a',1])", "pickw(('a',True))", "pickw()",
    "randf(2,1)", "randf(1,2,11)", "randf(1,2,True)", "randf(1,1e999)",
    "randf(0.001,0.009,2)", "randdate('2026-1-01','2026-01-02')",
    "randdate('2026-02-30','2026-03-01')", "randdate('2026-02-01','2026-01-01')",
    "randdt('2026-01-01','2026-01-02')", "uuid(1)", "var('missing')",
    "pick(__import__('os'))", "rand(a=1,b=2)", "sample('bad-name','id')",
    "sample('users','id', {'mode':'scan'})", "sample('users','id', {'sample_size':10001})",
])
def test_invalid_templates_rejected(expression):
    with pytest.raises(ValueError, match="sql_1 第 1 条 SQL"):
        compile_tasks(parse_sql_tasks("SELECT {{" + expression + "}}"), {})


def test_quotes_comments_delimiters_and_weight_text_are_opaque():
    sql = "SELECT '{{rand(1,2)}};\n-- weight: 99', `a;b`, {{pick('}};','a')}}; /* ; {{bad()}} */ SELECT 2"
    tasks = parse_sql_tasks(sql)
    assert len(tasks) == 2
    assert [t['weight'] for t in tasks] == [1, 1]
    statement = compile_statement(tasks[0]['statements'][0])
    query, args = statement.bind({})
    assert "'{{rand(1,2)}};\n-- weight: 99'" in query
    assert args[0] in ('}};', 'a')
    untouched = "SELECT '{{uuid()}}', `{{rand(1,2)}}` /* {{bad()}} */ -- {{bad()}}"
    query, args = compile_statement(untouched).bind({})
    assert query == untouched and args == ()
    assert split_statements("SELECT 5--2;") == ["SELECT 5--2"]


@pytest.mark.parametrize("server_status", [0, 512])
def test_driver_escaping_and_percent_preservation(server_status):
    connection = pymysql.connect(defer_connect=True)
    connection.server_status = server_status
    cursor = connection.cursor()
    value = "O'Reilly\\中文\n\"% {{uuid()}}"
    sql = "SELECT {{pick(" + repr(value) + ")}}, 5 % 2, 'abc%', '%s'"
    statement = compile_statement(sql)
    query, args = statement.bind({})
    assert args == (value,)
    assert cursor.mogrify(query, args) == "SELECT " + connection.literal(value) + ", 5 % 2, 'abc%', '%s'"
    assert cursor.mogrify(*compile_statement("SELECT '100%', 5 % 2").bind({})) == "SELECT '100%', 5 % 2"


def test_typed_values_and_decimal_grid():
    statement = compile_statement("SELECT {{pick(None)}}, {{pick(True)}}, {{pickw((42,1))}}, "
                                  "{{randdate('2026-01-01','2026-01-01')}}, "
                                  "{{randdt('2026-01-01 00:00:00','2026-01-01 00:00:00')}}, "
                                  "{{randf(-0.019,0.019,2)}}")
    for _ in range(20):
        _, args = statement.bind({})
        assert args[:5] == (None, True, 42, date(2026, 1, 1), datetime(2026, 1, 1))
        assert isinstance(args[5], Decimal)
        assert args[5] in (Decimal('-0.01'), Decimal('0.00'), Decimal('0.01'))


def test_sample_placeholder_is_typed_and_uses_runtime_sampler():
    statement = compile_statement("SELECT {{sample('users','city', {'mode':'weighted','sample_size':10})}}")
    query, args = statement.bind({"__sample__": lambda table, column, options: (table, column, options["mode"])})
    assert query == "SELECT %s"
    assert args == (("users", "city", "weighted"),)


def test_sample_placeholder_supports_random_mix_ratio():
    statement = compile_statement(
        "SELECT {{sample('users','id', {'sample_ratio': 0, 'random': 'rand(7,7)'})}}"
    )
    query, args = statement.bind({"__sample__": lambda *_: 99})
    assert query == "SELECT %s"
    assert args == (7,)


def test_variables_cannot_reference_variables():
    with pytest.raises(ValueError, match="不允许引用变量"):
        compile_variables({'x': "var('y')", 'y': 'uuid()'})
    with pytest.raises(ValueError, match="变量名"):
        compile_variables({'1x': 'uuid()'})


def test_error_location():
    with pytest.raises(ValueError, match="第 2 行第 8 列"):
        compile_statement("SELECT 1,\n       {{rand(9,1)}}")


@pytest.mark.parametrize("sql", ["SELECT {{rand(1,2)", "SELECT 'abc", "SELECT /* abc"])
def test_unclosed_tokens(sql):
    with pytest.raises(ValueError):
        parse_sql_tasks(sql)


def test_mysql_block_comments_preserved():
    assert split_statements("/* only a comment */") == []
    assert split_statements("SELECT /*+ MAX_EXECUTION_TIME(1000) */ 1;") == [
        "SELECT /*+ MAX_EXECUTION_TIME(1000) */ 1"]
    assert split_statements("/*!40101 SET @x=1 */;") == ["/*!40101 SET @x=1 */"]


def test_old_database_gets_empty_variable_configuration(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    connection = sqlite3.connect(tmp_path / 'old.db', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    old_schema = db.SCHEMA.replace("  variables_json TEXT NOT NULL DEFAULT '{}',\n", "")
    old_schema = old_schema.replace("  groups_json TEXT NOT NULL DEFAULT '[]',\n", "")
    connection.executescript(old_schema)
    connection.execute("INSERT INTO runs (id,name,status,sql_source,sql_content,concurrency,"
                       "spawn_rate,duration_sec,db_dsn_json,created_at) "
                       "VALUES ('old','old','finished','paste','SELECT 1',1,1,5,'{}','now')")
    connection.commit()
    monkeypatch.setattr(db, '_conn', connection)
    monkeypatch.setattr(db, 'seed_root_user', lambda: None)
    try:
        db.init_schema()
        db.init_schema()
        assert db.get_run('old')['variables_json'] == '{}'
        assert db.get_run('old')['groups_json'] == '[]'
    finally:
        connection.close()


def test_runtime_scope_binding_and_rollback(tmp_path, monkeypatch):
    for key in ('HOST', 'USER', 'PASSWORD', 'NAME'):
        monkeypatch.setenv('TARGET_DB_' + key, 'test')
    sql = "-- weight: 1\nBEGIN; SELECT {{var('id')}}, {{rand(1,9)}}; SELECT {{var('id')}}; COMMIT;"
    code = render_locustfile(parse_sql_tasks(sql), tmp_path / 'locustfile.py', {'id': 'rand(1,9)'})
    ns = {}
    exec(compile(code, 'locustfile.py', 'exec'), ns)
    events = MagicMock()
    user = ns['SqlUser'](SimpleNamespace(events=events))
    user.db = MagicMock()
    cursor = user.db.cursor.return_value.__enter__.return_value
    sequence = iter([1, 2, 3, 4, 5, 6])
    monkeypatch.setattr('app.services.sql_params.random.randint', lambda *args: next(sequence))
    user.sql_1()
    user.sql_1()
    calls = cursor.execute.call_args_list
    assert calls[1].args == ('SELECT %s, %s', (1, 2))
    assert calls[2].args == ('SELECT %s', (1,))
    assert calls[5].args == ('SELECT %s, %s', (3, 4))
    assert calls[6].args == ('SELECT %s', (3,))
    assert events.request.fire.call_count == 2
    cursor.execute.side_effect = RuntimeError('database failure')
    user.sql_1()
    user.db.rollback.assert_called_once()
    assert isinstance(events.request.fire.call_args.kwargs['exception'], RuntimeError)


def test_weight_group_without_transaction_reuses_variables():
    tasks = parse_sql_tasks("-- weight: 2\nSELECT {{var('x')}}; SELECT {{var('x')}};")
    definitions, compiled = compile_tasks(tasks, {'x': 'uuid()'})
    values = {name: g.sample({}) for name, g in definitions.items()}
    assert compiled['sql_1'][0].bind(values)[1] == compiled['sql_1'][1].bind(values)[1]
