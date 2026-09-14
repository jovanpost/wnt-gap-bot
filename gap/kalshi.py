"""Kalshi REST client — read path only for Phase 1 (events, markets, quotes)."""
from __future__ import annotations

import base64
import logging
import random
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config as C

log = logging.getLogger("gap.kalshi")


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str, endpoint: str):
        self.status = status
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"{endpoint} -> {status}: {body[:300]}")


def _to_count(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if d != d.to_integral_value() or (0 < abs(d) < 1):
        return int((d * 100).to_integral_value())
    return int(d)


class KalshiClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_pem: str | None = None,
        private_key_path: str | None = None,
        base_url: str | None = None,
    ):
        self.key_id = key_id if key_id is not None else C.KALSHI_KEY_ID
        self.base_url = (base_url or C.BASE_URL).rstrip("/")
        self._key = self._load_key(
            private_key_pem if private_key_pem is not None else C.KALSHI_PRIVATE_KEY_PEM,
            private_key_path if private_key_path is not None else C.KALSHI_PRIVATE_KEY_PATH,
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": C.USER_AGENT})

    @staticmethod
    def _load_key(pem_text: str, pem_path: str):
        raw = None
        if pem_text and "BEGIN" in pem_text:
            raw = pem_text.replace("\\n", "\n").strip().encode()
        elif pem_path:
            try:
                with open(pem_path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                log.warning("could not read private key at %s: %s", pem_path, exc)
        if raw is None:
            return None
        try:
            return serialization.load_pem_private_key(raw, password=None)
        except Exception as exc:
            log.error("private key failed to parse: %s", exc)
            return None

    @property
    def authenticated(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def _headers(self, method: str, sign_path: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if not self.authenticated:
            return headers
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + sign_path.split("?")[0]).encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        headers.update({
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        })
        return headers

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body: dict | None = None,
        auth: bool = True,
        retries: int = 4,
        timeout: int = 20,
    ) -> dict:
        sign_path = C.API_ROOT + endpoint
        url = self.base_url + sign_path
        last: Exception | None = None

        for attempt in range(retries + 1):
            headers = self._headers(method, sign_path) if auth else {
                "Content-Type": "application/json", "User-Agent": C.USER_AGENT,
            }
            try:
                resp = self.session.request(
                    method, url, params=params, json=body,
                    headers=headers, timeout=timeout,
                )
            except requests.RequestException as exc:
                last = exc
                if attempt >= retries:
                    raise
                time.sleep(min(2 ** attempt, 8) + random.random())
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last = KalshiError(resp.status_code, resp.text, endpoint)
                if attempt >= retries:
                    raise last
                time.sleep(min(0.25 * (2 ** attempt), 5) + random.random() * 0.25)
                continue

            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, endpoint)

            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise last or RuntimeError("unreachable")

    def paginate(self, endpoint: str, key: str, params: dict | None = None,
                 auth: bool = True, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            data = self.request("GET", endpoint, params=page_params, auth=auth)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        return self.paginate(
            "/events", "events",
            {"series_ticker": series_ticker, "status": status, "limit": 200},
            auth=False,
        )

    def get_markets(self, event_ticker: str) -> list[dict]:
        return self.paginate(
            "/markets", "markets",
            {"event_ticker": event_ticker, "limit": 200},
            auth=False,
        )

    def get_market(self, ticker: str) -> dict:
        return (self.request("GET", f"/markets/{ticker}", auth=False) or {}).get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        raw = self.request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth}, auth=False,
        ) or {}
        book = raw.get("orderbook_fp") or raw.get("orderbook") or {}
        out: dict[str, list[tuple[int, float]]] = {"yes": [], "no": []}
        for side in ("yes", "no"):
            levels = book.get(f"{side}_dollars")
            if levels is None:
                levels = book.get(side)
            parsed = []
            for level in levels or []:
                try:
                    cents = _to_cents(level[0])
                    count = _to_count(level[1])
                except (IndexError, TypeError):
                    continue
                if cents is not None:
                    parsed.append((cents, count))
            parsed.sort(key=lambda x: x[0])
            out[side] = parsed
        return out

    def get_market_candlesticks(
        self,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period: int = 1,
        series_ticker: str | None = None,
    ) -> list[dict]:
        series = series_ticker or C.SERIES
        data = self.request(
            "GET",
            f"/series/{series}/markets/{ticker}/candlesticks",
            params={
                "start_ts": int(start_ts),
                "end_ts": int(end_ts),
                "period_interval": int(period),
            },
            auth=False,
        )
        return data.get("candlesticks") or []

    def get_event_candlesticks(
        self,
        event_ticker: str,
        start_ts: int,
        end_ts: int,
        period: int = 1,
        series_ticker: str | None = None,
    ) -> dict[str, list[dict]]:
        series = series_ticker or C.SERIES
        data = self.request(
            "GET",
            f"/series/{series}/events/{event_ticker}/candlesticks",
            params={
                "start_ts": int(start_ts),
                "end_ts": int(end_ts),
                "period_interval": int(period),
            },
            auth=False,
        )
        tickers = data.get("market_tickers") or []
        groups = data.get("market_candlesticks") or []
        out: dict[str, list[dict]] = {}
        for i, ticker in enumerate(tickers):
            out[ticker] = groups[i] if i < len(groups) else []
        if out:
            return out
        # Some payloads nest ticker on each candle group.
        if isinstance(groups, list) and groups and isinstance(groups[0], dict):
            if "ticker" in groups[0]:
                for g in groups:
                    out[str(g.get("ticker"))] = g.get("candlesticks") or []
        return out

    def get_trades(self, ticker: str, min_ts: int | None = None, limit: int = 1000) -> list[dict]:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self.paginate("/markets/trades", "trades", params, auth=False, max_pages=5)


