"""v1.11.1 tests: the paid Grok search run is OFF unless switched on, has cheap defaults when it is
switched on, and the hand-run test script asks before it spends money. No network.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

from gap import config as C, xai

ROOT = pathlib.Path(__file__).resolve().parents[1]
XAI_KEYS = ("XAI_EXPERT_ON", "XAI_PLAIN_ON", "XAI_EXPERT_MAX_TURNS", "XAI_EXPERT_EFFORT",
            "XAI_EXPERT_MAX_PAID_TRIES", "XAI_NIGHTLY_BUDGET_USD", "XAI_EFFORT", "XAI_MODEL")


def _defaults(extra_env=None) -> dict:
    """Import gap.config in a clean process (no XAI_* settings, no Streamlit secrets file) and read it."""
    env = {k: v for k, v in os.environ.items() if k not in XAI_KEYS and k != "XAI_API_KEY"}
    env.update(extra_env or {})
    code = ("import json; from gap import config as C; "
            "print(json.dumps({k: getattr(C, k) for k in %r}))" % (XAI_KEYS,))
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_search_run_is_off_by_default():
    d = _defaults()
    assert d["XAI_EXPERT_ON"] is False                      # real money: opt in only
    assert d["XAI_PLAIN_ON"] is True                        # the cheap no-tools run stays on
    assert d["XAI_EXPERT_MAX_TURNS"] == 6 and d["XAI_EXPERT_EFFORT"] == "low"
    assert d["XAI_EXPERT_MAX_PAID_TRIES"] == 1              # a broken answer is not paid for twice
    assert d["XAI_NIGHTLY_BUDGET_USD"] == 6.0


def test_search_run_can_still_be_switched_on():
    d = _defaults({"XAI_EXPERT_ON": "true", "XAI_EXPERT_MAX_TURNS": "3"})
    assert d["XAI_EXPERT_ON"] is True and d["XAI_EXPERT_MAX_TURNS"] == 3


def test_only_the_plain_run_is_enabled_with_a_key(monkeypatch):
    monkeypatch.setattr(C, "XAI_API_KEY", "xai-secret-key")
    monkeypatch.setattr(C, "XAI_PLAIN_ON", True)
    monkeypatch.setattr(C, "XAI_EXPERT_ON", False)
    assert xai.enabled_modes() == ["plain"]
    monkeypatch.setattr(C, "XAI_API_KEY", "")
    assert xai.enabled_modes() == []                        # no key: nothing runs, nothing fails


def _load_script(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["xai_now.py"] + argv)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost:1/none")   # shape only; never connected here
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key")
    spec = importlib.util.spec_from_file_location("xai_now_test", ROOT / "scripts" / "xai_now.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_asks_before_a_paid_search_run(monkeypatch, capsys):
    mod = _load_script(monkeypatch, ["--only-expert"])
    asked = []

    def ask(q):
        asked.append(q)
        return "no"

    assert mod.confirm_search(ask=ask) is False
    assert len(asked) == 1
    assert mod.confirm_search(ask=lambda q: " YES ") is True
    out = capsys.readouterr().out
    assert "about $3" in out and "rounds" in out
    assert "xai-secret-key" not in out and "postgresql" not in out


def test_script_yes_flag_and_closed_input(monkeypatch):
    mod = _load_script(monkeypatch, ["--only-expert", "--yes"])

    def never(q):
        raise AssertionError("must not ask when --yes was given")

    assert mod.confirm_search(ask=never) is True
    mod = _load_script(monkeypatch, ["--only-expert"])

    def closed(q):
        raise EOFError

    assert mod.confirm_search(ask=closed) is False          # no keyboard: do not spend
