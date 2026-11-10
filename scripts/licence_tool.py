"""VENDOR TOOL: creates the signing key and issues licence keys. Customers never get this file's private key.

    python scripts\\licence_tool.py keygen  --out D:\\secure\\triage1-licence-private.pem
        Makes the Ed25519 key pair once. Writes the PRIVATE key to --out and prints the PUBLIC key, which goes into
        app/licence.py (PUBLIC_KEY). Keep the private key off every customer machine and out of the repository:
        whoever holds it can issue licences.

    python scripts\\licence_tool.py issue --key D:\\secure\\triage1-licence-private.pem --customer "Acme Ltd" --plan Team --itsm jira --days 365
        Prints a licence key. --servers / --seats / --tickets override the plan's numbers (0 = unlimited),
        --features a,b overrides its features, --days 0 never expires.

    python scripts\\licence_tool.py show --licence TRIAGE1-....
        Prints what a key contains, after checking its signature against the public key in app/licence.py."""
import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization                              # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey       # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def public_of(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def keygen(out: Path) -> str:
    if out.exists():
        raise SystemExit(f"{out} already exists. Refusing to overwrite a signing key: every licence issued with it would stop working.")
    private = Ed25519PrivateKey.generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return public_of(private)


def load_private(path: Path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def issue(private: Ed25519PrivateKey, customer: str, plan: str, days: int = 365, servers=None, seats=None, tickets=None,
          features=None, now: float = None, itsm: str = "servicenow") -> str:
    from app import licence
    if plan not in licence.PLANS:
        raise SystemExit(f"Unknown plan '{plan}'. Plans: {', '.join(licence.PLANS)}")
    if itsm not in licence.ITSM:
        raise SystemExit(f"Unknown ticketing tool '{itsm}'. Tools: {', '.join(licence.ITSM)}")
    base = licence.PLANS[plan]
    feats = base["features"] if features is None else features
    bad = [f for f in feats if f not in licence.FEATURES]
    if bad:
        raise SystemExit(f"Unknown feature(s): {', '.join(bad)}. Features: {', '.join(licence.FEATURES)}")
    now = now or time.time()
    payload = {"id": uuid.uuid4().hex[:12], "customer": customer, "plan": plan, "itsm": itsm,
               "servers": base["servers"] if servers is None else servers,
               "seats": base["seats"] if seats is None else seats,
               "tickets_month": base["tickets_month"] if tickets is None else tickets,
               "features": list(feats), "issued": int(now), "expires": int(now + days * 86400) if days else 0}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "TRIAGE1-" + _b64(raw) + "." + _b64(private.sign(raw))


def main() -> None:
    ap = argparse.ArgumentParser(description="TRIAGE-1 licence tool (vendor only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("keygen")
    g.add_argument("--out", required=True, type=Path)
    i = sub.add_parser("issue")
    i.add_argument("--key", required=True, type=Path)
    i.add_argument("--customer", required=True)
    i.add_argument("--plan", required=True)
    i.add_argument("--itsm", required=True, help="the one ticketing tool this key is for: servicenow, jira, bmc, manageengine, freshservice")
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--servers", type=int)
    i.add_argument("--seats", type=int)
    i.add_argument("--tickets", type=int)
    i.add_argument("--features")
    s = sub.add_parser("show")
    s.add_argument("--licence", required=True)
    a = ap.parse_args()
    if a.cmd == "keygen":
        public = keygen(a.out)
        print("Private key written to", a.out)
        print("PUBLIC_KEY =", json.dumps(public))
    elif a.cmd == "issue":
        feats = None if a.features is None else [f.strip() for f in a.features.split(",") if f.strip()]
        print(issue(load_private(a.key), a.customer, a.plan, a.days, a.servers, a.seats, a.tickets, feats, itsm=a.itsm))
    else:
        from app import licence
        print(json.dumps(licence.parse(a.licence), indent=1))


if __name__ == "__main__":
    main()
