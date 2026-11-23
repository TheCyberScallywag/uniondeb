"""``uniondeb synth generate|stream`` subcommands."""

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("synth", help="synthetic traffic and the journaled email stream")
    ssub = p.add_subparsers(dest="synth_command", required=True)
    g = ssub.add_parser("generate", help="write a labelled synthetic run (see --help)", add_help=False)
    g.set_defaults(func=lambda a: _generate(a.extra), passthrough=True)
    s = ssub.add_parser("stream", help="stream journaled email as JSON: replay|live (see --help)", add_help=False)
    s.set_defaults(func=lambda a: _stream(a.extra), passthrough=True)


def _generate(argv: list[str]) -> int:
    from .generate import main

    main(argv)
    return 0


def _stream(argv: list[str]) -> int:
    from .stream import main

    main(argv)
    return 0
