#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini-mem0 server — 本地参考实现（纯 Python 标准库，零依赖）

核心能力与安全边界：
  - Part 1 (存储)     -> memories / memory_history 表（持久化 embed_model 元数据防止异构模型混算）
  - Part 2 (检索)     -> 余弦相似度 × 类型权重 × 时间新近度 复合打分
  - Part 3 (写入决策) -> 锁外 Embedding + BEGIN IMMEDIATE 快照校验重试，支持单条与整批原子提交
  - 挑战A (审计)      -> memory_history 表与主操作同事务原子留痕，保留被替代原文支持审计恢复
  - 挑战B (隔离)      -> 支持两种明确分离的部署模式：
      1) 单用户/可信网关模式（默认）：兼容传入 user_id 做归属核验；可选 MEM_ADMIN_KEY 保护全局管理操作
      2) 多租户强身份绑定模式（配置 MEM_TENANT_TOKENS）：从已验证凭据直接映射唯一租户身份，禁止伪造或跨租户访问
  - 探针分离          -> 免认证存活探针 /healthz 与就绪探针 /readyz（不暴露内部文件路径，未就绪返 503）
"""

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
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
API_KEY = os.environ.get("MEM_API_KEY", "")          # 单用户/可信网关共享密钥
ADMIN_KEY = os.environ.get("MEM_ADMIN_KEY", "")      # 独立管理员密钥（保护 /v1/reset 与跨租户审计）
TENANT_TOKENS_RAW = os.environ.get("MEM_TENANT_TOKENS", "")  # 多租户凭据映射：JSON {"tok":"uid"} 或 "uid:tok,..."
REQUIRE_USER_ID = os.environ.get("MEM_REQUIRE_USER_ID", "").lower() in ("1", "true", "yes")
ENABLE_RESET = os.environ.get("MEM_ENABLE_RESET", "1").lower() not in ("0", "false", "no")

EMBED_DIM = int(os.environ.get("EMBED_DIM", "256"))
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "")
EMBED_PROVIDERS_RAW = os.environ.get("EMBED_PROVIDERS_JSON", "")
ALLOW_CUSTOM_EMBED_URL = os.environ.get("MEM_ALLOW_CUSTOM_EMBED_URL", "").lower() in ("1", "true", "yes")
ALLOWED_EMBED_HOSTS_RAW = os.environ.get("MEM_ALLOWED_EMBED_HOSTS", "")

MAX_LIMIT = int(os.environ.get("MEM_MAX_LIMIT", "10000"))
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB
OCC_MAX_RETRIES = 64

THRESHOLD_NOOP = 0.95   # >= 0.95 几乎重复 -> NOOP
THRESHOLD_RELATED = 0.80  # 0.80~0.95 相关 -> UPDATE / DELETE
CATEGORY_WEIGHT = {"semantic": 0.6, "episodic": 0.3, "procedural": 0.1}
DECAY = 0.01  # exp(-0.01 * days)

CONTRADICTION_PAIRS = [
    ("素食", "鸡胸肉"), ("素食", "吃肉"), ("素食", "排骨"), ("素食", "牛肉"),
    ("素食", "鸡腿"), ("素食", "火腿"),
    ("暗色模式", "浅色模式"),
    ("喝咖啡", "戒咖啡"), ("喝牛奶", "乳糖不耐"),
    ("住在上海", "住在北京"), ("住在深圳", "住在北京"), ("住在上海", "住在深圳"),
]
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]|(?:但|但是|不过|而是|却)")
_DOUBLE_NEGATION_RE = re.compile(r"(?:并非|不是|不会)(?:不|没)[吃喝用看住是去爱喜欢想能会要再过着了的]{0,2}$")
_NEGATION_TAIL_RE = re.compile(
    r"(?:不再是|不再|没有|并非|不是|从不|绝不|从未|戒掉|戒了|拒绝|讨厌|不喜欢|不爱|不|没|未)"
    r"[吃喝用看住是去爱喜欢想能会要再过着了的]{0,3}$"
)
# 排除过去时背景子句（如"以前是素食者"）与转述他人偏好子句（如"朋友喜欢吃鸡胸肉" / "别人说素食好"）
_PAST_TENSE_CLAUSE_RE = re.compile(r"(?:以前|过去|曾经|早年|小时候|去年|上个月)")
_THIRD_PARTY_CLAUSE_RE = re.compile(
    r"(?:朋友|同事|室友|家人|父母|爸爸|妈妈|老公|老婆|男友|女友|别人|他人|老板|医生|网友)"
    r"[^，,。；;]{0,8}$|(?:听说|据说|转述|提到说|问我)"
)

CONCEPT_DICT = {
    "素食": "diet_vegetarian", "蔬菜": "diet_vegetarian", "豆腐": "diet_vegetarian",
    "西兰花": "diet_vegetarian",
    "鸡胸肉": "diet_meat", "吃肉": "diet_meat", "排骨": "diet_meat",
    "牛肉": "diet_meat", "鸡腿": "diet_meat", "火腿": "diet_meat",
    "科幻": "movie_scifi", "电影": "movie", "惊悚": "movie_thriller",
    "rust": "coding", "编程": "coding", "代码": "coding",
    "咖啡": "coffee", "牛奶": "milk",
}

_DB_INIT_LOCK = threading.Lock()
_WRITE_LOCK = threading.RLock()
_INITIALIZED_DBS = set()


class BadRequestError(ValueError):
    """客户端请求参数非法时抛出，HTTP 层统一返回 400。"""


class ForbiddenError(PermissionError):
    """权限不足、租户身份不匹配或未授权出站目标时抛出，HTTP 层返回 403。"""


class _RetryConflictError(Exception):
    """乐观并发快照不匹配时触发自动回滚与重算。"""


def parse_tenant_tokens(raw):
    """解析多租户 Token 映射表，返回 {token_str: user_id_str}。
    支持 JSON 对象 {"tok_alice": "alice"} 或逗号分隔 "alice:tok_alice,bob:tok_bob"。"""
    if not raw or not raw.strip():
        return {}
    text = raw.strip()
    mapping = {}
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("MEM_TENANT_TOKENS JSON must be an object {token: user_id}")
        for tok, uid in data.items():
            if isinstance(tok, str) and tok.strip() and isinstance(uid, str) and uid.strip():
                mapping[tok.strip()] = uid.strip()
        return mapping
    for item in text.split(","):
        part = item.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError("Invalid MEM_TENANT_TOKENS entry (expected user_id:token)")
        uid, tok = part.split(":", 1)
        if uid.strip() and tok.strip():
            mapping[tok.strip()] = uid.strip()
    return mapping


def parse_embed_providers(raw_json, default_url="", default_key="", default_model=""):
    """构建服务端受信 Embedding Provider 注册表：{provider_name: {base_url, api_key, model}}。"""
    providers = {}
    if default_url and default_key:
        providers["default"] = {
            "base_url": default_url.rstrip("/"),
            "api_key": default_key,
            "model": default_model or "text-embedding-3-small",
        }
    if raw_json and raw_json.strip():
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            for name, cfg in parsed.items():
                if isinstance(cfg, dict) and cfg.get("base_url") and cfg.get("api_key"):
                    providers[str(name).strip()] = {
                        "base_url": str(cfg["base_url"]).rstrip("/"),
                        "api_key": str(cfg["api_key"]),
                        "model": str(cfg.get("model") or default_model or "text-embedding-3-small"),
                    }
    return providers


def _validate_custom_embed_url(url, allowed_hosts):
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("https", "http") or not host:
        raise ForbiddenError("Invalid custom embedding URL")
    if host not in allowed_hosts:
        raise ForbiddenError(f"Embedding host {host!r} is not in MEM_ALLOWED_EMBED_HOSTS")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ForbiddenError("Link-local or metadata IP is forbidden for embedding URL")
    except ValueError:
        pass
    if scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ForbiddenError("Custom external embedding URL must use HTTPS")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are disallowed for embedding requests", headers, fp)


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


def current_embed_model_tag(provider_cfg=None):
    if provider_cfg:
        host = urllib.parse.urlsplit(provider_cfg["base_url"]).hostname or "remote"
        return f"remote:{host}:{provider_cfg['model']}"
    if EMBED_BASE_URL and EMBED_API_KEY and EMBED_MODEL:
        host = urllib.parse.urlsplit(EMBED_BASE_URL).hostname or "remote"
        return f"remote:{host}:{EMBED_MODEL}"
    return f"local-hash-{EMBED_DIM}"


def resolve_embed_provider(provider_name="", custom_url="", custom_key="", custom_model=""):
    """解析并校验本次请求的 Embedding 目标配置，杜绝服务端凭据泄露到外部地址（SSRF 防护）。"""
    providers = parse_embed_providers(EMBED_PROVIDERS_RAW, EMBED_BASE_URL, EMBED_API_KEY, EMBED_MODEL)
    if provider_name:
        if provider_name not in providers:
            raise BadRequestError(f"Unknown embedding provider: {provider_name!r}")
        cfg = dict(providers[provider_name])
        if custom_model:
            cfg["model"] = custom_model
        return cfg

    if custom_url:
        norm_url = custom_url.rstrip("/")
        default_cfg = providers.get("default")
        # 仅当自定义 URL 与服务端配置的受信 URL 完全一致时，才允许在不传 key 时使用服务端 key
        if default_cfg and norm_url == default_cfg["base_url"] and not custom_key:
            cfg = dict(default_cfg)
            if custom_model:
                cfg["model"] = custom_model
            return cfg
        # 严禁将服务端密钥拼接到调用者指定的任意 URL！
        if not custom_key:
            raise ForbiddenError("Custom embedding base_url cannot use server-side API credentials")
        if not ALLOW_CUSTOM_EMBED_URL:
            raise ForbiddenError("Custom embedding base_url is disabled; select a trusted provider instead")
        allowed_hosts = {h.strip().lower() for h in ALLOWED_EMBED_HOSTS_RAW.split(",") if h.strip()}
        _validate_custom_embed_url(norm_url, allowed_hosts)
        return {
            "base_url": norm_url,
            "api_key": custom_key,
            "model": custom_model or EMBED_MODEL or "text-embedding-3-small",
        }

    if custom_key:
        default_cfg = providers.get("default")
        if not default_cfg:
            raise BadRequestError("X-Embed-Api-Key provided without a configured embedding provider")
        return {
            "base_url": default_cfg["base_url"],
            "api_key": custom_key,
            "model": custom_model or default_cfg["model"],
        }

    return providers.get("default")


def remote_embed(text, provider_cfg=None):
    """OpenAI 兼容 embedding API（禁止自动跟随重定向）。"""
    cfg = provider_cfg
    if cfg is None:
        if not (EMBED_BASE_URL and EMBED_API_KEY and EMBED_MODEL):
            return None
        cfg = {"base_url": EMBED_BASE_URL.rstrip("/"), "api_key": EMBED_API_KEY, "model": EMBED_MODEL}
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/embeddings",
            data=json.dumps({"input": text, "model": cfg["model"]}).encode("utf-8"),
            headers={"Authorization": "Bearer " + cfg["api_key"],
                     "Content-Type": "application/json"},
            method="POST")
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["data"][0]["embedding"]
    except Exception:
        return None


def embed(text):
    return remote_embed(text) or local_embed(text)


def _embed_with_cfg(text, provider_cfg=None):
    if provider_cfg is not None:
        vec = remote_embed(text, provider_cfg=provider_cfg)
        return vec if vec is not None else local_embed(text)
    return embed(text)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ── 混合相似度：向量余弦 ∨ 词汇包含度 ∨ 概念关联 ─────────────────────────
def token_set(text):
    toks = set(tokenize(text))
    toks.update(re.findall(r"[\u4e00-\u9fff]", text))
    return toks


def lexical_sim(t_new, t_old):
    if not t_new or not t_old:
        return 0.0
    inter = len(t_new & t_old)
    return math.sqrt(inter / min(len(t_new), len(t_old)))


def extract_concepts(text):
    return set(c for kw, c in CONCEPT_DICT.items() if kw in text.lower())


def concept_sim(c_new, c_old):
    if not c_new or not c_old or not (c_new & c_old):
        return 0.0
    union = len(c_new | c_old)
    return 0.8 + 0.1 * (len(c_new & c_old) / union)


def _parse_stored_embedding(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return None
    return None


def _extract_model_from_metadata(raw_meta):
    if isinstance(raw_meta, dict):
        return raw_meta.get("embed_model")
    if isinstance(raw_meta, str) and raw_meta:
        try:
            d = json.loads(raw_meta)
            if isinstance(d, dict):
                return d.get("embed_model")
        except Exception:
            return None
    return None


def _models_compatible(new_model_tag, old_meta_raw):
    old_tag = _extract_model_from_metadata(old_meta_raw)
    if not old_tag or not new_model_tag:
        return True
    return old_tag == new_model_tag


def text_sim(text_new, vec_new, text_old, vec_old=None, model_tag_new=None, old_metadata=None):
    """总相似度 = max(向量余弦, 词汇包含度, 概念关联)。
    严格复用已持久化的 vec_old（O(1) 次模型调用），且同时核验维度与模型标识一致，防止同维异构模型混算。"""
    old_vec = _parse_stored_embedding(vec_old)
    if (
        old_vec
        and vec_new
        and len(old_vec) == len(vec_new)
        and _models_compatible(model_tag_new, old_metadata)
    ):
        s_cos = cosine(vec_new, old_vec)
    else:
        s_cos = 0.0
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
CREATE TABLE IF NOT EXISTS memory_history (        -- 挑战A：审计留痕
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER,
    user_id     TEXT,
    operation   TEXT,                    -- ADD/UPDATE/DELETE/NOOP
    old_value   TEXT,
    new_value   TEXT,
    detail      TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_user ON memory_history(user_id);
"""


