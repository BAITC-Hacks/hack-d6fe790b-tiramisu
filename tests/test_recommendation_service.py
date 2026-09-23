"""Stdlib-only checks for the recommendation service and JSONL catalog.

Run from the repository root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import os
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FIXTURES = ROOT / "tests" / "fixtures" / "invalid_synthetic_additions"
sys.path.insert(0, str(BACKEND))

from recommendation_service import (  # noqa: E402
    DATE_MAX,
    DATE_MIN,
    RecommendationService,
    load_catalog,
    validate_profile,
)


class RecommendationServiceTests(unittest.TestCase):
    """Contract checks that use whichever catalog is selected by load_catalog."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles, cls.catalog_source = load_catalog(BACKEND / "data")
        cls.service = RecommendationService(cls.profiles, cls.catalog_source)

    @staticmethod
    def _days() -> list[str]:
        total = (DATE_MAX - DATE_MIN).days
        return [(DATE_MIN + timedelta(days=offset)).isoformat() for offset in range(total + 1)]

    def _open_date_for(self, profile: dict) -> str:
        busy = set(profile["busy_dates"])
        return next(day for day in self._days() if day not in busy)

    def _request_for(self, profile: dict, *, event_type: str | None = None, date_value: str | None = None, budget: int | None = None) -> dict:
        return {
            "city": profile["city"],
            "date": date_value or self._open_date_for(profile),
            "event_type": event_type or profile["event_formats"][0],
            "category": profile["categories"][0],
            "budget_kzt": budget if budget is not None else max(int(item["price_from_kzt"]) for item in self.profiles) + 1,
        }

    def _profile_with_busy_date(self) -> tuple[dict, str]:
        for profile in self.profiles:
            for busy_day in profile["busy_dates"]:
                busy_date = date.fromisoformat(busy_day)
                if DATE_MIN <= busy_date <= DATE_MAX:
                    return profile, busy_day
        self.fail("The catalog has no busy date in the supported request range")

    def setUp(self) -> None:
        # Tests are offline and must never use a developer's real API key.
        self._environment = patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
        self._environment.start()

    def tearDown(self) -> None:
        self._environment.stop()

    def test_recommendation_returns_at_most_three_cards_and_is_fast(self) -> None:
        profile = self.profiles[0]
        payload = self._request_for(profile)
        started = time.perf_counter()
        response, status = self.service.recommend(payload)
        elapsed = time.perf_counter() - started

        self.assertEqual(status, 200)
        self.assertEqual(response["outcome"], "recommended")
        self.assertGreaterEqual(response["total_eligible"], len(response["results"]))
        self.assertLessEqual(len(response["results"]), 3)
        self.assertLess(elapsed, 10, "A local catalog request must complete within 10 seconds")
        for card in response["results"]:
            self.assertTrue(card["explanation"])
            self.assertIn("₸", card["explanation"])

    def test_no_category_in_city_is_a_distinct_outcome(self) -> None:
        profile = self.profiles[0]
        payload = self._request_for(profile)
        payload["category"] = "Несуществующая категория для теста"

        response, status = self.service.recommend(payload)
        self.assertEqual(status, 200)
        self.assertEqual(response["outcome"], "no_category_in_city")
        self.assertEqual(response["results"], [])

    def test_rejection_counts_report_busy_budget_and_format(self) -> None:
        profile, busy_day = self._profile_with_busy_date()
        high_budget = max(int(item["price_from_kzt"]) for item in self.profiles) + 1

        busy_response, busy_status = self.service.recommend(
            self._request_for(profile, date_value=busy_day, budget=high_budget)
        )
        self.assertEqual(busy_status, 200)
        self.assertGreaterEqual(busy_response["rejection_counts"]["busy"], 1)
        self.assertEqual(busy_response["availability_summary"]["date"], busy_day)
        self.assertEqual(
            busy_response["availability_summary"]["busy_excluded"],
            busy_response["rejection_counts"]["busy"],
        )

        budget_response, budget_status = self.service.recommend(
            self._request_for(profile, budget=0)
        )
        self.assertEqual(budget_status, 200)
        self.assertGreaterEqual(budget_response["rejection_counts"]["budget"], 1)

        format_response, format_status = self.service.recommend(
            self._request_for(profile, event_type="несовместимый формат теста", budget=high_budget)
        )
        self.assertEqual(format_status, 200)
        self.assertGreaterEqual(format_response["rejection_counts"]["format"], 1)

    def test_required_and_optional_request_validation(self) -> None:
        response, status = self.service.recommend({})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "invalid_request")

        profile = self.profiles[0]
        invalid_optional = self._request_for(profile)
        invalid_optional["duration_hours"] = 0
        response, status = self.service.recommend(invalid_optional)
        self.assertEqual(status, 400)

        valid_optional = self._request_for(profile)
        valid_optional["language"] = profile["languages"][0]
        valid_optional["duration_hours"] = 1
        response, status = self.service.recommend(valid_optional)
        self.assertEqual(status, 200)
        self.assertIn(response["outcome"], {"recommended", "no_eligible_candidates"})

    def test_repeated_request_has_identical_order(self) -> None:
        payload = self._request_for(self.profiles[0])
        first, first_status = self.service.recommend(payload)
        second, second_status = self.service.recommend(payload)

        self.assertEqual((first_status, first["outcome"]), (second_status, second["outcome"]))
        self.assertEqual([card["id"] for card in first["results"]], [card["id"] for card in second["results"]])

    def test_two_dates_change_availability_and_name_busy_reason(self) -> None:
        profile, busy_day = self._profile_with_busy_date()
        open_day = self._open_date_for(profile)
        high_budget = max(int(item["price_from_kzt"]) for item in self.profiles) + 1
        busy_payload = self._request_for(profile, date_value=busy_day, budget=high_budget)
        open_payload = self._request_for(profile, date_value=open_day, budget=high_budget)

        # A single catalog record makes the date-driven change unambiguous even
        # when a production category has more than three suitable candidates.
        isolated_service = RecommendationService([profile], self.catalog_source)
        busy_response, _ = isolated_service.recommend(busy_payload)
        open_response, _ = isolated_service.recommend(open_payload)
        busy_ids = {card["id"] for card in busy_response["results"]}
        open_ids = {card["id"] for card in open_response["results"]}

        self.assertNotIn(profile["id"], busy_ids)
        self.assertIn(profile["id"], open_ids)
        self.assertGreaterEqual(busy_response["availability_summary"]["busy_excluded"], 1)
        self.assertEqual(busy_response["availability_summary"]["date"], busy_day)
        self.assertNotEqual(busy_ids, open_ids)

    def test_absent_openai_key_uses_deterministic_lexical_fallback(self) -> None:
        payload = self._request_for(self.profiles[0])
        first, first_status = self.service.recommend(payload)
        second, second_status = self.service.recommend(payload)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(first["degraded"])
        self.assertTrue(second["degraded"])
        self.assertEqual([card["id"] for card in first["results"]], [card["id"] for card in second["results"]])

    def test_malformed_embeddings_response_uses_lexical_fallback(self) -> None:
        payload = self._request_for(self.profiles[0])
        with patch.object(self.service, "_embeddings", side_effect=IndexError("bad embedding index")):
            response, status = self.service.recommend(payload)
        self.assertEqual(status, 200)
        self.assertEqual(response["outcome"], "recommended")
        self.assertTrue(response["degraded"])

    def test_documented_fallback_demo_scenarios(self) -> None:
        if self.catalog_source != "demo_fallback":
            self.skipTest("README fallback scenarios apply only before the source catalog is added")
        scenarios = (
            ({"city": "Алматы", "date": "2026-10-18", "event_type": "юбилей", "category": "Ведущий", "budget_kzt": 350000, "duration_hours": 5, "language": "русский"}, "recommended", 6, 3),
            ({"city": "Алматы", "date": "2026-11-01", "event_type": "юбилей", "category": "Флорист", "budget_kzt": 250000, "language": "русский"}, "recommended", 3, 3),
            ({"city": "Алматы", "date": "2026-11-14", "event_type": "свадьба", "category": "Ведущий", "budget_kzt": 100000, "language": "русский"}, "no_eligible_candidates", 0, 0),
        )
        for payload, outcome, eligible, displayed in scenarios:
            with self.subTest(payload=payload):
                response, status = self.service.recommend(payload)
                self.assertEqual(status, 200)
                self.assertEqual(response["outcome"], outcome)
                self.assertEqual(response["total_eligible"], eligible)
                self.assertEqual(len(response["results"]), displayed)


class CatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles, cls.catalog_source = load_catalog(BACKEND / "data")

    def test_catalog_records_have_valid_required_fields_dates_and_synthetic_flag(self) -> None:
        self.assertTrue(self.profiles)
        for profile in self.profiles:
            self.assertIsInstance(profile["synthetic"], bool)
            self.assertIsInstance(profile["price_from_kzt"], (int, float))
            self.assertIsInstance(profile["categories"], list)
            for busy_day in profile["busy_dates"]:
                date.fromisoformat(busy_day)

    def test_synthetic_fallback_has_the_documented_catalog_shape(self) -> None:
        if self.catalog_source != "demo_fallback":
            self.skipTest("The real source catalog defines its own category distribution")
        category_counts = {}
        for profile in self.profiles:
            for category in profile["categories"]:
                category_counts[category] = category_counts.get(category, 0) + 1
        self.assertEqual(len(self.profiles), 66)
        self.assertTrue(all(profile["synthetic"] for profile in self.profiles))
        self.assertEqual(category_counts["Ведущий"], 15)
        self.assertEqual(category_counts["Фотограф"], 12)
        self.assertEqual(category_counts["Банкетный зал"], 8)

    def test_profile_validation_rejects_missing_fields_bad_dates_and_wrong_synthetic_type(self) -> None:
        valid = copy.deepcopy(self.profiles[0])
        cases = []
        missing = copy.deepcopy(valid)
        del missing["city"]
        cases.append(missing)
        bad_date = copy.deepcopy(valid)
        bad_date["busy_dates"] = ["2026-99-99"]
        cases.append(bad_date)
        bad_synthetic = copy.deepcopy(valid)
        bad_synthetic["synthetic"] = "true"
        cases.append(bad_synthetic)

        for record in cases:
            with self.subTest(record=record):
                with self.assertRaisesRegex(RuntimeError, "Invalid profile"):
                    validate_profile(record, Path("invalid.jsonl"), 1)

    def test_synthetic_additions_require_the_synthetic_flag(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic-additions"):
            load_catalog(FIXTURES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
