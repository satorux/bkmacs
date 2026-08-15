"""The commands, the key bindings, and the loop that runs them.

Key bindings are Emacs', with two changes carried over from the ``~/.emacs``
this was written from: ``C-h`` deletes backwards like DEL, since there is no
help system for it to prefix, and ``C-\\`` undoes, for terminals that will not
send ``C-/``.  Nothing here is configurable, on purpose -- the defaults are the
code.

Commands are looked up by name rather than called directly from the key table,
which costs one dictionary lookup and buys the "C-x C-j is undefined" message,
the undo grouping, and somewhere obvious to hang ``M-x`` if it is ever wanted.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .buffer import Buffer, KillRing, Pos, advance, adjust
from .display import (Display, Window, back_screen, point_screen, screen_rows,
                      segment_point)
from .history import History
from .layout import display_width, expand, fill, index_at_column, pad, unfill
from .search import glob_regexp, search_backward, search_forward
from .term import Terminal, with_meta

#: How long the buffer has to sit still before it is written to its file.
#: Emacs' auto-save-visited-interval, set to the same 0.5 seconds.
AUTOSAVE_MS = 500

#: Where M-q wraps to.  Emacs' default is 70; the ~/.emacs this was written
#: from sets 74, so 74 it is.
FILL_COLUMN = 74

#: What a filled line begins with and keeps.  Emacs calls this the adaptive
#: fill prefix, and the two kinds of it behave oppositely: a comment marker is
#: repeated on every line, because a comment that stops being one no longer
#: compiles, while a bullet belongs to the first line alone and the lines under
#: it get the same width of blank instead.
COMMENT = re.compile(r"[ \t]*(?:[#;>]+|//+|--)[ \t]*")
BULLET = re.compile(r"[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
INDENT = re.compile(r"[ \t]*")

#: Columns TAB inserts.  Two because the ~/.emacs this was written from sets
#: every language to two, and without major modes there is nothing else to go
#: on.
TAB_STOP = 2

OPENERS = "([{"
CLOSERS = ")]}"
PAIRS = {")": "(", "]": "[", "}": "{"}
#: Characters a parenthesis scan will look at before giving up, so that an
#: unbalanced file cannot make a keystroke take a noticeable amount of time.
SCAN_LIMIT = 20000

#: Commands whose kills join onto the previous one, so that four C-k in a row
#: yank back as four lines rather than one.
KILL_COMMANDS = {"kill-line", "kill-region", "kill-word", "backward-kill-word"}

#: Commands that leave the region alone.  Anything else deactivates it, as
#: transient-mark-mode does.
MARK_SAFE = {"set-mark-command", "exchange-point-and-mark", "kill-ring-save",
             "isearch-forward", "isearch-backward"}

#: Directories grep walks straight past.
GREP_SKIP = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv",
             ".mypy_cache", ".pytest_cache", ".tox", "eln-cache"}
GREP_LIMIT = 2000  # Matches, after which the search stops and says so.
GREP_MAX_BYTES = 4_000_000  # Anything larger is not what you were grepping for.

#: ``path:line:text``, the format grep has always used and the one the *grep*
#: buffer parses back to jump to a hit.
HIT = re.compile(r"(.+?):(\d+):")

#: ``   12:text``, which is how occur numbers the lines of one buffer.
OCCUR = re.compile(r"\s*(\d+):")


def is_word(character: str) -> bool:
    return character.isalnum() or character == "_"


def prefix_of(line: str) -> str:
    """What this line starts with that a fill has to preserve."""
    for pattern in (COMMENT, BULLET, INDENT):
        found = pattern.match(line)
        if found and found.group(0):
            return found.group(0)
    return ""


def continuation_of(opening: str) -> str:
    """What the lines under ``opening`` start with, given only that line.

    A comment marker carries down; a bullet does not, and becomes the blank
    that lines its text up underneath itself.
    """
    if COMMENT.match(opening) and COMMENT.match(opening).group(0) == opening:
        return opening
    return " " * display_width(opening)


def abbreviate(path: str) -> str:
    """Emacs' abbreviate-file-name: the home directory written as ``~``.

    Only for showing a path to somebody.  Everything that acts on one expands
    it again, so the two never disagree -- and a prompt that starts at ``~/``
    is a prompt you can read the end of on an eighty-column screen.
    """
    home = os.path.expanduser("~")
    if home and home != os.sep and (path == home
                                    or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def spans_of(regexp, line: str, offset: int) -> tuple:
    """Where every match sits in a results line, once the prefix is allowed for.

    Emacs highlights what matched rather than just the line it was on, which
    is the difference between reading a screenful of results and searching it
    again with your eyes.
    """
    return tuple((offset + found.start(), offset + found.end())
                 for found in regexp.finditer(line) if found.end() > found.start())


def in_columns(items: list[str], width: int) -> list[str]:
    """Lay a list out in columns, measured rather than counted."""
    step = max(display_width(item) for item in items) + 2
    per_row = max(1, width // step)
    return ["".join(pad(item, step) for item in items[start:start + per_row]
                    ).rstrip()
            for start in range(0, len(items), per_row)]


class Editor:
    """One session: the buffers, the screen, and the key loop."""

    BINDINGS = {
        "C-f": "forward-char", "C-b": "backward-char",
        "C-n": "next-line", "C-p": "previous-line",
        "C-a": "move-beginning-of-line", "C-e": "move-end-of-line",
        "M-f": "forward-word", "M-b": "backward-word",
        "C-M-n": "forward-list", "C-M-p": "backward-list",
        "M-<": "beginning-of-buffer", "M->": "end-of-buffer",
        "C-v": "scroll-up", "M-v": "scroll-down", "C-l": "recenter",
        "M-g": "goto-line",
        "RET": "newline", "C-j": "newline-and-indent", "TAB": "indent-for-tab",
        "C-o": "open-line", "C-t": "transpose-chars",
        "C-d": "delete-char",
        "DEL": "backward-delete-char", "C-h": "backward-delete-char",
        "M-d": "kill-word", "M-DEL": "backward-kill-word",
        "C-k": "kill-line",
        "C-w": "kill-region", "M-w": "kill-ring-save",
        "C-y": "yank", "M-y": "yank-pop",
        "C-@": "set-mark-command",
        "C-s": "isearch-forward", "C-r": "isearch-backward",
        "M-%": "query-replace", "M-q": "fill-paragraph",
        "C-/": "undo", "C-_": "undo", "C-\\": "undo",
        "C-g": "keyboard-quit",
        "C-z": "suspend",
        "M-x": "execute-extended-command",
        "<resize>": "ignore",
    }

    #: Commands with no key of their own, reachable through M-x.
    EXTRA = ("grep", "occur", "next-error", "revert-buffer")

    CTRL_X = {
        "C-f": "find-file", "C-s": "save-buffer",
        "C-c": "save-buffers-kill-terminal",
        "b": "switch-to-buffer", "k": "kill-buffer", "C-b": "list-buffers",
        "C-x": "exchange-point-and-mark", "u": "undo",
        " ": "fixup-whitespace", "`": "next-error",
        "2": "split-window-below", "1": "delete-other-windows",
        "0": "delete-window", "o": "other-window",
        "C-u": "upcase-region", "C-l": "downcase-region",
    }

    def __init__(self, stdscr, paths: list[str], warning: str = "") -> None:
        self.term = Terminal(stdscr)
        self.display = Display(stdscr)
        self.kill_ring = KillRing()
        self.history = History()
        self.buffers: list[Buffer] = [Buffer.from_file(path) for path in paths]
        if not self.buffers:
            self.buffers.append(Buffer("*scratch*"))
        self.windows = [Window(0)]
        self.current = 0
        self.previous = 0
        self.message = warning
        self.running = True
        self.last_command = ""
        self.pending: Optional[str] = None
        self._yank_start: Optional[Pos] = None
        #: The window *Completions* is borrowing, and what to put back there.
        self._completions: Optional[tuple[int, Optional[tuple]]] = None
        #: The last *grep* or *Occur* buffer, and whether any of its hits
        #: have been visited yet -- the first next-error should go to the one
        #: under point rather than past it.  Held as the buffer itself, so
        #: that killing some other buffer cannot renumber it out from under.
        self._results: Optional[Buffer] = None
        self._results_fresh = False

    @property
    def index(self) -> int:
        return self.windows[self.current].index

    @property
    def buffer(self) -> Buffer:
        return self.buffers[self.index]

    def redisplay(self, message: Optional[str] = None,
                  minibuffer: Optional[tuple[str, str, int]] = None,
                  parens: Optional[tuple[Pos, ...]] = None) -> None:
        self.display.render(
            self.windows, self.buffers, self.current,
            self.message if message is None else message, minibuffer,
            self.matching_parens() if parens is None else parens)

    def prefix(self, label: str) -> Optional[str]:
        """Show a half-typed key sequence and wait for the rest of it.

        Emacs echoes ``C-x-`` while it waits, which is the difference between
        a prefix key and a hung editor.
        """
        self.redisplay(message=label)
        return self.term.read_key()

    # -- the loop --------------------------------------------------------

    def run(self) -> None:
        while self.running:
            self.redisplay()
            key = self.pending or self.read_key()
            self.pending = None
            if key is not None:
                self.dispatch(key)

    def read_key(self) -> Optional[str]:
        """Wait for a key, waking up to save if the typing stops."""
        waiting = [b for b in self.buffers
                   if b.modified and b.path is not None and not b.conflict]
        key = self.term.read_key(AUTOSAVE_MS if waiting else None)
        if key is None:
            for buffer in waiting:
                self.autosave(buffer)
        return key

    def autosave(self, buffer: Buffer) -> None:
        if buffer.externally_changed():
            buffer.conflict = True
            self.message = ("%s changed on disk; autosave stopped"
                            % buffer.name)
            return
        try:
            buffer.save()
        except OSError as error:
            buffer.conflict = True
            self.message = "Autosave failed: %s" % error

    def dispatch(self, key: str) -> None:
        self.message = ""
        if key == "ESC":
            # ESC is a prefix key with no clock on it, which is the only way
            # Meta can be typed on a terminal that will not send it: Option
            # unmapped on macOS, or a browser tab that keeps Alt for itself.
            # ESC ESC quits, for the same reason C-g cannot be relied on.
            second = self.prefix("ESC-")
            if second is None:
                return
            if second == "ESC":
                self.invoke("keyboard-quit")
                return
            self.dispatch(second if second.startswith(("M-", "C-M-"))
                          else with_meta(second))
            return
        if key == "C-x":
            second = self.prefix("C-x-")
            if second is None:
                return
            name = self.CTRL_X.get(second)
            if name is None:
                self.message = "C-x %s is undefined" % second
                self.last_command = ""
                return
            self.invoke(name)
        elif key in self.BINDINGS:
            self.invoke(self.BINDINGS[key])
        elif len(key) == 1 and key.isprintable():
            self.self_insert(key)
        else:
            self.message = "%s is undefined" % key
            self.last_command = ""

    def invoke(self, name: str) -> None:
        buffer = self.buffer
        revision = buffer.revision
        try:
            getattr(self, name.replace("-", "_"))()
        except Exception as error:  # An editor that dies loses the buffer.
            self.message = "%s: %s" % (type(error).__name__, error)
        self.after(name, buffer, revision)

    def self_insert(self, character: str) -> None:
        buffer = self.buffer
        revision = buffer.revision
        try:
            buffer.insert(character)
        except ValueError as error:
            self.message = str(error)
        self.after("self-insert", buffer, revision)

    def commands(self) -> list[str]:
        names = set(self.BINDINGS.values()) | set(self.CTRL_X.values())
        names.update(self.EXTRA)
        names.discard("prefix")
        names.discard("ignore")
        return sorted(names)

    def execute_extended_command(self) -> None:
        name = self.read_string("M-x ", completer=self.complete_command,
                                kind="command")
        if not name:
            return
        name = name.strip()
        if name not in self.commands():
            self.message = "[No match]"
            return
        self.invoke(name)

    def complete_command(self, text: str) -> tuple[Optional[str], list[str]]:
        matches = [name for name in self.commands() if name.startswith(text)]
        if not matches:
            self.message = "No match"
            return None, []
        return os.path.commonprefix(matches), matches

    def after(self, name: str, buffer: Buffer, revision: int) -> None:
        """Housekeeping every command needs: undo grouping, mark, goal column."""
        if name != "self-insert":
            buffer.close_group()
        if name not in ("next-line", "previous-line"):
            buffer.goal_column = None
        if buffer.revision != revision and name not in MARK_SAFE:
            buffer.mark_active = False
        self.last_command = name

    def ignore(self) -> None:
        pass

    # -- positions -------------------------------------------------------

    def char_after(self, pos: Pos) -> Optional[str]:
        row, column = pos
        line = self.buffer.lines[row]
        if column < len(line):
            return line[column]
        if row + 1 < len(self.buffer.lines):
            return "\n"
        return None

    def forward(self, pos: Pos) -> Optional[Pos]:
        row, column = pos
        if column < len(self.buffer.lines[row]):
            return (row, column + 1)
        if row + 1 < len(self.buffer.lines):
            return (row + 1, 0)
        return None

    def backward(self, pos: Pos) -> Optional[Pos]:
        row, column = pos
        if column > 0:
            return (row, column - 1)
        if row > 0:
            return (row - 1, len(self.buffer.lines[row - 1]))
        return None

    def display_column(self, pos: Pos) -> int:
        columns = expand(self.buffer.lines[pos[0]]).columns
        return columns[min(pos[1], len(columns) - 1)]

    # -- moving ----------------------------------------------------------

    def forward_char(self) -> None:
        moved = self.forward(self.buffer.point)
        if moved is None:
            self.message = "End of buffer"
        else:
            self.buffer.point = moved

    def backward_char(self) -> None:
        moved = self.backward(self.buffer.point)
        if moved is None:
            self.message = "Beginning of buffer"
        else:
            self.buffer.point = moved

    def vertical(self, step: int) -> None:
        buffer = self.buffer
        row, _ = buffer.point
        if not 0 <= row + step < len(buffer.lines):
            self.message = "End of buffer" if step > 0 else "Beginning of buffer"
            return
        if buffer.goal_column is None:
            buffer.goal_column = self.display_column(buffer.point)
        columns = expand(buffer.lines[row + step]).columns
        buffer.point = (row + step, index_at_column(columns, buffer.goal_column))

    def next_line(self) -> None:
        self.vertical(1)

    def previous_line(self) -> None:
        self.vertical(-1)

    def move_beginning_of_line(self) -> None:
        self.buffer.point = (self.buffer.point[0], 0)

    def move_end_of_line(self) -> None:
        row = self.buffer.point[0]
        self.buffer.point = (row, len(self.buffer.lines[row]))

    def forward_word(self) -> None:
        position = self.buffer.point
        while True:
            character = self.char_after(position)
            if character is None:
                break
            if is_word(character):
                break
            position = self.forward(position) or position
        while True:
            character = self.char_after(position)
            if character is None or not is_word(character):
                break
            moved = self.forward(position)
            if moved is None:
                break
            position = moved
        self.buffer.point = position

    def backward_word(self) -> None:
        position = self.buffer.point
        while True:
            previous = self.backward(position)
            if previous is None:
                break
            if is_word(self.char_after(previous) or "\n"):
                break
            position = previous
        while True:
            previous = self.backward(position)
            if previous is None:
                break
            if not is_word(self.char_after(previous) or "\n"):
                break
            position = previous
        self.buffer.point = position

    # -- over parentheses ------------------------------------------------

    def forward_list(self) -> None:
        """``C-M-n``: over the next balanced group, however deep it nests."""
        position: Optional[Pos] = self.buffer.point
        depth, budget = 0, SCAN_LIMIT
        while position is not None and budget:
            character = self.char_after(position)
            if character is None:
                break
            if character in OPENERS:
                depth += 1
            elif character in CLOSERS:
                depth -= 1
                if depth == 0:
                    self.buffer.point = self.forward(position) or position
                    return
                if depth < 0:
                    break  # Out through the group we started inside.
            position = self.forward(position)
            budget -= 1
        self.message = "Scan error: Unbalanced parentheses"

    def backward_list(self) -> None:
        """``C-M-p``: the same, the other way."""
        position = self.buffer.point
        depth, budget = 0, SCAN_LIMIT
        while budget:
            previous = self.backward(position)
            if previous is None:
                break
            character = self.char_after(previous)
            if character in CLOSERS:
                depth += 1
            elif character in OPENERS:
                depth -= 1
                if depth == 0:
                    self.buffer.point = previous
                    return
                if depth < 0:
                    break
            position = previous
            budget -= 1
        self.message = "Scan error: Unbalanced parentheses"

    def beginning_of_buffer(self) -> None:
        self.buffer.mark = self.buffer.point
        self.buffer.point = (0, 0)

    def end_of_buffer(self) -> None:
        self.buffer.mark = self.buffer.point
        self.buffer.point = self.buffer.last

    def scroll_up(self) -> None:
        buffer = self.buffer
        height, width = self.window_height(), self.display.size()[1]
        step = max(1, height - 2)  # Emacs keeps two lines of context.
        rows = screen_rows(buffer, width, buffer.top, step + 1)
        if len(rows) <= step:
            if buffer.point == buffer.last:
                self.message = "End of buffer"
            buffer.point = buffer.last
            return
        buffer.top = rows[step]
        if point_screen(buffer, width, buffer.point) < buffer.top:
            buffer.point = segment_point(buffer, width, buffer.top)

    def scroll_down(self) -> None:
        buffer = self.buffer
        height, width = self.window_height(), self.display.size()[1]
        step = max(1, height - 2)
        if buffer.top == (0, 0):
            if buffer.point == (0, 0):
                self.message = "Beginning of buffer"
            buffer.point = (0, 0)
            return
        buffer.top = back_screen(buffer, width, buffer.top, step)
        rows = screen_rows(buffer, width, buffer.top, height)
        if rows and point_screen(buffer, width, buffer.point) not in rows:
            buffer.point = segment_point(buffer, width, rows[-1])

    def recenter(self) -> None:
        buffer = self.buffer
        width = self.display.size()[1]
        buffer.top = back_screen(buffer, width,
                                 point_screen(buffer, width, buffer.point),
                                 self.window_height() // 2)

    def goto_line(self) -> None:
        answer = self.read_string("Goto line: ", kind="goto")
        if not answer:
            return
        try:
            number = int(answer.strip())
        except ValueError:
            self.message = "Not a number"
            return
        row = max(0, min(number - 1, len(self.buffer.lines) - 1))
        self.buffer.point = (row, 0)

    # -- changing --------------------------------------------------------

    def newline(self) -> None:
        if self.buffer.kind in ("grep", "occur"):
            self.goto_hit()
            return
        self.buffer.insert("\n")

    def newline_and_indent(self) -> None:
        line = self.buffer.lines[self.buffer.point[0]]
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        self.buffer.insert("\n" + indent[:self.buffer.point[1]])

    def indent_for_tab(self) -> None:
        """Two spaces.  Never a tab character -- indent-tabs-mode is off.

        Two rather than a tab stop because that is what the ~/.emacs this
        was written from sets for every language, and there is no major mode
        here to ask.
        """
        self.buffer.insert(" " * TAB_STOP)

    def fill_paragraph(self) -> None:
        """``M-q``: rewrap the paragraph around point to 74 columns.

        The paragraph is what lies between the blank lines above and below.
        Its first line keeps whatever it starts with; the lines after it take
        the prefix of the second line, which is what makes a bullet stay a
        bullet and a hanging indent stay hung.  A one-line paragraph that
        starts with a bullet gets that width back as spaces, for the same
        reason.
        """
        buffer = self.buffer
        row = buffer.point[0]
        if not buffer.lines[row].strip():
            self.message = "Nothing to fill"
            return

        start = row
        while start > 0 and buffer.lines[start - 1].strip():
            start -= 1
        end = row
        while end + 1 < len(buffer.lines) and buffer.lines[end + 1].strip():
            end += 1
        block = buffer.lines[start:end + 1]

        opening = prefix_of(block[0])
        # With a second line there is no guessing: whatever it starts with is
        # what the author meant the rest to start with.
        following = (prefix_of(block[1]) if len(block) > 1
                     else continuation_of(opening))
        body = unfill([line[len(prefix_of(line)):] for line in block])
        if not body:
            return

        width = max(8, FILL_COLUMN - display_width(following))
        filled = fill(body, width)
        lines = [opening + filled[0]] + [following + line
                                         for line in filled[1:]]
        if lines == block:
            self.message = "Paragraph is already filled"
            return
        buffer.edit((start, 0), (end, len(buffer.lines[end])),
                    "\n".join(lines))
        buffer.point = buffer.clamp((start + len(lines) - 1, 0))

    def open_line(self) -> None:
        point = self.buffer.point
        self.buffer.insert("\n")
        self.buffer.point = point

    def transpose_chars(self) -> None:
        """Swap the two characters around point, and step past them.

        The typo this fixes is usually noticed a moment too late, so Emacs
        makes the end of a line mean the two characters before it rather than
        nothing at all -- ``teh`` becomes ``the`` with one key, without going
        back for it.  A newline is a character like any other here, which is
        how a stray character at the start of a line gets pulled up onto the
        end of the line above.
        """
        buffer = self.buffer
        point = buffer.point
        if point[1] == len(buffer.lines[point[0]]):
            moved = self.backward(point)
            if moved is None:
                self.message = "Beginning of buffer"
                return
            point = moved
        before, after = self.backward(point), self.forward(point)
        if before is None:
            self.message = "Beginning of buffer"
            return
        if after is None:
            self.message = "End of buffer"
            return
        buffer.edit(before, after, buffer.text_between(point, after)
                    + buffer.text_between(before, point))

    def delete_char(self) -> None:
        end = self.forward(self.buffer.point)
        if end is None:
            self.message = "End of buffer"
            return
        self.buffer.edit(self.buffer.point, end, "")

    def backward_delete_char(self) -> None:
        """DEL and C-h, deleting a tab as the spaces it was showing."""
        buffer = self.buffer
        row, column = buffer.point
        if column == 0:
            if row == 0:
                self.message = "Beginning of buffer"
                return
            buffer.edit((row - 1, len(buffer.lines[row - 1])), (row, 0), "")
            return
        line = buffer.lines[row]
        if line[column - 1] == "\t":
            columns = expand(line).columns
            span = columns[column] - columns[column - 1]
            buffer.edit((row, column - 1), (row, column), " " * (span - 1))
            return
        buffer.edit((row, column - 1), (row, column), "")

    def kill_line(self) -> None:
        buffer = self.buffer
        row, column = buffer.point
        line = buffer.lines[row]
        if column >= len(line):
            if row + 1 >= len(buffer.lines):
                self.message = "End of buffer"
                return
            text, end = "\n", (row + 1, 0)
        else:
            text, end = line[column:], (row, len(line))
        self.kill_ring.kill(text, append=self.last_command in KILL_COMMANDS)
        buffer.edit(buffer.point, end, "")

    def kill_word(self) -> None:
        start = self.buffer.point
        self.forward_word()
        end = self.buffer.point
        self.buffer.point = start
        self.kill_ring.kill(self.buffer.text_between(start, end),
                            append=self.last_command in KILL_COMMANDS)
        self.buffer.edit(start, end, "")

    def backward_kill_word(self) -> None:
        end = self.buffer.point
        self.backward_word()
        start = self.buffer.point
        self.kill_ring.kill(self.buffer.text_between(start, end),
                            prepend=self.last_command in KILL_COMMANDS)
        self.buffer.edit(start, end, "")

    def region(self) -> Optional[tuple[Pos, Pos]]:
        buffer = self.buffer
        if buffer.mark is None:
            self.message = "No mark set in this buffer"
            return None
        start, end = buffer.mark, buffer.point
        return (start, end) if start <= end else (end, start)

    def kill_region(self) -> None:
        span = self.region()
        if span is None:
            return
        self.kill_ring.kill(self.buffer.text_between(*span),
                            append=self.last_command in KILL_COMMANDS)
        self.buffer.edit(span[0], span[1], "")

    def kill_ring_save(self) -> None:
        span = self.region()
        if span is None:
            return
        self.kill_ring.kill(self.buffer.text_between(*span))
        self.buffer.mark_active = False
        self.message = "Saved"

    def yank(self) -> None:
        text = self.kill_ring.current()
        if text is None:
            self.message = "Kill ring is empty"
            return
        self.buffer.mark = self.buffer.point
        self._yank_start = self.buffer.point
        self.buffer.insert(text)

    def yank_pop(self) -> None:
        if self.last_command not in ("yank", "yank-pop") or self._yank_start is None:
            self.message = "Previous command was not a yank"
            return
        text = self.kill_ring.rotate()
        if text is None:
            return
        start = self._yank_start
        self.buffer.edit(start, self.buffer.point, text)
        self.buffer.mark = start

    def set_mark_command(self) -> None:
        self.buffer.mark = self.buffer.point
        self.buffer.mark_active = True
        self.message = "Mark set"

    def exchange_point_and_mark(self) -> None:
        buffer = self.buffer
        if buffer.mark is None:
            self.message = "No mark set in this buffer"
            return
        buffer.mark, buffer.point = buffer.point, buffer.clamp(buffer.mark)
        buffer.mark_active = True

    def keyboard_quit(self) -> None:
        self.buffer.mark_active = False
        self.message = "Quit"

    def undo(self) -> None:
        continuing = self.last_command == "undo"
        if self.buffer.undo(continuing):
            self.message = "Undo!"
        else:
            self.message = "No further undo information"

    def fixup_whitespace(self) -> None:
        """Collapse the whitespace around point to the one space it needs."""
        buffer = self.buffer
        row, column = buffer.point
        line = buffer.lines[row]
        start = column
        while start > 0 and line[start - 1] in " \t":
            start -= 1
        end = column
        while end < len(line) and line[end] in " \t":
            end += 1
        wanted = ""
        if start > 0 and end < len(line):
            before, after = line[start - 1], line[end]
            if before not in OPENERS and after not in CLOSERS:
                wanted = " "
        buffer.edit((row, start), (row, end), wanted)

    def upcase_region(self) -> None:
        self.case_region(str.upper)

    def downcase_region(self) -> None:
        self.case_region(str.lower)

    def case_region(self, convert) -> None:
        span = self.region()
        if span is None:
            return
        point = self.buffer.point
        self.buffer.edit(span[0], span[1],
                         convert(self.buffer.text_between(*span)))
        self.buffer.point = self.buffer.clamp(point)

    # -- files and buffers -----------------------------------------------

    def find_file(self) -> None:
        directory = os.path.dirname(self.buffer.path or os.getcwd() + "/")
        answer = self.read_string("Find file: ", abbreviate(directory) + "/",
                                  completer=self.complete_path, kind="file")
        if not answer:
            return
        path = os.path.abspath(os.path.expanduser(answer))
        if not self.open_path(path):
            self.message = "(New file)"

    def open_path(self, path: str) -> bool:
        """Visit ``path``, reusing its buffer if it is already open."""
        for index, buffer in enumerate(self.buffers):
            if buffer.path == path:
                self.select(index)
                return True
        self.buffers.append(Buffer.from_file(path))
        self.select(len(self.buffers) - 1)
        return os.path.exists(path)

    def complete_path(self, text: str) -> tuple[Optional[str], list[str]]:
        directory, slash, prefix = text.rpartition("/")
        root = os.path.expanduser(directory) or "."
        try:
            names = os.listdir(root)
        except OSError:
            self.message = "No such directory"
            return None, []
        matches = sorted(name for name in names if name.startswith(prefix))
        if not matches:
            self.message = "No match"
            return None, []
        completed = directory + slash + os.path.commonprefix(matches)
        if len(matches) == 1 and os.path.isdir(os.path.expanduser(completed)):
            completed += "/"
        # A trailing slash in the listing says which ones are directories.
        listed = [name + "/" if os.path.isdir(os.path.join(root, name))
                  else name for name in matches]
        return completed, listed

    def save_buffer(self) -> None:
        buffer = self.buffer
        if buffer.path is None:
            answer = self.read_string("File to save in: ",
                                      abbreviate(os.getcwd()) + "/",
                                      completer=self.complete_path,
                                      kind="file")
            if not answer:
                self.message = "Save cancelled"
                return
            buffer.path = os.path.abspath(os.path.expanduser(answer))
            buffer.name = os.path.basename(buffer.path)
        if not buffer.modified and not buffer.conflict:
            self.message = "(No changes need to be saved)"
            return
        if buffer.externally_changed():
            question = ("%s has changed since visited or saved. Save anyway? "
                        % buffer.name)
            if not self.yes_or_no(question):
                self.message = "Save not confirmed"
                return
        buffer.save()
        self.message = "Wrote %s" % abbreviate(buffer.path)

    def switch_to_buffer(self) -> None:
        default = self.buffers[self.previous].name
        answer = self.read_string("Switch to buffer (default %s): " % default,
                                  completer=self.complete_buffer,
                                  kind="buffer")
        if answer is None:
            return
        name = answer.strip() or default
        for index, buffer in enumerate(self.buffers):
            if buffer.name == name:
                self.select(index)
                return
        self.buffers.append(Buffer(name))
        self.select(len(self.buffers) - 1)

    def complete_buffer(self, text: str) -> tuple[Optional[str], list[str]]:
        matches = sorted(b.name for b in self.buffers
                         if b.name.startswith(text))
        if not matches:
            self.message = "No match"
            return None, []
        return os.path.commonprefix(matches), matches

    def kill_buffer(self) -> None:
        buffer = self.buffer
        if buffer.modified and buffer.path is None:
            if not self.yes_or_no("Buffer %s modified; kill anyway? "
                                  % buffer.name):
                return
        if buffer.modified and buffer.path is not None and not buffer.conflict:
            buffer.save()
        gone = self.index
        del self.buffers[gone]
        if not self.buffers:
            self.buffers.append(Buffer("*scratch*"))
        # Every window pointing past the hole has to be pulled back one.
        for window in self.windows:
            if window.index > gone:
                window.index -= 1
            elif window.index == gone:
                window.index = min(gone, len(self.buffers) - 1)
        self.previous = min(self.previous, len(self.buffers) - 1)

    def list_buffers(self) -> None:
        rows = ["%s %-24s %s" % ("*" if b.modified else " ", b.name,
                                 abbreviate(b.path) if b.path else "")
                for b in self.buffers]
        listing = self.named_buffer("*Buffer List*")
        listing.read_only = False
        listing.lines = [" MR Buffer                   File", ""] + rows
        listing.point = (2, 0)
        listing.top = (0, 0)
        listing.modified = False
        listing.read_only = True

    def buffer_named(self, name: str) -> tuple[int, Buffer]:
        """Find a buffer by name, making it if it is not there.  Shows nothing."""
        for index, buffer in enumerate(self.buffers):
            if buffer.name == name:
                return index, buffer
        self.buffers.append(Buffer(name))
        return len(self.buffers) - 1, self.buffers[-1]

    def named_buffer(self, name: str) -> Buffer:
        index, buffer = self.buffer_named(name)
        self.select(index)
        return buffer

    def display_buffer(self, index: int) -> bool:
        """Show a buffer in another window without leaving this one.

        Emacs' display-buffer: results appear beside what you were doing
        rather than on top of it.  Returns whether the frame had to be split,
        so that whoever asked can put it back.
        """
        split = False
        if len(self.windows) == 1:
            if self.window_height() < 4:
                self.select(index)  # No room to split; take the window.
                return False
            self.split_window_below()
            split = True
        other = (self.current + 1) % len(self.windows)
        window = self.windows[other]
        shown = self.buffers[index]
        # Take the buffer's own place in itself, not the top: a grep buffer
        # arrives with point already on its first hit.
        window.index, window.top, window.point = index, shown.top, shown.point
        return split

    def select(self, index: int) -> None:
        """Show a buffer in the selected window."""
        if index != self.index:
            self.previous = self.index
        self.windows[self.current].index = index

    # -- windows ---------------------------------------------------------

    def window_height(self) -> int:
        """Text rows in the selected window, which is what C-v moves by."""
        areas = self.display.areas(len(self.windows), self.display.size()[0])
        return areas[self.current][1]

    def enter_window(self, index: int) -> None:
        """Leave the selected window and select another.

        Point and the scroll position live on the buffer while a window is
        selected, so they are stashed on the way out and restored on the way
        in.  Without that, two windows on one file could not sit at two
        different places in it.
        """
        window, buffer = self.windows[self.current], self.buffer
        window.top, window.point = buffer.top, buffer.point
        self.current = index
        window, buffer = self.windows[self.current], self.buffer
        buffer.top, buffer.point = window.top, buffer.clamp(window.point)

    def other_window(self) -> None:
        if len(self.windows) < 2:
            self.message = "No other window"
            return
        self.enter_window((self.current + 1) % len(self.windows))

    def split_window_below(self) -> None:
        if self.window_height() < 4:
            self.message = "Window too small to be split"
            return
        buffer = self.buffer
        self.windows.insert(self.current + 1,
                            Window(self.index, buffer.top, buffer.point))

    def delete_window(self) -> None:
        if len(self.windows) < 2:
            self.message = "Attempt to delete minibuffer or sole ordinary window"
            return
        del self.windows[self.current]
        self.current = min(self.current, len(self.windows) - 1)
        window, buffer = self.windows[self.current], self.buffer
        buffer.top, buffer.point = window.top, buffer.clamp(window.point)

    def delete_other_windows(self) -> None:
        self.windows = [self.windows[self.current]]
        self.current = 0

    def save_buffers_kill_terminal(self) -> None:
        for buffer in self.buffers:
            if buffer.modified and buffer.path and not buffer.conflict:
                try:
                    buffer.save()
                except OSError as error:
                    buffer.conflict = True
                    self.message = str(error)
        unsaved = [b for b in self.buffers if b.modified and (b.conflict or b.path)]
        if unsaved:
            names = ", ".join(b.name for b in unsaved)
            if not self.yes_or_no("Modified buffers exist (%s); exit anyway? "
                                  % names):
                return
        self.running = False

    def revert_buffer(self) -> None:
        buffer = self.buffer
        if buffer.path is None:
            self.message = "Buffer does not seem to be associated with a file"
            return
        if buffer.modified and not self.yes_or_no(
                "Revert buffer from file %s? " % buffer.path):
            return
        fresh = Buffer.from_file(buffer.path)
        buffer.lines = fresh.lines
        buffer.newline = fresh.newline
        buffer.disk_mtime = fresh.disk_mtime
        buffer.reset_history()  # The old changes no longer describe this text.
        buffer.point = buffer.clamp(buffer.point)
        buffer.mark, buffer.mark_active = None, False
        buffer.modified = False
        buffer.conflict = False
        self.message = "Reverted %s" % buffer.name

    def suspend(self) -> None:
        self.term.suspend()

    # -- grep ------------------------------------------------------------

    def grep(self) -> None:
        """Search a tree of files, in Python rather than through grep(1).

        Doing it here rather than shelling out means one regexp dialect --
        Python's -- instead of whatever the platform's grep happens to be, no
        quoting to get wrong, and nothing to install.  Case follows the same
        rule as isearch: a pattern typed in lower case ignores case.
        """
        pattern = self.read_string("Grep (regexp): ", kind="grep")
        if not pattern:
            return
        flags = re.IGNORECASE if pattern == pattern.lower() else 0
        try:
            regexp = re.compile(pattern, flags)
        except re.error as error:
            self.message = "Invalid regexp: %s" % error
            return
        # The default goes in the prompt rather than into the input: prefilling
        # it means every real glob has to be typed after deleting a star first,
        # and "*" + "**/*.py" is a pattern that quietly matches nothing.
        glob = self.read_string("In files (glob, default *): ", kind="glob")
        if glob is None:
            return
        glob = glob.strip() or "*"
        # Emacs greps in the buffer's default-directory, and a *grep* buffer's
        # is the tree it searched -- so a second grep from the results stays
        # where the first one was rather than falling back to the cwd.
        root = (os.path.dirname(self.buffer.path) if self.buffer.path
                else self.buffer.root or os.getcwd())

        hits, truncated = self.walk(regexp, glob, root)
        listing_index, listing = self.buffer_named("*grep*")
        listing.read_only = False
        listing.lines = (["grep -n %s %s   (in %s)"
                          % (pattern, glob, abbreviate(root)), ""]
                         + [text for text, _ in hits]
                         + ["",
                            "Grep %s -- %d match%s"
                            % ("truncated" if truncated else "finished",
                               len(hits), "" if len(hits) == 1 else "es")])
        # Two header lines above the first hit, which is where the spans go.
        listing.highlights = {2 + row: spans
                              for row, (_, spans) in enumerate(hits)}
        listing.kind = "grep"
        listing.root = root
        listing.read_only = True
        listing.modified = False
        listing.point = (2 if hits else 0, 0)
        listing.top = (0, 0)
        # Below what you were reading, not instead of it -- and the cursor
        # stays where it was, so C-x ` walks the hits without going anywhere.
        self.display_buffer(listing_index)
        self.record_results(listing)
        self.message = "%d match%s" % (len(hits), "" if len(hits) == 1 else "es")

    def walk(self, regexp, glob: str,
             root: str) -> tuple[list[tuple[str, tuple]], bool]:
        hits: list[tuple[str, tuple]] = []
        matches = glob_regexp(glob)
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = sorted(name for name in subdirectories
                                       if name not in GREP_SKIP)
            for name in sorted(names):
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, root)
                if not matches.match(relative):
                    continue
                try:
                    if os.path.getsize(path) > GREP_MAX_BYTES:
                        continue
                    with open(path, "r", encoding="utf-8",
                              errors="surrogateescape") as handle:
                        for number, line in enumerate(handle, 1):
                            if "\0" in line:
                                break  # Binary; nobody meant to grep this.
                            line = line.rstrip("\n")
                            if regexp.search(line):
                                prefix = "%s:%d:" % (relative, number)
                                hits.append((prefix + line[:500],
                                             spans_of(regexp, line,
                                                      len(prefix))))
                                if len(hits) >= GREP_LIMIT:
                                    return hits, True
                except (OSError, ValueError):
                    continue
        return hits, False

    def occur(self) -> None:
        """``M-x occur``: every line of this buffer matching a regexp.

        The same idea as grep with the search narrowed to one buffer, so it
        shares everything below: the window it appears in, RET, and C-x `.
        The one difference is that a hit points at a buffer and a line rather
        than at a file, since the buffer may never have been a file.
        """
        source = self.buffer
        pattern = self.read_string("List lines matching regexp: ",
                                   kind="search")
        if not pattern:
            return
        flags = re.IGNORECASE if pattern == pattern.lower() else 0
        try:
            regexp = re.compile(pattern, flags)
        except re.error as error:
            self.message = "Invalid regexp: %s" % error
            return

        hits = [("%6d:%s" % (number, line[:500]),
                 spans_of(regexp, line, len("%6d:" % number)))
                for number, line in enumerate(source.lines, 1)
                if regexp.search(line)]
        index, listing = self.buffer_named("*Occur*")
        listing.read_only = False
        listing.lines = (["%d match%s for \"%s\" in buffer: %s"
                          % (len(hits), "" if len(hits) == 1 else "es",
                             pattern, source.name), ""]
                         + [text for text, _ in hits])
        listing.highlights = {2 + row: spans
                              for row, (_, spans) in enumerate(hits)}
        listing.kind = "occur"
        listing.root = source.name
        listing.read_only = True
        listing.modified = False
        listing.point = (2 if hits else 0, 0)
        listing.top = (0, 0)
        self.display_buffer(index)
        self.record_results(listing)
        self.message = "%d match%s" % (len(hits), "" if len(hits) == 1 else "es")

    # -- walking results -------------------------------------------------

    def record_results(self, listing: Buffer) -> None:
        self._results, self._results_fresh = listing, True

    def hit_target(self, listing: Buffer,
                   line: str) -> Optional[tuple[str, int]]:
        """Where one line of a results buffer points, or None if nowhere."""
        if listing.kind == "occur":
            match = OCCUR.match(line)
            return (listing.root, int(match.group(1))) if match else None
        match = HIT.match(line)
        if match is None:
            return None
        return (os.path.abspath(os.path.join(listing.root, match.group(1))),
                int(match.group(2)))

    def visit(self, listing: Buffer, line: str) -> bool:
        target = self.hit_target(listing, line)
        if target is None:
            self.message = "No hit on this line"
            return False
        where, number = target
        if listing.kind == "occur":
            for index, buffer in enumerate(self.buffers):
                if buffer.name == where:
                    self.select(index)
                    break
            else:
                self.message = "No buffer named %s" % where
                return False
        else:
            self.open_path(where)
        row = max(0, min(number - 1, len(self.buffer.lines) - 1))
        self.buffer.point = (row, 0)
        self.recenter()
        return True

    def goto_hit(self) -> None:
        """RET on a line of a results buffer: go to what it found.

        With the frame split, it opens in the other window and the results
        stay put -- which is the whole reason to split it.
        """
        listing = self.buffer
        line = listing.lines[listing.point[0]]
        if self.hit_target(listing, line) is None:
            self.message = "No hit on this line"
            return
        self._results_fresh = False
        if len(self.windows) > 1:
            self.other_window()
        self.visit(listing, line)

    def next_error(self) -> None:
        """``C-x ```: the next hit, without going back to look for it."""
        listing = self._results
        if listing is None or listing not in self.buffers:
            self.message = "No grep or occur buffer"
            return
        showing = [index for index, window in enumerate(self.windows)
                   if self.buffers[window.index] is listing]
        # Point lives on the buffer for the selected window and on the window
        # for any other, so where the last hit was depends on which it is.
        if self.buffer is listing or not showing:
            point = listing.point
        else:
            point = self.windows[showing[0]].point
        # The first next-error after a search visits the hit it is already
        # sitting on; afterwards it moves on to the next one.
        row = point[0] if self._results_fresh else point[0] + 1
        self._results_fresh = False
        while (row < len(listing.lines)
               and self.hit_target(listing, listing.lines[row]) is None):
            row += 1
        if row >= len(listing.lines):
            self.message = "Moved past last match"
            return
        listing.point = (row, 0)
        for index in showing:
            self.windows[index].point = (row, 0)
        # Jump somewhere that is not the window holding the results.
        if showing and self.current in showing and len(self.windows) > 1:
            self.other_window()
        self.visit(listing, listing.lines[row])

    # -- the minibuffer --------------------------------------------------

    def read_string(self, prompt: str, initial: str = "",
                    completer=None, kind: str = "") -> Optional[str]:
        """Read a line in the echo area.  ``None`` means C-g.

        An ambiguous TAB opens a *Completions* window below, the way Emacs
        does, and it is taken down again on the way out -- the echo area is
        one line and the candidates rarely fit on it.
        """
        try:
            answer = self.read_loop(prompt, initial, len(initial), completer,
                                    kind)
        finally:
            self.close_completions()
        if answer and kind:
            self.history.add(kind, answer)
        return answer

    def read_loop(self, prompt: str, text: str, index: int,
                  completer, kind: str) -> Optional[str]:
        note = ""  # "[No match]" and the like, shown next to what was typed.
        walked = -1  # How far back through the ring, -1 being not yet in it.
        typed = text  # What was there before the ring was walked into.
        while True:
            self.redisplay(minibuffer=(prompt, text + note, index), parens=())
            key = self.resolve_meta(prompt, text + note, index)
            if key is None:
                continue
            if key != "TAB":
                note = ""
            if key == "RET":
                return text
            if key in ("C-g", "ESC"):
                self.message = "Quit"
                return None
            if key in ("M-p", "C-p", "M-n", "C-n"):
                ring = self.history.get(kind)
                back = key in ("M-p", "C-p")
                if back and walked + 1 < len(ring):
                    if walked < 0:
                        typed = text
                    walked += 1
                elif not back and walked >= 0:
                    walked -= 1
                else:
                    self.message = ""
                    note = "  [%s]" % ("Beginning of history"
                                       if back else "End of history")
                    continue
                text = ring[walked] if walked >= 0 else typed
                index = len(text)
            elif key in ("DEL", "C-h"):
                if index > 0:
                    text, index = text[:index - 1] + text[index:], index - 1
            elif key == "C-d":
                text = text[:index] + text[index + 1:]
            elif key == "C-a":
                index = 0
            elif key == "C-e":
                index = len(text)
            elif key == "C-b":
                index = max(0, index - 1)
            elif key == "C-f":
                index = min(len(text), index + 1)
            elif key == "C-k":
                text = text[:index]
            elif key == "C-y":
                yanked = self.kill_ring.current() or ""
                text = text[:index] + yanked + text[index:]
                index += len(yanked)
            elif key == "C-z":
                self.term.suspend()
            elif key == "TAB" and completer is not None:
                self.message = ""
                completed, candidates = completer(text)
                note, self.message = self.message, ""
                if note:
                    note = "  [%s]" % note
                if completed is not None:
                    text, index = completed, len(completed)
                if len(candidates) > 1:
                    self.open_completions(candidates)
                else:
                    self.close_completions()
            elif len(key) == 1 and key.isprintable():
                text, index = text[:index] + key + text[index:], index + 1

    def open_completions(self, candidates: list[str]) -> None:
        """Put the candidates in a window below, as Emacs' *Completions* does.

        The echo area is one line; a directory of forty files is not going to
        fit on it, and truncating the list is worse than useless because the
        one you wanted is the one that got cut.
        """
        index, buffer = self.buffer_named("*Completions*")
        buffer.read_only = False
        buffer.lines = (["Possible completions are:"]
                        + in_columns(candidates, self.display.size()[1]))
        buffer.read_only = True
        buffer.modified = False
        buffer.point, buffer.top = (0, 0), (0, 0)
        if self._completions is not None:
            return  # Already on screen; it has just been refilled.

        if len(self.windows) == 1:
            if self.window_height() < 4:
                return  # No room to split; the note in the echo area will do.
            self.split_window_below()
            other = (self.current + 1) % len(self.windows)
            self._completions = (other, None)  # None: this window is ours.
        else:
            other = (self.current + 1) % len(self.windows)
            borrowed = self.windows[other]
            self._completions = (other, (borrowed.index, borrowed.top,
                                         borrowed.point))
        window = self.windows[other]
        window.index, window.top, window.point = index, (0, 0), (0, 0)

    def close_completions(self) -> None:
        if self._completions is None:
            return
        other, borrowed = self._completions
        self._completions = None
        if other >= len(self.windows):
            return
        if borrowed is None:
            del self.windows[other]
            self.current = min(self.current, len(self.windows) - 1)
        else:
            window = self.windows[other]
            window.index, window.top, window.point = borrowed

    def resolve_meta(self, prompt: str, shown: str,
                     index: int) -> Optional[str]:
        """Read a key, letting ESC be the Meta prefix it is everywhere else.

        The minibuffer needs this as much as the buffer does: ``ESC p`` is how
        M-p gets typed on a terminal that will not send Meta, and that is the
        key the history is on.  ESC ESC still quits.
        """
        key = self.term.read_key()
        if key != "ESC":
            return key
        self.redisplay(minibuffer=(prompt, shown + "  ESC-", index), parens=())
        second = self.term.read_key()
        if second is None:
            return None
        if second == "ESC":
            return "ESC"  # Which every caller treats as a quit.
        return (second if second.startswith(("M-", "C-M-"))
                else with_meta(second))

    def yes_or_no(self, question: str) -> bool:
        while True:
            self.redisplay(minibuffer=(question + "(y or n) ", "", 0),
                           parens=())
            key = self.term.read_key()
            if key in ("y", "Y"):
                return True
            if key in ("n", "N", "C-g", "ESC"):
                return False

    # -- searching -------------------------------------------------------

    def isearch_forward(self) -> None:
        self.isearch(True)

    def isearch_backward(self) -> None:
        self.isearch(False)

    def isearch(self, forward: bool) -> None:
        """Incremental search, with the history Emacs keeps so that DEL undoes.

        Each keystroke pushes the pattern, where the search started from and
        what it found; deleting a character pops back to exactly the state
        before it was typed, rather than searching again from scratch.
        """
        buffer = self.buffer
        origin = buffer.point
        recalled = -1  # Position in the saved search ring, if it is walked.
        history: list[tuple[str, Pos, Optional[Pos]]] = [("", origin, None)]
        while True:
            pattern, anchor, found = history[-1]
            if found is not None:
                end = (found[0], found[1] + len(pattern))
                buffer.mark, buffer.mark_active = found, True
                buffer.point = end if forward else found
            prompt = "%sI-search%s: " % (
                "" if found is not None or not pattern else "Failing ",
                "" if forward else " backward")
            self.redisplay(minibuffer=(prompt, pattern, len(pattern)),
                           parens=())
            key = self.resolve_meta(prompt, pattern, len(pattern))
            if key is None:
                continue
            if key in ("C-s", "C-r"):
                forward = key == "C-s"
                if not pattern and len(history) == 1:
                    continue
                start = buffer.point if forward else (found or buffer.point)
                if forward and found is not None:
                    start = (found[0], found[1] + 1)
                hit = (search_forward(buffer, pattern, start) if forward
                       else search_backward(buffer, pattern, start))
                if hit is None:  # Wrap, the way isearch does on a second try.
                    hit = (search_forward(buffer, pattern, (0, 0)) if forward
                           else search_backward(buffer, pattern, buffer.last))
                    self.message = "Wrapped"
                history.append((pattern, start, hit))
            elif key in ("DEL", "C-h"):
                if len(history) > 1:
                    history.pop()
                else:
                    buffer.point = origin
            elif key == "RET":
                break
            elif key in ("C-g", "ESC"):
                buffer.point = origin
                self.message = "Quit"
                break
            elif key == "C-z":
                self.term.suspend()
            elif key in ("M-p", "M-n"):
                ring = self.history.get("search")
                if ring:
                    recalled += 1 if key == "M-p" else -1
                    recalled = max(0, min(recalled, len(ring) - 1))
                    hit = (search_forward(buffer, ring[recalled], origin)
                           if forward
                           else search_backward(buffer, ring[recalled],
                                                buffer.last))
                    history.append((ring[recalled], origin, hit))
            elif len(key) == 1 and key.isprintable():
                extended = pattern + key
                hit = (search_forward(buffer, extended, anchor) if forward
                       else search_backward(buffer, extended,
                                            advance(anchor, extended)))
                history.append((extended, anchor, hit))
            else:
                self.pending = key  # Any other key ends the search and runs.
                break
        if history[-1][0]:
            self.history.add("search", history[-1][0])
        buffer.mark_active = False

    def query_replace(self) -> None:
        """``M-%``.  One undo group for the whole session, so that a run of
        replacements can be taken back with a single ``C-/``."""
        buffer = self.buffer
        old = self.read_string("Query replace: ", kind="search")
        if not old:
            return
        new = self.read_string("Query replace %s with: " % old,
                               kind="replace")
        if new is None:
            return

        start, bound = buffer.point, None
        if buffer.mark_active and buffer.mark is not None:
            span = self.region()
            if span is not None:
                start, bound = span
        buffer.mark_active = False
        buffer.close_group()

        position, replaced, quit_early = start, 0, False
        while not quit_early:
            found = search_forward(buffer, old, position, bound)
            if found is None:
                break
            end = (found[0], found[1] + len(old))
            buffer.point, buffer.mark, buffer.mark_active = found, end, True
            self.redisplay(
                message="Query replacing %s with %s: (y, n, !, ., q)"
                        % (old, new), parens=())
            key = self.term.read_key()
            if key in ("y", " "):
                bound = self.replace_at(found, end, new, bound)
                position, replaced = advance(found, new), replaced + 1
            elif key in ("n", "DEL"):
                position = end
            elif key == "!":
                while found is not None:
                    end = (found[0], found[1] + len(old))
                    bound = self.replace_at(found, end, new, bound)
                    position, replaced = advance(found, new), replaced + 1
                    found = search_forward(buffer, old, position, bound)
                break
            elif key == ".":
                bound = self.replace_at(found, end, new, bound)
                replaced += 1
                break
            elif key in ("q", "RET", "C-g", "ESC"):
                quit_early = True
            elif key == "C-z":
                self.term.suspend()

        buffer.mark_active = False
        buffer.close_group()
        self.message = "Replaced %d occurrence%s" % (
            replaced, "" if replaced == 1 else "s")

    def replace_at(self, start: Pos, end: Pos, new: str,
                   bound: Optional[Pos]) -> Optional[Pos]:
        """Replace one match, dragging the region's end along with it."""
        self.buffer.edit(start, end, new)
        if bound is None:
            return None
        return adjust(bound, start, end, advance(start, new))

    # -- show-paren ------------------------------------------------------

    def matching_parens(self) -> tuple[Pos, ...]:
        buffer = self.buffer
        row, column = buffer.point
        line = buffer.lines[row]
        if column > 0 and line[column - 1] in CLOSERS:
            return self.scan(( row, column - 1), -1)
        if column < len(line) and line[column] in OPENERS:
            return self.scan((row, column), 1)
        return ()

    def scan(self, start: Pos, step: int) -> tuple[Pos, ...]:
        """Walk out from a parenthesis to its partner, ignoring nothing.

        There is no syntax table here, so a parenthesis inside a string or a
        comment counts.  Emacs without a major mode behaves the same way.
        """
        buffer = self.buffer
        wanted = OPENERS if step < 0 else CLOSERS
        depth, budget = 0, SCAN_LIMIT
        position: Optional[Pos] = start
        while position is not None and budget > 0:
            character = self.char_after(position)
            if character is None:
                break
            if character in OPENERS + CLOSERS:
                depth += 1 if (character in OPENERS) == (step > 0) else -1
                if depth == 0:
                    matched = character in wanted
                    opener = (buffer.lines[start[0]][start[1]] if step > 0
                              else character)
                    closer = (character if step > 0
                              else buffer.lines[start[0]][start[1]])
                    if matched and PAIRS.get(closer) == opener:
                        return (start, position)
                    return ()
            budget -= 1
            position = self.forward(position) if step > 0 else self.backward(position)
        return ()
