"""Offline regression tests for catalog, ranking fallback and demo scenarios.

Run from the repository root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from copy import deepcopy
from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from ranking import RankingSnapshot, build_artifact, catalog_fingerprint, query_inputs  # noqa: E402
from recommendation_service import RecommendationService, load_catalog, load_profiles  # noqa: E402


DATA_DIR = BACKEND / "data"
MANIFEST_PATH = DATA_DIR / "demo-scenarios.json"
FIXTURES = ROOT / "tests" / "fixtures" / "invalid_synthetic_additions"
GENERATOR_SPEC = spec_from_file_location("generate_demo_catalog", ROOT / "scripts" / "generate_demo_catalog.py")
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
GENERATOR = module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(GENERATOR)


class CatalogCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = load_profiles(DATA_DIR / "profiles.jsonl")

    def test_synthetic_fallback_has_realistic_seasonal_occupancy(self) -> None:
        self.assertEqual(len(self.profiles), 66)
        self.assertTrue(all(profile["synthetic"] is True for profile in self.profiles))
        targets = {9: (3, 8), 10: (12, 31), 11: (12, 30), 12: (23, 31)}
        for profile in self.profiles:
            busy_dates = [date.fromisoformat(value) for value in profile["busy_dates"]]
            for month, (expected_count, days_in_window) in targets.items():
                busy_count = sum(day.month == month for day in busy_dates)
                self.assertEqual(busy_count, expected_count, profile["id"])
                rate = busy_count / days_in_window
                if month == 12:
                    self.assertGreaterEqual(rate, 0.70)
                    self.assertLessEqual(rate, 0.80)
                else:
                    self.assertGreaterEqual(rate, 0.30)
                    self.assertLessEqual(rate, 0.50)

        december = [date.fromisoformat(value) for profile in self.profiles for value in profile["busy_dates"] if value[5:7] == "12"]
        weekend_share = sum(day.weekday() >= 5 for day in december) / len(december)
        self.assertGreater(weekend_share, 8 / 31, "December selection should favour weekends")

    def test_catering_prices_are_event_packages_and_photo_service_has_hours(self) -> None:
        by_id = {profile["id"]: profile for profile in self.profiles}
        for profile_id in ("demo-007", "demo-014"):
            profile = by_id[profile_id]
            self.assertGreaterEqual(profile["price_from_kzt"], 100_000)
            self.assertIn("пакет", profile["description"].lower())
        self.assertEqual(by_id["demo-018"]["max_hours"], 8)

    def test_primary_catalog_rejects_non_synthetic_additions_and_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            load_catalog(FIXTURES)

        duplicate = deepcopy(self.profiles[0])
        with patch("recommendation_service.Path.is_file", return_value=True), patch("recommendation_service.load_profiles", side_effect=[[duplicate, deepcopy(duplicate)], []]):
            with self.assertRaisesRegex(RuntimeError, "Duplicate profile IDs"):
                load_catalog(Path("catalog-with-duplicates"))

    def test_calendar_generator_is_deterministic_and_refuses_non_synthetic_input(self) -> None:
        first = GENERATOR.rebuild(deepcopy(self.profiles))
        second = GENERATOR.rebuild(deepcopy(self.profiles))
        self.assertEqual(first, second)
        non_synthetic = deepcopy(self.profiles[:1])
        non_synthetic[0]["synthetic"] = False
        with self.assertRaisesRegex(ValueError, "synthetic"):
            GENERATOR.rebuild(non_synthetic)


class ManifestAndRecommendationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The manifest documents only the synthetic fallback. It stays
        # testable even when an organizer dataset is present beside it.
        cls.profiles = load_profiles(DATA_DIR / "profiles.jsonl")
        cls.service = RecommendationService(cls.profiles, "demo_fallback", embeddings_path=None)
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_matches_catalog_version_and_required_demo_coverage(self) -> None:
        self.assertEqual(self.manifest["catalog_version"], catalog_fingerprint(self.profiles))
        scenarios = {item["id"]: item for item in self.manifest["scenarios"]}
        self.assertEqual(set(scenarios), {"dense-autumn", "rare-under-three", "no-eligible", "no-category", "dense-date-one", "dense-date-two", "banquet-hall", "december-season"})
        self.assertGreaterEqual(scenarios["dense-autumn"]["expected"]["total_eligible"], 4)
        self.assertLess(scenarios["rare-under-three"]["expected"]["total_eligible"], 3)
        self.assertEqual(scenarios["no-eligible"]["expected"]["outcome"], "no_eligible_candidates")
        self.assertEqual(scenarios["no-category"]["expected"]["outcome"], "no_category_in_city")

    def test_each_manifest_request_matches_expected_lexical_result_without_network(self) -> None:
        with patch("urllib.request.urlopen", side_effect=AssertionError("HTTP is forbidden during serving")):
            for scenario in self.manifest["scenarios"]:
                with self.subTest(scenario=scenario["id"]):
                    response, status = self.service.recommend(scenario["request"])
                    expected = scenario["expected"]
                    self.assertEqual(status, 200)
                    self.assertEqual(response["outcome"], expected["outcome"])
                    self.assertEqual(response["total_eligible"], expected["total_eligible"])
                    self.assertEqual([card["id"] for card in response["results"]], expected["ids"])
                    self.assertEqual(response["rejection_counts"]["busy"], expected["busy_excluded"])
                    self.assertEqual(response["ranking"]["mode"], "lexical")

    def test_recommendations_are_deterministic_and_cards_have_traceable_evidence(self) -> None:
        request = next(item["request"] for item in self.manifest["scenarios"] if item["id"] == "dense-autumn")
        first, _ = self.service.recommend(request)
        second, _ = self.service.recommend(request)
        self.assertEqual([card["id"] for card in first["results"]], [card["id"] for card in second["results"]])
        for card in first["results"]:
            self.assertIn("В описании", card["explanation"])
            self.assertTrue(card["evidence"])
            self.assertTrue({"kind", "label", "value", "source"}.issubset(card["evidence"][0]))
            self.assertEqual(card["evidence"][0]["source"], "busy_dates")

    def test_date_pair_changes_ids_and_reports_busy_reason(self) -> None:
        scenarios = {item["id"]: item for item in self.manifest["scenarios"]}
        first, _ = self.service.recommend(scenarios["dense-date-one"]["request"])
        second, _ = self.service.recommend(scenarios["dense-date-two"]["request"])
        self.assertNotEqual({card["id"] for card in first["results"]}, {card["id"] for card in second["results"]})
        self.assertNotEqual(first["availability_summary"]["busy_excluded"], second["availability_summary"]["busy_excluded"])

    def _snapshot_from_payload(self, payload: str) -> RankingSnapshot:
        fake_path = Path("test-artifact.json")
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "stat", return_value=SimpleNamespace(st_size=len(payload))), patch.object(Path, "read_text", return_value=payload):
            return RankingSnapshot(self.profiles, fake_path)

    def test_malformed_stale_and_partial_artifacts_fall_back_to_lexical(self) -> None:
        stale = self._snapshot_from_payload(json.dumps({"catalog_version": "another-catalog"}))
        malformed = self._snapshot_from_payload("{")
        partial = self._snapshot_from_payload(json.dumps({"schema_version": 1, "model": "text-embedding-3-small", "catalog_version": catalog_fingerprint(self.profiles), "profiles": {}, "queries": {}}))
        self.assertEqual((stale.mode, stale.artifact_status), ("lexical", "stale"))
        self.assertEqual((malformed.mode, malformed.artifact_status), ("lexical", "invalid"))
        self.assertEqual((partial.mode, partial.artifact_status), ("lexical", "invalid"))

    def test_valid_semantic_artifact_is_reused_without_external_requests(self) -> None:
        profile_vectors = {profile["id"]: [1.0, 0.0] for profile in self.profiles}
        query_vectors = {key: [1.0, 0.0] for key in query_inputs(self.profiles)}
        payload = json.dumps(build_artifact(self.profiles, profile_vectors, query_vectors))
        first = self._snapshot_from_payload(payload)
        second = self._snapshot_from_payload(payload)
        request = next(item["request"] for item in self.manifest["scenarios"] if item["id"] == "dense-autumn")
        candidates = [profile for profile in self.profiles if profile["city"] == request["city"] and request["category"] in profile["categories"]]
        self.assertEqual((first.mode, first.artifact_status), ("semantic", "ready"))
        self.assertEqual(first.scores(request, candidates), second.scores(request, candidates))


class RequestValidationAndFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profiles = load_profiles(DATA_DIR / "profiles.jsonl")
        profile = dict(profiles[0])
        profile.update({
            "id": "filter-fixture",
            "city": "Тестовый город",
            "categories": ["Тестовая категория"],
            "price_from_kzt": 100,
            "event_formats": ["Тестовый формат"],
            "languages": ["русский"],
            "max_hours": 2,
            "busy_dates": ["2026-10-01"],
            "description": "Тестовый подрядчик для проверки независимых фильтров.",
            "synthetic": True,
        })
        cls.service = RecommendationService([profile], "test", embeddings_path=None)

    @staticmethod
    def request(**changes: object) -> dict[str, object]:
        payload: dict[str, object] = {"city": "Тестовый город", "date": "2026-10-02", "event_type": "Тестовый формат", "category": "Тестовая категория", "budget_kzt": 100}
        payload.update(changes)
        return payload

    def test_required_and_invalid_fields_are_rejected(self) -> None:
        invalid = [
            {},
            self.request(city=""),
            self.request(date="2026-2-1"),
            self.request(budget_kzt=True),
            self.request(budget_kzt=math.nan),
            self.request(budget_kzt=10 ** 10000),
            self.request(duration_hours=0),
            self.request(language=""),
        ]
        for index, payload in enumerate(invalid):
            with self.subTest(case=index):
                response, status = self.service.recommend(payload)
                self.assertEqual((status, response["error"]), (400, "invalid_request"))

    def test_busy_budget_format_language_and_duration_filters_are_independent(self) -> None:
        checks = [
            (self.request(date="2026-10-01"), "busy"),
            (self.request(budget_kzt=99), "budget"),
            (self.request(event_type="Другой формат"), "format"),
            (self.request(language="казахский"), "language"),
            (self.request(duration_hours=3), "duration"),
        ]
        for payload, expected_reason in checks:
            with self.subTest(reason=expected_reason):
                response, status = self.service.recommend(payload)
                self.assertEqual(status, 200)
                self.assertEqual(response["outcome"], "no_eligible_candidates")
                self.assertEqual(response["rejection_counts"][expected_reason], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
