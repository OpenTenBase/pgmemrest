import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


SERVER = Path(__file__).resolve().parents[1] / "server.py"
DIET = "alice 是素食者，平时不吃肉，喜欢吃蔬菜和豆腐"
MOVIES = "alice 喜欢看科幻电影，不喜欢惊悚片"
NEW_DIET = "alice 开始吃鸡胸肉"
RELATED_MOVIES = "周末想重温经典科幻作品"
NEW_FACT = "Collects antique telescopes"

# Bind port 0 in the child and report the actual port after SQLite is ready.
BOOTSTRAP = """
import runpy
import sys

server = runpy.run_path(sys.argv[1])
server["reset_db"]()
with server["ThreadingHTTPServer"](
    (server["HOST"], server["PORT"]), server["Handler"]
) as httpd:
    print(httpd.server_port, flush=True)
    httpd.serve_forever()
"""


class ServerTests(unittest.TestCase):
    def start_server(self, api_key=""):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db_path = str(Path(directory) / "memories.db")
        env = dict(os.environ, MEM_DB_PATH=db_path, MEM_HOST="127.0.0.1",
                   MEM_PORT="0", MEM_API_KEY=api_key, EMBED_DIM="256",
                   EMBED_BASE_URL="", EMBED_API_KEY="", EMBED_MODEL="")
        process = subprocess.Popen(
            [sys.executable, "-c", BOOTSTRAP, str(SERVER)],
            env=env, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_server, process)
        port = process.stdout.readline().strip()
        self.assertTrue(port.isdecimal(), f"Server failed to report its port: {port!r}")
        self.base_url = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}))
        return db_path

    @staticmethod
    def stop_server(process):
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            process.stdout.close()

    def request(self, method, path, body=None, *, status=200, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            self.assertEqual(response.code, status, payload)
        return payload

    def memories(self, user_id="alice"):
        return self.request("POST", "/v2/memories", {"user_id": user_id})["results"]

    def write(self, fact, user_id="alice", **options):
        response = self.request("POST", "/v1/memories", {
            "user_id": user_id, "messages": [{"content": fact}], **options,
        })
        result, = response["results"]
        return result

    def test_ping(self):
        db_path = self.start_server()
        self.assertEqual(self.request("GET", "/v1/ping"), {
            "status": "ok", "backend": "sqlite", "db": db_path,
        })

    def test_reset_and_list_initial_memories(self):
        self.start_server()
        self.assertEqual(self.write(NEW_FACT)["event"], "ADD")
        self.assertEqual(self.request("POST", "/v1/reset"), {
            "status": "reset", "seeded": 2,
        })
        self.assertEqual(
            [(row["user_id"], row["memory"], row["categories"]) for row in self.memories()],
            [("alice", MOVIES, "semantic"), ("alice", DIET, "semantic")],
        )
        self.assertEqual(self.memories("bob"), [])
        self.assertEqual(self.request("GET", "/v1/history")["results"], [])

    def test_write_decisions(self):
        self.start_server()
        initial = {row["memory"]: row["id"] for row in self.memories()}

        added = self.write(NEW_FACT)
        self.assertEqual(added["event"], "ADD")
        duplicate = self.write(NEW_FACT)
        self.assertEqual(duplicate["event"], "NOOP")
        self.assertEqual(duplicate["duplicated_with"], added["memory_id"])
        self.assertEqual(len(self.memories()), 3)

        deleted = self.write(NEW_DIET)
        self.assertEqual(deleted["event"], "DELETE")
        self.assertEqual(deleted["deleted_memory"], DIET)
        self.request("GET", f"/v1/memories/{initial[DIET]}", status=404)

        updated = self.write(RELATED_MOVIES)
        self.assertEqual(updated["event"], "UPDATE")
        self.assertEqual(updated["memory_id"], initial[MOVIES])
        self.assertEqual(updated["old_memory"], MOVIES)
        self.assertEqual(
            {row["id"]: row["memory"] for row in self.memories()},
            {added["memory_id"]: NEW_FACT, deleted["new_memory_id"]: NEW_DIET,
             initial[MOVIES]: f"{MOVIES}；{RELATED_MOVIES}"},
        )

    def test_search_composite_scoring(self):
        self.start_server()
        fresh = self.write("alice astronomy telescope observatory", category="episodic")
        self.write(MOVIES, user_id="bob")
        body = {"user_id": "alice", "query": MOVIES}
        results = self.request("POST", "/v2/memories/search", body)["results"]
        self.assertEqual({row["memory"] for row in results}, {
            MOVIES, DIET, fresh["memory"],
        })
        scores = [row["final_score"] for row in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

        by_memory = {row["memory"]: row for row in results}
        for fact, weight, age in [(MOVIES, 0.6, 60), (DIET, 0.6, 90),
                                  (fresh["memory"], 0.3, 0)]:
            with self.subTest(fact=fact):
                row = by_memory[fact]
                self.assertEqual(row["category_weight"], weight)
                self.assertAlmostEqual(row["recency"], math.exp(-0.01 * age), delta=0.0001)
                expected = row["similarity"] * weight * row["recency"]
                self.assertAlmostEqual(row["final_score"], expected, delta=0.0001)
        self.assertEqual(results[0]["memory"], MOVIES)
        self.assertEqual(results[0]["similarity"], 1.0)
        limited = self.request("POST", "/v2/memories/search", {**body, "limit": 1})
        self.assertEqual([row["id"] for row in limited["results"]], [results[0]["id"]])

    def test_get_delete_and_user_isolation(self):
        self.start_server()
        alice_memory = self.memories()[0]
        bob_memory = self.write(NEW_FACT, user_id="bob")
        path = f'/v1/memories/{alice_memory["id"]}'

        self.assertEqual(self.request("GET", path), alice_memory)
        self.assertEqual([row["id"] for row in self.memories("bob")],
                         [bob_memory["memory_id"]])
        self.assertEqual(self.request("DELETE", path, {"user_id": "bob"}, status=403),
                         {"error": "not your memory"})
        self.assertEqual(self.request("GET", path), alice_memory)
        self.assertEqual(self.request("DELETE", path, {"user_id": "alice"}),
                         {"status": "deleted", "id": alice_memory["id"]})
        self.assertEqual(self.request("GET", path, status=404), {"error": "not found"})
        self.request("DELETE", path, {"user_id": "alice"}, status=404)
        self.assertEqual([row["id"] for row in self.memories("bob")],
                         [bob_memory["memory_id"]])

    def test_history_audit_trail(self):
        self.start_server()
        initial = {row["memory"]: row["id"] for row in self.memories()}
        added = self.write(NEW_FACT)
        self.write(NEW_FACT)
        replacement = self.write(NEW_DIET)
        self.write(RELATED_MOVIES)
        self.request("DELETE", f'/v1/memories/{added["memory_id"]}', {"user_id": "alice"})

        rows = self.request("GET", "/v1/history")["results"]
        self.assertEqual(
            [(row["operation"], row["memory_id"], row["old_value"], row["new_value"])
             for row in rows],
            [
                ("DELETE", added["memory_id"], NEW_FACT, None),
                ("UPDATE", initial[MOVIES], MOVIES, f"{MOVIES}；{RELATED_MOVIES}"),
                ("ADD", replacement["new_memory_id"], None, NEW_DIET),
                ("DELETE", initial[DIET], DIET, None),
                ("NOOP", None, NEW_FACT, NEW_FACT),
                ("ADD", added["memory_id"], None, NEW_FACT),
            ],
        )
        for row in rows:
            self.assertEqual(row["user_id"], "alice")
            self.assertTrue(row["detail"])
            self.assertTrue(row["occurred_at"])

    def test_api_key_authentication(self):
        key = "integration-test-key"
        self.start_server(api_key=key)
        for token in (None, "wrong-key"):
            with self.subTest(token=token):
                self.assertEqual(
                    self.request("GET", "/v1/ping", token=token, status=401),
                    {"error": "invalid api key"},
                )
        self.assertEqual(self.request("GET", "/v1/ping", token=key)["status"], "ok")
