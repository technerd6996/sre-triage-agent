"""AWS EC2 Hosts adapter. Wraps app/tools/aws.py unchanged (boto3 stays lazy)."""
from __future__ import annotations

import importlib

from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable
from app.ports.hosts import CAP_FIND_IN_TEXT, CAP_FRESH_LOOKUP
from app.ports.manifest import Field, Manifest
from app.ports.models import Host

PROVIDER = "aws"

MANIFEST = Manifest(
    port="hosts",
    provider=PROVIDER,
    title="AWS EC2",
    capabilities=frozenset({CAP_FIND_IN_TEXT, CAP_FRESH_LOOKUP}),
    fields=(Field("region", "AWS region", required=True, live=False,
                  help="Credentials come from the instance role or the standard AWS environment."),),
)


def to_host(inst: dict) -> Host:
    return Host(id=inst["instance_id"], name=inst.get("name", ""), os=inst.get("platform", ""),
                state=inst.get("state", ""), address=inst.get("public_ip", ""),
                private_address=inst.get("private_ip", ""), provider=PROVIDER)


def map_error(e: Exception) -> PortError:
    if isinstance(e, PortError):
        return e
    resp = getattr(e, "response", None)
    code = str(resp.get("Error", {}).get("Code", "")) if isinstance(resp, dict) else ""
    name, msg = type(e).__name__, str(e)
    if code.startswith("InvalidInstanceID") or isinstance(e, IndexError):
        return NotFound(msg or "Instance not found")
    if code in ("AuthFailure", "UnauthorizedOperation", "InvalidClientTokenId", "ExpiredToken") \
            or name in ("NoCredentialsError", "PartialCredentialsError"):
        return AuthFailed(msg)
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                "ConnectionClosedError") or code in ("RequestLimitExceeded", "Throttling", "ServiceUnavailable"):
        return Unavailable(msg)
    return PortError(msg)


class AwsHosts:
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    @staticmethod
    def _aws():
        return importlib.import_module("app.tools.aws")

    def _run(self, fn):
        try:
            return fn(self._aws())
        except Exception as e:                      # noqa: BLE001
            raise map_error(e) from e

    def list_hosts(self, force: bool = False) -> list[Host]:
        return [to_host(i) for i in self._run(lambda a: a.list_instances(force))]

    def get_host(self, host_id: str) -> Host:
        return to_host(self._run(lambda a: a.get_instance(host_id)))

    def find_in_text(self, text: str) -> Host | None:
        inst = self._run(lambda a: a.find_in_text(text))
        return to_host(inst) if inst else None

    def connect_address(self, host_id: str) -> str:
        h = self.get_host(host_id)                  # fresh: a restarted instance has a new IP
        label = h.name or h.id
        if h.state != "running":
            raise Unavailable(f"Instance {label} is {h.state}")
        if not h.address:
            raise Unavailable(f"Instance {label} has no public IP")
        return h.address