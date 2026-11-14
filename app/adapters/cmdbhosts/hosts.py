"""CMDB Hosts adapter: servers are CIs in the ServiceNow CMDB. Host id = CI name."""
from __future__ import annotations

import importlib
import os
import re
import time

from app.ports.errors import NotFound, PortError, Unavailable
from app.ports.hosts import CAP_FIND_IN_TEXT
from app.ports.manifest import Manifest
from app.ports.models import Host

PROVIDER = "cmdb"
CAP_FIND_BY_NAME = "find_by_name"
MANIFEST = Manifest(port="hosts", provider=PROVIDER, title="ServiceNow CMDB",
                    capabilities=frozenset({CAP_FIND_IN_TEXT, CAP_FIND_BY_NAME}))
TTL = 300
_CACHE = {"t": 0.0, "rows": []}


def _cmdb():
    return importlib.import_module("app.tools.cmdb")


def _os_of(row: dict) -> str:
    txt = f"{row.get('sys_class_name') or ''} {row.get('os') or ''}".lower()
    if "windows" in txt or "win_server" in txt:
        return "windows"
    return "linux" if ("linux" in txt or "unix" in txt) else ""


def _rows(force: bool = False) -> list:
    if not force and _CACHE["rows"] and time.time() - _CACHE["t"] < TTL:
        return _CACHE["rows"]
    snow = importlib.import_module("app.tools.snow")
    ipf = os.getenv("CMDB_IP_FIELD", "ip_address")
    try:
        rows = snow._request("GET", os.getenv("CMDB_HOST_TABLE", "cmdb_ci_server"), params={
            "sysparm_query": "ORDERBYname", "sysparm_display_value": "false", "sysparm_limit": 1000,
            "sysparm_fields": f"sys_id,name,sys_class_name,os,operational_status,{ipf}"})
    except Exception as e:                                   # noqa: BLE001
        raise Unavailable(f"CMDB read failed: {str(e)[:150]}") from e
    _CACHE.update(t=time.time(), rows=rows)
    return rows


def _to_host(row: dict) -> Host:
    c = _cmdb()
    ip = c._usable_ip(str(row.get(os.getenv("CMDB_IP_FIELD", "ip_address")) or ""),
                      c._list("HOST_ADDRESS_ALLOWED_CIDRS", ""))
    up = str(row.get("operational_status") or "1") == "1"    # 1 = Operational
    return Host(id=row["name"], name=row["name"], os=_os_of(row), state="running" if up else "offline",
                address=ip, private_address=ip, provider=PROVIDER, extras={"sys_id": row.get("sys_id", "")})


def _tags():
    return importlib.import_module("app.tools.cmdb_tags")


class CmdbHosts:
    """managed_only=False sees every CI (used only for hints such as the OS of an unmanaged server)."""
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    def __init__(self, managed_only: bool = True):
        self.managed_only = managed_only

    def _managed_rows(self, force: bool = False) -> list:
        rows = _rows(force)
        if not self.managed_only:
            return rows
        try:
            cis = _tags().tagged_cis()
        except Exception as e:                               # noqa: BLE001
            raise Unavailable(str(e)[:200]) from e
        return rows if cis is None else [r for r in rows if _tags().ci_is_managed(r.get("sys_id", ""), cis)]

    def list_hosts(self, force: bool = False) -> list[Host]:
        return [_to_host(r) for r in self._managed_rows(force) if r.get("name")]

    def find_by_name(self, identifier: str) -> Host | None:
        ident = (identifier or "").strip().lower()
        ipf = os.getenv("CMDB_IP_FIELD", "ip_address")
        for r in self._managed_rows():
            match_targets = (
                str(r.get("name", "")).lower(), 
                str(r.get(ipf, "")).lower(), 
                str(r.get("sys_id", "")).lower()
            )
            if ident in match_targets:
                return _to_host(r)
        return None

    def get_host(self, host_id: str) -> Host:
        h = self.find_by_name(host_id)
        if h is None:
            _rows(force=True)
            h = self.find_by_name(host_id)
        if h is None:
            if self.managed_only and CmdbHosts(managed_only=False).find_by_name(host_id):
                raise NotFound(_tags().not_managed_message(host_id))
            raise NotFound(f"No CI named {host_id}")
        return h

    def find_in_text(self, text: str) -> Host | None:
        hits = {r["name"] for r in self._managed_rows() if r.get("name") and re.search(
            rf"(?<![A-Za-z0-9._-]){re.escape(r['name'])}(?![A-Za-z0-9._-])", text or "", re.IGNORECASE)}
        return self.find_by_name(hits.pop()) if len(hits) == 1 else None

    def find_unmanaged_in_text(self, text: str) -> Host | None:
        """A CI named in the text that exists but lacks the managed-server tag."""
        h = CmdbHosts(managed_only=False).find_in_text(text)
        return h if h and self.managed_only and self.find_by_name(h.name) is None else None

    def connect_address(self, host_id: str) -> str:
        h = self.get_host(host_id)
        if h.state != "running":
            raise Unavailable(f"CI {h.name} is not operational in the CMDB")
        ip = _cmdb().host_address(h.name)                    # fresh read, 60 s cache
        if not ip:
            raise Unavailable(f"CMDB record for {h.name} has no usable IP address")
        return ip