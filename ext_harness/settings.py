"""Harness settings, read from ``HARNESS_*`` environment variables."""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import jwt

EXTENSIONS = frozenset({".jsonl", ".csv", ".parquet"})
DATASETS = frozenset({"calls", "messages", "handoff", "email"})


@dataclass(frozen=True)
class Settings:
    uniondeb_url: str = "http://127.0.0.1:8000"
    token: str | None = field(
        default=None, repr=False
    )  # a ready-made uploader token (production: from the OIDC provider)
    auth_secret: str | None = field(default=None, repr=False)  # development: mint tokens with Uniondeb's shared secret
    subject: str = "ext-harness"
    api_key: str | None = field(default=None, repr=False)  # when set, callers of the harness must present it
    spool: Path = Path("data/harness-spool")
    max_upload_bytes: int = 512 * 1024 * 1024
    batch_records: int = 500  # a stream is cut into files of this many records …
    batch_seconds: float = 5.0  # … or after this long with nothing new
    workers: int = 2
    retries: int = 5
    retry_seconds: float = 2.0
    poll_seconds: float = 2.0
    poll_timeout: float = 900.0
    keep_delivered: bool = False  # keep spooled files after Uniondeb has ingested them

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str, default=None, cast=str):
            raw = env.get(f"HARNESS_{name}")
            return default if raw in (None, "") else cast(raw)

        return cls(
            uniondeb_url=get("UNIONDEB_URL", cls.uniondeb_url).rstrip("/"),
            token=get("UNIONDEB_TOKEN"),
            auth_secret=get("AUTH_SECRET") or env.get("UNIONDEB_AUTH_SECRET") or None,
            subject=get("SUBJECT", cls.subject),
            api_key=get("API_KEY"),
            spool=get("SPOOL", cls.spool, Path),
            max_upload_bytes=get("MAX_UPLOAD_BYTES", cls.max_upload_bytes, int),
            batch_records=get("BATCH_RECORDS", cls.batch_records, int),
            batch_seconds=get("BATCH_SECONDS", cls.batch_seconds, float),
            workers=get("WORKERS", cls.workers, int),
            retries=get("RETRIES", cls.retries, int),
            retry_seconds=get("RETRY_SECONDS", cls.retry_seconds, float),
            poll_seconds=get("POLL_SECONDS", cls.poll_seconds, float),
            poll_timeout=get("POLL_TIMEOUT", cls.poll_timeout, float),
            keep_delivered=get("KEEP_DELIVERED", "0") == "1",
        )

    def bearer(self) -> str:
        """The token to present to Uniondeb: the configured one, else a short-lived development token."""
        if self.token:
            return self.token
        if not self.auth_secret:
            raise RuntimeError("set HARNESS_UNIONDEB_TOKEN, or UNIONDEB_AUTH_SECRET to mint development tokens")
        claims = {"sub": self.subject, "roles": ["uploader"], "exp": int(time.time()) + 300}
        return jwt.encode(claims, self.auth_secret, algorithm="HS256")
