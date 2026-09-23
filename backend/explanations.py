"""Select useful, traceable description excerpts without inventing benefits.

Ranking and eligibility are separate. These rules choose what to explain, not
which vendors to return. A sentence about another format is never used as
evidence for the requested format merely because it mentions 'family'.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from .ranking import tokens
except ImportError:
    from ranking import tokens

FORMAT_STEMS = {
    "свадьба": ("свад", "невест", "жених", "молодож", "бракосоч", "клятв"),
    "той": ("той", "тоев", "тои", "тоя", "тою", "тоях"),
    "корпоратив": ("корпорат",),
    "конференция": ("конферен", "форум", "спикер"),
    "юбилей": ("юбил",),
    "день рождения": ("рожден", "именин"),
}
FEATURE_STEMS = {
    "Ведущий": ("программ", "интерактив", "координир", "координац", "конкурс", "благослов", "истори", "персональ", "перевож", "тост", "викторин", "тайминг", "сценар"),
    "Фотограф": ("репортаж", "портрет", "постанов", "камер", "кадр", "анонс", "переда", "отда", "цветокорр", "свет"),
    "Банкетный зал": ("сцен", "экран", "проектор", "террас", "сад", "зон", "вместим", "рассад", "звук", "включ", "гример"),
    "Декоратор": ("цвет", "композиц", "фотозон", "сервиров", "сцен", "ткан", "свет"),
    "Флорист": ("букет", "композиц", "цвет", "палитр", "монтаж", "сезон"),
    "Кейтеринг": ("меню", "пакет", "фуршет", "кофе", "обслужив"),
    "Музыкант": ("репертуар", "акустич", "вокал", "композиц", "звук", "оборудован", "жив"),
    "Инструменталист": ("саксофон", "домбр", "клавиш", "гитар", "фон", "репертуар", "номер"),
    "Видеооператор": ("ролик", "фильм", "поздравлен", "репортаж", "публикац", "тайминг"),
    "Ведущий церемонии": ("сценар", "двуязыч", "клятв", "перевод", "традиц"),
    "Фото и видеобудки": ("печат", "реквизит", "галере", "ролик", "поздравлен", "рамк"),
    "Подарки и сувениры": ("набор", "надпис", "упаков", "карточк", "логотип", "персональ"),
    "Отель": ("размещ", "номер", "прожив", "ресторан", "кофе", "ужин"),
}
DESCRIPTION_LABELS = {
    "Ведущий": "В описании программы",
    "Ведущий церемонии": "В описании церемонии",
    "Фотограф": "В описании съёмки",
    "Видеооператор": "В описании съёмки",
    "Банкетный зал": "В описании площадки",
    "Отель": "В описании площадки",
}


def _formats(words: set[str]) -> set[str]:
    return {event for event, stems in FORMAT_STEMS.items() if any(word.startswith(stem) for stem in stems for word in words)}


def description_excerpt(request: dict[str, Any], profile: dict[str, Any], peers: list[dict[str, Any]]) -> str | None:
    # Keep whole sentences: truncation can discard a condition or negation.
    sentences = [part.rstrip(".!? ") for part in re.split(r"(?<=[.!?])\s+", profile["description"].strip()) if part.strip(".!? ")]
    other_words = set().union(*(tokens(peer["description"]) for peer in peers if peer["id"] != profile["id"]))
    features = FEATURE_STEMS.get(request["category"], ())
    candidates = []
    for index, sentence in enumerate(sentences):
        if len(sentence) > 240:
            continue
        words = tokens(sentence)
        formats = _formats(words)
        if formats and request["event_type"] not in formats:
            continue
        feature_count = sum(any(word.startswith(stem) for word in words) for stem in features)
        distinct_count = len(words - other_words)
        # A concrete service detail carries more decision value than a list of
        # supported formats already checked in event_formats.
        score = (feature_count, request["event_type"] in formats, distinct_count, -index)
        candidates.append((score, sentence))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def price_comparison(profile: dict[str, Any], peers: list[dict[str, Any]]) -> str | None:
    others = [peer for peer in peers if peer["id"] != profile["id"]]
    if others and all(profile["price_from_kzt"] < peer["price_from_kzt"] for peer in others):
        return f"Самая низкая начальная цена среди {len(others) + 1} показанных вариантов"
    return None
