"""Searching Japanese by typing romaji, which is what migemo does.

Incremental search is the one command that cannot wait for a kana-kanji
conversion.  ``C-s`` searches while you type, and a conversion does not
produce anything until it is finished and confirmed, so searching for 検索 on
a Japanese keyboard means composing the word somewhere else and pasting it
into the search -- by which time it is not incremental any more.

Migemo's answer is to search for what the romaji *might* become instead of
waiting to be told.  Every keystroke is turned into a regexp covering the
romaji itself, the kana it spells, and the words that are written that way::

    kensaku -> けんさく -> (?:kensaku|けんさく|ケンサク|検[索査]|研削|健作|...)

and that regexp matches 検索 in the buffer at the third or fourth letter,
long before the whole word has been typed.  Nothing is ever converted and
nothing is confirmed; the pattern is thrown away and rebuilt on the next
keystroke.

Half-typed romaji is the interesting case, and the reason this is not just a
lookup table.  ``kensak`` is けんさ and a k that is not a kana yet, and the k
is not noise: it says the next sound is か, き, く, け, こ, one of きゃ, きゅ,
きょ, or the っ that a doubled consonant is typed as, and nothing else.  So
the search narrows on every letter rather than only on every syllable, which
is what makes it feel like a search rather than like an input method.

One letter is where this stops being a lookup: ``d`` is で and だ and ど, and
the dictionary has ten thousand words under those.  A pattern that reads part
of that and stops is not a smaller pattern, it is a wrong one -- what it drops
are the readings that sort last, so ``d`` finds だ and walks past 電子.  Above
:data:`SPELLED_OUT` words the pattern says what those words *begin* with
instead, for all of them; see :func:`initials`.

The dictionary is a text file sorted by reading, and it is searched where it
lies -- a couple of dozen seeks into ``migemo.dict`` rather than two megabytes
read into memory at startup for a command that may never be used.  Sorted
order is therefore load-bearing; see ``tools/make-migemo-dict.py``, which
writes it.

What this leaves out of Ruby/Migemo is the part that its Emacs cannot do
without: matching across a line break by threading ``\\s*`` between the
characters, so that a word M-q has wrapped through the middle of is still
found.  Here a search already runs a line at a time, so a pattern that could
span lines would only be a pattern that could not be found.

Case is the editor's own rule rather than migemo's: ``kensaku`` ignores it and
``Kensaku`` does not, and since kana and kanji have no case to fold, what a
capital letter changes is the ASCII half of the pattern.  See :func:`compile`.
"""

from __future__ import annotations

import os
import re
from typing import Optional

#: Where hiragana lives, and the distance from a hiragana to its katakana.
FIRST, LAST, SHIFT = "ぁ", "ゖ", 0x60

#: What may sit between two katakana without being part of the word: the
#: interpunct that splits a foreign name into its parts, in both widths, and
#: the double hyphen that does the same job in ジャン＝ポール.  Written into
#: every katakana pattern as an optional character, because whether a
#: particular file spells it ヴーヴ・クリコ or ヴーヴクリコ is not something
#: the person searching for it knows in advance.
SEPARATORS = "[・･゠＝]?"

#: The gojuon, five kana to a consonant, in the order a i u e o.  A dash is a
#: sound with no kana of its own: nobody types "yi", and there is nothing for
#: it to mean if they did.
GOJUON = {
    "": "あいうえお", "k": "かきくけこ", "s": "さしすせそ",
    "t": "たちつてと", "n": "なにぬねの", "h": "はひふへほ",
    "m": "まみむめも", "y": "や-ゆ-よ", "r": "らりるれろ",
    "w": "わ---を", "g": "がぎぐげご", "z": "ざじずぜぞ",
    "d": "だぢづでど", "b": "ばびぶべぼ", "p": "ぱぴぷぺぽ",
    "x": "ぁぃぅぇぉ", "l": "ぁぃぅぇぉ",
}

