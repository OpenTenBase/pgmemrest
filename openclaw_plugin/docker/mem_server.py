#!/usr/bin/env python3
"""
mem_server.py — 本地 mem HTTP API 服务，为 pg memory-rest 插件提供记忆存储能力。

通过 PostgREST REST API 访问远端 PostgreSQL，不直接连接数据库。
Embedding 由 pg 运行时提供（调用时通过请求头传入 API 配置）。

依赖的 PostgREST 端点：
  GET  /memories                         — api.memories 视图（id, memory, user_id, hash, created_at, updated_at）
  POST /rpc/add_memory                   — api.add_memory(p_user_id, p_text) 不含向量
  POST /rpc/add_memory_with_embedding    — api.add_memory_with_embedding(p_user_id, p_text, p_embedding) 含向量
  POST /rpc/search_memories              — api.search_memories(query_embedding, match_user_id, match_count) 向量搜索
  POST /rpc/delete_memory                — api.delete_memory(p_memory_id)

本服务暴露的 HTTP 端点：
  GET    /v1/ping/              — 健康检查
  POST   /v1/memories/          — 添加记忆
  POST   /v2/memories/          — 列出记忆
  POST   /v2/memories/search/   — 语义搜索（需远端有 rpc/search_memories）
  DELETE /v1/memories/{id}/     — 按 ID 删除
  DELETE /v1/memories/          — 删除用户所有记忆
  GET    /v1/memories/{id}/     — 获取单条记忆

认证方式: Authorization: Token <MEM_API_KEY>
"""

import os
import logging
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mem_server")

# ── Config ──────────────────────────────────────────────────────────────────

POSTGREST_URL = os.environ.get("POSTGREST_URL", "")
CLOUDBASE_API_KEY = os.environ.get("CLOUDBASE_API_KEY", "")
CLOUDBASE_ENV_ID = os.environ.get("CLOUDBASE_ENV_ID", "")
SERVER_API_KEY = os.environ.get("MEM_API_KEY", "mem-demo-key")

# pg 运行时传入的 embedding 配置（通过请求头或环境变量）
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "hunyuan-embedding")

# ── PostgREST Client ────────────────────────────────────────────────────────

