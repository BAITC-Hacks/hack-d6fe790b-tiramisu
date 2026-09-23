"""Regressions for factual, request-specific explanations on the unchanged data."""

import copy
import json
import unittest
from pathlib import Path

from backend.explanations import description_excerpt, price_comparison
from backend.recommendation_service import RecommendationService, load_profiles

DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


class ExplanationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = load_profiles(DATA / "profiles.jsonl")
        cls.by_id = {profile["id"]: profile for profile in cls.profiles}
        cls.service = RecommendationService(cls.profiles, "demo_fallback", embeddings_path=None)
        cls.scenarios = json.loads((DATA / "demo-scenarios.json").read_text(encoding="utf-8"))["scenarios"]

    def test_birthday_uses_program_details_instead_of_other_event_advertising(self):
        query = next(item["request"] for item in self.scenarios if item["id"] == "dense-autumn")
        result, _ = self.service.recommend(query)
        excerpts = {card["id"]: next(fact["value"] for fact in card["evidence"] if fact["kind"] == "description") for card in result["results"]}
        self.assertEqual(excerpts["demo-021"], "Заранее собираю истории гостей для персональной программы")
        self.assertEqual(excerpts["demo-026"], "В программе — традиционные благословения и спокойные конкурсы для разных поколений")
        self.assertEqual(excerpts["demo-001"], "Интерактивная программа и координация вечера")
        self.assertEqual(len(set(excerpts.values())), 3)
        self.assertNotIn("семейных тоев", " ".join(excerpts.values()))
        cheapest = result["results"][0]
        self.assertIn("Самая низкая", next(fact["value"] for fact in cheapest["evidence"] if fact["kind"] == "comparison"))

    def test_excerpts_are_exact_complete_source_sentences_for_every_demo(self):
        for scenario in self.scenarios:
            result, _ = self.service.recommend(scenario["request"])
            for card in result["results"]:
                with self.subTest(scenario=scenario["id"], profile=card["id"]):
                    for fact in card["evidence"]:
                        if fact["kind"] == "description":
                            self.assertIn(fact["value"], self.by_id[card["id"]]["description"])
                            self.assertFalse(fact["value"].endswith("…"))
                    self.assertIn(len(card["explanation"].split(". ")), (1, 2))

    def test_other_format_only_description_is_not_repurposed(self):
        profile = copy.deepcopy(self.profiles[0])
        profile["description"] = "Веду современные тои и свадьбы. Программа для молодожёнов."
        request = {"event_type": "день рождения", "category": "Ведущий"}
        self.assertIsNone(description_excerpt(request, profile, [profile]))
        profile["description"] = "Свадебная программа. Интерактив длится 2 часа только при наличии отдельной сцены."
        self.assertEqual(description_excerpt(request, profile, [profile]), "Интерактив длится 2 часа только при наличии отдельной сцены")

    def test_missing_relevant_excerpt_does_not_invent_a_distinguishing_fact(self):
        profile = copy.deepcopy(self.by_id["demo-001"])
        profile["description"] = "Ведущий свадеб. Программа для молодожёнов."
        service = RecommendationService([profile], "test", embeddings_path=None)
        request = {"city": profile["city"], "date": "2026-10-02", "event_type": "день рождения", "category": "Ведущий", "budget_kzt": 500000}
        result, _ = service.recommend(request)
        card = result["results"][0]
        self.assertFalse(any(fact["kind"] == "description" for fact in card["evidence"]))
        self.assertNotIn("молодож", card["explanation"])
        self.assertNotIn("В описании", card["explanation"])
        self.assertIn("день рождения", card["explanation"])

    def test_price_comparison_handles_ties_and_does_not_compare_outside_cards(self):
        a, b, c = ({"id": key, "price_from_kzt": price} for key, price in (("a", 100), ("b", 100), ("c", 200)))
        self.assertIsNone(price_comparison(a, [a]))
        self.assertIsNone(price_comparison(a, [a, b, c]))
        self.assertIsNone(price_comparison(c, [a, c]))
        self.assertEqual(price_comparison(a, [a, c]), "Самая низкая начальная цена среди 2 показанных вариантов")

    def test_explanations_keep_requested_language_duration_and_budget_facts(self):
        profile = self.by_id["demo-001"]
        service = RecommendationService([profile], "test", embeddings_path=None)
        request = {"city": profile["city"], "date": "2026-10-02", "event_type": "день рождения", "category": "Ведущий", "budget_kzt": 500000, "language": "русский", "duration_hours": 1}
        response, status = service.recommend(request)
        self.assertEqual(status, 200)
        card = response["results"][0]
        facts = {fact["kind"]: fact["value"] for fact in card["evidence"]}
        self.assertIn("русский", facts["language"])
        self.assertIn("Запрошено 1 ч", facts["duration"])
        self.assertIn("500 000", facts["budget"])
        self.assertNotIn("comparison", facts)


if __name__ == "__main__":
    unittest.main()
