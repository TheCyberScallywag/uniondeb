"""``uniondeb evals gate|online``."""

import argparse
import asyncio
import json
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("evals", help="offline eval gate and online eval report")
    p.add_argument("action", choices=["gate", "online"])
    p.add_argument("--run", type=Path, help="gate: an existing synthetic run (default: generate the pinned run)")
    p.add_argument("--model", help="gate: judge with this model instead of the ground-truth script")
    p.add_argument("--json", type=Path, help="write the report here as JSON")
    p.add_argument("--synthetic", action="store_true", help="online: read the synthetic database")
    p.set_defaults(func=lambda a: asyncio.run(_main(a)))


async def _main(args: argparse.Namespace) -> int:
    import logfire

    logfire.configure(send_to_logfire="if-token-present", console=False)
    if args.action == "online":
        from uniondeb.evals.online import online_report
        from uniondeb.store import StoreSettings, open_runner

        async with open_runner(StoreSettings.from_env(synthetic=args.synthetic or None)) as runner:
            report = await online_report(runner)
        print(report.model_dump_json(indent=1))
        return 1 if report.warnings else 0
    from uniondeb.evals.gate import run_gate

    report = await run_gate(args.run, model=args.model)
    for s in report.suites:
        print(f"{'ok  ' if s.passed else 'FAIL'} {s.score:6.1%} (target {s.target:.0%}, {s.cases} cases)  {s.name}")
        if not s.passed:
            print("     ", json.dumps(s.detail, default=str)[:600])
    print(
        f"judge: {report.judge}"
        + (" (scripted from ground truth: measures the pipeline, not a model)" if report.judge == "scripted" else "")
    )
    if args.json:
        args.json.write_text(report.model_dump_json(indent=1) + "\n")
    return 0 if report.passed else 1
