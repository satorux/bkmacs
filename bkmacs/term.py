"""Raw terminal input, named the way Emacs names it.

Two things have to be taken away from the terminal driver before an Emacs
layout is even possible.  ``C-s`` and ``C-q`` are flow control, and without
clearing ``IXON`` an incremental search freezes the screen instead of starting;
``C-\\`` sends SIGQUIT, and without clearing that it dumps core instead of
undoing.  ``curses.raw()`` is supposed to do both, but ncurses on macOS is old
enough to be worth double-checking, so the flags are cleared again by hand --
it costs four lines and removes a whole class of "why is my terminal wedged".

Keys come back as Emacs key descriptions -- ``"C-x"``, ``"M-%"``, ``"C-M-f"``,
``"RET"``, ``"あ"`` -- so that the binding tables read like a manual.  Meta is
the ESC prefix that terminals actually send, resolved by waiting a moment to
see whether anything follows.
"""

from __future__ import annotations

import curses
import locale
import os
import signal
import termios
from typing import Optional

#: How long to wait after ESC before deciding it was ESC and not Meta.  Long
#: enough for the rest of a sequence to arrive over ssh, short enough that a
#: deliberate ESC does not feel stuck.
META_TIMEOUT_MS = 60

#: How long ncurses itself waits after ESC before giving it to us.  Its own
#: default is a full second.
ESCDELAY_MS = 25

#: Special keys that stand in for the Emacs binding of the same effect, so
#: that arrows and page keys work without giving them bindings of their own.
_SPECIAL = {
    curses.KEY_UP: "C-p",
    curses.KEY_DOWN: "C-n",
    curses.KEY_LEFT: "C-b",
    curses.KEY_RIGHT: "C-f",
    curses.KEY_HOME: "C-a",
    curses.KEY_END: "C-e",
    curses.KEY_NPAGE: "C-v",
    curses.KEY_PPAGE: "M-v",
    curses.KEY_DC: "C-d",
    curses.KEY_BACKSPACE: "DEL",
    curses.KEY_ENTER: "RET",
    curses.KEY_RESIZE: "<resize>",
}

_CONTROL_NAMES = {
    0: "C-@",  # What the terminal sends for C-SPC, and what Emacs calls it.
    9: "TAB",
    10: "C-j",
    13: "RET",
    27: "ESC",
    28: "C-\\",
    29: "C-]",
    30: "C-^",
    31: "C-_",
    127: "DEL",
}


def key_name(code: int) -> str:
    """Emacs' name for a single input character."""
    if code in _CONTROL_NAMES:
        return _CONTROL_NAMES[code]
    if code < 32:
        return "C-" + chr(code + 96)
    return chr(code)


def with_meta(key: str) -> str:
    """``"x"`` becomes ``"M-x"``; ``"C-f"`` becomes ``"C-M-f"``."""
    if key.startswith("C-"):
        return "C-M-" + key[2:]
    return "M-" + key


class Terminal:
    """The screen and the keyboard, for as long as the editor is running."""

    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        self._saved = None
        try:
            self._saved = termios.tcgetattr(0)
        except termios.error:
            pass
        self._configure()

    def _configure(self) -> None:
        curses.raw()  # Pass C-c, C-z, C-\ and C-s through as characters.
        curses.noecho()
        curses.nonl()  # Keep RET as C-m, so C-j stays distinguishable.
        self.stdscr.keypad(True)
        try:
            # ncurses waits a full second after ESC to see whether a function
            # key is arriving, which would make every Meta key and every quit
            # feel broken.  Meta is disambiguated below instead, on our own
            # much shorter clock, so ncurses only needs long enough for an
            # arrow key's bytes to stay together.
            curses.set_escdelay(ESCDELAY_MS)
        except (AttributeError, curses.error):
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._clear_flow_control()

    def _clear_flow_control(self) -> None:
        try:
            flags = termios.tcgetattr(0)
        except termios.error:
            return
        flags[0] &= ~(termios.IXON | termios.IXOFF)  # C-s and C-q are ours.
        flags[3] &= ~(termios.ISIG | termios.IEXTEN)  # So is C-\ and C-z.
        try:
            termios.tcsetattr(0, termios.TCSANOW, flags)
        except termios.error:
            pass

    def restore(self) -> None:
        if self._saved is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self._saved)
            except termios.error:
                pass

    # -- input -----------------------------------------------------------

    def read_key(self, timeout_ms: Optional[int] = None) -> Optional[str]:
        """Wait for one key.  ``None`` means the wait timed out."""
        self.stdscr.timeout(-1 if timeout_ms is None else timeout_ms)
        first = self._read_raw()
        if first is None:
            return None
        if first != "ESC":
            return first
        # Meta arrives as ESC followed by the key, and a bare ESC arrives as
        # ESC followed by nothing.  Only the clock tells them apart.
        self.stdscr.timeout(META_TIMEOUT_MS)
        second = self._read_raw()
        if second is None:
            return "ESC"
        if second == "ESC":
            return "ESC"
        return with_meta(second)

    def _read_raw(self) -> Optional[str]:
        try:
            key = self.stdscr.get_wch()
        except curses.error:
            return None  # Timed out.
        except KeyboardInterrupt:
            return "C-g"  # Should not happen with ISIG off, but be safe.
        if isinstance(key, int):
            # A key with no Emacs equivalent still has to be distinguishable
            # from having read nothing at all, which is what None means here.
            return _SPECIAL.get(key, "<unknown>")
        if len(key) == 1 and (ord(key) < 32 or ord(key) == 0x7F):
            return key_name(ord(key))
        return key

    # -- suspending ------------------------------------------------------

    def suspend(self) -> None:
        """``C-z``: hand the terminal back to the shell until ``fg``.

        The screen state has to be saved before giving the terminal up and put
        back afterwards, including the flags cleared above -- coming back to a
        terminal where ``C-s`` freezes the display again would be a strange
        way to be punished for suspending.
        """
        curses.def_prog_mode()
        curses.endwin()
        self.restore()
        os.kill(os.getpid(), signal.SIGTSTP)
        # Execution resumes here on SIGCONT, when the user types fg.
        curses.reset_prog_mode()
        self._configure()
        self.stdscr.redrawwin()
        self.stdscr.refresh()


def setup_locale() -> bool:
    """Put the process in the terminal's locale; report whether it is UTF-8.

    curses cannot draw or read a multibyte character until this has happened,
    so Japanese depends on it entirely.
    """
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    encoding = locale.getpreferredencoding(False) or ""
    if encoding.lower().replace("-", "") == "utf8":
        return True
    for candidate in ("en_US.UTF-8", "C.UTF-8", "ja_JP.UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            return True
        except locale.Error:
            continue
    return False
