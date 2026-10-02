"""Grok through xAI's API (v1.11.0). PAPER ONLY, never traded. The live Book L still waits for the
manual Grok paste in Telegram.

Two challengers, both on the same nightly file every other model gets:

  xai:<model>          "plain": no tools, same short note as the other challengers ("you have no web
                       or X tools; the news blocks in the file are your research").
  xai:<model>+search   "expert": the replica of the manual step. The file goes in exactly as it is
                       pasted into Grok Expert (one user message, no extra note), with xAI's own
                       web_search and x_search tools switched on and reasoning set high.

API: POST https://api.x.ai/v1/responses  (docs.x.ai, checked Oct 2 2026)
  body      {"model", "input": [{role, content}], "reasoning": {"effort"}, "tools": [...], "max_turns"}
  answer    output[] -> items of type "message" -> content[] of type "output_text" -> text
  usage     input_tokens, output_tokens, reasoning_tokens, cached_tokens, num_server_side_tools_used,
            server_side_tool_usage_details (x_posts_fetched...), cost_in_usd_ticks (1 USD = 10^10 ticks:
            covers tokens AND tool calls of that request)
  prices    web_search $5 / 1k calls; x_search $5 / 1k posts fetched; tokens per the model's price.

Money guards (this is the only challenger that costs real dollars):
  * every answer's usage and cost is stored (gap_llm_runs) and shown in the Telegram table;
  * the search run is capped by XAI_EXPERT_MAX_TURNS and is never re-sent after a timeout (it may
    already have been billed); a broken answer is re-asked at most XAI_EXPERT_MAX_PAID_TRIES times;
  * no search run starts once tonight's recorded xAI spend reached XAI_NIGHTLY_BUDGET_USD.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

import requests

from . import config as C, netlimit, parser

log = logging.getLogger("gap.xai")

URL = "https://api.x.ai/v1/responses"
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
BACKOFF_S = (30, 60, 120, 180, 300)
TICKS_PER_USD = 10_000_000_000
NO_TOOLS = "You have NO web, X or browsing tools in this run."


class Fatal(Exception):
    """Waiting or re-sending would not help (key refused, bad request, out of credit)."""


def label(mode: str) -> str:
    return f"xai:{C.XAI_MODEL}" + ("+search" if mode == "expert" else "")


def enabled_modes() -> list[str]:
    if not C.XAI_API_KEY:
        return []
    return [m for m, on in (("plain", C.XAI_PLAIN_ON), ("expert", C.XAI_EXPERT_ON)) if on]


def _headers() -> dict:
    return {"Authorization": f"Bearer {C.XAI_API_KEY}", "Content-Type": "application/json"}


def build_body(mode: str, paste: str, event_date: str, preface: str, with_max_turns: bool = True) -> dict:
    if mode == "expert":
        # Exactly the manual step: the whole file as ONE user message, nothing added.
        since = (date.fromisoformat(event_date) - timedelta(days=C.XAI_X_SEARCH_DAYS)).isoformat()
        body = {
            "model": C.XAI_MODEL,
            "input": [{"role": "user", "content": paste}],
            "reasoning": {"effort": C.XAI_EXPERT_EFFORT},
            "tools": [{"type": "web_search"}, {"type": "x_search", "from_date": since}],
        }
        if with_max_turns and C.XAI_EXPERT_MAX_TURNS > 0:
            body["max_turns"] = C.XAI_EXPERT_MAX_TURNS
        return body
    return {
        "model": C.XAI_MODEL,
        "input": [{"role": "system", "content": preface.format(tools=NO_TOOLS)},
                  {"role": "user", "content": paste}],
        "reasoning": {"effort": C.XAI_EFFORT},
    }


def answer_text(resp: dict) -> str:
    """The final message text. Reasoning and tool-call items in output[] are skipped."""
    last = ""
    for item in resp.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        parts = item.get("content") or []
        if isinstance(parts, str):
            last = parts or last
            continue
        txt = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") in ("output_text", "text"))
        last = txt or last
    last = last or (resp.get("output_text") or "")
    if not last.strip():
        why = resp.get("status") or "empty"
        detail = (resp.get("incomplete_details") or {}).get("reason") if isinstance(resp.get("incomplete_details"), dict) else ""
        raise ValueError(f"empty answer ({why}{' ' + detail if detail else ''})")
    return last


def usage_of(resp: dict) -> dict:
    """Flat usage numbers from one answer; cost_usd from xAI's own cost_in_usd_ticks."""
    u = resp.get("usage") or {}
    out_d = u.get("output_tokens_details") or {}
    in_d = u.get("input_tokens_details") or {}
    tool_d = u.get("server_side_tool_usage_details") or resp.get("server_side_tool_usage_details") or {}
    ticks = u.get("cost_in_usd_ticks")
    tools = u.get("num_server_side_tools_used")
    if tools is None:
        by_tool = u.get("server_side_tool_usage") or resp.get("server_side_tool_usage")
        if isinstance(by_tool, dict):
            tools = sum(int(v or 0) for v in by_tool.values())
        elif isinstance(by_tool, list):
            tools = len(by_tool)
    return {
        "input_tokens": int(u.get("input_tokens") or u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or u.get("completion_tokens") or 0),
        "reasoning_tokens": int(u.get("reasoning_tokens") or out_d.get("reasoning_tokens") or 0),
        "cached_tokens": int(u.get("cached_tokens") or in_d.get("cached_tokens") or 0),
        "tool_calls": int(tools or 0),
        "x_posts": int((tool_d or {}).get("x_posts_fetched") or 0),
        "cost_usd": (float(ticks) / TICKS_PER_USD) if ticks is not None else None,
        "citations": len(resp.get("citations") or []),
    }


