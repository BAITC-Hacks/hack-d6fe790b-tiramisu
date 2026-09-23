"""Explicitly prepare a complete OpenAI embedding snapshot outside HTTP requests.

Run from the repository root:
    python scripts/prepare_embeddings.py --dry-run
    python scripts/prepare_embeddings.py --prompt-key

Alternatively, supply OPENAI_API_KEY in this process's environment. No key is
written to the artifact, log, command arguments, or repository.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from recommendation_service import load_catalog  # noqa: E402
from ranking import (  # noqa: E402
    DEFAULT_EMBEDDINGS_PATH,
    EMBEDDING_MODEL,
    build_artifact,
    canonical_json,
    catalog_fingerprint,
    profile_text,
    query_inputs,
    validate_vector,
)

BATCH_SIZE = 64
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def parse_embedding_response(payload: Any, count: int) -> list[list[float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Embedding response has no data array")
    if payload.get("model") != EMBEDDING_MODEL:
        raise ValueError("Embedding response has an unexpected model")
    items = payload["data"]
    if len(items) != count:
        raise ValueError("Embedding response has an incorrect number of vectors")
    by_index: dict[int, list[float]] = {}
    dimension: int | None = None
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid embedding record")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < count or index in by_index:
            raise ValueError("Embedding response has an invalid or duplicate index")
        vector = validate_vector(item.get("embedding"), dimension)
        dimension = len(vector)
        by_index[index] = vector
    return [by_index[index] for index in range(count)]


def fetch_embeddings(texts: list[str], api_key: str) -> list[list[float]]:
    # Reject malformed header values with a fixed message: urllib's own error
    # for embedded newlines can otherwise include the complete secret value.
    if not api_key or len(api_key) > 4096 or any(not 33 <= ord(character) <= 126 for character in api_key):
        raise ValueError("API key must be a non-empty printable ASCII token without whitespace")
    request = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=canonical_json({"model": EMBEDDING_MODEL, "input": texts, "encoding_format": "float"}),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Embedding API response exceeds the size limit")
    return parse_embedding_response(json.loads(body), len(texts))


def prepare(data_dir: Path, output: Path, api_key: str) -> dict[str, Any]:
    profiles, _ = load_catalog(data_dir)
    profiles = sorted(profiles, key=lambda profile: profile["id"])
    queries = query_inputs(profiles)
    texts = [profile_text(profile) for profile in profiles] + list(queries.values())
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        vectors.extend(fetch_embeddings(texts[start:start + BATCH_SIZE], api_key))
    profile_vectors = {profile["id"]: vector for profile, vector in zip(profiles, vectors[:len(profiles)])}
    query_vectors = dict(zip(queries, vectors[len(profiles):]))
    artifact = build_artifact(profiles, profile_vectors, query_vectors)
    # Do not publish a snapshot of files that changed while the API was running.
    current_profiles, _ = load_catalog(data_dir)
    if catalog_fingerprint(current_profiles) != artifact["catalog_version"]:
        raise RuntimeError("Catalog changed during preparation; rerun the command")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".embeddings-", suffix=".tmp", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(canonical_json(artifact))
            stream.write(b"\n")
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "backend" / "data")
    parser.add_argument("--output", type=Path, default=DEFAULT_EMBEDDINGS_PATH)
    parser.add_argument("--prompt-key", action="store_true", help="Read the API key privately from an interactive terminal")
    parser.add_argument("--dry-run", action="store_true", help="Show input counts without reading a key or contacting the API")
    args = parser.parse_args(argv)
    try:
        profiles, source = load_catalog(args.data_dir)
        count = len(profiles) + len(query_inputs(profiles))
        print(f"Catalog: {source}; profiles: {len(profiles)}; total texts: {count}; model: {EMBEDDING_MODEL}")
        if args.dry_run:
            print("Dry run: no API request made and no files changed.")
            return 0
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if args.prompt_key:
            if not sys.stdin.isatty():
                print("--prompt-key requires an interactive terminal. The key must not be echoed or passed as an argument.", file=sys.stderr)
                return 2
            # Refuse getpass's echoing fallback if this terminal cannot hide
            # input; an API key must never appear in terminal transcripts.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                api_key = getpass.getpass("OpenAI API key (hidden; used only for this run): ").strip()
        if not api_key:
            print("Set OPENAI_API_KEY or run --prompt-key in a terminal. No API request made.", file=sys.stderr)
            return 2
        artifact = prepare(args.data_dir, args.output, api_key)
        print(f"Saved {args.output}; catalog version: {artifact['catalog_version']}.")
        print("Restart the server to activate this fixed ranking snapshot.")
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Embedding API returned HTTP {exc.code}; the existing artifact was kept.", file=sys.stderr)
    except urllib.error.URLError:
        print("Embedding API could not be reached; the existing artifact was kept.", file=sys.stderr)
    except getpass.GetPassWarning:
        print("This terminal cannot hide input. Use a local interactive terminal; no key was read.", file=sys.stderr)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        # These errors describe local data and validation, never API headers or
        # response bodies. HTTP errors above deliberately expose only a status.
        print(f"Preparation failed: {exc}", file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Preparation cancelled; the existing artifact was kept.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
