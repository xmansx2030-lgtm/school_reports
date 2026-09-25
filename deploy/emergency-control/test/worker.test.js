import assert from "node:assert/strict";
import { timingSafeEqual, webcrypto } from "node:crypto";
import { afterEach, beforeEach, test } from "node:test";
import worker from "../src/index.js";

const originalFetch = globalThis.fetch;
const originalCrypto = globalThis.crypto;
const originalLog = console.log;
const validToken = "a".repeat(48);
const env = () => ({
  ENABLED: "true",
  SERVER_ID: "155662703",
  SERVER_SLUG: "school-reports-prod",
  HETZNER_READ_TOKEN: "provider-read-test-token",
  HETZNER_WRITE_TOKEN: "provider-write-test-token",
  EMERGENCY_ACCESS_TOKEN: validToken,
  OPS_RATE_LIMITER: { limit: async () => ({ success: true }) },
});
const request = (path, method = "GET", token = validToken, body) =>
  new Request(`https://emergency.example${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    body: body && JSON.stringify(body),
  });
const server = (status = "off", name = "school-reports-prod") => ({
  server: { id: 155662703, name, status, protection: { delete: true } },
});

beforeEach(() => {
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      subtle: {
        digest: (algorithm, value) => webcrypto.subtle.digest(algorithm, value),
        timingSafeEqual: (a, b) => timingSafeEqual(Buffer.from(a), Buffer.from(b)),
      },
    },
  });
  console.log = () => {};
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: originalCrypto });
  console.log = originalLog;
});

test("fails closed before provider access", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("unexpected"); };
  const disabled = await worker.fetch(request("/v1/server"), { ...env(), ENABLED: "false" });
  const unauthorized = await worker.fetch(request("/v1/server", "GET", "wrong"), env());
  assert.equal(disabled.status, 503);
  assert.equal(unauthorized.status, 401);
  assert.equal(calls, 0);
});

test("rejects mismatched server identity and never sends a power command", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json(server("off", "wrong-server")); };
  const response = await worker.fetch(request(
    "/v1/actions/poweron", "POST", validToken,
    { confirmation: "school-reports-prod:poweron" },
  ), env());
  assert.equal(response.status, 409);
  assert.equal(calls, 1);
});

test("accepts only the fixed power command after explicit confirmation", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method, redirect: options.redirect });
    return Response.json(calls.length === 1 ? server() : { action: { id: 77, status: "running" } });
  };
  const denied = await worker.fetch(request(
    "/v1/actions/poweron", "POST", validToken, { confirmation: "wrong" },
  ), env());
  assert.equal(denied.status, 409);
  assert.equal(calls.length, 0);
  const accepted = await worker.fetch(request(
    "/v1/actions/poweron", "POST", validToken,
    { confirmation: "school-reports-prod:poweron" },
  ), env());
  assert.equal(accepted.status, 202);
  assert.deepEqual(calls.map((row) => row.method), ["GET", "POST"]);
  assert.deepEqual(calls.map((row) => row.redirect), ["manual", "manual"]);
  assert.equal(calls[1].url, "https://api.hetzner.cloud/v1/servers/155662703/actions/poweron");
});

test("rejects rate limited actions before provider access", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json(server()); };
  const limited = env();
  limited.OPS_RATE_LIMITER.limit = async () => ({ success: false });
  const response = await worker.fetch(request(
    "/v1/actions/poweron", "POST", validToken,
    { confirmation: "school-reports-prod:poweron" },
  ), limited);
  assert.equal(response.status, 429);
  assert.equal(calls, 0);
});

test("overview reads only the fixed server and tolerates a partial provider failure", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method, authorization: options.headers.Authorization });
    if (String(url).endsWith("/servers/155662703")) return Response.json(server("running"));
    if (String(url).includes("/metrics?")) {
      return Response.json({ metrics: { time_series: { cpu: { values: [[1, "7.0"]] } } } });
    }
    if (String(url).includes("/images?")) return new Response("unavailable", { status: 503 });
    return Response.json({ actions: [{ id: 42, command: "reboot", status: "success" }] });
  };
  const response = await worker.fetch(request("/v1/overview"), env());
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(payload.partial_errors, ["backups"]);
  assert.equal(payload.recent_actions[0].id, 42);
  assert.equal(calls.length, 4);
  assert.ok(calls.every((row) => row.method === "GET" && row.authorization === "Bearer provider-read-test-token"));
});

test("action status is visible only when its resources contain the fixed server", async () => {
  globalThis.fetch = async () => Response.json({ action: {
    id: 88, status: "success", resources: [{ type: "server", id: 999 }],
  } });
  const denied = await worker.fetch(request("/v1/actions/88"), env());
  assert.equal(denied.status, 404);
  globalThis.fetch = async () => Response.json({ action: {
    id: 88, status: "success", resources: [{ type: "server", id: 155662703 }],
  } });
  const allowed = await worker.fetch(request("/v1/actions/88"), env());
  assert.equal(allowed.status, 200);
  assert.equal((await allowed.json()).status, "success");
});
