---
name: threads
description: Register and check open-ended waits — "I'll hold until the verdict lands" — as a resolution predicate in the threads registry (~/.cog/status/threads.json), so the intention survives compaction and session end instead of dying silently in conversation context. Use whenever you state that you're waiting on something external (a CI verdict, a review decision, an operator reply, an async job) and the wait could plausibly outlive this conversation turn or this session. Also covers how to write a good predicate vs a bad one, and how to close a thread once it's actually been looked at.
version: 1.0.0
author: myrgic
tags: [cogos, session-management, continuity, threads, predicates]
---

# Skill: threads

A registered thread answers one question truthfully, at any later point, with
no memory of who registered it: **is the thing being waited on actually
true yet?** That's the whole registry. Everything else in this skill is in
service of keeping that question well-posed.

<invocation>
MUST register a thread the moment you state an open-ended wait ("I'll hold
until X", "let me know when Y lands", "waiting on the verdict") — not after
the fact, not "if I remember." The registration IS the mechanism that
survives compaction; saying it in conversation text is not.
MUST write the predicate so it answers "is the condition true", never "did
the process finish". See "A good predicate vs a bad one" below — this is
the single most important distinction in this skill.
MUST NOT invent or hand-set resolved/orphaned/overdue/age when reading a
thread back. Those are derived by running the predicate NOW
(`threads check`) — never trust a stale mental model of what a thread's
state was, and never write those fields into the registry yourself (the
CLI doesn't expose flags for them; there is no back door).
MUST close a thread explicitly (`threads close <id>`) once you've actually
looked at its resolution and acted on it. A resolved predicate does not
auto-close itself — resolving and closing are different declared moments;
see "Closing a thread" below.
</invocation>

## Why this exists

The seat states an intention to wait on something and wires nothing to tell
it when. The intention lives only in conversation context, so it dies at
compaction or session end and the thread is silently dropped — the operator
catches it, not the system. This happened on 2026-08-07: a session watched a
CI *run* and reported "done" when the process exited, but the thing actually
being waited on was the *review verdict*, which had not yet propagated. That
gap between "the run finished" and "the thing I actually care about is now
true" is exactly what a resolution predicate closes.

## A good predicate vs a bad one

The worked example, verbatim from the 2026-08-07 incident:

**Bad** — watches the process:
```
gh run view <run-id> --json status --jq '.status == "completed"'
```
This answers "did the run finish", not "is the verdict in". A run can
complete long before the review decision it triggers has actually landed
(or before anyone has looked at it). Treating "completed" as "resolved"
is the exact failure this registry exists to catch — the CI case is proof
it isn't hypothetical.

**Good** — checks the actual condition at HEAD:
```
[ "$(gh pr view <pr-number> --json reviewDecision --jq .reviewDecision)" = "APPROVED" ]
```
This is a **bounded shell command whose exit code answers the real
question**. Note the explicit `[ ... = ... ]`, not
`--jq '.reviewDecision == "APPROVED"'` piped straight through: `gh --jq`
prints jq's boolean result (`true`/`false`) but still **exits 0 either
way** — `false` is a well-formed jq value, not a jq error — so a predicate
written as `gh pr view <n> --json reviewDecision --jq '.reviewDecision ==
"APPROVED"'` reports **resolved even when the answer is `false`**. This
was caught empirically while building this skill, not spotted by
inspection — always test a predicate by hand
(`bash -c '<predicate>'; echo $?`) against a real unresolved case before
registering it, not just the resolved case. Wrapping in `test`/`[ ... ]`
against the raw value is what makes exit-0-means-resolved unambiguous
regardless of how the inner tool encodes truth.

The general test to apply before registering any predicate: **if this
command exits 0 right now, is the thing I actually care about true right
now?** Not "has some proxy process terminated." Not "does a log line exist
saying it's done." The condition itself, checked directly, at the current
moment — checkable by anyone, with no memory of who registered it.

Other examples:

| Waiting on | Bad predicate (process/signal) | Good predicate (condition) |
|---|---|---|
| A deploy landing | `kubectl get job deploy-x -o jsonpath='{.status.succeeded}'` | `curl -sf https://service/health \| jq -e '.version == "1.4.0"'` |
| An operator reply | (nothing — "I'll wait for them to say something") | `gh api repos/OWNER/REPO/issues/N/comments --jq 'any(.[]; .user.login == "OPERATOR" and (.created_at > "TS"))'` |
| A background job finishing correctly | `test -f /path/job.done` | `test -f /path/job.done && jq -e '.status == "ok"' /path/job-result.json` |
| A file being written and stable | `test -f out.csv` | `test -f out.csv && [ "$(stat -f %m out.csv)" -lt $(($(date +%s) - 30)) ]` (exists AND hasn't been touched in 30s — catches a still-being-written file) |

The pattern in every "bad" column: it checks for *evidence a process ran*,
not the *outcome that process was supposed to produce*. The pattern in
every "good" column: it re-derives the actual answer from source, fresh,
every time it's run.

## Registering a thread

```
threads add \
  --what "PR 118 review verdict" \
  --why  "blocking merge; agreed not to proceed without an approval" \
  --predicate '[ "$(gh pr view 118 --json reviewDecision --jq .reviewDecision)" = "APPROVED" ]' \
  --expected-by 1d \
  --id pr118-verdict
```

- `--what` / `--why` — one line each. `why` is what makes an orphaned thread
  worth someone's attention later instead of noise to be auto-closed; write
  it for a reader with zero context on this conversation.
- `--predicate` — the bounded shell command from the section above. Test it
  by hand first (`bash -c '<predicate>'; echo $?`) before registering —
  a predicate that's wrong at registration time is worse than no thread at
  all, because it launders false confidence.
- `--expected-by` — a duration (`2h`, `1d`, `45m`, `30s`, `1w`) relative to
  registration time, or an absolute ISO8601 timestamp. This is what turns
  "still waiting" into "orphaned" — pick it honestly. If you don't have a
  real estimate, use a generous duration rather than omitting it; a thread
  with no meaningful deadline can never be flagged overdue.
- `--owner` — defaults to `$COGOS_SESSION_ID` (or `unknown`). Set it
  explicitly if you're registering on behalf of a different seat.
- `--id` — defaults to an auto-slug of `--what` plus a timestamp suffix.
  Give it an explicit, memorable id when you expect to reference it again
  (`threads check pr118-verdict`, `threads close pr118-verdict`).

## Checking threads

```
threads list                 # open threads, one line each
threads list --all           # include closed
threads check                # run every open thread's predicate NOW, print derived status
threads check pr118-verdict  # just this one
threads check --verbose      # + what/why/predicate/owner per thread
```

`threads check` always re-runs the predicate — it never reads a cached
verdict. Exit code is `1` if any checked thread came back orphaned, `0`
otherwise, so it composes in scripts.

The `hooks/threads-warn.py` UserPromptSubmit hook does a bounded version of
this check every turn — bounded in two ways worth knowing about:

- It only actually *runs* a thread's predicate once that thread is past its
  `expected_by`. A thread with a distant deadline costs nothing, turn after
  turn, until it's actually due — its predicate's exit code can't change
  the orphaned/not-orphaned answer before then, so the hook skips invoking
  it (no subprocess, no possible network call) rather than paying full
  predicate latency every single turn for the thread's entire lifetime.
- The whole pass over all open threads is bounded by an overall wall-clock
  budget (`COGOS_THREADS_TOTAL_BUDGET`). If the budget runs out before
  every open thread could be checked, the hook says so explicitly (`N open
  thread(s) not checked this turn — over budget`) rather than staying
  silent — silence specifically means "checked and found nothing," never
  "ran out of time before looking." Scan order also rotates across turns,
  so a thread that gets skipped for budget this turn is checked earlier on
  a later one instead of starving forever.

It speaks only when a thread comes back orphaned (unresolved AND past
`expected_by`) or when the budget note above applies. A quiet turn means
every open thread that was actually checked came back fine — not that the
registry was never looked at.

## Closing a thread

```
threads close pr118-verdict --reason "approved, merged in #118"
```

Closing is a separate, explicit, declared act from the predicate
resolving. A resolved-but-unclosed thread still shows up in `threads list`
and, once it's past `expected_by`, still gets checked by the warn hook
(though a resolved thread is never reported as orphaned, regardless of
`expected_by`) — this is intentional:
resolution is a fact about the world, closing is an acknowledgment that a
person or agent actually looked at that fact and did something about it.
Don't script "auto-close on resolve" — that collapses the two moments back
into the completion-signal failure mode this registry exists to avoid.

## What NOT to do

- **Don't register a completion signal disguised as a predicate.** If your
  predicate's true/false hinges on whether a process ran rather than what
  it produced, rewrite it — see "A good predicate vs a bad one."
- **Don't skip registration because "I'll remember."** The entire point is
  that conversation-context intentions die at compaction; if it's worth
  saying "I'll wait for X," it's worth ten seconds to `threads add`.
- **Don't leave `--expected-by` vague or absent.** An unbounded wait can
  never become "orphaned" — it just silently accumulates as a thread
  nobody is ever told to look at again.
- **Don't hand-edit `~/.cog/status/threads.json`** to set `resolved` or
  similar — there's no such field; the schema only has declared fields
  (id/what/why/predicate/opened_at/expected_by/owner/closed_at/closed_reason).
  Anything else is derived by `threads check`, always freshly.
- **Don't auto-close on resolve.** See "Closing a thread" above.

## Enforcement tier (off by default)

`hooks/threads-gate-pr.py` can require an open thread to exist before `gh pr
create` is allowed to run — forcing the predicate to be registered at the
moment the wait begins, not reconstructed from memory afterward. It ships
disabled: set `"enforce_pr_create_thread": true` in
`~/.cog/status/threads-config.json` to arm it. Not enabled by default, and
this skill does not instruct you to enable it — that's an operator decision
about their own membrane.
