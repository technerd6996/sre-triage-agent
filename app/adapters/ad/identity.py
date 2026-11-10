"""Active Directory (LDAP) adapter."""
import os

from app import telemetry
import logging
from ssl import CERT_NONE, CERT_REQUIRED
from ldap3 import Server, Connection, ALL, SUBTREE, NTLM, SIMPLE, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.ports.errors import AuthFailed, Unavailable
from app.ports.manifest import Field, Manifest

PROVIDER = "ad"
MANIFEST = Manifest(
    port="identity",
    provider=PROVIDER,
    title="Active Directory",
    capabilities=frozenset({"authenticate", "groups", "search"}),
    fields=(
        Field("server", "LDAP Server (e.g., ldaps://EC2AMAZ-DKPOL3O)", required=True),
        Field("domain", "Domain (e.g., lab.local)", required=True),
        Field("search_base", "Search Base (e.g., DC=lab,DC=local)", required=True),
        Field("bind_user", "Service Account DN"),
        Field("bind_password", "Service Account Password", kind="secret"),
    ),
)


def _parse_server(raw: str) -> tuple[str, int, bool]:
    raw = (raw or "").strip() or "ldap://127.0.0.1"
    use_ssl = raw.lower().startswith("ldaps://")
    hostport = raw.split("://", 1)[-1].rstrip("/")
    host, _, port = hostport.partition(":")
    return host or "127.0.0.1", int(port) if port.isdigit() else (636 if use_ssl else 389), use_ssl


