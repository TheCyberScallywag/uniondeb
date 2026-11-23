"""``uniondeb extract contracts|reap``."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from uniondeb.extract.contracts import ContractRegistry


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("extract", help="extract contracts and expiry")
    p.add_argument("action", choices=["contracts", "reap"])
    p.add_argument("--dir", type=Path, help="contracts directory (default: the packaged contracts)")
    p.add_argument("--root", type=Path, help="extract volume (default: $UNIONDEB_EXTRACT_ROOT)")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    registry = ContractRegistry.load(args.dir)
    if args.action == "contracts":
        for c in registry:
            print(
                json.dumps({"id": c.id, "agent": c.agent, "role": c.aura_role, "hash": c.hash, "max_rows": c.max_rows})
            )
        return 0
    root = args.root or Path(os.environ.get("UNIONDEB_EXTRACT_ROOT", "/run/uniondeb/extracts"))
    return asyncio.run(_reap(registry, root))


async def _reap(registry: ContractRegistry, root: Path) -> int:
    from uniondeb.extract.builder import Echdynnwr
    from uniondeb.store import StoreSettings, Tarddiad, open_runner

    async with open_runner(StoreSettings.from_env()) as runner:
        reaped = await Echdynnwr(runner, Tarddiad(runner), registry, root=root).reap()
    print(json.dumps({"reaped": reaped}))
    return 0
