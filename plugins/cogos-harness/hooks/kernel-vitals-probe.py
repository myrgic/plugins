#!/usr/bin/env python3
"""
kernel-vitals-probe.py — out-of-band substrate-vitals collector.

Fired DETACHED (non-blocking) by user-scope-proprioception.py when its cache
is stale. Does the slow work (curl /health, tail the kernel log, `gh` calls)
off the prompt-submit path and writes a small cache the in-path hook reads.
Never blocks a prompt; the hook itself never makes a network call.

Cache: ${CLAUDE_PLUGIN_DATA}/.kernel-vitals.json (falls back to
~/.claude/hooks/.kernel-vitals.json when run outside a plugin install, e.g.
during local development).

Safety: never raises; writes whatever fields it could gather; partial is
fine. Every external call (kernel HTTP, launchctl, gh, disk/log reads)
degrades independently to an empty/absent field on failure or absence —
this probe never assumes any of them exist.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or (Path.home() / ".claude" / "hooks"))
CACHE = _DATA_DIR / ".kernel-vitals.json"
# COGOS_KERNEL_URL, when set, is the single source of truth for the kernel
# endpoint — same precedence as seat-identity-heal.py, so the two hooks
# never point at different kernels. COGOS_KERNEL_PORT remains a
# localhost-only convenience default for anyone who hasn't moved off
# 127.0.0.1. PORT is kept for display (the "UNREACHABLE (:PORT)" line).
KERNEL_URL = os.environ.get("COGOS_KERNEL_URL") or \
    f"http://127.0.0.1:{os.environ.get('COGOS_KERNEL_PORT', '6931')}"
try:
    PORT = urllib.parse.urlparse(KERNEL_URL).port or 6931
except Exception:
    PORT = 6931
REPO = os.environ.get("COGOS_KERNEL_REPO", "myrgic/cogos")
LABEL = "com.cogos.kernel"
DISK_FREE_THRESHOLD_GB = 25.0


def _workspace() -> Path:
    env = os.environ.get("COGOS_WORKSPACE")
    if env:
        return Path(env)
    return Path.home() / "workspaces" / "cog"


def _health() -> dict:
    """GET /health (2s timeout). Returns {reachable, version, status, state}."""
    out = {"reachable": False}
    try:
        with urllib.request.urlopen(f"{KERNEL_URL}/health", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        out["reachable"] = True
        out["version"] = d.get("version", "")
        out["status"] = d.get("status", "")
        out["state"] = d.get("state", "")
        node = d.get("node")
        if isinstance(node, dict):
            down = [k for k, v in node.items() if v not in ("healthy", "ok", "up", None)]
            out["node_down"] = down
    except Exception:
        pass
    return out


def _log_counts() -> dict:
    """Count ERROR + reconcile anomalies in the current boot window of the kernel
    JSONL log. Cheap: read the tail, slice from the last 'kernel booted'."""
    out = {"errors": 0, "anomalies": 0, "anomaly_kinds": [], "boot_ts": "", "window": ""}
    try:
        log = _workspace() / ".cog" / "run" / "kernel.log.jsonl"
        if not log.exists():
            return out
        size = log.stat().st_size
        # Backscan in steps until the boot marker is inside the read window.
        # A fixed tail silently degrades to an arbitrary-window count on long
        # uptimes (boot line ages out of the tail), which then looks like a
        # draining backlog as old lines slide out. Bound the scan so a huge
        # log can't make the probe expensive; if the marker still isn't
        # found, declare the window honestly instead of implying since-boot.
        lines = []
        start = 0
        found_boot = False
        for span in (1_500_000, 4_000_000, 10_000_000, 24_000_000):
            with open(log, "rb") as f:
                f.seek(max(0, size - span))
                tail = f.read().decode("utf-8", "ignore")
            lines = tail.splitlines()
            for i in range(len(lines) - 1, -1, -1):
                if '"kernel booted"' in lines[i]:
                    start = i
                    found_boot = True
                    try:
                        out["boot_ts"] = json.loads(lines[i]).get("time", "")[11:16]
                    except Exception:
                        pass
                    break
            if found_boot or span >= size:
                break
        if found_boot:
            out["window"] = "boot"
        else:
            # Honest fallback: report the span actually counted.
            try:
                w0 = json.loads(lines[0]).get("time", "")[:16]
            except Exception:
                w0 = "?"
            out["window"] = f"tail since {w0}"
        errors = 0
        raised = 0
        cleared = 0
        kinds = set()
        for ln in lines[start:]:
            if '"level":"ERROR"' in ln:
                errors += 1
            if "provider anomaly" in ln:
                # The kernel emits two lifecycle lines per episode — WARN
                # "reconcile: provider anomaly" (raised) and INFO "... anomaly
                # cleared" — and both contain this substring. Count net-open,
                # not lines, or every self-healed episode displays as 2.
                if "provider anomaly cleared" in ln:
                    cleared += 1
                else:
                    raised += 1
                    try:
                        rs = json.loads(ln).get("reasons") or []
                        for r in rs:
                            kinds.add(r)
                    except Exception:
                        pass
        out["errors"] = errors
        out["anomalies"] = max(0, raised - cleared)
        out["anomaly_episodes"] = raised
        out["anomaly_kinds"] = sorted(kinds)
    except Exception:
        pass
    return out


def _uptime() -> str:
    """Kernel process uptime via launchctl pid + `ps -o etime`. macOS only;
    fails open (empty string) on any other platform or if the service isn't
    managed by launchd."""
    try:
        p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True, text=True, timeout=3)
        pid = ""
        for ln in p.stdout.splitlines():
            if "pid =" in ln:
                pid = ln.split("=", 1)[1].strip()
                break
        if not pid:
            return ""
        e = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                          capture_output=True, text=True, timeout=3)
        return e.stdout.strip()
    except Exception:
        return ""


def _gh_json(args: list[str]):
    try:
        r = subprocess.run(["gh", *args, "--repo", REPO],
                          capture_output=True, text=True, timeout=12)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _release(running_version: str) -> dict:
    out = {}
    d = _gh_json(["release", "view", "--json", "tagName,isPrerelease"])
    if isinstance(d, dict):
        latest = d.get("tagName", "")
        out["latest"] = latest

        def norm(v):
            return (v or "").lstrip("v")
        out["update_available"] = bool(
            latest and running_version and norm(latest) != norm(running_version)
        )
    return out


def _prs() -> dict:
    out = {}
    d = _gh_json(["pr", "list", "--state", "open", "--json", "number"])
    if isinstance(d, list):
        out["open"] = len(d)
    return out


def _parse_ts(s):
    """Best-effort ISO8601 -> tz-aware datetime; None on any failure."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    for cand in (t, re.sub(r"\.\d+", "", t)):
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None


