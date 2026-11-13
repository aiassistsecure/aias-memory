// openclaw/index.ts
var DEFAULT_SERVICE_URL = "http://127.0.0.1:8000";
var isLoopbackHost = (hostname) => {
  const h = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return h === "localhost" || h.endsWith(".localhost") || h === "::1" || /^127(?:\.\d{1,3}){3}$/.test(h);
};
var resolveServiceUrl = (raw) => {
  const trimmed = raw.replace(/\/$/, "");
  let url;
  try {
    url = new URL(trimmed);
  } catch {
    throw new Error(`aias-memory: serviceUrl is not a valid URL: ${raw}`);
  }
  if (url.protocol === "https:") return trimmed;
  if (url.protocol === "http:" && isLoopbackHost(url.hostname)) return trimmed;
  throw new Error(
    `aias-memory: serviceUrl must use HTTPS for non-loopback destinations; plain HTTP is only allowed for loopback addresses (localhost, 127.0.0.0/8, ::1). Refusing: ${raw}`
  );
};
var ALLOWED_ROLES = /* @__PURE__ */ new Set(["system", "user", "assistant"]);
var sanitizeMessages = (input) => {
  if (!Array.isArray(input)) return [];
  const out = [];
  for (const m of input) {
    if (m && typeof m === "object" && ALLOWED_ROLES.has(m.role) && typeof m.content === "string") {
      const msg = m;
      out.push({ role: msg.role, content: msg.content });
    }
  }
  return out;
};
var index_default = {
  id: "aias-memory",
  name: "AiAS Memory for OpenClaw \u2014 powered by NEDB",
  description: "AiAS fact memory + discussion window + broadcast directives/KB as an OpenClaw context engine, stored in NEDB.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    if (cfg.enabled === false) return;
    let serviceUrl;
    try {
      serviceUrl = resolveServiceUrl(cfg.serviceUrl ?? DEFAULT_SERVICE_URL);
    } catch (err) {
      api.logger?.error?.(String(err));
      return;
    }
    const workspaceId = cfg.workspaceId ?? "openclaw";
    const agentId = cfg.agentId;
    const post = async (path, body) => {
      const res = await fetch(`${serviceUrl}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        redirect: "error"
      });
      if (!res.ok) throw new Error(`aias-memory ${path}: HTTP ${res.status}`);
      return res.json();
    };
    const lastOf = (messages, role) => [...messages ?? []].reverse().find((m) => m.role === role)?.content ?? "";
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
            `aias-memory: service unreachable at ${serviceUrl} (${String(err)}); start it with run.sh from the aias-memory repo.`
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
            user_message: lastOf(params.messages, "user")
          });
          const clean = sanitizeMessages(data.messages);
          if (clean.length > 0) {
            return { messages: clean };
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
            provider: "openai"
          });
        } catch (err) {
          api.logger?.warn?.(`aias-memory: extract failed (${String(err)})`);
        }
      }
    }));
  }
};
export {
  index_default as default
};
