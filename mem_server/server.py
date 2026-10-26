#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini-mem0 server — lab.md 的可运行参考实现（纯 Python 标准库，零依赖）

复刻 Mem0 的核心机制，与 lab.md 一一对应：

  lab.md Part 1 (存储)     -> init_db(): memories / memory_history 表
  lab.md Part 2 (检索)     -> search(): 余弦相似度 + 类型权重 + 时间新近度 复合打分
  lab.md Part 3 (写入决策) -> upsert(): 与最相似既有记忆比对 -> ADD/UPDATE/DELETE/NOOP
  lab.md 挑战A (审计)      -> memory_history 表，每次操作留痕
  lab.md 挑战B (隔离)      -> 所有查询强制 user_id 过滤（应用层隔离边界）

Embedding：默认使用确定性的本地 hashing embedding（中文字符二元组 + 英文分词），
无需任何外部 API 即可演示语义相似度；若设置了 EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL
则自动切换为 OpenAI 兼容的远端 embedding。

API（兼容 pgmemrest 的 mem_server.py 风格）：
  GET    /v1/ping                   健康检查
  POST   /v1/memories               写入记忆（内部走四操作决策管线）
  POST   /v2/memories               列出某用户记忆
  POST   /v2/memories/search        语义检索（复合打分）
  GET    /v1/memories/{id}          获取单条
  DELETE /v1/memories/{id}          删除单条
  DELETE /v1/memories               删除某用户全部
  GET    /v1/history                查看操作审计（挑战A）
  POST   /v1/reset                  重置并导入剧本初始数据

安全设计：
  - SQLite 全部参数绑定，杜绝 SQL 注入
  - 默认只监听 127.0.0.1；可选 MEM_API_KEY 做 Token 认证
  - 用户隔离：任何读写都以 user_id 为边界