def _watermarks() -> dict:
    """Reflexive-observation watermarks. Reads per-source watermark records,
    if a watermark-writing stage has started producing them, and surfaces
    staleness as an ambient vital.

    Fail-open, by design: this whole primitive is additive on top of a
    directory that may not exist yet, may be mid-write, or may contain
    malformed/partial records. Any failure here must render identically to
    "no watermark dir at all" -- never raise, never block, never degrade
    the rest of the vitals line.

    Expected shape (one JSON file per source under WATERMARK_DIR):
    {"source": str, "cursor": ..., "content_hash": str, "last_success":
    iso8601 str, "observed_at": iso8601 str}. Any file that doesn't parse as
    that shape is silently skipped, not counted as stale or fresh -- absence
    of signal, not a false alarm.
    """
    out: dict = {}
    try:
        wdir = _workspace() / ".cog" / "state" / "watermarks"
        if not wdir.is_dir():
            return out
        now = datetime.now(timezone.utc)
        stale_after = 172800  # 48h -- generous; daily-cadence sources should
        # never trip this under normal operation, only on real breakage.
        stale = []
        fresh_count = 0
        for f in sorted(wdir.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
                if not isinstance(rec, dict):
                    continue
                source = rec.get("source") or f.stem
                observed_at = rec.get("observed_at")
                if not observed_at:
                    continue
                ts = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (now - ts).total_seconds()
                if age > stale_after:
                    stale.append(source)
                else:
                    fresh_count += 1
            except Exception:
                # One malformed/partial watermark file must never break the
                # scan of the rest, or the probe run as a whole.
                continue
        if stale or fresh_count:
            out["sources_ok"] = fresh_count
            if stale:
                out["stale_sources"] = stale
    except Exception:
        pass
    return out


def _external_prs() -> dict:
    """External (non-org) PR lifecycle sensor. Reads a cache JSON a
    site-local refresher script may write under
    .cog/state/watermarks/external-prs.json -- NEVER calls `gh` here. This
    probe just reads whatever watermark that refresher (if any) left behind,
    same division of labor as _watermarks(). Absent file = "no data",
    silently.

    Cache shape (external-prs.json): {"written_at": iso8601, "open_count":
    int, "stale_count": int, "stale_after_days": int, "prs": [...]}.
    """
    out: dict = {}
    try:
        cache = _workspace() / ".cog" / "state" / "watermarks" / "external-prs.json"
        if not cache.exists():
            return out
        rec = json.loads(cache.read_text())
        if not isinstance(rec, dict):
            return out
        if "open_count" in rec:
            out["open"] = rec["open_count"]
        if "stale_count" in rec:
            out["stale"] = rec["stale_count"]
        if "written_at" in rec:
            out["written_at"] = rec["written_at"]
    except Exception:
        pass
    return out


def _disk_free_gb(threshold_gb: float = DISK_FREE_THRESHOLD_GB) -> dict:
    """Internal-SSD free space, cheapest reliable call. Prefers `df` on the
    data volume (fast, always available). macOS-shaped (/System/Volumes/Data);
    fails open to {} on any other layout so it never blocks the rest of the
    vitals line.
    """
    out: dict = {}
    try:
        r = subprocess.run(["df", "-k", "/System/Volumes/Data"],
                           capture_output=True, text=True, timeout=3)
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return out
        fields = lines[-1].split()
        # df -k: Filesystem 1024-blocks Used Available Capacity ... Mounted-on
        avail_kb = float(fields[3])
        free_gb = avail_kb / (1024 * 1024)
        out["free_gb"] = round(free_gb, 1)
        out["threshold_gb"] = threshold_gb
        out["low"] = free_gb < threshold_gb
    except Exception:
        return {}
    return out


def _write_disk_watermark(disk: dict) -> None:
    """Watermark-style record so a downstream observatory sees internal-SSD
    free space like any other source (per the per-source watermark
    convention under .cog/state/watermarks/). Best-effort only -- a failure
    here must never affect the vitals cache or the formatted line."""
    if not disk:
        return
    try:
        ws = _workspace()
        if not (ws / ".cog").is_dir():
            # Never fabricate a cog workspace on a machine that has none —
            # sibling hooks detect "cog workspace present" via this exact
            # (workspace / ".cog").is_dir() predicate, so an unconditional
            # mkdir here would manufacture that condition and cause every
            # other hook to believe a substrate exists where it doesn't.
            return
        wdir = ws / ".cog" / "state" / "watermarks"
        wdir.mkdir(parents=True, exist_ok=True)
        rec = {
            "source": "internal-ssd",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "free_gb": disk.get("free_gb"),
            "threshold_gb": disk.get("threshold_gb"),
        }
        (wdir / "internal-ssd.json").write_text(json.dumps(rec, indent=2))
    except Exception:
        pass


def _self_update_cfg() -> dict:
    out = {}
    try:
        cfg = _workspace() / ".cog" / "config" / "self-update.yaml"
        if not cfg.exists():
            out["enabled"] = False
            return out
        for ln in cfg.read_text().splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ":" not in ln:
                continue
            k, v = (x.strip() for x in ln.split(":", 1))
            if k in ("enabled", "auto_apply"):
                out[k] = v.lower() == "true"
            elif k in ("channel", "pin"):
                out[k] = v
    except Exception:
        pass
    return out


def _join_truncated(items: list[str], limit: int) -> str:
    """Comma-join whole items up to `limit` chars, never a partial item.
    Was `",".join(items)[:24]`, which truncates the JOINED string and can
    slice a kind name mid-word (observed: "over_budget,quarantined" -> "qua").
    Stops BEFORE any item that would blow the budget instead."""
    out: list[str] = []
    length = 0
    for item in items:
        add = len(item) + (1 if out else 0)  # +1 for the joining comma
        if length + add > limit:
            break
        out.append(item)
        length += add
    return ",".join(out)


def _format_line(vit: dict) -> str:
    """The single source of truth for the vitals one-liner. Both the
    proprioception hook and a statusline (if any) read vit["line"] — neither
    re-formats. Problems (errors, anomalies, unreachable, update) are loud."""
    k = vit.get("kernel", {})
    if not k.get("reachable"):
        return f"cogos ⚠ UNREACHABLE (:{PORT})"
    log = vit.get("log", {})
    errs, anoms = log.get("errors", 0), log.get("anomalies", 0)
    e = f"⚠{errs}err" if errs else "0err"
    if anoms:
        kinds = _join_truncated(log.get("anomaly_kinds", []), 24)
        a = f"⚠{anoms}anom" + (f"({kinds})" if kinds else "")
        win = log.get("window", "")
        if win and win != "boot":
            a += f"[{win}]"  # count window ≠ since-boot; say so
    else:
        a = "0anom"
    core = f"cogos {k.get('version', '?')} {k.get('status', '?')} · {e} {a}"
    up = (k.get("uptime") or "").rsplit(":", 1)[0]  # trim trailing :SS
    if up:
        core += f" · up {up}"
    parts = [core]
    rel = vit.get("release", {})
    if rel.get("update_available"):
        parts.append(f"release {rel.get('latest', '?')} ⬆ UPDATE AVAILABLE")
    elif rel.get("latest"):
        parts.append(f"release {rel['latest']} (current)")
    su = vit.get("self_update", {})
    if su:
        if not su.get("enabled"):
            parts.append("self-update off")
        else:
            parts.append(f"self-update {'auto' if su.get('auto_apply') else 'notify'}/{su.get('pin') or su.get('channel', 'stable')}")
    prs = vit.get("prs", {})
    if "open" in prs:
        parts.append(f"PRs {prs['open']}")
    ext = vit.get("external_prs", {})
    if "open" in ext:
        extp = f"extPR {ext['open']}"
        if ext.get("stale"):
            extp += f"·{ext['stale']}stale"
        parts.append(extp)
    wm = vit.get("watermarks", {})
    stale = wm.get("stale_sources")
    if stale:
        names = ",".join(stale)[:40]
        parts.append(f"⚠ stale:{names}")
    disk = vit.get("disk", {})
    if disk.get("low"):
        parts.append(f"⚠disk {disk.get('free_gb', '?')}G")
    return " | ".join(parts)


def main() -> int:
    health = _health()
    vit = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kernel": health,
        "log": _log_counts(),
        "uptime": _uptime(),
        "self_update": _self_update_cfg(),
        "watermarks": _watermarks(),
        "disk": _disk_free_gb(),
        "external_prs": _external_prs(),
    }
    vit["kernel"]["uptime"] = vit.pop("uptime")
    # gh calls only when the kernel is reachable (skip noise when nothing's running).
    if health.get("reachable"):
        vit["release"] = _release(health.get("version", ""))
        vit["prs"] = _prs()
    vit["line"] = _format_line(vit)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(vit))
    except Exception:
        pass
    _write_disk_watermark(vit.get("disk", {}))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
