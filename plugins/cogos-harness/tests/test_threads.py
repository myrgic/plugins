#!/usr/bin/env python3
"""Self-tests for the threads registry: the CLI (bin/threads), the shared
library (lib/threads_core.py), and the warn hook (hooks/threads-warn.py).

Run directly: `python3 tests/test_threads.py -v`
No third-party dependencies -- stdlib unittest only, per this plugin's
"python3 on PATH" requirement.

Every state-file fixture in this file lives under a per-test tempdir via
COGOS_THREADS_STATE -- these tests never touch the operator's real
~/.cog/status/threads.json.

The hook-silence tests are the load-bearing ones: they assert BYTE-EXACT
empty stdout for every failure mode the build's hard gate calls out
(missing file, corrupt file, predicate timeout, predicate error) plus the
two "nothing wrong" cases (no threads registered, one healthy thread) --
because the gate this hook exists under is "silent unless something is
wrong", and the only way to trust that is to check it directly, per input,
rather than infer it from the code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = PLUGIN_ROOT / "lib"
BIN_THREADS = PLUGIN_ROOT / "bin" / "threads"
HOOK_WARN = PLUGIN_ROOT / "hooks" / "threads-warn.py"

sys.path.insert(0, str(LIB_DIR))
import threads_core as core  # noqa: E402


def run_cli(*args, env_extra=None, input_text=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(BIN_THREADS), *args],
        capture_output=True, text=True, timeout=15, env=env, input=input_text,
    )


def run_hook(env_extra=None, event="UserPromptSubmit"):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_WARN)],
        capture_output=True, text=True, timeout=15, env=env,
        input=json.dumps({"hook_event_name": event}),
    )


class TempState(unittest.TestCase):
    """Base: gives every test its own throwaway state file path, never the
    operator's real one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "threads.json"
        self.env = {"COGOS_THREADS_STATE": str(self.state_path)}

    def tearDown(self):
        self._tmp.cleanup()


# --------------------------------------------------------------- CLI tests --

