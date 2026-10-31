---
name: cogos-onboard
description: "Use on an operator's machine to set up a CogOS node and seed it from THEIR OWN claude.ai history. Triggers: 'set up CogOS for me', 'install my cog node', 'onboard me'."
version: 0.2.0
author: cog
license: MIT
metadata:
  hermes:
    tags: [cogos, onboarding, bootstrap, observatory, seed, harvest, agent]
---

# CogOS onboarding — install a node, seed it from the operator's OWN history

## When to use

A fresh Claude Code on the **operator's own machine** (their own, not a shared one), pointed here to set them up as a
CogOS operator. You (the agent) *are* the installer — this skill is the procedure you run.

**The delivery principle: this plugin ships METHOD, not anyone's data.** The operator's conversation
history is pulled from **their own claude.ai account, on their own machine**, under their own logged-in
browser. Nothing of their history ships in this plugin — the seeders and the harvester are generic
code. (This is deliberate: an operator's conversation data must never live in a shared repo.)

What it does:
1. Scaffold a user-tier CogOS node (`~/.cogos/`).
2. **Harvest the operator's own claude.ai history** (their browser, their account) into a seed file.
3. Seed their **observatory** + a pre-populated **stage room** from that history.
4. Mint an agent from that history via `agent-from-history` — the operator gets *their* agent (e.g.
   Wingnut, if that's who they worked with), derived from their own sessions, not shipped by us.
5. They mint their own identity keys.

No kernel-binary build at onboard time — the node tier is files + config the agent scaffolds.
This plugin ships **no agent and no operator data** — only the generic method.

## Preconditions

- `git`, `python3` (3.8+) present.
- The operator is **logged into claude.ai in a browser** they can expose over CDP — e.g. launch
  Chrome/Chromium with `--remote-debugging-port=9222` and log into claude.ai in it. The harvest reads
  **that** session (the proper OAuth token CANNOT reach claude.ai web history — wrong auth plane +
  Cloudflare; only the logged-in browser works; see `claude-web-history-harvest`).
- Confirm with the operator this is **their** machine before writing to `~/.cogos/`.
- Shipped with this plugin (generic code, **no operator data, no agent**):
  `scripts/harvest_claude_web.py`, `scripts/install_cogos_node.sh`, `scripts/seed_observatory.py`,
  `scripts/seed_room.py`. The operator's agent identity is **derived on their machine** from their own
  harvested history by the `agent-from-history` skill — nothing agent-specific ships here.

## Procedure

### 1. Harvest the operator's OWN claude.ai history

With their logged-in browser exposed over CDP (default `http://127.0.0.1:9222`):

```sh
# count first (cheap sanity check that the session is live), then harvest
python3 scripts/harvest_claude_web.py --cdp http://127.0.0.1:9222 --count-only
python3 scripts/harvest_claude_web.py --cdp http://127.0.0.1:9222 --out ./harvest
```

This fetches `/api/bootstrap` → their orgs → `chat_conversations` (paginated) → full messages, inside
their own tab so their cookies ride along and pass Cloudflare. It writes a conversation seed JSON
(`{source_url, subject, turns:[{role,text}]}`) under `./harvest/`.

**If it reports 0 conversations or a Cloudflare block (exit 3): the browser isn't logged in / the CDP
port isn't reachable. Stop and fix the session — do not proceed with an empty harvest.**

### 2. Scaffold the node + seed the observatory and a room

```sh
sh scripts/install_cogos_node.sh --conversation ./harvest/<conversation>.json
```

This scaffolds `~/.cogos/` (POSIX `for`-loop mkdir, not brace expansion) and runs `seed_observatory.py`
+ `seed_room.py` to populate the observatory and a pre-populated room from the harvested history.
Re-running is safe (idempotent). Verified clean in a fresh no-network container 2026-10-31. This step
ships **no agent** — it only lands the operator's own history into their own node.

### 3. Mint the operator's agent from that history

Hand off to the **`agent-from-history`** skill: point it at the just-seeded sessions (and the
counterpart they worked with, e.g. "Wingnut"). It reads *their* turns from the observatory, derives
how they work and how they work with that agent, and writes an `identity_<name>` card whose keyholder
is the operator — **derived on their machine, not shipped by us.** That is how the operator loads their
own agent (e.g. Wingnut) from their own session history.

### 4. Mint identity keys (operator-owned)

Generate their identity keypair (ed25519) under `~/.cogos/keys/`, mode 0600. Write the Identity CRD to
`~/.cogos/config/identity/default.yaml` with `sub` chosen by the operator. **They own these keys** —
never generate silently; prompt for `sub` and confirm before writing.

### 4. (Optional) Acquire the CogOS binary

- **Existing binary:** if `cogos` is on PATH, `ln -sf "$(command -v cogos)" ~/.cogos/bin/cogos`.
- **From GitHub:** clone the CogOS repo the operator points you at; pin the commit in
  `~/.cogos/config/org-pins.yaml`. **Do NOT invent a URL** — ask; a wrong clone target is worse than a
  prompt.

### 5. Handoff

Report: node tier at `~/.cogos/`, observatory seeded (N turns), the seeded history room, default agent, and
identity `<sub>`. They open the seeded room and are inside their own history with their agent.

## Pitfalls

- **Harvest reads only the operator's own session.** It holds no credentials and reads only the CDP
  endpoint it is given. It must never be pointed at anyone else's browser/account.
- **A 0/short harvest is a blocked session, not an empty history** — re-check the login, don't seed empty.
- **Don't clobber an existing node.** Identity + `USER.md` are skip-if-exists; the memory append is
  marker-guarded; the room seeder no-ops on re-run. If `~/.cogos/` already has a real identity, stop
  and ask — re-seeding preferences is fine, overwriting keys is not.
- **The observatory seed is additive** — adds one source, never prunes the operator's others.
- **Keys are theirs.** The seed carries a self-model and history, never a private key. The operator mints.
- **Verify, don't assume.** After step 2, count the seeded turns and read one back; after step 1, run
  `--count-only` first. "Copied successfully" is not verification.
