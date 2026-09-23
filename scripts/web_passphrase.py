"""Generate a WEB_PASSPHRASE_HASH value for .env (web-chat plan track 2).

    uv run python scripts/web_passphrase.py

Prompts twice with `getpass` (never echoed, never taken as an argv/env
value that could end up in shell history or a process list) and prints
one line: `WEB_PASSPHRASE_HASH=scrypt$17$8$1$<salt_b64>$<hash_b64>`.
Paste that into `.env` or the deployment environment.

The cost parameters (N=2**17, r=8, p=1) and the format string are
hardcoded here to match app/web/auth.py's `_SCRYPT_N`/`_SCRYPT_R`/
`_SCRYPT_P` and `parse_passphrase_hash` exactly, and app/config.py's
`_PASSPHRASE_HASH_RE` -- all three are independent, but must agree on
one shape, since this script is the only thing that ever produces a
value the other two only ever parse.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import secrets
import sys

N = 2**17
R = 8
P = 1
MAXMEM = 256 * 1024 * 1024  # 256 MiB, headroom for N=2**17/r=8's ~128 MiB working set
SALT_BYTES = 16
DKLEN = 64


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_hash(passphrase: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=DKLEN
    )
    return f"scrypt$17${R}${P}${_b64(salt)}${_b64(digest)}"


def main() -> int:
    first = getpass.getpass("Passphrase: ")
    if not first:
        print("Passphrase must not be empty.", file=sys.stderr)
        return 1
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("Passphrases did not match.", file=sys.stderr)
        return 1
    print(f"WEB_PASSPHRASE_HASH={make_hash(first)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
