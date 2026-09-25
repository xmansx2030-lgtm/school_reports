const encoder = new TextEncoder();
const API_BASE = "https://api.hetzner.cloud/v1";

function json(data, status = 200) {
  return Response.json(data, {
    status,
    headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

async function authorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  const candidate = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!candidate || !env.EMERGENCY_ACCESS_TOKEN) return false;
  const a = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(candidate)));
  const b = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(env.EMERGENCY_ACCESS_TOKEN)));
  return crypto.subtle.timingSafeEqual(a, b);
}

async function hetzner(path, env, method = "GET", payload) {
  const providerToken = method === "GET" ? env.HETZNER_READ_TOKEN : env.HETZNER_WRITE_TOKEN;
  const response = await fetch(API_BASE + path, {
    method,
    headers: {
      Authorization: `Bearer ${providerToken}`,
      Accept: "application/json",
      ...(payload ? { "Content-Type": "application/json" } : {}),
    },
    body: payload ? JSON.stringify(payload) : undefined,
    redirect: "manual",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    if (response.status === 429) return { error: "rate_limited", status: 429 };
    return { error: "hetzner_unavailable", status: 502 };
  }
  return { value: await response.json(), status: response.status };
}

async function smallJson(request) {
  if (!request.body) throw new Error("empty");
  const reader = request.body.getReader();
  const parts = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 500) {
      await reader.cancel();
      throw new Error("too_large");
    }
    parts.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    bytes.set(part, offset);
    offset += part.byteLength;
  }
  return JSON.parse(new TextDecoder().decode(bytes));
}

