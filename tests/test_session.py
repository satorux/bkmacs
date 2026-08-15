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

from bkmacs.layout import char_width  # noqa: E402

CSI_PARAMETERS = "0123456789;?<=>!"

BLANK = (" ", frozenset())

#: SGR codes worth remembering.  Attributes matter as much as characters: a
#: mode line drawn dim over reverse video is on the screen and invisible, and
#: a grid that only kept the text would call that a pass.
STYLES = {1: "bold", 2: "dim", 4: "underline", 7: "reverse"}
UNSTYLES = {22: ("bold", "dim"), 24: ("underline",), 27: ("reverse",)}


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

    def __init__(self, *paths: str, rows: int = 24, columns: int = 80,
                 history: str = "", home: str = "",
                 entry: "list[str] | None" = None, cwd: str = "") -> None:
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
        self.settle(0.6)

    def send(self, keys: str) -> None:
        os.write(self.master, keys.encode("utf-8"))
        self.settle(0.15)

    def settle(self, seconds: float) -> None:
        """Let the editor catch up, draining what it drew."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            os.set_blocking(self.master, False)
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                chunk = b""
            if chunk:
                self.output += chunk
                self.display.feed(chunk.decode("utf-8", "replace"))
            else:
                time.sleep(0.02)

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

    # -- the tests -------------------------------------------------------

    def test_types_and_autosaves_to_the_file_itself(self):
        path = self.file("a.txt", "hello\n")
        session = self.start(path)
        session.send(CTRL["e"] + " world")
        session.settle(1.0)  # Longer than the half-second idle.
        self.assertEqual(self.contents(path), "hello world\n")

    def test_control_s_is_a_search_and_not_flow_control(self):
        path = self.file("b.txt", "alpha\nbeta\ngamma\n")
        session = self.start(path)
        session.send(CTRL["s"] + "gam")
        session.settle(0.3)
        self.assertIn("I-search: gam", session.echo())
        # Emacs leaves point at the end of the match, so C-k takes the rest.
        session.send(RET + CTRL["k"])
        session.settle(1.0)
        self.assertEqual(self.contents(path), "alpha\nbeta\ngam\n")

    def test_query_replace(self):
        path = self.file("c.txt", "one two one two\n")
        session = self.start(path)
        session.send(ESC + "%")  # M-%
        session.send("one" + RET)
        session.send("ONE" + RET)
        session.settle(0.3)
        self.assertIn("Query replacing one with ONE", session.echo())
        session.send("y")
        session.send("!")
        session.settle(1.0)
        self.assertEqual(self.contents(path), "ONE two ONE two\n")

    def test_transpose_chars(self):
        path = self.file("t.txt", "teh\nx\ny\n")
        session = self.start(path)
        session.send(CTRL["e"])  # At the end of the line: fixes the typo.
        session.send(CTRL["t"])
        session.send(CTRL["n"] + CTRL["a"])  # Start of "x": swaps it with the
        session.send(CTRL["t"])  # newline, pulling it up and leaving a blank.
        session.settle(1.0)
        self.assertEqual(self.contents(path), "thex\n\ny\n")

    def test_region_kill_and_yank(self):
        path = self.file("d.txt", "abcdef\n")
        session = self.start(path)
        session.send(SET_MARK)
        session.send(CTRL["f"] * 3)  # Mark ... point over "abc".
        session.send(CTRL["w"])  # Kill it.
        session.send(CTRL["e"])
        session.send(CTRL["y"])  # Yank it back at the end.
        session.settle(1.0)
        self.assertEqual(self.contents(path), "defabc\n")

    def test_undo_by_control_backslash(self):
        path = self.file("e.txt", "keep\n")
        session = self.start(path)
        session.send(CTRL["e"] + "XYZ")
        session.settle(0.8)
        self.assertEqual(self.contents(path), "keepXYZ\n")
        session.send(UNDO)
        session.settle(0.8)
        self.assertEqual(self.contents(path), "keep\n")

    def test_backspace_and_control_h_both_delete_backwards(self):
        path = self.file("f.txt", "abcdef\n")
        session = self.start(path)
        session.send(CTRL["e"] + DEL + CTRL["h"])
        session.settle(0.8)
        self.assertEqual(self.contents(path), "abcd\n")

    def test_two_files_and_switching_between_them(self):
        first = self.file("one.txt", "first\n")
        second = self.file("two.txt", "second\n")
        session = self.start(first, second)
        session.send(CTRL["e"] + "!")
        session.send(CTRL["x"] + "b" + "two.txt" + RET)
        session.send(CTRL["e"] + "?")
        session.settle(1.0)
        self.assertEqual(self.contents(first), "first!\n")
        self.assertEqual(self.contents(second), "second?\n")

    def test_japanese_is_typed_and_measured(self):
        path = self.file("j.txt", "")
        session = self.start(path)
        session.send("吾輩は猫である")
        session.settle(1.0)
        self.assertEqual(self.contents(path), "吾輩は猫である")
        self.assertEqual(session.display.row(0), "吾輩は猫である")
        self.assertIn("(1,14)", session.mode_line())  # Seven wide ones.

    def test_mode_line_shows_the_file_and_the_position(self):
        path = self.file("m.txt", "one\ntwo\n")
        session = self.start(path)
        session.send(CTRL["n"] + CTRL["e"])
        session.settle(0.3)
        self.assertIn("m.txt", session.mode_line())
        self.assertIn("(2,3)", session.mode_line())
        self.assertIn("-UUU:---", session.mode_line())
        session.send("!")
        session.settle(0.2)
        self.assertIn("-UUU:-**", session.mode_line())
        session.settle(0.8)  # The autosave clears the modified flag.
        self.assertIn("-UUU:---", session.mode_line())

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
        session.settle(1.2)
        self.assertIn("[disk changed]", session.mode_line())

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
        session.settle(1.0)
        self.assertEqual(self.contents(path), "text!\n")

    def test_external_change_stops_the_autosave_instead_of_clobbering(self):
        path = self.file("x.txt", "mine\n")
        session = self.start(path)
        time.sleep(1.1)  # Make sure the new mtime differs.
        with open(path, "w") as handle:
            handle.write("theirs\n")
        session.send(CTRL["e"] + "!")
        session.settle(1.2)
        self.assertEqual(self.contents(path), "theirs\n")
        self.assertIn("changed on disk", session.screen())

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
        session.settle(0.3)
        self.assertIn("two.py", session.display.row(11))
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
        session.settle(0.5)
        self.assertIn("a.txt:1:", session.screen())
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
        session.settle(0.3)
        self.assertIn("(1,0)", session.display.row(11))
        session.send(CTRL["x"] + "`")
        session.settle(0.3)
        self.assertIn("(3,0)", session.display.row(11))

        # And RET from the results side does the same.
        session.send(CTRL["x"] + "o")
        session.send(CTRL["n"] + CTRL["n"])
        session.send(RET)
        session.settle(0.3)
        self.assertIn("(5,0)", session.display.row(11))
        self.assertIn("o.py", session.display.row(11))

    def test_occur_works_in_a_buffer_that_is_not_a_file(self):
        session = self.start()
        session.send("alpha" + CTRL["j"] + "beta" + CTRL["j"] + "alpha again")
        session.settle(0.3)
        session.send(ESC + "x" + "occur" + RET)
        session.send("alpha" + RET)
        session.settle(0.5)
        self.assertIn("2 matches", session.echo())
        self.assertIn("in buffer: *scratch*", session.screen())
        session.send(CTRL["x"] + "`")
        session.settle(0.3)
        self.assertIn("*scratch*", session.display.row(11))
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
        session.settle(0.4)
        session.send(CTRL["x"] + "o")  # Into the results window.
        session.send("XXX")
        session.settle(0.2)
        self.assertIn("read-only", session.echo())

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
        session.settle(0.3)
        self.assertIn("a.py", session.display.row(11))
        session.send(CTRL["x"] + "`")
        session.settle(0.3)
        self.assertIn("b.py", session.display.row(11))
        self.assertIn("*grep*", session.display.row(22))

    def test_m_x_completes(self):
        path = self.file("c.txt", "text\n")
        session = self.start(path)
        session.send(ESC + "x" + "query-r" + "\t")
        session.settle(0.3)
        self.assertIn("M-x query-replace", session.echo())
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
        session.settle(1.2)
        self.assertEqual(self.contents(path), "somebody else\n")
        self.assertIn("[disk changed]", session.mode_line())

    def test_scratch_buffer_when_no_file_is_given(self):
        session = self.start()
        session.settle(0.3)
        self.assertIn("*scratch*", session.mode_line())
        session.send("typed into scratch")
        session.settle(0.8)
        self.assertIn("typed into scratch", session.display.row(0))
        session.send(CTRL["x"] + CTRL["c"])
        session.process.wait(timeout=5)

    def test_escape_quits_like_control_g(self):
        """A browser terminal may never deliver C-g; ESC has to be enough."""
        path = self.file("esc.txt", "alpha beta\n")
        session = self.start(path)

        session.send(CTRL["x"] + "b")  # A prompt that has to be escapable.
        session.settle(0.2)
        self.assertIn("Switch to buffer", session.echo())
        session.send(ESC)
        session.settle(0.3)
        self.assertIn("ESC-", session.echo())  # A prefix, even here.
        session.send(ESC)
        session.settle(0.3)
        self.assertNotIn("Switch to buffer", session.echo())

        session.send(CTRL["s"] + "beta")  # And a search that has to abort.
        session.settle(0.2)
        self.assertIn("I-search: beta", session.echo())
        session.send(ESC)
        session.settle(0.3)
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
        session.settle(0.3)
        self.assertIn("(101,0)", session.mode_line())

        session.send(ESC)
        session.settle(0.4)
        session.send("<")
        session.settle(0.3)
        self.assertIn("(1,0)", session.mode_line())

    def test_esc_percent_starts_query_replace(self):
        path = self.file("qr.txt", "one two one\n")
        session = self.start(path)
        session.send(ESC)
        session.settle(0.4)
        session.send("%")
        session.settle(0.3)
        self.assertIn("Query replace", session.echo())
        session.send("one" + RET)
        session.send("1" + RET)
        session.settle(0.3)
        session.send("!")
        session.settle(1.0)
        self.assertEqual(self.contents(path), "1 two 1\n")

    def test_esc_esc_quits(self):
        path = self.file("qq.txt", "text\n")
        session = self.start(path)
        session.send(CTRL["x"] + "b")
        session.settle(0.2)
        self.assertIn("Switch to buffer", session.echo())
        session.send(ESC)
        session.settle(0.3)
        session.send(ESC)
        session.settle(0.3)
        self.assertNotIn("Switch to buffer", session.echo())

    def test_split_window_gives_two_mode_lines(self):
        path = self.file("s.txt", "".join("line %d\n" % n
                                          for n in range(1, 101)))
        session = self.start(path)
        session.send(CTRL["x"] + "2")
        session.settle(0.3)
        self.assertIn("s.txt", session.display.row(11))
        self.assertIn("s.txt", session.display.row(22))

        # The two windows keep their own places in the same file.
        session.send(CTRL["x"] + "o")
        session.send(ESC)
        session.settle(0.3)
        session.send(">")
        session.settle(0.3)
        self.assertIn("(1,0)", session.display.row(11))
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
        session.settle(0.4)
        self.assertIn("one.py", session.display.row(11))
        self.assertIn("*grep*", session.display.row(22))

        session.send(CTRL["x"] + "`")  # next-error, from the file window.
        session.settle(0.4)
        self.assertIn("two.py", session.display.row(11))
        self.assertIn("*grep*", session.display.row(22))

    def test_history_survives_the_editor_being_closed(self):
        store = os.path.join(self.directory, "history.json")
        target = self.file("remembered.txt", "hello\n")

        first = self.start(self.file("a.txt", "a\n"), history=store)
        first.send(CTRL["x"] + CTRL["f"])
        first.settle(0.2)
        first.send(CTRL["a"] + CTRL["k"] + target + RET)
        first.settle(0.4)
        self.assertIn("remembered.txt", first.mode_line())
        first.send(CTRL["x"] + CTRL["c"])
        first.process.wait(timeout=5)

        second = self.start(self.file("b.txt", "b\n"), history=store)
        second.send(CTRL["x"] + CTRL["f"])
        second.settle(0.2)
        second.send(ESC)  # ESC p is how M-p is typed without a Meta key.
        second.settle(0.3)
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
        session.settle(0.2)
        session.send(ESC)
        session.settle(0.3)
        session.send("p")
        session.settle(0.3)
        self.assertIn("Grep (regexp): alpha", session.echo())

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
        session.settle(0.3)
        self.assertIn("-UUU:---  F1  u.txt", session.mode_line())
        self.assertIn("(Fundamental)", session.mode_line())

        dos = os.path.join(self.directory, "d.txt")
        with open(dos, "wb") as handle:
            handle.write(b"one\r\ntwo\r\n")
        session.send(CTRL["x"] + CTRL["f"])
        session.settle(0.2)
        session.send(CTRL["a"] + CTRL["k"] + dos + RET)
        session.settle(0.4)
        # The one character of the group that carries news: CRLF.
        self.assertIn("-UUU\\---  F1  d.txt", session.mode_line())

    def test_a_long_answer_scrolls_so_the_cursor_stays_visible(self):
        session = self.start(self.file("s.txt", "text\n"))
        session.send(CTRL["x"] + CTRL["f"])
        session.settle(0.2)
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
        session.settle(0.3)
        self.assertIn("Find file: /directory00", session.echo())
        session.send(CTRL["g"])

    def test_moving_over_parentheses(self):
        path = self.file("p.py", "value = call(a, nest(b), c) + tail\n")
        session = self.start(path)
        session.settle(0.3)

        # C-M-n from the start goes past the whole group, nesting included.
        session.send(ESC)
        session.settle(0.25)
        session.send(CTRL["n"])
        session.settle(0.3)
        self.assertIn("(1,27)", session.mode_line())  # Just past the ")".

        # C-M-p comes back to the "(" that opened it.
        session.send(ESC)
        session.settle(0.25)
        session.send(CTRL["p"])
        session.settle(0.3)
        self.assertIn("(1,12)", session.mode_line())

    def test_an_unbalanced_group_says_so_rather_than_wandering(self):
        path = self.file("u.py", "open(this one never closes\n")
        session = self.start(path)
        session.settle(0.3)
        session.send(ESC)
        session.settle(0.25)
        session.send(CTRL["n"])
        session.settle(0.3)
        self.assertIn("Scan error", session.echo())
        self.assertIn("(1,0)", session.mode_line())  # And did not move.

    def test_paths_are_shown_with_the_home_directory_as_a_tilde(self):
        os.makedirs(os.path.join(self.directory, "work"))
        path = self.file("work/w.txt", "text\n")
        session = self.start(path, home=self.directory)

        session.send(CTRL["x"] + CTRL["f"])
        session.settle(0.3)
        self.assertIn("Find file: ~/work/", session.echo())
        self.assertNotIn(self.directory, session.echo())

        # And it is only how it is shown: completion still resolves it.
        session.send("w.txt" + RET)
        session.settle(0.4)
        self.assertIn("w.txt", session.mode_line())

        # C-x C-s reports where it wrote, abbreviated the same way.
        session.send("!" + CTRL["x"] + CTRL["s"])
        session.settle(0.4)
        self.assertIn("Wrote ~/work/w.txt", session.echo())

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
        session.settle(1.0)
        self.assertEqual(self.contents(path), "run me!\n")
        self.assertIn("r.txt", session.mode_line())

    def test_m_q_fills_the_paragraph_around_point(self):
        long_line = ("This is one very long line of prose that goes well past "
                     "the seventy-four columns that this editor fills to, and "
                     "so has to be broken up into several.")
        path = self.file("f.txt", long_line + "\n\nA second paragraph.\n")
        session = self.start(path)

        session.send(ESC)
        session.settle(0.25)
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
        session.settle(0.25)
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

    def process_state(self, pid: int) -> str:
        """The first letter of the process state, on macOS as well as Linux."""
        output = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                capture_output=True, text=True).stdout
        return output.strip()[:1]


if __name__ == "__main__":
    unittest.main()
