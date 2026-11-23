BEGIN;

CREATE SCHEMA memory_governance;
REVOKE CREATE ON SCHEMA memory_governance FROM PUBLIC;

CREATE TABLE memory_governance.memories (
    user_id     text NOT NULL,
    id          bigserial NOT NULL,
    agent_id    text NOT NULL DEFAULT 'agent_ou',
    memory      text NOT NULL,
    category    text NOT NULL DEFAULT 'semantic',
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding   jsonb,
    created_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, id)
) DISTRIBUTE BY SHARD(user_id);

CREATE INDEX memories_decay_idx
    ON memory_governance.memories (updated_at);

CREATE TABLE memory_governance.memories_archive (
    user_id      text NOT NULL,
    id           bigint NOT NULL,
    agent_id     text NOT NULL,
    memory       text NOT NULL,
    category     text NOT NULL,
    metadata     jsonb NOT NULL,
    embedding    jsonb,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    archived_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, id)
) DISTRIBUTE BY SHARD(user_id);

CREATE TABLE memory_governance.memory_audit_log (
    user_id      text NOT NULL,
    audit_id     bigserial NOT NULL,
    memory_id    bigint NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    old_row      jsonb,
    new_row      jsonb,
    actor        name NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id, audit_id)
) DISTRIBUTE BY SHARD(user_id);

REVOKE ALL ON memory_governance.memory_audit_log FROM PUBLIC;
REVOKE ALL ON SEQUENCE memory_governance.memory_audit_log_audit_id_seq FROM PUBLIC;

CREATE FUNCTION memory_governance.audit_memory_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO memory_governance.memory_audit_log
            (user_id, memory_id, operation, new_row, actor)
        VALUES (NEW.user_id, NEW.id, TG_OP, to_jsonb(NEW), SESSION_USER);
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO memory_governance.memory_audit_log
            (user_id, memory_id, operation, old_row, new_row, actor)
        VALUES (NEW.user_id, NEW.id, TG_OP, to_jsonb(OLD), to_jsonb(NEW), SESSION_USER);
    ELSE
        INSERT INTO memory_governance.memory_audit_log
            (user_id, memory_id, operation, old_row, actor)
        VALUES (OLD.user_id, OLD.id, TG_OP, to_jsonb(OLD), SESSION_USER);
    END IF;

    RETURN NULL;
END;
$$;

REVOKE EXECUTE ON FUNCTION memory_governance.audit_memory_change() FROM PUBLIC;

CREATE TRIGGER audit_memory_change
AFTER INSERT OR UPDATE OR DELETE ON memory_governance.memories
FOR EACH ROW EXECUTE PROCEDURE memory_governance.audit_memory_change();

CREATE FUNCTION memory_governance.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'memory_audit_log is append-only (% blocked)', TG_OP
        USING ERRCODE = '42501';
END;
$$;

CREATE TRIGGER reject_audit_mutation
BEFORE UPDATE OR DELETE ON memory_governance.memory_audit_log
FOR EACH ROW EXECUTE PROCEDURE memory_governance.reject_audit_mutation();

CREATE TRIGGER reject_audit_truncate
BEFORE TRUNCATE ON memory_governance.memory_audit_log
FOR EACH STATEMENT EXECUTE PROCEDURE memory_governance.reject_audit_mutation();

CREATE FUNCTION memory_governance.archive_decayed_memories(days_threshold integer DEFAULT 90)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    cutoff timestamptz;
    removed memory_governance.memories%ROWTYPE;
    archived_count bigint := 0;
BEGIN
    IF days_threshold IS NULL OR days_threshold < 1 THEN
        RAISE EXCEPTION 'days_threshold must be a positive integer'
            USING ERRCODE = '22023';
    END IF;

    cutoff := statement_timestamp() - days_threshold * interval '24 hours';

    -- Archive only rows actually deleted, using the exact version that was removed.
    FOR removed IN
        DELETE FROM memory_governance.memories
        WHERE updated_at < cutoff
        RETURNING *
    LOOP
        INSERT INTO memory_governance.memories_archive
            (user_id, id, agent_id, memory, category, metadata, embedding, created_at, updated_at)
        VALUES
            (removed.user_id, removed.id, removed.agent_id, removed.memory, removed.category,
             removed.metadata, removed.embedding, removed.created_at, removed.updated_at);

        archived_count := archived_count + 1;
    END LOOP;

    RETURN archived_count;
END;
$$;

REVOKE EXECUTE ON FUNCTION memory_governance.archive_decayed_memories(integer) FROM PUBLIC;

CREATE VIEW memory_governance.v_memory_compression_candidates AS
SELECT user_id, id, agent_id, memory, category, created_at, updated_at,
       recency_rank, category_count
FROM (
    SELECT user_id, id, agent_id, memory, category, created_at, updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, category
               ORDER BY updated_at DESC, id DESC
           ) AS recency_rank,
           COUNT(*) OVER (PARTITION BY user_id, category) AS category_count
    FROM memory_governance.memories
) AS ranked
WHERE recency_rank > 1;

COMMIT;
