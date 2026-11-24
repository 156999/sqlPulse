"""Regression checks for selecting localized pages and first-party assets."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from jinja2 import Environment, select_autoescape
from pydantic import ValidationError

from app.config import Settings, settings
from app.ui_language import (
    LocaleStaticFiles, LocaleTemplateLoader, js_catalog, ui_text,
)

APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_language_is_read_from_environment(monkeypatch, language):
    monkeypatch.setenv("SQLPULSE_UI_LANGUAGE", language)
    assert Settings(_env_file=None, auth_enabled=False).ui_language == language


def test_language_defaults_to_chinese_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("SQLPULSE_UI_LANGUAGE", raising=False)
    assert Settings(_env_file=None, auth_enabled=False).ui_language == "zh-CN"
    monkeypatch.setenv("SQLPULSE_UI_LANGUAGE", "unknown")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_enabled=False)


@pytest.mark.parametrize("language,expected", [("en", "Sign In"), ("zh-CN", "登录")])
def test_login_selects_language_and_keeps_html_escaping(monkeypatch, language, expected):
    monkeypatch.setattr(settings, "ui_language", language)
    env = Environment(
        loader=LocaleTemplateLoader(APP_DIR / "templates"),
        autoescape=select_autoescape(),
    )
    env.filters["ui_text"] = ui_text
    # Compile the entire selected tree, including rarely opened report fragments.
    for template in (APP_DIR / "templates").glob("*.html"):
        env.get_template(template.name)
    html = env.get_template("login.html").render(
        current_user=None, auth_enabled=True, auth_allow_register=True,
        username="<script>alert(1)</script>", error="", next="/runs/new",
    )
    assert f'<html lang="{language}">' in html
    assert expected in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_localized_assets_preserve_shared_static_files(monkeypatch, language):
    monkeypatch.setattr(settings, "ui_language", language)
    app = FastAPI()

    @app.get("/static/ui-language.js")
    def language_script():
        return Response(js_catalog(language), media_type="text/javascript")

    app.mount("/static", LocaleStaticFiles(APP_DIR / "static"))
    prefix = APP_DIR / "locales/en/static" if language == "en" else APP_DIR / "static"
    with TestClient(app) as client:
        for asset in ("run_form.js", "examples/demo.sql", "examples/datagen.sql"):
            response = client.get("/static/" + asset)
            assert response.status_code == 200
            assert response.content == (prefix / asset).read_bytes()
        shared = client.get("/static/monitor_timeline.js")
        assert shared.content == (APP_DIR / "static/monitor_timeline.js").read_bytes()
        assert client.get("/static/ui-language.js").status_code == 200


def test_dynamic_diagnostics_preserve_measurements_and_source_values(monkeypatch):
    monkeypatch.setattr(settings, "ui_language", "en")
    message = "存在可优化项（置信度中：样本不足）"
    assert ui_text(message) == (
        "Optimization opportunities found (confidence: medium; sample too small)"
    )
    assert ui_text("阈值 0.5，样本 14 点") == "Threshold: 0.5; samples: 14"
    assert ui_text("任务 bdc69690 不存在") == "Test bdc69690 was not found."
    assert ui_text(23.7) == 23.7
    assert message == "存在可优化项（置信度中：样本不足）"
    monkeypatch.setattr(settings, "ui_language", "zh-CN")
    assert ui_text(message) == message