#: Consonants that have no y-row of their own, and the two rows that are not
#: consonants at all.  Everything else takes "kya" for き and a small ゃ.
NO_YOUON = ("", "y", "w", "x", "l")

#: The spellings the gojuon does not generate.  Two kinds are mixed here and
#: both have to be: the syllables English spells irregularly -- shi, chi, tsu,
#: fu, ji -- and the second spelling of each of those, since a keyboard
#: trained on "si" and "tu" is as common as one trained on "shi" and "tsu".
EXTRA = {
    "shi": "し", "si": "し", "sha": "しゃ", "shu": "しゅ", "she": "しぇ",
    "sho": "しょ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "chi": "ち", "ti": "ち", "cha": "ちゃ", "chu": "ちゅ", "che": "ちぇ",
    "cho": "ちょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "tsu": "つ", "tu": "つ", "tsa": "つぁ", "tsi": "つぃ", "tse": "つぇ",
    "tso": "つぉ", "tha": "てゃ", "thi": "てぃ", "thu": "てゅ",
    "the": "てぇ", "tho": "てょ",
    "fu": "ふ", "hu": "ふ", "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ",
    "fo": "ふぉ", "fya": "ふゃ", "fyu": "ふゅ", "fyo": "ふょ",
    "ji": "じ", "zi": "じ", "ja": "じゃ", "ju": "じゅ", "je": "じぇ",
    "jo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "di": "ぢ", "du": "づ", "dha": "でゃ", "dhi": "でぃ", "dhu": "でゅ",
    "dhe": "でぇ", "dho": "でょ",
    "ca": "か", "cu": "く", "co": "こ",
    "va": "ゔぁ", "vi": "ゔぃ", "vu": "ゔ", "ve": "ゔぇ", "vo": "ゔぉ",
    "wi": "うぃ", "we": "うぇ", "wha": "うぁ", "who": "うぉ",
    "n": "ん", "nn": "ん", "n'": "ん", "xn": "ん",
    "xtu": "っ", "xtsu": "っ", "ltu": "っ", "ltsu": "っ",
    "xya": "ゃ", "xyu": "ゅ", "xyo": "ょ", "xwa": "ゎ",
    "lya": "ゃ", "lyu": "ゅ", "lyo": "ょ", "lwa": "ゎ",
    "-": "ー",
}

#: Consonants that double into っ.  Not n, which doubles into ん and has a
#: rule of its own, and not the small-kana prefixes x and l, where a double
#: letter is a typing mistake rather than a sound.
#:
#: The same list says which half-typed consonants might still be the front of
#: a っ rather than of a kana; see :func:`expansions`.
SOKUON = "bcdfghjkmprstvwyz"

#: What an n can be followed by and still be the start of な rather than ん.
#: A lone n at the end of what has been typed is not in the branch at all,
#: since ``rest[1:2]`` is then the empty string and the empty string is in
#: every string: it goes to the table, which reads it as ん, which is what
#: somebody who has typed ``hon`` and stopped is looking for.
AFTER_N = "aiueoy"

#: The longest romaji spelling of one kana, which is how far ahead the
#: conversion has to look.
LONGEST = 4

#: How many words a pattern will spell out.  Above this the dictionary is
#: still read in full, but what goes into the regexp is the characters those
#: words begin with rather than the words themselves; see :func:`initials`.
#:
#: Fifteen hundred because that is more than any two-kana reading has under
#: it -- こう, the largest, has 1381 -- so a query of two kana or more is
#: always written out in full.  Spelling out that many costs about twenty
#: milliseconds, which is inside the gap between two keystrokes.
SPELLED_OUT = 1500

#: How many words one lookup will read before giving up on the idea, which is
#: a backstop against a dictionary far larger than the one that ships here
#: rather than a limit anything reaches: the worst query there is, ``k``, has
#: sixteen thousand words under it and takes seventeen milliseconds.
SCAN = 40000

#: The dictionary, which lives beside this file so that a checkout is enough.
DICTIONARY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "migemo.dict")


