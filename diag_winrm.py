from dotenv import load_dotenv
load_dotenv()

from app.tools import infra, active, aws

inst = aws.find_in_text("term-win-srv")
print("instance:", inst)

active.STRICT = True
active.bind({"windows": inst["instance_id"]})

try:
    print("address:", active.current_address("windows"))
    out = infra._win("Get-Service -Name 'AppXSvc' | Select-Object Name,Status | ConvertTo-Json -Compress")
    print("OK:", out)
except Exception as e:
    print(type(e).__name__, repr(e))