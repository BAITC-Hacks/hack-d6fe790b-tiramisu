#!/usr/bin/env python3
"""Stdlib HTTP server for the EventMatch recommendation demo."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from recommendation_service import RecommendationService, load_catalog


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
FRONTEND_DIR = ROOT / "frontend"
MAX_BODY_BYTES = 32_768
PROFILES, CATALOG_SOURCE = load_catalog(DATA_DIR)
ENGINE = RecommendationService(PROFILES, CATALOG_SOURCE)


def options() -> dict[str, Any]:
    return ENGINE.options()


def recommend(payload: Any) -> tuple[dict[str, Any], int]:
    return ENGINE.recommend(payload)


class Handler(BaseHTTPRequestHandler):
    server_version = "EventMatchDemo/1.1"

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
        body, status = recommend(payload)
        self._json(status, body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EventMatch API listening at http://{host}:{port}")
    print(f"Profiles loaded: {len(PROFILES)} ({CATALOG_SOURCE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
