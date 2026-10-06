// lib/cfg.mjs — the shared mods config (plan §3.1). Owned by the skeleton PR.
//
// File: ~/.cog/conf/harness-mods.json (override: $COGOS_HARNESS_MODS_CONF).
//   { "version": 1, "mods": { "redact": "off|shadow|on", "guard": ..., ... }, ... }
// A missing, unreadable or malformed file means EVERY mod is off: the plugin
// is inert by default. An unknown mode string is treated as off.
//
// register(on, options) has no `$`, so config cannot decide registration:
// every mod hook asks modeOf() at dispatch and returns next(e) when "off".
// The parsed config is cached in $.state (cogos-harness / cfg) and re-read at
// most once per CFG_TTL_MS, so the off path is one state read per hook.
//
// `claude plugin validate` only lets `$` flow into functions declared in the
// same file, so lib functions take an `io` adapter (see lib/io.md) that each
// mod builds from `$` with its own same-file ioOf($).

/** @typedef {"off" | "shadow" | "on"} Mode */
/** @typedef {"redact" | "guard" | "context" | "status" | "receipt"} ModName */

export const MODES = /** @type {const} */ (["off", "shadow", "on"]);
export const MOD_NAMES = /** @type {const} */ (["redact", "guard", "context", "status", "receipt"]);
export const CFG_TTL_MS = 60_000;

/** @returns {Record<ModName, Mode>} */
export function allOff() {
  return { redact: "off", guard: "off", context: "off", status: "off", receipt: "off" };
}

/**
 * Parse the config text. Never throws: bad input → everything off.
 * @param {string | undefined | null} text
 */
export function parseCfg(text) {
  const mods = allOff();
  /** @type {any} */
  let raw = null;
  try {
    raw = text ? JSON.parse(text) : null;
  } catch {
    raw = null;
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { version: 1, source: /** @type {const} */ ("default-off"), mods, raw: {} };
  }
  const m = raw.mods && typeof raw.mods === "object" ? raw.mods : {};
  for (const name of MOD_NAMES) {
    const v = m[name];
    if (typeof v === "string" && /** @type {readonly string[]} */ (MODES).includes(v)) {
      mods[name] = /** @type {Mode} */ (v);
    }
  }
  return { version: Number(raw.version) || 1, source: /** @type {const} */ ("file"), mods, raw };
}

/**
 * Load (or reuse) the session's config. Never throws.
 * @param {any} io  needs now, home, confPath, read, getCfg, setCfg
 */
export async function loadCfg(io) {
  let now = 0;
  try {
    now = await io.now();
  } catch {
    now = 0;
  }
  try {
    const held = await io.getCfg();
    const v = held && held.value;
    if (v && typeof v.loaded_at === "number" && now - v.loaded_at < CFG_TTL_MS) return v;
  } catch {
    /* state unavailable: fall through to a fresh read */
  }
  /** @type {string | null} */
  let text = null;
  try {
    const override = await io.confPath();
    const home = await io.home();
    const path = override || (home ? `${home}/.cog/conf/harness-mods.json` : null);
    if (path) {
      const t = await io.read(path);
      text = typeof t === "string" ? t : null;
    }
  } catch {
    text = null; // missing file → all off
  }
  const cfg = { ...parseCfg(text), loaded_at: now };
  try {
    await io.setCfg(cfg);
  } catch {
    /* not fatal: next call re-reads */
  }
  return cfg;
}

/**
 * The mode of one mod for this dispatch. Never throws; any failure → "off".
 * @param {any} io
 * @param {ModName} name
 * @returns {Promise<Mode>}
 */
export async function modeOf(io, name) {
  try {
    const cfg = await loadCfg(io);
    return cfg.mods[name] || "off";
  } catch {
    return "off";
  }
}