def _parse_ts(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def event_open_at(event: dict | None, markets: list[dict] | None = None) -> datetime | None:
    blobs: list[dict] = []
    if event:
        blobs.append(event)
    for m in markets or []:
        blobs.append(m)
        raw = m.get("raw")
        if isinstance(raw, dict):
            blobs.append(raw)
    for obj in blobs:
        for key in ("open_time", "open_ts", "start_time"):
            dt = _parse_ts(obj.get(key))
            if dt is not None:
                return dt
    return None


def market_result(market: dict) -> str | None:
    """Official YES/NO after settlement. None if still open."""
    raw = (market.get("result") or market.get("settlement_result") or "").strip().lower()
    status = (market.get("status") or "").strip().lower()
    if raw in ("yes", "no"):
        return raw
    if status in ("settled", "finalized", "closed") and raw in ("", "all_no", "void", "scalar"):
        if raw in ("void",):
            return "void"
    return None


def market_yes_quotes(market: dict) -> tuple[int | None, int | None]:
    bid = None
    ask = None
    for key in ("yes_bid_dollars", "yes_bid", "yes_bid_cents"):
        if market.get(key) is not None:
            bid = _to_cents(market.get(key))
            break
    for key in ("yes_ask_dollars", "yes_ask", "yes_ask_cents"):
        if market.get(key) is not None:
            ask = _to_cents(market.get(key))
            break
    return bid, ask


def book_metrics(book: dict, yes_limit: int) -> dict:
    """Nofade crossing math, parameterized by our YES limit.

    SELL YES @ L fills against YES bids at >= L.
    BUY YES @ L fills against YES asks <= L, which are NO bids at >= 100-L.
    """
    yes = book.get("yes") or []
    no = book.get("no") or []
    return {
        "best_yes_bid": yes[-1][0] if yes else None,
        "best_no_bid": no[-1][0] if no else None,
        "yes_size_total": sum(c for _, c in yes),
        "no_size_total": sum(c for _, c in no),
        "yes_size_that_would_fill_sell": sum(c for p, c in yes if p >= yes_limit),
        "yes_size_that_would_fill_buy": sum(c for p, c in no if p >= (100 - yes_limit)),
    }


def market_mid_prob(bid_cents: int | None, ask_cents: int | None) -> float | None:
    if bid_cents is None and ask_cents is None:
        return None
    if bid_cents is None:
        return ask_cents / 100.0
    if ask_cents is None:
        return bid_cents / 100.0
    return ((bid_cents + ask_cents) / 2.0) / 100.0


_COUNT_RE = re.compile(r"(\d+)\s*\+?\s*(?:times|mentions|x)\b", re.I)
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{1,80})[\"“”']")


def word_from_market(market: dict) -> str:
    """Best-effort strike phrase. Prompt uses this exact string."""
    for key in ("yes_sub_title", "no_sub_title", "subtitle", "custom_strike", "strike"):
        raw = market.get(key)
        if isinstance(raw, str) and raw.strip() and len(raw.strip()) < 80:
            return _normalize_word(raw.strip())

    title = (market.get("title") or "").strip()
    m = _QUOTED_RE.search(title)
    if m:
        return _normalize_word(m.group(1))

    ticker = market.get("ticker") or market.get("market_ticker") or ""
    tail = ticker.split("-")[-1] if ticker else ""
    if tail and not tail.isdigit():
        guessed = tail.replace("_", " ").strip()
        count = _COUNT_RE.search(title) or _COUNT_RE.search(ticker)
        if count:
            return _normalize_word(f"{guessed} {count.group(1)}+")
        return _normalize_word(guessed)

    return _normalize_word(title or ticker or "unknown")


def _normalize_word(word: str) -> str:
    word = re.sub(r"\s+", " ", word).strip()
    word = word.replace("—", "-")
    return word


def uniquify_words(rows: list[dict]) -> list[dict]:
    """If two markets collapse to the same word, keep them distinct."""
    seen: dict[str, int] = {}
    out = []
    for row in rows:
        base = row["word"]
        n = seen.get(base.lower(), 0)
        seen[base.lower()] = n + 1
        if n:
            row = dict(row)
            row["word"] = f"{base} [{n + 1}]"
        out.append(row)
    return out
