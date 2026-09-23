"""HTTP contract checks using the in-process stdlib server factory."""

from __future__ import annotations

import http.client
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
        scenario = next(item for item in manifest["scenarios"] if item["id"] == "dense-autumn")
        body = json.dumps(scenario["request"], ensure_ascii=False).encode("utf-8")
        started = time.perf_counter()
        status, payload = self.request("POST", "/api/recommendations", body, {"Content-Type": "application/json"})
        self.assertLess(time.perf_counter() - started, 10)
        self.assertEqual(status, 200)
        self.assertEqual(payload["outcome"], scenario["expected"]["outcome"])
        self.assertEqual(payload["total_eligible"], scenario["expected"]["total_eligible"])
        self.assertEqual([card["id"] for card in payload["results"]], scenario["expected"]["ids"])

    def test_http_rejects_wrong_content_type_and_nonstandard_json_constants(self) -> None:
        status, payload = self.request("POST", "/api/recommendations", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual((status, payload["error"]), (415, "unsupported_content_type"))
        status, payload = self.request("POST", "/api/recommendations", b'{"budget_kzt":NaN}', {"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]), (400, "invalid_json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
