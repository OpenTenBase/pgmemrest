/**
 * PG Memory (mem) Plugin
 *
 * 本地长期记忆，基于 Docker 容器中的 mem 服务。
 * mem 通过 PostgREST REST API 访问远端 PostgreSQL（用户不接触数据库连接信息）。
 * Embedding 由 pg 运行时提供。
 * 用户唯一需要配置的：PostgREST API 地址。
 *
 * API protocol: compatible with Alibaba RDS Long-term Memory API
 *   POST   /v1/memories/          — add memory
 *   POST   /v2/memories/search/   — semantic search
 *   POST   /v2/memories/          — list all
 *   DELETE /v1/memories/{id}/     — delete by id
 *   GET    /v1/ping/              — health check
 */
declare const _default: import("./pg-stub.js").PluginEntryDef;
export default _default;
