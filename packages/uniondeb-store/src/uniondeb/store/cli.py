"""``uniondeb store migrate|status|roles``."""

import argparse
import asyncio
import json

from uniondeb.store import roles
from uniondeb.store.migrations import migrate, status
from uniondeb.store.runner import open_runner
from uniondeb.store.settings import StoreSettings


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("store", help="AuraDB / Neo4j store administration")
    p.add_argument("--uri", help="overrides UNIONDEB_NEO4J_URI (neo4j+s://…, bolt://…, relate:<path>)")
    p.add_argument("--database", help="overrides UNIONDEB_NEO4J_DATABASE")
    p.add_argument("--synthetic", action="store_true", help="target the synthetic database")
    p.add_argument("action", choices=["migrate", "status", "roles", "keygen", "backup", "restore", "rotate-key"])
    p.add_argument("--apply", action="store_true", help="with roles: reconcile the server (default prints the script)")
    p.add_argument("--file", help="with backup / restore: the backup file")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    settings = StoreSettings.from_env(uri=args.uri, database=args.database, synthetic=args.synthetic or None)
    if args.action == "keygen":
        from uniondeb.store.crypto import FieldCipher

        print(FieldCipher.generate_key())
        return 0
    if args.action == "roles" and not args.apply:
        print(";\n".join(roles.render(settings.target_database)) + ";")
        return 0
    return asyncio.run(_main(settings, args.action, args.file))


async def _main(settings: StoreSettings, action: str, file: str | None = None) -> int:
    async with open_runner(settings) as runner:
        if action in ("backup", "restore"):
            from pathlib import Path

            from uniondeb.store import maintenance

            if not file:
                raise SystemExit(f"{action} needs --file")
            summary = await getattr(maintenance, action)(runner, Path(file))
            print(json.dumps({"database": settings.target_database, action: summary}))
            return 0
        if action == "rotate-key":
            import os

            from uniondeb.store.crypto import FieldCipher
            from uniondeb.store.maintenance import rotate_field_key

            new = os.environ.get("UNIONDEB_FIELD_KEY_NEW")
            if not new:
                raise SystemExit("set UNIONDEB_FIELD_KEY (current) and UNIONDEB_FIELD_KEY_NEW (generate with keygen)")
            print(json.dumps({"rotated": await rotate_field_key(runner, FieldCipher.from_env(), FieldCipher(new))}))
            return 0
        if action == "roles":
            if not hasattr(runner, "run_system"):
                raise SystemExit("roles --apply needs Neo4j Enterprise or Aura, not Relate")
            counts = await roles.apply(runner, settings.target_database)
            print(json.dumps({"database": settings.target_database, "roles": counts}))
            return 0
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
