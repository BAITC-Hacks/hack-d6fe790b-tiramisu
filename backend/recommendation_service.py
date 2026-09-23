"""Deterministic recommendation logic, isolated from HTTP delivery."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

DATE_MIN = date(2026, 9, 23)
DATE_MAX = date(2026, 12, 31)
EMBEDDING_MODEL = "text-embedding-3-small"
REQUIRED_PROFILE_FIELDS = ("id", "anon_name", "categories", "city", "price_from_kzt", "event_formats", "languages", "max_hours", "busy_dates", "description", "synthetic", "city_imputed", "price_imputed")


def _error(path: Path, line: int, text: str) -> RuntimeError:
    return RuntimeError(f"Invalid profile in {path.name}, line {line}: {text}")


def validate_profile(profile: Any, path: Path, line: int) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise _error(path, line, "record must be a JSON object")
    missing = [field for field in REQUIRED_PROFILE_FIELDS if field not in profile]
    if missing:
        raise _error(path, line, f"missing fields: {', '.join(missing)}")
    for field in ("id", "anon_name", "city", "description"):
        if not isinstance(profile[field], str) or not profile[field].strip():
            raise _error(path, line, f"{field} must be a non-empty string")
    for field in ("categories", "event_formats", "languages", "busy_dates"):
        value = profile[field]
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise _error(path, line, f"{field} must be an array of non-empty strings")
    price = profile["price_from_kzt"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
        raise _error(path, line, "price_from_kzt must be a non-negative finite number")
    hours = profile["max_hours"]
    if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0):
        raise _error(path, line, "max_hours must be null or a positive finite number")
    for field in ("synthetic", "city_imputed", "price_imputed"):
        if not isinstance(profile[field], bool):
            raise _error(path, line, f"{field} must be boolean")
    for busy_day in profile["busy_dates"]:
        try:
            date.fromisoformat(busy_day)
        except ValueError as exc:
            raise _error(path, line, f"busy_dates contains invalid date {busy_day!r}") from exc
    return profile


def load_profiles(path: Path) -> list[dict[str, Any]]:
    profiles = []
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                profiles.append(validate_profile(json.loads(line), path, line_no))
            except json.JSONDecodeError as exc:
                raise _error(path, line_no, f"invalid JSON: {exc.msg}") from exc
    if not profiles:
        raise RuntimeError(f"Profile catalog {path.name} is empty")
    return profiles


def load_catalog(data_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Use original data first; optional additions never alter original rows."""
    primary = data_dir / "hackathon-dataset-anonymized.jsonl"
    additions = data_dir / "synthetic-additions.jsonl"
    fallback = data_dir / "profiles.jsonl"
    if primary.is_file():
        profiles, source = load_profiles(primary), "primary"
        if additions.is_file():
            synthetic_profiles = load_profiles(additions)
            if any(not profile["synthetic"] for profile in synthetic_profiles):
                raise RuntimeError("Every profile in synthetic-additions.jsonl must set synthetic: true")
            profiles.extend(synthetic_profiles)
    elif fallback.is_file():
        profiles, source = load_profiles(fallback), "demo_fallback"
    else:
        raise RuntimeError("Missing profile catalog: add hackathon-dataset-anonymized.jsonl or profiles.jsonl")
    ids = [profile["id"] for profile in profiles]
    duplicate_ids = sorted({profile_id for profile_id in ids if ids.count(profile_id) > 1})
    if duplicate_ids:
        raise RuntimeError(f"Duplicate profile IDs: {', '.join(duplicate_ids)}")
    return profiles, source


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    return {word for word in words if len(word) > 2 and word not in {"для", "или", "это", "как", "что", "при", "the", "and", "with"}}


def _lexical_score(request: dict[str, Any], profile: dict[str, Any]) -> float:
    query = _tokens(f"{request['event_type']} {request['category']}")
    profile_text = _tokens(" ".join((profile["description"], *profile["categories"], *profile["event_formats"])))
    return len(query & profile_text) / len(query | profile_text) if query else 0.0


