"""CMDB service context (P2a, D-18). Read-only, no LLM.

Starts from the service CI (for example the nginx application CI that runs on the host) or, when there
is none, from the host CI, and walks UP the CMDB relationships to the services that depend on it.
Reports criticality, owner, support group and health-check URL.

Env (all optional):
  CMDB_CRITICALITY_FIELDS   fields tried in order   (default business_criticality,busines_criticality,criticality)
  CMDB_URL_FIELDS           fields tried in order   (default u_health_check_url,url)
  CMDB_WALK_DEPTH           relationship levels     (default 3)
  CRITICALITY_AUTONOMY_MAX  highest criticality number that counts as critical (default 1)
"""
import os
import re
import time

from app.tools.snow import _request

_CACHE: dict = {}
TTL = 300  # seconds


def _list(name: str, default: str) -> list:
    return [f.strip() for f in os.getenv(name, default).split(",") if f.strip()]


def _ref(v) -> str:
    """A reference field comes back as {'value': sys_id, 'link': ...} or as a plain string."""
    return (v.get("value") if isinstance(v, dict) else v) or ""


def _txt(v) -> str:
    if isinstance(v, dict):
        return str(v.get("display_value") or v.get("value") or "")
    return str(v or "")


def _num(label):
    """'1 - most critical' -> 1"""
    m = re.match(r"\s*(\d)", str(label or ""))
    return int(m.group(1)) if m else None


def _find(name: str) -> list:
    rows = _request("GET", "cmdb_ci", params={
        "sysparm_query": f"name={name.replace('^', ' ')}", "sysparm_fields": "sys_id", "sysparm_limit": 10})
    return [r["sys_id"] for r in rows]


def _on_host(candidates: list, host_ids: list) -> list:
    """Several CIs can share a name (one nginx per server): keep the one that runs on this host."""
    if len(candidates) < 2 or not host_ids:
        return candidates
    rows = _request("GET", "cmdb_rel_ci", params={
        "sysparm_query": f"childIN{','.join(host_ids)}^parentIN{','.join(candidates)}",
        "sysparm_fields": "parent", "sysparm_limit": 20})
    return sorted({_ref(r.get("parent")) for r in rows} - {""}) or candidates


def _parents(ids: list) -> set:
    rows = _request("GET", "cmdb_rel_ci", params={
        "sysparm_query": f"childIN{','.join(ids)}", "sysparm_fields": "parent", "sysparm_limit": 200})
    return {p for p in (_ref(r.get("parent")) for r in rows) if p}


def _details(ids: list) -> list:
    crit_f = _list("CMDB_CRITICALITY_FIELDS", "business_criticality,busines_criticality,criticality")
    url_f = _list("CMDB_URL_FIELDS", "u_health_check_url,url")
    fields = ",".join(["sys_id", "name", "sys_class_name", "owned_by", "support_group"] + crit_f + url_f)
    merged: dict = {}
    # A field defined on a subclass is not returned by the parent table, so read both.
    for table in ("cmdb_ci", "cmdb_ci_service"):
        rows = _request("GET", table, params={
            "sysparm_query": f"sys_idIN{','.join(ids)}", "sysparm_fields": fields,
            "sysparm_display_value": "true", "sysparm_limit": 100})
        for r in rows:
            m = merged.setdefault(r["sys_id"], {})
            for k, v in r.items():
                if v not in ("", None, {}) and not m.get(k):
                    m[k] = v
    out = []
    for r in merged.values():
        out.append({
            "name": _txt(r.get("name")), "class": _txt(r.get("sys_class_name")),
            "criticality": next((_num(r.get(f)) for f in crit_f if _num(r.get(f)) is not None), None),
            "criticality_label": next((_txt(r[f]) for f in crit_f if r.get(f)), ""),
            "owner": _txt(r.get("owned_by")), "support_group": _txt(r.get("support_group")),
            "url": next((_txt(r[f]) for f in url_f if r.get(f)), "")})
    return out


def _summary(ctx: dict) -> str:
    if not ctx["services"]:
        return ""
    where = "service CI" if ctx["scope"] == "service" else "host CI, no CI is named for the service"
    parts = []
    for s in ctx["services"]:
        bits = [f"criticality {s['criticality_label'] or s['criticality'] or 'not set'}"]
        if s["owner"]:
            bits.append(f"owner {s['owner']}")
        if s["support_group"]:
            bits.append(f"support group {s['support_group']}")
        if s["url"]:
            bits.append(f"health check {s['url']}")
        parts.append(f"{s['name']} ({', '.join(bits)})")
    return (f"Service context ({ctx['service'] or 'host'} on {ctx['host']}, from the {where}): "
            f"affects {'; '.join(parts)}.")


def service_context(host: str, service: str = "") -> dict:
    """Never raises. found=False with an error text when the CMDB cannot be read."""
    key = f"{host}|{service}".lower()
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out = {"host": host, "service": service, "found": False, "scope": "", "criticality": None,
           "services": [], "summary": "", "error": ""}
    try:
        host_ids = _find(host) if host else []
        start, scope = [], ""
        if service:
            start, scope = _on_host(_find(service), host_ids), "service"
        if not start and host_ids:
            start, scope = host_ids, "host"
        if start:
            seen, frontier = set(start), set(start)
            for _ in range(int(os.getenv("CMDB_WALK_DEPTH", "3"))):
                frontier = _parents(sorted(frontier)) - seen
                if not frontier:
                    break
                seen |= frontier
            above = sorted(seen - set(start))
            if above:
                out["services"] = [c for c in _details(above)
                                   if any(w in c["class"].lower() for w in ("service", "web site", "website"))
                                   or c["criticality"] is not None]
        crits = [s["criticality"] for s in out["services"] if s["criticality"] is not None]
        out.update(scope=scope, found=bool(out["services"]), criticality=min(crits) if crits else None)
        out["summary"] = _summary(out)
        _CACHE[key] = (time.time(), out)       # errors are not cached
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def critical_by_cmdb(host: str, service: str) -> tuple:
    """(is critical, reason). Only a CI named for the service counts; a host-level match never does."""
    ctx = service_context(host, service)
    limit = int(os.getenv("CRITICALITY_AUTONOMY_MAX", "1"))
    c = ctx.get("criticality")
    if ctx.get("scope") == "service" and c is not None and c <= limit:
        names = ", ".join(s["name"] for s in ctx["services"])
        return True, f"CMDB: {service} supports {names} (criticality {c})"
    return False, ""