// lib/ledger.mjs — one line per mod hook run into the existing hook-health
// ledger (plan §3.5), so hook_health.py and the vitals line keep working.
//
// Row: { ts, hook: "mod/<name>/<event>", status, ms, shadow?, would?, ... }
//
// $.fs.write replaces a whole file and the ledger is shared with the shell
// hooks (hookrun.py appends concurrently), so a read-modify-write would race
// and drop rows. Append goes through io.run (`$.process.run`) with `cat >>`:
// one O_APPEND write per row, the same guarantee hookrun.py relies on.
// Fails open: a ledger failure never fails a hook.

/**
 * Build one ledger row (pure; tests use it directly).
 * @param {{ mod: string, event: string, status?: string, ms?: number, shadow?: boolean, would?: string, extra?: Record<string, unknown>, ts?: number }} r
 */
export function ledgerRow(r) {
  /** @type {Record<string, unknown>} */
  const row = {
    ts: (r.ts ?? Date.now()) / 1000,
    hook: `mod/${r.mod}/${r.event}`,
    status: r.status ?? "ok",
    ms: Math.round(r.ms ?? 0),
  };
  if (r.shadow) {
    row.shadow = true;
    if (r.would) row.would = r.would;
  }
  if (r.extra) Object.assign(row, r.extra);
  return row;
}

/**
 * Append one row. Never throws.
 * @param {any} io  needs now, home, run
 * @param {Parameters<typeof ledgerRow>[0]} r
 */
export async function logRun(io, r) {
  try {
    let ts = r.ts;
    if (ts === undefined) {
      try {
        ts = await io.now();
      } catch {
        ts = Date.now();
      }
    }
    const line = JSON.stringify(ledgerRow({ ...r, ts })) + "\n";
    const home = await io.home();
    if (!home) return false;
    const path = `${home}/.cog/run/hook-health.jsonl`;
    await io.run(["/bin/sh", "-c", 'cat >> "$1"', "sh", path], { stdin: line, timeoutMs: 2000 });
    return true;
  } catch {
    return false;
  }
}
