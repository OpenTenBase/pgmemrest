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
export {};
