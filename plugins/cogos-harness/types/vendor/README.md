# Vendored Claude Code mod types

`claude-code/index.d.ts` and `claude-code-tools/index.d.ts` are the engine's own
declarations, **written by Claude Code 2.1.292** (first line of each file).
They exist so `tsc -p plugins/cogos-harness/tsconfig.json --noEmit` works in a
fresh clone and in CI. Do not edit them.

Refresh after a Claude Code upgrade: load the plugin once from this folder
(e.g. `claude -p ok --plugin-dir plugins/cogos-harness --max-turns 1`); the
engine writes `.claude-plugin/types/` (git-ignored by its own `.gitignore`).
Copy `claude-code/` and `claude-code-tools/` from there to here, and update the
version in this file.
