"""Startup guard: refuse to run with a Supabase key that cannot do the server's job safely.

TRIAGE-1 needs the SECRET (service-role) key. A publishable/anon key either fails on
RLS-protected tables or, worse, tempts someone to switch RLS off. This check reads the
key's shape only; it never prints the key and never calls the network.
"""
import base64
import json


class BadSupabaseKey(RuntimeError):
    pass


def _jwt_role(key: str):
    parts = key.split(".")
    if len(parts) != 3:
        return None
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(body)).get("role")
    except Exception:
        return None


def key_kind(key: str) -> str:
    """Return 'secret', 'publishable', 'anon', 'service_role', 'empty' or 'unknown'."""
    key = (key or "").strip()
    if not key:
        return "empty"
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "publishable"
    role = _jwt_role(key)
    if role == "service_role":
        return "service_role"
    if role == "anon":
        return "anon"
    return "unknown"


def require_secret_key(key: str, name: str = "SUPABASE_KEY") -> str:
    """Return the key if it is a secret/service-role key, else raise BadSupabaseKey."""
    kind = key_kind(key)
    if kind in ("secret", "service_role"):
        return key.strip()
    if kind == "empty":
        raise BadSupabaseKey(f"{name} is not set in .env")
    if kind in ("publishable", "anon"):
        raise BadSupabaseKey(
            f"{name} holds a {kind} key. TRIAGE-1 needs the secret key "
            "(Supabase: Settings > API Keys). Never switch off RLS to make a public key work.")
    raise BadSupabaseKey(f"{name} is not a recognisable Supabase key (expected sb_secret_... or a service_role JWT)")