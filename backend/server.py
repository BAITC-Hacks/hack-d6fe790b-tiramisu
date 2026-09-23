#!/usr/bin/env python3
"""Small stdlib-only recommendation API and static-file server for the demo."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = Path(__file__).resolve().parent / "data" / "profiles.jsonl"
FRONTEND_DIR = ROOT / "frontend"
DATE_MIN = date(2026, 9, 23)
DATE_MAX = date(2026, 12, 31)
EMBEDDING_MODEL = "text-embedding-3-small"
MAX_BODY_BYTES = 32_768


def load_profiles() -> list[dict[str, Any]]:
    profiles = []
    with DATA_PATH.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                profile = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"Invalid JSONL at line {line_number}: {error}") from error
            required = ("id", "anon_name", "categories", "city", "price_from_kzt", "event_formats", "languages", "max_hours", "busy_dates", "description", "synthetic")
            missing = [key for key in required if key not in profile]
            if missing:
                raise RuntimeError(f"Profile line {line_number} is missing: {', '.join(missing)}")
            profiles.append(profile)
    return profiles


PROFILES = load_profiles()
EMBEDDING_CACHE: dict[str, list[float]] = {}


def options() -> dict[str, Any]:
    return {
        "cities": sorted({p["city"] for p in PROFILES}),
        "categories": sorted({category for p in PROFILES for category in p["categories"]}),
        "event_formats": sorted({event for p in PROFILES for event in p["event_formats"]}),
        "languages": sorted({language for p in PROFILES for language in p["languages"]}),
        "date_min": DATE_MIN.isoformat(),
        "date_max": DATE_MAX.isoformat(),
    }


def tokenize(text: str) -> set[str]:
    # Stable lexical fallback: Unicode words, normalized case, common Russian endings trimmed.
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    stop = {"для", "или", "это", "как", "что", "при", "над", "под", "the", "and", "with"}
    tokens = set()
    for word in words:
        if len(word) > 5 and re.search(r"(ами|ями|ого|ему|ыми|ими|ать|ять|ить|ться|ого|его|ов|ев|ей|ам|ям|ах|ях|ый|ий|ая|яя|ое|ее|ы|и|а|я)$", word):
            word = re.sub(r"(ами|ями|ого|ему|ыми|ими|ать|ять|ить|ться|ов|ев|ей|ам|ям|ах|ях|ый|ий|ая|яя|ое|ее|ы|и|а|я)$", "", word)
        if len(word) > 2 and word not in stop:
            tokens.add(word)
    return tokens


def lexical_score(request: dict[str, Any], profile: dict[str, Any]) -> float:
    query = tokenize(" ".join((request["event_type"], request["category"])))
    description = tokenize(" ".join((profile["description"], *profile["categories"], *profile["event_formats"])))
    if not query:
        return 0.0
    return len(query & description) / len(query | description)


def _embedding(texts: list[str]) -> list[list[float]]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    missing = [text for text in texts if text not in EMBEDDING_CACHE]
    if missing:
        body = json.dumps({"model": EMBEDDING_MODEL, "input": missing}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/embeddings", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        for item in sorted(result["data"], key=lambda item: item["index"]):
            EMBEDDING_CACHE[missing[item["index"]]] = item["embedding"]
    return [EMBEDDING_CACHE[text] for text in texts]


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def _profile_text(profile: dict[str, Any]) -> str:
    return " ".join((profile["description"], *profile["categories"], *profile["event_formats"]))


def _scores(request: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[dict[str, float], bool]:
    if not candidates:
        return {}, False
    query = f"Мероприятие: {request['event_type']}. Категория подрядчика: {request['category']}."
    texts = [query] + [_profile_text(profile) for profile in candidates]
    try:
        vectors = _embedding(texts)
        return {p["id"]: cosine(vectors[0], vectors[index + 1]) for index, p in enumerate(candidates)}, False
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, urllib.error.URLError):
        return {p["id"]: lexical_score(request, p) for p in candidates}, True


def _validate(payload: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
    return {
        "city": payload["city"].strip(), "date": requested_date.isoformat(),
        "event_type": payload["event_type"].strip(), "category": payload["category"].strip(),
        "budget_kzt": budget, "duration_hours": duration, "language": language.strip() if language else None,
    }, None


def _description_fact(profile: dict[str, Any]) -> str:
    description = profile["description"].strip()
    sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].rstrip(".!? ")
    if len(sentence) > 160:
        sentence = sentence[:157].rstrip() + "…"
    return sentence


def _explanation(request: dict[str, Any], profile: dict[str, Any]) -> str:
    reasons = []
    price = profile["price_from_kzt"]
    if price <= request["budget_kzt"]:
        reasons.append(f"цена от {price:,} ₸ укладывается в бюджет {request['budget_kzt']:,.0f} ₸".replace(",", " "))
    reasons.append(f"берёт формат «{request['event_type']}»")
    if request["language"]:
        reasons.append(f"работает на языке «{request['language']}»")
    if request["duration_hours"] is not None and profile["max_hours"] is not None:
        reasons.append(f"допускает до {profile['max_hours']} ч на площадке")
    matching_conditions = "; ".join(reasons)
    description_fact = _description_fact(profile)
    if description_fact:
        return f"{matching_conditions.capitalize()}. В описании: «{description_fact}»."
    return matching_conditions.capitalize() + "."


def recommend(payload: Any) -> tuple[dict[str, Any], int]:
    request, error = _validate(payload)
    if error:
        return error, 400
    city_category = [p for p in PROFILES if p["city"] == request["city"] and request["category"] in p["categories"]]
    if not city_category:
        return {"outcome": "no_category_in_city", "requested": request, "total_eligible": 0, "results": [], "rejection_counts": {"busy": 0, "budget": 0, "format": 0, "language": 0, "duration": 0}, "degraded": False}, 200

    counts = {"busy": 0, "budget": 0, "format": 0, "language": 0, "duration": 0}
    eligible = []
    for profile in city_category:
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

    scores, degraded = _scores(request, eligible)
    eligible.sort(key=lambda p: (-scores[p["id"]], p["price_from_kzt"], p["id"]))
    result_cards = [{
        "id": p["id"], "anon_name": p["anon_name"], "category": request["category"],
        "city": p["city"], "price_from_kzt": p["price_from_kzt"], "synthetic": bool(p["synthetic"]),
        "explanation": _explanation(request, p),
    } for p in eligible[:3]]
    return {
        "outcome": "recommended" if eligible else "no_eligible_candidates",
        "requested": request, "total_eligible": len(eligible), "results": result_cards,
        "rejection_counts": counts, "degraded": degraded,
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "EventMatchDemo/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/options":
            return self._json(200, options())
        if path == "/" or path.startswith("/frontend/"):
            if not FRONTEND_DIR.is_dir():
                return self._send(503, b"Frontend files are not available yet. API: /api/options and /api/recommendations", "text/plain; charset=utf-8")
            relative = "index.html" if path == "/" else unquote(path.removeprefix("/frontend/"))
            target = (FRONTEND_DIR / relative).resolve()
            if FRONTEND_DIR.resolve() not in target.parents and target != FRONTEND_DIR.resolve():
                return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            if not target.is_file():
                return self._send(404, b"Not found", "text/plain; charset=utf-8")
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}.get(target.suffix.lower(), "application/octet-stream")
            return self._send(200, target.read_bytes(), mime)
        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/recommendations":
            return self._json(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._json(413, {"error": "invalid_body_size", "message": "Размер JSON должен быть от 1 до 32768 байт."})
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "invalid_json", "message": "Тело запроса должно быть корректным JSON."})
        response, status = recommend(payload)
        self._json(status, response)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EventMatch API listening at http://{host}:{port}")
    print(f"Profiles loaded: {len(PROFILES)} (all demo profiles are synthetic)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
