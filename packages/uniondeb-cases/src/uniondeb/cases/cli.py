"""``uniondeb audit verify|anchor``: the nightly check of the audit chain (plan §7.4).

``verify`` recomputes the chain and exits 1 when it is broken (and, with ``--anchor``, when a
previously written anchor no longer matches). ``anchor --out`` verifies first, then writes the
signed head outside the database; the key comes from ``UNIONDEB_AUDIT_ANCHOR_KEY``.

Implements: UND-071
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from uniondeb.cases.audit import AuditLog
from uniondeb.store.runner import open_runner
from uniondeb.store.settings import StoreSettings

KEY_ENV = "UNIONDEB_AUDIT_ANCHOR_KEY"


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("audit", help="verify and anchor the hash-chained audit log")
    p.add_argument("action", choices=["verify", "anchor"])
    p.add_argument("--uri", help="overrides UNIONDEB_NEO4J_URI (neo4j+s://…, bolt://…, relate:<path>)")
    p.add_argument("--database", help="overrides UNIONDEB_NEO4J_DATABASE")
    p.add_argument("--synthetic", action="store_true", help="target the synthetic database")
    p.add_argument("--from-seq", type=int, default=1, help="with verify: start at this event")
    p.add_argument("--anchor", type=Path, help="with verify: also check this anchor file")
    p.add_argument("--out", type=Path, help="with anchor: where to write the signed head")
    p.set_defaults(func=_run)


def _key() -> bytes:
    if not (key := os.environ.get(KEY_ENV)):
        raise SystemExit(f"set {KEY_ENV} to sign or check an anchor")
    return key.encode()


def _run(args: argparse.Namespace) -> int:
    if args.action == "anchor" and args.out is None:
        raise SystemExit("audit anchor needs --out <path>")
    key = _key() if args.action == "anchor" or args.anchor else b""
    settings = StoreSettings.from_env(uri=args.uri, database=args.database, synthetic=args.synthetic or None)
    return asyncio.run(_main(settings, args, key))


async def _main(settings: StoreSettings, args: argparse.Namespace, key: bytes) -> int:
    async with open_runner(settings) as runner:
        log = AuditLog(runner)
        result = await log.verify(from_seq=args.from_seq if args.action == "verify" else 1)
        out = {"database": settings.target_database, "verify": result.model_dump()}
        ok = result.ok
        if ok and args.action == "anchor":
            out["anchor"] = (await log.anchor(args.out, key)).model_dump() | {"path": str(args.out)}
        elif args.action == "verify" and args.anchor:
            check = await log.check_anchor(args.anchor, key)
            out["anchor"] = check.model_dump()
            ok = ok and check.ok
        print(json.dumps(out))
        return 0 if ok else 1
