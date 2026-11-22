"""A generated run pushed through render → Cennad: every message is published or dead-lettered, none lost.

Verifies: UND-025
Verifies: UND-026
"""

import pytest
from pydantic import ValidationError

from uniondeb.core.mail import EmailEvent
from uniondeb.mail import render

generate = pytest.importorskip("uniondeb.synth.generate")
stream = pytest.importorskip("uniondeb.synth.stream")


def test_synthetic_run_is_fully_accounted_for(tmp_path, cennad, store, publisher):
    run = tmp_path / "run"
    generate.main(
        ["--out", str(run), "--days", "2", "--employees", "10", "--externals", "16"]
        + ["--rows-per-file", "200", "--error-rate", "0.03", "--seed", "7"]
    )
    events = []
    for doc in stream.replay_docs(run):
        try:
            events.append(EmailEvent.model_validate(doc))
        except ValidationError, ValueError:
            continue  # an injected fault: the generator's own dead letters
    assert events

    outcomes = [cennad.handle(render(ev), peer_ip="192.0.2.10") for ev in events]
    assert len(publisher.sent) + len(publisher.dead) == len(outcomes) == len(events)
    assert all(store.exists(o.raw_sha256) for o in outcomes)

    by_key = {s.key: s for s in publisher.sent}
    for ev, out in zip(events, outcomes, strict=True):
        if not out.published:
            continue
        doc = by_key[ev.message_id].doc
        assert doc["journal"]["source"] == ev.journal.source and doc["direction"] == ev.direction
        assert doc["subject"] == ev.subject and doc["sender"]["address"] == ev.sender.address
        assert [a["address"] for a in doc["bcc"]] == [a.address for a in ev.bcc]
    # valid events survive the wire: nothing the contract accepted is turned away by the parsers
    assert [o.error for o in outcomes if not o.published] == []
