# OpenTenBase memory governance

Run the SQL once against an OpenTenBase coordinator with a configured default
sharding group and PL/pgSQL enabled in the target database. The deployment
role creates and owns the `memory_governance`
schema and its objects. This is a standalone schema for the OpenTenBase
deployment. Import existing SQLite data separately and map `categories` to
`category`. All three tables use `DISTRIBUTE BY SHARD(user_id)` and include
`user_id` in their primary keys. [OpenTenBase sharded tables](https://docs.opentenbase.org/en/guide/03-basic-use/)

```sh
psql -h COORDINATOR -U GOVERNANCE_OWNER -d DATABASE \
  -v ON_ERROR_STOP=1 -f sql/opentenbase_governance.sql
```

## Audit access

Every row inserted, updated, or deleted in `memories` produces a corresponding
entry in the same transaction. The entry captures the session login, event
time, and available before and after snapshots. Rolled-back changes roll back
their audit entries as well.

Keep the schema and functions under a trusted owner. Route application
connections through coordinators, where OpenTenBase runs sharded-table
triggers. Grant application roles
schema `USAGE` and the required DML and sequence privileges on `memories`.
Reserve `memory_audit_log` reads for authorized roles because snapshots contain
memory content. Keep direct audit writes, sequence access, and `TRUNCATE` on
`memories` restricted to the trusted owner. The audit trigger appends with
owner privileges, and guard triggers reject updates, deletes, and truncation
of the audit log. The protection applies to application SQL. Table owners and
superusers can change or disable the triggers.

## Archive by age

```sql
SELECT memory_governance.archive_decayed_memories();
SELECT memory_governance.archive_decayed_memories(30);
```

The function moves rows whose `updated_at` is strictly older than the
threshold, measured in 24-hour days from statement start. The default is 90
days. Set `updated_at` when writing a new version of a memory to restart its
decay period. The function returns the number moved. Archiving preserves the
original row and adds an archive timestamp. The live delete, its audit entry,
and the archive insert commit together. Grant function `EXECUTE` only to roles
authorized to archive across all users.

## Review compression candidates

```sql
SELECT id, category, memory, updated_at, recency_rank, category_count
FROM memory_governance.v_memory_compression_candidates
WHERE user_id = 'alice'
ORDER BY category, recency_rank;
```

The view ranks live memories within each user and category by most recently
updated first, breaking ties by descending ID. It lists ranks 2 and higher
and includes the full group count. Category and recency provide a heuristic
for review. The view performs no content comparison or writes.
