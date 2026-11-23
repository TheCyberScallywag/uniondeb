# /// script
# requires-python = ">=3.14"
# ///
"""Spike: can Relate carry per-agent extracts? Measures build rate, file size, read-only open,
query latency and memory for a Didolwr-style content extract cut from a loaded synthetic store.

    uv run python scripts/spikes/relate_extract.py --run traffic/run --out spike-results.json

The source store stands in for Aura (no Aura in this environment); the extract side, which is
what an agent touches, is measured for real. Results feed docs/adr/0002-relate-agent-extracts.md.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import logfire
import relate

from uniondeb.store import RelateRunner, StoreSettings, migrate
from uniondeb.store.load import load_run

EXTRACT_QUERY = """
MATCH (f:SourceFile)-[:CONTAINED]->(r:Record)
WHERE r.dataset IN ['messages', 'email', 'calls']
OPTIONAL MATCH (r)-[:FROM]->(fd:Device)
OPTIONAL MATCH (r)-[:TO]->(td:Device)
RETURN r.key AS key, r.dataset AS dataset, r.occurred_at AS at, r.channel AS channel,
       coalesce(r.body, r.transcript, r.content, '') AS text, coalesce(r.subject, '') AS subject,
       f.sha256 AS file, fd.id AS from_dev, td.id AS to_dev
"""


RSS_PROBE = """
import re, sys, relate
def hwm():  # VmHWM resets on exec; ru_maxrss is inherited from the parent on Linux
    return int(re.search(r"VmHWM:\\s+(\\d+)", open("/proc/self/status").read()).group(1))
base = hwm()
c = relate.connect(sys.argv[1], read_only=True)
c.execute("MATCH (r:Record) RETURN count(r) AS n").fetchall()
print((hwm() - base) / 1024)
"""


def open_rss_growth_mb(path: Path) -> float:
    """Peak RSS a fresh process gains by opening the extract read-only (Relate loads it whole)."""
    out = subprocess.run([sys.executable, "-c", RSS_PROBE, str(path)], capture_output=True, text=True, check=True)
    return round(float(out.stdout.strip()), 1)


def pseudonym(value: str | None, salt: str = "scan-2026-11") -> str | None:
    import hashlib
    import hmac

    return None if value is None else "p_" + hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]


async def main(run: Path, out: Path | None) -> dict:
    logfire.configure(send_to_logfire=False, console=False)
    tmp = Path(tempfile.mkdtemp(prefix="relate-spike-"))
    results: dict = {"run": str(run)}

    source = RelateRunner.open(str(tmp / "source.rl"))
    await migrate(source)
    t = time.perf_counter()
    summary = (await load_run(source, run, StoreSettings(uri="relate::memory:", synthetic=True))).summary()
    results["source"] = {"records": summary["records"], "load_s": round(time.perf_counter() - t, 2)}

    t = time.perf_counter()
    rows = await source.run(EXTRACT_QUERY)
    results["pull"] = {"rows": len(rows), "seconds": round(time.perf_counter() - t, 3)}
    await source.close()
    results["source"]["file_mb"] = round((tmp / "source.rl").stat().st_size / 1e6, 2)

    for r in rows:  # contract: parties arrive pseudonymised
        r["from_dev"], r["to_dev"] = pseudonym(r["from_dev"]), pseudonym(r["to_dev"])
    path = tmp / "didolwr.tag-batch.v1.rl"
    ext = RelateRunner.open(str(path))
    t = time.perf_counter()
    await ext.run("CREATE CONSTRAINT rk IF NOT EXISTS FOR (r:Record) REQUIRE r.key IS UNIQUE")
    for i in range(0, len(rows), 2000):
        await ext.run(
            "UNWIND $rows AS row CREATE (r:Record {key: row.key, dataset: row.dataset, at: row.at, "
            "channel: row.channel, text: row.text, subject: row.subject, file: row.file}) "
            "FOREACH (_ IN CASE WHEN row.from_dev IS NULL THEN [] ELSE [1] END | "
            "  MERGE (a:Party {id: row.from_dev}) CREATE (r)-[:FROM]->(a)) "
            "FOREACH (_ IN CASE WHEN row.to_dev IS NULL THEN [] ELSE [1] END | "
            "  MERGE (b:Party {id: row.to_dev}) CREATE (r)-[:TO]->(b))",
            {"rows": rows[i : i + 2000]},
        )
    build_s = time.perf_counter() - t
    await ext.close()
    size = path.stat().st_size
    results["extract"] = {
        "rows": len(rows),
        "build_s": round(build_s, 2),
        "rows_per_s": round(len(rows) / build_s) if build_s else None,
        "file_mb": round(size / 1e6, 3),
        "bytes_per_record": round(size / max(len(rows), 1)),
    }

    t = time.perf_counter()
    conn = relate.connect(str(path), read_only=True)
    open_s = time.perf_counter() - t
    t = time.perf_counter()
    q = conn.execute("MATCH (r:Record)-[:FROM]->(p:Party) RETURN p.id AS p, count(r) AS n ORDER BY n DESC LIMIT 5")
    top = q.fetchall()
    query_ms = (time.perf_counter() - t) * 1000
    try:
        conn.execute("CREATE (:Record {key: 'x'})").fetchall()
        refused = False
    except Exception as exc:
        refused = type(exc).__name__
    conn.close()
    results["read_only"] = {
        "open_ms": round(open_s * 1000, 1),
        "aggregate_query_ms": round(query_ms, 1),
        "write_refused_with": refused,
        "open_rss_growth_mb": open_rss_growth_mb(path),
        "top_parties_sample": len(top),
    }
    results["file_mode"] = oct(os.stat(path).st_mode & 0o777)
    results["plaintext_visible"] = (
        rows[0]["text"][:12].encode() in path.read_bytes() if rows and rows[0]["text"] else None
    )
    if out:
        out.write_text(json.dumps(results, indent=2) + "\n")
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    print(json.dumps(asyncio.run(main(a.run, a.out)), indent=2))
