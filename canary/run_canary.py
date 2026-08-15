#!/usr/bin/env python3
"""
canary/run_canary.py — the CC membrane canary for the myrgic/plugins marketplace.

Installs cogos-harness@plugins from the REAL myrgic/plugins remote into a
fresh, throwaway HOME (own keychain, own OAuth-free plugin state, no
contact with the real seat's config), then executes every hook script from
the *installed cache path* — the copy Claude Code would actually run, not
the repo checkout — with fixture stdin, against both a kernel-present and a
kernel-absent COGOS_KERNEL_URL. It asserts exit 0 and the documented output
shape for each hook, checks that nothing escaped the sandbox HOME, runs
`claude plugin validate --strict` against the marketplace manifest, greps
the installed package for leaked personal paths/identifiers, and cleans up
any session it registered on the live kernel afterward.

Sibling of the Hermes canary: same JSON report + digest + exit-code
contract (see "Exit codes" below), this one's subject is the Claude Code
membrane instead of the Hermes agent runtime. `name` in the report is
"membrane".

Ground truth for the sandbox recipe: the fresh-HOME install pattern
documented in the sandboxed-cc-seat-pattern memory file. macOS has no
`timeout` binary — every subprocess call below uses Python's own
subprocess timeout, never a shelled-out `timeout`.

Usage:
    python3 canary/run_canary.py [--out PATH] [--keep-sandbox]

Exit codes -- the sibling's three-tier vocabulary, so a wrapper can triage
both canaries with one rule:

    0  clean   every check passed
    1  drift   upstream/marketplace moved in a way worth a human look, but
               nothing this canary asserts is broken. Reserved: no check
               currently emits it. It exists so a future drift-class signal
               can be added without colliding with "broken", and so exit 1
               never means the same thing here as a real regression.
    2  broken  at least one check UNDER TEST failed -- a real conformance
               regression in the membrane.
    3  setup   the instrument could not establish its own sandbox, so the
               checks under test never ran. This is the instrument failing,
               not the membrane failing, and it must never be reported as
               "broken": doing so trains the reader to ignore exit 2. A
               setup failure means the run produced NO EVIDENCE either way.

A JSON report is written to --out (default: canary/.last_run.json,
gitignored) and a human digest is printed to stdout. The report carries the
same code in report["exit_code"].
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

NAME = "membrane"

# Three-tier exit vocabulary, shared with the Hermes canary. See the module
# docstring: 1 is deliberately reserved for drift so that a real regression
# here and a benign upstream move there never share an exit code.
EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_BROKEN = 2
EXIT_SETUP = 3

# Sandbox-owned keychain basename. Deliberately not "login.keychain" -- macOS
# special-cases that name and refuses to unlock it with an empty password even
# under a sandbox HOME. See setup_keychain's docstring for the measurement.
SANDBOX_KEYCHAIN = "cc-membrane-canary.keychain"

MARKETPLACE_REPO = "myrgic/plugins"
PLUGIN_SPEC = "cogos-harness@plugins"
PLUGIN_NAME = "cogos-harness"

DEAD_KERNEL_URL = "http://127.0.0.1:1"  # reserved port, connection refused immediately
LIVE_KERNEL_PORT = os.environ.get("COGOS_KERNEL_PORT", "6931")
DEFAULT_KERNEL_URL = f"http://127.0.0.1:{LIVE_KERNEL_PORT}"

DEFAULT_HOOK_TIMEOUT = 12
HOOK_TIMEOUTS = {
    # gh calls with 12s timeouts x2 + launchctl/df/health -- give real headroom.
    "kernel-vitals-probe.py": 45,
}
CLI_TIMEOUT = 90

# Patterns that must never appear in an installed, publishable package.
FORBIDDEN_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9_.-]+"),
    re.compile(r"\bslowbro\b", re.I),
    re.compile(r"\bchazmaniandinkle\b", re.I),
    re.compile(r"\bchaz\s*dinkle\b", re.I),
    re.compile(r"\bdarkstar\b", re.I),
    # The bare compound "cog-workspace" is legitimate generic terminology
    # used throughout this codebase's own docstrings (e.g. "inside a
    # cog-workspace"); only the retired ~/cog-workspace symlink alias is a
    # personal-machine reference worth flagging.
    re.compile(r"~/cog-workspace\b", re.I),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd, env, cwd=None, input_bytes=None, timeout=DEFAULT_HOOK_TIMEOUT):
    """subprocess.run wrapper -- Python-level timeout, never a shelled-out `timeout`."""
    try:
        p = subprocess.run(
            cmd, env=env, cwd=cwd, input=input_bytes,
            capture_output=True, timeout=timeout,
        )
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"returncode": None, "stdout": e.stdout or b"", "stderr": e.stderr or b"", "timed_out": True}
    except FileNotFoundError as e:
        return {"returncode": None, "stdout": b"", "stderr": str(e).encode(), "timed_out": False}


def probe_kernel(url=DEFAULT_KERNEL_URL, timeout=1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sandbox HOME + keychain isolation
# ---------------------------------------------------------------------------

def setup_keychain(sandbox_home: Path, report: dict) -> None:
    """Own keychain under the sandbox HOME so `claude` never touches the real
    seat's keychain. Verifies the REAL default keychain (queried with the
    real, unoverridden environment) is unchanged before/after.

    The keychain is deliberately NOT named `login.keychain`. macOS
    special-cases that basename: `security unlock-keychain -p "" login.keychain`
    resolves against the real login keychain semantics and fails with "The user
    name or passphrase you entered is not correct" even when HOME points at a
    sandbox and the file was just created there with an empty password. That
    failure is an artifact of the NAME, not of the isolation -- it fired on
    every run from 2026-08-03 to 2026-08-15 while isolation itself was intact.

    Verified 2026-08-15 under an identical sandbox HOME:
        create+unlock  login.keychain              -> SecKeychainUnlock error
        create+unlock  cc-membrane-canary.keychain -> exit 0

    A sandbox-owned basename cannot collide with the special case, so the
    failure it produced is now unreachable rather than tolerated.
    """
    real_env = os.environ.copy()
    pre = run(["security", "default-keychain"], env=real_env, timeout=10)
    pre_default = pre["stdout"].decode(errors="replace").strip()

    kc_env = os.environ.copy()
    kc_env["HOME"] = str(sandbox_home)
    steps = [
        ["security", "create-keychain", "-p", "", SANDBOX_KEYCHAIN],
        ["security", "default-keychain", "-s", SANDBOX_KEYCHAIN],
        ["security", "unlock-keychain", "-p", "", SANDBOX_KEYCHAIN],
        ["security", "set-keychain-settings", SANDBOX_KEYCHAIN],
    ]
    errors = []
    for cmd in steps:
        r = run(cmd, env=kc_env, timeout=10)
        if r["returncode"] != 0:
            errors.append(f"{' '.join(cmd)}: {r['stderr'].decode(errors='replace').strip()}")

    post = run(["security", "default-keychain"], env=real_env, timeout=10)
    post_default = post["stdout"].decode(errors="replace").strip()

    ok = (pre_default == post_default) and not errors
    report["steps"].append({
        "name": "keychain_isolation", "ok": ok,
        "real_default_keychain_before": pre_default,
        "real_default_keychain_after": post_default,
        "errors": errors,
    })

    # A changed REAL default keychain is a genuine isolation breach -- that is
    # the membrane failing, and it belongs in failures (exit 2).
    if pre_default != post_default:
        report["failures"].append(
            f"real default keychain changed during sandbox setup: {pre_default!r} -> {post_default!r}"
        )
    # Errors building the instrument's OWN sandbox are the instrument failing.
    # They say nothing about the membrane, so they are tracked separately and
    # graded EXIT_SETUP -- never folded into failures, which would report
    # "broken" for a run that produced no evidence at all.
    if errors:
        report["setup_failures"].append(f"sandbox keychain setup had errors: {errors}")


def teardown_keychain(sandbox_home: Path) -> None:
    kc_path = sandbox_home / "Library" / "Keychains" / f"{SANDBOX_KEYCHAIN}-db"
    if kc_path.exists():
        run(["security", "delete-keychain", str(kc_path)],
            env={**os.environ, "HOME": str(sandbox_home)}, timeout=10)


def cli_env(sandbox_home: Path) -> dict:
    """Broad env for driving the real `claude` CLI (marketplace/install/validate
    subcommands) -- these aren't the safety boundary under test, so inheriting
    PATH/etc. from the real environment is fine. Only HOME is redirected."""
    e = os.environ.copy()
    e["HOME"] = str(sandbox_home)
    return e


def hook_env(sandbox_home: Path, cache_root: Path, data_dir: Path, kernel_url: str) -> dict:
    """Minimal, EXPLICIT env for hook invocation -- built from scratch, never
    os.environ.copy(). This is the actual safety boundary: none of the
    operator's real COGOS_WORKSPACE / MYRGIC_REPOS_ROOT / COGOS_KERNEL_PORT /
    COGOS_SEAT_ROLE env vars can leak in and redirect a hook at the real
    substrate, even if this canary process happens to be run from a shell
    where they're set."""
    return {
        "HOME": str(sandbox_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CLAUDE_PLUGIN_ROOT": str(cache_root),
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "COGOS_KERNEL_URL": kernel_url,
    }


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_plugin(sandbox_home: Path, work_dir: Path, report: dict) -> bool:
    env = cli_env(sandbox_home)

    r1 = run(["claude", "plugin", "marketplace", "add", MARKETPLACE_REPO],
             env=env, cwd=str(work_dir), timeout=CLI_TIMEOUT)
    ok1 = r1["returncode"] == 0
    report["steps"].append({
        "name": "marketplace_add", "ok": ok1,
        "stdout": r1["stdout"].decode(errors="replace")[-2000:],
        "stderr": r1["stderr"].decode(errors="replace")[-2000:],
    })
    if not ok1:
        report["failures"].append(f"claude plugin marketplace add {MARKETPLACE_REPO} failed")
        return False

    r2 = run(["claude", "plugin", "install", PLUGIN_SPEC],
             env=env, cwd=str(work_dir), timeout=CLI_TIMEOUT)
    ok2 = r2["returncode"] == 0
    report["steps"].append({
        "name": "plugin_install", "ok": ok2,
        "stdout": r2["stdout"].decode(errors="replace")[-2000:],
        "stderr": r2["stderr"].decode(errors="replace")[-2000:],
    })
    if not ok2:
        report["failures"].append(f"claude plugin install {PLUGIN_SPEC} failed")
        return False
    return True


def find_cache_root(sandbox_home: Path, report: dict):
    """Locate the INSTALLED cache copy of cogos-harness -- the path Claude
    Code actually dispatches hooks from -- and the CLAUDE_PLUGIN_DATA path
    the real harness would set for it (observed convention:
    ~/.claude/plugins/data/<plugin>-<marketplace>/)."""
    cache_dir = sandbox_home / ".claude" / "plugins" / "cache"
    candidates = sorted(
        p for p in cache_dir.glob(f"*/{PLUGIN_NAME}/*")
        if p.is_dir() and (p / ".claude-plugin" / "plugin.json").exists()
    )
    if not candidates:
        report["failures"].append("no installed cogos-harness cache directory found after install")
        return None

    cache_root = candidates[-1]
    plugin = cache_root.parent.name
    marketplace = cache_root.parent.parent.name
    version_dir = cache_root.name

    try:
        manifest = json.loads((cache_root / ".claude-plugin" / "plugin.json").read_text())
    except Exception as e:
        report["failures"].append(f"could not parse installed plugin.json: {e}")
        manifest = {}
    manifest_version = manifest.get("version")

    report["environment"]["plugin"] = {
        "name": plugin,
        "marketplace": marketplace,
        "cache_dir_version": version_dir,
        "manifest_version": manifest_version,
        "cache_root": str(cache_root),
    }
    if manifest_version != version_dir:
        report["failures"].append(
            f"cache dir version ({version_dir!r}) != plugin.json version ({manifest_version!r})"
        )

    data_dir = sandbox_home / ".claude" / "plugins" / "data" / f"{plugin}-{marketplace}"
    return cache_root, data_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def write_fixtures(work_dir: Path):
    empty_transcript = work_dir / "fixture-transcript-empty.jsonl"
    empty_transcript.write_text("")

    full_transcript = work_dir / "fixture-transcript-full.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {
            "content": "<system-reminder>noise the hook must filter out</system-reminder>"
        }}),
        json.dumps({"type": "user", "message": {
            "content": "Canary fixture: verify compaction-handoff surfaces operator-verbatim text."
        }}),
    ]
    full_transcript.write_text("\n".join(lines) + "\n")
    return empty_transcript, full_transcript


