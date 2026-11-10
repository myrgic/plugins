---
name: reflect
description: "Use when asked how we work over time, or to make work easier."
version: 0.1.0
author: Chaz (chazmaniandinkle), Cog
license: MIT
tags: [reflection, self-improvement, skills, config, history]
---

# reflect

The front door for "look at how we work and make it easier". It routes to six single-verb
skills that share one run directory and one ledger. Read this, pick the route, load that skill.
It does no work of its own.

## When to use

- "How am I using you?" / "what are my patterns?" / "how do we work?"
- "Make this easier." / "What should we change?" / "Improve your setup."
- Any ask about past sessions, repeated corrections, stale skills, or "never do that again"
  where the operator didn't name a step.

Don't use for: reviewing *code* that was produced (that's code review), or recalling what was
said in a past session (that's session search).

## Route

| Operator says | Load | Then |
|---|---|---|
| "look at how we work and make it easier" (default) | `gather-history` → `find-friction` → `propose-fixes` → `review-proposals` | stop at the cards |
| "how am I using you / what are my patterns / what's slowing us down" | `gather-history` → `find-friction` | offer to propose fixes |
| "why do I keep having to tell you X" | `find-friction` scoped to X → `propose-fixes` | |
| "turn that into a skill / make that permanent / never do that again" | `propose-fixes` (direct) → `review-proposals` | |
| "what did we learn / wrap up / retro" | `session-retro` | |
| "show me the proposals / what's pending / did that fix work?" | `review-proposals` | |
| "clean up skills / what's stale / too many skills" | `curate-skills` | |

## The shared contract

- Run dir: one per run. `scripts/reflect_collect.py` creates it and prints its path. On a CogOS
  node it is `.cog/var/reflect/runs/<id>/` (never committed); elsewhere `~/.cogos-reflect/runs/<id>/`.
- Artifacts, each written by one step: `bundle.json` (gather) → `findings.json` (find) →
  `proposals/<slug>/` (propose) → `cards.md` (review).
- Ledger: `scripts/reflect_ledger.py`. Append-only; the only writer of finding state. Every step
  reads it; denials stick.
- Any step can run first: if its input is missing, it runs the previous step with defaults and
  says so in one line.

All scripts live in this plugin's `scripts/` dir, are stdlib-only Python, take `--help`, and
print JSON.

## Defaults

30 days, every source found, at most 3 proposals, nothing applied without an approved card.
Override in words: "just this week", "only Claude Code", "only the stage repo", "up to 5".

## Pitfalls

- Don't paste analysis into chat. Every step ends with ≤ 5 lines, a path, and the obvious next
  step.
- Don't skip the gate because the operator sounds sure. A yes is a yes to a specific card.
