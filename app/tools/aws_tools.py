"""Read-only AWS tools (L0). Server comes from the ticket via aws.ACTIVE, never from the model."""
import os
from datetime import datetime, timedelta, timezone

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
    """Whether the server's security groups allow inbound TCP on a port (e.g. 5985, 22, 3389), and from which sources."""
    try:
        ec2 = aws._ec2()
        inst = ec2.describe_instances(InstanceIds=[_iid(target)])["Reservations"][0]["Instances"][0]
        ids = [g["GroupId"] for g in inst["SecurityGroups"]]
        sources = []
        for sg in ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"]:
            for p in sg["IpPermissions"]:
                lo, hi = p.get("FromPort", 0), p.get("ToPort", 65535)
                if p.get("IpProtocol") in ("-1", "tcp") and lo <= port <= hi:
                    sources += [r["CidrIp"] for r in p.get("IpRanges", [])]
        return {"port": port, "allowed": bool(sources), "sources": sources}
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


AWS_TOOLS = [check_ec2_status, check_security_group, get_cloudwatch_metric]