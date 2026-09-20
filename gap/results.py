"""Official Kalshi results, cached forever once published.

A yes/no/void result never changes, so it is stored in gap_results the first time
we see it. Used by the weekly dump (every word, traded or not) and by the WORD
HISTORY block sent to Grok.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import store
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.results")


def results_for(tickers, client: KalshiClient | None = None,
                budget_s: int = 90, workers: int = 5) -> tuple[dict, int]:
    """Returns ({ticker: 'yes'|'no'|'void'|None}, n_lookup_errors).
    Cached results are free; the rest are fetched in parallel within budget_s."""
    tickers = sorted({t for t in tickers if t})
    out: dict = {}
    try:
        out.update(store.results_cached(tickers))
    except Exception as exc:
        log.warning("results cache read failed: %s", exc)
    need = [t for t in tickers if t not in out]
    errors = 0
    if need:
        client = client or KalshiClient()

        def one(t):
            return t, market_result(client.get_market(t))

        fresh: dict = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, t) for t in need]
            try:
                for fut in as_completed(futs, timeout=budget_s):
                    try:
                        t, res = fut.result()
                        if res in ("yes", "no", "void"):
                            fresh[t] = res
                    except Exception as exc:
                        errors += 1
                        log.warning("result lookup failed: %s", exc)
            except Exception:
                errors += sum(1 for f in futs if not f.done())
                for f in futs:
                    f.cancel()
        out.update(fresh)
        try:
            store.results_save(fresh)
        except Exception as exc:
            log.warning("results cache write failed: %s", exc)
    for t in tickers:
        out.setdefault(t, None)
    return out, errors