def _connect_sqlite(db_path):
    # isolation_level=None 配合显式 BEGIN IMMEDIATE 控制短写事务边界
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path=None):
    """幂等初始化建表并启用 WAL 日志模式（减少并发写锁冲突）。"""
    path = db_path or DB_PATH
    with _DB_INIT_LOCK:
        conn = _connect_sqlite(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            _INITIALIZED_DBS.add(os.path.abspath(path))
        finally:
            conn.close()


def get_db():
    abs_path = os.path.abspath(DB_PATH)
    if abs_path not in _INITIALIZED_DBS or not os.path.exists(DB_PATH):
        init_db(DB_PATH)
    return _connect_sqlite(DB_PATH)


from contextlib import contextmanager


@contextmanager
def _immediate_tx(conn):
    """显式开启 SQLite BEGIN IMMEDIATE 写事务，支持跨连接/跨进程并发写协调，异常自动回滚。"""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


SEED = [
    ("alice", "alice 是素食者，平时不吃肉，喜欢吃蔬菜和豆腐", "semantic", 90),
    ("alice", "alice 喜欢看科幻电影，不喜欢惊悚片", "semantic", 60),
]


def reset_db():
    model_tag = current_embed_model_tag()
    seeded_rows = []
    for user_id, memory, cat, days in SEED:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - days * 86400))
        meta = json.dumps({"embed_model": model_tag}, ensure_ascii=False)
        seeded_rows.append((user_id, memory, cat, meta, json.dumps(embed(memory)), ts, ts))

    with _WRITE_LOCK:
        conn = get_db()
        try:
            with _immediate_tx(conn):
                conn.execute("DELETE FROM memories")
                conn.execute("DELETE FROM memory_history")
                for row in seeded_rows:
                    conn.execute(
                        "INSERT INTO memories(user_id, memory, categories, metadata, embedding, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        row,
                    )
        finally:
            conn.close()


