"""A small async Cypher runner interface with two implementations.

``Neo4jRunner`` wraps the official async driver (Aura or Enterprise). ``RelateRunner`` wraps an
embedded Relate connection, so the same migrations and repositories run locally without a server.
Every statement runs in its own auto-commit transaction, because Neo4j does not allow schema and
data changes in one transaction.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from uniondeb.store.settings import StoreSettings

Row = dict[str, Any]


@runtime_checkable
class CypherRunner(Protocol):
    async def run(self, query: str, params: Mapping[str, Any] | None = None) -> list[Row]: ...

    async def close(self) -> None: ...


class Neo4jRunner:
    def __init__(self, driver: Any, database: str) -> None:
        self.driver = driver
        self.database = database

    @classmethod
    def from_settings(cls, settings: StoreSettings) -> Neo4jRunner:
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            settings.uri,
            auth=(settings.user, settings.password.get_secret_value()),
            user_agent="uniondeb-store/0.1",
        )
        return cls(driver, settings.target_database)

    async def verify(self) -> None:
        await self.driver.verify_connectivity()

    async def run(self, query: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        records, _, _ = await self.driver.execute_query(query, parameters_=dict(params or {}), database_=self.database)
        return [r.data() for r in records]

    async def run_system(self, query: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        """Administration commands (databases, roles, privileges) run against ``system``."""
        records, _, _ = await self.driver.execute_query(query, parameters_=dict(params or {}), database_="system")
        return [r.data() for r in records]

    async def close(self) -> None:
        await self.driver.close()


class RelateRunner:
    """Relate is synchronous and its connections are thread-affine (DB-API threadsafety 1), so a
    single dedicated worker thread opens the connection and runs every statement on it."""

    def __init__(self, path: str = ":memory:", *, read_only: bool = False) -> None:
        import relate

        self.path = path
        self.read_only = read_only
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relate")
        kwargs: dict[str, Any] = {} if path == ":memory:" else {"read_only": read_only}
        self.connection = self._pool.submit(relate.connect, path, **kwargs).result()

    @classmethod
    def open(cls, path: str = ":memory:", *, read_only: bool = False) -> RelateRunner:
        return cls(path, read_only=read_only)

    def _run_sync(self, query: str, params: dict[str, Any]) -> list[Row]:
        rows = self.connection.execute(query, params).fetchall()
        if not getattr(self.connection, "autocommit", True):
            self.connection.commit()
        return [dict(r) for r in rows]

    async def run(self, query: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._run_sync, query, dict(params or {}))

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, self.connection.close)
        self._pool.shutdown(wait=True)


@asynccontextmanager
async def open_runner(settings: StoreSettings, *, verify: bool = True) -> AsyncIterator[CypherRunner]:
    """Open the runner the settings describe, and always close it."""
    runner: Neo4jRunner | RelateRunner
    if settings.backend == "relate":
        runner = RelateRunner.open(settings.relate_path)
    else:
        runner = Neo4jRunner.from_settings(settings)
        if verify:
            await runner.verify()
    try:
        yield runner
    finally:
        await runner.close()