class PostgRESTClient:
    """通过 PostgREST REST API 操作远端 PostgreSQL 中的记忆数据。
    
    远端已暴露：
      GET  /memories                         — api.memories 视图
      POST /rpc/add_memory                   — 写入记忆（不含向量）
      POST /rpc/add_memory_with_embedding    — 写入记忆（含 embedding 向量）
      POST /rpc/search_memories              — 向量相似度搜索
      POST /rpc/delete_memory                — 删除记忆
    """

    def __init__(self, base_url: str, api_key: str = "", env_id: str = ""):
        self.api_key = api_key
        self.env_id = env_id
        self.base_url = self._normalize_base_url(base_url, env_id)
        self.client = httpx.AsyncClient(timeout=30.0)

    def _normalize_base_url(self, base_url: str, env_id: str) -> str:
        """规范化 CloudBase PostgreSQL RESTful API 地址。"""
        return base_url.rstrip("/") if base_url else ""

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造 CloudBase PostgreSQL RESTful API 请求头。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "envid": self.env_id,
        }
        if extra:
            headers.update(extra)
        return {k: v for k, v in headers.items() if v}

    async def list_memories(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """列出某用户的记忆（通过 GET /memories 视图）"""
        resp = await self.client.get(
            f"{self.base_url}/memories",
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": str(limit),
                "offset": str(offset),
            },
            headers=self._headers({"Accept": "application/json"}),
        )
        if resp.status_code != 200:
            logger.error(f"list_memories failed: {resp.status_code} {resp.text}")
            return []
        return resp.json()

    async def get_by_id(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取单条记忆"""
        resp = await self.client.get(
            f"{self.base_url}/memories",
            params={"id": f"eq.{memory_id}"},
            headers=self._headers({"Accept": "application/json"}),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data[0] if data else None

    async def add_memory(self, user_id: str, text: str) -> Optional[str]:
        """通过 RPC 写入一条记忆（不含向量），返回新记忆的 UUID"""
        resp = await self.client.post(
            f"{self.base_url}/rpc/add_memory",
            json={"p_user_id": user_id, "p_text": text},
            headers=self._headers({
                "Content-Type": "application/json",
                "Accept": "application/json",
            }),
        )
        if resp.status_code != 200:
            raise Exception(f"add_memory RPC failed: {resp.status_code} {resp.text}")
        result = resp.json()
        if isinstance(result, str):
            return result
        return str(result) if result else None

    async def add_memory_with_embedding(
        self, user_id: str, text: str, embedding: List[float]
    ) -> Optional[str]:
        """通过 RPC 写入一条记忆（含 embedding 向量），返回新记忆的 UUID"""
        resp = await self.client.post(
            f"{self.base_url}/rpc/add_memory_with_embedding",
            json={
                "p_user_id": user_id,
                "p_text": text,
                "p_embedding": embedding,
            },
            headers=self._headers({
                "Content-Type": "application/json",
                "Accept": "application/json",
            }),
        )
        if resp.status_code != 200:
            raise Exception(
                f"add_memory_with_embedding RPC failed: {resp.status_code} {resp.text}"
            )
        result = resp.json()
        if isinstance(result, str):
            return result
        return str(result) if result else None

    async def delete_memory(self, memory_id: str) -> bool:
        """通过 RPC 删除一条记忆"""
        resp = await self.client.post(
            f"{self.base_url}/rpc/delete_memory",
            json={"p_memory_id": memory_id},
            headers=self._headers({"Content-Type": "application/json"}),
        )
        return resp.status_code == 200

    async def delete_by_user(self, user_id: str) -> bool:
        """删除某用户的所有记忆（通过逐条删除）"""
        memories = await self.list_memories(user_id, limit=10000)
        for m in memories:
            await self.delete_memory(m["id"])
        return True

    async def search_text(
        self, user_id: str, keyword: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """关键词模糊搜索（降级方案，当向量搜索不可用时使用）"""
        resp = await self.client.get(
            f"{self.base_url}/memories",
            params={
                "user_id": f"eq.{user_id}",
                "memory": f"like.*{keyword}*",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=self._headers({"Accept": "application/json"}),
        )
        if resp.status_code != 200:
            return []
        return resp.json()

    async def search_by_embedding(
        self, user_id: str, embedding: List[float], limit: int = 5
    ) -> List[Dict[str, Any]]:
        """通过 PostgREST RPC 调用向量相似度搜索。
        
        远端已创建 api.search_memories 函数。
        如果调用失败，会返回空列表并降级到关键词搜索。
        """
        try:
            resp = await self.client.post(
                f"{self.base_url}/rpc/search_memories",
                json={
                    "query_embedding": embedding,
                    "match_user_id": user_id,
                    "match_count": limit,
                },
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }),
            )
            if resp.status_code != 200:
                logger.warning(
                    f"search_memories RPC not available ({resp.status_code}), "
                    f"vector search disabled"
                )
                return []
            return resp.json()
        except Exception as e:
            logger.warning(f"search_memories RPC error: {e}")
            return []

    async def health_check(self) -> bool:
        """检查 PostgREST 是否可达"""
        try:
            resp = await self.client.get(
                f"{self.base_url}/memories",
                params={"select": "id", "limit": "1"},
                headers=self._headers({"Accept": "application/json"}),
                timeout=5.0,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ── Embedding Client ─────────────────────────────────────────────────────────

class EmbeddingClient:
    """调用 OpenAI 兼容的 Embedding API。
    配置从 pg 运行时获取（环境变量或请求头传入）。
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    async def embed(
        self,
        text: str,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
    ) -> Optional[List[float]]:
        """生成文本的 embedding 向量，失败返回 None"""
        url = (base_url or EMBED_BASE_URL).rstrip("/")
        key = api_key or EMBED_API_KEY
        mdl = model or EMBED_MODEL

        if not url or not key:
            logger.warning("Embedding 配置缺失，向量搜索不可用")
            return None

        try:
            resp = await self.client.post(
                f"{url}/embeddings",
                json={"input": text, "model": mdl},
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                logger.error(f"Embedding API failed: {resp.status_code} {resp.text}")
                return None

            data = resp.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return None


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="mem local server", version="2.0.0")
pg_client: PostgRESTClient = None
embed_client: EmbeddingClient = None


@app.on_event("startup")
async def startup():
    global pg_client, embed_client

    if not CLOUDBASE_API_KEY:
        logger.error("CLOUDBASE_API_KEY 未设置！请通过环境变量提供 CloudBase PostgreSQL RESTful API 的 api-key")

    if not CLOUDBASE_ENV_ID:
        logger.error("CLOUDBASE_ENV_ID 未设置！请通过环境变量提供 CloudBase 环境 ID（envid）")

    if not POSTGREST_URL:
        logger.error("POSTGREST_URL 未设置！请通过环境变量提供 CloudBase PostgreSQL RESTful API 地址")
    else:
        logger.info(f"PostgREST URL: {POSTGREST_URL}")

    pg_client = PostgRESTClient(POSTGREST_URL, CLOUDBASE_API_KEY, CLOUDBASE_ENV_ID)
    embed_client = EmbeddingClient()

    # 检查 PostgREST 连通性
    if pg_client and pg_client.base_url:
        healthy = await pg_client.health_check()
        if healthy:
            logger.info("PostgREST 连接正常 ✅")
        else:
            logger.warning(f"PostgREST 不可达: {pg_client.base_url}")

    logger.info("mem server ready (PostgREST backend)")


def verify_token(authorization: Optional[str]):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    token = authorization.replace("Token ", "").replace("Bearer ", "").strip()
    if token != SERVER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def extract_embed_config(request: Request) -> Dict[str, str]:
    """从请求头中提取 pg 传入的 embedding 配置"""
    return {
        "base_url": request.headers.get("X-Embed-Base-Url", ""),
        "api_key": request.headers.get("X-Embed-Api-Key", ""),
        "model": request.headers.get("X-Embed-Model", ""),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/v1/ping/")
async def ping():
    pg_ok = await pg_client.health_check() if pg_client and pg_client.base_url else False
    return {"status": "ok", "postgrest": pg_ok}


@app.post("/v1/memories/")
async def add_memories(request: Request, authorization: Optional[str] = Header(None)):
    """添加记忆 — 优先带 embedding 写入，embed 失败则降级为纯文本写入"""
    verify_token(authorization)
    data = await request.json()
    messages = data.get("messages")
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    user_id = data.get("user_id", "default")
    embed_cfg = extract_embed_config(request)

    try:
        results = []
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            if not content.strip():
                continue

            # 尝试生成 embedding，带向量写入
            embedding = await embed_client.embed(content, **embed_cfg)
            if embedding:
                new_id = await pg_client.add_memory_with_embedding(
                    user_id, content, embedding
                )
                logger.info(f"写入记忆（含向量）: {content[:50]}...")
            else:
                # embedding 失败，降级为纯文本写入
                new_id = await pg_client.add_memory(user_id, content)
                logger.warning(f"写入记忆（无向量，embedding 失败）: {content[:50]}...")

            results.append({
                "id": new_id or "unknown",
                "memory": content,
                "event": "ADD",
            })

        return JSONResponse(content={"results": results})
    except Exception as e:
        logger.error(f"add error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/memories/{memory_id}/")
async def get_memory(memory_id: str, authorization: Optional[str] = Header(None)):
    """获取单条记忆 — 通过 GET /memories?id=eq.xxx"""
    verify_token(authorization)
    try:
        record = await pg_client.get_by_id(memory_id)
        if not record:
            raise HTTPException(status_code=404, detail="Memory not found")
        return JSONResponse(content=record)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/v1/memories/{memory_id}/")
async def delete_memory(memory_id: str, authorization: Optional[str] = Header(None)):
    """删除单条记忆 — 通过 PostgREST rpc/delete_memory"""
    verify_token(authorization)
    try:
        ok = await pg_client.delete_memory(memory_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Delete failed")
        return JSONResponse(content={"status": "deleted", "id": memory_id})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/v1/memories/")
async def delete_all(request: Request, authorization: Optional[str] = Header(None)):
    """删除某用户的所有记忆"""
    verify_token(authorization)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_id = body.get("user_id", "default")
    try:
        await pg_client.delete_by_user(user_id)
        return JSONResponse(content={"status": "deleted_all", "user_id": user_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v2/memories/")
async def list_memories(request: Request, authorization: Optional[str] = Header(None)):
    """列出记忆 — 通过 GET /memories 视图"""
    verify_token(authorization)
    data = await request.json()
    user_id = data.get("user_id", "default")
    limit = data.get("limit", 20)
    try:
        records = await pg_client.list_memories(user_id, limit)
        return JSONResponse(content={"results": records})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v2/memories/search/")
async def search_memories(request: Request, authorization: Optional[str] = Header(None)):
    """语义搜索记忆。
    
    优先尝试向量搜索（需远端有 rpc/search_memories），
    如果不可用则降级为关键词模糊匹配。
    """
    verify_token(authorization)
    data = await request.json()
    query = data.get("query")
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    user_id = data.get("user_id", "default")
    limit = data.get("limit", 5)
    embed_cfg = extract_embed_config(request)

    try:
        # 尝试向量搜索
        embedding = await embed_client.embed(query, **embed_cfg)
        if embedding:
            results = await pg_client.search_by_embedding(user_id, embedding, limit)
            if results:
                return JSONResponse(content={"results": results})
            # 向量搜索返回空（可能 RPC 不存在），降级到关键词搜索
            logger.info("向量搜索无结果，降级到关键词搜索")

        # 降级：关键词模糊搜索
        # 提取查询中的关键词（支持中文：按字符级别提取 2-3 字的片段）
        import re
        # 先按空格分词（英文），再对中文提取连续字符片段
        words = [w for w in query.split() if len(w) >= 2]
        # 提取中文字符序列
        chinese_parts = re.findall(r'[\u4e00-\u9fff]+', query)
        for part in chinese_parts:
            # 对每段中文，提取 2 字的滑动窗口片段作为关键词
            if len(part) >= 2:
                for i in range(len(part) - 1):
                    words.append(part[i:i+2])
        # 去重并保持顺序
        seen_kw = set()
        keywords = []
        for w in words:
            if w not in seen_kw:
                seen_kw.add(w)
                keywords.append(w)
        if not keywords:
            keywords = [query]

        all_results = []
        seen_ids = set()
        for kw in keywords[:3]:  # 最多用前 3 个关键词
            matches = await pg_client.search_text(user_id, kw, limit)
            for m in matches:
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    all_results.append(m)

        return JSONResponse(content={"results": all_results[:limit]})
    except Exception as e:
        logger.error(f"search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("MEM_PORT", "8080"))
    logger.info(f"Starting on 0.0.0.0:{port}")
    logger.info(f"PostgREST URL: {POSTGREST_URL or '(未设置)'}")
    uvicorn.run(app, host="0.0.0.0", port=port)
