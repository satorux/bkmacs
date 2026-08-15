"""The text being edited, and everything that changes it.

A buffer is a list of lines without their newlines, which makes ``"\\n".join``
the whole of saving and ``split("\\n")`` the whole of loading -- and, usefully,
round trips a file whether or not it ends in a newline: ``"abc\\n"`` becomes
``["abc", ""]`` and joins back to itself, which is also exactly what Emacs
shows, an empty last line you can move the cursor onto.

Every modification goes through :meth:`Buffer.edit`, which replaces a range
with a string; insertion and deletion are that operation with one side empty.
Having a single primitive is what keeps undo honest, since the inverse of a
replacement is another replacement.

Only UTF-8 is supported, so there is no encoding to detect.  Bytes that are
not valid UTF-8 are still read, through ``surrogateescape``, and written back
unchanged -- without that, opening a binary file by mistake would let the
half-second autosave overwrite it with replacement characters.
"""

from __future__ import annotations

import os
import time
from typing import NamedTuple, Optional

#: A position in the buffer: a row, and a character index within that row.
#: Not a display column -- the screen has to ask :mod:`bkmacs.layout` for that.
Pos = tuple[int, int]


class Change(NamedTuple):
    """One replacement, and enough context to reverse it."""

    start: Pos
    removed: str
    inserted: str
    point_before: Pos


def advance(start: Pos, text: str) -> Pos:
    """The position just past ``text`` if it were inserted at ``start``."""
    if "\n" not in text:
        return (start[0], start[1] + len(text))
    rows = text.split("\n")
    return (start[0] + len(rows) - 1, len(rows[-1]))


def adjust(pos: Pos, start: Pos, end: Pos, new_end: Pos) -> Pos:
    """Move ``pos`` to follow a replacement of ``[start, end)``.

    The mark has to survive edits made elsewhere in the buffer, the way an
    Emacs marker does; a position inside the replaced text collapses to its
    start, having lost what it was pointing at.
    """
    if pos <= start:
        return pos
    if pos < end:
        return start
    row = pos[0] + (new_end[0] - end[0])
    column = pos[1] + (new_end[1] - end[1]) if pos[0] == end[0] else pos[1]
    return (row, column)


