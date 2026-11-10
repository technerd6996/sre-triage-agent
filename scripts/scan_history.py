"""Finds secrets that are (or were) in this repository: every commit in git history, plus the working folder.

It reports WHERE a secret is and WHAT KIND it is. It never prints the value: the output is safe to paste into a ticket.
Use it for the credential rotation checklist (docs/CREDENTIAL-ROTATION.md): everything it lists must be rotated, because
a secret that was ever committed stays readable in history even after the file is deleted.

    .venv\\Scripts\\python.exe scripts\\scan_history.py            # history and working folder
    .venv\\Scripts\\python.exe scripts\\scan_history.py --json     # machine readable

Exit code 1 when anything is found, so it can be used as a check before a release."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (kind, pattern). Patterns match the secret itself; only the kind and the location are ever printed.
PATTERNS = (
    ("private key file (PEM)", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("PuTTY private key", re.compile(r"PuTTY-User-Key-File-\d")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}")),
    ("database address with a password", re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s$%{<][^@\s]{3,}@")),     # not a $variable or {placeholder}
    ("Supabase secret key", re.compile(r"\bsb_secret_[A-Za-z0-9_\-]{10,}")),
    ("JWT or service token", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}")),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("password assignment", re.compile(r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|token)\w*\s*[=:]\s*[\"']?[^\s\"'<>{}$]{8,}")),
)
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
# files whose "password = ..." lines are code or documentation, not secrets (the specific patterns above still apply to them)
CODE_SUFFIXES = {".py", ".js", ".html", ".md", ".ps1", ".sql", ".css", ".json", ".toml", ".yml", ".yaml", ".txt", ".bak"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, errors="replace").stdout


def kinds_in(text: str, path: str) -> list:
    code = Path(path).suffix.lower() in CODE_SUFFIXES and not path.replace("\\", "/").lower().startswith("creds/")
    out = []
    for kind, pat in PATTERNS:
        if kind == "password assignment" and code:
            continue                                          # too noisy in source code; real leaks there match a specific pattern
        if pat.search(text):
            out.append(kind)
    return out


def scan_history() -> list:
    """One row per (path, kind) with the commits where that file version held it."""
    found = {}
    seen_blobs = {}
    for commit in _git("rev-list", "--all").split():
        for line in _git("ls-tree", "-r", commit).splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            blob = parts[2]
            if blob not in seen_blobs:
                seen_blobs[blob] = kinds_in(_git("cat-file", "-p", blob), path)
            for kind in seen_blobs[blob]:
                found.setdefault((path, kind), []).append(commit[:7])
    tracked = set(_git("ls-files").splitlines())
    return [{"where": "git history", "path": p, "kind": k, "commits": len(c), "first_commit": c[-1], "last_commit": c[0],
             "still_tracked": p in tracked} for (p, k), c in sorted(found.items())]


def scan_folder() -> list:
    out = []
    for f in ROOT.rglob("*"):
        if not f.is_file() or SKIP_DIRS & set(f.relative_to(ROOT).parts) or f.stat().st_size > 2_000_000:
            continue
        rel = str(f.relative_to(ROOT))
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out += [{"where": "working folder", "path": rel, "kind": k} for k in kinds_in(text, rel)]
    return out


def main() -> int:
    rows = scan_history() + scan_folder()
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
        return 1 if rows else 0
    if not rows:
        print("Nothing found in git history or the working folder.")
        return 0
    print("Secrets found (values are never shown). Everything listed under 'git history' must be rotated.\n")
    for r in rows:
        extra = (f"  in {r['commits']} commit(s), {r['first_commit']}..{r['last_commit']}"
                 + ("  STILL TRACKED" if r["still_tracked"] else "  (file no longer tracked)")) if r["where"] == "git history" else ""
        print(f"  [{r['where']}] {r['path']}: {r['kind']}{extra}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
