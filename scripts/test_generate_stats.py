import unittest
import xml.etree.ElementTree as ET
from datetime import date

from generate_stats import THEMES, Streak, compute_streaks, render_languages, render_stats, render_streak, top_languages

TODAY = date(2026, 3, 10)


def days(*pairs):
    return {day: count for day, count in pairs}


SAMPLE = {
    "login": "Mafu2k",
    "stars": 1234,
    "commits": 567,
    "pull_requests": 12,
    "issues": 3,
    "repos": 20,
    "contributed_to": 4,
    "languages": [
        {"name": "Java", "color": "#b07219", "size": 600},
        {"name": "Kotlin", "color": "#A97BFF", "size": 300},
        {"name": "C++ & <friends>", "color": None, "size": 100},
    ],
    "days": days(("2026-03-08", 2), ("2026-03-09", 1), ("2026-03-10", 0)),
}


class ComputeStreaksTest(unittest.TestCase):
    def test_empty_calendar_has_no_streaks(self):
        self.assertEqual(compute_streaks({}, TODAY), (Streak(), Streak()))

    def test_today_without_contributions_keeps_current_streak(self):
        current, _ = compute_streaks(SAMPLE["days"], TODAY)
        self.assertEqual(current, Streak(2, date(2026, 3, 8), date(2026, 3, 9)))

    def test_gap_before_yesterday_breaks_current_streak(self):
        calendar = days(("2026-03-07", 5), ("2026-03-08", 0), ("2026-03-09", 0), ("2026-03-10", 0))
        current, longest = compute_streaks(calendar, TODAY)
        self.assertEqual(current, Streak())
        self.assertEqual(longest, Streak(1, date(2026, 3, 7), date(2026, 3, 7)))

    def test_longest_streak_spans_year_boundary(self):
        calendar = days(
            ("2025-12-30", 1), ("2025-12-31", 1), ("2026-01-01", 1),
            ("2026-01-02", 0), ("2026-03-10", 4),
        )
        current, longest = compute_streaks(calendar, TODAY)
        self.assertEqual(current, Streak(1, TODAY, TODAY))
        self.assertEqual(longest, Streak(3, date(2025, 12, 30), date(2026, 1, 1)))

    def test_missing_days_count_as_gaps(self):
        calendar = days(("2026-03-01", 1), ("2026-03-03", 1))
        _, longest = compute_streaks(calendar, TODAY)
        self.assertEqual(longest.length, 1)


class TopLanguagesTest(unittest.TestCase):
    def test_percentages_cover_everything(self):
        result = top_languages(SAMPLE["languages"])
        self.assertEqual([lang["name"] for lang in result], ["Java", "Kotlin", "C++ & <friends>"])
        self.assertAlmostEqual(sum(lang["percent"] for lang in result), 100)

    def test_tail_is_folded_into_other(self):
        languages = [{"name": f"L{i}", "color": "#000000", "size": 10 - i} for i in range(10)]
        result = top_languages(languages, limit=4)
        self.assertEqual([lang["name"] for lang in result], ["L0", "L1", "L2", "Other"])
        self.assertAlmostEqual(sum(lang["percent"] for lang in result), 100)

    def test_no_code_means_no_languages(self):
        self.assertEqual(top_languages([]), [])


class RenderTest(unittest.TestCase):
    def assertValidSvg(self, svg):
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        return svg

    def test_all_cards_are_valid_svg_in_every_theme(self):
        for theme in THEMES.values():
            self.assertValidSvg(render_stats(SAMPLE, theme))
            self.assertValidSvg(render_streak(SAMPLE, theme, TODAY))
            self.assertValidSvg(render_languages(SAMPLE, theme))

    def test_cards_render_for_a_brand_new_account(self):
        empty = {**SAMPLE, "languages": [], "days": {}}
        theme = THEMES["dark"]
        self.assertIn("No public code yet", self.assertValidSvg(render_languages(empty, theme)))
        self.assertIn("No active streak", self.assertValidSvg(render_streak(empty, theme, TODAY)))

    def test_numbers_are_formatted(self):
        self.assertIn(">1,234<", render_stats(SAMPLE, THEMES["dark"]))


if __name__ == "__main__":
    unittest.main()
