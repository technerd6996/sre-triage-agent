"""Credentials adapter: AWS Secrets Manager. This is the lookup code that used to live in app/tools/pam.py.

Each host has one secret named  <prefix>/<key>  holding JSON, for example:
  Linux:   {"username": "ec2-user", "port": 22, "private_key": "-----BEGIN ...", "passphrase": ""}
  Windows: {"username": "Administrator", "password": "...", "port": 5985, "transport": "ntlm"}
<key> is the stable host id (the EC2 instance id) first, then the address as a fallback.
AWS credentials come from the standard chain: instance role, AWS_PROFILE, or the AWS_* variables.
Never log, print, or return the values this module reads, except to the caller of fetch().
"""
import json
import logging
import os
import threading

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.ports.errors import NotFound, Unavailable
from app.ports.manifest import Manifest

log = logging.getLogger("triage.pam")

PROVIDER = "awssm"
MANIFEST = Manifest(port="credentials", provider=PROVIDER, title="AWS Secrets Manager",
                    capabilities=frozenset({"fetch"}))

_lock = threading.Lock()
_sm = None


def _prefix() -> str:
    return os.getenv("PAM_SECRET_PREFIX", "triage/pam/hosts")


def _client():
    """One shared Secrets Manager client. Credentials are resolved lazily by boto3."""
    global _sm
    with _lock:
        if _sm is None:
            session = boto3.session.Session(region_name=os.getenv("AWS_REGION") or None)
            _sm = session.client("secretsmanager")
        return _sm


class AwsSecretsManager:
    provider = PROVIDER

    def __init__(self, **_):
        # Read-only unless PAM_MANAGE_IN_APP=1. Secrets are normally managed in AWS, not in this app.
        write = os.getenv("PAM_MANAGE_IN_APP", "0") == "1"
        self.capabilities = MANIFEST.capabilities if write else frozenset({"fetch", "list"})

    def fetch(self, *keys: str) -> tuple[str, dict]:
        tried = [k for k in keys if k]
        try:
            sm = _client()
            for key in tried:
                try:
                    r = sm.get_secret_value(SecretId=f"{_prefix()}/{key}")
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        continue                      # not stored under this key: try the next one
                    raise
                data = json.loads(r["SecretString"])
                if not isinstance(data, dict):
                    raise ValueError("secret is not a JSON object")
                return key, data
        except (ClientError, BotoCoreError, ValueError) as e:
            # The error type and AWS error code are safe to log. Secret values never are.
            code = e.response.get("Error", {}).get("Code", "") if isinstance(e, ClientError) else ""
            label = f"{type(e).__name__}{': ' + code if code else ''}"
            log.warning("credentials lookup failed: %s", label)
            raise Unavailable(label) from e
        # Key names are not secret, so they are safe to log. Values never are.
        log.warning("credentials: nothing stored under: %s", ", ".join(tried) or "(no key)")
        raise NotFound(", ".join(tried) or "(no key)")