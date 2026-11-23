"""Work out what a file is: its format from its bytes, its dataset from its columns.

Detection never guesses silently. Format comes from magic bytes, then the first character. The
dataset is the one whose contract accepts the most sample rows; when none accepts any, it is the
one whose alias table recognises the most keys, so a file with faults is still routed to the right
contract and its rows fail there with useful errors.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from uniondeb.core.mail import EmailEvent
from uniondeb.core.resolve import normalize
from uniondeb.core.schemas import DATASETS
from uniondeb.store.load import parse_row

SAMPLE = 25
_EMAIL_KEYS = {normalize(k) for k in ("message_id", "internetMessageId", "journal", "envelope_recipients", "bcc")}


class DetectionError(ValueError):
    pass


def detect_format(path: Path) -> str:
    with path.open("rb") as fh:
        head = fh.read(4096)
    if head[:4] == b"PAR1":
        return "parquet"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not text:
        raise DetectionError("the file is empty")
    return "jsonl" if text[:1] == b"{" else "csv"


def _coverage(dataset: str, rows: list[dict[str, Any]]) -> float:
    if dataset == "email":
        known = set(EmailEvent._key_lookup)
        return sum(
            len({normalize(k) for k in r} & known) + 3 * len({normalize(k) for k in r} & _EMAIL_KEYS) for r in rows
        )
    ds = DATASETS[dataset]
    known = {normalize(c.name) for c in ds.columns} | set(ds.model._key_lookup)
    return sum(len({normalize(k) for k in r} & known) for r in rows)


def detect_dataset(rows: list[dict[str, Any]]) -> str:
    sample = rows[:SAMPLE]
    if not sample:
        raise DetectionError("the file has no rows")
    scores: dict[str, tuple[int, float]] = {}
    for name in (*DATASETS, "email"):
        ok = 0
        for row in sample:
            try:
                parse_row(name, row)
                ok += 1
            except ValidationError, ValueError, TypeError, KeyError:
                pass
        scores[name] = (ok, _coverage(name, sample))
    best = max(scores, key=lambda n: scores[n])
    if scores[best] == (0, 0):
        raise DetectionError("no dataset contract recognises any column of this file")
    return best
