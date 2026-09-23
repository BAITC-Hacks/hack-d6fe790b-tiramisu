"""Versioned, network-free ranking for the recommendation request path.

Embeddings are prepared explicitly by scripts/prepare_embeddings.py. A service
keeps one validated snapshot for its whole lifetime, including during outages.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

EMBEDDING_MODEL = "text-embedding-3-small"
ARTIFACT_SCHEMA_VERSION = 1
LEXICAL_VERSION = "lexical-evidence-v2"
DEFAULT_EMBEDDINGS_PATH = Path(__file__).resolve().parent / "data" / "embeddings.json"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024

# Small explicit fallback vocabulary, not trained data or a substitute AI model.
# Counting distinct matching concepts avoids penalizing a longer description.
EVENT_CONCEPTS = {
    "свадьба": ("свад", "невест", "жених", "брак", "церемон", "романтич", "love"),
    "той": ("той", "казах", "традиц", "домбр", "националь", "дастархан"),
    "корпоратив": ("корпорат", "команд", "бренд", "делов", "коллег", "бизнес"),
    "конференция": ("конферен", "делов", "спикер", "презентац", "форум", "конгресс"),
    "юбилей": ("юбил", "семейн", "поколен", "архив", "истори", "поздрав"),
    "день рождения": ("рожден", "именин", "семейн", "детск", "праздн", "поздрав"),
}
STOP_WORDS = {"для", "или", "это", "как", "что", "при", "the", "and", "with"}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def catalog_fingerprint(profiles: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(sorted(profiles, key=lambda profile: profile["id"]))).hexdigest()


def query_key(event_type: str, category: str) -> str:
    return json.dumps([event_type, category], ensure_ascii=False, separators=(",", ":"))


def query_text(event_type: str, category: str) -> str:
    return f"Мероприятие: {event_type}. Категория подрядчика: {category}."


def profile_text(profile: dict[str, Any]) -> str:
    return " ".join((profile["description"], *profile["categories"], *profile["event_formats"]))


def query_inputs(profiles: list[dict[str, Any]]) -> dict[str, str]:
    categories = sorted({category for profile in profiles for category in profile["categories"]})
    formats = sorted({event for profile in profiles for event in profile["event_formats"]})
    return {query_key(event, category): query_text(event, category) for event in formats for category in categories}


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    return {word for word in words if len(word) > 2 and word not in STOP_WORDS}


def text_relevance(event_type: str, category: str, text: str) -> float:
    words = tokens(text)
    query_words = tokens(f"{event_type} {category}")
    coverage = len(query_words & words) / len(query_words) if query_words else 0.0
    concepts = EVENT_CONCEPTS.get(event_type, ())
    matches = sum(any(word.startswith(stem) for word in words) for stem in concepts)
    concept_coverage = matches / len(concepts) if concepts else 0.0
    return coverage + concept_coverage


def lexical_score(request: dict[str, Any], profile: dict[str, Any]) -> float:
    # Eligibility already established category and format support. Rank the
    # description, so ubiquitous structured tags do not drown out distinctions.
    return text_relevance(request["event_type"], request["category"], profile["description"])


def validate_vector(value: Any, dimension: int | None = None) -> list[float]:
    if not isinstance(value, list) or not value or len(value) > 4096:
        raise ValueError("An embedding must be a non-empty vector of at most 4096 values")
    if dimension is not None and len(value) != dimension:
        raise ValueError("Embedding dimensions differ")
    try:
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
            raise ValueError("Embedding values must be numbers")
        vector = [float(item) for item in value]
    except OverflowError as exc:
        raise ValueError("Embedding value is too large") from exc
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("Embedding values must be finite")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding norm must be positive and finite")
    return vector


def normalized_vector(value: Any, dimension: int | None = None) -> list[float]:
    vector = validate_vector(value, dimension)
    norm = math.hypot(*vector)
    return [number / norm for number in vector]


def build_artifact(profiles: list[dict[str, Any]], profile_vectors: dict[str, list[float]], query_vectors: dict[str, list[float]]) -> dict[str, Any]:
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model": EMBEDDING_MODEL,
        "catalog_version": catalog_fingerprint(profiles),
        "profiles": profile_vectors,
        "queries": query_vectors,
    }
    validate_artifact(artifact, profiles)
    return artifact


def validate_artifact(artifact: Any, profiles: list[dict[str, Any]]) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    if not isinstance(artifact, dict):
        raise ValueError("Artifact must be an object")
    if type(artifact.get("schema_version")) is not int or artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("Unsupported artifact schema")
    if artifact.get("model") != EMBEDDING_MODEL:
        raise ValueError("Unexpected embedding model")
    if artifact.get("catalog_version") != catalog_fingerprint(profiles):
        raise ValueError("Artifact belongs to a different catalog")
    profile_vectors, query_vectors = artifact.get("profiles"), artifact.get("queries")
    if not isinstance(profile_vectors, dict) or set(profile_vectors) != {profile["id"] for profile in profiles}:
        raise ValueError("Artifact must contain every catalog profile exactly once")
    if not isinstance(query_vectors, dict) or set(query_vectors) != set(query_inputs(profiles)):
        raise ValueError("Artifact must contain every category and event combination")
    first = next(iter(profile_vectors.values()), None)
    dimension = len(validate_vector(first))
    prepared_profiles = {key: normalized_vector(vector, dimension) for key, vector in profile_vectors.items()}
    prepared_queries = {key: normalized_vector(vector, dimension) for key, vector in query_vectors.items()}
    return prepared_profiles, prepared_queries


class RankingSnapshot:
    """An immutable choice of ranking mode made once at application startup."""

    def __init__(self, profiles: list[dict[str, Any]], artifact_path: Path | None = DEFAULT_EMBEDDINGS_PATH) -> None:
        self._profiles: dict[str, list[float]] = {}
        self._queries: dict[str, list[float]] = {}
        self.mode = "lexical"
        self.model: str | None = None
        self.version = LEXICAL_VERSION
        self.artifact_status = "disabled" if artifact_path is None else "missing"
        if artifact_path is None:
            return
        path = Path(artifact_path)
        try:
            if not path.is_file():
                return
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("Embedding artifact exceeds the size limit")
            artifact = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(artifact, dict) and artifact.get("catalog_version") != catalog_fingerprint(profiles):
                self.artifact_status = "stale"
                return
            self._profiles, self._queries = validate_artifact(artifact, profiles)
            self.version = hashlib.sha256(canonical_json(artifact)).hexdigest()
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            self.artifact_status = "invalid"
            return
        self.mode = "semantic"
        self.model = EMBEDDING_MODEL
        self.artifact_status = "ready"

    def metadata(self) -> dict[str, Any]:
        return {"mode": self.mode, "version": self.version, "model": self.model, "artifact_status": self.artifact_status}

    def scores(self, request: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, float]:
        query = self._queries.get(query_key(request["event_type"], request["category"]))
        if self.mode == "semantic" and query is not None:
            return {profile["id"]: math.fsum(left * right for left, right in zip(query, self._profiles[profile["id"]])) for profile in candidates}
        return {profile["id"]: lexical_score(request, profile) for profile in candidates}
