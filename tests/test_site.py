"""Сквозные проверки сайта. Запуск: .venv\\Scripts\\python.exe -m unittest discover tests -v"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp()
os.environ["SITE_PASSWORD"] = "unit-test-password"
os.environ["DB_PATH"] = os.path.join(TMP, "test.sqlite3")
os.environ["SECRET_KEY"] = "unit-test-secret"

from app import create_app  # noqa: E402
from content import check_answer, correct_answer_text  # noqa: E402


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class SiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.cur = cls.app.extensions["study"]["curriculum"]
        cls.lib = cls.app.extensions["study"]["library"]

    def setUp(self):
        self.client = self.app.test_client()

    def login(self):
        html = self.client.get("/login").get_data(as_text=True)
        r = self.client.post("/login", data={"csrf": csrf_of(html), "password": "unit-test-password"})
        self.assertEqual(r.status_code, 302)
        return csrf_of(self.client.get("/").get_data(as_text=True))

    # --- контент ---

    def test_all_books_from_plan_exist(self):
        missing = [k for k in self.cur.books if k not in self.lib.by_key]
        self.assertEqual(missing, [], f"Книги из плана не найдены в папке: {missing}")

    def test_every_topic_has_questions_and_pages_in_range(self):
        renderer = self.app.extensions["study"]["renderer"]
        for topic in self.cur.topics.values():
            self.assertTrue(topic.questions, f"Нет вопросов для {topic.id}")
            for item in topic.read + topic.problems:
                book = self.lib.by_key.get(item.get("book"))
                if book and book.is_pdf and item.get("page"):
                    total = renderer.page_count(book.path)
                    self.assertLessEqual(item.get("to", item["page"]), total, f"{topic.id}: {item}")

    def test_answer_checking(self):
        num = {"type": "number", "answer": 0.5, "tol": 0.01}
        self.assertTrue(check_answer(num, "0,5"))
        self.assertTrue(check_answer(num, " 0.505 "))
        self.assertFalse(check_answer(num, "0.6"))
        self.assertFalse(check_answer(num, "abc"))
        self.assertTrue(check_answer({"type": "number", "answer": -4}, "−4"))
        self.assertTrue(check_answer({"type": "text", "answer": ["\\dt"]}, " \\DT "))
        choice = {"type": "choice", "answer": 2, "options": ["a", "b", "c"]}
        self.assertTrue(check_answer(choice, "2"))
        self.assertFalse(check_answer(choice, ""))
        self.assertEqual(correct_answer_text(choice), "c")

    # --- доступ ---

    def test_pages_require_login(self):
        for url in ["/", "/catalog", "/subject/prog", "/topic/c1"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])

    def test_wrong_password_and_missing_csrf(self):
        html = self.client.get("/login").get_data(as_text=True)
        r = self.client.post("/login", data={"csrf": csrf_of(html), "password": "nope"})
        self.assertIn("Неверный пароль", r.get_data(as_text=True))
        r = self.client.post("/login", data={"password": "unit-test-password"})
        self.assertEqual(r.status_code, 400)

    def test_open_redirect_blocked(self):
        html = self.client.get("/login").get_data(as_text=True)
        r = self.client.post("/login?next=//evil.example", data={"csrf": csrf_of(html), "password": "unit-test-password"})
        self.assertEqual(r.headers["Location"], "/")

    # --- страницы и прогресс ---

    def test_all_pages_render(self):
        self.login()
        for url in ["/", "/catalog"] + [f"/subject/{s.id}" for s in self.cur.subjects] \
                + [f"/topic/{t}" for t in self.cur.topics] + [f"/test/{t}" for t in self.cur.topics] \
                + [f"/placement/{s.id}" for s in self.cur.subjects]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/topic/nope").status_code, 404)

    def test_topic_flow(self):
        csrf = self.login()
        topic = self.cur.topics["c1"]
        form = {"csrf": csrf, "qid": [q["id"] for q in topic.questions]}
        for q in topic.questions:
            form[f"a_{q['id']}"] = str(q["answer"])
        r = self.client.post("/test/c1", data=form)
        self.assertEqual(r.status_code, 302)
        result = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"{len(topic.questions)} / {len(topic.questions)}", result)

        self.client.post("/topic/c1/status", data={"csrf": csrf, "status": "read"})
        self.client.post("/topic/c1/problem/0", data={"csrf": csrf})
        page = self.client.get("/topic/c1").get_data(as_text=True)
        self.assertIn("Пройдена", page)
        self.assertIn("1 из 2", page)

    def test_placement_recommends_first_weak_level(self):
        csrf = self.login()
        subj = self.cur.subject_by_id["calc"]
        form = {"csrf": csrf, "qid": [q["id"] for q in subj.placement]}
        for q in subj.placement:  # уровень 1 верно, остальные пусто
            if q["level"] == 1:
                form[f"a_{q['id']}"] = str(q["answer"])
        r = self.client.post("/placement/calc", data=form)
        result = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Старт с уровня 2", result)

        self.client.post("/placement/calc/apply", data={"csrf": csrf, "level": "2"})
        page = self.client.get("/subject/calc").get_data(as_text=True)
        level1 = sum(1 for t in subj.topics if t.level == 1)
        self.assertIn(f"{level1} из {len(subj.topics)} тем пройдено", page)

    def test_theme_dark_by_default_and_cookie_switch(self):
        self.assertIn('data-theme="dark"', self.client.get("/login").get_data(as_text=True))
        self.client.set_cookie("theme", "light")
        self.assertIn('data-theme="light"', self.client.get("/login").get_data(as_text=True))
        self.client.set_cookie("theme", "<script>")  # мусор в cookie → тёмная по умолчанию
        self.assertIn('data-theme="dark"', self.client.get("/login").get_data(as_text=True))

    def test_plural(self):
        from app import plural_ru
        forms = ("день", "дня", "дней")
        self.assertEqual([plural_ru(n, *forms) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)],
                         ["день", "дня", "дней", "дней", "дней", "день", "дня", "дней", "дней"])

    def test_dashboard_streak(self):
        from datetime import date
        from app import _streak
        today = date(2026, 11, 3)
        self.assertEqual(_streak({"2026-11-03", "2026-11-02", "2026-10-31"}, today), 2)
        self.assertEqual(_streak({"2026-11-02", "2026-11-01"}, today), 2)  # сегодня ещё не занимался
        self.assertEqual(_streak({"2026-10-30"}, today), 0)

    def test_reader_renders_page(self):
        self.login()
        book = self.lib.by_key["kr"]
        r = self.client.get(f"/read/{book.id}?page=9")
        self.assertEqual(r.status_code, 200)
        img = self.client.get(f"/page/{book.id}/9.jpg?w=600")
        self.assertEqual(img.mimetype, "image/jpeg")
        self.assertTrue(img.data.startswith(b"\xff\xd8"))
        self.assertEqual(self.client.get(f"/page/{book.id}/99999.jpg").status_code, 404)
        r = self.client.get(f"/read/{book.id}?page=99999")  # номер страницы ограничивается
        self.assertIn("/ 238", r.get_data(as_text=True))

    def test_covers(self):
        self.login()
        with_cover = self.lib.by_key["abbott"]       # у Abbott настоящая обложка
        blank = self.lib.by_key["zorich1"]          # у Зорича белый титульный лист
        r = self.client.get(f"/cover/{with_cover.id}.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"\xff\xd8"))
        r = self.client.get(f"/cover/{blank.id}.jpg")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data, b"")
        catalog = self.client.get("/catalog").get_data(as_text=True)
        self.assertIn("cover-art", catalog)


if __name__ == "__main__":
    unittest.main()
