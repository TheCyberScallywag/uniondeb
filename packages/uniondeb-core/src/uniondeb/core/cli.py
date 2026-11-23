"""The ``uniondeb`` command line. Packages add subcommands through the ``uniondeb.cli`` entry-point
group: each entry point names a ``register(subparsers)`` function.
"""

import argparse
import sys
from importlib.metadata import entry_points


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uniondeb", description="Uniondeb platform tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for ep in sorted(entry_points(group="uniondeb.cli"), key=lambda e: e.name):
        ep.load()(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and not getattr(args, "passthrough", False):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.extra = extra  # subcommands that wrap their own parser set passthrough=True
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
