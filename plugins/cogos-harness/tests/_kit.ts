// tests/_kit.ts — shared stubs for the mod tests (skeleton-owned).
// Not a test file itself (no .test.ts suffix).
import { mock } from 'claude-code/testing'

export const RUN_OK = { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
export const CONF = '/home/test/.cog/conf/harness-mods.json'

/**
 * The world beneath the plugin: HOME, an optional harness-mods.json, a ledger
 * that records every `cat >>` row (hook-health), and counts of every write.
 * `conf` undefined = the file is missing (the default: everything off).
 */
export function world(on: any, conf?: Record<string, unknown>) {
  const ledger: any[] = []
  const writes: string[] = []
  const fetches: string[] = []
  mock.env(on, { HOME: '/home/test' })
  on('fs.read', (_$: any, e: any) =>
    e.path === CONF && conf !== undefined ? { value: JSON.stringify(conf) } : { deny: `ENOENT: ${e.path}` })
  on('fs.write', (_$: any, e: any) => { writes.push(e.path); return { value: undefined } })
  on('process.run', (_$: any, e: any) => {
    const stdin = String(e.init?.stdin ?? '')
    if (stdin.trim().startsWith('{')) ledger.push(JSON.parse(stdin))
    return RUN_OK
  })
  on('http.fetch', (_$: any, e: any) => { fetches.push(e.url); return { value: { status: 0, ok: false, headers: {}, text: '' } } })
  return { ledger, writes, fetches }
}

export const modes = (m: Partial<Record<'redact' | 'guard' | 'context' | 'status' | 'receipt', 'off' | 'shadow' | 'on'>>) =>
  ({ version: 1, mods: m })

/** Drive one tool call through the whole plugin; the bottom echoes the input it got. */
export async function toolCall($: any, on: any, command: string) {
  const seen: any[] = []
  on('tool.call', (_$: any, e: any) => { seen.push(e.input); return { result: { ok: true, input: e.input } } })
  const r = await $.tool.call({ tool: 'Bash', input: { command } }).catch((err: any) => ({ err: String(err) }))
  return { r, seen }
}
