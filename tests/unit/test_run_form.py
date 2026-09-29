import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models import SqlInput, FormTools, TaskCreate
from app.services.run_form import convert, normalize, group_tasks, prepare, preview, form_tools
from app.services.runner import parse_sql_tasks
from app.services import connection_manager
from app import db
from tests.unit.test_connections import client, api_app, tmp_db


def group(sql='SELECT 1;', mode='autocommit', name='订单查询', id='g1', weight=1):
    return dict(id=id, name=name, execution_mode=mode, weight=weight, sql=sql)


def test_sources_exclusive_and_weight_strict():
    for payload in ({}, {'groups': [], 'sql_content': 'SELECT 1'}, {'groups': [group(weight=0)]},
                    {'groups': [group(weight=1.5)]}, {'groups': [group(weight=True)]}):
        with pytest.raises(ValidationError):
            SqlInput(**payload)


def test_normalization_retains_comments_and_only_removes_outer_boundaries():
    source = "-- before\nBEGIN; -- inner\nSELECT 'COMMIT;BEGIN'; /* after */ COMMIT; -- tail"
    result = normalize(source)
    assert result['execution_mode'] == 'transaction'
    for text in ('-- before', '-- inner', "'COMMIT;BEGIN'", '/* after */', '-- tail'):
        assert text in result['sql']
    tasks, _, sql = group_tasks([group(source, 'transaction')])
    assert tasks[0]['statements'][0] == 'BEGIN'
    assert tasks[0]['statements'][-1] == 'COMMIT'
    assert len(tasks[0]['statements']) == 3
    assert parse_sql_tasks(sql)[0]['statements'] == tasks[0]['statements']
    with pytest.raises(ValueError, match='冲突'):
        group_tasks([group(source)])


@pytest.mark.parametrize('sql', [
    'BEGIN; SELECT 1;', 'BEGIN; SELECT 1; ROLLBACK;', 'SAVEPOINT a;',
    'SET @@session.autocommit=0; SELECT 1;', 'BEGIN; BEGIN; SELECT 1; COMMIT; COMMIT;',
    'BEGIN; SELECT 1; COMMIT; BEGIN; SELECT 2; COMMIT;',
    'XA START \'a\';',
])
def test_complex_transactions_stay_in_text(sql):
    with pytest.raises(ValueError):
        group_tasks([group(sql)])
    with pytest.raises(ValueError):
        convert('-- weight: 1\n' + sql)


def test_round_trip_preserves_group_semantics_comments_and_percent():
    source = "-- header\n-- weight: 70 query\nSELECT '{{var(\"x\")}}', 'abc%'; -- after\nSELECT 2;\n-- weight: 30\nBEGIN;\nSELECT 3;\nCOMMIT; -- end"
    groups = convert(source)
    assert len(groups) == 2
    assert groups[1]['execution_mode'] == 'transaction'
    assert '-- header' in groups[0]['sql']
    assert '-- query' in groups[0]['sql']
    assert '-- after' in groups[0]['sql']
    assert '-- end' in groups[1]['sql']
    tasks, _, text = group_tasks(groups)
    assert [t['weight'] for t in tasks] == [70, 30]
    assert group_tasks(convert(text))[0] == tasks


def test_unweighted_conversion_does_not_merge_statements():
    groups = convert('SELECT 1; SELECT 2; BEGIN; SELECT 3; SELECT 4; COMMIT;')
    assert len(groups) == 3
    assert groups[-1]['execution_mode'] == 'transaction'


def test_preview_scope_types_and_no_database(monkeypatch):
    monkeypatch.setattr(connection_manager, 'test_dsn', lambda *_: pytest.fail('preview must not connect'))
    values = iter(['first', 'second'])
    monkeypatch.setattr('app.services.sql_params.uuid.uuid4', lambda: next(values))
    sql = "SELECT {{var('id')}}, {{var('id')}}, {{randf(1,1,2)}}, {{pick(None)}};"
    body = SqlInput(groups=[group(sql), group(sql, id='g2')], variables={'id': 'uuid()'})
    result = preview(body)
    first, second = result['groups']
    assert first['variables']['id']['value'] == 'first'
    assert second['variables']['id']['value'] == 'second'
    args = first['statements'][0]['parameters']
    assert args[0] == args[1]
    assert args[2] == {'type': 'Decimal', 'value': '1.00'}
    assert args[3] == {'type': 'null', 'value': 'NULL'}


def test_preview_errors_locate_original_editor_text(client):
    r = client.post('/api/runs/preview', json={'groups': [group('SELECT 1;\nSELECT {{var(\'missing\')}};')]})
    assert r.status_code == 200
    error = r.json()['errors'][0]
    assert error['group_id'] == 'g1' and error['statement'] == 2
    assert error['line'] == 2 and error['column'] == 8
    assert not r.json()['ok']


def test_text_preview_errors_use_global_editor_location(client):
    response = client.post('/api/runs/preview', json={'sql_content': '-- weight: 1\nSELECT 1;\nSELECT {{unknown()}};'})
    error = response.json()['errors'][0]
    assert error['line'] == 3 and error['column'] == 8 and error['statement'] == 2
    response = client.post('/api/runs/preview', json={'groups': [group("SELECT 1;\nSELECT {{rand(1,2)")]})
    assert response.json()['errors'][0]['line'] == 2