# ---------------------------------------------------------------------------
# Hook execution + assertion
# ---------------------------------------------------------------------------

def run_hook(hook_path: Path, stdin_obj, env: dict):
    payload = json.dumps(stdin_obj).encode() if stdin_obj is not None else b""
    timeout = HOOK_TIMEOUTS.get(hook_path.name, DEFAULT_HOOK_TIMEOUT)
    return run(["python3", str(hook_path)], env=env, input_bytes=payload, timeout=timeout)


def record_hook(report: dict, hook: str, cycle: str, variant: str, expect: dict, result: dict) -> bool:
    checks = []
    ok = True

    if result["timed_out"]:
        checks.append("TIMEOUT"); ok = False
    elif result["returncode"] != 0:
        checks.append(f"exit={result['returncode']} (expected 0)"); ok = False
    else:
        checks.append("exit=0")

    if result["stderr"]:
        checks.append(f"stderr non-empty ({len(result['stderr'])}b, expected none)"); ok = False

    stdout_text = result["stdout"].decode("utf-8", "replace")

    if expect.get("stdout_empty"):
        if stdout_text.strip():
            checks.append("expected silent stdout, got content"); ok = False
        else:
            checks.append("stdout empty (silent, as documented)")

    needle = expect.get("stdout_contains")
    if needle:
        if needle not in stdout_text:
            checks.append(f"missing expected substring {needle!r}"); ok = False
        else:
            checks.append(f"contains {needle!r}")

    needle = expect.get("stdout_not_contains")
    if needle:
        if needle in stdout_text:
            checks.append(f"unexpectedly contains {needle!r}"); ok = False

    want_event = expect.get("stdout_json_hook_event")
    if want_event:
        try:
            d = json.loads(stdout_text)
            hev = (d.get("hookSpecificOutput") or {}).get("hookEventName")
            if hev != want_event:
                checks.append(f"hookEventName={hev!r} expected {want_event!r}"); ok = False
            else:
                checks.append("hookSpecificOutput.hookEventName ok")
        except Exception as e:
            checks.append(f"stdout not valid JSON: {e}"); ok = False

    entry = {
        "hook": hook, "cycle": cycle, "variant": variant, "ok": ok,
        "returncode": result["returncode"], "timed_out": result["timed_out"],
        "checks": checks,
        "stdout_preview": stdout_text[:500],
        "stderr_preview": result["stderr"].decode("utf-8", "replace")[:500],
    }
    report["hook_results"].append(entry)
    if not ok:
        report["failures"].append(f"{hook} [{cycle}/{variant}]: {'; '.join(checks)}")
    return ok


