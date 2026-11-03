"""Хранилище прогресса в SQLite: статусы тем, задачи, попытки тестов, позиции в книгах."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from flask import g

SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_status (
    topic_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('new', 'reading', 'read', 'credited')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS problem_done (
    topic_id TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    done_at  TEXT NOT NULL,
    PRIMARY KEY (topic_id, idx)
);
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('topic', 'placement')),
    ref        TEXT NOT NULL,          -- id темы или id предмета
    score      INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    details    TEXT NOT NULL,          -- JSON: ответы и итоги по уровням
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_ref ON attempts (kind, ref);
CREATE TABLE IF NOT EXISTS book_position (
    book_id    TEXT PRIMARY KEY,
    page       INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init(path: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)


def get(path: str) -> sqlite3.Connection:
    """Одно соединение на запрос; закрывается в teardown приложения."""
    if "db" not in g:
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
    return g.db


def close(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---------- Статусы тем ----------

def topic_statuses(conn) -> dict[str, str]:
    return {r["topic_id"]: r["status"] for r in conn.execute("SELECT * FROM topic_status")}


def set_topic_status(conn, topic_id: str, status: str) -> None:
    if status == "new":
        conn.execute("DELETE FROM topic_status WHERE topic_id = ?", (topic_id,))
    else:
        conn.execute(
            "INSERT INTO topic_status VALUES (?, ?, ?) "
            "ON CONFLICT (topic_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
            (topic_id, status, now()),
        )
    conn.commit()


def credit_topics(conn, topic_ids: list[str]) -> None:
    """Зачесть темы по итогам проверки уровня (в том числе прочитанные, но без сданного теста)."""
    conn.executemany(
        "INSERT INTO topic_status VALUES (?, 'credited', ?) "
        "ON CONFLICT (topic_id) DO UPDATE SET status = 'credited', updated_at = excluded.updated_at "
        "WHERE topic_status.status <> 'credited'",
        [(tid, now()) for tid in topic_ids],
    )
    conn.commit()


# ---------- Задачи ----------

def problems_done(conn) -> dict[str, set[int]]:
    done: dict[str, set[int]] = {}
    for r in conn.execute("SELECT topic_id, idx FROM problem_done"):
        done.setdefault(r["topic_id"], set()).add(r["idx"])
    return done


def toggle_problem(conn, topic_id: str, idx: int) -> None:
    deleted = conn.execute(
        "DELETE FROM problem_done WHERE topic_id = ? AND idx = ?", (topic_id, idx)
    ).rowcount
    if not deleted:
        conn.execute("INSERT INTO problem_done VALUES (?, ?, ?)", (topic_id, idx, now()))
    conn.commit()


# ---------- Попытки тестов ----------

def save_attempt(conn, kind: str, ref: str, score: int, total: int, details: dict) -> int:
    cur = conn.execute(
        "INSERT INTO attempts (kind, ref, score, total, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, ref, score, total, json.dumps(details, ensure_ascii=False), now()),
    )
    conn.commit()
    return cur.lastrowid


def get_attempt(conn, attempt_id: int):
    row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        return None
    attempt = dict(row)
    attempt["details"] = json.loads(attempt["details"])
    return attempt


def best_topic_scores(conn) -> dict[str, float]:
    """Лучший результат по каждой теме, в долях от 1."""
    rows = conn.execute(
        "SELECT ref, MAX(CAST(score AS REAL) / total) AS best FROM attempts "
        "WHERE kind = 'topic' AND total > 0 GROUP BY ref"
    )
    return {r["ref"]: r["best"] for r in rows}


def last_placements(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM attempts WHERE id IN "
        "(SELECT MAX(id) FROM attempts WHERE kind = 'placement' GROUP BY ref)"
    )
    return {r["ref"]: {**dict(r), "details": json.loads(r["details"])} for r in rows}


def recent_attempts(conn, limit: int = 6) -> list[dict]:
    rows = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def attempts_since(conn, days: int) -> int:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute("SELECT COUNT(*) FROM attempts WHERE created_at >= ?", (since,)).fetchone()[0]


def recent_average(conn, limit: int = 10) -> int | None:
    """Средний процент по последним попыткам тестов."""
    rows = conn.execute(
        "SELECT CAST(score AS REAL) / total AS share FROM attempts WHERE total > 0 ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return round(100 * sum(r["share"] for r in rows) / len(rows)) if rows else None


def activity_dates(conn) -> set[str]:
    """Дни (YYYY-MM-DD), в которые было любое учебное действие."""
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) FROM attempts "
        "UNION SELECT substr(updated_at, 1, 10) FROM topic_status "
        "UNION SELECT substr(done_at, 1, 10) FROM problem_done "
        "UNION SELECT substr(updated_at, 1, 10) FROM book_position"
    )
    return {r[0] for r in rows}


# ---------- Позиции в книгах ----------

def save_position(conn, book_id: str, page: int) -> None:
    conn.execute(
        "INSERT INTO book_position VALUES (?, ?, ?) "
        "ON CONFLICT (book_id) DO UPDATE SET page = excluded.page, updated_at = excluded.updated_at",
        (book_id, page, now()),
    )
    conn.commit()


def positions(conn) -> dict[str, int]:
    return {r["book_id"]: r["page"] for r in conn.execute("SELECT * FROM book_position")}


def recent_positions(conn, limit: int = 3) -> list[dict]:
    rows = conn.execute("SELECT * FROM book_position ORDER BY updated_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
