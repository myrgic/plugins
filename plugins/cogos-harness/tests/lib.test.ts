// tests/lib.test.ts — the shared lib/ helpers (skeleton-owned).
import { describe, expect, test } from 'claude-code/testing'
import { parseCfg, allOff, MOD_NAMES } from '../lib/cfg.mjs'
import { ageMs, staleSuffix, STALE_MS, readVitals, writeVitals } from '../lib/cache.mjs'
import { ledgerRow } from '../lib/ledger.mjs'

describe('lib/cfg', () => {
  test('missing / empty / malformed / wrong-shape config → every mod off', () => {
    for (const t of [null, undefined, '', '{', '[]', '"on"', '{"mods": "on"}']) {
      expect(parseCfg(t as any).mods).toEqual(allOff())
    }
    expect(parseCfg(null).source).toBe('default-off')
  })
  test('valid modes are read; unknown modes and unknown mods are ignored', () => {
    const c = parseCfg(JSON.stringify({ version: 1, mods: { redact: 'on', guard: 'shadow', context: 'ON', status: 'yes', bogus: 'on' } }))
    expect(c.mods).toEqual({ redact: 'on', guard: 'shadow', context: 'off', status: 'off', receipt: 'off' })
    expect(c.source).toBe('file')
    expect([...MOD_NAMES]).toEqual(['redact', 'guard', 'context', 'status', 'receipt'])
  })
})

describe('lib/cache', () => {
  test('age: fresh shows nothing, >2 min shows the age, absent says so', () => {
    const now = 10_000_000
    expect(staleSuffix({ written_at: now - 1000 }, now)).toBe('')
    expect(staleSuffix({ written_at: now - STALE_MS - 60_000 }, now)).toBe(' (stale 3m)')
    expect(staleSuffix({ written_at: now - 54 * 3600_000 }, now)).toBe(' (stale 2d)')
    expect(staleSuffix(undefined, now)).toBe(' (no vitals)')
    expect(ageMs(undefined, now)).toBe(Infinity)
  })
  test('write stamps written_at, mirrors to ~/.cog/run/vitals.json; read reports age', async () => {
    let t = 1_000_000
    let held: any
    const files: Record<string, string> = {}
    const io = {
      now: async () => t,
      home: async () => '/home/test',
      write: async (p: string, s: string) => { files[p] = s },
      setVitals: async (v: any) => { held = v },
      getVitals: async () => ({ value: held, version: 1 }),
    }
    await writeVitals(io, { kernel: { reachable: true } })
    expect(held.written_at).toBe(1_000_000)
    expect(JSON.parse(files['/home/test/.cog/run/vitals.json']!).kernel.reachable).toBe(true)
    t += STALE_MS + 1
    const r = await readVitals(io)
    expect(r.stale).toBe(true)
    expect(r.age_ms).toBe(STALE_MS + 1)
  })
})

describe('lib/ledger', () => {
  test('row shape matches the hook-health ledger; shadow rows carry would', () => {
    expect(ledgerRow({ mod: 'guard', event: 'tool.call', ms: 1.6, ts: 2000 })).toEqual({ ts: 2, hook: 'mod/guard/tool.call', status: 'ok', ms: 2 })
    expect(ledgerRow({ mod: 'guard', event: 'tool.call', shadow: true, would: 'deny', ts: 0 })).toEqual({ ts: 0, hook: 'mod/guard/tool.call', status: 'ok', ms: 0, shadow: true, would: 'deny' })
  })
})
