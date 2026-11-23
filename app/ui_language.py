"""Language selection for the web UI without changing stored data or the API."""

import json
import re
from functools import lru_cache
from pathlib import Path

from jinja2 import FileSystemLoader
from starlette.staticfiles import StaticFiles

from app.config import settings

LOCALES = Path(__file__).with_name("locales")
_LOCALIZED_ASSETS = frozenset({"run_form.js", "examples/demo.sql", "examples/datagen.sql"})
_HAN = re.compile(r"[\u3400-\u9fff]")


def is_english() -> bool:
    return settings.ui_language == "en"


class LocaleTemplateLoader(FileSystemLoader):
    """English templates override their counterparts in the default template tree."""

    def __init__(self, templates_dir: Path):
        paths = [LOCALES / "en" / "templates", templates_dir] if is_english() else [templates_dir]
        super().__init__(paths)


class LocaleStaticFiles(StaticFiles):
    """Keep the original URLs and localize only selected first-party assets."""

    def __init__(self, shared_dir: Path):
        super().__init__(directory=shared_dir)
        self.english = StaticFiles(directory=LOCALES / "en" / "static")

    async def get_response(self, path, scope):
        if is_english() and path.replace("\\", "/") in _LOCALIZED_ASSETS:
            return await self.english.get_response(path, scope)
        return await super().get_response(path, scope)


@lru_cache(maxsize=1)
def messages():
    return json.loads((LOCALES / "en" / "messages.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _message_pattern():
    return re.compile("|".join(map(re.escape, sorted(messages(), key=len, reverse=True))))


_FORMATTED_MESSAGES = (
    (r"发现 (\d+) 处计划问题", r"\1 execution plan issue(s) found"),
    (r"被采集的 (\d+) 条语句中有 (\d+) 处值得关注。.*",
     r"\2 issue(s) found across \1 collected statements. Use these plan findings alongside the measured statement latency."),
    (r"被采集的 (\d+) 条语句都没有坏味道（共 (\d+) 条语句）。",
     r"No issues in the \1 collected statements (\2 submitted)."),
    (r"(.+) 第 (\d+) 条", r"\1, statement \2"),
    (r"表 (.+?) 全表扫描（type=ALL，预估 (.+?) 行）。?",
     r"Table \1: full table scan (type=ALL, estimated rows: \2)."),
    (r"阈值 (.+?)，样本 (\d+) 点", r"Threshold: \1; samples: \2"),
    (r"另有 (\d+) 条语句不产生执行计划（事务控制、SET 等）：(.+)",
     r"\1 additional statement(s) do not produce execution plans (transaction control, SET, etc.): \2"),
    (r"另有 (\d+) 条语句超出本轮采集额度，未采集：(.+)",
     r"\1 additional statement(s) exceeded the collection limit: \2"),
    (r"另有 (\d+) 条语句未采集：(.+)", r"\1 additional statement(s) were not collected: \2"),
    (r"任务 (.+?) 不存在", r"Test \1 was not found."),
    (r"任务 (.+?) 没有 EXPLAIN 采集产物", r"No EXPLAIN results for test \1."),
)
_CONFIDENCE = {"高": "high", "中": "medium", "低": "low"}


def ui_text(value):
    """Display app-owned messages in English. Unrecognized messages pass through."""
    if not is_english() or not isinstance(value, str) or not _HAN.search(value):
        return value
    catalog = messages()
    if value in catalog:
        return catalog[value]
    confidence = re.fullmatch(r"(.+)（置信度([高中低])：(.+)）", value)
    if confidence:
        title, level, reason = confidence.groups()
        details = "; ".join(ui_text(part) for part in reason.split("、"))
        return f"{ui_text(title)} (confidence: {_CONFIDENCE[level]}; {details})"
    limited = re.fullmatch(r"(未发现问题，但证据不足)（(.+)）", value)
    if limited:
        title, reason = limited.groups()
        return f"{ui_text(title)} ({'; '.join(ui_text(part) for part in reason.split('、'))})"
    for pattern, english in _FORMATTED_MESSAGES:
        if re.fullmatch(pattern, value):
            return re.sub(pattern, english, value)
    # A few report fields combine several app-owned messages in a single row.
    if "：" in value and "。" in value:
        return re.sub(r"(^|[;；]\s*)([^:：]+)[:：]\s*([^;；]+)", _translate_evidence, value)
    return _message_pattern().sub(lambda m: catalog[m[0]], value).strip()


def _translate_evidence(match):
    prefix, label, detail = match.groups()
    if any(re.fullmatch(pattern, detail) for pattern, _ in _FORMATTED_MESSAGES):
        return f"{prefix}{label}: {ui_text(detail)}"
    return _message_pattern().sub(lambda m: messages()[m[0]], match[0])


@lru_cache(maxsize=2)
def js_catalog(language: str) -> str:
    if language != "en":
        return "globalThis.sqlPulseUiText = value => value;"
    catalog = json.dumps(messages(), ensure_ascii=True, separators=(",", ":"))
    return "const sqlPulseMessages = " + catalog + ";\n" + (
        LOCALES / "en" / "ui-language.js"
    ).read_text(encoding="utf-8")
