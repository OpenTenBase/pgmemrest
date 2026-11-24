-- Run against a fresh database after install.sql, with permission to create NOLOGIN roles.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
CREATE ROLE pgmemrest_writer NOLOGIN;
CREATE ROLE pgmemrest_maintainer NOLOGIN;
CREATE ROLE pgmemrest_auditor NOLOGIN;
\ir grants.sql

-- Backdate synthetic inputs to exercise the retention boundary without waiting.
INSERT INTO memory_governance.memories
    (user_id, fact_key, category, memory_text, created_at, updated_at)
SELECT 'alice', x.fact_key, 'preferences', x.memory_text,
    now() - x.age, now() - x.age
FROM (VALUES
    ('expired', 'old preference', interval '63 days'),
    ('boundary', 'boundary preference', interval '1440 hours'),
    ('not-due', 'current preference', interval '59 days'),
    ('alias-a', 'reads data sheets', interval '2 days'),
    ('alias-b', 'reads data sheets', interval '1 day'),
    ('alias-c', 'reads data sheets', interval '1 day')
) AS x(fact_key, memory_text, age);
INSERT INTO memory_governance.memories (user_id, fact_key, category, memory_text)
VALUES ('bob', 'alias-a', 'preferences', 'reads data sheets'),
       ('alice', 'different-category', 'routine', 'reads data sheets');

SET LOCAL ROLE pgmemrest_writer;
DO $$
DECLARE
    result text;
    original_stamp timestamptz;
BEGIN
    INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
    VALUES ('alice', ' Tool ', 'WORK', 'Uses simulator A', 'assert') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'ADD' THEN RAISE EXCEPTION 'Expected ADD'; END IF;
    SELECT updated_at INTO original_stamp FROM memory_governance.active_memories
        WHERE user_id = 'alice' AND fact_key = 'tool';
    IF original_stamp IS NULL THEN RAISE EXCEPTION 'Missing active ADD'; END IF;

    INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
    VALUES ('alice', 'tool', 'work', ' Uses   simulator A ', 'assert') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'NOOP' OR original_stamp IS DISTINCT FROM (
        SELECT updated_at FROM memory_governance.active_memories
        WHERE user_id = 'alice' AND fact_key = 'tool'
    ) THEN RAISE EXCEPTION 'NOOP must preserve the timestamp'; END IF;

    INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
    VALUES ('alice', 'tool', 'work', 'Uses simulator B', 'assert') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'UPDATE' OR NOT EXISTS (
        SELECT 1 FROM memory_governance.active_memories
        WHERE user_id = 'alice' AND fact_key = 'tool' AND memory_text = 'Uses simulator B'
    ) THEN RAISE EXCEPTION 'Expected UPDATE of the same fact'; END IF;

    INSERT INTO memory_governance.inbox (user_id, fact_key, intent)
    VALUES ('alice', 'tool', 'retract') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'DELETE' OR EXISTS (
        SELECT 1 FROM memory_governance.active_memories WHERE user_id = 'alice' AND fact_key = 'tool'
    ) THEN RAISE EXCEPTION 'Expected logical DELETE'; END IF;

    INSERT INTO memory_governance.inbox (user_id, fact_key, intent)
    VALUES ('alice', 'tool', 'retract') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'NOOP' THEN RAISE EXCEPTION 'Repeated retract must be NOOP'; END IF;

    INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
    VALUES ('alice', 'tool', 'work', 'Uses simulator C', 'assert') RETURNING decision INTO result;
    IF result IS DISTINCT FROM 'ADD' THEN RAISE EXCEPTION 'Expected reactivation'; END IF;

    BEGIN
        INSERT INTO memory_governance.inbox (user_id, fact_key, intent) VALUES ('alice', 'bad', NULL);
        RAISE EXCEPTION 'Invalid input was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;

    BEGIN
        UPDATE memory_governance.memories SET memory_text = 'attempt' WHERE user_id = 'alice';
        RAISE EXCEPTION 'Writer could bypass the intake';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        TRUNCATE memory_governance.audit_log;
        RAISE EXCEPTION 'Writer could erase the audit log';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        ALTER TABLE memory_governance.memories DISABLE TRIGGER audit_memory_change;
        RAISE EXCEPTION 'Writer could disable the audit trigger';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO memory_governance.inbox (decision) VALUES ('ADD');
        RAISE EXCEPTION 'Writer could forge a decision receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM memory_governance.archive_stale(1);
        RAISE EXCEPTION 'Writer could schedule maintenance';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;
