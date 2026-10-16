"""AWS lookups: find the EC2 instance a ticket is about and get its CURRENT public IP.
Public IPs change when an instance is stopped and started, so we ask AWS each time.
Needs only ec2:DescribeInstances."""
import os
import re
import time
from functools import cache

import boto3

STRICT = False  # the agent sets this True: no fallback to the static hosts in .env
ACTIVE = {}     # platform ("windows"/"linux") -> instance_id of the server the current ticket is about
_CACHE = {"at": 0.0, "instances": []}
_TTL = 30  # seconds


from botocore.config import Config

_CFG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=15)


@cache
def _ec2():
    return boto3.client("ec2", region_name=os.getenv("AWS_REGION"), config=_CFG)


def _flatten(inst: dict) -> dict:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    return {
        "instance_id": inst["InstanceId"],
        "name": tags.get("Name", ""),
        "state": inst["State"]["Name"],
        "platform": "windows" if inst.get("Platform") == "windows" else "linux",
        "public_ip": inst.get("PublicIpAddress", ""),
        "private_ip": inst.get("PrivateIpAddress", ""),
    }


def list_instances(force: bool = False) -> list:
    if force or time.time() - _CACHE["at"] > _TTL:
        pages = _ec2().get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name",
                      "Values": ["pending", "running", "stopping", "stopped"]}]
        )
        found = [_flatten(i) for p in pages for r in p["Reservations"] for i in r["Instances"]]
        _CACHE.update(at=time.time(), instances=found)
    return _CACHE["instances"]


def get_instance(instance_id: str) -> dict:
    """Fresh data for one instance (skips the cache, so a restarted instance shows its new IP)."""
    res = _ec2().describe_instances(InstanceIds=[instance_id])
    return _flatten(res["Reservations"][0]["Instances"][0])


def find_in_text(text: str):
    """The one instance whose Name tag, instance id or IP appears in the text. None if zero or several match."""
    low = text.lower()
    hits = []
    for inst in list_instances():
        for tok in (inst["name"], inst["instance_id"], inst["public_ip"], inst["private_ip"]):
            if tok and re.search(rf"(?<![\w.-]){re.escape(tok.lower())}(?![\w-])", low):
                hits.append(inst)
                break
    return hits[0] if len(hits) == 1 else None


def current_host(target: str):
    """Public IP of the active instance for this platform, or None if no instance was resolved."""
    iid = ACTIVE.get(target)
    if not iid:
        return None
    inst = get_instance(iid)
    label = inst["name"] or iid
    if inst["state"] != "running":
        raise RuntimeError(f"Instance {label} is {inst['state']}")
    if not inst["public_ip"]:
        raise RuntimeError(f"Instance {label} has no public IP")
    return inst["public_ip"]