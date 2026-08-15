"""What was typed into the minibuffer last time, and the time before.

Emacs keeps this with savehist, which writes elisp ``setq`` forms into
``~/.emacs.d/history``.  This writes JSON into ``~/.bkmacs-history`` for the
same reason: the file you opened yesterday is usually the one you want today,
and typing a grep pattern twice is worse than storing it.

Written on every addition rather than at exit, because an editor that is
killed is exactly the case where you wanted the history kept.  The file is
private -- what you searched for is nobody else's business.
"""

from __future__ import annotations

import json
import os
from typing import Optional

#: Entries kept per ring.  Emacs' history-length default is 100 too.
LIMIT = 100

DEFAULT_PATH = "~/.bkmacs-history"


class History:
    """Named rings of strings, newest first, persisted as one JSON object."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.expanduser(
            path or os.environ.get("BKMACS_HISTORY") or DEFAULT_PATH)
        self.rings: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return  # No history yet, or a file that is no longer readable.
        if not isinstance(stored, dict):
            return
        for kind, items in stored.items():
            if isinstance(items, list):
                self.rings[str(kind)] = [item for item in items
                                         if isinstance(item, str)][:LIMIT]

    def get(self, kind: str) -> list[str]:
        return self.rings.setdefault(kind, [])

    def add(self, kind: str, text: str) -> None:
        """Record an entry, moving a repeat back to the front rather than
        letting the ring fill up with the same path."""
        if not text:
            return
        ring = self.get(kind)
        if text in ring:
            ring.remove(text)
        ring.insert(0, text)
        del ring[LIMIT:]
        self.save()

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        temporary = os.path.join(directory, ".bkmacs-history.%d" % os.getpid())
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.rings, stream, ensure_ascii=False, indent=1)
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass  # Losing the history is not worth an error message.
