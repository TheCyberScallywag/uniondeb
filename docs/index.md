# Uniondeb

*Uniondeb* is Welsh for "integrity". It is a conduct-assurance platform for CyberScallywags UK:
it takes in records of calls, messages, hand-offs and email sent from the organisation's own
accounts and equipment, tags each one against the code of ethics, looks for patterns across them,
and gives the people responsible a place to review, investigate and report.

!!! warning "Synthetic data only"
    The software is built and passes its tests on synthetic data. It must not be given real
    communications until the governance gate is signed off and an independent security test has
    been done. See [Governance and status](governance.md).

## What it does

| Function | What happens | Who or what does it |
|---|---|---|
| **Ingest** | Files (JSON lines, CSV, Parquet) and journaled email are checked, normalised and written to the graph, each record tied to the SHA-256 of the file it came from. Rows that fail are held for a person. | Porthor (files), Cennad (mail) |
| **Tag** | Every record gets one tag: *clean*, *question* or *dirty*. A trigger-word hit is at least *question*. A model may propose *dirty*; only two reviewers can confirm it. | Didolwr |
| **Scan** | Scheduled scans compute concern rates per person and team and look for graph patterns such as unregistered devices, identifier churn and channel switching. They raise alerts about pseudonyms, not names. | Gwyliwr |
| **Review** | Reviewers work queues of ingest errors, question tags, proposed dirty tags and alerts. Dirty tags and alerts need two different reviewers. | People, in the review console |
| **Investigate** | An alert that two reviewers agree on becomes a case. An assigned investigator sees the timeline, contacts and provenance, adds notes and flags, and drafts a suspicious activity report. | People, in the investigation portal |
| **Report** | A nominated officer approves a SAR draft, which is submitted to a mock portal. | People |
| **Account for itself** | Every write records what produced it (provenance). Every read of a case is written to a hash-chained audit log. | Tarddiad, the audit log |

## Rules the code enforces

- **People decide.** No agent can make a record currently *dirty*, open a case or submit a report.
- **Tags are append-only.** A new tag supersedes the old one; nothing is overwritten.
- **A model cannot clear a trigger hit.** It can only raise a tag, never lower one.
- **Alerts are pseudonymous.** A name is revealed only to a compliance lead, with a reason, and the reveal is audited.
- **Agents see only what their contract allows.** Each agent reads a temporary, read-only extract built from a versioned contract that lists the columns it may have.
- **Cases are invisible to the unassigned.** A case you are not assigned to and a case that does not exist both answer `404`.
- **Synthetic and real data never share a database.**

## Where to go next

- [How it works](how-it-works.md): the components, the data model and the path a record takes.
- [Getting started](getting-started.md): start the stack and sign in.
- [Sending data directly](run-direct.md) or [through the delivery harness](run-harness.md).
- [Review, scans and cases](review-and-cases.md): what to do once data is in.
