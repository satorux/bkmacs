"""End-to-end tests: the real editor, on a real terminal, driven by keystrokes.

The editor is started under a pseudo-terminal and typed at, and what it did is
checked by looking at the file it was editing.  That covers the parts unit
tests cannot reach -- raw mode, the key decoding, dispatch, and the autosave --
and it is the only way to find out whether ``C-s`` reaches the program or gets
eaten by the terminal driver on the way.
"""

from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from bkmacs.crypt import ITERATIONS  # noqa: E402
from bkmacs.layout import char_width  # noqa: E402

CSI_PARAMETERS = "0123456789;?<=>!"

#: How long the terminal has to stay silent before the editor counts as having
#: finished drawing.  A redraw arrives in one write, so this only has to be
#: longer than the gap between two of them and shorter than the waits it is
#: saving -- and the whole suite is otherwise four fifths sleep.
QUIET = 0.05

BLANK = (" ", frozenset())

#: SGR codes worth remembering.  Attributes matter as much as characters: a
#: mode line drawn dim over reverse video is on the screen and invisible, and
#: a grid that only kept the text would call that a pass.
STYLES = {1: "bold", 2: "dim", 3: "italic", 4: "underline", 7: "reverse"}
UNSTYLES = {22: ("bold", "dim"), 23: ("italic",), 24: ("underline",),
            27: ("reverse",)}