# ── Part 3 灵魂：写入决策管线（两阶段 + 锁外模型调用 + 乐观版本校验）─────────
def affirmed(text, phrase, ignore_past_or_third_party=False):
    """短语在文本中是否被'肯定'。
    ignore_past_or_third_party=True 时，额外过滤过去时背景子句（如'以前是素食者'）和他人转述子句（如'朋友爱吃牛肉'）。"""
    start = 0
    while True:
        i = text.find(phrase, start)
        if i < 0:
            return False
        before = text[:i]
        clauses_before = _CLAUSE_SPLIT_RE.split(before)
        clause_prefix = clauses_before[-1].rstrip()
        after_clause = _CLAUSE_SPLIT_RE.split(text[i:])[0]
        full_clause = clause_prefix + after_clause

        negated = False
        if not _DOUBLE_NEGATION_RE.search(clause_prefix) and _NEGATION_TAIL_RE.search(clause_prefix):
            negated = True

        if not negated:
            if ignore_past_or_third_party:
                # 若整句还有后续转折子句且当前子句带明确过去时词，视为历史背景而非当前状态断言
                has_later_clause = len(_CLAUSE_SPLIT_RE.split(text[i + len(phrase):])) > 1 or bool(
                    re.search(r"[，,。；;]|(?:但|但是|不过|现在|如今)", text[i + len(phrase):])
                )
                if _PAST_TENSE_CLAUSE_RE.search(full_clause) and has_later_clause:
                    start = i + 1
                    continue
                if _THIRD_PARTY_CLAUSE_RE.search(clause_prefix):
                    start = i + 1
                    continue
            return True
        start = i + 1


