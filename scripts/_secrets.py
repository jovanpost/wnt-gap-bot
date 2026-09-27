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
