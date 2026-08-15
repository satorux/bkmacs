"""Unit tests for the parts that do not need a terminal.

Run with ``python3 -m unittest discover tests`` -- unittest rather than pytest
so that there is still nothing to install.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bkmacs.history
from bkmacs.buffer import Buffer, KillRing, advance, adjust
from bkmacs.editor import in_columns
from bkmacs.history import History
from bkmacs.layout import (char_width, display_width, expand, fill,
                           index_at_column, joinable, span_at_columns,
                           split_columns, truncate, unfill)
from bkmacs.search import glob_regexp, search_backward, search_forward


class TestLayout(unittest.TestCase):
    def test_width(self):
        self.assertEqual(char_width("a"), 1)
        self.assertEqual(char_width("漢"), 2)
        self.assertEqual(char_width("́"), 0)  # Combining acute.
        self.assertEqual(display_width("あa"), 3)

    def test_truncate_keeps_wide_characters_whole(self):
        self.assertEqual(truncate("あい", 3), "あ")
        self.assertEqual(truncate("あい", 4), "あい")

    def test_expand_tabs_to_the_next_stop(self):
        expanded = expand("a\tb")
        self.assertEqual(expanded.text, "a" + " " * 7 + "b")
        self.assertEqual(expanded.columns, [0, 1, 8, 9])
        self.assertEqual(expanded.owners[1], 1)  # The spaces belong to the tab.
        self.assertEqual(expanded.owners[8], 2)

    def test_expand_control_and_raw_bytes(self):
        self.assertEqual(expand("\x01").text, "^A")
        self.assertEqual(expand("\x7f").text, "^?")
        self.assertEqual(expand("\udcc3").text, "\\303")

    def test_expand_measures_wide_characters(self):
        self.assertEqual(expand("あa").columns, [0, 2, 3])

    def test_index_at_column_never_lands_inside_a_wide_character(self):
        columns = expand("あa").columns
        self.assertEqual(index_at_column(columns, 0), 0)
        self.assertEqual(index_at_column(columns, 1), 0)  # Second half of あ.
        self.assertEqual(index_at_column(columns, 2), 1)
        self.assertEqual(index_at_column(columns, 99), 2)  # End of line.

    def test_span_at_columns_takes_only_whole_characters(self):
        columns = expand("aあいb").columns  # Columns 0, 1, 3, 5.
        self.assertEqual(span_at_columns(columns, 1, 5), (1, 3))  # あい.
        # Both edges inside a wide character, which is therefore left alone.
        self.assertEqual(span_at_columns(columns, 2, 4), (2, 2))
        self.assertEqual(span_at_columns(columns, 0, 99), (0, 4))
        # No width is a place to insert, and covers nothing.
        self.assertEqual(span_at_columns(columns, 3, 3), (2, 2))
        # Past the end of the line: nothing to take, and still a valid index.
        self.assertEqual(span_at_columns(columns, 20, 30), (4, 4))

    def test_split_leaves_room_for_the_continuation_marker(self):
        segments = split_columns("abcdefghij", 5)
        # Four columns of text per continued row, five on the last.
        self.assertEqual([(s.start, s.end) for s in segments],
                         [(0, 4), (4, 8), (8, 10)])

    def test_split_does_not_break_a_wide_character(self):
        segments = split_columns("あいう", 5)
        self.assertEqual([(s.start, s.end) for s in segments], [(0, 2), (2, 3)])
        self.assertEqual(segments[0].width, 4)  # One column left before the \.

    def test_short_line_is_one_segment(self):
        self.assertEqual(len(split_columns("abc", 80)), 1)
        self.assertEqual(len(split_columns("", 80)), 1)


class TestFill(unittest.TestCase):
    def test_latin_breaks_at_spaces(self):
        text = "The quick brown fox jumps over the lazy dog"
        self.assertEqual(fill(text, 20),
                         ["The quick brown fox", "jumps over the lazy", "dog"])

    def test_japanese_breaks_between_characters(self):
        lines = fill("吾輩は猫である。名前はまだ無い。", 12)
        self.assertEqual(lines, ["吾輩は猫であ", "る。名前はま", "だ無い。"])
        for line in lines:
            self.assertLessEqual(display_width(line), 12)

    def test_a_full_stop_never_starts_a_line(self):
        # Six columns of the eight would be これは猫, but that leaves 。 at
        # the head of the next line.  So the break moves back one and 猫。
        # goes down together -- what typesetters call oidashi, pushing out,
        # rather than letting the stop hang past the right margin.
        lines = fill("これは猫。それは犬。", 8)
        self.assertEqual(lines, ["これは", "猫。それ", "は犬。"])
        for line in lines:
            self.assertNotEqual(line[0], "。")
            self.assertLessEqual(display_width(line), 8)

    def test_an_opening_bracket_never_ends_a_line(self):
        self.assertNotEqual(fill("あいう「かきくけこ」", 8)[0][-1], "「")

    def test_a_word_longer_than_the_width_overruns_rather_than_splits(self):
        self.assertEqual(fill("a supercalifragilistic word", 10),
                         ["a", "supercalifragilistic", "word"])

    def test_unfill_joins_latin_with_a_space_and_japanese_without(self):
        self.assertEqual(unfill(["hello", "world"]), "hello world")
        self.assertEqual(unfill(["吾輩は", "猫である"]), "吾輩は猫である")
        self.assertEqual(unfill(["hello", "世界"]), "hello 世界")

    def test_filling_is_stable(self):
        text = ("Japanese makes this non-obvious: a character can occupy two "
                "columns, or zero when it is a combining mark.")
        once = fill(text, 40)
        self.assertEqual(fill(unfill(once), 40), once)

    def test_break_rules(self):
        self.assertTrue(joinable("ab cd", 3))  # After a space.
        self.assertFalse(joinable("abcd", 2))  # Inside a Latin word.
        self.assertTrue(joinable("あい", 1))  # Between two wide characters.


class TestBuffer(unittest.TestCase):
    def buffer(self, text: str) -> Buffer:
        return Buffer("t", None, text.split("\n"))

    def test_advance(self):
        self.assertEqual(advance((0, 0), "abc"), (0, 3))
        self.assertEqual(advance((2, 5), "ab\ncd"), (3, 2))

    def test_insert_and_delete(self):
        buffer = self.buffer("hello")
        buffer.point = (0, 5)
        buffer.insert(" world")
        self.assertEqual(buffer.text(), "hello world")
        buffer.edit((0, 5), (0, 11), "")
        self.assertEqual(buffer.text(), "hello")

    def test_newline_splits_and_joins(self):
        buffer = self.buffer("ab")
        buffer.point = (0, 1)
        buffer.insert("\n")
        self.assertEqual(buffer.lines, ["a", "b"])
        self.assertEqual(buffer.point, (1, 0))
        buffer.edit((0, 1), (1, 0), "")
        self.assertEqual(buffer.lines, ["ab"])

    def test_mark_follows_edits_elsewhere(self):
        buffer = self.buffer("one\ntwo\nthree")
        buffer.mark = (2, 1)
        buffer.edit((0, 0), (0, 0), "x\ny\n")
        self.assertEqual(buffer.mark, (4, 1))

    def test_mark_inside_a_deletion_collapses(self):
        self.assertEqual(adjust((1, 2), (0, 1), (2, 0), (0, 1)), (0, 1))

    def test_undo_then_redo_by_undoing_the_undo(self):
        buffer = self.buffer("abc")
        buffer.point = (0, 3)
        buffer.insert("!")
        buffer.close_group()
        self.assertEqual(buffer.text(), "abc!")

        self.assertTrue(buffer.undo(continuing=False))
        self.assertEqual(buffer.text(), "abc")

        # Not continuing a run of undos, so this undoes the undo: Emacs' redo.
        self.assertTrue(buffer.undo(continuing=False))
        self.assertEqual(buffer.text(), "abc!")

    def test_undo_walks_back_through_a_run(self):
        buffer = self.buffer("")
        for character in "abc":
            buffer.insert(character)
            buffer.close_group()
        self.assertEqual(buffer.text(), "abc")
        buffer.undo(continuing=False)
        buffer.undo(continuing=True)
        self.assertEqual(buffer.text(), "a")
        buffer.undo(continuing=True)
        self.assertEqual(buffer.text(), "")
        self.assertFalse(buffer.undo(continuing=True))

    def test_undo_restores_point(self):
        buffer = self.buffer("abc")
        buffer.point = (0, 1)
        buffer.insert("XY")
        buffer.close_group()
        buffer.point = (0, 0)
        buffer.undo(continuing=False)
        self.assertEqual(buffer.point, (0, 1))


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def test_round_trip_with_trailing_newline(self):
        path = self.path("a.txt")
        with open(path, "w") as handle:
            handle.write("one\ntwo\n")
        buffer = Buffer.from_file(path)
        self.assertEqual(buffer.lines, ["one", "two", ""])
        buffer.save()
        with open(path) as handle:
            self.assertEqual(handle.read(), "one\ntwo\n")

    def test_round_trip_without_trailing_newline(self):
        path = self.path("b.txt")
        with open(path, "w") as handle:
            handle.write("one\ntwo")
        buffer = Buffer.from_file(path)
        buffer.save()
        with open(path) as handle:
            self.assertEqual(handle.read(), "one\ntwo")

    def test_crlf_survives(self):
        path = self.path("c.txt")
        with open(path, "wb") as handle:
            handle.write(b"one\r\ntwo\r\n")
        buffer = Buffer.from_file(path)
        self.assertEqual(buffer.lines, ["one", "two", ""])
        buffer.save()
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"one\r\ntwo\r\n")

    def test_invalid_bytes_survive(self):
        path = self.path("d.bin")
        with open(path, "wb") as handle:
            handle.write(b"a\xc3(b\n")
        buffer = Buffer.from_file(path)
        buffer.save()
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"a\xc3(b\n")

    def test_save_keeps_permissions_and_replaces_atomically(self):
        path = self.path("e.txt")
        with open(path, "w") as handle:
            handle.write("x")
        os.chmod(path, 0o640)
        buffer = Buffer.from_file(path)
        buffer.insert("y")
        buffer.save()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(os.listdir(self.directory), ["e.txt"])

    def test_external_change_is_noticed(self):
        path = self.path("f.txt")
        with open(path, "w") as handle:
            handle.write("x")
        buffer = Buffer.from_file(path)
        self.assertFalse(buffer.externally_changed())
        os.utime(path, (0, 0))
        self.assertTrue(buffer.externally_changed())

    def test_missing_file_opens_empty(self):
        buffer = Buffer.from_file(self.path("new.txt"))
        self.assertEqual(buffer.lines, [""])
        self.assertIsNone(buffer.disk_mtime)


class TestKillRing(unittest.TestCase):
    def test_consecutive_kills_join(self):
        ring = KillRing()
        ring.kill("one\n")
        ring.kill("two\n", append=True)
        self.assertEqual(ring.current(), "one\ntwo\n")

    def test_backward_kills_join_at_the_front(self):
        ring = KillRing()
        ring.kill("world")
        ring.kill("hello ", prepend=True)
        self.assertEqual(ring.current(), "hello world")

    def test_rotate(self):
        ring = KillRing()
        ring.kill("a")
        ring.kill("b")
        self.assertEqual(ring.current(), "b")
        self.assertEqual(ring.rotate(), "a")
        self.assertEqual(ring.rotate(), "b")


class TestSearch(unittest.TestCase):
    def buffer(self, text: str) -> Buffer:
        return Buffer("t", None, text.split("\n"))

    def test_forward_across_lines(self):
        buffer = self.buffer("one\ntwo\nthree")
        self.assertEqual(search_forward(buffer, "two", (0, 0)), (1, 0))
        self.assertEqual(search_forward(buffer, "e", (0, 3)), (2, 3))
        self.assertIsNone(search_forward(buffer, "zzz", (0, 0)))

    def test_backward(self):
        buffer = self.buffer("one\ntwo\none")
        self.assertEqual(search_backward(buffer, "one", (2, 3)), (2, 0))
        self.assertEqual(search_backward(buffer, "one", (2, 0)), (0, 0))

    def test_lower_case_ignores_case_and_upper_case_does_not(self):
        buffer = self.buffer("Hello World")
        self.assertEqual(search_forward(buffer, "hello", (0, 0)), (0, 0))
        self.assertIsNone(search_forward(buffer, "Hello world", (0, 0)))

    def test_bound_stops_the_search(self):
        buffer = self.buffer("one\ntwo\nthree")
        self.assertIsNone(search_forward(buffer, "three", (0, 0), bound=(1, 3)))
        self.assertEqual(search_forward(buffer, "two", (0, 0), bound=(1, 3)),
                         (1, 0))

    def test_japanese(self):
        buffer = self.buffer("吾輩は猫である\n名前はまだ無い")
        self.assertEqual(search_forward(buffer, "猫", (0, 0)), (0, 3))
        self.assertEqual(search_forward(buffer, "は", (0, 3)), (1, 2))


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "history.json")

    def test_newest_first_and_no_duplicates(self):
        history = History(self.path)
        history.add("file", "a.txt")
        history.add("file", "b.txt")
        history.add("file", "a.txt")  # Moves back to the front, not a second.
        self.assertEqual(history.get("file"), ["a.txt", "b.txt"])

    def test_rings_are_separate(self):
        history = History(self.path)
        history.add("file", "a.txt")
        history.add("grep", "needle")
        self.assertEqual(history.get("grep"), ["needle"])
        self.assertEqual(history.get("file"), ["a.txt"])

    def test_survives_a_round_trip_and_stays_private(self):
        history = History(self.path)
        history.add("grep", "吾輩は猫")
        self.assertEqual(History(self.path).get("grep"), ["吾輩は猫"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_damaged_file_is_ignored_rather_than_fatal(self):
        with open(self.path, "w") as handle:
            handle.write("this is not json")
        self.assertEqual(History(self.path).get("file"), [])

    def test_the_ring_is_capped(self):
        history = History(self.path)
        for number in range(bkmacs.history.LIMIT + 20):
            history.add("file", "file%d" % number)
        self.assertEqual(len(history.get("file")), bkmacs.history.LIMIT)
        self.assertEqual(history.get("file")[0],
                         "file%d" % (bkmacs.history.LIMIT + 19))


class TestColumns(unittest.TestCase):
    def test_candidates_are_laid_out_in_columns(self):
        # Widest item plus two, so three four-column slots fit across twelve.
        self.assertEqual(in_columns(["aa", "bb", "cc", "dd"], 12),
                         ["aa  bb  cc", "dd"])

    def test_wide_characters_are_measured_not_counted(self):
        # 漢字 is four columns, so it is padded to six, not to eight.
        self.assertEqual(in_columns(["漢字", "ab"], 40), ["漢字  ab"])

    def test_a_narrow_screen_still_gives_one_per_row(self):
        self.assertEqual(in_columns(["long-name", "x"], 4),
                         ["long-name", "x"])


class TestGlob(unittest.TestCase):
    def matches(self, pattern: str, path: str) -> bool:
        return glob_regexp(pattern).match(path) is not None

    def test_a_bare_pattern_applies_at_any_depth(self):
        self.assertTrue(self.matches("*.py", "editor.py"))
        self.assertTrue(self.matches("*.py", "bkmacs/editor.py"))
        self.assertTrue(self.matches("*.py", "a/b/c/editor.py"))
        self.assertFalse(self.matches("*.py", "notes.txt"))

    def test_double_star_crosses_directories(self):
        self.assertTrue(self.matches("**/*.py", "bkmacs/editor.py"))
        self.assertTrue(self.matches("**/*.py", "a/b/c.py"))
        # zsh and git both match a top-level file here, so this does too.
        self.assertTrue(self.matches("**/*.py", "editor.py"))

    def test_a_leading_directory_pins_the_search(self):
        self.assertTrue(self.matches("tests/*.py", "tests/test_bkmacs.py"))
        self.assertFalse(self.matches("tests/*.py", "bkmacs/editor.py"))
        self.assertFalse(self.matches("tests/*.py", "a/tests/x.py"))

    def test_single_star_stops_at_a_slash(self):
        self.assertFalse(self.matches("bkmacs/*.py", "bkmacs/deep/editor.py"))

    def test_question_mark_and_classes(self):
        self.assertTrue(self.matches("?.py", "a.py"))
        self.assertFalse(self.matches("?.py", "ab.py"))
        self.assertTrue(self.matches("*.[ch]", "src/main.c"))
        self.assertFalse(self.matches("*.[ch]", "src/main.o"))

    def test_dots_are_literal(self):
        self.assertFalse(self.matches("*.py", "pyx"))
        self.assertTrue(self.matches("Makefile", "sub/Makefile"))


if __name__ == "__main__":
    unittest.main()
