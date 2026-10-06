# cogos-harness

The first versioned slice of a CogOS node's Claude Code membrane: the
session-lifecycle hooks, ambient kernel-vitals proprioception, a threads
registry for open-ended waits, and the `cogos-kernel` MCP server, packaged
as one installable plugin instead of hand-copied files under
`~/.claude/hooks/`.

This is a deliberately small first cut. It ships the portion of the
membrane that is (a) already generic across any CogOS node and (b) safe to
publish. See "What's excluded" below for the rest.

## What ships

**MCP server** (`.mcp.json`): `cogos-kernel`, an HTTP connection to a
locally-running CogOS kernel at `http://127.0.0.1:6931/mcp`. If no kernel
is running, the server simply has nothing to answer — Claude Code surfaces
that as an unavailable tool, not an error. The port is not currently
env-parameterized in the MCP config (Claude Code plugin `.mcp.json` doesn't
support arbitrary `${ENV_VAR}` substitution in server URLs, only the
`CLAUDE_PLUGIN_*`/`CLAUDE_PROJECT_DIR` trio); if your kernel runs on a
different port, edit the installed plugin's `.mcp.json` or register a
second `cogos-kernel` entry in your own `.mcp.json` — see "Known
limitation" below.

**Hooks** (`hooks/hooks.json`), all Python 3, all fail-open (never block a
turn, never raise past their own `main()`, degrade to silence on any
missing dependency):

