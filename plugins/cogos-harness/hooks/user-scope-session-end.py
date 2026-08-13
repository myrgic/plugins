#!/usr/bin/env python3
"""
User-scope SessionEnd hook — presence.ended dispatcher.

Companion to user-scope-session-start.py. Same cwd-detection and
skip-if-inside-cog-workspace logic. Delegates to the canonical
51-presence-ended.py handler under the workspace's own .cog/hooks tree.

Presence is registry-gated, not workspace-gated (#17), mirroring the
session-start fallback exactly: after any delegation, this hook asks the
registry whether the session is still live (GET /v1/sessions/presence
lists live, not-yet-ended sessions) and POSTs /v1/sessions/{id}/end --
the REST counterpart of cog_end_session -- when it is.

The gate is the registry, not handler-existence, for the same reason as
on the start side: 51-presence-ended.py emits a presence.ended BUS event
and never ends the registry row. Gating on "a handler ran" would let a
workspace that ships the end handler suppress this fallback and strand a
seat the start-side fallback registered -- registered, heartbeating, and
then never ended: a zombie. Asking the registry ends exactly the seats
that are actually still open, and no others.

Safety contract: never raises, always exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Same env precedence as user-scope-session-start.py / seat-identity-heal.py.
KERNEL_URL = os.environ.get("COGOS_KERNEL_URL") or \
    f"http://127.0.0.1:{os.environ.get('COGOS_KERNEL_PORT', '6931')}"
PROBE_TIMEOUT = 1.0
END_TIMEOUT = 1.5
GRANT_TIMEOUT = 1.0

# v0.16.29: kernel writes require an X-Cogos-Grant header. Resolved once per
# process and cached: vault file first, loopback grants/current GET as
# fallback. Any acquisition failure means "proceed without the header" --
# fail-open, same contract as the rest of this hook. A broken vault must
# never break session-end; the 401 that follows lands in the existing
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
    cwd = Path.cwd().resolve()

    try:
        cwd.relative_to(cog_ws)
        return True
    except ValueError:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2, cwd=str(cwd),
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip()).resolve()
            if common_dir == (cog_ws / ".git").resolve():
                return True
    except Exception:
        pass

    return False


def _read_stdin_bytes() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except Exception:
        return b"{}"


def _parse_hook_data(stdin_data: bytes) -> dict:
    try:
        data = json.loads(stdin_data.decode("utf-8", "ignore") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _session_is_live(session_id: str) -> bool:
    """True only when the kernel answers cleanly AND lists this session in
    GET /v1/sessions/presence (live, not-yet-ended sessions). Doubles as
    the reachability gate, replacing the old GET /health probe: an
    unreachable or non-200 registry returns False, so the caller does
    nothing. Same lookup as _registry_state() on the start side."""
    try:
        with urllib.request.urlopen(f"{KERNEL_URL}/v1/sessions/presence", timeout=PROBE_TIMEOUT) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read())
        for s in payload.get("sessions") or []:
            if isinstance(s, dict) and s.get("session_id") == session_id:
                return True
        return False
    except Exception:
        return False


def _end_direct(session_id: str) -> None:
    """POST /v1/sessions/{id}/end -- the REST counterpart of
    cog_end_session. Fire-and-forget, fail-open on any error (unknown
    session -> 404, already-ended -> 409, kernel down -> connection error
    -- all silently ignored, matching the rest of this hook's contract)."""
    headers = {"Content-Type": "application/json"}
    grant = _get_grant()
    if grant:
        headers["X-Cogos-Grant"] = grant
    req = urllib.request.Request(
        f"{KERNEL_URL}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/end",
        # "session_end_hook" is the fleet-wide end_reason vocabulary
        # (established by the settings.local.json session-awareness hook);
        # consumers key on it, so the plugin fallback must not fork it.
        data=json.dumps({"reason": "session_end_hook"}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=END_TIMEOUT) as r:
            r.read()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def _maybe_end_direct(hook_data: dict) -> None:
    """The #17 fallback: registry-gated, not handler-gated. Runs after any
    delegation has already completed (subprocess.run is synchronous), so
    if the workspace handler DID end the registry row, the lookup below
    sees that and this is a no-op. If it only emitted a bus event -- which
    is what the canonical handler actually does -- the seat is still live
    here and gets ended. Never raises."""
    try:
        session_id = str(hook_data.get("session_id") or "")
        if not session_id:
            return
        if not _session_is_live(session_id):
            return
        _end_direct(session_id)
    except Exception:
        pass


def main() -> int:
    stdin_data = _read_stdin_bytes()
    cog_ws = _find_cog_workspace()

    # Mirrors the start side: cwd-inside-workspace is the workspace hooks'
    # domain and this hook stays out of it. Delegation is preferred and
    # tried first but does not gate the fallback -- the registry does.
    workspace_handles_presence = False

    if cog_ws is not None:
        if _cwd_is_inside_cog(cog_ws):
            workspace_handles_presence = True
        else:
            handler = cog_ws / ".cog" / "hooks" / "session-end.d" / "51-presence-ended.py"
            if handler.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(handler)],
                        input=stdin_data,
                        capture_output=False,
                        timeout=4,
                    )
                except subprocess.TimeoutExpired:
                    sys.stderr.write("[user-scope-session-end] presence handler timed out\n")
                except Exception as e:
                    sys.stderr.write(f"[user-scope-session-end] handler exec failed: {e}\n")

    if not workspace_handles_presence:
        _maybe_end_direct(_parse_hook_data(stdin_data))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"[user-scope-session-end] unexpected error: {e}\n")
        sys.exit(0)
