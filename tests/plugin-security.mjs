// Security regression tests for the AiAS Memory OpenClaw plugin.
// Covers the ClawScan MCP06 remediation: transport policy for the
// configured serviceUrl. Run: node tests/plugin-security.mjs
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
