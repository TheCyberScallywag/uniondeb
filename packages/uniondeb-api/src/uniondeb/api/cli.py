"""``uniondeb api serve|token``."""

import argparse
import os


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("api", help="the API gateway, review console and investigation portal")
    p.add_argument("action", choices=["serve", "token"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--sub", help="with token: the user id")
    p.add_argument("--roles", default="", help="with token: comma-separated roles")
    p.add_argument("--person-id", help="with token: the user's own roster id")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    if args.action == "token":
        from uniondeb.api.auth import AuthSettings, mint

        secret = AuthSettings.from_env().secret
        if secret is None or not args.sub:
            raise SystemExit("token needs UNIONDEB_AUTH_SECRET (development only) and --sub")
        print(
            mint(secret.get_secret_value(), args.sub, [r for r in args.roles.split(",") if r], person_id=args.person_id)
        )
        return 0
    import uvicorn

    uvicorn.run(
        "uniondeb.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    return 0
