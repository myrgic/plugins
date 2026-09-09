#!/usr/bin/env python3
"""threads_core.py — shared primitives for the threads registry.

The registry exists to answer one question truthfully at any later point,
with no memory of who asked it to be true: **is the thing this thread is
waiting on actually resolved?** That's why a thread's identity is a
RESOLUTION PREDICATE (a bounded shell command whose exit code answers
"resolved?"), not a completion signal (did some process exit). The
distinguishing failure this fixes: watching a CI *run* answers "did it
finish", not "is the verdict in" — a run can finish long before the thing
being waited on (e.g. a review decision) has actually landed. See
skills/threads/SKILL.md for the worked example and the write-a-good-
predicate guidance.

Schema — DECLARED fields (the only ones a caller ever writes):
  id            str  — short, unique, human-chosen or auto-slugged
  what          str  — one line: what is being waited on
  why           str  — one line: why it matters
  predicate     str  — a bounded shell command; exit 0 == resolved
  opened_at     str  — ISO8601, stamped by `threads add`
  expected_by   str  — ISO8601 timestamp OR a duration ("2h","1d","45m","30s")
                        relative to opened_at
  owner         str  — session/seat id that registered the thread
  closed_at     str | None — set only by `threads close` (an explicit,
                        declared action — closing is not the same thing as
                        the predicate resolving; a resolved thread still
                        needs an operator/agent to look at it and close it)
  closed_reason str | None

Everything else — resolved, orphaned, overdue, age — is DERIVED: computed
fresh by RUNNING the predicate and comparing timestamps, never read back out
of a stored field. A registry that could self-report "all clear" from a
hand-written flag is exactly the failure this replaces (see the module
docstring's node_count:0-since-January reference in the build brief this
shipped from). derive_status() is the single place that computation happens;
nothing else in this module or its callers may set those fields by hand.

Storage: a single JSON file, {"version": 1, "threads": [...]}. Atomic
writes (write-to-tempfile + os.replace) so a hook reading mid-write never
sees a torn file. A corrupt or unreadable-but-present file is reported via
CorruptStateError, NEVER silently replaced with a fresh empty state — losing
someone's registered threads because the reader panicked and reset the file
would be worse than the bug this registry exists to catch.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = Path.home() / ".cog" / "status" / "threads.json"
STATE_PATH = Path(os.environ.get("COGOS_THREADS_STATE", str(DEFAULT_STATE_PATH)))

DEFAULT_CONFIG_PATH = Path.home() / ".cog" / "status" / "threads-config.json"
CONFIG_PATH = Path(os.environ.get("COGOS_THREADS_CONFIG", str(DEFAULT_CONFIG_PATH)))

SCHEMA_VERSION = 1
DECLARED_FIELDS = (
    "id", "what", "why", "predicate", "opened_at", "expected_by", "owner",
    "closed_at", "closed_reason",
)

DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class CorruptStateError(Exception):
    """Raised when the state file exists but does not parse as the expected
    shape. Callers MUST surface this, never swallow it into a fresh reset —
    see module docstring."""


class ThreadNotFoundError(Exception):
    pass


class LockTimeoutError(Exception):
    """Raised when locked_state() cannot acquire the state-file lock within
    its timeout. A concurrent writer is holding it too long (or died while
    holding it -- flock releases automatically on process exit, so a stuck
    lock past the timeout means genuine contention, not a stale lock)."""


# --------------------------------------------------------------------- time --

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(raw: str | None) -> datetime | None:
    """Best-effort ISO8601 -> tz-aware UTC datetime. None on any failure —
    absence of a parseable timestamp is a signal callers must handle, never
    an exception that takes the whole hook down with it."""
    if not raw or not isinstance(raw, str):
        return None
    t = raw.strip().replace("Z", "+00:00")
    for cand in (t, re.sub(r"\.\d+", "", t)):
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def parse_expected_by(raw: str | None, opened_at: datetime | None) -> datetime | None:
    """expected_by is either an absolute ISO8601 timestamp, or a duration
    ("2h", "45m", "1d", "30s", "1w") measured from opened_at. Returns None
    if it parses as neither — callers must treat that as "no deadline
    known", not as an error (a thread with an unparsable expected_by is
    still a valid open thread; it just never triggers the overdue path)."""
    if not raw:
        return None
    m = DURATION_RE.match(raw)
    if m and opened_at is not None:
        qty, unit = m.groups()
        return opened_at + timedelta(seconds=float(qty) * _DURATION_SECONDS[unit.lower()])
    return parse_ts(raw)


def human_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 0:
        return "0s"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h{mins % 60:02d}m"
    days = hrs // 24
    return f"{days}d{hrs % 24:02d}h"


# -------------------------------------------------------------------- state --

def _default_state() -> dict:
    return {"version": SCHEMA_VERSION, "threads": []}


def load_state(path: Path = STATE_PATH) -> dict:
    """Missing file -> fresh empty state (the common, harmless case: nobody
    has registered a thread yet). Present-but-unreadable/malformed file ->
    CorruptStateError, always -- never silently reset. Empty-but-present
    file is treated as corrupt too (a 0-byte file is not a valid empty
    state; our own atomic writer never produces one, so its presence means
    something wrote outside this module's contract).

    Also enforces SCHEMA_VERSION: a `version` newer than this code
    supports raises CorruptStateError rather than being parsed under the
    wrong assumptions. This is a single-user machine, not a service with
    rolling deploys and compatibility obligations to a fleet of other
    readers -- a future schema bump silently parsed under old-schema
    assumptions is a worse failure than a loud, immediate refusal to
    load. (F7 in the 2026-08-07 independent review: SCHEMA_VERSION was
    written on every save but never read back on load.)

    A `version` field that IS present but isn't an int (a hand-edited
    `"2"`, `2.0`, `null`-as-a-string, whatever) is rejected the same way
    -- `isinstance(version, int) and version > SCHEMA_VERSION` alone lets
    a non-int value slide through un-checked, since `isinstance(..., int)`
    is simply False for it and the newer-than-supported branch never
    fires. That's the same "silently parse under the wrong assumptions"
    failure this guard exists to prevent, just via a type mismatch
    instead of a numeric one (residual from the 2026-08-07 independent
    review's delta pass). `bool` is deliberately excluded even though
    Python's `bool` subclasses `int` -- `"version": true` is not a
    version number."""
    if not path.exists():
        return _default_state()
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        raise CorruptStateError(f"{path}: unreadable ({e})") from e
    if not raw.strip():
        raise CorruptStateError(f"{path}: present but empty (0 bytes) — not a valid state file")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise CorruptStateError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("threads"), list):
        raise CorruptStateError(
            f"{path}: unexpected shape (expected an object with a 'threads' list)"
        )
    version = data.get("version")
    if version is not None:
        if not isinstance(version, int) or isinstance(version, bool):
            raise CorruptStateError(
                f"{path}: 'version' is {version!r}, expected an integer -- "
                f"refusing to load a state file whose own schema-version "
                f"field doesn't parse as one."
            )
        if version > SCHEMA_VERSION:
            raise CorruptStateError(
                f"{path}: state file is schema version {version}, this code only "
                f"understands up to version {SCHEMA_VERSION} -- refusing to load "
                f"a file a newer version of this plugin may have written in a "
                f"shape this code doesn't understand. Upgrade the plugin, or "
                f"move the file aside by hand."
            )
    for t in data["threads"]:
        if not isinstance(t, dict) or not t.get("id"):
            raise CorruptStateError(f"{path}: a thread entry is malformed (missing 'id')")
    return data


def atomic_write(data: dict, path: Path = STATE_PATH) -> None:
    """Write-to-tempfile-then-os.replace so a concurrent reader (the warn
    hook, firing on every turn) never observes a torn/partial file. Same
    filesystem as the target by construction (tempfile lives alongside it),
    so os.replace is atomic on every platform this plugin targets (macOS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked_state(path: Path = STATE_PATH, timeout: float = 5.0):
    """Context manager for a read-modify-write on the state file that is
    safe against concurrent CLI invocations. Holds an exclusive advisory
    lock (flock on a sibling `.threads.json.lock` file) across the whole
    load -> mutate -> atomic_write span, so concurrent `threads add`/
    `close` processes serialize instead of racing a last-writer-wins clobber
    against `atomic_write`'s os.replace (atomic_write only makes the final
    rename atomic; it was never a substitute for a lock around the
    surrounding read-modify-write).

    Yields the loaded state dict for in-place mutation by the caller.
    Writes it back via atomic_write on clean exit; an exception inside the
    `with` block skips the write and propagates, leaving the on-disk state
    untouched (same "never partially commit" contract as atomic_write
    itself). CorruptStateError from the initial load propagates before any
    lock-holding write is attempted. Uses fcntl.flock, which is released
    automatically if the holding process dies, so there is no stale-lock
    cleanup burden -- a timeout here means a live concurrent writer, not a
    crashed one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    fh = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"could not acquire lock on {lock_path} within {timeout}s "
                        f"-- another 'threads' invocation is holding it"
                    )
                time.sleep(0.05)
        data = load_state(path)
        yield data
        atomic_write(data, path)
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def find_thread(data: dict, thread_id: str) -> dict:
    for t in data.get("threads", []):
        if t.get("id") == thread_id:
            return t
    raise ThreadNotFoundError(thread_id)


def open_threads(data: dict) -> list[dict]:
    """Threads not yet explicitly closed. `closed_at` is the only gate —
    resolved-but-not-yet-closed threads still show up here on purpose:
    resolution and closing are different declared moments (see module
    docstring)."""
    return [t for t in data.get("threads", []) if not t.get("closed_at")]


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "thread"


def new_id(what: str, existing_ids: set[str]) -> str:
    base = slugify(what)
    ts = str(int(time.time()))[-5:]
    cand = f"{base}-{ts}"
    if cand not in existing_ids:
        return cand
    return f"{base}-{ts}-{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------- predicate --

@dataclass
class PredicateResult:
    resolved: bool
    note: str = ""          # "" on a clean run; "timeout" / "error: ..." otherwise
    duration_s: float = 0.0


def run_predicate(cmd: str, timeout: float) -> PredicateResult:
    """Runs `cmd` via `/bin/sh -c`, hard-bounded by `timeout`. Contract:
    exit code 0 == resolved; any nonzero exit == not resolved (no special
    meaning attached to particular nonzero codes — a predicate author who
    wants nuance reduces it to exit-0/nonzero themselves, e.g. via `test` or
    `[[ ... ]]`, same convention as the shell `until` idiom this environment
    already uses elsewhere). Never raises: a hung, missing, or erroring
    predicate is reported as unresolved with a note, not an exception —
    callers (in particular the per-turn hook) must be able to treat every
    predicate outcome uniformly.

    stdin/stdout/stderr are all discarded (DEVNULL), not captured: nothing
    in this module or its callers reads a predicate's output, only its
    exit code, so capturing it bought nothing while costing everything --
    an unbounded-output predicate (a stray `cat`, a paging `gh api`, a
    `find /`) was buffering gigabytes into this process's memory and
    blowing well past both this timeout and the caller's wall-clock budget
    on the decode/alloc/free of a buffer nobody looks at. Discarding output
    also means a predicate can never leak bytes onto this hook's own JSON
    stdout channel, and a non-UTF-8-emitting-but-exit-0 predicate can never
    be misreported as unresolved by a decode error.

    Runs `/bin/sh` in its own session (`start_new_session=True`, i.e. a
    fresh process group with `/bin/sh`'s pid as its leader) and, on
    timeout, kills the WHOLE group via `os.killpg` rather than just the
    `/bin/sh` pid. A plain `subprocess.run(timeout=...)` only signals the
    direct child -- a predicate that backgrounds or forks something the
    shell doesn't `wait` on (a stray `&`, a `curl`/`gh` call left running)
    survives its parent's death, reparented and still running: one leaked
    process per turn per overdue slow/hanging thread, forever (F8 in the
    2026-08-07 independent review). killpg reaches every process in the
    group in one signal, whether or not the shell itself was still
    waiting on it."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return PredicateResult(resolved=False, note=f"error: {e}", duration_s=time.monotonic() - start)

    try:
        returncode = proc.wait(timeout=timeout)
        return PredicateResult(resolved=(returncode == 0), duration_s=time.monotonic() - start)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid)
        try:
            # NEW-4 (2026-08-07 independent review, delta pass): this reap
            # wait can itself add up to 2s of wall-clock overshoot beyond
            # the predicate's own timeout on a killed process -- bounded
            # and accepted at shipped defaults (THREADS_PREDICATE_TIMEOUT
            # 3s, THREADS_TOTAL_BUDGET 4s keep the warn hook's worst case
            # well under hooks.json's 8s timeout for threads-warn.py; see
            # README.md's env var reference for both).
            proc.wait(timeout=2)
        except Exception:
            pass  # best-effort reap; a still-unreaped zombie here is not
            # this function's failure mode to fix -- killpg already
            # stopped it from doing any further work.
        return PredicateResult(resolved=False, note="timeout", duration_s=time.monotonic() - start)
    except Exception as e:
        _killpg(proc.pid)
        return PredicateResult(resolved=False, note=f"error: {e}", duration_s=time.monotonic() - start)


def _killpg(pid: int) -> None:
    """Best-effort SIGKILL to the process group led by `pid`. Swallows
    everything -- a group that's already gone (the shell and everything
    it spawned already exited between the timeout firing and this call)
    is success, not an error, and this must never raise past its caller
    (run_predicate's own timeout/error handling)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


# -------------------------------------------------------------------- derive --

@dataclass
class ThreadStatus:
    thread: dict
    resolved: bool
    predicate_note: str
    age: timedelta
    expected_by_ts: datetime | None
    overdue: bool
    orphaned: bool
    orphan_reasons: list[str] = field(default_factory=list)
    # NEW-2 in the 2026-08-07 independent review's delta pass: `age` and
    # `expected_by_ts` above are still populated (for any caller that
    # genuinely wants the synthesized fallback value), but when
    # `opened_at` fails to parse, both are computed from `now` standing
    # in for the real timestamp -- NOT a real measurement. Rendering them
    # as if they were (e.g. "age 0s, expected_by <tomorrow>" right next
    # to "overdue") is self-contradicting evidence on the operator's
    # triage surface: it looks like a fresh, healthy, not-yet-due thread
    # that also somehow orphaned. Callers that render a human-facing line
    # (the warn hook, `threads check`, `threads list`) MUST check these
    # flags and render "?" instead of the synthesized value when set.
    age_unknown: bool = False
    expected_by_unknown: bool = False


def derive_status(
    thread: dict,
    timeout: float,
    now: datetime | None = None,
    skip_predicate_if_not_due: bool = False,
) -> ThreadStatus:
    """The one place resolved/overdue/orphaned/age get computed. Always
    RUNS the predicate — never trusts a cached/prior value — because a
    registry that reports staleness from its own stale cache can't be
    trusted to report staleness at all. The one exception is
    `skip_predicate_if_not_due` (see below), which skips the *invocation*
    without ever trusting a stored value in its place.

    A missing or blank `predicate` is never treated as resolved: a plain
    `/bin/sh -c ''` exits 0, which would let a thread that lost its
    predicate field (hand-edit, partial write, future schema drift)
    self-report "all clear" from nothing -- exactly the failure this
    registry's derived-not-declared design exists to prevent. It is
    reported as an explicit unresolved result with a note instead.

    `skip_predicate_if_not_due=True` (used only by the per-turn warn hook,
    never by `threads check`) skips running the predicate at all when the
    thread cannot possibly be orphaned this call -- i.e. when it is not yet
    past `expected_by` (or has no parseable deadline at all). `orphaned`
    requires both "unresolved" AND "overdue"; when `overdue` is already
    False on timestamps alone, the predicate's exit code cannot change the
    answer, so running it is pure cost with no signal for this caller. This
    is what keeps the per-turn hook from executing arbitrary (possibly
    network-calling) predicate shell every turn for every open thread that
    simply isn't due yet -- the common case for the lifetime of any
    multi-hour/multi-day thread. `resolved` is reported as False (unknown,
    not "checked and false") in the skipped case; that value is never
    surfaced by the warn hook, because a not-yet-due thread can never be
    orphaned regardless of it. `threads check` never sets this flag, so
    the interactive CLI's `resolved` column always reflects a real
    predicate run.

    A second defense-in-depth case, same class as the blank-predicate one
    above (F6 in the 2026-08-07 independent review): if `opened_at` fails
    to parse (missing, corrupt, hand-edited) it falls back to `now` below
    -- fine on its own, but when `expected_by` is a *duration* ("1d")
    rather than an absolute timestamp, `expected` is computed as `opened +
    duration`, i.e. `now + duration`, recomputed FRESH every call. A
    deadline defined as "now plus a fixed offset" can never be reached --
    it recedes by exactly the wall-clock gap between calls, so the thread
    would silently never become overdue and never be checkable by this
    hook again. Rather than trust that receding deadline, a thread in
    exactly this state (unparseable opened_at + duration expected_by) is
    treated as already overdue-eligible.

    NEW-2 in that same review's delta pass: the fix above makes the
    thread correctly DETECTABLE as orphaned, but `age` (`now - opened`,
    with `opened` standing in for the unparseable real value) and, in
    the duration-expected_by case, `expected_by_ts` (computed FROM that
    same stand-in) are synthesized, not measured -- rendering them
    verbatim next to "overdue" reads as self-contradicting evidence
    ("age 0s, expected_by tomorrow" beside "orphaned, right now"). Both
    are still returned (some callers may legitimately want a fallback
    value rather than nothing), but `age_unknown`/`expected_by_unknown`
    tell a rendering caller when to show "?" instead: `age_unknown` is
    set whenever `opened_at` didn't parse at all (age is ALWAYS a guess
    in that case, regardless of what `expected_by` looks like);
    `expected_by_unknown` is set only in the narrower case above, where
    `expected_by_ts` was itself derived from the guessed `opened`. An
    absolute (non-duration) `expected_by` paired with a corrupt
    `opened_at` is NOT `expected_by_unknown` -- that deadline is real,
    independent of `opened_at` entirely."""
    now = now or now_utc()
    opened_parsed = parse_ts(thread.get("opened_at"))
    opened = opened_parsed or now
    expected = parse_expected_by(thread.get("expected_by"), opened)
    predicate = (thread.get("predicate") or "").strip()
    can_be_overdue = bool(expected) and now > expected
    opened_at_unparseable_override = False
    if opened_parsed is None and DURATION_RE.match((thread.get("expected_by") or "").strip()):
        can_be_overdue = True
        opened_at_unparseable_override = True

    if skip_predicate_if_not_due and not can_be_overdue:
        pred = PredicateResult(resolved=False, note="skipped: not due yet")
    elif not predicate:
        pred = PredicateResult(resolved=False, note="no predicate set")
    else:
        pred = run_predicate(predicate, timeout)

    age = now - opened
    overdue = can_be_overdue and not pred.resolved
    reasons = []
    if overdue:
        reasons.append("overdue")
        if opened_at_unparseable_override:
            reasons.append("unparseable_opened_at")
    # Owner-liveness ("the registering session is gone") is intentionally
    # NOT checked here: it would require a network call to the kernel, and
    # this function is called from the per-turn warn hook. `threads check`
    # (the interactive CLI, not a per-turn hook) may add a liveness check
    # later without changing this schema or this function's contract. Note
    # that the predicate itself is caller-supplied arbitrary shell and MAY
    # make network calls (e.g. `gh pr view`) -- `skip_predicate_if_not_due`
    # bounds how often the per-turn hook pays for that, it does not forbid
    # it outright; a predicate for an already-overdue thread still runs.
    orphaned = bool(reasons) and not pred.resolved
    return ThreadStatus(
        thread=thread,
        resolved=pred.resolved,
        predicate_note=pred.note,
        age=age,
        expected_by_ts=expected,
        overdue=overdue,
        orphaned=orphaned,
        orphan_reasons=reasons,
        age_unknown=(opened_parsed is None),
        expected_by_unknown=opened_at_unparseable_override,
    )


# -------------------------------------------------------------------- config --

def load_config(path: Path = CONFIG_PATH) -> dict:
    """Best-effort config read for the (disabled-by-default) enforcement
    tier. Missing/corrupt file -> {} -> every key reads as its documented
    default. Never raises."""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
