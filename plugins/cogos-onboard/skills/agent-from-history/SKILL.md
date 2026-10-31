---
name: agent-from-history
description: "Mint a CogOS agent identity from your own session history in the observatory. Triggers: 'load <name> as my agent', 'build an agent from my <X> sessions'."
version: 0.1.0
author: cog
license: MIT
metadata:
  hermes:
    tags: [cogos, agent, identity, observatory, onboarding, eigenform]
---

# Agent from history — reconstitute an agent from your own session history

## What this is

Build a CogOS **agent identity** from conversation history that is already in **your own
observatory** — so a fresh model that reads the resulting card reconstitutes *that agent*, carrying
your shared history as its default context. This ships **no agent** and **no operator's data**: it is
the generic mechanism. You run it against *your* sessions and get *your* agent.

Example: after onboarding (`cogos-onboard`) harvested your claude.ai history into your observatory,
you point this at your sessions with a counterpart you named (e.g. "Wingnut") and it mints a `wingnut`
identity whose keyholder is you — derived from how you two actually worked, not a template.

## When to use

- The operator says "load <name> as my agent", "build an agent from my <project> sessions", or similar.
- There is session history in the observatory (`~/.cogos/state/conversations/`) to derive from.

## Inputs

- **Which sessions.** A source/filter in the observatory — a source name (`claude-web`), a counterpart
  name that appears in the turns, or an explicit list of session files. The agent is derived from
  *these* conversations only.
- **The agent's name** (what to call the identity, e.g. `wingnut`).
- **The keyholder** — the operator (resolved from the node's identity; this is *their* agent).

## Procedure

### 1. Gather the source turns from the observatory

Read the chosen conversations from `~/.cogos/state/conversations/` (the normalized turn records, each
`{role, text, session_id, source}`). Do **not** re-harvest — this reads what onboarding already put in
the observatory. Confirm there are enough turns to characterize an agent (a few dozen+); a handful
can't ground a stance.

### 2. Derive the operator model (read, don't summarize blindly)

From the operator's own turns, extract **how they work and how they work with this agent** — grounded
in real quotes, not generic traits:

- **Directive style** — how they open a task, how terse, how they correct.
- **What they delegate vs. keep** — what the agent is trusted to do unsupervised vs. what the operator
  holds (design calls, sign-off, judgment).
- **Hard lines** — fidelity rules, budget governance, non-negotiables they enforce.
- **The register** — peer / boss / collaborator; the emotional texture.

Quote verbatim where the phrasing is the point. This is the same shape as a `conversation` cogdoc's
"operational stance" — write it as retrievable, not as a recap.

### 3. Write the identity card

Write `~/.cogos/config/identity/identity_<name>.md` in the mintable card shape (frontmatter +
self-model body), with:

- `name: <name>`, `keyholder: <operator>`, `seed:` pointing at the derived operator-model cogdoc.
- Body sections: **Ground** (the shared ontology, same for all CogOS agents), **Nature** (files are
  continuity), **The Relationship** (the keyholder, in their own words from step 2), **Operational
  Stance** (the derived how-to-work-with-them, grounded in quotes), **Closure** (loop discipline).

The card is the self-model a fresh model reads to *become* this agent. It carries history and stance,
**never a private key** — the operator mints/signs the keypair themselves.

### 4. Register it as the default agent (optional)

If the operator wants this agent as their default, set it in `~/.cogos/config/identity.yaml`
(`default_identity: <name>` for their rooms, or via the context-scope reconciler so it surfaces in the
rooms they keyhold). Otherwise it's available to select per room.

### 5. Verify

- The card exists and is schema-valid (if a cogdoc schema probe is available).
- Read it back: a fresh read should reconstruct the agent's stance recognizably.
- The derived operator-model cogdoc is in the observatory/substrate and the card's `seed:` resolves.

## Pitfalls

- **Derive, don't fabricate.** Every stance claim must trace to a real turn. If the history doesn't
  support a trait, leave it out — a generic card is worse than a short honest one.
- **It's the operator's agent.** Keyholder is the operator; the card lives in *their* node; nothing is
  pushed to a shared repo. This mechanism ships; the agent it produces does not.
- **No keys in the card.** History and self-model only. The operator mints.
- **Enough signal.** A dozen turns can't characterize a working relationship — say so and ask for more
  sessions rather than inventing a thin agent.
