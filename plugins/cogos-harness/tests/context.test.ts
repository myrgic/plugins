// tests/context.test.ts — cog-context skeleton: loads, and is a pure no-op when off.
// Owner: cog-context. Replace the stub cases as the mod gains behaviour; keep
// the 'off' cases (the plugin must stay inert with no config file).
import { describe, expect, test } from 'claude-code/testing'
import { world, modes } from './_kit'

describe('cog-context', () => {
  test('prompt.context: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    on('prompt.context', (_$: any, e: any) => ({ blocks: e.blocks }))
    const r: any = await $.prompt.context({ blocks: [{ name: 'x', text: 'y' }] } as any)
    expect(r.blocks).toEqual([{ name: 'x', text: 'y' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('prompt.context: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ context: 'off' }))
    on('prompt.context', (_$: any, e: any) => ({ blocks: e.blocks }))
    const r: any = await $.prompt.context({ blocks: [{ name: 'x', text: 'y' }] } as any)
    expect(r.blocks).toEqual([{ name: 'x', text: 'y' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('prompt.context: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ context: 'shadow' }))
    on('prompt.context', (_$: any, e: any) => ({ blocks: e.blocks }))
    const r: any = await $.prompt.context({ blocks: [{ name: 'x', text: 'y' }] } as any)
    expect(r.blocks).toEqual([{ name: 'x', text: 'y' }])
    const mine = w.ledger.filter((row) => row.hook === 'mod/context/prompt.context')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/context/'))).toBe(true) // other mods stay off
  })
})
