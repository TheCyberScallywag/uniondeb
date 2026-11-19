"""``uniondeb store migrate|status``.

Implements: UND-013
"""

import argparse
import asyncio
import json

from uniondeb.store.migrations import migrate, status
from uniondeb.store.runner import open_runner
from uniondeb.store.settings import StoreSettings


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("store", help="AuraDB / Neo4j store administration")
    p.add_argument("--uri", help="overrides UNIONDEB_NEO4J_URI (neo4j+s://…, bolt://…, relate:<path>)")
    p.add_argument("--database", help="overrides UNIONDEB_NEO4J_DATABASE")
    p.add_argument("--synthetic", action="store_true", help="target the synthetic database")
    p.add_argument("action", choices=["migrate", "status"])
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    settings = StoreSettings.from_env(uri=args.uri, database=args.database, synthetic=args.synthetic or None)
    return asyncio.run(_main(settings, args.action))


async def _main(settings: StoreSettings, action: str) -> int:
    async with open_runner(settings) as runner:
        if action == "migrate":
            ran = await migrate(runner)
            print(
                json.dumps(
                    {"database": settings.target_database, "applied": [f"{m.version:04d}_{m.name}" for m in ran]}
                )
            )
            return 0
        rows = await status(runner)
        print(json.dumps({"database": settings.target_database, "migrations": rows}))
        return 1 if any(r["state"] == "drift" for r in rows) else 0
