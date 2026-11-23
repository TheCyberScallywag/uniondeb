# Governance and status

Uniondeb monitors people's communications and can lead to an allegation of criminal conduct about
a named person. The software alone does not make that lawful or fair. This page says what the
organisation must decide before any real data is processed, and what in the software is not
finished.

!!! danger "Not ready for real data"
    None of the governance items below has been signed off, and no independent security test has
    been done. Everything so far has run on synthetic data only.

## The governance gate

These are decisions and documents for people, not code. Each blocks processing of real data.
[Design §2](design.md#2-before-anything-touches-real-data-governance-gate) gives the reasoning.

| # | Item | Status | What the repository holds |
|---|---|---|---|
| G1 | Data protection impact assessment, with workers and members consulted | Not done | Nothing |
| G2 | Lawful basis recorded, with a legitimate-interests assessment | Not done | Nothing |
| G3 | Scope limited to organisation-provided channels and accounts | Not decided | The synthetic data includes personal devices to test detection only |
| G4 | Criminal-offence data condition identified | Not done | Nothing |
| G5 | Code of ethics adopted, with stable clause ids | Not done | A draft placeholder: `uniondeb-agents/…/config/code_of_ethics.draft.yaml` |
| G6 | Trigger list owned, versioned and change-controlled | Not done | A draft list with no named owner: `…/config/triggers.v1.yaml`. No `CODEOWNERS` file |
| G7 | Privacy notice and communication to staff and members | Not done | Nothing |
| G8 | No automated adverse decisions | Built in | Agents cannot confirm a *dirty* tag, open a case or submit a report. Confirm the legal position with counsel |
| G9 | Escalation and legal-advice route for suspected crime | Not decided | The SAR route is mocked. An `escalate_legal` decision exists for compliance leads |
| G10 | Retention schedule | Not done | A draft: `uniondeb-worker/…/config/retention.draft.yaml` |
| G11 | Nominated officer decision and SAR procedure | Not decided | The role exists in the software. The SAR glossary codes are placeholders |
| G12 | Confidentiality of investigations | Built in | Cases answer `404` to the unassigned; every case read is audited |
| G13 | Email scope: personal mail, privileged mail, third parties | Not decided | **Nothing implements an exclusion.** There is no legal-adviser allowlist or sealed queue |

G2, G4, G8 and G9 need a qualified data-protection or legal adviser.

## Decisions the organisation still owes

From the design. The recommendation is the design's, not a decision.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Channels in scope for real data | CyberScallywags-provided accounts and devices only |
| D2 | Code of ethics text and clause ids | Adopt formally; replace `code_of_ethics.DRAFT.md` |
| D3 | Trigger list owner and change process | Compliance lead; PR-reviewed; versioned |
| D4 | Aura tier and region | Business Critical, UK region (needed for fine-grained roles, ABAC and CDC) |
| D5 | Model hosting for content | Local model for Didolwr and Crynhoi; frontier models only for content-free tasks or under zero retention |
| D6 | Queue | arq + Redis (simplest); Kafka if streaming ingest from Sgwrs is wanted |
| D7 | Review console | Streamlit (fast, consistent with Llyw and Negesydd) |
| D8 | "Employee" definition | Include volunteers and members only if the DPIA and consultation support it |
| D9 | Retention periods | Set with the DPIA |
| D10 | Relate adoption | Go/no-go after the phase-3 spike; fallback stays ready |
| D11 | Who receives escalations | Named role plus legal adviser; never automated |
| D12 | Appoint a nominated officer? | Yes, with a deputy, knowing the duties that come with the role |
| D13 | Reporting routes to mock | NCA SAR · police referral · FCA report; the real integration route to be confirmed |
| D14 | Investigator conflict rules | Not self, own team or line manager; no shared household devices; reviewed by the nominated officer |
| D15 | Flags vocabulary | Start with §7.3; changes PR-reviewed and versioned like the trigger list |
| D16 | Mail platform(s) | Microsoft 365 envelope journaling if available (full BCC visibility); Google Workspace BCC routing otherwise |
| D17 | Inbound mail | Out of scope initially; sent mail only, as asked ("from company equipment") |
| D18 | Privileged and personal mail | Legal-adviser domain allowlist → sealed queue, never tagged by agents; personal-mail policy in the DPIA |
| D19 | Raw mail storage | S3-compatible object storage (for example MinIO), encrypted, retention matched to the record |

Two were settled by the build and are recorded in the decision records: the queue (D6) is `Job`
nodes in the graph, and the review console (D7) is server-rendered pages in the API
([ADR 0003](adr/0003-one-store-one-process-family.md)). Relate (D10) was adopted
([ADR 0002](adr/0002-relate-agent-extracts.md)).

## What in the software is not finished

A review of the code against its acceptance criteria found 28 of 40 software work items fully
met, 11 partly met and one not met. The findings below come from reading the code, and from
running the whole stack end to end on synthetic data.

### Must be closed before a pilot

| Gap | Detail |
|---|---|
| **No independent penetration test** | Only a self-review exists: [Security review](security-review.md), with open findings |
| **Database roles are not enforced per component** | The API and worker each use one database account. The fine-grained roles are defined (`uniondeb store roles`) but nothing applies them per agent, so extract contracts are enforced in code only |
| **Journaled email is not wired in** | Cennad publishes to a topic, but nothing runs the consumer: no command, no worker job, no compose service, and the Kafka client is not a dependency. Dead-lettered mail never reaches a review queue in a running system, and mail ingested this way is never tagged. Email reaches the system today only as files |
| **No privileged or personal mail exclusion** | See G13 |
| **Retention leaves copies behind** | Purging removes record content and raw failed rows. It does not purge uploaded source files, raw email, quoted evidence in tags and review items, or case packs. Purging raw failed rows ignores legal hold and records no provenance |
| **Some reads are not audited** | The concern and evaluation dashboards and the audit verification endpoint write no audit event. Audit anchoring runs only if `UNIONDEB_AUDIT_ANCHOR` is set, and the compose stack does not set it |
| **A real model has never been measured** | The evaluation gate scripts the ethics judge from the ground truth, so it measures the pipeline around the model. `uniondeb evals gate --model …` measures a real one |
| **People are matched by display name** | Roster people and email senders are joined on their name. That will mis-join on real data. There is no command to import a roster |
| **The conflict check trusts a typed-in id** | The officer assigning an investigator types the investigator's roster id; it is not taken from the identity provider |
| **Never run against production services** | Not AuraDB, not a Kafka broker, not a real Microsoft 365 or Google Workspace tenant |

### Missing or partial functions

- **Scans**: no out-of-hours signal and no BCC-to-external signal. Channel switching detects a move to a landline only. The contact-spike signal needs a calendar of sensitive dates that the stack does not supply. The list of personal mail domains is a placeholder.
- **Cases**: no way to lift a legal hold or remove an investigator through the API or portal. An investigator can make a record *dirty* in a case on their own decision, outside the two-reviewer rule. A compliance lead who is not also a reviewer cannot complete an `open_case` decision.
- **Flags**: flags never feed back into scans, and nothing shows a flag outside its case.
- **Provenance**: lineage reaches a source file from a record. From a tag, alert or case pack it stops at the extract; the portal works around this by tracing the record.
- **Holwr**: an answer that cites no records at all is accepted.
- **Evaluation**: no gate suites for Holwr, Crynhoi or mapping proposals. The online report is a snapshot with no trend, cost or alerting, and leaves email with no device out of the team comparison.
- **Observability**: the mail receiver does not configure logging, the evaluation command bypasses content scrubbing, and no framework instrumentation is switched on.
- **Notifications**: there is no sender and no alert on queue age; reviewers see a count when they look.
- **Backups**: `uniondeb store backup` writes an unencrypted file that includes record content, and does not cover uploaded files or raw mail.
- **Runbooks** describe two steps with no tooling: recording an `audit.recovery` event, and restoring uploads and raw mail from their own backup.

### Departures from the design

Recorded in [ADR 0003](adr/0003-one-store-one-process-family.md): the job queue is in the graph, not
Redis; agents write back in process, not over the network; the user interfaces are pages served
by the API.
