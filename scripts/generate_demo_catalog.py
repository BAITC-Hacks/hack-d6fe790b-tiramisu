#!/usr/bin/env python3
"""Rebuild the synthetic demo calendar deterministically.

This script never reads or changes the organizer's original JSONL dataset.
It transforms only the repository's synthetic fallback catalog.  Provider
identity, category, city and descriptions are retained, except for two
legacy catering prices that were per guest and are normalised to packages.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any


SEED = "tiramisu-demo-calendar-v1"
YEAR = 2026
# Sep only covers the request window 23--30. Counts correspond to 37.5%,
# 38.7%, 40.0% and 74.2% busy occupancy respectively.
MONTH_TARGETS = {9: 3, 10: 12, 11: 12, 12: 23}
PACKAGE_FIXES = {
    "demo-007": (285_000, "Выездной кофе-брейк и фуршет для конференций и корпоративных встреч. Цена указана за пакет обслуживания мероприятия; меню согласуется заранее."),
    "demo-014": (360_000, "Банкетное обслуживание и фуршеты для свадеб и деловых мероприятий. Цена указана за пакет обслуживания мероприятия; состав меню согласуется заранее."),
}


def _days_for_month(month: int) -> list[date]:
    first = date(YEAR, month, 23) if month == 9 else date(YEAR, month, 1)
    next_month = date(YEAR + 1, 1, 1) if month == 12 else date(YEAR, month + 1, 1)
    return [first + timedelta(days=offset) for offset in range((next_month - first).days)]


def _pick_busy_days(profile_id: str, month: int) -> list[str]:
    """Pick a stable set; December weekends are intentionally more likely."""
    candidates = _days_for_month(month)
    target = MONTH_TARGETS[month]
    rng = random.Random(f"{SEED}:{profile_id}:{month}")
    selected: list[date] = []
    while len(selected) < target:
        weights = [5 if month == 12 and item.weekday() >= 5 else 1 for item in candidates]
        chosen = rng.choices(candidates, weights=weights, k=1)[0]
        selected.append(chosen)
        candidates.remove(chosen)
    return [item.isoformat() for item in sorted(selected)]


def rebuild(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(profile.get("synthetic") is not True for profile in profiles):
        raise ValueError("The demo generator accepts only records explicitly marked synthetic: true")
    rebuilt = []
    for source in profiles:
        profile = dict(source)
        profile["busy_dates"] = [day for month in MONTH_TARGETS for day in _pick_busy_days(profile["id"], month)]
        if profile["id"] in PACKAGE_FIXES:
            profile["price_from_kzt"], profile["description"] = PACKAGE_FIXES[profile["id"]]
        rebuilt.append(profile)
    return rebuilt


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, profiles: list[dict[str, Any]]) -> None:
    content = "\n".join(json.dumps(profile, ensure_ascii=False, separators=(",", ":")) for profile in profiles) + "\n"
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the synthetic Tiramisu demo catalog.")
    parser.add_argument("--input", type=Path, default=Path("backend/data/profiles.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("backend/data/profiles.jsonl"))
    args = parser.parse_args()
    protected_name = "hackathon-dataset-anonymized.jsonl"
    if args.input.name == protected_name or args.output.name == protected_name:
        raise SystemExit("Refusing to read or write the organizer dataset; this generator is only for profiles.jsonl.")
    write_jsonl(args.output, rebuild(load_jsonl(args.input)))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
