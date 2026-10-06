// mods/cog-context.mjs — One in-process context injector (plan §5.3). Clock-refreshed
// readers write $.state context.*; the prompt path only reads. Reads
// status.vitals via lib/cache.mjs readVitals(io); never writes it. Owns $.state context.*.
//
// SKELETON STUB (cogos-harness 0.4.0): loads, registers, does nothing.
// Mode "off" (the default: no ~/.cog/conf/harness-mods.json) → every hook
// here is next(e) after one cached config read. "shadow"/"on" → one
// ledger row per run, event still unchanged.
//
// Registration rules (measured, `claude plugin validate`, CC 2.1.292):
// every on() in this plugin carries a matcher (`{}` = all), because one
// plugin may not register an event twice without one; hooks nest in
// registration order, first outermost (hooks/index.mjs MOD_ORDER).

import { stubRun } from "../lib/stub.mjs";

export const NAME = "context";

// ── io adapter: IDENTICAL in all five mods (skeleton-owned; plan §4) ──────
// `claude plugin validate` follows `$` only into functions declared in the
// same file, never across an import, and wants $.state refs and $.env names
// as literals. So each mod builds this adapter from `$` and hands it to lib/.
// A mod adds its OWN keys (e.g. cog-status's setVitals) below the marker.
/** @param {import('claude-code').EngineInterface} $ */
function ioOf($) {
  return {
    now: () => $.clock.now(),
    home: () => $.env.get("HOME"),
    confPath: () => $.env.get("COGOS_HARNESS_MODS_CONF"),
    kernelUrlEnv: () => $.env.get("COGOS_KERNEL_URL"),
    kernelPortEnv: () => $.env.get("COGOS_KERNEL_PORT"),
    read: (/** @type {string} */ path) => $.fs.read(path),
    write: (/** @type {string} */ path, /** @type {string} */ text) => $.fs.write(path, text),
    run: (/** @type {string[]} */ argv, /** @type {any} */ init) => $.process.run(argv, init),
    fetch: (/** @type {string} */ url, /** @type {any} */ init) => $.http.fetch(url, init),
    getCfg: () => $.state.get({ plugin: "cogos-harness", key: "cfg" }),
    setCfg: (/** @type {any} */ v) => $.state.set({ plugin: "cogos-harness", key: "cfg" }, v),
    getVitals: () => $.state.get({ plugin: "cogos-harness", key: "status.vitals" }),
    // ── mod-specific io below this line ──
  };
}
// ─────────────────────────────────────────────────────────────────────────

/**
 * @param {import('claude-code').On} on
 * @param {import('claude-code').PluginOptions} [options]
 */
export function register(on, options) {
  on("prompt.context", {}, async ($, e, next) => {
    await stubRun(ioOf($), NAME, "prompt.context");
    return next(e);
  });
}
