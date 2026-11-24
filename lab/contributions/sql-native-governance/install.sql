BEGIN;

CREATE SCHEMA memory_governance;
REVOKE ALL ON SCHEMA memory_governance FROM PUBLIC;

CREATE TABLE memory_governance.memories (
    user_id text NOT NULL,
    fact_key text NOT NULL,
    category text NOT NULL,
    memory_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    archived_at timestamptz,
    archive_reason text,
    PRIMARY KEY (user_id, fact_key),
    CHECK ((archived_at IS NULL AND archive_reason IS NULL)
        OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL
            AND archive_reason IN ('retracted', 'age', 'duplicate')))
);

CREATE INDEX memories_active_by_age
    ON memory_governance.memories (updated_at, user_id, fact_key)
    WHERE archived_at IS NULL;

CREATE TABLE memory_governance.audit_log (
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor name NOT NULL DEFAULT session_user,
    user_id text NOT NULL,
    fact_key text NOT NULL,
    action text NOT NULL CHECK (action IN ('ADD', 'UPDATE', 'DELETE', 'NOOP', 'ARCHIVE', 'COMPRESS')),
    reason text
);

CREATE FUNCTION memory_governance.audit_change()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    decision text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO memory_governance.audit_log (user_id, fact_key, action, reason)
        VALUES (OLD.user_id, OLD.fact_key, 'DELETE', 'physical_delete');
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        decision := 'ADD';
    ELSIF NEW.archived_at IS NOT NULL AND OLD.archived_at IS NULL THEN
        decision := CASE NEW.archive_reason
            WHEN 'retracted' THEN 'DELETE'
            WHEN 'age' THEN 'ARCHIVE'
            WHEN 'duplicate' THEN 'COMPRESS'
        END;
    ELSIF OLD.archived_at IS NOT NULL AND NEW.archived_at IS NULL THEN
        decision := 'ADD';
    ELSE
        decision := 'UPDATE';
    END IF;
    INSERT INTO memory_governance.audit_log (user_id, fact_key, action, reason)
    VALUES (NEW.user_id, NEW.fact_key, decision, NEW.archive_reason);
    RETURN NULL;
END;
$$;

CREATE TRIGGER audit_memory_change
    AFTER INSERT OR UPDATE OR DELETE ON memory_governance.memories
    FOR EACH ROW EXECUTE PROCEDURE memory_governance.audit_change();

CREATE TABLE memory_governance.inbox (
    user_id text,
    fact_key text,
    category text,
    memory_text text,
    intent text,
    decision text NOT NULL CHECK (decision IN ('ADD', 'UPDATE', 'DELETE', 'NOOP')),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (user_id IS NULL AND fact_key IS NULL AND category IS NULL
        AND memory_text IS NULL AND intent IS NULL)
);

CREATE FUNCTION memory_governance.decide_write()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    previous memory_governance.memories%ROWTYPE;
    observed_at timestamptz;
