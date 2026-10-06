// lib/kernel.mjs — $.http.fetch to the CogOS kernel, with the X-Cogos-Grant
// header the 0.3.0 shell hooks already send (v0.16.29 enforcement).
//
// URL: COGOS_KERNEL_URL, else http://127.0.0.1:${COGOS_KERNEL_PORT:-6931}
// (same precedence as the Python hooks). Grant: ~/.cog/vault/node-root-grant,
// read once per session and held only in module memory (never logged, never
// written to $.state). Fails open: an unreachable kernel resolves
// { ok: false, status: 0 } and never throws.

/** @type {{ tried: boolean, token: string | null }} */
const grantCache = { tried: false, token: null };

/** @param {any} io  needs kernelUrlEnv, kernelPortEnv */
export async function kernelUrl(io) {
  try {
    const url = await io.kernelUrlEnv();
    if (url) return url.replace(/\/+$/, "");
    const port = await io.kernelPortEnv();
    return `http://127.0.0.1:${port || "6931"}`;
  } catch {
    return "http://127.0.0.1:6931";
  }
}

/** @param {any} io  needs home, read */
export async function grant(io) {
  if (grantCache.tried) return grantCache.token;
  grantCache.tried = true;
  try {
    const home = await io.home();
    if (home) {
      const raw = await io.read(`${home}/.cog/vault/node-root-grant`);
      const t = typeof raw === "string" ? raw.trim() : "";
      grantCache.token = t || null;
    }
  } catch {
    grantCache.token = null;
  }
  return grantCache.token;
}

/**
 * @param {any} io  needs kernelUrlEnv, kernelPortEnv, home, read, fetch
 * @param {string} path  e.g. "/health"
 * @param {{ method?: string, body?: unknown, headers?: Record<string, string> }} [init]
 * @returns {Promise<{ ok: boolean, status: number, text: string, json?: any }>}
 */
export async function kernelFetch(io, path, init = {}) {
  try {
    const base = await kernelUrl(io);
    /** @type {Record<string, string>} */
    const headers = { ...(init.headers || {}) };
    const method = init.method || "GET";
    let body;
    if (init.body !== undefined) {
      body = typeof init.body === "string" ? init.body : JSON.stringify(init.body);
      headers["Content-Type"] = headers["Content-Type"] || "application/json";
    }
    if (method !== "GET") {
      const g = await grant(io);
      if (g) headers["X-Cogos-Grant"] = g;
    }
    const res = await io.fetch(`${base}${path}`, { method, headers, body });
    /** @type {any} */
    let json;
    try {
      json = res.text ? JSON.parse(res.text) : undefined;
    } catch {
      json = undefined;
    }
    return { ok: !!res.ok, status: res.status, text: res.text || "", json };
  } catch {
    return { ok: false, status: 0, text: "" };
  }
}

/** Test seam: forget the cached grant. */
export function _resetGrant() {
  grantCache.tried = false;
  grantCache.token = null;
}
