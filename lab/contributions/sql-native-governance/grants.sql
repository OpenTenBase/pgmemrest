-- Run as the schema owner after a database administrator provisions these roles.
GRANT USAGE ON SCHEMA memory_governance TO pgmemrest_writer, pgmemrest_maintainer, pgmemrest_auditor;
GRANT INSERT (user_id, fact_key, category, memory_text, intent), SELECT (decision)
    ON memory_governance.inbox TO pgmemrest_writer;
GRANT SELECT ON memory_governance.active_memories TO pgmemrest_writer;
GRANT EXECUTE ON FUNCTION memory_governance.archive_stale(integer) TO pgmemrest_maintainer;
GRANT EXECUTE ON FUNCTION memory_governance.compress_user(text) TO pgmemrest_maintainer;
GRANT SELECT ON memory_governance.audit_log TO pgmemrest_auditor;
