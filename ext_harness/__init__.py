"""External delivery harness for Uniondeb.

A small FastAPI application that stands where a source system would: it accepts files (``POST
/files``) and record streams (``WS /ws/stream/{dataset}``), forwards them to the Uniondeb API's
``POST /uploads``, follows each one through ingest, and reports progress as server-sent events
(``GET /events``). It talks to Uniondeb only over HTTP, with an ``uploader`` token.

    uv run python -m ext_harness make-data      # build the test data set in data/
    uv run python -m ext_harness serve          # the delivery platform on 127.0.0.1:8090
    uv run python -m ext_harness send data/run-001/data --watch

See ``ext_harness/README.md``.
"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from ext_harness.app import create_app as build

    return build(*args, **kwargs)
