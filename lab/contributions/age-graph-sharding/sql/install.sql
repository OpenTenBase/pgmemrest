-- Copyright (c) 2026 gechengfu. SPDX-License-Identifier: MIT
-- Run on the CN in a dedicated database AFTER installing the patched AGE
-- shared library and extension files on every CN/DN host.
\set ON_ERROR_STOP on
BEGIN;
CREATE EXTENSION IF NOT EXISTS age;
CREATE SCHEMA age_dist;
REVOKE ALL ON SCHEMA age_dist FROM PUBLIC;

CREATE OR REPLACE FUNCTION ag_catalog._age_local_nextval(regclass)
RETURNS bigint AS '$libdir/age', 'age_otb_nextval'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE;
CREATE OR REPLACE FUNCTION ag_catalog._age_otb_worker(text,text,text,integer)
RETURNS text AS '$libdir/age', 'age_otb_worker'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE
SET search_path = ag_catalog, pg_catalog;
CREATE OR REPLACE FUNCTION ag_catalog._age_otb_dispatch(text,text,text,text,integer)
RETURNS text AS '$libdir/age', 'age_otb_dispatch'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE;
REVOKE ALL ON FUNCTION ag_catalog._age_local_nextval(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION ag_catalog._age_otb_worker(text,text,text,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ag_catalog._age_otb_dispatch(text,text,text,text,integer) FROM PUBLIC;

-- The mapping is persisted: adding a DN must not silently move existing graphs.
CREATE TABLE age_dist.graph_routes (
    graph_name text PRIMARY KEY CHECK (octet_length(graph_name) BETWEEN 1 AND 63),
    data_node name NOT NULL,
    created_at timestamptz NOT NULL DEFAULT current_timestamp
) DISTRIBUTE BY REPLICATION;
REVOKE ALL ON age_dist.graph_routes FROM PUBLIC;

CREATE FUNCTION age_dist.create_graph(graph_name text, data_node name DEFAULT NULL)
RETURNS name LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, age_dist
AS $function$
DECLARE
    target name := data_node;
    nodes name[];
    slot bigint;
BEGIN
    IF graph_name IS NULL THEN
        RAISE EXCEPTION 'graph name cannot be NULL' USING ERRCODE = '22004';
    END IF;
    IF graph_name !~ '^[A-Za-z_][A-Za-z_0-9]*$'
       OR octet_length(graph_name) > 63
       OR graph_name ~ '^(pg_|ag_|age_dist$|public$|information_schema$)' THEN
        RAISE EXCEPTION 'invalid or reserved graph name' USING ERRCODE = '22023';
    END IF;
    IF target IS NULL THEN
        SELECT array_agg(node_name ORDER BY node_name::text COLLATE "C")
          INTO nodes FROM pg_catalog.pgxc_node WHERE node_type = 'D';
        IF array_length(nodes, 1) IS NULL THEN
            RAISE EXCEPTION 'no data nodes available' USING ERRCODE = '55000';
        END IF;
        slot := ('x' || substr(md5(graph_name), 1, 8))::bit(32)::bigint;
        target := nodes[1 + (slot % array_length(nodes, 1))::integer];
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pgxc_node
                   WHERE node_name = target AND node_type = 'D') THEN
        RAISE EXCEPTION 'unknown data node: %', target USING ERRCODE = '22023';
    END IF;
    -- Lock the logical route before replicated writes. Concurrent inserts of
    -- the same key can otherwise lock different replicas in opposite order.
    PERFORM pg_catalog.pg_advisory_xact_lock(4278084, hashtext(graph_name));
    -- Insert first; a failed DN create rolls this registration back.
    INSERT INTO age_dist.graph_routes VALUES (graph_name, target, current_timestamp);
    PERFORM ag_catalog._age_otb_dispatch(target::text, 'create', graph_name, '', 1);
    RETURN target;
END
$function$;

CREATE FUNCTION age_dist.cypher(graph_name text, query text, columns integer DEFAULT 1)
RETURNS jsonb LANGUAGE plpgsql STRICT VOLATILE
SET search_path = pg_catalog, age_dist
AS $function$
DECLARE target name;
BEGIN
    -- OpenTenBase's CN advisory locks guard logical routes. Keep the shared
    -- route lock until commit; the worker serializes operations on its DN.
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(4278084, hashtext(graph_name));
    SELECT data_node INTO target FROM age_dist.graph_routes r
      WHERE r.graph_name = cypher.graph_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'graph is not registered: %', graph_name USING ERRCODE = '3F000';
    END IF;
    RETURN ag_catalog._age_otb_dispatch(target::text, 'cypher', graph_name, query, columns)::jsonb;
END
$function$;

CREATE FUNCTION age_dist.drop_graph(graph_name text)
RETURNS void LANGUAGE plpgsql STRICT VOLATILE
SET search_path = pg_catalog, age_dist
AS $function$
DECLARE target name;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(4278084, hashtext(graph_name));
    SELECT data_node INTO target FROM age_dist.graph_routes r
      WHERE r.graph_name = drop_graph.graph_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'graph is not registered: %', graph_name USING ERRCODE = '3F000';
    END IF;
    PERFORM ag_catalog._age_otb_dispatch(target::text, 'drop', graph_name, '', 1);
    DELETE FROM age_dist.graph_routes r WHERE r.graph_name = drop_graph.graph_name;
END
$function$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA age_dist FROM PUBLIC;
COMMENT ON SCHEMA age_dist IS 'Experimental Apache AGE graph-level sharding; superuser lab use only';
COMMIT;
