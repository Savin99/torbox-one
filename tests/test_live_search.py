"""Живая проверка поиска раздач на боевом сервере: как ищет приложение, по kp_id.

Запуск только вручную или по расписанию — ходит в настоящие трекеры:

    RUN_LIVE_TESTS=1 SEARCH_API_URL=https://… SEARCH_API_TOKEN=… \\
        .venv/bin/python -m unittest tests.test_live_search -v

Набор — фильмы и сериалы, на которых поиск уже ломался или легко сломается:
одноимённые фильмы разных лет (02.11.2026: «Обитель зла» 2026 тонула
в 2002 и 2016, «Одиссея» 2026 — в «Космической одиссее» 1968), новинки,
старое кино и сериал с сезоном.
"""
from __future__ import annotations

import json
import os
import re
import unittest
import urllib.parse
import urllib.request

LIVE = os.getenv("RUN_LIVE_TESTS") == "1"
BASE = os.getenv("SEARCH_API_URL", "").rstrip("/")
TOKEN = os.getenv("SEARCH_API_TOKEN", "")
GOOD_QUALITY = re.compile(r"(?i)web-?dl|webrip|web\b|bdrip|blu-?ray|bdremux|remux|hdrip")
CAM = re.compile(r"(?i)\b(cam|camrip|ts|telesync|hdts|tc|telecine)\b")
YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

# (название для отчёта, параметры поиска, год фильма или None для сериала, слова названия через |)
CASES: list[tuple[str, dict[str, object], int | None, str]] = [
    ("Обитель зла 2026", {"kp_id": 7378605, "kind": "movie", "year": 2026}, 2026, "обитель|evil"),
    ("Одиссея 2026", {"kp_id": 6385370, "kind": "movie", "year": 2026}, 2026, "одисс|odyssey"),
    ("Интерстеллар 2014", {"kp_id": 258687, "kind": "movie", "year": 2014}, 2014, "интерстеллар|interstellar"),
    ("Дюна 2021", {"q": "Дюна", "kind": "movie", "year": 2021}, 2021, "дюна|dune"),
    ("Слово пацана, 1 сезон", {"q": "Слово пацана", "kind": "series", "season": 1}, None, "пацана|pacana"),
]


def search(params: dict[str, object]) -> dict:
    query = urllib.parse.urlencode({**params, "limit": 60, "deadline": 45})
    request = urllib.request.Request(f"{BASE}/search?{query}", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


@unittest.skipUnless(LIVE and BASE and TOKEN, "живые тесты: RUN_LIVE_TESTS=1, SEARCH_API_URL, SEARCH_API_TOKEN")
class LiveSearchTests(unittest.TestCase):
    """Поиск работает вдоль и поперёк: нужный фильм, все трекеры, нормальное качество."""

    maxDiff = None

    def check(self, label: str, params: dict[str, object], year: int | None, word: str) -> None:
        data = search(params)
        results = data.get("results", [])
        states = {source["name"]: source["state"] for source in data.get("sources", [])}
        with self.subTest(label=label, check="трекеры"):
            for name in ("NNM-Club", "Prowlarr"):
                self.assertEqual(states.get(name), "ok", f"{label}: {name} — {states.get(name)}")
            for name in ("RuTracker", "Kinozal"):
                # Byparr медленный: холодный поиск может не успеть, но не падать.
                self.assertIn(states.get(name, "нет"), {"ok", "timeout"}, f"{label}: {name} — {states.get(name)}")
        with self.subTest(label=label, check="есть выдача"):
            self.assertGreaterEqual(len(results), 10, f"{label}: всего {len(results)}")
        if year is not None:
            with self.subTest(label=label, check="чужие годы не забивают выдачу"):
                foreign = [item["title"] for item in results
                           if (years := {int(value) for value in YEAR.findall(item["title"])})
                           and not any(abs(value - year) <= 1 for value in years)]
                self.assertLessEqual(len(foreign), len(results) // 10, f"{label}: чужие годы {foreign[:5]}")
        with self.subTest(label=label, check="это тот фильм"):
            named = [item for item in results if re.search(word, item["title"].casefold())]
            self.assertGreaterEqual(len(named), len(results) // 2, f"{label}: со словом «{word}» {len(named)} из {len(results)}")
        with self.subTest(label=label, check="нормальное качество"):
            good = [item for item in results if GOOD_QUALITY.search(item["title"]) and not CAM.search(item["title"]) and item["seeders"] >= 5]
            self.assertTrue(good, f"{label}: нет WEB-DL/BDRip с сидами")

    def test_cases(self) -> None:
        for label, params, year, word in CASES:
            self.check(label, params, year, word)


if __name__ == "__main__":
    unittest.main()
