// tests/status.test.ts — cog-status skeleton: loads, and is a pure no-op when off.
// Owner: cog-status. Replace the stub cases as the mod gains behaviour; keep
// the 'off' cases (the plugin must stay inert with no config file).
import { describe, expect, test } from 'claude-code/testing'
import { world, modes } from './_kit'

describe('cog-status', () => {
  test('session.measure: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    on('session.measure', (_$: any, e: any) => ({ changed: e.changed }))
    const r: any = await $.session.measure({ context: {}, rateLimits: [], changed: [] } as any)
    expect(r.changed).toEqual([])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('session.measure: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ status: 'off' }))
    on('session.measure', (_$: any, e: any) => ({ changed: e.changed }))
    const r: any = await $.session.measure({ context: {}, rateLimits: [], changed: [] } as any)
    expect(r.changed).toEqual([])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('session.measure: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ status: 'shadow' }))
    on('session.measure', (_$: any, e: any) => ({ changed: e.changed }))
    const r: any = await $.session.measure({ context: {}, rateLimits: [], changed: [] } as any)
    expect(r.changed).toEqual([])
    const mine = w.ledger.filter((row) => row.hook === 'mod/status/session.measure')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/status/'))).toBe(true) // other mods stay off
  })
})
