"""HTTP contract checks using the in-process stdlib server factory."""

from __future__ import annotations

import http.client
import copy
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import server  # noqa: E402
from backend.recommendation_service import RecommendationService  # noqa: E402
from backend.ranking import RankingSnapshot, build_artifact, query_inputs  # noqa: E402


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = server.create_server(port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.httpd.server_address[:2]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_options_exposes_catalog_and_ranking_metadata(self) -> None:
        status, payload = self.request("GET", "/api/options")
        self.assertEqual(status, 200)
        self.assertEqual(payload["catalog"]["source"], server.ENGINE.catalog_source)
        self.assertGreater(payload["catalog"]["total_profiles"], 0)
        self.assertIn(payload["ranking"]["mode"], {"lexical", "semantic"})

    def test_demo_manifest_is_served_only_when_version_matches_fallback_catalog(self) -> None:
        status, payload = self.request("GET", "/api/demo-scenarios")
        self.assertEqual(status, 200)
        if server.ENGINE.catalog_source == "demo_fallback":
            self.assertEqual(payload["catalog_version"], server.ENGINE.catalog_version)
            self.assertEqual(len(payload["scenarios"]), 8)
        else:
            self.assertEqual(payload, {"scenarios": []})

        stale_engine = SimpleNamespace(catalog_source="demo_fallback", catalog_version="stale")
        primary_engine = SimpleNamespace(catalog_source="primary", catalog_version=server.ENGINE.catalog_version)
        with patch.object(server, "ENGINE", stale_engine):
            self.assertEqual(server.demo_scenarios(), {"scenarios": []})
        with patch.object(server, "ENGINE", primary_engine):
            self.assertEqual(server.demo_scenarios(), {"scenarios": []})

    def test_manifest_request_round_trips_through_http_api(self) -> None:
        _, manifest = self.request("GET", "/api/demo-scenarios")
        if not manifest["scenarios"]:
            self.assertNotEqual(server.ENGINE.catalog_source, "demo_fallback")
            return
        for scenario in manifest["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                body = json.dumps(scenario["request"], ensure_ascii=False).encode("utf-8")
                started = time.perf_counter()
                status, payload = self.request("POST", "/api/recommendations", body, {"Content-Type": "application/json"})
                self.assertLess(time.perf_counter() - started, 10)
                self.assertEqual(status, 200)
                self.assertEqual(payload["outcome"], scenario["expected"]["outcome"])
                self.assertEqual(payload["total_eligible"], scenario["expected"]["total_eligible"])
                self.assertEqual(payload["availability_summary"]["busy_excluded"], scenario["expected"]["busy_excluded"])
                ids = [card["id"] for card in payload["results"]]
                self.assertEqual(len(ids), min(3, payload["total_eligible"]))
                self.assertEqual(len(ids), len(set(ids)))
                self.assert_cards_eligible(payload, scenario["request"], server.ENGINE.profiles)
                _, repeated = self.request("POST", "/api/recommendations", body, {"Content-Type": "application/json"})
                self.assertEqual(ids, [card["id"] for card in repeated["results"]])
                # The manifest pins lexical IDs only. Semantic ordering is
                # tested independently below with known, unequal vectors.
                if payload["ranking"]["mode"] == "lexical":
                    self.assertEqual(ids, scenario["expected"]["ids"])

    def assert_cards_eligible(self, payload: dict, request: dict, profiles: list[dict]) -> None:
        by_id = {profile["id"]: profile for profile in profiles}
        for card in payload["results"]:
            profile = by_id[card["id"]]
            self.assertEqual(profile["city"], request["city"])
            self.assertIn(request["category"], profile["categories"])
            self.assertNotIn(request["date"], profile["busy_dates"])
            self.assertLessEqual(profile["price_from_kzt"], request["budget_kzt"])
            self.assertIn(request["event_type"], profile["event_formats"])
            if request.get("language"):
                self.assertIn(request["language"], profile["languages"])
            if request.get("duration_hours") is not None and profile["max_hours"] is not None:
                self.assertLessEqual(request["duration_hours"], profile["max_hours"])

    @staticmethod
    def semantic_fixture() -> RecommendationService:
        base = copy.deepcopy(server.ENGINE.profiles[0])
        base.update(city="Алматы", categories=["Ведущий"], event_formats=["день рождения"],
                    languages=["русский"], max_hours=3, busy_dates=[],
                    description="Ведущий с интерактивной программой.")
        changes = [
            {"id": "a", "price_from_kzt": 100},
            {"id": "b", "price_from_kzt": 200},
            {"id": "c", "price_from_kzt": 300},
            {"id": "busy", "price_from_kzt": 50, "busy_dates": ["2026-10-02"]},
            {"id": "budget", "price_from_kzt": 400},
            {"id": "format", "price_from_kzt": 50, "event_formats": ["свадьба"]},
            {"id": "language", "price_from_kzt": 50, "languages": ["английский"]},
            {"id": "duration", "price_from_kzt": 50, "max_hours": 1},
        ]
        profiles = [dict(base, **change) for change in changes]
        vectors = {profile["id"]: [1.0, 0.0] for profile in profiles}
        vectors.update(a=[0.0, 1.0], b=[0.6, 0.8], c=[1.0, 0.0])
        artifact = build_artifact(profiles, vectors, {key: [1.0, 0.0] for key in query_inputs(profiles)})
        serialized = json.dumps(artifact)
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "stat", return_value=SimpleNamespace(st_size=len(serialized))), patch.object(Path, "read_text", return_value=serialized):
            ranking = RankingSnapshot(profiles, Path("in-memory-semantic-fixture.json"))
        engine = RecommendationService(profiles, "test", embeddings_path=None)
        engine.ranking = ranking
        return engine

    def test_semantic_http_order_uses_vectors_and_preserves_all_hard_filters(self) -> None:
        request = {"city": "Алматы", "category": "Ведущий", "date": "2026-10-02", "event_type": "день рождения", "budget_kzt": 300, "language": "русский", "duration_hours": 2}
        body = json.dumps(request).encode("utf-8")
        semantic = self.semantic_fixture()
        lexical = RecommendationService(semantic.profiles, "test", embeddings_path=None)
        self.assertEqual([card["id"] for card in lexical.recommend(request)[0]["results"]], ["a", "b", "c"])
        versions = []
        for engine in (semantic, self.semantic_fixture()):
            with patch.object(server, "ENGINE", engine), patch("urllib.request.urlopen", side_effect=AssertionError("Serving must not contact OpenAI")):
                for _ in range(2):
                    status, payload = self.request("POST", "/api/recommendations", body, {"Content-Type": "application/json"})
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["ranking"]["mode"], "semantic")
                    self.assertFalse(payload["degraded"])
                    self.assertEqual([card["id"] for card in payload["results"]], ["c", "b", "a"])
                    self.assertEqual(payload["total_eligible"], 3)
                    self.assertEqual(payload["rejection_counts"], {"busy": 1, "budget": 1, "format": 1, "language": 1, "duration": 1})
                    self.assert_cards_eligible(payload, request, engine.profiles)
                    versions.append(payload["ranking"]["version"])
        self.assertEqual(len(set(versions)), 1)

    def test_manifest_http_accepts_semantic_order_different_from_lexical_ids(self) -> None:
        manifest = server.demo_scenarios()
        if not manifest["scenarios"]:
            self.skipTest("Demo manifest belongs to the synthetic catalog only")
        profiles = server.ENGINE.profiles
        scenario = next(item for item in manifest["scenarios"] if item["id"] == "dense-autumn")
        query = scenario["request"]
        eligible = [profile for profile in profiles if profile["city"] == query["city"] and query["category"] in profile["categories"] and query["date"] not in profile["busy_dates"] and profile["price_from_kzt"] <= query["budget_kzt"] and query["event_type"] in profile["event_formats"]]
        target = next(profile["id"] for profile in eligible if profile["id"] not in scenario["expected"]["ids"])
        artifact = build_artifact(profiles, {profile["id"]: ([1.0, 0.0] if profile["id"] == target else [0.0, 1.0]) for profile in profiles}, {key: [1.0, 0.0] for key in query_inputs(profiles)})
        serialized = json.dumps(artifact)
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "stat", return_value=SimpleNamespace(st_size=len(serialized))), patch.object(Path, "read_text", return_value=serialized):
            ranking = RankingSnapshot(profiles, Path("in-memory-manifest-artifact.json"))
        engine = RecommendationService(profiles, "demo_fallback", embeddings_path=None)
        engine.ranking = ranking
        self.assertEqual(engine.recommend(query)[0]["results"][0]["id"], target)
        with patch.object(server, "ENGINE", engine):
            self.test_manifest_request_round_trips_through_http_api()

    def test_http_rejects_wrong_content_type_and_nonstandard_json_constants(self) -> None:
        status, payload = self.request("POST", "/api/recommendations", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual((status, payload["error"]), (415, "unsupported_content_type"))
        status, payload = self.request("POST", "/api/recommendations", b'{"budget_kzt":NaN}', {"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]), (400, "invalid_json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
