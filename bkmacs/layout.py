"""Measuring and fitting text to terminal columns.

Japanese makes this non-obvious: a character can occupy two columns, or zero
when it is a combining mark, so every width here is measured rather than
counted from the length of a string.

An editor needs two things a viewer does not.  It has to show characters that
have no width of their own -- a tab reaches to the next tab stop, a control
character shows as ``^A``, a byte that is not valid UTF-8 shows as ``\\303`` --
and it has to map back and forth between a position in the text and a position
on the screen, because that is where the cursor goes.  Both are handled by
:func:`expand`, which turns one line of the buffer into what is actually drawn
plus a column for every character that went into it.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_left, bisect_right
from functools import lru_cache
from typing import NamedTuple

#: Columns between tab stops.  Emacs' default, and not worth configuring.
TAB_WIDTH = 8


def char_width(character: str) -> int:
    """Terminal columns one character occupies."""
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(character) for character in text)


def truncate(text: str, columns: int) -> str:
    """Cut ``text`` to fit ``columns``, without splitting a wide character."""
    if columns <= 0:
        return ""
    used = 0
    for index, character in enumerate(text):
        width = char_width(character)
        if used + width > columns:
            return text[:index]
        used += width
    return text


def pad(text: str, columns: int) -> str:
    """Fit ``text`` to exactly ``columns`` terminal columns."""
    text = truncate(text, columns)
    return text + " " * (columns - display_width(text))


class Expanded(NamedTuple):
    """One line of the buffer, ready to be drawn."""

    text: str  #: What to draw: tabs, control characters and raw bytes resolved.
    columns: list[int]  #: Display column each source character starts at, plus
    #: a final entry for the position just past the end -- the cursor is placed
    #: through this list, which is why it needs that entry.
    owners: list[int]  #: Source character each drawn character came from, so
    #: that a highlight expressed in buffer positions can be drawn.


@lru_cache(maxsize=4096)
def expand(text: str, tab_width: int = TAB_WIDTH) -> Expanded:
    """Render one line for display, and say where each character landed.

    Everything with a width that is not simply the character's own is resolved
    here, so that what comes back can be measured with :func:`char_width`
    alone.  Invalid bytes arrive as the surrogates that ``surrogateescape``
    produced when the file was read; they are shown in octal, the way Emacs
    shows a raw byte, and they survive back to disk untouched.
    """
    if text.isascii() and text.isprintable():
        # Which is most lines, and every line of a minified file: each
        # character stands for itself and is one column wide, so the answer
        # is arithmetic rather than a walk with a width lookup per character.
        return Expanded(text, list(range(len(text) + 1)),
                        list(range(len(text))))

    pieces: list[str] = []
    columns: list[int] = []
    owners: list[int] = []
    column = 0
    for index, character in enumerate(text):
        columns.append(column)
        if character == "\t":
            span = tab_width - column % tab_width
            piece = " " * span
        else:
            code = ord(character)
            if 0xDC80 <= code <= 0xDCFF:  # A byte that was not valid UTF-8.
                piece = "\\%03o" % (code - 0xDC00)
            elif code < 32:
                piece = "^" + chr(code + 64)
            elif code == 0x7F:
                piece = "^?"
            else:
                piece = character
        pieces.append(piece)
        owners.extend([index] * len(piece))
        column += display_width(piece)
    columns.append(column)
    return Expanded("".join(pieces), columns, owners)


def index_at_column(columns: list[int], column: int) -> int:
    """The character sitting at a display column, for vertical movement.

    A cursor cannot come to rest inside a wide character or a tab, so this
    answers with the last character that starts at or before the column --
    which is also what makes a trip down through short lines and back up land
    where it started.
    """
    return max(0, bisect_right(columns, column) - 1)


def span_at_columns(columns: list[int], left: int, right: int) -> tuple[int, int]:
    """The characters lying between two display columns, for a rectangle.

    A rectangle is cut by column rather than by character, and a wide
    character can straddle the cut.  Rather than split one down the middle,
    both edges move to the nearest character boundary inside the rectangle,
    so what comes back is what the rectangle covers whole.  A rectangle with
    no width covers nothing, which is what makes it a place to insert.
    """
    begin = min(bisect_left(columns, left), len(columns) - 1)
    if right <= left:
        return (begin, begin)
    return (begin, max(begin, bisect_right(columns, right) - 1))


class Segment(NamedTuple):
    """One screen row's worth of a line that had to be continued."""

    start: int  #: Index into the expanded text.
    end: int  #: One past the last index into the expanded text.
    column: int  #: Display column the segment begins at within the line.
    width: int  #: Columns the segment occupies.


