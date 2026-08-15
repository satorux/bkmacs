"""Files encrypted the way ``openssl enc`` writes them.

A file named ``*.ossl`` is one of these: base64 armour around AES-256-CBC,
with the key derived from a password by PBKDF2.  The format is not ours.  It
is what these two lines write and read, and the point of this module is that
a file written by the first opens here, and one saved here is read by the
second::

    openssl enc -e -pbkdf2 -iter 600000 -md sha256 -base64 -aes-256-cbc \\
        -salt -pass ... -in plain -out secret.ossl
    openssl enc -d -pbkdf2 -iter 600000 -md sha256 -base64 -aes-256-cbc \\
        -pass ... -in secret.ossl

The cipher is spelled out rather than read from the file because the file does
not say: ``openssl enc`` records the salt and nothing else, so the two sides
have to have agreed beforehand.

Encryption goes out to ``openssl`` rather than being done here, for two
reasons.  Python's standard library has PBKDF2 but no AES, so doing it in
process would mean carrying a cipher implementation; and a file already on
disk was written by a particular openssl, so the only way to be certain of
reading it back is to hand it to the same program.

The password never appears in a command line or in the environment -- ``ps``
shows both to anyone on the machine.  It goes down a pipe instead, which is
what ``-pass fd:`` is for.  Neither does the plain text ever reach the disk:
it is written to openssl's standard input and comes back on its standard
output, and it is the *encrypted* form that is written to a file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

#: What marks a file as encrypted.  There is nothing to look inside for --
#: base64 is just letters -- and Emacs' epa decides the same way, by name.
SUFFIX = ".ossl"

#: PBKDF2 rounds.  Has to match whatever wrote the file, since the file does
#: not carry it: a mismatch is indistinguishable from a wrong password.
ITERATIONS = 600000

#: The cipher, the digest, and the armour, in openssl's spelling.
CIPHER = ["-aes-256-cbc", "-md", "sha256", "-base64"]

#: Long enough for a slow machine to grind through the rounds above, short
#: enough that a wedged openssl does not hang the editor for good.
TIMEOUT = 120


class CryptError(Exception):
    """Something went wrong encrypting or decrypting -- usually the password."""


def is_encrypted(path: Optional[str]) -> bool:
    return path is not None and path.endswith(SUFFIX)


def _run(arguments: list, data: bytes, password: str):
    program = shutil.which("openssl")
    if program is None:
        raise CryptError("openssl is not installed")
    read, write = os.pipe()
    try:
        # Small enough to fit the pipe buffer, so this cannot block: openssl
        # is not running yet and there is nobody at the other end to read it.
        os.write(write, password.encode("utf-8") + b"\n")
        os.close(write)
        try:
            return subprocess.run(
                [program, "enc"] + arguments + CIPHER
                + ["-pass", "fd:%d" % read],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(read,), timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            raise CryptError("openssl: %s" % error)
    finally:
        os.close(read)


def _reason(completed) -> str:
    """The last thing openssl said, which is the part worth repeating."""
    lines = [line for line in completed.stderr.decode("utf-8", "replace"
                                                      ).splitlines()
             if line.strip()]
    return lines[-1] if lines else ""


def decrypt(path: str, password: str) -> tuple:
    """The plain text of an encrypted file, and whether it is in the old
    format.

    PBKDF2 is tried first and the older derivation -- ``openssl enc`` without
    ``-pbkdf2``, which is what it used to default to -- second, because
    nothing in the file says which one it is: the only test is which of them
    decrypts.  Saving writes PBKDF2 either way, so an old file is migrated by
    opening and saving it.

    A wrong password is usually caught by the padding, but roughly one time in
    two hundred and fifty-six the padding happens to check out and what comes
    back is noise.  Decoding strictly catches almost all of those: real text
    is UTF-8 and noise is not.
    """
    if not os.path.exists(path):
        # Otherwise a missing file comes back as a wrong password, which is a
        # discouraging thing to tell somebody typing the right one.
        raise CryptError("%s no longer exists" % os.path.basename(path))
    for legacy, derivation in ((False, ["-pbkdf2", "-iter", str(ITERATIONS)]),
                               (True, [])):
        completed = _run(["-d"] + derivation + ["-in", path], b"", password)
        if completed.returncode != 0:
            continue
        try:
            return completed.stdout.decode("utf-8"), legacy
        except UnicodeDecodeError:
            continue
    raise CryptError("Wrong password")


def encrypt(text: str, password: str, path: str) -> None:
    """Write ``text`` to ``path``, encrypted.  Nothing plain touches the disk.

    Always PBKDF2, whatever the file was before, which is what migrates one
    written back when openssl derived its keys the other way.
    """
    completed = _run(["-e", "-pbkdf2", "-iter", str(ITERATIONS), "-salt",
                      "-out", path],
                     text.encode("utf-8"), password)
    if completed.returncode != 0:
        raise CryptError(_reason(completed) or "openssl could not encrypt")
    # The saving side fsyncs what it writes before renaming it into place, and
    # this file has to be as safe as one of those: openssl exiting means the
    # bytes have left it, not that they have reached the disk.
    handle = os.open(path, os.O_WRONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
