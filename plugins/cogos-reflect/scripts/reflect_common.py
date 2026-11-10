"""Shared helpers for cogos-reflect scripts. Stdlib only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SCHEMA_VERSION = "cogos-reflect/0.1"

# --------------------------------------------------------------------------- homes


def find_cog_workspace(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) to a dir holding .cog/ (a CogOS workspace)."""
    env = os.environ.get("COG_WORKSPACE")
    if env and (Path(env) / ".cog").is_dir():
        return Path(env)
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / ".cog").is_dir() and (d / ".cog" / "mem").is_dir():
            return d
    return None


def reflect_home() -> Path:
    """Where the ledger lives. Committed on a CogOS node, private otherwise."""
    env = os.environ.get("REFLECT_HOME")
    if env:
        return Path(env).expanduser()
    ws = find_cog_workspace()
    if ws:
        return ws / ".cog" / "reflect"
    return Path.home() / ".cogos-reflect"


def runs_home() -> Path:
    """Where run artifacts live (bundles carry quotes: never committed)."""
    env = os.environ.get("REFLECT_RUNS")
    if env:
        return Path(env).expanduser()
    ws = find_cog_workspace()
    if ws:
        return ws / ".cog" / "var" / "reflect" / "runs"
    return reflect_home() / "runs"


def new_run_dir(run_id: str | None = None) -> Path:
    rid = run_id or time.strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}", rid) or ".." in rid:
        die(f"run id must be a plain name (letters, digits, . _ -), got {rid!r}")
    d = runs_home() / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_run_dir() -> Path | None:
    r = runs_home()
    if not r.is_dir():
        return None
    dirs = sorted([d for d in r.iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime)
    return dirs[-1] if dirs else None


# --------------------------------------------------------------------------- time


def parse_since(s: str) -> float:
    """'30d' | '12h' | '2w' | ISO date -> epoch seconds lower bound."""
    s = s.strip()
    m = re.fullmatch(r"(\d+)([hdw])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return time.time() - n * {"h": 3600, "d": 86400, "w": 604800}[u]
    from datetime import datetime

    return datetime.fromisoformat(s).timestamp()


def iso_to_epoch(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts) / (1000.0 if ts > 1e12 else 1.0)
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_to_iso(t: float | None) -> str | None:
    if t is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# --------------------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b\w*(api[_-]?key|token|secret|password|passwd|pwd|auth)\w*\s*[=:]\s*\S{4,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}"),
    re.compile(r"(?<=\s-p)(?!\s)\S{4,}"),  # mysql -pSECRET
    re.compile(r"(?i)(?<=--password[= ])\S+"),
    re.compile(r"/mcp-connector/[A-Za-z0-9_\-]{8,}"),
    re.compile(r"/r/[A-Za-z0-9_\-]{16,}"),
]


def redact(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for p in _SECRET_PATTERNS:
        out = p.sub("[REDACTED]", out)
    return out


_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def redact_path(p: str | None) -> str:
    """Machine-local home dirs become ~ so artifacts don't carry usernames."""
    return _HOME.sub("~", redact(p or ""))


def quote(text: str | None, cap: int = 200) -> str:
    """A redacted, single-line, capped quote of operator/agent text."""
    t = redact_path(text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= cap else t[: cap - 1] + "…"


# --------------------------------------------------------------------------- text keys

_STOP = set(
    """a an the and or but if then else of to in on at by for with from as is are was were be been being it its
    this that these those i you we he she they me my your our us them do does did done not no yes so just
    please can could would should will shall may might must have has had there here what which who whom when
    where why how all any each every some such only own same than too very s t also into out up down over
    again once about let lets get got make made go going want need like ok okay sure yeah hey hi thanks thank
    one two now still even really actually right well im ive dont doesnt didnt cant wont youre thats its""".split()
)


def content_words(text: str) -> list[str]:
    # Redact BEFORE tokenizing: cluster keys and ledger titles are built from these words.
    words = re.findall(r"[a-z][a-z0-9_\-]{2,}", redact(text or "").lower())
    words = [w for w in words if not w.startswith("redacted") and not _TOKENISH.fullmatch(w)]
    return [stem(w) for w in words if w not in _STOP]


_TOKENISH = re.compile(r"(?=[a-z0-9_\-]*\d)[a-z0-9_\-]{20,}")  # long mixed tokens look like ids/secrets


def stem(w: str) -> str:
    """Crude suffix strip so routes/routed/routing compare equal. Good enough for overlap scoring."""
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def stable_id(*parts: str, prefix: str = "F") -> str:
    h = hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:10]
    return f"{prefix}-{h}"


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------- io


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)
