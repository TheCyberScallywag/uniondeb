# Runbooks

Short procedures for the things that will go wrong. Each says how you find out, what to do first,
and who must be told. They assume the compose stack in `deploy/`; adapt service names to the
production platform. "The governance owner" and "the nominated officer" are the people named
under backlog steps UND-005 and UND-001.

| Runbook | When |
|---|---|
| [Audit chain verification failed](audit-chain-failure.md) | The nightly `audit_verify` job is dead, or `uniondeb audit verify` exits 1 |
| [Backup and restore](backup-restore.md) | Scheduled restore drill; data loss; moving databases |
| [Key rotation](key-rotation.md) | Scheduled rotation; a key may have been exposed |
| [Incidents](incidents.md) | Suspected unauthorised access, a leak, or a wrong disclosure |
| [Dead jobs and stuck queues](jobs.md) | Uploads not being ingested; jobs in state `dead` |

**Status of the drill.** Backup, restore and key rotation are exercised by automated tests on the
embedded engine and on Neo4j Enterprise (`tests/test_disaster_recovery.py`,
`tests/test_integration_pipeline.py`). A restore of an AuraDB managed backup has not been tested:
there is no Aura instance yet. Do that drill, and record the time it took, before the pilot.