def test_preview_is_authenticated(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as c:
        assert c.post('/api/runs/preview', json={'sql_content': 'SELECT 1'}).status_code == 401
        assert c.post('/api/runs/form-tools', json={'action': 'convert', 'sql_content': 'SELECT 1'}).status_code == 401


def test_variable_rename_does_not_change_quoted_or_commented_text():
    source = "SELECT {{ var('old') }}, '{{var(\"old\")}}'; -- {{var('old')}}"
    result = form_tools(FormTools(action='rename', sql_content=source, old_name='old', new_name='new'))['sql_content']
    assert result == "SELECT {{var('new')}}, '{{var(\"old\")}}'; -- {{var('old')}}"


def test_variable_migration_preserves_scalar_types_and_characters():
    literal = "a,b'\\\n中文"
    config = {'x': f'pick({literal!r},1,1.5,True,None)', 'y': "pickw(('A',2),('B',3))", 'z': 'randf(1,2)'}
    rows = form_tools(FormTools(action='variables', variables=config))['rows']
    assert [c['type'] for c in rows[0]['choices']] == ['string', 'integer', 'number', 'boolean', 'null']
    assert rows[0]['choices'][0]['value'] == literal
    assert rows[1]['choices'][1]['weight'] == '3'
    assert rows[2]['args'] == ['1', '2', '2']


def test_warning_does_not_reject_quoted_placeholder_or_ddl():
    result = preview(SqlInput(groups=[group("CREATE TABLE t(id INT); SELECT '{{uuid()}}';", 'transaction')]))
    assert len(result['groups'][0]['warnings']) == 2


def test_structured_run_persists_groups_and_revalidates(client, monkeypatch):
    from app.services.runner import runner
    monkeypatch.setattr(connection_manager, 'test_dsn', lambda *_: {'ok': True})
    monkeypatch.setattr(runner, 'start', lambda *_: None)
    groups = [group("SELECT {{var('id')}};", 'transaction')]
    body = dict(name='form', groups=groups, variables={'id': 'rand(1,2)'}, concurrency=1, spawn_rate=1, duration_sec=5, db_dsn={})
    r = client.post('/api/runs', json=body)
    assert r.status_code == 201, r.text
    run_id = r.json()['run_id']
    stored = db.get_run(run_id)
    assert json.loads(stored['groups_json']) == groups
    assert client.get(f'/api/runs/{run_id}').json()['groups'] == groups
    body['variables'] = {}
    rejected = client.post('/api/runs', json=body)
    assert rejected.status_code == 400
    assert rejected.json()['detail']['group_id'] == 'g1'


def test_preview_and_serialize_routes_do_not_save_or_connect(client, monkeypatch):
    monkeypatch.setattr(db, 'create_run', lambda *_: pytest.fail('must not persist'))
    monkeypatch.setattr(connection_manager, 'test_dsn', lambda *_: pytest.fail('must not connect'))
    assert client.post('/api/runs/preview', json={'groups': [group()]}).json()['ok']
    assert 'sql_content' in client.post('/api/runs/form-tools', json={'action': 'serialize', 'groups': [group()]}).json()


def test_new_form_page_is_rendered(client):
    response = client.get('/runs/new')
    assert response.status_code == 200
    for text in ('SQL 任务组', 'variable-list', '/static/run_form.js', '校验与预览'):
        assert text in response.text


def test_import_summary_allows_missing_definitions_but_rejects_bad_functions(client):
    source = "-- weight: 70\nSELECT {{var('order_id')}};\n-- weight: 30\nBEGIN; SELECT 2; COMMIT;"
    response = client.post('/api/runs/form-tools', json={'action': 'import', 'sql_content': source})
    assert response.status_code == 200
    data = response.json()
    assert len(data['groups']) == 2
    assert data['groups'][1]['execution_mode'] == 'transaction'
    assert data['summary'][0]['references'] == ['order_id']
    assert data['summary'][1]['statement_count'] == 1
    rejected = client.post('/api/runs/form-tools', json={'action': 'import', 'sql_content': 'SELECT {{missing()}};'})
    assert rejected.status_code == 400


@pytest.mark.parametrize('source', [
    '-- weight: 0\nSELECT 1;', '-- weight: -1\nSELECT 1;', '-- weight: 1.5\nSELECT 1;',
    '-- weight: 1\nSELECT 1;\n-- weight: 2\nBEGIN; SELECT 2; ROLLBACK;',
    '-- only comments', '',
])
def test_import_is_all_or_nothing_and_does_not_offer_direct_text_submission(client, source):
    response = client.post('/api/runs/form-tools', json={'action': 'import', 'sql_content': source})
    assert response.status_code == 400
    assert 'groups' not in response.json()
    assert '请使用文本模式' not in response.text


def test_import_does_not_connect_or_persist(client, monkeypatch):
    monkeypatch.setattr(db, 'create_run', lambda *_: pytest.fail('must not persist import'))
    monkeypatch.setattr(connection_manager, 'test_dsn', lambda *_: pytest.fail('must not connect'))
    response = client.post('/api/runs/form-tools', json={'action': 'import', 'sql_content': "SELECT '{{uuid()}}';"})
    assert response.status_code == 200
    assert response.json()['summary'][0]['warnings']
