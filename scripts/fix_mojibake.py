"""Repair UTF-8 text that was decoded as Windows-1252 (an em dash turning into three odd characters).

Dry run by default. Use --write to change files (a .bak copy is kept).
  python -m scripts.fix_mojibake            # report only
  python -m scripts.fix_mojibake --write    # apply
  python -m scripts.fix_mojibake path1 path2
Line endings and any BOM are preserved. Only runs that decode cleanly are replaced.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

EXTS = {".py", ".js", ".html", ".css", ".md", ".txt", ".sql"}
SKIP_DIRS = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache"}

_CONT = "\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013\u2014\u2018-\u201a\u201c-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122"
RUN = re.compile(f"[\u00c2-\u00f4][{_CONT}]+")


def _to_bytes(s: str):
    out = bytearray()
    for ch in s:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:      # 0x81, 0x8d, 0x8f, 0x90, 0x9d are undefined in cp1252
                out.append(ord(ch))
            else:
                return None
    return bytes(out)


def _fix_run(run: str) -> str:
    """Decode the longest prefix that is valid UTF-8; keep any leftover as is."""
    for end in range(len(run), 1, -1):
        b = _to_bytes(run[:end])
        if b is None:
            continue
        try:
            return b.decode("utf-8") + run[end:]
        except UnicodeDecodeError:
            continue
    return run


def fix_text(text: str):
    """Return (fixed_text, number_of_runs_changed)."""
    n = 0

    def sub(m):
        nonlocal n
        new = _fix_run(m.group(0))
        if new != m.group(0):
            n += 1
        return new
    return RUN.sub(sub, text), n


def iter_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
        else:
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in EXTS and not (set(f.parts) & SKIP_DIRS) and not f.name.endswith(".bak"):
                    yield f


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--write", action="store_true", help="apply changes (keeps .bak)")
    a = ap.parse_args(argv)
    total_files = total_runs = 0
    for f in iter_files(a.paths):
        raw = f.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            print(f"skip (not utf-8): {f}")
            continue
        fixed, n = fix_text(text)
        if not n:
            continue
        total_files += 1
        total_runs += n
        lines = sum(1 for x, y in zip(text.split("\n"), fixed.split("\n")) if x != y)
        print(f"{'FIXED' if a.write else 'would fix'}: {f}  ({n} runs, {lines} lines)")
        if a.write:
            shutil.copy2(f, f.with_name(f.name + ".bak"))
            f.write_bytes(fixed.encode("utf-8"))
    print(f"\n{total_runs} runs in {total_files} files" + ("" if a.write else "  (dry run; add --write to apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())