def _table() -> dict:
    """Romaji to kana: the gojuon spelled out, then the exceptions on top."""
    table = {}
    for consonant, kana in GOJUON.items():
        for vowel, character in zip("aiueo", kana):
            if character != "-":
                table[consonant + vowel] = character
    for consonant, kana in GOJUON.items():  # きゃ, しゅ, ちょ and the rest.
        if consonant in NO_YOUON or kana[1] == "-":
            continue
        for vowel, small in zip("auo", "ゃゅょ"):
            table[consonant + "y" + vowel] = kana[1] + small
    table.update(EXTRA)
    return table


TABLE = _table()


def katakana(kana: str) -> str:
    """The same sounds in katakana, so that one search finds both."""
    return "".join(chr(ord(c) + SHIFT) if FIRST <= c <= LAST else c
                   for c in kana)


def spaced(kana: str) -> str:
    """Katakana as a regexp with room for the marks that split a foreign name.

    ヴーヴ・クリコ and ヴーヴクリコ are the same word, and which of the two a
    particular file uses is not something the person searching it should have
    to know -- so the katakana half of every pattern allows an interpunct
    between any two characters.  It is also why the dictionary does not carry
    the ・ spellings of names it can already spell without one: with this they
    would be the same word written down twice.
    """
    return SEPARATORS.join(re.escape(character) for character in kana)


def to_hiragana(romaji: str) -> tuple:
    """``kensaku`` as けんさく, with whatever is not a kana yet kept apart.

    The tail is the point of the second half of the pair: ``kensak`` comes
    back as けんさ and ``k``, and a search that threw the k away would be a
    search that stopped narrowing between one syllable and the next.
    """
    kana: list = []
    index = 0
    while index < len(romaji):
        rest = romaji[index:]
        if len(rest) > 1 and rest[0] == rest[1] and rest[0] in SOKUON:
            kana.append("っ")  # kitte, kissa: the doubled letter is the つ.
            index += 1
            continue
        if rest[0] == "n" and rest[1:2] not in AFTER_N:
            # ん, as in kanji and konbanwa.  The second n of a pair belongs to
            # it -- konn is こん -- unless a vowel is coming, where the second
            # n has started な instead and konna is こんな rather than こんあ.
            kana.append("ん")
            after = rest[2:3]
            both = rest[1] == "'" or (rest[1] == "n"
                                      and (not after or after not in AFTER_N))
            index += 2 if both else 1
            continue
        for size in range(min(LONGEST, len(rest)), 0, -1):
            if rest[:size] in TABLE:
                kana.append(TABLE[rest[:size]])
                index += size
                break
        else:
            # Not a kana, but it may be the beginning of one, and that is the
            # tail.  Anything else is a letter with no sound -- a digit, a
            # space -- and goes through untouched.
            if any(spelling.startswith(rest) for spelling in TABLE):
                return "".join(kana), rest
            kana.append(rest[0])
            index += 1
    return "".join(kana), ""


def expansions(tail: str) -> list:
    """The kana a half-typed ``k`` could still turn out to be.

    A single consonant is two answers, not one.  It can be the front of か or
    き or one of the others -- and it can be the front of っか, because the way
    っ is typed is by doubling the consonant after it.  The t of ``set`` has
    not decided yet whether it is せた or せっ, so both go in, which is what
    makes ``set`` find 設定 rather than making you type ``sett`` first.
    """
    if not tail:
        return []
    found = {kana for spelling, kana in TABLE.items()
             if spelling.startswith(tail)}
    if len(tail) == 1 and tail in SOKUON:
        found.add("っ")
    return sorted(found)


def prefixes(romaji: str) -> list:
    """The readings this romaji could be the beginning of.

    One reading if it ends on a kana, and one for each way the last letters
    could still be finished if it does not.
    """
    kana, tail = to_hiragana(romaji)
    if not tail:
        return [kana] if kana else []
    return [kana + rest for rest in expansions(tail)]


