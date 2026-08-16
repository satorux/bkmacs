"""Markdown, coloured only as far as writing a README needs.

This is the one file type this editor knows anything about, and it knows the
least it can get away with.  There is no parser here and no document tree:
:func:`spans` takes one line and returns the runs of it that should be drawn
differently, which is all the display can use anyway.  Everything that would
need a document -- reference definitions resolving to their links, list nesting,
what is a paragraph -- is left out, because none of it changes how a line looks
while you are typing it.

Only one construct in Markdown genuinely spans lines: the fenced code block.
:func:`fenced` walks the buffer once and says, for each line, whether it is
inside one, and that single boolean is the only context :func:`spans` is given.
That is what keeps the per-line work cacheable, and it is the whole reason the
split is drawn here rather than anywhere else.

The colours are not decided here either -- a span carries a kind, a string, and
the display turns kinds into terminal attributes.  Nothing in this module
imports curses, so all of it can be tested without a terminal.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple, Optional

#: What a run of a line is, for the display to colour.  Markers -- the ``#`` of
#: a heading, the ``-`` of a bullet, the ``>`` of a quote -- are covered by the
#: span of the thing they mark rather than being a kind of their own, except
#: where the marker is the whole of it: a bullet marks a line whose text is
#: ordinary text, and colouring that text would colour most of the file.
HEADING = "heading"
CODE = "code"  #: An inline code span, or a line inside a fence.
FENCE = "fence"  #: The ``` line itself, opening or closing.
LINK = "link"  #: The ``[text]`` half of a link.
URL = "url"  #: The ``(...)`` half, an autolink, or a bare one.
MARKER = "marker"  #: A bullet, a quote's ``>``, a thematic break.
STRONG = "strong"
EMPHASIS = "emphasis"


class Span(NamedTuple):
    """A run of one line to draw differently."""

    start: int  #: Character index into the line, not a display column.
    end: int  #: One past the last character.
    kind: str


#: Three or more backticks or tildes, indented no further than a code block
#: would be.  The info string after an opening fence is not looked at: an
#: opening fence and a closing one are drawn the same way, so nothing here has
#: to tell them apart -- :func:`fenced` does it by counting instead.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")

#: ``#`` through ``######``, and the ``===`` that underlines a setext heading.
#: The ``---`` that underlines the other kind is not here: it is far more often
#: a thematic break, and telling them apart needs the line before, which is the
#: one piece of context this module has decided not to have.
_HEADING = re.compile(r" {0,3}#{1,6}(\s|$)")
_SETEXT = re.compile(r" {0,3}=+\s*$")

_RULE = re.compile(r" {0,3}([-*_])[ \t]*(\1[ \t]*){2,}$")
_QUOTE = re.compile(r"( {0,3}>[ \t]?)+")
_BULLET = re.compile(r"[ \t]*([-*+]|\d{1,9}[.)])(?=[ \t])")

#: A bare URL in running text.  Trailing punctuation is trimmed afterwards,
#: since a sentence ending in a link puts its full stop against the URL.
_BARE = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCTUATION = ".,;:!?"


def is_markdown(name: str) -> bool:
    """Is a buffer of this name worth colouring?"""
    return name.lower().endswith((".md", ".markdown"))


def fenced(lines: list[str]) -> list[bool]:
    """Which lines lie inside a fenced code block, the fences themselves too.

    A fence that is never closed leaves the rest of the buffer inside it, which
    looks alarming and is exactly right: that is what the file says, and seeing
    it is how you notice the missing fence.
    """
    states: list[bool] = []
    inside = False
    for line in lines:
        if _FENCE.match(line):
            states.append(True)
            inside = not inside
        else:
            states.append(inside)
    return states


@lru_cache(maxsize=2048)
def spans(line: str, inside_fence: bool = False) -> tuple[Span, ...]:
    """The runs of one line to draw differently, left to right and disjoint.

    Cached the way :func:`bkmacs.display.layout_line` is cached, and for the
    same reason: redisplay runs on every keystroke, and all but one line on the
    screen is the same line it was before.
    """
    if inside_fence:
        if not line:
            return ()
        return (Span(0, len(line), FENCE if _FENCE.match(line) else CODE),)
    if _HEADING.match(line) or _SETEXT.match(line):
        return (Span(0, len(line), HEADING),)
    if _RULE.match(line):
        return (Span(0, len(line), MARKER),)

    found: list[Span] = []
    start = 0
    quote = _QUOTE.match(line)
    if quote:
        found.append(Span(0, quote.end(), MARKER))
        start = quote.end()
    bullet = _BULLET.match(line, start)
    if bullet:
        found.append(Span(bullet.start(1), bullet.end(1), MARKER))
        start = bullet.end(1)
    _inline(line, start, found)
    return tuple(found)


def _inline(line: str, start: int, found: list[Span]) -> None:
    """Walk a line once, taking the first construct that opens at each point.

    One pass rather than a regexp per construct, because the order matters and
    a pass makes it obvious: a backtick opens a code span before anything
    inside it can mean anything, and a backslash takes the character after it
    out of the running entirely.
    """
    index = start
    length = len(line)
    while index < length:
        character = line[index]
        if character == "\\":
            index += 2
            continue
        if character == "`":
            end = _code(line, index)
            if end:
                found.append(Span(index, end, CODE))
                index = end
                continue
        elif character == "[" or (character == "!"
                                  and line[index + 1:index + 2] == "["):
            link = _link(line, index)
            if link:
                found.extend(link)
                index = link[-1].end
                continue
        elif character == "<":
            end = _autolink(line, index)
            if end:
                found.append(Span(index, end, URL))
                index = end
                continue
        elif character == "h" and (index == 0 or not line[index - 1].isalnum()):
            bare = _BARE.match(line, index)
            if bare:
                end = bare.end()
                while end > index and line[end - 1] in _TRAILING_PUNCTUATION:
                    end -= 1
                found.append(Span(index, end, URL))
                index = end
                continue
        elif character in "*_":
            emphasis = _emphasis(line, index)
            if emphasis:
                found.append(emphasis)
                index = emphasis.end
                continue
        index += 1


def _code(line: str, start: int) -> int:
    """The end of a code span opening at ``start``, or 0 if it never closes.

    A run of backticks is closed by a run of exactly the same length, which is
    how ``` ``a ` b`` ``` puts a backtick inside a code span.  A longer run is
    not a closer, so it is stepped over whole rather than matched against.
    """
    run = 0
    while start + run < len(line) and line[start + run] == "`":
        run += 1
    closer = "`" * run
    index = start + run
    while True:
        at = line.find(closer, index)
        if at < 0:
            return 0
        index = at
        while index < len(line) and line[index] == "`":
            index += 1
        if index - at == run:
            return index


