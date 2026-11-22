"""Build, register, open and expire agent extracts (plan §3.3, ADR 0002).

``Echdynnwr.build`` runs a contract's queries against the system of record under the contract's
role, pseudonymises the listed columns with a key that exists only for that extract, and writes
the rows into a fresh file that only this service ever writes. The agent then opens it read-only.
Each build is an ``Extract`` entity whose activity used the contract version and generated the
file hash, so anything an agent concludes can be traced to exactly what it was shown.

Relate has no encryption at rest, so ``root`` must be an encrypted, per-run volume (tmpfs in dev
and CI). Files are created ``0600`` and deleted by ``reap`` once their TTL passes.

The fallback backend writes the same tables as Parquet. Agents read tables through
``ExtractReader.rows``, which is the same on both; only free Cypher (Holwr) needs Relate.

Implements: UND-032
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.extract.contracts import Contract, ContractRegistry, Table
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner, RelateRunner

ECHDYNNWR = AgentRef(name="echdynnwr", version="0.1.0")
BATCH = 2000
type Backend = Literal["relate", "parquet"]


class ExtractError(RuntimeError):
    pass


class ExtractTooLarge(ExtractError):
    pass


class ExtractHandle(BaseModel):
    id: str
    contract_id: str
    contract_hash: str
    path: str
    sha256: str  # of the file the agent opens
    content_sha256: str  # of the rows before pseudonymisation: equal for equal contract, params and data
    rows: int
    backend: Backend
    created_at: datetime
    expires_at: datetime


def pseudonym(key: bytes, value: Any) -> str:
    return "P-" + hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:16]


class ExtractReader:
    """What an agent holds: tables from one extract, read-only."""

    def __init__(self, handle: ExtractHandle, contract: Contract) -> None:
        self.handle, self.contract = handle, contract
        self._tables = {t.name: t for t in contract.tables}
        self._relate: RelateRunner | None = None
        if datetime.now(UTC) >= handle.expires_at:
            raise ExtractError(f"extract {handle.id} expired at {handle.expires_at.isoformat()}")
        if not Path(handle.path).exists():
            raise ExtractError(f"extract {handle.id} is gone (expired or never built)")

    @property
    def graph(self) -> RelateRunner:
        """The read-only graph, for Cypher. Relate extracts only."""
        if self.handle.backend != "relate":
            raise ExtractError("this extract is tabular (fallback backend); it has no Cypher interface")
        if self._relate is None:
            self._relate = RelateRunner.open(self.handle.path, read_only=True)
        return self._relate

    async def rows(self, table: str) -> list[dict[str, Any]]:
        t = self._tables[table]
        if self.handle.backend == "parquet":
            import pyarrow.parquet as pq

            return pq.read_table(Path(self.handle.path) / f"{table}.parquet").to_pylist()
        if t.node:
            found = await self.graph.run(f"MATCH (n:`{t.node}` {{_table: $t}}) RETURN n {{.*}} AS row", {"t": table})
        else:
            found = await self.graph.run(
                f"MATCH ()-[r:`{t.rel}` {{_table: $t}}]->() RETURN r {{.*}} AS row", {"t": table}
            )
        return [{c: r["row"].get(c) for c in t.columns} for r in found]

    async def close(self) -> None:
        if self._relate is not None:
            await self._relate.close()
            self._relate = None

    async def __aenter__(self) -> ExtractReader:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class Echdynnwr:
    def __init__(
        self,
        source: CypherRunner | Callable[[str], CypherRunner],
        tarddiad: Tarddiad,
        registry: ContractRegistry,
        *,
        root: Path,
        cipher: FieldCipher | None = None,
        backend: Backend = "relate",
    ) -> None:
        self._source = source if callable(source) else (lambda _role: source)
        self.tarddiad, self.registry, self.root, self.cipher, self.backend = (
            tarddiad,
            registry,
            Path(root),
            cipher,
            backend,
        )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ── build ───────────────────────────────────────────────────────────────────────
    async def build(self, contract_id: str, params: Mapping[str, Any] | None = None) -> ExtractHandle:
        contract = self.registry.get(contract_id)
        given = dict(params or {})
        if unknown := sorted(set(given) - set(contract.params)):
            raise ExtractError(f"{contract_id} takes {list(contract.params)}, not {unknown}")
        if missing := sorted(set(contract.params) - set(given) - set(contract.defaults)):
            raise ExtractError(f"{contract_id} needs parameters {missing}")
        bound = {**contract.defaults, **given}
        source = self._source(contract.aura_role)

        total = 0
        for table in contract.tables:  # fail fast, before anything is read or written
            [row] = await source.run(f"CALL {{ {table.cypher} }} RETURN count(*) AS n", bound)
            total += row["n"]
        if total > contract.max_rows:
            raise ExtractTooLarge(f"{contract_id} would return {total} rows; the cap is {contract.max_rows}")

        extract_id, key = str(uuid.uuid7()), secrets.token_bytes(32)
        now = datetime.now(UTC)
        path = self.root / (f"{extract_id}.rl" if self.backend == "relate" else extract_id)
        reverse: dict[str, str] = {}
        await self.tarddiad.runner.run(
            "MERGE (c:ExtractContract {hash: $hash}) ON CREATE SET c.id = $id, c.agent = $agent, c.role = $role",
            {"hash": contract.hash, "id": contract.id, "agent": contract.agent, "role": contract.aura_role},
        )
        async with self.tarddiad.activity(
            ActivityKind.EXTRACT,
            agent=ECHDYNNWR,
            trigger=f"{contract.agent}:{contract.id}",
            config_versions={"contract": contract.hash[:16], "backend": self.backend},
        ) as act:
            act.used(EntityRef(label="ExtractContract", key=contract.hash))
            try:
                tables, content = {}, hashlib.sha256(contract.hash.encode())
                for table in contract.tables:
                    rows = await source.run(table.cypher, bound)
                    content.update(_canonical(table, rows))
                    tables[table.name] = self._prepare(contract, table, rows, key, reverse)
                if self.backend == "relate":
                    await self._write_relate(contract, path, tables)
                else:
                    self._write_parquet(contract, path, tables)
            except BaseException:
                _remove(path)
                raise
            handle = ExtractHandle(
                id=extract_id,
                contract_id=contract.id,
                contract_hash=contract.hash,
                path=str(path),
                sha256=_digest(path),
                content_sha256=content.hexdigest(),
                rows=sum(len(r) for r in tables.values()),
                backend=self.backend,
                created_at=now,
                expires_at=now + timedelta(minutes=contract.ttl_minutes),
            )
            props = handle.model_dump(mode="json", exclude={"path"}) | {
                "agent": contract.agent,
                "params_json": json.dumps(bound, default=str, sort_keys=True),
                "file": path.name,
                "expires_ts": handle.expires_at.timestamp(),
            }
            if reverse and self.cipher is not None:
                props["pseudonyms_enc"] = self.cipher.encrypt_json(reverse)
            await self.tarddiad.runner.run("CREATE (x:Extract) SET x = $props", {"props": props})
            act.generated(EntityRef(label="Extract", key=extract_id))
        return handle

    def _prepare(
        self, contract: Contract, table: Table, rows: list[dict[str, Any]], key: bytes, reverse: dict[str, str]
    ) -> list[dict[str, Any]]:
        declared, masked = set(table.columns), contract.pseudonymised(table)
        out = []
        for row in rows:
            if extra := sorted(set(row) - declared):
                raise ExtractError(f"{contract.id}/{table.name} returned undeclared columns {extra}")
            clean = {c: row.get(c) for c in table.columns}
            for c in masked:
                if clean[c] is not None:
                    p = pseudonym(key, clean[c])
                    reverse[p] = str(clean[c])
                    clean[c] = p
            out.append(clean)
        return out

    async def _write_relate(self, contract: Contract, path: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
        old = os.umask(0o177)  # the file is 0600 from its first byte
        try:
            writer = RelateRunner.open(str(path))
        finally:
            os.umask(old)
        try:
            keys = {t.node: t.key for t in contract.tables if t.node and t.key}
            for label, k in keys.items():
                await writer.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.`{k}` IS UNIQUE")
            for table in sorted(contract.tables, key=lambda t: t.node is None):  # nodes before relationships
                rows = [{**r, "_table": table.name} for r in tables[table.name]]
                for i in range(0, len(rows), BATCH):
                    part = rows[i : i + BATCH]
                    if table.node and table.key:
                        q = f"UNWIND $rows AS row MERGE (n:`{table.node}` {{`{table.key}`: row.`{table.key}`}}) SET n += row"
                    elif table.node:
                        q = f"UNWIND $rows AS row CREATE (n:`{table.node}`) SET n += row"
                    else:
                        s, e = table.start, table.end
                        q = (
                            f"UNWIND $rows AS row MATCH (a:`{s.node}` {{`{keys[s.node]}`: row.`{s.column}`}}) "
                            f"MATCH (b:`{e.node}` {{`{keys[e.node]}`: row.`{e.column}`}}) "
                            f"CREATE (a)-[r:`{table.rel}`]->(b) SET r += row"
                        )
                    await writer.run(q, {"rows": part})
        finally:
            await writer.close()
        os.chmod(path, 0o600)

    def _write_parquet(self, contract: Contract, path: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path.mkdir(mode=0o700)
        for table in contract.tables:
            rows = tables[table.name]
            data = {c: [r[c] for r in rows] for c in table.columns}
            target = path / f"{table.name}.parquet"
            pq.write_table(pa.table(data), target)
            os.chmod(target, 0o600)

    # ── use ─────────────────────────────────────────────────────────────────────────
    def open(self, handle: ExtractHandle) -> ExtractReader:
        return ExtractReader(handle, self.registry.get(handle.contract_id))

    async def resolve(self, extract_id: str, pseudonyms: list[str]) -> dict[str, str]:
        """Real values behind pseudonyms from one extract. Trusted callers only (the write-back API)."""
        rows = await self.tarddiad.runner.run(
            "MATCH (x:Extract {id: $id}) RETURN x.pseudonyms_enc AS enc", {"id": extract_id}
        )
        if not rows:
            raise ExtractError(f"no extract {extract_id}")
        if not rows[0]["enc"] or self.cipher is None:
            return {}
        table = self.cipher.decrypt_json(rows[0]["enc"])
        return {p: table[p] for p in pseudonyms if p in table}

    # ── expire ──────────────────────────────────────────────────────────────────────
    async def reap(self, now: datetime | None = None) -> list[str]:
        """Delete expired extract files and record each deletion. Returns the extract ids."""
        now = now or datetime.now(UTC)
        due = await self.tarddiad.runner.run(
            "MATCH (x:Extract) WHERE x.deleted_at IS NULL AND x.expires_ts <= $now RETURN x.id AS id, x.file AS file",
            {"now": now.timestamp()},
        )
        reaped = []
        for row in due:
            async with self.tarddiad.activity(ActivityKind.EXPIRE, agent=ECHDYNNWR, trigger="ttl") as act:
                act.used(EntityRef(label="Extract", key=row["id"]))
                _remove(self.root / row["file"])
                await self.tarddiad.runner.run(
                    "MATCH (x:Extract {id: $id}) SET x.deleted_at = $now REMOVE x.pseudonyms_enc",
                    {"id": row["id"], "now": now.isoformat()},
                )
            reaped.append(row["id"])
        known = {r["file"] for r in await self.tarddiad.runner.run("MATCH (x:Extract) RETURN x.file AS file")}
        for stray in self.root.iterdir():  # a crash mid-build can leave a file no Extract node names
            if stray.name not in known and _age(stray, now) > timedelta(hours=24):
                _remove(stray)
        return reaped


def _canonical(table: Table, rows: list[dict[str, Any]]) -> bytes:
    lines = sorted(json.dumps([r.get(c) for c in table.columns], default=str, ensure_ascii=False) for r in rows)
    return (table.name + "\n" + "\n".join(lines) + "\n").encode()


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _age(path: Path, now: datetime) -> timedelta:
    return now - datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
        if f.is_file():
            h.update(f.name.encode())
            with f.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
    return h.hexdigest()
