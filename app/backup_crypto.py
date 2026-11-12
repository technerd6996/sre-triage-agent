"""Encrypts backup files with a recovery key. AES-256-GCM in 1 MB chunks, so a large dump never has to fit in memory.

Each chunk is authenticated together with the file header, its position and whether it is the last one, so a changed,
reordered or cut-off file is refused rather than restored."""
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"TRIAGE1-BACKUP1\n"
_SALT, _PREFIX, _CHUNK = 16, 7, 1 << 20
_HEADER = len(MAGIC) + _SALT + _PREFIX
BAD = "The recovery key is wrong, or the backup file is damaged or incomplete."


def _derive(secret: str, salt: bytes) -> AESGCM:
    if not secret:
        raise ValueError("A recovery key is needed.")
    return AESGCM(Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(secret.encode()))


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    return prefix + struct.pack(">I", index) + (b"\x01" if last else b"\x00")


def is_encrypted(path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def encrypt_file(src, dst, secret: str) -> None:
    salt, prefix = os.urandom(_SALT), os.urandom(_PREFIX)
    header, aes = MAGIC + salt + prefix, _derive(secret, salt)
    with open(src, "rb") as fin, open(dst, "wb") as out:
        out.write(header)
        index, current = 0, fin.read(_CHUNK)
        while True:
            following = fin.read(_CHUNK)
            last = not following
            box = aes.encrypt(_nonce(prefix, index, last), current, header)
            out.write(struct.pack(">I", len(box)) + box)
            if last:
                return
            index, current = index + 1, following


def decrypt_file(src, dst, secret: str) -> None:
    """Writes into dst, which may already exist with locked-down permissions. Nothing is left behind on failure."""
    try:
        with open(src, "rb") as fin, open(dst, "wb") as out:
            header = fin.read(_HEADER)
            if len(header) != _HEADER or not header.startswith(MAGIC):
                raise ValueError(BAD)
            salt, prefix = header[len(MAGIC):len(MAGIC) + _SALT], header[len(MAGIC) + _SALT:]
            aes, index = _derive(secret, salt), 0
            size = fin.read(4)
            while True:
                if len(size) != 4:
                    raise ValueError(BAD)
                box = fin.read(struct.unpack(">I", size)[0])
                following = fin.read(4)
                out.write(aes.decrypt(_nonce(prefix, index, not following), box, header))
                if not following:
                    return
                index, size = index + 1, following
    except (InvalidTag, ValueError, struct.error) as e:
        Path(dst).unlink(missing_ok=True)
        raise ValueError(BAD) from e
