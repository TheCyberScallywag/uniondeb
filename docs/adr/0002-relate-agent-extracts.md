# ADR 0002: Relate files for per-agent extracts

- Status: accepted (go, with conditions)
- Date: 2026-11-19
- Step: UND-030
- Evidence: `scripts/spikes/relate_extract.py`, results in `docs/spikes/und-030-relate.json`

## Context

The plan (§3.3) gives each agent a Relate (`relate-db`) extract cut from Aura for the job in hand,
never Aura itself. Relate was eight days old when the plan was written, so the spike had to answer
four questions before Echdynnwr is built: how a subgraph gets from Aura into Relate, how large an
extract can be, whether read-only mode holds, and how the files are encrypted at rest.

The spike ran on `relate-db` 1.1.0 with Python 3.14, on a 2-vCPU, 7 GB container. It loaded a 30-day
synthetic run (55,651 records) into a Relate store standing in for Aura. It then cut a Didolwr-style
content extract (54,660 records, parties pseudonymised with an HMAC) into a fresh `.rl` file.
Finally it opened that file read-only, as an agent would.

## Findings

| Question | Result |
|---|---|
| Aura → Relate pull | **No pull API in the Python package.** `relate.connect(path, namespace, read_only)` is the whole surface. Echdynnwr must read Aura with the official driver under the contract's role and write Relate with batched `UNWIND`. |
| Build rate | ~9,600–11,000 rows/s written into a fresh extract (2,000-row batches). |
| Size | 645 bytes per record on disk, so 35 MB for 54,660 records. A read-only open costs ~114 MB of resident memory, about 3.2× the file size, and takes ~0.9 s. |
| Query speed | A party-level aggregate over the whole extract runs in ~36 ms. |
| Read-only | Enforced. A write on a `read_only=True` connection raises `OperationalError`, and a missing file is not created. |
| Encryption at rest | **None.** Text is visible in the `.rl` bytes. Files are created mode `0600`. |
| Concurrency | Connections are thread-affine (DB-API threadsafety 1). Sharing a connection across threads raises `ProgrammingError`. `RelateRunner` pins each connection to one worker thread. |
| Cypher coverage | Everything the store and extracts need works: constraints (unique, node key, existence), composite indexes, `MERGE … ON CREATE`, `UNWIND $rows`, `FOREACH`, `CALL {}` subqueries, `COUNT {}`, map projection, `properties()`, `datetime()`, and constraint violations as `IntegrityError`. |
| Loader throughput (source side) | 55,651 records in 128 s (~430/s) through the MERGE-heavy loader with provenance. This is fine for a stand-in, but it is not a target for Aura, where the loader will be measured separately. |

## Decision

**Go:** agent extracts are Relate files, built by Echdynnwr, opened read-only by agents. This
holds under the following conditions.

1. **Pull path.** Echdynnwr runs the contract's Cypher against Aura through the scoped service role
   (UND-014), pseudonymises in process, and writes the `.rl` with batched `UNWIND`. Agents never
   hold Aura credentials. If a native Aura pull ships later, it replaces only this step.
2. **Size limits.** The default cap per extract is **250,000 records**, about 160 MB on disk and
   roughly 520 MB resident. Contracts may set a lower cap. Anything larger is split by time window
   or by source file, and the agent processes the batches in turn. The cap is checked before the
   build starts, from a `count()` on the contract query, so an oversized pull fails fast.
3. **Read-only.** Agents open extracts only with `read_only=True`. Echdynnwr is the single writer
   and closes the file before handing over its path and SHA-256.
4. **Encryption at rest.** Relate has none, so extracts live on an **encrypted, per-run volume**: a
   tmpfs (RAM-backed, never on disk) for dev and CI, and a LUKS/dm-crypt or cloud-KMS encrypted
   ephemeral volume in production. Files are `0600`, owned by the worker user. The builder's TTL
   reaper (UND-032) deletes them, and each deletion is recorded as provenance. Extracts are never copied to
   object storage.
5. **Provenance.** Every extract is an `Extract` entity whose build `Activity` USED the contract
   version and GENERATED the file hash, so a tag can be traced to the extract it was made from.

## Fallback

The plan's fallback stands and needs no contract changes. Echdynnwr can emit read-only Parquet
queried with DuckDB, or start a throwaway Neo4j Enterprise container per run. Echdynnwr switches
to it if Relate 1.x shows data loss, crashes under the read-only load, or a contract needs more
than the cap and batching is unacceptable.

## Consequences

- UND-031 (contract registry) adds `max_records` and the pseudonymisation list to each contract.
- UND-032 (extract builder) implements the pull path, the cap check, pseudonymisation, the file
  hash, the encrypted-volume mount, TTL deletion and the fallback backend.
- Re-run the spike on each Relate minor release, and after the first run against real Aura.