class Dictionary:
    """The words a reading can begin, found by seeking rather than loading.

    ``migemo.dict`` is two megabytes and is sorted by reading, so a binary
    search over byte offsets answers a query in about twenty seeks and no
    parsing at all.  No reading in it is longer than five beats, which is as
    far as an incremental search is ever going to be typed.  Reading it into a
    dictionary in memory would be a shorter piece of code -- one call to
    :mod:`bisect` instead of the twenty lines below -- and would cost every
    session that never searches in Japanese forty milliseconds and five
    megabytes it never gets back.  Which of those is the right way round is a
    matter of taste; this one leaves the file on the disk, where it can also
    still be read by a person.

    Bytes are compared rather than characters, which works because UTF-8 was
    built so that it would: the byte order of two encoded strings is the code
    point order of the strings themselves.
    """

    def __init__(self, path: str = DICTIONARY) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        """Whether there is a dictionary to search.  Without one migemo still
        finds kana and katakana, which is worth saying out loud rather than
        looking like a search that has quietly stopped finding things."""
        return os.access(self.path, os.R_OK)

    @staticmethod
    def _line_at(handle, offset: int) -> bytes:
        """The first whole line at or after ``offset``.

        The awkward part of bisecting a file is that the middle of it is the
        middle of a line.  Reading one line off throws that half away and
        leaves a whole one, and the whole one is what the offset stands for.
        """
        handle.seek(offset)
        if offset:
            handle.readline()
        return handle.readline()

    def _seek(self, handle, key: bytes) -> None:
        """Leave the file at the first line at or after ``key``.

        The bisection is over the offsets rather than over the lines, because
        the lines cannot be counted without reading all of them.  What makes
        that work is that :meth:`_line_at` never goes backwards as the offset
        goes up, so the offsets whose line has reached ``key`` are a run that
        an ordinary binary search finds the beginning of.  Off that beginning
        it then has to step forward to the line itself, since the offset is
        still, in all likelihood, in the middle of the line before.
        """
        handle.seek(0, os.SEEK_END)
        low, high = 0, handle.tell()
        while low < high:
            middle = (low + high) // 2
            line = self._line_at(handle, middle)
            if line and line < key:
                low = middle + 1
            else:
                high = middle
        handle.seek(low)
        if low:
            handle.readline()

    def words(self, prefix: str, limit: int = SCAN) -> list:
        """The words written with a reading that starts with ``prefix``.

        The file is opened for each lookup rather than held open.  Twenty
        seeks are going to cost more than the open does, and an editor that
        keeps no file descriptor of its own is one where a dictionary that is
        replaced on the disk is simply the dictionary from then on.
        """
        if not prefix:
            return []
        key = prefix.encode("utf-8")
        found: list = []
        try:
            with open(self.path, "rb") as handle:
                self._seek(handle, key)
                while len(found) < limit:
                    line = handle.readline()
                    if not line or not line.startswith(key):
                        break
                    fields = line.decode("utf-8", "replace"
                                         ).rstrip("\n").split("\t")
                    found.extend(fields[1:])
        except OSError:  # No dictionary is a search without one, not a crash.
            return []
        return found[:limit]


#: The one dictionary, opened the first time somebody searches with it.
dictionary = Dictionary()


def _tree(words) -> dict:
    """The words as a trie, an empty key marking where one of them ends."""
    root: dict = {}
    for word in words:
        node = root
        for character in word:
            node = node.setdefault(character, {})
        node[""] = {}
    return root


def _branch(node: dict) -> str:
    """One node of the trie as a regexp matching what can follow it."""
    single: list = []
    parts: list = []
    for character, child in sorted(node.items()):
        if character == "":
            continue
        rest = _branch(child)
        if rest:
            parts.append(re.escape(character) + rest)
        else:
            single.append(character)  # A word ends here; nothing follows.
    if single:
        parts.insert(0, re.escape(single[0]) if len(single) == 1
                     else "[%s]" % "".join(re.escape(c) for c in single))
    if not parts:
        return ""
    if "" in node:
        # A word ends here and others go on: 検, 検索 and 検索結果 are
        # 検(?:索(?:結果)?)?, and the group is not optional decoration -- an
        # unwrapped 検索結果? would make only the last character optional.
        return "(?:%s)?" % "|".join(parts)
    return parts[0] if len(parts) == 1 else "(?:%s)" % "|".join(parts)