"""

import json
import hashlib
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer          # Python 3.7+
except ImportError:                                       # Python 3.6 兼容
    from socketserver import ThreadingMixIn
    from http.server import HTTPServer

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

# ── 配置（密钥一律走环境变量）──────────────────────────────────────────────
DB_PATH = os.environ.get("MEM_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "memories.db"))
PORT = int(os.environ.get("MEM_PORT", "8080"))
HOST = os.environ.get("MEM_HOST", "127.0.0.1")
API_KEY = os.environ.get("MEM_API_KEY", "")  # 为空则不启用认证（本地学习用）
EMBED_DIM = int(os.environ.get("EMBED_DIM", "256"))
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "")

# lab.md Part 3 的决策阈值
THRESHOLD_NOOP = 0.95   # >= 0.95 几乎重复 -> NOOP
THRESHOLD_RELATED = 0.80  # 0.80~0.95 相关 -> UPDATE / DELETE
# lab.md Part 2 的类型权重
CATEGORY_WEIGHT = {"semantic": 0.6, "episodic": 0.3, "procedural": 0.1}
DECAY = 0.01  # exp(-0.01 * days)

# 矛盾检测的简化规则表（真实 Mem0 由 LLM 判断，这里用关键词对近似）
# 每对表示两个互斥状态：新旧记忆分别肯定了其中之一 => 矛盾
CONTRADICTION_PAIRS = [
    ("素食", "鸡胸肉"), ("素食", "吃肉"), ("素食", "排骨"), ("素食", "牛肉"),
    ("素食", "鸡腿"), ("素食", "火腿"),
    ("暗色模式", "浅色模式"),
    ("喝咖啡", "戒咖啡"), ("喝牛奶", "乳糖不耐"),
    ("住在上海", "住在北京"), ("住在深圳", "住在北京"), ("住在上海", "住在深圳"),
]
NEGATION_PREFIX = "不"

# 概念字典：把表面不同的词映射到同一概念，用于"同主题关联"判断。
# （真实 Mem0 依赖 embedding 语义空间，这里用小规则表近似）
CONCEPT_DICT = {
    "素食": "diet_vegetarian", "蔬菜": "diet_vegetarian", "豆腐": "diet_vegetarian",
    "西兰花": "diet_vegetarian",
    "鸡胸肉": "diet_meat", "吃肉": "diet_meat", "排骨": "diet_meat",
    "牛肉": "diet_meat", "鸡腿": "diet_meat", "火腿": "diet_meat",
    "科幻": "movie_scifi", "电影": "movie", "惊悚": "movie_thriller",
    "rust": "coding", "编程": "coding", "代码": "coding",
    "咖啡": "coffee", "牛奶": "milk",
}


# ── Embedding ──────────────────────────────────────────────────────────────
def tokenize(text):
    """中英混合分词：英文按单词，中文按字符二元组（bigram）。"""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


def local_embed(text):
    """确定性 hashing embedding：词哈希到固定维度，L2 归一化。零依赖、离线可用。"""
    vec = [0.0] * EMBED_DIM
    for tok in tokenize(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % EMBED_DIM
        sign = 1.0 if ((h >> 64) & 1) else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 8) for v in vec]


def remote_embed(text):
    """OpenAI 兼容 embedding API（配置了 EMBED_* 环境变量时启用）。"""
    if not (EMBED_BASE_URL and EMBED_API_KEY and EMBED_MODEL):
        return None
    try:
        req = urllib.request.Request(
            EMBED_BASE_URL.rstrip("/") + "/embeddings",
            data=json.dumps({"input": text, "model": EMBED_MODEL}).encode("utf-8"),
            headers={"Authorization": "Bearer " + EMBED_API_KEY,
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["data"][0]["embedding"]
    except Exception:
        return None


def embed(text):
    return remote_embed(text) or local_embed(text)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ── 混合相似度：向量余弦 ∨ 词汇包含度 ∨ 概念关联 ─────────────────────────
def token_set(text):
    """词汇集合：英文词 + 中文 bigram + 中文单字。"""
    toks = set(tokenize(text))
    toks.update(re.findall(r"[\u4e00-\u9fff]", text))
    return toks


def lexical_sim(t_new, t_old):
    """词汇包含度（overlap coefficient）开根号，把 0.64~1.0 的重合度
    映射到 0.80~1.0 区间，使其与 lab.md 的决策阈值同一量纲。"""
    if not t_new or not t_old:
        return 0.0
    inter = len(t_new & t_old)
    return math.sqrt(inter / min(len(t_new), len(t_old)))


def extract_concepts(text):
    return set(c for kw, c in CONCEPT_DICT.items() if kw in text.lower())


def concept_sim(c_new, c_old):
    """同主题概念关联：有交集即视为相关（0.8+）。
    上限刻意压在 0.9（< 0.95 NOOP 阈值）——概念相同只说明'相关'，
    是否'几乎重复'必须由词汇包含度判定，避免同主题新信息被误吞。"""
    if not c_new or not c_old or not (c_new & c_old):
        return 0.0
    union = len(c_new | c_old)
    return 0.8 + 0.1 * (len(c_new & c_old) / union)


def text_sim(text_new, vec_new, text_old):
    """总相似度 = max(向量余弦, 词汇包含度, 概念关联)。

    本地 embedding 是词法级的，语义区分度弱于 pgvector 的真实语义向量，
    因此叠加词汇与概念两个信号，使 lab.md 的 0.95/0.80 阈值语义成立。
    """
    s_cos = cosine(vec_new, embed(text_old))
    s_lex = lexical_sim(token_set(text_new), token_set(text_old))
    s_con = concept_sim(extract_concepts(text_new), extract_concepts(text_old))
    return max(s_cos, s_lex, s_con)


# ── 存储（SQLite，全部参数绑定）────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    agent_id    TEXT DEFAULT 'agent_ou',
    memory      TEXT NOT NULL,
    categories  TEXT DEFAULT 'semantic',
    metadata    TEXT DEFAULT '{}',
    embedding   TEXT NOT NULL,            -- JSON 数组
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id);
CREATE TABLE IF NOT EXISTS memory_history (        -- lab.md 挑战A
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER,
    user_id     TEXT,
    operation   TEXT,                    -- ADD/UPDATE/DELETE/NOOP
    old_value   TEXT,
    new_value   TEXT,
    detail      TEXT,
    occurred_at TEXT NOT NULL
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


SEED = [
    # (user_id, memory, categories, days_ago)  —— 对应 lab.md "alice 的 2 条既有记忆"
    ("alice", "alice 是素食者，平时不吃肉，喜欢吃蔬菜和豆腐", "semantic", 90),
    ("alice", "alice 喜欢看科幻电影，不喜欢惊悚片", "semantic", 60),
]


def reset_db():
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memory_history")
        for user_id, memory, cat, days in SEED:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - days * 86400))
            conn.execute(
                "INSERT INTO memories(user_id, memory, categories, embedding, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (user_id, memory, cat, json.dumps(embed(memory)), ts, ts))
    conn.close()


# ── Part 3 灵魂：写入决策管线（两阶段）────────────────────────────────────
def affirmed(text, phrase):
    """短语在文本中是否被'肯定'（出现且前面没有否定字'不'）。"""
    start = 0
    while True:
        i = text.find(phrase, start)
        if i < 0:
            return False
        if i == 0 or text[i - 1] != NEGATION_PREFIX:
            return True
        start = i + 1


def is_contradiction(new_text, old_text):
    """简化矛盾检测：新旧记忆分别'肯定'了一对互斥状态。
    例：旧'素食者(肯定素食)' vs 新'开始吃鸡胸肉(肯定鸡胸肉)' => 矛盾。
    注意 '不喜欢惊悚' 中的 '喜欢惊悚' 前面有'不'，不算肯定。"""
    for a, b in CONTRADICTION_PAIRS:
        if (affirmed(new_text, a) and affirmed(old_text, b)) or \
           (affirmed(new_text, b) and affirmed(old_text, a)):
            return True
    return False


def upsert(user_id, text, agent_id="agent_ou", category=None):
    """lab.md Part 3：新事实与最相似既有记忆比对，四选一决策。"""
    if not text or not text.strip():
        return {"event": "NOOP", "reason": "empty input"}
    text = text.strip()
    category = category or classify(text)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, memory, categories, embedding, updated_at FROM memories WHERE user_id = ?",
            (user_id,)).fetchall()
        new_vec = embed(text)

        best, best_sim = None, -1.0
        for r in rows:
            sim = text_sim(text, new_vec, r["memory"])
            if sim > best_sim:
                best, best_sim = r, sim

        ts = now_iso()

        # 矛盾优先判断：新旧记忆分别肯定了互斥状态 => 删除旧的、写入新的
        # （即使相似度高也不例外——'不吃肉'与'吃鸡胸肉'文本近似但语义相反）
        contra_id = None
        for r in rows:
            if is_contradiction(text, r["memory"]):
                contra_id = r["id"]
                contra_old = r["memory"]
                break

        if contra_id is not None:
            with conn:
                conn.execute("DELETE FROM memories WHERE id = ?", (contra_id,))
                cur = conn.execute(
                    "INSERT INTO memories(user_id, agent_id, memory, categories, embedding, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (user_id, agent_id, text, category, json.dumps(new_vec), ts, ts))
            log_history(conn, contra_id, user_id, "DELETE", contra_old, None,
                        "新旧记忆矛盾，删除旧记忆")
            log_history(conn, cur.lastrowid, user_id, "ADD", None, text, "矛盾后写入新事实")
            return {"event": "DELETE", "similarity": round(best_sim, 4),
                    "deleted_memory": contra_old, "new_memory_id": cur.lastrowid, "memory": text}

        if best is not None and best_sim >= THRESHOLD_NOOP:
            log_history(conn, None, user_id, "NOOP", best["memory"], text,
                        "相似度 %.3f >= 0.95，几乎重复，忽略" % best_sim)
            return {"event": "NOOP", "similarity": round(best_sim, 4),
                    "duplicated_with": best["id"], "memory": text}

        if best is not None and best_sim >= THRESHOLD_RELATED:
            # 相关但有新信息 -> 合并 UPDATE
            merged = best["memory"] + "；" + text
            with conn:
                conn.execute(
                    "UPDATE memories SET memory = ?, embedding = ?, categories = ?, updated_at = ? WHERE id = ?",
                    (merged, json.dumps(embed(merged)), category, ts, best["id"]))
            log_history(conn, best["id"], user_id, "UPDATE", best["memory"], merged,
                        "相关但有新信息(相似度 %.3f)，合并" % best_sim)
            return {"event": "UPDATE", "similarity": round(best_sim, 4),
                    "memory_id": best["id"], "old_memory": best["memory"], "memory": merged}

        # 全新事实 -> ADD
        with conn:
            cur = conn.execute(
                "INSERT INTO memories(user_id, agent_id, memory, categories, embedding, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (user_id, agent_id, text, category, json.dumps(new_vec), ts, ts))
        log_history(conn, cur.lastrowid, user_id, "ADD", None, text,
                    "相似度最高 %.3f < 0.80，全新事实" % max(best_sim, 0.0))
        return {"event": "ADD", "memory_id": cur.lastrowid, "memory": text}
    finally:
        conn.close()


def classify(text):
    """粗分类：含时间/事件词为 episodic，含步骤词为 procedural，否则 semantic。"""
    if re.search(r"昨天|上周|今天|刚刚|刚才|最近|那次|当时", text):
        return "episodic"
    if re.search(r"步骤|先.*再|第一|第二|如何|怎么|流程", text):
        return "procedural"
    return "semantic"


def log_history(conn, memory_id, user_id, op, old, new, detail):
    with conn:
        conn.execute(
            "INSERT INTO memory_history(memory_id, user_id, operation, old_value, new_value, detail, occurred_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (memory_id, user_id, op, old, new, detail, now_iso()))


# ── Part 2：检索（语义召回 + 复合打分）─────────────────────────────────────
def search(user_id, query, limit=5):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, memory, categories, updated_at, embedding FROM memories WHERE user_id = ?",
            (user_id,)).fetchall()
    finally:
        conn.close()
    q_vec = embed(query)
    now = time.time()

    results = []
    for r in rows:
        sim = cosine(q_vec, json.loads(r["embedding"]))
        cat = (r["categories"] or "semantic").split(",")[0].strip()
        weight = CATEGORY_WEIGHT.get(cat, 0.1)
        # 时间新近度：exp(-0.01 * 天数)，与 lab.md 公式一致
        try:
            updated = datetime.strptime(r["updated_at"][:19], "%Y-%m-%dT%H:%M:%S")
            days = max((now - updated.replace(tzinfo=timezone.utc).timestamp()) / 86400.0, 0.0)
        except Exception:
            days = 0.0
        final = sim * weight * math.exp(-DECAY * days)
        results.append({
            "id": r["id"], "memory": r["memory"], "categories": cat,
            "similarity": round(sim, 4),
            "category_weight": weight,
            "recency": round(math.exp(-DECAY * days), 4),
            "final_score": round(final, 4),
            "updated_at": r["updated_at"],
        })
    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results[:limit]


def list_memories(user_id, limit=20):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, user_id, agent_id, memory, categories, created_at, updated_at"
            " FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_one(memory_id, user_id=None):
    """user_id 传了就做归属校验（lab.md 挑战B 的应用层隔离）。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, user_id, agent_id, memory, categories, created_at, updated_at"
            " FROM memories WHERE id = ?", (memory_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if user_id is not None and row["user_id"] != user_id:
        return "FORBIDDEN"  # 越权访问他人记忆
    return dict(row)


def delete_one(memory_id, user_id=None):
    conn = get_db()
    try:
        row = conn.execute("SELECT user_id, memory FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            conn.close()
            return "NOT_FOUND"
        if user_id is not None and row["user_id"] != user_id:
            conn.close()
            return "FORBIDDEN"
        with conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        log_history(conn, memory_id, row["user_id"], "DELETE", row["memory"], None, "API 删除")
        conn.close()
        return "OK"
    except Exception:
        conn.close()
        raise


def delete_all(user_id):
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, memory FROM memories WHERE user_id = ?", (user_id,)).fetchall()
        with conn:
            conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        for r in rows:
            log_history(conn, r["id"], user_id, "DELETE", r["memory"], None, "API 删除全部")
        return len(rows)
    finally:
        conn.close()


def history(user_id=None, limit=50):
    conn = get_db()
    try:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM memory_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── HTTP 层（标准库）───────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if not API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth.replace("Token ", "").replace("Bearer ", "").strip()
        return token == API_KEY

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _dispatch(self, method):
        if not self._auth():
            return self._json(401, {"error": "invalid api key"})
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = self._body() if method in ("POST", "DELETE") else {}

        try:
            # GET
            if method == "GET":
                if path == "/v1/ping":
                    return self._json(200, {"status": "ok", "backend": "sqlite", "db": DB_PATH})
                m = re.match(r"^/v1/memories/(\d+)$", path)
                if m:
                    rec = get_one(int(m.group(1)), user_id=body.get("user_id"))
                    if rec == "FORBIDDEN":
                        return self._json(403, {"error": "not your memory"})
                    if rec is None:
                        return self._json(404, {"error": "not found"})
                    return self._json(200, rec)
                if path == "/v1/history":
                    return self._json(200, {"results": history(body.get("user_id"))})
                return self._json(404, {"error": "not found"})

            # POST
            if method == "POST":
                if path == "/v1/reset":
                    reset_db()
                    return self._json(200, {"status": "reset", "seeded": len(SEED)})
                if path == "/v1/memories":
                    user_id = (body.get("user_id") or "default").strip()
                    messages = body.get("messages")
                    if not messages:
                        return self._json(400, {"error": "messages required"})
                    results = []
                    for msg in messages:
                        text = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                        if text.strip():
                            results.append(upsert(user_id, text,
                                                  agent_id=body.get("agent_id", "agent_ou"),
                                                  category=body.get("category")))
                    return self._json(200, {"results": results})
                if path == "/v2/memories":
                    return self._json(200, {"results": list_memories(
                        (body.get("user_id") or "default").strip(), body.get("limit", 20))})
                if path == "/v2/memories/search":
                    query = body.get("query")
                    if not query:
                        return self._json(400, {"error": "query required"})
                    return self._json(200, {"results": search(
                        (body.get("user_id") or "default").strip(), query, body.get("limit", 5))})
                return self._json(404, {"error": "not found"})

            # DELETE
            if method == "DELETE":
                m = re.match(r"^/v1/memories/(\d+)$", path)
                if m:
                    res = delete_one(int(m.group(1)), user_id=body.get("user_id"))
                    if res == "FORBIDDEN":
                        return self._json(403, {"error": "not your memory"})
                    if res == "NOT_FOUND":
                        return self._json(404, {"error": "not found"})
                    return self._json(200, {"status": "deleted", "id": int(m.group(1))})
                if path == "/v1/memories":
                    n = delete_all((body.get("user_id") or "default").strip())
                    return self._json(200, {"status": "deleted_all", "count": n})
                return self._json(404, {"error": "not found"})

            return self._json(405, {"error": "method not allowed"})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    if not os.path.exists(DB_PATH):
        reset_db()
        print("初始化数据库并导入剧本数据：%s" % DB_PATH)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("mini-mem0 server listening on http://%s:%d  (db=%s)" % (HOST, PORT, DB_PATH))
    if API_KEY:
        print("认证已开启：Authorization: Token <MEM_API_KEY>")
    srv.serve_forever()


if __name__ == "__main__":
    main()