export default {
  async fetch(request, env) {
    if (env.ENABLED !== "true" || !env.HETZNER_READ_TOKEN || !env.HETZNER_WRITE_TOKEN || !env.EMERGENCY_ACCESS_TOKEN || !env.OPS_RATE_LIMITER) {
      return json({ detail: "خدمة الطوارئ غير مفعلة." }, 503);
    }
    if (!(await authorized(request, env))) return json({ detail: "غير مصرح." }, 401);
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && pathname === "/v1/server") {
      try {
        const result = await hetzner(`/servers/${env.SERVER_ID}`, env);
        if (result.error) return json({ detail: result.error }, result.status);
        const server = result.value.server;
        if (String(server.id) !== String(env.SERVER_ID) || server.name !== env.SERVER_SLUG) {
          return json({ detail: "هوية الخادم لا تطابق إعداد الطوارئ." }, 409);
        }
        return json({
          source: "hetzner",
          id: server.id,
          name: server.name,
          status: server.status,
          server_type: server.server_type?.name || "",
          location: server.location?.name || "",
          backup_window: server.backup_window,
          protection: server.protection,
          fetched_at: new Date().toISOString(),
        });
      } catch (error) {
        console.error(JSON.stringify({
          event: "provider_read_failure",
          name: String(error?.name || "unknown").slice(0, 60),
          message: String(error?.message || "unknown").slice(0, 160),
        }));
        return json({ detail: "تعذر الاتصال بـ Hetzner." }, 502);
      }
    }
    if (request.method === "GET" && pathname === "/v1/overview") {
      try {
        const result = await hetzner(`/servers/${env.SERVER_ID}`, env);
        if (result.error) return json({ detail: result.error }, result.status);
        const server = result.value.server;
        if (String(server?.id) !== String(env.SERVER_ID) || server.name !== env.SERVER_SLUG) {
          return json({ detail: "هوية الخادم لا تطابق إعداد الطوارئ." }, 409);
        }
        const now = new Date();
        const params = new URLSearchParams({
          type: "cpu,disk,network",
          start: new Date(now.getTime() - 3 * 3600000).toISOString(),
          end: now.toISOString(),
          step: "300",
        });
        const safe = async (path) => {
          try { return await hetzner(path, env); }
          catch { return { error: "unavailable" }; }
        };
        const resources = await Promise.all([
          safe(`/servers/${env.SERVER_ID}/metrics?${params}`),
          safe(`/images?bound_to=${env.SERVER_ID}&type=backup&per_page=10`),
          safe(`/servers/${env.SERVER_ID}/actions?per_page=10&sort=id%3Adesc`),
        ]);
        const partialErrors = [];
        for (const [index, name] of ["metrics", "backups", "actions"].entries()) {
          if (resources[index].error) partialErrors.push(name);
        }
        return json({
          source: "hetzner",
          id: server.id,
          name: server.name,
          status: server.status,
          server_type: server.server_type?.name || "",
          location: server.location?.name || "",
          backup_window: server.backup_window,
          protection: server.protection || {},
          metrics: resources[0].value?.metrics || {},
          backups: (resources[1].value?.images || [])
            .filter((row) => row.type === "backup")
            .map((row) => ({
              id: row.id, description: row.description, created: row.created,
              status: row.status, image_size: row.image_size,
            })),
          recent_actions: (resources[2].value?.actions || []).map((row) => ({
            id: row.id, command: row.command, status: row.status,
            started: row.started, finished: row.finished,
          })),
          partial_errors: partialErrors,
          fetched_at: now.toISOString(),
        });
      } catch (error) {
        console.error(JSON.stringify({
          event: "provider_overview_failure",
          name: String(error?.name || "unknown").slice(0, 60),
        }));
        return json({ detail: "تعذر قراءة تفاصيل Hetzner." }, 502);
      }
    }
    const statusMatch = pathname.match(/^\/v1\/actions\/([1-9][0-9]{0,15})$/);
    if (request.method === "GET" && statusMatch) {
      try {
        const result = await hetzner(`/actions/${statusMatch[1]}`, env);
        if (result.error) return json({ detail: result.error }, result.status);
        const action = result.value.action;
        if (!Array.isArray(action?.resources) ||
            !action.resources.some((row) => row.type === "server" && String(row.id) === String(env.SERVER_ID))) {
          return json({ detail: "الإجراء لا يخص هذا الخادم." }, 404);
        }
        return json({
          id: action.id,
          command: action.command,
          status: action.status,
          error_code: action.error?.code || "",
          started: action.started,
          finished: action.finished,
        });
      } catch {
        return json({ detail: "تعذر التحقق من حالة الإجراء." }, 502);
      }
    }
    if (request.method === "POST" && /^\/v1\/actions\/(poweron|reboot|shutdown|snapshot)$/.test(pathname)) {
      const action = pathname.split("/").at(-1);
      const length = Number(request.headers.get("Content-Length") || 0);
      if (length > 500) return json({ detail: "طلب كبير جدًا." }, 413);
      let body;
      try {
        body = await smallJson(request);
      } catch {
        return json({ detail: "طلب غير صالح." }, 400);
      }
      if (body?.confirmation !== `${env.SERVER_SLUG}:${action}`) {
        return json({ detail: "تأكيد الإجراء غير مطابق." }, 409);
      }
      const limit = await env.OPS_RATE_LIMITER.limit({ key: `emergency:${env.SERVER_ID}` });
      if (!limit.success) return json({ detail: "انتظر قبل طلب إجراء جديد." }, 429);
      try {
        const current = await hetzner(`/servers/${env.SERVER_ID}`, env);
        if (current.error) return json({ detail: current.error }, current.status);
        if (String(current.value.server.id) !== String(env.SERVER_ID) ||
            current.value.server.name !== env.SERVER_SLUG) {
          return json({ detail: "هوية الخادم لا تطابق إعداد الطوارئ." }, 409);
        }
        const state = current.value.server.status;
        if ((action === "poweron" && state !== "off") ||
            (["reboot", "shutdown", "snapshot"].includes(action) && state !== "running")) {
          return json({ detail: "حالة الخادم لا تسمح بهذا الإجراء.", status: state }, 409);
        }
        const route = action === "snapshot"
          ? `/servers/${env.SERVER_ID}/actions/create_image`
          : `/servers/${env.SERVER_ID}/actions/${action}`;
        const result = await hetzner(route, env, "POST",
          action === "snapshot" ? {
            type: "snapshot",
            description: `operations-${new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14)}`,
          } : undefined);
        if (result.error) return json({ detail: result.error }, result.status);
        const providerAction = result.value.action || {};
        if (!providerAction.id) {
          return json({ detail: "لم تؤكد Hetzner رقم الإجراء؛ راجع حالته قبل إعادة الطلب." }, 502);
        }
        console.log(JSON.stringify({
          event: "emergency_action",
          action,
          server_id: env.SERVER_ID,
          provider_action_id: providerAction.id,
          status: providerAction.status,
        }));
        return json({
          action,
          status: providerAction.status || "running",
          provider_action_id: providerAction.id,
        }, 202);
      } catch {
        return json({ detail: "تعذر الاتصال بـ Hetzner." }, 502);
      }
    }
    return json({ detail: "المسار غير متاح." }, 404);
  },
};
