"""Backup, restore and field-key rotation: the operations a disaster-recovery drill exercises.

AuraDB's managed backups are the first line. This is the portable second line and the thing a
restore *test* can run anywhere: a logical dump of every node and relationship to one compressed
file, a restore into an empty database, and a digest that must match on both sides. The audit
chain is then verified on the restored copy (``uniondeb audit verify``), which proves the restore
kept the one structure that cannot be rebuilt.

A backup contains everything, including record content, so it must be stored as the database is:
encrypted, access-controlled, and inside the retention schedule.

Implements: UND-092
"""

import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from uniondeb.store.crypto import CipherError, FieldCipher
from uniondeb.store.migrations import LEDGER_CONSTRAINT, load_migrations
from uniondeb.store.runner import CypherRunner

PAGE = 5000
ENCRYPTED: tuple[tuple[str, str, str], ...] = (
    ("IngestError", "id", "raw_enc"),
    ("CasePack", "id", "pack_enc"),
    ("Extract", "id", "pseudonyms_enc"),
)


class RestoreError(RuntimeError):
    pass


def _q(name: str) -> str:
    """A label, type or property name from a backup file, quoted so it can only ever be a name."""
    return "`" + name.replace("`", "``") + "`"


def _out(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if hasattr(value, "iso_format"):  # the Neo4j driver's own temporal types
        return {"$datetime": value.iso_format()}
    if isinstance(value, list):
        return [_out(v) for v in value]
    return value


def _in(props: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Plain properties, and the names of those that must be turned back into datetimes."""
    plain, stamps = {}, []
    for k, v in props.items():
        if isinstance(v, dict) and "$datetime" in v:
            plain[k], _ = v["$datetime"], stamps.append(k)
        else:
            plain[k] = v
    return plain, stamps


async def _pages(runner: CypherRunner, query: str):
    skip = 0
    while True:
        rows = await runner.run(f"{query} SKIP $skip LIMIT $limit", {"skip": skip, "limit": PAGE})
        yield rows
        if len(rows) < PAGE:
            return
        skip += PAGE


async def digest(runner: CypherRunner) -> dict[str, Any]:
    """Counts and an order-independent hash of every node and relationship (ids excluded)."""
    acc, nodes, rels = 0, 0, 0

    def add(obj: Any) -> None:
        nonlocal acc
        line = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
        acc ^= int.from_bytes(hashlib.sha256(line.encode()).digest(), "big")

    async for rows in _pages(
        runner, "MATCH (n) WITH n ORDER BY elementId(n) RETURN labels(n) AS l, properties(n) AS p"
    ):
        for r in rows:
            add([sorted(r["l"]), _out_all(r["p"])])
        nodes += len(rows)
    async for rows in _pages(
        runner, "MATCH ()-[r]->() WITH r ORDER BY elementId(r) RETURN type(r) AS t, properties(r) AS p"
    ):
        for r in rows:
            add([r["t"], _out_all(r["p"])])
        rels += len(rows)
    return {"nodes": nodes, "relationships": rels, "sha256": f"{acc:064x}"}


def _out_all(props: dict[str, Any]) -> dict[str, Any]:
    return {k: _out(v) for k, v in props.items()}


async def backup(runner: CypherRunner, path: Path) -> dict[str, Any]:
    """Write every node and relationship to ``path`` (gzip JSON lines). Returns the digest stored in it."""
    summary = await digest(runner)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"uniondeb_backup": 1, **summary}) + "\n")
        async for rows in _pages(
            runner,
            "MATCH (n) WITH n ORDER BY elementId(n) RETURN elementId(n) AS id, labels(n) AS l, properties(n) AS p",
        ):
            for r in rows:
                fh.write(
                    json.dumps({"n": r["id"], "l": sorted(r["l"]), "p": _out_all(r["p"])}, ensure_ascii=False) + "\n"
                )
        async for rows in _pages(
            runner,
            "MATCH (a)-[r]->(b) WITH a, r, b ORDER BY elementId(r) "
            "RETURN elementId(a) AS s, elementId(b) AS e, type(r) AS t, properties(r) AS p",
        ):
            for r in rows:
                fh.write(
                    json.dumps({"s": r["s"], "e": r["e"], "t": r["t"], "p": _out_all(r["p"])}, ensure_ascii=False)
                    + "\n"
                )
    path.chmod(0o600)
    return summary


async def restore(runner: CypherRunner, path: Path) -> dict[str, Any]:
    """Load a backup into an **empty** database and check its digest. Raises if anything differs."""
    if (await runner.run("MATCH (n) RETURN count(n) AS n"))[0]["n"]:
        raise RestoreError("restore needs an empty database: it never merges into existing data")
    await runner.run(LEDGER_CONSTRAINT)
    for migration in load_migrations():  # constraints and indexes first; the ledger nodes come from the backup
        for statement in migration.statements:
            await runner.run(statement)
    await runner.run("CREATE INDEX restore_bid IF NOT EXISTS FOR (n:RestoreTmp) ON (n.bid)")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        if header.get("uniondeb_backup") != 1:
            raise RestoreError(f"{path} is not a Uniondeb backup")
        nodes: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict]] = defaultdict(list)
        rels: dict[tuple[str, tuple[str, ...]], list[dict]] = defaultdict(list)

        async def flush(force: bool = False) -> None:
            for (labels, stamps), rows in list(nodes.items()):
                if force or len(rows) >= PAGE:
                    label = "".join(f":{_q(x)}" for x in (*labels, "RestoreTmp"))
                    fix = "".join(f" SET n.{_q(s)} = datetime(n.{_q(s)})" for s in stamps)
                    await runner.run(
                        f"UNWIND $rows AS row CREATE (n{label}) SET n = row.p SET n.bid = row.id{fix}", {"rows": rows}
                    )
                    del nodes[(labels, stamps)]
            for (kind, stamps), rows in list(rels.items()):
                if force or len(rows) >= PAGE:
                    fix = "".join(f" SET r.{_q(s)} = datetime(r.{_q(s)})" for s in stamps)
                    await runner.run(
                        "UNWIND $rows AS row MATCH (a:RestoreTmp {bid: row.s}) MATCH (b:RestoreTmp {bid: row.e}) "
                        f"CREATE (a)-[r:{_q(kind)}]->(b) SET r = row.p{fix}",
                        {"rows": rows},
                    )
                    del rels[(kind, stamps)]

        seen_rel = False
        for line in fh:
            item = json.loads(line)
            props, stamps = _in(item["p"])
            if "n" in item:
                nodes[(tuple(item["l"]), tuple(stamps))].append({"id": item["n"], "p": props})
            else:
                if not seen_rel:  # every node exists before the first relationship is created
                    await flush(force=True)
                    seen_rel = True
                rels[(item["t"], tuple(stamps))].append({"s": item["s"], "e": item["e"], "p": props})
            await flush()
        await flush(force=True)
    await runner.run("MATCH (n:RestoreTmp) REMOVE n:RestoreTmp, n.bid")
    await runner.run("DROP INDEX restore_bid IF EXISTS")
    restored = await digest(runner)
    expected = {k: header[k] for k in ("nodes", "relationships", "sha256")}
    if restored != expected:
        raise RestoreError(f"restored data does not match the backup: {restored} != {expected}")
    return restored


async def rotate_field_key(runner: CypherRunner, old: FieldCipher, new: FieldCipher) -> dict[str, int]:
    """Re-encrypt every encrypted property under ``new``. Safe to re-run: values already under the
    new key are left alone, and a value neither key opens stops the run before anything is lost."""
    counts: dict[str, int] = {}
    for label, key, prop in ENCRYPTED:
        rows = await runner.run(
            f"MATCH (n:`{label}`) WHERE n.`{prop}` IS NOT NULL RETURN n.`{key}` AS k, n.`{prop}` AS v ORDER BY k"
        )
        done = 0
        for row in rows:
            try:
                plain = old.decrypt(row["v"])
            except CipherError:
                new.decrypt(row["v"])  # already rotated; raises if neither key opens it
                continue
            await runner.run(
                f"MATCH (n:`{label}` {{`{key}`: $k}}) SET n.`{prop}` = $v", {"k": row["k"], "v": new.encrypt(plain)}
            )
            done += 1
        counts[f"{label}.{prop}"] = done
    return counts
