"""Дерево целей: главная цель наверху, ветки и шаги ведут к ней, у каждого шага может быть своё поддерево.

Дерево хранится в SQLite (таблица goal_node в instance/) — это личные данные, в репозиторий они не попадают.
Стартовое наполнение берётся из instance/goals_seed.json, если файл есть; иначе — одна точка «Главная цель».
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from db import now

STATUSES = {"todo": "Не начато", "doing": "В работе", "done": "Готово"}
PALETTE = ["#22C55E", "#EAB308", "#3B82F6", "#EF4444", "#A855F7", "#06B6D4", "#F97316", "#EC4899"]
TITLE_MAX = 120
NOTE_MAX = 2000


@dataclass
class Node:
    id: int
    parent_id: int | None
    title: str
    note: str
    status: str
    due: str | None
    subject_id: str | None
    color: str | None
    position: int
    children: list["Node"] = field(default_factory=list)
    progress: int = 0  # 0–100, считается в build_tree

    @property
    def leaf_count(self) -> int:
        return 1 if not self.children else sum(c.leaf_count for c in self.children)


# ---------- Чтение и подсчёт прогресса ----------

def load_nodes(conn) -> dict[int, Node]:
    rows = conn.execute("SELECT * FROM goal_node ORDER BY parent_id, position, id")
    nodes = {r["id"]: Node(**{k: r[k] for k in ("id", "parent_id", "title", "note", "status", "due",
                                                  "subject_id", "color", "position")}) for r in rows}
    for node in nodes.values():
        if node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
    return nodes


def root_of(nodes: dict[int, Node]) -> Node | None:
    return next((n for n in nodes.values() if n.parent_id is None), None)


def compute_progress(node: Node, subject_percent: dict[str, int]) -> int:
    """Готово — 100%. Привязка к предмету — процент пройденных тем. Иначе — среднее по детям."""
    for child in node.children:
        compute_progress(child, subject_percent)
    if node.status == "done":
        node.progress = 100
    elif node.subject_id in subject_percent:
        node.progress = subject_percent[node.subject_id]
    elif node.children:
        node.progress = round(sum(c.progress for c in node.children) / len(node.children))
    else:
        node.progress = 0
    return node.progress


def path_to(nodes: dict[int, Node], node: Node) -> list[Node]:
    """Цепочка от главной цели до узла — для хлебных крошек."""
    chain = [node]
    while chain[-1].parent_id in nodes:
        chain.append(nodes[chain[-1].parent_id])
    return chain[::-1]


def branch_color(nodes: dict[int, Node], node: Node) -> str:
    """Цвет узла: свой, иначе цвет предка, иначе цвет из палитры по месту ветки у главной цели."""
    for n in reversed(path_to(nodes, node)):
        if n.color:
            return n.color
    chain = path_to(nodes, node)
    if len(chain) < 2:
        return PALETTE[0]
    top_branch = chain[1]
    return PALETTE[nodes[top_branch.parent_id].children.index(top_branch) % len(PALETTE)]


# ---------- Изменение ----------

def ensure_root(conn, seed_path: Path | None) -> None:
    """Пустое дерево: заполнить из seed-файла или создать одну главную цель."""
    if conn.execute("SELECT 1 FROM goal_node LIMIT 1").fetchone():
        return
    seed = {"title": "Главная цель", "note": "Нажми «Изменить», чтобы назвать свою цель."}
    if seed_path and seed_path.exists():
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
        except ValueError as e:  # битый seed не должен ронять страницу: начнём с одной точки
            print(f"Не удалось прочитать {seed_path.name}: {e}")
    _insert_tree(conn, seed, None, 0)
    conn.commit()


def _insert_tree(conn, raw: dict, parent_id: int | None, position: int) -> None:
    cur = conn.execute(
        "INSERT INTO goal_node (parent_id, title, note, status, due, subject_id, color, position, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (parent_id, raw["title"][:TITLE_MAX], raw.get("note", "")[:NOTE_MAX], raw.get("status", "todo"),
         raw.get("due"), raw.get("subject"), raw.get("color"), position, now()),
    )
    for i, child in enumerate(raw.get("children", [])):
        _insert_tree(conn, child, cur.lastrowid, i)


def add_child(conn, parent_id: int, title: str) -> int:
    position = conn.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM goal_node WHERE parent_id = ?",
                            (parent_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO goal_node (parent_id, title, note, status, position, updated_at) VALUES (?, ?, '', 'todo', ?, ?)",
        (parent_id, title[:TITLE_MAX], position, now()),
    )
    conn.commit()
    return cur.lastrowid


def update_node(conn, node_id: int, *, title: str, note: str, status: str, due: str | None,
                subject_id: str | None, color: str | None) -> None:
    conn.execute(
        "UPDATE goal_node SET title = ?, note = ?, status = ?, due = ?, subject_id = ?, color = ?, updated_at = ? "
        "WHERE id = ?",
        (title[:TITLE_MAX], note[:NOTE_MAX], status, due, subject_id, color, now(), node_id),
    )
    conn.commit()


def set_status(conn, node_id: int, status: str) -> None:
    conn.execute("UPDATE goal_node SET status = ?, updated_at = ? WHERE id = ?", (status, now(), node_id))
    conn.commit()


def move(conn, node: Node, siblings: list[Node], step: int) -> None:
    """Сдвинуть узел среди братьев на step (−1 — раньше, +1 — позже); позиции перенумеровываются."""
    order = [s.id for s in siblings]
    i = order.index(node.id)
    j = i + step
    if not 0 <= j < len(order):
        return
    order[i], order[j] = order[j], order[i]
    conn.executemany("UPDATE goal_node SET position = ? WHERE id = ?", [(p, nid) for p, nid in enumerate(order)])
    conn.commit()


def delete(conn, node: Node) -> None:
    """Удалить узел вместе со всем поддеревом."""
    ids, stack = [], [node]
    while stack:
        n = stack.pop()
        ids.append(n.id)
        stack.extend(n.children)
    conn.executemany("DELETE FROM goal_node WHERE id = ?", [(i,) for i in ids])
    conn.commit()
