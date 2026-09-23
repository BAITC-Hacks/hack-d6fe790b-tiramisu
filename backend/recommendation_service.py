"""Deterministic recommendation logic, isolated from HTTP delivery."""

from __future__ import annotations

import copy
import json
import math
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

try:
    from .ranking import DEFAULT_EMBEDDINGS_PATH, EMBEDDING_MODEL, RankingSnapshot, catalog_fingerprint
    from .explanations import DESCRIPTION_LABELS, description_excerpt, price_comparison
except ImportError:
    from ranking import DEFAULT_EMBEDDINGS_PATH, EMBEDDING_MODEL, RankingSnapshot, catalog_fingerprint
    from explanations import DESCRIPTION_LABELS, description_excerpt, price_comparison

DATE_MIN = date(2026, 9, 23)
DATE_MAX = date(2026, 12, 31)
REQUIRED_PROFILE_FIELDS = ("id", "anon_name", "categories", "city", "price_from_kzt", "event_formats", "languages", "max_hours", "busy_dates", "description", "synthetic", "city_imputed", "price_imputed")


def _error(path: Path, line: int, text: str) -> RuntimeError:
    return RuntimeError(f"Invalid profile in {path.name}, line {line}: {text}")


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value: int | float) -> str:
    return f"{value:,}".replace(",", " ").removesuffix(".0")


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
        if field != "busy_dates" and not value:
            raise _error(path, line, f"{field} must not be empty")
        if len(value) != len(set(value)):
            raise _error(path, line, f"{field} must not contain duplicates")
    price = profile["price_from_kzt"]
    if not _finite_number(price) or price < 0:
        raise _error(path, line, "price_from_kzt must be a non-negative finite number")
    hours = profile["max_hours"]
    if hours is not None and (not _finite_number(hours) or hours <= 0):
        raise _error(path, line, "max_hours must be null or a positive finite number")
    for field in ("synthetic", "city_imputed", "price_imputed"):
        if not isinstance(profile[field], bool):
            raise _error(path, line, f"{field} must be boolean")
    for busy_day in profile["busy_dates"]:
        try:
            parsed_day = date.fromisoformat(busy_day)
        except ValueError as exc:
            raise _error(path, line, f"busy_dates contains invalid date {busy_day!r}") from exc
        if busy_day != parsed_day.isoformat():
            raise _error(path, line, "busy_dates must use canonical YYYY-MM-DD dates")
        if not DATE_MIN <= parsed_day <= DATE_MAX:
            raise _error(path, line, f"busy_dates must be between {DATE_MIN} and {DATE_MAX}")
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
    ids = Counter(profile["id"] for profile in profiles)
    duplicate_ids = sorted(profile_id for profile_id, count in ids.items() if count > 1)
    if duplicate_ids:
        raise RuntimeError(f"Duplicate profile IDs: {', '.join(duplicate_ids)}")
    return profiles, source


