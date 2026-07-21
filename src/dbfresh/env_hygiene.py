"""Detect a `.env` beside a config that git would track (not gitignored)."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Same tuple, same rationale as store._GIT_LOOKUP_ERRORS: an inline
# `except (A, B):` reads ambiguously next to the no-parens `except A, B:`
# spelling (also valid Python), and ruff's formatter normalizes an inline
# except-clause tuple to that unparenthesized form -- so this stays a
# named constant rather than an inline tuple.
_GIT_LOOKUP_ERRORS = (OSError, subprocess.SubprocessError)


def committable_env_file(config_path: Path) -> Path | None:
    """The ``.env`` beside ``config_path`` that git would track, or None.

    Returns the ``.env`` path when a ``.env`` exists in the config's
    directory, that directory is inside a git working tree, and ``.env`` is
    not matched by any gitignore rule -- i.e. git would commit it. Returns
    None when there is no ``.env``, the directory is not a git working
    tree, ``.env`` is already ignored, or git is not available. The
    shared-config workflow relies on the per-user ``.env`` staying out of
    git, so a committable ``.env`` likely holds secrets that must not be
    committed.
    """
    env_path = config_path.parent / ".env"
    if not env_path.is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=config_path.parent,
            capture_output=True,
            timeout=5,
        )
    except _GIT_LOOKUP_ERRORS:
        return None
    if proc.returncode == 0:
        return None  # ignored
    if proc.returncode == 1:
        return env_path  # not ignored, inside a repo -- git would track it
    return None  # not a git working tree, or an error
