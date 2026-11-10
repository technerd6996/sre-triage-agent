"""Credentials adapter: CyberArk Central Credential Provider (CCP), the REST route CyberArk offers for applications.

    GET <base>/AIMWebService/api/Accounts?AppID=...&Safe=...&Object=...

The app identifies itself to CCP with the Application ID and, where the customer requires it, a client certificate.
Each host is one account in the safe. The Object name comes from the template (default `{key}`), and <key> is what
TRIAGE-1 already looks up: the stable host id first, then the address.

The reply's Content is the secret. A value that looks like a private key is treated as an SSH key, anything else as a
password, and the result has the same shape the AWS adapter returns, so nothing above this port changes.
Never log, print or return the values this module reads, except to the caller of fetch().
"""
import logging

import requests

from app import config
from app.ports.errors import AuthFailed, NotFound, Unavailable
from app.ports.manifest import Field, Manifest

log = logging.getLogger("triage.pam")

PROVIDER = "cyberark"
MANIFEST = Manifest(port="credentials", provider=PROVIDER, title="CyberArk (Central Credential Provider)",
                    capabilities=frozenset({"fetch"}),
                    fields=(Field("ccp_url", "CCP address", required=True), Field("ccp_app_id", "Application ID", required=True),
                            Field("ccp_safe", "Safe", required=True)))

_g = "credentials"
config.register("credentials.ccp_url", "CyberArk CCP address", "", "text", _g, max_len=300, help="For example https://ccp.company.com. Used only when the provider is CyberArk.")
config.register("credentials.ccp_app_id", "CyberArk Application ID", "", "text", _g, max_len=200, help="The application defined for TRIAGE-1 in CyberArk.")
config.register("credentials.ccp_safe", "CyberArk safe", "", "text", _g, max_len=200, help="The safe that holds the server accounts.")
config.register("credentials.ccp_object_template", "CyberArk account name pattern", "{key}", "text", _g, max_len=200,
                help="How a server maps to an account's Object name. {key} is the host id or address.")
config.register("credentials.ccp_folder", "CyberArk folder", "Root", "text", _g, max_len=200)
config.register("credentials.ccp_client_cert", "Client certificate file (PEM)", "", "text", _g, max_len=400, help="Path on this server. Leave empty if CCP does not ask for a certificate.")
config.register("credentials.ccp_client_key", "Client certificate key file (PEM)", "", "text", _g, max_len=400)
config.register("credentials.ccp_ca_bundle", "CA bundle for CCP (PEM)", "", "text", _g, max_len=400, help="Path to your internal CA certificate. Empty uses the system's trusted list. Certificate checks are never switched off.")
config.register("credentials.ccp_timeout", "CCP timeout in seconds", 15, "int", _g, lo=1, hi=120)
config.register("credentials.ccp_linux_user", "SSH user when CyberArk does not return one", "", "text", _g, max_len=100)


def _to_secret(data: dict) -> dict:
    """CCP's reply in the shape the connection code expects."""
    content = data.get("Content") or ""
    out = {"username": data.get("UserName") or config.get("credentials.ccp_linux_user") or ""}
    if data.get("Port"):
        out["port"] = data["Port"]
    if "PRIVATE KEY" in content or content.startswith("PuTTY-User-Key-File"):
        out["private_key"] = content
    else:
        out["password"] = content
    return out


class CyberArkCredentials:
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    def __init__(self, **_):
        pass

    def _get(self, key: str):
        base = (config.get("credentials.ccp_url") or "").strip().rstrip("/")
        app_id, safe = config.get("credentials.ccp_app_id"), config.get("credentials.ccp_safe")
        if not (base and app_id and safe):
            raise Unavailable("CyberArk is not configured: CCP address, Application ID and safe are needed")
        if not base.lower().startswith("https://"):
            raise Unavailable("The CCP address must start with https://")
        cert, cert_key = config.get("credentials.ccp_client_cert"), config.get("credentials.ccp_client_key")
        return requests.get(
            f"{base}/AIMWebService/api/Accounts",
            params={"AppID": app_id, "Safe": safe, "Folder": config.get("credentials.ccp_folder") or "Root",
                    "Object": config.get("credentials.ccp_object_template").replace("{key}", key)},
            cert=((cert, cert_key) if cert_key else cert) if cert else None,
            verify=config.get("credentials.ccp_ca_bundle") or True,
            timeout=int(config.get("credentials.ccp_timeout")))

    def fetch(self, *keys: str) -> tuple[str, dict]:
        tried = [k for k in keys if k]
        for key in tried:
            try:
                r = self._get(key)
            except requests.RequestException as e:
                log.warning("CyberArk lookup failed: %s", type(e).__name__)           # the type only: never the reply
                raise Unavailable(type(e).__name__) from e
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError as e:
                    raise Unavailable("CCP answered with something that is not JSON") from e
                if not data.get("Content"):
                    raise Unavailable("CCP returned an account with no secret")
                return key, _to_secret(data)
            code = ""
            try:
                code = str(r.json().get("ErrorCode", ""))            # the code is safe; the message can name the account
            except ValueError:
                pass
            if r.status_code == 404 or code == "APPAP004E":          # no such account: try the next key
                continue
            if r.status_code in (401, 403):
                raise AuthFailed(f"CCP refused the application (HTTP {r.status_code} {code}). Check the Application ID, certificate and safe membership.")
            log.warning("CyberArk lookup failed: HTTP %s %s", r.status_code, code)
            raise Unavailable(f"CCP answered HTTP {r.status_code} {code}".strip())
        log.warning("credentials: nothing stored under: %s", ", ".join(tried) or "(no key)")
        raise NotFound(", ".join(tried) or "(no key)")