def is_contradiction(new_text, old_text):
    """矛盾检测：新旧记忆分别就当前主体肯定了一对互斥状态（过滤过去时铺垫与第三人称转述反例）。"""
    for a, b in CONTRADICTION_PAIRS:
        if (affirmed(new_text, a, ignore_past_or_third_party=True) and affirmed(old_text, b, ignore_past_or_third_party=True)) or \
           (affirmed(new_text, b, ignore_past_or_third_party=True) and affirmed(old_text, a, ignore_past_or_third_party=True)):
            return True
    return False


def _insert_history(conn, memory_id, user_id, op, old, new, detail):
    conn.execute(
        "INSERT INTO memory_history(memory_id, user_id, operation, old_value, new_value, detail, occurred_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (memory_id, user_id, op, old, new, detail, now_iso()),
    )


def log_history(conn, memory_id, user_id, op, old, new, detail):
    with _immediate_tx(conn):
        _insert_history(conn, memory_id, user_id, op, old, new, detail)


def _snapshot_signature(rows):
    return tuple((r["id"], r["updated_at"], r["memory"]) for r in rows)


def _fetch_user_rows(conn, user_id):
    return conn.execute(
        "SELECT id, memory, categories, metadata, embedding, updated_at FROM memories WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    ).fetchall()


def _plan_single_decision(text, new_vec, rows, category, model_tag, provider_cfg=None):
    """纯内存 + 锁外模型调用阶段：基于当前快照规划决策（如需合并 UPDATE 则在锁外预先算好 merged_vec）。"""
    best, best_sim = None, -1.0
    for r in rows:
        sim = text_sim(
            text,
            new_vec,
            r["memory"],
            vec_old=r["embedding"],
            model_tag_new=model_tag,
            old_metadata=r["metadata"],
        )
        if sim > best_sim:
            best, best_sim = r, sim

    contradictions = [r for r in rows if is_contradiction(text, r["memory"])]
    if contradictions:
        return {"kind": "DELETE", "contradictions": contradictions, "best_sim": best_sim}

    if best is not None and best_sim >= THRESHOLD_NOOP:
        return {"kind": "NOOP", "best": best, "best_sim": best_sim}

    if best is not None and best_sim >= THRESHOLD_RELATED:
        merged = best["memory"] + "；" + text
        merged_vec = _embed_with_cfg(merged, provider_cfg=provider_cfg)
        return {
            "kind": "UPDATE",
            "best": best,
            "best_sim": best_sim,
            "merged": merged,
            "merged_vec": merged_vec,
            "category": category,
        }

    return {"kind": "ADD", "best_sim": best_sim, "category": category}


def _apply_planned_decision(conn, user_id, agent_id, text, new_vec, model_tag, plan):
    """在已开启的 SQLite 写事务中执行单步落库与审计留痕。"""
    ts = now_iso()
    meta_json = json.dumps({"embed_model": model_tag}, ensure_ascii=False)
    kind = plan["kind"]

    if kind == "DELETE":
        contradictions = plan["contradictions"]
        deleted_texts = [r["memory"] for r in contradictions]
        cur = conn.execute(
            "INSERT INTO memories(user_id, agent_id, memory, categories, metadata, embedding, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user_id, agent_id, text, classify(text), meta_json, json.dumps(new_vec), ts, ts),
        )
        new_id = cur.lastrowid
        for r in contradictions:
            conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (r["id"], user_id))
            _insert_history(
                conn,
                r["id"],
                user_id,
                "DELETE",
                r["memory"],
                text,
                f"新旧记忆矛盾，旧记忆已被新记忆 #{new_id} 替代（原文已保存在 old_value 可恢复）",
            )
        _insert_history(conn, new_id, user_id, "ADD", None, text, "矛盾后写入新事实")
        return {
            "id": new_id,
            "event": "DELETE",
            "similarity": round(max(plan["best_sim"], 0.0), 4),
            "deleted_memory": deleted_texts[0],
            "deleted_memories": deleted_texts,
            "deleted_count": len(deleted_texts),
            "new_memory_id": new_id,
            "memory": text,
        }

    if kind == "NOOP":
        best = plan["best"]
        _insert_history(
            conn,
            best["id"],
            user_id,
            "NOOP",
            best["memory"],
            text,
            "相似度 %.3f >= 0.95，几乎重复，忽略" % plan["best_sim"],
        )
        return {
            "id": best["id"],
            "event": "NOOP",
            "similarity": round(plan["best_sim"], 4),
            "duplicated_with": best["id"],
            "memory": text,
        }

    if kind == "UPDATE":
        best = plan["best"]
        cur = conn.execute(
            "UPDATE memories SET memory = ?, categories = ?, metadata = ?, embedding = ?, updated_at = ?"
            " WHERE id = ? AND user_id = ? AND updated_at = ?",
            (
                plan["merged"],
                plan["category"],
                meta_json,
                json.dumps(plan["merged_vec"]),
                ts,
                best["id"],
                user_id,
                best["updated_at"],
            ),
        )
        if cur.rowcount == 0:
            raise sqlite3.IntegrityError("Concurrent modification detected during UPDATE")
        _insert_history(
            conn,
            best["id"],
            user_id,
            "UPDATE",
            best["memory"],
            plan["merged"],
            "相关但有新信息(相似度 %.3f)，合并" % plan["best_sim"],
        )
        return {
            "id": best["id"],
            "event": "UPDATE",
            "similarity": round(plan["best_sim"], 4),
            "memory_id": best["id"],
            "old_memory": best["memory"],
            "memory": plan["merged"],
        }

    cur = conn.execute(
        "INSERT INTO memories(user_id, agent_id, memory, categories, metadata, embedding, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (user_id, agent_id, text, plan["category"], meta_json, json.dumps(new_vec), ts, ts),
    )
    _insert_history(
        conn,
        cur.lastrowid,
        user_id,
        "ADD",
        None,
        text,
        "相似度最高 %.3f < 0.80，全新事实" % max(plan["best_sim"], 0.0),
    )
    return {"id": cur.lastrowid, "event": "ADD", "memory_id": cur.lastrowid, "memory": text}


