#!/usr/bin/env python3
"""
User-scope SessionStart hook — presence.started dispatcher.

Runs from ANY cwd. It locates the cog workspace, checks whether the current
cwd is already inside it (in which case the workspace-scope hooks already
fired and we skip to avoid double-emit), then delegates to the canonical
51-presence-started.py handler under the workspace's own .cog/hooks tree.

Presence is registry-gated, not workspace-gated (#17). Delegation to the
workspace handler stays preferred and is still tried first, but it no
longer decides whether this seat ends up in the kernel's session
registry — because handler-existence is not a proxy for registration.
The canonical 51-presence-started.py emits a presence.started event on
the kernel BUS; it never writes the session registry. Gating the
fallback on "a handler ran" therefore left the seat unregistered on
exactly the machines that have a cog workspace — the #17 symptom itself.

So after delegation this hook asks the registry directly (GET
/v1/sessions/presence, which lists live not-yet-ended sessions) and
registers via POST /v1/sessions/register — the exact REST counterpart of
cog_register_session — only when this session is genuinely absent from
it. Workspace info still ENRICHES a registration when available; its
absence no longer VETOES presence.

Checking presence rather than handler-existence also protects durable
seat roles: /v1/sessions/register is a full-row replace, not a field
merge (see seat-identity-heal.py), so an unconditional fallback would
overwrite role/extras on every resume or compact SessionStart. Skipping
an already-registered session makes the fallback additive only.

Safety contract:
  - NEVER raises. All errors are caught and exit 0 is returned.
  - Delegation path (workspace handler) is tried first, unchanged from
    before #17.
  - The direct-registration fallback fires only when cwd is outside the
    cog workspace AND the registry answers cleanly AND this session is
    not already registered. Both the lookup and the registration POST
    use bounded, short timeouts and fail open silently — an unreachable
    registry means "do nothing", never "retry" or "block".

Workspace location (in priority order):
  1. COGOS_WORKSPACE env var
  2. $HOME/workspaces/cog

Worktree awareness: a cwd that is a git worktree under the cog workspace
is considered "inside the cog workspace" and skips to avoid double-fire
from workspace hooks.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# COGOS_KERNEL_URL takes precedence when set; COGOS_KERNEL_PORT is a
# localhost-only convenience default. Same precedence as
# seat-identity-heal.py / kernel-vitals-probe.py so all hooks resolve to
# the same kernel.
KERNEL_URL = os.environ.get("COGOS_KERNEL_URL") or \
    f"http://127.0.0.1:{os.environ.get('COGOS_KERNEL_PORT', '6931')}"
PROBE_TIMEOUT = 1.0  # short, bounded probe -- never block the hook budget
REGISTER_TIMEOUT = 1.5
GRANT_TIMEOUT = 1.0

# v0.16.29: kernel writes require an X-Cogos-Grant header. Resolved once per
# process and cached: vault file first, loopback grants/current GET as
# fallback. Any acquisition failure means "proceed without the header" --
# fail-open, same contract as the rest of this hook. A broken vault must
# never break registration; the 401 that follows lands in the existing
# fire-and-forget error handling exactly as it did before this header
# existed.
_GRANT_CACHE: dict = {"tried": False, "token": None}


def _get_grant() -> str | None:
    if _GRANT_CACHE["tried"]:
        return _GRANT_CACHE["token"]
    _GRANT_CACHE["tried"] = True
    token = None
    try:
        raw = (Path.home() / ".cog" / "vault" / "node-root-grant").read_text(encoding="utf-8")
        token = raw.strip() or None
    except Exception:
        token = None
    if not token:
        try:
            with urllib.request.urlopen(
                f"{KERNEL_URL}/v1/identity/grants/current?surface=node-root",
                timeout=GRANT_TIMEOUT,
            ) as r:
                if r.status == 200:
                    token = (json.loads(r.read()).get("token")) or None
        except Exception:
            token = None
    _GRANT_CACHE["token"] = token
    return token


def _find_cog_workspace() -> Path | None:
    """Return the cog workspace root if it exists and has .cog/."""
    candidates = []
    env = os.environ.get("COGOS_WORKSPACE")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / "workspaces" / "cog")

    for c in candidates:
        try:
            if (c / ".cog").is_dir():
                return c.resolve()
        except OSError:
            continue
    return None


def _cwd_is_inside_cog(cog_ws: Path) -> bool:
    """
    Return True if the current working directory lives inside the cog
    workspace tree (direct or via a git worktree).

    Covers:
      - cwd == cog_ws
      - cwd is a subdirectory of cog_ws
      - cwd is a git worktree whose common gitdir is cog_ws/.git
    """
    cwd = Path.cwd().resolve()

    # Direct containment (including the workspace root itself).
    try:
        cwd.relative_to(cog_ws)
        return True
    except ValueError:
        pass

    # Worktree check: ask git for the common dir.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2, cwd=str(cwd),
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip()).resolve()
            # Common dir for a worktree is the main repo's .git directory.
            if common_dir == (cog_ws / ".git").resolve():
                return True
    except Exception:
        pass

    return False


def _read_stdin_bytes() -> bytes:
    """Read stdin safely, returning b'{}' on error."""
    try:
        return sys.stdin.buffer.read()
    except Exception:
        return b"{}"


def _parse_hook_data(stdin_data: bytes) -> dict:
    """Best-effort parse of the hook stdin JSON. Never raises; returns {}
    on any problem so callers can treat missing fields uniformly."""
    try:
        data = json.loads(stdin_data.decode("utf-8", "ignore") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _registry_state(session_id: str) -> str:
    """Ask the registry itself whether this session is already live.

    Returns one of:
      "present"     -- the session is in GET /v1/sessions/presence (which
                       lists live, not-yet-ended sessions), so SOMETHING
                       already registered it and the fallback must not
                       touch it.
      "absent"      -- kernel answered cleanly and this session is not in
                       the registry: the fallback is the only thing that
                       will put it there.
      "unreachable" -- kernel down / timeout / non-200 / unparseable. Fail
                       open: the caller does nothing.

    This single call replaces the old GET /health probe. It is both the
    reachability gate AND the ground truth for whether registration is
    needed, which matters because handler-existence is NOT a proxy for
    registration: a workspace's 51-presence-started.py emits a
    presence.started BUS event and never writes the session registry, so
    gating on "a handler ran" leaves the seat unregistered (#17) and
    gating on "no handler ran" would let a resume/compact SessionStart
    re-POST register over a durable role (register is a full-row replace,
    not a field merge -- see seat-identity-heal.py). Asking the registry
    is the only predicate that gets both cases right."""
    try:
        with urllib.request.urlopen(f"{KERNEL_URL}/v1/sessions/presence", timeout=PROBE_TIMEOUT) as r:
            if r.status != 200:
                return "unreachable"
            payload = json.loads(r.read())
        sessions = payload.get("sessions") or []
        for s in sessions:
            if isinstance(s, dict) and s.get("session_id") == session_id:
                return "present"
        return "absent"
    except Exception:
        return "unreachable"


def _register_direct(session_id: str, workspace: str, source: str = "") -> None:
    """POST /v1/sessions/register -- the exact REST counterpart of the
    cog_register_session MCP tool, same endpoint/env conventions as
    seat-identity-heal.py's _register(). Fire-and-forget: the response is
    read (to let the connection close cleanly) but never inspected --
    presence registration is best-effort by design, per #17.

    extras.source carries the harness's own SessionStart source value
    (startup | resume | clear | compact) so registry rows share one
    vocabulary regardless of which lifecycle path registered them;
    extras.via records that this row came from the plugin fallback."""
    body = {
        "session_id": session_id,
        "workspace": workspace,
        "role": os.environ.get("COGOS_SEAT_ROLE", "claude-code"),
        "hostname": socket.gethostname(),
        "extras": {"source": source or "startup", "via": "cogos-harness"},
    }
    headers = {"Content-Type": "application/json"}
    grant = _get_grant()
    if grant:
        headers["X-Cogos-Grant"] = grant
    req = urllib.request.Request(
        f"{KERNEL_URL}/v1/sessions/register",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REGISTER_TIMEOUT) as r:
            r.read()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def _maybe_register_direct(hook_data: dict) -> None:
    """The #17 fallback: registry-gated, not workspace-gated and not
    handler-gated. Runs after any delegation has already completed
    (subprocess.run is synchronous, so the workspace handler's effect --
    if it has one -- is visible by now). Registers only when a session_id
    is available AND the registry answers cleanly AND this session is not
    already in it. Consequences of that last clause:

      - a seat whose workspace handler only emitted a bus event still
        gets registered (the actual #17 symptom), and
      - a resume/compact SessionStart on an already-registered seat is a
        no-op, so a durable role is never clobbered by a full-row-replace
        register.

    Never raises."""
    try:
        session_id = str(hook_data.get("session_id") or "")
        if not session_id:
            return
        if _registry_state(session_id) != "absent":
            return
        workspace = str(hook_data.get("cwd") or os.getcwd())
        source = str(hook_data.get("source") or "")
        _register_direct(session_id, workspace, source)
    except Exception:
        pass


def main() -> int:
    stdin_data = _read_stdin_bytes()
    cog_ws = _find_cog_workspace()

    # workspace_handles_presence: cwd is inside the cog workspace tree, so
    # the WORKSPACE's own session-start hooks (a separate mechanism from
    # this delegator) already fire for this session -- and the
    # proprioception hook likewise stands down there, so this hook stays
    # out of that domain entirely rather than registering a seat nothing
    # here would heartbeat.
    #
    # Delegation to the workspace handler is still preferred and tried
    # first, but it is NOT a gate on the fallback: whether the fallback
    # fires is decided by the registry (see _maybe_register_direct), not
    # by whether a handler file happened to exist.
    workspace_handles_presence = False

    if cog_ws is not None:
        if _cwd_is_inside_cog(cog_ws):
            # Workspace-scope hooks already handle presence for this
            # session. Skip delegation to prevent double-emit.
            workspace_handles_presence = True
        else:
            handler = cog_ws / ".cog" / "hooks" / "session-start.d" / "51-presence-started.py"
            if handler.exists():
                # Delegate: re-exec the canonical handler with the same stdin.
                try:
                    subprocess.run(
                        [sys.executable, str(handler)],
                        input=stdin_data,
                        capture_output=False,  # pass stdout/stderr through unchanged
                        timeout=4,
                    )
                except subprocess.TimeoutExpired:
                    sys.stderr.write("[user-scope-session-start] presence handler timed out\n")
                except Exception as e:
                    sys.stderr.write(f"[user-scope-session-start] handler exec failed: {e}\n")

    if not workspace_handles_presence:
        _maybe_register_direct(_parse_hook_data(stdin_data))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"[user-scope-session-start] unexpected error: {e}\n")
        sys.exit(0)
