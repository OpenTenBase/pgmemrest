/**
 * postinstall.ts
 *
 * Runs automatically after `npm install` (i.e., after `/plugins install pg-memory-rest`).
 * Writes the minimum required config into pg.json so the user doesn't have to:
 *   - plugins.slots.memory = "memory-postgrest"  (activate this plugin as the memory provider)
 *   - plugins.allow = ["memory-postgrest"]        (suppress untrusted-plugin warning)
 *   - plugins.entries.memory-postgrest.enabled = true
 *
 * All existing config is preserved. This script is idempotent.
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
const PLUGIN_ID = "memory-postgrest";
function resolvePGConfigDir() {
    // Respect PG_STATE_DIR (set by --profile flag)
    if (process.env.PG_STATE_DIR) {
        return process.env.PG_STATE_DIR;
    }
    const profile = process.env.PG_PROFILE;
    if (profile && profile !== "default") {
        return join(homedir(), `.pg-${profile}`);
    }
    return join(homedir(), ".pg");
}
function run() {
    const configDir = resolvePGConfigDir();
    const configPath = join(configDir, "pg.json");
    if (!existsSync(configPath)) {
        // No pg config found — skip silently (pg may not be installed yet,
        // or the user is running npm install outside of pg context).
        return;
    }
    let config;
    try {
        const raw = readFileSync(configPath, "utf-8");
        config = JSON.parse(raw);
    }
    catch {
        // Corrupt config — don't touch it.
        return;
    }
    let changed = false;
    // Ensure plugins object exists
    if (!config.plugins || typeof config.plugins !== "object" || Array.isArray(config.plugins)) {
        config.plugins = {};
        changed = true;
    }
    const plugins = config.plugins;
    // 1. Set memory slot to this plugin
    if (!plugins.slots || typeof plugins.slots !== "object" || Array.isArray(plugins.slots)) {
        plugins.slots = {};
        changed = true;
    }
    const slots = plugins.slots;
    if (slots.memory !== PLUGIN_ID) {
        slots.memory = PLUGIN_ID;
        changed = true;
    }
    // 2. Add to allow list
    if (!Array.isArray(plugins.allow)) {
        plugins.allow = [];
        changed = true;
    }
    const allow = plugins.allow;
    if (!allow.includes(PLUGIN_ID)) {
        allow.push(PLUGIN_ID);
        changed = true;
    }
    // 3. Ensure entry exists and is enabled
    if (!plugins.entries || typeof plugins.entries !== "object" || Array.isArray(plugins.entries)) {
        plugins.entries = {};
        changed = true;
    }
    const entries = plugins.entries;
    if (!entries[PLUGIN_ID] ||
        typeof entries[PLUGIN_ID] !== "object" ||
        Array.isArray(entries[PLUGIN_ID])) {
        entries[PLUGIN_ID] = { enabled: true };
        changed = true;
    }
    else {
        const entry = entries[PLUGIN_ID];
        if (entry.enabled !== true) {
            entry.enabled = true;
            changed = true;
        }
    }
    if (!changed) {
        // Already configured — nothing to do.
        return;
    }
    try {
        writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n", "utf-8");
        console.log(`\n✅  memory-postgrest: pg configured (memory slot activated).\n` +
            `    Next steps:\n` +
            `    1. Configure PostgREST URL: pg plugins config memory-postgrest postgrestUrl=<your-postgrest-url>\n` +
            `    2. Start mem service:      pg pgmem setup\n` +
            `    3. Restart the pg gateway to apply.\n`);
    }
    catch (err) {
        // Write failed (e.g. permissions) — non-fatal. User can set manually.
        console.warn(`\n⚠️  memory-postgrest: could not update ${configPath}: ${String(err)}`);
        console.warn(`    Add manually: plugins.slots.memory = "${PLUGIN_ID}"\n`);
    }
}
run();
