/**
 * Re-exports pg plugin SDK types.
 * Points to a local stub during standalone builds;
 * at runtime pg loads the real implementation.
 */
export { definePluginEntry } from "./pg-stub.js";
