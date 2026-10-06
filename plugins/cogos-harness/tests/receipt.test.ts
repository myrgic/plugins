// tests/receipt.test.ts — cog-receipt skeleton: loads, and is a pure no-op when off.
// Owner: cog-receipt. Replace the stub cases as the mod gains behaviour; keep
// the 'off' cases (the plugin must stay inert with no config file).
import { describe, expect, test } from 'claude-code/testing'
import { world, modes, toolCall } from './_kit'

describe('cog-receipt', () => {
  test('tool.call: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('turn.complete: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    on('turn.complete', () => ({ text: 'done' }) as any)
    const r: any = await $.turn.complete({ answer: 'a', durationMs: 1, isAborted: false, turnId: 't1' } as any)
    expect(r.text).toBe('done')
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('tool.call: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ receipt: 'off' }))
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('turn.complete: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ receipt: 'off' }))
    on('turn.complete', () => ({ text: 'done' }) as any)
    const r: any = await $.turn.complete({ answer: 'a', durationMs: 1, isAborted: false, turnId: 't1' } as any)
    expect(r.text).toBe('done')
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('tool.call: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ receipt: 'shadow' }))
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    const mine = w.ledger.filter((row) => row.hook === 'mod/receipt/tool.call')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/receipt/'))).toBe(true) // other mods stay off
  })
  test('turn.complete: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ receipt: 'shadow' }))
    on('turn.complete', () => ({ text: 'done' }) as any)
    const r: any = await $.turn.complete({ answer: 'a', durationMs: 1, isAborted: false, turnId: 't1' } as any)
    expect(r.text).toBe('done')
    const mine = w.ledger.filter((row) => row.hook === 'mod/receipt/turn.complete')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/receipt/'))).toBe(true) // other mods stay off
  })
})
