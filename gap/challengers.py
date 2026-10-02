"""More free challengers through OpenAI-compatible APIs (v1.9.0). PAPER ONLY, never traded.

Same job as the Gemini challenger in gap/shadow.py: read tonight's Grok file (same text, same
news, built once) and return a probability for every word. One generic client covers every
service that speaks the OpenAI "chat completions" format:

  nvidia      build.nvidia.com      NVIDIA_API_KEY      free, open models hosted by NVIDIA in the US
                                                         (DeepSeek, Kimi, GLM, Qwen, Llama, Nemotron ...)
  cerebras    cloud.cerebras.ai     CEREBRAS_API_KEY    free daily tokens (gpt-oss-120b, Qwen ...)
  mistral     console.mistral.ai    MISTRAL_API_KEY     free "Experiment" plan
  openrouter  openrouter.ai         OPENROUTER_API_KEY  free models (ids ending ":free"), 50 requests/day
  groq        console.groq.com      GROQ_API_KEY        free tier too small for our file; works if paid

Which models run is the CHALLENGERS secret, e.g. "nvidia:deepseek, nvidia:kimi, cerebras:gpt-oss-120b".
The part after the colon is an exact model id, or a word looked up in that service's model list
(newest version wins; coder/embedding/safety models are never picked).

Retry rule (same as Gemini, Jovan Oct 1): the SAME request again while the service is busy
(429/5xx), slow, or answers with broken JSON -- waits 30s, 60s, 120s, 180s, then 300s -- for up to
CHALLENGER_RETRY_BUDGET_S (30 min). A refused key, a missing model or a too-long request fails at
once (waiting would not help). Never re-fetches news. Never touches orders.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from . import config as C, netlimit, parser

log = logging.getLogger("gap.challengers")

PROVIDERS = {
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
BACKOFF_S = (30, 60, 120, 180, 300)
NEVER_PICK = ("coder", "codestral", "embed", "guard", "safety", "reward", "parse", "vision", "omni",
              "distill", "audio", "tts", "whisper", "image", "rerank", "retriever", "ocr")
NO_TOOLS = "You have NO web, X or browsing tools in this run."


class Fatal(Exception):
    """Waiting would not help (key refused, model missing, request too big)."""


def key_for(provider: str) -> str:
    _base, env = PROVIDERS[provider]
    return getattr(C, env, "") or ""


def enabled_specs() -> list[tuple[str, str]]:
    """[(provider, model_or_word)] from CHALLENGERS whose provider has a key."""
    out = []
    for spec in C.CHALLENGERS:
        prov, _, model = spec.partition(":")
        prov, model = prov.strip().lower(), model.strip()
        if prov in PROVIDERS and model and key_for(prov):
            out.append((prov, model))
    return out


def _headers(provider: str) -> dict:
    h = {"Authorization": f"Bearer {key_for(provider)}", "Content-Type": "application/json"}
    if provider == "openrouter":
        h["X-Title"] = "wnt-gap-bot"
    return h


def _version_key(model_id: str) -> tuple:
    nums = tuple(float(x) for x in re.findall(r"\d+(?:\.\d+)?", model_id.split("/")[-1]))
    return (nums, -len(model_id))


def pick_model(provider: str, wanted: str, ids: list[str]) -> str | None:
    """Exact id if listed; otherwise the newest id containing every word of `wanted`.
    'openrouter:free' = any free model; on OpenRouter only ':free' ids are ever picked."""
    if wanted in ids:
        return wanted
    words = [w for w in re.split(r"[\s\-_/]+", wanted.lower()) if w and w != "free"]
    cands = []
    for i in ids:
        low = i.lower()
        if provider == "openrouter" and not low.endswith(":free"):
            continue
        if any(b in low for b in NEVER_PICK):
            continue
        if all(w in low for w in words):
            cands.append(i)
    if not cands:
        return None
    return sorted(cands, key=_version_key, reverse=True)[0]


def list_models(provider: str, timeout: float = 30) -> list[str]:
    base, _ = PROVIDERS[provider]
    url = f"{base}/models"
    with netlimit.ticket(url):
        r = requests.get(url, headers=_headers(provider), timeout=timeout)
    if r.status_code in (401, 403):
        raise Fatal(f"{provider}: key refused (HTTP {r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"{provider} model list: HTTP {r.status_code}")
    data = r.json()
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for m in rows or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        ctx = m.get("context_length")
        # v1.9.4: a model that cannot read our whole file answers empty ("length"). When the service
        # says how much a model can read, skip the ones under MIN_CONTEXT_TOKENS.
        if isinstance(ctx, (int, float)) and ctx < C.CHALLENGER_MIN_CONTEXT:
            continue
        out.append(str(m["id"]))
    return out


def pick_models(provider: str, wanted: str, ids: list[str], n: int = 3) -> list[str]:
    """Like pick_model but the best n (OpenRouter fallbacks). Free models from different makers first,
    so one busy host (e.g. Google for Gemma) does not take down every fallback at once."""
    first = pick_model(provider, wanted, ids)
    if not first:
        return []
    rest = [i for i in ids if i != first and pick_model(provider, wanted, [i]) == i]
    rest.sort(key=_version_key, reverse=True)
    out, makers = [first], {first.split("/")[0]}
    for i in rest:
        if i.split("/")[0] not in makers:
            out.append(i)
            makers.add(i.split("/")[0])
        if len(out) >= n:
            break
    return out


def resolve(provider: str, wanted: str) -> str:
    return resolve_list(provider, wanted)[0]


def resolve_list(provider: str, wanted: str) -> list[str]:
    ids = list_models(provider)
    got = pick_models(provider, wanted, ids, 3 if provider == "openrouter" else 1)
    if not got:
        raise Fatal(f"{provider}: no model matching {wanted!r} ({len(ids)} models listed)")
    return got


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _answer_text(resp: dict) -> str:
    choices = resp.get("choices") or []
    if not choices:
        raise ValueError("no choices in answer")
    msg = choices[0].get("message") or {}
    txt = msg.get("content") or ""
    if isinstance(txt, list):                                   # some services send parts
        txt = "".join(p.get("text", "") for p in txt if isinstance(p, dict))
    txt = _THINK_RE.sub("", txt)
    if not txt.strip():
        why = choices[0].get("finish_reason") or "empty"
        raise ValueError(f"empty answer ({why})")
    return txt


def _limit_info(r) -> tuple[str, float | None]:
    """(short text of the service's own rate-limit headers, Retry-After seconds or None)."""
    try:
        h = {k.lower(): v for k, v in (r.headers or {}).items()}
    except Exception:  # noqa: BLE001
        return "", None
    keep = [f"{k}={v}" for k, v in sorted(h.items())
            if ("ratelimit" in k or k == "retry-after") and len(str(v)) < 40][:6]
    ra = None
    try:
        if "retry-after" in h:
            ra = float(h["retry-after"])
    except ValueError:
        ra = None
    return ("; ".join(keep), ra)


def _hdrs(r) -> dict:
    try:
        return {k.lower(): str(v).strip() for k, v in (r.headers or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def _zero_allowance(r) -> bool:
    """True only when a TOKEN limit is 0 (the key really may not use the model).
    v1.9.4: Mistral sends x-ratelimit-limit-req-minute=0 to mean "no per-minute request limit"
    (it limits per second instead) -- v1.9.2 wrongly read that as blocked."""
    return any(k.startswith("x-ratelimit-limit") and "token" in k and v == "0" for k, v in _hdrs(r).items())


def _token_limit(r) -> int | None:
    """The per-minute token limit the service reports, if it reports one."""
    for k, v in _hdrs(r).items():
        if k.startswith("x-ratelimit-limit") and "token" in k and ("minute" in k or k.endswith("tokens")):
            try:
                return int(float(v))
            except ValueError:
                continue
    return None


def est_tokens(payload: str, max_tokens: int) -> int:
    """Rough size of one request as a rate limiter counts it: the text (about 4 characters a token)
    plus the room reserved for the answer."""
    return len(payload) // 4 + max_tokens


def _err_msg(r) -> str:
    try:
        js = r.json()
        e = js.get("error") if isinstance(js, dict) else None
        if isinstance(e, dict):
            return str(e.get("message") or e.get("code") or "")[:160]
        if isinstance(e, str):
            return e[:160]
        return str(js.get("message") or js.get("detail") or "")[:160]
    except Exception:  # noqa: BLE001
        return ""


def forecast(provider: str, model: str, paste: str, words: list[str], event_date: str, preface: str, *,
             budget_s: float | None = None, sleep=None, now=None, on_attempt=None,
             fallbacks: list[str] | None = None) -> dict:
    """{"model": "provider:model", "forecasts", "seconds", "attempts", "waited_s"}; Fatal / RuntimeError on failure."""
    sleep = sleep or time.sleep
    now = now or time.monotonic
    budget = C.CHALLENGER_RETRY_BUDGET_S if budget_s is None else budget_s
    say = on_attempt or (lambda msg: log.info("%s: %s", provider, msg))
    base, _ = PROVIDERS[provider]
    url = f"{base}/chat/completions"
    body = {                                     # built ONCE: every retry sends exactly this
        "model": model,
        "messages": [{"role": "system", "content": preface.format(tools=NO_TOOLS)},
                     {"role": "user", "content": paste}],
        "temperature": 0.3,
        "max_tokens": C.CHALLENGER_MAX_TOKENS,
    }
    if fallbacks:
        body["models"] = [model] + [m for m in fallbacks if m != model][:2]   # OpenRouter: try these in order
    payload = json.dumps(body)
    need = est_tokens(payload, C.CHALLENGER_MAX_TOKENS)
    label = f"{provider}:{model}"
    start = now()
    attempt, waited = 0, 0.0
    while True:
        attempt += 1
        t0 = now()
        err = None
        try:
            with netlimit.ticket(url):
                r = requests.post(url, headers=_headers(provider), data=payload, timeout=C.CHALLENGER_TIMEOUT_S)
            retry_after = None
            if r.status_code == 200:
                try:
                    js = r.json()
                    data = parser.validate(_answer_text(js), words, event_date)
                    used = f"{provider}:{js.get('model')}" if (fallbacks and js.get("model")) else label
                    return {"model": used, "forecasts": data["forecasts"], "seconds": round(now() - t0, 1),
                            "attempts": attempt, "waited_s": round(waited)}
                except Exception as exc:  # noqa: BLE001
                    err = f"bad answer ({str(exc)[:120]})"
            elif r.status_code in (401, 403):
                raise Fatal(f"{label}: key refused (HTTP {r.status_code}) {_err_msg(r)}".strip())
            elif r.status_code == 404:
                raise Fatal(f"{label}: model not found (HTTP 404) {_err_msg(r)}".strip())
            elif r.status_code == 429 and _zero_allowance(r):
                raise Fatal(f"{label}: this key is allowed 0 tokens a minute on this model (waiting will not help)")
            elif r.status_code == 429 and (_token_limit(r) or 10**12) < need:
                raise Fatal(f"{label}: one request is about {need:,} tokens but this model allows "
                            f"{_token_limit(r):,} a minute -- it can never fit (pick a model with a bigger limit)")
            elif r.status_code in RETRYABLE:
                info, retry_after = _limit_info(r)
                err = f"HTTP {r.status_code} {_err_msg(r)}".strip() + (f" [{info}]" if info else "")
            else:
                raise Fatal(f"{label}: HTTP {r.status_code} {_err_msg(r)}".strip())   # e.g. 400/413 too long
        except requests.Timeout:
            err, retry_after = f"no answer within {C.CHALLENGER_TIMEOUT_S:.0f}s", None
        except requests.RequestException as exc:
            err, retry_after = type(exc).__name__, None

        elapsed = now() - start
        delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        if retry_after and retry_after > delay:      # the service said how long to wait: obey it
            delay = int(min(retry_after, 900))
        if elapsed + delay > budget:
            raise RuntimeError(f"{label}: gave up after {attempt} tries in {elapsed / 60:.0f} min (last: {err})")
        say(f"try {attempt}: {label}: {err}; same request again in {delay}s")
        sleep(delay)
        waited += delay


def resolve_with_retry(provider: str, wanted: str, *, budget_s: float, sleep=None, now=None, on_attempt=None) -> list[str]:
    """Model-list lookup with the same patience (a busy service can also refuse the list)."""
    sleep = sleep or time.sleep
    now = now or time.monotonic
    say = on_attempt or (lambda msg: log.info("%s: %s", provider, msg))
    start, attempt = now(), 0
    while True:
        attempt += 1
        try:
            return resolve_list(provider, wanted)
        except Fatal:
            raise
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:120]
        delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        if now() - start + delay > budget_s:
            raise RuntimeError(f"{provider}: model list failed after {attempt} tries (last: {err})")
        say(f"{provider} model list try {attempt}: {err}; again in {delay}s")
        sleep(delay)
