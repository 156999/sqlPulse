"""Opt-in: SQLPULSE_TEST_MYSQL=1, with TEST_MYSQL_* for a disposable MySQL database.

Only connection-local TEMPORARY tables are created; no existing tables are modified.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pymysql
import pytest

from app.services.run_form import group_tasks
from app.services.runner import render_locustfile

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('SQLPULSE_TEST_MYSQL') != '1', reason='requires opt-in test MySQL')]


@pytest.mark.parametrize('mode,expected', [('autocommit', 1), ('transaction', 0)])
def test_form_commit_and_failure_rollback(mode, expected, tmp_path, monkeypatch):
    connection = pymysql.connect(
        host=os.environ.get('TEST_MYSQL_HOST', '127.0.0.1'),
        port=int(os.environ.get('TEST_MYSQL_PORT', '3306')),
        user=os.environ.get('TEST_MYSQL_USER', 'root'),
        password=os.environ.get('TEST_MYSQL_PASSWORD', ''),
        database=os.environ.get('TEST_MYSQL_DATABASE', 'sqlpulse_demo'),
        autocommit=True,
    )
    try:
        with connection.cursor() as cur:
            cur.execute('CREATE TEMPORARY TABLE form_commit_probe (id INT PRIMARY KEY) ENGINE=InnoDB')
        tasks = group_tasks([dict(id='probe', name='probe', weight=1, execution_mode=mode,
                                 sql='INSERT INTO form_commit_probe VALUES (1); '
                                     'INSERT INTO form_commit_probe VALUES (1); '
                                     'INSERT INTO form_commit_probe VALUES (2);')])[0]
        for name in ('HOST', 'USER', 'PASSWORD', 'NAME'):
            monkeypatch.setenv('TARGET_DB_' + name, 'unused')
        namespace = {}
        code = render_locustfile(tasks, tmp_path / 'probe.py')
        exec(compile(code, 'probe.py', 'exec'), namespace)
        events = MagicMock()
        user = namespace['SqlUser'](SimpleNamespace(events=events))
        user.db = connection
        user.sql_1()
        assert events.request.fire.call_args.kwargs.get('exception') is not None
        with connection.cursor() as cur:
            cur.execute('SELECT COUNT(*), COALESCE(MAX(id),0) FROM form_commit_probe')
            assert cur.fetchone() == (expected, expected)
    finally:
        connection.close()
