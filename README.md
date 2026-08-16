# bkmacs

An Emacs-shaped editor for the terminal, in pure Python. Standard library
only — no C extension, no build step, nothing to install.

This exists because macOS stopped shipping Emacs and it is easy to
install Python via Apple's Command Line Tools, and `curses` and
`unicodedata` come with them.

```console
$ git clone https://github.com/satorux/bkmacs
$ cd bkmacs
$ python3 -m bkmacs README.md
```

That is the whole installation. To run it from anywhere else, point at
the package directory — a directory holding a `__main__.py` is runnable,
so the doubled name is the checkout on the outside and the package on the
inside:

```sh
alias bkmacs='python3 ~/src/bkmacs/bkmacs'
```

Python 3.9 or newer; macOS gives 3.9.6, which is what this is tested on.
The one thing outside the standard library is `openssl`, and only for
[encrypted files](#encrypted-files); it is on macOS already too.

## There is no configuration file

The defaults are compiled in, and they are not Emacs' defaults — they are
one particular `~/.emacs`, the one this was written from. Knowing which
is the difference between an editor that seems to have odd ideas and one
whose ideas you can predict:

| Behavior | From |
| --- | --- |
| `C-h` deletes backwards, like DEL | `keyboard-translate` swapping them; there is no help system for `C-h` to prefix |
| `C-\` undoes, as do `C-/` and `C-_` | "for terminals that cannot send `C-/`" |
| The file itself is saved after half a second of quiet | `auto-save-visited-mode`, `auto-save-visited-interval 0.5` |
| The window follows the cursor one line at a time | `scroll-step 1` |
| The region is highlighted | `transient-mark-mode` |
| Tabs are never inserted | `indent-tabs-mode nil` |
| Trailing whitespace is marked | `show-trailing-whitespace` |
| The mode line carries line *and* column | `line-number-mode`, `column-number-mode` |
| `C-n` at the end of the buffer does not add a line | `next-line-add-newlines nil` |
| `M-g` is `goto-line` directly | `(define-key global-map "\M-g" 'goto-line)` |

If none of that is how you like to work, the place to change it is the
source: the tables at the top of `bkmacs/editor.py` are the key bindings,
and there are no defaults hiding anywhere else.

UTF-8 only. Bytes that are not valid UTF-8 are shown in octal, like
Emacs, and written back untouched.

Color, but never a background color — which is why grep's colors have
survived every terminal anyone has ever used. Painting a background is
the part that cannot be made to work: black on yellow reads on a dark
terminal and disappears on a light one, where yellow is a dark olive.

Foreground alone is not quite enough either: half of the sixteen colors
are light inks and half are dark ones, and not one of them is legible
against both backgrounds. So the terminal is asked which it has, and the
palette follows the answer. See [the colours are asked for, not
configured](#the-colours-are-asked-for-not-configured).

So a search match is bold red, a matching parenthesis is bold cyan or
bold blue depending on that answer, and the region and the mode line are
reverse video while trailing whitespace is underlined — a space has no
glyph to color, and reverse video would turn a line holding nothing but
its old indentation into a bar across the screen. The terminal's own
background is never touched.

## Keys

Everything there is. Arrow, Home/End and Page keys work too, as the Emacs
key of the same effect.

### Moving

| Key | Command | |
| --- | --- | --- |
| `C-f` `C-b` | forward-char, backward-char | |
| `C-n` `C-p` | next-line, previous-line | keeps the column it started from |
| `C-a` `C-e` | beginning/end of line | |
| `M-f` `M-b` | forward-word, backward-word | kana and kanji count as word characters |
| `C-M-n` `C-M-p` | forward-list, backward-list | over the next group, however deep it nests |
| `M-<` `M->` | beginning/end of buffer | sets the mark where you were |
| `C-v` `M-v` | scroll forward, back | two lines of context, as Emacs keeps |
| `C-l` | recenter | and repaints the screen |
| `M-g` | goto-line | `M-g` directly, not `M-g M-g` |

### Changing

| Key | Command | |
| --- | --- | --- |
| `RET` | newline | |
| `C-j` | newline-and-indent | copies the current line's indentation |
| `TAB` | indent | two spaces, never a tab character |
| `C-o` | open-line | |
| `C-t` | transpose-chars | at the end of a line, the two characters before it |
| `C-d` | delete-char | |
| `DEL` `C-h` | backward-delete-char-untabify | both, and a tab deletes as the spaces it was showing |
| `M-d` `M-DEL` | kill-word, backward-kill-word | |
| `C-k` | kill-line | consecutive kills join into one entry |
| `M-q` | fill-paragraph | rewraps to 74 columns, kinsoku and all |
| `C-x SPC` | fixup-whitespace | |
| `C-q` | quoted-insert | the next key as a character: `C-q TAB` is the only way to a real tab |
| `C-x C-u` `C-x C-l` | upcase-region, downcase-region | |
| `M-u` `M-l` `M-c` | upcase, downcase, capitalize the word | from point to the end of it, and point lands there |
| `C-/` `C-_` `C-\` | undo | Emacs' undo ring — see below |

### The region

| Key | Command | |
| --- | --- | --- |
| `C-SPC` | set-mark-command | the region is highlighted while it is active |
| `C-w` `M-w` | kill-region, kill-ring-save | |
| `C-y` `M-y` | yank, yank-pop | the ring holds 60 entries |
| `C-x C-x` | exchange-point-and-mark | |
| `C-x h` | mark-whole-buffer | point at the top, the mark at the bottom |
| `C-x r k` `C-x r y` | kill-rectangle, yank-rectangle | the region as a shape; cut by column, so a wide character is never split |
| `C-x r t` | string-rectangle | a rectangle with no width puts the same text down a column of lines |

### Searching

| Key | Command | |
| --- | --- | --- |
| `C-s` `C-r` | isearch forward, backward | `DEL` steps back through the search itself; wraps on a second try |
| `C-s C-s` `C-r C-r` | isearch-repeat | with nothing typed, the search you did last |
| `M-m` | migemo, inside a search | on to begin with; `M-m` turns it off, `DEL` takes that back too |
| `M-%` | query-replace | `y` `n` `!` `.` `q`; one undo takes back the whole session |
| `M-x query-replace-regexp` | query-replace-regexp | the same questions, by regexp; `\1` is the first group |
| `M-x grep` | grep | Python regexps, over a tree of files |
| `M-x occur` | occur | the same, over this buffer |
| ``C-x ` `` | next-error | walks the hits of whichever ran last |

A pattern typed in lower case ignores case; one capital letter makes it
exact. With the region active, either query-replace works inside it and
nowhere else.

A search shows what it found by highlighting it, and leaves point at the
end of the match going forwards and at the front of it going backwards,
which is where Emacs leaves it and where the next `C-d` or `C-k` will
bite. `M-p` and `M-n` walk back through earlier searches; `C-s` or `C-r`
with nothing typed yet recalls the last one, which then behaves as though
you had typed it.

### Files and buffers

| Key | Command | |
| --- | --- | --- |
| `C-x C-f` | find-file | `TAB` completes |
| `C-x C-v` | find-alternate-file | in place of this buffer; `RET` on the name already there reads the file again |
| `C-x C-s` | save-buffer | rarely needed; see the autosave below — except for [encrypted files](#encrypted-files), where it is the only way |
| `C-x C-w` | write-file | writes it somewhere else and goes on editing it there |
| `C-x b` `C-x k` | switch-to-buffer, kill-buffer | |
| `C-x C-b` | list-buffers | |
| `M-x revert-buffer` | revert-buffer | the same file again, keeping point |
| `C-x C-c` | exit | |

### Windows

| Key | Command | |
| --- | --- | --- |
| `C-x 2` | split-window-below | horizontally only, shared evenly |
| `C-x o` | other-window | |
| `C-x 1` `C-x 0` | delete-other-windows, delete-window | |

Two windows on one file keep their own positions in it.

### Everything else

| Key | Command | |
| --- | --- | --- |
| `C-u` | universal-argument | `C-u 3 C-n`, `C-u 70 -`; on its own it is 4 and `C-u C-u` is 16 |
| `C-g`, `ESC ESC` | keyboard-quit | |
| `C-z` | suspend | `fg` brings it back |
| `M-x` | execute-extended-command | `TAB` completes |
| `ESC` | Meta prefix | no timeout — see below |

### C-u

`C-u` counts the command after it: `C-u 3 C-n` goes three lines down and
`C-u 70 -` draws a rule across the page. On its own it is four, and `C-u
C-u` is sixteen, as in Emacs.

The count is how many times to run the command, which is not always what
Emacs makes of it. There, `C-u 2 C-k` kills two whole lines; here `C-k`
takes a line and then takes its newline, so `C-u 4 C-k` is what does.
Repeating the keystroke is the rule, and being the rule everywhere is
worth more than a table of exceptions. Commands that ask a question —
`C-x C-f`, `M-x`, a search — ignore the count, since asking four times is
not four of anything. There are no negative arguments: `C-u -` in Emacs
means the same command backwards, and backwards is a key of its own here.

### ESC

`ESC` is Meta, as a prefix key with no clock on it: `ESC <` is `M-<`
however long you take over it, in the minibuffer and in a search as much
as in the buffer. That is the only way to type Meta on a terminal that
will not send it — Option unmapped on macOS, or a browser tab keeping Alt
for itself. `ESC ESC` quits, which matters because `C-g` is not always
available either: Chrome takes it for the Gemini side panel
(`chrome://settings/ai/gemini` turns that off).

### The minibuffer

`TAB` completes. When the answer is ambiguous the candidates go into a
`*Completions*` window below, which is taken down again when the prompt
is answered — the echo area is one line, and a directory of forty files
does not fit on it.

`M-p` and `M-n` — or the up and down arrows — walk back through what was
typed at that prompt before, with a separate ring for filenames, buffers,
commands, grep patterns, globs and searches. The rings are kept in
`~/.bkmacs-history`, written on every addition and readable only by you.
Emacs does the same thing with `savehist`, which writes elisp into
`~/.emacs.d/history`.

### Undo

Emacs' undo, not the usual undo/redo pair: undoing is itself a change, so
it gets recorded like any other. Walk backwards with repeated `C-/`; type
anything else and `C-/` again, and what you are now undoing is the undo.
That is how Emacs redoes without ever having a redo key.

### Filling

`M-q` rewraps the paragraph around point to 74 columns — the
`fill-column` from the `~/.emacs` this came from, and a width that leaves
room on an eighty-column terminal for the `\` that marks a continued line.

Japanese has no spaces to break at, so it breaks between characters, and
the half of kinsoku shori that readers actually notice is enforced: a
full stop or a closing bracket never begins a line, an opening bracket
never ends one. Where that would overrun, the break moves back and takes
the offending character down with it. Breaking mid-word is not avoided,
because Japanese typesetting does not avoid it either.

The first line of a paragraph keeps whatever it starts with. The lines
under it take the prefix of the second line, which is what makes a
hanging indent stay hung; where there is no second line to copy, a
comment marker carries down and a list bullet becomes the blank that
lines its text up underneath.

### The mode line

Emacs', including the parts of it that look like line noise:

```
-UUU:---  F1  README.md      Top   (1,0)      (Fundamental) ----------------
```

`-` is the input method, none being active. `UUU` is the coding system in
three places — keyboard, terminal, file — where `U` is UTF-8, which here
it always is. The `:` after them is the end-of-line convention, and it
becomes `\` for a file that arrived with CRLF; that is the one character
of the group that ever carries news. Then the remote-file indicator,
always `-` since nothing here opens files over a network, and then `--`
unmodified, `**` modified, `%%` read-only.

`F1` is the frame identification: on a terminal Emacs numbers its frames,
because a terminal can hold several. This has one, so it always says `F1`.

Then the buffer name, how much of it is above the window (`Top`, `Bot`,
`All`, or a percentage), `(line,column)` with the column counted from
zero, and the major mode — `Fundamental` unless the buffer is a `*grep*`,
an `*Occur*`, a `*Completions*` or a Markdown file.

### grep and occur

`M-x grep` asks for a Python regular expression and a filename glob, then
walks the tree under the current file. What matched is shown in bold in
the results, not just the line it was on. It does not shell out — one
regexp dialect instead of whichever grep the platform came with, and
nothing to quote wrong.

The glob is matched against each file's path relative to that tree, with
git's rule: a pattern containing no slash applies at any depth, so `*.py`
and `**/*.py` both find every Python file, while `tests/*.py` pins the
search to one directory. `*` and `?` stop at a slash; `**` crosses them.

`M-x occur` is the same search narrowed to the current buffer, listing
the lines that match with their line numbers. It works in a buffer that
was never a file, and jumps back into that buffer rather than opening
anything.

Either way the results appear in a window below and the cursor stays
where it was. ``C-x ` `` walks the hits from there, first one first,
opening each in the window you are already in. `RET` on a line of the
results does the same from the other side. The results stay put
throughout.

## Japanese, without stopping to convert (Migemo)

`C-s` searches while you type. A kana-kanji conversion produces nothing
until it has been finished and confirmed, so searching for 検索 on a
Japanese keyboard means composing the word somewhere else and pasting it
into the search — by which point it is not an incremental search any
more.

[Migemo](https://0xcc.net/migemo/)'s answer is to search for what the
romaji *might* become instead of waiting to be told. Every keystroke of
`C-s` builds a regexp out of three things: the letters themselves, the
kana they spell, and the words the dictionary writes with those kana.

```
kensaku → けんさく → (?:kensaku|けんさく|ケンサク|検[査索]|研削|健[作策]|…)
```

Half-typed romaji is the interesting part, and the reason this is more
than a lookup table. `kensak` is けんさ and a `k` that is not a kana yet,
and the `k` is not noise: it says the next sound is か, き, く, け, こ, one
of きゃ, きゅ, きょ, or the っ that a doubled consonant is typed as, and
nothing else. So the search narrows on every letter rather than only on
every syllable, which is the difference between a search and an input
method — `kens` has already ruled out 検討 while it is still finding 検索
and 検査. `nez` finds 鼠 for the same reason, and `set` finds 設定 without
waiting for the second `t`.

It is on from the start. A search that had to be switched into the mode
it is nearly always wanted in would be a search that got switched every
single time, so the prompt stays the plain `I-search:` and `M-m` turns
migemo *off* — which the prompt does say, as `I-search [literal]:`, since
a search that has stopped finding Japanese should say so rather than be
worked out. Because the toggle is pushed onto the same stack the pattern
is, `DEL` takes back an `M-m` exactly as it takes back a letter, and
wherever a search is left is where the next one starts.

Nothing else changes when it is on. The romaji is in the pattern too, so
`kensaku` still finds `kensaku`, and in a file with no Japanese in it
there is nothing else for the rest of the pattern to match.

One letter is a special case, and the reason is arithmetic. `d` is で, だ,
ど and the rest, and the dictionary has ten thousand words under those —
too many to write into a regexp, and writing the first few hundred of them
is worse than useless, because the few hundred are whichever readings sort
first. So a pattern that would spell out more than thirteen hundred words
spells out the characters they *begin* with instead: all ten thousand of
them, as a character class. `d` matches the 電 of 電子 and the ダ of データ,
one character at a time, which is as much as one letter has narrowed
anything down to. Ruby/Migemo met the same wall and answered it by
pre-generating the patterns for `a`, `ka`, `sa` and the rest and caching
them; this is the other way out of it, and it needs nothing kept.

Thirteen hundred is more than any two-kana reading has under it, so from
the second kana on the words are always written out in full. That matters
for more than tidiness: a search must never *find* something because you
typed more of it, and a budget that spent itself alphabetically did
exactly that — `kei` walked past 経由 and `keiy`, which asks for strictly
less, found it.

The katakana half of the pattern allows an interpunct between any two
characters, so `vu-vukuriko` finds ヴーヴ・クリコ and ヴーヴクリコ both,
and `janpo-ru` finds ジャン＝ポール. Whether a particular file splits a
foreign name up is not something the person searching for it knows in
advance. It is also why the dictionary carries no ・ spelling of a name
it can already spell without one — with this, those were the same word
written down twice.

Ruby/Migemo threads `\s*` between the characters of its patterns so that
a match survives a line break — which is what its Emacs needs once `M-q`
has wrapped a sentence through the middle of a word. Not here: searching
works a line at a time, so a pattern that could span lines would only be
one that could never be found.

A capital letter means here what it means in every other search: `kensaku`
ignores case and `Kensaku` does not. It changes only the ASCII half of the
pattern, kana and kanji having no case to fold.

### The dictionary

`bkmacs/migemo.dict` is 2.0MB: 77,000 readings and the words written with
them, one reading to a line, sorted.

```
しんこう	進行	神功	信仰	振興	新光	新港	神幸	侵攻	新興	神鋼	親交
```

It is searched where it lies — about twenty seeks into the file on the
disk — rather than read in. A dictionary in memory would be a shorter
piece of code, one call to `bisect` instead of the file arithmetic, and
would cost every session that never searches in Japanese forty
milliseconds and five megabytes it never gets back. Sorted order is what
makes seeking possible, so a re-sorted or re-encoded copy of the file
stops working.

Nor is it compressed, which would take it to 0.66MB. Git's own packing
already gets it to 0.66MB either way, so a clone downloads the same
number of bytes; what a `.gz` would buy is 1.4MB of working tree, and
what it would cost is that two versions of it no longer delta against
each other — a repository holding two dictionaries is 1.0MB packed as
text and 1.6MB packed as gzip — along with the ability to grep the thing
or read a diff of it.

No reading in it is longer than five beats. Mozc has
にほんがくじゅつかいぎきょうりょくがくじゅつけんきゅうだんたい in it,
thirty kana and about fifty keystrokes, and an incremental search is
never going to reach the end of one of those; five beats is seven or
eight keystrokes, which is a long search already. Beats and not
characters, because きょ is two characters, one beat and three
keystrokes, and counting characters would have cut じょうきょう, which is
six characters, four beats and the word 状況.

The cut is not the same as making those words unfindable. A long word is
still found by whatever it begins with — アイルランド共和軍 by the
katakana of アイルランド, 検索エンジン by 検索 and 東京大学 by 東京, each
under its own shorter reading. What it costs is that a search stops
narrowing once it is past five beats, and that 706 words, most of them
archaic, lose their only way in.

It is not only 479KB smaller, though. The cut is made before the ranking,
so every reading it throws out hands its place to a shorter one — at the
same `--keep` the five-beat dictionary reaches 95.8% of the words in the
sample below where the uncut one reaches 95.1%. Smaller and better at
once, which is not the usual shape of that trade.

Nothing in it is written entirely in ASCII either. IRA was in Mozc under
あいあーるえい, and migemo is for the things a keyboard cannot type
directly: anybody looking for IRA types IRA. Words with only some ASCII
in them stay — `aishi-ka-do` is the only way to reach ICカード.

It was generated from Mozc's open source dictionary, which is IPAdic plus
a few thousand words Google added, ranked by Mozc's own cost and cut off
where the words stop being words anybody types:

```console
$ mozc=https://raw.githubusercontent.com/google/mozc/master/src/data
$ for i in 0 1 2 3 4 5 6 7 8 9; do
>     curl -O $mozc/dictionary_oss/dictionary0$i.txt
> done
$ python3 tools/make-migemo-dict.py dictionary0*.txt > bkmacs/migemo.dict
```

`--keep` sets how many reading-and-word pairs survive, and it is what the
size of the file really is. Where to stop was measured rather than
guessed, against a dozen Japanese Wikipedia articles — 44,000 kanji words
between them — by asking how much of each of those words the dictionary
can jump to:

| `--keep` | size | can reach | by two characters or more |
| --- | --- | --- | --- |
| 60,000 | 1.1MB | 92.6% | 58.7% |
| 120,000 | 2.0MB | 95.8% | 63.9% |
| 200,000 | 3.2MB | 96.7% | 66.2% |

The second column is the honest one. Landing on a single 東, which occurs
everywhere, is not finding anything; a match of two characters or more is
a search. Half the size gives up four points of it and twice the size
buys two and a half, so 120,000 is where this stops.

`--max-reading` is the five beats above.

Which pairs are the cheapest is not quite Mozc's question, and the last
two things the generator does are about the difference. Mozc costs a word
*written that way*, and for a word usually written in kana that is not the
same as whether anybody searches for it: ねずみ costs 4909 and 鼠 costs
5785, so ranking on spellings alone drops 鼠 — the word migemo's own page
opens by finding — while keeping thousands nobody has ever looked for. So
the first spelling of a reading is charged what the reading costs, and the
rest are charged what they are worth.

The other correction is for two-kanji words, which are charged a thousand
less than Mozc asks. The words somebody wants in their own files are not
the words a web corpus is full of: 外字 is rank 152,159 there, and it is
the first thing anyone would search for in a program that reads EPWING
dictionaries. Two kanji is the shape almost all of those have — 索引,
全角, 母音, 端末, 引数 — and the cheapest shape to store, six bytes and no
okurigana. The discount costs about six per cent of the file and buys back
a whole class of word the ranking is systematically wrong about: it is
worth six points of that second column above.

Migemo has always been built on the SKK dictionary, which is a better one
for this — but SKK's is GPL, and a permissively licensed
dictionary is one that can simply be checked in next to MIT code without
the repository having to explain two licences at once. IPAdic asks for a
notice and nothing else; it is in [NOTICE](NOTICE), and it covers that one
file.

It is also in the head of the dictionary itself, as a hundred and thirty
lines of `;;`. What IPAdic asks is that any copy carry the notice, and two
megabytes of words is exactly the kind of file that gets taken out of a
checkout on its own and turns up somewhere with nothing to say for itself.
It costs three thousandths of the file. `NOTICE` is the original of the
two — the one a person or a licence scanner finds — and the generator
copies it into the dictionary, so the two cannot come to disagree.

## Markdown

A file called `*.md` is coloured. It is the only file type this editor
knows anything about, and the reason it knows about one is that a README
is the longest thing anybody here writes by hand — long enough that
finding a heading, or seeing where a code fence ends, is worth more than
it would be in a language a compiler is going to check anyway.

Headings are magenta. Both halves of a link take one colour with the URL
underlined, so that you can see where the words stop and the address
starts. `**strong**` is bold and `*emphasis*` is italic, or underlined on
a terminal with no italics to give. Bullets, the `>` of a quote and `---`
are markers: only the marker is coloured, and the text after it is left
as ordinary text. What colour the rest of it is depends on the terminal,
which is asked — see below.

Nothing inside a fence means anything, which is what a fence is for — a
`console` block full of globs and asterisks comes out as the shell wrote
it. Anything that would need more than the line in front of it is left
out: no reference definitions resolved, no list nesting, and `---` is
always a thematic break rather than sometimes the underline of a
heading, since telling those apart needs the line before. A code span or
an emphasis that a filled paragraph has broken across two lines is not
joined back up either — it renders as it should and is simply left
uncoloured here, which is a fair price for a code fence being the only
thing in the file that needs more than one line to understand.

It is not a major mode, and there is nothing to turn on or off. `M-q`
still fills a paragraph the way it does everywhere else, and every key
does what it does in any other buffer.

### The colours are asked for, not configured

Only foreground colours are ever set, never a background. Black on
yellow reads on a dark terminal and disappears on a light one, where
yellow is a dark olive, and that is true of any background you might
choose.

That much is the usual advice, and on its own it is not enough. Half of
the sixteen colours are light inks and half are dark ones, and not one
of them is legible against both. Measured against Terminal.app's own
palette, green is 6.5:1 against its black and 3.3:1 against its white;
blue is 12.8:1 on that white and 1.6:1 on that black. Nothing in the
sixteen clears 4.5:1 both ways, so there is no palette that is simply
correct.

So the terminal is asked. `ESC ] 11 ; ?` is the question, and what it
asks is *what colour are you painted*. It goes out before curses starts,
so that the answer cannot arrive as though somebody had typed it.
`ESC [ c` follows immediately: every terminal since the VT100 answers
that one, so its answer arriving is what says the first answer is not
coming rather than merely late. Without it, the only way to give up
would be a timeout long enough to be a stutter at startup.

If anything is already waiting on the keyboard when that moment comes —
somebody who started typing while Python was still starting — nothing is
asked at all. Reading past a keystroke to find out a colour would throw
the keystroke away, because a terminal cannot be handed input back.

A dark terminal gets green code, cyan links and yellow markers. A light
one gets blue code and magenta links, and the markers give up their
colour rather than take yellow at 3:1 — where a bullet sits is already
most of what says it is one. A terminal that will not answer is taken to
be dark, which is what a terminal has usually been.

This is not a setting, and that is the point of doing it this way. What
colour the terminal is painted is a fact about the terminal; a setting
would be that fact written down a second time, somewhere it can go out
of date.

## Autosave, and the one case it stops

The buffer is written to its own file — not to a `#file#` copy — half a
second after you stop typing, through a temporary file in the same
directory and `os.replace`, so the file is never half-written.
Permissions are preserved.

If the file changes underneath you (`git checkout`, another window), the
autosave stops rather than overwriting that change, `[disk changed]`
appears in the mode line, and `C-x C-s` asks before going ahead.

## Encrypted files

A file named `*.ossl` is one encrypted with `openssl enc` — AES-256-CBC
under a PBKDF2 key, in base64 armour. Exactly this, in other words, and
the point is that a file written by that line opens here, and one saved
here opens with it:

```console
$ openssl enc -e -pbkdf2 -iter 600000 -md sha256 -base64 -aes-256-cbc \
      -salt -in notes -out notes.ossl
```

Opening one asks for its password in the minibuffer, echoed as asterisks,
and asks again as long as you keep getting it wrong. `C-g` gives up and
leaves the buffer empty and read-only, with `(Encrypted)` in the mode
line; visiting the file again, or `M-x revert-buffer`, gets you another
go. The password is then remembered for as long as the buffer is, so
saving does not ask for it again. It is never written anywhere —
particularly not to the minibuffer history file.

Encrypted buffers are **not autosaved**: six hundred thousand rounds of
PBKDF2 half a second after you stop typing is a stutter, not an autosave.
So `**` in the mode line means what it means everywhere else and nowhere
else here — unsaved work — and `C-x C-s` is how you write it. `C-x C-c`
and `C-x k` ask (`Save file ~/notes.ossl?`) rather than doing it quietly.
No backup copy is made, dated or otherwise.

Saving writes PBKDF2 whatever the file was before, which converts one
from back when `openssl enc` derived its keys the other way — the old
format is still read, so a copy predating that change opens too. The
plain text never touches the disk: it goes to `openssl` down a pipe, and
only what comes back gets written. The password goes down a pipe of its
own rather than on a command line or in the environment, either of which
`ps` would show to anyone on the machine. A new `*.ossl` file asks for a
password twice, and is created `0600`.

The one thing this needs that nothing else here does is `openssl` on the
path. It is on macOS by default.

## Not here

Major modes, elisp, extension in Python, keyboard macros, dabbrev,
registers. All deliberate, and syntax highlighting was on this list until
Markdown came off it — a colour for a `*.md` file costs one module and no
configuration, where a mode for every language costs both. `C-x r` holds
the rectangle commands and nothing else. Windows split horizontally only
(`C-x 2`), and share the height evenly rather than being resizable.

## Tests

```console
$ python3 -m unittest discover -s tests
```

`test_bkmacs.py` covers the text and layout arithmetic. `test_session.py`
starts the real editor under a pseudo-terminal, types at it, and reads
the screen back through a small terminal emulator — which is the only way
to find out whether `C-s` reaches the program or gets eaten by the
terminal driver on the way.

## Forks, not pull requests

Fork it. That is what the license is for, and an editor already built to
one person's taste is a better place to start from than a general-purpose
one with a settings screen to argue with.

Pull requests are turned off, though, and that is not unfriendliness. The
whole point of this editor is that every decision in it was made to suit
one person: `TAB` inserts two spaces, `C-h` deletes backwards, there is
no configuration file to make either of those negotiable. A change that
makes it fit somebody else better usually makes it worse at the only
thing it is for. Your fork can be exactly as opinionated, and about you.

## License

MIT. See [LICENSE](LICENSE).

The one file that is not ours is `bkmacs/migemo.dict`, which came out of
Mozc's dictionary and carries IPAdic's notice with it. See
[NOTICE](NOTICE).

## Disclaimer

This is a personal project. The views, code, and opinions expressed here
are my own and do not represent those of my current or past employers.
