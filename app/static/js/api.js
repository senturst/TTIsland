/** 网络层：统一携带 client_id，统一错误处理。 */

const CLIENT_KEY = "ttisland.client_id";

export function getClientId() {
  let id = localStorage.getItem(CLIENT_KEY);
  if (!id) {
    id = (crypto.randomUUID && crypto.randomUUID()) ||
      "c" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem(CLIENT_KEY, id);
  }
  return id;
}

async function request(path, { method = "GET", body = null } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Client-Id": getClientId(),
    },
    body: body ? JSON.stringify(body) : null,
  });

  if (res.status === 429) {
    throw new Error("操作太频繁了，慢一点。");
  }
  if (!res.ok) {
    let detail = `请求失败 (${res.status})`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch { /* 非 JSON 响应，用默认文案 */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export const api = {
  hello: (name) => request("/api/player/hello", { method: "POST", body: { name } }),
  setName: (name) => request("/api/player/set_name", { method: "POST", body: { name } }),
  meta: () => request("/api/meta/config"),
  start: () => request("/api/run/start", { method: "POST" }),
  active: () => request("/api/run/active"),
  action: (action, payload = {}) =>
    request("/api/run/action", { method: "POST", body: { action, payload } }),
  leaderboard: (by = "score") =>
    request(`/api/run/leaderboard?by=${encodeURIComponent(by)}&limit=15`),
  notifications: () => request("/api/notifications"),
  markNotifications: (ids) =>
    request("/api/notifications/read", { method: "POST", body: ids }),
};
