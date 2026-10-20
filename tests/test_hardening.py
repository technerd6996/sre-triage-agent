"""Offline hardening tests: key guard, mojibake fixer, repo hygiene. Run: python -m pytest tests -q"""
import base64
import json
import re
from pathlib import Path

import pytest

from app.secret_check import BadSupabaseKey, key_kind, require_secret_key
from scripts.fix_mojibake import fix_text

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache"}


def _jwt(role):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS256'})}.{b({'role': role})}.sig"


def test_secret_keys_accepted():
    assert require_secret_key("sb_secret_abc123") == "sb_secret_abc123"
    assert require_secret_key(_jwt("service_role"))


@pytest.mark.parametrize("key", ["sb_publishable_abc", _jwt("anon")])
def test_public_keys_refused(key):
    with pytest.raises(BadSupabaseKey) as e:
        require_secret_key(key)
    assert key not in str(e.value)          # never echo the key


@pytest.mark.parametrize("key", ["", None, "   ", "not-a-key"])
def test_empty_or_unknown_refused(key):
    with pytest.raises(BadSupabaseKey):
        require_secret_key(key)


def test_key_kind():
    assert key_kind(_jwt("anon")) == "anon"
    assert key_kind("sb_publishable_x") == "publishable"


@pytest.mark.parametrize("bad,good", [
    ("Working\u00e2\u20ac\u00a6", "Working\u2026"),
    ("a \u00e2\u20ac\u201d b", "a \u2014 b"),
    ("Delete \u00e2\u20ac\u0153x.pdf\u00e2\u20ac\x9d?", "Delete \u201cx.pdf\u201d?"),
    ("\u00c2\u00b7 dot", "\u00b7 dot"),
    ("\u00e2\u2020\u2019 next", "\u2192 next"),
    ("\u00f0\u0178\u201d\u2019 Close", "\U0001F512 Close"),
    ("\u00f0\u0178\u203a\xa0 Report", "\U0001F6E0 Report"),
])
def test_mojibake_fixed(bad, good):
    assert fix_text(bad)[0] == good


def test_clean_text_untouched():
    s = "Plain text, em dash \u2014, arrow \u2192, lock \U0001F512, caf\u00e9.\r\n"
    assert fix_text(s) == (s, 0)


def test_fix_is_idempotent():
    once, _ = fix_text("a \u00e2\u20ac\u201d b")
    assert fix_text(once) == (once, 0)


def _source_files(*exts):
    for f in ROOT.rglob("*"):
        if f.is_file() and f.suffix in exts and not (set(f.parts) & SKIP) and not f.name.endswith(".bak"):
            yield f


def test_no_mojibake_left_in_repo():
    bad = []
    for f in _source_files(".py", ".js", ".html", ".css"):
        if f.name in ("test_hardening.py", "fix_mojibake.py"):
            continue
        if fix_text(f.read_text(encoding="utf-8", errors="replace"))[1]:
            bad.append(str(f.relative_to(ROOT)))
    assert not bad, f"run python -m scripts.fix_mojibake --write: {bad}"


def test_env_is_gitignored():
    gi = ROOT / ".gitignore"
    assert gi.exists(), ".gitignore missing"
    lines = {l.strip() for l in gi.read_text().splitlines()}
    assert ".env" in lines or ".env*" in lines


def test_no_hardcoded_secrets():
    pats = [re.compile(r"sb_secret_[A-Za-z0-9_\-]{10,}"), re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),
            re.compile(r"postgres(?:ql)?://[^:\s]+:[^@\s]{4,}@")]
    hits = []
    for f in _source_files(".py", ".js", ".html", ".md", ".txt", ".sql"):
        t = f.read_text(encoding="utf-8", errors="replace")
        hits += [f"{f.relative_to(ROOT)}: {p.pattern[:20]}" for p in pats if p.search(t)]
    assert not hits, hits