class Buffer:
    """One file's worth of text, its cursor, and its history."""

    def __init__(self, name: str, path: Optional[str] = None,
                 lines: Optional[list[str]] = None) -> None:
        self.name = name
        self.path = path
        self.lines = lines if lines is not None else [""]
        #: What separated the lines on disk, so that a file written on Windows
        #: goes back the way it came instead of being quietly rewritten by the
        #: autosave half a second after it is opened.
        self.newline = "\n"
        self.point: Pos = (0, 0)
        self.mark: Optional[Pos] = None
        self.mark_active = False
        self.modified = False
        #: Bumped by every edit, so that a command can be asked afterwards
        #: whether it changed anything -- which is what deactivates the region.
        self.revision = 0
        #: Display column a vertical move is trying to keep, as Emacs does so
        #: that a trip down through short lines and back up returns to where
        #: it started.  Cleared by anything that is not a vertical move.
        self.goal_column: Optional[int] = None
        #: Scroll position, as a row and which of its wrapped segments is on
        #: the first screen row.
        self.top: tuple[int, int] = (0, 0)
        #: What the file's modification time was when we last agreed with it.
        self.disk_mtime: Optional[float] = None
        #: Set when the file changed underneath us; stops the autosave.
        self.conflict = False
        #: What this buffer is for, when it is not a file.  The only value
        #: that means anything is "grep", which makes RET jump to the hit on
        #: the current line -- the one place this editor has a mode at all.
        self.kind = ""
        self.read_only = False
        #: Where a grep buffer's relative paths are relative to, or the name
        #: of the buffer an occur listing came from.
        self.root = ""
        #: Row -> character ranges to draw as matches.  Only results buffers
        #: use this, and they are read-only, so nothing has to keep it in step
        #: with edits.
        self.highlights: dict[int, tuple[tuple[int, int], ...]] = {}

        self._undo: list[list[Change]] = []
        self._group: list[Change] = []
        self._pending = 0  # Where a run of undo commands has walked back to.

    # -- reading ---------------------------------------------------------

    @property
    def last(self) -> Pos:
        """The end of the buffer."""
        return (len(self.lines) - 1, len(self.lines[-1]))

    def clamp(self, pos: Pos) -> Pos:
        row = max(0, min(pos[0], len(self.lines) - 1))
        return (row, max(0, min(pos[1], len(self.lines[row]))))

    def text_between(self, start: Pos, end: Pos) -> str:
        if start > end:
            start, end = end, start
        if start[0] == end[0]:
            return self.lines[start[0]][start[1]:end[1]]
        parts = [self.lines[start[0]][start[1]:]]
        parts.extend(self.lines[start[0] + 1:end[0]])
        parts.append(self.lines[end[0]][:end[1]])
        return "\n".join(parts)

    def text(self) -> str:
        return "\n".join(self.lines)

    def encoded(self) -> str:
        return self.newline.join(self.lines)

    # -- writing ---------------------------------------------------------

    def edit(self, start: Pos, end: Pos, text: str) -> Pos:
        """Replace ``[start, end)`` with ``text``; return the new end."""
        if self.read_only:
            raise ValueError("Buffer is read-only: %s" % self.name)
        if start > end:
            start, end = end, start
        removed = self.text_between(start, end)
        if not removed and not text:
            return start

        point_before = self.point
        head = self.lines[start[0]][:start[1]]
        tail = self.lines[end[0]][end[1]:]
        middle = (head + text + tail).split("\n")
        self.lines[start[0]:end[0] + 1] = middle
        new_end = advance(start, text)

        self._group.append(Change(start, removed, text, point_before))
        self.modified = True
        self.revision += 1
        self.point = new_end
        if self.mark is not None:
            self.mark = adjust(self.mark, start, end, new_end)
        return new_end

    def insert(self, text: str) -> None:
        self.edit(self.point, self.point, text)

    def delete(self, start: Pos, end: Pos) -> str:
        removed = self.text_between(start, end)
        self.edit(start, end, "")
        return removed

    # -- undo ------------------------------------------------------------

    def reset_history(self) -> None:
        """Forget the undo history, for when the text is replaced wholesale."""
        self._undo.clear()
        self._group.clear()
        self._pending = 0

    def close_group(self) -> None:
        """End the run of changes that one command, or one burst of typing,
        should undo together."""
        if self._group:
            self._undo.append(self._group)
            self._group = []

    def undo(self, continuing: bool) -> bool:
        """Undo one group.  Returns False when there is nothing left.

        This is Emacs' undo rather than the usual undo/redo pair, and the
        difference is worth the dozen lines it costs: undoing is itself a
        change, so it is recorded like any other.  Walk back through history
        with repeated undos; type anything else and undo once more, and what
        you are now undoing is the undo -- which is how Emacs redoes without
        ever having a redo key.
        """
        self.close_group()
        if not continuing:
            self._pending = len(self._undo)
        if self._pending <= 0:
            return False

        group = self._undo[self._pending - 1]
        for change in reversed(group):
            self.edit(change.start,
                      advance(change.start, change.inserted),
                      change.removed)
        self.point = self.clamp(group[0].point_before)
        self.close_group()
        self._pending -= 1
        return True

    # -- files -----------------------------------------------------------

    @classmethod
    def from_file(cls, path: str) -> "Buffer":
        path = os.path.abspath(path)
        name = os.path.basename(path) or path
        try:
            with open(path, "r", encoding="utf-8", errors="surrogateescape",
                      newline="") as handle:
                content = handle.read()
        except FileNotFoundError:
            buffer = cls(name, path)  # A new file, as Emacs would open it.
            return buffer
        buffer = cls(name, path)
        if "\r\n" in content:
            buffer.newline = "\r\n"
            content = content.replace("\r\n", "\n")
        buffer.lines = content.split("\n")
        buffer.disk_mtime = os.path.getmtime(path)
        return buffer

    def externally_changed(self) -> bool:
        """Has the file moved out from under us since we last wrote it?"""
        if self.path is None:
            return False
        if self.disk_mtime is None:
            # A buffer visiting a file that did not exist.  If it exists now,
            # somebody else made it, and it is not ours to overwrite.
            return os.path.exists(self.path)
        try:
            return os.path.getmtime(self.path) != self.disk_mtime
        except OSError:
            return False

    def save(self) -> None:
        """Write the buffer to its file, replacing it atomically.

        The temporary file has to live in the same directory as the target for
        ``os.replace`` to be a rename rather than a copy, and its permissions
        have to be put back afterwards because it is created private.
        """
        if self.path is None:
            raise ValueError("buffer has no file")
        directory = os.path.dirname(self.path) or "."
        temporary = os.path.join(directory, ".bkmacs-%d.tmp" % os.getpid())
        try:
            mode = None
            try:
                mode = os.stat(self.path).st_mode
            except OSError:
                pass
            with open(temporary, "w", encoding="utf-8",
                      errors="surrogateescape", newline="") as handle:
                handle.write(self.encoded())
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.modified = False
        self.conflict = False
        try:
            self.disk_mtime = os.path.getmtime(self.path)
        except OSError:
            self.disk_mtime = time.time()


class KillRing:
    """Emacs' clipboard: a ring, so that ``M-y`` can walk back through it."""

    LIMIT = 60

    def __init__(self) -> None:
        self.entries: list[str] = []
        self.index = 0

    def kill(self, text: str, append: bool = False, prepend: bool = False) -> None:
        """Add ``text``, or fold it into the last kill.

        Consecutive ``C-k`` builds one entry rather than many, so that killing
        four lines and yanking brings back four lines; killing backwards folds
        in at the front for the same reason.
        """
        if not text:
            return
        if (append or prepend) and self.entries:
            existing = self.entries[0]
            self.entries[0] = existing + text if append else text + existing
        else:
            self.entries.insert(0, text)
            del self.entries[self.LIMIT:]
        self.index = 0

    def current(self) -> Optional[str]:
        if not self.entries:
            return None
        return self.entries[self.index]

    def rotate(self) -> Optional[str]:
        if not self.entries:
            return None
        self.index = (self.index + 1) % len(self.entries)
        return self.entries[self.index]
