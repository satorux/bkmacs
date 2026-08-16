"""Drawing the Emacs screen: text, mode line, echo area.

The screen is Emacs' -- the bottom row is the echo area and the minibuffer,
the row above it is the mode line in reverse video, and everything above that
is the buffer.  Lines longer than the window wrap, with a ``\\`` in the last
column of every row that continues, and the space past the end of the buffer
is left blank rather than filled with anything.

Scrolling follows ``scroll-step 1``: when the cursor leaves the window, the
window moves one row to follow it, and only if one row is not enough does the
cursor get recentered.  That is Emacs' rule when ``scroll-step`` is set, and it
is also the reason this module never has to scroll by an arbitrary amount --
reading down a long file keeps the text moving one line at a time, which is
the whole point of setting it.
"""

from __future__ import annotations

import curses
from functools import lru_cache
from typing import Optional

from . import markdown
from .buffer import Buffer, Pos
from .layout import (Expanded, char_width, display_width, expand, pad,
                     split_columns, truncate)

#: Italics are not in the terminfo of every terminal, and ncurses is old enough
#: in places not to have the attribute at all.  Underline stands in: emphasis
#: has to differ from ordinary text somehow, and the alternative is bold, which
#: is what strong emphasis already is.
ITALIC = getattr(curses, "A_ITALIC", curses.A_UNDERLINE)

#: The foreground colours, in the order their pairs are allocated.  Both
#: palettes are drawn from these six; which of them is used is the whole of the
#: difference between a light terminal and a dark one.  ``color_pair`` cannot
#: be called until curses has started, so the numbers are turned into
#: attributes in :meth:`Display._palette` rather than here.
COLORS = (curses.COLOR_RED, curses.COLOR_CYAN, curses.COLOR_MAGENTA,
          curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE)

#: A row of the buffer and which of its wrapped segments is meant.
ScreenPos = tuple[int, int]


@lru_cache(maxsize=2048)
def layout_line(text: str, width: int) -> tuple[Expanded, tuple]:
    """Expand and wrap one line, remembering the result.

    Redisplay runs on every keystroke and almost every line on the screen is
    unchanged from the last one, so this cache turns most of redisplay into
    dictionary lookups.
    """
    expanded = expand(text)
    return expanded, tuple(split_columns(expanded.text, width))


def segment_count(buffer: Buffer, width: int, row: int) -> int:
    return len(layout_line(buffer.lines[row], width)[1])


def next_screen(buffer: Buffer, width: int, pos: ScreenPos) -> Optional[ScreenPos]:
    row, segment = pos
    if segment + 1 < segment_count(buffer, width, row):
        return (row, segment + 1)
    if row + 1 < len(buffer.lines):
        return (row + 1, 0)
    return None


def previous_screen(buffer: Buffer, width: int, pos: ScreenPos) -> Optional[ScreenPos]:
    row, segment = pos
    if segment > 0:
        return (row, segment - 1)
    if row > 0:
        return (row - 1, segment_count(buffer, width, row - 1) - 1)
    return None


def screen_rows(buffer: Buffer, width: int, top: ScreenPos,
                count: int) -> list[ScreenPos]:
    """The positions filling ``count`` screen rows starting at ``top``."""
    rows: list[ScreenPos] = []
    position: Optional[ScreenPos] = top
    while position is not None and len(rows) < count:
        rows.append(position)
        position = next_screen(buffer, width, position)
    return rows


def back_screen(buffer: Buffer, width: int, pos: ScreenPos,
                count: int) -> ScreenPos:
    for _ in range(count):
        previous = previous_screen(buffer, width, pos)
        if previous is None:
            return pos
        pos = previous
    return pos


def segment_point(buffer: Buffer, width: int, position: ScreenPos) -> Pos:
    """The buffer position a screen row begins at."""
    row, index = position
    expanded, segments = layout_line(buffer.lines[row], width)
    start = segments[index].start
    if start < len(expanded.owners):
        return (row, expanded.owners[start])
    return (row, len(buffer.lines[row]))


