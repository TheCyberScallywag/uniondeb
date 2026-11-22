"""What a worker or the API needs to do its job, built once from the environment.

Implements: UND-024
"""

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import yaml

from uniondeb.agents.gwyliwr import SensitiveDate
from uniondeb.cases import AuditLog, CaseService, FlagService, SarService
from uniondeb.extract import ContractRegistry, Echdynnwr
from uniondeb.store import StoreSettings, Tarddiad, open_runner
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.runner import CypherRunner
from uniondeb.worker.queue import JobQueue


class UploadTooLarge(ValueError):
    pass


class UploadStore:
    """Uploaded files, kept once each under their SHA-256."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def save(self, stream: BinaryIO, *, max_bytes: int) -> tuple[str, int, bool]:
        """Stream to disk while hashing; refuse past ``max_bytes``. Returns (sha256, size, new)."""
        h, size = hashlib.sha256(), 0
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
                    h.update(chunk)
                    out.write(chunk)
            target = self.path(h.hexdigest())
            new = not target.exists()
            if new:
                target.parent.mkdir(exist_ok=True, mode=0o700)
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            return h.hexdigest(), size, new
        finally:
            Path(tmp).unlink(missing_ok=True)


@dataclass
class Services:
    runner: CypherRunner
    cipher: FieldCipher
    uploads: UploadStore
    extract_root: Path
    pseudonym_key: bytes
    calendar: list[SensitiveDate] = field(default_factory=list)
    models: dict[str, Any] = field(default_factory=dict)  # agent name → Pydantic AI model; else the environment decides
    anchor_path: Path | None = None
    anchor_key: bytes | None = None

    def __post_init__(self) -> None:
        self.tarddiad = Tarddiad(self.runner)
        self.queue = JobQueue(self.runner)
        self.echdynnwr = Echdynnwr(
            self.runner, self.tarddiad, ContractRegistry.load(), root=self.extract_root, cipher=self.cipher
        )
        self.audit = AuditLog(self.runner)
        self.cases = CaseService(self.runner, self.audit, self.tarddiad)
        self.flags = FlagService(self.runner, self.audit, self.cases)
        self.sars = SarService(self.runner, self.audit, self.cases, self.flags)

    def model(self, agent: str) -> Any | None:
        """The model for ``agent``, or ``None`` when none is configured (the deterministic path still runs)."""
        return self.models.get(agent) or os.environ.get(f"UNIONDEB_{agent.upper()}_MODEL") or None

    @classmethod
    @asynccontextmanager
    async def from_env(cls, env: Mapping[str, str] | None = None, **store: Any) -> AsyncIterator[Services]:
        env = os.environ if env is None else env
        key = env.get("UNIONDEB_PSEUDONYM_KEY")
        if not key or len(key) < 32:
            raise RuntimeError("set UNIONDEB_PSEUDONYM_KEY to at least 32 random characters")
        data = Path(env.get("UNIONDEB_DATA_DIR", "/var/lib/uniondeb"))
        calendar = []
        if path := env.get("UNIONDEB_CALENDAR"):
            calendar = [SensitiveDate.model_validate(e) for e in yaml.safe_load(Path(path).read_text()) or []]
        anchor_key = env.get("UNIONDEB_AUDIT_ANCHOR_KEY")
        if env.get("UNIONDEB_SYNTHETIC") == "1":
            store.setdefault("synthetic", True)
        async with open_runner(StoreSettings.from_env(env, **store)) as runner:
            yield cls(
                runner=runner,
                cipher=FieldCipher.from_env(env),
                uploads=UploadStore(Path(env.get("UNIONDEB_UPLOAD_DIR", data / "uploads"))),
                extract_root=Path(env.get("UNIONDEB_EXTRACT_ROOT", "/run/uniondeb/extracts")),
                pseudonym_key=key.encode(),
                calendar=calendar,
                anchor_path=Path(env["UNIONDEB_AUDIT_ANCHOR"]) if env.get("UNIONDEB_AUDIT_ANCHOR") else None,
                anchor_key=anchor_key.encode() if anchor_key else None,
            )
