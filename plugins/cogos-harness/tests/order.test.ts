// tests/order.test.ts — the PreToolUse order contract (plan §3.2):
// redact → guard → context → status → receipt; redact BEFORE guard.
// Owner: cog-redact. The two cases marked PENDING-CONTRACT become real
// assertions when cog-redact and cog-guard land (see each comment).
import { describe, expect, test } from 'claude-code/testing'
import { world, modes, toolCall } from './_kit'
import { ORDER, before } from '../lib/order.mjs'

const ALL_SHADOW = modes({ redact: 'shadow', guard: 'shadow', context: 'shadow', status: 'shadow', receipt: 'shadow' })

describe('order', () => {
  test('the typed contract: redact precedes guard, receipt is last', () => {
    expect([...ORDER]).toEqual(['redact', 'guard', 'context', 'status', 'receipt'])
    expect(before('redact', 'guard')).toBe(true)
    expect(before('guard', 'redact')).toBe(false)
    expect(ORDER[ORDER.length - 1]).toBe('receipt')
  })

  test('the real chain: on tool.call, redact runs before guard, and receipt after both', async ($: any, on) => {
    const w = world(on, ALL_SHADOW)
    await toolCall($, on, 'echo hi')
    // Every stub logs BEFORE calling next(e), so ledger order is chain order.
    const hooks = w.ledger.map((row) => row.hook).filter((h) => h.endsWith('/tool.call'))
    expect(hooks).toEqual(['mod/redact/tool.call', 'mod/guard/tool.call', 'mod/receipt/tool.call'])
  })

  test('control: the order check can fail (a reversed expectation is detected)', async ($: any, on) => {
    const w = world(on, ALL_SHADOW)
    await toolCall($, on, 'echo hi')
    const hooks = w.ledger.map((row) => row.hook).filter((h) => h.endsWith('/tool.call'))
    expect(hooks).not.toEqual(['mod/guard/tool.call', 'mod/redact/tool.call', 'mod/receipt/tool.call'])
  })

  test('with only guard on, redact being off does not reorder or drop guard', async ($: any, on) => {
    const w = world(on, modes({ guard: 'shadow' }))
    await toolCall($, on, 'echo hi')
    expect(w.ledger.map((row) => row.hook)).toEqual(['mod/guard/tool.call'])
  })

  // PENDING-CONTRACT (cog-redact + cog-guard PRs turn these on): a payload with
  // a fake key plus an attribution line is denied by guard; the deny text holds
  // the «secret:…» placeholder, never the key; receipt's record holds no key.
  // Kept as a running test over the stubs so the file is green today: with the
  // stubs nothing redacts or denies, so it asserts the pass-through baseline.
  test('PENDING-CONTRACT baseline: stubs neither deny nor rewrite a secret-bearing call', async ($: any, on) => {
    world(on, ALL_SHADOW)
    const fake = 'sk-ant-' + 'TESTONLY'.repeat(4)
    const { r, seen } = await toolCall($, on, `git commit -m "x\n\nCo-Authored-By: Claude" # ${fake}`)
    expect(JSON.stringify(r)).not.toContain('deny')
    expect(String(seen[0]?.command)).toContain(fake)
  })
})
