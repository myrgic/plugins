---
name: cogos-cloud-node
description: "Use to set up a CogOS node in a Claude Code Cloud environment (phone-friendly). Triggers: 'set up my cloud cog node', 'cogos in the cloud', 'onboard me to CogOS on my phone'."
version: 0.1.0
author: myrgic
license: MIT
---

# CogOS cloud node — guided setup

Stand up a CogOS node that lives in a Claude Code **Cloud environment**, so you can use it from a phone
(a phone has no filesystem/shell; the Cloud session is where the node runs). This skill guides the
steps; the **operator-template repo** does the actual bootstrap (its committed SessionStart hook
scaffolds the node when the repo is attached to a Cloud env — no UI paste, no data shipped from anyone).

## The flow

### 1. Fork the operator-template repo (yours, clean)
Fork `myrgic/cogos-operator-template` to your own GitHub account. It carries only the mechanism — a
SessionStart hook + a data-export seeder — and **no one's memory or identity**.

### 2. Create a Cloud environment and attach your fork
In Claude Code → New environment → **Cloud**. Attach your forked repo. Set:
- **Network access:** Full (this is *capability*, not containment — an env-var proxy is ignorable; it
  lets the node reach git/pip, it does not confine it).
- **Network secrets:** **your own** `GITHUB_PAT` (+ a provider key if needed) — in the *Network secrets*
  slot, not Environment variables. Never someone else's token.

### 3. Open a session — the node self-scaffolds
When the Cloud session starts, the repo's SessionStart hook runs `cogos-node-ensure.py` and scaffolds
`~/.cogos/`. Confirm: `cat ~/.cogos/bootstrap-state.json`.

### 4. Seed your own history (phone-doable)
Request a **data export** from claude.ai (Settings → Privacy → Export — works on a phone). Commit the
`conversations.json` to your fork, then:
```sh
python3 .cog/hooks/seed_from_export.py conversations.json ~/.cogos/state/conversations/
```
Your history stays yours.

### 5. Use it
You now have a CogOS node in the cloud, reachable from your phone's Claude.

## Honest note
Creating the Cloud environment and setting Network secrets are settings-UI actions. Whether they're
fully doable on a phone (vs. needing a desktop once) is **not yet confirmed** — see the friction
checklist in the README. The node itself, once set up, is used from the phone.
