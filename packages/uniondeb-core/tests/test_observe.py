"""Verifies: UND-016"""

import logfire
import pytest

from uniondeb.core import observe


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    observe._reset_for_tests()
    yield
    observe._reset_for_tests()


def test_configure_once_per_service(monkeypatch):
    calls = []
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.append(kw))
    first = observe.configure("porthor")
    second = observe.configure("didolwr")
    assert first is second and first.service_name == "porthor" and len(calls) == 1


def test_sends_only_with_a_token(monkeypatch):
    calls = []
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.append(kw))
    assert observe.configure("porthor").send_to_logfire is False
    assert calls[0]["send_to_logfire"] is False and calls[0]["token"] is None
    observe._reset_for_tests()
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_test")
    assert observe.configure("porthor").send_to_logfire is True
    assert calls[1]["send_to_logfire"] is True


def test_content_keys_are_scrubbed(monkeypatch):
    calls = []
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.append(kw))
    observe.configure("cennad")
    patterns = calls[0]["scrubbing"].extra_patterns
    assert {"body", "transcript", "imei"} <= set(patterns)


def test_trace_id_inside_and_outside_a_span():
    observe.configure("tarddiad")
    assert observe.current_trace_id() is None
    with logfire.span("work"):
        tid = observe.current_trace_id()
    assert tid is not None and len(tid) == 32 and int(tid, 16) != 0
