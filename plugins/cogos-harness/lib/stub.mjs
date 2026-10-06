// lib/stub.mjs — the skeleton's no-op body, shared by the five stub mods.
// A mod replaces its stubRun() calls with real logic in its own PR.
//
// off    → returns "off" at once: no ledger row, no write of any kind.
// shadow → one ledger row {hook:"mod/<name>/<event>", shadow:true, would:"none"}
//          written BEFORE the caller's next(e), so ledger order == chain order
//          (tests/order.test.ts relies on this).
// on     → the same row without the shadow flag.
// The caller always returns next(e) unchanged: a stub never alters an event.

import { modeOf } from "./cfg.mjs";
import { logRun } from "./ledger.mjs";

/**
 * @param {any} io
 * @param {import("./cfg.mjs").ModName} name
 * @param {string} event
 * @returns {Promise<import("./cfg.mjs").Mode>}
 */
export async function stubRun(io, name, event) {
  const mode = await modeOf(io, name);
  if (mode === "off") return mode;
  await logRun(io, { mod: name, event, status: "ok", ms: 0, shadow: mode === "shadow", would: "none" });
  return mode;
}
