"""Finding text, for incremental search and query-replace.

Searching goes line by line rather than over one big string, which costs
nothing on the buffer sizes this editor is for and keeps every result in the
(row, column) positions the rest of the editor speaks.

Case follows Emacs' rule, which is worth keeping because it is invisible until
you need it: a search typed in lower case ignores case, and typing a single
capital makes the search exact.
"""

from __future__ import annotations

import re
from typing import Optional

from .buffer import Buffer, Pos


def folds(pattern: str) -> bool:
    """Should this pattern ignore case?"""
    return pattern == pattern.lower()


def search_forward(buffer: Buffer, pattern: str, start: Pos,
                   bound: Optional[Pos] = None) -> Optional[Pos]:
    """Where ``pattern`` next appears at or after ``start``."""
    if not pattern:
        return None
    ignore = folds(pattern)
    needle = pattern.lower() if ignore else pattern
    row, column = start
    while row < len(buffer.lines):
        line = buffer.lines[row]
        index = (line.lower() if ignore else line).find(
            needle, column if row == start[0] else 0)
        if index >= 0:
            if bound is not None and (row, index + len(pattern)) > bound:
                return None
            return (row, index)
        if bound is not None and row >= bound[0]:
            return None
        row += 1
    return None


def glob_regexp(pattern: str):
    """Translate a filename glob into a regexp for a path relative to the root.

    Matching the whole relative path rather than just the file name is what
    makes ``**/*.py`` and ``tests/*.py`` mean anything; matching only the name,
    as :mod:`fnmatch` invites, silently finds nothing whenever the pattern has
    a slash in it.

    A pattern with no slash is matched at any depth, so ``*.py`` still means
    every Python file in the tree -- the same rule git applies to the same
    syntax.  ``*`` and ``?`` stop at a slash; ``**`` crosses them.
    """
    if "/" not in pattern:
        pattern = "**/" + pattern
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:[^/]+/)*")  # Any number of directories, or none.
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                parts.append(re.escape(pattern[index]))
                index += 1
            else:
                body = pattern[index + 1:close]
                parts.append("[%s]" % ("^" + body[1:] if body[:1] == "!"
                                       else body))
                index = close + 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def search_backward(buffer: Buffer, pattern: str,
                    start: Pos) -> Optional[Pos]:
    """Where ``pattern`` last begins strictly before ``start``."""
    if not pattern:
        return None
    ignore = folds(pattern)
    needle = pattern.lower() if ignore else pattern
    row, column = start
    while row >= 0:
        line = buffer.lines[row]
        limit = column if row == start[0] else len(line)
        index = (line.lower() if ignore else line).rfind(needle, 0, limit)
        if index >= 0:
            return (row, index)
        row -= 1
    return None
