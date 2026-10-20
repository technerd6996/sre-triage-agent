"""Show the CMDB service context the agent would use (read-only).
Run from the repo root:  python -m scripts.check_service_context victim-test nginx
"""
import json
import sys

from dotenv import load_dotenv
load_dotenv()

from app.tools import cmdb

host = sys.argv[1] if len(sys.argv) > 1 else "victim-test"
svc = sys.argv[2] if len(sys.argv) > 2 else "nginx"
ctx = cmdb.service_context(host, svc)
print(json.dumps(ctx, indent=2))
print("critical by CMDB:", cmdb.critical_by_cmdb(host, svc))