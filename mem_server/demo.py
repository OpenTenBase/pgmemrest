#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo.py — 按 lab.md Part 3 的"四幕剧本"驱动 mini-mem0 server，观察四操作决策序列。

用法：先启动 server（python3 server.py），另开终端执行 python3 demo.py
"""
import json
import sys
import urllib.request

BASE = "http://%s:%s" % (sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1",
                         sys.argv[2] if len(sys.argv) > 2 else "8080")


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def banner(title):
    print("\n" + "=" * 62 + "\n%s\n" % title + "=" * 62)


banner("重置环境（对应 lab.md Checkpoint 2：alice 的 2 条既有记忆）")
print(json.dumps(call("POST", "/v1/reset"), ensure_ascii=False, indent=2))
print(json.dumps(call("POST", "/v2/memories", {"user_id": "alice"}), ensure_ascii=False, indent=2))

banner("Part 2 检索：'alice 平时吃什么？'（复合打分）")
print(json.dumps(call("POST", "/v2/memories/search",
                      {"user_id": "alice", "query": "alice 平时吃什么", "limit": 3}),
                 ensure_ascii=False, indent=2))

banner("Part 3 四幕剧本（对应 lab.md 4. 逐条写入，观察决策）")
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

banner("记忆表终态（Checkpoint 4：'素食'那条还在吗？）")
print(json.dumps(call("POST", "/v2/memories", {"user_id": "alice"}), ensure_ascii=False, indent=2))

banner("挑战A 审计留痕：GET /v1/history")
print(json.dumps(call("GET", "/v1/history"), ensure_ascii=False, indent=2))

banner("挑战B 用户隔离：bob 读不到 alice 的记忆")
print("bob 的搜索结果（应为空）:")
print(json.dumps(call("POST", "/v2/memories/search",
                      {"user_id": "bob", "query": "素食 吃什么"}), ensure_ascii=False, indent=2))
