// lib/order.mjs — the registration order of the five mods (plan §3.2).
// Owner after the skeleton lands: cog-redact.
//
// Chain semantics: the mod registered first sees an event first and its
// result last. So on tool.call (PreToolUse):
//   redact  rewrites secret-bearing input to placeholders before anyone reads it,
//   guard   decides on the REDACTED input (its deny text can never leak a secret),
//   receipt is last, so it records what actually ran and the scrubbed result.
// hooks/index.mjs registers mods in exactly this order; tests/order.test.ts
// fails if it doesn't.

/** @type {readonly ["redact", "guard", "context", "status", "receipt"]} */
export const ORDER = Object.freeze(["redact", "guard", "context", "status", "receipt"]);

/**
 * Typed precedence check: true when `a` sees a tool.call before `b`.
 * @param {typeof ORDER[number]} a
 * @param {typeof ORDER[number]} b
 */
export function before(a, b) {
  return ORDER.indexOf(a) < ORDER.indexOf(b);
}
