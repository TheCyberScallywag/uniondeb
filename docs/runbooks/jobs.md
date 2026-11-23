# Dead jobs and stuck queues

Jobs are `(:Job)` nodes. A job that fails is retried with backoff (three attempts by default) and
then parked as `dead`. Nothing is dropped: the upload, its `SourceFile` and the job all remain.

```cypher
MATCH (j:Job) RETURN j.state, count(*);
MATCH (j:Job {state: 'dead'}) RETURN j.id, j.kind, j.error, j.finished_at ORDER BY j.finished_at DESC;
```

| Symptom | Likely cause | Do |
|---|---|---|
| Uploads stay `uploaded` | No worker running | Start `worker`; check its logs |
| `ingest` dead: file not found | Upload volume not shared between `api` and `worker` | Fix the mount, then requeue |
| `tag` dead: model error | The model named in `UNIONDEB_DIDOLWR_MODEL` is unreachable | Fix it, or unset it to run the deterministic pass alone |
| `tag` dead: extract too large | A file has more rows than the contract's cap | Split the file and upload the parts |
| `audit_verify` dead | The chain is broken | [Audit chain verification failed](audit-chain-failure.md), now |
| Jobs stay `running` | A worker died mid-job | They requeue by themselves after 30 minutes |

**Requeue a dead job** once the cause is fixed. Every handler is idempotent, so running it again is safe:

```cypher
MATCH (j:Job {id: $id, state: 'dead'}) SET j.state = 'queued', j.attempts = 0, j.run_after_ts = 0;
```

Or enqueue a fresh one: `uniondeb worker enqueue ingest --args '{"sha256": "<hash>"}'`.