| Hook | Event | What it does |
|---|---|---|
| `user-scope-session-start.py` | `SessionStart` (`*`) | Locates a cog workspace (via `COGOS_WORKSPACE` or `~/workspaces/cog`) and delegates to its `session-start.d` presence handler, if one exists. **Presence is registry-gated, not workspace-gated**: after delegation, this hook asks the kernel whether the session is actually in the session registry (`GET /v1/sessions/presence`) and, only if it is absent, registers the seat itself via `POST /v1/sessions/register` (session_id from the hook's stdin, workspace=cwd, role=`$COGOS_SEAT_ROLE` or `claude-code`, hostname, `extras.source=plugin-startup`) — the exact REST counterpart of `cog_register_session`. The registry, not handler-existence, is the gate: the workspace's `51-presence-started.py` emits a `presence.started` *bus* event and never writes the session registry, so a handler running is no evidence the seat was registered. Checking presence also means an already-registered seat is left alone, so a `resume`/`compact` `SessionStart` never overwrites a durable role (`register` is a full-row replace). No-ops entirely when the kernel is unreachable. |
| `seat-identity-heal.py` | `SessionStart` (`*`) | Re-asserts a durable session role against the kernel if `~/.cog/status/seat-identity.json` exists. No-ops with no identity file. |
| `compaction-handoff.py` | `SessionStart` (`compact`) | Re-injects operator-verbatim messages, in-flight background task state, and uncommitted-repo status after compaction — a bypass path around what the compaction summary flattens. |
| `user-scope-session-end.py` | `SessionEnd` (`*`) | Presence-ended counterpart to the session-start hook: delegates to the workspace's `session-end.d` handler, then — gated on the same registry lookup — `POST`s `/v1/sessions/{id}/end` for the session if it is still listed as live, the REST counterpart of `cog_end_session`. Mirroring the start side matters: `51-presence-ended.py` also only emits a bus event, so handler-gating here would strand every seat the start-side fallback registered. |
| `user-scope-proprioception.py` | `UserPromptSubmit` (`*`) | Emits a one-line `<cogos_proprioception>` block outside a cog workspace: branch, local wall-clock + day-phase, session-elapsed time, last-turn model, context-window usage, and (via the detached probe below) kernel health. When that cached vitals read already shows the kernel reachable, also `POST`s a session heartbeat (`/v1/sessions/{id}/heartbeat`, the REST counterpart of `cog_heartbeat_session`) — no extra network probe, at most once per turn, silently skipped whenever the kernel is absent, and on a tight 0.25s timeout because this is the one hook on the per-turn critical path. |
| `kernel-vitals-probe.py` | fired detached by the proprioception hook | Off-the-critical-path collector: kernel `/health`, error/anomaly counts from the kernel log, process uptime, release/PR status via `gh`. Writes a cache the proprioception hook reads; never called synchronously. |
| `threads-warn.py` | `UserPromptSubmit` (`*`) | Reads the threads registry (below) and, for each open thread that is actually past its `expected_by`, runs its resolution predicate under a hard per-predicate timeout plus an overall wall-clock budget (a not-yet-due thread's predicate is never invoked — its exit code can't affect the outcome yet, see `skip_predicate_if_not_due` in `lib/threads_core.py`). Emits a compact warning block for orphaned threads (unresolved and past `expected_by`) **and**, distinctly, whenever the wall-clock budget ran out before every open thread could be checked (never silent about that — an unconditional "N not checked this turn" note, with scan order rotated across turns so the same thread doesn't starve the budget forever). Silent only when nothing was found and nothing was skipped: an empty registry, an all-healthy registry, a missing state file, or a corrupt one — see `skills/threads/SKILL.md` and the module docstring for the full silence contract. |
| `threads-gate-pr.py` | `PreToolUse` (`Bash`) | **Scaffolded, disabled by default.** Would deny `gh pr create` when zero open threads are registered, forcing the resolution predicate to be written at the moment a wait begins rather than reconstructed from memory later. Runs on every Bash call but its own first action is an allow-and-return unless `enforce_pr_create_thread: true` is set in `~/.cog/status/threads-config.json` — nothing in this plugin sets that key. Fails open on any internal error, missing/corrupt registry, or unreadable config. |

All three session-lifecycle hooks resolve the kernel the same way:
`COGOS_KERNEL_URL` wins when set, else
`http://127.0.0.1:${COGOS_KERNEL_PORT:-6931}` — the same precedence as
`seat-identity-heal.py` and `kernel-vitals-probe.py`, so a non-default
kernel location only needs to be set once. The threads hooks are
kernel-independent (no network calls at all) and don't participate in that
resolution.

**Threads registry** (`lib/threads_core.py`, `bin/threads`,
`hooks/threads-warn.py`): a small CLI (`threads add|list|check|close`) plus
its warn-tier hook, for open-ended waits that would otherwise die silently
at compaction or session end — "I'll hold until the verdict lands," with
nothing wired to say when. A thread is a **resolution predicate**, not a
completion signal: a bounded shell command whose exit code answers "is the
condition true", checkable by anyone at any time with no memory of who
registered it. State lives at `~/.cog/status/threads.json` (override with
`COGOS_THREADS_STATE`), atomic writes, corrupt files reported never
silently reset. Only `id / what / why / predicate / opened_at /
expected_by / owner / closed_at / closed_reason` are ever hand-written;
`resolved / orphaned / overdue / age` are derived by running the predicate,
never stored as source of truth. See `skills/threads/SKILL.md` for the full
procedure, including the worked good-predicate-vs-bad-predicate example.

**Skills** (`skills/`): `btw` (fork the session into a parenthetical aside
via `cog_fork_session`), `consolidate` (reflective session-arc capture:
settled / heating-up / at-risk distinctions), `handoff` (transactional
session handoff over the kernel's event bus), `threads` (register and check
open-ended waits as resolution predicates — when to register, how to write
a good predicate, how to close). `pull-context-dispatch` is **not**
duplicated here — it already ships in this marketplace's `cogos-workflow`
package; install that alongside this plugin if you want it too.

## Mods (0.4.0): one function-hooks module, five mods

Alongside the shell hooks above (which all keep running unchanged: `hooks.json`
carries both `"hooks"` and `"modules"`), 0.4.0 adds Claude Code **mods**
(function hooks, Claude Code >= 2.1.287). In 0.4.0 they are **stubs**: they
load, register, and do nothing.

| Mod | File | Hooks (stub) | Brief |
|---|---|---|---|
| cog-redact | `mods/cog-redact.mjs` | `tool.call`, `prompt.submit` | secrets out before the model reads them |
| cog-guard | `mods/cog-guard.mjs` | `tool.call` | one permission mod for every gate |
| cog-context | `mods/cog-context.mjs` | `prompt.context` | one in-process context injector |
| cog-status | `mods/cog-status.mjs` | `session.measure` | status band + the vitals-cache owner |
| cog-receipt | `mods/cog-receipt.mjs` | `tool.call`, `turn.complete` | turn receipts to the stage and the bus |

**Off by default.** Each mod reads its mode from `~/.cog/conf/harness-mods.json`
(override path: `COGOS_HARNESS_MODS_CONF`):

```json
{ "version": 1, "mods": { "redact": "off", "guard": "off", "context": "off", "status": "off", "receipt": "off" } }
```

`off` = the hook is `next(e)` after one cached config read; `shadow` = runs and
logs (`hook: "mod/<name>/<event>"`, `shadow: true`) to
`~/.cog/run/hook-health.jsonl` but changes nothing; `on` = in effect. A missing
or malformed file means every mod is off.

**Layout.**

```
hooks/index.mjs      the ONE hooks module: calls each mod's register(on, options) in order
mods/cog-*.mjs       one file per mod (each owns only its file + tests/<name>.test.ts + its types block)
lib/cfg.mjs          shared config loader (off/shadow/on; missing file = all off)
lib/order.mjs        the typed order: redact → guard → context → status → receipt
lib/cache.mjs        shared vitals cache read/write with age (writer: cog-status)
lib/ledger.mjs       hook-health ledger rows (append via process.run, never rewrite)
lib/kernel.mjs       kernel fetch with X-Cogos-Grant
lib/stub.mjs         the skeleton's no-op body
types/index.d.ts     PluginState contract, one block per mod
types/vendor/        Claude Code's own mod types (2.1.292), for tsc
tests/*.test.ts      one file per mod + order.test.ts + lib.test.ts
```

**Engine rules the layout follows** (each measured with `claude plugin
validate` on 2.1.292):

- One plugin lists one hooks module, so `hooks/index.mjs` composes the mods.
- One plugin may not register the same event twice without a matcher, so
  every mod hook carries one (`{}` matches everything).
- `$` is followed only into functions declared in the same file, never across
  an import. So `lib/` functions take an `io` adapter, and each mod builds it
  from `$` with its own same-file `ioOf($)`. The base keys are identical in all five.
- Hooks nest in registration order, first outermost. `redact` therefore sees a
  tool call before `guard` (plan §3.2), and `tests/order.test.ts` checks this
  on the real chain.

### Build and test: one command for every mod agent

From the repo root:

```bash
claude plugin validate plugins/cogos-harness \
  && claude plugin test plugins/cogos-harness \
  && npx -y -p typescript@5 tsc -p plugins/cogos-harness/tsconfig.json --noEmit
```

All three must pass before a mod PR is opened. A mod PR touches only
`mods/<name>.mjs`, `tests/<name>.test.ts` and its own block in
`types/index.d.ts`. `hooks/index.mjs`, `lib/cfg.mjs`, `lib/order.mjs` and the
`ioOf` base keys are shared; change them through a separate one-line PR. The
exceptions: cog-redact owns the order block and `lib/order.mjs`, and
cog-status owns `lib/cache.mjs`.

## What's excluded, and why

- **`dispatch-model-probe.py`** — audited as a "by-reference companion" to
  the proprioception hook, but its only call site in that hook has been
  commented out since before this plugin existed (a dead code path, kept
  only for easy revival). It also reads the local Claude Code OAuth
  credentials file directly and makes live calls to
  `api.anthropic.com` with them — appropriate for a single operator's own
  incident-response tooling, not for default behavior in a plugin anyone
  installs. Excluded outright rather than ported.
- **Plan-usage tracking** (the `use S24%·W18%` style segment some
  proprioception hooks carry) — depends on a statusline side-channel cache
  that isn't part of this plugin and isn't itself portable membrane;
  dropped rather than partially ported. The context-window (`ctx N%`)
  segment, which is self-contained, is kept.
- **Book/reading-surface sync** — the source `kernel-vitals-probe.py` this
  was cut from also read a specific private repo's comment ledger to
  surface an unrelated personal writing project's review queue. That
  entire code path (repo constant, thread-state diffing, the `book ✎N`
  segment) is removed from the shipped version, not just disabled.
- **Everything else the membrane audit classified as machine-literal,
  project-specific, or operator-personal** — `hermes-ops`-style
  machine-named triggers, family/book-project references, hardcoded
  `/Volumes/...` paths, personal usernames in example payloads — is out of
  scope for this plugin by construction; none of it was in the source
  files this plugin was built from.

## What stays outside this plugin (user scope, not plugin scope)

Deliberately not shipped, and not something a future version of this
plugin should absorb without a separate decision:

- **`CLAUDE.md` principles** — how you want an agent to operate is
  identity, not mechanism. A plugin can offer hooks and tools; it
  shouldn't write your operating philosophy for you.
- **The memory corpus** — durable knowledge, feedback files, and any
  cogdoc/memory tree are per-operator content, not plugin payload.
- **`settings.json` risk-posture flags** — `defaultMode`, permission
  allow-lists, `skipDangerousModePermissionPrompt` and siblings are
  operator risk decisions. This plugin's hooks are all read-only or
  fail-open by design and don't need any of these flags to work; it never
  writes them.
- **The default-agent / model-tiering setting** — a per-operator choice
  about which model handles what, orthogonal to the membrane.
- **Node bootstrap** (installing the `cogos` binary itself, scaffolding a
  workspace, starting the kernel) — this plugin *configures* a client
  against an already-running kernel; it does not install or start one. See
  the kernel repo's own install docs for that step.

## Requirements

- `python3` on `PATH`
- Optional, degrades cleanly if absent: a running CogOS kernel at
  `127.0.0.1:6931`, a `~/workspaces/cog` (or `$COGOS_WORKSPACE`) workspace,
  `gh` authenticated against the kernel repo, `launchctl` (macOS) if the
  kernel runs as a launchd service

## Path/env conventions

- `COGOS_WORKSPACE` — the active CogOS workspace; falls back to
  `~/workspaces/cog`
- `MYRGIC_REPOS_ROOT` — local checkouts of myrgic-org repos; falls back to
  `~/workspaces/myrgic`
- `COGOS_KERNEL_PORT` — kernel HTTP port on `127.0.0.1`, read by the vitals
  probe, `seat-identity-heal.py`, the three session-lifecycle hooks
  (`user-scope-session-start.py`, `user-scope-session-end.py`,
  `user-scope-proprioception.py`'s heartbeat), and the bundled `.mcp.json`
  (via `${COGOS_KERNEL_PORT:-6931}`); falls back to `6931`
- `COGOS_KERNEL_URL` — full kernel base URL, for a kernel that isn't on
  `127.0.0.1`; takes precedence over `COGOS_KERNEL_PORT` everywhere above
  except the bundled `.mcp.json` (which only resolves `COGOS_KERNEL_PORT`,
  so a non-default host still needs a hand-edited or project-level
  `.mcp.json` override); falls back to `http://127.0.0.1:6931`
- `COGOS_KERNEL_REPO` — `owner/repo` for the kernel release/PR checks in
  the vitals probe; falls back to `myrgic/cogos`
- `COGOS_SEAT_ROLE` — the `role` field the session-start fallback
  registers with when it fires (no cog workspace, or a workspace with no
  presence handler); falls back to `claude-code`
- `COGOS_THREADS_STATE` — path to the threads registry JSON file, read/
  written by `bin/threads`, `hooks/threads-warn.py`, and
  `hooks/threads-gate-pr.py`; falls back to `~/.cog/status/threads.json`.
  Two tiny sidecar files live next to it, named from it (not independently
  configurable): `.threads.json.lock`, an flock-guarded advisory lock
  `locked_state()` holds across a CLI read-modify-write so concurrent
  `threads add`/`close` invocations serialize instead of racing; and
  `.threads.json.rotation`, a persisted counter `threads-warn.py` uses to
  rotate its scan order across turns (see F1/`_rotation_offset()` above).
  Both are best-effort scratch state — safe to delete by hand, and every
  read failure on either one degrades to a default (block briefly and
  retry for the lock; offset 0, today's un-rotated order, for the
  counter) rather than an error.
- `COGOS_THREADS_CONFIG` — path to the threads config file (currently just
  the `enforce_pr_create_thread` key); falls back to
  `~/.cog/status/threads-config.json`
- `COGOS_THREADS_PREDICATE_TIMEOUT` — per-predicate hard timeout in seconds
  for `threads-warn.py`; falls back to `3`
- `COGOS_THREADS_TOTAL_BUDGET` — overall wall-clock budget in seconds for
  `threads-warn.py`'s pass over all open threads in a single turn (once
  spent, remaining threads are skipped for that turn rather than pushing
  the hook past its latency budget); falls back to `4`

## Testing

`tests/test_threads.py` — stdlib `unittest`, no third-party dependencies:
`python3 tests/test_threads.py -v` (82 tests). Covers the CLI round trip,
the shared library's state/predicate/derive primitives, the
disabled-by-default enforcement gate, and — the load-bearing set — the warn
hook's silence contract checked byte-exact against real stdout for every
failure mode the build's hard gate calls out: missing state file, corrupt
state file, empty state file, a predicate that times out, a predicate that
errors, an unresolved-but-not-yet-overdue thread, and a closed (formerly
orphaned) thread. Every fixture runs against a per-test tempdir via
`COGOS_THREADS_STATE` (and, for the gate, `COGOS_THREADS_CONFIG`); nothing
here touches a real `~/.cog/status/threads.json` or
`~/.cog/status/threads-config.json`.

Also covers the fixes from a 2026-08-07 independent review: the
budget-exhaustion notice and scan-order rotation (deterministic,
clock-driven reproduction of the exact starvation scenario — budget
consumed by non-orphaned predicates ahead of a genuine orphan in registry
order), the `gh pr create` gate's segment-tokenizing command parser (allows
the phrase inside a quoted `git commit -m`/`grep` argument, still denies a
real invocation, documents variable-substitution evasion as out of scope),
an unparseable `opened_at` paired with a duration `expected_by`, enforcement
of `SCHEMA_VERSION` on load, and killing a predicate's whole process group
(not just its immediate `/bin/sh` child) on timeout.

## Dogfood plan

Not part of this PR: tapping `myrgic/plugins` and installing
`cogos-harness@plugins` in a real seat, and confirming the installed copy
serves the hooks in place of the hand-wired `~/.claude/hooks/` files it
was cut from. That's the natural next step once this lands, and is
tracked as follow-up work rather than bundled into this change.

For the threads registry specifically, the dogfood step is narrower and
concrete: the next time a session states an open-ended wait ("I'll hold
until the verdict lands"), register it with `threads add` instead of
carrying the intention in conversation context, and confirm
`threads-warn.py` actually surfaces it if it goes orphaned — the 2026-08-07
incident this shipped from is the acceptance test. The enforcement tier
(`threads-gate-pr.py`) stays off; arming it (`enforce_pr_create_thread:
true` in `~/.cog/status/threads-config.json`) is a separate, later,
operator-only decision once the warn tier has some real mileage on it.