def _add(total: dict, u: dict) -> dict:
    for k, v in u.items():
        if v is None:
            continue
        total[k] = (total.get(k) or 0) + v
    return total


def _err(r) -> str:
    try:
        js = r.json()
    except Exception:  # noqa: BLE001
        return ""
    e = js.get("error") if isinstance(js, dict) else None
    if isinstance(e, dict):
        return str(e.get("message") or e.get("code") or "")[:200]
    if isinstance(e, str):
        return e[:200]
    return str((js or {}).get("message") or (js or {}).get("detail") or "")[:200] if isinstance(js, dict) else ""


def forecast(mode: str, paste: str, words: list[str], event_date: str, preface: str, *,
             budget_s: float | None = None, sleep=None, now=None, on_attempt=None) -> dict:
    """{"model", "forecasts", "seconds", "attempts", "waited_s", "usage"}.
    Raises Fatal / RuntimeError; a RuntimeError carries `.usage` when paid answers were thrown away."""
    if not C.XAI_API_KEY:
        raise Fatal("XAI_API_KEY not set")
    sleep = sleep or time.sleep
    now = now or time.monotonic
    expert = mode == "expert"
    budget = C.CHALLENGER_RETRY_BUDGET_S if budget_s is None else budget_s
    timeout = C.XAI_EXPERT_TIMEOUT_S if expert else C.XAI_TIMEOUT_S
    max_paid = C.XAI_EXPERT_MAX_PAID_TRIES if expert else 3
    say = on_attempt or (lambda msg: log.info("xai: %s", msg))
    name = label(mode)
    with_turns = True
    payload = json.dumps(build_body(mode, paste, event_date, preface, with_turns))
    start = now()
    attempt = paid = 0
    waited = 0.0
    spent: dict = {}

    def fail(msg: str, cls=RuntimeError):
        exc = cls(f"{name}: {msg}")
        exc.usage = dict(spent)
        return exc

    while True:
        attempt += 1
        t0 = now()
        err = None
        try:
            with netlimit.ticket(URL):
                r = requests.post(URL, headers=_headers(), data=payload, timeout=timeout)
        except requests.Timeout:
            if expert:
                raise fail(f"no answer within {timeout:.0f}s. NOT re-sent: the search run may already be billed. "
                           "Check the xAI console; raise XAI_EXPERT_TIMEOUT_S or lower XAI_EXPERT_MAX_TURNS.")
            err = f"no answer within {timeout:.0f}s"
        except requests.RequestException as exc:
            if expert and attempt > 1:
                raise fail(f"connection problem ({type(exc).__name__}); not re-sent again")
            err = type(exc).__name__
        else:
            if r.status_code == 200:
                paid += 1
                try:
                    js = r.json()
                except Exception:  # noqa: BLE001
                    js = {}
                _add(spent, usage_of(js))
                try:
                    data = parser.validate(answer_text(js), words, event_date)
                    return {"model": name, "forecasts": data["forecasts"], "seconds": round(now() - t0, 1),
                            "attempts": attempt, "waited_s": round(waited), "usage": dict(spent)}
                except Exception as exc:  # noqa: BLE001
                    err = f"bad answer ({str(exc)[:120]})"
                    if paid >= max_paid:
                        raise fail(f"{err}; stopped after {paid} paid answer(s)")
            elif r.status_code in (401, 403):
                raise fail(f"key refused (HTTP {r.status_code}) {_err(r)}".strip(), Fatal)
            elif r.status_code in (400, 422) and with_turns and expert and "max_turns" in _err(r).lower():
                with_turns = False                       # the API did not accept the cap: send without it once
                payload = json.dumps(build_body(mode, paste, event_date, preface, False))
                say(f"try {attempt}: {name}: max_turns was not accepted; sending without it")
                continue
            elif r.status_code in RETRYABLE:
                err = f"HTTP {r.status_code} {_err(r)}".strip()
            else:
                raise fail(f"HTTP {r.status_code} {_err(r)}".strip(), Fatal)

        elapsed = now() - start
        delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        if elapsed + delay > budget:
            raise fail(f"gave up after {attempt} tries in {elapsed / 60:.0f} min (last: {err})")
        say(f"try {attempt}: {name}: {err}; same request again in {delay}s")
        sleep(delay)
        waited += delay


def cost_line(u: dict | None) -> str:
    """'$0.41, 15,200 in / 9,800 out (6,100 thinking), 38 tool calls, 220 X posts' -- for tables and logs."""
    if not u:
        return ""
    bits = []
    if u.get("cost_usd") is not None:
        bits.append(f"${u['cost_usd']:.2f}" if u["cost_usd"] >= 0.01 else f"${u['cost_usd']:.4f}")
    if u.get("input_tokens") or u.get("output_tokens"):
        think = f" ({u['reasoning_tokens']:,} thinking)" if u.get("reasoning_tokens") else ""
        bits.append(f"{u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out{think}")
    if u.get("tool_calls"):
        bits.append(f"{u['tool_calls']} tool calls")
    if u.get("x_posts"):
        bits.append(f"{u['x_posts']} X posts")
    return ", ".join(bits)