class RecommendationService:
    def __init__(self, profiles: list[dict[str, Any]], catalog_source: str = "primary") -> None:
        self.profiles = profiles
        self.catalog_source = catalog_source
        self._embedding_cache: dict[str, list[float]] = {}

    def options(self) -> dict[str, Any]:
        return {"cities": sorted({p["city"] for p in self.profiles}), "categories": sorted({c for p in self.profiles for c in p["categories"]}), "event_formats": sorted({e for p in self.profiles for e in p["event_formats"]}), "languages": sorted({language for p in self.profiles for language in p["languages"]}), "date_min": DATE_MIN.isoformat(), "date_max": DATE_MAX.isoformat()}

    def _validate(self, payload: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not isinstance(payload, dict):
            return None, {"error": "invalid_request", "message": "Ожидается JSON-объект."}
        required = ("city", "date", "event_type", "category", "budget_kzt")
        if any(key not in payload for key in required):
            return None, {"error": "invalid_request", "message": "Нужны city, date, event_type, category и budget_kzt."}
        for key in ("city", "date", "event_type", "category"):
            if not isinstance(payload[key], str) or not payload[key].strip():
                return None, {"error": "invalid_request", "message": f"Поле {key} должно быть непустой строкой."}
        try:
            requested_date = date.fromisoformat(payload["date"])
        except ValueError:
            return None, {"error": "invalid_request", "message": "Дата должна быть в формате YYYY-MM-DD."}
        if not DATE_MIN <= requested_date <= DATE_MAX:
            return None, {"error": "invalid_request", "message": f"Дата должна быть от {DATE_MIN.isoformat()} до {DATE_MAX.isoformat()}."}
        budget = payload["budget_kzt"]
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0:
            return None, {"error": "invalid_request", "message": "budget_kzt должен быть неотрицательным числом."}
        duration = payload.get("duration_hours")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0 or duration > 24):
            return None, {"error": "invalid_request", "message": "duration_hours должен быть числом от 0 до 24."}
        language = payload.get("language")
        if language is not None and (not isinstance(language, str) or not language.strip()):
            return None, {"error": "invalid_request", "message": "language должен быть непустой строкой."}
        return {"city": payload["city"].strip(), "date": requested_date.isoformat(), "event_type": payload["event_type"].strip(), "category": payload["category"].strip(), "budget_kzt": budget, "duration_hours": duration, "language": language.strip() if language else None}, None

    def _embeddings(self, texts: list[str]) -> list[list[float]]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        missing = [text for text in texts if text not in self._embedding_cache]
        if missing:
            body = json.dumps({"model": EMBEDDING_MODEL, "input": missing}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            for item in sorted(result["data"], key=lambda item: item["index"]):
                self._embedding_cache[missing[item["index"]]] = item["embedding"]
        return [self._embedding_cache[text] for text in texts]

    def _scores(self, request: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[dict[str, float], bool]:
        if not candidates:
            return {}, False
        query = f"Мероприятие: {request['event_type']}. Категория подрядчика: {request['category']}."
        texts = [query] + [" ".join((p["description"], *p["categories"], *p["event_formats"])) for p in candidates]
        try:
            vectors = self._embeddings(texts)
            query_norm = math.sqrt(sum(v * v for v in vectors[0]))
            scores = {}
            for index, profile in enumerate(candidates, 1):
                vector = vectors[index]
                norm = math.sqrt(sum(v * v for v in vector))
                scores[profile["id"]] = sum(a * b for a, b in zip(vectors[0], vector)) / (query_norm * norm) if query_norm and norm else 0.0
            return scores, False
        except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, urllib.error.URLError):
            return {p["id"]: _lexical_score(request, p) for p in candidates}, True

    @staticmethod
    def _fact(profile: dict[str, Any]) -> str:
        fact = re.split(r"(?<=[.!?])\s+", profile["description"].strip(), maxsplit=1)[0].rstrip(".!? ")
        return fact[:157].rstrip() + "…" if len(fact) > 160 else fact

    def _explanation(self, request: dict[str, Any], profile: dict[str, Any]) -> str:
        evidence = [f"Цена от {profile['price_from_kzt']:,.0f} ₸ укладывается в бюджет {request['budget_kzt']:,.0f} ₸".replace(",", " "), f"подрядчик берёт формат «{request['event_type']}»"]
        if request["language"]:
            evidence.append(f"работает на языке «{request['language']}»")
        if request["duration_hours"] is not None and profile["max_hours"] is not None:
            evidence.append(f"доступен на площадке до {profile['max_hours']} ч")
        fact = self._fact(profile)
        return f"{'; '.join(evidence)}. В описании: «{fact}»." if fact else f"{'; '.join(evidence)}."

    def recommend(self, payload: Any) -> tuple[dict[str, Any], int]:
        request, error = self._validate(payload)
        if error:
            return error, 400
        candidates = [p for p in self.profiles if p["city"] == request["city"] and request["category"] in p["categories"]]
        counts = {"busy": 0, "budget": 0, "format": 0, "language": 0, "duration": 0}
        if not candidates:
            return {"outcome": "no_category_in_city", "requested": request, "total_eligible": 0, "results": [], "rejection_counts": counts, "availability_summary": {"date": request["date"], "busy_excluded": 0}, "degraded": False}, 200
        eligible = []
        for profile in candidates:
            if request["date"] in profile["busy_dates"]: counts["busy"] += 1
            elif profile["price_from_kzt"] > request["budget_kzt"]: counts["budget"] += 1
            elif request["event_type"] not in profile["event_formats"]: counts["format"] += 1
            elif request["language"] and request["language"] not in profile["languages"]: counts["language"] += 1
            elif request["duration_hours"] is not None and profile["max_hours"] is not None and request["duration_hours"] > profile["max_hours"]: counts["duration"] += 1
            else: eligible.append(profile)
        scores, degraded = self._scores(request, eligible)
        eligible.sort(key=lambda p: (-scores[p["id"]], p["price_from_kzt"], p["id"]))
        cards = [{"id": p["id"], "anon_name": p["anon_name"], "category": request["category"], "city": p["city"], "price_from_kzt": p["price_from_kzt"], "synthetic": p["synthetic"], "explanation": self._explanation(request, p)} for p in eligible[:3]]
        return {"outcome": "recommended" if eligible else "no_eligible_candidates", "requested": request, "total_eligible": len(eligible), "results": cards, "rejection_counts": counts, "availability_summary": {"date": request["date"], "busy_excluded": counts["busy"]}, "degraded": degraded}, 200
