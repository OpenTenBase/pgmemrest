# pgmemrest

为 AI Agent / 应用提供基于 PostgreSQL RESTful API（PostgREST）的长期记忆服务。无需本地数据库部署，通过云上 PostgREST 端点即可实现记忆的存储、语义召回与管理。

## 准备

| 依赖 | 用途 |
|------|------|
| **Python 3.8+** | 本地部署 mem 服务 |
| **或 Docker** | 容器化部署 mem 服务 |
| **CloudBase PG 实例** | PostgreSQL + PostgREST 远端端点 |

## 快速开始

> **第 1 步完成后，后续步骤可直接让 AI 根据本 README 自动完成。**

### 1. 创建 CloudBase PG 实例

1. 购买云开发 CloudBase，开通 PG 实例。
2. 在 CloudBase 控制台获取 **Env ID（envid）** 和 **API Key**。
3. 进入 PG 控制台，创建数据库账号（如 `root`）。
4. 使用 DMC 登录，执行初始化建表 SQL：

```sql
begin;

create extension if not exists pgcrypto;
create extension if not exists vector;

create table if not exists public.memories (
    id uuid primary key default gen_random_uuid(),
    user_id text not null default 'default',
    memory text not null,
    embedding vector,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists memories_user_id_created_at_idx
    on public.memories (user_id, created_at desc);

create or replace function public.add_memory(
    p_user_id text,
    p_text text
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    insert into public.memories (
        user_id,
        memory
    )
    values (
        coalesce(nullif(p_user_id, ''), 'default'),
        p_text
    )
    returning id into v_id;

    return v_id;
end;
$$;

create or replace function public.add_memory_with_embedding(
    p_user_id text,
    p_text text,
    p_embedding vector
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    insert into public.memories (
        user_id,
        memory,
        embedding
    )
    values (
        coalesce(nullif(p_user_id, ''), 'default'),
        p_text,
        p_embedding
    )
    returning id into v_id;

    return v_id;
end;
$$;

create or replace function public.search_memories(
    query_embedding vector,
    match_user_id text,
    match_count integer default 5
)
returns table (
    id uuid,
    memory text,
    user_id text,
    created_at timestamptz,
    updated_at timestamptz,
    score double precision
)
language sql
stable
security definer
set search_path = public
as $$
    select
        m.id,
        m.memory,
        m.user_id,
        m.created_at,
        m.updated_at,
        1 - (m.embedding <=> query_embedding) as score
    from public.memories m
    where m.user_id = match_user_id
      and m.embedding is not null
    order by m.embedding <=> query_embedding
    limit greatest(match_count, 1);
$$;

create or replace function public.delete_memory(
    p_memory_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    delete from public.memories
    where id = p_memory_id;

    return found;
end;
$$;

alter table public.memories enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'memories'
          and policyname = 'memories_select_service_role'
    ) then
        create policy memories_select_service_role
        on public.memories
        for select
        to service_role
        using (true);
    end if;
end;
$$;

-- 最小权限说明：PostgREST 需要以 db-service_role-role 解析 public.memories 和 public.* RPC，
-- 因此 schema usage 是命名空间访问权限，不是表读写权限；没有它对象级 grant 仍可能不可用。
grant usage on schema public to service_role;

-- GET /memories 只需要 SELECT；不授予 INSERT/UPDATE/DELETE 表权限。
grant select on public.memories to service_role;

-- POST /rpc/* 只需要对应函数 EXECUTE；写入/删除由 SECURITY DEFINER 函数内部完成。
grant execute on function public.add_memory(text, text) to service_role;
grant execute on function public.add_memory_with_embedding(text, text, vector) to service_role;
grant execute on function public.search_memories(vector, text, integer) to service_role;
grant execute on function public.delete_memory(uuid) to service_role;

notify pgrst, 'reload schema';

commit;

```

> 初始化 SQL 执行完成后，CloudBase 会自动生成 PostgREST RESTful API 端点。

### 2. 安装插件

```bash
npx pg-memory-rest
```

安装过程中会交互式询问:
- CloudBase Env ID
- CloudBase API Key

插件会自动完成:
1. 拷贝插件到 `~/.pg/extensions/memory-postgrest/`
2. 写入 `~/.pg/pg.json` 配置
3. 部署 mem 服务（默认 pip + 本地启动，Docker 用户加 `--docker`）

#### Docker 部署（可选）

```bash
npx pg-memory-rest --docker
```

要求 Docker 已安装且正常运行。

### 3. 重启 gateway

```bash
pg gateway restart
```

### 4. 验证

```bash
# 检查插件状态
pg plugins list | grep memory-postgrest

# 查看 mem 统计
pg pgmem stats

# 测试搜索
pg pgmem search "test"
```

## 配置

安装后可通过 `pg config` 调整配置：

```bash
# CloudBase 认证
pg config set plugins.entries.memory-postgrest.config.apiKey "<API_KEY>"
pg config set plugins.entries.memory-postgrest.config.envId "<ENV_ID>"

# PostgREST 地址（默认由 envId 自动拼接，可覆盖）
pg config set plugins.entries.memory-postgrest.config.postgrestUrl "<URL>"

# 自动召回
pg config set plugins.entries.memory-postgrest.config.autoRecall true

# 自动捕获
pg config set plugins.entries.memory-postgrest.config.autoCapture true
```

## 架构

```
Agent / 应用
    │
    ├── memory_store / memory_recall / memory_forget (工具调用)
    │
    ▼
 pg-memory-rest 插件
    │
    ├── 本地 mem (pip 或 Docker)
    │       │
    │       ▼
    └── PostgREST ──→ CloudBase PostgreSQL
```

- **mem 服务**：本地 HTTP API，负责记忆语义搜索与增删
- **PostgREST**：CloudBase 自动生成的 RESTful API，转发 SQL 操作
- **Embedding**：由 pg 运行时通过请求头传入，无需手动配置

## 卸载

```bash
pg plugins uninstall memory-postgrest
```

## License

MIT
