"""Offline OpenAI response-contract and snapshot-publication tests."""

import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import prepare_embeddings as prepare


class EmbeddingPreparationTests(unittest.TestCase):
    @staticmethod
    def response():
        return {"model": prepare.EMBEDDING_MODEL, "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}

    def test_response_indices_restore_input_order(self):
        self.assertEqual(prepare.parse_embedding_response(self.response(), 2), [[1.0, 0.0], [0.0, 1.0]])

    def test_invalid_api_response_is_rejected_before_publication(self):
        variants = [None, {}, dict(self.response(), model="unexpected-model")]
        for index in (0, 2, True, "1"):
            value = self.response()
            value["data"][0]["index"] = index
            variants.append(value)
        for vector in ([], [0, 0], [True, 1], [float("nan"), 1], [float("inf"), 1], [1, 0, 0]):
            value = self.response()
            value["data"][0]["embedding"] = vector
            variants.append(value)
        variants.append(dict(self.response(), data=self.response()["data"][:1]))
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare.parse_embedding_response(value, 2)

    def test_http_request_uses_configured_model_and_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(self.response()).encode()
        with patch.object(prepare.urllib.request, "urlopen", return_value=response) as call:
            vectors = prepare.fetch_embeddings(["первый текст", "второй текст"], "test-token")
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/embeddings")
        self.assertEqual(json.loads(request.data), {"model": prepare.EMBEDDING_MODEL, "input": ["первый текст", "второй текст"], "encoding_format": "float"})
        self.assertEqual(call.call_args.kwargs["timeout"], 30)
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])

    def test_invalid_header_key_is_not_echoed_or_sent(self):
        secret = "private-token\nnot-a-header"
        with patch.object(prepare.urllib.request, "urlopen") as call:
            with self.assertRaises(ValueError) as error:
                prepare.fetch_embeddings(["text"], secret)
        call.assert_not_called()
        self.assertNotIn("private-token", str(error.exception))

    def test_preparation_publishes_complete_artifact_without_key(self):
        profiles, _ = prepare.load_catalog(prepare.ROOT / "backend" / "data")
        writer = MagicMock()
        writer.__enter__.return_value.name = "memory-only.tmp"
        with patch.object(prepare, "fetch_embeddings", side_effect=lambda texts, key: [[1.0, 0.0] for _ in texts]) as fetch, patch.object(prepare.tempfile, "NamedTemporaryFile", return_value=writer), patch.object(Path, "mkdir"), patch.object(Path, "exists", return_value=False), patch.object(prepare.os, "replace") as replace:
            artifact = prepare.prepare(prepare.ROOT / "backend" / "data", Path("memory-only-output.json"), "test-private-token")
        self.assertEqual(len(artifact["profiles"]), len(profiles))
        self.assertEqual(len(artifact["queries"]), len(prepare.query_inputs(profiles)))
        self.assertNotIn("test-private-token", json.dumps(artifact))
        self.assertGreater(fetch.call_count, 1)
        replace.assert_called_once_with(Path("memory-only.tmp"), Path("memory-only-output.json"))

    def test_failed_batch_preserves_previous_snapshot(self):
        with patch.object(prepare, "fetch_embeddings", side_effect=[[[1.0, 0.0]] * 64, OSError("offline")]), patch.object(prepare.os, "replace") as replace:
            with self.assertRaises(OSError):
                prepare.prepare(prepare.ROOT / "backend" / "data", Path("memory-only-output.json"), "test-token")
        replace.assert_not_called()

    def test_dry_run_needs_no_key_and_makes_no_network_request(self):
        with patch.dict(prepare.os.environ, {"OPENAI_API_KEY": ""}), patch.object(prepare, "fetch_embeddings") as fetch, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(prepare.main(["--dry-run"]), 0)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
