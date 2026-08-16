#!/usr/bin/env python3
"""Build ``bkmacs/migemo.dict`` out of Mozc's open source dictionary.

The dictionary migemo needs is a small one: for every reading, the words that
are written with it.  Nothing else in a Japanese dictionary matters here --
not the part of speech, not the connection costs, not the conjugation tables
-- because a search does not have to be right about grammar, only about what
the characters on the screen might be.

Mozc's ``dictionary_oss`` is that, plus a cost per entry::

    けんさく	1851	1851	3315	検索

The cost is Mozc's estimate of how unlikely the word is, and it is the reason
this file exists rather than a bare ``sort | uniq``.  There are 1.3 million
entries, most of them place names in prefectures nobody is grepping for, so
the entries are ranked by cost and the common end of the list is kept.  That
is the whole difference between a two megabyte dictionary and a twenty one
megabyte one, and the two megabyte one finds 検索, 実装 and 高林 just the
same.

Mozc's dictionary is used rather than the SKK dictionary that migemo was
written against because its licence is a permissive one -- IPAdic's, which
asks for a notice and nothing more -- and so it can be checked in next to MIT
code without the repository having to explain two licences at once.

That notice lives in ``NOTICE`` and is copied from there into the head of the
dictionary, because what IPAdic asks is that any copy carry it and a two
megabyte file of words is exactly the sort of thing that gets taken out of a
checkout on its own.  A hundred and thirty lines of ``;;`` is three
thousandths of the file, and it means the file can always say where it came
from.  ``NOTICE`` stays the original: it is the one a person or a licence
scanner finds, and the copy is written by this program so that the two cannot
come to disagree.

Five kinds of entry are dropped as noise:

* a word spelled exactly like its reading (けんさく for けんさく), which no
  regexp needs to be told about, since the reading is in the pattern already;
* a word that is the reading in katakana (プログラム for ぷろぐらむ), for the
  same reason -- migemo writes the katakana itself.  The interpunct that
  splits a foreign name does not save an entry from this: ヴーヴ・クリコ is
  ヴーヴクリコ as far as a search is concerned, because every katakana pattern
  migemo builds allows an interpunct between any two characters;
* a reading that is not entirely kana, which is Mozc's way of holding entries
  that are typed some other way;
* a reading longer than ``MAX_READING`` beats, which is a reading nobody is
  going to finish typing into an incremental search;
* a word written entirely in ASCII (IRA for あいあーるえい), because migemo
  is for the things a keyboard cannot type directly and those are not among
  them: anybody looking for IRA types IRA.  A word with only some ASCII in it
  stays -- ICカード has no other way of being found.

To run it::

    mozc=https://raw.githubusercontent.com/google/mozc/master/src/data
    for i in 0 1 2 3 4 5 6 7 8 9; do
        curl -O $mozc/dictionary_oss/dictionary0$i.txt
    done
    python3 tools/make-migemo-dict.py dictionary0*.txt > bkmacs/migemo.dict

The output is sorted by reading and is meant to stay that way: the editor
binary-searches the file on the disk rather than reading it in, which is only
possible while it is in order.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

#: How many reading-and-word pairs to keep, cheapest -- which is to say most
#: ordinary -- first.  This is the size of the dictionary: a hundred thousand
#: is 2.3 megabytes, two hundred thousand is 4.5, forty thousand is 0.9.
#:
#: A hundred and twenty thousand because of where the returns fall off.
#: Measured against a dozen Japanese Wikipedia articles -- 44,000 kanji words
#: -- a dictionary this size can jump to 95.8 per cent of them, and to 63.9
#: per cent of them by two characters or more, which is the number that says
#: whether a search is a search: landing on a single 東 that occurs everywhere
#: is not finding anything.  Half the size gives up four points of the second
#: number, and twice the size buys three.
KEEP = 120000

#: How many words one reading may hold.  A reading with sixty spellings is a
#: reading whose sixtieth spelling will never be the one that was meant, and
#: it is the reading of every one-kana query.
PER_READING = 16

#: How long a reading may be, in beats.  にほんがくじゅつかいぎきょうりょく
#: がくじゅつけんきゅうだんたい is in Mozc, and it is thirty kana and about
#: fifty keystrokes: nobody searching incrementally will ever reach the end of
#: it, and the words under a reading like that stopped mattering as soon as
#: its beginning had been typed.  Five beats is seven or eight keystrokes,
#: which is a long search already.
#:
#: Cutting there is not the same as making those words unfindable.  A long
#: word is still found by whatever it begins with: アイルランド共和軍 by the
#: katakana of アイルランド, 検索エンジン by 検索 and 東京大学 by 東京, each
#: under its own shorter reading.  What is lost is that a search stops
#: narrowing once it is past the cut.
#:
#: And what is gained is not only the bytes.  The cut is applied before the
#: ranking, so every reading it drops hands its place to a shorter one -- the
#: dictionary gets smaller and better at the same time, which is why this is
#: five rather than the eight it started at.
MAX_READING = 5

#: What a two-character kanji word is charged, against what Mozc thinks of it.
#:
#: Mozc costs a word by how often it turns up, and the words that matter to
#: somebody searching their own files are not the ones that turn up in a web
#: corpus: 外字 is rank 152,159 there and is the first thing you would look
#: for in a program that reads EPWING dictionaries.  Two kanji is the shape
#: almost every one of those has -- 索引, 全角, 母音, 端末, 引数 -- and it is
#: also the cheapest shape to store, six bytes and no okurigana.  So it is
#: charged a thousand less than it costs, which is worth about six per cent of
#: the file and buys back a class of word the ranking is systematically wrong
#: about.
TWO_KANJI = 1000

#: The kana that are not a beat of their own: they finish the one before them,
#: and they are why the length of a reading is not the length of its string.
#: きょ is two characters and one beat and three keystrokes, so counting the
#: characters would cut とうきょうだいがく, which is 東京大学 and a word people
#: certainly do type.
SMALL = "ぁぃぅぇぉゃゅょゎっ"

#: Where hiragana lives, and the distance to the katakana of the same sound.
FIRST, LAST, SHIFT = "ぁ", "ゖ", 0x60

#: Two kanji and nothing else, which is the shape :data:`TWO_KANJI` is about.
TWO_KANJI_WORD = re.compile(r"[一-龥々]{2}\Z")

#: The marks that split a foreign name into its parts.  They carry no sound,
#: so a word is the reading in katakana whether they are there or not; the
#: editor's patterns allow them anywhere, and the same list is in migemo.py.
SEPARATORS = "・･゠＝"

#: The notices the dictionary has to carry, kept in ``NOTICE`` beside the
#: code and copied into the head of the dictionary from there.  One original
#: and one copy rather than two originals: the two said the same thing today
#: and there is no reason to believe they would still be saying it in a year.
NOTICE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NOTICE")

#: What the head of the dictionary says before the notices, which is the part
#: that is about this file rather than about who owns the words in it.
PREAMBLE = """\
The words migemo searches for, and their readings, one reading to a line.
Sorted by reading: bkmacs binary-searches this file where it lies, so a
re-sorted or re-encoded copy of it stops working.

