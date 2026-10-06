// lib/cache.mjs — the shared vitals cache (plan §3.3). Owner after the
// skeleton lands: cog-status (the single writer). cog-context only reads.
//
// In-process: $.state  cogos-harness / "status.vitals"  (survives hot reload)
// Mirror:     ~/.cog/run/vitals.json  for out-of-process readers (stage,
//             Hermes darkstar-vitals, CogBar).
//
// Shape (Vitals): { written_at, kernel, usage, disk, git, prs, hook_health }
// Every reader must show the age once it passes STALE_MS (2 minutes): the
// fix for the 54 h-stale vitals finding. Never hide a stale value.

export const STALE_MS = 2 * 60_000;

/**
 * @typedef {{
 *   written_at: number,
 *   kernel?: { reachable: boolean, version?: string, state?: string },
 *   usage?: { context_pct?: number, five_h_pct?: number, week_pct?: number },
 *   disk?: { free_gb?: number, low?: boolean },
 *   git?: { branch?: string, dirty?: number, ahead?: number },
 *   prs?: { open?: number, stale?: number },
 *   hook_health?: Record<string, unknown>,
 * }} Vitals
 */

/**
 * Age of a vitals record in ms, or Infinity when absent/unstamped.
 * @param {Vitals | undefined | null} v
 * @param {number} now
 */
export function ageMs(v, now) {
  if (!v || typeof v.written_at !== "number") return Infinity;
  return Math.max(0, now - v.written_at);
}

/** @param {number} ms */
export function fmtAge(ms) {
  if (!Number.isFinite(ms)) return "never";
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
}

/**
 * The suffix a reader appends: "" when fresh, " (stale 3m)" when older than
 * STALE_MS, " (no vitals)" when absent.
 * @param {Vitals | undefined | null} v
 * @param {number} now
 */
export function staleSuffix(v, now) {
  const a = ageMs(v, now);
  if (!Number.isFinite(a)) return " (no vitals)";
  return a > STALE_MS ? ` (stale ${fmtAge(a)})` : "";
}

/**
 * Read the vitals and their age. Never throws.
 * @param {any} io  needs now, getVitals
 * @returns {Promise<{ vitals: Vitals | undefined, age_ms: number, stale: boolean }>}
 */
export async function readVitals(io) {
  let now = 0;
  try {
    now = await io.now();
  } catch {
    now = 0;
  }
  /** @type {Vitals | undefined} */
  let vitals;
  try {
    const held = await io.getVitals();
    vitals = held && held.value ? held.value : undefined;
  } catch {
    vitals = undefined;
  }
  const age_ms = ageMs(vitals, now);
  return { vitals, age_ms, stale: age_ms > STALE_MS };
}

/**
 * Write the vitals (stamps written_at) to $.state and mirror the JSON file.
 * Only cog-status may call this (only its ioOf has setVitals). Never throws.
 * @param {any} io  needs now, setVitals, home, write
 * @param {Omit<Vitals, "written_at">} partial
 * @param {{ mirror?: boolean }} [opts]
 */
export async function writeVitals(io, partial, opts = {}) {
  let now = 0;
  try {
    now = await io.now();
  } catch {
    now = Date.now();
  }
  /** @type {Vitals} */
  const vitals = { ...partial, written_at: now };
  try {
    await io.setVitals(vitals);
  } catch {
    /* fail open */
  }
  if (opts.mirror !== false) {
    try {
      const home = await io.home();
      if (home) await io.write(`${home}/.cog/run/vitals.json`, JSON.stringify(vitals, null, 2) + "\n");
    } catch {
      /* fail open */
    }
  }
  return vitals;
}
