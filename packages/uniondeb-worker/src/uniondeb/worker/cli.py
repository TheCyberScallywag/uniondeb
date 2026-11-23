"""``uniondeb worker run|once|enqueue``."""

import argparse
import asyncio
import json
import signal

from uniondeb.core.observe import configure
from uniondeb.worker.services import Services
from uniondeb.worker.worker import Worker, schedule_due


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("worker", help="run queued and scheduled jobs")
    p.add_argument("action", choices=["run", "once", "enqueue"])
    p.add_argument("kind", nargs="?", help="with enqueue: ingest | tag | scan | reap | retention | audit_verify")
    p.add_argument("--args", default="{}", help="with enqueue: job arguments as JSON")
    p.add_argument("--synthetic", action="store_true", help="work on the synthetic database")
    p.set_defaults(func=lambda a: asyncio.run(_main(a)))


async def _main(args: argparse.Namespace) -> int:
    configure("uniondeb-worker")
    async with Services.from_env(synthetic=args.synthetic or None) as services:
        if args.action == "enqueue":
            if not args.kind:
                raise SystemExit("enqueue needs a job kind")
            print(json.dumps({"job": await services.queue.enqueue(args.kind, json.loads(args.args))}))
            return 0
        worker = Worker(services)
        if args.action == "once":
            await schedule_due(services)
            print(json.dumps({"ran": await worker.drain(), "jobs": await services.queue.counts()}))
            return 0
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await worker.run_forever(stop)
        return 0
