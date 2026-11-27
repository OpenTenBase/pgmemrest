#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo.py — 按"四幕剧本"驱动 mini-mem0 server，观察四操作决策序列、审计留痕与跨用户隔离。

用法：先启动 server（python3 server.py），另开终端执行 python3 demo.py
若服务端启用了 MEM_API_KEY，请在运行 demo.py 前导出相同的 MEM_API_KEY 环境变量。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://%s:%s" % (sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1",
                         sys.argv[2] if len(sys.argv) > 2 else "8080")
API_KEY = os.environ.get("MEM_API_KEY", "")


def call(method, path, body=None):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Token " + API_KEY
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def banner(title):
    print("\n" + "=" * 62 + "\n%s\n" % title + "=" * 62)


banner("重置环境（alice 的 2 条既有记忆）")
print(json.dumps(call("POST", "/v1/reset"), ensure_ascii=False, indent=2))
alice_init = call("POST", "/v2/memories", {"user_id": "alice"})
print(json.dumps(alice_init, ensure_ascii=False, indent=2))

banner("Part 2 检索：'alice 平时吃什么？'（复合打分）")
print(json.dumps(call("POST", "/v2/memories/search",
                      {"user_id": "alice", "query": "alice 平时吃什么", "limit": 3}),
                 ensure_ascii=False, indent=2))

banner("Part 3 四幕剧本（逐条写入，观察决策）")
scenes = [
    ("幕 1（预期 ADD：全新领域）", "alice 最近在学 Rust 编程语言"),
    ("幕 2（预期 NOOP：几乎重复既有记忆）", "alice 是素食者，平时不吃肉"),
    ("幕 3（预期 DELETE：alice 改变了饮食习惯！）", "alice 最近开始吃鸡胸肉了"),
    ("幕 4（预期 NOOP：重复了一遍电影偏好）", "alice 喜欢看科幻电影，不喜欢惊悚片"),
]
for title, text in scenes:
    print("\n--- %s : %s" % (title, text))
    print(json.dumps(call("POST", "/v1/memories",
                          {"user_id": "alice", "messages": [text]}),
                     ensure_ascii=False, indent=2))

banner("记忆表终态（'素食'那条还在吗？）")
alice_final = call("POST", "/v2/memories", {"user_id": "alice"})
print(json.dumps(alice_final, ensure_ascii=False, indent=2))

banner("挑战A 审计留痕：GET /v1/history?user_id=alice")
print(json.dumps(call("GET", "/v1/history?user_id=alice"), ensure_ascii=False, indent=2))

banner("挑战B 用户隔离：bob 读不到 alice 的记忆且无法越权 GET 单条")
print("bob 的搜索结果（应为空）:")
print(json.dumps(call("POST", "/v2/memories/search",
                      {"user_id": "bob", "query": "素食 吃什么"}), ensure_ascii=False, indent=2))
if alice_final.get("results"):
    target_id = alice_final["results"][0]["id"]
    try:
        call("GET", f"/v1/memories/{target_id}?user_id=bob")
        print("意外：bob 越权读取了 alice 的记忆！")
    except urllib.error.HTTPError as e:
        print(f"bob 越权 GET /v1/memories/{target_id}?user_id=bob 已拦截 -> HTTP {e.code}")
