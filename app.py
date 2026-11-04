"""Личный учебный сайт: каталог книг, план по уровням, прогресс, тесты и проверка уровня.

Запуск: python app.py (настройки — в .env, см. .env.example).
"""
import hmac
import logging
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import (Blueprint, Flask, Response, abort, current_app, flash, g, redirect,
                   render_template, request, send_file, session, url_for)

import db
import goals
from bank import LANGS, SIZES, QuestionBank, mastered_ids, pick
from content import (ContentError, Curriculum, Library, Subject, Topic, check_answer,
                     correct_answer_text, given_answer_text, prepare_questions)
from reader import PdfRenderer

BASE_DIR = Path(__file__).parent
INSTANCE_DIR = BASE_DIR / "instance"
DEFAULT_BOOKS_DIR = r"C:\учеба\книги"
COVER_WIDTH = 400
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 60
STATUS_LABELS = {"new": "Не начата", "reading": "Читаю", "read": "Прочитана", "credited": "Зачтена"}

bp = Blueprint("site", __name__)


# ---------- Создание приложения ----------

def create_app() -> Flask:
    load_dotenv(BASE_DIR / ".env")
    password = os.environ.get("SITE_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("Задай в .env пароль SITE_PASSWORD длиной от 8 символов (пример — .env.example).")
    try:
        curriculum = Curriculum()
        question_bank = QuestionBank([s.id for s in curriculum.subjects], curriculum.levels)
    except ContentError as e:
        raise SystemExit(f"Ошибка в файлах content/: {e}")

    INSTANCE_DIR.mkdir(exist_ok=True)
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_load_secret_key(),
        SITE_PASSWORD=password,
        DB_PATH=os.environ.get("DB_PATH") or str(INSTANCE_DIR / "progress.sqlite3"),
        # личное стартовое наполнение дерева целей — только локально, в git не попадает
        GOALS_SEED=Path(os.environ.get("GOALS_SEED") or INSTANCE_DIR / "goals_seed.json"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_SAMESITE="Lax",
    )
    books_dir = Path(os.environ.get("BOOKS_DIR", DEFAULT_BOOKS_DIR))
    if not books_dir.is_dir():
        print(f"Внимание: папка с книгами не найдена: {books_dir}")
    app.extensions["study"] = {
        "curriculum": curriculum,
        "library": Library(books_dir, curriculum.books),
        "renderer": PdfRenderer(),
        "bank": question_bank,
    }
    db.init(app.config["DB_PATH"])
    app.teardown_appcontext(db.close)
    app.register_blueprint(bp)
    app.jinja_env.globals.update(STATUS_LABELS=STATUS_LABELS, levels=curriculum.levels,
                                 pass_percent=curriculum.pass_percent, book_accent=curriculum.accent_for,
                                 # версия стилей в URL: браузер не подмешает старый CSS после обновления
                                 css_version=int((BASE_DIR / "static" / "style.css").stat().st_mtime))
    app.jinja_env.filters["plural"] = plural_ru
    return app


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """plural_ru(3, 'тема', 'темы', 'тем') → 'темы'."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _load_secret_key() -> str:
    """SECRET_KEY из окружения или сгенерированный один раз и сохранённый в instance/."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    key_file = INSTANCE_DIR / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def cur() -> Curriculum:
    return current_app.extensions["study"]["curriculum"]


def lib() -> Library:
    return current_app.extensions["study"]["library"]


def conn():
    return db.get(current_app.config["DB_PATH"])


# ---------- Вход, CSRF ----------

_login_guard = {"fails": 0, "locked_until": 0.0}
_login_lock = threading.Lock()


@bp.before_app_request
def require_login_and_csrf():
    if request.endpoint in ("site.login", "static"):
        pass
    elif not session.get("auth"):
        return redirect(url_for("site.login", next=request.full_path))
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Форма устарела — обнови страницу и попробуй снова.")


@bp.app_context_processor
def inject_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"]}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with _login_lock:
            wait = _login_guard["locked_until"] - time.time()
            if wait > 0:
                flash(f"Слишком много попыток. Подожди {int(wait) + 1} с.")
                return render_template("login.html"), 429
            entered = request.form.get("password", "").encode("utf-8")
            if hmac.compare_digest(entered, current_app.config["SITE_PASSWORD"].encode("utf-8")):
                _login_guard["fails"] = 0
                session.clear()
                session.permanent = True
                session["auth"] = True
                return redirect(_safe_next(request.args.get("next")))
            _login_guard["fails"] += 1
            if _login_guard["fails"] >= LOGIN_MAX_FAILS:
                _login_guard.update(fails=0, locked_until=time.time() + LOGIN_LOCK_SECONDS)
        flash("Неверный пароль.")
    return render_template("login.html")


def _safe_next(target: str | None) -> str:
    """Пускаем только на внутренние пути, чтобы ?next= нельзя было увести на чужой сайт."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("site.dashboard")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("site.login"))


# ---------- Прогресс ----------

@dataclass
class TopicState:
    status: str
    best: float | None
    passed: bool
    complete: bool
    problems_done: set


class Progress:
    """Снимок прогресса за один запрос: все нужные данные читаются из БД один раз."""

    def __init__(self, c):
        self.statuses = db.topic_statuses(c)
        self.best = db.best_topic_scores(c)
        self.done = db.problems_done(c)
        self.placements = db.last_placements(c)
        self.pass_share = cur().pass_percent / 100

    def state(self, topic: Topic) -> TopicState:
        status = self.statuses.get(topic.id, "new")
        best = self.best.get(topic.id)
        passed = best is not None and best >= self.pass_share
        complete = status == "credited" or (status == "read" and (passed or not topic.questions))
        return TopicState(status, best, passed, complete, self.done.get(topic.id, set()))

    def level_stats(self, subject: Subject) -> list[dict]:
        stats = []
        for level, topics in subject.topics_by_level().items():
            done = sum(self.state(t).complete for t in topics)
            stats.append({"level": level, "done": done, "total": len(topics)})
        return stats

    def summary(self, subject: Subject) -> dict:
        ordered = sorted(subject.topics, key=lambda t: t.level)
        complete = [t for t in ordered if self.state(t).complete]
        next_topic = next((t for t in ordered if not self.state(t).complete), None)
        return {
            "subject": subject,
            "done": len(complete),
            "total": len(ordered),
            "percent": round(100 * len(complete) / len(ordered)) if ordered else 0,
            "next": next_topic,
            "placement": self.placements.get(subject.id),
        }


def get_progress() -> Progress:
    """Один снимок прогресса на запрос: им пользуются и страница, и боковая навигация."""
    if "progress" not in g:
        g.progress = Progress(conn())
    return g.progress


def recent_reading(limit: int = 3) -> list[dict]:
    reading = []
    for pos in db.recent_positions(conn(), limit):
        book = lib().by_id.get(pos["book_id"])
        if book:
            reading.append({"book": book, "page": pos["page"], "updated_at": pos["updated_at"]})
    return reading


THEMES = ("dark", "light")


@bp.app_context_processor
def inject_theme():
    """Тема из cookie (ставит переключатель на странице); по умолчанию тёмная."""
    theme = request.cookies.get("theme")
    qlang = request.cookies.get("qlang")  # язык вопросов из банка, переключатель RU/EN
    return {"theme": theme if theme in THEMES else "dark", "qlang": qlang if qlang in LANGS else "ru"}


@bp.app_context_processor
def inject_nav():
    """Данные для боковой панели и нижней навигации — только для вошедшего пользователя."""
    if not session.get("auth") or request.endpoint in ("site.login", "static", None):
        return {}
    progress = get_progress()
    last = recent_reading(1)
    return {
        "nav_subjects": [progress.summary(s) for s in cur().subjects],
        "nav_last_book": last[0] if last else None,
    }


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _greeting(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "Доброе утро"
    if 12 <= now.hour < 18:
        return "Добрый день"
    if 18 <= now.hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def _streak(days: set[str], today: date) -> int:
    """Сколько дней подряд была учёба; сегодняшний пропуск серию ещё не обрывает."""
    day = today if today.isoformat() in days else today - timedelta(days=1)
    count = 0
    while day.isoformat() in days:
        count += 1
        day -= timedelta(days=1)
    return count


# ---------- Главная, предметы, темы ----------

@bp.route("/")
def dashboard():
    c = conn()
    progress = get_progress()
    summaries = [progress.summary(s) for s in cur().subjects]
    done = sum(s["done"] for s in summaries)
    total = sum(s["total"] for s in summaries)
    now = datetime.now()
    return render_template(
        "dashboard.html", summaries=summaries, reading=recent_reading(),
        attempts=[_describe_attempt(a) for a in db.recent_attempts(c, 5)],
        done=done, total=total, week_attempts=db.attempts_since(c, 7),
        streak=_streak(db.activity_dates(c), now.date()), average=db.recent_average(c),
        greeting=_greeting(now),
        today=f"{WEEKDAYS[now.weekday()].capitalize()}, {now.day} {MONTHS[now.month - 1]}",
        needs_placement=[s for s in summaries if not s["placement"]],
    )


def _describe_attempt(a: dict) -> dict:
    if a["kind"] == "topic":
        topic = cur().topics.get(a["ref"])
        title = topic.title if topic else a["ref"]
    else:
        subject = cur().subject_by_id.get(a["ref"])
        prefix = "Тест: " if a["kind"] == "bank" else "Проверка уровня: "
        title = prefix + (subject.title if subject else a["ref"])
    return {**a, "title": title, "percent": round(100 * a["score"] / a["total"]) if a["total"] else 0}


@bp.route("/subject/<sid>")
def subject(sid):
    subj = cur().subject_by_id.get(sid) or abort(404)
    progress = get_progress()
    return render_template("subject.html", summary=progress.summary(subj),
                           by_level=subj.topics_by_level(), progress=progress,
                           level_stats={s["level"]: s for s in progress.level_stats(subj)})


@bp.route("/topic/<tid>")
def topic(tid):
    t = cur().topics.get(tid) or abort(404)
    subj = cur().subject_by_id[t.subject_id]
    ordered = sorted(subj.topics, key=lambda x: x.level)
    pos = ordered.index(t)
    return render_template(
        "topic.html", topic=t, subject=subj, state=get_progress().state(t),
        reading=[_resolve_item(i) for i in t.read], problems=[_resolve_item(i) for i in t.problems],
        prev_topic=ordered[pos - 1] if pos > 0 else None,
        next_topic=ordered[pos + 1] if pos + 1 < len(ordered) else None,
        number=pos + 1, count=len(ordered),
    )


def _resolve_item(item: dict) -> dict:
    """Пункт «читать»/«решать» → ссылка на страницу книги (или пометка, что файла нет)."""
    resolved = {**item, "url": None, "missing": False, "book_title": None, "book_id": None, "book_obj": None}
    key = item.get("book")
    if not key:
        return resolved
    book = lib().by_key.get(key)
    resolved["book_title"] = book.title if book else cur().books[key]["title"]
    if book is None:
        resolved["missing"] = True
        return resolved
    resolved["book_id"], resolved["book_obj"] = book.id, book
    if book.is_pdf:
        resolved["url"] = url_for("site.read", bid=book.id, page=item.get("page"))
    else:
        resolved["url"] = url_for("site.book_file", bid=book.id)
    return resolved


@bp.route("/topic/<tid>/status", methods=["POST"])
def topic_status(tid):
    if tid not in cur().topics:
        abort(404)
    status = request.form.get("status")
    if status not in STATUS_LABELS:
        abort(400)
    db.set_topic_status(conn(), tid, status)
    return redirect(url_for("site.topic", tid=tid))


@bp.route("/topic/<tid>/problem/<int:idx>", methods=["POST"])
def toggle_problem(tid, idx):
    t = cur().topics.get(tid) or abort(404)
    if not 0 <= idx < len(t.problems):
        abort(404)
    db.toggle_problem(conn(), tid, idx)
    return redirect(url_for("site.topic", tid=tid) + "#problems")


# ---------- Тесты ----------

@bp.route("/test/<tid>", methods=["GET", "POST"])
def topic_test(tid):
    t = cur().topics.get(tid) or abort(404)
    if not t.questions:
        flash("Для этой темы пока нет вопросов.")
        return redirect(url_for("site.topic", tid=tid))
    if request.method == "GET":
        return render_template("test.html", title=t.title, questions=prepare_questions(t.questions),
                               back_url=url_for("site.topic", tid=tid),
                               subject=cur().subject_by_id[t.subject_id])
    score, answers = _grade(t.questions)
    attempt_id = db.save_attempt(conn(), "topic", tid, score, len(answers), {"answers": answers})
    return redirect(url_for("site.result", aid=attempt_id))


@bp.route("/placement/<sid>", methods=["GET", "POST"])
def placement(sid):
    subj = cur().subject_by_id.get(sid) or abort(404)
    if not subj.placement:
        abort(404)
    if request.method == "GET":
        return render_template("test.html", title=f"Проверка уровня: {subj.title}",
                               questions=prepare_questions(subj.placement),
                               back_url=url_for("site.subject", sid=sid), placement=True, subject=subj)
    score, answers = _grade(subj.placement)
    by_level = {}
    for a in answers:
        stats = by_level.setdefault(str(a["level"]), [0, 0])
        stats[0] += a["ok"]
        stats[1] += 1
    details = {"answers": answers, "by_level": by_level, "recommended": _recommend_level(by_level)}
    attempt_id = db.save_attempt(conn(), "placement", sid, score, len(answers), details)
    return redirect(url_for("site.result", aid=attempt_id))


def _grade(questions: list[dict]) -> tuple[int, list[dict]]:
    """Проверяет присланную форму. Учитываются только вопросы, реально показанные в тесте."""
    by_id = {q["id"]: q for q in questions}
    qids = dict.fromkeys(request.form.getlist("qid"))  # без повторов, порядок сохраняется
    shown = [by_id[qid] for qid in qids if qid in by_id]
    if not shown:
        abort(400, "В форме нет вопросов.")
    answers = []
    for q in shown:
        raw = request.form.get(f"a_{q['id']}")
        answers.append({
            "id": q["id"], "q": q["q"], "code": q.get("code"), "level": q.get("level"),
            "ok": check_answer(q, raw), "given": given_answer_text(q, raw),
            "correct": correct_answer_text(q), "explain": q.get("explain", ""),
            "mono": bool(q.get("code_options") or q.get("strict")),  # ответы-код показать моноширинным
        })
    return sum(a["ok"] for a in answers), answers


def _recommend_level(by_level: dict[str, list[int]]) -> int:
    """Первый уровень, где результат ниже проходного; если всё сдано — самый высокий."""
    levels = sorted(int(k) for k in by_level)
    for level in levels:
        ok, total = by_level[str(level)]
        if ok / total * 100 < cur().pass_percent:
            return level
    return levels[-1]


@bp.route("/result/<int:aid>")
def result(aid):
    attempt = db.get_attempt(conn(), aid) or abort(404)
    attempt = {**attempt, **_describe_attempt({k: attempt[k] for k in ("kind", "ref", "score", "total")})}
    back_url = {"topic": lambda: url_for("site.topic", tid=attempt["ref"]),
                "placement": lambda: url_for("site.subject", sid=attempt["ref"]),
                "bank": lambda: url_for("site.tests")}[attempt["kind"]]()
    if attempt["kind"] == "topic" and attempt["ref"] in cur().topics:
        subj = cur().subject_by_id[cur().topics[attempt["ref"]].subject_id]
    else:
        subj = cur().subject_by_id.get(attempt["ref"])
    return render_template("result.html", a=attempt, back_url=back_url, subject=subj,
                           nav_section="tests" if attempt["kind"] == "bank" else None)


@bp.route("/placement/<sid>/apply", methods=["POST"])
def apply_placement(sid):
    subj = cur().subject_by_id.get(sid) or abort(404)
    level = request.form.get("level", type=int)
    if level not in cur().levels:
        abort(400)
    lower = [t.id for t in subj.topics if t.level < level]
    db.credit_topics(conn(), lower)
    flash(f"Зачтено тем: {len(lower)}. Начинай с уровня {level}.")
    return redirect(url_for("site.subject", sid=sid))


# ---------- Тесты из открытых банков ----------

def qbank() -> QuestionBank:
    return current_app.extensions["study"]["bank"]


@bp.route("/tests")
def tests():
    c = conn()
    cards = []
    for subj in cur().subjects:
        questions = qbank().questions(subj.id)
        if not questions:
            continue
        attempts = db.bank_attempts(c, subj.id)
        last = attempts[-1] if attempts else None
        cards.append({
            "subject": subj, "total": len(questions), "levels": qbank().level_counts(subj.id),
            "mastered": len(mastered_ids(attempts) & {q["id"] for q in questions}),
            "attempts": len(attempts), "last": _describe_attempt(last) if last else None,
        })
    return render_template("tests.html", cards=cards, sizes=SIZES, nav_section="tests")


@bp.route("/tests/<sid>", methods=["GET", "POST"])
def bank_test(sid):
    subj = cur().subject_by_id.get(sid) or abort(404)
    pool = qbank().questions(sid)
    if not pool:
        abort(404)
    if request.method == "POST":
        # Форма отправляется на тот же адрес с ?level=&n= — по ним строится кнопка «Ещё тест».
        score, answers = _grade(pool)
        retry = {k: v for k, v in request.args.items() if k in ("level", "n")}
        details = {"answers": answers, "retry": retry}
        attempt_id = db.save_attempt(conn(), "bank", sid, score, len(answers), details)
        return redirect(url_for("site.result", aid=attempt_id))

    level = request.args.get("level", type=int)
    level = level if level in cur().levels else None
    size = request.args.get("n", type=int)
    size = size if size in SIZES else SIZES[0]
    questions = qbank().questions(sid, level)
    if not questions:
        flash("На этом уровне вопросов нет — выбери другой.")
        return redirect(url_for("site.tests"))
    chosen = pick(questions, size, mastered_ids(db.bank_attempts(conn(), sid)))
    title = f"Тест: {subj.title}" + (f" · уровень {level}" if level else "")
    return render_template("test.html", title=title, questions=prepare_questions(chosen),
                           back_url=url_for("site.tests"), subject=subj, bilingual=True,
                           nav_section="tests")


# ---------- Дерево целей ----------

def _goal_tree() -> tuple[dict[int, goals.Node], goals.Node]:
    c = conn()
    goals.ensure_root(c, current_app.config["GOALS_SEED"])
    nodes = goals.load_nodes(c)
    root = goals.root_of(nodes)
    progress = get_progress()
    goals.compute_progress(root, {s.id: progress.summary(s)["percent"] for s in cur().subjects})
    return nodes, root


def _goal_or_404(nodes: dict[int, goals.Node], nid: int) -> goals.Node:
    return nodes.get(nid) or abort(404)


@bp.route("/goals")
def goals_root():
    _, root = _goal_tree()
    return redirect(url_for("site.goal_view", nid=root.id))


@bp.route("/goals/<int:nid>")
def goal_view(nid):
    nodes, root = _goal_tree()
    node = _goal_or_404(nodes, nid)
    # Цвета веток на экране: первая ветка сохраняет цвет узла, остальные идут дальше по палитре.
    base = goals.branch_color(nodes, node)
    start = goals.PALETTE.index(base) if base in goals.PALETTE else 0
    branches = [{"node": child, "color": child.color or goals.PALETTE[(start + i) % len(goals.PALETTE)]}
                for i, child in enumerate(node.children)]
    siblings = nodes[node.parent_id].children if node.parent_id in nodes else []
    return render_template(
        "goals.html", node=node, root=root, branches=branches, color=base,
        crumbs=goals.path_to(nodes, node), siblings=siblings, today=date.today().isoformat(),
        statuses=goals.STATUSES, palette=goals.PALETTE, subjects=cur().subjects, nav_section="goals",
    )


def _form_goal_fields() -> dict:
    title = request.form.get("title", "").strip()
    status = request.form.get("status", "todo")
    due = request.form.get("due", "").strip() or None
    subject_id = request.form.get("subject", "") or None
    color = request.form.get("color", "") or None
    if not title or status not in goals.STATUSES:
        abort(400, "Нужно название и правильный статус.")
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            abort(400, "Срок — дата в формате ГГГГ-ММ-ДД.")
    if subject_id and subject_id not in cur().subject_by_id:
        abort(400, "Неизвестный предмет.")
    if color and color not in goals.PALETTE:
        abort(400, "Неизвестный цвет.")
    return {"title": title, "note": request.form.get("note", "").strip(), "status": status,
            "due": due, "subject_id": subject_id, "color": color}


@bp.route("/goals/<int:nid>/edit", methods=["POST"])
def goal_edit(nid):
    nodes, _ = _goal_tree()
    _goal_or_404(nodes, nid)
    goals.update_node(conn(), nid, **_form_goal_fields())
    return redirect(url_for("site.goal_view", nid=nid))


@bp.route("/goals/<int:nid>/status", methods=["POST"])
def goal_status(nid):
    nodes, _ = _goal_tree()
    node = _goal_or_404(nodes, nid)
    status = request.form.get("status")
    if status not in goals.STATUSES:
        abort(400)
    goals.set_status(conn(), nid, status)
    return redirect(url_for("site.goal_view", nid=node.id))


@bp.route("/goals/<int:nid>/add", methods=["POST"])
def goal_add(nid):
    nodes, _ = _goal_tree()
    _goal_or_404(nodes, nid)
    title = request.form.get("title", "").strip()
    if not title:
        flash("Напиши название шага.")
    else:
        goals.add_child(conn(), nid, title)
    return redirect(url_for("site.goal_view", nid=nid) + "#edit")


@bp.route("/goals/<int:nid>/move", methods=["POST"])
def goal_move(nid):
    nodes, _ = _goal_tree()
    node = _goal_or_404(nodes, nid)
    parent = nodes.get(node.parent_id) or abort(400, "Главную цель двигать некуда.")
    goals.move(conn(), node, parent.children, -1 if request.form.get("dir") == "up" else 1)
    return redirect(url_for("site.goal_view", nid=parent.id) + "#edit")


@bp.route("/goals/<int:nid>/delete", methods=["POST"])
def goal_delete(nid):
    nodes, _ = _goal_tree()
    node = _goal_or_404(nodes, nid)
    parent = nodes.get(node.parent_id) or abort(400, "Главную цель удалить нельзя — её можно переименовать.")
    goals.delete(conn(), node)
    flash(f"Удалено: «{node.title}» и всё, что внутри.")
    return redirect(url_for("site.goal_view", nid=parent.id) + "#edit")


# ---------- Книги ----------

@bp.route("/catalog")
def catalog():
    library = lib()
    library.rescan()  # подхватить новые файлы без перезапуска
    return render_template("catalog.html", folders=library.folders(),
                           positions=db.positions(conn()), books_dir=library.books_dir)


def _book_or_404(bid):
    return lib().by_id.get(bid) or abort(404)


@bp.route("/read/<bid>")
def read(bid):
    book = _book_or_404(bid)
    if not book.is_pdf:
        return redirect(url_for("site.book_file", bid=bid))
    renderer = current_app.extensions["study"]["renderer"]
    try:
        total = renderer.page_count(book.path)
    except Exception as e:  # битый файл не должен ронять сайт
        abort(500, f"Не удалось открыть PDF: {e}")
    page = request.args.get("page", type=int) or db.positions(conn()).get(bid, 1)
    page = max(1, min(total, page))
    db.save_position(conn(), bid, page)
    return render_template("read.html", book=book, page=page, total=total)


@bp.route("/page/<bid>/<int:page>.jpg")
def page_image(bid, page):
    book = _book_or_404(bid)
    if not book.is_pdf:
        abort(404)
    renderer = current_app.extensions["study"]["renderer"]
    try:
        image = renderer.render(book.path, page, request.args.get("w", 1000, type=int))
    except IndexError:
        abort(404)
    response = Response(image, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


@bp.route("/cover/<bid>.jpg")
def cover_image(bid):
    """Обложка книги с кешем на диске; 404 — если в PDF нет нормальной обложки."""
    book = _book_or_404(bid)
    if not book.is_pdf:
        abort(404)
    cache = INSTANCE_DIR / "covers" / f"{bid}.jpg"
    no_cover = cache.with_suffix(".none")
    no_cover_response = Response(status=404, headers={"Cache-Control": "private, max-age=604800"})
    if no_cover.exists():
        return no_cover_response  # без HTML-страницы ошибки: это запрос картинки
    if not cache.exists():
        image = current_app.extensions["study"]["renderer"].render_cover(book.path, COVER_WIDTH)
        cache.parent.mkdir(exist_ok=True)
        if image is None:
            no_cover.touch()
            return no_cover_response
        cache.write_bytes(image)
    response = send_file(cache, mimetype="image/jpeg", max_age=604800)
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


@bp.route("/file/<bid>")
def book_file(bid):
    book = _book_or_404(bid)
    return send_file(book.path, conditional=True, download_name=book.path.name,
                     as_attachment=not book.is_pdf)


@bp.app_errorhandler(400)
@bp.app_errorhandler(404)
@bp.app_errorhandler(500)
def error_page(e):
    return render_template("error.html", error=e), e.code


# ---------- Запуск ----------

def _port_is_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


def _log_to_file_if_hidden() -> None:
    """Без консоли (pythonw.exe, автозапуск) сообщения и ошибки пишутся в instance/server.log."""
    if sys.stdout is not None:
        return
    INSTANCE_DIR.mkdir(exist_ok=True)
    log = open(INSTANCE_DIR / "server.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    logging.basicConfig(stream=log, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} запуск в фоне ===")


if __name__ == "__main__":
    from waitress import serve

    _log_to_file_if_hidden()
    load_dotenv(BASE_DIR / ".env")  # HOST/PORT из .env нужны до создания приложения
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if _port_is_busy(host, port):
        raise SystemExit(
            f"Порт {port} уже занят — скорее всего, сайт уже запущен в другом окне.\n"
            f"Открой http://{host}:{port} или закрой старое окно сервера и запусти снова."
        )
    application = create_app()
    print(f"Учебный сайт: http://{host}:{port}  (Ctrl+C — остановить)", flush=True)
    serve(application, host=host, port=port, threads=8)