class Screen:
    """Just enough of a terminal to read back what the editor drew.

    curses sends the smallest patch it can get away with -- a mode line that
    goes from ``(1,0)`` to ``(2,3)`` arrives as two single characters aimed at
    two columns -- so the only way to assert on what is displayed is to keep a
    grid and apply the patches to it.
    """

    def __init__(self, rows: int = 24, columns: int = 80) -> None:
        self.rows, self.columns = rows, columns
        self.cells = [[BLANK] * columns for _ in range(rows)]
        self.style: frozenset = frozenset()
        self.y = self.x = 0
        # ncurses deletes lines by setting a scroll region and scrolling it,
        # so a grid that ignores margins goes quietly out of sync with the
        # real screen and shows text that was scrolled away.
        self.top_margin, self.bottom_margin = 0, rows - 1

    def scroll(self, count: int) -> None:
        """Positive scrolls the region up, negative scrolls it down."""
        for _ in range(abs(count)):
            if count > 0:
                del self.cells[self.top_margin]
                self.cells.insert(self.bottom_margin, [BLANK] * self.columns)
            else:
                del self.cells[self.bottom_margin]
                self.cells.insert(self.top_margin, [BLANK] * self.columns)

    def put(self, character: str) -> None:
        if self.y >= self.rows or self.x >= self.columns:
            return
        self.cells[self.y][self.x] = (character, self.style)
        width = max(1, char_width(character))
        for offset in range(1, width):
            if self.x + offset < self.columns:
                self.cells[self.y][self.x + offset] = ("", self.style)
        self.x += width

    def feed(self, data: str) -> None:
        index = 0
        while index < len(data):
            character = data[index]
            if character == "\x1b":
                index = self.escape(data, index + 1)
                continue
            index += 1
            if character == "\r":
                self.x = 0
            elif character == "\n":
                self.y = min(self.y + 1, self.rows - 1)
            elif character == "\b":
                self.x = max(0, self.x - 1)
            elif character == "\t":
                self.x = min(self.columns - 1, (self.x // 8 + 1) * 8)
            elif character >= " ":
                self.put(character)

    def escape(self, data: str, index: int) -> int:
        if index >= len(data):
            return index
        if data[index] == "[":
            index += 1
            start = index
            while index < len(data) and data[index] in CSI_PARAMETERS:
                index += 1
            if index >= len(data):
                return index
            self.control(data[start:index], data[index])
            return index + 1
        if data[index] == "]":  # An OS command, ended by BEL.
            while index < len(data) and data[index] != "\x07":
                index += 1
            return index + 1
        if data[index] in "()#%":
            return index + 2
        if data[index] == "D":  # Index: down a line, scrolling at the bottom.
            if self.y == self.bottom_margin:
                self.scroll(1)
            else:
                self.y = min(self.rows - 1, self.y + 1)
        elif data[index] == "M":  # Reverse index.
            if self.y == self.top_margin:
                self.scroll(-1)
            else:
                self.y = max(0, self.y - 1)
        return index + 1

    def control(self, parameters: str, final: str) -> None:
        if parameters.startswith("?"):
            return  # Private modes: alternate screen, cursor visibility.
        numbers = [int(part) if part.isdigit() else 0
                   for part in parameters.split(";")] or [0]
        first = numbers[0]
        if final == "m":
            style = set(self.style)
            for number in numbers:
                if number == 0:
                    style.clear()
                elif number in STYLES:
                    style.add(STYLES[number])
                elif number in UNSTYLES:
                    style.difference_update(UNSTYLES[number])
                elif 30 <= number <= 37 or number == 39:
                    style = {item for item in style
                             if not item.startswith("fg")}
                    if number != 39:
                        style.add("fg%d" % (number - 30))
                elif 40 <= number <= 47 or number == 49:
                    style = {item for item in style
                             if not item.startswith("bg")}
                    if number != 49:
                        style.add("bg%d" % (number - 40))
            self.style = frozenset(style)
            return
        if final in "Hf":
            self.y = max(0, (numbers[0] or 1) - 1)
            self.x = max(0, ((numbers[1] if len(numbers) > 1 else 1) or 1) - 1)
        elif final == "d":
            self.y = max(0, (first or 1) - 1)
        elif final in "G`":
            self.x = max(0, (first or 1) - 1)
        elif final == "A":
            self.y = max(0, self.y - max(1, first))
        elif final == "B":
            self.y = min(self.rows - 1, self.y + max(1, first))
        elif final == "C":
            self.x = min(self.columns - 1, self.x + max(1, first))
        elif final == "D":
            self.x = max(0, self.x - max(1, first))
        elif final == "J":
            self.erase_display(first)
        elif final == "K":
            self.erase_line(first)
        elif final == "X":  # Erase characters in place, cursor unmoved.
            for offset in range(max(1, first)):
                if self.x + offset < self.columns:
                    self.cells[self.y][self.x + offset] = BLANK
        elif final == "P":
            for _ in range(max(1, first)):
                del self.cells[self.y][self.x]
                self.cells[self.y].append(BLANK)
        elif final == "@":
            for _ in range(max(1, first)):
                self.cells[self.y].insert(self.x, BLANK)
                del self.cells[self.y][-1]
        elif final == "L":  # Insert lines, within the margins.
            for _ in range(max(1, first)):
                self.cells.insert(self.y, [BLANK] * self.columns)
                del self.cells[self.bottom_margin + 1]
        elif final == "M":  # Delete lines, within the margins.
            for _ in range(max(1, first)):
                del self.cells[self.y]
                self.cells.insert(self.bottom_margin, [BLANK] * self.columns)
        elif final == "S":
            self.scroll(max(1, first))
        elif final == "T":
            self.scroll(-max(1, first))
        elif final == "r":
            top = (numbers[0] or 1) - 1
            bottom = ((numbers[1] if len(numbers) > 1 else self.rows)
                      or self.rows) - 1
            self.top_margin = max(0, min(top, self.rows - 1))
            self.bottom_margin = max(self.top_margin,
                                     min(bottom, self.rows - 1))
            self.y = self.x = 0  # DECSTBM homes the cursor.

    def erase_display(self, mode: int) -> None:
        if mode == 2:
            self.cells = [[BLANK] * self.columns for _ in range(self.rows)]
        elif mode == 0:
            self.erase_line(0)
            for row in range(self.y + 1, self.rows):
                self.cells[row] = [BLANK] * self.columns

    def erase_line(self, mode: int) -> None:
        if mode == 2:
            self.cells[self.y] = [BLANK] * self.columns
        elif mode == 0:
            for column in range(self.x, self.columns):
                self.cells[self.y][column] = BLANK

    def row(self, index: int) -> str:
        return "".join(cell[0] for cell in self.cells[index]).rstrip()

    def styles_at(self, row: int, column: int) -> frozenset:
        return self.cells[row][column][1]

    def styles_of(self, row: int, text: str) -> frozenset:
        """The attributes covering a substring of a row, wherever it is."""
        line = self.row(row)
        start = line.index(text)
        used: set = set()
        for column in range(start, start + len(text)):
            used.update(self.styles_at(row, column))
        return frozenset(used)

    def styles(self, index: int) -> frozenset:
        """Every attribute in use on a row that has anything on it."""
        used: set = set()
        for character, style in self.cells[index]:
            if character.strip():
                used.update(style)
        return frozenset(used)

    def text(self) -> str:
        return "\n".join(self.row(index) for index in range(self.rows))

ESC = "\x1b"
CTRL = {name: chr(index + 1) for index, name in enumerate("abcdefghijklmnopqrstuvwxyz")}
RET = "\r"
DEL = "\x7f"
UNDO = "\x1c"  # C-\, which every terminal can send, unlike C-/.
SET_MARK = "\x00"  # C-@, which is what C-SPC actually sends.


class Session:
    """A running bkmacs, and a keyboard to type at it with."""

    #: What the editor sends at startup to find out what colour the terminal
    #: is painted, and what it sends straight after to find out whether the
    #: first question is going to be answered at all.
    BACKGROUND_QUERY = b"\x1b]11;?"
    ATTRIBUTES_QUERY = b"\x1b[c"

    def __init__(self, *paths: str, rows: int = 24, columns: int = 80,
                 history: str = "", home: str = "",
                 entry: "list[str] | None" = None, cwd: str = "",
                 background: str = "", early: str = "") -> None:
        #: The background to claim when asked, as the three hex components of
        #: an ``ESC ] 11`` reply.  Empty is a terminal that does not answer the
        #: question, which is the common one and so the default here: every
        #: other test then runs against the palette a plain terminal gets.
        self.background = background
        self.answered: set = set()
        self.display = Screen(rows, columns)
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0))
        environment = dict(os.environ, TERM="xterm-256color", LINES=str(rows),
                           COLUMNS=str(columns))
        environment.setdefault(
            "LC_ALL", "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8")
        environment.setdefault("BKMACS_HISTORY", history or os.devnull)
        if home:
            environment["HOME"] = home
        self.process = subprocess.Popen(
            [sys.executable] + (entry or ["-m", "bkmacs"]) + list(paths),
            stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd or ROOT, env=environment, start_new_session=False)
        os.close(slave)
        self.output = b""
        # Typed before the editor has finished starting, which is the case
        # the questions it asks at startup have to be careful of.
        if early:
            os.write(self.master, early.encode("utf-8"))
        # Started, rather than started and a fixed wait later: what says the
        # editor is up is the mode line, and it is there as soon as the first
        # frame is drawn.  Python's own startup is most of this.
        self.settle(5.0, quiet=QUIET, until=lambda: self.mode_line().strip())

    def send(self, keys: str) -> None:
        os.write(self.master, keys.encode("utf-8"))
        self.settle(0.15, quiet=QUIET)

    def settle(self, seconds: float, quiet: float = 0,
               until=None) -> None:
        """Let the editor catch up, draining what it drew.

        ``seconds`` is how long to allow, and by default it is also how long
        this takes: a wait with nothing to see is still a real wait, since
        the autosave writes a file half a second after the typing stops and
        draws nothing at all while it does it.

        The two ways of finishing early are for the waits that do have
        something to see.  ``until`` is a question to ask after each read --
        the screen having been drawn at all, say -- and ``quiet`` says to
        stop once that many seconds have gone by with nothing arriving,
        which is what a keystroke's worth of redrawing looks like when it is
        over.

        Quiet only counts after something has arrived.  The terminal is
        silent in the moment after a keystroke as well as in the moment after
        the redraw it causes, and the two are told apart by which side of the
        drawing they are on, not by how they sound.
        """
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            os.set_blocking(self.master, False)
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                chunk = b""
            if chunk:
                self.output += chunk
                self.display.feed(chunk.decode("utf-8", "replace"))
                self.answer_queries()
                last = time.monotonic()
            else:
                time.sleep(0.01)
            if until is not None and until():
                return
            if quiet and last is not None and time.monotonic() - last > quiet:
                return

    def answer_queries(self) -> None:
        """Be the terminal the editor is asking questions of.

        The device-attributes question is always answered, and answered even
        when the background one is not: that is how a real terminal without
        the colour question behaves, and answering it is what keeps every
        session in this file from waiting out the editor's timeout at startup.
        """
        if (self.background and self.BACKGROUND_QUERY in self.output
                and "background" not in self.answered):
            self.answered.add("background")
            os.write(self.master,
                     b"\x1b]11;rgb:" + self.background.encode() + b"\x07")
        if (self.ATTRIBUTES_QUERY in self.output
                and "attributes" not in self.answered):
            self.answered.add("attributes")
            os.write(self.master, b"\x1b[?1;2c")

    def screen(self) -> str:
        return self.display.text()

    def mode_line(self) -> str:
        return self.display.row(self.display.rows - 2)

    def echo(self) -> str:
        return self.display.row(self.display.rows - 1)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        os.close(self.master)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.sessions: list[Session] = []

    def tearDown(self):
        for session in self.sessions:
            session.close()

    def file(self, name: str, content: str) -> str:
        path = os.path.join(self.directory, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def start(self, *paths: str, **options) -> Session:  # noqa: ANN003
        session = Session(*paths, **options)
        self.sessions.append(session)
        return session

    def contents(self, path: str) -> str:
        with open(path) as handle:
            return handle.read()

    def assertEcho(self, session, text: str, timeout: float = 3.0) -> None:
        """Wait for the echo area to say ``text``, and fail with it if it
        never does."""
        session.settle(timeout, until=lambda: text in session.echo())
        self.assertIn(text, session.echo())

    def assertScreen(self, session, text: str, timeout: float = 3.0) -> None:
        """The same, of the screen as a whole."""
        session.settle(timeout, until=lambda: text in session.screen())
        self.assertIn(text, session.screen())

    def assertModeLine(self, session, text: str, timeout: float = 3.0) -> None:
        """The same, of the mode line."""
        session.settle(timeout, until=lambda: text in session.mode_line())
        self.assertIn(text, session.mode_line())

    def assertRow(self, session, row: int, text: str,
                  timeout: float = 3.0) -> None:
        """The same, of one row of it -- which is how the window below says
        what it is showing, since its own mode line is a row like any other."""
        session.settle(timeout,
                       until=lambda: text in session.display.row(row))
        self.assertIn(text, session.display.row(row))

    def assertSaved(self, path: str, text: str, timeout: float = 3.0) -> None:
        """Wait for the file to hold ``text``, and fail with it if it never
        does.

        The autosave draws nothing.  It writes the file half a second after
        the typing stops, and the only way to watch that happen is to look at
        the file -- so this looks, rather than sleeping past the moment and
        hoping.  Which is the difference between a suite that waits as long
        as the editor takes and one that waits as long as it was told to.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                found = self.contents(path)
            except OSError:
                found = ""
            if found == text or time.monotonic() > deadline:
                return self.assertEqual(found, text)
            time.sleep(0.02)

    # -- the tests -------------------------------------------------------

    def test_types_and_autosaves_to_the_file_itself(self):
        path = self.file("a.txt", "hello\n")
        session = self.start(path)
        session.send(CTRL["e"] + " world")
        self.assertSaved(path, "hello world\n")

    def test_control_s_is_a_search_and_not_flow_control(self):
        path = self.file("b.txt", "alpha\nbeta\ngamma\n")
        session = self.start(path)
        session.send(CTRL["s"] + "gam")
        self.assertEcho(session, "I-search: gam")
        # Emacs leaves point at the end of the match, so C-k takes the rest.
        session.send(RET + CTRL["k"])
        self.assertSaved(path, "alpha\nbeta\ngam\n")

    def highlighted(self, session, row: int) -> str:
        """The characters drawn in reverse video on one row of the screen."""
        return "".join(character for character, styles
                       in session.display.cells[row] if "reverse" in styles)

    def test_isearch_backward_shows_what_it_found(self):
        # The match is highlighted by being the region, and searching
        # backwards leaves point at the front of it -- so the mark has to go
        # to the far end.  Both at the front is a region of no width: the
        # search works and the screen says nothing about it.
        path = self.file("d.txt", "beta one\nalpha\nbeta two\n")
        session = self.start(path)
        session.send(ESC + ">")  # M->, since backwards from the top finds
        session.settle(0.3)      # nothing at all.
        session.send(CTRL["r"] + "beta")
        self.assertEcho(session, "I-search backward: beta")
        self.assertEqual(self.highlighted(session, 2), "beta")
        # And on again to the one before it.
        session.send(CTRL["r"])
        session.settle(0.4)
        self.assertEqual(self.highlighted(session, 0), "beta")
        # Point is left at the front of the match, which is where C-d bites.
        session.send(RET + CTRL["d"])
        self.assertSaved(path, "eta one\nalpha\nbeta two\n")

    def test_a_second_control_s_repeats_the_last_search(self):
        path = self.file("e.txt", "alpha\nbeta\ngamma\nbeta\n")
        session = self.start(path)
        session.send(CTRL["s"] + "beta" + RET)
        session.send(ESC + "<")  # Back to the top, with nothing typed.
        session.send(CTRL["s"] + CTRL["s"])
        session.settle(0.5)
        # The recalled search is in the minibuffer as though it were typed,
        # and it has found the first beta.
        self.assertIn("I-search: beta", session.echo())
        self.assertEqual(self.highlighted(session, 1), "beta")
        # DEL takes the recall back off again, like any other keystroke.
        session.send(DEL)
        self.assertEcho(session, "I-search:")
        self.assertNotIn("beta", session.echo())

    def test_a_second_control_r_repeats_it_backwards(self):
        path = self.file("f.txt", "alpha\nbeta\ngamma\nbeta\n")
        session = self.start(path)
        session.send(CTRL["s"] + "beta" + RET)
        session.send(ESC + ">")
        session.send(CTRL["r"] + CTRL["r"])
        self.assertEcho(session, "I-search backward: beta")
        self.assertEqual(self.highlighted(session, 3), "beta")

    def test_find_alternate_file_reads_the_file_again(self):
        path = self.file("g.txt", "one\ntwo\nthree\n")
        session = self.start(path)
        session.send(CTRL["n"] + CTRL["n"])
        session.send(CTRL["x"] + CTRL["v"])
        session.settle(0.4)
        # The prompt starts out holding the name of the file already here,
        # which is what makes RET on its own mean "again".
        self.assertIn("Find alternate file: ", session.echo())
        self.assertIn("g.txt", session.echo())
        with open(path, "w") as handle:
            handle.write("one\ntwo\nthree\nfour\n")
        session.send(RET)
        session.settle(0.5)
        self.assertEqual(session.display.row(3), "four")
        # Read afresh, so point is at the top rather than where it was.
        self.assertIn("(1,0)", session.mode_line())

    def test_find_alternate_file_takes_the_old_buffer_s_place(self):
        path = self.file("h.txt", "aaa\n")
        self.file("i.txt", "bbb\n")
        session = self.start(path)
        session.send(CTRL["x"] + CTRL["v"])
        session.send(DEL * len("h.txt") + "i.txt" + RET)
        session.settle(0.5)
        self.assertEqual(session.display.row(0), "bbb")
        self.assertIn("i.txt", session.mode_line())
        # The buffer it was visiting is gone, not left behind: that is the
        # difference between this and C-x C-f.
        session.send(CTRL["x"] + CTRL["b"])
        session.settle(0.4)
        self.assertNotIn("h.txt", session.screen())

    def test_migemo_searches_japanese_from_romaji(self):
        path = self.file("d.txt", "one\n吾輩は猫である\n")
        session = self.start(path)
        session.send(CTRL["s"] + "neko")  # No M-m: migemo is where it starts.
        self.assertEcho(session, "I-search: neko")
        # Point is left at the end of what the pattern matched, which is the
        # one character 猫 and not the four letters that were typed.
        session.send(RET + CTRL["k"])
        self.assertSaved(path, "one\n吾輩は猫\n")

    def test_migemo_can_be_turned_off_and_stays_off(self):
        path = self.file("e.txt", "吾輩は猫である\n")
        session = self.start(path)
        session.send(CTRL["s"] + "neko" + ESC + "m")
        session.settle(0.3)
        # Off, so romaji in a file that has none of it finds nothing.
        self.assertIn("Failing I-search [literal]: neko", session.echo())
        session.send(ESC + "m")
        self.assertEcho(session, "I-search: neko")
        self.assertNotIn("Failing", session.echo())
        # Where the search is left is where the next one starts.
        session.send(ESC + "m" + RET + CTRL["s"])
        self.assertEcho(session, "I-search [literal]:")

    def test_the_case_of_a_word(self):
        path = self.file("case.txt", "hello brave new world\n")
        session = self.start(path)
        session.send(ESC + "c")            # M-c: Hello, point after it.
        session.send(ESC + "u")            # M-u: BRAVE.
        session.send(ESC + "l")            # M-l: new, already down.
        self.assertSaved(path, "Hello BRAVE new world\n")
        # And from the middle of a word, from point on -- as Emacs does it.
        session.send(CTRL["a"] + CTRL["f"] * 3 + ESC + "u")
        self.assertSaved(path, "HelLO BRAVE new world\n")

    def test_control_x_h_takes_the_whole_buffer(self):
        path = self.file("all.txt", "one\ntwo\nthree\n")
        session = self.start(path)
        session.send(CTRL["n"])            # Somewhere in the middle of it.
        session.send(CTRL["x"] + "h")
        session.send(CTRL["w"])            # The region is everything.
        self.assertSaved(path, "")
        session.send(CTRL["y"])            # And all of it comes back.
        self.assertSaved(path, "one\ntwo\nthree\n")

    def test_control_q_inserts_the_next_key_as_itself(self):
        path = self.file("q.txt", "")
        session = self.start(path)
        # TAB indents by two spaces; C-q TAB is the only way to a real one.
        session.send("a" + CTRL["q"] + "\t" + "b")
        session.send(CTRL["q"] + CTRL["l"])
        self.assertSaved(path, "a\tb\x0c")
        # And it is shown the way control characters are shown.
        self.assertRow(session, 0, "^L")

    def test_query_replace_regexp(self):
        path = self.file("r.txt", "one 1, two 22, three 333\n")
        session = self.start(path)
        session.send(ESC + "x" + "query-replace-regexp" + RET)
        session.send(r"(\d+)" + RET)
        session.send(r"<\1>" + RET)
        self.assertEcho(session, "Query replacing")
        session.send("y")          # The first, with its group expanded.
        session.send("n")          # Not the second.
        session.send("!")          # And the rest without asking.
        self.assertSaved(path, "one <1>, two 22, three <333>\n")

    def test_query_replace_regexp_stays_inside_the_region(self):
        path = self.file("s.txt", "aa\naa\naa\n")
        session = self.start(path)
        session.send(SET_MARK + CTRL["n"] + CTRL["n"])  # The first two lines.
        session.send(ESC + "x" + "query-replace-regexp" + RET)
        session.send("a+" + RET + "b" + RET)
        session.send("!")
        self.assertSaved(path, "b\nb\naa\n")

    def test_write_file_carries_the_buffer_to_the_new_name(self):
        path = self.file("w.txt", "text\n")
        other = os.path.join(self.directory, "copy.txt")
        session = self.start(path)
        session.send(CTRL["x"] + CTRL["w"])
        self.assertEcho(session, "Write file: ")
        session.send(DEL * len("w.txt") + "copy.txt" + RET)
        self.assertSaved(other, "text\n")
        self.assertModeLine(session, "copy.txt")
        # The buffer is visiting the new file now, so the autosave goes
        # there and the old one is left as it was.
        session.send(CTRL["e"] + "!")
        self.assertSaved(other, "text!\n")
        self.assertEqual(self.contents(path), "text\n")

    def test_write_file_asks_before_overwriting(self):
        path = self.file("v.txt", "mine\n")
        self.file("theirs.txt", "theirs\n")
        session = self.start(path)
        session.send(CTRL["x"] + CTRL["w"])
        session.send(DEL * len("v.txt") + "theirs.txt" + RET)
        self.assertEcho(session, "exists; overwrite?")
        session.send("n")
        self.assertEcho(session, "Write cancelled")
        self.assertEqual(self.contents(os.path.join(self.directory,
                                                    "theirs.txt")), "theirs\n")

    def test_a_numeric_argument_counts_the_next_command(self):
        path = self.file("u.txt", "one\ntwo\nthree\nfour\nfive\n")
        session = self.start(path)
        session.send(CTRL["u"] + "3" + CTRL["n"])   # Three lines down.
        self.assertModeLine(session, "(4,0)")
        # The count is how many times to press the key, not what Emacs makes
        # of it per command: C-k takes a line and then its newline, so four
        # of them take two lines -- and they join into one kill.
        session.send(CTRL["u"] + "4" + CTRL["k"])
        self.assertSaved(path, "one\ntwo\nthree\n")
        session.send(CTRL["y"])
        self.assertSaved(path, "one\ntwo\nthree\nfour\nfive\n")

    def test_a_numeric_argument_repeats_a_character(self):
        path = self.file("d.txt", "")
        session = self.start(path)
        session.send(CTRL["u"] + "20" + "-")
        self.assertSaved(path, "-" * 20)
        # One edit, so one undo takes the whole rule away again.
        session.send(UNDO)
        self.assertSaved(path, "")

    def test_control_u_on_its_own_is_four(self):
        path = self.file("f.txt", "abcdefghij\n")
        session = self.start(path)
        session.send(CTRL["u"] + CTRL["d"])         # Four characters gone.
        self.assertSaved(path, "efghij\n")
        session.send(CTRL["u"] + CTRL["u"] + CTRL["d"])  # Sixteen, or what
        self.assertSaved(path, "")                       # is left of them.

    def test_a_numeric_argument_does_not_repeat_a_question(self):
        path = self.file("g.txt", "text\n")
        session = self.start(path)
        session.send(CTRL["u"] + "3" + CTRL["x"] + CTRL["f"])
        self.assertEcho(session, "Find file: ")
        session.send(CTRL["g"])
        self.assertEcho(session, "Quit")

    def test_query_replace(self):
        path = self.file("c.txt", "one two one two\n")
        session = self.start(path)
        session.send(ESC + "%")  # M-%
        session.send("one" + RET)
        session.send("ONE" + RET)
        self.assertEcho(session, "Query replacing one with ONE")
        session.send("y")
        session.send("!")
        self.assertSaved(path, "ONE two ONE two\n")

    def test_transpose_chars(self):
        path = self.file("t.txt", "teh\nx\ny\n")
        session = self.start(path)
        session.send(CTRL["e"])  # At the end of the line: fixes the typo.
        session.send(CTRL["t"])
        session.send(CTRL["n"] + CTRL["a"])  # Start of "x": swaps it with the
        session.send(CTRL["t"])  # newline, pulling it up and leaving a blank.
        self.assertSaved(path, "thex\n\ny\n")

    def test_region_kill_and_yank(self):
        path = self.file("d.txt", "abcdef\n")
        session = self.start(path)
        session.send(SET_MARK)
        session.send(CTRL["f"] * 3)  # Mark ... point over "abc".
        session.send(CTRL["w"])  # Kill it.
        session.send(CTRL["e"])
        session.send(CTRL["y"])  # Yank it back at the end.
        self.assertSaved(path, "defabc\n")

    def test_kill_and_yank_rectangle(self):
        path = self.file("r.txt", "abcdef\nabcdef\nabcdef\n")
        session = self.start(path)
        session.send(CTRL["f"])
        session.send(SET_MARK)
        session.send(CTRL["n"] * 2 + CTRL["f"] * 3)  # Corner at row 2, "e".
        session.send(CTRL["x"] + "r")
        session.settle(0.2)
        self.assertIn("C-x r-", session.echo())  # A prefix under a prefix.
        session.send("k")
        self.assertSaved(path, "aef\naef\naef\n")
        session.send(CTRL["e"])
        session.send(CTRL["x"] + "ry")
        self.assertSaved(path, "aefbcd\naefbcd\naefbcd\n")

    def test_string_rectangle_pads_a_line_too_short_to_reach_it(self):
        path = self.file("s.txt", "aaaa\nb\ncccc\n")
        session = self.start(path)
        session.send(CTRL["f"] * 2)
        session.send(SET_MARK)
        session.send(CTRL["n"] * 2 + CTRL["f"])  # One column wide, three deep.
        session.send(ESC + "x" + "string-rectangle" + RET)
        session.send("X" + RET)
        self.assertSaved(path, "aaXa\nb X\nccXc\n")

    def test_undo_by_control_backslash(self):
        path = self.file("e.txt", "keep\n")
        session = self.start(path)
        session.send(CTRL["e"] + "XYZ")
        self.assertSaved(path, "keepXYZ\n")
        session.send(UNDO)
        self.assertSaved(path, "keep\n")

    def test_backspace_and_control_h_both_delete_backwards(self):
        path = self.file("f.txt", "abcdef\n")
        session = self.start(path)
        session.send(CTRL["e"] + DEL + CTRL["h"])
        self.assertSaved(path, "abcd\n")

    def test_two_files_and_switching_between_them(self):
        first = self.file("one.txt", "first\n")
        second = self.file("two.txt", "second\n")
        session = self.start(first, second)
        session.send(CTRL["e"] + "!")
        session.send(CTRL["x"] + "b" + "two.txt" + RET)
        session.send(CTRL["e"] + "?")
        self.assertSaved(first, "first!\n")
        self.assertEqual(self.contents(second), "second?\n")

    def test_japanese_is_typed_and_measured(self):
        path = self.file("j.txt", "")
        session = self.start(path)
        session.send("吾輩は猫である")
        self.assertSaved(path, "吾輩は猫である")
        self.assertEqual(session.display.row(0), "吾輩は猫である")
        self.assertIn("(1,14)", session.mode_line())  # Seven wide ones.

    def test_mode_line_shows_the_file_and_the_position(self):
        path = self.file("m.txt", "one\ntwo\n")
        session = self.start(path)
        session.send(CTRL["n"] + CTRL["e"])
        self.assertModeLine(session, "m.txt")
        self.assertIn("(2,3)", session.mode_line())
        self.assertIn("-UUU:---", session.mode_line())
        session.send("!")
        self.assertModeLine(session, "-UUU:-**")
        self.assertModeLine(session, "-UUU:---")

    def test_long_lines_wrap_with_a_continuation_marker(self):
        path = self.file("w.txt", "x" * 100 + "\n")
        session = self.start(path)
        session.settle(0.3)
        self.assertEqual(session.display.row(0), "x" * 79 + "\\")
        self.assertEqual(session.display.row(1), "x" * 21)

    def test_external_change_is_reported_in_the_mode_line(self):
        path = self.file("y.txt", "mine\n")
        session = self.start(path)
        time.sleep(1.1)
        with open(path, "w") as handle:
            handle.write("theirs\n")
        session.send(CTRL["e"] + "!")
        self.assertModeLine(session, "[disk changed]")

    def test_suspend_stops_the_process_and_fg_brings_it_back(self):
        path = self.file("z.txt", "text\n")
        session = self.start(path)
        session.send(CTRL["z"])
        time.sleep(0.4)
        state = self.process_state(session.process.pid)
        self.assertEqual(state, "T", "C-z should have stopped the process")
        os.kill(session.process.pid, signal.SIGCONT)
        time.sleep(0.4)
        self.assertNotEqual(self.process_state(session.process.pid), "T")
        session.send(CTRL["e"] + "!")
        self.assertSaved(path, "text!\n")

    def test_external_change_stops_the_autosave_instead_of_clobbering(self):
        path = self.file("x.txt", "mine\n")
        session = self.start(path)
        time.sleep(1.1)  # Make sure the new mtime differs.
        with open(path, "w") as handle:
            handle.write("theirs\n")
        session.send(CTRL["e"] + "!")
        # The refusal is what to wait for: the file is already what it should
        # stay, so watching it says nothing about whether the autosave has
        # run yet and decided to leave it alone.
        self.assertScreen(session, "changed on disk")
        self.assertSaved(path, "theirs\n")

    def test_grep_finds_hits_and_ret_jumps_to_one(self):
        self.file("one.py", "import os\nprint(os.getcwd())\n")
        self.file("two.py", "import sys\n")
        self.file("three.txt", "import nothing\n")
        session = self.start(os.path.join(self.directory, "one.py"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("^import" + RET)
        session.send("*.py" + RET)
        session.settle(0.5)
        screen = session.screen()
        self.assertIn("one.py:1:import os", screen)
        self.assertIn("two.py:1:import sys", screen)
        self.assertNotIn("three.txt", screen)  # The glob excluded it.
        self.assertIn("2 matches", session.echo())

        # The frame split: the file above, the results below, cursor unmoved.
        self.assertIn("one.py", session.display.row(11))
        self.assertIn("*grep*", session.display.row(22))

        session.send(CTRL["x"] + "o")  # Into the results.
        session.send(CTRL["n"])  # Down to the second hit.
        session.send(RET)
        self.assertRow(session, 11, "two.py")
        self.assertIn("*grep*", session.display.row(22))

    def test_grep_globs_reach_into_subdirectories(self):
        os.makedirs(os.path.join(self.directory, "pkg"))
        os.makedirs(os.path.join(self.directory, "tests"))
        self.file("top.py", "needle at the top\n")
        self.file("pkg/deep.py", "needle down here\n")
        self.file("tests/t.py", "needle in tests\n")
        session = self.start(os.path.join(self.directory, "top.py"))

        session.send(ESC + "x" + "grep" + RET)
        session.send("needle" + RET)
        session.send("**/*.py" + RET)
        session.settle(0.5)
        screen = session.screen()
        self.assertIn("pkg/deep.py:1:", screen)
        self.assertIn("tests/t.py:1:", screen)
        self.assertIn("top.py:1:", screen)

        session.send(ESC + "x" + "grep" + RET)
        session.send("needle" + RET)
        session.send("tests/*.py" + RET)
        session.settle(0.5)
        screen = session.screen()
        self.assertIn("tests/t.py:1:", screen)
        self.assertNotIn("pkg/deep.py", screen)
        self.assertIn("1 match", session.echo())

    def test_grep_glob_defaults_to_everything_without_prefilling_it(self):
        self.file("a.txt", "findable\n")
        self.file("b.py", "findable\n")
        session = self.start(os.path.join(self.directory, "a.txt"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("findable" + RET)
        session.settle(0.2)
        # The default is offered in the prompt, not typed into the input, so
        # that what the user types is exactly the glob that gets used.
        self.assertIn("default *", session.echo())
        session.send(RET)
        self.assertScreen(session, "a.txt:1:")
        self.assertIn("b.py:1:", session.screen())

    def test_occur_lists_matching_lines_of_this_buffer(self):
        path = self.file("o.py", "".join([
            "import os\n", "x = 1\n", "import sys\n", "y = 2\n",
            "import re\n"]))
        session = self.start(path)
        session.send(ESC + "x" + "occur" + RET)
        session.send("^import" + RET)
        session.settle(0.5)
        screen = session.screen()
        self.assertIn("3 matches", session.echo())
        self.assertIn('for "^import" in buffer: o.py', screen)
        self.assertIn("1:import os", screen)
        self.assertIn("3:import sys", screen)
        self.assertIn("5:import re", screen)
        # The non-matching lines are in the source window above, not the list.
        listed = "\n".join(session.display.row(row) for row in range(12, 22))
        self.assertNotIn("x = 1", listed)
        self.assertIn("x = 1", session.display.row(1))

        # Results below, source above, cursor left where it was.
        self.assertIn("o.py", session.display.row(11))
        self.assertIn("*Occur*", session.display.row(22))
        self.assertIn("(Occur)", session.display.row(22))

        # C-x ` walks them in the source window, first one first.
        session.send(CTRL["x"] + "`")
        self.assertRow(session, 11, "(1,0)")
        session.send(CTRL["x"] + "`")
        self.assertRow(session, 11, "(3,0)")

        # And RET from the results side does the same.
        session.send(CTRL["x"] + "o")
        session.send(CTRL["n"] + CTRL["n"])
        session.send(RET)
        self.assertRow(session, 11, "(5,0)")
        self.assertIn("o.py", session.display.row(11))

    def test_occur_works_in_a_buffer_that_is_not_a_file(self):
        session = self.start()
        session.send("alpha" + CTRL["j"] + "beta" + CTRL["j"] + "alpha again")
        session.send(ESC + "x" + "occur" + RET)
        session.send("alpha" + RET)
        self.assertEcho(session, "2 matches")
        self.assertIn("in buffer: *scratch*", session.screen())
        session.send(CTRL["x"] + "`")
        self.assertRow(session, 11, "*scratch*")
        self.assertIn("(1,0)", session.display.row(11))

    def test_the_matched_text_is_highlighted_in_the_results(self):
        self.file("h.py", "the needle is here\nno match on this line\n")
        session = self.start(os.path.join(self.directory, "h.py"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("needle" + RET)
        session.send("*.py" + RET)
        session.settle(0.5)

        row = next(index for index in range(12, 22)
                   if "h.py:1:" in session.display.row(index))
        # What matched is bold red, the way grep has always drawn it; the
        # path and the rest of the line are left alone.
        self.assertIn("bold", session.display.styles_of(row, "needle"))
        self.assertIn("fg1", session.display.styles_of(row, "needle"))
        self.assertNotIn("bold", session.display.styles_of(row, "h.py:1:"))
        self.assertNotIn("fg1", session.display.styles_of(row, "is here"))
        # No background anywhere: that is the part a light theme cannot take.
        self.assertFalse([style for style
                          in session.display.styles_of(row, "needle")
                          if style.startswith("bg")])

    def test_occur_highlights_every_match_on_a_line(self):
        path = self.file("m.txt", "one and one and two\n")
        session = self.start(path)
        session.send(ESC + "x" + "occur" + RET)
        session.send("one" + RET)
        session.settle(0.5)

        row = next(index for index in range(12, 22)
                   if "1:one and one" in session.display.row(index))
        line = session.display.row(row)
        first = line.index("one")
        second = line.index("one", first + 3)
        self.assertIn("bold", session.display.styles_at(row, first))
        self.assertIn("bold", session.display.styles_at(row, second))
        self.assertNotIn("bold", session.display.styles_at(row, first + 4))

    def test_grep_buffer_is_read_only(self):
        self.file("a.py", "match me\n")
        session = self.start(os.path.join(self.directory, "a.py"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("match" + RET)
        session.send("*.py" + RET)
        session.send(CTRL["x"] + "o")  # Into the results window.
        session.send("XXX")
        self.assertEcho(session, "read-only")

    def test_next_error_walks_the_hits(self):
        self.file("a.py", "target\n")
        self.file("b.py", "target\n")
        session = self.start(os.path.join(self.directory, "a.py"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("target" + RET)
        session.send("*.py" + RET)
        session.settle(0.4)
        # The first next-error visits the first hit, not the second.
        session.send(CTRL["x"] + "`")
        self.assertRow(session, 11, "a.py")
        session.send(CTRL["x"] + "`")
        self.assertRow(session, 11, "b.py")
        self.assertIn("*grep*", session.display.row(22))

    def test_m_x_completes(self):
        path = self.file("c.txt", "text\n")
        session = self.start(path)
        session.send(ESC + "x" + "query-r" + "\t")
        self.assertEcho(session, "M-x query-replace")
        session.send(CTRL["g"])

    def test_completion_opens_a_window_of_candidates(self):
        path = self.file("c.txt", "text\n")
        session = self.start(path)
        session.send(ESC + "x" + "k" + "\t")
        session.settle(0.4)
        screen = session.screen()
        self.assertIn("Possible completions are:", screen)
        self.assertIn("kill-line", screen)
        self.assertIn("kill-region", screen)
        self.assertIn("keyboard-quit", screen)
        self.assertIn("*Completions*", session.display.row(22))
        # "k" is as far as it can complete: keyboard-quit shares only that.
        self.assertIn("M-x k", session.echo())

        session.send(CTRL["g"])  # The window goes away again.
        session.settle(0.3)
        self.assertNotIn("*Completions*", session.screen())
        self.assertIn("c.txt", session.mode_line())

    def test_a_new_file_appearing_underneath_is_not_overwritten(self):
        path = os.path.join(self.directory, "later.txt")
        session = self.start(path)
        session.settle(0.3)
        with open(path, "w") as handle:
            handle.write("somebody else\n")
        session.send("mine")
        self.assertModeLine(session, "[disk changed]")
        self.assertSaved(path, "somebody else\n")

    def test_scratch_buffer_when_no_file_is_given(self):
        session = self.start()
        self.assertModeLine(session, "*scratch*")
        session.send("typed into scratch")
        self.assertRow(session, 0, "typed into scratch")
        session.send(CTRL["x"] + CTRL["c"])
        session.process.wait(timeout=5)

    def test_escape_quits_like_control_g(self):
        """A browser terminal may never deliver C-g; ESC has to be enough."""
        path = self.file("esc.txt", "alpha beta\n")
        session = self.start(path)

        session.send(CTRL["x"] + "b")  # A prompt that has to be escapable.
        self.assertEcho(session, "Switch to buffer")
        session.send(ESC)
        session.settle(0.3)
        self.assertIn("ESC-", session.echo())  # A prefix, even here.
        session.send(ESC)
        session.settle(0.3)
        self.assertNotIn("Switch to buffer", session.echo())

        session.send(CTRL["s"] + "beta")  # And a search that has to abort.
        self.assertEcho(session, "I-search: beta")
        session.send(ESC)
        session.send(ESC)
        session.settle(0.3)
        self.assertIn("(1,0)", session.mode_line())  # Back where it started.
        self.assertEqual(self.contents(path), "alpha beta\n")

    def test_esc_is_a_prefix_key_with_no_clock_on_it(self):
        """Typing ESC and then a key, slowly, has to mean Meta."""
        path = self.file("p.txt", "".join("line %d\n" % n
                                          for n in range(1, 101)))
        session = self.start(path)

        session.send(ESC)
        session.settle(0.5)  # Far longer than any Meta timeout.
        self.assertIn("ESC-", session.echo())  # Waiting, and saying so.
        session.send(">")
        self.assertModeLine(session, "(101,0)")

        session.send(ESC)
        session.send("<")
        self.assertModeLine(session, "(1,0)")

    def test_esc_percent_starts_query_replace(self):
        path = self.file("qr.txt", "one two one\n")
        session = self.start(path)
        session.send(ESC)
        session.send("%")
        self.assertEcho(session, "Query replace")
        session.send("one" + RET)
        session.send("1" + RET)
        session.send("!")
        self.assertSaved(path, "1 two 1\n")

    def test_esc_esc_quits(self):
        path = self.file("qq.txt", "text\n")
        session = self.start(path)
        session.send(CTRL["x"] + "b")
        self.assertEcho(session, "Switch to buffer")
        session.send(ESC)
        session.send(ESC)
        session.settle(0.3)
        self.assertNotIn("Switch to buffer", session.echo())

    def test_split_window_gives_two_mode_lines(self):
        path = self.file("s.txt", "".join("line %d\n" % n
                                          for n in range(1, 101)))
        session = self.start(path)
        session.send(CTRL["x"] + "2")
        self.assertRow(session, 11, "s.txt")
        self.assertIn("s.txt", session.display.row(22))

        # The two windows keep their own places in the same file.
        session.send(CTRL["x"] + "o")
        session.send(ESC)
        session.send(">")
        self.assertRow(session, 11, "(1,0)")
        self.assertIn("(101,0)", session.display.row(22))

        session.send(CTRL["x"] + "1")
        session.settle(0.3)
        self.assertEqual(session.display.row(11).strip(), "")
        self.assertIn("(101,0)", session.mode_line())

    def test_grep_opens_the_hit_in_the_other_window(self):
        self.file("one.py", "the needle is here\n")
        self.file("two.py", "another needle\n")
        session = self.start(os.path.join(self.directory, "one.py"))
        session.send(ESC + "x" + "grep" + RET)
        session.send("needle" + RET)
        session.send("*.py" + RET)
        session.settle(0.5)

        # grep split the frame itself: one.py above, *grep* below.
        self.assertIn("one.py", session.display.row(11))
        self.assertIn("*grep*", session.display.row(22))

        session.send(CTRL["x"] + "o")
        session.send(RET)  # RET on the first hit, from the results window.
        self.assertRow(session, 11, "one.py")
        self.assertIn("*grep*", session.display.row(22))

        session.send(CTRL["x"] + "`")  # next-error, from the file window.
        self.assertRow(session, 11, "two.py")
        self.assertIn("*grep*", session.display.row(22))

    def test_history_survives_the_editor_being_closed(self):
        store = os.path.join(self.directory, "history.json")
        target = self.file("remembered.txt", "hello\n")

        first = self.start(self.file("a.txt", "a\n"), history=store)
        first.send(CTRL["x"] + CTRL["f"])
        first.send(CTRL["a"] + CTRL["k"] + target + RET)
        first.settle(0.4)
        self.assertIn("remembered.txt", first.mode_line())
        first.send(CTRL["x"] + CTRL["c"])
        first.process.wait(timeout=5)

        second = self.start(self.file("b.txt", "b\n"), history=store)
        second.send(CTRL["x"] + CTRL["f"])
        second.send(ESC)  # ESC p is how M-p is typed without a Meta key.
        second.send("p")
        second.settle(0.3)
        # macOS temporary directories are long enough that the recalled path
        # does not fit on one row, so what has to be visible is its end --
        # which is also where the cursor is.
        self.assertIn("remembered.txt", second.echo())
        second.send(RET)
        second.settle(0.4)
        self.assertIn("remembered.txt", second.mode_line())

    def test_grep_pattern_comes_back_with_m_p(self):
        store = os.path.join(self.directory, "history.json")
        self.file("x.py", "alpha\n")
        session = self.start(os.path.join(self.directory, "x.py"),
                             history=store)
        session.send(ESC + "x" + "grep" + RET)
        session.send("alpha" + RET)
        session.send("*.py" + RET)
        session.settle(0.5)

        session.send(ESC + "x" + "grep" + RET)
        session.send(ESC)
        session.send("p")
        self.assertEcho(session, "Grep (regexp): alpha")

    def test_trailing_whitespace_is_marked_quietly_and_without_color(self):
        # The second line holds nothing but indentation, which is what most
        # blank lines in indented code look like.  Reverse video turns those
        # into a solid bar; an underline does not.
        path = self.file("t.txt", "text with a tail   \n    \nclean\n")
        session = self.start(path)
        session.settle(0.3)
        tail = len(session.display.row(0).rstrip())

        self.assertIn("underline", session.display.styles_at(0, tail))
        self.assertNotIn("underline", session.display.styles_at(0, tail - 1))
        self.assertNotIn("reverse", session.display.styles_at(0, tail))
        self.assertFalse([style for style in session.display.styles_at(0, tail)
                          if style.startswith(("fg", "bg"))])

        # A whitespace-only line is marked the same quiet way, end to end.
        self.assertIn("underline", session.display.styles_at(1, 0))
        self.assertNotIn("reverse", session.display.styles_at(1, 0))
        self.assertEqual(session.display.row(2), "clean")

    def test_both_mode_lines_are_visible_when_split(self):
        """A mode line drawn dim over reverse video is not a mode line."""
        path = self.file("v.txt", "text\n")
        session = self.start(path)
        session.send(CTRL["x"] + "2")
        session.settle(0.4)
        for row in (11, 22):
            self.assertIn("v.txt", session.display.row(row))
            self.assertIn("reverse", session.display.styles(row))
            self.assertNotIn("dim", session.display.styles(row))
        # The selected window's mode line is the bold one.
        self.assertIn("bold", session.display.styles(11))
        self.assertNotIn("bold", session.display.styles(22))

    def test_mode_line_carries_the_coding_and_eol_convention(self):
        unix = self.file("u.txt", "one\n")
        session = self.start(unix)
        self.assertModeLine(session, "-UUU:---  F1  u.txt")
        self.assertIn("(Fundamental)", session.mode_line())

        dos = os.path.join(self.directory, "d.txt")
        with open(dos, "wb") as handle:
            handle.write(b"one\r\ntwo\r\n")
        session.send(CTRL["x"] + CTRL["f"])
        session.send(CTRL["a"] + CTRL["k"] + dos + RET)
        session.settle(0.4)
        # The one character of the group that carries news: CRLF.
        self.assertIn("-UUU\\---  F1  d.txt", session.mode_line())

    def test_a_long_answer_scrolls_so_the_cursor_stays_visible(self):
        session = self.start(self.file("s.txt", "text\n"))
        session.send(CTRL["x"] + CTRL["f"])
        session.send(CTRL["a"] + CTRL["k"])

        long_path = "/" + "/".join("directory%02d" % n for n in range(12))
        session.send(long_path + "/target.txt")
        session.settle(0.3)
        echo = session.echo()
        # Typing past the right edge has to keep showing what is being typed.
        self.assertIn("/target.txt", echo)
        self.assertLessEqual(len(echo), 80)
        self.assertNotIn("directory00", echo)  # Scrolled off to the left.

        # And going back to the front of it scrolls the other way.
        session.send(CTRL["a"])
        self.assertEcho(session, "Find file: /directory00")
        session.send(CTRL["g"])

    def test_moving_over_parentheses(self):
        path = self.file("p.py", "value = call(a, nest(b), c) + tail\n")
        session = self.start(path)
        session.settle(0.3)

        # C-M-n from the start goes past the whole group, nesting included.
        session.send(ESC)
        session.send(CTRL["n"])
        session.settle(0.3)
        self.assertIn("(1,27)", session.mode_line())  # Just past the ")".

        # C-M-p comes back to the "(" that opened it.
        session.send(ESC)
        session.send(CTRL["p"])
        self.assertModeLine(session, "(1,12)")

    def test_an_unbalanced_group_says_so_rather_than_wandering(self):
        path = self.file("u.py", "open(this one never closes\n")
        session = self.start(path)
        session.send(ESC)
        session.send(CTRL["n"])
        self.assertEcho(session, "Scan error")
        self.assertIn("(1,0)", session.mode_line())  # And did not move.

    def test_paths_are_shown_with_the_home_directory_as_a_tilde(self):
        os.makedirs(os.path.join(self.directory, "work"))
        path = self.file("work/w.txt", "text\n")
        session = self.start(path, home=self.directory)

        session.send(CTRL["x"] + CTRL["f"])
        self.assertEcho(session, "Find file: ~/work/")
        self.assertNotIn(self.directory, session.echo())

        # And it is only how it is shown: completion still resolves it.
        session.send("w.txt" + RET)
        self.assertModeLine(session, "w.txt")

        # C-x C-s reports where it wrote, abbreviated the same way.
        session.send("!" + CTRL["x"] + CTRL["s"])
        self.assertEcho(session, "Wrote ~/work/w.txt")

    def test_the_package_directory_is_runnable(self):
        """The form the README's alias uses, from a directory of its own.

        Every other test here starts the editor with -m from the checkout;
        this one starts it the way somebody with the alias would, by naming
        the package directory. If that stops working the alias is a lie.
        """
        path = self.file("r.txt", "run me\n")
        session = self.start(path, entry=[os.path.join(ROOT, "bkmacs")],
                             cwd=self.directory)
        session.send(CTRL["e"] + "!")
        self.assertSaved(path, "run me!\n")
        self.assertIn("r.txt", session.mode_line())

    def test_m_q_fills_the_paragraph_around_point(self):
        long_line = ("This is one very long line of prose that goes well past "
                     "the seventy-four columns that this editor fills to, and "
                     "so has to be broken up into several.")
        path = self.file("f.txt", long_line + "\n\nA second paragraph.\n")
        session = self.start(path)

        session.send(ESC)
        session.send("q")
        session.settle(1.0)
        filled = self.contents(path).split("\n")
        self.assertGreater(len(filled), 3)
        for line in filled:
            self.assertLessEqual(len(line), 74)
        # The paragraph below is left alone, blank line and all.
        self.assertEqual(filled[-2], "A second paragraph.")
        self.assertEqual(filled[-3], "")

    def test_m_q_keeps_a_comment_a_comment(self):
        path = self.file("c.py", "    # one two three four five six seven "
                                 "eight nine ten eleven twelve thirteen\n")
        session = self.start(path)
        session.send(ESC)
        session.send("q")
        session.settle(1.0)
        lines = self.contents(path).rstrip("\n").split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertTrue(line.startswith("    # "), line)

    def test_control_x_control_c_exits(self):
        path = self.file("q.txt", "bye\n")
        session = self.start(path)
        session.send(CTRL["x"] + CTRL["c"])
        session.process.wait(timeout=5)
        self.assertEqual(session.process.returncode, 0)

    # -- encrypted files -------------------------------------------------

    PASSWORD = "correct horse"

    def encrypted(self, name: str, content: str) -> str:
        """A file written by openssl enc from the command line."""
        path = os.path.join(self.directory, name)
        subprocess.run(["openssl", "enc", "-e", "-pbkdf2", "-iter",
                        str(ITERATIONS), "-md", "sha256", "-base64",
                        "-aes-256-cbc", "-salt",
                        "-pass", "pass:" + self.PASSWORD, "-out", path],
                       input=content.encode("utf-8"), check=True,
                       stderr=subprocess.DEVNULL)
        return path

    def decrypted(self, path: str) -> str:
        """The same file read back by openssl enc, the same way."""
        done = subprocess.run(["openssl", "enc", "-d", "-pbkdf2", "-iter",
                               str(ITERATIONS), "-md", "sha256", "-base64",
                               "-aes-256-cbc",
                               "-pass", "pass:" + self.PASSWORD, "-in", path],
                              stdout=subprocess.PIPE, check=True,
                              stderr=subprocess.DEVNULL)
        return done.stdout.decode("utf-8")

    @unittest.skipIf(shutil.which("openssl") is None, "no openssl")
    def test_an_encrypted_file_asks_for_its_password(self):
        path = self.encrypted("notes.ossl", "秘密\nsecond line\n")
        store = os.path.join(self.directory, "history.json")
        session = self.start(path, history=store)
        self.assertIn("Password for", session.echo())

        session.send("nope")
        self.assertIn("****", session.echo())  # Echoed, but not shown.
        self.assertNotIn("nope", session.echo())
        session.send(RET)
        self.assertEcho(session, "Wrong password")

        session.send(self.PASSWORD + RET)
        self.assertScreen(session, "秘密")
        self.assertIn("(Encrypted)", session.mode_line())

        # No autosave here, however long the typing stops for: half a second
        # of PBKDF2 between keystrokes is not something anyone wants.
        session.send(CTRL["e"] + "!")
        session.settle(1.5)
        self.assertEqual(self.decrypted(path), "秘密\nsecond line\n")
        self.assertIn("**", session.mode_line())

        session.send(CTRL["x"] + CTRL["s"])
        session.settle(3.0)
        self.assertEqual(self.decrypted(path), "秘密!\nsecond line\n")
        self.assertIn("Wrote", session.echo())
        # The password went nowhere near the minibuffer history file.
        if os.path.exists(store):
            self.assertNotIn(self.PASSWORD, self.contents(store))

    @unittest.skipIf(shutil.which("openssl") is None, "no openssl")
    def test_exiting_asks_before_saving_an_encrypted_file(self):
        path = self.encrypted("notes.ossl", "one\n")
        session = self.start(path)
        session.send(self.PASSWORD + RET)
        session.settle(2.0)
        session.send("two ")
        session.send(CTRL["x"] + CTRL["c"])
        self.assertEcho(session, "Save file")
        session.send("y")
        session.process.wait(timeout=10)
        self.assertEqual(self.decrypted(path), "two one\n")

    MARKDOWN = ("# Title\n"
                "\n"
                "A [link](https://example.com) and `code` and **bold** *em*.\n"
                "\n"
                "```console\n"
                "$ ls **not bold**\n"
                "```\n"
                "\n"
                "- an item\n")

    def test_markdown_is_coloured_by_what_the_line_is(self):
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path)
        styles = session.display.styles_of
        # Magenta and bold for a heading, so that structure is visible from
        # across the room; cyan for both halves of a link, green for code.
        self.assertEqual(styles(0, "# Title"), frozenset({"bold", "fg5"}))
        self.assertIn("fg6", styles(2, "[link]"))
        self.assertIn("fg6", styles(2, "(https://example.com)"))
        self.assertIn("underline", styles(2, "(https://example.com)"))
        self.assertIn("fg2", styles(2, "`code`"))
        self.assertIn("bold", styles(2, "**bold**"))
        self.assertIn("italic", styles(2, "*em*"))
        self.assertEqual(styles(2, " and "), frozenset())
        self.assertIn("fg3", styles(8, "-"))
        self.assertEqual(styles(8, "an item"), frozenset())

    def test_nothing_inside_a_fence_is_markdown(self):
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path)
        # The whole block is code, asterisks and all -- a shell line full of
        # globs is the usual thing to find in a README's console block.
        self.assertIn("fg2", session.display.styles_of(5, "$ ls **not bold**"))
        self.assertNotIn("bold",
                         session.display.styles_of(5, "**not bold**"))

    def test_colouring_follows_the_text_as_it_is_typed(self):
        path = self.file("README.md", "plain\n")
        session = self.start(path)
        self.assertEqual(session.display.styles_of(0, "plain"), frozenset())
        session.send("## ")
        self.assertRow(session, 0, "## plain")
        self.assertIn("fg5", session.display.styles_of(0, "## plain"))
        self.assertModeLine(session, "Markdown")

    def test_a_light_terminal_gets_the_dark_half_of_the_palette(self):
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path, background="ffff/ffff/ffff")
        styles = session.display.styles_of
        # Blue for code where a dark terminal gets green, and magenta for
        # links where it gets cyan: on white those two are 3:1 and worse.
        self.assertIn("fg4", styles(2, "`code`"))
        self.assertNotIn("fg2", styles(2, "`code`"))
        self.assertIn("fg5", styles(2, "[link]"))
        self.assertNotIn("fg6", styles(2, "[link]"))
        # The bullet gives up its colour rather than take yellow at 3:1;
        # where a bullet goes is already most of what says it is one.
        self.assertEqual(styles(8, "-"), frozenset({"bold"}))
        self.assertEqual(styles(8, "an item"), frozenset())

    def test_a_dark_terminal_keeps_the_light_half(self):
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path, background="1c1c/1c1c/1c1c")
        self.assertIn("fg2", session.display.styles_of(2, "`code`"))
        self.assertIn("fg6", session.display.styles_of(2, "[link]"))
        self.assertIn("fg3", session.display.styles_of(8, "-"))

    def test_a_terminal_that_will_not_say_is_taken_to_be_dark(self):
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path)  # Answers the second question only.
        self.assertIn(Session.BACKGROUND_QUERY, session.output)
        self.assertIn("fg2", session.display.styles_of(2, "`code`"))

    def test_typing_ahead_of_startup_survives_the_colour_question(self):
        path = self.file("t.md", "`code` here\n")
        session = self.start(path, background="ffff/ffff/ffff", early="typed ")
        # The keystrokes arrive first and are still in front of any answer,
        # so the question is not asked at all rather than asked and its answer
        # read over the top of them -- a terminal cannot be handed input back.
        self.assertRow(session, 0, "typed `code` here")
        self.assertNotIn(Session.BACKGROUND_QUERY, session.output)
        self.assertIn("fg2", session.display.styles_of(0, "`code`"))

    def test_the_background_is_weighed_rather_than_averaged(self):
        # A terminal painted pure green is a light one, and averaging the
        # three components would have made it a third as bright as it looks.
        path = self.file("README.md", self.MARKDOWN)
        session = self.start(path, background="0000/ffff/0000")
        self.assertIn("fg4", session.display.styles_of(2, "`code`"))

    def test_a_search_match_wins_over_the_markdown_colour(self):
        path = self.file("README.md", "# Title here\n\nplain\n")
        session = self.start(path)
        self.assertEqual(session.display.styles_of(0, "Title"),
                         frozenset({"bold", "fg5"}))
        session.send(CTRL["s"] + "Title")
        # What you just searched for has to be findable on the screen, so it
        # takes the row over from whatever the text happened to be.
        self.assertIn("reverse", session.display.styles_of(0, "Title"))
        self.assertEqual(session.display.styles_of(0, "here"),
                         frozenset({"bold", "fg5"}))

    def test_a_file_that_is_not_markdown_is_left_alone(self):
        path = self.file("plain.txt", "# Title\n\n- `code` here\n")
        session = self.start(path)
        self.assertEqual(session.display.styles(0), frozenset())
        self.assertEqual(session.display.styles(2), frozenset())
        self.assertModeLine(session, "Fundamental")

    def process_state(self, pid: int) -> str:
        """The first letter of the process state, on macOS as well as Linux."""
        output = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                capture_output=True, text=True).stdout
        return output.strip()[:1]


if __name__ == "__main__":
    unittest.main()