class ActiveDirectoryIdentity:
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    def _server(self) -> Server:
        host, port, use_ssl = _parse_server(os.getenv("IDENTITY_SERVER", ""))
        tls = None
        if use_ssl:
            # Lab default: encrypt but do not verify. Set IDENTITY_TLS_VERIFY=1 (and
            # IDENTITY_CA_FILE for a private CA) before production.
            if os.getenv("IDENTITY_TLS_VERIFY", "0") == "1":
                tls = Tls(validate=CERT_REQUIRED, ca_certs_file=os.getenv("IDENTITY_CA_FILE") or None)
            else:
                tls = Tls(validate=CERT_NONE)
        return Server(host, port=port, use_ssl=use_ssl, tls=tls, get_info=ALL)

    # What an AD bind failure sub-code means (the "data xxx" part of the server's message)
    _AD_CODES = {"525": "user not found", "52e": "wrong password", "530": "logon not permitted at this time",
                 "531": "not permitted from this workstation", "532": "password expired", "533": "account disabled",
                 "568": "too many context IDs", "701": "account expired", "773": "must change password at next logon",
                 "775": "account locked out"}

    def _bind(self, conn: Connection) -> Connection:
        """Binds and, on failure, raises with the directory's own reason (for example 'wrong password')."""
        if conn.bind():
            return conn
        msg = str((conn.result or {}).get("message", "") or (conn.result or {}).get("description", ""))
        code = ""
        if "data " in msg:
            code = msg.split("data ", 1)[1].split(",", 1)[0].strip().lower()
        why = self._AD_CODES.get(code)
        raise AuthFailed(f"bind refused ({(conn.result or {}).get('description', 'unknown')}"
                         + (f", AD code {code}: {why}" if why else (f", {msg[:120]}" if msg else "")) + ")")

    def _get_connection(self, user: str, password: str) -> Connection:
        domain = os.getenv("IDENTITY_DOMAIN", "").strip()
        _, _, use_ssl = _parse_server(os.getenv("IDENTITY_SERVER", ""))
        user_str = (user or "").strip()
        try:
            if use_ssl:
                # Simple bind is safe inside TLS and satisfies the DC's signing requirement.
                if "\\" not in user_str and "@" not in user_str and domain:
                    user_str = f"{user_str}@{domain}"
                return self._bind(Connection(self._server(), user=user_str, password=password,
                                             authentication=SIMPLE, auto_bind=False))

            # Plain LDAP: NTLM (only works while the DC does not require signing)
            netbios_domain = domain.split(".")[0].upper() if domain else "LAB"
            if "\\" in user_str:
                ntlm_user = user_str
            elif "@" in user_str:
                ntlm_user = f"{netbios_domain}\\{user_str.split('@')[0]}"
            else:
                ntlm_user = f"{netbios_domain}\\{user_str}"
            return self._bind(Connection(self._server(), user=ntlm_user, password=password,
                                         authentication=NTLM, auto_bind=False))
        except LDAPException as e:
            raise AuthFailed(f"LDAP Bind Failed: {str(e)}")

    def _service_connection(self) -> Connection:
        return self._get_connection(os.getenv("IDENTITY_BIND_USER"), os.getenv("IDENTITY_BIND_PASSWORD", ""))

    @telemetry.traced("ad", "authenticate")
    def authenticate(self, username: str, password: str) -> bool:
        domain = os.getenv("IDENTITY_DOMAIN", "")
        upn = f"{username}@{domain}" if "@" not in username else username
        # Accounts without a UserPrincipalName cannot bind as user@domain, so a failed UPN bind is retried once
        # as NETBIOS\sAMAccountName (the short name before the @). A wrong password fails both times.
        candidates = [upn]
        sam = username.split("@", 1)[0].strip()
        netbios = domain.split(".")[0].upper() if domain else ""
        if sam and netbios and "\\" not in username:
            candidates.append(f"{netbios}\\{sam}")
        last = None
        for who in candidates:
            try:
                conn = self._get_connection(who, password)
                conn.unbind()
                return True
            except (LDAPException, AuthFailed) as e:
                last = e
        logging.getLogger(__name__).warning("AD authenticate failed for %s (tried %d form(s)): %s",
                                            username, len(candidates), last)
        return False

    @staticmethod
    def _domain_root(search_base: str) -> str:
        """'OU=Triage-App,DC=lab,DC=local' -> 'DC=lab,DC=local' (the search base itself if it has no DC parts)."""
        parts = [p.strip() for p in (search_base or "").split(",") if p.strip().upper().startswith("DC=")]
        return ",".join(parts) or search_base

    @telemetry.traced("ad", "get_groups")
    def get_groups(self, username: str) -> list[str]:
        """Stable group IDs (objectGUID), including nested groups. Expects a bare sAMAccountName."""
        search_base = os.getenv("IDENTITY_SEARCH_BASE", "")
        conn = None
        try:
            conn = self._service_connection()
            # The user may sit outside the OU that holds the Triage groups, so find the account in the whole domain
            # (the DC=... part of the search base). Only groups under the search base are returned below.
            conn.search(self._domain_root(search_base), f"(sAMAccountName={escape_filter_chars(username)})",
                        search_scope=SUBTREE, attributes=["distinguishedName", "userAccountControl"])
            if not conn.entries:
                return []
            uac = conn.entries[0].userAccountControl.value
            if isinstance(uac, int) and uac & 2:      # ACCOUNTDISABLE
                return []
            user_dn = escape_filter_chars(conn.entries[0].entry_dn)
            conn.search(search_base, f"(member:1.2.840.113556.1.4.1941:={user_dn})",
                        search_scope=SUBTREE, attributes=["objectGUID", "name"])
            return [str(e.objectGUID) for e in conn.entries]
        except (LDAPException, AuthFailed) as e:
            raise Unavailable(f"Failed to fetch groups: {str(e)}")
        finally:
            if conn:
                conn.unbind()

    @telemetry.traced("ad", "search_groups")
    def search_groups(self, prefix: str) -> list[dict]:
        search_base = os.getenv("IDENTITY_SEARCH_BASE", "")
        conn = None
        try:
            conn = self._service_connection()
            conn.search(search_base, f"(&(objectCategory=Group)(name={escape_filter_chars(prefix)}*))",
                        search_scope=SUBTREE, attributes=["objectGUID", "name"])
            return [{"id": str(e.objectGUID), "name": str(e.name)} for e in conn.entries]
        except (LDAPException, AuthFailed) as e:
            raise Unavailable(f"Group search failed: {str(e)}")
        finally:
            if conn:
                conn.unbind()