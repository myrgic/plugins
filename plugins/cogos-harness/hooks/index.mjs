// hooks/index.mjs — THE one hooks module of cogos-harness (plan §2).
//
// hooks.json "modules" takes exactly one entry per plugin, so this file
// composes the five mods by calling each one's register(on, options).
// Hooks nest in registration order, first outermost: the first mod sees an
// event first and its result last. Each mod gates itself at dispatch
// (lib/cfg.mjs); with ~/.cog/conf/harness-mods.json missing every mod is off
// and every hook is next(e): the plugin is inert by default.
//
// Shared file: changes go through a one-line PR the mod agents can see
// (plan §4). cog-redact owns the order block.

import { register as redact } from "../mods/cog-redact.mjs";
import { register as guard } from "../mods/cog-guard.mjs";
import { register as context } from "../mods/cog-context.mjs";
import { register as status } from "../mods/cog-status.mjs";
import { register as receipt } from "../mods/cog-receipt.mjs";
import { ORDER } from "../lib/order.mjs";

// ── order block (owner: cog-redact; contract: plan §3.2) ─────────────────
// redact rewrites secrets out of tool input BEFORE guard decides on it;
// guard decides on redacted input; receipt (last) records what ran.
// tests/order.test.ts fails if this order changes. The validator wants `on`
// handed to functions named at the top of the file, so the order is spelled
// as literal calls below, which MUST match lib/order.mjs ORDER (the typed
// contract; tests/order.test.ts checks the real chain against it).
export const MOD_ORDER = ORDER;

/**
 * @param {import('claude-code').On} on
 * @param {import('claude-code').PluginOptions} options
 */
export function register(on, options) {
  redact(on, options);
  guard(on, options);
  context(on, options);
  status(on, options);
  receipt(on, options);
}
// ─────────────────────────────────────────────────────────────────────────
