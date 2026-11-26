-- Copyright (c) 2026 gechengfu. SPDX-License-Identifier: MIT
-- Requires two DNs. Everything created here is rolled back at the end.
\set ON_ERROR_STOP on
\pset pager off
SELECT version();
SELECT node_name AS dn_a FROM pgxc_node WHERE node_type='D' ORDER BY node_name LIMIT 1 \gset
SELECT node_name AS dn_b FROM pgxc_node WHERE node_type='D' ORDER BY node_name OFFSET 1 LIMIT 1 \gset
BEGIN;
SELECT age_dist.create_graph('age_course_demo_alice', :'dn_a');
SELECT age_dist.create_graph('age_course_demo_bob', :'dn_b');
SELECT graph_name, data_node FROM age_dist.graph_routes
WHERE graph_name IN ('age_course_demo_alice','age_course_demo_bob') ORDER BY graph_name;
SELECT age_dist.cypher('age_course_demo_alice', $$
CREATE (:Memory {key:"preference", value:"green tea"})
  -[:SUPPORTS]->(:Memory {key:"habit", value:"morning"})
  -[:SUPPORTS]->(:Memory {key:"goal", value:"health"})
$$);
SELECT age_dist.cypher('age_course_demo_bob', $$
CREATE (n:Memory {key:"preference", value:"coffee"}) RETURN n.value
$$);
SELECT age_dist.cypher('age_course_demo_alice', $$
MATCH (:Memory {key:"preference"})-[:SUPPORTS*1..2]->(m)
RETURN m.key, m.value ORDER BY m.key
$$, 2);
SELECT age_dist.cypher('age_course_demo_bob', $$MATCH (n:Memory) RETURN count(n)$$);
SELECT format('EXECUTE DIRECT ON (%I) %L', :'dn_a',
  'SELECT name FROM ag_catalog.ag_graph WHERE name IN (''age_course_demo_alice'',''age_course_demo_bob'') ORDER BY name') \gexec
SELECT format('EXECUTE DIRECT ON (%I) %L', :'dn_b',
  'SELECT name FROM ag_catalog.ag_graph WHERE name IN (''age_course_demo_alice'',''age_course_demo_bob'') ORDER BY name') \gexec
ROLLBACK;
SELECT count(*) AS remaining_demo_routes FROM age_dist.graph_routes
WHERE graph_name IN ('age_course_demo_alice','age_course_demo_bob');