class RecommendationService:
    def __init__(self, profiles: list[dict[str, Any]], catalog_source: str = "primary", *, embeddings_path: Path | None = DEFAULT_EMBEDDINGS_PATH) -> None:
        # Take a snapshot so caller mutation cannot silently alter eligibility
        # while keeping an old catalog version and old embedding vectors.
        self.profiles = copy.deepcopy(profiles)
        self.catalog_source = catalog_source
        self.catalog_version = catalog_fingerprint(self.profiles)
        self.ranking = RankingSnapshot(self.profiles, embeddings_path)

    def catalog_metadata(self) -> dict[str, Any]:
        synthetic_count = sum(profile["synthetic"] for profile in self.profiles)
        return {"source": self.catalog_source, "version": self.catalog_version, "total_profiles": len(self.profiles), "synthetic_profiles": synthetic_count, "non_synthetic_profiles": len(self.profiles) - synthetic_count}

    def options(self) -> dict[str, Any]:
        return {"cities": sorted({p["city"] for p in self.profiles}), "categories": sorted({c for p in self.profiles for c in p["categories"]}), "event_formats": sorted({e for p in self.profiles for e in p["event_formats"]}), "languages": sorted({language for p in self.profiles for language in p["languages"]}), "date_min": DATE_MIN.isoformat(), "date_max": DATE_MAX.isoformat(), "catalog": self.catalog_metadata(), "ranking": self.ranking.metadata()}

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
        if payload["date"] != requested_date.isoformat():
            return None, {"error": "invalid_request", "message": "Дата должна быть в формате YYYY-MM-DD."}
        if not DATE_MIN <= requested_date <= DATE_MAX:
            return None, {"error": "invalid_request", "message": f"Дата должна быть от {DATE_MIN.isoformat()} до {DATE_MAX.isoformat()}."}
        budget = payload["budget_kzt"]
        if not _finite_number(budget) or budget < 0:
            return None, {"error": "invalid_request", "message": "budget_kzt должен быть неотрицательным числом."}
        duration = payload.get("duration_hours")
        if duration is not None and (not _finite_number(duration) or duration <= 0 or duration > 24):
            return None, {"error": "invalid_request", "message": "duration_hours должен быть больше 0 и не больше 24."}
        language = payload.get("language")
        if language is not None and (not isinstance(language, str) or not language.strip()):
            return None, {"error": "invalid_request", "message": "language должен быть непустой строкой."}
        return {"city": payload["city"].strip(), "date": requested_date.isoformat(), "event_type": payload["event_type"].strip(), "category": payload["category"].strip(), "budget_kzt": budget, "duration_hours": duration, "language": language.strip() if language else None}, None

    @staticmethod
    def _description_evidence(request: dict[str, Any], profile: dict[str, Any], peers: list[dict[str, Any]]) -> str | None:
        return description_excerpt(request, profile, peers)

    def _evidence(self, request: dict[str, Any], profile: dict[str, Any], peers: list[dict[str, Any]]) -> list[dict[str, str]]:
        evidence = [
            {"kind": "availability", "label": "Дата", "value": f"По календарю свободен {request['date']}", "source": "busy_dates"},
            {"kind": "budget", "label": "Бюджет", "value": f"Начальная цена {_number(profile['price_from_kzt'])} ₸; бюджет {_number(request['budget_kzt'])} ₸", "source": "price_from_kzt"},
            {"kind": "format", "label": "Формат", "value": f"Берёт формат «{request['event_type']}»", "source": "event_formats"},
        ]
        if request["language"]:
            evidence.append({"kind": "language", "label": "Язык", "value": f"Работает на языке «{request['language']}»", "source": "languages"})
        if request["duration_hours"] is not None:
            if profile["max_hours"] is None:
                value = "Услуга не привязана к присутствию на площадке; ограничение по часам не применяется"
            else:
                value = f"Запрошено {_number(request['duration_hours'])} ч; максимум {_number(profile['max_hours'])} ч"
            evidence.append({"kind": "duration", "label": "Длительность", "value": value, "source": "max_hours"})
        excerpt = self._description_evidence(request, profile, peers)
        if excerpt:
            evidence.append({"kind": "description", "label": "Из описания", "value": excerpt, "source": "description"})
        comparison = price_comparison(profile, peers)
        if comparison:
            evidence.append({"kind": "comparison", "label": "Сравнение цены", "value": comparison, "source": "price_from_kzt"})
        return evidence

    @staticmethod
    def _explanation(request: dict[str, Any], profile: dict[str, Any], evidence: list[dict[str, str]]) -> str:
        details = [f"По календарю свободен {request['date']}", f"начальная цена {_number(profile['price_from_kzt'])} ₸ в пределах бюджета {_number(request['budget_kzt'])} ₸", f"берёт формат «{request['event_type']}»"]
        if request["language"]:
            details.append(f"язык — {request['language']}")
        if request["duration_hours"] is not None:
            if profile["max_hours"] is None:
                details.append("услуга не привязана к присутствию на площадке")
            else:
                details.append(f"запрошенные {_number(request['duration_hours'])} ч не превышают максимум {_number(profile['max_hours'])} ч")
        fact = next((item["value"] for item in evidence if item["kind"] == "description"), None)
        comparison = next((item["value"] for item in evidence if item["kind"] == "comparison"), None)
        specific = []
        if fact:
            specific.append(f"{DESCRIPTION_LABELS.get(request['category'], 'В описании услуги')}: «{fact}»")
        if comparison:
            specific.append(comparison if not specific else comparison[0].lower() + comparison[1:])
        explanation = f"{'; '.join(details)}."
        return explanation + (f" {'; '.join(specific)}." if specific else "")

    @staticmethod
    def _summary(request: dict[str, Any], candidate_count: int, eligible_count: int, counts: dict[str, int]) -> str:
        if not candidate_count:
            return f"В городе «{request['city']}» в каталоге нет категории «{request['category']}»."
        if not eligible_count:
            summary = f"В этой категории и городе есть кандидаты ({candidate_count}), но ни один не проходит условия."
        else:
            summary = f"Подходящих профилей: {eligible_count} из {candidate_count}; показано: {min(eligible_count, 3)}."
            if eligible_count < 3:
                summary += " Это все профили, которые проходят условия."
        reasons = [f"заняты на {request['date']} — {counts['busy']}"]
        for key, label in (("budget", "начальная цена выше бюджета"), ("format", "не берут этот формат"), ("language", "не работают на выбранном языке"), ("duration", "не подходят по длительности")):
            if counts[key]:
                reasons.append(f"{label} — {counts[key]}")
        return summary + " Причины исключения: " + "; ".join(reasons) + ". Для каждого исключённого профиля учтена первая причина."

    def recommend(self, payload: Any) -> tuple[dict[str, Any], int]:
        request, error = self._validate(payload)
        if error:
            return error, 400
        candidates = [p for p in self.profiles if p["city"] == request["city"] and request["category"] in p["categories"]]
        counts = {"busy": 0, "budget": 0, "format": 0, "language": 0, "duration": 0}
        eligible = []
        for profile in candidates:
            if request["date"] in profile["busy_dates"]:
                counts["busy"] += 1
            elif profile["price_from_kzt"] > request["budget_kzt"]:
                counts["budget"] += 1
            elif request["event_type"] not in profile["event_formats"]:
                counts["format"] += 1
            elif request["language"] and request["language"] not in profile["languages"]:
                counts["language"] += 1
            elif request["duration_hours"] is not None and profile["max_hours"] is not None and request["duration_hours"] > profile["max_hours"]:
                counts["duration"] += 1
            else:
                eligible.append(profile)
        scores = self.ranking.scores(request, eligible)
        eligible.sort(key=lambda p: (-scores[p["id"]], p["price_from_kzt"], p["id"]))
        cards = []
        for profile in eligible[:3]:
            evidence = self._evidence(request, profile, eligible[:3])
            cards.append({"id": profile["id"], "anon_name": profile["anon_name"], "category": request["category"], "city": profile["city"], "price_from_kzt": profile["price_from_kzt"], "synthetic": profile["synthetic"], "city_imputed": profile["city_imputed"], "price_imputed": profile["price_imputed"], "explanation": self._explanation(request, profile, evidence), "evidence": evidence})
        outcome = "recommended" if eligible else "no_eligible_candidates" if candidates else "no_category_in_city"
        return {"outcome": outcome, "message": self._summary(request, len(candidates), len(eligible), counts), "requested": request, "candidate_count": len(candidates), "total_eligible": len(eligible), "results": cards, "rejection_counts": counts, "availability_summary": {"date": request["date"], "busy_excluded": counts["busy"]}, "degraded": self.ranking.mode != "semantic", "catalog": self.catalog_metadata(), "ranking": self.ranking.metadata()}, 200
