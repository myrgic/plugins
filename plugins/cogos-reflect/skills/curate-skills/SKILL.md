---
name: curate-skills
description: "Use when asked to clean up skills or find what's stale."
version: 0.1.0
author: Chaz (chazmaniandinkle), Cog
license: MIT
tags: [reflection, skills, curation, cleanup]
---

# curate-skills

An evidence-backed keep / update / merge / retire pass over every installed skill. It proposes
changes; `review-proposals` applies them. Nothing is deleted.

## When to use

- "Clean up my skills." / "What aren't we using?" / "Too many skills." / "Is X still right?"

Don't use for: Hermes's own background curator (it marks stale skills on a timer). This is the
judgment pass on top of it, and covers the Claude Code and marketplace skills it can't see.

## Steps

`R` = this plugin's `scripts/` dir (`${CLAUDE_PLUGIN_ROOT}/scripts` in Claude Code; else `../../scripts` from this file).

1. **Inventory.** Run:
   ```
   python3 $R/reflect_skills.py
   ```
   It writes `skills.json` into the latest run (run `gather-history` first if there is none, so
   in-window invocation counts exist). Each skill gets: home, size, description length, uses in
   window, lifetime uses (Hermes telemetry), last used, absolute paths it names that no longer
   exist, and other skills with overlapping descriptions.
2. **Verdict per skill.** Judge from the evidence, not the name:

   | Verdict | When |
   |---|---|
   | keep | Used, accurate, distinct |
   | update | Used, but broken refs, a drifted procedure, or a description that won't trigger (no "Use when", trigger buried past ~57 chars) |
   | merge | Two skills cover the same ground; name the survivor |
   | retire | Unused for 60+ days and not pinned, or superseded |

   Leave plugin- or marketplace-owned skills alone unless broken: they update upstream. Report
   them, don't change them.
3. **Table.** Columns: skill, verdict, one-line reason with the number (e.g. "0 uses in 90d,
   2 broken paths"). Lead with update/merge/retire rows; collapse the keeps into a count.
4. **Proposals.** Each non-keep row becomes a `proposals/<slug>/` entry in `propose-fixes`
   format:
   - retire: target = move to the archive dir
   - merge: the combined SKILL.md
   - update: a diff

   Record each in the ledger: kind `stale-skill` or `overlap`.
5. Hand off to `review-proposals`.

## Report

≤ 5 lines: totals by verdict, the top three non-keep rows, then "want to go through them?".

## Rules

- **Archive, never delete.**
  - Hermes: the skill-management tool's archive.
  - Elsewhere: move to `<skills dir>/.archive/<date>-<name>/`.
- **Pinned skills are keep.**
- **A skill with zero uses but a narrow, rare trigger** (e.g. disaster recovery) is keep. Say why.
- **Description quality is a verdict, not a nit.** A skill that never triggers is as dead as an
  unused one.