RESET ROLE;
\echo PASS write decisions, unchanged timestamp, input validation and writer permissions

SAVEPOINT rollback_write;
SET LOCAL ROLE pgmemrest_writer;
INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
VALUES ('alice', 'rollback', 'work', 'Rollback sample', 'assert');
ROLLBACK TO SAVEPOINT rollback_write;

SET LOCAL ROLE pgmemrest_maintainer;
DO $$
BEGIN
    IF memory_governance.archive_stale(1) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'First retention batch must archive one row';
    END IF;
    IF memory_governance.archive_stale(1) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'Second retention batch must archive the boundary row';
    END IF;
    IF memory_governance.archive_stale() IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION 'Archive sweep must be idempotent';
    END IF;
    IF memory_governance.compress_user('alice') IS DISTINCT FROM 2 THEN
        RAISE EXCEPTION 'Expected two duplicate rows';
    END IF;
    IF memory_governance.compress_user('alice') IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION 'Compression must be idempotent';
    END IF;
    BEGIN
        DELETE FROM memory_governance.audit_log;
        RAISE EXCEPTION 'Maintainer could delete the audit log';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;
RESET ROLE;
\echo PASS batch limit, score boundary, deterministic compression and repeat runs

DO $$
BEGIN
    IF (SELECT count(*) FROM memory_governance.inbox) <> 6 OR EXISTS (
        SELECT 1 FROM memory_governance.inbox
        WHERE user_id IS NOT NULL OR fact_key IS NOT NULL OR category IS NOT NULL
           OR memory_text IS NOT NULL OR intent IS NOT NULL
    ) THEN RAISE EXCEPTION 'Unexpected persisted receipt data'; END IF;

    IF NOT EXISTS (SELECT 1 FROM memory_governance.active_memories
        WHERE user_id = 'alice' AND fact_key = 'not-due' AND retention_score > 0.25)
        OR NOT EXISTS (SELECT 1 FROM memory_governance.active_memories
            WHERE user_id = 'alice' AND fact_key = 'alias-b')
        OR NOT EXISTS (SELECT 1 FROM memory_governance.active_memories
            WHERE user_id = 'alice' AND fact_key = 'different-category')
        OR NOT EXISTS (SELECT 1 FROM memory_governance.active_memories
            WHERE user_id = 'bob' AND fact_key = 'alias-a')
        OR EXISTS (SELECT 1 FROM memory_governance.active_memories
            WHERE user_id = 'alice' AND fact_key IN ('expired', 'boundary', 'alias-a', 'alias-c', 'rollback')) THEN
        RAISE EXCEPTION 'Unexpected active rows after governance';
    END IF;
END;
$$;

SET LOCAL ROLE pgmemrest_auditor;
DO $$
BEGIN
    IF (SELECT count(*) FROM memory_governance.audit_log WHERE action = 'ARCHIVE') <> 2
        OR (SELECT count(*) FROM memory_governance.audit_log WHERE action = 'COMPRESS') <> 2
        OR (SELECT count(*) FROM memory_governance.audit_log WHERE fact_key = 'tool') <> 6
        OR (SELECT count(*) FROM memory_governance.audit_log WHERE fact_key = 'tool' AND action = 'NOOP') <> 2
        OR (SELECT count(*) FROM memory_governance.audit_log WHERE fact_key = 'tool' AND action = 'DELETE') <> 1
        OR EXISTS (SELECT 1 FROM memory_governance.audit_log WHERE fact_key IN ('bad', 'rollback')) THEN
        RAISE EXCEPTION 'Audit events do not match committed behavior';
    END IF;
    BEGIN
        UPDATE memory_governance.audit_log SET reason = 'attempt';
        RAISE EXCEPTION 'Auditor could modify logs';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;
SELECT action, count(*) AS events FROM memory_governance.audit_log GROUP BY action ORDER BY action;
RESET ROLE;
\echo PASS audit access, compressed-row retention and transactional rollback

ROLLBACK;
