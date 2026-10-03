#!/usr/bin/env python3
"""Headless entry point: the same background work the Streamlit app starts, without the page.

Run by systemd on the server (klsh-worker@wnt-gap-bot). It waits until it holds the worker
lease, then runs the loops. SIGTERM gives the lease back and exits 0. If another place takes the
lease, the process exits with code 3 and systemd starts it again (it then waits).
"""
from __future__ import annotations

import logging
import signal
import sys
import threading

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

from gap import config as C, runtime  # noqa: E402

log = logging.getLogger("gap.worker")
_stop = threading.Event()


def _term(signum, _frame) -> None:
    _stop.set()
    runtime.interrupt()


def main() -> int:
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    me = runtime.host("vps")
    print("%s worker starting on %s" % (C.VERSION, me), flush=True)
    if me == "streamlit":
        print("KLSH_HOST is 'streamlit' here. That name belongs to the Streamlit app: two places with "
              "one name would both run. Set KLSH_HOST = \"vps\" in this bot's secrets.", flush=True)
        return 2
    if not runtime.run_workers():
        print("RUN_WORKERS is false here: nothing to do. Remove that setting on the server.", flush=True)
        return 2
    runtime.start_workers(where="vps", block=True, exit_on_lost=True)
    _stop.wait()
    log.info("stopping: giving the lease back")
    runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
