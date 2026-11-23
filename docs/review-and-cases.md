# Review, scans and cases

Once data is in, the rest is people's work. This page follows it from the review queues to a closed
case. Every step works in the browser (<http://localhost:8000>) and through the API; the examples
use the API.

```sh
set -a; . deploy/.env; set +a
token() { uv run uniondeb api token --sub "$1" --roles "$2" ${3:+--person-id "$3"}; }
api()   { curl -s -H "Authorization: Bearer $1" -H "Content-Type: application/json" "${@:2}"; }

ANN=$(token ann reviewer);  BOB=$(token bob reviewer)
LEAD=$(token lead reviewer,compliance_lead);  COMP=$(token comp compliance)
OFFICER=$(token olwen nominated_officer);  INV=$(token ifan investigator CS-0030)
```

## 1. The review queues

```sh
api $ANN http://localhost:8000/review/queues
api $ANN "http://localhost:8000/review/items?kind=question_tag&limit=50"
api $ANN http://localhost:8000/review/items/<item id>
```

An item's detail shows the record, why it was queued and the decisions open to you.

```sh
api $ANN -X POST http://localhost:8000/review/items/<item id>/decisions \
    -d '{"decision": "escalate", "rationale": "reads as a supply arrangement"}'
```

- **`question_tag`**: `clear` it, or `escalate` it, which creates a `dirty_tag` item.
- **`dirty_tag`**: needs two different reviewers. After the first, the item is `awaiting_second`.
  The same reviewer deciding twice is refused. Only when both choose `confirm_dirty` does the
  record's current tag become *dirty*.
- **`ingest_error`**: `fix_resubmit` with the corrected row in `fixed_row`, `reject`, or
  `source_issue`.

## 2. Run a scan

Scans run daily and weekly on their own, over the days up to now. The test data is dated October
2026, so ask for that window:

```sh
api $COMP -X POST http://localhost:8000/scans \
    -d '{"kind": "on_demand", "window_days": 14, "window_to": "2026-10-19T00:00:00+00:00"}'
api $COMP http://localhost:8000/jobs/<job>
api $COMP http://localhost:8000/dashboards/concern
```

The job's result gives the number of alerts raised. Scans need the
[roster](test-data.md#the-roster) to attribute devices to people.

## 3. Work an alert

Alerts arrive in the `alert` queue and name a pseudonym such as `S-69D8DE7D27F8`.

```sh
api $ANN "http://localhost:8000/review/items?kind=alert"
api $ANN -X POST http://localhost:8000/review/alerts/<alert id>/pack          # a case pack: timeline and evidence
api $LEAD -X POST http://localhost:8000/review/alerts/<alert id>/identity \
    -d '{"reason": "two reviewers intend to open a case; conflict check"}'
```

Only a compliance lead can reveal the name, and must give a reason. Two reviewers then decide:

```sh
for T in $ANN $BOB; do
  api $T -X POST http://localhost:8000/review/items/<item id>/decisions \
      -d '{"decision": "open_case", "rationale": "unregistered device contact", "case_pack_version": "1"}'
done
```

The second decision returns the new case in `generated`.

## 4. Investigate the case

The nominated officer assigns an investigator. The investigator's roster id is checked against the
subject: not the same person, not the same team, not their line manager.

```sh
api $OFFICER http://localhost:8000/cases
api $OFFICER -X POST http://localhost:8000/cases/<case id>/assignments \
    -d '{"investigator_id": "ifan", "person_id": "CS-0030"}'
```

Until assigned, the investigator gets `404` for the case. After:

```sh
api $INV http://localhost:8000/cases/<case id>/timeline
api $INV http://localhost:8000/cases/<case id>/contacts
api $INV "http://localhost:8000/cases/<case id>/records/<record key>/provenance"
api $INV -X POST http://localhost:8000/cases/<case id>/identity -d '{"reason": "needed to interview"}'
api $INV -X POST http://localhost:8000/cases/<case id>/notes -d '{"text": "…"}'
api $INV -X POST http://localhost:8000/cases/<case id>/flags \
    -d '{"label": "legal_hold", "reason": "preserve for possible referral"}'
```

A `legal_hold` flag stops retention from purging the subject's records. Every one of these calls
is written to the audit log.

With a model configured, `POST /cases/<id>/holwr` answers a question about the case and
`POST /cases/<id>/sar/narrative` drafts a narrative. Without one they answer `503`.

## 5. Report and close

```sh
api $INV -X POST http://localhost:8000/cases/<case id>/sar/drafts -d '{}'
api $INV -X PUT  http://localhost:8000/sar/drafts/<draft id> -d '{
  "subject": {"name": "…", "date_of_birth": "UNKNOWN", "address": "UNKNOWN",
              "identifiers": ["CS-0010"], "occupation": "…"},
  "glossary_codes": ["XXD9XX"],
  "reason_for_suspicion": "XXD9XX …"}'
api $OFFICER -X POST http://localhost:8000/sar/drafts/<draft id>/approval -d '{}'
```

The reason for suspicion must begin with its glossary codes. Only the nominated officer can
approve, and approval submits to the **mock** portal and returns a reference beginning `MOCK-SAR-`.
The glossary codes shipped are placeholders.

```sh
api $OFFICER -X POST http://localhost:8000/cases/<case id>/closure \
    -d '{"outcome": "sar_submitted", "retention_review": "review at 6 years from closure"}'
```

Outcomes are `closed_no_action`, `closed_conduct_process`, `sar_submitted` and `referred`. A case
cannot be closed as `sar_submitted` unless a submission exists.

## 6. Check the audit log

```sh
AUDITOR=$(token ada auditor)
api $AUDITOR http://localhost:8000/audit/verification
api $AUDITOR "http://localhost:8000/audit/events?case_id=<case id>"
uv run uniondeb audit verify --synthetic
```

`ok: true` means every event's hash matches the chain. If it does not, follow the
[runbook](runbooks/audit-chain-failure.md).

## What to expect from the test data

With the standard [test data](test-data.md) and no model configured, a scan over 5–19 October
raises two alerts once reviewers have confirmed the scenario's *dirty* messages. Their subjects are
the two scenario actors, `CS-0010` (drug supply) and `CS-0001` (insider dealing).
`data/run-001/ground_truth/` has the expected tag for every record.
