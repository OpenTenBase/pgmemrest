# mini-mem0 server — lab.md 的本地参考实现

这是 [lab.md](../lab.md) 的**可运行参考答案**：一个纯 Python 标准库（零依赖）、SQLite 存储的迷你 Mem0 服务。学生先在本地把整条链路跑通、理解每个决策，再按 lab.md 在 OpenTenBase 上用 SQL 亲手实现一遍。

## 快速开始

```bash
cd /data/workspace/memserver
python3 server.py            # 默认监听 http://127.0.0.1:8080
# 另开终端
python3 demo.py              # 重放 lab.md 的四幕剧本，观察 ADD/UPDATE/DELETE/NOOP
```

首次启动自动建库并导入 alice 的 2 条既有记忆（lab.md Checkpoint 2）。

## API

| 方法 | 路径 | 说明 | 对应 lab.md |
|---|---|---|---|
| GET | `/v1/ping` | 健康检查 | — |
| POST | `/v1/reset` | 重置并导入剧本初始数据 | Part 1 |
| POST | `/v1/memories` | 写入记忆（内部走四操作决策管线） | **Part 3** |
| POST | `/v2/memories` | 列出某用户记忆 | Part 1 |
| POST | `/v2/memories/search` | 语义检索（相似度 × 类型权重 × 时间新近度） | **Part 2** |
| GET | `/v1/memories/{id}` | 获取单条 | — |
| DELETE | `/v1/memories/{id}` | 删除单条（校验归属） | — |
| DELETE | `/v1/memories` | 删除某用户全部 | — |
| GET | `/v1/history` | 操作审计留痕 | 挑战 A |

```bash
# 手动试用
curl -s -XPOST localhost:8080/v2/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","query":"alice 平时吃什么"}' | python3 -m json.tool
```

## 与 lab.md 的机制对照

| 机制 | 本实现（Python/SQLite） | lab.md（OpenTenBase/pgvector） |
|---|---|---|
| 记忆表 schema | `memories` 表（id/user_id/memory/categories/embedding/时间戳） | Part 1 同构 schema + `DISTRIBUTE BY SHARD(user_id)` |
| 向量 | 确定性 hashing embedding（离线），可切 OpenAI 兼容 API | pgvector 1536 维 `<=>` 余弦距离 |
| 复合打分 | `相似度 × 类型权重(0.6/0.3/0.1) × exp(-0.01×天数)` | Part 3.2 同一公式 |
| 四操作决策 | 矛盾优先 DELETE；≥0.95 NOOP；≥0.80 UPDATE；否则 ADD | Part 3 `memory_upsert` |
| 相似度 | `max(向量余弦, 词汇包含度, 概念关联)`——本地词法 embedding 语义弱，三信号混合使 0.95/0.80 阈值语义成立；pgvector 环境只需余弦 | 仅余弦距离 |
| 矛盾检测 | 规则表（素食↔鸡胸肉 等互斥对）+ 否定前缀识别 | 同为简化规则表，真实 Mem0 由 LLM 判断 |
| 审计留痕 | `memory_history` 表 | 挑战 A |
| 用户隔离 | 所有读写以 `user_id` 为边界 + 归属校验 | 挑战 B（RLS） |

## demo 的四幕剧本（对应 lab.md Part 3）

Agent「小欧」跨会话服务 alice，依次听到四句话，`demo.py` 输出的决策序列：

| 幕 | 新事实 | 决策 | 原因 |
|---|---|---|---|
| 1 | 最近在学 Rust | **ADD** | 与既有记忆相似度 < 0.80，全新领域 |
| 2 | 是素食者，平时不吃肉 | **NOOP** | 词汇包含度 1.0 ≥ 0.95，几乎重复既有记忆 |
| 3 | 最近开始吃鸡胸肉了 | **DELETE** | 与"素食"记忆矛盾（互斥状态：素食 ↔ 鸡胸肉），旧记忆删除、新事实写入 |
| 4 | 喜欢看科幻电影，不喜欢惊悚片 | **NOOP** | 重复了电影偏好，无新信息 |

再补一句"每天喝手冲咖啡"（已有"喜欢拿铁咖啡"）可观察 **UPDATE**：相关但有新信息 → 合并。

## 配置（均为环境变量，密钥不落盘）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEM_PORT` / `MEM_HOST` | 8080 / 127.0.0.1 | 监听地址 |
| `MEM_DB_PATH` | `./memories.db` | SQLite 文件 |
| `MEM_API_KEY` | 空（不认证） | 设置后需 `Authorization: Token <key>` |
| `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL` | 空 | 配置后切换为远端 OpenAI 兼容 embedding |

## 建议的学生路径

1. 跑 `demo.py`，对照 lab.md Checkpoint 4 思考：幕 3/幕 4 为什么决策不同？"素食"那条该不该在？
2. 读 `server.py` 中的 `upsert()`（约 60 行），这是整个记忆系统的灵魂。
3. 按 lab.md 在 OpenTenBase 上用 SQL 重写同样的逻辑（Part 1→3），本实现即验收对照答案。