def _matching(line: str, start: int, opening: str, closing: str) -> int:
    """One past the bracket closing the one at ``start``, or 0 if unmatched."""
    if line[start:start + 1] != opening:
        return 0
    depth = 0
    index = start
    while index < len(line):
        character = line[index]
        if character == "\\":
            index += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return 0


def _link(line: str, start: int) -> tuple[Span, ...]:
    """``[text](url)`` or ``![alt](url)``, as its two halves.

    Both halves are coloured, differently: what you check when you are editing
    a README is that the right words point at the right address, and that is
    much easier to do when you can see where one ends and the other begins.
    """
    bracket = start + 1 if line[start] == "!" else start
    text = _matching(line, bracket, "[", "]")
    if not text:
        return ()
    target = _matching(line, text, "(", ")")
    if not target:
        return ()
    return (Span(start, text, LINK), Span(text, target, URL))


def _autolink(line: str, start: int) -> int:
    """The end of a ``<https://...>``, or 0 if that is not what this is."""
    at = line.find(">", start)
    if at < 0:
        return 0
    inner = line[start + 1:at]
    if not inner or any(character.isspace() for character in inner):
        return 0
    return at + 1 if inner.startswith(("http://", "https://")) else 0


def _emphasis(line: str, start: int) -> Optional[Span]:
    """``*text*`` or ``**text**``, markers included, or ``None``.

    The two rules worth having out of CommonMark's several are here.  A marker
    with a space after it does not open anything, so the ``*`` in ``2 * 3`` is
    multiplication; and an underscore with a word character on the outside of
    it does not either, so ``snake_case_names`` are left alone.  Asterisks have
    no such rule -- inside a word they really do emphasise.
    """
    character = line[start]
    run = 2 if line[start + 1:start + 2] == character else 1
    if character == "_" and start and _word(line[start - 1]):
        return None
    body = start + run
    if body >= len(line) or line[body].isspace():
        return None

    index = body
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line.startswith(character * run, index):
            end = index + run
            if not line[index - 1].isspace() and not (
                    character == "_" and _word(line[end:end + 1])):
                return Span(start, end, STRONG if run == 2 else EMPHASIS)
            index = end
            continue
        index += 1
    return None


def _word(character: str) -> bool:
    return bool(character) and (character.isalnum() or character == "_")
