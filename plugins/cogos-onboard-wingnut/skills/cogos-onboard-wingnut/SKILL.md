---
name: cogos-onboard-wingnut
description: "Use on Vroku's machine to install a CogOS node and seed his observatory with the Wingnut history + preferences. Triggers: 'set up CogOS for me', 'install my cog node', 'onboard me (Vroku)'."
version: 0.1.0
author: cog
license: proprietary
metadata:
  hermes:
    tags: [cogos, onboarding, bootstrap, observatory, seed, vroku, wingnut]
---

# CogOS onboarding — install Vroku's node, seed it with Wingnut

## When to use

A fresh Claude Code on **Vroku's** machine, pointed here to set him up as a CogOS operator. You (the
agent) *are* the installer — this skill is the procedure you run. It does three things:

1. Scaffold a user-tier CogOS node (`~/.cogos/`), per `node-bootstrap-and-plugin-install-flow`.
2. Seed his **observatory** with the Wingnut conversation history (973 turns).
3. Seed his **identity + preferences**: `USER.md`, preference memory, and `identity_wingnut` as his
   default agent — so his Cog starts already knowing how he works.

This is the skill-does-the-install model: no kernel binary build required at onboard time. The node
tier is files + config the agent scaffolds; the kernel daemon is wired in a later step (or runs in
his sandbox). Seeding the observatory is the novel payload and the reason to onboard *this* way.

## Preconditions

- `git`, `python3` present. (`podman` optional — only if running the node in a container.)
- The **cognition pack** `vroku-wingnut.pack` — fetched at install time from its hosted artifact
  (release / single-use URL), NOT shipped in this repo. It carries the operator's own conversation and
  is private data; it must never be committed into a shared/public repo. The plugin ships thin: this
  skill + the seeder scripts + a pointer to the pack. Unpack it (`tar xzf vroku-wingnut.pack`) and
  verify every item's sha256 against `pack.json` before installing.
- Confirm with the operator this is Vroku's machine before writing to `~/.cogos/`.

## Procedure

### 1. Scaffold the user tier (idempotent)

```sh
for d in bin config/identity hooks mem state/conversations logs org; do mkdir -p ~/.cogos/$d; done
test -f ~/.cogos/bootstrap-state.json || echo '{"bootstrap_version":"0.1.0","phases":{}}' > ~/.cogos/bootstrap-state.json
```

Re-running is safe — `mkdir -p` and the `test -f` guard mean a partial install resumes. (Use the
`for` loop, not `mkdir -p ~/.cogos/{a,b}` — brace expansion is bash-only and silently fails under the
POSIX `sh` many install environments default to. Verified in a sandbox run 2026-10-31.)

### 2. Acquire CogOS (per RFC Phase 3; choose by availability)

- **Existing binary:** if `cogos` is on PATH, symlink it: `ln -sf "$(command -v cogos)" ~/.cogos/bin/cogos`.
- **From GitHub:** clone the org substrate the operator points you at (the public CogOS repo). Pin the
  commit in `~/.cogos/config/org-pins.yaml`. Do NOT invent a URL — ask the operator for the repo if
  it is not already configured; a wrong clone target is worse than a prompt.

Record the mode + sha in `bootstrap-state.json.phases.binary`.

### 3. Seed identity + preferences

```sh
cp seed/USER.md              ~/.cogos/config/USER.md
cp seed/identity_wingnut.md  ~/.cogos/config/identity/identity_wingnut.md
# preference memory → his memory store (append, don't clobber an existing one)
cat seed/USER-memory.md >> ~/.cogos/mem/USER.md
```

`identity_wingnut` is Vroku's **default agent** — the eigenform seeded from his own collaboration.
He mints/signs the identity keypair himself (RFC Phase 5); this only places the self-model it starts
from.

### 4. Seed the observatory (the payload)

Normalize the Wingnut conversation export into his observatory index, so his Cog can retrieve the
shared history from turn one:

```sh
python3 scripts/seed_observatory.py seed/wingnut-conversation.json ~/.cogos/state/conversations/
```

The script writes one index file per the observatory's `<source>__<session>.json` shape, source
`wingnut-share`, each turn carrying `refs.stable_id`. Verify: the file exists and the turn count
matches the export (973).

### 5. Mint identity keys (RFC Phase 5 — operator-owned)

Generate his identity keypair (ed25519) under `~/.cogos/keys/`, 0600. Write the Identity CRD to
`~/.cogos/config/identity/default.yaml` with `sub` chosen by Vroku. **He owns these keys** — never
generate them silently; prompt for `sub` and confirm before writing.

### 6. Handoff

Report: node tier at `~/.cogos/`, observatory seeded (N turns), default agent Wingnut, identity
`<sub>`. Next: open a repo with a `.cog/`, run the node, start a session.

## Pitfalls

- **Don't clobber an existing node.** Every write is guarded or appended. If `~/.cogos/` already has a
  real identity, stop and ask — re-seeding preferences is fine, overwriting his keys is not.
- **The observatory seed is additive.** It adds a `wingnut-share` source; it never prunes his other
  sources (see `conversation-observatory`: never reconcile an ingest surface down).
- **Keys are his.** The seed carries a self-model and history, never a private key. Vroku mints.
- **Verify, don't assume.** After step 4, actually count the seeded turns and read one back. "Copied
  successfully" is not verification.
