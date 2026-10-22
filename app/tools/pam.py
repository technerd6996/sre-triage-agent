"""Fetches host credentials from OpenBao. Used inside the tool layer only.
Never log, print, or return the values this module gives back."""
import io
import os

import hvac
import paramiko


def _client() -> hvac.Client:
    c = hvac.Client(url=os.environ["BAO_ADDR"], timeout=10)
    c.auth.approle.login(role_id=os.environ["BAO_ROLE_ID"],
                         secret_id=os.environ["BAO_SECRET_ID"])
    return c


def checkout(host: str) -> dict:
    """Return the secret stored at secret/pam/hosts/<host> (host is the IP)."""
    try:
        r = _client().secrets.kv.v2.read_secret_version(
            path=f"pam/hosts/{host}", mount_point="secret",
            raise_on_deleted_version=True)
        return r["data"]["data"]
    except Exception:
        # Deliberately generic: never echo secrets or server internals upward.
        raise RuntimeError(f"No credential available for {host}")


def load_pkey(text: str, passphrase: str | None = None):
    """Build a Paramiko key object from the PEM text stored in OpenBao."""
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase)
        except paramiko.SSHException:
            continue
    raise RuntimeError("Unsupported private key format")