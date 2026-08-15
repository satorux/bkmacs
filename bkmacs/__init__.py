"""bkmacs: an Emacs-shaped editor in the terminal, in pure Python.

Nothing to install and nothing to build -- it runs on the Python that macOS
ships, which is why it exists at all.  There is no configuration file: the
defaults are compiled in, and they are one particular set of Emacs habits
rather than Emacs' own.  See the README for which, and why.

    python3 -m bkmacs [file ...]
"""

from __future__ import annotations

import curses
import sys

__version__ = "0.1.0"


def main(argv: "list[str] | None" = None) -> int:
    from .editor import Editor
    from .term import setup_locale

    paths = list(sys.argv[1:] if argv is None else argv)
    warning = ("" if setup_locale()
               else "Terminal is not UTF-8; Japanese will not display")

    def start(stdscr) -> None:
        editor = Editor(stdscr, paths, warning)
        try:
            editor.run()
        finally:
            editor.term.restore()

    curses.wrapper(start)
    return 0
