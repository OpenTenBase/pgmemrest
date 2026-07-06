---
name: memory-postgrest
description: Install and configure the CloudBase PostgREST-backed long-term memory plugin, and directly operate database tables via CloudBase PostgreSQL RESTful API. Use when the user wants to set up persistent memory, or when asked to perform CRUD operations on database tables (insert, query, update, delete). For memory operations use the built-in memory tools; for direct table operations use CloudBase PostgreSQL RESTful API via curl.
---

# Memory (CloudBase PostgreSQL RESTful API)

本插件提供两种能力：

1. **长期记忆**（通过 mem API） — 使用 `memory_recall` / `memory_store` / `memory_forget` 工具
2. **直接操作数据库表**（通过 CloudBase PostgreSQL RESTful API） — 使用 `curl` 直接调用 CloudBase PostgreSQL RESTful API

---

## 能力一：长期记忆（mem）

三个内置工具：

- **memory_recall** — 语义搜索记忆
- **memory_store** — 存储事实、偏好、决策
- **memory_forget** — 删除记忆（按 ID 或搜索）

agent 在回答前自动召回相关记忆（`autoRecall: true`），对话后自动捕获重要信息（`autoCapture: true`）。

---

## 能力二：直接操作数据库表（CloudBase PostgreSQL RESTful API）

**重要**：当用户要求对数据库表进行 CRUD 操作（建表、插入、查询、更新、删除等）时，**直接通过 CloudBase PostgreSQL RESTful API 调用**，不要走 mem 记忆 API。

### 前提

用户必须提供 CloudBase PostgreSQL RESTful API 的 `api-key` 和 `envid`。如果用户没有提供，请主动询问：

> 请提供你的 CloudBase PostgreSQL RESTful API 的 `api-key` 和 `envid`。

### CloudBase PostgreSQL RESTful API 用法

以下所有操作通过 `curl` 命令执行，`BASE_URL` 替换为 CloudBase PostgreSQL RESTful API 地址，`API_KEY` 替换为用户提供的 `api-key`，`ENV_ID` 替换为用户提供的 `envid`。每次请求都需要携带 `api-key` 和 `envid` 请求头。

#### 查询数据

```bash
# 查询表的所有数据
curl <BASE_URL>/<table_name> \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'

# 条件过滤
curl '<BASE_URL>/<table_name>?<column>=eq.<value>' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'

# 模糊搜索
curl '<BASE_URL>/<table_name>?<column>=like.*<keyword>*' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'

# 指定返回字段
curl '<BASE_URL>/<table_name>?select=<col1>,<col2>' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'

# 排序
curl '<BASE_URL>/<table_name>?order=<column>.desc' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'

# 分页
curl '<BASE_URL>/<table_name>?limit=10&offset=0' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'
```

#### 插入数据

```bash
curl -X POST <BASE_URL>/<table_name> \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>' \
  -H 'Content-Type: application/json' \
  -d '{"<column>": "<value>", ...}'
```

#### 更新数据

```bash
curl -X PATCH '<BASE_URL>/<table_name>?<column>=eq.<value>' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>' \
  -H 'Content-Type: application/json' \
  -d '{"<column>": "<new_value>"}'
```

#### 删除数据

```bash
curl -X DELETE '<BASE_URL>/<table_name>?<column>=eq.<value>' \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'
```

#### 查看所有可用表（OpenAPI 文档）

```bash
curl <BASE_URL>/ \
  -H 'api-key: <API_KEY>' \
  -H 'envid: <ENV_ID>'
```

### 判断逻辑

收到用户请求时，按以下规则判断使用哪种方式：

| 用户意图 | 使用方式 |
|---------|---------|
| "记住…"、"我喜欢…"、"以后…" | → mem 工具（`memory_store`） |
| "搜索记忆"、"你还记得…" | → mem 工具（`memory_recall`） |
| "向 xxx 表插入…"、"查询 xxx 表"、"删除 xxx 表中…"、"建表" | → CloudBase PostgreSQL RESTful API（`curl`） |
| "操作数据库"、"执行 SQL" | → CloudBase PostgreSQL RESTful API（`curl`） |

### 注意事项

- CloudBase PostgreSQL RESTful API 只能操作已暴露的表
- 如果返回 404，说明表不存在或未暴露
- 如果返回 401/403，说明 `api-key` 或 `envid` 错误，或权限不足
- 如果用户要操作的表不存在，提示用户需要先在 PostgreSQL 中创建表并授权

---

## 安装与配置

### 1 — 安装插件

```bash
pg plugins install pg-memory-rest
```

### 2 — 部署本地 mem（需要 Docker）

```bash
pg pgmem setup
```

### 3 — 配置

```bash
# 配置 CloudBase PostgreSQL RESTful API 认证信息
pg config set plugins.entries.memory-postgrest.config.apiKey "<API_KEY>"
pg config set plugins.entries.memory-postgrest.config.envId "<ENV_ID>"

# 可选：覆盖 CloudBase PostgreSQL RESTful API 地址
pg config set plugins.entries.memory-postgrest.config.postgrestUrl "<BASE_URL>"

# 启用 auto-recall
pg config set plugins.entries.memory-postgrest.config.autoRecall true

# 可选：启用 auto-capture
pg config set plugins.entries.memory-postgrest.config.autoCapture true
```

### 4 — 重启 gateway

```bash
pg gateway restart
```

### 5 — 验证

```bash
pg plugins list | grep memory-postgrest
pg pgmem stats
pg pgmem search "test"
```

## Troubleshooting

**mem 不可达** — 运行 `pg pgmem setup` 启动 Docker 容器。

**PostgREST 404** — 表不存在或未暴露；需要在 PostgreSQL 中创建表并授权给对应角色。

**PostgREST 401/403** — `api-key` 或 `envid` 错误，或权限不足；检查 CloudBase PostgreSQL RESTful API 配置。