def split_columns(expanded: str, width: int) -> list[Segment]:
    """Break an expanded line into the screen rows it wraps onto.

    A continued row gives up its last column to the ``\\`` that marks the
    continuation, so it holds ``width - 1`` columns of text; the row that ends
    the line keeps all of them.  A wide character that will not fit in what is
    left simply starts the next row, which leaves the column before the marker
    blank -- Emacs does the same.

    How much of the line is left is counted rather than measured.  Measuring
    it -- ``display_width(expanded[start:])`` once per row -- is a pass over
    the rest of the line for every row of it, which is quadratic and is only
    invisible while lines are short: a line of four thousand characters took
    38 milliseconds to lay out that way, one of sixty-four thousand took
    thirteen seconds, and a minified file was not openable at all.  The
    columns already used come to the same number by subtraction, since every
    character is counted into exactly one row.
    """
    if width <= 1:
        width = 2  # Degenerate, but never divide the screen into nothing.
    if not expanded:
        return [Segment(0, 0, 0, 0)]

    segments: list[Segment] = []
    start = 0
    column = 0
    length = len(expanded)
    total = display_width(expanded)
    while start < length:
        remaining = total - column
        if remaining <= width:
            segments.append(Segment(start, length, column, remaining))
            break
        end = start
        used = 0
        while end < length:
            step = char_width(expanded[end])
            if used + step > width - 1:
                break
            used += step
            end += 1
        if end == start:
            end = start + 1  # A wide character in a one-column space.
            used = char_width(expanded[start])
        segments.append(Segment(start, end, column, used))
        start = end
        column += used
    return segments

#: Characters that may not begin a line, and characters that may not end one.
#: Japanese line breaking has no spaces to break on, so it breaks between
#: characters -- but not just anywhere: a closing bracket or a full stop
#: cannot start a line, and an opening bracket cannot end one.  This is the
#: useful half of what typesetters call kinsoku shori.
NO_BREAK_BEFORE = "、。，．,.:;：；!?！？)]}）］｝」』】〕》〉ー〜…ゝゞ々ぁぃぅぇぉっゃゅょァィゥェォッャュョ"
NO_BREAK_AFTER = "([{（［｛「『【〔《〈"


def joinable(text: str, index: int) -> bool:
    """May the line break so that ``text[index]`` starts the next one?"""
    before, after = text[index - 1], text[index]
    if after in NO_BREAK_BEFORE or before in NO_BREAK_AFTER:
        return False
    if before == " ":
        return True
    # No space between them, so this is only a break if one side is Japanese.
    return char_width(before) == 2 or char_width(after) == 2


def unfill(lines: list[str]) -> str:
    """Join filled lines back into one, ready to be filled again.

    A newline becomes a space, except between two wide characters, where
    Japanese never had a space and would be wrong to gain one.
    """
    joined = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not joined:
            joined = line
        elif char_width(joined[-1]) == 2 and char_width(line[0]) == 2:
            joined += line
        else:
            joined += " " + line
    return joined


def fill(text: str, columns: int) -> list[str]:
    """Break one long line into lines of at most ``columns`` columns.

    Latin breaks at spaces and Japanese between characters, measured rather
    than counted, and a run with nowhere legal to break is allowed to overrun
    rather than be cut in the middle of a word.
    """
    text = text.strip()
    if not text:
        return [""]
    lines: list[str] = []
    while True:
        if display_width(text) <= columns:
            lines.append(text)
            return lines

        used, limit = 0, len(text)
        for index, character in enumerate(text):
            width = char_width(character)
            if used + width > columns:
                limit = index
                break
            used += width

        cut = next((index for index in range(limit, 0, -1)
                    if joinable(text, index)), 0)
        if cut == 0:
            # Nowhere legal to break within the width; take the first chance
            # after it rather than splitting a word down the middle.
            cut = next((index for index in range(limit + 1, len(text))
                        if joinable(text, index)), len(text))
        lines.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
        if not text:
            return lines
