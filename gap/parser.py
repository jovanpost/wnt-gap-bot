"""Accept Grok chat JSON. Reject price leakage and missing words."""
from __future__ import annotations

import json
import re
from typing import Any

# Kalshi tape only. Do not flag oil/$107 or $5,000 dividend copy.
PRICE_RE = re.compile(
    r"(¢|"
    r"\b\d{1,2}\s*c(?:ents)?\b|"
    r"\byes\s*bid\b|\bno\s*ask\b|"
    r"\bbid\s*[:@]|\bask\s*[:@]|"
    r"kalshi\.com|"
    r"\blimit\s+\d{1,2}\b)",
    re.I,
)


def normalize_quotes(text: str) -> str:
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


class ParseError(ValueError):
    pass


def strip_fences(raw: str) -> str:
    text = normalize_quotes(raw.strip())
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    # Grok wraps JSON in research commentary. Pull the outermost object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def load_json(raw: str) -> dict:
    blob = strip_fences(raw)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("top level must be a JSON object")
    return data


def validate(raw: str, expected_words: list[str], event_date: str | None = None) -> dict[str, Any]:
    data = load_json(raw)
    forecasts = data.get("forecasts")
    if not isinstance(forecasts, list) or not forecasts:
        raise ParseError("missing forecasts[]")

    expected_map = {w.lower(): w for w in expected_words}
    seen: dict[str, dict] = {}
    price_hits: list[str] = []

    for i, row in enumerate(forecasts):
        if not isinstance(row, dict):
            raise ParseError(f"forecasts[{i}] is not an object")
        word = str(row.get("word") or "").strip()
        if not word:
            raise ParseError(f"forecasts[{i}] missing word")
        key = word.lower()
        if key not in expected_map:
            raise ParseError(f"unexpected word {word!r} (not on tonight's list)")
        if key in seen:
            raise ParseError(f"duplicate word {word!r}")

        prob = row.get("probability")
        try:
            prob_i = int(prob)
        except (TypeError, ValueError) as exc:
            raise ParseError(f"{word}: probability must be int 1-99") from exc
        if not 1 <= prob_i <= 99:
            raise ParseError(f"{word}: probability {prob_i} not in 1-99")
        row = dict(row)
        row["probability"] = prob_i
        row["word"] = expected_map[key]

        reason = str(row.get("reasoning") or "")
        if PRICE_RE.search(reason) or PRICE_RE.search(word):
            price_hits.append(word)
        seen[key] = row

    missing = [w for w in expected_words if w.lower() not in seen]
    if missing:
        raise ParseError(f"missing {len(missing)} word(s): {', '.join(missing[:8])}")

    if price_hits:
        raise ParseError(
            "price leakage in reasoning for: " + ", ".join(price_hits[:8])
        )

    if event_date and str(data.get("date") or "") not in ("", event_date):
        # Warn-level: keep going but surface it.
        data["_date_mismatch"] = data.get("date")

    data["forecasts"] = [seen[w.lower()] for w in expected_words]
    return data
