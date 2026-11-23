# Audit chain verification failed

**What it means.** An audit event was changed, removed or reordered after it was written, or the
database was restored to an earlier point. Treat it as a security incident until shown otherwise.

**How you find out.** The nightly `audit_verify` job fails and is parked as `dead` with
`audit chain broken at seq N`; or `uniondeb audit verify` exits 1; or the anchor check fails.

1. **Stop changes, keep evidence.** Stop the `api` and `worker` services. Do not restart the
   database. Take a backup of the current state: `uniondeb store --file broken-<date>.jsonl.gz backup`.
2. **Locate the break.** `uniondeb audit verify` prints `broken_at` (the first bad sequence number)
   and the reason: an edited field, a gap, a relinked chain. `uniondeb audit verify --anchor <file>`
   compares the chain with the last anchor written outside the database; a mismatch there means the
   chain was rewritten or truncated after that anchor.
3. **Tell** the governance owner and the nominated officer now, not after diagnosis. If a case is
   affected, its investigator stops work on it.
4. **Decide the cause.** A restore from backup to an earlier time produces a clean chain that is
   shorter than the anchor: that is an operational event, and step 5 applies. Anything else is
   tampering: follow [Incidents](incidents.md) and involve whoever provides legal advice (plan G9).
5. **Recover.** Never edit or delete audit events to "repair" the chain. Restore the most recent
   backup whose chain verifies and whose head matches an anchor
   ([Backup and restore](backup-restore.md)). Record what was lost between that backup and the
   break as a new audit event (`audit.recovery`, with the reason), so the gap is itself on the record.
6. **Afterwards.** Rotate the database credentials and the anchor key
   ([Key rotation](key-rotation.md)), and write up how the events could be changed at all.
