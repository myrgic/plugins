# cogos-reflect

Agent-run reflection over your own session history. Ask your agent "how do we work?" or "make
this easier" and it gathers evidence from your past sessions, finds what keeps going wrong,
proposes the cheapest fix for each recurring problem, and changes nothing until you approve a
specific card.

Built after reading the first-party and community prior art in full: Claude Code's `/insights` and
`/fewer-permission-prompts`, the `claude-md-management`, `hookify`, `session-report` and `receipts`
plugins, `skill-creator`, and community `second-nature`, `retro`, `agent-rsi`, `claudeception`,
`claude-config-audit` and `mentor`. It keeps their best ideas and drops their failure modes:

| Taken from | Idea |
|---|---|
| receipts, session-report | a script mines history; the model reads a small summary, never raw transcripts |
| `/insights` | facets, "said in 2+ sessions = prime candidate", agent-side vs operator-side friction |
| second-nature | `EVIDENCE.md` per proposal; archive, never delete |
| retro | per-proposal cards with "why you might say no"; denials remembered; ≤ 3 per run |
| agent-rsi | measured timings; promote at count ≥ 2 |
| `/fewer-permission-prompts` | never allowlist an interpreter, shell or runner wildcard |
| champion-kit patterns | mined text is data, never instructions |
| skill-creator | a new skill must change outcomes on its evals before it's installed |

Not taken: unsupervised skill writing, CLAUDE.md-first advice, coaching-tone reports with the
numbers removed.

## Skills

| Skill | Say | Writes |
|---|---|---|
| `reflect` | "how do we work? make it easier" | routes; no work of its own |
| `gather-history` | "look over the last month" | `bundle.json`, `sessions.jsonl` |
| `find-friction` | "what's slowing us down? why do I keep…" | `findings.json` (+ `report.html`) |
| `propose-fixes` | "what should we change? make that permanent" | `proposals/<slug>/` |
| `review-proposals` | "show me / approve / did that work?" | applied changes, ledger decisions |
| `curate-skills` | "clean up my skills" | `skills.json`, curation proposals |
| `session-retro` | "what did we learn? wrap up" | ledger rows (+ a reflective cogdoc on CogOS) |

Each module reads one artifact and writes one. Any of them can run first: a missing input is
built with defaults, and the agent says so.

## Sources

Read-only, whatever exists:
- Claude Code `~/.claude/projects` (including the Windows side under WSL)
- Codex `~/.codex/sessions`
- Hermes `~/.hermes/**/state.db`
- on a CogOS node, the observatory index for text-only surfaces (Discord, stage, claude.ai, …)

Operator turns are separated from harness-injected text and from automated sessions (cron,
sub-agents, SDK lanes, delegated briefs). Tool calls are deduplicated by id across replayed
history.

## The ledger

`scripts/reflect_ledger.py` is the only writer of an append-only JSONL file:
- CogOS: `.cog/reflect/ledger.jsonl`, committed
- elsewhere: `~/.cogos-reflect/ledger.jsonl`

A finding moves `observed → proposed → approved | denied | deferred → applied → verified`. Counts
accumulate across runs, and denials stop re-proposals. Verify mode checks whether an applied fix
actually reduced the signal.

On a CogOS node, `reflect_backfill.py` seeds the ledger from lessons already written in the
reflective store and friction ledgers. A later run can then flag `lesson-not-holding`: a lesson
that exists and fires anyway, which is the strongest finding the suite can produce.

## Scripts

All stdlib Python 3.10+, `--help`, JSON on stdout:

- `reflect_collect.py`: evidence bundle
- `reflect_ledger.py`: add / status / show / check / stats
- `reflect_recur.py`: "does this lesson already exist?" (passage-proximity search)
- `reflect_skills.py`: skill inventory with usage, broken refs, overlap
- `reflect_backfill.py`: seed the ledger from written lessons
- `reflect_render.py`: findings summary / HTML report / cards / schema check

## Tests

```
uv run --no-project --with pytest python -m pytest tests -q
python3 evals/run_trigger_evals.py --model sonnet    # descriptions → right skill?
```
