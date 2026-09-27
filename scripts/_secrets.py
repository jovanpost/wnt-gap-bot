"""
Ask for secrets the way a terminal never should: hidden input, never echoed,
never left in shell history. Call need_database_url() BEFORE importing
anything from `gap` -- gap/config.py reads DATABASE_URL at import time, so
the prompt has to happen first or the import already ran with an empty value.

Every script the user runs BY HAND (not GitHub Actions, which reads secrets
from the environment non-interactively) must go through this module instead
of `export DATABASE_URL=...` on the command line or `DATABASE_URL=... python
...`. Both of those print the secret to the screen and save it in shell
history -- getpass does neither.
"""
from __future__ import annotations

import getpass
import os
import re

_URL_RE = re.compile(r"^postgres(?:ql)?://", re.I)
_PASS_RE = re.compile(r"(postgres(?:ql)?://[^:@/\s]+:)([^@\s]+)(@)", re.I)


def need_database_url(prompt: str = "Database URL (postgresql://...): ") -> str:
    """Prompt with hidden input, validate the shape, set os.environ, return it.
    Never echoes what was typed and never prints it back."""
    existing = os.environ.get("DATABASE_URL", "")
    if existing and _URL_RE.match(existing.strip()):
        return existing  # GitHub Actions / already-set case: no prompt needed
    url = getpass.getpass(prompt).strip()
    if not _URL_RE.match(url):
        raise SystemExit("that doesn't look like a postgres:// or postgresql:// URL -- aborting")
    os.environ["DATABASE_URL"] = url
    return url


def mask(text: str) -> str:
    """Redact the password portion of any postgres://user:PASSWORD@host in a string,
    so status/error messages are always safe to paste into chat."""
    return _PASS_RE.sub(r"\1***\3", text)


def need_database_url_optional(
    prompt: str = "Supabase DATABASE_URL (blank = use local SQLite instead): ",
) -> str:
    """Same hidden-input contract as need_database_url(), but blank is a valid
    answer -- gap/config.py falls back to SQLITE_PATH when DATABASE_URL is empty.
    Use this for read-only checks where a real Postgres connection is nice to
    have but not required."""
    existing = os.environ.get("DATABASE_URL", "")
    if existing and _URL_RE.match(existing.strip()):
        return existing
    url = getpass.getpass(prompt).strip()
    if url and not _URL_RE.match(url):
        raise SystemExit("that doesn't look like a postgres:// or postgresql:// URL -- aborting")
    if url:
        os.environ["DATABASE_URL"] = url
    return url


def _prompt_hidden(label: str) -> str:
    return getpass.getpass("%s (hidden -- paste it and press Enter): " % label).strip()


def _prompt_multiline_hidden(label: str) -> str:
    print(label)
    print("Paste the FULL value (multi-line is fine), then on its own new line press Ctrl-D:")
    lines: list[str] = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass
    return "\n".join(lines).strip()


def need_kalshi_credentials() -> tuple[str, str]:
    """Prompt for KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PEM with hidden input if
    they aren't already set as env vars (e.g. from Streamlit/Actions secrets).
    Never prints either one. Sets both in os.environ and returns them -- call
    this BEFORE importing anything from `gap`, since gap/config.py reads both
    at import time."""
    key_id = os.environ.get("KALSHI_KEY_ID", "").strip()
    if not key_id:
        key_id = _prompt_hidden("Kalshi API key ID")
        if key_id:
            os.environ["KALSHI_KEY_ID"] = key_id

    pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
    if not pem:
        pem = _prompt_multiline_hidden(
            "Kalshi private key (the full -----BEGIN...----- to -----END...----- PEM block):"
        )
        if pem:
            os.environ["KALSHI_PRIVATE_KEY_PEM"] = pem

    return key_id, pem
