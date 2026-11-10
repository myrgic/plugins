---
name: review-proposals
description: "Use to approve proposals or check if an applied fix worked."
version: 0.1.0
author: Chaz (chazmaniandinkle), Cog
license: MIT
tags: [reflection, approval, gate, apply, verify]
---

# review-proposals

The only path from a drafted change to a real one. Shows one card per proposal, takes the
operator's answer in plain words, applies only what was approved, and records every decision so
refused ideas don't come back.

## When to use

- "Show me what you'd change." / "Go through the proposals." / "What's pending?"
- Right after `propose-fixes`.
- "Did that fix work?" (verify mode, below).

## Steps

1. **Load the cards.** Run `python3 $R/reflect_render.py cards` in the latest run, or the run
   the operator names. If it has no proposals, check the ledger for `proposed` items from older
   runs: `reflect_ledger.py show --status proposed`.
2. **Present all cards at once**, then ask once. Use the question tool if the harness has one;
   otherwise ask in plain text. Accept one answer per card:

   | Answer | Do |
   |---|---|
   | approve | Apply it (step 3), then `status --to applied --ref <target>` |
   | cheaper fix / "make it a skill instead" | Rewrite the proposal as that mechanism, show the card again, ask again |
   | refine: "<edit>" | Edit the proposal, show the card again, ask about that one only |
   | deny: "<reason>" | `status --to denied --reason "<their words>"`. It won't be proposed again without new evidence |
   | later | `status --to deferred` |

   No answer is not approval. If the conversation moves on, leave the cards `proposed`.
3. **Apply an approved card** to its real home:
   - **Skill**:
     - Hermes: the skill-management tool (create or patch).
     - Claude Code: `~/.claude/skills/<name>/` or `<project>/.claude/skills/<name>/`.
     - Marketplace skills: open a PR, never write to main.
   - **New skill**: first run its `evals.json` with and without the skill on a cheap sub-agent.
     If the skill doesn't change the outcome on at least 2 of 3 should-trigger prompts, don't
     install it. Report that, and mark the card `deferred` with the reason.
   - **Hook / settings / permissions**: merge into the existing file. Keep existing keys, dedupe,
     don't reorder unrelated fields. Show the final diff.
   - **Memory**: the memory tool. **Reflective note**: write the cogdoc with the sector's
     frontmatter (`source_memories` = evidence ids) and commit it.
   - **Instruction file**: edit the narrowest file that applies (project over global).
4. **Verify the apply.** Re-read the target, or run the hook or script once. For skills, check
   that a should-trigger prompt actually loads it.
5. **Record.** One ledger `status` row per card. Every card ends `applied`, `denied`, `deferred`
   or still `proposed`.

## Verify mode: "did that fix work?"

For each `applied` finding older than 7 days:
1. Run `gather-history` over the period since it was applied.
2. Count the same signal (same cluster words, the same command shape, or the same error family).
3. Record the result:
   - It dropped: mark it `verified`. On CogOS, write or update a
     `.cog/mem/reflective/consolidations/` doc (pattern → fix → before/after counts).
   - It didn't drop: open a new `lesson-not-holding` finding pointing at the applied one.

## Report

Cover four things:
- What was applied and where (paths).
- What was denied, in their words.
- What's still pending.
- For new skills, the trigger phrase that loads it.

## Rules

- **One yes, one card.** "Sure, do them all" covers the cards shown, not later edits.
- **Never widen a permission** beyond the card. Never write `permissions.deny` or `ask`
  unless the card says so.
- **Archive, don't delete.** Retiring or replacing a file keeps the old copy.
