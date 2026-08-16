"""bkmacs: an Emacs-shaped editor in the terminal, in pure Python.

Nothing to build and nothing to depend on: Python comes with Apple's
Command Line Tools on macOS, and curses and unicodedata come with it.

There is no configuration file: the defaults are compiled in, and they are
one particular set of Emacs habits rather than Emacs' own.  See the README
for which, and why.

    python3 -m bkmacs [file ...]
"""

from __future__ import annotations

import curses
import sys

__version__ = "0.1.0"


def main(argv: "list[str] | None" = None) -> int:
    from .editor import Editor
    from .term import background_is_light, setup_locale

    paths = list(sys.argv[1:] if argv is None else argv)
    warning = ("" if setup_locale()
               else "Terminal is not UTF-8; Japanese will not display")
    # Asked here rather than from inside the editor, because the terminal
    # answers on the keyboard and curses has not started listening yet.  A
    # terminal that will not say is taken to be dark, which is what one is.
    light = background_is_light() is True

    def start(stdscr) -> None:
        editor = Editor(stdscr, paths, warning, light)
        try:
            editor.run()
        finally:
            editor.term.restore()

    curses.wrapper(start)
    return 0
