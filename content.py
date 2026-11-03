"""Учебный план, банк вопросов и каталог книг.

Весь учебный контент лежит в папке content/ в виде JSON, чтобы его можно было
дополнять без правки кода. При загрузке контент проверяется: опечатка в id книги
или темы должна ломать запуск с понятной ошибкой, а не всплывать посреди теста.
"""
import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

CONTENT_DIR = Path(__file__).parent / "content"
BOOK_EXTENSIONS = {".pdf", ".djvu"}
QUESTION_TYPES = {"choice", "number", "text"}


class ContentError(Exception):
    """Ошибка в JSON-файлах учебного плана или вопросов."""


@dataclass
class BookFile:
    id: str
    path: Path
    folder: str
    key: str | None = None  # ключ из curriculum.json, если книга там описана
    meta: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.path.stem

    @property
    def author(self) -> str:
        """«Зорич — Математический анализ» → «Зорич» (для обложки)."""
        head, sep, _ = self.title.replace(" - ", " — ").partition(" — ")
        return head if sep else ""

    @property
    def short_title(self) -> str:
        _, sep, tail = self.title.replace(" - ", " — ").partition(" — ")
        return tail if sep else self.title

    @property
    def is_pdf(self) -> bool:
        return self.path.suffix.lower() == ".pdf"

    @property
    def size_mb(self) -> float:
        return self.path.stat().st_size / 1_048_576


class Library:
    """Файлы книг из папки BOOKS_DIR, сопоставленные с описаниями из curriculum.json."""

    def __init__(self, books_dir: Path, book_meta: dict):
        self.books_dir = books_dir
        self.book_meta = book_meta
        self.by_id: dict[str, BookFile] = {}
        self.by_key: dict[str, BookFile] = {}
        self.rescan()

    def rescan(self) -> None:
        by_id, by_key = {}, {}
        if self.books_dir.is_dir():
            for path in sorted(self.books_dir.rglob("*")):
                if path.suffix.lower() not in BOOK_EXTENSIONS or not path.is_file():
                    continue
                rel = path.relative_to(self.books_dir).as_posix()
                book = BookFile(
                    id=hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12],
                    path=path,
                    folder=path.parent.name if path.parent != self.books_dir else "",
                )
                for key, meta in self.book_meta.items():
                    if meta["match"] in path.name:
                        book.key, book.meta = key, meta
                        by_key[key] = book
                        break
                by_id[book.id] = book
        self.by_id, self.by_key = by_id, by_key

    def folders(self) -> dict[str, list[BookFile]]:
        grouped: dict[str, list[BookFile]] = {}
        for book in self.by_id.values():
            grouped.setdefault(book.folder, []).append(book)
        return dict(sorted(grouped.items()))


@dataclass
class Topic:
    id: str
    subject_id: str
    level: int
    title: str
    goal: str
    read: list[dict]
    problems: list[dict]
    note: str = ""
    questions: list[dict] = field(default_factory=list)


@dataclass
class Subject:
    id: str
    title: str
    topics: list[Topic]
    placement: list[dict]
    accent: str = "#6366F1"  # цвет предмета в интерфейсе
    glyph: str = "•"         # короткий знак предмета для иконки
    short: str = ""          # короткое название для навигации

    def topics_by_level(self) -> dict[int, list[Topic]]:
        grouped: dict[int, list[Topic]] = {}
        for topic in sorted(self.topics, key=lambda t: t.level):
            grouped.setdefault(topic.level, []).append(topic)
        return grouped


