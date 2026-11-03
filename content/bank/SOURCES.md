# Банк вопросов: источники и лицензии

Вопросы вкладки «Тесты» отобраны из трёх открытых наборов. Отбор, переводы на русский, пояснения (`explain`) и правки текста (Unicode вместо LaTeX, восстановление испорченных символов) сделаны для этого сайта. Вопросы, где ответ спорный или устарел, в банк не вошли.

| Префикс id | Источник | Лицензия | Что взято |
|---|---|---|---|
| `college-…`, `high-school-…`, `conceptual-…`, `abstract-algebra-…` | [MMLU](https://github.com/hendrycks/test) — D. Hendrycks et al., «Measuring Massive Multitask Language Understanding» (2021); данные: [cais/mmlu](https://huggingface.co/datasets/cais/mmlu) | MIT | физика, матанализ, линейная алгебра, алгоритмы (тестовая выборка) |
| `crux-…` | [CRUXEval](https://github.com/facebookresearch/cruxeval) — A. Gu et al. (2024); данные: [cruxeval-org/cruxeval](https://huggingface.co/datasets/cruxeval-org/cruxeval) | MIT | «что вернёт функция» на Python |
| `li-…` | [LinkedIn Skill Assessments Quizzes](https://github.com/Ebazhanov/linkedin-skill-assessments-quizzes) — Ebazhanov and contributors | AGPL-3.0 | квизы C, Python, MySQL (общий SQL) |

Номер в конце id — номер вопроса в исходном наборе: строка тестовой выборки для MMLU и CRUXEval, номер `Qn` в markdown-файле квиза для LinkedIn.

Вопросы с префиксом `li-` и их переводы — производная работа от проекта под AGPL-3.0 и распространяются на тех же условиях: <https://www.gnu.org/licenses/agpl-3.0.html>.

## Формат файла `<предмет>.json`

```json
{"questions": [{
  "id": "college-physics-9", "src": "mmlu", "level": 3, "type": "choice",
  "q": {"ru": "…", "en": "…"},
  "options": {"ru": ["…"], "en": ["…"]},
  "answer": 3,
  "code": "необязательно: фрагмент кода к вопросу",
  "explain": "необязательно: пояснение на русском"
}]}
```

- `type: "choice"`: `answer` — номер правильного варианта, с 0.
- `type: "text"`: `answer` — список допустимых ответов. Сравнение с учётом регистра, но без учёта пробелов и вида кавычек.
- `level` 1–3 — те же уровни, что в учебном плане.
