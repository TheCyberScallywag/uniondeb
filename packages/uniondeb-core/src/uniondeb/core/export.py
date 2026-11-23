"""``uniondeb schemas export``: write JSON Schema, Frictionless Table Schema and Arrow/Parquet schema
files for every dataset (calls, messages, handoff) and the email JSON Schema.
"""

import argparse
import json
from pathlib import Path

from .schemas import DATASETS, arrow_schema, email_json_schema, json_schema, table_schema


def export(out: Path) -> list[Path]:
    written: list[Path] = []
    for sub in ("json", "csv", "parquet"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for name, ds in DATASETS.items():
        for path, text in (
            (out / "json" / f"{name}.schema.json", json.dumps(json_schema(ds), indent=2, ensure_ascii=False)),
            (out / "csv" / f"{name}.tableschema.json", json.dumps(table_schema(ds), indent=2, ensure_ascii=False)),
            (out / "parquet" / f"{name}.arrow-schema.txt", arrow_schema(ds).to_string(show_field_metadata=True)),
        ):
            path.write_text(text + "\n")
            written.append(path)
    email = out / "json" / "email.schema.json"
    email.write_text(json.dumps(email_json_schema(), indent=2, ensure_ascii=False) + "\n")
    written.append(email)
    return written


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("schemas", help="dataset schemas")
    ssub = p.add_subparsers(dest="schemas_command", required=True)
    e = ssub.add_parser("export", help="write JSON / Table Schema / Arrow schema files")
    e.add_argument("--out", type=Path, default=Path("schemas"))
    e.set_defaults(func=lambda a: print("\n".join(str(p) for p in export(a.out))))
