import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import channel as ch  # noqa: E402
import signals as sg  # noqa: E402

# synthetic posts written in the same style as public setup channels
LONG = """$ABC

Тренд вверх на часовике и на старшем, работаю от лонга. Цена 1.25$.

Лонг: стоп 1.18$, цели 1.32$ / 1.40$ / 1.55$."""
SHORT = """$XYZ · хай-риск

Тренд вниз, работаю от шорта. Цена 0.00434$.

Вариант для шорта: стоп за 0.004603, первый тейк 0.003798, дальше веду позицию стопом."""
CONTRADICTORY = """$BAD
Работаю от лонга. Цена 0.0784$.
Под лонг: стоп за 0.0135$, тейки 0.143$ / 0.195$ / 0.254$."""
EDU = "Зачем нужен старший таймфрейм. Старший ТФ даёт направление, младший — точку входа. #ОБУЧАЮЩИЙПОСТ"

PAGE = "".join(
    f'<div class="tgme_widget_message_wrap"><div data-post="chan/{i}"><time datetime="2026-10-24T1{i}:00:00+00:00">'
    f'</time><div class="tgme_widget_message_text js-message_text">{text.replace(chr(10), "<br/>")}</div></div></div>'
    for i, text in enumerate([LONG, SHORT, CONTRADICTORY, EDU, LONG], start=1))


class Parse(unittest.TestCase):
    def test_long_with_three_targets(self):
        s = ch.parse_setup(LONG)
        self.assertEqual((s["ticker"], s["side"], s["entry"], s["stop"]), ("ABC", "long", 1.25, 1.18))
        self.assertEqual(s["tps"], [1.32, 1.40, 1.55])
        self.assertTrue(ch.valid(s))

    def test_short_first_target(self):
        s = ch.parse_setup(SHORT)
        self.assertEqual((s["ticker"], s["side"], s["stop"], s["tps"]), ("XYZ", "short", 0.004603, [0.003798]))
        self.assertTrue(ch.valid(s))

    def test_contradictory_numbers_rejected(self):
        s = ch.parse_setup(CONTRADICTORY)
        self.assertIsNotNone(s)
        self.assertFalse(ch.valid(s))

    def test_education_post_ignored(self):
        self.assertIsNone(ch.parse_setup(EDU))

    def test_page_parsing_and_dedupe(self):
        posts = ch.parse_posts(PAGE)
        self.assertEqual([p[0] for p in posts], [1, 2, 3, 4, 5])
        got = ch.setups(posts)
        self.assertEqual([(s["post_id"], s["ticker"]) for s in got], [(1, "ABC"), (2, "XYZ")])  # repost #5 dropped


class Parts(unittest.TestCase):
    def test_parts_follow_number_of_targets(self):
        c = {"qty_step": 0.01, "min_qty": 0.01, "taker_fee": 0.0005}
        one = sg.size("long", 100, 98, 50, 2, 20, c, n_parts=1)
        self.assertEqual(len(one["parts"]), 1)
        self.assertAlmostEqual(one["parts"][0], one["qty"])
        two = sg.size("long", 100, 98, 50, 2, 20, c, n_parts=2)
        self.assertEqual(len(two["parts"]), 2)
        self.assertAlmostEqual(sum(two["parts"]), two["qty"])


if __name__ == "__main__":
    unittest.main()
