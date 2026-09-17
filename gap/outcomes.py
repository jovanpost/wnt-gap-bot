"""Official Kalshi yes/no by broadcast date. Source of truth for PnL."""
from __future__ import annotations

# Word said on WNT = "yes". Miss = "no".
OFFICIAL: dict[str, dict[str, str]] = {
    "2026-09-16": {
        "fed": "yes",
        "federal reserve": "yes",
        "interest rate": "yes",
        "inflation": "yes",
        "mortgage": "yes",
        "oil": "yes",
        "gas": "yes",
        "gasoline": "yes",
        "iran": "yes",
        "diesel": "yes",
        "saudi": "yes",
        "helicopter": "yes",
        "ed sheeran": "yes",
        "macklemore": "yes",
        "ai": "no",
        "artificial intelligence": "no",
        "kennedy": "no",
        "trump": "no",
        "yemen": "no",
        "houthi": "no",
        "canada": "no",
        "canadian": "no",
        "measles": "no",
        "kash": "no",
        "patel": "no",
        "supreme court": "no",
        "scotus": "no",
    },
}


def _tokens(word: str) -> list[str]:
    raw = (word or "").lower().replace("al /", "ai /")
    parts = []
    for chunk in raw.replace("/", " ").replace("(", " ").replace(")", " ").split():
        if chunk in ("times", "5+", "3+", "&"):
            continue
        parts.append(chunk)
    return parts


def official_for(date_str: str, word: str) -> str | None:
    table = OFFICIAL.get(str(date_str)[:10])
    if not table:
        return None
    blob = " ".join(_tokens(word))
    for key, val in table.items():
        if key in blob:
            return val
    return None
