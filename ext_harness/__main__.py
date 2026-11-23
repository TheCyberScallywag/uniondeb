"""``python -m ext_harness serve|make-data|send|stream|watch``."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from ext_harness.deliveries import TERMINAL
from ext_harness.settings import DATASETS, EXTENSIONS

DEFAULT_URL = "http://127.0.0.1:8090"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ext_harness", description="Delivery harness for Uniondeb")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the delivery platform")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)

    p = sub.add_parser("make-data", help="build the synthetic test data set")
    p.add_argument("--out", type=Path, default=Path("data"))
    p.add_argument("--run", default="run-001")
    p.add_argument("--force", action="store_true", help="replace an existing data set")
    for name, kind in (("seed", int), ("start", str), ("days", int), ("employees", int), ("externals", int)):
        p.add_argument(f"--{name}", type=kind)

    def client(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", default=os.environ.get("HARNESS_URL", DEFAULT_URL), help="the harness")
        p.add_argument("--key", default=os.environ.get("HARNESS_API_KEY"), help="the harness key, if it has one")

    p = sub.add_parser("send", help="deliver files, or every data file under a folder")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--dataset", choices=sorted(DATASETS), help="default: taken from the path, else Uniondeb detects it")
    p.add_argument("--source-system")
    p.add_argument("--limit", type=int, help="send at most this many files")
    p.add_argument("--watch", action="store_true", help="wait until every delivery reaches a final state")
    client(p)

    p = sub.add_parser("stream", help="send a file of JSON lines over the WebSocket, one record per frame")
    p.add_argument("file", type=Path)
    p.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    p.add_argument("--rate", type=float, default=0, help="records per second; 0 = as fast as possible")
    p.add_argument("--batch", type=int, help="records per delivered file")
    p.add_argument("--limit", type=int)
    p.add_argument("--source-system")
    p.add_argument("--watch", action="store_true", help="wait until every batch reaches a final state")
    client(p)

    p = sub.add_parser("watch", help="print status events as they happen")
    p.add_argument("--replay", action="store_true", help="start with the events the harness still remembers")
    client(p)

    args = ap.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("ext_harness.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.command == "make-data":
        from ext_harness.make_data import build

        options = {k: getattr(args, k) for k in ("seed", "start", "days", "employees", "externals")}
        c = build(args.out, run=args.run, force=args.force, **options)
        print(f"{args.out}/: {len(c['files'])} files, {len(c['streams'])} streams, {len(c['edge_cases'])} edge cases")
        print(f"see {args.out}/README.md")
        return 0
    try:
        return asyncio.run({"send": _send, "stream": _stream, "watch": _watch}[args.command](args))
    except httpx.HTTPStatusError as exc:
        hint = ": pass --key or set HARNESS_API_KEY" if exc.response.status_code == 401 else ""
        print(f"the harness answered HTTP {exc.response.status_code}{hint}", file=sys.stderr)
        return 2
    except httpx.ConnectError, ConnectionRefusedError:
        print(f"no harness at {args.url}: start it with `python -m ext_harness serve`", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {"X-API-Key": args.key} if args.key else {}


def _dataset_of(path: Path) -> str | None:
    return next((part for part in reversed(path.parts[:-1]) if part in DATASETS), None)


async def _send(args: argparse.Namespace) -> int:
    paths: list[Path] = []
    for path in args.paths:
        found = sorted(p for p in path.rglob("*") if p.suffix.lower() in EXTENSIONS) if path.is_dir() else [path]
        paths.extend(found)
    paths = paths[: args.limit] if args.limit else paths
    if not paths:
        print("nothing to send", file=sys.stderr)
        return 1
    ids, refused, names = [], 0, {}
    async with httpx.AsyncClient(base_url=args.url, headers=_headers(args), timeout=120) as http:
        for path in paths:
            form = {"dataset": args.dataset or _dataset_of(path.resolve()), "source_system": args.source_system}
            with path.open("rb") as fh:
                r = await http.post(
                    "/files", data={k: v for k, v in form.items() if v}, files={"files": (path.name, fh)}
                )
            if r.status_code == 202:
                ids += [d["id"] for d in r.json()["accepted"]]
                names[ids[-1]] = str(path)
                print(f"accepted  {path}")
            else:
                refused += 1
                detail = r.json().get("detail")
                reason = detail["refused"][0]["reason"] if isinstance(detail, dict) else detail
                print(f"refused   {path}: {reason}")
        print(f"{len(ids)} accepted, {refused} refused")
        if args.watch and ids:
            return await _wait(http, ids, names)
    return 1 if refused and not ids else 0


async def _stream(args: argparse.Namespace) -> int:
    from urllib.parse import urlencode

    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    query = {"batch": args.batch, "source_system": args.source_system, "key": args.key}
    url = args.url.replace("http", "ws", 1) + f"/ws/stream/{args.dataset}"
    if query := urlencode({k: v for k, v in query.items() if v}):
        url += "?" + query
    ids: list[str] = []
    sent = 0

    try:
        ws = await connect(url, max_size=None)
    except InvalidStatus as exc:
        print(
            f"the harness refused the stream (HTTP {exc.response.status_code}): check --dataset and --key",
            file=sys.stderr,
        )
        return 2
    async with ws:

        async def listen() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "batch":
                    ids.append(msg["delivery_id"])
                    print(f"batch     {msg['records']} records → delivery {msg['delivery_id']}")
                elif msg["type"] == "rejected":
                    print(f"rejected  {msg['reason']}: {msg['frame'][:80]}")
                elif msg["type"] == "closed":
                    print(f"closed    {msg['received']} received, {msg['rejected']} rejected, {msg['batches']} batches")

        listener = asyncio.create_task(listen())
        started = time.monotonic()
        with args.file.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if args.limit and sent >= args.limit:
                    break
                await ws.send(line.strip())
                sent += 1
                if args.rate > 0:
                    await asyncio.sleep(max(0.0, started + sent / args.rate - time.monotonic()))
        await ws.send(json.dumps({"$control": "close"}))
        await listener
    print(f"{sent} records sent")
    if args.watch and ids:
        async with httpx.AsyncClient(base_url=args.url, headers=_headers(args), timeout=120) as http:
            return await _wait(http, ids)
    return 0


async def _wait(http: httpx.AsyncClient, ids: list[str], names: dict[str, str] | None = None) -> int:
    """Follow the event stream until every delivery in ``ids`` is final; non-zero if any did not arrive."""
    pending, final = set(ids), {}
    async for event, data in _events(http, replay=True):
        if event != "delivery" or data["id"] not in pending:
            continue
        if data["state"] in TERMINAL:
            pending.discard(data["id"])
            final[data["id"]] = data["state"]
            u = data.get("uniondeb") or {}
            counts = (
                f"rows {u.get('rows')}, loaded {u.get('loaded')}, in review {u.get('in_review')}, linked {u.get('linked')}"
                if u
                else ""
            )
            print(
                f"{data['state']:<13} {(names or {}).get(data['id'], data['filename'])}  {counts}{' — ' + data['detail'] if data.get('detail') else ''}"
            )
        if not pending:
            break
    summary: dict[str, int] = {}
    for state in final.values():
        summary[state] = summary.get(state, 0) + 1
    print(", ".join(f"{n} {state}" for state, n in sorted(summary.items())))
    return 0 if set(final.values()) <= {"ingested", "duplicate"} else 1


async def _watch(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.url, headers=_headers(args), timeout=None) as http:
        async for event, data in _events(http, replay=args.replay):
            if event == "delivery":
                print(
                    f"{data['at']}  {data['state']:<13} {data['filename']}{'  ' + data['detail'] if data.get('detail') else ''}"
                )
            else:
                print(f"{data['at']}  {event:<13} {json.dumps({k: v for k, v in data.items() if k != 'at'})}")
    return 0


async def _events(http: httpx.AsyncClient, *, replay: bool):
    """Parse the harness's SSE stream into (event, data) pairs."""
    async with http.stream("GET", "/events", params={"replay": str(replay).lower()}, timeout=None) as response:
        response.raise_for_status()
        event = "message"
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                yield event, json.loads(line[5:])
                event = "message"


if __name__ == "__main__":
    sys.exit(main())