def run_cycle(cycle_name, kernel_url, sandbox_home, work_dir, cache_root, data_dir,
              report, empty_transcript, full_transcript) -> str:
    """Run every hook in the installed cache against one kernel-reachability
    condition, in the order the real harness would fire them within a
    session (start -> ... -> end), so session-end naturally cleans up
    whatever session-start registered."""
    sid = str(uuid.uuid4())
    hooks_dir = cache_root / "hooks"
    env = hook_env(sandbox_home, cache_root, data_dir, kernel_url)

    # 1. SessionStart(*) -- presence dispatcher. Documented contract: never
    # prints, side effects only (registry POST when kernel reachable and
    # session absent).
    r = run_hook(hooks_dir / "user-scope-session-start.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "SessionStart",
        "source": "startup", "transcript_path": str(empty_transcript),
    }, env)
    record_hook(report, "user-scope-session-start.py", cycle_name, "startup", {"stdout_empty": True}, r)

    # 2. SessionStart(*) -- seat-identity-heal. No identity file in a fresh
    # sandbox HOME -> documented as a fully silent no-op.
    r = run_hook(hooks_dir / "seat-identity-heal.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "SessionStart", "source": "startup",
    }, env)
    record_hook(report, "seat-identity-heal.py", cycle_name, "no-identity-file", {"stdout_empty": True}, r)

    # 3a. SessionStart(compact), no transcript/in-flight/uncommitted work to
    # report -> silent (nothing to promise the resumed session).
    r = run_hook(hooks_dir / "compaction-handoff.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "SessionStart", "source": "compact",
    }, env)
    record_hook(report, "compaction-handoff.py", cycle_name, "compact-no-transcript", {"stdout_empty": True}, r)

    # 3b. SessionStart(compact) with a fixture transcript carrying one noise
    # line and one operator line -- exercises the NOISE_MARKERS filter and
    # the verbatim re-injection path.
    r = run_hook(hooks_dir / "compaction-handoff.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "SessionStart", "source": "compact",
        "transcript_path": str(full_transcript),
    }, env)
    record_hook(report, "compaction-handoff.py", cycle_name, "compact-with-transcript", {
        "stdout_contains": "<compaction_handoff>",
        "stdout_not_contains": "<system-reminder>",
    }, r)

    # 4. kernel-vitals-probe.py -- not registered in hooks.json (it's fired
    # detached by the proprioception hook below), but it IS one of the
    # membrane's hook scripts and ships in hooks/, so it gets its own direct
    # pass here rather than only an indirect, racy exercise via Popen.
    r = run_hook(hooks_dir / "kernel-vitals-probe.py", None, env)
    record_hook(report, "kernel-vitals-probe.py", cycle_name, "detached-probe", {"stdout_empty": True}, r)

    vitals_cache = data_dir / ".kernel-vitals.json"
    vit = {}
    if vitals_cache.exists():
        try:
            vit = json.loads(vitals_cache.read_text())
        except Exception:
            pass
    reachable_expected = (cycle_name == "kernel-present")
    reachable_actual = bool((vit.get("kernel") or {}).get("reachable"))
    vitals_ok = reachable_actual == reachable_expected
    report["steps"].append({
        "name": f"vitals_cache_{cycle_name}", "ok": vitals_ok,
        "expected_reachable": reachable_expected, "actual_reachable": reachable_actual,
    })
    if not vitals_ok:
        report["failures"].append(
            f"kernel-vitals-probe cache shows reachable={reachable_actual}, "
            f"expected {reachable_expected} in {cycle_name}"
        )

    # 5. UserPromptSubmit -- the one hook documented to ALWAYS emit
    # structured output (a <cogos_proprioception> block) outside a cog
    # workspace, regardless of kernel reachability. Kept distinct from the
    # "silent" contract of the other hooks rather than folded into it.
    r = run_hook(hooks_dir / "user-scope-proprioception.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "UserPromptSubmit",
        "prompt": "canary fixture prompt", "transcript_path": str(full_transcript),
    }, env)
    record_hook(report, "user-scope-proprioception.py", cycle_name, "prompt", {
        "stdout_contains": "<cogos_proprioception>",
        "stdout_json_hook_event": "UserPromptSubmit",
    }, r)

    # 6. SessionEnd(*) -- runs LAST in this cycle by construction, so it
    # naturally ends whatever session-start registered (kernel-present) or
    # no-ops cleanly (kernel-absent). registry_hygiene() below re-verifies.
    r = run_hook(hooks_dir / "user-scope-session-end.py", {
        "session_id": sid, "cwd": str(work_dir), "hook_event_name": "SessionEnd", "reason": "other",
    }, env)
    record_hook(report, "user-scope-session-end.py", cycle_name, "end", {"stdout_empty": True}, r)

    return sid


