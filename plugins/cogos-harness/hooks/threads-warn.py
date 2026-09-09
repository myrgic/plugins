#!/usr/bin/env python3
"""threads-warn.py — UserPromptSubmit warn-tier for the threads registry.

Fires on every turn of every session (see the plugin-wide hard gate in
README.md). That means this file's failure modes are load-bearing, not
incidental:

  - Any exception, missing file, malformed state, or unavailable dependency
    -> emit NOTHING and exit 0. Never raise past main(). Never print a
    traceback -- the operator's daily driver must never see one from here.
  - Bounded work only: predicates run with a hard per-predicate timeout
    (THREADS_PREDICATE_TIMEOUT) AND the whole hook is bounded by an overall
    wall-clock budget (THREADS_TOTAL_BUDGET) -- once the budget is spent,
    remaining threads are skipped for this turn rather than pushing the
    hook past its latency budget. No network calls originate from this
    file itself (see derive_status()'s docstring in lib/threads_core.py
    for why owner-liveness checking is deferred rather than added here).
  - Skipped-for-budget is NEVER silent (F1 in the 2026-08-07 independent
    review). A registry order that happens to put a genuine orphan behind
    enough slow-but-non-orphaned predicates to exhaust the budget used to
    produce fully empty output, indistinguishable from "nothing is wrong"
    -- reproduced 5/5 in review, deterministically, because scan order was
    plain registry order every single turn, so the same thread always
    starved. Two independent fixes, both applied: (1) the budget note now
    renders even when zero orphans were found among the threads actually
    checked -- see `_render_block()`; (2) scan order is rotated across
    invocations (`_rotation_offset()`) so a thread that starves this turn
    is scanned earlier on a later one, instead of starving forever.
  - SILENT only when nothing was found AND nothing was skipped. An open
    registry that is empty, all-healthy, unreadable, or fully checked this
    turn with no orphans produces silence -- absence of a block is not
    evidence everything is fine, only that nothing here demanded attention
    *and* the hook actually got to look at everything.

Corrupt/missing state file: silent, on purpose. `threads list`/`threads
check`, run by a human or an agent that actually wants to know, report
corruption loudly (see bin/threads's _load_or_die). This hook is not that
surface -- surfacing a corrupt-file warning on every single turn is exactly
the wallpaper failure mode the gate warns about, and the CLI already
reports it whenever someone actually looks.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

THREADS_PREDICATE_TIMEOUT = float(os.environ.get("COGOS_THREADS_PREDICATE_TIMEOUT", "3"))
THREADS_TOTAL_BUDGET = float(os.environ.get("COGOS_THREADS_TOTAL_BUDGET", "4"))
MAX_LINES = 5  # cap how many orphan lines render; rest collapse to a count


def _read_event_name() -> str:
    try:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw).get("hook_event_name") or "UserPromptSubmit"
    except Exception:
        pass
    return "UserPromptSubmit"


def _emit(evt: str, block: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": evt,
            "additionalContext": block,
        }
    }))


def _line_for(core, status) -> str:
    t = status.thread
    reasons = ",".join(status.orphan_reasons) or "?"
    # NEW-2 (2026-08-07 independent review, delta pass): when opened_at
    # didn't parse, `age`/`expected_by_ts` are synthesized stand-ins, not
    # real measurements -- rendering them verbatim next to "overdue"
    # reads as self-contradicting evidence ("age 0s, expected_by
    # tomorrow" beside "orphaned, right now"). Render "?" instead
    # whenever derive_status flagged the value as unknown; see
    # ThreadStatus's docstring in lib/threads_core.py.
    age_str = "?" if status.age_unknown else core.human_age(status.age)
    if status.expected_by_unknown:
        exp = "?"
    else:
        exp = core.iso(status.expected_by_ts) if status.expected_by_ts else "?"
    note = f" [{status.predicate_note}]" if status.predicate_note else ""
    what = (t.get("what") or "")[:70]
    return (
        f'⚠ {t.get("id")} ({reasons}): "{what}" — age {age_str}, '
        f'expected_by {exp}, owner={t.get("owner","?")}{note}'
    )


def _rotation_offset(n: int, path: Path) -> int:
    """Best-effort persisted rotation counter so scan order shifts across
    invocations of this hook (one per turn) rather than staying fixed at
    plain registry order forever. Without this, a thread that happens to
    sit late enough in registry order starves every single turn whenever
    the budget is consistently consumed by the threads ahead of it --
    reproduced 5/5 in the 2026-08-07 independent review (F1) with a fixed
    scan order and a deliberately-last genuine orphan.

    Lives in a tiny counter file next to the state file. ANY read/write
    failure here just falls back to offset 0 (today's un-rotated order,
    i.e. exactly the old behavior) -- rotation is a fairness improvement
    across turns, never a correctness dependency within a turn, so a
    broken or racing counter file must never be able to take this hook
    down. (Concurrent writers to this counter aren't lock-guarded the way
    the state file's read-modify-write is: worst case under a race is an
    uneven or repeated rotation, not a wrong orphaned/resolved verdict --
    the counter only ever influences which threads get CHECKED first
    within a budget, not the verdict for any thread that does get
    checked.)"""
    if n <= 1:
        return 0
    try:
        try:
            counter = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            counter = 0
        offset = counter % n
        path.write_text(str(counter + 1), encoding="utf-8")
        return offset
    except Exception:
        return 0


def _scan(
    open_threads: list[dict],
    core,
    budget: float,
    predicate_timeout: float,
    clock=time.monotonic,
    rotation_path: Path | None = None,
) -> tuple[list[str], int]:
    """Runs derive_status for as many of `open_threads` as fit in `budget`
    wall-clock seconds (per the module docstring's bounded-work contract),
    returning `(orphan_lines, skipped_for_budget)`.

    Scan order is rotated first (see `_rotation_offset`) so the fairness
    fix actually changes which threads get checked, not just how the
    result is reported.

    `clock` is injectable (defaults to `time.monotonic`) purely so tests
    can drive the budget-exhaustion path deterministically -- real
    predicate/OS timing is exactly the kind of thing that makes a test
    flaky by construction, and the scan logic itself has nothing to do
    with wall-clock time beyond calling this function twice per
    iteration."""
    rotation_path = rotation_path or core.STATE_PATH.with_name(f".{core.STATE_PATH.name}.rotation")
    offset = _rotation_offset(len(open_threads), rotation_path)
    if offset:
        open_threads = open_threads[offset:] + open_threads[:offset]

    deadline = clock() + budget
    orphan_lines: list[str] = []
    skipped_for_budget = 0

    for t in open_threads:
        if clock() >= deadline:
            skipped_for_budget += 1
            continue
        try:
            per_thread_timeout = min(predicate_timeout, max(0.1, deadline - clock()))
            # skip_predicate_if_not_due=True: this hook only ever emits for
            # orphaned (overdue-and-unresolved) threads, so for any thread
            # not yet past its expected_by the predicate's exit code cannot
            # change this hook's output -- skip running it (no subprocess,
            # no possible network call) rather than paying full predicate
            # latency/budget every turn for a deadline that hasn't arrived.
            status = core.derive_status(t, timeout=per_thread_timeout, skip_predicate_if_not_due=True)
        except Exception:
            # A single bad thread entry (or a predicate that errors in some
            # exotic way derive_status didn't already catch) must never
            # take down the check for every other thread.
            continue
        if status.orphaned:
            try:
                orphan_lines.append(_line_for(core, status))
            except Exception:
                continue

    return orphan_lines, skipped_for_budget


def _render_block(orphan_lines: list[str], skipped_for_budget: int) -> str | None:
    """Builds the additionalContext block from scan results, or None for
    full silence.

    F1 fix: silence requires BOTH no orphans found AND nothing skipped for
    budget. The pre-fix version returned early whenever `orphan_lines` was
    empty, full stop -- which conflated "checked everything, all healthy"
    with "budget ran out before finishing," and the second case can be
    hiding a genuine orphan in the unchecked remainder. Both cases now
    render, distinctly worded."""
    if not orphan_lines and not skipped_for_budget:
        return None

    if orphan_lines:
        shown = orphan_lines[:MAX_LINES]
        extra = len(orphan_lines) - len(shown)
        body = "\n".join(shown)
        if extra:
            body += f"\n… +{extra} more orphaned thread(s) (`threads list`, `threads check`)"
    else:
        body = "(no orphans found among the threads checked this turn)"

    if skipped_for_budget:
        body += f"\n({skipped_for_budget} open thread(s) not checked this turn — over budget)"

    return (
        f'<threads_warn count="{len(orphan_lines)}">\n{body}\n'
        f"(`threads check <id>` for detail, `threads close <id>` to close)\n"
        f"</threads_warn>"
    )


def main() -> None:
    evt = _read_event_name()
    try:
        import threads_core as core
    except Exception:
        return  # can't even import the library -> fully silent, per contract

    try:
        data = core.load_state()
    except Exception:
        # Missing, corrupt, or otherwise unreadable state: silent by design
        # on this hook -- see module docstring. `threads list` is where
        # corruption gets reported loudly.
        return

    try:
        open_threads = core.open_threads(data)
    except Exception:
        return
    if not open_threads:
        return

    try:
        orphan_lines, skipped_for_budget = _scan(
            open_threads, core, THREADS_TOTAL_BUDGET, THREADS_PREDICATE_TIMEOUT
        )
    except Exception:
        return

    block = _render_block(orphan_lines, skipped_for_budget)
    if block is None:
        return
    _emit(evt, block)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
