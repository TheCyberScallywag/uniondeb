# Backup and restore

AuraDB's managed backups are the primary backup. `uniondeb store backup` is the portable second
line and what the restore drill uses. A backup holds everything, including record content, so store
it like the database: encrypted, access-controlled, and deleted on the retention schedule.

## Take a backup

```bash
uniondeb store --file uniondeb-$(date +%F).jsonl.gz backup     # add --synthetic for the synthetic database
```

It prints node and relationship counts and a digest. Keep that line with the file.

## Restore

Restore only into an **empty** database; the command refuses anything else and never merges.

```bash
uniondeb store --database <new-empty-database> --file uniondeb-<date>.jsonl.gz restore
uniondeb audit --database <new-empty-database> verify --anchor <latest anchor file>
```

1. `restore` recreates constraints, loads the data, and compares the digest with the one stored in
   the file. It fails if they differ; do not use a database that failed this check.
2. `audit verify` must exit 0. With `--anchor` it also proves the restored chain reaches the last
   anchor. If the backup is older than the anchor, the difference is what was lost: record it
   (see [Audit chain verification failed](audit-chain-failure.md), step 5).
3. Point the services at the restored database and start `worker` before `api`.
4. Uploaded files and raw mail live outside the database (`UNIONDEB_UPLOAD_DIR`, Cennad's object
   store). Restore them from their own backup; files are named by SHA-256, so a missing one is
   detectable: every `SourceFile` node names the hash that must exist.

Agent extracts are never backed up. They are rebuilt from the database on demand.

## The drill

Quarterly, and before the pilot: restore last night's backup into a scratch database, run the two
commands above, run `uniondeb evals online` against it, and record how long it took. The automated
tests do this on every build with synthetic data; the drill proves it with the real backup and the
real people.
