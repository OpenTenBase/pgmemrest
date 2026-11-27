#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向一回归与安全加固测试（纯 Python 标准库，零外部依赖）：
  1. GET / DELETE 跨用户归属隔离与 query/header 解析
  2. 多租户凭据强绑定（MEM_TENANT_TOKENS）与独立管理员密钥（MEM_ADMIN_KEY）隔离测试
  3. 请求验证闭环：显式空 query (?user_id=)、重复参数、unhashable category ([]/{})、全批原子校验防半写入
  4. 出站 Embedding Provider 白名单与服务端密钥防泄漏 + 同维度异构模型标识隔离
  5. 锁外慢速 Embedding 不阻塞其他租户 + 并发合并写入 OCC 重试无丢更新（Lost Update 防护）
  6. 多条矛盾记忆清除 + 中文隔字否定、过去时背景与第三人称转述反例防护
  7. 免认证存活/就绪探针（/healthz, /readyz）信息最小化测试
"""

import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SERVER_PATH = os.path.join(REPO_ROOT, "mem_server", "server.py")


def load_mem_server():
    spec = importlib.util.spec_from_file_location("pgmemrest_mem_server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MemServerHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_memories.db")
        self.server_mod = load_mem_server()
        self.server_mod.DB_PATH = self.db_path
        self.server_mod.API_KEY = ""
        self.server_mod.ADMIN_KEY = ""
        self.server_mod.TENANT_TOKENS_RAW = ""
        self.server_mod.REQUIRE_USER_ID = False
        self.server_mod.ENABLE_RESET = True
        self.server_mod._INITIALIZED_DBS.clear()
        self.server_mod.reset_db()

        self.httpd = self.server_mod.ThreadingHTTPServer(("127.0.0.1", 0), self.server_mod.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmpdir.cleanup()

    def request(self, method, path, body=None, raw_body=None, headers=None):
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if raw_body is not None:
            data = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
        else:
            data = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=hdrs,
            method=method,
        )
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # ── 1. 用户隔离与 query/header 解析 ─────────────────────────────────────
    def test_get_single_memory_enforces_user_isolation(self):
        st, listed = self.request("POST", "/v2/memories", {"user_id": "alice"})
        self.assertEqual(st, 200)
        alice_id = listed["results"][0]["id"]

        self.assertEqual(self.request("GET", f"/v1/memories/{alice_id}?user_id=bob")[0], 403)
        self.assertEqual(self.request("GET", f"/v1/memories/{alice_id}", headers={"X-User-Id": "bob"})[0], 403)
        st_ok, body_ok = self.request("GET", f"/v1/memories/{alice_id}?user_id=alice")
        self.assertEqual(st_ok, 200)
        self.assertEqual(body_ok["user_id"], "alice")

    def test_get_history_filters_by_user_id_and_limit(self):
        self.request("POST", "/v1/memories", {"user_id": "alice", "messages": ["alice 在学 Go 语言"]})
        self.request("POST", "/v1/memories", {"user_id": "bob", "messages": ["bob 喜欢打羽毛球"]})

        st_bob, hist_bob = self.request("GET", "/v1/history?user_id=bob")
        self.assertEqual(st_bob, 200)
        self.assertGreaterEqual(len(hist_bob["results"]), 1)
        self.assertTrue(all(item["user_id"] == "bob" for item in hist_bob["results"]))

        st_charlie, hist_charlie = self.request("GET", "/v1/history?user_id=charlie")
        self.assertEqual(st_charlie, 200)
        self.assertEqual(hist_charlie["results"], [])

        st_lim, hist_lim = self.request("GET", "/v1/history?user_id=alice&limit=1")
        self.assertEqual(st_lim, 200)
        self.assertEqual(len(hist_lim["results"]), 1)

    def test_delete_single_memory_supports_query_and_enforces_isolation(self):
        _, listed = self.request("POST", "/v2/memories", {"user_id": "alice"})
        alice_id = listed["results"][0]["id"]

        self.assertEqual(self.request("DELETE", f"/v1/memories/{alice_id}?user_id=bob")[0], 403)
        self.assertEqual(self.request("GET", f"/v1/memories/{alice_id}?user_id=alice")[0], 200)
        self.assertEqual(self.request("DELETE", f"/v1/memories/{alice_id}?user_id=alice")[0], 200)

    def test_strict_require_user_id_mode(self):
        self.server_mod.REQUIRE_USER_ID = True
        _, listed = self.request("POST", "/v2/memories", {"user_id": "alice"})
        alice_id = listed["results"][0]["id"]

        self.assertEqual(self.request("GET", f"/v1/memories/{alice_id}")[0], 400)
        self.assertEqual(self.request("GET", "/v1/history")[0], 400)
        self.assertEqual(self.request("DELETE", f"/v1/memories/{alice_id}")[0], 400)
        self.assertEqual(self.request("DELETE", "/v1/memories")[0], 400)
        self.assertEqual(self.request("POST", "/v1/memories", {"messages": ["hi"]})[0], 400)
        self.assertEqual(self.request("POST", "/v2/memories", {})[0], 400)
        self.assertEqual(self.request("POST", "/v2/memories/search", {"query": "素食"})[0], 400)
        self.assertEqual(self.request("GET", f"/v1/memories/{alice_id}?user_id=alice")[0], 200)

    # ── 2. 多租户强身份绑定与独立管理员权限 ──────────────────────────────────
    def test_multi_tenant_token_binding_and_admin_privilege(self):
        self.server_mod.TENANT_TOKENS_RAW = json.dumps({"tok-alice": "alice", "tok-bob": "bob"})
        self.server_mod.ADMIN_KEY = "super-admin-key"
        h_alice = {"Authorization": "Token tok-alice"}
        h_bob = {"Authorization": "Bearer tok-bob"}
        h_admin = {"Authorization": "Token super-admin-key"}

        # 未带有效租户 token 拒绝
        self.assertEqual(self.request("POST", "/v2/memories", {})[0], 401)

        # alice 不传 user_id 自动锁定为 alice；伪造 user_id=bob 立即 403
        st_w, w_body = self.request("POST", "/v1/memories", {"messages": ["alice 专属密钥记忆"]}, headers=h_alice)
        self.assertEqual(st_w, 200)
        aid = w_body["results"][0]["id"]
        self.assertEqual(
            self.request("POST", "/v1/memories", {"user_id": "bob", "messages": ["spoof"]}, headers=h_alice)[0],
            403,
        )

        # bob 即使不传 user_id 也无法读/删 alice 的单条记录，历史也只看到 bob 自己
        self.assertEqual(self.request("GET", f"/v1/memories/{aid}", headers=h_bob)[0], 403)
        self.assertEqual(self.request("DELETE", f"/v1/memories/{aid}", headers=h_bob)[0], 403)
        _, bob_hist = self.request("GET", "/v1/history", headers=h_bob)
        self.assertEqual(bob_hist["results"], [])

        # bob 批删只影响 bob 自己，不影响 alice
        self.request("POST", "/v1/memories", {"messages": ["bob 自己的记录"]}, headers=h_bob)
        st_bdel, bdel = self.request("DELETE", "/v1/memories", headers=h_bob)
        self.assertEqual(st_bdel, 200)
        self.assertEqual(bdel["user_id"], "bob")
        self.assertEqual(bdel["count"], 1)
        self.assertGreaterEqual(len(self.request("POST", "/v2/memories", {}, headers=h_alice)[1]["results"]), 3)

        # 租户 token 无权执行 /v1/reset（返回 403），仅 ADMIN_KEY 可执行（返回 200）
        self.assertEqual(self.request("POST", "/v1/reset", headers=h_alice)[0], 403)
        self.assertEqual(self.request("POST", "/v1/reset", headers=h_admin)[0], 200)

    # ── 3. 出站凭据保护、探针分离与异构模型维度隔离 ─────────────────────────
    def test_outbound_credential_protection_and_probes(self):
        self.server_mod.API_KEY = "svc-key"
        self.server_mod.EMBED_BASE_URL = "https://trusted.embed.internal/v1"
        self.server_mod.EMBED_API_KEY = "server-secret-embed-key"
        self.server_mod.EMBED_MODEL = "trusted-v1"
        auth = {"Authorization": "Token svc-key"}

        # 1) 免认证 /healthz 与 /readyz 在启用了 API_KEY 时仍返回 200，且不泄露 db 路径
        st_h, h_body = self.request("GET", "/healthz")
        st_r, r_body = self.request("GET", "/readyz")
        self.assertEqual(st_h, 200)
        self.assertEqual(st_r, 200)
        self.assertNotIn("db", h_body)
        self.assertNotIn("db", r_body)

        # 2) 客户端指定未知外部 X-Embed-Base-Url 试图窃取服务端 EMBED_API_KEY -> 403 且 0 出站流量
        outbound_urls = []
        orig_remote = self.server_mod.remote_embed
        self.server_mod.remote_embed = lambda t, provider_cfg=None: (
            outbound_urls.append(provider_cfg) or [0.1] * self.server_mod.EMBED_DIM
        )
        try:
            st_ssrf, _ = self.request(
                "POST",
                "/v1/memories",
                {"user_id": "alice", "messages": ["敏感记忆内容"]},
                headers={**auth, "X-Embed-Base-Url": "https://attacker.example/v1"},
            )
            self.assertEqual(st_ssrf, 403)
            self.assertEqual(outbound_urls, [])

            # 未知 provider 名 -> 400 且 0 出站流量
            st_unk, _ = self.request(
                "POST",
                "/v1/memories",
                {"user_id": "alice", "messages": ["敏感记忆内容"]},
                headers={**auth, "X-Embed-Provider": "unknown-provider"},
            )
            self.assertEqual(st_unk, 400)
            self.assertEqual(outbound_urls, [])
        finally:
            self.server_mod.remote_embed = orig_remote
            self.server_mod.EMBED_BASE_URL = ""
            self.server_mod.EMBED_API_KEY = ""
            self.server_mod.EMBED_MODEL = ""

    def test_upsert_reuses_stored_embeddings_and_isolates_model_tags(self):
        topics = [
            "喜欢登山和徒步旅行", "正在学习 Kubernetes 容器编排", "每天早上喝美式咖啡",
            "住在北京海淀区", "养了一只橘猫叫布丁", "习惯使用暗色模式写代码",
            "周末经常去国家图书馆看书", "对花生严重过敏", "擅长 Python 和 Go 语言开发",
            "计划明年去新疆自驾游",
        ]
        for t in topics:
            self.server_mod.upsert("bench", t)
        self.assertEqual(len(self.server_mod.list_memories("bench", limit=50)), 10)

        orig_embed = self.server_mod.embed
        calls = []
        self.server_mod.embed = lambda text: (calls.append(text) or orig_embed(text))
        try:
            # ADD 场景：10 条既有记忆仅触发 1 次 embed()
            res_add = self.server_mod.upsert("bench", "最近入手了一台机械键盘")
            self.assertEqual(res_add["event"], "ADD")
            self.assertEqual(len(calls), 1)

            # UPDATE 场景：触发 2 次 embed()（1 次新输入 + 1 次合并文本），不随旧记录数增长
            calls.clear()
            res_upd = self.server_mod.upsert("bench", "下午茶偏好手冲瑰夏咖啡")
            self.assertEqual(res_upd["event"], "UPDATE")
            self.assertEqual(len(calls), 2)
        finally:
            self.server_mod.embed = orig_embed

        # 同维度（256 维）但不同 embed_model 标识：search() 不得把异构模型向量当作同空间计算非零余弦
        conn = self.server_mod.get_db()
        with self.server_mod._immediate_tx(conn):
            conn.execute(
                "INSERT INTO memories(user_id, memory, categories, metadata, embedding, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    "model_iso",
                    "完全不相关的词法内容 xyz",
                    "semantic",
                    json.dumps({"embed_model": "remote:other-vendor:v9"}),
                    json.dumps([1.0] + [0.0] * (self.server_mod.EMBED_DIM - 1)),
                    self.server_mod.now_iso(),
                    self.server_mod.now_iso(),
                ),
            )
        conn.close()
        sr = self.server_mod.search("model_iso", "随便查询", limit=1)
        self.assertEqual(sr[0]["similarity"], 0.0)

    # ── 4. 请求校验闭环与全批原子提交 ────────────────────────────────────────
    def test_blank_query_unhashable_category_and_atomic_batch_validation(self):
        # 1) ?user_id= 显式空白不得静默落入 default，必须返回 400
        self.assertEqual(self.request("GET", "/v1/history?user_id=")[0], 400)
        self.assertEqual(self.request("POST", "/v2/memories?user_id=", {})[0], 400)
        self.assertEqual(self.request("DELETE", "/v1/memories?user_id=")[0], 400)
        # 重复同名参数拒绝
        self.assertEqual(self.request("GET", "/v1/history?user_id=alice&user_id=bob")[0], 400)

        # 2) 数组或字典类型的 category 不得触发 unhashable TypeError 500，必须返回 400
        for bad_cat in (["semantic"], {"k": "v"}, 123, "invalid"):
            st_cat, _ = self.request(
                "POST",
                "/v1/memories",
                {"user_id": "cat_u", "messages": ["测试分类"], "category": bad_cat},
            )
            self.assertEqual(st_cat, 400, f"category={bad_cat!r} should return 400, got {st_cat}")

        # 3) messages 后项非法时整批拒绝（前项绝不落库，记忆表与历史表均保持 0 条！）
        st_batch, _ = self.request(
            "POST",
            "/v1/memories",
            {"user_id": "atomic_u", "messages": ["第一条合法记忆", {"content": 99999}]},
        )
        self.assertEqual(st_batch, 400)
        self.assertEqual(self.server_mod.list_memories("atomic_u"), [])
        self.assertEqual(self.server_mod.history("atomic_u"), [])

        # 4) 合法多条批写在同一事务内演进提交
        st_ok, ok_body = self.request(
            "POST",
            "/v1/memories",
            {"user_id": "atomic_u", "messages": ["atomic_u 喜欢拿铁咖啡", "atomic_u 每天喝手冲咖啡"]},
        )
        self.assertEqual(st_ok, 200)
        self.assertEqual([r["event"] for r in ok_body["results"]], ["ADD", "UPDATE"])
        self.assertEqual(len(self.server_mod.list_memories("atomic_u")), 1)
        self.assertEqual(len(self.server_mod.history("atomic_u")), 2)

    # ── 5. 矛盾清理及主语/转述/过去时反例防护 ────────────────────────────────
    def test_multi_contradiction_and_context_counterexamples(self):
        conn = self.server_mod.get_db()
        with self.server_mod._immediate_tx(conn):
            for txt in ("我是严格素食主义者", "平时坚持素食习惯"):
                conn.execute(
                    "INSERT INTO memories(user_id, memory, categories, embedding, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    ("multi", txt, "semantic", json.dumps(self.server_mod.embed(txt)),
                     self.server_mod.now_iso(), self.server_mod.now_iso()),
                )
        conn.close()

        res = self.server_mod.upsert("multi", "今天开始吃鸡胸肉增肌")
        self.assertEqual(res["event"], "DELETE")
        self.assertEqual(res["deleted_count"], 2)
        self.assertEqual([m["memory"] for m in self.server_mod.list_memories("multi")], ["今天开始吃鸡胸肉增肌"])
        # 验证被删原文完整保留在审计 old_value 中可供恢复
        del_logs = [h for h in self.server_mod.history("multi") if h["operation"] == "DELETE"]
        self.assertEqual({h["old_value"] for h in del_logs}, {"我是严格素食主义者", "平时坚持素食习惯"})

        # 反例防护：隔字否定、第三人称转述、过去时背景子句均不误触发矛盾删除
        self.assertFalse(self.server_mod.is_contradiction("alice 不吃鸡胸肉", "alice 是素食者"))
        self.assertFalse(self.server_mod.is_contradiction("alice 的朋友喜欢吃鸡胸肉", "alice 是素食者"))
        self.assertFalse(self.server_mod.is_contradiction("同事推荐吃牛肉，但 alice 坚持素食", "alice 是素食者"))
        self.assertFalse(
            self.server_mod.is_contradiction("alice 以前是素食者，但现在吃鸡胸肉", "alice 每天吃鸡胸肉")
        )

    # ── 6. 锁外慢模型不阻塞其他用户 + 并发合并 OCC 无丢更新 ──────────────────
    def test_slow_merge_embed_outside_lock_and_occ_concurrent_merge(self):
        self.server_mod.upsert("slow_u", "slow_u 喜欢拿铁咖啡")
        orig_embed = self.server_mod._embed_with_cfg
        entered_slow = threading.Event()
        release_slow = threading.Event()

        def slow_merge_embed(text, provider_cfg=None):
            if "；" in text:
                entered_slow.set()
                release_slow.wait(timeout=3.0)
            return orig_embed(text, provider_cfg=provider_cfg)

        self.server_mod._embed_with_cfg = slow_merge_embed
        try:
            t_slow = threading.Thread(
                target=lambda: self.request(
                    "POST", "/v1/memories", {"user_id": "slow_u", "messages": ["slow_u 每天喝手冲咖啡"]}
                )
            )
            t_slow.start()
            self.assertTrue(entered_slow.wait(timeout=2.0))

            # 当 slow_u 正在等待慢速合并模型响应时，fast_u 的写入必须在 0.5s 内立即完成（未被全局写锁卡死！）
            t0 = time.monotonic()
            st_fast, _ = self.request("POST", "/v1/memories", {"user_id": "fast_u", "messages": ["fast_u 喜欢游泳"]})
            elapsed = time.monotonic() - t0
            self.assertEqual(st_fast, 200)
            self.assertLess(elapsed, 0.8, f"Other tenant blocked by slow embed: {elapsed:.2f}s")
            release_slow.set()
            t_slow.join(timeout=3.0)
        finally:
            self.server_mod._embed_with_cfg = orig_embed

        # 并发合并 OCC 验证：两个线程同时向同一用户的咖啡记忆追加新信息，两条都不得丢失
        self.server_mod.upsert("occ_u", "occ_u 喜欢喝拿铁咖啡")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.server_mod.upsert, "occ_u", "occ_u 早上喝冰美式咖啡")
            f2 = pool.submit(self.server_mod.upsert, "occ_u", "occ_u 下午喝手冲瑰夏咖啡")
            r1, r2 = f1.result(), f2.result()
        self.assertEqual({r1["event"], r2["event"]}, {"UPDATE"})
        final_mem = self.server_mod.list_memories("occ_u")[0]["memory"]
        self.assertIn("冰美式咖啡", final_mem)
        self.assertIn("手冲瑰夏咖啡", final_mem)
        upd_hist = [h for h in self.server_mod.history("occ_u") if h["operation"] == "UPDATE"]
        self.assertEqual(len(upd_hist), 2)

    # ── 7. 多线程并发写入 + 四幕剧本回归 ────────────────────────────────────
    def test_concurrent_writes_and_four_scene_demo_regression(self):
        unique_facts = [
            f"alpha{i} beta{i * 7 + 1} gamma{i * 13 + 2} delta{i * 29 + 3} omega{i * 47 + 5}"
            for i in range(24)
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(
                pool.map(
                    lambda fact: self.request("POST", "/v1/memories", {"user_id": "concurrent_u", "messages": [fact]}),
                    unique_facts,
                )
            )
        self.assertTrue(all(st == 200 and b["results"][0]["event"] == "ADD" for st, b in outcomes))
        self.assertEqual(len(self.request("POST", "/v2/memories", {"user_id": "concurrent_u", "limit": 50})[1]["results"]), 24)

        scenes = [
            ("alice 最近在学 Rust 编程语言", "ADD"),
            ("alice 是素食者，平时不吃肉", "NOOP"),
            ("alice 最近开始吃鸡胸肉了", "DELETE"),
            ("alice 喜欢看科幻电影，不喜欢惊悚片", "NOOP"),
        ]
        for text, expected_event in scenes:
            st, body = self.request("POST", "/v1/memories", {"user_id": "alice", "messages": [text]})
            self.assertEqual(st, 200)
            self.assertEqual(body["results"][0]["event"], expected_event)


if __name__ == "__main__":
    unittest.main()
