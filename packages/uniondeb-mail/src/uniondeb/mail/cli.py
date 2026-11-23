"""``uniondeb cennad``: run the journal receiver, or parse one ``.eml`` to see what it would publish.

uniondeb cennad serve                  # settings from CENNAD_* (see uniondeb.mail.settings)
uniondeb cennad parse report.eml       # canonical JSON on stdout, or the parse error; exit 1
"""

import argparse
import contextlib
import logging
import signal
import sys
import threading
from pathlib import Path


def _serve(args: argparse.Namespace) -> int:
    from .receiver import from_settings
    from .settings import CennadSettings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = CennadSettings.from_env(
            host=args.host,
            port=args.port,
            allow_plaintext=args.allow_plaintext or None,
            synthetic=args.synthetic or None,
        )
        controller = from_settings(settings)
    except (ValueError, OSError) as exc:
        print(f"cennad: {exc}", file=sys.stderr)
        return 2
    if not settings.allow:
        print("cennad: CENNAD_ALLOW is empty, every sender will be rejected", file=sys.stderr)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    controller.start()
    print(f"cennad: listening on {settings.host}:{settings.port}, store {settings.store}", file=sys.stderr)
    try:
        stop.wait()
    finally:
        controller.stop()
        with contextlib.suppress(Exception):
            controller.handler.cennad.publisher.close()
    return 0


def _parse(args: argparse.Namespace) -> int:
    from .parse import ParseError, parse

    try:
        parsed = parse(Path(args.file).read_bytes())
    except OSError as exc:
        print(f"cennad: {exc.strerror or exc}: {args.file}", file=sys.stderr)
        return 1
    except ParseError as exc:
        print(f"cennad: {exc.reason}", file=sys.stderr)
        return 1
    print(parsed.event.model_dump_json(indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("cennad", help="mail journal receiver (Cennad)", description=__doc__)
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    commands = parser.add_subparsers(dest="cennad_command", required=True)

    serve = commands.add_parser("serve", help="run the SMTP receiver until interrupted")
    serve.add_argument("--host", help="overrides CENNAD_HOST")
    serve.add_argument("--port", type=int, help="overrides CENNAD_PORT")
    serve.add_argument("--allow-plaintext", action="store_true", help="accept mail without STARTTLS (demos only)")
    serve.add_argument("--synthetic", action="store_true", help="mark published documents as synthetic")
    serve.set_defaults(func=_serve)

    one = commands.add_parser("parse", help="parse one .eml and print the canonical JSON")
    one.add_argument("file", help="path to a journal report, BCC copy or plain message")
    one.set_defaults(func=_parse)
