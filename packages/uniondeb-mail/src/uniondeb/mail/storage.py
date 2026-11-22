"""Content-addressed object storage for raw journal mail and its attachments.

The raw ``.eml`` SHA-256 is the provenance anchor for each message, and attachments are stored
once by SHA-256: a board pack forwarded to a personal account links to the original by hash even
when the subject line changes. Writes are therefore idempotent (the same bytes are the same
object) and atomic (a reader never sees half a message), and files are private to the service
account (0600).

``ObjectStore`` is deliberately small so an S3/MinIO adapter can replace ``LocalObjectStore``.

Implements: UND-026
"""

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, Protocol

type Kind = Literal["raw", "attachments"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUFFIX: dict[str, str] = {"raw": ".eml", "attachments": ""}


class ObjectStore(Protocol):
    def put(self, sha256: str, data: bytes, *, kind: Kind = "raw") -> bool:
        """Store ``data`` under its hash. True when it was new, False when already held."""
        ...

    def get(self, sha256: str, *, kind: Kind = "raw") -> bytes: ...
    def exists(self, sha256: str, *, kind: Kind = "raw") -> bool: ...


class LocalObjectStore:
    """``<root>/raw/ab/abcdef….eml`` and ``<root>/attachments/ab/abcdef…``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, sha256: str, *, kind: Kind = "raw") -> Path:
        if not _SHA256.match(sha256) or kind not in _SUFFIX:
            raise ValueError("objects are addressed by a lowercase SHA-256 under raw/ or attachments/")
        return self.root / kind / sha256[:2] / f"{sha256}{_SUFFIX[kind]}"

    def put(self, sha256: str, data: bytes, *, kind: Kind = "raw") -> bool:
        path = self.path(sha256, kind=kind)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("content does not match its SHA-256 address")
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # mkstemp creates the file 0600
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return True

    def get(self, sha256: str, *, kind: Kind = "raw") -> bytes:
        return self.path(sha256, kind=kind).read_bytes()

    def exists(self, sha256: str, *, kind: Kind = "raw") -> bool:
        return self.path(sha256, kind=kind).is_file()
