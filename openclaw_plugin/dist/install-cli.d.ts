#!/usr/bin/env node
/**
 * Self-installer for pg-memory-rest
 *
 * Usage:   npx pg-memory-rest
 *          npx pg-memory-rest --docker   (备选：Docker 容器部署)
 *
 * Does everything in one shot:
 *   1. Copies the plugin into ~/.pg/extensions/memory-postgrest/
 *   2. Writes plugins.slots.memory + allow + entries into pg.json
 *   3. 部署 mem 服务：
 *      - 默认：本地 pip 安装依赖 + 后台启动 mem_server.py
 *      - --docker：Docker 容器（自动拉起 mem 服务）
 *   4. 交互式询问用户 PostgREST API 地址（唯一需要的配置）
 *
 * Idempotent — safe to run multiple times.
 */
export {};
