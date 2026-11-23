"""``uniondeb cennad parse``: canonical JSON for a good file, the reason and exit 1 for a bad one."""

import json

from uniondeb.core.cli import main
from uniondeb.mail import to_journal_report


def test_parse_prints_canonical_json(tmp_path, capsys, make_event):
    path = tmp_path / "report.eml"
    path.write_bytes(to_journal_report(make_event()))
    assert main(["cennad", "parse", str(path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["journal"]["source"] == "m365_envelope" and doc["bcc"][0]["address"] == "sian.private@mail.example"


def test_parse_reports_the_error_and_exits_1(tmp_path, capsys):
    path = tmp_path / "bad.eml"
    path.write_bytes(b"Subject: cyfrinach\r\n\r\nbody\r\n")
    assert main(["cennad", "parse", str(path)]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "no Message-ID" in err and "cyfrinach" not in err
    assert main(["cennad", "parse", str(tmp_path / "missing.eml")]) == 1
