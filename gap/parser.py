"""Accept Grok chat JSON. Survive Telegram splits, curly quotes, prose."""
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


class ParseError(ValueError):
    pass


def normalize_quotes(text: str) -> str:
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u00ab", '"')
        .replace("\u00bb", '"')
    )


def _extract_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def escape_controls_in_strings(text: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch in "\n\r\t":
                continue
            elif ord(ch) < 32:
                continue
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return "".join(out)


def collapse_ws(text: str) -> str:
    """Turn every newline/tab Telegram injected into a single space."""
    return re.sub(r"[\r\n\t]+", " ", text)


def strip_fences(raw: str) -> str:
    text = normalize_quotes(raw or "")
    text = re.sub(r"```(?:json)?", "", text, flags=re.I)
    return _extract_object(text.strip())


def _clean_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k).strip(): _clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_keys(v) for v in obj]
    if isinstance(obj, str):
        return obj.replace("\n", " ").replace("\r", " ").strip()
    return obj


def _try_load(blob: str) -> dict | None:
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return _clean_keys(data)
    return None


def load_json(raw: str) -> dict:
    """Parse Grok output even when Telegram split it across messages."""
    text = normalize_quotes(raw or "")
    candidates = []

    extracted = _extract_object(text)
    candidates.append(extracted)
    candidates.append(escape_controls_in_strings(extracted))
    # One-line dumps that Telegram wrapped: delete every raw newline.
    candidates.append(_extract_object(text.replace("\r", "").replace("\n", "")))

    seen: set[str] = set()
    last_err = "not valid JSON"
    for blob in candidates:
        if blob in seen:
            continue
        seen.add(blob)
        data = _try_load(blob)
        if data is not None:
            return data
        try:
            json.loads(blob)
        except json.JSONDecodeError as exc:
            last_err = str(exc)

    raise ParseError(f"not valid JSON: {last_err}")


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
        # Keys may have picked up a leading newline from a split: "\\nword"
        if "word" not in row:
            for k in list(row.keys()):
                if k.strip().lower() == "word":
                    row["word"] = row.pop(k)
                    break
        word = str(row.get("word") or "").strip()
        word = re.sub(r"\s+", " ", word)
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
        raise ParseError("price leakage in reasoning for: " + ", ".join(price_hits[:8]))

    if event_date and str(data.get("date") or "") not in ("", event_date):
        data["_date_mismatch"] = data.get("date")

    data["forecasts"] = [seen[w.lower()] for w in expected_words]
    return data
