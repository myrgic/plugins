# cogos-cloud-node

Set up a CogOS node in a Claude Code **Cloud environment** — usable from a phone. The plugin is a thin
guide; the **operator-template repo** owns bootstrap (a committed SessionStart hook scaffolds the node).
Ships method only, no operator data.

## Install

```
claude plugin marketplace add myrgic/plugins
claude plugin install cogos-cloud-node@plugins
```

Then: "set up my cloud cog node".

## Friction checklist (HONEST — every step tagged)

The north star is "install and just use it." Here is the *real* step count, tagged by who/where. We do
not hide steps.

| # | Step | phone-doable? |
|---|------|---------------|
| 1 | Install the plugin | ✅ phone |
| 2 | Fork the operator-template repo | ✅ phone (GitHub mobile) |
| 3 | Create a Cloud environment | ⚠️ **NEEDS-VERIFY** — Cloud env creation is a Claude settings-UI action; phone-doability unconfirmed. May be **needs-desktop once**. |
| 4 | Set Network access = Full | ⚠️ same UI; **needs-verify** |
| 5 | Add your own `GITHUB_PAT` to Network secrets | ⚠️ same UI; **needs-verify**. Must be YOUR token, never another operator's. |
| 6 | Attach your fork to the env | ⚠️ same UI; **needs-verify** |
| 7 | Open a session (node self-scaffolds) | ✅ phone |
| 8 | Request a claude.ai data export | ✅ phone (Settings → Privacy → Export) |
| 9 | Commit `conversations.json`, run the seeder | ✅ phone (if step 7's session is usable from phone) |
| 10 | Use the node | ✅ phone |

**Honest bottom line:** steps 1-2 and 7-10 are phone-native. Steps **3-6 (the one-time Cloud-env + secrets
setup) are a settings-UI action whose phone-doability is UNCONFIRMED** — they may need a desktop once, or
an operator (e.g. Chaz) may do them once on the new operator's behalf. After that, the node is used from
the phone. The final phone end-to-end confirmation requires a real Cloud session (not verifiable in a
container).

## Security notes

- **Network access: Full is capability, not containment** — an env-var proxy can be ignored by a
  process; it is not a trust boundary. (External prior art: zekker6/devsandbox proxy.md.)
- **Secrets go in Network secrets, never in the plugin or the repo.** The template and plugin ship no
  credential values. Your `GITHUB_PAT` is yours.
- **The template repo ships no operator data** — bring your own history via a data export.
