---
name: session-retro
description: "Use for this session: what went wrong, lessons, never again."
version: 0.1.0
author: Chaz (chazmaniandinkle), Cog
license: MIT
tags: [reflection, retro, session, lessons, friction]
---

# session-retro

A short, honest pass over **this** session: what was attempted, where the operator had to correct
you, what got redone. Findings go into the shared ledger so the weekly pass can see whether they
recur. Only an explicit "never again" becomes a proposal immediately.

## When to use

- "What did we learn?" / "Wrap up." / "Retro." / "What went wrong there?"
- "Don't let that happen again." (immediate proposal path)
- End of a substantial session (several commits, an incident, or a wrong claim), if the operator
  wants it.

Don't use for: a clean one-shot task. A retro of nothing is noise.

## Steps

`R` = this plugin's `scripts/` dir (`${CLAUDE_PLUGIN_ROOT}/scripts` in Claude Code; else `../../scripts` from this file).

1. **Review the session in three bullets** from the conversation itself:
   - **Attempted:** what it set out to do and whether that landed. Two sentences max.
   - **Corrected by the operator:** each correction in one line, quoting their words. "None" if
     none. A slip you caught yourself before they saw it doesn't count.
   - **Rework:** artifacts redone, or loops of 3+ rounds, with the cause. Include your own wrong
     claims; they're the highest-value lines.
2. **Findings.** A correction counts only if a concrete action would have prevented it. "Be more
   careful" is not an action. For each one:
   ```
   python3 $R/reflect_ledger.py check --title "<what went wrong>"
   ```
   - If it matches an existing finding: `add` with the same id. The count rises; say "seen before".
   - If it's new: `add --kind session-retro --sessions 1 --evidence <this session id>`.
3. **Check whether the lesson already exists:** run `reflect_recur.py "<words>"`. If it does and
   it fired anyway, the finding is `lesson-not-holding`. That is the strongest signal there is.
4. **Immediate proposal** only when the operator said "never/always/don't again", or the finding is
   `lesson-not-holding`: run the `propose-fixes` steps for that one finding, then
   `review-proposals`. Everything else waits for recurrence.
5. **Write it down** (CogOS node): `.cog/mem/reflective/retros/<date>-<slug>.cog.md` with sector
   frontmatter:
   ```yaml
   ---
   title: "<session in a phrase>"
   memory_sector: reflective
   synthesis_type: retrospective
   source_memories: [<session ids>]
   tags: [retro]
   ---
   ```
   Body: the three bullets, the findings with ledger ids, and a `## My own method failures`
   section. Commit it. Elsewhere, append the same text to the project memory dir or skip
   writing; the ledger rows are what matter.

## Report

The three bullets, the finding ids ("new" or "seen before ×N"), and any proposal card. ≤ 10 lines.

## Rules

- **Quote corrections; don't paraphrase them softer.**
- **Never write `journal/` or authored reflections.** Those are not generated output.
- **Don't propose from one occurrence** unless the operator asked for "never again".