What follows is NOTICE from https://github.com/satorux/bkmacs, carried here
because IPAdic asks that any copy of these words carry it, and this is the
file that gets copied.  None of it covers bkmacs itself, which is MIT.
"""


def header(path: str) -> str:
    """The preamble and the notices, as the comment the dictionary opens with.

    A semicolon sorts before every kana there is, so the whole of this lands
    ahead of the first entry and the binary search never has to know it is
    here.
    """
    with open(path, encoding="utf-8") as source:
        text = PREAMBLE + "\n" + source.read()
    return "".join((";; " + line).rstrip() + "\n"
                   for line in text.rstrip("\n").split("\n"))


def katakana(reading: str) -> str:
    return "".join(chr(ord(c) + SHIFT) if FIRST <= c <= LAST else c
                   for c in reading)


def bare(word: str) -> str:
    """The word without the marks that only separate its parts."""
    return "".join(c for c in word if c not in SEPARATORS)


def beats(reading: str) -> int:
    """How many kana long a reading is, counting きょ as the one it sounds."""
    return sum(1 for character in reading if character not in SMALL)


def is_kana(reading: str) -> bool:
    return bool(reading) and all(FIRST <= c <= LAST or c == "ー"
                                 for c in reading)


def read(paths: list, longest: int = MAX_READING) -> tuple:
    """What Mozc knows, as the cost of every (reading, word) it has and the
    cost of every reading.

    The same pair turns up several times over -- once per part of speech it
    can be -- and the cheapest of those is the one that says how common the
    word is.

    The cost of the reading is taken before the filters below throw anything
    away, and that is the point of it: the entry that says ねずみ is an
    everyday word is ネズミ, which is exactly one of the entries being
    dropped for being the reading in katakana.
    """
    best: dict = {}
    common: dict = {}
    for path in paths:
        with open(path, encoding="utf-8") as source:
            for line in source:
                fields = line.rstrip("\n").split("\t")
                if len(fields) != 5:
                    continue
                reading, _, _, cost, word = fields
                if not is_kana(reading) or not word:
                    continue
                if beats(reading) > longest:
                    continue
                price = int(cost)
                if price < common.get(reading, price + 1):
                    common[reading] = price
                if bare(word) in (reading, katakana(reading)):
                    continue
                if word.isascii():
                    continue  # Whoever wants IRA can type IRA.
                if any(c.isspace() for c in word):
                    continue  # A word with a space in it cannot be a field.
                key = (reading, word)
                if price < best.get(key, price + 1):
                    best[key] = price
    return best, common


def rank(best: dict, common: dict) -> list:
    """The pairs, cheapest first, the first spelling of a reading charged
    what the reading costs rather than what the spelling does.

    Mozc's cost is the cost of writing a word *that way*, and for a word
    whose usual spelling is kana that is not the same question as whether
    anybody searches for it.  ねずみ costs 4909 and 鼠 costs 5785, so a
    dictionary ranked on spellings alone drops 鼠 while keeping thousands of
    words nobody has ever looked for -- and 鼠 is the word the migemo page
    opens by finding.  Charging the reading's price to its first spelling
    fixes that without letting a common reading drag in all sixteen of the
    ways it can be written: those still cost what they are worth.

    A two-kanji word is then discounted again, for the reason in
    :data:`TWO_KANJI`.
    """
    head: dict = {}
    for (reading, word), cost in best.items():
        if cost < head.get(reading, (cost + 1,))[0]:
            head[reading] = (cost, word)
    return sorted((price(word, common.get(reading, cost)
                         if head[reading][1] == word else cost),
                   reading, word)
                  for (reading, word), cost in best.items())


def price(word: str, cost: int) -> int:
    return cost - TWO_KANJI if TWO_KANJI_WORD.match(word) else cost


def group(ranked: list, keep: int) -> dict:
    """The cheapest ``keep`` pairs, gathered under their readings."""
    readings: dict = {}
    for cost, reading, word in ranked[:keep]:
        readings.setdefault(reading, []).append((cost, word))
    return readings


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dictionaries", nargs="+",
                        help="Mozc's dictionary0*.txt")
    parser.add_argument("--keep", type=int, default=KEEP,
                        help="how many reading-and-word pairs to keep")
    parser.add_argument("--per-reading", type=int, default=PER_READING,
                        help="how many words one reading may hold")
    parser.add_argument("--max-reading", type=int, default=MAX_READING,
                        help="how long a reading may be, in beats")
    options = parser.parse_args(argv[1:])

    best, common = read(options.dictionaries, options.max_reading)
    readings = group(rank(best, common), options.keep)
    print("%d pairs read, %d kept under %d readings"
          % (len(best), sum(len(w) for w in readings.values()), len(readings)),
          file=sys.stderr)

    out = sys.stdout
    out.write(header(NOTICE))
    for reading in sorted(readings):
        words = [word for _, word
                 in sorted(readings[reading])][:options.per_reading]
        out.write("%s\t%s\n" % (reading, "\t".join(words)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