def upsert(user_id, text, agent_id="agent_ou", category=None, provider_cfg=None):
    """单条写入决策：所有外部模型调用均在锁外执行，提交阶段采用 BEGIN IMMEDIATE + 快照版本核验自动重试。"""
    if not text or not isinstance(text, str) or not text.strip():
        return {"event": "NOOP", "reason": "empty input"}
    text = text.strip()
    cat = category or classify(text)
    model_tag = current_embed_model_tag(provider_cfg)
    # 锁外计算新记忆向量，不阻塞其他用户
    new_vec = _embed_with_cfg(text, provider_cfg=provider_cfg)

    for _attempt in range(OCC_MAX_RETRIES):
        conn = get_db()
        try:
            rows = _fetch_user_rows(conn, user_id)
            snap = _snapshot_signature(rows)
        finally:
            conn.close()

        # 锁外规划决策（若命中 UPDATE 也在锁外算好 merged_vec）
        plan = _plan_single_decision(text, new_vec, rows, cat, model_tag, provider_cfg=provider_cfg)

        with _WRITE_LOCK:
            conn = get_db()
            try:
                with _immediate_tx(conn):
                    current_rows = _fetch_user_rows(conn, user_id)
                    if _snapshot_signature(current_rows) != snap:
                        raise _RetryConflictError()
                    return _apply_planned_decision(conn, user_id, agent_id, text, new_vec, model_tag, plan)
            except (_RetryConflictError, sqlite3.IntegrityError):
                continue
            finally:
                conn.close()

    raise RuntimeError("Concurrent write contention exceeded retry limit")


def upsert_batch(user_id, texts, agent_id="agent_ou", category=None, provider_cfg=None):
    """整批原子写入：预计算新向量，并在单一数据库事务中原子演进提交整批消息（任一步失败则整批回滚）。"""
    cleaned = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return [upsert(user_id, cleaned[0], agent_id=agent_id, category=category, provider_cfg=provider_cfg)]

    model_tag = current_embed_model_tag(provider_cfg)
    # 锁外预计算所有输入文本的初始向量
    initial_vecs = [_embed_with_cfg(t, provider_cfg=provider_cfg) for t in cleaned]

    for _attempt in range(OCC_MAX_RETRIES):
        conn = get_db()
        try:
            sim_rows = [dict(r) for r in _fetch_user_rows(conn, user_id)]
            snap = tuple((r["id"], r["updated_at"], r["memory"]) for r in sim_rows)
        finally:
            conn.close()

        # 在锁外模拟整批状态演进，提前算好所有可能触发 UPDATE 的合并向量
        precomputed_merges = {}
        virtual_next_id = -1
        for text, new_vec in zip(cleaned, initial_vecs):
            cat = category or classify(text)
            plan = _plan_single_decision(text, new_vec, sim_rows, cat, model_tag, provider_cfg=provider_cfg)
            if plan["kind"] == "UPDATE":
                precomputed_merges[(plan["best"]["memory"], text)] = plan["merged_vec"]
                for r in sim_rows:
                    if r["id"] == plan["best"]["id"]:
                        r["memory"] = plan["merged"]
                        r["embedding"] = json.dumps(plan["merged_vec"])
                        break
            elif plan["kind"] == "DELETE":
                del_ids = {c["id"] for c in plan["contradictions"]}
                sim_rows = [r for r in sim_rows if r["id"] not in del_ids]
                sim_rows.append({
                    "id": virtual_next_id,
                    "memory": text,
                    "categories": cat,
                    "metadata": json.dumps({"embed_model": model_tag}),
                    "embedding": json.dumps(new_vec),
                    "updated_at": now_iso(),
                })
                virtual_next_id -= 1
            elif plan["kind"] == "ADD":
                sim_rows.append({
                    "id": virtual_next_id,
                    "memory": text,
                    "categories": cat,
                    "metadata": json.dumps({"embed_model": model_tag}),
                    "embedding": json.dumps(new_vec),
                    "updated_at": now_iso(),
                })
                virtual_next_id -= 1

        with _WRITE_LOCK:
            conn = get_db()
            try:
                with _immediate_tx(conn):
                    live_rows = _fetch_user_rows(conn, user_id)
                    if _snapshot_signature(live_rows) != snap:
                        raise _RetryConflictError()
                    results = []
                    for text, new_vec in zip(cleaned, initial_vecs):
                        cat = category or classify(text)
                        cur_rows = _fetch_user_rows(conn, user_id)
                        # 复用锁外算好的 merged_vec，避免在事务内发起远程请求
                        best, best_sim = None, -1.0
                        for r in cur_rows:
                            sim = text_sim(text, new_vec, r["memory"], vec_old=r["embedding"],
                                           model_tag_new=model_tag, old_metadata=r["metadata"])
                            if sim > best_sim:
                                best, best_sim = r, sim
                        contradictions = [r for r in cur_rows if is_contradiction(text, r["memory"])]
                        if contradictions:
                            plan = {"kind": "DELETE", "contradictions": contradictions, "best_sim": best_sim}
                        elif best is not None and best_sim >= THRESHOLD_NOOP:
                            plan = {"kind": "NOOP", "best": best, "best_sim": best_sim}
                        elif best is not None and best_sim >= THRESHOLD_RELATED:
                            merged = best["memory"] + "；" + text
                            m_vec = precomputed_merges.get((best["memory"], text)) or local_embed(merged)
                            plan = {
                                "kind": "UPDATE",
                                "best": best,
                                "best_sim": best_sim,
                                "merged": merged,
                                "merged_vec": m_vec,
                                "category": cat,
                            }
                        else:
                            plan = {"kind": "ADD", "best_sim": best_sim, "category": cat}
                        results.append(_apply_planned_decision(conn, user_id, agent_id, text, new_vec, model_tag, plan))
                    return results
            except (_RetryConflictError, sqlite3.IntegrityError):
                continue
            finally:
                conn.close()

    raise RuntimeError("Concurrent batch write contention exceeded retry limit")