def point_screen(buffer: Buffer, width: int, point: Pos) -> ScreenPos:
    """Which screen row of its line a position is on."""
    row, column = point
    row = max(0, min(row, len(buffer.lines) - 1))
    expanded, segments = layout_line(buffer.lines[row], width)
    target = expanded.columns[min(column, len(expanded.columns) - 1)]
    for index, segment in enumerate(segments):
        if target < segment.column + segment.width:
            return (row, index)
    return (row, len(segments) - 1)


class Window:
    """A viewport onto a buffer.

    Point and the scroll position live on the buffer, not here, because that
    is where every command expects to find them.  A window keeps its own copy
    only for while it is not the selected one -- saved when it is left, put
    back when it is returned to -- which is what lets two windows show the
    same buffer at two different places.
    """

    def __init__(self, index: int, top: ScreenPos = (0, 0),
                 point: Pos = (0, 0)) -> None:
        self.index = index  #: Which buffer, by position in the editor's list.
        self.top = top
        self.point = point


class Display:
    """Everything that puts characters on the terminal."""

    #: Never a background color, only a foreground one.  Painting a background
    #: of our own is what cannot be made to work: black on yellow reads on a
    #: dark terminal and vanishes on a light one, where yellow is a dark olive.
    #: This is also why grep's colors have survived every terminal anyone has
    #: ever used.
    #:
    #: Foreground alone is not enough on its own, though, which is the thing
    #: it is tempting to assume.  Measured against Terminal.app's own palette,
    #: green is 6.5:1 on black and 3.3:1 on white; blue is 12.8:1 on white and
    #: 1.6:1 on black.  No color in the sixteen clears 4.5:1 on both, because
    #: half of them are light inks and half are dark ones.  So the terminal is
    #: asked which it has -- see :func:`bkmacs.term.background_is_light` -- and
    #: the light half of the palette is used against dark backgrounds and the
    #: dark half against light ones.  Dark is assumed when it will not say,
    #: since that is what a terminal has usually been.
    #:
    #: Trailing whitespace is underlined rather than reversed or colored.  A
    #: space has no glyph to color, bold does not show on one either, and
    #: reverse turns a line holding nothing but its old indentation -- most of
    #: the blank lines in most code -- into a solid bar across the screen.
    TRAILING = curses.A_UNDERLINE
    PAREN = curses.A_BOLD
    MATCH = curses.A_BOLD

    #: Markdown, on a terminal that has no colours to give.  The structure a
    #: README has -- headings, code, the two halves of a link -- survives in
    #: attributes alone, which is not as good and is much better than nothing.
    MARKDOWN = {
        markdown.HEADING: curses.A_BOLD,
        markdown.FENCE: curses.A_BOLD,
        markdown.CODE: curses.A_NORMAL,
        markdown.LINK: curses.A_NORMAL,
        markdown.URL: curses.A_UNDERLINE,
        markdown.MARKER: curses.A_BOLD,
        markdown.STRONG: curses.A_BOLD,
        markdown.EMPHASIS: ITALIC,
    }

    def __init__(self, stdscr, light: bool = False) -> None:
        self.stdscr = stdscr
        self._size = self.size()
        # curses.wrapper has already called start_color, which leaves color
        # pair 0 meaning white on black -- so ncurses writes an explicit
        # white-on-black for ordinary text and paints over whatever background
        # the terminal was set to.  use_default_colors makes pair 0 mean "the
        # colors the terminal already had", which is what leaves the theme
        # alone.  Everything after it depends on it having worked, so the
        # attribute-only values above stand as the fallback.
        try:
            curses.use_default_colors()
            for pair, color in enumerate(COLORS, start=1):
                curses.init_pair(pair, color, -1)
            self._palette(light)
        except curses.error:
            pass

    def _palette(self, light: bool) -> None:
        """The colors, chosen for the background the terminal turned out to have.

        Contrast ratios below are against Terminal.app's palette on its own
        white and its own black, which is the pair this was checked on.

        A light background is the awkward one, because the colors that survive
        it are the three dark inks -- blue, magenta, red -- and there are more
        things here that want a color than there are of them.  Two give way.
        Bullets and quote markers lose their color rather than take yellow at
        3:1, since a ``-`` at the start of a line is already where a bullet
        goes and bold is enough to finish the job; and links share magenta with
        headings, which are whole lines where a link is a few words inside one.
        """
        red, cyan, magenta, green, yellow, blue = (
            curses.color_pair(pair) for pair in range(1, len(COLORS) + 1))
        bold, underline = curses.A_BOLD, curses.A_UNDERLINE
        if light:
            self.MATCH = red | bold  # 4.8:1, and grep's own colour.
            self.PAREN = blue | bold  # 8.6:1, where cyan would be 1.6:1.
            self.MARKDOWN = dict(self.MARKDOWN, **{
                markdown.HEADING: magenta | bold,  # 3.8:1
                markdown.CODE: blue,  # 12.8:1, where green would be 3.3:1.
                markdown.FENCE: blue | bold,
                markdown.LINK: magenta,  # 5.9:1
                markdown.URL: magenta | underline,
                markdown.MARKER: bold,
            })
        else:
            self.MATCH = red | bold  # 4.3:1
            self.PAREN = cyan | bold  # 13.3:1
            self.MARKDOWN = dict(self.MARKDOWN, **{
                markdown.HEADING: magenta | bold,  # 5.5:1
                markdown.CODE: green,  # 6.5:1
                markdown.FENCE: green | bold,
                markdown.LINK: cyan,  # 7.1:1
                markdown.URL: cyan | underline,
                markdown.MARKER: yellow,  # 6.9:1
            })

    # -- geometry --------------------------------------------------------

    def size(self) -> tuple[int, int]:
        height, width = self.stdscr.getmaxyx()
        return max(3, height), max(2, width)

    def text_height(self) -> int:
        return self.size()[0] - 2

    def areas(self, count: int, height: int) -> list[tuple[int, int]]:
        """Where each window starts and how many text rows it gets.

        The rows are shared out evenly rather than remembered per window, so a
        split survives the terminal being resized without any bookkeeping.
        Every window pays for its own mode line; the echo area comes off the
        bottom first.
        """
        available = max(1, height - 1)
        areas: list[tuple[int, int]] = []
        y = 0
        for index in range(count):
            share = available // count + (1 if index < available % count else 0)
            areas.append((y, max(1, share - 1)))
            y += share
        return areas

    def scroll(self, buffer: Buffer, width: int, height: int,
               top: ScreenPos, point: Pos) -> ScreenPos:
        """Where the window has to start for the cursor to be in it."""
        top = (max(0, min(top[0], len(buffer.lines) - 1)), top[1])
        if top[1] >= segment_count(buffer, width, top[0]):
            top = (top[0], 0)

        target = point_screen(buffer, width, point)
        if target in screen_rows(buffer, width, top, height):
            return top

        # One row, the way scroll-step asks.
        stepped = (previous_screen(buffer, width, top) if target < top
                   else next_screen(buffer, width, top))
        if stepped is not None:
            if target in screen_rows(buffer, width, stepped, height):
                return stepped

        # One row was not enough, so recenter -- again, what Emacs does.
        return back_screen(buffer, width, target, height // 2)

    # -- drawing ---------------------------------------------------------

    def render(self, windows: list[Window], buffers: list[Buffer],
               current: int, message: str = "",
               minibuffer: Optional[tuple[str, str, int]] = None,
               parens: tuple[Pos, ...] = ()) -> None:
        height, width = self.size()
        if (height, width) != self._size:
            # After a resize curses' idea of the screen and the screen itself
            # have diverged, and a diff against the wrong picture leaves
            # rubbish behind.  Repaint the lot.
            self._size = (height, width)
            self.stdscr.clearok(True)
        self.stdscr.erase()
        cursor = None

        for index, (window, (origin, rows_high)) in enumerate(
                zip(windows, self.areas(len(windows), height))):
            buffer = buffers[window.index]
            selected = index == current
            point = buffer.point if selected else buffer.clamp(window.point)
            top = self.scroll(buffer, width, rows_high,
                              buffer.top if selected else window.top, point)
            if selected:
                buffer.top = top
            else:
                window.top, window.point = top, point

            rows = screen_rows(buffer, width, top, rows_high)
            region = self._region(buffer) if selected else None
            fences = self._fences(buffer)
            at = point_screen(buffer, width, point)
            for offset, position in enumerate(rows):
                self._draw_row(origin + offset, width, buffer, position,
                               region, parens if selected else (), fences)
                if selected and position == at:
                    cursor = (origin + offset,
                              self._cursor_column(buffer, width, position,
                                                  point))
            # Reverse video for both, bold for the selected one.  Not dim
            # for the other: terminals implement dim by pulling the foreground
            # towards the background, which over reverse video paints the bar
            # in its own background color and makes it disappear.
            self._draw_bar(origin + rows_high, width,
                           self._mode_line(buffer, width, rows, point),
                           curses.A_REVERSE | (curses.A_BOLD if selected
                                               else 0))

        if minibuffer is not None:
            shown, column = self._minibuffer(width, *minibuffer)
            self._draw_bar(height - 1, width, shown, curses.A_NORMAL)
            cursor = (height - 1, column)
        else:
            self._draw_bar(height - 1, width, message, curses.A_NORMAL)

        if cursor is not None:
            y, x = cursor
            if x >= width:  # A line that filled the row exactly.
                y, x = min(y + 1, height - 2), 0
            try:
                self.stdscr.move(max(0, min(y, height - 1)),
                                 max(0, min(x, width - 1)))
            except curses.error:
                pass
        self.stdscr.refresh()

    def _region(self, buffer: Buffer) -> Optional[tuple[Pos, Pos]]:
        if not buffer.mark_active or buffer.mark is None:
            return None
        start, end = buffer.mark, buffer.point
        return (start, end) if start <= end else (end, start)

    def _fences(self, buffer: Buffer) -> Optional[list[bool]]:
        """Which of a Markdown buffer's lines are inside a code fence.

        ``None`` for every other buffer, which is what turns the colouring off
        for them.  Recomputed on every frame rather than cached: it is one
        comparison per line and only Markdown buffers pay it, where a cache
        would have to be told about every edit, every undo, and every reread of
        the file from disk -- three chances to leave the screen wrong.
        """
        if buffer.kind or not markdown.is_markdown(buffer.name):
            return None
        return markdown.fenced(buffer.lines)

    def _draw_row(self, y: int, width: int, buffer: Buffer, position: ScreenPos,
                  region: Optional[tuple[Pos, Pos]],
                  parens: tuple[Pos, ...],
                  fences: Optional[list[bool]] = None) -> None:
        row, index = position
        line = buffer.lines[row]
        expanded, segments = layout_line(line, width)
        segment = segments[index]
        text = expanded.text[segment.start:segment.end]
        if not text:
            return

        trailing = len(line.rstrip(" \t"))
        highlight = {column for (parenthesis_row, column) in parens
                     if parenthesis_row == row}
        matches = buffer.highlights.get(row, ())
        styled = () if fences is None else markdown.spans(line, fences[row])

        # Group characters into runs sharing one attribute; a per-character
        # addstr would be a few thousand curses calls per keystroke.  What the
        # cascade below says, in order, is that what a character is matters
        # less than what is being done to it: Markdown is what the text is,
        # trailing whitespace is a warning about it, a search match and a paren
        # are answers to something just typed, and the region is the thing the
        # next command is about to act on.
        runs: list[tuple[str, int]] = []
        for offset, character in enumerate(text):
            source = expanded.owners[segment.start + offset]
            attribute = curses.A_NORMAL
            for start, end, kind in styled:
                if start <= source < end:
                    attribute = self.MARKDOWN[kind]
                    break
            if source >= trailing and trailing < len(line):
                attribute = self.TRAILING
            if any(start <= source < end for start, end in matches):
                attribute = self.MATCH
            if source in highlight:
                attribute = self.PAREN
            if region is not None and region[0] <= (row, source) < region[1]:
                attribute = curses.A_REVERSE
            if runs and runs[-1][1] == attribute:
                runs[-1] = (runs[-1][0] + character, attribute)
            else:
                runs.append((character, attribute))

        x = 0
        for run, attribute in runs:
            if x >= width:
                break
            visible = truncate(run, width - x)
            if visible:
                try:
                    self.stdscr.addstr(y, x, visible, attribute)
                except curses.error:
                    pass  # The bottom-right cell always refuses.
            x += display_width(visible)

        if segment.end < len(expanded.text):
            try:
                self.stdscr.insstr(y, width - 1, "\\")
            except curses.error:
                pass

    def _cursor_column(self, buffer: Buffer, width: int,
                       position: ScreenPos, point: Pos) -> int:
        row, column = point
        expanded, segments = layout_line(buffer.lines[row], width)
        target = expanded.columns[min(column, len(expanded.columns) - 1)]
        return target - segments[position[1]].column

    def _mode_line(self, buffer: Buffer, width: int,
                   rows: list[ScreenPos], point: Pos) -> str:
        """Emacs' mode line, which says more than it looks like it does.

        ``-UUU:---`` is four separate things.  The leading ``-`` is the input
        method, none being active.  ``UUU`` is the coding system in three
        places -- keyboard, terminal, file -- and U is UTF-8, which here it
        always is.  The ``:`` after them is the end-of-line convention, and it
        turns into ``\\`` for a file that came with CRLF, which is the one
        character of that group carrying real news.  Then the remote-file
        indicator, always ``-`` since nothing here opens files over a network,
        and then ``--`` unmodified, ``**`` modified, ``%%`` read-only.

        ``F1`` is the frame identification: on a terminal Emacs numbers its
        frames, because a terminal can hold several.  This has one, so it
        always says F1 -- it is here because the line looks wrong without it.
        """
        row, column = point
        expanded, _ = layout_line(buffer.lines[row], width)
        display_column = expanded.columns[min(column, len(expanded.columns) - 1)]

        end_of_line = "\\" if buffer.newline == "\r\n" else ":"
        if buffer.read_only:
            flags = "%%"
        elif buffer.modified:
            flags = "**"
        else:
            flags = "--"
        name = buffer.name + " " * max(0, 12 - display_width(buffer.name))
        where = self._where(buffer, rows)
        left = "-UUU%s-%s  F1  %s   %s   (%d,%d)      (%s)" % (
            end_of_line, flags, name, where + " " * max(0, 3 - len(where)),
            row + 1, display_column, self._mode_name(buffer))
        if buffer.conflict:
            left += " [disk changed]"
        return left + " " + "-" * max(0, width - display_width(left) - 1)

    def _mode_name(self, buffer: Buffer) -> str:
        if buffer.kind == "grep":
            return "Grep"
        if buffer.kind == "occur":
            return "Occur"
        if buffer.name == "*Completions*":
            return "Completion List"
        if buffer.encrypted:
            # Worth a word of its own on the line: this is the one kind of
            # buffer that is not being autosaved, so ``**`` on it means what
            # it means everywhere else and nowhere else here -- unsaved work.
            return "Encrypted"
        if markdown.is_markdown(buffer.name):
            return "Markdown"
        return "Fundamental"

    def _where(self, buffer: Buffer, rows: list[ScreenPos]) -> str:
        if not rows:
            return "All"
        first, last = rows[0], rows[-1]
        at_top = first == (0, 0)
        at_bottom = last[0] == len(buffer.lines) - 1
        if at_top and at_bottom:
            return "All"
        if at_top:
            return "Top"
        if at_bottom:
            return "Bot"
        return "%d%%" % (100 * first[0] // max(1, len(buffer.lines) - 1))

    def _minibuffer(self, width: int, prompt: str, content: str,
                    index: int) -> tuple[str, int]:
        """Fit a prompt and its answer on one row, cursor included.

        Emacs grows the minibuffer window over several lines; there is only
        one line here, so the answer scrolls sideways instead.  Without this,
        typing a path longer than the screen means typing blind: the end of
        what you wrote and the cursor are both off the right edge.  The prompt
        stays put, since it is what tells you which question you are answering.
        """
        available = max(1, width - 1)
        column = display_width(prompt) + display_width(content[:index])
        start = 0
        while column >= available and start < index:
            column -= char_width(content[start])
            start += 1
        return prompt + content[start:], max(0, min(column, available - 1))

    def _draw_bar(self, y: int, width: int, text: str, attribute: int) -> None:
        """Fill a whole row, working around the unwritable last cell."""
        head = pad(truncate(text, width - 1), width - 1)
        try:
            self.stdscr.addstr(y, 0, head, attribute)
        except curses.error:
            pass
        try:
            self.stdscr.insstr(y, width - 1, " ", attribute)
        except curses.error:
            pass
