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
 *   4. 交互式询问用户 CloudBase Env ID 和 API Key
 *
 * Idempotent — safe to run multiple times.
 */
import { execSync, spawn } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
const PLUGIN_ID = "memory-postgrest";
const __dirname = dirname(fileURLToPath(import.meta.url));
// bin/install.js → package root
const PACKAGE_ROOT = resolve(__dirname, "..");
// ─── Helpers ────────────────────────────────────────────────────────────────
function log(msg) {
    console.log(`  ${msg}`);
}
function resolvePGConfigDir() {
    if (process.env.PG_STATE_DIR)
        return process.env.PG_STATE_DIR;
    const profile = process.env.PG_PROFILE;
    if (profile && profile !== "default")
        return join(homedir(), `.pg-${profile}`);
    return join(homedir(), ".pg");
}
function dockerAvailable() {
    try {
        execSync("docker info --format '{{.ID}}'", { stdio: "ignore" });
        return true;
    }
    catch {
        return false;
    }
}
function pythonAvailable() {
    for (const cmd of ["python3.12", "python3.11", "python3.10", "python3.9", "python3.8", "python3", "python"]) {
        try {
            const ver = execSync(`${cmd} --version`, { stdio: "pipe" }).toString().trim();
            const match = ver.match(/Python (\d+)\.(\d+)/);
            if (match) {
                const major = parseInt(match[1]);
                const minor = parseInt(match[2]);
                if (major >= 3 && minor >= 8)
                    return true;
            }
        }
        catch {
            // 继续
        }
    }
    return false;
}
function getPythonCmd() {
    // 优先选择高版本 Python（依赖包兼容性更好）
    for (const cmd of ["python3.12", "python3.11", "python3.10", "python3.9", "python3.8", "python3", "python"]) {
        try {
            const ver = execSync(`${cmd} --version`, { stdio: "pipe" }).toString().trim();
            const match = ver.match(/Python (\d+)\.(\d+)/);
            if (match) {
                const major = parseInt(match[1]);
                const minor = parseInt(match[2]);
                if (major >= 3 && minor >= 8)
                    return cmd;
            }
        }
        catch {
            // 继续
        }
    }
    return "python3";
}
async function askQuestion(prompt) {
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    return new Promise((resolve) => {
        rl.question(prompt, (answer) => {
            rl.close();
            resolve(answer.trim());
        });
    });
}
function buildCloudBasePostgrestUrl(envId) {
    return `https://${envId}.api.tcloudbasegateway.com/v1/rdb/rest`;
}
// ─── Step 1: Copy plugin to extensions dir ──────────────────────────────────
function installToExtensions(configDir) {
    const extensionsDir = join(configDir, "extensions");
    const targetDir = join(extensionsDir, PLUGIN_ID);
    mkdirSync(extensionsDir, { recursive: true });
    // Remove old installation if present
    if (existsSync(targetDir)) {
        rmSync(targetDir, { recursive: true, force: true });
    }
    // Copy entire package (dist, docker, pg.plugin.json, package.json)
    cpSync(PACKAGE_ROOT, targetDir, {
        recursive: true,
        filter: (src) => {
            const name = src.split("/").pop() ?? "";
            return !["node_modules", "src", ".git", "tsconfig.json"].includes(name);
        },
    });
    // Install production deps in target dir
    try {
        execSync("npm install --omit=dev --ignore-scripts", {
            cwd: targetDir,
            stdio: "pipe",
        });
    }
    catch {
        // Non-fatal
    }
    return targetDir;
}
// ─── Step 2: Write pg.json config ─────────────────────────────────────
function writeConfig(configDir, installPath, cloudbaseConfig) {
    const configPath = join(configDir, "pg.json");
    let config;
    if (existsSync(configPath)) {
        try {
            config = JSON.parse(readFileSync(configPath, "utf-8"));
        }
        catch {
            config = {};
        }
    }
    else {
        config = {};
    }
    // Ensure plugins object
    if (!config.plugins || typeof config.plugins !== "object" || Array.isArray(config.plugins)) {
        config.plugins = {};
    }
    const plugins = config.plugins;
    // slots.memory
    if (!plugins.slots || typeof plugins.slots !== "object" || Array.isArray(plugins.slots)) {
        plugins.slots = {};
    }
    plugins.slots.memory = PLUGIN_ID;
    // allow
    if (!Array.isArray(plugins.allow))
        plugins.allow = [];
    if (!plugins.allow.includes(PLUGIN_ID)) {
        plugins.allow.push(PLUGIN_ID);
    }
    // entries — 写入 CloudBase 配置
    if (!plugins.entries || typeof plugins.entries !== "object" || Array.isArray(plugins.entries)) {
        plugins.entries = {};
    }
    const entries = plugins.entries;
    const pluginConfig = {
        apiKey: cloudbaseConfig.apiKey,
        envId: cloudbaseConfig.envId,
        postgrestUrl: cloudbaseConfig.postgrestUrl,
    };
    if (!entries[PLUGIN_ID] || typeof entries[PLUGIN_ID] !== "object") {
        entries[PLUGIN_ID] = { enabled: true, config: pluginConfig };
    }
    else {
        const entry = entries[PLUGIN_ID];
        entry.enabled = true;
        if (!entry.config || typeof entry.config !== "object") {
            entry.config = pluginConfig;
        }
        else {
            entry.config.apiKey = cloudbaseConfig.apiKey;
            entry.config.envId = cloudbaseConfig.envId;
            entry.config.postgrestUrl = cloudbaseConfig.postgrestUrl;
        }
    }
    // installs record
    if (!plugins.installs || typeof plugins.installs !== "object" || Array.isArray(plugins.installs)) {
        plugins.installs = {};
    }
    plugins.installs[PLUGIN_ID] = {
        source: "npm",
        sourcePath: `@pg/${PLUGIN_ID}`,
        installPath,
        version: JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf-8")).version,
        installedAt: new Date().toISOString(),
    };
    mkdirSync(configDir, { recursive: true });
    writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n", "utf-8");
    return true;
}
// ─── Step 3a: Docker 部署（备选，需 --docker 参数） ─────────────────────────
function startDocker(installPath, cloudbaseConfig) {
    const cf = join(installPath, "docker", "docker-compose.yml");
    if (!existsSync(cf)) {
        log("⚠  docker-compose.yml not found; skip Docker setup.");
        return false;
    }
    if (!dockerAvailable()) {
        log("❌  Docker not running. Please start Docker and retry.");
        return false;
    }
    log("🐳 Starting mem via Docker…");
    try {
        execSync(`CLOUDBASE_API_KEY="${cloudbaseConfig.apiKey}" CLOUDBASE_ENV_ID="${cloudbaseConfig.envId}" POSTGREST_URL="${cloudbaseConfig.postgrestUrl}" docker compose -f "${cf}" up -d --build`, { stdio: "inherit" });
        return true;
    }
    catch {
        log("⚠  Docker Compose failed.");
        return false;
    }
}
// ─── Step 3b: 本地 pip 部署（默认） ─────────────────────────────────────────
function startLocalMem(installPath, cloudbaseConfig) {
    const serverScript = join(installPath, "docker", "mem_server.py");
    if (!existsSync(serverScript)) {
        log(`⚠  mem_server.py not found at: ${serverScript}`);
        return false;
    }
    // 安装 Python 依赖
    const pythonCmd = getPythonCmd();
    const deps = ["fastapi", "uvicorn", "httpx"];
    log("📦 Installing Python dependencies via pip…");
    try {
        execSync(`${pythonCmd} -m pip install --quiet ${deps.join(" ")}`, {
            stdio: "inherit",
        });
    }
    catch {
        log("⚠  pip install failed. Please install manually:");
        log(`   ${pythonCmd} -m pip install ${deps.join(" ")}`);
        return false;
    }
    log("🚀 Starting mem_server.py in background…");
    try {
        const child = spawn(pythonCmd, [serverScript], {
            stdio: "ignore",
            detached: true,
            env: {
                ...process.env,
                MEM_PORT: process.env.MEM_PORT || "8080",
                MEM_API_KEY: process.env.MEM_API_KEY || "mem-demo-key",
                CLOUDBASE_API_KEY: cloudbaseConfig.apiKey,
                CLOUDBASE_ENV_ID: cloudbaseConfig.envId,
                POSTGREST_URL: cloudbaseConfig.postgrestUrl,
            },
        });
        child.unref();
        log(`   → PID: ${child.pid}`);
        return true;
    }
    catch (e) {
        log(`⚠  Failed to start mem_server.py: ${String(e)}`);
        return false;
    }
}
// ─── Main ───────────────────────────────────────────────────────────────────
async function main() {
    const useDocker = process.argv.includes("--docker");
    console.log();
    console.log(`🔌 Installing ${PLUGIN_ID}…`);
    console.log();
    const configDir = resolvePGConfigDir();
    // Step 1
    log("📦 Copying plugin to extensions…");
    const installPath = installToExtensions(configDir);
    log(`   → ${installPath}`);
    // 询问用户 CloudBase 配置
    let envId = process.env.CLOUDBASE_ENV_ID || "";
    if (!envId) {
        envId = await askQuestion("\n🌐 请输入 CloudBase Env ID（envid）: ");
    }
    if (!envId) {
        console.error("❌  CloudBase Env ID 不能为空。");
        process.exit(1);
    }
    let apiKey = process.env.CLOUDBASE_API_KEY || "";
    if (!apiKey) {
        apiKey = await askQuestion("\n🔑 请输入 CloudBase API Key（api-key）: ");
    }
    if (!apiKey) {
        console.error("❌  CloudBase API Key 不能为空。");
        process.exit(1);
    }
    const postgrestUrl = process.env.POSTGREST_URL || buildCloudBasePostgrestUrl(envId);
    const cloudbaseConfig = { apiKey, envId, postgrestUrl };
    // Step 2
    log("⚙️  Writing pg config…");
    writeConfig(configDir, installPath, cloudbaseConfig);
    log("   → plugins.slots.memory = memory-postgrest");
    log(`   → envId = ${envId}`);
    log(`   → postgrestUrl = ${postgrestUrl}`);
    // Step 3: 部署 mem 服务
    let deployOk = false;
    if (useDocker) {
        // 用户明确要求 Docker 部署
        if (!dockerAvailable()) {
            console.error("❌  Docker is not running. Please start Docker and retry.");
            console.error("    Or remove --docker flag to use local pip deployment (requires Python 3.8+).");
            process.exit(1);
        }
        deployOk = startDocker(installPath, cloudbaseConfig);
    }
    else {
        // 默认：本地 pip 部署
        if (!pythonAvailable()) {
            console.error("❌  Python 3.8+ not found. Please install Python.");
            console.error("    Or use: npx pg-memory-rest --docker (requires Docker)");
            process.exit(1);
        }
        deployOk = startLocalMem(installPath, cloudbaseConfig);
    }
    // Done
    console.log();
    if (deployOk) {
        console.log("✅ Done! Restart the pg gateway to activate long-term memory.");
        console.log(`    CloudBase Env ID: ${envId}`);
    }
    else {
        console.log("✅ Plugin installed. Start mem service + restart gateway to activate.");
        console.log("   Local:  pg pgmem setup");
        console.log("   Docker: pg pgmem setup --docker");
    }
    console.log();
}
main();
