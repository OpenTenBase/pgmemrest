#!/usr/bin/env python3
# Copyright (c) 2026 gechengfu. SPDX-License-Identifier: MIT
"""Integration tests against a real CN + >=2 DNs; no database substitutes.

PSQL may name a local psql executable or an executable transport wrapper.
PGHOST/PGPORT/PGUSER/PGDATABASE are consumed by psql. Never put passwords here.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid

PSQL = os.environ.get("PSQL", "psql")
RECORDS = []


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def run(sql, ok=True):
    result = subprocess.run(
        [PSQL, "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"],
        input="SET statement_timeout='20s';\n" + sql + "\n",
        text=True, capture_output=True, timeout=45,
    )
    RECORDS.append({"sql": sql, "returncode": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr})
    if ok and result.returncode:
        raise AssertionError(result.stderr)
    if not ok and result.returncode == 0:
        raise AssertionError("Expected SQL to fail: " + sql)
    return result.stdout.strip() if ok else result.stderr


def cypher_sql(graph, query, width=1):
    return (f"SELECT age_dist.cypher({literal(graph)}, {literal(query)}, {width});")


def cypher(graph, query, width=1):
    return json.loads(run(cypher_sql(graph, query, width)))


def direct(node, sql):
    return run(f"EXECUTE DIRECT ON ({identifier(node)}) {literal(sql)};")


class ClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prefix = "age_test_" + uuid.uuid4().hex[:10]
        cls.nodes = run("SELECT node_name FROM pgxc_node WHERE node_type='D' ORDER BY node_name;").splitlines()
        if len(cls.nodes) < 2:
            raise RuntimeError("This distributed test requires at least two data nodes")
        print("Actual server:", run("SELECT version();"), flush=True)
        print("Data nodes:", ", ".join(cls.nodes), flush=True)
        cls.a, cls.b = cls.prefix + "_a", cls.prefix + "_b"
        cls.owned = {cls.a, cls.b}
        for graph, node in zip((cls.a, cls.b), cls.nodes):
            run(f"SELECT age_dist.create_graph({literal(graph)}, {literal(node)});")

    @classmethod
    def tearDownClass(cls):
        # Only this test run's explicitly recorded graph names are removed.
        for graph in sorted(cls.owned):
            exists = run(f"SELECT count(*) FROM age_dist.graph_routes WHERE graph_name={literal(graph)};")
            if exists == "1":
                run(f"SELECT age_dist.drop_graph({literal(graph)});")

    def test_01_physical_placement(self):
        for graph, target in ((self.a, self.nodes[0]), (self.b, self.nodes[1])):
            for node in self.nodes:
                count = direct(node, f"SELECT count(*) FROM ag_catalog.ag_graph WHERE name={literal(graph)}")
                self.assertEqual(count, "1" if node == target else "0")

    def test_02_native_create_match_and_isolation(self):
        cypher(self.a, 'CREATE (:Memory {key:"isolated", value:"tea"})')
        self.assertEqual(cypher(self.a, 'MATCH (n:Memory {key:"isolated"}) RETURN n.value'), [{"c0": '"tea"'}])
        self.assertEqual(cypher(self.b, 'MATCH (n) RETURN count(n)'), [{"c0": "0"}])

    def test_03_variable_length_traversal(self):
        cypher(self.a, 'CREATE (:Chain {k:"a"})-[:NEXT]->(:Chain {k:"b"})-[:NEXT]->(:Chain {k:"c"})')
        self.assertEqual(cypher(self.a, 'MATCH (:Chain {k:"a"})-[:NEXT*1..2]->(n) RETURN n.k ORDER BY n.k'),
                         [{"c0": '"b"'}, {"c0": '"c"'}])

    def test_04_set_merge_delete(self):
        for value in ("first", "updated"):
            cypher(self.a, f'MERGE (n:Editable {{k:"same"}}) SET n.v="{value}" RETURN n.v')
        self.assertEqual(cypher(self.a, 'MATCH (n:Editable) RETURN count(n), max(n.v)', 2),
                         [{"c0": "1", "c1": '"updated"'}])
        cypher(self.a, 'MATCH (n:Editable) DELETE n')
        self.assertEqual(cypher(self.a, 'MATCH (n:Editable) RETURN count(n)'), [{"c0": "0"}])

    def test_05_native_vertex_and_path_encoding(self):
        row = cypher(self.a, 'MATCH p=(:Chain)-[:NEXT]->(:Chain) RETURN p LIMIT 1')[0]["c0"]
        self.assertIn("::vertex", row)
        self.assertIn("::edge", row)
        self.assertTrue(row.endswith("::path"))

    def test_06_cross_node_commit(self):
        run("BEGIN;\n" + cypher_sql(self.a, 'CREATE (:Committed {v:1})') + "\n" +
            cypher_sql(self.b, 'CREATE (:Committed {v:2})') + "\nCOMMIT;")
        for graph, value in ((self.a, "1"), (self.b, "2")):
            self.assertEqual(cypher(graph, 'MATCH (n:Committed) RETURN n.v'), [{"c0": value}])

    def test_07_cross_node_rollback(self):
        run("BEGIN;\n" + cypher_sql(self.a, 'CREATE (:Aborted {v:1})') + "\n" +
            cypher_sql(self.b, 'CREATE (:Aborted {v:2})') + "\nROLLBACK;")
        for graph in (self.a, self.b):
            self.assertEqual(cypher(graph, 'MATCH (n:Aborted) RETURN count(n)'), [{"c0": "0"}])

    def test_08_error_rolls_back_both_nodes(self):
        error = run("BEGIN;\n" + cypher_sql(self.a, 'CREATE (:ErrorAbort {v:1})') + "\n" +
                    cypher_sql(self.b, 'CREATE (:ErrorAbort {v:2})') + "\n" +
                    cypher_sql(self.a, 'THIS IS NOT CYPHER') + "\nCOMMIT;", ok=False)
        self.assertIn("ERROR", error)
        for graph in (self.a, self.b):
            self.assertEqual(cypher(graph, 'MATCH (n:ErrorAbort) RETURN count(n)'), [{"c0": "0"}])

    def test_09_graph_create_rollback(self):
        graph = self.prefix + "_rollback"
        self.owned.add(graph)
        run(f"BEGIN; SELECT age_dist.create_graph({literal(graph)}, {literal(self.nodes[1])}); ROLLBACK;")
        self.assertEqual(run(f"SELECT count(*) FROM age_dist.graph_routes WHERE graph_name={literal(graph)};"), "0")
        self.assertEqual(direct(self.nodes[1], f"SELECT count(*) FROM ag_catalog.ag_graph WHERE name={literal(graph)}"), "0")

    def test_10_duplicate_create_keeps_original_mapping(self):
        error = run(f"SELECT age_dist.create_graph({literal(self.a)}, {literal(self.nodes[1])});", ok=False)
        self.assertIn("23505", error)
        self.assertEqual(run(f"SELECT data_node FROM age_dist.graph_routes WHERE graph_name={literal(self.a)};"), self.nodes[0])

    def test_11_automatic_placement_is_persisted(self):
        graph = self.prefix + "_auto"
        self.owned.add(graph)
        node = run(f"SELECT age_dist.create_graph({literal(graph)});")
        self.assertIn(node, self.nodes)
        self.assertEqual(run(f"SELECT data_node FROM age_dist.graph_routes WHERE graph_name={literal(graph)};"), node)
        self.assertEqual(direct(node, f"SELECT count(*) FROM ag_catalog.ag_graph WHERE name={literal(graph)}"), "1")

    def test_12_drop_and_recreate(self):
        graph = self.prefix + "_drop"
        self.owned.add(graph)
        for _ in range(2):
            run(f"SELECT age_dist.create_graph({literal(graph)}, {literal(self.nodes[0])});")
            cypher(graph, 'CREATE (:ToDrop {v:1})')
            run(f"SELECT age_dist.drop_graph({literal(graph)});")
            self.assertEqual(direct(self.nodes[0], f"SELECT count(*) FROM pg_namespace WHERE nspname={literal(graph)}"), "0")

    def test_13_quotes_unicode_and_delimiter_injection(self):
        value = "用户说：it's tea; $age_query_0$ $$ '\"; DROP SCHEMA age_dist CASCADE; --"
        query = "CREATE (n:Quoted {value:" + json.dumps(value, ensure_ascii=False) + "}) RETURN n.value"
        result = cypher(self.a, query)[0]["c0"]
        self.assertEqual(json.loads(result), value)
        self.assertEqual(run("SELECT count(*) FROM pg_namespace WHERE nspname='age_dist';"), "1")

    def test_14_invalid_arguments(self):
        for graph in ("x;DROP SCHEMA age_dist CASCADE", "x" * 64, "pg_bad", "age_dist"):
            self.assertIn("22023", run(f"SELECT age_dist.create_graph({literal(graph)});", ok=False))
        self.assertIn("22004", run("SELECT age_dist.create_graph(NULL);", ok=False))
        self.assertIn("22023", run(f"SELECT age_dist.create_graph({literal(self.prefix + '_bad')}, 'missing_node');", ok=False))
        for width in (0, 33):
            self.assertIn("22023", run(cypher_sql(self.a, 'RETURN 1', width), ok=False))
        self.assertIn("3F000", run(cypher_sql(self.prefix + "_missing", 'RETURN 1'), ok=False))

    def test_15_read_only_transaction_rejected(self):
        self.assertIn("25006", run("BEGIN READ ONLY;\n" + cypher_sql(self.a, 'CREATE (:Forbidden)') + "\nCOMMIT;", ok=False))
        self.assertEqual(cypher(self.a, 'MATCH (n:Forbidden) RETURN count(n)'), [{"c0": "0"}])

    def test_16_non_admin_cannot_use_router(self):
        self.assertIn("42501", run("SET ROLE pg_monitor;\n" + cypher_sql(self.a, 'RETURN 1'), ok=False))

    def test_17_concurrent_writes_allocate_unique_ids(self):
        # Repeat the race on three previously nonexistent labels.
        all_ids = []
        for batch in range(3):
            label = f"Concurrent{batch}"
            def insert(i):
                return cypher(self.a, f'CREATE (n:{label} {{i:{i}}}) RETURN id(n)')[0]["c0"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                ids = list(executor.map(insert, range(12)))
            all_ids.extend(ids)
            self.assertEqual(cypher(self.a, f'MATCH (n:{label}) RETURN count(n)'), [{"c0": "12"}])
        self.assertEqual(len(set(all_ids)), 36)

    def test_18_concurrent_duplicate_graph_creation(self):
        for batch in range(3):
            graph = self.prefix + f"_race{batch}"
            self.owned.add(graph)
            def create(node):
                try:
                    return run(f"SELECT age_dist.create_graph({literal(graph)}, {literal(node)});")
                except AssertionError as error:
                    self.assertIn("23505", str(error))
                    return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, self.nodes[:2]))
            winners = [x for x in results if x is not None]
            self.assertEqual(len(winners), 1)
            for node in self.nodes:
                self.assertEqual(direct(node, f"SELECT count(*) FROM ag_catalog.ag_graph WHERE name={literal(graph)}"),
                                 "1" if node == winners[0] else "0")

    def test_19_native_cn_calls_rejected(self):
        self.assertIn("0A000", run("LOAD 'age'; SELECT ag_catalog.create_graph('unexpected_cn_graph');", ok=False))
        sql = "LOAD 'age'; SELECT * FROM ag_catalog.cypher('ignored', $$RETURN 1$$) AS (v ag_catalog.agtype);"
        self.assertIn("0A000", run(sql, ok=False))

    def test_20_metadata_replicated(self):
        for node in self.nodes:
            count = direct(node, f"SELECT count(*) FROM age_dist.graph_routes WHERE graph_name IN ({literal(self.a)},{literal(self.b)})")
            self.assertEqual(count, "2")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        if os.environ.get("AGE_EVIDENCE"):
            Path(os.environ["AGE_EVIDENCE"]).write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2) + "\n")