def alternation(words) -> str:
    """One regexp matching any of ``words``, with shared beginnings shared.

    Two hundred alternatives written out one after another is a regexp that
    backtracks through two hundred failures at every position in the line.
    Factored into a trie it is one pass: 検索, 検査 and 検事 become 検[索査事],
    which is what migemo's own regexp optimizer is for and what keeps its
    patterns short enough for the searches underneath to accept them.
    """
    return _branch(_tree(word for word in words if word))


def initials(words) -> str:
    """The characters those words begin with, as a character class.

    This is what a query too short to write out becomes.  ``d`` is で, だ, ど
    and the rest, and the dictionary has ten thousand words under those: a
    regexp holding all of them would be slower to build than the search is
    worth, and one holding the first two hundred of them is worse than
    useless, because the two hundred are whichever ones sort first.  That is
    exactly the way ``d`` used to find だ and walk straight past 電子.

    The characters those words begin with are a few hundred at most, and they
    say the one thing a single letter has to say -- that a word starting here
    is read で-something -- for every one of the ten thousand rather than for
    an alphabetical slice.  The match is one character long instead of the
    whole word, which is the right size for what one letter has narrowed
    things down to.
    """
    return "[%s]" % "".join(re.escape(character) for character
                            in sorted({word[0] for word in words if word}))


def pattern(query: str, words: Optional[Dictionary] = None) -> str:
    """The regexp for what somebody typing this romaji might have meant.

    Three things go in.  The romaji itself, because a file of Japanese has
    ASCII in it too and ``kensaku`` should still find ``kensaku``.  The kana
    it spells, in hiragana and in katakana, because those need no dictionary
    and are right even when the dictionary has never heard of the word.  And
    the words the dictionary writes with those readings, which is the part
    that finds 検索.

    The katakana is the one piece that is not a plain word: it comes out of
    :func:`spaced`, which lets an interpunct fall between any two characters,
    and so it is joined on at the end rather than shared with the rest.

    Every reading is read out of the dictionary in full, and it is only after
    they are all in hand that the pattern decides whether to spell them out.
    Reading part of one and stopping is the thing not to do: what gets left
    behind is not the rare words but the ones whose readings sort last, so
    ``kei`` would find 刑 and 京 and never reach 経由 -- and then ``keiy``,
    which asks for strictly less, would find it, because a narrower question
    fits inside the budget.  A search that starts finding things as you type
    more of it is a search that has lied to you about the letter before.
    """
    if not query:
        return ""
    readings = prefixes(query.lower())
    found = {query}
    found.update(readings)
    spellings: list = []
    for reading in readings:
        spellings.extend((words or dictionary).words(reading))
    parts = [alternation(sorted(found | set(spellings)))
             if len(spellings) <= SPELLED_OUT else alternation(sorted(found))]
    parts.extend(spaced(katakana(reading)) for reading in readings)
    if len(spellings) > SPELLED_OUT:
        parts.append(initials(spellings))
    return "|".join(parts)


def compile(query: str, words: Optional[Dictionary] = None):
    """``pattern`` ready to search with, or ``None`` if there is nothing to
    search for.

    Case follows the rule the rest of the editor searches by: a query typed
    in lower case ignores case, and a single capital makes it exact.  It is
    the same rule and not a coincidence -- ``kensaku`` is lower case, so the
    ASCII half of the pattern matches ``Kensaku`` as well, which is what
    somebody who typed no capitals meant.
    """
    text = pattern(query, words)
    if not text:
        return None
    try:
        return re.compile(text, re.IGNORECASE if query == query.lower() else 0)
    except re.error:  # Nothing here builds one, but a search must not die.
        return None
