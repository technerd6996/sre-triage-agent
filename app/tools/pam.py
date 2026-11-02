"""Credential checkout for the agent's tools. Used inside the tool layer only.
Never log, print, or return the values this module gives back.

The tools ask for a host's credential here. app/pam_service.py applies the rules (a live run,
rate limits, an audit row) and the active Credentials adapter does the lookup.
The helpers below turn a stored private key into something Paramiko can use."""
import io
import re
import textwrap

import paramiko


def checkout(*keys: str) -> dict:
    """The credential stored for the first key that has one.
    Pass the stable host id first, then the address as a fallback."""
    from app import pam_service            # lazy: keeps this module light to import
    return pam_service.checkout(*keys)


def checkout_for_inquiry(user_id: str, *keys: str) -> dict:
    """Read-only credential fetch with no agent run, used only by /api/approver-chat/inquire.
    Pass the stable host id first, then the address as a fallback."""
    from app import pam_service            # lazy: keeps this module light to import
    return pam_service.checkout_for_inquiry(user_id, *keys)


def _normalize_pem(text: str) -> str:
    """Rebuild a PEM key whose line breaks were lost or escaped when it was stored
    (everything on one line, literal \\n, or spaces). Encrypted legacy PEMs are left as is."""
    text = (text or "").strip().replace("\\r", "").replace("\\n", "\n")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        return text + "\n"
    label, body = m.group(1), m.group(2)
    if "ENCRYPTED" in body[:200] or "Proc-Type" in body:
        return text + "\n"
    body = re.sub(r"\s+", "", body)
    return f"-----BEGIN {label}-----\n" + "\n".join(textwrap.wrap(body, 64)) + f"\n-----END {label}-----\n"


def load_pkey(text: str, passphrase: str | None = None):
    """Build a Paramiko key object from the PEM text stored in the secret."""
    text = _normalize_pem(text)
    reasons = []
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase)
        except paramiko.PasswordRequiredException:
            raise RuntimeError("Private key is passphrase-protected but no passphrase is stored")
        except paramiko.SSHException as e:
            reasons.append(f"{cls.__name__}: {type(e).__name__}")
    raise RuntimeError("Unsupported private key format (" + "; ".join(reasons) + ")")