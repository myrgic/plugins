// tests/redact.test.ts — cog-redact skeleton: loads, and is a pure no-op when off.
// Owner: cog-redact. Replace the stub cases as the mod gains behaviour; keep
// the 'off' cases (the plugin must stay inert with no config file).
import { describe, expect, test } from 'claude-code/testing'
import { world, modes, toolCall } from './_kit'

describe('cog-redact', () => {
  test('tool.call: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('prompt.submit: missing config file (default): every hook is next(e), no ledger row, no writes', async ($: any, on) => {
    const w = world(on, undefined)
    on('prompt.submit', (_$: any, e: any) => ({ text: e.text }))
    const r: any = await $.prompt.submit({ text: 'hello' } as any)
    expect(r.text).toBe('hello')
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('tool.call: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ redact: 'off' }))
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('prompt.submit: explicit off: same as missing', async ($: any, on) => {
    const w = world(on, modes({ redact: 'off' }))
    on('prompt.submit', (_$: any, e: any) => ({ text: e.text }))
    const r: any = await $.prompt.submit({ text: 'hello' } as any)
    expect(r.text).toBe('hello')
    expect(w.ledger).toEqual([])
    expect(w.writes).toEqual([])
    expect(w.fetches).toEqual([])
  })
  test('tool.call: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ redact: 'shadow' }))
    const { r, seen } = await toolCall($, on, 'echo hello')
    expect(JSON.stringify(r)).toContain('echo hello')
    expect(seen).toEqual([{ command: 'echo hello' }])
    const mine = w.ledger.filter((row) => row.hook === 'mod/redact/tool.call')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/redact/'))).toBe(true) // other mods stay off
  })
  test('prompt.submit: shadow: event unchanged, one shadow ledger row per hook run', async ($: any, on) => {
    const w = world(on, modes({ redact: 'shadow' }))
    on('prompt.submit', (_$: any, e: any) => ({ text: e.text }))
    const r: any = await $.prompt.submit({ text: 'hello' } as any)
    expect(r.text).toBe('hello')
    const mine = w.ledger.filter((row) => row.hook === 'mod/redact/prompt.submit')
    expect(mine.length).toBe(1)
    expect(mine[0].shadow).toBe(true)
    expect(mine[0].would).toBe('none')
    expect(w.ledger.every((row) => row.hook.startsWith('mod/redact/'))).toBe(true) // other mods stay off
  })
})