# ---------------------------------------------------------------------------
# Registry hygiene (kernel-present cycle only)
# ---------------------------------------------------------------------------

def registry_presence(kernel_url: str, session_id: str, timeout=2.0):
    """None = registry unreachable/unparseable (unknown, not a failure)."""
    try:
        with urllib.request.urlopen(f"{kernel_url}/v1/sessions/presence", timeout=timeout) as r:
            payload = json.loads(r.read())
        sessions = payload.get("sessions") or []
        return any(isinstance(s, dict) and s.get("session_id") == session_id for s in sessions)
    except Exception:
        return None


def end_session_direct(kernel_url: str, session_id: str, timeout=2.0) -> bool:
    """POST /v1/sessions/{id}/end directly -- the fallback cleanup path if
    the sibling user-scope-session-end.py hook (already run once per cycle,
    see run_cycle step 6) somehow left the session live. Uses the SAME
    fleet-wide end_reason vocabulary ("session_end_hook") that hook sends,
    established by the settings.local.json session-awareness hook and
    documented in user-scope-session-end.py -- not a bespoke canary reason."""
    req = urllib.request.Request(
        f"{kernel_url}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/end",
        data=json.dumps({"reason": "session_end_hook"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def registry_hygiene(report: dict, kernel_url: str, session_id: str) -> None:
    present = registry_presence(kernel_url, session_id)
    hygiene = {"session_id": session_id, "present_after_hook_end": present, "fallback_end_invoked": False}

    if present is True:
        end_session_direct(kernel_url, session_id)
        hygiene["fallback_end_invoked"] = True
        present_after_fallback = registry_presence(kernel_url, session_id)
        hygiene["present_after_fallback"] = present_after_fallback
        hygiene["ok"] = present_after_fallback is False
        if present_after_fallback is not False:
            report["failures"].append(
                f"registry hygiene: session {session_id} still present after fallback /end "
                f"(present_after_fallback={present_after_fallback})"
            )
    elif present is None:
        hygiene["ok"] = True  # registry unreachable at verification time -- not this canary's fault
        hygiene["note"] = "registry unreachable at hygiene check; nothing further to clean up"
    else:
        hygiene["ok"] = True  # the sibling session-end hook already cleaned it up, as designed

    report["registry_hygiene"] = hygiene


# ---------------------------------------------------------------------------
# validate --strict / scrub-grep / filesystem safety
# ---------------------------------------------------------------------------

def validate_marketplace(repo_root: Path, sandbox_home: Path, report: dict) -> None:
    env = cli_env(sandbox_home)
    r = run(["claude", "plugin", "validate", "--strict", str(repo_root)], env=env, timeout=CLI_TIMEOUT)
    ok = r["returncode"] == 0
    report["steps"].append({
        "name": "validate_strict", "ok": ok,
        "stdout": r["stdout"].decode(errors="replace"),
        "stderr": r["stderr"].decode(errors="replace"),
    })
    if not ok:
        report["failures"].append("claude plugin validate --strict failed on the marketplace root")


def scrub_grep(cache_root: Path):
    matches = []
    for p in cache_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        for pat in FORBIDDEN_PATTERNS:
            for m in pat.finditer(text):
                matches.append({
                    "file": str(p.relative_to(cache_root)),
                    "pattern": pat.pattern,
                    "match": m.group(0),
                })
    return matches


def snapshot_real_host():
    """Uses the canary process's OWN (real, unoverridden) environment --
    every subprocess call elsewhere in this script gets an explicit HOME
    override, but this function deliberately does not, because it exists
    to check the real host was never touched."""
    real_home = Path.home()
    snap = {}
    for rel in (".cog", "workspaces/cog/.cog/state/watermarks"):
        p = real_home / rel
        if not p.exists():
            snap[str(p)] = "absent"
            continue
        try:
            count = sum(1 for _ in p.rglob("*")) if p.is_dir() else None
            snap[str(p)] = (round(p.stat().st_mtime, 3), count)
        except Exception as e:
            snap[str(p)] = f"error: {e}"
    return snap


def check_real_host_untouched(before, after, report: dict) -> None:
    ok = before == after
    report["steps"].append({
        "name": "real_host_untouched", "ok": ok,
        "before": before, "after": after,
    })
    if not ok:
        report["failures"].append(f"real host filesystem changed during canary run: before={before} after={after}")


def check_no_fabricated_workspace(sandbox_home: Path, report: dict) -> None:
    """kernel-vitals-probe.py explicitly guards against manufacturing a cog
    workspace where none exists (see its _write_disk_watermark docstring):
    other hooks detect "cog workspace present" via (workspace/".cog").is_dir(),
    so an unconditional mkdir would fool every sibling hook. Assert that
    invariant held for real, across both cycles."""
    fake_cog = sandbox_home / "workspaces" / "cog" / ".cog"
    ok = not fake_cog.exists()
    report["steps"].append({"name": "no_fabricated_cog_workspace", "ok": ok, "checked_path": str(fake_cog)})
    if not ok:
        report["failures"].append(f"a hook fabricated a cog workspace directory at {fake_cog}")


# ---------------------------------------------------------------------------
# Report + digest
# ---------------------------------------------------------------------------

def write_report(report: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))


def build_digest(report: dict) -> str:
    lines = []
    status = "PASS" if report.get("ok") else "FAIL"
    lines.append(f"membrane canary -- {status} -- {report.get('finished_at', '')}")

    env = report.get("environment", {})
    plugin = env.get("plugin", {})
    lines.append(
        f"plugin: {plugin.get('name', '?')}@{plugin.get('manifest_version', '?')} "
        f"(marketplace: {plugin.get('marketplace', '?')}) . claude cli {env.get('claude_cli_version', '?')}"
    )
    lines.append(f"kernel reachable at start: {env.get('kernel_reachable_at_start')}")

    by_hook = {}
    for r in report.get("hook_results", []):
        by_hook.setdefault(r["hook"], []).append(r)
    for hook, results in by_hook.items():
        mark = "OK  " if all(r["ok"] for r in results) else "FAIL"
        marks = " ".join(f"{r['cycle']}/{r['variant']}={'ok' if r['ok'] else 'FAIL'}" for r in results)
        lines.append(f"  {mark}  {hook:<32} {marks}")

    for s in report.get("steps", []):
        if s.get("name") == "validate_strict":
            lines.append(f"validate --strict: {'PASS' if s['ok'] else 'FAIL'}")

    # report["scrub"] is None when the run aborted before the scrub could
    # execute (e.g. marketplace add / install failed). Say so explicitly --
    # printing "0 MATCH(ES)" for a check that never ran reads as "the leak
    # guard passed", which is the one thing a canary must never fabricate.
    scrub = report.get("scrub")
    if scrub is None:
        lines.append("scrub-grep: NOT RUN (aborted before the package was installed)")
    elif scrub.get("clean"):
        lines.append("scrub-grep: CLEAN")
    else:
        lines.append(f"scrub-grep: {len(scrub.get('matches', []))} MATCH(ES)")

    rh = report.get("registry_hygiene")
    if rh:
        lines.append(f"registry hygiene: session={rh['session_id'][:8]}... ok={rh['ok']}")

    if report.get("failures"):
        lines.append("failures:")
        for f in report["failures"]:
            lines.append(f"  - {f}")

    if report.get("setup_failures"):
        lines.append("")
        lines.append("setup failures (INSTRUMENT, not the membrane -- checks under test did not run):")
        for f in report["setup_failures"]:
            lines.append(f"  - {f}")

    lines.append("")
    lines.append(
        {
            EXIT_CLEAN: "RESULT: clean",
            EXIT_DRIFT: "RESULT: drift",
            EXIT_BROKEN: "RESULT: broken",
            EXIT_SETUP: "RESULT: setup-failed (instrument could not build its sandbox; "
                        "no evidence either way about the membrane)",
        }[report.get("exit_code", EXIT_BROKEN)]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CC membrane canary for myrgic/plugins (cogos-harness).")
    ap.add_argument("--out", default=None, help="path to write the JSON report (default: canary/.last_run.json)")
    ap.add_argument("--keep-sandbox", action="store_true", help="don't delete the sandbox HOME on exit (debugging)")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    out_path = Path(args.out) if args.out else script_dir / ".last_run.json"

    report = {
        "name": NAME,
        "started_at": now_iso(),
        "environment": {},
        "steps": [],
        "hook_results": [],
        "registry_hygiene": None,
        "scrub": None,
        "failures": [],
        # Instrument-side failures: the sandbox could not be built, so the
        # checks under test never ran. Kept separate from "failures" so a
        # broken instrument can never be reported as a broken membrane.
        "setup_failures": [],
    }

    real_host_before = snapshot_real_host()

    sandbox_home = Path(tempfile.mkdtemp(prefix="cc-membrane-canary-"))
    work_dir = sandbox_home / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    report["environment"]["sandbox_home"] = str(sandbox_home)

    try:
        v = run(["claude", "--version"], env=os.environ.copy(), timeout=15)
        report["environment"]["claude_cli_version"] = v["stdout"].decode(errors="replace").strip()

        setup_keychain(sandbox_home, report)

        if install_plugin(sandbox_home, work_dir, report):
            found = find_cache_root(sandbox_home, report)
            if found:
                cache_root, data_dir = found
                data_dir.mkdir(parents=True, exist_ok=True)

                empty_transcript, full_transcript = write_fixtures(work_dir)

                run_cycle("kernel-absent", DEAD_KERNEL_URL, sandbox_home, work_dir,
                          cache_root, data_dir, report, empty_transcript, full_transcript)

                kernel_reachable = probe_kernel()
                report["environment"]["kernel_reachable_at_start"] = kernel_reachable

                if kernel_reachable:
                    sid = run_cycle("kernel-present", DEFAULT_KERNEL_URL, sandbox_home, work_dir,
                                     cache_root, data_dir, report, empty_transcript, full_transcript)
                    registry_hygiene(report, DEFAULT_KERNEL_URL, sid)
                else:
                    report["steps"].append({
                        "name": "kernel_present_cycle", "ok": True, "skipped": True,
                        "reason": f"kernel not reachable at 127.0.0.1:{LIVE_KERNEL_PORT} when the canary started",
                    })

                validate_marketplace(repo_root, sandbox_home, report)

                matches = scrub_grep(cache_root)
                report["scrub"] = {"clean": len(matches) == 0, "matches": matches}
                if matches:
                    report["failures"].append(
                        f"scrub-grep found {len(matches)} leaked path/identifier match(es) in the installed package"
                    )

                check_no_fabricated_workspace(sandbox_home, report)
    except Exception as e:
        report["failures"].append(f"unexpected exception: {type(e).__name__}: {e}")
    finally:
        teardown_keychain(sandbox_home)
        real_host_after = snapshot_real_host()
        check_real_host_untouched(real_host_before, real_host_after, report)
        if not args.keep_sandbox:
            shutil.rmtree(sandbox_home, ignore_errors=True)

    report["finished_at"] = now_iso()
    report["ok"] = len(report["failures"]) == 0

    # Grading order matters. A real conformance failure outranks a setup
    # failure: if a check under test actually failed, the reader must see
    # "broken" even if the sandbox was also imperfect. Only when nothing
    # under test failed does a setup failure decide the code -- that is the
    # case where the run produced no evidence at all.
    if report["failures"]:
        report["exit_code"] = EXIT_BROKEN
    elif report["setup_failures"]:
        report["exit_code"] = EXIT_SETUP
    else:
        report["exit_code"] = EXIT_CLEAN

    write_report(report, out_path)
    digest = build_digest(report)
    print(digest)
    print(f"\nfull report: {out_path}")

    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
