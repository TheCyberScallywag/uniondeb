"""Verifies: UND-013"""

import json

from uniondeb.core.cli import main


def test_store_migrate_then_status_via_relate_file(tmp_path, capsys, monkeypatch):
    for k in ("UNIONDEB_NEO4J_URI", "UNIONDEB_NEO4J_DATABASE"):
        monkeypatch.delenv(k, raising=False)
    uri = f"relate:{tmp_path / 'store.rl'}"
    assert main(["store", "--uri", uri, "migrate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"][0].startswith("0001_")
    assert main(["store", "--uri", uri, "status"]) == 0
    assert {m["state"] for m in json.loads(capsys.readouterr().out)["migrations"]} == {"applied"}
    assert main(["store", "--uri", uri, "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == []
