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

Color, but never a background color. A terminal theme is exactly a
promise that its foreground colors are legible against its own
background, so touching only the foreground is legible everywhere by
construction — which is why grep's colors have survived every terminal
anyone has ever used. Painting a background breaks the promise: black on
yellow reads on a dark terminal and disappears on a light one, where
yellow is a dark olive.

So a search match is bold red and a matching parenthesis is bold cyan,
while the region and the mode line are reverse video and trailing
whitespace is underlined — a space has no glyph to color, and reverse
video would turn a line holding nothing but its old indentation into a
bar across the screen. The terminal's own background is never touched.

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
| `C-d` | delete-char | |
| `DEL` `C-h` | backward-delete-char-untabify | both, and a tab deletes as the spaces it was showing |
| `M-d` `M-DEL` | kill-word, backward-kill-word | |
| `C-k` | kill-line | consecutive kills join into one entry |
| `M-q` | fill-paragraph | rewraps to 74 columns, kinsoku and all |
| `C-x SPC` | fixup-whitespace | |
| `C-x C-u` `C-x C-l` | upcase-region, downcase-region | |
| `C-/` `C-_` `C-\` | undo | Emacs' undo ring — see below |

### The region

| Key | Command | |
| --- | --- | --- |
| `C-SPC` | set-mark-command | the region is highlighted while it is active |
| `C-w` `M-w` | kill-region, kill-ring-save | |
| `C-y` `M-y` | yank, yank-pop | the ring holds 60 entries |
| `C-x C-x` | exchange-point-and-mark | |

### Searching

| Key | Command | |
| --- | --- | --- |
| `C-s` `C-r` | isearch forward, backward | `DEL` steps back through the search itself; wraps on a second try |
| `M-%` | query-replace | `y` `n` `!` `.` `q`; one undo takes back the whole session |
| `M-x grep` | grep | Python regexps, over a tree of files |
| `M-x occur` | occur | the same, over this buffer |
| `C-x \`` | next-error | walks the hits of whichever ran last |

A pattern typed in lower case ignores case; one capital letter makes it
exact. With the region active, query-replace works inside it and nowhere
else.

### Files and buffers

| Key | Command | |
| --- | --- | --- |
| `C-x C-f` | find-file | `TAB` completes |
| `C-x C-s` | save-buffer | rarely needed; see the autosave below |
| `C-x b` `C-x k` | switch-to-buffer, kill-buffer | |
| `C-x C-b` | list-buffers | |
| `M-x revert-buffer` | revert-buffer | |
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
| `C-g`, `ESC ESC` | keyboard-quit | |
| `C-z` | suspend | `fg` brings it back |
| `M-x` | execute-extended-command | `TAB` completes |
| `ESC` | Meta prefix | no timeout — see below |

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
an `*Occur*` or a `*Completions*`.

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
where it was. `C-x \`` walks the hits from there, first one first,
opening each in the window you are already in. `RET` on a line of the
results does the same from the other side. The results stay put
throughout.

## Autosave, and the one case it stops

The buffer is written to its own file — not to a `#file#` copy — half a
second after you stop typing, through a temporary file in the same
directory and `os.replace`, so the file is never half-written.
Permissions are preserved.

If the file changes underneath you (`git checkout`, another window), the
autosave stops rather than overwriting that change, `[disk changed]`
appears in the mode line, and `C-x C-s` asks before going ahead.

## Not here

Syntax highlighting, major modes, elisp, extension in Python, keyboard
macros, dabbrev, `M-q`. All deliberate. Windows split horizontally only
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

## Disclaimer

This is a personal project. The views, code, and opinions expressed here
are my own and do not represent those of my current or past employers.
