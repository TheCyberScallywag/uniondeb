"""API test fixtures: an app over an embedded store, and tokens for each role.

Verifies: UND-020
"""

import httpx
import pytest

from uniondeb.api import AuthSettings, create_app, mint
from uniondeb.store.crypto import FieldCipher
from uniondeb.worker import Services, UploadStore

SECRET = "s" * 40
ROLES = {
    "uploader": ["uploader"],
    "ann": ["reviewer"],
    "bob": ["reviewer"],
    "lead": ["reviewer", "compliance_lead"],
    "compliance": ["compliance"],
    "officer": ["nominated_officer"],
    "inv": ["investigator"],
    "inv2": ["investigator"],
    "auditor": ["auditor"],
    "agent": ["service"],
    "nobody": [],
}


def token(user: str, **kw) -> dict[str, str]:
    return {"Authorization": "Bearer " + mint(SECRET, user, ROLES[user], **kw)}


def make(runner, tmp_path, **models) -> tuple[Services, httpx.AsyncClient]:
    services = Services(
        runner=runner,
        cipher=FieldCipher(FieldCipher.generate_key()),
        uploads=UploadStore(tmp_path / "uploads"),
        extract_root=tmp_path / "extracts",
        pseudonym_key=b"k" * 32,
        models=models,
    )
    app = create_app(services, AuthSettings(secret=SECRET), max_upload_bytes=5_000_000)
    return services, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://uniondeb.test")


@pytest.fixture
async def api(empty_store, tmp_path):
    services, client = make(empty_store, tmp_path)
    async with client:
        yield services, client


@pytest.fixture
async def tagged_api(tagged_store, tmp_path):
    services, client = make(tagged_store, tmp_path)
    async with client:
        yield services, client