class Curriculum:
    def __init__(self, content_dir: Path = CONTENT_DIR):
        raw = _read_json(content_dir / "curriculum.json")
        self.pass_percent: int = raw["pass_percent"]
        self.levels: dict[int, str] = {int(k): v for k, v in raw["levels"].items()}
        self.books: dict = raw["books"]
        self.subjects: list[Subject] = []
        self.topics: dict[str, Topic] = {}
        for s in raw["subjects"]:
            bank = _read_json(content_dir / "questions" / f"{s['id']}.json")
            self.subjects.append(self._build_subject(s, bank))
        self.subject_by_id = {s.id: s for s in self.subjects}
        # Цвет книги в интерфейсе — цвет предмета, где она встречается первой.
        self.book_accent: dict[str, str] = {}
        for s in self.subjects:
            for t in s.topics:
                for item in t.read + t.problems:
                    if "book" in item:
                        self.book_accent.setdefault(item["book"], s.accent)

    def accent_for(self, book) -> str:
        return self.book_accent.get(getattr(book, "key", None), "#5B5BD6")

    def _build_subject(self, raw: dict, bank: dict) -> Subject:
        topics = []
        for t in raw["topics"]:
            if t["id"] in self.topics:
                raise ContentError(f"Тема {t['id']} встречается дважды")
            if t["level"] not in self.levels:
                raise ContentError(f"Тема {t['id']}: неизвестный уровень {t['level']}")
            for item in t["read"] + t["problems"]:
                if "book" in item and item["book"] not in self.books:
                    raise ContentError(f"Тема {t['id']}: неизвестная книга {item['book']}")
            topic = Topic(
                id=t["id"], subject_id=raw["id"], level=t["level"], title=t["title"],
                goal=t.get("goal", ""), read=t["read"], problems=t["problems"],
                note=t.get("note", ""), questions=bank["topics"].get(t["id"], []),
            )
            for q in topic.questions:
                _check_question(q, topic.id)
            self.topics[topic.id] = topic
            topics.append(topic)

        unknown = set(bank["topics"]) - {t.id for t in topics}
        if unknown:
            raise ContentError(f"Вопросы для несуществующих тем: {', '.join(sorted(unknown))}")
        for q in bank.get("placement", []):
            _check_question(q, f"проверка уровня {raw['id']}")
            if q.get("level") not in self.levels:
                raise ContentError(f"Вопрос {q['id']}: у вопросов проверки уровня нужен level")
        return Subject(id=raw["id"], title=raw["title"], topics=topics,
                       placement=bank.get("placement", []),
                       accent=raw.get("accent", "#6366F1"), glyph=raw.get("glyph", "•"),
                       short=raw.get("short") or raw["title"])


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContentError(f"Нет файла {path}") from None
    except json.JSONDecodeError as e:
        raise ContentError(f"{path.name}: ошибка JSON в строке {e.lineno}: {e.msg}") from None


def _check_question(q: dict, where: str) -> None:
    if q.get("type") not in QUESTION_TYPES or "id" not in q or "q" not in q or "answer" not in q:
        raise ContentError(f"{where}: вопрос {q.get('id', '?')} заполнен не полностью")
    if q["type"] == "choice" and not 0 <= q["answer"] < len(q.get("options", [])):
        raise ContentError(f"{where}: в вопросе {q['id']} номер ответа вне списка вариантов")


# ---------- Тесты ----------

def prepare_questions(questions: list[dict]) -> list[dict]:
    """Перемешивает вопросы и варианты ответов для показа.

    Для choice-вопросов возвращается список пар (исходный индекс, текст),
    в форму уходит исходный индекс — так проверка не зависит от перемешивания.
    """
    prepared = []
    for q in random.sample(questions, len(questions)):
        shown = dict(q)
        if q["type"] == "choice":
            order = list(enumerate(q["options"]))
            random.shuffle(order)
            shown["shuffled"] = order
        prepared.append(shown)
    return prepared


def check_answer(q: dict, raw: str | None) -> bool:
    raw = (raw or "").strip()
    if not raw:
        return False
    if q["type"] == "choice":
        return raw.isdigit() and int(raw) == q["answer"]
    if q["type"] == "number":
        try:
            value = float(raw.replace(",", ".").replace("−", "-").replace(" ", ""))
        except ValueError:
            return False
        return abs(value - q["answer"]) <= q.get("tol", 1e-9)
    accepted = q["answer"] if isinstance(q["answer"], list) else [q["answer"]]
    return _normalize(raw) in {_normalize(a) for a in accepted}


def correct_answer_text(q: dict) -> str:
    if q["type"] == "choice":
        return q["options"][q["answer"]]
    if q["type"] == "number":
        return f"{q['answer']:g}" + (f" (±{q['tol']:g})" if "tol" in q else "")
    return q["answer"][0] if isinstance(q["answer"], list) else q["answer"]


def given_answer_text(q: dict, raw: str | None) -> str:
    raw = (raw or "").strip()
    if q["type"] == "choice" and raw.isdigit() and int(raw) < len(q["options"]):
        return q["options"][int(raw)]
    return raw or "—"


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())