BEGIN
    NEW.user_id := btrim(NEW.user_id);
    NEW.fact_key := lower(btrim(NEW.fact_key));
    NEW.category := lower(btrim(NEW.category));
    NEW.memory_text := btrim(regexp_replace(NEW.memory_text, '[[:space:]]+', ' ', 'g'));
    IF coalesce(char_length(NEW.user_id), 0) NOT BETWEEN 1 AND 128
        OR coalesce(char_length(NEW.fact_key), 0) NOT BETWEEN 1 AND 128
        OR (NEW.intent IS DISTINCT FROM 'assert' AND NEW.intent IS DISTINCT FROM 'retract')
        OR (NEW.intent = 'assert' AND
            (coalesce(char_length(NEW.category), 0) NOT BETWEEN 1 AND 64
             OR coalesce(char_length(NEW.memory_text), 0) NOT BETWEEN 1 AND 4000)) THEN
        RAISE EXCEPTION 'Invalid memory write' USING ERRCODE = '22023';
    END IF;

    -- Serialize writes and compression for this user on the same database server.
    PERFORM pg_advisory_xact_lock(81523819, hashtext(NEW.user_id));
    SELECT m.* INTO previous FROM memory_governance.memories AS m
    WHERE m.user_id = NEW.user_id AND m.fact_key = NEW.fact_key
    FOR UPDATE;
    observed_at := clock_timestamp();

    IF NEW.intent = 'retract' THEN
        IF NOT FOUND OR previous.archived_at IS NOT NULL THEN
            NEW.decision := 'NOOP';
        ELSE
            UPDATE memory_governance.memories AS m
            SET archived_at = observed_at, archive_reason = 'retracted'
            WHERE m.user_id = NEW.user_id AND m.fact_key = NEW.fact_key;
            NEW.decision := 'DELETE';
        END IF;
    ELSIF NOT FOUND THEN
        INSERT INTO memory_governance.memories
            (user_id, fact_key, category, memory_text, created_at, updated_at)
        VALUES (NEW.user_id, NEW.fact_key, NEW.category, NEW.memory_text, observed_at, observed_at);
        NEW.decision := 'ADD';
    ELSIF previous.archived_at IS NULL
        AND previous.category = NEW.category AND previous.memory_text = NEW.memory_text THEN
        NEW.decision := 'NOOP';
    ELSE
        UPDATE memory_governance.memories AS m
        SET category = NEW.category, memory_text = NEW.memory_text, updated_at = observed_at,
            archived_at = NULL, archive_reason = NULL
        WHERE m.user_id = NEW.user_id AND m.fact_key = NEW.fact_key;
        NEW.decision := CASE WHEN previous.archived_at IS NULL THEN 'UPDATE' ELSE 'ADD' END;
    END IF;

    IF NEW.decision = 'NOOP' THEN
        INSERT INTO memory_governance.audit_log (user_id, fact_key, action, reason)
        VALUES (NEW.user_id, NEW.fact_key, 'NOOP',
            CASE WHEN NEW.intent = 'retract' THEN 'absent_or_archived' ELSE 'unchanged' END);
    END IF;
    -- The persisted receipt carries the decision only. The audit log owns identity.
    NEW.user_id := NULL;
    NEW.fact_key := NULL;
    NEW.category := NULL;
    NEW.memory_text := NULL;
    NEW.intent := NULL;
    RETURN NEW;
END;
$$;

CREATE TRIGGER decide_memory_write
    BEFORE INSERT ON memory_governance.inbox
    FOR EACH ROW EXECUTE PROCEDURE memory_governance.decide_write();

CREATE VIEW memory_governance.active_memories AS
SELECT user_id, fact_key, category, memory_text, created_at, updated_at,
    power(0.5::double precision,
          greatest(0, extract(epoch FROM statement_timestamp() - updated_at)) / 2592000.0)
          AS retention_score
FROM memory_governance.memories
WHERE archived_at IS NULL;

CREATE FUNCTION memory_governance.archive_stale(p_limit integer DEFAULT 500)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    changed integer;
    sweep_at timestamptz := statement_timestamp();
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'Batch size must be between 1 and 10000' USING ERRCODE = '22023';
    END IF;
    WITH stale AS (
        SELECT user_id, fact_key
        FROM memory_governance.memories
        WHERE archived_at IS NULL AND updated_at <= sweep_at - interval '1440 hours'
        ORDER BY updated_at, user_id, fact_key
        LIMIT p_limit FOR UPDATE SKIP LOCKED
    )
    UPDATE memory_governance.memories AS m
    SET archived_at = sweep_at, archive_reason = 'age'
    FROM stale AS s
    WHERE m.user_id = s.user_id AND m.fact_key = s.fact_key;
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed;
END;
$$;

CREATE FUNCTION memory_governance.compress_user(p_user_id text)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    changed integer;
BEGIN
    IF coalesce(char_length(btrim(p_user_id)), 0) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'Invalid user id' USING ERRCODE = '22023';
    END IF;
    p_user_id := btrim(p_user_id);
    PERFORM pg_advisory_xact_lock(81523819, hashtext(p_user_id));

    WITH ranked AS (
        SELECT user_id, fact_key,
            row_number() OVER (
                PARTITION BY user_id, category, memory_text
                ORDER BY updated_at DESC, fact_key
            ) AS preference
        FROM memory_governance.memories
        WHERE user_id = p_user_id AND archived_at IS NULL
    )
    UPDATE memory_governance.memories AS m
    SET archived_at = clock_timestamp(), archive_reason = 'duplicate'
    FROM ranked AS r
    WHERE m.user_id = r.user_id AND m.fact_key = r.fact_key
        AND r.preference > 1 AND m.archived_at IS NULL;
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed;
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA memory_governance FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA memory_governance FROM PUBLIC;

COMMIT;
