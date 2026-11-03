"""Банк вопросов из открытых источников (вкладка «Тесты»).

Вопросы лежат в content/bank/<предмет>.json на двух языках: оригинал (en) и перевод (ru).
Откуда они взяты и под какими лицензиями — content/bank/SOURCES.md.

При загрузке вопросы приводятся к формату вопросов учебного плана: текст и варианты —
словари {"ru": ..., "en": ...}. Поэтому их проверяют и показывают те же функции и шаблоны.
"""
import json
import random
from pathlib import Path

from content import CONTENT_DIR, ContentError

BANK_DIR = CONTENT_DIR / "bank"
LANGS = ("ru", "en")
SIZES = (10, 20, 30)
CODE_SOURCES = {"li", "crux"}  # источники с вопросами по коду


class QuestionBank:
    def __init__(self, subject_ids: list[str], levels: dict[int, str], bank_dir: Path = BANK_DIR):
        self.by_subject: dict[str, list[dict]] = {}
        for sid in subject_ids:
            path = bank_dir / f"{sid}.json"
            if path.exists():
                self.by_subject[sid] = [_normalize(q, levels, path.name) for q in _read(path)["questions"]]
        ids = [q["id"] for qs in self.by_subject.values() for q in qs]
        if len(ids) != len(set(ids)):
            raise ContentError("В банке вопросов повторяются id")

    def questions(self, sid: str, level: int | None = None) -> list[dict]:
        return [q for q in self.by_subject.get(sid, []) if level is None or q["level"] == level]

    def level_counts(self, sid: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        for q in self.by_subject.get(sid, []):
            counts[q["level"]] = counts.get(q["level"], 0) + 1
        return dict(sorted(counts.items()))


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ContentError(f"bank/{path.name}: ошибка JSON в строке {e.lineno}: {e.msg}") from None


def _normalize(raw: dict, levels: dict[int, str], file_name: str) -> dict:
    where = f"bank/{file_name}, вопрос {raw.get('id', '?')}"
    if raw.get("level") not in levels or set(raw.get("q", {})) != set(LANGS):
        raise ContentError(f"{where}: нужен level и текст на ru и en")
    if not {"id", "src", "type", "answer"} <= raw.keys():
        raise ContentError(f"{where}: нет id, src, type или answer")
    q = {"id": raw["id"], "src": raw["src"], "level": raw["level"], "q": raw["q"],
         "answer": raw["answer"], "explain": raw.get("explain", ""), "code": raw.get("code")}
    if raw["type"] == "choice":
        ru, en = raw["options"]["ru"], raw["options"]["en"]
        if len(ru) != len(en) or not 0 <= raw["answer"] < len(en):
            raise ContentError(f"{where}: варианты ru/en не совпадают или ответ вне списка")
        # Непереведённые варианты из этих источников — код или SQL: показываем моноширинным.
        q.update(type="choice", options=[{"ru": r, "en": e} for r, e in zip(ru, en)],
                 code_options=raw["src"] in CODE_SOURCES and ru == en)
    elif raw["type"] == "text":
        # Ответ — вывод программы: сравнивается с учётом регистра, без пробелов (см. content.check_answer).
        q.update(type="text", strict=True)
    else:
        raise ContentError(f"{where}: неизвестный тип {raw['type']}")
    return q


def pick(questions: list[dict], size: int, mastered: set[str]) -> list[dict]:
    """Сначала новые вопросы и те, где была ошибка; освоенные — только чтобы добрать до size."""
    fresh = [q for q in questions if q["id"] not in mastered]
    known = [q for q in questions if q["id"] in mastered]
    chosen = random.sample(fresh, min(size, len(fresh)))
    chosen += random.sample(known, min(size - len(chosen), len(known)))
    return chosen


def mastered_ids(attempts: list[dict]) -> set[str]:
    """Вопросы, на которые в последний раз ответили верно. attempts — по возрастанию id."""
    last: dict[str, bool] = {}
    for attempt in attempts:
        for answer in attempt["details"]["answers"]:
            last[answer["id"]] = answer["ok"]
    return {qid for qid, ok in last.items() if ok}
