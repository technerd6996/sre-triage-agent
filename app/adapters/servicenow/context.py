"""ServiceNow CMDB Context adapter. Wraps app/tools/cmdb.py unchanged."""
from __future__ import annotations

import importlib

from app.ports.manifest import Field, Manifest

PROVIDER = "servicenow"

MANIFEST = Manifest(
    port="context",
    provider=PROVIDER,
    title="ServiceNow CMDB",
    capabilities=frozenset({"parents", "criticality", "owner", "health_url"}),
    fields=(
        Field("criticality_fields", "Criticality fields (in order)",
              default="business_criticality,busines_criticality,criticality"),
        Field("url_fields", "Health-check URL fields (in order)", default="u_health_check_url,url"),
        Field("walk_depth", "Relationship levels to walk", kind="number", default="3"),
    ),
)


class ServiceNowContext:
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    @staticmethod
    def _cmdb():
        return importlib.import_module("app.tools.cmdb")

    def service_context(self, host: str, service: str = "") -> dict:
        return self._cmdb().service_context(host, service)     # never raises; found=False + error text

    def is_critical(self, host: str, service: str) -> tuple[bool, str]:
        try:
            return self._cmdb().critical_by_cmdb(host, service)
        except Exception:                                      # noqa: BLE001
            return False, ""