def classify(text):
    if re.search(r"昨天|上周|今天|刚刚|刚才|最近|那次|当时", text):
        return "episodic"
    if re.search(r"步骤|先.*再|第一|第二|如何|怎么|流程", text):
        return "procedural"
    return "semantic"


# ── Part 2：检索（语义召回 + 复合打分）─────────────────────────────────────
def search(user_id, query, limit=5, provider_cfg=None):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, memory, categories, metadata, updated_at, embedding FROM memories WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    model_tag = current_embed_model_tag(provider_cfg)
    q_vec = _embed_with_cfg(query, provider_cfg=provider_cfg)
    now = time.time()

    results = []
    for r in rows:
        stored_vec = _parse_stored_embedding(r["embedding"]) or []
        if stored_vec and len(stored_vec) == len(q_vec) and _models_compatible(model_tag, r["metadata"]):
            sim = cosine(q_vec, stored_vec)
        else:
            sim = 0.0
        cat = (r["categories"] or "semantic").split(",")[0].strip()
        weight = CATEGORY_WEIGHT.get(cat, 0.1)
        try:
            updated = datetime.strptime(r["updated_at"][:19], "%Y-%m-%dT%H:%M:%S")
            days = max((now - updated.replace(tzinfo=timezone.utc).timestamp()) / 86400.0, 0.0)
        except Exception:
            days = 0.0
        final = sim * weight * math.exp(-DECAY * days)
        rounded_final = round(final, 4)
        results.append({
            "id": r["id"], "memory": r["memory"], "categories": cat,
            "similarity": round(sim, 4),
            "category_weight": weight,
            "recency": round(math.exp(-DECAY * days), 4),
            "score": rounded_final,
            "final_score": rounded_final,
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
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_one(memory_id, user_id=None):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, user_id, agent_id, memory, categories, created_at, updated_at"
            " FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if user_id is not None and row["user_id"] != user_id:
        return "FORBIDDEN"
    return dict(row)


def delete_one(memory_id, user_id=None):
    with _WRITE_LOCK:
        conn = get_db()
        try:
            with _immediate_tx(conn):
                row = conn.execute("SELECT user_id, memory FROM memories WHERE id = ?", (memory_id,)).fetchone()
                if row is None:
                    return "NOT_FOUND"
                if user_id is not None and row["user_id"] != user_id:
                    return "FORBIDDEN"
                conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                _insert_history(conn, memory_id, row["user_id"], "DELETE", row["memory"], None, "API 删除")
            return "OK"
        finally:
            conn.close()


def delete_all(user_id):
    with _WRITE_LOCK:
        conn = get_db()
        try:
            with _immediate_tx(conn):
                rows = conn.execute("SELECT id, memory FROM memories WHERE user_id = ?", (user_id,)).fetchall()
                conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
                for r in rows:
                    _insert_history(conn, r["id"], user_id, "DELETE", r["memory"], None, "API 删除全部")
            return len(rows)
        finally:
            conn.close()


def history(user_id=None, limit=50):
    conn = get_db()
    try:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM memory_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_db_ready():
    try:
        conn = get_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ── 请求校验与身份解析辅助 ───────────────────────────────────────────────────
def _parse_limit(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise BadRequestError("limit must be a positive integer")
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, str) and re.fullmatch(r"\d+", raw.strip()):
        val = int(raw.strip())
    else:
        raise BadRequestError("limit must be a positive integer")
    if val < 1 or val > MAX_LIMIT:
        raise BadRequestError("limit must be between 1 and %d" % MAX_LIMIT)
    return val


def _parse_optional_user_id(value, explicit_present=False):
    if value is None:
        if explicit_present:
            raise BadRequestError("user_id must be a non-empty string")
        return None
    if not isinstance(value, str):
        raise BadRequestError("user_id must be a non-empty string")
    cleaned = value.strip()
    if not cleaned:
        if explicit_present:
            raise BadRequestError("user_id must be a non-empty string")
        return None
    return cleaned


def _validate_messages_batch(messages):
    """全批前置校验：在执行任何模型调用或数据库写入前验证整批消息合法性，杜绝半写入。"""
    if not isinstance(messages, list) or len(messages) == 0:
        raise BadRequestError("messages must be a non-empty list")
    validated = []
    for msg in messages:
        if isinstance(msg, dict):
            if "content" not in msg or not isinstance(msg.get("content"), str):
                raise BadRequestError("message content must be a string")
            text = msg["content"]
        elif isinstance(msg, str):
            text = msg
        else:
            raise BadRequestError("each message must be a string or {content: str}")
        if text.strip():
            validated.append(text.strip())
    return validated


def _extract_auth_token(auth_header):
    if not auth_header:
        return ""
    m = re.match(r"^\s*(?:Token|Bearer)\s+(.+?)\s*$", auth_header, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return auth_header.strip()


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

    def _authenticate(self):
        """返回 (authenticated: bool, role: str, bound_user: Optional[str])。
        role 取值：'admin' | 'tenant' | 'service' | 'open' | 'none'。"""
        token = _extract_auth_token(self.headers.get("Authorization", ""))
        tenant_map = parse_tenant_tokens(TENANT_TOKENS_RAW)

        if ADMIN_KEY and token and hmac.compare_digest(token.encode("utf-8"), ADMIN_KEY.encode("utf-8")):
            return True, "admin", None

        if tenant_map:
            matched_tenant = None
            for t_tok, t_uid in tenant_map.items():
                if token and hmac.compare_digest(token.encode("utf-8"), t_tok.encode("utf-8")):
                    matched_tenant = t_uid
            if matched_tenant is not None:
                return True, "tenant", matched_tenant
            # 若启用了多租户绑定表，仅允许已注册租户 Token 或 ADMIN_KEY，拒绝未绑定的共享密钥
            return False, "none", None

        if API_KEY:
            if token and hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8")):
                return True, "service", None
            return False, "none", None

        if ADMIN_KEY:
            # 仅配置了 ADMIN_KEY 而未配 API_KEY 时，未带 ADMIN_KEY 的请求视为普通 open 请求
            return True, "open", None

        return True, "open", None

    def _query_params(self):
        """保留空白参数（keep_blank_values=True）以便对 ?user_id= 返回 400，并拒绝重复同名参数防参数污染。"""
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        out = {}
        for k, vals in params.items():
            if len(vals) > 1:
                raise BadRequestError(f"duplicate query parameter: {k}")
            out[k] = vals[0]
        return out

    def _body(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len == "":
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise BadRequestError("invalid Content-Length header")
        if length < 0:
            raise BadRequestError("invalid Content-Length header")
        if length == 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BadRequestError("request body too large")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BadRequestError("invalid JSON request body")
        if not isinstance(parsed, dict):
            raise BadRequestError("JSON request body must be an object")
        return parsed

    def _resolve_embed_cfg(self):
        return resolve_embed_provider(
            provider_name=(self.headers.get("X-Embed-Provider") or "").strip(),
            custom_url=(self.headers.get("X-Embed-Base-Url") or "").strip(),
            custom_key=(self.headers.get("X-Embed-Api-Key") or "").strip(),
            custom_model=(self.headers.get("X-Embed-Model") or "").strip(),
        )

    def _resolve_user_id(self, query, body, bound_user=None, required=False):
        """解析并核验请求作用域 user_id：
        - 若当前凭据绑定了租户（bound_user），任何跨租户声明一律抛出 ForbiddenError（403），未声明时自动锁定为 bound_user。
        - 显式传入空白（如 ?user_id= 或 {"user_id": ""}）或非字符串一律抛出 BadRequestError（400）。"""
        explicit_present = False
        if "user_id" in query:
            raw = query.get("user_id")
            explicit_present = True
        elif isinstance(body, dict) and "user_id" in body:
            raw = body.get("user_id")
            explicit_present = True
        elif "X-User-Id" in self.headers:
            raw = self.headers.get("X-User-Id")
            explicit_present = True
        else:
            raw = None

        parsed = _parse_optional_user_id(raw, explicit_present=explicit_present)
        if bound_user is not None:
            if parsed is not None and parsed != bound_user:
                raise ForbiddenError("user_id does not match authenticated tenant")
            return bound_user

        if parsed is not None:
            return parsed
        if REQUIRE_USER_ID:
            raise BadRequestError("user_id required")
        return "default" if required else None

    def _dispatch(self, method):
        parsed_url = urllib.parse.urlsplit(self.path)
        path = parsed_url.path.rstrip("/") or "/"

        # 1. 免认证云原生存活与就绪探针（不暴露内部文件路径或敏感配置）
        if method == "GET" and path == "/healthz":
            return self._json(200, {"status": "alive"})
        if method == "GET" and path == "/readyz":
            ready = check_db_ready()
            return self._json(200 if ready else 503, {"status": "ready" if ready else "not_ready", "backend": "sqlite"})

        # 2. 业务端点身份认证
        authed, role, bound_user = self._authenticate()
        if not authed:
            return self._json(401, {"error": "invalid api key"})

        try:
            query = self._query_params()
            body = self._body() if method in ("POST", "DELETE") else {}

            # GET
            if method == "GET":
                if path == "/v1/ping":
                    ready = check_db_ready()
                    payload = {"status": "ok" if ready else "degraded", "backend": "sqlite", "backend_healthy": ready}
                    if role == "admin" or (not API_KEY and not TENANT_TOKENS_RAW):
                        payload["db"] = DB_PATH
                    return self._json(200 if ready else 503, payload)
                m = re.match(r"^/v1/memories/(\d+)$", path)
                if m:
                    uid = self._resolve_user_id(query, {}, bound_user=bound_user, required=False)
                    rec = get_one(int(m.group(1)), user_id=uid)
                    if rec == "FORBIDDEN":
                        return self._json(403, {"error": "not your memory"})
                    if rec is None:
                        return self._json(404, {"error": "not found"})
                    return self._json(200, rec)
                if path == "/v1/history":
                    uid = self._resolve_user_id(query, {}, bound_user=bound_user, required=False)
                    if uid is None and ADMIN_KEY and role != "admin":
                        return self._json(403, {"error": "global history requires admin credentials or explicit user_id"})
                    limit = _parse_limit(query.get("limit"), 50)
                    return self._json(200, {"results": history(user_id=uid, limit=limit)})
                return self._json(404, {"error": "not found"})

            # POST
            if method == "POST":
                if path == "/v1/reset":
                    if not ENABLE_RESET:
                        return self._json(403, {"error": "reset endpoint is disabled"})
                    if role == "tenant":
                        return self._json(403, {"error": "tenant token is not allowed to reset global state"})
                    if ADMIN_KEY and role != "admin":
                        return self._json(403, {"error": "admin credentials required for reset"})
                    reset_db()
                    return self._json(200, {"status": "reset", "seeded": len(SEED)})
                if path == "/v1/memories":
                    user_id = self._resolve_user_id(query, body, bound_user=bound_user, required=True)
                    agent_id = body.get("agent_id", "agent_ou")
                    if not isinstance(agent_id, str) or not agent_id.strip():
                        return self._json(400, {"error": "agent_id must be a non-empty string"})
                    category = body.get("category")
                    if category is not None and (not isinstance(category, str) or category not in CATEGORY_WEIGHT):
                        return self._json(400, {"error": "category must be one of semantic, episodic, procedural"})
                    # 全批前置验证 + 解析受信 embedding provider，任一非法则整批拒绝不落库
                    valid_texts = _validate_messages_batch(body.get("messages"))
                    provider_cfg = self._resolve_embed_cfg()
                    results = upsert_batch(
                        user_id,
                        valid_texts,
                        agent_id=agent_id.strip(),
                        category=category,
                        provider_cfg=provider_cfg,
                    )
                    return self._json(200, {"results": results})
                if path == "/v2/memories":
                    user_id = self._resolve_user_id(query, body, bound_user=bound_user, required=True)
                    limit = _parse_limit(body.get("limit", query.get("limit")), 20)
                    return self._json(200, {"results": list_memories(user_id, limit)})
                if path == "/v2/memories/search":
                    query_text = body.get("query")
                    if not isinstance(query_text, str) or not query_text.strip():
                        return self._json(400, {"error": "query must be a non-empty string"})
                    user_id = self._resolve_user_id(query, body, bound_user=bound_user, required=True)
                    limit = _parse_limit(body.get("limit", query.get("limit")), 5)
                    provider_cfg = self._resolve_embed_cfg()
                    return self._json(
                        200,
                        {"results": search(user_id, query_text.strip(), limit, provider_cfg=provider_cfg)},
                    )
                return self._json(404, {"error": "not found"})

            # DELETE
            if method == "DELETE":
                m = re.match(r"^/v1/memories/(\d+)$", path)
                if m:
                    uid = self._resolve_user_id(query, body, bound_user=bound_user, required=False)
                    res = delete_one(int(m.group(1)), user_id=uid)
                    if res == "FORBIDDEN":
                        return self._json(403, {"error": "not your memory"})
                    if res == "NOT_FOUND":
                        return self._json(404, {"error": "not found"})
                    return self._json(200, {"status": "deleted", "id": int(m.group(1))})
                if path == "/v1/memories":
                    user_id = self._resolve_user_id(query, body, bound_user=bound_user, required=True)
                    n = delete_all(user_id)
                    return self._json(200, {"status": "deleted_all", "user_id": user_id, "count": n})
                return self._json(404, {"error": "not found"})

            return self._json(405, {"error": "method not allowed"})
        except BadRequestError as e:
            return self._json(400, {"error": str(e)})
        except ForbiddenError as e:
            return self._json(403, {"error": str(e)})
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
    else:
        init_db(DB_PATH)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("mini-mem0 server listening on http://%s:%d  (db=%s)" % (HOST, PORT, DB_PATH))
    if TENANT_TOKENS_RAW:
        print("多租户强身份绑定已开启（MEM_TENANT_TOKENS）")
    elif API_KEY:
        print("认证已开启：Authorization: Token <MEM_API_KEY>")
    if REQUIRE_USER_ID:
        print("严格用户隔离已开启：所有业务请求必须携带 user_id")
    srv.serve_forever()


if __name__ == "__main__":
    main()
