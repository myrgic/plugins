"""Mutation check: each redaction / dedupe guard must be caught by the suite when removed.

Run from the plugin root: python3 tests/mutation_check.py
Copies the plugin to a temp dir, applies one mutation at a time, runs pytest there, restores.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTS = {
    "content_words without redact": ("scripts/reflect_common.py", 'redact(text or "").lower())', '(text or "").lower())'),
    "cwd without redact_path": ("scripts/reflect_collect.py", '"cwd": redact_path(s["cwd"])', '"cwd": s["cwd"]'),
    "claude adapter without call id": ("scripts/reflect_collect.py",
                                       'fam = add_call(s, x.get("name"), x.get("input"), ts, x.get("id"))',
                                       'fam = add_call(s, x.get("name"), x.get("input"), ts)'),
    "recur line without quote": ("scripts/reflect_recur.py", "best, best_line = f_, quote(lines[i], 200)",
                                 "best, best_line = f_, lines[i][:200]"),
    "quote without redact_path": ("scripts/reflect_common.py", 't = redact_path(text or "")', 't = text or ""'),
    "ledger fold without guard": ("scripts/reflect_ledger.py",
                                  'if not fid or r.get("op") not in ("add", "status") or (r.get("op") == "status" and r.get("to") not in STATUSES):',
                                  "if not fid:"),
    "ledger append without redaction": ("scripts/reflect_ledger.py", "            row[k] = quote(row[k], cap)", "            pass"),
    "ledger count without int guard": ("scripts/reflect_ledger.py", 'f["count"] = max(f["count"], _int(r.get("count")))',
                                       'f["count"] = max(f["count"], int(r.get("count") or 0))'),
    "error gist without quote": ("scripts/reflect_collect.py", "    return quote(t, 160)", "    return t[:160]"),
    "codex adapter without call id": ("scripts/reflect_collect.py",
                                      'ts, p.get("call_id"))\n                        if fam:',
                                      'ts)\n                        if fam:'),
    "hermes adapter without call id": ("scripts/reflect_collect.py",
                                       'fam = add_call(s, fn.get("name"), args if isinstance(args, dict) else {}, ts, cid)',
                                       'fam = add_call(s, fn.get("name"), args if isinstance(args, dict) else {}, ts)'),
    "bash_shape keeps attached flag value": ("scripts/reflect_collect.py", "                f = f[:2]  # -pSECRET", "                pass  # -pSECRET"),
    "norm_command keeps attached flag value": ("scripts/reflect_collect.py", 'r"\\1V", c)  # -pHunter2Secret', 'r"\\g<0>", c)  # -pHunter2Secret'),
    "SEEN_CALLS not reset per run": ("scripts/reflect_collect.py", "    SEEN_CALLS.clear()\n    if a.run", "    if a.run"),
}


def main() -> int:
    missed = 0
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "p"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        for name, (f, old, new) in MUTS.items():
            p = work / f
            src = p.read_text()
            if old not in src:
                print(f"STALE  {name}: anchor not found")
                missed += 1
                continue
            p.write_text(src.replace(old, new))
            r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-x", "-p", "no:cacheprovider"],
                               cwd=work, capture_output=True, text=True)
            ok = r.returncode != 0
            missed += not ok
            print(f"{'CAUGHT' if ok else 'MISSED'} {name}", flush=True)
            p.write_text(src)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
