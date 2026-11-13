/**
 * AiAS Memory for OpenClaw — powered by NEDB
 *
 * A drop-in OpenClaw context-engine plugin. It replaces OpenClaw's
 * built-in context assembly with the AiAS memory stack, served by the
 * AiAS Memory service (a small Python service that stores everything
 * in NEDB, an embedded journal-backed database):
 *
 *   - conversational fact memory: facts are extracted from each turn,
 *     confidence-filtered (>= 0.75), PII-filtered, deduplicated, and
 *     injected into later prompts as background context;
 *   - a discussion window: the most recent turns, verbatim;
 *   - broadcast directives + a knowledge base layered into every
 *     system prompt.
 *
 * This file targets OpenClaw's documented plugin SDK surface —
 * `api.registerContextEngine(id, factory)` — and talks to the memory
 * service over plain HTTP, so the service can run anywhere:
 *
 *   POST {serviceUrl}/v1/memory/assemble  -> { messages, layers, ... }
 *   POST {serviceUrl}/v1/memory/extract   -> { extracted, fact_count }
 *   GET  {serviceUrl}/health
 *
 * Copyright (c) 2026 INTERCHAINED LLC. MIT License.
 * Distilled from AiAS (aiassistsecure) — see the aias-memory repo.
 */

// Minimal local types for the documented plugin API surface, so this
// file is self-contained and drops in without a build step.
type PluginConfig = {
  serviceUrl?: string;
  enabled?: boolean;
  workspaceId?: string;
  agentId?: string;
};

type ContextEngine = {
  info?: { id: string; name: string };
  bootstrap?: (params: { sessionKey?: string }) => Promise<void>;
  assemble: (params: {
    sessionKey?: string;
    messages?: Array<{ role: string; content: string }>;
    tokenBudget?: number;
  }) => Promise<{ messages: Array<{ role: string; content: string }> }>;
  afterTurn?: (params: {
    sessionKey?: string;
    messages?: Array<{ role: string; content: string }>;
  }) => Promise<void>;
};

type PluginApi = {
  pluginConfig?: PluginConfig;
  registerContextEngine: (id: string, factory: () => ContextEngine) => void;
  logger?: { info?: (msg: string) => void; warn?: (msg: string) => void };
};

const DEFAULT_SERVICE_URL = "http://127.0.0.1:8000";

export default {
  id: "aias-memory",
  name: "AiAS Memory for OpenClaw — powered by NEDB",
  description:
    "AiAS fact memory + discussion window + broadcast directives/KB as an OpenClaw context engine, stored in NEDB.",

  register(api: PluginApi) {
    const cfg = api.pluginConfig ?? {};
    if (cfg.enabled === false) return;

    const serviceUrl = (cfg.serviceUrl ?? DEFAULT_SERVICE_URL).replace(/\/$/, "");
    const workspaceId = cfg.workspaceId ?? "openclaw";
    const agentId = cfg.agentId;

    const post = async (path: string, body: unknown) => {
      const res = await fetch(`${serviceUrl}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`aias-memory ${path}: HTTP ${res.status}`);
      return res.json();
    };

    const lastOf = (
      messages: Array<{ role: string; content: string }> | undefined,
      role: string,
    ) => [...(messages ?? [])].reverse().find((m) => m.role === role)?.content ?? "";

    api.registerContextEngine("aias-memory", () => ({
      info: { id: "aias-memory", name: "AiAS Memory (NEDB)" },

      // Session start: fail soft — if the memory service is down,
      // OpenClaw still runs, assembly just returns messages unchanged.
      async bootstrap() {
        try {
          const res = await fetch(`${serviceUrl}/health`);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          api.logger?.info?.(`aias-memory: connected to ${serviceUrl}`);
        } catch (err) {
          api.logger?.warn?.(
            `aias-memory: service unreachable at ${serviceUrl} (${String(err)}); ` +
              "start it with run.sh from the aias-memory repo.",
          );
        }
      },

      // Before each model call: let the service assemble the prompt —
      // broadcast directives + KB + stored facts + discussion window.
      async assemble(params) {
        try {
          const data = await post("/v1/memory/assemble", {
            workspace_id: workspaceId,
            agent_id: agentId,
            user: params.sessionKey,
            user_message: lastOf(params.messages, "user"),
          });
          if (Array.isArray(data.messages) && data.messages.length > 0) {
            return { messages: data.messages };
          }
        } catch (err) {
          api.logger?.warn?.(`aias-memory: assemble failed (${String(err)}); passing messages through`);
        }
        return { messages: params.messages ?? [] };
      },

      // After each turn: hand the finished turn to the service, which
      // records it in the discussion log and extracts durable facts.
      async afterTurn(params) {
        const userMsg = lastOf(params.messages, "user");
        const aiMsg = lastOf(params.messages, "assistant");
        if (!userMsg && !aiMsg) return;
        try {
          await post("/v1/memory/extract", {
            session_id: `${workspaceId}:${params.sessionKey ?? "default"}`,
            workspace_id: workspaceId,
            user_msg: userMsg,
            ai_response: aiMsg,
            provider: "openai",
          });
        } catch (err) {
          api.logger?.warn?.(`aias-memory: extract failed (${String(err)})`);
        }
      },
    }));
  },
};
