// Security and behavior regression tests for the AiAS Memory
// OpenClaw plugin. Covers the transport policy for the configured
// serviceUrl (the part of ClawScan MCP06 we adopted) and the
// intended assembly behavior: the service's assembled context is
// the prompt, malformed entries are dropped, and any failure
// passes the gateway's own messages through untouched.
// Run: node tests/plugin-security.mjs
import assert from "node:assert/strict";
import plugin from "../openclaw/dist/index.js";

function attempt(serviceUrl) {
  const state = { registered: false, errors: [] };
  const api = {
    pluginConfig: serviceUrl === undefined ? {} : { serviceUrl },
    logger: {
      info() {},
      warn() {},
      error(msg) {
        state.errors.push(String(msg));
      },
    },
    registerContextEngine() {
      state.registered = true;
    },
  };
  plugin.register(api);
  return state;
}

// Default config (loopback) registers.
assert.equal(attempt(undefined).registered, true, "default loopback registers");

// Loopback variants register.
for (const u of [
  "http://127.0.0.1:8000",
  "http://localhost:9000",
  "http://[::1]:8000",
  "https://memory.example.com",
]) {
  assert.equal(attempt(u).registered, true, `${u} registers`);
}

// Remote plaintext and malformed URLs are rejected, loudly.
for (const u of [
  "http://memory.example.com",
  "http://192.168.1.10:8000",
  "http://10.0.0.5:8000",
  "ftp://127.0.0.1:8000",
  "not a url",
]) {
  const s = attempt(u);
  assert.equal(s.registered, false, `${u} must NOT register`);
  assert.ok(s.errors.length > 0, `${u} logs an error`);
}

console.log("plugin-security: PASS (transport policy enforced)");

// --- Intended assembly: the service's context is the prompt ---
function makeEngine(pluginConfig, serviceResponse, fetchImpl) {
  let factory = null;
  const api = {
    pluginConfig,
    logger: { info() {}, warn() {}, error() {} },
    registerContextEngine(id, f) {
      factory = f;
    },
  };
  plugin.register(api);
  assert.ok(factory, "engine factory registered");
  globalThis.fetch =
    fetchImpl ??
    (async () => ({
      ok: true,
      status: 200,
      json: async () => serviceResponse,
    }));
  return factory();
}

const PARAMS = {
  sessionKey: "s1",
  messages: [
    { role: "system", content: "GATEWAY SYSTEM" },
    { role: "user", content: "hello" },
  ],
};

// 1. The assembled context comes back whole: directives and facts in
// the system seat, the discussion window, the current turn.
{
  const assembled = [
    { role: "system", content: "DIRECTIVES + FACTS" },
    { role: "user", content: "earlier question" },
    { role: "assistant", content: "earlier answer" },
    { role: "user", content: "hello" },
  ];
  const engine = makeEngine({}, { messages: assembled });
  const out = await engine.assemble(PARAMS);
  assert.deepEqual(out.messages, assembled);
}

// 2. Malformed entries are dropped; well-formed ones survive.
{
  const engine = makeEngine({}, {
    messages: [
      { role: "system", content: "DIRECTIVES + FACTS" },
      { role: "tool", content: "unknown role" },
      { role: "user", content: 42 },
      { role: "user", content: "hello" },
    ],
  });
  const out = await engine.assemble(PARAMS);
  assert.deepEqual(out.messages, [
    { role: "system", content: "DIRECTIVES + FACTS" },
    { role: "user", content: "hello" },
  ]);
}

// 3. Empty service response passes the gateway's messages through.
{
  const engine = makeEngine({}, { messages: [] });
  const out = await engine.assemble(PARAMS);
  assert.deepEqual(out.messages, PARAMS.messages);
}

// 4. A failing service passes the gateway's messages through.
{
  const engine = makeEngine({}, null, async () => {
    throw new Error("connection refused");
  });
  const out = await engine.assemble(PARAMS);
  assert.deepEqual(out.messages, PARAMS.messages);
}

console.log("plugin-security: PASS (intended assembly preserved)");