class TestCliRoundTrip(TempState):
    def test_add_list_check_close(self):
        r = run_cli("add", "--what", "PR verdict", "--why", "blocks merge",
                     "--predicate", "true", "--expected-by", "2h",
                     "--id", "t1", env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("registered thread 't1'", r.stdout)

        r = run_cli("list", env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("t1", r.stdout)
        self.assertIn("[open", r.stdout)

        r = run_cli("check", "t1", env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESOLVED", r.stdout)

        r = run_cli("close", "t1", "--reason", "done", env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("closed 't1'", r.stdout)

        r = run_cli("list", env_extra=self.env)
        self.assertIn("(no open threads)", r.stdout)
        r = run_cli("list", "--all", env_extra=self.env)
        self.assertIn("t1", r.stdout)
        self.assertIn("[closed", r.stdout)

    def test_add_rejects_unparsable_expected_by(self):
        r = run_cli("add", "--what", "x", "--why", "y", "--predicate", "true",
                     "--expected-by", "not-a-time-or-duration", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("neither an ISO8601", r.stderr)

    def test_add_rejects_duplicate_id(self):
        run_cli("add", "--what", "x", "--why", "y", "--predicate", "true",
                 "--expected-by", "1h", "--id", "dup", env_extra=self.env)
        r = run_cli("add", "--what", "x2", "--why", "y", "--predicate", "true",
                     "--expected-by", "1h", "--id", "dup", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)

    def test_check_orphan_sets_nonzero_exit(self):
        run_cli("add", "--what", "x", "--why", "y", "--predicate", "false",
                 "--expected-by", "1s", "--id", "will-orphan", env_extra=self.env)
        import time
        time.sleep(1.2)
        r = run_cli("check", env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ORPHAN", r.stdout)

    def test_close_unknown_id_fails(self):
        r = run_cli("close", "nope", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such thread", r.stderr)

    def test_list_missing_file_is_empty_not_error(self):
        r = run_cli("list", env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("(no open threads)", r.stdout)

    def test_cli_refuses_corrupt_file_never_resets_it(self):
        self.state_path.write_text("{not valid json", encoding="utf-8")
        before = self.state_path.read_text(encoding="utf-8")
        r = run_cli("list", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("corrupt", r.stderr)
        self.assertIn("Never auto-reset", r.stderr)
        after = self.state_path.read_text(encoding="utf-8")
        self.assertEqual(before, after, "corrupt file must be left untouched")

    def test_add_rejects_empty_predicate(self):
        r = run_cli("add", "--what", "x", "--why", "y", "--predicate", "",
                     "--expected-by", "1h", "--id", "emptypred", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not be empty", r.stderr)
        # and it must not have been written to state at all
        r = run_cli("list", "--all", env_extra=self.env)
        self.assertNotIn("emptypred", r.stdout)

    def test_add_rejects_whitespace_only_predicate(self):
        r = run_cli("add", "--what", "x", "--why", "y", "--predicate", "   ",
                     "--expected-by", "1h", "--id", "wspred", env_extra=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not be empty", r.stderr)

    def test_concurrent_add_does_not_lose_registrations(self):
        # Reproduces the lost-write race: N concurrent `threads add`
        # invocations against a fresh registry must all survive. Before the
        # locked_state() fix this reliably dropped entries (last-writer-wins
        # on the unguarded read-modify-write) while every process still
        # printed "registered" and exited 0.
        import concurrent.futures
        n = 8
        ids = [f"r{i}" for i in range(n)]

        def add_one(tid):
            return run_cli("add", "--what", f"concurrent {tid}", "--why", "y",
                            "--predicate", "true", "--expected-by", "1h",
                            "--id", tid, env_extra=self.env)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(add_one, ids))

        for r in results:
            self.assertEqual(r.returncode, 0, r.stderr)

        r = run_cli("list", "--all", "--json", env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        registered = {t["id"] for t in json.loads(r.stdout)}
        self.assertEqual(registered, set(ids),
                          f"lost registrations: expected {sorted(ids)}, got {sorted(registered)}")

    def test_check_unparseable_opened_at_renders_unknown_age_and_expected_by(self):
        # NEW-2 (2026-08-07 independent review, delta pass): same
        # self-contradicting-rendering hazard as the warn hook, checked
        # against `threads check`'s own output. Constructed by
        # hand-writing state, since `threads add` always stamps a real
        # opened_at.
        state = {
            "version": 1,
            "threads": [{
                "id": "f6thread", "what": "corrupt opened_at", "why": "y",
                "predicate": "false", "opened_at": "not-a-timestamp",
                "expected_by": "1d", "owner": "me",
                "closed_at": None, "closed_reason": None,
            }],
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        r = run_cli("check", "f6thread", env_extra=self.env)
        self.assertEqual(r.returncode, 1)  # orphaned -> nonzero exit
        self.assertIn("ORPHAN", r.stdout)
        self.assertIn("age=?", r.stdout)
        self.assertIn("expected_by=?", r.stdout)
        self.assertNotRegex(r.stdout, r"expected_by=\d{4}-\d{2}-\d{2}")


# ------------------------------------------------------------- library tests --

class TestLibrary(unittest.TestCase):
    def test_load_state_missing_file_is_empty_default(self):
        p = Path(tempfile.mkdtemp()) / "nope.json"
        data = core.load_state(p)
        self.assertEqual(data, {"version": 1, "threads": []})

    def test_load_state_empty_file_is_corrupt(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text("", encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_bad_json_is_corrupt(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text("{oops", encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_wrong_shape_is_corrupt(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"threads": "not-a-list"}), encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_atomic_write_survives_a_reader_mid_write(self):
        # Not a true concurrency test (that needs real threads/processes),
        # but proves the write path never leaves a partial target: the
        # target file is either the old complete content or the new
        # complete content, never a torn write, because atomic_write always
        # goes through a tempfile + os.replace.
        p = Path(tempfile.mkdtemp()) / "threads.json"
        core.atomic_write({"version": 1, "threads": []}, p)
        core.atomic_write({"version": 1, "threads": [{"id": "a"}]}, p)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in data["threads"]], ["a"])
        # no leftover tempfiles
        leftovers = [f for f in p.parent.iterdir() if f.name.startswith(".threads.json.tmp-")]
        self.assertEqual(leftovers, [])

    def test_parse_expected_by_duration_and_timestamp(self):
        opened = core.now_utc()
        d = core.parse_expected_by("2h", opened)
        self.assertIsNotNone(d)
        self.assertAlmostEqual((d - opened).total_seconds(), 7200, delta=1)

        ts = core.iso(opened)
        d2 = core.parse_expected_by(ts, opened)
        self.assertIsNotNone(d2)

        self.assertIsNone(core.parse_expected_by("garbage", opened))

    def test_run_predicate_resolved_on_exit_zero(self):
        r = core.run_predicate("true", timeout=2)
        self.assertTrue(r.resolved)
        self.assertEqual(r.note, "")

    def test_run_predicate_unresolved_on_nonzero_exit(self):
        r = core.run_predicate("false", timeout=2)
        self.assertFalse(r.resolved)

    def test_run_predicate_times_out_never_raises(self):
        r = core.run_predicate("sleep 5", timeout=0.3)
        self.assertFalse(r.resolved)
        self.assertEqual(r.note, "timeout")

    def test_run_predicate_unbounded_output_is_cheap_and_bounded(self):
        # A predicate that floods stdout must not blow the timeout or buffer
        # gigabytes into this process -- output is DEVNULL'd, never
        # captured, since nothing reads it. `cat /dev/zero` never exits on
        # its own, so this also exercises the timeout path with a predicate
        # that produces unbounded bytes until it's killed.
        start = core.time.monotonic()
        r = core.run_predicate("cat /dev/zero", timeout=0.5)
        elapsed = core.time.monotonic() - start
        self.assertFalse(r.resolved)
        self.assertEqual(r.note, "timeout")
        # generous ceiling -- was 9+ seconds when output was captured
        self.assertLess(elapsed, 3.0, f"took {elapsed:.2f}s; output capture likely regressed")

    def test_run_predicate_non_utf8_output_is_not_misreported(self):
        # A predicate that exits 0 but emits non-UTF-8 bytes must still be
        # reported resolved -- output is never decoded, only the exit code
        # is read.
        r = core.run_predicate("head -c 64 /dev/urandom >/dev/null; exit 0", timeout=2)
        self.assertTrue(r.resolved)
        self.assertEqual(r.note, "")

    def test_run_predicate_swallows_exceptions(self):
        # Force an internal error path (not a shell nonzero-exit, an actual
        # exception in subprocess.run) by pointing at a timeout value that
        # is invalid for the underlying API, and confirm run_predicate still
        # returns a PredicateResult rather than propagating.
        r = core.run_predicate("true", timeout=-1)
        self.assertFalse(r.resolved)
        self.assertTrue(r.note == "timeout" or r.note.startswith("error"))

    def test_derive_status_orphan_requires_unresolved_and_overdue(self):
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": core.iso(opened), "expected_by": "1s",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.resolved)
        self.assertTrue(st.overdue)
        self.assertTrue(st.orphaned)

    def test_derive_status_resolved_thread_is_never_orphaned_even_if_overdue(self):
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {
            "id": "x", "predicate": "true",
            "opened_at": core.iso(opened), "expected_by": "1s",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.resolved)
        self.assertFalse(st.orphaned)

    def test_derive_status_unresolved_but_not_overdue_is_not_orphaned(self):
        opened = core.now_utc()
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": core.iso(opened), "expected_by": "1h",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.resolved)
        self.assertFalse(st.overdue)
        self.assertFalse(st.orphaned)

    def test_derive_status_empty_predicate_is_never_resolved(self):
        # A thread that lost its predicate (hand-edit, partial write, future
        # schema drift) must never self-report "all clear" -- `/bin/sh -c
        # ''` exits 0, which would make it resolved if run at all. It must
        # be reported as an explicit unresolved result, and it must be
        # capable of going orphaned once overdue (unlike a healthy silence).
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {"id": "x", "predicate": "", "opened_at": core.iso(opened), "expected_by": "1s"}
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.resolved)
        self.assertTrue(st.overdue)
        self.assertTrue(st.orphaned)
        self.assertIn("predicate", st.predicate_note)

    def test_derive_status_missing_predicate_key_is_never_resolved(self):
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {"id": "x", "opened_at": core.iso(opened), "expected_by": "1s"}
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.resolved)
        self.assertTrue(st.orphaned)

    def test_derive_status_skips_predicate_when_not_due(self):
        # skip_predicate_if_not_due=True (the per-turn hook's mode): a
        # thread nowhere near its deadline must never actually invoke the
        # predicate, since the exit code can't change orphaned/overdue
        # either way. Use a predicate that would time out if it ran at all,
        # with a timeout far shorter than that predicate needs, and confirm
        # it still comes back near-instantly and non-orphaned.
        opened = core.now_utc()
        thread = {
            "id": "x", "predicate": "sleep 30",
            "opened_at": core.iso(opened), "expected_by": "1h",
        }
        start = core.time.monotonic()
        st = core.derive_status(thread, timeout=5, skip_predicate_if_not_due=True)
        elapsed = core.time.monotonic() - start
        self.assertLess(elapsed, 1.0, "predicate should never have been invoked")
        self.assertFalse(st.overdue)
        self.assertFalse(st.orphaned)
        self.assertIn("skipped", st.predicate_note)

    def test_derive_status_still_runs_predicate_when_overdue_even_in_skip_mode(self):
        # skip_predicate_if_not_due must not suppress the predicate once a
        # thread actually is past its deadline -- that's the one case its
        # exit code still matters.
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {
            "id": "x", "predicate": "true",
            "opened_at": core.iso(opened), "expected_by": "1s",
        }
        st = core.derive_status(thread, timeout=2, skip_predicate_if_not_due=True)
        self.assertTrue(st.resolved)
        self.assertFalse(st.orphaned)  # resolved, so never orphaned even though overdue

    def test_derive_status_skip_mode_does_not_change_check_command_behavior(self):
        # threads check never passes skip_predicate_if_not_due, so its
        # resolved/orphaned reporting for a not-yet-due thread must still
        # reflect an actually-executed predicate (default False == old
        # behavior, unchanged).
        opened = core.now_utc()
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": core.iso(opened), "expected_by": "1h",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertEqual(st.predicate_note, "")
        self.assertFalse(st.resolved)


    # ---------------------------------------------------------- F6 --
    # opened_at unparseable + expected_by a *duration* would otherwise
    # recompute `now + duration` fresh on every call -- a deadline that
    # recedes forever and can never be reached, making the thread
    # permanently invisible to the warn hook. Defense-in-depth mirrors the
    # blank-predicate case: treat it as already overdue-eligible.

    def test_derive_status_unparseable_opened_at_with_duration_expected_by_is_overdue(self):
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": "not-a-timestamp", "expected_by": "1d",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.overdue)
        self.assertTrue(st.orphaned)

    def test_derive_status_missing_opened_at_with_duration_expected_by_is_overdue(self):
        thread = {"id": "x", "predicate": "false", "expected_by": "1h"}
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.overdue)
        self.assertTrue(st.orphaned)

    def test_derive_status_resolved_thread_with_unparseable_opened_at_still_never_orphaned(self):
        # The overdue-eligible override must not itself force orphaned --
        # a resolved predicate is still never orphaned, same invariant as
        # test_derive_status_resolved_thread_is_never_orphaned_even_if_overdue.
        thread = {
            "id": "x", "predicate": "true",
            "opened_at": "garbage", "expected_by": "1h",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.resolved)
        self.assertFalse(st.orphaned)

    def test_derive_status_unparseable_opened_at_with_absolute_expected_by_is_unaffected(self):
        # The F6 defense-in-depth is scoped to DURATION expected_by only
        # -- an absolute ISO8601 expected_by is computed independently of
        # opened_at and does not recede regardless, so a corrupt
        # opened_at paired with a not-yet-due absolute deadline must
        # behave exactly as before: not overdue.
        future = core.iso(core.now_utc() + core.timedelta(hours=1))
        thread = {"id": "x", "predicate": "false", "opened_at": "garbage", "expected_by": future}
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.overdue)
        self.assertFalse(st.orphaned)

    def test_derive_status_parseable_opened_at_with_duration_expected_by_is_unaffected(self):
        # Sanity check that the F6 override only fires when opened_at
        # itself fails to parse -- a normal, healthy thread with a
        # parseable opened_at and a duration expected_by must behave
        # exactly as before (not yet overdue, since it was just opened).
        opened = core.now_utc()
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": core.iso(opened), "expected_by": "1h",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.overdue)
        self.assertFalse(st.orphaned)

    # --------------------------------------------------------- NEW-2 --
    # Delta-pass residual (2026-08-07 independent review, second pass):
    # the F6 override above correctly DETECTS the orphan, but rendering
    # its synthesized age/expected_by verbatim ("age 0s, expected_by
    # tomorrow" beside "orphaned, right now") is self-contradicting
    # evidence. derive_status now flags age_unknown/expected_by_unknown
    # and adds an explicit "unparseable_opened_at" reason.

    def test_derive_status_unparseable_opened_at_flags_age_and_expected_by_unknown(self):
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": "not-a-timestamp", "expected_by": "1d",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.age_unknown)
        self.assertTrue(st.expected_by_unknown)
        self.assertIn("unparseable_opened_at", st.orphan_reasons)
        self.assertIn("overdue", st.orphan_reasons)

    def test_derive_status_unparseable_opened_at_with_absolute_expected_by_is_only_age_unknown(self):
        # Asymmetric case: opened_at is corrupt (age is always a guess),
        # but a non-duration expected_by is a real, independent deadline
        # -- expected_by_unknown must stay False, and since it's not yet
        # due, "unparseable_opened_at" must not appear as a reason (the
        # thread isn't even overdue, let alone orphaned via this path).
        future = core.iso(core.now_utc() + core.timedelta(hours=1))
        thread = {"id": "x", "predicate": "false", "opened_at": "garbage", "expected_by": future}
        st = core.derive_status(thread, timeout=2)
        self.assertTrue(st.age_unknown)
        self.assertFalse(st.expected_by_unknown)
        self.assertNotIn("unparseable_opened_at", st.orphan_reasons)

    def test_derive_status_parseable_opened_at_is_never_age_or_expected_by_unknown(self):
        opened = core.now_utc() - core.timedelta(seconds=5)
        thread = {
            "id": "x", "predicate": "false",
            "opened_at": core.iso(opened), "expected_by": "1s",
        }
        st = core.derive_status(thread, timeout=2)
        self.assertFalse(st.age_unknown)
        self.assertFalse(st.expected_by_unknown)
        self.assertNotIn("unparseable_opened_at", st.orphan_reasons)

    # ---------------------------------------------------------- F7 --
    # SCHEMA_VERSION was written on every save but never read back on
    # load -- a future schema bump would parse silently under old-schema
    # assumptions instead of failing loudly.

    def test_load_state_future_schema_version_is_corrupt(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": core.SCHEMA_VERSION + 1, "threads": []}), encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_current_schema_version_loads_fine(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": core.SCHEMA_VERSION, "threads": []}), encoding="utf-8")
        data = core.load_state(p)
        self.assertEqual(data["version"], core.SCHEMA_VERSION)

    def test_load_state_missing_version_key_loads_fine(self):
        # A version key is only enforced when PRESENT -- a state file
        # that predates this field entirely (or one some other tool wrote
        # without it) must not be rejected on that basis alone.
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"threads": []}), encoding="utf-8")
        data = core.load_state(p)
        self.assertEqual(data["threads"], [])

    def test_load_state_string_version_is_corrupt(self):
        # F7 residual (2026-08-07 independent review, delta pass):
        # isinstance(version, int) alone lets a non-int version slide
        # through un-checked (the newer-than-supported branch simply
        # never fires for it) -- a hand-edited '"version": "2"' must be
        # rejected, not silently loaded under this code's assumptions.
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": "2", "threads": []}), encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_float_version_is_corrupt(self):
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": 1.0, "threads": []}), encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_bool_version_is_corrupt(self):
        # bool is an int subclass in Python -- must not slip past the
        # isinstance(int) check as if `true` were a real version number.
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": True, "threads": []}), encoding="utf-8")
        with self.assertRaises(core.CorruptStateError):
            core.load_state(p)

    def test_load_state_null_version_is_treated_as_absent(self):
        # Explicit JSON null and a missing key are the same "no
        # information" case -- both must load fine, not be rejected as a
        # type mismatch.
        p = Path(tempfile.mkdtemp()) / "threads.json"
        p.write_text(json.dumps({"version": None, "threads": []}), encoding="utf-8")
        data = core.load_state(p)
        self.assertEqual(data["threads"], [])



    # ---------------------------------------------------------- F8 --
    # A predicate that backgrounds or forks a grandchild the shell
    # doesn't wait on used to survive the timeout kill (only the direct
    # /bin/sh child was signaled) -- one leaked, reparented process per
    # turn per overdue slow/hanging thread. run_predicate now runs the
    # predicate in its own process group (start_new_session=True) and
    # kills the WHOLE group via os.killpg on timeout.

    def test_run_predicate_timeout_kills_backgrounded_descendant(self):
        marker = f"threads_core_leak_test_{os.getpid()}_{int(core.time.time() * 1000)}"
        # The outer predicate backgrounds a long-sleeping python3 process
        # (identifiable via the marker in its argv) that the shell does
        # NOT wait on, then itself blocks past the timeout -- reproducing
        # exactly the "grandchild the shell doesn't wait on" leak F8
        # describes.
        cmd = f"python3 -c 'import time; time.sleep(30)' {marker} & sleep 30"
        try:
            r = core.run_predicate(cmd, timeout=0.3)
            self.assertFalse(r.resolved)
            self.assertEqual(r.note, "timeout")

            deadline = core.time.monotonic() + 3.0
            leaked = True
            while core.time.monotonic() < deadline:
                check = subprocess.run(
                    ["pgrep", "-f", marker], capture_output=True, text=True
                )
                if check.returncode != 0:  # pgrep: no matching process
                    leaked = False
                    break
                core.time.sleep(0.2)
            self.assertFalse(
                leaked,
                f"backgrounded descendant matching {marker!r} survived the "
                f"predicate timeout -- process group was not fully killed",
            )
        finally:
            # Best-effort cleanup regardless of assertion outcome -- never
            # leave a real sleep-30 process behind because a test failed.
            subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)




class TestWarnHookSilence(TempState):
    """Every scenario the build's hard gate calls out for the hook,
    checked directly against the hook's actual stdout."""

    def test_nothing_registered_is_silent(self):
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_missing_state_file_is_silent(self):
        self.assertFalse(self.state_path.exists())
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_corrupt_state_file_is_silent(self):
        self.state_path.write_text("{not json at all", encoding="utf-8")
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_empty_state_file_is_silent(self):
        self.state_path.write_text("", encoding="utf-8")
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_one_healthy_thread_is_silent(self):
        run_cli("add", "--what", "healthy thing", "--why", "y",
                 "--predicate", "true", "--expected-by", "2h", "--id", "healthy",
                 env_extra=self.env)
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_unresolved_but_not_overdue_is_silent(self):
        run_cli("add", "--what", "still waiting", "--why", "y",
                 "--predicate", "false", "--expected-by", "1h", "--id", "waiting",
                 env_extra=self.env)
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_predicate_timeout_but_not_overdue_is_silent(self):
        run_cli("add", "--what", "slow but not due", "--why", "y",
                 "--predicate", "sleep 5", "--expected-by", "1h", "--id", "slow-ok",
                 env_extra=self.env)
        env = dict(self.env, COGOS_THREADS_PREDICATE_TIMEOUT="0.3")
        r = run_hook(env_extra=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_not_due_thread_never_pays_predicate_latency(self):
        # The cost-side counterpart to the silence test above: a thread with
        # a deadline nowhere near due must not just stay silent, it must
        # come back fast -- its predicate's exit code cannot affect
        # `orphaned` (which requires overdue), so the hook must skip
        # running it (skip_predicate_if_not_due) rather than paying the
        # full per-predicate timeout every single turn for the thread's
        # entire lifetime. Uses a predicate timeout the hook would have to
        # wait out if it actually ran the predicate, to make a regression
        # here fail loudly rather than by a hair.
        run_cli("add", "--what", "distant deadline", "--why", "y",
                 "--predicate", "sleep 30", "--expected-by", "1w", "--id", "distant",
                 env_extra=self.env)
        env = dict(self.env, COGOS_THREADS_PREDICATE_TIMEOUT="3")
        import time
        start = time.monotonic()
        r = run_hook(env_extra=env)
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertLess(elapsed, 2.0, f"hook took {elapsed:.2f}s; predicate was likely run unnecessarily")

    def test_predicate_error_but_not_overdue_is_silent(self):
        run_cli("add", "--what", "bad cmd not due", "--why", "y",
                 "--predicate", "nonexistent_command_xyz", "--expected-by", "1h",
                 "--id", "err-ok", env_extra=self.env)
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_closed_orphan_stays_silent(self):
        run_cli("add", "--what", "was orphaned", "--why", "y", "--predicate", "false",
                 "--expected-by", "1s", "--id", "closed-orphan", env_extra=self.env)
        import time
        time.sleep(1.2)
        run_cli("close", "closed-orphan", env_extra=self.env)
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class TestWarnHookSpeaksWhenWarranted(TempState):
    def test_one_orphan_produces_a_block(self):
        run_cli("add", "--what", "PR 118 review verdict", "--why", "blocks merge",
                 "--predicate", "false", "--expected-by", "1s", "--id", "pr118",
                 env_extra=self.env)
        import time
        time.sleep(1.2)
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(r.stdout.strip(), "")
        payload = json.loads(r.stdout)
        block = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pr118", block)
        self.assertIn("threads_warn", block)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_orphan_via_timeout_produces_a_block(self):
        run_cli("add", "--what", "hangs", "--why", "y", "--predicate", "sleep 5",
                 "--expected-by", "1s", "--id", "hangs", env_extra=self.env)
        import time
        time.sleep(1.2)
        env = dict(self.env, COGOS_THREADS_PREDICATE_TIMEOUT="0.3")
        r = run_hook(env_extra=env)
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(r.stdout.strip(), "")
        self.assertIn("hangs", r.stdout)
        self.assertIn("timeout", r.stdout)

    def test_healthy_and_orphan_mixed_reports_only_the_orphan(self):
        run_cli("add", "--what", "fine", "--why", "y", "--predicate", "true",
                 "--expected-by", "2h", "--id", "fine", env_extra=self.env)
        run_cli("add", "--what", "stuck", "--why", "y", "--predicate", "false",
                 "--expected-by", "1s", "--id", "stuck", env_extra=self.env)
        import time
        time.sleep(1.2)
        r = run_hook(env_extra=self.env)
        payload = json.loads(r.stdout)
        block = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("stuck", block)
        self.assertNotIn("fine", block)

    def test_unparseable_opened_at_orphan_renders_unknown_age_and_expected_by(self):
        # NEW-2 (2026-08-07 independent review, delta pass): this thread
        # can only be constructed by hand-writing state -- `threads add`
        # always stamps a real opened_at, so this simulates hand-edited /
        # partial-write state, the same class this defense-in-depth
        # exists for. Must render "age ?, expected_by ?" and the explicit
        # "unparseable_opened_at" reason, never a synthesized "age 0s"
        # next to a synthesized future expected_by.
        state = {
            "version": 1,
            "threads": [{
                "id": "f6thread", "what": "corrupt opened_at", "why": "y",
                "predicate": "false", "opened_at": "not-a-timestamp",
                "expected_by": "1d", "owner": "me",
                "closed_at": None, "closed_reason": None,
            }],
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        r = run_hook(env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        block = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("f6thread", block)
        self.assertIn("unparseable_opened_at", block)
        self.assertIn("age ?,", block)
        self.assertIn("expected_by ?,", block)
        # The old (buggy) rendering would have shown a real ISO timestamp
        # for expected_by -- make sure none leaked through.
        self.assertNotRegex(block, r"expected_by \d{4}-\d{2}-\d{2}")


def _load_warn_hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cogos_threads_warn_hook", HOOK_WARN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ScriptedClock:
    """A fake `time.monotonic`-shaped callable that returns a scripted
    sequence of values, repeating the last one once exhausted. Lets a
    test drive threads-warn.py's budget-exhaustion path deterministically
    instead of depending on real predicate/OS timing -- see F1 tests
    below for why that matters."""

    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def __call__(self):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


class TestWarnHookBudgetFairness(TempState):
    """F1 (HIGH, 2026-08-07 independent review): budget exhaustion used
    to silently suppress the entire orphan report whenever the threads
    ahead of a genuine orphan in registry order consumed the whole
    wall-clock budget -- reproduced 5/5 in review. Two fixes, both
    exercised here: the budget note now renders unconditionally
    (`_render_block`), and scan order rotates across invocations
    (`_rotation_offset`) so the same thread doesn't starve forever.

    These drive threads-warn.py's internal functions directly with a
    scripted clock rather than spawning the hook as a subprocess and
    hoping real predicate/OS timing lines up -- the reviewer's own repro
    needed "COGOS_THREADS_TOTAL_BUDGET=1.0, 5/5 runs" phrasing because
    real-timing reproduction is inherently a race; a scripted clock makes
    the exact scenario deterministic instead."""

    def setUp(self):
        super().setUp()
        self.hook = _load_warn_hook_module()

    def _overdue_thread(self, id_, predicate):
        import threads_core as core
        opened = core.now_utc() - core.timedelta(seconds=5)
        return {
            "id": id_, "predicate": predicate,
            "opened_at": core.iso(opened), "expected_by": "1s",
        }

    # -- _render_block: the unconditional-notice half of the fix --------

    def test_render_block_is_none_when_nothing_found_and_nothing_skipped(self):
        self.assertIsNone(self.hook._render_block([], 0))

    def test_render_block_budget_only_notice_is_nonempty(self):
        # This is the exact case F1 flags: zero orphans found, but some
        # threads were never checked. Pre-fix this rendered nothing.
        block = self.hook._render_block([], skipped_for_budget=2)
        self.assertIsNotNone(block)
        self.assertNotEqual(block.strip(), "")
        self.assertIn("over budget", block)
        self.assertIn("threads_warn", block)

    def test_render_block_orphans_plus_budget_note_both_present(self):
        block = self.hook._render_block(['⚠ x (overdue): "y"'], skipped_for_budget=1)
        self.assertIn("⚠ x", block)
        self.assertIn("over budget", block)

    # -- _scan: reproduces the exact starvation scenario ----------------

    def test_starvation_scenario_budget_consumed_by_nonorphaned_threads(self):
        # Registry order: two threads whose predicates run to completion
        # and resolve TRUE (never orphaned) ahead of a genuine orphan
        # last in registry order. The scripted clock simulates the
        # budget being consumed by the time the first thread finishes,
        # so the second and third (including the genuine orphan) are
        # dropped at the loop-top budget check without their predicate
        # ever running -- byte-for-byte the reviewer's repro, just
        # clock-driven instead of timing-driven.
        import threads_core as core
        threads = [
            self._overdue_thread("healthy-a", "true"),
            self._overdue_thread("healthy-b", "true"),
            self._overdue_thread("genuine-orphan", "false"),
        ]
        clock = _ScriptedClock([0.0, 0.1, 0.2, 5.0, 5.0, 5.0, 5.0, 5.0])
        rotation_path = self.state_path.with_name(".rotation-starvation-test")

        orphan_lines, skipped = self.hook._scan(
            threads, core, budget=1.0, predicate_timeout=3.0,
            clock=clock, rotation_path=rotation_path,
        )

        self.assertEqual(orphan_lines, [], "setup sanity: the orphan must be among the SKIPPED threads")
        self.assertEqual(skipped, 2)

        # The fixed hook's actual output for this exact scan result must
        # be non-empty -- this is the assertion F1 calls out as missing:
        # "no test exercises COGOS_THREADS_TOTAL_BUDGET exhaustion at
        # all."
        block = self.hook._render_block(orphan_lines, skipped)
        self.assertIsNotNone(block, "F1: must not stay silent when threads were skipped for budget")
        self.assertNotEqual(block.strip(), "")
        self.assertIn("over budget", block)

    # -- _rotation_offset / rotation-in-_scan: the fairness half --------

    def test_rotation_offset_cycles_through_every_position(self):
        rotation_path = self.state_path.with_name(".rotation-cycle-test")
        offsets = [self.hook._rotation_offset(3, rotation_path) for _ in range(7)]
        self.assertEqual(offsets, [0, 1, 2, 0, 1, 2, 0])

    def test_rotation_offset_single_thread_is_always_zero(self):
        rotation_path = self.state_path.with_name(".rotation-single-test")
        self.assertEqual(self.hook._rotation_offset(1, rotation_path), 0)
        self.assertEqual(self.hook._rotation_offset(1, rotation_path), 0)

    def test_rotation_offset_missing_counter_file_starts_at_zero(self):
        rotation_path = self.state_path.with_name(".rotation-missing-test")
        self.assertFalse(rotation_path.exists())
        self.assertEqual(self.hook._rotation_offset(4, rotation_path), 0)

    def test_rotation_offset_corrupt_counter_file_falls_back_to_zero(self):
        rotation_path = self.state_path.with_name(".rotation-corrupt-test")
        rotation_path.write_text("not-a-number", encoding="utf-8")
        self.assertEqual(self.hook._rotation_offset(3, rotation_path), 0)

    def test_rotation_eventually_surfaces_the_orphan(self):
        # The scenario rotation exists to fix: with a FIXED scan order, a
        # budget tight enough to check only one thread per turn NEVER
        # reaches the genuine orphan sitting last in registry order --
        # every turn starves it identically. Across repeated invocations
        # (one rotation counter, persisted, like real per-turn hook
        # calls), rotation must eventually put it first.
        import threads_core as core
        threads = [
            self._overdue_thread("healthy-a", "true"),
            self._overdue_thread("healthy-b", "true"),
            self._overdue_thread("genuine-orphan", "false"),
        ]
        rotation_path = self.state_path.with_name(".rotation-eventual-test")
        found_orphan_on_turn = []

        for turn in range(3):
            # Fresh scripted clock each "turn" (each hook invocation gets
            # its own budget window in reality); same shape as the
            # starvation test -- budget covers exactly one thread.
            clock = _ScriptedClock([0.0, 0.1, 0.2, 5.0, 5.0, 5.0, 5.0, 5.0])
            orphan_lines, _skipped = self.hook._scan(
                threads, core, budget=1.0, predicate_timeout=3.0,
                clock=clock, rotation_path=rotation_path,
            )
            if any("genuine-orphan" in line for line in orphan_lines):
                found_orphan_on_turn.append(turn)

        self.assertTrue(found_orphan_on_turn, "rotation never surfaced the orphan across 3 turns")
        # Deterministic for this exact setup: a fresh rotation counter
        # cycles offsets 0, 1, 2 across the three turns, and the orphan
        # sits at index 2 in registry order -- so it's scanned FIRST (and
        # thus actually checked, since the budget only covers one thread)
        # on exactly the third turn.
        self.assertEqual(found_orphan_on_turn, [2])




class TestGatePrScaffoldDisabledByDefault(TempState):
    """The enforcement-tier hook itself, per the build's requirement that
    it's present but inert unless explicitly armed."""

    def setUp(self):
        super().setUp()
        # F5: every gate test must supply its own COGOS_THREADS_CONFIG, a
        # nonexistent-by-default fixture path -- without this, any test
        # that doesn't explicitly arm the gate (i.e. doesn't build its own
        # cfg_path, as the "armed" tests below do) leaves the env var
        # unset, which resolves to the OPERATOR'S REAL
        # ~/.cog/status/threads-config.json. If that file happens to have
        # enforce_pr_create_thread=true set on the machine actually
        # running these tests, "disabled by default" assertions below
        # would fail against live machine state instead of the fixture.
        self.config_path = Path(self._tmp.name) / "threads-config.json"
        self.env["COGOS_THREADS_CONFIG"] = str(self.config_path)

    def _run_gate(self, command, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        gate = PLUGIN_ROOT / "hooks" / "threads-gate-pr.py"
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        return subprocess.run(
            [sys.executable, str(gate)], capture_output=True, text=True,
            timeout=10, env=env, input=json.dumps(payload),
        )

    def test_default_off_allows_even_with_no_open_threads(self):
        r = self._run_gate("gh pr create --title x --body y", env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {})

    def test_non_matching_command_always_allowed(self):
        r = self._run_gate("ls -la", env_extra=self.env)
        self.assertEqual(json.loads(r.stdout), {})

    def test_armed_and_no_open_threads_denies(self):
        cfg_dir = Path(tempfile.mkdtemp())
        cfg_path = cfg_dir / "threads-config.json"
        cfg_path.write_text(json.dumps({"enforce_pr_create_thread": True}), encoding="utf-8")
        env = dict(self.env, COGOS_THREADS_CONFIG=str(cfg_path))
        r = self._run_gate("gh pr create --title x --body y", env_extra=env)
        resp = json.loads(r.stdout)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_armed_and_open_thread_present_allows(self):
        run_cli("add", "--what", "x", "--why", "y", "--predicate", "true",
                 "--expected-by", "1h", "--id", "t1", env_extra=self.env)
        cfg_dir = Path(tempfile.mkdtemp())
        cfg_path = cfg_dir / "threads-config.json"
        cfg_path.write_text(json.dumps({"enforce_pr_create_thread": True}), encoding="utf-8")
        env = dict(self.env, COGOS_THREADS_CONFIG=str(cfg_path))
        r = self._run_gate("gh pr create --title x --body y", env_extra=env)
        self.assertEqual(json.loads(r.stdout), {})

    def test_armed_but_corrupt_registry_fails_open(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        cfg_dir = Path(tempfile.mkdtemp())
        cfg_path = cfg_dir / "threads-config.json"
        cfg_path.write_text(json.dumps({"enforce_pr_create_thread": True}), encoding="utf-8")
        env = dict(self.env, COGOS_THREADS_CONFIG=str(cfg_path))
        r = self._run_gate("gh pr create --title x --body y", env_extra=env)
        self.assertEqual(json.loads(r.stdout), {})


    # ---------------------------------------------------------- F2 --
    # A bare substring match (`"gh pr create" in command`) flagged any
    # command that merely CONTAINS those three words, including as a
    # quoted argument to an unrelated command -- editing this feature's
    # own docs tripped the gate. These tests arm the gate with zero open
    # threads (the state under which a real `gh pr create` invocation
    # would be denied) so a false positive is observable as an incorrect
    # deny, and a false negative as an incorrect allow.

    def _armed_env(self):
        cfg_dir = Path(tempfile.mkdtemp())
        cfg_path = cfg_dir / "threads-config.json"
        cfg_path.write_text(json.dumps({"enforce_pr_create_thread": True}), encoding="utf-8")
        return dict(self.env, COGOS_THREADS_CONFIG=str(cfg_path))

    def test_gh_pr_create_inside_git_commit_message_is_not_denied(self):
        # Armed, zero open threads: a real `gh pr create` invocation would
        # be denied here. This isn't one -- the literal string only
        # appears inside a quoted -m argument to `git commit`.
        env = self._armed_env()
        r = self._run_gate(
            'git commit -m "docs: describe gh pr create gating"', env_extra=env
        )
        self.assertEqual(json.loads(r.stdout), {}, r.stdout)

    def test_gh_pr_create_inside_grep_pattern_is_not_denied(self):
        env = self._armed_env()
        r = self._run_gate('grep -rn "gh pr create" ./docs', env_extra=env)
        self.assertEqual(json.loads(r.stdout), {}, r.stdout)

    def test_plain_gh_pr_create_is_still_denied_when_armed(self):
        # The regression check for the fix above: an actual invocation
        # must still be caught, tokenized or not.
        env = self._armed_env()
        r = self._run_gate("gh pr create --title x --body y", env_extra=env)
        resp = json.loads(r.stdout)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gh_pr_create_after_shell_operator_is_still_denied(self):
        # A real invocation chained after `;`/`&&` must still be caught --
        # the tokenizer must not treat "not the very first segment" as
        # "not a real invocation".
        env = self._armed_env()
        r = self._run_gate("echo hi && gh pr create --title x", env_extra=env)
        resp = json.loads(r.stdout)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_variable_substitution_evasion_is_out_of_scope_and_allowed(self):
        # Documented limit, not a bug: this gate never runs a shell, so it
        # cannot know `$C` expands to "create". A warn-tier client-side
        # gate cannot close this without the side effects it exists to
        # avoid; see _split_commands()'s docstring.
        env = self._armed_env()
        r = self._run_gate("C=create; gh pr $C --title x", env_extra=env)
        self.assertEqual(json.loads(r.stdout), {}, r.stdout)


class TestGatePrCommandTokenizer(unittest.TestCase):
    """Unit-level tests directly against threads-gate-pr.py's tokenizer
    (_split_commands / _looks_like_gh_pr_create), independent of the
    enforcement config -- these exist so the parsing logic itself is
    pinned precisely, not just observed indirectly through allow/deny."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        gate_path = PLUGIN_ROOT / "hooks" / "threads-gate-pr.py"
        spec = importlib.util.spec_from_file_location("cogos_threads_gate_pr", gate_path)
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)

    def test_quoted_in_git_commit_is_not_an_invocation(self):
        self.assertFalse(
            self.gate._looks_like_gh_pr_create(
                'git commit -m "docs: describe gh pr create gating"'
            )
        )

    def test_quoted_in_grep_is_not_an_invocation(self):
        self.assertFalse(
            self.gate._looks_like_gh_pr_create('grep -rn "gh pr create" ./docs')
        )

    def test_plain_invocation_is_detected(self):
        self.assertTrue(
            self.gate._looks_like_gh_pr_create("gh pr create --title x --body y")
        )

    def test_invocation_after_semicolon_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("echo hi; gh pr create --title x"))

    def test_invocation_after_double_ampersand_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("ls -la && gh pr create --title x"))

    def test_invocation_after_pipe_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create('echo body | gh pr create --title x --body-file -'))

    def test_command_constructed_via_xargs_template_not_detected(self):
        # A real limit, not a target: xargs builds "gh pr create {}" as a
        # TEMPLATE it later substitutes into an argv -- the literal
        # segment's argv here starts with "xargs", not "gh". This is the
        # same class of limitation as variable-substitution evasion
        # (documented in _split_commands' docstring), not something this
        # tokenizer claims to catch.
        self.assertFalse(self.gate._looks_like_gh_pr_create("echo hi | xargs -I{} gh pr create {}"))

    def test_invocation_inside_command_substitution_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("FOO=$(gh pr create --title x)"))

    def test_invocation_quoted_as_a_single_string_is_not_an_invocation(self):
        self.assertFalse(self.gate._looks_like_gh_pr_create("echo 'gh pr create' | cat"))

    def test_variable_substitution_evasion_not_detected(self):
        # Documented limitation, asserted explicitly so a future change
        # doesn't silently start (or silently keep failing to) catch it
        # without anyone noticing either way.
        self.assertFalse(self.gate._looks_like_gh_pr_create("C=create; gh pr $C --title x"))

    def test_unbalanced_quotes_do_not_raise(self):
        # A segment shlex can't parse must be skipped, not raised -- this
        # gate's fail-open contract depends on parsing never taking down
        # the whole check.
        self.assertFalse(self.gate._looks_like_gh_pr_create('echo "unterminated'))

    # ---------------------------------------------------------- NEW-1 --
    # Delta-pass residuals (2026-08-07 independent review, second pass):
    # a leading env-assignment token or a fused subshell paren shifted
    # argv just enough to defeat the argv[:3] == ["gh","pr","create"]
    # compare -- for the env-assignment case in particular, this was a
    # COVERAGE REGRESSION relative to the OLD bare-substring match, which
    # caught `GH_TOKEN=x gh pr create` for free (it didn't care about
    # argv position at all). None of these are evasion; they're ordinary
    # usage.

    def test_leading_env_assignment_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("GH_TOKEN=x gh pr create --title x"))

    def test_multiple_leading_env_assignments_are_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("A=1 B=2 gh pr create --title x"))

    def test_single_ampersand_is_a_separator(self):
        # A lone `&` (backgrounding) must not fuse this segment to the
        # next one -- `gh pr create & sleep 1` is a real invocation
        # followed by an unrelated backgrounded command, not one long
        # non-matching argv.
        self.assertTrue(self.gate._looks_like_gh_pr_create("gh pr create --title x & sleep 1"))
        self.assertTrue(self.gate._looks_like_gh_pr_create("sleep 1 & gh pr create --title x"))

    def test_subshell_paren_fused_to_first_token_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("(gh pr create --title x)"))

    def test_subshell_paren_with_space_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("( gh pr create --title x )"))

    def test_subshell_paren_plus_env_assignment_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("(GH_TOKEN=x gh pr create --title x)"))

    def test_env_assignment_style_argument_value_is_not_stripped(self):
        # _strip_leading_noise only strips LEADING NAME=value tokens --
        # gh's own --field=value-style arguments (which never precede the
        # command name) must not be affected, and a false env-assignment
        # match must not accidentally consume part of a real invocation.
        self.assertTrue(self.gate._looks_like_gh_pr_create("gh pr create --title=x"))

    def test_backtick_substitution_not_treated_as_a_boundary(self):
        # Documented limit: backticks aren't special-cased the way $( is.
        # Asserted explicitly (like the variable-substitution case above)
        # so a future change doesn't silently start or silently keep
        # failing to catch it without anyone noticing either way.
        self.assertFalse(self.gate._looks_like_gh_pr_create("echo `gh pr create --title x`"))

    # ---------------------------------------------------------- NEW-5 --
    # Delta-pass residual, second pass (2026-08-07 independent review):
    # a trailing `)` fused to the LAST of the first three tokens defeated
    # the subshell strip -- `_strip_leading_noise()` only ever handled
    # the OPENING `(`. The no-trailing-flags form of each subshell case
    # above (i.e. nothing after `create` for the `)` to land on instead)
    # is exactly where this bites: `create)` != `create`.

    def test_subshell_paren_no_flags_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("(gh pr create)"))

    def test_command_substitution_no_flags_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("FOO=$(gh pr create)"))

    def test_subshell_paren_plus_env_assignment_no_flags_is_detected(self):
        self.assertTrue(self.gate._looks_like_gh_pr_create("(GH_TOKEN=x gh pr create)"))

    def test_trailing_paren_stripping_does_not_reintroduce_false_positives(self):
        # Regression guard: rstrip(")") on the first three tokens must
        # not turn an unrelated command into a false match, and the
        # documented-limit cases from the sections above must stay
        # exactly as documented (still not caught).
        self.assertFalse(self.gate._looks_like_gh_pr_create('git commit -m "docs: gh pr create)"'))
        self.assertFalse(self.gate._looks_like_gh_pr_create("echo hi | xargs -I{} gh pr create {}"))
        self.assertFalse(self.gate._looks_like_gh_pr_create("echo `gh pr create`"))
        self.assertFalse(self.gate._looks_like_gh_pr_create("C=create; gh pr $C"))


class TestGatePrArmedDeniesNewFalseNegatives(TempState):
    """Subprocess-level companions to the NEW-1 unit tests above: the
    full gate (armed, zero open threads) must actually DENY these real
    invocations, not just have the tokenizer function return True in
    isolation."""

    def setUp(self):
        super().setUp()
        cfg_dir = Path(tempfile.mkdtemp())
        cfg_path = cfg_dir / "threads-config.json"
        cfg_path.write_text(json.dumps({"enforce_pr_create_thread": True}), encoding="utf-8")
        self.env["COGOS_THREADS_CONFIG"] = str(cfg_path)

    def _run_gate(self, command):
        gate = PLUGIN_ROOT / "hooks" / "threads-gate-pr.py"
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        env = dict(os.environ)
        env.update(self.env)
        return subprocess.run(
            [sys.executable, str(gate)], capture_output=True, text=True,
            timeout=10, env=env, input=json.dumps(payload),
        )

    def _assert_denied(self, command):
        r = self._run_gate(command)
        resp = json.loads(r.stdout)
        self.assertEqual(
            resp.get("hookSpecificOutput", {}).get("permissionDecision"), "deny",
            f"expected deny for {command!r}, got {r.stdout}",
        )

    def test_leading_env_assignment_denied_when_armed(self):
        self._assert_denied("GH_TOKEN=x gh pr create --title x --body y")

    def test_ampersand_backgrounded_invocation_denied_when_armed(self):
        self._assert_denied("gh pr create --title x --body y & sleep 1")

    def test_subshell_paren_invocation_denied_when_armed(self):
        self._assert_denied("(gh pr create --title x --body y)")

    # NEW-5 (2026-08-07 independent review, delta pass, second pass):
    # the exact three reproductions from the review, at the full-gate
    # (armed, zero threads) level, not just the tokenizer function.

    def test_subshell_paren_no_flags_denied_when_armed(self):
        self._assert_denied("(gh pr create)")

    def test_command_substitution_no_flags_denied_when_armed(self):
        self._assert_denied("FOO=$(gh pr create)")

    def test_subshell_paren_plus_env_assignment_no_flags_denied_when_armed(self):
        self._assert_denied("(GH_TOKEN=x gh pr create)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
