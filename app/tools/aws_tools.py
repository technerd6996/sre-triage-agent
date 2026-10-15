"""Read-only AWS tools (L0). Server comes from the ticket via aws.ACTIVE, never from the model."""
import os
from datetime import datetime, timedelta, timezone
import json
import boto3
from langchain_core.tools import tool

from app.tools import aws


def _iid(target: str) -> str:
    iid = aws.ACTIVE.get(target)
    if not iid:
        raise RuntimeError("No server was identified for this ticket.")
    return iid


@tool
def check_ec2_status(target: str) -> dict:
    """EC2 state and status checks (system and instance) of the server. target is "windows" or "linux"."""
    try:
        r = aws._ec2().describe_instance_status(
            InstanceIds=[_iid(target)], IncludeAllInstances=True)["InstanceStatuses"][0]
        return {"state": r["InstanceState"]["Name"],
                "system_status": r["SystemStatus"]["Status"],
                "instance_status": r["InstanceStatus"]["Status"]}
    except Exception as e:
        return {"error": str(e)[:300]}


@tool
def check_security_group(target: str, port: int) -> dict:
    """Whether the server's security groups allow inbound TCP on a port (e.g. 5985, 22, 3389), and from which sources.
    If the port is not allowed, also lists the inbound ports that are open."""
    try:
        ec2 = aws._ec2()
        inst = ec2.describe_instances(InstanceIds=[_iid(target)])["Reservations"][0]["Instances"][0]
        ids = [g["GroupId"] for g in inst["SecurityGroups"]]
        sources, open_ports = [], set()
        for sg in ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"]:
            for p in sg["IpPermissions"]:
                if p.get("IpProtocol") not in ("-1", "tcp"):
                    continue
                lo, hi = p.get("FromPort", 0), p.get("ToPort", 65535)
                srcs = ([r["CidrIp"] for r in p.get("IpRanges", [])]
                        + [r["CidrIpv6"] for r in p.get("Ipv6Ranges", [])]
                        + [g["GroupId"] for g in p.get("UserIdGroupPairs", [])]
                        + [x["PrefixListId"] for x in p.get("PrefixListIds", [])])
                open_ports.add(f"{lo}-{hi}" if lo != hi else str(lo))
                if lo <= port <= hi:
                    sources += srcs
        out = {"port": port, "allowed": bool(sources), "sources": sources}
        if not sources:
            out["open_inbound_ports"] = sorted(open_ports)
        return out
    except Exception as e:
        return {"error": str(e)[:300]}

@tool
def get_cloudwatch_metric(target: str, metric: str = "CPUUtilization", minutes: int = 30) -> dict:
    """Recent CloudWatch EC2 metric for the server (5-minute points). metric is CPUUtilization or StatusCheckFailed."""
    if metric not in ("CPUUtilization", "StatusCheckFailed"):
        return {"error": "metric must be CPUUtilization or StatusCheckFailed"}
    try:
        cw = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION"))
        end = datetime.now(timezone.utc)
        pts = cw.get_metric_statistics(
            Namespace="AWS/EC2", MetricName=metric,
            Dimensions=[{"Name": "InstanceId", "Value": _iid(target)}],
            StartTime=end - timedelta(minutes=min(minutes, 180)), EndTime=end,
            Period=300, Statistics=["Average", "Maximum"])["Datapoints"]
        pts.sort(key=lambda d: d["Timestamp"])
        return {"metric": metric,
                "points": [{"avg": round(p["Average"], 1), "max": round(p["Maximum"], 1)} for p in pts]}
    except Exception as e:
        return {"error": str(e)[:300]}


@tool
def escalate_l3(number: str, reason: str) -> dict:
    """Page the on-call engineers for a CRITICAL (L3) incident: major outage or anything unsafe to fix automatically.
    This only sends a notification and fixes nothing. Also hand the ticket over with escalate_incident."""
    try:
        lam = boto3.client("lambda", region_name=os.getenv("AWS_REGION"))
        r = lam.invoke(
            FunctionName=os.getenv("L3_LAMBDA", "triage1-l3-notify"),
            Payload=json.dumps({"ticket": number, "reason": reason, "server": dict(aws.ACTIVE)}).encode(),
        )
        if r.get("FunctionError"):
            return {"error": r["Payload"].read().decode()[:300]}
        return {"notified": True, "ticket": number}
    except Exception as e:
        return {"error": str(e)[:300]}


AWS_TOOLS = [check_ec2_status, check_security_group, get_cloudwatch_metric, escalate_l3]