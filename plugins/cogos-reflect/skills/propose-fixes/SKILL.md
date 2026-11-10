---
name: propose-fixes
description: "Use when asked to make a fix stick: skill, hook, rule, script."
version: 0.1.0
author: Chaz (chazmaniandinkle), Cog
license: MIT
tags: [reflection, proposals, skills, hooks, config]
---

# propose-fixes

Turns recurring findings into at most 3 concrete, reviewable changes. Each change is the
**cheapest mechanism that actually stops the friction**, checked against what already exists
and against everything the operator has already refused.

## When to use

- "What should we change?" / "Fix that for good." / "Turn that into a skill." / "Never do that again."
- After `find-friction`.

Don't use for: applying anything. That's `review-proposals`. This skill writes drafts only.

## Steps

1. **Input.** Use `findings.json` from the latest run. If there is none, run `find-friction`
   first and say so. A finding the operator names in chat is input too: record it, then go on.
2. **Pick ≤ 3.** Take findings with sessions ≥ 2, strongest evidence first. Operator-named
   findings skip the threshold. If the operator asks for more, go up to 5.
3. **Does it already exist?** For each finding run:
   ```
   python3 $R/reflect_recur.py "<3–6 distinctive words>"
   ```
   - Hit, and the friction recurred anyway: the lesson exists and isn't holding. Propose
     moving or upgrading it (a memory line becomes a skill step or a hook), or fixing its
     trigger. Never write it a second time.
   - Hit on a skill that covers it: propose a patch to that skill.
4. **Ledger.** Run `python3 $R/reflect_ledger.py check --title "<title>"`.
   - `denied-before`: skip it unless the evidence is new. Name the earlier denial.
5. **Choose the mechanism.** Go down this ladder and stop at the first rung that would actually
   stop the friction:
   1. **Patch an existing skill.** Use when its steps or trigger are the gap.
   2. **New skill.** Use for a multi-step workflow re-derived in 2+ sessions.
   3. **Script or command.** Use for an exact incantation typed again and again.
   4. **Hook.** Use for a behavior that must be blocked or checked mechanically.
   5. **Permission allowlist entry.** Use for read-only calls that keep prompting. Never
      allowlist an interpreter, shell, package runner, `make *`, `npm run *`, `gh api *`,
      `docker exec`, or `sudo` wildcard. Those grant arbitrary execution.
   6. **Memory line.** Use for a fact about the operator or environment.
   7. **Reflective note** (CogOS: `.cog/mem/reflective/`). Use for know-why that is retrieved
      when relevant but not loaded every session.
   8. **Always-on instruction** (CLAUDE.md / AGENTS.md / SOUL.md). This is the last resort,
      because every session pays for it. Say why nothing cheaper works.
6. **Write the proposal dir** at `proposals/<slug>/`. The slug is intent-bearing, never a bare
   number.
   - `PROPOSAL.md` must contain these bolded fields, each 1–2 lines (`reflect_render.py check`
     enforces them):
     ```
     # <imperative title>
     **What changes:** <file, named in plain words>
     **What you get:** <the behavior that's different next time>
     **Why you might say no:** <one honest reason; every proposal has one>
     **Mechanism:** <rung> (cheaper rungs rejected because …)
     **Evidence:** <N sessions, M occurrences, finding id>
     **Exact change:** change.diff (or the new file path inside this dir)
     ```
   - `EVIDENCE.md`: session ids with dates, a one-line description of each occurrence, ≤ 3
     short quotes, and the recur/ledger check results.
   - `change.diff`: a unified diff against the real target file. For a new skill, write the
     full `SKILL.md` in this dir, plus `evals.json` with 3 should-trigger prompts and 2
     should-not prompts taken from the evidence.
7. **Record.** Run `reflect_ledger.py status --id <F-id> --to proposed --ref <proposal dir>`.
8. **Validate.** Run `python3 $R/reflect_render.py check`, then `reflect_render.py cards`.

## Report

The cards (rendered), then "approve, cheaper fix, refine, deny, or later?". Then hand off to
`review-proposals`.

## Rules

- **Generalize from the pattern.** Don't replay the transcript: a skill drafted from three
  sessions must work for the fourth.
- **No secrets in proposals.** Use placeholders (`$API_KEY`, `<your-token>`). If the pattern
  can't be separated from private data, drop it and say why.
- **No dates, commit hashes, or narrative inside the change itself.** Those go in EVIDENCE.md.
- **Too general changes nothing** ("be careful"). **Too specific never fires again.** Name the
  situation and the action.
