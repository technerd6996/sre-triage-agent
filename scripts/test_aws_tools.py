"""Run from the project root:  python -m scripts.test_aws_tools
Optional: python -m scripts.test_aws_tools victim-test linux
Calls the three read-only AWS tools directly (no agent, no Colab needed)."""
import sys

from dotenv import load_dotenv

load_dotenv()

from app.agent import hosts
from app.tools import active
from app.tools.aws_tools import check_ec2_status, check_security_group, get_cloudwatch_metric

name = sys.argv[1] if len(sys.argv) > 1 else "term-win-srv"
inst, why = hosts.resolve_server(name)      # also binds the server for this thread
if not inst:
    raise SystemExit(f"No single instance matches '{name}': {why} Run: python -m scripts.test_aws")

target = inst["platform"]
port = 5985 if target == "windows" else 22
print(f"Testing {inst['name']} ({target}, {inst['instance_id']})\n")

print("check_ec2_status:", check_ec2_status.invoke({"target": target}))
print("check_security_group:", check_security_group.invoke({"target": target, "port": port}))
print("get_cloudwatch_metric:", get_cloudwatch_metric.invoke({"target": target}))