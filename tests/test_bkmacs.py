"""Unit tests for the parts that do not need a terminal.

Run with ``python3 -m unittest discover tests`` -- unittest rather than pytest
so that there is still nothing to install.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bkmacs.history
from bkmacs import crypt, migemo
from bkmacs.buffer import Buffer, KillRing, advance, adjust
from bkmacs.editor import capitalized, in_columns, quoted
from bkmacs.history import History
from bkmacs.layout import (char_width, display_width, expand, fill,
                           index_at_column, joinable, span_at_columns,
                           split_columns, truncate, unfill)
from bkmacs.search import (glob_regexp, search_backward,
                           search_backward_regexp, search_forward,
                           search_forward_regexp)


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

    def test_a_long_line_is_laid_out_in_one_pass(self):
        # The cost of laying a line out has to be the length of the line, not
        # the length times the rows it wraps onto: measuring what was left
        # for every row was quadratic, and a line of sixty-four thousand
        # characters took thirteen seconds to put on the screen.
        line = "x" * 64_000
        start = time.monotonic()
        segments = split_columns(expand(line).text, 80)
        self.assertEqual(len(segments), 811)
        self.assertLess(time.monotonic() - start, 2.0)
        # The last row holds what is left over, and the ones before it give
        # up a column each to the continuation marker.
        self.assertEqual(segments[0].width, 79)
        self.assertEqual(segments[-1].end, 64_000)

    def test_the_fast_path_for_plain_lines_agrees_with_the_slow_one(self):
        # expand answers by arithmetic when every character is one ASCII
        # column, which is most lines; anything else walks them.  The two
        # have to give the same answer where they overlap.
        for text in ("plain ascii text", "", " leading and trailing ",
                     "~!@#$%^&*()_+", "a" * 500):
            plain = expand(text)
            self.assertEqual(plain.text, text)
            self.assertEqual(plain.columns, list(range(len(text) + 1)))
            self.assertEqual(plain.owners, list(range(len(text))))
        # And the walk still happens for everything else.
        self.assertEqual(expand("a\tb").text, "a" + " " * 7 + "b")
        self.assertEqual(expand("あ").columns, [0, 2])

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


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class TestEncrypted(unittest.TestCase):
    """Files written by ``openssl enc`` from the command line.

    The point of these is compatibility, so the fixtures are made by calling
    openssl directly rather than by calling our own encrypt: a round trip
    through one implementation would pass no matter what the format was.
    """

    PASSWORD = "correct horse"

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def openssl_encrypt(self, name: str, text: str, pbkdf2: bool = True) -> str:
        path = self.path(name)
        derivation = ["-pbkdf2", "-iter", str(crypt.ITERATIONS)] if pbkdf2 else []
        subprocess.run(["openssl", "enc", "-e"] + derivation
                       + ["-md", "sha256", "-base64", "-aes-256-cbc", "-salt",
                          "-pass", "pass:" + self.PASSWORD, "-out", path],
                       input=text.encode("utf-8"), check=True,
                       stderr=subprocess.DEVNULL)
        return path

    def openssl_decrypt(self, path: str) -> str:
        done = subprocess.run(
            ["openssl", "enc", "-d", "-pbkdf2", "-iter",
             str(crypt.ITERATIONS), "-md", "sha256", "-base64",
             "-aes-256-cbc", "-pass", "pass:" + self.PASSWORD, "-in", path],
            stdout=subprocess.PIPE, check=True, stderr=subprocess.DEVNULL)
        return done.stdout.decode("utf-8")

    def test_reads_what_openssl_wrote(self):
        path = self.openssl_encrypt("notes.ossl", "秘密\nsecond line\n")
        buffer = Buffer.from_file(path)
        self.assertTrue(buffer.locked and buffer.read_only)
        self.assertEqual(buffer.lines, [""])  # Nothing until it is unlocked.
        self.assertFalse(buffer.decrypt_with(self.PASSWORD))
        self.assertEqual(buffer.lines, ["秘密", "second line", ""])
        self.assertFalse(buffer.locked or buffer.read_only or buffer.modified)

    def test_openssl_reads_what_we_wrote(self):
        path = self.openssl_encrypt("notes.ossl", "one\n")
        buffer = Buffer.from_file(path)
        buffer.decrypt_with(self.PASSWORD)
        buffer.point = (1, 0)
        buffer.insert("two\n")
        buffer.save()
        self.assertEqual(self.openssl_decrypt(path), "one\ntwo\n")

    def test_the_old_key_derivation_is_read_and_migrated(self):
        path = self.openssl_encrypt("old.ossl", "ancient\n", pbkdf2=False)
        buffer = Buffer.from_file(path)
        self.assertTrue(buffer.decrypt_with(self.PASSWORD))  # Said so.
        buffer.insert("x")
        buffer.save()
        self.assertEqual(self.openssl_decrypt(path), "xancient\n")  # Now PBKDF2.

    def test_a_wrong_password_is_refused(self):
        path = self.openssl_encrypt("notes.ossl", "secret\n")
        buffer = Buffer.from_file(path)
        with self.assertRaises(crypt.CryptError):
            buffer.decrypt_with("horse correct")
        self.assertTrue(buffer.locked)  # Still shut, and still empty.
        self.assertEqual(buffer.lines, [""])

    def test_nothing_plain_is_left_behind_and_the_file_is_private(self):
        path = self.path("new.ossl")
        buffer = Buffer.from_file(path)  # A file that does not exist yet.
        self.assertTrue(buffer.encrypted)
        self.assertFalse(buffer.locked)
        buffer.password = self.PASSWORD
        buffer.insert("plain text\n")
        buffer.save()
        self.assertEqual(os.listdir(self.directory), ["new.ossl"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, "rb") as handle:
            self.assertNotIn(b"plain", handle.read())
        self.assertEqual(self.openssl_decrypt(path), "plain text\n")

    def test_saving_without_a_password_refuses_rather_than_writing_plain(self):
        buffer = Buffer.from_file(self.path("new.ossl"))
        buffer.insert("secret\n")
        with self.assertRaises(ValueError):
            buffer.save()
        self.assertEqual(os.listdir(self.directory), [])


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


class TestSearchRegexp(unittest.TestCase):
    def buffer(self, text: str) -> Buffer:
        return Buffer("t", None, text.split("\n"))

    def test_forward_answers_with_the_whole_span(self):
        buffer = self.buffer("one\n吾輩は猫である")
        found = search_forward_regexp(buffer, re.compile("猫で?"), (0, 0))
        self.assertEqual(found, ((1, 3), (1, 5)))

    def test_backward_takes_the_last_match_before_the_start(self):
        buffer = self.buffer("ab ab\nab")
        regexp = re.compile("ab")
        self.assertEqual(search_backward_regexp(buffer, regexp, (1, 0)),
                         ((0, 3), (0, 5)))
        self.assertEqual(search_backward_regexp(buffer, regexp, (0, 3)),
                         ((0, 0), (0, 2)))

    def test_an_empty_match_does_not_count_as_a_hit(self):
        buffer = self.buffer("xxa")
        found = search_forward_regexp(buffer, re.compile("b*a?"), (0, 0))
        self.assertEqual(found, ((0, 2), (0, 3)))

    def test_bound_stops_the_search(self):
        buffer = self.buffer("one\ntwo\nthree")
        regexp = re.compile("t..")
        self.assertIsNone(search_forward_regexp(buffer, regexp, (0, 0),
                                                bound=(1, 2)))
        self.assertEqual(search_forward_regexp(buffer, regexp, (0, 0),
                                               bound=(1, 3)),
                         ((1, 0), (1, 3)))


class TestMigemo(unittest.TestCase):
    def test_romaji_becomes_kana(self):
        self.assertEqual(migemo.to_hiragana("kensaku"), ("けんさく", ""))
        self.assertEqual(migemo.to_hiragana("nihon"), ("にほん", ""))
        self.assertEqual(migemo.to_hiragana("shashin"), ("しゃしん", ""))
        self.assertEqual(migemo.to_hiragana("syashin"), ("しゃしん", ""))
        self.assertEqual(migemo.to_hiragana("kitte"), ("きって", ""))
        self.assertEqual(migemo.to_hiragana("tsukau"), ("つかう", ""))

    def test_n_is_the_awkward_one(self):
        # ん before a consonant, and な when a vowel is coming even though the
        # letters are the same either way: konbanwa, konna, kanji, kannji.
        self.assertEqual(migemo.to_hiragana("konbanwa"), ("こんばんわ", ""))
        self.assertEqual(migemo.to_hiragana("konna"), ("こんな", ""))
        self.assertEqual(migemo.to_hiragana("onna"), ("おんな", ""))
        self.assertEqual(migemo.to_hiragana("kanji"), ("かんじ", ""))
        self.assertEqual(migemo.to_hiragana("kannji"), ("かんじ", ""))
        self.assertEqual(migemo.to_hiragana("kon'yaku"), ("こんやく", ""))
        self.assertEqual(migemo.to_hiragana("shinnyuu"), ("しんにゅう", ""))
        # Half typed: こん is a word, so a pair of them is not left waiting.
        self.assertEqual(migemo.to_hiragana("hon"), ("ほん", ""))
        self.assertEqual(migemo.to_hiragana("konn"), ("こん", ""))
        self.assertEqual(migemo.to_hiragana("konnb"), ("こん", "b"))

    def test_a_half_typed_syllable_is_kept_as_the_tail(self):
        self.assertEqual(migemo.to_hiragana("kensak"), ("けんさ", "k"))
        self.assertEqual(migemo.to_hiragana("kensaky"), ("けんさ", "ky"))
        # The doubled letter is already っ; what is left is half of the next.
        self.assertEqual(migemo.to_hiragana("kitt"), ("きっ", "t"))

    def test_the_tail_says_which_kana_can_follow(self):
        self.assertEqual(migemo.expansions("k"),
                         ["か", "き", "きゃ", "きゅ", "きょ", "く", "け", "こ",
                          "っ"])  # And っ, since kk is how っ is typed.
        # A lone n is ん as much as it is the start of な.
        self.assertIn("ん", migemo.expansions("n"))
        self.assertIn("な", migemo.expansions("n"))

    def test_a_consonant_may_still_be_the_front_of_a_doubled_one(self):
        # っ is typed by doubling the consonant after it, so the t of set has
        # not decided whether it is せた or せっ -- and only one of those is
        # 設定.  Both are open until the next letter.
        self.assertIn("た", migemo.expansions("t"))
        self.assertIn("っ", migemo.expansions("t"))
        self.assertNotIn("っ", migemo.expansions("n"))   # n doubles into ん.
        self.assertNotIn("っ", migemo.expansions("ky"))  # Past the doubling.
        self.assertIn("せっ", migemo.prefixes("set"))
        self.assertIn("せた", migemo.prefixes("set"))

    def test_prefixes_of_a_half_typed_word(self):
        self.assertEqual(migemo.prefixes("kensaku"), ["けんさく"])
        self.assertEqual(migemo.prefixes("nez"),
                         ["ねざ", "ねじ", "ねじゃ", "ねじゅ", "ねじょ",
                          "ねず", "ねぜ", "ねぞ", "ねっ"])

    def test_katakana(self):
        self.assertEqual(migemo.katakana("けんさく"), "ケンサク")
        self.assertEqual(migemo.katakana("こーひー"), "コーヒー")

    def test_alternation_shares_what_the_words_share(self):
        self.assertEqual(migemo.alternation(["検索", "検査"]), "検[査索]")
        self.assertEqual(migemo.alternation(["検索", "検査", "研削"]),
                         "(?:検[査索]|研削)")
        # A word that is the beginning of another leaves the rest optional,
        # and the group is what keeps the ? from landing on one character.
        self.assertEqual(migemo.alternation(["検索", "検索結果"]),
                         "検索(?:結果)?")

    def test_a_pattern_finds_the_romaji_the_kana_and_the_katakana(self):
        regexp = re.compile(migemo.pattern("kensaku", words=migemo.Dictionary(
            os.devnull)))
        for text in ("kensaku", "けんさく", "ケンサク"):
            self.assertRegex(text, regexp)
        self.assertNotRegex("けんさ", regexp)

    def test_katakana_ignores_the_marks_that_split_a_name(self):
        # Whether a file writes ヴーヴ・クリコ or ヴーヴクリコ is not something
        # the person searching for it knows, so neither spelling is a miss --
        # and neither one has to be in the dictionary, since this is the
        # katakana the reading makes rather than a word that was looked up.
        nothing = migemo.Dictionary(os.devnull)
        regexp = re.compile(migemo.pattern("vu-vukuriko", words=nothing))
        self.assertRegex("ヴーヴ・クリコ", regexp)
        self.assertRegex("ヴーヴクリコ", regexp)
        self.assertRegex("ヴーヴ･クリコ", regexp)  # And in half width.
        regexp = re.compile(migemo.pattern("janpo-ru", words=nothing))
        self.assertRegex("ジャン＝ポール", regexp)
        # The mark is allowed, not required, and only between characters.
        self.assertNotRegex("ヴーヴ・クリ", re.compile(
            migemo.pattern("vu-vukuriko", words=nothing)))

    def test_a_missing_dictionary_leaves_the_kana(self):
        words = migemo.Dictionary("/no/such/dictionary")
        self.assertFalse(words.available)
        self.assertEqual(words.words("けんさく"), [])
        self.assertRegex("ケンサク", re.compile(migemo.pattern("kensaku",
                                                              words=words)))


class TestMigemoDictionary(unittest.TestCase):
    """The dictionary that ships with the editor, searched where it lies."""

    @classmethod
    def setUpClass(cls):
        if not migemo.dictionary.available:
            raise unittest.SkipTest("no migemo.dict in this checkout")

    def test_a_reading_finds_the_words_written_with_it(self):
        self.assertIn("検索", migemo.dictionary.words("けんさく"))
        self.assertIn("実装", migemo.dictionary.words("じっそう"))
        # 外字 is rank 152,159 by how often Mozc sees it written, and the
        # first word you would look for in a program that reads EPWING
        # dictionaries.  It is here because two-kanji words are discounted.
        self.assertIn("外字", migemo.dictionary.words("がいじ"))

    def test_a_partial_reading_finds_the_longer_ones(self):
        self.assertIn("実装", migemo.dictionary.words("じっそ"))
        # 鼠 is the word migemo's own page opens by finding, and it is only
        # here because the first spelling of a reading is ranked by how
        # ordinary the reading is: writing ねずみ as 鼠 is not ordinary.
        self.assertIn("鼠", migemo.dictionary.words("ねず"))
        self.assertRegex("鼠", re.compile(migemo.pattern("nez")))

    def test_the_bisection_lands_on_the_first_line_of_the_range(self):
        # Every word that comes back has to belong to a reading that starts
        # with what was asked for; landing one line early is the failure this
        # is here to catch, and it is invisible in the answer otherwise.
        for reading in ("あ", "けんさく", "ん", "こんぴゅーた"):
            found = migemo.dictionary.words(reading, 5)
            self.assertTrue(all(found), reading)

    def test_a_reading_nothing_starts_with_finds_nothing(self):
        self.assertEqual(migemo.dictionary.words("ゑゑゑゑ"), [])

    def test_the_limit_is_honoured(self):
        self.assertEqual(len(migemo.dictionary.words("か", 7)), 7)

    def test_the_words_are_in_the_pattern(self):
        regexp = re.compile(migemo.pattern("kensaku"))
        for text in ("検索", "検索結果", "けんさく", "kensaku"):
            self.assertRegex(text, regexp)

    def test_a_longer_query_never_finds_what_a_shorter_one_missed(self):
        # けい has three hundred words under it, and a pattern that read part
        # of that and stopped left behind whichever readings sort last: kei
        # found 刑 and 京 and walked past 経由, and then keiy, which asks for
        # strictly less, found it.  Typing more may only take matches away.
        for query in ("kei", "keiy", "keiyu"):
            self.assertRegex("経由", re.compile(migemo.pattern(query)))

    def test_one_letter_matches_where_those_words_begin(self):
        # d is で and だ and ど and ten thousand words, too many to spell out,
        # so the pattern is the characters they start with -- all of them,
        # rather than the two hundred that sort first, which is what used to
        # find だ and walk past 電子.
        regexp = re.compile(migemo.pattern("d"))
        for text in ("電子ブック", "だんだん", "データ", "ndtpd"):
            self.assertRegex(text, regexp)
        self.assertEqual(regexp.search("電子ブック").group(0), "電")
        self.assertNotRegex("猫", regexp)

    def test_a_word_with_a_doubled_consonant_is_found_before_it_is_typed(self):
        # せってい is not reachable from せ by any single kana, only by せっ,
        # so a t that could not still turn into っ made set fail and sett
        # work -- which is a strange thing for one keystroke to decide.
        self.assertRegex("設定", re.compile(migemo.pattern("set")))
        self.assertRegex("学校", re.compile(migemo.pattern("gak")))
        self.assertRegex("切手", re.compile(migemo.pattern("kit")))

    def test_a_half_typed_syllable_still_narrows_the_search(self):
        # けん and an s that is not a kana yet: けんさ, けんし, けんす and the
        # rest, but not けんと, and so 検索 and 検査 but not 検討.
        regexp = re.compile(migemo.pattern("kens"))
        for text in ("検索", "検査", "検察"):
            self.assertRegex(text, regexp)
        self.assertNotRegex("検討", regexp)

    def test_the_file_is_sorted_which_is_what_makes_it_searchable(self):
        with open(migemo.DICTIONARY, encoding="utf-8") as handle:
            readings = [line.split("\t")[0] for line in handle
                        if not line.startswith(";;")]
        self.assertEqual(readings, sorted(readings))

    def test_the_dictionary_carries_the_notice_that_covers_it(self):
        # IPAdic asks that any copy of the words carry its notice, and two
        # megabytes of words is the sort of file that gets taken out of a
        # checkout on its own.  NOTICE is the original and the head of the
        # dictionary is the copy; this is what keeps them the same text.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "NOTICE"), encoding="utf-8") as handle:
            notice = handle.read()
        with open(migemo.DICTIONARY, encoding="utf-8") as handle:
            head = [line[3:].rstrip() for line in handle
                    if line.startswith(";;")]
        for line in notice.splitlines():
            self.assertIn(line.rstrip(), head)

    def test_no_reading_is_longer_than_a_search_will_ever_be(self):
        small = "ぁぃぅぇぉゃゅょゎっ"
        with open(migemo.DICTIONARY, encoding="utf-8") as handle:
            longest = max(sum(1 for c in line.split("\t")[0] if c not in small)
                          for line in handle if not line.startswith(";;"))
        self.assertLessEqual(longest, 5)
        # Beats, not characters: じょうきょう is six characters and four
        # beats, and counting characters would have thrown 状況 away.
        self.assertIn("状況", migemo.dictionary.words("じょうきょう"))
        # What is past the cut is not lost, only shorter: 東京大学 has no
        # reading of its own any more and is found by 東京 instead.
        self.assertEqual(migemo.dictionary.words("とうきょうだいがく"), [])
        self.assertRegex("東京大学", re.compile(migemo.pattern("toukyou")))

    def test_the_file_holds_no_word_that_needs_no_looking_up(self):
        # The reading, and the reading in katakana with or without the marks
        # that split a foreign name, are in every pattern already, and a word
        # written in ASCII is one the keyboard can type.  A line carrying any
        # of those back is a line that costs bytes to say nothing.
        marks = str.maketrans("", "", "・･゠＝")
        with open(migemo.DICTIONARY, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(";;"):
                    continue
                fields = line.rstrip("\n").split("\t")
                spellings = (fields[0], migemo.katakana(fields[0]))
                for word in fields[1:]:
                    self.assertNotIn(word.translate(marks), spellings, line)
                    self.assertFalse(word.isascii(), line)


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


class TestCapitalized(unittest.TestCase):
    def test_the_first_letter_goes_up_and_the_rest_come_down(self):
        self.assertEqual(capitalized("hello"), "Hello")
        self.assertEqual(capitalized("hELLO"), "Hello")

    def test_the_letter_is_found_past_whatever_is_in_front_of_it(self):
        # M-c takes the word from point, and point is usually on the space
        # before it -- where str.capitalize would capitalize the space and
        # call it done.
        self.assertEqual(capitalized(" hello"), " Hello")
        self.assertEqual(capitalized("  1st"), "  1St")

    def test_a_word_with_no_letters_in_it_is_left_alone(self):
        self.assertEqual(capitalized("123"), "123")
        self.assertEqual(capitalized("日本語"), "日本語")


class TestQuoted(unittest.TestCase):
    def test_a_printable_key_is_itself(self):
        self.assertEqual(quoted("x"), "x")
        self.assertEqual(quoted("あ"), "あ")

    def test_a_named_key_is_the_character_it_was(self):
        self.assertEqual(quoted("TAB"), "\t")
        self.assertEqual(quoted("RET"), "\r")
        self.assertEqual(quoted("C-l"), "\x0c")
        self.assertEqual(quoted("C-@"), "\0")

    def test_a_key_that_is_no_character_at_all(self):
        self.assertIsNone(quoted("M-x"))
        self.assertIsNone(quoted("<resize>"))


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
