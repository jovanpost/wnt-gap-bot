"""Kalshi REST client.

Read path (events, markets, quotes) has been here since Phase 1. The order-
placement path below (create_no_order / cancel_order / batch_cancel /
get_balance / get_fills / get_resting_orders / get_positions) is new for
Book L and is ported near-verbatim from wnt-nofade-bot's wnt/kalshi.py --
same signing, same endpoints, same v1/v2 order-API branching -- per the
explicit instruction to reuse that bot's proven live-order plumbing rather
than invent a new one. Only Book L calls these; every paper book (A/B/E/F/G/
H/I/K/M/SCALP) still only ever calls the read methods."""
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

from . import config as C, lease

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

    # ------------------------------------------------------------------
    # Real-money order path (Book L only). Ported from wnt-nofade-bot/wnt/kalshi.py.
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def get_resting_orders(self, series_prefix: str | None = None) -> list[dict]:
        orders = self.paginate("/portfolio/orders", "orders", {"status": "resting", "limit": 200})
        if series_prefix:
            orders = [o for o in orders if str(o.get("ticker", "")).startswith(series_prefix)]
        return orders

    def get_fills(self, ticker: str | None = None, limit: int = 200) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.paginate("/portfolio/fills", "fills", params)

    def get_positions(self) -> list[dict]:
        return self.paginate("/portfolio/positions", "market_positions", {"limit": 200})

    def create_no_order(
        self,
        ticker: str,
        no_price_cents: int,
        count: float,
        client_order_id: str,
        post_only: bool = True,
        expiration_epoch: int | None = None,
    ) -> dict:
        """Buy NO at no_price_cents (== sell YES at 100-no_price_cents). Same
        v1/v2 branching as nofade's client, selected by C.ORDER_API."""
        lease.require("send a real order")   # only the place that holds the worker lease may send
        if C.ORDER_API == "v1":
            body = {
                "ticker": ticker,
                "client_order_id": client_order_id,
                "action": "buy",
                "side": "no",
                "count": int(count),
                "type": "limit",
                "no_price": int(no_price_cents),
                "post_only": bool(post_only),
            }
            if expiration_epoch:
                body["expiration_ts"] = int(expiration_epoch)
            resp = self.request("POST", "/portfolio/orders", body=body)
            order = resp.get("order") or {}
            return {
                "order_id": order.get("order_id"),
                "client_order_id": order.get("client_order_id") or client_order_id,
                "fill_count": _to_count(order.get("taker_fill_count") or 0),
                "remaining_count": _to_count(order.get("remaining_count") or count),
                "avg_fill_price_cents": _to_cents(order.get("taker_fill_cost")),
                "raw": resp,
            }

        yes_price = 100 - int(no_price_cents)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": "ask",
            "count": f"{float(count):.2f}",
            "price": f"{yes_price / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
            "reduce_only": False,
        }
        if expiration_epoch:
            body["expiration_time"] = int(expiration_epoch)
        resp = self.request("POST", "/portfolio/events/orders", body=body)
        return {
            "order_id": resp.get("order_id"),
            "client_order_id": resp.get("client_order_id") or client_order_id,
            "fill_count": _to_count(resp.get("fill_count")),
            "remaining_count": _to_count(resp.get("remaining_count")),
            "avg_fill_price_cents": _to_cents(resp.get("average_fill_price")),
            "raw": resp,
        }

    def cancel_order(self, order_id: str) -> bool:
        for endpoint in (f"/portfolio/events/orders/{order_id}", f"/portfolio/orders/{order_id}"):
            try:
                self.request("DELETE", endpoint, retries=3)
                return True
            except KalshiError as exc:
                if exc.status == 404:
                    return True
                log.warning("cancel via %s failed: %s", endpoint, exc)
        return False

    def batch_cancel(self, order_ids: list[str]) -> tuple[int, list[str]]:
        if not order_ids:
            return 0, []
        try:
            resp = self.request(
                "DELETE", "/portfolio/events/orders/batched",
                body={"orders": [{"order_id": oid} for oid in order_ids]},
                retries=3,
            )
            failed = []
            ok = 0
            for entry in resp.get("orders", []):
                if entry.get("error"):
                    failed.append(entry.get("order_id"))
                else:
                    ok += 1
            if ok or not failed:
                return ok, [f for f in failed if f]
        except KalshiError as exc:
            log.warning("batch cancel failed (%s), falling back to singles", exc)
        ok, failed = 0, []
        for oid in order_ids:
            if self.cancel_order(oid):
                ok += 1
            else:
                failed.append(oid)
            time.sleep(0.05)
        return ok, failed


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


def resolve_real_open(client: "KalshiClient", event_ticker: str, event_date: str) -> datetime | None:
    """The REAL Kalshi open time for tonight's event, read from the API itself
    (get_markets -> open_time/open_ts/start_time), never a guessed clock string.
    Kalshi's own open time moves night to night -- sometimes before 12:30 CT,
    sometimes after -- so nothing that gates real order placement should assume
    a fixed time. Cached in gap_state once found (per event_date) so repeated
    poll ticks don't re-hit the API; returns None (meaning: not known yet, keep
    waiting) until Kalshi has actually published it for this event."""
    from . import store  # local import: store has no dependency on this module

    key = f"real_open_at:{event_date}"
    cached = store.get_state(key)
    if cached and cached.get("iso"):
        dt = _parse_ts(cached["iso"])
        if dt is not None:
            return dt
    try:
        markets = client.get_markets(event_ticker)
    except Exception:
        markets = []
    dt = event_open_at({"event_ticker": event_ticker}, markets)
    if dt is not None:
        store.set_state(key, {"iso": dt.isoformat()})
    return dt


def market_result(market: dict) -> str | None:
    """Official YES/NO from Kalshi only. Never infer from last price."""
    for key in ("result", "settlement_result", "market_result"):
        raw = str(market.get(key) or "").strip().lower()
        if raw in ("yes", "no", "void"):
            return raw
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
