/**
 * Stub implementation for pg/plugin-sdk/plugin-entry
 * Used during standalone plugin development.
 * At runtime, pg injects the real implementation.
 */
export type PGPluginApi = {
    pluginConfig: unknown;
    logger: {
        info(msg: string): void;
        warn(msg: string): void;
        error(msg: string): void;
        debug(msg: string): void;
    };
    registerTool(def: ToolDef, opts?: {
        name?: string;
    }): void;
    registerCli(fn: (ctx: {
        program: any;
    }) => void, opts?: {
        commands?: string[];
    }): void;
    registerService(svc: {
        id: string;
        start(): void | Promise<void>;
        stop(): void | Promise<void>;
    }): void;
    on(event: string, handler: (event: any) => any): void;
};
export type ToolDef = {
    name: string;
    label?: string;
    description?: string;
    parameters?: unknown;
    execute(id: string, params: unknown): Promise<{
        content: Array<{
            type: string;
            text: string;
        }>;
        details?: unknown;
    }>;
};
export type PluginEntryDef = {
    id: string;
    name: string;
    description?: string;
    kind?: string;
    configSchema?: {
        parse(v: unknown): unknown;
    };
    register(api: PGPluginApi): void;
};
export declare function definePluginEntry(def: PluginEntryDef): PluginEntryDef;
