"""Which servers TRIAGE-1 manages: the CIs that carry a ServiceNow tag (setting cmdb.managed_tag).

A ServiceNow tag is a `label` row; each tagged record is a `label_entry` row (table_key = the record's sys_id).
An EC2 instance is managed when a tagged CI has its instance id in asset_tag, or its EC2 Name as the CI name.
If ServiceNow cannot be read, the last list read is used; with none read yet the lookup fails (nothing is managed)."""
from __future__ import annotations

import logging
import threading
import time

from app import config

log = logging.getLogger("triage")

_lock = threading.Lock()
_CACHE: dict = {"tag": None, "at": 0.0, "cis": None, "error": ""}


NOT_MANAGED = "is not managed by TRIAGE-1 App"       # the agent escalates with this wording


class TagsUnreadable(RuntimeError):
    """The managed-server tag is set but its CIs could not be read from ServiceNow, and no earlier list exists."""


def managed_tag() -> str:
    """The ServiceNow tag name, or '' when every server is managed. Read at call time."""
    return (config.get("cmdb.managed_tag") or "").strip()


def _snow():
    from app.tools import snow
    return snow


def _read(tag: str) -> list[dict]:
    """Tagged CIs as [{"sys_id", "name", "asset_tag"}]. Several tags may share a name (one per owner): all count.
    label_entry is queried by the tag's name (dot-walk), because an integration user is often not allowed to read
    the label table itself (403 'Insufficient rights')."""
    snow = _snow()
    entries = snow._request("GET", "label_entry", params={
        "sysparm_query": f"label.name={tag}", "sysparm_fields": "table_key", "sysparm_display_value": "false",
        "sysparm_limit": 5000}) or []
    keys = sorted({str(e.get("table_key") or "") for e in entries} - {""})
    out = []
    for i in range(0, len(keys), 100):                       # cmdb_ci holds every CI class
        out += snow._request("GET", "cmdb_ci", params={
            "sysparm_query": f"sys_idIN{','.join(keys[i:i + 100])}", "sysparm_fields": "sys_id,name,asset_tag",
            "sysparm_display_value": "false", "sysparm_limit": 100}) or []
    return [{"sys_id": r.get("sys_id", ""), "name": r.get("name", ""), "asset_tag": r.get("asset_tag", "")} for r in out]


def tagged_cis(force: bool = False):
    """Tagged CIs, or None when no tag is set (every server is managed). Cached for cmdb.managed_tag_ttl seconds,
    keyed on the tag so a changed setting applies at once."""
    tag = managed_tag()
    if not tag:
        return None
    with _lock:
        fresh = _CACHE["tag"] == tag and _CACHE["cis"] is not None
        if fresh and not force and time.time() - _CACHE["at"] < config.get("cmdb.managed_tag_ttl"):
            return _CACHE["cis"]
        try:
            cis = _read(tag)
        except Exception as e:                               # noqa: BLE001
            _CACHE["error"] = str(e)[:200]
            if fresh:
                log.warning("cmdb tags: ServiceNow could not be read, using the last list (%s)", _CACHE["error"])
                return _CACHE["cis"]
            raise TagsUnreadable(f"could not read the ServiceNow tag '{tag}' from the CMDB: {_CACHE['error']}") from e
        _CACHE.update(tag=tag, at=time.time(), cis=cis, error="")
        return cis


def ec2_is_managed(inst: dict, cis=None) -> bool:
    """A tagged CI has this instance id as its asset_tag, or this instance's Name as its name."""
    cis = tagged_cis() if cis is None else cis
    if cis is None:
        return True
    iid, name = str(inst.get("instance_id", "")).lower(), str(inst.get("name", "")).lower()
    for ci in cis:
        if iid and str(ci["asset_tag"]).strip().lower() == iid:
            return True
        if name and str(ci["name"]).strip().lower() == name:
            return True
    return False


def ci_is_managed(sys_id: str, cis=None) -> bool:
    cis = tagged_cis() if cis is None else cis
    return cis is None or any(ci["sys_id"] == sys_id for ci in cis)


def not_managed_message(label: str) -> str:
    return (f"Server {label} {NOT_MANAGED}: its ServiceNow CMDB record does not have the tag "
            f"'{managed_tag()}' (Admin > Connections > Ticketing > Managed-server tag)")


def _drop_cache(key: str) -> None:
    if key == "cmdb.managed_tag":
        with _lock:
            _CACHE.update(tag=None, at=0.0, cis=None, error="")


config.on_change(_drop_cache)
